"""Transactional publication-outbox behavior and migration acceptance tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    BigInteger,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError

from opendata.pipeline import publication_outbox as outbox
from opendata.pipeline.publication_store import (
    PUBLICATION_HEADS,
    PUBLICATION_METADATA,
    PUBLICATION_OUTBOX_EVENTS,
    PUBLICATIONS,
    PUBLISHED_FACT_VERSIONS,
    PublishedRecordInput,
    publish_in_connection,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine


_CONTRACT_SHA256 = "c" * 64
_PUBLICATION_ID = "a" * 32
_PAYLOAD_SHA256 = "b" * 64
_PUBLISHED_AT_US = 1_792_992_000_000_000
_EXPECTED_EVENT_SHA256 = "d380e093f72707f0139492fea0814b2c11f8ec8bf04ce2b6beac381277871d57"
_MIGRATION_PATH = (
    Path(__file__).parents[1] / "alembic_data" / "versions" / "20261028-0018_publication_outbox.py"
)


class _FakeTableInspector:
    """Small metadata-only inspector double for MySQL fail-closed guards."""

    def __init__(self, engines: dict[str, str | None]) -> None:
        self.engines = engines

    def has_table(self, table_name: str) -> bool:
        return table_name in self.engines

    def get_table_options(self, table_name: str) -> dict[str, str]:
        engine = self.engines.get(table_name)
        return {"mysql_engine": engine} if engine is not None else {}


class _FakeMySQLRows:
    """Mapping result returned by one fake information_schema query."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeMySQLRows:
        return self

    def all(self) -> list[dict[str, object]]:
        return self._rows


