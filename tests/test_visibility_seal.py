"""Post-commit publication visibility seal and monotonic sequence behaviour tests.

Every guard below is paired with a counterfactual: the same store, engines, and
publication that the guard rejects must succeed once the rejected condition is
removed, so a passing refusal proves the guard rather than the fixture.
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import (
    Column,
    Integer,
    create_engine,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError

from opendata.data.metadata_receipt_schema import METADATA_PUBLICATION_RECEIPTS
from opendata.data.metadata_receipt_writer import receive_metadata_publication_receipt
from opendata.pipeline import publication_outbox as _outbox
from opendata.pipeline import visibility_seal as seal_module
from opendata.pipeline.publication_store import (
    PUBLICATION_HEADS,
    PUBLICATION_METADATA,
    PUBLICATION_OUTBOX_EVENTS,
    PUBLICATIONS,
    PUBLISHED_FACT_VERSIONS,
    PublishedRecordInput,
    publish_in_connection,
)
from opendata.pipeline.visibility_seal import (
    VISIBILITY_SEALS,
    VisibilityClockPolicy,
    VisibilityClockRewindError,
    VisibilityClockUntrustedError,
    VisibilityCommitVisibilityError,
    VisibilitySealConflictError,
    VisibilitySealDisabledError,
    VisibilitySealError,
    VisibilitySealStore,
    make_visibility_seal_tables,
    visibility_seal_store_from_settings,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from opendata.pipeline.publication_store import PublicationRecord

_PRODUCER = "opendata-producer-alpha"
_TEN_YEARS_US = 10 * 365 * 86_400 * 1_000_000
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


def _engine(tmp_path: Path, name: str) -> Engine:
    """Create a disposable file-backed SQLite engine with a bounded busy timeout."""
    return create_engine(f"sqlite:///{tmp_path / name}", connect_args={"timeout": 30})


@pytest.fixture
def warehouse(tmp_path: Path) -> Iterator[Engine]:
    """Provide publication storage plus the new seal and sequence-ledger tables."""
    database = _engine(tmp_path, "visibility-warehouse.sqlite")
    # Seal storage is registered inside the publication registry, so one call builds both.
    PUBLICATION_METADATA.create_all(database)
    try:
        yield database
    finally:
        database.dispose()


@pytest.fixture
def metadata_db(tmp_path: Path) -> Iterator[Engine]:
    """Provide the separate control database that holds receipt copies."""
    database = _engine(tmp_path, "visibility-metadata.sqlite")
    METADATA_PUBLICATION_RECEIPTS.create(database)
    try:
        yield database
    finally:
        database.dispose()


def _records(idempotency_key: str, count: int) -> list[PublishedRecordInput]:
    """Build one bounded synthetic fact batch with publication-unique business keys."""
    return [
        PublishedRecordInput(
            business_key=f"{idempotency_key}:2026-10-{27 + index}",
            payload={"symbol": "600000.SH", "close": 12.5 + index},
            business_date=date(2026, 10, 27 + index),
        )
        for index in range(count)
    ]


def _publish(
    connection,
    idempotency_key: str,
    *,
    dataset: str = "equity.daily",
    count: int = 1,
    record_base: str | None = None,
) -> PublicationRecord:
    """Publish one deterministic synthetic publication through the real writer."""
    return publish_in_connection(
        connection,
        dataset_id=dataset,
        source_id="provider.alpha",
        schema_version="2",
        semantic_version="2.1",
        unit="CNY/share",
        adjustment_basis="raw",
        quality={"status": "passed"},
        provenance={"provider": "alpha"},
        idempotency_key=idempotency_key,
        records=_records(record_base or idempotency_key, count),
    )


def _receive(connection, event_json: str, event_sha256: str) -> None:
    """Persist one frozen outbox event copy in the metadata database."""
    receive_metadata_publication_receipt(
        connection,
        producer_identity=_PRODUCER,
        event_json_utf8=event_json.encode("utf-8"),
        event_sha256=event_sha256,
    )


def _commit_publication(
    warehouse: Engine,
    idempotency_key: str,
    *,
    dataset: str = "equity.daily",
    count: int = 1,
    record_base: str | None = None,
) -> PublicationRecord:
    """Commit a publication plus its outbox event and return the immutable record."""
    with warehouse.begin() as connection:
        return _publish(
            connection, idempotency_key, dataset=dataset, count=count, record_base=record_base
        )


def _committed_event(warehouse: Engine, publication_id: str):
    """Read one committed outbox event row."""
    with warehouse.connect() as connection:
        return (
            connection.execute(
                select(PUBLICATION_OUTBOX_EVENTS).where(
                    PUBLICATION_OUTBOX_EVENTS.c.publication_id == publication_id
                )
            )
            .mappings()
            .one()
        )


def _commit_receipt(warehouse: Engine, metadata_db: Engine, publication_id: str) -> None:
    """Copy one committed outbox event into the separate control database."""
    event = _committed_event(warehouse, publication_id)
    with metadata_db.begin() as connection:
        _receive(connection, event["event_json"], event["event_sha256"])


def _commit_everything(
    warehouse: Engine,
    metadata_db: Engine,
    idempotency_key: str,
    *,
    dataset: str = "equity.daily",
    count: int = 1,
) -> PublicationRecord:
    """Run the real producer plus receiver flow end to end for one publication."""
    publication = _commit_publication(warehouse, idempotency_key, dataset=dataset, count=count)
    _commit_receipt(warehouse, metadata_db, publication.publication_id)
    return publication


def _restamp(
    warehouse: Engine,
    metadata_db: Engine,
    publication_id: str,
    published_at_us: int,
) -> None:
    """Rewrite one committed publication's in-transaction stamp and its receipt copy.

    ``published_at_us`` is frozen inside the outbox event the receiver holds, so an
    operator who changes only the warehouse column is refused on the receipt check.
    Rewriting both keeps the test pointed at the guard it claims to exercise.
    """
    _execute(
        warehouse,
        "UPDATE od_publications SET published_at_us = :us WHERE publication_id = :pid",
        us=published_at_us,
        pid=publication_id,
    )
    event = json.loads(_receipt_event(metadata_db, publication_id)[0].decode("utf-8"))
    event["published_at_us"] = published_at_us
    body = _canonical_event_body(event)
    _execute(
        metadata_db,
        "UPDATE od_metadata_publication_receipts SET event_json_utf8 = :body, "
        "event_sha256 = :sha, published_at_us = :us WHERE publication_id = :pid",
        body=body,
        sha=hashlib.sha256(_outbox._EVENT_HASH_DOMAIN + body).hexdigest(),
        us=published_at_us,
        pid=publication_id,
    )


def _canonical_event_body(event: dict[str, object]) -> bytes:
    """Re-encode one receipt event exactly as the outbox freezes it."""
    return json.dumps(
        event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _store(
    warehouse: Engine,
    metadata_db: Engine,
    *,
    enabled: bool = True,
    policy: VisibilityClockPolicy | None = None,
    host_clock: Callable[[], datetime] | None = None,
) -> VisibilitySealStore:
    """Build a seal store over the two engines."""
    return VisibilitySealStore(
        warehouse,
        metadata_db,
        enabled=enabled,
        clock_policy=policy or VisibilityClockPolicy(),
        host_clock=host_clock,
    )


def _seal(store: VisibilitySealStore, publication_id: str):
    """Seal one committed publication with the deployment producer pin."""
    return store.seal_after_commit(publication_id=publication_id, producer_identity=_PRODUCER)


def _count(engine: Engine, table) -> int:
    """Count rows in one table."""
    with engine.connect() as connection:
        return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def _execute(engine: Engine, statement: str, **parameters: object) -> None:
    """Run one mutating statement outside the store, simulating an operator or a crash."""
    with engine.begin() as connection:
        connection.execute(text(statement), parameters)


def _failing_clock() -> datetime:
    """Stand in for a host clock the deployment cannot read."""
    raise RuntimeError("clock device unavailable")


def test_seal_is_refused_before_the_publishing_transaction_commits(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """An uncommitted publication is invisible to the seal and burns no sequence."""
    store = _store(warehouse, metadata_db)
    with warehouse.connect() as connection:
        transaction = connection.begin()
        publication = _publish(connection, "pre-commit-key")
        with pytest.raises(VisibilityCommitVisibilityError, match="not committed"):
            _seal(store, publication.publication_id)
        transaction.rollback()

    assert store.get_seal(publication.publication_id) is None
    assert store.sequence_state().initialised is False
    assert store.sequence_state().last_visibility_sequence == 0

    # Counterfactual: the same store seals once the facts really are committed.
    committed = _commit_everything(warehouse, metadata_db, "post-commit-key")
    assert _seal(store, committed.publication_id).record.visibility_sequence == 1


def test_seal_freezes_committed_evidence_and_emits_rfc3339_utc(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """The seal binds facts, pointer, receipt, and one trusted UTC instant."""
    publication = _commit_everything(warehouse, metadata_db, "evidence-key")
    record = _seal(_store(warehouse, metadata_db), publication.publication_id).record

    assert record.dataset_id == "equity.daily"
    assert record.publication_sequence == publication.sequence
    assert record.payload_sha256 == publication.payload_hash
    assert record.record_count == publication.record_count
    assert record.schema_version == publication.schema_version
    assert record.producer_identity == _PRODUCER
    assert (
        record.receipt_event_sha256
        == _committed_event(warehouse, publication.publication_id)["event_sha256"]
    )
    assert re.fullmatch(r"[0-9a-f]{64}", record.fact_set_sha256)
    assert record.visibility_sequence == 1
    assert record.sealed_at.tzinfo is timezone.utc
    assert _RFC3339.fullmatch(record.sealed_at_rfc3339)
    assert record.clock_evidence["authoritative_clock"] == "warehouse_database_utc_clock"
    assert record.clock_evidence["host_agreement"] == "not_supplied"


def test_fact_set_digest_changes_with_committed_fact_content(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """The seal digests the exact committed partition, not just its row count."""
    one = _commit_everything(warehouse, metadata_db, "digest-one")
    two = _commit_everything(warehouse, metadata_db, "digest-two")
    store = _store(warehouse, metadata_db)
    first = _seal(store, one.publication_id).record
    second = _seal(store, two.publication_id).record
    assert first.fact_set_sha256 != second.fact_set_sha256
    assert first.payload_sha256 != second.payload_sha256


def test_unchanged_delta_publication_seals_without_appending_a_revision(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A delta whose content already exists appends no revision and still seals."""
    original = _commit_everything(warehouse, metadata_db, "delta-original")
    assert (
        _seal(_store(warehouse, metadata_db), original.publication_id).record.visibility_sequence
        == 1
    )

    with warehouse.begin() as connection:
        # The same business key and canonical content as the committed publication.
        replayed = _publish(connection, "delta-replay", record_base="delta-original")
    _commit_receipt(warehouse, metadata_db, replayed.publication_id)
    with warehouse.connect() as connection:
        bound = connection.execute(
            select(func.count())
            .select_from(PUBLISHED_FACT_VERSIONS)
            .where(PUBLISHED_FACT_VERSIONS.c.publication_id == replayed.publication_id)
        ).scalar_one()
    assert bound == 0 and replayed.record_count == 1

    store = _store(warehouse, metadata_db)
    sealed = _seal(store, replayed.publication_id).record
    assert sealed.visibility_sequence == 2
    original_seal = store.get_seal(original.publication_id)
    assert original_seal is not None
    # The appended-partition digest distinguishes the delta from its predecessor.
    assert sealed.fact_set_sha256 != original_seal.fact_set_sha256


