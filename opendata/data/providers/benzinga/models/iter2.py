"""Bounded adapters for Benzinga's documented ratings-analyst and news APIs."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError, field_validator

from opendata.data.capability import Capability
from opendata.data.models.benzinga import (
    BenzingaAnalystAccuracy,
    BenzingaAnalystSearch,
    BenzingaCompanyNews,
    BenzingaWorldNews,
)
from opendata.data.protocol import FetchContext, Fetcher
from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.http_json import (
    HttpGet,
    HttpResponse,
    ProviderEngineError,
    ProviderEngineQueryError,
)
from opendata.data.providers.benzinga._source import SOURCE

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel

BASE_URL = "https://api.benzinga.com"
CREDENTIAL_ENV_VAR = "BENZINGA_API_KEY"
_SYMBOLS = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,31}\Z")
_ACCURACY_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("1m_average_return", "one_month_average_return", "float", True),
    ("1m_smart_score", "one_month_smart_score", "float", False),
    ("1m_stdev", "one_month_stdev", "float", True),
    ("1m_success_rate", "one_month_success_rate", "float", True),
    ("1m_loss_count", "one_month_loss_count", "int", False),
    ("3m_average_return", "three_month_average_return", "float", True),
    ("3m_smart_score", "three_month_smart_score", "float", False),
    ("3m_stdev", "three_month_stdev", "float", True),
    ("3m_success_rate", "three_month_success_rate", "float", True),
    ("3m_loss_count", "three_month_loss_count", "int", False),
    ("9m_average_return", "nine_month_average_return", "float", True),
    ("9m_smart_score", "nine_month_smart_score", "float", False),
    ("9m_stdev", "nine_month_stdev", "float", True),
    ("9m_success_rate", "nine_month_success_rate", "float", True),
    ("9m_loss_count", "nine_month_loss_count", "int", False),
    ("1y_average_return", "one_year_average_return", "float", True),
    ("1y_smart_score", "one_year_smart_score", "float", False),
    ("1y_stdev", "one_year_stdev", "float", True),
    ("1y_success_rate", "one_year_success_rate", "float", True),
    ("1y_loss_count", "one_year_loss_count", "int", False),
    ("2y_average_return", "two_year_average_return", "float", True),
    ("2y_smart_score", "two_year_smart_score", "float", False),
    ("2y_stdev", "two_year_stdev", "float", True),
    ("2y_success_rate", "two_year_success_rate", "float", True),
    ("2y_loss_count", "two_year_loss_count", "int", False),
    ("3y_average_return", "three_year_average_return", "float", True),
    ("3y_smart_score", "three_year_smart_score", "float", False),
    ("3y_stdev", "three_year_stdev", "float", True),
    ("3y_success_rate", "three_year_success_rate", "float", True),
    ("3y_loss_count", "three_year_loss_count", "int", False),
    ("overall_average_return", "overall_average_return", "float", True),
    ("overall_avg_return_percentile", "overall_average_return_percentile", "float", True),
    ("overall_smart_score", "overall_smart_score", "float", False),
    ("overall_stdev", "overall_stdev", "float", True),
    ("overall_success_rate", "overall_success_rate", "float", True),
    ("overall_loss_count", "overall_loss_count", "int", False),
    ("smart_score", "smart_score", "float", False),
    ("total_ratings", "total_ratings", "int", False),
    ("total_ratings_percentile", "total_ratings_percentile", "float", True),
)


class _Query(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: StrictStr | None = None
    market: StrictStr | None = None


class _AnalystQuery(_Query):
    analyst: StrictStr | None = None
    analyst_name: StrictStr | None = None
    firm: StrictStr | None = None
    firm_name: StrictStr | None = None
    updated: int | None = None
    page: int = 0
    page_size: int = 100


class _NewsQuery(_Query):
    limit: int = 250
    start_date: date | None = None
    end_date: date | None = None

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


class _CompanyNewsQuery(_NewsQuery):
    symbol: StrictStr


def _symbols(value: str) -> tuple[str, ...]:
    parts = tuple(item.strip() for item in value.split(","))
    if not 1 <= len(parts) <= 50 or any(not _SYMBOLS.fullmatch(item) for item in parts):
        raise ProviderEngineQueryError("BENZINGA_QUERY_INVALID", invalid=("symbol",))
    return parts


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timestamp must match the documented RFC 1123 form") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include the documented timezone")
    return parsed


def _number(value: object, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("numeric metric has wrong shape")
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("numeric metric is invalid") from exc
    if isinstance(parsed, float) and not math.isfinite(parsed):
        raise ValueError("numeric metric must be finite")
    return parsed


def _nonempty_token(value: str | None, field: str) -> str | None:
    if value is not None and (not value.strip() or len(value) > 500):
        raise ProviderEngineQueryError("BENZINGA_QUERY_INVALID", invalid=(field,))
    return value


def _array_names(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("news tags must be an array")
    names: list[str] = []
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            raise ValueError("news tag item is malformed")
        names.append(item["name"])
    return names


class BenzingaAPIModelFetcher(Fetcher[Any, Any]):
    """Governed JSON transport and strict row boundary for the Benzinga adapters."""

    async_mode = "bounded_thread"
    http_transport: ClassVar[HttpGet | None] = None
    query_model: ClassVar[type[_Query]]
    row_model: ClassVar[type[ContractModel]]
    max_rows = 500

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
                "BENZINGA_QUERY_INVALID", missing=missing, rejected=rejected, invalid=invalid
            ) from None
        if params.source not in (None, self.capability.source):
            raise ProviderEngineQueryError("BENZINGA_QUERY_INVALID", invalid=("source",))
        if params.market not in (None, self.capability.market):
            raise ProviderEngineQueryError("BENZINGA_QUERY_INVALID", invalid=("market",))
        for name in ("analyst", "analyst_name", "firm", "firm_name"):
            _nonempty_token(getattr(params, name, None), name)
        if isinstance(params, _AnalystQuery):
            if params.page < 0 or params.page > 100_000 or not 1 <= params.page_size <= 1000:
                raise ProviderEngineQueryError(
                    "BENZINGA_QUERY_INVALID", invalid=("page", "page_size")
                )
            if params.updated is not None and params.updated < 0:
                raise ProviderEngineQueryError("BENZINGA_QUERY_INVALID", invalid=("updated",))
        if isinstance(params, _NewsQuery):
            if not 1 <= params.limit <= 500:
                raise ProviderEngineQueryError("BENZINGA_QUERY_INVALID", invalid=("limit",))
            if (
                params.start_date is not None
                and params.end_date is not None
                and params.start_date > params.end_date
            ):
                raise ProviderEngineQueryError(
                    "BENZINGA_QUERY_INVALID", invalid=("start_date", "end_date")
                )
        if isinstance(params, _CompanyNewsQuery):
            _symbols(params.symbol)
        return params

    def _token(self) -> str:
        token = os.environ.get(CREDENTIAL_ENV_VAR, "").strip()
        if not token:
            raise ProviderEngineError("BENZINGA_CREDENTIAL_MISSING")
        return token

    def _get(self, url: str, params: dict[str, str], ctx: FetchContext) -> HttpResponse:
        transport = self.http_transport or http_json._http_get_json
        timeout = min(float(ctx.timeout or 15.0), 15.0)
        response = transport(
            url,
            params,
            timeout=timeout,
            source=SOURCE,
            headers={"accept": "application/json"},
        )
        if not 200 <= response.status < 300:
            if response.status == 429:
                code = "BENZINGA_RATE_LIMITED"
            elif response.status in (401, 403):
                code = "BENZINGA_CREDENTIAL_REJECTED"
            elif response.status in (400, 422):
                code = "BENZINGA_QUERY_REJECTED"
            else:
                code = "BENZINGA_HTTP_ERROR"
            raise ProviderEngineError(code, status=response.status, url=url)
        return response

    def transform_data(self, raw: object, params: object) -> tuple[ContractModel, ...]:
        """Build contract rows from a list payload, refusing oversized input."""
        if not isinstance(raw, list) or len(raw) > self.max_rows:
            raise ProviderEngineError("BENZINGA_SHAPE_INVALID")
        rows: list[ContractModel] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ProviderEngineError("BENZINGA_SHAPE_INVALID")
            try:
                rows.append(
                    self.row_model.model_validate(self.map_record(item, params), strict=True)
                )
            except (KeyError, TypeError, ValueError, ValidationError):
                raise ProviderEngineError("BENZINGA_SHAPE_INVALID") from None
        return tuple(rows)

    def map_record(self, item: Mapping[str, object], params: object) -> dict[str, object]:
        """Abstract per-record hook; rows map through ``row_model``."""
        raise NotImplementedError


class BenzingaAnalystSearchFetcher(BenzingaAPIModelFetcher):
    """Search analysts and map their documented nested accuracy metrics."""

    canonical_model = "AnalystSearch"
    query_model = _AnalystQuery
    row_model = BenzingaAnalystSearch
    capability = Capability(
        source=SOURCE,
        domain="benzinga_analyst_search",
        asset_class="equity",
        period="na",
        market="us",
        verified=False,
        notes="Official response contract; no live validation.",
    )

    def extract_data(self, params: _AnalystQuery, ctx: FetchContext) -> list[dict[str, object]]:
        """Fetch one bounded page of analyst ratings metadata."""
        query = {
            "token": self._token(),
            "page": str(params.page),
            "pageSize": str(params.page_size),
        }
        for name in ("analyst", "analyst_name", "firm", "firm_name", "updated"):
            value = getattr(params, name)
            if value is not None:
                query[name] = str(value)
        document = self._get(f"{BASE_URL}/api/v2.1/calendar/ratings/analysts", query, ctx).document
        if not isinstance(document, dict):
            raise ProviderEngineError("BENZINGA_SHAPE_INVALID")
        items = document.get("analyst_ratings_analyst")
        if not isinstance(items, list) or len(items) > self.max_rows:
            raise ProviderEngineError("BENZINGA_SHAPE_INVALID")
        return items

    def map_record(self, item: Mapping[str, object], params: object) -> dict[str, object]:
        """Normalize one analyst record and its percentage accuracy metrics."""
        metrics = item.get("ratings_accuracy")
        if not isinstance(metrics, Mapping):
            raise ValueError("ratings_accuracy is required")
        accuracy: dict[str, object] = {}
        for source, target, kind, is_percent in _ACCURACY_FIELDS:
            raw = metrics.get(source)
            if raw is None:
                accuracy[target] = None
                continue
            value = _number(raw, integer=kind == "int")
            accuracy[target] = value / 100.0 if is_percent else value
        updated = item.get("updated")
        if isinstance(updated, bool) or not isinstance(updated, int):
            raise ValueError("updated must be an epoch-second integer")
        return {
            "firm_id": item.get("firm_id"),
            "firm_name": item.get("firm_name"),
            "analyst_id": item.get("id"),
            "name_first": item.get("name_first"),
            "name_full": item.get("name_full"),
            "name_last": item.get("name_last"),
            "ratings_accuracy": BenzingaAnalystAccuracy.model_validate(accuracy, strict=True),
            "updated": datetime.fromtimestamp(updated, timezone.utc),
        }


class _BenzingaNewsFetcher(BenzingaAPIModelFetcher):
    query_model: ClassVar[type[_Query]] = _NewsQuery
    max_rows = 500

    def extract_data(self, params: _NewsQuery, ctx: FetchContext) -> list[dict[str, object]]:
        token = self._token()
        pages: list[dict[str, object]] = []
        page_size = min(100, params.limit)
        for offset in range(0, params.limit, page_size):
            query = {
                "token": token,
                "page": str(offset),
                "pageSize": str(page_size),
                "sort": "created:asc",
                "displayOutput": "full",
            }
            if isinstance(params, _CompanyNewsQuery):
                query["tickers"] = ",".join(_symbols(params.symbol))
            if params.start_date is not None:
                query["dateFrom"] = params.start_date.isoformat()
            if params.end_date is not None:
                query["dateTo"] = params.end_date.isoformat()
            response = self._get(f"{BASE_URL}/api/v2/news", query, ctx).document
            if not isinstance(response, list) or len(response) > page_size:
                raise ProviderEngineError("BENZINGA_SHAPE_INVALID")
            if any(not isinstance(item, dict) for item in response):
                raise ProviderEngineError("BENZINGA_SHAPE_INVALID")
            pages.extend(response)
            if len(response) < page_size:
                break
        if isinstance(params, _CompanyNewsQuery):
            pages = [{**row, "_query_symbol": params.symbol} for row in pages]
        pages.sort(key=lambda row: _datetime(row.get("created")))
        return pages[: params.limit]

    def map_record(self, item: Mapping[str, object], params: object) -> dict[str, object]:
        channels = _array_names(item.get("channels"))
        stocks = _array_names(item.get("stocks"))
        tags = _array_names(item.get("tags"))
        mapped: dict[str, object] = {
            "id": item.get("id"),
            "author": item.get("author"),
            "created": _datetime(item.get("created")),
            "updated": _datetime(item.get("updated")),
            "title": item.get("title"),
            "teaser": item.get("teaser"),
            "body": item.get("body"),
            "url": item.get("url"),
            "channels": channels,
            "stocks": stocks,
            "tags": tags,
        }
        if "symbol" in self.row_model.model_fields:
            mapped["symbol"] = item.get("_query_symbol")
        return mapped


class BenzingaCompanyNewsFetcher(_BenzingaNewsFetcher):
    """Fetch bounded news items filtered to the requested company ticker."""

    canonical_model = "CompanyNews"
    query_model = _CompanyNewsQuery
    row_model = BenzingaCompanyNews
    capability = Capability(
        source=SOURCE,
        domain="benzinga_company_news",
        asset_class="equity",
        period="d",
        market="us",
        verified=False,
        notes="Official response contract; no live validation.",
    )


class BenzingaWorldNewsFetcher(_BenzingaNewsFetcher):
    """Fetch bounded world news items without a ticker filter."""

    canonical_model = "WorldNews"
    row_model = BenzingaWorldNews
    capability = Capability(
        source=SOURCE,
        domain="benzinga_world_news",
        asset_class="news",
        period="d",
        market="all",
        verified=False,
        notes="Official response contract; no live validation.",
    )


__all__ = [
    "BenzingaAnalystSearchFetcher",
    "BenzingaCompanyNewsFetcher",
    "BenzingaWorldNewsFetcher",
]
