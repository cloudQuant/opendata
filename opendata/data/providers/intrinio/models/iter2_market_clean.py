"""Clean-room, typed adapters for documented Intrinio market endpoints.

The implementation uses only the public Intrinio v2 HTTP contract and the
public OpenBB row schemas.  It does not import provider SDKs or call raw
``requests``; every network attempt goes through the repository's governed
HTTP client.
"""

from __future__ import annotations

import math
import os
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar, Generic, Literal, TypeVar
from urllib.parse import quote

from pydantic import ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from opendata.data.capability import Capability
from opendata.data.http_client import get_shared_http_client
from opendata.data.models.base import ContractModel  # noqa: TC001 - get_type_hints reads row_model
from opendata.data.models.intrinio_market_clean import (
    IntrinioEquityHistorical,
    IntrinioEquityInfo,
    IntrinioEquityQuote,
    IntrinioEtfHistorical,
    IntrinioEtfHolding,
    IntrinioEtfInfo,
    IntrinioEtfSearch,
    IntrinioHistoricalMarketCap,
    IntrinioIndexHistorical,
    IntrinioOptionsChain,
    IntrinioOptionsUnusual,
    IntrinioPriceTargetConsensus,
    IntrinioShareStatistics,
)
from opendata.data.protocol import BaseQueryParams, FetchContext, Fetcher, QueryParams
from opendata.data.providers.intrinio._source import SOURCE

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opendata.data.protocol import FetchResult

INTRINIO_API_BASE = "https://api-v2.intrinio.com"
_MAX_PAGES = 20
_PAGE_SIZE = 100
_INTERVAL_TO_FREQUENCY = {
    "1d": "daily",
    "1W": "weekly",
    "1M": "monthly",
    "1Q": "quarterly",
    "1Y": "yearly",
}
_QUOTE_SOURCES = frozenset({"iex", "utp_delayed", "cta_a_delayed", "cta_b_delayed", "delayed_sip"})
CalendarDate = date
JsonObject = dict[str, object]
JsonRows = list[JsonObject]
_QueryT = TypeVar("_QueryT", bound=BaseQueryParams)
_RawT = TypeVar("_RawT")


class IntrinioMarketError(RuntimeError):
    """Base class for sanitized Intrinio contract failures.

    Every refusal this slice raises owns a stable ``code`` face and a ``status``
    face, the same two attributes the financial slice's :class:`IntrinioFinancialError`
    publishes through ``ProviderEngineError``.  ``status`` stays ``None`` because each
    of these refusals is raised *after* the governed client returned HTTP 200, so there
    is no upstream status to report; the code is what identifies the broken contract.
    """

    code: ClassVar[str] = "INTRINIO_MARKET_CONTRACT_INVALID"
    status: ClassVar[int | None] = None


class IntrinioMissingApiKeyError(IntrinioMarketError):
    """Raised before transport when the required Web API credential is absent."""

    code: ClassVar[str] = "INTRINIO_MARKET_API_KEY_MISSING"


class IntrinioResponseShapeError(IntrinioMarketError):
    """Raised when a response differs from the documented JSON shape."""

    code: ClassVar[str] = "INTRINIO_MARKET_SHAPE_INVALID"


class IntrinioPaginationLimitError(IntrinioMarketError):
    """Raised instead of publishing a silently truncated paged response."""

    code: ClassVar[str] = "INTRINIO_MARKET_PAGINATION_LIMIT"


class _MarketQuery(QueryParams):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: StrictStr = Field(min_length=1)
    source: Literal["auto", "intrinio"] = "auto"
    market: Literal["us", "all"] | None = None
    allows_date_window: ClassVar[bool] = False

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("symbol is invalid")
        return value.strip()

    @classmethod
    def _validate_window(cls, start_date: date | None, end_date: date | None) -> None:
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ValueError("start_date must not be after end_date")


class _DailyQuery(_MarketQuery):
    interval: Literal["1d", "1W", "1M", "1Q", "1Y"] = "1d"
    allows_date_window: ClassVar[bool] = True


class _CompanyInfoQuery(_MarketQuery):
    pass


class _EtfInfoQuery(_MarketQuery):
    pass


class _HistoricalMarketCapQuery(_MarketQuery):
    interval: Literal["day", "week", "month", "quarter", "year"] = "day"
    allows_date_window: ClassVar[bool] = True


class _IndexHistoricalQuery(_MarketQuery):
    interval: Literal["1d"] = "1d"
    allows_date_window: ClassVar[bool] = True


class _EquityQuoteQuery(_MarketQuery):
    quote_source: (
        Literal[
            "iex",
            "utp_delayed",
            "cta_a_delayed",
            "cta_b_delayed",
            "otc_delayed",
            "delayed_sip",
            "nasdaq_basic",
            "cboe_one_delayed",
            "iex_delayed",
        ]
        | None
    ) = None
    active_only: StrictBool | None = None


class _EtfHoldingsQuery(_MarketQuery):
    date: CalendarDate | None = None


class _EtfSearchQuery(QueryParams):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    query: StrictStr = Field(min_length=1)
    mode: Literal["starts_with"] = "starts_with"
    source: Literal["auto", "intrinio"] = "auto"
    market: Literal["us", "all"] | None = None


