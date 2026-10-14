"""Typed Deribit request models and clean-room fetchers for five public data products."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping  # noqa: TC003 - runtime reflection resolves _validate_row
from datetime import date as date_type
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import ClassVar, Literal, TypeVar, cast, overload

from pydantic import (
    ConfigDict,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from opendata.data.capability import Capability
from opendata.data.models.base import ContractModel
from opendata.data.models.deribit import (
    DeribitFuturesCurve,
    DeribitFuturesHistorical,
    DeribitFuturesInfo,
    DeribitFuturesInstruments,
    DeribitOptionsChains,
    DeribitTickSizeStep,
)
from opendata.data.protocol import (
    BaseQueryParams,
    FetchContext,
    Fetcher,
    FetchResult,
    MultiSymbolQueryParams,
    QueryParams,
)
from opendata.data.providers.deribit import client
from opendata.data.providers.deribit._source import SOURCE
from opendata.data.providers.deribit.client import DeribitProviderError, RpcReply

InstrumentCurrency = Literal["BTC", "ETH", "USDC", "USDT", "EURR", "any"]
MarketCurrency = Literal["BTC", "ETH", "USDC", "USDT", "EURR"]
HistoricalInterval = Literal[
    "1m",
    "3m",
    "5m",
    "10m",
    "15m",
    "30m",
    "1h",
    "2h",
    "3h",
    "6h",
    "12h",
    "1d",
]

MAX_RESPONSE_ROWS = 10_000
MAX_HISTORY_CANDLES = 10_000
MAX_HISTORY_DAYS = 36_500
MAX_FUTURE_INFO_SYMBOLS = 32
MAX_OPTION_TICKERS = 30
_FUTURE_SYMBOL_RE = re.compile(r"^[A-Z0-9]+(?:_[A-Z0-9]+)?-(?:PERPETUAL|\d{1,2}[A-Z]{3}\d{2})$")
_DATED_FUTURE_SYMBOL_RE = re.compile(
    r"^[A-Z0-9]+(?:_[A-Z0-9]+)?-(?P<day>\d{1,2})(?P<month>[A-Z]{3})(?P<year>\d{2})$"
)
_UNDERLYING_RE = re.compile(r"^[A-Z0-9]+(?:_[A-Z0-9]+)?$")
_EXPIRATION_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_OPTION_SYMBOL_RE = re.compile(
    r"^(?P<underlying>[A-Z0-9]+(?:_[A-Z0-9]+)?)-"
    r"(?P<expiry>\d{1,2}[A-Z]{3}\d{2})-"
    r"(?P<strike>\d+(?:d\d+)?)-(?P<option_type>[CP])$"
)
_INTERVAL_RESOLUTIONS: dict[str, tuple[str, int]] = {
    "1m": ("1", 1),
    "3m": ("3", 3),
    "5m": ("5", 5),
    "10m": ("10", 10),
    "15m": ("15", 15),
    "30m": ("30", 30),
    "1h": ("60", 60),
    "2h": ("120", 120),
    "3h": ("180", 180),
    "6h": ("360", 360),
    "12h": ("720", 720),
    "1d": ("1D", 1_440),
}

_RowT = TypeVar("_RowT", bound=ContractModel)


def _capability(domain: str, period: str, model: str) -> Capability:
    """Declare each adapter as unverified until an independent live-data review."""
    return Capability(
        asset_class="derivative",
        domain=domain,
        period=period,
        market="global",
        source=SOURCE,
        verified=False,
        notes=f"{model}: official public API contract; offline fixtures only",
    )


class _DeribitQuery(BaseQueryParams):
    """Strict source and market validator shared by both symbol families."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @model_validator(mode="after")
    def _validate_source(self) -> _DeribitQuery:
        if self.source not in {"auto", "deribit"}:
            raise ValueError("source must be 'auto' or 'deribit'")
        return self


class _DeribitScalarQuery(_DeribitQuery, QueryParams):
    """Deribit provider root with a scalar symbol field."""


class _DeribitMultiQuery(_DeribitQuery, MultiSymbolQueryParams):
    """Deribit provider root that retains multi-symbol input shapes."""


class FuturesHistoricalQuery(_DeribitMultiQuery):
    """One or more instruments and an inclusive UTC calendar-date range."""

    symbol: str | list[str]
    interval: HistoricalInterval = "1d"
    expiration: str | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def _validate_symbol(cls, value: object) -> str | list[str]:
        if isinstance(value, str):
            return _canonical_future_symbol(value)
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_FUTURE_INFO_SYMBOLS:
            raise ValueError(f"symbol must contain 1 to {MAX_FUTURE_INFO_SYMBOLS} instruments")
        symbols = [_canonical_future_symbol(item) for item in value]
        if len(set(symbols)) != len(symbols):
            raise ValueError("symbol values must be unique")
        return symbols

    @field_validator("expiration")
    @classmethod
    def _validate_expiration_month(cls, value: str | None) -> str | None:
        if value is not None and not _EXPIRATION_MONTH_RE.fullmatch(value):
            raise ValueError("expiration must use YYYY-MM")
        return value

    @model_validator(mode="after")
    def _validate_window(self) -> FuturesHistoricalQuery:
        if self.start_date is None or self.end_date is None:
            raise ValueError("start_date and end_date are required for historical data")
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        if (self.end_date - self.start_date).days + 1 > MAX_HISTORY_DAYS:
            raise ValueError(f"historical window is limited to {MAX_HISTORY_DAYS} days")
        if self.end_date == date_type.max:
            raise ValueError("end_date is too late to form an inclusive UTC date window")
        if self.expiration is not None:
            symbols = _symbols(self.symbol)
            if any(_future_expiration_month(symbol) != self.expiration for symbol in symbols):
                raise ValueError("expiration must match each explicitly dated future symbol")
        _validate_market(self.market)
        _check_history_size(self.start_date, self.end_date, self.interval)
        return self


