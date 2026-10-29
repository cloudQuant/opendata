"""Isolated behavioral tests for revalidated temporal identity candidate import."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import Engine, create_engine, text

import opendata.pipeline.temporal_identity_import as import_module
from opendata.data.temporal_identity_staging import (
    TemporalIdentityStagingResult,
    stage_temporal_identity_pin,
)
from opendata.data.temporal_identity_store import (
    TemporalIdentitySnapshotReceipt,
    TemporalIdentitySnapshotStore,
    TemporalIdentitySnapshotStoreError,
    TemporalIdentitySnapshotValidationError,
)
from opendata.data.temporal_instrument_identity import (
    TemporalIdentityAssignment,
    TemporalIdentityRegistry,
)
from opendata.data.temporal_instrument_identity_snapshot import (
    encode_temporal_identity_snapshot,
)
from opendata.pipeline.temporal_identity_import import (
    TemporalIdentityImportConflictError,
    TemporalIdentityImportReadbackError,
    TemporalIdentityImportValidationError,
    TemporalIdentityImportWriteOutcomeUnknownError,
    import_temporal_identity_candidate,
)

_CREATE_HEADER = """
CREATE TABLE od_temporal_identity_snapshots (
    authority_id TEXT NOT NULL,
    registry_id TEXT NOT NULL,
    reference_version TEXT NOT NULL,
    total_bytes INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    snapshot_sha256 BLOB NOT NULL,
    created_at_us INTEGER NOT NULL,
    PRIMARY KEY (authority_id, registry_id, reference_version)
)
"""
_CREATE_CHUNKS = """
CREATE TABLE od_temporal_identity_snapshot_chunks (
    authority_id TEXT NOT NULL,
    registry_id TEXT NOT NULL,
    reference_version TEXT NOT NULL,
    chunk_no INTEGER NOT NULL,
    chunk_bytes BLOB NOT NULL,
    chunk_sha256 BLOB NOT NULL,
    PRIMARY KEY (authority_id, registry_id, reference_version, chunk_no),
    FOREIGN KEY (authority_id, registry_id, reference_version)
      REFERENCES od_temporal_identity_snapshots
        (authority_id, registry_id, reference_version)
)
"""


@pytest.fixture
def engine() -> Engine:
    """Create only the two temporary tables required by the explicit store."""
    database = create_engine("sqlite:///:memory:")
    with database.begin() as connection:
        connection.exec_driver_sql(_CREATE_HEADER)
        connection.exec_driver_sql(_CREATE_CHUNKS)
    yield database
    database.dispose()


def _snapshot(*, evidence_ref: str = "caller-evidence") -> bytes:
    """Encode one small, synthetic source assignment with a fixed business key."""
    registry = TemporalIdentityRegistry(
        reference_version="registry-v1",
        evidence_ref=evidence_ref,
        assignments=(
            TemporalIdentityAssignment(
                assignment_id="assignment-1",
                instrument_id="instrument-1",
                exchange="XSHG",
                symbol="600000",
                valid_from=date(2024, 1, 2),
                valid_to=None,
                source_id="akshare",
                source_version="caller-version",
                evidence_ref=evidence_ref,
                source_sha256="a" * 64,
            ),
        ),
    )
    return encode_temporal_identity_snapshot(registry)


def _candidate(*, evidence_ref: str = "caller-evidence") -> TemporalIdentityStagingResult:
    """Stage an explicit unverified candidate without reading any external source."""
    return stage_temporal_identity_pin(
        _snapshot(evidence_ref=evidence_ref),
        None,
        dataset_id="stock_daily",
        source_id="akshare",
        authority_id="caller-authority",
        registry_id="caller-registry",
        reference_version="registry-v1",
    )


def _row_counts(engine: Engine) -> tuple[int, int]:
    """Count rows only in this fixture's in-memory SQLite tables."""
    with engine.connect() as connection:
        return (
            connection.execute(
                text("SELECT count(*) FROM od_temporal_identity_snapshots")
            ).scalar_one(),
            connection.execute(
                text("SELECT count(*) FROM od_temporal_identity_snapshot_chunks")
            ).scalar_one(),
        )