class _OptionsUnusualQuery(_MarketQuery):
    source: Literal["auto", "intrinio"] = "auto"
    option_source: Literal["realtime", "delayed"] = "delayed"


class _OptionsChainQuery(_MarketQuery):
    requested_date: CalendarDate | None = Field(default=None, alias="date")
    option_source: Literal["realtime", "delayed"] = "delayed"
    option_type: Literal["call", "put"] | None = None
    moneyness: Literal["all", "in_the_money", "out_of_the_money", "near_the_money"] | None = None
    stock_price_source: Literal[
        "iex",
        "delayed_sip",
        "utp_delayed",
        "otc_delayed",
        "cta_a_delayed",
        "cta_b_delayed",
        "nasdaq_basic",
        "cboe_one_delayed",
        "iex_delayed",
    ] = "iex"
    pricing_model: Literal["black_scholes", "bjerk"] = "black_scholes"
    show_stats: StrictBool = True
    show_extended_price: StrictBool = True
    include_related_symbols: StrictBool = False


class _PriceTargetConsensusQuery(_MarketQuery):
    industry_group_number: StrictInt | None = None


class _ShareStatisticsQuery(_MarketQuery):
    allows_date_window: ClassVar[bool] = True


class _IntrinioMarketFetcher(Fetcher[_QueryT, _RawT], Generic[_QueryT, _RawT]):
    """Shared finite-transport and JSON-shape behavior for this slice."""

    async_mode: ClassVar[str] = "bounded_thread"
    capability: ClassVar[Capability]
    canonical_model: ClassVar[str]
    query_model: ClassVar[type[_QueryT]]
    #: The witnessed subclasses below each name the class their own ``intrinio_*`` domain
    #: resolves to, so the governed service can revalidate rows against the frozen contract
    #: (``tests/test_federal_reserve_iter2_models.py:382`` pins that equality for every
    #: fetcher that declares a ``row_model``).
    row_model: ClassVar[type[ContractModel]]

    def transform_query(self, **kwargs: object) -> _QueryT:
        params = self.query_model.model_validate(kwargs)
        if isinstance(params, _MarketQuery):
            self._validate_standard_market_query(params)
        return params

    def _validate_standard_market_query(self, params: _MarketQuery) -> None:
        if not params.allows_date_window and (
            params.start_date is not None or params.end_date is not None
        ):
            raise ValueError("date window is not supported for this Intrinio endpoint")
        _MarketQuery._validate_window(params.start_date, params.end_date)

    def extract_data(self, params: _QueryT, ctx: FetchContext) -> _RawT:
        raise NotImplementedError

    def transform_data(self, raw: _RawT, params: _QueryT) -> FetchResult:
        raise NotImplementedError

    @staticmethod
    def _symbol_path(symbol: str) -> str:
        return quote(symbol, safe="")

    @staticmethod
    def _credential() -> str:
        key = os.getenv("INTRINIO_API_KEY", "")
        if not key.strip():
            raise IntrinioMissingApiKeyError("INTRINIO_API_KEY is required")
        return key.strip()

    def _get_json(
        self,
        path: str,
        params: Mapping[str, str] | None,
        ctx: FetchContext,
    ) -> JsonObject:
        key = self._credential()
        response = get_shared_http_client().get(
            f"{INTRINIO_API_BASE}{path}",
            params=params,
            headers={"Authorization": f"Bearer {key}"},
            timeout=ctx.timeout or 10.0,
            source=SOURCE,
            cache_context={"canonical_model": self.canonical_model},
        )
        try:
            payload: object = response.json()
        except Exception as exc:
            raise IntrinioResponseShapeError("Intrinio response is not valid JSON") from exc
        return _json_object(payload, "Intrinio response must be a JSON object")

    def _get_pages(
        self,
        path: str,
        params: Mapping[str, str],
        rows_key: str,
        ctx: FetchContext,
    ) -> JsonRows:
        cursor: str | None = None
        seen: set[str] = set()
        records: JsonRows = []
        base = dict(params)
        base["page_size"] = str(_PAGE_SIZE)
        for _ in range(_MAX_PAGES):
            page_params = dict(base)
            if cursor is not None:
                page_params["next_page"] = cursor
            payload = self._get_json(path, page_params, ctx)
            rows = payload.get(rows_key)
            if not isinstance(rows, list):
                raise IntrinioResponseShapeError(
                    f"Intrinio response field {rows_key!r} must be an array of objects"
                )
            try:
                records.extend(
                    _json_object(
                        row,
                        f"Intrinio response field {rows_key!r} must be an array of objects",
                    )
                    for row in rows
                )
            except IntrinioResponseShapeError as exc:
                raise IntrinioResponseShapeError(
                    f"Intrinio response field {rows_key!r} must be an array of objects"
                ) from exc
            if "next_page" not in payload:
                raise IntrinioResponseShapeError("Intrinio paged response must include next_page")
            next_page = payload.get("next_page")
            if next_page is None:
                return records
            if not isinstance(next_page, str) or not next_page.strip():
                raise IntrinioResponseShapeError(
                    "Intrinio next_page must be a nonempty string or null"
                )
            if next_page in seen:
                raise IntrinioResponseShapeError("Intrinio pagination cursor repeated")
            seen.add(next_page)
            cursor = next_page
        raise IntrinioPaginationLimitError(
            f"Intrinio pagination exceeded the {_MAX_PAGES}-page safety ceiling"
        )

    @staticmethod
    def _rows(raw: object, key: str) -> JsonRows:
        if not isinstance(raw, list):
            raise IntrinioResponseShapeError(f"{key!r} source rows are invalid")
        try:
            return [_json_object(row, f"{key!r} rows must be JSON objects") for row in raw]
        except IntrinioResponseShapeError as exc:
            raise IntrinioResponseShapeError(f"{key!r} rows must be JSON objects") from exc


