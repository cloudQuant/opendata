"""Versioned REST access to immutable published datasets and snapshots.

The routes in this module read only the publication store. Provider acquisition,
cleaning, and publication happen in separate pipeline jobs. Every data read is
bound to a reviewed dataset grant and a fixed publication snapshot.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import threading
from collections.abc import Callable, Coroutine, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse

from opendata.api.dependencies import (
    CurrentPrincipal,  # noqa: TC001 - FastAPI needs this runtime dependency alias.
)
from opendata.api.published_access import (
    AccessContext,
    DatasetGrantRegistry,
    GrantConfigurationError,
    PublishedAccessError,
    list_permitted_datasets,
    load_dataset_grant_registry,
    require_published_access,
)
from opendata.core.config import settings
from opendata.data.dataset_contract_registry import (
    DatasetContractRegistry,
    canonical_sha256,
    load_dataset_contract_registry,
)
from opendata.data.platform_contracts import (
    AdjustmentBasis,
    AssetClass,
    DatasetContract,
    Frequency,
    PitPolicy,
    RecordKind,
    TemporalSemantics,
)
from opendata.pipeline.publication_store import (
    MAX_PAGE_SIZE,
    PageSizeLimitError,
    PublicationRecord,
    PublicationStore,
    PublishedRecord,
    SnapshotError,
    SnapshotExpiredError,
    SnapshotRecord,
    SnapshotSizeLimitError,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from opendata.api.dependencies import Principal

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_CATALOG_ITEMS = 10_000
_ROUTER_PREFIX = "/api/v2"
_CONFIGURED_STORE: PublicationStore | None = None
_CONFIGURED_ENGINE: Engine | None = None
_STORE_LOCK = threading.Lock()
_IDENTITY_SEMANTICS: dict[str, object] = {
    "version": 1,
    "identity_key_encoding": "canonical-json-array-v1",
    "revision": "latest non-deleted revision per business_key at the pinned publication sequence",
    "temporal_fields": {
        "business_date": (
            "business-date bucket; null ordering last (route rejects null time values)"
        ),
        "observed_at": "source observation time",
        "source_published_at": "source publication time",
        "available_at": "platform availability time",
        "event_at": "source event time",
    },
}


class ErrorDetail(BaseModel):
    """Stable, sanitized API error body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    request_id: str


class ErrorEnvelope(BaseModel):
    """Top-level error response used by all v2 route failures."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error: ErrorDetail


class SourceResponse(BaseModel):
    """Public source identity copied from the reviewed data contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    source: str


class SchemaFieldResponse(BaseModel):
    """One versioned result field and its declared JSON value type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    data_type: Literal[
        "string", "integer", "number", "boolean", "date", "datetime", "object", "array"
    ]
    nullable: bool


class QualityResponse(BaseModel):
    """Normalized quality receipt stored with the publication."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed", "warning", "unknown"]
    report_id: str | None
    issue_count: StrictInt


class WindowResponse(BaseModel):
    """Half-open business-date window pinned to a snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: date | None
    end: date | None
    end_exclusive: bool


class StorageOrderTermResponse(BaseModel):
    """One actual store ordering term, distinct from declared contract fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    direction: Literal["asc", "desc"]
    nulls: Literal["first", "last"] | None = None
    comparison: Literal["native", "binary_lexicographic"]


class IdentitySemanticsResponse(BaseModel):
    """Declared dataset keys paired with the store's stable page ordering."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: StrictInt
    business_key_fields: list[str]
    identity_key_encoding: Literal["canonical-json-array-v1"]
    revision: str
    ordering_fields: list[str]
    storage_order: list[StorageOrderTermResponse]
    temporal_semantics: str
    time_field: str | None
    temporal_fields: dict[str, str]


class ContractMetadataResponse(BaseModel):
    """Contract, publication, and source facts shared by dataset and snapshot DTOs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str
    publication_id: str
    sequence: StrictInt
    schema_version: str
    semantic_version: str
    contract_sha256: str
    source: SourceResponse
    schema_: list[SchemaFieldResponse] = Field(serialization_alias="schema")
    quality: QualityResponse
    field_units: dict[str, str]
    field_currencies: dict[str, str]
    field_adjustments: dict[str, str]
    currency: str | None
    adjustment_basis: AdjustmentBasis
    asset_class: AssetClass
    frequency: Frequency
    record_kind: RecordKind
    business_key_fields: list[str]
    ordering_fields: list[str]
    storage_order: list[StorageOrderTermResponse]
    identity_key_encoding: Literal["canonical-json-array-v1"]
    filter_fields: list[str]
    time_field: str | None
    temporal_semantics: TemporalSemantics
    source_available_at_field: str | None
    calendar_id: str | None
    timezone: str
    pit_policy: PitPolicy
    as_of: datetime
    identity_semantics: IdentitySemanticsResponse


class DatasetResponse(ContractMetadataResponse):
    """Current published head metadata for one permitted dataset."""

    record_count: StrictInt
    content_sha256: str


