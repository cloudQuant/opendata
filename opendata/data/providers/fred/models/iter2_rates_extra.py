"""Strict FRED adapters for fixed-income and commodity reference models.

All reads use the shared, scope-aware FRED observations pager. Composite
outputs are assembled only from the explicit FRED series maps below; unknown
selectors fail validation instead of generating a guessed series identifier.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date as date_type
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, ClassVar, Literal, NoReturn, TypeVar

from pydantic import Field, ValidationInfo, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.models.economic_series import (  # noqa: TC001 - Pydantic resolves these aliases at runtime.
    FredFrequency,
    FredOutputType,
    FredTransformUnits,
)
from opendata.data.models.fred_rates_extra import (
    FredBondIndices,
    FredCommercialPaper,
    FredCommoditySpotPrices,
    FredEuroShortTermRate,
    FredHighQualityMarketCorporateBond,
    FredSpotRate,
    FredYieldCurve,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.fred.models import _client
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models._fixed_observation_pages import (
    FixedObservationPagerSpec,
    _check_json_depth_text,
    fetch_fixed_observation_pages,
    validate_fixed_observation_pages,
)
from opendata.data.providers.fred.models.series import FredSeriesQuery
from opendata.data.request_budget import (
    RequestAuthorizationError,
    RequestBudgetError,
    authorize_request,
    check_scope_liveness,
    current_request_scopes,
    validate_scope_grants,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from opendata.data.models.base import ContractModel

_DATE_TOKEN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_NUMBER_TOKEN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_SOURCE = "fred"
_HOST = "api.stlouisfed.org"
_URL = f"https://{_HOST}/fred/series/observations"
_RELEASE_URL = f"https://{_HOST}/fred/v2/release/observations"
_HQM_RELEASE_ID = 402
_HQM_RELEASE_NAME = "Corporate Bond Yield Curve"
_RELEASE_LIMIT = 500_000
_MAX_RELEASE_PAGES = 100
_MAX_RELEASE_OBSERVATIONS = 500_000
_MAX_PAGE_BODY_BYTES = 32 * 1024 * 1024
_MAX_TOTAL_BODY_BYTES = 256 * 1024 * 1024
_MAX_JSON_DEPTH = 64
_MAX_SERIES_PER_QUERY = 64
_DateSelector = date_type | list[date_type] | tuple[date_type, date_type] | None

_FREQUENCIES = frozenset(
    {
        "d",
        "w",
        "bw",
        "m",
        "q",
        "sa",
        "a",
        "wef",
        "weth",
        "wew",
        "wetu",
        "wem",
        "wesu",
        "wesa",
        "bwew",
        "bwem",
    }
)
_CURVE_TYPES = frozenset(
    {"nominal", "real", "breakeven", "treasury_minus_fed_funds", "corporate_spot", "corporate_par"}
)
_COMMODITY_UNITS = {
    "wti": ("DCOILWTICO", "Dollars per Barrel"),
    "brent": ("DCOILBRENTEU", "Dollars per Barrel"),
    "natural_gas": ("DHHNGSP", "Dollars per Million Btu"),
    "jet_fuel": ("DJFUELUSGULF", "Dollars per Gallon"),
    "propane": ("DPROPANEMBTX", "Dollars per Gallon"),
    "heating_oil": ("DHOILNYH", "Dollars per Gallon"),
    "diesel_gulf_coast": ("DDFUELUSGULF", "Dollars per Gallon"),
    "diesel_ny_harbor": ("DDFUELNYH", "Dollars per Gallon"),
    "diesel_la": ("DDFUELLA", "Dollars per Gallon"),
    "gasoline_ny_harbor": ("DGASNYH", "Dollars per Gallon"),
    "gasoline_gulf_coast": ("DGASUSGULF", "Dollars per Gallon"),
    "rbob": ("DRGASLA", "Dollars per Gallon"),
}
_ESTR_SERIES = {
    "rate": "ECBESTRVOLWGTTRMDMNRT",
    "percentile_25": "ECBESTRRT25THPCTVOL",
    "percentile_75": "ECBESTRRT75THPCTVOL",
    "volume": "ECBESTRTOTVOL",
    "transactions": "ECBESTRNUMTRANS",
    "number_of_banks": "ECBESTRNUMACTBANKS",
    "large_bank_share_of_volume": "ECBESTRSHRVOL5LRGACTBNK",
}
_CP_MATURITY = {
    "overnight": "01",
    "7d": "07",
    "15d": "15",
    "30d": "30",
    "60d": "60",
    "90d": "90",
}
_CP_CATEGORIES = {
    "asset_backed": "AAAD",
    "financial": "FAAD",
    "nonfinancial": "NAAD",
    "a2p2": "NA2P2D",
}


def _decimal_key(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


_HQM_SPOT_GRID = {
    _decimal_key(Decimal(index) / Decimal(2)): f"{_decimal_key(Decimal(index) / Decimal(2))}-Year"
    for index in range(1, 201)
}
_HQM_PAR_GRID = {key: f"{key}-Year" for key in ("2", "5", "10", "30")}
_BOND_INDEX_SERIES = {
    "BAMLC1A0C13YEY": (
        "1-3 Year",
        "ICE BofA 1-3 Year US Corporate Index Effective Yield",
    ),
    "BAMLC2A0C35YEY": (
        "3-5 Year",
        "ICE BofA 3-5 Year US Corporate Index Effective Yield",
    ),
    "BAMLC3A0C57YEY": (
        "5-7 Year",
        "ICE BofA 5-7 Year US Corporate Index Effective Yield",
    ),
    "BAMLC4A0C710YEY": (
        "7-10 Year",
        "ICE BofA 7-10 Year US Corporate Index Effective Yield",
    ),
    "BAMLC7A0C1015YEY": (
        "10-15 Year",
        "ICE BofA 10-15 Year US Corporate Index Effective Yield",
    ),
    "BAMLC8A0C15PYEY": (
        "15+ Year",
        "ICE BofA 15+ Year US Corporate Index Effective Yield",
    ),
}
_NOMINAL_CURVE = {
    "1-Month": "DGS1MO",
    "2-Month": "DGS2MO",
    "3-Month": "DGS3MO",
    "4-Month": "DGS4MO",
    "6-Month": "DGS6MO",
    "1-Year": "DGS1",
    "2-Year": "DGS2",
    "3-Year": "DGS3",
    "5-Year": "DGS5",
    "7-Year": "DGS7",
    "10-Year": "DGS10",
    "20-Year": "DGS20",
    "30-Year": "DGS30",
}
_REAL_CURVE = {
    "5-Year": "DFII5",
    "7-Year": "DFII7",
    "10-Year": "DFII10",
    "20-Year": "DFII20",
    "30-Year": "DFII30",
}
_BREAKEVEN_CURVE = {
    "5-Year": "T5YIE",
    "7-Year": "T7YIEM",
    "10-Year": "T10YIE",
    "20-Year": "T20YIEM",
    "30-Year": "T30YIEM",
}
_TREASURY_MINUS_FED_FUNDS_CURVE = {
    "3-Month": "T3MFF",
    "6-Month": "T6MFF",
    "1-Year": "T1YFF",
    "5-Year": "T5YFF",
    "10-Year": "T10YFF",
}


def _parse_date(value: object) -> date_type:
    if type(value) is date_type:
        return value
    if isinstance(value, datetime):
        raise ValueError("date values must not include a time")
    if type(value) is not str or _DATE_TOKEN.fullmatch(value) is None:
        raise ValueError("date values must be date objects or canonical YYYY-MM-DD strings")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date values must be valid YYYY-MM-DD dates") from exc
    if parsed.isoformat() != value:
        raise ValueError("date values must be canonical YYYY-MM-DD strings")
    return parsed


class FredRateQuery(QueryParams):
    """Shared strict query fields exposed by the new rate/curve fetchers."""

    source: Literal["auto", "fred"] = "auto"
    market: Literal["us", "eu"] | None = None
    symbol: None = None
    transform: FredTransformUnits | None = None
    output_type: FredOutputType = 1
    realtime_start: date_type | None = None
    realtime_end: date_type | None = None
    as_of: date_type | None = None
    vintage_dates: list[date_type] | None = None
    frequency: FredFrequency | None = None
    aggregation_method: Literal["avg", "sum", "eop"] = "avg"
    sort_order: Literal["asc", "desc"] = "asc"
    page_size: int = Field(default=100_000, ge=1, le=100_000, strict=True)
    offset: int = Field(default=0, ge=0, strict=True)
    max_records: int = Field(default=200_000, ge=1, le=1_000_000, strict=True)
    max_pages: int = Field(default=2, ge=1, le=100, strict=True)

    @field_validator(
        "start_date", "end_date", "realtime_start", "realtime_end", "as_of", mode="before"
    )
    @classmethod
    def _validate_dates(cls, value: object) -> date_type | None:
        return None if value is None else _parse_date(value)

    @field_validator("output_type", mode="before")
    @classmethod
    def _validate_output_type(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("output_type must be an integer from 1 through 4")
        return value

    @field_validator("vintage_dates", mode="before")
    @classmethod
    def _validate_vintage_dates(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, (list, tuple)) or not value or len(value) > 2000:
            raise ValueError("vintage_dates must contain between 1 and 2000 ordered dates")
        return [_parse_date(item) for item in value]

    @model_validator(mode="after")
    def _validate_date_windows(self) -> FredRateQuery:
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if self.realtime_start and self.realtime_end and self.realtime_start > self.realtime_end:
            raise ValueError("realtime_start must not be after realtime_end")
        if self.as_of is not None and (
            self.realtime_start is not None or self.realtime_end is not None
        ):
            raise ValueError("as_of cannot be combined with a real-time window")
        if self.vintage_dates is not None and (
            self.as_of is not None
            or self.realtime_start is not None
            or self.realtime_end is not None
        ):
            raise ValueError("vintage_dates cannot be combined with another real-time selector")
        if self.frequency is not None and self.frequency not in _FREQUENCIES:
            raise ValueError("frequency is not supported by the FRED observations API")
        return self


class FredEuroShortTermRateQuery(FredRateQuery):
    """Query parameters for the official euro short-term rate series."""


class FredCommercialPaperQuery(FredRateQuery):
    """Select commercial-paper categories and maturities."""

    category: Literal["all", "asset_backed", "financial", "nonfinancial", "a2p2"] | list[str] = (
        "all"
    )
    maturity: Literal["all", "overnight", "7d", "15d", "30d", "60d", "90d"] | list[str] = "all"

    @field_validator("category", "maturity", mode="before")
    @classmethod
    def _validate_lists(cls, value: object, info: ValidationInfo) -> object:
        allowed = (
            {"all", *tuple(_CP_CATEGORIES)}
            if info.field_name == "category"
            else {"all", *tuple(_CP_MATURITY)}
        )
        if isinstance(value, str):
            if value not in allowed:
                raise ValueError(f"unsupported commercial-paper {info.field_name}")
            return value
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError(f"{info.field_name} must be a selector or nonempty list")
        items = tuple(value)
        if any(type(item) is not str or item not in allowed - {"all"} for item in items):
            raise ValueError(f"unsupported commercial-paper {info.field_name}")
        if len(items) != len(set(items)):
            raise ValueError(f"{info.field_name} must not contain duplicates")
        return list(items)


class FredSpotRateQuery(FredRateQuery):
    """Select mapped Treasury corporate spot or par curve points."""

    category: Literal["spot_rate", "par_yield"] | list[Literal["spot_rate", "par_yield"]] = (
        "spot_rate"
    )
    maturity: tuple[str, ...] = ("10",)

    @field_validator("category", mode="before")
    @classmethod
    def _validate_categories(cls, value: object) -> object:
        allowed = {"spot_rate", "par_yield"}
        if isinstance(value, str):
            if value not in allowed:
                raise ValueError("category must be spot_rate or par_yield")
            return value
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("category must select at least one rate curve")
        values = tuple(value)
        if any(type(item) is not str or item not in allowed for item in values):
            raise ValueError("category must contain only spot_rate or par_yield")
        if len(values) != len(set(values)):
            raise ValueError("category must not contain duplicates")
        return list(values)

    @field_validator("maturity", mode="before")
    @classmethod
    def _validate_maturity(cls, value: object) -> object:
        values = value if isinstance(value, (list, tuple)) else (value,)
        if not values or len(values) > len(_HQM_SPOT_GRID):
            raise ValueError("maturity must select one or more mapped maturities")
        normalized: list[str] = []
        for item in values:
            if isinstance(item, bool) or type(item) not in (str, int, float):
                raise ValueError("maturity values must be numeric years")
            try:
                number = Decimal(str(item))
            except InvalidOperation as exc:
                raise ValueError("maturity values must be numeric years") from exc
            key = _decimal_key(number)
            if not number.is_finite() or key not in _HQM_SPOT_GRID:
                raise ValueError("maturity must be on the official 0.5-to-100-year spot grid")
            normalized.append(key)
        if len(normalized) != len(set(normalized)):
            raise ValueError("maturity must not contain duplicates")
        return tuple(normalized)

    @model_validator(mode="after")
    def _reject_unsupported_release_parameters(self) -> FredSpotRateQuery:
        if (
            self.transform is not None
            or self.output_type != 1
            or self.realtime_start is not None
            or self.realtime_end is not None
            or self.as_of is not None
            or self.vintage_dates is not None
            or self.frequency is not None
            or self.aggregation_method != "avg"
            or self.offset != 0
        ):
            raise ValueError("spot rates support only date, maturity, and category selectors")
        return self


class FredHighQualityMarketCorporateBondQuery(FredRateQuery):
    """Select mapped HQM corporate bond curve maturities and dates."""

    yield_curve: Literal["spot", "par"] = "spot"
    maturities: tuple[str, ...] | None = None
    date: date_type | list[date_type] | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _validate_date_selector(cls, value: object) -> object:
        if value is None:
            return None
        values = value if isinstance(value, (list, tuple)) else (value,)
        if not values or len(values) > 1000:
            raise ValueError("date selector must contain between 1 and 1000 dates")
        parsed = [_parse_date(item) for item in values]
        if len(parsed) != len(set(parsed)):
            raise ValueError("date selector must not contain duplicates")
        return parsed if isinstance(value, (list, tuple)) else parsed[0]

    @field_validator("maturities", mode="before")
    @classmethod
    def _validate_maturities(cls, value: object) -> object:
        if value is None:
            return None
        values = value if isinstance(value, (list, tuple)) else (value,)
        if not values:
            raise ValueError("maturities must not be empty")
        normalized = []
        for item in values:
            if isinstance(item, bool) or type(item) not in (str, int, float):
                raise ValueError("maturities must be mapped numeric years")
            try:
                number = Decimal(str(item))
            except InvalidOperation as exc:
                raise ValueError("maturities must be mapped numeric years") from exc
            key = _decimal_key(number)
            if not number.is_finite() or key not in _HQM_SPOT_GRID:
                raise ValueError("maturity is outside the official 0.5-to-100-year curve grid")
            normalized.append(key)
        if len(normalized) != len(set(normalized)):
            raise ValueError("maturities must not contain duplicates")
        return tuple(normalized)

    @model_validator(mode="after")
    def _reject_unsupported_vintage_parameters(self) -> FredHighQualityMarketCorporateBondQuery:
        # The documented FRED HQM query exposes only date and yield_curve.
        # FRED v2 release observations returns the release's current snapshot;
        # it has no realtime/vintage or transformed-unit selectors.
        if (
            self.start_date is not None
            or self.end_date is not None
            or self.transform is not None
            or self.output_type != 1
            or self.realtime_start is not None
            or self.realtime_end is not None
            or self.as_of is not None
            or self.vintage_dates is not None
            or self.frequency is not None
            or self.aggregation_method != "avg"
            or self.offset != 0
        ):
            raise ValueError("HQM supports only its documented date and yield_curve selectors")
        return self


class FredBondIndicesQuery(FredRateQuery):
    """Query the supported U.S. bond-index selector family."""

    index_type: Literal["yield", "yield_to_worst", "total_return", "oas"] = "yield"
    category: str = "us"
    index: str | list[str] = "yield_curve"

    @field_validator("category")
    @classmethod
    def _validate_category(cls, value: str) -> str:
        if value != "us":
            raise ValueError("only the officially mapped US yield series are available")
        return value

    @field_validator("index", mode="before")
    @classmethod
    def _validate_index(cls, value: object) -> object:
        if value == "yield_curve":
            return "yield_curve"
        if isinstance(value, (list, tuple)) and tuple(value) == ("yield_curve",):
            return "yield_curve"
        raise ValueError("only the US yield_curve mapping is implemented")

    @field_validator("index_type")
    @classmethod
    def _only_yield_is_mapped(cls, value: str) -> str:
        if value != "yield":
            raise ValueError("only FRED Percent effective-yield series are mapped")
        return value


class FredCommoditySpotPricesQuery(FredRateQuery):
    """Select mapped energy commodity spot-price series."""

    commodity: str = "all"

    @field_validator("commodity")
    @classmethod
    def _validate_commodity(cls, value: str) -> str:
        if value != "all" and value not in _COMMODITY_UNITS:
            raise ValueError("commodity must be one of the documented FRED spot commodities")
        return value


class FredYieldCurveQuery(FredRateQuery):
    """Select a Treasury or HQM curve and target observation dates."""

    yield_curve_type: Literal[
        "nominal",
        "real",
        "breakeven",
        "treasury_minus_fed_funds",
        "corporate_spot",
        "corporate_par",
    ] = "nominal"
    date: date_type | list[date_type] | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _validate_date_selector(cls, value: object) -> object:
        if value is None:
            return None
        values = value if isinstance(value, (list, tuple)) else (value,)
        if not values or len(values) > 1000:
            raise ValueError("date selector must contain between 1 and 1000 dates")
        parsed = [_parse_date(item) for item in values]
        if len(parsed) != len(set(parsed)):
            raise ValueError("date selector must not contain duplicates")
        return parsed if isinstance(value, (list, tuple)) else parsed[0]

    @model_validator(mode="after")
    def _reject_unsupported_release_parameters(self) -> FredYieldCurveQuery:
        if self.yield_curve_type in {"corporate_spot", "corporate_par"} and (
            self.transform is not None
            or self.output_type != 1
            or self.realtime_start is not None
            or self.realtime_end is not None
            or self.as_of is not None
            or self.vintage_dates is not None
            or self.frequency is not None
            or self.aggregation_method != "avg"
            or self.offset != 0
        ):
            raise ValueError("corporate curves support only date and yield_curve_type selectors")
        return self


class FredRateObservationQuery(FredSeriesQuery):
    """Internal exact-series request consumed by the governed FRED pager."""

    source: Literal["fred"] = "fred"
    market: Literal["us", "eu"] | None = None
    symbol: None = None

    @field_validator("output_type", mode="before")
    @classmethod
    def _require_exact_output_type(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("output_type must be an integer from 1 through 4")
        return value

    @field_validator(
        "start_date", "end_date", "realtime_start", "realtime_end", "as_of", mode="before"
    )
    @classmethod
    def _strict_transport_dates(cls, value: object) -> date_type | None:
        return None if value is None else _parse_date(value)

    @field_validator("vintage_dates", mode="before")
    @classmethod
    def _strict_transport_vintages(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, (list, tuple)) or not value or len(value) > 2000:
            raise ValueError("vintage_dates must contain between 1 and 2000 dates")
        return [_parse_date(item) for item in value]


class _RateQueryLimitError(FredProviderError):
    def __init__(self, limit_kind: str, maximum: int) -> None:
        super().__init__("FRED_QUERY_LIMIT", url=_URL)
        self.limit_kind = limit_kind
        self.maximum = maximum
        self.partial = False


def _raise_query_limit(limit_kind: str, maximum: int) -> NoReturn:
    raise _RateQueryLimitError(limit_kind, maximum) from None


def _pager_spec(canonical_model: str) -> FixedObservationPagerSpec[FredRateObservationQuery]:
    return FixedObservationPagerSpec(
        query_type=FredRateObservationQuery,
        source=_SOURCE,
        canonical_model=canonical_model,
        host=_HOST,
        url=_URL,
        max_page_body_bytes=_MAX_PAGE_BODY_BYTES,
        max_total_body_bytes=_MAX_TOTAL_BODY_BYTES,
        max_json_depth=_MAX_JSON_DEPTH,
        get_http_client=get_shared_http_client,
        require_api_key=_client.require_api_key,
        json_loads=json.loads,
        raise_query_limit=_raise_query_limit,
    )


def _number(raw: str, *, nullable: bool) -> float | None:
    if raw == ".":
        if nullable:
            return None
        raise ValueError("non-null FRED model value is missing")
    if _NUMBER_TOKEN.fullmatch(raw) is None:
        raise ValueError("FRED values must be ASCII numeric tokens or '.'")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("FRED value must be finite")
    return value


def _integer(raw: str) -> int | None:
    if raw == ".":
        return None
    if _NUMBER_TOKEN.fullmatch(raw) is None:
        raise ValueError("FRED counts must be ASCII numeric tokens or '.'")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("FRED count is malformed") from exc
    if not value.is_finite() or value != value.to_integral_value():
        raise ValueError("FRED count must be an integral numeric value")
    return int(value)


def _selection_dates(value: _DateSelector) -> tuple[date_type, ...] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, tuple):
        return value
    return (value,)


def _selected_maturities(value: object, allowed: dict[str, tuple[str, str]]) -> tuple[str, ...]:
    if value is None:
        return tuple(allowed)
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple)):
        values = tuple(value)
    else:
        raise ValueError("maturities must be a mapped selector")
    keys = []
    for item in values:
        if isinstance(item, bool) or type(item) not in (str, int, float):
            raise ValueError("maturities must be numeric years")
        key = _decimal_key(Decimal(str(item)))
        if key not in allowed:
            raise ValueError("maturity is not in the explicitly mapped FRED subset")
        keys.append(key)
    if len(keys) != len(set(keys)):
        raise ValueError("maturities must not contain duplicates")
    return tuple(keys)


def _query_model_dump(query: FredRateQuery) -> dict[str, object]:
    return query.model_dump(mode="python")


def _as_series_query(
    query: FredRateQuery,
    *,
    series_id: str,
    market: Literal["us", "eu"],
    extra_date_selector: _DateSelector = None,
) -> FredRateObservationQuery:
    """Translate validated public selectors into one exact FRED series call."""
    start = query.start_date
    end = query.end_date
    if extra_date_selector is not None:
        dates = _selection_dates(extra_date_selector)
        if dates:
            date_start, date_end = min(dates), max(dates)
            start = date_start if start is None else max(start, date_start)
            end = date_end if end is None else min(end, date_end)
            if start > end:
                raise ValueError("date selector does not intersect the requested date window")
    payload: dict[str, object] = {
        "series_id": series_id,
        "source": "fred",
        "market": market,
        "start_date": start,
        "end_date": end,
        "transform_units": query.transform or "lin",
        "output_type": query.output_type,
        "realtime_start": query.realtime_start,
        "realtime_end": query.realtime_end,
        "as_of": query.as_of,
        "vintage_dates": query.vintage_dates,
        "frequency": query.frequency,
        "aggregation_method": query.aggregation_method,
        "sort_order": query.sort_order,
        "page_size": query.page_size,
        "offset": query.offset,
        "max_records": query.max_records,
        "max_pages": query.max_pages,
    }
    return FredRateObservationQuery.model_validate(payload)


QueryT = TypeVar("QueryT", bound=FredRateQuery)


class _RateExtraFetcher(Fetcher[QueryT, tuple[tuple[str, tuple[dict[str, Any], ...]], ...]]):
    """Three-stage strict base for the fixed FRED rate model family."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = None
    expected_market: ClassVar[Literal["us", "eu"]] = "us"
    capability: ClassVar[Capability]

    def transform_query(self, **kwargs: object) -> QueryT:
        query = self._query_type().model_validate(kwargs)
        if query.source not in {"auto", "fred"} or query.market not in (None, self.expected_market):
            raise ValueError("FRED source or market does not match this model")
        self._series_ids(query)
        selector = self._date_selector(query)
        dates = _selection_dates(selector)
        if dates:
            if query.start_date is not None and query.start_date > max(dates):
                raise ValueError("date selector does not intersect the requested date window")
            if query.end_date is not None and query.end_date < min(dates):
                raise ValueError("date selector does not intersect the requested date window")
        return query

    def extract_data(
        self,
        params: QueryT,
        ctx: FetchContext,
    ) -> tuple[tuple[str, tuple[dict[str, Any], ...]], ...]:
        query = self._checked_query(params)
        ids = self._series_ids(query)
        if len(ids) > _MAX_SERIES_PER_QUERY:
            raise FredProviderError("FRED_QUERY_LIMIT", url=_URL)
        spec = _pager_spec(self.canonical_model or "")
        output: list[tuple[str, tuple[dict[str, Any], ...]]] = []
        try:
            for series_id in ids:
                transport_query = _as_series_query(
                    query,
                    series_id=series_id,
                    market=self.expected_market,
                    extra_date_selector=self._date_selector(query),
                )

                def validate_page_tuple(
                    page_tuple: tuple[dict[str, Any], ...],
                    validated: FredRateObservationQuery,
                ) -> object:
                    return validate_fixed_observation_pages(
                        page_tuple,
                        validated,
                        spec=spec,
                    )

                pages = fetch_fixed_observation_pages(
                    transport_query,
                    spec=spec,
                    timeout=ctx.timeout,
                    final_validate=validate_page_tuple,
                )
                if type(pages) is not tuple or any(type(page) is not dict for page in pages):
                    raise FredProviderError("FRED_BAD_OBSERVATION", url=_URL)
                output.append((series_id, pages))
        except (FredProviderError, RequestBudgetError):
            raise
        except Exception:
            raise FredProviderError("FRED_BAD_OBSERVATION", url=_URL) from None
        return tuple(output)

    def transform_data(
        self,
        raw: tuple[tuple[str, tuple[dict[str, Any], ...]], ...],
        params: QueryT,
    ) -> FetchResult:
        query = self._checked_query(params)
        try:
            expected_ids = self._series_ids(query)
            if (
                type(raw) is not tuple
                or tuple(item[0] for item in raw) != expected_ids
                or any(type(item) is not tuple or type(item[1]) is not tuple for item in raw)
            ):
                raise ValueError("raw source series set/order differs from the query")
            spec = _pager_spec(self.canonical_model or "")
            rows: dict[str, dict[str, dict[str, str]]] = {}
            for series_id, pages in raw:
                transport_query = _as_series_query(
                    query,
                    series_id=series_id,
                    market=self.expected_market,
                    extra_date_selector=self._date_selector(query),
                )
                observations = validate_fixed_observation_pages(pages, transport_query, spec=spec)
                series_rows: dict[str, dict[str, str]] = {}
                for observation in observations:
                    prior = series_rows.get(observation["date"])
                    if prior is not None and prior != observation:
                        raise ValueError("same series/date has conflicting values")
                    series_rows[observation["date"]] = observation
                rows[series_id] = series_rows
            return self._normalize(query, rows)
        except Exception:
            raise FredProviderError("FRED_BAD_OBSERVATION", url=_URL) from None

    def _checked_query(self, value: object) -> QueryT:
        query_type = self._query_type()
        if not isinstance(value, FredRateQuery) or type(value) is not query_type:
            raise FredProviderError("FRED_BAD_QUERY", url=_URL)
        try:
            query = query_type.model_validate(value.model_dump(mode="python"), strict=True)
            if query.source not in {"auto", "fred"} or query.market not in (
                None,
                self.expected_market,
            ):
                raise ValueError
            self._series_ids(query)
            selector = self._date_selector(query)
            dates = _selection_dates(selector)
            if dates and (
                (query.start_date is not None and query.start_date > max(dates))
                or (query.end_date is not None and query.end_date < min(dates))
            ):
                raise ValueError
        except Exception:
            raise FredProviderError("FRED_BAD_QUERY", url=_URL) from None
        return query

    def _query_type(self) -> type[QueryT]:
        raise NotImplementedError

    def _date_selector(self, query: QueryT) -> _DateSelector:
        return None

    def _series_ids(self, query: QueryT) -> tuple[str, ...]:
        raise NotImplementedError

    def _normalize(
        self,
        query: QueryT,
        rows: dict[str, dict[str, dict[str, str]]],
    ) -> tuple[ContractModel, ...]:
        raise NotImplementedError


