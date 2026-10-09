#!/usr/bin/env python3
"""Measure a bounded, explicit ODS-to-ODS write through ``OdsWriter``.

The source is opened read-only in one repeatable-read snapshot. By default the
command only validates the registered source table and reports an exact source
count; ``--apply`` is required to stream pages into a caller-created loopback
benchmark schema. Progress is JSON Lines and never includes either database URL
or raw exception text.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import resource
import subprocess  # nosec B404  # sole subprocess.run: ps from (/bin/ps,/usr/bin/ps) tuple; literal argv
import sys
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pandas as pd
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Connection, Engine, make_url

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from opendata.core.config import get_settings  # noqa: E402
from opendata.data.domains import ods_table, require_domain  # noqa: E402
from opendata.data.mapping import mapping_sources, require_domain_mapping  # noqa: E402
from opendata.pipeline.ods_writer import DEFAULT_BATCH_SIZE, OdsWriter  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

TARGET_URL_ENV = "C65_BENCH_TARGET_URL"
TARGET_PORT = 33565
TARGET_DATABASE_PREFIX = "opendata_c65_"
DEFAULT_PAGE_SIZE = DEFAULT_BATCH_SIZE
_ALLOWED_SOURCE_SQL = frozenset({"SELECT", "SHOW", "EXPLAIN", "SET"})
_REGISTRY_DOMAIN = "stock_daily"


@dataclass(frozen=True)
class SourceSpec:
    """Registered stock-daily source table and its raw business key."""

    source: str
    table: str
    key: tuple[str, ...]


@dataclass(frozen=True)
class TableSchema:
    """Reflected table columns and ordered primary key."""

    columns: tuple[tuple[str, str, bool], ...]
    primary_key: tuple[str, ...]


def _positive_int(value: str) -> int:
    """Parse a positive CLI integer."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _quote_identifier(identifier: str) -> str:
    """Quote a registered identifier and reject unsafe names."""
    if (
        not identifier
        or identifier[0].isdigit()
        or any(not (character.isalnum() or character == "_") for character in identifier)
    ):
        raise ValueError("registered table schema contains an unsafe identifier")
    return f"`{identifier}`"


def _source_spec(source: str) -> SourceSpec:
    """Resolve a registered explicit source mapping for stock_daily."""
    if source not in mapping_sources():
        raise ValueError("source is not registered in the mapping catalog")
    require_domain(_REGISTRY_DOMAIN)
    mapping = require_domain_mapping(source, _REGISTRY_DOMAIN)
    key = mapping.source_key
    if not key or len(set(key)) != len(key):
        raise ValueError("registered stock_daily mapping has an invalid business key")
    table = ods_table(_REGISTRY_DOMAIN, source)
    _quote_identifier(table)
    for column in key:
        _quote_identifier(column)
    return SourceSpec(source=source, table=table, key=key)


def _parse_mysql_url(value: str, label: str) -> URL:
    """Parse a MySQL URL without ever including its value in an error."""
    try:
        url = make_url(value)
    except Exception as exc:
        raise ValueError(f"invalid {label} database URL") from exc
    if url.drivername not in {"mysql", "mysql+pymysql"}:
        raise ValueError(f"{label} database must use MySQL")
    if not url.host or not url.database:
        raise ValueError(f"{label} database URL must name a host and schema")
    return url


def _is_loopback_host(host: str) -> bool:
    """Return whether a hostname is an explicit loopback address."""
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _endpoint_key(url: URL) -> tuple[str, int, str]:
    """Normalize endpoint identity so localhost aliases cannot bypass checks."""
    host = str(url.host or "").casefold()
    if _is_loopback_host(host):
        host = "loopback"
    else:
        with suppress(ValueError):
            host = ipaddress.ip_address(host).compressed
    return host, url.port or 3306, str(url.database or "").casefold()


def _validate_database_urls(source_url: str, target_url: str) -> None:
    """Fail closed unless the target is the isolated C65 loopback schema."""
    source = _parse_mysql_url(source_url, "source")
    target = _parse_mysql_url(target_url, "target")
    if not _is_loopback_host(str(target.host)):
        raise ValueError("target database host must be loopback")
    if target.port != TARGET_PORT:
        raise ValueError(f"target database port must be {TARGET_PORT}")
    database = str(target.database or "")
    if not database.startswith(TARGET_DATABASE_PREFIX) or len(database) <= len(
        TARGET_DATABASE_PREFIX
    ):
        raise ValueError("target schema must use the isolated opendata_c65_ prefix")
    if _endpoint_key(source) == _endpoint_key(target):
        raise ValueError("source and target databases must be different")


