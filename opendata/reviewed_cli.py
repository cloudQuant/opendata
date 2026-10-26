"""Strict offline CLI protocol for explicitly injected reviewed applications.

The module does not load settings, dotenv files, database URLs, application
factories, handler paths, or legacy scripts. A composition root supplies the
reviewed dispatcher and (when enabled) the worker-once SPI. Without that
injected application, the command exits with configuration error 78.

Request examples (UTF-8 JSON on stdin or in a regular file)::

    {"schema_version":1,"operation":"scheduled","job_id":"daily",
     "version":"v1","occurrence":"2026-10-26T09:30:00Z","dry_run":true}
    {"schema_version":1,"operation":"manual","job_id":"daily",
     "version":"v1","payload":{"idempotency_key":"adhoc-01"}}
    {"schema_version":1,"operation":"backfill","job_id":"daily",
     "version":"v1","payload":{"idempotency_key":"bf-01",
     "backfill_start":"2026-10-01","backfill_end":"2026-10-26"}}
    {"schema_version":1,"operation":"worker_once","worker":"worker-01",
     "lease_seconds":60}

An application calls ``run_reviewed_cli(argv, application=...)`` after it has
constructed its reviewed catalog, dispatcher, database engine, and role plan.
The standalone ``main()`` deliberately has no import-string or environment
factory option, so ``python -m opendata.reviewed_cli`` fails closed until a
reviewed composition root wires this API.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from opendata.pipeline.run_dispatch import DispatchReceipt, RunDispatcher
from opendata.pipeline.run_ledger import RunLedgerConflictError
from opendata.pipeline.run_service import RunOutcome
from opendata.pipeline.runtime_roles import RuntimeRolePlan

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping, Sequence
    from typing import TextIO

    from opendata.pipeline.runtime_roles import WorkerOnceSPI
MAX_REQUEST_BYTES = 65_536
MAX_REQUEST_DEPTH = 12
MAX_REQUEST_NODES = 8_192
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_WORKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,254}$")
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
_REQUEST_FIELDS: Mapping[str, frozenset[str]] = {
    "scheduled": frozenset(
        {"schema_version", "operation", "job_id", "version", "occurrence", "payload", "dry_run"}
    ),
    "manual": frozenset({"schema_version", "operation", "job_id", "version", "payload", "dry_run"}),
    "backfill": frozenset(
        {"schema_version", "operation", "job_id", "version", "payload", "dry_run"}
    ),
    "worker_once": frozenset({"schema_version", "operation", "worker", "lease_seconds"}),
}


class _CLIRequest(BaseModel):
    """Strict versioned wire model; validation errors are never printed raw."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    operation: Literal["scheduled", "manual", "backfill", "worker_once"]
    job_id: str | None = Field(default=None, min_length=1, max_length=128)
    version: str | None = Field(default=None, min_length=1, max_length=128)
    occurrence: str | None = Field(default=None, min_length=20, max_length=40)
    payload: dict[str, JsonValue] | None = None
    dry_run: bool = False
    worker: str | None = Field(default=None, min_length=1, max_length=255)
    lease_seconds: int | None = Field(default=None, ge=1, le=86_400)

    @model_validator(mode="after")
    def validate_operation_shape(self) -> _CLIRequest:
        """Require exactly the fields supported by the selected operation."""
        supplied = self.model_fields_set
        allowed = _REQUEST_FIELDS[self.operation]
        if supplied - allowed:
            raise ValueError("request contains fields outside the selected operation")
        if self.operation == "worker_once":
            if self.worker is None or self.lease_seconds is None:
                raise ValueError("worker_once requires worker and lease_seconds")
            if not _WORKER.fullmatch(self.worker):
                raise ValueError("worker identity is invalid")
            return self

        if self.job_id is None or self.version is None:
            raise ValueError("dispatch request requires job_id and version")
        if not _IDENTIFIER.fullmatch(self.job_id) or not _IDENTIFIER.fullmatch(self.version):
            raise ValueError("job identity is invalid")
        if self.operation == "scheduled" and self.occurrence is None:
            raise ValueError("scheduled request requires occurrence")
        if self.operation != "scheduled" and self.occurrence is not None:
            raise ValueError("only scheduled requests accept occurrence")
        if self.operation in {"manual", "backfill"} and self.payload is None:
            raise ValueError("manual and backfill requests require payload")
        _reject_execution_fields(self.payload)
        return self


