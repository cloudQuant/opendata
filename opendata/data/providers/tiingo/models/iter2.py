"""Bounded adapters for Tiingo's documented Iteration 2 API responses."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError, field_validator

from opendata.data.capability import Capability
from opendata.data.models.tiingo import (
    TiingoCompanyNews,
    TiingoCurrencyHistorical,
    TiingoEquityHistorical,
    TiingoEtfHistorical,
    TiingoTrailingDividendYield,
    TiingoWorldNews,
)
from opendata.data.protocol import FetchContext, Fetcher
from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.http_json import (
    HttpGet,
    HttpResponse,
    ProviderEngineError,
    ProviderEngineQueryError,
)
from opendata.data.providers.tiingo._source import SOURCE

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel

BASE_URL = "https://api.tiingo.com"
CREDENTIAL_ENV_VAR = "TIINGO_TOKEN"
_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,31}\Z")
_INTERVALS: dict[str, tuple[str, str]] = {
    "1d": ("daily", "daily"),
    "1w": ("daily", "weekly"),
    "1mo": ("daily", "monthly"),
    "5m": ("iex", "5min"),
}
_FX_INTERVALS = {"1d": "1day", "5m": "5min"}


class _Query(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: StrictStr | None = None
    market: StrictStr | None = None


class _HistoricalQuery(_Query):
    symbol: StrictStr
    start_date: date | None = None
    end_date: date | None = None
    interval: Literal["1d", "1w", "1mo", "5m"] = "1d"

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _parse_dates(cls, value: object) -> object:
        if value is None or isinstance(value, date):
            return value
        if not isinstance(value, str):
            raise ValueError("date must be an ISO calendar date")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date must be an ISO calendar date") from exc
        if parsed.isoformat() != value:
            raise ValueError("date must be an ISO calendar date")
        return parsed


class _NewsQuery(_Query):
    sort_by: Literal["publishedDate", "crawlDate"] = "publishedDate"


class _CompanyNewsQuery(_NewsQuery):
    symbol: StrictStr


class _CurrencyQuery(_Query):
    symbol: StrictStr
    start_date: date | None = None
    end_date: date | None = None
    interval: Literal["1d", "5m"] = "1d"

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _parse_dates(cls, value: object) -> object:
        return _HistoricalQuery._parse_dates(value)


class _DividendQuery(_Query):
    symbol: StrictStr


def _validate_symbol_text(value: str, *, max_symbols: int = 5) -> tuple[str, ...]:
    symbols = tuple(item.strip() for item in value.split(","))
    if not 1 <= len(symbols) <= max_symbols or any(not _SYMBOL.fullmatch(x) for x in symbols):
        raise ProviderEngineQueryError("TIINGO_QUERY_INVALID", invalid=("symbol",))
    if len({x.casefold() for x in symbols}) != len(symbols):
        raise ProviderEngineQueryError("TIINGO_QUERY_INVALID", invalid=("symbol",))
    return symbols


def _date_value(value: object, *, datetime_allowed: bool) -> date | datetime:
    if isinstance(value, datetime):
        if not datetime_allowed:
            raise ValueError("unexpected timestamp")
        return value
    if not isinstance(value, str):
        raise ValueError("date field must be text")
    try:
        if "T" in value or " " in value:
            if not datetime_allowed:
                raise ValueError("unexpected timestamp")
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date field is not an ISO date/time") from exc
    if parsed.isoformat() != value:
        raise ValueError("date field is not canonical")
    return parsed


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("timestamp field must be text")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp field is invalid") from exc


def _number(value: object, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("numeric field must be a JSON number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError("numeric field must be finite")
    return converted


def _integer(value: object, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("integer field must be a JSON integer")
    return value


def _string(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError("text field must be a string")
    return value


def _array_of_strings(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("expected an array of strings")
    return list(value)


class TiingoAPIModelFetcher(Fetcher[Any, Any]):
    """Common governed transport, strict-query validation and typed row validation."""

    async_mode = "bounded_thread"
    http_transport: ClassVar[HttpGet | None] = None
    query_model: ClassVar[type[_Query]]
    row_model: ClassVar[type[ContractModel]]
    max_rows = 10_000

    def transform_query(self, **kwargs: object) -> BaseModel:
        """Validate keyword arguments against ``query_model``, refusing unknown or mistyped keys."""
        try:
            params = self.query_model.model_validate(kwargs)
        except ValidationError as exc:
            missing = sorted({str(e["loc"][0]) for e in exc.errors() if e["type"] == "missing"})
            rejected = sorted(
                {str(e["loc"][0]) for e in exc.errors() if e["type"] == "extra_forbidden"}
            )
            invalid = sorted(
                {
                    str(e["loc"][0])
                    for e in exc.errors()
                    if e["type"] not in {"missing", "extra_forbidden"} and e["loc"]
                }
            )
            raise ProviderEngineQueryError(
                "TIINGO_QUERY_INVALID", missing=missing, rejected=rejected, invalid=invalid
            ) from None
        if params.source not in (None, self.capability.source):
            raise ProviderEngineQueryError("TIINGO_QUERY_INVALID", invalid=("source",))
        if params.market not in (None, self.capability.market):
            raise ProviderEngineQueryError("TIINGO_QUERY_INVALID", invalid=("market",))
        for name in ("symbol",):
            value = getattr(params, name, None)
            if value is not None:
                _validate_symbol_text(value)
        start = getattr(params, "start_date", None)
        end = getattr(params, "end_date", None)
        if start is not None and end is not None and start > end:
            raise ProviderEngineQueryError(
                "TIINGO_QUERY_INVALID", invalid=("start_date", "end_date")
            )
        return params

    def _token(self) -> str:
        token = os.environ.get(CREDENTIAL_ENV_VAR, "").strip()
        if not token:
            raise ProviderEngineError("TIINGO_CREDENTIAL_MISSING")
        return token

    def _get(self, url: str, params: dict[str, str], ctx: FetchContext) -> HttpResponse:
        transport = self.http_transport or http_json._http_get_json
        timeout = min(float(ctx.timeout or 15.0), 15.0)
        response = transport(url, params, timeout=timeout, source=SOURCE)
        if not 200 <= response.status < 300:
            if response.status == 429:
                code = "TIINGO_RATE_LIMITED"
            elif response.status in (401, 403):
                code = "TIINGO_CREDENTIAL_REJECTED"
            elif response.status in (400, 422):
                code = "TIINGO_QUERY_REJECTED"
            else:
                code = "TIINGO_HTTP_ERROR"
            raise ProviderEngineError(code, status=response.status, url=url)
        return response

    def transform_data(self, raw: object, params: object) -> tuple[ContractModel, ...]:
        """Build contract rows from a list payload, refusing oversized input."""
        if not isinstance(raw, list) or len(raw) > self.max_rows:
            raise ProviderEngineError("TIINGO_SHAPE_INVALID")
        rows: list[ContractModel] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ProviderEngineError("TIINGO_SHAPE_INVALID")
            try:
                mapped = self.map_record(item, params)
                rows.append(self.row_model.model_validate(mapped, strict=True))
            except (KeyError, TypeError, ValueError, ValidationError):
                raise ProviderEngineError("TIINGO_SHAPE_INVALID") from None
        return tuple(rows)

    def map_record(self, item: Mapping[str, object], params: object) -> dict[str, object]:
        """Abstract per-record hook; rows map through ``row_model``."""
        raise NotImplementedError


class TiingoEquityHistoricalFetcher(TiingoAPIModelFetcher):
    """Fetch bounded daily and IEX historical bars for equity symbols."""

    canonical_model = "EquityHistorical"
    query_model = _HistoricalQuery
    row_model = TiingoEquityHistorical
    capability = Capability(
        source=SOURCE,
        domain="tiingo_equity_historical",
        asset_class="equity",
        period="d",
        market="us",
        verified=False,
        notes="Tiingo official-response contract; no live validation.",
    )

    def extract_data(self, params: _HistoricalQuery, ctx: FetchContext) -> list[dict[str, object]]:
        """Fetch each requested symbol's documented historical price rows."""
        symbols = _validate_symbol_text(params.symbol)
        if params.interval not in _INTERVALS:
            raise ProviderEngineQueryError("TIINGO_QUERY_INVALID", invalid=("interval",))
        api, resample = _INTERVALS[params.interval]
        token = self._token()
        rows: list[dict[str, object]] = []
        for symbol in symbols:
            if api == "daily":
                url = f"{BASE_URL}/tiingo/daily/{quote(symbol, safe='')}/prices"
            else:
                url = f"{BASE_URL}/iex/{quote(symbol, safe='')}/prices"
            query = {"token": token, "resampleFreq": resample}
            if params.start_date is not None:
                query["startDate"] = params.start_date.isoformat()
            if params.end_date is not None:
                query["endDate"] = params.end_date.isoformat()
            if api == "iex":
                query["columns"] = "open,high,low,close,volume"
            document = self._get(url, query, ctx).document
            if not isinstance(document, list) or len(document) > self.max_rows - len(rows):
                raise ProviderEngineError("TIINGO_SHAPE_INVALID", url=url)
            for record in document:
                if not isinstance(record, dict):
                    raise ProviderEngineError("TIINGO_SHAPE_INVALID", url=url)
                rows.append({**record, "ticker": symbol})
        return rows

    def map_record(self, item: Mapping[str, object], params: object) -> dict[str, object]:
        """Map one Tiingo bar into the strict equity row contract."""
        return {
            "symbol": _string(item.get("ticker")),
            "date": _date_value(item.get("date"), datetime_allowed=True),
            "open": _number(item.get("open"), optional=True),
            "high": _number(item.get("high"), optional=True),
            "low": _number(item.get("low"), optional=True),
            "close": _number(item.get("close")),
            "volume": _integer(item.get("volume"), optional=True),
            "adj_open": _number(item.get("adjOpen"), optional=True),
            "adj_high": _number(item.get("adjHigh"), optional=True),
            "adj_low": _number(item.get("adjLow"), optional=True),
            "adj_close": _number(item.get("adjClose"), optional=True),
            "adj_volume": _integer(item.get("adjVolume"), optional=True),
            "div_cash": _number(item.get("divCash"), optional=True),
            "split_factor": _number(item.get("splitFactor"), optional=True),
        }


