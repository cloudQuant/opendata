"""Behavioral checks for the empty metadata publication-receipt schema."""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, insert, inspect, select
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from opendata.data.metadata_receipt_schema import METADATA_PUBLICATION_RECEIPTS

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from types import ModuleType

    from sqlalchemy.engine import Engine

_REVISION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "2026_10_28-0008_metadata_publication_receipt.py"
)


def _load_revision() -> ModuleType:
    """Load revision 0008 without importing or configuring the application."""
    spec = importlib.util.spec_from_file_location("metadata_receipt_revision_0008", _REVISION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load revision 0008")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_receipt(**overrides: object) -> dict[str, object]:
    """Return one synthetic, explicitly unverified receipt row."""
    row: dict[str, object] = {
        "producer_identity": "opendata:deployment-pin-alpha",
        "producer_identity_evidence_kind": "caller_supplied_external_pin_unverified",
        "publication_id": "a" * 32,
        "dataset_id": "market.equity.daily",
        "publication_sequence": 1,
        "record_count": 0,
        "payload_sha256": "b" * 64,
        "contract_sha256": "c" * 64,
        "contract_evidence_kind": "caller_supplied_unverified",
        "provenance_sha256": "d" * 64,
        "schema_version": "v1",
        "event_version": 1,
        "event_json_utf8": b'{"event_type":"opendata.publication.published"}',
        "event_sha256": "e" * 64,
        "published_at_us": 1_800_000_000_000_000,
        "received_at_us": 1_800_000_000_000_100,
    }
    row.update(overrides)
    return row


@contextmanager
def _sqlite_engine() -> Iterator[Engine]:
    """Yield a private in-memory SQLite database and always dispose its engine."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        METADATA_PUBLICATION_RECEIPTS.create(engine)
        yield engine
    finally:
        engine.dispose()


@contextmanager
def _migrated_sqlite_engine(revision: ModuleType) -> Iterator[Engine]:
    """Run revision 0008 in memory, disposing the engine even after a failed assertion."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            revision.__dict__["op"] = Operations(MigrationContext.configure(connection))
            revision.upgrade()
        yield engine
    finally:
        engine.dispose()


def _table_shape(engine: Engine) -> tuple[object, ...]:
    """Return portable column, key, unique, and check metadata for comparison."""
    inspector = inspect(engine)
    columns = tuple(
        (item["name"], item["nullable"], item["type"].__class__.__name__)
        for item in inspector.get_columns("od_metadata_publication_receipts")
    )
    primary_key = tuple(
        inspector.get_pk_constraint("od_metadata_publication_receipts")["constrained_columns"]
    )
    unique = tuple(
        sorted(
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints("od_metadata_publication_receipts")
        )
    )
    checks = tuple(
        sorted(
            (item["name"], " ".join(item["sqltext"].split()))
            for item in inspector.get_check_constraints("od_metadata_publication_receipts")
        )
    )
    return columns, primary_key, unique, checks


def test_revision_0008_matches_the_core_schema_and_starts_empty() -> None:
    """The migration and runtime schema describe the same empty table."""
    revision = _load_revision()
    with _sqlite_engine() as core_engine, _migrated_sqlite_engine(revision) as migrated_engine:
        assert _table_shape(core_engine) == _table_shape(migrated_engine)
        with migrated_engine.connect() as connection:
            count = connection.execute(
                select(func.count()).select_from(METADATA_PUBLICATION_RECEIPTS)
            ).scalar_one()
        assert count == 0


def test_sqlite_identity_keys_are_binary_and_hash_evidence_is_preserved() -> None:
    """Case-distinct external pins remain distinct and evidence stays unverified."""
    with _sqlite_engine() as engine:
        table = METADATA_PUBLICATION_RECEIPTS
        with engine.begin() as connection:
            connection.execute(insert(table).values(**_valid_receipt()))
            connection.execute(
                insert(table).values(
                    **_valid_receipt(
                        producer_identity="OpenData:deployment-pin-alpha",
                        publication_sequence=2,
                        publication_id="f" * 32,
                        contract_sha256=None,
                        contract_evidence_kind="absent",
                        event_json_utf8=b'{"publication_id":"different-case-pin"}',
                        event_sha256="1" * 64,
                    )
                )
            )
            rows = connection.execute(
                select(
                    table.c.producer_identity,
                    table.c.contract_sha256,
                    table.c.contract_evidence_kind,
                    table.c.event_json_utf8,
                ).order_by(table.c.producer_identity)
            ).all()

        assert rows == [
            (
                "OpenData:deployment-pin-alpha",
                None,
                "absent",
                b'{"publication_id":"different-case-pin"}',
            ),
            (
                "opendata:deployment-pin-alpha",
                "c" * 64,
                "caller_supplied_unverified",
                b'{"event_type":"opendata.publication.published"}',
            ),
        ]


@pytest.mark.parametrize(
    ("overrides", "constraint_name"),
    [
        (
            {"producer_identity_evidence_kind": "authenticated"},
            "ck_od_meta_receipt_producer_evidence",
        ),
        ({"contract_sha256": None}, "ck_od_meta_receipt_contract_evidence"),
        ({"publication_sequence": 0}, "ck_od_meta_receipt_sequence"),
        ({"event_version": 2}, "ck_od_meta_receipt_event_version"),
        ({"received_at_us": -1}, "ck_od_meta_receipt_received_at"),
    ],
)
def test_schema_rejects_invalid_receipt_evidence_and_bounds(
    overrides: Mapping[str, object], constraint_name: str
) -> None:
    """SQLite raises the exact named CHECK constraint for each invalid row."""
    with _sqlite_engine() as engine:
        with engine.begin() as connection, pytest.raises(IntegrityError) as error:
            connection.execute(
                insert(METADATA_PUBLICATION_RECEIPTS).values(**_valid_receipt(**overrides))
            )
        assert str(error.value.orig) == f"CHECK constraint failed: {constraint_name}"


def test_event_body_limit_counts_utf8_bytes_on_sqlite() -> None:
    """The BLOB column applies the same exact-byte bound used by the event digest."""
    with _sqlite_engine() as engine:
        multibyte_body = b'"' + ("é" * 4_095).encode("utf-8") + b'"'
        assert len(multibyte_body) == 8_192
        with engine.begin() as connection:
            connection.execute(
                insert(METADATA_PUBLICATION_RECEIPTS).values(
                    **_valid_receipt(event_json_utf8=multibyte_body)
                )
            )

        oversized_body = b'"' + ("é" * 4_096).encode("utf-8") + b'"'
        assert len(oversized_body) == 8_194
        with engine.begin() as connection, pytest.raises(IntegrityError) as error:
            connection.execute(
                insert(METADATA_PUBLICATION_RECEIPTS).values(
                    **_valid_receipt(
                        publication_id="f" * 32,
                        publication_sequence=2,
                        event_json_utf8=oversized_body,
                        event_sha256="1" * 64,
                    )
                )
            )
        assert str(error.value.orig) == "CHECK constraint failed: ck_od_meta_receipt_event_size"


def test_mysql_ddl_uses_case_sensitive_identity_and_binary_event_bytes() -> None:
    """The MySQL DDL pins identity collation and stores exact event bytes."""
    statement = str(CreateTable(METADATA_PUBLICATION_RECEIPTS).compile(dialect=mysql.dialect()))

    assert "utf8mb4_0900_bin" in statement
    assert "ENGINE=InnoDB" in statement
    assert "event_json_utf8 BLOB" in statement


def test_downgrade_refuses_nonempty_receipts_and_allows_empty_table() -> None:
    """Downgrade preserves received evidence and removes only an empty table."""
    revision = _load_revision()
    with _migrated_sqlite_engine(revision) as engine:
        with engine.begin() as connection:
            revision.__dict__["op"] = Operations(MigrationContext.configure(connection))
            connection.execute(insert(METADATA_PUBLICATION_RECEIPTS).values(**_valid_receipt()))
            with pytest.raises(RuntimeError, match="non-empty publication receipt table"):
                revision.downgrade()
            assert inspect(connection).has_table("od_metadata_publication_receipts")

        with engine.begin() as connection:
            revision.__dict__["op"] = Operations(MigrationContext.configure(connection))
            connection.execute(METADATA_PUBLICATION_RECEIPTS.delete())
            revision.downgrade()
            assert not inspect(connection).has_table("od_metadata_publication_receipts")


class _StubOperations:
    """Supply just enough Alembic context to test migration target guards offline."""

    def __init__(self, *, as_sql: bool = False, dialect: object | None = None) -> None:
        self._as_sql = as_sql
        self._dialect = dialect

    def get_context(self) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(as_sql=self._as_sql)

    def get_bind(self) -> object:
        from types import SimpleNamespace

        if self._dialect is None:
            raise AssertionError("offline migration guard must fail before asking for a bind")
        return SimpleNamespace(dialect=self._dialect)


def test_upgrade_refuses_offline_sql_before_any_ddl() -> None:
    """The public migration entry point fails closed when Alembic is in SQL mode."""
    revision = _load_revision()
    revision.__dict__["op"] = _StubOperations(as_sql=True)

    with pytest.raises(RuntimeError) as error:
        revision.upgrade()
    assert str(error.value) == "revision 0008 requires a live SQLite or MySQL connection"


@pytest.mark.parametrize(
    ("dialect", "expected_error"),
    [
        (
            {"name": "mariadb", "is_mariadb": False, "server_version_info": (10, 11, 0)},
            "revision 0008 supports MySQL 8.0.16+, not MariaDB",
        ),
        (
            {"name": "mysql", "is_mariadb": True, "server_version_info": (10, 11, 0)},
            "revision 0008 supports MySQL 8.0.16+, not MariaDB",
        ),
        (
            {"name": "postgresql", "is_mariadb": False, "server_version_info": (16, 0, 0)},
            "revision 0008 supports SQLite and MySQL 8.0.16+ only",
        ),
    ],
)
def test_upgrade_rejects_unsupported_dialects(
    dialect: Mapping[str, object], expected_error: str
) -> None:
    """The migration rejects MariaDB and unrelated SQL dialects before DDL."""
    from types import SimpleNamespace

    revision = _load_revision()
    revision.__dict__["op"] = _StubOperations(dialect=SimpleNamespace(**dialect))

    with pytest.raises(RuntimeError) as error:
        revision.upgrade()
    assert str(error.value) == expected_error


@pytest.mark.parametrize(
    "version",
    [None, (), (8, 0), (8, 0, "16"), (5, 7, 44), (8, 0, 15)],
)
def test_upgrade_rejects_mysql_versions_without_enforced_checks(version: object) -> None:
    """Malformed, non-8.x, and pre-8.0.16 MySQL versions fail closed."""
    from types import SimpleNamespace

    revision = _load_revision()
    dialect = SimpleNamespace(name="mysql", is_mariadb=False, server_version_info=version)
    revision.__dict__["op"] = _StubOperations(dialect=dialect)

    with pytest.raises(RuntimeError) as error:
        revision.upgrade()
    assert str(error.value) == "revision 0008 requires MySQL 8.0.16 or later within MySQL 8"


@pytest.mark.parametrize("version", [(8, 0, 16), (8, 4, 0)])
def test_mysql_8_supported_versions_pass_the_migration_guard(version: tuple[int, int, int]) -> None:
    """The lower bound and later MySQL 8 releases are accepted without DB access."""
    from types import SimpleNamespace

    revision = _load_revision()
    dialect = SimpleNamespace(name="mysql", is_mariadb=False, server_version_info=version)
    revision.__dict__["op"] = _StubOperations(dialect=dialect)

    revision._require_supported_target()
