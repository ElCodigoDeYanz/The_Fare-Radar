"""The shipped catalogue of sources, how it is selected and imported, and the politeness-first verifier.
No real site is contacted: every response is canned."""

import json
import random
import re
import time
import pytest
from radar import sources as src, verify
from radar.identity import gtin_key
from radar.providers.base import ProviderError
from test_discovery import ROBOTS, index, pages, product_page, site, urlset
from test_providers import ld

NOW = time.time()
VALID_EAN13 = "4006381333931"


def good_source(**kw):
    base = {
        "id": "shop", "domain": "shop.test", "currency": "PEN", "name": "Shop", "kind": "store", "country": "PE",
        "catalogs": [{"category": "tecnologia", "subcategory": "laptops", "include": "laptop"}],
    }
    return base | kw


# --- the catalogue that ships with the project ----------------------------------------------------------------
def test_the_shipped_catalogue_loads_and_is_internally_consistent():
    sources = src.load_sources()
    stores = [s for s in sources.values() if s["kind"] == "store"]
    travel = [s for s in sources.values() if s["kind"] != "store"]
    assert len(stores) >= 90 and len(travel) >= 60 and len(sources) == len(stores) + len(travel)
    assert {s["kind"] for s in travel} <= {"bus", "airline", "agency"}
    for s in stores:
        assert s["currency"] and s["domain"] == s["domain"].lower() and "/" not in s["domain"] and s["catalogs"]
        assert s["status"] == "unverified"  # nothing shipped claims to have been checked against the real site
    for s in travel:
        assert s["connector"] is None and s["status"] == "sin_conector"  # honest: no fake price source for them
    domains = [s["domain"] for s in sources.values()]
    assert len(domains) == len(set(domains))  # one entry per site
    assert len({s["id"] for s in sources.values()}) == len(sources)


def test_every_include_pattern_compiles_and_cannot_stall_the_matcher():
    templates = json.loads((src.SOURCES_DIR / "categories.json").read_text())["templates"]
    probe = "a" * 4000 + "/" + "-".join(["laptop"] * 400)
    started = time.perf_counter()
    for name, tpl in templates.items():
        rx = re.compile(tpl["include"], re.I)
        rx.search(probe)
        assert tpl["category"] and tpl["subcategory"], name
    assert time.perf_counter() - started < 1.0  # no catastrophic backtracking in any shipped pattern


def test_the_shipped_files_carry_no_secrets_proxies_or_evasion_settings():
    for name in ("stores.json", "travel.json", "categories.json"):
        text = (src.SOURCES_DIR / name).read_text().lower()
        for word in ("proxy", "token", "password", "secret", "api_key", "apikey", "captcha", "stealth", "user-agent", "rotate"):
            assert word not in text, f"{name} mentions {word}"


def test_every_country_used_has_a_currency_and_the_groups_expand_to_real_categories():
    data = json.loads((src.SOURCES_DIR / "stores.json").read_text())
    countries = {s["country"] for s in data["sources"]}
    assert countries <= set(data["currency_by_country"])
    sources = src.load_sources()
    assert all(len(s["catalogs"]) == len({c["subcategory"] for c in s["catalogs"]}) for s in sources.values() if s["kind"] == "store")


# --- loading errors are loud ----------------------------------------------------------------------------------
def write_dir(tmp_path, stores, categories=None, travel=None):
    categories = categories or {
        "templates": {"laptops": {"category": "tecnologia", "subcategory": "laptops", "include": "laptop"}},
        "groups": {"@pc": ["laptops"]},
    }
    (tmp_path / "categories.json").write_text(json.dumps(categories))
    (tmp_path / "stores.json").write_text(json.dumps({"currency_by_country": {"PE": "PEN"}, "sources": stores}))
    if travel is not None:
        (tmp_path / "travel.json").write_text(json.dumps({"sources": travel}))
    return tmp_path


def raw_store(**kw):
    return {"id": "ok-pe", "name": "OK", "kind": "store", "domain": "ok.example", "country": "PE", "cats": ["@pc"], "status": "unverified"} | kw


