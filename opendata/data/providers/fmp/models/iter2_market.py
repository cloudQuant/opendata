"""Bounded, clean-room FMP Stable market models for Iteration 2."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date as date_type
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Literal, TypeVar

from pydantic import Field, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.base import ContractModel
from opendata.data.models.fmp_market import (
    CryptoHistorical,
    CryptoSearch,
    CurrencyHistorical,
    CurrencyPair,
    CurrencySnapshot,
    EquityActive,
    EquityGainers,
    EquityInfo,
    EquityLosers,
    EquityPeers,
    EquityScreener,
    FmpIndexConstituent,
    IndexHistorical,
    MarketSnapshot,
    PricePerformance,
    ShareStatistics,
)
from opendata.data.protocol import (
    BaseQueryParams,
    FetchContext,
    Fetcher,
    FetchResult,
    MultiSymbolQueryParams,
    QueryParams,
)
from opendata.data.providers.fmp.models._client import (
    FMP_HISTORICAL_RESPONSE_LIMIT,
    FMPProviderError,
    FMPQueryLimitError,
    fetch_array,
)

if TYPE_CHECKING:
    from opendata.data.providers._engine.http_json import HttpGet

_MAX_SYMBOL_REQUESTS = 8
_MAX_REQUESTS = 20
_MAX_RECORDS = 20_000
_MAX_LIST_RECORDS = 20_000
_MAX_SCREENER_PAGES = 5
_MAX_SCREENER_LIMIT = 1_000
_MAX_MARKET_ROWS = 5_000
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.^_-]{1,32}$", re.ASCII)
_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$", re.ASCII)
ModelT = TypeVar("ModelT", bound=ContractModel)
QueryT = TypeVar("QueryT", bound="_MarketQueryBase")
RecordT = TypeVar("RecordT", bound=ContractModel)
HistoricalQueryT = TypeVar("HistoricalQueryT", bound="HistoricalQuery")
HistoricalRecordT = TypeVar("HistoricalRecordT", bound=ContractModel)


def _date_bound(value: object) -> object:
    if isinstance(value, datetime):
        raise ValueError("date bounds must not include a time")
    if isinstance(value, date_type):
        return value
    if not isinstance(value, str):
        raise ValueError("date bounds must be ISO dates")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date bounds must be ISO dates") from exc
    if parsed.isoformat() != value:
        raise ValueError("date bounds must be ISO dates")
    return parsed


def _validate_symbol(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("symbol must be text")
    normalized = value.strip().upper()
    if not normalized or not _SYMBOL_RE.fullmatch(normalized):
        raise ValueError("symbol contains unsupported characters")
    return normalized


def _symbol_list(value: object) -> object:
    if isinstance(value, str):
        values: Sequence[object] = (value,)
    elif isinstance(value, list):
        values = value
    else:
        raise ValueError("symbol must be text or a list of symbols")
    normalized = [_validate_symbol(item) for item in values]
    if not normalized or len(normalized) > _MAX_SYMBOL_REQUESTS:
        raise ValueError("symbol count must be between 1 and 8")
    if len(normalized) != len(set(normalized)):
        raise ValueError("symbol list must not contain duplicates")
    return normalized


def _finite_or_none(value: object) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FMPProviderError("FMP_BAD_FIELD")
    if not math.isfinite(float(value)):
        raise FMPProviderError("FMP_BAD_FIELD")
    return value


def _required_value(row: Mapping[str, Any], key: str) -> object:
    if key not in row or row[key] is None:
        raise FMPProviderError("FMP_BAD_FIELD")
    return row[key]


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise FMPProviderError("FMP_BAD_FIELD")
    return value


def _required_text(row: Mapping[str, Any], key: str) -> str:
    value = _required_value(row, key)
    if not isinstance(value, str) or not value.strip():
        raise FMPProviderError("FMP_BAD_FIELD")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise FMPProviderError("FMP_BAD_FIELD")
    return value


def _number(row: Mapping[str, Any], key: str, *, required: bool = False) -> int | float | None:
    if key not in row:
        if required:
            raise FMPProviderError("FMP_BAD_FIELD")
        return None
    value = row[key]
    if value is None and not required:
        return None
    number = _finite_or_none(value)
    if number is None and required:
        raise FMPProviderError("FMP_BAD_FIELD")
    return number


def _integer(row: Mapping[str, Any], key: str, *, required: bool = False) -> int | None:
    """Read a JSON integer without silently truncating source decimals."""
    if key not in row or row[key] is None:
        if required:
            raise FMPProviderError("FMP_BAD_FIELD")
        return None
    value = row[key]
    if type(value) is not int or value < 0:
        raise FMPProviderError("FMP_BAD_FIELD")
    return value


def _maybe_alias_number(
    row: Mapping[str, Any], primary: str, alternate: str, *, required: bool = False
) -> int | float | None:
    if (
        primary in row
        and alternate in row
        and row[primary] is not None
        and row[alternate] is not None
        and row[primary] != row[alternate]
    ):
        raise FMPProviderError("FMP_AMBIGUOUS_FIELD")
    key = primary if primary in row and row[primary] is not None else alternate
    return _number(row, key, required=required)


def _maybe_alias_integer(
    row: Mapping[str, Any], primary: str, alternate: str, *, required: bool = False
) -> int | None:
    if (
        primary in row
        and alternate in row
        and row[primary] is not None
        and row[alternate] is not None
        and row[primary] != row[alternate]
    ):
        raise FMPProviderError("FMP_AMBIGUOUS_FIELD")
    key = primary if primary in row and row[primary] is not None else alternate
    return _integer(row, key, required=required)


def _maybe_alias_text(
    row: Mapping[str, Any], primary: str, alternate: str, *, required: bool = False
) -> str | None:
    if (
        primary in row
        and alternate in row
        and row[primary] is not None
        and row[alternate] is not None
        and row[primary] != row[alternate]
    ):
        raise FMPProviderError("FMP_AMBIGUOUS_FIELD")
    key = primary if primary in row and row[primary] is not None else alternate
    if key not in row or row[key] is None:
        if required:
            raise FMPProviderError("FMP_BAD_FIELD")
        return None
    value = row[key]
    if not isinstance(value, str) or not value.strip():
        raise FMPProviderError("FMP_BAD_FIELD")
    return _optional_text(value)


def _profile_range(row: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """Map the documented low-high profile range without guessing a bound."""
    value = row.get("range")
    if value is None:
        return None, None
    if not isinstance(value, str) or value.count("-") != 1:
        raise FMPProviderError("FMP_BAD_FIELD")
    low_text, high_text = value.split("-", 1)
    try:
        low, high = float(low_text), float(high_text)
    except ValueError as exc:
        raise FMPProviderError("FMP_BAD_FIELD") from exc
    if not math.isfinite(low) or not math.isfinite(high) or low < 0 or high < low:
        raise FMPProviderError("FMP_BAD_FIELD")
    return low, high


def _profile_employee_count(row: Mapping[str, Any]) -> int | None:
    value = row.get("fullTimeEmployees")
    if value is None:
        return None
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise FMPProviderError("FMP_BAD_FIELD")


def _flexible_source_date(row: Mapping[str, Any], key: str) -> date_type | str | None:
    """Preserve source labels when public IndexConstituents permits date text."""
    if key not in row or row[key] is None:
        return None
    value = row[key]
    if isinstance(value, date_type) and not isinstance(value, datetime):
        return value
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise FMPProviderError("FMP_BAD_FIELD")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError:
        return value
    return parsed if parsed.isoformat() == value else value


def _source_date(row: Mapping[str, Any], key: str, *, required: bool = False) -> date_type | None:
    if key not in row or row[key] is None:
        if required:
            raise FMPProviderError("FMP_BAD_FIELD")
        return None
    value = row[key]
    if isinstance(value, datetime):
        raise FMPProviderError("FMP_BAD_FIELD")
    if isinstance(value, date_type):
        return value
    if not isinstance(value, str):
        raise FMPProviderError("FMP_BAD_FIELD")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError as exc:
        raise FMPProviderError("FMP_BAD_FIELD") from exc
    if parsed.isoformat() != value:
        raise FMPProviderError("FMP_BAD_FIELD")
    return parsed


def _base_contract(
    row: dict[str, Any],
    params: _MarketQueryBase,
    model: type[ModelT],
    values: dict[str, Any],
) -> ModelT:
    del row, params
    try:
        return model.model_validate(values)
    except Exception as exc:
        raise FMPProviderError("FMP_BAD_FIELD") from exc


class _MarketQueryBase(BaseQueryParams):
    """Provider-common fields shared by scalar and multi-symbol query roots."""

    source: Literal["fmp"] = "fmp"
    market: Literal["global", "us"] = "global"
    max_records: int = Field(default=5_000, ge=1, le=_MAX_RECORDS, strict=True)


class _MarketScalarQuery(_MarketQueryBase, QueryParams):
    """FMP query family whose symbol is a single ticker or null."""


class _MarketMultiQuery(_MarketQueryBase, MultiSymbolQueryParams):
    """FMP query family that preserves either one ticker or a ticker list."""


class MarketQuery(_MarketScalarQuery):
    """FMP global-market query identity."""

    market: Literal["global"] = "global"


class USMarketQuery(_MarketScalarQuery):
    """A US-only route whose service-injected market is part of identity."""

    market: Literal["us"] = "us"


class HistoricalQuery(_MarketMultiQuery):
    market: Literal["global"] = "global"
    symbol: str | list[str]
    start_date: date_type
    end_date: date_type
    interval: Literal["1d"] = "1d"
    max_requests: int = Field(default=8, ge=1, le=_MAX_SYMBOL_REQUESTS, strict=True)

    @field_validator("symbol", mode="before")
    @classmethod
    def _validate_symbols(cls, value: object) -> object:
        return _symbol_list(value)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _validate_dates(cls, value: object) -> object:
        return _date_bound(value)

    @model_validator(mode="after")
    def _validate_window(self) -> HistoricalQuery:
        symbols = self.symbol if isinstance(self.symbol, list) else [self.symbol]
        if len(symbols) > self.max_requests:
            raise ValueError("max_requests must cover every requested symbol")
        days = (self.end_date - self.start_date).days + 1
        if self.start_date > self.end_date or days > FMP_HISTORICAL_RESPONSE_LIMIT:
            raise ValueError("historical window must be ordered and no longer than 5000 days")
        if self.max_records > FMP_HISTORICAL_RESPONSE_LIMIT * len(symbols):
            raise ValueError("max_records exceeds the FMP per-symbol historical ceiling")
        return self


class SingleSeriesHistoricalQuery(HistoricalQuery):
    """Daily series whose public row contract does not carry its symbol."""

    @model_validator(mode="after")
    def _one_symbol_only(self) -> SingleSeriesHistoricalQuery:
        symbols = self.symbol if isinstance(self.symbol, list) else [self.symbol]
        if len(symbols) != 1:
            raise ValueError("this historical output contract accepts one symbol per query")
        return self


class CurrencyHistoricalQuery(SingleSeriesHistoricalQuery):
    """One FMP FX pair because the public bar contract has no symbol column."""

    @field_validator("symbol", mode="after")
    @classmethod
    def _validate_currency_pair(cls, value: str | list[str]) -> str | list[str]:
        symbols = value if isinstance(value, list) else [value]
        if any(
            len(symbol.replace("-", "")) != 6 or not symbol.replace("-", "").isalpha()
            for symbol in symbols
        ):
            raise ValueError("currency symbols must contain two three-letter currency codes")
        return value


class SymbolQuery(MarketQuery):
    symbol: str

    @field_validator("symbol", mode="before")
    @classmethod
    def _validate_symbol_value(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("this model accepts one symbol")
        return _validate_symbol(value)


class BoundedSymbolQuery(SymbolQuery):
    max_requests: int = Field(default=1, ge=1, le=8, strict=True)

    @model_validator(mode="after")
    def _one_symbol_request(self) -> BoundedSymbolQuery:
        if self.max_requests != 1:
            raise ValueError("single-symbol query max_requests must equal one")
        return self


class MultiSymbolQuery(_MarketMultiQuery):
    """Bounded symbol-list query for FMP routes with per-symbol responses."""

    market: Literal["global"] = "global"
    symbol: str | list[str]
    max_requests: int = Field(default=8, ge=1, le=_MAX_SYMBOL_REQUESTS, strict=True)

    @field_validator("symbol", mode="before")
    @classmethod
    def _validate_symbols(cls, value: object) -> object:
        return _symbol_list(value)

    @model_validator(mode="after")
    def _cover_symbols(self) -> MultiSymbolQuery:
        symbols = self.symbol if isinstance(self.symbol, list) else [self.symbol]
        if len(symbols) > self.max_requests:
            raise ValueError("max_requests must cover every requested symbol")
        return self


class ListQuery(MarketQuery):
    query: str | None = None
    limit: int = Field(default=100, ge=1, le=1_000, strict=True)
    max_records: int = Field(default=_MAX_LIST_RECORDS, ge=1, le=_MAX_LIST_RECORDS, strict=True)

    @field_validator("query")
    @classmethod
    def _validate_query_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if (
            not value
            or len(value) > 100
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("query must be non-empty text of at most 100 characters")
        return value


class CurrencyPairsQuery(MarketQuery):
    """Filter and bound the currency-pair directory request."""

    query: str | None = None
    limit: int = Field(default=500, ge=1, le=1_000, strict=True)
    max_records: int = Field(default=_MAX_LIST_RECORDS, ge=1, le=_MAX_LIST_RECORDS, strict=True)

    @field_validator("query")
    @classmethod
    def _validate_query_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if len(value) > 100 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("query must be at most 100 characters and contain no controls")
        return value or None


class CurrencySnapshotsQuery(MarketQuery):
    """Request bounded FX quotes in a declared base-currency perspective."""

    base: str = "USD"
    quote_type: Literal["direct", "indirect"] = "indirect"
    counter_currencies: list[str]
    max_requests: int = Field(default=10, ge=1, le=_MAX_REQUESTS, strict=True)
    max_records: int = Field(default=20, ge=1, le=20, strict=True)

    @field_validator("base", mode="before")
    @classmethod
    def _validate_base(cls, value: object) -> str:
        return _currency_code(value)

    @field_validator("counter_currencies", mode="before")
    @classmethod
    def _normalize_counter_codes(cls, value: object) -> object:
        if isinstance(value, str):
            values = value.split(",")
        elif isinstance(value, list):
            values = value
        else:
            raise ValueError("counter_currencies must be text or a list")
        codes = [_currency_code(item) for item in values]
        if not codes or len(codes) > 20 or len(codes) != len(set(codes)):
            raise ValueError("counter_currencies must contain 1 to 20 unique currencies")
        return codes

    @model_validator(mode="after")
    def _validate_counter_selection(self) -> CurrencySnapshotsQuery:
        if self.base in self.counter_currencies:
            raise ValueError("base must not appear in counter_currencies")
        if len(self.counter_currencies) > self.max_requests:
            raise ValueError("max_requests must cover every requested currency")
        return self


def _currency_code(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("currency code must be text")
    normalized = value.strip().upper()
    if not _CURRENCY_RE.fullmatch(normalized):
        raise ValueError("currency code must be three ASCII letters")
    return normalized


class MarketMoversQuery(USMarketQuery):
    limit: int = Field(default=100, ge=1, le=500, strict=True)
    sort: Literal["asc", "desc"] = "desc"
    max_records: int = Field(default=1_000, ge=1, le=5_000, strict=True)


class EquityScreenerQuery(MarketQuery):
    """Bound and validate the supported FMP company-screener filters."""

    market_cap_min: int | None = Field(default=None, ge=0, strict=True)
    market_cap_max: int | None = Field(default=None, ge=0, strict=True)
    price_min: float | None = Field(default=None, ge=0, strict=True)
    price_max: float | None = Field(default=None, ge=0, strict=True)
    beta_min: float | None = None
    beta_max: float | None = None
    volume_min: int | None = Field(default=None, ge=0, strict=True)
    volume_max: int | None = Field(default=None, ge=0, strict=True)
    avg_volume_min: int | None = Field(default=None, ge=0, strict=True)
    avg_volume_max: int | None = Field(default=None, ge=0, strict=True)
    dividend_min: float | None = Field(default=None, ge=0, strict=True)
    dividend_max: float | None = Field(default=None, ge=0, strict=True)
    sector: str | None = None
    industry: str | None = None
    exchange: str | None = None
    country: str | None = None
    is_etf: bool | None = None
    is_fund: bool | None = None
    is_actively_trading: bool | None = None
    limit: int = Field(default=500, ge=1, le=_MAX_SCREENER_LIMIT, strict=True)
    page: int = Field(default=0, ge=0, le=_MAX_SCREENER_PAGES - 1, strict=True)
    max_pages: int = Field(default=2, ge=1, le=_MAX_SCREENER_PAGES, strict=True)
    max_records: int = Field(default=1_000, ge=1, le=5_000, strict=True)

    @field_validator("beta_min", "beta_max", "dividend_min", "dividend_max", mode="before")
    @classmethod
    def _finite_filter(cls, value: object) -> object:
        if value is None:
            return None
        return _finite_or_none(value)

    @field_validator("sector", "industry", "exchange", "country")
    @classmethod
    def _clean_filter(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if (
            not value
            or len(value) > 100
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("screener text filter must be clean non-empty text")
        return value

    @model_validator(mode="after")
    def _validate_bounds(self) -> EquityScreenerQuery:
        for low_name, high_name in (
            ("market_cap_min", "market_cap_max"),
            ("price_min", "price_max"),
            ("beta_min", "beta_max"),
            ("volume_min", "volume_max"),
            ("avg_volume_min", "avg_volume_max"),
            ("dividend_min", "dividend_max"),
        ):
            low = getattr(self, low_name)
            high = getattr(self, high_name)
            if low is not None and high is not None and low > high:
                raise ValueError(f"{low_name} must not exceed {high_name}")
        return self


class MarketSnapshotsQuery(MarketQuery):
    """Select one exchange-wide or explicit-symbol quote snapshot request."""

    exchange: str | None = None
    symbols: list[str] | None = None
    max_records: int = Field(default=2_000, ge=1, le=_MAX_MARKET_ROWS, strict=True)

    @field_validator("exchange")
    @classmethod
    def _validate_exchange(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if (
            not 1 <= len(normalized) <= 16
            or not normalized.isascii()
            or not re.fullmatch(r"[A-Z0-9.-]+", normalized)
        ):
            raise ValueError("exchange contains unsupported characters")
        return normalized

    @field_validator("symbols", mode="before")
    @classmethod
    def _validate_symbols(cls, value: object) -> object:
        if value is None:
            return None
        return _symbol_list(value)

    @model_validator(mode="after")
    def _require_snapshot_selector(self) -> MarketSnapshotsQuery:
        if (self.exchange is None) == (self.symbols is None):
            raise ValueError("provide exactly one of exchange or symbols")
        if self.symbols is not None and len(self.symbols) > self.max_records:
            raise ValueError("max_records must cover every requested symbol")
        return self


class IndexConstituentsQuery(USMarketQuery):
    """Choose a supported US index and current or historical constituent data."""

    symbol: Literal["sp500", "nasdaq", "dowjones"] = "dowjones"
    historical: bool = Field(default=False, strict=True)
    max_records: int = Field(default=1_000, ge=1, le=5_000, strict=True)


class _MarketFetcher(Fetcher[QueryT, list[dict[str, Any]]], Generic[QueryT, RecordT]):
    """Shared query, governed request, completeness, and typed-output pipeline."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = None
    capability: ClassVar[Capability]
    query_model: ClassVar[type[QueryT]]
    record_model: ClassVar[type[RecordT]]
    endpoint: ClassVar[str]
    normalizer: ClassVar[staticmethod[[dict[str, Any], QueryT], RecordT]]
    key_fields: ClassVar[tuple[str, ...]] = ("symbol",)
    #: The offline witness seam; ``None`` keeps the governed shared HTTP client.
    http_transport: ClassVar[HttpGet | None] = None

    def transform_query(self, **kwargs: object) -> QueryT:
        return self.query_model.model_validate(kwargs)

    def request_specs(self, params: QueryT) -> list[tuple[str, dict[str, str]]]:
        return [(self.endpoint, self.request_params(params))]

    def request_params(self, params: QueryT) -> dict[str, str]:
        del params
        return {}

    def extract_data(self, params: QueryT, ctx: FetchContext) -> list[dict[str, Any]]:
        specs = self.request_specs(params)
        maximum_requests = getattr(params, "max_requests", _MAX_REQUESTS)
        if len(specs) > maximum_requests:
            raise FMPQueryLimitError(
                "FMP_REQUEST_BUDGET_EXCEEDED",
                limit_kind="max_requests",
                maximum=maximum_requests,
                requests_made=0,
            )
        raw: list[dict[str, Any]] = []
        for endpoint, request_params in specs:
            rows = fetch_array(endpoint, request_params, ctx.timeout, self.http_transport)
            raw.extend(rows)
            if len(raw) > params.max_records:
                raise FMPQueryLimitError(
                    "FMP_INCOMPLETE_RECORD_BUDGET",
                    limit_kind="max_records",
                    maximum=params.max_records,
                    requests_made=len(raw),
                )
        return raw

    def transform_data(self, raw: list[dict[str, Any]], params: QueryT) -> FetchResult:
        if len(raw) > params.max_records:
            raise FMPQueryLimitError(
                "FMP_INCOMPLETE_RECORD_BUDGET",
                limit_kind="max_records",
                maximum=params.max_records,
                requests_made=len(raw),
            )
        records: list[RecordT] = [self.normalizer(item, params) for item in raw]
        seen: set[tuple[object, ...]] = set()
        for record in records:
            key = tuple(getattr(record, field) for field in self.key_fields)
            if key in seen:
                raise FMPProviderError("FMP_DUPLICATE_ROW")
            seen.add(key)
        if isinstance(params, MultiSymbolQuery):
            expected = set(params.symbol if isinstance(params.symbol, list) else [params.symbol])
            observed = {_record_symbol(record) for record in records}
            if expected - observed:
                raise FMPProviderError("FMP_MISSING_DATA")
            if observed - expected:
                raise FMPProviderError("FMP_UNREQUESTED_SYMBOL")
        if isinstance(params, MarketSnapshotsQuery) and params.symbols is not None:
            expected = set(params.symbols)
            observed = {_record_symbol(record) for record in records}
            if expected - observed:
                raise FMPProviderError("FMP_MISSING_DATA")
            if observed - expected:
                raise FMPProviderError("FMP_UNREQUESTED_SYMBOL")
        return tuple(records)


