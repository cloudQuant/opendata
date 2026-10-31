"""Offline contract tests for hash-pinned stock-daily evidence loading."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import TYPE_CHECKING

import pytest

from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.instrument_reference_snapshot import encode_instrument_reference_snapshot
from opendata.pipeline.history_coverage import CoverageStatus
from opendata.pipeline.stock_daily_gap_evidence_bundle import (
    StockDailyGapEvidenceError,
    load_stock_daily_gap_evidence_bundle,
)
from opendata.pipeline.stock_daily_reconcile import EndBoundaryKind

if TYPE_CHECKING:
    from collections.abc import Callable

DATASET = "test.stock.daily"
SOURCE = "test.provider.daily"
INSTRUMENT = "CN:600519"
START = date(2024, 2, 5)
END = date(2024, 2, 9)
SESSIONS = tuple(date.fromisoformat(f"2024-02-{day:02}") for day in range(5, 10))


def _json_bytes(value: object) -> bytes:
    """Encode a deterministic fixture document without using production serializer."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _registry(
    *,
    known_start: date | None = START,
    status: LifecycleStatus = LifecycleStatus.ACTIVE,
    with_alias: bool = False,
) -> InstrumentReferenceRegistry:
    """Create one reviewed in-memory lifecycle for the exact fixture series."""
    lifecycle = InstrumentLifecycle(
        dataset_id=DATASET,
        source_id=SOURCE,
        instrument_id=INSTRUMENT,
        lifecycle_version="life-v1",
        status=status,
        status_as_of=END,
        status_evidence_ref="status:600519:v1",
        known_start_date=known_start,
        start_boundary_unknown=known_start is None,
        start_evidence_ref="listing:600519:v1" if known_start is not None else None,
        delisted_on=END if status is LifecycleStatus.DELISTED else None,
        delist_evidence_ref="delisting:600519:v1" if status is LifecycleStatus.DELISTED else None,
    )
    aliases = (
        (
            InstrumentAlias(
                dataset_id=DATASET,
                source_id=SOURCE,
                alias="600519.SH",
                instrument_id=INSTRUMENT,
                alias_version="ticker-v1",
                evidence_ref="alias:600519:v1",
                valid_from=START,
                valid_through=END,
            ),
        )
        if with_alias
        else ()
    )
    return InstrumentReferenceRegistry(
        registry_version="registry-v1",
        evidence_ref="registry:stock-daily:v1",
        lifecycles=(lifecycle,),
        aliases=aliases,
    )


def _document(
    *,
    reference: str,
    dataset_id: str | None,
    source_id: str | None,
    instrument_id: str | None,
    payload: dict[str, object],
    publication_id: str | None = None,
) -> dict[str, object]:
    """Build one raw fixture evidence document with its declared identity."""
    return {
        "evidence_ref": reference,
        "schema_version": 1,
        "dataset_id": dataset_id,
        "source_id": source_id,
        "instrument_id": instrument_id,
        "publication_id": publication_id,
        "payload": payload,
    }


