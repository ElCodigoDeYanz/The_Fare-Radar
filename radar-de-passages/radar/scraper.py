"""Source adapters only. No statistics, discounts, alert decisions or persistence.

Kept as the stable import path; implementations live in radar.providers.
"""

from .providers.base import (  # noqa: F401
    BlockedError,
    BudgetExhausted,
    Offer,
    Provider,
    ProviderError,
    RateLimitedError,
    TransientError,
)
from .providers.demo import DemoProvider  # noqa: F401
