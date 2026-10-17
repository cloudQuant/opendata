"""Typed fetchers for the six fixed Fama/French model identities."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import Field, StrictStr, model_validator

from opendata.data.capability import Capability
from opendata.data.models.famafrench import (
    FamaFrenchBreakpoint,
    FamaFrenchCountryPortfolioReturn,
    FamaFrenchFactorObservation,
    FamaFrenchInternationalIndexReturn,
    FamaFrenchPortfolioReturn,
    FrenchDataRequirement,
    FrenchFrequency,
    FrenchPortfolioSide,
    FrenchSort,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.famafrench import datasets
from opendata.data.providers.famafrench._source import (
    MAX_OUTPUT_ROWS,
    FrenchProviderError,
    SourceBreakpoint,
    SourceDatRatio,
    SourceDatReturn,
    SourceObservation,
    archive_text,
    fetch_archive,
    parse_breakpoint_table,
    parse_dat_ratios,
    parse_dat_returns,
    parse_factor_table,
    parse_factor_text_table,
    parse_portfolio_table,
    parse_portfolio_text_table,
)

if TYPE_CHECKING:
    from datetime import date

    from opendata.data.models.base import ContractModel

_WEIGHTING = Literal["value", "equal"]
_COUNTRY_MEASURE = Literal["usd", "local", "ratios"]
_ARCHIVE_FORMAT = Literal["csv", "txt"]

_BREAKPOINT_PERCENTILES = tuple(range(5, 101, 5))
_BREAKPOINT_COUNT_FIELDS = {
    "sample": "num_firms",
    "nonpositive": "num_firms_less_than_0",
    "positive": "num_firms_greater_than_0",
}
_FACTOR_FIELDS = {
    "mkt-rf": "mkt_rf",
    "smb": "smb",
    "hml": "hml",
    "rmw": "rmw",
    "cma": "cma",
    "rf": "rf",
    "mom": "mom",
    "wml": "wml",
    "st_rev": "st_rev",
    "lt_rev": "lt_rev",
}
_DAT_PUBLIC_FIELDS = {
    ("market", "market"): "mkt",
    ("be_me", "high"): "be_me_high",
    ("be_me", "low"): "be_me_low",
    ("e_p", "high"): "e_p_high",
    ("e_p", "low"): "e_p_low",
    ("ce_p", "high"): "ce_p_high",
    ("ce_p", "low"): "ce_p_low",
    ("dividend_yield", "high"): "yld_high",
    ("dividend_yield", "low"): "yld_low",
    ("dividend_yield", "zero"): "yld_zero",
}
_BREAKPOINT_SELECTORS = {
    **{name: name for name in datasets.BREAKPOINT_DATASETS},
    "op": "operating_profitability",
    "inv": "investment",
    "ep": "earnings_price",
    "e_p": "earnings_price",
    "cfp": "cashflow_price",
    "cf_p": "cashflow_price",
    "dp": "dividend_yield",
    "d_p": "dividend_yield",
    "2_12": "prior_2_12_return",
}
_FACTOR_SUFFIXES = {
    "3_factors": "ff3",
    "5_factors": "ff5",
    "momentum": "momentum",
    "short_term_reversal": "short_term_reversal",
    "long_term_reversal": "long_term_reversal",
}
_FACTOR_REGIONS = {
    "america": "us",
    "us": "us",
    "developed": "developed",
    "developed_ex_us": "developed_ex_us",
    "europe": "europe",
    "japan": "japan",
    "asia_pacific_ex_japan": "asia_pacific_ex_japan",
    "north_america": "north_america",
    "emerging": "emerging",
}
_INDEX_SELECTORS = {
    **{name: name for name in datasets.INDEX_MEMBERS},
    "europe_ex_uk": "europe_without_uk",
}


def _normalize_selector(value: str) -> str:
    """Normalize documented selector spelling without changing its meaning."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.casefold())).strip("_")


def _factor_kind(value: str) -> str | None:
    """Map a public factor name to its statically allowlisted source suffix."""
    normalized = _normalize_selector(value)
    return _FACTOR_SUFFIXES.get(normalized)


