"""Background scheduler: every watch is re-checked at a random moment in [min_s, max_s].

Design (see README, "Programación y cortesía"):
  * each watch gets its own random next_run_at after EVERY run (default 300-900 s = 5-15 min), so no two
    watches, and no two cycles of the same watch, share a rhythm;
  * live sources are additionally gated per domain (radar.politeness): a jittered minimum gap
    between any two requests, exponential backoff on 403/429/challenge, circuit breaker;
  * if there are more watches on one domain than the gap allows, checks stretch automatically
    (reported in status()["capacity"]) instead of bursting;
  * a lease row in SQLite guarantees one active scheduler even if two instances start;
  * the clock and the RNG are injected, so tests run it deterministically without sleeping.
"""

from concurrent.futures import ThreadPoolExecutor
import calendar
import json
import logging
import os
import random
import threading
import time
import uuid
from . import budget, discovery, restrictions, trace
from .models import parse_criteria
from .politeness import DomainGate
from .providers.base import ProviderError
from .restrictions import PAUSE_KINDS, TRANSIENT_KINDS, WATCH_KINDS
from .service import Busy, refresh

log = logging.getLogger("radar.scheduler")
LEASE_TTL = 60.0
PAUSE_AFTER_FAILS = 8  # consecutive failures of one watch before it is paused for a human
GONE_AFTER = 3  # consecutive 404/410 before a watch is paused
STRUCTURE_AFTER = 5  # consecutive "page changed" before a watch is paused
SITEMAP_EVERY = (6 * 3600.0, 12 * 3600.0)


