"""Prepare caller-fetched Fuyao observations for the immutable archive.

This bridge performs no network access and creates no database engine or
transaction. It archives the HTTPX entity bytes first, verifies that artifact
through the existing reader, and returns a frozen store DTO for a caller-owned
transaction. Filesystem publication and SQL commit are separate operations and
cannot be made atomic by this module.

Request digests, raw-source fields, unit profiles, and source-attempt values are
caller declarations. A nonempty fetch trace can be compared with the declared
attempt and successful response fields, but both Python DTOs remain caller
supplied and the comparison is not proof that a particular transport sent
these bytes. Retention labels do not establish data rights. Captured artifacts
do not reconstruct historical point-in-time values, quarterly cumulative
semantics, or coverage.

This bridge constrains raw-source identity to ``ths / ths_financial_statement /
{statement_type}_{periodicity}``. That triple is a new bridge-local convention,
not an established upstream or historical producer contract. The existing store
test fixture uses the same dataset label, but it does not establish a production
contract. There is no registry that establishes this triple as canonical.
Consumers querying manifests by source must use this bridge's identity; changing
it later may require a consumer migration or an explicit archive migration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from opendata.data.providers.ths.models.financial_observation_fetcher import (
    FinancialObservationFetchResult,
    ThsFinancialNativeUnitProfile,
    canonical_native_unit_mapping_sha256,
)
from opendata.data.providers.ths.models.financial_observation_raw import (
    FinancialObservationDecodeError,
    FinancialObservationDecodeResult,
    FinancialObservationSourceReference,
    QuarantinedFinancialObservationRow,
    decode_financial_observation_body,
)
from opendata.data.providers.ths.transport.credentials import DEFAULT_FUYAO_API_BASE_URL
from opendata.data.providers.ths.transport.http_client import (
    FuyaoTransportAttemptTrace,  # noqa: TC001 -- public DTO annotations resolve at runtime
)
from opendata.data.source_attempt_gate import (
    AttemptReservation,
    ReservationStatus,
    TransportAttempt,
)
from opendata.pipeline.financial_observation_store import (
    FinancialObservationCaptureContext,
    PreparedCapture,
    prepare_financial_observation_capture,
)
from opendata.pipeline.raw_evidence import (
    HistoryLimit,
    RawResponseArtifactWriter,
    RawResponseSource,
    RetentionClass,
)
from opendata.pipeline.raw_evidence_reader import RawResponseArtifactReader

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_INSTRUMENT_PATTERN = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_SOURCE_ID = "ths_fuyao"
_SOURCE = "ths"
_DATASET_ID = "ths_financial_statement"
_MODEL = "financial_statement"
_ATTEMPT_OPERATION = "query"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_TIMESTAMP_US = 253_402_300_799_999_999


class FuyaoObservationCaptureBridgeError(ValueError):
    """Raised when the supplied fetch result and declarations cannot be bound."""


@dataclass(frozen=True, slots=True)
class FuyaoCaptureProvenance:
    """Operator declarations passed to the raw-response manifest."""

    raw_source: RawResponseSource
    retention_class: RetentionClass
    source_history_limit: HistoryLimit


@dataclass(frozen=True, slots=True)
class FuyaoSourceAttemptDeclaration:
    """Caller-supplied attempt and reservation values without gate proof.

    When a fetch result includes a local trace, the bridge compares its final
    request/attempt/reservation projection with these values. Both Python DTOs
    remain caller-supplied, so this equality check cannot authenticate that the
    gate actually produced the declaration or that Fuyao processed the response.
    """

    attempt: TransportAttempt = field(repr=False)
    reservation: AttemptReservation = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedFuyaoObservationCapture:
    """Prepared SQL values plus addresses and process-local attempt metadata.

    A nonempty trace is matched only against caller-supplied declaration fields
    and the fetch result's success metadata. Both DTOs can be constructed or
    altered by callers; this is not an authenticated gate record or upstream
    receipt. Trace values are deliberately omitted from ``repr``.
    """

    prepared: PreparedCapture = field(repr=False)
    manifest_sha256: str
    source_attempt_declaration: FuyaoSourceAttemptDeclaration = field(repr=False)
    attempt_trace: tuple[FuyaoTransportAttemptTrace, ...] = field(
        default=(), repr=False, compare=False
    )
    attempt_association_status: Literal[
        "caller_declared_unverified", "local_trace_matched_unverified"
    ] = "caller_declared_unverified"
    source_request_id_match_status: Literal[
        "not_checked_no_trace", "matched_unverified", "unavailable_unverified"
    ] = "not_checked_no_trace"


def prepare_fuyao_observation_capture(
    fetch_result: FinancialObservationFetchResult,
    *,
    unit_profile: ThsFinancialNativeUnitProfile,
    provenance: FuyaoCaptureProvenance,
    source_attempt_declaration: FuyaoSourceAttemptDeclaration,
    writer: RawResponseArtifactWriter,
) -> PreparedFuyaoObservationCapture:
    """Archive and prepare one already-fetched Fuyao response.

    All input declarations and the response body are revalidated before the
    first filesystem write. The request-summary digest is reused verbatim from
    ``fetch_result`` and a missing digest fails closed. The writer stores the
    exact HTTPX entity bytes, then the existing reader and archive preparation
    API verify the newly written manifest and decode references.

    An empty ``attempt_trace`` preserves the legacy caller-declared behavior.
    When a trace is present, its final local attempt and successful response
    fields are compared with the supplied declaration and fetch result before
    any artifact is written. A match only establishes equality among these
    caller-supplied in-memory values. It does not authenticate either DTO,
    independently verify the gate, or prove that Fuyao received or processed
    the request. A missing trace source request ID is reported explicitly as
    unavailable and is not inferred from the fetch result.

    The returned ``prepared`` value is suitable for
    :func:`persist_prepared_financial_observation_capture` inside a transaction
    opened and committed by the caller. The wrapper preserves the caller's
    source-attempt declaration and any matched local trace as explicitly
    unverified in-memory metadata; the current SQL archive schema does not
    persist either value.

    Args:
        fetch_result: Exact response DTO returned by the Fuyao fetcher.
        unit_profile: Exact operator-declared unit profile used for decoding.
        provenance: Caller-declared raw source, retention, and history labels.
            For this bridge the raw-source triple is fixed to
            ``ths / ths_financial_statement / {statement_type}_{periodicity}``;
            this is a bridge-local convention, even though the dataset label
            matches a test fixture, and is not an established historical
            producer contract.
        source_attempt_declaration: Caller-declared gate attempt and reservation.
        writer: Exact writer bound to an explicit private artifact root.

    Returns:
        A prepared archive DTO and the SHA-256 address of its raw manifest.

    Raises:
        FuyaoObservationCaptureBridgeError: Invalid or inconsistent input is
            rejected before raw bytes are written.
        FinancialObservationDecodeError: The body no longer decodes under the
            declared statement context and operator unit profile.
        ValueError: The existing raw-evidence or store preparation boundary
            rejected the persisted artifact.

    Notes:
        Object and manifest writes precede caller SQL commit. A later failure
        can leave an orphan artifact, and this bridge does not clean orphan files.
        Every fresh call asks the writer to create a manifest, usually with a
        different identity for the same body. Identity is derived from manifest
        contents, and capture timestamps can coincide, so distinct identities
        are not guaranteed. SQL replay idempotency applies only when the caller
        reuses this same prepared DTO and capture ID. Keep and retry it after a
        SQL failure; after a process restart, the artifact alone lacks enough
        context to rebuild a prepared capture safely. The wrapper does not
        persist the attempt declaration or unit-profile evidence reference;
        the archive retains only the declared
        unit version/hash. An outer runner can still refetch after a restart;
        preventing that requires durable runner state. This function itself
        never retries or reissues network I/O.
    """
    checked_result = _validate_fetch_result(fetch_result)
    checked_profile = _validate_unit_profile(unit_profile, checked_result)
    checked_provenance = _validate_provenance(provenance, checked_result)
    checked_attempt = _validate_source_attempt(
        source_attempt_declaration,
        observed_at_utc=checked_result.observed_at,
    )
    attempt_association_status, source_id_match_status = _match_attempt_trace(
        checked_result,
        checked_attempt,
    )
    if type(writer) is not RawResponseArtifactWriter:
        raise FuyaoObservationCaptureBridgeError("writer is not the reviewed exact type")
    request_digest = checked_result.request_summary_sha256
    if type(request_digest) is not str or _SHA256_PATTERN.fullmatch(request_digest) is None:
        raise FuyaoObservationCaptureBridgeError(
            "fetch result request digest is missing or invalid"
        )

    # The decoder is pure and bounded. Run it before writing so a mutated or
    # mismatched result cannot leave avoidable evidence artifacts behind.
    decoded = _decode_and_match(checked_result, checked_profile)
    if checked_result.observed_at > datetime.now(timezone.utc):
        raise FuyaoObservationCaptureBridgeError(
            "response observation time is later than the local capture clock"
        )

    captured = writer.capture(
        (checked_result.raw_body,),
        source=checked_provenance.raw_source,
        request_summary_sha256=request_digest,
        http_status=checked_result.status_code,
        retention_class=checked_provenance.retention_class,
        source_history_limit=checked_provenance.source_history_limit,
    )
    if (
        captured.manifest.response_sha256 != checked_result.body_sha256
        or captured.manifest.response_size_bytes != len(checked_result.raw_body)
    ):
        raise FuyaoObservationCaptureBridgeError(
            "persisted entity bytes differ from the fetched response"
        )

    reader = RawResponseArtifactReader(
        writer.artifact_root,
        max_response_bytes=_MAX_RESPONSE_BYTES,
    )
    context = FinancialObservationCaptureContext(
        source_id=_SOURCE_ID,
        instrument_id=checked_result.instrument_id,
        statement_type=checked_result.statement_type,
        periodicity=checked_result.periodicity,
        observed_at_utc=checked_result.observed_at,
        raw_source=checked_provenance.raw_source,
        request_summary_sha256=request_digest,
    )
    prepared = prepare_financial_observation_capture(
        context=context,
        reader=reader,
        manifest_sha256=captured.manifest_path.stem,
        expected_http_status=checked_result.status_code,
        result=decoded,
    )
    return PreparedFuyaoObservationCapture(
        prepared=prepared,
        manifest_sha256=captured.manifest_path.stem,
        source_attempt_declaration=checked_attempt,
        attempt_trace=checked_result.attempt_trace,
        attempt_association_status=attempt_association_status,
        source_request_id_match_status=source_id_match_status,
    )


def _validate_fetch_result(
    result: FinancialObservationFetchResult,
) -> FinancialObservationFetchResult:
    """Reconstruct the fetch DTO to rerun its boundary validation."""
    if type(result) is not FinancialObservationFetchResult:
        raise FuyaoObservationCaptureBridgeError("fetch result is not the reviewed exact type")
    try:
        checked = replace(result)
    except (TypeError, ValueError, OverflowError):
        raise FuyaoObservationCaptureBridgeError(
            "fetch result failed boundary validation"
        ) from None
    if (
        type(checked.instrument_id) is not str
        or _INSTRUMENT_PATTERN.fullmatch(checked.instrument_id) is None
        or not 1 <= len(checked.raw_body) <= _MAX_RESPONSE_BYTES
    ):
        raise FuyaoObservationCaptureBridgeError("fetch result failed capture boundary checks")
    return checked


def _validate_unit_profile(
    profile: ThsFinancialNativeUnitProfile,
    result: FinancialObservationFetchResult,
) -> ThsFinancialNativeUnitProfile:
    """Deep-copy the unit profile and bind it to the fetched declaration."""
    if type(profile) is not ThsFinancialNativeUnitProfile:
        raise FuyaoObservationCaptureBridgeError("unit profile is not the reviewed exact type")
    try:
        checked = ThsFinancialNativeUnitProfile(
            statement_type=profile.statement_type,
            version=profile.version,
            native_units=dict(profile.native_units.items()),
            mapping_sha256_declared=profile.mapping_sha256_declared,
            evidence_ref=profile.evidence_ref,
        )
        recomputed = canonical_native_unit_mapping_sha256(
            statement_type=checked.statement_type,
            version=checked.version,
            native_units=checked.native_units,
        )
    except (AttributeError, TypeError, ValueError):
        raise FuyaoObservationCaptureBridgeError(
            "unit profile failed boundary validation"
        ) from None
    if (
        recomputed != checked.mapping_sha256_declared
        or checked.statement_type != result.statement_type
        or checked.version != result.unit_mapping_version_declared
        or checked.mapping_sha256_declared != result.unit_mapping_sha256_declared
        or result.unit_mapping_evidence_kind != "operator_declared"
    ):
        raise FuyaoObservationCaptureBridgeError(
            "unit profile differs from the fetched operator declaration"
        )
    return checked


def _validate_provenance(
    provenance: FuyaoCaptureProvenance,
    result: FinancialObservationFetchResult,
) -> FuyaoCaptureProvenance:
    """Revalidate raw source and ensure labels use the writer's finite vocabulary."""
    if type(provenance) is not FuyaoCaptureProvenance:
        raise FuyaoObservationCaptureBridgeError("provenance is not the reviewed exact type")
    if type(provenance.raw_source) is not RawResponseSource:
        raise FuyaoObservationCaptureBridgeError("raw source is not the reviewed exact type")
    try:
        source = RawResponseSource.model_validate(
            provenance.raw_source.model_dump(mode="python"), strict=True
        )
    except (AttributeError, TypeError, ValueError):
        raise FuyaoObservationCaptureBridgeError("raw source failed boundary validation") from None
    expected_operation_id = f"{result.statement_type}_{result.periodicity}"
    if (
        source.provider_id != _SOURCE
        or source.dataset_id != _DATASET_ID
        or source.operation_id != expected_operation_id
    ):
        raise FuyaoObservationCaptureBridgeError(
            "raw source identity differs from the fetched statement scope"
        )
    if provenance.retention_class not in (
        "unknown",
        "ephemeral",
        "restricted",
        "durable_candidate",
    ):
        raise FuyaoObservationCaptureBridgeError("retention declaration is invalid")
    if provenance.source_history_limit not in (
        "unknown",
        "provider_retention_window",
        "snapshot_only",
        "partial_history",
        "none_documented",
    ):
        raise FuyaoObservationCaptureBridgeError("source history declaration is invalid")
    return FuyaoCaptureProvenance(
        raw_source=source,
        retention_class=provenance.retention_class,
        source_history_limit=provenance.source_history_limit,
    )


