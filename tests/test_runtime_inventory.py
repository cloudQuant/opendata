"""Behavioral coverage for the fail-closed legacy runtime inventory."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from opendata.migration_inventory import build_inventory
from opendata.runtime_inventory import (
    MAX_ENV_FILE_BYTES,
    MAX_IDENTITY_PIN_BYTES,
    METADATA_DATABASE,
    SCRIPT_TABLE,
    TASK_TABLE,
    WAREHOUSE_DATABASE,
    ApprovedEndpoint,
    RuntimeInventoryError,
    _canonical_json,
    _configure_mysql_readonly,
    _configure_sqlite_readonly,
    _driver_flag_is_true,
    _exact_count,
    _file_hash,
    _metadata_inventory,
    _nested_exact_count,
    _parameter_summary,
    _parse_args,
    _read_rows,
    _readonly_snapshot,
    _report_scalar,
    _resolve_columns,
    _safe_env_urls,
    _safe_identity_pins,
    _source_path,
    _sqlite_warehouse_schema,
    _sync_mysql_url,
    _task_map_rows,
    _validate_endpoint,
    _validate_expected_server_uuids,
    _write_report,
    build_runtime_inventory,
    main,
)

_MYSQL_UUID = "123e4567-e89b-12d3-a456-426614174000"
_WAREHOUSE_UUID = "123e4567-e89b-12d3-a456-426614174001"


def _write_identity_file(
    path: Path,
    *,
    metadata_uuid: str = _MYSQL_UUID,
    warehouse_uuid: str = _MYSQL_UUID,
) -> Path:
    """Create a synthetic operator pin file for CLI contract tests."""
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "metadata": {"database": METADATA_DATABASE, "server_uuid": metadata_uuid},
                "warehouse": {"database": WAREHOUSE_DATABASE, "server_uuid": warehouse_uuid},
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


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
    env_file = tmp_path / "verified-service.env"
    env_file.write_text(
        f"DATABASE_URL={metadata_url}\nAKSHARE_DATA_DATABASE_URL={warehouse_url}\n",
        encoding="utf-8",
    )
    identity_file = _write_identity_file(tmp_path / "database-identities.json")

    report = build_runtime_inventory(
        source_root=source_root,
        env_file=env_file,
        identity_file=identity_file,
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
    assert report["runtime_table_engine_check"] is None
    assert report["warehouse_schema"]["tables"][0]["estimated_rows"] is None
    assert report["env_file_sha256"] == hashlib.sha256(env_file.read_bytes()).hexdigest()
    assert report["identity_pin_sha256"] == hashlib.sha256(identity_file.read_bytes()).hexdigest()

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
        "export UNRELATED_SECRET=must-not-be-returned\n"
        "UNRELATED_SETTING=first\nUNRELATED_SETTING=second\n",
        encoding="utf-8",
    )

    metadata_url, warehouse_url, env_hash = _safe_env_urls(env_file)

    assert METADATA_DATABASE in metadata_url
    assert "akshare_data" in warehouse_url
    assert len(env_hash) == 64
    assert "UNRELATED_SECRET" not in metadata_url + warehouse_url
    assert "must-not-be-returned" not in metadata_url + warehouse_url


@pytest.mark.parametrize("target_key", ["DATABASE_URL", "AKSHARE_DATA_DATABASE_URL"])
def test_dotenv_rejects_duplicate_target_keys_without_exposing_values(
    tmp_path: Path, target_key: str
) -> None:
    """Duplicate approved keys cannot select a different endpoint by parser order."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=sqlite:///metadata.db\n"
        "AKSHARE_DATA_DATABASE_URL=sqlite:///warehouse.db\n"
        f"{target_key}=mysql+pymysql://reader:secret-value@127.0.0.1:3306/decoy\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeInventoryError, match="dotenv_target_key_duplicate") as error:
        _safe_env_urls(env_file)
    assert "secret-value" not in str(error.value)


def test_dotenv_rejects_malformed_unrelated_line_fail_closed(tmp_path: Path) -> None:
    """Malformed dotenv syntax fails even when it is unrelated to URL keys."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=sqlite:///metadata.db\n"
        "AKSHARE_DATA_DATABASE_URL=sqlite:///warehouse.db\n"
        'UNRELATED_SETTING="unterminated\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeInventoryError, match="dotenv_file_invalid"):
        _safe_env_urls(env_file)


def test_dotenv_file_is_bounded_before_parsing(tmp_path: Path) -> None:
    """Oversized dotenv inputs fail before parsing or hashing their contents."""
    with pytest.raises(RuntimeInventoryError, match="dotenv_file_not_regular"):
        _safe_env_urls(tmp_path)

    env_file = tmp_path / ".env"
    env_file.write_bytes(b"x" * (MAX_ENV_FILE_BYTES + 1))

    with pytest.raises(RuntimeInventoryError, match="dotenv_file_exceeds_limit"):
        _safe_env_urls(env_file)


def test_dotenv_urls_and_digest_use_exactly_one_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The URL parser and reported digest cannot come from separate file opens."""
    from opendata import runtime_inventory

    first_content = (
        b"DATABASE_URL=sqlite:///first.db\nAKSHARE_DATA_DATABASE_URL=sqlite:///first-warehouse.db\n"
    )
    read_count = 0

    def read_once(_path: Path) -> bytes:
        nonlocal read_count
        read_count += 1
        if read_count > 1:
            raise AssertionError("dotenv content must be read once")
        return first_content

    monkeypatch.setattr(runtime_inventory, "_read_env_file_bytes", read_once)
    metadata_url, warehouse_url, digest = _safe_env_urls(Path("unused.env"))

    assert metadata_url == "sqlite:///first.db"
    assert warehouse_url == "sqlite:///first-warehouse.db"
    assert digest == hashlib.sha256(first_content).hexdigest()
    assert read_count == 1