def _factor_dataset_kind(dataset_key: str) -> str | None:
    """Identify the factor family encoded by one allowlisted dataset key."""
    for kind in sorted(set(_FACTOR_SUFFIXES.values()), key=len, reverse=True):
        if dataset_key.endswith(f"_{kind}"):
            return kind
    return None


def _archive_stem(filename: str) -> str:
    """Return a normalized source stem from an allowlisted CSV ZIP filename."""
    return _normalize_selector(filename.removesuffix("_CSV.zip"))


def _resolve_portfolio_selector(
    selector: str,
    table: dict[str, datasets.TableDataset],
) -> tuple[str, FrenchFrequency | None]:
    """Resolve a public portfolio filename slug to an allowlisted table key."""
    normalized = _normalize_selector(selector)
    forced_frequency: FrenchFrequency | None = None
    frequency_suffixes: tuple[tuple[str, FrenchFrequency], ...] = (
        ("_daily", "daily"),
        ("_weekly", "weekly"),
    )
    for suffix, frequency in frequency_suffixes:
        if normalized.endswith(suffix):
            forced_frequency = frequency
            break

    matches = [
        key
        for key, spec in table.items()
        if any(_archive_stem(filename) == normalized for filename in spec.files.values())
    ]
    if not matches and forced_frequency is not None:
        base_selector = normalized.removesuffix(f"_{forced_frequency}")
        matches = [
            key
            for key, spec in table.items()
            if any(
                frequency in {"monthly", "annual"} and _archive_stem(filename) == base_selector
                for frequency, filename in spec.files.items()
            )
        ]
    if not matches:
        raise ValueError("portfolio is not in the supported source allowlist")
    return matches[0], forced_frequency


def _portfolio_family(dataset: datasets.TableDataset) -> str:
    """Return the table identity used to check canonical/legacy selector pairs."""
    filename = dataset.files.get("monthly") or dataset.files.get("annual")
    if filename is None:
        filename = next(iter(dataset.files.values()))
    return _archive_stem(filename)


def _same_portfolio_family(
    left: datasets.TableDataset,
    right: datasets.TableDataset,
) -> bool:
    """Compare aliases by exact static archive family, not friendly key names."""
    return _portfolio_family(left) == _portfolio_family(right)


def _table_archive_text(
    filename: str,
    archive_format: _ARCHIVE_FORMAT,
    timeout: float | None,
) -> str:
    """Fetch one allowlisted CSV archive or its documented TXT mirror."""
    if archive_format == "csv":
        archive_name = filename
        suffix: Literal[".csv", ".txt"] = ".csv"
    else:
        if not filename.endswith("_CSV.zip"):
            raise FrenchProviderError("FRENCH_TXT_MIRROR_UNAVAILABLE")
        stem = filename[:-8]
        archive_name = stem + "_TXT.zip"
        suffix = ".txt"
    stem = archive_name[:-8]
    return archive_text(
        fetch_archive(archive_name, timeout),
        suffix=suffix,
        member_name=stem + suffix,
    )


class _FrenchQuery(QueryParams):
    """Common source/date bounds with explicit rejection of unused base fields."""

    expected_market: ClassVar[str]
    source: StrictStr = "auto"
    max_records: int = Field(default=100_000, ge=1, le=MAX_OUTPUT_ROWS, strict=True)

    @model_validator(mode="after")
    def _validate_common(self) -> _FrenchQuery:
        if self.source not in {"auto", "famafrench"}:
            raise ValueError("source must be auto or famafrench")
        if self.symbol is not None:
            raise ValueError("symbol is not a selector in the French Data Library")
        if self.market is not None and self.market != self.expected_market:
            raise ValueError("market must match the fetcher's declared market")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self


