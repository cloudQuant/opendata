"""Admin API for submitting and observing explicitly reviewed pipeline runs.

The router only uses the runtime installed by a reviewed application factory.
It does not construct engines, discover settings, load job paths, or accept
execution instructions from request data.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Annotated, Literal, NoReturn, Protocol
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from starlette.concurrency import run_in_threadpool

from opendata.api.dependencies import (
    CurrentAdmin,  # noqa: TC001 - FastAPI needs Annotated Depends metadata.
)
from opendata.pipeline.run_dispatch import DispatchReceipt, ReviewedTaskCatalog, RunDispatcher
from opendata.pipeline.run_identity import (
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    IdempotencyToken,
    Identifier,
    RunRequest,
    ShardIdentifier,
    TaskDefinition,
)
from opendata.pipeline.run_ledger import RunLedger, RunLedgerConflictError, RunRecord
from opendata.pipeline.runtime_roles import RuntimeRolePlan

if TYPE_CHECKING:
    from collections.abc import Mapping

_MAX_REQUEST_BODY_BYTES = 64 * 1024
_RUN_KEY = re.compile(r"^[0-9a-f]{64}$")
_RUN_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})
_EXECUTION_FIELDS = frozenset(
    {
        "command",
        "dsn",
        "entrypoint",
        "exec",
        "function",
        "importpath",
        "module",
        "pythonpath",
        "script",
        "shell",
        "sql",
    }
)
_CONNECTION_SELECTOR_ROOTS = (
    "connection",
    "database",
    "db",
    "conn",
    "jdbc",
    "odbc",
    "sqlalchemy",
    "mysql",
    "postgres",
    "postgresql",
    "sqlite",
    "mariadb",
    "mssql",
    "oracle",
)
_CONNECTION_SELECTOR_SUFFIXES = (
    "url",
    "uri",
    "dsn",
    "connectionstring",
    "connstring",
    "connectstring",
    "connstr",
    "string",
)
_QUERY_SENSITIVE_KEY_PARTS = (
    "accesskey",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "token",
    "pwd",
)
_URI_CANDIDATE = re.compile(r"(?i)(?<![A-Z0-9+.-])(?=([A-Z][A-Z0-9+.-]*://[^\s\"'<>;,)}\]]+))")
_JDBC_CONNECTION_PREFIX = re.compile(r"(?i)(?<![A-Z0-9])jdbc:[A-Z][A-Z0-9+.-]*:")
_DSN_VALUE = r"(?:\{[^}]*\}|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^;,&\s]+)"
_PASSWORD_ASSIGNMENT = re.compile(
    rf"(?i)(?<![A-Z0-9])(?:pwd|password|passwd|passphrase)\s*=\s*{_DSN_VALUE}"
)
_DSN_CONTEXT_ASSIGNMENT = re.compile(
    rf"(?i)(?:^|[;,\s])(?:driver|dsn|file\s*dsn|server|server\s*name|provider)\s*=\s*{_DSN_VALUE}"
)
_DSN_USER_ASSIGNMENT = re.compile(
    rf"(?i)(?:^|[;,\s])(?:uid|user(?:\s*(?:id|name))?)\s*=\s*{_DSN_VALUE}"
)


class ReviewedRuntimeAccess(Protocol):
    """The runtime fields consumed by this router after exact-type validation."""

    @property
    def closed(self) -> bool:
        """Whether the owning application has deactivated the runtime."""
        ...

    @property
    def runtime_plan(self) -> RuntimeRolePlan:
        """The immutable role plan installed by the application factory."""
        ...

    @property
    def catalog(self) -> ReviewedTaskCatalog:
        """The immutable reviewed job catalog."""
        ...

    @property
    def dispatcher(self) -> RunDispatcher:
        """The dispatcher bound to the runtime's reviewed ledger."""
        ...

    @property
    def ledger(self) -> RunLedger:
        """The runtime's explicit persistent run ledger."""
        ...


class ManualRunBody(BaseModel):
    """Strict public shape for one manual reviewed-run request."""

    model_config = ConfigDict(extra="forbid", strict=True)

    job_id: Identifier
    version: Identifier
    idempotency_key: IdempotencyToken
    query: dict[str, JsonValue] = Field(default_factory=dict)
    partition: dict[str, JsonValue] = Field(default_factory=dict)
    shard: ShardIdentifier | None = None


class APIError(BaseModel):
    """Stable non-sensitive error envelope used by the reviewed-run routes."""

    detail: str


