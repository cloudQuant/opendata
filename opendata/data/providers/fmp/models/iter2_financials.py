"""Typed, bounded FMP fetchers for financial statements and metrics."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar, cast

from pydantic import Field, ValidationError, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.fmp_financials import (
    BalanceSheet,
    BalanceSheetGrowth,
    CashFlowStatement,
    CashFlowStatementGrowth,
    FinancialPeriod,
    FinancialRatios,
    FMPFinancialPeriod,
    IncomeStatement,
    IncomeStatementGrowth,
    KeyMetrics,
    RevenueBusinessLine,
    RevenueGeographic,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.fmp.models._client import (
    FMPProviderError,
    FMPQueryLimitError,
    FMPQueryValidationError,
    contract_shape_error,
    fetch_array,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date as date_type

    from opendata.data.providers._engine.http_json import HttpGet

MAX_FINANCIAL_PERIODS = 1_000
MAX_REVENUE_SEGMENTS = 10_000
FinancialFrequency = Literal["annual", "quarter"]
ModelT = TypeVar("ModelT", bound=FMPFinancialPeriod)


class _FMPFinancialQuery(QueryParams):
    """Symbol and frequency accepted by one documented stable financial route."""

    source: Literal["fmp"] = "fmp"
    market: Literal["global"] = "global"
    symbol: str
    period: FinancialFrequency = "annual"

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
    def _reject_unsupported_source_filters(self) -> _FMPFinancialQuery:
        # These stable symbol routes document period/limit, not date-bound or
        # point-in-time selectors. Do not pretend local filtering is source-side.
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("this FMP financial route does not accept date bounds")
        return self


class FMPFinancialQuery(_FMPFinancialQuery):
    """Statement, growth, ratio, and key-metric query with bounded limit."""

    limit: int = Field(default=5, ge=1, le=MAX_FINANCIAL_PERIODS, strict=True)


class FMPRevenueSegmentQuery(_FMPFinancialQuery):
    """Segmentation query; the documented route has no limit parameter."""


def _bad_query(query_type: type[QueryParams], kwargs: dict[str, object]) -> QueryParams:
    """Convert pydantic details into a stable, sanitized provider error."""
    try:
        return query_type.model_validate(kwargs)
    except ValidationError as exc:
        raise FMPQueryValidationError.from_validation_error(exc) from None
    except (TypeError, ValueError):
        raise FMPQueryValidationError() from None


def _validated_record(model: type[ModelT], raw: Mapping[str, object]) -> ModelT:
    """Build one explicit contract row and avoid leaking source response details.

    The refusal attributes the columns the row contract itself refused, translated through the
    model's alias map, so a caller learns which documented cell the source broke.
    """
    try:
        record = model.model_validate(raw)
    except (ValidationError, TypeError, ValueError) as exc:
        raise contract_shape_error(model, exc) from None
    return record


def _validate_records(
    raw: list[dict[str, Any]],
    *,
    model: type[ModelT],
    query: FMPFinancialQuery,
) -> tuple[ModelT, ...]:
    """Validate period records, row identity, frequency, and requested limit."""
    if len(raw) > query.limit:
        raise FMPProviderError("FMP_PROVIDER_LIMIT_VIOLATION", partial=True, requests_made=1)
    rows: list[ModelT] = []
    seen: set[tuple[str, object, str]] = set()
    for item in raw:
        record = _validated_record(model, item)
        if (
            record.symbol is None
            or record.period_ending is None
            or record.fiscal_year is None
            or record.fiscal_period is None
        ):
            raise FMPProviderError("FMP_BAD_SHAPE")
        if record.symbol != query.symbol:
            raise FMPProviderError("FMP_SYMBOL_MISMATCH")
        if query.period == "annual" and record.fiscal_period != "FY":
            raise FMPProviderError("FMP_FREQUENCY_MISMATCH")
        if query.period == "quarter" and record.fiscal_period == "FY":
            raise FMPProviderError("FMP_FREQUENCY_MISMATCH")
        identity = (record.symbol, record.period_ending, record.fiscal_period)
        if identity in seen:
            raise FMPProviderError("FMP_DUPLICATE_FINANCIAL_PERIOD")
        seen.add(identity)
        rows.append(record)
    return tuple(rows)


class _FinancialFetcher(Fetcher[FMPFinancialQuery, list[dict[str, Any]]]):
    """Common validated query and extraction flow for one financial identity."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None]
    endpoint: ClassVar[str]
    record_model: ClassVar[type[FMPFinancialPeriod]]
    row_model: ClassVar[type[FMPFinancialPeriod]]
    capability: ClassVar[Capability]
    #: The offline witness seam; ``None`` keeps the governed shared HTTP client in production.
    http_transport: ClassVar[HttpGet | None] = None

    def transform_query(self, **kwargs: object) -> FMPFinancialQuery:
        """Reject unsupported filters and bad request limits before transport."""
        return cast("FMPFinancialQuery", _bad_query(FMPFinancialQuery, kwargs))

    def extract_data(
        self,
        params: FMPFinancialQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Use FMP's shared key-safe HTTP client for exactly one bounded request."""
        return fetch_array(
            self.endpoint,
            {
                "symbol": params.symbol,
                "period": params.period,
                "limit": str(params.limit),
            },
            ctx.timeout,
            self.http_transport,
        )

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: FMPFinancialQuery,
    ) -> FetchResult:
        """Return strict typed rows without guessing units or filling missing values."""
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise FMPProviderError("FMP_BAD_SHAPE")
        return cast(
            "tuple[FMPFinancialPeriod, ...]",
            _validate_records(raw, model=self.record_model, query=params),
        )


