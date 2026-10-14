"""Typed adapters for documented, delayed Cboe All Access endpoints."""

from __future__ import annotations

import math
import re
from datetime import date as date_type
from datetime import datetime
from typing import Any, ClassVar, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.base import (
    ContractModel,  # noqa: TC001 - runtime reflection resolves _validate_records
)
from opendata.data.models.cboe_index import CboeEquitySearchListing
from opendata.data.models.congress_cboe_iter2 import (
    CboeEquityHistorical,
    CboeEquityQuote,
    CboeEtfHistorical,
    CboeOptionsChains,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.cboe.models import _all_access_client as _client

_MARKET_TZ = ZoneInfo("America/New_York")
_EARLIEST_HISTORY_DATE = date_type(2011, 1, 1)
_MAX_HISTORY_CALENDAR_DAYS = 28
_MAX_HISTORY_TRADING_DAYS = 20
_MAX_CHAIN_CONTRACTS = 5_000
_SYMBOL_PATTERN = re.compile(r"[A-Z0-9.^_$-]{1,32}")


def _market_today() -> date_type:
    """Return today's date in the exchange's documented market timezone."""
    return datetime.now(_MARKET_TZ).date()


def _validate_snapshot_date(value: date_type) -> None:
    """Enforce Cboe's documented historical floor and disallow future snapshots."""
    if value < _EARLIEST_HISTORY_DATE:
        raise ValueError("Cboe All Access historical snapshots start in 2011")
    if value > _market_today():
        raise ValueError("snapshot date must not be in the future")


class _CboeAllAccessQuery(QueryParams):
    """Base identity accepted by provider_model_query's market/source injection."""

    source: Literal["cboe"] = "cboe"
    market: Literal["us"] = "us"


class _CboeSymbolQuery(_CboeAllAccessQuery):
    """One documented Cboe underlying symbol."""

    symbol: str

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not _SYMBOL_PATTERN.fullmatch(normalized):
            raise ValueError("symbol must be one safe Cboe symbol")
        return normalized


class CboeHistoricalQuery(_CboeSymbolQuery):
    """One bounded daily history window; the documented 1m bar is not synthesized."""

    start_date: date_type
    end_date: date_type
    interval: Literal["1d"] = "1d"

    @model_validator(mode="after")
    def _validate_window(self) -> CboeHistoricalQuery:
        if self.start_date < _EARLIEST_HISTORY_DATE:
            raise ValueError("Cboe All Access underlying history starts in 2011")
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if (self.end_date - self.start_date).days + 1 > _MAX_HISTORY_CALENDAR_DAYS:
            raise ValueError("history window exceeds the 28-calendar-day local bound")
        if self.end_date >= _market_today():
            raise ValueError("Cboe daily history excludes the current market date")
        return self


class CboeQuoteQuery(_CboeSymbolQuery):
    """One daily snapshot; current-market date defaults in New York time."""

    date: date_type = Field(default_factory=_market_today)

    @model_validator(mode="after")
    def _reject_window(self) -> CboeQuoteQuery:
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("equity quote accepts date, not a historical window")
        _validate_snapshot_date(self.date)
        return self


class CboeEquitySearchQuery(_CboeAllAccessQuery):
    """Optional local search over the documented All Access symbols collection."""

    query: str | None = None
    is_symbol: bool = False

    @field_validator("query")
    @classmethod
    def _normalize_search_query(cls, value: str | None) -> str | None:
        normalized = value.strip() if value is not None else None
        return normalized or None


class CboeOptionsChainsQuery(_CboeSymbolQuery):
    """One date-bounded options snapshot with optional documented server-side filters."""

    date: date_type = Field(default_factory=_market_today)
    root: str | None = None
    option_type: Literal["C", "P"] | None = None
    min_expiry: date_type | None = None
    max_expiry: date_type | None = None
    min_strike: float | None = Field(default=None, ge=0)
    max_strike: float | None = Field(default=None, ge=0)

    @field_validator("root")
    @classmethod
    def _normalize_root(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not _SYMBOL_PATTERN.fullmatch(normalized):
            raise ValueError("root must be one safe Cboe option root")
        return normalized

    @field_validator("option_type", mode="before")
    @classmethod
    def _normalize_option_type(cls, value: object) -> object:
        return value.upper().strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_filters(self) -> CboeOptionsChainsQuery:
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("options chains accepts one source date, not a history window")
        _validate_snapshot_date(self.date)
        if (
            self.min_expiry is not None
            and self.max_expiry is not None
            and self.min_expiry > self.max_expiry
        ):
            raise ValueError("min_expiry must not be after max_expiry")
        if (
            self.min_strike is not None
            and self.max_strike is not None
            and self.min_strike > self.max_strike
        ):
            raise ValueError("min_strike must not be greater than max_strike")
        return self


class CboeEquityHistoricalFetcher(Fetcher[CboeHistoricalQuery, list[dict[str, Any]]]):
    """Fetch daily equity bars from Cboe's documented historical snapshot route."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "EquityHistorical"
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="cboe_equity_historical",
        period="1d",
        market="us",
        source="cboe",
        verified=False,
        notes=(
            "Delayed All Access API; daily OHLCV only. The API token is caller supplied and "
            "does not imply storage or redistribution rights."
        ),
    )

    def transform_query(self, **kwargs: object) -> CboeHistoricalQuery:
        """Validate source/date bounds before any token or transport access."""
        return CboeHistoricalQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: CboeHistoricalQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Read trading dates and one official daily quote snapshot per trading date."""
        return _fetch_daily_bars(params, ctx)

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: CboeHistoricalQuery,
    ) -> FetchResult:
        """Validate daily bars into the public Cboe historical row shape."""
        del params
        return _validate_records(CboeEquityHistorical, raw, "CBOE_LIVEVOL_BAD_HISTORY_ROW")


class CboeEtfHistoricalFetcher(CboeEquityHistoricalFetcher):
    """Fetch daily ETF bars through the same documented underlying-history route."""

    canonical_model = "EtfHistorical"
    capability = Capability(
        asset_class="etf",
        domain="cboe_etf_historical",
        period="1d",
        market="us",
        source="cboe",
        verified=False,
        notes=(
            "Cboe's public historical model documents ETF use of the same daily route. "
            "The API token is caller supplied; rights remain unapproved."
        ),
    )

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: CboeHistoricalQuery,
    ) -> FetchResult:
        """Validate ETF daily bars into the Cboe ETF historical contract."""
        del params
        return _validate_records(CboeEtfHistorical, raw, "CBOE_LIVEVOL_BAD_HISTORY_ROW")


