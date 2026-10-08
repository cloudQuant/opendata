"""Source-faithful Bank of England SONIA observations through FRED."""

from __future__ import annotations

from typing import Any, ClassVar

from opendata.data.capability import Capability
from opendata.data.models.fred_sonia import (
    _SONIA_PARAMETER_MAP,
    FredSoniaObservation,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models._sonia_client import (
    fetch_sonia_pages,
    validate_sonia_pages,
)
from opendata.data.providers.fred.models._sonia_query import FredSoniaQuery
from opendata.data.request_budget import RequestBudgetError


class FredSoniaFetcher(Fetcher[FredSoniaQuery, tuple[dict[str, Any], ...]]):
    """Fetch complete SONIA page envelopes and normalize validated source rows.

    The pure SONIA client owns transport, page/header validation, pagination,
    revision binding, and output-shape checks. This Fetcher preserves the
    returned page tuple for raw capture and builds the normalized result
    transactionally, retaining source tokens and real-time intervals. Daily
    results are checked against the requested observation window. For
    low-frequency aggregation, source period labels are preserved without
    inventing a label-window rule. Output type 4 remains request context; this
    class does not infer point-in-time or first-publication rows.
    """

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "SONIA"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="sonia",
        period="variable",
        market="gb",
        source="fred",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> FredSoniaQuery:
        """Validate the typed query and preserve Pydantic's client-error path."""
        return FredSoniaQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: FredSoniaQuery,
        ctx: FetchContext,
    ) -> tuple[dict[str, Any], ...]:
        """Fetch every bounded page while retaining full original envelopes."""
        query = _checked_query(params)
        try:
            pages = fetch_sonia_pages(query, timeout=ctx.timeout)
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
        params: FredSoniaQuery,
    ) -> FetchResult:
        """Validate the whole page result before returning any observations."""
        query = _checked_query(params)
        try:
            if type(raw) is not tuple or any(type(page) is not dict for page in raw):
                raise ValueError("raw SONIA pages must be a tuple of JSON envelopes")

            rows = validate_sonia_pages(raw, query)
            if type(rows) is not tuple or any(type(row) is not dict for row in rows):
                raise ValueError("SONIA validator must return observation row dictionaries")

            series_id, native_units = _SONIA_PARAMETER_MAP[query.parameter]
            if query.series_id != series_id:
                raise ValueError("SONIA parameter and series identifier do not match")

            observations: list[FredSoniaObservation] = []
            for row in rows:
                source_value = row["value"]
                if type(source_value) is not str:
                    raise ValueError("SONIA source values must retain their text token")
                value = None if source_value == "." else float(source_value)
                observation = FredSoniaObservation.model_validate(
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
                        "parameter": query.parameter,
                        "source_value": source_value,
                        "native_units": native_units,
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


def _checked_query(params: object) -> FredSoniaQuery:
    """Revalidate exact query instances at direct Fetcher stage boundaries."""
    if type(params) is not FredSoniaQuery:
        raise FredProviderError("FRED_BAD_QUERY") from None
    try:
        return FredSoniaQuery.model_validate(params.model_dump(mode="python"), strict=True)
    except Exception:
        raise FredProviderError("FRED_BAD_QUERY") from None