def _fixture(
    *,
    known_start: date | None = START,
    observed: tuple[date, ...] = SESSIONS,
    include_retention: bool = False,
    calendar_instrument: str = INSTRUMENT,
    status_start: date | None = None,
    duplicate_session: bool = False,
    calendar_sessions: tuple[date, ...] = SESSIONS,
    status: LifecycleStatus = LifecycleStatus.ACTIVE,
    with_alias: bool = False,
    delisted_end: date | None = None,
) -> tuple[bytes, dict[str, bytes], InstrumentReferenceRegistry]:
    """Return a complete strict JSON bundle whose evidence hashes are literal bytes."""
    registry = _registry(known_start=known_start, status=status, with_alias=with_alias)
    lifecycle = registry.lifecycles[0]
    if status_start is None:
        status_start = known_start
    session_dates = [item.isoformat() for item in calendar_sessions]
    if duplicate_session:
        session_dates[1] = session_dates[0]
    documents: dict[str, tuple[str, dict[str, object]]] = {
        lifecycle.status_evidence_ref: (
            "instrument_status",
            _document(
                reference=lifecycle.status_evidence_ref,
                dataset_id=DATASET,
                source_id=SOURCE,
                instrument_id=INSTRUMENT,
                payload={
                    "registry_version": registry.registry_version,
                    "lifecycle_version": lifecycle.lifecycle_version,
                    "status": lifecycle.status.value,
                    "status_as_of": lifecycle.status_as_of.isoformat(),
                    "status_evidence_ref": lifecycle.status_evidence_ref,
                    "known_start_date": status_start.isoformat() if status_start else None,
                    "start_boundary_unknown": lifecycle.start_boundary_unknown,
                    "start_evidence_ref": lifecycle.start_evidence_ref,
                    "delisted_on": lifecycle.delisted_on.isoformat()
                    if lifecycle.delisted_on
                    else None,
                    "delist_evidence_ref": lifecycle.delist_evidence_ref,
                },
            ),
        ),
        "calendar:600519:v1": (
            "business_calendar",
            _document(
                reference="calendar:600519:v1",
                dataset_id=DATASET,
                source_id=SOURCE,
                instrument_id=calendar_instrument,
                payload={
                    "calendar_id": "SSE",
                    "calendar_version": "sse-fixture-v1",
                    "covered_from": START.isoformat(),
                    "covered_through": END.isoformat(),
                    "session_dates": session_dates,
                },
            ),
        ),
        "expectation:600519:v1": (
            "instrument_expectation",
            _document(
                reference="expectation:600519:v1",
                dataset_id=DATASET,
                source_id=SOURCE,
                instrument_id=INSTRUMENT,
                payload={
                    "calendar_ref": "calendar:600519:v1",
                    "calendar_id": "SSE",
                    "calendar_version": "sse-fixture-v1",
                    "covered_from": START.isoformat(),
                    "covered_through": END.isoformat(),
                    "expected_business_dates": [item.isoformat() for item in SESSIONS],
                    "unresolved_observation_dates": [],
                },
            ),
        ),
        "publication:600519:v1": (
            "daily_publication",
            _document(
                reference="publication:600519:v1",
                dataset_id=DATASET,
                source_id=SOURCE,
                instrument_id=INSTRUMENT,
                publication_id="pub-20240209-001",
                payload={"content_ref": "publication-content:600519:v1"},
            ),
        ),
    }
    publication_content = _json_bytes(
        {
            "rows": [
                {"trade_date": item.isoformat(), "close": 100.0 + index}
                for index, item in enumerate(observed)
            ]
        }
    )
    if known_start is not None:
        documents[lifecycle.start_evidence_ref or ""] = (
            "instrument_start",
            _document(
                reference=lifecycle.start_evidence_ref or "",
                dataset_id=DATASET,
                source_id=SOURCE,
                instrument_id=INSTRUMENT,
                payload={"start_date": known_start.isoformat()},
            ),
        )
    if lifecycle.delist_evidence_ref is not None:
        documents[lifecycle.delist_evidence_ref] = (
            "instrument_delisting",
            _document(
                reference=lifecycle.delist_evidence_ref,
                dataset_id=DATASET,
                source_id=SOURCE,
                instrument_id=INSTRUMENT,
                payload={"delisted_on": lifecycle.delisted_on.isoformat()},
            ),
        )
    if with_alias:
        alias = registry.aliases[0]
        documents[alias.evidence_ref] = (
            "instrument_alias",
            _document(
                reference=alias.evidence_ref,
                dataset_id=DATASET,
                source_id=SOURCE,
                instrument_id=INSTRUMENT,
                payload={
                    "alias": alias.alias,
                    "instrument_id": alias.instrument_id,
                    "alias_version": alias.alias_version,
                    "valid_from": alias.valid_from.isoformat(),
                    "valid_through": alias.valid_through.isoformat(),
                    "evidence_ref": alias.evidence_ref,
                },
            ),
        )
    if include_retention:
        documents["retention:600519:v1"] = (
            "source_retention",
            _document(
                reference="retention:600519:v1",
                dataset_id=DATASET,
                source_id=SOURCE,
                instrument_id=INSTRUMENT,
                payload={
                    "retained_from": START.isoformat(),
                    "retained_through": END.isoformat(),
                },
            ),
        )
    if delisted_end is not None:
        documents["delisted-end:600519:v1"] = (
            "delisted_end_boundary",
            _document(
                reference="delisted-end:600519:v1",
                dataset_id=DATASET,
                source_id=SOURCE,
                instrument_id=INSTRUMENT,
                payload={"boundary_date": delisted_end.isoformat()},
            ),
        )

    artifact_bytes = {
        registry.evidence_ref: encode_instrument_reference_snapshot(registry),
        **{reference: _json_bytes(document) for reference, (_, document) in documents.items()},
        "publication-content:600519:v1": publication_content,
    }
    kinds = {registry.evidence_ref: "instrument_registry"}
    kinds.update({reference: kind for reference, (kind, _) in documents.items()})
    kinds["publication-content:600519:v1"] = "daily_publication_content"
    artifact_descriptors = [
        {
            "evidence_ref": reference,
            "kind": kinds[reference],
            "sha256": hashlib.sha256(artifact_bytes[reference]).hexdigest(),
        }
        for reference in sorted(artifact_bytes)
    ]
    series = {
        "dataset_id": DATASET,
        "source_id": SOURCE,
        "instrument_identifier": "600519.SH" if with_alias else INSTRUMENT,
        "identity_as_of": START.isoformat() if with_alias else None,
        "request_start": START.isoformat(),
        "request_end": END.isoformat(),
        "publication_ref": "publication:600519:v1",
        "publication_id": "pub-20240209-001",
        "calendar_ref": "calendar:600519:v1",
        "expectation_ref": "expectation:600519:v1",
        "lifecycle_ref": lifecycle.status_evidence_ref,
        "retention_ref": "retention:600519:v1" if include_retention else None,
        "delisted_end_ref": "delisted-end:600519:v1" if delisted_end is not None else None,
    }
    manifest = _json_bytes(
        {
            "schema_version": 1,
            "registry_ref": registry.evidence_ref,
            "artifacts": artifact_descriptors,
            "series": [series],
        }
    )
    return manifest, artifact_bytes, registry


