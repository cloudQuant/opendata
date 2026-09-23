"""Capability registration for the yfinance source (C1 P0).

One fetcher per implemented domain; the SDK isolation lives in
``models/_sdk.py`` so registration never imports the optional dependency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opendata.data.providers.yfinance.models.stock_daily import YfinanceStockDailyFetcher

if TYPE_CHECKING:
    from opendata.data.capability import Capability
    from opendata.data.protocol import Fetcher
    from opendata.data.registry import ProviderRegistry

#: The implemented yfinance fetchers.
FETCHERS: tuple[Fetcher[Any, Any], ...] = (YfinanceStockDailyFetcher(),)


def register(registry: ProviderRegistry | None = None) -> list[Capability]:
    """Register the yfinance fetchers into the registry, idempotently.

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
