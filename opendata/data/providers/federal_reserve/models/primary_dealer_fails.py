"""New York Fed primary-dealer fails-to-deliver history."""

from __future__ import annotations

from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.models.federal_reserve_macro import FederalReservePrimaryDealerFail
from opendata.data.protocol import FetchContext, Fetcher
from opendata.data.providers.federal_reserve._iter2_client import FederalReserveSourceError
from opendata.data.providers.federal_reserve.models._primary_dealer import (
    PrimaryDealerQuery,
    RawPrimaryDealerRecord,
    fetch_primary_dealer_series,
    normalize_primary_dealer_records,
)


class FederalReservePrimaryDealerFailsFetcher(
    Fetcher[PrimaryDealerQuery, tuple[RawPrimaryDealerRecord, ...]]
):
    """Fetch selected source-defined fails series and retain their published USD-million units."""

    async_mode: ClassVar[str] = "bounded_thread"
    canonical_model: ClassVar[str | None] = "PrimaryDealerFails"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="federal_reserve_primary_dealer_fails",
        period="w",
        market="us",
        source="federal_reserve",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> PrimaryDealerQuery:
        """Validate series identifiers and inclusive date bounds."""
        return PrimaryDealerQuery.model_validate(kwargs)

    def extract_data(
        self, params: PrimaryDealerQuery, ctx: FetchContext
    ) -> tuple[RawPrimaryDealerRecord, ...]:
        """Resolve requested ids from the official NY Fed series catalogue before fetching."""
        if type(params) is not PrimaryDealerQuery:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_QUERY")
        return fetch_primary_dealer_series(params.key_ids, category="fails", timeout=ctx.timeout)

    def transform_data(
        self,
        raw: tuple[RawPrimaryDealerRecord, ...],
        params: PrimaryDealerQuery,
    ) -> tuple[FederalReservePrimaryDealerFail, ...]:
        """Parse exact numeric strings and produce the reviewed typed fails rows."""
        rows = normalize_primary_dealer_records(raw, params, category="fails")
        return tuple(FederalReservePrimaryDealerFail.model_validate(row) for row in rows)


__all__ = ["FederalReservePrimaryDealerFailsFetcher"]
