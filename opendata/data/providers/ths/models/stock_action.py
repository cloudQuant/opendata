"""Corporate action fetcher (domain ``stock_action``, contract ``CorporateAction``).

Wraps the fuyao adjustment-factor endpoint. The upstream does not label event
types: ``dividend_per_share`` is the cash dividend, ``per_share_bonus`` the
stock dividend and the allotment columns map onto the rights fields, which is
what the contract carries.
"""

from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.models import CorporateAction
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.providers.ths._source import SOURCE
from opendata.data.providers.ths.models._client import ThsProviderError, client, resolve_code


class StockActionQuery(QueryParams):
    """Validated query for the corporate action domain."""

    symbol: str


class ThsStockActionFetcher(Fetcher[StockActionQuery, tuple[CorporateAction, ...]]):
    """Ex-rights/ex-dividend events for one A-share symbol.

    ``verified`` is true because the adapter's event mapping was checked
    against the live endpoint (A3.2/A3.4 live cases).
    """

    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="stock_action",
        period="1D",
        market="cn",
        source=SOURCE,
        verified=True,
    )

    def transform_query(self, **kwargs: object) -> StockActionQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required), optional ``start_date`` /
                ``end_date`` bounding the event window.

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields.
        """
        return StockActionQuery.model_validate(kwargs)

    def extract_data(
        self, params: StockActionQuery, ctx: FetchContext
    ) -> tuple[CorporateAction, ...]:
        """Fetch the upstream event stream (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            Contract events for the requested window.

        Raises:
            ThsProviderError: Credentials missing or the symbol is not
                resolvable to exactly one upstream code.
        """
        from opendata_fuyao.endpoints import fetch_adjustment_factors

        with client(timeout_seconds=ctx.timeout) as active:
            code = resolve_code(active, params.symbol)
            return fetch_adjustment_factors(
                active, symbol=code, start=params.start_date, end=params.end_date
            )

    def transform_data(
        self, raw: tuple[CorporateAction, ...], params: StockActionQuery
    ) -> tuple[CorporateAction, ...]:
        """Validate and return the contract events (the normalize stage).

        Args:
            raw: Events from the extraction stage.
            params: The validated query.

        Returns:
            The events, newest ex-date first.

        Raises:
            ThsProviderError: The response carries no events for the symbol.
        """
        if not raw:
            raise ThsProviderError("THS_EMPTY_RESPONSE")
        return tuple(sorted(raw, key=lambda event: event.ex_date, reverse=True))