class _FakeMySQLConnection:
    """Connection double that captures the single parameterized engine query."""

    def __init__(
        self,
        *,
        engines: dict[str, str | None] | None = None,
        rows: list[dict[str, object]] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.dialect = SimpleNamespace(
            name="mysql",
            server_version_info=(8, 0, 36),
            is_mariadb=False,
        )
        self._rows = (
            rows
            if rows is not None
            else [
                {"table_name": name, "engine": engine} for name, engine in (engines or {}).items()
            ]
        )
        self.failure = failure
        self.query_count = 0
        self.statement: object | None = None
        self.parameters: dict[str, str] | None = None

    def execute(self, statement: object, parameters: dict[str, str]) -> _FakeMySQLRows:
        self.query_count += 1
        self.statement = statement
        self.parameters = parameters
        if self.failure is not None:
            raise self.failure
        return _FakeMySQLRows(self._rows)


def _load_migration():
    """Load the 0018 migration module without changing the Alembic chain."""
    spec = importlib.util.spec_from_file_location("test_0018_publication_outbox", _MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load migration 0018")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Create a disposable SQLite database with runtime publication metadata."""
    database = create_engine(f"sqlite:///{tmp_path / 'publication-outbox.sqlite'}")
    PUBLICATION_METADATA.create_all(database)
    try:
        yield database
    finally:
        database.dispose()


def _publish(
    connection,
    idempotency_key: str,
    provenance: dict[str, object],
    watermark: dict[str, object] | None = None,
):
    """Publish one deterministic synthetic fact through the real writer."""
    return publish_in_connection(
        connection,
        dataset_id="equity.daily",
        source_id="provider.alpha",
        schema_version="2",
        semantic_version="2.1",
        unit="CNY/share",
        adjustment_basis="unadjusted",
        quality={"status": "passed"},
        provenance=provenance,
        idempotency_key=idempotency_key,
        records=[
            PublishedRecordInput(
                business_key="600000.SH:2026-10-27",
                payload={"symbol": "600000.SH", "close": 12.5},
                business_date=date(2026, 10, 27),
            )
        ],
        watermark=watermark,
    )


def _count(connection, table: Table) -> int:
    """Count rows in one runtime table using the supplied connection."""
    return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def _outbox_publication_row(provenance_json: object) -> dict[str, object]:
    """Build persisted publication fields for focused freeze-validation tests."""
    return {
        "publication_id": _PUBLICATION_ID,
        "dataset_id": "equity.daily",
        "sequence": 7,
        "record_count": 1,
        "payload_hash": _PAYLOAD_SHA256,
        "provenance_json": provenance_json,
        "schema_version": "2",
        "published_at_us": _PUBLISHED_AT_US,
    }


def test_new_publication_writes_event_with_unverified_contract_and_provenance_hash(
    engine: Engine,
) -> None:
    """The outbox freezes contract evidence honestly in the new-row transaction."""
    provenance = {"contract_sha256": _CONTRACT_SHA256, "provider": "alpha"}
    with engine.begin() as connection:
        publication = _publish(connection, "new-with-contract", provenance)
        event = (
            connection.execute(
                select(PUBLICATION_OUTBOX_EVENTS).where(
                    PUBLICATION_OUTBOX_EVENTS.c.publication_id == publication.publication_id
                )
            )
            .mappings()
            .one()
        )
        assert event["dataset_id"] == publication.dataset_id
        assert event["publication_sequence"] == publication.sequence
        assert event["record_count"] == publication.record_count
        assert event["payload_sha256"] == publication.payload_hash
        assert event["schema_version"] == publication.schema_version
        assert event["contract_sha256"] == _CONTRACT_SHA256
        assert event["contract_evidence_kind"] == "caller_supplied_unverified"
        assert (
            event["provenance_sha256"]
            == hashlib.sha256(
                json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        )
        assert json.loads(event["event_json"])["contract_evidence_kind"] == (
            "caller_supplied_unverified"
        )
        assert (
            event["event_sha256"]
            == hashlib.sha256(
                b"opendata.publication-outbox.event.v1\n" + event["event_json"].encode("utf-8")
            ).hexdigest()
        )


def test_missing_contract_hash_is_nullable_and_never_synthesized(engine: Engine) -> None:
    """A generic publication emits explicit absent evidence, not a fake digest."""
    provenance = {"provider": "alpha"}
    with engine.begin() as connection:
        publication = _publish(connection, "new-without-contract", provenance)
        event = (
            connection.execute(
                select(PUBLICATION_OUTBOX_EVENTS).where(
                    PUBLICATION_OUTBOX_EVENTS.c.publication_id == publication.publication_id
                )
            )
            .mappings()
            .one()
        )
    body = json.loads(event["event_json"])
    assert event["contract_sha256"] is None
    assert event["contract_evidence_kind"] == "absent"
    assert event["provenance_sha256"] == hashlib.sha256(b'{"provider":"alpha"}').hexdigest()
    assert body["contract_sha256"] is None
    assert body["contract_evidence_kind"] == "absent"


def test_exact_publication_retry_does_not_repair_a_missing_historical_event(
    engine: Engine,
) -> None:
    """An idempotency hit returns the old publication without creating an event."""
    provenance = {"provider": "alpha"}
    with engine.begin() as connection:
        first = _publish(connection, "replay-key", provenance)
        connection.execute(
            PUBLICATION_OUTBOX_EVENTS.delete().where(
                PUBLICATION_OUTBOX_EVENTS.c.publication_id == first.publication_id
            )
        )

    with engine.begin() as connection:
        replay = _publish(connection, "replay-key", provenance)
        assert replay.publication_id == first.publication_id
        assert _count(connection, PUBLICATION_OUTBOX_EVENTS) == 0


def test_outer_rollback_removes_new_publication_facts_head_update_and_event(engine: Engine) -> None:
    """The event shares the caller transaction and rollback preserves the prior head."""
    with engine.begin() as connection:
        baseline = _publish(
            connection,
            "rollback-baseline",
            {"provider": "alpha"},
            watermark={"business_date": "2026-10-27"},
        )

    with engine.connect() as connection:
        transaction = connection.begin()
        _publish(
            connection,
            "rollback-key",
            {"provider": "alpha"},
            watermark={"business_date": "2026-10-28"},
        )
        assert _count(connection, PUBLICATION_OUTBOX_EVENTS) == 2
        transaction.rollback()

    with engine.connect() as connection:
        assert _count(connection, PUBLICATIONS) == 1
        assert _count(connection, PUBLISHED_FACT_VERSIONS) == 1
        assert _count(connection, PUBLICATION_OUTBOX_EVENTS) == 1
        head = (
            connection.execute(
                select(PUBLICATION_HEADS).where(PUBLICATION_HEADS.c.dataset_id == "equity.daily")
            )
            .mappings()
            .one()
        )
        assert head["latest_sequence"] == baseline.sequence
        assert head["latest_publication_id"] == baseline.publication_id
        assert head["watermark_json"] == '{"business_date":"2026-10-27"}'


def test_outbox_insert_failure_rolls_back_the_new_publication(engine: Engine) -> None:
    """A failed event insert restores facts, publication, and head to prior values."""
    with engine.begin() as connection:
        baseline = _publish(
            connection,
            "event-failure-baseline",
            {"provider": "alpha"},
            watermark={"business_date": "2026-10-27"},
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_outbox_insert BEFORE INSERT ON od_publication_outbox "
            "BEGIN SELECT RAISE(ABORT, 'synthetic outbox failure'); END"
        )
    with (
        pytest.raises(IntegrityError, match="synthetic outbox failure"),
        engine.begin() as connection,
    ):
        _publish(
            connection,
            "event-failure-key",
            {"provider": "alpha"},
            watermark={"business_date": "2026-10-28"},
        )

    with engine.connect() as connection:
        assert _count(connection, PUBLICATIONS) == 1
        assert _count(connection, PUBLISHED_FACT_VERSIONS) == 1
        assert _count(connection, PUBLICATION_OUTBOX_EVENTS) == 1
        head = (
            connection.execute(
                select(PUBLICATION_HEADS).where(PUBLICATION_HEADS.c.dataset_id == "equity.daily")
            )
            .mappings()
            .one()
        )
        assert head["latest_sequence"] == baseline.sequence
        assert head["latest_publication_id"] == baseline.publication_id
        assert head["watermark_json"] == '{"business_date":"2026-10-27"}'


def test_invalid_caller_contract_digest_rolls_back_publication(engine: Engine) -> None:
    """Malformed supplied evidence fails before a false contract value is persisted."""
    with pytest.raises(outbox._PublicationOutboxSourceError), engine.begin() as connection:
        _publish(connection, "invalid-contract", {"contract_sha256": "not-a-digest"})
    with engine.connect() as connection:
        assert _count(connection, PUBLICATIONS) == 0
        assert _count(connection, PUBLICATION_OUTBOX_EVENTS) == 0


@pytest.mark.parametrize(
    ("provenance_json", "message"),
    [
        (None, "canonical JSON text"),
        ("x" * 262_145, "exceeds its bound"),
        ('{"provider": "synthetic"}', "publication provenance is invalid"),
    ],
)
def test_publication_row_freeze_rejects_invalid_persisted_provenance(
    provenance_json: object,
    message: str,
) -> None:
    """The event boundary rejects malformed or oversized persisted lineage."""
    with pytest.raises(outbox._PublicationOutboxSourceError, match=message):
        outbox._candidate_from_publication_row(_outbox_publication_row(provenance_json))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("publication_id", "A" * 32, "publication_id"),
        ("dataset_id", " ", "dataset_id"),
        ("sequence", 0, "sequence"),
        ("record_count", -1, "record_count"),
        ("payload_sha256", "g" * 64, "payload_sha256"),
        ("contract_sha256", "g" * 64, "contract_sha256"),
        ("contract_evidence_kind", "caller_supplied_unverified", "contract evidence kind"),
        ("schema_version", " ", "schema_version"),
        ("published_at_us", -1, "published_at_us"),
    ],
)
def test_frozen_candidate_rejects_invalid_identity_and_bounds(
    field: str,
    value: object,
    message: str,
) -> None:
    """Every event key and contract-bound field is validated before hashing."""
    candidate = outbox._candidate_from_publication_row(
        _outbox_publication_row('{"provider":"synthetic"}')
    )
    malformed = replace(candidate, **{field: value})
    with pytest.raises((TypeError, ValueError), match=message):
        outbox._normalize_candidate(malformed)


def test_frozen_candidate_rejects_non_candidate_input() -> None:
    """Internal event builders reject objects that bypass the candidate type."""
    with pytest.raises(TypeError, match="candidate must be"):
        outbox._normalize_candidate(object())


def test_publication_event_enforces_canonical_envelope_byte_limit(monkeypatch) -> None:
    """A frozen envelope is bounded even when its individual fields are valid."""
    candidate = outbox._candidate_from_publication_row(
        _outbox_publication_row('{"provider":"synthetic"}')
    )
    monkeypatch.setattr(outbox, "_MAX_EVENT_BYTES", 64)
    with pytest.raises(ValueError, match="byte limit"):
        outbox._build_publication_event(candidate)


def test_event_has_a_stable_canonical_golden_hash() -> None:
    """The event envelope and domain-separated hash are a fixed wire contract."""
    provenance_json = '{"provider":"synthetic"}'
    candidate = outbox._candidate_from_publication_row(
        {
            "publication_id": _PUBLICATION_ID,
            "dataset_id": "equity.daily",
            "sequence": 7,
            "record_count": 1,
            "payload_hash": _PAYLOAD_SHA256,
            "provenance_json": provenance_json,
            "schema_version": "2",
            "published_at_us": _PUBLISHED_AT_US,
        }
    )
    event = outbox._build_publication_event(candidate)
    expected_json = (
        '{"contract_evidence_kind":"absent","contract_sha256":null,'
        '"dataset_id":"equity.daily","event_type":"opendata.publication.published",'
        '"event_version":1,"payload_sha256":"' + _PAYLOAD_SHA256 + '",'
        '"provenance_sha256":"'
        + hashlib.sha256(provenance_json.encode("utf-8")).hexdigest()
        + '","publication_id":"'
        + _PUBLICATION_ID
        + '","publication_sequence":7,"published_at_us":1792992000000000,'
        '"record_count":1,"schema_version":"2"}'
    )
    assert event.event_json == expected_json
    assert event.event_sha256 == _EXPECTED_EVENT_SHA256
    assert (
        hashlib.sha256(
            b"opendata.publication-outbox.event.v1\n" + event.event_json.encode("utf-8")
        ).hexdigest()
        == _EXPECTED_EVENT_SHA256
    )


def test_exact_event_replay_is_accepted_and_changed_identity_fails() -> None:
    """A duplicate event identity accepts only the byte-exact frozen envelope."""
    candidate = outbox._candidate_from_publication_row(
        {
            "publication_id": _PUBLICATION_ID,
            "dataset_id": "equity.daily",
            "sequence": 7,
            "record_count": 1,
            "payload_hash": _PAYLOAD_SHA256,
            "provenance_json": '{"provider":"synthetic"}',
            "schema_version": "2",
            "published_at_us": _PUBLISHED_AT_US,
        }
    )
    event = outbox._build_publication_event(candidate)
    row = outbox._event_values(event)
    outbox._require_exact_event_replay(row, event)

    changed_identity = outbox._build_publication_event(
        replace(candidate, dataset_id="equity.weekly")
    )
    with pytest.raises(outbox._PublicationOutboxConflictError, match="different frozen"):
        outbox._require_exact_event_replay(row, changed_identity)

    corrupted = dict(row)
    corrupted["event_json"] = event.event_json.replace("equity.daily", "equity.weekly")
    with pytest.raises(outbox._PublicationOutboxConflictError, match="integrity"):
        outbox._require_exact_event_replay(corrupted, event)


def test_event_replay_rejects_non_text_canonical_columns() -> None:
    """Stored event JSON and hash must retain their text wire representation."""
    candidate = outbox._candidate_from_publication_row(
        _outbox_publication_row('{"provider":"synthetic"}')
    )
    event = outbox._build_publication_event(candidate)
    malformed = outbox._event_values(event)
    malformed["event_json"] = None
    with pytest.raises(outbox._PublicationOutboxConflictError, match="integrity"):
        outbox._require_exact_event_replay(malformed, event)


def test_existing_event_lookup_rejects_multiple_matching_rows() -> None:
    """A publication and its dataset sequence cannot resolve to two event rows."""
    candidate = outbox._candidate_from_publication_row(
        _outbox_publication_row('{"provider":"synthetic"}')
    )
    expected = outbox._build_publication_event(candidate)

    class TwoRows:
        def mappings(self) -> TwoRows:
            return self

        def all(self) -> list[dict[str, object]]:
            return [
                {
                    "publication_id": expected.publication_id,
                    "dataset_id": "equity.other",
                    "publication_sequence": expected.sequence + 1,
                },
                {
                    "publication_id": "f" * 32,
                    "dataset_id": expected.dataset_id,
                    "publication_sequence": expected.sequence,
                },
            ]

    connection = SimpleNamespace(execute=lambda _statement: TwoRows())
    with pytest.raises(outbox._PublicationOutboxConflictError, match="different outbox events"):
        outbox._find_existing_event(connection, PUBLICATION_OUTBOX_EVENTS, expected)


def _migration_schema() -> tuple[MetaData, Table, Table]:
    """Build only the legacy parent and version tables required by migration 0018."""
    metadata = MetaData()
    publications = Table(
        "od_publications",
        metadata,
        Column("publication_id", String(32), primary_key=True, nullable=False),
        Column("dataset_id", String(128), nullable=False),
        Column("source_id", String(128), nullable=False),
        Column("sequence", BigInteger, nullable=False),
        Column("schema_version", String(64), nullable=False),
        Column("semantic_version", String(64), nullable=False),
        Column("unit", String(128), nullable=False),
        Column("adjustment_basis", String(128), nullable=False),
        Column("quality_json", Text, nullable=False),
        Column("provenance_json", Text, nullable=False),
        Column("watermark_json", Text, nullable=True),
        Column("idempotency_key", String(256), nullable=False),
        Column("payload_hash", String(64), nullable=False),
        Column("record_count", Integer, nullable=False),
        Column("published_at_us", BigInteger, nullable=False),
    )
    version = Table(
        "alembic_version_data",
        metadata,
        Column("version_num", String(64), primary_key=True, nullable=False),
    )
    return metadata, publications, version


@pytest.fixture
def migration_engine(tmp_path: Path) -> Iterator[tuple[Engine, Table]]:
    """Create a disposable 0017-like schema with one historical publication."""
    database = create_engine(f"sqlite:///{tmp_path / 'outbox-migration.sqlite'}")
    metadata, publications, version = _migration_schema()
    metadata.create_all(database)
    with database.begin() as connection:
        connection.execute(
            insert(publications).values(
                publication_id=_PUBLICATION_ID,
                dataset_id="equity.daily",
                source_id="synthetic.provider",
                sequence=7,
                schema_version="2",
                semantic_version="1",
                unit="CNY/share",
                adjustment_basis="unadjusted",
                quality_json="{}",
                provenance_json='{"provider":"synthetic"}',
                watermark_json=None,
                idempotency_key="historical:one",
                payload_hash=_PAYLOAD_SHA256,
                record_count=1,
                published_at_us=_PUBLISHED_AT_US,
            )
        )
        connection.execute(insert(version).values(version_num="0017_sina_capture_discovery_index"))
    try:
        yield database, publications
    finally:
        database.dispose()


def _run_migration(connection, function) -> None:
    """Run one migration function with the configured warehouse marker name."""
    with Operations.context(
        MigrationContext.configure(
            connection,
            opts={"version_table": "alembic_version_data"},
        )
    ):
        function()


def test_0018_adds_empty_outbox_without_backfilling_old_publications(migration_engine) -> None:
    """Migration creates event storage but leaves a historical publication untouched."""
    database, _ = migration_engine
    migration = _load_migration()
    with database.begin() as connection:
        _run_migration(connection, migration.upgrade)
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_publication_outbox")).scalar_one() == 0
        )
        assert connection.execute(text("SELECT COUNT(*) FROM od_publications")).scalar_one() == 1


