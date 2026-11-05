"""Catalog discovery: give the monitor a store CATEGORY page or SITEMAP instead of one link per
product. One sweep creates the product watches (up to a per-catalog cap) and, when the listing
page publishes prices in JSON-LD, prices them all from that ONE request.

  listing  a category page. Products come from its schema.org JSON-LD (ItemList/Product with
           url and exact price) and, if `include_regex` is set, from links that match it.
  sitemap  a sitemap (or the site root: robots.txt 'Sitemap:' lines are followed). Product
           sitemaps are picked by name or by `include_regex`; newest lastmod first. It creates
           watches only; each product page is then monitored on the normal schedule.

Everything goes through the store provider, so robots.txt, the allow-list, the SSRF check,
the size caps, the per-domain gap and the circuit breaker all apply. Nothing is guessed:
a page that publishes no products raises a StructureError instead of inventing any.
"""

import json
import re
import uuid
import zlib
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from .models import ProductCriteria, WatchInput
from .providers.base import ProviderError, StructureError
from .providers.storepage import extract_links, extract_listing
from . import service

PRODUCT_SITEMAP = re.compile(r"(?i)product|producto|item|sku|catalog|articul|goods")
MAX_SITEMAP_FETCHES = 4
MAX_INFLATED = 25 * 1024 * 1024
MAX_SITEMAP_URLS = 50_000  # the sitemap protocol's own limit per file
DTD = re.compile(rb"<!\s*(?:doctype|entity)", re.I)


def inflate(data):
    """gzip → bytes with a hard cap (a compressed 'bomb' stops at MAX_INFLATED)."""
    if data[:2] != b"\x1f\x8b":
        return data
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = decompressor.decompress(data, MAX_INFLATED)
    if decompressor.unconsumed_tail:
        raise StructureError("El sitemap descomprimido supera el límite permitido.")
    return out


def parse_sitemap(data):
    """('index'|'urlset', [(loc, lastmod|None), ...]). No DTDs/entities are ever accepted."""
    raw = inflate(data)
    if DTD.search(raw):  # whole document: a DOCTYPE hidden behind a long comment must not slip through
        raise StructureError("El sitemap declara DTD/entidades; se rechaza por seguridad.")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise StructureError("El sitemap no es XML válido.") from exc
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in ("sitemapindex", "urlset"):
        raise StructureError("El documento no es un sitemap.")
    entries = []
    for node in root:
        fields = {c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in node}
        if fields.get("loc"):
            entries.append((fields["loc"], fields.get("lastmod") or None))
        if len(entries) >= MAX_SITEMAP_URLS:
            break
    return ("index" if kind == "sitemapindex" else "urlset"), entries


def is_product_sitemap(url):
    """Name-based hint that a sitemap FILE lists products. Only the path is read (never the host)
    and the word 'sitemap' itself is ignored: it contains 'item', which would make every
    sitemap look like a product sitemap (blog, pages, categories...)."""
    return PRODUCT_SITEMAP.search(urlsplit(url).path.lower().replace("sitemap", " ")) is not None


def _in_domain(url, domain):
    host = urlsplit(url).hostname or ""
    return urlsplit(url).scheme == "https" and (host == domain or host.endswith("." + domain))


def _match(include, url):
    """include_regex is tested against the URL path (and query), never the host name."""
    parts = urlsplit(url)
    return include is None or re.search(include, parts.path + ("?" + parts.query if parts.query else ""), re.I) is not None


def collect_listing(provider, catalog, now):
    html = provider.fetch_document(catalog["url"], now, expect="html")
    priced, urls = extract_listing(html, catalog["url"])
    include = catalog["include_regex"] or None
    if include:
        urls = urls + [u for u in extract_links(html, catalog["url"]) if _match(include, u) and u not in urls]
    if not urls:
        raise StructureError(
            "La página de catálogo no publica productos en JSON-LD y no hay include_regex "
            "con el que reconocer enlaces de producto."
        )
    urls = [u for u in urls if _in_domain(u, catalog["domain"]) and _match(include, u)]
    keep = set(urls)
    return [p for p in priced if p["url"] in keep], [(u, catalog["category"], catalog["subcategory"]) for u in urls]


def _rules(catalog):
    raw = catalog.get("rules_json")
    return json.loads(raw) if raw else None


def collect_sitemap(provider, catalog, now, pause):
    include = catalog["include_regex"] or None
    rules = _rules(catalog)
    path = urlsplit(catalog["url"]).path
    queue = (
        provider.robots_sitemaps(catalog["domain"], now)
        if path in ("", "/") or path.endswith("robots.txt")
        else [catalog["url"]]
    )
    if not queue:
        raise StructureError("El sitio no declara sitemaps en robots.txt; indica la URL del sitemap.")
    entries, fetched = [], 0
    while queue and fetched < MAX_SITEMAP_FETCHES:
        url = queue.pop(0)
        if not _in_domain(url, catalog["domain"]):
            continue
        if fetched:
            pause()
        fetched += 1
        kind, items = parse_sitemap(provider.fetch_document(url, now, expect="xml", raw=True))
        if kind == "index":
            wanted = [loc for loc, _ in items if (include and _match(include, loc)) or is_product_sitemap(loc)]
            if not wanted and (include or rules):
                # No file name hints at products. include/rules still filter every URL, so trying the
                # files is safe; MAX_SITEMAP_FETCHES keeps it to a handful of requests per sweep.
                wanted = [loc for loc, _ in items]
            queue = wanted + queue
        else:
            product_file = is_product_sitemap(url)
            if not include and not rules and not product_file:
                raise StructureError(
                    "El sitemap no es de productos y no hay include_regex ni reglas para reconocerlos."
                )
            entries += [(loc, mod) for loc, mod in items if _in_domain(loc, catalog["domain"]) and _match(include, loc)]
    if not entries:
        raise StructureError("No se encontraron URLs de producto en los sitemaps.")
    entries.sort(key=lambda e: e[1] or "", reverse=True)  # newest lastmod first; undated last
    return entries


