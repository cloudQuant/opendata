"""Governed Intrinio v2 fetchers for news, filings, and data attributes.

Wire fields and routes follow Intrinio's public API reference. Output rows are
typed local contracts; the adapter intentionally does not infer units or point-
in-time semantics for financial data values.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol
from urllib.parse import quote

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from opendata.data.capability import Capability
from opendata.data.http_client import (
    HttpFetchError,
    get_shared_http_client,
)
from opendata.data.models.intrinio_news_attributes import (
    IntrinioCompanyFiling,
    IntrinioCompanyNews,
    IntrinioFredSeries,
    IntrinioHistoricalAttribute,
    IntrinioLatestAttribute,
    IntrinioNewsCompany,
    IntrinioNewsSecurity,
    IntrinioSearchAttribute,
    IntrinioWorldNews,
)
from opendata.data.protocol import FetchContext, Fetcher
from opendata.data.providers._engine.http_json import HttpGet, ProviderEngineError
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel


class _HttpClient(Protocol):
    """The governed GET surface used by this adapter and its offline seam."""

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str],
        timeout: float,
        source: str,
        cache_context: Mapping[str, str],
    ) -> object: ...


SOURCE = "intrinio"
BASE_URL = "https://api-v2.intrinio.com"
# B105 exception: this is an environment-variable name; os.environ.get reads the secret.
TOKEN_ENV = "INTRINIO_API_KEY"  # noqa: S105 - environment variable name, not a secret  # nosec B105
PAGE_SIZE = 100
MAX_ROWS = 5_000
MAX_REQUESTS = 50
MAX_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
MAX_TIMEOUT = 25.0


class IntrinioProviderError(ProviderEngineError):
    """Stable, sanitized errors from the Intrinio adapter.

    The adapter speaks the engine's typed refusal because a refusal a witness cannot attribute is
    a refusal nobody can check: ``code``/``status``/``url`` and the ``rejected``/``missing``/
    ``invalid`` faces are the same typed attributes the declarative intrinio models publish, and
    the message keeps this adapter's existing ``code status=N`` spelling. This class stays a
    :class:`RuntimeError` through the engine base, so every existing caller and test that caught
    the adapter's own errors catches them unchanged.
    """

    def __init__(self, code: str, *, status: int | None = None, url: str | None = None) -> None:
        """Create an adapter error using a non-sensitive code, status and request."""
        super().__init__(code, status=status, url=url)


class IntrinioShapeError(IntrinioProviderError):
    """An upstream response did not match the documented response shape."""


class _Query(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source: Literal["auto", "intrinio"] = "intrinio"
    market: Literal["us", "all"] | None = None


class _WindowQuery(_Query):
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def _ordered_window(self) -> _WindowQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self


def _validate_identifier_query(value: object, *, field_name: str | None, max_items: int) -> object:
    """Reject unsafe identifiers during query validation, before any transport."""
    if isinstance(value, str):
        values = tuple(part.strip() for part in value.split(","))
    elif isinstance(value, list):
        values = tuple(value)
    else:
        raise ValueError(f"{field_name} must be text or a list of text")
    if not 1 <= len(values) <= max_items:
        raise ValueError(f"{field_name} item count is outside the supported range")
    for item in values:
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or len(item) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in item)
        ):
            raise ValueError(f"{field_name} contains an invalid identifier")
    if len({item.casefold() for item in values}) != len(values):
        raise ValueError(f"{field_name} contains duplicates")
    return value


class _NewsQuery(_WindowQuery):
    limit: StrictInt = Field(default=100, gt=0, le=1_000)
    specific_source: StrictStr | None = None
    sentiment: Literal["positive", "neutral", "negative"] | None = None
    topic: StrictStr | None = None
    security: StrictStr | None = None
    language: StrictStr | None = None
    word_count_greater_than: StrictInt | None = Field(default=None, ge=0)
    word_count_less_than: StrictInt | None = Field(default=None, ge=0)
    is_spam: StrictBool | None = None
    business_relevance_greater_than: float | None = Field(default=None, ge=0, le=1)
    business_relevance_less_than: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _ordered_filters(self) -> _NewsQuery:
        if (
            self.word_count_greater_than is not None
            and self.word_count_less_than is not None
            and self.word_count_greater_than > self.word_count_less_than
        ):
            raise ValueError("word-count bounds must be ordered")
        if (
            self.business_relevance_greater_than is not None
            and self.business_relevance_less_than is not None
            and self.business_relevance_greater_than > self.business_relevance_less_than
        ):
            raise ValueError("business-relevance bounds must be ordered")
        return self


class _CompanyNewsQuery(_NewsQuery):
    symbol: StrictStr | list[StrictStr]

    @field_validator("symbol")
    @classmethod
    def _symbol_is_valid(cls, value: object) -> object:
        return _validate_identifier_query(value, field_name="symbol", max_items=10)


class _WorldNewsQuery(_NewsQuery):
    pass


class _CompanyFilingsQuery(_WindowQuery):
    symbol: StrictStr | None = None
    cik: StrictStr | None = None
    report_type: StrictStr | None = None
    limit: StrictInt = Field(default=100, gt=0, le=1_000)

    @model_validator(mode="after")
    def _identifier_required(self) -> _CompanyFilingsQuery:
        if self.symbol is None and self.cik is None:
            raise ValueError("symbol or cik is required")
        return self

    @field_validator("symbol", "cik")
    @classmethod
    def _identifier_is_valid(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_identifier_query(value, field_name="identifier", max_items=1)
        return value


class _SearchAttributesQuery(_Query):
    query: StrictStr
    limit: StrictInt = Field(default=1_000, gt=0, le=1_000)

    @model_validator(mode="after")
    def _nonempty_query(self) -> _SearchAttributesQuery:
        if not self.query.strip():
            raise ValueError("query must be nonempty")
        return self


class _LatestAttributesQuery(_Query):
    symbol: StrictStr | list[StrictStr]
    tag: StrictStr | list[StrictStr]
    value_type: Literal["number", "text"] = "number"

    @field_validator("symbol", "tag")
    @classmethod
    def _identifiers_are_valid(cls, value: object, info: ValidationInfo) -> object:
        return _validate_identifier_query(value, field_name=info.field_name, max_items=10)


class _HistoricalAttributesQuery(_WindowQuery):
    symbol: StrictStr | list[StrictStr]
    tag: StrictStr | list[StrictStr]
    frequency: Literal["daily", "weekly", "monthly", "quarterly", "yearly"] = "yearly"
    limit: StrictInt = Field(default=1_000, gt=0, le=MAX_ROWS)
    tag_type: StrictStr | None = None
    sort: Literal["asc", "desc"] = "desc"

    @field_validator("symbol", "tag")
    @classmethod
    def _identifiers_are_valid(cls, value: object, info: ValidationInfo) -> object:
        return _validate_identifier_query(value, field_name=info.field_name, max_items=10)


class _FredSeriesQuery(_WindowQuery):
    symbol: StrictStr
    limit: StrictInt = Field(default=1_000, gt=0, le=MAX_ROWS)

    @field_validator("symbol")
    @classmethod
    def _identifier_is_valid(cls, value: str) -> str:
        _validate_identifier_query(value, field_name="symbol", max_items=1)
        return value


class _CallBudget:
    """Per-fetch logical request cap in addition to the shared request grant."""

    def __init__(self) -> None:
        self.calls = 0

    def reserve(self) -> None:
        self.calls += 1
        if self.calls > MAX_REQUESTS:
            raise IntrinioProviderError("INTRINIO_REQUEST_CAP_EXCEEDED")


def _identifier(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise IntrinioProviderError("INTRINIO_QUERY_INVALID", status=None)
    if len(value) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise IntrinioProviderError("INTRINIO_QUERY_INVALID", status=None)
    return value


def _id_path(value: str) -> str:
    return quote(_identifier(value, name="identifier"), safe="-._~")


def _as_values(value: str | list[str], *, field_name: str, max_items: int) -> tuple[str, ...]:
    if isinstance(value, str):
        values = tuple(part.strip() for part in value.split(","))
    elif isinstance(value, list):
        values = tuple(value)
    else:
        raise IntrinioProviderError("INTRINIO_QUERY_INVALID")
    if not 1 <= len(values) <= max_items:
        raise IntrinioProviderError("INTRINIO_QUERY_INVALID")
    clean = tuple(_identifier(item, name=field_name) for item in values)
    if len({item.casefold() for item in clean}) != len(clean):
        raise IntrinioProviderError("INTRINIO_QUERY_INVALID")
    return clean


def _validate_source_market(params: _Query, capability: Capability) -> None:
    if params.source not in {"auto", SOURCE}:
        raise ValidationError.from_exception_data(
            type(params).__name__,
            [
                {
                    "type": "value_error",
                    "loc": ("source",),
                    "input": None,
                    "ctx": {"error": ValueError("source does not match provider")},
                }
            ],
        )
    if params.market is not None and params.market != capability.market:
        raise ValidationError.from_exception_data(
            type(params).__name__,
            [
                {
                    "type": "value_error",
                    "loc": ("market",),
                    "input": None,
                    "ctx": {"error": ValueError("market does not match provider capability")},
                }
            ],
        )


def _strict_object(value: object, *, code: str = "INTRINIO_SHAPE_INVALID") -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise IntrinioShapeError(code)
    return value


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("required text field is missing")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("text field has the wrong type")
    return value


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("integer field has the wrong type")
    return value


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError("boolean field has the wrong type")
    return value


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("numeric field has the wrong type")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError("numeric field is not finite")
    return converted


def _response_rows(
    document: object,
    key: str,
    *,
    max_rows: int,
) -> tuple[Mapping[str, object], ...]:
    obj = _strict_object(document)
    values = obj.get(key)
    if not isinstance(values, list) or len(values) > max_rows:
        raise IntrinioShapeError("INTRINIO_SHAPE_INVALID")
    return tuple(_strict_object(item) for item in values)


def _next_page(document: object) -> str | None:
    obj = _strict_object(document)
    if "next_page" not in obj:
        raise IntrinioShapeError("INTRINIO_PAGING_SHAPE_INVALID")
    cursor = obj["next_page"]
    if cursor is None:
        return None
    if (
        not isinstance(cursor, str)
        or not cursor
        or len(cursor) > 2_048
        or any(ord(char) < 32 or ord(char) == 127 for char in cursor)
    ):
        raise IntrinioShapeError("INTRINIO_PAGING_SHAPE_INVALID")
    return cursor


def _extract_typed_company(value: object) -> IntrinioNewsCompany | None:
    if value is None:
        return None
    item = _strict_object(value)
    fields = {key: item[key] for key in ("id", "ticker", "name", "lei", "cik") if key in item}
    try:
        return IntrinioNewsCompany.model_validate(fields)
    except Exception:
        raise IntrinioShapeError("INTRINIO_SHAPE_INVALID") from None


def _extract_typed_security(value: object) -> IntrinioNewsSecurity | None:
    if value is None:
        return None
    item = _strict_object(value)
    fields = {
        key: item[key] for key in ("id", "company_id", "ticker", "primary_listing") if key in item
    }
    try:
        return IntrinioNewsSecurity.model_validate(fields)
    except Exception:
        raise IntrinioShapeError("INTRINIO_SHAPE_INVALID") from None


def _first_nested(
    item: Mapping[str, object], *, direct: tuple[str, ...], collections: tuple[str, ...]
) -> object:
    for name in direct:
        if name in item and item[name] is not None:
            return item[name]
    for name in collections:
        value = item.get(name)
        if value is None:
            continue
        if not isinstance(value, list):
            raise IntrinioShapeError("INTRINIO_SHAPE_INVALID")
        if value:
            return value[0]
    return None


def _topic_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise IntrinioShapeError("INTRINIO_SHAPE_INVALID")
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
        else:
            record = _strict_object(item)
            name = record.get("name")
            if not isinstance(name, str):
                raise IntrinioShapeError("INTRINIO_SHAPE_INVALID")
            names.append(name)
    return ",".join(names)


def _news_record(
    item: Mapping[str, object],
    *,
    world: bool,
    requested_symbols: tuple[str, ...] = (),
) -> dict[str, object]:
    date_value = item.get("publication_date")
    if not isinstance(date_value, (str, date)):
        raise IntrinioShapeError("INTRINIO_SHAPE_INVALID")
    if isinstance(date_value, str) and not date_value:
        raise IntrinioShapeError("INTRINIO_SHAPE_INVALID")
    try:
        base: dict[str, object] = {
            "date": date_value,
            "title": _required_text(item.get("title")),
            "author": _optional_text(item.get("author")),
            "excerpt": _optional_text(item.get("summary")),
            "url": _required_text(item.get("url")),
            "symbols": None,
            "source": _optional_text(item.get("source")),
            "summary": _optional_text(item.get("summary")),
            "topics": _topic_text(item.get("topics")),
            "word_count": _optional_integer(item.get("word_count")),
            "business_relevance": _optional_number(item.get("business_relevance")),
            "sentiment": _optional_text(item.get("article_sentiment")),
            "sentiment_confidence": _optional_number(item.get("article_sentiment_confidence")),
            "language": _optional_text(item.get("language")),
            "spam": _optional_bool(item.get("spam")),
            "copyright": _optional_text(item.get("copyright")),
            "id": _required_text(item.get("id")),
        }
    except (TypeError, ValueError):
        raise IntrinioShapeError("INTRINIO_SHAPE_INVALID") from None

    company_raw = _first_nested(
        item,
        direct=("company", "issuer_company"),
        collections=("companies",),
    )
    security_raw = _first_nested(
        item,
        direct=("security", "issuer_security"),
        collections=("securities",),
    )
    company = _extract_typed_company(company_raw) if world else None
    security = _extract_typed_security(security_raw)

    discovered_symbols: list[str] = list(requested_symbols)
    discovered_symbols.extend(
        value.ticker for value in (company, security) if value is not None and value.ticker
    )
    for name in ("companies", "securities"):
        collection = item.get(name)
        if collection is None:
            continue
        if not isinstance(collection, list):
            raise IntrinioShapeError("INTRINIO_SHAPE_INVALID")
        for nested in collection:
            record = _strict_object(nested)
            ticker = record.get("ticker")
            if ticker is not None:
                if not isinstance(ticker, str):
                    raise IntrinioShapeError("INTRINIO_SHAPE_INVALID")
                discovered_symbols.append(ticker)
    if item.get("company") is not None and world and company is not None and company.ticker:
        discovered_symbols.append(company.ticker)
    if item.get("security") is not None and security is not None and security.ticker:
        discovered_symbols.append(security.ticker)
    base["symbols"] = ",".join(dict.fromkeys(discovered_symbols)) or None
    if world:
        base["company"] = company
    base["security"] = security
    return base


def _request_params(params: _WindowQuery, *, page_size: int) -> dict[str, str]:
    result = {"page_size": str(page_size)}
    if params.start_date is not None:
        result["start_date"] = params.start_date.isoformat()
    if params.end_date is not None:
        result["end_date"] = params.end_date.isoformat()
    return result


class _IntrinioFetcher(Fetcher[Any, Any]):
    """Shared governed transport and response parsing for this model slice."""

    async_mode = "bounded_thread"
    capability: ClassVar[Capability]
    query_model: ClassVar[type[BaseModel]]
    row_model: ClassVar[type[ContractModel]]
    canonical_model: ClassVar[str]
    http_client: _HttpClient | None = None
    http_transport: ClassVar[HttpGet | None] = None

    def transform_query(self, **kwargs: object) -> BaseModel:
        try:
            params = self.query_model.model_validate(kwargs)
        except Exception:
            raise
        if not isinstance(params, _Query):
            raise IntrinioProviderError("INTRINIO_QUERY_MODEL_INVALID")
        _validate_source_market(params, self.capability)
        return params

    def _key(self) -> str:
        key = os.environ.get(TOKEN_ENV, "")
        if not key or not key.strip() or len(key) > 4_096:
            raise IntrinioProviderError("INTRINIO_CREDENTIAL_MISSING_OR_INVALID")
        if any(ord(char) < 32 or ord(char) == 127 for char in key):
            raise IntrinioProviderError("INTRINIO_CREDENTIAL_MISSING_OR_INVALID")
        return key.strip()

    def _get_json(
        self,
        url: str,
        params: Mapping[str, str] | None,
        ctx: FetchContext,
        call_budget: _CallBudget,
    ) -> object:
        call_budget.reserve()
        timeout = DEFAULT_TIMEOUT if ctx.timeout is None else min(float(ctx.timeout), MAX_TIMEOUT)
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._key()}"}
        transport = self.http_transport
        if transport is not None:
            # The repository's offline witness seam (the same one EconDB and Benzinga adapters
            # speak): one declared request, answered in-memory from a fixture document. The
            # governed client below stays the production default whenever no transport is bound.
            response = transport(
                url,
                dict(params or {}),
                timeout=timeout,
                source=SOURCE,
                headers=headers,
            )
            if response.status == 429:
                raise IntrinioProviderError("INTRINIO_RATE_LIMITED", status=429, url=url)
            if response.status < 200 or response.status >= 300:
                raise IntrinioProviderError("INTRINIO_HTTP_ERROR", status=response.status, url=url)
            return response.document
        client = self.http_client if self.http_client is not None else get_shared_http_client()
        try:
            client_response = client.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
                source=SOURCE,
                cache_context={"model": self.canonical_model},
            )
        except RequestBudgetError:
            raise
        except HttpFetchError as exc:
            code = "INTRINIO_RATE_LIMITED" if exc.status == 429 else "INTRINIO_HTTP_ERROR"
            raise IntrinioProviderError(code, status=exc.status) from None
        except Exception:
            raise IntrinioProviderError("INTRINIO_TRANSPORT_ERROR") from None
        status = getattr(client_response, "status_code", None)
        if not isinstance(status, int):
            raise IntrinioShapeError("INTRINIO_HTTP_RESPONSE_INVALID")
        if status == 429:
            raise IntrinioProviderError("INTRINIO_RATE_LIMITED", status=status)
        if status < 200 or status >= 300:
            raise IntrinioProviderError("INTRINIO_HTTP_ERROR", status=status)
        body = getattr(client_response, "content", None)
        if not isinstance(body, bytes) or len(body) > MAX_BODY_BYTES:
            raise IntrinioShapeError("INTRINIO_BODY_INVALID")
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise IntrinioShapeError("INTRINIO_JSON_INVALID") from None
        return document

    def _pages(
        self,
        *,
        url: str,
        base_params: Mapping[str, str],
        row_key: str,
        row_limit: int,
        ctx: FetchContext,
        call_budget: _CallBudget,
    ) -> list[Mapping[str, object]]:
        rows: list[Mapping[str, object]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_REQUESTS):
            remaining = row_limit - len(rows)
            if remaining <= 0:
                return rows
            params = dict(base_params)
            params["page_size"] = str(min(PAGE_SIZE, remaining))
            if cursor is not None:
                params["next_page"] = cursor
            document = self._get_json(url, params, ctx, call_budget)
            page = _response_rows(document, row_key, max_rows=PAGE_SIZE)
            if len(page) > int(params["page_size"]):
                raise IntrinioShapeError("INTRINIO_PAGE_TOO_LARGE")
            rows.extend(page)
            next_cursor = _next_page(document)
            if next_cursor is None:
                return rows
            if next_cursor in seen:
                raise IntrinioShapeError("INTRINIO_CURSOR_REPEATED")
            seen.add(next_cursor)
            cursor = next_cursor
        raise IntrinioProviderError("INTRINIO_PAGE_CAP_EXCEEDED")

    def transform_data(self, raw: object, params: BaseModel) -> tuple[ContractModel, ...]:
        if not isinstance(raw, list) or len(raw) > MAX_ROWS:
            raise IntrinioShapeError("INTRINIO_NORMALIZED_ROWS_INVALID")
        try:
            return tuple(self.row_model.model_validate(row) for row in raw)
        except Exception:
            raise IntrinioShapeError("INTRINIO_NORMALIZED_ROW_INVALID") from None


class IntrinioCompanyNewsFetcher(_IntrinioFetcher):
    """Fetch and normalize news for one or more company identifiers."""

    canonical_model = "CompanyNews"
    query_model = _CompanyNewsQuery
    row_model = IntrinioCompanyNews
    capability = Capability(
        source=SOURCE,
        domain="intrinio_company_news",
        asset_class="equity",
        period="event",
        market="us",
        verified=False,
        notes="Official API contract only; no live validation.",
    )

    def extract_data(self, params: _CompanyNewsQuery, ctx: FetchContext) -> list[dict[str, object]]:
        """Fetch company news pages within the caller's request budget."""
        symbols = _as_values(params.symbol, field_name="symbol", max_items=10)
        self._key()
        budget = _CallBudget()
        rows: list[dict[str, object]] = []
        for symbol in symbols:
            if len(rows) >= params.limit:
                break
            query = _request_params(params, page_size=min(PAGE_SIZE, params.limit - len(rows)))
            query.update(_news_filters(params))
            page_rows = self._pages(
                url=f"{BASE_URL}/companies/{_id_path(symbol)}/news",
                base_params=query,
                row_key="news",
                row_limit=params.limit - len(rows),
                ctx=ctx,
                call_budget=budget,
            )
            for item in page_rows:
                rows.append(_news_record(item, world=False, requested_symbols=(symbol,)))
                if len(rows) >= params.limit:
                    break
        return rows


