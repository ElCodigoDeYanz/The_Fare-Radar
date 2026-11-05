"""Flights from the Travelpayouts / Aviasales Data API (`prices_for_dates`): an authorised data
source instead of scraping airline sites.

Read this before trusting a price from here:
  * It is a CACHE of searches real users made on Aviasales (kept up to 7 days), not a live fare
    check. A price can be gone by the time you look. Every offer says so in its source text, and
    the alert message tells you to verify on the airline before anything else.
  * It is the cheapest cached fare for the route and dates ("route price index"). Baggage, refund
    and change rules are not published, so they are stored as "unspecified" (comparable with
    themselves, never assumed to be included).
  * Access needs a token you request from Travelpayouts (RADAR_TRAVELPAYOUTS_TOKEN). Whether it is
    free for you, its rate limits and its terms of use are for YOU to confirm in their agreement;
    the documentation page consulted on 01-11-2026 does not state them. Set
    RADAR_BUDGET_TRAVELPAYOUTS_MONTHLY_CALLS to cap usage.
  * The contract (endpoint, parameters, fields) was taken from their public documentation. This
    code has been tested only against canned responses; no real call has been made.
Only 1 adult in economy is supported (the API prices one ticket and does not publish cabin).
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from datetime import datetime, timezone
import hashlib
import httpx
from .. import restrictions, trace
from ..security import Secret
from .base import Offer, ProviderError, RateLimitedError, TransientError

ENDPOINT = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
DOMAIN = "api.travelpayouts.com"
FRESHNESS_S = 7200  # a cached fare is shown as current for two hours after we saw it
MAX_CENTS = 10**11  # anything above one billion units of currency is not a fare (and would overflow the database)


def build_params(criteria):
    params = {
        "origin": criteria.origin,
        "destination": criteria.destination,
        "departure_at": criteria.departure,
        "currency": criteria.currency.lower(),
        "sorting": "price",
        "unique": "false",
        "limit": "30",
        "direct": "true" if criteria.max_connections == 0 else "false",
        "one_way": "false" if criteria.return_date else "true",
    }
    if criteria.return_date:
        params["return_at"] = criteria.return_date
    return params


def _cents(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():  # NaN / Infinity are legal Python floats but never a fare
            return None
        cents = int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return cents if 0 < cents <= MAX_CENTS else None


def _expiry(value, now):
    if not isinstance(value, str):
        return now + FRESHNESS_S
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return min(now + FRESHNESS_S, when.timestamp())
    except ValueError:
        return now + FRESHNESS_S


class TravelpayoutsProvider:
    name = "travelpayouts"
    kinds = ("flight",)
    live = True
    min_interval = 300
    monthly_cap = None

    def __init__(self, token, *, client=None, timeout=15):
        if not token or not token.strip():
            raise ValueError("Se requiere un token de Travelpayouts.")
        self._token = Secret(token.strip())
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)

    def domain(self, criteria):
        return DOMAIN

    def fetch(self, criteria, scenario, now):
        if criteria.adults != 1 or criteria.children or criteria.infants:
            raise ProviderError("Esta fuente precia un solo adulto; crea el seguimiento con 1 adulto.")
        if criteria.cabin != "ECONOMY":
            raise ProviderError("Esta fuente no publica la cabina; solo se admite ECONOMY.")
        import time as _time

        started = _time.monotonic()
        try:
            response = self.client.get(
                ENDPOINT,
                params=build_params(criteria),
                headers={"X-Access-Token": self._token.reveal(), "Accept-Encoding": "gzip, deflate"},
            )
        except httpx.HTTPError as exc:
            trace.add(DOMAIN, ENDPOINT, None, _time.monotonic() - started, 0, "direct")
            raise restrictions.classify_exception(exc) from exc
        trace.add(DOMAIN, ENDPOINT, response.status_code, _time.monotonic() - started, len(response.content), "direct", "application/json")
        status = response.status_code
        if status == 429:
            raise RateLimitedError("Travelpayouts limitó las consultas (HTTP 429).", _retry_after(response)).with_evidence(status, ENDPOINT)
        if status in (401, 403):
            raise ProviderError(
                f"Travelpayouts rechazó el token (HTTP {status}); revisa RADAR_TRAVELPAYOUTS_TOKEN."
            ).with_evidence(status, ENDPOINT)
        if status >= 500:
            raise TransientError(f"Travelpayouts respondió HTTP {status}.").with_evidence(status, ENDPOINT)
        if status != 200:
            raise ProviderError(f"Respuesta inesperada de Travelpayouts (HTTP {status}).").with_evidence(status, ENDPOINT)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("Travelpayouts devolvió algo que no es JSON.") from exc
        if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(payload.get("data"), list):
            raise ProviderError("Travelpayouts no devolvió datos válidos.")
        returned = str(payload.get("currency", criteria.currency)).upper()
        if returned != criteria.currency:
            raise ProviderError("Travelpayouts devolvió otra moneda; no se convierte ninguna.")

        wanted = set(criteria.airlines)
        candidates = []
        for item in payload["data"]:
            if not isinstance(item, dict):
                continue
            cents = _cents(item.get("price"))
            if cents is None:
                continue
            if wanted and item.get("airline") not in wanted:
                continue
            transfers = item.get("transfers")
            back = item.get("return_transfers")
            if not isinstance(transfers, int) or transfers > criteria.max_connections:
                continue
            if criteria.return_date and (not isinstance(back, int) or back > criteria.max_connections):
                continue
            expires = _expiry(item.get("expires_at"), now)
            if expires <= now:
                continue
            candidates.append((cents, expires, item))
        if not candidates:
            raise ProviderError("Sin tarifas en caché para esa ruta, fechas y filtros.")
        cents, expires, item = min(candidates, key=lambda c: c[0])
        flight = f"{item.get('airline', '??')}{item.get('flight_number', '')}"
        conditions = {
            # One series per route + dates + connection limit: "cheapest cached fare".
            "flight_reference": f"ruta:{criteria.origin}-{criteria.destination}:{criteria.departure}:{criteria.return_date or 'solo-ida'}:max{criteria.max_connections}",
            "fare_reference": "mas-barata-en-cache",
            "checked_bags_per_person": "unspecified",
            "cabin_bags_per_person": "unspecified",
            "refundability": "unspecified",
            "changes": "unspecified",
            "cabin": "ECONOMY",
        }
        identity = hashlib.sha256(
            "|".join([self.name, criteria.canonical(), str(cents), flight, str(item.get("departure_at")), str(item.get("return_at"))]).encode()
        ).hexdigest()
        return [
            Offer(
                self.name, identity, now, expires, cents, criteria.currency, criteria, conditions,
                f"Aviasales (caché de búsquedas, NO verificado en la aerolínea): {flight}, "
                f"{item.get('transfers')} escala(s) ida, sale {item.get('departure_at')}",
                False,
            )
        ]


def _retry_after(response):
    value = response.headers.get("retry-after", "")
    return min(int(value), 24 * 3600) if value.strip().isdigit() else None