def _json_object(value: object, message: str) -> JsonObject:
    if not isinstance(value, dict):
        raise IntrinioResponseShapeError(message)
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise IntrinioResponseShapeError(message)
        result[key] = item
    return result


def _capability(asset_class: str, domain: str, period: str, market: str = "us") -> Capability:
    return Capability(
        asset_class=asset_class,
        domain=f"intrinio_{domain}",
        period=period,
        market=market,
        source=SOURCE,
        verified=False,
        notes="official-docs-and-offline-contract-only",
    )


class IntrinioEquityInfoFetcher(_IntrinioMarketFetcher[_CompanyInfoQuery, JsonObject]):
    """Fetch and normalize a single Intrinio company profile."""

    canonical_model = "EquityInfo"
    row_model = IntrinioEquityInfo
    query_model = _CompanyInfoQuery
    capability = _capability("equity", "equity_info", "snapshot")

    def extract_data(self, params: _CompanyInfoQuery, ctx: FetchContext) -> JsonObject:
        """Fetch the documented company profile endpoint."""
        return self._get_json(f"/companies/{self._symbol_path(params.symbol)}", None, ctx)

    def transform_data(
        self, raw: JsonObject, params: _CompanyInfoQuery
    ) -> list[IntrinioEquityInfo]:
        """Map a company profile into the strict public row contract."""
        if not isinstance(raw, dict):
            raise IntrinioResponseShapeError("company response must be a JSON object")
        row = dict(raw)
        row["symbol"] = row.get("ticker") or params.symbol
        allowed = set(IntrinioEquityInfo.model_fields)
        return [
            IntrinioEquityInfo.model_validate(
                {key: value for key, value in row.items() if key in allowed}
            )
        ]


class _PricesFetcher(_IntrinioMarketFetcher[_DailyQuery, JsonRows]):
    query_model = _DailyQuery
    model_type: ClassVar[type[ContractModel]]

    def extract_data(self, params: _DailyQuery, ctx: FetchContext) -> JsonRows:
        query: dict[str, str] = {
            "frequency": _INTERVAL_TO_FREQUENCY[params.interval],
            "sort_order": "asc",
        }
        if params.start_date is not None:
            query["start_date"] = params.start_date.isoformat()
        if params.end_date is not None:
            query["end_date"] = params.end_date.isoformat()
        return self._get_pages(
            f"/securities/{self._symbol_path(params.symbol)}/prices", query, "stock_prices", ctx
        )

    def transform_data(self, raw: JsonRows, params: _DailyQuery) -> list[ContractModel]:
        rows: list[ContractModel] = []
        for source_row in self._rows(raw, "stock_prices"):
            record = {
                "symbol": params.symbol,
                "date": source_row.get("date"),
                "open": source_row.get("open"),
                "high": source_row.get("high"),
                "low": source_row.get("low"),
                "close": source_row.get("close"),
                "volume": source_row.get("volume"),
                "vwap": None,
            }
            rows.append(self.model_type.model_validate(record))
        return rows


class IntrinioEquityHistoricalFetcher(_PricesFetcher):
    """Fetch historical OHLCV rows for an equity security."""

    canonical_model = "EquityHistorical"
    model_type = IntrinioEquityHistorical
    #: ``contract_model("intrinio_equity_historical")`` prints
    #: ``opendata.data.models.intrinio_market_clean.IntrinioEquityHistorical``.
    row_model = IntrinioEquityHistorical
    capability = _capability("equity", "equity_historical", "series")


class IntrinioEtfHistoricalFetcher(_PricesFetcher):
    """Fetch historical OHLCV rows for an ETF symbol."""

    canonical_model = "EtfHistorical"
    model_type = IntrinioEtfHistorical
    #: ``contract_model("intrinio_etf_historical")`` prints
    #: ``opendata.data.models.intrinio_market_clean.IntrinioEtfHistorical``.
    row_model = IntrinioEtfHistorical
    capability = _capability("etf", "etf_historical", "series")