class IntrinioWorldNewsFetcher(_IntrinioFetcher):
    """Fetch and normalize the all-company news feed."""

    canonical_model = "WorldNews"
    query_model = _WorldNewsQuery
    row_model = IntrinioWorldNews
    capability = Capability(
        source=SOURCE,
        domain="intrinio_world_news",
        asset_class="news",
        period="event",
        market="us",
        verified=False,
        notes="Official API contract only; no live validation.",
    )

    def extract_data(self, params: _WorldNewsQuery, ctx: FetchContext) -> list[dict[str, object]]:
        """Fetch the global company-news route within the caller's budget."""
        self._key()
        budget = _CallBudget()
        query = _request_params(params, page_size=min(PAGE_SIZE, params.limit))
        query.update(_news_filters(params))
        page_rows = self._pages(
            url=f"{BASE_URL}/companies/news",
            base_params=query,
            row_key="news",
            row_limit=params.limit,
            ctx=ctx,
            call_budget=budget,
        )
        return [_news_record(item, world=True) for item in page_rows]


def _news_filters(params: _NewsQuery) -> dict[str, str]:
    result: dict[str, str] = {}
    for attr, upstream in (
        ("specific_source", "specific_source"),
        ("sentiment", "sentiment"),
        ("topic", "topic"),
        ("security", "security"),
        ("language", "language"),
        ("word_count_greater_than", "word_count_greater_than"),
        ("word_count_less_than", "word_count_less_than"),
        ("business_relevance_greater_than", "business_relevance_greater_than"),
        ("business_relevance_less_than", "business_relevance_less_than"),
    ):
        value = getattr(params, attr)
        if value is not None:
            result[upstream] = str(value)
    if params.is_spam is not None:
        result["is_spam"] = str(params.is_spam).lower()
    return result