class EuroShortTermRateFetcher(_RateExtraFetcher[FredEuroShortTermRateQuery]):
    """The public wide €STR contract sourced from seven ECB FRED series."""

    canonical_model: ClassVar[str | None] = "EuroShortTermRate"
    expected_market: ClassVar[Literal["eu"]] = "eu"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="euro_short_term_rate",
        period="daily",
        market="eu",
        source="fred",
        verified=False,
    )

    def _query_type(self) -> type[FredEuroShortTermRateQuery]:
        return FredEuroShortTermRateQuery

    def _series_ids(self, query: FredEuroShortTermRateQuery) -> tuple[str, ...]:
        del query
        return tuple(_ESTR_SERIES.values())

    def _normalize(
        self, query: FredEuroShortTermRateQuery, rows: dict[str, dict[str, dict[str, str]]]
    ) -> tuple[FredEuroShortTermRate, ...]:
        field_by_series = {series_id: field for field, series_id in _ESTR_SERIES.items()}
        dates = sorted(
            {day for series in rows.values() for day in series}, reverse=query.sort_order == "desc"
        )
        if len(dates) > query.max_records:
            raise ValueError("wide output exceeds max_records")
        result = []
        for day in dates:
            record: dict[str, object] = {"date": day}
            for series_id, field in field_by_series.items():
                observation = rows[series_id].get(day)
                raw = None if observation is None else observation["value"]
                if field in {"transactions", "number_of_banks"}:
                    record[field] = None if raw is None else _integer(raw)
                else:
                    record[field] = None if raw is None else _number(raw, nullable=field != "rate")
            result.append(FredEuroShortTermRate.model_validate(record))
        return tuple(result)