def _source_statement_guard(
    connection: object,
    cursor: object,
    statement: str,
    parameters: object,
    context: object,
    executemany: bool,
) -> None:
    """Permit only read or session-configuration SQL on the source engine."""
    del connection, cursor, parameters, context, executemany
    match = re.match(r"\s*([A-Za-z]+)", statement)
    command = match.group(1).upper() if match is not None else ""
    if command not in _ALLOWED_SOURCE_SQL:
        raise PermissionError("source SQL guard blocked a non-read statement")


def _driver_flag_is_true(value: object) -> bool:
    """Recognize MySQL boolean values returned by different drivers."""
    if value is True or value == 1:
        return True
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    return isinstance(value, str) and value.casefold() in {
        "1",
        "on",
        "true",
    }


def _configure_readonly_session(connection: Connection) -> None:
    """Set and verify read-only repeatable-read defaults before a transaction."""
    raw = connection.connection.driver_connection
    if raw is None:
        raise RuntimeError("source DBAPI connection is unavailable")
    cursor = raw.cursor()
    try:
        cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cursor.execute("SET SESSION TRANSACTION READ ONLY")
        cursor.execute("SELECT @@session.transaction_read_only")
        row = cursor.fetchone()
    finally:
        cursor.close()
    raw.commit()
    if row is None or not row or not _driver_flag_is_true(row[0]):
        raise RuntimeError("source session did not confirm read-only mode")


@contextmanager
def _readonly_source_snapshot(engine: Engine) -> Iterator[Connection]:
    """Yield one guarded read-only repeatable-read source transaction."""
    event.listen(engine, "before_cursor_execute", _source_statement_guard)
    try:
        with engine.connect() as connection:
            _configure_readonly_session(connection)
            with connection.begin():
                state = connection.execute(
                    text("SELECT @@session.transaction_read_only, @@session.transaction_isolation")
                ).one()
                isolation = str(state[1]).replace("_", "-").casefold()
                if not _driver_flag_is_true(state[0]) or isolation != "repeatable-read":
                    raise RuntimeError("source transaction is not read-only repeatable-read")
                yield connection
    finally:
        with suppress(Exception):
            event.remove(engine, "before_cursor_execute", _source_statement_guard)


def _table_schema(connection: Connection, table: str) -> TableSchema:
    """Inspect a caller-created table without creating or altering it."""
    exists = connection.execute(
        text(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :c65_table"
        ),
        {"c65_table": table},
    ).scalar_one_or_none()
    if exists is None:
        raise LookupError("required ODS table is missing")
    raw_columns = connection.execute(
        text(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :c65_table"
        ),
        {"c65_table": table},
    )
    columns = tuple(
        sorted(
            (
                str(name),
                str(column_type).casefold(),
                str(nullable).casefold() == "yes",
            )
            for name, column_type, nullable in raw_columns.fetchall()
        )
    )
    if not columns:
        raise LookupError("required ODS table has no reflected columns")
    raw_primary_key = connection.execute(
        text(
            "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :c65_table "
            "AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION"
        ),
        {"c65_table": table},
    )
    primary_key = tuple(str(row[0]) for row in raw_primary_key.fetchall())
    return TableSchema(columns=columns, primary_key=primary_key)


def _require_matching_schema(
    source: TableSchema, target: TableSchema, key: tuple[str, ...]
) -> None:
    """Require identical columns and the exact registered key on both tables."""
    if source.columns != target.columns:
        raise ValueError("source and target ODS column schemas differ")
    if source.primary_key != key or target.primary_key != key:
        raise ValueError("source and target primary keys must match the registered business key")


def _exact_row_count(connection: Connection, table: str) -> int:
    """Return an exact table row count with a registry-derived identifier."""
    # The identifier comes from the source mapping registry and is quoted above.
    result = connection.execute(
        text(f"SELECT COUNT(*) FROM {_quote_identifier(table)}")  # noqa: S608  # nosec B608  # table from registry, _quote_identifier
    )
    return int(result.scalar_one())


