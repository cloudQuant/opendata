"""Read-only inventory of the legacy AFI scheduler and warehouse schema.

The command deliberately does not import the AFI application, execute a legacy
script, modify a database, or expose parameter values. MySQL connections are
checked against the two explicitly approved local database endpoints and run
inside a verified read-only consistent snapshot. SQLite support exists only
for isolated tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath

from sqlalchemy import Column, Engine, MetaData, Table, create_engine, select, text
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.sqltypes import NullType

SCHEMA_VERSION = "1.0"
METADATA_DATABASE = "ai_for_investor"
WAREHOUSE_DATABASE = "akshare_data"
APPROVED_HOST = "127.0.0.1"
APPROVED_PORT = 3306
TASK_TABLE = "ak_scheduled_tasks"
SCRIPT_TABLE = "ak_data_scripts"
MAX_TASK_ROWS = 100_000
MAX_PARAMETER_BYTES = 1_048_576

_TASK_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id",),
    "script_id": ("script_id",),
    "schedule_type": ("schedule_type",),
    "expression": ("expression", "schedule_expression"),
    "is_active": ("is_active",),
    "last_execution": ("last_execution", "last_execution_at", "last_run_at", "last_run"),
    "next_execution": ("next_execution", "next_execution_at", "next_run_at", "next_run"),
    "timeout": ("timeout", "timeout_seconds"),
    "retries": ("retries", "max_retries", "retry_count"),
    "retry_on_failure": ("retry_on_failure",),
    "task_kind": ("task_kind",),
    "parameters": ("parameters", "params", "dependencies"),
}
_SCRIPT_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "script_id": ("script_id",),
    "module_path": ("module_path",),
    "function_name": ("function_name",),
    "target_table": ("target_table",),
    "is_active": ("is_active",),
    "source": ("source",),
    "parameters": ("parameters", "params", "dependencies"),
}
_CREDENTIAL_KEY_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:api[_-]?key|access[_-]?key|secret|password|passwd|token|"
    r"authorization|private[_-]?key|database[_-]?url|connection[_-]?string|dsn)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
_CREDENTIAL_VALUE_RE = re.compile(
    r"(?:bearer\s+\S+|(?:sk|ak|rk)-[a-z0-9_-]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"mysql(?:\+\w+)?://[^\s/:]+:[^\s@]+@)",
    re.IGNORECASE,
)


class RuntimeInventoryError(RuntimeError):
    """An expected fail-closed inventory error without sensitive details."""


@dataclass(frozen=True, slots=True)
class ApprovedEndpoint:
    """A local database role and its only permitted MySQL identity."""

    role: str
    database: str


def _canonical_json(value: object) -> bytes:
    """Serialize JSON-compatible values deterministically and within a bound."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_json_default,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeInventoryError("parameter_payload_not_canonical_json") from exc
    if len(encoded) > MAX_PARAMETER_BYTES:
        raise RuntimeInventoryError("parameter_payload_exceeds_limit")
    return encoded


def _json_default(value: object) -> str | dict[str, object]:
    """Encode the small set of scalar values database drivers may return."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return {
            "binary_sha256": hashlib.sha256(value).hexdigest(),
            "byte_length": len(value),
        }
    raise TypeError(type(value).__name__)


def _report_scalar(value: object) -> object:
    """Normalize database scalars to safe JSON values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _json_default(value)


def _parameter_summary(value: object) -> dict[str, object]:
    """Return a digest and key names without returning any parameter values."""
    payload = value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            payload = value
    encoded = _canonical_json(payload)
    names: set[str] = set()
    credential_flag = False

    def visit(item: object, depth: int = 0) -> None:
        nonlocal credential_flag
        if depth > 12:
            raise RuntimeInventoryError("parameter_payload_depth_exceeded")
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                if not isinstance(raw_key, str):
                    credential_flag = True
                    continue
                key = raw_key.strip()
                if key:
                    names.add(key[:100])
                if _CREDENTIAL_KEY_RE.search(key):
                    credential_flag = True
                visit(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, str) and _CREDENTIAL_VALUE_RE.search(item):
            credential_flag = True

    visit(payload)
    return {
        "canonical_payload_sha256": hashlib.sha256(encoded).hexdigest(),
        "key_names": sorted(names)[:200],
        "credential_flag": credential_flag,
        "payload_present": value is not None,
    }


