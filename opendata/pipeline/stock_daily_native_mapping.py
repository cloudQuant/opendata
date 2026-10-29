"""Prepare an offline stock-daily identity mapping candidate from local evidence.

The orchestrator opens raw bytes only through the content-addressed reader,
parses the fixed source-native response, resolves a caller-supplied identity
runtime's exact dataset pin and snapshot, then delegates mapping to the
source-native mapper. It does not import the API layer, create a runtime,
connect to a provider, write to a database, persist a candidate, or publish it.

The runtime is an explicit dependency-injection boundary. Protocol conformance
and a matching snapshot digest do not authenticate the Python object that
supplied the pin. Production callers must pass the active runtime produced by
``create_temporal_identity_runtime`` in their trusted application lifecycle;
this module cannot prove that origin. Candidate fields therefore retain
unverified source-authenticity and coverage status, and the result labels the
pin origin as caller-supplied.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pydantic import ValidationError

from opendata.data.providers.akshare.stock_daily_mapper import (
    StockDailyNativeMappedBatch,
    map_stock_daily_native_envelope,
)
from opendata.data.providers.akshare.stock_daily_native import (
    ADAPTER_ID,
    OPERATION_ID,
    StockDailyNativeEnvelope,
    StockDailyNativeEvidence,
    StockDailyNativeRequest,
    parse_eastmoney_stock_daily_response,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.data.temporal_instrument_identity import TemporalIdentityRegistry
from opendata.pipeline.raw_evidence import RawResponseManifest, RawResponseSource
from opendata.pipeline.raw_evidence_reader import (
    RawEvidenceReadError,
    RawEvidenceReadResult,
    RawResponseArtifactReader,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$", re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_REQUEST_FIELDS = ("adjust", "end_date", "market", "source", "start_date", "symbol")


class _PinLookup(Protocol):
    """Minimal runtime interface for one operator-declared dataset pin."""

    def get(self, dataset_id: str) -> object | None:
        """Return the exact dataset pin, if one is configured."""


class _SnapshotLookup(Protocol):
    """Read-only runtime interface for one explicitly pinned snapshot."""

    def get(
        self,
        *,
        authority_id: str,
        registry_id: str,
        reference_version: str,
        expected_sha256: str,
    ) -> object:
        """Return a fully decoded snapshot whose complete bytes match the pin."""


class StockDailyIdentityRuntime(Protocol):
    """Injected capability expected from the trusted application lifecycle.

    This protocol avoids a pipeline-to-API import. Implementing it does not
    establish that the object came from the runtime factory; callers remain
    responsible for supplying that trusted capability.
    """

    @property
    def enabled(self) -> bool:
        """Whether the embedding runtime was explicitly enabled."""

    @property
    def pin_lookup(self) -> _PinLookup | None:
        """Return the exact lookup facade supplied by the runtime."""

    @property
    def snapshot_store(self) -> _SnapshotLookup | None:
        """Return the read-only snapshot facade supplied by the runtime."""


class StockDailyNativeMappingError(ValueError):
    """Expose a stable, content-free failure code for candidate preparation."""

    def __init__(self, code: str) -> None:
        """Create an error without retaining provider bytes, paths, or SQL details."""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class StockDailyNativeMappingCandidate:
    """A mapped source-native candidate with conservative provenance labels.

    The local artifact manifest and body passed their reader integrity checks.
    The pin was obtained through the injected lookup and the snapshot was
    obtained through its read interface, but this result does not authenticate
    the injected objects or the upstream source. The nested batch remains a
    candidate and is neither stored nor published.
    """

    batch: StockDailyNativeMappedBatch
    raw_evidence_integrity_status: Literal["local_manifest_and_body_verified"] = (
        "local_manifest_and_body_verified"
    )
    identity_pin_origin_status: Literal["caller_supplied_runtime_unverified"] = (
        "caller_supplied_runtime_unverified"
    )
    source_authenticity_status: Literal["unverified"] = "unverified"
    history_coverage_status: Literal["unknown"] = "unknown"
    persistence_status: Literal["not_persisted"] = "not_persisted"
    publication_status: Literal["not_published"] = "not_published"


@dataclass(frozen=True, slots=True)
class _PinFields:
    """Bounded copy of the six values needed for a pinned store lookup."""

    dataset_id: str
    source_id: str
    authority_id: str
    registry_id: str
    reference_version: str
    snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class _CopiedPinRegistry:
    """Give the mapper only the already copied, validated one-dataset pin."""

    pin: _PinFields

    def get(self, dataset_id: str) -> _PinFields | None:
        """Return the pin only for its exact configured dataset key."""
        return self.pin if dataset_id == self.pin.dataset_id else None


def prepare_stock_daily_native_mapping_candidate(
    *,
    reader: RawResponseArtifactReader,
    identity_runtime: StockDailyIdentityRuntime,
    manifest_sha256: str,
    dataset_id: str,
    request: StockDailyNativeRequest,
    captured_at_not_after: datetime,
) -> StockDailyNativeMappingCandidate:
    """Read, validate, and map one saved Eastmoney-shaped stock-daily capture.

    The function revalidates the explicit stock request, computes its fixed
    allowlisted summary, and asks the exact local reader to verify the manifest
    and original response bytes. It then fetches the pin and snapshot through
    the injected runtime interfaces before invoking the pure source-native
    mapper. No network, environment, filesystem write, database write,
    migration, candidate persistence, or publication occurs here.

    Args:
        reader: Exact ``RawResponseArtifactReader`` bound to a private evidence root.
        identity_runtime: Active, factory-created read-only runtime supplied by
            the trusted application lifecycle. This structural boundary cannot
            detect a fabricated lookalike; the candidate marks its origin unverified.
        manifest_sha256: Exact lowercase digest of the canonical saved manifest.
        dataset_id: Dataset key expected both in the manifest and operator pin.
        request: One explicit bounded SH/SZ request; no broad or inferred universe.
        captured_at_not_after: Caller-trusted UTC upper bound for the manifest time.

    Returns:
        A frozen identity-bound candidate. Source authenticity and history
        coverage remain unverified/unknown, and the candidate is not persisted.

    Raises:
        StockDailyNativeMappingError: Any input, raw-evidence, runtime, pin,
            snapshot, or mapping validation fails. Messages contain only a code.
        TypeError: The reader or request is not the exact supported type.
    """
    checked_request = _validate_request(request)
    if type(reader) is not RawResponseArtifactReader:
        raise TypeError("reader must be the exact RawResponseArtifactReader")
    _validate_top_level_inputs(manifest_sha256, dataset_id, captured_at_not_after)
    pin_lookup, snapshot_lookup = _validate_runtime_boundary(identity_runtime)
    expected_source = _make_expected_source(dataset_id)
    expected_request_hash = _request_digest(checked_request)

    try:
        evidence = reader.read(
            manifest_sha256,
            expected_source=expected_source,
            expected_request_summary_sha256=expected_request_hash,
            captured_at_not_after=captured_at_not_after,
            expected_http_status=200,
        )
    except RawEvidenceReadError:
        raise StockDailyNativeMappingError("raw_evidence_rejected") from None
    except Exception:
        raise StockDailyNativeMappingError("raw_evidence_reader_failed") from None

    manifest = _validate_reader_result(
        evidence,
        manifest_sha256=manifest_sha256,
        expected_source=expected_source,
        expected_request_hash=expected_request_hash,
        captured_at_not_after=captured_at_not_after,
    )
    envelope = _parse_verified_envelope(evidence, manifest, checked_request)
    pin = _read_pin(pin_lookup, dataset_id)
    if pin.source_id != ADAPTER_ID:
        raise StockDailyNativeMappingError("identity_pin_mismatch")
    identity_registry = _read_snapshot(snapshot_lookup, pin)
    try:
        mapped = map_stock_daily_native_envelope(
            envelope,
            operator_pin_registry=_CopiedPinRegistry(pin),
            identity_registry=identity_registry,
        )
    except (TypeError, ValueError):
        raise StockDailyNativeMappingError("identity_mapping_rejected") from None
    except Exception:
        raise StockDailyNativeMappingError("identity_mapping_failed") from None
    if type(mapped) is not StockDailyNativeMappedBatch:
        raise StockDailyNativeMappingError("identity_mapping_invalid_result")
    return StockDailyNativeMappingCandidate(batch=mapped)


def _validate_request(request: StockDailyNativeRequest) -> StockDailyNativeRequest:
    """Require the exact immutable request model and re-run its validators."""
    if type(request) is not StockDailyNativeRequest:
        raise TypeError("request must be the exact StockDailyNativeRequest")
    try:
        values = request.model_dump(mode="python")
        checked = StockDailyNativeRequest.model_validate(values, strict=True)
    except (AttributeError, TypeError, ValidationError):
        raise StockDailyNativeMappingError("request_invalid") from None
    if checked.request_source != ADAPTER_ID:
        raise StockDailyNativeMappingError("request_source_unsupported")
    return checked


def _validate_top_level_inputs(
    manifest_sha256: str,
    dataset_id: str,
    captured_at_not_after: datetime,
) -> None:
    """Reject malformed evidence selectors and non-UTC capture bounds."""
    if type(manifest_sha256) is not str or _SHA256.fullmatch(manifest_sha256) is None:
        raise StockDailyNativeMappingError("manifest_digest_invalid")
    if type(dataset_id) is not str or _IDENTIFIER.fullmatch(dataset_id) is None:
        raise StockDailyNativeMappingError("dataset_id_invalid")
    if type(captured_at_not_after) is not datetime:
        raise StockDailyNativeMappingError("capture_bound_invalid")
    try:
        offset = captured_at_not_after.utcoffset()
    except (OverflowError, ValueError):
        raise StockDailyNativeMappingError("capture_bound_invalid") from None
    if captured_at_not_after.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise StockDailyNativeMappingError("capture_bound_invalid")


def _make_expected_source(dataset_id: str) -> RawResponseSource:
    """Build only the fixed AkShare stock-daily source identity."""
    return RawResponseSource(
        provider_id=ADAPTER_ID,
        dataset_id=dataset_id,
        operation_id=OPERATION_ID,
    )


def _request_digest(request: StockDailyNativeRequest) -> str:
    """Recompute the provider job's fixed allowlisted request summary."""
    return request_summary_sha256(
        {
            "adjust": "",
            "end_date": request.end_date,
            "market": "cn",
            "source": request.request_source,
            "start_date": request.start_date,
            "symbol": request.symbol,
        },
        allowed_fields=_REQUEST_FIELDS,
    )