class BreakpointQuery(_FrenchQuery):
    """Selectors for one allowlisted French-library breakpoint table."""

    expected_market: ClassVar[str] = "us"
    breakpoint_type: StrictStr = "me"
    breakpoint: StrictStr | None = None
    dataset: StrictStr | None = None
    frequency: FrenchFrequency | None = None
    archive_format: _ARCHIVE_FORMAT = "csv"

    @property
    def dataset_key(self) -> str:
        """Resolve V5 selectors and the documented/legacy aliases to one file key."""
        canonical = _BREAKPOINT_SELECTORS.get(_normalize_selector(self.breakpoint_type))
        if canonical is None:
            raise ValueError("breakpoint_type is not in the supported source allowlist")
        candidates: list[str] = []
        if "breakpoint_type" in self.model_fields_set or not (
            self.breakpoint is not None or self.dataset is not None
        ):
            candidates.append(canonical)
        if self.breakpoint is not None:
            alias = _BREAKPOINT_SELECTORS.get(_normalize_selector(self.breakpoint))
            if alias is None:
                raise ValueError("breakpoint is not in the supported source allowlist")
            candidates.append(alias)
        if self.dataset is not None:
            alias = _BREAKPOINT_SELECTORS.get(_normalize_selector(self.dataset))
            if alias is None:
                raise ValueError("dataset is not in the breakpoint compatibility allowlist")
            candidates.append(alias)
        if len(set(candidates)) != 1:
            raise ValueError("breakpoint_type, breakpoint, and dataset selectors conflict")
        return candidates[0]

    @model_validator(mode="after")
    def _validate_dataset_and_frequency(self) -> BreakpointQuery:
        spec = datasets.BREAKPOINT_DATASETS[self.dataset_key]
        if self.frequency is not None and self.frequency != spec.frequency:
            raise ValueError("frequency does not match the official breakpoint source precision")
        return self

    @property
    def source_frequency(self) -> FrenchFrequency:
        """Return the frequency evidenced by the selected source table."""
        return datasets.BREAKPOINT_DATASETS[self.dataset_key].frequency


class FactorQuery(_FrenchQuery):
    """V5 region/factor selectors plus a checked legacy dataset alias."""

    expected_market: ClassVar[str] = "global"
    region: StrictStr = "america"
    factor: StrictStr = "3_factors"
    frequency: FrenchFrequency = "monthly"
    dataset: StrictStr | None = None
    archive_format: _ARCHIVE_FORMAT = "csv"

    @property
    def dataset_key(self) -> str:
        """Map the public region/factor pair to an allowlisted source table."""
        if self.dataset is not None:
            if self.dataset not in datasets.FACTOR_DATASETS:
                raise ValueError("dataset is not in the factor compatibility allowlist")
            spec = datasets.FACTOR_DATASETS[self.dataset]
            if "region" in self.model_fields_set:
                region_key = _FACTOR_REGIONS.get(_normalize_selector(self.region))
                if region_key != spec.region:
                    raise ValueError("dataset conflicts with the explicit region selector")
            if "factor" in self.model_fields_set:
                factor_key = _factor_kind(self.factor)
                if factor_key is None or _factor_dataset_kind(self.dataset) != factor_key:
                    raise ValueError("dataset conflicts with the explicit factor selector")
            return self.dataset
        region_key = _FACTOR_REGIONS.get(_normalize_selector(self.region))
        factor_key = _factor_kind(self.factor)
        if region_key is None or factor_key is None:
            raise ValueError("region/factor is not in the supported source allowlist")
        return f"{region_key}_{factor_key}"

    @model_validator(mode="after")
    def _validate_dataset_and_frequency(self) -> FactorQuery:
        spec = datasets.FACTOR_DATASETS.get(self.dataset_key)
        if spec is None or self.frequency not in spec.files:
            raise ValueError("region/factor/frequency is not in the official factor allowlist")
        return self


class _PortfolioQuery(_FrenchQuery):
    """Public portfolio selector and optional compatibility dataset alias."""

    expected_market: ClassVar[str]
    portfolio: StrictStr
    frequency: FrenchFrequency = "monthly"
    dataset: StrictStr | None = None
    archive_format: _ARCHIVE_FORMAT = "csv"
    measure: _WEIGHTING = "value"

    @property
    def resolved_dataset(self) -> str:
        """Resolve public filename selectors without accepting caller paths."""
        table = self.dataset_table
        if self.dataset is not None and self.dataset not in table:
            raise ValueError("dataset is not in the portfolio compatibility allowlist")
        if "portfolio" not in self.model_fields_set and self.dataset is not None:
            return self.dataset
        canonical_key, _ = _resolve_portfolio_selector(self.portfolio, table)
        if self.dataset is not None and not _same_portfolio_family(
            table[self.dataset], table[canonical_key]
        ):
            raise ValueError("portfolio conflicts with the dataset compatibility selector")
        return canonical_key

    @property
    def source_frequency(self) -> FrenchFrequency:
        """Apply a daily/weekly suffix, otherwise use the documented frequency."""
        if "portfolio" in self.model_fields_set:
            _, suffix_frequency = _resolve_portfolio_selector(self.portfolio, self.dataset_table)
            if suffix_frequency is not None:
                return suffix_frequency
        return self.frequency

    @property
    def dataset_table(self) -> dict[str, datasets.TableDataset]:
        """Return the provider-specific static table allowlist."""
        raise NotImplementedError

    def validate_source_selection(self) -> None:
        """Reject selectors that do not resolve to a published table/frequency."""
        spec = self.dataset_table.get(self.resolved_dataset)
        if spec is None or self.source_frequency not in spec.files:
            raise ValueError("portfolio/frequency is not in the official source allowlist")