def _endpoint_fingerprint(url: URL, expected_database: str) -> str:
    """Hash non-secret endpoint identity without including username/password."""
    identity = "|".join(
        (
            url.get_backend_name() or "",
            url.host or "",
            str(url.port or ""),
            expected_database,
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _sync_mysql_url(url: URL) -> URL:
    """Convert supported async MySQL URL schemes to the synchronous driver."""
    if url.get_backend_name() != "mysql":
        return url
    if url.drivername == "mysql+aiomysql":
        return url.set(drivername="mysql+pymysql")
    if url.drivername == "mysql+asyncmy":
        return url.set(drivername="mysql+pymysql")
    if url.drivername == "mysql":
        return url.set(drivername="mysql+pymysql")
    if url.drivername != "mysql+pymysql":
        raise RuntimeInventoryError("unsupported_mysql_driver")
    return url


def _validate_endpoint(
    url: URL,
    endpoint: ApprovedEndpoint,
    *,
    allow_sqlite_for_tests: bool = False,
) -> URL:
    """Reject any MySQL endpoint outside the two known local databases."""
    normalized = _sync_mysql_url(url)
    if normalized.get_backend_name() == "sqlite":
        if allow_sqlite_for_tests:
            return normalized
        raise RuntimeInventoryError("sqlite_is_test_only")
    if normalized.get_backend_name() != "mysql":
        raise RuntimeInventoryError("database_backend_not_supported")
    if (
        normalized.host != APPROVED_HOST
        or normalized.port != APPROVED_PORT
        or normalized.database != endpoint.database
    ):
        raise RuntimeInventoryError(f"{endpoint.role}_endpoint_not_approved")
    return normalized


def _driver_flag_is_true(value: object) -> bool:
    """Normalize boolean values from supported MySQL drivers."""
    if value is True or value == 1:
        return True
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    return isinstance(value, str) and value.casefold() in {"1", "on", "true"}


def _configure_mysql_readonly(connection: Connection) -> None:
    """Set and verify a repeatable-read, read-only transaction before queries."""
    raw = connection.connection.driver_connection
    if raw is None:
        raise RuntimeInventoryError("mysql_driver_connection_unavailable")
    cursor = raw.cursor()
    try:
        cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cursor.execute("SET SESSION TRANSACTION READ ONLY")
        cursor.execute("SELECT @@session.transaction_read_only")
        readonly_row = cursor.fetchone()
        if not readonly_row or not _driver_flag_is_true(readonly_row[0]):
            raise RuntimeInventoryError("mysql_readonly_session_not_confirmed")
        raw.commit()
        cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        cursor.execute("SELECT @@session.transaction_read_only, @@session.transaction_isolation")
        state = cursor.fetchone()
    except RuntimeInventoryError:
        raise
    except Exception as exc:
        raise RuntimeInventoryError("mysql_readonly_transaction_setup_failed") from exc
    finally:
        cursor.close()
    if not state or not _driver_flag_is_true(state[0]):
        raise RuntimeInventoryError("mysql_readonly_snapshot_not_confirmed")
    isolation = str(state[1]).replace("_", "-").casefold()
    if isolation != "repeatable-read":
        raise RuntimeInventoryError("mysql_snapshot_isolation_not_confirmed")


def _configure_sqlite_readonly(connection: Connection) -> None:
    """Enable SQLite's query-only guard for isolated tests."""
    try:
        connection.exec_driver_sql("PRAGMA query_only = ON")
        value = connection.exec_driver_sql("PRAGMA query_only").scalar_one()
    except Exception as exc:
        raise RuntimeInventoryError("sqlite_readonly_setup_failed") from exc
    if value != 1:
        raise RuntimeInventoryError("sqlite_readonly_not_confirmed")


@contextmanager
def _readonly_snapshot(
    engine: Engine,
    *,
    expected_database: str | None,
) -> Iterator[tuple[Connection, str, str, str]]:
    """Yield an asserted read-only transaction and its backend evidence."""
    try:
        with engine.connect() as connection:
            backend = engine.dialect.name
            if backend == "mysql":
                _configure_mysql_readonly(connection)
                observed = connection.execute(text("SELECT DATABASE()")).scalar_one_or_none()
                if not isinstance(observed, str) or not observed:
                    raise RuntimeInventoryError("mysql_database_identity_missing")
                if expected_database and observed != expected_database:
                    raise RuntimeInventoryError("mysql_observed_database_mismatch")
                state = connection.execute(
                    text("SELECT @@session.transaction_read_only, @@session.transaction_isolation")
                ).one()
                if not _driver_flag_is_true(state[0]):
                    raise RuntimeInventoryError("mysql_readonly_snapshot_not_confirmed")
                isolation = str(state[1]).replace("_", "-").casefold()
                if isolation != "repeatable-read":
                    raise RuntimeInventoryError("mysql_snapshot_isolation_not_confirmed")
                captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                yield connection, observed, isolation, captured_at
            elif backend == "sqlite":
                _configure_sqlite_readonly(connection)
                connection.exec_driver_sql("BEGIN")
                observed = "sqlite-test-only"
                captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                yield connection, observed, "sqlite-readonly", captured_at
            else:
                raise RuntimeInventoryError("database_backend_not_supported")
    except RuntimeInventoryError:
        raise
    except Exception as exc:
        raise RuntimeInventoryError("readonly_database_inventory_failed") from exc


def _table_columns(connection: Connection, database: str, table: str) -> list[dict[str, object]]:
    """Read safe column descriptors for one known MySQL table."""
    rows = connection.execute(
        text(
            "SELECT COLUMN_NAME, ORDINAL_POSITION, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, EXTRA "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table "
            "ORDER BY ORDINAL_POSITION"
        ),
        {"schema": database, "table": table},
    ).mappings()
    return [
        {
            "name": row["COLUMN_NAME"],
            "ordinal": int(row["ORDINAL_POSITION"]),
            "type": row["COLUMN_TYPE"],
            "nullable": row["IS_NULLABLE"] == "YES",
            "key": row["COLUMN_KEY"],
            "extra": row["EXTRA"],
        }
        for row in rows
    ]


def _sqlite_table_columns(connection: Connection, table: str) -> list[dict[str, object]]:
    """Read scratch-test table columns with a quoted SQLite PRAGMA."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise RuntimeInventoryError("invalid_test_table_name")
    rows = connection.exec_driver_sql(f'PRAGMA table_info("{table}")').mappings()
    return [
        {
            "name": row["name"],
            "ordinal": int(row["cid"]) + 1,
            "type": row["type"],
            "nullable": not bool(row["notnull"]),
            "key": "PRIMARY KEY" if row["pk"] else "",
            "extra": "",
        }
        for row in rows
    ]


def _warehouse_schema(connection: Connection, database: str) -> dict[str, object]:
    """Inventory warehouse tables and indexes from information_schema only."""
    if connection.dialect.name == "sqlite":
        return _sqlite_warehouse_schema(connection)
    tables = list(
        connection.execute(
            text(
                "SELECT TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_ROWS "
                "FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :schema ORDER BY TABLE_NAME"
            ),
            {"schema": database},
        ).mappings()
    )
    columns = list(
        connection.execute(
            text(
                "SELECT TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, COLUMN_TYPE, IS_NULLABLE, "
                "COLUMN_KEY, EXTRA FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :schema ORDER BY TABLE_NAME, ORDINAL_POSITION"
            ),
            {"schema": database},
        ).mappings()
    )
    indexes = list(
        connection.execute(
            text(
                "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, "
                "INDEX_TYPE FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = :schema ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"
            ),
            {"schema": database},
        ).mappings()
    )
    columns_by_table: dict[str, list[dict[str, object]]] = {}
    primary_keys: dict[str, list[str]] = {}
    for row in columns:
        name = str(row["TABLE_NAME"])
        column = {
            "name": row["COLUMN_NAME"],
            "ordinal": int(row["ORDINAL_POSITION"]),
            "type": row["COLUMN_TYPE"],
            "nullable": row["IS_NULLABLE"] == "YES",
            "key": row["COLUMN_KEY"],
            "extra": row["EXTRA"],
        }
        columns_by_table.setdefault(name, []).append(column)
        if row["COLUMN_KEY"] == "PRI":
            primary_keys.setdefault(name, []).append(str(row["COLUMN_NAME"]))
    indexes_by_table: dict[str, dict[str, dict[str, object]]] = {}
    for row in indexes:
        table_name = str(row["TABLE_NAME"])
        index_name = str(row["INDEX_NAME"])
        index = indexes_by_table.setdefault(table_name, {}).setdefault(
            index_name,
            {
                "name": index_name,
                "unique": not bool(row["NON_UNIQUE"]),
                "type": row["INDEX_TYPE"],
                "columns": [],
            },
        )
        columns_list = index["columns"]
        if not isinstance(columns_list, list):
            raise RuntimeInventoryError("warehouse_index_schema_invalid")
        columns_list.append(row["COLUMN_NAME"])
    result_tables: list[dict[str, object]] = []
    for row in tables:
        name = str(row["TABLE_NAME"])
        estimate = row["TABLE_ROWS"]
        result_tables.append(
            {
                "name": name,
                "type": row["TABLE_TYPE"],
                "engine": row["ENGINE"],
                "estimated_rows": int(estimate) if estimate is not None else None,
                "row_count_semantics": (
                    "information_schema estimate only; not coverage or exact count"
                ),
                "primary_key_columns": primary_keys.get(name, []),
                "columns": columns_by_table.get(name, []),
                "indexes": list(indexes_by_table.get(name, {}).values()),
            }
        )
    if not result_tables:
        raise RuntimeInventoryError("warehouse_schema_is_empty_or_unavailable")
    return {
        "table_count": len(result_tables),
        "tables": result_tables,
        "row_count_semantics": (
            "TABLE_ROWS estimates are metadata only and never establish data coverage"
        ),
    }


def _sqlite_warehouse_schema(connection: Connection) -> dict[str, object]:
    """Inventory scratch SQLite schema without counting or reading data rows."""
    rows = connection.exec_driver_sql(
        "SELECT name, type FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).mappings()
    result_tables: list[dict[str, object]] = []
    for row in rows:
        table_name = str(row["name"])
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
            raise RuntimeInventoryError("invalid_test_schema_table_name")
        columns = _sqlite_table_columns(connection, table_name)
        primary_keys = [str(column["name"]) for column in columns if column["key"] == "PRIMARY KEY"]
        index_rows = list(
            connection.exec_driver_sql(f'PRAGMA index_list("{table_name}")').mappings()
        )
        indexes: list[dict[str, object]] = []
        for index_row in index_rows:
            index_name = str(index_row["name"])
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", index_name):
                raise RuntimeInventoryError("invalid_test_schema_index_name")
            index_columns = connection.exec_driver_sql(
                f'PRAGMA index_info("{index_name}")'
            ).mappings()
            indexes.append(
                {
                    "name": index_name,
                    "unique": bool(index_row["unique"]),
                    "type": "BTREE",
                    "columns": [index_column["name"] for index_column in index_columns],
                }
            )
        result_tables.append(
            {
                "name": table_name,
                "type": "BASE TABLE" if row["type"] == "table" else "VIEW",
                "engine": "SQLite test fixture",
                "estimated_rows": None,
                "row_count_semantics": "not queried in scratch SQLite fixture",
                "primary_key_columns": primary_keys,
                "columns": columns,
                "indexes": indexes,
            }
        )
    return {
        "table_count": len(result_tables),
        "tables": result_tables,
        "row_count_semantics": "scratch SQLite schema only; no row counts are collected",
    }


def _resolve_columns(
    available: set[str], aliases: Mapping[str, tuple[str, ...]], *, required: set[str]
) -> dict[str, str | None]:
    """Resolve canonical names against a table's observed allowlisted fields."""
    resolved: dict[str, str | None] = {}
    for canonical, candidates in aliases.items():
        resolved[canonical] = next(
            (candidate for candidate in candidates if candidate in available), None
        )
    missing = sorted(name for name in required if resolved.get(name) is None)
    if missing:
        raise RuntimeInventoryError(f"required_table_columns_missing:{','.join(missing)}")
    return resolved


def _read_rows(
    connection: Connection,
    *,
    table: str,
    resolved: Mapping[str, str | None],
    order_by: str,
) -> list[dict[str, object]]:
    """Read only mapped columns from one exact legacy table."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise RuntimeInventoryError("invalid_legacy_table_name")
    selected = [(canonical, column) for canonical, column in resolved.items() if column]
    if not selected:
        raise RuntimeInventoryError("no_allowlisted_table_columns")
    field_names = list(dict.fromkeys(column for _canonical, column in selected))
    table_object = Table(
        table,
        MetaData(),
        *(Column(column_name, NullType()) for column_name in field_names),
    )
    order_column = resolved.get(order_by)
    if not order_column:
        raise RuntimeInventoryError("stable_order_column_missing")
    query = (
        select(*(table_object.c[column].label(canonical) for canonical, column in selected))
        .select_from(table_object)
        .order_by(table_object.c[order_column])
        .limit(MAX_TASK_ROWS + 1)
    )
    rows = [dict(row) for row in connection.execute(query).mappings()]
    if len(rows) > MAX_TASK_ROWS:
        raise RuntimeInventoryError("legacy_table_row_limit_exceeded")
    return rows


def _source_path(module_path: object, *, source_root: Path) -> tuple[str | None, str]:
    """Map known legacy module namespaces to exact AFI-relative script paths."""
    if not isinstance(module_path, str) or not module_path.strip():
        return None, "MODULE_PATH_MISSING"
    value = module_path.strip()
    expected_root = PurePosixPath("src/backend/app/data_fetch/scripts")
    candidate: PurePosixPath | None = None
    normalized = value.replace("\\", "/")
    path_value = PurePosixPath(normalized)
    if ".." in path_value.parts:
        return None, "MODULE_PATH_TRAVERSAL_REJECTED"
    parts = path_value.parts
    if parts and parts[0] == "src" and expected_root.parts[: len(parts)] == parts:
        candidate = path_value
    elif "data_fetch" in parts and "scripts" in parts:
        data_index = parts.index("data_fetch")
        if data_index + 1 < len(parts) and parts[data_index + 1] == "scripts":
            suffix = parts[data_index + 2 :]
            candidate = expected_root.joinpath(*suffix)
    else:
        dotted = value.removesuffix(".py").split(".")
        for prefix in (
            ("app", "data_fetch", "scripts"),
            ("data_fetch", "scripts"),
        ):
            if tuple(dotted[: len(prefix)]) == prefix:
                candidate = expected_root.joinpath(*dotted[len(prefix) :])
                break
    if candidate is None:
        return None, "MODULE_NAMESPACE_UNRESOLVED"
    if candidate.suffix != ".py":
        candidate = candidate.with_suffix(".py")
    if candidate.name == "__init__.py" or len(candidate.parts) <= len(expected_root.parts):
        return None, "MODULE_NOT_A_SCRIPT_FILE"
    resolved = source_root.joinpath(*candidate.parts)
    try:
        resolved.resolve(strict=False).relative_to(source_root)
    except ValueError:
        return None, "MODULE_PATH_OUTSIDE_SOURCE_ROOT"
    return candidate.as_posix(), "PATH_CANDIDATE"


def _unresolved_path(kind: str, identifier: object) -> str:
    """Build a deterministic safe sentinel path for an unresolved mapping."""
    digest = hashlib.sha256(str(identifier).encode("utf-8", errors="replace")).hexdigest()
    return f"__unresolved__/{kind}-{digest[:24]}.py"


def _task_map_rows(
    tasks: Sequence[dict[str, object]], scripts: Sequence[dict[str, object]]
) -> list[dict[str, object]]:
    """Build task-map-compatible rows for the existing static scanner."""
    by_script_id = {str(item["script_id"]): item for item in scripts}
    mapped_script_ids: set[str] = set()
    result: list[dict[str, object]] = []
    for task in tasks:
        task_id = str(task["id"])
        script_id = task.get("script_id")
        script = by_script_id.get(str(script_id)) if script_id is not None else None
        if script is not None:
            mapped_script_ids.add(str(script_id))
            source_path = script.get("source_path")
            status = script.get("path_status")
            path = (
                source_path if isinstance(source_path, str) else _unresolved_path("task", task_id)
            )
        else:
            status = "SCRIPT_REFERENCE_MISSING" if script_id is not None else "NO_SCRIPT_REFERENCE"
            path = _unresolved_path("task", task_id)
        result.append(
            {
                "task_id": f"scheduled:{task_id}",
                "script_path": path,
                "script_id": script_id,
                "runtime_task_kind": task.get("task_kind"),
                "runtime_is_active": task.get("is_active"),
                "path_status": status,
            }
        )
    for script in scripts:
        script_id = str(script["script_id"])
        if script_id in mapped_script_ids:
            continue
        source_path = script.get("source_path")
        status = script.get("path_status")
        path = (
            source_path if isinstance(source_path, str) else _unresolved_path("script", script_id)
        )
        result.append(
            {
                "task_id": f"script:{script_id}",
                "script_path": path,
                "script_id": script_id,
                "runtime_task_kind": "script_registry_only",
                "runtime_is_active": script.get("is_active"),
                "path_status": status,
            }
        )
    return result


def _table_exists(columns: Sequence[dict[str, object]]) -> bool:
    """Return whether table metadata exists."""
    return bool(columns)


def _metadata_inventory(
    connection: Connection,
    *,
    database: str,
    source_root: Path,
) -> dict[str, object]:
    """Read task/script records and relevant metadata-table schema safely."""
    if connection.dialect.name == "sqlite":
        task_columns = _sqlite_table_columns(connection, TASK_TABLE)
        script_columns = _sqlite_table_columns(connection, SCRIPT_TABLE)
    else:
        task_columns = _table_columns(connection, database, TASK_TABLE)
        script_columns = _table_columns(connection, database, SCRIPT_TABLE)
    if not _table_exists(task_columns) or not _table_exists(script_columns):
        raise RuntimeInventoryError("expected_legacy_runtime_tables_missing")
    task_available = {str(column["name"]) for column in task_columns}
    script_available = {str(column["name"]) for column in script_columns}
    task_resolved = _resolve_columns(
        task_available,
        _TASK_COLUMN_ALIASES,
        required={
            "id",
            "script_id",
            "schedule_type",
            "expression",
            "is_active",
            "last_execution",
            "next_execution",
            "timeout",
            "retries",
        },
    )
    script_resolved = _resolve_columns(
        script_available,
        _SCRIPT_COLUMN_ALIASES,
        required={
            "script_id",
            "module_path",
            "function_name",
            "target_table",
            "is_active",
            "source",
        },
    )
    task_raw = _read_rows(
        connection,
        table=TASK_TABLE,
        resolved=task_resolved,
        order_by="id",
    )
    script_raw = _read_rows(
        connection,
        table=SCRIPT_TABLE,
        resolved=script_resolved,
        order_by="script_id",
    )

    tasks: list[dict[str, object]] = []
    for row in task_raw:
        parameters = _parameter_summary(row.pop("parameters", None))
        tasks.append(
            {
                **{key: _report_scalar(value) for key, value in row.items()},
                "parameter_summary": parameters,
            }
        )
    scripts: list[dict[str, object]] = []
    for row in script_raw:
        raw_module_path = row.get("module_path")
        source_path, path_status = _source_path(raw_module_path, source_root=source_root)
        parameters = _parameter_summary(row.pop("parameters", None))
        row.pop("module_path", None)
        scripts.append(
            {
                **{key: _report_scalar(value) for key, value in row.items()},
                "source_path": source_path,
                "path_status": path_status,
                "module_path_sha256": hashlib.sha256(
                    str(raw_module_path or "").encode("utf-8", errors="replace")
                ).hexdigest(),
                "parameter_summary": parameters,
            }
        )

    active_tasks = sum(bool(row.get("is_active")) for row in tasks)
    active_scripts = sum(bool(row.get("is_active")) for row in scripts)
    script_ids = {str(row["script_id"]) for row in scripts}
    dangling_task_script_ids = sum(
        row.get("script_id") is not None and str(row["script_id"]) not in script_ids
        for row in tasks
    )
    unresolved_scripts = sum(row.get("source_path") is None for row in scripts)
    return {
        "database": database,
        "tables": {
            TASK_TABLE: {"columns": task_columns},
            SCRIPT_TABLE: {"columns": script_columns},
        },
        "task_columns_selected": dict(task_resolved),
        "script_columns_selected": dict(script_resolved),
        "scheduled_tasks": {
            "row_count_exact": len(tasks),
            "active_count_exact": active_tasks,
            "inactive_count_exact": len(tasks) - active_tasks,
            "dangling_script_reference_count": dangling_task_script_ids,
            "records": tasks,
        },
        "data_scripts": {
            "row_count_exact": len(scripts),
            "active_count_exact": active_scripts,
            "inactive_count_exact": len(scripts) - active_scripts,
            "unresolved_module_path_count": unresolved_scripts,
            "records": scripts,
        },
        "task_map_tasks": _task_map_rows(tasks, scripts),
    }


def _exact_count(value: object, label: str) -> int:
    """Validate and return an exact non-negative metadata count."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeInventoryError(f"inventory_count_invalid:{label}")
    return value


def _nested_exact_count(source: Mapping[str, object], section_key: str, count_key: str) -> int:
    """Read one typed exact count from the metadata report."""
    section = source.get(section_key)
    if not isinstance(section, Mapping):
        raise RuntimeInventoryError(f"inventory_section_missing:{section_key}")
    return _exact_count(section.get(count_key), f"{section_key}.{count_key}")


def _safe_env_urls(env_file: Path) -> tuple[str, str, str]:
    """Read only the two approved connection keys and hash the dotenv file."""
    try:
        from dotenv import dotenv_values

        values = dotenv_values(env_file, interpolate=False)
    except Exception as exc:
        raise RuntimeInventoryError("dotenv_file_unreadable") from exc
    metadata_url = values.get("DATABASE_URL")
    warehouse_url = values.get("AKSHARE_DATA_DATABASE_URL")
    if not isinstance(metadata_url, str) or not metadata_url.strip():
        raise RuntimeInventoryError("dotenv_missing_DATABASE_URL")
    if not isinstance(warehouse_url, str) or not warehouse_url.strip():
        raise RuntimeInventoryError("dotenv_missing_AKSHARE_DATA_DATABASE_URL")
    try:
        file_hash = hashlib.sha256(env_file.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeInventoryError("dotenv_file_unreadable") from exc
    return metadata_url, warehouse_url, file_hash


def _code_hashes() -> dict[str, str]:
    """Hash the inventory implementation and its direct command entrypoint."""
    module_path = Path(__file__).resolve()
    cli_path = module_path.parents[1] / "scripts" / "ops" / "inventory_legacy_runtime.py"
    result: dict[str, str] = {}
    for label, path in (("runtime_inventory_module", module_path), ("cli_entrypoint", cli_path)):
        result[label] = _file_hash(path, label)
    return result


def _file_hash(path: Path, label: str) -> str:
    """Hash one source file without leaking filesystem or permission details."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeInventoryError(f"code_hash_unavailable:{label}") from exc


def build_runtime_inventory(
    *,
    metadata_url: str,
    warehouse_url: str,
    source_root: Path,
    env_file_sha256: str | None = None,
    allow_sqlite_for_tests: bool = False,
) -> dict[str, object]:
    """Inventory scheduler records and warehouse schema under read-only guards.

    Args:
        metadata_url: Explicit URL for the approved ``ai_for_investor`` endpoint.
        warehouse_url: Explicit URL for the approved ``akshare_data`` endpoint.
        source_root: AFI repository root used only to resolve source paths.
        env_file_sha256: Optional hash of the dotenv file from which URLs came.
        allow_sqlite_for_tests: Permit SQLite URLs for isolated test fixtures only.

    Returns:
        A JSON-compatible report; parameter values and connection credentials are
        never included.

    Raises:
        RuntimeInventoryError: If endpoint identity, read-only mode, schema, or
            expected table contents cannot be verified.
    """
    root = source_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise RuntimeInventoryError("source_root_not_a_directory")
    endpoints = (
        (ApprovedEndpoint("metadata", METADATA_DATABASE), metadata_url),
        (ApprovedEndpoint("warehouse", WAREHOUSE_DATABASE), warehouse_url),
    )
    endpoint_evidence: dict[str, dict[str, object]] = {}
    engines: dict[str, Engine] = {}
    parsed_urls: dict[str, URL] = {}
    try:
        for endpoint, raw_url in endpoints:
            try:
                parsed = make_url(raw_url)
                normalized = _validate_endpoint(
                    parsed,
                    endpoint,
                    allow_sqlite_for_tests=allow_sqlite_for_tests,
                )
                options: dict[str, object] = {
                    "poolclass": NullPool,
                    "hide_parameters": True,
                    "echo": False,
                    "future": True,
                }
                if normalized.get_backend_name() == "mysql":
                    options.update(
                        {
                            "pool_pre_ping": True,
                            "connect_args": {
                                "connect_timeout": 8,
                                "read_timeout": 20,
                                "write_timeout": 20,
                            },
                        }
                    )
                engine = create_engine(normalized, **options)
            except RuntimeInventoryError:
                raise
            except Exception as exc:
                raise RuntimeInventoryError(
                    f"{endpoint.role}_connection_configuration_invalid"
                ) from exc
            engines[endpoint.role] = engine
            parsed_urls[endpoint.role] = parsed

        metadata_endpoint = ApprovedEndpoint("metadata", METADATA_DATABASE)
        metadata_engine = engines["metadata"]
        with _readonly_snapshot(
            metadata_engine,
            expected_database=metadata_endpoint.database,
        ) as (connection, observed_database, isolation, captured_at):
            metadata = _metadata_inventory(
                connection,
                database=observed_database,
                source_root=root,
            )
        endpoint_evidence["metadata"] = {
            "requested_database": metadata_endpoint.database,
            "observed_database": observed_database,
            "endpoint_fingerprint": _endpoint_fingerprint(
                parsed_urls["metadata"], metadata_endpoint.database
            ),
            "read_only_confirmed": True,
            "snapshot_isolation": isolation,
            "snapshot_captured_at_utc": captured_at,
        }

        warehouse_endpoint = ApprovedEndpoint("warehouse", WAREHOUSE_DATABASE)
        warehouse_engine = engines["warehouse"]
        with _readonly_snapshot(
            warehouse_engine,
            expected_database=warehouse_endpoint.database,
        ) as (connection, observed_database, isolation, captured_at):
            warehouse = _warehouse_schema(connection, observed_database)
        endpoint_evidence["warehouse"] = {
            "requested_database": warehouse_endpoint.database,
            "observed_database": observed_database,
            "endpoint_fingerprint": _endpoint_fingerprint(
                parsed_urls["warehouse"], warehouse_endpoint.database
            ),
            "read_only_confirmed": True,
            "snapshot_isolation": isolation,
            "snapshot_captured_at_utc": captured_at,
        }
    finally:
        for engine in engines.values():
            engine.dispose()

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    task_map = metadata.pop("task_map_tasks")
    counts: dict[str, object] = {
        "scheduled_task_rows_exact": _nested_exact_count(
            metadata, "scheduled_tasks", "row_count_exact"
        ),
        "scheduled_task_active_exact": _nested_exact_count(
            metadata, "scheduled_tasks", "active_count_exact"
        ),
        "scheduled_task_inactive_exact": _nested_exact_count(
            metadata, "scheduled_tasks", "inactive_count_exact"
        ),
        "data_script_rows_exact": _nested_exact_count(metadata, "data_scripts", "row_count_exact"),
        "data_script_active_exact": _nested_exact_count(
            metadata, "data_scripts", "active_count_exact"
        ),
        "data_script_inactive_exact": _nested_exact_count(
            metadata, "data_scripts", "inactive_count_exact"
        ),
        "warehouse_tables": _exact_count(warehouse.get("table_count"), "warehouse.table_count"),
        "warehouse_estimated_rows_semantics": ("information_schema estimate only; never coverage"),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": generated_at,
        "source_root": root.as_posix(),
        "code_sha256": _code_hashes(),
        "env_file_sha256": env_file_sha256,
        "readonly_transaction": {
            "statement": (
                "SET SESSION TRANSACTION READ ONLY; START TRANSACTION WITH CONSISTENT SNAPSHOT"
            ),
            "metadata_confirmed": endpoint_evidence["metadata"]["read_only_confirmed"],
            "warehouse_confirmed": endpoint_evidence["warehouse"]["read_only_confirmed"],
            "snapshot_semantics": (
                "Each database is read in its own repeatable-read snapshot; the two snapshots "
                "are not a distributed cross-database transaction."
            ),
        },
        "endpoints": endpoint_evidence,
        "metadata": metadata,
        "warehouse_schema": warehouse,
        "tasks": task_map,
        "counts": counts,
        "verification_notice": (
            "Scheduler counts are exact for the selected tables at the captured snapshot. "
            "Warehouse row counts are TABLE_ROWS estimates only. Source paths are candidates; "
            "the task map is designed for the existing AST inventory's bidirectional anti-join."
        ),
    }


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    """Create a new JSON report without overwriting any existing path."""
    try:
        path = path.expanduser().resolve(strict=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                default=_json_default,
                allow_nan=False,
            )
            + "\n"
        )
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
    except FileExistsError as exc:
        raise RuntimeInventoryError("output_already_exists_refusing_overwrite") from exc
    except OSError as exc:
        raise RuntimeInventoryError("output_write_failed") from exc


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse command-line options without inspecting unrelated environment keys."""
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--env-file", type=Path, help="Read only the two approved DB URL keys")
    source.add_argument("--metadata-url", help="Explicit ai_for_investor MySQL URL")
    parser.add_argument("--warehouse-url", help="Explicit akshare_data MySQL URL")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.env_file is None and not args.warehouse_url:
        parser.error("--warehouse-url is required with --metadata-url")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Run the guarded runtime inventory and return a fail-closed exit code."""
    args = _parse_args(argv)
    try:
        env_hash = None
        if args.env_file is not None:
            metadata_url, warehouse_url, env_hash = _safe_env_urls(args.env_file)
        else:
            metadata_url = args.metadata_url
            warehouse_url = args.warehouse_url
        report = build_runtime_inventory(
            metadata_url=metadata_url,
            warehouse_url=warehouse_url,
            source_root=args.source_root,
            env_file_sha256=env_hash,
        )
        _write_report(args.output, report)
        summary = report["counts"]
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except RuntimeInventoryError as exc:
        print(f"runtime inventory failed: {exc}", file=sys.stderr)
        return 1
    except Exception:
        # Do not print raw SQLAlchemy exceptions because they may include a DSN.
        print("runtime inventory failed: unexpected internal error", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
