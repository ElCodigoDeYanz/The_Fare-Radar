"""Command line entry points (what the container runs) and the Telegram delivery order and failure states."""

import io
import json
import logging
import sys
import time
import httpx
import pytest
from radar import main as cli, restrictions
from radar.database import Database
from radar.notifications import deliver_telegram
from radar.security import install_log_redaction
from radar.service import add_watch
from conftest import product_watch

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


def run_cli(monkeypatch, capsys, tmp_path, *args, env=None):
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "cli.db"))
    for key in ("RADAR_ENABLED_SOURCES", "RADAR_TELEGRAM_ENABLED", "RADAR_ALLOWED_STORE_DOMAINS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["radar.main", *args])
    cli.main()
    return capsys.readouterr().out


# --- the command line --------------------------------------------------------------------------------------------
def test_list_sources_prints_the_catalogue_and_touches_no_database(monkeypatch, capsys, tmp_path):
    out = run_cli(monkeypatch, capsys, tmp_path, "--list-sources")
    lines = out.strip().splitlines()
    assert len(lines) >= 150 and any(l.startswith("falabella-pe") and "unverified" in l for l in lines)
    assert not (tmp_path / "cli.db").exists()


def test_import_sources_creates_catalogs_once_and_names_the_domains_to_allow(monkeypatch, capsys, tmp_path):
    out = run_cli(monkeypatch, capsys, tmp_path, "--import-sources", "--only", "falabella-pe,wong-pe")
    first = json.loads(out.splitlines()[0])
    assert first == {"created": 2, "existing": 0, "stores": 2}
    assert out.splitlines()[1].startswith("Dominios que deben estar permitidos:") and "falabella.com.pe" in out
    again = run_cli(monkeypatch, capsys, tmp_path, "--import-sources", "--only", "falabella-pe,wong-pe")
    assert json.loads(again.splitlines()[0]) == {"created": 0, "existing": 2, "stores": 2}
    db = Database(tmp_path / "cli.db")
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM catalogs WHERE channel='simulation' AND alert_mode='smart'").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 0  # nothing is requested by importing


def test_import_uses_the_environment_when_no_selector_is_given_and_refuses_to_guess(monkeypatch, capsys, tmp_path):
    out = run_cli(monkeypatch, capsys, tmp_path, "--import-sources", env={"RADAR_ENABLED_SOURCES": "wong-pe", "RADAR_ITEMS_PER_CATEGORY": "3"})
    assert json.loads(out.splitlines()[0])["created"] == 1
    with Database(tmp_path / "cli.db").connect() as con:
        assert con.execute("SELECT max_items FROM catalogs").fetchone()[0] == 3
    with pytest.raises(SystemExit, match="Indica qué fuentes"):
        run_cli(monkeypatch, capsys, tmp_path / "other", "--import-sources")
    with pytest.raises(SystemExit, match="inválida"):
        run_cli(monkeypatch, capsys, tmp_path, "--import-sources", "--only", "no-such-store")


def test_telegram_makes_catalog_alerts_telegram_only_when_explicitly_enabled(monkeypatch, capsys, tmp_path):
    run_cli(monkeypatch, capsys, tmp_path, "--import-sources", "--only", "wong-pe", env={"RADAR_TELEGRAM_ENABLED": "1"})
    with Database(tmp_path / "cli.db").connect() as con:
        assert con.execute("SELECT channel FROM catalogs").fetchone()[0] == "telegram"


def test_verify_sources_only_allows_the_selected_stores_and_writes_the_report(monkeypatch, capsys, tmp_path):
    seen = {}

    def fake_verify(provider, selected, now, pause, between=lambda: None, stop_on_block=False):
        seen["allowed"] = provider.allowed
        seen["ids"] = [s["id"] for s in selected]
        return {"generated_at": now, "summary": {"usable": len(selected)}, "sources": []}

    monkeypatch.setattr("radar.verify.verify_all", fake_verify)
    report = tmp_path / "out" / "report.json"
    out = run_cli(monkeypatch, capsys, tmp_path, "--verify-sources", "--only", "wong-pe", "--out", str(report))
    assert json.loads(out) == {"usable": 1} and seen["ids"] == ["wong-pe"] and seen["allowed"] == ("wong.pe",)  # only the selected store may be contacted
    assert json.loads(report.read_text())["summary"] == {"usable": 1}


def test_incidents_resume_metrics_and_status_commands(monkeypatch, capsys, tmp_path):
    db = Database(tmp_path / "cli.db")
    restrictions.record_incident(db, 1_700_000_000, domain="a.test", kind="captcha", detail="verificación humana", status=403)
    with db.connect() as con:
        con.execute("INSERT INTO domain_state(domain,status,block_count,blocked_until) VALUES ('a.test','disabled',5,9999999999)")
    listing = run_cli(monkeypatch, capsys, tmp_path, "--incidents")
    assert "a.test" in listing and "captcha" in listing and "high" in listing and "HTTP 403" in listing
    assert run_cli(monkeypatch, capsys, tmp_path, "--resume", "nope.test").strip() == "dominio sin estado registrado"
    assert run_cli(monkeypatch, capsys, tmp_path, "--resume", "a.test").strip() == "reanudado"
    with db.connect() as con:
        assert dict(con.execute("SELECT * FROM domain_state").fetchone())["status"] == "ok"
    metrics = json.loads(run_cli(monkeypatch, capsys, tmp_path, "--metrics"))
    assert {"cpu_seconds", "rss_mb", "database_mb", "disk_free_gb", "requests_total", "downloaded_mb", "rows"} <= set(metrics)
    status = json.loads(run_cli(monkeypatch, capsys, tmp_path, "--status"))
    assert status["interval_s"] == [300.0, 900.0] and "resources" in status


def test_run_in_the_foreground_starts_works_and_stops_cleanly(monkeypatch, capsys, tmp_path):
    out = run_cli(monkeypatch, capsys, tmp_path, "--run", "--run-for", "0.3")
    status = json.loads(out)
    assert status["interval_s"] == [300.0, 900.0] and status["running"] is False  # it stopped when asked


def test_the_removed_provider_variable_does_nothing(monkeypatch, capsys, tmp_path):
    out = run_cli(monkeypatch, capsys, tmp_path, "--status", env={"RADAR_DUFFEL_TOKEN": "x"})
    assert "duffel" not in out.lower()


# --- log hygiene --------------------------------------------------------------------------------------------------------
def test_request_urls_with_a_telegram_token_never_reach_a_log_line():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.INFO)
    try:
        install_log_redaction([TOKEN])
        install_log_redaction([TOKEN])  # idempotent
        assert sum(type(f).__name__ == "RedactingFilter" for f in handler.filters) == 1
        logging.getLogger("radar.test").info("POST https://api.telegram.org/bot%s/sendMessage", TOKEN)
        logging.getLogger("radar.test").warning("TOKEN=%s used", "short:tok")
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))).post(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
    text = stream.getvalue()
    assert TOKEN not in text and "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in text and "short:tok" not in text
    assert logging.getLogger("httpx").level >= logging.WARNING  # per-request INFO lines are off altogether


# --- Telegram delivery ---------------------------------------------------------------------------------------------------
@pytest.fixture
def telegram(monkeypatch):
    monkeypatch.setenv("RADAR_TELEGRAM_ENABLED", "1")
    monkeypatch.setenv("RADAR_TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setenv("RADAR_TELEGRAM_CHAT_ID", "42")


def seed_alert(db, n, rank, *, status="pending", expires=None, channel="telegram", created=None):
    key = add_watch(db, product_watch(n))
    now = time.time()
    with db.connect() as con:
        con.execute(
            "INSERT INTO observations(id,watch_id,provider,provider_observation_id,observed_at,expires_at,currency,total_cents,criteria_json,"
            "conditions_json,comparison_key,source,synthetic) VALUES (?,?,'fake','p',?,?,'PEN',1000,'{}','{}','k','s',0)",
            (f"o{n}", key, now, expires if expires is not None else now + 3600),
        )
        con.execute(
            "INSERT INTO alerts(id,watch_id,observation_id,created_at,channel,status,message,priority_rank) VALUES (?,?,?,?,?,?,?,?)",
            (f"a{n}", key, f"o{n}", created or now + n, channel, status, f"mensaje {n}", rank),
        )
    return f"a{n}"


def sender(sent, status=200, payload=None, raises=None):
    def handler(request):
        sent.append(json.loads(request.content)["text"])
        if raises:
            raise raises
        return httpx.Response(status, json=payload if payload is not None else {"ok": True})

    return httpx.Client(transport=httpx.MockTransport(handler))


def states(db):
    with db.connect() as con:
        return {r["id"]: (r["status"], r["attempts"]) for r in con.execute("SELECT id,status,attempts FROM alerts")}


def test_real_delivery_is_off_unless_explicitly_enabled_and_configured(db, monkeypatch):
    monkeypatch.delenv("RADAR_TELEGRAM_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="desactivado"):
        deliver_telegram(db)
    monkeypatch.setenv("RADAR_TELEGRAM_ENABLED", "1")
    monkeypatch.delenv("RADAR_TELEGRAM_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="Falta token"):
        deliver_telegram(db)


def test_the_most_important_alert_goes_first_then_oldest_first(telegram, db):
    seed_alert(db, 1, rank=1)
    seed_alert(db, 2, rank=4)
    seed_alert(db, 3, rank=2)
    seed_alert(db, 4, rank=4)
    sent = []
    result = deliver_telegram(db, sender(sent))
    assert sent == ["mensaje 2", "mensaje 4", "mensaje 3", "mensaje 1"]  # maxima (oldest first), alta... then objetivo
    assert [r["status"] for r in result] == ["sent"] * 4
    assert set(states(db).values()) == {("sent", 1)}


def test_only_telegram_alerts_that_are_pending_are_sent(telegram, db):
    seed_alert(db, 1, 1, channel="simulation", status="simulated")
    seed_alert(db, 2, 1, status="sent")
    seed_alert(db, 3, 1, status="failed")
    sent = []
    assert deliver_telegram(db, sender(sent)) == [] and sent == []


def test_an_alert_whose_price_already_expired_is_not_sent(telegram, db):
    seed_alert(db, 1, 4, expires=time.time() - 5)
    sent = []
    deliver_telegram(db, sender(sent))
    assert sent == [] and states(db)["a1"][0] == "failed"
    with db.connect() as con:
        assert "caducó" in con.execute("SELECT error FROM alerts").fetchone()[0]


@pytest.mark.parametrize(
    "kwargs,expected",
    [({"status": 200, "payload": {"ok": True}}, "sent"), ({"status": 200, "payload": {"ok": False}}, "failed"),
     ({"status": 400, "payload": {"ok": False}}, "failed"), ({"status": 500}, "uncertain"),
     ({"raises": httpx.ReadTimeout("slow")}, "uncertain"), ({"raises": httpx.ConnectError("down")}, "uncertain")],
)
def test_every_outcome_is_recorded_and_an_ambiguous_one_is_never_retried(telegram, db, kwargs, expected):
    seed_alert(db, 1, 3)
    sent = []
    deliver_telegram(db, sender(sent, **kwargs))
    assert states(db)["a1"][0] == expected
    deliver_telegram(db, sender(sent, **kwargs))  # the next cycle
    assert len(sent) == 1  # nothing is pending any more: no duplicate message, not even after a failure
    with db.connect() as con:
        assert TOKEN not in (con.execute("SELECT COALESCE(error,'') FROM alerts").fetchone()[0])


def test_an_alert_stuck_in_sending_after_a_crash_is_marked_uncertain_not_resent(telegram, db):
    seed_alert(db, 1, 3, status="sending")
    sent = []
    deliver_telegram(db, sender(sent))
    assert sent == [] and states(db)["a1"][0] == "uncertain"