def test_visibility_sequence_is_monotonic_persistent_and_never_reused(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A fresh store over the same ledger continues the persisted sequence."""
    first = _commit_everything(warehouse, metadata_db, "mono-one")
    second = _commit_everything(warehouse, metadata_db, "mono-two")
    assert (
        _seal(_store(warehouse, metadata_db), first.publication_id).record.visibility_sequence == 1
    )
    # A new store object stands in for a restarted process.
    restarted = _store(warehouse, metadata_db)
    assert _seal(restarted, second.publication_id).record.visibility_sequence == 2
    state = restarted.sequence_state()
    assert state.last_visibility_sequence == 2
    assert state.seal_count == 2
    assert state.last_sealed_at is not None


def test_missing_cross_database_receipt_fails_closed(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """Without a committed receipt the publication stays unsealed."""
    publication = _commit_publication(warehouse, "no-receipt-key")
    store = _store(warehouse, metadata_db)
    with pytest.raises(VisibilityCommitVisibilityError, match="receipt is not committed"):
        _seal(store, publication.publication_id)
    assert store.get_seal(publication.publication_id) is None
    assert store.sequence_state().initialised is False

    # Counterfactual: the same store seals as soon as the receipt commits.
    _commit_receipt(warehouse, metadata_db, publication.publication_id)
    assert _seal(store, publication.publication_id).record.visibility_sequence == 1


def test_uncommitted_receipt_copy_is_invisible_until_committed(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """The seal verifies the receipt read-after-commit, not inside its writer."""
    publication = _commit_publication(warehouse, "receipt-tx-key")
    event = _committed_event(warehouse, publication.publication_id)
    store = _store(warehouse, metadata_db)
    with metadata_db.connect() as connection:
        transaction = connection.begin()
        _receive(connection, event["event_json"], event["event_sha256"])
        with pytest.raises(VisibilityCommitVisibilityError, match="receipt is not committed"):
            _seal(store, publication.publication_id)
        transaction.rollback()

    with pytest.raises(VisibilityCommitVisibilityError, match="receipt is not committed"):
        _seal(store, publication.publication_id)

    with metadata_db.begin() as connection:
        _receive(connection, event["event_json"], event["event_sha256"])
    assert _seal(store, publication.publication_id).record.visibility_sequence == 1


def test_tampered_receipt_bytes_fail_closed(warehouse: Engine, metadata_db: Engine) -> None:
    """A receipt whose frozen event bytes no longer match its digest is refused."""
    publication = _commit_everything(warehouse, metadata_db, "tamper-key")
    _execute(
        metadata_db,
        "UPDATE od_metadata_publication_receipts SET event_json_utf8 = :payload",
        payload=b'{"dataset_id":"equity.daily"}',
    )
    store = _store(warehouse, metadata_db)
    with pytest.raises(VisibilityCommitVisibilityError, match="frozen domain-separated digest"):
        _seal(store, publication.publication_id)
    assert store.get_seal(publication.publication_id) is None


def test_receipt_content_bound_to_another_publication_fails_closed(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A receipt for a different producer pin is not evidence for this publication."""
    publication = _commit_publication(warehouse, "wrong-producer-key")
    event = _committed_event(warehouse, publication.publication_id)
    with metadata_db.begin() as connection:
        receive_metadata_publication_receipt(
            connection,
            producer_identity="some-other-producer",
            event_json_utf8=event["event_json"].encode("utf-8"),
            event_sha256=event["event_sha256"],
        )
    store = _store(warehouse, metadata_db)
    with pytest.raises(VisibilityCommitVisibilityError, match="receipt is not committed"):
        _seal(store, publication.publication_id)


def test_incomplete_warehouse_fact_partition_fails_closed(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A publication whose committed facts are short of its count cannot seal."""
    publication = _commit_publication(warehouse, "short-facts-key", count=2)
    _commit_receipt(warehouse, metadata_db, publication.publication_id)
    assert publication.record_count == 2
    with warehouse.begin() as connection:
        connection.execute(
            PUBLISHED_FACT_VERSIONS.delete().where(
                PUBLISHED_FACT_VERSIONS.c.publication_id == publication.publication_id
            )
        )
        connection.execute(
            insert(PUBLISHED_FACT_VERSIONS).values(
                dataset_id="equity.daily",
                business_key="short-facts-key:2026-10-27",
                revision=1,
                publication_id=publication.publication_id,
                publication_sequence=publication.sequence,
                business_date=date(2026, 10, 27),
                record_json="{}",
                record_hash="a" * 64,
                is_deleted=False,
            )
        )

    store = _store(warehouse, metadata_db)
    with pytest.raises(VisibilityCommitVisibilityError, match="fact partition is incomplete"):
        _seal(store, publication.publication_id)
    assert store.get_seal(publication.publication_id) is None


def test_rolled_back_publication_pointer_fails_closed(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """The dataset publication pointer must be committed and advanced for the seal."""
    publication = _commit_everything(warehouse, metadata_db, "pointer-key")
    _execute(
        warehouse,
        "UPDATE od_publication_heads SET latest_sequence = 0 WHERE dataset_id = :dataset",
        dataset="equity.daily",
    )
    store = _store(warehouse, metadata_db)
    with pytest.raises(VisibilityCommitVisibilityError, match="pointer has not advanced"):
        _seal(store, publication.publication_id)
    assert store.get_seal(publication.publication_id) is None


def test_publication_pointer_ahead_of_target_still_seals(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """An older publication whose dataset moved on is still sealable exactly once."""
    older = _commit_everything(warehouse, metadata_db, "pointer-ahead-older")
    newer = _commit_everything(warehouse, metadata_db, "pointer-ahead-newer")
    store = _store(warehouse, metadata_db)
    assert _seal(store, older.publication_id).record.visibility_sequence == 1
    with warehouse.connect() as connection:
        head = (
            connection.execute(
                select(PUBLICATION_HEADS).where(PUBLICATION_HEADS.c.dataset_id == "equity.daily")
            )
            .mappings()
            .one()
        )
    assert head["latest_publication_id"] == newer.publication_id
    assert _seal(store, newer.publication_id).record.visibility_sequence == 2


def test_pointer_naming_an_unbound_successor_fails_closed(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A head row naming a successor from another dataset is rejected."""
    publication = _commit_everything(warehouse, metadata_db, "bad-successor-key")
    other = _commit_publication(warehouse, "bad-successor-other", dataset="fund.nav")
    _execute(
        warehouse,
        "UPDATE od_publication_heads SET latest_publication_id = :publication_id, "
        "latest_sequence = 99 WHERE dataset_id = :dataset",
        publication_id=other.publication_id,
        dataset="equity.daily",
    )
    store = _store(warehouse, metadata_db)
    with pytest.raises(VisibilityCommitVisibilityError, match="successor"):
        _seal(store, publication.publication_id)


def test_crash_between_commit_and_seal_then_retry_is_idempotent(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """The crash window leaves the publication unsealed; the retry seals it once."""
    publication = _commit_everything(warehouse, metadata_db, "crash-window-key")
    store = _store(warehouse, metadata_db)
    # The process died after the commit and before the seal: nothing is visible.
    assert store.get_seal(publication.publication_id) is None

    first = _seal(store, publication.publication_id)
    assert first.replayed is False
    retry = _seal(store, publication.publication_id)
    assert retry.replayed is True
    assert retry.record == first.record

    assert _count(warehouse, VISIBILITY_SEALS) == 1
    assert store.sequence_state().last_visibility_sequence == first.record.visibility_sequence


def test_partial_seal_rollback_never_burns_a_visibility_sequence(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A failure inside the seal transaction leaves no seal and no sequence advance."""
    publication = _commit_everything(warehouse, metadata_db, "partial-key")
    _execute(
        warehouse,
        "CREATE TRIGGER fail_seal_insert BEFORE INSERT ON od_publication_visibility_seals "
        "BEGIN SELECT RAISE(ABORT, 'synthetic seal failure'); END",
    )
    store = _store(warehouse, metadata_db)
    with pytest.raises(Exception, match="synthetic seal failure"):
        _seal(store, publication.publication_id)

    assert store.get_seal(publication.publication_id) is None
    assert store.sequence_state().initialised is False

    _execute(warehouse, "DROP TRIGGER fail_seal_insert")
    # Counterfactual: with the injected failure removed, the same request seals at sequence 1.
    assert _seal(store, publication.publication_id).record.visibility_sequence == 1


def test_concurrent_duplicate_seal_attempts_write_exactly_one_sequence(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """Racing seals of one publication replay the winner instead of renumbering it."""
    publication = _commit_everything(warehouse, metadata_db, "race-same-key")
    store = _store(warehouse, metadata_db)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: _seal(store, publication.publication_id), range(3)))

    assert all(result.record == results[0].record for result in results)
    assert results[0].record.visibility_sequence == 1
    assert _count(warehouse, VISIBILITY_SEALS) == 1
    assert store.sequence_state().last_visibility_sequence == 1
    assert store.sequence_state().seal_count == 1


def test_concurrent_seals_of_distinct_publications_never_duplicate_a_sequence(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """Parallel writers advance one monotonic ledger with no repeated value."""
    publications = [
        _commit_everything(warehouse, metadata_db, f"race-distinct-{index}") for index in range(4)
    ]
    store = _store(warehouse, metadata_db)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda item: _seal(store, item.publication_id), publications),
        )

    assert sorted(result.record.visibility_sequence for result in results) == [1, 2, 3, 4]
    assert len({result.record.publication_id for result in results}) == 4
    assert store.sequence_state().last_visibility_sequence == 4
    assert store.sequence_state().seal_count == 4
    assert _count(warehouse, VISIBILITY_SEALS) == 4


def test_clock_rewind_against_the_persisted_high_water_fails_closed(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A seal instant behind the ledger high-water mark is refused without sealing."""
    first = _commit_everything(warehouse, metadata_db, "rewind-one")
    store = _store(warehouse, metadata_db)
    _seal(store, first.publication_id)
    # Persisted evidence of a later seal makes any current reading a rewind.
    _execute(
        warehouse,
        "UPDATE od_visibility_sequence_state SET last_sealed_at_us = last_sealed_at_us + :delta "
        "WHERE state_id = 1",
        delta=_TEN_YEARS_US,
    )
    second = _commit_everything(warehouse, metadata_db, "rewind-two")

    with pytest.raises(VisibilityClockRewindError, match="high-water mark"):
        _seal(store, second.publication_id)
    assert store.get_seal(second.publication_id) is None
    assert store.sequence_state().last_visibility_sequence == 1


def test_forward_clock_gate_rejects_both_persisted_reversals() -> None:
    """The pure gate rejects a ledger rewind and a pre-commit seal instant."""
    gate = seal_module._require_forward_clock
    gate(1_000, 1_000, 1)
    with pytest.raises(VisibilityClockRewindError, match="high-water mark"):
        gate(999, 1_000, 1)
    too_early = 1_000 + seal_module._COMMIT_INSTANT_TOLERANCE_US + 1
    with pytest.raises(VisibilityClockRewindError, match="publication commit instant"):
        gate(1_000, 1_000, too_early)


def test_untrusted_host_clock_disagreement_fails_closed(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A host clock skewed against the authoritative clock cannot seal anything."""
    publication = _commit_everything(warehouse, metadata_db, "skew-key")
    skewed = _store(
        warehouse,
        metadata_db,
        host_clock=lambda: datetime.now(timezone.utc) + timedelta(hours=1),
    )
    with pytest.raises(VisibilityClockUntrustedError, match="disagrees"):
        _seal(skewed, publication.publication_id)
    assert skewed.get_seal(publication.publication_id) is None

    # Counterfactual: an agreeing host clock seals and records the agreement.
    aligned = _store(
        warehouse,
        metadata_db,
        policy=VisibilityClockPolicy(max_host_database_skew_us=30_000_000),
        host_clock=lambda: datetime.now(timezone.utc),
    )
    record = _seal(aligned, publication.publication_id).record
    assert record.clock_evidence["host_agreement"] == "agreed"
    assert record.clock_evidence["host_utc_us"] is not None


@pytest.mark.parametrize(
    ("host_clock", "message"),
    [
        (lambda: datetime(2026, 10, 31, 9, 0, 0), "timezone-aware"),
        (
            lambda: datetime(2026, 10, 31, 9, 0, 0, tzinfo=timezone(timedelta(hours=8))),
            "must return a timezone-aware UTC datetime",
        ),
        (_failing_clock, "unreadable"),
    ],
)
def test_naive_or_unreadable_host_clock_is_untrusted(
    warehouse: Engine, metadata_db: Engine, host_clock, message: str
) -> None:
    """Only an aware UTC host reading is even considered as trust evidence."""
    publication = _commit_everything(warehouse, metadata_db, "host-clock-key")
    store = _store(warehouse, metadata_db, host_clock=host_clock)
    with pytest.raises(VisibilityClockUntrustedError, match=message):
        _seal(store, publication.publication_id)
    assert store.get_seal(publication.publication_id) is None


def test_clock_outside_the_plausible_utc_window_fails_closed(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """An implausible authoritative instant never becomes a seal."""
    publication = _commit_everything(warehouse, metadata_db, "implausible-key")
    store = _store(
        warehouse,
        metadata_db,
        policy=VisibilityClockPolicy(
            min_plausible_utc=datetime(2099, 1, 1, tzinfo=timezone.utc),
            max_plausible_utc=datetime(2100, 1, 1, tzinfo=timezone.utc),
        ),
    )
    with pytest.raises(VisibilityClockUntrustedError, match="plausible UTC window"):
        _seal(store, publication.publication_id)
    assert store.get_seal(publication.publication_id) is None


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: VisibilityClockPolicy(
                min_plausible_utc=datetime(2100, 1, 1, tzinfo=timezone.utc),
                max_plausible_utc=datetime(2099, 1, 1, tzinfo=timezone.utc),
            ),
            "must precede",
        ),
        (
            lambda: VisibilityClockPolicy(min_plausible_utc=datetime(2020, 1, 1)),
            "timezone-aware",
        ),
        (lambda: VisibilityClockPolicy(max_host_database_skew_us=-1), "max_host_database_skew_us"),
        (lambda: VisibilityClockPolicy(sequence_attempts=0), "sequence_attempts"),
    ],
)
def test_unusable_clock_policy_is_rejected(factory, message: str) -> None:
    """A policy that could not fail closed is refused when it is built."""
    with pytest.raises(ValueError, match=message):
        factory()


def test_replay_after_committed_evidence_change_reports_conflict(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A seal whose fact partition was rewritten afterwards is not replayed."""
    publication = _commit_everything(warehouse, metadata_db, "conflict-key")
    store = _store(warehouse, metadata_db)
    sealed = _seal(store, publication.publication_id).record
    _execute(
        warehouse,
        "UPDATE od_published_fact_versions SET record_hash = :hash WHERE publication_id = :pid",
        hash="f" * 64,
        pid=publication.publication_id,
    )

    with pytest.raises(VisibilitySealConflictError, match="no longer matches"):
        _seal(store, publication.publication_id)
    assert store.get_seal(publication.publication_id) == sealed
    assert _count(warehouse, VISIBILITY_SEALS) == 1


def test_disabled_flag_writes_no_seal_and_no_ledger(warehouse: Engine, metadata_db: Engine) -> None:
    """The default-off surface refuses to create any visibility state."""
    publication = _commit_everything(warehouse, metadata_db, "disabled-key")
    disabled = _store(warehouse, metadata_db, enabled=False)
    assert disabled.enabled is False
    with pytest.raises(VisibilitySealDisabledError, match="VISIBILITY_SEAL_ENABLED"):
        _seal(disabled, publication.publication_id)
    assert disabled.get_seal(publication.publication_id) is None
    assert disabled.sequence_state().initialised is False

    # Counterfactual: the identical target seals once the flag is on.
    assert _seal(_store(warehouse, metadata_db), publication.publication_id).replayed is False


def test_settings_flag_defaults_off_and_only_literal_true_enables(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """Settings-driven construction is default-off and never coerces a truthy value."""
    built = visibility_seal_store_from_settings
    for value in (
        {},
        {"visibility_seal_enabled": False},
        {"visibility_seal_enabled": "true"},
        {"visibility_seal_enabled": 1},
    ):
        store = built(
            SimpleNamespace(**value),
            warehouse_engine=warehouse,
            metadata_engine=metadata_db,
        )
        assert store.enabled is False
    assert (
        built(
            SimpleNamespace(visibility_seal_enabled=True),
            warehouse_engine=warehouse,
            metadata_engine=metadata_db,
        ).enabled
        is True
    )


def test_server_settings_ship_the_seal_flag_disabled() -> None:
    """The shipped server setting keeps sealing off by default."""
    from opendata.core.config import Settings

    assert Settings().visibility_seal_enabled is False


def test_unsealed_publication_is_absent_from_sealed_reads(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """Only sealed publications can appear in the store-level cutoff reads."""
    first = _commit_everything(warehouse, metadata_db, "read-one")
    second = _commit_everything(warehouse, metadata_db, "read-two")
    store = _store(warehouse, metadata_db)
    _seal(store, first.publication_id)

    assert store.get_seal(second.publication_id) is None
    assert [record.publication_id for record in store.list_seals("equity.daily")] == [
        first.publication_id
    ]
    assert store.list_seals("equity.daily", through_visibility_sequence=0) == ()
    _seal(store, second.publication_id)
    sealed = store.list_seals("equity.daily")
    assert [record.visibility_sequence for record in sealed] == [1, 2]
    assert [record.visibility_sequence for record in store.list_seals("equity.daily", limit=1)] == [
        1
    ]


def test_sequence_state_refuses_to_report_a_ledger_that_disagrees(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """Corrupted ledger counters are reported as an error, not as visibility."""
    first = _commit_everything(warehouse, metadata_db, "corrupt-one")
    second = _commit_everything(warehouse, metadata_db, "corrupt-two")
    store = _store(warehouse, metadata_db)
    _seal(store, first.publication_id)
    _seal(store, second.publication_id)
    assert store.sequence_state().seal_count == 2
    # A ledger that lost one of the two durable seals must not be reported as visibility.
    _execute(
        warehouse,
        "UPDATE od_visibility_sequence_state SET seal_count = 1 WHERE state_id = 1",
    )
    with pytest.raises(VisibilitySealError, match="does not match persisted seals"):
        store.sequence_state()


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("publication_id", "publication_id must be a lowercase"),
        ("producer_identity", "producer_identity must be a bounded"),
        ("dataset_id", "dataset_id must be a bounded"),
        ("limit", "limit must be between"),
    ],
)
def test_malformed_arguments_are_rejected(
    warehouse: Engine, metadata_db: Engine, entry: str, message: str
) -> None:
    """Every seal entry point validates identity and bound inputs before any I/O."""
    store = _store(warehouse, metadata_db)
    with pytest.raises(ValueError, match=re.escape(message)):
        if entry == "publication_id":
            store.seal_after_commit(publication_id="NOT-A-HEX-ID", producer_identity=_PRODUCER)
        elif entry == "producer_identity":
            store.seal_after_commit(publication_id="a" * 32, producer_identity="bad\nidentity")
        elif entry == "dataset_id":
            store.list_seals("bad dataset ")
        else:
            store.list_seals("equity.daily", limit=0)


def test_engine_types_are_validated_at_construction(warehouse: Engine, metadata_db: Engine) -> None:
    """The store refuses a non-Engine target rather than failing later."""
    with pytest.raises(TypeError, match="warehouse_engine"):
        VisibilitySealStore(object(), metadata_db)
    with pytest.raises(TypeError, match="metadata_engine"):
        VisibilitySealStore(warehouse, object())


def test_partially_registered_seal_storage_is_refused() -> None:
    """A registry holding only one of the two seal tables is not adopted."""
    from sqlalchemy import MetaData, Table

    metadata = MetaData()
    Table("od_visibility_sequence_state", metadata, Column("state_id", Integer, primary_key=True))
    with pytest.raises(ValueError, match="only partly registered"):
        make_visibility_seal_tables(metadata)


@pytest.mark.parametrize(
    ("policy_factory", "message"),
    [
        (
            lambda: VisibilityClockPolicy(
                max_plausible_utc=datetime(2100, 1, 1, tzinfo=timezone(timedelta(hours=2)))
            ),
            "must use UTC",
        ),
        (lambda: VisibilityClockPolicy(min_plausible_utc="2020-01-01"), "timezone-aware"),
    ],
)
def test_non_utc_or_untyped_clock_bound_is_rejected(policy_factory, message: str) -> None:
    """The policy cannot carry a bound the fail-closed gate could not compare."""
    with pytest.raises(ValueError, match=message):
        policy_factory()


def test_missing_publication_head_row_fails_closed(warehouse: Engine, metadata_db: Engine) -> None:
    """Without the committed pointer row the publication is not visible."""
    publication = _commit_everything(warehouse, metadata_db, "no-head-key")
    _execute(
        warehouse,
        "DELETE FROM od_publication_heads WHERE dataset_id = :dataset",
        dataset="equity.daily",
    )
    store = _store(warehouse, metadata_db)
    with pytest.raises(VisibilityCommitVisibilityError, match="pointer is not committed"):
        _seal(store, publication.publication_id)


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "UPDATE od_publications SET schema_version = '' WHERE publication_id = :pid",
            "schema version is invalid",
        ),
        (
            "UPDATE od_publications SET published_at_us = 9000000000000000000 "
            "WHERE publication_id = :pid",
            "out of the supported range",
        ),
    ],
)
def test_malformed_committed_publication_row_fails_closed(
    warehouse: Engine, metadata_db: Engine, statement: str, message: str
) -> None:
    """A corrupt publication row never becomes a visibility seal."""
    publication = _commit_everything(warehouse, metadata_db, "corrupt-pub-key")
    _execute(warehouse, statement, pid=publication.publication_id)
    store = _store(warehouse, metadata_db)
    with pytest.raises(VisibilityCommitVisibilityError, match=message):
        _seal(store, publication.publication_id)


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "UPDATE od_published_fact_versions SET dataset_id = 'other.daily' "
            "WHERE publication_id = :pid",
            "different dataset or publication sequence",
        ),
        (
            "UPDATE od_published_fact_versions SET business_key = '' WHERE publication_id = :pid",
            "no business key",
        ),
        (
            "UPDATE od_published_fact_versions SET record_hash = 'zz' WHERE publication_id = :pid",
            "record_hash must be a lowercase SHA-256 digest",
        ),
    ],
)
def test_malformed_committed_fact_row_fails_closed(
    warehouse: Engine, metadata_db: Engine, statement: str, message: str
) -> None:
    """Every fact row must still describe this publication before the seal is written."""
    publication = _commit_everything(warehouse, metadata_db, "corrupt-fact-key")
    _execute(warehouse, statement, pid=publication.publication_id)
    store = _store(warehouse, metadata_db)
    with pytest.raises(VisibilityCommitVisibilityError, match=message):
        _seal(store, publication.publication_id)


