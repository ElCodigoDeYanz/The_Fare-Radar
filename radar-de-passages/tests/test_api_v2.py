"""HTTP API: authentication, catalogue, kinds, providers, politeness on manual refresh."""

import base64
import time
import pytest
from fastapi.testclient import TestClient
from radar.api import create_app
from radar.providers.base import BlockedError
from conftest import FakeLive, bus_watch, flight_watch, product_watch, registry_with


def basic(user="admin", password="correct-horse-battery"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def client(tmp_path, env=None, **kw):
    return TestClient(create_app(tmp_path / "api.db", env=env or {}, **kw))


def body(value):
    return value.model_dump()


# --- authentication ---------------------------------------------------------------------------
def test_production_refuses_to_start_without_a_strong_password(tmp_path):
    with pytest.raises(RuntimeError, match="RADAR_ADMIN_PASSWORD"):
        create_app(tmp_path / "a.db", env={"RADAR_ENV": "production"})
    with pytest.raises(RuntimeError):
        create_app(tmp_path / "a.db", env={"RADAR_ENV": "production", "RADAR_ADMIN_PASSWORD": "short"})
    create_app(tmp_path / "a.db", env={"RADAR_ENV": "production", "RADAR_ADMIN_PASSWORD": "x" * 12})


def test_everything_except_health_needs_credentials_when_a_password_is_set(tmp_path):
    env = {"RADAR_ADMIN_PASSWORD": "correct-horse-battery"}
    with client(tmp_path, env) as c:
        assert c.get("/api/health").status_code == 200
        for path in ("/", "/api/watches", "/api/alerts", "/api/status", "/api/catalog", "/static/app.js"):
            r = c.get(path)
            assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"], path
        assert c.post("/api/watches", json=body(flight_watch())).status_code == 401
        assert c.post("/api/demo").status_code == 401
        assert c.get("/api/watches", headers=basic()).status_code == 200
        assert c.get("/", headers=basic()).status_code == 200
        for bad in (basic(password="wrong"), basic(user="root"), {"Authorization": "Basic !!!"}, {"Authorization": "Bearer x"}):
            assert c.get("/api/watches", headers=bad).status_code == 401


def test_without_a_password_local_development_stays_open(tmp_path):
    with client(tmp_path) as c:
        assert c.get("/api/watches").status_code == 200


def test_security_headers_and_hsts_in_production(tmp_path):
    env = {"RADAR_ENV": "production", "RADAR_ADMIN_PASSWORD": "correct-horse-battery"}
    with client(tmp_path, env) as c:
        r = c.get("/api/watches", headers=basic())
        assert r.headers["strict-transport-security"].startswith("max-age=")
        assert "script-src 'self'" in r.headers["content-security-policy"] and r.headers["cache-control"] == "no-store"


# --- catalogue and health ---------------------------------------------------------------------
def test_catalog_lists_kinds_categories_and_the_providers_of_this_deployment(tmp_path):
    with client(tmp_path) as c:
        data = c.get("/api/catalog").json()
    assert set(data["kinds"]) == {"flight", "bus", "product"}
    assert "tecnologia" in data["categories"] and "laptops" in data["categories"]["tecnologia"]["subcategories"]
    assert [p["name"] for p in data["providers"]] == ["demo"]


def test_health_reports_what_is_live(tmp_path):
    with client(tmp_path) as c:
        h = c.get("/api/health").json()
    assert h["live_flights"] is False and h["live_products"] is False and h["scheduler"] is False
    registry = registry_with(FakeLive())
    with client(tmp_path / "x", registry=registry) as c:
        h = c.get("/api/health").json()
    assert h["live_products"] is True and h["provider"] == "live + synthetic"


# --- creating watches -------------------------------------------------------------------------
def test_all_three_kinds_can_be_created_and_listed_with_filters(tmp_path):
    with client(tmp_path) as c:
        for value in (flight_watch(), bus_watch(), product_watch(1, provider="demo")):
            assert c.post("/api/watches", json=body(value)).status_code == 201
        assert len(c.get("/api/watches").json()) == 3
        assert [w["kind"] for w in c.get("/api/watches", params={"kind": "bus"}).json()] == ["bus"]
        tech = c.get("/api/watches", params={"category": "tecnologia"}).json()
        assert len(tech) == 1 and tech[0]["subcategory"] == "laptops" and tech[0]["criteria"]["kind"] == "product"
        assert len(c.get("/api/watches", params={"category": "viajes"}).json()) == 2
        assert c.get("/api/watches", params={"kind": "nope"}).status_code == 422


def test_unavailable_provider_or_wrong_kind_is_refused(tmp_path):
    with client(tmp_path) as c:
        unconfigured = flight_watch().model_copy(update={"provider": "aerolinea-x"})
        assert c.post("/api/watches", json=body(unconfigured)).status_code == 422
        store = product_watch(1, provider="store")
        assert c.post("/api/watches", json=body(store)).status_code == 422


def test_store_watches_are_limited_to_allow_listed_domains(tmp_path):
    from radar.providers.registry import build_registry

    registry = build_registry({"RADAR_ALLOWED_STORE_DOMAINS": "shop.test"})
    with client(tmp_path, registry=registry) as c:
        assert c.post("/api/watches", json=body(product_watch(1, provider="store"))).status_code == 201
        other = product_watch(1, provider="store")
        other.criteria.url = "https://elsewhere.test/p/1"
        assert c.post("/api/watches", json=body(other)).status_code == 422
        # A store provider cannot be used for a flight.
        assert c.post("/api/watches", json=body(flight_watch().model_copy(update={"provider": "store"}))).status_code == 422


def test_invalid_payloads_are_422_not_500(tmp_path):
    good = body(product_watch(1, provider="demo"))
    with client(tmp_path) as c:
        for mutation in (
            {"category": "armas"},
            {"criteria": good["criteria"] | {"url": "https://127.0.0.1/x"}},
            {"criteria": good["criteria"] | {"url": "http://shop.test/x"}},
            {"criteria": good["criteria"] | {"kind": "train"}},
            {"target_cents": 0},
            {"provider": "Bad Name!"},
            {"unexpected": 1},
        ):
            assert c.post("/api/watches", json=good | mutation).status_code == 422, mutation
        assert c.post("/api/watches", json=good).status_code == 201
        assert c.post("/api/watches", json=good).status_code == 409


def test_demo_endpoint_creates_each_kind_and_is_repeatable(tmp_path):
    with client(tmp_path) as c:
        for kind in ("flight", "bus", "product"):
            first = c.post("/api/demo", params={"kind": kind})
            assert first.status_code == 201 and first.json()["query"]["status"] == "success"
            assert c.post("/api/demo", params={"kind": kind}).status_code == 201  # no duplicate error
        assert len(c.get("/api/watches").json()) == 3
        assert c.post("/api/demo", params={"kind": "train"}).status_code == 422
    from radar.providers.registry import Registry

    with client(tmp_path / "no-demo", registry=Registry([])) as c:
        assert c.post("/api/demo").status_code == 404


# --- refreshing, politeness ---------------------------------------------------------------------
def test_manual_refresh_of_a_live_provider_respects_the_domain_gate(tmp_path):
    fake = FakeLive()
    with client(tmp_path, registry=registry_with(fake)) as c:
        a = c.post("/api/watches", json=body(product_watch(1))).json()["id"]
        b = c.post("/api/watches", json=body(product_watch(2))).json()["id"]
        assert c.post(f"/api/watches/{a}/refresh", json={}).json()["status"] == "success"
        denied = c.post(f"/api/watches/{b}/refresh", json={})  # same domain, immediately after
        assert denied.status_code == 429 and int(denied.headers["retry-after"]) >= 1
        assert len(fake.calls) == 1


def test_a_block_during_manual_refresh_pauses_the_domain_for_everyone(tmp_path):
    def blocked(criteria, now):
        raise BlockedError("403")

    fake = FakeLive(blocked)
    with client(tmp_path, registry=registry_with(fake)) as c:
        a = c.post("/api/watches", json=body(product_watch(1))).json()["id"]
        out = c.post(f"/api/watches/{a}/refresh", json={}).json()
        assert out["status"] == "error" and out["error_kind"] == "blocked"
        domain = c.get("/api/status").json()["domains"][0]
        assert domain["domain"] == "shop.test" and domain["status"] == "cooling"
        assert c.post("/api/domains/shop.test/resume").json()["status"] == "ok"
        assert c.get("/api/status").json()["domains"][0]["status"] == "ok"
        assert c.post("/api/domains/unknown.test/resume").status_code == 404


def test_demo_refresh_is_not_gated_and_scenarios_validate(tmp_path):
    with client(tmp_path) as c:
        key = c.post("/api/watches", json=body(flight_watch())).json()["id"]
        assert c.post(f"/api/watches/{key}/refresh", json={"scenario": "baseline"}).json()["status"] == "success"
        assert c.post(f"/api/watches/{key}/refresh", json={"scenario": "nonsense"}).status_code == 422
        time.sleep(5.1)  # the synthetic provider's own 5 s limit
        blocked = c.post(f"/api/watches/{key}/refresh", json={"scenario": "blocked"}).json()
        assert blocked["error_kind"] == "blocked"
        assert c.get("/api/status").json()["domains"] == []  # synthetic blocks never pause a real domain
        assert c.post("/api/watches/missing/refresh", json={}).status_code == 404


def test_status_exposes_scheduler_budget_and_domains(tmp_path):
    with client(tmp_path) as c:
        status = c.get("/api/status").json()
    assert status["scheduler"]["interval_s"] == [300.0, 900.0]
    assert status["scheduler"]["running"] is False
    assert status["usage_this_month"] == {} and status["domains"] == []


def test_interval_and_gap_come_from_the_environment(tmp_path):
    env = {"RADAR_MIN_INTERVAL_S": "90", "RADAR_MAX_INTERVAL_S": "300", "RADAR_DOMAIN_GAP_MIN_S": "20", "RADAR_DOMAIN_GAP_MAX_S": "40"}
    with client(tmp_path, env) as c:
        s = c.get("/api/status").json()["scheduler"]
    assert s["interval_s"] == [90.0, 300.0] and s["domain_gap_s"] == [20.0, 40.0]


def test_scheduler_runs_inside_the_app_and_stops_with_it(tmp_path):
    env = {"RADAR_SCHEDULER": "1", "RADAR_MIN_INTERVAL_S": "5", "RADAR_MAX_INTERVAL_S": "6"}
    app = create_app(tmp_path / "s.db", env=env)
    with TestClient(app) as c:
        c.post("/api/watches", json=body(product_watch(1, provider="demo", target=210_000)))
        deadline = time.time() + 20
        while time.time() < deadline:
            watch = c.get("/api/watches").json()[0]
            if watch["last_query"] and watch["last_query"]["status"] == "success":
                break
            time.sleep(0.5)
        else:
            pytest.fail("the background scheduler never ran the watch")
        assert c.get("/api/status").json()["scheduler"]["running"] is True
        assert watch["next_run_at"] is not None
    assert app.state.scheduler._thread.is_alive() is False
    with app.state.db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM scheduler_lease").fetchone()[0] == 0  # released on shutdown


def test_history_is_paginated_in_sql_and_bounded(tmp_path):
    with client(tmp_path) as c:
        key = c.post("/api/watches", json=body(product_watch(1, provider="demo"))).json()["id"]
        db = c.app.state.db
        from radar.service import refresh

        now = time.time()
        for i in range(30):
            refresh(db, key, "auto", now=now + i * 900, registry=c.app.state.registry)
        page = c.get(f"/api/watches/{key}/history", params={"limit": 10, "offset": 10}).json()
        assert page["query_count"] == 30 and len(page["queries"]) == 10 and len(page["observations"]) == 10
        assert page["observation_count"] >= 10
        assert c.get(f"/api/watches/{key}/history", params={"limit": 101}).status_code == 422


def test_backup_is_a_consistent_openable_copy_and_never_overwrites(tmp_path):
    import sqlite3
    from radar.main import backup
    from radar.service import add_watch
    from radar.database import Database

    db = Database(tmp_path / "live.db")
    add_watch(db, product_watch(1, provider="demo"))
    target = tmp_path / "bk" / "copy.sqlite3"
    backup(db, target)
    con = sqlite3.connect(target)
    assert con.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 1
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()
    Database(target)  # the copy is a complete, current-version database
    with pytest.raises(SystemExit):
        backup(db, target)
