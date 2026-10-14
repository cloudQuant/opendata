"""Bounded FMP ETF fetchers with explicit source-shaped contracts."""

from __future__ import annotations

import json
from datetime import date as date_type
from datetime import datetime
from typing import Any, ClassVar, Literal, TypeVar, cast
from urllib.parse import quote

from pydantic import Field, ValidationError, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.models.base import ContractModel
from opendata.data.models.fmp_etf import (
    FmpEtfCountry,
    FmpEtfEquityExposure,
    FmpEtfHistorical,
    FmpEtfHistoricalSourceRow,
    FmpEtfHolding,
    FmpEtfInfo,
    FmpEtfPricePerformance,
    FmpEtfQuoteSourceRow,
    FmpEtfSearch,
    FmpEtfSector,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.fmp.models._client import (
    FMPProviderError,
    FMPQueryLimitError,
    FMPQueryValidationError,
    fetch_array,
    require_api_key,
)
from opendata.data.request_budget import RequestBudgetError

_MAX_DIRECTORY_RECORDS = 200_000
_MAX_LOCAL_RECORDS = 200_000
_ModelT = TypeVar("_ModelT", bound=ContractModel)
_ETF_QUOTE_FIELDS = frozenset(
    {
        "symbol",
        "name",
        "price",
        "changePercentage",
        "change",
        "volume",
        "dayLow",
        "dayHigh",
        "yearHigh",
        "yearLow",
        "marketCap",
        "priceAvg50",
        "priceAvg200",
        "exchange",
        "open",
        "previousClose",
        "timestamp",
    }
)


class _FmpEtfQuery(QueryParams):
    """FMP-only ETF request base with a fixed source and global market."""

    source: Literal["fmp"] = "fmp"
    market: Literal["global"] = "global"


class _SymbolEtfQuery(_FmpEtfQuery):
    """One ticker-bound ETF/fund endpoint query."""

    symbol: str

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not symbol or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 for char in symbol
        ):
            raise ValueError("symbol must be a nonblank token without whitespace")
        return symbol

    @model_validator(mode="after")
    def _reject_dates(self) -> _SymbolEtfQuery:
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("this FMP ETF endpoint does not accept date bounds")
        return self


class EtfInfoQuery(_SymbolEtfQuery):
    """Single ETF information query."""


class EtfCountriesQuery(_SymbolEtfQuery):
    """Single ETF country-weighting query."""


class EtfSectorsQuery(_SymbolEtfQuery):
    """Single ETF sector-weighting query."""


class EtfHoldingsQuery(_SymbolEtfQuery):
    """ETF holdings query with FMP's documented optional historical date."""

    date: date_type | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _validate_date(cls, value: object) -> object:
        return None if value is None else _parse_iso_date(value)


class EtfEquityExposureQuery(_SymbolEtfQuery):
    """A stock symbol whose ETF exposure is requested."""


class EtfPricePerformanceQuery(_SymbolEtfQuery):
    """One ETF quote query; stable ETF quotes evidence only current daily change."""

    max_records: int = Field(default=20_000, ge=1, le=_MAX_LOCAL_RECORDS, strict=True)


class EtfSearchQuery(_FmpEtfQuery):
    """ETF-directory query with optional local substring matching."""

    query: str | None = None
    max_records: int = Field(default=20_000, ge=1, le=_MAX_LOCAL_RECORDS, strict=True)

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(ord(char) < 32 or ord(char) == 127 for char in normalized):
            raise ValueError("query must be nonblank printable text")
        return normalized

    @model_validator(mode="after")
    def _reject_symbol_and_dates(self) -> EtfSearchQuery:
        if self.symbol is not None or self.start_date is not None or self.end_date is not None:
            raise ValueError("ETF directory search does not accept symbol or date filters")
        return self


class EtfHistoricalQuery(_FmpEtfQuery):
    """Explicit bounded daily ETF query against FMP's documented legacy route."""

    symbol: str
    start_date: date_type
    end_date: date_type
    interval: Literal["1d"] = "1d"
    max_records: int = Field(default=20_000, ge=1, le=_MAX_LOCAL_RECORDS, strict=True)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _validate_dates(cls, value: object) -> object:
        return _parse_iso_date(value)

    @model_validator(mode="after")
    def _validate_window(self) -> EtfHistoricalQuery:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


