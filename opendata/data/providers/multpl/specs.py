"""Output contract and verified public pages for the Multpl S&P 500 series."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# TC001 is intentionally suppressed: reflection resolves MetricPage's postponed
# annotations through this module's runtime globals.
from opendata.data.models.multpl import (  # noqa: TC001
    SP500Frequency,
    SP500Metric,
    SP500ValueUnit,
)

MULTPL_BASE_URL: Final = "https://www.multpl.com"


@dataclass(frozen=True)
class MetricPage:
    """One official HTML table, including its published cadence and value unit."""

    path: str
    frequency: SP500Frequency
    value_unit: SP500ValueUnit


METRIC_PAGES: Final[dict[SP500Metric, MetricPage]] = {
    "pe_ratio": MetricPage(
        "/s-p-500-pe-ratio/table/by-month",
        "monthly",
        "ratio",
    ),
    "shiller_pe": MetricPage(
        "/shiller-pe/table/by-month",
        "monthly",
        "ratio",
    ),
    "price_to_book": MetricPage(
        "/s-p-500-price-to-book/table/by-year",
        "annual",
        "ratio",
    ),
    "earnings_yield": MetricPage(
        "/s-p-500-earnings-yield/table/by-month",
        "monthly",
        "percent",
    ),
    "price_to_sales": MetricPage(
        "/s-p-500-price-to-sales/table/by-year",
        "annual",
        "ratio",
    ),
}

DECLARED_MODELS: Final[tuple[str, ...]] = ("SP500Multiples",)
NOT_DECLARED: Final[dict[str, str]] = {}
