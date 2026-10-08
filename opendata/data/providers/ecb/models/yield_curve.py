"""Controlled, source-faithful ECB yield-curve Fetcher."""

from __future__ import annotations

from typing import ClassVar

from pydantic import ValidationError

from opendata.data.capability import Capability
from opendata.data.models.ecb_series import EcbYieldCurveObservation
from opendata.data.protocol import FetchContext, Fetcher
from opendata.data.providers.ecb.models._client import EcbProviderError
from opendata.data.providers.ecb.models._series_client import fetch_yield_curve
from opendata.data.providers.ecb.models._series_query import EcbYieldCurveQuery
from opendata.data.providers.ecb.models._series_sdmx import (
    RawEcbSeriesRecord,
    normalize_yield_curve_records,
)


def _validated_query(params: object) -> EcbYieldCurveQuery:
    """Revalidate the exact query class to catch construct/copy tampering."""
    if type(params) is not EcbYieldCurveQuery:
        raise EcbProviderError("ECB_BAD_QUERY")
    try:
        values = params.model_dump(mode="python", round_trip=True)
        return EcbYieldCurveQuery.model_validate(values)
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise EcbProviderError("ECB_BAD_QUERY") from None


class EcbYieldCurveFetcher(Fetcher[EcbYieldCurveQuery, tuple[RawEcbSeriesRecord, ...]]):
    """Fetch and normalize one YC business-day series without inferred values."""

    async_mode: ClassVar[str] = "bounded_thread"
    canonical_model: ClassVar[str | None] = "YieldCurve"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="yield_curve",
        period="1D",
        market="eu",
        source="ecb",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EcbYieldCurveQuery:
        """Validate a strict, complete YC key and optional daily window."""
        return EcbYieldCurveQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: EcbYieldCurveQuery,
        ctx: FetchContext,
    ) -> tuple[RawEcbSeriesRecord, ...]:
        """Call the governed YC client with the query and caller timeout."""
        query = _validated_query(params)
        return fetch_yield_curve(query, timeout=ctx.timeout)

    def transform_data(
        self,
        raw: tuple[RawEcbSeriesRecord, ...],
        params: EcbYieldCurveQuery,
    ) -> tuple[EcbYieldCurveObservation, ...]:
        """Normalize atomically and reject any row outside the requested slice."""
        query = _validated_query(params)
        try:
            if len(raw) > query.max_records:
                raise EcbProviderError("ECB_BAD_OBSERVATION")
            records = normalize_yield_curve_records(raw)
        except EcbProviderError:
            raise
        except Exception:
            raise EcbProviderError("ECB_BAD_OBSERVATION") from None

        if len(records) > query.max_records:
            raise EcbProviderError("ECB_BAD_OBSERVATION")
        for record in records:
            if type(record) is not EcbYieldCurveObservation:
                raise EcbProviderError("ECB_BAD_OBSERVATION")
            if record.series_key != query.series_key or record.frequency != "B":
                raise EcbProviderError("ECB_BAD_OBSERVATION")
            if query.start_date is not None and record.date < query.start_date:
                raise EcbProviderError("ECB_BAD_OBSERVATION")
            if query.end_date is not None and record.date > query.end_date:
                raise EcbProviderError("ECB_BAD_OBSERVATION")
        return records


__all__ = ("EcbYieldCurveFetcher",)
