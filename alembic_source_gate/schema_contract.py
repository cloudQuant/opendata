"""Exact schema and marker contract for the isolated source-gate chain.

The v1 adoption baseline is generated in a private in-memory SQLite database
by the pinned original revision 0015. No warehouse migration chain is loaded.
"""

from __future__ import annotations

import hashlib
import re
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import TYPE_CHECKING

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

if TYPE_CHECKING:
    from types import CodeType, ModuleType

    from sqlalchemy.engine import Connection

VERSION_TABLE = "alembic_version_source_gate"
V1_REVISION = "sg0001_gate_v1_adopt"
V2_REVISION = "sg0002_source_attempt_scope_v2"
V3_REVISION = "sg0003_source_attempt_scope_v3"
V1_REFERENCE_SHA256 = "74d9b163a309eb37b35200f83d7b7d224fc3f05a433b59847f4fb03b3d865cf5"
V1_REFERENCE_REVISION = "0015_source_attempt_gate"
V1_REFERENCE_PARENT = "0014_sina_fzb_slot_reconciliation"
V2_SCOPE_JSON_FIELDS = (
    "attempt_id",
    "attempt_no",
    "budget_binding_sha256",
    "budget_policy_revision",
    "budget_policy_sha256",
    "budget_quota_key",
    "canonical_model",
    "capture_intent_id",
    "expected_grant_revision",
    "grant_id",
    "host",
    "operation",
    "principal_scope",
    "request_id",
    "request_summary_sha256",
    "run_key",
    "scope_version",
    "source",
    "source_policy_id",
    "source_policy_revision",
    "source_policy_sha256",
)
V3_SCOPE_JSON_FIELDS = (
    "attempt_id",
    "attempt_no",
    "budget_binding_sha256",
    "budget_policy_revision",
    "budget_policy_sha256",
    "budget_quota_key",
    "canonical_model",
    "capture_intent_id",
    "expected_grant_revision",
    "expected_rights_evidence_reference",
    "grant_id",
    "host",
    "operation",
    "principal_scope",
    "request_host",
    "request_id",
    "request_method",
    "request_path",
    "request_scheme",
    "request_summary_sha256",
    "run_key",
    "scope_version",
    "source",
    "source_policy_id",
    "source_policy_revision",
    "source_policy_sha256",
)

V1_TABLES = frozenset(
    {
        "od_source_gate_clock",
        "od_source_gate_policies",
        "od_source_gate_source_bindings",
        "od_source_gate_state",
        "od_source_grants",
        "od_source_grant_hosts",
        "od_source_grant_host_sets",
        "od_source_grant_events",
        "od_source_attempt_reservations",
    }
)
V2_TABLE = "od_source_attempt_scope_v2"
V3_TABLE = "od_source_attempt_scope_v3"
V2_OBJECT_SQL = (
    """CREATE TABLE od_source_attempt_scope_v2 (
        attempt_id TEXT NOT NULL PRIMARY KEY,
        scope_version INTEGER NOT NULL CHECK (scope_version = 2),
        scope_json TEXT NOT NULL CHECK (
            length(CAST(scope_json AS BLOB)) BETWEEN 1 AND 16384
        ),
        scope_sha256 TEXT NOT NULL CHECK (
            length(scope_sha256) = 64 AND scope_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        recorded_at_us INTEGER NOT NULL,
        FOREIGN KEY (attempt_id)
            REFERENCES od_source_attempt_reservations (attempt_id)
            ON DELETE RESTRICT
    )""",
    """CREATE TRIGGER trg_od_source_attempt_scope_v2_immutable_update
        BEFORE UPDATE ON od_source_attempt_scope_v2
        BEGIN
            SELECT RAISE(ABORT, 'source attempt scope v2 rows are immutable');
        END""",
    """CREATE TRIGGER trg_od_source_attempt_scope_v2_immutable_delete
        BEFORE DELETE ON od_source_attempt_scope_v2
        BEGIN
            SELECT RAISE(ABORT, 'source attempt scope v2 rows are permanent');
        END""",
)
V3_OBJECT_SQL = (
    """CREATE TABLE od_source_attempt_scope_v3 (
        attempt_id TEXT NOT NULL PRIMARY KEY,
        scope_version INTEGER NOT NULL CHECK (scope_version = 3),
        scope_json TEXT NOT NULL CHECK (
            length(CAST(scope_json AS BLOB)) BETWEEN 1 AND 16384
        ),
        scope_sha256 TEXT NOT NULL CHECK (
            length(scope_sha256) = 64 AND scope_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        recorded_at_us INTEGER NOT NULL,
        FOREIGN KEY (attempt_id)
            REFERENCES od_source_attempt_reservations (attempt_id)
            ON DELETE RESTRICT
    )""",
    """CREATE TRIGGER trg_od_source_attempt_scope_v3_immutable_update
        BEFORE UPDATE ON od_source_attempt_scope_v3
        BEGIN
            SELECT RAISE(ABORT, 'source attempt scope v3 rows are immutable');
        END""",
    """CREATE TRIGGER trg_od_source_attempt_scope_v3_immutable_delete
        BEFORE DELETE ON od_source_attempt_scope_v3
        BEGIN
            SELECT RAISE(ABORT, 'source attempt scope v3 rows are permanent');
        END""",
)