@dataclass(frozen=True, slots=True)
class ReviewedCLIApplication:
    """Dependencies supplied by a reviewed application composition root."""

    runtime_plan: RuntimeRolePlan
    dispatcher: RunDispatcher | None = None
    worker_once: WorkerOnceSPI | None = None

    def __post_init__(self) -> None:
        """Require explicit reviewed roles and their injected runtime boundaries."""
        if not isinstance(self.runtime_plan, RuntimeRolePlan):
            raise TypeError("runtime_plan must be a RuntimeRolePlan")
        if self.runtime_plan.profile != "reviewed":
            raise ValueError("reviewed CLI requires an explicit reviewed runtime plan")
        if self.runtime_plan.roles & {"api", "scheduler"}:
            if not isinstance(self.dispatcher, RunDispatcher):
                raise ValueError("enabled dispatch roles require an injected RunDispatcher")
        elif self.dispatcher is not None:
            raise ValueError("RunDispatcher was supplied while dispatch roles are disabled")
        if "worker" in self.runtime_plan.roles:
            if not callable(self.worker_once):
                raise ValueError("enabled worker role requires an injected worker-once SPI")
        elif self.worker_once is not None:
            raise ValueError("worker-once SPI was supplied while worker role is disabled")


@dataclass(frozen=True, slots=True)
class _CLIResult:
    """One sanitized command outcome and its process exit code."""

    exit_code: int
    payload: Mapping[str, object]


def run_reviewed_cli(
    argv: Sequence[str],
    *,
    application: ReviewedCLIApplication | None,
    stdin: object | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Parse one bounded request and call only explicitly injected interfaces.

    Args:
        argv: Exact command arguments; only ``--request PATH`` is accepted.
        application: Reviewed dependencies from a trusted composition root.
        stdin: Optional binary or text stream used when PATH is ``-``.
        stdout: Optional text stream for the sanitized success receipt.
        stderr: Optional text stream for sanitized failures.

    Returns:
        Process exit code: 0 success, 1 operation failure, 2 bad request,
        78 missing or incompatible reviewed configuration, or 130 interruption.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        source = _parse_arguments(argv)
    except ValueError:
        return _emit(_CLIResult(2, {"status": "rejected", "code": "invalid_arguments"}), err)
    if application is None:
        return _emit(
            _CLIResult(78, {"status": "rejected", "code": "reviewed_application_required"}),
            err,
        )
    if not isinstance(application, ReviewedCLIApplication):
        return _emit(
            _CLIResult(78, {"status": "rejected", "code": "reviewed_application_invalid"}),
            err,
        )

    try:
        request_bytes = _read_request(source, stdin=stdin)
        request = _parse_request(request_bytes)
    except KeyboardInterrupt:
        return _emit(_CLIResult(130, {"status": "cancelled", "code": "interrupted"}), err)
    except Exception:
        return _emit(_CLIResult(2, {"status": "rejected", "code": "invalid_request"}), err)

    try:
        result = _execute_request(request, application)
    except _RoleUnavailableError:
        return _emit(_CLIResult(78, {"status": "rejected", "code": "role_not_enabled"}), err)
    except RunLedgerConflictError:
        return _emit(_CLIResult(1, {"status": "rejected", "code": "identity_conflict"}), err)
    except ValueError:
        return _emit(_CLIResult(2, {"status": "rejected", "code": "request_rejected"}), err)
    except KeyboardInterrupt:
        return _emit(_CLIResult(130, {"status": "cancelled", "code": "interrupted"}), err)
    except Exception:
        return _emit(_CLIResult(1, {"status": "failed", "code": "operation_failed"}), err)
    return _emit(result, out)


def main(argv: Sequence[str] | None = None) -> int:
    """Fail closed when called without a trusted composition-root application."""
    return run_reviewed_cli(
        sys.argv[1:] if argv is None else argv,
        application=None,
    )


class _RoleUnavailableError(RuntimeError):
    """Raised internally when a request exceeds the injected role plan."""


def _parse_arguments(argv: Sequence[str]) -> str:
    """Accept only one explicit request source without shell interpretation."""
    if len(argv) != 2 or argv[0] != "--request":
        raise ValueError("expected --request PATH")
    source = argv[1]
    if not isinstance(source, str) or not source or len(source) > 4_096 or "\x00" in source:
        raise ValueError("request path is invalid")
    return source


def _read_request(source: str, *, stdin: object | None) -> bytes:
    """Read at most one request limit from stdin or a no-follow regular file."""
    if source == "-":
        stream = stdin if stdin is not None else getattr(sys.stdin, "buffer", sys.stdin)
        read = getattr(stream, "read", None)
        if not callable(read):
            raise ValueError("stdin is not readable")
        value = read(MAX_REQUEST_BYTES + 1)
        if isinstance(value, str):
            raw = value.encode("utf-8", errors="strict")
        elif isinstance(value, bytes):
            raw = value
        else:
            raise ValueError("stdin returned an unsupported value")
    else:
        path = Path(source)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is not None:
            flags |= nofollow
        elif path.is_symlink():
            raise ValueError("request path must not be a symbolic link")
        descriptor = os.open(path, flags)
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError("request source must be a regular file")
            if file_stat.st_size > MAX_REQUEST_BYTES:
                raise ValueError("request exceeds the byte limit")
            chunks: list[bytes] = []
            remaining = MAX_REQUEST_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 8_192))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request exceeds the byte limit")
    return raw