def _record_symbol(record: ContractModel) -> str:
    """Return a validated string symbol for routes that promise symbol rows."""
    symbol = getattr(record, "symbol", None)
    if not isinstance(symbol, str):
        raise FMPProviderError("FMP_BAD_FIELD")
    return symbol


def _history_record(model: type[ModelT], raw: dict[str, Any], params: _MarketQueryBase) -> ModelT:
    symbol = _required_text(raw, "symbol").upper()
    observed_date = _source_date(raw, "date", required=True)
    if observed_date is None:
        raise FMPProviderError("FMP_BAD_FIELD")
    if isinstance(params, HistoricalQuery):
        requested = params.symbol if isinstance(params.symbol, list) else [params.symbol]
        accepted = {
            item.replace("-", "") if model is CurrencyHistorical else item for item in requested
        }
        if symbol not in accepted:
            raise FMPProviderError("FMP_UNREQUESTED_SYMBOL")
        if not params.start_date <= observed_date <= params.end_date:
            raise FMPProviderError("FMP_OUT_OF_WINDOW_ROW")
    values: dict[str, Any] = {
        "date": observed_date,
        "open": _number(raw, "open"),
        "high": _number(raw, "high"),
        "low": _number(raw, "low"),
        "close": _number(raw, "close", required=True),
        "volume": _number(raw, "volume"),
        "vwap": _number(raw, "vwap"),
        "change": _number(raw, "change"),
        "change_percent": _number(raw, "changePercent"),
    }
    if model is IndexHistorical:
        values["symbol"] = symbol
    return _base_contract(raw, params, model, values)


