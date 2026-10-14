"""Strict output contracts for Congress.gov and licensed Cboe API models.

Congress.gov collection and detail endpoints are snapshots; text files and committee
materials are source documents. These models retain that distinction and only expose
fields normalized from the documented v3 API.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from opendata.data.models.base import ContractModel


class CongressLatestAction(ContractModel):
    """Latest action fields published by the bills and amendments endpoints."""

    action_date: date | None = None
    text: str | None = None
    action_type: str | None = None


class CongressSponsor(ContractModel):
    """A sponsor or cosponsor identified by the Congress.gov member fields."""

    bioguide_id: str | None = None
    name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    party: str | None = None
    state: str | None = None
    district: StrictInt | None = None
    sponsorship_date: date | None = None
    is_original_cosponsor: bool | None = None


class CongressBill(ContractModel):
    """OpenBB CongressBills data row, normalized from Congress.gov v3."""

    update_date: date
    latest_action_date: date | None = None
    bill_url: str
    congress: StrictInt
    bill_number: StrictInt
    origin_chamber: str
    origin_chamber_code: str
    bill_type: str
    title: str
    latest_action: str | None = None
    update_date_including_text: datetime | None = None

    @field_validator("bill_type")
    @classmethod
    def _bill_type_is_uppercase(cls, value: str) -> str:
        if value != value.upper() or not value.isalpha():
            raise ValueError("bill_type must use the OpenBB uppercase type code")
        return value


class CongressBillSubject(ContractModel):
    """One legislative subject or policy area attached to a bill."""

    name: str
    subject_type: str | None = None


class CongressBillSummary(ContractModel):
    """One published summary version attached to a bill."""

    action_date: date | None = None
    version_code: str | None = None
    text: str
    update_date: str | None = None


class CongressCommitteeRef(ContractModel):
    """A committee reference carried by a bill, amendment, or meeting."""

    system_code: str | None = None
    name: str | None = None
    chamber: str | None = None
    url: str | None = None


class CongressBillReference(ContractModel):
    """A related bill returned by the Congress.gov related-bills endpoint."""

    congress: StrictInt
    bill_type: str
    bill_number: StrictInt
    title: str | None = None
    relationship: str | None = None
    url: str | None = None


class CongressBillInfo(ContractModel):
    """Published CongressBillInfo output shape with a typed raw-data payload."""

    markdown_content: str
    raw_data: CongressBillInfoRawData
    source_url: str


class CongressBillInfoRawData(ContractModel):
    """Known bill fields and bounded subresources retained under ``raw_data``."""

    congress: StrictInt
    bill_type: str
    bill_number: StrictInt
    title: str
    update_date: date | None = None
    update_date_including_text: datetime | None = None
    introduced_date: date | None = None
    latest_action: CongressLatestAction | None = None
    sponsors: tuple[CongressSponsor, ...] = ()
    cosponsors: tuple[CongressSponsor, ...] = ()
    subjects: tuple[CongressBillSubject, ...] = ()
    summaries: tuple[CongressBillSummary, ...] = ()
    committees: tuple[CongressCommitteeRef, ...] = ()
    actions: tuple[CongressLatestAction, ...] = ()
    related_bills: tuple[CongressBillReference, ...] = ()
    titles: tuple[str, ...] = ()
    source_url: str
    included_components: tuple[str, ...] = ()


class CongressAmendment(ContractModel):
    """OpenBB CongressAmendments data row, normalized from Congress.gov v3."""

    congress: StrictInt
    amendment_type: str
    number: StrictInt
    amended_bill: str | None = None
    amended_bill_title: str | None = None
    description: str | None = None
    purpose: str | None = None
    latest_action_date: date | None = None
    latest_action: str | None = None
    latest_action_time: str | None = None
    sponsor: str | None = None
    submitted_date: date | None = None
    update_date: date | None = None
    amendment_url: str


class CongressAmendmentInfo(ContractModel):
    """Published CongressAmendmentInfo output shape with typed raw data."""

    markdown_content: str
    raw_data: CongressAmendmentInfoRawData
    source_url: str


class CongressAmendmentInfoRawData(ContractModel):
    """Known amendment fields and bounded subresources retained under raw_data."""

    congress: StrictInt
    amendment_type: str
    amendment_number: StrictInt
    description: str | None = None
    purpose: str | None = None
    update_date: date | None = None
    latest_action: CongressLatestAction | None = None
    sponsors: tuple[CongressSponsor, ...] = ()
    cosponsors: tuple[CongressSponsor, ...] = ()
    actions: tuple[CongressLatestAction, ...] = ()
    text_versions: tuple[str, ...] = ()
    source_url: str
    included_components: tuple[str, ...] = ()


class CongressTextDocument(ContractModel):
    """Downloaded original bill or amendment text bytes, represented as base64."""

    congress: StrictInt
    version_code: str | None = None
    version_date: date | None = None
    document_format: str
    content_base64: str = Field(min_length=1)
    media_type: str
    source_url: str


class CongressBillText(CongressTextDocument):
    """One original bill text file from a documented text version."""

    bill_type: str
    bill_number: StrictInt


class CongressAmendmentText(CongressTextDocument):
    """One original amendment text file from a documented text version."""

    amendment_type: str
    amendment_number: StrictInt


class CongressCommitteeHistory(ContractModel):
    """One committee identity period from the official committee API."""

    official_name: str | None = None
    library_of_congress_name: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    committee_type: str | None = None
    establishing_authority: str | None = None


class CongressCommitteeInfo(ContractModel):
    """Published CongressCommitteeInfo output shape with typed raw data."""

    markdown_content: str
    raw_data: CongressCommitteeInfoRawData
    source_url: str


class CongressCommitteeInfoRawData(ContractModel):
    """Committee endpoint data; cross-source membership is explicitly omitted."""

    system_code: str
    name: str
    chamber: str
    committee_type: str | None = None
    website_url: str | None = None
    is_current: bool | None = None
    parent: CongressCommitteeRef | None = None
    subcommittees: tuple[CongressCommitteeRef, ...] = ()
    history: tuple[CongressCommitteeHistory, ...] = ()
    bill_count: StrictInt | None = None
    report_count: StrictInt | None = None
    nomination_count: StrictInt | None = None
    communication_count: StrictInt | None = None
    source_url: str
    membership_roster_included: Literal[False] = False


class CongressCommitteeDocument(ContractModel):
    """One official committee item or meeting document reference."""

    document_category: str
    committee_code: str
    chamber: str
    congress: StrictInt | None = None
    document_id: str
    title: str
    description: str | None = None
    document_type: str | None = None
    event_date: date | None = None
    document_url: str | None = None
    api_url: str | None = None


class CboeEquityHistorical(ContractModel):
    """One daily OHLC row returned by the documented Cboe All Access endpoint."""

    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int | float | None = None
    vwap: float | None = None
    calls_volume: StrictInt | None = None
    puts_volume: StrictInt | None = None
    total_options_volume: StrictInt | None = None


class CboeEtfHistorical(CboeEquityHistorical):
    """One daily ETF OHLC row, using Cboe's documented equity-history route."""


