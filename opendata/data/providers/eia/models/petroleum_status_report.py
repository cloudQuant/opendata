"""EIA Weekly Petroleum Status Report table adapter."""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import ClassVar, Literal
from urllib.parse import quote

from pydantic import field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.eia import PetroleumStatusReport
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.eia.models import _client

_CATEGORY_FILES: dict[str, tuple[str, str]] = {
    "balance_sheet": ("table1.csv", "U.S. Petroleum Balance Sheet"),
    "inputs_and_production": ("table2.csv", "U.S. Inputs and Production by PAD District"),
    "refiner_blender_net_production": ("table3.csv", "Refiner and Blender Net Production"),
    "crude_petroleum_stocks": ("table4.csv", "Crude Oil and Petroleum Product Stocks"),
    "gasoline_fuel_stocks": ("table5.csv", "Motor Gasoline and Fuel Ethanol Stocks"),
    "total_gasoline_by_sub_padd": (
        "table5a.csv",
        "Motor Gasoline and Fuel Ethanol Stocks by PAD and Sub-PADD",
    ),
    "distillate_fuel_oil_stocks": (
        "table6.csv",
        "Distillate, Jet Fuel, Residual Fuel, and Propane Stocks",
    ),
    "imports": ("table7.csv", "Imports and Exports of Crude Oil and Products"),
    "imports_by_country": ("table8.csv", "Preliminary Crude Imports by Country of Origin"),
    "weekly_estimates": ("table9.csv", "U.S. and PAD District Weekly Estimates"),
    "spot_prices_crude_gas_heating": (
        "table11.csv",
        "Spot Prices of Crude Oil, Gasoline, and Heating Oil",
    ),
    "spot_prices_diesel_jet_fuel_propane": (
        "table12.csv",
        "Spot Prices of Diesel, Jet Fuel, and Propane",
    ),
    "retail_prices": ("table14.csv", "U.S. Retail Motor Gasoline and On-Highway Diesel Prices"),
}
_CATEGORIES = Literal[
    "balance_sheet",
    "inputs_and_production",
    "refiner_blender_net_production",
    "crude_petroleum_stocks",
    "gasoline_fuel_stocks",
    "total_gasoline_by_sub_padd",
    "distillate_fuel_oil_stocks",
    "imports",
    "imports_by_country",
    "weekly_estimates",
    "spot_prices_crude_gas_heating",
    "spot_prices_diesel_jet_fuel_propane",
    "retail_prices",
]
_TABLES = frozenset(
    {
        "all",
        "conventional_gas",
        "crude",
        "crude_production",
        "crude_production_avg",
        "diesel",
        "ethanol_plant_production",
        "ethanol_plant_production_avg",
        "exports",
        "exports_avg",
        "heating_oil",
        "imports",
        "imports_avg",
        "imports_by_country",
        "imports_by_country_avg",
        "inputs_and_utilization",
        "inputs_and_utilization_avg",
        "jet_fuel",
        "monthly",
        "net_imports_inc_spr_avg",
        "net_imports_incl_spr",
        "net_production",
        "net_production_avg",
        "net_production_by_product",
        "net_production_by_production_avg",
        "product_by_region",
        "product_by_region_avg",
        "product_supplied",
        "product_supplied_avg",
        "propane",
        "rbob",
        "refiner_blender_net_production",
        "refiner_blender_net_production_avg",
        "stocks",
        "supply",
        "supply_avg",
        "ulta_low_sulfur_distillate_reclassification",
        "ulta_low_sulfur_distillate_reclassification_avg",
        "weekly",
    }
)
_TABLE_NEEDLES = {
    "conventional_gas": ("conventional gasoline", "conventional gas"),
    "crude": ("crude",),
    "crude_production": ("crude production",),
    "crude_production_avg": ("crude production",),
    "diesel": ("diesel",),
    "ethanol_plant_production": ("ethanol plant production",),
    "ethanol_plant_production_avg": ("ethanol plant production",),
    "exports": ("export",),
    "exports_avg": ("export",),
    "heating_oil": ("heating oil",),
    "imports": ("import",),
    "imports_avg": ("import",),
    "imports_by_country": ("import", "country"),
    "imports_by_country_avg": ("import", "country"),
    "inputs_and_utilization": ("input", "utilization"),
    "inputs_and_utilization_avg": ("input", "utilization"),
    "jet_fuel": ("jet fuel",),
    "monthly": ("monthly",),
    "net_imports_inc_spr_avg": ("net imports",),
    "net_imports_incl_spr": ("net imports",),
    "net_production": ("net production",),
    "net_production_avg": ("net production",),
    "net_production_by_product": ("net production", "product"),
    "net_production_by_production_avg": ("net production",),
    "product_by_region": ("product", "region"),
    "product_by_region_avg": ("product", "region"),
    "product_supplied": ("product supplied",),
    "product_supplied_avg": ("product supplied",),
    "propane": ("propane",),
    "rbob": ("rbob", "reformulated gasoline"),
    "refiner_blender_net_production": ("net production",),
    "refiner_blender_net_production_avg": ("net production",),
    "stocks": ("stock",),
    "supply": ("supply",),
    "supply_avg": ("supply",),
    "ulta_low_sulfur_distillate_reclassification": ("ultra-low sulfur distillate",),
    "ulta_low_sulfur_distillate_reclassification_avg": ("ultra-low sulfur distillate",),
    "weekly": ("weekly",),
}
# The public v5 reference publishes a collection-wide list, but not the complete
# category-by-selector matrix. Keep explicit selector support to pairs anchored
# in the public reference or this repository's official-shape fixture; `all` is
# verified for non-weekly categories because it is their documented default.
_VERIFIED_TABLES_BY_CATEGORY = {
    "balance_sheet": frozenset({"crude"}),
    "weekly_estimates": frozenset({"stocks", "imports"}),
}
_DATE_HEADER = re.compile(r"(?:\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})\Z")
_STUB = re.compile(r"STUB_\d+\Z", re.IGNORECASE)
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?\Z")
WPSR_BASE_URL = "https://ir.eia.gov/wpsr/"


