"""Offline regression tests for the standalone source-gate migration chain."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from alembic import command
from alembic_source_gate import schema_contract
from alembic_source_gate.path_contract import (
    DatabaseFileIdentity,
    assert_database_file_identity,
    inspect_existing_database_path,
    validate_existing_database_path,
)
from scripts import migrate_source_gate

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "alembic_source_gate.ini"


def _insert_reserved_attempt(
    connection: sqlite3.Connection | Connection,
    attempt_id: str,
    request_id: str,
    attempt_no: int,
) -> None:
    """Add a minimal valid synthetic reservation for sidecar constraints."""
    statement = (
        "INSERT INTO od_source_attempt_reservations "
        "(attempt_id, request_id, attempt_no, grant_id, expected_revision, source, "
        "canonical_model, operation, principal_scope, host, outcome, decided_at_us, "
        "reserved_at_us) VALUES (?, ?, ?, 'g1', 1, 'fuyao', 'model', 'query', 'p', "
        "'api.example', 'RESERVED', 10, 10)"
    )
    parameters = (attempt_id, request_id, attempt_no)
    if isinstance(connection, sqlite3.Connection):
        connection.execute(statement, parameters)
    else:
        connection.exec_driver_sql(statement, parameters)


def _create_v1_database(path: Path) -> None:
    """Create a disposable v1 database by applying only the pinned gate revision."""
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.connect() as connection:
            schema_contract._apply_v1_reference(connection)
            connection.exec_driver_sql(
                "CREATE TABLE warehouse_sentinel (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO warehouse_sentinel (id, value) VALUES (1, 'keep-me')"
            )
            _insert_reserved_attempt(
                connection,
                "historical-attempt",
                "historical-request",
                1,
            )
            connection.commit()
    finally:
        engine.dispose()


def _upgrade(path: Path, revision: str = "head") -> None:
    """Invoke the independent chain with an explicit test-file path."""
    config = Config(str(CONFIG_PATH))
    config.attributes["source_gate_path"] = validate_existing_database_path(path)
    command.upgrade(config, revision)


def _downgrade(path: Path, revision: str) -> None:
    """Invoke a downgrade against one explicitly named test file."""
    config = Config(str(CONFIG_PATH))
    config.attributes["source_gate_path"] = validate_existing_database_path(path)
    command.downgrade(config, revision)


def _sqlite_rows(
    path: Path,
    statement: str,
    parameters: tuple[object, ...] = (),
) -> list[tuple[object, ...]]:
    """Read deterministic rows from a disposable migration-test database."""
    with closing(sqlite3.connect(f"file:{path}?mode=rw", uri=True)) as connection:
        return connection.execute(statement, parameters).fetchall()


def _marker(path: Path) -> str | None:
    """Return the standalone marker revision, if the version table exists."""
    tables = _sqlite_rows(
        path,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version_source_gate'",
    )
    if not tables:
        return None
    rows = _sqlite_rows(path, "SELECT version_num FROM alembic_version_source_gate")
    if not rows or not isinstance(rows[0][0], str):
        return None
    return rows[0][0]


def test_complete_v1_is_adopted_and_unrelated_data_is_preserved(tmp_path: Path) -> None:
    """Adoption changes only the private revision marker and leaves data intact."""
    path = tmp_path / "source-gate.sqlite"
    _create_v1_database(path)

    _upgrade(path)

    assert _marker(path) == schema_contract.V2_REVISION
    assert _sqlite_rows(path, "SELECT id, value FROM warehouse_sentinel") == [(1, "keep-me")]
    assert len(_sqlite_rows(path, "SELECT * FROM od_source_attempt_scope_v2")) == 0
    assert _sqlite_rows(
        path,
        "SELECT attempt_id FROM od_source_attempt_reservations",
    ) == [("historical-attempt",)]


def test_partial_v1_schema_fails_without_creating_marker_or_sidecar(tmp_path: Path) -> None:
    """A partial gate is refused before any migration marker or table is written."""
    path = tmp_path / "partial.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE od_source_attempt_reservations (attempt_id TEXT)")
        connection.execute(
            "CREATE TABLE warehouse_sentinel (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO warehouse_sentinel VALUES (1, 'keep-me')")

    with pytest.raises(RuntimeError, match="schema|version|marker"):
        _upgrade(path)

    assert _marker(path) is None
    assert _sqlite_rows(path, "SELECT id, value FROM warehouse_sentinel") == [(1, "keep-me")]
    assert not _sqlite_rows(
        path,
        "SELECT name FROM sqlite_master WHERE name='od_source_attempt_scope_v2'",
    )


@pytest.mark.parametrize("object_name", ["od_source_unreviewed", "OD_SOURCE_UNREVIEWED"])
def test_unknown_gate_object_blocks_adoption_without_mutation(
    tmp_path: Path, object_name: str
) -> None:
    """Unknown source-gate objects are not silently adopted or dropped."""
    path = tmp_path / "unknown.sqlite"
    _create_v1_database(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"CREATE TABLE {object_name} (id INTEGER PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="unexpected|schema"):
        _upgrade(path)

    assert _marker(path) is None
    assert _sqlite_rows(
        path,
        "SELECT name FROM sqlite_master WHERE name = ?",
        (object_name,),
    )


def test_missing_or_symlinked_paths_are_rejected_without_creation(tmp_path: Path) -> None:
    """The migration target must already exist and may not traverse symlinks."""
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(RuntimeError, match="exist"):
        validate_existing_database_path(missing)
    assert not missing.exists()

    actual = tmp_path / "actual.sqlite"
    actual.touch()
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(actual)
    with pytest.raises(RuntimeError, match="symbolic|symlink"):
        validate_existing_database_path(alias)


def test_path_contract_rejects_ambiguous_and_nonregular_inputs(tmp_path: Path) -> None:
    """Path validation rejects malformed inputs and symlinked parent directories."""
    actual = tmp_path / "actual.sqlite"
    actual.touch()
    assert validate_existing_database_path(str(actual)) == actual

    for invalid in (None, ""):
        with pytest.raises(RuntimeError, match="explicit absolute path"):
            inspect_existing_database_path(invalid)

    with pytest.raises(RuntimeError, match="absolute and canonical"):
        validate_existing_database_path(Path("relative.sqlite"))
    with pytest.raises(RuntimeError, match="absolute and canonical"):
        validate_existing_database_path(tmp_path / ".." / "traversal.sqlite")
    with pytest.raises(RuntimeError, match="regular"):
        validate_existing_database_path(tmp_path)

    real_directory = tmp_path / "real"
    real_directory.mkdir()
    target = real_directory / "target.sqlite"
    target.touch()
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    with pytest.raises(RuntimeError, match="traverse symbolic links"):
        validate_existing_database_path(linked_directory / "target.sqlite")


def test_pinned_reference_rejects_unavailable_changed_and_mislabeled_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adoption fails closed when its pinned migration source cannot be trusted."""
    reference_path = schema_contract._reference_path()
    expected_digest = schema_contract.V1_REFERENCE_SHA256

    monkeypatch.setattr(schema_contract, "_reference_path", lambda: tmp_path / "missing.py")
    with pytest.raises(RuntimeError, match="reference is unavailable"):
        schema_contract._load_v1_reference()

    monkeypatch.setattr(schema_contract, "_reference_path", lambda: reference_path)
    monkeypatch.setattr(schema_contract, "V1_REFERENCE_SHA256", "0" * 64)
    with pytest.raises(RuntimeError, match="reference changed"):
        schema_contract._load_v1_reference()

    monkeypatch.setattr(schema_contract, "V1_REFERENCE_SHA256", expected_digest)
    monkeypatch.setattr(schema_contract, "V1_REFERENCE_REVISION", "unexpected_revision")
    with pytest.raises(RuntimeError, match="identity is invalid"):
        schema_contract._load_v1_reference()


