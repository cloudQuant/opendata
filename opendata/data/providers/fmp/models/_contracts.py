"""FMP query validation and compatibility exports for equity price contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import field_validator

from opendata.data.models.equity_price import EquityHistorical, EquityQuote
from opendata.data.protocol import QueryParams

__all__ = ["EquityHistorical", "EquityQuote", "FMPQueryParams"]


class FMPQueryParams(QueryParams):
    """Common FMP query requiring a safe, non-empty symbol."""

    source: Literal["fmp"] = "fmp"
    market: Literal["us"] = "us"
    symbol: str

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("symbol must not be empty")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("symbol must not contain control characters")
        return value