def test_0018_downgrade_refuses_to_drop_persisted_events(migration_engine) -> None:
    """A non-empty outbox is durable evidence and blocks downgrade."""
    database, _ = migration_engine
    migration = _load_migration()
    with database.begin() as connection:
        _run_migration(connection, migration.upgrade)
        connection.execute(
            text("UPDATE alembic_version_data SET version_num = '0018_publication_outbox'")
        )
        connection.execute(
            text(
                "INSERT INTO od_publication_outbox "
                "(publication_id, dataset_id, publication_sequence, record_count, "
                "payload_sha256, contract_sha256, contract_evidence_kind, provenance_sha256, "
                "schema_version, event_version, event_json, event_sha256, published_at_us) "
                "VALUES (:id, :dataset, :sequence, :count, :payload, NULL, 'absent', :provenance, "
                ":schema, 1, '{}', :event_sha, :published_at)"
            ),
            {
                "id": _PUBLICATION_ID,
                "dataset": "equity.daily",
                "sequence": 7,
                "count": 1,
                "payload": _PAYLOAD_SHA256,
                "provenance": "d" * 64,
                "schema": "2",
                "event_sha": "e" * 64,
                "published_at": _PUBLISHED_AT_US,
            },
        )
        with pytest.raises(RuntimeError, match="refuses to discard"):
            _run_migration(connection, migration.downgrade)
        assert inspect(connection).has_table("od_publication_outbox")