class BalanceSheetFetcher(_FinancialFetcher):
    """Annual or quarterly balance sheets."""

    canonical_model = "BalanceSheet"
    endpoint = "balance-sheet-statement"
    record_model = BalanceSheet
    row_model = BalanceSheet
    capability = Capability(
        asset_class="equity",
        domain="fmp_balance_sheet",
        period="periodic",
        market="global",
        source="fmp",
        verified=False,
    )


class BalanceSheetGrowthFetcher(_FinancialFetcher):
    """Annual or quarterly balance-sheet growth ratios."""

    canonical_model = "BalanceSheetGrowth"
    endpoint = "balance-sheet-statement-growth"
    record_model = BalanceSheetGrowth
    row_model = BalanceSheetGrowth
    capability = Capability(
        asset_class="equity",
        domain="fmp_balance_sheet_growth",
        period="periodic",
        market="global",
        source="fmp",
        verified=False,
    )


class CashFlowStatementFetcher(_FinancialFetcher):
    """Annual or quarterly cash flow statements."""

    canonical_model = "CashFlowStatement"
    endpoint = "cash-flow-statement"
    record_model = CashFlowStatement
    row_model = CashFlowStatement
    capability = Capability(
        asset_class="equity",
        domain="fmp_cash_flow_statement",
        period="periodic",
        market="global",
        source="fmp",
        verified=False,
    )


class CashFlowStatementGrowthFetcher(_FinancialFetcher):
    """Annual or quarterly cash flow growth ratios."""

    canonical_model = "CashFlowStatementGrowth"
    endpoint = "cash-flow-statement-growth"
    record_model = CashFlowStatementGrowth
    row_model = CashFlowStatementGrowth
    capability = Capability(
        asset_class="equity",
        domain="fmp_cash_flow_statement_growth",
        period="periodic",
        market="global",
        source="fmp",
        verified=False,
    )


class IncomeStatementFetcher(_FinancialFetcher):
    """Annual or quarterly income statements."""

    canonical_model = "IncomeStatement"
    endpoint = "income-statement"
    record_model = IncomeStatement
    row_model = IncomeStatement
    capability = Capability(
        asset_class="equity",
        domain="fmp_income_statement",
        period="periodic",
        market="global",
        source="fmp",
        verified=False,
    )


class IncomeStatementGrowthFetcher(_FinancialFetcher):
    """Annual or quarterly income statement growth ratios."""

    canonical_model = "IncomeStatementGrowth"
    endpoint = "income-statement-growth"
    record_model = IncomeStatementGrowth
    row_model = IncomeStatementGrowth
    capability = Capability(
        asset_class="equity",
        domain="fmp_income_statement_growth",
        period="periodic",
        market="global",
        source="fmp",
        verified=False,
    )


class FinancialRatiosFetcher(_FinancialFetcher):
    """Annual or quarterly ratios aligned to the requested statement frequency."""

    canonical_model = "FinancialRatios"
    endpoint = "ratios"
    record_model = FinancialRatios
    row_model = FinancialRatios
    capability = Capability(
        asset_class="equity",
        domain="fmp_financial_ratios",
        period="periodic",
        market="global",
        source="fmp",
        verified=False,
    )


class KeyMetricsFetcher(_FinancialFetcher):
    """Annual or quarterly company key metrics."""

    canonical_model = "KeyMetrics"
    endpoint = "key-metrics"
    record_model = KeyMetrics
    row_model = KeyMetrics
    capability = Capability(
        asset_class="equity",
        domain="fmp_key_metrics",
        period="periodic",
        market="global",
        source="fmp",
        verified=False,
    )


