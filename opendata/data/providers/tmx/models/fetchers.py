"""Strict, source-specific TMX Money GraphQL queries and row mappings."""

from __future__ import annotations

import datetime
import re
from typing import ClassVar, Literal, TypeVar

from pydantic import ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.base import ContractModel
from opendata.data.models.tmx import (
    TmxCompanyFiling,
    TmxEquityHistorical,
    TmxEquityInfo,
    TmxEquityQuote,
    TmxEquitySearch,
    TmxHistoricalDividend,
    TmxIndexConstituent,
    TmxPriceTargetConsensus,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.tmx.models import client
from opendata.data.providers.tmx.models.client import (
    DEFAULT_TIMEOUT_SECONDS,
    DIVIDEND_PAGE_SIZE,
    MAX_DIVIDEND_PAGES,
    MAX_RESPONSE_ROWS,
    GraphqlReply,
    TmxProviderError,
    list_rows,
)

_Date = datetime.date

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9^][A-Za-z0-9.^:/_-]{0,31}$")
_MAX_HISTORY_DAYS = 10_000
_MAX_FILINGS_LIMIT = 1_000
_QueryT = TypeVar("_QueryT", bound=QueryParams)
_ContractT = TypeVar("_ContractT", bound=ContractModel)


def _capability(domain: str, period: str, model: str) -> Capability:
    return Capability(
        asset_class="equity",
        domain=domain,
        period=period,
        market="CA",
        source="tmx",
        verified=False,
        notes=f"{model}: public TMX Money UI GraphQL shape; synthetic fixtures only",
    )


class _TmxQuery(QueryParams):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    source: Literal["auto", "tmx"] = "auto"
    market: Literal["CA"] | None = None

    @model_validator(mode="after")
    def _validate_source(self) -> _TmxQuery:
        if self.source not in {"auto", "tmx"}:
            raise ValueError("source must be 'auto' or 'tmx'")
        return self


def _validate_symbol(value: str) -> str:
    if not value or value != value.strip() or not _SYMBOL_RE.fullmatch(value):
        raise ValueError("symbol must be a nonempty TMX identifier without whitespace")
    return value


class EquitySearchQuery(_TmxQuery):
    keywords: StrictStr

    @field_validator("keywords")
    @classmethod
    def _validate_keywords(cls, value: str) -> str:
        if not value or value != value.strip() or len(value) > 120:
            raise ValueError("keywords must be 1..120 characters without outer whitespace")
        if any(ord(character) < 32 for character in value):
            raise ValueError("keywords must not contain control characters")
        return value