def _validate_source_attempt(
    declaration: FuyaoSourceAttemptDeclaration,
    *,
    observed_at_utc: datetime,
) -> FuyaoSourceAttemptDeclaration:
    """Validate only caller-declared attempt shape, never authenticate linkage."""
    if type(declaration) is not FuyaoSourceAttemptDeclaration:
        raise FuyaoObservationCaptureBridgeError(
            "source attempt declaration is not the reviewed exact type"
        )
    if type(declaration.attempt) is not TransportAttempt:
        raise FuyaoObservationCaptureBridgeError("transport attempt declaration is invalid")
    attempt = declaration.attempt
    try:
        checked_attempt = TransportAttempt(
            attempt_id=attempt.attempt_id,
            request_id=attempt.request_id,
            attempt_no=attempt.attempt_no,
            grant_id=attempt.grant_id,
            expected_revision=attempt.expected_revision,
            source=attempt.source,
            canonical_model=attempt.canonical_model,
            operation=attempt.operation,
            principal_scope=attempt.principal_scope,
            host=attempt.host,
            run_key=attempt.run_key,
            expected_rights_evidence_reference=attempt.expected_rights_evidence_reference,
        )
    except (AttributeError, TypeError, ValueError):
        raise FuyaoObservationCaptureBridgeError(
            "transport attempt declaration failed boundary validation"
        ) from None
    expected_host = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
    if (
        checked_attempt.source != _SOURCE
        or checked_attempt.canonical_model != _MODEL
        or checked_attempt.operation != _ATTEMPT_OPERATION
        or checked_attempt.host != expected_host
    ):
        raise FuyaoObservationCaptureBridgeError(
            "transport attempt declaration has an unexpected Fuyao scope"
        )
    if type(declaration.reservation) is not AttemptReservation:
        raise FuyaoObservationCaptureBridgeError("attempt reservation declaration is invalid")
    reservation = declaration.reservation
    if (
        reservation.status is not ReservationStatus.RESERVED_NEW
        or reservation.denial_code is not None
        or reservation.retry_after_us is not None
        or type(reservation.reserved_at_us) is not int
        or not 0 <= reservation.reserved_at_us <= _MAX_TIMESTAMP_US
        or reservation.reserved_at_us > _epoch_microseconds(observed_at_utc)
    ):
        raise FuyaoObservationCaptureBridgeError(
            "attempt reservation declaration is not a new successful reservation"
        )
    return FuyaoSourceAttemptDeclaration(
        attempt=checked_attempt,
        reservation=AttemptReservation(
            status=reservation.status,
            reserved_at_us=reservation.reserved_at_us,
        ),
    )


