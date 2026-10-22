"""Source-native yfinance fetchers for the Iteration 2 market model set."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import date as date_type
from datetime import datetime, timezone
from numbers import Integral, Real
from typing import ClassVar, Literal, TypeVar, cast

import pandas as pd
from pydantic import field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.yfinance_iter2 import (
    YfinanceCompanyNewsObservation,
    YfinanceHistoricalBar,
    YfinanceKeyExecutiveObservation,
    YfinanceKeyMetricObservation,
    YfinanceOptionChainObservation,
    YfinancePayloadObservation,
    YfinancePriceTargetConsensus,
    YfinanceScreenerObservation,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.yfinance._source import SOURCE
from opendata.data.providers.yfinance.models.iter2 import (
    MAX_YFINANCE_SYMBOL_TOKENS,
    MAX_YFINANCE_SYMBOLS,
    YfinanceMultiSymbolQuery,
    YfinanceSymbolQuery,
    _as_date,
    _is_missing_scalar,
    _source_number,
    _validated_query,
)
from opendata.data.providers.yfinance.models.stock_daily import YfinanceProviderError

UTC = timezone.utc
MAX_EQUITY_HISTORICAL_SYMBOLS = MAX_YFINANCE_SYMBOLS
MAX_EQUITY_HISTORICAL_SYMBOL_TOKENS = MAX_YFINANCE_SYMBOL_TOKENS
YFINANCE_HISTORY_BUDGET_HOST = "query1.finance.yahoo.com"
RawBySymbol = TypeVar("RawBySymbol")
HistoryModel = Literal[
    "CryptoHistorical",
    "CurrencyHistorical",
    "EquityHistorical",
    "EtfHistorical",
    "FuturesHistorical",
    "IndexHistorical",
]
HistoryInterval = Literal[
    "1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1W", "1M", "1Q"
]
_YFINANCE_INTERVALS: dict[str, str] = {
    "1m": "1m",
    "2m": "2m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "60m": "60m",
    "90m": "90m",
    "1h": "1h",
    "1d": "1d",
    "5d": "5d",
    "1W": "1wk",
    "1M": "1mo",
    "1Q": "3mo",
}
_INTRADAY_INTERVALS = frozenset(_YFINANCE_INTERVALS) - {"1d", "5d", "1W", "1M", "1Q"}


class YfinanceHistoricalQuery(YfinanceMultiSymbolQuery):
    """A bounded yfinance history query using the public interval vocabulary."""

    start_date: date_type | None = None
    end_date: date_type | None = None
    interval: HistoryInterval = "1d"

    @model_validator(mode="after")
    def _validate_window(self) -> YfinanceHistoricalQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self


class YfinanceEquityHistoricalQuery(YfinanceHistoricalQuery):
    """Equity bars include the public action, adjustment and extended-hours controls."""

    extended_hours: bool = False
    include_actions: bool = True
    adjustment: Literal["splits_only", "splits_and_dividends"] = "splits_only"


class YfinanceEtfHistoricalQuery(YfinanceEquityHistoricalQuery):
    """ETF history has the same public historical controls as equity history."""


class YfinanceCurrencyHistoricalQuery(YfinanceHistoricalQuery):
    """Normalize ODP pair forms into Yahoo's ``BASEQUOTE=X`` ticker form."""

    currency_pairs: ClassVar[bool] = True


class YfinanceFuturesHistoricalQuery(YfinanceHistoricalQuery):
    """Continuous-futures query; yfinance documents ticker history, not expiry mapping."""

    expiration: str | None = None

    @field_validator("expiration")
    @classmethod
    def _validate_expiration(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) != 7 or value[4] != "-" or not value[:4].isdigit() or not value[5:].isdigit():
            raise ValueError("expiration must use YYYY-MM")
        if not 1 <= int(value[5:]) <= 12:
            raise ValueError("expiration must use YYYY-MM")
        raise ValueError("yfinance does not document a futures expiration-to-ticker mapping")


class YfinanceCurrentSymbolQuery(YfinanceSymbolQuery):
    """A snapshot request cannot accept historical date filters that it would ignore."""

    @model_validator(mode="after")
    def _reject_history_dates(self) -> YfinanceCurrentSymbolQuery:
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("current snapshot queries do not accept historical date filters")
        return self


class YfinanceCurrentMultiSymbolQuery(YfinanceMultiSymbolQuery):
    """Current snapshot query that rejects history filters instead of ignoring them."""

    @model_validator(mode="after")
    def _reject_history_dates(self) -> YfinanceCurrentMultiSymbolQuery:
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("current snapshot queries do not accept historical date filters")
        return self


def _history_rows(
    raw: object, params: YfinanceHistoricalQuery, model_id: HistoryModel, symbol: str
) -> tuple[YfinanceHistoricalBar, ...]:
    if not isinstance(raw, pd.DataFrame):
        raise YfinanceProviderError("YFINANCE_HISTORY_SHAPE_INVALID")
    if raw.empty:
        raise YfinanceProviderError("YFINANCE_EMPTY_RESPONSE")
    required = {"Open", "High", "Low", "Close"}
    if not required.issubset(raw.columns) or raw.columns.has_duplicates or raw.index.has_duplicates:
        raise YfinanceProviderError("YFINANCE_HISTORY_SHAPE_INVALID")
    rows: list[YfinanceHistoricalBar] = []
    for timestamp, values in raw.iterrows():
        session_date = _as_history_date(timestamp, field="session_date", interval=params.interval)
        session_day = session_date.date() if isinstance(session_date, datetime) else session_date
        if params.start_date is not None and session_day < params.start_date:
            continue
        if params.end_date is not None and session_day > params.end_date:
            continue
        value_map = values.to_dict()
        open_price = _source_number(value_map["Open"], allow_missing=False)
        high_price = _source_number(value_map["High"], allow_missing=False)
        low_price = _source_number(value_map["Low"], allow_missing=False)
        close_price = _source_number(value_map["Close"], allow_missing=False)
        open_value = _required_source_number(open_price, code="YFINANCE_BAR_VALUE_MISSING")
        high_value = _required_source_number(high_price, code="YFINANCE_BAR_VALUE_MISSING")
        low_value = _required_source_number(low_price, code="YFINANCE_BAR_VALUE_MISSING")
        close_value = _required_source_number(close_price, code="YFINANCE_BAR_VALUE_MISSING")
        rows.append(
            YfinanceHistoricalBar(
                model_id=model_id,
                symbol=symbol,
                session_date=session_date,
                open=open_value,
                high=high_value,
                low=low_value,
                close=close_value,
                adjusted_close=_optional_source_number(
                    value_map.get(
                        "Adj Close",
                        close_price
                        if getattr(params, "adjustment", None) == "splits_and_dividends"
                        else None,
                    )
                ),
                volume=_optional_source_number(value_map.get("Volume")),
                dividends=(
                    _optional_source_number(value_map.get("Dividends"))
                    if getattr(params, "include_actions", True)
                    else None
                ),
                stock_splits=(
                    _optional_source_number(value_map.get("Stock Splits"))
                    if getattr(params, "include_actions", True)
                    else None
                ),
                interval=params.interval,
                price_adjustment=getattr(params, "adjustment", "not_applicable"),
            )
        )
    if not rows:
        raise YfinanceProviderError("YFINANCE_EMPTY_RESPONSE")
    return tuple(sorted(rows, key=lambda row: row.session_date))


def _as_history_date(value: object, *, field: str, interval: str) -> date_type | datetime:
    """Keep exchange-local timestamp and timezone for intraday yfinance bars."""
    if interval not in _INTRADAY_INTERVALS:
        return _as_date(value, field=field)
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            raise YfinanceProviderError(f"YFINANCE_{field.upper()}_INVALID")
        return cast("datetime", value.to_pydatetime())
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise YfinanceProviderError(f"YFINANCE_{field.upper()}_INVALID") from exc
        if "T" not in value and " " not in value:
            raise YfinanceProviderError(f"YFINANCE_{field.upper()}_INVALID")
        return parsed
    raise YfinanceProviderError(f"YFINANCE_{field.upper()}_INVALID")


def _optional_source_number(value: object) -> float | None:
    if _is_missing_scalar(value):
        return None
    return _source_number(value, allow_missing=False)


def _required_source_number(value: object, *, code: str) -> float:
    number = _source_number(value, allow_missing=False)
    if number is None:
        raise YfinanceProviderError(code)
    return number


HistoryRaw = tuple[tuple[str, pd.DataFrame | None], ...]


def _fetch_by_symbol(
    symbols: tuple[str, ...], fetch_one: Callable[[str], RawBySymbol]
) -> tuple[tuple[str, RawBySymbol], ...]:
    """Run bounded, budgeted per-symbol SDK calls in normalized request order."""
    results = [(symbol, fetch_one(symbol)) for symbol in symbols]
    return tuple(results)


