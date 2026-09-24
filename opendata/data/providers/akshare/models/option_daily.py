"""Option daily bars fetcher (domain ``option_daily``, contract ``Bar``).

Wraps ``opendata_http.option_sse_daily_sina`` (sina SSE stock-option
daily line). B1.2 registration: ``verified=false`` keeps the capability
out of ``source=auto`` routing until the AC-6 P1 fidelity sampling lands.

The source publishes no turnover column, so ``amount`` is 0.0 - an
honest absence rather than a fabricated value (same shape as the
futures daily chain). ``volume`` stays in contracts (张): it is not a
lot-denominated share count, so the §8.2 手->股 rule does not apply.
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import Bar
from opendata.data.protocol import FetchContext, FetchResult, Fetcher, QueryParams
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.providers.akshare.models._normalize import as_date, plain_symbol

#: Columns the sina option frame must carry to be normalizable.
REQUIRED_COLUMNS = ("日期", "开盘", "最高", "最低", "收盘", "成交量")


class OptionDailyQuery(QueryParams):
    """Validated query for the option daily bars domain."""

    symbol: str


class AkshareOptionDailyFetcher(Fetcher[OptionDailyQuery, pd.DataFrame]):
    """Daily OHLCV bars for one SSE stock-option contract."""

    capability: ClassVar[Capability] = Capability(
        asset_class="option",
        domain="option_daily",
        period="1D",
        market="cn",
        source=SOURCE,
        verified=False,  # B1.2: P1 sampling pending, explicit source only
    )

    def transform_query(self, **kwargs: object) -> OptionDailyQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required, e.g. ``10003889``).

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields.
        """
        return OptionDailyQuery.model_validate(kwargs)

    def extract_data(self, params: OptionDailyQuery, ctx: FetchContext) -> pd.DataFrame:
        """Fetch the sina option daily line (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            The upstream frame.
        """
        import opendata_http  # lazy: load the ported tree on routing only

        return opendata_http.option_sse_daily_sina(symbol=plain_symbol(params.symbol))

    def transform_data(self, raw: pd.DataFrame, params: OptionDailyQuery) -> FetchResult:
        """Normalize the option frame into ``Bar`` rows (the normalize stage).

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
                    volume=float(record["成交量"]),
                    amount=0.0,
                )
            )
        return sorted(bars, key=lambda bar: bar.trade_date)
