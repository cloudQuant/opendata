"""Exercise revision 0012 against the frozen revision 0011 schema."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from types import ModuleType

    from sqlalchemy.engine import Connection, Engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261027-0012_instrument_capture_scope.py"
)
MIGRATION_NAME = "instrument_capture_scope_migration_0012"
FACT_TABLE = "od_published_fact_versions"
SNAPSHOT_TABLE = "od_publication_snapshots"
NEIGHBOR_TABLE = "od_migration_neighbor"


def _load_migration() -> ModuleType:
    """Load only the selected revision module, without Alembic's project env."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frozen_key_type(length: int) -> sa.String:
    """Return revision 0011's literal cross-dialect key type."""
    from sqlalchemy.dialects import mysql

    return sa.String(length).with_variant(
        mysql.VARCHAR(length, collation="utf8mb4_0900_bin"),
        "mysql",
    )


def _frozen_json_type() -> sa.Text:
    """Return revision 0011's literal cross-dialect JSON-text type."""
    from sqlalchemy.dialects import mysql

    return sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql")


def _apply_frozen_0011(connection: Connection) -> None:
    """Create the frozen 0011 publication schema using explicit Operations."""
    operations_context = MigrationContext.configure(connection)
    with Operations.context(operations_context) as operations:
        operations.create_table(
            "od_publication_heads",
            sa.Column("dataset_id", _frozen_key_type(128), primary_key=True),
            sa.Column("latest_sequence", sa.BigInteger(), nullable=False),
            sa.Column("latest_publication_id", sa.String(32), nullable=True),
            sa.Column("watermark_json", _frozen_json_type(), nullable=True),
            sa.Column("updated_at_us", sa.BigInteger(), nullable=False),
            sa.CheckConstraint("latest_sequence >= 0", name="ck_od_publication_head_sequence"),
        )
        operations.create_table(
            "od_publications",
            sa.Column("publication_id", sa.String(32), primary_key=True),
            sa.Column("dataset_id", _frozen_key_type(128), nullable=False),
            sa.Column("source_id", _frozen_key_type(128), nullable=False),
            sa.Column("sequence", sa.BigInteger(), nullable=False),
            sa.Column("schema_version", sa.String(64), nullable=False),
            sa.Column("semantic_version", sa.String(64), nullable=False),
            sa.Column("unit", sa.String(128), nullable=False),
            sa.Column("adjustment_basis", sa.String(128), nullable=False),
            sa.Column("quality_json", _frozen_json_type(), nullable=False),
            sa.Column("provenance_json", _frozen_json_type(), nullable=False),
            sa.Column("watermark_json", _frozen_json_type(), nullable=True),
            sa.Column("idempotency_key", _frozen_key_type(256), nullable=False),
            sa.Column("payload_hash", sa.String(64), nullable=False),
            sa.Column("record_count", sa.Integer(), nullable=False),
            sa.Column("published_at_us", sa.BigInteger(), nullable=False),
            sa.CheckConstraint("sequence > 0", name="ck_od_publication_sequence"),
            sa.CheckConstraint("record_count >= 0", name="ck_od_publication_record_count"),
        )
        operations.create_index(
            "uq_od_publication_dataset_sequence",
            "od_publications",
            ["dataset_id", "sequence"],
            unique=True,
        )
        operations.create_index(
            "uq_od_publication_dataset_idempotency",
            "od_publications",
            ["dataset_id", "idempotency_key"],
            unique=True,
        )
        operations.create_index(
            "ix_od_publication_dataset_published",
            "od_publications",
            ["dataset_id", "published_at_us"],
        )
        operations.create_table(
            FACT_TABLE,
            sa.Column("dataset_id", _frozen_key_type(128), primary_key=True),
            sa.Column("business_key", _frozen_key_type(512), primary_key=True),
            sa.Column("revision", sa.Integer(), primary_key=True),
            sa.Column(
                "publication_id",
                sa.String(32),
                sa.ForeignKey("od_publications.publication_id"),
                nullable=False,
            ),
            sa.Column("publication_sequence", sa.BigInteger(), nullable=False),
            sa.Column("business_date", sa.Date(), nullable=True),
            sa.Column("event_at", sa.String(40), nullable=True),
            sa.Column("observed_at", sa.String(40), nullable=True),
            sa.Column("source_published_at", sa.String(40), nullable=True),
            sa.Column("available_at", sa.String(40), nullable=True),
            sa.Column("record_json", _frozen_json_type(), nullable=False),
            sa.Column("record_hash", sa.String(64), nullable=False),
            sa.Column("is_deleted", sa.Boolean(), nullable=False),
            sa.CheckConstraint("revision > 0", name="ck_od_published_fact_revision"),
        )
        operations.create_index(
            "ix_od_published_fact_asof",
            FACT_TABLE,
            ["dataset_id", "business_key", "publication_sequence", "revision"],
        )
        operations.create_index(
            "ix_od_published_fact_date",
            FACT_TABLE,
            ["dataset_id", "business_date", "business_key"],
        )
        operations.create_table(
            SNAPSHOT_TABLE,
            sa.Column("snapshot_id", sa.String(32), primary_key=True),
            sa.Column("dataset_id", _frozen_key_type(128), nullable=False),
            sa.Column("schema_version", sa.String(64), nullable=False),
            sa.Column(
                "publication_id",
                sa.String(32),
                sa.ForeignKey("od_publications.publication_id"),
                nullable=False,
            ),
            sa.Column("publication_sequence", sa.BigInteger(), nullable=False),
            sa.Column("start_date", sa.Date(), nullable=True),
            sa.Column("end_date", sa.Date(), nullable=True),
            sa.Column("page_size", sa.Integer(), nullable=False),
            sa.Column("access_scope", _frozen_key_type(128), nullable=False),
            sa.Column("selection_sha256", sa.String(64), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("record_count", sa.BigInteger(), nullable=False),
            sa.Column("as_of_us", sa.BigInteger(), nullable=False),
            sa.Column("created_at_us", sa.BigInteger(), nullable=False),
            sa.Column("expires_at_us", sa.BigInteger(), nullable=False),
            sa.CheckConstraint("page_size > 0", name="ck_od_snapshot_page_size"),
            sa.CheckConstraint("record_count >= 0", name="ck_od_snapshot_record_count"),
        )
        operations.create_index("ix_od_snapshot_expiry", SNAPSHOT_TABLE, ["expires_at_us"])


def _new_database(tmp_path: Path) -> Engine:
    """Create a caller-owned SQLite database in pytest's temporary directory."""
    database_path = tmp_path / "publication-capture-migration.sqlite"
    return sa.create_engine(f"sqlite:///{database_path}")


def _apply_revision(engine: Engine, operation_name: str) -> None:
    """Call one revision function through an owned connection and Operations."""
    migration = _load_migration()
    operation = getattr(migration, operation_name)
    with engine.begin() as connection:
        migration_context = MigrationContext.configure(connection)
        with Operations.context(migration_context):
            operation()


def _create_neighbor(connection: Connection) -> None:
    """Create a representative table owned by an adjacent migration."""
    connection.execute(
        sa.text(
            "CREATE TABLE od_migration_neighbor "
            "(id INTEGER PRIMARY KEY, marker VARCHAR(32) NOT NULL)"
        )
    )
    connection.execute(
        sa.text("INSERT INTO od_migration_neighbor (id, marker) VALUES (1, :marker)"),
        {"marker": "preserve-me"},
    )


def _insert_publication(connection: Connection) -> None:
    """Insert the publication referenced by the frozen fact and snapshot rows."""
    connection.execute(
        sa.text(
            "INSERT INTO od_publications (publication_id, dataset_id, source_id, "
            "sequence, schema_version, semantic_version, unit, adjustment_basis, "
            "quality_json, provenance_json, watermark_json, idempotency_key, "
            "payload_hash, record_count, published_at_us) VALUES "
            "(:publication_id, :dataset_id, :source_id, :sequence, :schema_version, "
            ":semantic_version, :unit, :adjustment_basis, :quality_json, "
            ":provenance_json, :watermark_json, :idempotency_key, :payload_hash, "
            ":record_count, :published_at_us)"
        ),
        {
            "publication_id": "publication-0001",
            "dataset_id": "sina_fzb_report_observations",
            "source_id": "sina-fzb",
            "sequence": 1,
            "schema_version": "source-native-report-v1",
            "semantic_version": "sina-fzb-source-native-v1",
            "unit": "per-field",
            "adjustment_basis": "not_applicable",
            "quality_json": "{}",
            "provenance_json": "{}",
            "watermark_json": None,
            "idempotency_key": "migration-fixture",
            "payload_hash": "a" * 64,
            "record_count": 1,
            "published_at_us": 1,
        },
    )


def _insert_fact(
    connection: Connection,
    *,
    business_key: str = "legacy-key",
    instrument_id: str | None = None,
    capture_id: str | None = None,
) -> None:
    """Insert a fact row with optional capture selectors."""
    connection.execute(
        sa.text(
            "INSERT INTO od_published_fact_versions (dataset_id, business_key, revision, "
            "publication_id, publication_sequence, business_date, event_at, "
            "observed_at, source_published_at, available_at, record_json, "
            "record_hash, is_deleted, instrument_id, capture_id) VALUES "
            "(:dataset_id, :business_key, :revision, :publication_id, "
            ":publication_sequence, :business_date, :event_at, :observed_at, "
            ":source_published_at, :available_at, :record_json, :record_hash, "
            ":is_deleted, :instrument_id, :capture_id)"
        ),
        {
            "dataset_id": "sina_fzb_report_observations",
            "business_key": business_key,
            "revision": 1,
            "publication_id": "publication-0001",
            "publication_sequence": 1,
            "business_date": None,
            "event_at": None,
            "observed_at": None,
            "source_published_at": None,
            "available_at": None,
            "record_json": '{"value":"source-value"}',
            "record_hash": "b" * 64,
            "is_deleted": False,
            "instrument_id": instrument_id,
            "capture_id": capture_id,
        },
    )


def _insert_snapshot(
    connection: Connection,
    *,
    snapshot_id: str = "legacy-snapshot",
    instrument_id: str | None = None,
    capture_id: str | None = None,
) -> None:
    """Insert a fixed snapshot with optional capture selectors."""
    connection.execute(
        sa.text(
            "INSERT INTO od_publication_snapshots (snapshot_id, dataset_id, schema_version, "
            "publication_id, publication_sequence, start_date, end_date, page_size, "
            "access_scope, selection_sha256, content_hash, record_count, as_of_us, "
            "created_at_us, expires_at_us, instrument_id, capture_id) VALUES "
            "(:snapshot_id, :dataset_id, :schema_version, :publication_id, "
            ":publication_sequence, :start_date, :end_date, :page_size, "
            ":access_scope, :selection_sha256, :content_hash, :record_count, "
            ":as_of_us, :created_at_us, :expires_at_us, :instrument_id, :capture_id)"
        ),
        {
            "snapshot_id": snapshot_id,
            "dataset_id": "sina_fzb_report_observations",
            "schema_version": "source-native-report-v1",
            "publication_id": "publication-0001",
            "publication_sequence": 1,
            "start_date": None,
            "end_date": None,
            "page_size": 100,
            "access_scope": "test-scope",
            "selection_sha256": "c" * 64,
            "content_hash": "d" * 64,
            "record_count": 1,
            "as_of_us": 1,
            "created_at_us": 1,
            "expires_at_us": 2,
            "instrument_id": instrument_id,
            "capture_id": capture_id,
        },
    )


def _prepare_legacy_database(engine: Engine) -> None:
    """Apply the frozen base schema and save representative legacy data."""
    with engine.begin() as connection:
        _apply_frozen_0011(connection)
        _create_neighbor(connection)
        _insert_publication(connection)
        _insert_fact(connection)
        _insert_snapshot(connection)


def _schema_signature(connection: Connection) -> tuple[object, ...]:
    """Capture affected table columns, indexes, and check constraints."""
    inspector = sa.inspect(connection)
    signature: list[object] = []
    for table_name in (FACT_TABLE, SNAPSHOT_TABLE):
        columns = tuple(
            (column["name"], str(column["type"]), column["nullable"])
            for column in inspector.get_columns(table_name)
        )
        indexes = tuple(
            sorted(
                (
                    index["name"],
                    tuple(index["column_names"]),
                    bool(index["unique"]),
                )
                for index in inspector.get_indexes(table_name)
            )
        )
        checks = tuple(
            sorted(
                (constraint["name"], " ".join(constraint["sqltext"].split()))
                for constraint in inspector.get_check_constraints(table_name)
            )
        )
        signature.append((table_name, columns, indexes, checks))
    return tuple(signature)


def _assert_scoped_columns(engine: Engine) -> None:
    """Assert the two exact nullable capture-key columns exist on both tables."""
    inspector = sa.inspect(engine)
    for table_name in (FACT_TABLE, SNAPSHOT_TABLE):
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        assert columns["instrument_id"]["nullable"] is True
        assert columns["capture_id"]["nullable"] is True
        assert columns["instrument_id"]["type"].length == 128
        assert columns["capture_id"]["type"].length == 64


def test_upgrade_preserves_legacy_rows_and_enforces_paired_scope(tmp_path: Path) -> None:
    """Upgrade legacy rows unchanged, allow complete scopes, and reject half pairs."""
    engine = _new_database(tmp_path)
    try:
        _prepare_legacy_database(engine)
        with engine.connect() as connection:
            legacy_fact_before = tuple(
                connection.execute(sa.text("SELECT * FROM od_published_fact_versions")).one()
            )
            legacy_snapshot_before = tuple(
                connection.execute(sa.text("SELECT * FROM od_publication_snapshots")).one()
            )
        _apply_revision(engine, "upgrade")
        _assert_scoped_columns(engine)

        with engine.begin() as connection:
            legacy_fact_after = tuple(
                connection.execute(sa.text("SELECT * FROM od_published_fact_versions")).one()
            )
            legacy_snapshot_after = tuple(
                connection.execute(sa.text("SELECT * FROM od_publication_snapshots")).one()
            )
            assert legacy_fact_after == (*legacy_fact_before, None, None)
            assert legacy_snapshot_after == (*legacy_snapshot_before, None, None)

            _insert_fact(
                connection,
                business_key="scoped-key",
                instrument_id="AAPL.US",
                capture_id="e" * 64,
            )
            _insert_snapshot(
                connection,
                snapshot_id="scoped-snapshot",
                instrument_id="AAPL.US",
                capture_id="e" * 64,
            )

            for instrument_id, capture_id in (("AAPL.US", None), (None, "f" * 64)):
                with pytest.raises(IntegrityError), connection.begin_nested():
                    _insert_fact(
                        connection,
                        business_key=f"invalid-fact-{instrument_id}-{capture_id}",
                        instrument_id=instrument_id,
                        capture_id=capture_id,
                    )
                with pytest.raises(IntegrityError), connection.begin_nested():
                    _insert_snapshot(
                        connection,
                        snapshot_id=f"invalid-snapshot-{instrument_id}-{capture_id}",
                        instrument_id=instrument_id,
                        capture_id=capture_id,
                    )

            checks = {
                table_name: {
                    constraint["name"]
                    for constraint in sa.inspect(connection).get_check_constraints(table_name)
                }
                for table_name in (FACT_TABLE, SNAPSHOT_TABLE)
            }
            assert "ck_od_fact_capture_pair" in checks[FACT_TABLE]
            assert "ck_od_published_fact_revision" in checks[FACT_TABLE]
            assert "ck_od_snapshot_capture_pair" in checks[SNAPSHOT_TABLE]
            assert checks[SNAPSHOT_TABLE] == {
                "ck_od_snapshot_capture_pair",
                "ck_od_snapshot_page_size",
                "ck_od_snapshot_record_count",
            }
            fact_indexes = {
                index["name"]: tuple(index["column_names"])
                for index in sa.inspect(connection).get_indexes(FACT_TABLE)
            }
            assert {
                "ix_od_published_fact_asof",
                "ix_od_published_fact_date",
                "ix_od_fact_capture_read",
            } <= fact_indexes.keys()
            assert fact_indexes["ix_od_fact_capture_read"] == (
                "dataset_id",
                "instrument_id",
                "capture_id",
                "publication_sequence",
            )
            snapshot_indexes = {
                index["name"] for index in sa.inspect(connection).get_indexes(SNAPSHOT_TABLE)
            }
            assert snapshot_indexes == {"ix_od_snapshot_expiry"}
            neighbor = connection.execute(
                sa.text("SELECT marker FROM od_migration_neighbor WHERE id = 1")
            ).scalar_one()
            assert neighbor == "preserve-me"
    finally:
        engine.dispose()


@pytest.mark.parametrize("scoped_table", [FACT_TABLE, SNAPSHOT_TABLE])
def test_downgrade_rejects_scope_before_any_ddl(
    tmp_path: Path,
    scoped_table: str,
) -> None:
    """Refuse rollback for scope in either table and preserve the full schema."""
    engine = _new_database(tmp_path)
    try:
        _prepare_legacy_database(engine)
        _apply_revision(engine, "upgrade")
        with engine.begin() as connection:
            if scoped_table == FACT_TABLE:
                _insert_fact(
                    connection,
                    business_key="scoped-key",
                    instrument_id="AAPL.US",
                    capture_id="e" * 64,
                )
            else:
                _insert_snapshot(
                    connection,
                    snapshot_id="scoped-snapshot",
                    instrument_id="AAPL.US",
                    capture_id="e" * 64,
                )
        with engine.connect() as connection:
            schema_before = _schema_signature(connection)

        with pytest.raises(RuntimeError, match="Scoped publication data"):
            _apply_revision(engine, "downgrade")

        with engine.connect() as connection:
            assert _schema_signature(connection) == schema_before
            scoped_table_expression = sa.table(
                scoped_table,
                sa.column("instrument_id"),
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(scoped_table_expression)
                    .where(scoped_table_expression.c.instrument_id.is_not(None))
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    sa.text("SELECT count(*) FROM od_migration_neighbor")
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()


def test_legacy_only_downgrade_preserves_data_and_can_upgrade_again(tmp_path: Path) -> None:
    """Allow a legacy-only rollback, retain rows, and reapply the revision."""
    engine = _new_database(tmp_path)
    try:
        _prepare_legacy_database(engine)
        _apply_revision(engine, "upgrade")
        _apply_revision(engine, "downgrade")

        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            fact_columns = {column["name"] for column in inspector.get_columns(FACT_TABLE)}
            snapshot_columns = {column["name"] for column in inspector.get_columns(SNAPSHOT_TABLE)}
            assert "instrument_id" not in fact_columns
            assert "capture_id" not in fact_columns
            assert "instrument_id" not in snapshot_columns
            assert "capture_id" not in snapshot_columns
            assert connection.execute(
                sa.text("SELECT business_key, record_json FROM od_published_fact_versions")
            ).one() == ("legacy-key", '{"value":"source-value"}')
            assert connection.execute(
                sa.text("SELECT snapshot_id, content_hash FROM od_publication_snapshots")
            ).one() == ("legacy-snapshot", "d" * 64)
            assert (
                connection.execute(
                    sa.text("SELECT marker FROM od_migration_neighbor WHERE id = 1")
                ).scalar_one()
                == "preserve-me"
            )

        _apply_revision(engine, "upgrade")
        _assert_scoped_columns(engine)
        with engine.connect() as connection:
            assert connection.execute(
                sa.text("SELECT instrument_id, capture_id FROM od_published_fact_versions")
            ).one() == (None, None)
            assert connection.execute(
                sa.text("SELECT instrument_id, capture_id FROM od_publication_snapshots")
            ).one() == (None, None)
    finally:
        engine.dispose()


def test_revision_identity_follows_frozen_0011() -> None:
    """Bind this test to the exact child/parent revision names."""
    migration = _load_migration()

    assert migration.revision == "0012_instrument_capture_scope"
    assert migration.down_revision == "0011_publication_snapshots"