class TiingoEtfHistoricalFetcher(TiingoEquityHistoricalFetcher):
    """Fetch historical bars using Tiingo's shared equity price shape."""

    canonical_model = "EtfHistorical"
    row_model = TiingoEtfHistorical
    capability = Capability(
        source=SOURCE,
        domain="tiingo_etf_historical",
        asset_class="etf",
        period="d",
        market="us",
        verified=False,
        notes="Tiingo official-response contract; no live validation.",
    )


class TiingoCurrencyHistoricalFetcher(TiingoAPIModelFetcher):
    """Fetch bounded FX price history for one or more currency pairs."""

    canonical_model = "CurrencyHistorical"
    query_model = _CurrencyQuery
    row_model = TiingoCurrencyHistorical
    capability = Capability(
        source=SOURCE,
        domain="tiingo_currency_historical",
        asset_class="currency",
        period="d",
        market="all",
        verified=False,
        notes="Tiingo official-response contract; no live validation.",
    )

    def extract_data(self, params: _CurrencyQuery, ctx: FetchContext) -> list[dict[str, object]]:
        """Fetch each requested currency pair's FX price rows."""
        symbols = _validate_symbol_text(params.symbol)
        token = self._token()
        rows: list[dict[str, object]] = []
        for symbol in symbols:
            url = f"{BASE_URL}/tiingo/fx/{quote(symbol, safe='')}/prices"
            query = {"token": token, "resampleFreq": _FX_INTERVALS[params.interval]}
            if params.start_date is not None:
                query["startDate"] = params.start_date.isoformat()
            if params.end_date is not None:
                query["endDate"] = params.end_date.isoformat()
            document = self._get(url, query, ctx).document
            if not isinstance(document, list) or len(document) > self.max_rows - len(rows):
                raise ProviderEngineError("TIINGO_SHAPE_INVALID", url=url)
            for record in document:
                if not isinstance(record, dict):
                    raise ProviderEngineError("TIINGO_SHAPE_INVALID", url=url)
                rows.append({**record, "ticker": symbol})
        return rows

    def transform_query(self, **kwargs: object) -> BaseModel:
        """Validate the query and restrict intervals to documented FX values."""
        params = super().transform_query(**kwargs)
        if getattr(params, "interval", None) not in _FX_INTERVALS:
            raise ProviderEngineQueryError("TIINGO_QUERY_INVALID", invalid=("interval",))
        return params

    def map_record(self, item: Mapping[str, object], params: object) -> dict[str, object]:
        """Map one Tiingo FX observation into the strict currency row contract."""
        return {
            "symbol": _string(item.get("ticker")),
            "date": _timestamp(item.get("date")),
            "open": _number(item.get("open")),
            "high": _number(item.get("high")),
            "low": _number(item.get("low")),
            "close": _number(item.get("close")),
        }


