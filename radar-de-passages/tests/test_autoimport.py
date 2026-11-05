"""RADAR_ENABLED_SOURCES is enough to start monitoring: catalogs are created at start-up, idempotently."""

import signal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from radar import main as cli, sources
from radar.api import create_app
from conftest import registry_with

PAIR = {"RADAR_ENABLED_SOURCES": "falabella-pe,wong-pe"}


def catalogs(db):
    with db.connect() as con:
        return [dict(r) for r in con.execute("SELECT source_id, domain, mode, alert_mode, enabled, channel FROM catalogs ORDER BY source_id")]


def test_nothing_happens_unless_the_operator_listed_sources(db):
    assert sources.import_enabled(db, {}) is None
    assert sources.import_enabled(db, {"RADAR_ENABLED_SOURCES": "  "}) is None
    assert catalogs(db) == []


def test_listed_stores_become_one_smart_sitemap_catalog_each_and_a_second_start_adds_nothing(db):
    first = sources.import_enabled(db, PAIR, now=1_800_000_000.0)
    assert first == {"created": 2, "existing": 0, "stores": 2}
    rows = catalogs(db)
    assert [r["source_id"] for r in rows] == ["falabella-pe", "wong-pe"]
    assert {(r["mode"], r["alert_mode"], r["enabled"], r["channel"]) for r in rows} == {("sitemap", "smart", 1, "simulation")}
    assert {r["domain"] for r in rows} == {"falabella.com.pe", "wong.pe"}
    assert sources.import_enabled(db, PAIR, now=1_800_000_500.0) == {"created": 0, "existing": 2, "stores": 2}
    assert len(catalogs(db)) == 2


def test_the_first_sweep_is_spread_out_and_telegram_is_only_used_when_opted_in(db):
    sources.import_enabled(db, PAIR | {"RADAR_TELEGRAM_ENABLED": "1"}, now=1_800_000_000.0)
    assert {r["channel"] for r in catalogs(db)} == {"telegram"}
    with db.connect() as con:
        when = [r[0] for r in con.execute("SELECT next_run_at FROM catalogs")]
    assert all(1_800_000_000.0 + 300 <= t <= 1_800_000_000.0 + 3600 for t in when)  # never an instant burst on boot


@pytest.mark.parametrize("spec", ["no-existe", "country=XX,bogus", "foo=bar"])
def test_a_typo_in_the_selector_stops_the_start_instead_of_monitoring_nothing(db, spec):
    with pytest.raises(sources.SourceError):
        sources.import_enabled(db, {"RADAR_ENABLED_SOURCES": spec})
    assert catalogs(db) == []


def test_bad_numbers_fall_back_to_the_defaults(db):
    sources.import_enabled(db, PAIR | {"RADAR_ITEMS_PER_CATEGORY": "-3", "RADAR_STORE_MAX_ITEMS": "abc"})
    with db.connect() as con:
        assert {r[0] for r in con.execute("SELECT max_items FROM catalogs")} == {10}


def test_the_cloud_process_imports_when_it_starts_and_a_plain_api_process_does_not(tmp_path):
    started = []
    stub = SimpleNamespace(gate=object(), start=lambda: started.append("start"), stop=lambda: started.append("stop"))
    env = PAIR | {"RADAR_ENABLE_DEMO": "0"}
    with TestClient(create_app(tmp_path / "a.db", env=env, registry=registry_with(), scheduler=stub, start_scheduler=True)) as c:
        assert [r["source_id"] for r in catalogs(c.app.state.db)] == ["falabella-pe", "wong-pe"]
        assert started == ["start"]
    assert started == ["start", "stop"]
    with TestClient(create_app(tmp_path / "b.db", env=env, registry=registry_with(), scheduler=stub, start_scheduler=False)) as c:
        assert catalogs(c.app.state.db) == []  # the UI-only process never creates jobs by itself


def test_the_headless_runner_imports_too(db, monkeypatch):
    monkeypatch.setenv("RADAR_ENABLED_SOURCES", "wong-pe")
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        status = cli.run_foreground(db, registry_with(), seconds=0.05)
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
    assert [r["source_id"] for r in catalogs(db)] == ["wong-pe"]
    assert isinstance(status, dict)
