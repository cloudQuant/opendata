"""Typed rows and query parameters for the FRED-backed SLOOS survey."""

from __future__ import annotations

from datetime import date as date_type
from typing import Literal

from pydantic import Field, field_validator, model_validator

from opendata.data.models.base import ContractModel
from opendata.data.protocol import QueryParams

SloosCategory = Literal[
    "spreads",
    "consumer",
    "auto",
    "credit_card",
    "firms",
    "mortgage",
    "commercial_real_estate",
    "standards",
    "demand",
    "foreign_banks",
]
SloosTransform = Literal["chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log"]
SloosFREDTransform = Literal[
    "lin",
    "chg",
    "ch1",
    "pch",
    "pc1",
    "pca",
    "cch",
    "cca",
    "log",
]
SloosSelectionScope = Literal[
    "all_banks_candidate_series",
    "all_banks_consumer_excludes_heloc",
    "all_banks_home_mortgage_excludes_heloc",
    "foreign_bank_lending_net_percentages_only",
]


class FredSeniorLoanOfficerSurvey(ContractModel):
    """One FRED observation with its Board-series and candidate-scope provenance.

    ``value`` is the numeric value returned by FRED for ``transform``. The
    source unit label and multiplier describe the retained Board crosswalk;
    this adapter does not rescale percentages or other source values.
    """

    date: date_type
    symbol: str
    value: float | None
    title: str | None
    category: SloosCategory
    board_series_id: str
    fred_series_id: str
    panel: Literal["DOM", "FGN"]
    bank_size: Literal["ALL"]
    loan_type: str
    measure: str
    terms: str
    source_unit_label: Literal["Percent", "Percentage"]
    source_unit_multiplier: Literal[1]
    source_frequency_code: Literal["162"]
    transform: SloosFREDTransform
    selection_scope: SloosSelectionScope
    categories_are_additive: Literal[False] = False


class FredSeniorLoanOfficerSurveyQuery(QueryParams):
    """One public category selection with a bounded, complete FRED fan-out.

    ``max_requests`` is a local request-count guardrail, not a FRED-published
    quota. A category is rejected before network access when the whole
    selected series set cannot fit in the caller's request budget. The
    default ``max_records`` bounds total rows across all selected series.
    """

    source: Literal["auto", "fred"] = "auto"
    market: Literal["us"] | None = None
    symbol: None = None
    category: SloosCategory = "spreads"
    transform: SloosTransform | None = None
    max_requests: int = Field(default=32, ge=1, le=32, strict=True)
    max_records: int = Field(default=10_000, ge=1, le=100_000, strict=True)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _validate_sloos_date(cls, value: object) -> date_type | None:
        """Accept only date objects or canonical YYYY-MM-DD strings."""
        if value is None:
            return None
        if type(value) is date_type:
            return value
        if type(value) is not str or len(value) != 10:
            raise ValueError("date bounds must be canonical YYYY-MM-DD dates")
        try:
            parsed = date_type.fromisoformat(value)
        except ValueError as error:
            raise ValueError("date bounds must be valid YYYY-MM-DD dates") from error
        if parsed.isoformat() != value:
            raise ValueError("date bounds must be canonical YYYY-MM-DD dates")
        return parsed

    @model_validator(mode="after")
    def _validate_date_window(self) -> FredSeniorLoanOfficerSurveyQuery:
        """Reject reversed date windows and all symbol-specific selection."""
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must be on or before end_date")
        return self
