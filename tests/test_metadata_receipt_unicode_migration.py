"""Regression checks for publisher character limits in receipt metadata."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from opendata.data.metadata_receipt_schema import METADATA_PUBLICATION_RECEIPTS

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

_MIGRATION_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"
_REVISION_0008 = _MIGRATION_DIR / "2026_10_28-0008_metadata_publication_receipt.py"
_REVISION_0009 = _MIGRATION_DIR / "2026_10_28-0009_metadata_receipt_unicode_lengths.py"
_TABLE = "od_metadata_publication_receipts"


def _load_revision(path: Path, name: str) -> ModuleType:
    """Load an Alembic revision without importing application settings."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load migration module {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_receipt(
    connection: Connection,
    *,
    dataset_id: str,
    schema_version: str,
    publication_id: str = "a" * 32,
    publication_sequence: int = 1,
) -> None:
    """Insert one bounded synthetic event row with caller supplied identifiers."""
    connection.execute(
        text(
            "INSERT INTO od_metadata_publication_receipts ("
            "producer_identity, producer_identity_evidence_kind, publication_id, dataset_id, "
            "publication_sequence, record_count, payload_sha256, contract_sha256, "
            "contract_evidence_kind, provenance_sha256, schema_version, event_version, "
            "event_json_utf8, event_sha256, published_at_us, received_at_us"
            ") VALUES ("
            ":producer_identity, :producer_identity_evidence_kind, :publication_id, :dataset_id, "
            ":publication_sequence, :record_count, :payload_sha256, :contract_sha256, "
            ":contract_evidence_kind, :provenance_sha256, :schema_version, :event_version, "
            ":event_json_utf8, :event_sha256, :published_at_us, :received_at_us)"
        ),
        {
            "producer_identity": "publisher:unicode-contract",
            "producer_identity_evidence_kind": "caller_supplied_external_pin_unverified",
            "publication_id": publication_id,
            "dataset_id": dataset_id,
            "publication_sequence": publication_sequence,
            "record_count": 0,
            "payload_sha256": "b" * 64,
            "contract_sha256": "c" * 64,
            "contract_evidence_kind": "caller_supplied_unverified",
            "provenance_sha256": "d" * 64,
            "schema_version": schema_version,
            "event_version": 1,
            "event_json_utf8": b'{"event_type":"opendata.publication.published"}',
            "event_sha256": "e" * 64,
            "published_at_us": 1_800_000_000_000_000,
            "received_at_us": 1_800_000_000_000_100,
        },
    )


def _run_revision(revision: ModuleType, connection: Connection, operation: str) -> None:
    """Run one revision entry point on the explicitly supplied SQLite connection."""
    revision.__dict__["op"] = Operations(MigrationContext.configure(connection))
    getattr(revision, operation)()


def test_runtime_schema_uses_dialect_specific_character_counting() -> None:
    """Use SQLite character length and MySQL CHAR_LENGTH for publisher limits."""
    sqlite_ddl = str(CreateTable(METADATA_PUBLICATION_RECEIPTS).compile(dialect=sqlite.dialect()))
    mysql_ddl = str(CreateTable(METADATA_PUBLICATION_RECEIPTS).compile(dialect=mysql.dialect()))

    assert "CHECK (length(dataset_id) BETWEEN 1 AND 128)" in sqlite_ddl
    assert "CHECK (length(schema_version) BETWEEN 1 AND 64)" in sqlite_ddl
    assert "char_length(dataset_id)" not in sqlite_ddl
    assert "char_length(schema_version)" not in sqlite_ddl
    assert "CHECK (char_length(dataset_id) BETWEEN 1 AND 128)" in mysql_ddl
    assert "CHECK (char_length(schema_version) BETWEEN 1 AND 64)" in mysql_ddl


def test_sqlite_upgrade_preserves_unicode_values_and_all_identity_keys() -> None:
    """Upgrade and downgrade preserve rows even when their UTF-8 byte lengths exceed limits."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    revision_0008 = _load_revision(_REVISION_0008, "metadata_receipt_0008_for_unicode_test")
    revision_0009 = _load_revision(_REVISION_0009, "metadata_receipt_0009_for_unicode_test")
    dataset_id = "股" * 100
    schema_version = "版本" * 20
    try:
        with engine.begin() as connection:
            _run_revision(revision_0008, connection, "upgrade")
            _insert_receipt(connection, dataset_id=dataset_id, schema_version=schema_version)
            before = (
                connection.execute(text("SELECT * FROM od_metadata_publication_receipts"))
                .mappings()
                .one()
            )

            _run_revision(revision_0009, connection, "upgrade")
            after = (
                connection.execute(text("SELECT * FROM od_metadata_publication_receipts"))
                .mappings()
                .one()
            )
            assert dict(after) == dict(before)

            checks = {
                item["name"]: " ".join(item["sqltext"].split())
                for item in inspect(connection).get_check_constraints(_TABLE)
            }
            assert checks["ck_od_meta_receipt_dataset_len"] == (
                "length(dataset_id) BETWEEN 1 AND 128"
            )
            assert checks["ck_od_meta_receipt_schema"] == (
                "length(schema_version) BETWEEN 1 AND 64"
            )

            _run_revision(revision_0009, connection, "downgrade")
            restored = (
                connection.execute(text("SELECT * FROM od_metadata_publication_receipts"))
                .mappings()
                .one()
            )
            assert dict(restored) == dict(before)
    finally:
        engine.dispose()


def test_sqlite_multibyte_character_boundaries_remain_inclusive() -> None:
    """SQLite accepts exactly 128/64 characters and rejects one more character."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    revision_0008 = _load_revision(_REVISION_0008, "metadata_receipt_0008_for_bounds_test")
    revision_0009 = _load_revision(_REVISION_0009, "metadata_receipt_0009_for_bounds_test")
    try:
        with engine.begin() as connection:
            _run_revision(revision_0008, connection, "upgrade")
            _run_revision(revision_0009, connection, "upgrade")

            _insert_receipt(connection, dataset_id="股" * 128, schema_version="版" * 64)
            with pytest.raises(IntegrityError):
                _insert_receipt(
                    connection,
                    dataset_id="股" * 129,
                    schema_version="版" * 64,
                    publication_id="f" * 32,
                    publication_sequence=2,
                )
            with pytest.raises(IntegrityError):
                _insert_receipt(
                    connection,
                    dataset_id="market.equity.daily",
                    schema_version="版" * 65,
                    publication_id="1" * 32,
                    publication_sequence=3,
                )
    finally:
        engine.dispose()