class CommercialPaperFetcher(_RateExtraFetcher[FredCommercialPaperQuery]):
    """Long commercial-paper observations assembled from official FRED IDs."""

    canonical_model: ClassVar[str | None] = "CommercialPaper"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="commercial_paper",
        period="daily",
        market="us",
        source="fred",
        verified=False,
    )

    def _query_type(self) -> type[FredCommercialPaperQuery]:
        return FredCommercialPaperQuery

    def _series_specs(self, query: FredCommercialPaperQuery) -> dict[str, tuple[str, str]]:
        categories = (
            tuple(_CP_CATEGORIES)
            if query.category == "all"
            else ((query.category,) if isinstance(query.category, str) else tuple(query.category))
        )
        maturities = (
            tuple(_CP_MATURITY)
            if query.maturity == "all"
            else ((query.maturity,) if isinstance(query.maturity, str) else tuple(query.maturity))
        )
        return {
            "RIFSPP" + _CP_CATEGORIES[category] + _CP_MATURITY[maturity] + "NB": (
                category,
                maturity,
            )
            for category in categories
            for maturity in maturities
        }

    def _series_ids(self, query: FredCommercialPaperQuery) -> tuple[str, ...]:
        return tuple(self._series_specs(query))

    def _normalize(
        self, query: FredCommercialPaperQuery, rows: dict[str, dict[str, dict[str, str]]]
    ) -> tuple[FredCommercialPaper, ...]:
        specs = self._series_specs(query)
        dates = sorted(
            {day for series in rows.values() for day in series}, reverse=query.sort_order == "desc"
        )
        output: list[FredCommercialPaper] = []
        for day in dates:
            for series_id, (category, maturity) in specs.items():
                observation = rows[series_id].get(day)
                if observation is None:
                    continue
                rate = _number(observation["value"], nullable=True)
                if rate is None:
                    continue
                output.append(
                    FredCommercialPaper.model_validate(
                        {
                            "date": day,
                            "symbol": series_id,
                            "maturity": maturity,
                            "rate": rate,
                            "title": None,
                            "asset_type": category,
                        }
                    )
                )
                if len(output) > query.max_records:
                    raise ValueError("long output exceeds max_records")
        return tuple(output)