class Scheduler:
    def __init__(
        self,
        db,
        registry,
        gate=None,
        *,
        min_s=300.0,
        max_s=900.0,
        rng=None,
        clock=time.time,
        max_workers=4,
        owner=None,
        after_tick=None,
        pause_after_fails=PAUSE_AFTER_FAILS,
        confirm_window=(3600.0, 7200.0),
        sleeper=time.sleep,
    ):
        if not 0 < min_s <= max_s:
            raise ValueError("Intervalo inválido: se requiere 0 < min <= max.")
        self.db, self.registry = db, registry
        self.rng = rng or random.Random()
        self.gate = gate or DomainGate(db, rng=self.rng)
        self.min_s, self.max_s = float(min_s), float(max_s)
        self.clock, self.max_workers = clock, max_workers
        self.owner = owner or f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.after_tick = after_tick
        self.pause_after_fails = pause_after_fails
        self.confirm_window = confirm_window
        self.sleeper = sleeper
        self.last_tick_at = None
        self.last_error = None
        self.holds_lease = False
        self._stop = threading.Event()
        self._thread = None

    # --- lease --------------------------------------------------------------------
    def hold_lease(self, now):
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM scheduler_lease WHERE id=1").fetchone()
            if row is None or row["owner"] == self.owner or row["expires_at"] <= now:
                con.execute(
                    "INSERT INTO scheduler_lease(id,owner,expires_at) VALUES (1,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at",
                    (self.owner, now + LEASE_TTL),
                )
                self.holds_lease = True
            else:
                self.holds_lease = False
            return self.holds_lease

    def release_lease(self):
        with self.db.connect() as con:
            con.execute("DELETE FROM scheduler_lease WHERE id=1 AND owner=?", (self.owner,))
        self.holds_lease = False

    # --- scheduling ---------------------------------------------------------------
    def next_delay(self):
        return self.rng.uniform(self.min_s, self.max_s)

    @staticmethod
    def _seconds_left_in_month(now):
        t = time.gmtime(now)
        last = calendar.monthrange(t.tm_year, t.tm_mon)[1]
        end = calendar.timegm((t.tm_year, t.tm_mon, last, 23, 59, 59))
        return max(60.0, end - now + 1)

    def pace(self, provider, now, used=None, watches=None):
        """Minimum seconds between checks of ONE watch so a metered provider's monthly cap
        lasts until the month ends (instead of being spent on day one)."""
        cap = getattr(provider, "monthly_cap", None)
        if not cap:
            return 0.0
        if used is None:
            used = budget.usage(self.db, now).get(provider.name, 0)
        if watches is None:
            with self.db.connect() as con:
                watches = con.execute(
                    "SELECT COUNT(*) FROM watches WHERE enabled=1 AND provider=?", (provider.name,)
                ).fetchone()[0]
        return self._seconds_left_in_month(now) * max(1, watches) / max(1, cap - used)

    def ensure_schedules(self, now):
        """Give enabled watches without a schedule a first run soon (5 s .. min_s, random)."""
        with self.db.connect() as con:
            missing = [
                r[0]
                for r in con.execute(
                    "SELECT w.id FROM watches w LEFT JOIN watch_schedule s ON s.watch_id=w.id "
                    "WHERE w.enabled=1 AND s.watch_id IS NULL"
                )
            ]
            for key in missing:
                con.execute(
                    "INSERT OR IGNORE INTO watch_schedule(watch_id,next_run_at) VALUES (?,?)",
                    (key, now + self.rng.uniform(5.0, max(6.0, self.min_s))),
                )

    def _due(self, now):
        with self.db.connect() as con:
            return [
                dict(r)
                for r in con.execute(
                    "SELECT w.*,s.fail_count FROM watch_schedule s JOIN watches w ON w.id=s.watch_id "
                    "WHERE w.enabled=1 AND s.next_run_at<=? ORDER BY s.next_run_at LIMIT 200",
                    (now,),
                )
            ]

    def _set_next(self, key, at, now, status, fail_count):
        with self.db.connect() as con:
            con.execute(
                "UPDATE watch_schedule SET next_run_at=?,fail_count=?,last_run_at=?,last_status=? WHERE watch_id=?",
                (at, fail_count, now, status, key),
            )

    def _due_catalogs(self, now):
        with self.db.connect() as con:
            return [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM catalogs WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at LIMIT 50",
                    (now,),
                )
            ]

    def tick(self, now=None):
        """Run every watch/catalog that is due AND allowed by its domain gate. Returns run summaries."""
        now = self.clock() if now is None else now
        if not self.hold_lease(now):
            return []
        self.ensure_schedules(now)
        jobs, seen_domains = [], set()
        # Catalog sweeps first: one listing request can price dozens of watches.
        for catalog in self._due_catalogs(now):
            if len(jobs) >= self.max_workers:
                break
            provider = self.registry.get(catalog["provider"])
            if provider is None or not getattr(provider, "live", False):
                jobs.append(("catalog", catalog, None, catalog["domain"]))
                continue
            domain = catalog["domain"]
            if domain in seen_domains:
                continue
            hint = getattr(provider, "crawl_delay", lambda host: 0.0)(domain)
            if not self.gate.reserve(domain, now, hint):
                continue
            seen_domains.add(domain)
            jobs.append(("catalog", catalog, provider, domain))
        for row in self._due(now):
            if len(jobs) >= self.max_workers:
                break
            provider = self.registry.get(row["provider"])
            if provider is None or row["kind"] not in provider.kinds:
                # Not configured here: run it anyway so the journal records WHY there is
                # no price (refresh() reports it), then back off through the normal path.
                jobs.append(("watch", row, None, "unconfigured"))
                continue
            criteria = parse_criteria(row["kind"], json.loads(row["criteria_json"]))
            domain = provider.domain(criteria)
            if provider.live:
                if domain in seen_domains:
                    continue  # never two simultaneous requests to one domain
                hint = getattr(provider, "min_gap_hint", lambda c: 0.0)(criteria)
                if not self.gate.reserve(domain, now, hint):
                    continue  # deferred: stays due, runs when the domain allows it
                seen_domains.add(domain)
            jobs.append(("watch", row, provider, domain))
        if not jobs:
            self.last_tick_at = now
            return []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            results = list(pool.map(lambda job: self._dispatch(*job), jobs))
        self.last_tick_at = now
        return results

    def _dispatch(self, what, row, provider, domain):
        return self._run_catalog(row, provider, domain) if what == "catalog" else self._run(row, provider, domain)

    # --- shared reaction to what a source answered ----------------------------------------
    def _account(self, records, domain, started):
        """Feed the traced requests to the gate (traffic + AutoThrottle). Returns the last record."""
        for rec in records:
            self.gate.record_request(
                rec["domain"] or domain, started, rec["latency_s"], rec["status"], rec["bytes"], rec["egress"]
            )
        return records[-1] if records else None

    def _incident(self, now, domain, kind, detail, *, row=None, catalog=None, last=None, provider=None, action=None, status=None, url=None):
        restrictions.record_incident(
            self.db, now, domain=domain, kind=kind, detail=detail, provider=provider,
            watch_id=row["id"] if row else None, catalog_id=catalog["id"] if catalog else None,
            url=url or (last or {}).get("url"), status=status if status is not None else (last or {}).get("status"),
            egress=(last or {}).get("egress"), action=action,
        )

    def _react(self, kind, error, retry_after, domain, started, fails, *, row=None, catalog=None, last=None, provider=None, http_status=None, url=None):
        """Decide what a failure means. Returns (next_run_at, pause_target) where pause_target says
        whether the watch/catalog itself must be disabled for a human to look at."""
        name = provider.name if provider else None
        ctx = dict(row=row, catalog=catalog, last=last, provider=name, status=http_status, url=url)
        if kind in PAUSE_KINDS:
            resume = self.gate.record_block(domain, started, retry_after, error)
            self._incident(started, domain, kind, error, **ctx)
            if resume is None:
                self._incident(started, domain, "circuit_open",
                               "Circuito abierto tras bloqueos repetidos; revisa la fuente y reanúdala a mano.", **ctx)
            log.warning("scheduler: %s %s → domain paused until %s", domain, kind, resume)
            return (resume or started + 86400) + self.rng.uniform(0, self.min_s), False
        if kind in WATCH_KINDS:
            limit = {"gone": GONE_AFTER, "structure": STRUCTURE_AFTER}.get(kind, self.pause_after_fails)
            if fails + 1 >= limit:
                self._incident(started, domain, kind, f"{error} (pausado tras {fails + 1} fallos seguidos; revísalo)", **ctx)
                return started + 365 * 86400, True
            self._incident(started, domain, kind, error, **ctx)
        elif kind in TRANSIENT_KINDS:
            until = self.gate.record_trouble(domain, started)
            if until is not None:
                self._incident(started, domain, kind, f"{error} (dominio en pausa breve por fallos repetidos)", **ctx, action="domain_cooling")
        if fails + 1 >= self.pause_after_fails:
            self._incident(started, domain, kind, f"{error} (pausado tras {fails + 1} fallos seguidos)", **ctx, action="paused")
            return started + 365 * 86400, True
        return started + min(3600.0, self.next_delay() * 2 ** min(fails + 1, 4)), False

    def _stamp_query(self, query_id, last):
        if query_id and last:
            with self.db.connect() as con:
                con.execute(
                    "UPDATE queries SET egress=?,http_status=? WHERE id=?",
                    (last.get("egress"), last.get("status"), query_id),
                )

    def _run(self, row, provider, domain):
        key, started = row["id"], self.clock()
        fails = row["fail_count"]
        live = bool(getattr(provider, "live", False))
        with trace.collect() as records:
            try:
                result = refresh(
                    self.db, key, scenario="auto", provider=provider, now=started, registry=self.registry
                )
            except Busy:
                wait = max(getattr(provider, "min_interval", 5), 5) + self.rng.uniform(0, 5)
                self._set_next(key, started + wait, started, "busy", fails)
                return {"watch_id": key, "status": "busy", "domain": domain}
            except Exception as exc:  # noqa: BLE001 - one bad watch must never stop the loop
                log.exception("scheduler: unexpected failure for %s", key)
                self.last_error = f"{type(exc).__name__}"
                delay = min(3600.0, self.next_delay() * 2 ** min(fails + 1, 4))
                self._set_next(key, started + delay, started, "internal", fails + 1)
                return {"watch_id": key, "status": "internal", "domain": domain}
        last = self._account(records, domain, started) if live else None
        self._stamp_query(result.get("query_id"), last)

        if result["status"] == "success":
            if live:
                self.gate.record_success(domain, started)
            # Random window, but never faster than the monthly budget allows (±10 % jitter).
            pace = self.pace(provider, started) * self.rng.uniform(0.9, 1.1) if provider else 0.0
            self._set_next(key, started + max(self.next_delay(), pace), started, "success", 0)
            return {"watch_id": key, "status": "success", "domain": domain}

        kind = result.get("error_kind", "provider")
        if kind == "past":
            # A trip in the past can never succeed: pause it instead of querying forever.
            with self.db.connect() as con:
                con.execute("UPDATE watches SET enabled=0 WHERE id=?", (key,))
            self._set_next(key, started + 365 * 86400, started, kind, fails)
            log.info("scheduler: watch %s paused, its travel date has passed", key)
        elif kind == "budget":
            self._set_next(key, started + 6 * 3600, started, kind, fails)
        else:
            at, pause = self._react(
                kind, result["error"], result.get("retry_after"), domain, started, fails,
                row=row, last=last, provider=provider, http_status=result.get("http_status"), url=result.get("url"),
            )
            if pause:
                with self.db.connect() as con:
                    con.execute("UPDATE watches SET enabled=0 WHERE id=?", (key,))
                log.warning("scheduler: watch %s paused after repeated %s", key, kind)
            self._set_next(key, at, started, kind, fails + 1)
        return {"watch_id": key, "status": kind, "domain": domain, "error": result["error"]}

    def _run_catalog(self, row, provider, domain):
        key, started = row["id"], self.clock()
        fails = row["fail_count"]
        if provider is None:
            self._catalog_done(key, started, started + 6 * 3600, "unconfigured", fails + 1,
                               "La fuente de este catálogo no está configurada en este despliegue.", 0)
            return {"catalog_id": key, "status": "unconfigured", "domain": domain}
        error = None
        with trace.collect() as records:
            try:
                summary = discovery.sweep(
                    self.db, provider, row, started, rng=self.rng, pause=lambda: self.sleeper(self.rng.uniform(3.0, 8.0)),
                    confirm=self.confirm_window,
                )
            except ProviderError as exc:
                error = exc
            except Exception as exc:  # noqa: BLE001
                log.exception("scheduler: catalog %s failed", key)
                self.last_error = f"{type(exc).__name__}"
                self._catalog_done(key, started, started + 3600, "internal", fails + 1, "Error interno en el catálogo.", 0)
                return {"catalog_id": key, "status": "internal", "domain": domain}
        last = self._account(records, domain, started)
        if error is None:
            self.gate.record_success(domain, started)
            window = (self.min_s, self.max_s) if row["mode"] == "listing" else SITEMAP_EVERY
            self._catalog_done(key, started, started + self.rng.uniform(*window), "success", 0, None, summary["found"])
            return {"catalog_id": key, "status": "success", "domain": domain, **summary}
        kind = getattr(error, "error_kind", "provider")
        at, pause = self._react(
            kind, str(error), getattr(error, "retry_after", None), domain, started, fails,
            catalog=row, last=last, provider=provider, http_status=error.status, url=error.url,
        )
        self._catalog_done(key, started, at, kind, fails + 1, str(error)[:300], row["found_count"], disable=pause)
        return {"catalog_id": key, "status": kind, "domain": domain, "error": str(error)}

    def _catalog_done(self, key, now, next_at, status, fails, error, found, disable=False):
        with self.db.connect() as con:
            con.execute(
                "UPDATE catalogs SET next_run_at=?,fail_count=?,last_run_at=?,last_status=?,last_error=?,found_count=?,enabled=CASE WHEN ? THEN 0 ELSE enabled END WHERE id=?",
                (next_at, fails, now, status, error, found, int(disable), key),
            )

    # --- loop ---------------------------------------------------------------------
    def next_wakeup(self, now):
        with self.db.connect() as con:
            row = con.execute(
                "SELECT MIN(s.next_run_at) FROM watch_schedule s JOIN watches w ON w.id=s.watch_id WHERE w.enabled=1"
            ).fetchone()
        due = row[0] if row and row[0] is not None else now + 5
        return max(0.5, min(5.0, due - now))

    def run_forever(self):
        log.info("scheduler started owner=%s interval=%s-%ss", self.owner, self.min_s, self.max_s)
        while not self._stop.is_set():
            try:
                self.tick()
                if self.after_tick:
                    self.after_tick()
            except Exception as exc:  # noqa: BLE001
                log.exception("scheduler tick failed")
                self.last_error = f"{type(exc).__name__}"
            self._stop.wait(self.next_wakeup(self.clock()))
        self.release_lease()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="radar-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout=10):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    # --- reporting ----------------------------------------------------------------
    def status(self, now=None):
        now = self.clock() if now is None else now
        gap_mean = sum(self.gate.gap) / 2
        interval_mean = (self.min_s + self.max_s) / 2
        with self.db.connect() as con:
            enabled = con.execute("SELECT COUNT(*) FROM watches WHERE enabled=1").fetchone()[0]
            due = con.execute(
                "SELECT COUNT(*) FROM watch_schedule s JOIN watches w ON w.id=s.watch_id WHERE w.enabled=1 AND s.next_run_at<=?",
                (now,),
            ).fetchone()[0]
            by_provider = con.execute(
                "SELECT provider,kind,COUNT(*) n FROM watches WHERE enabled=1 GROUP BY provider,kind"
            ).fetchall()
            lease = con.execute("SELECT owner,expires_at FROM scheduler_lease WHERE id=1").fetchone()
        capacity = []
        for row in by_provider:
            provider = self.registry.get(row["provider"])
            if not provider or not provider.live:
                continue
            # Watches sharing one domain are served one gap apart, so a full round takes n * gap.
            round_s = row["n"] * gap_mean
            paced = self.pace(provider, now, watches=row["n"])
            if paced > max(round_s, interval_mean):
                capacity.append(
                    {
                        "provider": row["provider"],
                        "watches": row["n"],
                        "effective_interval_s": round(paced),
                        "note": "El tope mensual de consultas de esta fuente obliga a espaciarlas "
                        "para que el cupo dure todo el mes.",
                    }
                )
            elif round_s > interval_mean:
                capacity.append(
                    {
                        "provider": row["provider"],
                        "watches": row["n"],
                        "effective_interval_s": round(round_s),
                        "note": "Más seguimientos que el espaciado permite: cada uno se revisará "
                        "más despacio que el rango configurado (se evita ráfagas a propósito).",
                    }
                )
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "holds_lease": self.holds_lease,
            "lease_owner": lease["owner"] if lease else None,
            "interval_s": [self.min_s, self.max_s],
            "domain_gap_s": list(self.gate.gap),
            "enabled_watches": enabled,
            "due_now": due,
            "last_tick_at": self.last_tick_at,
            "last_error": self.last_error,
            "capacity": capacity,
        }


