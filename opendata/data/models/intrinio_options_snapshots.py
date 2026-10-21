"""Intrinio options snapshot contract: one row per listed contract at the snapshot time."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, StrictFloat, StrictInt, StrictStr

from opendata.data.models.base import ContractModel


class IntrinioOptionsSnapshots(ContractModel):
    """One option contract as published in Intrinio's bulk snapshot file.

    The source publishes a gzip CSV whose columns are upper-case labels; the adapter maps
    them to these fields, splits the OCC-style contract id into underlying/expiration/type/
    strike, and keeps every price nullable because an untraded contract has no trade block.
    """

    underlying_symbol: StrictStr
    contract_symbol: StrictStr
    expiration: date
    dte: StrictInt | None = Field(default=None, ge=0)
    strike: StrictFloat = Field(ge=0)
    option_type: Literal["call", "put"]
    volume: StrictInt | None = Field(default=None, ge=0)
    open_interest: StrictInt | None = Field(default=None, ge=0)
    last_price: StrictFloat | None = None
    last_size: StrictInt | None = Field(default=None, ge=0)
    last_timestamp: datetime | None = None
    bid: StrictFloat | None = None
    bid_size: StrictInt | None = Field(default=None, ge=0)
    bid_timestamp: datetime | None = None
    ask: StrictFloat | None = None
    ask_size: StrictInt | None = Field(default=None, ge=0)
    ask_timestamp: datetime | None = None
    total_bid_volume: StrictInt | None = Field(default=None, ge=0)
    bid_high: StrictFloat | None = None
    bid_low: StrictFloat | None = None
    total_ask_volume: StrictInt | None = Field(default=None, ge=0)
    ask_high: StrictFloat | None = None
    ask_low: StrictFloat | None = None