class _PinnedSourceLoader(SourceFileLoader):
    """Load the exact pinned migration bytes without consulting a .pyc cache."""

    def __init__(self, name: str, path: Path, source: bytes) -> None:
        super().__init__(name, str(path))
        self._verified_source = source

    def get_code(self, fullname: str) -> CodeType:
        """Compile the verified source bytes instead of loading cached bytecode."""
        if fullname != self.name:
            raise ImportError("pinned source gate module identity changed")
        return compile(
            self._verified_source,
            self.path,
            "exec",
            dont_inherit=True,
        )


def _normalize_sql(sql: str | None) -> str | None:
    """Normalize only insignificant whitespace in SQLite's stored SQL text."""
    if sql is None:
        return None
    return re.sub(r"\s+", " ", sql).strip().rstrip(";")


def _has_sqlite_ascii_prefix(value: str, prefix: str) -> bool:
    """Match SQLite's ASCII case-insensitive identifier prefix semantics."""
    candidate = value[: len(prefix)]
    return len(value) >= len(prefix) and candidate.isascii() and candidate.lower() == prefix


def _gate_objects(connection: Connection) -> dict[tuple[str, str, str], str | None]:
    """Return every SQLite schema object owned by or attached to the gate."""
    result: dict[tuple[str, str, str], str | None] = {}
    rows = connection.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    )
    for row in rows:
        object_type, name, table_name, sql = row
        if (
            _has_sqlite_ascii_prefix(name, "od_source_")
            or _has_sqlite_ascii_prefix(name, "trg_od_source_")
            or _has_sqlite_ascii_prefix(table_name, "od_source_")
        ):
            result[(object_type, name, table_name)] = _normalize_sql(sql)
    return result


def _reference_path() -> Path:
    """Return the immutable source path for the original v1 gate revision."""
    return (
        Path(__file__).resolve().parents[1]
        / "alembic_data"
        / "versions"
        / "20261027-0015_source_attempt_gate.py"
    )


def _load_v1_reference() -> ModuleType:
    """Load 0015 only after its registered revision and bytes match the pin."""
    path = _reference_path()
    try:
        source = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("pinned source gate v1 reference is unavailable") from exc
    if hashlib.sha256(source).hexdigest() != V1_REFERENCE_SHA256:
        raise RuntimeError("source gate v1 reference changed; refusing ambiguous adoption")

    # Execute only repository-owned 0015 after its exact bytes match the
    # hard-coded SHA-256 pin. The path is fixed by _reference_path(), not
    # caller-controlled. The loader compiles those verified bytes directly,
    # bypasses adjacent .pyc files, and this module is used only to build the
    # expected schema on an in-memory SQLite connection.
    loader = _PinnedSourceLoader("_source_gate_v1_reference", path, source)
    spec = spec_from_loader(loader.name, loader, origin=str(path))
    if spec is None:
        raise RuntimeError("pinned source gate v1 reference cannot be imported")
    module = module_from_spec(spec)
    loader.exec_module(module)
    if (
        getattr(module, "revision", None) != V1_REFERENCE_REVISION
        or getattr(module, "down_revision", None) != V1_REFERENCE_PARENT
        or not callable(getattr(module, "upgrade", None))
    ):
        raise RuntimeError("pinned source gate v1 revision identity is invalid")
    return module