class _StubResult:
    """Return a configured first row without opening a database connection."""

    def __init__(self, row: object | None) -> None:
        self._row = row

    def first(self) -> object | None:
        """Return the synthetic first row."""
        return self._row


class _StubBind:
    """Capture migration preflight SQL using a declared MySQL dialect."""

    def __init__(
        self,
        *,
        first_row: object | None = None,
        dialect_name: str = "mysql",
    ) -> None:
        self.dialect = SimpleNamespace(
            name=dialect_name,
            is_mariadb=False,
            server_version_info=(8, 4, 0) if dialect_name == "mysql" else None,
        )
        self.first_row = first_row
        self.statements: list[str] = []

    def execute(self, statement: object) -> _StubResult:
        """Record a read-only query and return the configured result."""
        self.statements.append(str(statement))
        return _StubResult(self.first_row)


class _StubOperations:
    """Capture the MySQL migration DDL without database access."""

    def __init__(self, bind: _StubBind) -> None:
        self._bind = bind
        self.ddl: list[str] = []

    def get_context(self) -> SimpleNamespace:
        """Expose a live, non-offline Alembic-like context."""
        return SimpleNamespace(as_sql=False)

    def get_bind(self) -> _StubBind:
        """Return the synthetic MySQL bind."""
        return self._bind

    def execute(self, statement: object) -> None:
        """Record a DDL statement."""
        self.ddl.append(str(statement))


def test_mysql_upgrade_replaces_both_checks_in_one_character_length_statement() -> None:
    """MySQL migration uses one atomic ALTER statement with CHAR_LENGTH checks."""
    revision = _load_revision(_REVISION_0009, "metadata_receipt_0009_mysql_ddl_test")
    bind = _StubBind()
    operations = _StubOperations(bind)
    revision.__dict__["op"] = operations

    revision.upgrade()

    assert bind.statements == []
    assert len(operations.ddl) == 1
    ddl = operations.ddl[0]
    assert "ALTER TABLE `od_metadata_publication_receipts`" in ddl
    assert ddl.count("DROP CHECK") == 2
    assert "CHAR_LENGTH(`dataset_id`) BETWEEN 1 AND 128" in ddl
    assert "CHAR_LENGTH(`schema_version`) BETWEEN 1 AND 64" in ddl


def test_mysql_downgrade_refuses_overlong_utf8_before_any_ddl() -> None:
    """A row that violates old byte limits blocks downgrade before changing checks."""
    revision = _load_revision(_REVISION_0009, "metadata_receipt_0009_mysql_block_test")
    bind = _StubBind(first_row=(1,))
    operations = _StubOperations(bind)
    revision.__dict__["op"] = operations

    with pytest.raises(RuntimeError, match="multibyte dataset_id or schema_version"):
        revision.downgrade()

    assert len(bind.statements) == 1
    assert "LENGTH(`dataset_id`) > 128" in bind.statements[0]
    assert "LENGTH(`schema_version`) > 64" in bind.statements[0]
    assert operations.ddl == []


def test_mysql_downgrade_rechecks_previous_byte_constraints_atomically() -> None:
    """A passing preflight still restores byte checks in one DDL statement."""
    revision = _load_revision(_REVISION_0009, "metadata_receipt_0009_mysql_restore_test")
    bind = _StubBind(first_row=None)
    operations = _StubOperations(bind)
    revision.__dict__["op"] = operations

    revision.downgrade()

    assert len(bind.statements) == 1
    assert len(operations.ddl) == 1
    ddl = operations.ddl[0]
    assert "LENGTH(`dataset_id`) BETWEEN 1 AND 128" in ddl
    assert "LENGTH(`schema_version`) BETWEEN 1 AND 64" in ddl


def test_postgresql_is_rejected_before_query_or_ddl() -> None:
    """Unsupported dialects fail closed before touching the receipt table."""
    revision = _load_revision(_REVISION_0009, "metadata_receipt_0009_postgresql_guard_test")
    bind = _StubBind(dialect_name="postgresql")
    operations = _StubOperations(bind)
    revision.__dict__["op"] = operations

    with pytest.raises(RuntimeError, match=r"SQLite and MySQL 8.0.16\+ only"):
        revision.upgrade()

    assert bind.statements == []
    assert operations.ddl == []