def _repin_manifest(manifest: bytes, artifacts: dict[str, bytes]) -> bytes:
    """Return a manifest with current artifact hashes after a semantic mutation."""
    value = json.loads(manifest)
    value["artifacts"] = [
        {
            **item,
            "sha256": hashlib.sha256(artifacts[item["evidence_ref"]]).hexdigest(),
        }
        for item in value["artifacts"]
    ]
    return _json_bytes(value)


def test_loader_builds_typed_inputs_and_preserves_unknown_retention() -> None:
    """A missing observed session is found while omitted retention stays unknown."""
    observed = tuple(day for day in SESSIONS if day != SESSIONS[2])
    manifest, artifacts, _ = _fixture(observed=observed)

    bundle = load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)

    assert len(bundle.series_evidence) == 1
    typed = bundle.series_evidence[0]
    assert typed.coverage_request.instrument_key == INSTRUMENT
    assert typed.coverage_request.source_retained_from is None
    assert typed.coverage_request.source_retained_through is None
    item = bundle.reconciliation.series[0]
    assert item.diagnostic_gap_dates == (SESSIONS[2],)
    assert item.status is CoverageStatus.UNKNOWN
    assert bundle.reconciliation.all_a_universe_complete is False
    content_ref = "publication-content:600519:v1"
    publication_ref = "publication:600519:v1"
    assert typed.publication is not None
    assert typed.publication.content_sha256 == hashlib.sha256(artifacts[content_ref]).hexdigest()
    assert (
        typed.publication.content_sha256 != hashlib.sha256(artifacts[publication_ref]).hexdigest()
    )
    serialized = bundle.to_dict()
    scope = serialized["scope"]
    assert isinstance(scope, dict)
    verified_content = next(item for item in bundle.artifacts if item.evidence_ref == content_ref)
    assert (
        verified_content.to_dict()["hash_semantics"]
        == "exact_input_publication_content_artifact_bytes"
    )
    assert scope["single_security_is_not_an_all_a_census"] is True
    assert scope["source_authenticity"] == "not_authenticated_by_hashes"
    assert scope["publication_content_identity_binding"] == (
        "caller_asserted_by_envelope_content_ref"
    )
    assert scope["calendar_market_binding"] == "caller_evidence_only"
    assert json.loads(bundle.to_json())["bundle_sha256"] == serialized["bundle_sha256"]


def test_complete_single_series_still_does_not_attest_all_a_universe() -> None:
    """Full evidence may certify one series but never an A-share universe census."""
    manifest, artifacts, _ = _fixture(include_retention=True)

    bundle = load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)

    assert bundle.reconciliation.series[0].status is CoverageStatus.COMPLETE
    assert bundle.reconciliation.all_requested_series_complete is True
    assert bundle.reconciliation.all_a_universe_complete is False
    scope = bundle.to_dict()["scope"]
    assert isinstance(scope, dict)
    assert scope["universe_census_attested"] is False


def test_publication_content_hash_covers_business_fields_not_only_dates() -> None:
    """Changing a pinned row field changes content SHA even when dates stay fixed."""
    manifest, artifacts, _ = _fixture()
    content_ref = "publication-content:600519:v1"
    original = load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)
    changed_document = json.loads(artifacts[content_ref])
    changed_document["rows"][0]["close"] = 999.25
    artifacts[content_ref] = _json_bytes(changed_document)
    changed_manifest = _repin_manifest(manifest, artifacts)

    changed = load_stock_daily_gap_evidence_bundle(changed_manifest, artifacts=artifacts)

    assert changed.series_evidence[0].publication is not None
    assert original.series_evidence[0].publication is not None
    assert (
        changed.series_evidence[0].publication.content_sha256
        != original.series_evidence[0].publication.content_sha256
    )
    assert (
        changed.series_evidence[0].publication.observed_business_dates
        == original.series_evidence[0].publication.observed_business_dates
    )