class IntrinioCompanyFilingsFetcher(_IntrinioFetcher):
    """Fetch SEC filing references for a company symbol or CIK."""

    canonical_model = "CompanyFilings"
    query_model = _CompanyFilingsQuery
    row_model = IntrinioCompanyFiling
    capability = Capability(
        source=SOURCE,
        domain="intrinio_company_filings",
        asset_class="equity",
        period="event",
        market="us",
        verified=False,
        notes="Official API contract only; no live validation.",
    )

    def extract_data(
        self,
        params: _CompanyFilingsQuery,
        ctx: FetchContext,
    ) -> list[dict[str, object]]:
        """Fetch filing pages and preserve the documented source fields."""
        self._key()
        identifier = params.cik or params.symbol
        if identifier is None:
            raise IntrinioProviderError("INTRINIO_QUERY_INVALID")
        query = _request_params(params, page_size=min(PAGE_SIZE, params.limit - 0))
        if params.report_type is not None:
            query["report_type"] = params.report_type
        budget = _CallBudget()
        page_rows = self._pages(
            url=f"{BASE_URL}/companies/{_id_path(identifier)}/filings",
            base_params=query,
            row_key="filings",
            row_limit=params.limit,
            ctx=ctx,
            call_budget=budget,
        )
        return [self._map_filing(item, params) for item in page_rows]

    @staticmethod
    def _map_filing(item: Mapping[str, object], params: _CompanyFilingsQuery) -> dict[str, object]:
        try:
            company_value = item.get("company")
            company = _strict_object(company_value) if company_value is not None else {}
            return {
                "filing_date": item.get("filing_date"),
                "report_type": _optional_text(item.get("report_type")),
                "report_url": _required_text(item.get("report_url")),
                "filing_url": _optional_text(item.get("filing_url")),
                "symbol": (
                    _optional_text(company.get("ticker")) or params.symbol
                    if params.cik is not None
                    else params.symbol or _optional_text(company.get("ticker"))
                ),
                "cik": params.cik or _optional_text(company.get("cik")),
                "accepted_date": item.get("accepted_date"),
                "id": _required_text(item.get("id")),
                "period_end_date": item.get("period_end_date"),
                "sec_unique_id": _required_text(item.get("sec_unique_id")),
                "instance_url": _optional_text(item.get("instance_url")),
            }
        except (TypeError, ValueError):
            raise IntrinioShapeError("INTRINIO_SHAPE_INVALID") from None


