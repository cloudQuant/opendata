"""Bounded, typed adapters for the documented EconDB v1 series and port APIs."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, ValidationError, field_validator

from opendata.data.capability import Capability
from opendata.data.models.econdb import (
    EconDBEconomicIndicator,
    EconDBGdpNominal,
    EconDBGdpReal,
    EconDBPortVolume,
    EconDBYieldCurve,
)
from opendata.data.protocol import FetchContext, Fetcher
from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.http_json import (
    HttpGet,
    HttpResponse,
    ProviderEngineError,
    ProviderEngineQueryError,
)
from opendata.data.providers.econdb._source import SOURCE

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel

BASE_URL = "https://api.econdb.com"
CREDENTIAL_ENV_VAR = "ECONDB_API_KEY"
MAX_ROWS = 10_000
MAX_YIELD_SERIES = 10
_SERIES_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_LOCODE = re.compile(r"[A-Z]{2} [A-Z0-9]{3}\Z")
_MATURITY = re.compile(r"(?:[1-9]|[12][0-9]|30)(?:D|W|M|Y)\Z")


class _Query(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: StrictStr | None = None
    market: StrictStr | None = None


class _SeriesQuery(_Query):
    symbol: StrictStr
    country: StrictStr | None = None
    frequency: StrictStr | None = None
    start_date: date_type | None = None
    end_date: date_type | None = None
    limit: StrictInt = 5000

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _date_input(cls, value: object) -> object:
        return _parse_query_date(value)


class _GdpQuery(_Query):
    symbol: StrictStr
    country: StrictStr | None = None
    frequency: StrictStr | None = None
    start_date: date_type | None = None
    end_date: date_type | None = None
    limit: StrictInt = 5000

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _date_input(cls, value: object) -> object:
        return _parse_query_date(value)


class _YieldCurveQuery(_Query):
    country: StrictStr
    series_ids: list[StrictStr]
    maturities: list[StrictStr]
    date: date_type | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _date_input(cls, value: object) -> object:
        return _parse_query_date(value)


class _PortVolumeQuery(_Query):
    port_code: StrictStr
    start_date: date_type | None = None
    end_date: date_type | None = None
    limit: StrictInt = 5000

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _date_input(cls, value: object) -> object:
        return _parse_query_date(value)


@dataclass(frozen=True)
class _SeriesDocument:
    """Validated metadata and the aligned date/value arrays from a v1 series response."""

    symbol: str
    description: str
    country: str
    frequency: str
    dataset: str
    units: str | None
    points: tuple[tuple[date_type, float | None], ...]


def _parse_query_date(value: object) -> object:
    if value is None or (isinstance(value, date_type) and not isinstance(value, datetime)):
        return value
    if not isinstance(value, str):
        raise ValueError("date must be a canonical ISO calendar date")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be a canonical ISO calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must be a canonical ISO calendar date")
    return parsed


def _query_failure(error: ValidationError) -> ProviderEngineQueryError:
    missing = sorted({str(item["loc"][0]) for item in error.errors() if item["type"] == "missing"})
    rejected = sorted(
        {str(item["loc"][0]) for item in error.errors() if item["type"] == "extra_forbidden"}
    )
    invalid = sorted(
        {
            str(item["loc"][0])
            for item in error.errors()
            if item["type"] not in {"missing", "extra_forbidden"} and item["loc"]
        }
    )
    return ProviderEngineQueryError(
        "ECONDB_QUERY_INVALID", missing=missing, rejected=rejected, invalid=invalid
    )


def _valid_dates(start: date_type | None, end: date_type | None, limit: int) -> None:
    if start is not None and end is not None and start > end:
        raise ProviderEngineQueryError("ECONDB_QUERY_INVALID", invalid=("start_date", "end_date"))
    if not 1 <= limit <= MAX_ROWS:
        raise ProviderEngineQueryError("ECONDB_QUERY_INVALID", invalid=("limit",))


def _valid_series_id(value: str) -> bool:
    return bool(_SERIES_ID.fullmatch(value))


def _parse_source_date(value: object) -> date_type:
    if not isinstance(value, str):
        raise ValueError("date must be a source ISO date string")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date is not an ISO calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError("date is not a canonical ISO calendar date")
    return parsed


def _number(value: object, *, optional: bool) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("value is not a JSON number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("value is not finite")
    return parsed


def _source_string(
    document: Mapping[str, object], name: str, *, optional: bool = False
) -> str | None:
    value = document.get(name)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is not non-empty text")
    return value


def _series_document(document: object, requested_symbol: str) -> _SeriesDocument:
    if not isinstance(document, Mapping):
        raise ValueError("series response is not an object")
    symbol = _source_string(document, "ticker")
    description = _source_string(document, "description")
    country = _source_string(document, "geography")
    frequency = _source_string(document, "frequency")
    dataset = _source_string(document, "dataset")
    units = _source_string(document, "units", optional=True)
    metadata = document.get("additional_metadata")
    data = document.get("data")
    if symbol is None or symbol.casefold() != requested_symbol.casefold():
        raise ValueError("returned ticker does not match requested series")
    if not isinstance(metadata, Mapping) or not isinstance(data, Mapping):
        raise ValueError("series metadata/data has the wrong shape")
    dates = data.get("dates")
    values = data.get("values")
    if not isinstance(dates, list) or not isinstance(values, list):
        raise ValueError("series dates and values must be arrays")
    if len(dates) != len(values) or len(dates) > MAX_ROWS:
        raise ValueError("series array lengths differ or exceed the response cap")
    points: list[tuple[date_type, float | None]] = []
    seen: set[date_type] = set()
    for raw_date, raw_value in zip(dates, values, strict=True):
        parsed_date = _parse_source_date(raw_date)
        if parsed_date in seen:
            raise ValueError("series has duplicate observation dates")
        seen.add(parsed_date)
        points.append((parsed_date, _number(raw_value, optional=True)))
    points.sort(key=lambda item: item[0])
    return _SeriesDocument(
        symbol=symbol,
        description=description or "",
        country=country or "",
        frequency=frequency or "",
        dataset=dataset or "",
        units=units,
        points=tuple(points),
    )


def _canonical_frequency(value: str) -> str | None:
    return {
        "a": "a",
        "annual": "a",
        "annually": "a",
        "year": "a",
        "yearly": "a",
        "q": "q",
        "quarter": "q",
        "quarterly": "q",
        "m": "m",
        "month": "m",
        "monthly": "m",
        "w": "w",
        "week": "w",
        "weekly": "w",
        "d": "d",
        "day": "d",
        "daily": "d",
    }.get(value.strip().casefold())


def _check_country(actual: str, requested: str | None) -> None:
    if requested is not None and actual.strip().casefold() != requested.strip().casefold():
        raise ProviderEngineError("ECONDB_QUERY_REJECTED", invalid=("country",))


def _check_frequency(actual: str, requested: str | None) -> None:
    if requested is None:
        return
    actual_code = _canonical_frequency(actual)
    requested_code = _canonical_frequency(requested)
    if actual_code is None or actual_code != requested_code:
        raise ProviderEngineError("ECONDB_QUERY_REJECTED", invalid=("frequency",))


def _windowed_points(
    points: tuple[tuple[date_type, float | None], ...],
    *,
    start: date_type | None,
    end: date_type | None,
    limit: int,
) -> tuple[tuple[date_type, float | None], ...]:
    selected = [
        (point_date, value)
        for point_date, value in points
        if (start is None or point_date >= start) and (end is None or point_date <= end)
    ]
    return tuple(selected[-limit:])


class EconDBAPIModelFetcher(Fetcher[Any, Any]):
    """Shared token transport, strict input validation, and typed output boundary."""

    async_mode = "bounded_thread"
    http_transport: ClassVar[HttpGet | None] = None
    query_model: ClassVar[type[_Query]]
    row_model: ClassVar[type[ContractModel]]
    max_rows: ClassVar[int] = MAX_ROWS

    def transform_query(self, **kwargs: object) -> BaseModel:
        try:
            params = self.query_model.model_validate(kwargs)
        except ValidationError as exc:
            raise _query_failure(exc) from None
        if params.source not in (None, self.capability.source):
            raise ProviderEngineQueryError("ECONDB_QUERY_INVALID", invalid=("source",))
        if params.market not in (None, self.capability.market):
            raise ProviderEngineQueryError("ECONDB_QUERY_INVALID", invalid=("market",))
        if isinstance(params, (_SeriesQuery, _GdpQuery)) and not _valid_series_id(params.symbol):
            raise ProviderEngineQueryError("ECONDB_QUERY_INVALID", invalid=("symbol",))
        if isinstance(params, (_SeriesQuery, _GdpQuery, _PortVolumeQuery)):
            _valid_dates(params.start_date, params.end_date, params.limit)
        if isinstance(params, _YieldCurveQuery):
            if not params.country.strip() or not 2 <= len(params.series_ids) <= MAX_YIELD_SERIES:
                raise ProviderEngineQueryError(
                    "ECONDB_QUERY_INVALID", invalid=("country", "series_ids")
                )
            if len(params.series_ids) != len(params.maturities):
                raise ProviderEngineQueryError(
                    "ECONDB_QUERY_INVALID", invalid=("series_ids", "maturities")
                )
            if len({item.casefold() for item in params.series_ids}) != len(params.series_ids):
                raise ProviderEngineQueryError("ECONDB_QUERY_INVALID", invalid=("series_ids",))
            normalized_maturities = tuple(item.strip().upper() for item in params.maturities)
            if (
                any(not _valid_series_id(item) for item in params.series_ids)
                or any(not _MATURITY.fullmatch(item) for item in normalized_maturities)
                or len(set(normalized_maturities)) != len(normalized_maturities)
            ):
                raise ProviderEngineQueryError(
                    "ECONDB_QUERY_INVALID", invalid=("series_ids", "maturities")
                )
            params.maturities = list(normalized_maturities)
        if isinstance(params, _PortVolumeQuery):
            code = params.port_code.strip().upper().replace(" ", "")
            if len(code) == 5:
                code = f"{code[:2]} {code[2:]}"
            if not _LOCODE.fullmatch(code):
                raise ProviderEngineQueryError("ECONDB_QUERY_INVALID", invalid=("port_code",))
            params.port_code = code
        if isinstance(params, (_SeriesQuery, _GdpQuery)):
            if params.country is not None and not params.country.strip():
                raise ProviderEngineQueryError("ECONDB_QUERY_INVALID", invalid=("country",))
            if params.frequency is not None and _canonical_frequency(params.frequency) is None:
                raise ProviderEngineQueryError("ECONDB_QUERY_INVALID", invalid=("frequency",))
        return params

    def _token(self) -> str:
        token = os.environ.get(CREDENTIAL_ENV_VAR, "").strip()
        if not token:
            raise ProviderEngineError("ECONDB_CREDENTIAL_MISSING")
        return token

    def _get(self, url: str, ctx: FetchContext) -> HttpResponse:
        token = self._token()
        transport = self.http_transport or http_json._http_get_json
        timeout = min(float(ctx.timeout or 15.0), 15.0)
        response = transport(
            url,
            {},
            timeout=timeout,
            source=SOURCE,
            headers={"Authorization": f"Token {token}", "Accept": "application/json"},
        )
        if not 200 <= response.status < 300:
            if response.status == 429:
                code = "ECONDB_RATE_LIMITED"
            elif response.status in (401, 403):
                code = "ECONDB_CREDENTIAL_REJECTED"
            elif response.status in (400, 404, 422):
                code = "ECONDB_QUERY_REJECTED"
            else:
                code = "ECONDB_HTTP_ERROR"
            raise ProviderEngineError(code, status=response.status, url=url)
        return response

    def _rows(self, records: list[dict[str, object]]) -> tuple[ContractModel, ...]:
        if len(records) > self.max_rows:
            raise ProviderEngineError("ECONDB_SHAPE_INVALID")
        try:
            return tuple(self.row_model.model_validate(row, strict=True) for row in records)
        except (TypeError, ValueError, ValidationError):
            raise ProviderEngineError("ECONDB_SHAPE_INVALID") from None


class EconDBEconomicIndicatorsFetcher(EconDBAPIModelFetcher):
    """Read and flatten one documented EconDB macro indicator series."""

    canonical_model = "EconomicIndicators"
    query_model = _SeriesQuery
    row_model = EconDBEconomicIndicator
    capability = Capability(
        source=SOURCE,
        domain="econdb_economic_indicators",
        asset_class="macro",
        period="d",
        market="global",
        verified=False,
        notes="Official v1 series metadata/arrays; no live provider validation.",
    )

    def extract_data(self, params: _SeriesQuery, ctx: FetchContext) -> object:
        """Fetch the source series envelope for the requested indicator."""
        return self._get(f"{BASE_URL}/v1/series/{quote(params.symbol, safe='')}/", ctx).document

    def transform_data(self, raw: object, params: object) -> tuple[ContractModel, ...]:
        """Validate series metadata and emit observations in the requested window."""
        if not isinstance(params, _SeriesQuery):
            raise TypeError("economic-indicator query was not validated")
        try:
            series = _series_document(raw, params.symbol)
        except (TypeError, ValueError):
            raise ProviderEngineError("ECONDB_SHAPE_INVALID") from None
        _check_country(series.country, params.country)
        _check_frequency(series.frequency, params.frequency)
        points = _windowed_points(
            series.points,
            start=params.start_date,
            end=params.end_date,
            limit=params.limit,
        )
        return self._rows(
            [
                {
                    "date": observation_date,
                    "symbol": series.symbol,
                    "country": series.country,
                    "value": value,
                    "unit": series.units,
                    "title": series.description,
                    "description": series.description,
                }
                for observation_date, value in points
            ]
        )


def _gdp_semantics(series: _SeriesDocument, *, real: bool) -> None:
    description = series.description.casefold()
    if "gdp" not in description and "gross domestic product" not in description:
        raise ProviderEngineError("ECONDB_SERIES_SEMANTICS_INVALID")
    if real:
        matches = any(
            phrase in description
            for phrase in ("real", "constant", "chain volume", "chained", "volume")
        )
    else:
        matches = any(
            phrase in description
            for phrase in (
                "nominal",
                "current price",
                "current prices",
                "current-price",
                "current value",
            )
        )
    if not matches:
        raise ProviderEngineError("ECONDB_SERIES_SEMANTICS_INVALID")
    if _canonical_frequency(series.frequency) not in {"q", "a"}:
        raise ProviderEngineError("ECONDB_SERIES_SEMANTICS_INVALID")


def _month_index(value: date_type) -> int:
    return value.year * 12 + value.month


def _growth_percentage(current: float, previous: float | None) -> float | None:
    """Compute percentage change when a matching nonzero prior value exists."""
    if previous is None or previous == 0:
        return None
    return (current / previous - 1.0) * 100.0


def _gdp_rows(
    series: _SeriesDocument,
    params: _GdpQuery,
    *,
    real: bool,
) -> list[dict[str, object]]:
    _check_country(series.country, params.country)
    _check_frequency(series.frequency, params.frequency)
    _gdp_semantics(series, real=real)
    frequency = _canonical_frequency(series.frequency)
    rows: list[dict[str, object]] = []
    for point_date, value in series.points:
        if value is None:
            continue
        previous_qoq = None
        if frequency == "q":
            previous_qoq = next(
                (
                    prior_value
                    for prior_date, prior_value in series.points
                    if _month_index(point_date) - _month_index(prior_date) == 3
                ),
                None,
            )
        previous_yoy = next(
            (
                prior_value
                for prior_date, prior_value in series.points
                if _month_index(point_date) - _month_index(prior_date) == 12
            ),
            None,
        )

        base: dict[str, object] = {
            "date": point_date,
            "country": series.country,
            "value": value,
            "units": series.units,
            "symbol": series.symbol,
            "description": series.description,
        }
        if real:
            base["real_growth_qoq"] = _growth_percentage(value, previous_qoq)
            base["real_growth_yoy"] = _growth_percentage(value, previous_yoy)
        else:
            base["nominal_growth_qoq"] = _growth_percentage(value, previous_qoq)
            base["nominal_growth_yoy"] = _growth_percentage(value, previous_yoy)
        rows.append(base)
    selected: list[dict[str, object]] = []
    for row in rows:
        row_date = row["date"]
        if not isinstance(row_date, date_type) or isinstance(row_date, datetime):
            raise ValueError("GDP row date is not a calendar date")
        if params.start_date is not None and row_date < params.start_date:
            continue
        if params.end_date is not None and row_date > params.end_date:
            continue
        selected.append(row)
    return selected[-params.limit :]


class _GdpFetcher(EconDBAPIModelFetcher):
    query_model = _GdpQuery

    def extract_data(self, params: _GdpQuery, ctx: FetchContext) -> object:
        return self._get(f"{BASE_URL}/v1/series/{quote(params.symbol, safe='')}/", ctx).document

    def transform_data(self, raw: object, params: object) -> tuple[ContractModel, ...]:
        if not isinstance(params, _GdpQuery):
            raise TypeError("GDP query was not validated")
        try:
            series = _series_document(raw, params.symbol)
        except (TypeError, ValueError):
            raise ProviderEngineError("ECONDB_SHAPE_INVALID") from None
        rows = _gdp_rows(series, params, real=self.row_model is EconDBGdpReal)
        return self._rows(rows)


class EconDBGdpNominalFetcher(_GdpFetcher):
    """Map an explicitly selected, metadata-verified nominal GDP series."""

    canonical_model = "GdpNominal"
    row_model = EconDBGdpNominal
    capability = Capability(
        source=SOURCE,
        domain="econdb_gdp_nominal",
        asset_class="macro",
        period="q",
        market="global",
        verified=False,
        notes="Official v1 series; metadata gates GDP/current-price semantics.",
    )


class EconDBGdpRealFetcher(_GdpFetcher):
    """Map an explicitly selected, metadata-verified real GDP series."""

    canonical_model = "GdpReal"
    row_model = EconDBGdpReal
    capability = Capability(
        source=SOURCE,
        domain="econdb_gdp_real",
        asset_class="macro",
        period="q",
        market="global",
        verified=False,
        notes="Official v1 series; metadata gates GDP/real-price semantics.",
    )


def _port_metadata(document: object, port_code: str) -> tuple[str | None, str | None]:
    if not isinstance(document, list) or len(document) > 500:
        raise ValueError("port roster is not a bounded array")
    matches: list[tuple[str | None, str | None]] = []
    seen: set[str] = set()
    for item in document:
        if not isinstance(item, Mapping):
            raise ValueError("port roster member is not an object")
        locode = item.get("locode")
        if locode is not None and not isinstance(locode, str):
            raise ValueError("port locode is not text")
        name = item.get("name")
        country_code = item.get("iso2")
        if name is not None and not isinstance(name, str):
            raise ValueError("port name is not text")
        if country_code is not None and (
            not isinstance(country_code, str) or not re.fullmatch(r"[A-Z]{2}", country_code)
        ):
            raise ValueError("port ISO2 code is malformed")
        if locode is None:
            continue
        key = locode.upper()
        if key in seen:
            raise ValueError("port roster contains duplicate locodes")
        seen.add(key)
        if key == port_code:
            matches.append((name, country_code))
    if len(matches) != 1:
        raise ProviderEngineError("ECONDB_QUERY_REJECTED", invalid=("port_code",))
    return matches[0]


def _port_points(document: object, *, dwell: bool) -> dict[date_type, dict[str, float | None]]:
    if not isinstance(document, Mapping):
        raise ValueError("port time series is not an object")
    if document.get("type") != "timeseries":
        raise ValueError("port response type is not timeseries")
    title = document.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("port time series has no title")
    title_text = title.casefold()
    if dwell:
        if "dwell" not in title_text or "day" not in title_text:
            raise ValueError("port dwell endpoint has unexpected metric units")
    elif "container volume" not in title_text or "teu" not in title_text:
        raise ValueError("port traffic endpoint has unexpected metric units")
    if not isinstance(document.get("series"), list) or not isinstance(document.get("data"), list):
        raise ValueError("port response has no series/data arrays")
    if len(document["series"]) > 20 or len(document["data"]) > MAX_ROWS:
        raise ValueError("port response exceeds its cap")
    expected_codes = (
        {"Exports", "Imports", "Transshipments", "Rail imports"}
        if dwell
        else {"Imports", "Exports", "Transshipment"}
    )
    series_codes: set[str] = set()
    for series in document["series"]:
        if not isinstance(series, Mapping):
            raise ValueError("port series item is malformed")
        code, name, axis = series.get("code"), series.get("name"), series.get("axis")
        if not isinstance(code, str) or not isinstance(name, str):
            raise ValueError("port series code/name is malformed")
        if isinstance(axis, bool) or not isinstance(axis, int):
            raise ValueError("port series axis is malformed")
        series_codes.add(code)
    if not expected_codes.issubset(series_codes):
        raise ValueError("port response does not publish the documented series")
    result: dict[date_type, dict[str, float | None]] = {}
    fields = (
        ("Exports", "Imports", "Transshipments", "Rail imports")
        if dwell
        else ("Exports", "Imports", "Transshipment")
    )
    for item in document["data"]:
        if not isinstance(item, Mapping):
            raise ValueError("port data item is malformed")
        point_date = _parse_source_date(item.get("Date"))
        if point_date in result:
            raise ValueError("port series contains duplicate dates")
        point: dict[str, float | None] = {}
        for field_name in fields:
            if field_name not in item:
                raise ValueError("port data item is missing a documented value")
            point[field_name] = _number(item[field_name], optional=True)
        result[point_date] = point
    return result


def _whole_teu(value: float | None) -> int | None:
    if value is None:
        return None
    if not value.is_integer():
        raise ValueError("TEU values must be whole units for the standard integer field")
    return int(value)


class EconDBPortVolumeFetcher(EconDBAPIModelFetcher):
    """Join the documented per-port TEU and dwell-time time series by date."""

    canonical_model = "PortVolume"
    query_model = _PortVolumeQuery
    row_model = EconDBPortVolume
    capability = Capability(
        source=SOURCE,
        domain="econdb_port_volume",
        asset_class="macro",
        period="d",
        market="global",
        verified=False,
        notes="Official port roster, container-traffic TEU, and dwell-time API shapes.",
    )

    def extract_data(self, params: _PortVolumeQuery, ctx: FetchContext) -> object:
        """Fetch the port roster, traffic rows, and dwell-time rows."""
        port_code = quote(params.port_code, safe="")
        roster = self._get(f"{BASE_URL}/v1/ports/", ctx).document
        try:
            port_name, country_code = _port_metadata(roster, params.port_code)
        except ValueError:
            raise ProviderEngineError("ECONDB_SHAPE_INVALID") from None
        traffic = self._get(f"{BASE_URL}/v1/ports/{port_code}/container-traffic/", ctx).document
        dwell = self._get(f"{BASE_URL}/v1/ports/{port_code}/dwell-times/", ctx).document
        return {
            "port_name": port_name,
            "country_code": country_code,
            "traffic": traffic,
            "dwell": dwell,
        }

    def transform_data(self, raw: object, params: object) -> tuple[ContractModel, ...]:
        """Join traffic and dwell observations into typed port-volume rows."""
        if not isinstance(params, _PortVolumeQuery) or not isinstance(raw, Mapping):
            raise ProviderEngineError("ECONDB_SHAPE_INVALID")
        try:
            traffic = _port_points(raw.get("traffic"), dwell=False)
            dwell = _port_points(raw.get("dwell"), dwell=True)
            port_name = raw.get("port_name")
            country_code = raw.get("country_code")
            if port_name is not None and not isinstance(port_name, str):
                raise ValueError("port name is not text")
            if country_code is not None and not isinstance(country_code, str):
                raise ValueError("country code is not text")
            rows: list[dict[str, object]] = []
            for point_date in sorted(set(traffic) | set(dwell)):
                if params.start_date is not None and point_date < params.start_date:
                    continue
                if params.end_date is not None and point_date > params.end_date:
                    continue
                volume = traffic.get(point_date, {})
                wait = dwell.get(point_date, {})
                rows.append(
                    {
                        "date": point_date,
                        "port_code": params.port_code,
                        "port_name": port_name,
                        # The official port roster exposes ISO2, not a country name. Keep the
                        # standard descriptive field null and retain the exact ISO2 in country_code.
                        "country": None,
                        "country_code": country_code,
                        "export_dwell_time": wait.get("Exports"),
                        "import_dwell_time": wait.get("Imports"),
                        "import_teu": _whole_teu(volume.get("Imports")),
                        "export_teu": _whole_teu(volume.get("Exports")),
                        "transshipment_teu": volume.get("Transshipment"),
                    }
                )
            return self._rows(rows[-params.limit :])
        except (TypeError, ValueError, ValidationError):
            raise ProviderEngineError("ECONDB_SHAPE_INVALID") from None


class EconDBYieldCurveFetcher(EconDBAPIModelFetcher):
    """Build a bounded curve from explicit, metadata-verified EconDB indicator series."""

    canonical_model = "YieldCurve"
    query_model = _YieldCurveQuery
    row_model = EconDBYieldCurve
    capability = Capability(
        source=SOURCE,
        domain="econdb_yield_curve",
        asset_class="macro",
        period="d",
        market="global",
        verified=False,
        notes="Caller maps maturity labels to documented series IDs; curve units are preserved.",
    )

    def extract_data(self, params: _YieldCurveQuery, ctx: FetchContext) -> object:
        """Fetch each explicitly mapped series in request order."""
        return [
            self._get(f"{BASE_URL}/v1/series/{quote(symbol, safe='')}/", ctx).document
            for symbol in params.series_ids
        ]

    def transform_data(self, raw: object, params: object) -> tuple[ContractModel, ...]:
        """Validate mapped series and emit their latest shared observation date."""
        if not isinstance(params, _YieldCurveQuery) or not isinstance(raw, list):
            raise ProviderEngineError("ECONDB_SHAPE_INVALID")
        if len(raw) != len(params.series_ids):
            raise ProviderEngineError("ECONDB_SHAPE_INVALID")
        try:
            series_items: list[_SeriesDocument] = []
            for symbol, document in zip(params.series_ids, raw, strict=True):
                series = _series_document(document, symbol)
                _check_country(series.country, params.country)
                description = series.description.casefold()
                if "yield" not in description and "treasury" not in description:
                    raise ProviderEngineError("ECONDB_SERIES_SEMANTICS_INVALID")
                if series.units is None or not any(
                    marker in series.units.casefold() for marker in ("%", "percent", "percentage")
                ):
                    raise ProviderEngineError("ECONDB_SERIES_SEMANTICS_INVALID")
                series_items.append(series)
            point_maps = [dict(item.points) for item in series_items]
            common_dates = set.intersection(
                *(
                    {point_date for point_date, value in item.points if value is not None}
                    for item in series_items
                )
            )
            if not common_dates:
                raise ProviderEngineError("ECONDB_NO_COMMON_OBSERVATION")
            query_date = params.date
            if query_date is None:
                observation_date = max(common_dates)
            else:
                observation_date = min(
                    common_dates,
                    key=lambda item: (abs((item - query_date).days), -item.toordinal()),
                )
            rows = []
            for maturity, series, points in zip(
                params.maturities, series_items, point_maps, strict=True
            ):
                value = points[observation_date]
                if value is None:
                    raise ValueError("common curve date has a missing rate")
                rows.append(
                    {
                        "date": observation_date,
                        "maturity": maturity,
                        "value": value,
                        "symbol": series.symbol,
                        "country": series.country,
                        "units": series.units,
                        "description": series.description,
                    }
                )
            return self._rows(rows)
        except (TypeError, ValueError, ValidationError):
            raise ProviderEngineError("ECONDB_SHAPE_INVALID") from None


__all__ = [
    "EconDBEconomicIndicatorsFetcher",
    "EconDBGdpNominalFetcher",
    "EconDBGdpRealFetcher",
    "EconDBPortVolumeFetcher",
    "EconDBYieldCurveFetcher",
]
