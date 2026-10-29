"""Synthetic SQLite tests for the immutable temporal identity snapshot store.

These tests create only their own temporary in-memory schema. They contain no
production database, credentials, provider calls, or full-market assertions.
MySQL/InnoDB remains unvalidated until a separately approved disposable MySQL
instance is available.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from threading import Barrier
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import IntegrityError

import opendata.data.temporal_identity_store as store_module
from opendata.data.temporal_identity_store import (
    MAX_SNAPSHOT_CHUNK_BYTES,
    TemporalIdentitySnapshotConflictError,
    TemporalIdentitySnapshotCorruptError,
    TemporalIdentitySnapshotDigestMismatchError,
    TemporalIdentitySnapshotNotFoundError,
    TemporalIdentitySnapshotStore,
    TemporalIdentitySnapshotStoreError,
    TemporalIdentitySnapshotValidationError,
)
from opendata.data.temporal_instrument_identity import (
    TemporalIdentityAssignment,
    TemporalIdentityRegistry,
)
from opendata.data.temporal_instrument_identity_snapshot import (
    TemporalIdentitySnapshotError,
    encode_temporal_identity_snapshot,
)

if TYPE_CHECKING:
    from pathlib import Path

_CREATE_HEADER = """
CREATE TABLE od_temporal_identity_snapshots (
    authority_id VARCHAR(128) COLLATE BINARY NOT NULL,
    registry_id VARCHAR(128) COLLATE BINARY NOT NULL,
    reference_version VARCHAR(128) COLLATE BINARY NOT NULL,
    total_bytes BIGINT NOT NULL CHECK (total_bytes BETWEEN 1 AND 67108864),
    chunk_count SMALLINT NOT NULL CHECK (chunk_count BETWEEN 1 AND 256),
    snapshot_sha256 BLOB NOT NULL
      CHECK (typeof(snapshot_sha256) = 'blob' AND length(snapshot_sha256) = 32),
    created_at_us BIGINT NOT NULL CHECK (created_at_us >= 0),
    CHECK (length(authority_id) BETWEEN 1 AND 128
      AND length(CAST(authority_id AS BLOB)) = length(authority_id)),
    CHECK (length(registry_id) BETWEEN 1 AND 128
      AND length(CAST(registry_id AS BLOB)) = length(registry_id)),
    CHECK (length(reference_version) BETWEEN 1 AND 128
      AND length(CAST(reference_version AS BLOB)) = length(reference_version)),
    PRIMARY KEY (authority_id, registry_id, reference_version)
)
"""
_CREATE_CHUNKS = """
CREATE TABLE od_temporal_identity_snapshot_chunks (
    authority_id VARCHAR(128) COLLATE BINARY NOT NULL,
    registry_id VARCHAR(128) COLLATE BINARY NOT NULL,
    reference_version VARCHAR(128) COLLATE BINARY NOT NULL,
    chunk_no SMALLINT NOT NULL CHECK (chunk_no BETWEEN 0 AND 255),
    chunk_bytes BLOB NOT NULL
      CHECK (typeof(chunk_bytes) = 'blob' AND length(chunk_bytes) BETWEEN 1 AND 262144),
    chunk_sha256 BLOB NOT NULL
      CHECK (typeof(chunk_sha256) = 'blob' AND length(chunk_sha256) = 32),
    CHECK (length(authority_id) BETWEEN 1 AND 128
      AND length(CAST(authority_id AS BLOB)) = length(authority_id)),
    CHECK (length(registry_id) BETWEEN 1 AND 128
      AND length(CAST(registry_id AS BLOB)) = length(registry_id)),
    CHECK (length(reference_version) BETWEEN 1 AND 128
      AND length(CAST(reference_version AS BLOB)) = length(reference_version)),
    PRIMARY KEY (authority_id, registry_id, reference_version, chunk_no),
    FOREIGN KEY (authority_id, registry_id, reference_version)
      REFERENCES od_temporal_identity_snapshots
        (authority_id, registry_id, reference_version)
      ON DELETE RESTRICT
)
"""


@pytest.fixture
def engine() -> Engine:
    """Provide an owned in-memory SQLite engine with only the frozen test DDL."""
    database = create_engine("sqlite:///:memory:")

    @event.listens_for(database, "connect")
    def _enable_foreign_keys(connection: object, _record: object) -> None:
        cursor = connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    with database.begin() as connection:
        connection.exec_driver_sql(_CREATE_HEADER)
        connection.exec_driver_sql(_CREATE_CHUNKS)
    yield database
    database.dispose()


def _registry(
    *, reference_version: str = "registry-v1", evidence_ref: str = "registry-evidence"
) -> TemporalIdentityRegistry:
    """Return a small explicit registry with no source-completeness claims."""
    return TemporalIdentityRegistry(
        reference_version=reference_version,
        evidence_ref=evidence_ref,
        assignments=(),
    )


def _large_registry(*, reference_version: str = "registry-large-v1") -> TemporalIdentityRegistry:
    """Return enough distinct synthetic assignments to cross chunk boundaries."""
    evidence = "e" * 500
    assignments = tuple(
        TemporalIdentityAssignment(
            assignment_id=f"assignment-{number:04d}",
            instrument_id=f"instrument-{number:04d}",
            exchange="XSHG",
            symbol=f"{number:06d}",
            valid_from=date(2020, 1, 1),
            valid_to=None,
            source_id="synthetic-source",
            source_version="fixture-v1",
            evidence_ref=evidence,
        )
        for number in range(1, 1_500)
    )
    return TemporalIdentityRegistry(
        reference_version=reference_version,
        evidence_ref=evidence,
        assignments=assignments,
    )


def _digest(registry: TemporalIdentityRegistry) -> str:
    """Compute the caller-side pin over every canonical codec byte."""
    return hashlib.sha256(encode_temporal_identity_snapshot(registry)).hexdigest()


def _stored_chunk(engine: Engine, chunk_no: int = 0) -> bytes:
    """Read one test-owned chunk for a targeted corruption scenario."""
    with engine.connect() as connection:
        value = connection.execute(
            text(
                "SELECT chunk_bytes FROM od_temporal_identity_snapshot_chunks "
                "WHERE authority_id='source-a' AND registry_id='registry-a' "
                "AND reference_version='registry-v1' AND chunk_no=:chunk_no"
            ),
            {"chunk_no": chunk_no},
        ).scalar_one()
    return value


def _insert_one(engine: Engine, registry: TemporalIdentityRegistry) -> str:
    """Store one small registry using the exact digest its caller independently pins."""
    return (
        TemporalIdentitySnapshotStore(engine)
        .put(
            authority_id="source-a",
            registry_id="registry-a",
            registry=registry,
            expected_sha256=_digest(registry),
        )
        .snapshot_sha256
    )


def test_store_round_trips_only_after_explicit_schema_migration(engine: Engine) -> None:
    """Persist header and chunk bytes atomically, then return a validated registry."""
    registry = _registry()
    encoded = encode_temporal_identity_snapshot(registry)
    digest = hashlib.sha256(encoded).hexdigest()
    store = TemporalIdentitySnapshotStore(engine)

    receipt = store.put(
        authority_id="source-a",
        registry_id="registry-a",
        registry=registry,
        expected_sha256=digest,
    )

    assert receipt.authority_id == "source-a"
    assert receipt.registry_id == "registry-a"
    assert receipt.reference_version == "registry-v1"
    assert receipt.snapshot_sha256 == digest
    assert receipt.total_bytes == len(encoded)
    assert receipt.chunk_count == 1
    assert receipt.created_at_us > 0
    assert receipt.already_existed is False
    assert (
        store.get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
            expected_sha256=digest,
        )
        == registry
    )
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM od_temporal_identity_snapshots")
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM od_temporal_identity_snapshot_chunks")
            ).scalar_one()
            == 1
        )


def test_store_same_version_identical_bytes_are_idempotent(engine: Engine) -> None:
    """An exact replay leaves the first timestamp and bytes unchanged."""
    registry = _registry()
    store = TemporalIdentitySnapshotStore(engine)
    expected = _digest(registry)
    first = store.put(
        authority_id="source-a",
        registry_id="registry-a",
        registry=registry,
        expected_sha256=expected,
    )
    second = store.put(
        authority_id="source-a",
        registry_id="registry-a",
        registry=registry,
        expected_sha256=expected,
    )

    assert second.already_existed is True
    assert second.created_at_us == first.created_at_us
    assert second.snapshot_sha256 == first.snapshot_sha256


def test_store_same_version_different_bytes_conflict_without_overwrite(engine: Engine) -> None:
    """A content change cannot replace an immutable version key."""
    store = TemporalIdentitySnapshotStore(engine)
    original = _registry()
    changed = _registry(evidence_ref="different-evidence")
    original_digest = _digest(original)
    store.put(
        authority_id="source-a",
        registry_id="registry-a",
        registry=original,
        expected_sha256=original_digest,
    )

    with pytest.raises(TemporalIdentitySnapshotConflictError):
        store.put(
            authority_id="source-a",
            registry_id="registry-a",
            registry=changed,
            expected_sha256=_digest(changed),
        )

    assert (
        store.get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
            expected_sha256=original_digest,
        )
        == original
    )


def test_store_external_digest_must_match_complete_codec_bytes(engine: Engine) -> None:
    """The codec's embedded content hash is not accepted as the external pin."""
    with pytest.raises(TemporalIdentitySnapshotValidationError, match="expected digest"):
        TemporalIdentitySnapshotStore(engine).put(
            authority_id="source-a",
            registry_id="registry-a",
            registry=_registry(),
            expected_sha256="0" * 64,
        )
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM od_temporal_identity_snapshots")
            ).scalar_one()
            == 0
        )


