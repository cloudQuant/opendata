"""Typed OECD SDMX fetchers for the fixed macro iteration identities.

The key positions below follow each OECD dataflow's public SDMX structure.
Query parameters are validated before the shared HTTP client is reached.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, ClassVar, Literal, TypeVar

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.base import ContractModel
from opendata.data.models.macro_iter2 import (
    OecdCompositeLeadingIndicator,
    OecdConsumerPriceIndex,
    OecdCountryInterestRate,
    OecdGdpForecast,
    OecdGdpNominal,
    OecdGdpReal,
    OecdHousePriceIndex,
    OecdSharePriceIndex,
    OecdUnemployment,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.oecd._source import SOURCE
from opendata.data.providers.oecd.models._client import (
    OecdProviderError,
    fetch_observations,
    normalize_period,
)

CLI_FLOW = "OECD.SDD.STES,DSD_STES@DF_CLI,4.1"
CPI_FLOW = "OECD.SDD.TPS,DSD_PRICES@DF_PRICES_ALL,1.0"
RATE_FLOW = "OECD.SDD.STES,DSD_KEI@DF_KEI,4.0"
SHARE_FLOW = "OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0"
HOUSE_PRICE_FLOW = "OECD.ECO.MPD,DSD_AN_HOUSE_PRICES@DF_HOUSE_PRICES,1.0"
GDP_USD_FLOW = "OECD.SDD.NAD,DSD_NAMAIN1@DF_QNA_EXPENDITURE_USD,1.1"
GDP_INDEX_FLOW = "OECD.SDD.NAD,DSD_NAMAIN1@DF_QNA_EXPENDITURE_INDICES,1.1"
GDP_ANNUAL_FLOW = "OECD.SDD.NAD,DSD_NAMAIN10@DF_TABLE1_EXPENDITURE,2.0"
GDP_ANNUAL_CAPITA_CURRENT_FLOW = "OECD.SDD.NAD,DSD_NAMAIN10@DF_TABLE1_EXPENDITURE_HCPC,2.0"
GDP_ANNUAL_CAPITA_VOLUME_FLOW = "OECD.SDD.NAD,DSD_NAMAIN10@DF_TABLE1_EXPENDITURE_HVPVOB,2.0"
GDP_FORECAST_FLOW = "OECD.ECO.MAD,DSD_EO@DF_EO,1.5"
UNEMPLOYMENT_FLOW = "OECD.SDD.TPS,DSD_LFS@DF_IALFS_INDIC,1.0"

ModelT = TypeVar("ModelT", bound=ContractModel)

_COUNTRY_ALIASES = {
    "ARGENTINA": "ARG",
    "AUSTRALIA": "AUS",
    "AUSTRIA": "AUT",
    "BELGIUM": "BEL",
    "BRAZIL": "BRA",
    "BULGARIA": "BGR",
    "CANADA": "CAN",
    "CHILE": "CHL",
    "CHINA": "CHN",
    "COLOMBIA": "COL",
    "COSTA_RICA": "CRI",
    "CROATIA": "HRV",
    "CZECHIA": "CZE",
    "CZECH_REPUBLIC": "CZE",
    "DENMARK": "DNK",
    "ESTONIA": "EST",
    "EU15": "EU15",
    "EU27": "EU27_2020",
    "EU22_OECD": "EU22OECD",
    "EURO_AREA19": "EA19",
    "EURO_AREA_17_COUNTRIES": "EA17",
    "EURO_AREA_19_COUNTRIES": "EA19",
    "EURO_AREA20": "EA20",
    "EURO_AREA": "EA20",
    "EURO_AREA_20_COUNTRIES": "EA20",
    "FINLAND": "FIN",
    "FRANCE": "FRA",
    "GERMANY": "DEU",
    "GREECE": "GRC",
    "HUNGARY": "HUN",
    "ICELAND": "ISL",
    "INDIA": "IND",
    "INDONESIA": "IDN",
    "IRELAND": "IRL",
    "ISRAEL": "ISR",
    "ITALY": "ITA",
    "JAPAN": "JPN",
    "KOREA": "KOR",
    "LATVIA": "LVA",
    "LITHUANIA": "LTU",
    "LUXEMBOURG": "LUX",
    "FORMER_USSR": "SUN_F",
    "SOUTH_KOREA": "KOR",
    "MEXICO": "MEX",
    "NETHERLANDS": "NLD",
    "NEW_ZEALAND": "NZL",
    "NORWAY": "NOR",
    "OECD_26_COUNTRIES": "OECD26",
    "OECD_ASIA_OCEANIA": "OECDSO",
    "OECD_EUROPE": "OECDE",
    "NON_OECD_ECONOMIES": "WXOECD",
    "OPEC": "OPEC",
    "OTHER_COUNTRIES": "W_O",
    "OTHER_MAJOR_OIL": "OIL_O",
    "OTHER_OIL_PRODUCERS": "OIL_SAU_O",
    "PERU": "PER",
    "REST_OF_WORLD": "WXD",
    "POLAND": "POL",
    "PORTUGAL": "PRT",
    "ROMANIA": "ROU",
    "RUSSIA": "RUS",
    "SAUDI_ARABIA": "SAU",
    "SLOVAK_REPUBLIC": "SVK",
    "SLOVAKIA": "SVK",
    "SLOVENIA": "SVN",
    "SOUTH_AFRICA": "ZAF",
    "SPAIN": "ESP",
    "SWEDEN": "SWE",
    "SWITZERLAND": "CHE",
    "TURKEY": "TUR",
    "TURKIYE": "TUR",
    "THAILAND": "THA",
    "UNITED_KINGDOM": "GBR",
    "UNITED_STATES": "USA",
    "WORLD": "W",
    "USMCA": "USMCA",
    "USA": "USA",
    "UK": "GBR",
    "G20": "G20",
    "G7": "G7",
    "ASIA5": "ASIA5",
    "NORTH_AMERICA": "NAFTA",
    "EUROPE4": "EUROPE4",
    "OECD_TOTAL": "OECD",
    "ALL": "",
}
_COUNTRY_RE = re.compile(r"^[A-Z0-9_]*$")
_OECD_AREA_CODES = {
    "ASIA5",
    "EU15",
    "EU27_2020",
    "EA19",
    "EA20",
    "EUROPE4",
    "G20",
    "G7",
    "NAFTA",
    "OECD",
    "OECD26",
    "OECDE",
    "USMCA",
    "EU22OECD",
    "EA17",
    "SUN_F",
    "OECDSO",
    "WXOECD",
    "OPEC",
    "W_O",
    "OIL_O",
    "OIL_SAU_O",
    "WXD",
    "W",
}
_FREQUENCIES = {"annual": "A", "quarter": "Q", "monthly": "M"}
_EXPENDITURES = {
    "actual_rentals": "CP041",
    "alcoholic_beverages_tobacco_narcotics": "CP02",
    "all_non_food_non_energy": "_TXCP01_NRG",
    "clothing_footwear": "CP03",
    "communication": "CP08",
    "education": "CP10",
    "electricity_gas_other_fuels": "CP045",
    "energy": "CP045_0722",
    "food_non_alcoholic_beverages": "CP01",
    "fuels_lubricants_personal": "CP0722",
    "furniture_household_equipment": "CP05",
    "goods": "GD",
    "health": "CP06",
    "housing": "CP041T043",
    "housing_excluding_rentals": "CP041T043X042",
    "housing_water_electricity_gas": "CP04",
    "imputed_rentals": "CP042",
    "maintenance_repair_dwelling": "CP043",
    "miscellaneous_goods_services": "CP12",
    "overall_excl_energy_food_alcohol_tobacco": "_TXNRG_01_02",
    "recreation_culture": "CP09",
    "residuals": "CPRES",
    "restaurants_hotels": "CP11",
    "services": "SERV",
    "services_less_house_excl_rentals": "SERVXCP041_0432",
    "services_less_housing": "SERVXCP041_042_0432",
    "total": "_T",
    "transport": "CP07",
    "water_supply_other_services": "CP044",
}


def _expenditure_codes(value: str | list[str]) -> tuple[str, ...]:
    raw_values = value if isinstance(value, list) else [value]
    if not raw_values or any(not isinstance(item, str) or not item.strip() for item in raw_values):
        raise ValueError("expenditure must contain one or more nonempty categories")
    codes = tuple(
        ""
        if item.strip().casefold() == "all"
        else "_T"
        if item.strip().casefold() == "_t"
        else _EXPENDITURES.get(item.strip().casefold().replace(" ", "_"))
        for item in raw_values
    )
    if any(code is None for code in codes):
        raise ValueError("expenditure must be a supported OECD CPI category")
    if "" in codes and len(codes) != 1:
        raise ValueError("the all-expenditure selector cannot be combined")
    if len(codes) != len(set(codes)):
        raise ValueError("expenditure categories must be unique")
    return tuple(str(code) for code in codes)


def _country_codes(value: str | list[str]) -> tuple[str, ...]:
    """Convert documented country names or SDMX area codes into exact keys."""
    raw_values = value if isinstance(value, list) else [value]
    if not raw_values or any(not isinstance(item, str) or not item.strip() for item in raw_values):
        raise ValueError("country must contain one or more nonempty area identifiers")
    result: list[str] = []
    for item in raw_values:
        token = item.strip().upper().replace(" ", "_").replace("-", "_")
        code = _COUNTRY_ALIASES.get(token, token)
        if not _COUNTRY_RE.fullmatch(code):
            raise ValueError("country must be an OECD area name or SDMX area code")
        if code and len(code) != 3 and code not in _OECD_AREA_CODES:
            raise ValueError("country name is not a supported OECD area alias")
        result.append(code)
    if "" in result and len(result) != 1:
        raise ValueError("the all-area selector cannot be combined with other areas")
    if len(result) != len(set(result)):
        raise ValueError("country values must be unique")
    return tuple(result)


def _country_key(countries: tuple[str, ...]) -> str:
    """Render one SDMX area position, preserving the API's '+' list syntax."""
    return "+".join(countries)


