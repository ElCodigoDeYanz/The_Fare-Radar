"""Orchestration and delivery adapters. Never calculates fare statistics or rules."""

import argparse
import json
import logging
import os
from pathlib import Path
import random
import signal
import threading
import time
import httpx  # noqa: F401  (kept importable for tests that mock the Telegram transport)
from . import metrics, restrictions, sources as source_catalog
from .database import Database
from .models import parse_criteria
from .providers.registry import build_registry, enabled_sources
from .scheduler import from_env
from .security import install_log_redaction
from .service import refresh, Busy

ROOT = Path(__file__).resolve().parents[1]

from .notifications import deliver_telegram, notify_incidents  # noqa: E402


def backup(db, target):
    """SQLite online backup: consistent even while the scheduler is writing."""
    import sqlite3

    target = Path(target)
    if target.exists():
        raise SystemExit(f"{target} ya existe; no se sobrescribe.")
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(db.path)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    return f"copia creada: {target}"


def make_after_tick(db, env=os.environ):
    """After each scheduler cycle: deliver pending alerts (highest priority first) and tell the
    owner about important access incidents. Both are opt-in (RADAR_TELEGRAM_ENABLED=1)."""
    log = logging.getLogger("radar.delivery")

    def after():
        if env.get("RADAR_TELEGRAM_ENABLED") != "1":
            return
        try:
            with db.connect() as con:
                pending = con.execute("SELECT COUNT(*) FROM alerts WHERE channel='telegram' AND status='pending'").fetchone()[0]
                incidents = con.execute("SELECT COUNT(*) FROM incidents WHERE notified=0 AND severity='high'").fetchone()[0]
            if pending:
                deliver_telegram(db)
            if incidents:
                notify_incidents(db)
        except RuntimeError as exc:
            log.error("telegram delivery skipped: %s", exc)

    return after


def run_foreground(db, registry, seconds=None):
    """Run the scheduler in this process until SIGTERM/SIGINT (or `seconds` elapse)."""
    summary = source_catalog.import_enabled(db, os.environ)
    if summary:
        logging.getLogger("radar").info("catálogos de RADAR_ENABLED_SOURCES: %s", summary)
    scheduler = from_env(db, registry, after_tick=make_after_tick(db))
    done = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: done.set())
    scheduler.start()
    logging.getLogger("radar").info("monitor en marcha (intervalo %s-%s s)", scheduler.min_s, scheduler.max_s)
    done.wait(seconds)
    scheduler.stop()
    return scheduler.status()


