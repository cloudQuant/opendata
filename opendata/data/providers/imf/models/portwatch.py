"""IMF PortWatch public ArcGIS tables with explicit paging and typed rows."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, ClassVar, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.models.macro_iter2 import (
    ImfMaritimeChokePointInfo,
    ImfMaritimeChokePointVolume,
    ImfPortInfo,
    ImfPortVolume,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.imf._source import SOURCE
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from collections.abc import Mapping

PORTWATCH_ARCGIS_ROOT = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"
PORTWATCH_PAGE_SIZE = 1000

ChokepointName = Literal[
    "suez_canal",
    "panama_canal",
    "bosporus_strait",
    "bab_el_mandeb_strait",
    "malacca_strait",
    "strait_of_hormuz",
    "cape_of_good_hope",
    "gibraltar_strait",
    "dover_strait",
    "oresund_strait",
    "taiwan_strait",
    "korea_strait",
    "tsugaru_strait",
    "luzon_strait",
    "lombok_strait",
    "ombai_strait",
    "bohai_strait",
    "torres_strait",
    "sunda_strait",
    "makassar_strait",
    "magellan_strait",
    "yucatan_channel",
    "windward_passage",
    "mona_passage",
    "balabac_strait",
    "bering_strait",
    "mindoro_strait",
    "kerch_strait",
]
Continent = Literal["north_america", "europe", "asia_pacific", "south_america", "africa"]


class ImfPortWatchError(RuntimeError):
    """Credential-safe, stable errors for PortWatch HTTP and payload failures."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        url: str | None = None,
        incomplete: bool = False,
    ) -> None:
        """Store a stable code and a query-free endpoint for diagnostics."""
        self.code = code
        self.status = status
        self.url = _safe_endpoint(url) if url else None
        self.incomplete = incomplete
        details = "" if status is None else f" status={status}"
        details += "" if self.url is None else f" url={self.url}"
        super().__init__(code + details)


@dataclass(frozen=True)
class _ArcGisFeature:
    """One ArcGIS feature after the response envelope has been checked."""

    attributes: Mapping[str, object]


@dataclass
class _ArcGisRequestBudget:
    """Count every count/page request in one source query."""

    maximum: int
    used: int = 0

    @property
    def remaining(self) -> int:
        return self.maximum - self.used