def test_inventory_api_rejects_disconnected_or_conflicting_env_inputs(tmp_path: Path) -> None:
    """Callers cannot inject a standalone dotenv digest or combine URL sources."""
    with pytest.raises(TypeError):
        build_runtime_inventory(
            source_root=tmp_path,
            metadata_url="sqlite://",
            warehouse_url="sqlite://",
            env_file_sha256="0" * 64,
        )  # type: ignore[call-arg]

    with pytest.raises(RuntimeInventoryError, match="database_url_source_conflict"):
        build_runtime_inventory(
            source_root=tmp_path,
            metadata_url="sqlite://",
            env_file=tmp_path / "not-read.env",
        )


def test_identity_pin_file_is_strict_bounded_and_digest_pinned(tmp_path: Path) -> None:
    """Reviewed UUID pins bind the two logical databases and reject ambiguity."""
    pin_file = _write_identity_file(
        tmp_path / "database-identities.json", warehouse_uuid=_WAREHOUSE_UUID
    )

    pins, digest = _safe_identity_pins(pin_file)

    assert pins == {"metadata": _MYSQL_UUID, "warehouse": _WAREHOUSE_UUID}
    assert digest == hashlib.sha256(pin_file.read_bytes()).hexdigest()
    assert _validate_expected_server_uuids(pins) == pins

    pin_file.write_text(
        '{"schema_version":1,"metadata":{"database":"ai_for_investor",'
        '"server_uuid":"123e4567-e89b-12d3-a456-426614174000"},'
        '"metadata":{"database":"ai_for_investor",'
        '"server_uuid":"123e4567-e89b-12d3-a456-426614174000"},'
        '"warehouse":{"database":"akshare_data",'
        '"server_uuid":"123e4567-e89b-12d3-a456-426614174000"}}',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeInventoryError, match="identity_pin_duplicate_json_key"):
        _safe_identity_pins(pin_file)

    pin_file.write_bytes(b"x" * (MAX_IDENTITY_PIN_BYTES + 1))
    with pytest.raises(RuntimeInventoryError, match="identity_pin_file_exceeds_limit"):
        _safe_identity_pins(pin_file)


def test_identity_pin_file_rejects_symlinks_directories_and_unsafe_modes(
    tmp_path: Path,
) -> None:
    """The pin file must be an owned regular file with no group/other write access."""
    target = _write_identity_file(tmp_path / "target.json")
    symlink = tmp_path / "pin-link.json"
    symlink.symlink_to(target)
    with pytest.raises(RuntimeInventoryError, match="identity_pin_file_symlink_rejected"):
        _safe_identity_pins(symlink)

    with pytest.raises(RuntimeInventoryError, match="identity_pin_file_not_regular"):
        _safe_identity_pins(tmp_path)

    target.chmod(0o666)
    with pytest.raises(RuntimeInventoryError, match="identity_pin_file_writable_by_group_or_other"):
        _safe_identity_pins(target)

    target.chmod(0o744)
    with pytest.raises(RuntimeInventoryError, match="identity_pin_file_permissions_not_trusted"):
        _safe_identity_pins(target)