class FuturesInfoQuery(_DeribitMultiQuery):
    """One or more futures live ticker queries."""

    symbol: str | list[str]

    @field_validator("symbol", mode="before")
    @classmethod
    def _validate_symbol(cls, value: object) -> str | list[str]:
        if isinstance(value, str):
            return _canonical_future_symbol(value)
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_FUTURE_INFO_SYMBOLS:
            raise ValueError(f"symbol must contain 1 to {MAX_FUTURE_INFO_SYMBOLS} futures")
        symbols = [_canonical_future_symbol(item) for item in value]
        if len(set(symbols)) != len(symbols):
            raise ValueError("symbol values must be unique")
        return symbols

    @model_validator(mode="after")
    def _validate_snapshot(self) -> FuturesInfoQuery:
        _check_snapshot(self, allow_symbol=True)
        return self


class FuturesInstrumentsQuery(_DeribitScalarQuery):
    """One current instrument directory read, optionally including recently delivered items."""

    currency: InstrumentCurrency
    expired: StrictBool = False
    underlying_symbol: str | None = None

    @field_validator("underlying_symbol")
    @classmethod
    def _validate_underlying_symbol(cls, value: str | None) -> str | None:
        return _check_underlying(value)

    @model_validator(mode="after")
    def _validate_snapshot(self) -> FuturesInstrumentsQuery:
        _check_snapshot(self)
        return self


class FuturesCurveQuery(_DeribitScalarQuery):
    """Current dated futures curve for one public currency or underlying root."""

    symbol: str = "BTC"
    currency: MarketCurrency | None = None
    expiration: str | None = None
    date: date_type | None = None
    hours_ago: StrictInt | None = None

    @field_validator("symbol")
    @classmethod
    def _validate_curve_symbol(cls, value: str) -> str:
        return _check_underlying(value) or "BTC"

    @field_validator("expiration")
    @classmethod
    def _validate_expiration_month(cls, value: str | None) -> str | None:
        if value is not None and not _EXPIRATION_MONTH_RE.fullmatch(value):
            raise ValueError("expiration must use YYYY-MM")
        return value

    @model_validator(mode="after")
    def _validate_snapshot(self) -> FuturesCurveQuery:
        _check_snapshot(self, allow_symbol=True)
        _currency_for_symbol(self.symbol, self.currency)
        if self.date is not None or self.hours_ago is not None:
            raise ValueError("Deribit public endpoints provide only the current curve")
        return self


class OptionsChainsQuery(_DeribitScalarQuery):
    """Current option chain for one underlying root, with bounded ticker detail calls."""

    symbol: str
    currency: MarketCurrency | None = None
    expiration_date: date_type | None = None
    option_type: Literal["call", "put"] | None = None

    @field_validator("symbol")
    @classmethod
    def _validate_option_symbol(cls, value: str) -> str:
        return _check_underlying(value) or ""

    @model_validator(mode="after")
    def _validate_snapshot(self) -> OptionsChainsQuery:
        _check_snapshot(self, allow_symbol=True)
        if not self.symbol:
            raise ValueError("symbol is required")
        _currency_for_symbol(self.symbol, self.currency)
        return self


class DeribitFuturesHistoricalFetcher(
    Fetcher[FuturesHistoricalQuery, tuple[tuple[str, RpcReply], ...]]
):
    """Historical OHLCV candles from public/get_tradingview_chart_data."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FuturesHistorical"
    capability: ClassVar[Capability] = _capability(
        "deribit_futures_historical", "intraday", "FuturesHistorical"
    )

    def transform_query(self, **kwargs: object) -> FuturesHistoricalQuery:
        """Validate caller fields before making a chart request."""
        return FuturesHistoricalQuery.model_validate(kwargs)

    def extract_data(
        self, params: FuturesHistoricalQuery, ctx: FetchContext
    ) -> tuple[tuple[str, RpcReply], ...]:
        """Fetch a bounded chart result for each requested instrument."""
        start_ms, end_ms = _date_window_ms(params.start_date, params.end_date)
        resolution, _minutes = _INTERVAL_RESOLUTIONS[params.interval]
        return tuple(
            (
                symbol,
                client.rpc_call(
                    "public/get_tradingview_chart_data",
                    {
                        "instrument_name": symbol,
                        "start_timestamp": start_ms,
                        "end_timestamp": end_ms,
                        "resolution": resolution,
                    },
                    timeout=ctx.timeout,
                ),
            )
            for symbol in _symbols(params.symbol)
        )

    def transform_data(
        self,
        raw: tuple[tuple[str, RpcReply], ...],
        params: FuturesHistoricalQuery,
    ) -> FetchResult:
        """Validate complete per-symbol responses and return typed candle rows."""
        symbols = _symbols(params.symbol)
        if len(raw) != len(symbols) or tuple(symbol for symbol, _ in raw) != symbols:
            raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
        return tuple(
            row
            for symbol, reply in raw
            for row in _historical_rows(reply, symbol=symbol, params=params)
        )


def _historical_rows(
    raw: RpcReply,
    *,
    symbol: str,
    params: FuturesHistoricalQuery,
) -> tuple[DeribitFuturesHistorical, ...]:
    """Validate one official chart response and normalize its arrays."""
    result = _object_result(raw.result)
    status = result.get("status")
    ticks = result.get("ticks")
    if status == "no_data":
        if any(
            result.get(name) not in ([], None)
            for name in ("ticks", "open", "high", "low", "close", "volume", "cost")
        ):
            raise DeribitProviderError("DERIBIT_BAD_CHART_RESPONSE")
        return ()
    if status != "ok":
        raise DeribitProviderError("DERIBIT_BAD_CHART_RESPONSE")
    if not isinstance(ticks, list) or not ticks:
        raise DeribitProviderError("DERIBIT_BAD_CHART_RESPONSE")
    if len(ticks) > MAX_HISTORY_CANDLES:
        raise DeribitProviderError("DERIBIT_RESPONSE_ROW_LIMIT")
    open_values = _number_array(result, "open", nullable=True)
    high_values = _number_array(result, "high", nullable=True)
    low_values = _number_array(result, "low", nullable=True)
    close_values = _number_array(result, "close", nullable=False)
    volume_values = _number_array(result, "volume", nullable=True)
    cost_values = _number_array(result, "cost", nullable=True)
    optional_arrays = (open_values, high_values, low_values, volume_values, cost_values)
    for values in optional_arrays:
        if values is not None and len(values) != len(ticks):
            raise DeribitProviderError("DERIBIT_BAD_CHART_RESPONSE")
    if len(close_values) != len(ticks):
        raise DeribitProviderError("DERIBIT_BAD_CHART_RESPONSE")

    start_ms, end_ms = _date_window_ms(params.start_date, params.end_date)
    rows: list[DeribitFuturesHistorical] = []
    previous_tick: int | None = None
    for index, raw_tick in enumerate(ticks):
        if type(raw_tick) is not int or raw_tick < start_ms or raw_tick > end_ms:
            raise DeribitProviderError("DERIBIT_BAD_TIME_BOUNDARY")
        if previous_tick is not None and raw_tick <= previous_tick:
            raise DeribitProviderError("DERIBIT_BAD_CHART_ORDER")
        previous_tick = raw_tick
        row_values: dict[str, object] = {
            "date": _timestamp_from_ms(raw_tick),
            "open": None if open_values is None else open_values[index],
            "high": None if high_values is None else high_values[index],
            "low": None if low_values is None else low_values[index],
            "close": close_values[index],
            "volume": None if volume_values is None else volume_values[index],
            "symbol": symbol,
            "volume_notional": None if cost_values is None else cost_values[index],
        }
        rows.append(_validate_row(DeribitFuturesHistorical, row_values))
    return tuple(rows)


class DeribitFuturesInfoFetcher(Fetcher[FuturesInfoQuery, tuple[RpcReply, ...]]):
    """Live quote and session statistics from public/ticker."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FuturesInfo"
    capability: ClassVar[Capability] = _capability(
        "deribit_futures_info", "snapshot", "FuturesInfo"
    )

    def transform_query(self, **kwargs: object) -> FuturesInfoQuery:
        """Validate one future instrument name and supported filters."""
        return FuturesInfoQuery.model_validate(kwargs)

    def extract_data(self, params: FuturesInfoQuery, ctx: FetchContext) -> tuple[RpcReply, ...]:
        """Fetch one ticker for each bounded, validated instrument name."""
        symbols = _symbols(params.symbol)
        replies = tuple(
            client.rpc_call(
                "public/ticker",
                {"instrument_name": symbol},
                timeout=ctx.timeout,
            )
            for symbol in symbols
        )
        return replies

    def transform_data(
        self,
        raw: tuple[RpcReply, ...],
        params: FuturesInfoQuery,
    ) -> FetchResult:
        """Validate each ticker and return its live quote/session-stat row."""
        symbols = _symbols(params.symbol)
        if len(raw) != len(symbols):
            raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
        rows = tuple(
            _future_info_row(reply.result, expected_symbol=symbol)
            for reply, symbol in zip(raw, symbols, strict=False)
        )
        return rows


