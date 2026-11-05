"""Which providers exist in THIS deployment. Everything is opt-in through the environment."""

import os
from .. import sources as source_catalog
from ..egress import EgressManager
from .demo import DemoProvider
from .storepage import StorePageProvider
from .travelpayouts import TravelpayoutsProvider


class Registry:
    def __init__(self, providers):
        self.providers = {p.name: p for p in providers}

    def get(self, name):
        return self.providers.get(name)

    def supports(self, name, kind):
        provider = self.providers.get(name)
        return provider is not None and kind in provider.kinds

    def describe(self):
        return [
            {
                "name": p.name,
                "kinds": list(p.kinds),
                "live": p.live,
                "monthly_cap": p.monthly_cap,
            }
            for p in self.providers.values()
        ]

    @property
    def any_live(self):
        return any(p.live for p in self.providers.values())

    def live_for(self, kind):
        return any(p.live and kind in p.kinds for p in self.providers.values())


def _cap(env, name):
    raw = env.get(f"RADAR_BUDGET_{name.upper()}_MONTHLY_CALLS", "").strip()
    return int(raw) if raw.isdigit() else None


def enabled_sources(env):
    """Stores of the shipped catalogue the operator switched on (RADAR_ENABLED_SOURCES)."""
    spec = env.get("RADAR_ENABLED_SOURCES", "").strip()
    return source_catalog.select(source_catalog.load_sources(), spec) if spec else []


def build_registry(env=None, *, store_client=None, renderer=None, travelpayouts_client=None):
    env = os.environ if env is None else env
    providers = []
    if env.get("RADAR_ENABLE_DEMO", "1") != "0":
        providers.append(DemoProvider())
    domains = [d for d in env.get("RADAR_ALLOWED_STORE_DOMAINS", "").split(",") if d.strip()]
    domains += source_catalog.allowed_domains(enabled_sources(env))
    if domains:
        render_domains = [d for d in env.get("RADAR_RENDER_DOMAINS", "").split(",") if d.strip()]
        if render_domains and renderer is None:
            from .rendered import PlaywrightRenderer  # optional dependency, imported only when asked for

            renderer = PlaywrightRenderer()
        egress = None if store_client is not None else EgressManager.from_env(env)
        store = StorePageProvider(
            domains,
            contact=env.get("RADAR_CONTACT", ""),
            client=store_client,
            egress=egress,
            renderer=renderer,
            render_domains=render_domains,
        )
        store.monthly_cap = _cap(env, "store")
        providers.append(store)
    token = env.get("RADAR_TRAVELPAYOUTS_TOKEN", "").strip()
    if token:
        flights = TravelpayoutsProvider(token, client=travelpayouts_client)
        flights.monthly_cap = _cap(env, "travelpayouts")
        providers.append(flights)
    return Registry(providers)
