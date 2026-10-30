"""Archive, verify, and optionally recover one bounded Sina LRB response.

The handler is an explicit wrapper around a reviewed ``ProcessFetchHandler``.
It remains a ``ProcessFetchHandler`` instance for persistent-worker heartbeat
validation, but it does not register a job, enable scheduling, or perform work
until its caller invokes it. Source access and raw retention require separate,
explicit authorization references. Those references are caller-supplied audit
metadata; this module does not authenticate the underlying grants.

The optional capture journal requires an externally managed signer. It binds
immutable receipts to the canonical run identity and current fencing token,
then lets an explicitly enabled caller reuse one fully verified prior capture.
It does not make a fresh raw manifest independently attributable to a run: the
manifest format contains no run identity, so fresh-capture signing depends on
the trusted handler/caller path. Reuse receipts must chain to a signed capture
receipt for the same run and request. The journal does not claim atomicity with
the warehouse transaction, inspect or reclaim leases, or make orphan manifests
attributable when a crash occurs before a receipt is written. RunLedger remains
the only authority for lease ownership and publication; the caller must keep the
job disabled until the complete recovery lifecycle is reviewed.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeGuard

if TYPE_CHECKING:
    from types import TracebackType

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
    _open_private_child,
    _open_root_directory,
    _verify_file,
    _write_all,
)
from opendata.pipeline.raw_evidence_reader import (
    RawEvidenceReadResult,
    RawResponseArtifactReader,
)
from opendata.pipeline.reviewed_worker import ProcessFetchHandler
from opendata.pipeline.run_identity import RunRequest, TaskDefinition, build_run_identity
from opendata.pipeline.run_ledger import RunRecord
from opendata.pipeline.run_service import RunExecutionContext

__all__ = [
    "SINA_LRB_RAW_RESPONSE_SOURCE",
    "SinaLrbArchivedCapture",
    "SinaLrbCaptureAuditReport",
    "SinaLrbCaptureReceipt",
    "SinaLrbCaptureRecoveryJournal",
    "CaptureReceiptSigner",
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
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$", re.ASCII)
_SIGNATURE_MAX_BYTES = 2_048
_JOURNAL_RECORD_MAX_BYTES = 16 * 1024
_MAX_JOURNAL_EVENTS_PER_RUN = 4_096
_JOURNAL_VERSION = 1
_JOURNAL_DIRECTORY = "lrbjournal"
_EVENT_FILENAME = re.compile(r"^[0-9a-f]{64}\.json$", re.ASCII)
_JOURNAL_TEMP_FILENAME = re.compile(r"^\.tmp-[0-9a-f]{32}$", re.ASCII)
_RECEIPT_FIELDS = frozenset(
    {
        "journal_version",
        "event_type",
        "run_key",
        "request_payload_sha256",
        "request_summary_sha256",
        "instrument_id",
        "fencing_token",
        "created_at_utc",
        "key_id",
        "manifest_sha256",
        "response_sha256",
        "status_code",
        "retrieved_at_utc",
        "captured_at_utc",
        "source_access_ref",
        "raw_retention_ref",
        "origin_fencing_token",
    }
)
_RETENTION_CLASSES = frozenset({"unknown", "ephemeral", "restricted", "durable_candidate"})
_HISTORY_LIMITS = frozenset(
    {"unknown", "provider_retention_window", "snapshot_only", "partial_history", "none_documented"}
)
_UTC = timezone.utc


class CaptureReceiptSigner(Protocol):
    """Externally managed signing service for durable capture receipts.

    Implementations must keep signing keys outside this repository and provide
    stable verification across worker restarts. This interface intentionally
    ships without a local-key fallback: a key stored beside the journal would
    not protect receipts from another process running as the same user.
    """

    @property
    def key_id(self) -> str:
        """Return a stable, non-secret identifier for the active verification key."""
        ...

    def sign(self, payload: bytes) -> bytes:
        """Sign exact canonical payload bytes using externally protected key material."""
        ...

    def verify(self, key_id: str, payload: bytes, signature: bytes) -> bool:
        """Verify a signature against a stable key identifier and exact bytes."""
        ...


class SinaLrbRawCaptureError(ValueError):
    """Stable, source-value-free error raised by the raw capture boundary."""

    _MESSAGES: dict[str, str] = {
        "authorization_denied": "Sina LRB raw capture authorization is unavailable.",
        "invalid_request": "Sina LRB raw capture request is invalid.",
        "invalid_capture": "Sina LRB process result is invalid.",
        "request_summary_invalid": "Sina LRB request summary is invalid.",
        "archive_failed": "Sina LRB raw response could not be verified after archival.",
        "recovery_invalid": "Sina LRB capture recovery evidence is invalid.",
        "recovery_ambiguous": "Sina LRB capture recovery has conflicting candidates.",
        "recovery_unavailable": "Sina LRB capture recovery evidence is unavailable.",
        "recovery_signing_failed": "Sina LRB capture receipt signing is unavailable.",
        "recovery_signature_invalid": "Sina LRB capture receipt signature is invalid.",
    }

    def __init__(
        self,
        code: Literal[
            "authorization_denied",
            "invalid_request",
            "invalid_capture",
            "request_summary_invalid",
            "archive_failed",
            "recovery_invalid",
            "recovery_ambiguous",
            "recovery_unavailable",
            "recovery_signing_failed",
            "recovery_signature_invalid",
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
    """Bounded body-free reference, optionally bound to a signed run receipt."""

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
    request_payload_sha256: str | None = None
    fencing_token: int | None = None
    origin_fencing_token: int | None = None

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
            or (
                self.request_payload_sha256 is not None
                and type(self.request_payload_sha256) is not str
            )
            or (
                self.request_payload_sha256 is not None
                and _SHA256.fullmatch(self.request_payload_sha256) is None
            )
            or (self.fencing_token is not None and type(self.fencing_token) is not int)
            or (self.fencing_token is not None and self.fencing_token < 1)
            or (
                self.origin_fencing_token is not None and type(self.origin_fencing_token) is not int
            )
            or (
                self.origin_fencing_token is not None
                and (self.fencing_token is None or self.origin_fencing_token < 1)
            )
            or (
                self.origin_fencing_token is not None
                and self.fencing_token is not None
                and self.origin_fencing_token >= self.fencing_token
            )
            or (self.fencing_token is not None and self.request_payload_sha256 is None)
        ):
            raise ValueError("Sina LRB archived-capture metadata is invalid.")


@dataclass(frozen=True, slots=True)
class SinaLrbCaptureReceipt:
    """Verified immutable journal event for one attempt or archived response."""

    event_type: Literal["attempt_started", "capture_archived", "capture_reused"]
    run_key: str
    request_payload_sha256: str
    request_summary_sha256: str
    instrument_id: str
    fencing_token: int
    created_at_utc: datetime
    key_id: str
    manifest_sha256: str | None = None
    response_sha256: str | None = None
    status_code: int | None = None
    retrieved_at_utc: datetime | None = None
    captured_at_utc: datetime | None = None
    source_access_ref: str | None = None
    raw_retention_ref: str | None = None
    origin_fencing_token: int | None = None


@dataclass(frozen=True, slots=True)
class SinaLrbCaptureAuditReport:
    """Conservative classification of capture receipts against one ledger row."""

    run_key: str
    ledger_status: str
    disposition: Literal[
        "no_journal_evidence",
        "unlinked_artifact_possible",
        "unpublished_capture_candidate",
        "capture_present_on_nonterminal_run",
        "ledger_succeeded_link_requires_review",
    ]
    capture_manifest_sha256s: tuple[str, ...]
    attempt_fencing_tokens: tuple[int, ...]


class SinaLrbCaptureRecoveryJournal:
    """Append-only, signed capture receipts in the private raw-artifact root.

    The journal records intent and verified captures under a per-run filesystem
    lock. Each event is canonical, content-addressed, signed by the supplied
    external signer, published without clobbering, and fsynced before return.
    It provides durable evidence and explicit reuse primitives, but the caller
    must still verify an active ledger lease and commit through RunLedger.
    """

    def __init__(self, artifact_root: Path | str, signer: CaptureReceiptSigner) -> None:
        """Bind one raw-artifact root and externally managed receipt signer."""
        if not isinstance(artifact_root, (str, Path)):
            raise TypeError("artifact_root must be a path")
        root = Path(artifact_root)
        if not root.is_absolute() or len(root.parts) < 2 or ".." in root.parts:
            raise ValueError("artifact_root must be an absolute non-root path")
        if not callable(getattr(signer, "sign", None)) or not callable(
            getattr(signer, "verify", None)
        ):
            raise TypeError("signer must provide sign and verify operations")
        _validate_key_id(getattr(signer, "key_id", None))
        self._artifact_root = root
        self._signer = signer

    def record_attempt_started(
        self,
        *,
        run_key: str,
        request_payload_sha256: str,
        request_summary_sha256: str,
        instrument_id: str,
        fencing_token: int,
    ) -> SinaLrbCaptureReceipt:
        """Durably record an attempt before its provider operation begins."""
        payload = _receipt_payload(
            event_type="attempt_started",
            run_key=run_key,
            request_payload_sha256=request_payload_sha256,
            request_summary_sha256=request_summary_sha256,
            instrument_id=instrument_id,
            fencing_token=fencing_token,
            created_at_utc=datetime.now(_UTC),
            key_id=_validate_key_id(getattr(self._signer, "key_id", None)),
        )
        self._append_signed_event(payload)
        return _receipt_from_payload(payload)

    def record_capture(
        self,
        archived: SinaLrbArchivedCapture,
        *,
        task: TaskDefinition,
        request: RunRequest,
        request_payload_sha256: str,
        fencing_token: int,
        context: RunExecutionContext | None = None,
        origin_fencing_token: int | None = None,
    ) -> SinaLrbCaptureReceipt:
        """Verify and durably link one manifest to its signed canonical run attempt.

        This boundary independently binds the task and request to either their
        ordinary RunIdentity or a RunService-issued canonical snapshot carried
        by the active context, then reads the manifest and body below its
        configured artifact root before issuing a signed receipt. Because raw
        manifests do not contain a run identity, fresh-capture provenance still
        depends on the trusted handler/caller path. A reuse receipt additionally
        requires a matching signed origin receipt in this run's verified journal.
        """
        if type(archived) is not SinaLrbArchivedCapture:
            raise SinaLrbRawCaptureError("invalid_capture")
        snapshot_json: str | None = None
        if context is not None:
            if (
                type(context) is not RunExecutionContext
                or context.run_key != archived.run_key
                or type(fencing_token) is not int
                or type(context.fencing_token) is not int
                or context.fencing_token < 1
                or context.fencing_token != fencing_token
            ):
                raise SinaLrbRawCaptureError("invalid_capture")
            try:
                context.checkpoint()
                snapshot_json = context._verified_snapshot_json()
            except Exception:
                raise SinaLrbRawCaptureError("recovery_invalid") from None
        try:
            request_snapshot, instrument_id = _snapshot_manual_request(request)
            identity_run_key, identity_payload_sha256 = _sina_lrb_request_identity(
                task,
                request_snapshot,
                expected_run_key=archived.run_key,
                snapshot_json=snapshot_json,
            )
        except Exception:
            raise SinaLrbRawCaptureError("invalid_capture") from None
        if (
            type(fencing_token) is not int
            or fencing_token < 1
            or archived.fencing_token != fencing_token
            or archived.request_payload_sha256 != request_payload_sha256
            or identity_run_key != archived.run_key
            or identity_payload_sha256 != request_payload_sha256
            or archived.instrument_id != instrument_id
            or archived.request_summary_sha256 != sina_lrb_request_summary_sha256(instrument_id)
            or archived.origin_fencing_token != origin_fencing_token
            or (origin_fencing_token is not None and type(origin_fencing_token) is not int)
            or (
                origin_fencing_token is not None
                and (origin_fencing_token < 1 or origin_fencing_token >= fencing_token)
            )
        ):
            raise SinaLrbRawCaptureError("invalid_capture")
        try:
            read_sina_lrb_archived_evidence(archived, artifact_root=self._artifact_root)
        except SinaLrbRawCaptureError:
            raise SinaLrbRawCaptureError("recovery_invalid") from None
        receipts = self.events_for_run(archived.run_key)
        if not any(
            item.event_type == "attempt_started"
            and item.fencing_token == fencing_token
            and item.request_payload_sha256 == request_payload_sha256
            and item.request_summary_sha256 == archived.request_summary_sha256
            and item.instrument_id == archived.instrument_id
            for item in receipts
        ):
            raise SinaLrbRawCaptureError("recovery_invalid")
        if origin_fencing_token is not None and not _origin_capture_receipt_exists(
            receipts,
            run_key=archived.run_key,
            request_payload_sha256=request_payload_sha256,
            request_summary_sha256=archived.request_summary_sha256,
            instrument_id=archived.instrument_id,
            origin_fencing_token=origin_fencing_token,
            manifest_sha256=archived.manifest_sha256,
            response_sha256=archived.response_sha256,
            status_code=archived.status_code,
            retrieved_at_utc=archived.retrieved_at_utc,
            captured_at_utc=archived.captured_at_utc,
            source_access_ref=archived.source_access_ref,
            raw_retention_ref=archived.raw_retention_ref,
        ):
            raise SinaLrbRawCaptureError("recovery_invalid")
        payload = _receipt_payload(
            event_type="capture_reused" if origin_fencing_token is not None else "capture_archived",
            run_key=archived.run_key,
            request_payload_sha256=request_payload_sha256,
            request_summary_sha256=archived.request_summary_sha256,
            instrument_id=archived.instrument_id,
            fencing_token=fencing_token,
            created_at_utc=datetime.now(_UTC),
            key_id=_validate_key_id(getattr(self._signer, "key_id", None)),
            manifest_sha256=archived.manifest_sha256,
            response_sha256=archived.response_sha256,
            status_code=archived.status_code,
            retrieved_at_utc=archived.retrieved_at_utc,
            captured_at_utc=archived.captured_at_utc,
            source_access_ref=archived.source_access_ref,
            raw_retention_ref=archived.raw_retention_ref,
            origin_fencing_token=origin_fencing_token,
        )
        self._append_signed_event(payload)
        return _receipt_from_payload(payload)

    def reusable_capture(
        self,
        *,
        run_key: str,
        request_payload_sha256: str,
        request_summary_sha256: str,
        instrument_id: str,
        current_fencing_token: int,
    ) -> SinaLrbCaptureReceipt | None:
        """Return the newest unambiguous signed capture for this exact run input.

        Only receipts at or below the caller's already validated fencing token
        are considered. The caller must check its live lease before and after
        this read and must re-read the artifact by manifest digest before reuse.
        """
        _validate_receipt_identity(
            run_key,
            request_payload_sha256,
            request_summary_sha256,
            instrument_id,
            current_fencing_token,
        )
        receipts = self.events_for_run(run_key)
        matching = [
            item
            for item in receipts
            if item.event_type in {"capture_archived", "capture_reused"}
            and item.fencing_token <= current_fencing_token
            and item.request_payload_sha256 == request_payload_sha256
            and item.request_summary_sha256 == request_summary_sha256
            and item.instrument_id == instrument_id
        ]
        if not matching:
            return None
        latest_token = max(item.fencing_token for item in matching)
        latest = [item for item in matching if item.fencing_token == latest_token]
        manifests = {item.manifest_sha256 for item in latest}
        if len(manifests) != 1:
            raise SinaLrbRawCaptureError("recovery_ambiguous")
        starts = {
            item.fencing_token
            for item in receipts
            if item.event_type == "attempt_started"
            and item.request_payload_sha256 == request_payload_sha256
            and item.request_summary_sha256 == request_summary_sha256
            and item.instrument_id == instrument_id
        }
        if latest_token not in starts:
            raise SinaLrbRawCaptureError("recovery_invalid")
        selected = latest[0]
        if any(not _same_capture_receipt(selected, item) for item in latest[1:]):
            raise SinaLrbRawCaptureError("recovery_ambiguous")
        return selected

    def verify_capture_receipt(
        self,
        archived: SinaLrbArchivedCapture,
        *,
        request_payload_sha256: str,
        fencing_token: int,
    ) -> SinaLrbCaptureReceipt:
        """Require one signed event matching every published capture reference."""
        if type(archived) is not SinaLrbArchivedCapture:
            raise SinaLrbRawCaptureError("recovery_invalid")
        if (
            type(fencing_token) is not int
            or fencing_token < 1
            or archived.fencing_token != fencing_token
        ):
            raise SinaLrbRawCaptureError("recovery_invalid")
        receipts = self.events_for_run(archived.run_key)
        matching = [
            item
            for item in receipts
            if item.event_type in {"capture_archived", "capture_reused"}
            and item.fencing_token == fencing_token
            and item.request_payload_sha256 == request_payload_sha256
            and item.request_summary_sha256 == archived.request_summary_sha256
            and item.instrument_id == archived.instrument_id
            and item.manifest_sha256 == archived.manifest_sha256
            and item.response_sha256 == archived.response_sha256
            and item.status_code == archived.status_code
            and item.retrieved_at_utc == archived.retrieved_at_utc
            and item.captured_at_utc == archived.captured_at_utc
            and item.source_access_ref == archived.source_access_ref
            and item.raw_retention_ref == archived.raw_retention_ref
            and item.origin_fencing_token == archived.origin_fencing_token
        ]
        if not matching or any(
            not _same_capture_receipt(matching[0], item) for item in matching[1:]
        ):
            raise SinaLrbRawCaptureError("recovery_invalid")
        if not any(
            item.event_type == "attempt_started"
            and item.fencing_token == fencing_token
            and item.request_payload_sha256 == request_payload_sha256
            and item.request_summary_sha256 == archived.request_summary_sha256
            and item.instrument_id == archived.instrument_id
            for item in receipts
        ):
            raise SinaLrbRawCaptureError("recovery_invalid")
        if archived.origin_fencing_token is not None and not _origin_capture_receipt_exists(
            receipts,
            run_key=archived.run_key,
            request_payload_sha256=request_payload_sha256,
            request_summary_sha256=archived.request_summary_sha256,
            instrument_id=archived.instrument_id,
            origin_fencing_token=archived.origin_fencing_token,
            manifest_sha256=archived.manifest_sha256,
            response_sha256=archived.response_sha256,
            status_code=archived.status_code,
            retrieved_at_utc=archived.retrieved_at_utc,
            captured_at_utc=archived.captured_at_utc,
            source_access_ref=archived.source_access_ref,
            raw_retention_ref=archived.raw_retention_ref,
        ):
            raise SinaLrbRawCaptureError("recovery_invalid")
        return max(matching, key=lambda item: item.created_at_utc)

    def events_for_run(self, run_key: str) -> tuple[SinaLrbCaptureReceipt, ...]:
        """Read and verify every signed event below one run's private directory."""
        if type(run_key) is not str or _RUN_KEY.fullmatch(run_key) is None:
            raise SinaLrbRawCaptureError("invalid_request")
        try:
            with self._locked_event_directory(run_key) as directory_fd:
                return _read_verified_events(directory_fd, run_key, self._signer)
        except SinaLrbRawCaptureError:
            raise
        except Exception:
            raise SinaLrbRawCaptureError("recovery_unavailable") from None

    def audit_run(self, record: RunRecord) -> SinaLrbCaptureAuditReport:
        """Classify receipts without claiming a publication-to-manifest join.

        ``succeeded`` is authoritative for the fact that RunLedger committed a
        publication transaction. The ledger does not persist the capture digest,
        so this report deliberately leaves that exact join for review.
        """
        if type(record) is not RunRecord:
            raise TypeError("record must be a RunRecord")
        receipts = self.events_for_run(record.run_key)
        try:
            ledger_payload_hash = hashlib.sha256(
                record.job_snapshot_json.encode("utf-8", errors="strict")
            ).hexdigest()
        except (AttributeError, UnicodeEncodeError):
            raise SinaLrbRawCaptureError("recovery_invalid") from None
        if any(item.request_payload_sha256 != ledger_payload_hash for item in receipts):
            raise SinaLrbRawCaptureError("recovery_invalid")
        captures = tuple(
            sorted(
                {
                    item.manifest_sha256
                    for item in receipts
                    if item.event_type in {"capture_archived", "capture_reused"}
                    and item.manifest_sha256 is not None
                }
            )
        )
        attempts = tuple(
            sorted(
                {item.fencing_token for item in receipts if item.event_type == "attempt_started"}
            )
        )
        if record.status == "succeeded":
            disposition: Literal[
                "no_journal_evidence",
                "unlinked_artifact_possible",
                "unpublished_capture_candidate",
                "capture_present_on_nonterminal_run",
                "ledger_succeeded_link_requires_review",
            ] = "ledger_succeeded_link_requires_review"
        elif captures and record.status in {"queued", "failed", "cancelled"}:
            disposition = "unpublished_capture_candidate"
        elif captures:
            disposition = "capture_present_on_nonterminal_run"
        elif attempts:
            disposition = "unlinked_artifact_possible"
        else:
            disposition = "no_journal_evidence"
        return SinaLrbCaptureAuditReport(
            run_key=record.run_key,
            ledger_status=record.status,
            disposition=disposition,
            capture_manifest_sha256s=captures,
            attempt_fencing_tokens=attempts,
        )

    def _append_signed_event(self, payload: dict[str, object]) -> None:
        run_key = _required_text(payload, "run_key")
        try:
            with self._locked_event_directory(run_key) as directory_fd:
                existing = _read_verified_events(directory_fd, run_key, self._signer)
                receipt = _receipt_from_payload(payload)
                if receipt in existing:
                    return
                if receipt.event_type in {
                    "capture_archived",
                    "capture_reused",
                } and not _capture_receipt_origin_chains_are_valid((*existing, receipt)):
                    raise SinaLrbRawCaptureError("recovery_invalid")
                if len(existing) >= _MAX_JOURNAL_EVENTS_PER_RUN:
                    raise SinaLrbRawCaptureError("recovery_invalid")

                canonical_payload = _canonical_json_bytes(payload)
                try:
                    signature = self._signer.sign(canonical_payload)
                except Exception:
                    raise SinaLrbRawCaptureError("recovery_signing_failed") from None
                if type(signature) is not bytes or not 1 <= len(signature) <= _SIGNATURE_MAX_BYTES:
                    raise SinaLrbRawCaptureError("recovery_signing_failed")
                key_id = _validate_key_id(_required_text(payload, "key_id"))
                try:
                    verified = self._signer.verify(key_id, canonical_payload, signature)
                except Exception:
                    verified = False
                if verified is not True:
                    raise SinaLrbRawCaptureError("recovery_signature_invalid")
                envelope = {
                    "payload": payload,
                    "signature": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
                }
                encoded = _canonical_json_bytes(envelope)
                if len(encoded) > _JOURNAL_RECORD_MAX_BYTES:
                    raise SinaLrbRawCaptureError("recovery_invalid")
                digest = hashlib.sha256(encoded).hexdigest()
                name = f"{digest}.json"
                if name in os.listdir(directory_fd):
                    existing_bytes = _read_private_event(directory_fd, name)
                    if (
                        existing_bytes != encoded
                        or hashlib.sha256(existing_bytes).hexdigest() != digest
                    ):
                        raise SinaLrbRawCaptureError("recovery_invalid")
                    return
                if len(existing) >= _MAX_JOURNAL_EVENTS_PER_RUN:
                    raise SinaLrbRawCaptureError("recovery_invalid")
                _store_private_event(directory_fd, name, encoded, digest)
        except SinaLrbRawCaptureError:
            raise
        except Exception:
            raise SinaLrbRawCaptureError("recovery_unavailable") from None

    def _locked_event_directory(self, run_key: str) -> _LockedRunJournalDirectory:
        """Open the owner-private journal directories and lock one run's log."""
        return _LockedRunJournalDirectory(self._artifact_root, run_key)