class PortfolioQuery(_PortfolioQuery):
    """Selectors for one allowlisted US portfolio return table."""

    expected_market: ClassVar[str] = "us"
    portfolio: StrictStr = "portfolios_formed_on_me"

    @property
    def dataset_table(self) -> dict[str, datasets.TableDataset]:
        """Return the static U.S. portfolio archive allowlist."""
        return datasets.US_PORTFOLIO_DATASETS

    @model_validator(mode="after")
    def _validate_dataset_and_frequency(self) -> PortfolioQuery:
        self.validate_source_selection()
        return self


class RegionalPortfolioQuery(_PortfolioQuery):
    """Selectors for one allowlisted regional portfolio return table."""

    expected_market: ClassVar[str] = "global"
    portfolio: StrictStr = "developed_ex_us_6_portfolios_me_op"

    @property
    def dataset_table(self) -> dict[str, datasets.TableDataset]:
        """Return the static regional portfolio archive allowlist."""
        return datasets.REGIONAL_PORTFOLIO_DATASETS

    @model_validator(mode="after")
    def _validate_dataset_and_frequency(self) -> RegionalPortfolioQuery:
        self.validate_source_selection()
        return self


class CountryPortfolioQuery(_FrenchQuery):
    """Selectors and filters for a country portfolio archive."""

    expected_market: ClassVar[str] = "country"
    country: StrictStr = "united_kingdom"
    frequency: Literal["monthly", "annual"] = "monthly"
    measure: _COUNTRY_MEASURE = "usd"
    all_data_items_required: bool = Field(default=True, strict=True)
    dividends: bool | None = Field(default=None, strict=True)
    sort: FrenchSort | None = None
    portfolio: FrenchPortfolioSide | None = None

    @model_validator(mode="after")
    def _validate_country_and_filters(self) -> CountryPortfolioQuery:
        if self.country not in datasets.COUNTRIES:
            raise ValueError("country is not in the official country archive allowlist")
        _validate_dat_portfolio_filter(self.sort, self.portfolio)
        if self.measure == "ratios" and (self.sort is not None or self.portfolio is not None):
            raise ValueError("sort and portfolio selectors are not valid for country ratios")
        return self

    @property
    def source_frequency(self) -> Literal["monthly", "annual"]:
        """Return annual source precision for the frequency-independent ratio table."""
        return "annual" if self.measure == "ratios" else self.frequency

    @property
    def source_dividends(self) -> bool:
        """Use the provider's documented-with-dividends archive when unspecified."""
        return True if self.dividends is None else self.dividends

    @property
    def source_requirement(self) -> FrenchDataRequirement:
        """Translate the public all-data-items selector to the source section label."""
        return "required" if self.all_data_items_required else "not_required"