class _TmxEquitySearchSourceRow(ContractModel):
    """Strict shape selected by TMX's company-search GraphQL operation."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    symbol: StrictStr | None
    name: StrictStr | None
    exchange: StrictStr | None
    instrument_type: StrictStr | None = Field(alias="instrumentType")


class EquityHistoricalQuery(_TmxQuery):
    symbol: StrictStr
    start_date: _Date
    end_date: _Date

    @field_validator("symbol")
    @classmethod
    def _validate_symbol_value(cls, value: str) -> str:
        return _validate_symbol(value)

    @model_validator(mode="after")
    def _validate_window(self) -> EquityHistoricalQuery:
        days = (self.end_date - self.start_date).days + 1
        if days <= 0:
            raise ValueError("start_date must be on or before end_date")
        if days > _MAX_HISTORY_DAYS:
            raise ValueError(f"history is limited to {_MAX_HISTORY_DAYS} calendar days")
        return self


class _SymbolQuery(_TmxQuery):
    symbol: StrictStr

    @field_validator("symbol")
    @classmethod
    def _validate_symbol_value(cls, value: str) -> str:
        return _validate_symbol(value)


class CompanyFilingsQuery(_SymbolQuery):
    start_date: _Date | None = None
    end_date: _Date | None = None
    limit: StrictInt = 100

    @model_validator(mode="after")
    def _validate_filters(self) -> CompanyFilingsQuery:
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        if not 1 <= self.limit <= _MAX_FILINGS_LIMIT:
            raise ValueError(f"limit must be from 1 through {_MAX_FILINGS_LIMIT}")
        return self


class HistoricalDividendsQuery(_SymbolQuery):
    start_date: _Date | None = None
    end_date: _Date | None = None

    @model_validator(mode="after")
    def _validate_filters(self) -> HistoricalDividendsQuery:
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        return self


class PriceTargetConsensusQuery(_SymbolQuery):
    pass


class EquityQuoteQuery(_SymbolQuery):
    pass


class EquityInfoQuery(_SymbolQuery):
    pass


class IndexConstituentsQuery(_SymbolQuery):
    pass


class _TypedFetcher(Fetcher[_QueryT, GraphqlReply]):
    async_mode = "bounded_thread"

    @staticmethod
    def _timeout(ctx: FetchContext) -> float:
        return (
            DEFAULT_TIMEOUT_SECONDS
            if ctx.timeout is None
            else min(ctx.timeout, DEFAULT_TIMEOUT_SECONDS)
        )


def _root(reply: GraphqlReply, field: str) -> object:
    return reply.data[field]


def _row(model: type[_ContractT], source: object, **fixed: object) -> _ContractT:
    if not isinstance(source, dict):
        raise TmxProviderError("TMX_BAD_ROW")
    try:
        return model.model_validate({**source, **fixed})
    except Exception:
        raise TmxProviderError("TMX_BAD_ROW") from None


def _rows(model: type[ContractModel], value: object) -> tuple[ContractModel, ...]:
    return tuple(_row(model, item) for item in list_rows(value))


def _equity_search_rows(value: object) -> tuple[TmxEquitySearch, ...]:
    """Validate the full source shape, then project TMX's documented fields."""
    rows: list[TmxEquitySearch] = []
    for item in list_rows(value):
        if not isinstance(item, dict):
            raise TmxProviderError("TMX_BAD_ROW")
        try:
            source = _TmxEquitySearchSourceRow.model_validate(item)
            # OpenBB's TMX EquitySearch output contract contains only symbol
            # and name. The additional selected source fields are validated
            # above, but are not part of this provider's public row contract.
            rows.append(TmxEquitySearch(symbol=source.symbol, name=source.name))
        except Exception:
            raise TmxProviderError("TMX_BAD_ROW") from None
    return tuple(rows)


class EquitySearchFetcher(_TypedFetcher[EquitySearchQuery]):
    """Search TMX Money companies by keyword."""

    canonical_model: ClassVar[str | None] = "EquitySearch"
    capability: ClassVar[Capability] = _capability("tmx_equity_search", "snapshot", "EquitySearch")

    def transform_query(self, **kwargs: object) -> EquitySearchQuery:
        """Validate company-search keywords and source selectors."""
        return EquitySearchQuery.model_validate(kwargs)

    def extract_data(self, params: EquitySearchQuery, ctx: FetchContext) -> GraphqlReply:
        """Run the allowlisted company-keyword GraphQL operation."""
        return client.graphql(
            "findTmxCompaniesByKeywords",
            {"keywords": params.keywords},
            timeout=self._timeout(ctx),
        )

    def transform_data(self, raw: GraphqlReply, params: EquitySearchQuery) -> FetchResult:
        """Validate source rows and project TMX EquitySearch output fields."""
        return _equity_search_rows(_root(raw, "findTmxCompaniesByKeywords"))


class EquityHistoricalFetcher(_TypedFetcher[EquityHistoricalQuery]):
    """Fetch and map bounded daily equity price history."""

    canonical_model: ClassVar[str | None] = "EquityHistorical"
    capability: ClassVar[Capability] = _capability(
        "tmx_equity_historical", "daily", "EquityHistorical"
    )

    def transform_query(self, **kwargs: object) -> EquityHistoricalQuery:
        """Validate the symbol and bounded historical date window."""
        return EquityHistoricalQuery.model_validate(kwargs)

    def extract_data(self, params: EquityHistoricalQuery, ctx: FetchContext) -> GraphqlReply:
        """Run the TMX daily price-history GraphQL operation."""
        return client.graphql(
            "getCompanyPriceHistory",
            {
                "symbol": params.symbol,
                "start": params.start_date.isoformat(),
                "end": params.end_date.isoformat(),
                "adjusted": False,
                "adjustmentType": None,
                "unadjusted": None,
                "limit": min(_MAX_HISTORY_DAYS, (params.end_date - params.start_date).days + 1),
            },
            timeout=self._timeout(ctx),
        )

    def transform_data(self, raw: GraphqlReply, params: EquityHistoricalQuery) -> FetchResult:
        """Map daily source rows into the strict history contract."""
        source_rows = list_rows(_root(raw, "getCompanyPriceHistory"))
        result = []
        for source_row in source_rows:
            mapped = {
                "symbol": params.symbol,
                "date": source_row.get("datetime"),
                "open": source_row.get("openPrice"),
                "high": source_row.get("high"),
                "low": source_row.get("low"),
                "close": source_row.get("closePrice"),
                "volume": source_row.get("volume"),
                "vwap": source_row.get("vwap"),
                "change": source_row.get("change"),
                # TMX's raw percent-change scale is not established by its UI schema.
                "change_percent": None,
                "transactions": source_row.get("numberOfTrade"),
                "transactions_value": source_row.get("tradeValue"),
            }
            result.append(_row(TmxEquityHistorical, mapped))
        return tuple(result)


