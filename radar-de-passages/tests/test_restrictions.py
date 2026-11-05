"""Access restrictions: one detector, typed errors, incidents, circuit breaker, egress, throttling."""

import json
import httpx
import pytest
from radar import restrictions
from radar.egress import EgressManager, SingleClient
from radar.notifications import notify_incidents
from radar.politeness import DomainGate
from radar.providers.base import (
    AccessDeniedError, BlockedError, CaptchaError, EgressError, EmptyResponseError, GoneError,
    NetworkFailure, ProviderError, RateLimitedError, SessionError, StructureError, TimeoutFailure,
    TransientError, UnexpectedRedirectError,
)
from radar.providers.storepage import StorePageProvider
from radar.service import add_watch
from radar.security import Secret, redact, sanitize_for_log, strip_credentials, RedactingFilter
from conftest import FakeLive, product_watch
from test_providers import GOOD, NOW, PUBLIC, crit, ld, store
from test_scheduler import T0, make, run_for, schedule_row

PAGE = "<html><head><title>x</title></head><body>" + "<p>contenido normal de la tienda</p>" * 20 + "</body></html>"


# --- the detector itself -------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,body,expected",
    [
        (429, "", RateLimitedError),
        (401, "", SessionError),
        (403, "", AccessDeniedError),
        (451, "", AccessDeniedError),
        (403, "please solve this captcha", CaptchaError),
        (200, PAGE.replace("normal", "captcha"), CaptchaError),
        (503, "Just a moment...", CaptchaError),
        (503, "Request blocked", AccessDeniedError),
        (200, "<html>" + "x" * 500 + "Access Denied</html>", AccessDeniedError),
        (404, "", GoneError),
        (410, "", GoneError),
        (500, "", TransientError),
        (503, "overloaded", TransientError),
        (200, "<html></html>", EmptyResponseError),
        (200, "   ", EmptyResponseError),
        (200, PAGE, None),
    ],
)
def test_classification_table(status, body, expected):
    problem = restrictions.classify_response(status, body)
    assert (type(problem) if problem else None) is expected


def test_a_recaptcha_script_on_a_page_that_has_price_data_is_not_a_block():
    page = PAGE.replace("normal", "recaptcha") + ' <script type="application/ld+json">{}</script>'
    assert restrictions.classify_response(200, page, has_data=True) is None
    assert isinstance(restrictions.classify_response(200, page), CaptchaError)


def test_every_explicit_rejection_is_a_pause_kind_and_severity_is_consistent():
    for cls in (BlockedError, RateLimitedError, CaptchaError, AccessDeniedError, SessionError, UnexpectedRedirectError):
        assert cls.error_kind in restrictions.PAUSE_KINDS and restrictions.severity(cls.error_kind) == "high"
    for cls in (StructureError, GoneError, EmptyResponseError):
        assert cls.error_kind in restrictions.WATCH_KINDS and restrictions.severity(cls.error_kind) == "medium"
    for cls in (TransientError, TimeoutFailure, NetworkFailure):
        assert cls.error_kind in restrictions.TRANSIENT_KINDS and restrictions.severity(cls.error_kind) == "low"
    assert restrictions.action_for("captcha") == "domain_paused" and restrictions.action_for("timeout") == "retry_backoff"


def allowed(host):
    return host.endswith("shop.test")


def test_redirect_classification():
    assert restrictions.classify_redirect("https://shop.test/p/1", "https://shop.test/p/1-nuevo", allowed) is None
    assert isinstance(restrictions.classify_redirect("https://shop.test/p/1", "https://evil.test/p/1", allowed), UnexpectedRedirectError)
    assert isinstance(restrictions.classify_redirect("https://shop.test/p/1", "https://shop.test/login?next=/p/1", allowed), SessionError)
    assert isinstance(restrictions.classify_redirect("https://shop.test/p/1", "https://shop.test/captcha/verify", allowed), UnexpectedRedirectError)
    # a page that already lives under /login is not "redirected to login"
    assert restrictions.classify_redirect("https://shop.test/login/help", "https://shop.test/login/faq", allowed) is None


# --- through the store provider (what the site really answered) --------------------------------------
def fetch(routes, **kw):
    provider, seen = store(routes, **kw)
    return provider, seen


