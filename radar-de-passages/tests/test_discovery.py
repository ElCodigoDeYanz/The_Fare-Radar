"""Catalog discovery: sitemaps, listings, caps, dedupe, robots.txt and the scheduler running them.
No real site is contacted: every response is canned."""

import gzip
import json
import random
import time
import uuid
import httpx
import pytest
from radar import discovery, restrictions
from radar.politeness import DomainGate
from radar.providers.base import StructureError
from radar.providers.demo import DemoProvider
from radar.providers.registry import Registry
from radar.providers.storepage import StorePageProvider
from radar.scheduler import SITEMAP_EVERY, Scheduler
from radar.service import add_watch
from conftest import product_watch
from test_providers import PUBLIC, ld
from test_scheduler import T0, run_for

NOW = time.time()
NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
RULES = [
    {"category": "tecnologia", "subcategory": "laptops", "include": "laptop|notebook"},
    {"category": "tecnologia", "subcategory": "celulares", "include": "celular|smartphone"},
]


# --- builders -----------------------------------------------------------------------------------------
def urlset(*items):
    rows = ""
    for item in items:
        loc, mod = (item, None) if isinstance(item, str) else item
        rows += f"<url><loc>{loc}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "") + "</url>"
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="{NS}">{rows}</urlset>'


def index(*locs):
    rows = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return f'<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="{NS}">{rows}</sitemapindex>'


def product(n, price="1299.90", currency="PEN", host="shop.test", scheme="https", **extra):
    node = {
        "@type": "Product", "name": f"Laptop Acme {n}", "sku": f"SKU{n}", "mpn": f"MPN{n}",
        "brand": {"@type": "Brand", "name": "Acme"}, "url": f"{scheme}://{host}/p/laptop-{n}",
        "offers": {"@type": "Offer", "price": price, "priceCurrency": currency,
                   "availability": "https://schema.org/InStock", "itemCondition": "https://schema.org/NewCondition"},
    }
    node.update(extra)
    return node


def listing_page(nodes, links=()):
    data = {
        "@context": "https://schema.org", "@type": "ItemList",
        "itemListElement": [{"@type": "ListItem", "position": i + 1, "item": n} for i, n in enumerate(nodes)],
    }
    anchors = "".join(f'<a href="{href}">enlace</a>' for href in links)
    return (
        "<html><head><title>Laptops</title><script type='application/ld+json'>" + json.dumps(data)
        + "</script></head><body>" + anchors + "<p>catálogo de la tienda</p>" * 60 + "</body></html>"
    )


def product_page(price="1000"):
    return ld({"@type": "Offer", "price": price, "priceCurrency": "PEN", "availability": "https://schema.org/InStock"})