class DatasetListResponse(BaseModel):
    """Accessible current publication summaries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[DatasetResponse]
    count: StrictInt


class SnapshotResponse(ContractMetadataResponse):
    """Immutable metadata and boundaries for one fixed publication snapshot."""

    snapshot_id: str
    window: WindowResponse
    page_size: StrictInt
    created_at: datetime
    expires_at: datetime
    record_count: StrictInt
    selection_sha256: str
    content_sha256: str


class PublishedRowResponse(BaseModel):
    """One published record with independent business and source timestamps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str
    business_key: str
    revision: StrictInt
    publication_id: str
    business_date: date | None
    event_at: datetime | None
    observed_at: datetime | None
    source_published_at: datetime | None
    available_at: datetime | None
    payload: dict[str, Any]
    record_hash: str


class PageMetaResponse(BaseModel):
    """Namespaced snapshot reference echoed alongside the top-level fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: SnapshotResponse
    ordering_fields: list[str]
    storage_order: list[StorageOrderTermResponse]
    identity_key_encoding: Literal["canonical-json-array-v1"]


class DataPageResponse(SnapshotResponse):
    """One page of records from a fixed snapshot."""

    rows: list[PublishedRowResponse]
    count: StrictInt
    next_cursor: str | None
    has_more: bool
    meta: PageMetaResponse


class CoverageRangeResponse(BaseModel):
    """Half-open date range in a coverage request or evidence response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: date
    end: date


class DataCoverageResponse(BaseModel):
    """Coverage state with explicit scope, evidence, and limitation fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["complete", "partial", "source_limited", "unknown", "not_applicable"]
    scope: Literal[
        "business_history",
        "archived_snapshot_observations",
        "current_snapshot",
        "point_in_time_records",
        "not_applicable",
        "unknown",
    ]
    requested: CoverageRangeResponse | None
    observed: CoverageRangeResponse | None
    missing_ranges: list[CoverageRangeResponse]
    evidence_refs: list[str]
    limitation: str | None


class CoverageResponse(BaseModel):
    """Coverage response that distinguishes unknown evidence from complete data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str
    publication_id: str
    contract_sha256: str
    coverage: DataCoverageResponse


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope, "description": message}
    for code, message in (
        (401, "Authentication required"),
        (403, "Dataset unavailable"),
        (404, "Published resource unavailable"),
        (410, "Snapshot expired"),
        (413, "Response exceeds the size limit"),
        (422, "Request validation failed"),
        (500, "Request failed"),
        (503, "Published data service unavailable"),
    )
}


class CreateSnapshotRequest(BaseModel):
    """Strict request body for creating one fixed read snapshot."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    dataset_id: str = Field(min_length=1, max_length=128)
    publication_id: str | None = Field(default=None, min_length=32, max_length=32)
    start_date: date | None = None
    end_date: date | None = None
    page_size: StrictInt = Field(default=100, ge=1, le=MAX_PAGE_SIZE)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def parse_json_date(cls, value: object) -> object:
        """Accept canonical JSON ISO dates while rejecting datetime strings."""
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("date must use YYYY-MM-DD") from exc
            if parsed.isoformat() != value:
                raise ValueError("date must use YYYY-MM-DD")
            return parsed
        return value

    @field_validator("publication_id")
    @classmethod
    def validate_publication_id(cls, value: str | None) -> str | None:
        """Require publication identifiers to use the store's lowercase hex form."""
        if value is not None and (
            len(value) != 32 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("publication_id is invalid")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> CreateSnapshotRequest:
        """Reject an empty or reversed half-open date window."""
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("both date window endpoints are required together")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date <= self.start_date
        ):
            raise ValueError("date window must satisfy start_date < end_date")
        return self


class _ErrorEnvelopeRoute(APIRoute):
    """Render route and dependency errors in a stable, sanitized envelope."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the FastAPI handler so app-level status handlers cannot swallow details."""
        route_handler = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await route_handler(request)
            except RequestValidationError:
                return _error_response(422, "invalid_request", "Request validation failed")
            except PublishedAccessError:
                return _error_response(403, "dataset_unavailable", "Dataset is unavailable")
            except SnapshotExpiredError:
                return _error_response(410, "snapshot_expired", "Snapshot has expired")
            except PageSizeLimitError:
                return _error_response(
                    413,
                    "response_too_large",
                    "Response exceeds the 8 MiB limit",
                )
            except SnapshotSizeLimitError:
                return _error_response(
                    413,
                    "snapshot_too_large",
                    "Snapshot exceeds the supported record limit",
                )
            except SnapshotError:
                return _error_response(
                    422, "invalid_snapshot_request", "Snapshot request is invalid"
                )
            except HTTPException as exc:
                if isinstance(exc.detail, dict):
                    code = exc.detail.get("code")
                    message = exc.detail.get("message")
                    if isinstance(code, str) and isinstance(message, str):
                        return _error_response(
                            exc.status_code,
                            code,
                            message,
                            headers=exc.headers,
                        )
                code, message = _http_error_contract(exc.status_code)
                return _error_response(exc.status_code, code, message, headers=exc.headers)
            except SQLAlchemyError:
                return _error_response(
                    503,
                    "published_data_unavailable",
                    "Published data service is unavailable",
                )
            except Exception:
                return _error_response(
                    500,
                    "internal_error",
                    "Published data request failed",
                )

        return handler