class DeribitFuturesInstrumentsFetcher(Fetcher[FuturesInstrumentsQuery, RpcReply]):
    """A bounded current or recently delivered future instrument directory."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FuturesInstruments"
    capability: ClassVar[Capability] = _capability(
        "deribit_futures_instruments", "snapshot", "FuturesInstruments"
    )

    def transform_query(self, **kwargs: object) -> FuturesInstrumentsQuery:
        """Validate currency, expiry mode, and optional directory filters."""
        return FuturesInstrumentsQuery.model_validate(kwargs)

    def extract_data(self, params: FuturesInstrumentsQuery, ctx: FetchContext) -> RpcReply:
        """Fetch one public future instrument directory."""
        return client.rpc_call(
            "public/get_instruments",
            {
                "currency": params.currency,
                "kind": "future",
                "expired": params.expired,
                "extended": False,
            },
            timeout=ctx.timeout,
        )

    def transform_data(self, raw: RpcReply, params: FuturesInstrumentsQuery) -> FetchResult:
        """Validate, deduplicate, filter, and type instrument rows."""
        records = _record_array(raw.result)
        rows: list[DeribitFuturesInstruments] = []
        seen_symbols: set[str] = set()
        for record in records:
            row = _future_instrument_row(record)
            if row.symbol in seen_symbols:
                raise DeribitProviderError("DERIBIT_DUPLICATE_INSTRUMENT")
            seen_symbols.add(row.symbol)
            if _matches_underlying(row.symbol, row.base_currency or "", params.underlying_symbol):
                rows.append(row)
        return tuple(rows)


class DeribitFuturesCurveFetcher(Fetcher[FuturesCurveQuery, tuple[RpcReply, RpcReply]]):
    """Join future definitions to current book summaries and keep dated live contracts."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FuturesCurve"
    capability: ClassVar[Capability] = _capability(
        "deribit_futures_curve", "snapshot", "FuturesCurve"
    )

    def transform_query(self, **kwargs: object) -> FuturesCurveQuery:
        """Validate currency and filters for the current futures curve."""
        return FuturesCurveQuery.model_validate(kwargs)

    def extract_data(
        self, params: FuturesCurveQuery, ctx: FetchContext
    ) -> tuple[RpcReply, RpcReply]:
        """Fetch definitions and current summaries for the requested curve root."""
        currency = _currency_for_symbol(params.symbol, params.currency)
        instruments = client.rpc_call(
            "public/get_instruments",
            {
                "currency": currency,
                "kind": "future",
                "expired": False,
                "extended": False,
            },
            timeout=ctx.timeout,
        )
        summaries = client.rpc_call(
            "public/get_book_summary_by_currency",
            {"currency": currency, "kind": "future"},
            timeout=ctx.timeout,
        )
        return instruments, summaries

    def transform_data(
        self,
        raw: tuple[RpcReply, RpcReply],
        params: FuturesCurveQuery,
    ) -> FetchResult:
        """Join snapshots by instrument name and return dated contracts."""
        instrument_reply, summary_reply = raw
        instrument_records = _record_array(instrument_reply.result)
        summary_records = _record_array(summary_reply.result)
        instruments: dict[str, DeribitFuturesInstruments] = {}
        for record in instrument_records:
            source_instrument = _future_instrument_row(record)
            if source_instrument.symbol in instruments:
                raise DeribitProviderError("DERIBIT_DUPLICATE_INSTRUMENT")
            instruments[source_instrument.symbol] = source_instrument

        rows: list[DeribitFuturesCurve] = []
        seen_summaries: set[str] = set()
        for summary in summary_records:
            symbol = _required_string(summary, "instrument_name")
            if symbol in seen_summaries:
                raise DeribitProviderError("DERIBIT_DUPLICATE_SUMMARY")
            seen_summaries.add(symbol)
            instrument = instruments.get(symbol)
            if instrument is None:
                raise DeribitProviderError("DERIBIT_INCONSISTENT_INSTRUMENTS")
            if instrument.settlement_period == "perpetual" or symbol.endswith("-PERPETUAL"):
                continue
            expiration_time = instrument.expiration_timestamp
            if expiration_time is None:
                raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
            if expiration_time <= summary_reply.observed_at:
                continue
            expiration_date = expiration_time.date()
            if not _matches_underlying(symbol, instrument.base_currency or "", params.symbol):
                continue
            expiration_month = expiration_date.strftime("%Y-%m")
            if params.expiration is not None and expiration_month != params.expiration:
                continue
            row_values: dict[str, object] = {
                "date": summary_reply.observed_at.date(),
                "expiration": expiration_month,
                "price": _optional_number(summary, "mark_price"),
                "symbol": symbol,
                "last_price": _optional_number(summary, "last"),
                "hours_ago": None,
            }
            rows.append(_validate_row(DeribitFuturesCurve, row_values))
        rows.sort(key=lambda row: (row.expiration, row.symbol or ""))
        return tuple(rows)