def test_identity_pin_file_rejects_untrusted_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A readable pin file owned by an unrelated account is not trusted."""
    from types import SimpleNamespace

    from opendata import runtime_inventory

    pin_file = _write_identity_file(tmp_path / "database-identities.json")
    actual_fstat = runtime_inventory._identity_file_stat

    def wrong_owner(descriptor: int) -> SimpleNamespace:
        observed = actual_fstat(descriptor)
        return SimpleNamespace(st_mode=observed.st_mode, st_uid=12345)

    monkeypatch.setattr(runtime_inventory, "_identity_file_stat", wrong_owner)
    monkeypatch.setattr(runtime_inventory, "_identity_file_effective_uid", lambda: 23456)

    with pytest.raises(RuntimeInventoryError, match="identity_pin_file_owner_not_trusted"):
        _safe_identity_pins(pin_file)


@pytest.mark.parametrize(
    ("metadata_database", "metadata_uuid"),
    [("other_database", _MYSQL_UUID), (METADATA_DATABASE, "not-a-uuid")],
)
def test_identity_pin_file_rejects_wrong_database_and_invalid_uuid(
    tmp_path: Path, metadata_database: str, metadata_uuid: str
) -> None:
    """A pin cannot be reused for another schema or contain a malformed UUID."""
    pin_file = tmp_path / "database-identities.json"
    pin_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "metadata": {"database": metadata_database, "server_uuid": metadata_uuid},
                "warehouse": {"database": WAREHOUSE_DATABASE, "server_uuid": _MYSQL_UUID},
            }
        ),
        encoding="utf-8",
    )
    pin_file.chmod(0o600)

    with pytest.raises(RuntimeInventoryError, match="identity_pin_file_invalid"):
        _safe_identity_pins(pin_file)


def test_expected_server_uuid_mapping_requires_both_canonical_roles() -> None:
    """A production pin set is complete, role-specific, and canonical."""
    assert _validate_expected_server_uuids({"metadata": _MYSQL_UUID, "warehouse": _MYSQL_UUID}) == {
        "metadata": _MYSQL_UUID,
        "warehouse": _MYSQL_UUID,
    }
    for invalid in (
        {"metadata": _MYSQL_UUID},
        {"metadata": _MYSQL_UUID, "warehouse": "not-a-uuid"},
        {"metadata": _MYSQL_UUID.upper(), "warehouse": _MYSQL_UUID},
    ):
        with pytest.raises(RuntimeInventoryError, match="mysql_identity_pins_invalid"):
            _validate_expected_server_uuids(invalid)  # type: ignore[arg-type]
    assert _validate_expected_server_uuids(None) is None


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


def test_mysql_endpoint_accepts_only_empty_query_or_one_utf8mb4_charset() -> None:
    """A single known encoding option is the only accepted MySQL URL query."""
    endpoint = ApprovedEndpoint("metadata", METADATA_DATABASE)
    base = f"mysql+pymysql://reader:private-password@127.0.0.1:3306/{METADATA_DATABASE}"

    assert _validate_endpoint(make_url(base), endpoint).database == METADATA_DATABASE
    assert (
        _validate_endpoint(make_url(f"{base}?charset=utf8mb4"), endpoint).query["charset"]
        == "utf8mb4"
    )


def test_mysql_runtime_inventory_requires_explicit_pins_before_engine_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production MySQL inventory fails before creating an unpinned connection."""
    from opendata import runtime_inventory

    def unexpected_connection(*_args: object, **_kwargs: object) -> object:
        pytest.fail("an unpinned MySQL endpoint must not create an engine")

    monkeypatch.setattr(runtime_inventory, "create_engine", unexpected_connection)
    with pytest.raises(RuntimeInventoryError, match="mysql_identity_pins_required"):
        build_runtime_inventory(
            metadata_url=("mysql+pymysql://reader:private@127.0.0.1:3306/ai_for_investor"),
            warehouse_url=("mysql+pymysql://reader:private@127.0.0.1:3306/akshare_data"),
            source_root=tmp_path,
        )


@pytest.mark.parametrize(
    "query",
    [
        "unix_socket=%2Fprivate%2Fmysql.sock",
        "host=attacker.example",
        "port=3307",
        "database=other_database",
        "ssl_ca=%2Fprivate%2Fca.pem",
        "charset=UTF8MB4",
        "charset=utf8mb4&charset=utf8mb4",
        "unix_socket=%2Fprivate%2Fmysql.sock&unix_socket=%2Fother%2Fmysql.sock",
        "charset=utf8mb4&host=attacker.example",
    ],
)
def test_mysql_endpoint_rejects_query_overrides_without_disclosing_values(query: str) -> None:
    """Driver target overrides and all non-allowlisted query values fail closed."""
    endpoint = ApprovedEndpoint("metadata", METADATA_DATABASE)
    raw_url = f"mysql+pymysql://reader:private-password@127.0.0.1:3306/{METADATA_DATABASE}?{query}"

    with pytest.raises(RuntimeInventoryError) as error:
        _validate_endpoint(make_url(raw_url), endpoint)

    assert str(error.value) == "endpoint_query_not_approved"
    assert "private-password" not in str(error.value)
    assert "attacker.example" not in str(error.value)
    assert "mysql.sock" not in str(error.value)
    assert "other_database" not in str(error.value)


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


def test_report_scalar_projects_supported_driver_values_to_safe_json_scalars() -> None:
    """Warehouse scalar normalization keeps exact dates and hashes raw byte values."""
    binary_value = b"private source bytes"
    assert _report_scalar(None) is None
    assert _report_scalar("known") == "known"
    assert _report_scalar(17) == 17
    assert _report_scalar(date(2026, 10, 28)) == "2026-10-28"
    assert _report_scalar(datetime(2026, 10, 28, tzinfo=timezone.utc)) == (
        "2026-10-28T00:00:00+00:00"
    )
    assert _report_scalar(Decimal("12.50")) == "12.50"
    assert _report_scalar(binary_value) == {
        "binary_sha256": hashlib.sha256(binary_value).hexdigest(),
        "byte_length": len(binary_value),
    }
    assert binary_value.decode() not in json.dumps(_report_scalar(binary_value))


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

    class BrokenConnection:
        def exec_driver_sql(self, _statement: str) -> object:
            raise OSError("private SQLite path")

    with pytest.raises(RuntimeInventoryError, match="sqlite_readonly_setup_failed"):
        _configure_sqlite_readonly(BrokenConnection())  # type: ignore[arg-type]

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


