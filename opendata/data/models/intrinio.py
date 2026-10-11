"""Reviewed contracts for intrinio's declared engine models.

Each field set is exactly the columns the matching ``ModelSpec`` publishes, which is what
``build_row_model`` checks before it hands a declaration its contract class. Field spellings and
renames follow the cited upstream output contract, nothing else.

``name`` appears on the two directory rows because both standard models publish it and neither
intrinio subclass aliases it away: ``CurrencyPairsData.name`` (standard_models/currency_pairs.py:21)
and ``EquitySearchData.name`` (standard_models/equity_search.py:24) ride into the provider's own
``__alias_dict__`` untouched (currency_pairs.py:25 lists only ``symbol<-code``,
equity_search.py:45-48 only ``intrinio_id<-id`` and ``symbol<-ticker``), so the record key and the
published column are the same spelling. Omitting it would drop a column the contract publishes.

``IntrinioDividendAdjustment.symbol`` is the one reviewed field whose value the response record does
not publish. Upstream builds every row as ``{"symbol": query.symbol, **item}``
(historical_dividends.py:102-104), and this repo's declaration says so with a
``ModelSpec.column_derives`` plan of ``source_kind="parameter"`` rather than a ``source_key``, so
the row carries the symbol the caller asked for. The field is required because the query parameter
is: a stamped ``None`` would mean the caller named no symbol, and that request never reaches the
wire.
"""

from __future__ import annotations

from datetime import date

from opendata.data.models.base import ContractModel


class IntrinioCurrencyPair(ContractModel):
    """One tradable forex pair in intrinio's published directory."""

    symbol: str | None
    name: str | None = None
    base_currency: str | None
    quote_currency: str | None


class IntrinioSearchedCompany(ContractModel):
    """One company row out of intrinio's company search."""

    symbol: str | None
    name: str | None = None
    intrinio_id: str | None
    cik: str | None = None
    lei: str | None = None


class IntrinioDividendAdjustment(ContractModel):
    """One adjustment row published for one security.

    ``ex_dividend_date`` and ``amount`` are non-null because the standard model declares them
    required (standard_models/historical_dividends.py:38-41), so a record the endpoint publishes
    without those keys is a shape failure here and a validation error upstream. Which adjustment
    kinds also carry ``factor``/``split_ratio`` is unverified -- no body of this endpoint has been
    captured in this repo -- so those three stay optional.
    """

    symbol: str | None
    ex_dividend_date: date
    amount: float
    currency: str | None = None
    factor: float | None = None
    split_ratio: float | None = None


class IntrinioPublicOffering(ContractModel):
    """One offering on intrinio's IPO calendar, with its nested objects flattened.

    The 35 fields are exactly the columns ``providers/intrinio/specs.py::CALENDAR_IPO`` declares:
    the 20 scalars the item itself prints, then the 5 ``company`` and 10 ``security`` scalars its
    ``RecordRepointPlan`` re-resolves inside each record.

    Every field is optional because every upstream field is: ``CalendarIpoData`` declares each one
    ``Optional`` with ``default=None`` (standard_models/calendar_ipo.py:34-39), the two that key
    ``intrinio_calendar_ipo`` included. So ``symbol`` and ``ipo_date`` stay nullable here, and a
    record publishing neither ``ticker`` nor ``date`` answers with two nulls rather than a refusal
    -- the promise ``tests/test_engine_row_expansion_spec.py`` judges for this declaration.
    """

    symbol: str | None = None
    ipo_date: date | None = None
    status: str | None = None
    exchange: str | None = None
    offer_amount: float | None = None
    share_price: float | None = None
    share_price_lowest: float | None = None
    share_price_highest: float | None = None
    share_count: int | None = None
    share_count_lowest: int | None = None
    share_count_highest: int | None = None
    announcement_url: str | None = None
    sec_report_url: str | None = None
    open_price: float | None = None
    close_price: float | None = None
    volume: int | None = None
    day_change: float | None = None
    week_change: float | None = None
    month_change: float | None = None
    id: str | None = None
    company_id: str | None = None
    company_ticker: str | None = None
    company_name: str | None = None
    company_lei: str | None = None
    company_cik: str | None = None
    security_id: str | None = None
    security_name: str | None = None
    security_code: str | None = None
    security_currency: str | None = None
    security_ticker: str | None = None
    security_composite_ticker: str | None = None
    security_figi: str | None = None
    security_composite_figi: str | None = None
    security_share_class_figi: str | None = None
    security_primary_listing: bool | None = None
