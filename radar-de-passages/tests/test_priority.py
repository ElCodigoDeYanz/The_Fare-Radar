"""Alert priority tiers, product identity and cross-store comparison."""

import pytest
from conftest import FakeLive, product_watch
from radar import identity, priority, rules
from radar.compare import compare_groups
from radar.models import ProductCriteria, WatchInput
from radar.priority import Thresholds, classify, tier_for, weighted_median
from radar.service import add_watch, refresh, get_watch

NOW = 1_800_000_000.0


def with_check(body):
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body)))
    return body + str((10 - total % 10) % 10)


EAN = with_check("400638133393")  # 4006381333931


def stats(price, median=100_000, minimum=100_000, sightings=10, span=7200, availability="in_stock", count=5):
    return {
        "current_total_cents": price, "median_cents": median, "minimum_cents": minimum,
        "history_sightings": sightings, "history_span_s": span, "comparable_count": count,
        "availability": availability,
    }


# --- identity -------------------------------------------------------------------------------
def test_gtin_needs_a_valid_check_digit_and_pads_to_one_key():
    assert identity.gtin_key(EAN) == "gtin:0" + EAN
    assert identity.gtin_key("0" + EAN) == identity.gtin_key(EAN)  # GTIN-14 form is the same product
    assert identity.gtin_key(EAN[:-1] + "0") is None  # wrong check digit
    assert identity.gtin_key("123") is None and identity.gtin_key("0" * 13) is None
    assert identity.gtin_key(True) is None and identity.gtin_key(None) is None


def test_mpn_needs_brand_and_a_specific_part_number_and_gtin_wins():
    assert identity.mpn_key("Logitech", "910-005469") == "mpn:logitech|910005469"
    assert identity.mpn_key({"name": "Logitech"}, "910-005469") == "mpn:logitech|910005469"
    assert identity.mpn_key("Logitech", "A1") is None and identity.mpn_key(None, "910-005469") is None
    assert identity.match_key({"gtin": EAN, "brand": "Acme", "mpn": "ABCD1234"}) == identity.gtin_key(EAN)
    assert identity.match_key({"brand": "Acme", "mpn": "ABCD1234"}).startswith("mpn:")
    assert identity.match_key({"title": "same name"}) is None and identity.match_key(None) is None


# --- tiers ------------------------------------------------------------------------------------
@pytest.mark.parametrize("drop,tier", [(5, None), (8, "baja"), (15, "media"), (25, "alta"), (40, "maxima"), (95, "maxima")])
def test_tier_thresholds(drop, tier):
    assert tier_for(drop, Thresholds()) == tier


def test_weighted_median_follows_how_long_each_price_lasted():
    assert weighted_median([(100, 1), (50, 1), (200, 1)]) == 100
    assert weighted_median([(100, 20), (50, 1)]) == 100  # the price held for 20 checks dominates
    assert weighted_median([]) is None


def test_without_enough_history_nothing_is_extraordinary():
    out = classify(stats(40_000, sightings=3))
    assert out["tier"] is None and "Historial insuficiente" in out["reasons"][0]
    assert classify(stats(40_000, span=600))["tier"] is None


@pytest.mark.parametrize("price,tier", [(95_000, None), (90_000, "baja"), (80_000, "media"), (70_000, "alta"), (55_000, "maxima")])
def test_history_ladder(price, tier):
    assert classify(stats(price, minimum=min(price, 100_000)))["tier"] == tier


def test_top_tier_must_also_be_the_lowest_price_ever_seen():
    out = classify(stats(55_000, minimum=40_000))  # 45 % below median but a cheaper price existed
    assert out["tier"] == "alta"


def test_unconfirmed_stock_never_gets_a_priority():
    assert classify(stats(40_000, availability="out_of_stock"))["tier"] is None
    assert classify(stats(40_000, availability=None))["tier"] is None