def _safe_endpoint(url: str) -> str:
    """Drop URL query and fragment before an endpoint appears in an error."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _http_get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
    """Read a public PortWatch ArcGIS REST endpoint through the shared client."""
    try:
        response = get_shared_http_client().get(
            url,
            params=params,
            timeout=timeout if timeout is not None else 30.0,
            source=SOURCE,
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise ImfPortWatchError(
            "IMF_PORTWATCH_HTTP_ERROR", status=exc.status, url=exc.url
        ) from None
    except Exception:
        raise ImfPortWatchError("IMF_PORTWATCH_HTTP_ERROR", url=url) from None
    return response.status_code, response.text


def _fetch_pages(
    *,
    service: str,
    where: str,
    out_fields: str,
    timeout: float | None,
    max_requests: int,
    request_budget: _ArcGisRequestBudget | None = None,
) -> tuple[_ArcGisFeature, ...]:
    """Read a complete stable ObjectId-ordered result or fail without truncating."""
    url = f"{PORTWATCH_ARCGIS_ROOT}/{service}/FeatureServer/0/query"
    budget = request_budget or _ArcGisRequestBudget(max_requests)
    if budget.remaining < 2:
        raise ImfPortWatchError("IMF_PORTWATCH_REQUEST_BUDGET_EXCEEDED", url=url, incomplete=True)

    def _get(params: dict[str, str]) -> tuple[int, str]:
        if budget.remaining <= 0:
            raise ImfPortWatchError(
                "IMF_PORTWATCH_REQUEST_BUDGET_EXCEEDED", url=url, incomplete=True
            )
        budget.used += 1
        return _http_get(url, params, timeout)

    def _count() -> int:
        status, body = _get({"where": where, "returnCountOnly": "true", "f": "json"})
        if status != 200:
            raise ImfPortWatchError("IMF_PORTWATCH_HTTP_ERROR", status=status, url=url)
        try:
            document = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            raise ImfPortWatchError("IMF_PORTWATCH_BAD_RESPONSE", status=status, url=url) from None
        count = document.get("count") if isinstance(document, dict) else None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ImfPortWatchError("IMF_PORTWATCH_BAD_RESPONSE", status=status, url=url)
        return count

    expected_count = _count()
    target_count = expected_count
    minimum_pages = (target_count + PORTWATCH_PAGE_SIZE - 1) // PORTWATCH_PAGE_SIZE
    # Reserve one request for a final count so mutations during keyset traversal
    # cannot be reported as a complete result.
    if minimum_pages > budget.remaining - 1:
        raise ImfPortWatchError("IMF_PORTWATCH_REQUEST_BUDGET_EXCEEDED", url=url, incomplete=True)

    rows: list[_ArcGisFeature] = []
    seen_object_ids: set[int] = set()
    last_object_id: int | None = None
    effective_fields = (
        out_fields if "ObjectId" in out_fields.split(",") else f"{out_fields},ObjectId"
    )
    while len(rows) < target_count:
        if budget.remaining <= 1:
            raise ImfPortWatchError(
                "IMF_PORTWATCH_REQUEST_BUDGET_EXCEEDED",
                url=url,
                incomplete=True,
            )
        page_where = where
        if last_object_id is not None:
            page_where = f"({where}) AND ObjectId > {last_object_id}"
        status, body = _get(
            {
                "where": page_where,
                "outFields": effective_fields,
                "returnGeometry": "false",
                "orderByFields": "ObjectId ASC",
                "resultRecordCount": str(min(PORTWATCH_PAGE_SIZE, target_count - len(rows))),
                "f": "json",
            }
        )
        if status != 200:
            raise ImfPortWatchError("IMF_PORTWATCH_HTTP_ERROR", status=status, url=url)
        try:
            document = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            raise ImfPortWatchError("IMF_PORTWATCH_BAD_RESPONSE", status=status, url=url) from None
        if not isinstance(document, dict) or "error" in document:
            raise ImfPortWatchError("IMF_PORTWATCH_BAD_RESPONSE", status=status, url=url)
        features = document.get("features")
        exceeded = document.get("exceededTransferLimit", False)
        if not isinstance(features, list) or not isinstance(exceeded, bool):
            raise ImfPortWatchError("IMF_PORTWATCH_BAD_RESPONSE", status=status, url=url)
        page: list[_ArcGisFeature] = []
        for feature in features:
            if not isinstance(feature, dict):
                raise ImfPortWatchError("IMF_PORTWATCH_BAD_RESPONSE", status=status, url=url)
            attributes = feature.get("attributes")
            if not isinstance(attributes, dict) or any(not isinstance(k, str) for k in attributes):
                raise ImfPortWatchError("IMF_PORTWATCH_BAD_RESPONSE", status=status, url=url)
            page.append(_ArcGisFeature(attributes))
        if not page:
            raise ImfPortWatchError("IMF_PORTWATCH_BAD_RESPONSE", status=status, url=url)
        requested_count = min(PORTWATCH_PAGE_SIZE, target_count - len(rows))
        if exceeded and len(page) < requested_count:
            raise ImfPortWatchError("IMF_PORTWATCH_BAD_RESPONSE", status=status, url=url)
        for feature in page:
            attributes = feature.attributes
            object_id = attributes.get("ObjectId")
            if isinstance(object_id, bool) or not isinstance(object_id, int):
                raise ImfPortWatchError("IMF_PORTWATCH_BAD_RESPONSE", status=status, url=url)
            if object_id in seen_object_ids or (
                last_object_id is not None and object_id <= last_object_id
            ):
                raise ImfPortWatchError("IMF_PORTWATCH_BAD_RESPONSE", status=status, url=url)
            seen_object_ids.add(object_id)
            last_object_id = object_id
        if len(rows) + len(page) > target_count:
            raise ImfPortWatchError("IMF_PORTWATCH_BAD_RESPONSE", status=status, url=url)
        if exceeded and len(rows) + len(page) >= target_count:
            raise ImfPortWatchError("IMF_PORTWATCH_BAD_RESPONSE", status=status, url=url)
        rows.extend(page)

    if _count() != expected_count:
        raise ImfPortWatchError("IMF_PORTWATCH_SOURCE_CHANGED", url=url, incomplete=True)
    return tuple(rows)


def _sql_text(value: str) -> str:
    """Quote a caller value for ArcGIS standardized SQL after strict validation."""
    return "'" + value.replace("'", "''") + "'"


def _where_dates(start: date | None, end: date | None) -> str:
    """Build inclusive DATE predicates for PortWatch DateOnly observations."""
    conditions: list[str] = []
    if start is not None:
        conditions.append(f"date >= DATE '{start.isoformat()}'")
    if end is not None:
        conditions.append(f"date <= DATE '{end.isoformat()}'")
    return " AND ".join(conditions) if conditions else "1=1"


def _attributes(feature: _ArcGisFeature) -> Mapping[str, object]:
    """Return the checked ArcGIS attributes for named-field normalization."""
    return feature.attributes


def _required_text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing or invalid source field {key}")
    return value.strip()


def _required_iso3(row: Mapping[str, object], key: str) -> str:
    """Require the three-letter source identity exposed by the layer schema."""
    value = _required_text(row, key).upper()
    if re.fullmatch(r"[A-Z]{3}", value) is None:
        raise ValueError(f"invalid ISO-3 source field {key}")
    return value


def _optional_text(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid source field {key}")
    return value.strip() or None


def _number(row: Mapping[str, object], key: str) -> float:
    value = row.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"missing or invalid numeric source field {key}")
    return float(value)


def _integer(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"missing or invalid integer source field {key}")
    return value


def _contract_integer(row: Mapping[str, object], key: str) -> int:
    """Preserve the documented integer contract without truncating source values."""
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"missing or invalid numeric source field {key}")
    if isinstance(value, int):
        return value
    if not math.isfinite(value) or not value.is_integer():
        raise ValueError(f"source field {key} is not an integral vessel count")
    return int(value)


def _require_integer_sum(
    row: Mapping[str, object], total: str, components: tuple[str, ...]
) -> None:
    """Reject source aggregates that contradict the published component formula."""
    if _contract_integer(row, total) != sum(
        _contract_integer(row, component) for component in components
    ):
        raise ValueError(f"source aggregate {total} does not equal its documented components")


def _metric_ton_integer(row: Mapping[str, object], key: str) -> int:
    """Read a finite, integral and non-negative ArcGIS tonnage field."""
    value = _contract_integer(row, key)
    if value < 0:
        raise ValueError(f"source metric-ton field {key} must be non-negative")
    try:
        finite_output = math.isfinite(float(value))
    except OverflowError:
        finite_output = False
    if not finite_output:
        raise ValueError(f"source metric-ton field {key} must be finite")
    return value


def _port_call_integer(row: Mapping[str, object], key: str) -> int:
    """Read a non-negative integer vessel-call count."""
    value = _integer(row, key)
    if value < 0:
        raise ValueError(f"source port-call field {key} must be non-negative")
    return value


def _source_date(row: Mapping[str, object]) -> date:
    value = row.get("date")
    if not isinstance(value, str):
        raise ValueError("missing or invalid DateOnly source field date")
    return date.fromisoformat(value)


class ImfChokePointInfoQuery(QueryParams):
    """Validated PortWatch metadata query."""

    theme: Literal["dark", "light"] | None = None
    max_requests: StrictInt = Field(default=64, ge=3, le=64)


class ImfChokePointVolumeQuery(QueryParams):
    """Validated daily transit query with an explicit page request bound."""

    chokepoint: ChokepointName | None = None
    max_requests: StrictInt = Field(default=64, ge=3, le=64)

    @model_validator(mode="after")
    def _validate_window(self) -> ImfChokePointVolumeQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        if self.chokepoint is not None and self.max_requests < 6:
            raise ValueError(
                "a chokepoint filter needs two consistent three-request source queries"
            )
        return self


class ImfPortInfoQuery(QueryParams):
    """Validated country/continent filter and ranked result limit."""

    continent: Continent | None = None
    country: StrictStr | None = None
    # OpenBB's standard contract retains this widget grouping field, which has no source filter.
    port_code: StrictStr | None = None
    limit: StrictInt | None = Field(default=None, ge=1, le=100000)
    max_requests: StrictInt = Field(default=64, ge=3, le=64)

    @field_validator("country")
    @classmethod
    def _country_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().upper()
        if re.fullmatch(r"[A-Z]{3}", cleaned) is None:
            raise ValueError("country must be a three-letter ISO code")
        return cleaned

    @field_validator("port_code")
    @classmethod
    def _port_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned or re.fullmatch(r"[A-Za-z0-9_-]+", cleaned) is None:
            raise ValueError("port_code must contain only letters, digits, '_' or '-'")
        return cleaned


class ImfPortVolumeQuery(QueryParams):
    """Validated daily port observation filter and pagination budget."""

    port_code: StrictStr | None = None
    country: StrictStr | None = None
    max_requests: StrictInt = Field(default=64, ge=3, le=64)

    @field_validator("port_code")
    @classmethod
    def _port_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned or re.fullmatch(r"[A-Za-z0-9_-]+", cleaned) is None:
            raise ValueError("port_code must contain only letters, digits, '_' or '-'")
        return cleaned

    @field_validator("country")
    @classmethod
    def _country_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().upper()
        if re.fullmatch(r"[A-Z]{3}", cleaned) is None:
            raise ValueError("country must be a three-letter ISO code")
        return cleaned

    @model_validator(mode="after")
    def _validate_window_and_scope(self) -> ImfPortVolumeQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        if self.port_code is not None and self.country is not None:
            raise ValueError("choose port_code or country, not both")
        if (
            self.port_code is None
            and self.country is None
            and self.start_date is None
            and self.end_date is None
        ):
            raise ValueError("a port, country, or date filter is required")
        return self


class ImfChokePointInfoFetcher(Fetcher[ImfChokePointInfoQuery, tuple[_ArcGisFeature, ...]]):
    """Read long-run chokepoint metadata from the official PortWatch layer."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "MaritimeChokePointInfo"
    row_model: ClassVar[type[ImfMaritimeChokePointInfo]] = ImfMaritimeChokePointInfo
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="imf_maritime_chokepoint_info",
        period="snapshot",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> ImfChokePointInfoQuery:
        """Validate caller filters and request budget before source I/O."""
        return ImfChokePointInfoQuery.model_validate(kwargs)

    def extract_data(
        self, params: ImfChokePointInfoQuery, ctx: FetchContext
    ) -> tuple[_ArcGisFeature, ...]:
        """Fetch every chokepoint metadata row in ObjectId order."""
        return _fetch_pages(
            service="PortWatch_chokepoints_database",
            where="1=1",
            out_fields=(
                "portid,portname,lat,lon,vessel_count_total,vessel_count_tanker,"
                "vessel_count_container,vessel_count_general_cargo,vessel_count_dry_bulk,"
                "vessel_count_RoRo,industry_top1,industry_top2,industry_top3"
            ),
            timeout=ctx.timeout,
            max_requests=params.max_requests,
        )

    def transform_data(
        self, raw: tuple[_ArcGisFeature, ...], params: ImfChokePointInfoQuery
    ) -> FetchResult:
        """Validate published totals and map rows to the fixed typed contract."""
        try:
            result: list[ImfMaritimeChokePointInfo] = []
            for row in map(_attributes, raw):
                _require_integer_sum(
                    row,
                    "vessel_count_total",
                    (
                        "vessel_count_container",
                        "vessel_count_dry_bulk",
                        "vessel_count_general_cargo",
                        "vessel_count_RoRo",
                        "vessel_count_tanker",
                    ),
                )
                result.append(
                    ImfMaritimeChokePointInfo(
                        chokepoint_code=_required_text(row, "portid"),
                        name=_required_text(row, "portname"),
                        latitude=_number(row, "lat"),
                        longitude=_number(row, "lon"),
                        vessel_count_total=_contract_integer(row, "vessel_count_total"),
                        vessel_count_tanker=_contract_integer(row, "vessel_count_tanker"),
                        vessel_count_container=_contract_integer(row, "vessel_count_container"),
                        vessel_count_general_cargo=_contract_integer(
                            row, "vessel_count_general_cargo"
                        ),
                        vessel_count_dry_bulk=_contract_integer(row, "vessel_count_dry_bulk"),
                        vessel_count_roro=_contract_integer(row, "vessel_count_RoRo"),
                        industry_top1=_optional_text(row, "industry_top1"),
                        industry_top2=_optional_text(row, "industry_top2"),
                        industry_top3=_optional_text(row, "industry_top3"),
                    )
                )
            return tuple(result)
        except (TypeError, ValueError):
            raise ImfPortWatchError("IMF_PORTWATCH_BAD_OBSERVATION") from None