class TiingoTrailingDividendYieldFetcher(TiingoAPIModelFetcher):
    """Fetch the trailing one-year distribution yield for one symbol."""

    canonical_model = "TrailingDividendYield"
    query_model = _DividendQuery
    row_model = TiingoTrailingDividendYield
    capability = Capability(
        source=SOURCE,
        domain="tiingo_trailing_dividend_yield",
        asset_class="equity",
        period="d",
        market="us",
        verified=False,
        notes="Tiingo official-response contract; no live validation.",
    )

    def extract_data(self, params: _DividendQuery, ctx: FetchContext) -> list[dict[str, object]]:
        """Fetch one symbol's trailing distribution-yield observation."""
        symbol = _validate_symbol_text(params.symbol, max_symbols=1)[0]
        url = f"{BASE_URL}/tiingo/corporate-actions/{quote(symbol, safe='')}/distribution-yield"
        response = self._get(url, {"token": self._token(), "columns": "trailingDiv1Y"}, ctx)
        if not isinstance(response.document, list) or len(response.document) > self.max_rows:
            raise ProviderEngineError("TIINGO_SHAPE_INVALID", url=url)
        if any(not isinstance(item, dict) for item in response.document):
            raise ProviderEngineError("TIINGO_SHAPE_INVALID", url=url)
        return [{**item, "ticker": symbol} for item in response.document]

    def map_record(self, item: Mapping[str, object], params: object) -> dict[str, object]:
        """Map source yield text into the strict dividend-yield row contract."""
        yield_text = item.get("trailingDiv1Y")
        if yield_text is None:
            parsed_yield = None
        elif isinstance(yield_text, str):
            try:
                parsed_yield = float(yield_text)
            except ValueError as exc:
                raise ValueError("invalid yield field") from exc
            if not math.isfinite(parsed_yield):
                raise ValueError("invalid yield field")
        else:
            raise ValueError("yield field must be source text")
        return {
            "symbol": _string(item.get("ticker")),
            "date": _timestamp(item.get("date")),
            "trailing_dividend_yield": parsed_yield,
        }


