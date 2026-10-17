"""Nasdaq company-filings fetcher with a bounded offset loop."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date  # noqa: TC003 - Pydantic resolves this query annotation at runtime.
from typing import Any, ClassVar

from pydantic import Field

from opendata.data.capability import Capability
from opendata.data.models.nasdaq import NasdaqCompanyFiling
from opendata.data.protocol import FetchContext, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import ProviderEngineError
from opendata.data.providers.nasdaq.models._common import (
    MAX_COMPANY_FILING_PAGES,
    MAX_COMPANY_FILING_ROWS,
    NasdaqFetcher,
    build_query,
    optional_date,
    optional_text,
    required_date,
    validate_symbol,
)

PAGE_SIZE = 100


class CompanyFilingsQuery(QueryParams):
    """One company's filings in a bounded source-page count."""

    symbol: str
    start_date: date | None = None
    end_date: date | None = None
    limit: int = Field(default=100, ge=1, le=MAX_COMPANY_FILING_ROWS)


class CompanyFilingsFetcher(NasdaqFetcher[CompanyFilingsQuery, tuple[Mapping[str, Any], ...]]):
    """Page until Nasdaq's declared total is satisfied or fail on the safety bound."""

    canonical_model: ClassVar[str] = "CompanyFilings"
    row_model: ClassVar[type[NasdaqCompanyFiling]] = NasdaqCompanyFiling
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="nasdaq_company_filings",
        period="event",
        market="us",
        source="nasdaq",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> CompanyFilingsQuery:
        """Validate the company ticker, date window, and output row limit."""
        query = build_query(
            CompanyFilingsQuery, kwargs, {"symbol", "start_date", "end_date", "limit"}
        )
        try:
            symbol = validate_symbol(query.symbol)
        except ValueError as error:
            raise ProviderEngineError("NASDAQ_QUERY_INVALID", invalid=("symbol",)) from error
        if query.start_date and query.end_date and query.start_date > query.end_date:
            raise ProviderEngineError("NASDAQ_QUERY_INVALID", invalid=("start_date", "end_date"))
        return query.model_copy(update={"symbol": symbol})

    def extract_data(
        self, params: CompanyFilingsQuery, ctx: FetchContext
    ) -> tuple[Mapping[str, Any], ...]:
        """Follow stable offset pages until the declared total is complete or bounded out."""
        collected: list[Mapping[str, Any]] = []
        expected_total: int | None = None
        target_total: int | None = None
        offset = 0
        for _ in range(MAX_COMPANY_FILING_PAGES):
            remaining = params.limit if target_total is None else target_total - len(collected)
            page_limit = min(PAGE_SIZE, remaining)
            document = self._json(
                f"/api/company/{params.symbol}/sec-filings",
                {
                    "limit": str(page_limit),
                    "offset": str(offset),
                    "sortColumn": "filed",
                    "sortOrder": "desc",
                    "IsQuoteMedia": "true",
                },
                ctx,
            )
            data = document.get("data")
            if not isinstance(data, Mapping):
                raise ProviderEngineError("NASDAQ_SHAPE_MISSING", missing=("data",))
            page = self._rows_or_empty(document, "data", "rows")
            total_value = data.get("totalRecords")
            if isinstance(total_value, bool) or not isinstance(total_value, int) or total_value < 0:
                raise ProviderEngineError("NASDAQ_SHAPE_MISSING", missing=("totalRecords",))
            if expected_total is None:
                expected_total = total_value
                target_total = min(expected_total, params.limit, MAX_COMPANY_FILING_ROWS)
            elif total_value != expected_total:
                raise ProviderEngineError("NASDAQ_PAGINATION_INCONSISTENT")
            if target_total is None:
                raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
            if len(page) > page_limit or len(collected) + len(page) > target_total:
                raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
            collected.extend(page)
            if len(collected) == target_total:
                return tuple(collected)
            if not page:
                raise ProviderEngineError("NASDAQ_INCOMPLETE")
            offset += len(page)
        if target_total is None or len(collected) != target_total:
            raise ProviderEngineError("NASDAQ_INCOMPLETE")
        return tuple(collected)

    def transform_data(
        self, raw: tuple[Mapping[str, Any], ...], params: CompanyFilingsQuery
    ) -> FetchResult:
        """Flatten filing links, validate required identity fields, and return newest first."""
        rows: list[NasdaqCompanyFiling] = []
        for item in raw:
            view = item.get("view", {})
            if view is None:
                view = {}
            if not isinstance(view, Mapping):
                raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
            report_url = (
                optional_text(view.get("htmlLink"))
                or optional_text(view.get("docLink"))
                or optional_text(view.get("pdfLink"))
            )
            filing_date = optional_date(item.get("filed"))
            if filing_date is None or report_url is None:
                raise ProviderEngineError(
                    "NASDAQ_SHAPE_INVALID", missing=("filing_date", "report_url")
                )
            if params.start_date and filing_date < params.start_date:
                continue
            if params.end_date and filing_date > params.end_date:
                continue
            rows.append(
                self._typed(
                    NasdaqCompanyFiling,
                    {
                        "filing_date": required_date(filing_date),
                        "report_type": optional_text(item.get("formType")),
                        "report_url": report_url,
                        "period_ending": optional_date(item.get("period")),
                        "name": optional_text(item.get("companyName") or item.get("name")),
                        "reporting_owner": optional_text(item.get("reportingOwner")),
                        "pdf_url": optional_text(view.get("pdfLink")),
                        "xls_url": optional_text(view.get("xlsLink")),
                        "xbr_url": optional_text(view.get("xbrLink")),
                        "doc_link": optional_text(view.get("docLink")),
                    },
                )
            )
        rows.sort(
            key=lambda row: (row.filing_date, row.report_type or "", row.report_url), reverse=True
        )
        return tuple(rows[: params.limit])