class ImfChokePointVolumeFetcher(Fetcher[ImfChokePointVolumeQuery, tuple[_ArcGisFeature, ...]]):
    """Read daily chokepoint transit counts and capacity estimates."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "MaritimeChokePointVolume"
    row_model: ClassVar[type[ImfMaritimeChokePointVolume]] = ImfMaritimeChokePointVolume
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="imf_maritime_chokepoint_volume",
        period="1D",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> ImfChokePointVolumeQuery:
        """Validate date filters, chokepoint selector and request budget."""
        return ImfChokePointVolumeQuery.model_validate(kwargs)

    def extract_data(
        self, params: ImfChokePointVolumeQuery, ctx: FetchContext
    ) -> tuple[_ArcGisFeature, ...]:
        """Fetch daily rows and, when selected, resolve the source chokepoint ID."""
        request_budget = _ArcGisRequestBudget(params.max_requests)
        where_parts: list[str] = []
        if params.chokepoint is not None:
            metadata = _fetch_pages(
                service="PortWatch_chokepoints_database",
                where="1=1",
                out_fields="portid,portname",
                timeout=ctx.timeout,
                max_requests=params.max_requests,
                request_budget=request_budget,
            )
            selected = [
                _required_text(_attributes(feature), "portid")
                for feature in metadata
                if _normalize_name(_required_text(_attributes(feature), "portname"))
                == _normalize_name(params.chokepoint)
            ]
            if len(selected) != 1:
                raise ImfPortWatchError("IMF_PORTWATCH_UNKNOWN_CHOKEPOINT")
            where_parts.append(f"portid = {_sql_text(selected[0])}")
        date_filter = _where_dates(params.start_date, params.end_date)
        if date_filter != "1=1":
            where_parts.append(date_filter)
        return _fetch_pages(
            service="Daily_Chokepoints_Data",
            where=" AND ".join(where_parts) if where_parts else "1=1",
            out_fields=(
                "date,portid,portname,n_container,n_dry_bulk,n_general_cargo,n_roro,"
                "n_tanker,n_cargo,n_total,capacity_container,capacity_dry_bulk,"
                "capacity_general_cargo,capacity_roro,capacity_tanker,capacity_cargo,capacity"
            ),
            timeout=ctx.timeout,
            max_requests=params.max_requests,
            request_budget=request_budget,
        )

    def transform_data(
        self, raw: tuple[_ArcGisFeature, ...], params: ImfChokePointVolumeQuery
    ) -> FetchResult:
        """Keep reported capacities and expose exact component deltas as diagnostics."""
        try:
            result: list[ImfMaritimeChokePointVolume] = []
            for row in map(_attributes, raw):
                _require_integer_sum(
                    row,
                    "n_cargo",
                    ("n_container", "n_dry_bulk", "n_general_cargo", "n_roro"),
                )
                _require_integer_sum(
                    row,
                    "n_total",
                    ("n_container", "n_dry_bulk", "n_general_cargo", "n_roro", "n_tanker"),
                )
                capacity_fields = (
                    "capacity_container",
                    "capacity_dry_bulk",
                    "capacity_general_cargo",
                    "capacity_roro",
                    "capacity_tanker",
                    "capacity_cargo",
                    "capacity",
                )
                capacity = {key: _metric_ton_integer(row, key) for key in capacity_fields}
                cargo_component_sum = sum(
                    capacity[key]
                    for key in (
                        "capacity_container",
                        "capacity_dry_bulk",
                        "capacity_general_cargo",
                        "capacity_roro",
                    )
                )
                total_component_sum = cargo_component_sum + capacity["capacity_tanker"]
                cargo_component_delta = capacity["capacity_cargo"] - cargo_component_sum
                total_component_delta = capacity["capacity"] - total_component_sum
                reported_total_delta = capacity["capacity"] - (
                    capacity["capacity_cargo"] + capacity["capacity_tanker"]
                )
                result.append(
                    ImfMaritimeChokePointVolume(
                        date=_source_date(row),
                        chokepoint=_required_text(row, "portname"),
                        vessels_total=_integer(row, "n_total"),
                        vessels_cargo=_integer(row, "n_cargo"),
                        vessels_tanker=_integer(row, "n_tanker"),
                        vessels_container=_integer(row, "n_container"),
                        vessels_general_cargo=_integer(row, "n_general_cargo"),
                        vessels_dry_bulk=_integer(row, "n_dry_bulk"),
                        vessels_roro=_integer(row, "n_roro"),
                        capacity_total=float(capacity["capacity"]),
                        capacity_cargo=float(capacity["capacity_cargo"]),
                        capacity_tanker=float(capacity["capacity_tanker"]),
                        capacity_container=float(capacity["capacity_container"]),
                        capacity_general_cargo=float(capacity["capacity_general_cargo"]),
                        capacity_dry_bulk=float(capacity["capacity_dry_bulk"]),
                        capacity_roro=float(capacity["capacity_roro"]),
                        capacity_cargo_component_delta_metric_tons=float(cargo_component_delta),
                        capacity_total_component_delta_metric_tons=float(total_component_delta),
                        capacity_reported_total_delta_metric_tons=float(reported_total_delta),
                        capacity_cargo_components_consistent=(cargo_component_delta == 0),
                        capacity_total_components_consistent=(total_component_delta == 0),
                        capacity_reported_total_consistent=(reported_total_delta == 0),
                    )
                )
            return tuple(sorted(result, key=lambda point: (point.chokepoint, point.date)))
        except (TypeError, ValueError):
            raise ImfPortWatchError("IMF_PORTWATCH_BAD_OBSERVATION") from None


class ImfPortInfoFetcher(Fetcher[ImfPortInfoQuery, tuple[_ArcGisFeature, ...]]):
    """Read PortWatch port metadata, optionally ranked by annual traffic."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "PortInfo"
    row_model: ClassVar[type[ImfPortInfo]] = ImfPortInfo
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="imf_port_info",
        period="snapshot",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> ImfPortInfoQuery:
        """Validate port filters and the complete-table request limit."""
        return ImfPortInfoQuery.model_validate(kwargs)

    def extract_data(
        self, params: ImfPortInfoQuery, ctx: FetchContext
    ) -> tuple[_ArcGisFeature, ...]:
        """Fetch the full filtered port snapshot before applying a rank limit."""
        where_parts: list[str] = []
        if params.country is not None:
            where_parts.append(f"ISO3 = {_sql_text(params.country)}")
        if params.continent is not None:
            source_continent = _source_continent(params.continent)
            where_parts.append(f"continent = {_sql_text(source_continent)}")
        # port_code is a grouping-only field in the documented model contract.
        return _fetch_pages(
            service="PortWatch_ports_database",
            where=" AND ".join(where_parts) if where_parts else "1=1",
            out_fields=(
                "portid,continent,country,ISO3,portname,fullname,lat,lon,vessel_count_total,"
                "vessel_count_tanker,vessel_count_container,vessel_count_general_cargo,"
                "vessel_count_dry_bulk,vessel_count_RoRo,industry_top1,industry_top2,"
                "industry_top3,share_country_maritime_import,share_country_maritime_export"
            ),
            timeout=ctx.timeout,
            max_requests=params.max_requests,
        )

    def transform_data(
        self, raw: tuple[_ArcGisFeature, ...], params: ImfPortInfoQuery
    ) -> FetchResult:
        """Validate annual count totals and preserve the source share scale."""
        try:
            result: list[ImfPortInfo] = []
            for row in map(_attributes, raw):
                _require_integer_sum(
                    row,
                    "vessel_count_total",
                    (
                        "vessel_count_container",
                        "vessel_count_dry_bulk",
                        "vessel_count_general_cargo",
                        "vessel_count_RoRo",
                        "vessel_count_tanker",
                    ),
                )
                result.append(
                    ImfPortInfo(
                        port_code=_required_text(row, "portid"),
                        continent=_required_text(row, "continent"),
                        country=_required_text(row, "country"),
                        country_code=_required_iso3(row, "ISO3"),
                        port_name=_required_text(row, "portname"),
                        port_full_name=_required_text(row, "fullname"),
                        latitude=_number(row, "lat"),
                        longitude=_number(row, "lon"),
                        vessel_count_total=_contract_integer(row, "vessel_count_total"),
                        vessel_count_tanker=_contract_integer(row, "vessel_count_tanker"),
                        vessel_count_container=_contract_integer(row, "vessel_count_container"),
                        vessel_count_general_cargo=_contract_integer(
                            row, "vessel_count_general_cargo"
                        ),
                        vessel_count_dry_bulk=_contract_integer(row, "vessel_count_dry_bulk"),
                        vessel_count_roro=_contract_integer(row, "vessel_count_RoRo"),
                        industry_top1=_optional_text(row, "industry_top1"),
                        industry_top2=_optional_text(row, "industry_top2"),
                        industry_top3=_optional_text(row, "industry_top3"),
                        share_country_maritime_import=_number(row, "share_country_maritime_import"),
                        share_country_maritime_export=_number(row, "share_country_maritime_export"),
                    )
                )
            # A rank limit must select from the complete table, not the first
            # ObjectId page. Stable sort retains ObjectId order for tied values.
            result.sort(key=lambda port: port.vessel_count_total, reverse=True)
            return tuple(result[: params.limit] if params.limit is not None else result)
        except (TypeError, ValueError):
            raise ImfPortWatchError("IMF_PORTWATCH_BAD_OBSERVATION") from None


