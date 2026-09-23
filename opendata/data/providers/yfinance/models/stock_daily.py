"""Overseas daily bars fetcher (domain ``stock_daily_overseas``, contract ``OverseasBar``).

Wraps the yfinance SDK (an **optional** dependency: importing this provider
package works without it, and routing to it fails closed with a stable code
when the SDK is absent - FR-7's isolation rule). The upstream returns OHLCV
in shares/quote-currency and no turnover, which is why the domain's contract
carries ``amount`` as nullable instead of faking zeros into the shared
``Bar`` used by the A-share sources.

Clean-room note (design §1.3): the implementation is self-written; only the
upstream SDK's public interface is referenced, no OpenBB code was consulted.
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import OverseasBar
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.yfinance._source import SOURCE


class StockDailyOverseasQuery(QueryParams):
    """Validated query for the overseas daily bars domain."""

    symbol: str


class YfinanceStockDailyFetcher(Fetcher[StockDailyOverseasQuery, pd.DataFrame]):
    """Daily OHLCV bars for one overseas symbol (unadjusted, D10).

    ``verified`` stays false until the adapter's output has been compared
    against the upstream's official values (R2: implement first, verify when
    the comparison fixtures are recorded), so auto routing skips it for now.
    """

    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="stock_daily_overseas",
        period="1D",
        market="global",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> StockDailyOverseasQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required, upstream ticker such as ``AAPL``),
                ``start_date``, ``end_date``.

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields or a blank symbol.
        """
        return StockDailyOverseasQuery.model_validate(kwargs)

    def extract_data(self, params: StockDailyOverseasQuery, ctx: FetchContext) -> pd.DataFrame:
        """Fetch the upstream window (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            The upstream frame (auto-adjusted OHLCV, ``Volume`` in shares).

        Raises:
            YfinanceProviderError: The SDK is not installed, or the upstream
                returned no rows for the request.
        """
        from opendata.data.providers.yfinance.models._sdk import require_history_frame

        frame = require_history_frame(
            params.symbol,
            start=params.start_date,
            end=params.end_date,
            timeout=ctx.timeout,
        )
        if frame is None or frame.empty:
            raise YfinanceProviderError("YFINANCE_EMPTY_RESPONSE")
        return frame

    def transform_data(self, raw: pd.DataFrame, params: StockDailyOverseasQuery) -> FetchResult:
        """Normalize the upstream frame (the normalize stage).

        Maps the SDK's lowercase OHLCV columns onto ``OverseasBar`` rows;
        ``amount`` stays ``None`` because the upstream provides no turnover
        (it is nullable on this contract by design).

        Args:
            raw: Upstream frame.
            params: The validated query.

        Returns:
            Contract rows sorted by trading date.

        Raises:
            YfinanceProviderError: A required upstream column is missing.
        """
        missing = [
            name for name in ("Open", "High", "Low", "Close", "Volume") if name not in raw.columns
        ]
        if missing:
            raise YfinanceProviderError("YFINANCE_COLUMNS_MISSING")
        frame = raw.reset_index().rename(
            columns={
                "Date": "trade_date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )
        frame["symbol"] = params.symbol.strip().upper()
        frame["amount"] = None
        rows = OverseasBar.from_frame(frame)
        return tuple(sorted(rows, key=lambda bar: bar.trade_date))


class YfinanceProviderError(RuntimeError):
    """Stable failures of the yfinance provider adapter."""

    def __init__(self, code: str) -> None:
        """Store the stable failure code (and use it as the message).

        Args:
            code: One of the provider's stable codes, for example
                ``YFINANCE_SDK_MISSING``.
        """
        self.code = code
        super().__init__(code)
