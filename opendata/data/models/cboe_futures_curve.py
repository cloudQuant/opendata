"""Typed Cboe futures-curve rows from the public daily settlement table.

The column list is the reference output contract of ``FuturesCurveData`` plus this repo's family
convention of stamping the requested symbol, and every cell is traced to the public document at
``https://www.cboe.com/us/futures/market_statistics/settlement/csv`` (captured unauthenticated
2026-10-19 in four dated takes at
``docs/evidence/C144/wave39-unauthenticated-access-20261019/sources/``; the header row is
``Product,Symbol,Expiration Date,Price``).

``symbol``
    The ``Product`` column. This document publishes every product's settlements in one table, so
    the symbol is read from the row rather than echoed from the request -- unlike the yfinance
    sibling, whose chain frame labels nothing.
``date``
    The settlement day the request selected. The table body carries no as-of cell: the day lives in
    the ``content-disposition`` filename the source sends (``FuturesSettlements_2026-10-16.csv``),
    which this repo's offline witness seam cannot deliver because the seam's fake response sets a
    content type and nothing else. So ``date`` is declared as the validated request echo and stays
    null when the caller asks for the table without naming a day. It is never stamped from a clock;
    ``DeribitFuturesCurve.date`` is the in-repo precedent for a nullable curve date.
``expiration``
    The ``Expiration Date`` column, published as an exact ``YYYY-MM-DD``. The reference contract
    types this as a month *string*; this repo's family declares ``expiration: date``, and the source
    answers the stricter type without loss, so no month is resolved to a first-of-month the way the
    yfinance sibling must.
``price``
    The ``Price`` column. Required and non-null: 56 of 56 rows in the 2026-10-16 capture carry a
    number, and a non-settlement day returns the header row alone rather than null-priced rows, so a
    cell that is not a number is a corrupt body rather than an untraded expiry.
"""

from __future__ import annotations

from datetime import date as date_type

from pydantic import StrictFloat, StrictStr

from opendata.data.models.base import ContractModel


class CboeFuturesCurve(ContractModel):
    """One dated Cboe futures settlement in the curve published for one product."""

    symbol: StrictStr
    date: date_type | None
    expiration: date_type
    price: StrictFloat


__all__ = ["CboeFuturesCurve"]