class ReviewedJobSummary(BaseModel):
    """Allowlisted descriptive fields for one reviewed job version."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    version: str
    dataset_id: str
    provider: str
    model: str
    enabled: bool
    reviewed: Literal[True]


class ReviewedJobsResponse(BaseModel):
    """Response containing the reviewed job directory."""

    jobs: list[ReviewedJobSummary]


class AcceptedRunResponse(BaseModel):
    """Bounded receipt returned when a manual run is submitted or replayed."""

    run_key: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    job_id: str
    version: str
    created_at: datetime


class RunStatusResponse(BaseModel):
    """Public status fields for a run without its snapshot or lease metadata."""

    run_key: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    failure_code: Literal["run_failed"] | None


class CancelRunResponse(BaseModel):
    """Immediate cancellation result and its current terminal outcome."""

    run: RunStatusResponse
    outcome: Literal["cancelled", "terminal"]


_MANUAL_REQUEST_SCHEMA = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["job_id", "version", "idempotency_key"],
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$",
                        },
                        "version": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$",
                        },
                        "idempotency_key": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,255}$",
                        },
                        "query": {"type": "object", "default": {}, "additionalProperties": True},
                        "partition": {
                            "type": "object",
                            "default": {},
                            "additionalProperties": True,
                        },
                        "shard": {
                            "type": ["string", "null"],
                            "minLength": 1,
                            "maxLength": 128,
                            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$",
                            "default": None,
                        },
                    },
                }
            }
        },
    }
}

_ERROR_RESPONSE = {
    "model": APIError,
    "description": "A fixed error code without exception or request details.",
}


router = APIRouter(prefix="/reviewed")


def get_reviewed_runtime(request: Request) -> ReviewedRuntimeAccess:
    """Return the exact reviewed runtime installed by the application factory.

    Missing, incompatible, or API-disabled runtime state fails closed. This
    dependency deliberately imports the runtime class only when requested so
    other application paths do not need to activate reviewed mode.
    """
    try:
        from opendata.pipeline.reviewed_runtime import ReviewedRuntime

        runtime = getattr(request.app.state, "reviewed_runtime", None)
        if type(runtime) is not ReviewedRuntime:
            raise RuntimeError("reviewed runtime is unavailable")
        if runtime.closed:
            raise RuntimeError("reviewed runtime is closed")
        plan = runtime.runtime_plan
        if (
            not isinstance(plan, RuntimeRolePlan)
            or plan.profile != "reviewed"
            or not plan.enables("api")
        ):
            raise RuntimeError("reviewed API role is unavailable")
        if (
            not isinstance(runtime.catalog, ReviewedTaskCatalog)
            or not isinstance(runtime.dispatcher, RunDispatcher)
            or not isinstance(runtime.ledger, RunLedger)
        ):
            raise RuntimeError("reviewed runtime components are unavailable")
        return runtime
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="reviewed_runtime_unavailable",
        ) from None


@router.get(
    "/jobs",
    response_model=ReviewedJobsResponse,
    responses={
        401: {"model": APIError, "description": "Authentication required"},
        403: {"model": APIError, "description": "CurrentAdmin required"},
        503: _ERROR_RESPONSE,
    },
)
def list_reviewed_jobs(
    _current_admin: CurrentAdmin,
    runtime: Annotated[ReviewedRuntimeAccess, Depends(get_reviewed_runtime)],
) -> ReviewedJobsResponse:
    """List only the safe declarative fields of reviewed jobs."""
    try:
        jobs = sorted(runtime.catalog.tasks(), key=lambda task: (task.job_id, task.version))
        return ReviewedJobsResponse(jobs=[_job_summary(task) for task in jobs])
    except Exception:
        raise _service_unavailable() from None


@router.post(
    "/runs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedRunResponse,
    openapi_extra=_MANUAL_REQUEST_SCHEMA,
    responses={
        401: {"model": APIError, "description": "Authentication required"},
        403: {"model": APIError, "description": "CurrentAdmin required"},
        404: {"model": APIError, "description": "job_not_found"},
        409: {"model": APIError, "description": "job_not_runnable or idempotency_conflict"},
        413: {"model": APIError, "description": "request_too_large"},
        415: {"model": APIError, "description": "json_required"},
        422: {"model": APIError, "description": "invalid_request"},
        503: _ERROR_RESPONSE,
    },
)
async def submit_reviewed_run(
    request: Request,
    _current_admin: CurrentAdmin,
    runtime: Annotated[ReviewedRuntimeAccess, Depends(get_reviewed_runtime)],
) -> AcceptedRunResponse:
    """Validate and enqueue one manual run through the reviewed dispatcher."""
    body = await _read_manual_body(request)
    task = runtime.catalog.resolve(body.job_id, body.version)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job_not_found")
    if not task.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="job_not_runnable")

    try:
        _reject_unsafe_parameters(body.query)
        _reject_unsafe_parameters(body.partition)
        # Exercise the public identity contract before any ledger write. The
        # dispatcher repeats this validation at its own trust boundary.
        RunRequest.model_validate(
            {
                "run_type": "manual",
                "idempotency_key": body.idempotency_key,
                "query": body.query,
                "partition": body.partition,
                "shard": body.shard,
            }
        )
    except (ValidationError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_request",
        ) from None

    payload: dict[str, object] = {
        "idempotency_key": body.idempotency_key,
        "query": body.query,
        "partition": body.partition,
        "shard": body.shard,
    }
    try:
        receipt = await run_in_threadpool(
            runtime.dispatcher.dispatch_manual,
            body.job_id,
            body.version,
            payload,
        )
    except RunLedgerConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="idempotency_conflict",
        ) from None
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_request",
        ) from None
    except Exception:
        raise _service_unavailable() from None

    try:
        return _accepted_receipt(receipt)
    except Exception:
        raise _service_unavailable() from None


@router.get(
    "/runs/{run_key}",
    response_model=RunStatusResponse,
    responses={
        401: {"model": APIError, "description": "Authentication required"},
        403: {"model": APIError, "description": "CurrentAdmin required"},
        404: {"model": APIError, "description": "run_not_found"},
        503: _ERROR_RESPONSE,
    },
)
def get_reviewed_run(
    run_key: str,
    _current_admin: CurrentAdmin,
    runtime: Annotated[ReviewedRuntimeAccess, Depends(get_reviewed_runtime)],
) -> RunStatusResponse:
    """Return a redacted public status for one run identity."""
    _require_run_key(run_key)
    try:
        record = runtime.ledger.get(run_key)
    except Exception:
        raise _service_unavailable() from None
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run_not_found")
    try:
        return _safe_run_status(record)
    except Exception:
        raise _service_unavailable() from None


@router.post(
    "/runs/{run_key}/cancel",
    response_model=CancelRunResponse,
    responses={
        401: {"model": APIError, "description": "Authentication required"},
        403: {"model": APIError, "description": "CurrentAdmin required"},
        404: {"model": APIError, "description": "run_not_found"},
        503: _ERROR_RESPONSE,
    },
)
def cancel_reviewed_run(
    run_key: str,
    _current_admin: CurrentAdmin,
    runtime: Annotated[ReviewedRuntimeAccess, Depends(get_reviewed_runtime)],
) -> CancelRunResponse:
    """Immediately cancel and fence an active run, or report its terminal state."""
    _require_run_key(run_key)
    try:
        record = runtime.ledger.cancel(run_key, datetime.now(timezone.utc))
    except Exception:
        raise _service_unavailable() from None
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run_not_found")
    try:
        return CancelRunResponse(
            run=_safe_run_status(record),
            outcome="cancelled" if record.status == "cancelled" else "terminal",
        )
    except Exception:
        raise _service_unavailable() from None


async def _read_manual_body(request: Request) -> ManualRunBody:
    """Read a bounded JSON request and reject ambiguous or unsafe payloads."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="json_required",
        )

    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > _MAX_REQUEST_BODY_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="request_too_large",
                )
            body.extend(chunk)
        decoded = bytes(body).decode("utf-8")
        payload = json.loads(
            decoded,
            object_pairs_hook=_unique_object_pairs,
            parse_constant=_reject_nonfinite_constant,
        )
        _validate_request_tree(payload)
        return ManualRunBody.model_validate(payload)
    except HTTPException:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
        TypeError,
        ValidationError,
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_request",
        ) from None
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_request",
        ) from None


