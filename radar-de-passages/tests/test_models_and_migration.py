"""Criteria per kind, catalogue rules, and the v1 -> v2 database migration."""

import json
import sqlite3
import time
import pytest
from pydantic import ValidationError
from radar.database import Database, SCHEMA_VERSION
from radar.models import BusCriteria, Criteria, ProductCriteria, WatchInput, normalize_url
from radar.service import add_watch, get_watch, refresh
from conftest import bus_watch, flight_watch, product_watch

# The exact schema the original (v1, flight-only) version created.
V1_SCHEMA = """
CREATE TABLE watches (
 id TEXT PRIMARY KEY, criteria_json TEXT NOT NULL UNIQUE,
 target_cents INTEGER NOT NULL CHECK(target_cents>0), enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 channel TEXT NOT NULL CHECK(channel IN ('simulation','telegram')), created_at REAL NOT NULL
);
CREATE TABLE queries (
 id TEXT PRIMARY KEY, watch_id TEXT NOT NULL REFERENCES watches(id),
 started_at REAL NOT NULL, finished_at REAL,
 status TEXT NOT NULL CHECK(status IN ('running','success','error')), error TEXT
);
CREATE TABLE observations (
 id TEXT PRIMARY KEY, watch_id TEXT NOT NULL REFERENCES watches(id),
 provider TEXT NOT NULL, provider_observation_id TEXT NOT NULL,
 observed_at REAL NOT NULL, expires_at REAL NOT NULL, currency TEXT NOT NULL,
 total_cents INTEGER NOT NULL CHECK(total_cents>0), criteria_json TEXT NOT NULL,
 conditions_json TEXT NOT NULL, comparison_key TEXT, source TEXT NOT NULL,
 synthetic INTEGER NOT NULL CHECK(synthetic IN (0,1)),
 UNIQUE(watch_id,provider,provider_observation_id)
);
CREATE TABLE query_observations (
 query_id TEXT NOT NULL REFERENCES queries(id), observation_id TEXT NOT NULL REFERENCES observations(id),
 PRIMARY KEY(query_id,observation_id)
);
CREATE TABLE alerts (
 id TEXT PRIMARY KEY, watch_id TEXT NOT NULL REFERENCES watches(id),
 observation_id TEXT NOT NULL REFERENCES observations(id), created_at REAL NOT NULL,
 channel TEXT NOT NULL CHECK(channel IN ('simulation','telegram')),
 status TEXT NOT NULL CHECK(status IN ('simulated','pending','sending','sent','uncertain','failed')),
 message TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 UNIQUE(watch_id,observation_id)
);
PRAGMA user_version=1;
"""
V1_CRITERIA = {"origin": "LIM", "destination": "MAD", "departure": "2099-01-01", "return_date": None,
               "adults": 1, "children": 0, "infants": 0, "currency": "USD", "cabin": "ECONOMY"}


def v1_database(path):
    con = sqlite3.connect(path)
    con.executescript(V1_SCHEMA)
    con.execute("INSERT INTO watches VALUES ('w1',?,35000,1,'simulation',1.0)", (json.dumps(V1_CRITERIA, sort_keys=True, separators=(",", ":")),))
    con.execute("INSERT INTO queries VALUES ('q1','w1',1.0,2.0,'success',NULL)")
    con.execute("INSERT INTO observations VALUES ('o1','w1','yanz-synthetic-v1','p1',1.0,9e9,'USD',31000,'{}','{}','k','src',1)")
    con.execute("INSERT INTO query_observations VALUES ('q1','o1')")
    con.execute("INSERT INTO alerts(id,watch_id,observation_id,created_at,channel,status,message) VALUES ('a1','w1','o1',1.0,'simulation','simulated','m')")
    con.commit()
    con.close()


