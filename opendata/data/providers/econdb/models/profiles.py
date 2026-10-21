"""Public EconDB export-destination widget adapter (no credential)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal

from opendata.data.capability import Capability
from opendata.data.models.econdb import EconDBExportDestinations
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import ProviderEngineError, _http_get_json
from opendata.data.providers.econdb._source import SOURCE

if TYPE_CHECKING:
    from opendata.data.providers._engine.http_json import HttpGet

BASE_URL = "https://www.econdb.com/widgets/top-trade-items/data/"
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


__all__ = ["EconDBExportDestinationsFetcher", "EcondbExportDestinationsQuery"]