class YfinanceHistoryFetcher(Fetcher[YfinanceHistoricalQuery, HistoryRaw]):
    """Common bounded-thread adapter for documented Ticker.history intervals."""

    async_mode = "bounded_thread"
    model_id: ClassVar[HistoryModel]
    canonical_model: ClassVar[str]
    domain: ClassVar[str]
    query_type: ClassVar[type[YfinanceHistoricalQuery]] = YfinanceHistoricalQuery

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        cls.capability = Capability(
            asset_class="market_data",
            domain=cls.domain,
            period="1d",
            market="global",
            source=SOURCE,
            verified=False,
        )

    def transform_query(self, **kwargs: object) -> YfinanceHistoricalQuery:
        """Validate keyword arguments into ``query_type`` without reaching the SDK."""
        return self.query_type.model_validate(kwargs)

    def extract_data(self, params: YfinanceHistoricalQuery, ctx: FetchContext) -> HistoryRaw:
        """Fetch one history payload per validated symbol through the SDK seam."""
        from opendata.data.providers.yfinance.models import _sdk

        query = _validated_query(params, self.query_type)
        return _fetch_by_symbol(
            query.symbols,
            lambda symbol: _sdk.require_history_snapshot(
                symbol,
                query.start_date,
                query.end_date,
                timeout=ctx.timeout,
                interval=_YFINANCE_INTERVALS[query.interval],
                prepost=getattr(query, "extended_hours", False),
                actions=getattr(query, "include_actions", True),
                auto_adjust=getattr(query, "adjustment", None) == "splits_and_dividends",
            ),
        )

    def transform_data(self, raw: HistoryRaw, params: YfinanceHistoricalQuery) -> FetchResult:
        """Convert per-symbol history into contract bars."""
        query = _validated_query(params, self.query_type)
        if not isinstance(raw, tuple) or len(raw) != len(query.symbols):
            raise YfinanceProviderError("YFINANCE_HISTORY_SHAPE_INVALID")
        rows: list[YfinanceHistoricalBar] = []
        for expected_symbol, entry in zip(query.symbols, raw, strict=True):
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or entry[0] != expected_symbol
                or (entry[1] is not None and not isinstance(entry[1], pd.DataFrame))
            ):
                raise YfinanceProviderError("YFINANCE_HISTORY_SYMBOL_MISMATCH")
            symbol_query = query.model_copy(update={"symbol": expected_symbol})
            symbol_rows = _history_rows(entry[1], symbol_query, self.model_id, expected_symbol)
            if any(row.symbol != expected_symbol for row in symbol_rows):
                raise YfinanceProviderError("YFINANCE_HISTORY_SYMBOL_MISMATCH")
            rows.extend(symbol_rows)
        if not rows:
            raise YfinanceProviderError("YFINANCE_EMPTY_RESPONSE")
        return tuple(sorted(rows, key=lambda row: row.session_date))


class YfinanceCryptoHistoricalFetcher(YfinanceHistoryFetcher):
    """Fetch bounded OHLCV history for crypto ticker symbols."""

    model_id = "CryptoHistorical"
    canonical_model = "CryptoHistorical"
    domain = "yfinance_crypto_historical"
    row_model: ClassVar[type[YfinanceHistoricalBar]] = YfinanceHistoricalBar


class YfinanceCurrencyHistoricalFetcher(YfinanceHistoryFetcher):
    """Fetch bounded OHLCV history for normalized currency-pair symbols."""

    model_id = "CurrencyHistorical"
    canonical_model = "CurrencyHistorical"
    domain = "yfinance_currency_historical"
    row_model: ClassVar[type[YfinanceHistoricalBar]] = YfinanceHistoricalBar
    query_type = YfinanceCurrencyHistoricalQuery


class YfinanceEquityHistoricalFetcher(YfinanceHistoryFetcher):
    """Fetch bounded historical OHLCV observations for equities."""

    model_id = "EquityHistorical"
    canonical_model = "EquityHistorical"
    domain = "yfinance_equity_historical"
    row_model: ClassVar[type[YfinanceHistoricalBar]] = YfinanceHistoricalBar
    query_type = YfinanceEquityHistoricalQuery


class YfinanceEtfHistoricalFetcher(YfinanceHistoryFetcher):
    """Fetch bounded historical OHLCV observations for ETFs."""

    model_id = "EtfHistorical"
    canonical_model = "EtfHistorical"
    domain = "yfinance_etf_historical"
    row_model: ClassVar[type[YfinanceHistoricalBar]] = YfinanceHistoricalBar
    query_type = YfinanceEtfHistoricalQuery


class YfinanceFuturesHistoricalFetcher(YfinanceHistoryFetcher):
    """Fetch continuous futures history when no unsupported expiry is requested."""

    model_id = "FuturesHistorical"
    canonical_model = "FuturesHistorical"
    domain = "yfinance_futures_historical"
    row_model: ClassVar[type[YfinanceHistoricalBar]] = YfinanceHistoricalBar
    query_type = YfinanceFuturesHistoricalQuery


class YfinanceIndexHistoricalFetcher(YfinanceHistoryFetcher):
    """Fetch bounded historical OHLCV observations for index tickers."""

    model_id = "IndexHistorical"
    canonical_model = "IndexHistorical"
    domain = "yfinance_index_historical"
    row_model: ClassVar[type[YfinanceHistoricalBar]] = YfinanceHistoricalBar