def test_v1_database_is_migrated_without_losing_history(tmp_path):
    path = tmp_path / "old.db"
    v1_database(path)
    db = Database(path)
    with db.connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for table, expected in (("queries", 1), ("observations", 1), ("query_observations", 1), ("alerts", 1)):
            assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == expected
        watch = con.execute("SELECT * FROM watches").fetchone()
        assert (watch["kind"], watch["provider"], watch["category"], watch["subcategory"]) == ("flight", "demo", "viajes", "aereo")
        assert watch["target_cents"] == 35000
    criteria = json.loads(watch["criteria_json"])
    assert criteria["kind"] == "flight" and criteria["max_connections"] == 1 and criteria["airlines"] == []
    # Canonical form: a new identical watch is recognised as a duplicate of the migrated one.
    same = WatchInput(criteria=Criteria(origin="LIM", destination="MAD", departure="2099-01-01"), target_cents=1)
    assert same.criteria.canonical() == watch["criteria_json"]
    with pytest.raises(ValueError, match="Ya existe"):
        add_watch(db, same)


def test_migrated_database_keeps_working_and_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    v1_database(path)
    Database(path)
    db = Database(path)  # opening again must not migrate twice
    key = add_watch(db, flight_watch())
    assert refresh(db, key, now=time.time())["status"] == "success"
    assert get_watch(db, "w1")["kind"] == "flight"
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 2


def test_database_from_a_newer_version_is_refused(tmp_path):
    path = tmp_path / "future.db"
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version=99")
    con.close()
    with pytest.raises(RuntimeError, match="más nueva"):
        Database(path)


def test_same_criteria_with_different_providers_are_distinct_watches(db):
    add_watch(db, product_watch(1, provider="demo"))
    add_watch(db, product_watch(1, provider="store"))
    with pytest.raises(ValueError):
        add_watch(db, product_watch(1, provider="store"))


def test_watch_limit_is_configurable(db, monkeypatch):
    monkeypatch.setenv("RADAR_MAX_WATCHES", "2")
    add_watch(db, product_watch(1))
    add_watch(db, product_watch(2))
    with pytest.raises(ValueError, match="Máximo 2"):
        add_watch(db, product_watch(3))


# --- criteria ------------------------------------------------------------------------------
def test_all_three_kinds_serialise_canonically_and_roundtrip():
    for value in (flight_watch(), bus_watch(), product_watch(1)):
        data = json.loads(value.model_dump_json())
        assert WatchInput.model_validate(data).criteria.canonical() == value.criteria.canonical()
        assert data["criteria"]["kind"] in ("flight", "bus", "product")


def test_kind_is_required_to_pick_the_right_criteria():
    with pytest.raises(ValidationError):
        WatchInput.model_validate({"criteria": {"origin": "LIM", "destination": "MAD", "departure": "2099-01-01"}, "target_cents": 1})


def test_flight_rules_are_unchanged_and_airlines_normalised():
    with pytest.raises(ValidationError):
        Criteria(origin="LIM", destination="LIM", departure="2099-01-01")
    with pytest.raises(ValidationError):
        Criteria(origin="LIM", destination="MAD", departure="2099-01-01", adults=1, infants=2)
    a = Criteria(origin="LIM", destination="MAD", departure="2099-01-01", airlines=["LA", "IB", "LA"])
    assert a.airlines == ["IB", "LA"]
    with pytest.raises(ValidationError):
        Criteria(origin="LIM", destination="MAD", departure="2099-01-01", airlines=["lan"])


def test_bus_places_are_cleaned_and_must_differ():
    b = BusCriteria(origin="  Lima  ", destination="Arequipa", departure="2099-01-01", operators=["Cruz  del Sur", "Cruz del Sur"])
    assert b.origin == "Lima" and b.operators == ["Cruz del Sur"]
    with pytest.raises(ValidationError):
        BusCriteria(origin="Lima", destination="LIMA", departure="2099-01-01")
    with pytest.raises(ValidationError):
        BusCriteria(origin="Lima", destination="Cusco", departure="2099-01-05", return_date="2099-01-01")


@pytest.mark.parametrize(
    "url",
    ["http://shop.test/p", "https://user:pw@shop.test/p", "https://127.0.0.1/p", "https://localhost/p",
     "https://10.0.0.1/p", "https://intranet/p", "https://box.local/p", "ftp://shop.test/p", "javascript:alert(1)", "https://"],
)
def test_product_urls_that_could_be_used_for_ssrf_are_rejected_at_the_edge(url):
    with pytest.raises((ValidationError, ValueError)):
        ProductCriteria(url=url + "x" * 3 if url == "https://" else url)