def _parse_request(raw: bytes) -> _CLIRequest:
    """Decode bounded JSON with duplicate-key, depth, and node-count checks."""
    text = raw.decode("utf-8", errors="strict")
    value = json.loads(
        text,
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_nonfinite_constant,
    )
    _validate_json_tree(value)
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    return _CLIRequest.model_validate(value)


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject ambiguous duplicate JSON keys before model validation."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> object:
    """Reject non-standard NaN and Infinity tokens."""
    del value
    raise ValueError("non-finite JSON number")


def _validate_json_tree(value: object) -> None:
    """Bound tree depth and node count and reject non-finite numeric values."""
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_REQUEST_NODES or depth > MAX_REQUEST_DEPTH:
            raise ValueError("request complexity limit exceeded")
        if isinstance(current, dict):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, float) and not math.isfinite(current):
            raise ValueError("non-finite JSON number")


def _reject_execution_fields(value: object) -> None:
    """Reject arbitrary-code and connection-selector keys at every JSON depth."""
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                normalized = "".join(
                    character for character in key.casefold() if character.isalnum()
                )
                if normalized in _EXECUTION_FIELDS:
                    raise ValueError("request includes an unsupported execution field")
                stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)


def _execute_request(request: _CLIRequest, application: ReviewedCLIApplication) -> _CLIResult:
    """Dispatch one request through reviewed interfaces and redact all payload data."""
    if request.operation == "worker_once":
        if not application.runtime_plan.enables("worker") or application.worker_once is None:
            raise _RoleUnavailableError("worker role is disabled")
        if request.worker is None or request.lease_seconds is None:
            raise ValueError("worker request is incomplete")
        outcome = application.worker_once(request.worker, timedelta(seconds=request.lease_seconds))
        return _worker_receipt(outcome)

    dispatcher = application.dispatcher
    if dispatcher is None:
        raise _RoleUnavailableError("dispatcher is not configured")
    if request.operation == "scheduled":
        if not application.runtime_plan.enables("scheduler"):
            raise _RoleUnavailableError("scheduler role is disabled")
        if request.job_id is None or request.version is None or request.occurrence is None:
            raise ValueError("scheduled request is incomplete")
        payload = _payload(request)
        receipt = dispatcher.dispatch_scheduled(
            request.job_id,
            request.version,
            occurrence=_parse_utc_occurrence(request.occurrence),
            payload=payload,
            dry_run=request.dry_run,
        )
        return _dispatch_receipt(receipt)

    if not application.runtime_plan.enables("api"):
        raise _RoleUnavailableError("API dispatch role is disabled")
    if request.job_id is None or request.version is None:
        raise ValueError("dispatch request is incomplete")
    payload = _payload(request)
    if request.operation == "manual":
        receipt = dispatcher.dispatch_manual(
            request.job_id,
            request.version,
            payload,
            dry_run=request.dry_run,
        )
    elif request.operation == "backfill":
        receipt = dispatcher.dispatch_backfill(
            request.job_id,
            request.version,
            payload,
            dry_run=request.dry_run,
        )
    else:  # pragma: no cover - model validation fixes the operation set.
        raise ValueError("unsupported operation")
    return _dispatch_receipt(receipt)