def test_store_rejects_invalid_ascii_keys_and_digest_before_database_access(engine: Engine) -> None:
    """Keys retain exact ASCII spelling and digest syntax is fixed lowercase hex."""
    store = TemporalIdentitySnapshotStore(engine)
    registry = _registry()
    expected = _digest(registry)
    for invalid in ("", "雪", "a" * 129, " leading"):
        with pytest.raises(TemporalIdentitySnapshotValidationError):
            store.put(
                authority_id=invalid,
                registry_id="registry-a",
                registry=registry,
                expected_sha256=expected,
            )
    with pytest.raises(TemporalIdentitySnapshotValidationError, match="64 lowercase"):
        store.get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
            expected_sha256="A" * 64,
        )
    with pytest.raises(TemporalIdentitySnapshotValidationError, match="64 lowercase"):
        store.get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
            expected_sha256=123,  # type: ignore[arg-type]
        )


def test_store_requires_external_digest_on_every_read(engine: Engine) -> None:
    """There is no read path that trusts only the database's own header hash."""
    store = TemporalIdentitySnapshotStore(engine)
    with pytest.raises(TypeError):
        store.get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
        )  # type: ignore[call-arg]


def test_store_read_rejects_wrong_external_digest(engine: Engine) -> None:
    """A syntactically valid but incorrect external pin never returns a registry."""
    registry = _registry()
    _insert_one(engine, registry)
    with pytest.raises(TemporalIdentitySnapshotDigestMismatchError):
        TemporalIdentitySnapshotStore(engine).get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
            expected_sha256="0" * 64,
        )


