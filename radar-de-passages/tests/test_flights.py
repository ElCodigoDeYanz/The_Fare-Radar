"""Flights from the Travelpayouts / Aviasales Data API. Canned responses only: no real call was ever made,
and these tests say so. They pin the request we send, what we accept, and how every failure is typed."""

import json
import time
import httpx
import pytest
from radar import trace
from radar.models import Criteria
from radar.providers.base import NetworkFailure, ProviderError, RateLimitedError, TimeoutFailure, TransientError
from radar.providers.registry import build_registry
from radar.providers.travelpayouts import DOMAIN, ENDPOINT, FRESHNESS_S, TravelpayoutsProvider, build_params
from radar.service import add_watch, refresh
from conftest import flight_watch

TOKEN = "tp-SECRET-token-0123456789"
NOW = 1_800_000_000.0


def criteria(**kw):
    data = {"origin": "LIM", "destination": "MAD", "departure": "2026-12-20", "currency": "USD", "max_connections": 1}
    return Criteria(**(data | kw))


def row(price=420, airline="IB", transfers=1, back=None, **kw):
    return {
        "origin": "LIM", "destination": "MAD", "price": price, "airline": airline, "flight_number": 6652,
        "departure_at": "2026-12-20T09:15:00-05:00", "return_at": None, "transfers": transfers, "return_transfers": back,
    } | kw


def payload(*rows, currency="usd", success=True):
    return {"success": success, "currency": currency, "data": list(rows)}


def api(body=None, status=200, headers=None, raises=None, **kw):
    """A provider whose HTTP client answers with `body`; every request is recorded."""
    seen = []

    def handler(request):
        seen.append(request)
        if raises is not None:
            raise raises
        content = body if isinstance(body, (str, bytes)) else json.dumps(body)
        return httpx.Response(status, content=content, headers={"content-type": "application/json"} | (headers or {}))

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return TravelpayoutsProvider(TOKEN, client=client, **kw), seen


# --- the request ---------------------------------------------------------------------------------------------
def test_request_parameters_follow_the_documented_contract():
    one_way = build_params(criteria())
    assert one_way == {
        "origin": "LIM", "destination": "MAD", "departure_at": "2026-12-20", "currency": "usd", "sorting": "price",
        "unique": "false", "limit": "30", "direct": "false", "one_way": "true",
    }
    round_trip = build_params(criteria(return_date="2027-01-05", max_connections=0))
    assert round_trip["one_way"] == "false" and round_trip["return_at"] == "2027-01-05" and round_trip["direct"] == "true"
    assert "return_at" not in one_way


def test_the_token_travels_in_a_header_never_in_the_url():
    provider, seen = api(payload(row()))
    provider.fetch(criteria(), "x", NOW)
    [request] = seen
    assert str(request.url).startswith(ENDPOINT + "?") and request.url.host == DOMAIN == provider.domain(criteria())
    assert request.headers["x-access-token"] == TOKEN
    assert TOKEN not in str(request.url) and "token" not in str(request.url).lower()
    assert request.method == "GET"


def test_the_default_client_never_follows_redirects_or_reads_proxy_environment_variables():
    provider = TravelpayoutsProvider(TOKEN)
    assert provider.client.follow_redirects is False and provider.client.trust_env is False


@pytest.mark.parametrize("token", ["", "   ", None])
def test_a_token_is_required(token):
    with pytest.raises(ValueError, match="token"):
        TravelpayoutsProvider(token)


def test_only_one_adult_in_economy_is_supported_and_nothing_is_requested_otherwise():
    provider, seen = api(payload(row()))
    for bad in (criteria(adults=2), criteria(children=1), criteria(adults=1, infants=1), criteria(cabin="BUSINESS")):
        with pytest.raises(ProviderError, match="adulto|cabina|ECONOMY"):
            provider.fetch(bad, "x", NOW)
    assert seen == []


