"""Warehouse revision 0022 seal-storage migration behaviour and chain tests."""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    BigInteger,
    Column,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    inspect,
    text,
)

from opendata.pipeline.visibility_seal import (
    VISIBILITY_SEALS,
    VISIBILITY_SEQUENCE_STATE,
    make_visibility_seal_tables,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from io import StringIO

    from sqlalchemy.engine import Engine

_REVISION = "0022_publication_visibility_seal"
_PARENT_REVISION = "0021_temporal_identity_snapshot"
_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic_data"
    / "versions"
    / "20261031-0022_publication_visibility_seal.py"
)
_SEALS = "od_publication_visibility_seals"
_STATE = "od_visibility_sequence_state"


def _load_migration():
    """Load the 0022 migration module without changing the Alembic chain."""
    spec = importlib.util.spec_from_file_location(
        "test_0022_publication_visibility_seal", _MIGRATION_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load migration 0022")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pre_0022_schema() -> tuple[MetaData, Table, Table]:
    """Build the parent schema 0022 inspects, deliberately without any seal storage."""
    metadata = MetaData()
    publications = Table(
        "od_publications",
        metadata,
        Column("publication_id", String(32), primary_key=True, nullable=False),
        Column("dataset_id", String(128), nullable=False),
        Column("sequence", BigInteger, nullable=False),
        Column("schema_version", String(64), nullable=False),
        Column("published_at_us", BigInteger, nullable=False),
    )
    version = Table(
        "alembic_version_data",
        metadata,
        Column("version_num", String(64), primary_key=True, nullable=False),
    )
    return metadata, publications, version


@pytest.fixture
def migration_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provide a disposable 0021 warehouse schema carrying one historical publication."""
    database = create_engine(f"sqlite:///{tmp_path / 'seal-migration.sqlite'}")
    metadata, publications, version = _pre_0022_schema()
    metadata.create_all(database)
    with database.begin() as connection:
        connection.execute(
            publications.insert().values(
                publication_id="a" * 32,
                dataset_id="equity.daily",
                sequence=1,
                schema_version="2",
                published_at_us=1_792_992_000_000_000,
            )
        )
        connection.execute(version.insert().values(version_num="0021_temporal_identity_snapshot"))
    try:
        yield database
    finally:
        database.dispose()


def _count_seals(connection) -> int:
    """Count rows in the newly created seal table."""
    return int(
        connection.execute(
            text("SELECT COUNT(*) FROM od_publication_visibility_seals")
        ).scalar_one()
    )


def _count_state(connection) -> int:
    """Count rows in the newly created visibility sequence ledger."""
    return int(
        connection.execute(text("SELECT COUNT(*) FROM od_visibility_sequence_state")).scalar_one()
    )


def _run(connection, function) -> None:
    """Run one migration function under the configured warehouse marker name."""
    with Operations.context(
        MigrationContext.configure(
            connection,
            opts={"version_table": "alembic_version_data"},
        )
    ):
        function()


def _alembic_data_revisions() -> list[tuple[str, tuple[str, ...]]]:
    """Return ``(revision, parent revisions)`` for every warehouse migration module."""
    versions = Path(__file__).parents[1] / "alembic_data" / "versions"
    entries: list[tuple[str, tuple[str, ...]]] = []
    for path in sorted(versions.glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"chain_{path.stem}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        down = getattr(module, "down_revision", None)
        parents = () if down is None else (down if isinstance(down, tuple) else (down,))
        entries.append((str(module.revision), tuple(str(item) for item in parents)))
    return entries


def test_revision_0022_is_the_single_warehouse_head() -> None:
    """The new head chains from 0021 and leaves exactly one head in the graph."""
    entries = _alembic_data_revisions()
    revisions = {revision for revision, _ in entries}
    referenced = {parent for _, parents in entries for parent in parents}
    heads = sorted(revisions - referenced)

    assert len(entries) == len(revisions), "revision ids must be unique"
    assert heads == [_REVISION], "a second warehouse head would fork the migration chain"
    assert (_REVISION, ("0021_temporal_identity_snapshot",)) in entries
    # Nothing else may hang off 0021, or 0022 would be a branch rather than the advance.
    assert [revision for revision, parents in entries if parents == (_PARENT_REVISION,)] == [
        _REVISION
    ]
    # Every non-head revision is referenced exactly once, so the chain is linear.
    parent_counts = Counter(parent for _, parents in entries for parent in parents)
    assert sorted(parent_counts.values()) == [1] * len(parent_counts)
    assert parent_counts[_PARENT_REVISION] == 1
    assert revisions - referenced == {_REVISION}


def test_revision_0022_chains_only_from_the_previous_head() -> None:
    """The 0022 module names its parent as a single string, never a merge tuple."""
    migration = _load_migration()
    assert migration.revision == _REVISION
    assert migration.down_revision == _PARENT_REVISION
    assert migration.branch_labels is None
    assert migration.depends_on is None


def test_upgrade_creates_seal_storage_without_marking_any_publication_visible(
    migration_engine: Engine,
) -> None:
    """Revision 0022 adds empty storage; historical publications are never sealed."""
    migration = _load_migration()
    with migration_engine.begin() as connection:
        _run(connection, migration.upgrade)
        assert _count_seals(connection) == 0
        assert _count_state(connection) == 0
        assert connection.execute(text("SELECT COUNT(*) FROM od_publications")).scalar_one() == 1


def test_upgrade_verifies_the_expected_marker(migration_engine: Engine) -> None:
    """A warehouse at the wrong marker is left untouched rather than chained anyway."""
    migration = _load_migration()
    with migration_engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version_data SET version_num = '0020_run_ledger_claim_policy'")
        )
    with (
        migration_engine.connect() as connection,
        pytest.raises(RuntimeError, match="requires exactly one Alembic marker"),
    ):
        _run(connection, migration.upgrade)
    assert not inspect(migration_engine).has_table(_SEALS)


def test_upgrade_recovers_a_crash_between_the_two_create_statements(
    migration_engine: Engine,
) -> None:
    """A completed first table is verified, and only the missing ledger is created."""
    migration = _load_migration()
    with migration_engine.begin() as connection:
        _run(connection, migration._create_seals)
    assert inspect(migration_engine).has_table(_SEALS)
    assert not inspect(migration_engine).has_table(_STATE)

    with migration_engine.begin() as connection:
        _run(connection, migration.upgrade)
    assert inspect(migration_engine).has_table(_STATE)


def test_upgrade_refuses_a_pre_existing_table_of_a_different_shape(
    migration_engine: Engine,
) -> None:
    """An unknown seal table is left for offline inspection instead of being adopted."""
    migration = _load_migration()
    with migration_engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE od_publication_visibility_seals (publication_id VARCHAR(32))")
        )
    with (
        migration_engine.connect() as connection,
        pytest.raises(RuntimeError, match="unexpected.*column set"),
    ):
        _run(connection, migration.upgrade)


def test_downgrade_is_refused_because_a_dropped_ledger_can_reissue_a_sequence() -> None:
    """Removing monotonic visibility state is not an automatic rollback."""
    migration = _load_migration()
    with pytest.raises(RuntimeError, match="can re-issue a sequence value"):
        migration.downgrade()


def test_migration_declarations_match_the_runtime_tables() -> None:
    """The revision and the runtime model describe one column set and one ledger."""
    assert tuple(column.name for column in VISIBILITY_SEALS.columns) == _MIGRATION_SEAL_COLUMNS
    assert {constraint.name for constraint in VISIBILITY_SEALS.constraints if constraint.name} == (
        set(_load_migration()._SEAL_CHECKS) | {"fk_od_visibility_seal_publication"}
    )
    assert tuple(column.name for column in VISIBILITY_SEQUENCE_STATE.columns) == (
        _load_migration()._STATE_COLUMNS
    )
    assert {
        constraint.name for constraint in VISIBILITY_SEQUENCE_STATE.constraints if constraint.name
    } == (set(_load_migration()._STATE_CHECKS))


def test_seal_tables_are_registered_once_per_metadata_registry() -> None:
    """The factory is idempotent and its publication foreign key resolves in the registry."""
    metadata = MetaData()
    Table("od_publications", metadata, Column("publication_id", String(32), primary_key=True))
    seals, state = make_visibility_seal_tables(metadata)
    assert make_visibility_seal_tables(metadata) == (seals, state)
    assert {element.target_fullname for element in seals.foreign_keys} == {
        "od_publications.publication_id"
    }
    assert {element.ondelete for element in seals.foreign_keys} == {"RESTRICT"}
    assert state.name == "od_visibility_sequence_state"
    # The runtime pair is declared inside the publication registry itself.
    runtime_seals, runtime_state = make_visibility_seal_tables(VISIBILITY_SEALS.metadata)
    assert (runtime_seals, runtime_state) == (VISIBILITY_SEALS, VISIBILITY_SEQUENCE_STATE)


_MIGRATION_SEAL_COLUMNS = (
    "publication_id",
    "dataset_id",
    "publication_sequence",
    "record_count",
    "schema_version",
    "producer_identity",
    "payload_sha256",
    "fact_set_sha256",
    "receipt_event_sha256",
    "visibility_sequence",
    "sealed_at_us",
    "clock_evidence_json",
    "seal_version",
)


def test_offline_render_declares_both_tables_without_a_live_read() -> None:
    """Revision 0022 renders fixed DDL offline, so it never fabricates an inspection."""
    from io import StringIO

    from opendata.core.config import settings

    migration = _load_migration()
    buffer: StringIO = StringIO()
    context = MigrationContext.configure(
        url=settings.data_database_url,
        opts={
            "as_sql": True,
            "output_buffer": buffer,
            "version_table": "alembic_version_data",
            "literal_binds": True,
            "dialect_opts": {"paramstyle": "named"},
        },
    )
    with Operations.context(context):
        migration.upgrade()
    sql = buffer.getvalue()

    assert "CREATE TABLE od_publication_visibility_seals (" in sql
    assert "CREATE TABLE od_visibility_sequence_state (" in sql
    assert "CREATE UNIQUE INDEX uq_od_visibility_seal_sequence" in sql
    assert "CREATE UNIQUE INDEX uq_od_visibility_seal_dataset_sequence" in sql
    assert (
        "CONSTRAINT fk_od_visibility_seal_publication FOREIGN KEY(publication_id) "
        "REFERENCES od_publications (publication_id)" in sql
    )
    assert "ON DELETE RESTRICT" in sql


def test_unsupported_live_target_is_refused() -> None:
    """A non-SQLite, non-MySQL warehouse dialect never gets the seal schema."""
    from sqlalchemy import create_mock_engine

    migration = _load_migration()
    rendered: list[str] = []

    def _dump(sql, *arguments, **kwargs) -> None:
        rendered.append(str(sql.compile(dialect=engine.dialect)))

    engine = create_mock_engine("postgresql://", _dump)
    with (
        Operations.context(
            MigrationContext.configure(engine, opts={"version_table": "alembic_version_data"})
        ),
        pytest.raises(RuntimeError, match="SQLite and MySQL 8.0.16"),
    ):
        migration.upgrade()
    assert rendered == []


def test_clock_evidence_column_uses_the_reviewed_storage_types() -> None:
    """The bounded JSON column survives MySQL's 64 KiB TEXT cap in both dialects."""
    assert isinstance(VISIBILITY_SEALS.c.clock_evidence_json.type, Text)
    assert isinstance(VISIBILITY_SEALS.c.sealed_at_us.type, BigInteger)