class ImfPortVolumeFetcher(Fetcher[ImfPortVolumeQuery, tuple[_ArcGisFeature, ...]]):
    """Read daily PortWatch port calls and estimated trade volumes."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "PortVolume"
    row_model: ClassVar[type[ImfPortVolume]] = ImfPortVolume
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="imf_port_volume",
        period="1D",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> ImfPortVolumeQuery:
        """Validate the daily port selector and inclusive date window."""
        return ImfPortVolumeQuery.model_validate(kwargs)

    def extract_data(
        self, params: ImfPortVolumeQuery, ctx: FetchContext
    ) -> tuple[_ArcGisFeature, ...]:
        """Fetch all daily port rows matching the validated query."""
        where_parts: list[str] = []
        if params.port_code is not None:
            where_parts.append(f"portid = {_sql_text(params.port_code)}")
        elif params.country is not None:
            where_parts.append(f"ISO3 = {_sql_text(params.country)}")
        date_filter = _where_dates(params.start_date, params.end_date)
        if date_filter != "1=1":
            where_parts.append(date_filter)
        return _fetch_pages(
            service="Daily_Ports_Data",
            where=" AND ".join(where_parts) if where_parts else "1=1",
            out_fields=(
                "date,portid,portname,country,ISO3,portcalls_container,portcalls_dry_bulk,"
                "portcalls_general_cargo,portcalls_roro,portcalls_tanker,portcalls,"
                "portcalls_cargo,"
                "import_container,import_dry_bulk,import_general_cargo,import_roro,"
                "import_tanker,import_cargo,import,export_container,export_dry_bulk,"
                "export_general_cargo,export_roro,export_tanker,export_cargo,export"
            ),
            timeout=ctx.timeout,
            max_requests=params.max_requests,
        )

    def transform_data(
        self, raw: tuple[_ArcGisFeature, ...], params: ImfPortVolumeQuery
    ) -> FetchResult:
        """Map source counts and metric-ton estimates with exact-sum diagnostics."""
        del params
        try:
            result: list[ImfPortVolume] = []
            for feature in raw:
                row = _attributes(feature)
                _require_integer_sum(
                    row,
                    "portcalls_cargo",
                    (
                        "portcalls_container",
                        "portcalls_dry_bulk",
                        "portcalls_general_cargo",
                        "portcalls_roro",
                    ),
                )
                _require_integer_sum(
                    row,
                    "portcalls",
                    (
                        "portcalls_container",
                        "portcalls_dry_bulk",
                        "portcalls_general_cargo",
                        "portcalls_roro",
                        "portcalls_tanker",
                    ),
                )

                imports_container = _metric_ton_integer(row, "import_container")
                imports_dry_bulk = _metric_ton_integer(row, "import_dry_bulk")
                imports_general_cargo = _metric_ton_integer(row, "import_general_cargo")
                imports_roro = _metric_ton_integer(row, "import_roro")
                imports_tanker = _metric_ton_integer(row, "import_tanker")
                imports_cargo = _metric_ton_integer(row, "import_cargo")
                imports_total = _metric_ton_integer(row, "import")
                exports_container = _metric_ton_integer(row, "export_container")
                exports_dry_bulk = _metric_ton_integer(row, "export_dry_bulk")
                exports_general_cargo = _metric_ton_integer(row, "export_general_cargo")
                exports_roro = _metric_ton_integer(row, "export_roro")
                exports_tanker = _metric_ton_integer(row, "export_tanker")
                exports_cargo = _metric_ton_integer(row, "export_cargo")
                exports_total = _metric_ton_integer(row, "export")

                imports_cargo_delta = imports_cargo - sum(
                    (
                        imports_container,
                        imports_dry_bulk,
                        imports_general_cargo,
                        imports_roro,
                    )
                )
                imports_total_delta = imports_total - sum(
                    (
                        imports_container,
                        imports_dry_bulk,
                        imports_general_cargo,
                        imports_roro,
                        imports_tanker,
                    )
                )
                exports_cargo_delta = exports_cargo - sum(
                    (
                        exports_container,
                        exports_dry_bulk,
                        exports_general_cargo,
                        exports_roro,
                    )
                )
                exports_total_delta = exports_total - sum(
                    (
                        exports_container,
                        exports_dry_bulk,
                        exports_general_cargo,
                        exports_roro,
                        exports_tanker,
                    )
                )

                result.append(
                    ImfPortVolume(
                        date=_source_date(row),
                        port_code=_required_text(row, "portid"),
                        port_name=_optional_text(row, "portname"),
                        country=_optional_text(row, "country"),
                        country_code=_required_iso3(row, "ISO3"),
                        portcalls=_port_call_integer(row, "portcalls"),
                        portcalls_cargo=_port_call_integer(row, "portcalls_cargo"),
                        portcalls_tanker=_port_call_integer(row, "portcalls_tanker"),
                        portcalls_container=_port_call_integer(row, "portcalls_container"),
                        portcalls_general_cargo=_port_call_integer(row, "portcalls_general_cargo"),
                        portcalls_dry_bulk=_port_call_integer(row, "portcalls_dry_bulk"),
                        portcalls_roro=_port_call_integer(row, "portcalls_roro"),
                        imports=float(imports_total),
                        imports_cargo=float(imports_cargo),
                        imports_tanker=float(imports_tanker),
                        imports_container=float(imports_container),
                        imports_general_cargo=float(imports_general_cargo),
                        imports_dry_bulk=float(imports_dry_bulk),
                        imports_roro=float(imports_roro),
                        exports=float(exports_total),
                        exports_cargo=float(exports_cargo),
                        exports_tanker=float(exports_tanker),
                        exports_container=float(exports_container),
                        exports_general_cargo=float(exports_general_cargo),
                        exports_dry_bulk=float(exports_dry_bulk),
                        exports_roro=float(exports_roro),
                        imports_cargo_component_delta_metric_tons=float(imports_cargo_delta),
                        imports_total_component_delta_metric_tons=float(imports_total_delta),
                        exports_cargo_component_delta_metric_tons=float(exports_cargo_delta),
                        exports_total_component_delta_metric_tons=float(exports_total_delta),
                        imports_cargo_components_consistent=imports_cargo_delta == 0,
                        imports_total_components_consistent=imports_total_delta == 0,
                        exports_cargo_components_consistent=exports_cargo_delta == 0,
                        exports_total_components_consistent=exports_total_delta == 0,
                    )
                )
            return tuple(sorted(result, key=lambda point: (point.port_code, point.date)))
        except (TypeError, ValueError):
            raise ImfPortWatchError("IMF_PORTWATCH_BAD_OBSERVATION") from None


def _normalize_name(value: str) -> str:
    """Compare a documented snake-case chokepoint name with source display text."""
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", ascii_value.casefold())


def _source_continent(value: Continent) -> str:
    """Map the fixed query enum to the official ports-layer category label."""
    if value == "asia_pacific":
        return "Asia & Pacific"
    return value.replace("_", " ").title()


__all__ = [
    "ImfChokePointInfoFetcher",
    "ImfChokePointVolumeFetcher",
    "ImfPortInfoFetcher",
    "ImfPortVolumeFetcher",
    "ImfPortWatchError",
]