def test_readonly_snapshot_blocks_sqlite_writes_and_unknown_backends() -> None:
    """Scratch snapshots reject writes and fail closed for unsupported engines."""
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE read_only_probe (value INTEGER)"))
            connection.execute(text("INSERT INTO read_only_probe VALUES (1)"))

        with (
            pytest.raises(RuntimeInventoryError, match="readonly_database_inventory_failed"),
            _readonly_snapshot(engine, expected_database=None) as (connection, *_),
        ):
            connection.execute(text("INSERT INTO read_only_probe VALUES (2)"))

        with engine.connect() as connection:
            count = connection.execute(text("SELECT count(*) FROM read_only_probe")).scalar_one()
            assert count == 1
    finally:
        engine.dispose()

    class UnsupportedEngine:
        dialect = type("Dialect", (), {"name": "oracle"})()

        def connect(self) -> object:
            return nullcontext(object())

    with (
        pytest.raises(RuntimeInventoryError, match="database_backend_not_supported"),
        _readonly_snapshot(UnsupportedEngine(), expected_database=None),  # type: ignore[arg-type]
    ):
        raise AssertionError("unsupported backend must not yield a connection")


def test_mysql_snapshot_requires_the_pinned_server_before_yielding() -> None:
    """A wrong physical MySQL instance is rejected before inventory SQL can run."""

    class Result:
        def __init__(self, row: tuple[object, ...] | None) -> None:
            self.row = row

        def scalar_one_or_none(self) -> object | None:
            return self.row[0] if self.row else None

        def one(self) -> tuple[object, ...]:
            assert self.row is not None
            return self.row

        def one_or_none(self) -> tuple[object, ...] | None:
            return self.row

    class Cursor:
        def __init__(self) -> None:
            self.row: tuple[object, ...] = ()

        def execute(self, statement: str) -> None:
            if statement == "SELECT @@session.transaction_read_only":
                self.row = (1,)
            elif statement.startswith("SELECT @@session.transaction_read_only,"):
                self.row = (1, "REPEATABLE_READ")

        def fetchone(self) -> tuple[object, ...]:
            return self.row

        def close(self) -> None:
            return None

    class RawConnection:
        def __init__(self) -> None:
            self.cursor_instance = Cursor()

        def cursor(self) -> Cursor:
            return self.cursor_instance

        def commit(self) -> None:
            return None

    class Connection:
        dialect = type("Dialect", (), {"name": "mysql"})()

        def __init__(self, server_uuid: str, database: str = METADATA_DATABASE) -> None:
            self.raw = RawConnection()
            self.connection = type("Driver", (), {"driver_connection": self.raw})()
            self.server_uuid = server_uuid
            self.database = database
            self.statements: list[str] = []

        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: object) -> Result:
            sql = str(statement)
            self.statements.append(sql)
            if sql == "SELECT DATABASE()":
                return Result((self.database,))
            if "@@session.transaction_read_only" in sql:
                return Result((1, "REPEATABLE_READ"))
            if "@@server_uuid" in sql:
                return Result(
                    (
                        self.database,
                        self.server_uuid,
                        "mysql-fixture",
                        3306,
                        "8.0.synthetic",
                        "MySQL fixture",
                    )
                )
            raise AssertionError("unexpected SQL passed the identity preflight")

    class Engine:
        dialect = type("Dialect", (), {"name": "mysql"})()

        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        def connect(self) -> Connection:
            return self.connection

    wrong = Connection("123e4567-e89b-12d3-a456-426614174001")
    with (
        pytest.raises(RuntimeInventoryError, match="mysql_server_uuid_mismatch") as error,
        _readonly_snapshot(
            Engine(wrong),
            expected_database=METADATA_DATABASE,
            expected_server_uuid=_MYSQL_UUID,
        ),
    ):
        pytest.fail("wrong physical server must not yield a snapshot")
    assert "123e4567-e89b-12d3-a456-426614174001" not in str(error.value)
    assert any("@@server_uuid" in statement for statement in wrong.statements)
    assert not any("ak_scheduled_tasks" in statement for statement in wrong.statements)

    role_pins = {
        METADATA_DATABASE: _MYSQL_UUID,
        WAREHOUSE_DATABASE: _WAREHOUSE_UUID,
    }
    for database, expected_uuid in role_pins.items():
        matched = Connection(expected_uuid, database=database)
        with _readonly_snapshot(
            Engine(matched),
            expected_database=database,
            expected_server_uuid=expected_uuid,
        ) as (_connection, observed_database, isolation, _captured_at, identity):
            assert observed_database == database
            assert isolation == "repeatable-read"
            assert identity is not None and identity["server_uuid"] == expected_uuid

        crossed_uuid = _WAREHOUSE_UUID if expected_uuid == _MYSQL_UUID else _MYSQL_UUID
        crossed = Connection(crossed_uuid, database=database)
        with (
            pytest.raises(RuntimeInventoryError, match="mysql_server_uuid_mismatch"),
            _readonly_snapshot(
                Engine(crossed),
                expected_database=database,
                expected_server_uuid=expected_uuid,
            ),
        ):
            pytest.fail("the other database role's pin must not be accepted")


