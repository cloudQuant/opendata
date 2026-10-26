"""Eastmoney-backed A-share daily bars, normalized to the unadjusted Bar contract."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal
from numbers import Real
from typing import ClassVar

import pandas as pd
from pydantic import field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models import Bar
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.akshare._source import SOURCE

_REQUIRED_COLUMNS = ("日期", "股票代码", "开盘", "收盘", "最高", "最低", "成交量", "成交额")
_SYMBOL_PATTERN = re.compile(r"(?:(SH|SZ|BJ)(\d{6})|(\d{6})(?:\.(SH|SZ|BJ))?)", re.IGNORECASE)
_DATE_PATTERN = re.compile(r"(?:\d{4}-\d{2}-\d{2}|\d{8})")


class StockDailyQuery(QueryParams):
    """Validated query for one A-share symbol and the unadjusted price basis."""

    symbol: str
    adjust: str = ""

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: object) -> str:
        """Require a supported A-share code with a matching optional exchange."""
        if not isinstance(value, str):
            raise ValueError("symbol must be a six-digit A-share code")
        return _normalize_symbol(value)

    @field_validator("source", mode="before")
    @classmethod
    def validate_source(cls, value: object) -> str:
        """Accept only this provider or normal registry auto-routing."""
        if not isinstance(value, str):
            raise ValueError("source must identify the akshare provider")
        source = value.strip().lower()
        if source not in {"auto", SOURCE}:
            raise ValueError(f"unsupported stock_daily source {value!r}; use {SOURCE!r}")
        return source

    @field_validator("market", mode="before")
    @classmethod
    def validate_market(cls, value: object) -> str | None:
        """Reject a market identity that does not match this A-share provider."""
        if value is None:
            return None
        if not isinstance(value, str) or value.strip().lower() != "cn":
            raise ValueError("stock_daily provider supports market='cn' only")
        return "cn"

    @field_validator("adjust", mode="before")
    @classmethod
    def validate_adjustment(cls, value: object) -> str:
        """Prevent adjusted prices from being mislabeled as a ``Bar``."""
        if not isinstance(value, str):
            raise ValueError("adjust must be an unadjusted price-basis string")
        adjustment = value.strip().lower()
        if adjustment in {"", "none", "raw", "unadjusted"}:
            return ""
        if adjustment in {"qfq", "hfq"}:
            raise ValueError(
                "adjusted stock bars are unsupported by the unadjusted Bar contract; "
                "use adjustment factors to derive a separate adjusted series"
            )
        raise ValueError(f"unsupported stock price adjustment {value!r}")

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def validate_business_date(cls, value: object) -> object:
        """Accept strict ISO or compact business dates, not epoch-like numbers."""
        if value is None or isinstance(value, date):
            return value
        if isinstance(value, str):
            candidate = value.strip()
            if not _DATE_PATTERN.fullmatch(candidate):
                raise ValueError("dates must use YYYY-MM-DD or YYYYMMDD")
            try:
                if len(candidate) == 8:
                    return datetime.strptime(candidate, "%Y%m%d").date()
                return date.fromisoformat(candidate)
            except ValueError as exc:
                raise ValueError("date must be a valid business date") from exc
        raise ValueError("dates must be date objects or YYYY-MM-DD / YYYYMMDD strings")

    @model_validator(mode="after")
    def validate_window(self) -> StockDailyQuery:
        """Require an ordered inclusive date window when both ends are supplied."""
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must be on or before end_date")
        return self


class AkshareStockDailyFetcher(Fetcher[StockDailyQuery, pd.DataFrame]):
    """Fetch Eastmoney daily klines through the governed HTTP client."""

    async_mode = "bounded_thread"

    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="stock_daily",
        period="1D",
        market="cn",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> StockDailyQuery:
        """Build and validate the query before any source request is made.

        Args:
            **kwargs: ``symbol``, inclusive ``start_date`` / ``end_date``,
                and an unadjusted ``adjust`` value. Registry ``source`` may be
                ``auto`` or the explicit ``akshare`` provider identity.

        Returns:
            A canonical query with a plain six-digit symbol.

        Raises:
            pydantic.ValidationError: If an identity or price basis is unsupported.
        """
        return StockDailyQuery.model_validate(kwargs)

    def extract_data(self, params: StockDailyQuery, ctx: FetchContext) -> pd.DataFrame:
        """Fetch raw Eastmoney daily bars without source fallback.

        Args:
            params: Validated symbol, window, and unadjusted basis.
            ctx: Per-call context carrying the timeout override.

        Returns:
            The upstream frame with native Chinese column names.
        """
        from opendata.data.providers.akshare.stock_daily_wire import fetch_stock_daily_wire

        return fetch_stock_daily_wire(
            params.symbol,
            start_date=params.start_date.strftime("%Y%m%d") if params.start_date else "19700101",
            end_date=params.end_date.strftime("%Y%m%d") if params.end_date else "20500101",
            timeout=ctx.timeout,
        )

    def transform_data(self, raw: pd.DataFrame, params: StockDailyQuery) -> FetchResult:
        """Validate every source row and normalize only observed unadjusted bars.

        Eastmoney's ``成交量`` is exposed in lots (手), while :class:`Bar`
        stores shares, so this boundary multiplies by 100. ``成交额`` remains
        in CNY per the Bar contract. No missing or malformed row is dropped.

        Args:
            raw: Eastmoney-shaped source frame.
            params: Validated query and expected instrument/date identity.

        Returns:
            Bars sorted by source business date.

        Raises:
            ValueError: If the source frame is empty, malformed, or mismatched.
        """
        if not isinstance(raw, pd.DataFrame):
            raise ValueError("Eastmoney stock history response must be a DataFrame")
        if raw.empty:
            raise ValueError("Eastmoney returned no stock history rows")
        if raw.columns.has_duplicates:
            raise ValueError("Eastmoney stock history contains duplicate column names")
        missing = sorted(set(_REQUIRED_COLUMNS).difference(raw.columns))
        if missing:
            raise ValueError(f"Eastmoney stock history is missing required columns: {missing}")

        bars: list[Bar] = []
        seen_dates: set[date] = set()
        expected_symbol = params.symbol
        for row_number, record in enumerate(raw.to_dict(orient="records"), start=1):
            trade_date = _parse_business_date(record["日期"], row_number=row_number)
            if params.start_date is not None and trade_date < params.start_date:
                raise ValueError(
                    f"Eastmoney row {row_number} date {trade_date} precedes requested start_date"
                )
            if params.end_date is not None and trade_date > params.end_date:
                raise ValueError(
                    f"Eastmoney row {row_number} date {trade_date} exceeds requested end_date"
                )
            if trade_date in seen_dates:
                raise ValueError(f"Eastmoney response contains duplicate date {trade_date}")
            seen_dates.add(trade_date)

            response_symbol = _normalize_symbol(record["股票代码"])
            if response_symbol != expected_symbol:
                raise ValueError(
                    f"Eastmoney response instrument {response_symbol} does not match "
                    f"requested symbol {expected_symbol}"
                )

            open_price = _finite_number(record["开盘"], "开盘", row_number)
            close_price = _finite_number(record["收盘"], "收盘", row_number)
            high_price = _finite_number(record["最高"], "最高", row_number)
            low_price = _finite_number(record["最低"], "最低", row_number)
            volume_lots = _finite_number(record["成交量"], "成交量", row_number)
            amount_cny = _finite_number(record["成交额"], "成交额", row_number)

            if min(open_price, close_price, high_price, low_price) <= 0:
                raise ValueError(f"Eastmoney row {row_number} has non-positive unadjusted OHLC")
            if high_price < max(open_price, close_price, low_price):
                raise ValueError(f"Eastmoney row {row_number} high is below an OHLC value")
            if low_price > min(open_price, close_price, high_price):
                raise ValueError(f"Eastmoney row {row_number} low is above an OHLC value")
            if volume_lots < 0 or amount_cny < 0:
                raise ValueError(f"Eastmoney row {row_number} has negative volume or amount")

            volume_shares = volume_lots * 100
            if not math.isfinite(volume_shares):
                raise ValueError(f"Eastmoney row {row_number} volume overflows share units")
            bars.append(
                Bar(
                    symbol=expected_symbol,
                    trade_date=trade_date,
                    open=open_price,
                    high=high_price,
                    low=low_price,
                    close=close_price,
                    volume=volume_shares,
                    amount=amount_cny,
                )
            )

        if not bars:
            raise ValueError("Eastmoney returned no stock history rows")
        return sorted(bars, key=lambda bar: bar.trade_date)


def _normalize_symbol(value: object) -> str:
    """Return a canonical six-digit code, rejecting unsupported exchanges."""
    if not isinstance(value, str):
        raise ValueError("A-share symbol identity must be a string")
    candidate = value.strip()
    match = _SYMBOL_PATTERN.fullmatch(candidate)
    if match is None:
        raise ValueError("A-share symbol must be six digits with an optional SH/SZ/BJ suffix")

    prefixed_exchange, prefixed_code, suffixed_code, suffixed_exchange = match.groups()
    code = prefixed_code or suffixed_code
    supplied_exchange = prefixed_exchange or suffixed_exchange
    if code is None:
        raise ValueError("A-share symbol is missing its code")
    code = code.upper()

    if code.startswith("6"):
        expected_exchange = "SH"
    elif code.startswith(("0", "3")):
        expected_exchange = "SZ"
    elif code.startswith(("4", "8", "920")):
        expected_exchange = "BJ"
    else:
        raise ValueError(f"unsupported A-share code prefix for {code}")
    if supplied_exchange is not None and supplied_exchange.upper() != expected_exchange:
        raise ValueError(
            f"symbol {value!r} carries exchange {supplied_exchange.upper()} "
            f"but code {code} belongs to {expected_exchange}"
        )
    return code


def _parse_business_date(value: object, *, row_number: int) -> date:
    """Parse a source business date without epoch coercion or NaT skipping."""
    if value is None or value is pd.NaT or value is pd.NA:
        raise ValueError(f"Eastmoney row {row_number} has invalid business date {value!r}")
    if isinstance(value, float) and math.isnan(value):
        raise ValueError(f"Eastmoney row {row_number} has invalid business date {value!r}")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        if not _DATE_PATTERN.fullmatch(candidate):
            raise ValueError(f"Eastmoney row {row_number} has invalid business date {value!r}")
        try:
            return (
                datetime.strptime(candidate, "%Y%m%d").date()
                if len(candidate) == 8
                else date.fromisoformat(candidate)
            )
        except ValueError as exc:
            raise ValueError(
                f"Eastmoney row {row_number} has invalid business date {value!r}"
            ) from exc
    raise ValueError(f"Eastmoney row {row_number} has invalid business date {value!r}")


def _finite_number(value: object, field: str, row_number: int) -> float:
    """Parse a finite source numeric scalar, treating null and NaN as errors."""
    if value is None or value is pd.NA or value is pd.NaT or isinstance(value, bool):
        raise ValueError(f"Eastmoney row {row_number} has missing/invalid {field}")
    if not isinstance(value, (str, Real, Decimal)):
        raise ValueError(f"Eastmoney row {row_number} has invalid {field}: {value!r}")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"Eastmoney row {row_number} has invalid {field}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"Eastmoney row {row_number} has non-finite {field}")
    return number
