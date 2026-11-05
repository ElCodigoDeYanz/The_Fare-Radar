"""Schema v3 is additive: a v2 database keeps every row, gains the new columns and tables, and ends up
byte-for-byte the same shape as a fresh database. A v1 database goes through both steps."""

import json
import sqlite3
import time
import pytest
from radar import database
from radar.database import Database, SCHEMA, SCHEMA_VERSION
from radar.service import add_watch, get_watch, refresh
from conftest import FakeLive, product_watch

T = 1_800_000_000.0
CRITERIA = json.dumps({"currency": "PEN", "kind": "product", "url": "https://shop.test/p/1"}, sort_keys=True, separators=(",", ":"))


def build_v2(path):
    """A database exactly as the previous release left it (schema v2) with a little history in it."""
    con = sqlite3.connect(path, isolation_level=None)
    for statement in database._split(SCHEMA):
        con.execute(statement)
    con.execute("PRAGMA user_version=2")
    con.execute(
        "INSERT INTO watches(id,kind,provider,category,subcategory,label,criteria_json,target_cents,enabled,channel,created_at) "
        "VALUES ('w1','product','store','tecnologia','laptops','Laptop X',?,50000,1,'telegram',?)",
        (CRITERIA, T),
    )
    con.execute("INSERT INTO queries(id,watch_id,started_at,finished_at,status,error,error_kind) VALUES ('q1','w1',?,?,'success',NULL,NULL)", (T, T + 1))
    con.execute(
        "INSERT INTO observations(id,watch_id,provider,provider_observation_id,observed_at,expires_at,currency,total_cents,criteria_json,"
        "conditions_json,comparison_key,source,synthetic) VALUES ('o1','w1','store','p1',?,?,'PEN',45000,?, '{\"availability\":\"in_stock\"}','k','Página de tienda',0)",
        (T, T + 1200, CRITERIA),
    )
    con.execute("INSERT INTO query_observations(query_id,observation_id) VALUES ('q1','o1')")
    con.execute("INSERT INTO alerts(id,watch_id,observation_id,created_at,channel,status,message) VALUES ('a1','w1','o1',?,'telegram','sent','viejo mensaje')", (T,))
    con.execute("INSERT INTO watch_schedule(watch_id,next_run_at,fail_count,last_status) VALUES ('w1',?,2,'blocked')", (T + 600,))
    con.execute("INSERT INTO domain_state(domain,block_count,status,blocked_until,last_error) VALUES ('shop.test',2,'cooling',?,'403')", (T + 900,))
    con.execute("INSERT INTO provider_usage(provider,month,calls) VALUES ('store','2026-11',7)")
    con.close()


def shape(path):
    """Tables with their columns (name, type, notnull, default, pk) and the index names."""
    con = sqlite3.connect(path)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        columns = {t: sorted(tuple(r[1:]) for r in con.execute(f"PRAGMA table_info({t})")) for t in tables}
        indexes = sorted(r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))
        return columns, indexes, con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def test_a_v2_database_is_upgraded_without_losing_or_rewriting_anything(tmp_path):
    path = tmp_path / "old.db"
    build_v2(path)
    db = Database(path)
    with db.connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
        w = dict(con.execute("SELECT * FROM watches").fetchone())
        assert (w["id"], w["target_cents"], w["channel"], w["label"], w["criteria_json"]) == ("w1", 50000, "telegram", "Laptop X", CRITERIA)
        assert (w["alert_mode"], w["catalog_id"], w["title"], w["match_key"]) == ("target", None, None, None)  # old watches keep their behaviour
        a = dict(con.execute("SELECT * FROM alerts").fetchone())
        assert (a["message"], a["status"], a["priority"], a["priority_rank"]) == ("viejo mensaje", "sent", None, 0)
        d = dict(con.execute("SELECT * FROM domain_state").fetchone())
        assert (d["block_count"], d["status"], d["blocked_until"], d["last_error"]) == (2, "cooling", T + 900, "403")  # a block in progress survives
        assert (d["requests"], d["bytes_in"], d["delay_s"], d["fail_streak"], d["latency_s"], d["egress"]) == (0, 0, 0, 0, None, None)
        q = dict(con.execute("SELECT * FROM queries").fetchone())
        assert (q["status"], q["egress"], q["http_status"]) == ("success", None, None)
        assert con.execute("SELECT total_cents FROM observations").fetchone()[0] == 45000
        assert con.execute("SELECT calls FROM provider_usage").fetchone()[0] == 7
        assert con.execute("SELECT fail_count,last_status FROM watch_schedule").fetchone()[:] == (2, "blocked")
        assert con.execute("SELECT COUNT(*) FROM catalogs").fetchone()[0] == 0 and con.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_the_upgraded_schema_is_identical_to_a_fresh_one(tmp_path):
    old = tmp_path / "old.db"
    build_v2(old)
    Database(old)
    Database(tmp_path / "fresh.db")
    assert shape(old) == shape(tmp_path / "fresh.db")


