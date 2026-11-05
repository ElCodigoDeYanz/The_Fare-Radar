from dataclasses import replace
from datetime import date, timedelta
import json
import time
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
import httpx
import pytest
from radar.api import create_app
from radar.database import Database
from radar.models import Criteria, WatchInput
from radar.service import add_watch, refresh, Busy
from radar.scraper import DemoProvider
from radar.main import deliver_telegram


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "radar.db")


def value(**kw):
    data = {
        "origin": "LIM",
        "destination": "MAD",
        "departure": (date.today() + timedelta(days=45)).isoformat(),
    }
    data.update(kw)
    return WatchInput(criteria=Criteria(**data), target_cents=35000)


def test_history_conditions_origin_and_exact_comparison(db):
    now = time.time()
    key = add_watch(db, value())
    first = refresh(db, key, now=now)
    drop = refresh(db, key, "drop", now=now + 6)
    stats = drop["observations"][0]["analytics"]
    assert first["status"] == drop["status"] == "success"
    assert stats["change_percent"] == -26.19 and stats["comparable_count"] == 1
    assert stats["minimum_cents"] == 42000
    with db.connect() as con:
        row = con.execute(
            "SELECT * FROM observations ORDER BY total_cents LIMIT 1"
        ).fetchone()
        assert (
            row["source"] == "Synthetic fixture: demo/offers.json"
            and row["synthetic"] == 1
        )
        assert json.loads(row["criteria_json"])["adults"] == 1
        assert row["observed_at"] == now + 6
        assert con.execute("SELECT COUNT(*) FROM queries").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1


def test_same_observation_no_duplicate_and_no_overwrite(db):
    now = int(time.time() / 900) * 900 + 100
    key = add_watch(db, value())
    refresh(db, key, "drop", now=now)
    refresh(db, key, "drop", now=now + 6)
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM queries").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM query_observations").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
        assert con.execute("SELECT observed_at FROM observations").fetchone()[0] == now


def test_failure_does_not_create_fresh_price(db):
    now = time.time()
    key = add_watch(db, value())
    refresh(db, key, now=now)
    result = refresh(db, key, "failure", now=now + 6)
    assert result["status"] == "error"
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        assert [
            r[0] for r in con.execute("SELECT status FROM queries ORDER BY started_at")
        ] == ["success", "error"]


def test_expired_not_alerted_and_incomplete_or_different_not_compared(db):
    now = time.time()
    key = add_watch(db, value())
    refresh(db, key, now=now)
    expired = refresh(db, key, "expired", now=now + 6)
    assert (
        expired["observations"][0]["expired"]
        and not expired["observations"][0]["alert_eligible"]
    )
    different = refresh(db, key, "different_conditions", now=now + 12)
    missing = refresh(db, key, "missing_conditions", now=now + 18)
    assert different["observations"][0]["analytics"]["comparable_count"] == 0
    assert different["observations"][0]["analytics"]["change_percent"] is None
    assert missing["observations"][0]["analytics"]["change_percent"] is None


def test_currency_and_passengers_mismatch_rejected_atomically(db):
    now = time.time()
    key = add_watch(db, value())

    class Mismatch:
        def fetch(self, criteria, scenario, now):
            offer = DemoProvider().fetch(criteria, "baseline", now)[0]
            return [
                offer,
                replace(
                    offer,
                    criteria=criteria.model_copy(update={"adults": 2}),
                    currency="PEN",
                ),
            ]

    assert refresh(db, key, provider=Mismatch(), now=now)["status"] == "error"
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_distinct_watches_never_merge(db):
    now = time.time()
    first = add_watch(db, value())
    second = add_watch(db, value(adults=2))
    third = add_watch(db, value(currency="PEN"))
    for key in (first, second, third):
        result = refresh(db, key, "drop", now=now)
        assert result["observations"][0]["analytics"]["comparable_count"] == 0
    with db.connect() as con:
        assert [
            r[0]
            for r in con.execute(
                "SELECT total_cents FROM observations WHERE watch_id=?", (second,)
            )
        ] == [62000]
        assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3