def _normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not symbol or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in symbol):
        raise ValueError("symbol must be a nonblank token without whitespace")
    return symbol


def _parse_iso_date(value: object) -> object:
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


def _bad_query(query_type: type[QueryParams], kwargs: dict[str, object]) -> QueryParams:
    """Translate query validation failures into a stable sanitized provider error."""
    try:
        return query_type.model_validate(kwargs)
    except ValidationError as exc:
        raise FMPQueryValidationError.from_validation_error(exc) from None
    except (TypeError, ValueError):
        raise FMPQueryValidationError() from None


def _validated(model: type[_ModelT], row: object) -> _ModelT:
    """Validate one source row without exposing its text or key material."""
    if not isinstance(row, dict):
        raise FMPProviderError("FMP_BAD_SHAPE")
    try:
        return model.model_validate(row)
    except (ValidationError, TypeError, ValueError):
        raise FMPProviderError("FMP_BAD_SHAPE") from None


def _source_rows(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise FMPProviderError("FMP_BAD_SHAPE")
    return cast("list[dict[str, Any]]", raw)


def _require_source_keys(
    row: dict[str, Any],
    *,
    required: frozenset[str],
    allowed: frozenset[str],
) -> None:
    """Reject missing required keys and unknown source columns."""
    if not required.issubset(row) or set(row) - allowed:
        raise FMPProviderError("FMP_BAD_SHAPE")


def _fetch_etf_directory(timeout: float | None) -> tuple[FmpEtfSearch, ...]:
    """Load the official ETF-only directory used to prove direct ETF ownership."""
    raw = _source_rows(fetch_array("etf-list", {}, timeout))
    if len(raw) >= _MAX_DIRECTORY_RECORDS:
        raise FMPQueryLimitError(
            "FMP_INCOMPLETE_DIRECTORY",
            limit_kind="max_records",
            maximum=_MAX_DIRECTORY_RECORDS,
            requests_made=1,
        )
    rows: list[FmpEtfSearch] = []
    seen: set[str] = set()
    for item in raw:
        record = _validated(FmpEtfSearch, item)
        key = record.symbol.casefold()
        if key in seen:
            raise FMPProviderError("FMP_DUPLICATE_ETF_SYMBOL")
        seen.add(key)
        rows.append(record)
    return tuple(rows)


def _legacy_http_error_code(status: int | None) -> str:
    """Map legacy-route transport statuses without retaining unsafe details."""
    if status == 401:
        return "FMP_AUTH_ERROR"
    if status == 403:
        return "FMP_FORBIDDEN"
    if status == 429:
        return "FMP_RATE_LIMITED"
    if status is not None and status >= 500:
        return "FMP_UPSTREAM_ERROR"
    return "FMP_HTTP_ERROR"


def _fetch_legacy_etf_history(
    symbol: str,
    start_date: date_type,
    end_date: date_type,
    timeout: float | None,
    max_records: int,
) -> list[dict[str, Any]]:
    """Read the official legacy ETF-specific history endpoint through governed HTTP."""
    api_key = require_api_key()
    safe_symbol = quote(symbol, safe="")
    url = f"https://financialmodelingprep.com/api/v3/historical-price-full/{safe_symbol}"
    try:
        response = get_shared_http_client().get(
            url,
            params={
                "from": start_date.isoformat(),
                "to": end_date.isoformat(),
                "apikey": api_key,
            },
            timeout=30.0 if timeout is None else timeout,
            source="fmp",
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise FMPProviderError(_legacy_http_error_code(exc.status), status=exc.status) from None
    except Exception:
        # HTTP exceptions may include the credential-bearing prepared URL.
        raise FMPProviderError("FMP_TRANSPORT_ERROR") from None

    status = response.status_code
    if not 200 <= status < 300:
        raise FMPProviderError(_legacy_http_error_code(status), status=status)
    body = response.text
    content_type = response.headers.get("Content-Type", "").lower()
    starts_with_html = body.lstrip().lower().startswith(("<!doctype html", "<html"))
    if "text/html" in content_type or starts_with_html:
        raise FMPProviderError("FMP_HTML_RESPONSE", status=status)
    try:
        document: object = json.loads(body)
    except json.JSONDecodeError:
        raise FMPProviderError("FMP_BAD_RESPONSE", status=status) from None
    if not isinstance(document, dict):
        raise FMPProviderError("FMP_BAD_SHAPE", status=status)
    if any(key in document for key in ("Error Message", "error", "Error", "message")):
        raise FMPProviderError("FMP_API_ERROR", status=status)
    if set(document) != {"symbol", "historical"}:
        raise FMPProviderError("FMP_BAD_SHAPE", status=status)
    response_symbol = document.get("symbol")
    raw_rows = document.get("historical")
    if not isinstance(response_symbol, str) or response_symbol.casefold() != symbol.casefold():
        raise FMPProviderError("FMP_WRONG_SYMBOL", status=status)
    if not isinstance(raw_rows, list) or any(not isinstance(row, dict) for row in raw_rows):
        raise FMPProviderError("FMP_BAD_SHAPE", status=status)
    if len(raw_rows) >= max_records:
        raise FMPQueryLimitError(
            "FMP_INCOMPLETE_RECORD_BUDGET",
            limit_kind="max_records",
            maximum=max_records,
            requests_made=1,
        )
    rows: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        source_row = _validated(FmpEtfHistoricalSourceRow, raw_row)
        # The documented envelope, not the individual historical row, owns this symbol.
        rows.append(
            {
                "symbol": response_symbol,
                "date": source_row.date,
                "open": source_row.open,
                "high": source_row.high,
                "low": source_row.low,
                "close": source_row.close,
                "volume": source_row.volume,
                "change": source_row.change,
                "changePercent": source_row.change_percent,
                "vwap": source_row.vwap,
            }
        )
    return rows


def _require_listed_etf(symbol: str, timeout: float | None) -> None:
    """Fail before ETF/fund endpoints when the symbol is not in the ETF directory."""
    if not any(row.symbol.casefold() == symbol.casefold() for row in _fetch_etf_directory(timeout)):
        raise FMPProviderError("FMP_SYMBOL_NOT_ETF")


def _validate_single_symbol_rows(
    raw: object,
    *,
    model: type[_ModelT],
    requested: str,
    source_symbol_field: str = "symbol",
) -> tuple[_ModelT, ...]:
    """Validate a single-fund response and prevent row provenance mismatches."""
    rows: list[_ModelT] = []
    for item in _source_rows(raw):
        source_symbol = item.get(source_symbol_field)
        if not isinstance(source_symbol, str) or source_symbol.casefold() != requested.casefold():
            raise FMPProviderError("FMP_WRONG_SYMBOL")
        rows.append(_validated(model, item))
    return tuple(rows)


class _SymbolEtfFetcher(Fetcher[_SymbolEtfQuery, list[dict[str, Any]]]):
    """Common entry point for symbol-specific ETFs and fund data."""

    async_mode = "bounded_thread"
    query_type: ClassVar[type[_SymbolEtfQuery]]
    canonical_model: ClassVar[str | None]
    endpoint: ClassVar[str]
    record_model: ClassVar[type[ContractModel]]
    capability: ClassVar[Capability]

    def transform_query(self, **kwargs: object) -> _SymbolEtfQuery:
        return cast("_SymbolEtfQuery", _bad_query(self.query_type, kwargs))

    def extract_data(
        self,
        params: _SymbolEtfQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        _require_listed_etf(params.symbol, ctx.timeout)
        request_params = {"symbol": params.symbol}
        if isinstance(params, EtfHoldingsQuery) and params.date is not None:
            request_params["date"] = params.date.isoformat()
        rows = _source_rows(fetch_array(self.endpoint, request_params, ctx.timeout))
        if len(rows) > _MAX_LOCAL_RECORDS:
            raise FMPQueryLimitError(
                "FMP_INCOMPLETE_RECORD_BUDGET",
                limit_kind="max_records",
                maximum=_MAX_LOCAL_RECORDS,
                requests_made=2,
            )
        return rows


class EtfInfoFetcher(_SymbolEtfFetcher):
    """Fetch one listed ETF's documented information row."""

    query_type = EtfInfoQuery
    canonical_model = "EtfInfo"
    endpoint = "etf/info"
    record_model = FmpEtfInfo
    capability = Capability(
        asset_class="etf",
        domain="fmp_etf_info",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_data(self, raw: list[dict[str, Any]], params: _SymbolEtfQuery) -> FetchResult:
        """Validate the single information record for the requested ETF."""
        rows = _validate_single_symbol_rows(raw, model=FmpEtfInfo, requested=params.symbol)
        if len(rows) > 1:
            raise FMPProviderError("FMP_EXPECTED_ONE_ETF_INFO_ROW")
        return rows


class EtfCountriesFetcher(_SymbolEtfFetcher):
    """Fetch country-weight rows for a directory-verified ETF."""

    query_type = EtfCountriesQuery
    canonical_model = "EtfCountries"
    endpoint = "etf/country-weightings"
    record_model = FmpEtfCountry
    capability = Capability(
        asset_class="etf",
        domain="fmp_etf_countries",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_data(self, raw: list[dict[str, Any]], params: _SymbolEtfQuery) -> FetchResult:
        """Validate unique country rows and attach the requested ETF identity."""
        records: list[FmpEtfCountry] = []
        seen: set[str] = set()
        for item in _source_rows(raw):
            _require_source_keys(
                item,
                required=frozenset({"country", "weightPercentage"}),
                allowed=frozenset({"symbol", "country", "weightPercentage"}),
            )
            if "symbol" in item:
                symbol = item.get("symbol")
                if not isinstance(symbol, str) or symbol.casefold() != params.symbol.casefold():
                    raise FMPProviderError("FMP_WRONG_SYMBOL")
            country = item.get("country")
            if not isinstance(country, str):
                raise FMPProviderError("FMP_BAD_SHAPE")
            key = country.casefold()
            if key in seen:
                raise FMPProviderError("FMP_DUPLICATE_ETF_COUNTRY")
            seen.add(key)
            records.append(
                _validated(
                    FmpEtfCountry,
                    {
                        "symbol": params.symbol,
                        "country": country,
                        "weight": item.get("weightPercentage"),
                    },
                )
            )
        return tuple(records)


class EtfSectorsFetcher(_SymbolEtfFetcher):
    """Fetch ETF sector-weight rows while preserving source percentage values."""

    query_type = EtfSectorsQuery
    canonical_model = "EtfSectors"
    endpoint = "etf/sector-weightings"
    record_model = FmpEtfSector
    capability = Capability(
        asset_class="etf",
        domain="fmp_etf_sectors",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_data(self, raw: list[dict[str, Any]], params: _SymbolEtfQuery) -> FetchResult:
        """Validate unique sector rows and attach the requested ETF identity."""
        records: list[FmpEtfSector] = []
        seen: set[str] = set()
        for item in _source_rows(raw):
            _require_source_keys(
                item,
                required=frozenset({"sector", "weightPercentage"}),
                allowed=frozenset({"symbol", "sector", "weightPercentage"}),
            )
            if "symbol" in item:
                symbol = item.get("symbol")
                if not isinstance(symbol, str) or symbol.casefold() != params.symbol.casefold():
                    raise FMPProviderError("FMP_WRONG_SYMBOL")
            sector = item.get("sector")
            if not isinstance(sector, str):
                raise FMPProviderError("FMP_BAD_SHAPE")
            key = sector.casefold()
            if key in seen:
                raise FMPProviderError("FMP_DUPLICATE_ETF_SECTOR")
            seen.add(key)
            records.append(
                _validated(
                    FmpEtfSector,
                    {
                        "symbol": params.symbol,
                        "sector": sector,
                        "weight": item.get("weightPercentage"),
                    },
                )
            )
        return tuple(records)


class EtfHoldingsFetcher(_SymbolEtfFetcher):
    """Fetch current or date-selected ETF holdings without changing source values."""

    query_type = EtfHoldingsQuery
    canonical_model = "EtfHoldings"
    endpoint = "etf/holdings"
    record_model = FmpEtfHolding
    capability = Capability(
        asset_class="etf",
        domain="fmp_etf_holdings",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_data(self, raw: list[dict[str, Any]], params: _SymbolEtfQuery) -> FetchResult:
        """Validate that each holdings row belongs to the requested ETF."""
        return _validate_single_symbol_rows(
            raw,
            model=FmpEtfHolding,
            requested=params.symbol,
        )


class EtfEquityExposureFetcher(Fetcher[EtfEquityExposureQuery, list[dict[str, Any]]]):
    """Find ETF records holding one requested equity symbol."""

    async_mode = "bounded_thread"
    canonical_model = "EtfEquityExposure"
    record_model = FmpEtfEquityExposure
    capability = Capability(
        asset_class="etf",
        domain="fmp_etf_equity_exposure",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EtfEquityExposureQuery:
        """Validate the requested equity ticker and source identity."""
        return cast(
            "EtfEquityExposureQuery",
            _bad_query(EtfEquityExposureQuery, kwargs),
        )

    def extract_data(
        self,
        params: EtfEquityExposureQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Fetch exposures and verify returned ETF symbols against the ETF directory."""
        etf_symbols = {row.symbol.casefold() for row in _fetch_etf_directory(ctx.timeout)}
        raw = _source_rows(
            fetch_array("etf/asset-exposure", {"symbol": params.symbol}, ctx.timeout)
        )
        if len(raw) > _MAX_LOCAL_RECORDS:
            raise FMPQueryLimitError(
                "FMP_INCOMPLETE_ETF_EXPOSURE",
                limit_kind="max_records",
                maximum=_MAX_LOCAL_RECORDS,
                requests_made=2,
            )
        for item in raw:
            etf_symbol = item.get("symbol")
            if not isinstance(etf_symbol, str):
                raise FMPProviderError("FMP_BAD_SHAPE")
            if etf_symbol.casefold() not in etf_symbols:
                raise FMPProviderError("FMP_SYMBOL_NOT_ETF")
        return raw

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: EtfEquityExposureQuery,
    ) -> FetchResult:
        """Validate equity identity, returned ETF identity, and source numeric values."""
        records: list[FmpEtfEquityExposure] = []
        seen: set[str] = set()
        for item in _source_rows(raw):
            _require_source_keys(
                item,
                required=frozenset({"symbol", "asset"}),
                allowed=frozenset(
                    {"symbol", "asset", "sharesNumber", "weightPercentage", "marketValue"}
                ),
            )
            asset = item.get("asset")
            etf_symbol = item.get("symbol")
            if not isinstance(asset, str) or asset.casefold() != params.symbol.casefold():
                raise FMPProviderError("FMP_WRONG_EQUITY_SYMBOL")
            if not isinstance(etf_symbol, str):
                raise FMPProviderError("FMP_BAD_SHAPE")
            if etf_symbol.casefold() in seen:
                raise FMPProviderError("FMP_DUPLICATE_ETF_EXPOSURE")
            seen.add(etf_symbol.casefold())
            records.append(
                _validated(
                    FmpEtfEquityExposure,
                    {
                        "equity_symbol": asset,
                        "etf_symbol": etf_symbol,
                        "weight": item.get("weightPercentage"),
                        "market_value": item.get("marketValue"),
                        "shares": item.get("sharesNumber"),
                    },
                )
            )
        return tuple(records)


class EtfPricePerformanceFetcher(Fetcher[EtfPricePerformanceQuery, list[dict[str, Any]]]):
    """Return the evidenced current one-day quote change for one ETF."""

    async_mode = "bounded_thread"
    canonical_model = "EtfPricePerformance"
    record_model = FmpEtfPricePerformance
    capability = Capability(
        asset_class="etf",
        domain="fmp_etf_price_performance",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EtfPricePerformanceQuery:
        """Validate an ETF quote query and its local record bound."""
        return cast("EtfPricePerformanceQuery", _bad_query(EtfPricePerformanceQuery, kwargs))

    def extract_data(
        self,
        params: EtfPricePerformanceQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Fetch the documented ETF quote batch and bound its unpaged response."""
        _require_listed_etf(params.symbol, ctx.timeout)
        raw = _source_rows(fetch_array("batch-etf-quotes", {"short": "false"}, ctx.timeout))
        if len(raw) > _MAX_LOCAL_RECORDS:
            raise FMPQueryLimitError(
                "FMP_INCOMPLETE_ETF_QUOTES",
                limit_kind="max_records",
                maximum=_MAX_LOCAL_RECORDS,
                requests_made=2,
            )
        return raw

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: EtfPricePerformanceQuery,
    ) -> FetchResult:
        """Return only the requested ETF's documented current one-day change."""
        matches: list[FmpEtfPricePerformance] = []
        for item in _source_rows(raw):
            _require_source_keys(
                item,
                required=frozenset({"symbol", "changePercentage"}),
                allowed=_ETF_QUOTE_FIELDS,
            )
            symbol = item.get("symbol")
            if not isinstance(symbol, str):
                raise FMPProviderError("FMP_BAD_SHAPE")
            if symbol.casefold() != params.symbol.casefold():
                continue
            quote = _validated(FmpEtfQuoteSourceRow, item)
            matches.append(
                _validated(
                    FmpEtfPricePerformance,
                    {"symbol": quote.symbol, "one_day": quote.change_percentage},
                )
            )
        if len(matches) > 1:
            raise FMPProviderError("FMP_DUPLICATE_ETF_QUOTE")
        if len(matches) > params.max_records:
            raise FMPQueryLimitError(
                "FMP_INCOMPLETE_ETF_QUOTES",
                limit_kind="max_records",
                maximum=params.max_records,
                requests_made=2,
            )
        return tuple(matches)


class EtfSearchFetcher(Fetcher[EtfSearchQuery, list[dict[str, Any]]]):
    """Search FMP's unpaged ETF directory using its source-provided names/tickers."""

    async_mode = "bounded_thread"
    canonical_model = "EtfSearch"
    record_model = FmpEtfSearch
    capability = Capability(
        asset_class="etf",
        domain="fmp_etf_search",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EtfSearchQuery:
        """Validate an ETF directory search and local result bound."""
        return cast("EtfSearchQuery", _bad_query(EtfSearchQuery, kwargs))

    def extract_data(self, params: EtfSearchQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Fetch the unpaged source directory without inventing pagination."""
        raw = _source_rows(fetch_array("etf-list", {}, ctx.timeout))
        if len(raw) > params.max_records:
            raise FMPQueryLimitError(
                "FMP_INCOMPLETE_ETF_DIRECTORY",
                limit_kind="max_records",
                maximum=params.max_records,
                requests_made=1,
            )
        return raw

    def transform_data(self, raw: list[dict[str, Any]], params: EtfSearchQuery) -> FetchResult:
        """Validate unique directory rows and apply the optional local substring filter."""
        rows = tuple(_validated(FmpEtfSearch, item) for item in _source_rows(raw))
        seen: set[str] = set()
        for record in rows:
            key = record.symbol.casefold()
            if key in seen:
                raise FMPProviderError("FMP_DUPLICATE_ETF_SYMBOL")
            seen.add(key)
        if params.query is None:
            return rows
        needle = params.query.casefold()
        return tuple(
            row for row in rows if needle in row.symbol.casefold() or needle in row.name.casefold()
        )


class EtfHistoricalFetcher(Fetcher[EtfHistoricalQuery, list[dict[str, Any]]]):
    """Fetch ETF-specific daily history using FMP's published legacy API route."""

    async_mode = "bounded_thread"
    canonical_model = "EtfHistorical"
    record_model = FmpEtfHistorical
    capability = Capability(
        asset_class="etf",
        domain="fmp_etf_historical",
        period="1d",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EtfHistoricalQuery:
        """Validate daily ETF history bounds and reject unsupported intervals."""
        return cast("EtfHistoricalQuery", _bad_query(EtfHistoricalQuery, kwargs))

    def extract_data(
        self,
        params: EtfHistoricalQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Fetch bounded daily history for an ETF proven by the source directory."""
        _require_listed_etf(params.symbol, ctx.timeout)
        return _fetch_legacy_etf_history(
            params.symbol,
            params.start_date,
            params.end_date,
            ctx.timeout,
            params.max_records,
        )

    def transform_data(self, raw: list[dict[str, Any]], params: EtfHistoricalQuery) -> FetchResult:
        """Validate symbol and date bounds, reject duplicate days, and sort rows."""
        rows_by_date: dict[date_type, FmpEtfHistorical] = {}
        for item in _source_rows(raw):
            record = _validated(FmpEtfHistorical, item)
            if record.symbol.casefold() != params.symbol.casefold():
                raise FMPProviderError("FMP_WRONG_SYMBOL")
            if not params.start_date <= record.date <= params.end_date:
                raise FMPProviderError("FMP_OUT_OF_WINDOW_ROW")
            previous = rows_by_date.get(record.date)
            if previous is not None:
                if previous.model_dump() != record.model_dump():
                    raise FMPProviderError("FMP_CONFLICTING_DUPLICATE_DATE")
                raise FMPProviderError("FMP_DUPLICATE_ETF_DATE")
            rows_by_date[record.date] = record
        return tuple(rows_by_date[day] for day in sorted(rows_by_date))