def test_a_minimal_valid_directory_loads(tmp_path):
    loaded = src.load_sources(write_dir(tmp_path, [raw_store()]))
    assert loaded["ok-pe"]["currency"] == "PEN" and loaded["ok-pe"]["catalogs"][0]["subcategory"] == "laptops"


@pytest.mark.parametrize(
    "mutation,message",
    [
        ({"id": "Bad Id"}, "id inválido"),
        ({"id": "x"}, "id inválido"),
        ({"kind": "supermarket"}, "kind inválido"),
        ({"domain": "no spaces allowed.example"}, "dominio inválido"),
        ({"domain": "127.0.0.1"}, "dominio inválido"),
        ({"country": "ZZ"}, "sin moneda"),
        ({"cats": ["@nope"]}, "categoría desconocida"),
    ],
)
def test_a_malformed_entry_fails_loudly_never_silently(tmp_path, mutation, message):
    with pytest.raises(src.SourceError, match=message):
        src.load_sources(write_dir(tmp_path, [raw_store(**mutation)]))


def test_duplicate_ids_are_refused(tmp_path):
    with pytest.raises(src.SourceError, match="repetido"):
        src.load_sources(write_dir(tmp_path, [raw_store(), raw_store(domain="other.example")]))


def test_a_category_that_does_not_exist_in_the_product_catalogue_is_refused(tmp_path):
    categories = {"templates": {"x": {"category": "tecnologia", "subcategory": "no-existe", "include": "x"}}, "groups": {}}
    with pytest.raises(Exception, match="no-existe|subcategor|categor"):
        src.load_sources(write_dir(tmp_path, [raw_store(cats=["x"])], categories))


# --- selecting what to monitor --------------------------------------------------------------------------------
def test_selectors_pick_only_stores_and_never_travel_entries():
    sources = src.load_sources()
    everything = src.select(sources, "all")
    assert len(everything) == sum(1 for s in sources.values() if s["kind"] == "store")
    pe = src.select(sources, "country=PE")
    assert pe and all(s["country"] == "PE" and s["kind"] == "store" for s in pe) and len(pe) < len(everything)
    food = src.select(sources, "cat=alimentos")
    assert food and all(any(c["category"] == "alimentos" for c in s["catalogs"]) for s in food)
    assert {s["id"] for s in src.select(sources, "falabella-pe, wong-pe")} == {"falabella-pe", "wong-pe"}
    assert src.select(sources, "") == [] and src.select(sources, None) == []
    assert src.select(sources, "cruzdelsur-pe") == []  # a bus line has no connector: it cannot be "enabled" as a store
    assert len(src.select(sources, "all,country=PE,falabella-pe")) == len(everything)  # union, no duplicates


@pytest.mark.parametrize("spec", ["nope-xx", "color=red", "all,ghost"])
def test_unknown_selectors_and_ids_are_errors(spec):
    with pytest.raises(src.SourceError):
        src.select(src.load_sources(), spec)


def test_allowed_domains_lists_each_selected_host_once_sorted():
    sources = src.load_sources()
    chosen = src.select(sources, "country=PE")
    domains = src.allowed_domains(chosen + chosen)
    assert domains == sorted(set(domains)) and set(domains) == {s["domain"] for s in chosen}


# --- importing catalogs ------------------------------------------------------------------------------------------
def catalogs(db):
    with db.connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM catalogs ORDER BY source_id")]


def test_import_creates_one_sitemap_catalog_per_store_with_category_rules_and_a_spread_start(db):
    chosen = src.select(src.load_sources(), "country=PE")
    rng = random.Random(4)
    result = src.import_catalogs(db, chosen, NOW, rng, items_per_category=7, store_max=30)
    assert result == {"created": len(chosen), "existing": 0, "stores": len(chosen)}
    rows = catalogs(db)
    assert len(rows) == len(chosen)
    for row in rows:
        source = next(s for s in chosen if s["id"] == row["source_id"])
        assert (row["mode"], row["provider"], row["url"], row["max_items"]) == ("sitemap", "store", f"https://{source['domain']}/", 7)
        assert row["currency"] == source["currency"] and row["alert_mode"] == "smart" and row["channel"] == "simulation" and row["enabled"] == 1
        rules = json.loads(row["rules_json"])
        assert len(rules) == len(source["catalogs"]) and all(r["include"] for r in rules)
        assert NOW + 300 <= row["next_run_at"] <= NOW + 3600  # first sweeps are spread out, never all at once
    assert len({round(r["next_run_at"]) for r in rows}) > len(rows) // 2