def _replace_candidate_pin(
    candidate: TemporalIdentityStagingResult,
    **updates: str,
) -> TemporalIdentityStagingResult:
    """Create a self-consistent pin document around one altered candidate pin."""
    assert candidate.candidate_pin is not None
    pin = candidate.candidate_pin.model_copy(update=updates)
    raw = json.dumps(
        {"schema_version": 1, "pins": [pin.model_dump(mode="json")]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return replace(
        candidate,
        candidate_pin=pin,
        pin_document_bytes=raw,
        pin_file_sha256=hashlib.sha256(raw).hexdigest(),
    )


def test_import_revalidates_puts_and_exactly_reads_back_without_publishing_pin(
    engine: Engine,
) -> None:
    """A successful import persists only snapshot chunks and returns metadata."""
    candidate = _candidate()
    store = TemporalIdentitySnapshotStore(engine)

    receipt = import_temporal_identity_candidate(candidate, store=store)

    assert receipt.readback_verified is True
    assert receipt.pin_published is False
    assert receipt.pin_file_sha256 == candidate.pin_file_sha256
    assert receipt.pin_count == 1
    assert receipt.snapshot_receipt.snapshot_sha256 == candidate.snapshot_sha256
    assert (
        store.get_bytes(
            authority_id="caller-authority",
            registry_id="caller-registry",
            reference_version="registry-v1",
            expected_sha256=candidate.snapshot_sha256,
        )
        == candidate.snapshot_bytes
    )
    assert _row_counts(engine) == (1, 1)


def test_forged_snapshot_digest_is_rejected_before_store_calls(engine: Engine) -> None:
    """A hand-built result cannot substitute its own digest for complete bytes."""
    candidate = replace(_candidate(), snapshot_sha256="0" * 64)

    with pytest.raises(TemporalIdentityImportValidationError) as exc:
        import_temporal_identity_candidate(candidate, store=TemporalIdentitySnapshotStore(engine))

    assert exc.value.code == "snapshot_digest_mismatch"
    assert exc.value.write_may_have_committed is False
    assert _row_counts(engine) == (0, 0)


def test_non_result_candidate_is_rejected_before_store_calls(engine: Engine) -> None:
    """The import entry point accepts only the staging result value type."""
    with pytest.raises(TemporalIdentityImportValidationError) as exc:
        import_temporal_identity_candidate(
            object(),  # type: ignore[arg-type]
            store=TemporalIdentitySnapshotStore(engine),
        )

    assert exc.value.code == "staging_result_invalid"
    assert _row_counts(engine) == (0, 0)


def test_import_rejects_a_non_store_injection(engine: Engine) -> None:
    """The importer requires an explicitly constructed synchronous snapshot store."""
    with pytest.raises(TemporalIdentityImportValidationError) as exc:
        import_temporal_identity_candidate(
            _candidate(),
            store=object(),  # type: ignore[arg-type]
        )

    assert exc.value.code == "injected_store_invalid"
    assert _row_counts(engine) == (0, 0)


@pytest.mark.parametrize(
    ("constant_name", "field_name", "expected_code"),
    [
        (
            "MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES",
            "snapshot_bytes",
            "snapshot_size_invalid",
        ),
        (
            "MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES",
            "pin_document_bytes",
            "pin_document_size_invalid",
        ),
    ],
)
def test_complete_input_byte_limits_fail_before_store_write(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    constant_name: str,
    field_name: str,
    expected_code: str,
) -> None:
    """Both full documents are bounded before parsing or database access."""
    staged = _candidate()
    raw = getattr(staged, field_name)
    monkeypatch.setattr(import_module, constant_name, len(raw) - 1)

    with pytest.raises(TemporalIdentityImportValidationError) as exc:
        import_temporal_identity_candidate(staged, store=TemporalIdentitySnapshotStore(engine))

    assert exc.value.code == expected_code
    assert _row_counts(engine) == (0, 0)


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("snapshot_bytes", bytearray(b"mutable"), "snapshot_bytes_invalid"),
        ("snapshot_bytes", b"", "snapshot_size_invalid"),
        ("pin_document_bytes", bytearray(b"mutable"), "pin_document_bytes_invalid"),
        ("pin_document_bytes", b"", "pin_document_size_invalid"),
        ("pin_file_sha256", object(), "pin_document_digest_mismatch"),
        ("candidate_pin", None, "candidate_pin_invalid"),
        ("assignment_count", 2, "assignment_count_mismatch"),
        ("pin_count", 2, "pin_count_mismatch"),
        ("existing_pin_count", 2, "existing_pin_count_invalid"),
        ("verification_status", "authenticated", "verification_status_invalid"),
    ],
)
def test_forged_stage_fields_fail_before_any_store_write(
    engine: Engine,
    field: str,
    value: object,
    expected_code: str,
) -> None:
    """Untrusted result metadata is recomputed or structurally rejected."""
    forged = replace(_candidate(), **{field: value})

    with pytest.raises(TemporalIdentityImportValidationError) as exc:
        import_temporal_identity_candidate(forged, store=TemporalIdentitySnapshotStore(engine))

    assert exc.value.code == expected_code
    assert _row_counts(engine) == (0, 0)