def _apply_v1_reference(connection: Connection) -> None:
    """Create the exact 0015 reference schema on a caller-owned scratch DB."""
    if connection.dialect.name != "sqlite":
        raise RuntimeError("source gate schema reference requires an in-memory SQLite target")
    module = _load_v1_reference()
    migration_context = MigrationContext.configure(connection)
    # Bind the pinned migration's local ``op`` name to the scratch context.
    # Operations.context installs a module-global proxy; nesting it while a
    # real Alembic revision is running would clear that outer proxy on exit.
    module.__dict__["op"] = Operations(migration_context)
    module.upgrade()


def _reference_schema(
    *, include_v2: bool, include_v3: bool = False
) -> dict[tuple[str, str, str], str | None]:
    """Build the pinned expected schema in memory and return its gate objects."""
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    try:
        with engine.connect() as connection:
            _apply_v1_reference(connection)
            if include_v2:
                for statement in V2_OBJECT_SQL:
                    connection.exec_driver_sql(statement)
            if include_v3:
                for statement in V3_OBJECT_SQL:
                    connection.exec_driver_sql(statement)
            objects = _gate_objects(connection)
            tables = {
                name
                for object_type, name, _table_name in objects
                if object_type == "table" and name.startswith("od_source_")
            }
            expected_tables = (
                V1_TABLES
                | ({V2_TABLE} if include_v2 else set())
                | ({V3_TABLE} if include_v3 else set())
            )
            if tables != expected_tables:
                raise RuntimeError("pinned source gate reference has an unexpected table set")
            return objects
    finally:
        engine.dispose()


def _require_utf8_database(connection: Connection) -> None:
    """Require UTF-8 because the v2 byte bound is defined over UTF-8 bytes."""
    encoding = connection.exec_driver_sql("PRAGMA encoding").scalar_one()
    if encoding != "UTF-8":
        raise RuntimeError("source gate database encoding must be UTF-8")


def _require_source_gate_foreign_keys(connection: Connection) -> None:
    """Reject existing source-gate rows that violate their pinned references."""
    rows = connection.exec_driver_sql("PRAGMA foreign_key_check")
    invalid_tables = sorted({row[0] for row in rows if row[0] in V1_TABLES | {V2_TABLE, V3_TABLE}})
    if invalid_tables:
        names = ", ".join(invalid_tables)
        raise RuntimeError(f"source gate foreign-key check failed for: {names}")


def _require_v1_clock_row(connection: Connection) -> None:
    """Require the original singleton clock while preserving its high-water."""
    rows = connection.exec_driver_sql(
        "SELECT clock_id, highwater_us FROM od_source_gate_clock ORDER BY clock_id"
    ).all()
    if len(rows) != 1 or rows[0][0] != 1 or rows[0][1] < 0:
        raise RuntimeError("source gate v1 singleton clock row is missing or malformed")


def _compare_reference_schema(
    connection: Connection, *, include_v2: bool, include_v3: bool = False
) -> None:
    """Require the exact pinned DDL and trigger set for the selected revision."""
    expected = _reference_schema(include_v2=include_v2, include_v3=include_v3)
    actual = _gate_objects(connection)
    if actual == expected:
        return
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    drifted = sorted(key for key in set(actual) & set(expected) if actual[key] != expected[key])
    parts = []
    if missing:
        parts.append(f"missing={','.join(key[1] for key in missing)}")
    if extra:
        parts.append(f"unexpected={','.join(key[1] for key in extra)}")
    if drifted:
        parts.append(f"changed={','.join(key[1] for key in drifted)}")
    raise RuntimeError("source gate schema does not match the pinned revision: " + "; ".join(parts))


