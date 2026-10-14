"""USDA FAS circular publication downloads."""

from __future__ import annotations

import base64
from datetime import date
from typing import ClassVar, Literal

from pydantic import Field, model_validator

from opendata.data.capability import Capability
from opendata.data.models.government_us import CommodityPsdReport
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.government_us.models import _client

USDA_HOSTS = frozenset({"apps.fas.usda.gov"})
# Only exact archived sample paths confirmed against official USDA PDFs.
# Keep the public selector vocabulary below; unsupported categories fail closed.
REPORT_FILES = {
    "grain": "Grain.pdf",
    "world_production": "production.pdf",
}
CommodityName = Literal[
    "citrus",
    "coffee",
    "cotton",
    "dairy",
    "fruit",
    "grain",
    "livestock",
    "oilseeds",
    "stone_fruit",
    "sugar",
    "tree_nuts",
    "world_production",
]


class CommodityPsdReportQuery(QueryParams):
    """One archived USDA circular identified by commodity, year, and month."""

    source: Literal["government_us"] = "government_us"
    market: Literal["us", "global"] = "global"
    commodity: CommodityName
    year: int = Field(ge=2000, le=2200)
    month: int = Field(ge=1, le=12)

    @model_validator(mode="after")
    def _validate_date(self) -> CommodityPsdReportQuery:
        today = date.today()
        if self.year > today.year or (self.year == today.year and self.month > today.month):
            raise ValueError("USDA circular query cannot be in the future")
        if self.start_date is not None or self.end_date is not None or self.symbol is not None:
            raise ValueError("USDA circular query uses commodity, year, and month only")
        return self


class CommodityPsdReportFetcher(
    Fetcher[CommodityPsdReportQuery, tuple[bytes, str, str]],
):
    """Download the selected official FAS circular and retain its PDF bytes."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CommodityPsdReport"
    capability: ClassVar[Capability] = Capability(
        asset_class="commodity",
        domain="government_us_commodity_psd_report",
        period="monthly",
        market="global",
        source="government_us",
        verified=False,
        notes=(
            "Official May 2022 archive samples confirm grain and world_production; "
            "other public category selectors remain unsupported pending source evidence."
        ),
    )

    def transform_query(self, **kwargs: object) -> CommodityPsdReportQuery:
        """Validate the bounded report identifier before HTTP access."""
        return CommodityPsdReportQuery.model_validate(kwargs)

    def extract_data(
        self, params: CommodityPsdReportQuery, ctx: FetchContext
    ) -> tuple[bytes, str, str]:
        """Read one exact year/month PDF from USDA's circular archive."""
        filename = REPORT_FILES.get(params.commodity)
        if filename is None:
            raise _client.GovernmentUSProviderError("USDA_REPORT_CATEGORY_UNVERIFIED")
        url = (
            f"https://apps.fas.usda.gov/PSDOnline/Circulars/"
            f"{params.year}/{params.month:02d}/{filename}"
        )
        body, content_type, _ = _client.get_bytes(
            url,
            params=None,
            headers={"Accept": "application/pdf"},
            context=ctx,
            source="government_us",
            allowed_hosts=USDA_HOSTS,
            maximum_bytes=_client.DOCUMENT_RESPONSE_LIMIT,
        )
        if not body.startswith(b"%PDF-"):
            raise _client.GovernmentUSProviderError("USDA_REPORT_NOT_PDF")
        if content_type not in {"application/pdf", "application/octet-stream", ""}:
            raise _client.GovernmentUSProviderError("USDA_REPORT_BAD_CONTENT_TYPE")
        return body, content_type or "application/pdf", url

    def transform_data(
        self,
        raw: tuple[bytes, str, str],
        params: CommodityPsdReportQuery,
    ) -> FetchResult:
        """Base64-encode the original PDF bytes without parsing them as data."""
        body, content_type, url = raw
        return (
            CommodityPsdReport(
                content=base64.b64encode(body).decode("ascii"),
                data_format={
                    "media_type": content_type,
                    "encoding": "base64",
                    "source_url": url,
                },
            ),
        )