class DeribitOptionsChainsFetcher(
    Fetcher[
        OptionsChainsQuery,
        tuple[RpcReply, RpcReply, tuple[tuple[str, RpcReply], ...]],
    ]
):
    """Current option quotes joined to official contract metadata and names."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "OptionsChains"
    capability: ClassVar[Capability] = _capability(
        "deribit_options_chains", "snapshot", "OptionsChains"
    )

    def transform_query(self, **kwargs: object) -> OptionsChainsQuery:
        """Validate currency and filters for the current option chain."""
        return OptionsChainsQuery.model_validate(kwargs)

    def extract_data(
        self, params: OptionsChainsQuery, ctx: FetchContext
    ) -> tuple[RpcReply, RpcReply, tuple[tuple[str, RpcReply], ...]]:
        """Fetch the option directory, summary and bounded per-contract tickers."""
        currency = _currency_for_symbol(params.symbol, params.currency)
        instruments = client.rpc_call(
            "public/get_instruments",
            {
                "currency": currency,
                "kind": "option",
                "expired": False,
                "extended": False,
            },
            timeout=ctx.timeout,
        )
        summaries = client.rpc_call(
            "public/get_book_summary_by_currency",
            {"currency": currency, "kind": "option"},
            timeout=ctx.timeout,
        )
        instrument_records = _record_array(instruments.result)
        summary_records = _record_array(summaries.result)
        instrument_names: set[str] = set()
        for record in instrument_records:
            name = _required_string(record, "instrument_name")
            if name in instrument_names:
                raise DeribitProviderError("DERIBIT_DUPLICATE_INSTRUMENT")
            instrument_names.add(name)
            if _required_string(record, "kind") != "option":
                raise DeribitProviderError("DERIBIT_BAD_INSTRUMENT_KIND")
            _required_integer(record, "instrument_id")

        selected: list[str] = []
        seen_summaries: set[str] = set()
        for summary in summary_records:
            name = _required_string(summary, "instrument_name")
            if name in seen_summaries:
                raise DeribitProviderError("DERIBIT_DUPLICATE_OPTION")
            seen_summaries.add(name)
            underlying, expiration, _strike, option_type = _parse_option_symbol(name)
            if not _matches_option_underlying(underlying, params.symbol):
                continue
            if params.expiration_date is not None and expiration != params.expiration_date:
                continue
            if params.option_type is not None and option_type != params.option_type:
                continue
            if expiration < summaries.observed_at.date():
                continue
            if name not in instrument_names:
                raise DeribitProviderError("DERIBIT_INCONSISTENT_INSTRUMENTS")
            selected.append(name)
        if len(selected) > MAX_OPTION_TICKERS:
            raise DeribitProviderError("DERIBIT_OPTION_CHAIN_LIMIT")

        tickers = tuple(
            (
                name,
                client.rpc_call(
                    "public/ticker",
                    {"instrument_name": name},
                    timeout=ctx.timeout,
                ),
            )
            for name in selected
        )
        return instruments, summaries, tickers

    def transform_data(
        self,
        raw: tuple[RpcReply, RpcReply, tuple[tuple[str, RpcReply], ...]],
        params: OptionsChainsQuery,
    ) -> FetchResult:
        """Join current summaries, definitions and detailed ticker quotes."""
        instrument_reply, summary_reply, ticker_replies = raw
        instrument_records = _record_array(instrument_reply.result)
        summary_records = _record_array(summary_reply.result)
        ticker_symbols = tuple(symbol for symbol, _reply in ticker_replies)
        if len(ticker_symbols) > MAX_OPTION_TICKERS or len(set(ticker_symbols)) != len(
            ticker_symbols
        ):
            raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
        instruments: dict[str, dict[str, object]] = {}
        for record in instrument_records:
            symbol = _required_string(record, "instrument_name")
            if symbol in instruments:
                raise DeribitProviderError("DERIBIT_DUPLICATE_INSTRUMENT")
            if _required_string(record, "kind") != "option":
                raise DeribitProviderError("DERIBIT_BAD_INSTRUMENT_KIND")
            # The exchange's instrument id is part of its documented identity;
            # requiring it also prevents a partial directory from being joined.
            _required_integer(record, "instrument_id")
            instruments[symbol] = record

        summaries: dict[str, dict[str, object]] = {}
        for summary in summary_records:
            contract_symbol = _required_string(summary, "instrument_name")
            if contract_symbol in summaries:
                raise DeribitProviderError("DERIBIT_DUPLICATE_OPTION")
            summaries[contract_symbol] = summary
        expected_symbols: list[str] = []
        for contract_symbol in summaries:
            underlying, expiration, _strike, option_type = _parse_option_symbol(contract_symbol)
            if not _matches_option_underlying(underlying, params.symbol):
                continue
            if params.expiration_date is not None and expiration != params.expiration_date:
                continue
            if params.option_type is not None and option_type != params.option_type:
                continue
            if expiration < summary_reply.observed_at.date():
                continue
            expected_symbols.append(contract_symbol)
        if tuple(expected_symbols) != ticker_symbols:
            raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")

        rows: list[DeribitOptionsChains] = []
        for contract_symbol, ticker_reply in ticker_replies:
            summary = summaries[contract_symbol]
            underlying, expiration, strike, option_type = _parse_option_symbol(contract_symbol)
            instrument = instruments.get(contract_symbol)
            if instrument is None:
                raise DeribitProviderError("DERIBIT_INCONSISTENT_INSTRUMENTS")
            if _required_string(instrument, "kind") != "option":
                raise DeribitProviderError("DERIBIT_BAD_INSTRUMENT_KIND")
            instrument_base = _optional_string(instrument, "base_currency")
            summary_base = _optional_string(summary, "base_currency")
            if (
                instrument_base is not None
                and instrument_base != underlying.split("_", maxsplit=1)[0]
            ):
                raise DeribitProviderError("DERIBIT_OPTION_UNDERLYING_MISMATCH")
            if summary_base is not None and summary_base != underlying.split("_", maxsplit=1)[0]:
                raise DeribitProviderError("DERIBIT_OPTION_UNDERLYING_MISMATCH")
            instrument_type = _optional_string(instrument, "instrument_type")
            ticker = _object_result(ticker_reply.result)
            if _required_string(ticker, "instrument_name") != contract_symbol:
                raise DeribitProviderError("DERIBIT_INCONSISTENT_TICKER")
            stats = _optional_object(ticker, "stats")
            if stats is None:
                raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
            greeks = _optional_object(ticker, "greeks")
            timestamp = _timestamp_from_ms(_required_integer(ticker, "timestamp"))
            row_values = _option_chain_row_values(
                contract_symbol=contract_symbol,
                expiration=expiration,
                strike=strike,
                option_type=option_type,
                instrument=instrument,
                instrument_type=instrument_type,
                summary=summary,
                ticker=ticker,
                stats=stats,
                greeks=greeks,
                timestamp=timestamp,
            )
            rows.append(_validate_row(DeribitOptionsChains, row_values))
        rows.sort(
            key=lambda row: (
                row.expiration,
                row.strike,
                row.option_type,
                row.contract_symbol,
            )
        )
        return tuple(rows)


def _check_snapshot(
    params: QueryParams | MultiSymbolQueryParams,
    *,
    allow_symbol: bool = False,
) -> None:
    """Reject date or market filters that a current snapshot endpoint cannot honor."""
    if params.start_date is not None or params.end_date is not None:
        raise ValueError("Deribit snapshots do not accept historical date filters")
    _validate_market(params.market)
    if params.symbol is not None and not allow_symbol:
        raise ValueError("this Deribit snapshot does not accept a symbol filter")


def _canonical_future_symbol(value: object) -> str:
    """Accept a Deribit future name, root, or documented perpetual pair alias."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("symbol must be a nonempty Deribit future identifier")
    if _FUTURE_SYMBOL_RE.fullmatch(value):
        if not value.endswith("-PERPETUAL") and _future_expiration_month(value) is None:
            raise ValueError("dated future symbols must contain a valid DMMMYY expiration")
        return value
    if not _UNDERLYING_RE.fullmatch(value):
        raise ValueError("symbol must be an uppercase Deribit future identifier")
    if "_" in value:
        return f"{value}-PERPETUAL"
    if value in {"BTC", "ETH"}:
        return f"{value}-PERPETUAL"
    for quote in ("USDC", "USDT", "EURR", "BTC", "ETH"):
        base = value[: -len(quote)] if value.endswith(quote) else ""
        if base:
            return f"{base}_{quote}-PERPETUAL"
    raise ValueError("symbol must be a listed perpetual pair alias or full instrument name")