def raises(routes, error):
    provider, _ = fetch(routes)
    with pytest.raises(error) as caught:
        provider.fetch(crit(), "x", NOW)
    return caught.value


def test_the_provider_records_http_evidence_on_every_restriction():
    err = raises({"/p/1": (403, "forbidden", {})}, AccessDeniedError)
    assert err.status == 403 and err.url.endswith("/p/1")
    assert raises({"/p/1": (401, "", {})}, SessionError).status == 401
    assert raises({"/p/1": (200, PAGE.replace("normal", "captcha"), {})}, CaptchaError).status == 200
    assert raises({"/p/1": (200, "<html></html>", {})}, EmptyResponseError)
    assert raises({"/p/1": (410, "", {})}, GoneError).status == 410


def test_a_recaptcha_widget_on_a_normal_product_page_does_not_stop_the_monitor():
    page = GOOD.replace("</head>", "<script src='https://x/recaptcha.js'></script></head>")
    provider, _ = fetch({"/p/1": (200, page, {})})
    assert provider.fetch(crit(), "x", NOW)[0].total_cents == 129990


def test_login_and_challenge_redirects_are_rejections_not_things_to_follow():
    provider, seen = fetch({"/p/1": (302, "", {"Location": "/login?next=/p/1"}), "/login": (200, PAGE, {})})
    with pytest.raises(SessionError):
        provider.fetch(crit(), "x", NOW)
    assert not any("/login" in u for u in seen)  # the login page was never requested
    provider, seen = fetch({"/p/1": (302, "", {"Location": "/captcha/check"}), "/captcha/check": (200, PAGE, {})})
    with pytest.raises(UnexpectedRedirectError):
        provider.fetch(crit(), "x", NOW)
    provider, seen = fetch({"/p/1": (301, "", {"Location": "https://other.test/p/1"})})
    with pytest.raises(UnexpectedRedirectError, match="no permitido"):
        provider.fetch(crit(), "x", NOW)


def test_only_html_is_parsed_for_a_product_page():
    err = raises({"/p/1": (200, json.dumps({"price": 10}), {"content-type": "application/json"})}, StructureError)
    assert "tipo esperado" in str(err)


def test_page_that_changed_structure_is_reported_as_structure_not_block():
    err = raises({"/p/1": (200, PAGE, {})}, StructureError)
    assert "JSON-LD" in str(err) and err.error_kind == "structure"


def test_timeouts_and_connection_failures_are_distinct_typed_transients():
    def provider_raising(exc):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(exc)))
        return StorePageProvider(["shop.test"], client=client, resolver=PUBLIC)

    with pytest.raises(TimeoutFailure):
        provider_raising(httpx.ReadTimeout("slow")).fetch(crit(), "x", NOW)
    with pytest.raises(NetworkFailure):
        provider_raising(httpx.ConnectError("down")).fetch(crit(), "x", NOW)


# --- the scheduler's reaction ---------------------------------------------------------------------------
def incidents(db):
    return restrictions.incidents(db, 100)


def raising(error):
    def behaviour(criteria, now):
        raise error.with_evidence(getattr(error, "http", 403), criteria.url)

    return behaviour


def test_a_captcha_pauses_the_domain_logs_the_incident_and_stops_the_requests(db):
    fake = FakeLive(raising(CaptchaError("verificación humana")))
    sched, clock = make(db, fake)
    key = add_watch(db, product_watch(1))
    run_for(sched, clock, 120)
    calls = len(fake.calls)
    assert calls == 1
    [incident] = incidents(db)
    assert (incident["kind"], incident["severity"], incident["action"]) == ("captcha", "high", "domain_paused")
    assert incident["domain"] == "shop.test" and incident["http_status"] == 403 and incident["watch_id"] == key
    assert incident["url"] == "https://shop.test/p/1" and incident["provider"] == "fake"
    run_for(sched, clock, 600)  # inside the pause window: no new requests reach the source
    assert len(fake.calls) == calls
    with db.connect() as con:
        state = dict(con.execute("SELECT * FROM domain_state WHERE domain='shop.test'").fetchone())
        row = dict(con.execute("SELECT * FROM queries WHERE watch_id=?", (key,)).fetchone())
    assert state["status"] == "cooling" and state["blocked_until"] > clock["now"]
    assert row["status"] == "error" and row["error_kind"] == "captcha"