def _json_safe(value: object) -> object:
    """Convert SDK data into strict JSON while representing source gaps as null."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if pd.notna(value) else None
    if isinstance(value, (date_type, datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, pd.DataFrame):
        return {
            "columns": [_json_safe(column) for column in value.columns.tolist()],
            "index": [_json_safe(index) for index in value.index.tolist()],
            "records": [_json_safe(record) for record in value.to_dict("records")],
        }
    if isinstance(value, pd.Series):
        return {
            "name": _json_safe(value.name),
            "index": [_json_safe(index) for index in value.index.tolist()],
            "values": [_json_safe(item) for item in value.tolist()],
        }
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise YfinanceProviderError("YFINANCE_PAYLOAD_SHAPE_INVALID")
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_safe(item())
        except (TypeError, ValueError, OverflowError) as exc:
            raise YfinanceProviderError("YFINANCE_PAYLOAD_SHAPE_INVALID") from exc
    raise YfinanceProviderError("YFINANCE_PAYLOAD_SHAPE_INVALID")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            _json_safe(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise YfinanceProviderError("YFINANCE_PAYLOAD_SHAPE_INVALID") from exc


def _payload_row(
    model_id: str, symbol: str, payload: object, ordinal: int = 0
) -> YfinancePayloadObservation:
    return YfinancePayloadObservation(
        model_id=model_id,
        symbol=symbol,
        ordinal=ordinal,
        retrieved_at_utc=datetime.now(UTC),
        raw_payload_json=_canonical_json(payload),
    )


class YfinanceNewsQuery(YfinanceMultiSymbolQuery):
    """News query accepting either ``count`` or the legacy ``limit`` alias."""

    limit: int | None = 10
    count: int | None = None

    @field_validator("limit")
    @classmethod
    def _validate_limit(cls, value: int | None) -> int | None:
        if value is not None and (isinstance(value, bool) or not 1 <= value <= 100):
            raise ValueError("limit must be between 1 and 100 or None")
        return value

    @field_validator("count")
    @classmethod
    def _validate_legacy_count(cls, value: int | None) -> int | None:
        if value is not None and (isinstance(value, bool) or not 1 <= value <= 100):
            raise ValueError("count must be between 1 and 100")
        return value

    @property
    def effective_count(self) -> int:
        """Return the row count implied by ``count``, then ``limit``, then the default of 100."""
        if self.count is not None:
            return self.count
        return self.limit if self.limit is not None else 100

    @model_validator(mode="after")
    def _validate_window(self) -> YfinanceNewsQuery:
        if (
            "limit" in self.model_fields_set
            and "count" in self.model_fields_set
            and self.count is not None
            and self.count != self.limit
        ):
            raise ValueError("limit and legacy count must not conflict")
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("yfinance get_news does not support a historical publish-date window")
        return self


def _news_date(item: Mapping[str, object]) -> datetime | None:
    content = item.get("content")
    record = content if isinstance(content, Mapping) else item
    for name in ("providerPublishTime", "pubDate", "published_at", "publishedAt"):
        value = record.get(name)
        if isinstance(value, Real) and not isinstance(value, bool):
            try:
                return datetime.fromtimestamp(float(value), tz=UTC)
            except (OverflowError, OSError, ValueError) as exc:
                raise YfinanceProviderError("YFINANCE_NEWS_DATE_INVALID") from exc
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise YfinanceProviderError("YFINANCE_NEWS_DATE_INVALID") from exc
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


NewsRaw = tuple[tuple[str, list[dict[str, object]]], ...]


class YfinanceCompanyNewsFetcher(Fetcher[YfinanceNewsQuery, NewsRaw]):
    """Fetch and normalize the newest bounded news items across symbols."""

    async_mode = "bounded_thread"
    canonical_model = "CompanyNews"
    row_model: ClassVar[type[YfinanceCompanyNewsObservation]] = YfinanceCompanyNewsObservation
    capability = Capability(
        asset_class="equity",
        domain="yfinance_company_news",
        period="event",
        market="global",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> YfinanceNewsQuery:
        """Validate a public company-news query."""
        return YfinanceNewsQuery.model_validate(kwargs)

    def extract_data(self, params: YfinanceNewsQuery, ctx: FetchContext) -> NewsRaw:
        """Fetch bounded per-symbol news candidates through the lazy SDK seam."""
        from opendata.data.providers.yfinance.models import _sdk

        query = _validated_query(params, YfinanceNewsQuery)
        return _fetch_by_symbol(
            query.symbols,
            lambda symbol: _sdk.require_news(symbol, count=query.effective_count),
        )

    def transform_data(self, raw: NewsRaw, params: YfinanceNewsQuery) -> FetchResult:
        """Validate attribution, parse dates, and apply the query-wide limit."""
        query = _validated_query(params, YfinanceNewsQuery)
        if not isinstance(raw, tuple) or len(raw) != len(query.symbols):
            raise YfinanceProviderError("YFINANCE_NEWS_SHAPE_INVALID")
        rows: list[YfinanceCompanyNewsObservation] = []
        for expected_symbol, entry in zip(query.symbols, raw, strict=True):
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or entry[0] != expected_symbol
                or not isinstance(entry[1], list)
                or any(not isinstance(item, Mapping) for item in entry[1])
            ):
                raise YfinanceProviderError("YFINANCE_NEWS_SYMBOL_MISMATCH")
            for ordinal, item in enumerate(entry[1]):
                published_at = _news_date(item)
                if published_at is None:
                    raise YfinanceProviderError("YFINANCE_NEWS_DATE_MISSING")
                rows.append(
                    YfinanceCompanyNewsObservation(
                        symbol=expected_symbol,
                        ordinal=ordinal,
                        published_at_utc=published_at,
                        raw_payload_json=_canonical_json(item),
                    )
                )
        # ``limit`` is query-wide. Ask each ticker for enough candidates, then
        # retain only the newest requested items across the merged response.
        rows.sort(key=lambda row: (-row.published_at_utc.timestamp(), row.symbol, row.ordinal))
        return tuple(rows[: query.effective_count])


PayloadRaw = tuple[tuple[str, object], ...]


class YfinancePayloadFetcher(Fetcher[YfinanceCurrentMultiSymbolQuery, PayloadRaw]):
    """Common current-snapshot output; raw SDK fields are retained as canonical JSON."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str]
    model_id: ClassVar[str]
    domain: ClassVar[str]

    def _fetch_one(self, symbol: str, ctx: FetchContext) -> object:
        raise NotImplementedError

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        cls.capability = Capability(
            asset_class="security",
            domain=cls.domain,
            period="snapshot",
            market="global",
            source=SOURCE,
            verified=False,
        )

    def transform_query(self, **kwargs: object) -> YfinanceCurrentMultiSymbolQuery:
        """Validate keyword arguments into ``YfinanceCurrentMultiSymbolQuery``."""
        return YfinanceCurrentMultiSymbolQuery.model_validate(kwargs)

    def extract_data(
        self, params: YfinanceCurrentMultiSymbolQuery, ctx: FetchContext
    ) -> PayloadRaw:
        """Fetch one current payload per validated symbol."""
        query = _validated_query(params, YfinanceCurrentMultiSymbolQuery)
        return _fetch_by_symbol(query.symbols, lambda symbol: self._fetch_one(symbol, ctx))

    def transform_data(
        self, raw: PayloadRaw, params: YfinanceCurrentMultiSymbolQuery
    ) -> FetchResult:
        """Convert per-symbol payloads into contract rows."""
        query = _validated_query(params, YfinanceCurrentMultiSymbolQuery)
        if not isinstance(raw, tuple) or len(raw) != len(query.symbols):
            raise YfinanceProviderError("YFINANCE_PAYLOAD_SHAPE_INVALID")
        rows: list[YfinancePayloadObservation] = []
        for expected_symbol, entry in zip(query.symbols, raw, strict=True):
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or entry[0] != expected_symbol
                or not isinstance(entry[1], Mapping)
                or not entry[1]
            ):
                raise YfinanceProviderError("YFINANCE_PAYLOAD_SYMBOL_MISMATCH")
            returned_symbol = entry[1].get("symbol")
            if returned_symbol is not None and (
                not isinstance(returned_symbol, str)
                or returned_symbol.strip().upper() != expected_symbol
            ):
                raise YfinanceProviderError("YFINANCE_PAYLOAD_SYMBOL_MISMATCH")
            rows.append(_payload_row(self.model_id, expected_symbol, entry[1]))
        return tuple(rows)


class YfinanceEquityInfoFetcher(YfinancePayloadFetcher):
    """Fetch complete current company information for equity tickers."""

    model_id = "EquityInfo"
    canonical_model = "EquityInfo"
    domain = "yfinance_equity_info"
    row_model: ClassVar[type[YfinancePayloadObservation]] = YfinancePayloadObservation

    def _fetch_one(self, symbol: str, ctx: FetchContext) -> object:
        from opendata.data.providers.yfinance.models import _sdk

        return _sdk.require_info(symbol)


class YfinanceEquityQuoteFetcher(YfinancePayloadFetcher):
    """Fetch the current fast-info quote snapshot for equity tickers."""

    model_id = "EquityQuote"
    canonical_model = "EquityQuote"
    domain = "yfinance_equity_quote"
    row_model: ClassVar[type[YfinancePayloadObservation]] = YfinancePayloadObservation

    def _fetch_one(self, symbol: str, ctx: FetchContext) -> object:
        from opendata.data.providers.yfinance.models import _sdk

        return _sdk.require_fast_info(symbol)


class YfinanceEtfInfoFetcher(YfinancePayloadFetcher):
    """Fetch documented fund metadata and holdings sections for ETF tickers."""

    model_id = "EtfInfo"
    canonical_model = "EtfInfo"
    domain = "yfinance_etf_info"
    row_model: ClassVar[type[YfinancePayloadObservation]] = YfinancePayloadObservation

    def _fetch_one(self, symbol: str, ctx: FetchContext) -> object:
        from opendata.data.providers.yfinance.models import _sdk

        return _sdk.require_fund_sections(symbol)


ExecutivesRaw = tuple[str, list[dict[str, object]]]


