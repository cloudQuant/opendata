"""USDA FAS PSD historical data provider."""

from __future__ import annotations

import os
import re
from datetime import date
from typing import Any, ClassVar, Literal

from pydantic import Field, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.government_us import CommodityPsdData
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.government_us.models import _client

USDA_API = "https://api.fas.usda.gov"
USDA_HOSTS = frozenset({"api.fas.usda.gov"})
MAX_MARKET_YEARS = 5
MAX_ROWS_PER_QUERY = 50_000
_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{1,200}$")


def _normal(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


class CommodityPsdDataQuery(QueryParams):
    """Explicit, bounded USDA commodity time-series query."""

    source: Literal["government_us"] = "government_us"
    market: Literal["us", "global"] = "global"
    report_id: str | None = "world_crop_production_summary"
    commodity: str | None = None
    attribute: str | list[str] | None = None
    country: str | list[str] | None = None
    aggregate_regions: bool = False
    start_year: int | None = Field(default=None, ge=1960, le=2200)
    end_year: int | None = Field(default=None, ge=1960, le=2200)

    @field_validator("report_id", "commodity")
    @classmethod
    def _clean_slug(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().casefold()
        if not cleaned or len(cleaned) > 100 or not re.fullmatch(r"[a-z0-9_ -]+", cleaned):
            raise ValueError("USDA query name must be a bounded plain identifier")
        return cleaned

    @field_validator("attribute", "country")
    @classmethod
    def _bound_filters(cls, value: str | list[str] | None) -> str | list[str] | None:
        items = value.split(",") if isinstance(value, str) else value
        if items is None:
            return None
        if not isinstance(items, list) or not items or len(items) > 20:
            raise ValueError("USDA filters accept one to twenty values")
        cleaned: list[str] = []
        for item in items:
            if not isinstance(item, str):
                raise ValueError("USDA filters must contain strings")
            item = item.strip().casefold()
            if not item or len(item) > 80 or not re.fullmatch(r"[a-z0-9_ -]+", item):
                raise ValueError("USDA filter value is invalid")
            cleaned.append(item)
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("USDA filters must not repeat values")
        return cleaned

    @model_validator(mode="after")
    def _bounded_history(self) -> CommodityPsdDataQuery:
        if self.commodity is None:
            if self.start_year is not None or self.end_year is not None:
                raise ValueError("USDA time bounds require commodity")
            return self
        if self.start_year is None:
            raise ValueError("USDA PSD time-series queries require start_year")
        end_year = self.end_year or date.today().year
        if end_year < self.start_year or end_year - self.start_year + 1 > MAX_MARKET_YEARS:
            raise ValueError("USDA PSD query window must span one to five market years")
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("USDA PSD uses start_year/end_year, not date fields")
        return self


class CommodityPsdDataFetcher(Fetcher[CommodityPsdDataQuery, list[dict[str, Any]]]):
    """Fetch and map FAS PSD rows from official USDA API outputs."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CommodityPsdData"
    row_model: ClassVar[type[CommodityPsdData]] = CommodityPsdData
    capability: ClassVar[Capability] = Capability(
        asset_class="commodity",
        domain="government_us_commodity_psd_data",
        period="annual",
        market="global",
        source="government_us",
        verified=False,
        notes="Source contract implemented; provider verification pending.",
    )

    def transform_query(self, **kwargs: object) -> CommodityPsdDataQuery:
        """Validate input and reject broad history before credential access."""
        return CommodityPsdDataQuery.model_validate(kwargs)

    def extract_data(
        self, params: CommodityPsdDataQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Resolve official FAS lookup codes and retrieve at most five years."""
        if params.commodity is None:
            raise _client.GovernmentUSProviderError("USDA_REPORT_TABLE_SOURCE_UNRESOLVED")
        api_key = os.environ.get("USDA_FAS_API_KEY", "").strip()
        if not _TOKEN.fullmatch(api_key):
            raise _client.GovernmentUSProviderError("USDA_FAS_API_KEY_MISSING_OR_INVALID")
        headers = {"X-Api-Key": api_key}
        commodities = self._lookup("commodities", headers, ctx)
        countries = self._lookup("countries", headers, ctx)
        regions = self._lookup("regions", headers, ctx)
        attributes = self._lookup("commodityAttributes", headers, ctx)
        commodity_code = self._resolve_code(
            commodities,
            name_key=("CommodityName", "CommodityDescription"),
            code_key="CommodityCode",
            requested=params.commodity,
            error="USDA_UNKNOWN_COMMODITY",
        )
        country_codes = self._resolve_countries(countries, params.country)
        if params.market == "us":
            us_codes = self._resolve_countries(countries, "United States")
            if us_codes is None:
                raise _client.GovernmentUSProviderError("USDA_UNITED_STATES_LOOKUP_MISSING")
            if country_codes is not None and not country_codes.issubset(us_codes):
                raise _client.GovernmentUSProviderError("USDA_MARKET_COUNTRY_CONFLICT")
            country_codes = us_codes if country_codes is None else country_codes
        attribute_ids = self._resolve_attributes(attributes, params.attribute)
        region_names = {
            str(row.get("RegionCode")): str(row.get("RegionName"))
            for row in regions
            if row.get("RegionCode") is not None and row.get("RegionName") is not None
        }
        region_codes = set(region_names)
        country_by_code = {
            str(row.get("CountryCode")): row
            for row in countries
            if row.get("CountryCode") is not None
        }
        end_year = params.end_year or date.today().year
        all_rows: list[dict[str, Any]] = []
        for year in range(params.start_year or end_year, end_year + 1):
            url = f"{USDA_API}/api/psd/commodity/{commodity_code}/country/all/year/{year}"
            document, _ = _client.get_json(
                url,
                params=None,
                headers=headers,
                context=ctx,
                allowed_hosts=USDA_HOSTS,
            )
            rows = _client.array_rows(document)
            if len(rows) > MAX_ROWS_PER_QUERY or len(all_rows) + len(rows) > MAX_ROWS_PER_QUERY:
                raise _client.GovernmentUSProviderError("USDA_RESULT_LIMIT_EXCEEDED")
            for row in rows:
                normalized = self._map_source_row(row, region_names, country_by_code)
                row_country = str(row.get("CountryCode", ""))
                row_attribute = row.get("AttributeId")
                if country_codes is not None and row_country not in country_codes:
                    continue
                if attribute_ids is not None and str(row_attribute) not in attribute_ids:
                    continue
                if not params.aggregate_regions and row_country in region_codes:
                    continue
                all_rows.append(normalized)
        return all_rows

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: CommodityPsdDataQuery,
    ) -> FetchResult:
        """Validate source rows against the named PSD output fields."""
        result: list[CommodityPsdData] = []
        for row in raw:
            try:
                result.append(CommodityPsdData.model_validate(row))
            except (TypeError, ValueError):  # noqa: PERF203 - translate the first malformed row immediately.
                raise _client.GovernmentUSProviderError("USDA_BAD_COMMODITY_ROW") from None
        return tuple(result)

    def _lookup(
        self, name: str, headers: dict[str, str], ctx: FetchContext
    ) -> list[dict[str, Any]]:
        document, _ = _client.get_json(
            f"{USDA_API}/api/psd/{name}",
            params=None,
            headers=headers,
            context=ctx,
            allowed_hosts=USDA_HOSTS,
        )
        return _client.array_rows(document)

    @staticmethod
    def _resolve_code(
        rows: list[dict[str, Any]],
        *,
        name_key: tuple[str, ...],
        code_key: str,
        requested: str,
        error: str,
    ) -> str:
        candidates = [
            row
            for row in rows
            if any(_normal(row.get(key, "")) == _normal(requested) for key in name_key)
        ]
        if len(candidates) != 1 or candidates[0].get(code_key) is None:
            raise _client.GovernmentUSProviderError(error)
        return str(candidates[0][code_key])

    @staticmethod
    def _resolve_countries(
        rows: list[dict[str, Any]], requested: str | list[str] | None
    ) -> set[str] | None:
        if requested is None:
            return None
        names = requested if isinstance(requested, list) else [requested]
        selected: set[str] = set()
        for requested_name in names:
            matches = [
                row
                for row in rows
                if _normal(row.get("CountryName", "")) == _normal(requested_name)
            ]
            if len(matches) != 1 or matches[0].get("CountryCode") is None:
                raise _client.GovernmentUSProviderError("USDA_UNKNOWN_COUNTRY")
            selected.add(str(matches[0]["CountryCode"]))
        return selected

    @staticmethod
    def _resolve_attributes(
        rows: list[dict[str, Any]], requested: str | list[str] | None
    ) -> set[str] | None:
        if requested is None:
            return None
        names = requested if isinstance(requested, list) else [requested]
        selected: set[str] = set()
        for requested_name in names:
            matches = [
                row
                for row in rows
                if _normal(row.get("AttributeName", "")) == _normal(requested_name)
            ]
            if len(matches) != 1 or matches[0].get("AttributeId") is None:
                raise _client.GovernmentUSProviderError("USDA_UNKNOWN_ATTRIBUTE")
            selected.add(str(matches[0]["AttributeId"]))
        return selected

    @staticmethod
    def _map_source_row(
        row: dict[str, Any],
        regions: dict[str, str],
        countries: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        country_code = str(row.get("CountryCode", ""))
        country = countries.get(country_code)
        country_name = row.get("CountryName") or (country or {}).get("CountryName")
        region = regions.get(country_code)
        if region is None and country is not None:
            region = regions.get(str(country.get("RegionCode", "")))
        return {
            "region": region,
            "country": None if country_code in regions else country_name,
            "commodity": row.get("CommodityDescription"),
            "attribute": row.get("AttributeDescription"),
            "marketing_year": str(row.get("MarketYear"))
            if row.get("MarketYear") is not None
            else None,
            "value": row.get("Value"),
            "unit": row.get("UnitDescription"),
        }
