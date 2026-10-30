"""Acceptance cases for explicit source-gate initialization of empty SQLite files."""

from __future__ import annotations

import runpy
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateTable

from alembic import command
from alembic_source_gate import schema_contract
from alembic_source_gate.path_contract import validate_existing_database_path
from scripts import migrate_source_gate

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "alembic_source_gate.ini"
DATA_VERSION_TABLE = "alembic_version_data"


def _upgrade(
    path: Path,
    revision: str = "head",
    *,
    initialize_empty: bool = False,
    require_empty: bool = False,
) -> None:
    """Upgrade one explicitly selected disposable test database."""
    config = Config(str(CONFIG_PATH))
    config.attributes["source_gate_path"] = validate_existing_database_path(path)
    config.attributes["source_gate_initialize_empty"] = initialize_empty
    config.attributes["source_gate_require_empty"] = require_empty
    command.upgrade(config, revision)


def _create_legacy_v1_database(path: Path) -> None:
    """Create a 0015-compatible fixture with unrelated data-chain state."""
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.begin() as connection:
            schema_contract._apply_v1_reference(connection)
            connection.exec_driver_sql(
                "CREATE TABLE warehouse_sentinel (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO warehouse_sentinel (id, payload) VALUES (1, 'preserve-me')"
            )
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version_data (version_num VARCHAR(64) NOT NULL PRIMARY KEY)"
            )
            connection.exec_driver_sql(
                "INSERT INTO alembic_version_data (version_num) "
                "VALUES ('0014_sina_fzb_slot_reconciliation')"
            )
            _insert_historical_attempt(connection)
    finally:
        engine.dispose()


def _insert_historical_attempt(connection: Connection) -> None:
    """Add a valid immutable reservation that initialization must preserve."""
    connection.exec_driver_sql(
        "INSERT INTO od_source_attempt_reservations "
        "(attempt_id, request_id, attempt_no, grant_id, expected_revision, source, "
        "canonical_model, operation, principal_scope, host, outcome, decided_at_us, "
        "reserved_at_us) VALUES "
        "('historical-attempt', 'historical-request', 1, 'legacy-grant', 1, 'fuyao', "
        "'model', 'query', 'worker', 'api.example', 'RESERVED', 10, 10)"
    )


def _sqlite_rows(path: Path, statement: str) -> list[tuple[object, ...]]:
    """Read deterministic rows from one disposable SQLite migration fixture."""
    with closing(sqlite3.connect(f"file:{path}?mode=rw", uri=True)) as connection:
        return connection.execute(statement).fetchall()


def _source_gate_marker(path: Path) -> str | None:
    """Return the source-gate marker, if Alembic has created it."""
    tables = _sqlite_rows(
        path,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version_source_gate'",
    )
    if not tables:
        return None
    rows = _sqlite_rows(path, "SELECT version_num FROM alembic_version_source_gate")
    return str(rows[0][0]) if len(rows) == 1 else None


def _user_schema_objects(path: Path) -> set[str]:
    """Return all non-internal SQLite schema object names."""
    return {
        str(row[0])
        for row in _sqlite_rows(
            path,
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index', 'view', 'trigger')",
        )
        if not str(row[0]).startswith("sqlite_")
    }


def _mark_existing_revision(connection: Connection, revision: str) -> None:
    """Write a fixture marker for an already-applied source-gate revision."""
    connection.exec_driver_sql(
        "CREATE TABLE alembic_version_source_gate (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
    )
    connection.exec_driver_sql(
        "INSERT INTO alembic_version_source_gate (version_num) VALUES (:revision)",
        {"revision": revision},
    )


def _create_marked_gate_database(path: Path, revision: str) -> None:
    """Build the exact schema corresponding to an existing marker fixture."""
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.begin() as connection:
            schema_contract._apply_v1_reference(connection)
            if revision in {schema_contract.V2_REVISION, schema_contract.V3_REVISION}:
                schema_contract.create_scope_v2(connection)
            if revision == schema_contract.V3_REVISION:
                schema_contract.create_scope_v3(connection)
            if revision == schema_contract.V4_REVISION:
                schema_contract.create_scope_v2(connection)
                schema_contract.create_scope_v3(connection)
                schema_contract.create_one_shot_v4(connection)
            _mark_existing_revision(connection, revision)
    finally:
        engine.dispose()