@pytest.mark.parametrize(
    "error", [AccessDeniedError("x"), SessionError("x"), UnexpectedRedirectError("x"), RateLimitedError("x", 120), BlockedError("x")]
)
def test_every_restriction_kind_pauses_the_whole_domain_not_just_one_watch(db, error):
    fake = FakeLive(raising(error))
    sched, clock = make(db, fake)
    first, second = add_watch(db, product_watch(1)), add_watch(db, product_watch(2))
    run_for(sched, clock, 300)
    assert len(fake.calls) == 1  # the second watch of the same domain was NOT tried after the rejection
    with db.connect() as con:
        assert con.execute("SELECT status FROM domain_state WHERE domain='shop.test'").fetchone()[0] == "cooling"
        untouched = [r[0] for r in con.execute("SELECT last_status FROM watch_schedule WHERE watch_id IN (?,?)", (first, second))]
    assert sorted(map(str, untouched)) == sorted(["None", error.error_kind])


def test_circuit_opens_after_repeated_rejections_and_only_a_human_resumes_it(db):
    fake = FakeLive(raising(AccessDeniedError("x")))
    sched, clock = make(db, fake, gate={"backoff_base": 900.0, "backoff_cap": 3600.0, "disable_after": 3})
    add_watch(db, product_watch(1))
    run_for(sched, clock, 3 * 86400, step=60)
    kinds = [i["kind"] for i in incidents(db)]
    assert kinds.count("denied") == 3 and kinds.count("circuit_open") == 1
    calls = len(fake.calls)
    run_for(sched, clock, 86400, step=600)
    assert len(fake.calls) == calls  # a disabled source gets NO further requests, however long we wait
    assert sched.gate.resume("shop.test") is True
    run_for(sched, clock, 1800, step=10)
    assert len(fake.calls) > calls


def test_a_page_that_stopped_having_prices_pauses_that_watch_only(db):
    bad = {"n": 0}

    def behaviour(criteria, now):
        if criteria.url.endswith("/p/1"):
            bad["n"] += 1
            raise StructureError("sin precio")
        return 100_000

    fake = FakeLive(behaviour)
    sched, clock = make(db, fake, gate={"disable_after": 99}, gap=(1.0, 2.0), min_s=20, max_s=40)
    broken, healthy = add_watch(db, product_watch(1)), add_watch(db, product_watch(2))
    run_for(sched, clock, 6 * 3600, step=10)
    with db.connect() as con:
        assert con.execute("SELECT enabled FROM watches WHERE id=?", (broken,)).fetchone()[0] == 0
        assert con.execute("SELECT enabled FROM watches WHERE id=?", (healthy,)).fetchone()[0] == 1
        assert con.execute("SELECT status FROM domain_state WHERE domain='shop.test'").fetchone()[0] != "disabled"
    assert bad["n"] == 5  # exactly STRUCTURE_AFTER attempts, then it stops asking
    medium = [i for i in incidents(db) if i["severity"] == "medium"]
    assert medium and "pausado" in medium[0]["detail"] and medium[0]["watch_id"] == broken


def test_a_page_that_is_gone_is_paused_after_three_attempts(db):
    calls = {"n": 0}

    def behaviour(criteria, now):
        calls["n"] += 1
        raise GoneError("404")

    sched, clock = make(db, FakeLive(behaviour), gate={"disable_after": 99}, gap=(1.0, 2.0), min_s=20, max_s=40)
    key = add_watch(db, product_watch(1))
    run_for(sched, clock, 6 * 3600, step=10)
    assert calls["n"] == 3
    with db.connect() as con:
        assert con.execute("SELECT enabled FROM watches WHERE id=?", (key,)).fetchone()[0] == 0