class SpotRateFetcher(_RateExtraFetcher[FredSpotRateQuery]):
    """Corporate spot/par rates from the complete official HQM release."""

    canonical_model: ClassVar[str | None] = "SpotRate"
    capability: ClassVar[Capability] = Capability(
        asset_class="fixed_income",
        domain="spot_rate",
        period="monthly",
        market="us",
        source="fred",
        verified=False,
    )

    def _query_type(self) -> type[FredSpotRateQuery]:
        return FredSpotRateQuery

    def _series_specs(self, query: FredSpotRateQuery) -> dict[str, tuple[str, str]]:
        categories = (query.category,) if isinstance(query.category, str) else tuple(query.category)
        specs: dict[str, tuple[str, str]] = {}
        for category in categories:
            mapping = _HQM_SPOT_GRID if category == "spot_rate" else _HQM_PAR_GRID
            for maturity in query.maturity:
                if maturity not in mapping:
                    raise ValueError("category/maturity pair is not in the official HQM curve")
                label = mapping[maturity]
                specs[f"{category}|{label}"] = (category, label)
        return specs

    def _series_ids(self, query: FredSpotRateQuery) -> tuple[str, ...]:
        # These are internal identity labels from the requested official
        # release rows, not generated FRED series IDs.
        return tuple(self._series_specs(query))

    def extract_data(
        self,
        params: FredSpotRateQuery,
        ctx: FetchContext,
    ) -> tuple[tuple[str, tuple[dict[str, str], ...]], ...]:
        """Fetch the official HQM release rows for the selected curve points."""
        query = self._checked_query(params)
        curves = _fetch_hqm_release_rows(
            ctx,
            canonical_model=self.canonical_model or "",
            max_pages=query.max_pages,
        )
        specs = self._series_specs(query)
        return tuple(
            (identity, curves["spot" if category == "spot_rate" else "par"][label])
            for identity, (category, label) in specs.items()
        )

    def transform_data(
        self,
        raw: tuple[tuple[str, tuple[dict[str, Any], ...]], ...],
        params: FredSpotRateQuery,
    ) -> FetchResult:
        """Validate release identities and serialize selected spot/par rows."""
        query = self._checked_query(params)
        try:
            expected = self._series_ids(query)
            if (
                type(raw) is not tuple
                or tuple(item[0] for item in raw) != expected
                or any(type(item) is not tuple or type(item[1]) is not tuple for item in raw)
            ):
                raise ValueError("bulk-release maturity set/order differs from the query")
            normalized: dict[str, dict[str, dict[str, str]]] = {}
            for identity, observations in raw:
                values: dict[str, dict[str, str]] = {}
                for observation in observations:
                    if (
                        type(observation) is not dict
                        or set(observation) != {"date", "value"}
                        or type(observation.get("date")) is not str
                        or type(observation.get("value")) is not str
                    ):
                        raise ValueError("bulk-release observation shape changed")
                    day = _parse_date(observation["date"]).isoformat()
                    value = observation["value"]
                    if value != "." and _NUMBER_TOKEN.fullmatch(value) is None:
                        raise ValueError("bulk-release observation value is malformed")
                    if day in values:
                        raise ValueError("bulk-release maturity has duplicate dates")
                    values[day] = {"date": day, "value": value}
                normalized[identity] = values
            return self._normalize(query, normalized)
        except Exception:
            raise FredProviderError("FRED_BAD_OBSERVATION", url=_RELEASE_URL) from None

    def _normalize(
        self,
        query: FredSpotRateQuery,
        rows: dict[str, dict[str, dict[str, str]]],
    ) -> tuple[FredSpotRate, ...]:
        specs = self._series_specs(query)
        output = []
        for identity, (category, label) in specs.items():
            for day, observation in rows[identity].items():
                if query.start_date is not None and day < query.start_date.isoformat():
                    continue
                if query.end_date is not None and day > query.end_date.isoformat():
                    continue
                output.append(
                    FredSpotRate.model_validate(
                        {
                            "date": day,
                            "category": category,
                            "maturity": label,
                            "rate": _number(observation["value"], nullable=True),
                        }
                    )
                )
        output.sort(key=lambda row: _maturity_number(row.maturity))
        output.sort(key=lambda row: row.category)
        output.sort(key=lambda row: row.date, reverse=query.sort_order == "desc")
        if len(output) > query.max_records:
            raise ValueError("output exceeds max_records")
        return tuple(output)


