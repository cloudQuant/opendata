"""Evidence-bounded FRED macro model adapters for iteration two.

Every selection here has a fixed official FRED series ID. Unsupported public
categories fail query validation instead of being inferred from a nearby
series. Requests use the existing FRED observation pager and shared HTTP
client; real-time fields remain private provenance and never become a
fabricated output ``as_of`` value.
"""

from __future__ import annotations

import io
import math
import re
import zipfile
from datetime import date as date_type
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Literal, TypeGuard, TypeVar

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import Field, ValidationError, field_validator, model_validator

import opendata.core.config as config
from opendata.data.capability import Capability
from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.models.economic_series import (  # noqa: TC001 - Pydantic resolves these aliases at runtime.
    FredFrequency,
    FredTransformUnits,
)
from opendata.data.models.fred_macro_iter2 import (
    FredConsumerPriceIndex,
    FredManufacturingOutlookNY,
    FredManufacturingOutlookTexas,
    FredMortgageIndex,
    FredNonFarmPayroll,
    FredPersonalConsumptionExpenditure,
    FredRetailPrice,
    FredSeniorLoanOfficerSurvey,
    FredSurveyOfEconomicConditionsChicago,
    FredUniversityOfMichigan,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.fred.models import _client
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models._paged import FredQueryLimitError
from opendata.data.providers.fred.models.iter2_auxiliary import (
    FredReleaseTableFetcher,
    _fred_json,
)
from opendata.data.providers.fred.models.series import FredSeriesFetcher, FredSeriesQuery

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel
    from opendata.data.models.fred_iter2 import FredReleaseTable
    from opendata.data.providers._engine.http_json import HttpGet

_DATE_TOKEN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_MAX_TOTAL_PAGES = 20
_MAX_RELEASE_DATES = 10
_FRED_FREQUENCY_VALUES = frozenset(
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


def _is_fred_frequency(value: object) -> TypeGuard[FredFrequency]:
    return value in _FRED_FREQUENCY_VALUES


def _normalize_csv_topics(value: object, allowed_topics: set[str]) -> str:
    """Validate a public comma-separated topic selector before source I/O."""
    if not isinstance(value, str):
        raise ValueError("topic must be a comma-separated string")
    topics: list[str] = []
    seen: set[str] = set()
    for part in value.split(","):
        topic = part.strip()
        if not topic:
            raise ValueError("topic entries must not be empty")
        if topic not in allowed_topics:
            raise ValueError("unsupported manufacturing topic")
        if topic not in seen:
            topics.append(topic)
            seen.add(topic)
    return ",".join(topics)


def _manufacturing_topics(params: QueryParams) -> tuple[str, ...]:
    """Read the already validated, order-preserving topic list."""
    topic = getattr(params, "topic", None)
    if not isinstance(topic, str):  # pragma: no cover - model validators enforce it
        raise ValueError("topic must be normalized before selection")
    return tuple(topic.split(","))


_NFP_ELEMENT_IDS: dict[str, str] = {
    "employees_nsa": "5645",
    "employees_sa": "4881",
    "employees_production_and_nonsupervisory": "6411",
    "employees_women": "6304",
    "employees_women_percent": "6433",
    "avg_hours": "6462",
    "avg_hours_production_and_nonsupervisory": "5923",
    "avg_hours_overtime": "6466",
    "avg_hours_overtime_production_and_nonsupervisory": "6442",
    "avg_earnings_hourly": "6471",
    "avg_earnings_hourly_production_and_nonsupervisory": "5943",
    "avg_earnings_weekly": "6490",
    "avg_earnings_weekly_production_and_nonsupervisory": "6020",
    "index_weekly_hours": "6637",
    "index_weekly_hours_production_and_nonsupervisory": "6095",
    "index_weekly_payrolls": "6654",
    "index_weekly_payrolls_production_and_nonsupervisory": "6280",
}
_PCE_ELEMENT_IDS: dict[str, str] = {
    "personal_income": "155443",
    "wages_by_industry": "3151",
    "real_pce_percent_change": "3160",
    "real_pce_quantity_index": "3196",
    "pce_price_index": "3208",
    "pce_dollars": "3220",
    "real_pce_chained_dollars": "3232",
    "pce_price_percent_change": "3172",
}
_RETAIL_ITEM_CODES: dict[str, str] = {
    "bacon": "704111",
    "bananas": "711211",
    "butter": "FS1101",
    "chops": "FD3101",
    "cookies": "702421",
    "diesel": "74717",
    "electricity": "72610",
    "flour": "701111",
    "gasoline": "7471A",
    "grapefruit": "711411",
    "ground_beef": "FC1101",
    "ham": "FD2101",
    "ice_cream": "710411",
    "lemons": "711412",
    "malt_beverages": "720111",
    "orange_juice": "713111",
    "potato_chips": "718311",
    "spaghetti": "701322",
    "strawberries": "711415",
    "sugar": "715211",
    "unleaded": "7471A",
    "vodka": "720222",
    "wine": "720311",
    "yogurt": "FJ4101",
}
_RETAIL_ITEM_DESCRIPTIONS: dict[str, str] = {
    "bacon": "Bacon, sliced, per lb. (453.6 gm)",
    "bananas": "Bananas, per lb. (453.6 gm)",
    "butter": "Butter, in sticks, per pound",
    "chops": "All pork chops, per lb. (453.6 gm)",
    "cookies": "Cookies, chocolate chip, per lb. (453.6 gm)",
    "diesel": "Automotive diesel fuel, per gallon/3.785 liters",
    "electricity": "Average price per KWH of electricity",
    "flour": "Flour, white, all purpose, per lb. (453.6 gm)",
    "gasoline": "Gasoline, all types, per gallon/3.785 liters",
    "grapefruit": "Grapefruit, per lb. (453.6 gm)",
    "ground_beef": "All uncooked ground beef, per lb. (453.6 gm)",
    "ham": "All ham (excluding canned ham and luncheon slices), per lb. (453.6 gm)",
    "ice_cream": "Ice cream, prepackaged, bulk, regular, per 1/2 gal (1.9 lit)",
    "lemons": "Lemons, per lb. (453.6 gm)",
    "malt_beverages": "Malt beverages, all types, all sizes, any origin, per 16 oz. (473.2 ml)",
    "orange_juice": "Orange juice, frozen concentrate, 12 oz. can, per 16 oz. (473.2 ml)",
    "potato_chips": "Potato chips, per 16 oz.",
    "spaghetti": "Spaghetti and macaroni, per lb. (453.6 gm)",
    "strawberries": "Strawberries, dry pint, per 12 oz. (340.2 gm)",
    "sugar": "Sugar, white, all sizes, per lb. (453.6 gm)",
    "unleaded": "Gasoline, all types, per gallon/3.785 liters",
    "vodka": "Vodka, all types, all sizes, any origin, per 1 liter (33.8 oz.)",
    "wine": "Wine, red and white table, all sizes, any origin, per 1 liter (33.8 oz.)",
    "yogurt": "Yogurt, per 8 ounce",
}
_RETAIL_AREA_CODES: dict[str, str] = {
    "all_city": "0000",
    "northeast": "0100",
    "midwest": "0200",
    "south": "0300",
    "west": "0400",
}
_RETAIL_AREA_NAMES: dict[str, str] = {
    "all_city": "U.S. city average",
    "northeast": "Northeast",
    "midwest": "Midwest",
    "south": "South",
    "west": "West",
}
_RETAIL_SELECTOR_GAPS: dict[str, str] = {
    "beverages": (
        "the public selector does not define whether this basket includes "
        "coffee, soft drinks, or alcoholic beverages"
    ),
    "cereals": (
        "BLS groups cereals with bakery products; the public selector does not "
        "define the basket membership"
    ),
    "dairy": (
        "BLS has a dairy-products category, but the public selector does not "
        "define whether it selects the complete category or a single item"
    ),
    "fuel": (
        "the default full fuel basket is not fully mapped; BLS Energy contains "
        "utility energy, electricity, gasoline, and diesel, and the public "
        "selector does not define its membership"
    ),
    "produce": (
        "BLS separates fresh and processed produce; the public selector does "
        "not define the included items"
    ),
    "meats": (
        "BLS splits beef, pork/other meat, and poultry/fish/eggs; the public "
        "selector does not define which groups to include"
    ),
    "beans": "BLS has dried beans, green beans, and pork-and-beans series",
    "beef": (
        "BLS exposes many overlapping beef cuts and composites; no single all-beef item is defined"
    ),
    "beer": (
        "the BLS malt-beverages series also includes sake and cider, so it is "
        "not an exact beer-only series"
    ),
    "bread": "BLS has multiple bread types, including white, French, and whole wheat",
    "cheese": "BLS has separate American processed and natural cheddar series",
    "chicken": "BLS has whole chicken, breast, and legs series",
    "coffee": "BLS has ground, instant, and multiple package-specific coffee series",
    "corn": "BLS has canned corn and a separate fresh corn-on-the-cob series",
    "eggs": "BLS has separate Grade A and Grade AA egg series",
    "gas": (
        "BLS has several piped-gas quantity series and a per-therm series; "
        "the public selector does not specify the unit"
    ),
    "lettuce": "BLS has separate iceberg and romaine series",
    "milk": "BLS has whole, low-fat, and skim milk series with different package sizes",
    "oil": (
        "BLS publishes fuel oil #2; the public selector does not define "
        "whether other oils are included"
    ),
    "oranges": "BLS has separate Navel and Valencia orange series",
    "pork": (
        "BLS separates pork chops and other-pork series; the public selector "
        "does not define the basket"
    ),
    "potatoes": "BLS separates white potatoes from frozen French fries",
    "rice": "BLS lists precooked and uncooked white long-grain rice series",
    "soft_drinks": (
        "BLS has separate 2-liter and 12-pack soft-drink series with different unit bases"
    ),
    "steak": "BLS has multiple cuts, grades, and discontinued steak series",
    "tomatoes": "BLS has fresh field-grown and canned tomato series",
    "usda": "BLS has several USDA Choice/Prime meat series, not one defined USDA basket",
}
_RETAIL_ITEM_CHOICES = (
    "beverages",
    "cereals",
    "dairy",
    "fuel",
    "produce",
    "meats",
    "bacon",
    "bananas",
    "beans",
    "beef",
    "beer",
    "bread",
    "butter",
    "cheese",
    "chicken",
    "chops",
    "coffee",
    "cookies",
    "corn",
    "diesel",
    "eggs",
    "electricity",
    "flour",
    "gas",
    "gasoline",
    "grapefruit",
    "ground_beef",
    "ham",
    "ice_cream",
    "lemons",
    "lettuce",
    "malt_beverages",
    "milk",
    "oil",
    "orange_juice",
    "oranges",
    "pork",
    "potato_chips",
    "potatoes",
    "rice",
    "soft_drinks",
    "spaghetti",
    "steak",
    "strawberries",
    "sugar",
    "tomatoes",
    "unleaded",
    "usda",
    "vodka",
    "wine",
    "yogurt",
)
_SLOOS_SELECTOR_FILTERS: dict[str, tuple[tuple[str, frozenset[str]], ...]] = {
    "spreads": (("MEASURE", frozenset({"TERMS"})), ("TERMS", frozenset({"SPREAD"}))),
    "consumer": (
        (
            "LOANTYPE",
            frozenset({"AUTO", "CC", "CONS", "INST", "NOCCAU", "NOTCC"}),
        ),
    ),
    "auto": (("LOANTYPE", frozenset({"AUTO"})),),
    "credit_card": (("LOANTYPE", frozenset({"CC"})),),
    "firms": (("LOANTYPE", frozenset({"CI", "CIINQ", "CILG", "CISM"})),),
    "mortgage": (
        (
            "LOANTYPE",
            frozenset(
                {
                    "GOV",
                    "GSE",
                    "MORT",
                    "NONTRAD",
                    "NQMJ",
                    "NQMNJ",
                    "PRIME",
                    "QMJ",
                    "QMNJNGSE",
                    "SPRIME",
                }
            ),
        ),
    ),
    "commercial_real_estate": (("LOANTYPE", frozenset({"CRE", "CRECLD", "CREMFR", "CRENFNR"})),),
    "standards": (("MEASURE", frozenset({"STND"})),),
    "demand": (("MEASURE", frozenset({"DEMAND"})),),
    "foreign_banks": (("PANEL", frozenset({"FGN"})),),
}
_SLOOS_ALLOWED_DIMENSIONS: dict[str, frozenset[str]] = {
    "PANEL": frozenset({"DOM", "FGN"}),
    "BANKSIZE": frozenset({"ALL", "LG", "OTH"}),
    "LOANTYPE": frozenset(
        {
            "CIINQ",
            "CI",
            "CILG",
            "CISM",
            "AUTO",
            "CC",
            "INST",
            "NOTCC",
            "NOCCAU",
            "CONS",
            "HELOC",
            "MORT",
            "NONTRAD",
            "PRIME",
            "SPRIME",
            "GSE",
            "GOV",
            "QMNJNGSE",
            "QMJ",
            "NQMJ",
            "NQMNJ",
            "CRE",
            "CRENFNR",
            "CRECLD",
            "CREMFR",
        }
    ),
    "MEASURE": frozenset({"INQUIRY", "DEMAND", "STND", "WILLING", "WGTWILL", "TERMS"}),
    "TERMS": frozenset(
        {
            "NA",
            "COL",
            "COST",
            "LIMITS",
            "EXCEPT",
            "COV",
            "MAT",
            "LINE",
            "PAY",
            "SCORE",
            "DOWN",
            "RISK",
            "SPREAD",
            "MAXCL",
            "INTFL",
        }
    ),
}
_SLOOS_SELECTOR_CHOICES = (
    "spreads",
    "consumer",
    "auto",
    "credit_card",
    "firms",
    "mortgage",
    "commercial_real_estate",
    "standards",
    "demand",
    "foreign_banks",
)
_SLOOS_DOWNLOAD_URL = "https://www.federalreserve.gov/DataDownload/Output.aspx"
_SLOOS_DATASET_ID = "http://www.federalreserve.gov/structure/compact/SLOOS_SLOOS"
_SLOOS_FRB_NS = "http://www.federalreserve.gov/structure/compact/common"
_SLOOS_COMMON_NS = "http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common"
_SLOOS_QUERY_NS = "http://www.federalreserve.gov/structure/compact/SLOOS_SLOOS"
_MAX_SLOOS_ARCHIVE_BYTES = 2_000_000
_MAX_SLOOS_XML_BYTES = 8_000_000
_MAX_SLOOS_SERIES = 1_000
_MAX_SLOOS_OBSERVATIONS_PER_SERIES = 1_000
_MORTGAGE_GROUPS: dict[str, tuple[str, ...]] = {
    "ltv_lte_80": (
        "ltv_lte80_fico_ge740",
        "ltv_lte80_fico_a720b739",
        "ltv_lte80_fico_a700b719",
        "ltv_lte80_fico_a680b699",
        "ltv_lte80_fico_lt680",
    ),
    "ltv_gt_80": (
        "ltv_gt80_fico_ge740",
        "ltv_gt80_fico_a720b739",
        "ltv_gt80_fico_a700b719",
        "ltv_gt80_fico_a680b699",
        "ltv_gt80_fico_lt680",
    ),
}


class _MacroQuery(QueryParams):
    """Common bounded date, revision and paging options for fixed FRED IDs."""

    source: Literal["auto", "fred"] = "auto"
    market: Literal["us"] | None = None
    symbol: None = None
    realtime_start: date_type | None = None
    realtime_end: date_type | None = None
    as_of: date_type | None = None
    vintage_dates: list[date_type] | None = None
    sort_order: Literal["asc", "desc"] = "asc"
    page_size: int = Field(default=100_000, ge=1, le=100_000, strict=True)
    offset: int = Field(default=0, ge=0, strict=True)
    max_records: int = Field(default=10_000, ge=1, le=10_000, strict=True)
    max_pages: int = Field(default=2, ge=1, le=2, strict=True)

    @field_validator(
        "start_date",
        "end_date",
        "as_of",
        "realtime_start",
        "realtime_end",
        mode="before",
    )
    @classmethod
    def _validate_strict_date(cls, value: object) -> date_type | None:
        if value is None:
            return None
        return _strict_date(value)

    @field_validator("vintage_dates", mode="before")
    @classmethod
    def _validate_vintage_dates(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, (list, tuple)) or len(value) != 1:
            raise ValueError("vintage_dates must contain one date for the date-keyed output")
        return [_strict_date(item) for item in value]

    @model_validator(mode="after")
    def _validate_windows(self) -> _MacroQuery:
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
        has_realtime_window = self.realtime_start is not None or self.realtime_end is not None
        if self.as_of is not None and has_realtime_window:
            raise ValueError("as_of cannot be combined with a real-time window")
        if self.vintage_dates is not None and (self.as_of is not None or has_realtime_window):
            raise ValueError("vintage_dates cannot be combined with another real-time selector")
        return self


_MacroQueryT = TypeVar("_MacroQueryT", bound=_MacroQuery)


class _ObservedMacroQuery(_MacroQuery):
    """Common FRED optional aggregation method."""

    aggregation_method: Literal["avg", "sum", "eop"] = "avg"


_MortgageIndex = Literal[
    "primary",
    "ltv_lte_80",
    "ltv_gt_80",
    "conforming_30y",
    "conforming_30y_na",
    "jumbo_30y",
    "fha_30y",
    "va_30y",
    "usda_30y",
    "conforming_15y",
    "ltv_lte80_fico_ge740",
    "ltv_lte80_fico_a720b739",
    "ltv_lte80_fico_a700b719",
    "ltv_lte80_fico_a680b699",
    "ltv_lte80_fico_lt680",
    "ltv_gt80_fico_ge740",
    "ltv_gt80_fico_a720b739",
    "ltv_gt80_fico_a700b719",
    "ltv_gt80_fico_a680b699",
    "ltv_gt80_fico_lt680",
]


def _default_mortgage_indices() -> list[_MortgageIndex]:
    return ["primary"]


class ConsumerPriceIndexQuery(_ObservedMacroQuery):
    """Only the U.S. all-items CPI series is mapped in this slice."""

    country: Literal["united_states"] = "united_states"
    expenditure: Literal["total"] = "total"
    harmonized: Literal[False] = False
    transform: Literal["index", "yoy", "period"] = "yoy"
    frequency: Literal["annual", "quarter", "monthly"] = "monthly"
    limit: int | None = Field(default=None, ge=1, le=10_000, strict=True)


class _ReleaseDateQuery(_MacroQuery):
    """Bounded exact-date queries against an official FRED release table."""

    date: list[date_type | None] | None = None
    max_nodes: int = Field(default=10_000, ge=1, le=10_000, strict=True)
    max_pages: int = Field(default=_MAX_RELEASE_DATES, ge=1, le=_MAX_RELEASE_DATES, strict=True)

    @field_validator("date", mode="before")
    @classmethod
    def _parse_dates(cls, value: object) -> list[date_type | None] | None:
        return _parse_date_list(value)

    @model_validator(mode="after")
    def _reject_unavailable_selectors(self) -> _ReleaseDateQuery:
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("release-table models use the exact date selector")
        if any(
            getattr(self, name, None) is not None
            for name in (
                "as_of",
                "realtime_start",
                "realtime_end",
                "vintage_dates",
                "frequency",
                "transform",
            )
        ):
            raise ValueError("release-table models do not support series transformations")
        if len(self.selected_dates()) > min(_MAX_RELEASE_DATES, self.max_pages):
            raise ValueError("release-table date fan-out exceeds the request budget")
        return self

    def selected_dates(self) -> tuple[date_type | None, ...]:
        return (None,) if self.date is None else tuple(self.date)


_ReleaseTableQueryT = TypeVar("_ReleaseTableQueryT", bound=_ReleaseDateQuery)


class NonFarmPayrollsQuery(_ReleaseDateQuery):
    """All documented nonfarm-payroll release table categories."""

    category: Literal[
        "employees_nsa",
        "employees_sa",
        "employees_production_and_nonsupervisory",
        "employees_women",
        "employees_women_percent",
        "avg_hours",
        "avg_hours_production_and_nonsupervisory",
        "avg_hours_overtime",
        "avg_hours_overtime_production_and_nonsupervisory",
        "avg_earnings_hourly",
        "avg_earnings_hourly_production_and_nonsupervisory",
        "avg_earnings_weekly",
        "avg_earnings_weekly_production_and_nonsupervisory",
        "index_weekly_hours",
        "index_weekly_hours_production_and_nonsupervisory",
        "index_weekly_payrolls",
        "index_weekly_payrolls_production_and_nonsupervisory",
    ] = "employees_nsa"


class PersonalConsumptionExpendituresQuery(_ReleaseDateQuery):
    """PCE category selector and exact observation-date filter."""

    category: Literal[
        "personal_income",
        "wages_by_industry",
        "real_pce_percent_change",
        "real_pce_quantity_index",
        "pce_price_index",
        "pce_dollars",
        "real_pce_chained_dollars",
        "pce_price_percent_change",
    ] = "personal_income"


class UniversityOfMichiganQuery(_ObservedMacroQuery):
    """Consumer sentiment and next-12-month inflation expectations."""

    frequency: Literal["annual", "quarter", "m", "q", "a"] | None = None
    transform: FredTransformUnits | None = None


class MortgageIndicesQuery(_ObservedMacroQuery):
    """Explicit OBMMI series and documented LTV/FICO selector groups."""

    frequency: FredFrequency | None = None
    transform: FredTransformUnits | None = None
    index: list[_MortgageIndex] = Field(default_factory=_default_mortgage_indices)

    @field_validator("index", mode="before")
    @classmethod
    def _parse_index_list(cls, value: object) -> object:
        if value is None:
            return ["primary"]
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("index must be a non-empty list or comma-separated string")
        choices = [item.strip() if isinstance(item, str) else item for item in value]
        if any(not item for item in choices):
            raise ValueError("index values must not be empty")
        if len(set(choices)) != len(choices):
            raise ValueError("index values must be unique")
        if "primary" in choices and len(choices) != 1:
            raise ValueError("primary cannot be combined with an explicit index")
        for group, members in _MORTGAGE_GROUPS.items():
            if group in choices and any(member in choices for member in members):
                raise ValueError("mortgage LTV groups cannot be combined with their members")
        return choices

    @model_validator(mode="after")
    def _native_daily_frequency(self) -> MortgageIndicesQuery:
        if self.frequency is not None and self.frequency not in {
            "d",
            "w",
            "bw",
            "m",
            "q",
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
        }:
            raise ValueError("unsupported FRED frequency aggregation")
        return self


class ManufacturingOutlookNYQuery(_ObservedMacroQuery):
    """Documented Empire State topics and seasonal modes."""

    topic: str = "new_orders"
    seasonally_adjusted: bool = False
    frequency: Literal["annual", "quarter", "m", "q", "a"] | None = None
    transform: FredTransformUnits | None = None

    @field_validator("topic", mode="before")
    @classmethod
    def _normalize_topic(cls, value: object) -> str:
        return _normalize_csv_topics(
            value,
            {
                "business_outlook",
                "hours_worked",
                "employment",
                "inventories",
                "prices_received",
                "prices_paid",
                "capex",
                "unfilled_orders",
                "new_orders",
                "shipments",
                "delivery_times",
            },
        )


class ManufacturingOutlookTexasQuery(_ObservedMacroQuery):
    """Documented Dallas Fed topics and official seasonal modes."""

    topic: str = "new_orders_growth"
    seasonally_adjusted: bool = False
    frequency: Literal["annual", "quarter", "m", "q", "a"] | None = None
    transform: FredTransformUnits | None = None

    @field_validator("topic", mode="before")
    @classmethod
    def _normalize_topic(cls, value: object) -> str:
        return _normalize_csv_topics(
            value,
            {
                "business_activity",
                "business_outlook",
                "capex",
                "prices_paid",
                "production",
                "inventory",
                "new_orders",
                "new_orders_growth",
                "unfilled_orders",
                "shipments",
                "delivery_time",
                "employment",
                "wages",
                "hours_worked",
            },
        )


class SurveyOfEconomicConditionsChicagoQuery(_ObservedMacroQuery):
    """Nine monthly Chicago survey components from their released series."""

    frequency: Literal["annual", "quarter", "m", "q", "a"] | None = None
    transform: FredTransformUnits | None = None


class RetailPricesQuery(_ObservedMacroQuery):
    """Exact BLS average-price selectors with a proven single-series mapping."""

    item: (
        Literal[
            "beverages",
            "cereals",
            "dairy",
            "fuel",
            "produce",
            "meats",
            "bacon",
            "bananas",
            "beans",
            "beef",
            "beer",
            "bread",
            "butter",
            "cheese",
            "chicken",
            "chops",
            "coffee",
            "cookies",
            "corn",
            "diesel",
            "eggs",
            "electricity",
            "flour",
            "gas",
            "gasoline",
            "grapefruit",
            "ground_beef",
            "ham",
            "ice_cream",
            "lemons",
            "lettuce",
            "malt_beverages",
            "milk",
            "oil",
            "orange_juice",
            "oranges",
            "pork",
            "potato_chips",
            "potatoes",
            "rice",
            "soft_drinks",
            "spaghetti",
            "steak",
            "strawberries",
            "sugar",
            "tomatoes",
            "unleaded",
            "usda",
            "vodka",
            "wine",
            "yogurt",
        ]
        | None
    ) = "fuel"
    country: Literal["united_states"] | None = "united_states"
    region: Literal["all_city", "northeast", "midwest", "south", "west"] = "all_city"
    frequency: Literal["annual", "quarter", "monthly"] | None = "monthly"
    transform: FredTransformUnits | None = None

    @model_validator(mode="after")
    def _require_exact_item_mapping(self) -> RetailPricesQuery:
        if self.item is None:
            raise ValueError("item=None selects the unsupported default full fuel basket")
        if self.item not in _RETAIL_ITEM_CODES:
            reason = _RETAIL_SELECTOR_GAPS.get(
                self.item,
                "the public selector has no verified single-series BLS mapping",
            )
            raise ValueError(f"RetailPrices item '{self.item}' is not mapped: {reason}")
        if self.country != "united_states":
            raise ValueError("only United States BLS average-price series are mapped")
        return self


class SeniorLoanOfficerSurveyQuery(_MacroQuery):
    """Official Fed SLOOS groups selected through the release's dimensions."""

    category: Literal[
        "spreads",
        "consumer",
        "auto",
        "credit_card",
        "firms",
        "mortgage",
        "commercial_real_estate",
        "standards",
        "demand",
        "foreign_banks",
    ] = "spreads"
    transform: FredTransformUnits | None = None
    max_records: int = Field(default=10_000, ge=1, le=50_000, strict=True)

    @model_validator(mode="after")
    def _reject_revision_selectors(self) -> SeniorLoanOfficerSurveyQuery:
        if any(
            getattr(self, name) is not None
            for name in ("as_of", "realtime_start", "realtime_end", "vintage_dates")
        ):
            raise ValueError("the official SLOOS bulk release has no historical real-time vintages")
        return self


class _FixedMacroFetcher(Fetcher[_MacroQueryT, list[dict[str, Any]]], Generic[_MacroQueryT]):
    """Fetch only this model's fixed official series through the FRED pager."""

    async_mode = "bounded_thread"
    query_model: ClassVar[type[_MacroQueryT]]
    canonical_model: ClassVar[str | None]
    capability: ClassVar[Capability]
    row_model: ClassVar[type[ContractModel]]
    #: Offline witness seam; ``None`` keeps the governed production HTTP client path.
    http_transport: ClassVar[HttpGet | None] = None

    def transform_query(self, **kwargs: object) -> _MacroQueryT:
        return self.query_model.model_validate(kwargs)

    def selected_series(self, params: _MacroQueryT) -> list[tuple[str, str]]:
        raise NotImplementedError

    def source_frequency(self, params: _MacroQueryT) -> FredFrequency | None:
        frequency = getattr(params, "frequency", None)
        if frequency == "annual":
            return "a"
        if frequency == "quarter":
            return "q"
        if frequency == "monthly":
            return "m"
        if frequency is None:
            return None
        if _is_fred_frequency(frequency):
            return frequency
        raise FredProviderError("FRED_BAD_QUERY") from None

    def source_transform(self, params: _MacroQueryT) -> FredTransformUnits:
        return getattr(params, "transform", None) or "lin"

    def extract_data(self, params: _MacroQueryT, ctx: FetchContext) -> list[dict[str, Any]]:
        checked = self._checked_query(params)
        selections = self.selected_series(checked)
        if not selections or len(selections) * checked.max_pages > _MAX_TOTAL_PAGES:
            raise FredProviderError("FRED_BAD_QUERY") from None
        rows: list[dict[str, Any]] = []
        try:
            for selection, series_id in selections:
                query_values = checked.model_dump(mode="python")
                query_fields = {
                    name: query_values[name]
                    for name in FredSeriesQuery.model_fields
                    if name in query_values
                }
                query = FredSeriesQuery.model_validate(
                    {
                        **query_fields,
                        "series_id": series_id,
                        "transform_units": self.source_transform(checked),
                        "output_type": 1,
                        "frequency": self.source_frequency(checked),
                        "aggregation_method": getattr(checked, "aggregation_method", "avg"),
                    }
                )
                inner = FredSeriesFetcher()
                inner.http_transport = self.http_transport
                observations = inner.extract_data(query, ctx)
                rows.extend(
                    {
                        "_selection": selection,
                        "_series_id": series_id,
                        "observation": observation,
                    }
                    for observation in observations
                )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_QUERY") from exc
        return rows

    def _checked_query(self, params: object) -> _MacroQueryT:
        if type(params) is not self.query_model:
            raise FredProviderError("FRED_BAD_QUERY") from None
        try:
            return self.query_model.model_validate(params.model_dump(mode="python"))
        except (AttributeError, RecursionError, TypeError, ValueError, ValidationError):
            raise FredProviderError("FRED_BAD_QUERY") from None

    def _validate_raw(
        self, raw: object, params: _MacroQueryT
    ) -> list[tuple[str, str, date_type, float | None, date_type, date_type]]:
        if type(raw) is not list:
            raise FredProviderError("FRED_BAD_OBSERVATION") from None
        expected = set(self.selected_series(params))
        validated: list[tuple[str, str, date_type, float | None, date_type, date_type]] = []
        seen: set[tuple[str, date_type]] = set()
        try:
            for item in raw:
                if type(item) is not dict or type(item.get("observation")) is not dict:
                    raise ValueError("observation wrapper has the wrong shape")
                selection = item["_selection"]
                series_id = item["_series_id"]
                if (selection, series_id) not in expected:
                    raise ValueError("unrequested fixed series identity")
                observation = item["observation"]
                date = _strict_date(observation["date"])
                realtime_start = _strict_date(observation["realtime_start"])
                realtime_end = _strict_date(observation["realtime_end"])
                if realtime_start > realtime_end:
                    raise ValueError("real-time interval is reversed")
                if params.start_date is not None and date < params.start_date:
                    raise ValueError("observation precedes requested start date")
                if params.end_date is not None and date > params.end_date:
                    raise ValueError("observation follows requested end date")
                _validate_vintage(realtime_start, realtime_end, params)
                identity = (series_id, date)
                if identity in seen:
                    raise ValueError("multiple revisions do not fit the output key")
                seen.add(identity)
                validated.append(
                    (
                        selection,
                        series_id,
                        date,
                        _parse_number(observation["value"]),
                        realtime_start,
                        realtime_end,
                    )
                )
            date_filter = getattr(params, "date", None)
            if date_filter is not None:
                allowed_dates = {item for item in date_filter if item is not None}
                validated = [row for row in validated if row[2] in allowed_dates]
        except (KeyError, TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_OBSERVATION") from exc
        return validated

    @staticmethod
    def _sort(rows: list[Any], params: QueryParams) -> tuple[Any, ...]:
        return tuple(
            sorted(
                rows,
                key=lambda row: (row.date, getattr(row, "symbol", "")),
                reverse=getattr(params, "sort_order", "asc") == "desc",
            )
        )

    def _merge_topic_fields(
        self,
        raw: object,
        params: _MacroQueryT,
        topic_to_series: dict[str, dict[str, str]],
        output_model: type[Any],
    ) -> FetchResult:
        """Merge one row per topic/date without combining survey topics."""
        expected = set(self.selected_series(params))
        series_to_output: dict[str, tuple[str, str]] = {}
        for topic, field_to_series in topic_to_series.items():
            for field, series_id in field_to_series.items():
                if series_id in series_to_output:
                    raise FredProviderError("FRED_BAD_QUERY") from None
                series_to_output[series_id] = (topic, field)

        by_topic_date: dict[tuple[date_type, str], dict[str, Any]] = {}
        for selection, series_id, date, value, realtime_start, realtime_end in self._validate_raw(
            raw, params
        ):
            identity = series_to_output.get(series_id)
            if (
                (selection, series_id) not in expected
                or identity is None
                or selection != identity[1]
            ):
                raise FredProviderError("FRED_BAD_OBSERVATION") from None
            topic, field = identity
            row_values = by_topic_date.setdefault((date, topic), {"date": date, "topic": topic})
            if field in row_values:
                raise FredProviderError("FRED_BAD_OBSERVATION") from None
            row_values[field] = value
            realtime = row_values.setdefault("_realtime", {})
            realtime[series_id] = (realtime_start, realtime_end)

        rows = []
        try:
            for _, values in sorted(by_topic_date.items()):
                realtime = values.pop("_realtime")
                row = output_model.model_validate(values)
                row._series_ids = tuple(sorted(realtime))
                row._realtime_by_series = realtime
                rows.append(row)
        except (TypeError, ValueError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_OBSERVATION") from exc
        if getattr(params, "sort_order", "asc") == "desc":
            rows.reverse()
        return tuple(rows)

    def _merge_fields(
        self,
        raw: object,
        params: _MacroQueryT,
        field_to_series: dict[str, str],
        output_model: type[Any],
        *,
        fixed_fields: dict[str, object] | None = None,
    ) -> FetchResult:
        series_to_field = {series_id: field for field, series_id in field_to_series.items()}
        expected = set(self.selected_series(params))
        by_date: dict[date_type, dict[str, Any]] = {}
        for selection, series_id, date, value, realtime_start, realtime_end in self._validate_raw(
            raw, params
        ):
            if (selection, series_id) not in expected or series_id not in series_to_field:
                raise FredProviderError("FRED_BAD_OBSERVATION") from None
            row_values = by_date.setdefault(date, {"date": date, **(fixed_fields or {})})
            field = series_to_field[series_id]
            if field in row_values:
                raise FredProviderError("FRED_BAD_OBSERVATION") from None
            row_values[field] = value
            realtime = row_values.setdefault("_realtime", {})
            realtime[series_id] = (realtime_start, realtime_end)
        rows = []
        try:
            for values in by_date.values():
                realtime = values.pop("_realtime")
                row = output_model.model_validate(values)
                row._series_ids = tuple(sorted(realtime))
                row._realtime_by_series = realtime
                rows.append(row)
        except (TypeError, ValueError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_OBSERVATION") from exc
        return self._sort(rows, params)


class _ReleaseTableMacroFetcher(
    _FixedMacroFetcher[_ReleaseTableQueryT], Generic[_ReleaseTableQueryT]
):
    """Adapt one fixed category from a bounded FRED release-table request."""

    release_id: ClassVar[str]
    output_model: ClassVar[type[Any]]
    category_element_ids: ClassVar[dict[str, str]]

    def selected_series(self, params: _ReleaseTableQueryT) -> list[tuple[str, str]]:
        category: object = getattr(params, "category", None)
        if not isinstance(category, str):
            raise FredProviderError("FRED_BAD_QUERY") from None
        element_id = self.category_element_ids.get(category)
        if element_id is None:
            raise FredProviderError("FRED_BAD_QUERY") from None
        return [(category, element_id)]

    def extract_data(self, params: _ReleaseTableQueryT, ctx: FetchContext) -> list[dict[str, Any]]:
        checked = self._checked_query(params)
        selections = self.selected_series(checked)
        dates = checked.selected_dates()
        if not dates or len(dates) > _MAX_RELEASE_DATES:
            raise FredProviderError("FRED_BAD_QUERY") from None
        category, element_id = selections[0]
        batches: list[dict[str, Any]] = []
        for observation_date in dates:
            request: dict[str, str] = {
                "release_id": self.release_id,
                "element_id": element_id,
                "include_observation_values": "true",
            }
            if observation_date is not None:
                request["observation_date"] = observation_date.isoformat()
            payload = _fred_json("/fred/release/tables", request, ctx.timeout)
            batches.append(
                {
                    "_selection": category,
                    "_element_id": element_id,
                    "requested_date": observation_date,
                    "payload": payload,
                    "release_id": self.release_id,
                    "max_nodes": checked.max_nodes,
                }
            )
        return batches

    def release_table_records(self, raw: object, params: _ReleaseTableQueryT) -> tuple[Any, ...]:
        checked = self._checked_query(params)
        selection, element_id = self.selected_series(checked)[0]
        if type(raw) is not list or len(raw) != len(checked.selected_dates()):
            raise FredProviderError("FRED_BAD_OBSERVATION") from None
        for batch, requested_date in zip(raw, checked.selected_dates(), strict=True):
            if (
                type(batch) is not dict
                or batch.get("_selection") != selection
                or batch.get("_element_id") != element_id
                or batch.get("release_id") != self.release_id
                or batch.get("requested_date") != requested_date
                or batch.get("max_nodes") != checked.max_nodes
            ):
                raise FredProviderError("FRED_BAD_OBSERVATION") from None
        table_fetcher = FredReleaseTableFetcher()
        table_rows: list[Any] = []
        for offset in range(0, len(raw), 3):
            chunk = raw[offset : offset + 3]
            chunk_dates = [batch["requested_date"] for batch in chunk]
            table_query = table_fetcher.transform_query(
                release_id=self.release_id,
                element_id=element_id,
                date=chunk_dates,
                max_nodes=checked.max_nodes,
            )
            table_rows.extend(table_fetcher.transform_data(chunk, table_query))
        return tuple(table_rows)

    def release_contract_values(
        self, source_row: FredReleaseTable, *, include_line: bool
    ) -> dict[str, object]:
        if (
            source_row.element_type != "series"
            or source_row.date is None
            or source_row.symbol is None
            or source_row.value is None
            or source_row.name is None
            or source_row.element_id is None
            or source_row.level is None
        ):
            raise FredProviderError("FRED_BAD_OBSERVATION") from None
        values: dict[str, object] = {
            "date": source_row.date,
            "symbol": source_row.symbol,
            "value": source_row.value,
            "name": source_row.name,
            "element_id": source_row.element_id,
            "parent_id": source_row.parent_id,
            "children": source_row.children,
            "level": source_row.level,
        }
        if include_line:
            if source_row.line is None:
                raise FredProviderError("FRED_BAD_OBSERVATION") from None
            values["line"] = source_row.line
        return values


class ConsumerPriceIndexFetcher(_FixedMacroFetcher[ConsumerPriceIndexQuery]):
    """Fetch the fixed U.S. all-items CPI series and expose its query mapping."""

    canonical_model = "ConsumerPriceIndex"
    capability = Capability(
        asset_class="macro",
        domain="fred_consumer_price_index",
        period="1M",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = ConsumerPriceIndexQuery
    row_model: ClassVar[type[ContractModel]] = FredConsumerPriceIndex

    def selected_series(self, params: ConsumerPriceIndexQuery) -> list[tuple[str, str]]:
        """Return the one FRED CPI series selected by this query."""
        return [(params.expenditure, "CPIAUCSL")]

    def source_transform(self, params: ConsumerPriceIndexQuery) -> FredTransformUnits:
        """Translate the public transform choice to FRED units syntax."""
        transforms: dict[str, FredTransformUnits] = {
            "index": "lin",
            "yoy": "pc1",
            "period": "pch",
        }
        return transforms[params.transform]

    def extract_data(
        self, params: ConsumerPriceIndexQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch bounded CPI observations, honoring the optional output limit."""
        checked = self._checked_query(params)
        if checked.limit is None:
            return super().extract_data(checked, ctx)
        if type(checked) is not ConsumerPriceIndexQuery:
            raise FredProviderError("FRED_BAD_QUERY") from None
        selection, series_id = self.selected_series(checked)[0]
        query_values = checked.model_dump(mode="python")
        query_fields = {
            name: query_values[name]
            for name in FredSeriesQuery.model_fields
            if name in query_values
        }
        series_query = FredSeriesQuery.model_validate(
            {
                **query_fields,
                "series_id": series_id,
                "transform_units": self.source_transform(checked),
                "output_type": 1,
                "frequency": self.source_frequency(checked),
                "aggregation_method": checked.aggregation_method,
            }
        )
        return _fetch_limited_observations(
            series_query,
            selection=selection,
            limit=checked.limit,
            timeout=ctx.timeout,
        )

    def transform_data(
        self, raw: list[dict[str, Any]], params: ConsumerPriceIndexQuery
    ) -> FetchResult:
        """Validate and serialize CPI observations with their source vintages."""
        rows: list[FredConsumerPriceIndex] = []
        for _selection, series_id, date, value, realtime_start, realtime_end in self._validate_raw(
            raw, params
        ):
            if value is None:
                raise FredProviderError("FRED_BAD_OBSERVATION") from None
            row = FredConsumerPriceIndex.model_validate(
                {"date": date, "country": params.country, "value": value}
            )
            row._series_ids = (series_id,)
            row._realtime_by_series = {series_id: (realtime_start, realtime_end)}
            rows.append(row)
        ordered = self._sort(rows, params)
        return ordered if params.limit is None else ordered[: params.limit]


class NonFarmPayrollsFetcher(_ReleaseTableMacroFetcher[NonFarmPayrollsQuery]):
    """Fetch a selected nonfarm-payroll category from the BLS release table."""

    canonical_model = "NonFarmPayrolls"
    capability = Capability(
        asset_class="macro",
        domain="fred_nonfarm_payrolls",
        period="1M",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = NonFarmPayrollsQuery
    release_id = "50"
    output_model = FredNonFarmPayroll
    row_model: ClassVar[type[ContractModel]] = FredNonFarmPayroll
    category_element_ids = _NFP_ELEMENT_IDS

    def selected_series(self, params: NonFarmPayrollsQuery) -> list[tuple[str, str]]:
        """Return the official release-table element for the category."""
        return [(params.category, self.category_element_ids[params.category])]

    def transform_data(
        self, raw: list[dict[str, Any]], params: NonFarmPayrollsQuery
    ) -> FetchResult:
        """Convert selected release-table series rows to the typed contract."""
        rows: list[FredNonFarmPayroll] = []
        for source_row in self.release_table_records(raw, params):
            if source_row.element_type != "series":
                continue
            try:
                rows.append(
                    FredNonFarmPayroll.model_validate(
                        self.release_contract_values(source_row, include_line=False)
                    )
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise FredProviderError("FRED_BAD_OBSERVATION") from exc
        return tuple(rows)


class PersonalConsumptionExpendituresFetcher(
    _ReleaseTableMacroFetcher[PersonalConsumptionExpendituresQuery]
):
    """Fetch a selected PCE category from the BEA release table."""

    canonical_model = "PersonalConsumptionExpenditures"
    capability = Capability(
        asset_class="macro",
        domain="fred_personal_consumption_expenditures",
        period="variable",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = PersonalConsumptionExpendituresQuery
    release_id = "54"
    output_model = FredPersonalConsumptionExpenditure
    row_model: ClassVar[type[ContractModel]] = FredPersonalConsumptionExpenditure
    category_element_ids = _PCE_ELEMENT_IDS

    def selected_series(
        self, params: PersonalConsumptionExpendituresQuery
    ) -> list[tuple[str, str]]:
        """Return the official release-table element for the category."""
        return [(params.category, self.category_element_ids[params.category])]

    def transform_data(
        self, raw: list[dict[str, Any]], params: PersonalConsumptionExpendituresQuery
    ) -> FetchResult:
        """Convert selected release-table series rows to the typed contract."""
        rows: list[FredPersonalConsumptionExpenditure] = []
        for source_row in self.release_table_records(raw, params):
            if source_row.element_type != "series":
                continue
            try:
                rows.append(
                    FredPersonalConsumptionExpenditure.model_validate(
                        self.release_contract_values(source_row, include_line=True)
                    )
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise FredProviderError("FRED_BAD_OBSERVATION") from exc
        return tuple(rows)


class UniversityOfMichiganFetcher(_FixedMacroFetcher[UniversityOfMichiganQuery]):
    """Fetch the University of Michigan's sentiment and inflation series."""

    canonical_model = "UniversityOfMichigan"
    capability = Capability(
        asset_class="macro",
        domain="fred_university_of_michigan",
        period="1M",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = UniversityOfMichiganQuery
    row_model: ClassVar[type[ContractModel]] = FredUniversityOfMichigan
    series_by_field: ClassVar[dict[str, str]] = {
        "consumer_sentiment": "UMCSENT",
        "inflation_expectation": "MICH",
    }

    def selected_series(self, params: UniversityOfMichiganQuery) -> list[tuple[str, str]]:
        """Return the sentiment and inflation-expectation series IDs."""
        del params
        return list(self.series_by_field.items())

    def transform_data(
        self, raw: list[dict[str, Any]], params: UniversityOfMichiganQuery
    ) -> FetchResult:
        """Merge source series by observation date into typed rows."""
        return self._merge_fields(
            raw,
            params,
            self.series_by_field,
            FredUniversityOfMichigan,
        )


class MortgageIndicesFetcher(_FixedMacroFetcher[MortgageIndicesQuery]):
    """Fetch explicitly selected Optimal Blue mortgage-rate index series."""

    canonical_model = "MortgageIndices"
    capability = Capability(
        asset_class="macro",
        domain="fred_mortgage_indices",
        period="1D",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = MortgageIndicesQuery
    row_model: ClassVar[type[ContractModel]] = FredMortgageIndex
    series_by_index: ClassVar[dict[str, str]] = {
        "conforming_30y": "OBMMIC30YF",
        "conforming_30y_na": "OBMMIC30YFNA",
        "jumbo_30y": "OBMMIJUMBO30YF",
        "fha_30y": "OBMMIFHA30YF",
        "va_30y": "OBMMIVA30YF",
        "usda_30y": "OBMMIUSDA30YF",
        "conforming_15y": "OBMMIC15YF",
        "ltv_lte80_fico_ge740": "OBMMIC30YFLVLE80FGE740",
        "ltv_lte80_fico_a720b739": "OBMMIC30YFLVLE80FB720A739",
        "ltv_lte80_fico_a700b719": "OBMMIC30YFLVLE80FB700A719",
        "ltv_lte80_fico_a680b699": "OBMMIC30YFLVLE80FB680A699",
        "ltv_lte80_fico_lt680": "OBMMIC30YFLVLE80FLT680",
        "ltv_gt80_fico_ge740": "OBMMIC30YFLVGT80FGE740",
        "ltv_gt80_fico_a720b739": "OBMMIC30YFLVGT80FB720A739",
        "ltv_gt80_fico_a700b719": "OBMMIC30YFLVGT80FB700A719",
        "ltv_gt80_fico_a680b699": "OBMMIC30YFLVGT80FB680A699",
        "ltv_gt80_fico_lt680": "OBMMIC30YFLVGT80FLT680",
    }
    primary_indices: ClassVar[tuple[str, ...]] = (
        "conforming_30y",
        "conforming_30y_na",
        "jumbo_30y",
        "fha_30y",
        "va_30y",
        "usda_30y",
        "conforming_15y",
    )
    group_indices: ClassVar[dict[str, tuple[str, ...]]] = _MORTGAGE_GROUPS

    def selected_series(self, params: MortgageIndicesQuery) -> list[tuple[str, str]]:
        """Expand validated mortgage index groups into source series IDs."""
        if params.index == ["primary"]:
            selected = self.primary_indices
        else:
            selected = tuple(
                member
                for choice in params.index
                for member in self.group_indices.get(choice, (choice,))
            )
        return [(index, self.series_by_index[index]) for index in selected]

    def transform_data(
        self, raw: list[dict[str, Any]], params: MortgageIndicesQuery
    ) -> FetchResult:
        """Convert selected source observations into typed mortgage-rate rows."""
        rows: list[FredMortgageIndex] = []
        for selection, series_id, date, value, realtime_start, realtime_end in self._validate_raw(
            raw, params
        ):
            if value is None:
                raise FredProviderError("FRED_BAD_OBSERVATION") from None
            row = FredMortgageIndex.model_validate(
                {"date": date, "symbol": selection, "name": selection, "rate": value}
            )
            row._series_ids = (series_id,)
            row._realtime_by_series = {series_id: (realtime_start, realtime_end)}
            rows.append(row)
        return self._sort(rows, params)


class ManufacturingOutlookNYFetcher(_FixedMacroFetcher[ManufacturingOutlookNYQuery]):
    """Fetch selected Empire State Manufacturing Survey topic series."""

    canonical_model = "ManufacturingOutlookNY"
    capability = Capability(
        asset_class="macro",
        domain="fred_manufacturing_outlook_ny",
        period="1M",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = ManufacturingOutlookNYQuery
    row_model: ClassVar[type[ContractModel]] = FredManufacturingOutlookNY
    topic_codes: ClassVar[dict[str, str]] = {
        "business_outlook": "GAF",
        "hours_worked": "AWC",
        "employment": "NEC",
        "inventories": "IVC",
        "prices_received": "PRC",
        "prices_paid": "PPC",
        "capex": "CEF",
        "unfilled_orders": "UOC",
        "new_orders": "NOC",
        "shipments": "SHC",
        "delivery_times": "DTC",
    }
    series_by_field: ClassVar[dict[str, str]] = {
        "diffusion_index": "NOCDINA066MNFRBNY",
        "percent_reporting_increase": "NOCINA156MNFRBNY",
        "percent_reporting_decrease": "NOCDNA156MNFRBNY",
        "percent_reporting_no_change": "NOCNNA156MNFRBNY",
    }

    def series_for_topic(self, params: ManufacturingOutlookNYQuery, topic: str) -> dict[str, str]:
        """Return the fixed FRED field map for one validated NY topic."""
        prefix = self.topic_codes[topic]
        if params.seasonally_adjusted:
            adjustment = "SA"
            suffix = "SFRBNY"
        else:
            adjustment = "NA"
            # NY Fed's current price-paid and price-received diffusion series
            # include the "NEM" marker in their NSA identifiers.
            suffix = "NEMFRBNY" if topic in {"prices_received", "prices_paid"} else "NFRBNY"
        return {
            "diffusion_index": f"{prefix}DI{adjustment}066M{suffix}",
            "percent_reporting_increase": f"{prefix}I{adjustment}156M{suffix}",
            "percent_reporting_decrease": f"{prefix}D{adjustment}156M{suffix}",
            "percent_reporting_no_change": f"{prefix}N{adjustment}156M{suffix}",
        }

    def series_for(self, params: ManufacturingOutlookNYQuery) -> dict[str, str]:
        """Retain the single-topic series-map helper for existing callers."""
        topics = _manufacturing_topics(params)
        if len(topics) != 1:
            raise ValueError("series_for only accepts one topic")
        return self.series_for_topic(params, topics[0])

    def topic_series_for(self, params: ManufacturingOutlookNYQuery) -> dict[str, dict[str, str]]:
        """Map every validated NY topic to its own four source series."""
        return {
            topic: self.series_for_topic(params, topic) for topic in _manufacturing_topics(params)
        }

    def selected_series(self, params: ManufacturingOutlookNYQuery) -> list[tuple[str, str]]:
        """Return topic series in the caller's normalized request order."""
        return [
            (field, series_id)
            for field_to_series in self.topic_series_for(params).values()
            for field, series_id in field_to_series.items()
        ]

    def transform_data(
        self, raw: list[dict[str, Any]], params: ManufacturingOutlookNYQuery
    ) -> FetchResult:
        """Return distinct, date/topic-keyed NY survey rows."""
        return self._merge_topic_fields(
            raw, params, self.topic_series_for(params), FredManufacturingOutlookNY
        )


class ManufacturingOutlookTexasFetcher(_FixedMacroFetcher[ManufacturingOutlookTexasQuery]):
    """Fetch selected Texas Manufacturing Outlook Survey topic series."""

    canonical_model = "ManufacturingOutlookTexas"
    capability = Capability(
        asset_class="macro",
        domain="fred_manufacturing_outlook_texas",
        period="1M",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = ManufacturingOutlookTexasQuery
    row_model: ClassVar[type[ContractModel]] = FredManufacturingOutlookTexas
    topic_codes: ClassVar[dict[str, str]] = {
        "business_activity": "BACT",
        "business_outlook": "FCOLK",
        "capex": "CEXP",
        "prices_paid": "PRM",
        "production": "PROD",
        "inventory": "FGI",
        "new_orders": "VNWO",
        "new_orders_growth": "GRO",
        "unfilled_orders": "UFIL",
        "shipments": "VSHP",
        "delivery_time": "DTM",
        "employment": "NEMP",
        "wages": "WGS",
        "hours_worked": "AVGWK",
    }
    series_by_field: ClassVar[dict[str, str]] = {
        "diffusion_index": "VNWOUAMFRBDAL",
        "percent_reporting_increase": "VNWOIUAMFRBDAL",
        "percent_reporting_decrease": "VNWODUAMFRBDAL",
        "percent_reporting_no_change": "VNWONUAMFRBDAL",
    }

    def series_for_topic(
        self, params: ManufacturingOutlookTexasQuery, topic: str
    ) -> dict[str, str]:
        """Return the fixed FRED field map for one validated Texas topic."""
        prefix = self.topic_codes[topic]
        adjustment = "SA" if params.seasonally_adjusted else "UA"
        return {
            "diffusion_index": f"{prefix}{adjustment}MFRBDAL",
            "percent_reporting_increase": f"{prefix}I{adjustment}MFRBDAL",
            "percent_reporting_decrease": f"{prefix}D{adjustment}MFRBDAL",
            "percent_reporting_no_change": f"{prefix}N{adjustment}MFRBDAL",
        }

    def series_for(self, params: ManufacturingOutlookTexasQuery) -> dict[str, str]:
        """Retain the single-topic series-map helper for existing callers."""
        topics = _manufacturing_topics(params)
        if len(topics) != 1:
            raise ValueError("series_for only accepts one topic")
        return self.series_for_topic(params, topics[0])

    def topic_series_for(self, params: ManufacturingOutlookTexasQuery) -> dict[str, dict[str, str]]:
        """Map every validated Texas topic to its own four source series."""
        return {
            topic: self.series_for_topic(params, topic) for topic in _manufacturing_topics(params)
        }

    def selected_series(self, params: ManufacturingOutlookTexasQuery) -> list[tuple[str, str]]:
        """Return topic series in the caller's normalized request order."""
        return [
            (field, series_id)
            for field_to_series in self.topic_series_for(params).values()
            for field, series_id in field_to_series.items()
        ]

    def transform_data(
        self, raw: list[dict[str, Any]], params: ManufacturingOutlookTexasQuery
    ) -> FetchResult:
        """Return distinct, date/topic-keyed Texas survey rows."""
        return self._merge_topic_fields(
            raw, params, self.topic_series_for(params), FredManufacturingOutlookTexas
        )


class SurveyOfEconomicConditionsChicagoFetcher(
    _FixedMacroFetcher[SurveyOfEconomicConditionsChicagoQuery]
):
    """Fetch the Chicago Fed survey's nine documented monthly series."""

    canonical_model = "SurveyOfEconomicConditionsChicago"
    capability = Capability(
        asset_class="macro",
        domain="fred_survey_of_economic_conditions_chicago",
        period="1M",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = SurveyOfEconomicConditionsChicagoQuery
    row_model: ClassVar[type[ContractModel]] = FredSurveyOfEconomicConditionsChicago
    series_by_field: ClassVar[dict[str, str]] = {
        "activity_index": "CFSBCACTIVITY",
        "one_year_outlook": "CFSBCOUTLOOK",
        "manufacturing_activity": "CFSBCACTIVITYMFG",
        "non_manufacturing_activity": "CFSBCACTIVITYNMFG",
        "capital_expenditures_expectations": "CFSBCCAPXEXP",
        "hiring_expectations": "CFSBCHIRINGEXP",
        "current_hiring": "CFSBCHIRING",
        "labor_costs": "CFSBCLABORCOSTS",
        "non_labor_costs": "CFSBCNONLABORCOSTS",
    }

    def selected_series(
        self, params: SurveyOfEconomicConditionsChicagoQuery
    ) -> list[tuple[str, str]]:
        """Return the fixed nine-series Chicago survey mapping."""
        del params
        return list(self.series_by_field.items())

    def transform_data(
        self, raw: list[dict[str, Any]], params: SurveyOfEconomicConditionsChicagoQuery
    ) -> FetchResult:
        """Merge the survey components by observation date into typed rows."""
        return self._merge_fields(
            raw,
            params,
            self.series_by_field,
            FredSurveyOfEconomicConditionsChicago,
        )


class RetailPricesFetcher(_FixedMacroFetcher[RetailPricesQuery]):
    """Fetch one verified BLS average-retail-price series."""

    canonical_model = "RetailPrices"
    capability = Capability(
        asset_class="macro",
        domain="fred_retail_prices",
        period="1M",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = RetailPricesQuery
    row_model: ClassVar[type[ContractModel]] = FredRetailPrice

    def selected_series(self, params: RetailPricesQuery) -> list[tuple[str, str]]:
        """Map the exact item and region to its BLS series identifier."""
        if type(params) is not RetailPricesQuery or params.item not in _RETAIL_ITEM_CODES:
            raise FredProviderError("FRED_BAD_QUERY") from None
        area_code = _RETAIL_AREA_CODES[params.region]
        item_code = _RETAIL_ITEM_CODES[params.item]
        return [(params.item, f"APU{area_code}{item_code}")]

    def transform_data(self, raw: list[dict[str, Any]], params: RetailPricesQuery) -> FetchResult:
        """Validate and serialize BLS average-price observations."""
        item = params.item
        if item is None:
            raise FredProviderError("FRED_BAD_QUERY") from None
        expected = set(self.selected_series(params))
        rows: list[FredRetailPrice] = []
        for selection, series_id, date, value, realtime_start, realtime_end in self._validate_raw(
            raw, params
        ):
            if (selection, series_id) not in expected:
                raise FredProviderError("FRED_BAD_OBSERVATION") from None
            description = (
                f"{_RETAIL_ITEM_DESCRIPTIONS[item]} in "
                f"{_RETAIL_AREA_NAMES[params.region]}, average price, not seasonally adjusted"
            )
            row = FredRetailPrice.model_validate(
                {
                    "date": date,
                    "symbol": series_id,
                    "country": "united_states",
                    "description": description,
                    "value": value,
                }
            )
            row._series_ids = (series_id,)
            row._realtime_by_series = {series_id: (realtime_start, realtime_end)}
            rows.append(row)
        return self._sort(rows, params)


class SeniorLoanOfficerSurveyFetcher(Fetcher[SeniorLoanOfficerSurveyQuery, bytes]):
    """Read the official SLOOS release and select by its coded dimensions."""

    async_mode = "bounded_thread"
    canonical_model = "SeniorLoanOfficerSurvey"
    capability = Capability(
        asset_class="macro",
        domain="fred_sloos",
        period="3M",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = SeniorLoanOfficerSurveyQuery

    def transform_query(self, **kwargs: object) -> SeniorLoanOfficerSurveyQuery:
        """Validate the public query fields before requesting the SLOOS package."""
        return self.query_model.model_validate(kwargs)

    def extract_data(self, params: SeniorLoanOfficerSurveyQuery, ctx: FetchContext) -> bytes:
        """Download the official SLOOS ZIP through the shared HTTP client."""
        if type(params) is not SeniorLoanOfficerSurveyQuery:
            raise FredProviderError("FRED_BAD_QUERY") from None
        try:
            checked = self.query_model.model_validate(params.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_QUERY") from exc
        if checked.category not in _SLOOS_SELECTOR_FILTERS:
            raise FredProviderError("FRED_BAD_QUERY") from None
        try:
            response = get_shared_http_client().get(
                _SLOOS_DOWNLOAD_URL,
                params={"rel": "SLOOS", "filetype": "zip"},
                timeout=ctx.timeout if ctx.timeout is not None else 30.0,
                source="fred",
            )
        except HttpFetchError as exc:
            raise FredProviderError(
                "FRED_HTTP_ERROR", status=exc.status, url=_SLOOS_DOWNLOAD_URL
            ) from exc
        if response.status_code != 200:
            raise FredProviderError(
                "FRED_HTTP_ERROR", status=response.status_code, url=_SLOOS_DOWNLOAD_URL
            )
        body = response.content
        if type(body) is not bytes or not body or len(body) > _MAX_SLOOS_ARCHIVE_BYTES:
            raise FredProviderError("FRED_BAD_RESPONSE", status=200, url=_SLOOS_DOWNLOAD_URL)
        return body

    def transform_data(self, raw: bytes, params: SeniorLoanOfficerSurveyQuery) -> FetchResult:
        """Parse the bounded official response and return typed observations."""
        if type(params) is not SeniorLoanOfficerSurveyQuery:
            raise FredProviderError("FRED_BAD_QUERY") from None
        return _parse_sloos_archive(raw, params)


def _parse_sloos_archive(
    archive_bytes: object, params: SeniorLoanOfficerSurveyQuery
) -> tuple[FredSeniorLoanOfficerSurvey, ...]:
    """Validate the official compact release, map dimensions, and transform rows."""
    if type(archive_bytes) is not bytes or not archive_bytes:
        raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None
    if len(archive_bytes) > _MAX_SLOOS_ARCHIVE_BYTES:
        raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            members = archive.infolist()
            if len(members) > 16 or any(
                "/" in item.filename or "\\" in item.filename for item in members
            ):
                raise ValueError("invalid SLOOS archive members")
            data_members = [item for item in members if item.filename == "SLOOS_data.xml"]
            if len(data_members) != 1 or data_members[0].file_size > _MAX_SLOOS_XML_BYTES:
                raise ValueError("missing or oversized SLOOS dataset")
            xml_bytes = archive.read(data_members[0])
        root = ElementTree.fromstring(xml_bytes)
    except (
        DefusedXmlException,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        ElementTree.ParseError,
    ) as exc:
        raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from exc
    message_ns = "http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message"
    if root.tag != f"{{{message_ns}}}MessageGroup":
        raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None
    header_name = root.findtext(f"{{{message_ns}}}Header/{{{message_ns}}}Name")
    if header_name != "Senior Loan Officer Opinion Survey on Bank Lending Practices":
        raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None

    series_nodes = root.findall(f".//{{{_SLOOS_QUERY_NS}}}Series")
    if not series_nodes or len(series_nodes) > _MAX_SLOOS_SERIES:
        raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None
    selected: list[tuple[str, str, list[tuple[date_type, float | None]]]] = []
    seen_symbols: set[str] = set()
    for series in series_nodes:
        attributes = series.attrib
        _validate_sloos_dimensions(attributes)
        symbol = attributes.get("SERIES_NAME")
        if (
            type(symbol) is not str
            or len(symbol) > 128
            or re.fullmatch(r"(?:H0\.)?[A-Z0-9_]+(?:\.[A-Z0-9]+)?", symbol) is None
        ):
            raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None
        if symbol in seen_symbols:
            raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None
        seen_symbols.add(symbol)
        if attributes.get("FREQ") != "162":
            raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None
        if not _sloos_matches_selector(attributes, params.category):
            continue
        title = _sloos_series_title(series)
        unit_multiplier = _parse_sloos_multiplier(attributes.get("UNIT_MULT"))
        seen_dates: set[date_type] = set()
        source_observations = series.findall(f"{{{_SLOOS_FRB_NS}}}Obs")
        if len(source_observations) > _MAX_SLOOS_OBSERVATIONS_PER_SERIES:
            raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None
        observations = [
            _parse_sloos_observation(observation, seen_dates, unit_multiplier)
            for observation in source_observations
        ]
        observations.sort(key=lambda item: item[0])
        selected.append((symbol, title, observations))
    if not selected:
        raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None

    rows: list[FredSeniorLoanOfficerSurvey] = []
    for symbol, title, observations in selected:
        transformed = _transform_sloos_observations(observations, params.transform)
        for day, value in transformed:
            if params.start_date is not None and day < params.start_date:
                continue
            if params.end_date is not None and day > params.end_date:
                continue
            if value is None:
                continue
            try:
                rows.append(
                    FredSeniorLoanOfficerSurvey.model_validate(
                        {"date": day, "symbol": symbol, "value": value, "title": title}
                    )
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise FredProviderError("FRED_BAD_OBSERVATION", url=_SLOOS_DOWNLOAD_URL) from exc
    rows.sort(key=lambda row: (row.date, row.symbol))
    if params.sort_order == "desc":
        rows.reverse()
    remaining = max(0, len(rows) - params.offset)
    if remaining > params.max_records:
        raise FredQueryLimitError(
            url=_SLOOS_DOWNLOAD_URL,
            limit_kind="max_records",
            maximum=params.max_records,
        )
    page_size = min(params.page_size, params.max_records)
    pages_required = (remaining + page_size - 1) // page_size
    if pages_required > params.max_pages:
        raise FredQueryLimitError(
            url=_SLOOS_DOWNLOAD_URL,
            limit_kind="max_pages",
            maximum=params.max_pages,
        )
    return tuple(rows[params.offset :])


def _validate_sloos_dimensions(attributes: dict[str, str]) -> None:
    for name, allowed in _SLOOS_ALLOWED_DIMENSIONS.items():
        value = attributes.get(name)
        if value not in allowed:
            raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None
    if (
        not attributes.get("CURRENCY")
        or not attributes.get("UNIT")
        or attributes.get("FREQ") != "162"
    ):
        raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None


def _sloos_matches_selector(attributes: dict[str, str], category: str) -> bool:
    filters = _SLOOS_SELECTOR_FILTERS.get(category)
    if filters is None:
        raise FredProviderError("FRED_BAD_QUERY") from None
    return all(attributes.get(name) in values for name, values in filters)


def _sloos_series_title(series: ElementTree.Element) -> str:
    annotations = series.find(f"{{{_SLOOS_FRB_NS}}}Annotations")
    if annotations is None:
        raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None
    descriptions: dict[str, str] = {}
    for annotation in annotations.findall(f"{{{_SLOOS_COMMON_NS}}}Annotation"):
        annotation_type = annotation.findtext(f"{{{_SLOOS_COMMON_NS}}}AnnotationType")
        annotation_text = annotation.findtext(f"{{{_SLOOS_COMMON_NS}}}AnnotationText")
        if annotation_type in {"Long Description", "Short Description"}:
            if type(annotation_text) is not str or not annotation_text.strip():
                raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None
            descriptions[annotation_type] = annotation_text.strip()
    title = descriptions.get("Long Description") or descriptions.get("Short Description")
    if title is None:
        raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None
    return title


def _parse_sloos_multiplier(raw: str | None) -> float:
    if raw is None:
        raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None
    try:
        multiplier = float(raw)
    except (TypeError, ValueError, OverflowError):
        raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None
    # These are the literal CL_UNIT_MULT values in the Federal Reserve's
    # frb_common.xsd.  UNIT_MULT is a factor (1 means one), not a base-10
    # exponent; treating "1" as 10 would corrupt every current SLOOS value.
    allowed = {
        0.01,
        0.1,
        1.0,
        1_000.0,
        1_000_000.0,
        100_000_000.0,
        1_000_000_000.0,
        1_000_000_000_000.0,
        1_000_000_000_000_000.0,
    }
    if not math.isfinite(multiplier) or multiplier not in allowed:
        raise FredProviderError("FRED_BAD_RESPONSE", url=_SLOOS_DOWNLOAD_URL) from None
    return multiplier


def _parse_sloos_value(status: str, raw: str) -> float | None:
    if status in {"NA", "ND", "NC"}:
        return None
    if status != "A":
        raise ValueError("unknown SLOOS observation status")
    if raw.strip() == ".":
        return None
    if not raw.strip():
        raise ValueError("empty SLOOS observation value")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("non-finite SLOOS observation")
    return value


def _parse_sloos_observation(
    observation: ElementTree.Element,
    seen_dates: set[date_type],
    unit_multiplier: float,
) -> tuple[date_type, float | None]:
    """Validate one source observation and apply its official unit multiplier."""
    try:
        day = _strict_date(observation.attrib["TIME_PERIOD"])
        if day in seen_dates:
            raise ValueError("duplicate SLOOS observation date")
        seen_dates.add(day)
        value = _parse_sloos_value(
            observation.attrib["OBS_STATUS"], observation.attrib["OBS_VALUE"]
        )
        if value is not None:
            value *= unit_multiplier
            if not math.isfinite(value):
                raise ValueError("non-finite SLOOS observation")
        return day, value
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise FredProviderError("FRED_BAD_OBSERVATION", url=_SLOOS_DOWNLOAD_URL) from exc


def _transform_sloos_observations(
    observations: list[tuple[date_type, float | None]],
    transform: FredTransformUnits | None,
) -> list[tuple[date_type, float | None]]:
    mode = transform or "lin"
    if mode == "lin":
        return observations
    transformed: list[tuple[date_type, float | None]] = []
    for index, (day, current) in enumerate(observations):
        lag = 4 if mode in {"ch1", "pc1"} else 1
        previous = observations[index - lag][1] if index >= lag else None
        value: float | None = None
        try:
            if current is not None:
                if mode == "log":
                    if current > 0:
                        value = math.log(current)
                elif mode in {"chg", "ch1"}:
                    if previous is not None:
                        value = current - previous
                elif mode in {"pch", "pc1"}:
                    if previous is not None and previous != 0:
                        value = (current / previous - 1.0) * 100.0
                elif mode == "pca":
                    if previous is not None and previous != 0:
                        value = ((current / previous) ** 4 - 1.0) * 100.0
                elif (
                    mode in {"cch", "cca"} and previous is not None and current > 0 and previous > 0
                ):
                    multiplier = 400.0 if mode == "cca" else 100.0
                    value = (math.log(current) - math.log(previous)) * multiplier
        except (OverflowError, ValueError, ZeroDivisionError):
            value = None
        if value is not None and not math.isfinite(value):
            value = None
        transformed.append((day, value))
    return transformed


def _strict_date(value: object) -> date_type:
    if type(value) is date_type:
        return value
    if type(value) is not str or _DATE_TOKEN.fullmatch(value) is None:
        raise ValueError("date values must be canonical YYYY-MM-DD")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date values must be valid YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("date values must be canonical YYYY-MM-DD")
    return parsed


def _fetch_limited_observations(
    query: FredSeriesQuery,
    *,
    selection: str,
    limit: int,
    timeout: float | None,
) -> list[dict[str, Any]]:
    """Fetch an intentional result limit without treating the full series as truncated."""
    base_url = config.get_settings().fred_api_base_url or _client.FRED_DEFAULT_BASE_URL
    url = f"{base_url.rstrip('/')}/fred/series/observations"
    page_size = min(query.page_size, query.max_records, limit)
    params: dict[str, str] = {
        "series_id": query.series_id,
        "units": query.transform_units,
        "output_type": str(query.output_type),
        "sort_order": query.sort_order,
        "aggregation_method": query.aggregation_method,
    }
    if query.frequency is not None:
        params["frequency"] = query.frequency
    if query.vintage_dates is not None:
        params["vintage_dates"] = ",".join(item.isoformat() for item in query.vintage_dates)
    if query.start_date is not None:
        params["observation_start"] = query.start_date.isoformat()
    if query.end_date is not None:
        params["observation_end"] = query.end_date.isoformat()
    if query.as_of is not None:
        params["realtime_start"] = query.as_of.isoformat()
        params["realtime_end"] = query.as_of.isoformat()
    else:
        if query.realtime_start is not None:
            params["realtime_start"] = query.realtime_start.isoformat()
        if query.realtime_end is not None:
            params["realtime_end"] = query.realtime_end.isoformat()

    rows: list[dict[str, Any]] = []
    seen: set[tuple[object, object, object]] = set()
    expected_count: int | None = None
    current_offset = query.offset
    while len(rows) < limit:
        request_limit = min(page_size, limit - len(rows))
        payload = _fred_json(
            "/fred/series/observations",
            {**params, "limit": str(request_limit), "offset": str(current_offset)},
            timeout,
        )
        count = payload.get("count")
        response_offset = payload.get("offset")
        response_limit = payload.get("limit")
        observations = payload.get("observations")
        if (
            type(count) is not int
            or count < 0
            or type(response_offset) is not int
            or response_offset != current_offset
            or type(response_limit) is not int
            or response_limit != request_limit
            or type(observations) is not list
            or any(type(item) is not dict for item in observations)
            or (expected_count is not None and count != expected_count)
        ):
            raise FredProviderError("FRED_PAGINATION_ERROR", url=url)
        if expected_count is None:
            expected_count = count
            remaining = max(0, count - query.offset)
            wanted = min(limit, remaining)
            if wanted > query.max_records:
                raise FredQueryLimitError(
                    url=url,
                    limit_kind="max_records",
                    maximum=query.max_records,
                )
            pages_required = (wanted + page_size - 1) // page_size
            if pages_required > query.max_pages:
                raise FredQueryLimitError(
                    url=url,
                    limit_kind="max_pages",
                    maximum=query.max_pages,
                )
            if wanted == 0:
                if observations:
                    raise FredProviderError("FRED_PAGINATION_ERROR", url=url)
                return []
        wanted = min(limit, max(0, expected_count - query.offset))
        expected_page_size = min(
            request_limit,
            max(0, expected_count - current_offset),
            max(0, wanted - len(rows)),
        )
        if len(observations) != expected_page_size:
            raise FredProviderError("FRED_PAGINATION_ERROR", url=url)
        for observation in observations:
            identity = (
                observation.get("date"),
                observation.get("realtime_start"),
                observation.get("realtime_end"),
            )
            if identity in seen:
                raise FredProviderError("FRED_PAGINATION_CONFLICT", url=url)
            seen.add(identity)
            rows.append(
                {
                    "_selection": selection,
                    "_series_id": query.series_id,
                    "observation": observation,
                }
            )
        current_offset += expected_page_size
        if len(rows) >= wanted:
            return rows
        if expected_page_size <= 0:
            raise FredProviderError("FRED_PAGINATION_ERROR", url=url)
    return rows


def _parse_date_list(value: object) -> list[date_type | None] | None:
    """Normalize one or more exact publication dates within the bounded cap."""
    if value is None:
        return None
    if type(value) is date_type:
        values: list[object] = [value]
    elif type(value) is str:
        tokens = value.split(",")
        if any(not token.strip() for token in tokens):
            raise ValueError("date list must not contain empty entries")
        values = [token.strip() for token in tokens]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise ValueError("date must be a date or a list of exact dates")
    if not values or len(values) > _MAX_RELEASE_DATES:
        raise ValueError(f"date must contain one to {_MAX_RELEASE_DATES} entries")
    parsed = [None if item is None else _strict_date(item) for item in values]
    if len(set(parsed)) != len(parsed):
        raise ValueError("date values must be unique")
    return parsed


def _parse_number(value: object) -> float | None:
    if type(value) is not str:
        raise ValueError("FRED observation value must be text")
    number_text = value.strip()
    if number_text == ".":
        return None
    if not number_text:
        raise ValueError("FRED observation value is empty")
    parsed = float(number_text)
    if not math.isfinite(parsed):
        raise ValueError("FRED observation value must be finite")
    return parsed


def _validate_vintage(
    realtime_start: date_type,
    realtime_end: date_type,
    params: QueryParams,
) -> None:
    if realtime_start > realtime_end:
        raise ValueError("FRED real-time interval is reversed")
    as_of = getattr(params, "as_of", None)
    if as_of is not None and not realtime_start <= as_of <= realtime_end:
        raise ValueError("observation does not cover the requested as_of date")
    realtime_from = getattr(params, "realtime_start", None)
    realtime_to = getattr(params, "realtime_end", None)
    if realtime_from is not None and realtime_end < realtime_from:
        raise ValueError("observation ends before the requested real-time window")
    if realtime_to is not None and realtime_start > realtime_to:
        raise ValueError("observation starts after the requested real-time window")
    vintage_dates = getattr(params, "vintage_dates", None)
    if vintage_dates is not None and not any(
        realtime_start <= vintage <= realtime_end for vintage in vintage_dates
    ):
        raise ValueError("observation does not cover any requested vintage date")


__all__ = (
    "ConsumerPriceIndexFetcher",
    "ConsumerPriceIndexQuery",
    "ManufacturingOutlookNYFetcher",
    "ManufacturingOutlookNYQuery",
    "ManufacturingOutlookTexasFetcher",
    "ManufacturingOutlookTexasQuery",
    "MortgageIndicesFetcher",
    "MortgageIndicesQuery",
    "NonFarmPayrollsFetcher",
    "NonFarmPayrollsQuery",
    "PersonalConsumptionExpendituresFetcher",
    "PersonalConsumptionExpendituresQuery",
    "RetailPricesFetcher",
    "RetailPricesQuery",
    "SeniorLoanOfficerSurveyFetcher",
    "SeniorLoanOfficerSurveyQuery",
    "SurveyOfEconomicConditionsChicagoFetcher",
    "SurveyOfEconomicConditionsChicagoQuery",
    "UniversityOfMichiganFetcher",
    "UniversityOfMichiganQuery",
)
