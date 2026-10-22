"""Shared parsing and query rules for Alpha Vantage price history."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import TYPE_CHECKING, ClassVar, Generic, Literal, TypedDict, TypeVar

from pydantic import StrictBool, StrictStr, field_validator, model_validator

from opendata.data.models.alpha_vantage import (
    AlphaVantageEquityHistorical,
    AlphaVantageEtfHistorical,
)
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.providers.alpha_vantage.client import (
    AlphaVantageClient,
    AlphaVantageResponseError,
)

if TYPE_CHECKING:
    from typing_extensions import Self

    from opendata.data.capability import Capability

Adjustment = Literal["unadjusted", "splits_and_dividends", "splits_only"]
Interval = Literal["1m", "5m", "15m", "30m", "60m", "1d", "1W", "1M"]
_INTRADAY_INTERVALS = {"1m", "5m", "15m", "30m", "60m"}
_AV_INTERVALS = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "60m": "60min",
}
_EARLIEST_INTRADAY_DATE = date(2000, 1, 1)
_NUMBER = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_INTEGER = re.compile(r"^\d+$")
_TIME_ZONE_KEY = re.compile(r"^\d+\.\s*Time Zone$")
_TIME_ZONE_VALUE = re.compile(r"^[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+)*$")


class _ParsedPriceRow(TypedDict):
    """Parsed price-row values before typed contract validation."""

    symbol: str
    date: date | datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None
    adj_close: float | None
    dividend: float | None
    split_ratio: float | None


class AlphaVantageHistoricalQuery(QueryParams):
    """Fail-closed query for the frequencies and adjustment modes AV exposes."""

    symbol: StrictStr
    interval: Interval = "1d"
    adjustment: Adjustment | None
    extended_hours: StrictBool | None = False

    @field_validator("symbol")
    @classmethod
    def _trimmed_symbol(cls, value: str) -> str:
        if not value or value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("symbol must be non-empty, trimmed, and contain no controls")
        return value

    @model_validator(mode="after")
    def _supported_request(self) -> Self:
        if self.source not in {"auto", "alpha_vantage"}:
            raise ValueError("source must be auto or alpha_vantage")
        if self.market not in {None, "global"}:
            raise ValueError("Alpha Vantage declares global capability, not market filtering")
        if self.adjustment is None:
            raise ValueError(
                "adjustment must be explicit because OpenBB's splits_only default is unsupported"
            )
        if self.adjustment == "splits_only":
            raise ValueError(
                "Alpha Vantage exposes raw or split-and-dividend adjusted series, not splits_only"
            )
        if self.extended_hours and self.interval not in _INTRADAY_INTERVALS:
            raise ValueError("extended_hours is only supported for intraday intervals")
        if (
            self.interval in _INTRADAY_INTERVALS
            and self.start_date is not None
            and self.start_date < _EARLIEST_INTRADAY_DATE
        ):
            raise ValueError("Alpha Vantage intraday month queries are documented from 2000-01")
        if (
            self.interval in _INTRADAY_INTERVALS
            and self.end_date is not None
            and self.end_date < _EARLIEST_INTRADAY_DATE
        ):
            raise ValueError("Alpha Vantage intraday month queries are documented from 2000-01")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must be on or before end_date")
        if self.end_date is not None and self.end_date > date.today():
            raise ValueError("end_date cannot be in the future")
        if (
            self.interval in _INTRADAY_INTERVALS
            and self.end_date is not None
            and self.start_date is None
        ):
            raise ValueError("intraday end_date requires start_date for a bounded month query")
        return self


PriceRow = TypeVar("PriceRow", AlphaVantageEquityHistorical, AlphaVantageEtfHistorical)


class AlphaVantageHistoricalFetcher(
    Fetcher[AlphaVantageHistoricalQuery, tuple[_ParsedPriceRow, ...]],
    Generic[PriceRow],
):
    """Shared execution for explicit equity and ETF model bindings."""

    capability: ClassVar[Capability]
    async_mode: ClassVar[str] = "bounded_thread"
    canonical_model: ClassVar[str | None] = None
    row_type: ClassVar[type[PriceRow]]
    domain: ClassVar[str]
    asset_class: ClassVar[str]

    def __init__(self, *, client: AlphaVantageClient | None = None) -> None:
        self._client = client or AlphaVantageClient()

    def transform_query(self, **kwargs: object) -> AlphaVantageHistoricalQuery:
        """Validate keyword arguments into ``AlphaVantageHistoricalQuery``."""
        return AlphaVantageHistoricalQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: AlphaVantageHistoricalQuery,
        ctx: FetchContext,
    ) -> tuple[_ParsedPriceRow, ...]:
        """Return the upstream document parsed into per-symbol price rows."""
        requests = _price_requests(params)
        collected: list[_ParsedPriceRow] = []
        seen_dates: set[date | datetime] = set()
        canonical_symbol: str | None = None
        source_timezone: str | None = None
        for request_params, series_key, adjusted_layout in requests:
            response = self._client.get_json(request_params, timeout=ctx.timeout)
            response_symbol, response_timezone = _read_metadata(response, params.symbol)
            if canonical_symbol is None:
                canonical_symbol = response_symbol
                source_timezone = response_timezone
            elif response_symbol.casefold() != canonical_symbol.casefold():
                raise AlphaVantageResponseError(
                    "Alpha Vantage response symbol changed during the requested time range"
                )
            elif response_timezone != source_timezone:
                raise AlphaVantageResponseError(
                    "Alpha Vantage response time zone changed during the requested time range"
                )
            series = _read_series(response, series_key)
            for time_key, fields in series.items():
                row_date = _parse_timestamp(
                    time_key,
                    intraday=params.interval in _INTRADAY_INTERVALS,
                )
                if not _within_dates(row_date, params.start_date, params.end_date):
                    continue
                if row_date in seen_dates:
                    raise AlphaVantageResponseError(
                        "Alpha Vantage returned duplicate time-series rows"
                    )
                seen_dates.add(row_date)
                collected.append(
                    _parse_price_record(
                        symbol=canonical_symbol,
                        row_date=row_date,
                        fields=fields,
                        adjusted_layout=adjusted_layout,
                    )
                )
        collected.sort(key=lambda row: row["date"])
        return tuple(collected)

    def transform_data(
        self,
        raw: tuple[_ParsedPriceRow, ...],
        params: AlphaVantageHistoricalQuery,
    ) -> tuple[PriceRow, ...]:
        """Convert parsed price rows into contract ``PriceRow`` objects."""
        return tuple(self.row_type.model_validate(row) for row in raw)


def _price_requests(
    params: AlphaVantageHistoricalQuery,
) -> list[tuple[dict[str, str], str, bool]]:
    """Build documented AV requests, faning explicit intraday windows by month."""
    interval = params.interval
    adjusted = params.adjustment == "splits_and_dividends"
    common = {"symbol": params.symbol, "datatype": "json"}
    if interval in _INTRADAY_INTERVALS:
        base = {
            **common,
            "function": "TIME_SERIES_INTRADAY",
            "interval": _AV_INTERVALS[interval],
            "adjusted": "true" if adjusted else "false",
            "extended_hours": "true" if params.extended_hours else "false",
            "outputsize": "full",
        }
        if params.start_date is None:
            return [(base, f"Time Series ({_AV_INTERVALS[interval]})", False)]
        last_date = params.end_date or date.today()
        return [
            (
                {**base, "month": month.strftime("%Y-%m")},
                f"Time Series ({_AV_INTERVALS[interval]})",
                False,
            )
            for month in _months_between(params.start_date, last_date)
        ]

    if interval == "1d":
        function = "TIME_SERIES_DAILY_ADJUSTED" if adjusted else "TIME_SERIES_DAILY"
        series_key = "Time Series (Daily)"
        return [({**common, "function": function, "outputsize": "full"}, series_key, adjusted)]

    if interval == "1W":
        function = "TIME_SERIES_WEEKLY_ADJUSTED" if adjusted else "TIME_SERIES_WEEKLY"
        series_key = "Weekly Adjusted Time Series" if adjusted else "Weekly Time Series"
        return [({**common, "function": function}, series_key, adjusted)]

    function = "TIME_SERIES_MONTHLY_ADJUSTED" if adjusted else "TIME_SERIES_MONTHLY"
    series_key = "Monthly Adjusted Time Series" if adjusted else "Monthly Time Series"
    return [({**common, "function": function}, series_key, adjusted)]


def _months_between(start: date, end: date) -> tuple[date, ...]:
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    months: list[date] = []
    while cursor <= last:
        months.append(cursor)
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return tuple(months)


def _read_series(
    payload: Mapping[str, object],
    series_key: str,
) -> Mapping[str, object]:
    allowed_top_level = {"Meta Data", series_key}
    unexpected = set(payload) - allowed_top_level
    if unexpected:
        raise AlphaVantageResponseError(
            f"Alpha Vantage response has unexpected top-level keys: {sorted(unexpected)}"
        )
    series = payload.get(series_key)
    if not isinstance(series, Mapping) or any(not isinstance(key, str) for key in series):
        raise AlphaVantageResponseError(f"Alpha Vantage response is missing {series_key!r}")
    return series


def _read_metadata(payload: Mapping[str, object], requested_symbol: str) -> tuple[str, str]:
    """Validate the official symbol/time-zone metadata before labeling price rows."""
    metadata = payload.get("Meta Data")
    if not isinstance(metadata, Mapping):
        raise AlphaVantageResponseError("Alpha Vantage response is missing Meta Data")

    response_symbol = metadata.get("2. Symbol")
    if (
        not isinstance(response_symbol, str)
        or not response_symbol
        or response_symbol != response_symbol.strip()
        or any(ord(char) < 32 for char in response_symbol)
    ):
        raise AlphaVantageResponseError("Alpha Vantage metadata has an invalid symbol")
    if response_symbol.casefold() != requested_symbol.casefold():
        raise AlphaVantageResponseError(
            "Alpha Vantage metadata symbol does not match the requested symbol"
        )

    timezone_keys = [
        key for key in metadata if isinstance(key, str) and _TIME_ZONE_KEY.fullmatch(key)
    ]
    if len(timezone_keys) != 1:
        raise AlphaVantageResponseError("Alpha Vantage metadata must contain one Time Zone field")
    response_timezone = metadata[timezone_keys[0]]
    if not isinstance(response_timezone, str) or not _TIME_ZONE_VALUE.fullmatch(response_timezone):
        raise AlphaVantageResponseError("Alpha Vantage metadata has an invalid Time Zone")
    return response_symbol, response_timezone


def _parse_timestamp(value: str, *, intraday: bool) -> date | datetime:
    try:
        if intraday:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError as exc:
        raise AlphaVantageResponseError(
            "Alpha Vantage returned an invalid time-series key"
        ) from exc


def _within_dates(value: date | datetime, start: date | None, end: date | None) -> bool:
    row_date = value.date() if isinstance(value, datetime) else value
    return (start is None or row_date >= start) and (end is None or row_date <= end)


def _parse_price_record(
    *,
    symbol: str,
    row_date: date | datetime,
    fields: object,
    adjusted_layout: bool,
) -> _ParsedPriceRow:
    if not isinstance(fields, Mapping):
        raise AlphaVantageResponseError("Alpha Vantage time-series record must be an object")
    basic_keys = {"1. open", "2. high", "3. low", "4. close", "5. volume"}
    if adjusted_layout:
        if "8. split coefficient" in fields:
            expected_keys = {
                "1. open",
                "2. high",
                "3. low",
                "4. close",
                "5. adjusted close",
                "6. volume",
                "7. dividend amount",
                "8. split coefficient",
            }
            volume_key = "6. volume"
        else:
            expected_keys = {
                "1. open",
                "2. high",
                "3. low",
                "4. close",
                "5. adjusted close",
                "6. volume",
                "7. dividend amount",
            }
            volume_key = "6. volume"
        fields_to_check = set(fields)
    else:
        expected_keys = basic_keys
        volume_key = "5. volume"
        fields_to_check = set(fields)
    if fields_to_check != expected_keys:
        raise AlphaVantageResponseError(
            "Alpha Vantage time-series record keys do not match the documented layout"
        )

    open_value = _number(fields["1. open"], "open")
    high_value = _number(fields["2. high"], "high")
    low_value = _number(fields["3. low"], "low")
    close_value = _number(fields["4. close"], "close")
    volume_value = _integer(fields[volume_key], "volume")
    result: _ParsedPriceRow = {
        "symbol": symbol,
        "date": row_date,
        "open": open_value,
        "high": high_value,
        "low": low_value,
        "close": close_value,
        "volume": volume_value,
        "vwap": None,
        "adj_close": None,
        "dividend": None,
        "split_ratio": None,
    }
    if adjusted_layout:
        result["adj_close"] = _number(fields["5. adjusted close"], "adjusted close")
        result["dividend"] = _number(fields["7. dividend amount"], "dividend amount")
        if "8. split coefficient" in fields:
            result["split_ratio"] = _number(fields["8. split coefficient"], "split coefficient")
    return result


def _number(value: object, name: str) -> float:
    if not isinstance(value, str) or not _NUMBER.fullmatch(value):
        raise AlphaVantageResponseError(f"Alpha Vantage {name} must be a numeric string")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise AlphaVantageResponseError(f"Alpha Vantage {name} must be finite")
    return parsed


def _integer(value: object, name: str) -> int:
    if not isinstance(value, str) or not _INTEGER.fullmatch(value):
        raise AlphaVantageResponseError(
            f"Alpha Vantage {name} must be a non-negative integer string"
        )
    return int(value)