def test_runtime_inventory_routes_distinct_role_pins_to_each_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metadata and warehouse use their own synthetic UUID pins in the final report."""
    from opendata import runtime_inventory

    source_root = tmp_path / "afi"
    source_root.mkdir()
    identity_file = _write_identity_file(
        tmp_path / "database-identities.json", warehouse_uuid=_WAREHOUSE_UUID
    )
    actual_uuids = {
        METADATA_DATABASE: _MYSQL_UUID,
        WAREHOUSE_DATABASE: _WAREHOUSE_UUID,
    }
    received_pins: dict[str, str | None] = {}

    class Engine:
        dialect = type("Dialect", (), {"name": "mysql"})()

        def dispose(self) -> None:
            return None

    @contextmanager
    def fake_snapshot(
        _engine: Engine,
        *,
        expected_database: str,
        expected_server_uuid: str | None = None,
    ):
        received_pins[expected_database] = expected_server_uuid
        if actual_uuids[expected_database] != expected_server_uuid:
            raise RuntimeInventoryError("mysql_server_uuid_mismatch")
        identity = {"server_uuid": actual_uuids[expected_database]}
        yield object(), expected_database, "repeatable-read", "2026-10-28T00:00:00+00:00", identity

    def metadata_inventory(
        _connection: object, *, database: str, source_root: Path
    ) -> dict[str, object]:
        del source_root
        return {
            "database": database,
            "task_map_tasks": [],
            "runtime_table_engine_check": {
                "status": "VERIFIED",
                "check": "BOTH_SCHEDULER_TABLES_ARE_BASE_INNODB",
                "source": "information_schema.TABLES",
                "database": database,
                "tables": {
                    TASK_TABLE: {"table_type": "BASE TABLE", "engine": "InnoDB"},
                    SCRIPT_TABLE: {"table_type": "BASE TABLE", "engine": "InnoDB"},
                },
            },
            "scheduled_tasks": {
                "row_count_exact": 0,
                "active_count_exact": 0,
                "inactive_count_exact": 0,
            },
            "data_scripts": {
                "row_count_exact": 0,
                "active_count_exact": 0,
                "inactive_count_exact": 0,
            },
        }

    monkeypatch.setattr(runtime_inventory, "create_engine", lambda *_args, **_kwargs: Engine())
    monkeypatch.setattr(runtime_inventory, "_readonly_snapshot", fake_snapshot)
    monkeypatch.setattr(runtime_inventory, "_metadata_inventory", metadata_inventory)
    monkeypatch.setattr(runtime_inventory, "_warehouse_schema", lambda *_args: {"table_count": 0})
    monkeypatch.setattr(runtime_inventory, "_code_hashes", lambda: {})

    def build() -> dict[str, object]:
        return build_runtime_inventory(
            metadata_url=("mysql+pymysql://reader:private@127.0.0.1:3306/ai_for_investor"),
            warehouse_url=("mysql+pymysql://reader:private@127.0.0.1:3306/akshare_data"),
            source_root=source_root,
            identity_file=identity_file,
        )

    report = build()

    assert received_pins == {
        METADATA_DATABASE: _MYSQL_UUID,
        WAREHOUSE_DATABASE: _WAREHOUSE_UUID,
    }
    endpoints = report["endpoints"]
    assert endpoints["metadata"]["server_identity"]["server_uuid"] == _MYSQL_UUID
    assert endpoints["metadata"]["server_uuid_pin_matched"] is True
    assert endpoints["warehouse"]["server_identity"]["server_uuid"] == _WAREHOUSE_UUID
    assert endpoints["warehouse"]["server_uuid_pin_matched"] is True
    assert report["runtime_table_engine_check"]["status"] == "VERIFIED"

    received_pins.clear()
    actual_uuids[WAREHOUSE_DATABASE] = _MYSQL_UUID
    with pytest.raises(RuntimeInventoryError, match="mysql_server_uuid_mismatch"):
        build()
    assert received_pins[WAREHOUSE_DATABASE] == _WAREHOUSE_UUID


def test_row_reader_and_source_path_mapping_keep_unknowns_explicit(tmp_path: Path) -> None:
    """Invalid identifiers fail, while supported module spellings map exactly."""
    source_root = tmp_path / "afi"
    source_root.mkdir()
    assert _source_path(None, source_root=source_root) == (None, "MODULE_PATH_MISSING")
    assert _source_path("  ", source_root=source_root) == (None, "MODULE_PATH_MISSING")
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
    assert _source_path(r"data_fetch\scripts\stocks\history.py", source_root=source_root) == (
        "src/backend/app/data_fetch/scripts/stocks/history.py",
        "PATH_CANDIDATE",
    )
    assert _source_path("src/backend/app/data_fetch/scripts", source_root=source_root) == (
        None,
        "MODULE_NOT_A_SCRIPT_FILE",
    )

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


def test_row_reader_enforces_its_configured_safety_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deterministic row cap fails closed instead of truncating inventory data."""
    from opendata import runtime_inventory

    engine = create_engine(f"sqlite:///{tmp_path / 'bounded.sqlite'}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE sample (id INTEGER PRIMARY KEY)"))
            connection.execute(text("INSERT INTO sample VALUES (1), (2)"))
        monkeypatch.setattr(runtime_inventory, "MAX_TASK_ROWS", 1)
        with (
            engine.connect() as connection,
            pytest.raises(RuntimeInventoryError, match="legacy_table_row_limit_exceeded"),
        ):
            _read_rows(
                connection,
                table="sample",
                resolved={"id": "id"},
                order_by="id",
            )
    finally:
        engine.dispose()


