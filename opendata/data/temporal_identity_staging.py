"""Pure offline preparation for one explicitly declared temporal identity pin.

The staging function accepts immutable snapshot and optional pin-document bytes.
It performs no file, environment, database, or network I/O. Its SHA-256 values
bind exact bytes only. All supplied identifiers, source associations, and
coverage remain caller-declared and unverified; staging does not establish
source authenticity, rights, completeness, or production readiness.
"""

from __future__ import annotations

import hashlib
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
    decode_temporal_identity_pin_registry,
)
from opendata.data.temporal_instrument_identity_snapshot import (
    MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES,
    TemporalIdentitySnapshotError,
    decode_temporal_identity_snapshot,
    encode_temporal_identity_snapshot,
)

if TYPE_CHECKING:
    from opendata.data.temporal_instrument_identity import TemporalIdentityRegistry

__all__ = [
    "TemporalIdentityStagingError",
    "TemporalIdentityStagingResult",
    "stage_temporal_identity_pin",
]

_STAGING_VERIFICATION: Literal["caller_declared_unverified"] = "caller_declared_unverified"


class TemporalIdentityStagingError(ValueError):
    """Report a fixed staging failure code without echoing supplied contents."""

    def __init__(self, code: str) -> None:
        """Create one content-free error suitable for local command output."""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TemporalIdentityStagingResult:
    """Hold canonical candidate material derived from explicit caller inputs.

    The byte fields are excluded from ``repr`` because snapshots and merged pin
    documents can be large and may contain caller-supplied evidence references.
    They remain immutable ``bytes`` values so an importer can revalidate them.
    """

    pin_document_bytes: bytes = field(repr=False)
    snapshot_sha256: str
    pin_file_sha256: str
    assignment_count: int
    existing_pin_count: int
    pin_count: int
    verification_status: Literal["caller_declared_unverified"]
    snapshot_bytes: bytes = field(default=b"", repr=False)
    candidate_pin: TemporalIdentityPin | None = None


def stage_temporal_identity_pin(
    snapshot_bytes: bytes,
    existing_pin_document_bytes: bytes | None,
    *,
    dataset_id: str,
    source_id: str,
    authority_id: str,
    registry_id: str,
    reference_version: str,
    replace_dataset_id: str | None = None,
) -> TemporalIdentityStagingResult:
    """Build a canonical runtime-compatible pin document without performing I/O.

    ``existing_pin_document_bytes`` is required. Pass ``None`` only when the
    caller declares that no existing pin document needs preservation; otherwise
    pass its complete bytes. Existing pins for other datasets are retained. A
    different pin for ``dataset_id`` can be replaced only by explicitly setting
    ``replace_dataset_id`` to that same dataset identifier.

    The snapshot must round-trip through the canonical codec, carry the exact
    caller-declared reference version, and contain at least one assignment whose
    ``source_id`` matches the caller-declared pin source. This checks internal
    consistency only. The authority, registry, source, rights, history coverage,
    and provenance are not independently verified.

    Args:
        snapshot_bytes: Complete canonical snapshot bytes supplied by the caller.
        existing_pin_document_bytes: Existing runtime pin bytes, or explicit
            ``None`` to declare that no existing pins need preservation.
        dataset_id: Caller-declared dataset key for the new pin.
        source_id: Caller-declared source; every snapshot assignment must match.
        authority_id: Caller-declared authority namespace.
        registry_id: Caller-declared registry namespace.
        reference_version: Caller-declared version, which must match the snapshot.
        replace_dataset_id: Explicit authorization to replace an existing pin for
            this exact dataset. Other dataset pins are always retained.

    Returns:
        Canonical runtime-compatible pin JSON bytes, complete snapshot and pin
        file SHA-256 values, row counts, and an explicit unverified status.

    Raises:
        TemporalIdentityStagingError: If any byte document, identifier,
            relationship, replacement, or fixed size/count limit is invalid.
    """
    registry = _decode_snapshot(snapshot_bytes)
    pin = _make_pin(
        dataset_id=dataset_id,
        source_id=source_id,
        authority_id=authority_id,
        registry_id=registry_id,
        reference_version=reference_version,
        snapshot_sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
    )
    _validate_snapshot_relationships(registry, pin)

    existing_pins = _decode_existing_pins(existing_pin_document_bytes)
    merged = _merge_pin(
        existing_pins,
        pin,
        replace_dataset_id=replace_dataset_id,
    )
    pin_bytes = _encode_runtime_pin_document(merged)
    pin_sha256 = hashlib.sha256(pin_bytes).hexdigest()
    _validate_runtime_pin_document(pin_bytes, pin_sha256)

    return TemporalIdentityStagingResult(
        pin_document_bytes=pin_bytes,
        snapshot_sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
        pin_file_sha256=pin_sha256,
        assignment_count=len(registry.assignments),
        existing_pin_count=len(existing_pins),
        pin_count=len(merged),
        verification_status=_STAGING_VERIFICATION,
        snapshot_bytes=snapshot_bytes,
        candidate_pin=pin,
    )