def _read_verified_events(
    directory_fd: int,
    run_key: str,
    signer: CaptureReceiptSigner,
) -> tuple[SinaLrbCaptureReceipt, ...]:
    """Clean crash temps and read a bounded, fully verified per-run journal."""
    names = sorted(os.listdir(directory_fd))
    _remove_incomplete_journal_temps(directory_fd, names)
    names = sorted(os.listdir(directory_fd))
    if len(names) > _MAX_JOURNAL_EVENTS_PER_RUN or any(
        _EVENT_FILENAME.fullmatch(name) is None for name in names
    ):
        raise SinaLrbRawCaptureError("recovery_invalid")
    events: list[SinaLrbCaptureReceipt] = []
    for name in names:
        encoded = _read_private_event(directory_fd, name)
        if hashlib.sha256(encoded).hexdigest() != name[:-5]:
            raise SinaLrbRawCaptureError("recovery_invalid")
        payload = _decode_signed_event(encoded, signer)
        if payload.run_key != run_key:
            raise SinaLrbRawCaptureError("recovery_invalid")
        events.append(payload)
    if not _capture_receipt_origin_chains_are_valid(events):
        raise SinaLrbRawCaptureError("recovery_invalid")
    return tuple(events)


def _origin_capture_receipt_exists(
    receipts: tuple[SinaLrbCaptureReceipt, ...],
    *,
    run_key: str,
    request_payload_sha256: str,
    request_summary_sha256: str,
    instrument_id: str,
    origin_fencing_token: int,
    manifest_sha256: str,
    response_sha256: str,
    status_code: int,
    retrieved_at_utc: datetime,
    captured_at_utc: datetime,
    source_access_ref: str,
    raw_retention_ref: str,
) -> bool:
    """Require a signed, same-run origin receipt with an intact origin chain."""
    for receipt in receipts:
        if (
            receipt.event_type not in {"capture_archived", "capture_reused"}
            or receipt.fencing_token != origin_fencing_token
            or receipt.run_key != run_key
            or receipt.request_payload_sha256 != request_payload_sha256
            or receipt.request_summary_sha256 != request_summary_sha256
            or receipt.instrument_id != instrument_id
            or receipt.manifest_sha256 != manifest_sha256
            or receipt.response_sha256 != response_sha256
            or receipt.status_code != status_code
            or receipt.retrieved_at_utc != retrieved_at_utc
            or receipt.captured_at_utc != captured_at_utc
            or receipt.source_access_ref != source_access_ref
            or receipt.raw_retention_ref != raw_retention_ref
        ):
            continue
        return True
    return False


