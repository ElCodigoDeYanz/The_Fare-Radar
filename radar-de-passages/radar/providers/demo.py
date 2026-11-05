"""Synthetic fixture provider for flights, buses and products. Never real data."""

import hashlib
import json
from pathlib import Path
from .base import BlockedError, Offer, ProviderError

FIXTURE = Path(__file__).resolve().parents[2] / "demo/offers.json"
SOURCE = "Synthetic fixture: demo/offers.json"
SCENARIOS = (
    "baseline",
    "drop",
    "failure",
    "expired",
    "different_conditions",
    "missing_conditions",
    "out_of_stock",
    "blocked",
    "auto",
)


class DemoProvider:
    name = "demo"
    kinds = ("flight", "bus", "product")
    live = False
    min_interval = 5  # synthetic limit only; not a real provider quota
    monthly_cap = None

    def __init__(self):
        self.fixture = json.loads(FIXTURE.read_text())

    def domain(self, criteria):
        return "demo.local"

    def _table(self, criteria):
        kind = criteria.kind
        if kind == "flight":
            return self.fixture["price_per_traveler_cents"], criteria.travelers
        if kind == "bus":
            return self.fixture["bus"]["price_per_passenger_cents"], criteria.travelers
        return self.fixture["product"]["price_cents"], 1

    def _conditions(self, criteria, scenario):
        kind = criteria.kind
        if kind == "flight":
            conditions = dict(self.fixture["conditions"])
            conditions["cabin"] = criteria.cabin
            if scenario == "different_conditions":
                conditions["checked_bags_per_person"] = 1
                conditions["fare_reference"] = "SYNTH-OTHER-FARE"
            if scenario == "missing_conditions":
                conditions["checked_bags_per_person"] = None
                conditions["fare_reference"] = None
            return conditions
        if kind == "bus":
            conditions = dict(self.fixture["bus"]["conditions"])
            if scenario == "different_conditions":
                conditions["fare_class"] = "synthetic-cama"
            if scenario == "missing_conditions":
                conditions["fare_class"] = None
            return conditions
        conditions = dict(self.fixture["product"]["conditions"])
        conditions["product_ref"] = criteria.url
        if scenario == "different_conditions":
            conditions["seller"] = "synthetic-other-seller"
        if scenario == "missing_conditions":
            conditions["seller"] = None
        if scenario == "out_of_stock":
            conditions["availability"] = "out_of_stock"
        return conditions

    def fetch(self, criteria, scenario, now):
        if scenario == "failure":
            raise ProviderError(
                "Fallo de proveedor simulado. No se recibió un precio actualizado."
            )
        if scenario == "blocked":
            raise BlockedError("Bloqueo simulado (HTTP 429). Se pausa el dominio.")
        table, units = self._table(criteria)
        if scenario == "out_of_stock" and criteria.kind != "product":
            raise ProviderError("Escenario no admitido para este tipo de seguimiento.")
        if scenario not in table and scenario != "auto":
            raise ProviderError("Escenario no admitido.")
        conditions = self._conditions(criteria, scenario)
        bucket = int(now) // 900
        digest = hashlib.sha256(
            (criteria.canonical() + str(bucket) + scenario).encode()
        ).hexdigest()
        if scenario == "auto":
            # Deterministic wander between 70% and 110% of baseline, one value per
            # 15-minute bucket, so a running scheduler shows moving synthetic prices.
            factor = 0.70 + (int(digest[:8], 16) % 41) / 100
            unit = round(table["baseline"] * factor)
        else:
            unit = table[scenario]
        expires = now - 1 if scenario == "expired" else (bucket + 1) * 900
        return [
            Offer(
                self.fixture["provider"],
                digest,
                now,
                expires,
                unit * units,
                criteria.currency,
                criteria,
                conditions,
                SOURCE,
                True,
            )
        ]
