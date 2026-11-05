"""Store connector against canned HTTP responses. No real site or API is contacted."""

import json
import time
import httpx
import pytest
from radar.models import Criteria, ProductCriteria
from radar.providers.base import BlockedError, ProviderError, RateLimitedError, TransientError
from radar.providers.registry import build_registry
from radar.providers.storepage import StorePageProvider, extract_offers, parse_price
from radar.service import add_watch, refresh
from conftest import product_watch

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731 - a public address
NOW = time.time()


def ld(offer, extra=""):
    return (
        '<html><head><script type="application/ld+json">'
        + json.dumps({"@context": "https://schema.org", "@type": "Product", "name": "X", "sku": "SKU1", "offers": offer})
        + "</script>" + extra + "</head><body>ok</body></html>"
    )


GOOD = ld({"@type": "Offer", "price": "1299.90", "priceCurrency": "PEN", "availability": "https://schema.org/InStock",
           "itemCondition": "https://schema.org/NewCondition", "seller": {"@type": "Organization", "name": "Tienda SA"}})


def store(routes, allowed=("shop.test",), **kw):
    """routes: {path: (status, body, headers)} served for any host; also records requests."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        status, body, headers = routes.get(request.url.path, (404, "", {}))
        headers = {"content-type": "text/html; charset=utf-8"} | dict(headers)
        return httpx.Response(status, text=body, headers=headers)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    provider = StorePageProvider(allowed, contact="ops@example.org", client=client, resolver=kw.pop("resolver", PUBLIC), **kw)
    return provider, seen


def crit(path="/p/1", **kw):
    return ProductCriteria(url=f"https://shop.test{path}", currency="PEN", **kw)


def test_reads_exact_price_from_json_ld_and_keeps_unknowns_explicit():
    provider, seen = store({"/robots.txt": (200, "User-agent: *\nAllow: /", {}), "/p/1": (200, GOOD, {})})
    [offer] = provider.fetch(crit(), "x", NOW)
    assert offer.total_cents == 129990 and offer.currency == "PEN" and offer.synthetic is False
    assert offer.conditions == {
        "product_ref": "https://shop.test/p/1", "sku": "SKU1", "seller": "Tienda SA",
        "condition": "new", "availability": "in_stock",
    }
    # Same page twice -> same identity (so the pipeline stores ONE observation, not one per check).
    [again] = provider.fetch(crit(), "x", NOW + 600)
    assert again.provider_observation_id == offer.provider_observation_id
    assert offer.expires_at == NOW + 1200


def test_condition_and_seller_not_stated_are_marked_not_assumed():
    page = ld({"@type": "Offer", "price": 50, "priceCurrency": "PEN", "availability": "InStock"})
    provider, _ = store({"/p/1": (200, page, {})})
    [offer] = provider.fetch(crit(), "x", NOW)
    assert offer.conditions["condition"] == "unspecified"  # never silently "new"
    assert offer.conditions["seller"] == "store:shop.test"


@pytest.mark.parametrize(
    "raw,cents",
    [("1299.90", 129990), (1299.9, 129990), ("1,299.00", 129900), ("0.99", 99), ("10", 1000),
     ("1.299,00", None), ("abc", None), ("0", None), ("-5", None), (True, None), (None, None), ("", None), ("1e3", None)],
)
def test_price_parsing_refuses_anything_ambiguous(raw, cents):
    assert parse_price(raw) == cents


def test_robots_txt_is_honoured_and_the_page_is_never_requested():
    provider, seen = store({"/robots.txt": (200, "User-agent: *\nDisallow: /p/", {}), "/p/1": (200, GOOD, {})})
    with pytest.raises(ProviderError, match="robots.txt"):
        provider.fetch(crit(), "x", NOW)
    assert not any(u.endswith("/p/1") for u in seen)


def test_robots_unavailable_semantics():
    ok, _ = store({"/p/1": (200, GOOD, {})})  # robots 404 -> no rules published -> allowed
    assert ok.fetch(crit(), "x", NOW)[0].total_cents == 129990
    down, seen = store({"/robots.txt": (503, "", {}), "/p/1": (200, GOOD, {})})
    with pytest.raises(TransientError):
        down.fetch(crit(), "x", NOW)
    assert not any(u.endswith("/p/1") for u in seen)


def test_crawl_delay_raises_the_minimum_gap_hint():
    provider, _ = store({"/robots.txt": (200, "User-agent: *\nCrawl-delay: 90", {}), "/p/1": (200, GOOD, {})})
    provider.fetch(crit(), "x", NOW)
    assert provider.min_gap_hint(crit()) == 90.0


def test_user_agent_identifies_the_monitor_and_has_no_browser_disguise():
    provider, _ = store({})
    ua = provider.user_agent
    assert "RadarDePassages" in ua and "ops@example.org" in ua and "robots.txt" in ua
    assert "Mozilla" not in ua and "Chrome" not in ua


@pytest.mark.parametrize(
    "status,headers,error",
    [(403, {}, BlockedError), (429, {"Retry-After": "120"}, RateLimitedError),
     (503, {}, TransientError), (500, {}, TransientError)],
)
def test_pushback_is_reported_as_a_typed_error(status, headers, error):
    provider, _ = store({"/p/1": (status, "denied", headers)})
    with pytest.raises(error) as caught:
        provider.fetch(crit(), "x", NOW)
    if status == 429:
        assert caught.value.retry_after == 120


def test_503_with_challenge_text_is_a_block_not_an_outage():
    provider, _ = store({"/p/1": (503, "<title>Just a moment...</title>", {})})
    with pytest.raises(BlockedError):
        provider.fetch(crit(), "x", NOW)


def test_challenge_page_served_with_200_is_a_block():
    provider, _ = store({"/p/1": (200, "<html>Please complete the CAPTCHA to continue</html>", {})})
    with pytest.raises(BlockedError):
        provider.fetch(crit(), "x", NOW)


def test_missing_product_is_an_error_not_a_block():
    provider, _ = store({"/p/1": (404, "", {})})
    with pytest.raises(ProviderError, match="404") as caught:
        provider.fetch(crit(), "x", NOW)
    assert not isinstance(caught.value, BlockedError)


def test_price_from_range_is_rejected_as_ambiguous():
    page = ld({"@type": "AggregateOffer", "lowPrice": "100", "highPrice": "900", "priceCurrency": "PEN"})
    provider, _ = store({"/p/1": (200, page, {})})
    with pytest.raises(ProviderError, match="desde"):
        provider.fetch(crit(), "x", NOW)


def test_several_different_offers_need_a_sku_to_choose():
    page = ld([
        {"@type": "Offer", "sku": "A", "price": "10", "priceCurrency": "PEN", "availability": "InStock"},
        {"@type": "Offer", "sku": "B", "price": "20", "priceCurrency": "PEN", "availability": "InStock"},
    ])
    provider, _ = store({"/p/1": (200, page, {})})
    with pytest.raises(ProviderError, match="SKU"):
        provider.fetch(crit(), "x", NOW)
    [offer] = provider.fetch(crit(sku="B"), "x", NOW)
    assert offer.total_cents == 2000


def test_page_without_price_or_currency_is_an_error():
    page = "<html><head><title>Producto</title></head><body>" + "<p>sin datos estructurados</p>" * 20 + "</body></html>"
    provider, _ = store({"/p/1": (200, page, {})})
    with pytest.raises(ProviderError, match="JSON-LD"):
        provider.fetch(crit(), "x", NOW)
    nocur = ld({"@type": "Offer", "price": "10", "availability": "InStock"})
    provider, _ = store({"/p/1": (200, nocur, {})})
    with pytest.raises(ProviderError, match="moneda"):
        provider.fetch(crit(), "x", NOW)


def test_only_allow_listed_domains_are_ever_contacted():
    provider, seen = store({}, allowed=("other.test",))
    with pytest.raises(ProviderError, match="no permitido"):
        provider.fetch(crit(), "x", NOW)
    assert seen == []


def test_subdomains_of_an_allowed_domain_are_allowed_lookalikes_are_not():
    provider, _ = store({}, allowed=("shop.test",))
    assert provider.allows("www.shop.test") and provider.allows("shop.test")
    assert not provider.allows("evilshop.test") and not provider.allows("shop.test.evil.com")


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "fd00::1"])
def test_domains_resolving_to_private_addresses_are_refused_without_a_request(address):
    provider, seen = store({"/p/1": (200, GOOD, {})}, resolver=lambda host: [address])
    with pytest.raises(ProviderError, match="no pública"):
        provider.fetch(crit(), "x", NOW)
    assert seen == []


def test_redirects_are_followed_only_inside_allowed_domains_over_https():
    provider, seen = store({
        "/p/1": (302, "", {"Location": "https://evil.test/steal"}),
    })
    with pytest.raises(ProviderError, match="no permitido"):
        provider.fetch(crit(), "x", NOW)
    assert not any("evil.test" in u for u in seen)
    provider, _ = store({"/p/1": (301, "", {"Location": "/p/2"}), "/p/2": (200, GOOD, {})})
    assert provider.fetch(crit(), "x", NOW)[0].total_cents == 129990
    provider, _ = store({"/p/1": (301, "", {"Location": "http://shop.test/p/2"})})
    with pytest.raises(ProviderError, match="https"):
        provider.fetch(crit(), "x", NOW)


def test_oversized_pages_are_truncated_not_loaded_whole():
    big = GOOD + ("x" * (4 * 1024 * 1024))
    provider, _ = store({"/p/1": (200, big, {})})
    assert provider.fetch(crit(), "x", NOW)[0].total_cents == 129990


def test_network_failure_is_transient():
    def boom(request):
        raise httpx.ConnectTimeout("slow")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    provider = StorePageProvider(["shop.test"], client=client, resolver=PUBLIC)
    with pytest.raises(TransientError):
        provider.fetch(crit(), "x", NOW)


def test_extract_ignores_malformed_blocks_and_finds_graph_products():
    html = (
        '<script type="application/ld+json">{not json</script>'
        '<script type="application/ld+json">' + json.dumps({"@graph": [
            {"@type": "BreadcrumbList"},
            {"@type": ["Product", "Thing"], "sku": "G", "offers": {"price": "5", "priceCurrency": "usd", "availability": "https://schema.org/OutOfStock"}},
        ]}) + "</script>"
    )
    offers, ambiguous = extract_offers(html)
    assert len(offers) == 1 and offers[0]["currency"] == "USD" and offers[0]["availability"] == "out_of_stock"


# --- through the real pipeline ----------------------------------------------------------------
def test_pipeline_rejects_a_page_priced_in_another_currency(db):
    usd = ld({"@type": "Offer", "price": "10", "priceCurrency": "USD", "availability": "InStock"})
    provider, _ = store({"/p/1": (200, usd, {})})
    key = add_watch(db, product_watch(1, provider="store"))
    result = refresh(db, key, provider=provider, now=NOW)
    assert result["status"] == "error" and result["error_kind"] == "invalid"
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_same_price_is_one_observation_and_a_drop_is_a_new_one_with_exact_change(db):
    price = {"v": "1000.00"}

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=ld({"@type": "Offer", "price": price["v"], "priceCurrency": "PEN", "availability": "InStock", "seller": {"name": "S"}}), headers={"content-type": "text/html"})

    provider = StorePageProvider(["shop.test"], client=httpx.Client(transport=httpx.MockTransport(handler)), resolver=PUBLIC)
    provider.min_interval = 0
    key = add_watch(db, product_watch(1, provider="store", target=80_000))
    refresh(db, key, provider=provider, now=NOW)
    refresh(db, key, provider=provider, now=NOW + 400)  # same price again
    price["v"] = "750.00"
    out = refresh(db, key, provider=provider, now=NOW + 800)
    stats = out["observations"][0]["analytics"]
    assert stats["change_percent"] == -25.0 and stats["minimum_cents"] == 100000
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM queries WHERE status='success'").fetchone()[0] == 3
        assert con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
        message = con.execute("SELECT message FROM alerts").fetchone()[0]
    assert "https://shop.test/p/1" in message and "[RADAR]" in message and "SINTÉTICA" not in message


def test_reconfirming_a_price_extends_its_validity_so_it_is_not_shown_expired(db):
    provider, _ = store({"/p/1": (200, GOOD, {})})
    provider.min_interval = 0
    key = add_watch(db, product_watch(1, provider="store", target=1))
    refresh(db, key, provider=provider, now=NOW)
    refresh(db, key, provider=provider, now=NOW + 1000)  # still valid 1000 s later
    with db.connect() as con:
        row = con.execute("SELECT observed_at,expires_at FROM observations").fetchone()
    assert row["observed_at"] == NOW and row["expires_at"] == NOW + 1000 + 1200


def test_out_of_stock_price_never_raises_an_alert(db):
    page = ld({"@type": "Offer", "price": "10", "priceCurrency": "PEN", "availability": "OutOfStock", "seller": {"name": "S"}})
    provider, _ = store({"/p/1": (200, page, {})})
    key = add_watch(db, product_watch(1, provider="store", target=99_999))
    out = refresh(db, key, provider=provider, now=NOW)
    assert out["status"] == "success" and out["observations"][0]["alert_eligible"] is False
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_availability_not_stated_blocks_the_alert_too(db):
    page = ld({"@type": "Offer", "price": "10", "priceCurrency": "PEN", "seller": {"name": "S"}})
    provider, _ = store({"/p/1": (200, page, {})})
    key = add_watch(db, product_watch(1, provider="store", target=99_999))
    out = refresh(db, key, provider=provider, now=NOW)
    assert out["observations"][0]["alert_eligible"] is False
    assert out["observations"][0]["analytics"]["comparable_count"] == 0


# --- registry ----------------------------------------------------------------------------------
def test_registry_is_opt_in_and_demo_can_be_disabled():
    only_demo = build_registry({})
    assert [p["name"] for p in only_demo.describe()] == ["demo"] and not only_demo.any_live
    full = build_registry({"RADAR_ALLOWED_STORE_DOMAINS": "a.test, b.test", "RADAR_ENABLE_DEMO": "0"})
    assert [p["name"] for p in full.describe()] == ["store"]
    assert full.get("store").allowed == ("a.test", "b.test")
    assert full.supports("store", "product") and not full.supports("store", "flight")


# --- regressions found in review ----------------------------------------------------------------
def test_robots_txt_that_redirects_is_followed_and_still_obeyed():
    provider, seen = store({
        "/robots.txt": (301, "", {"Location": "https://shop.test/real-robots.txt"}),
        "/real-robots.txt": (200, "User-agent: *\nDisallow: /p/", {}),
        "/p/1": (200, GOOD, {}),
    })
    with pytest.raises(ProviderError, match="robots.txt"):
        provider.fetch(crit(), "x", NOW)
    assert not any(u.endswith("/p/1") for u in seen)
    allowed, _ = store({
        "/robots.txt": (301, "", {"Location": "/other-robots"}),
        "/other-robots": (200, "User-agent: *\nAllow: /", {}),
        "/p/1": (200, GOOD, {}),
    })
    assert allowed.fetch(crit(), "x", NOW)[0].total_cents == 129990


def test_robots_redirects_cannot_leave_the_allow_list_or_loop():
    provider, seen = store({"/robots.txt": (302, "", {"Location": "https://evil.test/robots.txt"}), "/p/1": (200, GOOD, {})})
    with pytest.raises(ProviderError, match="no permitido"):
        provider.fetch(crit(), "x", NOW)
    assert not any("evil.test" in u or u.endswith("/p/1") for u in seen)
    loop, seen = store({"/robots.txt": (302, "", {"Location": "/robots.txt"}), "/p/1": (200, GOOD, {})})
    with pytest.raises(TransientError):
        loop.fetch(crit(), "x", NOW)
    assert not any(u.endswith("/p/1") for u in seen)
    plain, _ = store({"/robots.txt": (302, "", {"Location": "http://shop.test/robots.txt"})})
    with pytest.raises(ProviderError, match="https"):
        plain.fetch(crit(), "x", NOW)


@pytest.mark.parametrize(
    "address",
    ["64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b::a9fe:a9fe", "64:ff9b:1::1", "::ffff:127.0.0.1", "::ffff:10.1.2.3", "0.0.0.0", "::", "100.64.0.1"],
)
def test_embedded_ipv4_forms_are_judged_by_the_address_they_hide(address):
    from radar.providers.storepage import is_public_address

    assert is_public_address(address) is False
    provider, seen = store({"/p/1": (200, GOOD, {})}, resolver=lambda host: [address])
    with pytest.raises(ProviderError, match="no pública"):
        provider.fetch(crit(), "x", NOW)
    assert seen == []


def test_ordinary_public_addresses_still_pass():
    from radar.providers.storepage import is_public_address

    assert is_public_address("93.184.216.34") and is_public_address("64:ff9b::5db8:d822") and is_public_address("2606:2800:220:1:248:1893:25c8:1946")
