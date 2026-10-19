"""Cboe index snapshots from the public delayed-quotes JSON envelope.

One fixed, unauthenticated official document (``all_us_indices.json``) publishes the whole panel,
so the query selects locally by symbol and row limit; the source offers no server-side filter for
this file. Cell names are mapped through the envelope's own keys, and a numeric zero becomes null
because the reference contract publishes zero-valued snapshot cells as empty (see
``openbb_cboe/models/index_snapshots.py`` ``transform_data``: ``df.replace(0, None)`` before
validation), while ``price_change_percent`` is divided by 100 to the contract's normalized
percentage. ``name``, ``currency`` and ``status`` are declared on the contract but have no cell in
this envelope, so the fetcher never fills them -- the witness names that fact per column.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Literal
from zoneinfo import ZoneInfo

from pydantic import StrictBool, StrictInt, StrictStr, field_validator

from opendata.data.capability import Capability
from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.models.cboe_index_snapshots import CboeIndexSnapshots
from opendata.data.protocol import (
    FetchContext,
    Fetcher,
    FetchResult,
    MultiSymbolQueryParams,
)
from opendata.data.providers.cboe._source import SOURCE
from opendata.data.providers.cboe.specs import CBOE_BASE_URL
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel

SNAPSHOT_URL = f"{CBOE_BASE_URL}/api/global/delayed_quotes/quotes/all_us_indices.json"
_PANEL_SYMBOL = "all_us_indices"
_CHICAGO = ZoneInfo("America/Chicago")
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_ROWS = 5_000
_TIMEOUT_SECONDS = 30.0


class CboeIndexSnapshotsError(RuntimeError):
    """Sanitized transport or envelope failure for the Cboe snapshot panel."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        """Expose only a stable code and an optional HTTP status."""
        self.code = code
        self.status = status
        suffix = "" if status is None else f" status={status}"
        super().__init__(code + suffix)


class CboeIndexSnapshotsQuery(MultiSymbolQueryParams):
    """Optional symbol selection over the one published panel, plus a local row limit."""

    symbol: StrictStr | list[StrictStr] | None = None
    limit: StrictInt | None = None
    use_cache: StrictBool | None = True
    source: Literal["auto", "cboe"] = "auto"
    market: Literal["us"] | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbols(cls, value: object) -> object:
        """Uppercase text symbols without coercing anything else."""
        if isinstance(value, str):
            return value.strip().upper()
        if isinstance(value, list):
            return [item.strip().upper() if isinstance(item, str) else item for item in value]
        return value

    @field_validator("symbol")
    @classmethod
    def _reject_duplicate_symbols(
        cls, value: StrictStr | list[StrictStr] | None
    ) -> StrictStr | list[StrictStr] | None:
        if isinstance(value, list):
            if len(value) != len(set(value)):
                raise ValueError("symbol list must not contain duplicates")
            if len({_selection_key(symbol) for symbol in value}) != len(value):
                raise ValueError("symbol list names one panel index twice")
        return value

    @field_validator("limit")
    @classmethod
    def _bound_limit(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("limit must be positive")
        return value

    @property
    def symbols(self) -> tuple[str, ...]:
        """Requested symbols, empty when the whole panel was asked for."""
        if self.symbol is None:
            return ()
        return (self.symbol,) if isinstance(self.symbol, str) else tuple(self.symbol)


def _http_error_code(status: int | None) -> str:
    """Map a governed HTTP failure onto a provider-specific code."""
    if status == 403:
        return "CBOE_INDEX_SNAPSHOTS_FORBIDDEN"
    if status == 404:
        return "CBOE_INDEX_SNAPSHOTS_NOT_FOUND"
    if status == 429:
        return "CBOE_INDEX_SNAPSHOTS_RATE_LIMITED"
    if status is not None and status >= 500:
        return "CBOE_INDEX_SNAPSHOTS_UPSTREAM_ERROR"
    return "CBOE_INDEX_SNAPSHOTS_HTTP_ERROR"


def _fetch_panel(*, use_cache: bool) -> bytes:
    """Read the one official panel document through the governed, grant-checked client."""
    try:
        response = get_shared_http_client().get(
            SNAPSHOT_URL,
            params={},
            headers={"Accept": "application/json"},
            timeout=(_TIMEOUT_SECONDS, _TIMEOUT_SECONDS),
            source=SOURCE,
            use_cache=use_cache,
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise CboeIndexSnapshotsError(_http_error_code(exc.status), status=exc.status) from None
    except Exception:
        raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_TRANSPORT_ERROR") from None
    body = getattr(response, "content", None)
    if not isinstance(body, bytes) or not body:
        raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_BAD_RESPONSE")
    if len(body) > _MAX_RESPONSE_BYTES:
        raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_RESPONSE_TOO_LARGE")
    return body


def _number(cell: object, *, normalize_percent: bool = False) -> float | None:
    """Map one published numeric cell, turning the envelope's zero into the contract's null.

    Only a real ``null`` (or an omitted cell) is a null row value. A boolean, a string or any
    other non-number in a numeric cell is a corrupt body and must refuse: pydantic's ``StrictFloat``
    would reject it anyway, and widening ``True`` to a price of ``1.0`` is the failure this guard
    exists to prevent. The zero rule applies after percentage normalization, so a published ``0``
    percent cell is still null rather than a value of ``0.0``.
    """
    if cell is None:
        return None
    if isinstance(cell, bool) or not isinstance(cell, (int, float)):
        raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_BAD_NUMERIC_FIELD")
    value = float(cell)
    if not math.isfinite(value):
        raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_BAD_NUMERIC_FIELD")
    if normalize_percent:
        value = round(value / 100.0, 6)
    return None if value == 0.0 else value


def _integer(cell: object) -> int | None:
    """Map the envelope's published share count onto the contract's integer column.

    JSON numbers carry no int/float distinction, so an integral float is the same published count
    and is narrowed; a fractional or boolean cell is not a share count and must refuse rather than
    be rounded, because ``StrictInt`` would reject it and a rounded volume invents traded size.
    """
    if cell is None:
        return None
    if isinstance(cell, bool):
        raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_BAD_INTEGER_FIELD")
    if isinstance(cell, int):
        value = float(cell)
    elif isinstance(cell, float) and math.isfinite(cell) and cell.is_integer():
        value = cell
    else:
        raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_BAD_INTEGER_FIELD")
    return None if value == 0.0 else int(value)


def _trade_time(cell: object) -> datetime | None:
    """Parse the envelope's naive source-clock timestamp into a Chicago-aware datetime."""
    if not isinstance(cell, str) or not cell.strip():
        return None
    try:
        parsed = datetime.fromisoformat(cell.strip())
    except ValueError:
        raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_BAD_TIME") from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=_CHICAGO)


