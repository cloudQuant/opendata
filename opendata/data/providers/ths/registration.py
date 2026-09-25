"""Capability registration for the fuyao (THS) source (A3.4).

Declares the fetchers that a live call has verified and registers them into
the process registry. Domains whose upstream endpoints are not verified yet
(``financial_indicator``) are deliberately absent: routing them to a
half-checked adapter would be worse than leaving them to the akshare provider
until the cross-vendor comparison lands. ``fund_etf_daily`` spent C6 through
C19 on that list for a reason the mapping table records: the upstream ETF
K-line endpoint publishes a forward-adjusted series and its ``adjust``
parameter is inert, which D10 forbids storing. The route out of that was the
``fund_action`` leg (C15), and the conversion itself is now measured rather
than assumed - C20 reconciled the published series against sina and Tencent
over 10,608 price cells (2,652 per fund across four funds) and pinned the
boundary to "distributions strictly after the bar" on live ex-dates - so the
leg is registered and the domain has two verified sources. It is still not
the domain's authority: its coverage is a rolling ~5 years, so ``akshare``
answers "since inception" and ths answers "recently", in that order. The
``trading_calendar`` leg is the producer side of A4.7: the expectation
predicate in :mod:`opendata.pipeline.trading_calendar`
reads the warehouse table these rows land in, so registering the fetcher is what
makes the ``warehouse-calendar`` tier reachable at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opendata.data.providers.ths.models.financial_statement import (
    ThsFinancialStatementFetcher,
)
from opendata.data.providers.ths.models.fund_action import ThsFundActionFetcher
from opendata.data.providers.ths.models.fund_etf_daily import ThsFundEtfDailyFetcher
from opendata.data.providers.ths.models.futures_daily import ThsFuturesDailyFetcher
from opendata.data.providers.ths.models.index_constituent import ThsIndexConstituentFetcher
from opendata.data.providers.ths.models.index_daily import ThsIndexDailyFetcher
from opendata.data.providers.ths.models.instrument import ThsInstrumentFetcher
from opendata.data.providers.ths.models.option_daily import ThsOptionDailyFetcher
from opendata.data.providers.ths.models.stock_action import ThsStockActionFetcher
from opendata.data.providers.ths.models.stock_daily import ThsStockDailyFetcher
from opendata.data.providers.ths.models.trading_calendar import ThsTradingCalendarFetcher

if TYPE_CHECKING:
    from opendata.data.capability import Capability
    from opendata.data.protocol import Fetcher
    from opendata.data.registry import ProviderRegistry

#: The verified fuyao fetchers, one per registered domain (A3.4 scope).
FETCHERS: tuple[Fetcher[Any, Any], ...] = (
    ThsStockDailyFetcher(),
    ThsStockActionFetcher(),
    ThsIndexDailyFetcher(),
    ThsIndexConstituentFetcher(),
    ThsFinancialStatementFetcher(),
    ThsFundActionFetcher(),
    ThsFundEtfDailyFetcher(),
    ThsInstrumentFetcher(),
    ThsFuturesDailyFetcher(),
    ThsOptionDailyFetcher(),
    ThsTradingCalendarFetcher(),
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
