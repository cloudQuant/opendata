"""Clean-room ETF performance and current Nasdaq snapshot adapters for Intrinio.

Only public Intrinio v2 JSON endpoints are used. Option and historical security snapshots are
deliberately not implemented because their public endpoint contracts stop at opaque download
metadata and do not publish the downloadable row schema.
"""

from __future__ import annotations

import math
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote

from pydantic import ConfigDict, Field, StrictStr, field_validator

from opendata.data.capability import Capability
from opendata.data.models.intrinio_snapshots_clean import (
    IntrinioEtfPricePerformance,
    IntrinioMarketSnapshot,
)
from opendata.data.protocol import FetchContext, MultiSymbolQueryParams, QueryParams
from opendata.data.providers.intrinio._source import SOURCE
from opendata.data.providers.intrinio.models.iter2_market_clean import (
    IntrinioPaginationLimitError,
    IntrinioResponseShapeError,
    _IntrinioMarketFetcher,
)

_PAGE_SIZE = 1000
_MAX_PAGES = 16
_MAX_ETF_SYMBOLS = 3
_PriceRow = tuple[date, float, float, float | None]


class IntrinioSnapshotQueryError(ValueError):
    """Query cannot be answered by the documented Intrinio snapshot endpoint."""


class _EtfPricePerformanceQuery(MultiSymbolQueryParams):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: StrictStr | list[StrictStr]
    source: Literal["auto", "intrinio"] = "auto"
    market: Literal["us"] | None = None
    return_type: Literal["trailing", "calendar"] | None = "trailing"
    adjustment: Literal["splits_only", "splits_and_dividends"] | None = "splits_and_dividends"

    @field_validator("return_type", mode="before")
    @classmethod
    def _default_return_type(cls, value: object) -> object:
        return "trailing" if value is None else value

    @field_validator("adjustment", mode="before")
    @classmethod
    def _default_adjustment(cls, value: object) -> object:
        return "splits_and_dividends" if value is None else value

    @field_validator("symbol")
    @classmethod
    def _normalize_symbols(cls, value: str | list[str]) -> str | list[str]:
        symbols = [value] if isinstance(value, str) else value
        if not 1 <= len(symbols) <= _MAX_ETF_SYMBOLS:
            raise ValueError(f"symbol must contain 1 to {_MAX_ETF_SYMBOLS} ETF tickers")
        normalized: list[str] = []
        for symbol in symbols:
            item = symbol.strip().upper()
            if not item or any(ord(char) < 33 or ord(char) == 127 for char in item):
                raise ValueError("symbol contains an invalid ticker")
            normalized.append(item)
        if len(set(normalized)) != len(normalized):
            raise ValueError("symbol tickers must be unique")
        return normalized[0] if isinstance(value, str) else normalized


class _MarketSnapshotsQuery(QueryParams):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    market: Literal["nasdaq"] | None = "nasdaq"
    source: Literal["auto", "intrinio"] = "auto"
    requested_date: date | datetime | None = Field(default=None, alias="date")

    @field_validator("market", mode="before")
    @classmethod
    def _default_market(cls, value: object) -> object:
        return "nasdaq" if value is None else value


def _capability(domain: str, market: str) -> Capability:
    return Capability(
        asset_class="etf" if domain == "etf_price_performance" else "equity",
        domain=f"intrinio_{domain}",
        period="snapshot",
        market=market,
        source=SOURCE,
        verified=False,
        notes="official-docs-and-offline-contract-only",
    )