def _future_expiration_month(symbol: str) -> str | None:
    """Return YYYY-MM for a dated Deribit futures symbol, never for perpetuals."""
    match = _DATED_FUTURE_SYMBOL_RE.fullmatch(symbol)
    if match is None:
        return None
    month_number = {
        "JAN": 1,
        "FEB": 2,
        "MAR": 3,
        "APR": 4,
        "MAY": 5,
        "JUN": 6,
        "JUL": 7,
        "AUG": 8,
        "SEP": 9,
        "OCT": 10,
        "NOV": 11,
        "DEC": 12,
    }
    month = month_number.get(match.group("month"))
    if month is None:
        return None
    try:
        date_type(2000 + int(match.group("year")), month, int(match.group("day")))
    except ValueError:
        return None
    return f"20{match.group('year')}-{month:02d}"


def _symbols(value: str | list[str]) -> tuple[str, ...]:
    """Return the canonical ordered ticker symbols from a validated query."""
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _currency_for_symbol(symbol: str, currency: str | None) -> str:
    """Resolve a supported Deribit settlement currency without guessing."""
    if currency is not None:
        if "_" in symbol:
            pair_currency = symbol.rsplit("_", maxsplit=1)[1]
            if pair_currency in {"BTC", "ETH", "USDC", "USDT", "EURR"} and (
                pair_currency != currency
            ):
                raise ValueError("currency does not match the Deribit underlying pair")
        return currency
    if symbol in {"BTC", "ETH", "USDC", "USDT", "EURR"}:
        return symbol
    if "_" in symbol:
        pair_currency = symbol.rsplit("_", maxsplit=1)[1]
        if pair_currency in {"BTC", "ETH", "USDC", "USDT", "EURR"}:
            return pair_currency
    raise ValueError("currency is required when it cannot be inferred from symbol")