class CboeEquityQuoteFetcher(Fetcher[CboeQuoteQuery, list[dict[str, Any]]]):
    """Fetch one delayed Cboe quote snapshot for a company symbol."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "EquityQuote"
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="cboe_equity_quote",
        period="snapshot",
        market="us",
        source="cboe",
        verified=False,
        notes=(
            "Delayed All Access quote fields only; quote timestamps and venue/condition fields "
            "not published by this route remain null. Rights remain unapproved."
        ),
    )

    def transform_query(self, **kwargs: object) -> CboeQuoteQuery:
        """Validate one symbol and source date before any live access."""
        return CboeQuoteQuery.model_validate(kwargs)

    def extract_data(self, params: CboeQuoteQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Read and map one exact-symbol response from the quote collection."""
        payload = _client.get_json(
            "/market/underlying-quotes",
            params={"symbols": params.symbol, "date": params.date.isoformat(), "seq_no": "0"},
            context=ctx,
        )
        rows = _client.require_rows(payload)
        selected = [row for row in rows if _source_symbol(row) == params.symbol]
        if len(selected) > 1 or any(_source_symbol(row) != params.symbol for row in rows):
            raise _client.CboeAllAccessError("CBOE_LIVEVOL_SYMBOL_MISMATCH")
        if not selected:
            return []
        return [_map_quote(selected[0], params.symbol)]

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: CboeQuoteQuery,
    ) -> FetchResult:
        """Validate the quote as one typed source-specific row."""
        del params
        return _validate_records(CboeEquityQuote, raw, "CBOE_LIVEVOL_BAD_QUOTE_ROW")