class IntrinioEtfInfoFetcher(_IntrinioMarketFetcher[_EtfInfoQuery, JsonObject]):
    """Fetch and normalize a single Intrinio ETF profile."""

    canonical_model = "EtfInfo"
    query_model = _EtfInfoQuery
    #: ``contract_model("intrinio_etf_info")`` prints
    #: ``opendata.data.models.intrinio_market_clean.IntrinioEtfInfo``.
    row_model = IntrinioEtfInfo
    capability = _capability("etf", "etf_info", "snapshot")

    def extract_data(self, params: _EtfInfoQuery, ctx: FetchContext) -> JsonObject:
        """Fetch the documented ETF profile endpoint."""
        return self._get_json(f"/etfs/{self._symbol_path(params.symbol)}", None, ctx)

    def transform_data(self, raw: JsonObject, params: _EtfInfoQuery) -> list[IntrinioEtfInfo]:
        """Map ETF profile fields into the strict public row contract."""
        if not isinstance(raw, dict):
            raise IntrinioResponseShapeError("ETF profile must be a JSON object")
        mapping = {
            "symbol": raw.get("ticker") or params.symbol,
            "name": raw.get("name"),
            "issuer": raw.get("sponsor"),
            "domicile": raw.get("domicile"),
            "description": raw.get("description"),
            "inception_date": raw.get("inception_date"),
            "fund_listing_date": raw.get("fund_listing_date"),
            "data_change_date": raw.get("data_change_date"),
            "etn_maturity_date": raw.get("etn_maturity_date"),
            "is_listed": raw.get("is_live_listed"),
            "close_date": raw.get("close_date"),
            "exchange": raw.get("exchange_mic"),
            "isin": raw.get("isin"),
            "ric": raw.get("ric"),
            "sedol": raw.get("sedol"),
        }
        return [IntrinioEtfInfo.model_validate(mapping)]


class IntrinioHistoricalMarketCapFetcher(
    _IntrinioMarketFetcher[_HistoricalMarketCapQuery, JsonRows]
):
    """Fetch historical market-cap observations."""

    canonical_model = "HistoricalMarketCap"
    row_model = IntrinioHistoricalMarketCap
    query_model = _HistoricalMarketCapQuery
    capability = _capability("equity", "historical_market_cap", "series")

    def extract_data(self, params: _HistoricalMarketCapQuery, ctx: FetchContext) -> JsonRows:
        """Fetch market-cap observations at the requested frequency."""
        query: dict[str, str] = {
            "frequency": _INTERVAL_TO_FREQUENCY[
                {"day": "1d", "week": "1W", "month": "1M", "quarter": "1Q", "year": "1Y"}[
                    params.interval
                ]
            ],
            "sort_order": "asc",
        }
        if params.start_date is not None:
            query["start_date"] = params.start_date.isoformat()
        if params.end_date is not None:
            query["end_date"] = params.end_date.isoformat()
        return self._get_pages(
            f"/historical_data/{self._symbol_path(params.symbol)}/marketcap",
            query,
            "historical_data",
            ctx,
        )

    def transform_data(
        self, raw: JsonRows, params: _HistoricalMarketCapQuery
    ) -> list[IntrinioHistoricalMarketCap]:
        """Map source observations into typed market-cap rows."""
        return [
            IntrinioHistoricalMarketCap.model_validate(
                {"symbol": params.symbol, "date": row.get("date"), "market_cap": row.get("value")}
            )
            for row in self._rows(raw, "historical_data")
        ]


class IntrinioIndexHistoricalFetcher(_IntrinioMarketFetcher[_IndexHistoricalQuery, JsonRows]):
    """Fetch historical level observations for a market index."""

    canonical_model = "IndexHistorical"
    query_model = _IndexHistoricalQuery
    #: ``contract_model("intrinio_index_historical")`` prints
    #: ``opendata.data.models.intrinio_market_clean.IntrinioIndexHistorical``.
    row_model = IntrinioIndexHistorical
    capability = _capability("index", "index_historical", "series", market="all")

    def extract_data(self, params: _IndexHistoricalQuery, ctx: FetchContext) -> JsonRows:
        """Fetch the documented index-level history endpoint."""
        query: dict[str, str] = {"sort_order": "asc"}
        if params.start_date is not None:
            query["start_date"] = params.start_date.isoformat()
        if params.end_date is not None:
            query["end_date"] = params.end_date.isoformat()
        return self._get_pages(
            f"/indices/stock_market/{self._symbol_path(params.symbol)}/historical_data/level",
            query,
            "historical_data",
            ctx,
        )

    def transform_data(
        self, raw: JsonRows, params: _IndexHistoricalQuery
    ) -> list[IntrinioIndexHistorical]:
        """Map index levels into the typed historical row contract."""
        return [
            IntrinioIndexHistorical.model_validate(
                {
                    "symbol": params.symbol,
                    "date": row.get("date"),
                    "open": None,
                    "high": None,
                    "low": None,
                    "close": row.get("value"),
                    "volume": None,
                }
            )
            for row in self._rows(raw, "historical_data")
        ]


