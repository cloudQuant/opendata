"""Typed source-native fetchers for the yfinance Iteration 2 model slice."""

from __future__ import annotations

import math
from datetime import date, datetime
from numbers import Real
from typing import ClassVar, Literal, TypeVar

import pandas as pd
from pydantic import ValidationError, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.yfinance_iter2 import (
    YfinanceDividendObservation,
    YfinanceFinancialStatementObservation,
)
from opendata.data.protocol import (
    BaseQueryParams,
    FetchContext,
    Fetcher,
    FetchResult,
    MultiSymbolQueryParams,
    QueryParams,
)
from opendata.data.providers.yfinance._source import SOURCE
from opendata.data.providers.yfinance.models.stock_daily import YfinanceProviderError

StatementType = Literal["balance_sheet", "cash_flow_statement", "income_statement"]
StatementFrequency = Literal["yearly", "quarterly"]
MAX_YFINANCE_SYMBOLS = 8
MAX_YFINANCE_SYMBOL_TOKENS = 32
_YfinanceQueryT = TypeVar("_YfinanceQueryT", bound=BaseQueryParams)


def _normalize_yfinance_symbol(value: str) -> str:
    normalized = value.strip().upper()
    if not normalized or any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError("symbol must be a nonblank token without control characters")
    return normalized


class YfinanceSymbolQuery(QueryParams):
    """Validated symbol input shared by the statement and dividend fetchers."""

    symbol: str

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return _normalize_yfinance_symbol(value)


class YfinanceMultiSymbolQuery(MultiSymbolQueryParams):
    """A bounded stable symbol list encoded for source-native ``Ticker`` calls."""

    symbol: str | list[str]
    currency_pairs: ClassVar[bool] = False

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbols(cls, value: object) -> object:
        if isinstance(value, str):
            raw_tokens = value.split(",")
        elif isinstance(value, list):
            if any(not isinstance(item, str) for item in value):
                raise ValueError("symbols must be strings")
            raw_tokens = [token for item in value for token in item.split(",")]
        else:
            raise ValueError("symbol must be a string or list of strings")

        if len(raw_tokens) > MAX_YFINANCE_SYMBOL_TOKENS:
            raise ValueError("too many symbol tokens")

        symbols: list[str] = []
        seen: set[str] = set()
        for raw_token in raw_tokens:
            token = raw_token.strip()
            if not token:
                continue
            symbol = cls._normalize_symbol_token(token)
            if cls.currency_pairs:
                symbol = cls._normalize_currency_pair(symbol)
            if symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
            if len(symbols) > MAX_YFINANCE_SYMBOLS:
                raise ValueError("too many distinct symbols")

        if not symbols:
            raise ValueError("at least one symbol is required")
        return ",".join(symbols)

    @classmethod
    def _normalize_symbol_token(cls, value: str) -> str:
        return _normalize_yfinance_symbol(value)

    @classmethod
    def _normalize_currency_pair(cls, symbol: str) -> str:
        pair = symbol[:-2] if symbol.endswith("=X") else symbol
        pair = pair.replace("-", "")
        if len(pair) != 6 or not pair.isascii() or not pair.isalpha():
            raise ValueError("currency symbol must be a six-letter pair")
        return f"{pair}=X"

    @property
    def symbols(self) -> tuple[str, ...]:
        """Return normalized symbols in stable first-seen order."""
        if isinstance(self.symbol, str):
            return tuple(self.symbol.split(","))
        return tuple(self.symbol)


class YfinanceStatementQuery(YfinanceSymbolQuery):
    """The public yfinance statement request: symbol and a bounded period count."""

    limit: int | None = 5
    period: Literal["annual", "quarter"] | None = "annual"

    @property
    def effective_frequency(self) -> StatementFrequency:
        """Translate the public annual/quarter period into the SDK frequency."""
        return "quarterly" if self.period == "quarter" else "yearly"

    @field_validator("limit")
    @classmethod
    def _validate_limit(cls, value: int | None) -> int | None:
        if value is not None and (isinstance(value, bool) or not 1 <= value <= 100):
            raise ValueError("limit must be between 1 and 100 or None")
        return value

    @model_validator(mode="after")
    def _reject_unsupported_window(self) -> YfinanceStatementQuery:
        if {"start_date", "end_date"} & self.model_fields_set:
            raise ValueError("statement queries do not expose date filters")
        return self


class YfinanceIncomeStatementQuery(YfinanceStatementQuery):
    """Income statement query has the same public inputs as other statements."""


class YfinanceDividendQuery(YfinanceSymbolQuery):
    """Optional inclusive range over the historical dividend Series."""

    @model_validator(mode="after")
    def _validate_window(self) -> YfinanceDividendQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self


