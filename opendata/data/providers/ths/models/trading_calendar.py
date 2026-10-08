"""Trading calendar fetcher (domain ``trading_calendar``, contract ``TradingCalendar``).

Two things in this repository answer to a calendar and are not the same
thing: the contract model here is a **row** (one calendar date), while
:class:`opendata.pipeline.trading_calendar.CalendarView` is the
**expectation predicate** the batch window and the freshness lag are derived
from - and that view is built by projecting the landed rows of this very
contract (``calendar_from_contracts``). This adapter is that predicate's
producer: the rows it returns are what has to land in
``dwd_trading_calendar`` for the warehouse tier to speak.

Upstream semantics are as measured on 2026-09-25 (see ``docs/evidence/C11``
§6 and ``docs/evidence/C12/calendar-check.txt``): the A-share endpoint
publishes a **rolling trailing year** whose tail is simply the last session
upstream has finished publishing - not a rule tied to the read date. Re-reading
it on 2026-09-27 05:2x Beijing (``docs/evidence/C41``) still answered
``2025-09-29..2026-09-24``, i.e. one *completed* trading day (Friday
2026-09-25) short of the tail. So "yesterday's trading day is inside coverage"
must not be assumed: ``today`` is never inside it, and neither is the most
recent session or two. All six candidate window parameters
(``start_date`` / ``begin_date`` / ``start`` / ``year`` / ``days`` / ``limit``)
are ignored. Consequences encoded here rather than papered over:

* no window parameter is sent - asking for one would be a fiction;
* a caller-supplied window is answered by **slicing** the returned coverage,
  never by extrapolating past it;
* a window that does not overlap coverage at all **raises** instead of
  returning an empty tuple - an empty answer must mean "the market was closed
  for that stretch", not "we asked for 2030 and got nothing" (the C11a lesson);
* ``prev_trade_date`` is derivable from the neighbouring rows, and so is
  ``next_trade_date`` **except for the last row**, whose successor is in a
  part of the calendar upstream does not publish - it stays ``None``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from opendata.data.capability import Capability
from opendata.data.models import TradingCalendar
from opendata.data.protocol import Fetcher, QueryParams
from opendata.data.providers.ths._source import SOURCE
from opendata.data.providers.ths.models._client import ThsProviderError, client

if TYPE_CHECKING:
    from opendata.data.protocol import FetchContext

#: The only exchange label this adapter publishes. The upstream A-share
#: calendar is not per-exchange (measured), so the field is filled rather
#: than negotiated, and a caller that asks for another label is refused.
DEFAULT_EXCHANGE = "CN-SSE"


class ThsTradingCalendarQuery(QueryParams):
    """Validated query for the trading calendar domain."""

    exchange: str = DEFAULT_EXCHANGE


class ThsTradingCalendarFetcher(Fetcher[ThsTradingCalendarQuery, tuple[TradingCalendar, ...]]):
    """One row per trading day of the published (trailing-year) window.

    ``verified`` is true because the calendar was checked against an
    independent vendor: every one of the 131 bar dates the akshare chain
    landed falls on a day this calendar opens, and inside that chain's own
    span (2026-01-05..2026-07-21) the calendar reports no open day the chain
    has no bars for - set equality in both directions across two vendors (see
    ``docs/evidence/C13``). The fuyao bar chain agrees as well but proves
    nothing here: it is the same vendor as the calendar.
    """

    async_mode = "bounded_thread"

    capability: ClassVar[Capability] = Capability(
        asset_class="metadata",
        domain="trading_calendar",
        period="snapshot",
        market="cn",
        source=SOURCE,
        verified=True,
        notes="trailing-year coverage; no future open days published",
    )

    def transform_query(self, **kwargs: object) -> ThsTradingCalendarQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``exchange`` (defaults to ``CN-SSE``), optional
                ``start_date`` / ``end_date`` slices of the published window.

        Returns:
            The validated query.

        Raises:
            ValidationError: On an empty exchange label or unknown fields.
            ThsProviderError: On an exchange label other than ``CN-SSE``.
        """
        query = ThsTradingCalendarQuery.model_validate(kwargs)
        if query.exchange != DEFAULT_EXCHANGE:
            raise ThsProviderError(
                f"THS_CALENDAR_EXCHANGE_UNSUPPORTED: upstream publishes one "
                f"A-share calendar, not a per-exchange one (asked {query.exchange!r})"
            )
        return query

    def extract_data(
        self, params: ThsTradingCalendarQuery, ctx: FetchContext
    ) -> tuple[TradingCalendar, ...]:
        """Fetch the published window (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            Calendar rows in ascending date order, ``is_open`` true throughout.

        Raises:
            ThsProviderError: Credentials missing, or upstream returned nothing.
        """
        from opendata.data.providers.ths.endpoints import fetch_trading_calendar

        with client(timeout_seconds=ctx.timeout) as active:
            rows = fetch_trading_calendar(active, exchange=params.exchange)
        if not rows:
            raise ThsProviderError("THS_EMPTY_RESPONSE")
        return rows

    def transform_data(
        self, raw: tuple[TradingCalendar, ...], params: ThsTradingCalendarQuery
    ) -> tuple[TradingCalendar, ...]:
        """Derive the neighbouring trade dates and apply the requested slice.

        Args:
            raw: Rows from the extraction stage.
            params: The validated query.

        Returns:
            Rows ordered by date, inside ``[start_date, end_date]`` when given.

        Raises:
            ThsProviderError: The requested window does not overlap the
                published coverage.
        """
        rows = _with_neighbours(raw)
        start, end = params.start_date, params.end_date
        if start is None and end is None:
            return rows
        first, last = rows[0].date, rows[-1].date
        if (start is not None and start > last) or (end is not None and end < first):
            raise ThsProviderError(
                f"THS_CALENDAR_OUT_OF_COVERAGE: published {first}..{last}, asked "
                f"{start or '-'}..{end or '-'}"
            )
        return tuple(
            row
            for row in rows
            if (start is None or row.date >= start) and (end is None or row.date <= end)
        )


def _with_neighbours(
    rows: tuple[TradingCalendar, ...],
) -> tuple[TradingCalendar, ...]:
    """Fill ``prev_trade_date`` / ``next_trade_date`` from adjacent rows.

    Upstream only publishes trading days, so the neighbour of a row in this
    sequence *is* the adjacent trading day. The row past the end has no known
    successor, so its ``next_trade_date`` stays ``None`` rather than being
    guessed from a weekday rule - that is exactly the gap the warehouse-tier
    predicate in :mod:`opendata.pipeline.trading_calendar` refuses to invent.

    Args:
        rows: Ascending calendar rows.

    Returns:
        New rows carrying their neighbours, in the same order.
    """
    ordered = sorted(rows, key=lambda row: row.date)
    return tuple(
        TradingCalendar(
            exchange=row.exchange,
            date=row.date,
            is_open=row.is_open,
            prev_trade_date=ordered[index - 1].date if index else None,
            next_trade_date=ordered[index + 1].date if index + 1 < len(ordered) else None,
        )
        for index, row in enumerate(ordered)
    )


__all__ = ["DEFAULT_EXCHANGE", "ThsTradingCalendarFetcher", "ThsTradingCalendarQuery"]
