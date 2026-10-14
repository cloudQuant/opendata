"""Typed bounded FMP estimates, ESG, N-PORT, and yield-curve fetchers."""

from __future__ import annotations

from datetime import date as date_type
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar

from pydantic import Field, ValidationError, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.fmp_iter2 import (
    AnalystEstimate,
    FmpEsgScore,
    FmpYieldCurveObservation,
    ForwardEbitdaEstimate,
    ForwardEpsEstimate,
    NportDisclosureHolding,
    TreasuryRateObservation,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.fmp.models._client import (
    FMPProviderError,
    FMPQueryLimitError,
    FMPQueryValidationError,
    fetch_array,
)
from opendata.data.providers.fmp.models.iter2 import TreasuryRatesQuery

if TYPE_CHECKING:
    from datetime import date as date_type

    from opendata.data.models.base import ContractModel

ModelT = TypeVar("ModelT", bound="ContractModel")
QueryT = TypeVar("QueryT", bound=QueryParams)
TreasuryMaturity = Literal[
    "1M", "2M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"
]

MAX_ANALYST_ESTIMATE_ROWS = 1_000
MAX_LOCAL_ROWS = 100_000
FMP_TREASURY_MATURITIES: tuple[tuple[str, TreasuryMaturity], ...] = (
    ("month1", "1M"),
    ("month2", "2M"),
    ("month3", "3M"),
    ("month6", "6M"),
    ("year1", "1Y"),
    ("year2", "2Y"),
    ("year3", "3Y"),
    ("year5", "5Y"),
    ("year7", "7Y"),
    ("year10", "10Y"),
    ("year20", "20Y"),
    ("year30", "30Y"),
)


class AnalystEstimatesQuery(QueryParams):
    """One documented FMP analyst-estimates page for one symbol and frequency."""

    source: Literal["fmp"] = "fmp"
    market: Literal["global"] = "global"
    symbol: str
    period: Literal["annual", "quarter"]
    page: int = Field(default=0, ge=0, le=100_000, strict=True)
    limit: int = Field(default=10, ge=1, le=MAX_ANALYST_ESTIMATE_ROWS, strict=True)

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
    def _reject_date_bounds(self) -> AnalystEstimatesQuery:
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("FMP analyst estimates do not accept date bounds")
        return self


class _AnalystEstimatesFetcher(Fetcher[AnalystEstimatesQuery, list[dict[str, Any]]]):
    """Shared route and request semantics; each subclass retains its own output identity."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None]
    capability: ClassVar[Capability]

    def transform_query(self, **kwargs: object) -> AnalystEstimatesQuery:
        return _validated_query(AnalystEstimatesQuery, kwargs)

    def extract_data(
        self,
        params: AnalystEstimatesQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        return fetch_array(
            "analyst-estimates",
            {
                "symbol": params.symbol,
                "period": params.period,
                "page": str(params.page),
                "limit": str(params.limit),
            },
            ctx.timeout,
        )


class AnalystEstimatesFetcher(_AnalystEstimatesFetcher):
    """Fetch the documented full analyst-estimates row shape."""

    canonical_model = "AnalystEstimates"
    capability = Capability(
        asset_class="equity",
        domain="fmp_analyst_estimates",
        period="periodic",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: AnalystEstimatesQuery,
    ) -> FetchResult:
        """Return complete source estimate rows in their dedicated typed contract."""
        records = _validated_estimate_rows(raw, params)
        return tuple(records)


class ForwardEbitdaEstimatesFetcher(_AnalystEstimatesFetcher):
    """Project forward EBITDA fields from the official analyst-estimates response."""

    canonical_model = "ForwardEbitdaEstimates"
    capability = Capability(
        asset_class="equity",
        domain="fmp_forward_ebitda_estimates",
        period="periodic",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: AnalystEstimatesQuery,
    ) -> FetchResult:
        """Project EBITDA consensus values while preserving symbol and forecast date."""
        estimates = _validated_estimate_rows(raw, params)
        return tuple(
            ForwardEbitdaEstimate(
                symbol=row.symbol,
                date=row.date,
                ebitdaLow=row.ebitda_low,
                ebitdaHigh=row.ebitda_high,
                ebitdaAvg=row.ebitda_average,
            )
            for row in estimates
        )


class ForwardEpsEstimatesFetcher(_AnalystEstimatesFetcher):
    """Project forward EPS fields from the official analyst-estimates response."""

    canonical_model = "ForwardEpsEstimates"
    capability = Capability(
        asset_class="equity",
        domain="fmp_forward_eps_estimates",
        period="periodic",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: AnalystEstimatesQuery,
    ) -> FetchResult:
        """Project EPS consensus values while preserving the analyst count."""
        estimates = _validated_estimate_rows(raw, params)
        return tuple(
            ForwardEpsEstimate(
                symbol=row.symbol,
                date=row.date,
                epsAvg=row.eps_average,
                epsHigh=row.eps_high,
                epsLow=row.eps_low,
                numAnalystsEps=row.num_analysts_eps,
            )
            for row in estimates
        )


class EsgScoreQuery(QueryParams):
    """One FMP ESG disclosure history query for a U.S. symbol."""

    source: Literal["fmp"] = "fmp"
    market: Literal["us"] = "us"
    symbol: str
    max_records: int = Field(default=10_000, ge=1, le=MAX_LOCAL_ROWS, strict=True)

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
    def _reject_unsupported_filters(self) -> EsgScoreQuery:
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("FMP ESG disclosures do not accept date bounds")
        return self


class EsgScoreFetcher(Fetcher[EsgScoreQuery, list[dict[str, Any]]]):
    """Fetch ESG disclosure scores with source-published score and filing fields."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "EsgScore"
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="fmp_esg_score",
        period="event",
        market="us",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EsgScoreQuery:
        """Validate the symbol and reject unsupported dates before network I/O."""
        return _validated_query(EsgScoreQuery, kwargs)

    def extract_data(self, params: EsgScoreQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Read the documented ESG disclosures route for one symbol."""
        return fetch_array("esg-disclosures", {"symbol": params.symbol}, ctx.timeout)

    def transform_data(self, raw: list[dict[str, Any]], params: EsgScoreQuery) -> FetchResult:
        """Validate score components, symbol identity, duplicates, and the local row bound."""
        _check_record_budget(raw, params.max_records)
        records: list[FmpEsgScore] = []
        seen: set[tuple[date_type, date_type, str]] = set()
        for item in raw:
            record = _validate_record(FmpEsgScore, item)
            if record.symbol != params.symbol:
                raise FMPProviderError("FMP_SYMBOL_MISMATCH")
            identity = (record.date, record.accepted_date, record.form_type)
            if identity in seen:
                raise FMPProviderError("FMP_DUPLICATE_ESG_DISCLOSURE")
            seen.add(identity)
            records.append(record)
        return tuple(records)


class NportDisclosureQuery(QueryParams):
    """One fund and filing period for the documented N-PORT holdings route."""

    source: Literal["fmp"] = "fmp"
    market: Literal["us"] = "us"
    symbol: str
    year: int = Field(ge=1990, le=2100, strict=True)
    quarter: int = Field(ge=1, le=4, strict=True)
    cik: str | None = None
    max_records: int = Field(default=20_000, ge=1, le=MAX_LOCAL_ROWS, strict=True)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not symbol or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 for char in symbol
        ):
            raise ValueError("symbol must be a nonblank token without whitespace")
        return symbol

    @field_validator("cik")
    @classmethod
    def _validate_cik(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) != 10 or not value.isascii() or not value.isdigit():
            raise ValueError("cik must be a ten-digit identifier")
        return value

    @model_validator(mode="after")
    def _reject_date_bounds(self) -> NportDisclosureQuery:
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("FMP N-PORT disclosures do not accept date bounds")
        return self


class NportDisclosureFetcher(Fetcher[NportDisclosureQuery, list[dict[str, Any]]]):
    """Fetch a mutual-fund disclosure and bind the requested fund identity to rows."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "NportDisclosure"
    capability: ClassVar[Capability] = Capability(
        asset_class="fund",
        domain="fmp_nport_disclosure",
        period="event",
        market="us",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> NportDisclosureQuery:
        """Validate the requested fund and filing period before network I/O."""
        return _validated_query(NportDisclosureQuery, kwargs)

    def extract_data(
        self,
        params: NportDisclosureQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Read one documented mutual-fund disclosure period and optional CIK."""
        request = {
            "symbol": params.symbol,
            "year": str(params.year),
            "quarter": str(params.quarter),
        }
        if params.cik is not None:
            request["cik"] = params.cik
        return fetch_array("funds/disclosure", request, ctx.timeout)

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: NportDisclosureQuery,
    ) -> FetchResult:
        """Validate holdings and attach the requested fund identity to each row."""
        _check_record_budget(raw, params.max_records)
        records: list[NportDisclosureHolding] = []
        seen: set[tuple[str, date_type, str, str, str | None]] = set()
        for item in raw:
            try:
                payload = {**item, "fund_symbol": params.symbol}
                record = NportDisclosureHolding.model_validate(payload)
            except (ValidationError, TypeError, ValueError):
                raise FMPProviderError("FMP_BAD_SHAPE") from None
            if params.cik is not None and record.cik != params.cik:
                raise FMPProviderError("FMP_CIK_FILTER_MISMATCH")
            identity = (record.cik, record.date, record.accepted_date, record.symbol, record.cusip)
            if identity in seen:
                raise FMPProviderError("FMP_DUPLICATE_NPORT_HOLDING")
            seen.add(identity)
            records.append(record)
        return tuple(records)


class YieldCurveQuery(TreasuryRatesQuery):
    """Date-bounded U.S. curve query backed by the documented Treasury route."""


class YieldCurveFetcher(Fetcher[YieldCurveQuery, list[dict[str, Any]]]):
    """Expand the FMP wide maturity columns into long-form YieldCurve rows."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "YieldCurve"
    capability: ClassVar[Capability] = Capability(
        asset_class="rates",
        domain="fmp_yield_curve",
        period="daily",
        market="us",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> YieldCurveQuery:
        """Validate the bounded U.S. Treasury date range before network I/O."""
        return _validated_query(YieldCurveQuery, kwargs)

    def extract_data(self, params: YieldCurveQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Read the official wide Treasury rates response for the requested range."""
        return fetch_array(
            "treasury-rates",
            {"from": params.start_date.isoformat(), "to": params.end_date.isoformat()},
            ctx.timeout,
        )

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: YieldCurveQuery,
    ) -> FetchResult:
        """Expand each documented maturity into a long-form daily curve row."""
        _check_record_budget(raw, params.max_records)
        output_rows = len(raw) * len(FMP_TREASURY_MATURITIES)
        if output_rows > params.max_records:
            raise FMPQueryLimitError(
                "FMP_INCOMPLETE_RECORD_BUDGET",
                limit_kind="max_records",
                maximum=params.max_records,
                requests_made=1,
            )
        observations: list[FmpYieldCurveObservation] = []
        seen_dates: set[date_type] = set()
        for item in raw:
            rate_row = _validate_record(TreasuryRateObservation, item)
            if not params.start_date <= rate_row.date <= params.end_date:
                raise FMPProviderError("FMP_OUT_OF_WINDOW_ROW")
            if rate_row.date in seen_dates:
                raise FMPProviderError("FMP_DUPLICATE_TREASURY_DATE")
            seen_dates.add(rate_row.date)
            for field, maturity in FMP_TREASURY_MATURITIES:
                observations.append(
                    FmpYieldCurveObservation(
                        date=rate_row.date,
                        maturity=maturity,
                        rate=getattr(rate_row, field),
                    )
                )
        return tuple(observations)


def _validated_query(
    query_type: type[QueryT],
    kwargs: dict[str, object],
) -> QueryT:
    try:
        return query_type.model_validate(kwargs)
    except (ValidationError, TypeError, ValueError):
        raise FMPQueryValidationError() from None


def _validated_estimate_rows(
    raw: list[dict[str, Any]],
    query: AnalystEstimatesQuery,
) -> list[AnalystEstimate]:
    if not isinstance(raw, list) or len(raw) > query.limit:
        raise FMPProviderError("FMP_PROVIDER_LIMIT_VIOLATION", partial=True, requests_made=1)
    records: list[AnalystEstimate] = []
    seen_dates: set[date_type] = set()
    for item in raw:
        record = _validate_record(AnalystEstimate, item)
        if record.symbol != query.symbol:
            raise FMPProviderError("FMP_SYMBOL_MISMATCH")
        if record.date in seen_dates:
            raise FMPProviderError("FMP_DUPLICATE_ESTIMATE_DATE")
        seen_dates.add(record.date)
        records.append(record)
    return records


def _validate_record(model: type[ModelT], item: dict[str, Any]) -> ModelT:
    try:
        return model.model_validate(item)
    except (ValidationError, TypeError, ValueError):
        raise FMPProviderError("FMP_BAD_SHAPE") from None


def _check_record_budget(rows: list[dict[str, Any]], maximum: int) -> None:
    if len(rows) > maximum:
        raise FMPQueryLimitError(
            "FMP_INCOMPLETE_RECORD_BUDGET",
            limit_kind="max_records",
            maximum=maximum,
            requests_made=1,
        )


__all__ = [
    "AnalystEstimatesFetcher",
    "AnalystEstimatesQuery",
    "EsgScoreFetcher",
    "EsgScoreQuery",
    "ForwardEbitdaEstimatesFetcher",
    "ForwardEpsEstimatesFetcher",
    "NportDisclosureFetcher",
    "NportDisclosureQuery",
    "YieldCurveFetcher",
    "YieldCurveQuery",
]