# --- what is accepted ----------------------------------------------------------------------------------------
def test_the_cheapest_cached_fare_that_satisfies_every_filter_becomes_the_offer():
    provider, _ = api(payload(
        row(900, "LA", 1), row(380, "AV", 2),  # too many connections
        row(455, "IB", 1), row(430, "IB", 1), row(500, "UX", 0),
    ))
    [offer] = provider.fetch(criteria(), "x", NOW)
    assert offer.total_cents == 43000 and offer.currency == "USD" and offer.provider == "travelpayouts"
    assert offer.synthetic is False and offer.observed_at == NOW
    assert "caché" in offer.source and "NO verificado" in offer.source and "IB6652" in offer.source
    assert offer.expires_at == NOW + FRESHNESS_S  # a cached fare is "current" for two hours, no longer


def test_conditions_say_what_is_not_known_instead_of_assuming_it():
    provider, _ = api(payload(row()))
    [offer] = provider.fetch(criteria(), "x", NOW)
    assert offer.conditions == {
        "flight_reference": "ruta:LIM-MAD:2026-12-20:solo-ida:max1",
        "fare_reference": "mas-barata-en-cache",
        "checked_bags_per_person": "unspecified", "cabin_bags_per_person": "unspecified",
        "refundability": "unspecified", "changes": "unspecified", "cabin": "ECONOMY",
    }


def test_the_comparison_series_is_per_route_dates_and_connection_limit():
    provider, _ = api(payload(row()))
    first = provider.fetch(criteria(), "x", NOW)[0].conditions["flight_reference"]
    direct = api(payload(row(transfers=0)))[0].fetch(criteria(max_connections=0), "x", NOW)[0].conditions["flight_reference"]
    trip = api(payload(row(back=1)))[0].fetch(criteria(return_date="2027-01-05"), "x", NOW)[0].conditions["flight_reference"]
    assert len({first, direct, trip}) == 3  # different questions are never compared with each other


def test_the_same_fare_seen_twice_has_the_same_identity_and_a_new_price_a_new_one():
    provider, _ = api(payload(row(420)))
    a = provider.fetch(criteria(), "x", NOW)[0].provider_observation_id
    b = provider.fetch(criteria(), "x", NOW + 600)[0].provider_observation_id
    c = api(payload(row(410)))[0].fetch(criteria(), "x", NOW)[0].provider_observation_id
    assert a == b and a != c


def test_airline_filter_direct_only_and_return_connections():
    provider, _ = api(payload(row(300, "LA"), row(350, "IB"), row(360, "IB", 0)))
    assert provider.fetch(criteria(airlines=["IB"]), "x", NOW)[0].total_cents == 35000
    assert provider.fetch(criteria(max_connections=0), "x", NOW)[0].total_cents == 36000
    with pytest.raises(ProviderError, match="Sin tarifas"):
        provider.fetch(criteria(airlines=["AV"]), "x", NOW)
    both_ways = api(payload(row(300, back=2), row(330, back=1), row(310, back=None)))[0]
    assert both_ways.fetch(criteria(return_date="2027-01-05"), "x", NOW)[0].total_cents == 33000  # missing/over-limit return legs dropped


@pytest.mark.parametrize(
    "value,cents",
    [(420, 42000), ("99.99", 9999), (420.5, 42050), (0.01, 1), ("1e2", 10000), ("12.345", 1235)],
)
def test_prices_are_exact_decimals_in_cents(value, cents):
    provider, _ = api(payload(row(value)))
    assert provider.fetch(criteria(), "x", NOW)[0].total_cents == cents


@pytest.mark.parametrize("value", [0, -5, "0", True, None, "abc", "", [], {}, float("nan"), float("inf"), 1e30, "9" * 40])
def test_prices_that_are_not_positive_numbers_are_skipped_never_guessed(value):
    provider, _ = api(payload(row(value), row(500)))
    assert provider.fetch(criteria(), "x", NOW)[0].total_cents == 50000
    with pytest.raises(ProviderError, match="Sin tarifas"):
        api(payload(row(value)))[0].fetch(criteria(), "x", NOW)