def _maturity_number(value: str) -> float:
    token, _, unit = value.partition("-")
    number = float(token)
    if unit == "Month":
        return number / 12
    if unit == "Year":
        return number
    raise ValueError("maturity label must use Month or Year units")


def _sort_date_maturity(rows: list[Any], sort_order: str) -> None:
    """Sort by observation date, then maturity ascending within each date."""
    rows.sort(key=lambda row: _maturity_number(row.maturity))
    rows.sort(key=lambda row: row.date, reverse=sort_order == "desc")


def _release_unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _release_reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


def _hqm_title_identity(title: str) -> tuple[str, str] | None:
    """Extract the tenor and curve type from FRED release metadata titles."""
    year_match = re.search(r"(?P<tenor>[0-9]+(?:\.[0-9]+)?)\s*-\s*Year\b", title)
    month_match = re.search(r"(?P<months>[0-9]+)\s*-\s*Month\b", title)
    if year_match is not None:
        tenor = _decimal_key(Decimal(year_match.group("tenor")))
    elif month_match is not None:
        tenor = _decimal_key(Decimal(month_match.group("months")) / Decimal(12))
    else:
        return None
    lowered = title.casefold()
    if lowered.endswith("spot rate"):
        curve = "spot"
    elif lowered.endswith("par yield"):
        curve = "par"
    else:
        return None
    return curve, tenor