def test_explicit_empty_initialization_builds_source_gate_only(tmp_path: Path) -> None:
    """The opt-in creates the pinned v1-v4 schema without a data-chain head."""
    path = tmp_path / "empty-source-gate.sqlite"
    path.touch()

    _upgrade(path, initialize_empty=True, require_empty=True)

    assert _source_gate_marker(path) == schema_contract.V4_REVISION
    assert DATA_VERSION_TABLE not in _user_schema_objects(path)
    expected_gate_tables = (
        schema_contract.V1_TABLES
        | {schema_contract.V2_TABLE, schema_contract.V3_TABLE}
        | {schema_contract.V4_CONFIG_TABLE, schema_contract.V4_PERMIT_TABLE}
    )
    actual_gate_tables = {
        name for name in _user_schema_objects(path) if name.startswith("od_source_")
    }
    assert actual_gate_tables == expected_gate_tables
    assert _sqlite_rows(
        path,
        "SELECT clock_id, highwater_us FROM od_source_gate_clock",
    ) == [(1, 0)]
    empty_counts_sql = (
        "SELECT (SELECT COUNT(*) FROM od_source_gate_policies), "
        "(SELECT COUNT(*) FROM od_source_gate_source_bindings), "
        "(SELECT COUNT(*) FROM od_source_gate_state), "
        "(SELECT COUNT(*) FROM od_source_grants), "
        "(SELECT COUNT(*) FROM od_source_grant_hosts), "
        "(SELECT COUNT(*) FROM od_source_grant_host_sets), "
        "(SELECT COUNT(*) FROM od_source_grant_events), "
        "(SELECT COUNT(*) FROM od_source_attempt_reservations), "
        "(SELECT COUNT(*) FROM od_source_attempt_scope_v2), "
        "(SELECT COUNT(*) FROM od_source_attempt_scope_v3), "
        "(SELECT COUNT(*) FROM od_source_one_shot_configs), "
        "(SELECT COUNT(*) FROM od_source_one_shot_permits)"
    )
    assert _sqlite_rows(path, empty_counts_sql) == [(0,) * 12]

    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.connect() as connection:
            assert (
                schema_contract.validate_migration_state(connection) == schema_contract.V4_REVISION
            )
    finally:
        engine.dispose()


def test_empty_initialization_requires_explicit_opt_in_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """The default adoption path refuses an empty file before any schema write."""
    path = tmp_path / "empty-default.sqlite"
    path.touch()

    with pytest.raises(RuntimeError, match="--initialize-empty"):
        _upgrade(path)

    assert _user_schema_objects(path) == set()
    assert _source_gate_marker(path) is None


def test_empty_initialization_resumes_after_sg0000_marker(tmp_path: Path) -> None:
    """A committed V0 intermediate marker is recognized and upgrades forward."""
    path = tmp_path / "source-gate-v0-resume.sqlite"
    path.touch()

    _upgrade(
        path,
        schema_contract.V0_REVISION,
        initialize_empty=True,
        require_empty=True,
    )
    assert _source_gate_marker(path) == schema_contract.V0_REVISION
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.connect() as connection:
            assert (
                schema_contract.validate_migration_state(connection) == schema_contract.V0_REVISION
            )
    finally:
        engine.dispose()

    _upgrade(path)
    assert _source_gate_marker(path) == schema_contract.V4_REVISION
    assert DATA_VERSION_TABLE not in _user_schema_objects(path)


@pytest.mark.parametrize(
    "revision",
    [
        schema_contract.V1_REVISION,
        schema_contract.V2_REVISION,
        schema_contract.V3_REVISION,
        schema_contract.V4_REVISION,
    ],
)
def test_existing_revision_markers_advance_without_reinitializing(
    tmp_path: Path,
    revision: str,
) -> None:
    """Previously marked sg0001-sg0004 databases retain their schema and marker data."""
    path = tmp_path / f"existing-{revision}.sqlite"
    path.touch()
    _create_marked_gate_database(path, revision)

    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.connect() as connection:
            assert schema_contract.validate_migration_state(connection) == revision
    finally:
        engine.dispose()

    _upgrade(path)

    assert _source_gate_marker(path) == schema_contract.V4_REVISION
    assert (
        _sqlite_rows(
            path,
            "SELECT attempt_id FROM od_source_attempt_reservations",
        )
        == []
    )
    assert DATA_VERSION_TABLE not in _user_schema_objects(path)