class PetroleumStatusReportQuery(QueryParams):
    """Date/category/table selection over the official weekly CSV files."""

    source: Literal["eia"] = "eia"
    market: Literal["us"] = "us"
    category: _CATEGORIES = "balance_sheet"
    table: str | list[str] | None = None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _date_only(cls, value: object) -> object:
        if value is None or type(value) is date:
            return value
        if isinstance(value, datetime):
            raise ValueError("date bounds must not include a time")
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("date bounds must be ISO calendar dates") from exc
            if parsed.isoformat() == value:
                return parsed
        raise ValueError("date bounds must be ISO calendar dates without a time")

    @field_validator("table", mode="before")
    @classmethod
    def _table_choice(cls, value: object) -> object:
        if value is None:
            return None
        choices = value if isinstance(value, list) else [value]
        if not choices or any(
            type(choice) is not str or choice not in _TABLES for choice in choices
        ):
            raise ValueError("table must use a documented EIA WPSR table choice")
        if "all" in choices and len(choices) != 1:
            raise ValueError("table='all' cannot be combined with another table")
        if choices == ["all"]:
            return "all"
        return value

    @model_validator(mode="before")
    @classmethod
    def _documented_table_default(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if normalized.get("table") is None:
            category = normalized.get("category", "balance_sheet")
            normalized["table"] = "stocks" if category == "weekly_estimates" else "all"
        return normalized

    @model_validator(mode="after")
    def _validate_request(self) -> PetroleumStatusReportQuery:
        if self.symbol is not None:
            raise ValueError("PetroleumStatusReport uses category/table, not symbol")
        selectors = [self.table] if isinstance(self.table, str) else list(self.table or [])
        supported = _VERIFIED_TABLES_BY_CATEGORY.get(self.category, frozenset())
        if self.category != "weekly_estimates":
            supported = supported | frozenset({"all"})
        if any(selector not in supported for selector in selectors):
            raise ValueError("table selector is not verified for the selected WPSR category")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must be on or before end_date")
        return self


class PetroleumStatusReportFetcher(Fetcher[PetroleumStatusReportQuery, tuple[str, bytes]]):
    """Parse source date columns, preserve values, and leave undisclosed units null."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "PetroleumStatusReport"
    capability: ClassVar[Capability] = Capability(
        asset_class="energy",
        domain="energy_petroleum_status_report",
        period="1W",
        market="us",
        source="eia",
        verified=False,
        notes="Official WPSR CSV table layouts mapped; source verification pending.",
    )

    def transform_query(self, **kwargs: object) -> PetroleumStatusReportQuery:
        """Validate the category, table, and date selection."""
        return PetroleumStatusReportQuery.model_validate(kwargs)

    def extract_data(
        self, params: PetroleumStatusReportQuery, ctx: FetchContext
    ) -> tuple[str, bytes]:
        """Fetch the selected public CSV file using the governed EIA client."""
        filename, _source_title = _CATEGORY_FILES[params.category]
        body, _content_type = _client.get_bytes(
            f"{WPSR_BASE_URL}{filename}", params=None, context=ctx
        )
        return filename, body

    def transform_data(
        self, raw: tuple[str, bytes], params: PetroleumStatusReportQuery
    ) -> FetchResult:
        """Parse official table rows and apply the requested filters."""
        filename, body = raw
        file_table = filename.removesuffix(".csv")
        source_title = _CATEGORY_FILES[params.category][1]
        try:
            try:
                text = body.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = body.decode("cp1252")
            parsed_rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
        except (UnicodeDecodeError, csv.Error):
            raise _client.EIAProviderError("EIA_BAD_CSV") from None
        if not parsed_rows or not parsed_rows[0]:
            raise _client.EIAProviderError("EIA_BAD_CSV_SHAPE")
        header = parsed_rows[0]
        stub_indexes = [index for index, name in enumerate(header) if _STUB.fullmatch(name.strip())]
        date_columns = [
            (index, parsed_date)
            for index, name in enumerate(header)
            if (parsed_date := _parse_header_date(name.strip())) is not None
        ]
        if not stub_indexes or not date_columns:
            raise _client.EIAProviderError("EIA_BAD_CSV_SHAPE")
        selected_tables = _selected_tables(params.table)
        levels: list[str] = []
        result: list[PetroleumStatusReport] = []
        source_order = 0
        for csv_row in parsed_rows[1:]:
            if not csv_row or not any(cell.strip() for cell in csv_row):
                continue
            source_order += 1
            for level, index in enumerate(stub_indexes):
                label = csv_row[index].strip() if index < len(csv_row) else ""
                if label:
                    if len(levels) <= level:
                        levels.extend([""] * (level + 1 - len(levels)))
                    levels[level] = label
                    del levels[level + 1 :]
            path = tuple(label for label in levels if label)
            title = " / ".join(path)
            if not title or not _matches_selected_table(title, selected_tables):
                continue
            symbol = _row_locator(file_table, path)
            for column, observation_date in date_columns:
                if params.start_date is not None and observation_date < params.start_date:
                    continue
                if params.end_date is not None and observation_date > params.end_date:
                    continue
                cell = csv_row[column].strip() if column < len(csv_row) else ""
                value = _parse_value(cell)
                if value is None:
                    continue
                record = {
                    "date": observation_date,
                    "table": source_title,
                    "symbol": symbol,
                    "order": source_order,
                    "title": title,
                    "value": value,
                    # The CSV table does not carry a per-row unit column. Do not infer one
                    # across balance-sheet rows that mix stocks, flows, and prices.
                    "unit": None,
                }
                try:
                    result.append(PetroleumStatusReport.model_validate(record))
                except (TypeError, ValueError):
                    raise _client.EIAProviderError("EIA_BAD_CSV_ROW") from None
                if len(result) > 50_000:
                    raise _client.EIAProviderError("EIA_RESULT_LIMIT_EXCEEDED")
        return tuple(result)


def _parse_header_date(value: str) -> date | None:
    if not _DATE_HEADER.fullmatch(value):
        return None
    try:
        if "/" in value:
            month, day, year = (int(part) for part in value.split("/"))
            if year < 100:
                year += 2000 if year < 70 else 1900
            return date(year, month, day)
        return date.fromisoformat(value)
    except ValueError:
        raise _client.EIAProviderError("EIA_BAD_DATE_HEADER") from None


def _parse_value(value: str) -> int | float | None:
    if not value or value.casefold() in {"na", "n/a", "w", "--", "null"}:
        return None
    if not _NUMBER.fullmatch(value):
        raise _client.EIAProviderError("EIA_BAD_NUMBER")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise _client.EIAProviderError("EIA_BAD_NUMBER") from None
    if not parsed.is_finite():
        raise _client.EIAProviderError("EIA_BAD_NUMBER")
    return int(parsed) if parsed == parsed.to_integral_value() else float(parsed)


def _selected_tables(value: str | list[str] | None) -> tuple[str, ...]:
    if value is None or value == "all":
        return ()
    return (value,) if isinstance(value, str) else tuple(value)


def _row_locator(file_table: str, path: tuple[str, ...]) -> str:
    """Build a reversible row locator from the source table and stub hierarchy."""
    return f"{file_table}:row:" + "/".join(quote(part, safe="") for part in path)


def _matches_selected_table(title: str, selectors: tuple[str, ...]) -> bool:
    if not selectors:
        return True
    text = " ".join(title.casefold().replace("/", " ").split())
    for selector in selectors:
        needles = _TABLE_NEEDLES.get(selector, (selector.replace("_", " "),))
        if all(needle in text for needle in needles):
            return True
    return False