class InternationalIndexQuery(_FrenchQuery):
    """Selectors and filters for an international index archive."""

    expected_market: ClassVar[str] = "global"
    index: StrictStr = "all"
    region: StrictStr | None = None
    frequency: Literal["monthly", "annual"] = "monthly"
    measure: _COUNTRY_MEASURE = "usd"
    all_data_items_required: bool = Field(default=True, strict=True)
    dividends: bool | None = Field(default=None, strict=True)
    sort: FrenchSort | None = None
    portfolio: FrenchPortfolioSide | None = None

    @model_validator(mode="after")
    def _validate_region_and_filters(self) -> InternationalIndexQuery:
        if self.source_index_key not in datasets.INDEX_MEMBERS:
            raise ValueError("index is not in the official international index allowlist")
        _validate_dat_portfolio_filter(self.sort, self.portfolio)
        return self

    @property
    def source_index_key(self) -> str:
        """Resolve the public index selector and checked legacy region alias."""
        index_key = _INDEX_SELECTORS.get(_normalize_selector(self.index))
        if index_key is None:
            raise ValueError("index is not in the supported source allowlist")
        if self.region is None:
            return index_key
        region_key = _INDEX_SELECTORS.get(_normalize_selector(self.region))
        if region_key is None:
            raise ValueError("region is not in the index compatibility allowlist")
        if "index" in self.model_fields_set and index_key != region_key:
            raise ValueError("index and region compatibility selectors conflict")
        return index_key if "index" in self.model_fields_set else region_key

    @property
    def source_frequency(self) -> Literal["monthly", "annual"]:
        """Return the selected return-table frequency."""
        return self.frequency

    @property
    def source_requirement(self) -> FrenchDataRequirement:
        """Translate the public all-data-items selector to the source section label."""
        return "required" if self.all_data_items_required else "not_required"

    @property
    def source_dividends(self) -> bool:
        """Use the provider's documented-with-dividends archive when unspecified."""
        return True if self.dividends is None else self.dividends


def _validate_dat_portfolio_filter(
    sort: FrenchSort | None,
    portfolio: FrenchPortfolioSide | None,
) -> None:
    if portfolio is not None and sort is None:
        raise ValueError("portfolio requires a sort selector")
    if portfolio == "market" and sort not in {None, "market"}:
        raise ValueError("market portfolio is valid only with market sort")
    if portfolio == "zero" and sort not in {None, "dividend_yield"}:
        raise ValueError("zero portfolio is published only for dividend-yield sort")
    if sort == "market" and portfolio not in {None, "market"}:
        raise ValueError("market sort has only the market portfolio")


def _check_output_limit(length: int, params: _FrenchQuery) -> None:
    if length > params.max_records:
        raise FrenchProviderError("FRENCH_RESULT_LIMIT")


def _wide_dat_return_rows(rows: list[SourceDatReturn]) -> list[dict[str, object]]:
    """Map source sort/portfolio cells to one public conditional row per date."""
    rows_by_date: dict[date, dict[str, object]] = {}
    for row in rows:
        field_name = _DAT_PUBLIC_FIELDS.get((row.sort, row.portfolio))
        if field_name is None:
            raise FrenchProviderError("FRENCH_DAT_FIELD_UNMAPPED")
        record = rows_by_date.setdefault(row.date, {"date": row.date})
        if field_name in record:
            raise FrenchProviderError("FRENCH_DUPLICATE_DAT_RETURN_FIELD")
        record[field_name] = row.value
    return [rows_by_date[record_date] for record_date in sorted(rows_by_date)]


def _ratio_row(row: SourceDatRatio) -> dict[str, object]:
    """Project an official country ratio row onto the documented public fields."""
    return {
        "date": row.date,
        "firms": row.firms,
        "bm": row.bm,
        "ep": row.ep,
        "ce_p": row.ce_p,
        "yld": row.yld,
    }