class _TiingoNewsFetcher(TiingoAPIModelFetcher):
    query_model: ClassVar[type[_Query]] = _NewsQuery
    max_rows = 5_000

    def extract_data(self, params: _NewsQuery, ctx: FetchContext) -> list[dict[str, object]]:
        query: dict[str, str] = {"token": self._token(), "sortBy": params.sort_by}
        if isinstance(params, _CompanyNewsQuery):
            query["tickers"] = ",".join(_validate_symbol_text(params.symbol))
        response = self._get(f"{BASE_URL}/tiingo/news", query, ctx)
        if not isinstance(response.document, list) or len(response.document) > self.max_rows:
            raise ProviderEngineError("TIINGO_SHAPE_INVALID")
        if any(not isinstance(row, dict) for row in response.document):
            raise ProviderEngineError("TIINGO_SHAPE_INVALID")
        return [
            {**row, "ticker_filter": getattr(params, "symbol", None)} for row in response.document
        ]

    def map_record(self, item: Mapping[str, object], params: object) -> dict[str, object]:
        symbol = item.get("ticker_filter")
        return {
            "symbol": _string(symbol, optional=True),
            "id": _integer(item.get("id")),
            "title": _string(item.get("title")),
            "url": _string(item.get("url")),
            "description": _string(item.get("description"), optional=True),
            "published_date": _timestamp(item.get("publishedDate")),
            "crawl_date": _timestamp(item.get("crawlDate")),
            "source": _string(item.get("source")),
            "tickers": _array_of_strings(item.get("tickers")),
            "tags": _array_of_strings(item.get("tags")),
        }


class TiingoCompanyNewsFetcher(_TiingoNewsFetcher):
    """Fetch company news filtered by the requested ticker symbol."""

    canonical_model = "CompanyNews"
    query_model = _CompanyNewsQuery
    row_model = TiingoCompanyNews
    capability = Capability(
        source=SOURCE,
        domain="tiingo_company_news",
        asset_class="equity",
        period="d",
        market="us",
        verified=False,
        notes="Tiingo official-response contract; no live validation.",
    )


class TiingoWorldNewsFetcher(_TiingoNewsFetcher):
    """Fetch world news without applying a ticker filter."""

    canonical_model = "WorldNews"
    row_model = TiingoWorldNews
    capability = Capability(
        source=SOURCE,
        domain="tiingo_world_news",
        asset_class="news",
        period="d",
        market="all",
        verified=False,
        notes="Tiingo official-response contract; no live validation.",
    )


__all__ = [
    "TiingoCompanyNewsFetcher",
    "TiingoCurrencyHistoricalFetcher",
    "TiingoEquityHistoricalFetcher",
    "TiingoEtfHistoricalFetcher",
    "TiingoTrailingDividendYieldFetcher",
    "TiingoWorldNewsFetcher",
]
