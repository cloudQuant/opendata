"""Tradier option-chain fetcher with explicit expiry and bounded symbol fan-out."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import ValidationError

from opendata.data.capability import Capability
from opendata.data.models.tradier import TradierOptionChain
from opendata.data.protocol import FetchContext, Fetcher, FetchResult
from opendata.data.providers.tradier.models._client import (
    TradierProviderError,
    TradierQueryLimitError,
    fetch_json,
    object_rows,
    require_token,
)
from opendata.data.providers.tradier.models._queries import TradierOptionsChainQuery


class TradierOptionsChainsFetcher(Fetcher[TradierOptionsChainQuery, list[dict[str, Any]]]):
    """Fetch every requested underlying/expiration chain through its own API request."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "OptionsChains"
    capability: ClassVar[Capability] = Capability(
        asset_class="option",
        domain="tradier_options_chains",
        period="snapshot",
        market="us",
        source="tradier",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> TradierOptionsChainQuery:
        """Validate symbols, expiration, and request caps before key access."""
        return TradierOptionsChainQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: TradierOptionsChainQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Fetch all requested chains or fail without publishing a partial result."""
        require_token()
        rows: list[dict[str, Any]] = []
        requests_made = 0
        for requests_made, symbol in enumerate(params.requested_symbols, start=1):
            document = fetch_json(
                "/markets/options/chains",
                {
                    "symbol": symbol,
                    "expiration": params.expiration.isoformat(),
                    "greeks": str(params.greeks).lower(),
                },
                ctx.timeout,
            )
            option_rows = object_rows(document, "options", "option")
            if len(rows) + len(option_rows) > params.max_records:
                raise TradierQueryLimitError(
                    "TRADIER_RECORD_LIMIT",
                    maximum=params.max_records,
                    requests_made=requests_made,
                )
            rows.extend(
                {
                    "_requested_underlying": symbol,
                    "_requested_expiration": params.expiration,
                    **item,
                }
                for item in option_rows
            )
        return rows

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: TradierOptionsChainQuery,
    ) -> FetchResult:
        """Map the documented chain fields and enforce requested underlying/expiry identity."""
        rows: list[TradierOptionChain] = []
        seen: set[str] = set()
        try:
            for item in raw:
                record = TradierOptionChain.model_validate(
                    {
                        "symbol": item.get("symbol"),
                        "description": item.get("description"),
                        "exchange": item.get("exch"),
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
                        "open_interest": item.get("open_interest"),
                        "contract_size": item.get("contract_size"),
                        "expiration_date": item.get("expiration_date"),
                        "expiration_type": item.get("expiration_type"),
                        "option_type": item.get("option_type"),
                        "root_symbol": item.get("root_symbol"),
                        "underlying": item.get("underlying"),
                        "strike": item.get("strike"),
                        "lot_size": item.get("lot_size"),
                        "greeks": item.get("greeks"),
                    }
                )
                requested_underlying = item.get("_requested_underlying")
                if (
                    requested_underlying not in params.requested_symbols
                    or record.underlying.upper() != requested_underlying
                ):
                    raise TradierProviderError("TRADIER_WRONG_UNDERLYING")
                if record.expiration_date != params.expiration:
                    raise TradierProviderError("TRADIER_WRONG_EXPIRATION")
                if record.symbol in seen:
                    raise TradierProviderError("TRADIER_DUPLICATE_OPTION")
                seen.add(record.symbol)
                rows.append(record)
        except (ValidationError, TypeError, ValueError) as exc:
            if isinstance(exc, TradierProviderError):
                raise
            raise TradierProviderError("TRADIER_BAD_SHAPE") from None
        return tuple(rows)
