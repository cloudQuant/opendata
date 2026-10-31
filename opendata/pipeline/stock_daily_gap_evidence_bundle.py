"""Load bounded, hash-pinned offline evidence for stock-daily gap review.

The loader accepts strict JSON evidence documents and the existing canonical
instrument-reference snapshot format. It validates bytes, identity bindings,
and cross-document contracts before constructing the typed reconciliation
inputs. Hashes establish byte integrity only; they do not authenticate a
source or establish legal authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final, TypedDict

from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.instrument_reference_snapshot import (
    MAX_INSTRUMENT_REFERENCE_SNAPSHOT_BYTES,
    decode_instrument_reference_snapshot,
)
from opendata.pipeline.history_coverage import HistoryCoverageRequest
from opendata.pipeline.stock_daily_reconcile import (
    DailyPublicationSnapshot,
    EndBoundaryEvidence,
    EndBoundaryKind,
    InstrumentExpectedObservationEvidence,
    StockDailyGapReconciliation,
    StockDailySeriesEvidence,
    reconcile_stock_daily_gaps,
)

SCHEMA_VERSION: Final[int] = 1
MAX_MANIFEST_BYTES: Final[int] = 64 * 1024 * 1024
MAX_ARTIFACT_BYTES: Final[int] = 8 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES: Final[int] = 192 * 1024 * 1024
MAX_ARTIFACTS: Final[int] = 50_000
MAX_SERIES: Final[int] = 10_000
MAX_TOTAL_DATES: Final[int] = 5_000_000
MAX_DATE_ENTRIES_PER_SERIES: Final[int] = 100_000
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "instrument_registry",
        "instrument_status",
        "instrument_start",
        "instrument_delisting",
        "instrument_alias",
        "business_calendar",
        "instrument_expectation",
        "daily_publication",
        "daily_publication_content",
        "source_retention",
        "delisted_end_boundary",
    }
)


class StockDailyGapEvidenceError(ValueError):
    """Raised when offline evidence is malformed, unbounded, or cross-bound."""


class _BusinessCalendar(TypedDict):
    """Validated fields from one business-calendar artifact."""

    calendar_id: str
    calendar_version: str
    covered_from: date
    covered_through: date
    session_dates: tuple[date, ...]


class _InstrumentExpectation(TypedDict):
    """Validated expected-observation dates for one instrument."""

    calendar_ref: str
    calendar_id: str
    calendar_version: str
    covered_from: date
    covered_through: date
    expected_business_dates: tuple[date, ...]
    unresolved_observation_dates: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class VerifiedEvidenceArtifact:
    """Metadata for one artifact whose bytes and declared identity were checked."""

    evidence_ref: str
    kind: str
    sha256: str
    byte_count: int
    dataset_id: str | None
    source_id: str | None
    instrument_id: str | None
    publication_id: str | None

    def to_dict(self) -> dict[str, object]:
        """Serialize the verified pin without embedding artifact contents."""
        return {
            "evidence_ref": self.evidence_ref,
            "kind": self.kind,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "dataset_id": self.dataset_id,
            "source_id": self.source_id,
            "instrument_id": self.instrument_id,
            "publication_id": self.publication_id,
            "hash_semantics": (
                "exact_input_publication_content_artifact_bytes"
                if self.kind == "daily_publication_content"
                else "exact_input_evidence_artifact_bytes"
            ),
            "identity_binding": (
                "caller_asserted_by_publication_envelope_content_ref"
                if self.kind == "daily_publication_content"
                else "checked_by_artifact_codec_or_envelope"
            ),
            "authority_authenticated": False,
        }


@dataclass(frozen=True, slots=True)
class StockDailyGapEvidenceBundle:
    """Validated typed inputs plus their deterministic offline reconciliation."""

    manifest_sha256: str
    series_evidence: tuple[StockDailySeriesEvidence, ...]
    artifacts: tuple[VerifiedEvidenceArtifact, ...]
    reconciliation: StockDailyGapReconciliation

    def to_dict(self) -> dict[str, object]:
        """Return the canonical review bundle with explicit scope limitations."""
        body: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "manifest_sha256": self.manifest_sha256,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "reconciliation": self.reconciliation.to_dict(),
            "scope": {
                "all_requested_series_complete": (
                    self.reconciliation.all_requested_series_complete
                ),
                "all_a_universe_complete": False,
                "universe_census_attested": False,
                "single_security_is_not_an_all_a_census": True,
                "source_authenticity": "not_authenticated_by_hashes",
                "publication_hash_semantics": "exact_input_publication_content_artifact_bytes",
                "publication_content_identity_binding": ("caller_asserted_by_envelope_content_ref"),
                "publication_source_authenticity": "not_authenticated_by_hashes",
                "calendar_market_binding": "caller_evidence_only",
                "calendar_market_relationship_independently_verified": False,
                "dispatch_authorized": False,
                "bars_created": False,
            },
        }
        body["bundle_sha256"] = hashlib.sha256(_canonical_json(body)).hexdigest()
        return body

    def to_json(self) -> str:
        """Serialize the bundle as stable compact UTF-8 JSON."""
        return _canonical_json(self.to_dict()).decode("utf-8")


def load_stock_daily_gap_evidence_bundle(
    manifest: bytes,
    *,
    artifacts: Mapping[str, bytes],
) -> StockDailyGapEvidenceBundle:
    """Validate offline evidence, build typed series inputs, and reconcile gaps.

    The manifest schema is deliberately narrow. It contains ``schema_version``,
    an ``artifacts`` array of ``{evidence_ref, kind, sha256}`` pins, one
    ``registry_ref``, and a ``series`` array. Each series contains dataset,
    source and caller identifier, an optional alias-resolution date, request
    bounds, and refs for publication, calendar, per-instrument expectations,
    lifecycle, optional source-retention and optional delisted-end evidence.
    The registry ref points to the existing canonical instrument-reference
    snapshot bytes. Metadata artifacts are strict JSON objects whose embedded
    identities and values are cross-checked against that decoded registry and
    one another before reconciliation. A ``daily_publication`` envelope binds
    a separate ``daily_publication_content`` artifact through ``content_ref``.
    The content artifact is exact UTF-8 JSON bytes in the strict
    ``{"rows": [{"trade_date": "YYYY-MM-DD", ...}]}`` format. Its byte
    digest, rather than the metadata-envelope digest, is passed to the
    reconciler. This verifies the supplied content bytes only; it does not
    authenticate a provider or establish source authority. The association
    between content rows and dataset/source/instrument/publication identity is
    caller-asserted by the metadata envelope and its ``content_ref``.

    Missing start or retention boundaries remain unknown. The loader never
    substitutes the first or last observed date for independent evidence.
    It accepts no backfill plan and never dispatches work.

    Args:
        manifest: Raw UTF-8 manifest bytes containing the SHA-256 pins.
        artifacts: Exact ref-to-bytes map from the manifest. The registry entry
            is decoded with the existing instrument-reference snapshot codec.

    Returns:
        A bundle containing the typed inputs and an offline gap report.

    Raises:
        StockDailyGapEvidenceError: If any resource bound, hash, schema,
            evidence reference, identity, date, or cross-artifact contract fails.
        TypeError: If the manifest or artifact mapping has the wrong type.
    """
    if not isinstance(manifest, bytes):
        raise TypeError("manifest must be bytes")
    if len(manifest) > MAX_MANIFEST_BYTES:
        raise StockDailyGapEvidenceError("manifest exceeds the byte limit")
    if not isinstance(artifacts, Mapping):
        raise TypeError("artifacts must be a mapping of evidence refs to bytes")

    manifest_value = _json_object(manifest, "manifest")
    _exact_keys(
        manifest_value,
        {"schema_version", "registry_ref", "artifacts", "series"},
        "manifest",
    )
    _schema_version(manifest_value["schema_version"], "manifest.schema_version")
    registry_ref = _label(manifest_value["registry_ref"], "manifest.registry_ref")
    descriptors = _read_artifact_descriptors(manifest_value["artifacts"])
    if len(descriptors) > MAX_ARTIFACTS:
        raise StockDailyGapEvidenceError(f"artifacts exceeds {MAX_ARTIFACTS:,} entries")
    if set(artifacts) != set(descriptors):
        raise StockDailyGapEvidenceError(
            "artifact byte refs must exactly match the manifest artifact refs"
        )

    total_bytes = 0
    parsed_artifacts: dict[str, dict[str, object]] = {}
    verified: dict[str, VerifiedEvidenceArtifact] = {}
    instrument_registry: InstrumentReferenceRegistry | None = None
    for reference, (kind, expected_hash) in descriptors.items():
        raw = artifacts[reference]
        if not isinstance(raw, bytes):
            raise TypeError(f"artifact {reference!r} must be bytes")
        artifact_limit = (
            MAX_INSTRUMENT_REFERENCE_SNAPSHOT_BYTES
            if kind == "instrument_registry"
            else MAX_ARTIFACT_BYTES
        )
        if len(raw) > artifact_limit:
            raise StockDailyGapEvidenceError(f"artifact {reference!r} exceeds the byte limit")
        total_bytes += len(raw)
        if total_bytes > MAX_TOTAL_ARTIFACT_BYTES:
            raise StockDailyGapEvidenceError("total artifact bytes exceed the bundle limit")
        actual_hash = hashlib.sha256(raw).hexdigest()
        if actual_hash != expected_hash:
            raise StockDailyGapEvidenceError(f"artifact hash mismatch for {reference!r}")
        metadata: dict[str, str | None]
        if kind == "instrument_registry":
            if instrument_registry is not None:
                raise StockDailyGapEvidenceError("manifest must contain one registry snapshot")
            try:
                instrument_registry = decode_instrument_reference_snapshot(raw)
            except ValueError as exc:
                raise StockDailyGapEvidenceError(
                    "instrument registry snapshot fails its existing strict codec"
                ) from exc
            if instrument_registry.evidence_ref != reference:
                raise StockDailyGapEvidenceError("registry snapshot evidence_ref mismatch")
            metadata = {
                "dataset_id": None,
                "source_id": None,
                "instrument_id": None,
                "publication_id": None,
            }
        elif kind == "daily_publication_content":
            document = _json_object(raw, f"publication content artifact {reference!r}")
            _read_publication_content(document)
            parsed_artifacts[reference] = document
            metadata = {
                "dataset_id": None,
                "source_id": None,
                "instrument_id": None,
                "publication_id": None,
            }
        else:
            document = _json_object(raw, f"artifact {reference!r}")
            metadata = _validate_artifact_document(reference, kind, document)
            parsed_artifacts[reference] = document
        verified[reference] = VerifiedEvidenceArtifact(
            evidence_ref=reference,
            kind=kind,
            sha256=actual_hash,
            byte_count=len(raw),
            dataset_id=metadata["dataset_id"],
            source_id=metadata["source_id"],
            instrument_id=metadata["instrument_id"],
            publication_id=metadata["publication_id"],
        )

    registry_pin = verified.get(registry_ref)
    if registry_pin is None or registry_pin.kind != "instrument_registry":
        raise StockDailyGapEvidenceError("registry_ref must point to the registry snapshot")
    if instrument_registry is None:
        raise StockDailyGapEvidenceError("registry snapshot is missing")
    if registry_ref != instrument_registry.evidence_ref:
        raise StockDailyGapEvidenceError("registry_ref differs from registry.evidence_ref")

    raw_series = manifest_value["series"]
    if not isinstance(raw_series, list) or not raw_series:
        raise StockDailyGapEvidenceError("manifest.series must be a nonempty array")
    if len(raw_series) > MAX_SERIES:
        raise StockDailyGapEvidenceError(f"series exceeds {MAX_SERIES:,} entries")

    # Identical publication-content bytes must not be re-bound to multiple
    # series, even when the manifest gives the same bytes different refs.
    content_hashes: set[str] = set()
    for index, raw_entry in enumerate(raw_series):
        if not isinstance(raw_entry, dict):
            continue
        publication_ref = _optional_ref(
            raw_entry.get("publication_ref"), f"manifest.series[{index}].publication_ref"
        )
        if publication_ref is None:
            continue
        publication_document = _require_artifact(
            publication_ref, "daily_publication", parsed_artifacts, verified
        )
        publication_payload = _payload(publication_document, "daily_publication")
        _exact_keys(publication_payload, {"content_ref"}, "daily_publication.payload")
        content_ref = _label(publication_payload["content_ref"], "daily_publication.content_ref")
        _require_publication_content(content_ref, parsed_artifacts, verified)
        content_sha256 = verified[content_ref].sha256
        if content_sha256 in content_hashes:
            raise StockDailyGapEvidenceError(
                "identical publication content bytes cannot be reused by multiple series"
            )
        content_hashes.add(content_sha256)

    parsed_series: list[StockDailySeriesEvidence] = []
    used_refs: set[str] = {registry_ref}
    identities: set[tuple[str, str, str]] = set()
    total_calendar_dates = 0
    total_reconciliation_dates = 0
    for index, raw_entry in enumerate(raw_series):
        entry_name = f"manifest.series[{index}]"
        if not isinstance(raw_entry, dict):
            raise StockDailyGapEvidenceError(f"{entry_name} must be an object")
        _exact_keys(
            raw_entry,
            {
                "dataset_id",
                "source_id",
                "instrument_identifier",
                "identity_as_of",
                "request_start",
                "request_end",
                "publication_ref",
                "publication_id",
                "calendar_ref",
                "expectation_ref",
                "lifecycle_ref",
                "retention_ref",
                "delisted_end_ref",
            },
            entry_name,
        )
        dataset_id = _label(raw_entry["dataset_id"], f"{entry_name}.dataset_id")
        source_id = _label(raw_entry["source_id"], f"{entry_name}.source_id")
        identifier = _label(
            raw_entry["instrument_identifier"], f"{entry_name}.instrument_identifier"
        )
        identity_as_of = _optional_date(raw_entry["identity_as_of"], f"{entry_name}.identity_as_of")
        request_start = _date(raw_entry["request_start"], f"{entry_name}.request_start")
        request_end = _date(raw_entry["request_end"], f"{entry_name}.request_end")
        if request_start > request_end:
            raise StockDailyGapEvidenceError(f"{entry_name} request_start follows request_end")

        try:
            resolved = instrument_registry.resolve(
                dataset_id, source_id, identifier, as_of=identity_as_of
            )
        except (TypeError, ValueError) as exc:
            raise StockDailyGapEvidenceError(
                f"{entry_name} instrument does not resolve in the supplied registry"
            ) from exc
        canonical_id = resolved.instrument_id
        if resolved.alias_evidence_ref is not None and (
            (resolved.alias_valid_from is not None and request_start < resolved.alias_valid_from)
            or (
                resolved.alias_valid_through is not None
                and request_end > resolved.alias_valid_through
            )
        ):
            raise StockDailyGapEvidenceError(
                f"{entry_name} alias is not valid across the full requested history"
            )
        identity = (dataset_id, source_id, canonical_id)
        if identity in identities:
            raise StockDailyGapEvidenceError("duplicate dataset/source/instrument series")
        identities.add(identity)
        lifecycle = resolved.lifecycle

        publication_ref = _optional_ref(
            raw_entry["publication_ref"], f"{entry_name}.publication_ref"
        )
        publication_id = _optional_ref(raw_entry["publication_id"], f"{entry_name}.publication_id")
        if (publication_ref is None) != (publication_id is None):
            raise StockDailyGapEvidenceError(
                f"{entry_name} publication_ref and publication_id must be present together"
            )
        calendar_ref = _label(raw_entry["calendar_ref"], f"{entry_name}.calendar_ref")
        expectation_ref = _label(raw_entry["expectation_ref"], f"{entry_name}.expectation_ref")
        lifecycle_ref = _label(raw_entry["lifecycle_ref"], f"{entry_name}.lifecycle_ref")
        retention_ref = _optional_ref(raw_entry["retention_ref"], f"{entry_name}.retention_ref")
        delisted_end_ref = _optional_ref(
            raw_entry["delisted_end_ref"], f"{entry_name}.delisted_end_ref"
        )

        lifecycle_document = _require_artifact(
            lifecycle_ref, "instrument_status", parsed_artifacts, verified
        )
        if lifecycle_ref != lifecycle.status_evidence_ref:
            raise StockDailyGapEvidenceError(
                f"{entry_name}.lifecycle_ref differs from the registry status evidence"
            )
        _assert_identity(
            lifecycle_document, dataset_id, source_id, canonical_id, None, lifecycle_ref
        )
        _assert_lifecycle_payload(lifecycle_document, lifecycle, instrument_registry)

        if lifecycle.start_boundary_unknown:
            start_ref = None
        else:
            start_ref = lifecycle.start_evidence_ref
            if start_ref is None:
                raise StockDailyGapEvidenceError("known registry start has no evidence ref")
            start_document = _require_artifact(
                start_ref, "instrument_start", parsed_artifacts, verified
            )
            _assert_identity(start_document, dataset_id, source_id, canonical_id, None, start_ref)
            start_payload = _payload(start_document, "instrument_start")
            _exact_keys(start_payload, {"start_date"}, "instrument_start.payload")
            if _date(start_payload["start_date"], "instrument_start.start_date") != (
                lifecycle.known_start_date
            ):
                raise StockDailyGapEvidenceError("listing evidence differs from registry start")
            used_refs.add(start_ref)

        if resolved.alias_evidence_ref is not None:
            alias_ref = resolved.alias_evidence_ref
            alias_document = _require_artifact(
                alias_ref, "instrument_alias", parsed_artifacts, verified
            )
            _assert_identity(alias_document, dataset_id, source_id, canonical_id, None, alias_ref)
            _assert_alias_payload(
                alias_document,
                alias=next(
                    row
                    for row in instrument_registry.aliases
                    if row.evidence_ref == alias_ref
                    and row.alias == identifier
                    and row.instrument_id == canonical_id
                ),
            )
            used_refs.add(alias_ref)

        calendar_document = _require_artifact(
            calendar_ref, "business_calendar", parsed_artifacts, verified
        )
        expectation_document = _require_artifact(
            expectation_ref, "instrument_expectation", parsed_artifacts, verified
        )
        _assert_identity(calendar_document, dataset_id, source_id, canonical_id, None, calendar_ref)
        _assert_identity(
            expectation_document, dataset_id, source_id, canonical_id, None, expectation_ref
        )
        calendar = _read_calendar(calendar_document)
        expectation = _read_expectation(expectation_document, calendar_ref)
        if (
            expectation["calendar_id"] != calendar["calendar_id"]
            or expectation["calendar_version"] != calendar["calendar_version"]
            or expectation["covered_from"] < calendar["covered_from"]
            or expectation["covered_through"] > calendar["covered_through"]
        ):
            raise StockDailyGapEvidenceError("calendar and instrument evidence bounds differ")
        sessions = set(calendar["session_dates"])
        expected_dates = expectation["expected_business_dates"]
        unresolved_dates = expectation["unresolved_observation_dates"]
        if not set(expected_dates).issubset(sessions) or not set(unresolved_dates).issubset(
            sessions
        ):
            raise StockDailyGapEvidenceError(
                "instrument observation dates contain dates outside the pinned calendar"
            )
        if expectation["calendar_id"] != calendar["calendar_id"]:
            raise StockDailyGapEvidenceError("calendar identity changed between evidence files")

        publication: DailyPublicationSnapshot | None = None
        observed_dates: tuple[date, ...] = ()
        if publication_ref is not None:
            publication_document = _require_artifact(
                publication_ref, "daily_publication", parsed_artifacts, verified
            )
            _assert_identity(
                publication_document,
                dataset_id,
                source_id,
                canonical_id,
                publication_id,
                publication_ref,
            )
            publication_payload = _payload(publication_document, "daily_publication")
            _exact_keys(publication_payload, {"content_ref"}, "daily_publication.payload")
            publication_content_ref = _label(
                publication_payload["content_ref"], "daily_publication.content_ref"
            )
            publication_content_document = _require_publication_content(
                publication_content_ref, parsed_artifacts, verified
            )
            artifact_publication_id = _label(
                publication_document["publication_id"], "daily_publication.publication_id"
            )
            if artifact_publication_id != publication_id:
                raise StockDailyGapEvidenceError(
                    "publication artifact ID differs from the manifest publication pin"
                )
            observed_dates = _read_publication_content(
                publication_content_document,
                dataset_id=dataset_id,
                source_id=source_id,
                instrument_id=canonical_id,
                publication_id=publication_id,
            )
            publication = DailyPublicationSnapshot(
                dataset_id=dataset_id,
                source_id=source_id,
                instrument_id=canonical_id,
                publication_id=artifact_publication_id,
                content_sha256=verified[publication_content_ref].sha256,
                observed_business_dates=observed_dates,
            )
            used_refs.add(publication_content_ref)
        total_calendar_dates += len(calendar["session_dates"])
        # Match the existing reconciliation work accounting: its coverage and
        # typed publication each retain observations, and its coverage and
        # expectation evidence each retain expected business dates.
        total_reconciliation_dates += (
            2 * len(expected_dates) + 2 * len(observed_dates) + len(unresolved_dates)
        )
        if total_calendar_dates > MAX_TOTAL_DATES:
            raise StockDailyGapEvidenceError("total calendar dates exceed the bundle limit")
        if total_reconciliation_dates > MAX_TOTAL_DATES:
            raise StockDailyGapEvidenceError(
                "total reconciliation date entries exceed the existing evidence limit"
            )
        if (
            max(
                len(calendar["session_dates"]),
                len(expected_dates),
                len(unresolved_dates),
                len(observed_dates),
            )
            > MAX_DATE_ENTRIES_PER_SERIES
        ):
            raise StockDailyGapEvidenceError("a series exceeds the business-date entry limit")

        retention_from: date | None = None
        retention_through: date | None = None
        if retention_ref is not None:
            retention_document = _require_artifact(
                retention_ref, "source_retention", parsed_artifacts, verified
            )
            _assert_identity(
                retention_document, dataset_id, source_id, canonical_id, None, retention_ref
            )
            retention_payload = _payload(retention_document, "source_retention")
            _exact_keys(
                retention_payload,
                {"retained_from", "retained_through"},
                "source_retention.payload",
            )
            retention_from = _optional_date(
                retention_payload["retained_from"], "source_retention.retained_from"
            )
            retention_through = _optional_date(
                retention_payload["retained_through"], "source_retention.retained_through"
            )
            if retention_from is None and retention_through is None:
                raise StockDailyGapEvidenceError(
                    "source retention evidence must provide at least one boundary"
                )
            used_refs.add(retention_ref)

        if lifecycle.status is LifecycleStatus.ACTIVE:
            if delisted_end_ref is not None:
                raise StockDailyGapEvidenceError("active lifecycle cannot carry delisted_end_ref")
            terminal = EndBoundaryEvidence(
                dataset_id,
                canonical_id,
                EndBoundaryKind.ACTIVE_AS_OF,
                boundary_date=lifecycle.status_as_of,
                evidence_ref=lifecycle.status_evidence_ref,
            )
        elif lifecycle.status is LifecycleStatus.DELISTED and delisted_end_ref is not None:
            end_document = _require_artifact(
                delisted_end_ref, "delisted_end_boundary", parsed_artifacts, verified
            )
            _assert_identity(
                end_document, dataset_id, source_id, canonical_id, None, delisted_end_ref
            )
            end_payload = _payload(end_document, "delisted_end_boundary")
            _exact_keys(end_payload, {"boundary_date"}, "delisted_end_boundary.payload")
            boundary_date = _date(
                end_payload["boundary_date"], "delisted_end_boundary.boundary_date"
            )
            if lifecycle.delisted_on is None or boundary_date > lifecycle.delisted_on:
                raise StockDailyGapEvidenceError(
                    "delisted end boundary follows the independently recorded delisting date"
                )
            if not expected_dates or max(expected_dates) != boundary_date:
                raise StockDailyGapEvidenceError(
                    "delisted end boundary must equal the last evidenced expected date"
                )
            terminal = EndBoundaryEvidence(
                dataset_id,
                canonical_id,
                EndBoundaryKind.DELISTED_LAST_EXPECTED_OBSERVATION,
                boundary_date=boundary_date,
                evidence_ref=delisted_end_ref,
            )
            used_refs.add(delisted_end_ref)
        else:
            if delisted_end_ref is not None:
                raise StockDailyGapEvidenceError(
                    "delisted_end_ref is valid only for a delisted registry lifecycle"
                )
            terminal = EndBoundaryEvidence(dataset_id, canonical_id, EndBoundaryKind.UNKNOWN)

        coverage_request = HistoryCoverageRequest(
            dataset=dataset_id,
            source=source_id,
            instrument_key=canonical_id,
            request_start=request_start,
            request_end=request_end,
            expected_business_dates=expected_dates,
            observed_business_dates=observed_dates,
            calendar_id=calendar["calendar_id"],
            calendar_version=calendar["calendar_version"],
            business_calendar_evidence=calendar_ref,
            calendar_covered_from=calendar["covered_from"],
            calendar_covered_through=calendar["covered_through"],
            known_start_date=lifecycle.known_start_date,
            start_boundary_unknown=lifecycle.start_boundary_unknown,
            independent_start_evidence=start_ref,
            source_retained_from=retention_from,
            source_retained_through=retention_through,
            source_retention_evidence=retention_ref,
        )
        expected_evidence = InstrumentExpectedObservationEvidence(
            dataset_id=dataset_id,
            source_id=source_id,
            instrument_id=canonical_id,
            calendar_id=calendar["calendar_id"],
            calendar_version=calendar["calendar_version"],
            calendar_evidence_ref=calendar_ref,
            instrument_evidence_ref=expectation_ref,
            covered_from=expectation["covered_from"],
            covered_through=expectation["covered_through"],
            expected_business_dates=expected_dates,
            unresolved_observation_dates=unresolved_dates,
        )
        parsed_series.append(
            StockDailySeriesEvidence(
                coverage_request=coverage_request,
                publication=publication,
                expected_observations=expected_evidence,
                end_boundary=terminal,
            )
        )
        used_refs.update({calendar_ref, expectation_ref, lifecycle_ref})
        if publication_ref is not None:
            used_refs.add(publication_ref)

    # Include every independent registry boundary and alias document actually
    # referenced by the injected registry for the selected series.
    for item in parsed_series:
        req = item.coverage_request
        lifecycle = instrument_registry.resolve(
            req.dataset, req.source, req.instrument_key or ""
        ).lifecycle
        _require_artifact(
            lifecycle.status_evidence_ref, "instrument_status", parsed_artifacts, verified
        )
        used_refs.add(lifecycle.status_evidence_ref)
        if lifecycle.start_evidence_ref is not None:
            used_refs.add(lifecycle.start_evidence_ref)
        if lifecycle.delist_evidence_ref is not None:
            delist_doc = _require_artifact(
                lifecycle.delist_evidence_ref,
                "instrument_delisting",
                parsed_artifacts,
                verified,
            )
            _assert_identity(
                delist_doc,
                req.dataset,
                req.source,
                req.instrument_key or "",
                None,
                lifecycle.delist_evidence_ref,
            )
            delist_payload = _payload(delist_doc, "instrument_delisting")
            _exact_keys(delist_payload, {"delisted_on"}, "instrument_delisting.payload")
            if _date(delist_payload["delisted_on"], "instrument_delisting.delisted_on") != (
                lifecycle.delisted_on
            ):
                raise StockDailyGapEvidenceError("delisting evidence differs from registry")
            used_refs.add(lifecycle.delist_evidence_ref)
        resolved = instrument_registry.resolve(req.dataset, req.source, req.instrument_key or "")
        if resolved.alias_evidence_ref is not None:
            used_refs.add(resolved.alias_evidence_ref)

    if used_refs != set(descriptors):
        unused = set(descriptors) - used_refs
        missing = used_refs - set(descriptors)
        raise StockDailyGapEvidenceError(
            f"artifact references are incomplete or unused (missing={len(missing)}, "
            f"unused={len(unused)})"
        )

    ordered_artifacts = tuple(verified[key] for key in sorted(verified))
    series_tuple = tuple(parsed_series)
    reconciliation = reconcile_stock_daily_gaps(series_tuple)
    return StockDailyGapEvidenceBundle(
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        series_evidence=series_tuple,
        artifacts=ordered_artifacts,
        reconciliation=reconciliation,
    )


def _read_artifact_descriptors(value: object) -> dict[str, tuple[str, str]]:
    """Validate unique evidence refs, kinds, and lowercase hashes."""
    if not isinstance(value, list):
        raise StockDailyGapEvidenceError("manifest.artifacts must be an array")
    descriptors: dict[str, tuple[str, str]] = {}
    for index, item in enumerate(value):
        name = f"manifest.artifacts[{index}]"
        if not isinstance(item, dict):
            raise StockDailyGapEvidenceError(f"{name} must be an object")
        _exact_keys(item, {"evidence_ref", "kind", "sha256"}, name)
        reference = _label(item["evidence_ref"], f"{name}.evidence_ref")
        kind = _label(item["kind"], f"{name}.kind")
        digest = item["sha256"]
        if kind not in _ARTIFACT_KINDS:
            raise StockDailyGapEvidenceError(f"{name}.kind is unsupported")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise StockDailyGapEvidenceError(f"{name}.sha256 must be lowercase SHA-256")
        if reference in descriptors:
            raise StockDailyGapEvidenceError("manifest contains duplicate evidence refs")
        descriptors[reference] = (kind, digest)
    return descriptors


def _validate_artifact_document(
    reference: str, kind: str, document: dict[str, object]
) -> dict[str, str | None]:
    """Validate common JSON envelope fields and return its declared scope."""
    required = {
        "evidence_ref",
        "schema_version",
        "dataset_id",
        "source_id",
        "instrument_id",
        "publication_id",
        "payload",
    }
    _exact_keys(document, required, f"artifact {reference!r}")
    _schema_version(document["schema_version"], f"artifact {reference!r}.schema_version")
    if document["evidence_ref"] != reference:
        raise StockDailyGapEvidenceError(f"artifact ref mismatch for {reference!r}")
    global_registry = kind == "instrument_registry"
    identifiers: dict[str, str | None] = {}
    for field in ("dataset_id", "source_id", "instrument_id", "publication_id"):
        value = document[field]
        if value is None and (field == "publication_id" or global_registry):
            identifiers[field] = None
        else:
            identifiers[field] = _label(value, f"artifact {reference!r}.{field}")
    if kind == "daily_publication" and identifiers["publication_id"] is None:
        raise StockDailyGapEvidenceError("daily publication requires publication_id")
    if kind != "daily_publication" and identifiers["publication_id"] is not None:
        raise StockDailyGapEvidenceError("only a daily publication may declare publication_id")
    if global_registry and any(
        identifiers[field] is not None for field in ("dataset_id", "source_id", "instrument_id")
    ):
        raise StockDailyGapEvidenceError("registry artifact must use global null identity fields")
    if not global_registry and any(
        identifiers[field] is None for field in ("dataset_id", "source_id", "instrument_id")
    ):
        raise StockDailyGapEvidenceError("series evidence must bind dataset/source/instrument")
    _payload(document, kind)
    return identifiers


def _assert_identity(
    document: dict[str, object],
    dataset_id: str,
    source_id: str,
    instrument_id: str,
    publication_id: str | None,
    reference: str,
) -> None:
    """Require exact dataset/source/instrument/publication identity binding."""
    if (
        document.get("dataset_id") != dataset_id
        or document.get("source_id") != source_id
        or document.get("instrument_id") != instrument_id
    ):
        raise StockDailyGapEvidenceError(f"artifact identity mismatch for {reference!r}")
    actual_publication = document.get("publication_id")
    if actual_publication != publication_id:
        raise StockDailyGapEvidenceError(f"artifact publication binding mismatch for {reference!r}")


def _read_calendar(document: dict[str, object]) -> _BusinessCalendar:
    """Parse one exact calendar artifact with a bounded unique session list."""
    payload = _payload(document, "business_calendar")
    _exact_keys(
        payload,
        {"calendar_id", "calendar_version", "covered_from", "covered_through", "session_dates"},
        "business_calendar.payload",
    )
    start = _date(payload["covered_from"], "business_calendar.covered_from")
    end = _date(payload["covered_through"], "business_calendar.covered_through")
    if start > end:
        raise StockDailyGapEvidenceError("calendar covered_from follows covered_through")
    sessions = _date_tuple(payload["session_dates"], "business_calendar.session_dates")
    if len(sessions) != len(set(sessions)) or tuple(sorted(sessions)) != sessions:
        raise StockDailyGapEvidenceError("calendar session_dates must be sorted and unique")
    if any(day < start or day > end for day in sessions):
        raise StockDailyGapEvidenceError("calendar session falls outside its declared bounds")
    return {
        "calendar_id": _label(payload["calendar_id"], "business_calendar.calendar_id"),
        "calendar_version": _label(
            payload["calendar_version"], "business_calendar.calendar_version"
        ),
        "covered_from": start,
        "covered_through": end,
        "session_dates": sessions,
    }


def _read_expectation(document: dict[str, object], calendar_ref: str) -> _InstrumentExpectation:
    """Parse per-instrument expectations and bind their exact calendar pin."""
    payload = _payload(document, "instrument_expectation")
    _exact_keys(
        payload,
        {
            "calendar_ref",
            "calendar_id",
            "calendar_version",
            "covered_from",
            "covered_through",
            "expected_business_dates",
            "unresolved_observation_dates",
        },
        "instrument_expectation.payload",
    )
    if payload["calendar_ref"] != calendar_ref:
        raise StockDailyGapEvidenceError("expectation calendar_ref differs from manifest binding")
    start = _date(payload["covered_from"], "instrument_expectation.covered_from")
    end = _date(payload["covered_through"], "instrument_expectation.covered_through")
    if start > end:
        raise StockDailyGapEvidenceError("expectation covered_from follows covered_through")
    expected = _date_tuple(
        payload["expected_business_dates"], "instrument_expectation.expected_business_dates"
    )
    unresolved = _date_tuple(
        payload["unresolved_observation_dates"],
        "instrument_expectation.unresolved_observation_dates",
    )
    for field_name, dates in (("expected", expected), ("unresolved", unresolved)):
        if len(dates) != len(set(dates)) or tuple(sorted(dates)) != dates:
            raise StockDailyGapEvidenceError(
                f"expectation {field_name} dates must be sorted/unique"
            )
        if any(day < start or day > end for day in dates):
            raise StockDailyGapEvidenceError(f"expectation {field_name} date is outside its bounds")
    if set(expected) & set(unresolved):
        raise StockDailyGapEvidenceError("expected and unresolved dates overlap")
    return {
        "calendar_ref": calendar_ref,
        "calendar_id": _label(payload["calendar_id"], "instrument_expectation.calendar_id"),
        "calendar_version": _label(
            payload["calendar_version"], "instrument_expectation.calendar_version"
        ),
        "covered_from": start,
        "covered_through": end,
        "expected_business_dates": expected,
        "unresolved_observation_dates": unresolved,
    }


def _assert_lifecycle_payload(
    document: dict[str, object],
    lifecycle: InstrumentLifecycle,
    registry: InstrumentReferenceRegistry,
) -> None:
    """Bind the hashed lifecycle evidence document to the reviewed registry row."""
    payload = _payload(document, "instrument_status")
    expected: dict[str, object] = {
        "registry_version": registry.registry_version,
        "lifecycle_version": lifecycle.lifecycle_version,
        "status": LifecycleStatus(lifecycle.status).value,
        "status_as_of": lifecycle.status_as_of.isoformat(),
        "status_evidence_ref": lifecycle.status_evidence_ref,
        "known_start_date": (
            lifecycle.known_start_date.isoformat()
            if lifecycle.known_start_date is not None
            else None
        ),
        "start_boundary_unknown": lifecycle.start_boundary_unknown,
        "start_evidence_ref": lifecycle.start_evidence_ref,
        "delisted_on": lifecycle.delisted_on.isoformat() if lifecycle.delisted_on else None,
        "delist_evidence_ref": lifecycle.delist_evidence_ref,
    }
    _exact_keys(payload, set(expected), "instrument_status.payload")
    if type(payload["start_boundary_unknown"]) is not bool:
        raise StockDailyGapEvidenceError(
            "instrument_status.start_boundary_unknown must be a JSON boolean"
        )
    if payload != expected:
        raise StockDailyGapEvidenceError("status/lifecycle evidence differs from registry row")
    if document.get("evidence_ref") != lifecycle.status_evidence_ref:
        raise StockDailyGapEvidenceError("status artifact ref differs from registry row")


def _assert_alias_payload(document: dict[str, object], alias: InstrumentAlias) -> None:
    """Bind a hashed alias document to the exact reviewed registry alias."""
    payload = _payload(document, "instrument_alias")
    expected: dict[str, object] = {
        "alias": alias.alias,
        "instrument_id": alias.instrument_id,
        "alias_version": alias.alias_version,
        "valid_from": alias.valid_from.isoformat() if alias.valid_from else None,
        "valid_through": alias.valid_through.isoformat() if alias.valid_through else None,
        "evidence_ref": alias.evidence_ref,
    }
    _exact_keys(payload, set(expected), "instrument_alias.payload")
    if payload != expected or document.get("evidence_ref") != alias.evidence_ref:
        raise StockDailyGapEvidenceError("alias evidence differs from registry alias")


def _payload(document: dict[str, object], kind: str) -> dict[str, object]:
    """Return one artifact payload object, rejecting missing or non-object data."""
    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise StockDailyGapEvidenceError(f"{kind}.payload must be an object")
    return payload


def _require_artifact(
    reference: str,
    expected_kind: str,
    parsed: Mapping[str, dict[str, object]],
    verified: Mapping[str, VerifiedEvidenceArtifact],
) -> dict[str, object]:
    """Find a parsed artifact and enforce the manifest's declared kind."""
    document = parsed.get(reference)
    pin = verified.get(reference)
    if document is None or pin is None:
        raise StockDailyGapEvidenceError(f"required evidence artifact is missing: {reference!r}")
    if pin.kind != expected_kind:
        raise StockDailyGapEvidenceError(f"evidence kind mismatch for {reference!r}")
    if document.get("evidence_ref") != reference:
        raise StockDailyGapEvidenceError(f"evidence document ref mismatch for {reference!r}")
    return document


