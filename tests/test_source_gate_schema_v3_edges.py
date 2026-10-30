"""Fail-closed schema-contract edge cases for the source-gate v3 marker."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from alembic_source_gate import schema_contract


def _create_v3_schema(connection: Connection) -> None:
    """Install the repository-pinned gate DDL and a v3 marker in a scratch DB."""
    schema_contract._apply_v1_reference(connection)
    for statement in schema_contract.V2_OBJECT_SQL + schema_contract.V3_OBJECT_SQL:
        connection.exec_driver_sql(statement)
    connection.exec_driver_sql(
        "CREATE TABLE alembic_version_source_gate "
        "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
    )
    connection.exec_driver_sql(
        "INSERT INTO alembic_version_source_gate (version_num) VALUES (?)",
        (schema_contract.V3_REVISION,),
    )


@pytest.mark.parametrize(
    ("drift", "expected_detail"),
    [
        ("missing", "missing=trg_od_source_attempt_scope_v3_immutable_delete"),
        ("changed", "changed=trg_od_source_attempt_scope_v3_immutable_update"),
    ],
)
def test_v3_marker_rejects_missing_or_changed_immutable_trigger(
    drift: str, expected_detail: str
) -> None:
    """A v3 marker cannot bless a sidecar whose append-only guard has drifted."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            _create_v3_schema(connection)
            if drift == "missing":
                connection.exec_driver_sql(
                    "DROP TRIGGER trg_od_source_attempt_scope_v3_immutable_delete"
                )
            else:
                connection.exec_driver_sql(
                    "DROP TRIGGER trg_od_source_attempt_scope_v3_immutable_update"
                )
                connection.exec_driver_sql(
                    "CREATE TRIGGER trg_od_source_attempt_scope_v3_immutable_update "
                    "BEFORE UPDATE ON od_source_attempt_scope_v3 BEGIN "
                    "SELECT RAISE(ABORT, 'scope mutation permitted'); END"
                )

            with pytest.raises(RuntimeError, match="schema does not match") as exc_info:
                schema_contract.validate_migration_state(connection)

            assert expected_detail in str(exc_info.value)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("marker_ddl", "marker_rows", "marker_trigger", "expected_error"),
    [
        (
            "CREATE TABLE alembic_version_source_gate "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY, extra TEXT)",
            ("sg0003_source_attempt_scope_v3",),
            False,
            "unknown column layout",
        ),
        (
            "CREATE TABLE alembic_version_source_gate "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)",
            ("sg0003_source_attempt_scope_v3",),
            True,
            "unexpected triggers",
        ),
        (
            "CREATE TABLE alembic_version_source_gate "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)",
            ("sg0002_source_attempt_scope_v2", "sg0003_source_attempt_scope_v3"),
            False,
            "multiple current revisions",
        ),
    ],
)
def test_marker_reader_rejects_ambiguous_marker_shapes(
    marker_ddl: str,
    marker_rows: tuple[str, ...],
    marker_trigger: bool,
    expected_error: str,
) -> None:
    """Malformed marker layouts, attached triggers, and multiple heads fail closed."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(marker_ddl)
            for revision in marker_rows:
                connection.exec_driver_sql(
                    "INSERT INTO alembic_version_source_gate (version_num) VALUES (?)",
                    (revision,),
                )
            if marker_trigger:
                connection.exec_driver_sql(
                    "CREATE TRIGGER reject_marker_update "
                    "BEFORE UPDATE ON alembic_version_source_gate BEGIN "
                    "SELECT RAISE(ABORT, 'marker is guarded'); END"
                )

            with pytest.raises(RuntimeError, match=expected_error):
                schema_contract.read_migration_marker(connection)
    finally:
        engine.dispose()


def test_v3_marker_rejects_missing_singleton_clock_row() -> None:
    """A structurally exact v3 schema still needs the source-gate clock row."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            _create_v3_schema(connection)
            connection.exec_driver_sql("DELETE FROM od_source_gate_clock")

            with pytest.raises(RuntimeError, match="singleton clock row"):
                schema_contract.validate_migration_state(connection)
    finally:
        engine.dispose()