class _HistoricalFetcher(
    _MarketFetcher[HistoricalQueryT, HistoricalRecordT],
    Generic[HistoricalQueryT, HistoricalRecordT],
):
    """Shared bounded request construction for FMP daily historical rows."""

    endpoint = "historical-price-eod/full"

    def request_specs(self, params: HistoricalQueryT) -> list[tuple[str, dict[str, str]]]:
        """Build full-history requests with the validated date bounds."""
        symbols = params.symbol if isinstance(params.symbol, list) else [params.symbol]
        return [
            (
                self.endpoint,
                {
                    "symbol": symbol,
                    "from": params.start_date.isoformat(),
                    "to": params.end_date.isoformat(),
                },
            )
            for symbol in symbols
        ]


def _normalize_crypto_historical(
    row: dict[str, Any], query: SingleSeriesHistoricalQuery
) -> CryptoHistorical:
    return _history_record(CryptoHistorical, row, query)


def _normalize_currency_historical(
    row: dict[str, Any], query: CurrencyHistoricalQuery
) -> CurrencyHistorical:
    return _history_record(CurrencyHistorical, row, query)


def _normalize_index_historical(row: dict[str, Any], query: HistoricalQuery) -> IndexHistorical:
    return _history_record(IndexHistorical, row, query)