def _payload(request: _CLIRequest) -> dict[str, object]:
    """Copy a JSON payload and parse only declared backfill date fields."""
    payload: dict[str, object] = dict(request.payload or {})
    if request.operation == "backfill":
        for key in ("backfill_start", "backfill_end"):
            value = payload.get(key)
            if isinstance(value, str):
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError("backfill dates must use YYYY-MM-DD")
                payload[key] = parsed
    return payload


def _parse_utc_occurrence(value: str) -> datetime:
    """Parse one explicit UTC ISO-8601 timestamp required by the dispatcher."""
    if not (value.endswith("Z") or value.endswith("+00:00")):
        raise ValueError("scheduled occurrence must be explicit UTC ISO-8601")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    occurrence = datetime.fromisoformat(normalized)
    if occurrence.utcoffset() != timedelta(0):
        raise ValueError("scheduled occurrence must be UTC")
    return occurrence.astimezone(timezone.utc)


def _dispatch_receipt(receipt: DispatchReceipt) -> _CLIResult:
    """Convert a dispatcher receipt to an allowlisted machine-readable response."""
    if not isinstance(receipt, DispatchReceipt):
        raise RuntimeError("dispatcher returned an unsupported receipt")
    if receipt.status in {"dry_run", "queued", "running", "succeeded"}:
        exit_code = 0
    elif receipt.status == "failed":
        exit_code = 1
    elif receipt.status == "cancelled":
        exit_code = 130
    else:  # pragma: no cover - receipt status is a Literal in the dispatcher contract.
        raise RuntimeError("dispatcher returned an unknown status")
    return _CLIResult(
        exit_code,
        {
            "status": receipt.status,
            "run_key": receipt.identity.run_key,
            "run_type": receipt.identity.run_type,
            "job_id": receipt.identity.job_id,
            "version": receipt.identity.version,
            "request_bytes": receipt.identity.request_bytes,
        },
    )


def _worker_receipt(outcome: RunOutcome | None) -> _CLIResult:
    """Return only durable worker state and never expose snapshots or error text."""
    if outcome is None:
        return _CLIResult(0, {"status": "idle"})
    if not isinstance(outcome, RunOutcome):
        raise RuntimeError("worker returned an unsupported outcome")
    record = outcome.record
    if record is None:
        return _CLIResult(1, {"status": "failed", "code": "worker_state_unavailable"})
    if record.status == "succeeded":
        exit_code = 0
    elif record.status == "cancelled":
        exit_code = 130
    else:
        exit_code = 1
    return _CLIResult(exit_code, {"status": record.status, "run_key": record.run_key})


def _emit(result: _CLIResult, stream: TextIO) -> int:
    """Write one bounded JSON receipt and return its matching exit status."""
    stream.write(json.dumps(dict(result.payload), separators=(",", ":"), sort_keys=True))
    stream.write("\n")
    stream.flush()
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover - exercised by the module-entry smoke test.
    raise SystemExit(main())