class CboeEquityQuote(ContractModel):
    """Cboe equity quote contract; unavailable exchange timestamps remain null."""

    symbol: str
    asset_type: str | None = None
    name: str | None = None
    exchange: str | None = None
    bid: float | None = None
    bid_size: StrictInt | None = None
    bid_exchange: str | None = None
    ask: float | None = None
    ask_size: StrictInt | None = None
    ask_exchange: str | None = None
    quote_conditions: int | str | tuple[int | str, ...] | None = None
    quote_indicators: int | str | tuple[int | str, ...] | None = None
    sales_conditions: int | str | tuple[int | str, ...] | None = None
    sequence_number: StrictInt | None = None
    market_center: str | None = None
    participant_timestamp: datetime | None = None
    trf_timestamp: datetime | None = None
    sip_timestamp: datetime | None = None
    last_price: float | None = None
    last_tick: str | None = None
    last_size: StrictInt | None = None
    last_timestamp: datetime | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | float | None = None
    exchange_volume: int | float | None = None
    prev_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    year_high: float | None = None
    year_low: float | None = None
    iv30: float | None = None
    iv30_change: float | None = None
    iv30_change_percent: float | None = None
    iv30_annual_high: float | None = None
    hv30_annual_high: float | None = None
    iv30_annual_low: float | None = None
    hv30_annual_low: float | None = None
    iv60_annual_high: float | None = None
    hv60_annual_high: float | None = None
    iv60_annual_low: float | None = None
    hv60_annual_low: float | None = None
    iv90_annual_high: float | None = None
    hv90_annual_high: float | None = None
    iv90_annual_low: float | None = None
    hv90_annual_low: float | None = None