def test_rows_that_are_not_objects_or_lack_a_connection_count_are_ignored():
    provider, _ = api(payload("junk", 7, None, row(300, transfers=None), row(310, transfers="1"), row(320)))
    assert provider.fetch(criteria(), "x", NOW)[0].total_cents == 32000


def test_expired_rows_are_dropped_and_expiry_never_outlives_the_freshness_window():
    # NOW is 2027-01-15 08:00:00 UTC
    provider, _ = api(payload(row(300, expires_at="2027-01-15T07:00:00Z"), row(400, expires_at="2027-01-15T09:00:00Z"), row(500, expires_at="not a date")))
    [offer] = provider.fetch(criteria(), "x", NOW)
    assert offer.total_cents == 40000 and offer.expires_at == NOW + 3600  # the row's own expiry, earlier than our window
    [late] = api(payload(row(400, expires_at="2099-01-01T00:00:00Z")))[0].fetch(criteria(), "x", NOW)
    assert late.expires_at == NOW + FRESHNESS_S
    [garbled] = api(payload(row(500, expires_at="not a date")))[0].fetch(criteria(), "x", NOW)
    assert garbled.expires_at == NOW + FRESHNESS_S
    with pytest.raises(ProviderError, match="Sin tarifas"):
        api(payload(row(300, expires_at="2027-01-15T07:00:00Z")))[0].fetch(criteria(), "x", NOW)


# --- failures -------------------------------------------------------------------------------------------------
def test_rate_limit_is_a_typed_block_with_a_bounded_retry_after():
    provider, _ = api("", status=429, headers={"retry-after": "120"})
    with pytest.raises(RateLimitedError) as caught:
        provider.fetch(criteria(), "x", NOW)
    assert caught.value.retry_after == 120 and caught.value.status == 429 and caught.value.url == ENDPOINT
    huge, _ = api("", status=429, headers={"retry-after": "999999999"})
    with pytest.raises(RateLimitedError) as capped:
        huge.fetch(criteria(), "x", NOW)
    assert capped.value.retry_after == 24 * 3600
    junk, _ = api("", status=429, headers={"retry-after": "tomorrow"})
    with pytest.raises(RateLimitedError) as none:
        junk.fetch(criteria(), "x", NOW)
    assert none.value.retry_after is None


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_says_so_without_ever_printing_it(status):
    provider, _ = api("denied", status=status)
    with pytest.raises(ProviderError) as caught:
        provider.fetch(criteria(), "x", NOW)
    assert "RADAR_TRAVELPAYOUTS_TOKEN" in str(caught.value) and TOKEN not in str(caught.value) and caught.value.status == status


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_transient(status):
    provider, _ = api("", status=status)
    with pytest.raises(TransientError):
        provider.fetch(criteria(), "x", NOW)


@pytest.mark.parametrize("status", [400, 404, 418])
def test_other_statuses_are_reported_not_interpreted(status):
    provider, _ = api("", status=status)
    with pytest.raises(ProviderError, match=str(status)):
        provider.fetch(criteria(), "x", NOW)


@pytest.mark.parametrize(
    "body,message",
    [
        ("<html>not json</html>", "no es JSON"),
        (json.dumps([1, 2]), "datos válidos"),
        (json.dumps({"success": False, "data": []}), "datos válidos"),
        (json.dumps({"success": True, "data": "x"}), "datos válidos"),
        (json.dumps({"success": True}), "datos válidos"),
        (json.dumps({"success": "true", "data": []}), "datos válidos"),
        (json.dumps(payload(row(), currency="eur")), "otra moneda"),
    ],
)
def test_malformed_or_foreign_answers_are_rejected(body, message):
    provider, _ = api(body)
    with pytest.raises(ProviderError, match=message):
        provider.fetch(criteria(), "x", NOW)


def test_a_price_in_another_currency_is_never_converted():
    provider, _ = api(payload(row(100), currency="PEN"))
    with pytest.raises(ProviderError, match="no se convierte"):
        provider.fetch(criteria(), "x", NOW)