def test_rate_limit_and_atomic_parallel_claim(db):
    key = add_watch(db, value())
    now = time.time()

    def attempt(_):
        try:
            refresh(db, key, now=now)
            return True
        except Busy:
            return False

    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(attempt, range(4))) == 1
    with pytest.raises(Busy):
        refresh(db, key, now=now + 1)


def test_observation_identity_reuse_with_other_data_is_error(db):
    key = add_watch(db, value())
    now = int(time.time() / 900) * 900 + 100
    refresh(db, key, now=now)

    class Corrupt:
        def fetch(self, c, s, t):
            return [replace(DemoProvider().fetch(c, "baseline", t)[0], total_cents=1)]

    result = refresh(db, key, provider=Corrupt(), now=now + 6)
    assert result["status"] == "error"
    with db.connect() as con:
        assert (
            con.execute("SELECT total_cents FROM observations").fetchone()[0] == 42000
        )


def test_cooldown_between_new_observations(db):
    now = int(time.time() / 900) * 900 + 100
    key = add_watch(db, value())
    refresh(db, key, "drop", now=now)
    result = refresh(db, key, "different_conditions", now=now + 6)
    assert (
        not result["observations"][0]["alert_eligible"]
        and "Cooldown" in result["observations"][0]["reason"]
    )


def test_disabled_alerts(db):
    request = value()
    request.enabled = False
    key = add_watch(db, request)
    assert not refresh(db, key, "drop")["observations"][0]["alert_eligible"]


def test_api_and_configuration(tmp_path):
    with TestClient(create_app(tmp_path / "api.db")) as client:
        request = value().model_dump()
        response = client.post("/api/watches", json=request)
        assert response.status_code == 201
        key = response.json()["id"]
        assert client.post("/api/watches", json=request).status_code == 409
        assert (
            client.post(
                f"/api/watches/{key}/refresh", json={"scenario": "baseline"}
            ).json()["status"]
            == "success"
        )
        assert (
            client.post(
                f"/api/watches/{key}/refresh", json={"scenario": "drop"}
            ).status_code
            == 429
        )
        watch = client.get("/api/watches").json()[0]
        assert (
            watch["latest_offers"][0]["currency"] == "USD"
            and watch["last_success_at"] is not None
        )
        assert client.get(f"/api/watches/{key}/history").json()["query_count"] == 1
        assert (
            client.patch(
                f"/api/watches/{key}",
                json={"target_cents": 50000, "enabled": False, "channel": "simulation"},
            ).json()["enabled"]
            == 0
        )
        assert client.get("/api/health").json()["live_flights"] is False
        assert client.get("/").status_code == 200
        assert client.get("/api/watches/missing/history").status_code == 404
        assert (
            client.post(
                "/api/watches",
                json=request | {"criteria": request["criteria"] | {"adults": 0}},
            ).status_code
            == 422
        )


def test_telegram_mocked_ambiguous_delivery_is_not_retried(db, monkeypatch):
    request = value()
    request.channel = "telegram"
    key = add_watch(db, request)
    refresh(db, key, "drop")
    monkeypatch.setenv("RADAR_TELEGRAM_ENABLED", "1")
    monkeypatch.setenv("RADAR_TELEGRAM_TOKEN", "mock-token")
    monkeypatch.setenv("RADAR_TELEGRAM_CHAT_ID", "mock-own-chat")
    calls = []

    def transport(request):
        calls.append(1)
        raise httpx.ReadTimeout("unconfirmed")

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        result = deliver_telegram(db, client)
        assert result[0]["status"] == "uncertain"
        assert deliver_telegram(db, client) == []
    assert len(calls) == 1
    with db.connect() as con:
        row = con.execute("SELECT * FROM alerts").fetchone()
        assert "mock-token" not in (row["error"] or "") and row["attempts"] == 1


def test_telegram_real_requires_explicit_enable(db, monkeypatch):
    monkeypatch.delenv("RADAR_TELEGRAM_ENABLED", raising=False)
    with pytest.raises(RuntimeError):
        deliver_telegram(db)
