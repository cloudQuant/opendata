"""Behavioral coverage for the fail-closed legacy runtime inventory."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from opendata.migration_inventory import build_inventory
from opendata.runtime_inventory import (
    METADATA_DATABASE,
    ApprovedEndpoint,
    RuntimeInventoryError,
    _canonical_json,
    _configure_mysql_readonly,
    _configure_sqlite_readonly,
    _driver_flag_is_true,
    _exact_count,
    _nested_exact_count,
    _parameter_summary,
    _read_rows,
    _resolve_columns,
    _safe_env_urls,
    _source_path,
    _sync_mysql_url,
    _task_map_rows,
    _validate_endpoint,
    _write_report,
    build_runtime_inventory,
    main,
)

if TYPE_CHECKING:
    from pathlib import Path


def _create_database_pair(tmp_path: Path, source_root: Path) -> tuple[str, str]:
    """Create task, script, and warehouse schemas in isolated SQLite files."""
    source_file = source_root / "src/backend/app/data_fetch/scripts/stocks/daily/stock_daily.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("def fetch_data():\n    return []\n", encoding="utf-8")

    metadata_path = tmp_path / "metadata.sqlite"
    metadata_engine = create_engine(f"sqlite:///{metadata_path}")
    with metadata_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE ak_scheduled_tasks ("
                "id INTEGER PRIMARY KEY, script_id TEXT, schedule_type TEXT, "
                "expression TEXT, is_active INTEGER, last_execution_at TEXT, "
                "next_execution_at TEXT, timeout INTEGER, max_retries INTEGER, "
                "retry_on_failure INTEGER, task_kind TEXT, parameters TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE ak_data_scripts ("
                "script_id TEXT PRIMARY KEY, module_path TEXT, function_name TEXT, "
                "target_table TEXT, is_active INTEGER, source TEXT, parameters TEXT)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO ak_scheduled_tasks VALUES "
                "(11, 'stock_daily', 'daily', '17:30', 1, NULL, NULL, 300, 3, 1, "
                '\'script\', \'{"window":"2025-01-01","api_key":"unit-test-secret-value"}\'), '
                "(12, 'stock_daily', 'manual', 'manual', 0, NULL, NULL, 60, 0, 0, "
                "'script', '{\"adjust\":\"qfq\"}')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO ak_data_scripts VALUES "
                "('stock_daily', 'app.data_fetch.scripts.stocks.daily.stock_daily', "
                "'fetch_data', 'stock_daily', 1, 'akshare', '{\"period\":\"daily\"}')"
            )
        )
    metadata_engine.dispose()

    warehouse_path = tmp_path / "warehouse.sqlite"
    warehouse_engine = create_engine(f"sqlite:///{warehouse_path}")
    with warehouse_engine.begin() as connection:
        connection.execute(text("CREATE TABLE stock_daily (symbol TEXT, date TEXT, close REAL)"))
        connection.execute(
            text("CREATE UNIQUE INDEX ix_stock_daily_symbol_date ON stock_daily(symbol, date)")
        )
        connection.execute(text("INSERT INTO stock_daily VALUES ('600000', '2026-10-23', 10.5)"))
    warehouse_engine.dispose()
    return f"sqlite:///{metadata_path}", f"sqlite:///{warehouse_path}"


def test_runtime_inventory_is_redacted_and_feeds_bidirectional_ast_join(
    tmp_path: Path,
) -> None:
    """A real task map joins the static script while all parameter values stay private."""
    source_root = tmp_path / "afi"
    source_root.mkdir()
    metadata_url, warehouse_url = _create_database_pair(tmp_path, source_root)

    report = build_runtime_inventory(
        metadata_url=metadata_url,
        warehouse_url=warehouse_url,
        source_root=source_root,
        env_file_sha256="0" * 64,
        allow_sqlite_for_tests=True,
    )

    serialized = json.dumps(report, sort_keys=True)
    assert "unit-test-secret-value" not in serialized
    assert "mysql://" not in serialized
    assert report["counts"]["scheduled_task_rows_exact"] == 2
    assert report["counts"]["scheduled_task_active_exact"] == 1
    assert report["counts"]["scheduled_task_inactive_exact"] == 1
    assert report["counts"]["data_script_rows_exact"] == 1
    assert report["counts"]["warehouse_tables"] == 1
    assert report["warehouse_schema"]["tables"][0]["estimated_rows"] is None

    task_rows = report["metadata"]["scheduled_tasks"]["records"]
    assert task_rows[0]["parameter_summary"]["credential_flag"] is True
    assert task_rows[0]["parameter_summary"]["key_names"] == ["api_key", "window"]
    script_record = report["metadata"]["data_scripts"]["records"][0]
    assert "module_path" not in script_record
    assert script_record["source_path"] == (
        "src/backend/app/data_fetch/scripts/stocks/daily/stock_daily.py"
    )
    assert len(report["tasks"]) == 2

    task_map_path = tmp_path / "task-map.json"
    task_map_path.write_text(json.dumps({"tasks": report["tasks"]}), encoding="utf-8")
    ast_report = build_inventory(source_root, task_map_path=task_map_path)
    assert ast_report.task_map_status == "LOADED"
    assert ast_report.mapping_issues == []
    assert ast_report.scripts[0].mapped_task_ids == ["scheduled:11", "scheduled:12"]

    engine = create_engine(metadata_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM ak_scheduled_tasks")).scalar_one()
                == 2
            )
    finally:
        engine.dispose()


def test_sqlite_is_test_only_for_runtime_inventory(tmp_path: Path) -> None:
    """The production callable rejects SQLite unless a test explicitly opts in."""
    source_root = tmp_path / "afi"
    source_root.mkdir()
    metadata_url, warehouse_url = _create_database_pair(tmp_path, source_root)

    with pytest.raises(RuntimeInventoryError, match="sqlite_is_test_only"):
        build_runtime_inventory(
            metadata_url=metadata_url,
            warehouse_url=warehouse_url,
            source_root=source_root,
        )


def test_mysql_readonly_setup_fails_closed_when_server_does_not_confirm_readonly() -> None:
    """A broken MySQL read-only flag cannot fall through to ordinary reads."""

    class Cursor:
        def __init__(self) -> None:
            self.row = (0,)

        def execute(self, statement: str) -> None:
            if statement.startswith("SELECT"):
                self.row = (0,)

        def fetchone(self) -> tuple[int, ...]:
            return self.row

        def close(self) -> None:
            return None

    class RawConnection:
        def cursor(self) -> Cursor:
            return Cursor()

        def commit(self) -> None:
            return None

    class DriverConnection:
        driver_connection = RawConnection()

    class Connection:
        connection = DriverConnection()

    with pytest.raises(RuntimeInventoryError, match="mysql_readonly_session_not_confirmed"):
        _configure_mysql_readonly(Connection())  # type: ignore[arg-type]


def test_dotenv_reads_only_allowlisted_urls_and_never_returns_other_secrets(
    tmp_path: Path,
) -> None:
    """The dotenv adapter exposes only approved URL keys plus a file digest."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=mysql+aiomysql://reader:do-not-print@127.0.0.1:3306/ai_for_investor\n"
        "AKSHARE_DATA_DATABASE_URL=mysql+aiomysql://reader:do-not-print@127.0.0.1:3306/akshare_data\n"
        "UNRELATED_SECRET=must-not-be-returned\n",
        encoding="utf-8",
    )

    metadata_url, warehouse_url, env_hash = _safe_env_urls(env_file)

    assert METADATA_DATABASE in metadata_url
    assert "akshare_data" in warehouse_url
    assert len(env_hash) == 64
    assert "UNRELATED_SECRET" not in metadata_url + warehouse_url
    assert "must-not-be-returned" not in metadata_url + warehouse_url


