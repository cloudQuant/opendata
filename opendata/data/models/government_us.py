"""Typed government-US outputs built from public source contracts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from opendata.data.models.base import ContractModel


class CommodityPsdData(ContractModel):
    """One USDA PSD row normalized to the public OpenBB data contract."""

    region: str | None = None
    country: str | None = None
    commodity: str | None = None
    attribute: str | None = None
    marketing_year: str | None = None
    value: float | int | None = None
    unit: str | None = None


class CommodityPsdReport(ContractModel):
    """An unmodified USDA publication, base64 encoded as specified publicly."""

    content: str
    data_format: dict[str, str] | None = None


class TreasuryAuction(ContractModel):
    """TreasuryDirect/FiscalData auction row with the public typed field set."""

    cusip: str
    issue_date: date
    security_type: Literal["Bill", "Note", "Bond", "CMB", "TIPS", "FRN"]
    security_term: str
    maturity_date: date
    interest_rate: float | None = None
    cpi_on_issue_date: float | None = None
    cpi_on_dated_date: float | None = None
    announcement_date: date | None = None
    auction_date: date | None = None
    auction_date_year: int | None = None
    dated_date: date | None = None
    first_payment_date: date | None = None
    accrued_interest_per_100: float | None = None
    accrued_interest_per_1000: float | None = None
    adjusted_accrued_interest_per_100: float | None = None
    adjusted_accrued_interest_per_1000: float | None = None
    adjusted_price: float | None = None
    allocation_percentage: float | None = None
    allocation_percentage_decimals: float | None = None
    announced_cusip: str | None = None
    auction_format: str | None = None
    avg_median_discount_rate: float | None = None
    avg_median_investment_rate: float | None = None
    avg_median_price: float | None = None
    avg_median_discount_margin: float | None = None
    avg_median_yield: float | None = None
    back_dated: Literal["Yes", "No"] | None = None
    back_dated_date: date | None = None
    bid_to_cover_ratio: float | None = None
    call_date: date | None = None
    callable: Literal["Yes", "No"] | None = None
    called_date: date | None = None
    cash_management_bill: Literal["Yes", "No"] | None = None
    closing_time_competitive: str | None = None
    closing_time_non_competitive: str | None = None
    competitive_accepted: int | None = None
    competitive_accepted_decimals: int | None = None
    competitive_tendered: int | None = None
    competitive_tenders_accepted: Literal["Yes", "No"] | None = None
    corp_us_cusip: str | None = None
    cpi_base_reference_period: str | None = None
    currently_outstanding: int | None = None
    direct_bidder_accepted: int | None = None
    direct_bidder_tendered: int | None = None
    est_amount_of_publicly_held_maturing_security: int | None = None
    fima_included: Literal["Yes", "No"] | None = None
    fima_non_competitive_accepted: int | None = None
    fima_non_competitive_tendered: int | None = None
    first_interest_period: str | None = None
    first_interest_payment_date: date | None = None
    floating_rate: Literal["Yes", "No"] | None = None
    frn_index_determination_date: date | None = None
    frn_index_determination_rate: float | None = None
    high_discount_rate: float | None = None
    high_investment_rate: float | None = None
    high_price: float | None = None
    high_discount_margin: float | None = None
    high_yield: float | None = None
    index_ratio_on_issue_date: float | None = None
    indirect_bidder_accepted: int | None = None
    indirect_bidder_tendered: int | None = None
    interest_payment_frequency: str | None = None
    low_discount_rate: float | None = None
    low_investment_rate: float | None = None
    low_price: float | None = None
    low_discount_margin: float | None = None
    low_yield: float | None = None
    maturing_date: date | None = None
    max_competitive_award: int | None = None
    max_non_competitive_award: int | None = None
    max_single_bid: int | None = None
    min_bid_amount: int | None = None
    min_strip_amount: int | None = None
    min_to_issue: int | None = None
    multiples_to_bid: int | None = None
    multiples_to_issue: int | None = None
    nlp_exclusion_amount: int | None = None
    nlp_reporting_threshold: int | None = None
    non_competitive_accepted: int | None = None
    non_competitive_tenders_accepted: Literal["Yes", "No"] | None = None
    offering_amount: int | None = None
    original_cusip: str | None = None
    original_dated_date: date | None = None
    original_issue_date: date | None = None
    original_security_term: str | None = None
    pdf_announcement: str | None = None
    pdf_competitive_results: str | None = None
    pdf_non_competitive_results: str | None = None
    pdf_special_announcement: str | None = None
    price_per_100: float | None = None
    primary_dealer_accepted: int | None = None
    primary_dealer_tendered: int | None = None
    reopening: Literal["Yes", "No"] | None = None
    security_term_day_month: str | None = None
    security_term_week_year: str | None = None
    series: str | None = None
    soma_accepted: int | None = None
    soma_holdings: int | None = None
    soma_included: Literal["Yes", "No"] | None = None
    soma_tendered: int | None = None
    spread: float | None = None
    standard_payment_per_1000: float | None = None
    strippable: Literal["Yes", "No"] | None = None
    term: str | None = None
    tiin_conversion_factor_per_1000: float | None = None
    tips: Literal["Yes", "No"] | None = None
    total_accepted: int | None = None
    total_tendered: int | None = None
    treasury_retail_accepted: int | None = None
    treasury_retail_tenders_accepted: Literal["Yes", "No"] | None = None
    type: str | None = None
    unadjusted_accrued_interest_per_1000: float | None = None
    unadjusted_price: float | None = None
    updated_timestamp: datetime | None = None
    xml_announcement: str | None = None
    xml_competitive_results: str | None = None
    xml_special_announcement: str | None = None
    tint_cusip1: str | None = None
    tint_cusip2: str | None = None


class TreasuryPrice(ContractModel):
    """Reserved OpenBB output identity pending an available matching source."""

    issuer_name: str | None = None
    cusip: str | None = None
    isin: str | None = None
    security_type: str | None = None
    issue_date: date | None = None
    maturity_date: date | None = None
    call_date: date | None = None
    bid: float | None = None
    offer: float | None = None
    eod_price: float | None = None
    last_traded_date: date | None = None
    total_trades: int | None = None
    last_price: float | None = None
    highest_price: float | None = None
    lowest_price: float | None = None
    rate: float | None = None
    ytm: float | None = None
    date: date


class WeatherBulletin(ContractModel):
    """NOAA/NWS text-product metadata and original bulletin text.

    This is a source-defined contract pending confirmation of an OpenBB
    WeatherBulletin output schema.
    """

    id: str
    wmo_collective_id: str | None = None
    issuing_office: str
    issuance_time: datetime
    product_code: str
    product_name: str
    product_text: str


class WeatherBulletinDownload(ContractModel):
    """Original bulletin/document bytes encoded in the public base64 contract."""

    content: str
    data_format: dict[str, str] | None = None