def _match_attempt_trace(
    result: FinancialObservationFetchResult,
    declaration: FuyaoSourceAttemptDeclaration,
) -> tuple[
    Literal["caller_declared_unverified", "local_trace_matched_unverified"],
    Literal["not_checked_no_trace", "matched_unverified", "unavailable_unverified"],
]:
    """Match the successful local trace projection to supplied caller values.

    The fetch-result boundary has already checked that a nonempty trace is a
    structurally valid immutable tuple with a successful final response. This
    function adds equality against the separately supplied declaration before
    the raw evidence writer runs. It provides no authenticity guarantee.
    """
    trace = result.attempt_trace
    if not trace:
        return "caller_declared_unverified", "not_checked_no_trace"

    final_attempt = trace[-1]
    attempt = declaration.attempt
    reservation = declaration.reservation
    if (
        final_attempt.transport_request_id != attempt.request_id
        or final_attempt.attempt_id != attempt.attempt_id
        or final_attempt.attempt_no != attempt.attempt_no
        or final_attempt.gate_status != reservation.status.value
        or final_attempt.request_budget_reserved is not True
        or final_attempt.reserved_at_us != reservation.reserved_at_us
    ):
        raise FuyaoObservationCaptureBridgeError(
            "final local attempt trace differs from the caller declaration"
        )
    if (
        final_attempt.http_status != result.status_code
        or final_attempt.outcome != "response_observed"
        or final_attempt.failure_category is not None
    ):
        raise FuyaoObservationCaptureBridgeError(
            "final local attempt trace differs from the successful fetch result"
        )
    if final_attempt.source_request_id is None:
        source_id_status: Literal["matched_unverified", "unavailable_unverified"] = (
            "unavailable_unverified"
        )
    else:
        if final_attempt.source_request_id != result.source_request_id:
            raise FuyaoObservationCaptureBridgeError(
                "final local attempt trace source request ID differs from the fetch result"
            )
        source_id_status = "matched_unverified"
    return "local_trace_matched_unverified", source_id_status