def _executive_required_text(record: Mapping[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise YfinanceProviderError("YFINANCE_EXECUTIVES_REQUIRED_FIELD_MISSING")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise YfinanceProviderError("YFINANCE_EXECUTIVES_REQUIRED_FIELD_INVALID")
    return value


def _executive_optional_int(record: Mapping[str, object], key: str) -> int | None:
    value = record.get(key)
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise YfinanceProviderError("YFINANCE_EXECUTIVES_INTEGER_INVALID")
    return int(value)


class YfinanceKeyExecutivesFetcher(Fetcher[YfinanceCurrentSymbolQuery, ExecutivesRaw]):
    """Fetch company officer records from the current ticker information."""

    async_mode = "bounded_thread"
    model_id = "KeyExecutives"
    canonical_model = "KeyExecutives"
    domain = "yfinance_key_executives"
    row_model: ClassVar[type[YfinanceKeyExecutiveObservation]] = YfinanceKeyExecutiveObservation

    capability: ClassVar[Capability] = Capability(
        asset_class="security",
        domain="yfinance_key_executives",
        period="snapshot",
        market="global",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> YfinanceCurrentSymbolQuery:
        """Validate the single-symbol key-executives query."""
        return YfinanceCurrentSymbolQuery.model_validate(kwargs)

    def extract_data(self, params: YfinanceCurrentSymbolQuery, ctx: FetchContext) -> ExecutivesRaw:
        """Fetch and validate the source's company-officer list."""
        from opendata.data.providers.yfinance.models import _sdk

        query = _validated_query(params, YfinanceCurrentSymbolQuery)
        info = _sdk.require_info(query.symbol)
        if not isinstance(info, Mapping):
            raise YfinanceProviderError("YFINANCE_EXECUTIVES_SHAPE_INVALID")
        returned_symbol = info.get("symbol")
        if returned_symbol is not None and (
            not isinstance(returned_symbol, str) or returned_symbol.strip().upper() != query.symbol
        ):
            raise YfinanceProviderError("YFINANCE_EXECUTIVES_SYMBOL_MISMATCH")
        officers = info.get("companyOfficers")
        if not isinstance(officers, list) or any(
            not isinstance(item, Mapping) or any(not isinstance(key, str) for key in item)
            for item in officers
        ):
            raise YfinanceProviderError("YFINANCE_EXECUTIVES_SHAPE_INVALID")
        return query.symbol, [dict(item) for item in officers]

    def transform_data(self, raw: ExecutivesRaw, params: YfinanceSymbolQuery) -> FetchResult:
        """Map the sampled SDK officer aliases to the public named fields."""
        query = _validated_query(params, YfinanceCurrentSymbolQuery)
        if (
            not isinstance(raw, tuple)
            or len(raw) != 2
            or raw[0] != query.symbol
            or not isinstance(raw[1], list)
            or any(not isinstance(item, Mapping) for item in raw[1])
        ):
            raise YfinanceProviderError("YFINANCE_EXECUTIVES_SHAPE_INVALID")
        if not raw[1]:
            raise YfinanceProviderError("YFINANCE_EMPTY_RESPONSE")
        observations: list[YfinanceKeyExecutiveObservation] = []
        for ordinal, item in enumerate(raw[1]):
            observations.append(
                YfinanceKeyExecutiveObservation(
                    symbol=query.symbol,
                    title=_executive_required_text(item, "title"),
                    name=_executive_required_text(item, "name"),
                    pay=_executive_optional_int(item, "totalPay"),
                    currency_pay=None,
                    gender=None,
                    year_born=_executive_optional_int(item, "yearBorn"),
                    exercised_value=_executive_optional_int(item, "exercisedValue"),
                    unexercised_value=_executive_optional_int(item, "unexercisedValue"),
                    fiscal_year=_executive_optional_int(item, "fiscalYear"),
                    ordinal=ordinal,
                    retrieved_at_utc=datetime.now(UTC),
                    raw_payload_json=_canonical_json(item),
                )
            )
        return tuple(observations)


class YfinanceShareStatisticsFetcher(YfinancePayloadFetcher):
    """Fetch current share-count and short-interest statistics."""

    model_id = "ShareStatistics"
    canonical_model = "ShareStatistics"
    domain = "yfinance_share_statistics"
    row_model: ClassVar[type[YfinancePayloadObservation]] = YfinancePayloadObservation
    SHARE_FIELDS = frozenset(
        {
            "sharesOutstanding",
            "impliedSharesOutstanding",
            "floatShares",
            "sharesShort",
            "sharesShortPriorMonth",
            "sharesShortPreviousMonthDate",
            "sharesPercentSharesOut",
            "heldPercentInsiders",
            "heldPercentInstitutions",
            "shortRatio",
            "shortPercentOfFloat",
        }
    )

    def _fetch_one(self, symbol: str, ctx: FetchContext) -> object:
        from opendata.data.providers.yfinance.models import _sdk

        info = _sdk.require_info(symbol)
        shares = {key: value for key, value in info.items() if key in self.SHARE_FIELDS}
        if not shares:
            raise YfinanceProviderError("YFINANCE_SHARE_STATISTICS_MISSING")
        return shares


class YfinanceScreenLimitQuery(QueryParams):
    """Query base carrying only the screener ``limit``, default 200."""

    limit: int | None = 200

    @field_validator("limit")
    @classmethod
    def _validate_limit(cls, value: int | None) -> int | None:
        if value is not None and (isinstance(value, bool) or not 0 <= value <= 5000):
            raise ValueError("limit must be between 0 and 5000 or None")
        return value

    @model_validator(mode="after")
    def _reject_history_dates(self) -> YfinanceScreenLimitQuery:
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("screener snapshots do not accept historical date filters")
        return self


class YfinanceScreenerQuery(YfinanceScreenLimitQuery):
    """Screener query adding sort direction to the limit base."""

    sort: Literal["asc", "desc"] = "desc"


_EQUITY_OPERATORS = frozenset({"eq", "is-in", "btwn", "gt", "lt", "gte", "lte", "and", "or"})

_YFINANCE_REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "ae": ("are", "united arab emirates", "uae"),
    "ar": ("arg", "argentina"),
    "at": ("aut", "austria"),
    "au": ("aus", "australia"),
    "be": ("bel", "belgium"),
    "br": ("bra", "brazil"),
    "ca": ("can", "canada"),
    "ch": ("che", "switzerland"),
    "cl": ("chl", "chile"),
    "cn": ("chn", "china"),
    "co": ("col", "colombia"),
    "cz": ("cze", "czechia", "czech republic"),
    "de": ("deu", "germany"),
    "dk": ("dnk", "denmark"),
    "ee": ("est", "estonia"),
    "eg": ("egy", "egypt"),
    "es": ("esp", "spain"),
    "fi": ("fin", "finland"),
    "fr": ("fra", "france"),
    "gb": ("gbr", "united kingdom", "uk"),
    "gr": ("grc", "greece"),
    "hk": ("hkg", "hong kong"),
    "hu": ("hun", "hungary"),
    "id": ("idn", "indonesia"),
    "ie": ("irl", "ireland"),
    "il": ("isr", "israel"),
    "in": ("ind", "india"),
    "is": ("isl", "iceland"),
    "it": ("ita", "italy"),
    "jp": ("jpn", "japan"),
    "kr": ("kor", "south korea", "korea, republic of"),
    "kw": ("kwt", "kuwait"),
    "lk": ("lka", "sri lanka"),
    "lt": ("ltu", "lithuania"),
    "lv": ("lva", "latvia"),
    "mx": ("mex", "mexico"),
    "my": ("mys", "malaysia"),
    "nl": ("nld", "netherlands", "the netherlands"),
    "no": ("nor", "norway"),
    "nz": ("nzl", "new zealand"),
    "pe": ("per", "peru"),
    "ph": ("phl", "philippines"),
    "pk": ("pak", "pakistan"),
    "pt": ("prt", "portugal"),
    "qa": ("qat", "qatar"),
    "ro": ("rou", "romania"),
    "ru": ("rus", "russia"),
    "sa": ("sau", "saudi arabia"),
    "se": ("swe", "sweden"),
    "sg": ("sgp", "singapore"),
    "sr": ("sur", "suriname"),
    "th": ("tha", "thailand"),
    "tw": ("twn", "taiwan"),
    "us": ("usa", "united states", "united states of america"),
    "ve": ("ven", "venezuela"),
    "vn": ("vnm", "vietnam"),
    "za": ("zaf", "south africa"),
}
_YFINANCE_EXCHANGES = frozenset(
    (
        "DFM",
        "BUE",
        "VIE",
        "ASX",
        "CXA",
        "BRU",
        "SAO",
        "CNQ",
        "NEO",
        "TOR",
        "VAN",
        "EBS",
        "SGO",
        "SHH",
        "SHZ",
        "BVC",
        "PRA",
        "BER",
        "DUS",
        "EUX",
        "FRA",
        "GER",
        "HAM",
        "HAN",
        "MUN",
        "STU",
        "CPH",
        "TAL",
        "CAI",
        "MAD",
        "MCE",
        "HEL",
        "ENX",
        "PAR",
        "AQS",
        "CXE",
        "IOB",
        "LSE",
        "ATH",
        "HKG",
        "BUD",
        "JKT",
        "ISE",
        "TLV",
        "BSE",
        "NSI",
        "MDD",
        "MIL",
        "TLO",
        "FKA",
        "JPX",
        "OSA",
        "SAP",
        "KOE",
        "KSC",
        "KUW",
        "CSE",
        "LIT",
        "RIS",
        "MEX",
        "KLS",
        "AMS",
        "DXE",
        "OSL",
        "NZE",
        "PHP",
        "PHS",
        "KAR",
        "WSE",
        "LIS",
        "DOH",
        "BVB",
        "MCX",
        "SAU",
        "STO",
        "SES",
        "SET",
        "IST",
        "TAI",
        "TWO",
        "JNB",
        "VSE",
        "ASE",
        "BTS",
        "CXI",
        "NAE",
        "NCM",
        "NGM",
        "NMS",
        "NYQ",
        "OEM",
        "OQB",
        "OQX",
        "PCX",
        "PNK",
        "YHD",
    )
)
_YFINANCE_SECTORS = {
    "basic_materials": "Basic Materials",
    "communication_services": "Communication Services",
    "consumer_cyclical": "Consumer Cyclical",
    "consumer_defensive": "Consumer Defensive",
    "energy": "Energy",
    "financial_services": "Financial Services",
    "healthcare": "Healthcare",
    "industrials": "Industrials",
    "real_estate": "Real Estate",
    "technology": "Technology",
    "utilities": "Utilities",
}


def _normalize_country(value: str) -> str:
    normalized = " ".join(value.strip().lower().replace("_", " ").split())
    if normalized == "all":
        return "all"
    if normalized in _YFINANCE_REGION_ALIASES:
        return normalized
    for region, aliases in _YFINANCE_REGION_ALIASES.items():
        if normalized in aliases:
            return region
    raise ValueError("country must map to a yfinance-supported region")


def _normalize_exchange(value: str | None) -> str | None:
    if value is None:
        return None
    code = value.strip().upper()
    if code not in _YFINANCE_EXCHANGES:
        raise ValueError("exchange must be a yfinance EquityQuery exchange code")
    return code


def _validate_equity_filter(node: object, *, depth: int = 0) -> dict[str, object]:
    if depth > 8 or not isinstance(node, Mapping) or set(node) != {"operator", "operands"}:
        raise ValueError("custom filter must be a bounded operator/operands tree")
    operator, operands = node["operator"], node["operands"]
    if not isinstance(operator, str) or operator.lower() not in _EQUITY_OPERATORS:
        raise ValueError("unsupported yfinance EquityQuery operator")
    operator = operator.lower()
    if not isinstance(operands, list) or not operands:
        raise ValueError("filter operands must be a nonempty list")
    if operator in {"and", "or"}:
        if len(operands) < 2:
            raise ValueError("logical filters require at least two child filters")
        return {
            "operator": operator,
            "operands": [_validate_equity_filter(item, depth=depth + 1) for item in operands],
        }
    if not isinstance(operands[0], str) or not operands[0].strip():
        raise ValueError("comparison filter must begin with a field name")
    if operator == "btwn":
        if len(operands) != 3 or any(
            isinstance(item, bool) or not isinstance(item, Real) for item in operands[1:]
        ):
            raise ValueError("btwn requires a field and two numeric bounds")
    elif operator == "is-in":
        if len(operands) < 2 or any(isinstance(item, (dict, list, bool)) for item in operands[1:]):
            raise ValueError("is-in requires a field and scalar values")
    elif len(operands) != 2 or isinstance(operands[1], (dict, list)):
        raise ValueError("comparison requires a field and one scalar value")
    return {"operator": operator, "operands": operands}


class YfinanceCustomScreenerQuery(YfinanceScreenLimitQuery):
    """Typed ODP yfinance filters mapped to documented EquityQuery fields."""

    country: str | None = "us"
    exchange: str | None = None
    sector: (
        Literal[
            "basic_materials",
            "communication_services",
            "consumer_cyclical",
            "consumer_defensive",
            "energy",
            "financial_services",
            "healthcare",
            "industrials",
            "real_estate",
            "technology",
            "utilities",
        ]
        | None
    ) = None
    industry: str | None = None
    mktcap_min: int | None = 500_000_000
    mktcap_max: int | None = None
    price_min: float | None = 5
    price_max: float | None = None
    volume_min: int | None = 10_000
    volume_max: int | None = None
    beta_min: float | None = None
    beta_max: float | None = None

    @field_validator("country")
    @classmethod
    def _validate_country(cls, value: str | None) -> str | None:
        return _normalize_country(value) if value is not None else None

    @field_validator("exchange")
    @classmethod
    def _validate_exchange(cls, value: str | None) -> str | None:
        return _normalize_exchange(value)

    @field_validator("industry")
    @classmethod
    def _validate_industry(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError("industry must be a nonblank value")
        return value.strip() if value is not None else None

    @field_validator("mktcap_min", "mktcap_max", "volume_min", "volume_max")
    @classmethod
    def _validate_nonnegative_int(cls, value: int | None) -> int | None:
        if value is not None and (isinstance(value, bool) or value < 0):
            raise ValueError("integer screen bounds must be nonnegative")
        return value

    @field_validator("price_min", "price_max", "beta_min", "beta_max")
    @classmethod
    def _validate_finite_bound(cls, value: float | None) -> float | None:
        if value is not None and (not pd.notna(value) or abs(value) == float("inf")):
            raise ValueError("screen bounds must be finite numbers")
        return value

    @model_validator(mode="after")
    def _validate_bounds(self) -> YfinanceCustomScreenerQuery:
        for lower, upper in (
            (self.mktcap_min, self.mktcap_max),
            (self.price_min, self.price_max),
            (self.volume_min, self.volume_max),
            (self.beta_min, self.beta_max),
        ):
            if lower is not None and upper is not None and lower >= upper:
                raise ValueError("minimum screen bound must be below maximum")
        return self


class YfinanceAggressiveScreenerQuery(YfinanceScreenerQuery):
    """Aggressive-small-caps defaults to all source results, fetched pagewise."""

    limit: int | None = None


MAX_YFINANCE_SCREEN_PAGE_SIZE = 250
MAX_YFINANCE_SCREEN_PAGES = 20


def _equity_screener_filter(query: YfinanceCustomScreenerQuery) -> dict[str, object]:
    criteria: list[dict[str, object]] = []

    def add(operator: str, field_name: str, value: object) -> None:
        criteria.append({"operator": operator, "operands": [field_name, value]})

    if query.country is not None and query.country != "all":
        add("eq", "region", query.country)
    if query.exchange is not None:
        add("eq", "exchange", query.exchange)
    if query.sector is not None:
        add("eq", "sector", _YFINANCE_SECTORS[query.sector])
    if query.industry is not None:
        add("eq", "industry", query.industry)
    for field_name, lower, upper in (
        ("intradaymarketcap", query.mktcap_min, query.mktcap_max),
        ("intradayprice", query.price_min, query.price_max),
        ("dayvolume", query.volume_min, query.volume_max),
        ("beta", query.beta_min, query.beta_max),
    ):
        if lower is not None:
            add("gte", field_name, lower)
        if upper is not None:
            add("lte", field_name, upper)
    if not criteria:
        raise ValueError("at least one yfinance EquityQuery filter is required")
    if len(criteria) == 1:
        return criteria[0]
    return _validate_equity_filter({"operator": "and", "operands": criteria})


def _fetch_screen_pages(
    query: YfinanceScreenLimitQuery,
    *,
    fetch_page: Callable[[int, int], dict[str, object]],
    none_means_all: bool = False,
) -> dict[str, object]:
    """Fetch a bounded result set or fail rather than silently truncate an all query."""
    target = query.limit
    if target is None and not none_means_all:
        target = 200
    if target == 0 or (target is None and none_means_all):
        target = None
    page_size = (
        MAX_YFINANCE_SCREEN_PAGE_SIZE
        if target is None
        else min(target, MAX_YFINANCE_SCREEN_PAGE_SIZE)
    )
    if page_size == 0:
        return {"quotes": []}
    quotes: list[object] = []
    response: dict[str, object] = {"quotes": []}
    offset = 0
    for _ in range(MAX_YFINANCE_SCREEN_PAGES):
        response = fetch_page(offset, page_size)
        page_quotes = response.get("quotes")
        if not isinstance(page_quotes, list):
            raise YfinanceProviderError("YFINANCE_SCREEN_SHAPE_INVALID")
        quotes.extend(page_quotes)
        if target is not None and len(quotes) >= target:
            quotes = quotes[:target]
            break
        if len(page_quotes) < page_size:
            break
        offset += len(page_quotes)
    else:
        raise YfinanceProviderError("YFINANCE_SCREEN_RESULT_BOUND_EXCEEDED")
    return {**response, "quotes": quotes}


class YfinanceScreenerFetcher(Fetcher[YfinanceScreenerQuery, dict[str, object]]):
    """Screener fetcher bound to one ``screener_id``/``query_name`` pair."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str]
    screener_id: ClassVar[str]
    query_name: ClassVar[str]
    domain: ClassVar[str]
    sort_field: ClassVar[str]
    query_type: ClassVar[type[YfinanceScreenerQuery]] = YfinanceScreenerQuery
    row_model: ClassVar[type[YfinanceScreenerObservation]] = YfinanceScreenerObservation
    none_means_all: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        cls.capability = Capability(
            asset_class="equity",
            domain=cls.domain,
            period="snapshot",
            market="global",
            source=SOURCE,
            verified=False,
        )

    def transform_query(self, **kwargs: object) -> YfinanceScreenerQuery:
        """Validate keyword arguments into ``query_type``."""
        return self.query_type.model_validate(kwargs)

    def extract_data(self, params: YfinanceScreenerQuery, ctx: FetchContext) -> dict[str, object]:
        """Fetch the screener pages for the validated query through the SDK seam."""
        from opendata.data.providers.yfinance.models import _sdk

        query = _validated_query(params, self.query_type)
        return _fetch_screen_pages(
            query,
            none_means_all=self.none_means_all,
            fetch_page=lambda offset, limit: _sdk.require_screen(
                self.query_name,
                offset=offset,
                limit=limit,
                predefined=True,
                sort_field=self.sort_field,
                sort_ascending=query.sort == "asc",
            ),
        )

    def transform_data(self, raw: dict[str, object], params: YfinanceScreenerQuery) -> FetchResult:
        """Turn one screener document into observations."""
        _validated_query(params, self.query_type)
        if not isinstance(raw, Mapping) or not isinstance(raw.get("quotes"), list):
            raise YfinanceProviderError("YFINANCE_SCREEN_SHAPE_INVALID")
        return _screen_rows(raw, self.screener_id, 0)


def _screen_rows(
    raw: Mapping[str, object], screener_id: str, offset: int
) -> tuple[YfinanceScreenerObservation, ...]:
    quotes_value = raw["quotes"]
    if not isinstance(quotes_value, list):
        raise YfinanceProviderError("YFINANCE_SCREEN_SHAPE_INVALID")
    quotes: list[Mapping[str, object]] = []
    for item in quotes_value:
        if not isinstance(item, Mapping):
            raise YfinanceProviderError("YFINANCE_SCREEN_SHAPE_INVALID")
        quotes.append(item)
    rows = []
    for ordinal, quote in enumerate(quotes, start=offset):
        symbol = quote.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise YfinanceProviderError("YFINANCE_SCREEN_SYMBOL_INVALID")
        rows.append(
            YfinanceScreenerObservation(
                screener_id=screener_id,
                ordinal=ordinal,
                symbol=symbol,
                raw_payload_json=_canonical_json(quote),
            )
        )
    return tuple(rows)


class YfinanceEquityActiveFetcher(YfinanceScreenerFetcher):
    """Fetch Yahoo's predefined most-active-equities screen."""

    screener_id = "EquityActive"
    canonical_model = "EquityActive"
    query_name = "most_actives"
    sort_field = "dayvolume"
    domain = "yfinance_equity_active"


class YfinanceEquityAggressiveSmallCapsFetcher(YfinanceScreenerFetcher):
    """Fetch Yahoo's predefined aggressive-small-caps screen."""

    screener_id = "EquityAggressiveSmallCaps"
    canonical_model = "EquityAggressiveSmallCaps"
    query_name = "aggressive_small_caps"
    sort_field = "eodvolume"
    query_type = YfinanceAggressiveScreenerQuery
    none_means_all = True
    domain = "yfinance_equity_aggressive_small_caps"


class YfinanceEquityGainersFetcher(YfinanceScreenerFetcher):
    """Fetch Yahoo's predefined day-gainers screen."""

    screener_id = "EquityGainers"
    canonical_model = "EquityGainers"
    query_name = "day_gainers"
    sort_field = "percentchange"
    domain = "yfinance_equity_gainers"


class YfinanceEquityLosersFetcher(YfinanceScreenerFetcher):
    """Fetch Yahoo's predefined day-losers screen."""

    screener_id = "EquityLosers"
    canonical_model = "EquityLosers"
    query_name = "day_losers"
    sort_field = "percentchange"
    domain = "yfinance_equity_losers"


class YfinanceEquityUndervaluedGrowthFetcher(YfinanceScreenerFetcher):
    """Fetch Yahoo's predefined undervalued-growth screen."""

    screener_id = "EquityUndervaluedGrowth"
    canonical_model = "EquityUndervaluedGrowth"
    query_name = "undervalued_growth_stocks"
    sort_field = "eodvolume"
    domain = "yfinance_equity_undervalued_growth"


class YfinanceEquityUndervaluedLargeCapsFetcher(YfinanceScreenerFetcher):
    """Fetch Yahoo's predefined undervalued-large-caps screen."""

    screener_id = "EquityUndervaluedLargeCaps"
    canonical_model = "EquityUndervaluedLargeCaps"
    query_name = "undervalued_large_caps"
    sort_field = "eodvolume"
    domain = "yfinance_equity_undervalued_large_caps"


class YfinanceGrowthTechEquitiesFetcher(YfinanceScreenerFetcher):
    """Fetch Yahoo's predefined growth-technology equities screen."""

    screener_id = "GrowthTechEquities"
    canonical_model = "GrowthTechEquities"
    query_name = "growth_technology_stocks"
    sort_field = "eodvolume"
    domain = "yfinance_growth_tech_equities"


class YfinanceEquityScreenerFetcher(Fetcher[YfinanceCustomScreenerQuery, dict[str, object]]):
    """Fetch a custom Yahoo screen from the public typed equity filters."""

    async_mode = "bounded_thread"
    canonical_model = "EquityScreener"
    row_model: ClassVar[type[YfinanceScreenerObservation]] = YfinanceScreenerObservation
    capability = Capability(
        asset_class="equity",
        domain="yfinance_equity_screener",
        period="snapshot",
        market="global",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> YfinanceCustomScreenerQuery:
        """Validate the public custom-equity-screen query."""
        return YfinanceCustomScreenerQuery.model_validate(kwargs)

    def extract_data(
        self, params: YfinanceCustomScreenerQuery, ctx: FetchContext
    ) -> dict[str, object]:
        """Build documented EquityQuery filters and fetch bounded result pages."""
        from opendata.data.providers.yfinance.models import _sdk

        query = _validated_query(params, YfinanceCustomScreenerQuery)
        try:
            filters = _equity_screener_filter(query)
        except ValueError as exc:
            raise YfinanceProviderError("YFINANCE_SCREEN_QUERY_INVALID") from exc
        return _fetch_screen_pages(
            query,
            fetch_page=lambda offset, limit: _sdk.require_screen(
                filters, offset=offset, limit=limit, predefined=False
            ),
        )

    def transform_data(
        self, raw: dict[str, object], params: YfinanceCustomScreenerQuery
    ) -> FetchResult:
        """Validate screen rows and retain the upstream quote payloads."""
        _validated_query(params, YfinanceCustomScreenerQuery)
        if not isinstance(raw, Mapping) or not isinstance(raw.get("quotes"), list):
            raise YfinanceProviderError("YFINANCE_SCREEN_SHAPE_INVALID")
        return _screen_rows(raw, "EquityScreener", 0)


class YfinanceKeyMetricsQuery(YfinanceMultiSymbolQuery):
    """Key-metrics query with ttm, period and window fields."""

    ttm: Literal["include", "exclude", "only"] = "only"
    period: Literal["q1", "q2", "q3", "q4", "fy", "annual", "quarter"] = "annual"
    limit: int | None = None
    # Kept for existing callers; the public query contract uses period/limit.
    frequency: Literal["quarterly", "yearly"] | None = None
    periods: int | None = None

    @field_validator("limit", "periods")
    @classmethod
    def _validate_period_count(cls, value: int | None) -> int | None:
        if value is not None and (isinstance(value, bool) or not 0 <= value <= 100):
            raise ValueError("period count must be between 0 and 100 or None")
        return value

    @property
    def effective_ttm(self) -> Literal["include", "exclude", "only"]:
        """Return the TTM mode implied by explicit fields, dates, frequency and periods."""
        if "ttm" in self.model_fields_set:
            return self.ttm
        if self.start_date is not None or self.end_date is not None:
            return "exclude"
        if self.frequency is not None or self.periods is not None:
            return "include"
        return self.ttm

    @property
    def effective_frequency(self) -> Literal["quarterly", "yearly"]:
        """Return the frequency implied by ``frequency`` or the requested ``period``."""
        if self.frequency is not None:
            return self.frequency
        return "quarterly" if self.period in {"quarter", "q1", "q2", "q3", "q4"} else "yearly"

    @property
    def effective_periods(self) -> int:
        """Return the period count implied by the TTM mode, ``periods`` and ``limit``."""
        if self.effective_ttm == "only":
            return 0
        if self.periods is not None:
            return self.periods
        return self.limit if self.limit is not None else 5

    @model_validator(mode="after")
    def _validate_window(self) -> YfinanceKeyMetricsQuery:
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("start_date and end_date must be provided together")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        if self.period in {"q1", "q2", "q3", "q4"}:
            raise ValueError("yfinance valuation measures cannot select one fiscal quarter")
        if self.effective_ttm == "only" and "limit" in self.model_fields_set:
            raise ValueError("limit is only available when ttm is include or exclude")
        if ("ttm" in self.model_fields_set and self.ttm != "exclude") and (
            self.start_date is not None or self.end_date is not None
        ):
            raise ValueError("date filters conflict with explicit ttm selection")
        if self.frequency is not None and "period" in self.model_fields_set:
            period_frequency = "quarterly" if self.period == "quarter" else "yearly"
            if self.frequency != period_frequency:
                raise ValueError("frequency conflicts with public period")
        if (
            self.periods is not None
            and "limit" in self.model_fields_set
            and self.limit is not None
            and self.periods != self.limit
        ):
            raise ValueError("legacy periods conflicts with public limit")
        return self


_YFINANCE_VALUATION_FIELDS = {
    "Market Cap": "market_cap",
    "Enterprise Value": "enterprise_value",
    "Trailing P/E": "pe_ratio",
    "Forward P/E": "forward_pe",
    "PEG Ratio (5yr expected)": "peg_ratio",
    "Price/Sales": "price_to_sales",
    "Price/Book": "price_to_book",
    "Enterprise Value/Revenue": "enterprise_to_revenue",
    "Enterprise Value/EBITDA": "enterprise_to_ebitda",
}
_YFINANCE_METRIC_VALUE_FIELDS = frozenset(_YFINANCE_VALUATION_FIELDS.values())
_PUBLIC_UNRETURNED_METRIC_FIELDS = frozenset(
    {
        "peg_ratio_ttm",
        "eps_ttm",
        "eps_forward",
        "earnings_growth",
        "earnings_growth_quarterly",
        "revenue_per_share",
        "revenue_growth",
        "cash_per_share",
        "quick_ratio",
        "current_ratio",
        "debt_to_equity",
        "gross_margin",
        "operating_margin",
        "ebitda_margin",
        "profit_margin",
        "return_on_assets",
        "return_on_equity",
        "dividend_yield",
        "dividend_yield_5y_avg",
        "payout_ratio",
        "book_value",
        "overall_risk",
        "audit_risk",
        "board_risk",
        "compensation_risk",
        "shareholder_rights_risk",
        "beta",
        "price_return_1y",
    }
)
_YFINANCE_METRIC_FIELDS = _YFINANCE_METRIC_VALUE_FIELDS | _PUBLIC_UNRETURNED_METRIC_FIELDS


def _key_metric_value(field_name: str, value: object) -> int | float | None:
    if _is_missing_scalar(value):
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise YfinanceProviderError("YFINANCE_METRIC_VALUE_INVALID")
    if field_name == "enterprise_value":
        if isinstance(value, Integral):
            return int(value)
        numeric = _source_number(value, allow_missing=False)
        if numeric is None or not numeric.is_integer():
            raise YfinanceProviderError("YFINANCE_METRIC_INTEGER_INVALID")
        return int(numeric)
    if field_name == "market_cap" and isinstance(value, Integral):
        return int(value)
    return _source_number(value, allow_missing=False)


KeyMetricsRaw = tuple[tuple[str, pd.DataFrame | None], ...]


class YfinanceKeyMetricsFetcher(Fetcher[YfinanceKeyMetricsQuery, KeyMetricsRaw]):
    """Fetch current and historical valuation measures for multiple symbols."""

    async_mode = "bounded_thread"
    canonical_model = "KeyMetrics"
    row_model: ClassVar[type[YfinanceKeyMetricObservation]] = YfinanceKeyMetricObservation
    capability = Capability(
        asset_class="equity",
        domain="yfinance_key_metrics",
        period="reported_period",
        market="global",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> YfinanceKeyMetricsQuery:
        """Validate public TTM, period, and limit query fields."""
        return YfinanceKeyMetricsQuery.model_validate(kwargs)

    def extract_data(self, params: YfinanceKeyMetricsQuery, ctx: FetchContext) -> KeyMetricsRaw:
        """Fetch bounded valuation history for each requested symbol."""
        from opendata.data.providers.yfinance.models import _sdk

        query = _validated_query(params, YfinanceKeyMetricsQuery)
        return _fetch_by_symbol(
            query.symbols,
            lambda symbol: _sdk.require_valuation_measures(
                symbol,
                frequency=query.effective_frequency,
                periods=query.effective_periods,
            ),
        )

    def transform_data(self, raw: KeyMetricsRaw, params: YfinanceKeyMetricsQuery) -> FetchResult:
        """Validate per-symbol frames and retain dated or current metric values."""
        query = _validated_query(params, YfinanceKeyMetricsQuery)
        if not isinstance(raw, tuple) or len(raw) != len(query.symbols):
            raise YfinanceProviderError("YFINANCE_METRICS_SHAPE_INVALID")
        rows: list[YfinanceKeyMetricObservation] = []
        for expected_symbol, entry in zip(query.symbols, raw, strict=True):
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or entry[0] != expected_symbol
                or (entry[1] is not None and not isinstance(entry[1], pd.DataFrame))
            ):
                raise YfinanceProviderError("YFINANCE_METRICS_SYMBOL_MISMATCH")
            symbol_query = query.model_copy(update={"symbol": expected_symbol})
            rows.extend(_key_metric_rows(entry[1], symbol_query, expected_symbol))
        if not rows:
            raise YfinanceProviderError("YFINANCE_EMPTY_RESPONSE")
        return tuple(rows)


def _key_metric_rows(
    raw: pd.DataFrame | None, query: YfinanceKeyMetricsQuery, symbol: str
) -> tuple[YfinanceKeyMetricObservation, ...]:
    if (
        not isinstance(raw, pd.DataFrame)
        or raw.empty
        or raw.index.has_duplicates
        or raw.columns.has_duplicates
    ):
        raise YfinanceProviderError("YFINANCE_METRICS_SHAPE_INVALID")
    labels: list[tuple[date_type | None, Literal["current", "historical"]]] = []
    for column in raw.columns:
        if column == "Current":
            labels.append((None, "current"))
        else:
            labels.append((_as_date(column, field="metric_period"), "historical"))
    if len({(period_date, kind) for period_date, kind in labels}) != len(labels):
        raise YfinanceProviderError("YFINANCE_METRIC_KEYS_DUPLICATED")
    mapped_rows: list[tuple[str, list[object]]] = []
    for metric_name, row_values in raw.iterrows():
        if not isinstance(metric_name, str) or metric_name not in _YFINANCE_VALUATION_FIELDS:
            raise YfinanceProviderError("YFINANCE_METRIC_NAME_UNMAPPED")
        mapped_rows.append((_YFINANCE_VALUATION_FIELDS[metric_name], row_values.tolist()))

    if len({field_name for field_name, _ in mapped_rows}) != len(mapped_rows):
        raise YfinanceProviderError("YFINANCE_METRIC_KEYS_DUPLICATED")

    rows: list[YfinanceKeyMetricObservation] = []
    for column_index, (period_date, kind) in enumerate(labels):
        if kind == "current" and query.effective_ttm == "exclude":
            continue
        if kind == "historical" and query.effective_ttm == "only":
            continue
        if kind == "current" and (query.start_date is not None or query.end_date is not None):
            continue
        if period_date is not None:
            if query.start_date is not None and period_date < query.start_date:
                continue
            if query.end_date is not None and period_date > query.end_date:
                continue

        metric_values: dict[str, int | float | None] = dict.fromkeys(
            _YFINANCE_METRIC_VALUE_FIELDS, None
        )
        availability: dict[
            str, Literal["reported", "source_missing", "not_returned_by_sdk_method"]
        ] = dict.fromkeys(_YFINANCE_METRIC_FIELDS, "not_returned_by_sdk_method")
        for field_name, column_values in mapped_rows:
            raw_value = column_values[column_index]
            parsed_value = _key_metric_value(field_name, raw_value)
            metric_values[field_name] = parsed_value
            availability[field_name] = "source_missing" if parsed_value is None else "reported"

        rows.append(
            _key_metric_observation(
                symbol=symbol,
                period_date=period_date,
                period_kind=kind,
                availability=availability,
                values=metric_values,
            )
        )
    return tuple(rows)


def _key_metric_observation(
    *,
    symbol: str,
    period_date: date_type | None,
    period_kind: Literal["current", "historical"],
    availability: dict[str, Literal["reported", "source_missing", "not_returned_by_sdk_method"]],
    values: Mapping[str, int | float | None],
) -> YfinanceKeyMetricObservation:
    """Construct the named typed fields without dynamic keyword expansion."""
    enterprise_value = values.get("enterprise_value")
    if enterprise_value is not None and (
        isinstance(enterprise_value, bool) or not isinstance(enterprise_value, int)
    ):
        raise YfinanceProviderError("YFINANCE_METRIC_INTEGER_INVALID")
    return YfinanceKeyMetricObservation(
        symbol=symbol,
        period_ending=period_date,
        period_date=period_date,
        period_kind=period_kind,
        metric_availability=availability,
        market_cap=values.get("market_cap"),
        pe_ratio=values.get("pe_ratio"),
        forward_pe=values.get("forward_pe"),
        peg_ratio=values.get("peg_ratio"),
        peg_ratio_ttm=values.get("peg_ratio_ttm"),
        eps_ttm=values.get("eps_ttm"),
        eps_forward=values.get("eps_forward"),
        enterprise_to_ebitda=values.get("enterprise_to_ebitda"),
        earnings_growth=values.get("earnings_growth"),
        earnings_growth_quarterly=values.get("earnings_growth_quarterly"),
        revenue_per_share=values.get("revenue_per_share"),
        revenue_growth=values.get("revenue_growth"),
        enterprise_to_revenue=values.get("enterprise_to_revenue"),
        cash_per_share=values.get("cash_per_share"),
        quick_ratio=values.get("quick_ratio"),
        current_ratio=values.get("current_ratio"),
        debt_to_equity=values.get("debt_to_equity"),
        gross_margin=values.get("gross_margin"),
        operating_margin=values.get("operating_margin"),
        ebitda_margin=values.get("ebitda_margin"),
        profit_margin=values.get("profit_margin"),
        return_on_assets=values.get("return_on_assets"),
        return_on_equity=values.get("return_on_equity"),
        dividend_yield=values.get("dividend_yield"),
        dividend_yield_5y_avg=values.get("dividend_yield_5y_avg"),
        payout_ratio=values.get("payout_ratio"),
        book_value=values.get("book_value"),
        price_to_book=values.get("price_to_book"),
        enterprise_value=enterprise_value,
        overall_risk=values.get("overall_risk"),
        audit_risk=values.get("audit_risk"),
        board_risk=values.get("board_risk"),
        compensation_risk=values.get("compensation_risk"),
        shareholder_rights_risk=values.get("shareholder_rights_risk"),
        beta=values.get("beta"),
        price_return_1y=values.get("price_return_1y"),
        price_to_sales=values.get("price_to_sales"),
    )


class YfinanceOptionChainQuery(YfinanceSymbolQuery):
    """Option-chain query keyed by symbol, with either spelling of the expiry date."""

    date: date_type | None = None
    expiration_date: date_type | None = None

    @field_validator("expiration_date")
    @classmethod
    def _validate_expiration(cls, value: date_type | None) -> date_type | None:
        if value is not None and value < date_type.today():
            raise ValueError("expiration_date must not be in the past")
        return value

    @model_validator(mode="after")
    def _reject_history_dates(self) -> YfinanceOptionChainQuery:
        if self.date is not None:
            raise ValueError(
                "ODP date requests an EOD options snapshot; yfinance option_chain(date) "
                "semantics are not verified"
            )
        if self.expiration_date is None:
            raise ValueError(
                "yfinance requires an explicit expiration, which is not equivalent to "
                "the public date field"
            )
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("option chain snapshots do not accept historical date filters")
        return self


class YfinanceOptionsChainsFetcher(
    Fetcher[YfinanceOptionChainQuery, tuple[pd.DataFrame, pd.DataFrame]]
):
    """Fetch and validate calls and puts for a yfinance expiration snapshot."""

    async_mode = "bounded_thread"
    canonical_model = "OptionsChains"
    row_model: ClassVar[type[YfinanceOptionChainObservation]] = YfinanceOptionChainObservation
    capability = Capability(
        asset_class="option",
        domain="yfinance_options_chains",
        period="snapshot",
        market="global",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> YfinanceOptionChainQuery:
        """Validate the options-chain query and its expiry-date gap checks."""
        return YfinanceOptionChainQuery.model_validate(kwargs)

    def extract_data(
        self, params: YfinanceOptionChainQuery, ctx: FetchContext
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Fetch both option types for the requested explicit expiration."""
        from opendata.data.providers.yfinance.models import _sdk

        query = _validated_query(params, YfinanceOptionChainQuery)
        expiration = query.expiration_date
        if expiration is None:
            raise YfinanceProviderError("YFINANCE_BAD_QUERY")
        return _sdk.require_options_chain(query.symbol, expiration.isoformat())

    def transform_data(
        self, raw: tuple[pd.DataFrame, pd.DataFrame], params: YfinanceOptionChainQuery
    ) -> FetchResult:
        """Validate call/put frames and create typed contract observations."""
        query = _validated_query(params, YfinanceOptionChainQuery)
        if not isinstance(raw, tuple) or len(raw) != 2:
            raise YfinanceProviderError("YFINANCE_OPTIONS_SHAPE_INVALID")
        rows: list[YfinanceOptionChainObservation] = []
        option_types: tuple[Literal["call", "put"], Literal["call", "put"]] = (
            "call",
            "put",
        )
        expiration_date = query.expiration_date
        if expiration_date is None:
            raise YfinanceProviderError("YFINANCE_BAD_QUERY")
        for option_type, frame in zip(option_types, raw, strict=True):
            if not isinstance(frame, pd.DataFrame) or frame.columns.has_duplicates:
                raise YfinanceProviderError("YFINANCE_OPTIONS_SHAPE_INVALID")
            if not {"contractSymbol", "strike"}.issubset(frame.columns):
                raise YfinanceProviderError("YFINANCE_OPTIONS_SHAPE_INVALID")
            for _, item in frame.iterrows():
                record = item.to_dict()
                contract = record.get("contractSymbol")
                if not isinstance(contract, str) or not contract.strip():
                    raise YfinanceProviderError("YFINANCE_OPTION_CONTRACT_INVALID")
                rows.append(
                    YfinanceOptionChainObservation(
                        symbol=query.symbol,
                        expiration_date=expiration_date,
                        option_type=option_type,
                        contract_symbol=contract,
                        strike=_required_source_number(
                            record.get("strike"), code="YFINANCE_OPTION_VALUE_MISSING"
                        ),
                        last_price=_optional_source_number(record.get("lastPrice")),
                        bid=_optional_source_number(record.get("bid")),
                        ask=_optional_source_number(record.get("ask")),
                        volume=_optional_source_number(record.get("volume")),
                        open_interest=_optional_source_number(record.get("openInterest")),
                        implied_volatility=_optional_source_number(record.get("impliedVolatility")),
                        in_the_money=_optional_source_bool(record.get("inTheMoney")),
                        raw_payload_json=_canonical_json(record),
                    )
                )
        if not rows:
            raise YfinanceProviderError("YFINANCE_EMPTY_RESPONSE")
        return tuple(rows)


def _optional_source_bool(value: object) -> bool | None:
    if _is_missing_scalar(value):
        return None
    if not isinstance(value, bool):
        raise YfinanceProviderError("YFINANCE_OPTION_VALUE_INVALID")
    return value


PriceTargetRaw = tuple[tuple[str, dict[str, object]], ...]


class YfinancePriceTargetFetcher(Fetcher[YfinanceCurrentMultiSymbolQuery, PriceTargetRaw]):
    """Fetch and validate analyst price-target summaries per symbol."""

    async_mode = "bounded_thread"
    canonical_model = "PriceTargetConsensus"
    row_model: ClassVar[type[YfinancePriceTargetConsensus]] = YfinancePriceTargetConsensus
    capability = Capability(
        asset_class="equity",
        domain="yfinance_price_target_consensus",
        period="snapshot",
        market="global",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> YfinanceCurrentMultiSymbolQuery:
        """Validate the bounded multi-symbol price-target query."""
        return YfinanceCurrentMultiSymbolQuery.model_validate(kwargs)

    def extract_data(
        self, params: YfinanceCurrentMultiSymbolQuery, ctx: FetchContext
    ) -> PriceTargetRaw:
        """Fetch one price-target summary for each normalized symbol."""
        from opendata.data.providers.yfinance.models import _sdk

        query = _validated_query(params, YfinanceCurrentMultiSymbolQuery)
        return _fetch_by_symbol(query.symbols, _sdk.require_price_targets)

    def transform_data(
        self, raw: PriceTargetRaw, params: YfinanceCurrentMultiSymbolQuery
    ) -> FetchResult:
        """Validate ticker attribution and populate typed consensus rows."""
        query = _validated_query(params, YfinanceCurrentMultiSymbolQuery)
        if not isinstance(raw, tuple) or len(raw) != len(query.symbols):
            raise YfinanceProviderError("YFINANCE_PRICE_TARGET_SHAPE_INVALID")
        required = {"current", "low", "high", "mean", "median"}
        rows: list[YfinancePriceTargetConsensus] = []
        for expected_symbol, entry in zip(query.symbols, raw, strict=True):
            if not isinstance(entry, tuple) or len(entry) != 2 or not isinstance(entry[1], Mapping):
                raise YfinanceProviderError("YFINANCE_PRICE_TARGET_SHAPE_INVALID")
            if entry[0] != expected_symbol:
                raise YfinanceProviderError("YFINANCE_PRICE_TARGET_SYMBOL_MISMATCH")
            if not required.issubset(entry[1]):
                raise YfinanceProviderError("YFINANCE_PRICE_TARGET_SHAPE_INVALID")
            rows.append(
                YfinancePriceTargetConsensus(
                    symbol=expected_symbol,
                    current_price=_optional_source_number(entry[1]["current"]),
                    target_low=_optional_source_number(entry[1]["low"]),
                    target_high=_optional_source_number(entry[1]["high"]),
                    target_consensus=_optional_source_number(entry[1]["mean"]),
                    target_median=_optional_source_number(entry[1]["median"]),
                    retrieved_at_utc=datetime.now(UTC),
                )
            )
        return tuple(rows)


__all__ = [
    "YfinanceCompanyNewsFetcher",
    "YfinanceCryptoHistoricalFetcher",
    "YfinanceCurrencyHistoricalFetcher",
    "YfinanceEquityActiveFetcher",
    "YfinanceEquityAggressiveSmallCapsFetcher",
    "YfinanceEquityGainersFetcher",
    "YfinanceEquityHistoricalFetcher",
    "YfinanceEquityInfoFetcher",
    "YfinanceEquityLosersFetcher",
    "YfinanceEquityQuoteFetcher",
    "YfinanceEquityScreenerFetcher",
    "YfinanceEquityUndervaluedGrowthFetcher",
    "YfinanceEquityUndervaluedLargeCapsFetcher",
    "YfinanceEtfHistoricalFetcher",
    "YfinanceEtfInfoFetcher",
    "YfinanceFuturesHistoricalFetcher",
    "YfinanceGrowthTechEquitiesFetcher",
    "YfinanceIndexHistoricalFetcher",
    "YfinanceKeyExecutivesFetcher",
    "YfinanceKeyMetricsFetcher",
    "YfinanceOptionsChainsFetcher",
    "YfinancePriceTargetFetcher",
    "YfinanceShareStatisticsFetcher",
]
