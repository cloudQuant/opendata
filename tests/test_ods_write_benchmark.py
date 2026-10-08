"""Offline tests for the explicitly gated ODS write benchmark."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, cast

import pandas as pd
import pytest

from opendata.pipeline.ods_writer import WriteResult
from scripts.ops import benchmark_ods_write as benchmark

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

SOURCE_URL = "mysql+pymysql://source_user:source_secret@warehouse.example:3306/opendata_data"
TARGET_URL = "mysql+pymysql://target_user:target_secret@127.0.0.1:33565/opendata_c65_bench"
KEY = ("thscode", "trade_date")
SCHEMA = benchmark.TableSchema(
    columns=(
        ("close_price", "decimal(20,6)", True),
        ("thscode", "varchar(16)", False),
        ("trade_date", "date", False),
    ),
    primary_key=KEY,
)


class _ConnectionContext:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def __enter__(self) -> _FakeConnection:
        return self.connection

    def __exit__(self, *args: object) -> None:
        return None


class _FakeConnection:
    def __init__(self, role: str) -> None:
        self.role = role
        self.executed: list[tuple[str, object]] = []
        self.in_transaction = False
        self.row_count = 0

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def begin(self) -> _ConnectionContext:
        self.in_transaction = True
        return _ConnectionContext(self)

    def execution_options(self, **options: object) -> _FakeConnection:
        self.executed.append(("execution_options", options))
        return self

    def execute(self, statement: object, parameters: object = None) -> _OneRowResult:
        self.executed.append((str(statement), parameters))
        return _OneRowResult((1, "REPEATABLE-READ"))


class _OneRowResult:
    def __init__(self, row: tuple[object, ...]) -> None:
        self.row = row

    def one(self) -> tuple[object, ...]:
        return self.row


class _FakeEngine:
    def __init__(self, role: str) -> None:
        self.role = role
        self.connection = _FakeConnection(role)
        self.disposed = False

    def connect(self) -> _ConnectionContext:
        return _ConnectionContext(self.connection)

    def dispose(self) -> None:
        self.disposed = True


class _FakeWriter:
    def __init__(self, target_engine: _FakeEngine, page_size: int) -> None:
        self.target_engine = target_engine
        self.page_size = page_size
        self.frames: list[pd.DataFrame] = []

    def write(
        self,
        frame: pd.DataFrame,
        *,
        table: str,
        key: tuple[str, ...],
        source: str,
        batch_id: str,
    ) -> WriteResult:
        assert table == "ods_stock_daily_ths"
        assert key == KEY
        assert source == "ths"
        assert batch_id
        assert len(frame) <= self.page_size
        self.frames.append(frame.copy())
        self.target_engine.connection.row_count += len(frame)
        return WriteResult(rows=len(frame), batches=1)


def _patch_benchmark(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_rows: int = 5,
    target_rows: int = 0,
    page_lengths: tuple[int, ...] = (2, 2, 1),
    source_failure_after_pages: int | None = None,
) -> tuple[_FakeEngine, _FakeEngine, list[_FakeWriter]]:
    source_engine = _FakeEngine("source")
    target_engine = _FakeEngine("target")
    target_engine.connection.row_count = target_rows
    writers: list[_FakeWriter] = []

    def new_engine(url: str) -> _FakeEngine:
        assert url in {SOURCE_URL, TARGET_URL}
        return source_engine if url == SOURCE_URL else target_engine

    @contextmanager
    def source_snapshot(_engine: object):
        yield source_engine.connection

    def exact_count(connection: object, table: str) -> int:
        assert table == "ods_stock_daily_ths"
        if connection is source_engine.connection:
            return source_rows
        return int(target_engine.connection.row_count)

    def table_schema(_connection: object, table: str) -> benchmark.TableSchema:
        assert table == "ods_stock_daily_ths"
        return SCHEMA

    def pages(
        _connection: object,
        table: str,
        columns: list[str],
        key: tuple[str, ...],
        limit_rows: int | None,
        page_size: int,
    ):
        assert table == "ods_stock_daily_ths"
        assert key == KEY
        assert page_size > 0
        remaining = source_rows if limit_rows is None else min(source_rows, limit_rows)
        emitted = 0
        for page_number, page_length in enumerate(page_lengths, start=1):
            if emitted >= remaining:
                break
            if source_failure_after_pages == page_number:
                raise RuntimeError("failure with source_secret and target_secret")
            size = min(page_length, page_size, remaining - emitted)
            rows = [
                (
                    10.0,
                    f"SYM{emitted + offset:04d}",
                    f"2026-01-{(emitted + offset) % 28 + 1:02d}",
                )
                for offset in range(size)
            ]
            emitted += size
            yield pd.DataFrame(rows, columns=columns)
        if emitted < remaining and source_failure_after_pages is None:
            raise AssertionError("fake source pages did not cover the requested row count")

    def new_writer(engine: object, page_size: int) -> _FakeWriter:
        assert engine is target_engine
        writer = _FakeWriter(target_engine, page_size)
        writers.append(writer)
        return writer

    monkeypatch.setattr(benchmark, "_new_engine", new_engine)
    monkeypatch.setattr(benchmark, "_readonly_source_snapshot", source_snapshot)
    monkeypatch.setattr(benchmark, "_exact_row_count", exact_count)
    monkeypatch.setattr(benchmark, "_table_schema", table_schema)
    monkeypatch.setattr(benchmark, "_source_pages", pages)
    monkeypatch.setattr(benchmark, "_new_staging_writer", new_writer)
    monkeypatch.setattr(
        benchmark,
        "_memory_reading",
        lambda: {
            "current_rss_bytes": 4096,
            "peak_rss_bytes": 8192,
            "rss_unit": "bytes",
            "current_rss_source": "ps",
            "peak_rss_source": "resource.getrusage",
        },
    )
    return source_engine, target_engine, writers


def _run(
    monkeypatch: pytest.MonkeyPatch, **overrides: object
) -> tuple[
    dict[str, object], list[dict[str, object]], tuple[_FakeEngine, _FakeEngine, list[_FakeWriter]]
]:
    fixtures = _patch_benchmark(monkeypatch)
    records: list[dict[str, object]] = []
    report = benchmark.run_benchmark(
        source="ths",
        source_url=SOURCE_URL,
        target_url=TARGET_URL,
        progress=records.append,
        **overrides,
    )
    return report, records, fixtures


def test_url_guard_accepts_only_isolated_loopback_benchmark_database() -> None:
    benchmark._validate_database_urls(SOURCE_URL, TARGET_URL)
    rejected = (
        "mysql+pymysql://u:p@remote.example:33565/opendata_c65_bench",
        "mysql+pymysql://u:p@127.0.0.1:3306/opendata_c65_bench",
        "mysql+pymysql://u:p@127.0.0.1:33565/opendata_data",
    )
    for target_url in rejected:
        with pytest.raises(ValueError):
            benchmark._validate_database_urls(SOURCE_URL, target_url)
    same_url = "mysql+pymysql://u:p@127.0.0.1:33565/opendata_c65_bench"
    with pytest.raises(ValueError, match="different"):
        benchmark._validate_database_urls(same_url, same_url)


def test_source_sql_guard_allows_reads_and_session_setup_but_blocks_writes() -> None:
    for statement in (
        "SELECT COUNT(*) FROM ods_stock_daily_ths",
        "SHOW COLUMNS FROM ods_stock_daily_ths",
        "EXPLAIN SELECT 1",
        "SET SESSION TRANSACTION READ ONLY",
    ):
        benchmark._source_statement_guard(None, None, statement, None, None, False)
    for statement in (
        "INSERT INTO ods_stock_daily_ths VALUES (1)",
        "DELETE FROM ods_stock_daily_ths",
        "CREATE TABLE x (id int)",
    ):
        with pytest.raises(PermissionError):
            benchmark._source_statement_guard(None, None, statement, None, None, False)


class _RawCursor:
    def __init__(self, read_only: object) -> None:
        self.read_only = read_only
        self.statements: list[str] = []
        self.closed = False

    def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def fetchone(self) -> tuple[object]:
        return (self.read_only,)

    def close(self) -> None:
        self.closed = True


class _RawDriverConnection:
    def __init__(self, read_only: object) -> None:
        self.cursor_value = _RawCursor(read_only)
        self.committed = False

    def cursor(self) -> _RawCursor:
        return self.cursor_value

    def commit(self) -> None:
        self.committed = True


def test_readonly_session_sets_and_verifies_mysql_flag() -> None:
    driver = _RawDriverConnection(1)
    connection = type(
        "Connection", (), {"connection": type("DBAPI", (), {"driver_connection": driver})()}
    )()

    benchmark._configure_readonly_session(cast("Connection", connection))

    assert driver.cursor_value.statements == [
        "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "SET SESSION TRANSACTION READ ONLY",
        "SELECT @@session.transaction_read_only",
    ]
    assert driver.cursor_value.closed is True
    assert driver.committed is True

    bad_driver = _RawDriverConnection("0")
    bad_connection = type(
        "Connection", (), {"connection": type("DBAPI", (), {"driver_connection": bad_driver})()}
    )()
    with pytest.raises(RuntimeError, match="did not confirm read-only"):
        benchmark._configure_readonly_session(cast("Connection", bad_connection))


def test_readonly_snapshot_verifies_transaction_mode_and_repeatable_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine("source")
    listeners: list[str] = []
    monkeypatch.setattr(benchmark.event, "listen", lambda *_args: listeners.append("listen"))
    monkeypatch.setattr(benchmark.event, "remove", lambda *_args: listeners.append("remove"))
    monkeypatch.setattr(benchmark, "_configure_readonly_session", lambda _connection: None)

    with benchmark._readonly_source_snapshot(cast("Engine", engine)) as connection:
        assert connection is engine.connection
        assert connection.in_transaction is True
        assert connection.executed[-1][0].startswith("SELECT @@session.transaction_read_only")

    assert listeners == ["listen", "remove"]


class _FetchResult:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.fetch_sizes: list[int] = []
        self.closed = False

    def fetchmany(self, size: int) -> list[tuple[object, ...]]:
        self.fetch_sizes.append(size)
        rows, self.rows = self.rows[:size], self.rows[size:]
        return rows

    def close(self) -> None:
        self.closed = True


class _PageConnection:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.result = _FetchResult(rows)
        self.statement = ""
        self.parameters: object = None
        self.options: dict[str, object] = {}

    def execution_options(self, **options: object) -> _PageConnection:
        self.options = options
        return self

    def execute(self, statement: object, parameters: object) -> _FetchResult:
        self.statement = str(statement)
        self.parameters = parameters
        return self.result


class _ReflectionResult:
    def __init__(
        self,
        rows: list[tuple[object, ...]],
        scalar: object = None,
    ) -> None:
        self.rows = rows
        self.scalar = scalar

    def scalar_one_or_none(self) -> object:
        return self.scalar

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


class _ReflectionConnection:
    def __init__(self, *, exists: bool = True) -> None:
        self.exists = exists
        self.queries: list[tuple[str, object]] = []

    def execute(self, statement: object, parameters: object) -> _ReflectionResult:
        sql = str(statement)
        self.queries.append((sql, parameters))
        if "information_schema.TABLES" in sql:
            return _ReflectionResult([], "ods_stock_daily_ths" if self.exists else None)
        if "information_schema.COLUMNS" in sql:
            return _ReflectionResult(
                [
                    ("close_price", "decimal(20,6)", "YES"),
                    ("thscode", "varchar(16)", "NO"),
                    ("trade_date", "date", "NO"),
                ]
            )
        if "information_schema.KEY_COLUMN_USAGE" in sql:
            return _ReflectionResult([("thscode",), ("trade_date",)])
        raise AssertionError(f"unexpected reflection query: {sql}")


def test_table_reflection_uses_bound_information_schema_selects_only() -> None:
    connection = _ReflectionConnection()

    schema = benchmark._table_schema(cast("Connection", connection), "ods_stock_daily_ths")

    assert schema == SCHEMA
    assert len(connection.queries) == 3
    for sql, parameters in connection.queries:
        benchmark._source_statement_guard(None, None, sql, parameters, None, False)
        assert sql.lstrip().startswith("SELECT")
        assert ":c65_table" in sql
        assert parameters == {"c65_table": "ods_stock_daily_ths"}
        assert "DESCRIBE" not in sql.upper()


def test_table_reflection_fails_closed_when_table_is_missing() -> None:
    connection = _ReflectionConnection(exists=False)

    with pytest.raises(LookupError, match="table is missing"):
        benchmark._table_schema(cast("Connection", connection), "ods_stock_daily_ths")

    assert len(connection.queries) == 1


def test_source_reader_fetches_fixed_bounded_pages_with_bound_limit() -> None:
    connection = _PageConnection([(f"S{i}", f"2026-01-{i:02d}") for i in range(1, 4)])
    pages = list(
        benchmark._source_pages(
            cast("Connection", connection),
            "ods_stock_daily_ths",
            KEY,
            KEY,
            limit_rows=3,
            page_size=2,
        )
    )

    assert [len(page) for page in pages] == [2, 1]
    assert connection.options == {"stream_results": True, "max_row_buffer": 2}
    assert "ORDER BY `thscode`, `trade_date`" in connection.statement
    assert "LIMIT :c65_row_limit" in connection.statement
    assert connection.parameters == {"c65_row_limit": 3}
    assert connection.result.fetch_sizes == [2, 1]
    assert connection.result.closed is True


def test_dry_run_counts_without_constructing_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    source_engine, target_engine, writers = _patch_benchmark(monkeypatch)
    records: list[dict[str, object]] = []
    monkeypatch.setattr(
        benchmark,
        "_new_staging_writer",
        lambda *_args: pytest.fail("dry-run must not instantiate a writer"),
    )

    report = benchmark.run_benchmark(
        source="ths", source_url=SOURCE_URL, target_url=TARGET_URL, progress=records.append
    )

    assert report["status"] == "dry-run"
    assert report["source_rows_exact"] == 5
    assert report["rows_requested"] == 5
    assert report["rows_read"] == report["rows_written"] == 0
    assert report["target_rows_before"] == report["target_rows_after"] == 0
    assert writers == []
    assert [(record["record"], record["status"]) for record in records] == [
        ("initial", "running"),
        ("final", "dry-run"),
    ]
    assert source_engine.disposed is target_engine.disposed is True


def test_default_source_and_target_environment_are_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_engine, target_engine, _writers = _patch_benchmark(monkeypatch)
    monkeypatch.setattr(
        benchmark,
        "get_settings",
        lambda: type("SettingsStub", (), {"data_database_url": SOURCE_URL})(),
    )
    monkeypatch.setenv(benchmark.TARGET_URL_ENV, TARGET_URL)

    report = benchmark.run_benchmark(source="ths")

    assert report["status"] == "dry-run"
    assert source_engine.disposed is target_engine.disposed is True


def test_apply_reports_exact_counts_and_rss_in_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    report, records, (source_engine, target_engine, writers) = _run(
        monkeypatch, apply=True, page_size=2
    )

    assert report["status"] == "complete"
    assert report["source_rows_exact"] == report["rows_requested"] == 5
    assert report["rows_read"] == report["rows_written"] == 5
    assert report["pages_read"] == report["pages_written"] == 3
    assert report["target_rows_after"] == 5
    assert report["source_scan_complete"] is True
    assert report["full_source_write_verified"] is True
    assert [len(frame) for frame in writers[0].frames] == [2, 2, 1]
    assert [(record["record"], record["status"]) for record in records] == [
        ("initial", "running"),
        ("batch", "running"),
        ("batch", "running"),
        ("batch", "running"),
        ("final", "complete"),
    ]
    assert {record["rss_unit"] for record in records} == {"bytes"}
    assert all(record["current_rss_bytes"] == 4096 for record in records)
    assert all(record["peak_rss_bytes"] == 8192 for record in records)
    assert source_engine.disposed is target_engine.disposed is True


def test_limited_apply_reports_full_source_count_and_limited_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _records, (_source_engine, _target_engine, writers) = _run(
        monkeypatch, apply=True, page_size=2, limit_rows=3
    )

    assert report["status"] == "limited-complete"
    assert report["source_rows_exact"] == 5
    assert report["rows_requested"] == report["rows_read"] == report["rows_written"] == 3
    assert report["target_rows_after"] == 3
    assert report["source_scan_complete"] is False
    assert report["full_source_write_verified"] is False
    assert [len(frame) for frame in writers[0].frames] == [2, 1]


def test_apply_rejects_nonempty_target_before_source_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    source_engine, target_engine, writers = _patch_benchmark(monkeypatch, target_rows=4)
    source_reads: list[bool] = []
    records: list[dict[str, object]] = []

    @contextmanager
    def source_snapshot(_engine: object):
        source_reads.append(True)
        yield source_engine.connection

    monkeypatch.setattr(benchmark, "_readonly_source_snapshot", source_snapshot)
    report = benchmark.run_benchmark(
        source="ths",
        apply=True,
        source_url=SOURCE_URL,
        target_url=TARGET_URL,
        progress=records.append,
    )

    assert report["status"] == "error"
    assert report["error_type"] == "ValueError"
    assert report["target_rows_before"] == 4
    assert [(record["record"], record["status"]) for record in records] == [("final", "error")]
    assert records[0]["error_type"] == "ValueError"
    assert source_reads == []
    assert writers == []
    assert source_engine.disposed is target_engine.disposed is True


@pytest.mark.parametrize(
    "target_schema",
    [
        benchmark.TableSchema(
            columns=(
                ("close_price", "decimal(20,6)", False),
                *SCHEMA.columns[1:],
            ),
            primary_key=KEY,
        ),
        benchmark.TableSchema(columns=SCHEMA.columns, primary_key=("trade_date", "thscode")),
    ],
    ids=("columns-differ", "key-order-differs"),
)
def test_apply_rejects_source_target_schema_or_key_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    target_schema: benchmark.TableSchema,
) -> None:
    source_engine, target_engine, writers = _patch_benchmark(monkeypatch)

    def mismatched_schema(_connection: object, _table: str) -> benchmark.TableSchema:
        if _connection is source_engine.connection:
            return SCHEMA
        return target_schema

    monkeypatch.setattr(benchmark, "_table_schema", mismatched_schema)
    report = benchmark.run_benchmark(
        source="ths", apply=True, source_url=SOURCE_URL, target_url=TARGET_URL
    )

    assert report["status"] == "error"
    assert report["error_type"] == "ValueError"
    assert report["rows_written"] == 0
    assert writers == []
    assert source_engine.disposed is target_engine.disposed is True


def test_source_failure_keeps_partial_counts_disposes_and_redacts_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_engine, target_engine, _writers = _patch_benchmark(
        monkeypatch, page_lengths=(2, 2, 1), source_failure_after_pages=2
    )
    records: list[dict[str, object]] = []

    def record(item: dict[str, object]) -> None:
        if item["record"] == "final":
            assert source_engine.disposed is True
            assert target_engine.disposed is True
        records.append(item)

    report = benchmark.run_benchmark(
        source="ths",
        apply=True,
        page_size=2,
        source_url=SOURCE_URL,
        target_url=TARGET_URL,
        progress=record,
    )

    serialized = json.dumps({"report": report, "records": records}, sort_keys=True)
    assert report["status"] == "error"
    assert report["error_type"] == "RuntimeError"
    assert report["rows_read"] == report["rows_written"] == 2
    assert report["pages_read"] == report["pages_written"] == 1
    assert report["target_rows_after"] is None
    assert "source_secret" not in serialized
    assert "target_secret" not in serialized
    assert "warehouse.example" not in serialized
    assert "failure with" not in serialized
    assert records[-1]["record"] == "final"
    assert [(record["record"], record["status"]) for record in records] == [
        ("initial", "running"),
        ("batch", "running"),
        ("final", "error"),
    ]
    assert source_engine.disposed is target_engine.disposed is True


def test_final_progress_is_emitted_after_engines_are_disposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_engine, target_engine, _writers = _patch_benchmark(monkeypatch)
    records: list[dict[str, object]] = []

    def record(item: dict[str, object]) -> None:
        if item["record"] == "final":
            assert source_engine.disposed is True
            assert target_engine.disposed is True
        records.append(item)

    benchmark.run_benchmark(
        source="ths", source_url=SOURCE_URL, target_url=TARGET_URL, progress=record
    )

    assert records[-1]["record"] == "final"


def test_rss_resource_normalizes_macos_bytes_and_linux_kibibytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        benchmark.resource, "getrusage", lambda _who: type("Usage", (), {"ru_maxrss": 7})()
    )
    monkeypatch.setattr(benchmark, "_current_rss_bytes", lambda: 11)
    monkeypatch.setattr(benchmark.sys, "platform", "darwin")
    mac = benchmark._memory_reading()
    monkeypatch.setattr(benchmark.sys, "platform", "linux")
    linux = benchmark._memory_reading()

    assert mac["peak_rss_bytes"] == 7
    assert linux["peak_rss_bytes"] == 7 * 1024
    assert mac["current_rss_bytes"] == linux["current_rss_bytes"] == 11
    assert mac["rss_unit"] == linux["rss_unit"] == "bytes"
