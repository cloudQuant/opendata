"""Typed output for the bounded SEC 13F filing-manager directory."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import Field, field_validator, model_validator

from opendata.data.models.base import ContractModel

_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SecInstitutionSearchResult(ContractModel):
    """Public institution name/CIK plus filing-batch lineage.

    ``name`` and ``cik`` are the public InstitutionsSearch fields. The
    remaining fields make the selected SEC quarterly dataset and the source
    filing visible; they do not imply complete historical institution
    coverage.
    """

    name: str | None
    cik: str
    source_batch: str
    as_of: date
    filing_window_start: date
    filing_window_end: date
    filing_date: date
    submission_type: Literal["13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A"]
    accession_number: str
    report_calendar_or_quarter: date
    submission_line_number: int = Field(ge=2, strict=True)
    coverpage_line_number: int = Field(ge=2, strict=True)

    @field_validator("cik", mode="before")
    @classmethod
    def _normalize_cik(cls, value: object) -> str:
        """Normalize numeric and textual CIKs to SEC's ten-digit string form."""
        if isinstance(value, bool):
            raise ValueError("cik must contain one to ten digits")
        if isinstance(value, int):
            digits = str(value)
        elif isinstance(value, str):
            digits = value.strip()
        else:
            raise ValueError("cik must contain one to ten digits")
        if not digits.isdigit() or not 1 <= len(digits) <= 10 or int(digits) <= 0:
            raise ValueError("cik must contain one to ten nonzero digits")
        return digits.zfill(10)

    @field_validator("name", mode="before")
    @classmethod
    def _normalize_name(cls, value: object) -> str | None:
        """Represent blank SEC names as null and normalize visible whitespace."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("name must be text or null")
        normalized = " ".join(value.split())
        if not normalized:
            return None
        if len(normalized) > 150 or _CONTROL_RE.search(normalized) is not None:
            raise ValueError("name contains unsupported text")
        return normalized

    @field_validator("source_batch")
    @classmethod
    def _safe_batch_filename(cls, value: str) -> str:
        """Keep the batch lineage to a basename, never an arbitrary path."""
        if (
            not value
            or len(value) > 100
            or "/" in value
            or "\\" in value
            or not value.casefold().endswith("_form13f.zip")
        ):
            raise ValueError("source_batch must be an SEC Form 13F ZIP basename")
        return value

    @field_validator("accession_number")
    @classmethod
    def _valid_accession(cls, value: str) -> str:
        if _ACCESSION_RE.fullmatch(value) is None:
            raise ValueError("accession_number must use the EDGAR accession format")
        return value

    @model_validator(mode="after")
    def _valid_filing_window(self) -> SecInstitutionSearchResult:
        if self.filing_window_start > self.filing_window_end:
            raise ValueError("filing window start must not follow its end")
        return self