def test_publication_content_must_be_bound_by_its_own_artifact_kind() -> None:
    """Metadata cannot point at another already-pinned JSON evidence artifact."""
    manifest, artifacts, _ = _fixture()
    publication_ref = "publication:600519:v1"
    publication = json.loads(artifacts[publication_ref])
    publication["payload"]["content_ref"] = "expectation:600519:v1"
    artifacts[publication_ref] = _json_bytes(publication)
    manifest = _repin_manifest(manifest, artifacts)

    with pytest.raises(StockDailyGapEvidenceError, match="publication content kind mismatch"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_publication_content_bytes_cannot_be_reused_across_series() -> None:
    """Different refs cannot make identical unscoped rows belong to two series."""
    manifest, artifacts, _ = _fixture()
    value = json.loads(manifest)
    second_publication_ref = "publication:another-security:v1"
    second_content_ref = "publication-content:another-security:v1"
    second_publication_id = "pub-another-security"
    second_series = {
        **value["series"][0],
        "dataset_id": "another.stock.daily",
        "instrument_identifier": "CN:000001",
        "publication_ref": second_publication_ref,
        "publication_id": second_publication_id,
    }
    second_publication = _document(
        reference=second_publication_ref,
        dataset_id="another.stock.daily",
        source_id=SOURCE,
        instrument_id="CN:000001",
        publication_id=second_publication_id,
        payload={"content_ref": second_content_ref},
    )
    artifacts[second_publication_ref] = _json_bytes(second_publication)
    artifacts[second_content_ref] = artifacts["publication-content:600519:v1"]
    value["artifacts"].append(
        {
            "evidence_ref": second_publication_ref,
            "kind": "daily_publication",
            "sha256": hashlib.sha256(artifacts[second_publication_ref]).hexdigest(),
        }
    )
    value["artifacts"].append(
        {
            "evidence_ref": second_content_ref,
            "kind": "daily_publication_content",
            "sha256": hashlib.sha256(artifacts[second_content_ref]).hexdigest(),
        }
    )
    value["series"].append(second_series)

    with pytest.raises(StockDailyGapEvidenceError, match="identical publication content bytes"):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


def test_publication_content_rejects_duplicate_business_dates() -> None:
    """A stock-daily publication has at most one row for each business date."""
    manifest, artifacts, _ = _fixture()
    content_ref = "publication-content:600519:v1"
    content = json.loads(artifacts[content_ref])
    content["rows"][1]["trade_date"] = content["rows"][0]["trade_date"]
    artifacts[content_ref] = _json_bytes(content)
    manifest = _repin_manifest(manifest, artifacts)

    with pytest.raises(StockDailyGapEvidenceError, match="unique trade_date"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_observed_earliest_row_does_not_infer_unknown_lifecycle_start() -> None:
    """All rows in range cannot replace an independently evidenced start date."""
    manifest, artifacts, _ = _fixture(known_start=None)

    bundle = load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)

    coverage = bundle.reconciliation.series[0].coverage
    assert coverage.start_boundary_unknown is True
    assert coverage.known_start_date is None
    assert coverage.status is CoverageStatus.UNKNOWN


def test_artifact_hash_mismatch_fails_before_constructing_report() -> None:
    """Changed external bytes do not pass using a stale manifest hash."""
    manifest, artifacts, _ = _fixture()
    artifacts["calendar:600519:v1"] += b" "

    with pytest.raises(StockDailyGapEvidenceError, match="hash mismatch"):
        load_stock_daily_gap_evidence_bundle(
            manifest,
            artifacts=artifacts,
        )


def test_hash_pinned_calendar_cannot_be_reused_for_a_different_instrument() -> None:
    """The artifact's embedded series identity must match the resolved instrument."""
    manifest, artifacts, _ = _fixture(calendar_instrument="CN:000001")

    with pytest.raises(StockDailyGapEvidenceError, match="identity mismatch"):
        load_stock_daily_gap_evidence_bundle(
            manifest,
            artifacts=artifacts,
        )


def test_lifecycle_artifact_must_match_registry_even_when_its_hash_is_current() -> None:
    """A newly pinned document with a conflicting start is not trusted as registry data."""
    manifest, artifacts, _ = _fixture(status_start=date(2024, 2, 6))
    # Rebuild the manifest hash to prove the rejection is semantic, not stale-pin detection.
    value = json.loads(manifest)
    status_ref = "status:600519:v1"
    value["artifacts"] = [
        {
            **item,
            "sha256": hashlib.sha256(artifacts[status_ref]).hexdigest(),
        }
        if item["evidence_ref"] == status_ref
        else item
        for item in value["artifacts"]
    ]
    manifest = _json_bytes(value)

    with pytest.raises(StockDailyGapEvidenceError, match="differs from registry row"):
        load_stock_daily_gap_evidence_bundle(
            manifest,
            artifacts=artifacts,
        )


def test_expected_dates_must_be_subsets_of_the_exact_calendar_sessions() -> None:
    """Instrument dates outside the pinned calendar remain a hard input error."""
    manifest, artifacts, _ = _fixture(calendar_sessions=SESSIONS[:-1])

    with pytest.raises(StockDailyGapEvidenceError, match="outside the pinned calendar"):
        load_stock_daily_gap_evidence_bundle(
            manifest,
            artifacts=artifacts,
        )


def test_duplicate_json_keys_and_bool_schema_versions_are_rejected() -> None:
    """Parser ambiguity and Python's bool-as-int behavior cannot bypass the schema."""
    manifest, artifacts, _ = _fixture()
    duplicate = b'{"schema_version":1,"schema_version":1}'
    with pytest.raises(StockDailyGapEvidenceError, match="strict UTF-8 JSON"):
        load_stock_daily_gap_evidence_bundle(
            duplicate,
            artifacts=artifacts,
        )

    value = json.loads(manifest)
    value["schema_version"] = True
    with pytest.raises(StockDailyGapEvidenceError, match="integer 1"):
        load_stock_daily_gap_evidence_bundle(
            _json_bytes(value),
            artifacts=artifacts,
        )


def test_manifest_rejects_unreferenced_or_missing_artifact_bytes() -> None:
    """The byte map is exactly the descriptor set, avoiding silent omissions."""
    manifest, artifacts, _ = _fixture()
    artifacts["unused:artifact"] = b"{}"

    with pytest.raises(StockDailyGapEvidenceError, match="exactly match"):
        load_stock_daily_gap_evidence_bundle(
            manifest,
            artifacts=artifacts,
        )


def test_valid_alias_is_resolved_and_its_pinned_document_is_returned() -> None:
    """Aliases resolve through the registry and retain their reviewed evidence ref."""
    manifest, artifacts, _ = _fixture(with_alias=True)

    bundle = load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)

    assert bundle.series_evidence[0].coverage_request.instrument_key == INSTRUMENT
    alias_pin = next(item for item in bundle.artifacts if item.kind == "instrument_alias")
    assert alias_pin.evidence_ref == "alias:600519:v1"