def _require_publication_content(
    reference: str,
    parsed: Mapping[str, dict[str, object]],
    verified: Mapping[str, VerifiedEvidenceArtifact],
) -> dict[str, object]:
    """Find the separately hashed raw-content artifact for one publication."""
    document = parsed.get(reference)
    pin = verified.get(reference)
    if document is None or pin is None:
        raise StockDailyGapEvidenceError(f"publication content artifact is missing: {reference!r}")
    if pin.kind != "daily_publication_content":
        raise StockDailyGapEvidenceError(f"publication content kind mismatch for {reference!r}")
    return document


def _read_publication_content(
    document: dict[str, object],
    *,
    dataset_id: str | None = None,
    source_id: str | None = None,
    instrument_id: str | None = None,
    publication_id: str | None = None,
) -> tuple[date, ...]:
    """Parse dates from the bounded exact input bytes used for the content hash.

    Rows may contain provider business fields in addition to ``trade_date``;
    those fields remain inside the byte-pinned artifact. When canonical scope
    keys are present in a row, they must match the metadata envelope.
    """
    _exact_keys(document, {"rows"}, "daily_publication_content")
    rows = document["rows"]
    if not isinstance(rows, list):
        raise StockDailyGapEvidenceError("daily_publication_content.rows must be an array")
    if len(rows) > MAX_DATE_ENTRIES_PER_SERIES:
        raise StockDailyGapEvidenceError(
            "daily_publication_content.rows exceeds the per-series date limit"
        )

    dates: list[date] = []
    for index, value in enumerate(rows):
        if not isinstance(value, dict):
            raise StockDailyGapEvidenceError(
                f"daily_publication_content.rows[{index}] must be an object"
            )
        if "trade_date" not in value:
            raise StockDailyGapEvidenceError(
                f"daily_publication_content.rows[{index}].trade_date is required"
            )
        dates.append(
            _date(value["trade_date"], f"daily_publication_content.rows[{index}].trade_date")
        )
        for field, expected in (
            ("dataset_id", dataset_id),
            ("source_id", source_id),
            ("instrument_id", instrument_id),
            ("publication_id", publication_id),
        ):
            if field in value and expected is not None and value[field] != expected:
                raise StockDailyGapEvidenceError(
                    f"publication content row {field} differs from its metadata envelope"
                )
    if len(dates) != len(set(dates)):
        raise StockDailyGapEvidenceError(
            "daily_publication_content rows must have unique trade_date values"
        )
    return tuple(dates)


