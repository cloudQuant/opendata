"""Static allowlists for files published by Kenneth French's Data Library."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class BreakpointDataset:
    """Published archive, shape, and source-period facts for one breakpoint family."""

    filename: str
    frequency: Literal["daily", "weekly", "monthly", "annual"]
    count_columns: int
    count_1_name: str
    count_2_name: str | None
    units: str
    period_frequency: str | None = None
    annual_period_end_month: int | None = None


BREAKPOINT_DATASETS: Mapping[str, BreakpointDataset] = MappingProxyType(
    {
        "me": BreakpointDataset(
            "ME_Breakpoints_CSV.zip", "monthly", 1, "sample", None, "million_usd"
        ),
        "be_me": BreakpointDataset(
            "BE-ME_Breakpoints_CSV.zip",
            "annual",
            2,
            "nonpositive",
            "positive",
            "ratio",
            annual_period_end_month=6,
        ),
        "operating_profitability": BreakpointDataset(
            "OP_Breakpoints_CSV.zip",
            "annual",
            1,
            "sample",
            None,
            "ratio",
            annual_period_end_month=6,
        ),
        "investment": BreakpointDataset(
            "INV_Breakpoints_CSV.zip",
            "annual",
            1,
            "sample",
            None,
            "ratio",
            annual_period_end_month=6,
        ),
        "earnings_price": BreakpointDataset(
            "E-P_Breakpoints_CSV.zip",
            "annual",
            2,
            "nonpositive",
            "positive",
            "percent",
            annual_period_end_month=6,
        ),
        "cashflow_price": BreakpointDataset(
            "CF-P_Breakpoints_CSV.zip",
            "annual",
            2,
            "nonpositive",
            "positive",
            "percent",
            annual_period_end_month=6,
        ),
        "dividend_yield": BreakpointDataset(
            "D-P_Breakpoints_CSV.zip", "annual", 2, "nonpositive", "positive", "percent", "monthly"
        ),
        "prior_2_12_return": BreakpointDataset(
            "Prior_2-12_Breakpoints_CSV.zip", "monthly", 1, "sample", None, "percent"
        ),
    }
)


COUNTRIES: Mapping[str, str] = MappingProxyType(
    {
        "australia": "Austrlia.Dat",
        "austria": "Austria.Dat",
        "belgium": "Belgium.Dat",
        "canada": "Canada.Dat",
        "denmark": "Denmark.Dat",
        "finland": "Finland.Dat",
        "france": "France.Dat",
        "germany": "Germany.Dat",
        "hong_kong": "HongKong.Dat",
        "ireland": "Ireland.Dat",
        "italy": "Italy.Dat",
        "japan": "Japan.Dat",
        "malaysia": "Malaysia.Dat",
        "netherlands": "Nethrlnd.Dat",
        "new_zealand": "NewZland.Dat",
        "norway": "Norway.Dat",
        "singapore": "Singapor.Dat",
        "spain": "Spain.Dat",
        "sweden": "Sweden.Dat",
        "switzerland": "Swtzrlnd.Dat",
        "united_kingdom": "UK.Dat",
    }
)

COUNTRY_ARCHIVES: Mapping[bool, str] = MappingProxyType(
    {True: "F-F_International_Countries.zip", False: "F-F_International_Countries_Wout_Div.zip"}
)

INDEX_MEMBERS: Mapping[str, str] = MappingProxyType(
    {
        "all": "Ind_all.Dat",
        "asia_pacific": "Ind_Asia_Pacific.Dat",
        "europe_with_uk": "Ind_Eur_With_UK.Dat",
        "europe_without_uk": "Ind_Eur_WOut_UK.Dat",
        "scandinavia": "Ind_Scandanavia.Dat",
        "united_kingdom": "Ind_UK.Dat",
    }
)

INDEX_ARCHIVES: Mapping[bool, str] = MappingProxyType(
    {True: "F-F_International_Indices.zip", False: "F-F_International_Indices_Wout_Div.zip"}
)


@dataclass(frozen=True)
class TableDataset:
    """A model-approved data archive with period-specific official URLs."""

    files: Mapping[str, str]
    region: str


def _factor(files: Mapping[str, str], region: str) -> TableDataset:
    return TableDataset(MappingProxyType(dict(files)), region)


FACTOR_DATASETS: dict[str, TableDataset] = {}
FACTOR_DATASETS["us_ff3"] = _factor(
    {
        "monthly": "F-F_Research_Data_Factors_CSV.zip",
        "annual": "F-F_Research_Data_Factors_CSV.zip",
        "weekly": "F-F_Research_Data_Factors_weekly_CSV.zip",
        "daily": "F-F_Research_Data_Factors_daily_CSV.zip",
    },
    "us",
)
FACTOR_DATASETS["us_ff5"] = _factor(
    {
        "monthly": "F-F_Research_Data_5_Factors_2x3_CSV.zip",
        "annual": "F-F_Research_Data_5_Factors_2x3_CSV.zip",
        "daily": "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip",
    },
    "us",
)
FACTOR_DATASETS["us_momentum"] = _factor(
    {
        "monthly": "F-F_Momentum_Factor_CSV.zip",
        "annual": "F-F_Momentum_Factor_CSV.zip",
        "daily": "F-F_Momentum_Factor_daily_CSV.zip",
    },
    "us",
)
FACTOR_DATASETS["us_short_term_reversal"] = _factor(
    {
        "monthly": "F-F_ST_Reversal_Factor_CSV.zip",
        "annual": "F-F_ST_Reversal_Factor_CSV.zip",
        "daily": "F-F_ST_Reversal_Factor_daily_CSV.zip",
    },
    "us",
)
FACTOR_DATASETS["us_long_term_reversal"] = _factor(
    {
        "monthly": "F-F_LT_Reversal_Factor_CSV.zip",
        "annual": "F-F_LT_Reversal_Factor_CSV.zip",
        "daily": "F-F_LT_Reversal_Factor_daily_CSV.zip",
    },
    "us",
)

_REGIONAL_FACTOR_PREFIXES = {
    "developed": "Developed",
    "developed_ex_us": "Developed_ex_US",
    "europe": "Europe",
    "japan": "Japan",
    "asia_pacific_ex_japan": "Asia_Pacific_ex_Japan",
    "north_america": "North_America",
}
for _region, _prefix in _REGIONAL_FACTOR_PREFIXES.items():
    for _number in (3, 5):
        _stem = f"{_prefix}_{_number}_Factors"
        FACTOR_DATASETS[f"{_region}_ff{_number}"] = _factor(
            {
                "monthly": f"{_stem}_CSV.zip",
                "annual": f"{_stem}_CSV.zip",
                "daily": f"{_stem}_Daily_CSV.zip",
            },
            _region,
        )
    _momentum_stem = f"{_prefix}_Mom_Factor"
    if _region == "asia_pacific_ex_japan":
        _momentum_stem = f"{_prefix}_MOM_Factor"
    FACTOR_DATASETS[f"{_region}_momentum"] = _factor(
        {
            "monthly": f"{_momentum_stem}_CSV.zip",
            "annual": f"{_momentum_stem}_CSV.zip",
            "daily": f"{_momentum_stem}_Daily_CSV.zip",
        },
        _region,
    )
FACTOR_DATASETS["emerging_ff5"] = _factor(
    {"monthly": "Emerging_5_Factors_CSV.zip", "annual": "Emerging_5_Factors_CSV.zip"},
    "emerging",
)
FACTOR_DATASETS["emerging_momentum"] = _factor(
    {"monthly": "Emerging_MOM_Factor_CSV.zip", "annual": "Emerging_MOM_Factor_CSV.zip"},
    "emerging",
)


def _table_files(
    monthly: str,
    *,
    daily: str | None = None,
    weekly: str | None = None,
) -> Mapping[str, str]:
    files = {"monthly": monthly, "annual": monthly}
    if daily:
        files["daily"] = daily
    if weekly:
        files["weekly"] = weekly
    return MappingProxyType(files)


US_PORTFOLIO_DATASETS: dict[str, TableDataset] = {}
for _count, _stem, _daily, _weekly in (
    ("6", "6_Portfolios_2x3", "6_Portfolios_2x3_daily_CSV.zip", "6_Portfolios_2x3_weekly_CSV.zip"),
    ("25", "25_Portfolios_5x5", "25_Portfolios_5x5_Daily_CSV.zip", None),
    ("100", "100_Portfolios_10x10", "100_Portfolios_10x10_Daily_CSV.zip", None),
):
    US_PORTFOLIO_DATASETS[f"{_count}_beme"] = TableDataset(
        _table_files(f"{_stem}_CSV.zip", daily=_daily, weekly=_weekly), "us"
    )
    _without_dividends = _stem + "_Wout_Div"
    if _count in {"6", "25", "100"}:
        US_PORTFOLIO_DATASETS[f"{_count}_beme_ex_dividends"] = TableDataset(
            _table_files(f"{_without_dividends}_CSV.zip"), "us"
        )

for _sort, _official in (("me", "ME"), ("be_me", "BE-ME"), ("op", "OP"), ("investment", "INV")):
    _files = _table_files(
        f"Portfolios_Formed_on_{_official}_CSV.zip",
        daily=f"Portfolios_Formed_on_{_official}_Daily_CSV.zip"
        if _sort in {"me", "be_me", "op", "investment"}
        else None,
    )
    US_PORTFOLIO_DATASETS[f"single_{_sort}"] = TableDataset(_files, "us")

for _sort, _official in (
    ("earnings_price", "E-P"),
    ("cashflow_price", "CF-P"),
    ("dividend_yield", "D-P"),
):
    _monthly = f"Portfolios_Formed_on_{_official}_CSV.zip"
    US_PORTFOLIO_DATASETS[f"single_{_sort}"] = TableDataset(_table_files(_monthly), "us")

for _count, _base in (("6", "2x3"), ("25", "5x5")):
    _count_prefix = f"{_count}_Portfolios"
    for _sort, _official in (
        ("me_op", "ME_OP"),
        ("me_investment", "ME_INV"),
        ("beme_op", "BEME_OP"),
        ("beme_investment", "BEME_INV"),
        ("op_investment", "OP_INV"),
    ):
        if _count == "6" and _sort in {"beme_op", "beme_investment", "op_investment"}:
            continue
        _stem = f"{_count_prefix}_{_official}_{_base}"
        US_PORTFOLIO_DATASETS[f"{_count}_{_sort}"] = TableDataset(
            _table_files(f"{_stem}_CSV.zip"), "us"
        )

for _industry_count in (5, 10, 12, 17, 30, 38, 48, 49):
    _stem = f"{_industry_count}_Industry_Portfolios"
    _daily = f"{_stem}_daily_CSV.zip"
    US_PORTFOLIO_DATASETS[f"industry_{_industry_count}"] = TableDataset(
        _table_files(f"{_stem}_CSV.zip", daily=_daily), "us"
    )


# The official library has more US portfolio files than the short friendly
# aliases above. Keep exact published stems addressable as explicit dataset
# ids so the generic model covers the full public CSV table families without
# accepting caller-controlled archive names.
def _register_us_archive(
    stem: str,
    *,
    daily_stem: str | None = None,
    weekly_stem: str | None = None,
) -> None:
    US_PORTFOLIO_DATASETS.setdefault(
        stem.casefold(),
        TableDataset(
            _table_files(
                f"{stem}_CSV.zip",
                daily=None if daily_stem is None else f"{daily_stem}_CSV.zip",
                weekly=None if weekly_stem is None else f"{weekly_stem}_CSV.zip",
            ),
            "us",
        ),
    )


_US_EXTRA_PORTFOLIO_LAYOUTS: tuple[tuple[str, str | None, str | None], ...] = (
    ("100_Portfolios_10x10", "100_Portfolios_10x10_Daily", None),
    ("100_Portfolios_10x10_Wout_Div", None, None),
    ("100_Portfolios_ME_INV_10x10", "100_Portfolios_ME_INV_10x10_daily", None),
    ("100_Portfolios_10x10_ME_INV_Wout_Div", None, None),
    ("100_Portfolios_ME_OP_10x10", "100_Portfolios_ME_OP_10x10_daily", None),
    ("100_Portfolios_10x10_ME_OP_Wout_Div", None, None),
    ("10_Industry_Portfolios", "10_Industry_Portfolios_daily", None),
    ("10_Industry_Portfolios_Wout_Div", None, None),
    ("10_Portfolios_Prior_12_2", "10_Portfolios_Prior_12_2_Daily", None),
    ("10_Portfolios_Prior_1_0", "10_Portfolios_Prior_1_0_Daily", None),
    ("10_Portfolios_Prior_60_13", "10_Portfolios_Prior_60_13_Daily", None),
    ("12_Industry_Portfolios", "12_Industry_Portfolios_daily", None),
    ("12_Industry_Portfolios_Wout_Div", None, None),
    ("17_Industry_Portfolios", "17_Industry_Portfolios_daily", None),
    ("17_Industry_Portfolios_Wout_Div", None, None),
    ("25_Portfolios_5x5", "25_Portfolios_5x5_Daily", None),
    ("25_Portfolios_5x5_Wout_Div", None, None),
    ("25_Portfolios_BEME_INV_5x5", "25_Portfolios_BEME_INV_5x5_daily", None),
    ("25_Portfolios_BEME_INV_5x5_Wout_Div", None, None),
    ("25_Portfolios_BEME_OP_5x5", "25_Portfolios_BEME_OP_5x5_daily", None),
    ("25_Portfolios_BEME_OP_5x5_Wout_Div", None, None),
    ("25_Portfolios_ME_AC_5x5", None, None),
    ("25_Portfolios_ME_BETA_5x5", None, None),
    ("25_Portfolios_ME_INV_5x5", "25_Portfolios_ME_INV_5x5_daily", None),
    ("25_Portfolios_ME_INV_5x5_Wout_Div", None, None),
    ("25_Portfolios_ME_NI_5x5", None, None),
    ("25_Portfolios_ME_OP_5x5", "25_Portfolios_ME_OP_5x5_daily", None),
    ("25_Portfolios_ME_OP_5x5_Wout_Div", None, None),
    ("25_Portfolios_ME_Prior_12_2", "25_Portfolios_ME_Prior_12_2_Daily", None),
    ("25_Portfolios_ME_Prior_1_0", "25_Portfolios_ME_Prior_1_0_Daily", None),
    ("25_Portfolios_ME_Prior_60_13", "25_Portfolios_ME_Prior_60_13_Daily", None),
    ("25_Portfolios_ME_RESVAR_5x5", None, None),
    ("25_Portfolios_ME_VAR_5x5", None, None),
    ("25_Portfolios_OP_INV_5x5", "25_Portfolios_OP_INV_5x5_daily", None),
    ("25_Portfolios_OP_INV_5x5_Wout_Div", None, None),
    ("30_Industry_Portfolios", "30_Industry_Portfolios_daily", None),
    ("30_Industry_Portfolios_Wout_Div", None, None),
    ("32_Portfolios_ME_BEME_INV_2x4x4", None, None),
    ("32_Portfolios_ME_BEME_OP_2x4x4", None, None),
    ("32_Portfolios_ME_OP_INV_2x4x4", None, None),
    ("32_Portfolios_ME_BEME_INV_2x4x4_Wout_Div", None, None),
    ("32_Portfolios_ME_BEME_OP_2x4x4_Wout_Div", None, None),
    ("32_Portfolios_ME_OP_INV_2x4x4_Wout_Div", None, None),
    ("38_Industry_Portfolios", "38_Industry_Portfolios_daily", None),
    ("38_Industry_Portfolios_Wout_Div", None, None),
    ("48_Industry_Portfolios", "48_Industry_Portfolios_daily", None),
    ("48_Industry_Portfolios_Wout_Div", None, None),
    ("49_Industry_Portfolios", "49_Industry_Portfolios_daily", None),
    ("49_Industry_Portfolios_Wout_Div", None, None),
    ("5_Industry_Portfolios", "5_Industry_Portfolios_daily", None),
    ("5_Industry_Portfolios_Wout_Div", None, None),
    ("6_Portfolios_2x3", "6_Portfolios_2x3_daily", "6_Portfolios_2x3_weekly"),
    ("6_Portfolios_2x3_Wout_Div", None, None),
    ("6_Portfolios_ME_CFP_2x3", None, None),
    ("6_Portfolios_ME_CFP_2x3_Wout_Div", None, None),
    ("6_Portfolios_ME_DP_2x3", None, None),
    ("6_Portfolios_ME_DP_2x3_Wout_Div", None, None),
    ("6_Portfolios_ME_EP_2x3", None, None),
    ("6_Portfolios_ME_EP_2x3_Wout_Div", None, None),
    ("6_Portfolios_ME_INV_2x3", "6_Portfolios_ME_INV_2x3_daily", None),
    ("6_Portfolios_ME_INV_2x3_Wout_Div", None, None),
    ("6_Portfolios_ME_OP_2x3", "6_Portfolios_ME_OP_2x3_daily", None),
    ("6_Portfolios_ME_OP_2x3_Wout_Div", None, None),
    ("6_Portfolios_ME_Prior_12_2", "6_Portfolios_ME_Prior_12_2_Daily", None),
    ("6_Portfolios_ME_Prior_1_0", "6_Portfolios_ME_Prior_1_0_Daily", None),
    ("6_Portfolios_ME_Prior_60_13", "6_Portfolios_ME_Prior_60_13_Daily", None),
    ("Portfolios_Formed_on_AC", None, None),
    ("Portfolios_Formed_on_BE-ME", "Portfolios_Formed_on_BE-ME_Daily", None),
    ("Portfolios_Formed_on_BE-ME_Wout_Div", None, None),
    ("Portfolios_Formed_on_BETA", None, None),
    ("Portfolios_Formed_on_CF-P", None, None),
    ("Portfolios_Formed_on_CF-P_Wout_Div", None, None),
    ("Portfolios_Formed_on_D-P", None, None),
    ("Portfolios_Formed_on_D-P_Wout_Div", None, None),
    ("Portfolios_Formed_on_E-P", None, None),
    ("Portfolios_Formed_on_E-P_Wout_Div", None, None),
    ("Portfolios_Formed_on_INV", "Portfolios_Formed_on_INV_Daily", None),
    ("Portfolios_Formed_on_INV_Wout_Div", None, None),
    ("Portfolios_Formed_on_ME", "Portfolios_Formed_on_ME_Daily", None),
    ("Portfolios_Formed_on_ME_Wout_Div", None, None),
    ("Portfolios_Formed_on_NI", None, None),
    ("Portfolios_Formed_on_OP", "Portfolios_Formed_on_OP_Daily", None),
    ("Portfolios_Formed_on_OP_Wout_Div", None, None),
    ("Portfolios_Formed_on_RESVAR", None, None),
    ("Portfolios_Formed_on_VAR", None, None),
)
for _extra_stem, _extra_daily, _extra_weekly in _US_EXTRA_PORTFOLIO_LAYOUTS:
    _register_us_archive(_extra_stem, daily_stem=_extra_daily, weekly_stem=_extra_weekly)

REGIONAL_PORTFOLIO_DATASETS: dict[str, TableDataset] = {}
_REGIONAL_PORTFOLIO_PREFIXES = {
    "developed": "Developed",
    "developed_ex_us": "Developed_ex_US",
    "europe": "Europe",
    "japan": "Japan",
    "asia_pacific_ex_japan": "Asia_Pacific_ex_Japan",
    "north_america": "North_America",
}
for _region, _prefix in _REGIONAL_PORTFOLIO_PREFIXES.items():
    for _count, _dimension in (("6", "2x3"), ("25", "5x5")):
        for _sort, _official in (("beme", "ME_BE-ME"), ("op", "ME_OP"), ("investment", "ME_INV")):
            _stem = f"{_prefix}_{_count}_Portfolios_{_official}"
            _daily = f"{_stem}_daily_CSV.zip" if _sort == "beme" else f"{_stem}_Daily_CSV.zip"
            REGIONAL_PORTFOLIO_DATASETS[f"{_region}_{_count}_{_sort}"] = TableDataset(
                _table_files(f"{_stem}_CSV.zip", daily=_daily), _region
            )
        _prior = f"{_prefix}_{_count}_Portfolios_ME_Prior_12_2"
        REGIONAL_PORTFOLIO_DATASETS[f"{_region}_{_count}_momentum"] = TableDataset(
            _table_files(f"{_prior}_CSV.zip"), _region
        )
    for _sort, _official in (
        ("beme_op", "ME_BE-ME_OP"),
        ("beme_investment", "ME_BE-ME_INV(TA)"),
        ("op_investment", "ME_INV(TA)_OP"),
    ):
        _stem = f"{_prefix}_32_Portfolios_{_official}_2x4x4"
        REGIONAL_PORTFOLIO_DATASETS[f"{_region}_32_{_sort}"] = TableDataset(
            _table_files(f"{_stem}_CSV.zip"), _region
        )

for _count, _official_sort in (
    ("6", "ME_BE-ME"),
    ("6", "ME_OP"),
    ("6", "ME_INV"),
    ("6", "ME_Prior_12_2"),
):
    _stem = f"Emerging_Markets_{_count}_Portfolios_{_official_sort}"
    REGIONAL_PORTFOLIO_DATASETS[
        f"emerging_{_count}_{_official_sort.removeprefix('ME_').casefold().replace('-', '_')}"
    ] = TableDataset(_table_files(f"{_stem}_CSV.zip"), "emerging")

for _official_sort, _key in (
    ("BE-ME_OP", "beme_op"),
    ("BE-ME_INV", "beme_investment"),
    ("OP_INV", "op_investment"),
):
    _stem = f"Emerging_Markets_4_Portfolios_{_official_sort}"
    REGIONAL_PORTFOLIO_DATASETS[f"emerging_4_{_key}"] = TableDataset(
        _table_files(f"{_stem}_CSV.zip"), "emerging"
    )

for _region, _prefix in _REGIONAL_PORTFOLIO_PREFIXES.items():
    for _count in ("6", "25"):
        _stem = f"{_prefix}_{_count}_Portfolios_ME_Prior_250_20_daily"
        REGIONAL_PORTFOLIO_DATASETS[f"{_region}_{_count}_prior_250_20_daily"] = TableDataset(
            MappingProxyType({"daily": f"{_stem}_CSV.zip"}), _region
        )