def test_delisted_series_requires_last_expected_observation_evidence() -> None:
    """A delisted row is complete only when its final expected date is evidenced."""
    manifest, artifacts, _ = _fixture(
        status=LifecycleStatus.DELISTED,
        include_retention=True,
        delisted_end=END,
    )

    bundle = load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)

    assert bundle.reconciliation.series[0].status is CoverageStatus.COMPLETE
    assert (
        bundle.series_evidence[0].end_boundary.kind
        is EndBoundaryKind.DELISTED_LAST_EXPECTED_OBSERVATION
    )


def test_delisted_boundary_must_equal_last_expected_session() -> None:
    """A plausible date within the lifecycle still cannot contradict expected dates."""
    manifest, artifacts, _ = _fixture(
        status=LifecycleStatus.DELISTED,
        delisted_end=SESSIONS[-2],
    )

    with pytest.raises(StockDailyGapEvidenceError, match="last evidenced expected date"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_unknown_registry_status_keeps_terminal_boundary_unknown() -> None:
    """An unknown status is not upgraded from the observation tail."""
    manifest, artifacts, _ = _fixture(status=LifecycleStatus.UNKNOWN)

    bundle = load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)

    assert bundle.series_evidence[0].end_boundary.kind is EndBoundaryKind.UNKNOWN
    assert bundle.reconciliation.series[0].status is CoverageStatus.UNKNOWN


def test_source_retention_with_one_boundary_does_not_become_complete() -> None:
    """A one-sided source-retention claim leaves the overall historical boundary unknown."""
    manifest, artifacts, _ = _fixture()
    retention = _document(
        reference="retention:600519:v1",
        dataset_id=DATASET,
        source_id=SOURCE,
        instrument_id=INSTRUMENT,
        payload={"retained_from": START.isoformat(), "retained_through": None},
    )
    artifacts["retention:600519:v1"] = _json_bytes(retention)
    manifest_value = json.loads(manifest)
    manifest_value["series"][0]["retention_ref"] = "retention:600519:v1"
    manifest_value["artifacts"].append(
        {
            "evidence_ref": "retention:600519:v1",
            "kind": "source_retention",
            "sha256": hashlib.sha256(artifacts["retention:600519:v1"]).hexdigest(),
        }
    )

    bundle = load_stock_daily_gap_evidence_bundle(_json_bytes(manifest_value), artifacts=artifacts)

    assert bundle.series_evidence[0].coverage_request.source_retained_from == START
    assert bundle.series_evidence[0].coverage_request.source_retained_through is None
    assert bundle.reconciliation.series[0].status is CoverageStatus.UNKNOWN