class CboeEquitySearchFetcher(Fetcher[CboeEquitySearchQuery, list[dict[str, Any]]]):
    """Search the authenticated All Access symbols endpoint, without web-page scraping."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "EquitySearch"
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="cboe_equity_search",
        period="snapshot",
        market="us",
        source="cboe",
        verified=False,
        notes=(
            "Uses the documented reference/symbols API. DPM and floor post/station fields are "
            "nullable and not provided by this endpoint. Rights remain unapproved."
        ),
    )

    def transform_query(self, **kwargs: object) -> CboeEquitySearchQuery:
        """Validate optional local filters before any API request."""
        return CboeEquitySearchQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: CboeEquitySearchQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Fetch published symbol/name pairs and apply the public search filter locally."""
        rows = _client.require_rows(_client.get_json("/reference/symbols", params={}, context=ctx))
        if len(rows) > 100_000:
            raise _client.CboeAllAccessError("CBOE_LIVEVOL_SYMBOL_LIMIT")
        needle = params.query.casefold() if params.query is not None else None
        result: list[dict[str, Any]] = []
        for row in rows:
            symbol = row.get("name")
            company_name = row.get("company_name")
            if (
                not isinstance(symbol, str)
                or not _SYMBOL_PATTERN.fullmatch(symbol.upper())
                or not isinstance(company_name, str)
                or not company_name.strip()
            ):
                raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_SYMBOL_ROW")
            if needle is not None:
                candidate = symbol if params.is_symbol else company_name
                if needle not in candidate.casefold():
                    continue
            result.append(
                {
                    "symbol": symbol,
                    "name": company_name.strip(),
                    "dpm_name": None,
                    "post_station": None,
                }
            )
        return result

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: CboeEquitySearchQuery,
    ) -> FetchResult:
        """Validate symbol records against the published Cboe search contract."""
        del params
        return _validate_records(
            CboeEquitySearchListing,
            raw,
            "CBOE_LIVEVOL_BAD_SYMBOL_ROW",
        )


