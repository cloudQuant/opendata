"""Typed, bounded FMP news, filings, and disclosure fetchers.

The routes and source-native fields are documented in
``docs/evidence/C100/fmp-disclosures/README.md``.  HTTP is routed through the
shared governed client; these fetchers never call the provider directly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date as date_type
from datetime import datetime
from typing import Any, ClassVar, Generic, Literal, Protocol, TypeVar

from pydantic import Field, ValidationError, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.base import ContractModel
from opendata.data.models.fmp_disclosures import (
    FmpCompanyFiling,
    FmpCompanyNews,
    FmpDiscoveryFiling,
    FmpEarningsCallTranscript,
    FmpEquityOwnership,
    FmpExecutiveCompensation,
    FmpGovernmentTrade,
    FmpHistoricalDividend,
    FmpHistoricalEmployee,
    FmpHistoricalEps,
    FmpHistoricalMarketCap,
    FmpHistoricalSplit,
    FmpInsiderTrade,
    FmpInstitutionalOwnership,
    FmpKeyExecutive,
    FmpPriceTarget,
    FmpPriceTargetConsensus,
    FmpWorldNews,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.fmp.models._client import (
    FMPProviderError,
    FMPQueryLimitError,
    fetch_array,
)

_PAGE_MAX = 100
_DEFAULT_PAGE_SIZE = 100
_DEFAULT_PAGE_BUDGET = 8
_DEFAULT_RECORD_BUDGET = 20_000
_MAX_RECORD_BUDGET = 200_000
_ModelT = TypeVar("_ModelT", bound=ContractModel)
_QueryT = TypeVar("_QueryT", bound=QueryParams)
_RowFetcher = Callable[[str, Mapping[str, str], float | None], list[dict[str, Any]]]


class _PagedParameters(Protocol):
    limit: int
    max_requests: int
    max_records: int


class _FmpQuery(QueryParams):
    """FMP-only query base; source selection is fixed before any I/O."""

    source: Literal["fmp"] = "fmp"


class _SymbolQueryBase(_FmpQuery):
    symbol: str

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("symbol must be non-empty printable text")
        return value


class _SymbolQuery(_SymbolQueryBase):
    market: Literal["global"] = "global"


class _USSymbolQuery(_SymbolQueryBase):
    market: Literal["us"] = "us"


class _DateWindowFields(_FmpQuery):
    start_date: date_type | None = None
    end_date: date_type | None = None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _parse_iso_date(cls, value: object) -> object:
        if value is None:
            return None
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

    @model_validator(mode="after")
    def _validate_date_pair(self) -> _DateWindowFields:
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("start_date and end_date must be provided together")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self


class _DateWindowQuery(_DateWindowFields):
    market: Literal["global"] = "global"


class _USDateWindowQuery(_DateWindowFields):
    market: Literal["us"] = "us"


class _PagedQuery(_FmpQuery):
    market: Literal["global"] = "global"
    limit: int = Field(default=_DEFAULT_PAGE_SIZE, ge=1, le=1_000, strict=True)
    max_requests: int = Field(default=_DEFAULT_PAGE_BUDGET, ge=1, le=_PAGE_MAX, strict=True)
    max_records: int = Field(
        default=_DEFAULT_RECORD_BUDGET,
        ge=1,
        le=_MAX_RECORD_BUDGET,
        strict=True,
    )


class _PagedSymbolQuery(_SymbolQuery):
    limit: int = Field(default=_DEFAULT_PAGE_SIZE, ge=1, le=1_000, strict=True)
    max_requests: int = Field(default=_DEFAULT_PAGE_BUDGET, ge=1, le=_PAGE_MAX, strict=True)
    max_records: int = Field(
        default=_DEFAULT_RECORD_BUDGET,
        ge=1,
        le=_MAX_RECORD_BUDGET,
        strict=True,
    )


class _WindowPagedQuery(_DateWindowQuery):
    limit: int = Field(default=_DEFAULT_PAGE_SIZE, ge=1, le=1_000, strict=True)
    max_requests: int = Field(default=_DEFAULT_PAGE_BUDGET, ge=1, le=_PAGE_MAX, strict=True)
    max_records: int = Field(
        default=_DEFAULT_RECORD_BUDGET,
        ge=1,
        le=_MAX_RECORD_BUDGET,
        strict=True,
    )


class CompanyNewsQuery(_PagedSymbolQuery):
    """One company's stock-news feed with optional paired date bounds."""

    limit: int = Field(default=_DEFAULT_PAGE_SIZE, ge=1, le=250, strict=True)
    start_date: date_type | None = None
    end_date: date_type | None = None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _parse_news_date(cls, value: object) -> object:
        return _DateWindowQuery._parse_iso_date(value)

    @model_validator(mode="after")
    def _validate_news_window(self) -> CompanyNewsQuery:
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("start_date and end_date must be provided together")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self