class IntrinioEquityQuoteFetcher(_IntrinioMarketFetcher[_EquityQuoteQuery, JsonObject]):
    """Fetch the latest equity quote and its security metadata."""

    canonical_model = "EquityQuote"
    query_model = _EquityQuoteQuery
    #: ``contract_model("intrinio_equity_quote")`` prints
    #: ``opendata.data.models.intrinio_market_clean.IntrinioEquityQuote``.
    row_model = IntrinioEquityQuote
    capability = _capability("equity", "equity_quote", "snapshot")

    def extract_data(self, params: _EquityQuoteQuery, ctx: FetchContext) -> JsonObject:
        """Fetch the latest quote using supported source filters."""
        query: dict[str, str] = {}
        if params.quote_source is not None:
            if params.quote_source not in _QUOTE_SOURCES:
                raise ValueError(
                    "quote_source is not in the documented OpenBB/Intrinio intersection"
                )
            query["source"] = params.quote_source
        if params.active_only is not None:
            query["active_only"] = str(params.active_only).lower()
        return self._get_json(
            f"/securities/{self._symbol_path(params.symbol)}/quote",
            query or None,
            ctx,
        )

    def transform_data(
        self, raw: JsonObject, params: _EquityQuoteQuery
    ) -> list[IntrinioEquityQuote]:
        """Map an offset-aware quote response into one typed row."""
        if not isinstance(raw, dict):
            raise IntrinioResponseShapeError("quote response must be a JSON object")
        security = raw.get("security")
        if not isinstance(security, dict):
            raise IntrinioResponseShapeError("quote response must include security metadata")
        security = _json_object(security, "quote response must include security metadata")
        last_time = raw.get("last_time")
        if not isinstance(last_time, str):
            raise IntrinioResponseShapeError("quote response must include last_time")
        parsed_time = datetime.fromisoformat(last_time.replace("Z", "+00:00"))
        if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
            raise IntrinioResponseShapeError("quote last_time must be an offset-aware timestamp")
        record = {
            "symbol": security.get("ticker") or params.symbol,
            "asset_type": None,
            "name": security.get("name"),
            "exchange": security.get("exchange"),
            "bid": None,
            "bid_size": None,
            "ask": None,
            "ask_size": None,
            "last_price": raw.get("last"),
            "updated_on": parsed_time,
            "open": raw.get("open"),
            "high": raw.get("high"),
            "low": raw.get("low"),
            "close": raw.get("previous_close"),
            "volume": raw.get("market_volume"),
            "exchange_volume": raw.get("exchange_volume"),
            "prev_close": raw.get("previous_close"),
            "change": raw.get("change"),
            "change_percent": raw.get("change_percent"),
            "year_high": raw.get("eod_fifty_two_week_high"),
            "year_low": raw.get("eod_fifty_two_week_low"),
            "source": raw.get("source"),
            "security": security,
        }
        return [IntrinioEquityQuote.model_validate(record)]


class IntrinioEtfHoldingsFetcher(_IntrinioMarketFetcher[_EtfHoldingsQuery, JsonRows]):
    """Fetch and normalize an ETF's holdings rows."""

    canonical_model = "EtfHoldings"
    row_model = IntrinioEtfHolding
    query_model = _EtfHoldingsQuery
    capability = _capability("etf", "etf_holdings", "snapshot")

    def extract_data(self, params: _EtfHoldingsQuery, ctx: FetchContext) -> JsonRows:
        """Fetch paginated holdings for the requested as-of date."""
        query: dict[str, str] = {}
        if params.date is not None:
            query["as_of_date"] = params.date.isoformat()
        return self._get_pages(
            f"/etfs/{self._symbol_path(params.symbol)}/holdings", query, "holdings", ctx
        )

    def transform_data(self, raw: JsonRows, params: _EtfHoldingsQuery) -> list[IntrinioEtfHolding]:
        """Map holdings and normalize documented fractional weights."""
        results: list[IntrinioEtfHolding] = []
        for row in self._rows(raw, "holdings"):
            weighting = _percent_fraction(row.get("weighting"))
            # Intrinio documents weighting as a fraction; OpenBB publishes a normalized percent.
            weight = None if weighting is None else weighting * 100.0
            results.append(
                IntrinioEtfHolding.model_validate(
                    {
                        "symbol": row.get("ticker"),
                        "name": row.get("name"),
                        "security_type": row.get("type"),
                        "isin": row.get("isin"),
                        "ric": row.get("ric"),
                        "sedol": row.get("sedol"),
                        "share_class_figi": row.get("share_class_figi"),
                        "maturity_date": row.get("maturity"),
                        "contract_expiry_date": row.get("contract_expiry_date"),
                        "coupon": row.get("coupon"),
                        "balance": row.get("quantity_held"),
                        "unit": row.get("quantity_units"),
                        "units_per_share": row.get("quantity_per_share"),
                        "face_value": row.get("face"),
                        "derivatives_value": row.get("notional_value"),
                        "value": row.get("market_value_held"),
                        "weight": weight,
                        "updated": row.get("as_of_date"),
                    }
                )
            )
        return results