class CryptoHistoricalFetcher(_HistoricalFetcher[SingleSeriesHistoricalQuery, CryptoHistorical]):
    """Fetch daily crypto EOD bars for one bounded symbol and date range."""

    canonical_model = "CryptoHistorical"
    query_model = SingleSeriesHistoricalQuery
    record_model = CryptoHistorical
    row_model: ClassVar[type[ContractModel]] = CryptoHistorical
    normalizer = staticmethod(_normalize_crypto_historical)
    key_fields = ("date",)
    capability = Capability(
        asset_class="crypto",
        domain="fmp_crypto_historical",
        period="historical",
        market="global",
        source="fmp",
        verified=False,
    )


class CurrencyHistoricalFetcher(_HistoricalFetcher[CurrencyHistoricalQuery, CurrencyHistorical]):
    """Fetch daily forex EOD bars for one bounded currency pair."""

    canonical_model = "CurrencyHistorical"
    query_model = CurrencyHistoricalQuery
    record_model = CurrencyHistorical
    row_model: ClassVar[type[ContractModel]] = CurrencyHistorical
    endpoint = "historical-price-eod/full"
    key_fields = ("date",)
    capability = Capability(
        asset_class="currency",
        domain="fmp_currency_historical",
        period="historical",
        market="global",
        source="fmp",
        verified=False,
    )
    normalizer = staticmethod(_normalize_currency_historical)

    def request_specs(self, params: CurrencyHistoricalQuery) -> list[tuple[str, dict[str, str]]]:
        """Build FMP pair requests using compact six-letter symbols."""
        symbols = params.symbol if isinstance(params.symbol, list) else [params.symbol]
        normalized: list[str] = []
        for symbol in symbols:
            compact = symbol.replace("-", "")
            if len(compact) != 6 or not compact.isalpha():
                raise ValueError("currency symbols must contain two three-letter currency codes")
            normalized.append(compact)
        return [
            (
                self.endpoint,
                {
                    "symbol": symbol,
                    "from": params.start_date.isoformat(),
                    "to": params.end_date.isoformat(),
                },
            )
            for symbol in normalized
        ]


class IndexHistoricalFetcher(_HistoricalFetcher[HistoricalQuery, IndexHistorical]):
    """Fetch daily index EOD bars for the requested symbol set."""

    canonical_model = "IndexHistorical"
    record_model = IndexHistorical
    row_model: ClassVar[type[ContractModel]] = IndexHistorical
    query_model = HistoricalQuery
    endpoint = "historical-price-eod/full"
    key_fields = ("symbol", "date")
    capability = Capability(
        asset_class="index",
        domain="fmp_index_historical",
        period="historical",
        market="global",
        source="fmp",
        verified=False,
    )
    normalizer = staticmethod(_normalize_index_historical)


def _normalize_crypto_search(raw: dict[str, Any], params: _MarketQueryBase) -> CryptoSearch:
    values = {
        "symbol": _required_text(raw, "symbol"),
        "name": _optional_text(raw.get("name")),
        "exchange": _optional_text(raw.get("exchange")),
        "ico_date": _source_date(raw, "icoDate"),
        "circulating_supply": _number(raw, "circulatingSupply"),
        "total_supply": _number(raw, "totalSupply"),
    }
    return _base_contract(raw, params, CryptoSearch, values)