class WorldNewsQuery(_WindowPagedQuery):
    """General news feed with optional paired date bounds."""

    limit: int = Field(default=_DEFAULT_PAGE_SIZE, ge=1, le=250, strict=True)


class CompanyFilingsQuery(_USSymbolQuery):
    """A 90-day, paged SEC filing search for one symbol."""

    start_date: date_type
    end_date: date_type
    limit: int = Field(default=100, ge=1, le=1_000, strict=True)
    max_requests: int = Field(default=_DEFAULT_PAGE_BUDGET, ge=1, le=_PAGE_MAX, strict=True)
    max_records: int = Field(
        default=_DEFAULT_RECORD_BUDGET, ge=1, le=_MAX_RECORD_BUDGET, strict=True
    )

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _parse_dates(cls, value: object) -> object:
        return _DateWindowQuery._parse_iso_date(value)

    @model_validator(mode="after")
    def _validate_window(self) -> CompanyFilingsQuery:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if (self.end_date - self.start_date).days > 90:
            raise ValueError("SEC filing search window must not exceed 90 days")
        return self


class DiscoveryFilingsQuery(_USDateWindowQuery):
    """A 90-day market-wide latest-filings search."""

    start_date: date_type
    end_date: date_type
    limit: int = Field(default=100, ge=1, le=1_000, strict=True)
    max_requests: int = Field(default=_DEFAULT_PAGE_BUDGET, ge=1, le=_PAGE_MAX, strict=True)
    max_records: int = Field(
        default=_DEFAULT_RECORD_BUDGET, ge=1, le=_MAX_RECORD_BUDGET, strict=True
    )

    @model_validator(mode="after")
    def _validate_window(self) -> DiscoveryFilingsQuery:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if (self.end_date - self.start_date).days > 90:
            raise ValueError("SEC filing discovery window must not exceed 90 days")
        return self


class _LimitedSymbolQuery(_SymbolQuery):
    limit: int = Field(default=1_000, ge=1, le=10_000, strict=True)


class HistoricalDividendsQuery(_LimitedSymbolQuery):
    """Limit the number of historical dividend rows returned for a symbol."""

    limit: int = Field(default=1_000, ge=1, le=1_000, strict=True)


class HistoricalSplitsQuery(_LimitedSymbolQuery):
    """Limit the number of historical split rows returned for a symbol."""

    limit: int = Field(default=1_000, ge=1, le=1_000, strict=True)


class HistoricalEpsQuery(_LimitedSymbolQuery):
    """Limit the number of historical earnings rows returned for a symbol."""

    limit: int = Field(default=100, ge=1, le=1_000, strict=True)


class HistoricalEmployeesQuery(_USSymbolQuery):
    """Limit historical employee-count filings returned for a US symbol."""

    limit: int = Field(default=1_000, ge=1, le=10_000, strict=True)


class HistoricalMarketCapQuery(_SymbolQuery):
    """Bound historical market-cap rows by count and optional date range."""

    limit: int = Field(default=1_000, ge=1, le=5_000, strict=True)
    start_date: date_type | None = None
    end_date: date_type | None = None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _parse_dates(cls, value: object) -> object:
        return _DateWindowQuery._parse_iso_date(value)

    @model_validator(mode="after")
    def _validate_window(self) -> HistoricalMarketCapQuery:
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("start_date and end_date must be provided together")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self


class EarningsCallTranscriptQuery(_USSymbolQuery):
    """Select one earnings-call transcript by symbol, year, and quarter."""

    year: int = Field(ge=1990, le=2100, strict=True)
    quarter: int = Field(ge=1, le=4, strict=True)


