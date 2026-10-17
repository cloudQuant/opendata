"""U.S. Treasury auction records from Treasury FiscalData."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, ClassVar, Literal

from pydantic import Field, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.government_us import TreasuryAuction
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.government_us.models import _client

TREASURY_AUCTIONS_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
    "v1/accounting/od/auctions_query"
)
TREASURY_HOSTS = frozenset({"api.fiscaldata.treasury.gov"})
MAX_PAGE_SIZE = 1_000
MAX_DATE_SPAN_DAYS = 3_660


class TreasuryAuctionsQuery(QueryParams):
    """Bounded auction filter and one page of official FiscalData rows."""

    source: Literal["government_us"] = "government_us"
    market: Literal["us"] = "us"
    security_type: Literal["bill", "note", "bond", "cmb", "tips", "frn"] | None = None
    cusip: str | None = None
    page_size: int | None = Field(default=None, ge=1, le=MAX_PAGE_SIZE)
    page_num: int | None = Field(default=None, ge=1, le=10_000)

    @field_validator("cusip")
    @classmethod
    def _validate_cusip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if len(value) != 9 or not value.isalnum():
            raise ValueError("CUSIP must be exactly nine alphanumeric characters")
        return value

    @model_validator(mode="after")
    def _validate_window(self) -> TreasuryAuctionsQuery:
        if (self.page_size is None) != (self.page_num is None):
            raise ValueError("page_size and page_num must be supplied together")
        if self.start_date is not None and self.end_date is not None:
            if self.end_date < self.start_date:
                raise ValueError("end_date must not precede start_date")
            if (self.end_date - self.start_date).days > MAX_DATE_SPAN_DAYS:
                raise ValueError("Treasury auction window is limited to ten years")
        if self.symbol is not None:
            raise ValueError("Treasury auction queries use cusip, not symbol")
        return self


class TreasuryAuctionsFetcher(Fetcher[TreasuryAuctionsQuery, list[dict[str, Any]]]):
    """Fetch one declared FiscalData page and reject silent truncation."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "TreasuryAuctions"
    row_model: ClassVar[type[TreasuryAuction]] = TreasuryAuction
    capability: ClassVar[Capability] = Capability(
        asset_class="fixed_income",
        domain="government_us_treasury_auctions",
        period="event",
        market="us",
        source="government_us",
        verified=False,
        notes="FiscalData auction schema mapped; provider verification pending.",
    )

    def transform_query(self, **kwargs: object) -> TreasuryAuctionsQuery:
        """Validate and bound the auction request before transport."""
        return TreasuryAuctionsQuery.model_validate(kwargs)

    def extract_data(
        self, params: TreasuryAuctionsQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Request exactly one bounded page from the official auction table."""
        today = date.today()
        start = params.start_date or (today - timedelta(days=90))
        end = params.end_date or today
        if end < start or (end - start).days > MAX_DATE_SPAN_DAYS:
            raise _client.GovernmentUSProviderError("TREASURY_AUCTION_WINDOW_INVALID")
        page_size = params.page_size or MAX_PAGE_SIZE
        page_num = params.page_num or 1
        filters = [f"auction_date:gte:{start.isoformat()}", f"auction_date:lte:{end.isoformat()}"]
        if params.cusip:
            filters.append(f"cusip:eq:{params.cusip}")
        if params.security_type:
            type_map = {
                "bill": "Bill",
                "note": "Note",
                "bond": "Bond",
                "cmb": "CMB",
                "tips": "TIPS",
                "frn": "FRN",
            }
            filters.append(f"security_type:eq:{type_map[params.security_type]}")
        document, _ = _client.get_json(
            TREASURY_AUCTIONS_URL,
            params={
                "format": "json",
                "filter": ",".join(filters),
                "sort": "-auction_date",
                "page[size]": str(page_size),
                "page[number]": str(page_num),
            },
            headers=None,
            context=ctx,
            allowed_hosts=TREASURY_HOSTS,
        )
        rows = _client.array_rows(document)
        if len(rows) > page_size:
            raise _client.GovernmentUSProviderError("TREASURY_AUCTION_BAD_PAGE")
        if isinstance(document, dict):
            meta = document.get("meta")
            total = meta.get("total-count") if isinstance(meta, dict) else None
            try:
                total_count = int(total) if total is not None else None
            except (TypeError, ValueError):
                raise _client.GovernmentUSProviderError("TREASURY_AUCTION_BAD_META") from None
            if params.page_size is None and total_count is not None and total_count > page_size:
                raise _client.GovernmentUSProviderError("TREASURY_AUCTION_PAGE_REQUIRED")
        return rows

    def transform_data(
        self, raw: list[dict[str, Any]], params: TreasuryAuctionsQuery
    ) -> FetchResult:
        """Project the documented Treasury output schema and validate types."""
        fields = TreasuryAuction.model_fields
        result: list[TreasuryAuction] = []
        for row in raw:
            source_cusip = row.get("cusip")
            if (
                params.cusip is not None
                and isinstance(source_cusip, str)
                and source_cusip.strip().upper() != params.cusip
            ):
                raise _client.GovernmentUSProviderError("TREASURY_AUCTION_CUSIP_MISMATCH")
            normalized = {name: row.get(name) for name in fields if name in row}
            for name, value in tuple(normalized.items()):
                if value == "" or value == "null":
                    normalized[name] = None
            try:
                result.append(TreasuryAuction.model_validate(normalized))
            except (TypeError, ValueError):
                raise _client.GovernmentUSProviderError("TREASURY_AUCTION_BAD_ROW") from None
        return tuple(result)