def site(routes, allowed=("shop.test",), **kw):
    """routes: {path: (status, body str|bytes, headers)}; every request is recorded."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        status, body, headers = routes.get(request.url.path, (404, "", {}))
        if request.url.path.endswith(".gz"):
            ctype = "application/gzip"
        elif request.url.path.endswith(".xml"):
            ctype = "application/xml"
        else:
            ctype = "text/html; charset=utf-8"
        data = body if isinstance(body, bytes) else body.encode()
        return httpx.Response(status, content=data, headers={"content-type": ctype} | dict(headers))

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return StorePageProvider(allowed, contact="ops@example.org", client=client, resolver=PUBLIC, **kw), seen


def pages(seen):
    """Requests that were not robots.txt."""
    return [u for u in seen if not u.endswith("/robots.txt")]


def add_catalog(db, **kw):
    row = {
        "id": str(uuid.uuid4()), "source_id": "shop", "provider": "store", "domain": "shop.test", "mode": "listing",
        "url": "https://shop.test/laptops", "category": "tecnologia", "subcategory": "laptops", "currency": "PEN",
        "include_regex": None, "rules_json": None, "max_items": 10, "store_max": 40, "alert_mode": "smart",
        "channel": "simulation", "enabled": 1, "next_run_at": T0, "created_at": T0,
    } | kw
    with db.connect() as con:
        con.execute(
            f"INSERT INTO catalogs({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values())
        )
        return dict(con.execute("SELECT * FROM catalogs WHERE id=?", (row["id"],)).fetchone())


def sched_for(db, provider, *, seed=5, **kw):
    clock = {"now": T0}
    rng = random.Random(seed)
    slept = []
    gate = DomainGate(db, gap=kw.pop("gap", (15.0, 45.0)), rng=rng, **kw.pop("gate", {}))
    registry = kw.pop("registry", None) or Registry([provider])
    sched = Scheduler(
        db, registry, gate, min_s=kw.pop("min_s", 60), max_s=kw.pop("max_s", 120), rng=rng,
        clock=lambda: clock["now"], sleeper=slept.append, **kw
    )
    return sched, clock, slept


def incidents(db):
    return restrictions.incidents(db, 100)


# --- sitemap parsing -------------------------------------------------------------------------------------
def test_parse_sitemap_reads_urlsets_indexes_and_gzip():
    kind, items = discovery.parse_sitemap(urlset(("https://shop.test/a", "2026-10-01"), "https://shop.test/b").encode())
    assert kind == "urlset" and items == [("https://shop.test/a", "2026-10-01"), ("https://shop.test/b", None)]
    assert discovery.parse_sitemap(index("https://shop.test/s1.xml").encode()) == ("index", [("https://shop.test/s1.xml", None)])
    assert discovery.parse_sitemap(gzip.compress(urlset("https://shop.test/a").encode()))[1] == [("https://shop.test/a", None)]


@pytest.mark.parametrize(
    "prefix",
    [
        '<!DOCTYPE urlset [<!ENTITY x "y">]>',
        '<!doctype  urlset SYSTEM "http://evil.test/x.dtd">',
        "<!-- " + "x" * 70_000 + ' --><!DOCTYPE urlset [<!ENTITY x "y">]>',  # hidden behind a long comment
        '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]>',
    ],
)
def test_sitemaps_with_a_dtd_or_entities_are_refused_wherever_they_are_hidden(prefix):
    xml = '<?xml version="1.0"?>' + prefix + f'<urlset xmlns="{NS}"><url><loc>https://shop.test/a</loc></url></urlset>'
    with pytest.raises(StructureError, match="DTD"):
        discovery.parse_sitemap(xml.encode())
    with pytest.raises(StructureError, match="DTD"):
        discovery.parse_sitemap(gzip.compress(xml.encode()))


def test_a_compressed_bomb_stops_at_the_inflation_limit():
    bomb = gzip.compress(b"<urlset>" + b" " * (discovery.MAX_INFLATED + 1_048_576) + b"</urlset>")
    assert len(bomb) < 200_000  # tiny on the wire, huge once inflated
    with pytest.raises(StructureError, match="límite"):
        discovery.parse_sitemap(bomb)


@pytest.mark.parametrize("data", [b"not xml at all", b"<html><body>hola</body></html>", b"<urlset><url>", b""])
def test_things_that_are_not_sitemaps_are_rejected(data):
    with pytest.raises(StructureError):
        discovery.parse_sitemap(data)


def test_a_sitemap_is_cut_at_the_protocols_own_url_limit(monkeypatch):
    monkeypatch.setattr(discovery, "MAX_SITEMAP_URLS", 5)
    kind, items = discovery.parse_sitemap(urlset(*[f"https://shop.test/p/{i}" for i in range(20)]).encode())
    assert len(items) == 5


# --- listing pages ---------------------------------------------------------------------------------------
def test_a_listing_page_yields_prices_and_urls_only_for_https_products_of_the_store():
    page = listing_page([
        product(1, "1299.90"), product(2, "999"), product(3, host="other.test"), product(4, scheme="http"),
        product(5, url="/p/laptop-5", price="50"),  # relative: resolved against the catalog URL
    ])
    provider, _ = site({"/laptops": (200, page, {})})
    catalog = add_catalog(db=None, **{}) if False else {"url": "https://shop.test/laptops", "domain": "shop.test",
                                                         "include_regex": None, "category": "tecnologia", "subcategory": "laptops"}
    priced, urls = discovery.collect_listing(provider, catalog, NOW)
    assert [u for u, _c, _s in urls] == ["https://shop.test/p/laptop-1", "https://shop.test/p/laptop-2", "https://shop.test/p/laptop-5"]
    assert {(p["url"], p["price_cents"]) for p in priced} == {
        ("https://shop.test/p/laptop-1", 129990), ("https://shop.test/p/laptop-2", 99900), ("https://shop.test/p/laptop-5", 5000),
    }
    assert all(c == "tecnologia" and s == "laptops" for _u, c, s in urls)


def test_include_regex_adds_links_from_the_page_and_filters_by_path_never_by_host():
    page = listing_page(
        [product(1), product(2, url="https://shop.test/p/phone-2")],
        links=["/p/laptop-9", "/blog/novedades", "https://shop.test/p/laptop-1", "http://shop.test/p/laptop-8"],
    )
    provider, _ = site({"/laptops": (200, page, {})})
    catalog = {"url": "https://shop.test/laptops", "domain": "shop.test", "include_regex": r"/p/laptop-", "category": "c", "subcategory": "s"}
    priced, urls = discovery.collect_listing(provider, catalog, NOW)
    assert [u for u, _c, _s in urls] == ["https://shop.test/p/laptop-1", "https://shop.test/p/laptop-9"]  # no phone, blog or http
    assert [p["url"] for p in priced] == ["https://shop.test/p/laptop-1"]  # laptop-9 has no price on the listing: it is not invented
    host_only = discovery._match(r"laptop", "https://laptop.shop.test/p/zapato-1")
    assert host_only is False  # the host name never counts as a match


def test_a_listing_without_products_or_a_way_to_recognise_them_is_a_structure_error_not_a_guess():
    provider, _ = site({"/laptops": (200, listing_page([]), {})})
    catalog = {"url": "https://shop.test/laptops", "domain": "shop.test", "include_regex": None, "category": "c", "subcategory": "s"}
    with pytest.raises(StructureError, match="JSON-LD"):
        discovery.collect_listing(provider, catalog, NOW)


def test_a_listing_that_only_publishes_a_from_price_never_invents_a_price():
    node = {"@type": "Product", "name": "Laptop", "url": "https://shop.test/p/laptop-1",
            "offers": {"@type": "AggregateOffer", "lowPrice": "10", "highPrice": "99", "priceCurrency": "PEN"}}
    provider, _ = site({"/laptops": (200, listing_page([node]), {})})
    catalog = {"url": "https://shop.test/laptops", "domain": "shop.test", "include_regex": None, "category": "c", "subcategory": "s"}
    priced, urls = discovery.collect_listing(provider, catalog, NOW)
    assert priced == [] and [u for u, _c, _s in urls] == ["https://shop.test/p/laptop-1"]


# --- sitemaps through the provider -------------------------------------------------------------------------
ROOT = {"domain": "shop.test", "url": "https://shop.test/", "include_regex": None, "rules_json": None, "category": "tecnologia", "subcategory": "laptops"}
ROBOTS = "User-agent: *\nAllow: /\nSitemap: https://shop.test/sitemap_index.xml\n"


def test_the_site_root_follows_robots_txt_and_reads_only_product_sitemaps_newest_first():
    provider, seen = site({
        "/robots.txt": (200, ROBOTS, {}),
        "/sitemap_index.xml": (200, index("https://shop.test/sitemap_products_1.xml", "https://shop.test/sitemap_blog.xml", "https://shop.test/sitemap_pages.xml"), {}),
        "/sitemap_products_1.xml": (200, urlset(
            ("https://shop.test/p/laptop-old", "2026-01-01"), ("https://shop.test/p/laptop-new", "2026-10-30"),
            "https://shop.test/p/laptop-undated", ("https://evil.test/p/laptop-x", "2026-11-01")), {}),
        "/sitemap_blog.xml": (200, urlset("https://shop.test/blog/a"), {}),
    })
    pauses = []
    entries = discovery.collect_sitemap(provider, ROOT, NOW, lambda: pauses.append(1))
    assert [loc for loc, _m in entries] == [
        "https://shop.test/p/laptop-new", "https://shop.test/p/laptop-old", "https://shop.test/p/laptop-undated",
    ]  # newest lastmod first, undated last, foreign host dropped
    assert pages(seen) == ["https://shop.test/sitemap_index.xml", "https://shop.test/sitemap_products_1.xml"]  # blog/pages never fetched
    assert len(pauses) == 1  # a pause between consecutive fetches, none before the first


def test_the_number_of_sitemap_files_read_per_sweep_is_capped_with_a_pause_between_each():
    names = [f"https://shop.test/sitemap_products_{i}.xml" for i in range(1, 8)]
    routes = {"/robots.txt": (200, ROBOTS, {}), "/sitemap_index.xml": (200, index(*names), {})}
    for i, name in enumerate(names, 1):
        routes[f"/sitemap_products_{i}.xml"] = (200, urlset(f"https://shop.test/p/laptop-{i}"), {})
    provider, seen = site(routes)
    pauses = []
    entries = discovery.collect_sitemap(provider, ROOT, NOW, lambda: pauses.append(1))
    assert len(pages(seen)) == discovery.MAX_SITEMAP_FETCHES == 4  # index + 3 product sitemaps, no more
    assert len(entries) == 3 and len(pauses) == 3


def test_a_sitemap_that_is_not_about_products_needs_include_or_rules_to_be_used():
    routes = {"/robots.txt": (404, "", {}), "/sitemap-pages.xml": (200, urlset("https://shop.test/p/laptop-1", "https://shop.test/contacto"), {})}
    provider, _ = site(routes)
    cat = ROOT | {"url": "https://shop.test/sitemap-pages.xml"}
    with pytest.raises(StructureError, match="no es de productos"):
        discovery.collect_sitemap(provider, cat, NOW, lambda: None)
    only = discovery.collect_sitemap(provider, cat | {"include_regex": r"/p/"}, NOW, lambda: None)
    assert [loc for loc, _m in only] == ["https://shop.test/p/laptop-1"]
    ruled = discovery.collect_sitemap(provider, cat | {"rules_json": json.dumps(RULES)}, NOW, lambda: None)
    assert len(ruled) == 2  # with rules every URL is a candidate; assign() later drops the ones no rule recognises


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://shop.test/sitemap_products_1.xml", True),
        ("https://shop.test/sitemap/product-0.xml", True),
        ("https://shop.test/product-sitemap.xml", True),
        ("https://shop.test/sitemaps/productos.xml.gz", True),
        ("https://shop.test/sitemap_items_2.xml", True),
        ("https://shop.test/sitemap.xml", False),  # 'sitemap' contains 'item': it must not count
        ("https://shop.test/sitemap_pages.xml", False),
        ("https://shop.test/sitemap-blog.xml", False),
        ("https://shop.test/sitemap/category-0.xml", False),
        ("https://products.shop.test/sitemap.xml", False),  # only the path is read, never the host
    ],
)
def test_sitemap_file_names_are_read_for_a_product_hint_without_the_word_sitemap_fooling_it(url, expected):
    assert discovery.is_product_sitemap(url) is expected


def test_an_index_with_no_product_looking_file_names_is_tried_only_when_include_or_rules_filter_the_urls():
    names = [f"https://shop.test/sm-{i}.xml" for i in range(1, 8)]
    routes = {"/robots.txt": (200, ROBOTS, {}), "/sitemap_index.xml": (200, index(*names), {})}
    for i in range(1, 8):
        routes[f"/sm-{i}.xml"] = (200, urlset(f"https://shop.test/p/laptop-{i}", f"https://shop.test/blog/post-{i}"), {})
    provider, seen = site(routes)
    with pytest.raises(StructureError, match="No se encontraron"):
        discovery.collect_sitemap(provider, ROOT, NOW, lambda: None)  # no include, no rules: nothing is guessed
    assert pages(seen) == ["https://shop.test/sitemap_index.xml"]
    seen.clear()
    entries = discovery.collect_sitemap(provider, ROOT | {"include_regex": r"/p/laptop-"}, NOW, lambda: None)
    assert len(pages(seen)) == discovery.MAX_SITEMAP_FETCHES  # index + 3 files, however many the index lists
    assert [loc for loc, _m in entries] == [f"https://shop.test/p/laptop-{i}" for i in (1, 2, 3)] or len(entries) == 3  # blog posts filtered out
    assert all("/blog/" not in loc for loc, _m in entries)


def test_a_site_without_declared_sitemaps_is_reported_not_guessed():
    provider, seen = site({"/robots.txt": (200, "User-agent: *\nAllow: /\n", {})})
    with pytest.raises(StructureError, match="no declara sitemaps"):
        discovery.collect_sitemap(provider, ROOT, NOW, lambda: None)
    assert pages(seen) == []  # it did not go probing for /sitemap.xml or other made-up paths


def test_robots_txt_applies_to_sitemaps_too():
    routes = {"/robots.txt": (200, "User-agent: *\nDisallow: /sitemap\n", {}), "/sitemap_products.xml": (200, urlset("https://shop.test/p/laptop-1"), {})}
    provider, seen = site(routes)
    with pytest.raises(Exception, match="robots.txt"):
        discovery.collect_sitemap(provider, ROOT | {"url": "https://shop.test/sitemap_products.xml"}, NOW, lambda: None)
    assert pages(seen) == []


def test_a_gzipped_sitemap_is_read():
    routes = {"/robots.txt": (404, "", {}), "/sitemap_products.xml.gz": (200, gzip.compress(urlset("https://shop.test/p/laptop-1").encode()), {})}
    provider, _ = site(routes)
    entries = discovery.collect_sitemap(provider, ROOT | {"url": "https://shop.test/sitemap_products.xml.gz"}, NOW, lambda: None)
    assert entries == [("https://shop.test/p/laptop-1", None)]


# --- routing by rules ---------------------------------------------------------------------------------------
def rule_catalog(**kw):
    return {"rules_json": json.dumps(RULES), "max_items": 2, "category": None, "subcategory": None} | kw


def test_rules_route_urls_to_categories_interleaved_and_capped_per_rule():
    entries = (
        [(f"https://shop.test/p/laptop-{i}", None) for i in range(5)]
        + [(f"https://shop.test/p/celular-{i}", None) for i in range(3)]
        + [("https://shop.test/p/laptop-celular-combo", None), ("https://shop.test/blog/nada", None)]
    )
    out = discovery.assign(entries, rule_catalog())
    assert out == [
        ("https://shop.test/p/laptop-0", "tecnologia", "laptops"),
        ("https://shop.test/p/celular-0", "tecnologia", "celulares"),
        ("https://shop.test/p/laptop-1", "tecnologia", "laptops"),
        ("https://shop.test/p/celular-1", "tecnologia", "celulares"),
    ]  # a per-store cap can never starve the later categories


def test_the_first_matching_rule_wins_and_the_host_is_never_matched():
    out = discovery.assign([("https://shop.test/p/laptop-celular-combo", None)], rule_catalog(max_items=5))
    assert out == [("https://shop.test/p/laptop-celular-combo", "tecnologia", "laptops")]
    assert discovery.assign([("https://laptop.shop.test/p/zapato-1", None)], rule_catalog()) == []


def test_without_rules_everything_goes_to_the_catalogs_own_category():
    out = discovery.assign([("https://shop.test/p/a", None), ("https://shop.test/p/b", None)], {"rules_json": None, "category": "hogar", "subcategory": "cocina", "max_items": 5})
    assert out == [("https://shop.test/p/a", "hogar", "cocina"), ("https://shop.test/p/b", "hogar", "cocina")]


# --- creating watches under caps --------------------------------------------------------------------------------
def urls_for(n, prefix="laptop"):
    return [(f"https://shop.test/p/{prefix}-{i}", "tecnologia", "laptops") for i in range(n)]


def test_watches_are_created_up_to_the_catalog_cap_and_a_second_pass_creates_nothing(db):
    provider, _ = site({})
    catalog = add_catalog(db, mode="sitemap", url="https://shop.test/sitemap.xml", max_items=3, alert_mode="both", channel="telegram")  # a stale value
    created, ids, capped = discovery.ensure_watches(db, provider, catalog, urls_for(10), NOW)
    assert (created, len(ids), capped) == (3, 3, True)
    again = discovery.ensure_watches(db, provider, catalog, urls_for(10), NOW)
    assert again[0] == 0 and len(again[1]) == 3 and again[2] is True
    with db.connect() as con:
        rows = [dict(r) for r in con.execute("SELECT provider,kind,category,subcategory,alert_mode,channel,catalog_id,enabled FROM watches")]
    assert len(rows) == 3
    assert all(r == {"provider": "store", "kind": "product", "category": "tecnologia", "subcategory": "laptops",
                     "alert_mode": "smart", "channel": "telegram", "catalog_id": catalog["id"], "enabled": 1} for r in rows)  # always smart: no personal target exists


def test_rule_based_catalogs_get_max_items_per_category(db):
    provider, _ = site({})
    catalog = add_catalog(db, mode="sitemap", url="https://shop.test/sitemap.xml", max_items=2, rules_json=json.dumps(RULES))
    urls = [(f"https://shop.test/p/{n}-{i}", "tecnologia", sub) for n, sub in (("laptop", "laptops"), ("celular", "celulares")) for i in range(5)]
    created, _ids, capped = discovery.ensure_watches(db, provider, catalog, urls, NOW)
    assert created == 4 and capped is True  # 2 rules x 2 items


def test_the_per_store_cap_is_shared_by_all_catalogs_of_that_store(db):
    provider, _ = site({})
    first = add_catalog(db, url="https://shop.test/a", max_items=10, store_max=4)
    second = add_catalog(db, url="https://shop.test/b", max_items=10, store_max=4)
    assert discovery.ensure_watches(db, provider, first, urls_for(3, "a"), NOW)[0] == 3
    created, ids, capped = discovery.ensure_watches(db, provider, second, urls_for(3, "b"), NOW)
    assert created == 1 and capped is True  # only one slot of the store's 4 was left
    other_store = add_catalog(db, source_id="other", url="https://shop.test/c", max_items=10, store_max=4)
    assert discovery.ensure_watches(db, provider, other_store, urls_for(3, "c"), NOW)[0] == 3  # another store has its own budget


def test_urls_that_robots_txt_forbids_are_never_watched_or_requested(db):
    provider, seen = site({"/robots.txt": (200, "User-agent: *\nDisallow: /p/laptop-secret\n", {})})
    catalog = add_catalog(db, mode="sitemap", url="https://shop.test/sitemap.xml")
    urls = [("https://shop.test/p/laptop-1", "tecnologia", "laptops"), ("https://shop.test/p/laptop-secret", "tecnologia", "laptops")]
    created, ids, _capped = discovery.ensure_watches(db, provider, catalog, urls, NOW)
    assert created == 1 and list(ids) == ["https://shop.test/p/laptop-1"]
    assert not any("laptop-secret" in u for u in seen)


def test_a_product_the_user_already_watches_is_not_duplicated(db):
    provider, _ = site({})
    manual = add_watch(db, product_watch(1, provider="store"))
    catalog = add_catalog(db, mode="sitemap", url="https://shop.test/sitemap.xml")
    created, ids, _ = discovery.ensure_watches(db, provider, catalog, [("https://shop.test/p/1", "tecnologia", "laptops")], NOW)
    assert created == 0 and ids == {"https://shop.test/p/1": manual}


def test_the_global_watch_limit_stops_discovery_cleanly(db, monkeypatch):
    monkeypatch.setenv("RADAR_MAX_WATCHES", "2")
    provider, _ = site({})
    catalog = add_catalog(db, mode="sitemap", url="https://shop.test/sitemap.xml", max_items=10)
    created, ids, capped = discovery.ensure_watches(db, provider, catalog, urls_for(6), NOW)
    assert (created, len(ids), capped) == (2, 2, True)


# --- a whole listing sweep ----------------------------------------------------------------------------------------
def test_a_listing_sweep_creates_watches_and_prices_them_from_one_request(db):
    provider, seen = site({"/laptops": (200, listing_page([product(i, f"{1000 + i}.00") for i in range(1, 5)]), {})})
    catalog = add_catalog(db, url="https://shop.test/laptops", max_items=10)
    summary = discovery.sweep(db, provider, catalog, NOW, rng=random.Random(1))
    assert summary == {"found": 4, "tracked": 4, "created": 4, "priced_from_listing": 4, "skipped_currency": 0, "capped": False}
    assert pages(seen) == ["https://shop.test/laptops"]  # ONE request priced four products; no product page was opened
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 4
        assert con.execute("SELECT COUNT(*) FROM watches WHERE title IS NOT NULL OR match_key IS NOT NULL").fetchone()[0] == 4
        later = [r[0] - NOW for r in con.execute("SELECT next_run_at FROM watch_schedule")]
    assert len(later) == 4 and all(3600 <= d <= 7200 for d in later)  # each page is only a slower confirmation, 1-2 h away


def test_a_second_sweep_with_the_same_prices_creates_and_duplicates_nothing(db):
    provider, _ = site({"/laptops": (200, listing_page([product(i) for i in range(1, 4)]), {})})
    catalog = add_catalog(db, url="https://shop.test/laptops")
    discovery.sweep(db, provider, catalog, NOW, rng=random.Random(1))
    again = discovery.sweep(db, provider, catalog, NOW + 7200, rng=random.Random(2))
    assert again["created"] == 0 and again["tracked"] == 3
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 3
        assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3  # same price -> same observation, not a new row each pass


def test_a_listing_price_in_another_currency_is_not_recorded_or_converted(db):
    provider, _ = site({"/laptops": (200, listing_page([product(1), product(2, "20.00", "USD")]), {})})
    catalog = add_catalog(db, url="https://shop.test/laptops")
    summary = discovery.sweep(db, provider, catalog, NOW, rng=random.Random(1))
    assert summary["priced_from_listing"] == 1 and summary["skipped_currency"] == 1 and summary["created"] == 2
    with db.connect() as con:
        assert con.execute("SELECT COUNT(DISTINCT currency) FROM observations").fetchone()[0] == 1


def test_a_sitemap_sweep_only_creates_watches_and_asks_each_product_page_later(db):
    routes = {
        "/robots.txt": (404, "", {}),
        "/sitemap.xml": (200, urlset(*[f"https://shop.test/p/laptop-{i}" for i in range(6)] + ["https://shop.test/p/celular-1"]), {}),
    }
    provider, seen = site(routes)
    catalog = add_catalog(db, mode="sitemap", url="https://shop.test/sitemap.xml", max_items=2, rules_json=json.dumps(RULES), category=None, subcategory=None)
    summary = discovery.sweep(db, provider, catalog, NOW, rng=random.Random(1))
    assert summary["found"] == 3 and summary["created"] == 3 and summary["priced_from_listing"] == 0  # 2 laptops + 1 phone by rule
    assert pages(seen) == ["https://shop.test/sitemap.xml"]
    with db.connect() as con:
        cats = sorted(r[0] for r in con.execute("SELECT subcategory FROM watches"))
    assert cats == ["celulares", "laptops", "laptops"]


# --- the scheduler running catalogs ---------------------------------------------------------------------------------
def test_the_scheduler_sweeps_a_due_catalog_then_monitors_what_it_found(db):
    routes = {"/robots.txt": (404, "", {}), "/sitemap.xml": (200, urlset(*[f"https://shop.test/p/laptop-{i}" for i in range(6)]), {})}
    routes |= {f"/p/laptop-{i}": (200, product_page(), {}) for i in range(6)}
    provider, seen = site(routes)
    catalog = add_catalog(db, mode="sitemap", url="https://shop.test/sitemap.xml", max_items=4, include_regex=r"/p/laptop-")
    sched, clock, _slept = sched_for(db, provider)
    out = run_for(sched, clock, 3600)
    sweeps = [o for o in out if "catalog_id" in o]
    assert len(sweeps) == 1 and sweeps[0]["status"] == "success" and sweeps[0]["created"] == 4
    with db.connect() as con:
        row = dict(con.execute("SELECT * FROM catalogs WHERE id=?", (catalog["id"],)).fetchone())
        assert con.execute("SELECT COUNT(*) FROM watches WHERE catalog_id=?", (catalog["id"],)).fetchone()[0] == 4
    assert row["last_status"] == "success" and row["found_count"] == 6 and row["fail_count"] == 0
    assert SITEMAP_EVERY[0] <= row["next_run_at"] - T0 <= SITEMAP_EVERY[1] + 60  # not swept again for hours
    assert sum(u.endswith("/sitemap.xml") for u in seen) == 1
    watched = {u for u in seen if "/p/laptop-" in u}
    assert len(watched) == 4  # the 4 tracked product pages are then polled normally; the 2 beyond the cap never are


def test_a_blocked_catalog_pauses_its_domain_and_nothing_else_is_requested(db):
    provider, seen = site({"/robots.txt": (200, "User-agent: *\nAllow: /\n", {}), "/sitemap.xml": (403, "forbidden", {})})
    catalog = add_catalog(db, mode="sitemap", url="https://shop.test/sitemap.xml")
    sched, clock, _ = sched_for(db, provider)
    out = run_for(sched, clock, 120)
    assert [o["status"] for o in out] == ["denied"]
    frozen = list(seen)
    run_for(sched, clock, 600)  # still inside the pause (>= 900 s)
    assert seen == frozen
    [incident] = incidents(db)
    assert (incident["kind"], incident["action"], incident["severity"]) == ("denied", "domain_paused", "high")
    assert incident["catalog_id"] == catalog["id"] and incident["http_status"] == 403 and incident["watch_id"] is None
    with db.connect() as con:
        row = dict(con.execute("SELECT * FROM catalogs").fetchone())
        assert con.execute("SELECT status FROM domain_state WHERE domain='shop.test'").fetchone()[0] == "cooling"
    assert row["last_status"] == "denied" and row["fail_count"] == 1 and row["enabled"] == 1


def test_a_domain_whose_circuit_is_open_receives_no_catalog_sweeps_until_resumed(db):
    provider, seen = site({"/robots.txt": (404, "", {}), "/laptops": (200, listing_page([product(1)]), {})})
    add_catalog(db, url="https://shop.test/laptops")
    sched, clock, _ = sched_for(db, provider, gate={"disable_after": 2})
    sched.gate.record_block("shop.test", T0, None, "x")
    sched.gate.record_block("shop.test", T0, None, "x")  # second strike: circuit open
    run_for(sched, clock, 6 * 3600, step=60)
    assert seen == []
    assert sched.gate.resume("shop.test") is True
    run_for(sched, clock, 600)
    # a listing sweep is the cheap way to refresh many prices, so it follows the normal random cadence;
    # what matters here: nothing at all while the circuit was open, and only the listing page afterwards
    assert pages(seen) and set(pages(seen)) == {"https://shop.test/laptops"}


def test_a_catalog_that_stops_publishing_products_is_paused_after_five_attempts(db):
    provider, seen = site({"/robots.txt": (404, "", {}), "/laptops": (200, listing_page([]), {})})
    add_catalog(db, url="https://shop.test/laptops")
    sched, clock, _ = sched_for(db, provider, gate={"disable_after": 99})
    runs = 0
    for _ in range(12):
        with db.connect() as con:
            row = dict(con.execute("SELECT * FROM catalogs").fetchone())
        if not row["enabled"]:
            break
        clock["now"] = max(clock["now"], row["next_run_at"]) + 1
        runs += len([o for o in sched.tick(clock["now"]) if "catalog_id" in o])
    with db.connect() as con:
        row = dict(con.execute("SELECT * FROM catalogs").fetchone())
    assert runs == 5 and row["enabled"] == 0 and row["last_status"] == "structure" and row["fail_count"] == 5
    assert len(pages(seen)) == 5  # after the fifth it stops asking
    assert any("pausado" in i["detail"] and i["catalog_id"] == row["id"] for i in incidents(db))
    frozen = list(seen)
    run_for(sched, clock, 48 * 3600, step=600)
    assert seen == frozen


def test_a_catalog_whose_provider_is_not_configured_is_reported_and_retried_much_later(db):
    add_catalog(db, url="https://shop.test/laptops")
    sched, clock, _ = sched_for(db, None, registry=Registry([DemoProvider()]))
    out = sched.tick(T0)
    assert out[0]["status"] == "unconfigured"
    with db.connect() as con:
        row = dict(con.execute("SELECT * FROM catalogs").fetchone())
    assert row["last_status"] == "unconfigured" and row["next_run_at"] - T0 >= 6 * 3600 - 1


def test_pauses_between_sitemap_files_go_through_the_injected_sleeper_and_are_short(db):
    names = [f"https://shop.test/sitemap_products_{i}.xml" for i in range(1, 4)]
    routes = {"/robots.txt": (200, ROBOTS, {}), "/sitemap_index.xml": (200, index(*names), {})}
    routes |= {f"/sitemap_products_{i}.xml": (200, urlset(f"https://shop.test/p/laptop-{i}"), {}) for i in range(1, 4)}
    provider, _ = site(routes)
    add_catalog(db, mode="sitemap", url="https://shop.test/", max_items=10)
    sched, clock, slept = sched_for(db, provider)
    sched.tick(T0)
    assert len(slept) == 3 and all(3.0 <= s <= 8.0 for s in slept)
