"""API v3: sources, catalogs, incidents, cross-store comparison, network exits, smart alerts, resources."""

import httpx
import pytest
from radar import restrictions
from radar.egress import EgressManager
from radar.providers.registry import Registry
from radar.providers.storepage import StorePageProvider
from radar.service import add_watch, refresh
from conftest import FakeLive, product_watch, registry_with
from test_api_v2 import basic, body, client
from test_priority import EAN
from test_providers import PUBLIC

NOW = 1_800_000_000.0
CATALOG = {"url": "https://shop.test/laptops", "mode": "listing", "category": "tecnologia", "subcategory": "laptops", "currency": "PEN", "max_items": 5}


def idle_client():
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))


def with_store(**kw):
    return registry_with(StorePageProvider(["shop.test"], resolver=PUBLIC, client=idle_client(), **kw))


def with_exits(mapping="shop.test=proxy_a", enabled=True):
    env = {
        "RADAR_EGRESS_PROFILES": "proxy_a", "RADAR_EGRESS_PROXY_A_URL": "http://user:pw@a.proxy:8080",
        "RADAR_EGRESS_MAP": mapping, "RADAR_EGRESS_PROXY_A_ENABLED": "1" if enabled else "0",
    }
    manager = EgressManager.from_env(env, client_factory=lambda profile: idle_client())
    return registry_with(StorePageProvider(["shop.test"], resolver=PUBLIC, egress=manager))


# --- sources ------------------------------------------------------------------------------------------------
def test_sources_lists_the_shipped_catalogue_and_what_this_deployment_enabled(tmp_path):
    with client(tmp_path, {"RADAR_ENABLED_SOURCES": "falabella-pe,wong-pe", "RADAR_ENABLE_DEMO": "0"}) as c:
        rows = c.get("/api/sources").json()
    assert len(rows) >= 150
    assert {r["id"] for r in rows if r["enabled"]} == {"falabella-pe", "wong-pe"}
    travel = [r for r in rows if r["kind"] in ("bus", "airline", "agency")]
    assert travel and all(r["enabled"] is False and r["status"] == "sin_conector" for r in travel)
    assert all(set(r) == {"id", "name", "kind", "country", "domain", "status", "note", "enabled"} for r in rows)


# --- catalogs --------------------------------------------------------------------------------------------------
def test_a_catalog_can_be_created_listed_switched_and_is_never_duplicated(tmp_path):
    with client(tmp_path, registry=with_store()) as c:
        created = c.post("/api/catalogs", json=CATALOG)
        assert created.status_code == 201
        key = created.json()["id"]
        [row] = c.get("/api/catalogs").json()
        assert row["id"] == key and row["domain"] == "shop.test" and row["alert_mode"] == "smart" and row["watches"] == 0
        assert row["enabled"] == 1 and row["currency"] == "PEN" and row["mode"] == "listing"
        assert c.post("/api/catalogs", json=CATALOG).status_code == 409
        off = c.patch(f"/api/catalogs/{key}", json={"enabled": False})
        assert off.json() == {"id": key, "enabled": False} and c.get("/api/catalogs").json()[0]["enabled"] == 0
        assert c.patch("/api/catalogs/nope", json={"enabled": True}).status_code == 404
        assert c.patch(f"/api/catalogs/{key}", json={"enabled": "yes"}).status_code == 422


def test_re_enabling_a_catalog_clears_its_failure_state(tmp_path):
    with client(tmp_path, registry=with_store()) as c:
        key = c.post("/api/catalogs", json=CATALOG).json()["id"]
        with c.app.state.db.connect() as con:
            con.execute("UPDATE catalogs SET enabled=0,fail_count=5,last_error='estructura' WHERE id=?", (key,))
        assert c.patch(f"/api/catalogs/{key}", json={"enabled": True}).status_code == 200
        row = c.get("/api/catalogs").json()[0]
    assert row["enabled"] == 1 and row["fail_count"] == 0 and row["last_error"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://other.test/laptops"},  # not an allow-listed store
        {"url": "http://shop.test/laptops"},  # not https
        {"url": "https://127.0.0.1/laptops"},  # internal address
        {"category": "no-existe"},
        {"subcategory": "no-existe"},
        {"include_regex": "(unclosed"},
        {"include_regex": "a" * 201},
        {"max_items": 0},
        {"max_items": 501},
        {"currency": "JPY"},
        {"mode": "crawl"},
        {"alert_mode": "target"},  # a discovered product has no personal target
        {"alert_mode": "both"},
        {"channel": "sms"},
        {"unexpected": 1},
    ],
)
def test_invalid_catalogs_are_refused_with_422_not_500(tmp_path, change):
    with client(tmp_path, registry=with_store()) as c:
        assert c.post("/api/catalogs", json=CATALOG | change).status_code == 422
        assert c.get("/api/catalogs").json() == []