def _capture_receipt_origin_chains_are_valid(
    receipts: tuple[SinaLrbCaptureReceipt, ...] | list[SinaLrbCaptureReceipt],
) -> bool:
    """Validate all attempts and reuse links in fence order without recursion."""

    def artifact_key(receipt: SinaLrbCaptureReceipt) -> tuple[object, ...]:
        return (
            receipt.run_key,
            receipt.request_payload_sha256,
            receipt.request_summary_sha256,
            receipt.instrument_id,
            receipt.manifest_sha256,
            receipt.response_sha256,
            receipt.status_code,
            receipt.retrieved_at_utc,
            receipt.captured_at_utc,
            receipt.source_access_ref,
            receipt.raw_retention_ref,
        )

    attempts = {
        (
            item.run_key,
            item.request_payload_sha256,
            item.request_summary_sha256,
            item.instrument_id,
            item.fencing_token,
        )
        for item in receipts
        if item.event_type == "attempt_started"
    }
    captures = sorted(
        (item for item in receipts if item.event_type in {"capture_archived", "capture_reused"}),
        key=lambda item: item.fencing_token,
    )
    valid_origin_keys: set[tuple[int, tuple[object, ...]]] = set()
    for capture in captures:
        attempt_key = (
            capture.run_key,
            capture.request_payload_sha256,
            capture.request_summary_sha256,
            capture.instrument_id,
            capture.fencing_token,
        )
        if attempt_key not in attempts:
            return False
        if capture.event_type == "capture_archived":
            if capture.origin_fencing_token is not None:
                return False
            valid_origin_keys.add((capture.fencing_token, artifact_key(capture)))
            continue
        if (
            capture.origin_fencing_token is None
            or capture.origin_fencing_token >= capture.fencing_token
        ):
            return False
        if (capture.origin_fencing_token, artifact_key(capture)) not in valid_origin_keys:
            return False
        valid_origin_keys.add((capture.fencing_token, artifact_key(capture)))
    return True


