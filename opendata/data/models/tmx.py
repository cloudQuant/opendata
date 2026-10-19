"""Strict TMX Money output contracts backed by observed public UI fields.

These classes intentionally contain only fields whose source mapping is
reviewed in ``docs/evidence/C100/tmx/``.  They are source-specific contracts,
not claims that the TMX Money UI exposes every field in the general OpenBB
model.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import ConfigDict, StrictInt, StrictStr, field_validator

from opendata.data.models.base import ContractModel


def _number(value: object) -> object:
    """Reject JSON strings and booleans where TMX publishes numeric values."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("TMX numeric fields must be JSON numbers")
    return value


def _iso_date(value: object) -> object:
    """Parse only exact ISO calendar dates, without inventing a timezone."""
    if isinstance(value, datetime):
        raise ValueError("TMX calendar-date fields must not contain a time")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError("TMX calendar-date fields must be ISO date strings")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("TMX calendar-date fields must be exact ISO dates")
    return parsed


class _TmxContract(ContractModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class TmxEquitySearch(_TmxContract):
    """One row from the public TMX company keyword search."""

    symbol: StrictStr | None = None
    name: StrictStr | None = None


class TmxEquityHistorical(_TmxContract):
    """One daily price-history row returned by TMX Money's public chart query."""

    symbol: StrictStr
    date: date | datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | int | None = None
    vwap: float | None = None
    change: float | None = None
    change_percent: float | None = None
    transactions: StrictInt | None = None
    transactions_value: float | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _parse_source_datetime(cls, value: object) -> object:
        if isinstance(value, datetime | date):
            return value
        if not isinstance(value, str):
            raise ValueError("TMX history datetime must be an ISO string")
        try:
            parsed: date | datetime
            if "T" in value:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            else:
                parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("TMX history datetime must be ISO formatted") from exc
        return parsed

    @field_validator(
        "open",
        "high",
        "low",
        "close",
        "volume",
        "vwap",
        "change",
        "change_percent",
        "transactions_value",
        mode="before",
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


class TmxEquityQuote(_TmxContract):
    """Current quote fields with direct, documented field correspondences."""

    symbol: StrictStr
    name: StrictStr | None = None
    exchange: StrictStr | None = None
    last_price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | int | None = None
    prev_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    year_high: float | None = None
    year_low: float | None = None
    market_cap: float | None = None
    pe: float | None = None
    eps: float | None = None
    vwap: float | None = None
    currency: StrictStr | None = None

    @field_validator(
        "last_price",
        "open",
        "high",
        "low",
        "volume",
        "prev_close",
        "change",
        "change_percent",
        "year_high",
        "year_low",
        "market_cap",
        "pe",
        "eps",
        "vwap",
        mode="before",
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


class TmxEquityInfo(_TmxContract):
    """Profile fields present in the public TMX quote/profile selection."""

    symbol: StrictStr
    name: StrictStr | None = None
    stock_exchange: StrictStr | None = None
    long_description: StrictStr | None = None
    company_url: StrictStr | None = None
    business_address: StrictStr | None = None
    business_phone_no: StrictStr | None = None
    email: StrictStr | None = None
    employees: StrictInt | None = None
    sector: StrictStr | None = None
    issue_type: StrictStr | None = None
    shares_outstanding: StrictInt | None = None
    shares_escrow: StrictInt | None = None
    shares_total: StrictInt | None = None
    dividend_frequency: StrictStr | None = None


class TmxCompanyFiling(_TmxContract):
    """A public filing record; ``report_type`` stays null without a proven mapping."""

    symbol: StrictStr
    filing_date: date
    report_type: StrictStr | None = None
    report_url: StrictStr
    description: StrictStr | None = None
    size: StrictStr | StrictInt | None = None

    @field_validator("filing_date", mode="before")
    @classmethod
    def _parse_filing_date(cls, value: object) -> object:
        return _iso_date(value)


class TmxHistoricalDividend(_TmxContract):
    """One dividend row from TMX Money's explicitly paginated history query."""

    symbol: StrictStr
    ex_dividend_date: date
    amount: float
    currency: StrictStr | None = None
    declaration_date: date | None = None
    record_date: date | None = None
    payment_date: date | None = None

    @field_validator(
        "ex_dividend_date", "declaration_date", "record_date", "payment_date", mode="before"
    )
    @classmethod
    def _parse_dates(cls, value: object) -> object:
        return _iso_date(value) if value is not None else None

    @field_validator("amount", mode="before")
    @classmethod
    def _validate_amount(cls, value: object) -> object:
        return _number(value)


class TmxIndexConstituent(_TmxContract):
    """One constituent in the TMX Money index directory response."""

    symbol: StrictStr
    name: StrictStr | None = None
    exchange: StrictStr | None = None
    market_value: float | None = None
    weight: float | None = None

    @field_validator("market_value", "weight", mode="before")
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


class TmxPriceTargetConsensus(_TmxContract):
    """Analyst consensus fields exposed by the public TMX quote page."""

    symbol: StrictStr
    name: StrictStr | None = None
    target_high: float | None = None
    target_low: float | None = None
    target_consensus: float | None = None
    target_median: float | None = None
    target_upside: float | None = None
    total_analysts: StrictInt | None = None
    buy_ratings: StrictInt | None = None
    sell_ratings: StrictInt | None = None
    hold_ratings: StrictInt | None = None
    consensus_action: StrictStr | None = None

    @field_validator(
        "target_high",
        "target_low",
        "target_consensus",
        "target_median",
        "target_upside",
        mode="before",
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


TmxModelStatus = Literal["READY", "INCOMPLETE"]


class TmxCalendarEarnings(_TmxContract):
    """One TMX earnings-calendar row keyed by the reported quarter's date."""

    report_date: date
    symbol: StrictStr
    name: StrictStr
    eps_previous: float | None = None
    eps_consensus: float | None = None
    eps_actual: float | None = None
    eps_surprise: float | None = None
    surprise_percent: float | None = None
    reporting_time: StrictStr | None = None

    @field_validator("report_date", mode="before")
    @classmethod
    def _parse_report_date(cls, value: object) -> object:
        return _iso_date(value)

    @field_validator(
        "eps_previous",
        "eps_consensus",
        "eps_actual",
        "eps_surprise",
        "surprise_percent",
        mode="before",
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


class TmxCompanyNews(_TmxContract):
    """One TMX company news article; ``images`` keeps the source list shape."""

    date: datetime
    title: StrictStr
    url: StrictStr
    author: StrictStr | None = None
    excerpt: StrictStr | None = None
    body: StrictStr | None = None
    images: list[Any] | None = None
    symbols: StrictStr | None = None
    source: StrictStr | None = None


class TmxEquityGainers(_TmxContract):
    """One ranked row from the TMX gainers/losers screen."""

    symbol: StrictStr
    price: float
    change: float
    percent_change: float
    rank: StrictInt
    name: StrictStr | None = None
    volume: float | int | None = None
    thirty_day_price_change: float | None = None
    ninety_day_price_change: float | None = None
    dividend_yield: float | None = None
    avg_volume_10d: float | None = None

    @field_validator(
        "price",
        "change",
        "percent_change",
        "volume",
        "thirty_day_price_change",
        "ninety_day_price_change",
        "dividend_yield",
        "avg_volume_10d",
        mode="before",
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


class TmxInsiderTrading(_TmxContract):
    """One insider-transaction row; ``period`` is the source reporting period."""

    period: StrictStr
    symbol: StrictStr | None = None
    company_cik: StrictStr | None = None
    filing_date: date | datetime | None = None
    transaction_date: date | None = None
    owner_cik: StrictInt | StrictStr | None = None
    owner_name: StrictStr | None = None
    owner_title: StrictStr | None = None
    ownership_type: StrictStr | None = None
    transaction_type: StrictStr | None = None
    acquisition_or_disposition: StrictStr | None = None
    security_type: StrictStr | None = None
    securities_owned: StrictInt | None = None
    securities_transacted: StrictInt | None = None
    transaction_price: float | None = None
    filing_url: StrictStr | None = None
    acquisition_or_deposition: StrictStr | None = None
    number_of_trades: StrictInt | None = None
    trade_value: float | None = None
    securities_bought: StrictInt | None = None
    securities_sold: StrictInt | None = None
    net_activity: StrictInt | None = None

    @field_validator("filing_date", "transaction_date", mode="before")
    @classmethod
    def _parse_dates(cls, value: object) -> object:
        return None if value is None else _iso_date(value)

    @field_validator("transaction_price", "trade_value", mode="before")
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


class TmxEtfHistorical(_TmxContract):
    """One daily ETF price-history row; ``symbol`` is echoed from the request."""

    symbol: StrictStr
    date: date | datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | int | None = None
    vwap: float | None = None
    change: float | None = None
    change_percent: float | None = None
    transactions: StrictInt | None = None
    transactions_value: float | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _parse_source_datetime(cls, value: object) -> object:
        if isinstance(value, datetime | date):
            return value
        if not isinstance(value, str):
            raise ValueError("TMX ETF history datetime must be an ISO string")
        try:
            parsed: date | datetime
            if "T" in value:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            else:
                parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("TMX ETF history datetime must be ISO formatted") from exc
        return parsed

    @field_validator(
        "open",
        "high",
        "low",
        "close",
        "volume",
        "vwap",
        "change",
        "change_percent",
        "transactions_value",
        mode="before",
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


class TmxEtfInfo(_TmxContract):
    """ETF profile and performance fields from the shared TMX ETF directory."""

    symbol: StrictStr
    name: StrictStr | None = None
    issuer: StrictStr | None = None
    domicile: StrictStr | None = None
    website: StrictStr | None = None
    description: StrictStr | None = None
    inception_date: date | None = None
    investment_style: StrictStr | None = None
    esg: bool | None = None
    currency: StrictStr | None = None
    unit_price: float | None = None
    close: float | None = None
    prev_close: float | None = None
    return_1m: float | None = None
    return_3m: float | None = None
    return_6m: float | None = None
    return_ytd: float | None = None
    return_1y: float | None = None
    return_3y: float | None = None
    return_5y: float | None = None
    return_10y: float | None = None
    return_from_inception: float | None = None
    avg_volume: StrictInt | None = None
    avg_volume_30d: StrictInt | None = None
    aum: float | None = None
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    management_fee: float | None = None
    mer: float | None = None
    distribution_yield: float | None = None
    dividend_frequency: StrictStr | None = None

    @field_validator("inception_date", mode="before")
    @classmethod
    def _parse_inception_date(cls, value: object) -> object:
        return None if value is None else _iso_date(value)

    @field_validator(
        "unit_price",
        "close",
        "prev_close",
        "return_1m",
        "return_3m",
        "return_6m",
        "return_ytd",
        "return_1y",
        "return_3y",
        "return_5y",
        "return_10y",
        "return_from_inception",
        "aum",
        "pe_ratio",
        "pb_ratio",
        "management_fee",
        "mer",
        "distribution_yield",
        mode="before",
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


class TmxEtfSearch(_TmxContract):
    """One ETF row from the TMX ETF directory search, including beta columns."""

    symbol: StrictStr
    name: StrictStr | None = None
    short_name: StrictStr | None = None
    inception_date: StrictStr | None = None
    issuer: StrictStr | None = None
    investment_style: StrictStr | None = None
    esg: bool | None = None
    currency: StrictStr | None = None
    unit_price: float | None = None
    close: float | None = None
    prev_close: float | None = None
    return_1m: float | None = None
    return_3m: float | None = None
    return_6m: float | None = None
    return_ytd: float | None = None
    return_1y: float | None = None
    beta_1y: float | None = None
    return_3y: float | None = None
    beta_3y: float | None = None
    return_5y: float | None = None
    beta_5y: float | None = None
    return_10y: float | None = None
    beta_10y: float | None = None
    beta_15y: float | None = None
    return_from_inception: float | None = None
    avg_volume: StrictInt | None = None
    avg_volume_30d: StrictInt | None = None
    aum: float | None = None
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    management_fee: float | None = None
    mer: float | None = None
    distribution_yield: float | None = None
    dividend_frequency: StrictStr | None = None

    @field_validator(
        "unit_price",
        "close",
        "prev_close",
        "return_1m",
        "return_3m",
        "return_6m",
        "return_ytd",
        "return_1y",
        "beta_1y",
        "return_3y",
        "beta_3y",
        "return_5y",
        "beta_5y",
        "return_10y",
        "beta_10y",
        "beta_15y",
        "return_from_inception",
        "aum",
        "pe_ratio",
        "pb_ratio",
        "management_fee",
        "mer",
        "distribution_yield",
        mode="before",
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


class TmxEtfSectors(_TmxContract):
    """One sector weight from the shared TMX ETF directory."""

    sector: StrictStr
    weight: float
    symbol: StrictStr | None = None

    @field_validator("weight", mode="before")
    @classmethod
    def _validate_weight(cls, value: object) -> object:
        return _number(value)


class TmxEtfCountries(_TmxContract):
    """One country weight from the shared TMX ETF directory."""

    country: StrictStr
    weight: float
    symbol: StrictStr | None = None

    @field_validator("weight", mode="before")
    @classmethod
    def _validate_weight(cls, value: object) -> object:
        return _number(value)


class TmxEtfHoldings(_TmxContract):
    """One ETF holding row; TMX publishes several of these fields as strings."""

    symbol: StrictStr | None = None
    name: StrictStr | None = None
    weight: float | None = None
    shares: StrictInt | StrictStr | None = None
    market_value: float | StrictStr | None = None
    currency: StrictStr | None = None
    share_percentage: float | None = None
    share_change: float | StrictStr | None = None
    country: StrictStr | None = None
    exchange: StrictStr | None = None
    type_id: StrictStr | None = None
    fund_id: StrictStr | None = None

    @field_validator("weight", "share_percentage", mode="before")
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


class TmxBondPrices(_TmxContract):
    """One IIROC-designated bond row; identifiers stay null when the feed omits them."""

    isin: StrictStr | None = None
    lei: StrictStr | None = None
    figi: StrictStr | None = None
    cusip: StrictStr | None = None
    coupon_rate: float | None = None
    ytm: float | None = None
    price: float | None = None
    highest_price: float | None = None
    lowest_price: float | None = None
    total_trades: StrictInt | None = None
    last_traded_date: date | None = None
    maturity_date: date | None = None
    issue_date: date | None = None
    issuer_name: StrictStr | None = None

    @field_validator(
        "coupon_rate",
        "ytm",
        "price",
        "highest_price",
        "lowest_price",
        mode="before",
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)

    @field_validator("last_traded_date", "maturity_date", "issue_date", mode="before")
    @classmethod
    def _parse_dates(cls, value: object) -> object:
        return None if value is None else _iso_date(value)


class TmxTreasuryPrices(_TmxContract):
    """One IIROC treasury-bond row from the same debt listing as ``TmxBondPrices``."""

    issuer_name: StrictStr | None = None
    cusip: StrictStr | None = None
    isin: StrictStr | None = None
    security_type: StrictStr | None = None
    issue_date: date | None = None
    maturity_date: date | None = None
    call_date: date | None = None
    bid: float | None = None
    offer: float | None = None
    eod_price: float | None = None
    last_traded_date: date | None = None
    total_trades: StrictInt | None = None
    last_price: float | None = None
    highest_price: float | None = None
    lowest_price: float | None = None
    rate: float | None = None
    ytm: float | None = None

    @field_validator(
        "bid",
        "offer",
        "eod_price",
        "last_price",
        "highest_price",
        "lowest_price",
        "rate",
        "ytm",
        mode="before",
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)

    @field_validator("issue_date", "maturity_date", "call_date", "last_traded_date", mode="before")
    @classmethod
    def _parse_dates(cls, value: object) -> object:
        return None if value is None else _iso_date(value)


class TmxAvailableIndices(_TmxContract):
    """One index in the TMX information-services index directory."""

    symbol: StrictStr
    name: StrictStr | None = None
    exchange: StrictStr | None = None
    currency: StrictStr | None = None


class TmxIndexSectors(_TmxContract):
    """One sector weight published for one TMX index."""

    sector: StrictStr
    weight: float

    @field_validator("weight", mode="before")
    @classmethod
    def _validate_weight(cls, value: object) -> object:
        return _number(value)


_OPTIONS_CHAIN_NUMERIC_FIELDS: tuple[str, ...] = (
    "underlying_price",
    "strike",
    "contract_size",
    "open_interest",
    "volume",
    "theoretical_price",
    "last_trade_price",
    "last_trade_size",
    "tick",
    "bid",
    "bid_size",
    "ask",
    "ask_size",
    "mark",
    "open",
    "open_bid",
    "open_ask",
    "high",
    "bid_high",
    "ask_high",
    "low",
    "bid_low",
    "ask_low",
    "close",
    "close_size",
    "close_bid",
    "close_bid_size",
    "close_ask",
    "close_ask_size",
    "prev_close",
    "change",
    "change_percent",
    "implied_volatility",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "transactions",
    "total_value",
    "settlement_price",
)


class TmxOptionsChains(_TmxContract):
    """One listed option contract in the TMX/IIROC chain for one underlying and expiry.

    The field list is the upstream declared output contract of
    ``openbb_tmx/models/options_chains.py`` (spec sheet sha256
    ``dcb533c0de26bfdd5c3ee39cdb9560a0d4e953dcf91968376a1d2cd347be6d0a``), not an
    invention: TMX's chain is published as an m-x.ca quotes table plus an EOD CSV file, and
    every column below is one of the columns that pair carries.

    Nullability is a sourcing statement, not a shape statement.  ``contract_symbol``,
    ``expiration`` and ``strike`` are the three cells the m-x.ca quote table publishes for
    every row, so they are required; every other cell is optional because neither upstream
    prose nor this repo's evidence establishes that a chain row always carries it (a
    never-traded contract has no last trade, no bid/ask time and no greeks).
    ``option_type`` is kept a free string rather than a ``call``/``put`` literal because the
    vocabulary TMX's own CSV uses for it (``C``/``P``) is not established by any evidence in
    this repo, and typing a literal here would assert a source enum nobody has witnessed.
    ``last_trade_time``, ``bid_time``, ``ask_time``, ``close_time``, ``close_bid_time`` and
    ``close_ask_time`` stay strings for the same reason: they are exchange clock timestamps
    with no published timezone, and :func:`_iso_date` refuses anything that is not an exact
    ISO calendar date.
    """

    underlying_symbol: StrictStr | None = None
    underlying_price: float | None = None
    contract_symbol: StrictStr
    eod_date: date | None = None
    expiration: date
    dte: StrictInt | None = None
    strike: float
    option_type: StrictStr | None = None
    contract_size: float | None = None
    open_interest: StrictInt | None = None
    volume: StrictInt | None = None
    theoretical_price: float | None = None
    last_trade_price: float | None = None
    last_trade_size: StrictInt | None = None
    last_trade_time: StrictStr | None = None
    tick: float | None = None
    bid: float | None = None
    bid_size: StrictInt | None = None
    bid_time: StrictStr | None = None
    bid_exchange: StrictStr | None = None
    ask: float | None = None
    ask_size: StrictInt | None = None
    ask_time: StrictStr | None = None
    ask_exchange: StrictStr | None = None
    mark: float | None = None
    open: float | None = None
    open_bid: float | None = None
    open_ask: float | None = None
    high: float | None = None
    bid_high: float | None = None
    ask_high: float | None = None
    low: float | None = None
    bid_low: float | None = None
    ask_low: float | None = None
    close: float | None = None
    close_size: StrictInt | None = None
    close_time: StrictStr | None = None
    close_bid: float | None = None
    close_bid_size: StrictInt | None = None
    close_bid_time: StrictStr | None = None
    close_ask: float | None = None
    close_ask_size: StrictInt | None = None
    close_ask_time: StrictStr | None = None
    prev_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    transactions: StrictInt | None = None
    total_value: float | None = None
    settlement_price: float | None = None

    @field_validator("eod_date", "expiration", mode="before")
    @classmethod
    def _parse_dates(cls, value: object) -> object:
        return None if value is None else _iso_date(value)

    @field_validator(*_OPTIONS_CHAIN_NUMERIC_FIELDS, mode="before")
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


_INDEX_SNAPSHOT_NUMERIC_FIELDS: tuple[str, ...] = (
    "price",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "prev_close",
    "change",
    "change_percent",
    "year_high",
    "year_low",
    "return_mtd",
    "return_qtd",
    "return_ytd",
    "total_market_value",
    "number_of_constituents",
    "constituent_average_market_value",
    "constituent_median_market_value",
    "constituent_top10_market_value",
    "constituent_largest_market_value",
    "constituent_largest_weight",
    "constituent_smallest_market_value",
    "constituent_smallest_weight",
)


class TmxIndexSnapshots(_TmxContract):
    """One index snapshot row in the TMX regional index panel.

    The field list is the upstream declared output contract of
    ``openbb_tmx/models/index_snapshots.py`` (spec sheet sha256
    ``6bff37ba7cda7c7336cf83d545495a1098348f948369214c31209c53253c869b``).  Everything
    except ``symbol`` is nullable because the row is assembled from three different
    transports upstream -- the GraphQL quote leg, a static index JSON directory and a
    cached index backend -- and a snapshot that only reached the quote leg still has a
    price but publishes none of the constituent statistics.  Nothing here invents a
    source for the six ``constituent_*`` statistics; they are declared nullable so an
    implementer can only land them from a leg that actually returns them.
    """

    symbol: StrictStr
    name: StrictStr | None = None
    currency: StrictStr | None = None
    price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: StrictInt | None = None
    prev_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    year_high: float | None = None
    year_low: float | None = None
    return_mtd: float | None = None
    return_qtd: float | None = None
    return_ytd: float | None = None
    total_market_value: float | None = None
    number_of_constituents: StrictInt | None = None
    constituent_average_market_value: float | None = None
    constituent_median_market_value: float | None = None
    constituent_top10_market_value: float | None = None
    constituent_largest_market_value: float | None = None
    constituent_largest_weight: float | None = None
    constituent_smallest_market_value: float | None = None
    constituent_smallest_weight: float | None = None

    @field_validator(*_INDEX_SNAPSHOT_NUMERIC_FIELDS, mode="before")
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


__all__ = [
    "TmxBondPrices",
    "TmxAvailableIndices",
    "TmxCalendarEarnings",
    "TmxCompanyFiling",
    "TmxCompanyNews",
    "TmxInsiderTrading",
    "TmxEquityGainers",
    "TmxEquityHistorical",
    "TmxEquityInfo",
    "TmxEquityQuote",
    "TmxEquitySearch",
    "TmxEtfCountries",
    "TmxEtfHistorical",
    "TmxEtfHoldings",
    "TmxEtfInfo",
    "TmxEtfSearch",
    "TmxEtfSectors",
    "TmxHistoricalDividend",
    "TmxIndexConstituent",
    "TmxIndexSectors",
    "TmxIndexSnapshots",
    "TmxOptionsChains",
    "TmxPriceTargetConsensus",
    "TmxTreasuryPrices",
    "TmxModelStatus",
]