def _validate_reader_result(
    result: RawEvidenceReadResult,
    *,
    manifest_sha256: str,
    expected_source: RawResponseSource,
    expected_request_hash: str,
    captured_at_not_after: datetime,
) -> RawResponseManifest:
    """Recheck the reader DTO's exact binding before parsing any response bytes."""
    if type(result) is not RawEvidenceReadResult:
        raise StockDailyNativeMappingError("raw_evidence_result_invalid")
    if type(result.manifest) is not RawResponseManifest or type(result.body) is not bytes:
        raise StockDailyNativeMappingError("raw_evidence_result_invalid")
    try:
        manifest = RawResponseManifest.model_validate(
            result.manifest.model_dump(mode="python"), strict=True
        )
    except (AttributeError, TypeError, ValidationError):
        raise StockDailyNativeMappingError("raw_evidence_result_invalid") from None
    manifest_bytes = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if (
        type(result.manifest_sha256) is not str
        or result.manifest_sha256 != manifest_sha256
        or hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256
        or manifest.source != expected_source
        or manifest.request_summary_sha256 != expected_request_hash
        or manifest.http_status != 200
        or manifest.captured_at_utc > captured_at_not_after
        or len(result.body) != manifest.response_size_bytes
        or hashlib.sha256(result.body).hexdigest() != manifest.response_sha256
    ):
        raise StockDailyNativeMappingError("raw_evidence_binding_mismatch")
    return manifest