router = APIRouter(prefix=_ROUTER_PREFIX, route_class=_ErrorEnvelopeRoute)


def get_publication_store() -> PublicationStore:
    """Return the explicitly configured publication store or fail closed.

    This dependency creates no warehouse connection and performs no DDL. The
    first authorized store operation verifies the schema through a normal read.
    Tests and embedding applications may override it with an isolated store.
    """
    try:
        return _configured_publication_store()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Published data service is unavailable",
        ) from None


def _configured_publication_store() -> PublicationStore:
    """Build one sync store from server configuration without exposing secrets."""
    global _CONFIGURED_ENGINE, _CONFIGURED_STORE
    with _STORE_LOCK:
        if _CONFIGURED_STORE is not None:
            return _CONFIGURED_STORE
        cursor_key = _server_setting("PUBLISHED_DATA_CURSOR_SIGNING_KEY")
        if cursor_key is None or len(cursor_key.encode("utf-8")) < 32:
            raise RuntimeError("published data signing configuration is unavailable")

        explicit_database_fields = {
            "data_mysql_host",
            "data_mysql_user",
            "data_mysql_database",
        }
        if not explicit_database_fields.issubset(settings.model_fields_set):
            raise RuntimeError("published data warehouse configuration is unavailable")

        engine: Engine | None = None
        try:
            engine = create_engine(settings.data_database_url, pool_pre_ping=True)
            store = PublicationStore(engine, cursor_signing_key=cursor_key.encode("utf-8"))
            _CONFIGURED_ENGINE = engine
            _CONFIGURED_STORE = store
            return store
        except Exception:
            if engine is not None:
                engine.dispose()
            raise


def close_publication_store() -> None:
    """Dispose the process-owned warehouse connection pool during shutdown."""
    global _CONFIGURED_ENGINE, _CONFIGURED_STORE
    with _STORE_LOCK:
        engine = _CONFIGURED_ENGINE
        _CONFIGURED_ENGINE = None
        _CONFIGURED_STORE = None
    if engine is not None:
        engine.dispose()


def get_dataset_grants() -> DatasetGrantRegistry:
    """Load the operator-reviewed grant registry from an explicit server path."""
    path = _server_setting("PUBLISHED_DATASET_GRANTS_FILE")
    if path is None or not path.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Published data service is unavailable",
        )
    try:
        return load_dataset_grant_registry(path)
    except GrantConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Published data service is unavailable",
        ) from None


def get_dataset_contracts() -> DatasetContractRegistry:
    """Load immutable dataset contracts from an explicit operator-owned path."""
    path = _server_setting("PUBLISHED_DATASET_CONTRACTS_FILE")
    if path is None or not path.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Published data service is unavailable",
        )
    try:
        return load_dataset_contract_registry(path)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Published data service is unavailable",
        ) from None


def _server_setting(name: str) -> str | None:
    """Read one operator setting from the process environment or configured env file."""
    configured = os.environ.get(name)
    if configured is not None:
        return configured
    env_file = settings.model_config.get("env_file", ".env")
    if env_file is None:
        return None
    if isinstance(env_file, (str, Path)):
        candidate = env_file
    elif isinstance(env_file, (tuple, list)) and env_file:
        candidate = env_file[0]
        if not isinstance(candidate, (str, Path)):
            return None
    else:
        return None
    try:
        from dotenv import dotenv_values

        value = dotenv_values(candidate, interpolate=False).get(name)
    except (ImportError, OSError, TypeError, ValueError):
        return None
    return value if isinstance(value, str) else None


@router.get("/datasets", response_model=DatasetListResponse, responses=_ERROR_RESPONSES)
async def list_datasets(
    principal: CurrentPrincipal,
    grants: DatasetGrantRegistry = Depends(get_dataset_grants),
    contracts: DatasetContractRegistry = Depends(get_dataset_contracts),
    store: PublicationStore = Depends(get_publication_store),
) -> Response:
    """List only published datasets permitted by the caller's current grants."""
    allowed = {
        context.dataset_id: context
        for context in list_permitted_datasets(principal, grants, operation="data.read")
    }
    if not allowed:
        return _success_response({"items": [], "count": 0})
    summaries = await asyncio.to_thread(store.list_datasets, limit=MAX_CATALOG_ITEMS)
    items: list[dict[str, object]] = []
    for summary in summaries:
        context = allowed.get(summary.dataset_id)
        if context is None:
            continue
        publication = await asyncio.to_thread(store.get_publication, summary.publication_id)
        if publication is None:
            raise _service_unavailable()
        _require_source_match(context, publication)
        contract, contract_hash = _resolve_contract(contracts, context, publication)
        items.append(_dataset_summary(publication, contract, contract_hash))
    return _success_response({"items": items, "count": len(items)})


