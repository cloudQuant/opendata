"""Tradier current equity quote fetcher."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import ValidationError, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.tradier import TradierEquityQuote
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.tradier.models._client import (
    TradierProviderError,
    fetch_json,
    object_rows,
)
from opendata.data.providers.tradier.models._queries import _clean_symbol


class TradierEquityQuoteQuery(QueryParams):
    """Current quote request for a bounded symbol set."""

    source: Literal["tradier"] = "tradier"
    market: Literal["us"] = "us"
    symbol: str | None = None
    symbols: tuple[str, ...] | None = None
    include_lot_size: bool = False

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str | None) -> str | None:
        return None if value is None else _clean_symbol(value)

    @field_validator("symbols", mode="before")
    @classmethod
    def _validate_symbols(cls, values: object) -> object:
        if values is None:
            return None
        if not isinstance(values, (list, tuple)):
            raise ValueError("symbols must be a sequence of symbols")
        return tuple(_clean_symbol(value) for value in values)

    @field_validator("include_lot_size", mode="before")
    @classmethod
    def _require_bool_lot_size(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("include_lot_size must be a boolean")
        return value

    @model_validator(mode="after")
    def _validate_snapshot_query(self) -> TradierEquityQuoteQuery:
        if (self.symbol is None) == (self.symbols is None):
            raise ValueError("provide exactly one of symbol or symbols")
        if self.symbols is not None and (
            not 1 <= len(self.symbols) <= 50 or len(set(self.symbols)) != len(self.symbols)
        ):
            raise ValueError("symbols must contain 1 to 50 unique values")
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("quote queries are current snapshots and do not accept date windows")
        if type(self.include_lot_size) is not bool:
            raise ValueError("include_lot_size must be a boolean")
        return self

    @property
    def requested_symbols(self) -> tuple[str, ...]:
        """Return canonical symbols in caller order."""
        if self.symbol is not None:
            return (self.symbol,)
        return self.symbols or ()


class TradierEquityQuoteFetcher(Fetcher[TradierEquityQuoteQuery, list[dict[str, Any]]]):
    """Fetch a single quote response for one or more requested stock symbols."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "EquityQuote"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="tradier_equity_quote",
        period="snapshot",
        market="us",
        source="tradier",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> TradierEquityQuoteQuery:
        """Reject historical date windows and bad symbols before credential access."""
        return TradierEquityQuoteQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: TradierEquityQuoteQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Request quotes once using the documented comma-separated ``symbols`` parameter."""
        document = fetch_json(
            "/markets/quotes",
            {
                "symbols": ",".join(params.requested_symbols),
                "includeLotSize": str(params.include_lot_size).lower(),
            },
            ctx.timeout,
        )
        return list(object_rows(document, "quotes", "quote"))

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: TradierEquityQuoteQuery,
    ) -> FetchResult:
        """Map documented quote fields and refuse duplicate, unexpected, or partial responses."""
        if not raw:
            return ()
        records: dict[str, TradierEquityQuote] = {}
        try:
            for item in raw:
                symbol = item.get("symbol")
                if not isinstance(symbol, str):
                    raise TradierProviderError("TRADIER_BAD_SHAPE")
                canonical = symbol.strip().upper()
                record = TradierEquityQuote.model_validate(
                    {
                        "symbol": symbol,
                        "description": item.get("description"),
                        "exchange": item.get("exch"),
                        "security_type": item.get("type"),
                        "last": item.get("last"),
                        "change": item.get("change"),
                        "volume": item.get("volume"),
                        "open": item.get("open"),
                        "high": item.get("high"),
                        "low": item.get("low"),
                        "close": item.get("close"),
                        "bid": item.get("bid"),
                        "ask": item.get("ask"),
                        "change_percentage": item.get("change_percentage"),
                        "average_volume": item.get("average_volume"),
                        "last_volume": item.get("last_volume"),
                        "trade_date": item.get("trade_date"),
                        "previous_close": item.get("prevclose"),
                        "week_52_high": item.get("week_52_high"),
                        "week_52_low": item.get("week_52_low"),
                        "bid_size": item.get("bidsize"),
                        "bid_exchange": item.get("bidexch"),
                        "bid_date": item.get("bid_date"),
                        "ask_size": item.get("asksize"),
                        "ask_exchange": item.get("askexch"),
                        "ask_date": item.get("ask_date"),
                        "lot_size": item.get("lot_size"),
                    }
                )
                if canonical not in params.requested_symbols:
                    raise TradierProviderError("TRADIER_WRONG_SYMBOL")
                if record.security_type != "stock":
                    raise TradierProviderError("TRADIER_WRONG_SECURITY_TYPE")
                if canonical in records:
                    raise TradierProviderError("TRADIER_DUPLICATE_QUOTE")
                records[canonical] = record
        except (ValidationError, TypeError, ValueError) as exc:
            if isinstance(exc, TradierProviderError):
                raise
            raise TradierProviderError("TRADIER_BAD_SHAPE") from None
        missing = set(params.requested_symbols) - records.keys()
        if missing:
            raise TradierProviderError("TRADIER_MISSING_QUOTES")
        return tuple(records[symbol] for symbol in params.requested_symbols)
