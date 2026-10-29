"""Persist one revalidated temporal identity staging candidate.

This module performs no configuration, file, environment or network I/O. The
caller injects an already configured synchronous snapshot store. A successful
store ``put`` and the later validated readback are separate transactions; a
readback failure can therefore follow a committed write. Retrying the exact
same candidate is safe because the store treats an exact immutable version as
an idempotent replay. Importing never writes or publishes the candidate pin.

All candidate bytes and declarations remain caller supplied. Validation proves
canonical internal consistency and exact byte bindings only; it does not prove
source authenticity, rights, universe completeness or historical coverage.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from opendata.data.temporal_identity_pins import (
    MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES,
    MAX_TEMPORAL_IDENTITY_PINS,
    TemporalIdentityPin,
    TemporalIdentityPinDocumentError,
    decode_temporal_identity_pin_document,
)
from opendata.data.temporal_identity_staging import TemporalIdentityStagingResult
from opendata.data.temporal_identity_store import (
    TemporalIdentitySnapshotConflictError,
    TemporalIdentitySnapshotReceipt,
    TemporalIdentitySnapshotStore,
    TemporalIdentitySnapshotStoreError,
    TemporalIdentitySnapshotValidationError,
)
from opendata.data.temporal_instrument_identity_snapshot import (
    MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES,
    TemporalIdentitySnapshotError,
    decode_temporal_identity_snapshot,
    encode_temporal_identity_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opendata.data.temporal_instrument_identity import TemporalIdentityRegistry

__all__ = [
    "TemporalIdentityImportConflictError",
    "TemporalIdentityImportError",
    "TemporalIdentityImportReadbackError",
    "TemporalIdentityImportReceipt",
    "TemporalIdentityImportValidationError",
    "TemporalIdentityImportWriteOutcomeUnknownError",
    "import_temporal_identity_candidate",
]

_UNVERIFIED: Literal["caller_declared_unverified"] = "caller_declared_unverified"


class TemporalIdentityImportError(RuntimeError):
    """Base class with a stable, content-free import failure code."""

    write_may_have_committed = False
    retry_same_candidate = False

    def __init__(self, code: str) -> None:
        """Create an error without copying caller bytes or exception details."""
        self.code = code
        super().__init__(code)


class TemporalIdentityImportValidationError(ValueError):
    """Report invalid candidate material before any store write is attempted."""

    write_may_have_committed = False
    retry_same_candidate = False

    def __init__(self, code: str) -> None:
        """Create a fixed validation error that does not echo candidate values."""
        self.code = code
        super().__init__(code)


class TemporalIdentityImportConflictError(TemporalIdentityImportError):
    """Report that the immutable version key already contains different bytes."""


class TemporalIdentityImportWriteOutcomeUnknownError(TemporalIdentityImportError):
    """Report a store failure whose transaction outcome could not be established.

    The exception means the caller may safely retry the exact same candidate.
    The store either commits it once or recognizes the retry as an exact replay.
    """

    write_may_have_committed = True
    retry_same_candidate = True


class TemporalIdentityImportReadbackError(TemporalIdentityImportError):
    """Report an unverified post-write readback that may follow a committed put."""

    write_may_have_committed = True
    retry_same_candidate = True


@dataclass(frozen=True, slots=True)
class TemporalIdentityImportReceipt:
    """Describe a persisted candidate whose exact bytes passed public readback."""

    snapshot_receipt: TemporalIdentitySnapshotReceipt
    pin_file_sha256: str
    pin_count: int
    readback_verified: bool
    pin_published: bool


@dataclass(frozen=True, slots=True)
class _VerifiedCandidate:
    """Private validated values needed by the transaction and exact readback."""

    snapshot_bytes: bytes = field(repr=False)
    registry: TemporalIdentityRegistry = field(repr=False)
    candidate_pin: TemporalIdentityPin
    pin_file_sha256: str
    pin_count: int


def import_temporal_identity_candidate(
    candidate: TemporalIdentityStagingResult,
    *,
    store: TemporalIdentitySnapshotStore,
) -> TemporalIdentityImportReceipt:
    """Persist a fully revalidated candidate and compare its complete readback.

    The function never trusts that a ``TemporalIdentityStagingResult`` came from
    the staging helper. It recomputes both complete digests, decodes and
    byte-round-trips the canonical snapshot, parses the complete pin document,
    checks canonical pin JSON and verifies that exactly one document pin equals
    the candidate pin. The snapshot store then independently validates the
    canonical snapshot on ``put`` and on its public ``get_bytes`` readback.

    Args:
        candidate: Staging result, which may have been constructed or altered by
            any caller and is therefore treated as untrusted input.
        store: Already injected synchronous store backed by an explicitly
            configured and migrated SQLite or MySQL engine.

    Returns:
        A metadata-only receipt after byte-for-byte readback succeeds. The pin
        document is not written, installed or published.

    Raises:
        TemporalIdentityImportValidationError: If candidate bytes or bindings
            are invalid; this occurs before the store is called.
        TemporalIdentityImportConflictError: If the immutable key contains other
            canonical bytes; no replacement is attempted.
        TemporalIdentityImportWriteOutcomeUnknownError: If ``put`` fails and its
            transaction outcome cannot be established. Retrying the same bytes
            is safe.
        TemporalIdentityImportReadbackError: If post-write receipt or readback
            verification fails. The put may already be committed; exact replay is
            safe and no pin is published.
    """
    verified = _validate_candidate(candidate)
    if not isinstance(store, TemporalIdentitySnapshotStore):
        raise TemporalIdentityImportValidationError("injected_store_invalid")

    pin = verified.candidate_pin
    try:
        receipt = store.put(
            authority_id=pin.authority_id,
            registry_id=pin.registry_id,
            registry=verified.registry,
            expected_sha256=pin.snapshot_sha256,
        )
    except TemporalIdentitySnapshotConflictError:
        raise TemporalIdentityImportConflictError("immutable_version_conflict") from None
    except TemporalIdentitySnapshotValidationError:
        raise TemporalIdentityImportValidationError("store_rejected_candidate") from None
    except TemporalIdentitySnapshotStoreError:
        raise TemporalIdentityImportWriteOutcomeUnknownError(
            "store_write_outcome_unknown"
        ) from None
    except Exception:
        raise TemporalIdentityImportWriteOutcomeUnknownError(
            "store_write_outcome_unknown"
        ) from None

    try:
        stored_bytes = store.get_bytes(
            authority_id=pin.authority_id,
            registry_id=pin.registry_id,
            reference_version=pin.reference_version,
            expected_sha256=pin.snapshot_sha256,
        )
    except Exception:
        raise TemporalIdentityImportReadbackError("snapshot_readback_failed") from None
    if type(stored_bytes) is not bytes or not hmac.compare_digest(
        stored_bytes, verified.snapshot_bytes
    ):
        raise TemporalIdentityImportReadbackError("snapshot_readback_mismatch")
    if not _receipt_matches(receipt, verified):
        raise TemporalIdentityImportReadbackError("store_receipt_mismatch")

    return TemporalIdentityImportReceipt(
        snapshot_receipt=receipt,
        pin_file_sha256=verified.pin_file_sha256,
        pin_count=verified.pin_count,
        readback_verified=True,
        pin_published=False,
    )


def _validate_candidate(candidate: TemporalIdentityStagingResult) -> _VerifiedCandidate:
    """Recompute all byte digests, codec round trips and pin relationships."""
    if type(candidate) is not TemporalIdentityStagingResult:
        raise TemporalIdentityImportValidationError("staging_result_invalid")
    snapshot_bytes = candidate.snapshot_bytes
    pin_document_bytes = candidate.pin_document_bytes
    if type(snapshot_bytes) is not bytes:
        raise TemporalIdentityImportValidationError("snapshot_bytes_invalid")
    if not snapshot_bytes or len(snapshot_bytes) > MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES:
        raise TemporalIdentityImportValidationError("snapshot_size_invalid")
    computed_snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    if not _matches_digest(candidate.snapshot_sha256, computed_snapshot_sha256):
        raise TemporalIdentityImportValidationError("snapshot_digest_mismatch")
    try:
        registry = decode_temporal_identity_snapshot(snapshot_bytes)
        canonical_snapshot_bytes = encode_temporal_identity_snapshot(registry)
    except TemporalIdentitySnapshotError:
        raise TemporalIdentityImportValidationError("snapshot_invalid") from None
    if canonical_snapshot_bytes != snapshot_bytes:
        raise TemporalIdentityImportValidationError("snapshot_not_canonical")

    pin = _validate_candidate_pin(candidate.candidate_pin)
    _validate_snapshot_binding(registry, pin, computed_snapshot_sha256)

    if type(pin_document_bytes) is not bytes:
        raise TemporalIdentityImportValidationError("pin_document_bytes_invalid")
    if not pin_document_bytes or len(pin_document_bytes) > MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES:
        raise TemporalIdentityImportValidationError("pin_document_size_invalid")
    computed_pin_sha256 = hashlib.sha256(pin_document_bytes).hexdigest()
    if not _matches_digest(candidate.pin_file_sha256, computed_pin_sha256):
        raise TemporalIdentityImportValidationError("pin_document_digest_mismatch")
    try:
        pins = decode_temporal_identity_pin_document(pin_document_bytes, computed_pin_sha256)
    except TemporalIdentityPinDocumentError:
        raise TemporalIdentityImportValidationError("pin_document_invalid") from None
    if _encode_canonical_pin_document(pins) != pin_document_bytes:
        raise TemporalIdentityImportValidationError("pin_document_not_canonical")
    candidate_matches = tuple(existing for existing in pins if existing == pin)
    if len(candidate_matches) != 1:
        raise TemporalIdentityImportValidationError("candidate_pin_binding_mismatch")

    _validate_result_counts(candidate, len(registry.assignments), len(pins))
    if (
        type(candidate.verification_status) is not str
        or candidate.verification_status != _UNVERIFIED
    ):
        raise TemporalIdentityImportValidationError("verification_status_invalid")
    return _VerifiedCandidate(
        snapshot_bytes=snapshot_bytes,
        registry=registry,
        candidate_pin=pin,
        pin_file_sha256=computed_pin_sha256,
        pin_count=len(pins),
    )


def _validate_candidate_pin(candidate_pin: object) -> TemporalIdentityPin:
    """Require the exact shared immutable pin model with strict field values."""
    if type(candidate_pin) is not TemporalIdentityPin:
        raise TemporalIdentityImportValidationError("candidate_pin_invalid")
    try:
        validated = TemporalIdentityPin.model_validate(
            candidate_pin.model_dump(mode="python"), strict=True
        )
    except (ValidationError, TypeError, ValueError):
        raise TemporalIdentityImportValidationError("candidate_pin_invalid") from None
    if validated != candidate_pin:
        raise TemporalIdentityImportValidationError("candidate_pin_invalid")
    return validated


def _validate_snapshot_binding(
    registry: TemporalIdentityRegistry,
    pin: TemporalIdentityPin,
    snapshot_sha256: str,
) -> None:
    """Check version, exact digest and source assignment consistency only."""
    if pin.reference_version != registry.reference_version:
        raise TemporalIdentityImportValidationError("reference_version_mismatch")
    if not hmac.compare_digest(pin.snapshot_sha256, snapshot_sha256):
        raise TemporalIdentityImportValidationError("candidate_snapshot_binding_mismatch")
    if not registry.assignments:
        raise TemporalIdentityImportValidationError("snapshot_source_unverifiable")
    if any(assignment.source_id != pin.source_id for assignment in registry.assignments):
        raise TemporalIdentityImportValidationError("source_id_mismatch")


def _encode_canonical_pin_document(pins: Sequence[TemporalIdentityPin]) -> bytes:
    """Serialize the single strict runtime v1 pin shape deterministically."""
    document = {
        "schema_version": 1,
        "pins": [
            pin.model_dump(mode="json") for pin in sorted(pins, key=lambda item: item.dataset_id)
        ],
    }
    try:
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise TemporalIdentityImportValidationError("pin_document_invalid") from None


def _validate_result_counts(
    candidate: TemporalIdentityStagingResult,
    assignment_count: int,
    pin_count: int,
) -> None:
    """Reject stale or structurally impossible informational staging counts."""
    if (
        type(candidate.assignment_count) is not int
        or candidate.assignment_count != assignment_count
    ):
        raise TemporalIdentityImportValidationError("assignment_count_mismatch")
    if type(candidate.pin_count) is not int or candidate.pin_count != pin_count:
        raise TemporalIdentityImportValidationError("pin_count_mismatch")
    existing_pin_count = candidate.existing_pin_count
    if (
        type(existing_pin_count) is not int
        or not 0 <= existing_pin_count <= MAX_TEMPORAL_IDENTITY_PINS
        or not max(0, pin_count - 1) <= existing_pin_count <= pin_count
    ):
        raise TemporalIdentityImportValidationError("existing_pin_count_invalid")


def _matches_digest(value: object, computed: str) -> bool:
    """Compare one digest field without coercion or content-dependent messages."""
    return type(value) is str and hmac.compare_digest(value, computed)


def _receipt_matches(
    receipt: object,
    candidate: _VerifiedCandidate,
) -> bool:
    """Require the put receipt to describe exactly the requested immutable bytes."""
    if type(receipt) is not TemporalIdentitySnapshotReceipt:
        return False
    if (
        type(receipt.authority_id) is not str
        or type(receipt.registry_id) is not str
        or type(receipt.reference_version) is not str
        or type(receipt.snapshot_sha256) is not str
        or type(receipt.total_bytes) is not int
        or type(receipt.chunk_count) is not int
        or type(receipt.created_at_us) is not int
        or type(receipt.already_existed) is not bool
    ):
        return False
    pin = candidate.candidate_pin
    return (
        receipt.authority_id == pin.authority_id
        and receipt.registry_id == pin.registry_id
        and receipt.reference_version == pin.reference_version
        and hmac.compare_digest(receipt.snapshot_sha256, pin.snapshot_sha256)
        and receipt.total_bytes == len(candidate.snapshot_bytes)
        and receipt.chunk_count > 0
        and receipt.created_at_us >= 0
    )