@router.get("/datasets/{dataset_id}", response_model=DatasetResponse, responses=_ERROR_RESPONSES)
async def get_dataset(
    dataset_id: str,
    principal: CurrentPrincipal,
    grants: DatasetGrantRegistry = Depends(get_dataset_grants),
    contracts: DatasetContractRegistry = Depends(get_dataset_contracts),
    store: PublicationStore = Depends(get_publication_store),
) -> Response:
    """Return the current immutable publication metadata for one permitted dataset."""
    context = _require_access(principal, dataset_id, "data.read", grants)
    publication = await _current_publication(store, dataset_id)
    _require_source_match(context, publication)
    contract, contract_hash = _resolve_contract(contracts, context, publication)
    return _success_response(_dataset_summary(publication, contract, contract_hash))


@router.get(
    "/datasets/{dataset_id}/records",
    response_model=DataPageResponse,
    responses=_ERROR_RESPONSES,
)
async def get_dataset_records(
    dataset_id: str,
    request: Request,
    principal: CurrentPrincipal,
    grants: DatasetGrantRegistry = Depends(get_dataset_grants),
    contracts: DatasetContractRegistry = Depends(get_dataset_contracts),
    store: PublicationStore = Depends(get_publication_store),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    page_size: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
    publication_id: str | None = Query(default=None, min_length=32, max_length=32),
    snapshot_id: str | None = Query(default=None, min_length=32, max_length=32),
    cursor: str | None = Query(default=None, max_length=8_192),
) -> Response:
    """Read a fixed snapshot page, creating the snapshot on the first request."""
    _reject_unknown_query(
        request, {"start_date", "end_date", "page_size", "publication_id", "snapshot_id", "cursor"}
    )
    _validate_hex_id(publication_id, "publication_id")
    _validate_hex_id(snapshot_id, "snapshot_id")
    if start_date is not None and end_date is not None and end_date <= start_date:
        raise _http_problem(
            422, "invalid_request", "Date window must satisfy start_date < end_date"
        )
    if snapshot_id is None and (start_date is None) != (end_date is None):
        raise _http_problem(422, "invalid_request", "Provide both date window endpoints")

    if snapshot_id is None:
        if cursor is not None:
            raise _http_problem(422, "invalid_request", "cursor requires snapshot_id")
        data_access = _require_access(principal, dataset_id, "data.read", grants)
        create_access = _require_access(principal, dataset_id, "snapshot.create", grants)
        if data_access.source_id != create_access.source_id:
            raise _service_unavailable()
        publication = await _publication_for_request(store, dataset_id, publication_id, data_access)
        contract, contract_hash = _resolve_contract(contracts, data_access, publication)
        _require_supported_record_contract(contract, start_date, end_date)
        snapshot = await asyncio.to_thread(
            store.create_snapshot,
            dataset_id,
            publication_id=publication.publication_id,
            start_date=start_date,
            end_date=end_date,
            page_size=page_size or 100,
            access_scope=data_access.binding_sha256,
        )
        publication = await _publication_for_snapshot(store, snapshot, data_access)
        contract, contract_hash = _resolve_contract(contracts, data_access, publication)
        page = await asyncio.to_thread(
            store.read_page,
            snapshot.snapshot_id,
            access_scope=data_access.binding_sha256,
        )
    else:
        if (
            start_date is not None
            or end_date is not None
            or page_size is not None
            or publication_id is not None
        ):
            raise _http_problem(
                422,
                "invalid_request",
                "Snapshot pages cannot change the window, publication, or page size",
            )
        data_access = _require_access(principal, dataset_id, "data.read", grants)
        existing_snapshot = await asyncio.to_thread(
            store.get_snapshot,
            snapshot_id,
            access_scope=data_access.binding_sha256,
        )
        if existing_snapshot is None or existing_snapshot.dataset_id != dataset_id:
            raise _not_found()
        snapshot = existing_snapshot
        publication = await _publication_for_snapshot(store, snapshot, data_access)
        contract, contract_hash = _resolve_contract(contracts, data_access, publication)
        _require_supported_record_contract(contract, snapshot.start_date, snapshot.end_date)
        page = await asyncio.to_thread(
            store.read_page,
            snapshot.snapshot_id,
            cursor=cursor,
            access_scope=data_access.binding_sha256,
        )

    _validate_published_records(contract, page.records)
    return _success_response(
        _page_payload(
            publication, contract, contract_hash, page.snapshot, page.records, page.next_cursor
        )
    )