def test_a_v2_watch_keeps_working_after_the_upgrade(tmp_path):
    path = tmp_path / "old.db"
    build_v2(path)
    db = Database(path)
    watch = get_watch(db, "w1")
    assert watch["alert_mode"] == "target" and watch["criteria"]["url"] == "https://shop.test/p/1"
    fake = FakeLive(lambda c, now: 40_000)
    out = refresh(db, "w1", provider=fake, now=T + 3600)
    assert out["status"] == "success"
    with db.connect() as con:
        new = dict(con.execute("SELECT * FROM alerts WHERE message<>'viejo mensaje'").fetchone())
    assert new["priority"] == "objetivo" and new["priority_rank"] == 1  # 40 000 <= the old 50 000 target
    assert add_watch(db, product_watch(2)) != "w1"


def test_upgrading_twice_or_opening_a_current_database_changes_nothing(tmp_path):
    path = tmp_path / "old.db"
    build_v2(path)
    Database(path)
    first = shape(path)
    Database(path)
    Database(path)
    assert shape(path) == first
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 1


def test_a_failed_upgrade_rolls_back_completely(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    build_v2(path)
    before = shape(path)
    monkeypatch.setattr(database, "MIGRATE_2_TO_3", database.MIGRATE_2_TO_3 + ";\nALTER TABLE no_such_table ADD COLUMN x TEXT")
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    assert shape(path) == before  # still a clean v2: nothing half-applied, so the next start can retry
    assert shape(path)[2] == 2


def test_a_database_from_a_newer_release_is_refused_not_downgraded(tmp_path):
    path = tmp_path / "future.db"
    Database(path)
    with sqlite3.connect(path) as con:
        con.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="más nueva"):
        Database(path)


def test_the_new_constraints_hold(tmp_path):
    db = Database(tmp_path / "new.db")
    now = time.time()
    with db.connect() as con:
        with pytest.raises(sqlite3.IntegrityError):  # severity is one of low/medium/high
            con.execute("INSERT INTO incidents(id,at,domain,kind,severity,action,detail) VALUES ('x',?,'a.test','k','extreme','a','d')", (now,))
    with db.connect() as con:
        with pytest.raises(sqlite3.IntegrityError):  # a catalog without a positive cap
            con.execute(
                "INSERT INTO catalogs(id,source_id,domain,mode,url,currency,max_items,created_at) VALUES ('c','s','a.test','listing','https://a.test/','PEN',0,?)", (now,)
            )
    with db.connect() as con:
        con.execute("INSERT INTO catalogs(id,source_id,domain,mode,url,currency,max_items,created_at) VALUES ('c1','s','a.test','listing','https://a.test/','PEN',5,?)", (now,))
        with pytest.raises(sqlite3.IntegrityError):  # one catalog per source+mode+url
            con.execute("INSERT INTO catalogs(id,source_id,domain,mode,url,currency,max_items,created_at) VALUES ('c2','s','a.test','listing','https://a.test/','PEN',5,?)", (now,))