class IntrinioSearchAttributesFetcher(_IntrinioFetcher):
    """Search the Intrinio financial data-tag catalog."""

    canonical_model = "SearchAttributes"
    query_model = _SearchAttributesQuery
    row_model = IntrinioSearchAttribute
    capability = Capability(
        source=SOURCE,
        domain="intrinio_search_attributes",
        asset_class="equity",
        period="snapshot",
        market="us",
        verified=False,
        notes="Official API contract only; no live validation.",
    )

    def extract_data(
        self,
        params: _SearchAttributesQuery,
        ctx: FetchContext,
    ) -> list[dict[str, object]]:
        """Fetch and normalize matching data-tag metadata."""
        self._key()
        budget = _CallBudget()
        document = self._get_json(
            f"{BASE_URL}/data_tags/search",
            {"query": params.query, "page_size": str(params.limit)},
            ctx,
            budget,
        )
        rows = _response_rows(document, "tags", max_rows=MAX_ROWS)
        if len(rows) > params.limit:
            raise IntrinioShapeError("INTRINIO_PAGE_TOO_LARGE")
        return [self._map_tag(item) for item in rows]

    @staticmethod
    def _map_tag(item: Mapping[str, object]) -> dict[str, object]:
        try:
            return {
                "id": _required_text(item.get("id")),
                "name": _required_text(item.get("name")),
                "tag": _required_text(item.get("tag")),
                "statement_code": _required_text(item.get("statement_code")),
                "statement_type": _optional_text(item.get("statement_type")),
                "parent_name": _optional_text(item.get("parent")),
                "sequence": _optional_integer(item.get("sequence")),
                "factor": _optional_text(item.get("factor")),
                "transaction": _optional_text(item.get("balance")),
                "type": _optional_text(item.get("type")),
                "unit": _optional_text(item.get("unit")),
            }
        except (TypeError, ValueError):
            raise IntrinioShapeError("INTRINIO_SHAPE_INVALID") from None