def _unique_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys at every nesting level."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> NoReturn:
    """Reject JavaScript-style NaN and Infinity JSON tokens."""
    del value
    raise ValueError("non-finite JSON number")


def _validate_request_tree(value: object) -> None:
    """Bound parsed JSON depth and nodes, including exponent overflow values."""
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    nodes = 0
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH + 1:
            raise ValueError("request complexity limit exceeded")
        if isinstance(current, dict):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                pending.append((child, depth + 1))
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)
        elif isinstance(current, float) and not math.isfinite(current):
            raise ValueError("non-finite JSON number")


def _reject_unsafe_parameters(value: Mapping[str, JsonValue]) -> None:
    """Enforce reviewed CLI execution and connection-selector restrictions."""
    pending: list[object] = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                normalized = "".join(
                    character for character in key.casefold() if character.isalnum()
                )
                if normalized in _EXECUTION_FIELDS or _is_connection_selector(normalized):
                    raise ValueError("unsupported execution or connection selector")
                pending.append(child)
        elif isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, str) and _contains_unsupported_connection_value(current):
            raise ValueError("unsupported connection value")


def _is_connection_selector(normalized_key: str) -> bool:
    """Recognize normalized database and connection selector aliases."""
    return any(
        normalized_key.startswith(root) and normalized_key.endswith(suffix)
        for root in _CONNECTION_SELECTOR_ROOTS
        for suffix in _CONNECTION_SELECTOR_SUFFIXES
    )


