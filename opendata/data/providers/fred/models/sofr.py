"""Source-faithful FRED SOFR observations with bounded synchronous extraction."""

from __future__ import annotations

from typing import Any, ClassVar

from opendata.data.capability import Capability
from opendata.data.models.fred_sofr import FredSofrObservation
from opendata.data.protocol import FetchContext, Fetcher, FetchResult
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models._sofr_client import (
    fetch_sofr_pages,
    validate_sofr_pages,
)
from opendata.data.providers.fred.models._sofr_query import FredSofrQuery
from opendata.data.request_budget import RequestBudgetError

_NATIVE_UNITS: dict[str, str] = {
    "SOFR": "Percent",
    "SOFR30DAYAVG": "Percent",
    "SOFR90DAYAVG": "Percent",
    "SOFR180DAYAVG": "Percent",
    "SOFRINDEX": "Index Apr 2, 2018 = 1",
}


class FredSofrFetcher(Fetcher[FredSofrQuery, tuple[dict[str, Any], ...]]):
    """Fetch original SOFR JSON pages and normalize validated source rows.

    Page/header, paging, sort, result-count, real-time selector binding, and
    source row real-time interval checks are owned by the pure SOFR client
    validator. This class retains the complete raw page tuple for captures
    and never collapses source revisions. For a
    requested low-frequency aggregation it preserves source period labels;
    it does not claim a verified rule for filtering those labels against a
    mid-period observation window. Output type 4 records the request context
    and does not infer first-publication selection from response rows.
    """

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "SOFR"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="sofr",
        period="variable",
        market="us",
        source="fred",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> FredSofrQuery:
        """Validate request parameters for the API's typed 400-error mapping."""
        return FredSofrQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: FredSofrQuery,
        ctx: FetchContext,
    ) -> tuple[dict[str, Any], ...]:
        """Fetch all bounded JSON pages while preserving each full envelope."""
        query = _checked_query(params)
        try:
            pages = fetch_sofr_pages(query, timeout=ctx.timeout)
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
        params: FredSofrQuery,
    ) -> FetchResult:
        """Validate whole-page results, then transactionally build contracts."""
        query = _checked_query(params)
        try:
            if type(raw) is not tuple or any(type(page) is not dict for page in raw):
                raise ValueError("raw SOFR pages must be a tuple of JSON envelopes")
            rows = validate_sofr_pages(raw, query)
            if type(rows) is not tuple or any(type(row) is not dict for row in rows):
                raise ValueError("SOFR validator must return a tuple of observation rows")

            observations: list[FredSofrObservation] = []
            for row in rows:
                source_value = row["value"]
                if type(source_value) is not str:
                    raise ValueError("SOFR source value must retain its source token")
                value = None if source_value == "." else float(source_value)
                observation = FredSofrObservation.model_validate(
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
                        "native_units": _NATIVE_UNITS[query.series_id],
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


def _checked_query(params: object) -> FredSofrQuery:
    """Require an exact, currently valid SOFR query at direct stage calls."""
    if type(params) is not FredSofrQuery:
        raise FredProviderError("FRED_BAD_QUERY") from None
    try:
        return FredSofrQuery.model_validate(params.model_dump())
    except Exception:
        raise FredProviderError("FRED_BAD_QUERY") from None