def test_import_is_idempotent_and_never_duplicates_or_resets_a_store(db):
    chosen = src.select(src.load_sources(), "falabella-pe,wong-pe")
    first = src.import_catalogs(db, chosen, NOW, random.Random(1))
    before = catalogs(db)
    again = src.import_catalogs(db, chosen, NOW + 999, random.Random(2))
    assert first["created"] == 2 and again == {"created": 0, "existing": 2, "stores": 2}
    assert catalogs(db) == before


def test_import_can_be_limited_to_some_categories_and_skips_stores_without_them(db):
    sources = src.load_sources()
    chosen = src.select(sources, "country=PE")
    result = src.import_catalogs(db, chosen, NOW, random.Random(1), only_cats={"alimentos"})
    rows = catalogs(db)
    assert 0 < result["created"] <= len(chosen) and len(rows) == result["created"]
    assert all({r["category"] for r in json.loads(row["rules_json"])} == {"alimentos"} for row in rows)


def test_a_per_store_item_cap_in_the_catalogue_overrides_the_global_default(db):
    chosen = [good_source(max_items=5)]
    src.import_catalogs(db, chosen, NOW, random.Random(1), store_max=40)
    assert catalogs(db)[0]["store_max"] == 5


def test_catalogs_only_accept_smart_alerts_because_discovered_products_have_no_target(db):
    with pytest.raises(src.SourceError, match="smart"):
        src.import_catalogs(db, [good_source()], NOW, random.Random(1), alert_mode="target")
    assert catalogs(db) == []


def test_the_channel_is_stored_so_telegram_stays_opt_in(db):
    src.import_catalogs(db, [good_source()], NOW, random.Random(1), channel="telegram")
    assert catalogs(db)[0]["channel"] == "telegram"


# --- the verifier -------------------------------------------------------------------------------------------------
def sitemap_site(product_html, *, robots=ROBOTS, **kw):
    routes = {
        "/robots.txt": (200, robots, {}),
        "/sitemap_index.xml": (200, urlset("https://shop.test/p/laptop-1", "https://shop.test/p/laptop-2", "https://shop.test/blog/a"), {}),
        "/p/laptop-1": (200, product_html, {}),
        "/p/laptop-2": (200, product_html, {}),
    }
    return site(routes, **kw)


def run_verify(provider, **kw):
    pauses = []
    report = verify.verify_store(provider, good_source(**kw), NOW, lambda: pauses.append(1))
    return report, pauses


def product_with_ids():
    page = ld({"@type": "Offer", "price": "1299.90", "priceCurrency": "PEN", "availability": "https://schema.org/InStock"})
    return page.replace('"sku": "SKU1"', f'"sku": "SKU1", "gtin13": "{VALID_EAN13}"')


def test_a_store_that_publishes_everything_is_usable_and_comparable():
    assert gtin_key(VALID_EAN13)  # the fixture itself is a valid GTIN
    provider, seen = sitemap_site(product_with_ids())
    report, pauses = run_verify(provider)
    assert report["verdict"] == "usable"
    assert report["exact_price"] and report["currency"] == "PEN" and report["currency_matches_catalog"]
    assert report["availability_published"] and report["comparable_across_stores"]
    assert report["sitemap_urls"] == 3 and report["routed_urls"] == 2  # the blog URL is in the sitemap but matched no rule
    assert report["sample_url"] == "https://shop.test/p/laptop-1"
    assert len(pages(seen)) == 2 and len(pauses) >= 2  # one sitemap + ONE sample product, with pauses in between