def _date_window(start_date: date | None, end_date: date | None) -> None:
    """Reject an inverted window before any upstream request is made."""
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("start_date must not be after end_date")


def _shift_months(value: date, months: int) -> date:
    """Shift a normalized source period without overflowing leap dates."""
    absolute_month = value.year * 12 + value.month - 1 + months
    year, month0 = divmod(absolute_month, 12)
    month = month0 + 1
    # OECD period values are normalized to the first of the period.
    return date(year, month, 1)


def _fetch(
    flow: str,
    key: str,
    ctx: FetchContext,
    *,
    start: date | None,
    end: date | None,
) -> list[dict[str, object]]:
    """Use the shared guarded OECD transport for one exact SDMX key."""
    return fetch_observations(
        key,
        start=start,
        end=end,
        timeout=ctx.timeout,
        flow=flow,
    )


def _value(raw: object, *, nullable: bool) -> float | None:
    """Parse a CSV observation, retaining OECD's published missing marker."""
    if raw is None or str(raw).strip() in {"", ".."}:
        if nullable:
            return None
        raise ValueError("required OECD observation is missing")
    value = float(str(raw).strip())
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("OECD observation must be finite")
    return value


def _period(row: dict[str, object]) -> date:
    return date.fromisoformat(normalize_period(str(row["TIME_PERIOD"]).strip()))