class FamaFrenchBreakpointsFetcher(Fetcher[BreakpointQuery, list[SourceBreakpoint]]):
    """Read official ME, accounting-ratio, and prior-return breakpoint archives."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FamaFrenchBreakpoints"
    row_model: ClassVar[type[ContractModel]] = FamaFrenchBreakpoint
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="famafrench_breakpoints",
        period="variable",
        market="us",
        source="famafrench",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> BreakpointQuery:
        """Validate the dataset and frequency requested for breakpoints."""
        return BreakpointQuery.model_validate(kwargs)

    def extract_data(self, params: BreakpointQuery, ctx: FetchContext) -> list[SourceBreakpoint]:
        """Fetch and parse the selected breakpoint archive."""
        spec = datasets.BREAKPOINT_DATASETS[params.dataset_key]
        raw_text = _table_archive_text(spec.filename, params.archive_format, ctx.timeout)
        return parse_breakpoint_table(
            raw_text,
            dataset=params.dataset_key,
            frequency=spec.frequency,
            period_frequency=spec.period_frequency,
            annual_period_end_month=spec.annual_period_end_month,
            archive_format=params.archive_format,
            count_columns=spec.count_columns,
            start_date=params.start_date,
            end_date=params.end_date,
        )

    def transform_data(self, raw: list[SourceBreakpoint], params: BreakpointQuery) -> FetchResult:
        """Pivot source percentile cells into the public one-row-per-date shape."""
        spec = datasets.BREAKPOINT_DATASETS[params.dataset_key]
        rows_by_date: dict[date, dict[str, object]] = {}
        for row in raw:
            record = rows_by_date.setdefault(row.date, {"date": row.date})
            percentile_field = f"percentile_{row.percentile}"
            if percentile_field not in {
                f"percentile_{percentile}" for percentile in _BREAKPOINT_PERCENTILES
            }:
                raise FrenchProviderError("FRENCH_BREAKPOINT_PERCENTILE_UNMAPPED")
            if percentile_field in record:
                raise FrenchProviderError("FRENCH_DUPLICATE_BREAKPOINT_ROW")
            record[percentile_field] = row.value
            for count, source_name in (
                (row.count_1, spec.count_1_name),
                (row.count_2, spec.count_2_name),
            ):
                if source_name is None:
                    continue
                count_field = _BREAKPOINT_COUNT_FIELDS.get(source_name)
                if count_field is None or count is None:
                    raise FrenchProviderError("FRENCH_BREAKPOINT_COUNT_UNMAPPED")
                if count_field in record and record[count_field] != count:
                    raise FrenchProviderError("FRENCH_BREAKPOINT_COUNT_INCONSISTENT")
                record[count_field] = count
        expected_fields = {f"percentile_{percentile}" for percentile in _BREAKPOINT_PERCENTILES}
        for record in rows_by_date.values():
            if expected_fields.difference(record):
                raise FrenchProviderError("FRENCH_BREAKPOINT_PERCENTILE_INCOMPLETE")
        _check_output_limit(len(rows_by_date), params)
        return tuple(
            FamaFrenchBreakpoint.model_validate(rows_by_date[record_date])
            for record_date in sorted(rows_by_date)
        )


class FamaFrenchFactorsFetcher(Fetcher[FactorQuery, list[SourceObservation]]):
    """Fetch US and international factor sets from official CSV or TXT ZIP archives."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FamaFrenchFactors"
    row_model: ClassVar[type[ContractModel]] = FamaFrenchFactorObservation
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="famafrench_factors",
        period="variable",
        market="global",
        source="famafrench",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> FactorQuery:
        """Validate the factor dataset and source frequency."""
        return FactorQuery.model_validate(kwargs)

    def extract_data(self, params: FactorQuery, ctx: FetchContext) -> list[SourceObservation]:
        """Fetch and parse the selected factor archive."""
        spec = datasets.FACTOR_DATASETS[params.dataset_key]
        raw_text = _table_archive_text(
            spec.files[params.frequency], params.archive_format, ctx.timeout
        )
        parser = parse_factor_table if params.archive_format == "csv" else parse_factor_text_table
        return parser(
            raw_text,
            dataset=params.dataset_key,
            frequency=params.frequency,
            start_date=params.start_date,
            end_date=params.end_date,
        )

    def transform_data(self, raw: list[SourceObservation], params: FactorQuery) -> FetchResult:
        """Pivot selected source factor columns into one public row per date."""
        rows_by_date: dict[date, dict[str, object]] = {}
        for row in raw:
            factor_field = _FACTOR_FIELDS.get(row.key.casefold())
            if factor_field is None:
                raise FrenchProviderError("FRENCH_FACTOR_FIELD_UNMAPPED")
            record = rows_by_date.setdefault(row.date, {"date": row.date})
            if factor_field in record:
                raise FrenchProviderError("FRENCH_DUPLICATE_FACTOR_ROW")
            record[factor_field] = row.value
        if not rows_by_date:
            raise FrenchProviderError("FRENCH_EMPTY_RESULT")
        _check_output_limit(len(rows_by_date), params)
        return tuple(
            FamaFrenchFactorObservation.model_validate(rows_by_date[record_date])
            for record_date in sorted(rows_by_date)
        )


