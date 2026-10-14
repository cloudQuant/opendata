"""Typed output contracts for the second macro-provider iteration."""

from __future__ import annotations

from datetime import date

from pydantic import Field

from opendata.data.models.base import ContractModel


class ImfMaritimeChokePointInfo(ContractModel):
    """PortWatch metadata and long-run vessel statistics for one chokepoint."""

    chokepoint_code: str
    name: str
    latitude: float
    longitude: float
    vessel_count_total: int
    vessel_count_tanker: int
    vessel_count_container: int
    vessel_count_general_cargo: int
    vessel_count_dry_bulk: int
    vessel_count_roro: int
    industry_top1: str | None = None
    industry_top2: str | None = None
    industry_top3: str | None = None


class ImfMaritimeChokePointVolume(ContractModel):
    """Daily PortWatch transit counts, capacities and formula diagnostics."""

    date: date
    chokepoint: str
    vessels_total: int
    vessels_cargo: int
    vessels_tanker: int
    vessels_container: int
    vessels_general_cargo: int
    vessels_dry_bulk: int
    vessels_roro: int
    capacity_total: float
    capacity_cargo: float
    capacity_tanker: float
    capacity_container: float
    capacity_general_cargo: float
    capacity_dry_bulk: float
    capacity_roro: float
    capacity_cargo_component_delta_metric_tons: float | None = None
    capacity_total_component_delta_metric_tons: float | None = None
    capacity_reported_total_delta_metric_tons: float | None = None
    capacity_cargo_components_consistent: bool | None = None
    capacity_total_components_consistent: bool | None = None
    capacity_reported_total_consistent: bool | None = None


class ImfPortInfo(ContractModel):
    """PortWatch port metadata and long-run vessel statistics."""

    port_code: str
    continent: str
    country: str
    country_code: str
    port_name: str
    port_full_name: str
    latitude: float
    longitude: float
    vessel_count_total: int
    vessel_count_tanker: int
    vessel_count_container: int
    vessel_count_general_cargo: int
    vessel_count_dry_bulk: int
    vessel_count_roro: int
    industry_top1: str | None = None
    industry_top2: str | None = None
    industry_top3: str | None = None
    share_country_maritime_import: float
    share_country_maritime_export: float


class ImfPortVolume(ContractModel):
    """Daily port calls and estimated trade volumes with source formula diagnostics."""

    date: date
    port_code: str
    port_name: str | None = None
    country: str | None = None
    country_code: str
    portcalls: int
    portcalls_tanker: int
    portcalls_container: int
    portcalls_general_cargo: int
    portcalls_dry_bulk: int
    portcalls_roro: int
    imports: float
    imports_cargo: float
    imports_tanker: float
    imports_container: float
    imports_general_cargo: float
    imports_dry_bulk: float
    imports_roro: float
    exports: float
    exports_cargo: float
    exports_tanker: float
    exports_container: float
    exports_general_cargo: float
    exports_dry_bulk: float
    exports_roro: float
    portcalls_cargo: int = Field(
        description=(
            "Repository-specific extension: source portcalls_cargo, total non-tanker "
            "port calls; not present in the public OpenBB V5 output contract."
        )
    )
    imports_cargo_component_delta_metric_tons: float | None = Field(
        default=None,
        description=(
            "Source import_cargo minus the sum of container, dry-bulk, general-cargo "
            "and Ro-Ro imports; diagnostic in metric tons."
        ),
    )
    imports_total_component_delta_metric_tons: float | None = Field(
        default=None,
        description=(
            "Source import minus the sum of all five ship-category imports; "
            "diagnostic in metric tons."
        ),
    )
    exports_cargo_component_delta_metric_tons: float | None = Field(
        default=None,
        description=(
            "Source export_cargo minus the sum of container, dry-bulk, general-cargo "
            "and Ro-Ro exports; diagnostic in metric tons."
        ),
    )
    exports_total_component_delta_metric_tons: float | None = Field(
        default=None,
        description=(
            "Source export minus the sum of all five ship-category exports; "
            "diagnostic in metric tons."
        ),
    )
    imports_cargo_components_consistent: bool | None = Field(
        default=None,
        description="True only when the import_cargo diagnostic delta is exactly zero.",
    )
    imports_total_components_consistent: bool | None = Field(
        default=None,
        description="True only when the import diagnostic delta is exactly zero.",
    )
    exports_cargo_components_consistent: bool | None = Field(
        default=None,
        description="True only when the export_cargo diagnostic delta is exactly zero.",
    )
    exports_total_components_consistent: bool | None = Field(
        default=None,
        description="True only when the export diagnostic delta is exactly zero.",
    )


class ImfAvailableIndicator(ContractModel):
    """One indicator entry from IMF SDMX metadata."""

    symbol_root: str | None = None
    symbol: str | None = None
    country: str | None = None
    iso: str | None = None
    description: str | None = None
    frequency: str | None = None
    agency_id: str
    dataflow_id: str
    dataflow_name: str
    structure_id: str
    dimension_id: str
    long_description: str | None = None
    member_of: tuple[str, ...] | None = None
    extra_dimensions: tuple[str, ...] | None = None


class ImfConsumerPriceIndex(ContractModel):
    """One IMF CPI observation with the published unit and series identity."""

    date: date
    country: str
    value: float
    unit: str | None = None
    unit_multiplier: int | float | None = None
    country_code: str
    series_id: str
    expenditure: str
    title: str
    order: int | None = None


class ImfDirectionOfTrade(ContractModel):
    """One IMF DOTS observation between a reporting and counterpart area."""

    date: date
    symbol: str | None = None
    country: str
    counterpart: str
    title: str | None = None
    value: float
    scale: str | None = None
    unit: str | None = None
    country_code: str
    counterpart_code: str
    unit_multiplier: int | None = None


class ImfEconomicIndicator(ContractModel):
    """One observation from an explicitly selected IMF SDMX indicator."""

    date: date
    symbol_root: str | None = None
    symbol: str | None = None
    country: str | None = None
    value: float | None = None
    unit: str | None = None
    unit_multiplier: int | float | None = None
    scale: str | None = None
    order: int | float | None = None
    level: int | None = None
    title: str | None = None
    description: str | None = None
    country_code: str | None = None


class OecdCompositeLeadingIndicator(ContractModel):
    """One OECD composite leading-indicator observation."""

    date: date
    value: float | None
    country: str


class OecdConsumerPriceIndex(ContractModel):
    """One OECD CPI observation for a country and expenditure category."""

    date: date
    country: str
    value: float | None
    expenditure: str


class OecdCountryInterestRate(ContractModel):
    """One OECD interest-rate observation."""

    date: date
    value: float | None
    country: str


class OecdGdpNominal(ContractModel):
    """One OECD nominal-GDP observation."""

    date: date
    country: str | None
    value: float


class OecdGdpReal(ContractModel):
    """One OECD real-GDP observation."""

    date: date
    country: str | None
    value: float


class OecdGdpForecast(ContractModel):
    """One OECD Economic Outlook forecast observation."""

    date: date
    country: str
    value: float


class OecdHousePriceIndex(ContractModel):
    """One OECD real house-price index observation."""

    date: date
    country: str | None
    value: float | None


class OecdSharePriceIndex(ContractModel):
    """One OECD share-price index observation."""

    date: date
    country: str | None
    value: float | None


class OecdUnemployment(ContractModel):
    """One OECD unemployment-rate observation."""

    date: date
    country: str | None
    value: float | None