def _parse_verified_envelope(
    result: RawEvidenceReadResult,
    manifest: RawResponseManifest,
    request: StockDailyNativeRequest,
) -> StockDailyNativeEnvelope:
    """Parse the verified raw bytes and attach only their checked local manifest."""
    try:
        parsed = parse_eastmoney_stock_daily_response(result.body, request=request)
        evidence = StockDailyNativeEvidence(
            capture_id=manifest.capture_id,
            provider_id=manifest.source.provider_id,
            dataset_id=manifest.source.dataset_id,
            operation_id=manifest.source.operation_id,
            request_summary_sha256=manifest.request_summary_sha256,
            http_status=manifest.http_status,
            response_sha256=manifest.response_sha256,
            response_size_bytes=manifest.response_size_bytes,
            manifest_sha256=result.manifest_sha256,
            captured_at_utc=manifest.captured_at_utc,
            retention_class=manifest.retention_class,
            source_history_limit=manifest.source_history_limit,
        )
        return StockDailyNativeEnvelope.model_validate(
            {**parsed.model_dump(mode="python"), "evidence": evidence}, strict=True
        )
    except (TypeError, ValueError, ValidationError):
        raise StockDailyNativeMappingError("native_payload_invalid") from None


def _validate_runtime_boundary(
    runtime: StockDailyIdentityRuntime,
) -> tuple[_PinLookup, _SnapshotLookup]:
    """Require an explicitly enabled, callable read-only injection boundary.

    This is structural validation only. It intentionally does not mark a
    runtime implementation as authenticated; the returned candidate carries
    ``caller_supplied_runtime_unverified`` for that reason.
    """
    try:
        enabled = runtime.enabled
        pins = runtime.pin_lookup
        snapshots = runtime.snapshot_store
        pin_get = getattr(pins, "get", None)
        snapshot_get = getattr(snapshots, "get", None)
    except Exception:
        raise StockDailyNativeMappingError("identity_runtime_unavailable") from None
    if (
        enabled is not True
        or pins is None
        or snapshots is None
        or not callable(pin_get)
        or not callable(snapshot_get)
    ):
        raise StockDailyNativeMappingError("identity_runtime_unavailable")
    return pins, snapshots


