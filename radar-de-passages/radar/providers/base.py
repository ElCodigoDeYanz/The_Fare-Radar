"""Provider contract and typed errors. No statistics, alert decisions or persistence."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Offer:
    provider: str
    provider_observation_id: str
    observed_at: float
    expires_at: float
    total_cents: int
    currency: str
    criteria: object
    conditions: dict
    source: str
    synthetic: bool
    # Optional identifiers (gtin, mpn, brand, title) used to match the same product across stores.
    product_ids: dict | None = None


class ProviderError(RuntimeError):
    """The source did not give a usable price. error_kind feeds the query journal.
    status/url are the HTTP evidence of the incident (recorded, never retried around)."""

    error_kind = "provider"
    status = None
    url = None

    def with_evidence(self, status=None, url=None):
        self.status, self.url = status, url
        return self


class TransientError(ProviderError):
    """Timeout, 5xx or network failure. Worth retrying later with backoff."""

    error_kind = "transient"


class TimeoutFailure(TransientError):
    error_kind = "timeout"


class NetworkFailure(TransientError):
    error_kind = "network"


class BlockedError(ProviderError):
    """The site refused us (403, captcha, challenge). The scheduler pauses the whole
    domain with exponential backoff. It never tries to get around the block, and it never
    switches to another network exit to keep going."""

    error_kind = "blocked"

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimitedError(BlockedError):
    """HTTP 429 or an explicit quota message."""

    error_kind = "rate_limited"


class CaptchaError(BlockedError):
    """A human-verification page. The monitor never tries to solve it."""

    error_kind = "captcha"


class AccessDeniedError(BlockedError):
    """An 'access denied' page or an explicit prohibition (robots.txt does not count)."""

    error_kind = "denied"


class SessionError(BlockedError):
    """401, or a redirect to a login/verification page: the source wants an identity."""

    error_kind = "session"


class UnexpectedRedirectError(BlockedError):
    """A redirect that leaves the allowed host or goes to a login/captcha/blocked path."""

    error_kind = "redirect"


class StructureError(ProviderError):
    """A normal page without the price data we read: the site's structure probably changed
    (or the price is drawn with JavaScript). Needs a human, not a retry storm."""

    error_kind = "structure"


class EmptyResponseError(ProviderError):
    """200 but empty or truncated. Retried with backoff, a limited number of times."""

    error_kind = "empty"


class EgressError(ProviderError):
    """The network exit assigned to this domain is switched off or misconfigured."""

    error_kind = "egress"


class GoneError(ProviderError):
    """404/410: the page no longer exists."""

    error_kind = "gone"


class BudgetExhausted(ProviderError):
    """Monthly call cap reached for a metered provider."""

    error_kind = "budget"


class Provider(Protocol):
    name: str
    kinds: tuple
    live: bool  # False for synthetic sources: no politeness gate is applied
    min_interval: float  # minimum seconds between two queries of the same watch
    monthly_cap: int | None

    def domain(self, criteria) -> str: ...

    def fetch(self, criteria, scenario: str, now: float) -> list[Offer]: ...