class IntrinioEtfSearchFetcher(_IntrinioMarketFetcher[_EtfSearchQuery, JsonRows]):
    """Search for ETFs by the documented prefix mode."""

    canonical_model = "EtfSearch"
    query_model = _EtfSearchQuery
    #: ``contract_model("intrinio_etf_search")`` prints
    #: ``opendata.data.models.intrinio_market_clean.IntrinioEtfSearch``.
    row_model = IntrinioEtfSearch
    capability = _capability("etf", "etf_search", "snapshot")

    def transform_query(self, **kwargs: object) -> _EtfSearchQuery:
        """Validate and normalize the ETF search query."""
        params = self.query_model.model_validate(kwargs)
        if not params.query.strip():
            raise ValueError("query must not be empty")
        if params.start_date is not None or params.end_date is not None:
            raise ValueError("date window is not supported for ETF search")
        return params.model_copy(update={"query": params.query.strip()})

    def extract_data(self, params: _EtfSearchQuery, ctx: FetchContext) -> JsonRows:
        """Fetch all pages of matching ETFs."""
        return self._get_pages(
            "/etfs/search",
            {"query": params.query, "mode": params.mode},
            "etfs",
            ctx,
        )

    def transform_data(self, raw: JsonRows, params: _EtfSearchQuery) -> list[IntrinioEtfSearch]:
        """Map ETF search results into typed rows."""
        return [
            IntrinioEtfSearch.model_validate(
                {
                    "symbol": row.get("ticker"),
                    "name": row.get("name"),
                    "exchange": row.get("exchange_mic"),
                    "figi_ticker": row.get("figi_ticker"),
                    "ric": row.get("ric"),
                    "isin": row.get("isin"),
                    "sedol": row.get("sedol"),
                    "intrinio_id": row.get("id"),
                }
            )
            for row in self._rows(raw, "etfs")
        ]


class IntrinioOptionsUnusualFetcher(_IntrinioMarketFetcher[_OptionsUnusualQuery, JsonRows]):
    """Fetch and normalize unusual options trades for a symbol."""

    canonical_model = "OptionsUnusual"
    row_model = IntrinioOptionsUnusual
    query_model = _OptionsUnusualQuery
    capability = _capability("option", "options_unusual", "event")

    def extract_data(self, params: _OptionsUnusualQuery, ctx: FetchContext) -> JsonRows:
        """Fetch unusual activity trades from the selected source."""
        payload = self._get_json(
            f"/options/unusual_activity/{self._symbol_path(params.symbol)}",
            {"source": params.option_source},
            ctx,
        )
        rows = payload.get("trades")
        if not isinstance(rows, list):
            raise IntrinioResponseShapeError("unusual activity trades must be an array of objects")
        try:
            return [
                _json_object(row, "unusual activity trades must be an array of objects")
                for row in rows
            ]
        except IntrinioResponseShapeError as exc:
            raise IntrinioResponseShapeError(
                "unusual activity trades must be an array of objects"
            ) from exc

    def transform_data(
        self, raw: JsonRows, params: _OptionsUnusualQuery
    ) -> list[IntrinioOptionsUnusual]:
        """Map unusual trade records into strict typed rows."""
        return [
            IntrinioOptionsUnusual.model_validate(
                {
                    "underlying_symbol": row.get("symbol"),
                    "contract_symbol": row.get("contract"),
                    "trade_timestamp": row.get("timestamp"),
                    "trade_type": row.get("type"),
                    "sentiment": row.get("sentiment"),
                    "bid_at_execution": row.get("bid_at_execution"),
                    "ask_at_execution": row.get("ask_at_execution"),
                    "average_price": row.get("average_price"),
                    "underlying_price_at_execution": row.get("underlying_price_at_execution"),
                    "total_size": row.get("total_size"),
                    "total_value": row.get("total_value"),
                }
            )
            for row in self._rows(raw, "trades")
        ]


