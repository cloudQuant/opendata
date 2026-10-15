"""Daily history from the Cboe-linked public volatility-index CSV downloads.

The route set is deliberately an allowlist copied from Cboe's historical-data page. Date bounds
are applied locally after each complete CSV response; there is no source-side pagination or range
request documented for these files.
"""

from __future__ import annotations

import csv
import io
import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import ClassVar, Literal

from pydantic import StrictBool, StrictStr, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.models.base import ContractModel
from opendata.data.models.cboe_index_historical import CboeIndexHistorical
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, MultiSymbolQueryParams
from opendata.data.providers.cboe._source import SOURCE
from opendata.data.request_budget import RequestBudgetError

_HISTORY_URLS: dict[str, str] = {
    "VIX": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    "VVIX": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VVIX_History.csv",
    "VIX9D": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv",
    "OVX": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/OVX_History.csv",
    "GVZ": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/GVZ_History.csv",
    "VXAPL": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VXAPL_History.csv",
    "VXAZN": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VXAZN_History.csv",
    "VXEEM": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VXEEM_History.csv",
}
_OHLC_HEADER = ("DATE", "OPEN", "HIGH", "LOW", "CLOSE")
_CSV_HEADERS: dict[str, tuple[str, ...]] = {
    "VIX": _OHLC_HEADER,
    "VVIX": ("DATE", "VVIX"),
    "VIX9D": _OHLC_HEADER,
    "OVX": ("DATE", "OVX"),
    "GVZ": ("DATE", "GVZ"),
    "VXAPL": _OHLC_HEADER,
    "VXAZN": _OHLC_HEADER,
    "VXEEM": _OHLC_HEADER,
}
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_QUERY_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_QUERY_ROWS = 100_000
_TIMEOUT_SECONDS = 30.0


class CboeIndexHistoricalError(RuntimeError):
    """Sanitized CSV or transport failure for public Cboe index history."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        """Expose only a stable code and optional HTTP status."""
        self.code = code
        self.status = status
        suffix = "" if status is None else f" status={status}"
        super().__init__(code + suffix)


class CboeIndexHistoricalQuery(MultiSymbolQueryParams):
    """Linked volatility-index symbols, daily date filters, and raw-response cache choice."""

    symbol: StrictStr | list[StrictStr]
    interval: Literal["1d"] = "1d"
    use_cache: StrictBool | None = True
    start_date: date | None = None
    end_date: date | None = None
    source: Literal["auto", "cboe"] = "auto"
    market: Literal["us"] | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: object) -> object:
        """Normalize case and whitespace without converting non-text values."""
        if isinstance(value, str):
            return value.strip().upper()
        if isinstance(value, list):
            return [item.strip().upper() if isinstance(item, str) else item for item in value]
        return value

    @field_validator("symbol")
    @classmethod
    def _validate_linked_symbols(
        cls, value: StrictStr | list[StrictStr]
    ) -> StrictStr | list[StrictStr]:
        """Reject every symbol without a direct link on Cboe's public history page."""
        symbols = (value,) if isinstance(value, str) else tuple(value)
        if not symbols or len(symbols) > len(_HISTORY_URLS):
            raise ValueError("one through eight Cboe-linked symbols are required")
        if len(symbols) != len(set(symbols)):
            raise ValueError("symbol list must not contain duplicates")
        unsupported = set(symbols) - _HISTORY_URLS.keys()
        if unsupported:
            raise ValueError("symbol has no explicitly linked Cboe daily-history CSV")
        return value

    @model_validator(mode="after")
    def _validate_date_window(self) -> CboeIndexHistoricalQuery:
        """Validate local date filters before a whole-file request is admitted."""
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self

    @property
    def symbols(self) -> tuple[str, ...]:
        """Return the query's validated symbols in caller order."""
        return (self.symbol,) if isinstance(self.symbol, str) else tuple(self.symbol)


def _source_date(value: str) -> date:
    """Parse Cboe's observed MM/DD/YYYY source date, requiring its exact spelling."""
    try:
        parsed = datetime.strptime(value, "%m/%d/%Y").date()
    except ValueError:
        raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_BAD_CSV") from None
    if parsed.strftime("%m/%d/%Y") != value:
        raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_BAD_CSV")
    return parsed


def _source_number(value: str) -> float | None:
    """Parse one finite CSV number and preserve an empty nullable cell as null."""
    token = value.strip()
    if not token:
        return None
    try:
        parsed = Decimal(token)
        numeric = float(parsed)
    except (InvalidOperation, OverflowError, ValueError):
        raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_BAD_CSV") from None
    if not parsed.is_finite() or not math.isfinite(numeric):
        raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_BAD_CSV")
    return numeric


