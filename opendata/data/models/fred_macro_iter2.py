"""Typed contracts for evidence-backed FRED macro series adapters."""

from __future__ import annotations

from datetime import date as date_type

from pydantic import Field, PrivateAttr

from opendata.data.models.base import ContractModel


class _FredMacroProvenance(ContractModel):
    """Private source revision metadata kept out of public serialization."""

    _series_ids: tuple[str, ...] = PrivateAttr(default=())
    _realtime_by_series: dict[str, tuple[date_type, date_type]] = PrivateAttr(default_factory=dict)


class FredConsumerPriceIndex(_FredMacroProvenance):
    """One monthly observation of the U.S. all-items CPI series."""

    date: date_type = Field(strict=True)
    country: str = Field(strict=True)
    value: float = Field(strict=True)


class FredNonFarmPayroll(_FredMacroProvenance):
    """One nonfarm-payroll series observation from the BLS release table."""

    date: date_type = Field(strict=True)
    symbol: str = Field(strict=True)
    value: float = Field(strict=True)
    name: str = Field(strict=True)
    element_id: str = Field(strict=True)
    parent_id: str | None = Field(default=None, strict=True)
    children: str | None = Field(default=None, strict=True)
    level: int = Field(strict=True)


class FredPersonalConsumptionExpenditure(_FredMacroProvenance):
    """One PCE series observation from the BEA release table."""

    date: date_type = Field(strict=True)
    symbol: str = Field(strict=True)
    value: float = Field(strict=True)
    name: str = Field(strict=True)
    element_id: str = Field(strict=True)
    parent_id: str | None = Field(default=None, strict=True)
    children: str | None = Field(default=None, strict=True)
    level: int = Field(strict=True)
    line: int = Field(strict=True)


class FredUniversityOfMichigan(_FredMacroProvenance):
    """A date-keyed merge of Michigan sentiment and inflation expectations."""

    date: date_type = Field(strict=True)
    consumer_sentiment: float | None = Field(default=None, strict=True)
    inflation_expectation: float | None = Field(default=None, strict=True)


class FredMortgageIndex(_FredMacroProvenance):
    """One observation from an explicitly selected mortgage-rate index."""

    date: date_type = Field(strict=True)
    symbol: str = Field(strict=True)
    name: str = Field(strict=True)
    rate: float = Field(strict=True)


class _FredManufacturingOutlook(_FredMacroProvenance):
    date: date_type = Field(strict=True)
    topic: str = Field(strict=True)
    diffusion_index: float | None = Field(default=None, strict=True)
    percent_reporting_increase: float | None = Field(default=None, strict=True)
    percent_reporting_decrease: float | None = Field(default=None, strict=True)
    percent_reporting_no_change: float | None = Field(default=None, strict=True)


class FredManufacturingOutlookNY(_FredManufacturingOutlook):
    """One topic observation from the New York Fed manufacturing survey."""

    pass


class FredManufacturingOutlookTexas(_FredManufacturingOutlook):
    """One topic observation from the Dallas Fed manufacturing survey."""

    pass


class FredSurveyOfEconomicConditionsChicago(_FredMacroProvenance):
    """A date-keyed merge of Chicago Fed survey components."""

    date: date_type = Field(strict=True)
    activity_index: float | None = Field(default=None, strict=True)
    one_year_outlook: float | None = Field(default=None, strict=True)
    manufacturing_activity: float | None = Field(default=None, strict=True)
    non_manufacturing_activity: float | None = Field(default=None, strict=True)
    capital_expenditures_expectations: float | None = Field(default=None, strict=True)
    hiring_expectations: float | None = Field(default=None, strict=True)
    current_hiring: float | None = Field(default=None, strict=True)
    labor_costs: float | None = Field(default=None, strict=True)
    non_labor_costs: float | None = Field(default=None, strict=True)


class FredRetailPrice(_FredMacroProvenance):
    """One official FRED average-retail-price series observation."""

    date: date_type = Field(strict=True)
    symbol: str = Field(strict=True)
    country: str = Field(strict=True)
    description: str = Field(strict=True)
    value: float | None = Field(default=None, strict=True)


class FredSeniorLoanOfficerSurvey(ContractModel):
    """One observation and its official Federal Reserve survey-series label."""

    date: date_type = Field(strict=True)
    symbol: str = Field(strict=True)
    value: float = Field(strict=True)
    title: str = Field(strict=True)


CONTRACTS: tuple[type[ContractModel], ...] = (
    FredConsumerPriceIndex,
    FredNonFarmPayroll,
    FredPersonalConsumptionExpenditure,
    FredUniversityOfMichigan,
    FredMortgageIndex,
    FredManufacturingOutlookNY,
    FredManufacturingOutlookTexas,
    FredSurveyOfEconomicConditionsChicago,
    FredRetailPrice,
    FredSeniorLoanOfficerSurvey,
)


__all__ = (
    "FredConsumerPriceIndex",
    "FredManufacturingOutlookNY",
    "FredManufacturingOutlookTexas",
    "FredMortgageIndex",
    "FredNonFarmPayroll",
    "FredPersonalConsumptionExpenditure",
    "FredRetailPrice",
    "FredSeniorLoanOfficerSurvey",
    "FredSurveyOfEconomicConditionsChicago",
    "FredUniversityOfMichigan",
)