def _json_object(raw: bytes, name: str) -> dict[str, object]:
    """Parse strict UTF-8 JSON and reject duplicate keys or non-object roots."""
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise StockDailyGapEvidenceError(f"{name} is not valid strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise StockDailyGapEvidenceError(f"{name} root must be an object")
    return value


def _reject_json_constant(value: str) -> None:
    """Reject non-standard NaN/Infinity constants accepted by Python's parser."""
    raise ValueError(f"unsupported JSON constant {value}")


def _unique_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object and fail closed when a key is repeated."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _canonical_json(value: object) -> bytes:
    """Serialize deterministic JSON for content hashes and bundle output."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _exact_keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    """Require exact JSON object keys to prevent silent schema drift."""
    if set(value) != expected:
        raise StockDailyGapEvidenceError(f"{name} has missing or unsupported fields")


def _schema_version(value: object, name: str) -> None:
    """Require integer schema version 1, rejecting bool-as-int."""
    if type(value) is not int or value != SCHEMA_VERSION:
        raise StockDailyGapEvidenceError(f"{name} must be integer {SCHEMA_VERSION}")


def _label(value: object, name: str) -> str:
    """Return a bounded, trimmed, control-free identifier string."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 512
        or any(ord(character) < 32 for character in value)
    ):
        raise StockDailyGapEvidenceError(f"{name} must be a bounded nonempty string")
    return value