class EquityQuoteFetcher(_TypedFetcher[EquityQuoteQuery]):
    """Fetch the public TMX quote snapshot for one symbol."""

    canonical_model: ClassVar[str | None] = "EquityQuote"
    capability: ClassVar[Capability] = _capability("tmx_equity_quote", "snapshot", "EquityQuote")

    def transform_query(self, **kwargs: object) -> EquityQuoteQuery:
        """Validate the quote symbol and source selectors."""
        return EquityQuoteQuery.model_validate(kwargs)

    def extract_data(self, params: EquityQuoteQuery, ctx: FetchContext) -> GraphqlReply:
        """Run the TMX quote GraphQL operation."""
        return client.graphql(
            "getQuoteBySymbol",
            {"symbol": params.symbol, "locale": "en"},
            timeout=self._timeout(ctx),
        )

    def transform_data(self, raw: GraphqlReply, params: EquityQuoteQuery) -> FetchResult:
        """Map quote fields into one typed output row or an empty result."""
        value = _root(raw, "getQuoteBySymbol")
        if value is None:
            return ()
        if not isinstance(value, dict):
            raise TmxProviderError("TMX_BAD_ROW")
        return (
            _row(
                TmxEquityQuote,
                {
                    "symbol": value.get("symbol"),
                    "name": value.get("name"),
                    "exchange": value.get("exchangeCode"),
                    "last_price": value.get("price"),
                    "open": value.get("openPrice"),
                    "high": value.get("dayHigh"),
                    "low": value.get("dayLow"),
                    "volume": value.get("volume"),
                    "prev_close": value.get("prevClose"),
                    "change": value.get("priceChange"),
                    # TMX percentChange scale is not established by the UI schema.
                    "change_percent": None,
                    "year_high": value.get("weeks52high"),
                    "year_low": value.get("weeks52low"),
                    "market_cap": value.get("MarketCap"),
                    "pe": value.get("peRatio"),
                    "eps": value.get("eps"),
                    "vwap": value.get("vwap"),
                    "currency": value.get("currency"),
                },
            ),
        )


class EquityInfoFetcher(_TypedFetcher[EquityInfoQuery]):
    """Fetch public company profile fields from the TMX quote operation."""

    canonical_model: ClassVar[str | None] = "EquityInfo"
    capability: ClassVar[Capability] = _capability("tmx_equity_info", "snapshot", "EquityInfo")

    def transform_query(self, **kwargs: object) -> EquityInfoQuery:
        """Validate the company-info symbol and source selectors."""
        return EquityInfoQuery.model_validate(kwargs)

    def extract_data(self, params: EquityInfoQuery, ctx: FetchContext) -> GraphqlReply:
        """Run the TMX operation that supplies quote and profile fields."""
        return client.graphql(
            "getQuoteBySymbol",
            {"symbol": params.symbol, "locale": "en"},
            timeout=self._timeout(ctx),
        )

    def transform_data(self, raw: GraphqlReply, params: EquityInfoQuery) -> FetchResult:
        """Map available company profile fields into the typed contract."""
        value = _root(raw, "getQuoteBySymbol")
        if value is None:
            return ()
        if not isinstance(value, dict):
            raise TmxProviderError("TMX_BAD_ROW")
        return (
            _row(
                TmxEquityInfo,
                {
                    "symbol": value.get("symbol"),
                    "name": value.get("name"),
                    "stock_exchange": value.get("exchangeName"),
                    "long_description": value.get("longDescription"),
                    "company_url": value.get("website"),
                    "business_address": value.get("fullAddress"),
                    "business_phone_no": value.get("phoneNumber"),
                    "email": value.get("email"),
                    "employees": value.get("employees"),
                    "sector": value.get("sector"),
                    "issue_type": value.get("issueType"),
                    "shares_outstanding": value.get("shareOutStanding"),
                    "shares_escrow": value.get("sharesESCROW"),
                    "shares_total": value.get("totalSharesOutStanding"),
                    "dividend_frequency": value.get("dividendFrequency"),
                },
            ),
        )


