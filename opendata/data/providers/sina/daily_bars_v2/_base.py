"""Pure, offline normalization for explicitly profiled Sina daily-bar frames."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal
from numbers import Real
from typing import ClassVar

import numpy as np
import pandas as pd

from opendata.data.providers.sina.daily_bars_v2.contract import (
    SinaSourceProfile,
    SourceNativeBarV2,
)

_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_MIDNIGHT_NAIVE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]00:00(?::00)?\Z")


class SinaDailyBarV2Normalizer:
    """Project a DataFrame onto OHLCV using an exact source-column profile.

    The class is intentionally offline: it has no fetch method and imports no
    upstream client. Callers must supply the raw frame they obtained through a
    separately authorized and governed collection path. The projection can
    discard columns recorded in the profile; callers needing full fidelity
    must retain the original source frame separately.
    """

    profile: ClassVar[SinaSourceProfile]

    def __init__(self, profile: SinaSourceProfile | None = None) -> None:
        """Bind the source profile, optionally for a narrowly scoped test."""
        selected = profile or getattr(type(self), "profile", None)
        if selected is None:
            raise TypeError("a SinaSourceProfile is required")
        self._profile = selected

    @property
    def source_profile(self) -> SinaSourceProfile:
        """The immutable source-column and unit-status declaration."""
        return self._profile

    def normalize(
        self,
        raw: pd.DataFrame,
        *,
        symbol: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> tuple[SourceNativeBarV2, ...]:
        """Return source-profiled OHLCV projections in date order.

        Args:
            raw: DataFrame with the exact columns declared by ``profile``.
            symbol: Source-native instrument key associated with this frame.
            start_date: Optional inclusive lower bound.
            end_date: Optional inclusive upper bound.

        Returns:
            Immutable OHLCV projection rows, with ``amount`` always set and
            projection metadata identifying source fields that were omitted.

        Raises:
            TypeError: If the frame or symbol has an invalid type.
            ValueError: If the frame schema or any mapped source value is
                malformed, or if a duplicate symbol/date key is present.
        """
        if not isinstance(raw, pd.DataFrame):
            raise TypeError("raw must be a pandas DataFrame")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        if start_date is not None and (
            not isinstance(start_date, date) or isinstance(start_date, datetime)
        ):
            raise TypeError("start_date must be a date or None")
        if end_date is not None and (
            not isinstance(end_date, date) or isinstance(end_date, datetime)
        ):
            raise TypeError("end_date must be a date or None")
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ValueError("start_date must be on or before end_date")

        self._validate_columns(raw)
        normalized_symbol = self.normalize_symbol(symbol)
        rows: list[SourceNativeBarV2] = []
        seen_dates: set[date] = set()

        for row_number, record in enumerate(raw.to_dict("records"), start=1):
            trade_date = self._trade_date(record[self.source_profile.date_column], row_number)
            if start_date is not None and trade_date < start_date:
                continue
            if end_date is not None and trade_date > end_date:
                continue
            if trade_date in seen_dates:
                raise ValueError(
                    f"{self.source_profile.name}: duplicate trade date {trade_date.isoformat()}"
                )
            seen_dates.add(trade_date)

            amount = None
            if self.source_profile.amount_column is not None:
                amount = self._number(
                    record[self.source_profile.amount_column],
                    field=self.source_profile.amount_column,
                    row_number=row_number,
                    nullable=True,
                )

            open_value = self._required_number(
                record[self.source_profile.open_column], "open", row_number
            )
            high_value = self._required_number(
                record[self.source_profile.high_column], "high", row_number
            )
            low_value = self._required_number(
                record[self.source_profile.low_column], "low", row_number
            )
            close_value = self._required_number(
                record[self.source_profile.close_column], "close", row_number
            )
            volume_value = self._required_number(
                record[self.source_profile.volume_column], "volume", row_number
            )
            self._validate_ohlc(
                open_value,
                high_value,
                low_value,
                close_value,
                row_number=row_number,
            )

            rows.append(
                SourceNativeBarV2(
                    source=self.source_profile.source,
                    source_profile=self.source_profile.name,
                    asset_class=self.source_profile.asset_class,
                    domain=self.source_profile.domain,
                    projection="ohlcv_projection",
                    excluded_source_columns=self.source_profile.excluded_source_columns,
                    source_frame_retention_required=True,
                    symbol=normalized_symbol,
                    trade_date=trade_date,
                    open=open_value,
                    high=high_value,
                    low=low_value,
                    close=close_value,
                    volume=volume_value,
                    amount=amount,
                    price_unit_status=self.source_profile.price_unit_status,
                    volume_unit_status=self.source_profile.volume_unit_status,
                    amount_unit_status=self.source_profile.amount_unit_status,
                )
            )

        return tuple(sorted(rows, key=lambda bar: bar.trade_date))

    def normalize_symbol(self, symbol: str) -> str:
        """Return the source key unchanged unless a domain adapter overrides it."""
        return symbol.strip()

    def _validate_columns(self, raw: pd.DataFrame) -> None:
        if raw.columns.has_duplicates:
            raise ValueError(f"{self.source_profile.name}: duplicate column names are not allowed")
        actual = tuple(str(name) for name in raw.columns)
        expected = self.source_profile.expected_columns
        if set(actual) != set(expected) or len(actual) != len(expected):
            missing = sorted(set(expected) - set(actual))
            unexpected = sorted(set(actual) - set(expected))
            raise ValueError(
                f"{self.source_profile.name}: source schema drift; "
                f"missing columns={missing}, unexpected columns={unexpected}"
            )

    def _required_number(self, value: object, field: str, row_number: int) -> float:
        number = self._number(value, field=field, row_number=row_number, nullable=False)
        if number is None:
            raise ValueError(f"{field} cannot be null")
        return number

    def _trade_date(self, value: object, row_number: int) -> date:
        if isinstance(value, datetime):
            if value.tzinfo is not None or value.time().isoformat() != "00:00:00":
                raise ValueError(
                    f"{self.source_profile.name}: row {row_number} trade date must be "
                    "date-only or a timezone-naive midnight"
                )
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            text = value.strip()
            try:
                if _DATE_ONLY.fullmatch(text):
                    return date.fromisoformat(text)
                if _MIDNIGHT_NAIVE.fullmatch(text):
                    parsed = datetime.fromisoformat(text)
                    if parsed.tzinfo is None and parsed.time().isoformat() == "00:00:00":
                        return parsed.date()
            except ValueError as exc:
                raise ValueError(
                    f"{self.source_profile.name}: row {row_number} has an invalid trade date"
                ) from exc
            raise ValueError(
                f"{self.source_profile.name}: row {row_number} trade date must be "
                "date-only or a timezone-naive midnight"
            )
        raise ValueError(f"{self.source_profile.name}: row {row_number} has an invalid trade date")

    def _validate_ohlc(
        self,
        open_value: float,
        high_value: float,
        low_value: float,
        close_value: float,
        *,
        row_number: int,
    ) -> None:
        if (
            high_value < low_value
            or high_value < open_value
            or high_value < close_value
            or low_value > open_value
            or low_value > close_value
        ):
            raise ValueError(
                f"{self.source_profile.name}: row {row_number} has inconsistent OHLC values"
            )

    def _number(
        self,
        value: object,
        *,
        field: str,
        row_number: int,
        nullable: bool,
    ) -> float | None:
        if self._is_missing(value):
            if nullable:
                return None
            raise ValueError(
                f"{self.source_profile.name}: row {row_number} has missing required value {field!r}"
            )
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, (Real, Decimal)):
            raise ValueError(
                f"{self.source_profile.name}: row {row_number} value {field!r} must be numeric"
            )
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(
                f"{self.source_profile.name}: row {row_number} value {field!r} is invalid"
            ) from exc
        if not math.isfinite(number):
            raise ValueError(
                f"{self.source_profile.name}: row {row_number} value {field!r} must be finite"
            )
        if field == "volume" and number < 0:
            raise ValueError(
                f"{self.source_profile.name}: row {row_number} volume must be non-negative"
            )
        return number

    @staticmethod
    def _is_missing(value: object) -> bool:
        if isinstance(
            value,
            (list, tuple, dict, set, frozenset, np.ndarray, pd.Series, pd.DataFrame),
        ):
            raise ValueError("source cells must contain scalar values")
        if value is None or value is pd.NA or value is pd.NaT:
            return True
        if isinstance(value, (np.datetime64, np.timedelta64)):
            return bool(np.isnat(value))
        if isinstance(value, (bool, np.bool_)):
            return False
        if isinstance(value, Decimal):
            return value.is_nan()
        if isinstance(value, Real):
            return bool(value != value)
        return False