def assign(entries, catalog):
    """[(url, category, subcategory)]. A catalog with rules (one per product category) routes each
    URL to the FIRST rule whose regex matches its path, at most max_items per rule, then interleaves
    the rules so a per-store cap never starves the later categories."""
    rules = _rules(catalog)
    if not rules:
        return [(loc, catalog["category"], catalog["subcategory"]) for loc, _ in entries]
    compiled = [re.compile(r["include"], re.I) for r in rules]
    buckets = [[] for _ in rules]
    for loc, _mod in entries:
        parts = urlsplit(loc)
        text = parts.path + ("?" + parts.query if parts.query else "")
        for i, rx in enumerate(compiled):
            if len(buckets[i]) < catalog["max_items"] and rx.search(text):
                buckets[i].append(loc)
                break
    out = []
    for rank in range(catalog["max_items"]):
        for i, bucket in enumerate(buckets):
            if rank < len(bucket):
                out.append((bucket[rank], rules[i]["category"], rules[i]["subcategory"]))
    return out


def _criteria(url, currency):
    return ProductCriteria(url=url, currency=currency)


def ensure_watches(db, provider, catalog, urls, now, thresholds=None):
    """Create product watches for new (url, category, subcategory) until the caps. Returns (created, ids_by_url, capped)."""
    created, capped, ids = 0, False, {}
    rules = _rules(catalog)
    cap = catalog["max_items"] * (len(rules) if rules else 1)  # max_items is per category for rule-based catalogs
    with db.connect() as con:
        have = {
            json.loads(r["criteria_json"]).get("url"): r["id"]
            for r in con.execute("SELECT id,criteria_json FROM watches WHERE provider=?", (provider.name,))
        }
        active = con.execute(
            "SELECT COUNT(*) FROM watches WHERE catalog_id=? AND enabled=1", (catalog["id"],)
        ).fetchone()[0]
        store_active = con.execute(
            "SELECT COUNT(*) FROM watches w JOIN catalogs c ON c.id=w.catalog_id WHERE c.source_id=? AND w.enabled=1",
            (catalog["source_id"],),
        ).fetchone()[0]
    for url, category, subcategory in urls:
        try:
            criteria = _criteria(url, catalog["currency"])
        except ValueError:
            continue
        if criteria.url in have:
            ids[url] = have[criteria.url]
            continue
        if active + created >= cap or store_active + created >= catalog["store_max"]:
            capped = True  # per-catalog cap, or the per-store cap shared by all of its catalogs
            continue
        if not provider.allowed_by_robots(criteria.url, now):
            continue
        value = WatchInput(
            criteria=criteria,
            provider=provider.name,
            category=category,
            subcategory=subcategory,
            alert_mode="smart",  # a discovered product has no personal target price to compare against
            channel=catalog["channel"],
        )
        try:
            ids[url] = service.add_watch(db, value, now, catalog_id=catalog["id"])
            created += 1
        except ValueError as exc:
            if "Máximo" in str(exc):
                capped = True
                break
    return created, ids, capped


def ingest_listing_prices(db, provider, catalog, priced, ids, now, rng, confirm=(3600.0, 7200.0), thresholds=None):
    """Record the prices a listing page published. Returns (observed, skipped_currency)."""
    observed = skipped = 0
    for item in priced:
        key = ids.get(item["url"])
        if key is None:
            continue
        if item["currency"] != catalog["currency"]:
            skipped += 1
            continue
        watch = service.get_watch(db, key)
        criteria = _criteria(item["url"], catalog["currency"])
        offer = provider.make_offer(criteria, item, now)
        query_id = str(uuid.uuid4())
        with db.connect() as con:
            con.execute(
                "INSERT INTO queries(id,watch_id,started_at,finished_at,status,error) VALUES (?,?,?,NULL,'running',NULL)",
                (query_id, key, now),
            )
        try:
            service.ingest(db, key, watch, criteria, [offer], query_id, now, thresholds)
            observed += 1
        except (ProviderError, service.InvalidSource) as exc:
            service._fail(db, query_id, now, str(exc), getattr(exc, "error_kind", "invalid"))
            continue
        # The listing already priced this product: its own page is only re-checked as a slower confirmation.
        with db.connect() as con:
            con.execute(
                "INSERT INTO watch_schedule(watch_id,next_run_at) VALUES (?,?) "
                "ON CONFLICT(watch_id) DO UPDATE SET next_run_at=MAX(next_run_at,excluded.next_run_at)",
                (key, now + rng.uniform(*confirm)),
            )
    return observed, skipped


def sweep(db, provider, catalog, now, *, rng, pause=lambda: None, thresholds=None, confirm=(3600.0, 7200.0)):
    """One discovery pass over a catalog row. Raises typed ProviderErrors (the scheduler reacts)."""
    if catalog["mode"] == "listing":
        priced, urls = collect_listing(provider, catalog, now)
    else:
        priced, urls = [], assign(collect_sitemap(provider, catalog, now, pause), catalog)
    created, ids, capped = ensure_watches(db, provider, catalog, urls, now, thresholds)
    observed, skipped = ingest_listing_prices(db, provider, catalog, priced, ids, now, rng, confirm, thresholds)
    return {
        "found": len(urls),
        "tracked": len(ids),
        "created": created,
        "priced_from_listing": observed,
        "skipped_currency": skipped,
        "capped": capped,
    }