def test_unmarked_legacy_0015_gate_and_data_marker_are_preserved(tmp_path: Path) -> None:
    """The default CLI still adopts the exact legacy gate without touching data state."""
    path = tmp_path / "legacy-0015.sqlite"
    path.touch()
    _create_legacy_v1_database(path)

    assert migrate_source_gate.main(["--database-path", str(path)]) == 0

    assert _source_gate_marker(path) == schema_contract.V4_REVISION
    assert _sqlite_rows(path, "SELECT id, payload FROM warehouse_sentinel") == [(1, "preserve-me")]
    assert _sqlite_rows(
        path,
        "SELECT version_num FROM alembic_version_data",
    ) == [("0014_sina_fzb_slot_reconciliation",)]
    assert _sqlite_rows(
        path,
        "SELECT attempt_id FROM od_source_attempt_reservations",
    ) == [("historical-attempt",)]
    legacy_sidecar_counts_sql = (
        "SELECT (SELECT COUNT(*) FROM od_source_attempt_scope_v2), "
        "(SELECT COUNT(*) FROM od_source_attempt_scope_v3)"
    )
    assert _sqlite_rows(path, legacy_sidecar_counts_sql) == [(0, 0)]


def test_partial_gate_is_not_treated_as_an_empty_database(tmp_path: Path) -> None:
    """Opt-in initialization rejects partial gate objects without repairing them."""
    path = tmp_path / "partial-gate.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE od_source_attempt_reservations (attempt_id TEXT PRIMARY KEY)"
        )
        connection.execute("CREATE TABLE warehouse_sentinel (payload TEXT NOT NULL)")
        connection.execute("INSERT INTO warehouse_sentinel VALUES ('preserve-me')")

    with pytest.raises(RuntimeError):
        _upgrade(path, initialize_empty=True, require_empty=True)

    assert _source_gate_marker(path) is None
    assert _user_schema_objects(path) == {
        "od_source_attempt_reservations",
        "warehouse_sentinel",
    }
    assert _sqlite_rows(path, "SELECT payload FROM warehouse_sentinel") == [("preserve-me",)]


def test_forged_empty_source_gate_marker_is_rejected_even_with_opt_in(
    tmp_path: Path,
) -> None:
    """An operator opt-in does not adopt a pre-existing empty Alembic marker."""
    path = tmp_path / "forged-empty-marker.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE alembic_version_source_gate "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )

    with pytest.raises(RuntimeError, match="without a revision"):
        _upgrade(path, initialize_empty=True)

    assert _source_gate_marker(path) is None
    assert _user_schema_objects(path) == {"alembic_version_source_gate"}
    assert not (schema_contract.V1_TABLES & _user_schema_objects(path))