class IntrinioOptionsChainsFetcher(_IntrinioMarketFetcher[_OptionsChainQuery, JsonRows]):
    """Fetch documented real-time option-chain rows for a ticker."""

    canonical_model = "OptionsChains"
    row_model = IntrinioOptionsChain
    query_model = _OptionsChainQuery
    capability = _capability("option", "options_chains", "snapshot")

    def transform_query(self, **kwargs: object) -> _OptionsChainQuery:
        """Validate option filters and reject unsupported history requests."""
        params = self.query_model.model_validate(kwargs)
        self._validate_standard_market_query(params)
        if params.requested_date is not None:
            raise ValueError(
                "historical option chains are not supported by the documented realtime endpoint"
            )
        return params

    def extract_data(self, params: _OptionsChainQuery, ctx: FetchContext) -> JsonRows:
        """Fetch option contracts and their documented nested data."""
        query: dict[str, str] = {
            "source": params.option_source,
            "page_size": str(_PAGE_SIZE),
            "show_stats": str(params.show_stats).lower(),
            "show_extended_price": str(params.show_extended_price).lower(),
            "include_related_symbols": str(params.include_related_symbols).lower(),
            "stock_price_source": params.stock_price_source,
            "model": params.pricing_model,
        }
        if params.option_type is not None:
            query["type"] = params.option_type
        if params.moneyness is not None:
            query["moneyness"] = params.moneyness
        return self._get_pages(
            f"/options/prices/by_ticker/{self._symbol_path(params.symbol)}/realtime",
            query,
            "contracts",
            ctx,
        )

    def transform_data(
        self, raw: JsonRows, params: _OptionsChainQuery
    ) -> list[IntrinioOptionsChain]:
        """Map nested contract, price, statistics, and price-history fields."""
        results: list[IntrinioOptionsChain] = []
        for row in self._rows(raw, "contracts"):
            option_value = row.get("option")
            price_value = row.get("price")
            stats_value = row.get("stats")
            extended_value = row.get("extended_price")
            if not isinstance(option_value, dict) or not isinstance(price_value, dict):
                raise IntrinioResponseShapeError(
                    "option contract must include option and price objects"
                )
            if stats_value is not None and not isinstance(stats_value, dict):
                raise IntrinioResponseShapeError("option stats must be a JSON object or null")
            if extended_value is not None and not isinstance(extended_value, dict):
                raise IntrinioResponseShapeError(
                    "extended option prices must be a JSON object or null"
                )
            option = _json_object(
                option_value, "option contract must include option and price objects"
            )
            price = _json_object(
                price_value, "option contract must include option and price objects"
            )
            stats = (
                {}
                if stats_value is None
                else _json_object(stats_value, "option stats must be a JSON object or null")
            )
            extended = (
                {}
                if extended_value is None
                else _json_object(
                    extended_value, "extended option prices must be a JSON object or null"
                )
            )
            code = option.get("code")
            if not isinstance(code, str) or not code.strip():
                raise IntrinioResponseShapeError("option contract requires a nonempty code")
            results.append(
                IntrinioOptionsChain.model_validate(
                    {
                        "underlying_symbol": option.get("ticker") or params.symbol,
                        "underlying_price": stats.get("underlying_price"),
                        "contract_symbol": code,
                        "eod_date": None,
                        "expiration": option.get("expiration"),
                        "dte": None,
                        "strike": option.get("strike"),
                        "option_type": option.get("type"),
                        "contract_size": None,
                        "open_interest": price.get("open_interest"),
                        "volume": price.get("volume"),
                        "theoretical_price": None,
                        "last_trade_price": price.get("last"),
                        "last_trade_size": price.get("last_size"),
                        "last_trade_time": price.get("last_timestamp"),
                        "tick": None,
                        "bid": price.get("bid"),
                        "bid_size": price.get("bid_size"),
                        "bid_time": price.get("bid_timestamp"),
                        "bid_exchange": None,
                        "ask": price.get("ask"),
                        "ask_size": price.get("ask_size"),
                        "ask_time": price.get("ask_timestamp"),
                        "ask_exchange": None,
                        "mark": (
                            price.get("mark")
                            if price.get("mark") is not None
                            else extended.get("mark")
                        ),
                        "open": extended.get("trade_open"),
                        "open_bid": extended.get("bid_open"),
                        "open_ask": extended.get("ask_open"),
                        "high": extended.get("trade_high"),
                        "bid_high": extended.get("bid_high"),
                        "ask_high": extended.get("ask_high"),
                        "low": extended.get("trade_low"),
                        "bid_low": extended.get("bid_low"),
                        "ask_low": extended.get("ask_low"),
                        "close": extended.get("trade_close"),
                        "close_size": None,
                        "close_time": None,
                        "close_bid": extended.get("bid_close"),
                        "close_bid_size": None,
                        "close_bid_time": None,
                        "close_ask": extended.get("ask_close"),
                        "close_ask_size": None,
                        "close_ask_time": None,
                        "prev_close": None,
                        "change": None,
                        "change_percent": None,
                        "implied_volatility": stats.get("implied_volatility"),
                        "delta": stats.get("delta"),
                        "gamma": stats.get("gamma"),
                        "theta": stats.get("theta"),
                        "vega": stats.get("vega"),
                        "rho": stats.get("rho"),
                    }
                )
            )
        return results


class IntrinioPriceTargetConsensusFetcher(
    _IntrinioMarketFetcher[_PriceTargetConsensusQuery, JsonRows]
):
    """Fetch and normalize Zacks target-price consensus rows."""

    canonical_model = "PriceTargetConsensus"
    row_model = IntrinioPriceTargetConsensus
    query_model = _PriceTargetConsensusQuery
    capability = _capability("equity", "price_target_consensus", "snapshot")

    def extract_data(self, params: _PriceTargetConsensusQuery, ctx: FetchContext) -> JsonRows:
        """Fetch consensus rows for the requested symbol and group."""
        query: dict[str, str] = {"identifier": params.symbol}
        if params.industry_group_number is not None:
            query["industry_group_number"] = str(params.industry_group_number)
        return self._get_pages(
            "/zacks/target_price_consensuses", query, "target_price_consensuses", ctx
        )

    def transform_data(
        self, raw: JsonRows, params: _PriceTargetConsensusQuery
    ) -> list[IntrinioPriceTargetConsensus]:
        """Map consensus fields into the typed public row contract."""
        return [
            IntrinioPriceTargetConsensus.model_validate(
                {
                    "symbol": row.get("ticker"),
                    "name": row.get("company_name"),
                    "target_high": row.get("high"),
                    "target_low": row.get("low"),
                    "target_consensus": row.get("mean"),
                    "target_median": row.get("median"),
                    "standard_deviation": row.get("standard_deviation"),
                    "total_analysts": row.get("total"),
                    "most_recent_date": row.get("most_recent_date"),
                    "raised": row.get("raised"),
                    "lowered": row.get("lowered"),
                    "industry_group_number": _industry_group_number(
                        row.get("industry_group_number")
                    ),
                }
            )
            for row in self._rows(raw, "target_price_consensuses")
        ]


