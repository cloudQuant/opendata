"""NOAA/NWS text bulletin metadata and body."""

from __future__ import annotations

import re
from typing import Any, ClassVar, Literal

from pydantic import Field, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.government_us import WeatherBulletin
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.government_us.models import _client

NWS_API = "https://api.weather.gov"
NWS_HOSTS = frozenset({"api.weather.gov"})


class WeatherBulletinQuery(QueryParams):
    """One latest NWS product for a known product type and WFO location."""

    source: Literal["government_us"] = "government_us"
    market: Literal["us"] = "us"
    product_type: str = Field(default="TUV", min_length=3, max_length=6)
    location: str

    @field_validator("product_type", "location")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{3,6}", cleaned):
            raise ValueError("NWS product and location codes must be 3-6 alphanumeric characters")
        return cleaned

    @model_validator(mode="after")
    def _validate_request(self) -> WeatherBulletinQuery:
        if self.start_date is not None or self.end_date is not None or self.symbol is not None:
            raise ValueError("NWS bulletin query supports latest product by type and location")
        return self


class WeatherBulletinFetcher(Fetcher[WeatherBulletinQuery, dict[str, Any]]):
    """Read the latest official NWS text product for the selected location."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "WeatherBulletin"
    capability: ClassVar[Capability] = Capability(
        asset_class="weather",
        domain="government_us_weather_bulletin",
        period="snapshot",
        market="us",
        source="government_us",
        verified=False,
        notes="NOAA source-defined output; OpenBB model equivalence is NOT_RUN.",
    )

    def transform_query(self, **kwargs: object) -> WeatherBulletinQuery:
        """Validate short identifiers before provider I/O."""
        return WeatherBulletinQuery.model_validate(kwargs)

    def extract_data(self, params: WeatherBulletinQuery, ctx: FetchContext) -> dict[str, Any]:
        """Fetch one NWS Product JSON-LD object."""
        url = f"{NWS_API}/products/types/{params.product_type}/locations/{params.location}/latest"
        document, _ = _client.get_json(
            url,
            params=None,
            headers={
                "Accept": "application/ld+json, application/json",
                "User-Agent": "opendata research contact=none",
            },
            context=ctx,
            allowed_hosts=NWS_HOSTS,
        )
        if not isinstance(document, dict):
            raise _client.GovernmentUSProviderError("NWS_BAD_PRODUCT_SHAPE")
        return document

    def transform_data(self, raw: dict[str, Any], params: WeatherBulletinQuery) -> FetchResult:
        """Map NOAA JSON-LD fields into a named NOAA-only typed contract."""
        mapped = {
            "id": raw.get("id") or raw.get("@id"),
            "wmo_collective_id": raw.get("wmoCollectiveId"),
            "issuing_office": raw.get("issuingOffice"),
            "issuance_time": raw.get("issuanceTime"),
            "product_code": raw.get("productCode"),
            "product_name": raw.get("productName"),
            "product_text": raw.get("productText"),
        }
        try:
            return (WeatherBulletin.model_validate(mapped),)
        except (TypeError, ValueError):
            raise _client.GovernmentUSProviderError("NWS_BAD_PRODUCT_ROW") from None
