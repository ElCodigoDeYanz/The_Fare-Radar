"""Check, from YOUR server and politely, whether each source can really be monitored.

For every selected store it asks: does robots.txt allow it? does it publish sitemaps? do its
product pages carry an exact price in JSON-LD (and a GTIN/MPN to compare across stores)?
It makes at most a handful of requests per store, one store at a time, with random pauses, and
stops on the first block (a blocked store is reported, never retried around).
Nothing here is guesswork: the verdict only says what the site actually returned.
"""

import json
import time
from . import discovery
from .identity import match_key
from .providers.base import BlockedError, ProviderError
from .providers.storepage import extract_offers


def verify_store(provider, source, now, pause):
    report = {"id": source["id"], "domain": source["domain"], "checked_at": now}
    catalog = {
        "domain": source["domain"], "url": f"https://{source['domain']}/", "include_regex": None,
        "rules_json": json.dumps([{"category": c["category"], "subcategory": c["subcategory"], "include": c["include"]} for c in source["catalogs"]]),
        "category": None, "subcategory": None, "max_items": 3,
    }
    try:
        report["sitemaps"] = provider.robots_sitemaps(source["domain"], now)
        pause()
        entries = discovery.collect_sitemap(provider, catalog, now, pause)
        routed = discovery.assign(entries, catalog)
        report["sitemap_urls"] = len(entries)
        report["routed_urls"] = len(routed)
        if not routed:
            report["verdict"] = "sin_productos_reconocibles"
            return report
        sample = routed[0][0]
        report["sample_url"] = sample
        pause()
        html = provider.fetch_document(sample, now)
        offers, ambiguous = extract_offers(html)
        priced = [o for o in offers if o["price_cents"] is not None and o["currency"]]
        report.update(
            jsonld_products=len(offers),
            exact_price=bool(priced),
            only_price_range=bool(ambiguous and not priced),
            currency=priced[0]["currency"] if priced else None,
            currency_matches_catalog=bool(priced and priced[0]["currency"] == source["currency"]),
            availability_published=bool(priced and priced[0]["availability"]),
            comparable_across_stores=bool(priced and match_key(priced[0]["ids"])),
        )
        if priced and report["currency_matches_catalog"]:
            report["verdict"] = "usable"
        elif priced:
            report["verdict"] = "moneda_distinta"  # exact price, but not in the currency the catalogue expects: never converted
        elif offers or ambiguous:
            report["verdict"] = "precio_no_exacto"  # a price range or several different offers: never guessed
        else:
            report["verdict"] = "sin_json_ld"
    except BlockedError as exc:
        report.update(verdict="bloqueado", error={"kind": exc.error_kind, "message": str(exc)})
    except ProviderError as exc:
        report.update(verdict="no_verificable", error={"kind": exc.error_kind, "message": str(exc)})
    return report


def verify_all(provider, sources, now, pause, between=lambda: None, stop_on_block=False):
    reports = []
    for source in sources:
        report = verify_store(provider, source, now, pause)
        reports.append(report)
        if report["verdict"] == "bloqueado" and stop_on_block:
            break
        between()
    summary = {}
    for r in reports:
        summary[r["verdict"]] = summary.get(r["verdict"], 0) + 1
    return {"generated_at": now, "summary": summary, "sources": reports}
