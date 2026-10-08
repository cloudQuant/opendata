"""Source-faithful observation contracts for ECB YC and BPS data flows.

The dimension values remain opaque source tokens. These models preserve the
products' different observation periods: daily dates for YC and monthly or
quarterly period strings for BPS.
"""

from __future__ import annotations

import math
import re
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import Field, StrictStr, ValidationInfo, field_validator, model_validator

from opendata.data.models.base import ContractModel
from opendata.data.models.currency import _NUMBER_PATTERN, SdmxGroupContext, _strict_string_map

if TYPE_CHECKING:
    from collections.abc import Hashable, Mapping

_DIMENSION_TOKEN_PATTERN = re.compile(r"[A-Z0-9_][A-Z0-9_-]{0,31}\Z")
_MONTH_PERIOD_PATTERN = re.compile(r"(?P<year>[0-9]{4})-(?P<month>0[1-9]|1[0-2])\Z")
_QUARTER_PERIOD_PATTERN = re.compile(r"(?P<year>[0-9]{4})-Q(?P<quarter>[1-4])\Z")

__all__ = ("EcbBalanceOfPaymentsObservation", "EcbYieldCurveObservation")


def _validate_dimension_token(value: str, field_name: str) -> str:
    if not _DIMENSION_TOKEN_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a single uppercase source token")
    return value


def _validate_period_year(value: str) -> None:
    year = int(value[:4])
    if not 1 <= year <= 9999:
        raise ValueError("period year must be between 0001 and 9999")