class _LockedRunJournalDirectory:
    """Hold a per-run exclusive file lock and expose its immutable event folder."""

    def __init__(self, artifact_root: Path, run_key: str) -> None:
        self._artifact_root = artifact_root
        self._run_key = run_key
        self._descriptors: list[int] = []
        self._lock_descriptor: int | None = None
        self._event_descriptor: int | None = None

    def __enter__(self) -> int:
        """Open and lock the private run journal without following symlinks."""
        try:
            root_fd = _open_root_directory(self._artifact_root)
            self._descriptors.append(root_fd)
            _require_private_directory(root_fd)
            journal_fd = _open_private_child(root_fd, _JOURNAL_DIRECTORY)
            self._descriptors.append(journal_fd)
            _require_private_directory(journal_fd)
            runs_fd = _open_private_child(journal_fd, "runs")
            self._descriptors.append(runs_fd)
            _require_private_directory(runs_fd)
            run_fd = _open_private_child(runs_fd, self._run_key)
            self._descriptors.append(run_fd)
            _require_private_directory(run_fd)
            self._lock_descriptor = _open_private_lock(run_fd)
            fcntl.flock(self._lock_descriptor, fcntl.LOCK_EX)
            self._event_descriptor = _open_private_child(run_fd, "events")
            self._descriptors.append(self._event_descriptor)
            _require_private_directory(self._event_descriptor)
            return self._event_descriptor
        except BaseException:
            self._close()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Release the advisory lock and all descriptors on every path."""
        del exc_type, exc, traceback
        self._close()
        return False

    def _close(self) -> None:
        """Release descriptors in reverse order, including partial setup."""
        try:
            if self._lock_descriptor is not None:
                lock_descriptor = self._lock_descriptor
                self._lock_descriptor = None
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(lock_descriptor)
        finally:
            while self._descriptors:
                descriptor = self._descriptors.pop()
                os.close(descriptor)
            self._event_descriptor = None


def _open_private_lock(run_directory_fd: int) -> int:
    """Open the run lock as an owner-private regular file without symlinks."""
    name = "write.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=run_directory_fd)
    except OSError as exc:
        raise SinaLrbRawCaptureError("recovery_unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
        ):
            raise SinaLrbRawCaptureError("recovery_unavailable")
        os.fsync(run_directory_fd)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_private_directory(descriptor: int) -> None:
    """Require a current-user-owned 0700 directory for all journal components."""
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.geteuid()
    ):
        raise SinaLrbRawCaptureError("recovery_unavailable")


def _store_private_event(
    directory_fd: int,
    name: str,
    encoded: bytes,
    expected_sha256: str,
) -> None:
    """Publish one immutable event atomically and durably without clobbering."""
    if (
        _EVENT_FILENAME.fullmatch(name) is None
        or hashlib.sha256(encoded).hexdigest() != expected_sha256
    ):
        raise SinaLrbRawCaptureError("recovery_invalid")
    temporary_name = f".tmp-{secrets.token_hex(16)}"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        _unlink_journal_file(directory_fd, temporary_name)
        raise
    else:
        os.close(descriptor)
    try:
        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.fsync(directory_fd)
        except FileExistsError:
            _verify_file(
                directory_fd,
                name,
                expected_sha256,
                len(encoded),
                _JOURNAL_RECORD_MAX_BYTES,
            )
    finally:
        _unlink_journal_file(directory_fd, temporary_name)


def _unlink_journal_file(directory_fd: int, name: str) -> None:
    """Remove a private temporary event file and persist the directory update."""
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return
    os.fsync(directory_fd)


def _remove_incomplete_journal_temps(directory_fd: int, names: list[str]) -> None:
    """Remove only recognized private temp files left before atomic event publish."""
    removed = False
    for name in names:
        if not name.startswith(".tmp-"):
            continue
        if _JOURNAL_TEMP_FILENAME.fullmatch(name) is None:
            raise SinaLrbRawCaptureError("recovery_invalid")
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_nlink not in {1, 2}
        ):
            raise SinaLrbRawCaptureError("recovery_unavailable")
        os.unlink(name, dir_fd=directory_fd)
        removed = True
    if removed:
        os.fsync(directory_fd)


def _read_private_event(directory_fd: int, name: str) -> bytes:
    """Read one bounded owner-private event file without following a link."""
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise SinaLrbRawCaptureError("recovery_unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_size > _JOURNAL_RECORD_MAX_BYTES
        ):
            raise SinaLrbRawCaptureError("recovery_invalid")
        encoded = bytearray()
        while len(encoded) <= _JOURNAL_RECORD_MAX_BYTES:
            chunk = os.read(descriptor, min(4096, _JOURNAL_RECORD_MAX_BYTES + 1 - len(encoded)))
            if not chunk:
                break
            encoded.extend(chunk)
        if len(encoded) > _JOURNAL_RECORD_MAX_BYTES:
            raise SinaLrbRawCaptureError("recovery_invalid")
        return bytes(encoded)
    finally:
        os.close(descriptor)


def _canonical_json_bytes(value: object) -> bytes:
    """Encode strict canonical JSON for signing and content addressing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _receipt_payload(
    *,
    event_type: Literal["attempt_started", "capture_archived", "capture_reused"],
    run_key: str,
    request_payload_sha256: str,
    request_summary_sha256: str,
    instrument_id: str,
    fencing_token: int,
    created_at_utc: datetime,
    key_id: str,
    manifest_sha256: str | None = None,
    response_sha256: str | None = None,
    status_code: int | None = None,
    retrieved_at_utc: datetime | None = None,
    captured_at_utc: datetime | None = None,
    source_access_ref: str | None = None,
    raw_retention_ref: str | None = None,
    origin_fencing_token: int | None = None,
) -> dict[str, object]:
    """Validate and encode one signed event payload with fixed field names."""
    _validate_receipt_identity(
        run_key,
        request_payload_sha256,
        request_summary_sha256,
        instrument_id,
        fencing_token,
    )
    created_at = _validated_utc_datetime(created_at_utc)
    _validate_key_id(key_id)
    if event_type == "attempt_started":
        if any(
            value is not None
            for value in (
                manifest_sha256,
                response_sha256,
                status_code,
                retrieved_at_utc,
                captured_at_utc,
                source_access_ref,
                raw_retention_ref,
                origin_fencing_token,
            )
        ):
            raise SinaLrbRawCaptureError("recovery_invalid")
    else:
        if (
            type(manifest_sha256) is not str
            or _SHA256.fullmatch(manifest_sha256) is None
            or type(response_sha256) is not str
            or _SHA256.fullmatch(response_sha256) is None
            or type(status_code) is not int
            or status_code != 200
            or type(retrieved_at_utc) is not datetime
            or type(captured_at_utc) is not datetime
            or not _is_authorization_reference(source_access_ref, "source-auth")
            or not _is_authorization_reference(raw_retention_ref, "raw-retention")
        ):
            raise SinaLrbRawCaptureError("recovery_invalid")
        retrieved_at = _validated_utc_datetime(retrieved_at_utc)
        captured_at = _validated_utc_datetime(captured_at_utc)
        if captured_at < retrieved_at:
            raise SinaLrbRawCaptureError("recovery_invalid")
        if event_type == "capture_archived" and origin_fencing_token is not None:
            raise SinaLrbRawCaptureError("recovery_invalid")
        if event_type == "capture_reused" and (
            type(origin_fencing_token) is not int
            or origin_fencing_token < 1
            or origin_fencing_token >= fencing_token
        ):
            raise SinaLrbRawCaptureError("recovery_invalid")
    return {
        "journal_version": _JOURNAL_VERSION,
        "event_type": event_type,
        "run_key": run_key,
        "request_payload_sha256": request_payload_sha256,
        "request_summary_sha256": request_summary_sha256,
        "instrument_id": instrument_id,
        "fencing_token": fencing_token,
        "created_at_utc": _format_utc(created_at),
        "key_id": key_id,
        "manifest_sha256": manifest_sha256,
        "response_sha256": response_sha256,
        "status_code": status_code,
        "retrieved_at_utc": (
            _format_utc(_validated_utc_datetime(retrieved_at_utc))
            if retrieved_at_utc is not None
            else None
        ),
        "captured_at_utc": (
            _format_utc(_validated_utc_datetime(captured_at_utc))
            if captured_at_utc is not None
            else None
        ),
        "source_access_ref": source_access_ref,
        "raw_retention_ref": raw_retention_ref,
        "origin_fencing_token": origin_fencing_token,
    }


