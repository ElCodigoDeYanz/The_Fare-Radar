"""Statistics only; comparisons require the same fully specified conditions."""

from decimal import Decimal
import hashlib
import json
from .priority import weighted_median

REQUIRED_BY_KIND = {
    "flight": (
        "flight_reference",
        "fare_reference",
        "checked_bags_per_person",
        "cabin_bags_per_person",
        "refundability",
        "changes",
        "cabin",
    ),
    "bus": ("trip_reference", "operator", "fare_class", "refundability"),
    # "unspecified" is an explicit value meaning "the page does not state it"; it is
    # comparable with itself but never equal to "new". A missing key stays None.
    "product": ("product_ref", "seller", "condition", "availability"),
}
REQUIRED = REQUIRED_BY_KIND["flight"]


def comparison_key(criteria_json, conditions, provider, kind="flight"):
    if any(conditions.get(k) is None for k in REQUIRED_BY_KIND[kind]):
        return None
    return hashlib.sha256(
        (
            provider
            + criteria_json
            + json.dumps(conditions, sort_keys=True, separators=(",", ":"))
        ).encode()
    ).hexdigest()


def summarize(current, previous, last_alert_at=None, now=None):
    conditions = json.loads(current["conditions_json"]) if "conditions_json" in current else {}
    context = {
        # "n/a" for travel; products must be confirmed in stock before any alert.
        "availability": conditions.get("availability", "n/a"),
        "current_total_cents": current["total_cents"],
        "currency": current["currency"],
        "expires_at": current["expires_at"],
        "seconds_since_last_alert": None
        if last_alert_at is None or now is None
        else now - last_alert_at,
    }
    if current["comparison_key"] is None:
        return context | {
            "comparable_count": 0,
            "minimum_cents": None,
            "average_cents": None,
            "median_cents": None,
            "history_sightings": 0,
            "history_span_s": 0,
            "change_percent": None,
            "note": "Condiciones incompletas: no se compara como tarifa equivalente.",
        }
    rows = [
        r
        for r in previous
        if r["comparison_key"] == current["comparison_key"] and r["id"] != current["id"]
    ]
    prices = [r["total_cents"] for r in rows]
    previous_price = rows[0]["total_cents"] if rows else None
    change = (
        round(
            float(
                (Decimal(current["total_cents"]) - Decimal(previous_price))
                / Decimal(previous_price)
                * 100
            ),
            2,
        )
        if previous_price
        else None
    )
    # "seen" = how many checks confirmed that price: a proxy for how long it was in force.
    weighted = [(r["total_cents"], max(1, r.get("seen", 1))) for r in rows]
    span = (now - min(r["observed_at"] for r in rows)) if rows and now is not None else 0
    return context | {
        "comparable_count": len(rows),
        "minimum_cents": min(prices) if prices else None,
        "average_cents": round(sum(prices) / len(prices), 2) if prices else None,
        "median_cents": weighted_median(weighted),
        "history_sightings": sum(w for _, w in weighted),
        "history_span_s": max(0, span),
        "change_percent": change,
        "note": "Comparación solo con observaciones de iguales pasajeros, fechas, moneda, itinerario y condiciones conocidas."
        if rows
        else "Sin observación previa con condiciones equivalentes.",
    }