class CboeOptionsChainsFetcher(Fetcher[CboeOptionsChainsQuery, dict[str, Any] | None]):
    """Fetch synchronized delayed option/underlying snapshots from All Access."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "OptionsChains"
    capability: ClassVar[Capability] = Capability(
        asset_class="option",
        domain="cboe_options_chains",
        period="snapshot",
        market="us",
        source="cboe",
        verified=False,
        notes=(
            "Uses the documented option-and-underlying-quotes API. Optional output fields not "
            "published by that route remain null. Rights remain unapproved."
        ),
    )

    def transform_query(self, **kwargs: object) -> CboeOptionsChainsQuery:
        """Validate the option chain date, symbol, and source-side filters."""
        return CboeOptionsChainsQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: CboeOptionsChainsQuery,
        ctx: FetchContext,
    ) -> dict[str, Any] | None:
        """Read one source collection item and assemble the public column-oriented result."""
        request_params = {
            "symbol": params.symbol,
            "date": params.date.isoformat(),
        }
        for name in ("root", "option_type", "min_expiry", "max_expiry", "min_strike", "max_strike"):
            value = getattr(params, name)
            if value is not None:
                request_params[name] = (
                    value.isoformat() if isinstance(value, date_type) else str(value)
                )
        payload = _client.get_json(
            "/market/option-and-underlying-quotes",
            params=request_params,
            context=ctx,
        )
        if not isinstance(payload, list):
            raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_CHAIN_SHAPE")
        rows = _client.require_rows(payload, code="CBOE_LIVEVOL_BAD_CHAIN_SHAPE")
        selected = [row for row in rows if _source_symbol(row) == params.symbol]
        if len(selected) > 1 or any(_source_symbol(row) != params.symbol for row in rows):
            raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_CHAIN_SHAPE")
        if not selected:
            return None
        response = selected[0]
        options = response.get("options")
        if not isinstance(options, list):
            raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_CHAIN_SHAPE")
        if len(options) > _MAX_CHAIN_CONTRACTS:
            raise _client.CboeAllAccessError("CBOE_LIVEVOL_CHAIN_LIMIT")
        if any(not isinstance(option, dict) for option in options):
            raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_CHAIN_SHAPE")
        return _map_chain(response, options, params)

    def transform_data(
        self,
        raw: dict[str, Any] | None,
        params: CboeOptionsChainsQuery,
    ) -> FetchResult:
        """Validate column lengths and serialize the public Cboe chain shape."""
        del params
        if raw is None:
            return ()
        try:
            return (CboeOptionsChains.model_validate(raw),)
        except (TypeError, ValueError):
            raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_CHAIN_ROW") from None


def _fetch_daily_bars(params: CboeHistoricalQuery, ctx: FetchContext) -> list[dict[str, Any]]:
    trading_dates = _trading_dates(params, ctx)
    if len(trading_dates) > _MAX_HISTORY_TRADING_DAYS:
        raise _client.CboeAllAccessError("CBOE_LIVEVOL_HISTORY_REQUEST_LIMIT")
    result: list[dict[str, Any]] = []
    for trading_date in trading_dates:
        rows = _client.require_rows(
            _client.get_json(
                "/market/underlying-quotes",
                params={
                    "symbols": params.symbol,
                    "date": trading_date.isoformat(),
                    "seq_no": "0",
                },
                context=ctx,
            )
        )
        if any(_source_symbol(row) != params.symbol for row in rows):
            raise _client.CboeAllAccessError("CBOE_LIVEVOL_SYMBOL_MISMATCH")
        matching = [row for row in rows if _source_symbol(row) == params.symbol]
        if len(matching) > 1:
            raise _client.CboeAllAccessError("CBOE_LIVEVOL_DUPLICATE_QUOTE")
        if matching:
            result.append(_map_daily_bar(matching[0], params.symbol, trading_date))
    return result


def _trading_dates(
    params: CboeHistoricalQuery,
    ctx: FetchContext,
) -> tuple[date_type, ...]:
    payload = _client.get_json(
        "/reference/trading-days",
        params={
            "start_date": params.start_date.isoformat(),
            "end_date": params.end_date.isoformat(),
        },
        context=ctx,
    )
    if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
        raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_TRADING_DAYS")
    try:
        dates = tuple(date_type.fromisoformat(item) for item in payload)
    except ValueError:
        raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_TRADING_DAYS") from None
    if (
        len(dates) != len(set(dates))
        or any(item < params.start_date or item > params.end_date for item in dates)
        or tuple(sorted(dates)) != dates
    ):
        raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_TRADING_DAYS")
    return dates


def _map_daily_bar(
    row: dict[str, Any],
    symbol: str,
    trading_date: date_type,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "date": trading_date,
        "open": _required_number(row, "underlying_open"),
        "high": _required_number(row, "underlying_high"),
        "low": _required_number(row, "underlying_low"),
        "close": _required_number(row, "underlying_close"),
        "volume": _optional_number(row, "underlying_volume"),
        "vwap": None,
        "calls_volume": None,
        "puts_volume": None,
        "total_options_volume": None,
    }


def _map_quote(row: dict[str, Any], symbol: str) -> dict[str, Any]:
    mapping = {
        "symbol": symbol,
        "asset_type": None,
        "name": None,
        "exchange": None,
        "bid": _optional_number(row, "underlying_bid"),
        "bid_size": _optional_integer(row, "underlying_bid_size"),
        "bid_exchange": None,
        "ask": _optional_number(row, "underlying_ask"),
        "ask_size": _optional_integer(row, "underlying_ask_size"),
        "ask_exchange": None,
        "quote_conditions": None,
        "quote_indicators": None,
        "sales_conditions": None,
        "sequence_number": _optional_integer(row, "seq_no"),
        "market_center": None,
        "participant_timestamp": None,
        "trf_timestamp": None,
        "sip_timestamp": None,
        "last_price": _optional_number(row, "underlying_last_trade_price"),
        "last_tick": None,
        "last_size": _optional_integer(row, "underlying_last_trade_size"),
        "last_timestamp": None,
        "open": _optional_number(row, "underlying_open"),
        "high": _optional_number(row, "underlying_high"),
        "low": _optional_number(row, "underlying_low"),
        "close": _optional_number(row, "underlying_close"),
        "volume": _optional_number(row, "underlying_volume"),
        "exchange_volume": None,
        "prev_close": _optional_number(row, "underlying_prev_day_close"),
        "change": None,
        "change_percent": None,
        "year_high": None,
        "year_low": None,
        "iv30": _optional_number(row, "iv30"),
        "iv30_change": _optional_number(row, "iv30_change"),
        "iv30_change_percent": _optional_number(row, "iv30_change_percent"),
    }
    for field in (
        "iv30_annual_high",
        "hv30_annual_high",
        "iv30_annual_low",
        "hv30_annual_low",
        "iv60_annual_high",
        "hv60_annual_high",
        "iv60_annual_low",
        "hv60_annual_low",
        "iv90_annual_high",
        "hv90_annual_high",
        "iv90_annual_low",
        "hv90_annual_low",
    ):
        mapping[field] = None
    return mapping


def _map_chain(
    payload: dict[str, Any],
    options: list[dict[str, Any]],
    params: CboeOptionsChainsQuery,
) -> dict[str, Any]:
    count = len(options)
    result: dict[str, Any] = {
        "underlying_symbol": (params.symbol,) * count,
        "underlying_price": (_optional_number(payload, "underlying_last_trade_price"),) * count,
        "contract_symbol": tuple(_required_text(option, "option") for option in options),
        "eod_date": (params.date,) * count,
        "expiration": tuple(_required_date(option, "expiry") for option in options),
        "dte": tuple((_required_date(option, "expiry") - params.date).days for option in options),
        "strike": tuple(_required_number(option, "strike") for option in options),
        "option_type": tuple(_option_type(option) for option in options),
    }
    source_fields = {
        "open_interest": ("open_interest", _optional_integer),
        "volume": ("option_volume", _optional_integer),
        "theoretical_price": ("cboe_theo", _optional_number),
        "last_trade_price": ("option_last_trade_price", _optional_number),
        "bid": ("option_bid", _optional_number),
        "bid_size": ("option_bid_size", _optional_integer),
        "ask": ("option_ask", _optional_number),
        "ask_size": ("option_ask_size", _optional_integer),
        "mark": ("option_mid", _optional_number),
        "open": ("option_open", _optional_number),
        "high": ("option_high", _optional_number),
        "low": ("option_low", _optional_number),
        "close": ("option_close", _optional_number),
        "prev_close": ("option_prev_day_close", _optional_number),
        "implied_volatility": ("iv", _optional_number),
        "delta": ("delta", _optional_number),
        "gamma": ("gamma", _optional_number),
        "theta": ("theta", _optional_number),
        "vega": ("vega", _optional_number),
        "rho": ("rho", _optional_number),
    }
    for output_field, (source_field, converter) in source_fields.items():
        result[output_field] = tuple(converter(option, source_field) for option in options)
    for field in (
        "contract_size",
        "last_trade_size",
        "last_trade_time",
        "tick",
        "bid_time",
        "bid_exchange",
        "ask_time",
        "ask_exchange",
        "open_bid",
        "open_ask",
        "bid_high",
        "ask_high",
        "bid_low",
        "ask_low",
        "close_size",
        "close_time",
        "close_bid",
        "close_bid_size",
        "close_bid_time",
        "close_ask",
        "close_ask_size",
        "close_ask_time",
        "change",
        "change_percent",
    ):
        result[field] = (None,) * count
    return result


def _option_type(row: dict[str, Any]) -> Literal["call", "put"]:
    value = row.get("option_type")
    if value == "C":
        return "call"
    if value == "P":
        return "put"
    raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_OPTION_TYPE")


def _source_symbol(row: dict[str, Any]) -> str | None:
    value = row.get("symbol")
    return value.upper() if isinstance(value, str) else None


def _required_text(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_REQUIRED_FIELD")
    return value.strip()


def _required_number(row: dict[str, Any], field: str) -> float:
    value = _optional_number(row, field)
    if value is None:
        raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_REQUIRED_FIELD")
    return float(value)


def _optional_number(row: dict[str, Any], field: str) -> int | float | None:
    value = row.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_NUMERIC_FIELD")
    if not math.isfinite(float(value)):
        raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_NUMERIC_FIELD")
    return value


def _optional_integer(row: dict[str, Any], field: str) -> int | None:
    value = row.get(field)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_INTEGER_FIELD")
    return value


def _required_date(row: dict[str, Any], field: str) -> date_type:
    value = row.get(field)
    if not isinstance(value, str):
        raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_DATE_FIELD")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError:
        raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_DATE_FIELD") from None
    if parsed.isoformat() != value:
        raise _client.CboeAllAccessError("CBOE_LIVEVOL_BAD_DATE_FIELD")
    return parsed


def _validate_records(
    contract: type[ContractModel],
    rows: list[dict[str, Any]],
    error_code: str,
) -> FetchResult:
    try:
        return tuple(contract.model_validate(row) for row in rows)
    except (TypeError, ValueError):
        raise _client.CboeAllAccessError(error_code) from None