def test_reference_builder_rejects_unsupported_dialect_and_table_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scratch baseline itself must stay SQLite and match its expected table set."""
    unsupported_connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    with pytest.raises(RuntimeError, match="in-memory SQLite"):
        schema_contract._apply_v1_reference(unsupported_connection)
    with pytest.raises(RuntimeError, match="SQLite files only"):
        schema_contract.validate_v1_schema(unsupported_connection)
    with pytest.raises(RuntimeError, match="requires SQLite"):
        schema_contract.create_scope_v2(unsupported_connection)

    monkeypatch.setattr(
        schema_contract,
        "V1_TABLES",
        schema_contract.V1_TABLES | {"od_source_unexpected_baseline_table"},
    )
    with pytest.raises(RuntimeError, match="unexpected table set"):
        schema_contract._reference_schema(include_v2=False)


def test_marker_name_cannot_be_a_view() -> None:
    """A lookalike view cannot masquerade as Alembic's private marker table."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE VIEW alembic_version_source_gate AS "
                "SELECT 'sg0002_source_attempt_scope_v2' AS version_num"
            )
            with pytest.raises(RuntimeError, match="not a SQLite table"):
                schema_contract.read_migration_marker(connection)
    finally:
        engine.dispose()


def test_unknown_marker_revision_is_rejected() -> None:
    """A structurally valid marker still must name a known isolated revision."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version_source_gate "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
            connection.exec_driver_sql(
                "INSERT INTO alembic_version_source_gate (version_num) VALUES ('unknown')"
            )
            with pytest.raises(RuntimeError, match="marker is unknown"):
                schema_contract.validate_migration_state(connection)
    finally:
        engine.dispose()


def test_migration_cli_requires_an_explicit_database_path(tmp_path: Path) -> None:
    """The standalone CLI accepts an explicit path without opening it itself."""
    path = tmp_path / "source-gate.sqlite"
    with pytest.raises(SystemExit):
        migrate_source_gate.build_parser().parse_args([])

    arguments = migrate_source_gate.build_parser().parse_args(["--database-path", str(path)])
    assert arguments.database_path == path


def test_migration_cli_passes_explicit_target_to_alembic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI records the reviewed file identity and invokes the isolated chain."""
    path = tmp_path / "source-gate.sqlite"
    path.touch()
    invocation: dict[str, object] = {}

    def capture_upgrade(config: Config, revision: str) -> None:
        invocation["config"] = config
        invocation["revision"] = revision

    monkeypatch.setattr(migrate_source_gate.command, "upgrade", capture_upgrade)

    assert migrate_source_gate.main(["--database-path", str(path)]) == 0

    config = invocation["config"]
    assert isinstance(config, Config)
    assert config.attributes["source_gate_path"] == path.resolve()
    identity = config.attributes["source_gate_file_identity"]
    assert isinstance(identity, DatabaseFileIdentity)
    assert (identity.device, identity.inode) == (path.stat().st_dev, path.stat().st_ino)
    assert invocation["revision"] == "head"