def test_transient_failures_across_a_domain_make_it_cool_down_instead_of_being_hammered(db):
    calls = []

    def behaviour(criteria, now):
        calls.append(now)
        raise TimeoutFailure("lento")

    sched, clock = make(db, FakeLive(behaviour), gap=(1.0, 2.0), min_s=20, max_s=40)
    for i in range(6):
        add_watch(db, product_watch(i))
    run_for(sched, clock, 90, step=5)
    assert len(calls) == 3  # trouble_after=3 consecutive failures -> the whole domain rests (6 watches, 3 requests)
    with db.connect() as con:
        state = dict(con.execute("SELECT * FROM domain_state WHERE domain='shop.test'").fetchone())
        assert state["status"] == "cooling" and state["fail_streak"] == 3 and state["block_count"] == 0
        assert state["blocked_until"] >= calls[-1] + 120  # a transient failure is not a block: no strike, no circuit
    assert any(i["action"] == "domain_cooling" for i in incidents(db))
    run_for(sched, clock, 1800, step=5)
    assert len(calls) > 3  # it does come back, but never faster than its rest ...
    rests = [b - a for a, b in zip(calls, calls[1:])][2:]
    assert rests[0] >= 120 and rests[1] >= 240 and rests[1] >= 2 * 120  # ... and each rest doubles


def test_a_watch_is_paused_after_too_many_consecutive_failures_of_any_kind(db):
    calls = {"n": 0}

    def behaviour(criteria, now):
        calls["n"] += 1
        raise ProviderError("algo raro")

    sched, clock = make(db, FakeLive(behaviour), gap=(1.0, 2.0), min_s=20, max_s=40, pause_after_fails=4)
    key = add_watch(db, product_watch(1))
    run_for(sched, clock, 86400, step=20)
    assert calls["n"] == 4
    with db.connect() as con:
        assert con.execute("SELECT enabled FROM watches WHERE id=?", (key,)).fetchone()[0] == 0


# --- network exits ------------------------------------------------------------------------------------------
def manager(handlers, mapping, enabled=("proxy_a",)):
    clients = {n: httpx.Client(transport=httpx.MockTransport(h)) for n, h in handlers.items()}
    env = {"RADAR_EGRESS_PROFILES": "proxy_a,proxy_b", "RADAR_EGRESS_PROXY_A_URL": "http://user:pw@a.proxy:8080",
           "RADAR_EGRESS_PROXY_B_URL": "http://user:pw@b.proxy:8080", "RADAR_EGRESS_MAP": mapping}
    for name in ("proxy_a", "proxy_b"):
        if name not in enabled:
            env[f"RADAR_EGRESS_{name.upper()}_ENABLED"] = "0"
    return EgressManager.from_env(env, client_factory=lambda profile: clients.get(profile.name, clients["direct"]))


def test_each_domain_has_one_exit_exact_or_by_suffix_and_the_default_is_direct():
    m = manager({"direct": lambda r: httpx.Response(200)}, "shop.test=proxy_a,*.cl=proxy_b")
    assert m.profile_for("shop.test").name == "proxy_a"
    assert m.profile_for("tienda.cl").name == "proxy_b" and m.profile_for("cl").name == "proxy_b"
    assert m.profile_for("other.test").name == "direct" and m.profile_for("notcl.com").name == "direct"
    with pytest.raises(RuntimeError, match="no declarados"):
        EgressManager.from_env({"RADAR_EGRESS_MAP": "a.test=ghost"})


def test_proxy_credentials_never_appear_in_the_public_description_or_repr():
    m = manager({"direct": lambda r: httpx.Response(200)}, "shop.test=proxy_a")
    text = json.dumps(m.describe()) + repr(m.profiles["proxy_a"].url)
    assert "pw" not in text and "user" not in text and "a.proxy:8080" in text