def _hqm_release_rows(
    pages: tuple[dict[str, object], ...],
) -> dict[str, dict[str, tuple[dict[str, str], ...]]]:
    """Validate FRED v2 release pages and return curve/tenor observation rows.

    FRED's release endpoint publishes series identity in response metadata,
    rather than accepting a series selector. The approved tenor labels are
    derived from each release series' official title and checked against the
    official 200-spot / 4-par table inventory.
    """
    expected_spot = set(_HQM_SPOT_GRID)
    expected_par = set(_HQM_PAR_GRID)
    by_curve: dict[str, dict[str, list[dict[str, str]]]] = {"spot": {}, "par": {}}
    ids_by_tenor: dict[tuple[str, str], str] = {}
    metadata_by_id: dict[str, tuple[str, ...]] = {}
    seen_observations: set[tuple[str, str]] = set()
    total_observations = 0
    release_fingerprint: tuple[int, str, str] | None = None

    for page in pages:
        release = page.get("release")
        if type(release) is not dict:
            raise ValueError("release metadata is missing")
        if (
            type(release.get("release_id")) is not int
            or release["release_id"] != _HQM_RELEASE_ID
            or release.get("name") != _HQM_RELEASE_NAME
            or type(release.get("url")) is not str
            or type(release.get("sources")) is not list
        ):
            raise ValueError("release identity differs from the reviewed HQM release")
        fingerprint = (
            release["release_id"],
            release["name"],
            release["url"],
        )
        if release_fingerprint is None:
            release_fingerprint = fingerprint
        elif release_fingerprint != fingerprint:
            raise ValueError("release identity changed between pages")

        series_list = page.get("series")
        if type(series_list) is not list:
            raise ValueError("release series must be an array")
        for series in series_list:
            if type(series) is not dict:
                raise ValueError("release series entry must be an object")
            series_id = series.get("series_id")
            title = series.get("title")
            observations = series.get("observations")
            if (
                type(series_id) is not str
                or re.fullmatch(r"[A-Z0-9]+", series_id) is None
                or type(title) is not str
                or type(observations) is not list
            ):
                raise ValueError("release series identity is malformed")
            documented_text_fields = (
                "title",
                "frequency",
                "units",
                "seasonal_adjustment",
                "last_updated",
                "copyright_id",
                "notes",
            )
            if any(type(series.get(field)) is not str for field in documented_text_fields):
                raise ValueError("release series metadata is incomplete")
            metadata = tuple(series[field] for field in documented_text_fields)
            prior_metadata = metadata_by_id.get(series_id)
            if prior_metadata is not None and prior_metadata != metadata:
                raise ValueError("release series metadata changed between pages")
            metadata_by_id[series_id] = metadata

            identity = _hqm_title_identity(title)
            if identity is None:
                continue
            curve, tenor = identity
            expected_tenors = expected_spot if curve == "spot" else expected_par
            if tenor not in expected_tenors:
                raise ValueError("release contains an unexpected HQM tenor")
            tenor_identity = (curve, tenor)
            prior_id = ids_by_tenor.get(tenor_identity)
            if prior_id is not None and prior_id != series_id:
                raise ValueError("more than one release series claims an HQM tenor")
            ids_by_tenor[tenor_identity] = series_id

            if series.get("frequency") != "Monthly" or series.get("units") != "Percent":
                raise ValueError("HQM source frequency or unit differs from the reviewed contract")
            by_curve[curve].setdefault(tenor, [])
            for observation in observations:
                if type(observation) is not dict or set(observation) != {"date", "value"}:
                    raise ValueError("release observation has an unexpected shape")
                day, raw_value = observation.get("date"), observation.get("value")
                if (
                    type(day) is not str
                    or _DATE_TOKEN.fullmatch(day) is None
                    or type(raw_value) is not str
                ):
                    raise ValueError("release observation fields are malformed")
                _parse_date(day)
                if raw_value != "." and _NUMBER_TOKEN.fullmatch(raw_value) is None:
                    raise ValueError("release observation value is not numeric or missing")
                observation_identity = (series_id, day)
                if observation_identity in seen_observations:
                    raise ValueError("release pagination repeated a series/date observation")
                seen_observations.add(observation_identity)
                total_observations += 1
                if total_observations > _MAX_RELEASE_OBSERVATIONS:
                    raise ValueError("release observation count exceeds the fixed bound")
                by_curve[curve].setdefault(tenor, []).append({"date": day, "value": raw_value})

    if release_fingerprint is None:
        raise ValueError("release pages are empty")
    if set(ids_by_tenor) != (
        {("spot", tenor) for tenor in expected_spot} | {("par", tenor) for tenor in expected_par}
    ):
        raise ValueError("release does not contain the complete 200-spot/4-par HQM inventory")
    return {
        curve: {
            f"{tenor}-Year": tuple(sorted(rows, key=lambda row: row["date"]))
            for tenor, rows in tenor_rows.items()
        }
        for curve, tenor_rows in by_curve.items()
    }