def _source_pages(
    connection: Connection,
    table: str,
    columns: Sequence[str],
    key: Sequence[str],
    limit_rows: int | None,
    page_size: int,
) -> Iterator[pd.DataFrame]:
    """Stream stable key-ordered source pages with a fixed fetch size."""
    selected = ", ".join(_quote_identifier(column) for column in columns)
    order = ", ".join(_quote_identifier(column) for column in key)
    # Table and column names come only from the validated mapping/reflection seam.
    statement = f"SELECT {selected} FROM {_quote_identifier(table)} ORDER BY {order}"  # noqa: S608  # nosec B608  # _quote_identifier on reflected cols/key
    parameters: dict[str, int] = {}
    if limit_rows is not None:
        statement += " LIMIT :c65_row_limit"
        parameters["c65_row_limit"] = limit_rows
    result = connection.execution_options(stream_results=True, max_row_buffer=page_size).execute(
        text(statement), parameters
    )
    try:
        read_rows = 0
        while limit_rows is None or read_rows < limit_rows:
            fetch_size = page_size if limit_rows is None else min(page_size, limit_rows - read_rows)
            rows = result.fetchmany(fetch_size)
            if not rows:
                break
            read_rows += len(rows)
            yield pd.DataFrame.from_records([tuple(row) for row in rows], columns=list(columns))
    finally:
        result.close()


def _new_engine(url: str) -> Engine:
    """Create a synchronous engine for a prevalidated MySQL URL."""
    return create_engine(url, pool_pre_ping=True)


def _new_staging_writer(engine: Engine, page_size: int) -> OdsWriter:
    """Use OdsWriter's staging path at the fixed source page size."""
    return OdsWriter(engine, batch_size=page_size, mode="staging")