def test_store_missing_version_is_not_implicitly_resolved(engine: Engine) -> None:
    """A caller must ask for one exact version and gets no latest fallback."""
    with pytest.raises(TemporalIdentitySnapshotNotFoundError):
        TemporalIdentitySnapshotStore(engine).get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
            expected_sha256="0" * 64,
        )


def test_constructor_does_not_create_tables(engine: Engine) -> None:
    """The engine is merely bound; schema creation belongs to migrations."""
    bare_engine = create_engine("sqlite:///:memory:")
    try:
        TemporalIdentitySnapshotStore(bare_engine)
        with bare_engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM sqlite_master WHERE type='table'")
                ).scalar_one()
                == 0
            )
    finally:
        bare_engine.dispose()


def test_constructor_rejects_unsupported_and_mariadb_dialects(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only SQLite and Oracle MySQL are accepted, without probing a server."""
    monkeypatch.setattr(engine.dialect, "name", "postgresql")
    with pytest.raises(ValueError, match="migration-approved MySQL"):
        TemporalIdentitySnapshotStore(engine)

    monkeypatch.setattr(engine.dialect, "name", "sqlite")
    monkeypatch.setattr(engine.dialect, "is_mariadb", True, raising=False)
    with pytest.raises(ValueError, match="migration-approved MySQL"):
        TemporalIdentitySnapshotStore(engine)


def test_store_rejects_an_unmigrated_schema_without_partial_write() -> None:
    """A missing migrated table produces a bounded store error and no DDL."""
    bare_engine = create_engine("sqlite:///:memory:")
    try:
        with pytest.raises(TemporalIdentitySnapshotStoreError, match="storage operation"):
            TemporalIdentitySnapshotStore(bare_engine).put(
                authority_id="source-a",
                registry_id="registry-a",
                registry=_registry(),
                expected_sha256=_digest(_registry()),
            )
        with bare_engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM sqlite_master WHERE type='table'")
                ).scalar_one()
                == 0
            )
    finally:
        bare_engine.dispose()


def test_store_rejects_wrong_registry_type_before_database_access(engine: Engine) -> None:
    """The storage boundary accepts only the exact validated registry value type."""
    with pytest.raises(TemporalIdentitySnapshotValidationError, match="TemporalIdentityRegistry"):
        TemporalIdentitySnapshotStore(engine).put(
            authority_id="source-a",
            registry_id="registry-a",
            registry=object(),  # type: ignore[arg-type]
            expected_sha256="0" * 64,
        )


def test_store_translates_codec_failure_to_a_fixed_validation_error(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codec exceptions do not expose payload or parser details through storage errors."""

    def fail_encode(_registry: TemporalIdentityRegistry) -> bytes:
        raise TemporalIdentitySnapshotError("synthetic codec detail")

    monkeypatch.setattr(store_module, "encode_temporal_identity_snapshot", fail_encode)
    with pytest.raises(
        TemporalIdentitySnapshotValidationError, match="canonical temporal identity snapshot"
    ):
        TemporalIdentitySnapshotStore(engine).put(
            authority_id="source-a",
            registry_id="registry-a",
            registry=_registry(),
            expected_sha256="0" * 64,
        )


def test_store_requires_registry_codec_round_trip(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decoder disagreement is rejected before any durable write."""
    monkeypatch.setattr(
        store_module,
        "decode_temporal_identity_snapshot",
        lambda _raw: _registry(evidence_ref="unexpected-registry"),
    )
    with pytest.raises(TemporalIdentitySnapshotValidationError, match="round-trip"):
        TemporalIdentitySnapshotStore(engine).put(
            authority_id="source-a",
            registry_id="registry-a",
            registry=_registry(),
            expected_sha256="0" * 64,
        )


def test_store_enforces_byte_and_chunk_count_limits_before_beginning_write(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard limits are checked before the first header or chunk insert."""
    registry = _registry()
    encoded = encode_temporal_identity_snapshot(registry)
    store = TemporalIdentitySnapshotStore(engine)
    monkeypatch.setattr(store_module, "MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES", len(encoded) - 1)
    with pytest.raises(TemporalIdentitySnapshotValidationError, match="fixed bound"):
        store.put(
            authority_id="source-a",
            registry_id="registry-a",
            registry=registry,
            expected_sha256=hashlib.sha256(encoded).hexdigest(),
        )

    monkeypatch.setattr(store_module, "MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(store_module, "MAX_SNAPSHOT_CHUNKS", 0)
    with pytest.raises(TemporalIdentitySnapshotValidationError, match="chunk limit"):
        store.put(
            authority_id="source-a",
            registry_id="registry-a",
            registry=registry,
            expected_sha256=hashlib.sha256(encoded).hexdigest(),
        )
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM od_temporal_identity_snapshots")
            ).scalar_one()
            == 0
        )


def test_store_rejects_invalid_clock_before_insert(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-positive clock cannot create a timestamped immutable version."""
    monkeypatch.setattr(store_module.time, "time_ns", lambda: -1_000)
    registry = _registry()
    with pytest.raises(TemporalIdentitySnapshotStoreError, match="clock is invalid"):
        TemporalIdentitySnapshotStore(engine).put(
            authority_id="source-a",
            registry_id="registry-a",
            registry=registry,
            expected_sha256=_digest(registry),
        )
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM od_temporal_identity_snapshots")
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "UPDATE od_temporal_identity_snapshots SET total_bytes='not-an-integer'",
            "header is malformed",
        ),
        ("UPDATE od_temporal_identity_snapshots SET total_bytes=0", "byte count is invalid"),
        ("UPDATE od_temporal_identity_snapshots SET chunk_count=0", "chunk count is invalid"),
        (
            "UPDATE od_temporal_identity_snapshots SET snapshot_sha256='short'",
            "digest is malformed",
        ),
        (
            "UPDATE od_temporal_identity_snapshots SET snapshot_sha256=X'01'",
            "digest is malformed",
        ),
        ("UPDATE od_temporal_identity_snapshots SET created_at_us=-1", "timestamp is invalid"),
    ],
)
def test_read_rejects_malformed_header_scalar_fields(
    engine: Engine, statement: str, message: str
) -> None:
    """Header types, bounds, digest width, and clock values fail closed."""
    _insert_one(engine, _registry())
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.exec_driver_sql(statement)

    with pytest.raises(TemporalIdentitySnapshotCorruptError, match=message):
        TemporalIdentitySnapshotStore(engine).get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
            expected_sha256=_digest(_registry()),
        )


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "UPDATE od_temporal_identity_snapshot_chunks SET chunk_no=-1",
            "not consecutively ordered",
        ),
        (
            "UPDATE od_temporal_identity_snapshot_chunks SET chunk_bytes=X''",
            "chunk length is invalid",
        ),
        (
            "UPDATE od_temporal_identity_snapshot_chunks SET chunk_bytes='text-bytes'",
            "chunk length is invalid",
        ),
        (
            "UPDATE od_temporal_identity_snapshot_chunks SET chunk_sha256='text-hash'",
            "chunk digest is malformed",
        ),
        (
            "UPDATE od_temporal_identity_snapshot_chunks SET chunk_sha256=X'01'",
            "chunk digest is malformed",
        ),
    ],
)
def test_read_rejects_malformed_chunk_scalar_fields(
    engine: Engine, statement: str, message: str
) -> None:
    """Chunk ordinal, exact BLOB type, length, and digest width are enforced."""
    _insert_one(engine, _registry())
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.exec_driver_sql(statement)

    with pytest.raises(TemporalIdentitySnapshotCorruptError, match=message):
        TemporalIdentitySnapshotStore(engine).get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
            expected_sha256=_digest(_registry()),
        )


def test_read_caps_returned_blob_bytes_before_driver_materialization(engine: Engine) -> None:
    """An oversized SQLite BLOB is measured in SQL and is not fetched into Python."""
    _insert_one(engine, _registry())
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            text("UPDATE od_temporal_identity_snapshot_chunks SET chunk_bytes=:chunk_bytes"),
            {"chunk_bytes": b"x" * (MAX_SNAPSHOT_CHUNK_BYTES + 1)},
        )

    with pytest.raises(TemporalIdentitySnapshotCorruptError, match="chunk length is invalid"):
        TemporalIdentitySnapshotStore(engine).get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
            expected_sha256=_digest(_registry()),
        )


def test_read_rejects_more_than_256_rows_before_reassembling(engine: Engine) -> None:
    """A corrupt oversized child set is bounded by the query row limit."""
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            text(
                "INSERT INTO od_temporal_identity_snapshots "
                "(authority_id, registry_id, reference_version, total_bytes, "
                "chunk_count, snapshot_sha256, created_at_us) "
                "VALUES ('source-a', 'registry-a', 'registry-v1', 257, 256, :sha, 1)"
            ),
            {"sha": hashlib.sha256(b"x" * 257).digest()},
        )
        connection.execute(
            text(
                "INSERT INTO od_temporal_identity_snapshot_chunks "
                "(authority_id, registry_id, reference_version, chunk_no, "
                "chunk_bytes, chunk_sha256) "
                "VALUES ('source-a', 'registry-a', 'registry-v1', :chunk_no, :data, :sha)"
            ),
            [
                {
                    "chunk_no": number,
                    "data": b"x",
                    "sha": hashlib.sha256(b"x").digest(),
                }
                for number in range(257)
            ],
        )

    with pytest.raises(TemporalIdentitySnapshotCorruptError, match="too many chunks"):
        TemporalIdentitySnapshotStore(engine).get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
            expected_sha256=hashlib.sha256(b"x" * 257).hexdigest(),
        )


def test_read_checks_cumulative_bytes_before_joining_all_chunks(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cumulative chunk bytes stop at the configured bound before final join."""
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            text(
                "INSERT INTO od_temporal_identity_snapshots "
                "(authority_id, registry_id, reference_version, total_bytes, "
                "chunk_count, snapshot_sha256, created_at_us) "
                "VALUES ('source-a', 'registry-a', 'registry-v1', 80, 2, :sha, 1)"
            ),
            {"sha": hashlib.sha256(b"x" * 120).digest()},
        )
        connection.execute(
            text(
                "INSERT INTO od_temporal_identity_snapshot_chunks "
                "(authority_id, registry_id, reference_version, chunk_no, "
                "chunk_bytes, chunk_sha256) "
                "VALUES ('source-a', 'registry-a', 'registry-v1', :chunk_no, :data, :sha)"
            ),
            [
                {
                    "chunk_no": number,
                    "data": b"x" * 60,
                    "sha": hashlib.sha256(b"x" * 60).digest(),
                }
                for number in range(2)
            ],
        )
    monkeypatch.setattr(store_module, "MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES", 100)

    with pytest.raises(TemporalIdentitySnapshotCorruptError, match="exceeds its byte limit"):
        TemporalIdentitySnapshotStore(engine).get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
            expected_sha256=hashlib.sha256(b"x" * 120).hexdigest(),
        )