class PriceTargetQuery(_USSymbolQuery):
    """Bound the number of paged analyst price-target rows for a symbol."""

    limit: int = Field(default=100, ge=1, le=100, strict=True)
    max_requests: int = Field(default=_DEFAULT_PAGE_BUDGET, ge=1, le=_PAGE_MAX, strict=True)
    max_records: int = Field(
        default=_DEFAULT_RECORD_BUDGET, ge=1, le=_MAX_RECORD_BUDGET, strict=True
    )


class PriceTargetConsensusQuery(_USSymbolQuery):
    """One consensus snapshot, naturally bounded to a single response row."""


class KeyExecutivesQuery(_USSymbolQuery):
    """The complete key-executive list for one company."""


class ExecutiveCompensationQuery(_USSymbolQuery):
    """All documented executive compensation filings for one company."""


class GovernmentTradesQuery(_USSymbolQuery):
    """Paged Senate and House trades for one symbol."""

    limit: int = Field(default=100, ge=1, le=250, strict=True)
    max_requests: int = Field(default=16, ge=2, le=200, strict=True)
    max_records: int = Field(
        default=_DEFAULT_RECORD_BUDGET, ge=1, le=_MAX_RECORD_BUDGET, strict=True
    )


class InsiderTradingQuery(_USSymbolQuery):
    """Paged insider search constrained to one symbol."""

    limit: int = Field(default=100, ge=1, le=1_000, strict=True)
    max_requests: int = Field(default=_DEFAULT_PAGE_BUDGET, ge=1, le=_PAGE_MAX, strict=True)
    max_records: int = Field(
        default=_DEFAULT_RECORD_BUDGET, ge=1, le=_MAX_RECORD_BUDGET, strict=True
    )
    reporting_cik: str | None = None
    company_cik: str | None = None
    transaction_type: str | None = None

    @field_validator("reporting_cik", "company_cik", "transaction_type")
    @classmethod
    def _validate_optional_filter(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("insider filter must be non-empty printable text")
        return value


class InstitutionalOwnershipQuery(_USSymbolQuery):
    """Select one institutional-ownership summary by symbol and quarter."""

    year: int = Field(ge=2000, le=2100, strict=True)
    quarter: int = Field(ge=1, le=4, strict=True)


class EquityOwnershipQuery(_USSymbolQuery):
    """Beneficial ownership filings, returned in one provider response."""

    limit: int = Field(default=2_000, ge=1, le=2_000, strict=True)


class _DisclosureFetcher(Fetcher[_QueryT, list[dict[str, Any]]], Generic[_QueryT, _ModelT]):
    """Strict shared row normalization for the independently typed identities."""

    async_mode = "bounded_thread"
    query_model: ClassVar[type[QueryParams]]
    record_model: ClassVar[type[ContractModel]]
    check_symbol: ClassVar[bool] = True

    def transform_query(self, **kwargs: object) -> _QueryT:
        """Validate query values against the concrete disclosure query model."""
        return self.query_model.model_validate(kwargs)  # type: ignore[return-value]

    def transform_data(self, raw: list[dict[str, Any]], params: _QueryT) -> FetchResult:
        symbol = getattr(params, "symbol", None)
        records: list[_ModelT] = []
        for item in raw:
            if not isinstance(item, dict):
                raise FMPProviderError("FMP_BAD_SHAPE")
            try:
                record = self.record_model.model_validate(item)
            except (ValidationError, TypeError, ValueError):
                raise FMPProviderError("FMP_BAD_SHAPE") from None
            row_symbol = getattr(record, "symbol", None)
            if (
                self.check_symbol
                and isinstance(symbol, str)
                and isinstance(row_symbol, str)
                and row_symbol.casefold() != symbol.casefold()
            ):
                raise FMPProviderError("FMP_WRONG_SYMBOL")
            records.append(record)  # type: ignore[arg-type]
        return tuple(records)


class CompanyNewsFetcher(_DisclosureFetcher[CompanyNewsQuery, FmpCompanyNews]):
    """Fetch paged, date-filtered stock news for one symbol."""

    canonical_model: ClassVar[str | None] = "CompanyNews"
    query_model = CompanyNewsQuery
    record_model = FmpCompanyNews
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_company_news",
        period="event",
        market="global",
        source="fmp",
        verified=False,
    )

    def extract_data(self, params: CompanyNewsQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Fetch all company-news pages inside the caller's request budget."""
        request = {"symbols": params.symbol}
        request.update(_window_params(params))
        return _read_pages("news/stock", request, params, ctx.timeout, max_page_size=250)


class WorldNewsFetcher(_DisclosureFetcher[WorldNewsQuery, FmpWorldNews]):
    """Fetch the paged general-news feed with optional date bounds."""

    canonical_model: ClassVar[str | None] = "WorldNews"
    query_model = WorldNewsQuery
    record_model = FmpWorldNews
    check_symbol = False
    capability: ClassVar[Capability] = Capability(
        asset_class="market",
        domain="fmp_world_news",
        period="event",
        market="global",
        source="fmp",
        verified=False,
    )

    def extract_data(self, params: WorldNewsQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Fetch all general-news pages inside the caller's date window."""
        return _read_pages(
            "news/general-latest", _window_params(params), params, ctx.timeout, max_page_size=250
        )


class CompanyFilingsFetcher(_DisclosureFetcher[CompanyFilingsQuery, FmpCompanyFiling]):
    """Fetch a company's SEC filings using FMP's documented JSON route."""

    canonical_model: ClassVar[str | None] = "CompanyFilings"
    query_model = CompanyFilingsQuery
    record_model = FmpCompanyFiling
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_company_filings",
        period="event",
        market="us",
        source="fmp",
        verified=False,
    )

    def extract_data(self, params: CompanyFilingsQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Fetch every SEC filing page in the validated 90-day window."""
        request = {
            "symbol": params.symbol,
            "from": params.start_date.isoformat(),
            "to": params.end_date.isoformat(),
        }
        return _read_pages(
            "sec-filings-search/symbol",
            request,
            params,
            ctx.timeout,
            max_page_size=1_000,
        )


class DiscoveryFilingsFetcher(_DisclosureFetcher[DiscoveryFilingsQuery, FmpDiscoveryFiling]):
    """Fetch market-wide SEC filing discovery rows for a bounded window."""

    canonical_model: ClassVar[str | None] = "DiscoveryFilings"
    query_model = DiscoveryFilingsQuery
    record_model = FmpDiscoveryFiling
    check_symbol = False
    capability: ClassVar[Capability] = Capability(
        asset_class="market",
        domain="fmp_discovery_filings",
        period="event",
        market="us",
        source="fmp",
        verified=False,
    )

    def extract_data(
        self, params: DiscoveryFilingsQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch all discovery pages in the requested filing-date range."""
        request = {"from": params.start_date.isoformat(), "to": params.end_date.isoformat()}
        return _read_pages(
            "sec-filings-financials", request, params, ctx.timeout, max_page_size=1_000
        )


class HistoricalDividendsFetcher(
    _DisclosureFetcher[HistoricalDividendsQuery, FmpHistoricalDividend]
):
    """Fetch a bounded number of historical dividend rows for a symbol."""

    canonical_model: ClassVar[str | None] = "HistoricalDividends"
    query_model = HistoricalDividendsQuery
    record_model = FmpHistoricalDividend
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_historical_dividends",
        period="event",
        market="global",
        source="fmp",
        verified=False,
    )

    def extract_data(
        self, params: HistoricalDividendsQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch the symbol's historical dividends within the requested cap."""
        return _fetch_limited("dividends", params.symbol, params.limit, ctx.timeout)


class HistoricalSplitsFetcher(_DisclosureFetcher[HistoricalSplitsQuery, FmpHistoricalSplit]):
    """Fetch a bounded number of historical split rows for a symbol."""

    canonical_model: ClassVar[str | None] = "HistoricalSplits"
    query_model = HistoricalSplitsQuery
    record_model = FmpHistoricalSplit
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_historical_splits",
        period="event",
        market="global",
        source="fmp",
        verified=False,
    )

    def extract_data(
        self, params: HistoricalSplitsQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch the symbol's historical splits within the requested cap."""
        return _fetch_limited("splits", params.symbol, params.limit, ctx.timeout)


class HistoricalEpsFetcher(_DisclosureFetcher[HistoricalEpsQuery, FmpHistoricalEps]):
    """Fetch a bounded number of historical earnings rows for a symbol."""

    canonical_model: ClassVar[str | None] = "HistoricalEps"
    query_model = HistoricalEpsQuery
    record_model = FmpHistoricalEps
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_historical_eps",
        period="event",
        market="global",
        source="fmp",
        verified=False,
    )

    def extract_data(self, params: HistoricalEpsQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Fetch the symbol's earnings history within the requested cap."""
        return _fetch_limited("earnings", params.symbol, params.limit, ctx.timeout)


class HistoricalEmployeesFetcher(
    _DisclosureFetcher[HistoricalEmployeesQuery, FmpHistoricalEmployee]
):
    """Fetch historical employee-count filings for one US symbol."""

    canonical_model: ClassVar[str | None] = "HistoricalEmployees"
    query_model = HistoricalEmployeesQuery
    record_model = FmpHistoricalEmployee
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_historical_employees",
        period="event",
        market="us",
        source="fmp",
        verified=False,
    )

    def extract_data(
        self, params: HistoricalEmployeesQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch employee-count rows within the requested record cap."""
        return _fetch_limited("historical-employee-count", params.symbol, params.limit, ctx.timeout)


class HistoricalMarketCapFetcher(
    _DisclosureFetcher[HistoricalMarketCapQuery, FmpHistoricalMarketCap]
):
    """Fetch daily historical market capitalization for one symbol."""

    canonical_model: ClassVar[str | None] = "HistoricalMarketCap"
    query_model = HistoricalMarketCapQuery
    record_model = FmpHistoricalMarketCap
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_historical_market_cap",
        period="1d",
        market="global",
        source="fmp",
        verified=False,
    )

    def extract_data(
        self, params: HistoricalMarketCapQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch the requested date range and market-cap record cap."""
        request = {"symbol": params.symbol, "limit": str(params.limit)}
        request.update(_window_params(params))
        return _fetch_limited_raw(
            "historical-market-capitalization", request, params.limit, ctx.timeout
        )


class EarningsCallTranscriptFetcher(
    _DisclosureFetcher[EarningsCallTranscriptQuery, FmpEarningsCallTranscript]
):
    """Fetch one quarterly earnings-call transcript."""

    canonical_model: ClassVar[str | None] = "EarningsCallTranscript"
    query_model = EarningsCallTranscriptQuery
    record_model = FmpEarningsCallTranscript
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_earnings_call_transcript",
        period="event",
        market="us",
        source="fmp",
        verified=False,
    )

    def extract_data(
        self, params: EarningsCallTranscriptQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch the transcript selected by symbol, year, and quarter."""
        rows = fetch_array(
            "earning-call-transcript",
            {
                "symbol": params.symbol,
                "year": str(params.year),
                "quarter": str(params.quarter),
                "limit": "1",
            },
            ctx.timeout,
        )
        if len(rows) > 1:
            raise FMPProviderError("FMP_PROVIDER_LIMIT_VIOLATION", partial=True, requests_made=1)
        return rows

    def transform_data(
        self, raw: list[dict[str, Any]], params: EarningsCallTranscriptQuery
    ) -> FetchResult:
        """Validate that returned transcript identity matches the request."""
        records = super().transform_data(raw, params)
        for record in records:
            if not isinstance(record, FmpEarningsCallTranscript):
                raise FMPProviderError("FMP_BAD_SHAPE")
            if record.year != params.year or record.period != f"Q{params.quarter}":
                raise FMPProviderError("FMP_WRONG_PERIOD")
        return records


class PriceTargetFetcher(_DisclosureFetcher[PriceTargetQuery, FmpPriceTarget]):
    """Fetch paged analyst price-target news for one symbol."""

    canonical_model: ClassVar[str | None] = "PriceTarget"
    query_model = PriceTargetQuery
    record_model = FmpPriceTarget
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_price_target",
        period="event",
        market="us",
        source="fmp",
        verified=False,
    )

    def extract_data(self, params: PriceTargetQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Fetch all price-target pages inside the request's declared budgets."""
        return _read_pages(
            "price-target-news",
            {"symbol": params.symbol},
            params,
            ctx.timeout,
            max_page_size=100,
        )


class PriceTargetConsensusFetcher(
    _DisclosureFetcher[PriceTargetConsensusQuery, FmpPriceTargetConsensus]
):
    """Fetch the single analyst price-target consensus row for a symbol."""

    canonical_model: ClassVar[str | None] = "PriceTargetConsensus"
    query_model = PriceTargetConsensusQuery
    record_model = FmpPriceTargetConsensus
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_price_target_consensus",
        period="snapshot",
        market="us",
        source="fmp",
        verified=False,
    )

    def extract_data(
        self, params: PriceTargetConsensusQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch and enforce the route's one-row snapshot shape."""
        rows = fetch_array("price-target-consensus", {"symbol": params.symbol}, ctx.timeout)
        if len(rows) > 1:
            raise FMPProviderError("FMP_BAD_SHAPE")
        return rows


class KeyExecutivesFetcher(_DisclosureFetcher[KeyExecutivesQuery, FmpKeyExecutive]):
    """Fetch and locally identify the requested company's key executives."""

    canonical_model: ClassVar[str | None] = "KeyExecutives"
    query_model = KeyExecutivesQuery
    record_model = FmpKeyExecutive
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_key_executives",
        period="snapshot",
        market="us",
        source="fmp",
        verified=False,
    )

    def extract_data(self, params: KeyExecutivesQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Fetch the documented executive rows for a symbol."""
        return fetch_array("key-executives", {"symbol": params.symbol}, ctx.timeout)

    def transform_data(self, raw: list[dict[str, Any]], params: KeyExecutivesQuery) -> FetchResult:
        """Attach the requested symbol after rejecting conflicting identities."""
        enriched: list[dict[str, Any]] = []
        for row in raw:
            if not isinstance(row, dict):
                raise FMPProviderError("FMP_BAD_SHAPE")
            record = dict(row)
            existing_symbol = record.get("symbol")
            if (
                isinstance(existing_symbol, str)
                and existing_symbol.casefold() != params.symbol.casefold()
            ):
                raise FMPProviderError("FMP_WRONG_SYMBOL")
            record["symbol"] = params.symbol
            enriched.append(record)
        return super().transform_data(enriched, params)


class ExecutiveCompensationFetcher(
    _DisclosureFetcher[ExecutiveCompensationQuery, FmpExecutiveCompensation]
):
    """Fetch executive compensation filings for one company."""

    canonical_model: ClassVar[str | None] = "ExecutiveCompensation"
    query_model = ExecutiveCompensationQuery
    record_model = FmpExecutiveCompensation
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_executive_compensation",
        period="event",
        market="us",
        source="fmp",
        verified=False,
    )

    def extract_data(
        self, params: ExecutiveCompensationQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch the compensation rows reported for a symbol."""
        return fetch_array(
            "governance-executive-compensation", {"symbol": params.symbol}, ctx.timeout
        )


class GovernmentTradesFetcher(_DisclosureFetcher[GovernmentTradesQuery, FmpGovernmentTrade]):
    """Fetch and tag paged Senate and House trades for a symbol."""

    canonical_model: ClassVar[str | None] = "GovernmentTrades"
    query_model = GovernmentTradesQuery
    record_model = FmpGovernmentTrade
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_government_trades",
        period="event",
        market="us",
        source="fmp",
        verified=False,
    )

    def extract_data(
        self, params: GovernmentTradesQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch both chamber endpoints using one shared page budget."""
        return _read_two_chamber_trades(params, ctx.timeout)

    def transform_data(
        self, raw: list[dict[str, Any]], params: GovernmentTradesQuery
    ) -> FetchResult:
        """Add the endpoint-derived chamber field before typed validation."""
        tagged: list[dict[str, Any]] = []
        for row in raw:
            if not isinstance(row, dict):
                raise FMPProviderError("FMP_BAD_SHAPE")
            record = dict(row)
            chamber = record.pop("_fetch_chamber", None)
            tagged.append({**record, "chamber": chamber})
        return super().transform_data(tagged, params)


class InsiderTradingFetcher(_DisclosureFetcher[InsiderTradingQuery, FmpInsiderTrade]):
    """Fetch paged insider trades filtered to one symbol."""

    canonical_model: ClassVar[str | None] = "InsiderTrading"
    query_model = InsiderTradingQuery
    record_model = FmpInsiderTrade
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_insider_trading",
        period="event",
        market="us",
        source="fmp",
        verified=False,
    )

    def extract_data(self, params: InsiderTradingQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Fetch all insider-trading pages with validated optional filters."""
        request: dict[str, str] = {"symbol": params.symbol}
        for name, key in (
            ("reporting_cik", "reportingCik"),
            ("company_cik", "companyCik"),
            ("transaction_type", "transactionType"),
        ):
            value = getattr(params, name)
            if value is not None:
                request[key] = value
        return _read_pages(
            "insider-trading/search", request, params, ctx.timeout, max_page_size=1_000
        )


class InstitutionalOwnershipFetcher(
    _DisclosureFetcher[InstitutionalOwnershipQuery, FmpInstitutionalOwnership]
):
    """Fetch one institutional-ownership summary for a symbol and quarter."""

    canonical_model: ClassVar[str | None] = "InstitutionalOwnership"
    query_model = InstitutionalOwnershipQuery
    record_model = FmpInstitutionalOwnership
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_institutional_ownership",
        period="quarter",
        market="us",
        source="fmp",
        verified=False,
    )

    def extract_data(
        self, params: InstitutionalOwnershipQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch the route's quarterly symbol-position summary."""
        rows = fetch_array(
            "institutional-ownership/symbol-positions-summary",
            {"symbol": params.symbol, "year": str(params.year), "quarter": str(params.quarter)},
            ctx.timeout,
        )
        if len(rows) > 1:
            raise FMPProviderError("FMP_BAD_SHAPE")
        return rows


class EquityOwnershipFetcher(_DisclosureFetcher[EquityOwnershipQuery, FmpEquityOwnership]):
    """Fetch beneficial-ownership filings for one symbol."""

    canonical_model: ClassVar[str | None] = "EquityOwnership"
    query_model = EquityOwnershipQuery
    record_model = FmpEquityOwnership
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_equity_ownership",
        period="event",
        market="us",
        source="fmp",
        verified=False,
    )

    def extract_data(self, params: EquityOwnershipQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Fetch beneficial-ownership rows within the requested record cap."""
        return _fetch_limited_raw(
            "acquisition-of-beneficial-ownership",
            {"symbol": params.symbol, "limit": str(params.limit)},
            params.limit,
            ctx.timeout,
        )


def _window_params(params: object) -> dict[str, str]:
    start = getattr(params, "start_date", None)
    end = getattr(params, "end_date", None)
    result: dict[str, str] = {}
    if start is not None and end is not None:
        result["from"] = start.isoformat()
        result["to"] = end.isoformat()
    return result


def _read_pages(
    endpoint: str,
    base_params: dict[str, str],
    params: _PagedParameters,
    timeout: float | None,
    *,
    max_page_size: int,
    fetcher: _RowFetcher | None = None,
) -> list[dict[str, Any]]:
    """Read numbered pages and fail closed when the request budget hides a tail."""
    if params.limit > max_page_size:
        raise FMPProviderError("FMP_QUERY_LIMIT_EXCEEDED")
    fetch_page = fetch_array if fetcher is None else fetcher
    rows: list[dict[str, Any]] = []
    for page in range(params.max_requests):
        request = {**base_params, "page": str(page), "limit": str(params.limit)}
        page_rows = fetch_page(endpoint, request, timeout)
        if not isinstance(page_rows, list) or any(not isinstance(row, dict) for row in page_rows):
            raise FMPProviderError("FMP_BAD_SHAPE")
        if len(page_rows) > params.limit:
            raise FMPProviderError(
                "FMP_PROVIDER_LIMIT_VIOLATION", partial=True, requests_made=page + 1
            )
        if len(rows) + len(page_rows) > params.max_records:
            raise FMPQueryLimitError(
                "FMP_INCOMPLETE_RECORD_BUDGET",
                limit_kind="max_records",
                maximum=params.max_records,
                requests_made=page + 1,
            )
        rows.extend(page_rows)
        if len(page_rows) < params.limit:
            return rows
    raise FMPQueryLimitError(
        "FMP_INCOMPLETE_PAGE_BUDGET",
        limit_kind="max_requests",
        maximum=params.max_requests,
        requests_made=params.max_requests,
    )


def _fetch_limited(
    endpoint: str, symbol: str, limit: int, timeout: float | None
) -> list[dict[str, Any]]:
    return _fetch_limited_raw(endpoint, {"symbol": symbol, "limit": str(limit)}, limit, timeout)


def _fetch_limited_raw(
    endpoint: str,
    request: dict[str, str],
    limit: int,
    timeout: float | None,
) -> list[dict[str, Any]]:
    rows = fetch_array(endpoint, request, timeout)
    if len(rows) > limit:
        raise FMPProviderError("FMP_PROVIDER_LIMIT_VIOLATION", partial=True, requests_made=1)
    if len(rows) == limit:
        raise FMPQueryLimitError(
            "FMP_INCOMPLETE_PROVIDER_LIMIT",
            limit_kind="provider_max_records",
            maximum=limit,
            requests_made=1,
        )
    return rows


def _read_two_chamber_trades(
    params: GovernmentTradesQuery, timeout: float | None
) -> list[dict[str, Any]]:
    """Page both documented chamber endpoints without dropping a full-page tail."""
    rows: list[dict[str, Any]] = []
    active = {"house": "house-trades", "senate": "senate-trades"}
    page = 0
    requests_made = 0
    while active:
        for chamber, endpoint in tuple(active.items()):
            if requests_made >= params.max_requests:
                raise FMPQueryLimitError(
                    "FMP_INCOMPLETE_PAGE_BUDGET",
                    limit_kind="max_requests",
                    maximum=params.max_requests,
                    requests_made=requests_made,
                )
            page_rows = fetch_array(
                endpoint,
                {"symbol": params.symbol, "page": str(page), "limit": str(params.limit)},
                timeout,
            )
            requests_made += 1
            if len(page_rows) > params.limit:
                raise FMPProviderError(
                    "FMP_PROVIDER_LIMIT_VIOLATION", partial=True, requests_made=requests_made
                )
            if len(rows) + len(page_rows) > params.max_records:
                raise FMPQueryLimitError(
                    "FMP_INCOMPLETE_RECORD_BUDGET",
                    limit_kind="max_records",
                    maximum=params.max_records,
                    requests_made=requests_made,
                )
            rows.extend({**record, "_fetch_chamber": chamber} for record in page_rows)
            if len(page_rows) < params.limit:
                del active[chamber]
        if active:
            page += 1
            if page >= _PAGE_MAX:
                raise FMPQueryLimitError(
                    "FMP_INCOMPLETE_PAGE_BUDGET",
                    limit_kind="provider_max_pages",
                    maximum=_PAGE_MAX,
                    requests_made=requests_made,
                )
    return rows


__all__ = [
    "CompanyFilingsFetcher",
    "CompanyFilingsQuery",
    "CompanyNewsFetcher",
    "CompanyNewsQuery",
    "DiscoveryFilingsFetcher",
    "DiscoveryFilingsQuery",
    "EarningsCallTranscriptFetcher",
    "EarningsCallTranscriptQuery",
    "EquityOwnershipFetcher",
    "EquityOwnershipQuery",
    "ExecutiveCompensationFetcher",
    "ExecutiveCompensationQuery",
    "GovernmentTradesFetcher",
    "GovernmentTradesQuery",
    "HistoricalDividendsFetcher",
    "HistoricalDividendsQuery",
    "HistoricalEmployeesFetcher",
    "HistoricalEmployeesQuery",
    "HistoricalEpsFetcher",
    "HistoricalEpsQuery",
    "HistoricalMarketCapFetcher",
    "HistoricalMarketCapQuery",
    "HistoricalSplitsFetcher",
    "HistoricalSplitsQuery",
    "InsiderTradingFetcher",
    "InsiderTradingQuery",
    "InstitutionalOwnershipFetcher",
    "InstitutionalOwnershipQuery",
    "KeyExecutivesFetcher",
    "KeyExecutivesQuery",
    "PriceTargetConsensusFetcher",
    "PriceTargetConsensusQuery",
    "PriceTargetFetcher",
    "PriceTargetQuery",
    "WorldNewsFetcher",
    "WorldNewsQuery",
]
