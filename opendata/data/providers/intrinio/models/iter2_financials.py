"""Governed Intrinio financial statements, ratios, metrics, and estimates."""

from __future__ import annotations

import datetime as dt
import os
from datetime import datetime
from typing import Annotated, ClassVar, Literal, TypeVar, cast
from urllib.parse import quote

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from opendata.data.capability import Capability

# Runtime `get_type_hints` resolves this name for `_PagedEstimateFetcher.output_model`.
from opendata.data.models.base import ContractModel  # noqa: TC001
from opendata.data.models.intrinio_financials import (
    FinancialValue,
    IntrinioBalanceSheet,
    IntrinioCashFlowStatement,
    IntrinioFinancialLineItem,
    IntrinioFinancialPeriod,
    IntrinioFinancialRatios,
    IntrinioForwardEbitdaEstimate,
    IntrinioForwardEpsEstimate,
    IntrinioForwardPeEstimate,
    IntrinioForwardSalesEstimate,
    IntrinioIncomeStatement,
    IntrinioKeyMetrics,
    IntrinioReportedDimension,
    IntrinioReportedFinancials,
    IntrinioReportedLineItem,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import (
    DEFAULT_TIMEOUT,
    HttpResponse,
    ProviderEngineError,
    _http_get_json,
)
from opendata.data.request_budget import (
    RequestAttemptLimitError,
    authorize_request,
    capture_request_scopes,
)

INTRINIO_BASE_URL = "https://api-v2.intrinio.com"
INTRINIO_HOST = "api-v2.intrinio.com"
INTRINIO_SOURCE = "intrinio"
MAX_FINANCIAL_PERIODS = 20
MAX_PAGES_PER_RESULT = 10
MAX_FACTS_PER_RESULT = 5_000
MAX_ESTIMATE_PAGES = 20

FinancialFrequency = Literal["annual", "quarter", "ttm", "ytd"]
EstimateFrequency = Literal["annual", "quarter"]
NonEmptyCursor = Annotated[StrictStr, Field(min_length=1)]
_M = TypeVar("_M", bound=BaseModel)


class IntrinioFinancialError(ProviderEngineError):
    """Stable, sanitized error raised by this financial slice."""


def _fail(code: str, *, status: int | None = None) -> IntrinioFinancialError:
    return IntrinioFinancialError(f"INTRINIO_FINANCIALS_{code}", status=status)


def _strict_validate(model: type[_M], payload: object) -> _M:
    try:
        return model.model_validate(payload)
    except (ValidationError, TypeError, ValueError):
        raise _fail("BAD_SHAPE") from None


class _Document(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class _CompanyRef(_Document):
    id: StrictStr | None = None
    ticker: StrictStr | None = None
    name: StrictStr | None = None
    lei: StrictStr | None = None
    cik: StrictStr | None = None


class _Fundamental(_Document):
    id: StrictStr
    statement_code: StrictStr
    fiscal_year: StrictInt
    fiscal_period: StrictStr
    type: Literal["reported", "restated", "calculated"]
    start_date: dt.date
    end_date: dt.date
    filing_date: StrictStr | None = None
    is_latest: StrictBool
    updated_date: StrictStr | None = None
    first_calculable: StrictStr | None = None
    earnings_disclosed_at: StrictStr | None = None
    standardized_signature: StrictStr | None = None
    reported_signature: StrictStr | None = None
    company: _CompanyRef | None = None


class _FundamentalListItem(_Document):
    id: StrictStr
    statement_code: StrictStr
    fiscal_year: StrictInt
    fiscal_period: StrictStr
    type: Literal["reported", "restated", "calculated"]
    start_date: dt.date
    end_date: dt.date
    filing_date: StrictStr | None = None
    is_latest: StrictBool
    updated_date: StrictStr | None = None
    company: _CompanyRef | None = None


class _FundamentalsPage(_Document):
    fundamentals: list[_FundamentalListItem]
    company: _CompanyRef | None = None
    next_page: NonEmptyCursor | None = None


class _DataTag(_Document):
    id: StrictStr | None = None
    name: StrictStr | None = None
    tag: StrictStr
    parent: StrictStr | None = None
    sequence: StrictInt | None = None
    factor: StrictStr | None = None
    balance: StrictStr | None = None
    unit: StrictStr


class _StandardizedFact(_Document):
    data_tag: _DataTag
    value: FinancialValue


class _ReportedDimension(_Document):
    axis: StrictStr
    member: StrictStr


class _XbrlTag(_Document):
    tag: StrictStr
    name: StrictStr | None = None
    balance: StrictStr | None = None
    unit: StrictStr | None = None
    abstract: StrictBool | None = None
    sequence: StrictInt | None = None
    depth: StrictInt | None = None


class _ReportedFact(_Document):
    xbrl_tag: _XbrlTag
    value: FinancialValue
    dimensions: list[_ReportedDimension] = Field(default_factory=list)


class _StandardizedPage(_Document):
    standardized_financials: list[_StandardizedFact]
    fundamental: _Fundamental
    company: _CompanyRef | None = None
    next_page: NonEmptyCursor | None = None


class _ReportedPage(_Document):
    reported_financials: list[_ReportedFact]
    fundamental: _Fundamental
    company: _CompanyRef | None = None
    next_page: NonEmptyCursor | None = None


class _FinancialPeriodRaw(_Document):
    fundamental: _Fundamental
    company: _CompanyRef | None = None
    standardized_facts: list[_StandardizedFact] = Field(default_factory=list)
    reported_facts: list[_ReportedFact] = Field(default_factory=list)


class _FinancialQuery(QueryParams):
    """One company and a source-supported financial frequency/date selection."""

    source: Literal["auto", "intrinio"] = "auto"
    market: Literal["us"] | None = None
    symbol: StrictStr
    period: FinancialFrequency = "annual"
    limit: StrictInt = Field(default=5, ge=1, le=MAX_FINANCIAL_PERIODS)
    fiscal_year: StrictInt | None = Field(default=None, ge=1900, le=2100)
    fundamental_type: Literal["reported", "restated", "calculated"] | None = None

    @field_validator("symbol")
    @classmethod
    def _symbol_token(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not symbol or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 for char in symbol
        ):
            raise ValueError("symbol must be a nonblank identifier without whitespace")
        return symbol

    @model_validator(mode="after")
    def _date_bounds(self) -> _FinancialQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self


class _BalanceQuery(_FinancialQuery):
    period: Literal["annual", "quarter"] = "annual"


class _ReportedQuery(_FinancialQuery):
    period: Literal["annual", "quarter"] = "annual"
    statement_type: Literal["balance", "income", "cash"] = "income"

    @model_validator(mode="after")
    def _reported_type(self) -> _ReportedQuery:
        if self.fundamental_type not in {None, "reported"}:
            raise ValueError("reported_financials only supports reported fundamentals")
        return self


class _EstimateQuery(QueryParams):
    source: Literal["auto", "intrinio"] = "auto"
    market: Literal["us"] | None = None
    symbol: StrictStr
    period: EstimateFrequency = "annual"
    limit: StrictInt = Field(default=5, ge=1, le=MAX_FINANCIAL_PERIODS)
    fiscal_year: StrictInt | None = Field(default=None, ge=1900, le=2100)
    fiscal_period: Literal["FY", "Q1", "Q2", "Q3", "Q4"] | None = None
    calendar_year: StrictInt | None = Field(default=None, ge=1900, le=2100)
    calendar_period: Literal["FY", "Q1", "Q2", "Q3", "Q4"] | None = None

    @field_validator("symbol")
    @classmethod
    def _symbol_token(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not symbol or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 for char in symbol
        ):
            raise ValueError("symbol must be a nonblank identifier without whitespace")
        return symbol

    @model_validator(mode="after")
    def _period_filters(self) -> _EstimateQuery:
        for value in (self.fiscal_period, self.calendar_period):
            if value is not None and ((self.period == "annual") != (value == "FY")):
                raise ValueError("period filters must match the requested annual/quarter frequency")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self


class _EbitdaEstimateQuery(_EstimateQuery):
    estimate_type: Literal[
        "ebitda", "ebit", "enterprise_value", "cash_flow_per_share", "pretax_income"
    ] = "ebitda"


class _ForwardPeQuery(QueryParams):
    source: Literal["auto", "intrinio"] = "auto"
    market: Literal["us"] | None = None
    symbol: StrictStr

    @field_validator("symbol")
    @classmethod
    def _symbol_token(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not symbol or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 for char in symbol
        ):
            raise ValueError("symbol must be a nonblank identifier without whitespace")
        return symbol


def _validate_query(model: type[_M], kwargs: dict[str, object]) -> _M:
    try:
        return model.model_validate(kwargs)
    except (ValidationError, TypeError, ValueError):
        raise _fail("BAD_QUERY") from None


def _period_type(period: FinancialFrequency) -> str:
    return {"annual": "FY", "quarter": "QTR", "ttm": "TTM", "ytd": "YTD"}[period]


def _matches_frequency(fiscal_period: str, period: FinancialFrequency) -> bool:
    value = fiscal_period.upper()
    if period == "annual":
        return value == "FY"
    if period == "quarter":
        return value in {"Q1", "Q2", "Q3", "Q4"}
    if period == "ttm":
        return value in {"Q1TTM", "Q2TTM", "Q3TTM"}
    return value in {"Q2YTD", "Q3YTD"}


def _date_from_source(value: str | None) -> dt.date | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed.date()
    except (TypeError, ValueError):
        try:
            return dt.date.fromisoformat(value.strip())
        except (TypeError, ValueError):
            raise _fail("BAD_SHAPE") from None


def _datetime_from_source(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        raise _fail("BAD_SHAPE")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt.date.fromisoformat(text)
        except ValueError:
            raise _fail("BAD_SHAPE") from None
        return None
    if parsed.tzinfo is None:
        raise _fail("BAD_SHAPE")
    return parsed


def _looks_like_ticker(identifier: str) -> bool:
    return (
        len(identifier) <= 12
        and any(char.isalpha() for char in identifier)
        and all(char.isalnum() or char in ".-_" for char in identifier)
    )


def _company_for_query(
    company: _CompanyRef | None,
    query_symbol: str,
) -> tuple[str, str | None, str | None, str | None]:
    actual = None if company is None else company.ticker
    if (
        actual is not None
        and _looks_like_ticker(query_symbol)
        and actual.upper() != query_symbol.upper()
    ):
        raise _fail("SYMBOL_MISMATCH")
    symbol = query_symbol if actual is None else actual.upper()
    return (
        symbol,
        None if company is None else company.name,
        None if company is None else company.cik,
        actual,
    )


def _request(
    url: str,
    params: dict[str, str],
    ctx: FetchContext,
) -> HttpResponse:
    api_key = os.getenv("INTRINIO_API_KEY", "")
    if not api_key.strip():
        raise _fail("CREDENTIAL_MISSING")
    try:
        response = _http_get_json(
            url,
            params,
            timeout=DEFAULT_TIMEOUT if ctx.timeout is None else ctx.timeout,
            source=INTRINIO_SOURCE,
            headers={"Authorization": f"Bearer {api_key.strip()}"},
        )
    except ProviderEngineError as exc:
        status = exc.status
        codes = {
            400: "QUERY_REJECTED",
            401: "CREDENTIAL_REJECTED",
            403: "CREDENTIAL_REJECTED",
            404: "RESOURCE_MISSING",
            422: "QUERY_REJECTED",
            429: "RATE_LIMITED",
        }
        if status in codes:
            raise _fail(codes[status], status=status) from None
        if status is not None and status >= 500:
            raise _fail("UPSTREAM_UNAVAILABLE", status=status) from None
        raise _fail("HTTP_ERROR", status=status) from None
    except Exception:
        raise _fail("HTTP_ERROR") from None
    if not isinstance(response, HttpResponse):
        raise _fail("BAD_RESPONSE")
    if 200 <= response.status < 300:
        return response
    codes = {
        400: "QUERY_REJECTED",
        401: "CREDENTIAL_REJECTED",
        403: "CREDENTIAL_REJECTED",
        404: "RESOURCE_MISSING",
        422: "QUERY_REJECTED",
        429: "RATE_LIMITED",
    }
    if response.status in codes:
        raise _fail(codes[response.status], status=response.status)
    if response.status >= 500:
        raise _fail("UPSTREAM_UNAVAILABLE", status=response.status)
    raise _fail("HTTP_ERROR", status=response.status)


def _preflight(ctx: FetchContext, minimum_attempts: int) -> None:
    """Ensure even the minimum result fanout fits before the first transport attempt."""
    scopes = capture_request_scopes()
    policy = authorize_request(scopes, INTRINIO_SOURCE, INTRINIO_HOST)
    if policy is None:
        return
    budget = policy.budget
    remaining = min(
        policy.task_attempts - budget.attempts_used,
        policy.source_attempts - budget.attempts_used_for(INTRINIO_SOURCE),
    )
    if minimum_attempts > remaining:
        raise RequestAttemptLimitError("request attempt allowance cannot fit financial fanout")


def _fundamentals_params(
    query: _FinancialQuery, statement_code: str, *, reported_only: bool
) -> dict[str, str]:
    params = {
        "statement_code": statement_code,
        "type": _period_type(query.period),
        "page_size": str(query.limit),
        "latest_only": "true",
    }
    if query.fiscal_year is not None:
        params["fiscal_year"] = str(query.fiscal_year)
    if query.start_date is not None:
        params["start_date"] = query.start_date.isoformat()
    if query.end_date is not None:
        params["end_date"] = query.end_date.isoformat()
    if query.fundamental_type is not None:
        params["fundamental_type"] = query.fundamental_type
    if reported_only:
        params["reported_only"] = "true"
    return params


def _fundamental_pages(
    query: _FinancialQuery,
    ctx: FetchContext,
    *,
    statement_code: str,
    reported_only: bool = False,
) -> tuple[_FundamentalListItem, ...]:
    url = f"{INTRINIO_BASE_URL}/companies/{quote(query.symbol, safe='')}/fundamentals"
    params = _fundamentals_params(query, statement_code, reported_only=reported_only)
    selected: list[_FundamentalListItem] = []
    seen_cursors: set[str] = set()
    page_count = 0
    while len(selected) < query.limit:
        page_count += 1
        if page_count > MAX_PAGES_PER_RESULT:
            raise _fail("PAGE_LIMIT")
        page = _strict_validate(_FundamentalsPage, _request(url, params, ctx).document)
        for item in page.fundamentals:
            if item.statement_code != statement_code:
                raise _fail("STATEMENT_MISMATCH")
            if not _matches_frequency(item.fiscal_period, query.period):
                raise _fail("FREQUENCY_MISMATCH")
            if query.fundamental_type is not None and item.type != query.fundamental_type:
                continue
            if (
                item.company is not None
                and page.company is not None
                and item.company.id is not None
                and page.company.id is not None
                and item.company.id != page.company.id
            ):
                raise _fail("COMPANY_MISMATCH")
            company = item.company or page.company
            if company is None:
                raise _fail("MISSING_COMPANY_IDENTITY")
            _company_for_query(company, query.symbol)
            selected.append(item.model_copy(update={"company": company}))
            if len(selected) == query.limit:
                break
        if len(selected) >= query.limit or page.next_page is None:
            break
        if page.next_page in seen_cursors:
            raise _fail("PAGING_CURSOR_REPEATED")
        if not page.fundamentals:
            raise _fail("PAGING_NO_PROGRESS")
        seen_cursors.add(page.next_page)
        params["next_page"] = page.next_page
    if len({item.id for item in selected}) != len(selected):
        raise _fail("DUPLICATE_FUNDAMENTAL")
    return tuple(selected)


def _company_from_period(
    list_item: _FundamentalListItem,
    detail_company: _CompanyRef | None,
    detail_fundamental: _Fundamental | None = None,
) -> _CompanyRef | None:
    candidates = [list_item.company, detail_company]
    if detail_fundamental is not None:
        candidates.append(detail_fundamental.company)
    present = [company for company in candidates if company is not None]
    if not present:
        return None
    first = present[0]
    for candidate in present[1:]:
        for left, right in (
            (first.id, candidate.id),
            (first.ticker, candidate.ticker),
            (first.cik, candidate.cik),
        ):
            if left is not None and right is not None and left.casefold() != right.casefold():
                raise _fail("COMPANY_MISMATCH")
    return first


def _validate_fundamental_match(
    detail: _Fundamental,
    list_item: _FundamentalListItem,
    *,
    statement_code: str,
    query: _FinancialQuery,
) -> None:
    if (
        detail.id != list_item.id
        or detail.statement_code != statement_code
        or detail.fiscal_year != list_item.fiscal_year
        or detail.fiscal_period != list_item.fiscal_period
        or detail.end_date != list_item.end_date
        or detail.type != list_item.type
    ):
        raise _fail("FUNDAMENTAL_MISMATCH")
    if not _matches_frequency(detail.fiscal_period, query.period):
        raise _fail("FREQUENCY_MISMATCH")
    if query.fundamental_type is not None and detail.type != query.fundamental_type:
        raise _fail("FUNDAMENTAL_TYPE_MISMATCH")


def _standardized_result(
    query: _FinancialQuery,
    ctx: FetchContext,
    *,
    statement_code: str,
    reported_only: bool = False,
) -> list[_FinancialPeriodRaw]:
    fundamentals = _fundamental_pages(
        query,
        ctx,
        statement_code=statement_code,
        reported_only=reported_only,
    )
    rows: list[_FinancialPeriodRaw] = []
    for item in fundamentals:
        path_id = quote(item.id, safe="")
        url = f"{INTRINIO_BASE_URL}/fundamentals/{path_id}/standardized_financials"
        params: dict[str, str] = {}
        facts: list[_StandardizedFact] = []
        seen_cursors: set[str] = set()
        detail_company: _CompanyRef | None = None
        detail_fundamental: _Fundamental | None = None
        page_count = 0
        while True:
            page_count += 1
            if page_count > MAX_PAGES_PER_RESULT:
                raise _fail("PAGE_LIMIT")
            page = _strict_validate(_StandardizedPage, _request(url, params, ctx).document)
            _validate_fundamental_match(
                page.fundamental,
                item,
                statement_code=statement_code,
                query=query,
            )
            if detail_fundamental is None:
                detail_fundamental = page.fundamental
                detail_company = page.company
            elif detail_fundamental != page.fundamental:
                raise _fail("PAGING_FUNDAMENTAL_CHANGED")
            if len(facts) + len(page.standardized_financials) > MAX_FACTS_PER_RESULT:
                raise _fail("FACT_LIMIT")
            facts.extend(page.standardized_financials)
            if page.next_page is None:
                break
            if page.next_page in seen_cursors:
                raise _fail("PAGING_CURSOR_REPEATED")
            if not page.standardized_financials:
                raise _fail("PAGING_NO_PROGRESS")
            seen_cursors.add(page.next_page)
            params = {"next_page": page.next_page}
        if detail_fundamental is None:
            raise _fail("MISSING_FUNDAMENTAL")
        if not facts:
            raise _fail("MISSING_STANDARDIZED_FACTS")
        rows.append(
            _FinancialPeriodRaw(
                fundamental=detail_fundamental,
                company=_company_from_period(item, detail_company, detail_fundamental),
                standardized_facts=facts,
            )
        )
    return rows


def _reported_result(
    query: _FinancialQuery,
    ctx: FetchContext,
    *,
    statement_code: str,
) -> list[_FinancialPeriodRaw]:
    fundamentals = _fundamental_pages(
        query,
        ctx,
        statement_code=statement_code,
        reported_only=True,
    )
    rows: list[_FinancialPeriodRaw] = []
    for item in fundamentals:
        url = f"{INTRINIO_BASE_URL}/fundamentals/{quote(item.id, safe='')}/reported_financials"
        params: dict[str, str] = {}
        facts: list[_ReportedFact] = []
        seen_cursors: set[str] = set()
        detail_company: _CompanyRef | None = None
        detail_fundamental: _Fundamental | None = None
        page_count = 0
        while True:
            page_count += 1
            if page_count > MAX_PAGES_PER_RESULT:
                raise _fail("PAGE_LIMIT")
            page = _strict_validate(_ReportedPage, _request(url, params, ctx).document)
            _validate_fundamental_match(
                page.fundamental,
                item,
                statement_code=statement_code,
                query=query,
            )
            if detail_fundamental is None:
                detail_fundamental = page.fundamental
                detail_company = page.company
            elif detail_fundamental != page.fundamental:
                raise _fail("PAGING_FUNDAMENTAL_CHANGED")
            if len(facts) + len(page.reported_financials) > MAX_FACTS_PER_RESULT:
                raise _fail("FACT_LIMIT")
            facts.extend(page.reported_financials)
            if page.next_page is None:
                break
            if page.next_page in seen_cursors:
                raise _fail("PAGING_CURSOR_REPEATED")
            if not page.reported_financials:
                raise _fail("PAGING_NO_PROGRESS")
            seen_cursors.add(page.next_page)
            params = {"next_page": page.next_page}
        if detail_fundamental is None:
            raise _fail("MISSING_FUNDAMENTAL")
        if not facts:
            raise _fail("MISSING_REPORTED_FACTS")
        rows.append(
            _FinancialPeriodRaw(
                fundamental=detail_fundamental,
                company=_company_from_period(item, detail_company, detail_fundamental),
                reported_facts=facts,
            )
        )
    return rows


def _line_items(facts: list[_StandardizedFact]) -> list[IntrinioFinancialLineItem]:
    return [
        IntrinioFinancialLineItem(
            data_tag_id=fact.data_tag.id,
            tag=fact.data_tag.tag,
            name=fact.data_tag.name,
            value=fact.value,
            unit=fact.data_tag.unit,
            parent=fact.data_tag.parent,
            sequence=fact.data_tag.sequence,
            factor=fact.data_tag.factor,
            balance=fact.data_tag.balance,
        )
        for fact in facts
    ]


def _unit_key(value: str) -> str:
    normalized = " ".join(value.casefold().replace("_", " ").split())
    aliases = {
        "usd": "usd",
        "shares": "shares",
        "usd per share": "usd_per_share",
        "usdpershare": "usd_per_share",
        "float": "float",
        "multiple": "multiple",
        "percentage": "percentage",
    }
    return aliases.get(normalized, normalized)


def _reported_line_items(facts: list[_ReportedFact]) -> list[IntrinioReportedLineItem]:
    return [
        IntrinioReportedLineItem(
            tag=fact.xbrl_tag.tag,
            name=fact.xbrl_tag.name,
            value=fact.value,
            unit=fact.xbrl_tag.unit,
            balance=fact.xbrl_tag.balance,
            abstract=fact.xbrl_tag.abstract,
            sequence=fact.xbrl_tag.sequence,
            depth=fact.xbrl_tag.depth,
            dimensions=[
                IntrinioReportedDimension(axis=d.axis, member=d.member) for d in fact.dimensions
            ],
        )
        for fact in facts
    ]


def _mapped_financial_row(
    raw: _FinancialPeriodRaw,
    *,
    query_symbol: str,
    output_model: type[IntrinioFinancialPeriod],
    tag_mapping: dict[str, str],
    tag_units: dict[str, str],
) -> IntrinioFinancialPeriod:
    fundamental = raw.fundamental
    symbol, company_name, cik, _ = _company_for_query(raw.company, query_symbol)
    values: dict[str, object] = {
        "symbol": symbol,
        "name": company_name,
        "period_ending": fundamental.end_date,
        "fiscal_period": fundamental.fiscal_period,
        "fiscal_year": fundamental.fiscal_year,
        "filing_date": _date_from_source(fundamental.filing_date),
        "filed_at": _datetime_from_source(fundamental.filing_date),
        "accepted_date": None,
        "cik": cik or (None if raw.company is None else raw.company.cik),
        "fundamental_id": fundamental.id,
        "fundamental_type": fundamental.type,
        "statement_code": fundamental.statement_code,
        "reported_currency": None,
        "is_latest": fundamental.is_latest,
        "line_items": _line_items(raw.standardized_facts),
    }
    fields = output_model.model_fields
    for fact in raw.standardized_facts:
        output_field = tag_mapping.get(fact.data_tag.tag)
        if output_field is None or output_field not in fields:
            continue
        expected_unit = tag_units.get(fact.data_tag.tag)
        if expected_unit is None or _unit_key(fact.data_tag.unit) != expected_unit:
            raise _fail("TAG_UNIT_MISMATCH")
        current = values.get(output_field)
        if current is not None and current != fact.value:
            raise _fail("AMBIGUOUS_TAG_MAPPING")
        values[output_field] = fact.value
    try:
        return output_model.model_validate(values)
    except (ValidationError, TypeError, ValueError):
        raise _fail("CONTRACT_INVALID") from None


def _reported_row(raw: _FinancialPeriodRaw, query_symbol: str) -> IntrinioReportedFinancials:
    fundamental = raw.fundamental
    symbol, _, cik, _ = _company_for_query(raw.company, query_symbol)
    try:
        return IntrinioReportedFinancials(
            symbol=symbol,
            period_ending=fundamental.end_date,
            fiscal_period=fundamental.fiscal_period,
            fiscal_year=fundamental.fiscal_year,
            filing_date=_date_from_source(fundamental.filing_date),
            filed_at=_datetime_from_source(fundamental.filing_date),
            accepted_date=None,
            cik=cik or (None if raw.company is None else raw.company.cik),
            fundamental_id=fundamental.id,
            fundamental_type=fundamental.type,
            statement_code=fundamental.statement_code,
            reported_currency=None,
            is_latest=fundamental.is_latest,
            line_items=_reported_line_items(raw.reported_facts),
        )
    except (ValidationError, TypeError, ValueError):
        raise _fail("CONTRACT_INVALID") from None


class _StandardizedFinancialFetcher(Fetcher[_FinancialQuery, list[_FinancialPeriodRaw]]):
    """Two-stage fundamentals lookup with strict output and per-model tag mapping."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str]
    statement_code: ClassVar[str]
    output_model: ClassVar[type[IntrinioFinancialPeriod]]
    tag_mapping: ClassVar[dict[str, str]] = {}
    tag_units: ClassVar[dict[str, str]] = {}
    query_model: ClassVar[type[_FinancialQuery]] = _FinancialQuery
    capability: ClassVar[Capability]

    def transform_query(self, **kwargs: object) -> _FinancialQuery:
        return _validate_query(self.query_model, kwargs)

    def extract_data(self, params: _FinancialQuery, ctx: FetchContext) -> list[_FinancialPeriodRaw]:
        _preflight(ctx, 1 + params.limit)
        return _standardized_result(params, ctx, statement_code=self.statement_code)

    def transform_data(
        self,
        raw: list[_FinancialPeriodRaw],
        params: _FinancialQuery,
    ) -> FetchResult:
        if len(raw) > params.limit:
            raise _fail("PROVIDER_LIMIT_VIOLATION")
        return tuple(
            _mapped_financial_row(
                row,
                query_symbol=params.symbol,
                output_model=self.output_model,
                tag_mapping=self.tag_mapping,
                tag_units=self.tag_units,
            )
            for row in raw
        )


class IntrinioBalanceSheetFetcher(_StandardizedFinancialFetcher):
    """Fetch typed balance-sheet columns and verified USD tag aliases."""

    canonical_model = "BalanceSheet"
    statement_code = "balance_sheet_statement"
    output_model = IntrinioBalanceSheet
    query_model = _BalanceQuery
    tag_mapping = {
        "cashandequivalents": "cash_and_cash_equivalents",
        "shortterminvestments": "short_term_investments",
        "accountsreceivable": "accounts_receivables",
        "netinventory": "inventory",
        "othercurrentassets": "other_current_assets",
        "totalcurrentassets": "total_current_assets",
        "netppe": "plant_property_equipment_net",
        "goodwill": "goodwill",
        "intangibleassets": "intangible_assets",
        "longterminvestments": "long_term_investments",
        "currentdeferredtaxassets": "tax_assets",
        "othernoncurrentassets": "other_non_current_assets",
        "totalnoncurrentassets": "non_current_assets",
        "otherassets": "other_assets",
        "totalassets": "total_assets",
        "accountspayable": "accounts_payable",
        "prepaidexpenses": "prepaid_expenses",
        "accruedexpenses": "accrued_expenses",
        "currentdeferredtaxliabilities": "tax_payables",
        "shorttermdebt": "short_term_debt",
        "currentdeferredrevenue": "current_deferred_revenue",
        "othercurrentliabilities": "other_current_liabilities",
        "othershorttermpayables": "other_payables",
        "totalcurrentliabilities": "total_current_liabilities",
        "longtermdebt": "long_term_debt",
        "noncurrentdeferredrevenue": "deferred_revenue_non_current",
        "noncurrentdeferredtaxliabilities": "deferred_tax_liabilities_non_current",
        "otherlongtermliabilities": "other_non_current_liabilities",
        "totalnoncurrentliabilities": "total_non_current_liabilities",
        "capitalleaseobligations": "capital_lease_obligations",
        "totalliabilities": "total_liabilities",
        "commonequity": "common_stock",
        "treasurystock": "treasury_stock",
        "retainedearnings": "retained_earnings",
        "aoci": "accumulated_other_comprehensive_income",
        "otherequity": "other_shareholders_equity",
        "totalcommonequity": "total_common_equity",
        "noncontrollinginterests": "minority_interest",
        "totalequityandnoncontrollinginterests": "total_equity_non_controlling_interests",
        "totalliabilitiesandequity": "total_liabilities_and_shareholders_equity",
    }
    tag_units = dict.fromkeys(tag_mapping, "usd")
    capability = Capability(
        asset_class="equity",
        domain="intrinio_balance_sheet",
        period="periodic",
        market="us",
        source="intrinio",
        verified=False,
        notes="Official Intrinio standardized fundamentals contract; no live-provider verification",
    )


class IntrinioCashFlowStatementFetcher(_StandardizedFinancialFetcher):
    """Fetch typed cash-flow columns and verified USD tag aliases."""

    canonical_model = "CashFlowStatement"
    statement_code = "cash_flow_statement"
    output_model = IntrinioCashFlowStatement
    tag_mapping = {
        "netincome": "net_income",
        "increasedecreaseinoperatingcapital": "change_in_working_capital",
        "netcashfromoperatingactivities": "net_cash_from_operating_activities",
        "purchaseofplantpropertyandequipment": "purchase_of_property_plant_and_equipment",
        "acquisitions": "acquisitions",
        "purchaseofinvestments": "purchase_of_investment_securities",
        "saleofinvestments": "sale_and_maturity_of_investments",
        "otherinvestingactivitiesnet": "other_investing_activities",
        "netcashfrominvestingactivities": "net_cash_from_investing_activities",
        "repaymentofdebt": "repayment_of_debt",
        "paymentofdividends": "net_dividends_paid",
        "otherfinancingactivitiesnet": "other_financing_activities",
        "netcashfromfinancingactivities": "net_cash_from_financing_activities",
        "effectofexchangeratechanges": "effect_of_exchange_rate_changes_on_cash",
        "netchangeincash": "net_change_in_cash_and_equivalents",
    }
    tag_units = dict.fromkeys(tag_mapping, "usd")
    capability = Capability(
        asset_class="equity",
        domain="intrinio_cash_flow_statement",
        period="periodic",
        market="us",
        source="intrinio",
        verified=False,
        notes="Official Intrinio standardized fundamentals contract; no live-provider verification",
    )


class IntrinioIncomeStatementFetcher(_StandardizedFinancialFetcher):
    """Fetch typed income-statement columns and verified tag aliases."""

    canonical_model = "IncomeStatement"
    statement_code = "income_statement"
    output_model = IntrinioIncomeStatement
    tag_mapping = {
        "operatingrevenue": "operating_revenue",
        "totalrevenue": "revenue",
        "operatingcostofrevenue": "operating_cost_of_revenue",
        "totalcostofrevenue": "cost_of_revenue",
        "totalgrossprofit": "gross_profit",
        "sgaexpense": "selling_general_and_admin_expense",
        "rdexpense": "research_and_development_expense",
        "totaloperatingexpenses": "total_operating_expenses",
        "totaloperatingincome": "total_operating_income",
        "totalinterestincome": "interest_income",
        "totalinterestexpense": "total_interest_expense",
        "totalotherincome": "total_other_income_expenses",
        "totalpretaxincome": "total_pre_tax_income",
        "incometaxexpense": "income_tax_expense",
        "netincomecontinuing": "net_income_from_continuing_operations",
        "netincomediscontinued": "net_income_from_discontinued_operations",
        "netincome": "consolidated_net_income",
        "netincometocommon": "bottom_line_net_income",
        "otheradjustmentstoconsolidatednetincome": "other_adjustments_to_net_income",
        "weightedavebasicsharesos": "weighted_average_basic_shares_outstanding",
        "basiceps": "basic_earnings_per_share",
        "weightedavedilutedsharesos": "weighted_average_diluted_shares_outstanding",
        "dilutedeps": "diluted_earnings_per_share",
        "weightedavebasicdilutedsharesos": "weighted_average_basic_and_diluted_shares_outstanding",
        "cashdividendspershare": "cash_dividends_to_common_per_share",
    }
    tag_units = dict.fromkeys(tag_mapping, "usd")
    tag_units.update(
        {
            "weightedavebasicsharesos": "shares",
            "weightedavedilutedsharesos": "shares",
            "weightedavebasicdilutedsharesos": "shares",
            "basiceps": "usd_per_share",
            "dilutedeps": "usd_per_share",
            "cashdividendspershare": "usd_per_share",
        }
    )
    capability = Capability(
        asset_class="equity",
        domain="intrinio_income_statement",
        period="periodic",
        market="us",
        source="intrinio",
        verified=False,
        notes="Official Intrinio standardized fundamentals contract; no live-provider verification",
    )


class IntrinioFinancialRatiosFetcher(_StandardizedFinancialFetcher):
    """Fetch calculation tags with known numeric and multiple units."""

    canonical_model = "FinancialRatios"
    statement_code = "calculations"
    output_model = IntrinioFinancialRatios
    tag_mapping = {
        "arturnover": "receivables_turnover",
        "apturnover": "payables_turnover",
        "invturnover": "inventory_turnover",
        "faturnover": "fixed_asset_turnover",
        "assetturnover": "asset_turnover",
        "currentratio": "current_ratio",
        "quickratio": "quick_ratio",
        "pricetoearnings": "price_to_earnings",
        "pricetobook": "price_to_book",
        "pricetorevenue": "price_to_sales",
        "ebittointerestex": "interest_coverage_ratio",
        "debttoequity": "debt_to_equity",
        "finleverage": "financial_leverage_ratio",
        "bookvaluepershare": "book_value_per_share",
        "cashpershare": "cash_per_share",
    }
    tag_units = dict.fromkeys(tag_mapping, "float")
    tag_units.update(
        {
            "pricetoearnings": "multiple",
            "pricetobook": "multiple",
            "pricetorevenue": "multiple",
            "bookvaluepershare": "usd_per_share",
            "cashpershare": "usd_per_share",
        }
    )
    capability = Capability(
        asset_class="equity",
        domain="intrinio_financial_ratios",
        period="periodic",
        market="us",
        source="intrinio",
        verified=False,
        notes="Official Intrinio calculations fundamentals; unmapped ratios remain null",
    )


class IntrinioKeyMetricsFetcher(_StandardizedFinancialFetcher):
    """Fetch key metrics using only officially documented tag aliases."""

    canonical_model = "KeyMetrics"
    statement_code = "calculations"
    output_model = IntrinioKeyMetrics
    tag_mapping = {
        "marketcap": "market_cap",
        "enterprisevalue": "enterprise_value",
        "evtorevenue": "ev_to_sales",
        "evtoocf": "ev_to_operating_cash_flow",
        "evtofcff": "ev_to_free_cash_flow",
        "evtoebitda": "ev_to_ebitda",
        "netdebttoebitda": "net_debt_to_ebitda",
        "pricetoearnings": "pe_ratio",
        "pricetorevenue": "price_to_sales",
        "pricetobook": "price_to_book",
        "bookvaluepershare": "book_value_per_share",
        "debttoequity": "debt_to_equity",
        "ltdebttoequity": "long_term_debt_to_equity",
        "quickratio": "quick_ratio",
        "currentratio": "current_ratio",
        "nwc": "working_capital",
        "investedcapital": "invested_capital",
    }
    tag_units = dict.fromkeys(tag_mapping, "float")
    tag_units.update(
        {
            "marketcap": "usd",
            "enterprisevalue": "usd",
            "nwc": "usd",
            "investedcapital": "usd",
            "evtorevenue": "multiple",
            "evtoocf": "multiple",
            "evtofcff": "multiple",
            "evtoebitda": "multiple",
            "pricetoearnings": "multiple",
            "pricetorevenue": "multiple",
            "pricetobook": "multiple",
            "bookvaluepershare": "usd_per_share",
        }
    )
    capability = Capability(
        asset_class="equity",
        domain="intrinio_key_metrics",
        period="periodic",
        market="us",
        source="intrinio",
        verified=False,
        notes="Official Intrinio calculations fundamentals; unmapped metrics remain null",
    )


class IntrinioReportedFinancialsFetcher(Fetcher[_ReportedQuery, list[_FinancialPeriodRaw]]):
    """As-reported XBRL facts with filing metadata and dimensional context."""

    async_mode = "bounded_thread"
    canonical_model = "ReportedFinancials"
    capability = Capability(
        asset_class="equity",
        domain="intrinio_reported_financials",
        period="periodic",
        market="us",
        source="intrinio",
        verified=False,
        notes="Variable XBRL facts preserved; public output exposes only period metadata",
    )

    def transform_query(self, **kwargs: object) -> _ReportedQuery:
        """Validate statement type and requested fiscal frequency before I/O."""
        return _validate_query(_ReportedQuery, kwargs)

    def extract_data(self, params: _ReportedQuery, ctx: FetchContext) -> list[_FinancialPeriodRaw]:
        """Resolve reported XBRL facts through bounded standardized periods."""
        _preflight(ctx, 1 + params.limit)
        statement_code = {
            "balance": "balance_sheet_statement",
            "income": "income_statement",
            "cash": "cash_flow_statement",
        }[params.statement_type]
        api_query: _FinancialQuery = params
        if params.statement_type == "cash" and params.period == "quarter":
            # Source observations stay labeled Q2YTD/Q3YTD; no discrete quarter is derived.
            query_values = params.model_dump(mode="python", exclude={"statement_type"})
            query_values["period"] = "ytd"
            api_query = _validate_query(_FinancialQuery, query_values)
        return _reported_result(api_query, ctx, statement_code=statement_code)

    def transform_data(self, raw: list[_FinancialPeriodRaw], params: _ReportedQuery) -> FetchResult:
        """Return filing identity and typed XBRL facts without invented fields."""
        if len(raw) > params.limit:
            raise _fail("PROVIDER_LIMIT_VIOLATION")
        return tuple(_reported_row(row, params.symbol) for row in raw)


class _PagedEstimateFetcher(Fetcher[_EstimateQuery, list[BaseModel]]):
    async_mode = "bounded_thread"
    canonical_model: ClassVar[str]
    endpoint: ClassVar[str]
    response_key: ClassVar[str]
    output_model: ClassVar[type[ContractModel]]
    row_model: ClassVar[type[BaseModel]]
    page_model: ClassVar[type[_EstimatePage]]
    capability: ClassVar[Capability]

    def transform_query(self, **kwargs: object) -> _EstimateQuery:
        return _validate_query(_EstimateQuery, kwargs)

    def _params(self, query: _EstimateQuery) -> dict[str, str]:
        params = {
            "identifier": query.symbol,
            "page_size": str(query.limit),
        }
        if query.start_date is not None:
            params["start_date"] = query.start_date.isoformat()
        if query.end_date is not None:
            params["end_date"] = query.end_date.isoformat()
        if query.fiscal_year is not None:
            params["fiscal_year"] = str(query.fiscal_year)
        if query.calendar_year is not None:
            params["calendar_year"] = str(query.calendar_year)
        if query.fiscal_period is not None:
            params["fiscal_period"] = query.fiscal_period
        elif query.period == "annual":
            params["fiscal_period"] = "FY"
        if query.calendar_period is not None:
            params["calendar_period"] = query.calendar_period
        return params

    def extract_data(self, params: _EstimateQuery, ctx: FetchContext) -> list[BaseModel]:
        _preflight(ctx, 1)
        url = f"{INTRINIO_BASE_URL}{self.endpoint}"
        request_params = self._params(params)
        rows: list[BaseModel] = []
        seen_cursors: set[str] = set()
        for _page_count in range(1, MAX_ESTIMATE_PAGES + 1):
            page = _strict_validate(self.page_model, _request(url, request_params, ctx).document)
            page_rows_raw = getattr(page, self.response_key)
            next_page = page.next_page
            typed_rows = page_rows_raw
            for row in typed_rows:
                if not self._matches_frequency(row, params.period):
                    continue
                rows.append(row)
                if len(rows) >= params.limit:
                    break
            if len(rows) >= params.limit or next_page is None:
                break
            if next_page in seen_cursors:
                raise _fail("PAGING_CURSOR_REPEATED")
            if not page_rows_raw:
                raise _fail("PAGING_NO_PROGRESS")
            seen_cursors.add(next_page)
            request_params["next_page"] = next_page
        else:
            raise _fail("PAGE_LIMIT")
        return rows

    def _matches_frequency(self, row: BaseModel, period: EstimateFrequency) -> bool:
        fiscal = getattr(row, "fiscal_period", None)
        if fiscal is None:
            fiscal = getattr(row, "fiscal_quarter", None)
        is_annual = isinstance(fiscal, str) and fiscal.upper() == "FY"
        if period == "annual":
            return is_annual
        return isinstance(fiscal, str) and fiscal.upper() in {"Q1", "Q2", "Q3", "Q4"}

    def transform_data(self, raw: list[BaseModel], params: _EstimateQuery) -> FetchResult:
        rows: list[ContractModel] = []
        for item in raw:
            values = self._map_estimate(item, params)
            try:
                output = self.output_model.model_validate(values)
            except (ValidationError, TypeError, ValueError):
                raise _fail("CONTRACT_INVALID") from None
            rows.append(output)
        return tuple(rows)

    def _map_estimate(self, row: BaseModel, query: _EstimateQuery) -> dict[str, object]:
        raise NotImplementedError


class _EstimateCompany(_CompanyRef):
    ticker: StrictStr


class _EpsEstimateRow(_Document):
    messages: list[StrictStr] = Field(default_factory=list)
    company: _EstimateCompany
    date: dt.date
    fiscal_year: StrictInt
    fiscal_period: StrictStr | None = None
    fiscal_quarter: StrictStr | None = None
    calendar_year: StrictInt | None = None
    calendar_period: StrictStr | None = None
    calendar_quarter: StrictStr | None = None
    count: StrictInt | None = None
    mean: FinancialValue
    median: FinancialValue
    high: FinancialValue
    low: FinancialValue
    standard_deviation: FinancialValue
    percent_change: FinancialValue = None
    mean_7_days_ago: FinancialValue = None
    mean_30_days_ago: FinancialValue = None
    mean_60_days_ago: FinancialValue = None
    mean_90_days_ago: FinancialValue = None

    @model_validator(mode="after")
    def _period_aliases(self) -> _EpsEstimateRow:
        if self.fiscal_period is None and self.fiscal_quarter is None:
            raise ValueError("one fiscal-period field is required")
        if (
            self.fiscal_period is not None
            and self.fiscal_quarter is not None
            and self.fiscal_period != self.fiscal_quarter
        ):
            raise ValueError("fiscal_period conflicts with deprecated fiscal_quarter")
        if (
            self.calendar_period is not None
            and self.calendar_quarter is not None
            and self.calendar_period != self.calendar_quarter
        ):
            raise ValueError("calendar_period conflicts with deprecated calendar_quarter")
        return self


class _EstimatePage(_Document):
    next_page: NonEmptyCursor | None = None


class _EpsEstimatePage(_EstimatePage):
    estimates: list[_EpsEstimateRow]


class IntrinioForwardEpsEstimatesFetcher(_PagedEstimateFetcher):
    """Fetch Zacks EPS estimates and documented revision history."""

    canonical_model = "ForwardEpsEstimates"
    endpoint = "/zacks/eps_estimates"
    response_key = "estimates"
    output_model = IntrinioForwardEpsEstimate
    row_model = _EpsEstimateRow
    page_model = _EpsEstimatePage
    capability = Capability(
        asset_class="equity",
        domain="intrinio_forward_eps_estimates",
        period="periodic",
        market="us",
        source="intrinio",
        verified=False,
        notes="Official Zacks EPS estimates endpoint; no live-provider verification",
    )

    def _map_estimate(self, row: BaseModel, query: _EstimateQuery) -> dict[str, object]:
        item = cast("_EpsEstimateRow", row)
        symbol, name, _, _ = _company_for_query(item.company, query.symbol)
        return {
            "symbol": symbol,
            "name": name,
            "date": item.date,
            "fiscal_year": item.fiscal_year,
            "fiscal_period": item.fiscal_period or item.fiscal_quarter,
            "calendar_year": item.calendar_year,
            "calendar_period": item.calendar_period or item.calendar_quarter,
            "low_estimate": item.low,
            "high_estimate": item.high,
            "mean": item.mean,
            "median": item.median,
            "standard_deviation": item.standard_deviation,
            "number_of_analysts": item.count,
            "percent_change": item.percent_change,
            "revisions_change_percent": item.percent_change,
            "mean_7_days_ago": item.mean_7_days_ago,
            "mean_30_days_ago": item.mean_30_days_ago,
            "mean_60_days_ago": item.mean_60_days_ago,
            "mean_90_days_ago": item.mean_90_days_ago,
            "mean_1w": item.mean_7_days_ago,
            "mean_1m": item.mean_30_days_ago,
            "mean_2m": item.mean_60_days_ago,
            "mean_3m": item.mean_90_days_ago,
        }


class _SalesEstimateRow(_Document):
    messages: list[StrictStr] = Field(default_factory=list)
    company: _EstimateCompany
    date: dt.date
    fiscal_year: StrictInt
    fiscal_period: StrictStr | None = None
    fiscal_quarter: StrictStr | None = None
    calendar_year: StrictInt | None = None
    calendar_period: StrictStr | None = None
    calendar_quarter: StrictStr | None = None
    count: StrictInt | None = None
    mean: FinancialValue
    median: FinancialValue
    high: FinancialValue
    low: FinancialValue
    standard_deviation: FinancialValue
    analyst_revisions_percent_change_1w: FinancialValue = None
    analyst_revisions_up_1w: StrictInt | None = None
    analyst_revisions_down_1w: StrictInt | None = None
    analyst_revisions_percent_change_1m: FinancialValue = None
    analyst_revisions_up_1m: StrictInt | None = None
    analyst_revisions_down_1m: StrictInt | None = None
    analyst_revisions_percent_change_3m: FinancialValue = None
    analyst_revisions_up_3m: StrictInt | None = None
    analyst_revisions_down_3m: StrictInt | None = None

    @model_validator(mode="after")
    def _period_aliases(self) -> _SalesEstimateRow:
        if self.fiscal_period is None and self.fiscal_quarter is None:
            raise ValueError("one fiscal-period field is required")
        if (
            self.fiscal_period is not None
            and self.fiscal_quarter is not None
            and self.fiscal_period != self.fiscal_quarter
        ):
            raise ValueError("fiscal_period conflicts with deprecated fiscal_quarter")
        if (
            self.calendar_period is not None
            and self.calendar_quarter is not None
            and self.calendar_period != self.calendar_quarter
        ):
            raise ValueError("calendar_period conflicts with deprecated calendar_quarter")
        return self


class _SalesEstimatePage(_EstimatePage):
    estimates: list[_SalesEstimateRow]


class IntrinioForwardSalesEstimatesFetcher(_PagedEstimateFetcher):
    """Fetch Zacks sales estimates and documented revision history."""

    canonical_model = "ForwardSalesEstimates"
    endpoint = "/zacks/sales_estimates"
    response_key = "estimates"
    output_model = IntrinioForwardSalesEstimate
    row_model = _SalesEstimateRow
    page_model = _SalesEstimatePage
    capability = Capability(
        asset_class="equity",
        domain="intrinio_forward_sales_estimates",
        period="periodic",
        market="us",
        source="intrinio",
        verified=False,
        notes="Official Zacks sales estimates endpoint; no live-provider verification",
    )

    def _map_estimate(self, row: BaseModel, query: _EstimateQuery) -> dict[str, object]:
        item = cast("_SalesEstimateRow", row)
        symbol, name, _, _ = _company_for_query(item.company, query.symbol)
        return {
            "symbol": symbol,
            "name": name,
            "date": item.date,
            "fiscal_year": item.fiscal_year,
            "fiscal_period": item.fiscal_period or item.fiscal_quarter,
            "calendar_year": item.calendar_year,
            "calendar_period": item.calendar_period or item.calendar_quarter,
            "low_estimate": item.low,
            "high_estimate": item.high,
            "mean": item.mean,
            "median": item.median,
            "standard_deviation": item.standard_deviation,
            "number_of_analysts": item.count,
            "revisions_1w_change_percent": item.analyst_revisions_percent_change_1w,
            "revisions_1w_up": item.analyst_revisions_up_1w,
            "revisions_1w_down": item.analyst_revisions_down_1w,
            "revisions_1m_change_percent": item.analyst_revisions_percent_change_1m,
            "revisions_1m_up": item.analyst_revisions_up_1m,
            "revisions_1m_down": item.analyst_revisions_down_1m,
            "revisions_3m_change_percent": item.analyst_revisions_percent_change_3m,
            "revisions_3m_up": item.analyst_revisions_up_3m,
            "revisions_3m_down": item.analyst_revisions_down_3m,
        }


class _EbitdaEstimateRow(_Document):
    ticker: StrictStr
    company_name: StrictStr | None = None
    estimate_year: StrictInt
    estimate_month: StrictInt
    period: Literal["fy", "fq"]
    consensus_type: Literal[
        "ebitda", "ebit", "enterprise_value", "cash_flow_per_share", "pretax_income"
    ]
    updated_date: dt.date
    estimate_count: StrictInt
    high: FinancialValue
    low: FinancialValue
    mean: FinancialValue
    std_dev: FinancialValue


class _EbitdaEstimatePage(_Document):
    ebitda_consensus: list[_EbitdaEstimateRow] | None = None
    ebitda_consensuses: list[_EbitdaEstimateRow] | None = None
    next_page: NonEmptyCursor | None = None

    @model_validator(mode="after")
    def _one_documented_collection(self) -> _EbitdaEstimatePage:
        if (self.ebitda_consensus is None) == (self.ebitda_consensuses is None):
            raise ValueError("exactly one published EBITDA collection key must be present")
        return self

    def records(self) -> list[_EbitdaEstimateRow]:
        return (
            self.ebitda_consensus
            if self.ebitda_consensus is not None
            else cast("list[_EbitdaEstimateRow]", self.ebitda_consensuses)
        )


class IntrinioForwardEbitdaEstimatesFetcher(
    Fetcher[_EbitdaEstimateQuery, list[_EbitdaEstimateRow]]
):
    """Paged forward EBITDA consensus, preserving the source's year/month granularity."""

    async_mode = "bounded_thread"
    canonical_model = "ForwardEbitdaEstimates"
    capability = Capability(
        asset_class="equity",
        domain="intrinio_forward_ebitda_estimates",
        period="periodic",
        market="us",
        source="intrinio",
        verified=False,
        notes="Official Zacks sample has differing table/sample collection keys",
    )

    def transform_query(self, **kwargs: object) -> _EbitdaEstimateQuery:
        """Validate estimate type, ticker, frequency, and bounded result count."""
        return _validate_query(_EbitdaEstimateQuery, kwargs)

    def extract_data(
        self, params: _EbitdaEstimateQuery, ctx: FetchContext
    ) -> list[_EbitdaEstimateRow]:
        """Read bounded pages for the requested Zacks EBITDA consensus type."""
        _preflight(ctx, 1)
        url = f"{INTRINIO_BASE_URL}/zacks/ebitda_consensus"
        request_params = {"identifier": params.symbol, "type": params.estimate_type}
        result: list[_EbitdaEstimateRow] = []
        seen_cursors: set[str] = set()
        for _page_count in range(1, MAX_ESTIMATE_PAGES + 1):
            page = _strict_validate(
                _EbitdaEstimatePage, _request(url, request_params, ctx).document
            )
            for row in page.records():
                if row.ticker.upper() != params.symbol.upper() and _looks_like_ticker(
                    params.symbol
                ):
                    raise _fail("SYMBOL_MISMATCH")
                if row.consensus_type != params.estimate_type:
                    raise _fail("CONSENSUS_TYPE_MISMATCH")
                if (row.period == "fy") != (params.period == "annual"):
                    continue
                result.append(row)
                if len(result) >= params.limit:
                    break
            if len(result) >= params.limit or page.next_page is None:
                break
            if page.next_page in seen_cursors:
                raise _fail("PAGING_CURSOR_REPEATED")
            if not page.records():
                raise _fail("PAGING_NO_PROGRESS")
            seen_cursors.add(page.next_page)
            request_params["next_page"] = page.next_page
        else:
            raise _fail("PAGE_LIMIT")
        return result

    def transform_data(
        self,
        raw: list[_EbitdaEstimateRow],
        params: _EbitdaEstimateQuery,
    ) -> FetchResult:
        """Map reported consensus values without inferring a fiscal end date."""
        rows: list[IntrinioForwardEbitdaEstimate] = []
        for item in raw:
            symbol = item.ticker.upper()
            if _looks_like_ticker(params.symbol) and symbol != params.symbol:
                raise _fail("SYMBOL_MISMATCH")
            payload = {
                "symbol": symbol,
                "name": item.company_name,
                "last_updated": item.updated_date,
                "period_ending": None,
                "fiscal_year": item.estimate_year,
                "fiscal_period": None,
                "calendar_year": None,
                "calendar_period": None,
                "estimate_year": item.estimate_year,
                "estimate_month": item.estimate_month,
                "source_period": item.period,
                "low_estimate": item.low,
                "high_estimate": item.high,
                "mean": item.mean,
                "median": None,
                "standard_deviation": item.std_dev,
                "number_of_analysts": item.estimate_count,
                "conensus_type": item.consensus_type,
                "consensus_type": item.consensus_type,
            }
            rows.append(_strict_validate(IntrinioForwardEbitdaEstimate, payload))
        return tuple(rows)


class _ForwardPeRow(_Document):
    ticker: StrictStr
    company_name: StrictStr | None = None
    updated_date: dt.date
    forward_pe_year1: FinancialValue
    forward_pe_year2: FinancialValue
    forward_pe_year3: FinancialValue
    forward_pe_year4: FinancialValue
    forward_pe_year5: FinancialValue
    forward_peg_ratio_year1: FinancialValue
    last_ttm_eps: FinancialValue


class IntrinioForwardPeEstimatesFetcher(Fetcher[_ForwardPeQuery, _ForwardPeRow]):
    """One documented Zacks forward-PE snapshot."""

    async_mode = "bounded_thread"
    canonical_model = "ForwardPeEstimates"
    capability = Capability(
        asset_class="equity",
        domain="intrinio_forward_pe_estimates",
        period="snapshot",
        market="us",
        source="intrinio",
        verified=False,
        notes="Official Zacks forward PE by identifier contract; no live-provider verification",
    )

    def transform_query(self, **kwargs: object) -> _ForwardPeQuery:
        """Validate the required ticker before making the snapshot request."""
        return _validate_query(_ForwardPeQuery, kwargs)

    def extract_data(self, params: _ForwardPeQuery, ctx: FetchContext) -> _ForwardPeRow:
        """Fetch one bounded forward-PE snapshot for an identifier."""
        _preflight(ctx, 1)
        url = f"{INTRINIO_BASE_URL}/zacks/forward_pe/{quote(params.symbol, safe='')}"
        return _strict_validate(_ForwardPeRow, _request(url, {}, ctx).document)

    def transform_data(self, raw: _ForwardPeRow, params: _ForwardPeQuery) -> FetchResult:
        """Return typed valuation fields after verifying ticker identity."""
        if _looks_like_ticker(params.symbol) and raw.ticker.upper() != params.symbol.upper():
            raise _fail("SYMBOL_MISMATCH")
        return (
            _strict_validate(
                IntrinioForwardPeEstimate,
                {
                    "symbol": raw.ticker.upper(),
                    "name": raw.company_name,
                    "year1": raw.forward_pe_year1,
                    "year2": raw.forward_pe_year2,
                    "year3": raw.forward_pe_year3,
                    "year4": raw.forward_pe_year4,
                    "year5": raw.forward_pe_year5,
                    "peg_ratio_year1": raw.forward_peg_ratio_year1,
                    "eps_ttm": raw.last_ttm_eps,
                    "last_updated": raw.updated_date,
                },
            ),
        )


__all__ = [
    "IntrinioBalanceSheetFetcher",
    "IntrinioCashFlowStatementFetcher",
    "IntrinioFinancialRatiosFetcher",
    "IntrinioForwardEbitdaEstimatesFetcher",
    "IntrinioForwardEpsEstimatesFetcher",
    "IntrinioForwardPeEstimatesFetcher",
    "IntrinioForwardSalesEstimatesFetcher",
    "IntrinioIncomeStatementFetcher",
    "IntrinioKeyMetricsFetcher",
    "IntrinioReportedFinancialsFetcher",
]
