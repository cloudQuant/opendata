"""Futures daily bars fetcher (domain ``futures_daily``, contract ``Bar``).

Wraps the fuyao (THS) futures daily K-line endpoint through
:mod:`opendata_fuyao`. Futures prices are observed trade levels, so there is
no adjustment basis and the adapter rejects a caller that asks for one.

Unlike the A-share chain this adapter returns the qualified ``thscode``
(``IF2610.CFE``) in ``Bar.symbol``; the source mapping normalizes it to the
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
    resolve_futures_code,
)


class ThsFuturesDailyQuery(QueryParams):
    """Validated query for the futures daily bars domain."""

    symbol: str


class ThsFuturesDailyFetcher(Fetcher[ThsFuturesDailyQuery, tuple[Bar, ...]]):
    """Daily OHLCV bars for one futures contract (or commodity index).

    ``verified`` is true because the window semantics, field mapping and
    ordering were checked against the live endpoint and cross-compared with
    an independent futures series (see ``docs/evidence/C6``).
    """

    capability: ClassVar[Capability] = Capability(
        asset_class="futures",
        domain="futures_daily",
        period="1D",
        market="cn",
        source=SOURCE,
        verified=True,
    )

    def transform_query(self, **kwargs: object) -> ThsFuturesDailyQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required, ``IF2610`` or ``IF2610.CFE``),
                ``start_date``, ``end_date``.

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields (``adjust`` is one of them -
                contract prices have no adjustment basis).
        """
        return ThsFuturesDailyQuery.model_validate(kwargs)

    def extract_data(self, params: ThsFuturesDailyQuery, ctx: FetchContext) -> tuple[Bar, ...]:
        """Fetch the upstream window (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            Contract rows for the requested window.

        Raises:
            ThsProviderError: Credentials missing or the code is not exactly
                one contract in the upstream futures catalog.
        """
        from opendata_fuyao.endpoints import (
            FUTURES_PRICES_ENDPOINT,
            fetch_period_daily_bars,
        )

        start = params.start_date or date(1990, 1, 1)
        end = params.end_date or datetime.now(timezone.utc).date()
        # 中台窗口为半开 [start, end)；调用方的 end_date 是闭区间日，故 +1 天。
        window_end = date.fromordinal(end.toordinal() + 1)
        with client(timeout_seconds=ctx.timeout) as active:
            code = resolve_futures_code(active, params.symbol)
            return fetch_period_daily_bars(
                active, endpoint=FUTURES_PRICES_ENDPOINT, symbol=code, start=start, end=window_end
            )

    def transform_data(self, raw: tuple[Bar, ...], params: ThsFuturesDailyQuery) -> tuple[Bar, ...]:
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