class CompanyFilingsFetcher(_TypedFetcher[CompanyFilingsQuery]):
    """Fetch and map bounded company filing rows."""

    canonical_model: ClassVar[str | None] = "CompanyFilings"
    capability: ClassVar[Capability] = _capability(
        "tmx_company_filings", "document", "CompanyFilings"
    )

    def transform_query(self, **kwargs: object) -> CompanyFilingsQuery:
        """Validate filing symbol, date bounds, and row limit."""
        return CompanyFilingsQuery.model_validate(kwargs)

    def extract_data(self, params: CompanyFilingsQuery, ctx: FetchContext) -> GraphqlReply:
        """Run the bounded TMX company-filings GraphQL operation."""
        return client.graphql(
            "getCompanyFilings",
            {
                "symbol": params.symbol,
                "fromDate": params.start_date.isoformat() if params.start_date else None,
                "toDate": params.end_date.isoformat() if params.end_date else None,
                "limit": params.limit,
            },
            timeout=self._timeout(ctx),
        )

    def transform_data(self, raw: GraphqlReply, params: CompanyFilingsQuery) -> FetchResult:
        """Map source filings while retaining unclassified report types as null."""
        source_rows = list_rows(_root(raw, "filings"))
        rows = tuple(
            _row(
                TmxCompanyFiling,
                {
                    "symbol": params.symbol,
                    "filing_date": row.get("filingDate"),
                    # The source's name field has no proven report-type mapping.
                    "report_type": None,
                    "report_url": row.get("urlToPdf"),
                    "description": row.get("description"),
                    "size": row.get("size"),
                },
            )
            for row in source_rows
        )
        if len(rows) > params.limit:
            raise TmxProviderError("TMX_BAD_ROWS")
        return rows


class HistoricalDividendsFetcher(_TypedFetcher[HistoricalDividendsQuery]):
    """Fetch dividend history using explicit bounded source pagination."""

    canonical_model: ClassVar[str | None] = "HistoricalDividends"
    capability: ClassVar[Capability] = _capability(
        "tmx_historical_dividends", "event", "HistoricalDividends"
    )

    def transform_query(self, **kwargs: object) -> HistoricalDividendsQuery:
        """Validate the dividend symbol and optional ex-date bounds."""
        return HistoricalDividendsQuery.model_validate(kwargs)

    def extract_data(self, params: HistoricalDividendsQuery, ctx: FetchContext) -> GraphqlReply:
        """Read dividend pages until TMX reports an explicit terminal page."""
        pages: list[object] = []
        seen_page_numbers: set[int] = set()
        for page in range(1, MAX_DIVIDEND_PAGES + 1):
            reply = client.graphql(
                "getDividendsForSymbol",
                {"symbol": params.symbol, "page": page, "batch": DIVIDEND_PAGE_SIZE},
                timeout=self._timeout(ctx),
            )
            page_result = _root(reply, "dividendHistory")
            if not isinstance(page_result, dict):
                raise TmxProviderError("TMX_BAD_PAGE")
            page_number = page_result.get("pageNumber")
            has_next = page_result.get("hasNextPage")
            if (
                type(page_number) is not int
                or page_number != page
                or page_number in seen_page_numbers
            ):
                raise TmxProviderError("TMX_BAD_PAGE")
            if type(has_next) is not bool:
                raise TmxProviderError("TMX_BAD_PAGE")
            seen_page_numbers.add(page_number)
            rows = list_rows(page_result.get("dividends"))
            if len(rows) > DIVIDEND_PAGE_SIZE:
                raise TmxProviderError("TMX_BAD_PAGE")
            pages.extend(rows)
            if len(pages) > MAX_RESPONSE_ROWS:
                raise TmxProviderError("TMX_BAD_ROWS")
            if not has_next:
                return GraphqlReply(data={"dividends": pages})
        raise TmxProviderError("TMX_PAGE_LIMIT")

    def transform_data(self, raw: GraphqlReply, params: HistoricalDividendsQuery) -> FetchResult:
        """Map dividends and apply the requested ex-date bounds."""
        mapped = []
        for item in list_rows(_root(raw, "dividends")):
            row = _row(
                TmxHistoricalDividend,
                {
                    "symbol": params.symbol,
                    "ex_dividend_date": item.get("exDate"),
                    "amount": item.get("amount"),
                    "currency": item.get("currency"),
                    "payment_date": item.get("payableDate"),
                    "declaration_date": item.get("declarationDate"),
                    "record_date": item.get("recordDate"),
                },
            )
            if params.start_date is not None and row.ex_dividend_date < params.start_date:
                continue
            if params.end_date is not None and row.ex_dividend_date > params.end_date:
                continue
            mapped.append(row)
        return tuple(mapped)