def test_output_refuses_overwrite(tmp_path: Path) -> None:
    """An existing file is never replaced by the inventory writer."""
    output = tmp_path / "runtime.json"
    output.write_text("preserve", encoding="utf-8")

    with pytest.raises(RuntimeInventoryError, match="output_already_exists"):
        _write_report(output, {"schema_version": "test"})

    assert output.read_text(encoding="utf-8") == "preserve"


def test_wrong_mysql_database_is_rejected_before_connecting() -> None:
    """Only exact, named local endpoints pass the URL identity guard."""
    with pytest.raises(RuntimeInventoryError, match="metadata_endpoint_not_approved"):
        _validate_endpoint(
            make_url("mysql+pymysql://reader:secret@127.0.0.1:3306/other"),
            ApprovedEndpoint("metadata", METADATA_DATABASE),
        )


def test_parameter_summary_hashes_values_and_flags_credentials_without_returning_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parameter diagnostics expose only names, a digest, and a heuristic flag."""
    from opendata import runtime_inventory

    summary = _parameter_summary(
        {
            "APIKey": "sk-test-very-secret-value",
            "nested": [{"period": "daily", "since": date(2026, 1, 2)}],
            "blob": b"private-binary",
            "price": Decimal("12.50"),
        }
    )
    assert summary["credential_flag"] is True
    assert summary["key_names"] == ["APIKey", "blob", "nested", "period", "price", "since"]
    assert "sk-test-very-secret-value" not in json.dumps(summary)
    assert "private-binary" not in json.dumps(summary)
    assert _parameter_summary("not valid json")["payload_present"] is True

    with pytest.raises(RuntimeInventoryError, match="not_canonical_json"):
        _canonical_json(float("nan"))
    with pytest.raises(RuntimeInventoryError, match="not_canonical_json"):
        _canonical_json(object())

    monkeypatch.setattr(runtime_inventory, "MAX_PARAMETER_BYTES", 2)
    with pytest.raises(RuntimeInventoryError, match="exceeds_limit"):
        _canonical_json({"x": 1})


def test_parameter_summary_rejects_excessive_depth_and_marks_non_string_keys() -> None:
    """Malformed JSON keys and excessive nesting stay explicit and private."""
    assert _parameter_summary({1: "private"})["credential_flag"] is True
    nested: object = "bottom"
    for _ in range(14):
        nested = [nested]
    with pytest.raises(RuntimeInventoryError, match="depth_exceeded"):
        _parameter_summary(nested)


def test_mysql_url_normalization_and_database_identity_fail_closed() -> None:
    """Only the supported local MySQL drivers and exact host/port/database pass."""
    endpoint = ApprovedEndpoint("metadata", METADATA_DATABASE)
    for driver in ("mysql+aiomysql", "mysql+asyncmy", "mysql"):
        converted = _sync_mysql_url(
            make_url(f"{driver}://reader:secret@127.0.0.1:3306/{METADATA_DATABASE}")
        )
        assert converted.drivername == "mysql+pymysql"
        assert _validate_endpoint(converted, endpoint).database == METADATA_DATABASE
    assert _driver_flag_is_true(True)
    assert _driver_flag_is_true(b"ON")
    assert not _driver_flag_is_true("off")

    with pytest.raises(RuntimeInventoryError, match="unsupported_mysql_driver"):
        _sync_mysql_url(make_url("mysql+mysqldb://reader:secret@127.0.0.1/ai_for_investor"))
    with pytest.raises(RuntimeInventoryError, match="backend_not_supported"):
        _validate_endpoint(
            make_url("postgresql://reader:secret@127.0.0.1/ai_for_investor"), endpoint
        )
    for wrong_url in (
        "mysql+pymysql://reader:secret@localhost:3306/ai_for_investor",
        "mysql+pymysql://reader:secret@127.0.0.1:3307/ai_for_investor",
    ):
        with pytest.raises(RuntimeInventoryError, match="endpoint_not_approved"):
            _validate_endpoint(make_url(wrong_url), endpoint)


def test_mysql_readonly_setup_confirms_success_and_rejects_bad_snapshot_state() -> None:
    """The transaction guard checks both the session flag and isolation state."""

    class Cursor:
        def __init__(self, rows: list[tuple[object, ...]], fail: bool = False) -> None:
            self.rows = iter(rows)
            self.fail = fail
            self.closed = False

        def execute(self, statement: str) -> None:
            if self.fail and statement.startswith("START TRANSACTION"):
                raise OSError("driver error with a private DSN")

        def fetchone(self) -> tuple[object, ...]:
            return next(self.rows)

        def close(self) -> None:
            self.closed = True

    class RawConnection:
        def __init__(self, rows: list[tuple[object, ...]], fail: bool = False) -> None:
            self.cursor_instance = Cursor(rows, fail)

        def cursor(self) -> Cursor:
            return self.cursor_instance

        def commit(self) -> None:
            return None

    class Wrapper:
        def __init__(self, raw: RawConnection | None) -> None:
            self.connection = type("Driver", (), {"driver_connection": raw})()

    good = RawConnection([(b"1",), (1, "REPEATABLE_READ")])
    _configure_mysql_readonly(Wrapper(good))  # type: ignore[arg-type]
    assert good.cursor_instance.closed

    with pytest.raises(RuntimeInventoryError, match="snapshot_not_confirmed"):
        _configure_mysql_readonly(  # type: ignore[arg-type]
            Wrapper(RawConnection([(1,), (0, "REPEATABLE-READ")]))
        )
    with pytest.raises(RuntimeInventoryError, match="isolation_not_confirmed"):
        _configure_mysql_readonly(  # type: ignore[arg-type]
            Wrapper(RawConnection([(1,), (1, "READ-COMMITTED")]))
        )
    with pytest.raises(RuntimeInventoryError, match="transaction_setup_failed"):
        _configure_mysql_readonly(  # type: ignore[arg-type]
            Wrapper(RawConnection([(1,)], fail=True))
        )
    with pytest.raises(RuntimeInventoryError, match="driver_connection_unavailable"):
        _configure_mysql_readonly(Wrapper(None))  # type: ignore[arg-type]


def test_sqlite_readonly_guard_and_schema_column_resolution_fail_closed() -> None:
    """A denied SQLite read-only assertion and missing required columns are red."""

    class ScalarResult:
        def scalar_one(self) -> int:
            return 0

    class BadConnection:
        def exec_driver_sql(self, statement: str) -> ScalarResult:
            return ScalarResult()

    with pytest.raises(RuntimeInventoryError, match="sqlite_readonly_not_confirmed"):
        _configure_sqlite_readonly(BadConnection())  # type: ignore[arg-type]
    with pytest.raises(RuntimeInventoryError, match="required_table_columns_missing"):
        _resolve_columns(
            {"id"}, {"id": ("id",), "script_id": ("script_id",)}, required={"script_id"}
        )
    assert _resolve_columns(
        {"id", "script_id"},
        {"id": ("id",), "script_id": ("script_id",)},
        required={"id", "script_id"},
    ) == {"id": "id", "script_id": "script_id"}
    with pytest.raises(RuntimeInventoryError, match="inventory_count_invalid"):
        _exact_count(True, "count")
    with pytest.raises(RuntimeInventoryError, match="inventory_section_missing"):
        _nested_exact_count({}, "missing", "count")


def test_row_reader_and_source_path_mapping_keep_unknowns_explicit(tmp_path: Path) -> None:
    """Invalid identifiers fail, while supported module spellings map exactly."""
    source_root = tmp_path / "afi"
    source_root.mkdir()
    assert _source_path(None, source_root=source_root) == (None, "MODULE_PATH_MISSING")
    assert _source_path("../outside.py", source_root=source_root) == (
        None,
        "MODULE_PATH_TRAVERSAL_REJECTED",
    )
    assert _source_path("not_a_known_package", source_root=source_root) == (
        None,
        "MODULE_NAMESPACE_UNRESOLVED",
    )
    assert _source_path("app.data_fetch.scripts.__init__", source_root=source_root) == (
        None,
        "MODULE_NOT_A_SCRIPT_FILE",
    )
    candidate, status = _source_path(
        "src/backend/app/data_fetch/scripts/stocks/history.py", source_root=source_root
    )
    assert candidate == "src/backend/app/data_fetch/scripts/stocks/history.py"
    assert status == "PATH_CANDIDATE"

    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)"))
            connection.execute(text("INSERT INTO sample VALUES (1, 'x')"))
        with engine.connect() as connection:
            assert _read_rows(
                connection,
                table="sample",
                resolved={"id": "id", "value": "value"},
                order_by="id",
            ) == [{"id": 1, "value": "x"}]
            with pytest.raises(RuntimeInventoryError, match="invalid_legacy_table_name"):
                _read_rows(
                    connection,
                    table="sample;drop",
                    resolved={"id": "id"},
                    order_by="id",
                )
            with pytest.raises(RuntimeInventoryError, match="no_allowlisted_table_columns"):
                _read_rows(
                    connection,
                    table="sample",
                    resolved={"id": None},
                    order_by="id",
                )
            with pytest.raises(RuntimeInventoryError, match="stable_order_column_missing"):
                _read_rows(
                    connection,
                    table="sample",
                    resolved={"id": "id"},
                    order_by="absent",
                )
    finally:
        engine.dispose()


def test_task_map_retains_scripts_without_tasks_and_unresolved_task_references() -> None:
    """Unmapped registered scripts and tasks lacking code links get explicit sentinels."""
    rows = _task_map_rows(
        [
            {"id": 1, "script_id": None, "is_active": True},
            {"id": 2, "script_id": "missing", "is_active": False},
            {"id": 3, "script_id": "mapped", "is_active": True},
        ],
        [
            {
                "script_id": "mapped",
                "source_path": "src/backend/app/data_fetch/scripts/mapped.py",
                "path_status": "PATH_CANDIDATE",
                "is_active": True,
            },
            {
                "script_id": "unlinked",
                "source_path": None,
                "path_status": "MODULE_NAMESPACE_UNRESOLVED",
                "is_active": False,
            },
        ],
    )
    assert len(rows) == 4
    assert rows[0]["path_status"] == "NO_SCRIPT_REFERENCE"
    assert rows[1]["path_status"] == "SCRIPT_REFERENCE_MISSING"
    assert rows[2]["script_path"] == "src/backend/app/data_fetch/scripts/mapped.py"
    assert rows[3]["runtime_task_kind"] == "script_registry_only"
    assert str(rows[0]["script_path"]).startswith("__unresolved__/task-")


def test_dotenv_missing_keys_and_main_error_output_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing config and unexpected DB failures return safe errors without DSNs."""
    from opendata import runtime_inventory

    empty_env = tmp_path / ".env"
    empty_env.write_text("UNRELATED_SECRET=do-not-print\n", encoding="utf-8")
    with pytest.raises(RuntimeInventoryError, match="dotenv_missing_DATABASE_URL"):
        _safe_env_urls(empty_env)

    source_root = tmp_path / "afi"
    source_root.mkdir()

    def explode(**_kwargs: object) -> dict[str, object]:
        raise ValueError("mysql://reader:do-not-print@private.example/database")

    monkeypatch.setattr(runtime_inventory, "build_runtime_inventory", explode)
    exit_code = main(
        [
            "--metadata-url",
            "mysql+pymysql://reader:private@127.0.0.1:3306/ai_for_investor",
            "--warehouse-url",
            "mysql+pymysql://reader:private@127.0.0.1:3306/akshare_data",
            "--source-root",
            str(source_root),
            "--output",
            str(tmp_path / "failed.json"),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "unexpected internal error" in captured.err
    assert "do-not-print" not in captured.err