def test_very_large_drops_are_flagged_as_probable_pricing_errors():
    out = classify(stats(20_000, minimum=20_000))
    assert out["tier"] == "maxima" and out["suspect_error"] is True and out["margin_percent"] == 400.0
    assert classify(stats(55_000, minimum=55_000))["suspect_error"] is False


def test_other_stores_alone_can_reach_alta_but_never_maxima():
    peers = [100_000, 102_000, 98_000]
    out = classify(stats(50_000, sightings=0, span=0, count=0, median=None), peers)
    assert out["tier"] == "alta" and out["reference"] == "mediana de otras tiendas"
    assert classify(stats(50_000, sightings=0, span=0, count=0, median=None), peers[:1])["tier"] is None  # one peer is not a market


def test_a_cheaper_store_downgrades_the_alert_one_level():
    out = classify(stats(70_000), [66_000, 120_000, 130_000])  # history says alta, but another store is 6 % cheaper
    assert out["tier"] == "media"
    assert classify(stats(70_000), [68_000, 120_000, 130_000])["tier"] == "alta"  # within 5 %: no downgrade


def test_threshold_ladder_comes_from_env_and_a_broken_one_falls_back():
    assert Thresholds.from_env({"RADAR_TIER_ALTA_PCT": "30"}).alta == 30
    assert Thresholds.from_env({"RADAR_TIER_BAJA_PCT": "50"}) == Thresholds()  # not ascending -> documented defaults
    assert Thresholds.from_env({"RADAR_TIER_MEDIA_PCT": "abc"}) == Thresholds()


# --- rules ----------------------------------------------------------------------------------------
def metrics(price=50_000, availability="in_stock"):
    return {"expires_at": NOW + 600, "currency": "PEN", "current_total_cents": price, "availability": availability}


def watch(mode="smart", target=10_000):
    return {"enabled": 1, "criteria": {"currency": "PEN"}, "target_cents": target, "alert_mode": mode}


def test_modes_target_smart_both():
    prio = {"tier": "alta", "reasons": ["30 % bajo la mediana histórica."]}
    none = {"tier": None, "reasons": []}
    assert rules.decide(watch("target"), metrics(), NOW, prio)[0] is False  # priority ignored in target mode
    assert rules.decide(watch("smart"), metrics(), NOW, prio)[:1] == (True,)
    assert rules.decide(watch("smart"), metrics(), NOW, none)[0] is False
    assert rules.decide(watch("both", target=60_000), metrics(), NOW, none)[2] == "objetivo"
    assert rules.decide(watch("both", target=60_000), metrics(), NOW, prio)[2] == "alta"
    assert rules.decide({"enabled": 1, "criteria": {"currency": "PEN"}, "target_cents": 60_000}, metrics(), NOW)[0] is True  # legacy watch dict


def test_cooldown_blocks_repeats_but_a_higher_priority_escalates():
    prio = {"tier": "alta", "reasons": ["x"]}
    recent = (NOW - 300, 2)  # a MEDIA alert five minutes ago
    assert rules.decide(watch(), metrics(), NOW, {"tier": "media", "reasons": ["x"]}, recent)[0] is False
    assert rules.decide(watch(), metrics(), NOW, prio, recent)[0] is True  # ALTA > MEDIA: not silenced
    assert rules.decide(watch(), metrics(), NOW, prio, (NOW - 300, 3))[0] is False
    assert rules.decide(watch(), metrics(), NOW, prio, (NOW - 3600, 3))[0] is True  # cooldown over


# --- pipeline -----------------------------------------------------------------------------------
def smart_watch(n, provider="fake", **kw):
    return WatchInput(
        criteria=ProductCriteria(url=f"https://shop.test/p/{n}", currency="PEN"),
        alert_mode="smart", provider=provider, category="tecnologia", subcategory="laptops", **kw,
    )