def test_catalogs_need_a_configured_store_reader(tmp_path):
    with client(tmp_path, registry=registry_with()) as c:
        assert c.post("/api/catalogs", json=CATALOG).status_code == 422


# --- smart watches --------------------------------------------------------------------------------------------------
def smart_body(n=1, **kw):
    data = body(product_watch(n)) | {"alert_mode": "smart"} | kw
    data.pop("target_cents", None)
    return data


def test_a_smart_watch_needs_no_target_and_a_target_watch_still_does(tmp_path):
    with client(tmp_path, registry=registry_with(FakeLive())) as c:
        made = c.post("/api/watches", json=smart_body(1))
        assert made.status_code == 201 and made.json()["alert_mode"] == "smart"
        missing = smart_body(2, alert_mode="target")
        assert c.post("/api/watches", json=missing).status_code == 422
        both = c.post("/api/watches", json=body(product_watch(3)) | {"alert_mode": "both"})
        assert both.status_code == 201 and both.json()["alert_mode"] == "both" and both.json()["target_cents"] == 50_000
        assert c.post("/api/watches", json=smart_body(4, alert_mode="whenever")).status_code == 422


def test_patching_keeps_the_stored_target_and_refuses_a_target_mode_that_has_none(tmp_path):
    with client(tmp_path, registry=registry_with(FakeLive())) as c:
        smart = c.post("/api/watches", json=smart_body(1)).json()["id"]
        plain = c.post("/api/watches", json=body(product_watch(2))).json()["id"]
        kept = c.patch(f"/api/watches/{plain}", json={"enabled": False, "channel": "simulation"})
        assert kept.status_code == 200 and kept.json()["target_cents"] == 50_000 and kept.json()["enabled"] == 0
        assert c.patch(f"/api/watches/{plain}", json={"enabled": True, "alert_mode": "both"}).json()["alert_mode"] == "both"
        no_target = c.patch(f"/api/watches/{smart}", json={"enabled": True, "alert_mode": "both"})
        assert no_target.status_code == 422 and "objetivo" in no_target.json()["detail"]
        with_target = c.patch(f"/api/watches/{smart}", json={"enabled": True, "alert_mode": "both", "target_cents": 70_000})
        assert with_target.json()["alert_mode"] == "both" and with_target.json()["target_cents"] == 70_000
        assert c.patch(f"/api/watches/{smart}", json={"enabled": True, "target_cents": 0}).status_code == 422
        assert c.patch("/api/watches/nope", json={"enabled": True}).status_code == 404


def test_alerts_carry_their_priority_and_rank(tmp_path):
    fake = FakeLive(lambda c, now: 40_000)
    with client(tmp_path, registry=registry_with(fake)) as c:
        key = c.post("/api/watches", json=body(product_watch(1))).json()["id"]  # target 50 000, price 40 000
        refresh(c.app.state.db, key, provider=fake, now=NOW)
        [alert] = c.get("/api/alerts").json()
    assert alert["priority"] == "objetivo" and alert["priority_rank"] == 1


# --- incidents ----------------------------------------------------------------------------------------------------------
def test_incidents_are_listed_newest_first_filtered_and_bounded(tmp_path):
    with client(tmp_path) as c:
        db = c.app.state.db
        restrictions.record_incident(db, NOW, domain="a.test", kind="captcha", detail="verificación", status=403, url="https://a.test/p", egress="direct")
        restrictions.record_incident(db, NOW + 60, domain="b.test", kind="timeout", detail="lento")
        restrictions.record_incident(db, NOW + 120, domain="a.test", kind="denied", detail="no", url="https://u:p@a.test/x")
        rows = c.get("/api/incidents").json()
        assert [r["kind"] for r in rows] == ["denied", "timeout", "captcha"]
        assert [r["kind"] for r in c.get("/api/incidents", params={"domain": "a.test"}).json()] == ["denied", "captcha"]
        assert len(c.get("/api/incidents", params={"limit": 1}).json()) == 1
        assert c.get("/api/incidents", params={"limit": 0}).status_code == 422
        assert c.get("/api/incidents", params={"limit": 201}).status_code == 422
        assert all("u:p@" not in (r["url"] or "") for r in rows)  # credentials never reach the journal
        assert {r["severity"] for r in rows} == {"high", "low"}


