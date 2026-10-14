"""Current CFTC contract-market directory search."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import ValidationInfo, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.cftc import COTSearch
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.cftc.models import _client

if TYPE_CHECKING:
    from opendata.data.providers._engine.http_json import HttpGet

from opendata.data.providers.cftc.models.cot import (
    MAX_ROWS,
    PAGE_SIZE,
    REPORT_DATASETS,
    _parse_source_date,
)

_SEARCH_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,119}\Z")
_PARTIAL_CODE = re.compile(r"(?:CFTC_)?[A-Z0-9]{1,6}\Z")


class COTSearchQuery(QueryParams):
    """Filters for the latest published market-directory rows in a COT report."""

    source: Literal["cftc"] = "cftc"
    market: Literal["us"] = "us"
    code: str | None = None
    query: str | None = None
    report_type: Literal["legacy", "disaggregated", "financial", "supplemental"] = "legacy"
    futures_only: bool = False
    category: str | None = None
    subcategory: str | None = None

    @field_validator("query", "category", "subcategory", mode="before")
    @classmethod
    def _normalise_search_term(cls, value: object, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise ValueError(f"{info.field_name} must be a string")
        normalized = " ".join(value.strip().split())
        if not normalized or not _SEARCH_TEXT.fullmatch(normalized):
            raise ValueError(f"{info.field_name} contains unsupported characters")
        if info.field_name in ("category", "subcategory"):
            normalized = normalized.replace("_", " ").upper()
        return normalized

    @field_validator("code", mode="before")
    @classmethod
    def _normalise_code(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise ValueError("code must be a string")
        normalized = value.strip().upper()
        if not _PARTIAL_CODE.fullmatch(normalized):
            raise ValueError("code must be a CFTC code or a non-empty code prefix")
        return normalized.removeprefix("CFTC_")

    @model_validator(mode="after")
    def _validate_filters(self) -> COTSearchQuery:
        """Reject common QueryParams filters unsupported by this directory model."""
        if self.symbol is not None or self.start_date is not None or self.end_date is not None:
            raise ValueError("COTSearch uses code/query/category filters, not symbol or dates")
        if self.report_type == "supplemental" and self.futures_only:
            raise ValueError("the CFTC Supplemental report is combined only")
        return self


class COTSearchFetcher(Fetcher[COTSearchQuery, list[dict[str, Any]]]):
    """Find market codes and names active in the most recent CFTC report week."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "COTSearch"
    row_model: ClassVar[type[COTSearch]] = COTSearch
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="derivatives",
        domain="cftc_market_directory",
        period="snapshot",
        market="us",
        source="cftc",
        verified=False,
        notes="CFTC PRE current-report directory mapped; independent source verification pending.",
    )

    def transform_query(self, **kwargs: object) -> COTSearchQuery:
        """Validate supported directory filters before provider I/O."""
        return COTSearchQuery.model_validate(kwargs)

    def extract_data(self, params: COTSearchQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Read the most recent report date only, with complete bounded paging."""
        dataset_id = REPORT_DATASETS[(params.report_type, params.futures_only)]
        url = f"https://publicreporting.cftc.gov/resource/{dataset_id}.json"
        clauses = _where_clauses(params)
        initial_query = {
            "$order": "report_date_as_yyyy_mm_dd DESC, cftc_contract_market_code ASC",
            "$limit": str(PAGE_SIZE),
            "$offset": "0",
        }
        if clauses:
            initial_query["$where"] = " AND ".join(clauses)
        first = _client.array_rows(
            _client.get_json(
                url,
                params=initial_query,
                context=ctx,
                transport=self.http_transport,
            )
        )
        if not first:
            return []
        latest_date = _parse_source_date(first[0].get("report_date_as_yyyy_mm_dd"))
        clauses.append(f"report_date_as_yyyy_mm_dd = '{latest_date.isoformat()}T00:00:00.000'")
        where = " AND ".join(clauses)
        rows: list[dict[str, Any]] = []
        offset = 0
        while offset < MAX_ROWS + 1:
            page_length = min(PAGE_SIZE, MAX_ROWS + 1 - offset)
            page = _client.array_rows(
                _client.get_json(
                    url,
                    params={
                        "$where": where,
                        "$order": "cftc_contract_market_code ASC",
                        "$limit": str(page_length),
                        "$offset": str(offset),
                    },
                    context=ctx,
                    transport=self.http_transport,
                )
            )
            if len(page) > page_length:
                raise _client.CFTCProviderError("CFTC_BAD_PAGE")
            rows.extend(page)
            offset += len(page)
            if len(rows) > MAX_ROWS:
                raise _client.CFTCProviderError("CFTC_RESULT_LIMIT_EXCEEDED")
            if len(page) < PAGE_SIZE:
                return rows
        raise _client.CFTCProviderError("CFTC_RESULT_LIMIT_EXCEEDED")

    def transform_data(self, raw: list[dict[str, Any]], params: COTSearchQuery) -> FetchResult:
        """Project source identity and category fields into the exact search row model."""
        output: list[COTSearch] = []
        seen_codes: set[str] = set()
        for row in raw:
            code = _string_value(row.get("cftc_contract_market_code"))
            name = _string_value(row.get("commodity_name"))
            if code is None or name is None:
                raise _client.CFTCProviderError("CFTC_BAD_DIRECTORY_ROW")
            if code in seen_codes:
                continue
            seen_codes.add(code)
            normalized = {
                "code": code,
                "name": name,
                "category": _string_value(row.get("commodity_group_name")),
                "subcategory": _string_value(row.get("commodity_subgroup_name")),
                "units": _string_value(row.get("contract_units")),
                # The PRE does not publish an independent ticker/symbol identifier.
                "symbol": None,
                "commodity": name,
            }
            try:
                output.append(COTSearch.model_validate(normalized))
            except (TypeError, ValueError):
                raise _client.CFTCProviderError("CFTC_BAD_DIRECTORY_ROW") from None
        return tuple(output)


def _where_clauses(params: COTSearchQuery) -> list[str]:
    clauses: list[str] = []
    if params.code:
        clauses.append(f"upper(cftc_contract_market_code) like '%{params.code.upper()}%'")
    if params.category:
        clauses.append(f"upper(commodity_group_name) = '{params.category}'")
    if params.subcategory:
        clauses.append(f"upper(commodity_subgroup_name) = '{params.subcategory}'")
    if params.query:
        term = params.query.upper()
        clauses.append(
            "(upper(commodity_name) like "
            f"'%{term}%' OR upper(market_and_exchange_names) like '%{term}%')"
        )
    return clauses


def _string_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return None if not text or text.casefold() == "null" else text
    raise _client.CFTCProviderError("CFTC_BAD_TEXT")