class FamaFrenchUSPortfolioReturnsFetcher(Fetcher[PortfolioQuery, list[SourceObservation]]):
    """Read value/equal-weighted US portfolio return tables."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FamaFrenchUSPortfolioReturns"
    row_model: ClassVar[type[ContractModel]] = FamaFrenchPortfolioReturn
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="famafrench_us_portfolio_returns",
        period="variable",
        market="us",
        source="famafrench",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> PortfolioQuery:
        """Validate US portfolio dataset, frequency, and weighting selectors."""
        return PortfolioQuery.model_validate(kwargs)

    def extract_data(self, params: PortfolioQuery, ctx: FetchContext) -> list[SourceObservation]:
        """Fetch and parse the selected US portfolio archive."""
        spec = params.dataset_table[params.resolved_dataset]
        frequency = params.source_frequency
        raw_text = _table_archive_text(spec.files[frequency], params.archive_format, ctx.timeout)
        parser = (
            parse_portfolio_table if params.archive_format == "csv" else parse_portfolio_text_table
        )
        return parser(
            raw_text,
            frequency=frequency,
            weighting=params.measure,
            start_date=params.start_date,
            end_date=params.end_date,
        )

    def transform_data(self, raw: list[SourceObservation], params: PortfolioQuery) -> FetchResult:
        """Build typed rows for the parsed US portfolio returns."""
        _check_output_limit(len(raw), params)
        return tuple(
            FamaFrenchPortfolioReturn.model_validate(
                {
                    "date": row.date,
                    "portfolio": row.key,
                    "measure": params.measure,
                    "value": row.value,
                }
            )
            for row in raw
        )


class FamaFrenchRegionalPortfolioReturnsFetcher(
    Fetcher[RegionalPortfolioQuery, list[SourceObservation]]
):
    """Read developed, emerging, and regional portfolio return archives."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FamaFrenchRegionalPortfolioReturns"
    row_model: ClassVar[type[ContractModel]] = FamaFrenchPortfolioReturn
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="famafrench_regional_portfolio_returns",
        period="variable",
        market="global",
        source="famafrench",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> RegionalPortfolioQuery:
        """Validate the regional portfolio dataset and frequency."""
        return RegionalPortfolioQuery.model_validate(kwargs)

    def extract_data(
        self, params: RegionalPortfolioQuery, ctx: FetchContext
    ) -> list[SourceObservation]:
        """Fetch and parse the selected regional portfolio archive."""
        spec = params.dataset_table[params.resolved_dataset]
        frequency = params.source_frequency
        raw_text = _table_archive_text(spec.files[frequency], params.archive_format, ctx.timeout)
        parser = (
            parse_portfolio_table if params.archive_format == "csv" else parse_portfolio_text_table
        )
        return parser(
            raw_text,
            frequency=frequency,
            weighting=params.measure,
            start_date=params.start_date,
            end_date=params.end_date,
        )

    def transform_data(
        self, raw: list[SourceObservation], params: RegionalPortfolioQuery
    ) -> FetchResult:
        """Build typed rows for parsed regional portfolio returns."""
        _check_output_limit(len(raw), params)
        return tuple(
            FamaFrenchPortfolioReturn.model_validate(
                {
                    "date": row.date,
                    "portfolio": row.key,
                    "measure": params.measure,
                    "value": row.value,
                }
            )
            for row in raw
        )


