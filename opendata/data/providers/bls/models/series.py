"""BLS time-series observations with explicit v1/v2 query budgets."""

from __future__ import annotations

import math
import re
from datetime import date as date_type  # noqa: TC003 - Pydantic resolves at runtime.
from typing import ClassVar

from pydantic import Field, StrictStr, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.bls.models import _client
from opendata.data.providers.bls.models._client import BlsProviderError
from opendata.data.providers.bls.models._contracts import BlsFootnote, BlsObservation


class BlsSeriesQuery(QueryParams):
    """Requested IDs and a complete inclusive year or date window."""

    series_ids: list[StrictStr] = Field(min_length=1)
    start_year: int | None = Field(default=None, strict=True, ge=1000, le=9999)
    end_year: int | None = Field(default=None, strict=True, ge=1000, le=9999)
    as_of: date_type | None = None
    max_requests: int = Field(ge=1, le=500, strict=True)

    @field_validator("series_ids")
    @classmethod
    def _validate_series_ids(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            value = value.strip()
            if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("series IDs must be nonempty and contain no control characters")
            cleaned.append(value)
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("series_ids must be unique")
        return cleaned

    @model_validator(mode="after")
    def _validate_window(self) -> BlsSeriesQuery:
        has_year = self.start_year is not None or self.end_year is not None
        has_date = self.start_date is not None or self.end_date is not None
        if has_year and has_date:
            raise ValueError("choose either a year window or a date window")
        if has_year:
            if self.start_year is None or self.end_year is None:
                raise ValueError("start_year and end_year must be supplied together")
            if self.start_year > self.end_year:
                raise ValueError("start_year must not be after end_year")
        elif has_date:
            if self.start_date is None or self.end_date is None:
                raise ValueError("start_date and end_date must be supplied together")
            if self.start_date > self.end_date:
                raise ValueError("start_date must not be after end_date")
        else:
            raise ValueError("a complete year or date window is required")
        if self.as_of is not None:
            raise ValueError("BLS has no official vintage API; as_of is unsupported")
        return self

    @property
    def year_window(self) -> tuple[int, int]:
        """Return the inclusive calendar years represented by this query."""
        if self.start_year is not None and self.end_year is not None:
            return self.start_year, self.end_year
        if self.start_date is None or self.end_date is None:  # pragma: no cover - validated above
            raise ValueError("a complete year or date window is required")
        return self.start_date.year, self.end_date.year


class BlsSeriesFetcher(Fetcher[BlsSeriesQuery, list[dict[str, object]]]):
    """Retrieve one or more BLS series without dropping year or period data."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "BlsSeries"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="bls_series",
        period="variable",
        market="us",
        source="bls",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> BlsSeriesQuery:
        """Reject malformed windows, duplicate IDs, and as-of requests pre-I/O."""
        return BlsSeriesQuery.model_validate(kwargs)

    def extract_data(self, params: BlsSeriesQuery, ctx: FetchContext) -> list[dict[str, object]]:
        """Fetch every required ID/year batch within the explicit request budget."""
        start_year, end_year = params.year_window
        return _client.fetch_observations(
            series_ids=tuple(params.series_ids),
            start_year=start_year,
            end_year=end_year,
            max_requests=params.max_requests,
            timeout=ctx.timeout,
        )

    def transform_data(
        self,
        raw: list[dict[str, object]],
        params: BlsSeriesQuery,
    ) -> FetchResult:
        """Parse numeric values and derive preliminary only from a P footnote."""
        del params
        try:
            rows = [_normalize_observation(item) for item in raw]
            return tuple(sorted(rows, key=lambda row: (row.series_id, row.year, row.period)))
        except (TypeError, ValueError, OverflowError):
            raise BlsProviderError("BLS_BAD_OBSERVATION") from None


def _normalize_observation(item: dict[str, object]) -> BlsObservation:
    """Validate one raw observation without assigning units or a vintage."""
    series_id = item.get("series_id")
    year_text = item.get("year")
    value_text = item.get("value")
    raw_footnotes = item.get("footnotes")
    if (
        not isinstance(series_id, str)
        or not isinstance(year_text, str)
        or len(year_text) != 4
        or not year_text.isdigit()
        or not isinstance(value_text, str)
        or not isinstance(raw_footnotes, list)
    ):
        raise ValueError("invalid BLS observation fields")
    value = _parse_value(value_text)
    footnotes = tuple(BlsFootnote.model_validate(note) for note in raw_footnotes)
    preliminary = any(note.code == "P" for note in footnotes)
    return BlsObservation.model_validate(
        {
            "series_id": series_id,
            "year": int(year_text),
            "period": item.get("period"),
            "period_name": item.get("period_name"),
            "value": value,
            "footnotes": footnotes,
            "latest": item.get("latest"),
            "preliminary": preliminary,
            "api_version": item.get("api_version"),
        }
    )


def _parse_value(value_text: str) -> float:
    """Accept finite numeric source strings; no undocumented missing marker is inferred."""
    text = value_text.strip()
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?", text) is None:
        raise ValueError("BLS observation value is empty")
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("BLS observation value must be finite")
    return value