def test_0018_empty_downgrade_drops_only_the_outbox_table(migration_engine) -> None:
    """An exact 0018 marker allows downgrade only when the new table is empty."""
    database, _ = migration_engine
    migration = _load_migration()
    with database.begin() as connection:
        _run_migration(connection, migration.upgrade)
        connection.execute(
            text("UPDATE alembic_version_data SET version_num = '0018_publication_outbox'")
        )
        _run_migration(connection, migration.downgrade)
        assert not inspect(connection).has_table("od_publication_outbox")
        assert inspect(connection).has_table("od_publications")


@pytest.mark.parametrize(
    ("name", "version", "is_mariadb", "supported"),
    [
        ("sqlite", None, False, True),
        ("mysql", (8, 0, 16), False, True),
        ("mysql", (8, 4, 0), False, True),
        ("mysql", (8, 0, 15), False, False),
        ("mysql", (9, 0, 0), False, False),
        ("mysql", (10, 11, 0), True, False),
        ("postgresql", (16, 0), False, False),
    ],
)
def test_runtime_dialect_guard(name, version, is_mariadb, supported) -> None:
    """The write path has a fixed SQLite and MySQL 8.0.16+ support boundary."""
    mysql_connection = None
    if name == "mysql" and supported:
        table_names = outbox._MYSQL_TRANSACTIONAL_TABLES
        mysql_connection = _FakeMySQLConnection(engines=dict.fromkeys(table_names, "InnoDB"))
        connection = mysql_connection
    else:
        connection = SimpleNamespace(
            dialect=SimpleNamespace(
                name=name,
                server_version_info=version,
                is_mariadb=is_mariadb,
            )
        )
    if supported:
        outbox._require_supported_connection(connection)
        if mysql_connection is not None:
            assert mysql_connection.query_count == 1
            assert "information_schema.TABLES" in str(mysql_connection.statement)
            assert mysql_connection.parameters == {
                "publication_heads": "od_publication_heads",
                "publications": "od_publications",
                "fact_versions": "od_published_fact_versions",
                "outbox": "od_publication_outbox",
            }
    else:
        with pytest.raises(outbox._PublicationOutboxError):
            outbox._require_supported_connection(connection)