def test_replaced_database_path_fails_identity_recheck(tmp_path: Path) -> None:
    """A path replacement after validation is detected before migration reads."""
    target = tmp_path / "source-gate.sqlite"
    target.touch()
    path, identity = inspect_existing_database_path(target)

    replacement = tmp_path / "replacement.sqlite"
    replacement.touch()
    replacement.replace(target)

    with pytest.raises(RuntimeError, match="identity changed"):
        assert_database_file_identity(path, identity)


def test_forged_empty_marker_is_rejected_before_upgrade(tmp_path: Path) -> None:
    """An empty standalone version table is ambiguous and is never adopted."""
    path = tmp_path / "empty-marker.sqlite"
    _create_v1_database(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE alembic_version_source_gate "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )

    with pytest.raises(RuntimeError, match="without a revision"):
        _upgrade(path)

    assert _marker(path) is None
    assert _sqlite_rows(
        path,
        "SELECT attempt_id FROM od_source_attempt_reservations",
    ) == [("historical-attempt",)]


def test_scope_sidecar_constraints_and_immutable_triggers(tmp_path: Path) -> None:
    """The v2 table binds a reservation and rejects malformed or mutable rows."""
    path = tmp_path / "scope-v2.sqlite"
    _create_v1_database(path)
    _upgrade(path)
    with closing(sqlite3.connect(f"file:{path}?mode=rw", uri=True)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_reserved_attempt(connection, "a1", "r1", 1)
        _insert_reserved_attempt(connection, "a2", "r2", 1)
        connection.execute(
            "INSERT INTO od_source_attempt_scope_v2 "
            "(attempt_id, scope_version, scope_json, scope_sha256, recorded_at_us) "
            "VALUES ('a1', 2, '{\"ok\":true}', ?, 10)",
            ("b" * 64,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE od_source_attempt_scope_v2 SET scope_json='{}' WHERE attempt_id='a1'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM od_source_attempt_scope_v2 WHERE attempt_id='a1'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO od_source_attempt_scope_v2 "
                "(attempt_id, scope_version, scope_json, scope_sha256, recorded_at_us) "
                "VALUES ('a2', 2, '{}', ?, 10)",
                ("B" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO od_source_attempt_scope_v2 "
                "(attempt_id, scope_version, scope_json, scope_sha256, recorded_at_us) "
                "VALUES ('a2', 2, ?, ?, 10)",
                ("x" * 16385, "d" * 64),
            )
        connection.rollback()


def test_v2_migration_failure_rolls_back_marker_and_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after sidecar creation rolls the whole fresh upgrade back."""
    path = tmp_path / "rollback.sqlite"
    _create_v1_database(path)
    invalid_delete_trigger = "CREATE TRIGGER broken DELETE"
    monkeypatch.setitem(
        schema_contract.__dict__,
        "V2_OBJECT_SQL",
        (*schema_contract.V2_OBJECT_SQL[:2], invalid_delete_trigger),
    )

    with pytest.raises(SQLAlchemyError):
        _upgrade(path)

    assert _marker(path) is None
    assert not _sqlite_rows(
        path,
        "SELECT name FROM sqlite_master WHERE name='od_source_attempt_scope_v2'",
    )
    assert _sqlite_rows(path, "SELECT id, value FROM warehouse_sentinel") == [(1, "keep-me")]


def test_v2_downgrade_refuses_to_drop_persisted_scope_rows(tmp_path: Path) -> None:
    """Once a scope is recorded, downgrade cannot discard the audit sidecar."""
    path = tmp_path / "no-loss.sqlite"
    _create_v1_database(path)
    _upgrade(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_reserved_attempt(connection, "attempt", "request", 1)
        connection.execute(
            "INSERT INTO od_source_attempt_scope_v2 "
            "(attempt_id, scope_version, scope_json, scope_sha256, recorded_at_us) "
            "VALUES ('attempt', 2, '{}', ?, 10)",
            ("c" * 64,),
        )

    with pytest.raises(RuntimeError, match="evidence"):
        _downgrade(path, schema_contract.V1_REVISION)

    assert _marker(path) == schema_contract.V2_REVISION