def test_changed_registry_snapshot_is_rejected_by_its_existing_codec() -> None:
    """Updating the outer pin cannot bypass the snapshot codec's internal digest."""
    manifest, artifacts, _ = _fixture()
    registry_ref = "registry:stock-daily:v1"
    snapshot = json.loads(artifacts[registry_ref])
    snapshot["registry_version"] = "changed-version"
    artifacts[registry_ref] = _json_bytes(snapshot)
    manifest = _repin_manifest(manifest, artifacts)

    with pytest.raises(StockDailyGapEvidenceError, match="strict codec"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_manifest_registry_ref_must_name_the_registry_artifact() -> None:
    """A valid snapshot elsewhere in the manifest cannot satisfy a wrong registry ref."""
    manifest, artifacts, _ = _fixture()
    value = json.loads(manifest)
    value["registry_ref"] = "calendar:600519:v1"

    with pytest.raises(
        StockDailyGapEvidenceError,
        match="registry_ref must point to the registry snapshot",
    ):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


def test_manifest_cannot_contain_multiple_registry_snapshots() -> None:
    """A second registry pin is rejected even if it repeats the valid snapshot bytes."""
    manifest, artifacts, _ = _fixture()
    value = json.loads(manifest)
    registry_ref = value["registry_ref"]
    duplicate_ref = "registry:zz-second-snapshot:v1"
    duplicate_bytes = artifacts[registry_ref]
    artifacts[duplicate_ref] = duplicate_bytes
    value["artifacts"].append(
        {
            "evidence_ref": duplicate_ref,
            "kind": "instrument_registry",
            "sha256": hashlib.sha256(duplicate_bytes).hexdigest(),
        }
    )

    with pytest.raises(StockDailyGapEvidenceError, match="one registry snapshot"):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


@pytest.mark.parametrize(
    ("series", "message"),
    [([], "nonempty array"), ([None], "must be an object")],
)
def test_manifest_series_must_be_nonempty_objects(series: list[object], message: str) -> None:
    """The series collection is required and each entry must have an object shape."""
    manifest, artifacts, _ = _fixture()
    value = json.loads(manifest)
    value["series"] = series

    with pytest.raises(StockDailyGapEvidenceError, match=message):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


def test_series_request_start_must_not_follow_request_end() -> None:
    """Reversed request bounds fail before resolving identity or reading evidence."""
    manifest, artifacts, _ = _fixture()
    value = json.loads(manifest)
    value["series"][0]["request_start"] = END.isoformat()
    value["series"][0]["request_end"] = START.isoformat()

    with pytest.raises(StockDailyGapEvidenceError, match="request_start follows request_end"):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


def test_series_must_resolve_in_the_pinned_registry() -> None:
    """A caller-supplied instrument identifier cannot create a registry lifecycle."""
    manifest, artifacts, _ = _fixture()
    value = json.loads(manifest)
    value["series"][0]["dataset_id"] = "unregistered.stock.daily"

    with pytest.raises(StockDailyGapEvidenceError, match="does not resolve"):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


def test_alias_must_be_valid_for_the_full_requested_history() -> None:
    """Alias validity cannot be inferred from a resolution date inside the interval."""
    manifest, artifacts, _ = _fixture(with_alias=True)
    value = json.loads(manifest)
    value["series"][0]["request_start"] = date(2024, 2, 4).isoformat()

    with pytest.raises(StockDailyGapEvidenceError, match="alias is not valid across"):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


def test_duplicate_canonical_series_are_rejected_without_publications() -> None:
    """Distinct manifest rows cannot request the same canonical instrument twice."""
    manifest, artifacts, _ = _fixture()
    value = json.loads(manifest)
    first = value["series"][0]
    first["publication_ref"] = None
    first["publication_id"] = None
    value["series"].append(dict(first))
    value["artifacts"] = [
        item
        for item in value["artifacts"]
        if item["kind"] not in {"daily_publication", "daily_publication_content"}
    ]
    artifacts.pop("publication:600519:v1")
    artifacts.pop("publication-content:600519:v1")

    with pytest.raises(StockDailyGapEvidenceError, match="duplicate dataset/source/instrument"):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


@pytest.mark.parametrize(
    ("publication_ref", "publication_id"),
    [(None, "pub-20240209-001"), ("publication:600519:v1", None)],
)
def test_publication_reference_and_id_must_be_paired(
    publication_ref: str | None, publication_id: str | None
) -> None:
    """A publication artifact ref and its business identifier are an atomic pin."""
    manifest, artifacts, _ = _fixture()
    value = json.loads(manifest)
    value["series"][0]["publication_ref"] = publication_ref
    value["series"][0]["publication_id"] = publication_id

    with pytest.raises(StockDailyGapEvidenceError, match="must be present together"):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


def test_lifecycle_ref_must_equal_the_registry_status_reference() -> None:
    """A second valid status envelope cannot replace the registry's pinned ref."""
    manifest, artifacts, _ = _fixture()
    value = json.loads(manifest)
    alternate_ref = "status:alternate-ref:v1"
    status_document = json.loads(artifacts["status:600519:v1"])
    status_document["evidence_ref"] = alternate_ref
    alternate_bytes = _json_bytes(status_document)
    artifacts[alternate_ref] = alternate_bytes
    value["series"][0]["lifecycle_ref"] = alternate_ref
    value["artifacts"].append(
        {
            "evidence_ref": alternate_ref,
            "kind": "instrument_status",
            "sha256": hashlib.sha256(alternate_bytes).hexdigest(),
        }
    )

    with pytest.raises(
        StockDailyGapEvidenceError, match="differs from the registry status evidence"
    ):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


def test_listing_evidence_date_must_match_registry_start() -> None:
    """A correctly pinned listing artifact cannot change the registry boundary."""
    manifest, artifacts, _ = _fixture()
    listing_ref = "listing:600519:v1"
    listing_document = json.loads(artifacts[listing_ref])
    listing_document["payload"]["start_date"] = date(2024, 2, 6).isoformat()
    artifacts[listing_ref] = _json_bytes(listing_document)
    manifest = _repin_manifest(manifest, artifacts)

    with pytest.raises(StockDailyGapEvidenceError, match="listing evidence differs from registry"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_active_lifecycle_rejects_a_delisted_end_reference() -> None:
    """An active as-of boundary cannot also claim a terminal delisted observation."""
    manifest, artifacts, _ = _fixture(delisted_end=END)

    with pytest.raises(StockDailyGapEvidenceError, match="active lifecycle cannot carry"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_delisted_end_boundary_cannot_follow_registry_delisting_date() -> None:
    """A pinned terminal date after the registry's delisting date is rejected."""
    manifest, artifacts, _ = _fixture(
        status=LifecycleStatus.DELISTED,
        delisted_end=date(2024, 2, 10),
    )

    with pytest.raises(StockDailyGapEvidenceError, match="follows the independently recorded"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_unknown_lifecycle_cannot_claim_a_delisted_end_reference() -> None:
    """Unknown lifecycle status never adopts a delisted-only terminal claim."""
    manifest, artifacts, _ = _fixture(
        status=LifecycleStatus.UNKNOWN,
        delisted_end=END,
    )

    with pytest.raises(StockDailyGapEvidenceError, match="valid only for a delisted"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_delisted_lifecycle_without_end_evidence_keeps_terminal_unknown() -> None:
    """A delisting record alone does not establish the last expected observation."""
    manifest, artifacts, _ = _fixture(status=LifecycleStatus.DELISTED)

    bundle = load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)

    assert bundle.series_evidence[0].end_boundary.kind is EndBoundaryKind.UNKNOWN


def test_retention_evidence_with_both_boundaries_unknown_is_rejected() -> None:
    """A retention artifact with no known edge supplies no usable retention boundary."""
    manifest, artifacts, _ = _fixture()
    retention_ref = "retention:600519:v1"
    retention_document = _document(
        reference=retention_ref,
        dataset_id=DATASET,
        source_id=SOURCE,
        instrument_id=INSTRUMENT,
        payload={"retained_from": None, "retained_through": None},
    )
    retention_bytes = _json_bytes(retention_document)
    artifacts[retention_ref] = retention_bytes
    value = json.loads(manifest)
    value["series"][0]["retention_ref"] = retention_ref
    value["artifacts"].append(
        {
            "evidence_ref": retention_ref,
            "kind": "source_retention",
            "sha256": hashlib.sha256(retention_bytes).hexdigest(),
        }
    )

    with pytest.raises(StockDailyGapEvidenceError, match="at least one boundary"):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


def test_unreferenced_but_well_formed_artifact_is_rejected() -> None:
    """Valid pins still must be consumed by the selected series evidence graph."""
    manifest, artifacts, _ = _fixture()
    unused_ref = "retention:unreferenced:v1"
    unused_document = _document(
        reference=unused_ref,
        dataset_id=DATASET,
        source_id=SOURCE,
        instrument_id=INSTRUMENT,
        payload={"retained_from": START.isoformat(), "retained_through": END.isoformat()},
    )
    unused_bytes = _json_bytes(unused_document)
    artifacts[unused_ref] = unused_bytes
    value = json.loads(manifest)
    value["artifacts"].append(
        {
            "evidence_ref": unused_ref,
            "kind": "source_retention",
            "sha256": hashlib.sha256(unused_bytes).hexdigest(),
        }
    )

    with pytest.raises(StockDailyGapEvidenceError, match="unused=1"):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


def test_delisting_evidence_date_must_match_registry() -> None:
    """The registry's delisting boundary is checked against its pinned source document."""
    manifest, artifacts, _ = _fixture(status=LifecycleStatus.DELISTED)
    delisting_ref = "delisting:600519:v1"
    delisting_document = json.loads(artifacts[delisting_ref])
    delisting_document["payload"]["delisted_on"] = date(2024, 2, 8).isoformat()
    artifacts[delisting_ref] = _json_bytes(delisting_document)
    manifest = _repin_manifest(manifest, artifacts)

    with pytest.raises(
        StockDailyGapEvidenceError, match="delisting evidence differs from registry"
    ):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


@pytest.mark.parametrize(
    ("descriptor_change", "message"),
    [
        (lambda row: row.update(kind="not-a-kind"), "kind is unsupported"),
        (lambda row: row.update(sha256="A" * 64), "lowercase SHA-256"),
        (lambda row: row.update(extra="unknown"), "missing or unsupported fields"),
    ],
)
def test_artifact_descriptor_schema_is_exact(
    descriptor_change: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    """Unknown descriptor shapes, kinds and hash formats are rejected."""
    manifest, artifacts, _ = _fixture()
    value = json.loads(manifest)
    descriptor_change(value["artifacts"][0])

    with pytest.raises(StockDailyGapEvidenceError, match=message):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


def test_descriptor_rejects_duplicate_refs_and_missing_registry_snapshot() -> None:
    """Every evidence reference has one descriptor and the registry ref must resolve."""
    manifest, artifacts, _ = _fixture()
    value = json.loads(manifest)
    value["artifacts"].append(value["artifacts"][0])
    with pytest.raises(StockDailyGapEvidenceError, match="duplicate evidence refs"):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)

    manifest, artifacts, _ = _fixture()
    value = json.loads(manifest)
    value["artifacts"] = [
        item for item in value["artifacts"] if item["kind"] != "instrument_registry"
    ]
    registry_ref = value["registry_ref"]
    artifacts.pop(registry_ref)
    with pytest.raises(
        StockDailyGapEvidenceError,
        match="registry_ref must point to the registry snapshot",
    ):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


def test_artifact_document_rejects_unknown_keys_and_wrong_embedded_ref() -> None:
    """Hash-pinned bytes must still conform to the declared evidence schema."""
    manifest, artifacts, _ = _fixture()
    calendar_ref = "calendar:600519:v1"
    value = json.loads(artifacts[calendar_ref])
    value["extra"] = "ignored data is not accepted"
    artifacts[calendar_ref] = _json_bytes(value)
    manifest = _repin_manifest(manifest, artifacts)
    with pytest.raises(StockDailyGapEvidenceError, match="missing or unsupported fields"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)

    manifest, artifacts, _ = _fixture()
    value = json.loads(artifacts[calendar_ref])
    value["evidence_ref"] = "calendar:another-security"
    artifacts[calendar_ref] = _json_bytes(value)
    manifest = _repin_manifest(manifest, artifacts)
    with pytest.raises(StockDailyGapEvidenceError, match="artifact ref mismatch"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_wrong_artifact_kind_fails_even_when_document_hash_is_valid() -> None:
    """A valid file cannot be substituted into a different evidence role."""
    manifest, artifacts, _ = _fixture()
    value = json.loads(manifest)
    value["artifacts"] = [
        {**item, "kind": "instrument_expectation"}
        if item["evidence_ref"] == "calendar:600519:v1"
        else item
        for item in value["artifacts"]
    ]

    with pytest.raises(StockDailyGapEvidenceError, match="evidence kind mismatch"):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


def test_wrong_publication_identity_and_id_are_rejected() -> None:
    """A publication ID mismatch cannot be accepted under a manifest pin."""
    manifest, artifacts, _ = _fixture()
    publication_ref = "publication:600519:v1"
    value = json.loads(artifacts[publication_ref])
    value["publication_id"] = "pub-from-other-instrument"
    artifacts[publication_ref] = _json_bytes(value)
    manifest = _repin_manifest(manifest, artifacts)

    with pytest.raises(StockDailyGapEvidenceError, match="publication binding mismatch"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_missing_publication_is_reported_as_unknown() -> None:
    """A calendar without pinned publication rows cannot certify observed coverage."""
    manifest, artifacts, _ = _fixture()
    value = json.loads(manifest)
    value["series"][0]["publication_ref"] = None
    value["series"][0]["publication_id"] = None
    value["artifacts"] = [
        item
        for item in value["artifacts"]
        if item["kind"] not in {"daily_publication", "daily_publication_content"}
    ]
    publication_ref = "publication:600519:v1"
    artifacts.pop(publication_ref)
    artifacts.pop("publication-content:600519:v1")

    bundle = load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)

    assert bundle.series_evidence[0].publication is None
    assert bundle.reconciliation.series[0].status is CoverageStatus.UNKNOWN