def test_a_block_pauses_the_domain_and_never_moves_it_to_another_exit(db):
    seen = {"proxy_a": 0, "proxy_b": 0, "direct": 0}

    def answer(name, status):
        def handler(request):
            seen[name] += 1
            if request.url.path == "/robots.txt":
                return httpx.Response(404, headers={"content-type": "text/plain"})
            return httpx.Response(status, text="denied", headers={"content-type": "text/html"})

        return handler

    m = manager({"proxy_a": answer("proxy_a", 403), "proxy_b": answer("proxy_b", 200), "direct": answer("direct", 200)}, "shop.test=proxy_a")
    provider = StorePageProvider(["shop.test"], egress=m, resolver=PUBLIC)
    from radar.providers.registry import Registry
    from radar.scheduler import Scheduler
    import random

    clock = {"now": T0}
    rng = random.Random(3)
    sched = Scheduler(db, Registry([provider]), DomainGate(db, gap=(15.0, 45.0), rng=rng, backoff_base=900.0), min_s=60, max_s=120, rng=rng, clock=lambda: clock["now"])
    key = add_watch(db, product_watch(1, provider="store"))
    run_for(sched, clock, 600, step=10)  # inside the first pause window (>= 900 s)
    after_first = dict(seen)
    assert after_first["proxy_a"] >= 1 and after_first["proxy_b"] == 0 and after_first["direct"] == 0
    [incident] = [i for i in incidents(db) if i["kind"] == "denied"]
    assert incident["egress"] == "proxy_a" and incident["http_status"] == 403
    with db.connect() as con:
        assert dict(con.execute("SELECT * FROM queries WHERE watch_id=?", (key,)).fetchone())["egress"] == "proxy_a"
        assert con.execute("SELECT egress FROM domain_state WHERE domain='shop.test'").fetchone()[0] == "proxy_a"
    run_for(sched, clock, 300, step=10)
    assert dict(seen) == after_first  # while the pause lasts, not one request reaches the store, through ANY exit
    run_for(sched, clock, 4 * 3600, step=30)  # hours later it retries, but only ever through its own exit
    assert seen["proxy_a"] > after_first["proxy_a"] and seen["proxy_b"] == 0 and seen["direct"] == 0
    assert {i["egress"] for i in incidents(db) if i["kind"] in ("denied", "circuit_open")} == {"proxy_a"}


def test_a_disabled_exit_refuses_its_domains_and_never_falls_back_to_direct():
    seen = []

    def answering(label):
        def handler(request):
            seen.append(label)
            if request.url.path == "/robots.txt":
                return httpx.Response(404, headers={"content-type": "text/plain"})
            return httpx.Response(200, text=GOOD, headers={"content-type": "text/html"})

        return handler

    handlers = {"direct": answering("direct"), "proxy_a": answering("A")}
    m = manager(handlers, "shop.test=proxy_a", enabled=())
    provider = StorePageProvider(["shop.test"], egress=m, resolver=PUBLIC)
    with pytest.raises(EgressError, match="desactivada"):
        provider.fetch(crit(), "x", NOW)
    assert seen == []  # nothing left this server, through any exit
    m.set_enabled("proxy_a", True)
    assert provider.fetch(crit(), "x", NOW)[0].total_cents == 129990 and "A" in seen


# --- AutoThrottle, traffic accounting, incidents → Telegram --------------------------------------------------
def test_autothrottle_follows_latency_never_drops_on_errors_and_floors_the_gap(db):
    gate = DomainGate(db, gap=(1.0, 1.0), target_concurrency=1.0, max_delay=60.0)
    assert gate.record_request("slow.test", T0, 40.0, 200, 5000) == 20.0  # (0 + 40/1) / 2
    assert gate.record_request("slow.test", T0, 40.0, 200, 5000) == 30.0
    assert gate.record_request("slow.test", T0, 1.0, 503, 100) == 30.0  # a non-200 answer never speeds us up
    assert gate.record_request("slow.test", T0, 1.0, 200, 100) == 15.5  # healthy answers let it relax
    for _ in range(10):
        delay = gate.record_request("slow.test", T0, 500.0, 200, 1)
    assert delay <= 60.0  # clamped (Scrapy's AUTOTHROTTLE_MAX_DELAY)
    assert gate.reserve("slow.test", T0, 0) is True
    allowed, reason, wait = gate.check("slow.test", T0)
    assert allowed is False and wait >= 59.0  # the slow site's delay is the floor, not the 1 s gap
    with db.connect() as con:
        row = dict(con.execute("SELECT * FROM domain_state WHERE domain='slow.test'").fetchone())
    assert row["requests"] == 14 and row["bytes_in"] == 5000 + 5000 + 100 + 100 + 10


