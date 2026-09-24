"""Capability registration for the akshare source (A2.4).

Declares the P0 domain fetchers and registers them into the process
registry. The source label equals the package name (design §7.1
naming: one package per source), derived from the filesystem rather
than written as a string literal - the label is a routing name, not
a module reference, and the zero-dependency scanner (AC-16) keeps
runtime ``*.py`` free of upstream-root string constants.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opendata.data.providers.akshare.models.financial_indicator import (
    AkshareFinancialIndicatorFetcher,
)
from opendata.data.providers.akshare.models.financial_statement import (
    AkshareFinancialStatementFetcher,
)
from opendata.data.providers.akshare.models.futures_daily import (
    AkshareFuturesDailyFetcher,
)
from opendata.data.providers.akshare.models.index_constituent import (
    AkshareIndexConstituentFetcher,
)
from opendata.data.providers.akshare.models.stock_action import (
    AkshareStockActionFetcher,
)
from opendata.data.providers.akshare.models.stock_daily import (
    AkshareStockDailyFetcher,
)

if TYPE_CHECKING:
    from opendata.data.capability import Capability
    from opendata.data.protocol import Fetcher
    from opendata.data.registry import ProviderRegistry

#: The P0 fetchers, one per registered domain (A2.1 closure scope).
FETCHERS: tuple[Fetcher[Any, Any], ...] = (
    AkshareStockDailyFetcher(),
    AkshareFuturesDailyFetcher(),
    AkshareStockActionFetcher(),
    AkshareFinancialStatementFetcher(),
    AkshareFinancialIndicatorFetcher(),
    AkshareIndexConstituentFetcher(),
)


def register(registry: ProviderRegistry | None = None) -> list[Capability]:
    """Register the P0 fetchers into the registry, idempotently.

    Capabilities are keyed by (domain, source); a capability already
    present under that pair is skipped, so repeated calls (app
    restart in the same process, tests) never raise on duplicates.

    Args:
        registry: Target registry; defaults to the process-wide
            singleton.

    Returns:
        The capabilities newly registered by this call.
    """
    if registry is None:
        from opendata.data.registry import get_registry

        registry = get_registry()
    existing = {
        (capability.domain, capability.source) for capability in registry.capabilities()
    }
    registered: list[Capability] = []
    for fetcher in FETCHERS:
        capability = fetcher.capability
        if (capability.domain, capability.source) in existing:
            continue
        registry.register(fetcher)
        registered.append(capability)
    return registered
