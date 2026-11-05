"""Query orchestration. Sources -> persistence -> analytics -> rules -> alert outbox."""

import json
import math
import os
import sqlite3
import time
import uuid
from urllib.parse import urlsplit
from . import analytics, budget, identity, priority, rules
from .models import parse_criteria
from .providers.base import BudgetExhausted, ProviderError
from .providers.demo import DemoProvider

MIN_INTERVAL = (
    5  # Fallback for providers that declare none. Not an external provider quota.
)
HISTORY_WINDOW = 500  # comparable observations used for min/avg/change


def max_watches():
    raw = os.environ.get("RADAR_MAX_WATCHES", "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else 1000


class NotFound(KeyError):
    pass


class Busy(RuntimeError):
    pass


class InvalidSource(RuntimeError):
    error_kind = "invalid"


class PastTrip(InvalidSource):
    """The travel date has passed: no source can ever answer this watch again."""

    error_kind = "past"


_DEMO = None


def default_provider(name):
    """Provider used when the caller passes none (tests, CLI). Only the demo is built in."""
    global _DEMO
    if name == "demo":
        _DEMO = _DEMO or DemoProvider()
        return _DEMO
    return None


def watch_record(row):
    data = dict(row)
    data["criteria"] = json.loads(data.pop("criteria_json"))
    return data


def add_watch(db, value, now=None, catalog_id=None):
    now = time.time() if now is None else now
    key = str(uuid.uuid4())
    limit = max_watches()
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT COUNT(*) FROM watches").fetchone()[0] >= limit:
            raise ValueError(f"Máximo {limit} seguimientos (RADAR_MAX_WATCHES).")
        try:
            con.execute(
                "INSERT INTO watches(id,kind,provider,category,subcategory,label,criteria_json,"
                "target_cents,enabled,channel,created_at,alert_mode,catalog_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    value.criteria.kind,
                    value.provider,
                    value.category,
                    value.subcategory,
                    value.label,
                    value.criteria.canonical(),
                    value.target_cents,
                    int(value.enabled),
                    value.channel,
                    now,
                    value.alert_mode,
                    catalog_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Ya existe un seguimiento con estas condiciones.") from exc
    return key


def get_watch(db, key):
    with db.connect() as con:
        row = con.execute("SELECT * FROM watches WHERE id=?", (key,)).fetchone()
        if not row:
            raise NotFound(key)
        return watch_record(row)


def _fail(db, query_id, now, message, kind):
    with db.connect() as con:
        con.execute(
            "UPDATE queries SET status='error',finished_at=?,error=?,error_kind=? WHERE id=?",
            (now, message, kind, query_id),
        )


def _validate_offer(offer, criteria, now):
    if offer.criteria.canonical() != criteria.canonical() or offer.currency != criteria.currency:
        raise InvalidSource(
            "El proveedor devolvió pasajeros, moneda o condiciones de búsqueda incompatibles."
        )
    if (
        isinstance(offer.total_cents, bool)
        or not isinstance(offer.total_cents, int)
        or offer.total_cents <= 0
    ):
        raise InvalidSource("Precio del proveedor inválido.")
    if not math.isfinite(offer.observed_at) or not math.isfinite(offer.expires_at):
        raise InvalidSource("Fecha de observación o caducidad no finita.")
    if (
        offer.observed_at > now + 1
        or offer.observed_at < now - 86400
        or not offer.provider_observation_id
    ):
        raise InvalidSource("Observación sin identidad o fecha válida.")


def peer_prices(con, watch, offer_conditions, currency, now):
    """Current in-stock prices of the SAME product (same match_key, same condition) in other
    stores. One value per other store (its latest unexpired observation)."""
    if not watch.get("match_key") or watch["kind"] != "product":
        return []
    own_host = urlsplit(watch["criteria"].get("url", "")).hostname
    prices, seen_hosts = [], {own_host}
    rows = con.execute(
        "SELECT id,criteria_json FROM watches WHERE match_key=? AND id!=? AND enabled=1",
        (watch["match_key"], watch["id"]),
    ).fetchall()
    for row in rows:
        host = urlsplit(json.loads(row["criteria_json"]).get("url", "")).hostname
        if host in seen_hosts:
            continue
        obs = con.execute(
            "SELECT total_cents,currency,conditions_json,expires_at FROM observations "
            "WHERE watch_id=? ORDER BY observed_at DESC,id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if not obs or obs["expires_at"] <= now or obs["currency"] != currency:
            continue
        cond = json.loads(obs["conditions_json"])
        if cond.get("availability") != "in_stock" or cond.get("condition") != offer_conditions.get("condition"):
            continue
        seen_hosts.add(host)
        prices.append(obs["total_cents"])
    return prices


def ingest(db, key, watch, criteria, offers, query_id, now, thresholds=None):
    """Validate offers, persist them, run analytics + rules and queue alerts. Shared by the
    per-watch refresh and the catalog sweep (one catalog page can price many watches)."""
    thresholds = thresholds or priority.Thresholds.from_env()
    for offer in offers:
        _validate_offer(offer, criteria, now)
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        inserted = []
        for offer in offers:
            obs_id = str(uuid.uuid4())
            conditions = json.dumps(offer.conditions, sort_keys=True)
            compare = analytics.comparison_key(
                criteria.canonical(), offer.conditions, offer.provider, criteria.kind
            )
            prior = con.execute(
                "SELECT * FROM observations WHERE watch_id=? AND provider=? AND provider_observation_id=?",
                (key, offer.provider, offer.provider_observation_id),
            ).fetchone()
            if prior:
                if (
                    prior["total_cents"] != offer.total_cents
                    or prior["conditions_json"] != conditions
                    or prior["currency"] != offer.currency
                ):
                    raise InvalidSource(
                        "El proveedor reutilizó una identidad de observación con contenido distinto."
                    )
                obs_id = prior["id"]
                # The same price seen again confirms it: only its validity window moves.
                con.execute(
                    "UPDATE observations SET expires_at=? WHERE id=? AND expires_at<?",
                    (offer.expires_at, obs_id, offer.expires_at),
                )
            else:
                con.execute(
                    "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        obs_id,
                        key,
                        offer.provider,
                        offer.provider_observation_id,
                        offer.observed_at,
                        offer.expires_at,
                        offer.currency,
                        offer.total_cents,
                        criteria.canonical(),
                        conditions,
                        compare,
                        offer.source,
                        int(offer.synthetic),
                    ),
                )
            con.execute(
                "INSERT OR IGNORE INTO query_observations VALUES (?,?)", (query_id, obs_id)
            )
            ids = offer.product_ids or {}
            mkey = identity.match_key(ids)
            title = ids.get("title") if isinstance(ids.get("title"), str) else None
            if mkey and watch.get("match_key") != mkey:
                con.execute("UPDATE watches SET match_key=? WHERE id=?", (mkey, key))
                watch = watch | {"match_key": mkey}
            if title and not watch.get("title"):
                con.execute("UPDATE watches SET title=? WHERE id=?", (title[:160], key))
                watch = watch | {"title": title[:160]}
            current = dict(con.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone())
            previous = (
                [
                    dict(r)
                    for r in con.execute(
                        "SELECT o.*,(SELECT COUNT(*) FROM query_observations q WHERE q.observation_id=o.id) AS seen "
                        "FROM observations o WHERE o.watch_id=? AND o.comparison_key=? AND o.observed_at<=? "
                        "ORDER BY o.observed_at DESC,o.id DESC LIMIT ?",
                        (key, compare, now, HISTORY_WINDOW),
                    )
                ]
                if compare is not None
                else []
            )
            last = con.execute(
                "SELECT created_at,priority_rank FROM alerts WHERE watch_id=? ORDER BY created_at DESC LIMIT 1",
                (key,),
            ).fetchone()
            last_alert = (last["created_at"], last["priority_rank"]) if last else None
            stats = analytics.summarize(current, previous, last["created_at"] if last else None, now)
            peers = peer_prices(con, watch, offer.conditions, offer.currency, now)
            prio = priority.classify(stats, peers, thresholds)
            stats = stats | {"priority": prio}
            should_alert, reason, label = rules.decide(watch, stats, now, prio, last_alert)
            if should_alert:
                con.execute(
                    "INSERT OR IGNORE INTO alerts(id,watch_id,observation_id,created_at,channel,status,message,priority,priority_rank) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        key,
                        obs_id,
                        now,
                        watch["channel"],
                        "simulated" if watch["channel"] == "simulation" else "pending",
                        _message(watch, criteria, offer, conditions, reason, label, prio),
                        label,
                        priority.RANK[label],
                    ),
                )
            inserted.append(
                {
                    "observation_id": obs_id,
                    "analytics": stats,
                    "alert_eligible": should_alert,
                    "priority": label if should_alert else prio["tier"],
                    "reason": reason,
                    "expired": current["expires_at"] <= now,
                }
            )
        con.execute(
            "UPDATE queries SET status='success',finished_at=? WHERE id=?", (now, query_id)
        )
    return inserted


def _message(watch, criteria, offer, conditions, reason, label, prio):
    name = watch.get("title") or watch.get("label")
    name = f"{name}: " if name else ""
    synthetic = offer.synthetic
    head = "[DEMO SINTÉTICA] " if synthetic else "[RADAR] "
    if label != "objetivo":
        head += f"[PRIORIDAD {priority.LABEL[label]}] "
    tail = (
        "El precio observado puede cambiar. No es una oferta reservable ni comprable."
        if synthetic
        else "Verifica precio y disponibilidad en la fuente antes de decidir; puede haber cambiado."
    )
    extra = ""
    if prio.get("drop_percent") is not None and label != "objetivo":
        extra = (
            f" Baja {prio['drop_percent']:.1f} % frente a la {prio['reference']} "
            f"({offer.currency} {prio['reference_cents'] / 100:.2f}); margen bruto potencial "
            f"{prio['margin_percent']:.1f} % (sin envío, comisiones ni impuestos)."
        )
        if prio["best_peer_cents"] is not None:
            extra += (
                f" Mejor precio en otras {prio['peer_count']} tienda(s): "
                f"{offer.currency} {prio['best_peer_cents'] / 100:.2f}."
            )
        if prio["suspect_error"]:
            extra += " ATENCIÓN: caída tan grande puede ser un error de precio y cancelarse."
    link = f" Enlace: {criteria.url}." if criteria.kind == "product" else ""
    return (
        f"{head}{name}{criteria.describe()}; total {offer.currency} "
        f"{offer.total_cents / 100:.2f}. {reason}{extra} Procedencia: {offer.source}.{link} "
        f"Condiciones: {conditions}. {tail}"
    )


def refresh(db, key, scenario="baseline", provider=None, now=None, registry=None):
    """One query. With a registry, the provider comes ONLY from this deployment's registry
    (a disabled or unconfigured provider yields a recorded error, never a silent fallback)."""
    now = time.time() if now is None else now
    watch = get_watch(db, key)
    criteria = parse_criteria(watch["kind"], watch["criteria"])
    if provider is None:
        provider = (
            registry.get(watch["provider"])
            if registry is not None
            else default_provider(watch["provider"])
        )
    if provider is not None and watch["kind"] not in getattr(provider, "kinds", (watch["kind"],)):
        provider = None
    min_interval = getattr(provider, "min_interval", MIN_INTERVAL)
    live = bool(getattr(provider, "live", False))
    cap = getattr(provider, "monthly_cap", None)
    synthetic = not live
    query_id = str(uuid.uuid4())
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        # Recovery records the interrupted attempt, never a successful fresh price.
        con.execute(
            "UPDATE queries SET status='error',finished_at=?,error='Consulta interrumpida; timeout local de 30 s.',error_kind='timeout' WHERE watch_id=? AND status='running' AND started_at<=?",
            (now, key, now - 30),
        )
        last = con.execute(
            "SELECT * FROM queries WHERE watch_id=? ORDER BY started_at DESC,id DESC LIMIT 1",
            (key,),
        ).fetchone()
        if last and (last["status"] == "running" or now - last["started_at"] < min_interval):
            raise Busy(
                f"Espera al menos {min_interval:g} segundos entre consultas de este seguimiento."
            )
        con.execute(
            "INSERT INTO queries(id,watch_id,started_at,finished_at,status,error) VALUES (?,?,?,NULL,'running',NULL)",
            (query_id, key, now),
        )
    try:
        if provider is None:
            raise ProviderError(
                f"El proveedor «{watch['provider']}» no está configurado en este despliegue."
            )
        if criteria.is_past(now):
            raise PastTrip("La fecha de viaje ya pasó. Crea un seguimiento nuevo.")
        if live and not budget.reserve(db, provider.name, cap, now):
            raise BudgetExhausted(
                f"Tope mensual de {cap} consultas a «{provider.name}» alcanzado."
            )
        offers = provider.fetch(criteria, scenario, now)
        inserted = ingest(db, key, watch, criteria, offers, query_id, now)
        return {
            "query_id": query_id,
            "status": "success",
            "observations": inserted,
            "synthetic": synthetic,
        }
    except (ProviderError, InvalidSource) as exc:
        kind = getattr(exc, "error_kind", "provider")
        _fail(db, query_id, now, str(exc), kind)
        return {
            "query_id": query_id,
            "status": "error",
            "error": str(exc),
            "error_kind": kind,
            "retry_after": getattr(exc, "retry_after", None),
            "http_status": getattr(exc, "status", None),
            "url": getattr(exc, "url", None),
            "synthetic": synthetic,
        }
    except Exception:
        _fail(db, query_id, now, "Error interno; no se actualizó el precio.", "internal")
        raise
