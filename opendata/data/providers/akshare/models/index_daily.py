"""Index daily bars fetcher (domain ``index_daily``, contract ``Bar``).

Wraps ``opendata_http.index_zh_a_hist`` (eastmoney China index daily
klines). B1.2 registration: ``verified=false`` keeps the capability out
of ``source=auto`` routing until the AC-6 P1 fidelity sampling lands.

Unit conversion follows design §8.2: upstream ``成交量`` is in lots
(手), the contract stores share units, so ``normalize()`` multiplies by
100 - the same rule the A-share daily chain applies.
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import Bar
from opendata.data.protocol import FetchContext, FetchResult, Fetcher, QueryParams
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.providers.akshare.models._normalize import as_date, plain_symbol

#: Columns the eastmoney index frame must carry to be normalizable.
REQUIRED_COLUMNS = ("日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额")


class IndexDailyQuery(QueryParams):
    """Validated query for the index daily bars domain."""

    symbol: str


class AkshareIndexDailyFetcher(Fetcher[IndexDailyQuery, pd.DataFrame]):
    """Daily OHLCV bars for one China stock index (e.g. ``000300``)."""

    capability: ClassVar[Capability] = Capability(
        asset_class="index",
        domain="index_daily",
        period="1D",
        market="cn",
        source=SOURCE,
        verified=False,  # B1.2: P1 sampling pending, explicit source only
    )

    def transform_query(self, **kwargs: object) -> IndexDailyQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required, e.g. ``000300``).

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields.
        """
        return IndexDailyQuery.model_validate(kwargs)

    def extract_data(self, params: IndexDailyQuery, ctx: FetchContext) -> pd.DataFrame:
        """Fetch eastmoney index daily klines (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            The upstream frame (Chinese columns, volume in lots).
        """
        import opendata_http  # lazy: load the ported tree on routing only

        return opendata_http.index_zh_a_hist(
            symbol=plain_symbol(params.symbol),
            period="daily",
            start_date=params.start_date.strftime("%Y%m%d") if params.start_date else "19700101",
            end_date=params.end_date.strftime("%Y%m%d") if params.end_date else "20500101",
        )

    def transform_data(self, raw: pd.DataFrame, params: IndexDailyQuery) -> FetchResult:
        """Normalize the index frame into ``Bar`` rows (the normalize stage).

        Args:
            raw: Upstream frame with :data:`REQUIRED_COLUMNS`.
            params: Validated query (symbol fallback).

        Returns:
            One ``Bar`` per trading day, ascending; rows without a
            parseable date are dropped.
        """
        frame = raw.dropna(subset=[name for name in REQUIRED_COLUMNS if name in raw.columns])
        bars: list[Bar] = []
        for record in frame.to_dict("records"):
            trade_date = as_date(record.get("日期"))
            if trade_date is None:
                continue
            bars.append(
                Bar(
                    symbol=plain_symbol(params.symbol),
                    trade_date=trade_date,
                    open=float(record["开盘"]),
                    high=float(record["最高"]),
                    low=float(record["最低"]),
                    close=float(record["收盘"]),
                    volume=float(record["成交量"]) * 100,
                    amount=float(record["成交额"]),
                )
            )
        return bars