@pytest.mark.parametrize(
    "table_name",
    [
        "od_publication_heads",
        "od_publications",
        "od_published_fact_versions",
        "od_publication_outbox",
    ],
)
def test_mysql_runtime_engine_guard_rejects_non_innodb_tables(table_name) -> None:
    """Every table mutated by publication must be proven transactional."""
    engines = dict.fromkeys(outbox._MYSQL_TRANSACTIONAL_TABLES, "InnoDB")
    engines[table_name] = "MyISAM"
    connection = _FakeMySQLConnection(engines=engines)
    with pytest.raises(outbox._PublicationOutboxError, match=table_name):
        outbox._require_supported_connection(connection)
    assert connection.query_count == 1


def test_mysql_runtime_engine_guard_fails_closed_for_missing_table() -> None:
    """A missing table is not assumed to be InnoDB."""
    engines = dict.fromkeys(outbox._MYSQL_TRANSACTIONAL_TABLES, "InnoDB")
    del engines["od_publication_heads"]
    connection = _FakeMySQLConnection(engines=engines)
    with pytest.raises(outbox._PublicationOutboxError, match="all four MySQL transaction tables"):
        outbox._require_supported_connection(connection)
    assert connection.query_count == 1


def test_mysql_runtime_engine_guard_rejects_duplicate_metadata_rows() -> None:
    """The metadata result must have exactly one row for each expected name."""
    rows = [{"table_name": name, "engine": "InnoDB"} for name in outbox._MYSQL_TRANSACTIONAL_TABLES]
    rows.append(rows[0].copy())
    connection = _FakeMySQLConnection(rows=rows)
    with pytest.raises(outbox._PublicationOutboxError, match="duplicate"):
        outbox._require_supported_connection(connection)
    assert connection.query_count == 1