def _record_rows(
    rows: Iterable[dict[str, object]],
    *,
    countries: tuple[str, ...],
    start: date | None,
    end: date | None,
    contract: type[ModelT],
    nullable: bool = True,
    extras: dict[str, object] | None = None,
    row_extras: dict[str, str] | None = None,
    frequency: str | None = None,
    expected_dimensions: Mapping[str, str | tuple[str, ...]] | None = None,
    apply_unit_multiplier: bool = False,
) -> tuple[ModelT, ...]:
    """Validate a one-series CSV result and return typed, sorted records."""
    parsed: list[tuple[date, ModelT]] = []
    seen: set[tuple[object, ...]] = set()
    for row in rows:
        try:
            country = str(row["REF_AREA"]).strip()
            point_date = _period(row)
            value = _value(row["OBS_VALUE"], nullable=nullable)
            if apply_unit_multiplier and value is not None:
                exponent = int(str(row["UNIT_MULT"]).strip())
                if not -12 <= exponent <= 12:
                    raise ValueError("OECD UNIT_MULT is outside the supported exponent range")
                value *= 10**exponent
                if value != value or value in (float("inf"), float("-inf")):
                    raise ValueError("scaled OECD observation must be finite")
            extra_values = {
                field: str(row[source]).strip() for field, source in (row_extras or {}).items()
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise OecdProviderError("OECD_BAD_OBSERVATION") from exc
        if expected_dimensions:
            for dimension, expected in expected_dimensions.items():
                actual = str(row.get(dimension, "")).strip()
                if actual not in expected if isinstance(expected, tuple) else actual != expected:
                    raise OecdProviderError("OECD_BAD_RESPONSE")
        if countries and countries != ("",) and country not in countries:
            raise OecdProviderError("OECD_BAD_RESPONSE")
        if frequency is not None and not _matches_frequency(str(row["TIME_PERIOD"]), frequency):
            raise OecdProviderError("OECD_BAD_RESPONSE")
        if start is not None and point_date < start:
            continue
        if end is not None and point_date > end:
            continue
        extra_key = tuple(extra_values[field] for field in (row_extras or {}))
        natural_key = (country, point_date, *extra_key)
        if natural_key in seen:
            raise OecdProviderError("OECD_BAD_RESPONSE")
        seen.add(natural_key)
        fields = {"date": point_date, "country": country, "value": value}
        if extras:
            fields.update(extras)
        if extra_values:
            fields.update(extra_values)
        try:
            parsed.append((point_date, contract(**fields)))
        except (TypeError, ValueError) as exc:
            raise OecdProviderError("OECD_BAD_OBSERVATION") from exc
    return tuple(row for _, row in sorted(parsed, key=lambda item: item[0]))


def _matches_frequency(period: str, frequency: str) -> bool:
    """Keep observations whose source period has the requested granularity."""
    if frequency == "A":
        return len(period) == 4
    if frequency == "Q":
        return (
            len(period) == 7
            and period[4:5] == "-"
            and period[5:6] == "Q"
            and period[6:7] in {"1", "2", "3", "4"}
        )
    if frequency == "M":
        return (
            len(period) == 7
            and period[4:5] == "-"
            and period[5:7].isdigit()
            and 1 <= int(period[5:7]) <= 12
        )
    return False


class _DateQuery(QueryParams):
    """Common date validation for OECD series queries."""

    @model_validator(mode="after")
    def _valid_window(self) -> _DateQuery:
        _date_window(self.start_date, self.end_date)
        return self


class _CountryQuery(_DateQuery):
    country: str | list[str] = "united_states"

    @field_validator("country")
    @classmethod
    def _valid_country(cls, value: str | list[str]) -> str | list[str]:
        _country_codes(value)
        return value


class OecdCliQuery(_CountryQuery):
    """Leading composite indicator query, defaulting to the G20 scope and amplitude adjustment."""

    country: str | list[str] = "g20"
    adjustment: Literal["amplitude", "normalized"] = "amplitude"
    growth_rate: StrictBool = False


class OecdCpiQuery(_CountryQuery):
    """Consumer-price-index query over transform, frequency, harmonisation and expenditure."""

    transform: Literal["index", "yoy", "period"] = "yoy"
    frequency: Literal["annual", "quarter", "monthly"] = "monthly"
    harmonized: StrictBool = False
    expenditure: str | list[str] = "total"
    limit: StrictInt | None = Field(default=None, ge=1, le=10000)

    @field_validator("expenditure")
    @classmethod
    def _valid_expenditure(cls, value: str | list[str]) -> str | list[str]:
        _expenditure_codes(value)
        return value


class OecdRateQuery(_CountryQuery):
    """Interest-rate query keyed by ``duration``."""

    duration: Literal["immediate", "short", "long"] = "short"
    frequency: Literal["monthly", "quarter", "annual"] = "monthly"


class OecdSharePriceQuery(_CountryQuery):
    """Query model for the OECD share-price-index domain."""

    frequency: Literal["monthly", "quarter", "annual"] = "monthly"


class OecdHousePriceQuery(_CountryQuery):
    """Query model for the OECD house-price-index domain."""

    frequency: Literal["quarter", "annual"] = "quarter"
    transform: Literal["index", "yoy", "period"] = "index"


class OecdGdpQuery(_CountryQuery):
    """Query model for the OECD nominal GDP domain."""

    frequency: Literal["quarter", "annual"] = "quarter"
    units: Literal["level", "index", "capita"] = "level"
    price_base: Literal["current_prices", "volume"] = "current_prices"


class OecdGdpRealQuery(_CountryQuery):
    """Query model for the OECD real GDP domain."""

    frequency: Literal["quarter", "annual"] = "quarter"


class OecdGdpForecastQuery(_CountryQuery):
    """Query model for the OECD GDP forecast domain."""

    country: str | list[str] = "all"
    frequency: Literal["quarter", "annual"] = "annual"
    units: Literal["current_prices", "volume", "capita", "growth", "deflator"] = "volume"


class OecdUnemploymentQuery(_CountryQuery):
    """Query model for the OECD unemployment domain."""

    frequency: Literal["monthly", "quarter", "annual"] = "monthly"
    sex: Literal["total", "male", "female"] = "total"
    age: Literal["total", "15-24", "25+", "15_to_64", "15_or_over"] = "total"
    seasonal_adjustment: StrictBool = False


class OecdCompositeLeadingIndicatorFetcher(Fetcher[OecdCliQuery, list[dict[str, object]]]):
    """Monthly CLI (index, amplitude adjusted or normalized)."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CompositeLeadingIndicator"
    row_model: ClassVar[type[ContractModel]] = OecdCompositeLeadingIndicator
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_oecd_cli",
        period="1M",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> OecdCliQuery:
        """Validate CLI query selectors before source I/O."""
        return OecdCliQuery.model_validate(kwargs)

    def extract_data(self, params: OecdCliQuery, ctx: FetchContext) -> list[dict[str, object]]:
        """Fetch the selected CLI index observations and required lookback."""
        countries = _country_codes(params.country)
        adjustment = "AA" if params.adjustment == "amplitude" else "NOR"
        # CLI growth-rate observations are not published in this dataflow.
        # Fetch the index level and derive the documented one-year rate below.
        key = f"{_country_key(countries)}.M.LI...{adjustment}.IX..H"
        start = (
            _shift_months(params.start_date, -12)
            if params.growth_rate and params.start_date
            else params.start_date
        )
        return _fetch(CLI_FLOW, key, ctx, start=start, end=params.end_date)

    def transform_data(self, raw: list[dict[str, object]], params: OecdCliQuery) -> FetchResult:
        """Validate CLI source dimensions and map typed index observations."""
        result = _record_rows(
            raw,
            countries=_country_codes(params.country),
            start=None if params.growth_rate else params.start_date,
            end=params.end_date,
            contract=OecdCompositeLeadingIndicator,
            expected_dimensions={
                "FREQ": "M",
                "MEASURE": "LI",
                "ADJUSTMENT": "AA" if params.adjustment == "amplitude" else "NOR",
                "UNIT_MEASURE": "IX",
                "TRANSFORMATION": "IX",
                "ACTIVITY": "_Z",
                "TIME_HORIZ": "_Z",
                "METHODOLOGY": "H",
            },
        )
        if not params.growth_rate:
            return result

        by_period = {(row.country, row.date): row for row in result}
        transformed: list[OecdCompositeLeadingIndicator] = []
        for current in result:
            if params.start_date is not None and current.date < params.start_date:
                continue
            prior = by_period.get((current.country, _shift_months(current.date, -12)))
            value = None
            if current.value is not None and prior is not None and prior.value not in {None, 0}:
                value = (current.value / prior.value - 1.0) * 100.0
            transformed.append(
                OecdCompositeLeadingIndicator(
                    date=current.date,
                    country=current.country,
                    value=value,
                )
            )
        return tuple(sorted(transformed, key=lambda row: (row.country, row.date)))


class OecdConsumerPriceIndexFetcher(Fetcher[OecdCpiQuery, list[dict[str, object]]]):
    """OECD monthly, quarterly, or annual CPI observations."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "ConsumerPriceIndex"
    row_model: ClassVar[type[ContractModel]] = OecdConsumerPriceIndex
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_oecd_cpi",
        period="variable",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> OecdCpiQuery:
        """Validate country, frequency, transform, and expenditure selectors."""
        return OecdCpiQuery.model_validate(kwargs)

    def extract_data(self, params: OecdCpiQuery, ctx: FetchContext) -> list[dict[str, object]]:
        """Fetch the requested OECD CPI series and inclusive date window."""
        countries = _country_codes(params.country)
        freq = _FREQUENCIES[params.frequency]
        method = "HICP" if params.harmonized else "N"
        if params.transform == "index":
            unit, transformation = "IX", "N"
        elif params.transform == "period":
            unit, transformation = "PC", "G1"
        else:
            unit, transformation = "PA", "GY"
        expenditure = "+".join(_expenditure_codes(params.expenditure))
        key = (
            f"{_country_key(countries)}.{freq}.{method}.CPI.{unit}.{expenditure}.N.{transformation}"
        )
        flow = CPI_FLOW
        if params.harmonized:
            # The same flow's METHODOLOGY dimension publishes HICP values.
            flow = CPI_FLOW
        return _fetch(flow, key, ctx, start=params.start_date, end=params.end_date)

    def transform_data(self, raw: list[dict[str, object]], params: OecdCpiQuery) -> FetchResult:
        """Validate CPI dimensions and map typed observations."""
        countries = _country_codes(params.country)
        expenditure_codes = _expenditure_codes(params.expenditure)
        expected_dimensions: dict[str, str | tuple[str, ...]] = {
            "FREQ": _FREQUENCIES[params.frequency],
            "METHODOLOGY": "HICP" if params.harmonized else "N",
            "MEASURE": "CPI",
            "UNIT_MEASURE": {"index": "IX", "period": "PC", "yoy": "PA"}[params.transform],
            "ADJUSTMENT": "N",
            "TRANSFORMATION": {"index": "N", "period": "G1", "yoy": "GY"}[params.transform],
        }
        if expenditure_codes != ("",):
            expected_dimensions["EXPENDITURE"] = expenditure_codes
        result = _record_rows(
            raw,
            countries=countries,
            start=params.start_date,
            end=params.end_date,
            contract=OecdConsumerPriceIndex,
            row_extras={"expenditure": "EXPENDITURE"},
            expected_dimensions=expected_dimensions,
        )
        if params.limit is None:
            return result
        by_country: dict[str, list[OecdConsumerPriceIndex]] = {}
        for row in result:
            by_country.setdefault(f"{row.country}:{row.expenditure}", []).append(row)
        return tuple(
            row
            for country_rows in by_country.values()
            for row in sorted(country_rows, key=lambda item: item.date)[-params.limit :]
        )


class OecdCountryInterestRateFetcher(Fetcher[OecdRateQuery, list[dict[str, object]]]):
    """Interest rates reported as percent per annum by OECD."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CountryInterestRates"
    row_model: ClassVar[type[ContractModel]] = OecdCountryInterestRate
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_oecd_interest_rates",
        period="variable",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> OecdRateQuery:
        """Validate OECD rate duration, frequency, and country selectors."""
        return OecdRateQuery.model_validate(kwargs)

    def extract_data(self, params: OecdRateQuery, ctx: FetchContext) -> list[dict[str, object]]:
        """Fetch the selected OECD interest-rate series."""
        measure = {"immediate": "IRSTCI", "short": "IR3TIB", "long": "IRLT"}[params.duration]
        freq = _FREQUENCIES[params.frequency]
        key = f"{_country_key(_country_codes(params.country))}.{freq}.{measure}.PA..."
        return _fetch(RATE_FLOW, key, ctx, start=params.start_date, end=params.end_date)

    def transform_data(self, raw: list[dict[str, object]], params: OecdRateQuery) -> FetchResult:
        """Validate source dimensions and map typed interest-rate observations."""
        return _record_rows(
            raw,
            countries=_country_codes(params.country),
            start=params.start_date,
            end=params.end_date,
            contract=OecdCountryInterestRate,
            expected_dimensions={
                "FREQ": _FREQUENCIES[params.frequency],
                "MEASURE": {"immediate": "IRSTCI", "short": "IR3TIB", "long": "IRLT"}[
                    params.duration
                ],
                "UNIT_MEASURE": "PA",
            },
        )


class OecdSharePriceIndexFetcher(Fetcher[OecdSharePriceQuery, list[dict[str, object]]]):
    """OECD share-price index (index base as defined by the source flow)."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "SharePriceIndex"
    row_model: ClassVar[type[ContractModel]] = OecdSharePriceIndex
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_oecd_share_price_index",
        period="variable",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> OecdSharePriceQuery:
        """Validate OECD share-price country and frequency selectors."""
        return OecdSharePriceQuery.model_validate(kwargs)

    def extract_data(
        self, params: OecdSharePriceQuery, ctx: FetchContext
    ) -> list[dict[str, object]]:
        """Fetch the selected OECD share-price index observations."""
        freq = _FREQUENCIES[params.frequency]
        key = f"{_country_key(_country_codes(params.country))}.{freq}.SHARE.IX....."
        return _fetch(SHARE_FLOW, key, ctx, start=params.start_date, end=params.end_date)

    def transform_data(
        self, raw: list[dict[str, object]], params: OecdSharePriceQuery
    ) -> FetchResult:
        """Validate share-price dimensions and map typed rows."""
        return _record_rows(
            raw,
            countries=_country_codes(params.country),
            start=params.start_date,
            end=params.end_date,
            contract=OecdSharePriceIndex,
            expected_dimensions={
                "FREQ": _FREQUENCIES[params.frequency],
                "MEASURE": "SHARE",
                "UNIT_MEASURE": "IX",
            },
        )


class OecdHousePriceIndexFetcher(Fetcher[OecdHousePriceQuery, list[dict[str, object]]]):
    """OECD real house-price index at its published quarterly/annual cadence."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "HousePriceIndex"
    row_model: ClassVar[type[ContractModel]] = OecdHousePriceIndex
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_oecd_house_price_index",
        period="variable",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> OecdHousePriceQuery:
        """Validate house-price frequency, transform, and country selectors."""
        return OecdHousePriceQuery.model_validate(kwargs)

    def extract_data(
        self, params: OecdHousePriceQuery, ctx: FetchContext
    ) -> list[dict[str, object]]:
        """Fetch the selected real-house-price index and lookback periods."""
        freq = _FREQUENCIES[params.frequency]
        # The typed output exposes one value field, so pin its meaning to the
        # published real-house-price index unit rather than accepting the
        # separate long-term-average percentage series.
        key = f"{_country_key(_country_codes(params.country))}.{freq}.RHP.IX"
        start = params.start_date
        if start is not None and params.transform != "index":
            months = 3 if params.transform == "period" and params.frequency == "quarter" else 12
            start = _shift_months(start, -months)
        return _fetch(HOUSE_PRICE_FLOW, key, ctx, start=start, end=params.end_date)

    def transform_data(
        self, raw: list[dict[str, object]], params: OecdHousePriceQuery
    ) -> FetchResult:
        """Validate house-price metadata and map or derive typed rows."""
        countries = _country_codes(params.country)
        base = _record_rows(
            raw,
            countries=countries,
            start=None,
            end=params.end_date,
            contract=OecdHousePriceIndex,
            expected_dimensions={
                "FREQ": _FREQUENCIES[params.frequency],
                "MEASURE": "RHP",
                "UNIT_MEASURE": "IX",
                "ADJUSTMENT": "S",
                "BASE_PER": "2015",
                "UNIT_MULT": "0",
            },
        )
        if params.transform == "index":
            return tuple(
                row for row in base if params.start_date is None or row.date >= params.start_date
            )
        month_step = 3 if params.transform == "period" and params.frequency == "quarter" else 12
        by_period = {(row.country, row.date): row for row in base}
        transformed: list[OecdHousePriceIndex] = []
        for current in base:
            prior_date = _shift_months(current.date, -month_step)
            prior = by_period.get((current.country, prior_date))
            value = None
            if current.value is not None and prior is not None and prior.value not in {None, 0}:
                value = (current.value / prior.value - 1.0) * 100.0
            if params.start_date is not None and current.date < params.start_date:
                continue
            transformed.append(
                OecdHousePriceIndex(date=current.date, country=current.country, value=value)
            )
        return tuple(sorted(transformed, key=lambda row: (row.country, row.date)))


class OecdGdpNominalFetcher(Fetcher[OecdGdpQuery, list[dict[str, object]]]):
    """GDP in current US dollars from OECD Quarterly National Accounts."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "GdpNominal"
    row_model: ClassVar[type[ContractModel]] = OecdGdpNominal
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_oecd_gdp_nominal",
        period="variable",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> OecdGdpQuery:
        """Validate nominal GDP units, frequency, and price-base selectors."""
        return OecdGdpQuery.model_validate(kwargs)

    def extract_data(self, params: OecdGdpQuery, ctx: FetchContext) -> list[dict[str, object]]:
        """Fetch the GDP flow and dimensions for the requested unit branch."""
        if params.units == "capita" and params.frequency != "annual":
            raise ValueError("OECD GDP per capita is available only annually")
        if params.units == "index" and params.price_base != "volume":
            raise ValueError("OECD GDP index is published for chain-linked volume only")
        freq = _FREQUENCIES[params.frequency]
        country = _country_key(_country_codes(params.country))
        if params.units == "capita":
            unit = "USD_PPP_PS"
            flow = (
                GDP_ANNUAL_CAPITA_CURRENT_FLOW
                if params.price_base == "current_prices"
                else GDP_ANNUAL_CAPITA_VOLUME_FLOW
            )
            transformation = "N"
            transaction = "B1GQ_POP"
        elif params.units == "index":
            unit = "IX"
            flow = GDP_INDEX_FLOW if params.frequency == "quarter" else GDP_ANNUAL_FLOW
            transformation = "N"
            transaction = "B1GQ"
        else:
            unit = "USD_PPP"
            flow = GDP_USD_FLOW if params.frequency == "quarter" else GDP_ANNUAL_FLOW
            transformation = "LA" if params.frequency == "quarter" else "N"
            transaction = "B1GQ"
        price_base = "V" if params.price_base == "current_prices" else "LR"
        if params.frequency == "quarter":
            # DSD_NAMAIN1 positions: FREQ, ADJUSTMENT, REF_AREA, SECTOR,
            # COUNTERPART_SECTOR, TRANSACTION, INSTR_ASSET, ACTIVITY,
            # EXPENDITURE, UNIT_MEASURE, PRICE_BASE, TRANSFORMATION, TABLE.
            key = (
                f"{freq}.Y.{country}.S1.S1.{transaction}._Z._Z._Z.{unit}."
                f"{price_base}.{transformation}.T0102"
            )
        else:
            # DSD_NAMAIN10 has the same national-accounts dimensions without
            # quarterly adjustment and uses the annual table's N transform.
            key = f"{freq}.{country}.S1.S1.{transaction}._Z._Z._Z.{unit}.{price_base}.N.T0102"
        return _fetch(flow, key, ctx, start=params.start_date, end=params.end_date)

    def transform_data(self, raw: list[dict[str, object]], params: OecdGdpQuery) -> FetchResult:
        """Validate GDP dimensions and map values under the selected unit contract."""
        if params.units == "capita" and params.frequency != "annual":
            raise ValueError("OECD GDP per capita is available only annually")
        if params.units == "index" and params.price_base != "volume":
            raise ValueError("OECD GDP index is published for chain-linked volume only")
        expected = {
            "FREQ": _FREQUENCIES[params.frequency],
            "SECTOR": "S1",
            "COUNTERPART_SECTOR": "S1",
            "TRANSACTION": "B1GQ_POP" if params.units == "capita" else "B1GQ",
            "UNIT_MEASURE": (
                "USD_PPP_PS"
                if params.units == "capita"
                else "IX"
                if params.units == "index"
                else "USD_PPP"
            ),
            "PRICE_BASE": "V" if params.price_base == "current_prices" else "LR",
            "TRANSFORMATION": (
                "N" if params.units == "index" or params.frequency == "annual" else "LA"
            ),
            "TABLE_IDENTIFIER": "T0102",
        }
        if params.frequency == "quarter":
            expected["ADJUSTMENT"] = "Y"
        return _record_rows(
            raw,
            countries=_country_codes(params.country),
            start=params.start_date,
            end=params.end_date,
            contract=OecdGdpNominal,
            nullable=False,
            apply_unit_multiplier=params.units != "index",
            expected_dimensions=expected,
        )


class OecdGdpRealFetcher(Fetcher[OecdGdpRealQuery, list[dict[str, object]]]):
    """OECD GDP at chain-linked volume prices in PPP-converted USD."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "GdpReal"
    row_model: ClassVar[type[ContractModel]] = OecdGdpReal
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_oecd_gdp_real",
        period="variable",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> OecdGdpRealQuery:
        """Validate real GDP country, frequency, and date selectors."""
        return OecdGdpRealQuery.model_validate(kwargs)

    def extract_data(self, params: OecdGdpRealQuery, ctx: FetchContext) -> list[dict[str, object]]:
        """Fetch the selected chain-linked real GDP observations."""
        freq = _FREQUENCIES[params.frequency]
        country = _country_key(_country_codes(params.country))
        if params.frequency == "quarter":
            # Quarterly QNA: FREQ, adjustment, area, sector, counterpart,
            # transaction, asset, activity, expenditure, unit, price base,
            # transformation, table.
            key = f"{freq}.Y.{country}.S1.S1.B1GQ._Z._Z._Z.USD_PPP.LR.LA.T0102"
            flow = GDP_USD_FLOW
        else:
            # Annual national accounts use the corresponding 12-dimension DSD.
            key = f"{freq}.{country}.S1.S1.B1GQ._Z._Z._Z.USD_PPP.LR.N.T0102"
            flow = GDP_ANNUAL_FLOW
        return _fetch(flow, key, ctx, start=params.start_date, end=params.end_date)

    def transform_data(self, raw: list[dict[str, object]], params: OecdGdpRealQuery) -> FetchResult:
        """Validate real GDP dimensions and map typed observations."""
        expected = {
            "FREQ": _FREQUENCIES[params.frequency],
            "SECTOR": "S1",
            "COUNTERPART_SECTOR": "S1",
            "TRANSACTION": "B1GQ",
            "UNIT_MEASURE": "USD_PPP",
            "PRICE_BASE": "LR",
            "TRANSFORMATION": "LA" if params.frequency == "quarter" else "N",
            "TABLE_IDENTIFIER": "T0102",
        }
        if params.frequency == "quarter":
            expected["ADJUSTMENT"] = "Y"
        return _record_rows(
            raw,
            countries=_country_codes(params.country),
            start=params.start_date,
            end=params.end_date,
            contract=OecdGdpReal,
            nullable=False,
            apply_unit_multiplier=True,
            expected_dimensions=expected,
        )


class OecdGdpForecastFetcher(Fetcher[OecdGdpForecastQuery, list[dict[str, object]]]):
    """OECD Economic Outlook GDP forecast variables with declared units."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "GdpForecast"
    row_model: ClassVar[type[ContractModel]] = OecdGdpForecast
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_oecd_gdp_forecast",
        period="variable",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> OecdGdpForecastQuery:
        """Validate forecast measure, frequency, and country selectors."""
        return OecdGdpForecastQuery.model_validate(kwargs)

    def extract_data(
        self, params: OecdGdpForecastQuery, ctx: FetchContext
    ) -> list[dict[str, object]]:
        """Fetch Economic Outlook observations for the selected measure."""
        measure = {
            "current_prices": "GDP_USD",
            "volume": "GDPV_USD",
            "capita": "GDPVD_CAP",
            "growth": "GDPV_ANNPCT",
            "deflator": "PGDP",
        }[params.units]
        freq = _FREQUENCIES[params.frequency]
        key = f"{_country_key(_country_codes(params.country))}.{measure}.{freq}"
        return _fetch(GDP_FORECAST_FLOW, key, ctx, start=params.start_date, end=params.end_date)

    def transform_data(
        self, raw: list[dict[str, object]], params: OecdGdpForecastQuery
    ) -> FetchResult:
        """Validate forecast dimensions and map typed observations."""
        return _record_rows(
            raw,
            countries=_country_codes(params.country),
            start=params.start_date,
            end=params.end_date,
            contract=OecdGdpForecast,
            nullable=False,
            apply_unit_multiplier=True,
            expected_dimensions={
                "MEASURE": {
                    "current_prices": "GDP_USD",
                    "volume": "GDPV_USD",
                    "capita": "GDPVD_CAP",
                    "growth": "GDPV_ANNPCT",
                    "deflator": "PGDP",
                }[params.units],
                "FREQ": _FREQUENCIES[params.frequency],
                "UNIT_MEASURE": {
                    "current_prices": "USD_EXC",
                    "volume": "USD_EXC",
                    "capita": "USD_PPP_PS",
                    "growth": "PA",
                    "deflator": "IX",
                }[params.units],
            },
        )


class OecdUnemploymentFetcher(Fetcher[OecdUnemploymentQuery, list[dict[str, object]]]):
    """LFS unemployment rates, with frequency selected from source periods."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "Unemployment"
    row_model: ClassVar[type[ContractModel]] = OecdUnemployment
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_oecd_unemployment",
        period="variable",
        market="global",
        source=SOURCE,
        verified=False,
        notes="offline candidate; cross-source validation not completed",
    )

    def transform_query(self, **kwargs: object) -> OecdUnemploymentQuery:
        """Validate labor-force country, demographic, and frequency selectors."""
        return OecdUnemploymentQuery.model_validate(kwargs)

    def extract_data(
        self, params: OecdUnemploymentQuery, ctx: FetchContext
    ) -> list[dict[str, object]]:
        """Fetch the requested OECD labor-force unemployment series."""
        sex = {"total": "_T", "male": "M", "female": "F"}[params.sex]
        age = {
            "total": "Y_GE15",
            "15-24": "Y15T24",
            "25+": "Y_GE25",
            "15_to_64": "Y15T64",
            "15_or_over": "Y_GE15",
        }[params.age]
        measure = "UNE_LF_M" if params.frequency == "monthly" else "UNE_LF"
        adjustment = "Y" if params.seasonal_adjustment else "N"
        freq = _FREQUENCIES[params.frequency]
        key = (
            f"{_country_key(_country_codes(params.country))}.{measure}.PT_LF_SUB."
            f"_Z.{adjustment}.{sex}.{age}._Z.{freq}"
        )
        return _fetch(UNEMPLOYMENT_FLOW, key, ctx, start=params.start_date, end=params.end_date)

    def transform_data(
        self, raw: list[dict[str, object]], params: OecdUnemploymentQuery
    ) -> FetchResult:
        """Validate unemployment dimensions and map typed observations."""
        return _record_rows(
            raw,
            countries=_country_codes(params.country),
            start=params.start_date,
            end=params.end_date,
            contract=OecdUnemployment,
            frequency=_FREQUENCIES[params.frequency],
            expected_dimensions={
                "FREQ": _FREQUENCIES[params.frequency],
                "MEASURE": "UNE_LF_M" if params.frequency == "monthly" else "UNE_LF",
                "UNIT_MEASURE": "PT_LF_SUB",
                "TRANSFORMATION": "_Z",
                "ADJUSTMENT": "Y" if params.seasonal_adjustment else "N",
                "SEX": {"total": "_T", "male": "M", "female": "F"}[params.sex],
                "AGE": {
                    "total": "Y_GE15",
                    "15-24": "Y15T24",
                    "25+": "Y_GE25",
                    "15_to_64": "Y15T64",
                    "15_or_over": "Y_GE15",
                }[params.age],
                "ACTIVITY": "_Z",
            },
        )


__all__ = [
    "OecdCompositeLeadingIndicatorFetcher",
    "OecdConsumerPriceIndexFetcher",
    "OecdCountryInterestRateFetcher",
    "OecdGdpForecastFetcher",
    "OecdGdpNominalFetcher",
    "OecdGdpRealFetcher",
    "OecdHousePriceIndexFetcher",
    "OecdSharePriceIndexFetcher",
    "OecdUnemploymentFetcher",
]