def _matches_option_underlying(underlying: str, requested: str) -> bool:
    """Match an exact pair or a root symbol against Deribit option names."""
    if underlying == requested:
        return True
    return "_" not in requested and underlying.split("_", maxsplit=1)[0] == requested


def _validate_market(market: str | None) -> None:
    """Accept the provider's declared global market without sending it upstream."""
    if market not in {None, "global"}:
        raise ValueError("Deribit market must match its declared global capability")


def _check_underlying(value: str | None) -> str | None:
    """Require Deribit's documented uppercase underlying-symbol spelling."""
    if value is not None and (not isinstance(value, str) or not _UNDERLYING_RE.fullmatch(value)):
        raise ValueError("underlying_symbol must be an uppercase Deribit currency or pair")
    return value


def _check_history_size(start: date_type, end: date_type, interval: str) -> None:
    """Refuse ranges whose expected candle count exceeds the in-memory response bound."""
    _resolution, interval_minutes = _INTERVAL_RESOLUTIONS[interval]
    span_milliseconds = ((end - start).days + 1) * 86_400_000 - 1
    expected_candles = span_milliseconds // (interval_minutes * 60_000) + 1
    if expected_candles > MAX_HISTORY_CANDLES:
        raise ValueError(
            f"requested window may exceed the {MAX_HISTORY_CANDLES}-candle response limit"
        )


def _date_window_ms(start: date_type | None, end: date_type | None) -> tuple[int, int]:
    """Convert inclusive UTC calendar dates into inclusive millisecond endpoints."""
    if start is None or end is None:  # defensive guard after query validation
        raise DeribitProviderError("DERIBIT_HISTORY_WINDOW_REQUIRED")
    start_time = datetime.combine(start, time.min, tzinfo=timezone.utc)
    end_time = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return int(start_time.timestamp() * 1_000), int(end_time.timestamp() * 1_000) - 1


def _object_result(result: object) -> dict[str, object]:
    """Require one JSON object result."""
    if not isinstance(result, dict):
        raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
    return cast("dict[str, object]", result)


def _record_array(result: object) -> list[dict[str, object]]:
    """Require a complete bounded array of JSON object records."""
    if not isinstance(result, list) or len(result) > MAX_RESPONSE_ROWS:
        raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
    if any(not isinstance(record, dict) for record in result):
        raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
    return cast("list[dict[str, object]]", result)


def _required_string(record: Mapping[str, object], key: str) -> str:
    """Read a non-empty string source field."""
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
    return value


def _optional_string(record: Mapping[str, object], key: str) -> str | None:
    """Read an optional text field without stringifying arbitrary values."""
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
    return value


def _optional_object(record: Mapping[str, object], key: str) -> dict[str, object] | None:
    """Read a nested JSON object or null without accepting arbitrary values."""
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
    return cast("dict[str, object]", value)


def _required_integer(record: Mapping[str, object], key: str) -> int:
    """Read an exact integer, excluding booleans."""
    value = record.get(key)
    if type(value) is not int:
        raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
    return value


def _timestamp_from_ms(value: int) -> datetime:
    """Convert an exact millisecond timestamp into a UTC datetime."""
    if type(value) is not int or value < 0:
        raise DeribitProviderError("DERIBIT_BAD_TIMESTAMP")
    seconds, milliseconds = divmod(value, 1_000)
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(
            milliseconds=milliseconds
        )
    except (OverflowError, OSError, ValueError):
        raise DeribitProviderError("DERIBIT_BAD_TIMESTAMP") from None


def _required_number(record: Mapping[str, object], key: str) -> float:
    """Read one finite JSON number, rejecting booleans and numeric strings."""
    value = _optional_number(record, key)
    if value is None:
        raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
    return value


def _optional_number(record: Mapping[str, object], key: str) -> float | None:
    """Read an optional finite JSON number, preserving source null or omission."""
    value = record.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
    converted = float(value)
    if not math.isfinite(converted):
        raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
    return converted


def _optional_bool(record: Mapping[str, object], key: str) -> bool | None:
    """Read an optional exact JSON boolean field."""
    value = record.get(key)
    if value is None:
        return None
    if type(value) is not bool:
        raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
    return value


def _optional_timestamp(record: Mapping[str, object], key: str) -> datetime | None:
    """Read an optional millisecond epoch timestamp."""
    value = record.get(key)
    if value is None:
        return None
    if type(value) is not int:
        raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
    return _timestamp_from_ms(value)


def _optional_integer(record: Mapping[str, object], key: str) -> int | None:
    """Read an optional exact integer field."""
    value = record.get(key)
    if value is None:
        return None
    if type(value) is not int:
        raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
    return value


def _future_info_row(record_value: object, *, expected_symbol: str) -> DeribitFuturesInfo:
    """Map a public ticker response to the documented quote/session contract."""
    record = _object_result(record_value)
    symbol = _required_string(record, "instrument_name")
    if symbol != expected_symbol:
        raise DeribitProviderError("DERIBIT_INCONSISTENT_TICKER")
    stats = _optional_object(record, "stats")
    if stats is None:
        raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
    greeks = _optional_object(record, "greeks")
    row_values: dict[str, object] = {
        "symbol": symbol,
        "timestamp": _timestamp_from_ms(_required_integer(record, "timestamp")),
        "state": _optional_string(record, "state"),
        "combo_state": _optional_string(record, "combo_state"),
        "last_price": _optional_number(record, "last_price"),
        "bid": _optional_number(record, "best_bid_price"),
        "ask": _optional_number(record, "best_ask_price"),
        "bid_size": _optional_number(record, "best_bid_amount"),
        "ask_size": _optional_number(record, "best_ask_amount"),
        "implied_bid": _optional_number(record, "implied_bid"),
        "implied_ask": _optional_number(record, "implied_ask"),
        "mark_price": _optional_number(record, "mark_price"),
        "index_price": _optional_number(record, "index_price"),
        "settlement_price": _optional_number(record, "settlement_price"),
        "delivery_price": _optional_number(record, "delivery_price"),
        "estimated_delivery_price": _optional_number(record, "estimated_delivery_price"),
        "min_price": _optional_number(record, "min_price"),
        "max_price": _optional_number(record, "max_price"),
        "high": _optional_number(stats, "high"),
        "low": _optional_number(stats, "low"),
        "change_percent": _optional_number(stats, "price_change"),
        "volume": _optional_number(stats, "volume"),
        "volume_notional": _optional_number(stats, "volume_notional"),
        "volume_usd": _optional_number(stats, "volume_usd"),
        "open_interest": _optional_number(record, "open_interest"),
        "current_funding": _optional_number(record, "current_funding"),
        "funding_8h": _optional_number(record, "funding_8h"),
        "interest_value": _optional_number(record, "interest_value"),
        "interest_rate": _optional_number(record, "interest_rate"),
        "underlying_index": _optional_string(record, "underlying_index"),
        "underlying_price": _optional_number(record, "underlying_price"),
        "implied_volatility": _optional_number(record, "mark_iv"),
        "bid_iv": _optional_number(record, "bid_iv"),
        "ask_iv": _optional_number(record, "ask_iv"),
        "delta": None if greeks is None else _optional_number(greeks, "delta"),
        "gamma": None if greeks is None else _optional_number(greeks, "gamma"),
        "vega": None if greeks is None else _optional_number(greeks, "vega"),
        "theta": None if greeks is None else _optional_number(greeks, "theta"),
        "rho": None if greeks is None else _optional_number(greeks, "rho"),
    }
    return _validate_row(DeribitFuturesInfo, row_values)


