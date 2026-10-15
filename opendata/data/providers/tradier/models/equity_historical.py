"""Stock history fetcher with explicit bounds and a type-checked two-request chain."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import ValidationError

from opendata.data.capability import Capability
from opendata.data.models.tradier import TradierEquityHistorical
from opendata.data.protocol import FetchContext, Fetcher, FetchResult
from opendata.data.providers.tradier.models._client import (
    TradierProviderError,
    TradierQueryLimitError,
    fetch_json,
    object_rows,
    require_token,
)
from opendata.data.providers.tradier.models._queries import TradierHistoricalQuery


class TradierEquityHistoricalFetcher(Fetcher[TradierHistoricalQuery, list[dict[str, Any]]]):
    """Fetch stock EOD candles only through Tradier's historical endpoint."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "EquityHistorical"
    row_model: ClassVar[type[TradierEquityHistorical]] = TradierEquityHistorical
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="tradier_equity_historical",
        period="1d",
        market="us",
        source="tradier",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> TradierHistoricalQuery:
        """Validate complete date bounds and fan-out limits before key access."""
        return TradierHistoricalQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: TradierHistoricalQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Verify each symbol is a stock, then collect every requested history response."""
        require_token()
        rows: list[dict[str, Any]] = []
        requests_made = 0
        for symbol in params.requested_symbols:
            matches = object_rows(
                fetch_json("/markets/lookup", {"q": symbol, "types": "stock"}, ctx.timeout),
                "securities",
                "security",
            )
            requests_made += 1
            _require_security_type(matches, symbol, "stock", requests_made)

        for symbol in params.requested_symbols:
            document = fetch_json(
                "/markets/history",
                {
                    "symbol": symbol,
                    "interval": params.interval,
                    "start": params.start_date.isoformat(),
                    "end": params.end_date.isoformat(),
                },
                ctx.timeout,
            )
            requests_made += 1
            history = document.get("history")
            if not isinstance(history, dict):
                raise TradierProviderError("TRADIER_BAD_SHAPE", requests_made=requests_made)
            if "symbol" in history and not _same_symbol(history["symbol"], symbol):
                raise TradierProviderError(
                    "TRADIER_HISTORY_SYMBOL_MISMATCH",
                    requests_made=requests_made,
                )
            day_value = history.get("day")
            if day_value is None:
                continue
            if isinstance(day_value, dict):
                days = (day_value,)
            elif isinstance(day_value, list) and all(isinstance(day, dict) for day in day_value):
                days = tuple(day_value)
            else:
                raise TradierProviderError("TRADIER_BAD_SHAPE", requests_made=requests_made)
            if len(rows) + len(days) > params.max_records:
                raise TradierQueryLimitError(
                    "TRADIER_RECORD_LIMIT",
                    maximum=params.max_records,
                    requests_made=requests_made,
                )
            for day in days:
                if "_requested_symbol" in day:
                    raise TradierProviderError(
                        "TRADIER_RESERVED_FIELD_COLLISION",
                        requests_made=requests_made,
                    )
                row = dict(day)
                row["_requested_symbol"] = symbol
                rows.append(row)
        return rows

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: TradierHistoricalQuery,
    ) -> FetchResult:
        """Validate source rows, date bounds, and stable symbol/date identity."""
        rows_by_key: dict[tuple[str, object], TradierEquityHistorical] = {}
        try:
            for source_row in raw:
                symbol = source_row.get("_requested_symbol")
                record = TradierEquityHistorical.model_validate(
                    {
                        "symbol": symbol,
                        "date": source_row.get("date"),
                        "interval": params.interval,
                        "open": source_row.get("open"),
                        "high": source_row.get("high"),
                        "low": source_row.get("low"),
                        "close": source_row.get("close"),
                        "volume": source_row.get("volume"),
                    }
                )
                if record.symbol not in params.requested_symbols:
                    raise TradierProviderError("TRADIER_WRONG_SYMBOL")
                if not params.start_date <= record.date <= params.end_date:
                    raise TradierProviderError("TRADIER_OUT_OF_WINDOW_ROW")
                key = (record.symbol, record.date)
                if key in rows_by_key:
                    raise TradierProviderError("TRADIER_DUPLICATE_HISTORY_BAR")
                rows_by_key[key] = record
        except (ValidationError, TypeError, ValueError) as exc:
            if isinstance(exc, TradierProviderError):
                raise
            raise TradierProviderError("TRADIER_BAD_SHAPE") from None
        return tuple(rows_by_key[key] for key in sorted(rows_by_key))


def _require_security_type(
    records: tuple[dict[str, Any], ...],
    symbol: str,
    expected: str,
    requests_made: int,
) -> None:
    """Require an exact security lookup match before labeling its history domain."""
    exact = [record for record in records if _same_symbol(record.get("symbol"), symbol)]
    if not exact:
        raise TradierProviderError("TRADIER_SYMBOL_NOT_FOUND", requests_made=requests_made)
    if any(record.get("type") != expected for record in exact):
        raise TradierProviderError("TRADIER_WRONG_SECURITY_TYPE", requests_made=requests_made)


def _same_symbol(value: object, expected: str) -> bool:
    return isinstance(value, str) and value.strip().upper() == expected
