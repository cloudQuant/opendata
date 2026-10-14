"""Typed contracts for EIA petroleum and STEO report rows."""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import StrictBool, StrictFloat, StrictInt, StrictStr, field_validator

from opendata.data.models.base import ContractModel

if TYPE_CHECKING:
    from collections.abc import Hashable, Mapping
    from typing import Any


class PetroleumStatusReport(ContractModel):
    """One value from a dated EIA Weekly Petroleum Status Report table."""

    date: date_type
    table: StrictStr
    symbol: StrictStr
    order: StrictInt | None = None
    title: StrictStr | None = None
    value: StrictFloat | StrictInt
    unit: StrictStr | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _strict_date(cls, value: object) -> date_type:
        if type(value) is date_type:
            return value
        if type(value) is str and len(value) == 10:
            try:
                return date_type.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("date must be an ISO calendar date") from exc
        raise ValueError("date must be a date or YYYY-MM-DD string without a time")

    @classmethod
    def _coerce_record(cls, row: Mapping[Hashable, Any]) -> dict[str, Any]:
        if isinstance(row.get("date"), datetime):
            raise ValueError("date must not include a time")
        return super()._coerce_record(row)


class ShortTermEnergyOutlook(ContractModel):
    """One EIA STEO observation with the selected release and forecast state."""

    date: date_type
    release_date: date_type | None = None
    is_forecast: StrictBool | None = None
    table: StrictStr | None = None
    symbol: StrictStr
    order: StrictInt | None = None
    title: StrictStr | None = None
    value: StrictFloat | StrictInt
    unit: StrictStr | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _strict_date(cls, value: object) -> date_type:
        if type(value) is date_type:
            return value
        if type(value) is str and len(value) == 10:
            try:
                return date_type.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("date must be an ISO calendar date") from exc
        raise ValueError("date must be a date or YYYY-MM-DD string without a time")

    @field_validator("release_date", mode="before")
    @classmethod
    def _strict_release_date(cls, value: object) -> date_type | None:
        if value is None:
            return None
        if type(value) is date_type:
            return value
        if type(value) is str and len(value) == 10:
            try:
                return date_type.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("release_date must be an ISO calendar date") from exc
        raise ValueError("release_date must be a date or YYYY-MM-DD string without a time")

    @classmethod
    def _coerce_record(cls, row: Mapping[Hashable, Any]) -> dict[str, Any]:
        if isinstance(row.get("date"), datetime):
            raise ValueError("date must not include a time")
        return super()._coerce_record(row)