def test_smart_watch_needs_no_target_and_stores_a_placeholder():
    value = smart_watch(1)
    assert value.target_cents == 1
    with pytest.raises(ValueError, match="precio objetivo"):
        WatchInput(criteria=ProductCriteria(url="https://shop.test/p/9", currency="PEN"), category="tecnologia")


def test_pipeline_raises_a_maxima_alert_after_a_long_stable_history(db):
    price = {"v": 100_000}
    fake = FakeLive(lambda c, now: price["v"])
    key = add_watch(db, smart_watch(1))
    t = NOW
    for _ in range(8):  # eight confirmations of the same price over >1 hour
        refresh(db, key, provider=fake, now=t)
        t += 600
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
    price["v"] = 50_000
    out = refresh(db, key, provider=fake, now=t)
    assert out["observations"][0]["priority"] == "maxima"
    with db.connect() as con:
        alert = dict(con.execute("SELECT * FROM alerts").fetchone())
    assert alert["priority"] == "maxima" and alert["priority_rank"] == 4
    assert "PRIORIDAD MÁXIMA" in alert["message"] and "margen bruto" in alert["message"] and "mediana histórica" in alert["message"]
    price["v"] = 49_000  # a slightly lower price right after: inside the cooldown, same tier -> no second alert
    refresh(db, key, provider=fake, now=t + 700)
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1


def test_cross_store_matching_peers_and_comparison(db):
    ids = {"gtin": EAN, "title": "Laptop X"}
    shops = {"a.test": 100_000, "b.test": 102_000, "c.test": 98_000, "d.test": 50_000}
    keys = {}
    for i, (domain, cents) in enumerate(shops.items()):
        fake = FakeLive(lambda c, now, cents=cents: cents, domain=domain, ids=ids)
        value = WatchInput(criteria=ProductCriteria(url=f"https://{domain}/p/1", currency="PEN"), alert_mode="smart", provider="fake", category="tecnologia", subcategory="laptops")
        keys[domain] = add_watch(db, value)
        out = refresh(db, keys[domain], provider=fake, now=NOW + i * 10)
    assert get_watch(db, keys["a.test"])["match_key"] == identity.gtin_key(EAN)
    assert get_watch(db, keys["a.test"])["title"] == "Laptop X"
    # d.test was added last: three peers (median 100 000) -> its 50 000 is 50 % lower -> ALTA from other stores alone
    assert out["observations"][0]["priority"] == "alta"
    groups = compare_groups(db, NOW + 60)
    assert len(groups) == 1
    group = groups[0]
    assert [s["domain"] for s in group["stores"]] == ["d.test", "c.test", "a.test", "b.test"]
    assert group["cheapest"] == "d.test" and group["spread_percent"] == 104.0 and group["saving_cents"] == 52_000


def test_comparison_never_mixes_currencies_conditions_or_unavailable_prices(db):
    ids = {"gtin": EAN}
    for domain, cents, avail in (("a.test", 100_000, "in_stock"), ("b.test", 60_000, "out_of_stock")):
        fake = FakeLive(lambda c, now, cents=cents: cents, domain=domain, ids=ids, availability=avail)
        key = add_watch(db, WatchInput(criteria=ProductCriteria(url=f"https://{domain}/p/1", currency="PEN"), alert_mode="smart", provider="fake", category="tecnologia"))
        refresh(db, key, provider=fake, now=NOW)
    [group] = compare_groups(db, NOW + 60)
    assert group["ranked_count"] == 1 and group["cheapest"] is None and group["spread_percent"] is None  # out of stock is listed, never ranked
    # a USD listing of the same GTIN is a different group, not a cheaper store
    fake = FakeLive(lambda c, now: 10_000, domain="c.test", ids=ids)
    key = add_watch(db, WatchInput(criteria=ProductCriteria(url="https://c.test/p/1", currency="USD"), alert_mode="smart", provider="fake", category="tecnologia"))
    refresh(db, key, provider=fake, now=NOW)
    assert all(len(g["stores"]) >= 2 for g in compare_groups(db, NOW + 60))