def _read_pin(pin_lookup: _PinLookup, dataset_id: str) -> _PinFields:
    """Read and strictly copy the one dataset pin without trusting its issuer."""
    try:
        raw_pin = pin_lookup.get(dataset_id)
    except Exception:
        raise StockDailyNativeMappingError("identity_pin_unavailable") from None
    if raw_pin is None:
        raise StockDailyNativeMappingError("identity_pin_unavailable")
    names = (
        "dataset_id",
        "source_id",
        "authority_id",
        "registry_id",
        "reference_version",
        "snapshot_sha256",
    )
    try:
        values = tuple(getattr(raw_pin, name) for name in names)
    except Exception:
        raise StockDailyNativeMappingError("identity_pin_invalid") from None
    if any(type(value) is not str for value in values):
        raise StockDailyNativeMappingError("identity_pin_invalid")
    if any(_IDENTIFIER.fullmatch(value) is None for value in values[:-1]):
        raise StockDailyNativeMappingError("identity_pin_invalid")
    if _SHA256.fullmatch(values[-1]) is None:
        raise StockDailyNativeMappingError("identity_pin_invalid")
    pin = _PinFields(*values)
    if pin.dataset_id != dataset_id:
        raise StockDailyNativeMappingError("identity_pin_mismatch")
    return pin


def _read_snapshot(
    snapshot_lookup: _SnapshotLookup,
    pin: _PinFields,
) -> TemporalIdentityRegistry:
    """Read only the exact version and full-byte digest named by the copied pin."""
    try:
        registry = snapshot_lookup.get(
            authority_id=pin.authority_id,
            registry_id=pin.registry_id,
            reference_version=pin.reference_version,
            expected_sha256=pin.snapshot_sha256,
        )
    except Exception:
        raise StockDailyNativeMappingError("identity_snapshot_unavailable") from None
    if type(registry) is not TemporalIdentityRegistry:
        raise StockDailyNativeMappingError("identity_snapshot_invalid")
    return registry


__all__ = [
    "StockDailyIdentityRuntime",
    "StockDailyNativeMappingCandidate",
    "StockDailyNativeMappingError",
    "prepare_stock_daily_native_mapping_candidate",
]