class IntrinioShareStatisticsFetcher(_IntrinioMarketFetcher[_ShareStatisticsQuery, JsonRows]):
    """Fetch share-count observations within an optional date window."""

    canonical_model = "ShareStatistics"
    query_model = _ShareStatisticsQuery
    #: ``contract_model("intrinio_share_statistics")`` prints
    #: ``opendata.data.models.intrinio_market_clean.IntrinioShareStatistics``.
    row_model = IntrinioShareStatistics
    capability = _capability("equity", "share_statistics", "series")

    def extract_data(self, params: _ShareStatisticsQuery, ctx: FetchContext) -> JsonRows:
        """Fetch shares-outstanding rows using inclusive date bounds."""
        query: dict[str, str] = {}
        if params.start_date is not None:
            if params.start_date == date.min:
                raise ValueError("start_date cannot be the minimum representable date")
            query["end_date_greater_than"] = (params.start_date - timedelta(days=1)).isoformat()
        if params.end_date is not None:
            if params.end_date == date.max:
                raise ValueError("end_date cannot be the maximum representable date")
            query["end_date_less_than"] = (params.end_date + timedelta(days=1)).isoformat()
        payload = self._get_json(
            f"/companies/{self._symbol_path(params.symbol)}/shares_outstanding",
            query or None,
            ctx,
        )
        rows = payload.get("shares_outstanding")
        if not isinstance(rows, list):
            raise IntrinioResponseShapeError("shares_outstanding must be an array of objects")
        try:
            return [
                _json_object(row, "shares_outstanding must be an array of objects") for row in rows
            ]
        except IntrinioResponseShapeError as exc:
            raise IntrinioResponseShapeError(
                "shares_outstanding must be an array of objects"
            ) from exc

    def transform_data(
        self, raw: JsonRows, params: _ShareStatisticsQuery
    ) -> list[IntrinioShareStatistics]:
        """Map matching ticker rows into typed share statistics."""
        rows = self._rows(raw, "shares_outstanding")
        output: list[IntrinioShareStatistics] = []
        for row in rows:
            ticker = row.get("trading_symbol")
            if not isinstance(ticker, str) or not ticker.strip():
                raise IntrinioResponseShapeError("shares_outstanding row requires trading_symbol")
            if ticker.casefold() != params.symbol.casefold():
                continue
            output.append(
                IntrinioShareStatistics.model_validate(
                    {
                        "symbol": ticker or params.symbol,
                        "date": row.get("end_date"),
                        "outstanding_shares": row.get("shares_outstanding"),
                        "adjusted_outstanding_shares": row.get("adj_shares_outstanding"),
                    }
                )
            )
        return output


GAP_MODELS: dict[str, str] = {
    "EtfPricePerformance": (
        "Intrinio documents the period return fields but not whether their numeric values are "
        "fractions or normalized percentages; OpenBB requires normalized percentages."
    ),
    "MarketSnapshots": (
        "Intrinio's snapshot response publishes expiring download URLs and file part metadata, "
        "but the public endpoint docs do not specify the CSV schema or download host/auth contract."
    ),
    "OptionsSnapshots": (
        "Intrinio returns expiring file URLs and metadata but does not document the flat file "
        "row schema or download host/auth contract in the endpoint docs."
    ),
}


def _percent_fraction(value: object) -> float | None:
    """Validate a documented fractional weight before converting to percent."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IntrinioResponseShapeError("ETF weighting must be a finite JSON number")
    try:
        finite = math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        finite = False
    if not finite:
        raise IntrinioResponseShapeError("ETF weighting must be a finite JSON number")
    return float(value)


def _industry_group_number(value: object) -> int | None:
    """Convert Intrinio's numeric string to OpenBB's integer output field."""
    if value is None:
        return None
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    raise IntrinioResponseShapeError("industry_group_number must be a non-negative integer")


__all__ = [
    "GAP_MODELS",
    "IntrinioEquityHistoricalFetcher",
    "IntrinioEquityInfoFetcher",
    "IntrinioEquityQuoteFetcher",
    "IntrinioEtfHistoricalFetcher",
    "IntrinioEtfHoldingsFetcher",
    "IntrinioEtfInfoFetcher",
    "IntrinioEtfSearchFetcher",
    "IntrinioHistoricalMarketCapFetcher",
    "IntrinioIndexHistoricalFetcher",
    "IntrinioOptionsUnusualFetcher",
    "IntrinioOptionsChainsFetcher",
    "IntrinioPaginationLimitError",
    "IntrinioPriceTargetConsensusFetcher",
    "IntrinioResponseShapeError",
    "IntrinioShareStatisticsFetcher",
]
