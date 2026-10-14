"""Strict typed records for the six Kenneth French Data Library models."""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import field_validator, model_serializer

from opendata.data.models.base import ContractModel

if TYPE_CHECKING:
    from pydantic.functional_serializers import SerializerFunctionWrapHandler

FrenchFrequency = Literal["daily", "weekly", "monthly", "annual"]
FrenchReturnUnits = Literal["percent"]
FrenchBreakpointUnits = Literal["million_usd", "ratio", "percent"]
FrenchDataRequirement = Literal["not_required", "required"]
FrenchSort = Literal["market", "be_me", "e_p", "ce_p", "dividend_yield"]
FrenchPortfolioSide = Literal["market", "high", "low", "zero"]


class _DateRecord(ContractModel):
    """Shared exact-date validation; source precision is represented by frequency."""

    date: date_type

    @field_validator("date", mode="before")
    @classmethod
    def _require_date_object(cls, value: object) -> date_type:
        if type(value) is not date_type or isinstance(value, datetime):
            raise ValueError("date must be a calendar date parsed from the source period")
        return value


class _ConditionalDateRecord(_DateRecord):
    """Serialize only fields supplied by the selected source table shape."""

    @model_serializer(mode="wrap")
    def _serialize_present_fields(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        data = handler(self)
        return {name: value for name, value in data.items() if name in self.model_fields_set}


class FamaFrenchFactorObservation(_ConditionalDateRecord):
    """One date with the factor columns published for the selected dataset."""

    mkt_rf: float | None = None
    smb: float | None = None
    hml: float | None = None
    rmw: float | None = None
    cma: float | None = None
    rf: float | None = None
    mom: float | None = None
    wml: float | None = None
    lt_rev: float | None = None
    st_rev: float | None = None


class FamaFrenchBreakpoint(_ConditionalDateRecord):
    """One date with all published NYSE breakpoint percentiles."""

    num_firms: int | None = None
    num_firms_less_than_0: int | None = None
    num_firms_greater_than_0: int | None = None
    percentile_5: float | None
    percentile_10: float | None
    percentile_15: float | None
    percentile_20: float | None
    percentile_25: float | None
    percentile_30: float | None
    percentile_35: float | None
    percentile_40: float | None
    percentile_45: float | None
    percentile_50: float | None
    percentile_55: float | None
    percentile_60: float | None
    percentile_65: float | None
    percentile_70: float | None
    percentile_75: float | None
    percentile_80: float | None
    percentile_85: float | None
    percentile_90: float | None
    percentile_95: float | None
    percentile_100: float | None


class FamaFrenchPortfolioReturn(_DateRecord):
    """One public long-form portfolio return measure for a date."""

    portfolio: str
    measure: Literal["value", "equal"]
    value: float | None


class FamaFrenchCountryPortfolioReturn(_ConditionalDateRecord):
    """One date with published country returns or annual ratio fields."""

    mkt: float | None = None
    firms: int | None = None
    bm: float | None = None
    be_me_high: float | None = None
    be_me_low: float | None = None
    ep: float | None = None
    e_p_high: float | None = None
    e_p_low: float | None = None
    ce_p: float | None = None
    ce_p_high: float | None = None
    ce_p_low: float | None = None
    yld: float | None = None
    yld_high: float | None = None
    yld_low: float | None = None
    yld_zero: float | None = None


class FamaFrenchInternationalIndexReturn(_ConditionalDateRecord):
    """One date with public index return columns from a French-library archive."""

    mkt: float | None = None
    firms: int | None = None
    bm: float | None = None
    be_me_high: float | None = None
    be_me_low: float | None = None
    ep: float | None = None
    e_p_high: float | None = None
    e_p_low: float | None = None
    ce_p: float | None = None
    ce_p_high: float | None = None
    ce_p_low: float | None = None
    yld: float | None = None
    yld_high: float | None = None
    yld_low: float | None = None
    yld_zero: float | None = None
