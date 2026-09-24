"""Option daily bars fetcher (domain ``option_daily``, contract ``Bar``).

Wraps the fuyao (THS) options daily K-line endpoint through
:mod:`opendata_fuyao`. Option premiums are observed trade levels, so there is
no adjustment basis and the adapter rejects a caller that asks for one.

Option contracts are addressed by their qualified ``thscode`` only (see
:func:`~opendata.data.providers.ths.models._client.resolve_option_code`);
that code is what ``Bar.symbol`` carries, and the source mapping
normalizes it for the warehouse view.
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
    resolve_option_code,
)


class ThsOptionDailyQuery(QueryParams):
    """Validated query for the option daily bars domain."""

    symbol: str


class ThsOptionDailyFetcher(Fetcher[ThsOptionDailyQuery, tuple[Bar, ...]]):
    """Daily OHLCV bars for one listed option contract.

    ``verified`` is true because the window semantics, field mapping and
    ordering were checked against the live endpoint and cross-compared with
    an independent option series (see ``docs/evidence/C6``).
    """

    capability: ClassVar[Capability] = Capability(
        asset_class="option",
        domain="option_daily",
        period="1D",
        market="cn",
        source=SOURCE,
        verified=True,
    )

    def transform_query(self, **kwargs: object) -> ThsOptionDailyQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required, qualified such as
                ``90007464.SZ`` or ``IO2601-C-4000.CFE``), ``start_date``,
                ``end_date``.

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields (``adjust`` is one of them -
                premium prices have no adjustment basis).
        """
        return ThsOptionDailyQuery.model_validate(kwargs)

    def extract_data(self, params: ThsOptionDailyQuery, ctx: FetchContext) -> tuple[Bar, ...]:
        """Fetch the upstream window (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            Contract rows for the requested window.

        Raises:
            ThsProviderError: Credentials missing, or the code is a bare
                contract code that this endpoint cannot resolve.
        """
        from opendata_fuyao.endpoints import (
            OPTIONS_PRICES_ENDPOINT,
            fetch_period_daily_bars,
        )

        start = params.start_date or date(1990, 1, 1)
        end = params.end_date or datetime.now(timezone.utc).date()
        # 中台窗口为半开 [start, end)；调用方的 end_date 是闭区间日，故 +1 天。
        window_end = date.fromordinal(end.toordinal() + 1)
        with client(timeout_seconds=ctx.timeout) as active:
            code = resolve_option_code(active, params.symbol)
            return fetch_period_daily_bars(
                active, endpoint=OPTIONS_PRICES_ENDPOINT, symbol=code, start=start, end=window_end
            )

    def transform_data(self, raw: tuple[Bar, ...], params: ThsOptionDailyQuery) -> tuple[Bar, ...]:
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