def _fetch_hqm_release_rows(
    ctx: FetchContext,
    *,
    canonical_model: str,
    max_pages: int,
) -> dict[str, dict[str, tuple[dict[str, str], ...]]]:
    """Read and validate the complete public HQM inventory under the caller's scope."""
    scopes = current_request_scopes()
    if not scopes:
        raise RequestAuthorizationError("FRED HQM requests require an authorized execution scope")
    if any(scope.source != _SOURCE or scope.canonical_model != canonical_model for scope in scopes):
        raise RequestAuthorizationError("FRED HQM request scope identity does not match")
    validate_scope_grants(scopes)
    authorize_request(scopes, _SOURCE, _HOST)
    api_key = _client.require_api_key()
    if type(api_key) is not str or not api_key.strip():
        raise FredProviderError("FRED_API_KEY_MISSING", url=_RELEASE_URL)

    pages: list[dict[str, object]] = []
    cursor: str | None = None
    cursors: set[str] = set()
    total_bytes = 0
    for _page_index in range(min(max_pages, _MAX_RELEASE_PAGES)):
        check_scope_liveness(scopes)
        authorize_request(scopes, _SOURCE, _HOST)
        request_params = {
            "release_id": str(_HQM_RELEASE_ID),
            "format": "json",
            "limit": str(_RELEASE_LIMIT),
        }
        if cursor is not None:
            request_params["next_cursor"] = cursor
        try:
            response = get_shared_http_client().get(
                _RELEASE_URL,
                params=request_params,
                headers={"Authorization": f"Bearer {api_key.strip()}"},
                timeout=ctx.timeout,
                source=_SOURCE,
            )
        except RequestBudgetError:
            raise
        except HttpFetchError as exc:
            raise FredProviderError("FRED_HTTP_ERROR", status=exc.status, url=_RELEASE_URL) from exc
        check_scope_liveness(scopes)
        status = response.status_code
        if type(status) is not int:
            raise FredProviderError("FRED_BAD_RESPONSE", url=_RELEASE_URL)
        if status != 200:
            raise FredProviderError("FRED_HTTP_ERROR", status=status, url=_RELEASE_URL)
        body = response.content
        if type(body) is not bytes:
            raise FredProviderError("FRED_BAD_RESPONSE", status=status, url=_RELEASE_URL)
        if len(body) > _MAX_PAGE_BODY_BYTES or total_bytes + len(body) > _MAX_TOTAL_BODY_BYTES:
            raise FredProviderError("FRED_RESPONSE_LIMIT", status=status, url=_RELEASE_URL)
        total_bytes += len(body)
        try:
            text = body.decode("utf-8")
            _check_json_depth_text(text, maximum_depth=_MAX_JSON_DEPTH)
            document = json.loads(
                text,
                object_pairs_hook=_release_unique_object,
                parse_constant=_release_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
            raise FredProviderError("FRED_BAD_RESPONSE", status=status, url=_RELEASE_URL) from None
        if type(document) is not dict or not {"has_more", "release", "series"}.issubset(document):
            raise FredProviderError("FRED_BAD_RESPONSE", status=status, url=_RELEASE_URL)
        if document["has_more"] not in ("true", "false") or type(document["series"]) is not list:
            raise FredProviderError("FRED_BAD_RESPONSE", status=status, url=_RELEASE_URL)
        release = document["release"]
        if (
            type(release) is not dict
            or release.get("release_id") != _HQM_RELEASE_ID
            or release.get("name") != _HQM_RELEASE_NAME
        ):
            raise FredProviderError("FRED_BAD_RESPONSE", status=status, url=_RELEASE_URL)
        pages.append(document)
        check_scope_liveness(scopes)
        if document["has_more"] == "false":
            if "next_cursor" in document:
                raise FredProviderError("FRED_PAGINATION_ERROR", status=status, url=_RELEASE_URL)
            break
        next_cursor = document.get("next_cursor")
        if (
            type(next_cursor) is not str
            or not next_cursor.strip()
            or next_cursor == cursor
            or next_cursor in cursors
        ):
            raise FredProviderError("FRED_PAGINATION_ERROR", status=status, url=_RELEASE_URL)
        cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise FredProviderError("FRED_QUERY_LIMIT", url=_RELEASE_URL)

    try:
        return _hqm_release_rows(tuple(pages))
    except (TypeError, ValueError):
        raise FredProviderError("FRED_BAD_OBSERVATION", url=_RELEASE_URL) from None


class HighQualityMarketCorporateBondFetcher(
    _RateExtraFetcher[FredHighQualityMarketCorporateBondQuery]
):
    """Complete HQM curves from FRED's official bulk release endpoint."""

    canonical_model: ClassVar[str | None] = "HighQualityMarketCorporateBond"
    capability: ClassVar[Capability] = Capability(
        asset_class="fixed_income",
        domain="hqm_corporate_bond",
        period="monthly",
        market="us",
        source="fred",
        verified=False,
    )

    def _query_type(self) -> type[FredHighQualityMarketCorporateBondQuery]:
        return FredHighQualityMarketCorporateBondQuery

    def _series_ids(self, query: FredHighQualityMarketCorporateBondQuery) -> tuple[str, ...]:
        mapping = _HQM_SPOT_GRID if query.yield_curve == "spot" else _HQM_PAR_GRID
        selected = tuple(mapping) if query.maturities is None else query.maturities
        if any(key not in mapping for key in selected):
            raise ValueError("maturity is not in the selected official HQM curve")
        return tuple(mapping[key] for key in selected)

    def _date_selector(self, query: FredHighQualityMarketCorporateBondQuery) -> _DateSelector:
        return query.date

    def extract_data(
        self,
        params: FredHighQualityMarketCorporateBondQuery,
        ctx: FetchContext,
    ) -> tuple[tuple[str, tuple[dict[str, str], ...]], ...]:
        """Read the complete release through bounded FRED v2 cursor pages."""
        query = self._checked_query(params)
        curves = _fetch_hqm_release_rows(
            ctx,
            canonical_model=self.canonical_model or "",
            max_pages=query.max_pages,
        )
        selected_labels = self._series_ids(query)
        return tuple((label, curves[query.yield_curve][label]) for label in selected_labels)

    def transform_data(
        self,
        raw: tuple[tuple[str, tuple[dict[str, str], ...]], ...],
        params: FredHighQualityMarketCorporateBondQuery,
    ) -> FetchResult:
        """Validate HQM release rows and build complete selected curve outputs."""
        query = self._checked_query(params)
        try:
            expected_labels = self._series_ids(query)
            if (
                type(raw) is not tuple
                or tuple(item[0] for item in raw) != expected_labels
                or any(type(item) is not tuple or type(item[1]) is not tuple for item in raw)
            ):
                raise ValueError("bulk-release tenor set/order differs from the query")
            rows: dict[str, dict[str, dict[str, str]]] = {}
            for label, observations in raw:
                series: dict[str, dict[str, str]] = {}
                for observation in observations:
                    if (
                        type(observation) is not dict
                        or set(observation) != {"date", "value"}
                        or type(observation.get("date")) is not str
                        or type(observation.get("value")) is not str
                    ):
                        raise ValueError("bulk-release observation shape changed")
                    _parse_date(observation["date"])
                    value = observation["value"]
                    if value != "." and _NUMBER_TOKEN.fullmatch(value) is None:
                        raise ValueError("bulk-release value is malformed")
                    if observation["date"] in series:
                        raise ValueError("bulk-release contains duplicate dates")
                    series[observation["date"]] = dict(observation)
                rows[label] = series
            return self._normalize(query, rows)
        except Exception:
            raise FredProviderError("FRED_BAD_OBSERVATION", url=_RELEASE_URL) from None

    def _normalize(
        self,
        query: FredHighQualityMarketCorporateBondQuery,
        rows: dict[str, dict[str, dict[str, str]]],
    ) -> tuple[FredHighQualityMarketCorporateBond, ...]:
        selected_dates = _selection_dates(query.date)
        dates = sorted({day for series in rows.values() for day in series})
        if selected_dates is not None:
            selected_set = {item.isoformat() for item in selected_dates}
            dates = [day for day in dates if day in selected_set]
        if query.sort_order == "desc":
            dates.reverse()
        output = []
        for day in dates:
            daily_values: list[tuple[str, float]] = []
            for label, observations in rows.items():
                observation = observations.get(day)
                if observation is None or observation["value"] == ".":
                    break
                value = _number(observation["value"], nullable=False)
                if value is None:
                    break
                daily_values.append((label, value))
            if len(daily_values) != len(rows):
                if selected_dates is not None:
                    raise ValueError("requested HQM date does not have a complete tenor curve")
                continue
            daily_values.sort(key=lambda item: float(item[0].split("-", 1)[0]))
            for label, value in daily_values:
                output.append(
                    FredHighQualityMarketCorporateBond.model_validate(
                        {
                            "date": day,
                            "yield_curve": query.yield_curve,
                            "maturity": label,
                            "rate": value,
                        }
                    )
                )
        if len(output) > query.max_records:
            raise ValueError("output exceeds max_records")
        return tuple(output)


class BondIndicesFetcher(_RateExtraFetcher[FredBondIndicesQuery]):
    """Long public bond-index rows for the source-verified selector subset."""

    canonical_model: ClassVar[str | None] = "BondIndices"
    capability: ClassVar[Capability] = Capability(
        asset_class="fixed_income",
        domain="bond_indices",
        period="daily",
        market="us",
        source="fred",
        verified=False,
    )

    def _query_type(self) -> type[FredBondIndicesQuery]:
        return FredBondIndicesQuery

    def _series_ids(self, query: FredBondIndicesQuery) -> tuple[str, ...]:
        del query
        return tuple(_BOND_INDEX_SERIES)

    def _normalize(
        self, query: FredBondIndicesQuery, rows: dict[str, dict[str, dict[str, str]]]
    ) -> tuple[FredBondIndices, ...]:
        dates = sorted(
            {day for series in rows.values() for day in series}, reverse=query.sort_order == "desc"
        )
        output: list[FredBondIndices] = []
        for day in dates:
            for series_id, (maturity, title) in _BOND_INDEX_SERIES.items():
                observation = rows[series_id].get(day)
                if observation is None:
                    continue
                value = _number(observation["value"], nullable=True)
                if value is None:
                    continue
                output.append(
                    FredBondIndices.model_validate(
                        {
                            "date": day,
                            "symbol": series_id,
                            "value": value,
                            "maturity": maturity,
                            "title": title,
                        }
                    )
                )
                if len(output) > query.max_records:
                    raise ValueError("long output exceeds max_records")
        return tuple(output)


class CommoditySpotPricesFetcher(_RateExtraFetcher[FredCommoditySpotPricesQuery]):
    """EIA-distributed commodity spot prices with explicit FRED source IDs."""

    canonical_model: ClassVar[str | None] = "CommoditySpotPrices"
    capability: ClassVar[Capability] = Capability(
        asset_class="commodities",
        domain="commodity_spot_prices",
        period="daily",
        market="us",
        source="fred",
        verified=False,
    )

    def _query_type(self) -> type[FredCommoditySpotPricesQuery]:
        return FredCommoditySpotPricesQuery

    def _series_specs(self, query: FredCommoditySpotPricesQuery) -> dict[str, tuple[str, str]]:
        commodities = tuple(_COMMODITY_UNITS) if query.commodity == "all" else (query.commodity,)
        return {name: _COMMODITY_UNITS[name] for name in commodities}

    def _series_ids(self, query: FredCommoditySpotPricesQuery) -> tuple[str, ...]:
        return tuple(item[0] for item in self._series_specs(query).values())

    def _normalize(
        self, query: FredCommoditySpotPricesQuery, rows: dict[str, dict[str, dict[str, str]]]
    ) -> tuple[FredCommoditySpotPrices, ...]:
        by_id = {
            series_id: (commodity, unit)
            for commodity, (series_id, unit) in self._series_specs(query).items()
        }
        output = []
        for series_id, (commodity, unit) in by_id.items():
            for day, observation in rows[series_id].items():
                # The public data model has a required price; FRED's documented '.'
                # sentinel denotes no price observation and is therefore omitted.
                price = _number(observation["value"], nullable=True)
                if price is None:
                    continue
                output.append(
                    FredCommoditySpotPrices.model_validate(
                        {
                            "date": day,
                            "symbol": commodity,
                            "commodity": commodity,
                            "price": price,
                            "unit": unit,
                        }
                    )
                )
        output.sort(key=lambda row: row.commodity or "")
        output.sort(key=lambda row: row.date, reverse=query.sort_order == "desc")
        if len(output) > query.max_records:
            raise ValueError("output exceeds max_records")
        return tuple(output)


class YieldCurveFetcher(_RateExtraFetcher[FredYieldCurveQuery]):
    """FRED curves at the latest complete observation date no later than each target."""

    canonical_model: ClassVar[str | None] = "YieldCurve"
    capability: ClassVar[Capability] = Capability(
        asset_class="fixed_income",
        domain="fred_yield_curve",
        period="variable",
        market="us",
        source="fred",
        verified=False,
    )

    def _query_type(self) -> type[FredYieldCurveQuery]:
        return FredYieldCurveQuery

    def __init__(self, today_provider: Callable[[], date_type] = date_type.today) -> None:
        """Inject a date provider so default target selection is deterministic."""
        self._today_provider = today_provider

    def transform_query(self, **kwargs: object) -> FredYieldCurveQuery:
        """Fill an omitted date from the injected clock before validation."""
        if kwargs.get("date") is None:
            kwargs = {**kwargs, "date": self._today_provider()}
        return super().transform_query(**kwargs)

    def _series_specs(self, query: FredYieldCurveQuery) -> dict[str, str]:
        kind = query.yield_curve_type
        if kind == "nominal":
            return _NOMINAL_CURVE
        if kind == "real":
            return _REAL_CURVE
        if kind == "breakeven":
            return _BREAKEVEN_CURVE
        if kind == "treasury_minus_fed_funds":
            return _TREASURY_MINUS_FED_FUNDS_CURVE
        if kind == "corporate_spot":
            return {label: label for label in _HQM_SPOT_GRID.values()}
        if kind == "corporate_par":
            return {label: label for label in _HQM_PAR_GRID.values()}
        raise ValueError("yield curve type does not have an official FRED series map")

    def _series_ids(self, query: FredYieldCurveQuery) -> tuple[str, ...]:
        return tuple(self._series_specs(query).values())

    def _date_selector(self, query: FredYieldCurveQuery) -> tuple[date_type, date_type]:
        targets = _selection_dates(query.date)
        if targets is None:
            raise ValueError("the default yield-curve date must be injected before fetching")
        today = self._today_provider()
        if type(today) is not date_type:
            raise ValueError("today provider must return a date")
        effective_targets = tuple(min(target, today) for target in targets)
        target_start = min(effective_targets)
        target_end = max(effective_targets)
        lookback = 400
        search_start = target_start - timedelta(days=lookback)
        if query.start_date is not None:
            search_start = max(search_start, query.start_date)
        if query.end_date is not None:
            target_end = min(target_end, query.end_date)
        if search_start > target_end:
            raise ValueError(
                "requested yield-curve dates do not intersect the bounded history window"
            )
        return search_start, target_end

    def extract_data(
        self,
        params: FredYieldCurveQuery,
        ctx: FetchContext,
    ) -> tuple[tuple[str, tuple[dict[str, Any], ...]], ...]:
        """Fetch the selected Treasury series or the bounded HQM release."""
        query = self._checked_query(params)
        if query.yield_curve_type not in {"corporate_spot", "corporate_par"}:
            return super().extract_data(query, ctx)
        curve: Literal["spot", "par"] = (
            "spot" if query.yield_curve_type == "corporate_spot" else "par"
        )
        rows = _fetch_hqm_release_rows(
            ctx,
            canonical_model=self.canonical_model or "",
            max_pages=query.max_pages,
        )
        return tuple((label, rows[curve][label]) for label in self._series_ids(query))

    def transform_data(
        self,
        raw: tuple[tuple[str, tuple[dict[str, Any], ...]], ...],
        params: FredYieldCurveQuery,
    ) -> FetchResult:
        """Validate source rows and emit typed curve points for target dates."""
        query = self._checked_query(params)
        if query.yield_curve_type not in {"corporate_spot", "corporate_par"}:
            return super().transform_data(raw, query)
        try:
            expected_labels = self._series_ids(query)
            if (
                type(raw) is not tuple
                or tuple(item[0] for item in raw) != expected_labels
                or any(type(item) is not tuple or type(item[1]) is not tuple for item in raw)
            ):
                raise ValueError("bulk-release maturity set/order differs from the curve query")
            normalized: dict[str, dict[str, dict[str, str]]] = {}
            for label, observations in raw:
                values: dict[str, dict[str, str]] = {}
                for observation in observations:
                    if (
                        type(observation) is not dict
                        or set(observation) != {"date", "value"}
                        or type(observation.get("date")) is not str
                        or type(observation.get("value")) is not str
                    ):
                        raise ValueError("bulk-release observation shape changed")
                    day = _parse_date(observation["date"]).isoformat()
                    value = observation["value"]
                    if value != "." and _NUMBER_TOKEN.fullmatch(value) is None:
                        raise ValueError("bulk-release observation value is malformed")
                    if day in values:
                        raise ValueError("bulk-release maturity has duplicate dates")
                    values[day] = {"date": day, "value": value}
                normalized[label] = values
            return self._normalize(query, normalized)
        except Exception:
            raise FredProviderError("FRED_BAD_OBSERVATION", url=_RELEASE_URL) from None

    def _normalize(
        self, query: FredYieldCurveQuery, rows: dict[str, dict[str, dict[str, str]]]
    ) -> tuple[FredYieldCurve, ...]:
        series_to_maturity = {
            series_id: maturity for maturity, series_id in self._series_specs(query).items()
        }
        targets = _selection_dates(query.date)
        if targets is None:
            raise ValueError("yield curve query has no injected date")
        search_start, search_end = self._date_selector(query)
        complete_dates: set[str] | None = None
        numeric_by_series: dict[str, dict[str, float]] = {}
        for series_id in series_to_maturity:
            series_rows = rows.get(series_id)
            if type(series_rows) is not dict:
                raise ValueError("yield curve source is missing a maturity series")
            numeric: dict[str, float] = {}
            for day, observation in series_rows.items():
                if (
                    type(observation) is not dict
                    or not {"date", "value"}.issubset(observation)
                    or set(observation) - {"date", "value", "realtime_start", "realtime_end"}
                    or ({"realtime_start", "realtime_end"} & set(observation))
                    not in (set(), {"realtime_start", "realtime_end"})
                ):
                    raise ValueError("yield curve observation shape changed")
                value = _number(observation["value"], nullable=True)
                if value is not None:
                    numeric[day] = value
            numeric_by_series[series_id] = numeric
            series_dates = set(numeric)
            complete_dates = (
                series_dates if complete_dates is None else complete_dates & series_dates
            )

        available = {
            day
            for day in complete_dates or set()
            if search_start.isoformat() <= day <= search_end.isoformat()
        }
        selected_days: set[str] = set()
        for target in targets:
            effective_target = min(target, self._today_provider())
            lower = max(
                effective_target - timedelta(days=400),
                query.start_date or search_start,
                search_start,
            )
            upper = min(effective_target, search_end)
            eligible = [day for day in available if lower.isoformat() <= day <= upper.isoformat()]
            if not eligible:
                raise ValueError("no complete yield curve exists within the bounded lookback")
            selected_days.add(max(eligible))

        output = []
        for day in sorted(selected_days, reverse=query.sort_order == "desc"):
            for series_id, maturity in series_to_maturity.items():
                output.append(
                    FredYieldCurve.model_validate(
                        {
                            "date": day,
                            "yield_curve_type": query.yield_curve_type,
                            "maturity": maturity,
                            "rate": numeric_by_series[series_id][day],
                        }
                    )
                )
        _sort_date_maturity(output, query.sort_order)
        if len(output) > query.max_records:
            raise ValueError("output exceeds max_records")
        return tuple(output)


__all__ = (
    "BondIndicesFetcher",
    "CommercialPaperFetcher",
    "CommoditySpotPricesFetcher",
    "EuroShortTermRateFetcher",
    "FredBondIndicesQuery",
    "FredCommercialPaperQuery",
    "FredCommoditySpotPricesQuery",
    "FredEuroShortTermRateQuery",
    "FredHighQualityMarketCorporateBondQuery",
    "FredRateObservationQuery",
    "FredRateQuery",
    "FredSpotRateQuery",
    "FredYieldCurveQuery",
    "HighQualityMarketCorporateBondFetcher",
    "SpotRateFetcher",
    "YieldCurveFetcher",
)
