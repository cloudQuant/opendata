"""Archive and verify one bounded Sina LRB process response.

The handler is an explicit wrapper around a reviewed ``ProcessFetchHandler``.
It remains a ``ProcessFetchHandler`` instance for persistent-worker heartbeat
validation, but it does not register a job, enable scheduling, or perform work
until its caller invokes it. Source access and raw retention require separate,
explicit authorization references. Those references are caller-supplied audit
metadata; this module does not authenticate the underlying grants.

This module persists response bytes and returns a hash-bound reference. It does
not provide durable RunLedger linkage or crash recovery. The caller must keep
the job disabled until that lifecycle is implemented and reviewed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, Protocol, TypeGuard

from opendata.data.providers.sina.lrb_statement_process import (
    MAX_RAW_RESPONSE_BYTES,
    SINA_LRB_PROCESS_OPERATION_ID,
    SinaLrbQuery,
    build_sina_lrb_query_parameters,
    sina_lrb_request_summary_sha256,
)
from opendata.pipeline.raw_evidence import (
    HistoryLimit,
    RawResponseArtifactWriter,
    RawResponseSource,
    RetentionClass,
)
from opendata.pipeline.raw_evidence_reader import (
    RawEvidenceReadResult,
    RawResponseArtifactReader,
)
from opendata.pipeline.reviewed_worker import ProcessFetchHandler
from opendata.pipeline.run_identity import RunRequest
from opendata.pipeline.run_service import RunExecutionContext

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "SINA_LRB_RAW_RESPONSE_SOURCE",
    "SinaLrbArchivedCapture",
    "SinaLrbRawAuthorization",
    "SinaLrbRawCaptureError",
    "SinaLrbRawCaptureHandler",
    "build_sina_lrb_query_parameters",
    "read_sina_lrb_archived_evidence",
    "read_sina_lrb_archived_response",
    "sina_lrb_request_summary_sha256",
]

SINA_LRB_RAW_RESPONSE_SOURCE = RawResponseSource(
    provider_id="sina",
    dataset_id="sina_lrb_report_observations",
    operation_id=SINA_LRB_PROCESS_OPERATION_ID,
)
"""Fixed source identity for the raw LRB response artifact."""

_AUTHORIZATION_REFERENCE = re.compile(
    r"^(?:source-auth|raw-retention):[A-Za-z][A-Za-z0-9_.-]{0,63}$",
    re.ASCII,
)
_SECRET_SHAPED_REFERENCE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{16,}\b|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,})\b|\bAKIA[0-9A-Z]{16}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b|\bBearer\s+)",
    re.ASCII | re.IGNORECASE,
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_RUN_KEY = _SHA256
_RETENTION_CLASSES = frozenset({"unknown", "ephemeral", "restricted", "durable_candidate"})
_HISTORY_LIMITS = frozenset(
    {"unknown", "provider_retention_window", "snapshot_only", "partial_history", "none_documented"}
)
_UTC = timezone.utc


class SinaLrbRawCaptureError(ValueError):
    """Stable, source-value-free error raised by the raw capture boundary."""

    _MESSAGES: dict[str, str] = {
        "authorization_denied": "Sina LRB raw capture authorization is unavailable.",
        "invalid_request": "Sina LRB raw capture request is invalid.",
        "invalid_capture": "Sina LRB process result is invalid.",
        "request_summary_invalid": "Sina LRB request summary is invalid.",
        "archive_failed": "Sina LRB raw response could not be verified after archival.",
    }

    def __init__(
        self,
        code: Literal[
            "authorization_denied",
            "invalid_request",
            "invalid_capture",
            "request_summary_invalid",
            "archive_failed",
        ],
    ) -> None:
        """Create a stable error without embedding request or response content."""
        message = self._MESSAGES.get(code)
        if message is None:
            raise ValueError("Unsupported Sina LRB raw-capture error code.")
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SinaLrbRawAuthorization:
    """Explicit caller declarations for source access and raw-byte retention.

    ``source_access_authorized`` and ``raw_retention_authorized`` are separate
    because permission to query a provider does not itself grant permission to
    retain its response. The references must identify the operator's reviewed
    decisions; the booleans and references are not cryptographic proof.
    """

    source_access_ref: str
    raw_retention_ref: str
    source_access_authorized: bool
    raw_retention_authorized: bool

    def __post_init__(self) -> None:
        """Reject missing, malformed, or secret-shaped authorization labels."""
        if (
            not _is_authorization_reference(self.source_access_ref, "source-auth")
            or not _is_authorization_reference(self.raw_retention_ref, "raw-retention")
            or type(self.source_access_authorized) is not bool
            or type(self.raw_retention_authorized) is not bool
        ):
            raise ValueError("Sina LRB authorization metadata is invalid.")


@dataclass(frozen=True, slots=True)
class SinaLrbArchivedCapture:
    """Bounded reference to verified source bytes; the body is not retained here."""

    instrument_id: str
    run_key: str
    status_code: int
    retrieved_at_utc: datetime
    captured_at_utc: datetime
    request_summary_sha256: str
    response_sha256: str
    manifest_sha256: str
    source_access_ref: str
    raw_retention_ref: str

    def __post_init__(self) -> None:
        """Validate each field again at the archived-result trust boundary."""
        try:
            instrument_id = SinaLrbQuery(self.instrument_id).symbol
        except Exception:
            raise ValueError("Sina LRB archived-capture metadata is invalid.") from None
        retrieved_at = _validated_utc_datetime(self.retrieved_at_utc)
        captured_at = _validated_utc_datetime(self.captured_at_utc)
        if (
            type(self.instrument_id) is not str
            or instrument_id != self.instrument_id
            or type(self.run_key) is not str
            or _RUN_KEY.fullmatch(self.run_key) is None
            or type(self.status_code) is not int
            or self.status_code != 200
            or captured_at < retrieved_at
            or type(self.request_summary_sha256) is not str
            or _SHA256.fullmatch(self.request_summary_sha256) is None
            or self.request_summary_sha256 != sina_lrb_request_summary_sha256(instrument_id)
            or type(self.response_sha256) is not str
            or _SHA256.fullmatch(self.response_sha256) is None
            or type(self.manifest_sha256) is not str
            or _SHA256.fullmatch(self.manifest_sha256) is None
            or not _is_authorization_reference(self.source_access_ref, "source-auth")
            or not _is_authorization_reference(self.raw_retention_ref, "raw-retention")
        ):
            raise ValueError("Sina LRB archived-capture metadata is invalid.")


def read_sina_lrb_archived_evidence(
    archived: SinaLrbArchivedCapture,
    *,
    artifact_root: Path | str,
) -> RawEvidenceReadResult:
    """Read and verify the complete raw evidence for a body-free capture reference.

    The explicit root is required so normalization cannot silently read from an
    environment-selected location. The reader verifies manifest and response
    hashes, source identity, request digest, status, and the captured-time bound;
    this function additionally compares the verified manifest fields with the
    supplied archived DTO.

    Args:
        archived: Validated, body-free result returned by the capture handler.
        artifact_root: Same private artifact root injected into the handler.

    Returns:
        The verified response bytes, parsed manifest, and manifest digest.

    Raises:
        SinaLrbRawCaptureError: If the DTO or referenced artifact is invalid.
    """
    if type(archived) is not SinaLrbArchivedCapture:
        raise SinaLrbRawCaptureError("invalid_capture")
    try:
        reader = RawResponseArtifactReader(
            artifact_root,
            max_response_bytes=MAX_RAW_RESPONSE_BYTES,
        )
        result = reader.read(
            archived.manifest_sha256,
            expected_source=SINA_LRB_RAW_RESPONSE_SOURCE,
            expected_request_summary_sha256=archived.request_summary_sha256,
            captured_at_not_after=archived.captured_at_utc,
            expected_http_status=archived.status_code,
        )
    except Exception:
        raise SinaLrbRawCaptureError("archive_failed") from None
    if (
        result.manifest_sha256 != archived.manifest_sha256
        or result.manifest.source != SINA_LRB_RAW_RESPONSE_SOURCE
        or result.manifest.request_summary_sha256 != archived.request_summary_sha256
        or result.manifest.http_status != archived.status_code
        or result.manifest.response_sha256 != archived.response_sha256
        or result.manifest.captured_at_utc != archived.captured_at_utc
    ):
        raise SinaLrbRawCaptureError("archive_failed")
    return result


def read_sina_lrb_archived_response(
    archived: SinaLrbArchivedCapture,
    *,
    artifact_root: Path | str,
) -> bytes:
    """Return the exact verified body for compatibility with simple callers."""
    body: bytes = read_sina_lrb_archived_evidence(archived, artifact_root=artifact_root).body
    return body


class SinaLrbRawCaptureHandler(ProcessFetchHandler):
    """Archive one decoded LRB process response while preserving worker checks.

    This is a nominal ``ProcessFetchHandler`` subclass so the persistent worker
    continues to enforce the process-backed fetcher and heartbeat interval. It
    delegates execution to the exact reviewed process handler, then persists
    and re-reads the bounded body before returning a body-free reference.
    Construction is inert: no job registration, network access, or filesystem
    writes occur until the instance is called.
    """

    _delegate: ProcessFetchHandler
    _writer: RawResponseArtifactWriter
    _reader: RawResponseArtifactReader
    _authorization: SinaLrbRawAuthorization
    _retention_class: RetentionClass
    _source_history_limit: HistoryLimit

    __slots__ = (
        "_delegate",
        "_writer",
        "_reader",
        "_authorization",
        "_retention_class",
        "_source_history_limit",
    )

    def __init__(
        self,
        delegate: ProcessFetchHandler,
        *,
        artifact_root: Path | str,
        authorization: SinaLrbRawAuthorization,
        retention_class: RetentionClass,
        source_history_limit: HistoryLimit,
    ) -> None:
        """Bind an existing process handler and explicit raw-retention policy.

        Args:
            delegate: Fixed reviewed LRB process handler.
            artifact_root: Explicit private directory for content-addressed raw
                objects and manifests. It is never inferred from environment.
            authorization: Separate source-access and raw-retention decisions.
            retention_class: Explicit declared retention classification.
            source_history_limit: Explicit source-history boundary declaration.

        Raises:
            TypeError: If the process handler or authorization object is invalid.
            ValueError: If the root or policy labels are not supported.
        """
        if not isinstance(delegate, ProcessFetchHandler):
            raise TypeError("delegate must be a ProcessFetchHandler")
        if type(authorization) is not SinaLrbRawAuthorization:
            raise TypeError("authorization must be a SinaLrbRawAuthorization")
        if type(retention_class) is not str or retention_class not in _RETENTION_CLASSES:
            raise ValueError("retention_class is invalid")
        if type(source_history_limit) is not str or source_history_limit not in _HISTORY_LIMITS:
            raise ValueError("source_history_limit is invalid")
        self._delegate = delegate
        self._writer = RawResponseArtifactWriter(
            artifact_root,
            max_response_bytes=MAX_RAW_RESPONSE_BYTES,
        )
        self._reader = RawResponseArtifactReader(
            artifact_root,
            max_response_bytes=MAX_RAW_RESPONSE_BYTES,
        )
        self._authorization = authorization
        self._retention_class = retention_class
        self._source_history_limit = source_history_limit

    @property
    def heartbeat_interval_seconds(self) -> float:
        """Expose the delegated executor interval required by worker validation."""
        interval: float = self._delegate.heartbeat_interval_seconds
        return interval

    def __call__(self, request: RunRequest, context: RunExecutionContext) -> object:
        """Fetch through the reviewed process handler and archive before return.

        The raw response is retained only after both authorization declarations
        are true. The archive is then read back by manifest digest and checked
        against the expected request, status, response hash, and size.

        Args:
            request: Validated one-instrument manual LRB request.
            context: Current fenced RunService execution context.

        Returns:
            A body-free :class:`SinaLrbArchivedCapture` for normalization.

        Raises:
            SinaLrbRawCaptureError: If authorization, capture, or archive checks
                fail. Source values and response bytes are never included.
        """
        if (
            self._authorization.source_access_authorized is not True
            or self._authorization.raw_retention_authorized is not True
        ):
            raise SinaLrbRawCaptureError("authorization_denied")
        if type(context) is not RunExecutionContext:
            raise SinaLrbRawCaptureError("invalid_request")
        run_key = context.run_key
        if (
            type(run_key) is not str
            or _RUN_KEY.fullmatch(run_key) is None
            or type(context.worker) is not str
            or not context.worker
            or len(context.worker) > 128
            or type(context.fencing_token) is not int
            or context.fencing_token < 1
        ):
            raise SinaLrbRawCaptureError("invalid_request")
        request_snapshot, instrument_id = _snapshot_manual_request(request)
        try:
            request_digest = sina_lrb_request_summary_sha256(instrument_id)
        except Exception:
            raise SinaLrbRawCaptureError("request_summary_invalid") from None

        context.checkpoint()
        raw = self._delegate(request_snapshot, context)
        capture = _validate_process_capture(raw, expected_instrument_id=instrument_id)
        try:
            captured = self._writer.capture(
                (capture.body,),
                source=SINA_LRB_RAW_RESPONSE_SOURCE,
                request_summary_sha256=request_digest,
                http_status=capture.status_code,
                retention_class=self._retention_class,
                source_history_limit=self._source_history_limit,
            )
            manifest_sha256 = captured.manifest_path.stem
            verified = self._reader.read(
                manifest_sha256,
                expected_source=SINA_LRB_RAW_RESPONSE_SOURCE,
                expected_request_summary_sha256=request_digest,
                captured_at_not_after=datetime.now(_UTC),
                expected_http_status=capture.status_code,
            )
        except Exception:
            raise SinaLrbRawCaptureError("archive_failed") from None

        manifest = verified.manifest
        response_sha256 = hashlib.sha256(capture.body).hexdigest()
        if (
            verified.body != capture.body
            or manifest.response_sha256 != response_sha256
            or manifest.response_size_bytes != len(capture.body)
            or manifest.http_status != capture.status_code
            or manifest.request_summary_sha256 != request_digest
            or manifest.source != SINA_LRB_RAW_RESPONSE_SOURCE
            or manifest.retention_class != self._retention_class
            or manifest.source_history_limit != self._source_history_limit
            or verified.manifest_sha256 != manifest_sha256
            or manifest.captured_at_utc < capture.retrieved_at_utc
        ):
            raise SinaLrbRawCaptureError("archive_failed")
        context.checkpoint()
        return SinaLrbArchivedCapture(
            instrument_id=instrument_id,
            run_key=run_key,
            status_code=capture.status_code,
            retrieved_at_utc=capture.retrieved_at_utc,
            captured_at_utc=manifest.captured_at_utc,
            request_summary_sha256=request_digest,
            response_sha256=response_sha256,
            manifest_sha256=manifest_sha256,
            source_access_ref=self._authorization.source_access_ref,
            raw_retention_ref=self._authorization.raw_retention_ref,
        )


def _snapshot_manual_request(request: RunRequest) -> tuple[RunRequest, str]:
    """Revalidate and snapshot the exact one-symbol manual request shape."""
    if type(request) is not RunRequest:
        raise SinaLrbRawCaptureError("invalid_request")
    try:
        validated = RunRequest.model_validate(request.model_dump(mode="python"), strict=True)
    except Exception:
        raise SinaLrbRawCaptureError("invalid_request") from None
    if (
        validated.run_type != "manual"
        or type(validated.query) is not dict
        or validated.query
        or type(validated.partition) is not dict
        or set(validated.partition) != {"instrument_id"}
        or validated.shard is not None
        or validated.occurrence is not None
        or validated.backfill_start is not None
        or validated.backfill_end is not None
        or type(validated.partition["instrument_id"]) is not str
    ):
        raise SinaLrbRawCaptureError("invalid_request")
    try:
        instrument_id = SinaLrbQuery(validated.partition["instrument_id"]).symbol
    except Exception:
        raise SinaLrbRawCaptureError("invalid_request") from None
    return validated, instrument_id


def _validate_process_capture(
    value: object, *, expected_instrument_id: str
) -> _ValidatedProcessCapture:
    """Revalidate the exact parent-decoded LRB process DTO without exposing it."""
    if not _is_sina_lrb_process_capture(value):
        raise SinaLrbRawCaptureError("invalid_capture")
    if (
        type(value.query) is not SinaLrbQuery
        or value.query.symbol != expected_instrument_id
        or type(value.status_code) is not int
        or value.status_code != 200
        or type(value.body) is not bytes
        or len(value.body) > MAX_RAW_RESPONSE_BYTES
    ):
        raise SinaLrbRawCaptureError("invalid_capture")
    retrieved_at = value.retrieved_at_utc
    if type(retrieved_at) is not datetime or retrieved_at.tzinfo is None:
        raise SinaLrbRawCaptureError("invalid_capture")
    try:
        offset = retrieved_at.utcoffset()
        normalized = retrieved_at.astimezone(_UTC)
    except Exception:
        raise SinaLrbRawCaptureError("invalid_capture") from None
    if (
        type(offset) is not timedelta
        or offset != timedelta(0)
        or type(normalized) is not datetime
        or normalized.tzinfo is not _UTC
    ):
        raise SinaLrbRawCaptureError("invalid_capture")
    return _ValidatedProcessCapture(
        status_code=value.status_code,
        retrieved_at_utc=normalized,
        body=value.body,
    )


@dataclass(frozen=True, slots=True)
class _ValidatedProcessCapture:
    """Private normalized view of the process result used only during capture."""

    status_code: int
    retrieved_at_utc: datetime
    body: bytes


class _SinaLrbProcessCaptureView(Protocol):
    """Typed view of the process DTO fields used after exact runtime checking."""

    query: SinaLrbQuery
    status_code: int
    retrieved_at_utc: datetime
    body: bytes


def _is_sina_lrb_process_capture(value: object) -> TypeGuard[_SinaLrbProcessCaptureView]:
    """Check the exact process DTO type and expose its known fields to type checkers.

    The local import avoids a module cycle when the job factory installs this
    handler as its ProcessFetchHandler implementation.
    """
    from opendata.pipeline.sina_lrb_process_job import SinaLrbProcessCapture

    return type(value) is SinaLrbProcessCapture


def _validated_utc_datetime(value: object) -> datetime:
    """Return a normalized UTC datetime or reject a malformed capture timestamp."""
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("Sina LRB archived-capture metadata is invalid.")
    try:
        offset = value.utcoffset()
        normalized = value.astimezone(_UTC)
    except Exception:
        raise ValueError("Sina LRB archived-capture metadata is invalid.") from None
    if (
        type(offset) is not timedelta
        or offset != timedelta(0)
        or type(normalized) is not datetime
        or normalized.tzinfo is not _UTC
    ):
        raise ValueError("Sina LRB archived-capture metadata is invalid.")
    return normalized


def _is_authorization_reference(value: object, prefix: str) -> bool:
    """Accept a short audit label while rejecting obvious credential material."""
    return not (
        type(value) is not str
        or not value.startswith(f"{prefix}:")
        or _AUTHORIZATION_REFERENCE.fullmatch(value) is None
        or _SECRET_SHAPED_REFERENCE.search(value) is not None
    )