def _numeric(
    value: object,
    field: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IntrinioResponseShapeError(f"Intrinio {field} must be a JSON number or null")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise IntrinioResponseShapeError(f"Intrinio {field} must be finite") from exc
    if not math.isfinite(result) or (positive and result <= 0) or (non_negative and result < 0):
        requirement = "positive" if positive else "nonnegative" if non_negative else "finite"
        raise IntrinioResponseShapeError(f"Intrinio {field} must be a finite {requirement} number")
    return result


def _calendar_month_offset(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_month = divmod(month_index, 12)
    month = zero_month + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _iso_date(value: object, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise IntrinioResponseShapeError(f"Intrinio {field} must be an ISO calendar date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise IntrinioResponseShapeError(f"Intrinio {field} must be an ISO calendar date") from exc


def _aware_timestamp(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise IntrinioResponseShapeError(f"Intrinio {field} must be a timestamp string or null")
    candidate = value.strip()
    if not candidate:
        raise IntrinioResponseShapeError(f"Intrinio {field} must not be an empty timestamp")
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntrinioResponseShapeError(f"Intrinio {field} must be ISO 8601") from exc
    # Preserve the exact source string separately when its timezone is not declared.
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _adjusted_close_series(rows: list[dict[str, Any]]) -> list[_PriceRow]:
    """Validate rows and select Intrinio's documented split-and-dividend adjusted close."""
    parsed: list[tuple[date, float, float | None, float | None]] = []
    seen: set[date] = set()
    for row in rows:
        if any(field not in row for field in ("date", "close")):
            raise IntrinioResponseShapeError("daily price row requires date and close")
        row_date = _iso_date(row["date"], "price date")
        if row_date in seen:
            raise IntrinioResponseShapeError("daily price history contains duplicate dates")
        seen.add(row_date)
        if row.get("frequency") not in (None, "daily"):
            raise IntrinioResponseShapeError("daily price endpoint returned a non-daily row")
        close = _numeric(row.get("close"), "close", positive=True)
        if close is None:
            raise IntrinioResponseShapeError("daily price close must not be null")
        adj_close = _numeric(row.get("adj_close"), "adj_close", positive=True)
        volume = _numeric(row.get("volume"), "volume", non_negative=True)
        parsed.append((row_date, close, adj_close, volume))

    if not parsed:
        raise IntrinioResponseShapeError("Intrinio returned no daily ETF price history")
    parsed.sort(key=lambda item: item[0])
    adjusted: list[_PriceRow] = []
    for row_date, close, adj_close, volume in parsed:
        if adj_close is None:
            raise IntrinioResponseShapeError("daily price history is missing adj_close values")
        adjusted.append((row_date, adj_close, close, volume))
    return adjusted


def _value_on_or_before(prices: list[_PriceRow], anchor: date) -> float | None:
    # Price series are trading-session observations, so calendar anchors commonly land on
    # weekends/holidays. Use the last published close on or before the anchor; an arbitrary
    # calendar-day freshness cap would incorrectly null valid year-to-date values after holidays.
    for row_date, price, _, _ in reversed(prices):
        if row_date <= anchor:
            return price
    return None


def _return_fraction(prices: list[_PriceRow], start: date, end: date) -> float | None:
    starting_price = _value_on_or_before(prices, start)
    ending_price = _value_on_or_before(prices, end)
    if starting_price is None or ending_price is None:
        return None
    result = ending_price / starting_price - 1.0
    if not math.isfinite(result):
        raise IntrinioResponseShapeError("computed price return is not finite")
    return result


def _returns_for_history(
    prices: list[_PriceRow], return_type: str
) -> dict[str, float | date | None]:
    dates = [row[0] for row in prices]
    as_of = dates[-1]
    latest_price = prices[-1][2]
    previous_date = dates[-2] if len(dates) > 1 else None
    week_start = as_of - timedelta(days=as_of.weekday())
    quarter_start_month = ((as_of.month - 1) // 3) * 3 + 1
    quarter_start = date(as_of.year, quarter_start_month, 1)
    month_start = date(as_of.year, as_of.month, 1)
    year_start = date(as_of.year, 1, 1)

    values: dict[str, float | date | None] = {
        "one_day": (
            _return_fraction(prices, previous_date, as_of) if previous_date is not None else None
        ),
        "wtd": _return_fraction(prices, week_start - timedelta(days=1), as_of),
        "one_week": _return_fraction(prices, as_of - timedelta(days=7), as_of),
        "mtd": _return_fraction(prices, month_start - timedelta(days=1), as_of),
        "one_month": _return_fraction(prices, _calendar_month_offset(as_of, 1), as_of),
        "qtd": _return_fraction(prices, quarter_start - timedelta(days=1), as_of),
        "three_month": _return_fraction(prices, _calendar_month_offset(as_of, 3), as_of),
        "six_month": _return_fraction(prices, _calendar_month_offset(as_of, 6), as_of),
        "ytd": _return_fraction(prices, year_start - timedelta(days=1), as_of),
        "max": _return_fraction(prices, dates[0], as_of),
    }
    for years, field in (
        (1, "one_year"),
        (2, "two_year"),
        (3, "three_year"),
        (4, "four_year"),
        (5, "five_year"),
        (10, "ten_year"),
    ):
        if return_type == "trailing":
            anchor = date(
                as_of.year - years,
                as_of.month,
                min(as_of.day, monthrange(as_of.year - years, as_of.month)[1]),
            )
            values[field] = _return_fraction(prices, anchor, as_of)
        else:
            completed_year = as_of.year - years
            values[field] = _return_fraction(
                prices,
                date(completed_year, 1, 1) - timedelta(days=1),
                date(completed_year, 12, 31),
            )
    values["price"] = latest_price
    values["volume"] = prices[-1][3]
    values["updated"] = as_of
    return values


def _query_symbols(params: _EtfPricePerformanceQuery) -> list[str]:
    return [params.symbol] if isinstance(params.symbol, str) else list(params.symbol)


class IntrinioEtfPricePerformanceFetcher(_IntrinioMarketFetcher):
    """Compute OpenBB ETF return fields from documented Intrinio daily price rows."""

    canonical_model = "EtfPricePerformance"
    query_model = _EtfPricePerformanceQuery
    capability = _capability("etf_price_performance", "us")

    def _validate_etf_query(self, params: _EtfPricePerformanceQuery) -> None:
        if params.start_date is not None or params.end_date is not None:
            raise IntrinioSnapshotQueryError("EtfPricePerformance does not accept a date window")

    def transform_query(self, **kwargs: object) -> _EtfPricePerformanceQuery:
        """Validate ETF symbols and reject adjustment variants lacking source semantics."""
        params = self.query_model.model_validate(kwargs)
        self._validate_etf_query(params)
        if params.adjustment != "splits_and_dividends":
            raise IntrinioSnapshotQueryError(
                "Intrinio splits_only is unsupported: official documentation does not define "
                "split_ratio direction or its price-adjustment multiplier"
            )
        return params

    def _get_bounded_pages(
        self,
        path: str,
        params: dict[str, str],
        rows_key: str,
        ctx: FetchContext,
        *,
        identity_key: str,
        expected_ticker: str,
    ) -> list[dict[str, Any]]:
        cursor: str | None = None
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for _ in range(_MAX_PAGES):
            query = dict(params)
            query["page_size"] = str(_PAGE_SIZE)
            if cursor is not None:
                query["next_page"] = cursor
            payload = self._get_json(path, query, ctx)
            identity = payload.get(identity_key)
            if not isinstance(identity, dict):
                raise IntrinioResponseShapeError(
                    f"Intrinio price response requires {identity_key} metadata"
                )
            ticker = identity.get("ticker")
            if not isinstance(ticker, str) or ticker.casefold() != expected_ticker.casefold():
                raise IntrinioResponseShapeError("Intrinio price response ticker mismatch")
            rows = payload.get(rows_key)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise IntrinioResponseShapeError(
                    f"Intrinio response field {rows_key!r} must be an array of objects"
                )
            result.extend(rows)
            if "next_page" not in payload:
                raise IntrinioResponseShapeError("Intrinio paged response must include next_page")
            next_page = payload.get("next_page")
            if next_page is None:
                return result
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

    def extract_data(
        self, params: _EtfPricePerformanceQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch one ETF identity plus its complete bounded daily-price history per symbol."""
        results: list[dict[str, Any]] = []
        for symbol in _query_symbols(params):
            profile = self._get_json(f"/etfs/{quote(symbol, safe='')}", None, ctx)
            ticker = profile.get("ticker")
            if not isinstance(ticker, str) or ticker.casefold() != symbol.casefold():
                raise IntrinioResponseShapeError("Intrinio ETF profile ticker mismatch")
            prices = self._get_bounded_pages(
                f"/securities/{quote(symbol, safe='')}/prices",
                {"frequency": "daily"},
                "stock_prices",
                ctx,
                identity_key="security",
                expected_ticker=symbol,
            )
            results.append(
                {
                    "symbol": symbol,
                    "etf_ticker": ticker,
                    "security_ticker": symbol,
                    "prices": prices,
                }
            )
        return results

    def transform_data(
        self, raw: object, params: _EtfPricePerformanceQuery
    ) -> list[IntrinioEtfPricePerformance]:
        """Calculate public return fields from source price levels without unit guessing."""
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise IntrinioResponseShapeError("ETF price histories must be an array of objects")
        expected_symbols = _query_symbols(params)
        if len(raw) != len(expected_symbols):
            raise IntrinioResponseShapeError("ETF price histories are incomplete")
        output: list[IntrinioEtfPricePerformance] = []
        seen_symbols: set[str] = set()
        for item in raw:
            symbol = item.get("symbol")
            if not isinstance(symbol, str) or symbol.casefold() not in {
                requested.casefold() for requested in expected_symbols
            }:
                raise IntrinioResponseShapeError("ETF history symbol is not in the request")
            if symbol.casefold() in seen_symbols:
                raise IntrinioResponseShapeError("ETF history contains a duplicate symbol")
            seen_symbols.add(symbol.casefold())
            etf_ticker = item.get("etf_ticker")
            security_ticker = item.get("security_ticker")
            if not isinstance(etf_ticker, str) or etf_ticker.casefold() != symbol.casefold():
                raise IntrinioResponseShapeError("ETF profile ticker does not match request")
            if (
                not isinstance(security_ticker, str)
                or security_ticker.casefold() != symbol.casefold()
            ):
                raise IntrinioResponseShapeError("ETF price security ticker does not match request")
            rows = item.get("prices")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise IntrinioResponseShapeError("ETF daily price history must be an array")
            # Guard direct callers that bypass transform_query as well as the public fetch path.
            if params.adjustment != "splits_and_dividends":
                raise IntrinioSnapshotQueryError(
                    "Intrinio splits_only is unsupported without a documented split ratio direction"
                )
            prices = _adjusted_close_series(rows)
            return_type = params.return_type
            if return_type is None:
                return_type = "trailing"
            calculations = _returns_for_history(prices, return_type)
            output.append(
                IntrinioEtfPricePerformance.model_validate(
                    {
                        "symbol": symbol,
                        **calculations,
                        "return_type": return_type,
                        "adjustment": params.adjustment,
                        "calculation_scale": "end_price_over_start_price_minus_one",
                        "calculation_method": "adjusted_close_price_ratio_fraction_v1",
                    }
                )
            )
        return output


class _SecurityDirectoryRow:
    """Validated minimum directory data needed to classify a primary Nasdaq listing."""

    def __init__(self, security_id: str, ticker: str, name: str | None) -> None:
        self.security_id = security_id
        self.ticker = ticker
        self.name = name


class IntrinioMarketSnapshotsFetcher(_IntrinioMarketFetcher):
    """Fetch current Nasdaq quotes and join only verified XNAS primary security IDs."""

    canonical_model = "MarketSnapshots"
    query_model = _MarketSnapshotsQuery
    capability = _capability("market_snapshots", "nasdaq")

    def transform_query(self, **kwargs: object) -> _MarketSnapshotsQuery:
        """Validate the supported current Nasdaq selector before any request."""
        params = self.query_model.model_validate(kwargs)
        self._validate_snapshot_query(params)
        return params

    def _validate_snapshot_query(self, params: _MarketSnapshotsQuery) -> None:
        if params.market != "nasdaq":
            raise IntrinioSnapshotQueryError("Intrinio currently supports market='nasdaq' only")
        if params.requested_date is not None:
            raise IntrinioSnapshotQueryError(
                "Intrinio historical MarketSnapshots is unsupported without a public "
                "downloaded-row schema"
            )
        if params.start_date is not None or params.end_date is not None:
            raise IntrinioSnapshotQueryError("MarketSnapshots does not accept a date window")

    def _get_bounded_pages(
        self,
        path: str,
        params: dict[str, str],
        rows_key: str,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        cursor: str | None = None
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for _ in range(_MAX_PAGES):
            query = dict(params)
            query["page_size"] = str(_PAGE_SIZE)
            if cursor is not None:
                query["next_page"] = cursor
            payload = self._get_json(path, query, ctx)
            rows = payload.get(rows_key)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise IntrinioResponseShapeError(
                    f"Intrinio response field {rows_key!r} must be an array of objects"
                )
            result.extend(rows)
            if "next_page" not in payload:
                raise IntrinioResponseShapeError("Intrinio paged response must include next_page")
            next_page = payload.get("next_page")
            if next_page is None:
                return result
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

    def extract_data(self, params: _MarketSnapshotsQuery, ctx: FetchContext) -> dict[str, Any]:
        """Fetch the XNAS primary-security directory and current USCOMP delayed-SIP rows."""
        directory = self._get_bounded_pages(
            "/stock_exchanges/XNAS/securities", {}, "securities", ctx
        )
        prices = self._get_bounded_pages(
            "/stock_exchanges/USCOMP/prices/realtime",
            {"source": "delayed_sip"},
            "stock_prices",
            ctx,
        )
        return {"securities": directory, "stock_prices": prices}

    @staticmethod
    def _directory(raw: object) -> dict[str, _SecurityDirectoryRow]:
        if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
            raise IntrinioResponseShapeError("XNAS securities directory must be an array")
        result: dict[str, _SecurityDirectoryRow] = {}
        for row in raw:
            security_id = row.get("id")
            ticker = row.get("ticker")
            mic = row.get("exchange_mic")
            primary = row.get("primary_listing")
            if not isinstance(security_id, str) or not security_id.strip():
                raise IntrinioResponseShapeError("XNAS directory row requires a security id")
            if not isinstance(ticker, str) or not ticker.strip():
                raise IntrinioResponseShapeError("XNAS directory row requires a ticker")
            if not isinstance(mic, str) or not mic.strip():
                raise IntrinioResponseShapeError("XNAS directory row requires exchange_mic")
            if not isinstance(primary, bool):
                raise IntrinioResponseShapeError("XNAS directory row requires primary_listing bool")
            if not primary or mic != "XNAS":
                continue
            if security_id in result:
                raise IntrinioResponseShapeError("XNAS directory contains a duplicate security id")
            name = row.get("name")
            if name is not None and not isinstance(name, str):
                raise IntrinioResponseShapeError("XNAS directory name must be a string or null")
            result[security_id] = _SecurityDirectoryRow(security_id, ticker, name)
        return result

    @staticmethod
    def _snapshot_row(
        source_row: dict[str, Any], directory_row: _SecurityDirectoryRow
    ) -> IntrinioMarketSnapshot:
        security = source_row.get("security")
        if not isinstance(security, dict):
            raise IntrinioResponseShapeError("realtime stock price row requires security metadata")
        security_id = security.get("id")
        ticker = security.get("ticker")
        if security_id != directory_row.security_id:
            raise IntrinioResponseShapeError("realtime row security id does not match directory")
        if not isinstance(ticker, str) or ticker.casefold() != directory_row.ticker.casefold():
            raise IntrinioResponseShapeError("realtime row ticker does not match XNAS directory")
        source = source_row.get("source")
        if source != "delayed_sip":
            raise IntrinioResponseShapeError(
                "realtime row source does not match delayed_sip request"
            )

        last_price = _numeric(source_row.get("last_price"), "last_price", positive=True)
        previous_close = _numeric(
            source_row.get("eod_close_price"), "eod_close_price", positive=True
        )
        change = (
            last_price - previous_close
            if last_price is not None and previous_close is not None
            else None
        )
        change_percent = (
            change / previous_close
            if change is not None and previous_close not in (None, 0.0)
            else None
        )
        if change is not None and not math.isfinite(change):
            raise IntrinioResponseShapeError("computed quote change is not finite")
        if change_percent is not None and not math.isfinite(change_percent):
            raise IntrinioResponseShapeError("computed quote change percent is not finite")
        raw_last_time = source_row.get("last_time")
        raw_bid_time = source_row.get("bid_time")
        raw_ask_time = source_row.get("ask_time")
        raw_updated_on = source_row.get("updated_on")
        for field, value in (
            ("last_time", raw_last_time),
            ("bid_time", raw_bid_time),
            ("ask_time", raw_ask_time),
            ("updated_on", raw_updated_on),
        ):
            if value is not None and not isinstance(value, str):
                raise IntrinioResponseShapeError(f"Intrinio {field} must be a string or null")
        return IntrinioMarketSnapshot.model_validate(
            {
                "exchange": "XNAS",
                "symbol": directory_row.ticker,
                "name": directory_row.name,
                "open": _numeric(source_row.get("open_price"), "open_price", positive=True),
                "high": _numeric(source_row.get("high_price"), "high_price", positive=True),
                "low": _numeric(source_row.get("low_price"), "low_price", positive=True),
                # close_price is documented by Intrinio as IEX-only, so delayed_sip cannot fill it.
                "close": None,
                "volume": _numeric(
                    source_row.get("market_volume"), "market_volume", non_negative=True
                ),
                "prev_close": previous_close,
                "change": change,
                "change_percent": change_percent,
                "last_price": last_price,
                "last_size": _numeric(source_row.get("last_size"), "last_size", non_negative=True),
                "last_trade_timestamp": _aware_timestamp(raw_last_time, "last_time"),
                "bid_price": _numeric(source_row.get("bid_price"), "bid_price", positive=True),
                "bid_size": _numeric(source_row.get("bid_size"), "bid_size", non_negative=True),
                "last_bid_timestamp": _aware_timestamp(raw_bid_time, "bid_time"),
                "ask_price": _numeric(source_row.get("ask_price"), "ask_price", positive=True),
                "ask_size": _numeric(source_row.get("ask_size"), "ask_size", non_negative=True),
                "last_ask_timestamp": _aware_timestamp(raw_ask_time, "ask_time"),
                "source_updated_on": raw_updated_on,
                "source_last_time": raw_last_time,
                "source_bid_time": raw_bid_time,
                "source_ask_time": raw_ask_time,
                "source_market_volume": _numeric(
                    source_row.get("market_volume"), "market_volume", non_negative=True
                ),
                "source_exchange_volume": _numeric(
                    source_row.get("exchange_volume"), "exchange_volume", non_negative=True
                ),
            }
        )

    def transform_data(
        self, raw: object, params: _MarketSnapshotsQuery
    ) -> list[IntrinioMarketSnapshot]:
        """Join quotes by security ID and retain only XNAS primary listings."""
        if not isinstance(raw, dict):
            raise IntrinioResponseShapeError("market snapshot response must be an object")
        directory = self._directory(raw.get("securities"))
        source_rows = raw.get("stock_prices")
        if not isinstance(source_rows, list) or any(
            not isinstance(row, dict) for row in source_rows
        ):
            raise IntrinioResponseShapeError("stock_prices must be an array of objects")
        output: list[IntrinioMarketSnapshot] = []
        seen_ids: set[str] = set()
        for row in source_rows:
            security = row.get("security")
            if not isinstance(security, dict):
                raise IntrinioResponseShapeError(
                    "realtime stock price row requires security metadata"
                )
            security_id = security.get("id")
            if not isinstance(security_id, str) or not security_id:
                raise IntrinioResponseShapeError("realtime stock price row requires security id")
            directory_row = directory.get(security_id)
            if directory_row is None:
                continue
            if security_id in seen_ids:
                raise IntrinioResponseShapeError(
                    "realtime response contains duplicate security ids"
                )
            seen_ids.add(security_id)
            output.append(self._snapshot_row(row, directory_row))
        return output


__all__ = [
    "IntrinioEtfPricePerformanceFetcher",
    "IntrinioMarketSnapshotsFetcher",
    "IntrinioSnapshotQueryError",
]