def _first_optional_number(
    primary: Mapping[str, object],
    primary_key: str,
    fallback: Mapping[str, object],
    fallback_key: str,
) -> float | None:
    """Prefer a ticker value and use the public summary only when absent/null."""
    value = _optional_number(primary, primary_key)
    return value if value is not None else _optional_number(fallback, fallback_key)


def _first_optional_string(
    primary: Mapping[str, object],
    primary_key: str,
    fallback: Mapping[str, object],
    fallback_key: str,
) -> str | None:
    """Prefer a ticker text value and use its summary fallback if omitted."""
    value = _optional_string(primary, primary_key)
    return value if value is not None else _optional_string(fallback, fallback_key)


def _option_chain_row_values(
    *,
    contract_symbol: str,
    expiration: date_type,
    strike: float,
    option_type: Literal["call", "put"],
    instrument: Mapping[str, object],
    instrument_type: str | None,
    summary: Mapping[str, object],
    ticker: Mapping[str, object],
    stats: Mapping[str, object],
    greeks: Mapping[str, object] | None,
    timestamp: datetime,
) -> dict[str, object]:
    """Map Deribit ticker plus summary facts onto the complete chain schema."""
    return {
        "underlying_symbol": _first_optional_string(
            ticker, "underlying_index", summary, "underlying_index"
        ),
        "underlying_price": _first_optional_number(
            ticker, "underlying_price", summary, "underlying_price"
        ),
        "contract_symbol": contract_symbol,
        "eod_date": None,
        "expiration": expiration,
        "dte": max(0, (expiration - timestamp.date()).days),
        "strike": strike,
        "option_type": option_type,
        "contract_size": _optional_number(instrument, "contract_size"),
        "open_interest": _first_optional_number(ticker, "open_interest", summary, "open_interest"),
        "volume": _first_optional_number(stats, "volume", summary, "volume"),
        "theoretical_price": None,
        "last_trade_price": _first_optional_number(ticker, "last_price", summary, "last"),
        "last_trade_size": None,
        "last_trade_time": None,
        "tick": None,
        "bid": _first_optional_number(ticker, "best_bid_price", summary, "bid_price"),
        "bid_size": _optional_number(ticker, "best_bid_amount"),
        "bid_time": None,
        "bid_exchange": None,
        "ask": _first_optional_number(ticker, "best_ask_price", summary, "ask_price"),
        "ask_size": _optional_number(ticker, "best_ask_amount"),
        "ask_time": None,
        "ask_exchange": None,
        "mark": _first_optional_number(ticker, "mark_price", summary, "mark_price"),
        "open": None,
        "open_bid": None,
        "open_ask": None,
        "high": _first_optional_number(stats, "high", summary, "high"),
        "bid_high": None,
        "ask_high": None,
        "low": _first_optional_number(stats, "low", summary, "low"),
        "bid_low": None,
        "ask_low": None,
        "close": None,
        "close_size": None,
        "close_time": None,
        "close_bid": None,
        "close_bid_size": None,
        "close_bid_time": None,
        "close_ask": None,
        "close_ask_size": None,
        "close_ask_time": None,
        "prev_close": None,
        "change": None,
        "change_percent": _first_optional_number(stats, "price_change", summary, "price_change"),
        "implied_volatility": _first_optional_number(ticker, "mark_iv", summary, "mark_iv"),
        "delta": None if greeks is None else _optional_number(greeks, "delta"),
        "gamma": None if greeks is None else _optional_number(greeks, "gamma"),
        "theta": None if greeks is None else _optional_number(greeks, "theta"),
        "vega": None if greeks is None else _optional_number(greeks, "vega"),
        "rho": None if greeks is None else _optional_number(greeks, "rho"),
        "volume_notional": _first_optional_number(
            stats, "volume_notional", summary, "volume_notional"
        ),
        "bid_iv": _first_optional_number(ticker, "bid_iv", summary, "bid_iv"),
        "ask_iv": _first_optional_number(ticker, "ask_iv", summary, "ask_iv"),
        "interest_rate": _first_optional_number(ticker, "interest_rate", summary, "interest_rate"),
        "settlement_price": _first_optional_number(
            ticker, "settlement_price", summary, "settlement_price"
        ),
        "min_price": _first_optional_number(ticker, "min_price", summary, "min_price"),
        "max_price": _first_optional_number(ticker, "max_price", summary, "max_price"),
        "underlying_spot_price": _optional_number(ticker, "index_price"),
        "is_inverse": None if instrument_type is None else instrument_type == "reversed",
        "timestamp": timestamp,
    }