def _epoch_microseconds(value: datetime) -> int:
    """Convert an aware UTC timestamp to integer microseconds without floats."""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = value - epoch
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _decode_and_match(
    result: FinancialObservationFetchResult,
    profile: ThsFinancialNativeUnitProfile,
) -> FinancialObservationDecodeResult:
    """Re-decode bytes and require exact agreement with fetched row references."""
    try:
        decoded = decode_financial_observation_body(
            result.raw_body,
            expected_instrument_id=result.instrument_id,
            statement_type=result.statement_type,
            periodicity=result.periodicity,
            observed_at=result.observed_at,
            native_units=profile.native_units,
            native_unit_mapping_version_declared=profile.version,
            native_unit_mapping_sha256_declared=profile.mapping_sha256_declared,
        )
    except FinancialObservationDecodeError:
        raise
    except (TypeError, ValueError, OverflowError):
        raise FuyaoObservationCaptureBridgeError(
            "fetch result body could not be revalidated"
        ) from None
    if (
        decoded.body_sha256 != result.body_sha256
        or decoded.source_request_id != result.source_request_id
        or decoded.observations != result.observations
        or decoded.quarantined_rows != result.quarantined_rows
        or decoded.native_unit_mapping_version_declared != result.unit_mapping_version_declared
        or decoded.native_unit_mapping_sha256_declared != result.unit_mapping_sha256_declared
        or _usability_status(decoded.observations, decoded.quarantined_rows)
        != result.usability_status
    ):
        raise FuyaoObservationCaptureBridgeError(
            "fetch result metadata differs from its exact entity bytes"
        )
    return decoded


def _usability_status(
    observations: tuple[FinancialObservationSourceReference, ...],
    quarantined_rows: tuple[QuarantinedFinancialObservationRow, ...],
) -> str:
    """Derive the response-local usability label without implying coverage."""
    if not observations:
        return "NO_USABLE_OBSERVATIONS"
    if quarantined_rows:
        return "PARTIAL"
    return "USABLE_OBSERVATIONS_PRESENT"


__all__ = [
    "FuyaoCaptureProvenance",
    "FuyaoObservationCaptureBridgeError",
    "FuyaoSourceAttemptDeclaration",
    "PreparedFuyaoObservationCapture",
    "prepare_fuyao_observation_capture",
]