def _selection_key(symbol: str) -> str:
    """The panel's own spelling for one requested index.

    Every published US index symbol in this document is caret-prefixed (810 of 810 cells in the
    2026-10-19 capture), so a caller who names ``VIX`` means the panel row ``^VIX``. Nothing else
    is invented: a request already carrying the caret is looked up verbatim.
    """
    return symbol if symbol.startswith("^") else f"^{symbol}"


def _parse_panel(
    body: bytes, *, symbols: tuple[str, ...], limit: int | None
) -> list[dict[str, object]]:
    """Select the requested index rows out of the published panel."""
    try:
        document = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_BAD_JSON") from None
    if not isinstance(document, dict) or document.get("symbol") != _PANEL_SYMBOL:
        raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_BAD_PANEL")
    records = document.get("data")
    if not isinstance(records, list) or not records:
        raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_EMPTY_PANEL")
    wanted = {_selection_key(symbol) for symbol in symbols}
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_BAD_ROW")
        symbol = record.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_BAD_ROW")
        if wanted and symbol not in wanted:
            continue
        seen.add(symbol)
        rows.append(
            {
                "symbol": symbol,
                "price": _number(record.get("current_price")),
                "open": _number(record.get("open")),
                "high": _number(record.get("high")),
                "low": _number(record.get("low")),
                "close": _number(record.get("close")),
                "prev_close": _number(record.get("prev_day_close")),
                "change": _number(record.get("price_change")),
                "change_percent": _number(
                    record.get("price_change_percent"), normalize_percent=True
                ),
                "bid": _number(record.get("bid")),
                "ask": _number(record.get("ask")),
                "volume": _integer(record.get("volume")),
                "last_trade_time": _trade_time(record.get("last_trade_time")),
            }
        )
        if len(rows) > _MAX_ROWS:
            raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_RESPONSE_TOO_LARGE")
    missing = wanted - seen
    if missing:
        raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_UNKNOWN_SYMBOL")
    if limit is not None:
        rows = rows[:limit]
    return rows


class CboeIndexSnapshotsFetcher(Fetcher[CboeIndexSnapshotsQuery, list[dict[str, object]]]):
    """Publish Cboe's own delayed index panel as index snapshot rows."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "IndexSnapshots"
    row_model: ClassVar[type[ContractModel]] = CboeIndexSnapshots
    capability: ClassVar[Capability] = Capability(
        asset_class="index",
        domain="cboe_index_snapshots",
        period="snapshot",
        market="us",
        source=SOURCE,
        verified=False,
        notes=(
            "Official public Cboe delayed-quotes panel (all_us_indices.json). One document holds "
            "the whole panel, so symbol selection and limit are local. name/currency/status have "
            "no cell in this document and stay null."
        ),
    )

    def transform_query(self, **kwargs: object) -> CboeIndexSnapshotsQuery:
        """Validate selection and limit before any request is admitted."""
        return CboeIndexSnapshotsQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: CboeIndexSnapshotsQuery,
        ctx: FetchContext,
    ) -> list[dict[str, object]]:
        """Fetch the panel document and locally select the requested snapshot rows."""
        del ctx
        body = _fetch_panel(use_cache=params.use_cache is not False)
        return _parse_panel(body, symbols=params.symbols, limit=params.limit)

    def transform_data(
        self,
        raw: list[dict[str, object]],
        params: CboeIndexSnapshotsQuery,
    ) -> FetchResult:
        """Validate every selected row against the Cboe snapshot contract."""
        del params
        try:
            return [CboeIndexSnapshots.model_validate(row, strict=True) for row in raw]
        except Exception:
            raise CboeIndexSnapshotsError("CBOE_INDEX_SNAPSHOTS_BAD_ROW") from None


__all__ = [
    "SNAPSHOT_URL",
    "CboeIndexSnapshotsError",
    "CboeIndexSnapshotsFetcher",
    "CboeIndexSnapshotsQuery",
]