def test_mysql_runtime_engine_guard_fails_closed_on_query_error() -> None:
    """Metadata lookup failure prevents any publication write."""
    connection = _FakeMySQLConnection(failure=RuntimeError("metadata denied"))
    with pytest.raises(outbox._PublicationOutboxError, match="refusing writes"):
        outbox._require_supported_connection(connection)
    assert connection.query_count == 1


def test_mysql_runtime_table_metadata_declares_innodb() -> None:
    """Fresh metadata-created publication tables match migration 0018's engine."""
    for table in (
        PUBLICATION_HEADS,
        PUBLICATIONS,
        PUBLISHED_FACT_VERSIONS,
        PUBLICATION_OUTBOX_EVENTS,
    ):
        assert table.dialect_options["mysql"]["engine"] == "InnoDB"


@pytest.mark.parametrize(
    "table_name",
    ["od_publication_heads", "od_publications", "od_published_fact_versions"],
)
def test_0018_migration_guard_rejects_non_innodb_parent(table_name: str) -> None:
    """Migration never alters parent engines and refuses unsafe transaction tables."""
    migration = _load_migration()
    engines = dict.fromkeys(migration._TRANSACTIONAL_PARENT_TABLES, "InnoDB")
    engines[table_name] = "MyISAM"
    with pytest.raises(RuntimeError, match=table_name):
        migration._require_innodb_tables(
            _FakeTableInspector(engines), migration._TRANSACTIONAL_PARENT_TABLES
        )


def test_outbox_module_has_no_standalone_append_api() -> None:
    """Only the publisher's new-publication branch may perform event insertion."""
    assert outbox.__all__ == []
    assert not hasattr(outbox, "append_publication_event")
    assert not hasattr(outbox, "_append_publication_event_candidate")