def _validated_query(
    params: object,
    query_type: type[_YfinanceQueryT],
) -> _YfinanceQueryT:
    """Revalidate direct stage calls as well as the normal ``Fetcher.fetch`` path."""
    if not isinstance(params, BaseQueryParams):
        raise YfinanceProviderError("YFINANCE_BAD_QUERY")
    try:
        return query_type.model_validate(params.model_dump(exclude_unset=True))
    except ValidationError as exc:
        raise YfinanceProviderError("YFINANCE_BAD_QUERY") from exc


def _as_date(value: object, *, field: str) -> date:
    """Convert an SDK date label without inventing a day from an unknown shape."""
    if isinstance(value, pd.Period):
        return value.to_timestamp().date()
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            parts = value.split("/")
            if (
                len(parts) != 3
                or len(parts[0]) not in {1, 2}
                or len(parts[1]) not in {1, 2}
                or len(parts[2]) != 4
                or any(not part.isascii() or not part.isdigit() for part in parts)
            ):
                raise YfinanceProviderError(f"YFINANCE_{field.upper()}_INVALID") from exc
            try:
                return date(int(parts[2]), int(parts[0]), int(parts[1]))
            except ValueError as date_exc:
                raise YfinanceProviderError(f"YFINANCE_{field.upper()}_INVALID") from date_exc
        if parsed.isoformat() == value:
            return parsed
    raise YfinanceProviderError(f"YFINANCE_{field.upper()}_INVALID")


def _source_number(value: object, *, allow_missing: bool) -> float | None:
    """Validate one scalar SDK cell and preserve a source-side gap as ``None``."""
    if _is_missing_scalar(value):
        if allow_missing:
            return None
        raise YfinanceProviderError("YFINANCE_VALUE_MISSING")
    if isinstance(value, bool) or not isinstance(value, Real):
        raise YfinanceProviderError("YFINANCE_VALUE_INVALID")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise YfinanceProviderError("YFINANCE_VALUE_INVALID") from exc
    if not pd.notna(number) or number in (float("inf"), float("-inf")):
        raise YfinanceProviderError("YFINANCE_VALUE_INVALID")
    return number


def _is_missing_scalar(value: object) -> bool:
    """Recognize missing values from the scalar SDK cells consumed by these adapters."""
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    if isinstance(value, Real) and not isinstance(value, bool):
        try:
            return math.isnan(float(value))
        except (OverflowError, TypeError, ValueError):
            return False
    return False


def _statement_observations(
    frame: pd.DataFrame,
    params: YfinanceStatementQuery,
    statement_type: StatementType,
) -> tuple[YfinanceFinancialStatementObservation, ...]:
    """Transpose the SDK's item-by-period frame into source-native long rows."""
    if frame.empty or frame.index.empty or frame.columns.empty:
        raise YfinanceProviderError("YFINANCE_EMPTY_RESPONSE")
    if frame.columns.has_duplicates or frame.index.has_duplicates:
        raise YfinanceProviderError("YFINANCE_STATEMENT_KEYS_DUPLICATED")

    period_labels = [_as_date(value, field="report_period") for value in frame.columns]
    if len(period_labels) != len(set(period_labels)):
        raise YfinanceProviderError("YFINANCE_STATEMENT_KEYS_DUPLICATED")

    max_periods = params.limit if params.limit is not None else 100
    newest_periods = set(sorted(period_labels, reverse=True)[:max_periods])
    rows: list[YfinanceFinancialStatementObservation] = []
    for row_label, values in frame.iterrows():
        if not isinstance(row_label, str) or not row_label.strip():
            raise YfinanceProviderError("YFINANCE_STATEMENT_ITEM_INVALID")
        for period_end, raw_value in zip(period_labels, values.tolist(), strict=True):
            if period_end not in newest_periods:
                continue
            rows.append(
                YfinanceFinancialStatementObservation(
                    symbol=params.symbol,
                    statement_type=statement_type,
                    report_period=period_end,
                    line_item=row_label,
                    value=_source_number(raw_value, allow_missing=True),
                    frequency=params.effective_frequency,
                )
            )
    if not rows:
        raise YfinanceProviderError("YFINANCE_EMPTY_RESPONSE")
    return tuple(sorted(rows, key=lambda row: (row.report_period, row.line_item)))


class YfinanceStatementFetcher(Fetcher[YfinanceStatementQuery, pd.DataFrame | None]):
    """Common implementation for the three Ticker statement properties."""

    async_mode = "bounded_thread"
    statement_type: ClassVar[StatementType]
    canonical_model: ClassVar[str]
    query_type: ClassVar[type[YfinanceStatementQuery]] = YfinanceStatementQuery
    row_model: ClassVar[type[YfinanceFinancialStatementObservation]] = (
        YfinanceFinancialStatementObservation
    )

    def transform_query(self, **kwargs: object) -> YfinanceStatementQuery:
        """Validate symbol, bounded limit, and annual or quarterly period."""
        return self.query_type.model_validate(kwargs)

    def extract_data(
        self, params: YfinanceStatementQuery, ctx: FetchContext
    ) -> pd.DataFrame | None:
        """Call the exact yfinance Ticker method for this statement identity."""
        from opendata.data.providers.yfinance.models import _sdk

        query = _validated_query(params, self.query_type)
        return _sdk.require_statement_frame(
            query.symbol,
            self.statement_type,
            frequency=query.effective_frequency,
        )

    def transform_data(
        self, raw: pd.DataFrame | None, params: YfinanceStatementQuery
    ) -> FetchResult:
        """Preserve the SDK's original item labels and fiscal period dates."""
        query = _validated_query(params, self.query_type)
        if not isinstance(raw, pd.DataFrame):
            raise YfinanceProviderError("YFINANCE_STATEMENT_SHAPE_INVALID")
        return _statement_observations(raw, query, self.statement_type)