def test_a_price_in_another_currency_is_reported_not_converted():
    page = ld({"@type": "Offer", "price": "300", "priceCurrency": "USD", "availability": "InStock"})
    report, _ = run_verify(sitemap_site(page)[0])
    assert report["verdict"] == "moneda_distinta" and report["exact_price"] and report["currency"] == "USD"
    assert report["currency_matches_catalog"] is False


def test_a_price_range_is_not_an_exact_price():
    page = ld({"@type": "AggregateOffer", "lowPrice": "10", "highPrice": "20", "priceCurrency": "PEN"})
    report, _ = run_verify(sitemap_site(page)[0])
    assert report["verdict"] == "precio_no_exacto" and report["only_price_range"] and not report["exact_price"]


def test_pages_without_json_ld_prices_are_reported_as_such():
    report, _ = run_verify(sitemap_site("<html><head><title>x</title></head><body>" + "<p>precio en pantalla</p>" * 40 + "</body></html>")[0])
    assert report["verdict"] == "sin_json_ld"


def test_a_sitemap_without_recognisable_products_is_reported():
    routes = {"/robots.txt": (200, ROBOTS, {}), "/sitemap_index.xml": (200, urlset("https://shop.test/blog/a", "https://shop.test/contacto"), {})}
    report, _ = run_verify(site(routes)[0])
    assert report["verdict"] == "sin_productos_reconocibles" and report["routed_urls"] == 0


@pytest.mark.parametrize("status,body", [(403, "forbidden"), (429, ""), (401, ""), (200, "<html>" + "x" * 500 + " captcha </html>")])
def test_an_explicit_refusal_is_reported_as_blocked_and_nothing_else_is_asked(status, body):
    routes = {"/robots.txt": (200, ROBOTS, {}), "/sitemap_index.xml": (status, body, {"content-type": "text/html"})}
    provider, seen = site(routes)
    report, _ = run_verify(provider)
    assert report["verdict"] == "bloqueado" and report["error"]["kind"] in ("denied", "rate_limited", "session", "captcha")
    assert pages(seen) == ["https://shop.test/sitemap_index.xml"]  # one request, then it stops: no second try, no other path


def test_a_site_that_cannot_be_read_is_not_verifiable_rather_than_guessed():
    routes = {"/robots.txt": (200, ROBOTS, {}), "/sitemap_index.xml": (500, "", {})}
    report, _ = run_verify(site(routes)[0])
    assert report["verdict"] == "no_verificable" and report["error"]["kind"] == "transient"


def test_a_domain_that_is_not_allowed_is_not_verifiable_and_gets_no_request():
    provider, seen = sitemap_site(product_with_ids(), allowed=("other.test",))
    report, _ = run_verify(provider)
    assert report["verdict"] == "no_verificable" and seen == []


def test_robots_txt_that_forbids_the_sample_is_respected_by_the_verifier():
    provider, seen = sitemap_site(product_with_ids(), robots=ROBOTS + "Disallow: /p/\n")
    report, _ = run_verify(provider)
    assert report["verdict"] == "no_verificable" and not any("/p/" in u for u in pages(seen))


def test_verify_all_summarises_and_can_stop_at_the_first_block():
    blocked = {"/robots.txt": (200, ROBOTS, {}), "/sitemap_index.xml": (403, "no", {})}
    provider, seen = site(blocked)
    sources = [good_source(id=f"s{i}") for i in range(3)]
    waits = []
    full = verify.verify_all(provider, sources, NOW, lambda: None, between=lambda: waits.append(1))
    assert full["summary"] == {"bloqueado": 3} and len(waits) == 3
    seen.clear()
    cut = verify.verify_all(provider, sources, NOW, lambda: None, between=lambda: waits.append(1), stop_on_block=True)
    assert cut["summary"] == {"bloqueado": 1} and len(cut["sources"]) == 1


def test_a_verify_report_never_contains_the_contact_or_credentials():
    report, _ = run_verify(sitemap_site(product_with_ids())[0])
    text = json.dumps(report)
    assert "ops@example.org" not in text and "password" not in text.lower() and "token" not in text.lower()
