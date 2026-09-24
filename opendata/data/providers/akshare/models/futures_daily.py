"""Futures daily bars fetcher (domain ``futures_daily``, contract ``Bar``).

Wraps ``opendata_http.futures_zh_daily_sina`` (Sina commodity futures
daily). B1.2 registration: the capability carries ``verified=false`` so
``source=auto`` never routes to it until the fidelity replay (AC-6 P1
sampling) lands - the function is reachable via an explicit source
only, which is the reviewed semantics for unverified capabilities.

The Sina frame has no turnover column (``date/open/high/low/close/
volume/hold/settle``), so ``amount`` is 0.0 - an honest absence, the
same shape the migrated legacy A-share data has; the contract field
must stay present for the model but carries no fabricated value.
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import Bar
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.providers.akshare.models._normalize import as_date, plain_symbol


class FuturesDailyQuery(QueryParams):
    """Validated query for the futures daily bars domain."""

    symbol: str


class AkshareFuturesDailyFetcher(Fetcher[FuturesDailyQuery, pd.DataFrame]):
    """Daily OHLCV bars for one commodity-futures main contract."""

    capability: ClassVar[Capability] = Capability(
        asset_class="futures",
        domain="futures_daily",
        period="1D",
        market="cn",
        source=SOURCE,
        verified=False,  # B1.2: P1 sampling pending, explicit source only
    )

    def transform_query(self, **kwargs: object) -> FuturesDailyQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required, e.g. ``RB0``).

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields.
        """
        return FuturesDailyQuery.model_validate(kwargs)

    def extract_data(self, params: FuturesDailyQuery, ctx: FetchContext) -> pd.DataFrame:
        """Fetch Sina commodity futures daily data (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            The upstream frame.
        """
        import opendata_http  # lazy: load the ported tree on routing only

        return opendata_http.futures_zh_daily_sina(symbol=plain_symbol(params.symbol))

    def transform_data(self, raw: pd.DataFrame, params: FuturesDailyQuery) -> FetchResult:
        """Normalize the Sina frame into ``Bar`` rows (the normalize stage).

        Args:
            raw: Upstream frame (``date/open/high/low/close/volume/
                hold/settle``).
            params: Validated query.

        Returns:
            One ``Bar`` per trading day. ``amount`` is 0.0 (the source
            publishes no turnover); ``hold``/``settle`` are dropped as
            the contract does not carry them.
        """
        if raw is None or raw.empty:
            return ()
        records = []
        for row in raw.to_dict("records"):
            trade_date = as_date(row.get("date"))
            if trade_date is None:
                continue
            records.append(
                Bar(
                    symbol=plain_symbol(params.symbol),
                    trade_date=trade_date,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    amount=0.0,
                )
            )
        ordered = sorted(records, key=lambda bar: bar.trade_date)
        return tuple(ordered)