def test_cli_entrypoint_carries_explicit_empty_opt_in_and_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real script entrypoint validates its target and passes the opt-in to Alembic."""
    path = tmp_path / "cli-empty.sqlite"
    path.touch()
    captured: dict[str, object] = {}

    def capture_upgrade(config: Config, revision: str) -> None:
        captured["config"] = config
        captured["revision"] = revision

    monkeypatch.setattr(migrate_source_gate.command, "upgrade", capture_upgrade)
    monkeypatch.setattr(
        sys,
        "path",
        [entry for entry in sys.path if entry != str(PROJECT_ROOT)],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(PROJECT_ROOT / "scripts" / "migrate_source_gate.py"),
            "--database-path",
            str(path),
            "--initialize-empty",
        ],
    )
    with pytest.raises(SystemExit) as result:
        runpy.run_path(
            str(PROJECT_ROOT / "scripts" / "migrate_source_gate.py"),
            run_name="__main__",
        )

    assert result.value.code == 0
    assert str(PROJECT_ROOT) in sys.path
    config = captured["config"]
    assert isinstance(config, Config)
    assert config.attributes["source_gate_path"] == path.resolve()
    identity = config.attributes["source_gate_file_identity"]
    assert identity.device == path.stat().st_dev
    assert identity.inode == path.stat().st_ino
    assert config.attributes["source_gate_initialize_empty"] is True
    assert config.attributes["source_gate_require_empty"] is True
    assert captured["revision"] == "head"
    assert path.stat().st_size == 0


@pytest.mark.parametrize(
    ("path_kind", "expected_error"),
    [
        ("missing", "must already exist"),
        ("relative", "absolute and canonical"),
        ("symlink", "regular, non-symlink file"),
        ("directory", "regular, non-symlink file"),
    ],
)
def test_cli_rejects_invalid_database_paths_before_alembic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_kind: str,
    expected_error: str,
) -> None:
    """The actual CLI refuses absent or ambiguous targets before Alembic runs."""
    target = tmp_path / "target.sqlite"
    target.touch()
    if path_kind == "missing":
        candidate = tmp_path / "missing.sqlite"
    elif path_kind == "relative":
        candidate = Path("relative-source-gate.sqlite")
    elif path_kind == "symlink":
        candidate = tmp_path / "linked.sqlite"
        candidate.symlink_to(target)
    else:
        candidate = tmp_path / "directory.sqlite"
        candidate.mkdir()

    def fail_upgrade(_config: Config, _revision: str) -> None:
        pytest.fail("invalid CLI target reached Alembic")

    monkeypatch.setattr(migrate_source_gate.command, "upgrade", fail_upgrade)

    with pytest.raises(RuntimeError, match=expected_error):
        migrate_source_gate.main(["--database-path", str(candidate), "--initialize-empty"])

    assert target.read_bytes() == b""
    if path_kind == "missing":
        assert not candidate.exists()
    elif path_kind == "symlink":
        assert candidate.is_symlink()


def test_require_empty_rejects_existing_v1_schema_and_rows_unchanged(
    tmp_path: Path,
) -> None:
    """Fresh-empty mode refuses a valid legacy v1 database without modifying it."""
    path = tmp_path / "existing-v1-not-empty.sqlite"
    path.touch()
    _create_legacy_v1_database(path)
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="require-empty"):
        _upgrade(path, initialize_empty=True, require_empty=True)

    assert path.read_bytes() == before
    assert _source_gate_marker(path) is None
    assert _sqlite_rows(path, "SELECT payload FROM warehouse_sentinel") == [("preserve-me",)]
    assert _sqlite_rows(
        path,
        "SELECT attempt_id FROM od_source_attempt_reservations",
    ) == [("historical-attempt",)]
    assert _sqlite_rows(path, "SELECT version_num FROM alembic_version_data") == [
        ("0014_sina_fzb_slot_reconciliation",)
    ]


def test_require_empty_rejects_marked_gate_without_changing_marker_or_rows(
    tmp_path: Path,
) -> None:
    """Fresh-empty mode refuses an existing source-gate marker and preserves data."""
    path = tmp_path / "marked-gate-not-empty.sqlite"
    path.touch()
    _create_marked_gate_database(path, schema_contract.V1_REVISION)
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.begin() as connection:
            _insert_historical_attempt(connection)
    finally:
        engine.dispose()
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="require-empty"):
        _upgrade(path, initialize_empty=True, require_empty=True)

    assert path.read_bytes() == before
    assert _source_gate_marker(path) == schema_contract.V1_REVISION
    assert _sqlite_rows(
        path,
        "SELECT attempt_id FROM od_source_attempt_reservations",
    ) == [("historical-attempt",)]


def test_require_empty_rechecks_after_write_lock_before_any_revision_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schema object committed after preflight is refused under the write lock."""
    path = tmp_path / "schema-added-between-preflight-and-lock.sqlite"
    path.touch()
    original_exec_driver_sql = Connection.exec_driver_sql
    injected = False

    def add_concurrent_schema_before_lock(
        connection: Connection,
        statement: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal injected
        if statement == "BEGIN IMMEDIATE" and not injected:
            racer = sqlite3.connect(path)
            try:
                racer.execute("CREATE TABLE concurrent_sentinel (payload TEXT NOT NULL)")
                racer.execute("INSERT INTO concurrent_sentinel VALUES ('racing-writer')")
                racer.commit()
            finally:
                racer.close()
            injected = True
        return original_exec_driver_sql(connection, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", add_concurrent_schema_before_lock)

    with pytest.raises(RuntimeError, match="require-empty"):
        _upgrade(path, initialize_empty=True, require_empty=True)

    assert injected
    assert _source_gate_marker(path) is None
    assert _user_schema_objects(path) == {"concurrent_sentinel"}
    assert _sqlite_rows(path, "SELECT payload FROM concurrent_sentinel") == [("racing-writer",)]


def test_require_empty_writer_lock_contention_fails_without_schema_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A competing SQLite writer prevents bootstrap and leaves the empty target empty."""
    path = tmp_path / "contended-empty.sqlite"
    path.touch()
    competing_writer = sqlite3.connect(path, timeout=0.1)
    competing_writer.execute("BEGIN IMMEDIATE")
    original_connect = sqlite3.connect

    def short_timeout_connect(
        database: str | Path,
        *args: Any,
        **kwargs: Any,
    ) -> sqlite3.Connection:
        if kwargs.get("uri") is True and "mode=rw" in str(database):
            kwargs["timeout"] = 0.01
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", short_timeout_connect)
    try:
        with pytest.raises(OperationalError, match="locked"):
            _upgrade(path, initialize_empty=True, require_empty=True)
    finally:
        competing_writer.rollback()
        competing_writer.close()

    assert _user_schema_objects(path) == set()
    assert _source_gate_marker(path) is None


def test_preexisting_empty_data_version_marker_is_not_initialized(tmp_path: Path) -> None:
    """A data-chain marker makes the file non-empty and is never stamped or altered."""
    path = tmp_path / "data-marker-only.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE alembic_version_data (version_num VARCHAR(64) NOT NULL PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO alembic_version_data (version_num) "
            "VALUES ('0014_sina_fzb_slot_reconciliation')"
        )

    with pytest.raises(RuntimeError):
        _upgrade(path, initialize_empty=True, require_empty=True)

    assert _source_gate_marker(path) is None
    assert _sqlite_rows(path, "SELECT version_num FROM alembic_version_data") == [
        ("0014_sina_fzb_slot_reconciliation",)
    ]
    assert _user_schema_objects(path) == {DATA_VERSION_TABLE}


@pytest.mark.parametrize(
    ("injected_state", "expected_error"),
    [
        ("unexpected-schema", "unexpected schema objects"),
        ("nonempty-marker", "non-empty or modified marker"),
    ],
)
def test_sg0000_rolls_back_if_new_marker_is_modified_before_v1_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    injected_state: str,
    expected_error: str,
) -> None:
    """The initial revision refuses a marker changed after Alembic creates it."""
    path = tmp_path / f"modified-initial-marker-{injected_state}.sqlite"
    path.touch()
    original_execute_ddl = Connection._execute_ddl
    original_exec_driver_sql = Connection.exec_driver_sql
    injected = False

    def modify_new_marker(
        connection: Connection,
        ddl: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal injected
        if (
            isinstance(ddl, CreateTable)
            and ddl.element.name == "alembic_version_source_gate"
            and not injected
        ):
            result = original_execute_ddl(connection, ddl, *args, **kwargs)
            if injected_state == "unexpected-schema":
                original_exec_driver_sql(
                    connection,
                    "CREATE TABLE bootstrap_sentinel (id INTEGER PRIMARY KEY)",
                )
            else:
                original_exec_driver_sql(
                    connection,
                    "INSERT INTO alembic_version_source_gate (version_num) "
                    "VALUES ('forged-revision')",
                )
            injected = True
            return result
        return original_execute_ddl(connection, ddl, *args, **kwargs)

    monkeypatch.setattr(Connection, "_execute_ddl", modify_new_marker)

    with pytest.raises(RuntimeError, match=expected_error):
        _upgrade(path, initialize_empty=True, require_empty=True)

    assert injected
    assert _user_schema_objects(path) == set()
    assert _source_gate_marker(path) is None