def test_sqlite_schema_inventory_preserves_index_shape_and_rejects_unsafe_names() -> None:
    """SQLite schema inspection reports keys and refuses unsafe table or index names."""
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            )
            connection.execute(text("CREATE UNIQUE INDEX ix_sample_value ON sample(value)"))

        with engine.connect() as connection:
            schema = _sqlite_warehouse_schema(connection)
        assert schema["table_count"] == 1
        table = schema["tables"][0]
        assert table["primary_key_columns"] == ["id"]
        assert table["indexes"] == [
            {"name": "ix_sample_value", "unique": True, "type": "BTREE", "columns": ["value"]}
        ]

        with engine.begin() as connection:
            connection.execute(text('CREATE INDEX "unsafe index" ON sample(value)'))
        with (
            engine.connect() as connection,
            pytest.raises(RuntimeInventoryError, match="invalid_test_schema_index_name"),
        ):
            _sqlite_warehouse_schema(connection)

        with engine.begin() as connection:
            connection.execute(text('DROP INDEX "unsafe index"'))
            connection.execute(text('CREATE TABLE "unsafe table" (id INTEGER PRIMARY KEY)'))
        with (
            engine.connect() as connection,
            pytest.raises(RuntimeInventoryError, match="invalid_test_schema_table_name"),
        ):
            _sqlite_warehouse_schema(connection)
    finally:
        engine.dispose()


def test_metadata_inventory_rejects_missing_legacy_tables(tmp_path: Path) -> None:
    """An empty scratch schema cannot be reported as a valid scheduler inventory."""
    engine = create_engine("sqlite://")
    try:
        with (
            engine.connect() as connection,
            pytest.raises(RuntimeInventoryError, match="expected_legacy_runtime_tables_missing"),
        ):
            _metadata_inventory(
                connection,
                database="sqlite-test-only",
                source_root=tmp_path,
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("bad_table", "definitions", "error_code"),
    [
        (
            TASK_TABLE,
            [{"TABLE_NAME": TASK_TABLE, "TABLE_TYPE": "BASE TABLE", "ENGINE": "MyISAM"}],
            "metadata_runtime_table_not_innodb",
        ),
        (
            SCRIPT_TABLE,
            [{"TABLE_NAME": SCRIPT_TABLE, "TABLE_TYPE": "VIEW", "ENGINE": None}],
            "metadata_runtime_table_not_base_table",
        ),
        (TASK_TABLE, [], "metadata_runtime_table_metadata_missing"),
        (
            SCRIPT_TABLE,
            [
                {"TABLE_NAME": SCRIPT_TABLE, "TABLE_TYPE": "BASE TABLE", "ENGINE": "InnoDB"},
                {"TABLE_NAME": SCRIPT_TABLE, "TABLE_TYPE": "BASE TABLE", "ENGINE": "InnoDB"},
            ],
            "metadata_runtime_table_metadata_missing",
        ),
    ],
)
def test_mysql_metadata_inventory_rejects_non_innodb_runtime_tables_before_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_table: str,
    definitions: list[dict[str, object]],
    error_code: str,
) -> None:
    """Task and script rows are never read unless both source tables are InnoDB."""
    from opendata import runtime_inventory

    columns_by_table = {
        TASK_TABLE: [
            {"name": name}
            for name in (
                "id",
                "script_id",
                "schedule_type",
                "expression",
                "is_active",
                "last_execution_at",
                "next_execution_at",
                "timeout",
                "max_retries",
                "task_kind",
                "parameters",
            )
        ],
        SCRIPT_TABLE: [
            {"name": name}
            for name in (
                "script_id",
                "module_path",
                "function_name",
                "target_table",
                "is_active",
                "source",
                "parameters",
            )
        ],
    }
    metadata = {
        TASK_TABLE: [{"TABLE_NAME": TASK_TABLE, "TABLE_TYPE": "BASE TABLE", "ENGINE": "InnoDB"}],
        SCRIPT_TABLE: [
            {"TABLE_NAME": SCRIPT_TABLE, "TABLE_TYPE": "BASE TABLE", "ENGINE": "InnoDB"}
        ],
    }
    metadata[bad_table] = definitions
    row_reads: list[str] = []

    class Result:
        def __init__(self, rows: list[dict[str, object]]) -> None:
            self.rows = rows

        def mappings(self) -> list[dict[str, object]]:
            return self.rows

    class Connection:
        dialect = type("Dialect", (), {"name": "mysql"})()

        def execute(self, _statement: object, parameters: dict[str, object]) -> Result:
            return Result(metadata[str(parameters["table"])])

    monkeypatch.setattr(
        runtime_inventory,
        "_table_columns",
        lambda _connection, _database, table: columns_by_table[table],
    )

    def record_row_read(
        _connection: object,
        *,
        table: str,
        resolved: object,
        order_by: str,
    ) -> list[dict[str, object]]:
        del resolved, order_by
        row_reads.append(table)
        return []

    monkeypatch.setattr(runtime_inventory, "_read_rows", record_row_read)
    with pytest.raises(RuntimeInventoryError, match=error_code):
        _metadata_inventory(Connection(), database=METADATA_DATABASE, source_root=tmp_path)
    assert row_reads == []


