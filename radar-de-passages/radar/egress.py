"""Network exits (egress). Which connection a request leaves from is a configuration decision,
recorded on every request, and never a reaction to a block.

  * 'direct' is this server's own IP: the default, and what you want in almost every case.
  * Other profiles are authorised proxies YOU contracted (RADAR_EGRESS_<NAME>_URL). The URL holds
    credentials, so it is wrapped in Secret and never logged.
  * Each domain (or domain suffix) is assigned to ONE profile. Same domain, same exit, always:
    no sessions broken by hopping, and no rotation to dodge a limit.
  * A block pauses the DOMAIN (radar.scheduler). It does not move the domain to another exit.
  * A disabled profile refuses its domains; it never silently falls back to 'direct'
    (that would leak the IP you meant to keep separate and break the assignment).

Environment:
  RADAR_EGRESS_PROFILES=direct,proxy_a           names ('direct' always exists)
  RADAR_EGRESS_PROXY_A_URL=http://user:pass@host:port
  RADAR_EGRESS_PROXY_A_ENABLED=1                 set 0 to switch one provider off
  RADAR_EGRESS_MAP=shop.pe=proxy_a,*.cl=proxy_a  domain or *.suffix -> profile
  RADAR_EGRESS_DEFAULT=direct
"""

import httpx
from .security import Secret, strip_credentials

DIRECT = "direct"


class EgressDisabled(RuntimeError):
    """The profile assigned to a domain is switched off or has no URL."""


class Egress:
    def __init__(self, name, url=None, enabled=True):
        self.name = name
        self.url = Secret(url) if url else None
        self.enabled = enabled

    def public(self):
        return {
            "name": self.name,
            "kind": "direct" if self.name == DIRECT else "proxy",
            "enabled": self.enabled,
            "endpoint": None
            if self.url is None
            else strip_credentials(self.url.reveal()).split("//", 1)[-1],
        }


class EgressManager:
    def __init__(self, profiles=None, mapping=None, default=DIRECT, client_factory=None):
        self.profiles = {DIRECT: Egress(DIRECT)}
        for profile in profiles or []:
            self.profiles[profile.name] = profile
        self.mapping = dict(mapping or {})
        self.default = default if default in self.profiles else DIRECT
        self._clients = {}
        self._factory = client_factory or self._make_client

    @classmethod
    def from_env(cls, env, client_factory=None):
        names = [n.strip().lower() for n in env.get("RADAR_EGRESS_PROFILES", "").split(",") if n.strip()]
        profiles = []
        for name in names:
            if name == DIRECT:
                continue
            key = name.upper().replace("-", "_")
            profiles.append(
                Egress(
                    name,
                    env.get(f"RADAR_EGRESS_{key}_URL", "").strip() or None,
                    env.get(f"RADAR_EGRESS_{key}_ENABLED", "1") != "0",
                )
            )
        mapping = {}
        for item in env.get("RADAR_EGRESS_MAP", "").split(","):
            if "=" in item:
                domain, _, profile = item.partition("=")
                mapping[domain.strip().lower()] = profile.strip().lower()
        manager = cls(profiles, mapping, env.get("RADAR_EGRESS_DEFAULT", DIRECT).strip().lower(), client_factory)
        unknown = sorted(set(mapping.values()) - set(manager.profiles))
        if unknown:
            raise RuntimeError(f"RADAR_EGRESS_MAP usa perfiles no declarados: {', '.join(unknown)}.")
        return manager

    # --- assignment -----------------------------------------------------------------
    def profile_for(self, domain):
        domain = (domain or "").lower()
        if domain in self.mapping:
            return self.profiles[self.mapping[domain]]
        for pattern, name in self.mapping.items():
            if pattern.startswith("*.") and (domain == pattern[2:] or domain.endswith(pattern[1:])):
                return self.profiles[name]
        return self.profiles[self.default]

    def client_for(self, domain):
        """(profile name, httpx client). Raises EgressDisabled instead of falling back."""
        profile = self.profile_for(domain)
        if not profile.enabled:
            raise EgressDisabled(
                f"La salida de red «{profile.name}» asignada a {domain} está desactivada; "
                "no se usa otra en su lugar."
            )
        if profile.name != DIRECT and profile.url is None:
            raise EgressDisabled(f"La salida de red «{profile.name}» no tiene URL configurada.")
        if profile.name not in self._clients:
            self._clients[profile.name] = self._factory(profile)
        return profile.name, self._clients[profile.name]

    @staticmethod
    def _make_client(profile):
        kwargs = {"timeout": 15, "follow_redirects": False, "trust_env": False}
        if profile.url is not None:
            kwargs["proxy"] = profile.url.reveal()
        return httpx.Client(**kwargs)

    # --- administration ---------------------------------------------------------------
    def set_enabled(self, name, enabled):
        if name not in self.profiles:
            raise KeyError(name)
        self.profiles[name].enabled = bool(enabled)
        if not enabled:
            client = self._clients.pop(name, None)
            if client is not None:
                client.close()

    def describe(self):
        return [p.public() for p in self.profiles.values()]

    def close(self):
        for client in self._clients.values():
            client.close()
        self._clients.clear()


class SingleClient(EgressManager):
    """Wraps one ready-made httpx client as the 'direct' exit (tests, simple deployments)."""

    def __init__(self, client):
        super().__init__()
        self._clients[DIRECT] = client