def _decode_signed_event(encoded: bytes, signer: CaptureReceiptSigner) -> SinaLrbCaptureReceipt:
    """Parse a canonical signed envelope, verify its signature, and validate fields."""

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        envelope = json.loads(encoded.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise SinaLrbRawCaptureError("recovery_invalid") from None
    if (
        type(envelope) is not dict
        or set(envelope) != {"payload", "signature"}
        or type(envelope.get("payload")) is not dict
        or set(envelope["payload"]) != _RECEIPT_FIELDS
        or type(envelope.get("signature")) is not str
        or _canonical_json_bytes(envelope) != encoded
    ):
        raise SinaLrbRawCaptureError("recovery_invalid")
    payload = envelope["payload"]
    signature_text = envelope["signature"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,2731}", signature_text, re.ASCII):
        raise SinaLrbRawCaptureError("recovery_invalid")
    try:
        signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
    except (binascii.Error, ValueError):
        raise SinaLrbRawCaptureError("recovery_invalid") from None
    if (
        not signature
        or len(signature) > _SIGNATURE_MAX_BYTES
        or base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=") != signature_text
    ):
        raise SinaLrbRawCaptureError("recovery_invalid")
    try:
        receipt = _receipt_from_payload(payload)
        signature_valid = signer.verify(receipt.key_id, _canonical_json_bytes(payload), signature)
    except SinaLrbRawCaptureError:
        raise
    except Exception:
        raise SinaLrbRawCaptureError("recovery_signature_invalid") from None
    if signature_valid is not True:
        raise SinaLrbRawCaptureError("recovery_signature_invalid")
    return receipt


def _receipt_from_payload(payload: dict[str, object]) -> SinaLrbCaptureReceipt:
    """Validate the exact serialized event schema and return its typed view."""
    if (
        set(payload) != _RECEIPT_FIELDS
        or type(payload.get("journal_version")) is not int
        or payload.get("journal_version") != _JOURNAL_VERSION
    ):
        raise SinaLrbRawCaptureError("recovery_invalid")
    event_type_value = payload.get("event_type")
    if event_type_value == "attempt_started":
        event_type: Literal["attempt_started", "capture_archived", "capture_reused"] = (
            "attempt_started"
        )
    elif event_type_value == "capture_archived":
        event_type = "capture_archived"
    elif event_type_value == "capture_reused":
        event_type = "capture_reused"
    else:
        raise SinaLrbRawCaptureError("recovery_invalid")
    try:
        created_at = _parse_canonical_utc(payload.get("created_at_utc"))
        retrieved_at_value = payload.get("retrieved_at_utc")
        captured_at_value = payload.get("captured_at_utc")
        run_key = _required_text(payload, "run_key")
        request_payload_sha256 = _required_text(payload, "request_payload_sha256")
        request_summary_sha256 = _required_text(payload, "request_summary_sha256")
        instrument_id = _required_text(payload, "instrument_id")
        fencing_token = _required_integer(payload, "fencing_token")
        key_id = _required_text(payload, "key_id")
        manifest_sha256 = _optional_text(payload, "manifest_sha256")
        response_sha256 = _optional_text(payload, "response_sha256")
        status_code = _optional_integer(payload, "status_code")
        source_access_ref = _optional_text(payload, "source_access_ref")
        raw_retention_ref = _optional_text(payload, "raw_retention_ref")
        origin_fencing_token = _optional_integer(payload, "origin_fencing_token")
        parsed = _receipt_payload(
            event_type=event_type,
            run_key=run_key,
            request_payload_sha256=request_payload_sha256,
            request_summary_sha256=request_summary_sha256,
            instrument_id=instrument_id,
            fencing_token=fencing_token,
            created_at_utc=created_at,
            key_id=key_id,
            manifest_sha256=manifest_sha256,
            response_sha256=response_sha256,
            status_code=status_code,
            retrieved_at_utc=(
                _parse_canonical_utc(retrieved_at_value) if retrieved_at_value is not None else None
            ),
            captured_at_utc=(
                _parse_canonical_utc(captured_at_value) if captured_at_value is not None else None
            ),
            source_access_ref=source_access_ref,
            raw_retention_ref=raw_retention_ref,
            origin_fencing_token=origin_fencing_token,
        )
    except (TypeError, ValueError, OverflowError):
        raise SinaLrbRawCaptureError("recovery_invalid") from None
    if parsed != payload:
        raise SinaLrbRawCaptureError("recovery_invalid")
    return SinaLrbCaptureReceipt(
        event_type=event_type,
        run_key=run_key,
        request_payload_sha256=request_payload_sha256,
        request_summary_sha256=request_summary_sha256,
        instrument_id=instrument_id,
        fencing_token=fencing_token,
        created_at_utc=created_at,
        key_id=key_id,
        manifest_sha256=manifest_sha256,
        response_sha256=response_sha256,
        status_code=status_code,
        retrieved_at_utc=(
            _parse_canonical_utc(retrieved_at_value) if retrieved_at_value is not None else None
        ),
        captured_at_utc=(
            _parse_canonical_utc(captured_at_value) if captured_at_value is not None else None
        ),
        source_access_ref=source_access_ref,
        raw_retention_ref=raw_retention_ref,
        origin_fencing_token=origin_fencing_token,
    )


def _required_text(payload: dict[str, object], field: str) -> str:
    """Read one strict string field from a signed payload."""
    value = payload.get(field)
    if type(value) is not str:
        raise SinaLrbRawCaptureError("recovery_invalid")
    return value


def _optional_text(payload: dict[str, object], field: str) -> str | None:
    """Read one optional strict string field from a signed payload."""
    value = payload.get(field)
    if value is not None and type(value) is not str:
        raise SinaLrbRawCaptureError("recovery_invalid")
    return value


def _required_integer(payload: dict[str, object], field: str) -> int:
    """Read one strict integer field without accepting boolean values."""
    value = payload.get(field)
    if type(value) is not int:
        raise SinaLrbRawCaptureError("recovery_invalid")
    return value


def _optional_integer(payload: dict[str, object], field: str) -> int | None:
    """Read one optional strict integer field without accepting booleans."""
    value = payload.get(field)
    if value is not None and type(value) is not int:
        raise SinaLrbRawCaptureError("recovery_invalid")
    return value


def _validate_receipt_identity(
    run_key: object,
    request_payload_sha256: object,
    request_summary_sha256: object,
    instrument_id: object,
    fencing_token: object,
) -> None:
    """Validate strict run, request, instrument and fencing identity fields."""
    if (
        type(run_key) is not str
        or _RUN_KEY.fullmatch(run_key) is None
        or type(request_payload_sha256) is not str
        or _SHA256.fullmatch(request_payload_sha256) is None
        or type(request_summary_sha256) is not str
        or _SHA256.fullmatch(request_summary_sha256) is None
        or type(instrument_id) is not str
        or type(fencing_token) is not int
        or fencing_token < 1
    ):
        raise SinaLrbRawCaptureError("recovery_invalid")
    try:
        if SinaLrbQuery(instrument_id).symbol != instrument_id:
            raise ValueError
    except Exception:
        raise SinaLrbRawCaptureError("recovery_invalid") from None
    if sina_lrb_request_summary_sha256(instrument_id) != request_summary_sha256:
        raise SinaLrbRawCaptureError("recovery_invalid")


def _validate_key_id(value: object) -> str:
    """Require a short stable signer key identifier without credentials."""
    if (
        type(value) is not str
        or _KEY_ID.fullmatch(value) is None
        or _SECRET_SHAPED_REFERENCE.search(value)
    ):
        raise SinaLrbRawCaptureError("recovery_signing_failed")
    return value


def _format_utc(value: datetime) -> str:
    """Format a canonical UTC timestamp with microsecond precision."""
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_canonical_utc(value: object) -> datetime:
    """Parse one canonical UTC timestamp without accepting alternate offsets."""
    if type(value) is not str or not value.endswith("Z"):
        raise SinaLrbRawCaptureError("recovery_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        normalized = _validated_utc_datetime(parsed)
    except Exception:
        raise SinaLrbRawCaptureError("recovery_invalid") from None
    if _format_utc(normalized) != value:
        raise SinaLrbRawCaptureError("recovery_invalid")
    return normalized


def _same_capture_receipt(left: SinaLrbCaptureReceipt, right: SinaLrbCaptureReceipt) -> bool:
    """Compare all capture identity fields, ignoring event time and key rotation."""
    return (
        left.event_type == right.event_type
        and left.run_key == right.run_key
        and left.request_payload_sha256 == right.request_payload_sha256
        and left.request_summary_sha256 == right.request_summary_sha256
        and left.instrument_id == right.instrument_id
        and left.fencing_token == right.fencing_token
        and left.manifest_sha256 == right.manifest_sha256
        and left.response_sha256 == right.response_sha256
        and left.status_code == right.status_code
        and left.retrieved_at_utc == right.retrieved_at_utc
        and left.captured_at_utc == right.captured_at_utc
        and left.source_access_ref == right.source_access_ref
        and left.raw_retention_ref == right.raw_retention_ref
        and left.origin_fencing_token == right.origin_fencing_token
    )


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
    _task: TaskDefinition | None
    _journal: SinaLrbCaptureRecoveryJournal | None
    _reuse_prior_captures: bool

    __slots__ = (
        "_delegate",
        "_writer",
        "_reader",
        "_authorization",
        "_retention_class",
        "_source_history_limit",
        "_task",
        "_journal",
        "_reuse_prior_captures",
    )

    def __init__(
        self,
        delegate: ProcessFetchHandler,
        *,
        artifact_root: Path | str,
        authorization: SinaLrbRawAuthorization,
        retention_class: RetentionClass,
        source_history_limit: HistoryLimit,
        task: TaskDefinition | None = None,
        receipt_signer: CaptureReceiptSigner | None = None,
        reuse_prior_captures: bool = False,
    ) -> None:
        """Bind an existing process handler and explicit raw-retention policy.

        Args:
            delegate: Fixed reviewed LRB process handler.
            artifact_root: Explicit private directory for content-addressed raw
                objects and manifests. It is never inferred from environment.
            authorization: Separate source-access and raw-retention decisions.
            retention_class: Explicit declared retention classification.
            source_history_limit: Explicit source-history boundary declaration.
            task: Exact task metadata used to verify the canonical run key when
                a signed receipt journal is enabled.
            receipt_signer: Externally managed signer. When provided, attempts
                and completed captures are persisted as signed audit events.
            reuse_prior_captures: Explicit opt-in to reuse one matching capture
                from an earlier fencing token after verifying its signature,
                run identity, manifest and body. It does not claim a new lease.

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
        if task is not None and not isinstance(task, TaskDefinition):
            raise TypeError("task must be a TaskDefinition")
        if type(reuse_prior_captures) is not bool:
            raise TypeError("reuse_prior_captures must be a bool")
        if receipt_signer is not None and task is None:
            raise ValueError("task is required when a signed capture journal is enabled")
        if reuse_prior_captures and receipt_signer is None:
            raise ValueError("a receipt signer is required to reuse prior captures")
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
        self._task = task
        self._journal = (
            SinaLrbCaptureRecoveryJournal(artifact_root, receipt_signer)
            if receipt_signer is not None
            else None
        )
        self._reuse_prior_captures = reuse_prior_captures

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

        request_payload_sha256: str | None = None
        context.checkpoint()
        if self._task is not None:
            try:
                identity_run_key, request_payload_sha256 = _sina_lrb_request_identity(
                    self._task,
                    request_snapshot,
                    expected_run_key=run_key,
                    snapshot_json=context._verified_snapshot_json(),
                )
            except Exception:
                raise SinaLrbRawCaptureError("invalid_request") from None
            if identity_run_key != run_key:
                raise SinaLrbRawCaptureError("invalid_request")
        elif context._verified_snapshot_json() is not None:
            raise SinaLrbRawCaptureError("invalid_request")

        if self._journal is not None:
            if self._task is None or request_payload_sha256 is None:
                raise SinaLrbRawCaptureError("recovery_invalid")
            context.checkpoint()
            self._journal.record_attempt_started(
                run_key=run_key,
                request_payload_sha256=request_payload_sha256,
                request_summary_sha256=request_digest,
                instrument_id=instrument_id,
                fencing_token=context.fencing_token,
            )
            context.checkpoint()
            if self._reuse_prior_captures:
                previous = self._journal.reusable_capture(
                    run_key=run_key,
                    request_payload_sha256=request_payload_sha256,
                    request_summary_sha256=request_digest,
                    instrument_id=instrument_id,
                    current_fencing_token=context.fencing_token,
                )
                if previous is not None:
                    recovered = self._recovered_capture(
                        previous,
                        run_key=run_key,
                        request_payload_sha256=request_payload_sha256,
                        current_fencing_token=context.fencing_token,
                    )
                    context.checkpoint()
                    try:
                        read_sina_lrb_archived_evidence(
                            recovered,
                            artifact_root=self._writer.artifact_root,
                        )
                    except SinaLrbRawCaptureError:
                        raise
                    context.checkpoint()
                    if previous.fencing_token < context.fencing_token:
                        self._journal.record_capture(
                            recovered,
                            task=self._task,
                            request=request_snapshot,
                            request_payload_sha256=request_payload_sha256,
                            fencing_token=context.fencing_token,
                            context=context,
                            origin_fencing_token=previous.fencing_token,
                        )
                    context.checkpoint()
                    return recovered

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
        archived = SinaLrbArchivedCapture(
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
            request_payload_sha256=request_payload_sha256,
            fencing_token=context.fencing_token if request_payload_sha256 is not None else None,
        )
        context.checkpoint()
        if self._journal is not None:
            if self._task is None or request_payload_sha256 is None:
                raise SinaLrbRawCaptureError("recovery_invalid")
            self._journal.record_capture(
                archived,
                task=self._task,
                request=request_snapshot,
                request_payload_sha256=request_payload_sha256,
                fencing_token=context.fencing_token,
                context=context,
            )
        context.checkpoint()
        return archived

    @staticmethod
    def _recovered_capture(
        receipt: SinaLrbCaptureReceipt,
        *,
        run_key: str,
        request_payload_sha256: str,
        current_fencing_token: int,
    ) -> SinaLrbArchivedCapture:
        """Bind a verified old receipt to the currently held lease attempt."""
        if (
            receipt.event_type not in {"capture_archived", "capture_reused"}
            or receipt.run_key != run_key
            or receipt.request_payload_sha256 != request_payload_sha256
            or receipt.manifest_sha256 is None
            or receipt.response_sha256 is None
            or receipt.status_code != 200
            or receipt.retrieved_at_utc is None
            or receipt.captured_at_utc is None
            or receipt.source_access_ref is None
            or receipt.raw_retention_ref is None
            or receipt.fencing_token > current_fencing_token
        ):
            raise SinaLrbRawCaptureError("recovery_invalid")
        return SinaLrbArchivedCapture(
            instrument_id=receipt.instrument_id,
            run_key=run_key,
            status_code=receipt.status_code,
            retrieved_at_utc=receipt.retrieved_at_utc,
            captured_at_utc=receipt.captured_at_utc,
            request_summary_sha256=receipt.request_summary_sha256,
            response_sha256=receipt.response_sha256,
            manifest_sha256=receipt.manifest_sha256,
            source_access_ref=receipt.source_access_ref,
            raw_retention_ref=receipt.raw_retention_ref,
            request_payload_sha256=request_payload_sha256,
            fencing_token=current_fencing_token,
            origin_fencing_token=(
                receipt.fencing_token
                if receipt.fencing_token < current_fencing_token
                else receipt.origin_fencing_token
            ),
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


def _sina_lrb_request_identity(
    task: TaskDefinition,
    request: RunRequest,
    *,
    expected_run_key: str,
    snapshot_json: str | None,
) -> tuple[str, str]:
    """Bind a manual LRB request to its identity or verified RunService snapshot.

    Standalone callers retain the ordinary ``build_run_identity`` semantics,
    including user tokens that happen to start with ``sha256-``. A persisted
    RunService snapshot has already replaced the original token with a synthetic
    ``sha256-<digest>`` value; that value is interpreted only when the trusted
    context supplies the canonical snapshot, whose task, request, slot key and
    payload digest are independently revalidated here.
    """
    request_snapshot, _instrument_id = _snapshot_manual_request(request)
    try:
        identity = build_run_identity(task, request_snapshot)
    except Exception:
        raise SinaLrbRawCaptureError("invalid_request") from None
    if snapshot_json is None:
        if identity.idempotency_key != expected_run_key:
            raise SinaLrbRawCaptureError("invalid_request")
        return identity.idempotency_key, identity.payload_hash

    if type(snapshot_json) is not str:
        raise SinaLrbRawCaptureError("invalid_request")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate snapshot key")
            result[key] = value
        return result

    try:
        encoded_snapshot = snapshot_json.encode("utf-8")
        snapshot_value = json.loads(
            snapshot_json,
            object_pairs_hook=reject_duplicate_keys,
        )
        if type(snapshot_value) is not dict or (
            _canonical_json_bytes(snapshot_value) != encoded_snapshot
        ):
            raise ValueError("snapshot is not canonical")
    except (
        UnicodeEncodeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ):
        raise SinaLrbRawCaptureError("invalid_request") from None

    if (
        set(snapshot_value) != {"contract_version", "job", "run"}
        or type(snapshot_value.get("contract_version")) is not int
        or snapshot_value.get("contract_version") != task.contract_version
    ):
        raise SinaLrbRawCaptureError("invalid_request")
    job_value = snapshot_value.get("job")
    run_value = snapshot_value.get("run")
    if (
        type(job_value) is not dict
        or set(job_value) != {"job_id", "version", "dataset_id", "provider", "model"}
        or job_value
        != {
            "job_id": task.job_id,
            "version": task.version,
            "dataset_id": task.dataset_id,
            "provider": task.provider,
            "model": task.model,
        }
        or type(run_value) is not dict
        or set(run_value)
        != {
            "run_type",
            "occurrence",
            "request_key_sha256",
            "backfill_range",
            "shard",
            "business_request",
        }
    ):
        raise SinaLrbRawCaptureError("invalid_request")

    request_digest = run_value.get("request_key_sha256")
    business_request = run_value.get("business_request")
    if (
        type(request_digest) is not str
        or _SHA256.fullmatch(request_digest) is None
        or request_snapshot.idempotency_key != f"sha256-{request_digest}"
        or run_value.get("run_type") != "manual"
        or run_value.get("occurrence") is not None
        or run_value.get("backfill_range") is not None
        or run_value.get("shard") != request_snapshot.shard
        or type(business_request) is not dict
        or set(business_request) != {"query", "partition"}
        or business_request.get("query") != request_snapshot.query
        or business_request.get("partition") != request_snapshot.partition
    ):
        raise SinaLrbRawCaptureError("invalid_request")

    slot_payload = {
        "contract_version": task.contract_version,
        "job_id": task.job_id,
        "version": task.version,
        "run_type": "manual",
        "request_key_sha256": request_digest,
    }
    rebuilt_run_key = hashlib.sha256(_canonical_json_bytes(slot_payload)).hexdigest()
    payload_sha256 = hashlib.sha256(encoded_snapshot).hexdigest()
    if rebuilt_run_key != expected_run_key:
        raise SinaLrbRawCaptureError("invalid_request")
    return rebuilt_run_key, payload_sha256


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