def test_pin_file_digest_is_recomputed_over_the_complete_document(engine: Engine) -> None:
    """A candidate cannot supply a different expected digest for its pin bytes."""
    forged = replace(_candidate(), pin_file_sha256="0" * 64)

    with pytest.raises(TemporalIdentityImportValidationError) as exc:
        import_temporal_identity_candidate(forged, store=TemporalIdentitySnapshotStore(engine))

    assert exc.value.code == "pin_document_digest_mismatch"
    assert _row_counts(engine) == (0, 0)


def test_malformed_pin_document_is_rejected_after_full_digest_check(engine: Engine) -> None:
    """Rehashing malformed caller bytes does not bypass strict pin parsing."""
    malformed = b"{not-json"
    forged = replace(
        _candidate(),
        pin_document_bytes=malformed,
        pin_file_sha256=hashlib.sha256(malformed).hexdigest(),
    )

    with pytest.raises(TemporalIdentityImportValidationError) as exc:
        import_temporal_identity_candidate(forged, store=TemporalIdentitySnapshotStore(engine))

    assert exc.value.code == "pin_document_invalid"
    assert _row_counts(engine) == (0, 0)


def test_pin_reference_version_must_match_snapshot(engine: Engine) -> None:
    """A canonical pin document cannot relabel the registry's version."""
    forged = _replace_candidate_pin(_candidate(), reference_version="registry-v2")

    with pytest.raises(TemporalIdentityImportValidationError) as exc:
        import_temporal_identity_candidate(forged, store=TemporalIdentitySnapshotStore(engine))

    assert exc.value.code == "reference_version_mismatch"
    assert _row_counts(engine) == (0, 0)


def test_pin_digest_must_bind_the_complete_snapshot(engine: Engine) -> None:
    """A valid-looking pin digest is compared with the complete codec bytes."""
    forged = _replace_candidate_pin(_candidate(), snapshot_sha256="b" * 64)

    with pytest.raises(TemporalIdentityImportValidationError) as exc:
        import_temporal_identity_candidate(forged, store=TemporalIdentitySnapshotStore(engine))

    assert exc.value.code == "candidate_snapshot_binding_mismatch"
    assert _row_counts(engine) == (0, 0)


def test_empty_registry_cannot_claim_a_candidate_source(engine: Engine) -> None:
    """An empty valid codec carries no assignment with which to check the pin source."""
    staged = _candidate()
    empty_registry = TemporalIdentityRegistry(
        reference_version="registry-v1",
        evidence_ref="caller-declared-empty",
        assignments=(),
    )
    raw = encode_temporal_identity_snapshot(empty_registry)
    digest = hashlib.sha256(raw).hexdigest()
    empty_pin = _replace_candidate_pin(staged, snapshot_sha256=digest)
    forged = replace(
        empty_pin,
        snapshot_bytes=raw,
        snapshot_sha256=digest,
        assignment_count=0,
    )

    with pytest.raises(TemporalIdentityImportValidationError) as exc:
        import_temporal_identity_candidate(forged, store=TemporalIdentitySnapshotStore(engine))

    assert exc.value.code == "snapshot_source_unverifiable"
    assert _row_counts(engine) == (0, 0)


def test_candidate_pin_must_bind_the_exact_snapshot_and_pin_document(engine: Engine) -> None:
    """Changing the pin object while keeping its document is rejected before put."""
    staged = _candidate()
    assert staged.candidate_pin is not None
    altered_pin = staged.candidate_pin.model_copy(update={"source_id": "other-source"})
    candidate = replace(staged, candidate_pin=altered_pin)

    with pytest.raises(TemporalIdentityImportValidationError) as exc:
        import_temporal_identity_candidate(candidate, store=TemporalIdentitySnapshotStore(engine))

    assert exc.value.code == "source_id_mismatch"
    assert _row_counts(engine) == (0, 0)