def from_env(db, registry, env=None, **kw):
    env = os.environ if env is None else env

    def number(name, default):
        try:
            return float(env.get(name, default))
        except ValueError:
            return float(default)

    gap = (number("RADAR_DOMAIN_GAP_MIN_S", 15), number("RADAR_DOMAIN_GAP_MAX_S", 45))
    gate = DomainGate(
        db,
        gap=gap,
        backoff_base=number("RADAR_BACKOFF_BASE_S", 900),
        backoff_cap=number("RADAR_BACKOFF_CAP_S", 21600),
        disable_after=int(number("RADAR_DISABLE_AFTER_BLOCKS", 5)),
        target_concurrency=number("RADAR_AUTOTHROTTLE_TARGET", 1.0),
        max_delay=number("RADAR_AUTOTHROTTLE_MAX_DELAY_S", 60),
        trouble_after=int(number("RADAR_TROUBLE_AFTER", 3)),
    )
    kw.setdefault("max_workers", int(number("RADAR_WORKERS", 8)))
    kw.setdefault("pause_after_fails", int(number("RADAR_PAUSE_AFTER_FAILS", PAUSE_AFTER_FAILS)))
    return Scheduler(
        db,
        registry,
        gate,
        min_s=number("RADAR_MIN_INTERVAL_S", 300),
        max_s=number("RADAR_MAX_INTERVAL_S", 900),
        **kw,
    )