@router.post(
    "/snapshots",
    status_code=status.HTTP_201_CREATED,
    response_model=SnapshotResponse,
    responses=_ERROR_RESPONSES,
)
async def create_snapshot(
    body: CreateSnapshotRequest,
    request: Request,
    principal: CurrentPrincipal,
    grants: DatasetGrantRegistry = Depends(get_dataset_grants),
    contracts: DatasetContractRegistry = Depends(get_dataset_contracts),
    store: PublicationStore = Depends(get_publication_store),
) -> Response:
    """Create a fixed snapshot without reading or returning its records."""
    if "idempotency-key" in request.headers:
        raise _http_problem(
            422,
            "unsupported_header",
            "Idempotency-Key is not supported for snapshot creation",
        )
    context = _require_access(principal, body.dataset_id, "snapshot.create", grants)
    publication = await _publication_for_request(
        store, body.dataset_id, body.publication_id, context
    )
    contract, contract_hash = _resolve_contract(contracts, context, publication)
    _require_supported_record_contract(contract, body.start_date, body.end_date)
    snapshot = await asyncio.to_thread(
        store.create_snapshot,
        body.dataset_id,
        publication_id=publication.publication_id,
        start_date=body.start_date,
        end_date=body.end_date,
        page_size=body.page_size,
        access_scope=context.binding_sha256,
    )
    publication = await _publication_for_snapshot(store, snapshot, context)
    contract, contract_hash = _resolve_contract(contracts, context, publication)
    return _success_response(
        _snapshot_payload(publication, contract, contract_hash, snapshot), status_code=201
    )


@router.get(
    "/snapshots/{snapshot_id}",
    response_model=SnapshotResponse,
    responses=_ERROR_RESPONSES,
)
async def get_snapshot(
    snapshot_id: str,
    request: Request,
    principal: CurrentPrincipal,
    grants: DatasetGrantRegistry = Depends(get_dataset_grants),
    contracts: DatasetContractRegistry = Depends(get_dataset_contracts),
    store: PublicationStore = Depends(get_publication_store),
    dataset_id: str | None = Query(default=None, min_length=1, max_length=128),
) -> Response:
    """Read snapshot metadata after authorizing its explicit dataset identity."""
    _reject_unknown_query(request, {"dataset_id"})
    _validate_hex_id(snapshot_id, "snapshot_id")
    if dataset_id is None:
        raise _http_problem(422, "invalid_request", "dataset_id is required")
    context = _require_access(principal, dataset_id, "snapshot.read", grants)
    snapshot = await asyncio.to_thread(
        store.get_snapshot,
        snapshot_id,
        access_scope=context.binding_sha256,
    )
    if snapshot is None or snapshot.dataset_id != dataset_id:
        raise _not_found()
    publication = await _publication_for_snapshot(store, snapshot, context)
    contract, contract_hash = _resolve_contract(contracts, context, publication)
    return _success_response(_snapshot_payload(publication, contract, contract_hash, snapshot))


@router.get(
    "/datasets/{dataset_id}/coverage",
    response_model=CoverageResponse,
    responses=_ERROR_RESPONSES,
)
async def get_dataset_coverage(
    dataset_id: str,
    request: Request,
    principal: CurrentPrincipal,
    grants: DatasetGrantRegistry = Depends(get_dataset_grants),
    contracts: DatasetContractRegistry = Depends(get_dataset_contracts),
    store: PublicationStore = Depends(get_publication_store),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
) -> Response:
    """Return UNKNOWN until independent dataset-specific coverage evidence exists."""
    _reject_unknown_query(request, {"start_date", "end_date"})
    context = _require_access(principal, dataset_id, "coverage.read", grants)
    publication = await _current_publication(store, dataset_id)
    _require_source_match(context, publication)
    contract, contract_hash = _resolve_contract(contracts, context, publication)
    if (start_date is None) != (end_date is None):
        raise _http_problem(422, "invalid_request", "Provide both range endpoints or neither")
    if start_date is not None and end_date is not None and end_date <= start_date:
        raise _http_problem(
            422, "invalid_request", "Date window must satisfy start_date < end_date"
        )
    if start_date is not None and contract.temporal_semantics != "business_date":
        raise _http_problem(
            422,
            "unsupported_time_semantics",
            "Coverage date ranges are only supported for business_date datasets",
        )
    requested: dict[str, object] | None = None
    if start_date is not None and end_date is not None:
        requested = {"start": start_date.isoformat(), "end": end_date.isoformat()}
    coverage: dict[str, object] = {
        "status": "unknown",
        "scope": "unknown",
        "requested": requested,
        "observed": None,
        "missing_ranges": [],
        "evidence_refs": [],
        "limitation": "No independently verified coverage evidence is registered for this dataset.",
    }
    return _success_response(
        {
            "dataset_id": dataset_id,
            "publication_id": publication.publication_id,
            "contract_sha256": contract_hash,
            "coverage": coverage,
        }
    )


async def _current_publication(store: PublicationStore, dataset_id: str) -> PublicationRecord:
    """Resolve the latest published head for an exact dataset identifier."""
    summaries = await asyncio.to_thread(store.list_datasets, limit=MAX_CATALOG_ITEMS)
    summary = next((item for item in summaries if item.dataset_id == dataset_id), None)
    if summary is None:
        raise _not_found()
    publication = await asyncio.to_thread(store.get_publication, summary.publication_id)
    if publication is None or publication.dataset_id != dataset_id:
        raise _service_unavailable()
    return publication


async def _publication_for_request(
    store: PublicationStore,
    dataset_id: str,
    publication_id: str | None,
    context: AccessContext,
) -> PublicationRecord:
    """Resolve the requested publication and enforce the current source grant."""
    if publication_id is None:
        publication = await _current_publication(store, dataset_id)
    else:
        requested_publication = await asyncio.to_thread(store.get_publication, publication_id)
        if requested_publication is None or requested_publication.dataset_id != dataset_id:
            raise _not_found()
        publication = requested_publication
    _require_source_match(context, publication)
    return publication


