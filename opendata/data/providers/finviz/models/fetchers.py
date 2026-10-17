"""Strict query and HTTP fetcher implementations for Finviz's seven public page models."""

from __future__ import annotations

import re
from datetime import date as date_type
from datetime import datetime
from typing import ClassVar, Generic, Literal, TypeVar, cast

from pydantic import Field, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.base import ContractModel
from opendata.data.models.finviz import (
    FinvizEquityInfo,
    FinvizEquityScreener,
    FinvizEtfPricePerformance,
    FinvizGroupComparison,
    FinvizKeyMetrics,
    FinvizPricePerformance,
    FinvizPriceTarget,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.finviz import _source
from opendata.data.providers.finviz._html import (
    parse_equity_info,
    parse_equity_screener,
    parse_etf_price_performance,
    parse_group_comparison,
    parse_key_metrics,
    parse_price_performance,
    parse_price_targets,
)

_TICKER = re.compile(r"[A-Za-z][A-Za-z0-9.\-]{0,9}\Z")
_MAX_PAGE = 10_000
_PAGE_SIZE = 20


class FinvizPageQuery(QueryParams):
    """One numbered public screener page; no generic filters are silently ignored."""

    source: Literal["finviz"] = "finviz"
    market: Literal["us"] = "us"
    page: int = Field(default=1, strict=True, ge=1, le=_MAX_PAGE)

    @model_validator(mode="after")
    def _reject_unimplemented_filters(self) -> FinvizPageQuery:
        if self.symbol is not None or self.start_date is not None or self.end_date is not None:
            raise ValueError("this Finviz page model accepts only the page parameter")
        return self


class FinvizEquityScreenerQuery(QueryParams):
    """Only the anonymous, unfiltered first page of the public stock screener."""

    source: Literal["finviz"] = "finviz"
    market: Literal["us"] = "us"
    page: Literal[1] = 1

    @field_validator("page", mode="before")
    @classmethod
    def _require_exact_first_page(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("EquityScreener accepts only integer page=1")
        return value

    @model_validator(mode="after")
    def _reject_unimplemented_filters(self) -> FinvizEquityScreenerQuery:
        if self.symbol is not None or self.start_date is not None or self.end_date is not None:
            raise ValueError("EquityScreener accepts only the unfiltered first page")
        return self


class FinvizGroupQuery(QueryParams):
    """One documented public sector comparison view."""

    source: Literal["finviz"] = "finviz"
    market: Literal["us"] = "us"
    group: Literal["sector"] = "sector"
    metric: Literal["overview", "performance"] = "performance"

    @model_validator(mode="after")
    def _reject_unimplemented_filters(self) -> FinvizGroupQuery:
        if self.symbol is not None or self.start_date is not None or self.end_date is not None:
            raise ValueError("group comparison accepts only group and metric")
        return self


class FinvizStockPageQuery(QueryParams):
    """One public Finviz stock page selected by a single ticker."""

    source: Literal["finviz"] = "finviz"
    market: Literal["us"] = "us"
    symbol: str

    @field_validator("symbol")
    @classmethod
    def _validate_ticker(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not _TICKER.fullmatch(symbol):
            raise ValueError("symbol must be a safe Finviz ticker")
        return symbol

    @model_validator(mode="after")
    def _reject_date_filters(self) -> FinvizStockPageQuery:
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("this Finviz stock-page model does not accept date bounds")
        return self


class FinvizPriceTargetQuery(QueryParams):
    """Analyst-rating request with inclusive filters on the visible event dates."""

    source: Literal["finviz"] = "finviz"
    market: Literal["us"] = "us"
    symbol: str

    @field_validator("symbol")
    @classmethod
    def _validate_ticker(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not _TICKER.fullmatch(symbol):
            raise ValueError("symbol must be a safe Finviz ticker")
        return symbol

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
            raise ValueError("date bounds must be ISO calendar dates")
        try:
            parsed = date_type.fromisoformat(value)
        except ValueError as error:
            raise ValueError("date bounds must be ISO calendar dates") from error
        if parsed.isoformat() != value:
            raise ValueError("date bounds must be ISO calendar dates")
        return parsed

    @model_validator(mode="after")
    def _validate_date_window(self) -> FinvizPriceTargetQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self


QueryT = TypeVar("QueryT", bound=QueryParams)
ModelT = TypeVar("ModelT", bound=ContractModel)


class _FinvizFetcher(Fetcher[QueryT, bytes], Generic[QueryT, ModelT]):
    """Shared GET and validation pipeline for public Finviz HTML pages."""

    async_mode: ClassVar[str] = "bounded_thread"
    query_type: ClassVar[type[QueryParams]]
    endpoint: ClassVar[str]
    capability: ClassVar[Capability]
    canonical_model: ClassVar[str | None]

    def transform_query(self, **kwargs: object) -> QueryT:
        """Validate only fields the concrete page class can honor."""
        return cast("QueryT", self.query_type.model_validate(kwargs))

    def extract_data(self, params: QueryT, ctx: FetchContext) -> bytes:
        """Fetch one public HTML page without a credential or private route."""
        return _source.request_html(self.endpoint, self.request_params(params), timeout=ctx.timeout)

    def request_params(self, params: QueryT) -> dict[str, str]:
        """Build source query parameters from the validated model."""
        raise NotImplementedError

    def transform_data(self, raw: bytes, params: QueryT) -> FetchResult:
        """Parse a source page into concrete typed rows."""
        raise NotImplementedError


class FinvizCompareGroupsFetcher(_FinvizFetcher[FinvizGroupQuery, FinvizGroupComparison]):
    """Return one sector table from Finviz Groups."""

    query_type = FinvizGroupQuery
    endpoint = "/groups"
    canonical_model = "CompareGroups"
    row_model: ClassVar[type[ContractModel]] = FinvizGroupComparison
    capability = Capability(
        asset_class="equity_group",
        domain="finviz_compare_groups",
        period="snapshot",
        market="us",
        source="finviz",
        verified=False,
        notes="Public sector table; quotes delayed by one minute.",
    )

    def request_params(self, params: FinvizGroupQuery) -> dict[str, str]:
        """Build the selected group and view query parameters."""
        view = "110" if params.metric == "overview" else "140"
        return {"g": params.group, "o": "name", "st": "d1", "v": view}

    def transform_data(
        self,
        raw: bytes,
        params: FinvizGroupQuery,
    ) -> tuple[FinvizGroupComparison, ...]:
        """Parse the selected group metric from the source page."""
        return parse_group_comparison(raw, metric=params.metric)


class FinvizEtfPricePerformanceFetcher(_FinvizFetcher[FinvizPageQuery, FinvizEtfPricePerformance]):
    """Return one page of ETF performance rows."""

    query_type = FinvizPageQuery
    endpoint = "/screener"
    canonical_model = "EtfPricePerformance"
    capability = Capability(
        asset_class="etf",
        domain="finviz_etf_performance",
        period="snapshot",
        market="us",
        source="finviz",
        verified=False,
        notes="Public ETF Perf HTML page; percentage cells retain displayed percentage points.",
    )

    def request_params(self, params: FinvizPageQuery) -> dict[str, str]:
        """Build the ETF performance page selector and row offset."""
        return _page_params("191", params.page)

    def transform_data(
        self,
        raw: bytes,
        params: FinvizPageQuery,
    ) -> tuple[FinvizEtfPricePerformance, ...]:
        """Parse ETF performance rows from the selected page."""
        return parse_etf_price_performance(raw)


class FinvizPricePerformanceFetcher(_FinvizFetcher[FinvizPageQuery, FinvizPricePerformance]):
    """Return one page of stock lookback-performance rows."""

    query_type = FinvizPageQuery
    endpoint = "/screener"
    canonical_model = "PricePerformance"
    capability = Capability(
        asset_class="stock",
        domain="finviz_price_performance",
        period="snapshot",
        market="us",
        source="finviz",
        verified=False,
        notes="Public Performance HTML page; percentage cells retain displayed percentage points.",
    )

    def request_params(self, params: FinvizPageQuery) -> dict[str, str]:
        """Build the stock performance page selector and row offset."""
        return _page_params("141", params.page)

    def transform_data(
        self,
        raw: bytes,
        params: FinvizPageQuery,
    ) -> tuple[FinvizPricePerformance, ...]:
        """Parse stock performance rows from the selected page."""
        return parse_price_performance(raw)


class FinvizEquityInfoFetcher(_FinvizFetcher[FinvizStockPageQuery, FinvizEquityInfo]):
    """Return one company's public profile identity and selected facts."""

    query_type = FinvizStockPageQuery
    endpoint = "/stock"
    canonical_model = "EquityInfo"
    row_model: ClassVar[type[ContractModel]] = FinvizEquityInfo
    capability = Capability(
        asset_class="stock",
        domain="finviz_equity_info",
        period="snapshot",
        market="us",
        source="finviz",
        verified=False,
        notes=(
            "Public stock-page profile fields; source page is not timestamped as a profile record."
        ),
    )

    def request_params(self, params: FinvizStockPageQuery) -> dict[str, str]:
        """Build the single-symbol profile page query."""
        return {"t": params.symbol}

    def transform_data(
        self,
        raw: bytes,
        params: FinvizStockPageQuery,
    ) -> tuple[FinvizEquityInfo, ...]:
        """Parse the requested symbol's profile from the source page."""
        return (parse_equity_info(raw, symbol=params.symbol),)


class FinvizKeyMetricsFetcher(_FinvizFetcher[FinvizStockPageQuery, FinvizKeyMetrics]):
    """Return one company's recognized key metric snapshot."""

    query_type = FinvizStockPageQuery
    endpoint = "/stock"
    canonical_model = "KeyMetrics"
    row_model: ClassVar[type[ContractModel]] = FinvizKeyMetrics
    capability = Capability(
        asset_class="stock",
        domain="finviz_key_metrics",
        period="snapshot",
        market="us",
        source="finviz",
        verified=False,
        notes="Public stock-page metrics; values and percentages use Finviz display units.",
    )

    def request_params(self, params: FinvizStockPageQuery) -> dict[str, str]:
        """Build the single-symbol key-metrics page query."""
        return {"t": params.symbol}

    def transform_data(
        self,
        raw: bytes,
        params: FinvizStockPageQuery,
    ) -> tuple[FinvizKeyMetrics, ...]:
        """Parse recognized key metrics for the requested symbol."""
        return (parse_key_metrics(raw, symbol=params.symbol),)


class FinvizPriceTargetFetcher(_FinvizFetcher[FinvizPriceTargetQuery, FinvizPriceTarget]):
    """Return currently visible analyst actions on one stock page."""

    query_type = FinvizPriceTargetQuery
    endpoint = "/stock"
    canonical_model = "PriceTarget"
    row_model: ClassVar[type[ContractModel]] = FinvizPriceTarget
    capability = Capability(
        asset_class="stock",
        domain="finviz_price_target",
        period="event",
        market="us",
        source="finviz",
        verified=False,
        notes=(
            "Only analyst rows rendered on the public page are returned; "
            "older-history expansion is unavailable."
        ),
    )

    def request_params(self, params: FinvizPriceTargetQuery) -> dict[str, str]:
        """Build the single-symbol analyst-events page query."""
        return {"t": params.symbol}

    def transform_data(
        self,
        raw: bytes,
        params: FinvizPriceTargetQuery,
    ) -> tuple[FinvizPriceTarget, ...]:
        """Parse visible analyst actions within the requested date window."""
        return parse_price_targets(
            raw,
            symbol=params.symbol,
            start_date=params.start_date,
            end_date=params.end_date,
        )


class FinvizEquityScreenerFetcher(_FinvizFetcher[FinvizEquityScreenerQuery, FinvizEquityScreener]):
    """Return the anonymous, unfiltered first page of the public Stock Screener Overview table."""

    query_type = FinvizEquityScreenerQuery
    endpoint = "/screener"
    canonical_model = "EquityScreener"
    capability = Capability(
        asset_class="stock",
        domain="finviz_equity_screener",
        period="snapshot",
        market="us",
        source="finviz",
        verified=False,
        notes=(
            "Anonymous, unfiltered first page only; at most 20 source rows and an 11-column "
            "subset. "
            "This is not a complete-market universe or the full 61-field screener contract."
        ),
    )

    def request_params(self, params: FinvizEquityScreenerQuery) -> dict[str, str]:
        """Select only Finviz's public anonymous Overview first page."""
        return {"v": "111"}

    def transform_data(
        self,
        raw: bytes,
        params: FinvizEquityScreenerQuery,
    ) -> tuple[FinvizEquityScreener, ...]:
        """Parse equity screener rows from the selected page."""
        return parse_equity_screener(raw)


def _page_params(view: str, page: int) -> dict[str, str]:
    """Use Finviz's verified 20-row offset query for one page, with no invented bulk loop."""
    result = {"v": view}
    if page > 1:
        result["r"] = str((page - 1) * _PAGE_SIZE + 1)
    return result