class IndexConstituentsFetcher(_TypedFetcher[IndexConstituentsQuery]):
    """Fetch and map the member rows for one TMX index."""

    canonical_model: ClassVar[str | None] = "IndexConstituents"
    capability: ClassVar[Capability] = _capability(
        "tmx_index_constituents", "snapshot", "IndexConstituents"
    )

    def transform_query(self, **kwargs: object) -> IndexConstituentsQuery:
        """Validate the index symbol and source selectors."""
        return IndexConstituentsQuery.model_validate(kwargs)

    def extract_data(self, params: IndexConstituentsQuery, ctx: FetchContext) -> GraphqlReply:
        """Run the TMX index-constituent GraphQL operation."""
        return client.graphql(
            "getIndexConstituents",
            {"symbol": params.symbol},
            timeout=self._timeout(ctx),
        )

    def transform_data(self, raw: GraphqlReply, params: IndexConstituentsQuery) -> FetchResult:
        """Map source constituent rows into the strict output contract."""
        result = [
            _row(
                TmxIndexConstituent,
                {
                    "symbol": item.get("symbol"),
                    "name": item.get("longName"),
                    "exchange": item.get("exchange"),
                    "market_value": item.get("quotedMarketValue"),
                    "weight": item.get("weight"),
                },
            )
            for item in list_rows(_root(raw, "constituents"))
        ]
        return tuple(result)


class PriceTargetConsensusFetcher(_TypedFetcher[PriceTargetConsensusQuery]):
    """Fetch analyst target and consensus fields for one company."""

    canonical_model: ClassVar[str | None] = "PriceTargetConsensus"
    capability: ClassVar[Capability] = _capability(
        "tmx_price_target_consensus", "snapshot", "PriceTargetConsensus"
    )

    def transform_query(self, **kwargs: object) -> PriceTargetConsensusQuery:
        """Validate the price-target symbol and source selectors."""
        return PriceTargetConsensusQuery.model_validate(kwargs)

    def extract_data(self, params: PriceTargetConsensusQuery, ctx: FetchContext) -> GraphqlReply:
        """Run the TMX analyst-consensus GraphQL operation."""
        return client.graphql(
            "getCompanyAnalysts",
            {"symbol": params.symbol, "datatype": None},
            timeout=self._timeout(ctx),
        )

    def transform_data(self, raw: GraphqlReply, params: PriceTargetConsensusQuery) -> FetchResult:
        """Map supported target and analyst-count fields into one typed row."""
        value = _root(raw, "getCompanyAnalysts")
        if value is None:
            return ()
        if not isinstance(value, dict):
            raise TmxProviderError("TMX_BAD_ROW")
        price_target = value.get("priceTarget")
        consensus = value.get("consensusAnalysts")
        if not isinstance(price_target, dict) or not isinstance(consensus, dict):
            raise TmxProviderError("TMX_BAD_ROW")
        return (
            _row(
                TmxPriceTargetConsensus,
                {
                    "symbol": params.symbol,
                    "target_high": price_target.get("highPriceTarget"),
                    "target_low": price_target.get("lowPriceTarget"),
                    "target_consensus": price_target.get("priceTarget"),
                    # TMX does not publish a target median in this operation.
                    "target_median": None,
                    # The scale of TMX priceTargetUpside is not documented.
                    "target_upside": None,
                    "total_analysts": value.get("totalAnalysts"),
                    "buy_ratings": consensus.get("buy"),
                    "sell_ratings": consensus.get("sell"),
                    "hold_ratings": consensus.get("hold"),
                    # The value format of consensus is not established.
                    "consensus_action": None,
                },
            ),
        )


__all__ = [
    "CompanyFilingsFetcher",
    "EquityHistoricalFetcher",
    "EquityInfoFetcher",
    "EquityQuoteFetcher",
    "EquitySearchFetcher",
    "HistoricalDividendsFetcher",
    "IndexConstituentsFetcher",
    "PriceTargetConsensusFetcher",
]
