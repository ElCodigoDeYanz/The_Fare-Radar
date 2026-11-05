"""The shipped catalogue of sources (sources/*.json) and how it becomes monitoring jobs.

Every entry is a SUGGESTION: nothing is monitored until you enable it, and none of them has been
verified against the real site by the author (status "unverified"). `python -m radar.main
--verify-sources` checks robots.txt, sitemaps and JSON-LD prices from YOUR server, politely.
Read each site's terms before enabling it.

Selecting what to enable (RADAR_ENABLED_SOURCES, comma separated):
    all                 every store with a connector
    country=PE          stores of one country        kind=store   cat=tecnologia
    falabella-pe,wong-pe   explicit ids
"""

import json
from pathlib import Path
import random
import re
import time
import uuid
from .catalog import resolve_category
from .models import normalize_url

ROOT = Path(__file__).resolve().parents[1]
SOURCES_DIR = ROOT / "sources"
ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")
HOST = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
KINDS = {"store", "bus", "airline", "agency"}


class SourceError(ValueError):
    pass


def load_sources(directory=None):
    """{id: source}. Validates every entry; a malformed file fails loudly, never silently."""
    directory = Path(directory or SOURCES_DIR)
    templates = json.loads((directory / "categories.json").read_text())
    sources = {}
    for name in ("stores.json", "travel.json"):
        path = directory / name
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        currencies = data.get("currency_by_country", {})
        for raw in data["sources"]:
            source = dict(raw)
            if not ID.match(source.get("id", "")) or source["id"] in sources:
                raise SourceError(f"{name}: id inválido o repetido: {source.get('id')!r}")
            if source.get("kind") not in KINDS:
                raise SourceError(f"{source['id']}: kind inválido.")
            try:
                host = normalize_url("https://" + source["domain"] + "/").split("/")[2]
            except ValueError as exc:
                raise SourceError(f"{source['id']}: dominio inválido.") from exc
            if not HOST.match(host):
                raise SourceError(f"{source['id']}: dominio inválido: {source['domain']!r}.")
            source["domain"] = host
            if source["kind"] == "store":
                source["currency"] = currencies.get(source["country"])
                if not source["currency"]:
                    raise SourceError(f"{source['id']}: país sin moneda definida.")
                source["catalogs"] = expand_categories(source["id"], source["cats"], templates)
            sources[source["id"]] = source
    return sources


def expand_categories(source_id, names, templates):
    out, seen = [], set()
    for name in names:
        for item in templates["groups"].get(name, [name]):
            tpl = templates["templates"].get(item)
            if tpl is None:
                raise SourceError(f"{source_id}: categoría desconocida {item!r}.")
            resolve_category("product", tpl["category"], tpl["subcategory"])  # must exist in the catalogue
            if item not in seen:
                seen.add(item)
                out.append(tpl)
    return out


def select(sources, spec):
    """Sources (stores with a connector) matching an RADAR_ENABLED_SOURCES expression."""
    tokens = [t.strip() for t in (spec or "").split(",") if t.strip()]
    chosen = {}
    for token in tokens:
        if token == "all":
            matches = [s for s in sources.values() if s["kind"] == "store"]
        elif "=" in token:
            field, _, value = token.partition("=")
            if field == "cat":
                matches = [s for s in sources.values() if s["kind"] == "store" and any(c["category"] == value for c in s["catalogs"])]
            elif field in ("country", "kind"):
                matches = [s for s in sources.values() if str(s.get(field, "")).lower() == value.lower()]
            else:
                raise SourceError(f"Selector desconocido: {field}")
        elif token in sources:
            matches = [sources[token]]
        else:
            raise SourceError(f"Fuente desconocida: {token}")
        for s in matches:
            if s["kind"] == "store":
                chosen[s["id"]] = s
    return list(chosen.values())


def allowed_domains(selected):
    return sorted({s["domain"] for s in selected})


def import_catalogs(db, selected, now, rng, *, items_per_category=10, store_max=40, channel="simulation",
                    alert_mode="smart", only_cats=None):
    """Create (idempotently) ONE sitemap catalog per selected store; its rules route product URLs
    to categories. The first sweep of each is spread over the next 5-60 minutes."""
    if alert_mode != "smart":
        raise SourceError("Los catálogos solo admiten alert_mode=smart: un producto descubierto no tiene precio objetivo propio.")
    created = existing = 0
    with db.connect() as con:
        for source in selected:
            rules = [
                {"category": t_["category"], "subcategory": t_["subcategory"], "include": t_["include"]}
                for t_ in source["catalogs"]
                if not only_cats or t_["category"] in only_cats
            ]
            if not rules:
                continue
            url = f"https://{source['domain']}/"
            if con.execute("SELECT 1 FROM catalogs WHERE source_id=? AND mode='sitemap' AND url=?", (source["id"], url)).fetchone():
                existing += 1
                continue
            con.execute(
                "INSERT INTO catalogs(id,source_id,provider,domain,mode,url,category,subcategory,currency,include_regex,rules_json,"
                "max_items,store_max,alert_mode,channel,enabled,next_run_at,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                (str(uuid.uuid4()), source["id"], "store", source["domain"], "sitemap", url, None, None, source["currency"],
                 None, json.dumps(rules), items_per_category, source.get("max_items", store_max), alert_mode, channel,
                 now + rng.uniform(300, 3600), now),
            )
            created += 1
    return {"created": created, "existing": existing, "stores": len(selected)}


def import_enabled(db, env, now=None, rng=None):
    """Create (idempotently) the catalogs of the stores the operator listed in RADAR_ENABLED_SOURCES.

    Runs when the monitor starts, so deploying with that variable set is enough: no manual step and no
    `docker exec` as root. Returns None (and does nothing) when the variable is empty. An invalid
    selector raises SourceError so a typo stops the start-up instead of silently monitoring nothing."""
    spec = (env.get("RADAR_ENABLED_SOURCES") or "").strip()
    if not spec:
        return None

    def number(name, default):
        raw = (env.get(name) or "").strip()
        return int(raw) if raw.isdigit() and int(raw) > 0 else default

    return import_catalogs(
        db, select(load_sources(), spec), time.time() if now is None else now, rng or random.Random(),
        items_per_category=number("RADAR_ITEMS_PER_CATEGORY", 10),
        store_max=number("RADAR_STORE_MAX_ITEMS", 40),
        channel="telegram" if env.get("RADAR_TELEGRAM_ENABLED") == "1" else "simulation",
    )
