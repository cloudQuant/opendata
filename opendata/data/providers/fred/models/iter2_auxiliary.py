"""Evidence-bounded FRED release, regional, BOP, calendar and SEP models."""

from __future__ import annotations

import json
import math
import re
from datetime import date as date_type
from typing import Any, ClassVar, Literal

from pydantic import Field, StrictBool, ValidationError, field_validator, model_validator

import opendata.core.config as config
from opendata.data.capability import Capability
from opendata.data.models.fred_iter2 import (
    FredBalanceOfPayments,
    FredEconomicCalendar,
    FredPROJECTIONS,
    FredRegional,
    FredReleaseTable,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.fred.models import _client
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models.series import FredSeriesFetcher, FredSeriesQuery
from opendata.data.providers.fred.models.treasury_rates import _FixedRateQuery, _parse_strict_date

_DATE_TOKEN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_ID_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_MAX_RELEASE_TABLE_NODES = 10_000
_PROJECTIONS: dict[str, str] = {
    "range_high": "FEDTARRH",
    "central_tendency_high": "FEDTARCTH",
    "median": "FEDTARMD",
    "range_midpoint": "FEDTARRM",
    "central_tendency_midpoint": "FEDTARCTM",
    "range_low": "FEDTARRL",
    "central_tendency_low": "FEDTARCTL",
}
_PROJECTIONS_LONG_RUN: dict[str, str] = {
    "range_high": "FEDTARRHLR",
    "central_tendency_high": "FEDTARCTHLR",
    "median": "FEDTARMDLR",
    "range_midpoint": "FEDTARRMLR",
    "central_tendency_midpoint": "FEDTARCTMLR",
    "range_low": "FEDTARRLLR",
    "central_tendency_low": "FEDTARCTLLR",
}
_BOP_SERIES: dict[str, str] = {
    "current_account": "IEABC",
    "goods_balance": "IEABCG",
    "services_balance": "IEABCS",
    "primary_income_balance": "IEABCPI",
    "secondary_income_balance": "IEABCSI",
}


def _fred_json(
    endpoint: str,
    params: dict[str, str],
    timeout: float | None,
) -> dict[str, Any]:
    """Perform one query-only FRED GET and return a validated JSON object."""
    api_key = _client.require_api_key()
    base_url = config.get_settings().fred_api_base_url or _client.FRED_DEFAULT_BASE_URL
    url = f"{base_url.rstrip('/')}{endpoint}"
    status, body = _client._http_get(
        url,
        {**params, "api_key": api_key, "file_type": "json"},
        timeout,
    )
    if status != 200:
        raise FredProviderError("FRED_HTTP_ERROR", status=status, url=url)
    try:
        payload = json.loads(body)
    except (TypeError, json.JSONDecodeError) as exc:
        raise FredProviderError("FRED_BAD_RESPONSE", status=status, url=url) from exc
    if type(payload) is not dict:
        raise FredProviderError("FRED_BAD_RESPONSE", status=status, url=url)
    return payload


def _parse_date(value: object) -> date_type:
    """Parse a strict date object or canonical FRED ISO date string."""
    if type(value) is date_type:
        return value
    if type(value) is not str or _DATE_TOKEN.fullmatch(value) is None:
        raise ValueError("date must be canonical YYYY-MM-DD")
    parsed = date_type.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date must be canonical YYYY-MM-DD")
    return parsed


def _parse_value(value: object) -> float | None:
    """Accept finite numeric text and the documented dot missing sentinel."""
    if type(value) is not str:
        raise ValueError("FRED value must be text")
    numeric_text = value.strip()
    if numeric_text == ".":
        return None
    if not numeric_text:
        raise ValueError("FRED value must not be empty")
    number = float(numeric_text)
    if not math.isfinite(number):
        raise ValueError("FRED value must be finite")
    return number


def _required_int(value: object, *, name: str, minimum: int = 0) -> int:
    if type(value) is int and value >= minimum:
        return value
    if type(value) is str and re.fullmatch(r"[0-9]+", value):
        parsed = int(value)
        if parsed >= minimum:
            return parsed
    raise ValueError(f"{name} must be a nonnegative integer")


class FredReleaseTableQuery(QueryParams):
    """Release-table tree query with bounded date fan-out."""

    source: Literal["auto", "fred"] = "auto"
    market: Literal["us"] | None = None
    symbol: None = None
    start_date: None = None
    end_date: None = None
    release_id: str
    element_id: str | None = None
    date: date_type | list[date_type | None] | None = None
    max_nodes: int = Field(default=10_000, ge=1, le=10_000, strict=True)

    @field_validator("release_id", "element_id")
    @classmethod
    def _validate_numeric_ids(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if re.fullmatch(r"[0-9]{1,12}", value) is None:
            raise ValueError("release and element IDs must contain decimal digits only")
        return value

    @field_validator("date", mode="before")
    @classmethod
    def _validate_dates(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            if not value or len(value) > 3:
                raise ValueError("date must contain one to three dates")
            return [None if item is None else _parse_strict_date(item) for item in value]
        return _parse_strict_date(value)

    @model_validator(mode="after")
    def _one_date_per_item(self) -> FredReleaseTableQuery:
        if isinstance(self.date, list) and len(self.date) != len(set(self.date)):
            raise ValueError("date values must be unique")
        return self

    def selected_dates(self) -> tuple[date_type | None, ...]:
        """Return one requested date value for each bounded endpoint call."""
        if isinstance(self.date, list):
            return tuple(self.date)
        return (self.date,)


class FredReleaseTableFetcher(Fetcher[FredReleaseTableQuery, list[dict[str, Any]]]):
    """Flatten the official FRED release table tree without persisting it."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FredReleaseTable"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fred_release_table",
        period="event",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = FredReleaseTableQuery
    output_model = FredReleaseTable

    def transform_query(self, **kwargs: object) -> FredReleaseTableQuery:
        """Validate the release-table identity and date fan-out before I/O."""
        return self.query_model.model_validate(kwargs)

    def extract_data(
        self,
        params: FredReleaseTableQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Fetch each selected release-table tree through the shared client."""
        result: list[dict[str, Any]] = []
        for requested_date in params.selected_dates():
            request: dict[str, str] = {
                "release_id": params.release_id,
                "include_observation_values": "true",
            }
            if params.element_id is not None:
                request["element_id"] = params.element_id
            if requested_date is not None:
                request["observation_date"] = requested_date.isoformat()
            payload = _fred_json("/fred/release/tables", request, ctx.timeout)
            result.append(
                {
                    "requested_date": requested_date,
                    "payload": payload,
                    "max_nodes": params.max_nodes,
                    "release_id": params.release_id,
                }
            )
        return result

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: FredReleaseTableQuery,
    ) -> FetchResult:
        """Flatten and validate the release-table element tree."""
        if type(raw) is not list or len(raw) != len(params.selected_dates()):
            raise FredProviderError("FRED_BAD_RESPONSE")
        by_key: dict[tuple[str | None, date_type | None], FredReleaseTable] = {}
        try:
            for batch in raw:
                if type(batch) is not dict or type(batch.get("payload")) is not dict:
                    raise ValueError("release table batch must contain a JSON object")
                requested_date = batch["requested_date"]
                payload = batch["payload"]
                elements = payload.get("elements")
                if type(elements) is not dict:
                    raise ValueError("release table elements must be a mapping")
                if payload.get("release_id") is not None and _required_int(
                    payload["release_id"], name="release_id", minimum=1
                ) != int(batch["release_id"]):
                    raise ValueError("FRED returned a different release ID")
                nodes: list[dict[str, Any]] = []
                root = {
                    key: payload[key]
                    for key in (
                        "element_id",
                        "parent_id",
                        "line",
                        "type",
                        "name",
                        "level",
                        "series_id",
                        "children",
                        "date",
                        "value",
                    )
                    if key in payload
                }
                if root.get("element_id") is not None:
                    root.setdefault("children", list(elements.values()))
                    nodes.append(root)
                stack = list(elements.values())
                while stack:
                    node = stack.pop()
                    if type(node) is not dict:
                        raise ValueError("release table node must be an object")
                    nodes.append(node)
                    children = node.get("children", [])
                    if children is None:
                        children = []
                    if type(children) is not list:
                        raise ValueError("release table children must be an array")
                    stack.extend(reversed(children))
                seen_ids: dict[str, dict[str, Any]] = {}
                for node in nodes:
                    element_id = node.get("element_id")
                    element_text = (
                        None
                        if element_id is None
                        else str(_required_int(element_id, name="element_id"))
                    )
                    previous = seen_ids.get(element_text or "")
                    if previous is not None:
                        if previous != node:
                            raise ValueError("duplicate release-table element conflicts")
                        continue
                    if element_text is not None:
                        seen_ids[element_text] = node
                    children = node.get("children", [])
                    if any(type(child) is not dict for child in children):
                        raise ValueError("release table child must be an object")
                    child_ids = [
                        str(_required_int(child.get("element_id"), name="child element_id"))
                        for child in children
                        if child.get("element_id") is not None
                    ]
                    element_type = node.get("type")
                    if element_type is not None and type(element_type) is not str:
                        raise ValueError("element type must be text")
                    series_id = node.get("series_id")
                    if series_id is not None and (
                        type(series_id) is not str or _ID_TOKEN.fullmatch(series_id) is None
                    ):
                        raise ValueError("series ID must be an identifier")
                    if node.get("release_id") is not None and _required_int(
                        node["release_id"], name="release_id", minimum=1
                    ) != int(batch["release_id"]):
                        raise ValueError("release table node belongs to another release")
                    raw_date = node.get("date", node.get("observation_date"))
                    row_date = None
                    if raw_date is not None:
                        row_date = _parse_date(raw_date)
                    elif element_type == "series" and requested_date is not None:
                        row_date = requested_date
                    raw_value = node.get("value", node.get("observation_value"))
                    value = None if raw_value is None else _parse_value(raw_value)
                    raw_level = node.get("level")
                    raw_line = node.get("line")
                    record = FredReleaseTable.model_validate(
                        {
                            "date": row_date,
                            "level": None
                            if raw_level is None
                            else _required_int(raw_level, name="level"),
                            "element_type": element_type,
                            "line": None
                            if raw_line is None
                            else _required_int(raw_line, name="line"),
                            "element_id": element_text,
                            "parent_id": None
                            if node.get("parent_id") is None
                            else str(_required_int(node["parent_id"], name="parent_id")),
                            "children": ",".join(child_ids) if child_ids else None,
                            "symbol": series_id,
                            "name": node.get("name"),
                            "value": value,
                        }
                    )
                    key = (record.element_id, record.date)
                    prior = by_key.get(key)
                    if prior is not None and prior.model_dump() != record.model_dump():
                        raise ValueError("release table date batches conflict")
                    by_key[key] = record
                    if len(by_key) > min(params.max_nodes, _MAX_RELEASE_TABLE_NODES):
                        raise FredProviderError("FRED_QUERY_LIMIT")
        except (KeyError, TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_OBSERVATION") from exc
        return tuple(by_key.values())


class EconomicCalendarQuery(QueryParams):
    """FRED release-date calendar query within the endpoint's page limits."""

    source: Literal["auto", "fred"] = "auto"
    market: Literal["us"] | None = None
    symbol: None = None
    release_id: int | None = Field(default=None, ge=1, strict=True)
    include_release_dates_with_no_data: StrictBool = True
    limit: int = Field(default=1_000, ge=1, le=1_000, strict=True)
    max_pages: int = Field(default=5, ge=1, le=5, strict=True)
    sort_order: Literal["asc", "desc"] = "desc"

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _strict_dates(cls, value: object) -> date_type | None:
        if value is None:
            return None
        return _parse_strict_date(value)

    @model_validator(mode="after")
    def _valid_window(self) -> EconomicCalendarQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self


class EconomicCalendarFetcher(Fetcher[EconomicCalendarQuery, list[dict[str, Any]]]):
    """Fetch FRED's official publication dates for economic data releases."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "EconomicCalendar"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fred_economic_calendar",
        period="event",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = EconomicCalendarQuery
    output_model = FredEconomicCalendar

    def transform_query(self, **kwargs: object) -> EconomicCalendarQuery:
        """Validate the event-date window and bounded calendar paging."""
        return self.query_model.model_validate(kwargs)

    def extract_data(
        self,
        params: EconomicCalendarQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Fetch release dates and a single release's official name when needed."""
        endpoint = (
            "/fred/release/dates" if params.release_id is not None else "/fred/releases/dates"
        )
        fixed: dict[str, str] = {
            "order_by": "release_date",
            "sort_order": params.sort_order,
            "include_release_dates_with_no_data": str(
                params.include_release_dates_with_no_data
            ).lower(),
            # Query the current calendar snapshot. The public start/end dates
            # below filter event dates, not the API's real-time vintage axis.
            "realtime_start": date_type.today().isoformat(),
            "realtime_end": date_type.today().isoformat(),
        }
        release_name: str | None = None
        if params.release_id is not None:
            fixed["release_id"] = str(params.release_id)
            fixed["limit"] = "10000"
            max_pages = 1
            metadata = _fred_json(
                "/fred/release",
                {
                    "release_id": str(params.release_id),
                    "realtime_start": fixed["realtime_start"],
                    "realtime_end": fixed["realtime_end"],
                },
                ctx.timeout,
            )
            releases = metadata.get("releases")
            try:
                if (
                    type(releases) is not list
                    or len(releases) != 1
                    or type(releases[0]) is not dict
                    or _required_int(releases[0].get("id"), name="release_id", minimum=1)
                    != params.release_id
                    or type(releases[0].get("name")) is not str
                    or not releases[0]["name"].strip()
                ):
                    raise ValueError("release metadata does not match the query")
            except (KeyError, TypeError, ValueError) as exc:
                raise FredProviderError("FRED_BAD_RESPONSE") from exc
            release_name = releases[0]["name"]
        else:
            fixed["limit"] = str(params.limit)
            max_pages = params.max_pages
        pages: list[dict[str, Any]] = []
        offset = 0
        expected_count: int | None = None
        for _ in range(max_pages):
            request_params = {**fixed, "offset": str(offset)}
            payload = _fred_json(endpoint, request_params, ctx.timeout)
            rows_key = "release_dates"
            rows = payload.get(rows_key)
            try:
                count = _required_int(payload.get("count"), name="count")
                response_offset = _required_int(payload.get("offset"), name="offset")
                reported_limit = _required_int(payload.get("limit"), name="limit", minimum=1)
            except ValueError as exc:
                raise FredProviderError("FRED_PAGINATION_ERROR") from exc
            if (
                type(rows) is not list
                or response_offset != offset
                or reported_limit != int(fixed["limit"])
                or any(type(row) is not dict for row in rows)
                or (expected_count is not None and expected_count != count)
            ):
                raise FredProviderError("FRED_PAGINATION_ERROR")
            expected_count = count
            pages.append(
                {
                    "rows": rows,
                    "realtime_start": payload.get("realtime_start"),
                    "realtime_end": payload.get("realtime_end"),
                    "count": count,
                    "release_name": release_name,
                }
            )
            if offset + len(rows) >= count:
                return pages
            if not rows:
                raise FredProviderError("FRED_PAGINATION_ERROR")
            offset += len(rows)
        if expected_count is not None and offset < expected_count:
            raise FredProviderError("FRED_QUERY_LIMIT")
        return pages

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: EconomicCalendarQuery,
    ) -> FetchResult:
        """Apply event-date filters and normalize typed FRED calendar events."""
        if type(raw) is not list:
            raise FredProviderError("FRED_BAD_RESPONSE")
        result: dict[tuple[int, date_type], FredEconomicCalendar] = {}
        try:
            for page in raw:
                if type(page) is not dict or type(page.get("rows")) is not list:
                    raise ValueError("release calendar page is malformed")
                _parse_date(page["realtime_start"])
                _parse_date(page["realtime_end"])
                for item in page["rows"]:
                    release_id = _required_int(item.get("release_id"), name="release_id", minimum=1)
                    if params.release_id is not None and release_id != params.release_id:
                        raise ValueError("FRED returned a different release ID")
                    release_name = item.get("release_name")
                    if release_name is None:
                        # Single-release endpoint documents only the date and release ID.
                        # Release metadata is fetched separately below for an exact event name.
                        release_name = page.get("release_name")
                    if type(release_name) is not str or not release_name.strip():
                        raise ValueError("FRED release name is missing")
                    date = _parse_date(item.get("date"))
                    if params.start_date is not None and date < params.start_date:
                        continue
                    if params.end_date is not None and date > params.end_date:
                        continue
                    row = FredEconomicCalendar.model_validate(
                        {"date": date, "release_id": release_id, "event": release_name}
                    )
                    key = (release_id, date)
                    prior = result.get(key)
                    if prior is not None and prior != row:
                        raise ValueError("duplicate release dates conflict")
                    result[key] = row
        except (KeyError, TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_OBSERVATION") from exc
        rows = list(result.values())
        rows.sort(key=lambda row: (row.date, row.release_id), reverse=params.sort_order == "desc")
        return tuple(rows)


class FredRegionalQuery(QueryParams):
    """GeoFRED query; group queries name their spatial aggregation explicitly."""

    source: Literal["auto", "fred"] = "auto"
    market: Literal["us"] | None = None
    symbol: str
    is_series_group: StrictBool = False
    region_type: (
        Literal["bea", "msa", "frb", "necta", "state", "country", "county", "censusregion"] | None
    ) = None
    season: Literal["sa", "nsa", "ssa"] = "nsa"
    units: str | None = None
    frequency: (
        Literal[
            "a",
            "q",
            "m",
            "w",
            "d",
            "wef",
            "weth",
            "wew",
            "wetu",
            "wem",
            "wesu",
            "wesa",
            "bwew",
            "bwem",
        ]
        | None
    ) = None
    aggregation_method: Literal["avg", "sum", "eop"] = "eop"
    transformation: Literal["lin", "chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log"] = "lin"
    limit: int = Field(default=100_000, ge=1, le=100_000, strict=True)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _strict_dates(cls, value: object) -> date_type | None:
        if value is None:
            return None
        return _parse_strict_date(value)

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        if _ID_TOKEN.fullmatch(value) is None:
            raise ValueError("symbol must be a FRED identifier")
        return value

    @field_validator("units")
    @classmethod
    def _validate_units(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > 80):
            raise ValueError("units must be nonempty text of at most 80 characters")
        return value

    @model_validator(mode="after")
    def _valid_request(self) -> FredRegionalQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        group_only = (self.region_type, self.units, self.frequency)
        if self.is_series_group and self.region_type is None:
            raise ValueError("region_type is required for a series group")
        if self.is_series_group and self.units is None:
            raise ValueError("units is required for a series group")
        if not self.is_series_group and any(item is not None for item in group_only):
            raise ValueError("region_type, units and frequency require is_series_group")
        return self


class FredRegionalFetcher(Fetcher[FredRegionalQuery, list[dict[str, Any]]]):
    """Read FRED's GeoFRED regional cross-sections for a series or group."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FredRegional"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fred_regional",
        period="variable",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = FredRegionalQuery
    output_model = FredRegional

    def transform_query(self, **kwargs: object) -> FredRegionalQuery:
        """Validate GeoFRED IDs, spatial options, and the date range."""
        return self.query_model.model_validate(kwargs)

    def extract_data(self, params: FredRegionalQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Read a regional series cross-section or a named series group."""
        if params.is_series_group:
            request: dict[str, str] = {
                "series_group": params.symbol,
                "region_type": params.region_type or "",
                "season": params.season.upper(),
                "transformation": params.transformation,
                "aggregation_method": params.aggregation_method,
                "units": params.units or "",
            }
            request["date"] = (params.end_date or date_type.today()).isoformat()
            if params.start_date is not None:
                request["start_date"] = params.start_date.isoformat()
            if params.frequency is not None:
                request["frequency"] = params.frequency
            endpoint = "/geofred/regional/data"
        else:
            request = {"series_id": params.symbol}
            request["date"] = (params.end_date or date_type.today()).isoformat()
            if params.start_date is not None:
                request["start_date"] = params.start_date.isoformat()
            endpoint = "/geofred/series/data"
        payload = _fred_json(endpoint, request, ctx.timeout)
        return [{"payload": payload, "series_group": params.is_series_group, "limit": params.limit}]

    def transform_data(self, raw: list[dict[str, Any]], params: FredRegionalQuery) -> FetchResult:
        """Normalize GeoFRED rows and enforce the caller's result cap."""
        if type(raw) is not list or len(raw) != 1 or type(raw[0]) is not dict:
            raise FredProviderError("FRED_BAD_RESPONSE")
        payload = raw[0].get("payload")
        if type(payload) is not dict:
            raise FredProviderError("FRED_BAD_RESPONSE")
        rows: list[FredRegional] = []
        try:
            if params.is_series_group:
                for date_groups in payload.values():
                    if type(date_groups) is not dict:
                        raise ValueError("GeoFRED regional group must map dates to rows")
                    for date_text, observations in date_groups.items():
                        date = _parse_geofred_period(date_text)
                        if type(observations) is not list:
                            raise ValueError("GeoFRED date group must be a list")
                        rows.extend(_regional_row(item, date) for item in observations)
            else:
                meta = payload.get("meta")
                if type(meta) is not dict or type(meta.get("data")) is not list:
                    raise ValueError("GeoFRED series response lacks meta.data")
                meta_date = meta.get("date")
                for item in meta["data"]:
                    item_date = item.get("date") if type(item) is dict else None
                    date = _parse_date(item_date if item_date is not None else meta_date)
                    rows.append(_regional_row(item, date))
            if any(params.start_date is not None and row.date < params.start_date for row in rows):
                rows = [
                    row
                    for row in rows
                    if params.start_date is None or row.date >= params.start_date
                ]
            if params.end_date is not None:
                rows = [row for row in rows if row.date <= params.end_date]
            rows.sort(key=lambda row: (row.date, row.region, row.series_id))
            if len(rows) > params.limit:
                raise FredProviderError("FRED_QUERY_LIMIT")
        except (KeyError, TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_OBSERVATION") from exc
        return tuple(rows)


def _regional_row(item: object, date: date_type) -> FredRegional:
    if type(item) is not dict:
        raise ValueError("GeoFRED observation must be an object")
    region = item.get("region")
    code = item.get("code")
    series_id = item.get("series_id")
    if type(region) is not str or not region.strip():
        raise ValueError("region name is missing")
    if type(code) is not str or not code:
        raise ValueError("region code must be the exact source string")
    if type(series_id) is not str or _ID_TOKEN.fullmatch(series_id) is None:
        raise ValueError("region series ID is invalid")
    raw_value = item.get("value")
    value = None if raw_value is None else _parse_value(raw_value)
    return FredRegional.model_validate(
        {
            "date": date,
            "region": region,
            "code": code,
            "value": value,
            "series_id": series_id,
        }
    )


def _parse_geofred_period(value: object) -> date_type:
    """Parse GeoFRED JSON's date, year-month, and year group keys."""
    if type(value) is not str:
        raise ValueError("GeoFRED period must be text")
    if re.fullmatch(r"[0-9]{4}", value):
        return date_type(int(value), 1, 1)
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        return date_type.fromisoformat(f"{value}-01")
    return _parse_date(value)


class BalanceOfPaymentsQuery(_FixedRateQuery):
    """Only the quarterly U.S. BOP report is mapped for FRED."""

    report_type: (
        Literal[
            "main",
            "summary",
            "services",
            "investment_income",
            "direct_investment",
            "portfolio_investment",
            "other_investment",
        ]
        | None
    ) = "main"
    frequency: Literal["monthly", "quarterly"] | None = None
    country: Literal[
        "brazil",
        "canada",
        "china",
        "eu_ex_euro_area",
        "eu_institutions",
        "india",
        "japan",
        "russia",
        "switzerland",
        "united_kingdom",
        "united_states",
        "total",
    ] = "united_states"

    @model_validator(mode="after")
    def _supported_fred_slice(self) -> BalanceOfPaymentsQuery:
        if self.report_type not in (None, "main"):
            raise ValueError("FRED mapping currently supports only the main BOP report")
        if self.country != "united_states":
            raise ValueError("FRED mapping currently supports only the United States")
        if self.frequency != "quarterly":
            raise ValueError("FRED mapping requires explicit quarterly frequency")
        return self


class BalanceOfPaymentsFetcher(Fetcher[BalanceOfPaymentsQuery, list[dict[str, Any]]]):
    """Join five individually verified BEA International Transactions series."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "BalanceOfPayments"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fred_balance_of_payments",
        period="1Q",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = BalanceOfPaymentsQuery
    output_model = FredBalanceOfPayments

    def transform_query(self, **kwargs: object) -> BalanceOfPaymentsQuery:
        """Validate the supported U.S. quarterly BOP request subset."""
        return self.query_model.model_validate(kwargs)

    def extract_data(
        self, params: BalanceOfPaymentsQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch mapped BEA series through the standard observations pager."""
        result: list[dict[str, Any]] = []
        try:
            for field_name, series_id in _BOP_SERIES.items():
                query_data = params.model_dump(mode="python")
                series_params = {
                    key: value
                    for key, value in query_data.items()
                    if key in FredSeriesQuery.model_fields and key != "frequency"
                }
                query = FredSeriesQuery.model_validate(
                    {
                        **series_params,
                        "series_id": series_id,
                        "transform_units": "lin",
                        "output_type": 1,
                    }
                )
                observations = FredSeriesFetcher().extract_data(query, ctx)
                result.extend(
                    {
                        "field_name": field_name,
                        "series_id": series_id,
                        "observation": observation,
                    }
                    for observation in observations
                )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_QUERY") from exc
        return result

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: BalanceOfPaymentsQuery,
    ) -> FetchResult:
        """Join BOP values by quarter while preserving series revisions."""
        if type(raw) is not list:
            raise FredProviderError("FRED_BAD_OBSERVATION")
        columns: dict[str, dict[date_type, tuple[float | None, date_type, date_type]]] = {
            name: {} for name in _BOP_SERIES
        }
        try:
            for wrapper in raw:
                if type(wrapper) is not dict or type(wrapper.get("observation")) is not dict:
                    raise ValueError("BOP observation wrapper is malformed")
                name, series_id = wrapper["field_name"], wrapper["series_id"]
                if type(name) is not str or _BOP_SERIES.get(name) != series_id:
                    raise ValueError("unmapped BOP series")
                observation = wrapper["observation"]
                period = _parse_date(observation.get("date"))
                if period.month not in (1, 4, 7, 10) or period.day != 1:
                    raise ValueError("BOP observation date must identify a calendar-quarter start")
                realtime_start = _parse_date(observation.get("realtime_start"))
                realtime_end = _parse_date(observation.get("realtime_end"))
                if realtime_start > realtime_end:
                    raise ValueError("BOP real-time interval is reversed")
                if params.start_date is not None and period < params.start_date:
                    raise ValueError("BOP row precedes start_date")
                if params.end_date is not None and period > params.end_date:
                    raise ValueError("BOP row follows end_date")
                _validate_pit(realtime_start, realtime_end, params)
                if period in columns[name]:
                    raise ValueError("multiple vintages cannot fit the period key")
                columns[name][period] = (
                    _parse_value(observation.get("value")),
                    realtime_start,
                    realtime_end,
                )
            all_dates = set().union(*(set(column) for column in columns.values()))
            rows: list[FredBalanceOfPayments] = []
            for period in sorted(all_dates, reverse=params.sort_order == "desc"):
                fields: dict[str, object] = {
                    "period": period,
                    "currency_code": "USD",
                    "unit": "millions_of_us_dollars",
                    "source_series_ids": dict(_BOP_SERIES),
                }
                provenance: dict[str, tuple[date_type, date_type]] = {}
                for field_name, series_rows in columns.items():
                    source_row = series_rows.get(period)
                    fields[field_name] = None if source_row is None else source_row[0]
                    if source_row is not None:
                        provenance[_BOP_SERIES[field_name]] = (source_row[1], source_row[2])
                row = FredBalanceOfPayments.model_validate(fields)
                row._realtime_by_series = provenance
                rows.append(row)
        except (KeyError, TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_OBSERVATION") from exc
        return tuple(rows)


class ProjectionsQuery(QueryParams):
    """FOMC rate projections with explicit vintage and result bounds."""

    source: Literal["auto", "fred"] = "auto"
    market: Literal["us"] | None = None
    symbol: None = None
    long_run: StrictBool = False
    as_of: date_type | None = None
    realtime_start: date_type | None = None
    realtime_end: date_type | None = None
    vintage_dates: list[date_type] | None = None
    sort_order: Literal["asc", "desc"] = "asc"
    page_size: int = Field(default=100, ge=1, le=100, strict=True)
    max_records: int = Field(default=100, ge=1, le=100, strict=True)
    max_pages: int = Field(default=1, ge=1, le=1, strict=True)

    @field_validator(
        "start_date", "end_date", "as_of", "realtime_start", "realtime_end", mode="before"
    )
    @classmethod
    def _strict_dates(cls, value: object) -> date_type | None:
        if value is None:
            return None
        return _parse_strict_date(value)

    @field_validator("vintage_dates", mode="before")
    @classmethod
    def _strict_vintage_dates(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, (list, tuple)) or len(value) != 1:
            raise ValueError("vintage_dates must contain exactly one date")
        return [_parse_strict_date(value[0])]

    @model_validator(mode="after")
    def _valid_pit_window(self) -> ProjectionsQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        if (
            self.realtime_start is not None
            and self.realtime_end is not None
            and self.realtime_start > self.realtime_end
        ):
            raise ValueError("realtime_start must not be after realtime_end")
        has_window = self.realtime_start is not None or self.realtime_end is not None
        if self.as_of is not None and has_window:
            raise ValueError("as_of cannot be combined with a real-time window")
        if self.vintage_dates is not None and (self.as_of is not None or has_window):
            raise ValueError("vintage_dates cannot be combined with another PIT selector")
        return self


class ProjectionsFetcher(Fetcher[ProjectionsQuery, list[dict[str, Any]]]):
    """Join the seven annual, NSA FOMC federal-funds projection series."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "PROJECTIONS"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fred_projections",
        period="release",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = ProjectionsQuery
    output_model = FredPROJECTIONS

    def transform_query(self, **kwargs: object) -> ProjectionsQuery:
        """Validate the SEP vintage selector and explicit result limits."""
        return self.query_model.model_validate(kwargs)

    def extract_data(self, params: ProjectionsQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Fetch the seven regular or long-run SEP projection series."""
        mapping = _PROJECTIONS_LONG_RUN if params.long_run else _PROJECTIONS
        rows: list[dict[str, Any]] = []
        try:
            for field_name, series_id in mapping.items():
                values = params.model_dump(mode="python")
                series_params = {
                    key: value
                    for key, value in values.items()
                    if key in FredSeriesQuery.model_fields
                }
                query = FredSeriesQuery.model_validate(
                    {
                        **series_params,
                        "series_id": series_id,
                        "transform_units": "lin",
                        "output_type": 1,
                    }
                )
                observations = FredSeriesFetcher().extract_data(query, ctx)
                rows.extend(
                    {
                        "field_name": field_name,
                        "series_id": series_id,
                        "observation": observation,
                    }
                    for observation in observations
                )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_QUERY") from exc
        return rows

    def transform_data(self, raw: list[dict[str, Any]], params: ProjectionsQuery) -> FetchResult:
        """Align projections by observation date and preserve source vintages."""
        if type(raw) is not list:
            raise FredProviderError("FRED_BAD_OBSERVATION")
        mapping = _PROJECTIONS_LONG_RUN if params.long_run else _PROJECTIONS
        columns: dict[str, dict[date_type, tuple[float | None, date_type, date_type]]] = {
            name: {} for name in mapping
        }
        try:
            for wrapper in raw:
                if type(wrapper) is not dict or type(wrapper.get("observation")) is not dict:
                    raise ValueError("projection observation wrapper is malformed")
                field_name, series_id = wrapper["field_name"], wrapper["series_id"]
                if type(field_name) is not str or mapping.get(field_name) != series_id:
                    raise ValueError("unmapped FOMC projection series")
                observation = wrapper["observation"]
                date = _parse_date(observation.get("date"))
                if params.start_date is not None and date < params.start_date:
                    raise ValueError("projection precedes start_date")
                if params.end_date is not None and date > params.end_date:
                    raise ValueError("projection follows end_date")
                realtime_start = _parse_date(observation.get("realtime_start"))
                realtime_end = _parse_date(observation.get("realtime_end"))
                if realtime_start > realtime_end:
                    raise ValueError("projection vintage interval is reversed")
                _validate_pit(realtime_start, realtime_end, params)
                field_rows = columns[field_name]
                if date in field_rows:
                    raise ValueError("multiple projection vintages collide on date key")
                field_rows[date] = (
                    _parse_value(observation.get("value")),
                    realtime_start,
                    realtime_end,
                )
            dates = set().union(*(set(column) for column in columns.values()))
            rows: list[FredPROJECTIONS] = []
            for date in sorted(dates, reverse=params.sort_order == "desc"):
                values: dict[str, object] = {"date": date}
                provenance: dict[str, tuple[date_type, date_type]] = {}
                for name, column in columns.items():
                    observation = column.get(date)
                    values[name] = None if observation is None else observation[0]
                    if observation is not None:
                        provenance[mapping[name]] = (observation[1], observation[2])
                row = FredPROJECTIONS.model_validate(values)
                row._realtime_by_series = provenance
                rows.append(row)
        except (KeyError, TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_OBSERVATION") from exc
        return tuple(rows)


def _validate_pit(
    realtime_start: date_type,
    realtime_end: date_type,
    params: _FixedRateQuery | ProjectionsQuery,
) -> None:
    if params.as_of is not None and not realtime_start <= params.as_of <= realtime_end:
        raise ValueError("observation does not cover as_of")
    if params.realtime_start is not None and realtime_end < params.realtime_start:
        raise ValueError("observation is outside realtime_start")
    if params.realtime_end is not None and realtime_start > params.realtime_end:
        raise ValueError("observation is outside realtime_end")
    if params.vintage_dates is not None and not any(
        realtime_start <= vintage <= realtime_end for vintage in params.vintage_dates
    ):
        raise ValueError("observation does not cover requested vintage_dates")


__all__ = (
    "BalanceOfPaymentsFetcher",
    "BalanceOfPaymentsQuery",
    "EconomicCalendarFetcher",
    "EconomicCalendarQuery",
    "FredRegionalFetcher",
    "FredRegionalQuery",
    "FredReleaseTableFetcher",
    "FredReleaseTableQuery",
    "ProjectionsFetcher",
    "ProjectionsQuery",
)
