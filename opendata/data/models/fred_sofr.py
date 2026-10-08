"""Source-faithful contracts for FRED SOFR observations."""

from __future__ import annotations

import math
import re
from datetime import date as date_type
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import StrictFloat, StrictStr, field_validator, model_validator

from opendata.data.models.economic_series import SeriesObservation

if TYPE_CHECKING:
    from collections.abc import Hashable, Mapping
    from typing import Any


class FredSofrObservation(SeriesObservation):
    """One SOFR observation with its original FRED value token and native unit."""

    series_id: Literal["SOFR", "SOFR30DAYAVG", "SOFR90DAYAVG", "SOFR180DAYAVG", "SOFRINDEX"]
    value: StrictFloat | None
    source_value: StrictStr
    native_units: Literal["Percent", "Index Apr 2, 2018 = 1"]
    date: date_type
    realtime_start: date_type
    realtime_end: date_type

    @field_validator("value", mode="before")
    @classmethod
    def _require_float_or_none(cls, value: object) -> object:
        if value is not None and type(value) is not float:
            raise ValueError("value must be a float or None")
        return value

    @field_validator("output_type", mode="before")
    @classmethod
    def _require_exact_output_type_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("output_type must be an integer from 1 through 4")
        return value

    @field_validator("date", "realtime_start", "realtime_end", mode="before")
    @classmethod
    def _validate_strict_date(cls, value: object) -> date_type:
        if type(value) is date_type:
            return value
        if type(value) is not str or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
            raise ValueError("date fields must be date objects or strict YYYY-MM-DD strings")
        try:
            return date_type.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date fields must be valid YYYY-MM-DD dates") from exc

    @field_validator("source_value")
    @classmethod
    def _validate_source_value_token(cls, value: str) -> str:
        if value == ".":
            return value
        if (
            re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value)
            is None
        ):
            raise ValueError("source_value must be '.' or a decimal numeric token")
        try:
            converted = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError("source_value must convert to a finite float") from exc
        if not math.isfinite(converted):
            raise ValueError("source_value must convert to a finite float")
        return value

    @model_validator(mode="after")
    def _validate_source_value_and_units(self) -> FredSofrObservation:
        expected_units = "Index Apr 2, 2018 = 1" if self.series_id == "SOFRINDEX" else "Percent"
        if self.native_units != expected_units:
            raise ValueError("native_units must match the native units of series_id")

        if self.source_value == ".":
            if self.value is not None:
                raise ValueError("source_value '.' requires value=None")
            return self
        if self.value is None:
            raise ValueError("numeric source_value requires a float value")

        converted = float(self.source_value)
        zero_sign_mismatch = self.value == 0.0 and math.copysign(1.0, self.value) != math.copysign(
            1.0, converted
        )
        if self.value != converted or zero_sign_mismatch:
            raise ValueError("value must equal float(source_value), including zero sign")
        return self

    @classmethod
    def _coerce_record(cls, row: Mapping[Hashable, Any]) -> dict[str, Any]:
        """Reject timestamps before the base contract can discard their time component."""
        for field_name in ("date", "realtime_start", "realtime_end"):
            if isinstance(row.get(field_name), datetime):
                raise ValueError(f"{field_name} must not include a time")
        return super()._coerce_record(row)
