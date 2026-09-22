"""Stock daily bars fetcher (domain ``stock_daily``, contract ``Bar``).

Wraps ``opendata_http.stock_zh_a_hist`` (eastmoney daily klines).
Unit conversion: upstream ``成交量`` is in lots (手), the contract
stores shares, so ``normalize()`` multiplies by 100 (design §8.2).
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import Bar
from opendata.data.protocol import FetchContext, FetchResult, Fetcher, QueryParams
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.providers.akshare.models._normalize import as_date, plain_symbol


class StockDailyQuery(QueryParams):
    """Validated query for the stock daily bars domain."""

    symbol: str
    adjust: str = ""


class AkshareStockDailyFetcher(Fetcher[StockDailyQuery, pd.DataFrame]):
    """Daily OHLCV bars for one A-share symbol (unadjusted by default)."""

    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="stock_daily",
        period="1D",
        market="cn",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> StockDailyQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required), ``start_date``,
                ``end_date``, ``adjust`` in ``{"", "qfq", "hfq"}``.

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields or an invalid adjust.
        """
        return StockDailyQuery.model_validate(kwargs)

    def extract_data(
        self, params: StockDailyQuery, ctx: FetchContext
    ) -> pd.DataFrame:
        """Fetch eastmoney daily klines (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            The upstream frame (Chinese columns, volume in lots).
        """
        import opendata_http  # lazy: load the ported tree on routing only

        return opendata_http.stock_zh_a_hist(
            symbol=plain_symbol(params.symbol),
            period="daily",
            start_date=params.start_date.strftime("%Y%m%d") if params.start_date else "19700101",
            end_date=params.end_date.strftime("%Y%m%d") if params.end_date else "20500101",
            adjust=params.adjust,
            timeout=ctx.timeout,
        )

    def transform_data(
        self, raw: pd.DataFrame, params: StockDailyQuery
    ) -> FetchResult:
        """Normalize klines into ``Bar`` rows (the normalize stage).

        Args:
            raw: Upstream frame with the columns ``日期`` /
                ``股票代码`` / ``开盘`` / ``收盘`` / ``最高`` /
                ``最低`` / ``成交量`` (手) / ``成交额``.
            params: The validated query (symbol fallback).

        Returns:
            ``Bar`` rows; rows with missing prices are dropped.
        """
        required = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]
        frame = raw.dropna(subset=[name for name in required if name in raw.columns])
        bars: list[Bar] = []
        for record in frame.to_dict("records"):
            trade_date = as_date(record.get("日期"))
            if trade_date is None:
                continue
            code = record.get("股票代码") or plain_symbol(params.symbol)
            bars.append(
                Bar(
                    symbol=plain_symbol(str(code)),
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