# --- cross-store comparison ----------------------------------------------------------------------------------------------
def test_compare_lines_up_the_same_gtin_across_stores(tmp_path):
    with client(tmp_path, registry=registry_with()) as c:
        db = c.app.state.db
        for i, (domain, cents) in enumerate((("a.test", 100_000), ("b.test", 80_000))):
            fake = FakeLive(lambda criteria, now, cents=cents: cents, domain=domain, ids={"gtin": EAN, "title": "Laptop X"})
            key = add_watch(db, product_watch(1).model_copy(update={"criteria": product_watch(1).criteria.model_copy(update={"url": f"https://{domain}/p/1"}), "alert_mode": "smart"}))
            refresh(db, key, provider=fake, now=NOW + i)
        [group] = c.get("/api/compare").json()
        assert group["cheapest"] == "b.test" and group["spread_percent"] == 25.0 and group["saving_cents"] == 20_000
        assert [s["domain"] for s in group["stores"]] == ["b.test", "a.test"] and group["title"] == "Laptop X"
        assert c.get("/api/compare", params={"limit": 0}).status_code == 422


# --- network exits -----------------------------------------------------------------------------------------------------------
def test_an_exit_can_be_switched_off_and_on_and_its_credentials_never_leave_the_server(tmp_path):
    with client(tmp_path, registry=with_exits()) as c:
        status = c.get("/api/status").json()
        names = {p["name"]: p for p in status["egress"]}
        assert set(names) == {"direct", "proxy_a"} and names["proxy_a"]["enabled"] is True
        off = c.post("/api/egress/proxy_a", json={"enabled": False}).json()
        assert {p["name"]: p["enabled"] for p in off}["proxy_a"] is False
        assert c.post("/api/egress/proxy_a", json={"enabled": True}).status_code == 200
        text = c.get("/api/status").text + c.post("/api/egress/proxy_a", json={"enabled": True}).text
        assert "pw" not in text and "user:" not in text and "a.proxy" in text  # host is shown, credentials are not
        assert c.post("/api/egress/ghost", json={"enabled": False}).status_code == 404
        assert c.post("/api/egress/proxy_a", json={"enabled": 1}).status_code == 422  # strict booleans
        assert c.post("/api/egress/proxy_a", json={"enabled": True, "extra": 1}).status_code == 422


def test_without_a_store_reader_there_are_no_exits_to_switch(tmp_path):
    with client(tmp_path, registry=registry_with()) as c:
        assert c.get("/api/status").json()["egress"] == []
        assert c.post("/api/egress/direct", json={"enabled": False}).status_code == 404


# --- resources ---------------------------------------------------------------------------------------------------------------------
def test_status_reports_cpu_memory_storage_and_bandwidth(tmp_path):
    with client(tmp_path, registry=registry_with(FakeLive())) as c:
        c.post("/api/watches", json=body(product_watch(1)))
        resources = c.get("/api/status").json()["resources"]
    assert resources["cpu_seconds"] >= 0 and resources["rss_mb"] > 0 and resources["database_mb"] > 0 and resources["disk_free_gb"] > 0
    assert resources["rows"]["watches"] == 1 and resources["requests_total"] == 0 and resources["incidents_24h"] == 0
    assert "cabeceras" in resources["note"]


# --- authentication covers every new endpoint ----------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/sources"), ("get", "/api/catalogs"), ("post", "/api/catalogs"), ("patch", "/api/catalogs/x"),
        ("get", "/api/incidents"), ("get", "/api/compare"), ("post", "/api/egress/direct"), ("post", "/api/domains/shop.test/resume"),
    ],
)
def test_every_new_endpoint_needs_credentials_when_a_password_is_set(tmp_path, method, path):
    with client(tmp_path, {"RADAR_ADMIN_PASSWORD": "correct-horse-battery"}, registry=with_store()) as c:
        assert getattr(c, method)(path).status_code == 401
        allowed = getattr(c, method)(path, headers=basic(), **({"json": {}} if method != "get" else {}))
        assert allowed.status_code != 401