class CboeOptionsChains(ContractModel):
    """Column-oriented Cboe options-chain result matching its public data contract."""

    underlying_symbol: tuple[str, ...] | None = None
    underlying_price: tuple[float | None, ...] | None = None
    contract_symbol: tuple[str, ...]
    eod_date: tuple[date, ...] | None = None
    expiration: tuple[date, ...]
    dte: tuple[StrictInt | None, ...] | None = None
    strike: tuple[float, ...]
    option_type: tuple[Literal["call", "put"], ...]
    contract_size: tuple[float | None, ...] | None = None
    open_interest: tuple[StrictInt | None, ...] | None = None
    volume: tuple[StrictInt | None, ...] | None = None
    theoretical_price: tuple[float | None, ...] | None = None
    last_trade_price: tuple[float | None, ...] | None = None
    last_trade_size: tuple[float | None, ...] | None = None
    last_trade_time: tuple[datetime | None, ...] | None = None
    tick: tuple[str | None, ...] | None = None
    bid: tuple[float | None, ...] | None = None
    bid_size: tuple[StrictInt | None, ...] | None = None
    bid_time: tuple[datetime | None, ...] | None = None
    bid_exchange: tuple[str | None, ...] | None = None
    ask: tuple[float | None, ...] | None = None
    ask_size: tuple[StrictInt | None, ...] | None = None
    ask_time: tuple[datetime | None, ...] | None = None
    ask_exchange: tuple[str | None, ...] | None = None
    mark: tuple[float | None, ...] | None = None
    open: tuple[float | None, ...] | None = None
    open_bid: tuple[float | None, ...] | None = None
    open_ask: tuple[float | None, ...] | None = None
    high: tuple[float | None, ...] | None = None
    bid_high: tuple[float | None, ...] | None = None
    ask_high: tuple[float | None, ...] | None = None
    low: tuple[float | None, ...] | None = None
    bid_low: tuple[float | None, ...] | None = None
    ask_low: tuple[float | None, ...] | None = None
    close: tuple[float | None, ...] | None = None
    close_size: tuple[float | None, ...] | None = None
    close_time: tuple[datetime | None, ...] | None = None
    close_bid: tuple[float | None, ...] | None = None
    close_bid_size: tuple[float | None, ...] | None = None
    close_bid_time: tuple[datetime | None, ...] | None = None
    close_ask: tuple[float | None, ...] | None = None
    close_ask_size: tuple[float | None, ...] | None = None
    close_ask_time: tuple[datetime | None, ...] | None = None
    prev_close: tuple[float | None, ...] | None = None
    change: tuple[float | None, ...] | None = None
    change_percent: tuple[float | None, ...] | None = None
    implied_volatility: tuple[float | None, ...] | None = None
    delta: tuple[float | None, ...] | None = None
    gamma: tuple[float | None, ...] | None = None
    theta: tuple[float | None, ...] | None = None
    vega: tuple[float | None, ...] | None = None
    rho: tuple[float | None, ...] | None = None

    @model_validator(mode="after")
    def _columns_have_equal_length(self) -> CboeOptionsChains:
        """Keep the source's columnar result aligned across every contract."""
        row_count = len(self.contract_symbol)
        for name in type(self).model_fields:
            values = getattr(self, name)
            if values is not None and len(values) != row_count:
                raise ValueError(f"{name} must have the same number of entries as contract_symbol")
        return self