def _decode_snapshot(raw: bytes) -> TemporalIdentityRegistry:
    """Require bounded codec bytes that re-encode byte-for-byte."""
    if type(raw) is not bytes:
        raise TemporalIdentityStagingError("snapshot_bytes_invalid")
    if not raw or len(raw) > MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES:
        raise TemporalIdentityStagingError("snapshot_size_invalid")
    try:
        registry = decode_temporal_identity_snapshot(raw)
        canonical = encode_temporal_identity_snapshot(registry)
    except TemporalIdentitySnapshotError:
        raise TemporalIdentityStagingError("snapshot_invalid") from None
    if canonical != raw:
        raise TemporalIdentityStagingError("snapshot_round_trip_mismatch")
    return registry


def _make_pin(
    *,
    dataset_id: str,
    source_id: str,
    authority_id: str,
    registry_id: str,
    reference_version: str,
    snapshot_sha256: str,
) -> TemporalIdentityPin:
    """Use the shared strict model as the single pin identifier contract."""
    try:
        return TemporalIdentityPin(
            dataset_id=dataset_id,
            source_id=source_id,
            authority_id=authority_id,
            registry_id=registry_id,
            reference_version=reference_version,
            snapshot_sha256=snapshot_sha256,
        )
    except (ValidationError, TypeError, ValueError):
        raise TemporalIdentityStagingError("pin_metadata_invalid") from None


def _validate_snapshot_relationships(
    registry: TemporalIdentityRegistry,
    pin: TemporalIdentityPin,
) -> None:
    """Check only snapshot facts that can be verified from the supplied bytes."""
    if registry.reference_version != pin.reference_version:
        raise TemporalIdentityStagingError("reference_version_mismatch")
    if not registry.assignments:
        raise TemporalIdentityStagingError("snapshot_source_unverifiable")
    if any(row.source_id != pin.source_id for row in registry.assignments):
        raise TemporalIdentityStagingError("source_id_mismatch")


def _decode_existing_pins(raw: bytes | None) -> tuple[TemporalIdentityPin, ...]:
    """Validate supplied existing pins with the exact runtime decoder contract."""
    if raw is None:
        return ()
    if type(raw) is not bytes:
        raise TemporalIdentityStagingError("existing_pin_bytes_invalid")
    if not raw or len(raw) > MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES:
        raise TemporalIdentityStagingError("existing_pin_size_invalid")
    expected = hashlib.sha256(raw).hexdigest()
    try:
        return decode_temporal_identity_pin_document(raw, expected)
    except TemporalIdentityPinDocumentError as exc:
        raise TemporalIdentityStagingError("existing_pin_document_invalid") from exc


def _merge_pin(
    existing_pins: tuple[TemporalIdentityPin, ...],
    candidate: TemporalIdentityPin,
    *,
    replace_dataset_id: str | None,
) -> tuple[TemporalIdentityPin, ...]:
    """Retain every unrelated dataset and require explicit replacement intent."""
    if replace_dataset_id is not None and replace_dataset_id != candidate.dataset_id:
        raise TemporalIdentityStagingError("replacement_dataset_mismatch")
    existing_by_dataset = {pin.dataset_id: pin for pin in existing_pins}
    previous = existing_by_dataset.get(candidate.dataset_id)
    if replace_dataset_id is not None and previous is None:
        raise TemporalIdentityStagingError("replacement_target_missing")
    if previous is not None and previous != candidate and replace_dataset_id is None:
        raise TemporalIdentityStagingError("replacement_confirmation_required")
    existing_by_dataset[candidate.dataset_id] = candidate
    if len(existing_by_dataset) > MAX_TEMPORAL_IDENTITY_PINS:
        raise TemporalIdentityStagingError("pin_count_limit_exceeded")
    return tuple(existing_by_dataset[key] for key in sorted(existing_by_dataset))


def _encode_runtime_pin_document(pins: tuple[TemporalIdentityPin, ...]) -> bytes:
    """Serialize exactly the v1 runtime shape in deterministic UTF-8 JSON."""
    document = {
        "schema_version": 1,
        "pins": [pin.model_dump(mode="json") for pin in pins],
    }
    try:
        raw = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise TemporalIdentityStagingError("pin_document_encoding_failed") from None
    if not raw or len(raw) > MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES:
        raise TemporalIdentityStagingError("pin_file_size_limit_exceeded")
    return raw


def _validate_runtime_pin_document(raw: bytes, digest: str) -> None:
    """Prove that the shared parser accepts generated runtime bytes and digest."""
    try:
        decode_temporal_identity_pin_registry(raw, digest)
    except TemporalIdentityPinDocumentError:
        raise TemporalIdentityStagingError("pin_document_runtime_incompatible") from None
