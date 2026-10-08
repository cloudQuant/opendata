"""Source-faithful FRED IORB observations with bounded request execution."""

from __future__ import annotations

from typing import Any, ClassVar

from opendata.data.capability import Capability
from opendata.data.models.fred_iorb import FredIorbObservation
from opendata.data.protocol import FetchContext, Fetcher, FetchResult
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models._iorb_client import (
    fetch_iorb_pages,
    validate_iorb_pages,
)
from opendata.data.providers.fred.models._iorb_query import FredIorbQuery
from opendata.data.request_budget import RequestBudgetError


class FredIorbFetcher(Fetcher[FredIorbQuery, tuple[dict[str, Any], ...]]):
    """Fetch and normalize complete IORB page chains without losing source tokens.

    The client owns pagination, transport admission, and page validation. This
    adapter preserves the raw page envelopes for capture and returns normalized
    rows only after every source value passes the central IORB contract.
    Daily/native-frequency rows respect the requested date window; requested
    low-frequency labels remain as supplied by FRED without inferred filtering
    or window rewriting. Real-time intervals remain source facts, and output
    type 4 is only recorded request context.
    """

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "IORB"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="iorb",
        period="variable",
        market="us",
        source="fred",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> FredIorbQuery:
        """Validate user query values while preserving the typed client error."""
        return FredIorbQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: FredIorbQuery,
        ctx: FetchContext,
    ) -> tuple[dict[str, Any], ...]:
        """Fetch every page only after exact query revalidation."""
        query = _checked_query(params)
        try:
            pages = fetch_iorb_pages(query, timeout=ctx.timeout)
        except (FredProviderError, RequestBudgetError):
            raise
        except Exception:
            raise FredProviderError("FRED_BAD_OBSERVATION") from None
        if type(pages) is not tuple or any(type(page) is not dict for page in pages):
            raise FredProviderError("FRED_BAD_OBSERVATION") from None
        return pages

    def transform_data(
        self,
        raw: tuple[dict[str, Any], ...],
        params: FredIorbQuery,
    ) -> FetchResult:
        """Normalize all validated observations transactionally."""
        query = _checked_query(params)
        try:
            if type(raw) is not tuple or any(type(page) is not dict for page in raw):
                raise ValueError("raw IORB pages must be a tuple of JSON page envelopes")
            rows = validate_iorb_pages(raw, query)
            if type(rows) is not tuple or any(type(row) is not dict for row in rows):
                raise ValueError("IORB validator must return observation row dictionaries")

            observations: list[FredIorbObservation] = []
            for row in rows:
                source_value = row["value"]
                if type(source_value) is not str:
                    raise ValueError("IORB source values must retain their text token")
                value = None if source_value == "." else float(source_value)
                observation = FredIorbObservation.model_validate(
                    {
                        "series_id": query.series_id,
                        "date": row["date"],
                        "value": value,
                        "realtime_start": row["realtime_start"],
                        "realtime_end": row["realtime_end"],
                        "transform_units": query.transform_units,
                        "output_type": query.output_type,
                        "requested_frequency": query.frequency,
                        "requested_aggregation_method": query.aggregation_method,
                        "source_value": source_value,
                        "native_units": "Percent",
                    }
                )
                if query.frequency in (None, "d"):
                    if query.start_date is not None and observation.date < query.start_date:
                        raise ValueError("daily observation precedes the requested start")
                    if query.end_date is not None and observation.date > query.end_date:
                        raise ValueError("daily observation follows the requested end")
                observations.append(observation)
        except Exception:
            raise FredProviderError("FRED_BAD_OBSERVATION") from None
        return tuple(observations)


def _checked_query(params: object) -> FredIorbQuery:
    """Require and reconstruct the exact query at each direct stage boundary."""
    if type(params) is not FredIorbQuery:
        raise FredProviderError("FRED_BAD_QUERY") from None
    try:
        return FredIorbQuery.model_validate(params.model_dump(mode="python"), strict=True)
    except Exception:
        raise FredProviderError("FRED_BAD_QUERY") from None


__all__ = ("FredIorbFetcher",)
