"""Instrument catalog fetcher (domain ``instrument``, contract ``Instrument``).

Wraps ``/api/meta/tickers/list`` through :mod:`opendata_fuyao`. This is the
metadata backbone the other ths legs hang on: ``list_date`` / ``delist_date``
say whether a window a bar query returned nothing for is a window the symbol
did not exist in (the predicate C11 §6.3 says is needed to tell a
crawl-blocked empty frame apart from a genuinely empty window).

Two measured constraints shape the adapter (2026-09-25, ``docs/evidence/C14``):

* **one asset class per call.** The filter is required here, not upstream:
  the unfiltered catalog is every leaf type at once, and ``options`` alone
  exceeds the 10,000-row per-call cap, so an unfiltered refresh would page
  through hundreds of thousands of opaque contract codes nobody joins on.
* **it is a snapshot, not a history.** ``status`` is derived against the
  snapshot date the envelope carries (``data.timestamp`` = the daily catalog
  load), so a date range cannot be served - a caller asking for 2024 would
  get today's listing while believing it asked for the past. Fail closed
  instead, exactly like the constituent snapshot leg.

The rows are returned with the qualified ``thscode`` (``000001.SZ``), not the
plain ``ticker`` the bar domains store: plain codes collide across asset
classes (``000001`` is a stock, an index and a fund), and this table's job is
to be the thing those classes are reconciled against.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from opendata.data.capability import Capability
from opendata.data.models import Instrument
from opendata.data.protocol import Fetcher, QueryParams
from opendata.data.providers.ths._source import SOURCE
from opendata.data.providers.ths.models._client import ThsProviderError, client

if TYPE_CHECKING:
    from opendata.data.protocol import FetchContext

#: Documented ``asset_type`` values (``https://fuyao.aicubes.cn/llms-full.txt``,
#: 标的列表获取). Each row carries exactly one of these leaf types.
ASSET_TYPES = frozenset(
    {
        "a-share",
        "a-share-index",
        "fund-otc",
        "fund-etf",
        "fund-lof",
        "fund-reits",
        "forex",
        "futures",
        "futures-commodity-index",
        "options",
    }
)

#: Rows asked for per call - the upstream maximum, so a small catalog is one call.
PAGE_LIMIT = 10_000

#: Page ceiling before the loop is declared stuck. ``offset`` being ignored
#: would return page 1 forever; the duplicate check catches that on page 2,
#: this catches a catalog too large to be a catalog.
MAX_PAGES = 20


class ThsInstrumentQuery(QueryParams):
    """Validated query for the instrument catalog domain."""

    asset_type: str


class ThsInstrumentFetcher(Fetcher[ThsInstrumentQuery, tuple[Instrument, ...]]):
    """One asset class' current instrument listing, paged to exhaustion.

    ``verified`` is true because the catalog's load-bearing fields were
    checked against vendors other than fuyao: every 中证 index constituent
    resolves into the a-share listing, and sina's first daily bar equals the
    catalog ``list_date`` day-for-day on the judged sample (see
    ``docs/evidence/C14``). The status derivation it publishes is
    snapshot-relative - see the module docstring - and the catalog carries no
    delisted A-shares, so it is a name universe of what trades *today*, not a
    survivorship-bias-free one.
    """

    capability: ClassVar[Capability] = Capability(
        asset_class="metadata",
        domain="instrument",
        period="snapshot",
        market="cn",
        source=SOURCE,
        verified=True,
        notes="current listing only; no delisted a-shares published",
    )

    def transform_query(self, **kwargs: object) -> ThsInstrumentQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``asset_type`` (required, one of :data:`ASSET_TYPES`).
                ``start_date`` / ``end_date`` are rejected.

        Returns:
            The validated query.

        Raises:
            ThsProviderError: A date range was requested, or the asset type
                is not one of the documented leaf types.
            ValidationError: On unknown fields or a missing ``asset_type``.
        """
        query = ThsInstrumentQuery.model_validate(kwargs)
        if query.start_date is not None or query.end_date is not None:
            raise ThsProviderError("THS_INSTRUMENT_SNAPSHOT_ONLY")
        if query.asset_type.strip().lower() not in ASSET_TYPES:
            raise ThsProviderError(f"THS_INSTRUMENT_ASSET_TYPE_UNSUPPORTED: {query.asset_type!r}")
        return query

    def extract_data(self, params: ThsInstrumentQuery, ctx: FetchContext) -> tuple[Instrument, ...]:
        """Page through one asset class' listing (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            Contract rows in upstream order, every page concatenated.

        Raises:
            ThsProviderError: Credentials missing, or the pages never ended.
        """
        from opendata_fuyao.endpoints import list_instruments

        rows: list[Instrument] = []
        with client(timeout_seconds=ctx.timeout) as active:
            for page in range(MAX_PAGES):
                batch = list_instruments(
                    active,
                    limit=PAGE_LIMIT,
                    offset=page * PAGE_LIMIT,
                    asset_type=params.asset_type,
                )
                rows.extend(batch)
                if len(batch) < PAGE_LIMIT:
                    return tuple(rows)
        raise ThsProviderError(
            f"THS_INSTRUMENT_PAGINATION_STUCK: no short page within {MAX_PAGES} × {PAGE_LIMIT}"
        )

    def transform_data(
        self, raw: tuple[Instrument, ...], params: ThsInstrumentQuery
    ) -> tuple[Instrument, ...]:
        """Check the snapshot's shape and sort it (the normalize stage).

        Args:
            raw: Rows from the extraction stage.
            params: The validated query.

        Returns:
            The same rows ordered by ``symbol``.

        Raises:
            ThsProviderError: The listing is empty, repeats a ``thscode``, or
                was published without the snapshot date its statuses need.
        """
        if not raw:
            raise ThsProviderError("THS_EMPTY_RESPONSE")
        if any(row.status == "unknown" for row in raw):
            raise ThsProviderError("THS_INSTRUMENT_SNAPSHOT_DATE_MISSING")
        symbols = [row.symbol for row in raw]
        if len(set(symbols)) != len(symbols):
            raise ThsProviderError("THS_INSTRUMENT_DUPLICATE_SYMBOL")
        return tuple(sorted(raw, key=lambda row: row.symbol))


__all__ = ["ASSET_TYPES", "MAX_PAGES", "PAGE_LIMIT", "ThsInstrumentFetcher", "ThsInstrumentQuery"]