def test_mysql_metadata_inventory_reads_rows_after_both_innodb_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both engine checks precede task/script row reads in the same connection."""
    from opendata import runtime_inventory

    metadata = {
        TASK_TABLE: [{"TABLE_NAME": TASK_TABLE, "TABLE_TYPE": "BASE TABLE", "ENGINE": "InnoDB"}],
        SCRIPT_TABLE: [
            {"TABLE_NAME": SCRIPT_TABLE, "TABLE_TYPE": "BASE TABLE", "ENGINE": "InnoDB"}
        ],
    }
    checks: list[str] = []
    row_reads: list[str] = []
    columns_by_table = {
        TASK_TABLE: [
            {"name": name}
            for name in (
                "id",
                "script_id",
                "schedule_type",
                "expression",
                "is_active",
                "last_execution_at",
                "next_execution_at",
                "timeout",
                "max_retries",
                "task_kind",
                "parameters",
            )
        ],
        SCRIPT_TABLE: [
            {"name": name}
            for name in (
                "script_id",
                "module_path",
                "function_name",
                "target_table",
                "is_active",
                "source",
                "parameters",
            )
        ],
    }

    class Result:
        def __init__(self, rows: list[dict[str, object]]) -> None:
            self.rows = rows

        def mappings(self) -> list[dict[str, object]]:
            return self.rows

    class Connection:
        dialect = type("Dialect", (), {"name": "mysql"})()

        def execute(self, _statement: object, parameters: dict[str, object]) -> Result:
            table = str(parameters["table"])
            checks.append(table)
            return Result(metadata[table])

    monkeypatch.setattr(
        runtime_inventory,
        "_table_columns",
        lambda _connection, _database, table: columns_by_table[table],
    )

    def record_row_read(
        _connection: object,
        *,
        table: str,
        resolved: object,
        order_by: str,
    ) -> list[dict[str, object]]:
        del resolved, order_by
        row_reads.append(table)
        return []

    monkeypatch.setattr(runtime_inventory, "_read_rows", record_row_read)
    report = _metadata_inventory(Connection(), database=METADATA_DATABASE, source_root=tmp_path)

    assert checks == [TASK_TABLE, SCRIPT_TABLE]
    assert row_reads == [TASK_TABLE, SCRIPT_TABLE]
    assert report["scheduled_tasks"]["row_count_exact"] == 0  # type: ignore[index]
    assert report["runtime_table_engine_check"] == {
        "status": "VERIFIED",
        "check": "BOTH_SCHEDULER_TABLES_ARE_BASE_INNODB",
        "source": "information_schema.TABLES",
        "database": METADATA_DATABASE,
        "tables": {
            TASK_TABLE: {"table_type": "BASE TABLE", "engine": "InnoDB"},
            SCRIPT_TABLE: {"table_type": "BASE TABLE", "engine": "InnoDB"},
        },
    }


def test_source_path_rejects_symlinks_that_escape_the_afi_root(tmp_path: Path) -> None:
    """A known namespace cannot resolve through a symlink outside the source tree."""
    source_root = tmp_path / "afi"
    script_dir = source_root / "src/backend/app/data_fetch/scripts/stocks"
    script_dir.mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("def fetch_data():\n    return []\n", encoding="utf-8")
    (script_dir / "escaped.py").symlink_to(outside)

    assert _source_path(
        "src/backend/app/data_fetch/scripts/stocks/escaped.py", source_root=source_root
    ) == (None, "MODULE_PATH_OUTSIDE_SOURCE_ROOT")


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
    metadata_only_env = tmp_path / ".metadata-only.env"
    metadata_only_env.write_text(
        "DATABASE_URL=mysql+pymysql://reader:private@127.0.0.1:3306/ai_for_investor\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeInventoryError, match="dotenv_missing_AKSHARE_DATA_DATABASE_URL"):
        _safe_env_urls(metadata_only_env)

    source_root = tmp_path / "afi"
    source_root.mkdir()
    identity_file = _write_identity_file(tmp_path / "database-identities.json")

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
            "--identity-file",
            str(identity_file),
            "--output",
            str(tmp_path / "failed.json"),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "unexpected internal error" in captured.err
    assert "do-not-print" not in captured.err


def test_cli_env_file_path_writes_a_redacted_report_without_opening_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI can persist a safe summary after its inventory result is supplied."""
    from opendata import runtime_inventory

    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=mysql+pymysql://reader:private@127.0.0.1:3306/ai_for_investor\n"
        "AKSHARE_DATA_DATABASE_URL=mysql+pymysql://reader:private@127.0.0.1:3306/akshare_data\n",
        encoding="utf-8",
    )
    source_root = tmp_path / "afi"
    source_root.mkdir()
    output = tmp_path / "reports" / "runtime.json"
    identity_file = _write_identity_file(tmp_path / "database-identities.json")
    report = {"counts": {"scheduled_tasks": 2, "warehouse_tables": 1}}
    observed: dict[str, object] = {}

    def supplied_inventory(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return report

    monkeypatch.setattr(runtime_inventory, "build_runtime_inventory", supplied_inventory)
    exit_code = main(
        [
            "--env-file",
            str(env_file),
            "--source-root",
            str(source_root),
            "--identity-file",
            str(identity_file),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert observed == {
        "source_root": source_root,
        "env_file": env_file,
        "identity_file": identity_file,
    }
    assert json.loads(output.read_text(encoding="utf-8")) == report
    captured = capsys.readouterr()
    assert captured.out == json.dumps(report["counts"], ensure_ascii=False, sort_keys=True) + "\n"
    assert captured.err == ""
    assert "private" not in output.read_text(encoding="utf-8") + captured.out + captured.err


def test_cli_requires_warehouse_url_for_explicit_metadata_url(tmp_path: Path) -> None:
    """Argument validation rejects incomplete connection configuration preflight."""
    with pytest.raises(SystemExit) as error:
        _parse_args(
            [
                "--metadata-url",
                "mysql+pymysql://reader:private@127.0.0.1:3306/ai_for_investor",
                "--source-root",
                str(tmp_path),
                "--output",
                str(tmp_path / "report.json"),
            ]
        )
    assert error.value.code == 2


def test_cli_requires_an_identity_pin_file(tmp_path: Path) -> None:
    """The production CLI cannot start without explicit reviewed UUID pins."""
    with pytest.raises(SystemExit) as error:
        _parse_args(
            [
                "--metadata-url",
                "mysql+pymysql://reader:private@127.0.0.1:3306/ai_for_investor",
                "--warehouse-url",
                "mysql+pymysql://reader:private@127.0.0.1:3306/akshare_data",
                "--source-root",
                str(tmp_path),
                "--output",
                str(tmp_path / "report.json"),
            ]
        )
    assert error.value.code == 2


def test_cli_reports_expected_inventory_errors_without_raw_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Expected endpoint or schema errors retain their safe code at the CLI boundary."""
    from opendata import runtime_inventory

    source_root = tmp_path / "afi"
    source_root.mkdir()
    identity_file = _write_identity_file(tmp_path / "database-identities.json")

    def fail_closed(**_kwargs: object) -> dict[str, object]:
        raise RuntimeInventoryError("endpoint_query_not_approved")

    monkeypatch.setattr(runtime_inventory, "build_runtime_inventory", fail_closed)
    exit_code = main(
        [
            "--metadata-url",
            "mysql+pymysql://reader:private@127.0.0.1:3306/ai_for_investor",
            "--warehouse-url",
            "mysql+pymysql://reader:private@127.0.0.1:3306/akshare_data",
            "--source-root",
            str(source_root),
            "--identity-file",
            str(identity_file),
            "--output",
            str(tmp_path / "rejected.json"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == "runtime inventory failed: endpoint_query_not_approved\n"
    assert "private" not in captured.err
    assert captured.out == ""


def test_file_hash_failure_reports_only_the_logical_label(tmp_path: Path) -> None:
    """A missing evidence file does not expose its absolute path in diagnostics."""
    missing = tmp_path / "private" / "missing-source.py"
    with pytest.raises(RuntimeInventoryError) as error:
        _file_hash(missing, "runtime_inventory_module")
    assert str(error.value) == "code_hash_unavailable:runtime_inventory_module"
    assert str(missing) not in str(error.value)
