"""CFTC Commitments of Traders report fetcher."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Literal, get_args

from pydantic import Field, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.cftc import COT
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.cftc.models import _client

REPORT_DATASETS: dict[tuple[str, bool], str] = {
    ("legacy", False): "jun7-fc8e",
    ("legacy", True): "6dca-aqww",
    ("disaggregated", False): "gr4m-cvuh",
    ("disaggregated", True): "72hh-3qpy",
    ("financial", False): "yw9f-hn96",
    ("financial", True): "gpe5-46if",
    ("supplemental", False): "4zgm-a668",
}
PAGE_SIZE = 1_000
MAX_ROWS = 50_000
MAX_LIMIT = 50_000
_CODE = re.compile(r"(?:CFTC_)?[A-Z0-9]{6}\Z")
_SOURCE_CODE = re.compile(r"[A-Z0-9]{6}\Z")

_FIELD_SOURCE = {
    "date": "report_date_as_yyyy_mm_dd",
    "report_week": "yyyy_report_week_ww",
    "commodity": "commodity_name",
    "commodity_group": "commodity_group_name",
    "commodity_subgroup": "commodity_subgroup_name",
    "non_commercial_positions_long_all": "noncomm_positions_long_all",
    "non_commercial_positions_short_all": "noncomm_positions_short_all",
    "non_commercial_positions_spread_all": "noncomm_postions_spread_all",
    "non_commercial_positions_long_old": "noncomm_positions_long_old",
    "non_commercial_positions_short_old": "noncomm_positions_short_old",
    "non_commercial_positions_spread": "noncomm_postions_spread_old",
    "non_commercial_positions_long_other": "noncomm_positions_long_other",
    "non_commercial_positions_short_other": "noncomm_positions_short_other",
    "non_commercial_positions_spread_1": "noncomm_postions_spread_other",
    "commercial_positions_long_all": "comm_positions_long_all",
    "commercial_positions_short_all": "comm_positions_short_all",
    "commercial_positions_long_old": "comm_positions_long_old",
    "commercial_positions_short_old": "comm_positions_short_old",
    "commercial_positions_long_other": "comm_positions_long_other",
    "commercial_positions_short_other": "comm_positions_short_other",
    "total_reportable_positions_long_all": "tot_rept_positions_long_all",
    "total_reportable_positions_short": "tot_rept_positions_short_all",
    "total_reportable_positions_long_old": "tot_rept_positions_long_old",
    "total_reportable_positions_short_1": "tot_rept_positions_short_old",
    "total_reportable_positions_long_other": "tot_rept_positions_long_other",
    "total_reportable_positions_short_2": "tot_rept_positions_short_other",
    "non_reportable_positions_long_all": "nonrept_positions_long_all",
    "non_reportable_positions_short_all": "nonrept_positions_short_all",
    "non_reportable_positions_long_old": "nonrept_positions_long_old",
    "non_reportable_positions_short_old": "nonrept_positions_short_old",
    "non_reportable_positions_long_other": "nonrept_positions_long_other",
    "non_reportable_positions_short_other": "nonrept_positions_short_other",
    "producer_merchant_positions_long": "prod_merc_positions_long",
    "producer_merchant_positions_short": "prod_merc_positions_short",
    "producer_merchant_positions_long_1": "prod_merc_positions_long_old",
    "producer_merchant_positions_short_1": "prod_merc_positions_short_old",
    "producer_merchant_positions_long_2": "prod_merc_positions_long_other",
    "producer_merchant_positions_short_2": "prod_merc_positions_short_other",
    "managed_money_positions_long_all": "m_money_positions_long_all",
    "managed_money_positions_short_all": "m_money_positions_short_all",
    "managed_money_positions_spread": "m_money_positions_spread_all",
    "managed_money_positions_long_old": "m_money_positions_long_old",
    "managed_money_positions_short_old": "m_money_positions_short_old",
    "managed_money_positions_spread_1": "m_money_positions_spread_old",
    "managed_money_positions_long_other": "m_money_positions_long_other",
    "managed_money_positions_short_other": "m_money_positions_short_other",
    "managed_money_positions_spread_2": "m_money_positions_spread_other",
    "asset_manager_positions_long": "asset_mgr_positions_long",
    "asset_manager_positions_short": "asset_mgr_positions_short",
    "asset_manager_positions_spread": "asset_mgr_positions_spread",
    "leveraged_funds_positions_long": "lev_money_positions_long",
    "leveraged_funds_positions_short": "lev_money_positions_short",
    "leveraged_funds_positions_spread": "lev_money_positions_spread",
    "other_reportable_positions_long": "other_rept_positions_long_all",
    "other_reportable_positions_short": "other_rept_positions_short_all",
    "other_reportable_positions_spread": "other_rept_positions_spread_all",
    "other_reportable_positions_long_1": "other_rept_positions_long_old",
    "other_reportable_positions_short_1": "other_rept_positions_short_old",
    "other_reportable_positions_spread_1": "other_rept_positions_spread_old",
    "other_reportable_positions_long_2": "other_rept_positions_long_other",
    "other_reportable_positions_short_2": "other_rept_positions_short_other",
    "other_reportable_positions_spread_2": "other_rept_positions_spread_other",
    "non_commercial_positions_long_all_non_cit": "ncomm_positions_long_all_nocit",
    "non_commercial_positions_short_all_non_cit": "ncomm_positions_short_all_nocit",
    "non_commercial_positions_spread_all_non_cit": "ncomm_postions_spread_all_nocit",
    "commercial_positions_long_all_non_cit": "comm_positions_long_all_nocit",
    "commercial_positions_short_all_non_cit": "comm_positions_short_all_nocit",
    "change_in_non_commercial_long_all": "change_in_noncomm_long_all",
    "change_in_non_commercial_short_all": "change_in_noncomm_short_all",
    "change_in_commercial_long_all": "change_in_comm_long_all",
    "change_in_commercial_short_all": "change_in_comm_short_all",
    "change_in_open_interest_all": "change_in_oi_all",
    "change_in_non_commercial_spread_all": "change_in_noncomm_spread_all",
    "change_in_total_reportable_long_all": "change_in_tot_rept_long_all",
    "change_in_total_reportable_short": "change_in_tot_rept_short_all",
    "change_in_non_reportable_long_all": "change_in_nonrept_long_all",
    "change_in_non_reportable_short_all": "change_in_nonrept_short_all",
    "change_in_producer_merchant_long": "change_in_prod_merc_long",
    "change_in_producer_merchant_short": "change_in_prod_merc_short",
    "change_in_swap_long_all": "change_in_swap_long_all",
    "change_in_swap_short_all": "change_in_swap_short_all",
    "change_in_swap_spread_all": "change_in_swap_spread_all",
    "change_in_managed_money_long_all": "change_in_m_money_long_all",
    "change_in_managed_money_short_all": "change_in_m_money_short_all",
    "change_in_asset_manager_long": "change_in_asset_mgr_long",
    "change_in_asset_manager_short": "change_in_asset_mgr_short",
    "change_in_asset_manager_spread": "change_in_asset_mgr_spread",
    "change_in_leveraged_funds_long": "change_in_lev_money_long",
    "change_in_leveraged_funds_short": "change_in_lev_money_short",
    "change_in_leveraged_funds_spread": "change_in_lev_money_spread",
    "change_in_dealer_spread_all": "change_in_dealer_spread_all",
    "change_in_other_reportable_long": "change_in_other_rept_long",
    "change_in_other_reportable_short": "change_in_other_rept_short",
    "change_in_other_reportable_spread": "change_in_other_rept_spread",
    "change_open_interest_all": "change_open_interest_all",
    "change_non_commercial_long_all_non_cit": "change_noncomm_long_all_nocit",
    "change_non_commercial_short_all_non_cit": "change_noncomm_short_all_nocit",
    "change_non_commercial_spread_all_non_cit": "change_noncomm_spead_all_nocit",
    "change_commercial_long_all_non_cit": "change_comm_long_all_nocit",
    "change_commercial_short_all_non_cit": "change_comm_short_all_nocit",
    "change_cit_long_all": "change_cit_long_all",
    "change_cit_short_all": "change_cit_short_all",
    "change_total_reportable_long_all": "change_tot_rept_long_all",
    "change_total_reportable_short_all": "change_tot_rept_short_all",
    "change_non_reportable_long_all": "change_nonrept_long_all",
    "change_non_reportable_short_all": "change_nonrept_short_all",
    "open_interest_pct_all": "pct_of_oi",
    "open_interest_pct_old": "pct_of_oi_old",
    "open_interest_pct_other": "pct_of_oi_other",
    "open_interest_pct_non_commercial_long_all": "pct_of_oi_noncomm_long_all",
    "open_interest_pct_non_commercial_short_all": "pct_of_oi_noncomm_short_all",
    "open_interest_pct_non_commercial_spread": "pct_of_oi_noncomm_spread_all",
    "open_interest_pct_non_commercial_long_old": "pct_of_oi_noncomm_long_old",
    "open_interest_pct_non_commercial_short_old": "pct_of_oi_noncomm_short_old",
    "open_interest_pct_non_commercial_spread_1": "pct_of_oi_noncomm_spread_old",
    "open_interest_pct_non_commercial_long_other": "pct_of_oi_noncomm_long_other",
    "open_interest_pct_non_commercial_short_other": "pct_of_oi_noncomm_short_other",
    "open_interest_pct_non_commercial_spread_2": "pct_of_oi_noncomm_spread_other",
    "open_interest_pct_commercial_long_all": "pct_of_oi_comm_long_all",
    "open_interest_pct_commercial_short_all": "pct_of_oi_comm_short_all",
    "open_interest_pct_commercial_long_old": "pct_of_oi_comm_long_old",
    "open_interest_pct_commercial_short_old": "pct_of_oi_comm_short_old",
    "open_interest_pct_commercial_long_other": "pct_of_oi_comm_long_other",
    "open_interest_pct_commercial_short_other": "pct_of_oi_comm_short_other",
    "open_interest_pct_producer_merchant_long": "pct_of_oi_prod_merc_long",
    "open_interest_pct_producer_merchant_short": "pct_of_oi_prod_merc_short",
    "open_interest_pct_producer_merchant_long_1": "pct_of_oi_prod_merc_long_old",
    "open_interest_pct_producer_merchant_short_1": "pct_of_oi_prod_merc_short_old",
    "open_interest_pct_producer_merchant_long_2": "pct_of_oi_prod_merc_long_other",
    "open_interest_pct_producer_merchant_short_2": "pct_of_oi_prod_merc_short_other",
    "open_interest_pct_swap_long_all": "pct_of_oi_swap_long_all",
    "open_interest_pct_swap_short_all": "pct_of_oi_swap_short_all",
    "open_interest_pct_swap_spread_all": "pct_of_oi_swap_spread_all",
    "open_interest_pct_swap_long_old": "pct_of_oi_swap_long_old",
    "open_interest_pct_swap_short_old": "pct_of_oi_swap_short_old",
    "open_interest_pct_swap_spread_old": "pct_of_oi_swap_spread_old",
    "open_interest_pct_swap_long_other": "pct_of_oi_swap_long_other",
    "open_interest_pct_swap_short_other": "pct_of_oi_swap_short_other",
    "open_interest_pct_swap_spread_other": "pct_of_oi_swap_spread_other",
    "open_interest_pct_managed_money_long_all": "pct_of_oi_m_money_long_all",
    "open_interest_pct_managed_money_short_all": "pct_of_oi_m_money_short_all",
    "open_interest_pct_managed_money_spread": "pct_of_oi_m_money_spread_all",
    "open_interest_pct_managed_money_long_old": "pct_of_oi_m_money_long_old",
    "open_interest_pct_managed_money_short_old": "pct_of_oi_m_money_short_old",
    "open_interest_pct_managed_money_spread_1": "pct_of_oi_m_money_spread_old",
    "open_interest_pct_managed_money_long_other": "pct_of_oi_m_money_long_other",
    "open_interest_pct_managed_money_short_other": "pct_of_oi_m_money_short_other",
    "open_interest_pct_managed_money_spread_2": "pct_of_oi_m_money_spread_other",
    "open_interest_pct_dealer_long_all": "pct_of_oi_dealer_long_all",
    "open_interest_pct_dealer_short_all": "pct_of_oi_dealer_short_all",
    "open_interest_pct_dealer_spread_all": "pct_of_oi_dealer_spread_all",
    "open_interest_pct_asset_manager_long": "pct_of_oi_asset_mgr_long_all",
    "open_interest_pct_asset_manager_short": "pct_of_oi_asset_mgr_short_all",
    "open_interest_pct_asset_manager_spread": "pct_of_oi_asset_mgr_spread_all",
    "open_interest_pct_leveraged_funds_long": "pct_of_oi_lev_money_long_all",
    "open_interest_pct_leveraged_funds_short": "pct_of_oi_lev_money_short_all",
    "open_interest_pct_leveraged_funds_spread": "pct_of_oi_lev_money_spread_all",
    "open_interest_pct_other_reportable_long": "pct_of_oi_other_rept_long_all",
    "open_interest_pct_other_reportable_short": "pct_of_oi_other_rept_short_all",
    "open_interest_pct_other_reportable_spread": "pct_of_oi_other_rept_spread_all",
    "open_interest_pct_other_reportable_long_1": "pct_of_oi_other_rept_long_old",
    "open_interest_pct_other_reportable_short_1": "pct_of_oi_other_rept_short_old",
    "open_interest_pct_other_reportable_spread_1": "pct_of_oi_other_rept_spread_old",
    "open_interest_pct_other_reportable_long_2": "pct_of_oi_other_rept_long_other",
    "open_interest_pct_other_reportable_short_2": "pct_of_oi_other_rept_short_other",
    "open_interest_pct_other_reportable_spread_2": "pct_of_oi_other_rept_spread_other",
    "open_interest_pct_non_commercial_long_all_non_cit": "pct_oi_noncomm_long_all_nocit",
    "open_interest_pct_non_commercial_short_all_non_cit": "pct_oi_noncomm_short_all_nocit",
    "open_interest_pct_non_commercial_spread_all_non_cit": "pct_oi_noncomm_spread_all_nocit",
    "open_interest_pct_commercial_long_all_non_cit": "pct_oi_comm_long_all_nocit",
    "open_interest_pct_commercial_short_all_non_cit": "pct_oi_comm_short_all_nocit",
    "open_interest_pct_cit_long_all": "pct_oi_cit_long_all",
    "open_interest_pct_cit_short_all": "pct_oi_cit_short_all",
    "open_interest_pct_total_reportable_long_all_non_cit": "pct_oi_tot_rept_long_all_nocit",
    "open_interest_pct_total_reportable_short_all_non_cit": "pct_oi_tot_rept_short_all_nocit",
    "open_interest_pct_non_reportable_long_all_non_cit": "pct_oi_nonrept_long_all_nocit",
    "open_interest_pct_non_reportable_short_all_non_cit": "pct_oi_nonrept_short_all_nocit",
    "open_interest_pct_total_reportable_long_all": "pct_of_oi_tot_rept_long_all",
    "open_interest_pct_total_reportable_short": "pct_of_oi_tot_rept_short_all",
    "open_interest_pct_total_reportable_long_old": "pct_of_oi_tot_rept_long_old",
    "open_interest_pct_total_reportable_short_1": "pct_of_oi_tot_rept_short_old",
    "open_interest_pct_total_reportable_long_other": "pct_of_oi_tot_rept_long_other",
    "open_interest_pct_total_reportable_short_2": "pct_of_oi_tot_rept_short_other",
    "open_interest_pct_non_reportable_long_all": "pct_of_oi_nonrept_long_all",
    "open_interest_pct_non_reportable_short_all": "pct_of_oi_nonrept_short_all",
    "open_interest_pct_non_reportable_long_old": "pct_of_oi_nonrept_long_old",
    "open_interest_pct_non_reportable_short_old": "pct_of_oi_nonrept_short_old",
    "open_interest_pct_non_reportable_long_other": "pct_of_oi_nonrept_long_other",
    "open_interest_pct_non_reportable_short_other": "pct_of_oi_nonrept_short_other",
    "traders_non_commercial_long_all": "traders_noncomm_long_all",
    "traders_non_commercial_short_all": "traders_noncomm_short_all",
    "traders_total_all": "traders_tot_all",
    "traders_total_old": "traders_tot_old",
    "traders_total_other": "traders_tot_other",
    "traders_non_commercial_spread_all": "traders_noncomm_spread_all",
    "traders_non_commercial_long_old": "traders_noncomm_long_old",
    "traders_non_commercial_short_old": "traders_noncomm_short_old",
    "traders_non_commercial_spread_old": "traders_noncomm_spread_old",
    "traders_non_commercial_long_other": "traders_noncomm_long_other",
    "traders_non_commercial_short_other": "traders_noncomm_short_other",
    "traders_non_commercial_spread_other": "traders_noncomm_spread_other",
    "traders_commercial_long_all": "traders_comm_long_all",
    "traders_commercial_short_all": "traders_comm_short_all",
    "traders_commercial_long_old": "traders_comm_long_old",
    "traders_commercial_short_old": "traders_comm_short_old",
    "traders_commercial_long_other": "traders_comm_long_other",
    "traders_commercial_short_other": "traders_comm_short_other",
    "traders_producer_merchant_long_all": "traders_prod_merc_long_all",
    "traders_producer_merchant_short_all": "traders_prod_merc_short_all",
    "traders_producer_merchant_long_old": "traders_prod_merc_long_old",
    "traders_producer_merchant_short_old": "traders_prod_merc_short_old",
    "traders_producer_merchant_long_other": "traders_prod_merc_long_other",
    "traders_producer_merchant_short_other": "traders_prod_merc_short_other",
    "traders_swap_long_all": "traders_swap_long_all",
    "traders_swap_short_all": "traders_swap_short_all",
    "traders_swap_spread_all": "traders_swap_spread_all",
    "traders_swap_long_old": "traders_swap_long_old",
    "traders_swap_short_old": "traders_swap_short_old",
    "traders_swap_spread_old": "traders_swap_spread_old",
    "traders_swap_long_other": "traders_swap_long_other",
    "traders_swap_short_other": "traders_swap_short_other",
    "traders_swap_spread_other": "traders_swap_spread_other",
    "traders_managed_money_long_all": "traders_m_money_long_all",
    "traders_managed_money_short_all": "traders_m_money_short_all",
    "traders_managed_money_spread_all": "traders_m_money_spread_all",
    "traders_managed_money_long_old": "traders_m_money_long_old",
    "traders_managed_money_short_old": "traders_m_money_short_old",
    "traders_managed_money_spread_old": "traders_m_money_spread_old",
    "traders_managed_money_long_other": "traders_m_money_long_other",
    "traders_managed_money_short_other": "traders_m_money_short_other",
    "traders_managed_money_spread_other": "traders_m_money_spread_other",
    "traders_asset_manager_long_all": "traders_asset_mgr_long_all",
    "traders_asset_manager_short_all": "traders_asset_mgr_short_all",
    "traders_asset_manager_spread": "traders_asset_mgr_spread_all",
    "traders_dealer_long_all": "traders_dealer_long_all",
    "traders_dealer_short_all": "traders_dealer_short_all",
    "traders_dealer_spread_all": "traders_dealer_spread_all",
    "traders_leveraged_funds_long_all": "traders_lev_money_long_all",
    "traders_leveraged_funds_short_all": "traders_lev_money_short_all",
    "traders_leveraged_funds_spread": "traders_lev_money_spread_all",
    "traders_other_reportable_long_all": "traders_other_rept_long_all",
    "traders_other_reportable_short": "traders_other_rept_short_all",
    "traders_other_reportable_spread": "traders_other_rept_spread_all",
    "traders_other_reportable_long_old": "traders_other_rept_long_old",
    "traders_other_reportable_short_1": "traders_other_rept_short_old",
    "traders_other_reportable_spread_1": "traders_other_rept_spread_old",
    "traders_other_reportable_long_other": "traders_other_rept_long_other",
    "traders_other_reportable_short_2": "traders_other_rept_short_other",
    "traders_other_reportable_spread_2": "traders_other_rept_spread_other",
    "traders_non_commercial_long_all_non_cit": "traders_noncomm_long_all_nocit",
    "traders_non_commercial_short_all_non_cit": "traders_noncomm_short_all_nocit",
    "traders_non_commercial_spread_all_non_cit": "traders_noncomm_spread_all_nocit",
    "traders_commercial_long_all_non_cit": "traders_comm_long_all_nocit",
    "traders_commercial_short_all_non_cit": "traders_comm_short_all_nocit",
    "traders_cit_long_all": "traders_cit_long_all",
    "traders_cit_short_all": "traders_cit_short_all",
    "traders_total_reportable_long_all_non_cit": "traders_tot_rept_long_all_nocit",
    "traders_total_reportable_short_all_non_cit": "traders_tot_rept_short_all_nocit",
    "traders_total_reportable_long_all": "traders_tot_rept_long_all",
    "traders_total_reportable_short_all": "traders_tot_rept_short_all",
    "traders_total_reportable_long_old": "traders_tot_rept_long_old",
    "traders_total_reportable_short_old": "traders_tot_rept_short_old",
    "traders_total_reportable_long_other": "traders_tot_rept_long_other",
    "traders_total_reportable_short_other": "traders_tot_rept_short_other",
    "concentration_gross_top_4_traders_long": "conc_gross_le_4_tdr_long_all",
    "concentration_gross_top_4_traders_short": "conc_gross_le_4_tdr_short_all",
    "concentration_gross_top_8_traders_long": "conc_gross_le_8_tdr_long_all",
    "concentration_gross_top_8_traders_short": "conc_gross_le_8_tdr_short_all",
    "concentration_net_top_4_traders_long_all": "conc_net_le_4_tdr_long_all",
    "concentration_net_top_4_traders_short_all": "conc_net_le_4_tdr_short_all",
    "concentration_net_top_8_traders_long_all": "conc_net_le_8_tdr_long_all",
    "concentration_net_top_8_traders_short_all": "conc_net_le_8_tdr_short_all",
    "concentration_gross_top_4_traders_long_1": "conc_gross_le_4_tdr_long_old",
    "concentration_gross_top_4_traders_short_1": "conc_gross_le_4_tdr_short_old",
    "concentration_gross_top_8_traders_long_1": "conc_gross_le_8_tdr_long_old",
    "concentration_gross_top_8_traders_short_1": "conc_gross_le_8_tdr_short_old",
    "concentration_gross_top_4_traders_long_2": "conc_gross_le_4_tdr_long_other",
    "concentration_gross_top_4_traders_short_2": "conc_gross_le_4_tdr_short_other",
    "concentration_gross_top_8_traders_long_2": "conc_gross_le_8_tdr_long_other",
    "concentration_gross_top_8_traders_short_2": "conc_gross_le_8_tdr_short_other",
    "concentration_net_top_4_traders_long_old": "conc_net_le_4_tdr_long_old",
    "concentration_net_top_4_traders_short_old": "conc_net_le_4_tdr_short_old",
    "concentration_net_top_8_traders_long_old": "conc_net_le_8_tdr_long_old",
    "concentration_net_top_8_traders_short_old": "conc_net_le_8_tdr_short_old",
    "concentration_net_top_4_traders_long_other": "conc_net_le_4_tdr_long_other",
    "concentration_net_top_4_traders_short_other": "conc_net_le_4_tdr_short_other",
    "concentration_net_top_8_traders_long_other": "conc_net_le_8_tdr_long_other",
    "concentration_net_top_8_traders_short_other": "conc_net_le_8_tdr_short_other",
}
_BASE_FIELDS = frozenset(
    {
        "date",
        "report_type",
        "futonly_or_combined",
        "report_week",
        "market_and_exchange_names",
        "cftc_contract_market_code",
        "cftc_market_code",
        "cftc_region_code",
        "cftc_commodity_code",
        "commodity",
        "commodity_group",
        "commodity_subgroup",
        "contract_market_name",
        "contract_units",
        "open_interest_all",
    }
)


class COTQuery(QueryParams):
    """Bounded COT report request; absent date bounds select the latest report week."""

    source: Literal["cftc"] = "cftc"
    market: Literal["us"] = "us"
    code: str | None = None
    report_type: Literal["legacy", "disaggregated", "financial", "supplemental"] = "legacy"
    measure: Literal[
        "all",
        "positions",
        "changes",
        "percent_of_oi",
        "traders",
        "concentration",
    ] = "all"
    futures_only: bool = False
    limit: int | None = Field(default=None, ge=1, le=MAX_LIMIT)

    @field_validator("code")
    @classmethod
    def _code(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise ValueError("code must be a string")
        normalized = value.strip().upper()
        if not _CODE.fullmatch(normalized):
            raise ValueError("code must be a six-character CFTC contract market code")
        return normalized.removeprefix("CFTC_")

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

    @model_validator(mode="after")
    def _query_window(self) -> COTQuery:
        if self.symbol is not None:
            raise ValueError("COT uses code rather than symbol")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must be on or before end_date")
        if self.report_type == "supplemental" and self.futures_only:
            raise ValueError("the CFTC Supplemental report is combined only")
        return self


class COTFetcher(Fetcher[COTQuery, list[dict[str, Any]]]):
    """Fetch, page, and normalize one CFTC COT dataset using its public Socrata API."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "COT"
    capability: ClassVar[Capability] = Capability(
        asset_class="derivatives",
        domain="cftc_commitment_of_traders",
        period="1W",
        market="us",
        source="cftc",
        verified=False,
        notes="Public CFTC PRE schema mapped; independent source verification pending.",
    )

    def transform_query(self, **kwargs: object) -> COTQuery:
        """Validate the report selector and refuse symbols before any request."""
        return COTQuery.model_validate(kwargs)

    def extract_data(self, params: COTQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Read complete bounded pages, defaulting to all market rows in the latest week."""
        dataset_id = REPORT_DATASETS[(params.report_type, params.futures_only)]
        url = f"https://publicreporting.cftc.gov/resource/{dataset_id}.json"
        clauses: list[str] = []
        if params.code is not None:
            clauses.append(f"cftc_contract_market_code = '{params.code}'")
        if params.start_date is not None:
            clauses.append(
                f"report_date_as_yyyy_mm_dd >= '{params.start_date.isoformat()}T00:00:00.000'"
            )
        if params.end_date is not None:
            clauses.append(
                f"report_date_as_yyyy_mm_dd <= '{params.end_date.isoformat()}T23:59:59.999'"
            )

        if params.start_date is None and params.end_date is None and params.limit is None:
            latest_clauses = []
            if params.code is not None:
                latest_clauses.append(f"cftc_contract_market_code = '{params.code}'")
            latest = _client.array_rows(
                _client.get_json(
                    url,
                    params={
                        **({"$where": " AND ".join(latest_clauses)} if latest_clauses else {}),
                        "$order": "report_date_as_yyyy_mm_dd DESC",
                        "$limit": "1",
                        "$offset": "0",
                    },
                    context=ctx,
                )
            )
            if not latest:
                return []
            latest_date = _parse_source_date(latest[0].get("report_date_as_yyyy_mm_dd"))
            clauses.append(f"report_date_as_yyyy_mm_dd = '{latest_date.isoformat()}T00:00:00.000'")

        where = " AND ".join(clauses)
        # Fetch one sentinel row beyond the implicit safety cap so an exact
        # MAX_ROWS result is distinguishable from a truncated larger result.
        page_ceiling = params.limit if params.limit is not None else MAX_ROWS + 1
        rows: list[dict[str, Any]] = []
        offset = 0
        while offset < page_ceiling:
            page_length = min(PAGE_SIZE, page_ceiling - offset)
            query = {
                "$order": "report_date_as_yyyy_mm_dd DESC, cftc_contract_market_code ASC",
                "$limit": str(page_length),
                "$offset": str(offset),
            }
            if where:
                query["$where"] = where
            page = _client.array_rows(_client.get_json(url, params=query, context=ctx))
            if len(page) > page_length:
                raise _client.CFTCProviderError("CFTC_BAD_PAGE")
            rows.extend(page)
            if params.limit is None and len(rows) > MAX_ROWS:
                raise _client.CFTCProviderError("CFTC_RESULT_LIMIT_EXCEEDED")
            offset += len(page)
            if len(page) < page_length:
                return rows
        if len(rows) == MAX_ROWS and params.limit is None:
            raise _client.CFTCProviderError("CFTC_RESULT_LIMIT_EXCEEDED")
        return rows

    def transform_data(self, raw: list[dict[str, Any]], params: COTQuery) -> FetchResult:
        """Map CFTC native columns into the typed COT contract without unit conversion."""
        retained = _fields_for_measure(params.measure)
        results: list[COT] = []
        for source_row in raw:
            source_code = source_row.get("cftc_contract_market_code")
            if not isinstance(source_code, str) or not _SOURCE_CODE.fullmatch(
                source_code.strip().upper()
            ):
                raise _client.CFTCProviderError("CFTC_BAD_ROW")
            if params.code is not None and source_code.strip().upper() != params.code:
                raise _client.CFTCProviderError("CFTC_BAD_ROW")
            record: dict[str, Any] = {}
            for field_name in COT.model_fields:
                if field_name not in retained:
                    continue
                if field_name == "report_type":
                    record[field_name] = params.report_type
                    continue
                if field_name == "futonly_or_combined":
                    record[field_name] = "futures_only" if params.futures_only else "combined"
                    continue
                source_name = _FIELD_SOURCE.get(field_name, field_name)
                if source_name not in source_row:
                    continue
                value = source_row[source_name]
                if field_name == "date":
                    record[field_name] = _parse_source_date(value)
                elif _is_numeric_field(field_name):
                    numeric_kind: Literal["int", "float"]
                    if _contains_type(COT.model_fields[field_name].annotation, int):
                        numeric_kind = "int"
                    else:
                        numeric_kind = "float"
                    record[field_name] = _parse_numeric(value, kind=numeric_kind)
                else:
                    record[field_name] = _parse_text(value)
            try:
                results.append(COT.model_validate(record))
            except (TypeError, ValueError):
                raise _client.CFTCProviderError("CFTC_BAD_ROW") from None
        return tuple(results)


def _fields_for_measure(measure: str) -> frozenset[str]:
    if measure == "all":
        return frozenset(COT.model_fields)
    groups = {
        "positions": lambda name: "positions" in name,
        "changes": lambda name: name.startswith("change_"),
        "percent_of_oi": lambda name: name.startswith("open_interest_pct_"),
        "traders": lambda name: name.startswith("traders_"),
        "concentration": lambda name: name.startswith("concentration_"),
    }
    matches = groups[measure]
    return frozenset(name for name in COT.model_fields if name in _BASE_FIELDS or matches(name))


def _is_numeric_field(field_name: str) -> bool:
    """Identify strict numeric contract fields, including their nullable union."""
    annotation = COT.model_fields[field_name].annotation
    return _contains_type(annotation, int) or _contains_type(annotation, float)


def _contains_type(annotation: object, expected: type) -> bool:
    """Walk Optional/Annotated wrappers used by Pydantic strict field types."""
    if annotation is expected:
        return True
    return any(_contains_type(argument, expected) for argument in get_args(annotation))


def _parse_source_date(value: object) -> date:
    if isinstance(value, datetime):
        if value.time().isoformat() != "00:00:00":
            raise _client.CFTCProviderError("CFTC_BAD_DATE")
        return value.date()
    if not isinstance(value, str):
        raise _client.CFTCProviderError("CFTC_BAD_DATE")
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _client.CFTCProviderError("CFTC_BAD_DATE") from None
    if parsed.hour or parsed.minute or parsed.second or parsed.microsecond:
        raise _client.CFTCProviderError("CFTC_BAD_DATE")
    return parsed.date()


def _parse_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return None if not stripped or stripped.casefold() == "null" else stripped
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise _client.CFTCProviderError("CFTC_BAD_TEXT")


def _parse_numeric(value: object, *, kind: Literal["int", "float"]) -> int | float | None:
    if value is None or (isinstance(value, str) and value.strip().casefold() in {"", "null"}):
        return None
    if isinstance(value, bool):
        raise _client.CFTCProviderError("CFTC_BAD_NUMBER")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _client.CFTCProviderError("CFTC_BAD_NUMBER") from None
    if not parsed.is_finite():
        raise _client.CFTCProviderError("CFTC_BAD_NUMBER")
    if kind == "int":
        if parsed != parsed.to_integral_value():
            raise _client.CFTCProviderError("CFTC_BAD_NUMBER")
        return int(parsed)
    return float(parsed)