def _contains_unsupported_connection_value(value: str) -> bool:
    """Reject embedded connection credentials and executable URI selectors."""
    if _JDBC_CONNECTION_PREFIX.search(value):
        return True
    if _PASSWORD_ASSIGNMENT.search(value):
        return True
    if _DSN_CONTEXT_ASSIGNMENT.search(value) and _DSN_USER_ASSIGNMENT.search(value):
        return True

    for match in _URI_CANDIDATE.finditer(value):
        candidate = match.group(1)
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            authority = candidate.split("://", 1)[1].split("/", 1)[0]
            malformed_query = (
                candidate.split("?", 1)[1].split("#", 1)[0] if "?" in candidate else ""
            )
            if "@" in authority or _uri_query_has_sensitive_selector(malformed_query):
                return True
            continue
        if parsed.netloc and (parsed.username is not None or parsed.password is not None):
            return True
        if _uri_query_has_sensitive_selector(parsed.query):
            return True
    return False


def _uri_query_has_sensitive_selector(query: str) -> bool:
    """Reject sensitive, execution, and connection selectors inside URL queries."""
    for name, _ in parse_qsl(query, keep_blank_values=True, max_num_fields=MAX_JSON_NODES):
        normalized = "".join(character for character in name.casefold() if character.isalnum())
        if normalized in _EXECUTION_FIELDS or _is_connection_selector(normalized):
            return True
        if any(part in normalized for part in _QUERY_SENSITIVE_KEY_PARTS):
            return True
    return False


def _job_summary(task: TaskDefinition) -> ReviewedJobSummary:
    """Build an allowlisted job DTO without schedules or callable internals."""
    return ReviewedJobSummary(
        job_id=task.job_id,
        version=task.version,
        dataset_id=task.dataset_id,
        provider=task.provider,
        model=task.model,
        enabled=task.enabled,
        reviewed=True,
    )


def _accepted_receipt(receipt: DispatchReceipt) -> AcceptedRunResponse:
    """Validate and expose only the stable manual-submit receipt fields."""
    if (
        not isinstance(receipt, DispatchReceipt)
        or not _RUN_KEY.fullmatch(receipt.identity.run_key)
        or receipt.status == "dry_run"
        or receipt.created_at is None
    ):
        raise ValueError("dispatcher returned an invalid receipt")
    if receipt.status not in _RUN_STATUSES:
        raise ValueError("dispatcher returned an invalid receipt")
    return AcceptedRunResponse(
        run_key=receipt.identity.run_key,
        status=receipt.status,
        job_id=receipt.identity.job_id,
        version=receipt.identity.version,
        created_at=receipt.created_at,
    )


def _require_run_key(run_key: str) -> None:
    """Reject any path key that is not one lowercase SHA-256 identity."""
    if not _RUN_KEY.fullmatch(run_key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run_not_found")


def _safe_run_status(record: RunRecord) -> RunStatusResponse:
    """Serialize only public run state, with a fixed failure code."""
    if not isinstance(record, RunRecord) or record.status not in _RUN_STATUSES:
        raise ValueError("ledger returned an invalid run record")
    return RunStatusResponse(
        run_key=record.run_key,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        failure_code="run_failed" if record.status == "failed" else None,
    )


def _service_unavailable() -> HTTPException:
    """Return a fixed response without leaking exception or backend details."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="reviewed_run_service_unavailable",
    )
