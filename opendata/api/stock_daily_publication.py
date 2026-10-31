"""Read one explicitly pinned stock-daily publication through the v2 REST contract.

This route is a narrow bridge for the reviewed stock-daily publication shape.
It never fetches or republishes source data and always returns 404 until
separate production capacity acceptance. Coverage remains unknown unless a
separate evidence-backed coverage route is implemented and reviewed.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from opendata.api.dependencies import (  # noqa: TC001
    CurrentPrincipal,  # FastAPI resolves the dependency annotation at runtime.
)
from opendata.api.published_access import (
    DatasetGrantRegistry,  # noqa: TC001 - FastAPI resolves the dependency annotation at runtime.
)
from opendata.api.published_data import (
    _ERROR_RESPONSES,
    MAX_PAGE_SIZE,
    DataCoverageResponse,
    PublishedRowResponse,
    SnapshotResponse,
    StorageOrderTermResponse,
    _ErrorEnvelopeRoute,
    _http_problem,
    _not_found,
    _page_payload,
    _publication_for_request,
    _publication_for_snapshot,
    _reject_unknown_query,
    _require_access,
    _require_supported_record_contract,
    _resolve_contract,
    _service_unavailable,
    _success_response,
    _validate_hex_id,
    _validate_published_records,
    get_dataset_contracts,
    get_dataset_grants,
    get_publication_store,
)
from opendata.api.stock_daily_capacity import (
    StockDailyCapacityAttestation,
    get_stock_daily_capacity_gate,
)
from opendata.data.dataset_contract_registry import (
    DatasetContractRegistry,  # noqa: TC001 - FastAPI resolves the dependency annotation at runtime.
)
from opendata.data.market_symbol_selection import (
    MarketSymbolSelection,
    MarketSymbolSelectionError,
    validate_selector_pair,
)
from opendata.pipeline.publication_store import (  # noqa: TC001
    PublicationStore,  # FastAPI resolves the dependency annotation at runtime.
)

if TYPE_CHECKING:
    from opendata.api.published_access import AccessContext
    from opendata.data.platform_contracts import DatasetContract
    from opendata.pipeline.publication_store import (
        PublicationRecord,
        PublishedPage,
        PublishedRecord,
        SnapshotRecord,
    )

DATASET_ID = "cn-equity-stock-daily"
SOURCE_MARKET = "cn"
SEMANTIC_VERSION = "stock-daily-raw-v1"
MAX_WINDOW_DAYS = 3_660
_SYMBOL_PATTERN = re.compile(r"[0-9]{6}\Z", re.ASCII)
_SUPPORTED_MARKETS = frozenset({"CN-SSE", "CN-SZSE"})
_EXPECTED_FIELD_TYPES = {
    "market": "string",
    "symbol": "string",
    "trade_date": "date",
    "open": "number",
    "high": "number",
    "low": "number",
    "close": "number",
    "volume": "number",
    "amount": "number",
}
_EXPECTED_FIELD_UNITS = {
    "open": "CNY/share",
    "high": "CNY/share",
    "low": "CNY/share",
    "close": "CNY/share",
    "volume": "share",
    "amount": "CNY",
}
_EXPECTED_FIELD_ADJUSTMENTS = {
    "open": "raw",
    "high": "raw",
    "low": "raw",
    "close": "raw",
    "volume": "not_applicable",
    "amount": "not_applicable",
}
_EXPECTED_FIELD_CURRENCIES = {
    "open": "CNY",
    "high": "CNY",
    "low": "CNY",
    "close": "CNY",
    "amount": "CNY",
}


class StockSelectionResponse(BaseModel):
    """Exchange-qualified selection echoed beside OpenData's source-market value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    market: Literal["CN-SSE", "CN-SZSE"]
    symbol: str = Field(pattern=r"^[0-9]{6}$")


class StockDailySnapshotResponse(SnapshotResponse):
    """Existing v2 snapshot metadata plus this route's exact stock selection."""

    stock_selection: StockSelectionResponse