def test_corrupt_persisted_seal_evidence_is_refused_on_read(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A seal row whose clock evidence is no longer an object never parses as visibility."""
    publication = _commit_everything(warehouse, metadata_db, "corrupt-seal-key")
    store = _store(warehouse, metadata_db)
    sealed = _seal(store, publication.publication_id).record
    assert sealed.sealed_at_rfc3339
    _execute(
        warehouse,
        "UPDATE od_publication_visibility_seals SET clock_evidence_json = :payload "
        "WHERE publication_id = :pid",
        payload="[]",
        pid=publication.publication_id,
    )
    with pytest.raises(VisibilitySealError, match="not a JSON object"):
        store.get_seal(publication.publication_id)


def test_stale_ledger_read_retries_then_reports_sequence_contention(
    warehouse: Engine, metadata_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compare-and-set miss never writes a seal and surfaces as bounded contention."""
    first = _commit_everything(warehouse, metadata_db, "contend-one")
    second = _commit_everything(warehouse, metadata_db, "contend-two")
    store = _store(warehouse, metadata_db)
    _seal(store, first.publication_id)

    # Force the transaction to read a stale ledger, exactly as a losing concurrent writer does.
    monkeypatch.setattr(seal_module, "_locked_state", lambda _connection: (0, 0, 0))
    with pytest.raises(seal_module.VisibilitySequenceContentionError, match="contended"):
        _seal(store, second.publication_id)

    assert store.get_seal(second.publication_id) is None
    assert _count(warehouse, VISIBILITY_SEALS) == 1
    assert store.sequence_state().last_visibility_sequence == 1


def test_seal_that_cannot_be_read_back_is_reported_as_failure(
    warehouse: Engine, metadata_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-after-commit verification of the seal itself is not optional."""
    publication = _commit_everything(warehouse, metadata_db, "readback-key")
    store = _store(warehouse, metadata_db)
    calls = 0

    def blind(_self, _publication_id: str):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(VisibilitySealStore, "get_seal", blind)
    with pytest.raises(VisibilitySealError, match="could not be read back"):
        _seal(store, publication.publication_id)
    assert calls >= 1


def test_seal_read_back_of_different_values_is_a_conflict(
    warehouse: Engine, metadata_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read-back must equal the values the seal transaction wrote."""
    publication = _commit_everything(warehouse, metadata_db, "readback-diff-key")
    store = _store(warehouse, metadata_db)
    honest = VisibilitySealStore.get_seal
    state = {"calls": 0}

    def altered(self, publication_id: str):
        state["calls"] += 1
        record = honest(self, publication_id)
        if state["calls"] < 2 or record is None:
            return record
        from dataclasses import replace

        return replace(record, record_count=record.record_count + 7)

    monkeypatch.setattr(VisibilitySealStore, "get_seal", altered)
    with pytest.raises(VisibilitySealError, match="does not match the values written"):
        _seal(store, publication.publication_id)


def test_non_utc_clock_bound_policy_is_rejected() -> None:
    """A bound that is not a UTC instant cannot drive the fail-closed comparison."""
    with pytest.raises(ValueError, match="must use UTC"):
        VisibilityClockPolicy(
            min_plausible_utc=datetime(2020, 1, 1, tzinfo=timezone(timedelta(hours=2)))
        )


def test_missing_head_row_and_unnamed_successor_fail_closed(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """Both pointer-absent and pointer-without-a-successor refuse the seal."""
    publication = _commit_everything(warehouse, metadata_db, "head-missing-key")
    store = _store(warehouse, metadata_db)
    _execute(
        warehouse,
        "DELETE FROM od_publication_heads WHERE dataset_id = :dataset",
        dataset="equity.daily",
    )
    with pytest.raises(VisibilityCommitVisibilityError, match="pointer is not committed"):
        _seal(store, publication.publication_id)

    # Re-create the pointer as a row that advanced past this publication but names no
    # successor: the seal cannot prove which committed publication the pointer refers to.
    _execute(
        warehouse,
        "INSERT INTO od_publication_heads "
        "(dataset_id, latest_sequence, latest_publication_id, watermark_json, updated_at_us) "
        "VALUES (:dataset, 5, NULL, NULL, 1)",
        dataset="equity.daily",
    )
    with pytest.raises(VisibilityCommitVisibilityError, match="no committed successor"):
        _seal(store, publication.publication_id)


def test_corrupt_publication_and_fact_columns_fail_closed(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A publication or fact row outside the reviewed shape is never sealed."""
    publication = _commit_everything(warehouse, metadata_db, "corrupt-row-key")
    _execute(
        warehouse,
        "UPDATE od_publications SET schema_version = '' WHERE publication_id = :pid",
        pid=publication.publication_id,
    )
    store = _store(warehouse, metadata_db)
    with pytest.raises(VisibilityCommitVisibilityError, match="schema version is invalid"):
        _seal(store, publication.publication_id)

    _execute(
        warehouse,
        "UPDATE od_publications SET schema_version = '2' WHERE publication_id = :pid",
        pid=publication.publication_id,
    )
    _execute(
        warehouse,
        "UPDATE od_published_fact_versions SET business_key = '' WHERE publication_id = :pid",
        pid=publication.publication_id,
    )
    with pytest.raises(VisibilityCommitVisibilityError, match="no business key"):
        _seal(store, publication.publication_id)


def test_receipt_row_and_event_body_drift_fail_closed(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """The receipt must match the publication in its row columns and in its frozen body."""
    publication = _commit_everything(warehouse, metadata_db, "drift-key")
    _execute(
        metadata_db,
        "UPDATE od_metadata_publication_receipts SET publication_sequence = 99 "
        "WHERE publication_id = :pid",
        pid=publication.publication_id,
    )
    store = _store(warehouse, metadata_db)
    with pytest.raises(
        VisibilityCommitVisibilityError, match="receipt row sequence does not match"
    ):
        _seal(store, publication.publication_id)

    _execute(
        metadata_db,
        "UPDATE od_metadata_publication_receipts SET publication_sequence = :sequence "
        "WHERE publication_id = :pid",
        sequence=publication.sequence,
        pid=publication.publication_id,
    )
    body = b"{}"
    digest = hashlib.sha256(b"opendata.publication-outbox.event.v1\n" + body).hexdigest()
    _execute(
        metadata_db,
        "UPDATE od_metadata_publication_receipts SET event_json_utf8 = :body, event_sha256 = :sha "
        "WHERE publication_id = :pid",
        body=body,
        sha=digest,
        pid=publication.publication_id,
    )
    with pytest.raises(VisibilityCommitVisibilityError, match="content does not match"):
        _seal(store, publication.publication_id)


def test_exhausted_visibility_sequence_refuses_without_writing(
    warehouse: Engine, metadata_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the ledger cannot advance, the seal is refused rather than wrapped around."""
    first = _commit_everything(warehouse, metadata_db, "exhausted-one")
    second = _commit_everything(warehouse, metadata_db, "exhausted-two", dataset="fund.nav")
    store = _store(warehouse, metadata_db)
    assert _seal(store, first.publication_id).record.visibility_sequence == 1

    monkeypatch.setattr(seal_module, "_MAX_SEQUENCE", 1)
    with pytest.raises(VisibilitySealError, match="sequence is exhausted"):
        _seal(store, second.publication_id)
    assert store.get_seal(second.publication_id) is None
    assert store.sequence_state().last_visibility_sequence == 1


def test_contention_rolls_back_the_seal_and_surfaces_as_bounded_failure(
    warehouse: Engine, metadata_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compare-and-set miss never leaves a seal behind and stops at the retry bound."""
    first = _commit_everything(warehouse, metadata_db, "contention-one")
    second = _commit_everything(warehouse, metadata_db, "contention-two")
    store = _store(warehouse, metadata_db, policy=VisibilityClockPolicy(sequence_attempts=1))
    assert _seal(store, first.publication_id).record.visibility_sequence == 1

    # Read the ledger as it was before the winning seal committed, so the compare-and-set
    # advance matches zero rows exactly like a losing concurrent writer sees it.
    monkeypatch.setattr(seal_module, "_locked_state", lambda _connection: (0, 0, 0))
    with pytest.raises(seal_module.VisibilitySequenceContentionError, match="no seal was written"):
        _seal(store, second.publication_id)
    assert store.get_seal(second.publication_id) is None
    assert _count(warehouse, VISIBILITY_SEALS) == 1
    assert store.sequence_state().last_visibility_sequence == 1


def test_storage_parsing_guards_reject_impossible_persisted_values() -> None:
    """The readers refuse values the seal contract can never have produced."""
    with pytest.raises(VisibilitySealError, match="non-positive"):
        seal_module._monotonic_sequence(0, "visibility_sequence")
    with pytest.raises(VisibilitySealError, match="outside the supported UTC range"):
        seal_module._us_to_datetime(-1)
    with pytest.raises(VisibilitySealError, match="must be UTC"):
        seal_module._rfc3339_utc(datetime(2026, 10, 31, 9, 0, 0))
    with pytest.raises(VisibilityCommitVisibilityError, match="lowercase SHA-256"):
        seal_module._digest("ZZ", "payload_sha256")
    assert seal_module._receipt_event_bytes(bytearray(b"{}")) == b"{}"
    assert seal_module._receipt_event_bytes("{}") == b"{}"
    with pytest.raises(VisibilityCommitVisibilityError, match="not binary"):
        seal_module._receipt_event_bytes(7)


def test_race_error_classification_separates_duplicates_from_other_failures() -> None:
    """Only a key duplicate may become a replay or retry, never a different error."""
    from sqlalchemy.exc import IntegrityError, OperationalError

    def _integrity(message: str) -> IntegrityError:
        return IntegrityError("SELECT 1", {}, Exception(message))

    def _operational(message: str) -> OperationalError:
        return OperationalError("SELECT 1", {}, Exception(message))

    assert seal_module._is_duplicate_key_error(_integrity("UNIQUE constraint failed: seals.id"))
    assert seal_module._is_duplicate_key_error(_integrity("Duplicate entry 'x' for key 'PRIMARY'"))
    assert not seal_module._is_duplicate_key_error(_integrity("CHECK constraint failed: ck"))
    assert seal_module._is_retryable_lock_error(_operational("database is locked"))
    assert seal_module._is_retryable_lock_error(_operational("Deadlock found when trying to lock"))
    assert not seal_module._is_retryable_lock_error(_operational("no such table: seals"))


# ---------------------------------------------------------------------------
# Counterfactual completion.
#
# Every guard asserted above is re-proved here in one shape: trip only the guard,
# require the refusal, remove only the tripped condition, then require the very
# same store, engines and publication to seal. A refusal that survives this pair
# is caused by the guard, not by the fixture.
# ---------------------------------------------------------------------------


def _fact_partition(engine: Engine, publication_id: str) -> list[dict[str, object]]:
    """Capture one publication's committed fact rows exactly as stored."""
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(PUBLISHED_FACT_VERSIONS).where(
                    PUBLISHED_FACT_VERSIONS.c.publication_id == publication_id
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _write_fact_partition(
    engine: Engine, publication_id: str, rows: list[dict[str, object]]
) -> None:
    """Replace one publication's fact rows with a captured partition (restore or shorten)."""
    with engine.begin() as connection:
        connection.execute(
            PUBLISHED_FACT_VERSIONS.delete().where(
                PUBLISHED_FACT_VERSIONS.c.publication_id == publication_id
            )
        )
        if rows:
            connection.execute(insert(PUBLISHED_FACT_VERSIONS), rows)


def _head_row(engine: Engine, dataset: str) -> dict[str, object]:
    """Capture one committed publication pointer row exactly as stored."""
    with engine.connect() as connection:
        return dict(
            connection.execute(
                select(PUBLICATION_HEADS).where(PUBLICATION_HEADS.c.dataset_id == dataset)
            )
            .mappings()
            .one()
        )


def _restore_head(engine: Engine, dataset: str, row: dict[str, object]) -> None:
    """Restore one captured pointer row, whether it was edited or deleted."""
    with engine.begin() as connection:
        connection.execute(
            PUBLICATION_HEADS.delete().where(PUBLICATION_HEADS.c.dataset_id == dataset)
        )
        connection.execute(insert(PUBLICATION_HEADS), row)


def _publication_column(engine: Engine, publication_id: str, name: str) -> object:
    """Read one committed publication column so a trip can be undone exactly."""
    with engine.connect() as connection:
        return connection.execute(
            select(getattr(PUBLICATIONS.c, name)).where(
                PUBLICATIONS.c.publication_id == publication_id
            )
        ).scalar_one()


def _receipt_event(engine: Engine, publication_id: str) -> tuple[bytes, str]:
    """Read one committed receipt's frozen event bytes and digest."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(
                    METADATA_PUBLICATION_RECEIPTS.c.event_json_utf8,
                    METADATA_PUBLICATION_RECEIPTS.c.event_sha256,
                ).where(METADATA_PUBLICATION_RECEIPTS.c.publication_id == publication_id)
            )
            .mappings()
            .one()
        )
    payload = row["event_json_utf8"]
    return (
        payload if isinstance(payload, bytes) else str(payload).encode("utf-8"),
        str(row["event_sha256"]),
    )


def test_incomplete_fact_partition_guard_is_the_reason_for_refusal(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """Shortening the committed partition refuses the seal; restoring it seals at once."""
    publication = _commit_publication(warehouse, "cf-facts-key", count=3)
    _commit_receipt(warehouse, metadata_db, publication.publication_id)
    partition = _fact_partition(warehouse, publication.publication_id)
    assert len(partition) == 3

    _write_fact_partition(warehouse, publication.publication_id, partition[:2])
    store = _store(warehouse, metadata_db)
    with pytest.raises(VisibilityCommitVisibilityError, match="fact partition is incomplete"):
        _seal(store, publication.publication_id)
    assert store.get_seal(publication.publication_id) is None
    assert store.sequence_state().initialised is False

    # Counterfactual: the identical request seals the moment the partition is whole again.
    _write_fact_partition(warehouse, publication.publication_id, partition)
    assert _seal(store, publication.publication_id).record.visibility_sequence == 1
    assert store.sequence_state().last_visibility_sequence == 1


def test_oversized_fact_partition_guard_is_the_reason_for_refusal(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A partition carrying more revisions than declared refuses the seal."""
    publication = _commit_everything(warehouse, metadata_db, "cf-oversize-key", count=1)
    row = _fact_partition(warehouse, publication.publication_id)[0]
    extra = {**row, "business_key": "cf-oversize-extra", "record_hash": "c" * 64}
    _write_fact_partition(warehouse, publication.publication_id, [row, extra])
    store = _store(warehouse, metadata_db)
    with pytest.raises(VisibilityCommitVisibilityError, match="more revisions"):
        _seal(store, publication.publication_id)
    assert store.get_seal(publication.publication_id) is None

    # Counterfactual: with the extra revision removed the same publication seals.
    _write_fact_partition(warehouse, publication.publication_id, [row])
    assert _seal(store, publication.publication_id).record.visibility_sequence == 1


def test_fact_partition_identity_is_enforced_by_storage_before_the_seal(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A repeated business-key revision cannot exist, so the partition guard is a backstop."""
    publication = _commit_everything(warehouse, metadata_db, "cf-dup-fact-key")
    row = _fact_partition(warehouse, publication.publication_id)[0]
    duplicate = {**row, "record_hash": "d" * 64}

    # Storage already refuses the shape the guard defends against.
    with pytest.raises(IntegrityError, match="od_published_fact_versions"):
        _write_fact_partition(warehouse, publication.publication_id, [row, duplicate])
    assert _fact_partition(warehouse, publication.publication_id) == [row]

    # Counterfactual: one row per business-key revision is a sealable partition.
    assert (
        _seal(_store(warehouse, metadata_db), publication.publication_id).record.visibility_sequence
        == 1
    )


def test_publication_pointer_guards_are_the_reason_for_refusal(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """Each pointer defect alone refuses its own publication, and each repair seals it."""
    other = _commit_publication(warehouse, "cf-pointer-other", dataset="fund.nav")
    store = _store(warehouse, metadata_db)
    rounds = (
        (
            "UPDATE od_publication_heads SET latest_sequence = 0 WHERE dataset_id = :dataset",
            "pointer has not advanced",
        ),
        (
            "DELETE FROM od_publication_heads WHERE dataset_id = :dataset",
            "pointer is not committed",
        ),
        (
            "UPDATE od_publication_heads SET latest_publication_id = :successor, "
            "latest_sequence = 99 WHERE dataset_id = :dataset",
            "successor does not match",
        ),
        (
            "UPDATE od_publication_heads SET latest_sequence = 'not-a-number' "
            "WHERE dataset_id = :dataset",
            "head latest_sequence must be an integer",
        ),
    )
    for index, (statement, message) in enumerate(rounds, start=1):
        publication = _commit_everything(warehouse, metadata_db, f"cf-pointer-round-{index}")
        head = _head_row(warehouse, "equity.daily")
        _execute(warehouse, statement, dataset="equity.daily", successor=other.publication_id)
        with pytest.raises(VisibilityCommitVisibilityError, match=message):
            _seal(store, publication.publication_id)
        assert store.get_seal(publication.publication_id) is None
        # The refusal wrote nothing: the ledger still holds exactly the previous seals.
        assert _count(warehouse, VISIBILITY_SEALS) == index - 1
        _restore_head(warehouse, "equity.daily", head)
        # Counterfactual for this round: the identical store seals the repaired pointer.
        assert _seal(store, publication.publication_id).record.visibility_sequence == index

    assert store.sequence_state().last_visibility_sequence == len(rounds)


def test_fact_binding_guard_is_the_reason_for_refusal(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A fact row re-bound to another dataset or sequence refuses; rebinding seals."""
    rounds = (
        (
            "UPDATE od_published_fact_versions SET dataset_id = 'other.daily' "
            "WHERE publication_id = :pid",
            "UPDATE od_published_fact_versions SET dataset_id = 'equity.daily' "
            "WHERE publication_id = :pid",
        ),
        (
            "UPDATE od_published_fact_versions SET publication_sequence = 77 "
            "WHERE publication_id = :pid",
            "UPDATE od_published_fact_versions SET publication_sequence = :sequence "
            "WHERE publication_id = :pid",
        ),
    )
    store = _store(warehouse, metadata_db)
    for index, (trip, repair) in enumerate(rounds, start=1):
        publication = _commit_everything(warehouse, metadata_db, f"cf-bind-key-{index}")
        _execute(warehouse, trip, pid=publication.publication_id, sequence=publication.sequence)
        with pytest.raises(
            VisibilityCommitVisibilityError, match="different dataset or publication sequence"
        ):
            _seal(store, publication.publication_id)
        assert store.get_seal(publication.publication_id) is None
        assert _count(warehouse, VISIBILITY_SEALS) == index - 1
        _execute(warehouse, repair, pid=publication.publication_id, sequence=publication.sequence)
        # Counterfactual: the identical store seals once the row is bound to this dataset.
        assert _seal(store, publication.publication_id).record.visibility_sequence == index


def test_receipt_event_guards_are_the_reason_for_refusal(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """Digest drift, body drift and row-sequence drift each refuse; each repair seals."""
    store = _store(warehouse, metadata_db)
    forged = json.dumps({"dataset_id": "equity.daily"}).encode("utf-8")
    forged_sha = hashlib.sha256(_outbox._EVENT_HASH_DOMAIN + forged).hexdigest()
    rounds = (
        (
            "UPDATE od_metadata_publication_receipts SET event_json_utf8 = :body "
            "WHERE publication_id = :pid",
            "frozen domain-separated digest",
            {"body": forged},
        ),
        (
            "UPDATE od_metadata_publication_receipts SET event_json_utf8 = :body, "
            "event_sha256 = :sha WHERE publication_id = :pid",
            "content does not match",
            {"body": forged, "sha": forged_sha},
        ),
        (
            "UPDATE od_metadata_publication_receipts SET publication_sequence = 99 "
            "WHERE publication_id = :pid",
            "receipt row sequence does not match",
            {},
        ),
    )
    for index, (statement, message, extra) in enumerate(rounds, start=1):
        publication = _commit_everything(warehouse, metadata_db, f"cf-receipt-key-{index}")
        original_bytes, original_sha = _receipt_event(metadata_db, publication.publication_id)
        _execute(metadata_db, statement, pid=publication.publication_id, **extra)
        with pytest.raises(VisibilityCommitVisibilityError, match=message):
            _seal(store, publication.publication_id)
        assert store.get_seal(publication.publication_id) is None
        assert _count(warehouse, VISIBILITY_SEALS) == index - 1
        _execute(
            metadata_db,
            "UPDATE od_metadata_publication_receipts SET event_json_utf8 = :body, "
            "event_sha256 = :sha, publication_sequence = :sequence "
            "WHERE publication_id = :pid",
            body=original_bytes,
            sha=original_sha,
            sequence=publication.sequence,
            pid=publication.publication_id,
        )
        # Counterfactual for this round: the identical store seals on the repaired receipt.
        assert _seal(store, publication.publication_id).record.visibility_sequence == index

    assert _count(warehouse, VISIBILITY_SEALS) == len(rounds)


def test_receipt_is_scoped_to_the_pinned_producer_identity(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """Another producer's committed copy is not evidence for the pinned producer."""
    publication = _commit_publication(warehouse, "cf-producer-scope-key")
    event = _committed_event(warehouse, publication.publication_id)
    with metadata_db.begin() as connection:
        receive_metadata_publication_receipt(
            connection,
            producer_identity="opendata-producer-beta",
            event_json_utf8=event["event_json"].encode("utf-8"),
            event_sha256=event["event_sha256"],
        )
    store = _store(warehouse, metadata_db)
    with pytest.raises(VisibilityCommitVisibilityError, match="receipt is not committed"):
        _seal(store, publication.publication_id)
    assert store.get_seal(publication.publication_id) is None

    # Counterfactual: exactly one copy under the pinned producer is sufficient.
    _commit_receipt(warehouse, metadata_db, publication.publication_id)
    assert _seal(store, publication.publication_id).record.visibility_sequence == 1


def _receipt_row(engine: Engine, publication_id: str) -> dict[str, object]:
    """Read one committed receipt row as a mutable copy."""
    with engine.connect() as connection:
        return dict(
            connection.execute(
                select(METADATA_PUBLICATION_RECEIPTS).where(
                    METADATA_PUBLICATION_RECEIPTS.c.publication_id == publication_id
                )
            )
            .mappings()
            .one()
        )


def test_clock_rewind_guard_is_the_reason_for_refusal(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """Only the injected rewind refuses; removing it lets the same request seal."""
    first = _commit_everything(warehouse, metadata_db, "cf-rewind-one")
    second = _commit_everything(warehouse, metadata_db, "cf-rewind-two")
    store = _store(warehouse, metadata_db)
    assert _seal(store, first.publication_id).record.visibility_sequence == 1
    _execute(
        warehouse,
        "UPDATE od_visibility_sequence_state SET last_sealed_at_us = last_sealed_at_us + :delta "
        "WHERE state_id = 1",
        delta=_TEN_YEARS_US,
    )
    with pytest.raises(VisibilityClockRewindError, match="high-water mark"):
        _seal(store, second.publication_id)
    assert store.get_seal(second.publication_id) is None
    assert store.sequence_state().last_visibility_sequence == 1

    _execute(
        warehouse,
        "UPDATE od_visibility_sequence_state SET last_sealed_at_us = last_sealed_at_us - :delta "
        "WHERE state_id = 1",
        delta=_TEN_YEARS_US,
    )
    assert _seal(store, second.publication_id).record.visibility_sequence == 2


def test_commit_instant_guard_is_the_reason_for_refusal(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A stamp far ahead of the post-commit clock refuses; restoring it seals."""
    publication = _commit_everything(warehouse, metadata_db, "cf-commit-instant-key")
    original_us = int(_publication_column(warehouse, publication.publication_id, "published_at_us"))
    _restamp(warehouse, metadata_db, publication.publication_id, original_us + 10 * _TEN_YEARS_US)
    store = _store(warehouse, metadata_db)
    with pytest.raises(VisibilityClockRewindError, match="publication commit instant"):
        _seal(store, publication.publication_id)
    assert store.get_seal(publication.publication_id) is None

    _restamp(warehouse, metadata_db, publication.publication_id, original_us)
    assert _seal(store, publication.publication_id).record.visibility_sequence == 1


def test_plausible_window_guard_is_the_reason_for_refusal(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """The window, not the publication, is what refuses; a normal window seals it."""
    publication = _commit_everything(warehouse, metadata_db, "cf-window-key")
    blocked = _store(
        warehouse,
        metadata_db,
        policy=VisibilityClockPolicy(
            min_plausible_utc=datetime(2099, 1, 1, tzinfo=timezone.utc),
            max_plausible_utc=datetime(2100, 1, 1, tzinfo=timezone.utc),
        ),
    )
    with pytest.raises(VisibilityClockUntrustedError, match="plausible UTC window"):
        _seal(blocked, publication.publication_id)
    assert blocked.get_seal(publication.publication_id) is None
    assert blocked.sequence_state().initialised is False

    # Counterfactual: the identical committed publication seals under the shipped window.
    assert _seal(_store(warehouse, metadata_db), publication.publication_id).replayed is False
    assert _seal(_store(warehouse, metadata_db), publication.publication_id).replayed is True


def test_malformed_publication_row_guards_are_the_reason_for_refusal(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """Each corrupt publication column alone refuses the seal and each repair restores it."""
    publication = _commit_everything(warehouse, metadata_db, "cf-pubrow-key")
    store = _store(warehouse, metadata_db)
    rounds = (
        (
            "schema_version",
            "UPDATE od_publications SET schema_version = '' WHERE publication_id = :pid",
            "UPDATE od_publications SET schema_version = :value WHERE publication_id = :pid",
            VisibilityCommitVisibilityError,
            "schema version is invalid",
        ),
        (
            "published_at_us",
            "UPDATE od_publications SET published_at_us = 9000000000000000000 "
            "WHERE publication_id = :pid",
            "UPDATE od_publications SET published_at_us = :value WHERE publication_id = :pid",
            VisibilityCommitVisibilityError,
            "out of the supported range",
        ),
        (
            "published_at_us",
            "UPDATE od_publications SET published_at_us = -1 WHERE publication_id = :pid",
            "UPDATE od_publications SET published_at_us = :value WHERE publication_id = :pid",
            VisibilitySealError,
            "negative published_at_us",
        ),
    )
    for column, trip, repair, error, message in rounds:
        original = _publication_column(warehouse, publication.publication_id, column)
        _execute(warehouse, trip, pid=publication.publication_id)
        with pytest.raises(error, match=message):
            _seal(store, publication.publication_id)
        assert store.get_seal(publication.publication_id) is None
        assert store.sequence_state().initialised is False
        _execute(warehouse, repair, value=original, pid=publication.publication_id)
    assert _seal(store, publication.publication_id).record.visibility_sequence == 1


def test_untrusted_host_clock_guard_is_the_reason_for_refusal(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A naive host reading refuses; the same publication seals with an aware UTC clock."""
    publication = _commit_everything(warehouse, metadata_db, "cf-hostclock-key")
    naive = _store(warehouse, metadata_db, host_clock=lambda: datetime(2026, 10, 31, 9, 0, 0))
    with pytest.raises(VisibilityClockUntrustedError, match="timezone-aware"):
        _seal(naive, publication.publication_id)
    assert naive.get_seal(publication.publication_id) is None
    assert naive.sequence_state().initialised is False

    skewed = _store(
        warehouse,
        metadata_db,
        host_clock=lambda: datetime.now(timezone.utc) + timedelta(hours=1),
    )
    with pytest.raises(VisibilityClockUntrustedError, match="disagrees"):
        _seal(skewed, publication.publication_id)
    assert skewed.get_seal(publication.publication_id) is None

    # Counterfactual: the identical committed publication seals with an aware UTC host clock.
    aware = _store(warehouse, metadata_db, host_clock=lambda: datetime.now(timezone.utc))
    record = _seal(aware, publication.publication_id).record
    assert record.visibility_sequence == 1
    assert record.clock_evidence["host_agreement"] == "agreed"


def test_compare_and_set_guard_is_the_reason_for_refusal(
    warehouse: Engine, metadata_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale ledger read refuses and writes nothing; an honest read seals."""
    first = _commit_everything(warehouse, metadata_db, "cf-cas-one")
    second = _commit_everything(warehouse, metadata_db, "cf-cas-two")
    store = _store(warehouse, metadata_db, policy=VisibilityClockPolicy(sequence_attempts=1))
    assert _seal(store, first.publication_id).record.visibility_sequence == 1

    honest = seal_module._locked_state
    monkeypatch.setattr(seal_module, "_locked_state", lambda _connection: (0, 0, 0))
    with pytest.raises(seal_module.VisibilitySequenceContentionError, match="no seal was written"):
        _seal(store, second.publication_id)
    assert store.get_seal(second.publication_id) is None
    assert _count(warehouse, VISIBILITY_SEALS) == 1

    monkeypatch.setattr(seal_module, "_locked_state", honest)
    assert _seal(store, second.publication_id).record.visibility_sequence == 2


def test_ledger_mismatch_rolls_back_a_seal_that_storage_would_accept(
    warehouse: Engine, metadata_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ledger read that no longer matches refuses the seal even when storage would take it.

    The unique sequence index alone cannot catch this case: the stale read names a number
    no seal row holds, so only the compare-and-set advance can refuse it.
    """
    first = _commit_everything(warehouse, metadata_db, "cf-cas-gap-one")
    second = _commit_everything(warehouse, metadata_db, "cf-cas-gap-two")
    store = _store(warehouse, metadata_db, policy=VisibilityClockPolicy(sequence_attempts=1))
    assert _seal(store, first.publication_id).record.visibility_sequence == 1

    honest = seal_module._locked_state
    # A ledger read naming a number no seal row holds: only the compare-and-set can refuse it.
    monkeypatch.setattr(seal_module, "_locked_state", lambda _connection: (1_000, 0, 1_000))
    with pytest.raises(seal_module.VisibilitySequenceContentionError, match="no seal was written"):
        _seal(store, second.publication_id)
    assert store.get_seal(second.publication_id) is None
    assert _count(warehouse, VISIBILITY_SEALS) == 1
    assert store.sequence_state().last_visibility_sequence == 1

    monkeypatch.setattr(seal_module, "_locked_state", honest)
    assert _seal(store, second.publication_id).record.visibility_sequence == 2


def test_exhausted_sequence_guard_is_the_reason_for_refusal(
    warehouse: Engine, metadata_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound refuses the next number; the real bound issues it."""
    first = _commit_everything(warehouse, metadata_db, "cf-exhaust-one")
    second = _commit_everything(warehouse, metadata_db, "cf-exhaust-two", dataset="fund.nav")
    store = _store(warehouse, metadata_db)
    assert _seal(store, first.publication_id).record.visibility_sequence == 1

    monkeypatch.setattr(seal_module, "_MAX_SEQUENCE", 1)
    with pytest.raises(VisibilitySealError, match="sequence is exhausted"):
        _seal(store, second.publication_id)
    assert store.get_seal(second.publication_id) is None

    monkeypatch.setattr(seal_module, "_MAX_SEQUENCE", 9_223_372_036_854_775_807)
    assert _seal(store, second.publication_id).record.visibility_sequence == 2


def test_duplicate_publication_key_is_replayed_not_renumbered(
    warehouse: Engine, metadata_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost replay race falls back to the committed seal, never a second number."""
    publication = _commit_everything(warehouse, metadata_db, "cf-dupkey-key")
    store = _store(warehouse, metadata_db)
    sealed = _seal(store, publication.publication_id)
    honest_replay = VisibilitySealStore._replay_if_sealed
    state = {"calls": 0}

    def suppress_once(self, evidence):
        state["calls"] += 1
        if state["calls"] == 1:
            return None
        return honest_replay(self, evidence)

    monkeypatch.setattr(VisibilitySealStore, "_replay_if_sealed", suppress_once)
    again = _seal(store, publication.publication_id)

    assert again.replayed is True
    assert again.record == sealed.record
    assert _count(warehouse, VISIBILITY_SEALS) == 1
    assert store.sequence_state().last_visibility_sequence == 1
    assert state["calls"] == 2


def test_crash_between_seal_commit_and_read_back_replays_the_seal(
    warehouse: Engine, metadata_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seal survives the crash window; the retry never advances the sequence again."""
    publication = _commit_everything(warehouse, metadata_db, "cf-crash-readback-key")
    store = _store(warehouse, metadata_db)
    honest = VisibilitySealStore.get_seal
    monkeypatch.setattr(VisibilitySealStore, "get_seal", lambda _self, _pid: None)
    with pytest.raises(VisibilitySealError, match="could not be read back"):
        _seal(store, publication.publication_id)

    monkeypatch.setattr(VisibilitySealStore, "get_seal", honest)
    assert _count(warehouse, VISIBILITY_SEALS) == 1
    retry = _seal(store, publication.publication_id)
    assert retry.replayed is True
    assert retry.record.visibility_sequence == 1
    assert store.sequence_state().last_visibility_sequence == 1
    assert store.sequence_state().seal_count == 1


def test_crash_after_seal_insert_before_ledger_advance_burns_nothing(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """Losing the ledger advance rolls the seal back and leaves the sequence unused."""
    publication = _commit_everything(warehouse, metadata_db, "cf-crash-ledger-key")
    _execute(
        warehouse,
        "CREATE TRIGGER fail_ledger BEFORE UPDATE ON od_visibility_sequence_state "
        "BEGIN SELECT RAISE(ABORT, 'synthetic ledger failure'); END",
    )
    store = _store(warehouse, metadata_db)
    with pytest.raises(Exception, match="synthetic ledger failure"):
        _seal(store, publication.publication_id)

    assert store.get_seal(publication.publication_id) is None
    assert _count(warehouse, VISIBILITY_SEALS) == 0
    assert store.sequence_state().initialised is False

    _execute(warehouse, "DROP TRIGGER fail_ledger")
    # Counterfactual: the same request seals at sequence 1 once the injected crash is gone.
    assert _seal(store, publication.publication_id).record.visibility_sequence == 1


def test_partial_seal_retry_after_rollback_leaves_one_number(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A retried seal after a rolled-back attempt numbers the publication exactly once."""
    publication = _commit_everything(warehouse, metadata_db, "cf-partial-retry-key")
    _execute(
        warehouse,
        "CREATE TRIGGER fail_seal_retry BEFORE INSERT ON od_publication_visibility_seals "
        "BEGIN SELECT RAISE(ABORT, 'synthetic seal failure'); END",
    )
    store = _store(warehouse, metadata_db)
    for _attempt in range(3):
        with pytest.raises(Exception, match="synthetic seal failure"):
            _seal(store, publication.publication_id)
    assert store.get_seal(publication.publication_id) is None
    assert store.sequence_state().initialised is False

    _execute(warehouse, "DROP TRIGGER fail_seal_retry")
    first = _seal(store, publication.publication_id)
    second = _seal(store, publication.publication_id)
    assert first.replayed is False and second.replayed is True
    assert first.record.visibility_sequence == 1 == second.record.visibility_sequence
    assert store.sequence_state().seal_count == 1


def test_duplicate_visibility_sequence_is_refused_by_storage(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """The unique index and the CHECK, not just the writer, forbid reuse and reversal."""
    first = _commit_everything(warehouse, metadata_db, "cf-index-one")
    second = _commit_everything(warehouse, metadata_db, "cf-index-two")
    store = _store(warehouse, metadata_db)
    sealed = _seal(store, first.publication_id).record
    assert sealed.visibility_sequence == 1
    with warehouse.connect() as connection:
        row = dict(
            connection.execute(
                select(VISIBILITY_SEALS).where(
                    VISIBILITY_SEALS.c.publication_id == first.publication_id
                )
            )
            .mappings()
            .one()
        )
    clone = {
        **row,
        "publication_id": second.publication_id,
        "dataset_id": "fund.nav",
        "publication_sequence": 1,
    }

    # The already-issued number cannot be handed to another publication.
    with pytest.raises(IntegrityError, match="visibility_sequence"):
        _write(warehouse, insert(VISIBILITY_SEALS).values(clone))
    assert store.get_seal(second.publication_id) is None

    # Counterfactual: the same row at the next fresh number is accepted by storage.
    _write(warehouse, insert(VISIBILITY_SEALS).values({**clone, "visibility_sequence": 2}))
    assert _count(warehouse, VISIBILITY_SEALS) == 2
    with warehouse.begin() as connection:
        connection.execute(
            VISIBILITY_SEALS.delete().where(
                VISIBILITY_SEALS.c.publication_id == second.publication_id
            )
        )

    # And a non-positive number, which would reverse the ledger, violates its CHECK.
    with pytest.raises(IntegrityError, match="ck_od_visibility_seal_sequence"):
        _write(warehouse, insert(VISIBILITY_SEALS).values({**clone, "visibility_sequence": 0}))
    assert store.sequence_state().last_visibility_sequence == 1
    assert _count(warehouse, VISIBILITY_SEALS) == 1


def _write(engine: Engine, statement) -> None:
    """Run one statement in its own transaction, outside the seal store."""
    with engine.begin() as connection:
        connection.execute(statement)


def test_many_concurrent_writers_never_reuse_or_reverse_a_sequence(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """Independent stores racing eight publications still produce one 1..8 ledger."""
    publications = [
        _commit_everything(warehouse, metadata_db, f"cf-bulk-{index}") for index in range(8)
    ]
    stores = [_store(warehouse, metadata_db) for _ in range(3)]

    def _run(item):
        index, publication = item
        return _seal(stores[index % len(stores)], publication.publication_id).record

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(_run, enumerate(publications)))

    issued = sorted(record.visibility_sequence for record in records)
    assert issued == list(range(1, len(publications) + 1))
    assert len({record.publication_id for record in records}) == len(publications)
    state = stores[0].sequence_state()
    assert state.last_visibility_sequence == len(publications)
    assert state.seal_count == len(publications)
    listed = [record.visibility_sequence for record in stores[0].list_seals("equity.daily")]
    assert listed == sorted(listed)
    with warehouse.connect() as connection:
        duplicates = connection.execute(
            text(
                "SELECT visibility_sequence FROM od_publication_visibility_seals "
                "GROUP BY visibility_sequence HAVING COUNT(*) > 1"
            )
        ).all()
    assert duplicates == []


def test_published_at_us_is_not_repurposed_as_the_visibility_seal(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """The seal instant is the post-commit clock; the in-transaction stamp stays untouched."""
    publication = _commit_everything(warehouse, metadata_db, "cf-nonprobative-key")
    original_us = int(_publication_column(warehouse, publication.publication_id, "published_at_us"))
    # Rewrite the non-probative field, with its receipt copy, to a clearly older stamp.
    stale_us = 1_609_459_200_000_000  # 2021-01-01T00:00:00Z
    assert stale_us < original_us
    _restamp(warehouse, metadata_db, publication.publication_id, stale_us)

    record = _seal(_store(warehouse, metadata_db), publication.publication_id).record

    assert record.sealed_at > datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert seal_module._datetime_to_us(record.sealed_at) != stale_us
    assert record.clock_evidence["database_utc_us"] == seal_module._datetime_to_us(record.sealed_at)
    assert record.clock_evidence["authoritative_clock"] == "warehouse_database_utc_clock"
    # Sealing never rewrites or adopts the in-transaction field.
    assert (
        int(_publication_column(warehouse, publication.publication_id, "published_at_us"))
        == stale_us
    )
    assert "published_at_us" not in VISIBILITY_SEALS.columns
    assert {column.name for column in VISIBILITY_SEALS.c} >= {"sealed_at_us", "visibility_sequence"}


def test_receipt_event_body_guard_rejects_malformed_bodies_and_accepts_the_true_one() -> None:
    """The frozen receipt body must still describe the committed publication exactly."""
    body = {
        "publication_id": "a" * 32,
        "dataset_id": "equity.daily",
        "publication_sequence": 1,
        "record_count": 1,
        "payload_sha256": "b" * 64,
        "published_at_us": 1_792_992_000_000_000,
    }
    arguments = {
        "publication_id": "a" * 32,
        "dataset": "equity.daily",
        "sequence": 1,
        "record_count": 1,
        "payload_sha256": "b" * 64,
        "published_at_us": 1_792_992_000_000_000,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert seal_module._verify_receipt_event_body(encoded, **arguments) is None
    with pytest.raises(VisibilityCommitVisibilityError, match="not canonical JSON"):
        seal_module._verify_receipt_event_body(b"{truncated", **arguments)
    with pytest.raises(VisibilityCommitVisibilityError, match="not an object"):
        seal_module._verify_receipt_event_body(b"[]", **arguments)
    with pytest.raises(VisibilityCommitVisibilityError, match="content does not match"):
        seal_module._verify_receipt_event_body(
            json.dumps({**body, "record_count": 9}, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            ),
            **arguments,
        )


def test_persisted_seal_values_outside_the_contract_are_refused_on_read(
    warehouse: Engine, metadata_db: Engine
) -> None:
    """A seal row edited into an impossible shape never parses as visibility."""
    publication = _commit_everything(warehouse, metadata_db, "cf-readguard-key")
    store = _store(warehouse, metadata_db)
    sealed = _seal(store, publication.publication_id).record
    rounds = (
        (
            "UPDATE od_publication_visibility_seals SET publication_sequence = 'not-a-number' "
            "WHERE publication_id = :pid",
            "UPDATE od_publication_visibility_seals SET publication_sequence = :value "
            "WHERE publication_id = :pid",
            sealed.publication_sequence,
            "publication_sequence must be an integer",
        ),
        (
            "UPDATE od_publication_visibility_seals SET record_count = 'not-a-number' "
            "WHERE publication_id = :pid",
            "UPDATE od_publication_visibility_seals SET record_count = :value "
            "WHERE publication_id = :pid",
            sealed.record_count,
            "invalid record_count",
        ),
    )
    for trip, repair, value, message in rounds:
        _execute(warehouse, trip, pid=publication.publication_id)
        with pytest.raises(VisibilitySealError, match=re.escape(message)):
            store.get_seal(publication.publication_id)
        _execute(warehouse, repair, value=value, pid=publication.publication_id)
    assert store.get_seal(publication.publication_id) == sealed