def _future_instrument_row(record: Mapping[str, object]) -> DeribitFuturesInstruments:
    """Map the complete documented public future-instrument output fields."""
    if _required_string(record, "kind") != "future":
        raise DeribitProviderError("DERIBIT_BAD_INSTRUMENT_KIND")
    steps_value = record.get("tick_size_steps")
    tick_size_steps: list[DeribitTickSizeStep] | None = None
    if steps_value is not None:
        if not isinstance(steps_value, list) or len(steps_value) > 100:
            raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
        tick_size_steps = []
        for raw_step in steps_value:
            if not isinstance(raw_step, dict):
                raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE")
            step_values = {
                "above_price": _required_number(raw_step, "above_price"),
                "tick_size": _required_number(raw_step, "tick_size"),
            }
            tick_size_steps.append(_validate_row(DeribitTickSizeStep, step_values))
    row_values: dict[str, object] = {
        "symbol": _required_string(record, "instrument_name"),
        "instrument_id": _required_integer(record, "instrument_id"),
        "kind": "future",
        "instrument_type": _optional_string(record, "instrument_type"),
        "future_type": _optional_string(record, "future_type"),
        "state": _optional_string(record, "state"),
        "base_currency": _optional_string(record, "base_currency"),
        "quote_currency": _optional_string(record, "quote_currency"),
        "counter_currency": _optional_string(record, "counter_currency"),
        "settlement_currency": _optional_string(record, "settlement_currency"),
        "settlement_period": _optional_string(record, "settlement_period"),
        "underlying_type": _optional_string(record, "underlying_type"),
        "product_group": _optional_string(record, "product_group"),
        "price_index": _optional_string(record, "price_index"),
        "lot_size": _optional_integer(record, "lot_size"),
        "min_trade_amount": _optional_number(record, "min_trade_amount"),
        "tick_size": _optional_number(record, "tick_size"),
        "max_leverage": _optional_integer(record, "max_leverage"),
        "max_non_default_leverage": _optional_number(record, "max_non_default_leverage"),
        "max_liquidation_commission": _optional_number(record, "max_liquidation_commission"),
        "maker_commission": _optional_number(record, "maker_commission"),
        "taker_commission": _optional_number(record, "taker_commission"),
        "block_trade_commission": _optional_number(record, "block_trade_commission"),
        "block_trade_min_trade_amount": _optional_number(record, "block_trade_min_trade_amount"),
        "block_trade_tick_size": _optional_number(record, "block_trade_tick_size"),
        "is_active": _optional_bool(record, "is_active"),
        "contract_size": _optional_number(record, "contract_size"),
        "index_id": _optional_integer(record, "index_id"),
        "tick_size_steps": tick_size_steps,
        "base_currency_uuid": _optional_string(record, "base_currency_uuid"),
        "quote_currency_uuid": _optional_string(record, "quote_currency_uuid"),
        "creation_timestamp": _optional_timestamp(record, "creation_timestamp"),
        "expiration_timestamp": _optional_timestamp(record, "expiration_timestamp"),
    }
    return _validate_row(DeribitFuturesInstruments, row_values)


def _validate_row(row_type: type[_RowT], values: Mapping[str, object]) -> _RowT:
    """Validate the public row and hide implementation-level validation details."""
    try:
        return row_type.model_validate(values)
    except (ValidationError, TypeError, ValueError):
        raise DeribitProviderError("DERIBIT_BAD_RESPONSE_SHAPE") from None


@overload
def _number_array(
    record: Mapping[str, object], key: str, *, nullable: Literal[True]
) -> list[float] | None: ...


@overload
def _number_array(
    record: Mapping[str, object], key: str, *, nullable: Literal[False]
) -> list[float]: ...


def _number_array(record: Mapping[str, object], key: str, *, nullable: bool) -> list[float] | None:
    """Read a candle-array field, returning None only when an optional key is absent."""
    if key not in record:
        if nullable:
            return None
        raise DeribitProviderError("DERIBIT_BAD_CHART_RESPONSE")
    raw_values = record[key]
    if not isinstance(raw_values, list):
        raise DeribitProviderError("DERIBIT_BAD_CHART_RESPONSE")
    values: list[float] = []
    for raw_value in raw_values:
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise DeribitProviderError("DERIBIT_BAD_CHART_RESPONSE")
        value = float(raw_value)
        if not math.isfinite(value):
            raise DeribitProviderError("DERIBIT_BAD_CHART_RESPONSE")
        values.append(value)
    return values


def _parse_option_symbol(symbol: str) -> tuple[str, date_type, float, Literal["call", "put"]]:
    """Parse the official underlying-expiry-strike-kind option name convention."""
    match = _OPTION_SYMBOL_RE.fullmatch(symbol)
    if match is None:
        raise DeribitProviderError("DERIBIT_BAD_OPTION_SYMBOL")
    month_number = {
        "JAN": 1,
        "FEB": 2,
        "MAR": 3,
        "APR": 4,
        "MAY": 5,
        "JUN": 6,
        "JUL": 7,
        "AUG": 8,
        "SEP": 9,
        "OCT": 10,
        "NOV": 11,
        "DEC": 12,
    }
    expiry_text = match.group("expiry")
    try:
        expiration = date_type(
            2000 + int(expiry_text[-2:]),
            month_number[expiry_text[2:5]],
            int(expiry_text[:2]),
        )
    except (KeyError, OverflowError, ValueError):
        raise DeribitProviderError("DERIBIT_BAD_OPTION_SYMBOL") from None
    try:
        strike = float(Decimal(match.group("strike").replace("d", ".")))
    except (InvalidOperation, OverflowError, ValueError):
        raise DeribitProviderError("DERIBIT_BAD_OPTION_SYMBOL") from None
    if not math.isfinite(strike):
        raise DeribitProviderError("DERIBIT_BAD_OPTION_SYMBOL")
    option_type: Literal["call", "put"] = "call" if match.group("option_type") == "C" else "put"
    return match.group("underlying"), expiration, strike, option_type


def _matches_underlying(
    symbol: str,
    base_currency: str,
    requested: str | None,
) -> bool:
    """Match either the exact derivative pair or its base currency."""
    if requested is None:
        return True
    return base_currency == requested or symbol.startswith(f"{requested}-")
