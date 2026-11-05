import asyncio
from contextlib import asynccontextmanager
from datetime import date, timedelta
import base64
import hmac
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from urllib.parse import urlsplit
from typing import Literal
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from . import analytics, budget, catalog, compare, metrics, restrictions, sources as source_catalog, trace
from .database import Database
from .models import NO_TARGET, BusCriteria, Criteria, ProductCriteria, WatchInput, WatchConfig, normalize_url
from .main import make_after_tick
from .providers.demo import SCENARIOS
from .providers.registry import build_registry, enabled_sources
from .scheduler import from_env
from .service import add_watch, get_watch, watch_record, refresh, Busy, NotFound

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("radar.api")
MIN_PASSWORD = 12


def create_app(path=None, *, env=None, registry=None, scheduler=None, start_scheduler=None):
    env = os.environ if env is None else env
    production = env.get("RADAR_ENV") == "production"
    password = env.get("RADAR_ADMIN_PASSWORD", "")
    user = env.get("RADAR_ADMIN_USER", "admin")
    if production and len(password) < MIN_PASSWORD:
        raise RuntimeError(
            f"RADAR_ENV=production exige RADAR_ADMIN_PASSWORD de al menos {MIN_PASSWORD} caracteres."
        )
    db = Database(path or env.get("RADAR_DB", ROOT / "runtime/radar.sqlite3"))
    registry = registry or build_registry(env)

    scheduler = scheduler or from_env(db, registry, env, after_tick=make_after_tick(db, env))
    run_scheduler = (
        start_scheduler if start_scheduler is not None else env.get("RADAR_SCHEDULER", "0") == "1"
    )

    @asynccontextmanager
    async def lifespan(_app):
        if run_scheduler:
            summary = source_catalog.import_enabled(db, env)
            if summary:
                log.info("catálogos de RADAR_ENABLED_SOURCES: %s", summary)
            scheduler.start()
        yield
        if run_scheduler:
            scheduler.stop()

    app = FastAPI(
        title="Radar de Passages",
        version="2.0.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.db, app.state.registry, app.state.scheduler = db, registry, scheduler
    gate = scheduler.gate

    def authorized(header):
        if not header or not header.lower().startswith("basic "):
            return False
        try:
            given_user, _, given_pass = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        # Both comparisons always run, so timing does not reveal which part was wrong.
        ok_user = hmac.compare_digest(given_user.encode(), user.encode())
        ok_pass = hmac.compare_digest(given_pass.encode(), password.encode())
        return ok_user and ok_pass

    @app.middleware("http")
    async def headers(request: Request, call_next):
        if password and request.url.path != "/api/health" and not authorized(
            request.headers.get("authorization")
        ):
            await asyncio.sleep(0.4)  # slows password guessing
            return Response(
                "Autenticación requerida.",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Radar de Passages", charset="UTF-8"'},
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.get("/api/health")
    def health():
        live = registry.any_live
        return {
            "status": "ok",
            "provider": "live + synthetic" if live else "synthetic fixture",
            "live_flights": registry.live_for("flight"),
            "live_products": registry.live_for("product"),
            "live_buses": registry.live_for("bus"),
            "telegram_automatic_delivery": env.get("RADAR_TELEGRAM_ENABLED") == "1",
            "scheduler": run_scheduler,
        }

    @app.get("/api/catalog")
    def get_catalog():
        return catalog.public_catalog() | {"providers": registry.describe()}

    @app.get("/api/status")
    def status():
        now = time.time()
        store = registry.get("store")
        return {
            "scheduler": scheduler.status(now),
            "domains": gate.states(),
            "usage_this_month": budget.usage(db, now),
            "providers": registry.describe(),
            "egress": store.egress.describe() if store is not None else [],
            "resources": metrics.snapshot(db, now),
        }

    @app.get("/api/incidents")
    def incident_list(limit: int = Query(50, ge=1, le=200), domain: str | None = None):
        return restrictions.incidents(db, limit, domain)

    @app.get("/api/compare")
    def compare_stores(limit: int = Query(50, ge=1, le=200)):
        return compare.compare_groups(db, time.time(), limit)

    class EgressSwitch(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        enabled: bool

    @app.post("/api/egress/{name}")
    def egress_switch(name: str, value: EgressSwitch):
        store = registry.get("store")
        if store is None:
            raise HTTPException(404, "No hay fuente de tiendas configurada.")
        try:
            store.egress.set_enabled(name, value.enabled)
        except KeyError as exc:
            raise HTTPException(404, "Salida de red desconocida.") from exc
        return store.egress.describe()

    @app.get("/api/sources")
    def sources_catalog():
        enabled = {s["id"] for s in enabled_sources(env)}
        return [
            {k: s.get(k) for k in ("id", "name", "kind", "country", "domain", "status", "note")} | {"enabled": s["id"] in enabled}
            for s in source_catalog.load_sources().values()
        ]

    @app.get("/api/catalogs")
    def catalogs():
        with db.connect() as con:
            return [
                dict(r) | {"watches": con.execute("SELECT COUNT(*) FROM watches WHERE catalog_id=?", (r["id"],)).fetchone()[0]}
                for r in con.execute(
                    "SELECT id,source_id,provider,domain,mode,url,category,subcategory,currency,max_items,store_max,alert_mode,channel,"
                    "enabled,next_run_at,fail_count,last_run_at,last_status,last_error,found_count FROM catalogs ORDER BY domain,url"
                )
            ]

    class CatalogInput(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        url: str
        mode: Literal["listing", "sitemap"] = "listing"
        category: str
        subcategory: str | None = None
        currency: Literal["USD", "PEN", "EUR"] = "PEN"
        include_regex: str | None = None
        max_items: int = 25
        alert_mode: Literal["smart"] = "smart"
        channel: Literal["simulation", "telegram"] = "simulation"

    @app.post("/api/catalogs", status_code=201)
    def catalog_create(value: CatalogInput):
        store = registry.get("store")
        if store is None:
            raise HTTPException(422, "No hay fuente de tiendas configurada (RADAR_ALLOWED_STORE_DOMAINS).")
        try:
            url = normalize_url(value.url)
            catalog.resolve_category("product", value.category, value.subcategory)
            if value.include_regex:
                if len(value.include_regex) > 200:
                    raise ValueError("include_regex admite como máximo 200 caracteres.")
                re.compile(value.include_regex)
            if not 1 <= value.max_items <= 500:
                raise ValueError("max_items debe estar entre 1 y 500.")
        except re.error as exc:
            raise HTTPException(422, f"include_regex no es una expresión regular válida: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        host = urlsplit(url).hostname
        if not store.allows(host):
            raise HTTPException(422, "Esa tienda no está en RADAR_ALLOWED_STORE_DOMAINS.")
        key = uuid.uuid4().hex
        try:
            with db.connect() as con:
                con.execute(
                    "INSERT INTO catalogs(id,source_id,provider,domain,mode,url,category,subcategory,currency,include_regex,max_items,store_max,"
                    "alert_mode,channel,enabled,next_run_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                    (key, "manual", "store", host, value.mode, url, value.category, value.subcategory, value.currency,
                     value.include_regex, value.max_items, value.max_items, value.alert_mode, value.channel, time.time() + 10, time.time()),
                )
        except sqlite3.IntegrityError as exc:  # UNIQUE(source_id, mode, url)
            raise HTTPException(409, "Ya existe un catálogo con esa URL.") from exc
        return {"id": key}

    class CatalogSwitch(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        enabled: bool

    @app.patch("/api/catalogs/{key}")
    def catalog_switch(key: str, value: CatalogSwitch):
        with db.connect() as con:
            changed = con.execute(
                "UPDATE catalogs SET enabled=?,fail_count=0,last_error=CASE WHEN ? THEN NULL ELSE last_error END WHERE id=?",
                (int(value.enabled), int(value.enabled), key),
            ).rowcount
        if not changed:
            raise HTTPException(404, "Catálogo no encontrado.")
        return {"id": key, "enabled": value.enabled}

    @app.post("/api/domains/{domain}/resume")
    def resume(domain: str):
        if not gate.resume(domain):
            raise HTTPException(404, "Dominio sin estado registrado.")
        return {"domain": domain, "status": "ok"}

    @app.post("/api/watches", status_code=201)
    def create(value: WatchInput):
        provider = registry.get(value.provider)
        if provider is None or value.criteria.kind not in provider.kinds:
            raise HTTPException(
                422, f"El proveedor «{value.provider}» no está disponible para este tipo de seguimiento."
            )
        allows = getattr(provider, "allows", None)
        if allows and value.criteria.kind == "product" and not allows(value.criteria.domain):
            raise HTTPException(422, "Esa tienda no está en RADAR_ALLOWED_STORE_DOMAINS.")
        try:
            return get_watch(db, add_watch(db, value))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.patch("/api/watches/{key}")
    def configure(key: str, value: WatchConfig):
        try:
            current = get_watch(db, key)
        except NotFound as exc:
            raise HTTPException(404, "Seguimiento no encontrado.") from exc
        mode = value.alert_mode or current["alert_mode"]
        if mode != "smart" and value.target_cents is None and current["target_cents"] == NO_TARGET:
            raise HTTPException(422, "Indica el precio objetivo para este modo de alerta.")
        with db.connect() as con:
            con.execute(
                "UPDATE watches SET target_cents=COALESCE(?,target_cents),enabled=?,channel=?,alert_mode=COALESCE(?,alert_mode) WHERE id=?",
                (value.target_cents, int(value.enabled), value.channel, value.alert_mode, key),
            )
        return get_watch(db, key)

    @app.get("/api/watches")
    def watches(
        kind: Literal["flight", "bus", "product"] | None = None,
        category: str | None = None,
    ):
        with db.connect() as con:
            results = []
            query = (
                "SELECT w.*,s.next_run_at,s.last_status AS schedule_status,s.fail_count "
                "FROM watches w LEFT JOIN watch_schedule s ON s.watch_id=w.id WHERE 1=1"
            )
            args = []
            if kind:
                query += " AND w.kind=?"
                args.append(kind)
            if category:
                query += " AND w.category=?"
                args.append(category)
            for row in con.execute(query + " ORDER BY w.created_at DESC", args).fetchall():
                record = watch_record(row)
                last = con.execute(
                    "SELECT * FROM queries WHERE watch_id=? ORDER BY started_at DESC,id DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                success = con.execute(
                    "SELECT * FROM queries WHERE watch_id=? AND status='success' ORDER BY started_at DESC,id DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                recent = []
                if success:
                    recent = [
                        dict(r)
                        for r in con.execute(
                            "SELECT o.* FROM observations o JOIN query_observations q ON q.observation_id=o.id WHERE q.query_id=? ORDER BY o.total_cents",
                            (success["id"],),
                        )
                    ]
                provider = registry.get(row["provider"])
                record["provider_available"] = provider is not None
                record["last_query"] = dict(last) if last else None
                record["last_success_at"] = success["finished_at"] if success else None
                record["latest_offers"] = [
                    offer
                    | {
                        "expired": offer["expires_at"] <= time.time(),
                        "conditions": json.loads(offer["conditions_json"]),
                    }
                    for offer in recent
                ]
                results.append(record)
            return results

    class RefreshInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        scenario: Literal[SCENARIOS] = "baseline"  # only the synthetic provider uses it

    @app.post("/api/watches/{key}/refresh")
    def update(key: str, value: RefreshInput):
        try:
            watch = get_watch(db, key)
        except NotFound as exc:
            raise HTTPException(404, "Seguimiento no encontrado.") from exc
        provider = registry.get(watch["provider"])
        domain, live = "unconfigured", False
        if provider is not None and watch["kind"] in provider.kinds:
            from .models import parse_criteria

            criteria = parse_criteria(watch["kind"], watch["criteria"])
            domain, live = provider.domain(criteria), provider.live
            if live:
                hint = getattr(provider, "min_gap_hint", lambda c: 0.0)(criteria)
                if not gate.reserve(domain, time.time(), hint):
                    allowed, reason, wait = gate.check(domain, time.time())
                    raise HTTPException(
                        429,
                        reason or "Dominio en espera.",
                        headers={"Retry-After": str(int(wait) + 1) if wait else "3600"},
                    )
        try:
            with trace.collect() as records:
                result = refresh(db, key, value.scenario, registry=registry)
        except Busy as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "5"}) from exc
        if live:
            now = time.time()
            last = scheduler._account(records, domain, now)
            scheduler._stamp_query(result.get("query_id"), last)
            if result["status"] == "success":
                gate.record_success(domain, now)
            elif result.get("error_kind") not in ("past", "budget"):
                # Same reaction as the scheduler: explicit rejections pause the whole domain.
                scheduler._react(
                    result.get("error_kind", "provider"), result["error"], result.get("retry_after"), domain, now, 0,
                    row={"id": key}, last=last, provider=provider, http_status=result.get("http_status"), url=result.get("url"),
                )
        return result

    @app.get("/api/watches/{key}/history")
    def history(
        key: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
    ):
        try:
            get_watch(db, key)
        except NotFound as exc:
            raise HTTPException(404, "Seguimiento no encontrado.") from exc
        with db.connect() as con:
            queries = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM queries WHERE watch_id=? ORDER BY started_at DESC,id DESC LIMIT ? OFFSET ?",
                    (key, limit, offset),
                )
            ]
            page = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM observations WHERE watch_id=? ORDER BY observed_at DESC,id DESC LIMIT ? OFFSET ?",
                    (key, limit, offset),
                )
            ]
            rows = []
            for row in page:
                prior = (
                    [
                        dict(r)
                        for r in con.execute(
                            "SELECT * FROM observations WHERE watch_id=? AND comparison_key=? AND observed_at<? "
                            "ORDER BY observed_at DESC,id DESC LIMIT 500",
                            (key, row["comparison_key"], row["observed_at"]),
                        )
                    ]
                    if row["comparison_key"]
                    else []
                )
                rows.append(
                    row
                    | {
                        "conditions": json.loads(row["conditions_json"]),
                        "expired": row["expires_at"] <= time.time(),
                        "analytics": analytics.summarize(row, prior),
                    }
                )
            return {
                "queries": queries,
                "observations": rows,
                "observation_count": con.execute(
                    "SELECT COUNT(*) FROM observations WHERE watch_id=?", (key,)
                ).fetchone()[0],
                "query_count": con.execute(
                    "SELECT COUNT(*) FROM queries WHERE watch_id=?", (key,)
                ).fetchone()[0],
            }

    @app.get("/api/alerts")
    def alerts(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        with db.connect() as con:
            return [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM alerts ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            ]

    @app.post("/api/demo", status_code=201)
    def demo(kind: Literal["flight", "bus", "product"] = "flight"):
        if registry.get("demo") is None:
            raise HTTPException(404, "El proveedor sintético está desactivado en este despliegue.")
        departure = (date.today() + timedelta(days=45)).isoformat()
        back = (date.today() + timedelta(days=59)).isoformat()
        value = {
            "flight": lambda: WatchInput(
                criteria=Criteria(origin="LIM", destination="MAD", departure=departure, return_date=back, adults=1),
                target_cents=35000,
            ),
            "bus": lambda: WatchInput(
                criteria=BusCriteria(origin="Lima", destination="Arequipa", departure=departure, passengers=1),
                target_cents=7000,
                label="Demo bus Lima → Arequipa",
            ),
            "product": lambda: WatchInput(
                criteria=ProductCriteria(url="https://tienda-demo.example/producto/laptop-123", currency="PEN"),
                target_cents=210000,
                label="Demo laptop",
                category="tecnologia",
                subcategory="laptops",
            ),
        }[kind]()
        try:
            key = add_watch(db, value)
        except ValueError:
            with db.connect() as con:
                row = con.execute(
                    "SELECT id FROM watches WHERE provider='demo' AND criteria_json=?",
                    (value.criteria.canonical(),),
                ).fetchone()
                if not row:
                    raise HTTPException(
                        409, "No se pudo crear la demo; revisa el límite de seguimientos."
                    )
                key = row[0]
        try:
            result = refresh(db, key, registry=registry)
        except Busy:
            result = {"status": "rate_limited"}
        return {"watch_id": key, "synthetic": True, "query": result}

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static/index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app
