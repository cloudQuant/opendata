"""Typed contracts for CFTC Commitments of Traders data."""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import StrictFloat, StrictInt, StrictStr, field_validator

from opendata.data.models.base import ContractModel

if TYPE_CHECKING:
    from collections.abc import Hashable, Mapping
    from typing import Any


class COT(ContractModel):
    """One selected CFTC report row with report identity preserved.

    Counts are contracts or traders; percentages are percentage points. The
    report selectors are repeated on every row so identical market/date pairs
    from separate report datasets remain distinguishable.
    """

    date: date_type
    report_type: Literal["legacy", "disaggregated", "financial", "supplemental"]
    futonly_or_combined: Literal["futures_only", "combined"] | None = None
    report_week: StrictStr | None = None
    market_and_exchange_names: StrictStr | None = None
    cftc_contract_market_code: StrictStr | None = None
    cftc_market_code: StrictStr | None = None
    cftc_region_code: StrictStr | None = None
    cftc_commodity_code: StrictStr | None = None
    cftc_contract_market_code_quotes: StrictStr | None = None
    cftc_market_code_quotes: StrictStr | None = None
    cftc_commodity_code_quotes: StrictStr | None = None
    cftc_subgroup_code: StrictStr | None = None
    commodity: StrictStr | None = None
    commodity_group: StrictStr | None = None
    commodity_subgroup: StrictStr | None = None
    contract_market_name: StrictStr | None = None
    contract_units: StrictStr | None = None
    open_interest_all: StrictInt | None = None
    open_interest_old: StrictInt | None = None
    open_interest_other: StrictInt | None = None

    # Main long/short/spread positions. Source columns remain contracts; no scaling is applied.
    non_commercial_positions_long_all: StrictInt | None = None
    non_commercial_positions_short_all: StrictInt | None = None
    non_commercial_positions_spread_all: StrictInt | None = None
    non_commercial_positions_long_old: StrictInt | None = None
    non_commercial_positions_short_old: StrictInt | None = None
    non_commercial_positions_spread: StrictInt | None = None
    non_commercial_positions_long_other: StrictInt | None = None
    non_commercial_positions_short_other: StrictInt | None = None
    non_commercial_positions_spread_1: StrictInt | None = None
    commercial_positions_long_all: StrictInt | None = None
    commercial_positions_short_all: StrictInt | None = None
    commercial_positions_long_old: StrictInt | None = None
    commercial_positions_short_old: StrictInt | None = None
    commercial_positions_long_other: StrictInt | None = None
    commercial_positions_short_other: StrictInt | None = None
    producer_merchant_positions_long: StrictInt | None = None
    producer_merchant_positions_short: StrictInt | None = None
    producer_merchant_positions_long_1: StrictInt | None = None
    producer_merchant_positions_short_1: StrictInt | None = None
    producer_merchant_positions_long_2: StrictInt | None = None
    producer_merchant_positions_short_2: StrictInt | None = None
    swap_positions_long_all: StrictInt | None = None
    swap_positions_short_all: StrictInt | None = None
    swap_positions_spread_all: StrictInt | None = None
    swap_positions_long_old: StrictInt | None = None
    swap_positions_short_old: StrictInt | None = None
    swap_positions_spread_old: StrictInt | None = None
    swap_positions_long_other: StrictInt | None = None
    swap_positions_short_other: StrictInt | None = None
    swap_positions_spread_other: StrictInt | None = None
    managed_money_positions_long_all: StrictInt | None = None
    managed_money_positions_short_all: StrictInt | None = None
    managed_money_positions_spread: StrictInt | None = None
    managed_money_positions_long_old: StrictInt | None = None
    managed_money_positions_short_old: StrictInt | None = None
    managed_money_positions_spread_1: StrictInt | None = None
    managed_money_positions_long_other: StrictInt | None = None
    managed_money_positions_short_other: StrictInt | None = None
    managed_money_positions_spread_2: StrictInt | None = None
    dealer_positions_long_all: StrictInt | None = None
    dealer_positions_short_all: StrictInt | None = None
    dealer_positions_spread_all: StrictInt | None = None
    asset_manager_positions_long: StrictInt | None = None
    asset_manager_positions_short: StrictInt | None = None
    asset_manager_positions_spread: StrictInt | None = None
    leveraged_funds_positions_long: StrictInt | None = None
    leveraged_funds_positions_short: StrictInt | None = None
    leveraged_funds_positions_spread: StrictInt | None = None
    other_reportable_positions_long: StrictInt | None = None
    other_reportable_positions_short: StrictInt | None = None
    other_reportable_positions_spread: StrictInt | None = None
    other_reportable_positions_long_1: StrictInt | None = None
    other_reportable_positions_short_1: StrictInt | None = None
    other_reportable_positions_spread_1: StrictInt | None = None
    other_reportable_positions_long_2: StrictInt | None = None
    other_reportable_positions_short_2: StrictInt | None = None
    other_reportable_positions_spread_2: StrictInt | None = None
    non_commercial_positions_long_all_non_cit: StrictInt | None = None
    non_commercial_positions_short_all_non_cit: StrictInt | None = None
    non_commercial_positions_spread_all_non_cit: StrictInt | None = None
    commercial_positions_long_all_non_cit: StrictInt | None = None
    commercial_positions_short_all_non_cit: StrictInt | None = None
    cit_positions_long_all: StrictInt | None = None
    cit_positions_short_all: StrictInt | None = None
    total_reportable_positions_long_all: StrictInt | None = None
    total_reportable_positions_short: StrictInt | None = None
    total_reportable_positions_long_old: StrictInt | None = None
    total_reportable_positions_short_1: StrictInt | None = None
    total_reportable_positions_long_other: StrictInt | None = None
    total_reportable_positions_short_2: StrictInt | None = None
    non_reportable_positions_long_all: StrictInt | None = None
    non_reportable_positions_short_all: StrictInt | None = None
    non_reportable_positions_long_old: StrictInt | None = None
    non_reportable_positions_short_old: StrictInt | None = None
    non_reportable_positions_long_other: StrictInt | None = None
    non_reportable_positions_short_other: StrictInt | None = None

    # The CFTC publishes weekly changes, percentage of open interest, trader counts and
    # concentration measures as separate source columns. These are preserved without conversion.
    change_in_open_interest_all: StrictInt | None = None
    change_open_interest_all: StrictInt | None = None
    change_in_non_commercial_long_all: StrictInt | None = None
    change_in_non_commercial_short_all: StrictInt | None = None
    change_in_non_commercial_spread_all: StrictInt | None = None
    change_in_commercial_long_all: StrictInt | None = None
    change_in_commercial_short_all: StrictInt | None = None
    change_in_total_reportable_long_all: StrictInt | None = None
    change_in_total_reportable_short: StrictInt | None = None
    change_in_non_reportable_long_all: StrictInt | None = None
    change_in_non_reportable_short_all: StrictInt | None = None
    change_in_producer_merchant_long: StrictInt | None = None
    change_in_producer_merchant_short: StrictInt | None = None
    change_in_swap_long_all: StrictInt | None = None
    change_in_swap_short_all: StrictInt | None = None
    change_in_swap_spread_all: StrictInt | None = None
    change_in_managed_money_long_all: StrictInt | None = None
    change_in_managed_money_short_all: StrictInt | None = None
    change_in_managed_money_spread: StrictInt | None = None
    change_in_dealer_long_all: StrictInt | None = None
    change_in_dealer_short_all: StrictInt | None = None
    change_in_dealer_spread_all: StrictInt | None = None
    change_in_asset_manager_long: StrictInt | None = None
    change_in_asset_manager_short: StrictInt | None = None
    change_in_asset_manager_spread: StrictInt | None = None
    change_in_leveraged_funds_long: StrictInt | None = None
    change_in_leveraged_funds_short: StrictInt | None = None
    change_in_leveraged_funds_spread: StrictInt | None = None
    change_in_other_reportable_long: StrictInt | None = None
    change_in_other_reportable_short: StrictInt | None = None
    change_in_other_reportable_spread: StrictInt | None = None
    change_non_commercial_long_all_non_cit: StrictInt | None = None
    change_non_commercial_short_all_non_cit: StrictInt | None = None
    change_non_commercial_spread_all_non_cit: StrictInt | None = None
    change_commercial_long_all_non_cit: StrictInt | None = None
    change_commercial_short_all_non_cit: StrictInt | None = None
    change_cit_long_all: StrictInt | None = None
    change_cit_short_all: StrictInt | None = None
    change_total_reportable_long_all: StrictInt | None = None
    change_total_reportable_short_all: StrictInt | None = None
    change_non_reportable_long_all: StrictInt | None = None
    change_non_reportable_short_all: StrictInt | None = None
    open_interest_pct_all: StrictFloat | None = None
    open_interest_pct_old: StrictFloat | None = None
    open_interest_pct_other: StrictFloat | None = None
    open_interest_pct_non_commercial_long_all: StrictFloat | None = None
    open_interest_pct_non_commercial_short_all: StrictFloat | None = None
    open_interest_pct_non_commercial_spread: StrictFloat | None = None
    open_interest_pct_non_commercial_long_old: StrictFloat | None = None
    open_interest_pct_non_commercial_short_old: StrictFloat | None = None
    open_interest_pct_non_commercial_spread_1: StrictFloat | None = None
    open_interest_pct_non_commercial_long_other: StrictFloat | None = None
    open_interest_pct_non_commercial_short_other: StrictFloat | None = None
    open_interest_pct_non_commercial_spread_2: StrictFloat | None = None
    open_interest_pct_commercial_long_all: StrictFloat | None = None
    open_interest_pct_commercial_short_all: StrictFloat | None = None
    open_interest_pct_commercial_long_old: StrictFloat | None = None
    open_interest_pct_commercial_short_old: StrictFloat | None = None
    open_interest_pct_commercial_long_other: StrictFloat | None = None
    open_interest_pct_commercial_short_other: StrictFloat | None = None
    open_interest_pct_producer_merchant_long: StrictFloat | None = None
    open_interest_pct_producer_merchant_short: StrictFloat | None = None
    open_interest_pct_producer_merchant_long_1: StrictFloat | None = None
    open_interest_pct_producer_merchant_short_1: StrictFloat | None = None
    open_interest_pct_producer_merchant_long_2: StrictFloat | None = None
    open_interest_pct_producer_merchant_short_2: StrictFloat | None = None
    open_interest_pct_swap_long_all: StrictFloat | None = None
    open_interest_pct_swap_short_all: StrictFloat | None = None
    open_interest_pct_swap_spread_all: StrictFloat | None = None
    open_interest_pct_swap_long_old: StrictFloat | None = None
    open_interest_pct_swap_short_old: StrictFloat | None = None
    open_interest_pct_swap_spread_old: StrictFloat | None = None
    open_interest_pct_swap_long_other: StrictFloat | None = None
    open_interest_pct_swap_short_other: StrictFloat | None = None
    open_interest_pct_swap_spread_other: StrictFloat | None = None
    open_interest_pct_managed_money_long_all: StrictFloat | None = None
    open_interest_pct_managed_money_short_all: StrictFloat | None = None
    open_interest_pct_managed_money_spread: StrictFloat | None = None
    open_interest_pct_managed_money_long_old: StrictFloat | None = None
    open_interest_pct_managed_money_short_old: StrictFloat | None = None
    open_interest_pct_managed_money_spread_1: StrictFloat | None = None
    open_interest_pct_managed_money_long_other: StrictFloat | None = None
    open_interest_pct_managed_money_short_other: StrictFloat | None = None
    open_interest_pct_managed_money_spread_2: StrictFloat | None = None
    open_interest_pct_dealer_long_all: StrictFloat | None = None
    open_interest_pct_dealer_short_all: StrictFloat | None = None
    open_interest_pct_dealer_spread_all: StrictFloat | None = None
    open_interest_pct_asset_manager_long: StrictFloat | None = None
    open_interest_pct_asset_manager_short: StrictFloat | None = None
    open_interest_pct_asset_manager_spread: StrictFloat | None = None
    open_interest_pct_leveraged_funds_long: StrictFloat | None = None
    open_interest_pct_leveraged_funds_short: StrictFloat | None = None
    open_interest_pct_leveraged_funds_spread: StrictFloat | None = None
    open_interest_pct_other_reportable_long: StrictFloat | None = None
    open_interest_pct_other_reportable_short: StrictFloat | None = None
    open_interest_pct_other_reportable_spread: StrictFloat | None = None
    open_interest_pct_other_reportable_long_1: StrictFloat | None = None
    open_interest_pct_other_reportable_short_1: StrictFloat | None = None
    open_interest_pct_other_reportable_spread_1: StrictFloat | None = None
    open_interest_pct_other_reportable_long_2: StrictFloat | None = None
    open_interest_pct_other_reportable_short_2: StrictFloat | None = None
    open_interest_pct_other_reportable_spread_2: StrictFloat | None = None
    open_interest_pct_non_commercial_long_all_non_cit: StrictFloat | None = None
    open_interest_pct_non_commercial_short_all_non_cit: StrictFloat | None = None
    open_interest_pct_non_commercial_spread_all_non_cit: StrictFloat | None = None
    open_interest_pct_commercial_long_all_non_cit: StrictFloat | None = None
    open_interest_pct_commercial_short_all_non_cit: StrictFloat | None = None
    open_interest_pct_cit_long_all: StrictFloat | None = None
    open_interest_pct_cit_short_all: StrictFloat | None = None
    open_interest_pct_total_reportable_long_all_non_cit: StrictFloat | None = None
    open_interest_pct_total_reportable_short_all_non_cit: StrictFloat | None = None
    open_interest_pct_non_reportable_long_all_non_cit: StrictFloat | None = None
    open_interest_pct_non_reportable_short_all_non_cit: StrictFloat | None = None
    open_interest_pct_total_reportable_long_all: StrictFloat | None = None
    open_interest_pct_total_reportable_short: StrictFloat | None = None
    open_interest_pct_total_reportable_long_old: StrictFloat | None = None
    open_interest_pct_total_reportable_short_1: StrictFloat | None = None
    open_interest_pct_total_reportable_long_other: StrictFloat | None = None
    open_interest_pct_total_reportable_short_2: StrictFloat | None = None
    open_interest_pct_non_reportable_long_all: StrictFloat | None = None
    open_interest_pct_non_reportable_short_all: StrictFloat | None = None
    open_interest_pct_non_reportable_long_old: StrictFloat | None = None
    open_interest_pct_non_reportable_short_old: StrictFloat | None = None
    open_interest_pct_non_reportable_long_other: StrictFloat | None = None
    open_interest_pct_non_reportable_short_other: StrictFloat | None = None
    traders_total_all: StrictInt | None = None
    traders_total_old: StrictInt | None = None
    traders_total_other: StrictInt | None = None
    traders_non_commercial_long_all: StrictInt | None = None
    traders_non_commercial_short_all: StrictInt | None = None
    traders_non_commercial_spread_all: StrictInt | None = None
    traders_non_commercial_long_old: StrictInt | None = None
    traders_non_commercial_short_old: StrictInt | None = None
    traders_non_commercial_spread_old: StrictInt | None = None
    traders_non_commercial_long_other: StrictInt | None = None
    traders_non_commercial_short_other: StrictInt | None = None
    traders_non_commercial_spread_other: StrictInt | None = None
    traders_commercial_long_all: StrictInt | None = None
    traders_commercial_short_all: StrictInt | None = None
    traders_commercial_long_old: StrictInt | None = None
    traders_commercial_short_old: StrictInt | None = None
    traders_commercial_long_other: StrictInt | None = None
    traders_commercial_short_other: StrictInt | None = None
    traders_producer_merchant_long_all: StrictInt | None = None
    traders_producer_merchant_short_all: StrictInt | None = None
    traders_producer_merchant_long_old: StrictInt | None = None
    traders_producer_merchant_short_old: StrictInt | None = None
    traders_producer_merchant_long_other: StrictInt | None = None
    traders_producer_merchant_short_other: StrictInt | None = None
    traders_swap_long_all: StrictInt | None = None
    traders_swap_short_all: StrictInt | None = None
    traders_swap_spread_all: StrictInt | None = None
    traders_swap_long_old: StrictInt | None = None
    traders_swap_short_old: StrictInt | None = None
    traders_swap_spread_old: StrictInt | None = None
    traders_swap_long_other: StrictInt | None = None
    traders_swap_short_other: StrictInt | None = None
    traders_swap_spread_other: StrictInt | None = None
    traders_managed_money_long_all: StrictInt | None = None
    traders_managed_money_short_all: StrictInt | None = None
    traders_managed_money_spread_all: StrictInt | None = None
    traders_managed_money_long_old: StrictInt | None = None
    traders_managed_money_short_old: StrictInt | None = None
    traders_managed_money_spread_old: StrictInt | None = None
    traders_managed_money_long_other: StrictInt | None = None
    traders_managed_money_short_other: StrictInt | None = None
    traders_managed_money_spread_other: StrictInt | None = None
    traders_dealer_long_all: StrictInt | None = None
    traders_dealer_short_all: StrictInt | None = None
    traders_dealer_spread_all: StrictInt | None = None
    traders_asset_manager_long_all: StrictInt | None = None
    traders_asset_manager_short_all: StrictInt | None = None
    traders_asset_manager_spread: StrictInt | None = None
    traders_leveraged_funds_long_all: StrictInt | None = None
    traders_leveraged_funds_short_all: StrictInt | None = None
    traders_leveraged_funds_spread: StrictInt | None = None
    traders_other_reportable_long_all: StrictInt | None = None
    traders_other_reportable_short: StrictInt | None = None
    traders_other_reportable_spread: StrictInt | None = None
    traders_other_reportable_long_old: StrictInt | None = None
    traders_other_reportable_short_1: StrictInt | None = None
    traders_other_reportable_spread_1: StrictInt | None = None
    traders_other_reportable_long_other: StrictInt | None = None
    traders_other_reportable_short_2: StrictInt | None = None
    traders_other_reportable_spread_2: StrictInt | None = None
    traders_non_commercial_long_all_non_cit: StrictInt | None = None
    traders_non_commercial_short_all_non_cit: StrictInt | None = None
    traders_non_commercial_spread_all_non_cit: StrictInt | None = None
    traders_commercial_long_all_non_cit: StrictInt | None = None
    traders_commercial_short_all_non_cit: StrictInt | None = None
    traders_cit_long_all: StrictInt | None = None
    traders_cit_short_all: StrictInt | None = None
    traders_total_reportable_long_all_non_cit: StrictInt | None = None
    traders_total_reportable_short_all_non_cit: StrictInt | None = None
    traders_total_reportable_long_all: StrictInt | None = None
    traders_total_reportable_short_all: StrictInt | None = None
    traders_total_reportable_long_old: StrictInt | None = None
    traders_total_reportable_short_old: StrictInt | None = None
    traders_total_reportable_long_other: StrictInt | None = None
    traders_total_reportable_short_other: StrictInt | None = None
    concentration_gross_top_4_traders_long: StrictFloat | None = None
    concentration_gross_top_4_traders_short: StrictFloat | None = None
    concentration_gross_top_8_traders_long: StrictFloat | None = None
    concentration_gross_top_8_traders_short: StrictFloat | None = None
    concentration_net_top_4_traders_long_all: StrictFloat | None = None
    concentration_net_top_4_traders_short_all: StrictFloat | None = None
    concentration_net_top_8_traders_long_all: StrictFloat | None = None
    concentration_net_top_8_traders_short_all: StrictFloat | None = None
    concentration_gross_top_4_traders_long_1: StrictFloat | None = None
    concentration_gross_top_4_traders_short_1: StrictFloat | None = None
    concentration_gross_top_8_traders_long_1: StrictFloat | None = None
    concentration_gross_top_8_traders_short_1: StrictFloat | None = None
    concentration_net_top_4_traders_long_old: StrictFloat | None = None
    concentration_net_top_4_traders_short_old: StrictFloat | None = None
    concentration_net_top_8_traders_long_old: StrictFloat | None = None
    concentration_net_top_8_traders_short_old: StrictFloat | None = None
    concentration_gross_top_4_traders_long_2: StrictFloat | None = None
    concentration_gross_top_4_traders_short_2: StrictFloat | None = None
    concentration_gross_top_8_traders_long_2: StrictFloat | None = None
    concentration_gross_top_8_traders_short_2: StrictFloat | None = None
    concentration_net_top_4_traders_long_other: StrictFloat | None = None
    concentration_net_top_4_traders_short_other: StrictFloat | None = None
    concentration_net_top_8_traders_long_other: StrictFloat | None = None
    concentration_net_top_8_traders_short_other: StrictFloat | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _strict_report_date(cls, value: object) -> date_type:
        if type(value) is date_type:
            return value
        if type(value) is str and len(value) == 10:
            try:
                return date_type.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("date must be an ISO calendar date") from exc
        raise ValueError("date must be a date or YYYY-MM-DD string without a time")

    @classmethod
    def _coerce_record(cls, row: Mapping[Hashable, Any]) -> dict[str, Any]:
        if isinstance(row.get("date"), datetime):
            raise ValueError("date must not include a time")
        return super()._coerce_record(row)


class COTSearch(ContractModel):
    """One current market-directory row from a CFTC COT report dataset."""

    code: StrictStr
    name: StrictStr
    category: StrictStr | None = None
    subcategory: StrictStr | None = None
    units: StrictStr | None = None
    symbol: StrictStr | None = None
    commodity: StrictStr | None = None