def test_read_rejects_codec_version_that_differs_from_database_key(engine: Engine) -> None:
    """A valid codec snapshot cannot be relabeled under another version key."""
    registry = _registry(reference_version="registry-v1")
    raw = encode_temporal_identity_snapshot(registry)
    digest = hashlib.sha256(raw).digest()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO od_temporal_identity_snapshots "
                "(authority_id, registry_id, reference_version, total_bytes, "
                "chunk_count, snapshot_sha256, created_at_us) "
                "VALUES ('source-a', 'registry-a', 'registry-v2', :total, 1, :sha, 1)"
            ),
            {"total": len(raw), "sha": digest},
        )
        connection.execute(
            text(
                "INSERT INTO od_temporal_identity_snapshot_chunks "
                "(authority_id, registry_id, reference_version, chunk_no, "
                "chunk_bytes, chunk_sha256) VALUES "
                "('source-a', 'registry-a', 'registry-v2', 0, :raw, :chunk_sha)"
            ),
            {"raw": raw, "chunk_sha": hashlib.sha256(raw).digest()},
        )

    with pytest.raises(TemporalIdentitySnapshotCorruptError, match="codec version"):
        TemporalIdentitySnapshotStore(engine).get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v2",
            expected_sha256=digest.hex(),
        )


def test_concurrent_exact_mysql_style_conflict_is_accepted_as_replay(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a duplicate-key insert race, a fresh complete read may accept exact bytes."""
    registry = _registry()
    store = TemporalIdentitySnapshotStore(engine)
    expected = _digest(registry)
    first = store.put(
        authority_id="source-a",
        registry_id="registry-a",
        registry=registry,
        expected_sha256=expected,
    )
    original_transaction = store._transaction

    @contextmanager
    def simulate_insert_race(*, write: bool):
        if write:
            raise IntegrityError("duplicate", {}, RuntimeError("synthetic"))
        with original_transaction(write=write) as connection:
            yield connection

    monkeypatch.setattr(store, "_transaction", simulate_insert_race)
    replay = store.put(
        authority_id="source-a",
        registry_id="registry-a",
        registry=registry,
        expected_sha256=expected,
    )

    assert replay.already_existed is True
    assert replay.created_at_us == first.created_at_us


def test_concurrent_different_mysql_style_conflict_remains_immutable(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a duplicate-key race, another valid payload still conflicts."""
    original_registry = _registry()
    store = TemporalIdentitySnapshotStore(engine)
    original = store.put(
        authority_id="source-a",
        registry_id="registry-a",
        registry=original_registry,
        expected_sha256=_digest(original_registry),
    )
    original_transaction = store._transaction

    @contextmanager
    def simulate_insert_race(*, write: bool):
        if write:
            raise IntegrityError("duplicate", {}, RuntimeError("synthetic"))
        with original_transaction(write=write) as connection:
            yield connection

    monkeypatch.setattr(store, "_transaction", simulate_insert_race)
    changed_registry = _registry(evidence_ref="changed")
    with pytest.raises(TemporalIdentitySnapshotConflictError):
        store.put(
            authority_id="source-a",
            registry_id="registry-a",
            registry=changed_registry,
            expected_sha256=_digest(changed_registry),
        )
    assert (
        store.get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
            expected_sha256=original.snapshot_sha256,
        )
        == original_registry
    )