async def _publication_for_snapshot(
    store: PublicationStore,
    snapshot: SnapshotRecord,
    context: AccessContext,
) -> PublicationRecord:
    """Resolve immutable snapshot publication metadata and recheck its source grant."""
    publication = await asyncio.to_thread(store.get_publication, snapshot.publication_id)
    if (
        publication is None
        or publication.dataset_id != snapshot.dataset_id
        or publication.schema_version != snapshot.schema_version
    ):
        raise _service_unavailable()
    _require_source_match(context, publication)
    return publication


def _require_access(
    principal: Principal,
    dataset_id: str,
    operation: str,
    grants: DatasetGrantRegistry,
) -> AccessContext:
    """Authorize an exact operation against the server-side grant registry."""
    return require_published_access(principal, dataset_id, operation, grants)


def _require_source_match(context: AccessContext, publication: PublicationRecord) -> None:
    """Fail closed when a publication's source differs from the reviewed grant."""
    if publication.dataset_id != context.dataset_id or publication.source_id != context.source_id:
        raise _service_unavailable()


def _resolve_contract(
    contracts: DatasetContractRegistry,
    context: AccessContext,
    publication: PublicationRecord,
) -> tuple[DatasetContract, str]:
    """Resolve and verify the exact reviewed contract pinned by a publication."""
    try:
        contract = contracts.resolve(
            publication.dataset_id,
            publication.schema_version,
            publication.semantic_version,
        )
    except Exception:
        raise _service_unavailable() from None
    contract_hash = canonical_sha256(contract)
    if (
        contract.dataset_id != context.dataset_id
        or contract.source.source_id != context.source_id
        or publication.provenance.get("contract_sha256") != contract_hash
        or contract.adjustment_basis != publication.adjustment_basis
    ):
        raise _service_unavailable()
    _quality_receipt(publication)
    return contract, contract_hash


def _require_supported_record_contract(
    contract: DatasetContract,
    start_date: date | None,
    end_date: date | None,
) -> None:
    """Reject request semantics the current date-window store cannot guarantee."""
    if contract.temporal_semantics != "business_date" or contract.time_field is None:
        raise _http_problem(
            422,
            "unsupported_time_semantics",
            "This API supports business_date datasets only",
        )
    if contract.record_kind != "time_series":
        raise _http_problem(
            422,
            "unsupported_record_kind",
            "This API supports time_series datasets only",
        )
    if start_date is None or end_date is None:
        raise _http_problem(
            422,
            "date_window_required",
            "business_date reads require both half-open date endpoints",
        )
    expected_ordering = tuple(dict.fromkeys((contract.time_field, *contract.business_key)))
    if contract.ordering != expected_ordering:
        raise _http_problem(
            422,
            "unsupported_ordering",
            "This API requires ordering by the declared time field then business key fields",
        )
    field_by_name = {field.name: field for field in contract.fields}
    if any(
        field_by_name[name].data_type not in {"string", "date"} for name in contract.business_key
    ):
        raise _http_problem(
            422,
            "unsupported_business_key",
            "This API supports string and date business key fields only",
        )
    if contract.pit_policy == "required":
        raise _http_problem(
            422,
            "point_in_time_required",
            "This API does not support required source point-in-time selection",
        )


