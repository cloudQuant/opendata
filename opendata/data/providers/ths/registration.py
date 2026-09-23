"""Capability registration for the fuyao (THS) source (A3.4).

Declares the fetchers that a live call has verified and registers them into
the process registry. Domains whose upstream endpoints are not implemented
yet (financial statements/indicators, index constituents) are deliberately
absent: routing them to a half-checked adapter would be worse than leaving
them to the akshare provider until A3.4 continues.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opendata.data.providers.ths.models.stock_action import ThsStockActionFetcher
from opendata.data.providers.ths.models.stock_daily import ThsStockDailyFetcher

if TYPE_CHECKING:
    from opendata.data.capability import Capability
    from opendata.data.protocol import Fetcher
    from opendata.data.registry import ProviderRegistry

#: The verified fuyao fetchers, one per registered domain (A3.4 scope).
FETCHERS: tuple[Fetcher[Any, Any], ...] = (
    ThsStockDailyFetcher(),
    ThsStockActionFetcher(),
)


def register(registry: ProviderRegistry | None = None) -> list[Capability]:
    """Register the fuyao fetchers into the registry, idempotently.

    Capabilities are keyed by (domain, source); a capability already present
    under that pair is skipped, so repeated calls (app restart in the same
    process, tests) never raise on duplicates.

    Args:
        registry: Target registry; defaults to the process-wide singleton.

    Returns:
        The capabilities newly registered by this call.
    """
    if registry is None:
        from opendata.data.registry import get_registry

        registry = get_registry()
    existing = {(capability.domain, capability.source) for capability in registry.capabilities()}
    registered: list[Capability] = []
    for fetcher in FETCHERS:
        capability = fetcher.capability
        if (capability.domain, capability.source) in existing:
            continue
        registry.register(fetcher)
        registered.append(capability)
    return registered


__all__ = ["FETCHERS", "register"]
