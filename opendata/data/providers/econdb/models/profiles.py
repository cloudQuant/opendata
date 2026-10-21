"""Public EconDB export-destination widget adapter (no credential)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import urlencode

from opendata.data.capability import Capability
from opendata.data.models.econdb import EconDBCountryProfile, EconDBExportDestinations
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import ProviderEngineError, _http_get_json
from opendata.data.providers.econdb._source import SOURCE

if TYPE_CHECKING:
    from opendata.data.providers._engine.http_json import HttpGet

BASE_URL = "https://www.econdb.com/widgets/top-trade-items/data/"
CONTEXT_URL = "https://www.econdb.com/series/context/"
TIMEOUT_SECONDS = 20.0
_ORIGIN_NAMES = {
    "US": "United States",
    "CN": "China",
    "DE": "Germany",
    "JP": "Japan",
    "GB": "United Kingdom",
    "FR": "France",
    "IN": "India",
}


class EcondbExportDestinationsQuery(QueryParams):
    """The exporting country whose destination table is requested."""

    source: Literal["econdb"] = "econdb"
    market: Literal["global"] = "global"
    country: str


class EconDBExportDestinationsFetcher(
    Fetcher[EcondbExportDestinationsQuery, tuple[EconDBExportDestinations, ...]]
):
    """Fetch the public widget table and validate every destination row."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "ExportDestinations"
    row_model: ClassVar[type[EconDBExportDestinations]] = EconDBExportDestinations
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="econdb_export_destinations",
        period="variable",
        market="global",
        source="econdb",
        verified=False,
        notes="Public widget table; no credential, offline acceptance uses a fixture.",
    )

    def transform_query(self, **kwargs: object) -> EcondbExportDestinationsQuery:
        """Require a non-blank country."""
        query = EcondbExportDestinationsQuery.model_validate(kwargs)
        if not query.country.strip():
            raise ProviderEngineError("ECONDB_QUERY_INVALID", invalid=("country",))
        return query

    def _get(self, url: str, ctx: FetchContext) -> dict[str, Any]:
        transport = self.http_transport or _http_get_json
        response = transport(
            url,
            {},
            timeout=min(float(ctx.timeout or TIMEOUT_SECONDS), TIMEOUT_SECONDS),
            source=SOURCE,
            headers={"Accept": "application/json"},
        )
        if not 200 <= response.status < 300:
            raise ProviderEngineError("ECONDB_HTTP_ERROR", status=response.status, url=url)
        return response.document

    def extract_data(
        self,
        params: EcondbExportDestinationsQuery,
        ctx: FetchContext,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Request the widget table and require its plot data."""
        code = params.country.strip().upper()
        document = self._get(f"{BASE_URL}?country={code}&split_by=country", ctx)
        plots = document.get("plots") if isinstance(document, dict) else None
        if not plots or not isinstance(plots, list):
            raise ProviderEngineError("ECONDB_SHAPE_INVALID")
        meta = plots[0] if isinstance(plots[0], dict) else {}
        data = meta.get("data")
        if not isinstance(data, list) or not data:
            raise ProviderEngineError("ECONDB_SHAPE_INVALID")
        return data, meta

    def transform_data(
        self,
        raw: tuple[list[dict[str, Any]], dict[str, Any]],
        params: EcondbExportDestinationsQuery,
    ) -> FetchResult:
        """Map each widget row onto the frozen export-destination contract."""
        data, meta = raw
        series = meta.get("series") or []
        units = ""
        if series and isinstance(series[0], dict):
            units = str(series[0].get("code") or "").replace("Value million", "Millions of")
        origin = _ORIGIN_NAMES.get(params.country.strip().upper(), params.country.strip().title())
        rows: list[EconDBExportDestinations] = []
        for item in data:
            destination = item.get("Country")
            value = item.get("Value million USD")
            if not isinstance(destination, str) or not destination.strip():
                raise ProviderEngineError("ECONDB_SHAPE_INVALID")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ProviderEngineError("ECONDB_SHAPE_INVALID")
            rows.append(
                EconDBExportDestinations(
                    origin_country=origin,
                    destination_country=destination.replace("_", " ").title(),
                    value=float(value),
                    units=units,
                    title=str(meta.get("title") or ""),
                    footnote=(str(meta["footnote"]) if meta.get("footnote") else None),
                )
            )
        return tuple(rows)


_GDP_ADJUST = {
    "United States",
    "Canada",
    "Mexico",
    "South Africa",
    "Argentina",
    "Uzbekistan",
    "Kazakhstan",
    "Bosnia And Herzegovina",
}
_POP_MULTIPLIER = {
    "United States": 1000,
    "China": 10000,
    "Japan": 10000,
    "Russian Federation": 1000000,
    "Australia": 1000,
    "Brazil": 1000,
    "South Africa": 1000,
    "Switzerland": 1000,
    "Taiwan": 1000,
    "Bangladesh": 1000000,
    "Pakistan": 1000000,
    "Panama": 1000,
    "Honduras": 1000000,
    "Vietnam": 1000,
    "Malaysia": 1000,
    "Laos": 1000,
}
_SCALES = {
    "thousands": 1_000,
    "tens of thousands": 10_000,
    "tens thousands": 10_000,
    "hundreds of thousands": 100_000,
    "hundreds thousands": 100_000,
    "millions": 1_000_000,
    "milions": 1_000_000,
    "tens of millions": 10_000_000,
    "tens millions": 10_000_000,
    "hundreds of millions": 100_000_000,
    "hundreds millions": 100_000_000,
    "billions": 1_000_000_000,
}
_TICKERS = (
    ("GDP", "TUSD", "gdp_usd"),
    ("RGDP", "TPOP", "gdp_qoq"),
    ("RGDP", "TOYA", "gdp_yoy"),
    ("POP", None, "population"),
    ("CPI", "TOYA", "cpi_yoy"),
    ("CORE", "TOYA", "core_yoy"),
    ("RETA", "TOYA", "retail_sales_yoy"),
    ("IP", "TOYA", "industrial_production_yoy"),
    ("POLIR", None, "policy_rate"),
    ("Y10YD", None, "yield_10y"),
    ("GDEBT", "TUSD", None),
    ("CA", "TPGP", "current_account_gdp"),
    ("URATE", None, "jobless_rate"),
)


class EcondbCountryProfileQuery(QueryParams):
    """The country whose macro profile is requested."""

    source: Literal["econdb"] = "econdb"
    market: Literal["global"] = "global"
    country: str


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.replace("\u00a0", " ").replace(",", "").replace(" ", "").strip()
        if not text or text.casefold() in {"nan", "null"}:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _series(item: dict[str, Any]) -> list[tuple[str, float]]:
    key = str(item.get("id") or "")
    entries = []
    for entry in item.get("dataarray") or []:
        if not isinstance(entry, dict):
            continue
        value = _number(entry.get(key))
        if value is None:
            continue
        entries.append((str(entry.get("date") or ""), value))
    entries.sort(key=lambda pair: pair[0])
    return entries


def _scale(item: dict[str, Any]) -> int:
    meta = item.get("td") or {}
    return _SCALES.get(str(meta.get("scale") or "").lower(), 1)


class EconDBCountryProfileFetcher(
    Fetcher[EcondbCountryProfileQuery, tuple[EconDBCountryProfile, ...]]
):
    """Read all thirteen context series in one request and build the profile row."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CountryProfile"
    row_model: ClassVar[type[EconDBCountryProfile]] = EconDBCountryProfile
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="econdb_country_profile",
        period="variable",
        market="global",
        source="econdb",
        verified=False,
        notes="Public series-context table; no credential, offline acceptance uses a fixture.",
    )

    def transform_query(self, **kwargs: object) -> EcondbCountryProfileQuery:
        """Require a non-blank country code."""
        query = EcondbCountryProfileQuery.model_validate(kwargs)
        if not query.country.strip():
            raise ProviderEngineError("ECONDB_QUERY_INVALID", invalid=("country",))
        return query

    def _get(self, url: str, ctx: FetchContext) -> list[dict[str, Any]]:
        transport = self.http_transport or _http_get_json
        response = transport(
            url,
            {},
            timeout=min(float(ctx.timeout or TIMEOUT_SECONDS), 30.0),
            source=SOURCE,
            headers={"Accept": "application/json"},
        )
        if not 200 <= response.status < 300:
            raise ProviderEngineError("ECONDB_HTTP_ERROR", status=response.status, url=url)
        if not isinstance(response.document, list):
            raise ProviderEngineError("ECONDB_SHAPE_INVALID")
        return response.document

    def extract_data(
        self,
        params: EcondbCountryProfileQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Request all thirteen context tickers in one call."""
        country = params.country.strip().upper()
        tickers = [
            f"{symbol}{country}" + (f"~{transform}" if transform else "")
            for symbol, transform, _field in _TICKERS
        ]
        query = urlencode({"tickers": "[" + ",".join(tickers) + "]"})
        document = self._get(f"{CONTEXT_URL}?{query}", ctx)
        if len(document) < len(_TICKERS):
            raise ProviderEngineError("ECONDB_SHAPE_INVALID")
        return document

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: EcondbCountryProfileQuery,
    ) -> FetchResult:
        """Build the single profile row with the reference units and ratios."""
        by_id = {str(item.get("id") or ""): item for item in raw if isinstance(item, dict)}
        country = params.country.strip().upper()
        first_id = f"GDP{country}~TUSD"
        first = by_id.get(first_id)
        if first is None:
            raise ProviderEngineError("ECONDB_SHAPE_INVALID")
        geography = first.get("geography") or {}
        country_name = str(geography.get("name") or params.country.strip().title())
        values: dict[str, float | int | None] = {}
        for symbol, transform, field in _TICKERS:
            ticker = f"{symbol}{country}" + (f"~{transform}" if transform else "")
            item = by_id.get(ticker)
            if item is None:
                raise ProviderEngineError("ECONDB_SHAPE_INVALID")
            series = _series(item)
            if not series:
                raise ProviderEngineError("ECONDB_SHAPE_INVALID")
            scale = _scale(item)
            if field == "gdp_usd":
                window = series[-4:] if country_name not in _GDP_ADJUST else series[-1:]
                if len(window) < (1 if country_name in _GDP_ADJUST else 4):
                    raise ProviderEngineError("ECONDB_SHAPE_INVALID")
                values[field] = sum(value for _date, value in window) * scale / 1_000_000_000
            elif field == "population":
                values[field] = int(series[-1][1] * _POP_MULTIPLIER.get(country_name, 1))
            elif field in (
                "gdp_qoq",
                "gdp_yoy",
                "cpi_yoy",
                "core_yoy",
                "retail_sales_yoy",
                "industrial_production_yoy",
                "policy_rate",
                "yield_10y",
                "current_account_gdp",
                "jobless_rate",
            ):
                values[field] = series[-1][1] / 100
            else:
                values["_gdebt"] = series[-1][1] * scale / 1_000_000_000
        gdp = values.get("gdp_usd")
        debt = values.pop("_gdebt", None)
        if not isinstance(gdp, (int, float)) or gdp == 0 or not isinstance(debt, (int, float)):
            raise ProviderEngineError("ECONDB_SHAPE_INVALID")
        values["govt_debt_gdp"] = debt / gdp
        values["country"] = country_name
        return (EconDBCountryProfile.model_validate(values),)


__all__ = [
    "EconDBCountryProfileFetcher",
    "EconDBExportDestinationsFetcher",
    "EcondbCountryProfileQuery",
    "EcondbExportDestinationsQuery",
]
