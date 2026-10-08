"""Period-preserving economic series contracts used by data providers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal, overload

from pydantic import Field, StrictStr, field_validator

from opendata.data.models.base import ContractModel


class BlsCatalogItem(ContractModel):
    """One BLS series catalog row with survey-specific dimensions retained."""

    series_id: str
    survey: str
    title: str | None = None
    frequency: str | None = None
    units: str | None = None
    dimensions: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    source_metadata: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    catalog_as_of: date | None = None

    @field_validator("series_id", "survey")
    @classmethod
    def _require_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("title", "frequency", "units")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


@dataclass(frozen=True)
class BlsCatalogPage(Sequence[BlsCatalogItem]):
    """A bounded catalog window plus the full matching total."""

    items: tuple[BlsCatalogItem, ...]
    total: int
    offset: int
    limit: int

    def __post_init__(self) -> None:
        """Keep page metadata consistent with the returned window."""
        if self.total < 0 or self.offset < 0 or self.limit < 1:
            raise ValueError("invalid BLS catalog page metadata")
        if len(self.items) > self.limit:
            raise ValueError("BLS catalog page exceeds its limit")

    def __len__(self) -> int:
        """Return the number of items in this page."""
        return len(self.items)

    @overload
    def __getitem__(self, index: int) -> BlsCatalogItem: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[BlsCatalogItem, ...]: ...

    def __getitem__(self, index: int | slice) -> BlsCatalogItem | tuple[BlsCatalogItem, ...]:
        """Index or slice the page's typed catalog rows."""
        return self.items[index]


class BlsFootnote(ContractModel):
    """One source footnote, including the empty object BLS uses for no note."""

    code: str | None = None
    text: str | None = None


class BlsObservation(ContractModel):
    """One BLS observation, preserving its source year and period code."""

    series_id: str
    year: int = Field(ge=1000, le=9999, strict=True)
    period: str
    period_name: str
    value: float
    footnotes: tuple[BlsFootnote, ...]
    latest: bool | None = None
    preliminary: bool
    api_version: Literal["v1", "v2"]

    @field_validator("series_id", "period", "period_name")
    @classmethod
    def _require_nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value