def test_two_sqlite_connections_racing_same_version_commit_one_complete_copy(
    tmp_path: Path,
) -> None:
    """SQLite BEGIN IMMEDIATE serializes independent connections on one version key."""
    database = create_engine(
        f"sqlite:///{tmp_path / 'temporal-identity-race.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 20},
        pool_size=2,
        max_overflow=0,
    )

    @event.listens_for(database, "connect")
    def _enable_foreign_keys(connection: object, _record: object) -> None:
        cursor = connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    with database.begin() as connection:
        connection.exec_driver_sql(_CREATE_HEADER)
        connection.exec_driver_sql(_CREATE_CHUNKS)
    registry = _registry()
    expected = _digest(registry)
    barrier = Barrier(2)

    def insert_after_barrier() -> bool:
        barrier.wait(timeout=10)
        receipt = TemporalIdentitySnapshotStore(database).put(
            authority_id="source-a",
            registry_id="registry-a",
            registry=registry,
            expected_sha256=expected,
        )
        return receipt.already_existed

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _index: insert_after_barrier(), range(2)))
        assert sorted(outcomes) == [False, True]
        with database.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM od_temporal_identity_snapshots")
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM od_temporal_identity_snapshot_chunks")
                ).scalar_one()
                == 1
            )
        assert (
            TemporalIdentitySnapshotStore(database).get(
                authority_id="source-a",
                registry_id="registry-a",
                reference_version="registry-v1",
                expected_sha256=expected,
            )
            == registry
        )
    finally:
        database.dispose()


