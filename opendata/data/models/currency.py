"""Source-faithful currency reference-rate contracts."""

from __future__ import annotations

import math
import re
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, StrictStr, ValidationInfo, field_validator, model_validator

from opendata.data.models.base import ContractModel

if TYPE_CHECKING:
    from collections.abc import Hashable, Mapping

_NUMBER_PATTERN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


def _require_token(value: str, field_name: str, *, key_safe: bool = False) -> str:
    if (
        not value
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        or (key_safe and "." in value)
    ):
        raise ValueError(f"{field_name} must be a nonempty token")
    return value


def _strict_string_map(value: object, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a string-to-string map")
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise ValueError(f"{field_name} must contain only strings")
    return value


class SdmxGroupContext(ContractModel):
    """One SDMX Group in its source layer, without inferred attachment semantics."""

    group_type: StrictStr
    key: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    attributes: dict[StrictStr, StrictStr] = Field(default_factory=dict)

    @field_validator("group_type")
    @classmethod
    def _validate_group_type(cls, value: str) -> str:
        return _require_token(value, "group_type")

    @field_validator("key", "attributes", mode="before")
    @classmethod
    def _validate_string_maps(cls, value: object, info: ValidationInfo) -> dict[str, str]:
        return _strict_string_map(value, info.field_name or "map")


class CurrencyReferenceRate(ContractModel):
    """One ECB daily euro-reference observation with its raw source context."""

    series_key: StrictStr
    date: date_type
    frequency: Literal["D"]
    quote_currency: StrictStr
    base_currency: Literal["EUR"]
    rate_type: Literal["SP00"]
    rate_suffix: Literal["A"]
    value: float | None
    source_value: StrictStr | None
    dataset_attributes: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    series_attributes: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    observation_attributes: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    group_context: tuple[SdmxGroupContext, ...] = ()

    @classmethod
    def _coerce_record(cls, row: Mapping[Hashable, Any]) -> dict[str, Any]:
        """Keep frame input dates strict instead of inheriting timestamp coercion."""
        if isinstance(row.get("date"), datetime):
            raise ValueError("date must not include a time")
        return super()._coerce_record(row)

    @field_validator("date", mode="before")
    @classmethod
    def _validate_daily_date(cls, value: object) -> object:
        if isinstance(value, datetime):
            raise ValueError("date must not include a time")
        if isinstance(value, date_type):
            return value
        if not isinstance(value, str):
            raise ValueError("date must be a canonical ISO daily date")
        try:
            parsed = date_type.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date must be a canonical ISO daily date") from exc
        if parsed.isoformat() != value:
            raise ValueError("date must be a canonical ISO daily date")
        return parsed

    @field_validator("quote_currency")
    @classmethod
    def _validate_quote_currency(cls, value: str) -> str:
        return _require_token(value, "quote_currency", key_safe=True)

    @field_validator("value", mode="before")
    @classmethod
    def _validate_finite_float(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("value must be a finite number or None")
        try:
            number = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError("value must be a finite number or None") from exc
        if not math.isfinite(number):
            raise ValueError("value must be a finite number or None")
        return number

    @field_validator("source_value")
    @classmethod
    def _validate_source_number(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _NUMBER_PATTERN.fullmatch(value):
            raise ValueError("source_value must use finite decimal numeric syntax")
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("source_value must use finite decimal numeric syntax") from exc
        if not number.is_finite():
            raise ValueError("source_value must be finite")
        try:
            converted = float(number)
        except (OverflowError, ValueError) as exc:
            raise ValueError("source_value must convert to a finite float") from exc
        if not math.isfinite(converted):
            raise ValueError("source_value must convert to a finite float")
        return value

    @field_validator(
        "dataset_attributes", "series_attributes", "observation_attributes", mode="before"
    )
    @classmethod
    def _validate_attribute_maps(cls, value: object, info: ValidationInfo) -> dict[str, str]:
        return _strict_string_map(value, info.field_name or "map")

    @model_validator(mode="after")
    def _validate_series_key_and_value_pair(self) -> CurrencyReferenceRate:
        expected_key = (
            f"{self.frequency}.{self.quote_currency}.{self.base_currency}."
            f"{self.rate_type}.{self.rate_suffix}"
        )
        if self.series_key != expected_key:
            raise ValueError("series_key must exactly match the five reference-rate dimensions")
        if (self.value is None) != (self.source_value is None):
            raise ValueError("value and source_value must both be None or both be present")
        if self.source_value is not None and self.value != float(Decimal(self.source_value)):
            raise ValueError("value must equal the numeric conversion of source_value")
        return self