class StockDailyPageMetaResponse(BaseModel):
    """Existing records-page metadata with the stock-specific pinned snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: StockDailySnapshotResponse
    ordering_fields: list[str]
    storage_order: list[StorageOrderTermResponse]
    identity_key_encoding: Literal["canonical-json-array-v1"]


class StockDailyPageResponse(StockDailySnapshotResponse):
    """One page from the stock-specific fixed-publication records endpoint."""

    rows: list[PublishedRowResponse]
    count: StrictInt
    next_cursor: str | None
    has_more: bool
    coverage: DataCoverageResponse
    meta: StockDailyPageMetaResponse


router = APIRouter(prefix="/api/v2", route_class=_ErrorEnvelopeRoute)


@router.get(
    "/stock-daily/records",
    tags=["Stock Daily Publication"],
    include_in_schema=False,
    response_model=StockDailyPageResponse,
    responses=_ERROR_RESPONSES,
)
async def get_stock_daily_records(
    request: Request,
    *,
    capacity: StockDailyCapacityAttestation = Depends(get_stock_daily_capacity_gate),
    principal: CurrentPrincipal,
    grants: DatasetGrantRegistry = Depends(get_dataset_grants),
    contracts: DatasetContractRegistry = Depends(get_dataset_contracts),
    store: PublicationStore = Depends(get_publication_store),
    market: Literal["CN-SSE", "CN-SZSE"],
    symbol: str = Query(min_length=6, max_length=6),
    publication_id: str = Query(min_length=32, max_length=32),
    start_date: str | None = Query(default=None, min_length=10, max_length=10),
    end_date: str | None = Query(default=None, min_length=10, max_length=10),
    page_size: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
    snapshot_id: str | None = Query(default=None, min_length=32, max_length=32),
    cursor: str | None = Query(default=None, max_length=8_192),
) -> Response:
    """Read a pinned raw stock-daily publication in stable opaque-cursor pages.

    The first page requires an explicit publication and half-open date window.
    Later pages repeat the stock identity and publication id and supply the
    returned snapshot id. The stored snapshot binds the exact rows, window,
    selection, grant scope and publication across every page.

    Args:
        request: Incoming bounded query parameters.
        capacity: Active digest-pinned capacity attestation for this deployment.
        principal: Authenticated user or API-key principal.
        grants: Current operator-reviewed dataset/source grants.
        contracts: Operator-reviewed immutable contract registry.
        store: Configured publication read store.
        market: Exchange identity echoed by this bridge; source payload uses ``cn``.
        symbol: Canonical six-digit A-share code.
        publication_id: Exact lowercase 32-hex publication pin.
        start_date: Inclusive first date on the first page.
        end_date: Exclusive date boundary on the first page.
        page_size: Optional first-page size; later pages inherit the snapshot size.
        snapshot_id: Snapshot returned by the first page.
        cursor: Opaque store cursor returned by the preceding page.

    Returns:
        One snapshot-bound page, original contract units, and explicit unknown coverage.
    """
    allowed = {
        "market",
        "symbol",
        "publication_id",
        "start_date",
        "end_date",
        "page_size",
        "snapshot_id",
        "cursor",
    }
    _reject_unknown_query(request, allowed)
    _validate_hex_id(publication_id, "publication_id")
    _validate_hex_id(snapshot_id, "snapshot_id")
    selection = _validate_stock_selection(market=market, symbol=symbol)
    if cursor is not None and snapshot_id is None:
        raise _http_problem(422, "invalid_request", "cursor requires snapshot_id")

    if snapshot_id is None:
        start = _parse_date(start_date, "start_date")
        end = _parse_date(end_date, "end_date")
        if end <= start:
            raise _http_problem(
                422,
                "invalid_request",
                "Date window must satisfy start_date < end_date",
            )
        if (end - start).days > MAX_WINDOW_DAYS:
            raise _http_problem(422, "invalid_request", "Date window exceeds the supported limit")
        read_access = _require_access(principal, DATASET_ID, "data.read", grants)
        create_access = _require_access(principal, DATASET_ID, "snapshot.create", grants)
        if create_access.source_id != read_access.source_id:
            raise _service_unavailable()
        publication = await _publication_for_request(
            store,
            DATASET_ID,
            publication_id,
            read_access,
        )
        contract, contract_hash = _resolve_contract(contracts, read_access, publication)
        _validate_stock_daily_contract(contract, publication)
        _require_supported_record_contract(contract, start, end)
        _require_capacity_attestation(
            capacity,
            publication=publication,
            contract_hash=contract_hash,
            requested_publication_id=publication_id,
            start=start,
            end=end,
            page_size=page_size or 100,
        )
        snapshot = await _create_snapshot(
            store,
            publication=publication,
            start=start,
            end=end,
            page_size=page_size or 100,
            access=read_access,
            selection=selection,
        )
        publication = await _publication_for_snapshot(store, snapshot, read_access)
        contract, contract_hash = _resolve_contract(contracts, read_access, publication)
        _validate_stock_daily_contract(contract, publication)
        _require_snapshot_publication_match(snapshot, publication)
        _require_snapshot_match(
            snapshot,
            selection=selection,
            publication_id=publication_id,
            start=start,
            end=end,
            internal=True,
        )
        _require_supported_record_contract(contract, snapshot.start_date, snapshot.end_date)
        _require_capacity_attestation(
            capacity,
            publication=publication,
            contract_hash=contract_hash,
            requested_publication_id=publication_id,
            start=snapshot.start_date,
            end=snapshot.end_date,
            page_size=snapshot.page_size,
        )
        page = await _read_page(
            store,
            snapshot=snapshot,
            cursor=None,
            access=read_access,
            selection=selection,
            publication_id=publication_id,
        )
    else:
        if start_date is not None or end_date is not None or page_size is not None:
            raise _http_problem(
                422,
                "invalid_request",
                "Snapshot pages cannot change the window or page size",
            )
        read_access = _require_access(principal, DATASET_ID, "data.read", grants)
        existing = await _get_snapshot(
            store,
            snapshot_id=snapshot_id,
            access=read_access,
        )
        if existing is None or existing.dataset_id != DATASET_ID:
            raise _not_found()
        _require_snapshot_match(
            existing,
            selection=selection,
            publication_id=publication_id,
            start=existing.start_date,
            end=existing.end_date,
        )
        publication = await _publication_for_snapshot(store, existing, read_access)
        contract, contract_hash = _resolve_contract(contracts, read_access, publication)
        _validate_stock_daily_contract(contract, publication)
        _require_snapshot_publication_match(existing, publication)
        _require_supported_record_contract(contract, existing.start_date, existing.end_date)
        _require_capacity_attestation(
            capacity,
            publication=publication,
            contract_hash=contract_hash,
            requested_publication_id=publication_id,
            start=existing.start_date,
            end=existing.end_date,
            page_size=existing.page_size,
        )
        snapshot = existing
        page = await _read_page(
            store,
            snapshot=snapshot,
            cursor=cursor,
            access=read_access,
            selection=selection,
            publication_id=publication_id,
        )

    snapshot = page.snapshot
    _require_snapshot_match(
        snapshot,
        selection=selection,
        publication_id=publication_id,
        start=snapshot.start_date,
        end=snapshot.end_date,
        internal=True,
    )
    _validate_published_records(contract, page.records)
    _validate_stock_rows(
        page.records,
        selection=selection,
        publication=publication,
        snapshot=snapshot,
    )
    payload = _page_payload(
        publication,
        contract,
        contract_hash,
        snapshot,
        page.records,
        page.next_cursor,
    )
    selection_payload = {"market": selection.market, "symbol": selection.symbol}
    payload["stock_selection"] = selection_payload
    payload["coverage"] = _unknown_coverage(snapshot.start_date, snapshot.end_date)
    meta = payload.get("meta")
    if not isinstance(meta, dict) or not isinstance(meta.get("snapshot"), dict):
        raise _service_unavailable()
    meta["snapshot"]["stock_selection"] = selection_payload
    return _success_response(payload)


def _validate_stock_selection(*, market: object, symbol: object) -> MarketSymbolSelection:
    """Require one supported exchange and the canonical code prefix for it."""
    try:
        selection = validate_selector_pair(market=market, symbol=symbol)
    except MarketSymbolSelectionError:
        raise _http_problem(422, "invalid_selection", "Stock selection is invalid") from None
    if (
        selection is None
        or selection.market not in _SUPPORTED_MARKETS
        or _SYMBOL_PATTERN.fullmatch(selection.symbol) is None
    ):
        raise _http_problem(422, "invalid_selection", "Stock selection is invalid")
    if selection.market == "CN-SSE" and not selection.symbol.startswith("6"):
        raise _http_problem(422, "invalid_selection", "Stock selection is invalid")
    if selection.market == "CN-SZSE" and not selection.symbol.startswith(("0", "3")):
        raise _http_problem(422, "invalid_selection", "Stock selection is invalid")
    return selection


def _parse_date(value: str | None, name: str) -> date:
    """Parse one canonical ISO date string without accepting datetime coercion."""
    if type(value) is not str:
        raise _http_problem(422, "invalid_request", f"{name} is required")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise _http_problem(422, "invalid_request", f"{name} must use YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise _http_problem(422, "invalid_request", f"{name} must use YYYY-MM-DD")
    return parsed


async def _create_snapshot(
    store: PublicationStore,
    *,
    publication: PublicationRecord,
    start: date,
    end: date,
    page_size: int,
    access: AccessContext,
    selection: MarketSymbolSelection,
) -> SnapshotRecord:
    """Create one market/symbol snapshot pinned to the requested publication."""
    snapshot = await asyncio.to_thread(
        store.create_snapshot,
        DATASET_ID,
        publication_id=publication.publication_id,
        start_date=start,
        end_date=end,
        page_size=page_size,
        access_scope=access.binding_sha256,
        market=SOURCE_MARKET,
        symbol=selection.symbol,
    )
    _require_snapshot_match(
        snapshot,
        selection=selection,
        publication_id=publication.publication_id,
        start=start,
        end=end,
        internal=True,
    )
    return snapshot


async def _get_snapshot(
    store: PublicationStore,
    *,
    snapshot_id: str,
    access: AccessContext,
) -> SnapshotRecord | None:
    """Resolve the caller-scoped snapshot without falling back to the latest head."""
    try:
        return await asyncio.to_thread(
            store.get_snapshot,
            snapshot_id,
            access_scope=access.binding_sha256,
        )
    except Exception:
        raise _service_unavailable() from None


async def _read_page(
    store: PublicationStore,
    *,
    snapshot: SnapshotRecord,
    cursor: str | None,
    access: AccessContext,
    selection: MarketSymbolSelection,
    publication_id: str,
) -> PublishedPage:
    """Read one stable cursor page and verify the store returned the same scope."""
    page = await asyncio.to_thread(
        store.read_page,
        snapshot.snapshot_id,
        cursor=cursor,
        access_scope=access.binding_sha256,
    )
    if page.snapshot != snapshot:
        raise _service_unavailable()
    _require_snapshot_match(
        page.snapshot,
        selection=selection,
        publication_id=publication_id,
        start=snapshot.start_date,
        end=snapshot.end_date,
        internal=True,
    )
    return page


def _require_snapshot_match(
    snapshot: SnapshotRecord,
    *,
    selection: MarketSymbolSelection,
    publication_id: str,
    start: date | None,
    end: date | None,
    internal: bool = False,
) -> None:
    """Reject a snapshot or continuation whose immutable selector changed."""
    if (
        snapshot.dataset_id != DATASET_ID
        or snapshot.publication_id != publication_id
        or snapshot.market != SOURCE_MARKET
        or snapshot.symbol != selection.symbol
        or snapshot.instrument_id is not None
        or snapshot.capture_id is not None
        or snapshot.start_date != start
        or snapshot.end_date != end
        or snapshot.start_date is None
        or snapshot.end_date is None
    ):
        if internal:
            raise _service_unavailable()
        raise _http_problem(422, "invalid_snapshot_request", "Stock snapshot scope does not match")


def _require_snapshot_publication_match(
    snapshot: SnapshotRecord,
    publication: PublicationRecord,
) -> None:
    """Verify that snapshot metadata identifies the resolved immutable publication."""
    if (
        snapshot.publication_sequence != publication.sequence
        or snapshot.schema_version != publication.schema_version
        or publication.dataset_id != DATASET_ID
        or snapshot.dataset_id != publication.dataset_id
        or snapshot.publication_id != publication.publication_id
    ):
        raise _service_unavailable()


def _require_capacity_attestation(
    attestation: StockDailyCapacityAttestation,
    *,
    publication: PublicationRecord,
    contract_hash: str,
    requested_publication_id: str,
    start: date | None,
    end: date | None,
    page_size: int,
) -> None:
    """Bind the resolved publication and request bounds to the active attestation.

    ``PublicationRecord`` has no ``contract_sha256`` attribute. The publication
    store records the contract digest under ``provenance["contract_sha256"]``,
    and ``_resolve_contract`` already requires that stored digest to equal the
    resolved canonical contract hash. Binding to ``contract_hash`` here therefore
    checks the same exact value through the reviewed resolution path.

    Every mismatch is reported as the route's uniform 404 so capacity settings
    and publication identity cannot be probed.
    """
    if (
        not attestation.is_active()
        or publication.dataset_id != attestation.dataset_id
        or publication.source_id != attestation.source_id
        or publication.semantic_version != attestation.semantic_version
        or contract_hash != attestation.contract_sha256
        or (
            attestation.publication_id is not None
            and (
                attestation.publication_id != requested_publication_id
                or publication.publication_id != requested_publication_id
            )
        )
        or start is None
        or end is None
        or page_size < 1
        or (end - start).days > attestation.max_window_days
        or page_size > attestation.max_page_size
    ):
        raise _not_found()


def _validate_stock_daily_contract(
    contract: DatasetContract,
    publication: PublicationRecord,
) -> None:
    """Require the exact raw stock-daily contract and native field-unit map."""
    field_map = {field.name: field for field in contract.fields}
    if (
        contract.dataset_id != DATASET_ID
        or contract.semantic_version != SEMANTIC_VERSION
        or contract.asset_class != "equity"
        or contract.frequency != "1d"
        or contract.record_kind != "time_series"
        or contract.business_key != ("market", "symbol", "trade_date")
        or contract.ordering != ("trade_date", "market", "symbol")
        or contract.filter_fields != ("market", "symbol")
        or contract.time_field != "trade_date"
        or contract.temporal_semantics != "business_date"
        or contract.adjustment_basis != "raw"
        or contract.pit_policy != "unsupported"
        or contract.currency != "CNY"
        or contract.timezone != "Asia/Shanghai"
        or tuple(field.name for field in contract.fields) != tuple(_EXPECTED_FIELD_TYPES)
        or any(
            field_map[name].data_type != expected or field_map[name].nullable
            for name, expected in _EXPECTED_FIELD_TYPES.items()
        )
        or contract.field_units != _EXPECTED_FIELD_UNITS
        or contract.field_adjustments != _EXPECTED_FIELD_ADJUSTMENTS
        or contract.field_currencies != _EXPECTED_FIELD_CURRENCIES
        or publication.unit != "per-field"
        or publication.adjustment_basis != "raw"
    ):
        raise _service_unavailable()


def _validate_stock_rows(
    records: tuple[PublishedRecord, ...],
    *,
    selection: MarketSymbolSelection,
    publication: PublicationRecord,
    snapshot: SnapshotRecord,
) -> None:
    """Bind every returned row to the requested code, window, and publication."""
    if snapshot.publication_sequence != publication.sequence:
        raise _service_unavailable()
    for record in records:
        business_date = record.business_date
        payload = record.payload
        if (
            record.dataset_id != DATASET_ID
            or record.publication_id != publication.publication_id
            or record.publication_sequence != snapshot.publication_sequence
            or type(business_date) is not date
            or record.event_at is not None
            or record.source_published_at is not None
            or snapshot.start_date is None
            or snapshot.end_date is None
            or not snapshot.start_date <= business_date < snapshot.end_date
            or not isinstance(payload, Mapping)
            or payload.get("market") != SOURCE_MARKET
            or payload.get("symbol") != selection.symbol
            or payload.get("trade_date") != business_date.isoformat()
            or record.business_key
            != json.dumps(
                [SOURCE_MARKET, selection.symbol, business_date.isoformat()],
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        ):
            raise _service_unavailable()


def _unknown_coverage(start: date | None, end: date | None) -> dict[str, object]:
    """Report no coverage conclusion for the selected historical window."""
    requested = (
        {"start": start.isoformat(), "end": end.isoformat()}
        if start is not None and end is not None
        else None
    )
    return {
        "status": "unknown",
        "scope": "unknown",
        "requested": requested,
        "observed": None,
        "missing_ranges": [],
        "evidence_refs": [],
        "limitation": (
            "Coverage has not been independently assessed; rows describe only this pinned "
            "publication and do not establish complete history or full A-share coverage."
        ),
    }