class FamaFrenchCountryPortfolioReturnsFetcher(
    Fetcher[CountryPortfolioQuery, list[SourceDatReturn | SourceDatRatio]]
):
    """Parse one of the official country ``.Dat`` files from a ZIP archive."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FamaFrenchCountryPortfolioReturns"
    row_model: ClassVar[type[ContractModel]] = FamaFrenchCountryPortfolioReturn
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="famafrench_country_portfolio_returns",
        period="variable",
        market="country",
        source="famafrench",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> CountryPortfolioQuery:
        """Validate country, frequency, and optional portfolio selectors."""
        return CountryPortfolioQuery.model_validate(kwargs)

    def extract_data(
        self, params: CountryPortfolioQuery, ctx: FetchContext
    ) -> list[SourceDatReturn | SourceDatRatio]:
        """Fetch country returns or the documented annual country ratios."""
        archive = fetch_archive(datasets.COUNTRY_ARCHIVES[params.source_dividends], ctx.timeout)
        text = archive_text(archive, suffix=".dat", member_name=datasets.COUNTRIES[params.country])
        if params.measure == "ratios":
            ratio_rows: list[SourceDatReturn | SourceDatRatio] = []
            ratio_rows.extend(
                parse_dat_ratios(
                    text,
                    data_requirement=params.source_requirement,
                    start_date=params.start_date,
                    end_date=params.end_date,
                )
            )
            return ratio_rows
        rows = parse_dat_returns(
            text,
            frequency=params.source_frequency,
            currency_basis="dollar" if params.measure == "usd" else "local",
            data_requirement=params.source_requirement,
            start_date=params.start_date,
            end_date=params.end_date,
        )
        return [
            row
            for row in rows
            if (params.sort is None or row.sort == params.sort)
            and (params.portfolio is None or row.portfolio == params.portfolio)
        ]

    def transform_data(
        self, raw: list[SourceDatReturn | SourceDatRatio], params: CountryPortfolioQuery
    ) -> FetchResult:
        """Build typed country rows in the published wide return or ratio shape."""
        if not raw:
            raise FrenchProviderError("FRENCH_EMPTY_RESULT")
        if params.measure == "ratios":
            ratio_rows = [row for row in raw if isinstance(row, SourceDatRatio)]
            if len(ratio_rows) != len(raw):
                raise FrenchProviderError("FRENCH_DAT_ROW_KIND_MISMATCH")
            _check_output_limit(len(ratio_rows), params)
            return tuple(
                FamaFrenchCountryPortfolioReturn.model_validate(_ratio_row(row))
                for row in ratio_rows
            )
        return_rows = [row for row in raw if isinstance(row, SourceDatReturn)]
        if len(return_rows) != len(raw):
            raise FrenchProviderError("FRENCH_DAT_ROW_KIND_MISMATCH")
        wide_rows = _wide_dat_return_rows(return_rows)
        _check_output_limit(len(wide_rows), params)
        return tuple(FamaFrenchCountryPortfolioReturn.model_validate(row) for row in wide_rows)


class FamaFrenchInternationalIndexReturnsFetcher(
    Fetcher[InternationalIndexQuery, list[SourceDatReturn]]
):
    """Parse the requested official region ``.Dat`` file from the index ZIP."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FamaFrenchInternationalIndexReturns"
    row_model: ClassVar[type[ContractModel]] = FamaFrenchInternationalIndexReturn
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="famafrench_international_index_returns",
        period="variable",
        market="global",
        source="famafrench",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> InternationalIndexQuery:
        """Validate region, frequency, and optional portfolio selectors."""
        params = InternationalIndexQuery.model_validate(kwargs)
        if params.measure == "ratios":
            raise FrenchProviderError("FRENCH_INDEX_RATIO_SHAPE_UNSUPPORTED")
        return params

    def extract_data(
        self, params: InternationalIndexQuery, ctx: FetchContext
    ) -> list[SourceDatReturn]:
        """Fetch and filter the selected international index archive."""
        archive = fetch_archive(datasets.INDEX_ARCHIVES[params.source_dividends], ctx.timeout)
        text = archive_text(
            archive, suffix=".dat", member_name=datasets.INDEX_MEMBERS[params.source_index_key]
        )
        rows = parse_dat_returns(
            text,
            frequency=params.frequency,
            currency_basis="dollar" if params.measure == "usd" else "local",
            data_requirement=params.source_requirement,
            start_date=params.start_date,
            end_date=params.end_date,
        )
        return [
            row
            for row in rows
            if (params.sort is None or row.sort == params.sort)
            and (params.portfolio is None or row.portfolio == params.portfolio)
        ]

    def transform_data(
        self, raw: list[SourceDatReturn], params: InternationalIndexQuery
    ) -> FetchResult:
        """Build typed international-index rows in the published wide shape."""
        if not raw:
            raise FrenchProviderError("FRENCH_EMPTY_RESULT")
        wide_rows = _wide_dat_return_rows(raw)
        _check_output_limit(len(wide_rows), params)
        return tuple(FamaFrenchInternationalIndexReturn.model_validate(row) for row in wide_rows)
