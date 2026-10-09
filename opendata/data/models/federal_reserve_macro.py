"""Source-faithful contracts for the Fed's H.6 money stock and H.15 Treasury tables.

These are the two wide tables the ``datadownload/Output.aspx`` package endpoint publishes after
its five metadata rows, declared in ``opendata/data/providers/federal_reserve/specs.py``. Like
the cboe engine models before them, each declaration gets its own contract family and its own
domain, and the declaration must name exactly these fields: ``build_row_model`` publishes this
class -- and refuses with a column-symmetric-difference error -- whenever the domain's
semantics are declared.

Field types follow the source, not the wish:

* ``month`` is ``str`` because the H.6 body prints ``1959-01``, which no declared date kind
  parses; ``m1`` .. ``retail_money_market_funds`` are ``float | None`` because that capture's
  missing cells are blank and the engine reads a blank as null.
* ``date`` is a real ``datetime.date``: the H.15 body prints ``1962-01-02``, a strict ISO day.
  The eleven maturity columns are ``str | None`` on purpose -- the same body carries its own
  missing-data token ``ND`` in 8003 cells, and a numeric column would refuse those published
  rows. ``None`` is the blank cell; ``"ND"`` stays the token the source printed.
"""

from __future__ import annotations

from datetime import date as date_type

from opendata.data.models.base import ContractModel


class FederalReserveMoneyMeasure(ContractModel):
    """One monthly H.6 money-stock row: the period label plus the seven not-adjusted totals."""

    month: str
    m1: float | None = None
    m2: float | None = None
    currency: float | None = None
    demand_deposits: float | None = None
    other_liquid_deposits: float | None = None
    small_denomination_time_deposits: float | None = None
    retail_money_market_funds: float | None = None


class FederalReserveTreasuryRate(ContractModel):
    """One daily H.15 row, wide, with each maturity as the token the source published."""

    date: date_type
    month_1: str | None = None
    month_3: str | None = None
    month_6: str | None = None
    year_1: str | None = None
    year_2: str | None = None
    year_3: str | None = None
    year_5: str | None = None
    year_7: str | None = None
    year_10: str | None = None
    year_20: str | None = None
    year_30: str | None = None