class IntrinioLatestAttributesFetcher(_IntrinioFetcher):
    """Fetch latest numeric or text values for security tags."""

    canonical_model = "LatestAttributes"
    query_model = _LatestAttributesQuery
    row_model = IntrinioLatestAttribute
    capability = Capability(
        source=SOURCE,
        domain="intrinio_latest_attributes",
        asset_class="equity",
        period="snapshot",
        market="us",
        verified=False,
        notes="Official API contract only; no live validation.",
    )

    def extract_data(
        self,
        params: _LatestAttributesQuery,
        ctx: FetchContext,
    ) -> list[dict[str, object]]:
        """Fetch one latest data point for each requested symbol/tag pair."""
        self._key()
        symbols = _as_values(params.symbol, field_name="symbol", max_items=10)
        tags = _as_values(params.tag, field_name="tag", max_items=10)
        pairs = [(symbol, tag) for symbol in symbols for tag in tags]
        if len(pairs) > MAX_REQUESTS:
            raise IntrinioProviderError("INTRINIO_QUERY_INVALID")
        budget = _CallBudget()
        output: list[dict[str, object]] = []
        for symbol, tag in pairs:
            suffix = params.value_type
            url = f"{BASE_URL}/securities/{_id_path(symbol)}/data_point/{_id_path(tag)}/{suffix}"
            document = _strict_object(self._get_json(url, None, ctx, budget))
            if "value" not in document:
                raise IntrinioShapeError("INTRINIO_SHAPE_INVALID")
            value = document["value"]
            if value is None:
                parsed_value: str | float | None = None
            elif params.value_type == "text":
                parsed_value = _required_text(value)
            else:
                try:
                    parsed_value = _optional_number(value)
                except ValueError:
                    raise IntrinioShapeError("INTRINIO_SHAPE_INVALID") from None
            output.append({"symbol": symbol, "tag": tag, "value": parsed_value})
        return output