def _validate_published_records(
    contract: DatasetContract,
    records: tuple[PublishedRecord, ...],
) -> None:
    """Validate every persisted payload against the exact pinned field schema."""
    field_by_name = {field.name: field for field in contract.fields}
    for record in records:
        if (
            record.dataset_id != contract.dataset_id
            or not record.business_key
            or record.revision < 1
        ):
            raise _service_unavailable()
        payload = record.payload
        if not isinstance(payload, Mapping):
            raise _service_unavailable()
        payload_fields = set(payload)
        expected_fields = set(field_by_name)
        missing_required = {
            name
            for name, field in field_by_name.items()
            if not field.nullable and name not in payload_fields
        }
        if payload_fields - expected_fields or missing_required:
            raise _service_unavailable()
        for name, value in payload.items():
            field = field_by_name[name]
            if value is None:
                if not field.nullable:
                    raise _service_unavailable()
                continue
            if not _payload_value_matches(field.data_type, value):
                raise _service_unavailable()
        key_values = [payload.get(name) for name in contract.business_key]
        try:
            expected_key = json.dumps(
                key_values,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            raise _service_unavailable() from None
        if record.business_key != expected_key:
            raise _service_unavailable()
        if record.business_date is None:
            raise _service_unavailable()
        if contract.time_field is None:
            raise _service_unavailable()
        time_value = payload.get(contract.time_field)
        if not isinstance(time_value, str) or time_value != record.business_date.isoformat():
            raise _service_unavailable()


def _payload_value_matches(data_type: str, value: object) -> bool:
    """Check JSON storage values using the API contract's declared data type."""
    if data_type == "string":
        return isinstance(value, str)
    if data_type == "integer":
        return type(value) is int
    if data_type == "number":
        if type(value) is int:
            return True
        return type(value) is float and math.isfinite(value)
    if data_type == "boolean":
        return isinstance(value, bool)
    if data_type == "object":
        return isinstance(value, dict)
    if data_type == "array":
        return isinstance(value, list)
    if data_type == "date":
        if not isinstance(value, str):
            return False
        try:
            return date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False
    if data_type == "datetime":
        if not isinstance(value, str):
            return False
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return False
        return parsed.tzinfo is not None and parsed.utcoffset() is not None
    return False


def _quality_receipt(publication: PublicationRecord) -> dict[str, object]:
    """Read a bounded, normalized quality summary without inventing defaults."""
    quality = publication.quality
    status_value = quality.get("status")
    report_id = quality.get("report_id")
    issue_count = quality.get("issue_count")
    if (
        status_value not in {"passed", "warning", "unknown"}
        or (report_id is not None and not isinstance(report_id, str))
        or type(issue_count) is not int
        or issue_count < 0
    ):
        raise _service_unavailable()
    return {"status": status_value, "report_id": report_id, "issue_count": issue_count}


def _contract_metadata(
    publication: PublicationRecord,
    contract: DatasetContract,
    contract_hash: str,
    *,
    as_of: datetime,
) -> dict[str, object]:
    """Build the common fully contract-backed public metadata fields."""
    storage_order: list[dict[str, str]] = [
        {
            "field": "business_date",
            "direction": "asc",
            "nulls": "last",
            "comparison": "native",
        },
        {
            "field": "business_key",
            "direction": "asc",
            "comparison": "binary_lexicographic",
        },
    ]
    return {
        "dataset_id": publication.dataset_id,
        "publication_id": publication.publication_id,
        "sequence": publication.sequence,
        "schema_version": publication.schema_version,
        "semantic_version": publication.semantic_version,
        "contract_sha256": contract_hash,
        "source": contract.source.model_dump(mode="json"),
        "schema": [field.model_dump(mode="json") for field in contract.fields],
        "quality": _quality_receipt(publication),
        "field_units": dict(contract.field_units),
        "field_currencies": dict(contract.field_currencies),
        "field_adjustments": dict(contract.field_adjustments),
        "currency": contract.currency,
        "adjustment_basis": contract.adjustment_basis,
        "asset_class": contract.asset_class,
        "frequency": contract.frequency,
        "record_kind": contract.record_kind,
        "business_key_fields": list(contract.business_key),
        "ordering_fields": list(contract.ordering),
        "storage_order": storage_order,
        "identity_key_encoding": "canonical-json-array-v1",
        "filter_fields": list(contract.filter_fields),
        "time_field": contract.time_field,
        "temporal_semantics": contract.temporal_semantics,
        "source_available_at_field": contract.source_available_at_field,
        "calendar_id": contract.calendar_id,
        "timezone": contract.timezone,
        "pit_policy": contract.pit_policy,
        "as_of": _iso(as_of),
        "identity_semantics": {
            **_IDENTITY_SEMANTICS,
            "business_key_fields": list(contract.business_key),
            "ordering_fields": list(contract.ordering),
            "identity_key_encoding": "canonical-json-array-v1",
            "storage_order": storage_order,
            "temporal_semantics": contract.temporal_semantics,
            "time_field": contract.time_field,
        },
    }


def _dataset_summary(
    publication: PublicationRecord,
    contract: DatasetContract,
    contract_hash: str,
) -> dict[str, object]:
    """Project public metadata for the latest immutable publication."""
    return {
        **_contract_metadata(
            publication,
            contract,
            contract_hash,
            as_of=publication.published_at,
        ),
        "record_count": publication.record_count,
        "content_sha256": publication.content_sha256,
    }


def _snapshot_payload(
    publication: PublicationRecord,
    contract: DatasetContract,
    contract_hash: str,
    snapshot: SnapshotRecord,
) -> dict[str, object]:
    """Project all immutable scope and publication fields for a snapshot."""
    return {
        **_contract_metadata(publication, contract, contract_hash, as_of=snapshot.as_of),
        "snapshot_id": snapshot.snapshot_id,
        "window": _window(snapshot.start_date, snapshot.end_date),
        "page_size": snapshot.page_size,
        "created_at": _iso(snapshot.created_at),
        "expires_at": _iso(snapshot.expires_at),
        "record_count": snapshot.record_count,
        "selection_sha256": snapshot.selection_sha256,
        "content_sha256": snapshot.content_sha256,
    }


def _page_payload(
    publication: PublicationRecord,
    contract: DatasetContract,
    contract_hash: str,
    snapshot: SnapshotRecord,
    records: tuple[PublishedRecord, ...],
    next_cursor: str | None,
) -> dict[str, object]:
    """Build one fixed-window response and preserve row identity/timing provenance."""
    rows: list[dict[str, object]] = [
        {
            "dataset_id": record.dataset_id,
            "business_key": record.business_key,
            "revision": record.revision,
            "publication_id": record.publication_id,
            "business_date": record.business_date.isoformat()
            if record.business_date is not None
            else None,
            "event_at": _iso_or_none(record.event_at),
            "observed_at": _iso_or_none(record.observed_at),
            "source_published_at": _iso_or_none(record.source_published_at),
            "available_at": _iso_or_none(record.available_at),
            "payload": dict(record.payload),
            "record_hash": record.record_hash,
        }
        for record in records
    ]
    snapshot_metadata = _snapshot_payload(publication, contract, contract_hash, snapshot)
    return {
        **snapshot_metadata,
        "rows": rows,
        "count": len(rows),
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
        "meta": {
            "snapshot": snapshot_metadata,
            "ordering_fields": snapshot_metadata["ordering_fields"],
            "storage_order": snapshot_metadata["storage_order"],
            "identity_key_encoding": snapshot_metadata["identity_key_encoding"],
        },
    }


def _window(start: date | None, end: date | None) -> dict[str, object]:
    """Serialize a half-open date interval while preserving open endpoints."""
    return {
        "start": start.isoformat() if start is not None else None,
        "end": end.isoformat() if end is not None else None,
        "end_exclusive": True,
    }


def _iso(value: datetime) -> str:
    """Return an ISO-8601 timestamp, normalizing naive database values as UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _iso_or_none(value: datetime | None) -> str | None:
    """Serialize an optional timestamp without deriving it from other fields."""
    return _iso(value) if value is not None else None


def _reject_unknown_query(request: Request, allowed: set[str]) -> None:
    """Reject any query parameter the route does not explicitly implement."""
    keys = list(request.query_params.keys())
    if any(key not in allowed for key in keys) or any(
        len(request.query_params.getlist(key)) != 1 for key in keys
    ):
        raise _http_problem(
            422, "unsupported_query", "Query parameters are unsupported or repeated"
        )


def _validate_hex_id(value: str | None, name: str) -> None:
    """Reject malformed lowercase UUID-hex identifiers without echoing their value."""
    if value is not None and (
        len(value) != 32 or any(character not in "0123456789abcdef" for character in value)
    ):
        raise _http_problem(422, "invalid_request", f"{name} is invalid")


def _success_response(payload: Mapping[str, object], status_code: int = 200) -> Response:
    """Return a deterministic JSON response bounded by the public body limit."""
    response = JSONResponse(
        content=dict(payload),
        status_code=status_code,
        headers=_private_response_headers(),
    )
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise _http_problem(413, "response_too_large", "Response exceeds the 8 MiB limit")
    return response


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """Build a stable error envelope with no exception, path, or DSN details."""
    payload = {
        "error": {
            "code": code,
            "message": message,
            "request_id": uuid4().hex,
        }
    }
    response_headers = _private_response_headers()
    if headers is not None:
        response_headers.update(headers)
    return JSONResponse(content=payload, status_code=status_code, headers=response_headers)


def _private_response_headers() -> dict[str, str]:
    """Prevent private published data and errors from being cached by intermediaries."""
    return {
        "Cache-Control": "private, no-store",
        "Vary": "Authorization, X-API-Key",
        "X-Content-Type-Options": "nosniff",
    }


def _http_problem(status_code: int, code: str, message: str) -> HTTPException:
    """Create an internal HTTP failure that the route wrapper sanitizes."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _http_error_contract(status_code: int) -> tuple[str, str]:
    """Map HTTP status to stable public error identifiers and messages."""
    return {
        400: ("invalid_request", "Request is invalid"),
        401: ("authentication_required", "Authentication is required"),
        403: ("dataset_unavailable", "Dataset is unavailable"),
        404: ("not_found", "Published resource is unavailable"),
        409: ("conflict", "Request conflicts with the published resource"),
        410: ("snapshot_expired", "Snapshot has expired"),
        413: ("response_too_large", "Response exceeds the 8 MiB limit"),
        422: ("invalid_request", "Request validation failed"),
        429: ("rate_limited", "Request limit exceeded"),
        503: ("published_data_unavailable", "Published data service is unavailable"),
    }.get(status_code, ("request_failed", "Published data request failed"))


def _not_found() -> HTTPException:
    """Return one uniform not-found failure for unknown and cross-scope IDs."""
    return _http_problem(404, "not_found", "Published resource is unavailable")


def _service_unavailable() -> HTTPException:
    """Return a sanitized service-unavailable response for fail-closed conditions."""
    return _http_problem(503, "published_data_unavailable", "Published data service is unavailable")


__all__ = [
    "CreateSnapshotRequest",
    "CoverageResponse",
    "DataPageResponse",
    "DatasetListResponse",
    "DatasetResponse",
    "ErrorEnvelope",
    "MAX_CATALOG_ITEMS",
    "MAX_RESPONSE_BYTES",
    "SnapshotResponse",
    "close_publication_store",
    "create_snapshot",
    "get_dataset",
    "get_dataset_coverage",
    "get_dataset_contracts",
    "get_dataset_grants",
    "get_dataset_records",
    "get_publication_store",
    "get_snapshot",
    "list_datasets",
    "router",
]