class CryptoSearchFetcher(_MarketFetcher[ListQuery, CryptoSearch]):
    """Search the crypto directory locally after a bounded list request."""

    canonical_model = "CryptoSearch"
    query_model = ListQuery
    record_model = CryptoSearch
    row_model: ClassVar[type[ContractModel]] = CryptoSearch
    endpoint = "cryptocurrency-list"
    normalizer = staticmethod(_normalize_crypto_search)
    capability = Capability(
        asset_class="crypto",
        domain="fmp_crypto_search",
        period="directory",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_data(self, raw: list[dict[str, Any]], params: ListQuery) -> FetchResult:
        """Apply the local text query and return validated directory rows."""
        if len(raw) > params.max_records:
            raise FMPQueryLimitError(
                "FMP_INCOMPLETE_RECORD_BUDGET",
                limit_kind="max_records",
                maximum=params.max_records,
                requests_made=1,
            )
        query = params.query.casefold() if params.query is not None else None
        matches: list[CryptoSearch] = []
        for item in raw:
            record = self.normalizer(item, params)
            haystack = (record.symbol + " " + (record.name or "")).casefold()
            if query is None or query in haystack:
                matches.append(record)
        return tuple(matches[: params.limit])


def _normalize_currency_pair(raw: dict[str, Any], params: _MarketQueryBase) -> CurrencyPair:
    from_currency = _required_text(raw, "fromCurrency")
    to_currency = _required_text(raw, "toCurrency")
    from_name = _required_text(raw, "fromName")
    to_name = _required_text(raw, "toName")
    values = {
        "symbol": _required_text(raw, "symbol"),
        "name": f"{from_name} / {to_name}",
        "from_currency": from_currency,
        "to_currency": to_currency,
        "from_name": from_name,
        "to_name": to_name,
    }
    return _base_contract(raw, params, CurrencyPair, values)


class CurrencyPairsFetcher(_MarketFetcher[CurrencyPairsQuery, CurrencyPair]):
    """Fetch and locally filter the FMP forex-pair directory."""

    canonical_model = "CurrencyPairs"
    query_model = CurrencyPairsQuery
    record_model = CurrencyPair
    row_model: ClassVar[type[ContractModel]] = CurrencyPair
    endpoint = "forex-list"
    normalizer = staticmethod(_normalize_currency_pair)
    key_fields = ("symbol",)
    capability = Capability(
        asset_class="currency",
        domain="fmp_currency_pairs",
        period="directory",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_data(self, raw: list[dict[str, Any]], params: CurrencyPairsQuery) -> FetchResult:
        """Filter directory rows against the requested names or currency codes."""
        if len(raw) > params.max_records:
            raise FMPQueryLimitError(
                "FMP_INCOMPLETE_RECORD_BUDGET",
                limit_kind="max_records",
                maximum=params.max_records,
                requests_made=1,
            )
        query = params.query.casefold() if params.query else None
        result: list[CurrencyPair] = []
        for item in raw:
            record = self.normalizer(item, params)
            haystack = " ".join(
                (record.symbol, record.name or "", record.from_currency, record.to_currency)
            ).casefold()
            if query is None or query in haystack:
                result.append(record)
        return tuple(result[: params.limit])


def _normalize_currency_snapshot(raw: dict[str, Any], params: _MarketQueryBase) -> CurrencySnapshot:
    if not isinstance(params, CurrencySnapshotsQuery):
        raise FMPProviderError("FMP_QUERY_MODEL_MISMATCH")
    symbol = _required_text(raw, "symbol").upper()
    # FMP's FX quote symbols encode foreign currency first, domestic second.
    expected_symbols = {counter + params.base: counter for counter in params.counter_currencies}
    if symbol not in expected_symbols:
        raise FMPProviderError("FMP_UNREQUESTED_SYMBOL")
    counter = expected_symbols[symbol]
    direct = params.quote_type == "direct"
    price = _number(raw, "price", required=True)
    if price is None:
        raise FMPProviderError("FMP_BAD_FIELD")
    current_rate = float(price) if direct else _inverse(price)
    open_value = _number(raw, "open")
    high_value = _number(raw, "dayHigh")
    low_value = _number(raw, "dayLow")
    previous = _number(raw, "previousClose")
    avg50 = _number(raw, "priceAvg50")
    avg200 = _number(raw, "priceAvg200")
    year_high = _number(raw, "yearHigh")
    year_low = _number(raw, "yearLow")
    change = _number(raw, "change")
    change_percent = _maybe_alias_number(raw, "changePercentage", "changesPercentage")
    if not direct:
        open_value = _inverse_or_none(open_value)
        high_value, low_value = _inverse_or_none(low_value), _inverse_or_none(high_value)
        previous = _inverse_or_none(previous)
        avg50 = _inverse_or_none(avg50)
        avg200 = _inverse_or_none(avg200)
        year_high, year_low = _inverse_or_none(year_low), _inverse_or_none(year_high)
        change = None if previous is None else current_rate - previous
        if previous is None or previous == 0 or change is None:
            change_percent = None
        else:
            change_percent = (change / previous) * 100
    values = {
        "base_currency": params.base,
        "counter_currency": counter,
        "last_rate": current_rate,
        "open": open_value,
        "high": high_value,
        "low": low_value,
        "close": current_rate,
        "volume": _integer(raw, "volume"),
        "prev_close": previous,
        "change": change,
        "change_percent": change_percent,
        "ma50": avg50,
        "ma200": avg200,
        "year_high": year_high,
        "year_low": year_low,
        "last_rate_timestamp": _timestamp_seconds(raw.get("timestamp")),
    }
    return _base_contract(raw, params, CurrencySnapshot, values)


def _inverse(value: int | float) -> float:
    if value <= 0:
        raise FMPProviderError("FMP_INVALID_FX_RATE")
    return 1.0 / float(value)


def _inverse_or_none(value: int | float | None) -> float | None:
    return None if value is None else _inverse(value)


def _timestamp_seconds(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FMPProviderError("FMP_BAD_FIELD")
    from datetime import timezone

    return datetime.fromtimestamp(value, tz=timezone.utc)


class CurrencySnapshotsFetcher(_MarketFetcher[CurrencySnapshotsQuery, CurrencySnapshot]):
    """Fetch bounded live FX quotes and normalize rates to the requested base."""

    canonical_model = "CurrencySnapshots"
    query_model = CurrencySnapshotsQuery
    record_model = CurrencySnapshot
    row_model: ClassVar[type[ContractModel]] = CurrencySnapshot
    endpoint = "quote"
    normalizer = staticmethod(_normalize_currency_snapshot)
    key_fields = ("base_currency", "counter_currency")
    capability = Capability(
        asset_class="currency",
        domain="fmp_currency_snapshots",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def request_specs(self, params: CurrencySnapshotsQuery) -> list[tuple[str, dict[str, str]]]:
        """Create one quote request per requested counter currency."""
        return [
            ("quote", {"symbol": counter + params.base}) for counter in params.counter_currencies
        ]

    def transform_data(
        self, raw: list[dict[str, Any]], params: CurrencySnapshotsQuery
    ) -> FetchResult:
        """Validate quote completeness and orient direct or inverse rates."""
        expected = {counter + params.base for counter in params.counter_currencies}
        observed: set[str] = set()
        result: list[CurrencySnapshot] = []
        for item in raw:
            symbol = _required_text(item, "symbol").upper()
            if symbol in observed:
                raise FMPProviderError("FMP_DUPLICATE_ROW")
            observed.add(symbol)
            result.append(self.normalizer(item, params))
        missing = expected - observed
        if missing:
            raise FMPProviderError("FMP_MISSING_DATA")
        return tuple(result)


def _market_quote_record(
    raw: dict[str, Any], params: _MarketQueryBase, model: type[ModelT]
) -> ModelT:
    percent = _maybe_alias_number(raw, "changePercentage", "changesPercentage", required=True)
    values = {
        "symbol": _required_text(raw, "symbol"),
        "name": _maybe_alias_text(raw, "name", "companyName"),
        "price": _number(raw, "price", required=True),
        "change": _number(raw, "change", required=True),
        "percent_change": percent,
        "volume": _number(raw, "volume"),
        "exchange": _required_text(raw, "exchange"),
    }
    return _base_contract(raw, params, model, values)


_MoverRecord = EquityActive | EquityGainers | EquityLosers
_MoverNormalizer = Callable[[dict[str, Any], MarketMoversQuery], _MoverRecord]


def _mover(row: dict[str, Any], params: _MarketQueryBase) -> EquityActive:
    return _market_quote_record(row, params, EquityActive)


class EquityActiveFetcher(_MarketFetcher[MarketMoversQuery, EquityActive]):
    """Fetch US active stocks and normalize the documented quote summary."""

    canonical_model = "EquityActive"
    query_model = MarketMoversQuery
    record_model = EquityActive
    # contract_model('fmp_equity_active') -> opendata.data.models.fmp_market.EquityActive
    row_model: ClassVar[type[ContractModel]] = EquityActive
    endpoint = "most-actives"
    normalizer = staticmethod(_mover)
    capability = Capability(
        asset_class="stock",
        domain="fmp_equity_active",
        period="snapshot",
        market="us",
        source="fmp",
        verified=False,
    )

    def transform_data(self, raw: list[dict[str, Any]], params: MarketMoversQuery) -> FetchResult:
        """Validate active-stock rows against the bounded result limit."""
        return _movers(raw, params, self.normalizer, model=EquityActive)


def _mover_values(model: type[ModelT], raw: dict[str, Any], params: _MarketQueryBase) -> ModelT:
    return _market_quote_record(raw, params, model)


def _gainer(raw: dict[str, Any], params: _MarketQueryBase) -> EquityGainers:
    return _mover_values(EquityGainers, raw, params)


def _loser(raw: dict[str, Any], params: _MarketQueryBase) -> EquityLosers:
    return _mover_values(EquityLosers, raw, params)


def _movers(
    raw: list[dict[str, Any]],
    params: MarketMoversQuery,
    normalizer: _MoverNormalizer,
    *,
    model: type[_MoverRecord],
) -> FetchResult:
    if len(raw) > params.max_records:
        raise FMPQueryLimitError(
            "FMP_INCOMPLETE_RECORD_BUDGET",
            limit_kind="max_records",
            maximum=params.max_records,
            requests_made=1,
        )
    records: list[_MoverRecord] = [normalizer(item, params) for item in raw]

    def sort_value(item: EquityActive | EquityGainers | EquityLosers) -> float | int | None:
        if model is EquityActive:
            return item.volume
        if model is EquityLosers:
            return -item.percent_change
        return item.percent_change

    records.sort(
        key=lambda item: (sort_value(item) is None, sort_value(item)),
        reverse=params.sort == "desc",
    )
    keys: set[object] = set()
    for record in records:
        symbol = _record_symbol(record)
        if symbol in keys:
            raise FMPProviderError("FMP_DUPLICATE_ROW")
        keys.add(symbol)
    return tuple(records[: params.limit])


class EquityGainersFetcher(_MarketFetcher[MarketMoversQuery, EquityGainers]):
    """Fetch the bounded list of US equity gainers."""

    canonical_model = "EquityGainers"
    query_model = MarketMoversQuery
    record_model = EquityGainers
    # contract_model('fmp_equity_gainers') -> opendata.data.models.fmp_market.EquityGainers
    row_model: ClassVar[type[ContractModel]] = EquityGainers
    endpoint = "biggest-gainers"
    normalizer = staticmethod(_gainer)
    capability = Capability(
        asset_class="stock",
        domain="fmp_equity_gainers",
        period="snapshot",
        market="us",
        source="fmp",
        verified=False,
    )

    def transform_data(self, raw: list[dict[str, Any]], params: MarketMoversQuery) -> FetchResult:
        """Validate gainer rows against the requested order and limit."""
        return _movers(raw, params, self.normalizer, model=EquityGainers)


class EquityLosersFetcher(_MarketFetcher[MarketMoversQuery, EquityLosers]):
    """Fetch the bounded list of US equity losers."""

    canonical_model = "EquityLosers"
    query_model = MarketMoversQuery
    record_model = EquityLosers
    row_model: ClassVar[type[ContractModel]] = EquityLosers
    endpoint = "biggest-losers"
    normalizer = staticmethod(_loser)
    capability = Capability(
        asset_class="stock",
        domain="fmp_equity_losers",
        period="snapshot",
        market="us",
        source="fmp",
        verified=False,
    )

    def transform_data(self, raw: list[dict[str, Any]], params: MarketMoversQuery) -> FetchResult:
        """Validate loser rows against the requested order and limit."""
        return _movers(raw, params, self.normalizer, model=EquityLosers)


def _normalize_peers(raw: dict[str, Any], params: _MarketQueryBase) -> EquityPeers:
    return _base_contract(
        raw,
        params,
        EquityPeers,
        {
            "symbol": _required_text(raw, "symbol"),
            "name": _maybe_alias_text(raw, "companyName", "name", required=True),
            "price": _number(raw, "price"),
            "market_cap": _maybe_alias_integer(raw, "mktCap", "marketCap"),
        },
    )


class EquityPeersFetcher(_MarketFetcher[BoundedSymbolQuery, EquityPeers]):
    """Fetch peer companies for one requested equity symbol."""

    canonical_model = "EquityPeers"
    query_model = BoundedSymbolQuery
    record_model = EquityPeers
    row_model: ClassVar[type[ContractModel]] = EquityPeers
    endpoint = "stock-peers"
    normalizer = staticmethod(_normalize_peers)
    capability = Capability(
        asset_class="stock",
        domain="fmp_equity_peers",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def request_params(self, params: BoundedSymbolQuery) -> dict[str, str]:
        """Pass the validated symbol to the peers route."""
        return {"symbol": params.symbol}


def _normalize_equity_info(raw: dict[str, Any], params: _MarketQueryBase) -> EquityInfo:
    low, high = _profile_range(raw)
    return _base_contract(
        raw,
        params,
        EquityInfo,
        {
            "symbol": _required_text(raw, "symbol"),
            "name": _optional_text(raw.get("companyName")),
            "cik": _optional_text(raw.get("cik")),
            "cusip": _optional_text(raw.get("cusip")),
            "isin": _optional_text(raw.get("isin")),
            "stock_exchange": _optional_text(raw.get("exchangeFullName")),
            "long_description": _optional_text(raw.get("description")),
            "company_url": _optional_text(raw.get("website")),
            "ceo": _optional_text(raw.get("ceo")),
            "sector": _optional_text(raw.get("sector")),
            "industry_category": _optional_text(raw.get("industry")),
            "business_address": _optional_text(raw.get("address")),
            "business_phone_no": _optional_text(raw.get("phone")),
            "hq_address_city": _optional_text(raw.get("city")),
            "hq_state": _optional_text(raw.get("state")),
            "hq_address_postal_code": _optional_text(raw.get("zip")),
            "hq_country": _optional_text(raw.get("country")),
            "employees": _profile_employee_count(raw),
            "image": _optional_text(raw.get("image")),
            "currency": _optional_text(raw.get("currency")),
            "market_cap": _integer(raw, "marketCap"),
            "last_price": _number(raw, "price"),
            "year_low": low,
            "year_high": high,
            "volume_avg": _integer(raw, "averageVolume"),
            # FMP supplies lastDividend but not its regular payment frequency.
            "annualized_dividend_amount": None,
            "beta": _number(raw, "beta"),
            "is_etf": _strict_bool(raw, "isEtf"),
            "is_actively_trading": _strict_bool(raw, "isActivelyTrading"),
            "is_adr": _strict_bool(raw, "isAdr"),
            "is_fund": _strict_bool(raw, "isFund"),
        },
    )


class EquityInfoFetcher(_MarketFetcher[MultiSymbolQuery, EquityInfo]):
    """Fetch company profile fields for each requested equity symbol."""

    canonical_model = "EquityInfo"
    query_model = MultiSymbolQuery
    record_model = EquityInfo
    row_model: ClassVar[type[ContractModel]] = EquityInfo
    endpoint = "profile"
    normalizer = staticmethod(_normalize_equity_info)
    capability = Capability(
        asset_class="stock",
        domain="fmp_equity_info",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def request_specs(self, params: MultiSymbolQuery) -> list[tuple[str, dict[str, str]]]:
        """Build one profile lookup request per bounded input symbol."""
        symbols = params.symbol if isinstance(params.symbol, list) else [params.symbol]
        return [(self.endpoint, {"symbol": symbol}) for symbol in symbols]


def _normalize_equity_screener(raw: dict[str, Any], params: _MarketQueryBase) -> EquityScreener:
    values = {
        "symbol": _required_text(raw, "symbol"),
        "name": _maybe_alias_text(raw, "companyName", "name"),
        "market_cap": _integer(raw, "marketCap"),
        "sector": _optional_text(raw.get("sector")),
        "industry": _optional_text(raw.get("industry")),
        "beta": _number(raw, "beta"),
        "price": _number(raw, "price"),
        "last_annual_dividend": _number(raw, "lastAnnualDividend"),
        "volume": _integer(raw, "volume"),
        "exchange": _optional_text(raw.get("exchangeShortName")),
        "exchange_name": _optional_text(raw.get("exchange")),
        "country": _optional_text(raw.get("country")),
        "is_etf": _optional_bool(raw, "isEtf"),
        "is_fund": _optional_bool(raw, "isFund"),
        "actively_trading": _optional_bool(raw, "isActivelyTrading"),
    }
    return _base_contract(raw, params, EquityScreener, values)


def _strict_bool(raw: Mapping[str, Any], key: str) -> bool:
    value = _required_value(raw, key)
    if type(value) is not bool:
        raise FMPProviderError("FMP_BAD_FIELD")
    return value


def _optional_bool(raw: Mapping[str, Any], key: str) -> bool | None:
    if key not in raw or raw[key] is None:
        return None
    value = raw[key]
    if type(value) is not bool:
        raise FMPProviderError("FMP_BAD_FIELD")
    return value


class EquityScreenerFetcher(_MarketFetcher[EquityScreenerQuery, EquityScreener]):
    """Fetch screener pages up to the caller's page budget."""

    canonical_model = "EquityScreener"
    query_model = EquityScreenerQuery
    record_model = EquityScreener
    row_model: ClassVar[type[ContractModel]] = EquityScreener
    endpoint = "company-screener"
    normalizer = staticmethod(_normalize_equity_screener)
    capability = Capability(
        asset_class="stock",
        domain="fmp_equity_screener",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def request_specs(self, params: EquityScreenerQuery) -> list[tuple[str, dict[str, str]]]:
        """Translate validated filter names into FMP Stable route parameters."""
        names = {
            "market_cap_min": "marketCapMoreThan",
            "market_cap_max": "marketCapLowerThan",
            "beta_min": "betaMoreThan",
            "beta_max": "betaLowerThan",
            "price_min": "priceMoreThan",
            "price_max": "priceLowerThan",
            "dividend_min": "dividendMoreThan",
            "dividend_max": "dividendLowerThan",
            "volume_min": "volumeMoreThan",
            "volume_max": "volumeLowerThan",
            "avg_volume_min": "avgVolumeMoreThan",
            "avg_volume_max": "avgVolumeLowerThan",
            "sector": "sector",
            "industry": "industry",
            "exchange": "exchange",
            "country": "country",
            "is_etf": "isEtf",
            "is_fund": "isFund",
            "is_actively_trading": "isActivelyTrading",
        }
        values: dict[str, str] = {"limit": str(params.limit)}
        for field_name, endpoint_name in names.items():
            value = getattr(params, field_name)
            if value is not None:
                values[endpoint_name] = (
                    str(value).lower() if isinstance(value, bool) else str(value)
                )
        return [
            (self.endpoint, {**values, "page": str(params.page + offset)})
            for offset in range(params.max_pages)
        ]

    def extract_data(self, params: EquityScreenerQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Read only the page budget and fail if the result may be truncated."""
        raw: list[dict[str, Any]] = []
        specs = self.request_specs(params)
        for index, (endpoint, request_params) in enumerate(specs):
            rows = fetch_array(endpoint, request_params, ctx.timeout, self.http_transport)
            raw.extend(rows)
            if len(raw) > params.max_records:
                raise FMPQueryLimitError(
                    "FMP_INCOMPLETE_RECORD_BUDGET",
                    limit_kind="max_records",
                    maximum=params.max_records,
                    requests_made=index + 1,
                )
            if len(rows) < params.limit:
                break
        else:
            raise FMPQueryLimitError(
                "FMP_INCOMPLETE_PAGE_BUDGET",
                limit_kind="max_pages",
                maximum=params.max_pages,
                requests_made=len(specs),
            )
        return raw


def _normalize_market_snapshot(raw: dict[str, Any], params: _MarketQueryBase) -> MarketSnapshot:
    return _base_contract(
        raw,
        params,
        MarketSnapshot,
        {
            "exchange": _optional_text(raw.get("exchange")),
            "symbol": _required_text(raw, "symbol"),
            "name": _optional_text(raw.get("name", raw.get("companyName"))),
            "open": _number(raw, "open"),
            "high": _number(raw, "dayHigh"),
            "low": _number(raw, "dayLow"),
            "close": _number(raw, "price", required=True),
            "volume": _integer(raw, "volume"),
            "prev_close": _number(raw, "previousClose"),
            "change": _number(raw, "change"),
            "change_percent": _maybe_alias_number(raw, "changePercentage", "changesPercentage"),
            "ma50": _number(raw, "priceAvg50"),
            "ma200": _number(raw, "priceAvg200"),
            "year_high": _number(raw, "yearHigh"),
            "year_low": _number(raw, "yearLow"),
            "market_cap": _integer(raw, "marketCap"),
            "last_price_timestamp": _timestamp_seconds(raw.get("timestamp")),
        },
    )


class MarketSnapshotsFetcher(_MarketFetcher[MarketSnapshotsQuery, MarketSnapshot]):
    """Fetch quotes for an exchange or an explicit bounded symbol set."""

    canonical_model = "MarketSnapshots"
    query_model = MarketSnapshotsQuery
    record_model = MarketSnapshot
    row_model: ClassVar[type[ContractModel]] = MarketSnapshot
    endpoint = "batch-quote"
    normalizer = staticmethod(_normalize_market_snapshot)
    capability = Capability(
        asset_class="stock",
        domain="fmp_market_snapshots",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def request_specs(self, params: MarketSnapshotsQuery) -> list[tuple[str, dict[str, str]]]:
        """Select the exchange-batch or symbol-batch quote route."""
        if params.exchange is not None:
            return [("batch-exchange-quote", {"exchange": params.exchange, "short": "false"})]
        if params.symbols is None:
            raise FMPProviderError("FMP_QUERY_SELECTOR_MISSING")
        return [("batch-quote", {"symbols": ",".join(params.symbols)})]


def _normalize_price_performance(raw: dict[str, Any], params: _MarketQueryBase) -> PricePerformance:
    values = {
        "symbol": _required_text(raw, "symbol"),
        "one_day": _number(raw, "1D"),
        "wtd": None,
        # FMP defines 5D as a five-trading-day move, using today's price and
        # the open from five trading days ago; the public model's one_week is
        # the closest matching period, but this methodology is documented.
        "one_week": _number(raw, "5D"),
        "mtd": None,
        "one_month": _number(raw, "1M"),
        "qtd": None,
        "three_month": _number(raw, "3M"),
        "six_month": _number(raw, "6M"),
        "ytd": _number(raw, "ytd"),
        "one_year": _number(raw, "1Y"),
        "two_year": None,
        "three_year": _number(raw, "3Y"),
        "four_year": None,
        "five_year": _number(raw, "5Y"),
        "ten_year": _number(raw, "10Y"),
        "max": _number(raw, "max"),
    }
    return _base_contract(raw, params, PricePerformance, values)


class PricePerformanceFetcher(_MarketFetcher[MultiSymbolQuery, PricePerformance]):
    """Fetch FMP percentage changes across its documented time horizons."""

    canonical_model = "PricePerformance"
    query_model = MultiSymbolQuery
    record_model = PricePerformance
    row_model: ClassVar[type[ContractModel]] = PricePerformance
    endpoint = "stock-price-change"
    normalizer = staticmethod(_normalize_price_performance)
    capability = Capability(
        asset_class="stock",
        domain="fmp_price_performance",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def request_specs(self, params: MultiSymbolQuery) -> list[tuple[str, dict[str, str]]]:
        """Build one price-change request per requested symbol."""
        symbols = params.symbol if isinstance(params.symbol, list) else [params.symbol]
        return [(self.endpoint, {"symbol": symbol}) for symbol in symbols]


def _normalize_share_statistics(raw: dict[str, Any], params: _MarketQueryBase) -> ShareStatistics:
    values: dict[str, Any] = {
        "symbol": _required_text(raw, "symbol"),
        "date": raw.get("date"),
        "free_float": _number(raw, "freeFloat"),
        "float_shares": _number(raw, "floatShares"),
        "outstanding_shares": _number(raw, "outstandingShares"),
        "url": _optional_text(raw.get("source")),
    }
    return _base_contract(raw, params, ShareStatistics, values)


class ShareStatisticsFetcher(_MarketFetcher[MultiSymbolQuery, ShareStatistics]):
    """Fetch current float and outstanding-share statistics per symbol."""

    canonical_model = "ShareStatistics"
    query_model = MultiSymbolQuery
    record_model = ShareStatistics
    row_model: ClassVar[type[ContractModel]] = ShareStatistics
    endpoint = "shares-float"
    normalizer = staticmethod(_normalize_share_statistics)
    capability = Capability(
        asset_class="stock",
        domain="fmp_share_statistics",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def request_specs(self, params: MultiSymbolQuery) -> list[tuple[str, dict[str, str]]]:
        """Build one shares-float lookup per bounded input symbol."""
        symbols = params.symbol if isinstance(params.symbol, list) else [params.symbol]
        return [(self.endpoint, {"symbol": symbol}) for symbol in symbols]


def _normalize_index_constituent(
    raw: dict[str, Any], params: _MarketQueryBase
) -> FmpIndexConstituent:
    if isinstance(params, IndexConstituentsQuery) and params.historical:
        values = {
            "symbol": _required_text(raw, "symbol"),
            "name": _optional_text(raw.get("addedSecurity")),
            "sector": None,
            "industry": None,
            "headquarter": None,
            "date_added": _flexible_source_date(raw, "dateAdded"),
            "cik": None,
            "founded": None,
            "removed_symbol": _optional_text(raw.get("removedTicker")),
            "removed_name": _optional_text(raw.get("removedSecurity")),
            "reason": _optional_text(raw.get("reason")),
            "date": _flexible_source_date(raw, "date"),
        }
        return _base_contract(raw, params, FmpIndexConstituent, values)
    values = {
        "symbol": _required_text(raw, "symbol"),
        "name": _optional_text(raw.get("name")),
        "sector": _optional_text(raw.get("sector")),
        "industry": _optional_text(raw.get("subSector")),
        "headquarter": _optional_text(raw.get("headQuarter")),
        "date_added": _flexible_source_date(raw, "dateFirstAdded"),
        "cik": _optional_text(raw.get("cik")),
        "founded": _flexible_source_date(raw, "founded"),
        "removed_symbol": None,
        "removed_name": None,
        "reason": None,
        "date": None,
    }
    return _base_contract(raw, params, FmpIndexConstituent, values)


class IndexConstituentsFetcher(_MarketFetcher[IndexConstituentsQuery, FmpIndexConstituent]):
    """Fetch current index members or historical membership events."""

    canonical_model = "IndexConstituents"
    query_model = IndexConstituentsQuery
    record_model = FmpIndexConstituent
    row_model: ClassVar[type[ContractModel]] = FmpIndexConstituent
    endpoint = "sp500-constituent"
    normalizer = staticmethod(_normalize_index_constituent)
    key_fields = ("symbol", "date")
    capability = Capability(
        asset_class="index",
        domain="fmp_index_constituents",
        period="snapshot",
        market="us",
        source="fmp",
        verified=False,
    )

    def request_specs(self, params: IndexConstituentsQuery) -> list[tuple[str, dict[str, str]]]:
        """Select the current or historical route for the validated index."""
        index = {
            "sp500": "sp500-constituent",
            "nasdaq": "nasdaq-constituent",
            "dowjones": "dowjones-constituent",
        }[params.symbol]
        endpoint = f"historical-{params.symbol}-constituent" if params.historical else index
        return [(endpoint, {})]


__all__ = [
    "CryptoHistoricalFetcher",
    "CryptoHistoricalQuery",
    "CryptoSearchFetcher",
    "CryptoSearchQuery",
    "CurrencyHistoricalFetcher",
    "CurrencyHistoricalQuery",
    "CurrencyPairsFetcher",
    "CurrencyPairsQuery",
    "CurrencySnapshotsFetcher",
    "CurrencySnapshotsQuery",
    "EquityActiveFetcher",
    "EquityActiveQuery",
    "EquityGainersFetcher",
    "EquityGainersQuery",
    "EquityInfoFetcher",
    "EquityInfoQuery",
    "EquityLosersFetcher",
    "EquityLosersQuery",
    "EquityPeersFetcher",
    "EquityPeersQuery",
    "EquityScreenerFetcher",
    "EquityScreenerQuery",
    "IndexConstituentsFetcher",
    "IndexConstituentsQuery",
    "IndexHistoricalFetcher",
    "IndexHistoricalQuery",
    "MarketSnapshotsFetcher",
    "MarketSnapshotsQuery",
    "PricePerformanceFetcher",
    "PricePerformanceQuery",
    "ShareStatisticsFetcher",
    "ShareStatisticsQuery",
]

# Public query names are model-specific aliases over the strict query contracts.
CryptoHistoricalQuery = SingleSeriesHistoricalQuery
IndexHistoricalQuery = HistoricalQuery
CryptoSearchQuery = ListQuery
EquityActiveQuery = MarketMoversQuery
EquityGainersQuery = MarketMoversQuery
EquityLosersQuery = MarketMoversQuery
EquityInfoQuery = MultiSymbolQuery
EquityPeersQuery = BoundedSymbolQuery
PricePerformanceQuery = MultiSymbolQuery
ShareStatisticsQuery = MultiSymbolQuery