class IntrinioHistoricalAttributesFetcher(_IntrinioFetcher):
    """Fetch historical numeric security-tag observations."""

    canonical_model = "HistoricalAttributes"
    query_model = _HistoricalAttributesQuery
    row_model = IntrinioHistoricalAttribute
    capability = Capability(
        source=SOURCE,
        domain="intrinio_historical_attributes",
        asset_class="equity",
        period="variable",
        market="us",
        verified=False,
        notes="Official API contract only; no live validation.",
    )

    def extract_data(
        self,
        params: _HistoricalAttributesQuery,
        ctx: FetchContext,
    ) -> list[dict[str, object]]:
        """Fetch bounded historical pages for each requested pair."""
        self._key()
        symbols = _as_values(params.symbol, field_name="symbol", max_items=10)
        tags = _as_values(params.tag, field_name="tag", max_items=10)
        pairs = [(symbol, tag) for symbol in symbols for tag in tags]
        if len(pairs) > MAX_REQUESTS:
            raise IntrinioProviderError("INTRINIO_QUERY_INVALID")
        budget = _CallBudget()
        output: list[dict[str, object]] = []
        for symbol, tag in pairs:
            remaining = params.limit - len(output)
            if remaining <= 0:
                break
            query: dict[str, str] = {
                "frequency": params.frequency,
                "sort_order": params.sort,
            }
            if params.start_date is not None:
                query["start_date"] = params.start_date.isoformat()
            if params.end_date is not None:
                query["end_date"] = params.end_date.isoformat()
            if params.tag_type is not None:
                query["type"] = params.tag_type
            rows = self._pages(
                url=(f"{BASE_URL}/securities/{_id_path(symbol)}/historical_data/{_id_path(tag)}"),
                base_params=query,
                row_key="historical_data",
                row_limit=remaining,
                ctx=ctx,
                call_budget=budget,
            )
            for item in rows:
                try:
                    output.append(
                        {
                            "date": item.get("date"),
                            "symbol": symbol,
                            "tag": tag,
                            "value": _optional_number(item.get("value")),
                        }
                    )
                except ValueError:
                    raise IntrinioShapeError("INTRINIO_SHAPE_INVALID") from None
                if len(output) >= params.limit:
                    break
        return output