def validate_v1_schema(connection: Connection) -> None:
    """Validate complete v1 schema and data invariants without changing them."""
    if connection.dialect.name != "sqlite":
        raise RuntimeError("source gate migrations support SQLite files only")
    _require_utf8_database(connection)
    _compare_reference_schema(connection, include_v2=False)
    _require_v1_clock_row(connection)
    _require_source_gate_foreign_keys(connection)


def _version_table_exists(connection: Connection) -> bool:
    """Return whether the isolated source-gate marker table exists."""
    value = connection.exec_driver_sql(
        "SELECT type FROM sqlite_master WHERE name = :name",
        {"name": VERSION_TABLE},
    ).scalar_one_or_none()
    if value is None:
        return False
    if value != "table":
        raise RuntimeError("source gate migration marker is not a SQLite table")
    return True


def read_migration_marker(connection: Connection) -> str | None:
    """Read one unambiguous Alembic revision from the isolated version table."""
    if not _version_table_exists(connection):
        return None
    columns = connection.exec_driver_sql(f"PRAGMA table_info({VERSION_TABLE})").all()
    if len(columns) != 1:
        raise RuntimeError("source gate migration marker has an unknown column layout")
    _cid, name, column_type, not_null, default, primary_key = columns[0]
    if (
        name != "version_num"
        or column_type.upper() != "VARCHAR(32)"
        or not_null != 1
        or default is not None
        or primary_key != 1
    ):
        raise RuntimeError("source gate migration marker has an unknown column layout")
    indexes = connection.exec_driver_sql(f"PRAGMA index_list({VERSION_TABLE})").all()
    if len(indexes) != 1 or indexes[0][2] != 1 or indexes[0][3] != "pk":
        raise RuntimeError("source gate migration marker key is not the expected primary key")
    triggers = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=:name",
        {"name": VERSION_TABLE},
    ).all()
    if triggers:
        raise RuntimeError("source gate migration marker has unexpected triggers")
    rows = connection.exec_driver_sql("SELECT version_num FROM alembic_version_source_gate").all()
    if len(rows) > 1:
        raise RuntimeError("source gate migration marker has multiple current revisions")
    if not rows:
        raise RuntimeError("source gate migration marker exists without a revision")
    value = rows[0][0]
    if not isinstance(value, str):
        raise RuntimeError("source gate migration marker is malformed")
    return value


def validate_migration_state(connection: Connection) -> str | None:
    """Validate the marker and exact source-gate schema before or after upgrade."""
    marker = read_migration_marker(connection)
    if marker is None or marker == V1_REVISION:
        validate_v1_schema(connection)
        return marker
    if marker == V2_REVISION:
        _require_utf8_database(connection)
        _compare_reference_schema(connection, include_v2=True)
        _require_v1_clock_row(connection)
        _require_source_gate_foreign_keys(connection)
        return marker
    if marker == V3_REVISION:
        _require_utf8_database(connection)
        _compare_reference_schema(connection, include_v2=True, include_v3=True)
        _require_v1_clock_row(connection)
        _require_source_gate_foreign_keys(connection)
        return marker
    raise RuntimeError(f"source gate migration marker is unknown: {marker}")


def create_scope_v2(connection: Connection) -> None:
    """Create the immutable scope sidecar and its structural guards."""
    if connection.dialect.name != "sqlite":
        raise RuntimeError("source gate scope v2 requires SQLite")
    for statement in V2_OBJECT_SQL:
        connection.exec_driver_sql(statement)


def create_scope_v3(connection: Connection) -> None:
    """Create the immutable scope-v3 sidecar and its structural guards."""
    if connection.dialect.name != "sqlite":
        raise RuntimeError("source gate scope v3 requires SQLite")
    for statement in V3_OBJECT_SQL:
        connection.exec_driver_sql(statement)


__all__ = [
    "VERSION_TABLE",
    "V1_REVISION",
    "V2_REVISION",
    "V3_REVISION",
    "V2_SCOPE_JSON_FIELDS",
    "V3_SCOPE_JSON_FIELDS",
    "V2_TABLE",
    "V2_OBJECT_SQL",
    "V3_TABLE",
    "V3_OBJECT_SQL",
    "create_scope_v3",
    "create_scope_v2",
    "read_migration_marker",
    "validate_migration_state",
    "validate_v1_schema",
]
