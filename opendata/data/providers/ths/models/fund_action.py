"""Fund distribution events fetcher (domain ``fund_action``, contract ``CorporateAction``).

Wraps ``/api/fund/corporate-actions/dividends`` through :mod:`opendata_fuyao`.
One row is one distribution: ``per_ten_cash_before_tax`` over ten is the
contract's per-unit ``cash_dividend``, and ``ex_dividend_date_ms`` is its
``ex_date``.

Why this leg exists at all, measured 2026-09-25 (``docs/evidence/C15``,
re-measured cell by cell in ``docs/evidence/C20``): the on-exchange ETF daily
endpoint publishes a **forward-adjusted series only**, and its ``adjust``
parameter is worse than absent - it is accepted and ignored, so the request
side cannot see that nothing happened. D10 forbids storing that, which is why
``fund_etf_daily`` waited on a factor route from C6 until C20. The route is
this endpoint: for 510300 the gap between an unadjusted bar and the adjusted
one is, to the cent, the sum of the distributions whose ex-date follows the
bar (0.088 + 0.123 = 0.211 on the 2025-01-02 bar). This leg therefore
publishes the conversion key, not prices;
:func:`opendata_fuyao.endpoints.unadjust_bars` consumes it.

Three measured shapes are encoded rather than trusted:

* a fund that never distributed answers ``dividend_count: 0`` with **one
  all-null row**, not with an empty list. That row is dropped, and an empty
  result is a fact about the fund rather than a failure - deliberately unlike
  the A-share corporate-action leg, which raises on an empty event stream;
* ``progress`` carries the numeric code ``"2"`` on every implemented record
  while the documentation example shows the label 实施;
* ``dividend_total`` is in CNY per unit (not per ten units) and reconciles
  against the sum of the records, which the normalizer checks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from opendata.data.capability import Capability
from opendata.data.models import CorporateAction
from opendata.data.protocol import Fetcher, QueryParams
from opendata.data.providers.ths._source import SOURCE
from opendata.data.providers.ths.models._client import client, resolve_fund_code

if TYPE_CHECKING:
    from opendata.data.protocol import FetchContext


class ThsFundActionQuery(QueryParams):
    """Validated query for the fund distribution domain."""

    symbol: str


class ThsFundActionFetcher(Fetcher[ThsFundActionQuery, tuple[CorporateAction, ...]]):
    """Cash distributions of one on-exchange fund, newest ex-date first.

    ``verified`` is true because the event stream was checked against a vendor
    other than fuyao: sina's cumulative-dividend stream for 510300 carries
    exactly 14 records, on the same 14 ex-dates in both directions, and its
    run-to-run differences equal the per-unit cash amounts published here digit
    for digit (see ``docs/evidence/C15``).
    """

    capability: ClassVar[Capability] = Capability(
        asset_class="fund",
        domain="fund_action",
        period="1D",
        market="cn",
        source=SOURCE,
        verified=True,
        notes="cash distributions only, per unit, pre-tax; full history per call",
    )

    def transform_query(self, **kwargs: object) -> ThsFundActionQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required, ``510300`` or ``510300.SH``) and
                an optional ``start_date`` / ``end_date`` over ex-dates.

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields or a missing ``symbol``.
        """
        return ThsFundActionQuery.model_validate(kwargs)

    def extract_data(
        self, params: ThsFundActionQuery, ctx: FetchContext
    ) -> tuple[CorporateAction, ...]:
        """Fetch the fund's whole distribution history (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            Contract events, newest ex-date first; empty when the fund has
            never distributed.

        Raises:
            ThsProviderError: Credentials missing, or a bare code that the ETF
                listing does not resolve to exactly one instrument.
            FuyaoError: Upstream sent a row this adapter refuses to publish
                (unknown progress code, missing ex-date, non-positive cash) or
                its own totals disagree with its rows.
        """
        from opendata_fuyao.endpoints import fetch_fund_dividends

        with client(timeout_seconds=ctx.timeout) as active:
            code = resolve_fund_code(active, params.symbol)
            return fetch_fund_dividends(active, symbol=code)

    def transform_data(
        self, raw: tuple[CorporateAction, ...], params: ThsFundActionQuery
    ) -> tuple[CorporateAction, ...]:
        """Apply the requested ex-date window (the normalize stage).

        The upstream has no window parameter - it answers with the history
        since inception - so a window is served by slicing. Unlike the calendar
        leg, a slice that matches nothing returns empty rather than raising:
        coverage here starts at the fund's inception, so "no distribution in
        that stretch" is a fact a caller is entitled to hear.

        Args:
            raw: Events from the extraction stage.
            params: The validated query.

        Returns:
            Events ordered by descending ex-date.
        """
        start, end = params.start_date, params.end_date
        if start is None and end is None:
            return raw
        return tuple(
            event
            for event in raw
            if (start is None or event.ex_date >= start) and (end is None or event.ex_date <= end)
        )


__all__ = ["ThsFundActionFetcher", "ThsFundActionQuery"]
