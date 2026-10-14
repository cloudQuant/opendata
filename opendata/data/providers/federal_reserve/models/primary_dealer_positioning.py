"""New York Fed primary-dealer positioning history."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from opendata.data.capability import Capability
from opendata.data.models.federal_reserve_clean_nyfed import PrimaryDealerPositioning
from opendata.data.protocol import FetchContext, Fetcher
from opendata.data.providers.federal_reserve._iter2_client import FederalReserveSourceError
from opendata.data.providers.federal_reserve.models._primary_dealer import (
    PrimaryDealerQuery,
    RawPrimaryDealerRecord,
    fetch_primary_dealer_series,
    normalize_primary_dealer_records,
)

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel
    from opendata.data.providers._engine.http_json import HttpGet


class FederalReservePrimaryDealerPositioningFetcher(
    Fetcher[PrimaryDealerQuery, tuple[RawPrimaryDealerRecord, ...]]
):
    """Fetch selected source-defined position series in published USD-million units."""

    async_mode: ClassVar[str] = "bounded_thread"
    canonical_model: ClassVar[str | None] = "PrimaryDealerPositioning"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="federal_reserve_primary_dealer_positioning",
        period="w",
        market="us",
        source="federal_reserve",
        verified=False,
    )
    #: The contract ``domains.yaml`` declares for this domain — the governed query service
    #: serializes rows against it, so :meth:`transform_data` has to build exactly this class.
    row_model: ClassVar[type[ContractModel]] = PrimaryDealerPositioning
    #: The offline witness seam; ``None`` keeps the governed shared client on the live path.
    http_transport: ClassVar[HttpGet | None] = None

    def transform_query(self, **kwargs: object) -> PrimaryDealerQuery:
        """Validate series identifiers and inclusive date bounds."""
        return PrimaryDealerQuery.model_validate(kwargs)

    def extract_data(
        self, params: PrimaryDealerQuery, ctx: FetchContext
    ) -> tuple[RawPrimaryDealerRecord, ...]:
        """Resolve requested ids from the official NY Fed series catalogue before fetching."""
        if type(params) is not PrimaryDealerQuery:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_QUERY")
        return fetch_primary_dealer_series(
            params.key_ids,
            category="position",
            timeout=ctx.timeout,
            transport=self.http_transport,
        )

    def transform_data(
        self,
        raw: tuple[RawPrimaryDealerRecord, ...],
        params: PrimaryDealerQuery,
    ) -> tuple[PrimaryDealerPositioning, ...]:
        """Parse exact numeric strings and produce the domain contract's typed rows."""
        rows = normalize_primary_dealer_records(raw, params, category="position")
        return tuple(PrimaryDealerPositioning.model_validate(row) for row in rows)


__all__ = ["FederalReservePrimaryDealerPositioningFetcher"]
