"""Controlled, source-faithful ECB balance-of-payments Fetcher."""

from __future__ import annotations

from typing import ClassVar

from pydantic import ValidationError

from opendata.data.capability import Capability
from opendata.data.models.ecb_series import EcbBalanceOfPaymentsObservation
from opendata.data.protocol import FetchContext, Fetcher
from opendata.data.providers.ecb.models._client import EcbProviderError
from opendata.data.providers.ecb.models._series_client import fetch_balance_of_payments
from opendata.data.providers.ecb.models._series_query import EcbBalanceOfPaymentsQuery
from opendata.data.providers.ecb.models._series_sdmx import (
    RawEcbSeriesRecord,
    normalize_balance_of_payments_records,
)


def _validated_query(params: object) -> EcbBalanceOfPaymentsQuery:
    """Revalidate the exact query class to catch construct/copy tampering."""
    if type(params) is not EcbBalanceOfPaymentsQuery:
        raise EcbProviderError("ECB_BAD_QUERY")
    try:
        values = params.model_dump(mode="python", round_trip=True)
        return EcbBalanceOfPaymentsQuery.model_validate(values)
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise EcbProviderError("ECB_BAD_QUERY") from None


class EcbBalanceOfPaymentsFetcher(
    Fetcher[EcbBalanceOfPaymentsQuery, tuple[RawEcbSeriesRecord, ...]]
):
    """Fetch and normalize one BPS monthly or quarterly series."""

    async_mode: ClassVar[str] = "bounded_thread"
    canonical_model: ClassVar[str | None] = "BalanceOfPayments"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="balance_of_payments",
        period="1M/1Q",
        market="eu",
        source="ecb",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EcbBalanceOfPaymentsQuery:
        """Validate a strict, complete BPS key and matching source periods."""
        return EcbBalanceOfPaymentsQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: EcbBalanceOfPaymentsQuery,
        ctx: FetchContext,
    ) -> tuple[RawEcbSeriesRecord, ...]:
        """Call the governed BPS client with the query and caller timeout."""
        query = _validated_query(params)
        return fetch_balance_of_payments(query, timeout=ctx.timeout)

    def transform_data(
        self,
        raw: tuple[RawEcbSeriesRecord, ...],
        params: EcbBalanceOfPaymentsQuery,
    ) -> tuple[EcbBalanceOfPaymentsObservation, ...]:
        """Normalize atomically and reject rows outside the requested period range."""
        query = _validated_query(params)
        try:
            if len(raw) > query.max_records:
                raise EcbProviderError("ECB_BAD_OBSERVATION")
            records = normalize_balance_of_payments_records(raw)
        except EcbProviderError:
            raise
        except Exception:
            raise EcbProviderError("ECB_BAD_OBSERVATION") from None

        if len(records) > query.max_records:
            raise EcbProviderError("ECB_BAD_OBSERVATION")
        frequency = query.series_key.split(".", maxsplit=2)[1]
        for record in records:
            if type(record) is not EcbBalanceOfPaymentsObservation:
                raise EcbProviderError("ECB_BAD_OBSERVATION")
            if record.series_key != query.series_key or record.frequency != frequency:
                raise EcbProviderError("ECB_BAD_OBSERVATION")
            if query.start_period is not None and record.period < query.start_period:
                raise EcbProviderError("ECB_BAD_OBSERVATION")
            if query.end_period is not None and record.period > query.end_period:
                raise EcbProviderError("ECB_BAD_OBSERVATION")
        return records


__all__ = ("EcbBalanceOfPaymentsFetcher",)