class IntrinioFredSeriesFetcher(_IntrinioFetcher):
    """Fetch date/value observations for one Intrinio economic index."""

    canonical_model = "FredSeries"
    query_model = _FredSeriesQuery
    row_model = IntrinioFredSeries
    capability = Capability(
        source=SOURCE,
        domain="intrinio_fred_series",
        asset_class="macro",
        period="variable",
        market="us",
        verified=False,
        notes="Official API contract only; no live validation.",
    )

    def extract_data(
        self,
        params: _FredSeriesQuery,
        ctx: FetchContext,
    ) -> list[dict[str, object]]:
        """Fetch a bounded sequence of economic-index observations."""
        self._key()
        symbol = _identifier(params.symbol, name="symbol")
        query: dict[str, str] = {"tag": "level", "sort_order": "asc"}
        if params.start_date is not None:
            query["start_date"] = params.start_date.isoformat()
        if params.end_date is not None:
            query["end_date"] = params.end_date.isoformat()
        budget = _CallBudget()
        rows = self._pages(
            url=(f"{BASE_URL}/indices/economic/{_id_path(symbol)}/historical_data/level"),
            base_params=query,
            row_key="historical_data",
            row_limit=params.limit,
            ctx=ctx,
            call_budget=budget,
        )
        output: list[dict[str, object]] = []
        try:
            output = [
                {"date": item.get("date"), "value": _optional_number(item.get("value"))}
                for item in rows
            ]
        except ValueError:
            raise IntrinioShapeError("INTRINIO_SHAPE_INVALID") from None
        return output


__all__ = [
    "IntrinioCompanyFilingsFetcher",
    "IntrinioCompanyNewsFetcher",
    "IntrinioFredSeriesFetcher",
    "IntrinioHistoricalAttributesFetcher",
    "IntrinioLatestAttributesFetcher",
    "IntrinioProviderError",
    "IntrinioSearchAttributesFetcher",
    "IntrinioShapeError",
    "IntrinioWorldNewsFetcher",
]