def test_the_scheduler_feeds_latency_and_bytes_from_real_requests_into_the_gate(db):
    from radar.providers.registry import Registry
    from radar.scheduler import Scheduler
    import random

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, headers={"content-type": "text/plain"})
        return httpx.Response(200, text=GOOD, headers={"content-type": "text/html"})

    provider = StorePageProvider(["shop.test"], client=httpx.Client(transport=httpx.MockTransport(handler)), resolver=PUBLIC)
    clock, rng = {"now": T0}, random.Random(1)
    sched = Scheduler(db, Registry([provider]), DomainGate(db, rng=rng), min_s=60, max_s=120, rng=rng, clock=lambda: clock["now"])
    add_watch(db, product_watch(1, provider="store"))
    run_for(sched, clock, 300, step=5)
    with db.connect() as con:
        row = dict(con.execute("SELECT * FROM domain_state WHERE domain='shop.test'").fetchone())
    assert row["requests"] >= 2 and row["bytes_in"] > len(GOOD) and row["latency_s"] is not None and row["egress"] == "direct"


def telegram_client(sent, status=200):
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(status, json={"ok": True})

    return httpx.Client(transport=httpx.MockTransport(handler))


def seed_incident(db, at, domain="shop.test", kind="captcha"):
    restrictions.record_incident(db, at, domain=domain, kind=kind, detail="verificación humana", status=403, url="https://shop.test/p/1", egress="direct")


def test_important_incidents_reach_telegram_once_per_domain_per_hour_without_secrets(db, monkeypatch):
    monkeypatch.setenv("RADAR_TELEGRAM_ENABLED", "1")
    monkeypatch.setenv("RADAR_TELEGRAM_TOKEN", "123456:SECRETTOKEN-abcdefghijklmnopqrstuvwxyz")
    monkeypatch.setenv("RADAR_TELEGRAM_CHAT_ID", "42")
    sent = []
    seed_incident(db, T0)
    seed_incident(db, T0 + 5, kind="denied")
    assert notify_incidents(db, telegram_client(sent), now=T0 + 10) == [{"domain": "shop.test", "notified": 1}]
    assert len(sent) == 1 and "shop.test" in sent[0]["text"] and "denied" in sent[0]["text"] and "--resume shop.test" in sent[0]["text"]
    assert "SECRETTOKEN" not in json.dumps(sent)
    seed_incident(db, T0 + 600)
    assert notify_incidents(db, telegram_client(sent), now=T0 + 700) == [{"domain": "shop.test", "notified": 2}]  # suppressed
    assert len(sent) == 1
    seed_incident(db, T0 + 4000)
    notify_incidents(db, telegram_client(sent), now=T0 + 4100)
    assert len(sent) == 2
    assert notify_incidents(db, telegram_client(sent), now=T0 + 4200) == []  # nothing pending: nothing sent


def test_low_and_medium_incidents_never_page_the_owner(db, monkeypatch):
    monkeypatch.setenv("RADAR_TELEGRAM_ENABLED", "1")
    monkeypatch.setenv("RADAR_TELEGRAM_TOKEN", "1:x")
    monkeypatch.setenv("RADAR_TELEGRAM_CHAT_ID", "42")
    sent = []
    seed_incident(db, T0, kind="timeout")
    seed_incident(db, T0, kind="structure")
    assert notify_incidents(db, telegram_client(sent), now=T0 + 1) == [] and sent == []


# --- log hygiene ----------------------------------------------------------------------------------------------
def test_secrets_are_masked_and_log_text_is_neutralised():
    assert repr(Secret("hunter2")) == "Secret(***)" and str(Secret("hunter2")) == "Secret(***)" and Secret("hunter2").reveal() == "hunter2"
    line = "GET https://api.telegram.org/bot123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ012345/sendMessage Authorization: Bearer abc.def via http://user:pass@proxy:8080"
    cleaned = redact(line, ["abc.def"])
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in cleaned and "pass@" not in cleaned and "abc.def" not in cleaned
    assert sanitize_for_log("a\nb\r\x00c") == "a\\nb\\r?c"
    assert strip_credentials("http://user:pw@host:8080/x?y=1") == "http://host:8080/x?y=1"
    import logging

    record = logging.LogRecord("x", logging.INFO, "f", 1, "token=%s", ("SECRET123",), None)
    RedactingFilter(["SECRET123"]).filter(record)
    assert "SECRET123" not in record.getMessage()


def test_incident_journal_redacts_credentials_in_details_and_urls(db):
    restrictions.record_incident(db, T0, domain="x.test", kind="captcha", detail="fallo con Authorization: Bearer topsecret", url="https://u:p@x.test/p/1")
    [row] = restrictions.incidents(db)
    assert "topsecret" not in row["detail"] and "u:p@" not in row["url"]
