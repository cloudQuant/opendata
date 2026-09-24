"""Index daily bars fetcher (domain ``index_daily``, contract ``Bar``).

Wraps the fuyao (THS) index historical K-line endpoint through
:mod:`opendata_fuyao`. The response shape is identical to the A-share bar
item; indices simply have no adjustment semantics, so the request carries no
``adjust`` and the adapter rejects a caller that asks for one.

Unlike :class:`~opendata.data.providers.akshare.models.index_daily.AkshareIndexDailyFetcher`
this adapter returns the qualified ``thscode`` in ``Bar.symbol`` (the stock
chain does the same); the source mapping normalizes it to the plain
contract spelling for the warehouse view.
"""

from datetime import date, datetime, timezone
from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.models import Bar
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.providers.ths._source import SOURCE
from opendata.data.providers.ths.models._client import (
    ThsProviderError,
    client,
    resolve_index_code,
)


class ThsIndexDailyQuery(QueryParams):
    """Validated query for the index daily bars domain."""

    symbol: str


class ThsIndexDailyFetcher(Fetcher[ThsIndexDailyQuery, tuple[Bar, ...]]):
    """Daily OHLCV bars for one index (SSE/SZSE/CSI/THS).

    ``verified`` is true because the window semantics, field mapping and
    ordering were checked against the live endpoint and cross-compared with
    an independent index series (see ``docs/evidence/C5``).
    """

    capability: ClassVar[Capability] = Capability(
        asset_class="index",
        domain="index_daily",
        period="1D",
        market="cn",
        source=SOURCE,
        verified=True,
    )

    def transform_query(self, **kwargs: object) -> ThsIndexDailyQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required, ``000300`` or ``000300.SH``),
                ``start_date``, ``end_date``.

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields (``adjust`` is one of them -
                indices have no adjustment basis).
        """
        return ThsIndexDailyQuery.model_validate(kwargs)

    def extract_data(self, params: ThsIndexDailyQuery, ctx: FetchContext) -> tuple[Bar, ...]:
        """Fetch the upstream window (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            Contract rows for the requested window.

        Raises:
            ThsProviderError: Credentials missing or the code is not exactly
                one instrument in the upstream index universe.
        """
        from opendata_fuyao.endpoints import fetch_index_daily_bars

        start = params.start_date or date(1990, 1, 1)
        end = params.end_date or datetime.now(timezone.utc).date()
        # 中台窗口为半开 [start, end)；调用方的 end_date 是闭区间日，故 +1 天。
        window_end = date.fromordinal(end.toordinal() + 1)
        with client(timeout_seconds=ctx.timeout) as active:
            code = resolve_index_code(active, params.symbol)
            return fetch_index_daily_bars(active, symbol=code, start=start, end=window_end)

    def transform_data(self, raw: tuple[Bar, ...], params: ThsIndexDailyQuery) -> tuple[Bar, ...]:
        """Validate and return the contract rows (the normalize stage).

        Args:
            raw: Rows from the extraction stage.
            params: The validated query.

        Returns:
            The rows, ordered by trading date.

        Raises:
            ThsProviderError: The response carries no rows for the query.
        """
        if not raw:
            raise ThsProviderError("THS_EMPTY_RESPONSE")
        return tuple(sorted(raw, key=lambda bar: bar.trade_date))