def test_mysql_transaction_branch_is_synthetically_exercised_not_validated(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine.begin branch can be exercised locally without claiming MySQL QA."""
    monkeypatch.setattr(engine.dialect, "name", "mysql")
    registry = _registry()
    expected = _digest(registry)
    store = TemporalIdentitySnapshotStore(engine)
    receipt = store.put(
        authority_id="source-a",
        registry_id="registry-a",
        registry=registry,
        expected_sha256=expected,
    )
    assert (
        store.get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
            expected_sha256=expected,
        )
        == registry
    )
    assert receipt.snapshot_sha256 == expected


def test_store_splits_and_reassembles_bounded_ordered_chunks(engine: Engine) -> None:
    """A multi-chunk canonical snapshot preserves zero-based ordering and hashes."""
    registry = _large_registry()
    encoded = encode_temporal_identity_snapshot(registry)
    expected = hashlib.sha256(encoded).hexdigest()
    assert len(encoded) > MAX_SNAPSHOT_CHUNK_BYTES
    store = TemporalIdentitySnapshotStore(engine)
    receipt = store.put(
        authority_id="source-a",
        registry_id="registry-a",
        registry=registry,
        expected_sha256=expected,
    )

    assert receipt.chunk_count > 1
    assert (
        store.get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-large-v1",
            expected_sha256=expected,
        )
        == registry
    )
    with engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT chunk_no, chunk_bytes, chunk_sha256 "
                "FROM od_temporal_identity_snapshot_chunks "
                "ORDER BY chunk_no"
            )
        ).all()
    assert [row.chunk_no for row in stored] == list(range(receipt.chunk_count))
    assert all(1 <= len(row.chunk_bytes) <= MAX_SNAPSHOT_CHUNK_BYTES for row in stored)
    assert all(hashlib.sha256(row.chunk_bytes).digest() == row.chunk_sha256 for row in stored)
    assert b"".join(row.chunk_bytes for row in stored) == encoded


@pytest.mark.parametrize(
    "corruption",
    ["header_length", "header_chunk_count", "header_hash", "chunk_bytes", "chunk_hash"],
)
def test_read_rejects_tampered_header_or_chunk(engine: Engine, corruption: str) -> None:
    """Every stored length and digest field participates in read validation."""
    registry = _registry()
    expected = _insert_one(engine, registry)
    statements = {
        "header_length": ("UPDATE od_temporal_identity_snapshots SET total_bytes=total_bytes+1"),
        "header_chunk_count": (
            "UPDATE od_temporal_identity_snapshots SET chunk_count=chunk_count+1"
        ),
        "header_hash": ("UPDATE od_temporal_identity_snapshots SET snapshot_sha256=zeroblob(32)"),
        "chunk_hash": ("UPDATE od_temporal_identity_snapshot_chunks SET chunk_sha256=zeroblob(32)"),
    }
    if corruption == "chunk_bytes":
        original_chunk = _stored_chunk(engine)
        altered_chunk = original_chunk[:-1] + bytes([original_chunk[-1] ^ 1])
        assert altered_chunk != original_chunk
        assert len(altered_chunk) == len(original_chunk)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE od_temporal_identity_snapshot_chunks SET chunk_bytes=:chunk_bytes "
                    "WHERE authority_id='source-a' AND registry_id='registry-a' "
                    "AND reference_version='registry-v1' AND chunk_no=0"
                ),
                {"chunk_bytes": altered_chunk},
            )
            persisted_chunk, original_chunk_sha = connection.execute(
                text(
                    "SELECT chunk_bytes, chunk_sha256 "
                    "FROM od_temporal_identity_snapshot_chunks "
                    "WHERE authority_id='source-a' AND registry_id='registry-a' "
                    "AND reference_version='registry-v1' AND chunk_no=0"
                )
            ).one()
        assert type(persisted_chunk) is bytes
        assert persisted_chunk == altered_chunk
        assert hashlib.sha256(persisted_chunk).digest() != original_chunk_sha
    else:
        with engine.begin() as connection:
            connection.exec_driver_sql(statements[corruption])

    with pytest.raises(TemporalIdentitySnapshotCorruptError):
        TemporalIdentitySnapshotStore(engine).get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
            expected_sha256=expected,
        )


def test_read_rejects_a_missing_chunk_without_returning_partial_data(engine: Engine) -> None:
    """A chunk gap fails the complete snapshot rather than returning its prefix."""
    registry = _large_registry()
    expected = hashlib.sha256(encode_temporal_identity_snapshot(registry)).hexdigest()
    store = TemporalIdentitySnapshotStore(engine)
    receipt = store.put(
        authority_id="source-a",
        registry_id="registry-a",
        registry=registry,
        expected_sha256=expected,
    )
    assert receipt.chunk_count >= 2
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM od_temporal_identity_snapshot_chunks WHERE "
                "authority_id='source-a' AND registry_id='registry-a' "
                "AND reference_version='registry-large-v1' AND chunk_no=0"
            )
        )

    with pytest.raises(TemporalIdentitySnapshotCorruptError):
        store.get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-large-v1",
            expected_sha256=expected,
        )


def test_read_rejects_a_short_contiguous_chunk_list_by_header_count(engine: Engine) -> None:
    """A final missing chunk is rejected even when the remaining prefix is ordered."""
    registry = _large_registry()
    expected = hashlib.sha256(encode_temporal_identity_snapshot(registry)).hexdigest()
    store = TemporalIdentitySnapshotStore(engine)
    receipt = store.put(
        authority_id="source-a",
        registry_id="registry-a",
        registry=registry,
        expected_sha256=expected,
    )
    last_chunk = receipt.chunk_count - 1
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM od_temporal_identity_snapshot_chunks WHERE "
                "authority_id='source-a' AND registry_id='registry-a' "
                "AND reference_version='registry-large-v1' AND chunk_no=:chunk_no"
            ),
            {"chunk_no": last_chunk},
        )

    with pytest.raises(TemporalIdentitySnapshotCorruptError, match="chunk count does not match"):
        store.get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-large-v1",
            expected_sha256=expected,
        )


def test_read_redecodes_codec_after_valid_chunk_and_outer_hashes(engine: Engine) -> None:
    """Rehashed but semantically invalid bytes still fail the embedded canonical codec."""
    import json

    registry = _registry()
    original = encode_temporal_identity_snapshot(registry)
    original_digest = hashlib.sha256(original).hexdigest()
    _insert_one(engine, registry)
    chunk = _stored_chunk(engine)
    altered = chunk.replace(b"registry-evidence", b"registry-evidencf")
    assert len(altered) == len(chunk)
    altered_digest = hashlib.sha256(altered).digest()
    altered_outer_digest = hashlib.sha256(altered).digest()
    # Confirm the altered bytes still parse as JSON; failure belongs to the codec digest.
    json.loads(altered)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE od_temporal_identity_snapshot_chunks "
                "SET chunk_bytes=:chunk_bytes, chunk_sha256=:chunk_sha256"
            ),
            {"chunk_bytes": altered, "chunk_sha256": altered_digest},
        )
        connection.execute(
            text(
                "UPDATE od_temporal_identity_snapshots "
                "SET snapshot_sha256=:snapshot_sha256 WHERE authority_id='source-a' "
                "AND registry_id='registry-a' AND reference_version='registry-v1'"
            ),
            {"snapshot_sha256": altered_outer_digest},
        )

    assert original_digest != altered_outer_digest.hex()
    with pytest.raises(TemporalIdentitySnapshotCorruptError, match="canonical"):
        TemporalIdentitySnapshotStore(engine).get(
            authority_id="source-a",
            registry_id="registry-a",
            reference_version="registry-v1",
            expected_sha256=altered_outer_digest.hex(),
        )


def test_failed_chunk_insert_rolls_back_header_and_prior_chunks(engine: Engine) -> None:
    """One failed chunk leaves no visible header or incomplete child rows."""
    registry = _large_registry()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TRIGGER reject_second_chunk
            BEFORE INSERT ON od_temporal_identity_snapshot_chunks
            WHEN NEW.chunk_no = 1
            BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END
            """
        )

    with pytest.raises(TemporalIdentitySnapshotStoreError):
        TemporalIdentitySnapshotStore(engine).put(
            authority_id="source-a",
            registry_id="registry-a",
            registry=registry,
            expected_sha256=_digest(registry),
        )

    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM od_temporal_identity_snapshots")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM od_temporal_identity_snapshot_chunks")
            ).scalar_one()
            == 0
        )


def test_store_accepts_exact_case_sensitive_version_keys(engine: Engine) -> None:
    """Binary-collated version strings can differ only by their exact spelling."""
    store = TemporalIdentitySnapshotStore(engine)
    lower = _registry(reference_version="registry-v1")
    upper = _registry(reference_version="Registry-v1")
    store.put(
        authority_id="source-a",
        registry_id="registry-a",
        registry=lower,
        expected_sha256=_digest(lower),
    )
    store.put(
        authority_id="source-a",
        registry_id="registry-a",
        registry=upper,
        expected_sha256=_digest(upper),
    )

    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM od_temporal_identity_snapshots")
            ).scalar_one()
            == 2
        )