def test_pin_document_must_be_canonical_even_when_its_new_digest_matches(engine: Engine) -> None:
    """The importer reparses and canonicalizes all supplied pin bytes."""
    staged = _candidate()
    noncanonical = staged.pin_document_bytes + b" "
    candidate = replace(
        staged,
        pin_document_bytes=noncanonical,
        pin_file_sha256=hashlib.sha256(noncanonical).hexdigest(),
    )

    with pytest.raises(TemporalIdentityImportValidationError) as exc:
        import_temporal_identity_candidate(candidate, store=TemporalIdentitySnapshotStore(engine))

    assert exc.value.code == "pin_document_not_canonical"
    assert _row_counts(engine) == (0, 0)


def test_noncanonical_snapshot_bytes_are_rejected_even_with_recomputed_digest(
    engine: Engine,
) -> None:
    """Rehashing trailing JSON whitespace does not make snapshot bytes canonical."""
    staged = _candidate()
    noncanonical = staged.snapshot_bytes + b" "
    candidate = replace(
        staged,
        snapshot_bytes=noncanonical,
        snapshot_sha256=hashlib.sha256(noncanonical).hexdigest(),
    )

    with pytest.raises(TemporalIdentityImportValidationError):
        import_temporal_identity_candidate(candidate, store=TemporalIdentitySnapshotStore(engine))

    assert _row_counts(engine) == (0, 0)


