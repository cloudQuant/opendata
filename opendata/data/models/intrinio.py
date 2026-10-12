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

``IntrinioInsiderTransaction`` is the one contract here whose rows are exploded out of a nested
array: one row per transaction inside each entry of the document's ``transaction_filings`` list,
with the filing-level scalars re-published onto every sub-row by the ``nested_array_explode``
plan. Seven of its 29 fields are required, and six of those seven are still nullable -- the
expansion must publish the key on every sub-row, yet upstream's before-validator
(insider_trading.py:109-117) turns a published ``''`` into ``None``, and the declaration mirrors
that with ``columns.null_sentinel`` -- while ``company_name`` is the single required,
non-nullable column, exactly the one column that carries no sentinel in the declaration.
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


class IntrinioInsiderTransaction(ContractModel):
    """One insider transaction, with its filing's identity carried onto the row.

    The 29 fields are exactly the columns ``providers/intrinio/specs.py::INSIDER_TRADING``
    declares. Six of its spellings are the provider's own aliases, taken from the cited
    ``alias_dict`` (insider_trading.py:50-57): ``owner_title``, ``transaction_type``,
    ``acquisition_or_disposition``, ``security_type``, ``securities_owned`` and
    ``securities_transacted`` are what the upstream model publishes where the SEC filing says
    ``officer_title``, ``transaction_type_code``, ``acquisition_disposition_code``,
    ``security_title``, ``total_shares_owned`` and ``amount_of_shares``.
    """

    symbol: str | None
    company_cik: str | None
    company_name: str
    filing_date: date | None
    filing_url: str | None
    owner_cik: str | None
    owner_name: str | None
    transaction_date: date | None = None
    ownership_type: str | None = None
    owner_title: str | None = None
    transaction_type: str | None = None
    acquisition_or_disposition: str | None = None
    security_type: str | None = None
    securities_owned: float | None = None
    securities_transacted: float | None = None
    transaction_price: float | None = None
    conversion_exercise_price: float | None = None
    deemed_execution_date: date | None = None
    exercise_date: date | None = None
    expiration_date: date | None = None
    underlying_security_title: str | None = None
    underlying_shares: float | None = None
    nature_of_ownership: str | None = None
    director: bool | None = None
    officer: bool | None = None
    ten_percent_owner: bool | None = None
    other_relation: bool | None = None
    derivative_transaction: bool | None = None
    report_line_number: int | None = None