def _parse_csv(
    body: bytes,
    *,
    symbol: str,
    start_date: date | None,
    end_date: date | None,
) -> list[dict[str, object]]:
    """Validate this symbol's observed CSV columns and map only published values."""
    if not body or len(body) > _MAX_RESPONSE_BYTES:
        raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_BAD_CSV")
    try:
        text = body.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = tuple(next(reader))
    except (StopIteration, UnicodeDecodeError, csv.Error):
        raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_BAD_CSV") from None
    expected_header = _CSV_HEADERS[symbol]
    if header != expected_header:
        raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_BAD_CSV")

    source_dates: set[date] = set()
    records: list[tuple[date, dict[str, object]]] = []
    try:
        for cells in reader:
            if not cells:
                continue
            if len(cells) != len(expected_header):
                raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_BAD_CSV")
            row_date = _source_date(cells[0].strip())
            if row_date in source_dates:
                raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_DUPLICATE_DATE")
            source_dates.add(row_date)
            source_values = tuple(_source_number(cell) for cell in cells[1:])
            if expected_header == _OHLC_HEADER:
                open_value, high_value, low_value, close_value = source_values
            else:
                # These observed files publish one daily index value, not an OHLC bar.
                open_value, high_value, low_value = None, None, None
                close_value = source_values[0]
            if start_date is not None and row_date < start_date:
                continue
            if end_date is not None and row_date > end_date:
                continue
            records.append(
                (
                    row_date,
                    {
                        "symbol": symbol,
                        "date": row_date,
                        "open": open_value,
                        "high": high_value,
                        "low": low_value,
                        "close": close_value,
                        "volume": None,
                        "calls_volume": None,
                        "puts_volume": None,
                        "total_options_volume": None,
                    },
                )
            )
            if len(records) > _MAX_QUERY_ROWS:
                raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_RESPONSE_TOO_LARGE")
    except csv.Error:
        raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_BAD_CSV") from None
    return [row for _, row in sorted(records, key=lambda item: item[0])]


def _http_error_code(status: int | None) -> str:
    """Map a governed HTTP failure to a safe provider-specific code."""
    if status == 403:
        return "CBOE_INDEX_HISTORICAL_FORBIDDEN"
    if status == 404:
        return "CBOE_INDEX_HISTORICAL_NOT_FOUND"
    if status == 429:
        return "CBOE_INDEX_HISTORICAL_RATE_LIMITED"
    if status is not None and status >= 500:
        return "CBOE_INDEX_HISTORICAL_UPSTREAM_ERROR"
    return "CBOE_INDEX_HISTORICAL_HTTP_ERROR"


def _fetch_csv(url: str, context: FetchContext, *, use_cache: bool) -> bytes:
    """Fetch one fixed official CSV through the governed, grant-checked HTTP client."""
    timeout = (
        _TIMEOUT_SECONDS if context.timeout is None else min(_TIMEOUT_SECONDS, context.timeout)
    )
    try:
        response = get_shared_http_client().get(
            url,
            params={},
            headers={"Accept": "text/csv"},
            timeout=(min(5.0, timeout), timeout),
            source=SOURCE,
            use_cache=use_cache,
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise CboeIndexHistoricalError(_http_error_code(exc.status), status=exc.status) from None
    except Exception:
        raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_TRANSPORT_ERROR") from None

    body = getattr(response, "content", None)
    if not isinstance(body, bytes):
        raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_BAD_RESPONSE")
    if not body or len(body) > _MAX_RESPONSE_BYTES:
        raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_RESPONSE_TOO_LARGE")
    return body


class CboeIndexHistoricalFetcher(Fetcher[CboeIndexHistoricalQuery, list[dict[str, object]]]):
    """Read public Cboe-linked daily volatility-index CSV files."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "IndexHistorical"
    # Verified against contract_model('cboe_index_historical') ->
    # opendata.data.models.cboe_index_historical.CboeIndexHistorical (the frozen row this
    # fetcher's transform_data already builds), as the offline witness requires.
    row_model: ClassVar[type[ContractModel]] = CboeIndexHistorical
    capability: ClassVar[Capability] = Capability(
        asset_class="index",
        domain="cboe_index_historical",
        period="1d",
        market="us",
        source=SOURCE,
        verified=False,
        notes=(
            "Official Cboe-linked daily volatility-index CSV subset. The source publishes full "
            "files; date filters are local. Captured per-symbol headers are strict; single-value "
            "files populate close only. Other linked symbols remain outside this allowlist."
        ),
    )

    def transform_query(self, **kwargs: object) -> CboeIndexHistoricalQuery:
        """Validate identity, interval, linked symbols, and date ordering before I/O."""
        return CboeIndexHistoricalQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: CboeIndexHistoricalQuery,
        ctx: FetchContext,
    ) -> list[dict[str, object]]:
        """Fetch complete linked CSVs and locally select the requested daily rows."""
        total_bytes = 0
        records: list[dict[str, object]] = []
        for symbol in params.symbols:
            body = _fetch_csv(_HISTORY_URLS[symbol], ctx, use_cache=params.use_cache is not False)
            total_bytes += len(body)
            if total_bytes > _MAX_QUERY_RESPONSE_BYTES:
                raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_RESPONSE_TOO_LARGE")
            records.extend(
                _parse_csv(
                    body,
                    symbol=symbol,
                    start_date=params.start_date,
                    end_date=params.end_date,
                )
            )
            if len(records) > _MAX_QUERY_ROWS:
                raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_RESPONSE_TOO_LARGE")
        return records

    def transform_data(
        self,
        raw: list[dict[str, object]],
        params: CboeIndexHistoricalQuery,
    ) -> FetchResult:
        """Validate each source row against the Cboe index-history contract."""
        del params
        try:
            return [CboeIndexHistorical.model_validate(row, strict=True) for row in raw]
        except Exception:
            raise CboeIndexHistoricalError("CBOE_INDEX_HISTORICAL_BAD_ROW") from None


__all__ = [
    "CboeIndexHistoricalError",
    "CboeIndexHistoricalFetcher",
    "CboeIndexHistoricalQuery",
]