class _EcbSeriesObservation(ContractModel):
    """Shared source-preserving values, attributes, and series-key validation."""

    _KEY_PREFIX: ClassVar[str] = ""
    _DIMENSION_FIELDS: ClassVar[tuple[str, ...]] = ()

    series_key: StrictStr
    value: float | None
    source_value: StrictStr | None
    dataset_attributes: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    series_attributes: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    observation_attributes: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    group_context: tuple[SdmxGroupContext, ...] = ()

    @field_validator("value", mode="before")
    @classmethod
    def _validate_value(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("value must be a finite non-boolean int, float, or None")
        try:
            converted = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError("value must be a finite non-boolean int, float, or None") from exc
        if not math.isfinite(converted):
            raise ValueError("value must be a finite non-boolean int, float, or None")
        return converted

    @field_validator("source_value")
    @classmethod
    def _validate_source_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _NUMBER_PATTERN.fullmatch(value):
            raise ValueError("source_value must use finite decimal numeric syntax")
        try:
            decimal_value = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("source_value must use finite decimal numeric syntax") from exc
        if not decimal_value.is_finite():
            raise ValueError("source_value must be finite")
        try:
            converted = float(decimal_value)
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
        return _strict_string_map(value, info.field_name or "attributes")

    @model_validator(mode="after")
    def _validate_series_key_and_value_pair(self) -> _EcbSeriesObservation:
        dimensions = ".".join(getattr(self, field_name) for field_name in self._DIMENSION_FIELDS)
        expected_key = f"{self._KEY_PREFIX}.{dimensions}"
        if self.series_key != expected_key:
            raise ValueError(
                "series_key must exactly match the product dimensions in official order"
            )
        if (self.value is None) != (self.source_value is None):
            raise ValueError("value and source_value must both be None or both be present")
        if self.source_value is not None:
            converted_source_value = float(Decimal(self.source_value))
            zero_sign_mismatch = (
                self.value is not None
                and self.value == 0.0
                and math.copysign(1.0, self.value) != math.copysign(1.0, converted_source_value)
            )
            if self.value != converted_source_value or zero_sign_mismatch:
                raise ValueError("value must equal the float conversion of source_value")
        return self


class EcbYieldCurveObservation(_EcbSeriesObservation):
    """One ECB YC daily observation with all seven source dimensions."""

    _KEY_PREFIX: ClassVar[str] = "YC"
    _DIMENSION_FIELDS: ClassVar[tuple[str, ...]] = (
        "frequency",
        "ref_area",
        "currency",
        "provider_fm",
        "instrument_fm",
        "provider_fm_id",
        "data_type_fm",
    )

    frequency: Literal["B"]
    ref_area: StrictStr
    currency: StrictStr
    provider_fm: StrictStr
    instrument_fm: StrictStr
    provider_fm_id: StrictStr
    data_type_fm: StrictStr
    date: date_type

    @field_validator(
        "ref_area",
        "currency",
        "provider_fm",
        "instrument_fm",
        "provider_fm_id",
        "data_type_fm",
    )
    @classmethod
    def _validate_dimension_tokens(cls, value: str, info: ValidationInfo) -> str:
        return _validate_dimension_token(value, info.field_name or "dimension")

    @field_validator("date", mode="before")
    @classmethod
    def _validate_daily_date(cls, value: object) -> object:
        if isinstance(value, datetime):
            raise ValueError("date must not include a time")
        if isinstance(value, date_type):
            return value
        if not isinstance(value, str):
            raise ValueError("date must be a canonical YYYY-MM-DD date")
        try:
            parsed = date_type.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date must be a canonical YYYY-MM-DD date") from exc
        if parsed.isoformat() != value:
            raise ValueError("date must be a canonical YYYY-MM-DD date")
        return parsed

    @classmethod
    def _coerce_record(cls, row: Mapping[Hashable, Any]) -> dict[str, Any]:
        """Reject frame timestamps before the base contract can drop time precision."""
        if isinstance(row.get("date"), datetime):
            raise ValueError("date must not include a time")
        return super()._coerce_record(row)


class EcbBalanceOfPaymentsObservation(_EcbSeriesObservation):
    """One ECB BPS monthly or quarterly observation with all 17 dimensions."""

    _KEY_PREFIX: ClassVar[str] = "BPS"
    _DIMENSION_FIELDS: ClassVar[tuple[str, ...]] = (
        "frequency",
        "adjustment",
        "ref_area",
        "counterpart_area",
        "ref_sector",
        "counterpart_sector",
        "flow_stock_entry",
        "accounting_entry",
        "int_acc_item",
        "functional_cat",
        "instr_asset",
        "maturity",
        "unit_measure",
        "currency_denom",
        "valuation",
        "comp_method",
        "type_entity",
    )

    frequency: Literal["M", "Q"]
    adjustment: StrictStr
    ref_area: StrictStr
    counterpart_area: StrictStr
    ref_sector: StrictStr
    counterpart_sector: StrictStr
    flow_stock_entry: StrictStr
    accounting_entry: StrictStr
    int_acc_item: StrictStr
    functional_cat: StrictStr
    instr_asset: StrictStr
    maturity: StrictStr
    unit_measure: StrictStr
    currency_denom: StrictStr
    valuation: StrictStr
    comp_method: StrictStr
    type_entity: StrictStr
    period: StrictStr

    @field_validator(
        "adjustment",
        "ref_area",
        "counterpart_area",
        "ref_sector",
        "counterpart_sector",
        "flow_stock_entry",
        "accounting_entry",
        "int_acc_item",
        "functional_cat",
        "instr_asset",
        "maturity",
        "unit_measure",
        "currency_denom",
        "valuation",
        "comp_method",
        "type_entity",
    )
    @classmethod
    def _validate_dimension_tokens(cls, value: str, info: ValidationInfo) -> str:
        return _validate_dimension_token(value, info.field_name or "dimension")

    @field_validator("period")
    @classmethod
    def _validate_period_shape(cls, value: str) -> str:
        month_match = _MONTH_PERIOD_PATTERN.fullmatch(value)
        quarter_match = _QUARTER_PERIOD_PATTERN.fullmatch(value)
        match = month_match or quarter_match
        if match is None:
            raise ValueError("period must be YYYY-MM or YYYY-Qn")
        _validate_period_year(match.group("year"))
        return value

    @model_validator(mode="after")
    def _validate_frequency_period_pair(self) -> EcbBalanceOfPaymentsObservation:
        if self.frequency == "M" and not _MONTH_PERIOD_PATTERN.fullmatch(self.period):
            raise ValueError("monthly frequency requires a YYYY-MM period")
        if self.frequency == "Q" and not _QUARTER_PERIOD_PATTERN.fullmatch(self.period):
            raise ValueError("quarterly frequency requires a YYYY-Qn period")
        return self
