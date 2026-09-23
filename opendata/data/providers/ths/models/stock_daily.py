"""Stock daily bars fetcher (domain ``stock_daily``, contract ``Bar``).

Wraps the fuyao (THS) historical price endpoint through
:mod:`opendata_fuyao`. The upstream already returns the contract's numeric
shape (shares, CNY) and the adapter keeps only the unadjusted basis (D10), so
the normalize stage passes the contract rows through after validating that the
response covers the requested symbol.
"""

from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.models import Bar
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.providers.ths._source import SOURCE
from opendata.data.providers.ths.models._client import client, resolve_code


class StockDailyQuery(QueryParams):
    """Validated query for the stock daily bars domain."""

    symbol: str
    adjust: str = ""


class ThsStockDailyFetcher(Fetcher[StockDailyQuery, tuple[Bar, ...]]):
    """Daily OHLCV bars for one A-share symbol (unadjusted, D10).

    ``verified`` is true because the adapter's window semantics, field
    mapping and ordering were checked against the live API (A3.2/A3.4 live
    cases), which is what the flag means for a first-party client.
    """

    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="stock_daily",
        period="1D",
        market="cn",
        source=SOURCE,
        verified=True,
    )

    def transform_query(self, **kwargs: object) -> StockDailyQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required), ``start_date``, ``end_date``,
                ``adjust`` in ``{"", "unadjusted", "qfq", "hfq"}``.

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields or an invalid adjust.
        """
        return StockDailyQuery.model_validate(kwargs)

    def extract_data(self, params: StockDailyQuery, ctx: FetchContext) -> tuple[Bar, ...]:
        """Fetch the upstream window (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            Contract rows for the requested window.

        Raises:
            ThsProviderError: Credentials missing or the symbol is not
                resolvable to exactly one upstream code.
        """
        from datetime import date, datetime, timezone

        from opendata_fuyao.endpoints import fetch_daily_bars

        start = params.start_date or date(1990, 1, 1)
        end = params.end_date or datetime.now(timezone.utc).date()
        # 中台窗口为半开 [start, end)；调用方的 end_date 是闭区间日，故 +1 天。
        window_end = date.fromordinal(end.toordinal() + 1)
        with client(timeout_seconds=ctx.timeout) as active:
            code = resolve_code(active, params.symbol)
            return fetch_daily_bars(
                active, symbol=code, start=start, end=window_end, adjust="unadjusted"
            )

    def transform_data(self, raw: tuple[Bar, ...], params: StockDailyQuery) -> tuple[Bar, ...]:
        """Validate and return the contract rows (the normalize stage).

        Args:
            raw: Rows from the extraction stage.
            params: The validated query.

        Returns:
            The rows, ordered by trading date.

        Raises:
            ThsProviderError: The response carries no rows for the query.
        """
        from opendata.data.providers.ths.models._client import ThsProviderError

        if not raw:
            raise ThsProviderError("THS_EMPTY_RESPONSE")
        if params.adjust not in {"", "unadjusted"}:
            # D10: bars stay unadjusted; adjusted series come from local factors.
            raise ThsProviderError("THS_ADJUST_UNSUPPORTED")
        return tuple(sorted(raw, key=lambda bar: bar.trade_date))