def _optional_ref(value: object, name: str) -> str | None:
    """Validate an optional evidence reference."""
    return None if value is None else _label(value, name)


def _optional_date(value: object, name: str) -> date | None:
    """Parse an optional canonical ISO business date."""
    return None if value is None else _date(value, name)


def _date(value: object, name: str) -> date:
    """Parse only exact YYYY-MM-DD values, excluding timestamp coercion."""
    if not isinstance(value, str) or len(value) != 10:
        raise StockDailyGapEvidenceError(f"{name} must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise StockDailyGapEvidenceError(f"{name} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise StockDailyGapEvidenceError(f"{name} must use canonical YYYY-MM-DD")
    return parsed


def _date_tuple(value: object, name: str) -> tuple[date, ...]:
    """Parse a bounded date array and return dates in their supplied order."""
    if not isinstance(value, list):
        raise StockDailyGapEvidenceError(f"{name} must be an array")
    if len(value) > MAX_DATE_ENTRIES_PER_SERIES:
        raise StockDailyGapEvidenceError(f"{name} exceeds the per-series date limit")
    return tuple(_date(item, f"{name}[{index}]") for index, item in enumerate(value))


__all__ = [
    "MAX_ARTIFACTS",
    "MAX_ARTIFACT_BYTES",
    "MAX_DATE_ENTRIES_PER_SERIES",
    "MAX_MANIFEST_BYTES",
    "MAX_SERIES",
    "MAX_TOTAL_ARTIFACT_BYTES",
    "MAX_TOTAL_DATES",
    "SCHEMA_VERSION",
    "StockDailyGapEvidenceBundle",
    "StockDailyGapEvidenceError",
    "VerifiedEvidenceArtifact",
    "load_stock_daily_gap_evidence_bundle",
]