def test_product_url_is_normalised():
    assert normalize_url("https://SHOP.test:443/p/1?a=1#frag") == "https://shop.test/p/1?a=1"
    assert ProductCriteria(url="https://Shop.TEST/p/1#x").url == "https://shop.test/p/1"


def test_categories_are_validated_per_kind():
    ok = product_watch(1)
    assert (ok.category, ok.subcategory) == ("tecnologia", "laptops")
    assert (flight_watch().category, flight_watch().subcategory) == ("viajes", "aereo")
    assert (bus_watch().category, bus_watch().subcategory) == ("viajes", "terrestre")
    for bad in ({"category": "armas"}, {"category": None}, {"category": "ropa", "subcategory": "gpu"}):
        with pytest.raises(ValidationError):
            WatchInput(criteria=ProductCriteria(url="https://shop.test/p/1"), target_cents=1, **bad)
    with pytest.raises(ValidationError):
        WatchInput(criteria=Criteria(origin="LIM", destination="MAD", departure="2099-01-01"), target_cents=1, category="ropa")


def test_every_requested_product_category_exists_with_pc_components():
    from radar.catalog import CATEGORIES

    assert {"alimentos", "ropa", "juguetes", "electrodomesticos", "tecnologia"} <= set(CATEGORIES)
    tech = set(CATEGORIES["tecnologia"]["subcategories"])
    assert {"laptops", "televisores", "consolas", "componentes_gpu", "componentes_cpu", "componentes_ram"} <= tech


# --- demo provider across kinds -------------------------------------------------------------
@pytest.mark.parametrize("make", [flight_watch, bus_watch, lambda: product_watch(1, provider="demo", target=210_000)])
def test_demo_pipeline_works_for_every_kind(db, make):
    now = time.time()
    key = add_watch(db, make())
    base = refresh(db, key, "baseline", now=now)
    drop = refresh(db, key, "drop", now=now + 6)
    assert base["status"] == drop["status"] == "success" and drop["synthetic"] is True
    stats = drop["observations"][0]["analytics"]
    assert stats["comparable_count"] == 1 and stats["change_percent"] < 0
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
        assert con.execute("SELECT synthetic FROM observations").fetchone()[0] == 1


def test_demo_product_out_of_stock_is_recorded_but_never_alerts(db):
    now = time.time()
    key = add_watch(db, product_watch(1, provider="demo", target=999_999))
    out = refresh(db, key, "out_of_stock", now=now)
    assert out["observations"][0]["alert_eligible"] is False
    assert "disponibilidad" in out["observations"][0]["reason"]
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
    # The same price with stock does alert.
    assert refresh(db, key, "drop", now=now + 6)["observations"][0]["alert_eligible"] is True


def test_demo_out_of_stock_is_not_offered_for_flights(db):
    key = add_watch(db, flight_watch())
    out = refresh(db, key, "out_of_stock", now=time.time())
    assert out["status"] == "error"


def test_travel_dates_in_the_past_are_refused_for_flights_and_buses_only(db):
    key = add_watch(db, bus_watch())
    out = refresh(db, key, "baseline", now=time.time() + 40 * 86400)
    assert out["status"] == "error" and out["error_kind"] == "past"
    shop = add_watch(db, product_watch(1, provider="demo"))
    assert refresh(db, shop, "baseline", now=time.time() + 400 * 86400)["status"] == "success"


def test_auto_scenario_wanders_but_is_stable_inside_a_15_minute_bucket(db):
    from radar.providers.demo import DemoProvider

    demo = DemoProvider()
    c = product_watch(1, provider="demo").criteria
    bucket = int(time.time() // 900) * 900
    one = demo.fetch(c, "auto", bucket + 10)[0]
    two = demo.fetch(c, "auto", bucket + 800)[0]
    assert one.total_cents == two.total_cents and one.provider_observation_id == two.provider_observation_id
    prices = {demo.fetch(c, "auto", bucket + 900 * i)[0].total_cents for i in range(40)}
    assert len(prices) > 10
    base = demo.fixture["product"]["price_cents"]["baseline"]
    assert all(0.69 * base <= p <= 1.11 * base for p in prices)
