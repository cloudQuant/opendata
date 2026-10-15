"""Typed fetchers for the fixed IMF SDMX macro-model identities."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal

if TYPE_CHECKING:
    from datetime import date

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.macro_iter2 import (
    ImfAvailableIndicator,
    ImfConsumerPriceIndex,
    ImfDirectionOfTrade,
    ImfEconomicIndicator,
)
from opendata.data.protocol import (
    FetchContext,
    Fetcher,
    FetchResult,
    MultiSymbolQueryParams,
    QueryParams,
)
from opendata.data.providers.imf._source import SOURCE
from opendata.data.providers.imf.models import _sdmx
from opendata.data.providers.imf.models._sdmx import ImfSdmxError

CPI_FLOW = ("IMF.STA", "CPI", "5.0.0")
IMTS_FLOW = ("IMF.STA", "IMTS", "1.0.0")

_TOKEN = re.compile(r"^[A-Z0-9_]{1,48}$")
_FREQUENCIES = {
    "annual": "A",
    "yearly": "A",
    "quarter": "Q",
    "quarterly": "Q",
    "monthly": "M",
    "month": "M",
}
_TRANSFORMS = {
    "index": "IX",
    "period": "POP_PCH_PA_PT",
    "yoy": "YOY_PCH_PA_PT",
    "ref_index": "SRP_IX",
    "ref_period": "SRP_POP_PCH_PA_PT",
    "ref_yoy": "SRP_YOY_PCH_PA_PT",
    "weight": "WGT",
    "weight_percent": "WGT_PT",
}
_EXPENDITURES = {
    "total": "_T",
    "all": "_T",
    "food_non_alcoholic_beverages": "CP01",
    "alcoholic_beverages_tobacco_narcotics": "CP02",
    "clothing_footwear": "CP03",
    "housing_water_electricity_gas": "CP04",
    "furniture_household_equipment": "CP05",
    "health": "CP06",
    "transport": "CP07",
    "communication": "CP08",
    "recreation_culture": "CP09",
    "education": "CP10",
    "restaurants_hotels": "CP11",
    "miscellaneous_goods_services": "CP12",
}
_DOT_INDICATORS = {
    "exports": "XG_FOB_USD",
    "imports": "MG_CIF_USD",
    "balance": "TBG_USD",
}
_CURRENCY_CODES = {"XG_FOB_USD": "USD", "MG_FOB_USD": "USD", "MG_CIF_USD": "USD", "TBG_USD": "USD"}
_COUNTRY_TEXT = re.compile(r"^[A-Za-z][A-Za-z .,'’()/-]{0,79}$")


def _window(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and start > end:
        raise ValueError("start_date must not be after end_date")


def _as_code(value: object) -> str:
    token = str(value).strip().upper()
    if token in {"ALL", "*"}:
        return ""
    if not _TOKEN.fullmatch(token):
        raise ValueError("IMF SDMX codes must use letters, digits, and underscores")
    return token


def _code_list(value: str | list[str] | None) -> tuple[str, ...]:
    if value is None:
        return ("",)
    raw = value if isinstance(value, list) else value.split(",")
    if not raw or any(not isinstance(item, str) or not item.strip() for item in raw):
        raise ValueError("code filters must contain nonblank values")
    codes = tuple(_as_code(item) for item in raw)
    if "" in codes and len(codes) > 1:
        raise ValueError("the all selector cannot be combined with explicit codes")
    if len(codes) != len(set(codes)):
        raise ValueError("duplicate IMF SDMX codes are not allowed")
    return codes


def _country_filter(value: str | list[str] | None) -> tuple[str, ...]:
    """Accept IMF codes or human-readable names while blocking key injection."""
    if value is None:
        return ("",)
    raw = value if isinstance(value, list) else value.split(",")
    if not raw or any(not isinstance(item, str) or not item.strip() for item in raw):
        raise ValueError("country filters must contain nonblank names or IMF codes")
    selected: list[str] = []
    for item in raw:
        token = item.strip()
        if token.upper() in {"ALL", "*"}:
            selected.append("")
        elif _TOKEN.fullmatch(token.upper()):
            selected.append(token.upper())
        elif _COUNTRY_TEXT.fullmatch(token):
            selected.append(token)
        else:
            raise ValueError("country filter must be a name or IMF SDMX code")
    if "" in selected and len(selected) != 1:
        raise ValueError("the all-country selector cannot be combined with named countries")
    if len({item.casefold() for item in selected}) != len(selected):
        raise ValueError("country filters cannot repeat values")
    return tuple(selected)


def _resolve_country_filter(values: tuple[str, ...], labels: dict[str, str]) -> tuple[str, ...]:
    """Resolve names against official metadata and reject unknown IMF codes."""
    if values == ("",):
        return values
    inverse = {label.casefold(): code for code, label in labels.items()}
    result: list[str] = []
    for value in values:
        if _TOKEN.fullmatch(value):
            if value in labels:
                result.append(value)
            elif value.casefold() in inverse:
                # A human-readable single-token label (for example "China")
                # also matches the permissive SDMX token grammar after query
                # normalization. Prefer the official label when no such code
                # exists in this flow's codelist.
                result.append(inverse[value.casefold()])
            elif labels:
                raise ValueError("country code is not published by the selected IMF dataflow")
            else:
                result.append(value)
        elif value.casefold() in inverse:
            result.append(inverse[value.casefold()])
        else:
            raise ValueError("country name is not published by the selected IMF dataflow")
    return tuple(result)


def _key_values(values: tuple[str, ...]) -> str:
    return "+".join(values)


def _label(labels: dict[str, str], code: str, fallback: str | None = None) -> str:
    return labels.get(code, fallback or code)


def _finite_value(value: object, *, nullable: bool) -> float | None:
    text = "" if value is None else str(value).strip()
    if text in {"", "..", "NA", "N/A"}:
        if nullable:
            return None
        raise ValueError("required IMF observation is missing")
    parsed = float(text)
    if not math.isfinite(parsed):
        raise ValueError("IMF observation must be finite")
    return parsed


def _numeric(value: object, *, integer: bool = False) -> int | float | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    parsed = float(text)
    if not math.isfinite(parsed):
        raise ValueError("IMF metadata numeric field must be finite")
    return int(parsed) if integer and parsed.is_integer() else parsed


def _integer_value(value: int | float | None, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if value.is_integer():
        return int(value)
    raise ValueError(f"IMF {field} must be an integer")


def _power_of_ten(exponent: int) -> int | float:
    if exponent < 0:
        fractional_multiplier: float = 10**exponent
        return fractional_multiplier
    integral_multiplier: int = 10**exponent
    return integral_multiplier


def _unit_multiplier(row: dict[str, Any]) -> int | float | None:
    explicit = row.get("UNIT_MULTIPLIER")
    if explicit not in (None, ""):
        return _numeric(explicit)
    raw = row.get("UNIT_MULT")
    if raw not in (None, ""):
        value = _numeric(raw)
        if value is None:
            return None
        # UNIT_MULT is a power-of-ten code in IMF SDMX metadata.
        return _power_of_ten(int(value))
    scale = row.get("SCALE")
    if scale not in (None, ""):
        try:
            exponent = int(str(scale).strip())
        except ValueError:
            return None
        return _power_of_ten(exponent)
    return None


def _country_names(document: dict[str, Any], candidates: tuple[str, ...]) -> dict[str, str]:
    for codelist_id in candidates:
        labels = _sdmx.code_labels(document, codelist_id)
        if labels:
            return labels
    return {}


def _parse_csv_period(raw: object) -> date:
    try:
        return _sdmx.parse_period(raw)
    except (TypeError, ValueError) as exc:
        raise ImfSdmxError("IMF_BAD_OBSERVATION") from exc


class ImfCpiQuery(QueryParams):
    country: str | list[str] = "all"
    transform: Literal[
        "index", "period", "yoy", "ref_index", "ref_period", "ref_yoy", "weight", "weight_percent"
    ] = "yoy"
    frequency: Literal["annual", "quarter", "monthly"] = "monthly"
    harmonized: bool = False
    expenditure: str | list[str] = "total"
    limit: StrictInt | None = Field(default=None, ge=1, le=10000)

    @field_validator("country")
    @classmethod
    def _country_codes(cls, value: str | list[str]) -> str | list[str]:
        _country_filter(value)
        return value

    @field_validator("expenditure")
    @classmethod
    def _expenditure_codes(cls, value: str | list[str]) -> str | list[str]:
        values = value if isinstance(value, list) else [value]
        for item in values:
            token = item.strip().lower().replace(" ", "_")
            if token not in _EXPENDITURES and token.upper() not in _EXPENDITURES.values():
                raise ValueError("unsupported IMF CPI expenditure category")
        return value

    @model_validator(mode="after")
    def _window_order(self) -> ImfCpiQuery:
        _window(self.start_date, self.end_date)
        return self


@dataclass(frozen=True)
class _CpiRaw:
    metadata: dict[str, Any]
    observations: list[dict[str, Any]]


class ImfConsumerPriceIndexFetcher(Fetcher[ImfCpiQuery, _CpiRaw]):
    """National and harmonised CPI observations from the IMF CPI dataflow."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "ConsumerPriceIndex"
    row_model: ClassVar[type[ImfConsumerPriceIndex]] = ImfConsumerPriceIndex
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_imf_cpi",
        period="variable",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> ImfCpiQuery:
        """Validate CPI query selectors before source I/O."""
        return ImfCpiQuery.model_validate(kwargs)

    def extract_data(self, params: ImfCpiQuery, ctx: FetchContext) -> _CpiRaw:
        """Fetch CPI dimensions and observations selected by the query."""
        metadata = _sdmx.fetch_dataflow_structure(*CPI_FLOW, timeout=ctx.timeout)
        country_names = _country_names(metadata, ("CL_COUNTRY", "CL_CPI_COUNTRY"))
        countries = _resolve_country_filter(_country_filter(params.country), country_names)
        expenditures_raw = (
            params.expenditure if isinstance(params.expenditure, list) else [params.expenditure]
        )
        all_expenditures = any(item.strip().lower() == "all" for item in expenditures_raw)
        expenditures = tuple(
            _EXPENDITURES.get(item.strip().lower().replace(" ", "_"), item.strip().upper())
            for item in expenditures_raw
            if item.strip().lower() != "all"
        )
        if all_expenditures:
            expenditures = ("",)
        index_type = "HICP" if params.harmonized else "CPI"
        key = ".".join(
            (
                _key_values(countries),
                index_type,
                _key_values(expenditures),
                _TRANSFORMS[params.transform],
                {"annual": "A", "quarter": "Q", "monthly": "M"}[params.frequency],
            )
        )
        observations = _sdmx.fetch_observations(
            *CPI_FLOW,
            key,
            start=params.start_date.isoformat() if params.start_date else None,
            end=params.end_date.isoformat() if params.end_date else None,
            timeout=ctx.timeout,
        )
        return _CpiRaw(metadata, observations)

    def transform_data(self, raw: _CpiRaw, params: ImfCpiQuery) -> FetchResult:
        """Validate CPI response dimensions and build typed observations."""
        document = raw.metadata
        observations = raw.observations
        country_names = _country_names(document, ("CL_COUNTRY", "CL_CPI_COUNTRY"))
        expenditure_labels = _sdmx.code_labels(document, "CL_COICOP_1999")
        transformation_labels = _sdmx.code_labels(document, "CL_CPI_TYPE_OF_TRANSFORMATION")
        index_labels = _sdmx.code_labels(document, "CL_INDEX_TYPE")
        expenditure_codes = list(expenditure_labels)
        order = {code: index for index, code in enumerate(expenditure_codes)}
        allowed_countries = set(
            _resolve_country_filter(_country_filter(params.country), country_names)
        )
        all_expenditures = any(
            item.strip().lower() == "all"
            for item in (
                params.expenditure if isinstance(params.expenditure, list) else [params.expenditure]
            )
        )
        allowed_expenditures = {
            _EXPENDITURES.get(item.strip().lower().replace(" ", "_"), item.strip().upper())
            for item in (
                params.expenditure if isinstance(params.expenditure, list) else [params.expenditure]
            )
        }
        records: list[ImfConsumerPriceIndex] = []
        for row in observations:
            try:
                country_code = str(row["COUNTRY"]).strip()
                index_type = str(row["INDEX_TYPE"]).strip()
                expenditure_code = str(row["COICOP_1999"]).strip()
                transformation = str(row["TYPE_OF_TRANSFORMATION"]).strip()
                frequency = str(row["FREQUENCY"]).strip()
                if allowed_countries != {""} and country_code not in allowed_countries:
                    raise ImfSdmxError("IMF_BAD_RESPONSE")
                if index_type != ("HICP" if params.harmonized else "CPI"):
                    raise ImfSdmxError("IMF_BAD_RESPONSE")
                if not all_expenditures and expenditure_code not in allowed_expenditures:
                    raise ImfSdmxError("IMF_BAD_RESPONSE")
                if transformation != _TRANSFORMS[params.transform]:
                    raise ImfSdmxError("IMF_BAD_RESPONSE")
                if frequency != {"annual": "A", "quarter": "Q", "monthly": "M"}[params.frequency]:
                    raise ImfSdmxError("IMF_BAD_RESPONSE")
                point_date = _parse_csv_period(row["TIME_PERIOD"])
                value = _finite_value(row.get("OBS_VALUE"), nullable=True)
                if value is None:
                    continue
                if params.start_date and point_date < params.start_date:
                    continue
                if params.end_date and point_date > params.end_date:
                    continue
                unit = row.get("UNIT") or row.get("UNIT_MEASURE")
                title = row.get("TITLE") or ", ".join(
                    (
                        _label(index_labels, index_type),
                        _label(expenditure_labels, expenditure_code),
                        _label(transformation_labels, transformation),
                    )
                )
                records.append(
                    ImfConsumerPriceIndex(
                        date=point_date,
                        country=_label(country_names, country_code),
                        value=value,
                        unit=str(unit).strip() if unit not in (None, "") else None,
                        unit_multiplier=_unit_multiplier(row),
                        country_code=country_code,
                        series_id=".".join(
                            (country_code, index_type, expenditure_code, transformation, frequency)
                        ),
                        expenditure=_label(expenditure_labels, expenditure_code),
                        title=str(title).strip(),
                        order=order.get(expenditure_code),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, ImfSdmxError):
                    raise
                raise ImfSdmxError("IMF_BAD_OBSERVATION") from exc
        records.sort(key=lambda item: (item.country_code, item.expenditure, item.date))
        if params.limit is None:
            return tuple(records)
        groups: dict[tuple[str, str], list[ImfConsumerPriceIndex]] = defaultdict(list)
        for record in records:
            groups[(record.country_code, record.expenditure)].append(record)
        limited = [record for values in groups.values() for record in values[-params.limit :]]
        return tuple(
            sorted(limited, key=lambda item: (item.country_code, item.expenditure, item.date))
        )


class ImfDotQuery(QueryParams):
    country: str | list[str] | None = None
    counterpart: str | list[str] | None = None
    direction: Literal["exports", "imports", "balance", "all"] = "balance"
    frequency: Literal["annual", "quarter", "monthly"] = "monthly"

    @field_validator("country", "counterpart")
    @classmethod
    def _valid_codes(cls, value: str | list[str] | None) -> str | list[str] | None:
        if value is not None:
            _country_filter(value)
        return value

    @model_validator(mode="after")
    def _valid_request(self) -> ImfDotQuery:
        _window(self.start_date, self.end_date)
        if _country_filter(self.country) == ("",) and _country_filter(self.counterpart) == ("",):
            raise ValueError("at least one of country or counterpart must be selected")
        return self


@dataclass(frozen=True)
class _DotRaw:
    metadata: dict[str, Any]
    observations: list[dict[str, Any]]


class ImfDirectionOfTradeFetcher(Fetcher[ImfDotQuery, _DotRaw]):
    """IMF's current IMTS dataset, the official successor to DOTS."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "DirectionOfTrade"
    row_model: ClassVar[type[ImfDirectionOfTrade]] = ImfDirectionOfTrade
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_imf_direction_of_trade",
        period="variable",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> ImfDotQuery:
        """Validate reporter, counterpart, direction, and period selectors."""
        return ImfDotQuery.model_validate(kwargs)

    def extract_data(self, params: ImfDotQuery, ctx: FetchContext) -> _DotRaw:
        """Fetch IMTS metadata and observations for the selected trade series."""
        metadata = _sdmx.fetch_dataflow_structure(*IMTS_FLOW, timeout=ctx.timeout)
        country_names = _country_names(metadata, ("CL_IMTS_COUNTRY",))
        countries = _resolve_country_filter(_country_filter(params.country), country_names)
        counterparts = _resolve_country_filter(_country_filter(params.counterpart), country_names)
        indicator_codes = (
            tuple(_DOT_INDICATORS.values())
            if params.direction == "all"
            else (_DOT_INDICATORS[params.direction],)
        )
        key = ".".join(
            (
                _key_values(countries),
                _key_values(indicator_codes),
                _key_values(counterparts),
                {"annual": "A", "quarter": "Q", "monthly": "M"}[params.frequency],
            )
        )
        observations = _sdmx.fetch_observations(
            *IMTS_FLOW,
            key,
            start=params.start_date.isoformat() if params.start_date else None,
            end=params.end_date.isoformat() if params.end_date else None,
            timeout=ctx.timeout,
        )
        return _DotRaw(metadata, observations)

    def transform_data(self, raw: _DotRaw, params: ImfDotQuery) -> FetchResult:
        """Validate IMTS dimensions and map trade observations to typed rows."""
        document = raw.metadata
        observations = raw.observations
        countries = _country_names(document, ("CL_IMTS_COUNTRY",))
        indicators = _sdmx.code_labels(document, "CL_IMTS_INDICATOR")
        selected_countries = set(
            _resolve_country_filter(_country_filter(params.country), countries)
        )
        selected_counterparts = set(
            _resolve_country_filter(_country_filter(params.counterpart), countries)
        )
        selected_indicators = (
            set(_DOT_INDICATORS.values())
            if params.direction == "all"
            else {_DOT_INDICATORS[params.direction]}
        )
        expected_freq = {"annual": "A", "quarter": "Q", "monthly": "M"}[params.frequency]
        result: list[ImfDirectionOfTrade] = []
        for row in observations:
            try:
                country_code = str(row["COUNTRY"]).strip()
                counterpart_code = str(row["COUNTERPART_COUNTRY"]).strip()
                indicator = str(row["INDICATOR"]).strip()
                frequency = str(row["FREQUENCY"]).strip()
                if indicator not in selected_indicators or frequency != expected_freq:
                    raise ImfSdmxError("IMF_BAD_RESPONSE")
                if selected_countries != {""} and country_code not in selected_countries:
                    raise ImfSdmxError("IMF_BAD_RESPONSE")
                if selected_counterparts != {""} and counterpart_code not in selected_counterparts:
                    raise ImfSdmxError("IMF_BAD_RESPONSE")
                point_date = _parse_csv_period(row["TIME_PERIOD"])
                if params.start_date and point_date < params.start_date:
                    continue
                if params.end_date and point_date > params.end_date:
                    continue
                value = _finite_value(row.get("OBS_VALUE"), nullable=True)
                if value is None:
                    continue
                result.append(
                    ImfDirectionOfTrade(
                        date=point_date,
                        symbol=".".join((indicator, country_code, counterpart_code, frequency)),
                        country=_label(countries, country_code),
                        counterpart=_label(countries, counterpart_code),
                        title=_label(indicators, indicator),
                        value=value,
                        scale=str(row["SCALE"]).strip()
                        if row.get("SCALE") not in (None, "")
                        else None,
                        unit=str(row.get("UNIT") or _CURRENCY_CODES[indicator]),
                        country_code=country_code,
                        counterpart_code=counterpart_code,
                        unit_multiplier=_integer_value(
                            _unit_multiplier(row), "direction-of-trade unit multiplier"
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, ImfSdmxError):
                    raise
                raise ImfSdmxError("IMF_BAD_OBSERVATION") from exc
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.country_code,
                    item.counterpart_code,
                    item.date,
                    item.symbol or "",
                ),
            )
        )


class ImfEconomicIndicatorQuery(MultiSymbolQueryParams):
    symbol: str | list[str]
    country: str | list[str] | None = None
    frequency: str | None = None
    transform: str | None = None
    dimension_values: str | list[str] | None = None
    limit: StrictInt | None = Field(default=None, ge=1, le=10000)

    @field_validator("country")
    @classmethod
    def _valid_country_codes(cls, value: str | list[str] | None) -> str | list[str] | None:
        if value is not None:
            _country_filter(value)
        return value

    @field_validator("frequency")
    @classmethod
    def _valid_frequency(cls, value: str | None) -> str | None:
        if value is None or value.casefold() == "all" or value.upper() in {"A", "Q", "M"}:
            return value
        if value.casefold() not in _FREQUENCIES:
            raise ValueError("frequency must be annual, quarter, monthly, or an IMF code")
        return value

    @field_validator("transform")
    @classmethod
    def _valid_transform_code(cls, value: str | None) -> str | None:
        if value is not None:
            _as_code(value)
        return value.upper() if value is not None else None

    @model_validator(mode="after")
    def _valid_symbols_and_window(self) -> ImfEconomicIndicatorQuery:
        _window(self.start_date, self.end_date)
        symbols = _selected_symbols(self.symbol)
        parsed = [_parse_symbol(symbol) for symbol in symbols]
        if not parsed or len({flow for _, flow, _ in parsed}) != 1:
            raise ValueError("one IMF dataflow per economic-indicator request is supported")
        if self.dimension_values is not None:
            dimensions = (
                self.dimension_values
                if isinstance(self.dimension_values, list)
                else [self.dimension_values]
            )
            seen: set[str] = set()
            for item in dimensions:
                if not isinstance(item, str) or item.count(":") != 1:
                    raise ValueError("dimension_values must use DIMENSION:CODE form")
                dimension, code = item.split(":", maxsplit=1)
                if not _TOKEN.fullmatch(dimension.upper()) or not _TOKEN.fullmatch(code.upper()):
                    raise ValueError("dimension_values contains an invalid SDMX code")
                if dimension.upper() in seen:
                    raise ValueError("dimension_values cannot repeat a dimension")
                seen.add(dimension.upper())
        return self


@dataclass(frozen=True)
class _EconomicRaw:
    metadata: dict[str, Any]
    flow_id: str
    observations: list[dict[str, Any]]


def _parse_symbol(symbol: str) -> tuple[str | None, str, str]:
    text = symbol.strip().upper()
    if text.count("::") != 1:
        raise ValueError("IMF symbol must be DATAFLOW::INDICATOR")
    flow_ref, indicator = text.split("::", maxsplit=1)
    if ":" in flow_ref:
        agency, flow = flow_ref.split(":", maxsplit=1)
        if not _TOKEN.fullmatch(agency.replace(".", "_")):
            raise ValueError("IMF symbol agency is invalid")
    else:
        agency, flow = None, flow_ref
    if not _TOKEN.fullmatch(flow) or not _TOKEN.fullmatch(indicator):
        raise ValueError("IMF symbol contains an invalid code")
    return agency, flow, indicator


def _selected_symbols(value: str | list[str]) -> list[str]:
    raw = value if isinstance(value, list) else [value]
    symbols = [piece.strip() for item in raw for piece in item.split(",")]
    if not symbols or any(not item for item in symbols):
        raise ValueError("symbol must contain one or more IMF dataflow indicators")
    if len(symbols) != len(set(symbols)):
        raise ValueError("duplicate IMF symbols are not allowed")
    return symbols


def _find_flow(data: dict[str, Any], agency: str | None, flow_id: str) -> dict[str, Any]:
    matches = [flow for flow in _sdmx.flow_records(data) if flow.get("id", "").upper() == flow_id]
    if agency is not None:
        matches = [flow for flow in matches if str(flow.get("agencyID", "")).upper() == agency]
    if len(matches) != 1:
        raise ImfSdmxError("IMF_UNKNOWN_OR_AMBIGUOUS_DATAFLOW")
    return matches[0]


def _field_dimension(
    dimensions: list[dict[str, Any]], choices: tuple[str, ...]
) -> dict[str, Any] | None:
    for item in dimensions:
        if str(item.get("id", "")).upper() in choices:
            return item
    return None


def _frequency_code(raw: str | None) -> str:
    if raw is None or raw.casefold() == "all":
        return ""
    return _FREQUENCIES.get(raw.casefold(), raw.upper())


def _transform_code(raw: str | None, labels: dict[str, str]) -> str | None:
    if raw is None:
        return None
    normalized = raw.strip().casefold()
    if normalized in {"index", "yoy", "period"}:
        exact = {"index": "IX", "yoy": "YOY", "period": "G1"}[normalized]
        if exact in labels:
            return exact
        terms = {
            "index": ("index",),
            "yoy": ("year-over-year", "year over year", "yoy"),
            "period": ("period-over-period", "period over period", "period change"),
        }[normalized]
        matches = [
            code
            for code, label in labels.items()
            if any(term in label.casefold() for term in terms)
        ]
        if len(matches) == 1:
            return matches[0]
        raise ValueError("transform is ambiguous or unavailable for the IMF dataflow")
    code = raw.upper()
    if labels and code not in labels:
        raise ValueError("transform is not a member of the IMF dataflow dimension")
    return code


class ImfEconomicIndicatorFetcher(Fetcher[ImfEconomicIndicatorQuery, _EconomicRaw]):
    """Query one IMF SDMX dataflow with its source-declared dimensions."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "EconomicIndicators"
    row_model: ClassVar[type[ImfEconomicIndicator]] = ImfEconomicIndicator
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_imf_indicators",
        period="variable",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> ImfEconomicIndicatorQuery:
        """Validate indicator symbols and source-dimension filters."""
        return ImfEconomicIndicatorQuery.model_validate(kwargs)

    def extract_data(self, params: ImfEconomicIndicatorQuery, ctx: FetchContext) -> _EconomicRaw:
        """Discover each selected IMF flow and fetch its observations."""
        symbols = _selected_symbols(params.symbol)
        parsed = [_parse_symbol(symbol) for symbol in symbols]
        agency, flow_id, _ = parsed[0]
        catalog = _sdmx.fetch_dataflows(timeout=ctx.timeout)
        flow = _find_flow(catalog, agency, flow_id)
        dataflow_agency = str(flow["agencyID"])
        version = str(flow["version"])
        document = _sdmx.fetch_dataflow_structure(
            dataflow_agency, flow_id, version, timeout=ctx.timeout
        )
        dimensions = _sdmx.data_dimensions(document)
        indicator_dim = _field_dimension(dimensions, ("INDICATOR", "MEASURE", "SERIES"))
        if indicator_dim is None:
            raise ImfSdmxError("IMF_UNSUPPORTED_DATAFLOW")
        country_dim = _field_dimension(dimensions, ("COUNTRY", "REF_AREA"))
        frequency_dim = _field_dimension(dimensions, ("FREQUENCY", "FREQ"))
        transform_dim = _field_dimension(dimensions, ("TRANSFORMATION", "TRANSFORM"))
        requested_codes = tuple(_parse_symbol(symbol)[2] for symbol in symbols)
        indicator_labels = _sdmx.dimension_labels(document, indicator_dim)
        if indicator_labels and any(code not in indicator_labels for code in requested_codes):
            raise ValueError("symbol is not a member of the IMF dataflow indicator dimension")
        if params.country is not None and country_dim is None:
            raise ValueError("the selected IMF dataflow has no country dimension")
        requested_frequency = _frequency_code(params.frequency)
        if requested_frequency and frequency_dim is None:
            raise ValueError("the selected IMF dataflow has no frequency dimension")
        frequency_labels = (
            _sdmx.dimension_labels(document, frequency_dim) if frequency_dim is not None else {}
        )
        if requested_frequency and frequency_labels and requested_frequency not in frequency_labels:
            raise ValueError("frequency is not a member of the IMF dataflow dimension")
        selected: dict[str, str] = {}
        if country_dim is not None:
            country_labels = _sdmx.dimension_labels(document, country_dim)
            country_codes = _resolve_country_filter(_country_filter(params.country), country_labels)
            selected[str(country_dim["id"]).upper()] = _key_values(country_codes)
        selected[str(indicator_dim["id"]).upper()] = _key_values(requested_codes)
        if frequency_dim is not None:
            selected[str(frequency_dim["id"]).upper()] = requested_frequency
        if transform_dim is not None and params.transform is not None:
            labels = _sdmx.dimension_labels(document, transform_dim)
            transform_code = _transform_code(params.transform, labels)
            if transform_code is not None:
                selected[str(transform_dim["id"]).upper()] = transform_code
        elif params.transform is not None:
            raise ValueError("the selected IMF dataflow has no transformation dimension")
        if params.dimension_values is not None:
            values = (
                params.dimension_values
                if isinstance(params.dimension_values, list)
                else [params.dimension_values]
            )
            for item in values:
                dimension, code = item.split(":", maxsplit=1)
                dimension = dimension.upper()
                known = {str(part.get("id", "")).upper(): part for part in dimensions}
                if dimension not in known or dimension in selected:
                    raise ValueError(
                        "dimension_values targets an unknown or already selected dimension"
                    )
                labels = _sdmx.dimension_labels(document, known[dimension])
                if labels and code.upper() not in labels:
                    raise ValueError("dimension_values code is not in the IMF dataflow dimension")
                selected[dimension] = code.upper()
        key = ".".join(selected.get(str(dimension["id"]).upper(), "") for dimension in dimensions)
        observations = _sdmx.fetch_observations(
            dataflow_agency,
            flow_id,
            version,
            key,
            start=params.start_date.isoformat() if params.start_date else None,
            end=params.end_date.isoformat() if params.end_date else None,
            timeout=ctx.timeout,
        )
        return _EconomicRaw(document, flow_id, observations)

    def transform_data(self, raw: _EconomicRaw, params: ImfEconomicIndicatorQuery) -> FetchResult:
        """Validate returned dimensions and produce typed IMF indicator rows."""
        symbols = _selected_symbols(params.symbol)
        parsed = [_parse_symbol(symbol) for symbol in symbols]
        agency, flow_id, _ = parsed[0]
        document = raw.metadata
        dimensions = _sdmx.data_dimensions(document)
        indicator_dim = _field_dimension(dimensions, ("INDICATOR", "MEASURE", "SERIES"))
        country_dim = _field_dimension(dimensions, ("COUNTRY", "REF_AREA"))
        if indicator_dim is None:
            raise ImfSdmxError("IMF_UNSUPPORTED_DATAFLOW")
        indicator_dim_id = str(indicator_dim["id"])
        country_dim_id = str(country_dim["id"]) if country_dim is not None else None
        indicator_labels = _sdmx.dimension_labels(document, indicator_dim)
        country_labels = _sdmx.dimension_labels(document, country_dim) if country_dim else {}
        expected_symbols = {code for _, _, code in parsed}
        expected_countries = set(
            _resolve_country_filter(_country_filter(params.country), country_labels)
        )
        frequency_dim = _field_dimension(dimensions, ("FREQUENCY", "FREQ"))
        transform_dim = _field_dimension(dimensions, ("TRANSFORMATION", "TRANSFORM"))
        frequency_code = _frequency_code(params.frequency)
        if params.country is not None and country_dim is None:
            raise ImfSdmxError("IMF_BAD_RESPONSE")
        if frequency_code and frequency_dim is None:
            raise ImfSdmxError("IMF_BAD_RESPONSE")
        transform_code = _transform_code(
            params.transform,
            _sdmx.dimension_labels(document, transform_dim) if transform_dim else {},
        )
        dimension_filters: dict[str, set[str]] = {}
        if params.dimension_values is not None:
            values = (
                params.dimension_values
                if isinstance(params.dimension_values, list)
                else [params.dimension_values]
            )
            dimension_filters = {
                item.split(":", maxsplit=1)[0].upper(): {item.split(":", maxsplit=1)[1].upper()}
                for item in values
            }
        records: list[ImfEconomicIndicator] = []
        for row in raw.observations:
            try:
                indicator = str(row[indicator_dim_id]).strip()
                if indicator not in expected_symbols:
                    raise ImfSdmxError("IMF_BAD_RESPONSE")
                country_code = str(row[country_dim_id]).strip() if country_dim_id else ""
                if expected_countries != {""} and country_code not in expected_countries:
                    raise ImfSdmxError("IMF_BAD_RESPONSE")
                if (
                    frequency_dim is not None
                    and frequency_code
                    and str(row.get(str(frequency_dim["id"]), "")).strip() != frequency_code
                ):
                    raise ImfSdmxError("IMF_BAD_RESPONSE")
                if (
                    transform_dim is not None
                    and transform_code
                    and str(row.get(str(transform_dim["id"]), "")).strip() != transform_code
                ):
                    raise ImfSdmxError("IMF_BAD_RESPONSE")
                if any(
                    str(row.get(dimension, "")).strip().upper() not in selected_codes
                    for dimension, selected_codes in dimension_filters.items()
                ):
                    raise ImfSdmxError("IMF_BAD_RESPONSE")
                value = _finite_value(row.get("OBS_VALUE"), nullable=True)
                point_date = _parse_csv_period(row["TIME_PERIOD"])
                if params.start_date and point_date < params.start_date:
                    continue
                if params.end_date and point_date > params.end_date:
                    continue
                title = row.get("TITLE") or _label(indicator_labels, indicator)
                extras = [
                    f"{dimension['id']}={row[str(dimension['id'])]}"
                    for dimension in dimensions
                    if dimension not in (indicator_dim, country_dim)
                    and row.get(str(dimension["id"])) not in (None, "")
                ]
                root_symbol = f"{flow_id}::{indicator}"
                symbol = root_symbol if not extras else root_symbol + "::" + ",".join(extras)
                records.append(
                    ImfEconomicIndicator(
                        date=point_date,
                        symbol_root=root_symbol,
                        symbol=symbol,
                        country=_label(country_labels, country_code) if country_code else None,
                        value=value,
                        unit=str(row.get("UNIT") or row.get("UNIT_MEASURE") or "").strip() or None,
                        unit_multiplier=_unit_multiplier(row),
                        scale=str(row["SCALE"]).strip()
                        if row.get("SCALE") not in (None, "")
                        else None,
                        order=_numeric(row.get("ORDER")),
                        level=_integer_value(_numeric(row.get("LEVEL")), "indicator level"),
                        title=str(title).strip() if title else None,
                        description=str(row.get("DESCRIPTION")).strip()
                        if row.get("DESCRIPTION")
                        else _label(indicator_labels, indicator),
                        country_code=country_code or None,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, ImfSdmxError):
                    raise
                raise ImfSdmxError("IMF_BAD_OBSERVATION") from exc
        records.sort(key=lambda item: (item.symbol or "", item.country_code or "", item.date))
        if params.limit is None:
            return tuple(records)
        grouped: dict[tuple[str | None, str | None], list[ImfEconomicIndicator]] = defaultdict(list)
        for record in records:
            grouped[(record.symbol, record.country_code)].append(record)
        limited = [record for rows in grouped.values() for record in rows[-params.limit :]]
        return tuple(
            sorted(
                limited, key=lambda item: (item.symbol or "", item.country_code or "", item.date)
            )
        )


class ImfAvailableIndicatorQuery(QueryParams):
    """Complete catalogue by default; explicit flow pages are bounded."""

    dataflows: str | list[str] | None = None
    query: StrictStr | None = None
    keywords: str | list[str] | None = None
    symbol: str | None = None
    flow_offset: StrictInt | None = Field(default=None, ge=0, le=100000)
    flow_limit: StrictInt | None = Field(default=None, ge=1, le=63)
    max_requests: StrictInt = Field(default=64, ge=2, le=64)

    @field_validator("dataflows")
    @classmethod
    def _valid_dataflows(cls, value: str | list[str] | None) -> str | list[str] | None:
        _code_list(value)
        return value

    @field_validator("keywords")
    @classmethod
    def _valid_keywords(cls, value: str | list[str] | None) -> str | list[str] | None:
        values = value if isinstance(value, list) else ([value] if value is not None else [])
        if any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError("keywords must be nonblank")
        return value

    @model_validator(mode="after")
    def _bounded_catalog(self) -> ImfAvailableIndicatorQuery:
        flow_ids = _code_list(self.dataflows)
        if (self.flow_offset is None) != (self.flow_limit is None):
            raise ValueError("flow_offset and flow_limit must be supplied together")
        if self.flow_offset is not None and flow_ids != ("",):
            raise ValueError("flow paging cannot be combined with explicit dataflows")
        selected_count = self.flow_limit if self.flow_limit is not None else len(flow_ids)
        if selected_count and selected_count + 1 > self.max_requests:
            raise ValueError("selected IMF flows plus catalogue request exceeds max_requests")
        return self


class ImfAvailableIndicatorsFetcher(Fetcher[ImfAvailableIndicatorQuery, list[dict[str, Any]]]):
    """List indicator codes across the whole catalogue or an explicit flow page."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "AvailableIndicators"
    row_model: ClassVar[type[ImfAvailableIndicator]] = ImfAvailableIndicator
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_imf_available_indicators",
        period="snapshot",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> ImfAvailableIndicatorQuery:
        """Validate dataflow page, offset, and total request budget."""
        return ImfAvailableIndicatorQuery.model_validate(kwargs)

    def extract_data(
        self, params: ImfAvailableIndicatorQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Read the requested complete or explicitly paged IMF catalogue."""
        flow_ids = _code_list(params.dataflows)
        all_flows = flow_ids == ("",)
        catalog = _sdmx.fetch_dataflows(timeout=ctx.timeout)
        available_flows = _sdmx.flow_records(catalog)
        if all_flows and params.flow_limit is not None:
            if params.flow_offset is None:
                raise ValueError("flow_offset is required when flow_limit is set")
            flows = available_flows[params.flow_offset : params.flow_offset + params.flow_limit]
        elif all_flows:
            flows = available_flows
        else:
            flows = [_find_flow(catalog, None, flow_id) for flow_id in flow_ids]
        if len(flows) + 1 > params.max_requests:
            # The catalog size can change between releases. Check the actual
            # page before any per-flow requests and never return a partial page.
            raise ImfSdmxError("IMF_REQUEST_BUDGET_EXCEEDED")
        result: list[dict[str, Any]] = []
        for flow in flows:
            document = _sdmx.fetch_dataflow_structure(
                str(flow["agencyID"]), str(flow["id"]), str(flow["version"]), timeout=ctx.timeout
            )
            dimensions = _sdmx.data_dimensions(document)
            indicator_dimensions = [
                item
                for item in dimensions
                if str(item.get("id", "")).upper()
                in {"INDICATOR", "MEASURE", "SERIES", "INDEX_TYPE"}
            ]
            if not indicator_dimensions:
                if all_flows:
                    continue
                raise ImfSdmxError("IMF_UNSUPPORTED_DATAFLOW")
            codes: list[tuple[dict[str, Any], str, str]] = []
            for dimension in indicator_dimensions:
                code_map = _sdmx.dimension_labels(document, dimension)
                if not code_map:
                    raise ImfSdmxError("IMF_BAD_RESPONSE")
                codes.extend((dimension, code, label) for code, label in code_map.items())
            frequency_dimension = _field_dimension(dimensions, ("FREQUENCY", "FREQ"))
            frequency_labels = (
                _sdmx.dimension_labels(document, frequency_dimension) if frequency_dimension else {}
            )
            structure_id = str(document.get("dataStructures", [{}])[0].get("id", ""))
            dataflow_name = str(flow.get("name", flow["id"]))
            requested_symbol = params.symbol
            search = params.query.casefold() if params.query else ""
            keywords = (
                params.keywords
                if isinstance(params.keywords, list)
                else ([params.keywords] if params.keywords else [])
            )
            for dimension, code, label in codes:
                root = f"{flow['id']}::{code}"
                if requested_symbol and requested_symbol.upper() not in {
                    root.upper(),
                    code.upper(),
                }:
                    continue
                text = f"{label} {dataflow_name}".casefold()
                if search and search not in text:
                    continue
                if any(keyword.casefold() not in text for keyword in keywords):
                    continue
                indicator_id = str(dimension.get("id", ""))
                extra = tuple(
                    str(item.get("id"))
                    for item in dimensions
                    if str(item.get("id"))
                    not in {indicator_id, "FREQUENCY", "FREQ", "COUNTRY", "REF_AREA"}
                )
                long_description = None
                # SDMX code descriptions are distinct from the short display label.
                for code_item in _sdmx.codelist(
                    document, _sdmx.dimension_code_list_id(dimension) or ""
                ):
                    if code_item.get("id") == code:
                        descriptions = code_item.get("descriptions")
                        long_description = (
                            descriptions.get("en") if isinstance(descriptions, dict) else None
                        )
                        break
                result.append(
                    {
                        "symbol_root": root,
                        "symbol": root,
                        "country": None,
                        "iso": None,
                        "description": label,
                        "frequency": ",".join(frequency_labels.values())
                        if frequency_labels
                        else None,
                        "agency_id": str(flow["agencyID"]),
                        "dataflow_id": str(flow["id"]),
                        "dataflow_name": dataflow_name,
                        "structure_id": structure_id,
                        "dimension_id": indicator_id,
                        "long_description": long_description,
                        "member_of": None,
                        "extra_dimensions": extra or None,
                    }
                )
        return result

    def transform_data(
        self, raw: list[dict[str, Any]], params: ImfAvailableIndicatorQuery
    ) -> FetchResult:
        """Convert fetched dataflow indicators into unique typed catalogue rows."""
        del params
        try:
            rows = [ImfAvailableIndicator(**item) for item in raw]
        except (TypeError, ValueError) as exc:
            raise ImfSdmxError("IMF_BAD_RESPONSE") from exc
        keys = [(row.dataflow_id, row.dimension_id, row.symbol) for row in rows]
        if len(keys) != len(set(keys)):
            raise ImfSdmxError("IMF_BAD_RESPONSE")
        return tuple(
            sorted(rows, key=lambda item: (item.dataflow_id, item.dimension_id, item.symbol or ""))
        )


__all__ = [
    "ImfAvailableIndicatorsFetcher",
    "ImfConsumerPriceIndexFetcher",
    "ImfDirectionOfTradeFetcher",
    "ImfEconomicIndicatorFetcher",
]