def main():
    logging.basicConfig(level=os.environ.get("RADAR_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    install_log_redaction([os.environ.get("RADAR_TELEGRAM_TOKEN", ""), os.environ.get("RADAR_ADMIN_PASSWORD", "")])
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--refresh-all", action="store_true", help="one query per enabled watch, honouring domain politeness")
    group.add_argument("--deliver-telegram", action="store_true")
    group.add_argument("--status", action="store_true", help="print scheduler/domain/budget state")
    group.add_argument("--backup", metavar="FILE", help="consistent online copy of the database (safe while running)")
    group.add_argument("--run", action="store_true", help="run the monitor in the foreground (servers, containers)")
    group.add_argument("--import-sources", action="store_true", help="create catalogs for RADAR_ENABLED_SOURCES (or --only)")
    group.add_argument("--list-sources", action="store_true", help="show the shipped catalogue of sources")
    group.add_argument("--verify-sources", action="store_true", help="politely check robots/sitemaps/JSON-LD of the selected stores")
    group.add_argument("--resume", metavar="DOMAIN", help="re-enable a paused domain after you reviewed it")
    group.add_argument("--incidents", nargs="?", const=30, type=int, metavar="N", help="latest access incidents")
    group.add_argument("--metrics", action="store_true", help="CPU, memory, storage and bandwidth of the monitor")
    parser.add_argument("--only", metavar="SPEC", help="source selector: all | country=PE | cat=tecnologia | id,id")
    parser.add_argument("--run-for", type=float, metavar="SECONDS", help="with --run: stop after this long")
    parser.add_argument("--out", metavar="FILE", help="with --verify-sources: write the JSON report here")
    args = parser.parse_args()

    if args.list_sources:
        for s in source_catalog.load_sources().values():
            print(f"{s['id']:<22} {s['kind']:<8} {s['country']:<3} {s['domain']:<30} {s['status']}")
        return
    db = Database(os.environ.get("RADAR_DB", ROOT / "runtime/radar.sqlite3"))
    if args.backup:
        print(backup(db, args.backup))
        return
    if args.metrics:
        print(json.dumps(metrics.snapshot(db), indent=2, ensure_ascii=False))
        return
    if args.incidents is not None:
        for row in restrictions.incidents(db, args.incidents):
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(row["at"]))
            print(f"{when}Z {row['severity']:<6} {row['domain']:<28} {row['kind']:<12} HTTP {row['http_status'] or '-':<4} {row['action']:<14} {row['detail'][:90]}")
        return
    if args.resume:
        gate = from_env(db, build_registry()).gate
        print("reanudado" if gate.resume(args.resume) else "dominio sin estado registrado")
        return
    registry = build_registry()
    if args.deliver_telegram:
        print(deliver_telegram(db))
        return
    if args.import_sources or args.verify_sources:
        spec = args.only or os.environ.get("RADAR_ENABLED_SOURCES", "")
        if not spec:
            raise SystemExit("Indica qué fuentes: --only all|country=PE|cat=tecnologia|id,id (o RADAR_ENABLED_SOURCES).")
        try:
            selected = source_catalog.select(source_catalog.load_sources(), spec)
        except source_catalog.SourceError as exc:
            raise SystemExit(f"Selección de fuentes inválida: {exc}") from exc
        now, rng = time.time(), random.Random()
        if args.import_sources:
            channel = "telegram" if os.environ.get("RADAR_TELEGRAM_ENABLED") == "1" else "simulation"
            print(json.dumps(source_catalog.import_catalogs(
                db, selected, now, rng,
                items_per_category=int(os.environ.get("RADAR_ITEMS_PER_CATEGORY", 10)),
                store_max=int(os.environ.get("RADAR_STORE_MAX_ITEMS", 40)),
                channel=channel,
            )))
            print("Dominios que deben estar permitidos:", ",".join(source_catalog.allowed_domains(selected)))
            return
        from .verify import verify_all

        provider = build_registry(dict(os.environ) | {"RADAR_ENABLED_SOURCES": spec}).get("store")
        report = verify_all(provider, selected, now, pause=lambda: time.sleep(rng.uniform(2, 6)), between=lambda: time.sleep(rng.uniform(5, 15)))
        text = json.dumps(report, indent=2, ensure_ascii=False)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(text)
        print(json.dumps(report["summary"], ensure_ascii=False))
        return
    if args.run:
        print(json.dumps(run_foreground(db, registry, args.run_for), indent=2, ensure_ascii=False))
        return
    scheduler = from_env(db, registry)
    if args.status:
        print(json.dumps(scheduler.status() | {"resources": metrics.snapshot(db)}, indent=2, ensure_ascii=False))
        return
    gate = scheduler.gate
    with db.connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM watches WHERE enabled=1")]
    for row in rows:
        key = row["id"]
        provider = registry.get(row["provider"])
        try:
            if provider is not None and provider.live and row["kind"] in provider.kinds:
                criteria = parse_criteria(row["kind"], json.loads(row["criteria_json"]))
                if not gate.reserve(provider.domain(criteria), time.time()):
                    print(key, "domain paused or spaced; skipped")
                    continue
            result = refresh(db, key, scenario="auto", registry=registry)
            print(key, result["status"])
        except Busy:
            print(key, "rate limited; skipped")
        except Exception as exc:  # noqa: BLE001
            print(key, "failed", type(exc).__name__)


if __name__ == "__main__":
    main()
