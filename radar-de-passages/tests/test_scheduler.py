"""Scheduler: random cadence in range, per-domain spacing, backoff, circuit breaker, lease."""

import random
import statistics
import time
import pytest
from radar.politeness import DomainGate
from radar.providers.base import BlockedError, RateLimitedError
from radar.scheduler import Scheduler, LEASE_TTL
from radar.service import add_watch
from conftest import FakeLive, flight_watch, product_watch, registry_with

T0 = float(int(time.time()))  # start of the simulated clock; it is injected, nothing sleeps


def make(db, *providers, seed=7, **kw):
    clock = {"now": T0}
    rng = random.Random(seed)
    gate = DomainGate(db, gap=kw.pop("gap", (15.0, 45.0)), rng=rng, **kw.pop("gate", {}))
    kw.setdefault("min_s", 60.0)  # these simulations use the original 1-10 min window
    kw.setdefault("max_s", 600.0)
    sched = Scheduler(
        db,
        registry_with(*providers),
        gate,
        rng=rng,
        clock=lambda: clock["now"],
        max_workers=kw.pop("max_workers", 4),
        **kw,
    )
    return sched, clock


def run_for(sched, clock, seconds, step=10):
    """Advance the injected clock, ticking like the real loop; returns every run summary."""
    out = []
    for _ in range(int(seconds // step)):
        out += sched.tick(clock["now"])
        clock["now"] += step
    return out


def schedule_row(db, key):
    with db.connect() as con:
        return dict(con.execute("SELECT * FROM watch_schedule WHERE watch_id=?", (key,)).fetchone())


def test_next_run_is_always_inside_the_configured_window_and_varies(db):
    sched, clock = make(db)
    key = add_watch(db, flight_watch())
    deltas = []
    for _ in range(300):
        sched.ensure_schedules(clock["now"])
        clock["now"] = schedule_row(db, key)["next_run_at"]
        assert sched.tick(clock["now"])[0]["status"] == "success"
        deltas.append(schedule_row(db, key)["next_run_at"] - clock["now"])
    assert min(deltas) >= 60 and max(deltas) <= 600
    # Not a fixed beat: the spread covers most of the window and no value repeats a lot.
    assert max(deltas) - min(deltas) > 400
    assert statistics.pstdev(deltas) > 100
    assert len({round(d) for d in deltas}) > 200


def test_first_check_of_a_new_watch_is_soon_but_not_instant(db):
    sched, clock = make(db)
    keys = [add_watch(db, product_watch(n, provider="demo")) for n in range(1, 21)]
    sched.ensure_schedules(T0)
    firsts = [schedule_row(db, k)["next_run_at"] - T0 for k in keys]
    assert all(5 <= f <= 60 for f in firsts)
    assert len({round(f, 1) for f in firsts}) > 10  # spread out, not a thundering herd


def test_requests_to_one_domain_are_never_closer_than_the_minimum_gap(db):
    fake = FakeLive()
    sched, clock = make(db, fake, gap=(15.0, 45.0))
    for n in range(1, 9):
        add_watch(db, product_watch(n))
    run_for(sched, clock, 3 * 3600)  # three simulated hours
    times = sorted(fake.calls)
    assert len(times) > 50
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= 15.0


def test_two_domains_are_not_serialised_against_each_other(db):
    a, b = FakeLive(domain="a.test"), FakeLive(domain="b.test")
    b.name = "fake-b"
    sched, clock = make(db, a, b)
    add_watch(db, product_watch(1, provider="fake"))
    add_watch(db, product_watch(2, provider="fake-b"))
    ran = {r["domain"] for r in run_for(sched, clock, 120, step=1)}
    assert ran == {"a.test", "b.test"}


def test_block_pauses_the_domain_with_exponential_backoff_and_retry_after(db):
    def blocked(criteria, now):
        raise BlockedError("403")

    fake = FakeLive(blocked)
    sched, clock = make(db, fake, gate={"backoff_base": 900.0, "backoff_cap": 21600.0})
    add_watch(db, product_watch(1))
    add_watch(db, product_watch(2))
    first = None
    for _ in range(200):
        out = sched.tick(clock["now"])
        if out:
            first = clock["now"]
            break
        clock["now"] += 1
    assert first is not None and len(fake.calls) == 1
    state = sched.gate.states()[0]
    assert state["status"] == "cooling" and state["block_count"] == 1
    assert state["blocked_until"] - first >= 900
    # Nothing else is sent to that domain while it cools down (both watches wait).
    run_for(sched, clock, 850)
    assert len(fake.calls) == 1


def test_retry_after_longer_than_backoff_is_honoured(db):
    def limited(criteria, now):
        raise RateLimitedError("429", retry_after=7200)

    sched, clock = make(db, FakeLive(limited))
    add_watch(db, product_watch(1))
    while not sched.tick(clock["now"]):
        clock["now"] += 1
    assert sched.gate.states()[0]["blocked_until"] - clock["now"] >= 7200


def test_repeated_blocks_open_the_circuit_until_a_human_resumes(db):
    def blocked(criteria, now):
        raise BlockedError("captcha")

    fake = FakeLive(blocked)
    sched, clock = make(db, fake, gate={"disable_after": 3})
    add_watch(db, product_watch(1))
    run_for(sched, clock, 3 * 86400, step=60)
    assert len(fake.calls) == 3  # exactly the allowed number of blocks, then silence
    assert sched.gate.states()[0]["status"] == "disabled"
    assert sched.gate.resume("shop.test") is True
    assert sched.gate.states()[0]["block_count"] == 0
    # After resuming it is tried again.
    before = len(fake.calls)
    run_for(sched, clock, 2000)
    assert len(fake.calls) > before


def test_a_success_clears_the_block_history(db):
    state = {"fail": True}

    def flaky(criteria, now):
        if state["fail"]:
            raise BlockedError("403")
        return 90_000

    fake = FakeLive(flaky)
    sched, clock = make(db, fake)
    add_watch(db, product_watch(1))
    while not sched.tick(clock["now"]):
        clock["now"] += 1
    assert sched.gate.states()[0]["block_count"] == 1
    state["fail"] = False
    clock["now"] = sched.gate.states()[0]["blocked_until"] + 700
    while not sched.tick(clock["now"]):
        clock["now"] += 1
    assert sched.gate.states()[0]["block_count"] == 0
    assert sched.gate.states()[0]["status"] == "ok"


def test_transient_errors_back_off_only_that_watch(db):
    from radar.providers.base import TransientError

    def transient(criteria, now):
        if criteria.url.endswith("/1"):
            raise TransientError("timeout")
        return 90_000

    fake = FakeLive(transient)
    sched, clock = make(db, fake)
    bad = add_watch(db, product_watch(1))
    good = add_watch(db, product_watch(2))
    run_for(sched, clock, 40 * 60)
    assert schedule_row(db, bad)["fail_count"] >= 1
    assert schedule_row(db, good)["fail_count"] == 0
    assert sched.gate.states()[0]["status"] == "ok"  # not a block: the domain is not paused


def test_unexpected_exception_in_one_watch_does_not_stop_the_loop(db):
    def bug(criteria, now):
        raise ValueError("boom")

    sched, clock = make(db, FakeLive(bug))
    key = add_watch(db, product_watch(1))
    other = add_watch(db, flight_watch())
    seen = {(r["watch_id"], r["status"]) for r in run_for(sched, clock, 900, step=5)}
    assert (key, "internal") in seen
    assert (other, "success") in seen


def test_lease_gives_a_single_active_scheduler(db):
    a, clock = make(db, seed=1)
    b = Scheduler(db, a.registry, a.gate, clock=lambda: clock["now"], owner="other")
    add_watch(db, flight_watch())
    assert a.hold_lease(T0) is True
    assert b.hold_lease(T0) is False and b.tick(T0 + 1) == []
    # The holder keeps renewing; once it stops, the other takes over after the TTL.
    assert a.hold_lease(T0 + 30) is True
    assert b.hold_lease(T0 + 30 + LEASE_TTL - 1) is False
    assert b.hold_lease(T0 + 30 + LEASE_TTL + 1) is True
    assert a.hold_lease(T0 + 30 + LEASE_TTL + 2) is False
    b.release_lease()
    assert a.hold_lease(T0 + 200) is True


def test_unconfigured_provider_leaves_a_visible_error_and_backs_off(db):
    sched, clock = make(db)
    key = add_watch(db, flight_watch().model_copy(update={"provider": "aerolinea-x"}))
    while not sched.tick(clock["now"]):
        clock["now"] += 1
    with db.connect() as con:
        row = con.execute("SELECT * FROM queries WHERE watch_id=?", (key,)).fetchone()
    assert row["status"] == "error" and "no está configurado" in row["error"]
    assert schedule_row(db, key)["fail_count"] == 1


def test_disabled_provider_is_not_silently_replaced_by_the_demo(db):
    from radar.providers.registry import Registry

    clock = {"now": T0}
    registry = Registry([])  # e.g. RADAR_ENABLE_DEMO=0 and nothing else configured
    sched = Scheduler(db, registry, clock=lambda: clock["now"], rng=random.Random(3))
    key = add_watch(db, flight_watch())  # provider="demo"
    while not sched.tick(clock["now"]):
        clock["now"] += 1
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        assert con.execute("SELECT error FROM queries WHERE watch_id=?", (key,)).fetchone()[0]


def test_monthly_cap_stops_calls_before_they_reach_the_provider(db):
    fake = FakeLive()
    fake.monthly_cap = 3
    sched, clock = make(db, fake)
    sched.pace = lambda *a, **k: 0.0  # isolate the HARD cap from the month-long pacing
    key = add_watch(db, product_watch(1))
    run_for(sched, clock, 6 * 3600, step=30)
    assert len(fake.calls) == 3
    with db.connect() as con:
        errors = [r[0] for r in con.execute("SELECT error_kind FROM queries WHERE status='error'")]
    assert errors and set(errors) == {"budget"}
    assert schedule_row(db, key)["last_status"] == "budget"


def test_disabled_watches_are_never_scheduled(db):
    fake = FakeLive()
    sched, clock = make(db, fake)
    add_watch(db, product_watch(1).model_copy(update={"enabled": False}))
    run_for(sched, clock, 1200)
    assert fake.calls == []


def test_status_warns_when_a_domain_has_more_watches_than_the_spacing_allows(db):
    sched, clock = make(db, FakeLive(), gap=(15.0, 45.0))  # mean gap 30 s
    for n in range(1, 21):
        add_watch(db, product_watch(n))
    capacity = sched.status(T0)["capacity"]
    assert capacity and capacity[0]["watches"] == 20
    assert capacity[0]["effective_interval_s"] == 600  # 20 x 30 s


def test_invalid_interval_is_rejected(db):
    with pytest.raises(ValueError):
        Scheduler(db, registry_with(), min_s=600, max_s=60)
    with pytest.raises(ValueError):
        Scheduler(db, registry_with(), min_s=0, max_s=60)


def test_monthly_cap_is_paced_across_the_month_not_spent_on_day_one(db):
    import calendar

    start = float(calendar.timegm((2026, 11, 3, 0, 0, 0)))
    fake = FakeLive()
    fake.monthly_cap = 3000
    clock = {"now": start}
    rng = random.Random(5)
    sched = Scheduler(db, registry_with(fake), DomainGate(db, gap=(1.0, 2.0), rng=rng), rng=rng, clock=lambda: clock["now"])
    for n in range(1, 11):
        add_watch(db, product_watch(n))
    run_for(sched, clock, 2 * 86400, step=20)
    # Two days of a 28-day remaining window: about 3000 * 2/28 = 214 calls, never anywhere near the cap.
    assert 120 <= len(fake.calls) <= 300
    # Without the cap the same 10 watches would have made ~10 * 2 * 86400/330 = 5000+ calls.
    capacity = sched.status(clock["now"])["capacity"]
    assert capacity and "tope mensual" in capacity[0]["note"]
    assert capacity[0]["effective_interval_s"] > 3600


def test_uncapped_providers_are_not_paced(db):
    sched, _ = make(db, FakeLive())
    assert sched.pace(sched.registry.get("fake"), T0) == 0.0


def test_a_trip_whose_date_has_passed_is_paused_not_queried_forever(db):
    sched, clock = make(db)
    key = add_watch(db, flight_watch())  # departs in 45 days
    run_for(sched, clock, 86400, step=300)
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM queries WHERE status='success'").fetchone()[0] > 100
        assert con.execute("SELECT enabled FROM watches").fetchone()[0] == 1
    clock["now"] += 50 * 86400  # the departure date is now behind us
    run_for(sched, clock, 3 * 86400, step=600)
    with db.connect() as con:
        errors = [dict(r) for r in con.execute("SELECT error,error_kind FROM queries WHERE status='error'")]
        assert con.execute("SELECT enabled FROM watches").fetchone()[0] == 0
    assert len(errors) == 1 and errors[0]["error_kind"] == "past" and "ya pasó" in errors[0]["error"]
    assert schedule_row(db, key)["last_status"] == "past"


def test_a_second_instance_never_runs_jobs_while_the_first_holds_the_lease(db):
    """Two replicas on one database would double the traffic to every store: only the lease owner may tick."""
    first, _ = make(db, FakeLive(), seed=1)
    second, _ = make(db, FakeLive(), seed=2)
    key = add_watch(db, product_watch(1))
    assert first.owner != second.owner
    assert first.hold_lease(T0) is True
    first.ensure_schedules(T0)
    later = T0 + 3600  # the watch is long overdue for both of them
    assert first.hold_lease(later) is True  # the owner keeps renewing
    assert second.tick(later) == []  # not the owner: it must not touch anything
    assert second.holds_lease is False
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM queries WHERE watch_id=?", (key,)).fetchone()[0] == 0
    # the owner stops renewing: once the lease is stale the other instance takes over, and only it
    takeover = later + LEASE_TTL + 1
    assert len(second.tick(takeover)) == 1
    assert second.holds_lease is True
    assert first.tick(takeover + 1) == []
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM queries WHERE watch_id=?", (key,)).fetchone()[0] == 1
