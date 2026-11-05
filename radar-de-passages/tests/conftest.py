"""Shared helpers: a controllable live provider and builders. No network is ever used."""

from datetime import date, timedelta
import hashlib
import pytest
from radar.database import Database
from radar.models import BusCriteria, Criteria, ProductCriteria, WatchInput
from radar.providers.base import Offer
from radar.providers.registry import Registry
from radar.providers.demo import DemoProvider


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "radar.db")


class FakeLive:
    """A 'live' provider whose behaviour the test controls. Records every request time."""

    name = "fake"
    kinds = ("product", "flight", "bus")
    live = True
    min_interval = 1
    monthly_cap = None

    def __init__(self, behaviour=None, domain="shop.test", ids=None, availability="in_stock"):
        self.behaviour = behaviour  # callable(criteria, now) -> price cents, or raises
        self.domain_name = domain
        self.ids, self.availability = ids, availability
        self.calls = []

    def domain(self, criteria):
        return self.domain_name

    def fetch(self, criteria, scenario, now):
        self.calls.append(now)
        cents = self.behaviour(criteria, now) if self.behaviour else 100_000
        conditions = (
            {"product_ref": criteria.url, "seller": "s", "condition": "new", "availability": self.availability}
            if criteria.kind == "product"
            else {}
        )
        return [
            Offer(
                "fake",
                hashlib.sha256(f"{criteria.canonical()}{cents}".encode()).hexdigest(),
                now,
                now + 1200,
                cents,
                criteria.currency,
                criteria,
                conditions,
                "fake live source",
                False,
                self.ids,
            )
        ]


def registry_with(*providers, demo=True):
    items = list(providers)
    if demo:
        items.insert(0, DemoProvider())
    return Registry(items)


def product_watch(n=1, provider="fake", target=50_000, **kw):
    return WatchInput(
        criteria=ProductCriteria(url=f"https://shop.test/p/{n}", currency="PEN"),
        target_cents=target,
        provider=provider,
        category="tecnologia",
        subcategory="laptops",
        **kw,
    )


def flight_watch(**kw):
    data = {
        "origin": "LIM",
        "destination": "MAD",
        "departure": (date.today() + timedelta(days=45)).isoformat(),
    }
    data.update(kw)
    return WatchInput(criteria=Criteria(**data), target_cents=35000)


def bus_watch(**kw):
    data = {
        "origin": "Lima",
        "destination": "Arequipa",
        "departure": (date.today() + timedelta(days=30)).isoformat(),
    }
    data.update(kw)
    return WatchInput(criteria=BusCriteria(**data), target_cents=7000)
