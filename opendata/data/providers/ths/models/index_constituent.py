"""Index constituent fetcher (domain ``index_constituent``, contract ``IndexConstituent``).

Wraps the fuyao (THS) index constituent endpoint through
:mod:`opendata_fuyao`. The endpoint serves the **current** membership list of
one index only - there is no window parameter - so a query that asks for a
date range fails closed here rather than silently returning today's list for
someone who wanted a past rebalance.

Unlike the bar domains, this adapter stores plain six-digit codes on both
sides of the row (``000300`` / ``600519``): membership is a join key against
the other constituents source (the CSI close-weight chain does the same), and
a suffixed spelling would put the two sources in different rows instead of
two observations of one fact. The qualified code is still what goes upstream
(``000300.SH`` and ``399300.SZ`` are two spellings of 沪深 300; which one a
caller used is visible in the request, not in the stored row).
"""

from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.models import IndexConstituent
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.ths._source import SOURCE
from opendata.data.providers.ths.models._client import (
    ThsProviderError,
    client,
    resolve_index_code,
)


class ThsIndexConstituentQuery(QueryParams):
    """Validated query for the index constituent domain."""

    symbol: str


class ThsIndexConstituentFetcher(Fetcher[ThsIndexConstituentQuery, tuple[IndexConstituent, ...]]):
    """Current membership list for one index (SSE/SZSE/CSI/THS board).

    ``verified`` is true because the row identity and the membership set were
    checked against the live endpoint and cross-compared with the index
    publisher's own list (see ``docs/evidence/C9``). ``weight`` is always
    ``None``: this endpoint does not publish weights.

    The contract's ``as_of`` names the snapshot a list belongs to. This endpoint
    publishes no such date - its only timestamp is the request moment - so the
    rows carry the observation day and say so in ``notes``. That is a declared
    gap, not a borrowed date: C34 measured the consequence, and on indexes the
    publisher reconstitutes every quarter the list behind this one and the list
    behind the CSI weight file differ by an equal number of members in each
    direction.
    """

    capability: ClassVar[Capability] = Capability(
        asset_class="index",
        domain="index_constituent",
        period="snapshot",
        market="cn",
        source=SOURCE,
        verified=True,
        notes="as_of is the observation day; this endpoint publishes no list date",
    )

    def transform_query(self, **kwargs: object) -> ThsIndexConstituentQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required, ``000300`` or ``000300.SH``).
                ``start_date`` / ``end_date`` are rejected.

        Returns:
            The validated query.

        Raises:
            ThsProviderError: A date range was requested; the endpoint only
                serves the current list.
            ValidationError: On unknown fields.
        """
        query = ThsIndexConstituentQuery.model_validate(kwargs)
        if query.start_date is not None or query.end_date is not None:
            raise ThsProviderError("THS_CONSTITUENTS_SNAPSHOT_ONLY")
        return query

    def extract_data(
        self, params: ThsIndexConstituentQuery, ctx: FetchContext
    ) -> tuple[IndexConstituent, ...]:
        """Fetch the current constituent list (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            Contract rows carrying the qualified index code the call used.

        Raises:
            ThsProviderError: Credentials missing or the code is not exactly
                one instrument in the upstream index universe.
        """
        from opendata_fuyao.endpoints import fetch_index_constituents

        with client(timeout_seconds=ctx.timeout) as active:
            code = resolve_index_code(active, params.symbol)
            return fetch_index_constituents(active, symbol=code)

    def transform_data(
        self, raw: tuple[IndexConstituent, ...], params: ThsIndexConstituentQuery
    ) -> FetchResult:
        """Re-spell the rows with plain codes (the normalize stage).

        Args:
            raw: Rows from the extraction stage, ordered by member code.
            params: The validated query.

        Returns:
            The same rows with ``index_symbol`` stripped to the plain index
            code (member ``symbol`` is already plain upstream).

        Raises:
            ThsProviderError: The response carries no rows for the query.
        """
        if not raw:
            raise ThsProviderError("THS_EMPTY_RESPONSE")
        index_code = raw[0].index_symbol.partition(".")[0]
        return tuple(
            IndexConstituent(
                index_symbol=index_code,
                symbol=row.symbol,
                as_of=row.as_of,
                weight=row.weight,
            )
            for row in raw
        )
