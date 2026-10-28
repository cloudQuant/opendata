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
import re
import threading
from collections.abc import Callable, Coroutine, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

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
from opendata.data.capture_selection import (
    InstrumentCaptureSelection,
    InstrumentCaptureSelectionError,
    parse_instrument_capture_selection,
    selection_profile,
    validate_selector_pair,
)
from opendata.data.coverage_evidence_registry import (
    CoverageEvidenceConfigurationError,
    DatasetCoverageEvidence,
    DatasetCoverageEvidenceRegistry,
    load_dataset_coverage_evidence_registry,
)
from opendata.data.dataset_contract_registry import (
    DatasetContractRegistry,
    canonical_sha256,
    load_dataset_contract_registry,
)
from opendata.data.market_symbol_selection import MarketSymbolSelectionError
from opendata.data.market_symbol_selection import (
    selection_profile as market_symbol_selection_profile,
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
from opendata.data.providers.sina.financial_statement_publication import (
    build_sina_fzb_observation_contract,
)
from opendata.data.providers.ths.index_daily_publication import (
    INDEX_SOURCE_ID,
    INDEX_SYMBOL_BY_DATASET_ID,
    build_index_daily_contract,
)
from opendata.pipeline.coverage_service import (
    CoverageQuery,
    InstrumentCoverageService,
    InstrumentHistoryCoverageReport,
    PublicationCoverageScope,
    PublicationObservations,
)
from opendata.pipeline.history_coverage import CoverageStatus
from opendata.pipeline.publication_store import (
    MAX_COVERAGE_RECORDS,
    MAX_PAGE_SIZE,
    CaptureDiscoveryConsistencyError,
    CaptureDiscoveryCursorError,
    CaptureDiscoveryError,
    CaptureDiscoveryLimitError,
    CaptureDiscoveryPage,
    CaptureDiscoveryRecordIntegrityError,
    CoverageObservationLimitError,
    CoverageRecordIntegrityError,
    CoverageRecordSet,
    CoverageSnapshotConsistencyError,
    PageSizeLimitError,
    PublicationContractConflictError,
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
CoverageRegistryLoader = Callable[[], DatasetCoverageEvidenceRegistry]
_ATEMPORAL_TABLE_BUSINESS_DATE_SEMANTICS = (
    "null for atemporal table records; not a historical business date"
)
_ROUTER_PREFIX = "/api/v2"
_SINA_FZB_DATASET_ID = "sina_fzb_report_observations"
_SINA_FZB_INSTRUMENT_PATTERN = re.compile(r"[0-9]{6}\.(?:SH|SZ)\Z", re.ASCII)
# Market-symbol publication reads stay unavailable until production query-plan
# evidence establishes an acceptable snapshot-creation scan budget.
_MARKET_SYMBOL_API_ENABLED = False
# Discovery stays unavailable until a covering index and production query-plan
# evidence establish a physical scan budget. This hard gate is deliberately not
# runtime-configurable so deployment settings cannot enable an unreviewed query.
_SINA_CAPTURE_DISCOVERY_ENABLED = False


def _require_capture_discovery_enabled() -> None:
    """Fail closed before resolving authorization or store dependencies."""
    if not _SINA_CAPTURE_DISCOVERY_ENABLED:
        raise _not_found()


def _require_market_symbol_api_enabled(contract: DatasetContract) -> None:
    """Hard-disable market-symbol reads until a production scan budget is reviewed."""
    try:
        profile = market_symbol_selection_profile(contract)
    except MarketSymbolSelectionError:
        raise _service_unavailable() from None
    if profile == "market_symbol_v1" and not _MARKET_SYMBOL_API_ENABLED:
        raise _not_found()


_CONFIGURED_STORE: PublicationStore | None = None
_CONFIGURED_ENGINE: Engine | None = None
_STORE_LOCK = threading.Lock()
_IDENTITY_TEMPORAL_FIELDS: dict[str, str] = {
    "business_date": "business-date bucket; null ordering last (route rejects null time values)",
    "observed_at": "source observation time",
    "source_published_at": "source publication time",
    "available_at": "platform availability time",
    "event_at": "source event time",
}
_IDENTITY_SEMANTICS: dict[str, object] = {
    "version": 1,
    "identity_key_encoding": "canonical-json-array-v1",
    "revision": "latest non-deleted revision per business_key at the pinned publication sequence",
    "temporal_fields": _IDENTITY_TEMPORAL_FIELDS,
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


class InstrumentCaptureSelectionResponse(BaseModel):
    """Exact instrument/capture pair pinned by a selected snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: str
    capture_id: str


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
    selection: InstrumentCaptureSelectionResponse | None = None


class PublishedRowResponse(BaseModel):
    """One published record with independent business and source timestamps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str
    business_key: str
    revision: StrictInt
    publication_id: str
    publication_sequence: StrictInt
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


class CaptureCandidateResponse(BaseModel):
    """One capture written by the selected publication, with its observation time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capture_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime


class CaptureCandidatePageResponse(BaseModel):
    """Bounded Sina capture candidates from one immutable publication batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: Literal["sina_fzb_report_observations"]
    publication_id: str
    instrument_id: str
    page_size: StrictInt
    items: list[CaptureCandidateResponse]
    next_cursor: str | None
    has_more: bool
    scope: Literal["publication_batch_only"]
    empty_result_semantics: Literal["no_matching_rows_in_pinned_publication"]
    observed_at_semantics: Literal["capture_observation_time_not_point_in_time"]
    pit_policy: Literal["unsupported"]


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
        (409, "Published dataset contract changed"),
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
    instrument_id: str | None = Field(default=None, min_length=1, max_length=128)
    capture_id: str | None = Field(default=None, min_length=64, max_length=64)

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
            except CaptureDiscoveryCursorError:
                return _error_response(
                    422,
                    "invalid_capture_discovery_cursor",
                    "Capture discovery cursor is invalid",
                )
            except CaptureDiscoveryLimitError:
                return _error_response(
                    413,
                    "capture_discovery_limit_exceeded",
                    "Capture discovery exceeds the supported query limit",
                )
            except (CaptureDiscoveryConsistencyError, CaptureDiscoveryRecordIntegrityError):
                return _error_response(
                    503,
                    "published_data_unavailable",
                    "Published data service is unavailable",
                )
            except CaptureDiscoveryError:
                return _error_response(
                    422,
                    "invalid_capture_discovery_request",
                    "Capture discovery request is invalid",
                )
            except PublicationContractConflictError:
                return _error_response(
                    409,
                    "publication_contract_conflict",
                    "Published dataset contract changed",
                )
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


def get_dataset_coverage_evidence_registry() -> DatasetCoverageEvidenceRegistry:
    """Load optional exact-pinned coverage evidence from an operator-owned file."""
    path = _server_setting("PUBLISHED_DATASET_COVERAGE_EVIDENCE_FILE")
    if path is None or not path.strip():
        return DatasetCoverageEvidenceRegistry.empty()
    try:
        return load_dataset_coverage_evidence_registry(path)
    except CoverageEvidenceConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Published data service is unavailable",
        ) from None


def get_dataset_coverage_evidence_registry_loader() -> CoverageRegistryLoader:
    """Return a lazy evidence loader so authorization runs before file access."""
    return get_dataset_coverage_evidence_registry


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
    instrument_id: str | None = Query(default=None, min_length=1, max_length=128),
    capture_id: str | None = Query(default=None, min_length=64, max_length=64),
) -> Response:
    """Read a fixed snapshot page, creating the snapshot on the first request."""
    _reject_unknown_query(
        request,
        {
            "start_date",
            "end_date",
            "page_size",
            "publication_id",
            "snapshot_id",
            "cursor",
            "instrument_id",
            "capture_id",
        },
    )
    _validate_hex_id(publication_id, "publication_id")
    _validate_hex_id(snapshot_id, "snapshot_id")
    _validate_selection_families(instrument_id=instrument_id, capture_id=capture_id)
    selector_was_supplied = (
        "instrument_id" in request.query_params or "capture_id" in request.query_params
    )
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
        _require_market_symbol_api_enabled(contract)
        selection = _request_selection(
            contract,
            instrument_id=instrument_id,
            capture_id=capture_id,
            required=True,
            was_supplied=selector_was_supplied,
        )
        _require_supported_record_contract(contract, start_date, end_date)
        snapshot = await asyncio.to_thread(
            store.create_snapshot,
            dataset_id,
            publication_id=publication.publication_id,
            start_date=start_date,
            end_date=end_date,
            page_size=page_size or 100,
            access_scope=data_access.binding_sha256,
            instrument_id=selection.instrument_id if selection is not None else None,
            capture_id=selection.capture_id if selection is not None else None,
        )
        publication = await _publication_for_snapshot(store, snapshot, data_access)
        contract, contract_hash = _resolve_contract(contracts, data_access, publication)
        persisted_selection = _snapshot_selection(contract, snapshot)
        if persisted_selection != selection:
            raise _service_unavailable()
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
        _require_market_symbol_api_enabled(contract)
        selection = _validate_snapshot_selection_request(
            contract,
            snapshot,
            instrument_id=instrument_id,
            capture_id=capture_id,
            was_supplied=selector_was_supplied,
        )
        _require_supported_record_contract(contract, snapshot.start_date, snapshot.end_date)
        page = await asyncio.to_thread(
            store.read_page,
            snapshot.snapshot_id,
            cursor=cursor,
            access_scope=data_access.binding_sha256,
        )

    _validate_published_records(contract, page.records)
    _validate_rows_match_selection(selection, page.records)
    return _success_response(
        _page_payload(
            publication, contract, contract_hash, page.snapshot, page.records, page.next_cursor
        )
    )


@router.get(
    "/datasets/{dataset_id}/capture-candidates",
    response_model=CaptureCandidatePageResponse,
    responses=_ERROR_RESPONSES,
)
async def list_sina_capture_candidates(
    dataset_id: str,
    request: Request,
    *,
    capture_discovery_gate: None = Depends(_require_capture_discovery_enabled),
    principal: CurrentPrincipal,
    grants: DatasetGrantRegistry = Depends(get_dataset_grants),
    contracts: DatasetContractRegistry = Depends(get_dataset_contracts),
    store: PublicationStore = Depends(get_publication_store),
    publication_id: str | None = Query(default=None, min_length=32, max_length=32),
    instrument_id: str | None = Query(default=None, min_length=1, max_length=128),
    page_size: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None, max_length=8_192),
) -> Response:
    """List captures written by one pinned Sina FZB publication batch.

    The result is deliberately scoped to records written by ``publication_id``.
    An empty page does not establish that the instrument has no captures in
    earlier publications. ``observed_at`` is the capture observation time and
    does not imply point-in-time availability of the source information.
    """
    del capture_discovery_gate
    _reject_unknown_query(
        request,
        {"publication_id", "instrument_id", "page_size", "cursor"},
    )
    _validate_hex_id(publication_id, "publication_id")
    if publication_id is None or instrument_id is None:
        raise _http_problem(
            422,
            "invalid_request",
            "publication_id and instrument_id are required",
        )
    if not _SINA_FZB_INSTRUMENT_PATTERN.fullmatch(instrument_id):
        raise _http_problem(422, "invalid_selection", "Instrument selection is invalid")

    if dataset_id != _SINA_FZB_DATASET_ID:
        raise _not_found()
    access = _require_access(principal, dataset_id, "data.read", grants)
    publication = await _publication_for_request(store, dataset_id, publication_id, access)
    contract, contract_hash = _resolve_contract(contracts, access, publication)
    _require_sina_fzb_observation_contract(contract)

    page = await asyncio.to_thread(
        store.list_capture_discovery,
        dataset_id,
        publication.publication_id,
        expected_publication=publication,
        expected_contract_sha256=contract_hash,
        instrument_id=instrument_id,
        page_size=page_size,
        cursor=cursor,
        access_scope=access.binding_sha256,
    )
    return _success_response(
        _capture_candidate_page_payload(
            dataset_id=dataset_id,
            publication_id=publication.publication_id,
            instrument_id=instrument_id,
            page_size=page_size,
            page=page,
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
    _validate_selection_families(
        instrument_id=body.instrument_id,
        capture_id=body.capture_id,
    )
    selector_was_supplied = bool(
        {"instrument_id", "capture_id"}.intersection(body.model_fields_set)
    )
    context = _require_access(principal, body.dataset_id, "snapshot.create", grants)
    publication = await _publication_for_request(
        store, body.dataset_id, body.publication_id, context
    )
    contract, contract_hash = _resolve_contract(contracts, context, publication)
    _require_market_symbol_api_enabled(contract)
    selection = _request_selection(
        contract,
        instrument_id=body.instrument_id,
        capture_id=body.capture_id,
        required=True,
        was_supplied=selector_was_supplied,
    )
    _require_supported_record_contract(contract, body.start_date, body.end_date)
    snapshot = await asyncio.to_thread(
        store.create_snapshot,
        body.dataset_id,
        publication_id=publication.publication_id,
        start_date=body.start_date,
        end_date=body.end_date,
        page_size=body.page_size,
        access_scope=context.binding_sha256,
        instrument_id=selection.instrument_id if selection is not None else None,
        capture_id=selection.capture_id if selection is not None else None,
    )
    publication = await _publication_for_snapshot(store, snapshot, context)
    contract, contract_hash = _resolve_contract(contracts, context, publication)
    if _snapshot_selection(contract, snapshot) != selection:
        raise _service_unavailable()
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
    _require_market_symbol_api_enabled(contract)
    _snapshot_selection(contract, snapshot)
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
    evidence_registry_loader: CoverageRegistryLoader = Depends(
        get_dataset_coverage_evidence_registry_loader
    ),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    publication_id: str | None = Query(default=None, min_length=32, max_length=32),
) -> Response:
    """Return evidence-bound coverage for an exact, pinned single-index series."""
    _reject_unknown_query(request, {"start_date", "end_date", "publication_id"})
    _validate_hex_id(publication_id, "publication_id")
    context = _require_access(principal, dataset_id, "coverage.read", grants)
    evidence_registry = evidence_registry_loader()
    publication = await _publication_for_request(store, dataset_id, publication_id, context)
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
    evidence = evidence_registry.resolve(
        dataset_id, publication.source_id, publication.publication_id
    )
    requested = _coverage_requested_range(start_date, end_date)
    if evidence is None:
        coverage = _unknown_coverage(
            requested,
            "No independently verified coverage evidence is registered for this exact publication.",
        )
    elif evidence.visible_record_set is None:
        coverage = _unknown_coverage(
            requested,
            "Schema 1 coverage evidence does not pin the complete visible publication record set.",
            evidence_sha256=evidence.evidence_sha256,
        )
    elif evidence.visible_record_set.contract_sha256 != contract_hash:
        coverage = _unknown_coverage(
            requested,
            "The operator record-set pin does not match the published contract.",
            evidence_sha256=evidence.evidence_sha256,
        )
    elif not _is_trusted_single_index_contract(contract, evidence):
        coverage = _unknown_coverage(
            requested,
            "The published contract or instrument identity is outside the trusted single-index "
            "coverage scope.",
            evidence_sha256=evidence.evidence_sha256,
        )
    else:
        lifecycle = evidence.instrument_registry.resolve(
            dataset_id,
            publication.source_id,
            evidence.instrument_identifier,
        ).lifecycle
        query_start = start_date or lifecycle.known_start_date or evidence.publication_scope_start
        query_end = end_date or evidence.publication_scope_end_exclusive
        requested = _coverage_requested_range(query_start, query_end)
        today = _shanghai_business_date()
        latest_allowed_end = _latest_coverage_end_exclusive(today)
        if (
            query_start > today
            or query_end > latest_allowed_end
            or lifecycle.status_as_of > today
            or evidence.publication_scope_start > today
            or evidence.publication_scope_end_exclusive > latest_allowed_end
        ):
            return _success_response(
                {
                    "dataset_id": dataset_id,
                    "publication_id": publication.publication_id,
                    "contract_sha256": contract_hash,
                    "coverage": _unknown_coverage(
                        requested,
                        "Future lifecycle status, scan scope, or requested business dates cannot "
                        "establish historical coverage.",
                        evidence_sha256=evidence.evidence_sha256,
                    ),
                }
            )
        try:
            query = CoverageQuery(
                dataset_id=dataset_id,
                source_id=publication.source_id,
                instrument_identifier=evidence.instrument_identifier,
                start=query_start,
                end_exclusive=query_end,
            )
        except (TypeError, ValueError):
            coverage = _unknown_coverage(
                requested,
                "Registered lifecycle and publication scope do not define a valid coverage "
                "interval.",
                evidence_sha256=evidence.evidence_sha256,
            )
        else:
            try:
                record_set = await asyncio.to_thread(
                    store.read_coverage_record_set,
                    dataset_id,
                    publication.publication_id,
                    contract_sha256=contract_hash,
                    limit=MAX_COVERAGE_RECORDS,
                )
            except (
                CoverageObservationLimitError,
                CoverageRecordIntegrityError,
                CoverageSnapshotConsistencyError,
            ):
                coverage = _unknown_coverage(
                    requested,
                    "The pinned publication record set could not be verified completely; no "
                    "truncated or inconsistent result was evaluated.",
                    evidence_sha256=evidence.evidence_sha256,
                )
            else:
                if record_set is None:
                    raise _service_unavailable()
                if not _coverage_record_set_matches_pin(
                    record_set, publication, evidence, contract_hash
                ):
                    coverage = _unknown_coverage(
                        requested,
                        "The complete visible record set does not match the prior operator pin.",
                        evidence_sha256=evidence.evidence_sha256,
                    )
                elif not _coverage_facts_match_single_index(contract, evidence, record_set.records):
                    coverage = _unknown_coverage(
                        requested,
                        "The pinned publication contains malformed facts or a symbol outside "
                        "the single-index scope.",
                        evidence_sha256=evidence.evidence_sha256,
                    )
                else:
                    observed_dates = tuple(
                        record.business_date
                        for record in record_set.records
                        if record.business_date is not None
                    )
                    if any(
                        day < evidence.publication_scope_start
                        or day >= evidence.publication_scope_end_exclusive
                        or day > today
                        or day == date.max
                        for day in observed_dates
                    ):
                        coverage = _unknown_coverage(
                            requested,
                            "The pinned publication contains observations outside its "
                            "independently evidenced scan scope.",
                            evidence_sha256=evidence.evidence_sha256,
                        )
                        return _success_response(
                            {
                                "dataset_id": dataset_id,
                                "publication_id": publication.publication_id,
                                "contract_sha256": contract_hash,
                                "coverage": coverage,
                            }
                        )
                    publication_observations = PublicationObservations(
                        dataset_id=dataset_id,
                        source_id=publication.source_id,
                        instrument_id=evidence.instrument_identifier,
                        publication_id=publication.publication_id,
                        publication_version=str(publication.sequence),
                        evidence_ref=f"publication:{publication.publication_id}",
                        scope=PublicationCoverageScope.BUSINESS_HISTORY,
                        observed_dates=tuple(day for day in observed_dates if day is not None),
                        scope_start=evidence.publication_scope_start,
                        scope_end_exclusive=evidence.publication_scope_end_exclusive,
                        scope_evidence_ref=evidence.publication_scope_evidence_ref,
                    )
                    report = InstrumentCoverageService(evidence.instrument_registry).evaluate(
                        query,
                        calendar=evidence.calendar,
                        publication=publication_observations,
                    )
                    coverage = _coverage_from_report(
                        report,
                        requested,
                        observed_dates=tuple(day for day in observed_dates if day is not None),
                        evidence_sha256=evidence.evidence_sha256,
                        record_set=record_set,
                    )
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


def _coverage_requested_range(
    start: date | None, end_exclusive: date | None
) -> dict[str, object] | None:
    """Serialize one optional half-open date range for the v2 coverage response."""
    if start is None or end_exclusive is None:
        return None
    return {"start": start.isoformat(), "end": end_exclusive.isoformat()}


def _shanghai_business_date() -> date:
    """Return today's date in the market's declared Asia/Shanghai business timezone."""
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _latest_coverage_end_exclusive(today: date) -> date:
    """Allow today's observations in a half-open scan window without overflowing date.max."""
    try:
        return today + timedelta(days=1)
    except OverflowError:
        return date.max


def _unknown_coverage(
    requested: dict[str, object] | None,
    limitation: str,
    *,
    evidence_sha256: str | None = None,
) -> dict[str, object]:
    """Build a fail-closed response when evidence cannot support evaluation."""
    evidence_refs = (
        [f"operator-attestation-sha256:{evidence_sha256}"] if evidence_sha256 is not None else []
    )
    return {
        "status": "unknown",
        "scope": "unknown",
        "requested": requested,
        "observed": None,
        "missing_ranges": [],
        "evidence_refs": evidence_refs,
        "limitation": limitation,
    }


def _is_trusted_single_index_contract(
    contract: DatasetContract, evidence: DatasetCoverageEvidence
) -> bool:
    """Require the exact whitelisted THS single-symbol contract before scanning."""
    expected_symbol = INDEX_SYMBOL_BY_DATASET_ID.get(contract.dataset_id)
    if (
        expected_symbol is None
        or evidence.dataset_id != contract.dataset_id
        or evidence.source_id != INDEX_SOURCE_ID
        or evidence.instrument_identifier != expected_symbol
        or contract.source.source_id != INDEX_SOURCE_ID
    ):
        return False
    try:
        trusted_contract = build_index_daily_contract(expected_symbol)
        return canonical_sha256(contract) == canonical_sha256(trusted_contract)
    except (TypeError, ValueError):
        return False


def _coverage_facts_match_single_index(
    contract: DatasetContract,
    evidence: DatasetCoverageEvidence,
    records: tuple[PublishedRecord, ...],
) -> bool:
    """Validate every visible fact and require the complete whitelisted symbol scope."""
    try:
        _validate_published_records(contract, records)
    except HTTPException:
        return False
    return all(
        record.business_date is not None
        and record.payload.get("symbol") == evidence.instrument_identifier
        for record in records
    )


def _coverage_record_set_matches_pin(
    record_set: CoverageRecordSet,
    publication: PublicationRecord,
    evidence: DatasetCoverageEvidence,
    contract_sha256: str,
) -> bool:
    """Require exact agreement among sidecar, contract, publication, and verified scan."""
    pin = evidence.visible_record_set
    if pin is None:
        return False
    return (
        record_set.dataset_id == publication.dataset_id == evidence.dataset_id
        and record_set.source_id == publication.source_id == evidence.source_id
        and record_set.publication_id == publication.publication_id == evidence.publication_id
        and record_set.publication_sequence == publication.sequence == pin.publication_sequence
        and record_set.schema_version == publication.schema_version
        and record_set.semantic_version == publication.semantic_version
        and record_set.unit == publication.unit
        and record_set.adjustment_basis == publication.adjustment_basis
        and record_set.contract_sha256 == contract_sha256 == pin.contract_sha256
        and record_set.algorithm == pin.algorithm
        and record_set.sha256 == pin.sha256
        and record_set.record_count == pin.record_count == len(record_set.records)
    )


def _coverage_from_report(
    report: InstrumentHistoryCoverageReport,
    requested: dict[str, object] | None,
    *,
    observed_dates: tuple[date, ...],
    evidence_sha256: str,
    record_set: CoverageRecordSet,
) -> dict[str, object]:
    """Map an instrument-history report into the stable v2 coverage DTO."""
    coverage_report = report.coverage
    status = report.status
    observed_dates = tuple(
        day
        for day in observed_dates
        if report.requested_start <= day < report.requested_end_exclusive
    )
    observed_range: dict[str, str] | None = None
    if observed_dates:
        observed_range = {
            "start": min(observed_dates).isoformat(),
            "end": (max(observed_dates) + timedelta(days=1)).isoformat(),
        }
    missing_dates = tuple(
        sorted(
            set(
                coverage_report.head_gaps
                + coverage_report.interior_gaps
                + coverage_report.tail_gaps
            )
        )
    )
    missing_ranges = _coalesce_daily_gaps(missing_dates)
    limitation = None
    if status is not CoverageStatus.COMPLETE:
        reasons = report.reasons or coverage_report.reasons
        limitation = " ".join(reasons)[:1024] or "Coverage evidence is incomplete."
    evidence_refs = [
        report.registry_evidence_ref,
        report.lifecycle_status_evidence_ref,
        report.start_evidence_ref,
        report.calendar_evidence_ref,
        report.publication_evidence_ref,
        report.publication_scope_evidence_ref,
        f"operator-attestation-sha256:{evidence_sha256}",
        f"{record_set.algorithm}:sha256:{record_set.sha256}",
        f"publication-sequence:{record_set.publication_sequence}",
        f"visible-record-count:{record_set.record_count}",
        f"contract-sha256:{record_set.contract_sha256}",
    ]
    return {
        "status": status.value.lower(),
        "scope": (
            "not_applicable" if status is CoverageStatus.NOT_APPLICABLE else "business_history"
        ),
        "requested": requested,
        "observed": observed_range,
        "missing_ranges": missing_ranges,
        "evidence_refs": list(dict.fromkeys(ref for ref in evidence_refs if ref is not None)),
        "limitation": limitation,
    }


def _coalesce_daily_gaps(values: tuple[date, ...]) -> list[dict[str, str]]:
    """Combine adjacent missing calendar dates into half-open response ranges."""
    if not values:
        return []
    ranges: list[dict[str, str]] = []
    start = previous = values[0]
    for day in values[1:]:
        if day == previous + timedelta(days=1):
            previous = day
            continue
        ranges.append(
            {"start": start.isoformat(), "end": (previous + timedelta(days=1)).isoformat()}
        )
        start = previous = day
    ranges.append({"start": start.isoformat(), "end": (previous + timedelta(days=1)).isoformat()})
    return ranges


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
    try:
        selection_profile(contract)
        market_profile = market_symbol_selection_profile(contract)
    except InstrumentCaptureSelectionError:
        raise _service_unavailable() from None
    except MarketSymbolSelectionError:
        raise _service_unavailable() from None
    if market_profile == "market_symbol_v1" and _is_sina_contract(contract):
        raise _service_unavailable()
    _quality_receipt(publication)
    return contract, contract_hash


def _require_sina_fzb_observation_contract(contract: DatasetContract) -> None:
    """Keep capture discovery confined to the exact source-native Sina FZB contract."""
    try:
        expected = build_sina_fzb_observation_contract()
        contract_matches = canonical_sha256(contract) == canonical_sha256(expected)
    except (TypeError, ValueError):
        raise _service_unavailable() from None
    if contract.dataset_id != _SINA_FZB_DATASET_ID or not contract_matches:
        raise _service_unavailable()


def _capture_candidate_page_payload(
    *,
    dataset_id: str,
    publication_id: str,
    instrument_id: str,
    page_size: int,
    page: CaptureDiscoveryPage,
) -> dict[str, object]:
    """Validate the store page before returning its exact publication scope to a client."""
    if type(page) is not CaptureDiscoveryPage:
        raise _service_unavailable()
    items = page.items
    next_cursor = page.next_cursor
    if (
        not isinstance(items, tuple)
        or len(items) > page_size
        or (
            next_cursor is not None
            and (not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 8_192)
        )
    ):
        raise _service_unavailable()

    response_items: list[dict[str, object]] = []
    seen_capture_ids: set[str] = set()
    previous_key: tuple[datetime, str] | None = None
    for item in items:
        capture_id = item.capture_id
        observed_at = item.observed_at
        if (
            not isinstance(capture_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", capture_id, re.ASCII) is None
            or capture_id in seen_capture_ids
            or type(observed_at) is not datetime
            or observed_at.tzinfo is None
        ):
            raise _service_unavailable()
        try:
            offset = observed_at.utcoffset()
            normalized_observed_at = observed_at.astimezone(timezone.utc)
        except Exception:
            raise _service_unavailable() from None
        if type(offset) is not timedelta or offset != timedelta(0):
            raise _service_unavailable()
        ordering_key = (normalized_observed_at, capture_id)
        if previous_key is not None and (
            previous_key[0] < ordering_key[0]
            or (previous_key[0] == ordering_key[0] and previous_key[1] >= ordering_key[1])
        ):
            raise _service_unavailable()
        seen_capture_ids.add(capture_id)
        previous_key = ordering_key
        response_items.append({"capture_id": capture_id, "observed_at": _iso(observed_at)})

    return {
        "dataset_id": dataset_id,
        "publication_id": publication_id,
        "instrument_id": instrument_id,
        "page_size": page_size,
        "items": response_items,
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
        "scope": "publication_batch_only",
        "empty_result_semantics": "no_matching_rows_in_pinned_publication",
        "observed_at_semantics": "capture_observation_time_not_point_in_time",
        "pit_policy": "unsupported",
    }


def _validate_selection_families(
    *,
    instrument_id: object | None,
    capture_id: object | None,
) -> None:
    """Reject malformed instrument/capture pairs before store access."""
    try:
        validate_selector_pair(instrument_id=instrument_id, capture_id=capture_id)
    except InstrumentCaptureSelectionError:
        raise _http_problem(
            422, "invalid_selection", "Instrument/capture selection is invalid"
        ) from None


def _request_selection(
    contract: DatasetContract,
    *,
    instrument_id: object | None,
    capture_id: object | None,
    required: bool,
    was_supplied: bool,
) -> InstrumentCaptureSelection | None:
    """Parse request selectors against the exact reviewed contract profile."""
    try:
        selection = parse_instrument_capture_selection(
            contract,
            instrument_id=instrument_id,
            capture_id=capture_id,
            required=required,
        )
    except InstrumentCaptureSelectionError:
        raise _http_problem(
            422, "invalid_selection", "Instrument/capture selection is invalid"
        ) from None
    if was_supplied and selection is None:
        raise _http_problem(422, "invalid_selection", "Instrument/capture selection is invalid")
    return selection


def _snapshot_selection(
    contract: DatasetContract, snapshot: SnapshotRecord
) -> InstrumentCaptureSelection | None:
    """Validate persisted selectors against the pinned publication contract."""
    try:
        return parse_instrument_capture_selection(
            contract,
            instrument_id=snapshot.instrument_id,
            capture_id=snapshot.capture_id,
            required=True,
        )
    except InstrumentCaptureSelectionError:
        raise _service_unavailable() from None


def _validate_snapshot_selection_request(
    contract: DatasetContract,
    snapshot: SnapshotRecord,
    *,
    instrument_id: str | None,
    capture_id: str | None,
    was_supplied: bool,
) -> InstrumentCaptureSelection | None:
    """Use the stored snapshot scope and reject any changed repeated selector."""
    persisted = _snapshot_selection(contract, snapshot)
    requested = _request_selection(
        contract,
        instrument_id=instrument_id,
        capture_id=capture_id,
        required=False,
        was_supplied=was_supplied,
    )
    if was_supplied and requested != persisted:
        raise _http_problem(422, "invalid_selection", "Instrument/capture selection is invalid")
    return persisted


def _validate_rows_match_selection(
    selection: InstrumentCaptureSelection | None,
    records: tuple[PublishedRecord, ...],
) -> None:
    """Fail closed if any returned row contradicts its persisted capture selector."""
    if selection is None:
        return
    for record in records:
        if (
            record.payload.get("instrument_id") != selection.instrument_id
            or record.payload.get("capture_id") != selection.capture_id
        ):
            raise _service_unavailable()


def _is_sina_contract(contract: DatasetContract) -> bool:
    """Recognize the source identity that must remain capture-pair only."""
    return (
        contract.dataset_id == _SINA_FZB_DATASET_ID
        or contract.source.source_id.casefold() == "sina-fzb"
        or contract.source.source.casefold() == "sina"
    )


def _require_supported_record_contract(
    contract: DatasetContract,
    start_date: date | None,
    end_date: date | None,
) -> None:
    """Reject request semantics the current date-window store cannot guarantee."""
    if contract.record_kind == "table":
        if contract.temporal_semantics != "none" or contract.time_field is not None:
            raise _http_problem(
                422,
                "unsupported_time_semantics",
                "Table reads require atemporal records without a time field",
            )
        if start_date is not None or end_date is not None:
            raise _http_problem(
                422,
                "date_bounds_unsupported",
                "Atemporal table reads do not accept business-date bounds",
            )
        if contract.pit_policy != "unsupported":
            raise _http_problem(
                422,
                "unsupported_point_in_time_policy",
                "Atemporal table reads do not provide point-in-time selection",
            )
        if contract.ordering != contract.business_key:
            raise _http_problem(
                422,
                "unsupported_ordering",
                "Atemporal table reads require ordering by business key fields",
            )
        field_by_name = {field.name: field for field in contract.fields}
        if any(
            field_by_name[name].data_type not in {"string", "date"}
            for name in contract.business_key
        ):
            raise _http_problem(
                422,
                "unsupported_business_key",
                "This API supports string and date business key fields only",
            )
        return

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
        if contract.record_kind == "table":
            if (
                contract.temporal_semantics != "none"
                or contract.time_field is not None
                or record.business_date is not None
            ):
                raise _service_unavailable()
            continue
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
    temporal_fields = dict(_IDENTITY_TEMPORAL_FIELDS)
    if contract.record_kind == "table" and contract.temporal_semantics == "none":
        temporal_fields["business_date"] = _ATEMPORAL_TABLE_BUSINESS_DATE_SEMANTICS
    identity_semantics = {
        **_IDENTITY_SEMANTICS,
        "temporal_fields": temporal_fields,
        "business_key_fields": list(contract.business_key),
        "ordering_fields": list(contract.ordering),
        "identity_key_encoding": "canonical-json-array-v1",
        "storage_order": storage_order,
        "temporal_semantics": contract.temporal_semantics,
        "time_field": contract.time_field,
    }
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
        "identity_semantics": identity_semantics,
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
    payload: dict[str, object] = {
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
    capture_selection = _snapshot_selection(contract, snapshot)
    if capture_selection is not None:
        payload["selection"] = {
            "instrument_id": capture_selection.instrument_id,
            "capture_id": capture_selection.capture_id,
        }
    return payload


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
            "publication_sequence": record.publication_sequence,
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
    "CaptureCandidatePageResponse",
    "CaptureCandidateResponse",
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
    "get_dataset_coverage_evidence_registry",
    "get_dataset_coverage_evidence_registry_loader",
    "get_dataset_contracts",
    "get_dataset_grants",
    "get_dataset_records",
    "get_publication_store",
    "get_snapshot",
    "list_sina_capture_candidates",
    "list_datasets",
    "router",
]