def test_validation_rejects_a_pin_document_without_the_exact_candidate(engine: Engine) -> None:
    """A valid document for another dataset cannot authorize this candidate."""
    staged = _candidate()
    assert staged.candidate_pin is not None
    other_pin = staged.candidate_pin.model_copy(update={"dataset_id": "other_dataset"})
    raw = json.dumps(
        {"schema_version": 1, "pins": [other_pin.model_dump(mode="json")]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    candidate = replace(
        staged,
        pin_document_bytes=raw,
        pin_file_sha256=hashlib.sha256(raw).hexdigest(),
    )

    with pytest.raises(TemporalIdentityImportValidationError) as exc:
        import_temporal_identity_candidate(candidate, store=TemporalIdentitySnapshotStore(engine))

    assert exc.value.code == "candidate_pin_binding_mismatch"
    assert _row_counts(engine) == (0, 0)


def test_post_put_readback_failure_reports_possible_commit_and_exact_retry_is_idempotent(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed separate read transaction can follow commit; same bytes safely replay."""
    candidate = _candidate()
    store = TemporalIdentitySnapshotStore(engine)

    def unavailable_readback(**_kwargs: object) -> bytes:
        raise RuntimeError("synthetic readback interruption")

    monkeypatch.setattr(store, "get_bytes", unavailable_readback)
    with pytest.raises(TemporalIdentityImportReadbackError) as exc:
        import_temporal_identity_candidate(candidate, store=store)

    assert exc.value.code == "snapshot_readback_failed"
    assert exc.value.write_may_have_committed is True
    assert exc.value.retry_same_candidate is True
    assert _row_counts(engine) == (1, 1)
    monkeypatch.undo()

    retry = import_temporal_identity_candidate(candidate, store=store)

    assert retry.readback_verified is True
    assert retry.snapshot_receipt.already_existed is True
    assert _row_counts(engine) == (1, 1)


def test_exact_bytes_mismatch_after_put_is_reported_as_unverified_commit(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store response with valid type but different bytes cannot pass readback."""
    candidate = _candidate()
    store = TemporalIdentitySnapshotStore(engine)
    monkeypatch.setattr(store, "get_bytes", lambda **_kwargs: b"different bytes")

    with pytest.raises(TemporalIdentityImportReadbackError) as exc:
        import_temporal_identity_candidate(candidate, store=store)

    assert exc.value.code == "snapshot_readback_mismatch"
    assert _row_counts(engine) == (1, 1)


def test_non_bytes_readback_is_rejected_after_put(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutable lookalike values cannot stand in for immutable snapshot bytes."""
    candidate = _candidate()
    store = TemporalIdentitySnapshotStore(engine)
    monkeypatch.setattr(store, "get_bytes", lambda **_kwargs: bytearray(candidate.snapshot_bytes))

    with pytest.raises(TemporalIdentityImportReadbackError) as exc:
        import_temporal_identity_candidate(candidate, store=store)

    assert exc.value.code == "snapshot_readback_mismatch"
    assert _row_counts(engine) == (1, 1)


def test_receipt_metadata_must_match_after_public_bytes_readback(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inconsistent put receipt is rejected even when exact bytes read back."""
    candidate = _candidate()
    store = TemporalIdentitySnapshotStore(engine)
    original_put = store.put

    def inconsistent_receipt(**kwargs: object) -> TemporalIdentitySnapshotReceipt:
        receipt = original_put(**kwargs)  # type: ignore[arg-type]
        return replace(receipt, total_bytes=receipt.total_bytes + 1)

    monkeypatch.setattr(store, "put", inconsistent_receipt)

    with pytest.raises(TemporalIdentityImportReadbackError) as exc:
        import_temporal_identity_candidate(candidate, store=store)

    assert exc.value.code == "store_receipt_mismatch"
    assert _row_counts(engine) == (1, 1)


def test_receipt_must_be_the_store_receipt_type_after_readback(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown put return value cannot be mistaken for a verified receipt."""
    candidate = _candidate()
    store = TemporalIdentitySnapshotStore(engine)
    original_put = store.put

    def opaque_receipt(**kwargs: object) -> object:
        original_put(**kwargs)  # type: ignore[arg-type]
        return object()

    monkeypatch.setattr(store, "put", opaque_receipt)

    with pytest.raises(TemporalIdentityImportReadbackError) as exc:
        import_temporal_identity_candidate(candidate, store=store)

    assert exc.value.code == "store_receipt_mismatch"
    assert _row_counts(engine) == (1, 1)


def test_put_outcome_failure_is_conservatively_retryable(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store exception may be ambiguous and is surfaced without leaking details."""
    candidate = _candidate()
    store = TemporalIdentitySnapshotStore(engine)

    def interrupted_put(**_kwargs: object) -> TemporalIdentitySnapshotReceipt:
        raise RuntimeError("synthetic transport detail must not escape")

    monkeypatch.setattr(store, "put", interrupted_put)
    with pytest.raises(TemporalIdentityImportWriteOutcomeUnknownError) as exc:
        import_temporal_identity_candidate(candidate, store=store)

    assert exc.value.code == "store_write_outcome_unknown"
    assert exc.value.write_may_have_committed is True
    assert exc.value.retry_same_candidate is True
    assert "synthetic transport detail" not in str(exc.value)


def test_store_failure_without_specific_result_is_outcome_unknown(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic storage errors preserve the possibility of a committed write."""
    candidate = _candidate()
    store = TemporalIdentitySnapshotStore(engine)

    def unavailable_put(**_kwargs: object) -> TemporalIdentitySnapshotReceipt:
        raise TemporalIdentitySnapshotStoreError("synthetic database details")

    monkeypatch.setattr(store, "put", unavailable_put)
    with pytest.raises(TemporalIdentityImportWriteOutcomeUnknownError) as exc:
        import_temporal_identity_candidate(candidate, store=store)

    assert exc.value.code == "store_write_outcome_unknown"
    assert _row_counts(engine) == (0, 0)


def test_conflicting_immutable_version_is_not_overwritten(engine: Engine) -> None:
    """A changed snapshot under the same key produces an explicit conflict."""
    store = TemporalIdentitySnapshotStore(engine)
    first = _candidate(evidence_ref="first caller evidence")
    second = _candidate(evidence_ref="different caller evidence")
    import_temporal_identity_candidate(first, store=store)

    with pytest.raises(TemporalIdentityImportConflictError) as exc:
        import_temporal_identity_candidate(second, store=store)

    assert exc.value.code == "immutable_version_conflict"
    assert _row_counts(engine) == (1, 1)


def test_store_validation_failure_is_reported_before_an_unknown_write_state(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known pre-write store rejection remains a definite validation failure."""
    candidate = _candidate()
    store = TemporalIdentitySnapshotStore(engine)

    def reject_put(**_kwargs: object) -> TemporalIdentitySnapshotReceipt:
        raise TemporalIdentitySnapshotValidationError("synthetic details")

    monkeypatch.setattr(store, "put", reject_put)
    with pytest.raises(TemporalIdentityImportValidationError) as exc:
        import_temporal_identity_candidate(candidate, store=store)

    assert exc.value.code == "store_rejected_candidate"
    assert _row_counts(engine) == (0, 0)