def _current_rss_bytes() -> int | None:
    """Read this process's current RSS using only the stdlib ``ps`` command."""
    ps_executable = next(
        (path for path in ("/bin/ps", "/usr/bin/ps") if Path(path).is_file()), None
    )
    if ps_executable is None:
        return None
    try:
        process = subprocess.run(  # noqa: S603  # nosec B603 -- fixed ps argv, no shell
            [ps_executable, "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if process.returncode != 0 or not process.stdout.strip().isdigit():
        return None
    return int(process.stdout.strip()) * 1024


def _memory_reading() -> dict[str, int | str | None]:
    """Normalize current and peak process RSS to bytes."""
    peak_raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_bytes = int(peak_raw if sys.platform == "darwin" else peak_raw * 1024)
    return {
        "current_rss_bytes": _current_rss_bytes(),
        "peak_rss_bytes": peak_bytes,
        "rss_unit": "bytes",
        "current_rss_source": "ps",
        "peak_rss_source": "resource.getrusage",
    }


def _safe_environment() -> dict[str, str]:
    """Return interpreter and benchmark-source provenance without database settings."""
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "byte_unit": "bytes",
    }


def _state_counter(state: dict[str, object], name: str) -> int:
    """Read a counter from report state after checking its runtime type."""
    value = state[name]
    if not isinstance(value, int):
        raise RuntimeError(f"benchmark counter {name!r} is not an integer")
    return value


def run_benchmark(
    *,
    source: str,
    apply: bool = False,
    page_size: int = DEFAULT_PAGE_SIZE,
    limit_rows: int | None = None,
    source_url: str | None = None,
    target_url: str | None = None,
    progress: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    """Validate/count or explicitly stream one registered source table to staging ODS.

    Engine and I/O helpers remain module seams so unit tests can use fakes without
    constructing a connection from a configured warehouse URL.
    """
    started = time.monotonic()
    source_engine: Engine | None = None
    target_engine: Engine | None = None
    state: dict[str, object] = {
        "status": "running",
        "mode": "apply" if apply else "dry-run",
        "domain": _REGISTRY_DOMAIN,
        "source": None,
        "table": None,
        "business_key": [],
        "page_size": page_size,
        "limit_rows": limit_rows,
        "source_rows_exact": None,
        "rows_requested": None,
        "rows_read": 0,
        "rows_written": 0,
        "pages_read": 0,
        "pages_written": 0,
        "target_rows_before": None,
        "target_rows_after": None,
        "source_scan_complete": False,
        "full_source_write_verified": False,
        "elapsed_seconds": 0.0,
        **_safe_environment(),
    }

    def emit(record: str, **values: object) -> None:
        if progress is None:
            return
        event_record: dict[str, object] = {
            "record": record,
            **state,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            **_memory_reading(),
            **values,
        }
        progress(event_record)

    try:
        if page_size <= 0:
            raise ValueError("page size must be positive")
        if limit_rows is not None and limit_rows <= 0:
            raise ValueError("limit rows must be positive")
        spec = _source_spec(source)
        state.update(source=spec.source, table=spec.table, business_key=list(spec.key))
        resolved_source_url = get_settings().data_database_url if source_url is None else source_url
        resolved_target_url = os.environ.get(TARGET_URL_ENV) if target_url is None else target_url
        if not resolved_target_url:
            raise ValueError(f"{TARGET_URL_ENV} must name the isolated target database")
        _validate_database_urls(resolved_source_url, resolved_target_url)
        source_engine = _new_engine(resolved_source_url)
        target_engine = _new_engine(resolved_target_url)

        with target_engine.connect() as target_connection:
            target_schema = _table_schema(target_connection, spec.table)
            target_rows_before = _exact_row_count(target_connection, spec.table)
        state["target_rows_before"] = target_rows_before
        if apply and target_rows_before != 0:
            raise ValueError("apply requires an empty target ODS table")

        with _readonly_source_snapshot(source_engine) as source_connection:
            source_schema = _table_schema(source_connection, spec.table)
            _require_matching_schema(source_schema, target_schema, spec.key)
            source_rows = _exact_row_count(source_connection, spec.table)
            requested_rows = source_rows if limit_rows is None else min(source_rows, limit_rows)
            state.update(source_rows_exact=source_rows, rows_requested=requested_rows)
            emit("initial")

            if apply:
                columns = [column[0] for column in source_schema.columns]
                writer = _new_staging_writer(target_engine, page_size)
                batch_id = str(uuid4())
                for page_number, frame in enumerate(
                    _source_pages(
                        source_connection,
                        spec.table,
                        columns,
                        spec.key,
                        requested_rows,
                        page_size,
                    ),
                    start=1,
                ):
                    page_rows = len(frame)
                    if page_rows == 0 or page_rows > page_size:
                        raise RuntimeError("source reader returned an invalid page length")
                    rows_read = _state_counter(state, "rows_read") + page_rows
                    if rows_read > requested_rows:
                        raise RuntimeError("source reader exceeded the requested row count")
                    state["rows_read"] = rows_read
                    state["pages_read"] = page_number
                    result = writer.write(
                        frame,
                        table=spec.table,
                        key=spec.key,
                        source=spec.source,
                        batch_id=batch_id,
                    )
                    if result.rows != page_rows:
                        raise RuntimeError("ODS writer row count differed from the source page")
                    state["rows_written"] = _state_counter(state, "rows_written") + result.rows
                    state["pages_written"] = page_number
                    emit("batch", page_number=page_number, page_rows=page_rows)

                if _state_counter(state, "rows_read") != requested_rows:
                    raise RuntimeError("source stream ended before the exact requested row count")
                state["source_scan_complete"] = _state_counter(state, "rows_read") == source_rows
                with target_engine.connect() as target_connection:
                    target_rows_after = _exact_row_count(target_connection, spec.table)
                state["target_rows_after"] = target_rows_after
                if target_rows_after != _state_counter(state, "rows_written"):
                    raise RuntimeError("final target count differs from exact submitted row count")
                state["full_source_write_verified"] = (
                    _state_counter(state, "rows_read") == source_rows
                    and _state_counter(state, "rows_written") == source_rows
                )
                state["status"] = (
                    "complete"
                    if state["full_source_write_verified"] is True
                    else "limited-complete"
                )
            else:
                state["status"] = "dry-run"
                state["target_rows_after"] = target_rows_before
    except Exception as exc:
        state["status"] = "error"
        state["error_type"] = type(exc).__name__
    finally:
        disposed: set[int] = set()
        for engine in (source_engine, target_engine):
            if engine is not None and id(engine) not in disposed:
                disposed.add(id(engine))
                with suppress(Exception):
                    engine.dispose()
    return _finish(state, started, emit)


def _finish(
    state: dict[str, object],
    started: float,
    emit: Callable[..., None],
) -> dict[str, object]:
    """Stamp accurate completion counters and emit one final safe JSON record."""
    state["elapsed_seconds"] = round(time.monotonic() - started, 6)
    emit("final", **({} if "error_type" not in state else {"error_type": state["error_type"]}))
    return state


def _json_line(record: dict[str, object]) -> None:
    """Print one safe progress record as UTF-8 JSON Lines."""
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; default mode is validate/count only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        required=True,
        help="registered explicit stock_daily source (for example ths or akshare)",
    )
    parser.add_argument(
        "--page-size",
        type=_positive_int,
        default=DEFAULT_PAGE_SIZE,
        help=f"fixed fetch/write page size (default: {DEFAULT_PAGE_SIZE})",
    )
    parser.add_argument(
        "--limit-rows",
        type=_positive_int,
        help="read at most this many ordered source rows; reports a limited scope",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write to the guarded, caller-created loopback benchmark table",
    )
    args = parser.parse_args(argv)
    report = run_benchmark(
        source=args.source,
        apply=args.apply,
        page_size=args.page_size,
        limit_rows=args.limit_rows,
        progress=_json_line,
    )
    return 0 if report["status"] in {"dry-run", "complete", "limited-complete"} else 1


if __name__ == "__main__":
    sys.exit(main())