class YfinanceBalanceSheetFetcher(YfinanceStatementFetcher):
    """Annual or quarterly balance-sheet rows, as labeled by the SDK."""

    statement_type = "balance_sheet"
    canonical_model = "BalanceSheet"
    capability = Capability(
        asset_class="equity",
        domain="yfinance_balance_sheet",
        period="fiscal_period",
        market="global",
        source=SOURCE,
        verified=False,
    )


class YfinanceCashFlowStatementFetcher(YfinanceStatementFetcher):
    """Annual or quarterly cash-flow rows, as labeled by the SDK."""

    statement_type = "cash_flow_statement"
    canonical_model = "CashFlowStatement"
    capability = Capability(
        asset_class="equity",
        domain="yfinance_cash_flow_statement",
        period="fiscal_period",
        market="global",
        source=SOURCE,
        verified=False,
    )


class YfinanceIncomeStatementFetcher(YfinanceStatementFetcher):
    """Annual, quarterly, or trailing income-statement rows, as labeled by the SDK."""

    statement_type = "income_statement"
    canonical_model = "IncomeStatement"
    query_type = YfinanceIncomeStatementQuery
    capability = Capability(
        asset_class="equity",
        domain="yfinance_income_statement",
        period="fiscal_period",
        market="global",
        source=SOURCE,
        verified=False,
    )


class YfinanceHistoricalDividendsFetcher(Fetcher[YfinanceDividendQuery, pd.Series | None]):
    """Historical dividend events; no current quote fields are used as a fallback."""

    async_mode = "bounded_thread"
    canonical_model = "HistoricalDividends"
    row_model: ClassVar[type[YfinanceDividendObservation]] = YfinanceDividendObservation

    capability = Capability(
        asset_class="security",
        domain="yfinance_dividend_history",
        period="event",
        market="global",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> YfinanceDividendQuery:
        """Validate the symbol and an optional paired inclusive date window."""
        return YfinanceDividendQuery.model_validate(kwargs)

    def extract_data(self, params: YfinanceDividendQuery, ctx: FetchContext) -> pd.Series | None:
        """Read Ticker.dividends through the provider's lazy SDK seam."""
        from opendata.data.providers.yfinance.models import _sdk

        query = _validated_query(params, YfinanceDividendQuery)
        return _sdk.require_dividends_series(query.symbol)

    def transform_data(self, raw: pd.Series | None, params: YfinanceDividendQuery) -> FetchResult:
        """Normalize actual historical Series dates and amounts into event rows."""
        query = _validated_query(params, YfinanceDividendQuery)
        if not isinstance(raw, pd.Series):
            raise YfinanceProviderError("YFINANCE_DIVIDENDS_SHAPE_INVALID")
        if raw.empty or raw.index.has_duplicates:
            raise YfinanceProviderError("YFINANCE_EMPTY_OR_DUPLICATE_RESPONSE")

        rows: list[YfinanceDividendObservation] = []
        for raw_date, raw_amount in raw.items():
            event_date = _as_date(raw_date, field="ex_dividend_date")
            if query.start_date is not None and event_date < query.start_date:
                continue
            if query.end_date is not None and event_date > query.end_date:
                continue
            amount = _source_number(raw_amount, allow_missing=False)
            if amount is None:
                raise YfinanceProviderError("YFINANCE_VALUE_MISSING")
            rows.append(
                YfinanceDividendObservation(
                    symbol=query.symbol,
                    ex_dividend_date=event_date,
                    amount=amount,
                )
            )
        if not rows:
            raise YfinanceProviderError("YFINANCE_EMPTY_RESPONSE")
        return tuple(sorted(rows, key=lambda row: row.ex_dividend_date))


__all__ = [
    "YfinanceBalanceSheetFetcher",
    "YfinanceCashFlowStatementFetcher",
    "YfinanceDividendQuery",
    "YfinanceHistoricalDividendsFetcher",
    "YfinanceIncomeStatementFetcher",
    "YfinanceIncomeStatementQuery",
    "YfinanceStatementFetcher",
    "YfinanceStatementQuery",
    "YfinanceSymbolQuery",
]