def test_network_failures_are_typed_transients():
    with pytest.raises(TimeoutFailure):
        api(raises=httpx.ReadTimeout("slow"))[0].fetch(criteria(), "x", NOW)
    with pytest.raises(NetworkFailure):
        api(raises=httpx.ConnectError("down"))[0].fetch(criteria(), "x", NOW)


def test_no_fares_in_the_cache_is_a_plain_provider_error_not_a_block():
    provider, _ = api(payload())
    with pytest.raises(ProviderError, match="Sin tarifas en caché") as caught:
        provider.fetch(criteria(), "x", NOW)
    assert caught.value.error_kind == "provider"


# --- traceability and secrecy -----------------------------------------------------------------------------------
def test_every_request_is_traced_with_domain_status_and_exit_but_without_the_token():
    provider, _ = api(payload(row()))
    with trace.collect() as records:
        provider.fetch(criteria(), "x", NOW)
    [record] = records
    assert (record["domain"], record["status"], record["egress"], record["content_type"]) == (DOMAIN, 200, "direct", "application/json")
    assert record["url"] == ENDPOINT and record["bytes"] > 0 and TOKEN not in json.dumps(record)
    failing, _ = api("", status=503)
    with trace.collect() as records, pytest.raises(TransientError):
        failing.fetch(criteria(), "x", NOW)
    assert [r["status"] for r in records] == [503]
    down, _ = api(raises=httpx.ConnectError("down"))
    with trace.collect() as records, pytest.raises(NetworkFailure):
        down.fetch(criteria(), "x", NOW)
    assert [(r["status"], r["bytes"]) for r in records] == [(None, 0)]


def test_the_token_never_shows_in_repr_str_or_vars_of_the_provider():
    provider, _ = api(payload(row()))
    for text in (repr(provider), str(provider), repr(vars(provider)), repr(provider._token)):
        assert TOKEN not in text


# --- wiring ---------------------------------------------------------------------------------------------------------
def test_the_provider_exists_only_when_a_token_is_configured_and_has_its_own_budget():
    assert build_registry({"RADAR_ENABLE_DEMO": "0"}).get("travelpayouts") is None
    registry = build_registry({"RADAR_ENABLE_DEMO": "0", "RADAR_TRAVELPAYOUTS_TOKEN": TOKEN, "RADAR_BUDGET_TRAVELPAYOUTS_MONTHLY_CALLS": "5000"})
    provider = registry.get("travelpayouts")
    assert provider.kinds == ("flight",) and provider.monthly_cap == 5000 and provider.live is True
    assert registry.supports("travelpayouts", "flight") and not registry.supports("travelpayouts", "product")
    assert [p["name"] for p in registry.describe()] == ["travelpayouts"]


def test_the_removed_flight_provider_is_gone_from_the_code_and_the_configuration():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    scanned = list((root / "radar").rglob("*.py")) + list((root / "frontend").glob("*.ts")) + [root / ".env.example", root / "docker-compose.yml", root / "fly.toml"]
    for path in scanned:
        if path.exists():
            assert "duffel" not in path.read_text().lower(), path
    assert build_registry({"RADAR_DUFFEL_TOKEN": "x", "RADAR_ENABLE_DEMO": "0"}).providers == {}  # an old variable does nothing


def test_end_to_end_a_refresh_stores_the_fare_with_its_caveat_and_never_pretends_it_is_live(db):
    provider, _ = api(payload(row(420)))
    registry = build_registry({"RADAR_ENABLE_DEMO": "0", "RADAR_TRAVELPAYOUTS_TOKEN": TOKEN}, travelpayouts_client=provider.client)
    value = flight_watch().model_copy(update={"provider": "travelpayouts"})
    key = add_watch(db, value)
    result = refresh(db, key, scenario="auto", registry=registry)
    assert result["status"] == "success"
    with db.connect() as con:
        obs = dict(con.execute("SELECT * FROM observations WHERE watch_id=?", (key,)).fetchone())
    assert obs["provider"] == "travelpayouts" and obs["synthetic"] == 0 and obs["total_cents"] == 42000
    assert "NO verificado" in obs["source"] and json.loads(obs["conditions_json"])["fare_reference"] == "mas-barata-en-cache"