class _RevenueSegmentFetcher(Fetcher[FMPRevenueSegmentQuery, list[dict[str, Any]]]):
    """Common typed long-row expansion for nested FMP segmentation maps."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None]
    endpoint: ClassVar[str]
    label_field: ClassVar[Literal["business_line", "region"]]
    record_model: ClassVar[type[RevenueBusinessLine] | type[RevenueGeographic]]
    row_model: ClassVar[type[RevenueBusinessLine] | type[RevenueGeographic]]
    capability: ClassVar[Capability]
    #: The offline witness seam; ``None`` keeps the governed shared HTTP client in production.
    http_transport: ClassVar[HttpGet | None] = None

    def transform_query(self, **kwargs: object) -> FMPRevenueSegmentQuery:
        """Reject unsupported as-of/date/limit parameters before provider I/O."""
        return cast("FMPRevenueSegmentQuery", _bad_query(FMPRevenueSegmentQuery, kwargs))

    def extract_data(
        self,
        params: FMPRevenueSegmentQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Read the documented flat stable endpoint without adding unsupported limit."""
        return fetch_array(
            self.endpoint,
            {"symbol": params.symbol, "period": params.period, "structure": "flat"},
            ctx.timeout,
            self.http_transport,
        )

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: FMPRevenueSegmentQuery,
    ) -> FetchResult:
        """Flatten named segment values to one strict typed row per business line/region."""
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise FMPProviderError("FMP_BAD_SHAPE")
        if len(raw) >= MAX_FINANCIAL_PERIODS:
            raise FMPQueryLimitError(
                "FMP_INCOMPLETE_PERIOD_BUDGET",
                limit_kind="source_periods",
                maximum=MAX_FINANCIAL_PERIODS - 1,
                requests_made=1,
            )

        rows: list[RevenueBusinessLine | RevenueGeographic] = []
        seen: set[tuple[str | None, date_type | None, FinancialPeriod | None, str]] = set()
        for item in raw:
            if set(item) != {"symbol", "date", "fiscalYear", "period", "reportedCurrency", "data"}:
                raise FMPProviderError("FMP_BAD_SHAPE")
            metadata: dict[str, object] = {
                key: item[key] for key in ("symbol", "date", "fiscalYear", "period") if key in item
            }
            common = _validated_record(FMPFinancialPeriod, metadata)
            if (
                common.symbol is None
                or common.period_ending is None
                or common.fiscal_year is None
                or common.fiscal_period is None
            ):
                raise FMPProviderError("FMP_BAD_SHAPE")
            if common.symbol != params.symbol:
                raise FMPProviderError("FMP_SYMBOL_MISMATCH")
            if params.period == "annual" and common.fiscal_period != "FY":
                raise FMPProviderError("FMP_FREQUENCY_MISMATCH")
            if params.period == "quarter" and common.fiscal_period == "FY":
                raise FMPProviderError("FMP_FREQUENCY_MISMATCH")
            values = item.get("data")
            if not isinstance(values, dict) or not values:
                raise FMPProviderError("FMP_BAD_SEGMENT_DATA")
            for label, amount in values.items():
                if not isinstance(label, str) or not label.strip():
                    raise FMPProviderError("FMP_BAD_SEGMENT_LABEL")
                row_values = {
                    "symbol": common.symbol,
                    "period_ending": common.period_ending,
                    "fiscal_year": common.fiscal_year,
                    "fiscal_period": common.fiscal_period,
                    self.label_field: label,
                    "revenue": amount,
                    # FMP's flat segmentation response does not document filingDate.
                    # Keep the public optional field explicit without fabricating it.
                    "filing_date": None,
                }
                row = _validated_record(self.record_model, row_values)
                identity = (row.symbol, row.period_ending, row.fiscal_period, label)
                if identity in seen:
                    raise FMPProviderError("FMP_DUPLICATE_REVENUE_SEGMENT")
                seen.add(identity)
                rows.append(row)
                if len(rows) > MAX_REVENUE_SEGMENTS:
                    raise FMPQueryLimitError(
                        "FMP_INCOMPLETE_SEGMENT_BUDGET",
                        limit_kind="max_records",
                        maximum=MAX_REVENUE_SEGMENTS,
                        requests_made=1,
                    )
        return tuple(rows)


class RevenueBusinessLineFetcher(_RevenueSegmentFetcher):
    """Business-line revenue rows normalized from FMP product segmentation."""

    canonical_model = "RevenueBusinessLine"
    endpoint = "revenue-product-segmentation"
    label_field = "business_line"
    record_model = RevenueBusinessLine
    row_model = RevenueBusinessLine
    capability = Capability(
        asset_class="equity",
        domain="fmp_revenue_business_line",
        period="periodic",
        market="global",
        source="fmp",
        verified=False,
    )


class RevenueGeographicFetcher(_RevenueSegmentFetcher):
    """Geographic revenue rows normalized from FMP geography segmentation."""

    canonical_model = "RevenueGeographic"
    endpoint = "revenue-geographic-segmentation"
    label_field = "region"
    record_model = RevenueGeographic
    row_model = RevenueGeographic
    capability = Capability(
        asset_class="equity",
        domain="fmp_revenue_geographic",
        period="periodic",
        market="global",
        source="fmp",
        verified=False,
    )


__all__ = [
    "BalanceSheetFetcher",
    "BalanceSheetGrowthFetcher",
    "CashFlowStatementFetcher",
    "CashFlowStatementGrowthFetcher",
    "FinancialRatiosFetcher",
    "FMPFinancialQuery",
    "FMPRevenueSegmentQuery",
    "IncomeStatementFetcher",
    "IncomeStatementGrowthFetcher",
    "KeyMetricsFetcher",
    "RevenueBusinessLineFetcher",
    "RevenueGeographicFetcher",
]
