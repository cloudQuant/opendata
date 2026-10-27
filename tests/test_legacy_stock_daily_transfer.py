from __future__ import annotations

import json
import stat
from contextlib import contextmanager
from datetime import date, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql

from scripts.ops import migrate_legacy_stock_daily as transfer

if TYPE_CHECKING:
    from collections.abc import Iterator


def _source_row() -> tuple[object, ...]:
    return (
        "2024-01-02",
        "600000",
        10.0,
        11.0,
        12.0,
        9.0,
        2,
        123.5,
        30.0,
        10.0,
        1.0,
        "0.25",
    )


def _target_shape() -> transfer.TargetShape:
    columns = tuple(
        sorted(
            (
                transfer.TargetColumn(
                    name=name,
                    column_type=column_type,
                    nullable=nullable,
                    default=None,
                    extra="",
                )
                for name, (column_type, nullable) in transfer.TARGET_COLUMN_TYPES.items()
            ),
            key=lambda item: item.name,
        )
    )
    return transfer.TargetShape(
        columns=columns,
        primary_key=transfer.TARGET_KEY,
        partitions=transfer.EXPECTED_PARTITIONS,
        engine="innodb",
    )


def test_source_conversion_preserves_raw_units_and_all_twelve_fields() -> None:
    converted = transfer._normalize_source_row(_source_row())

    assert len(converted) == 12
    assert converted[:2] == (date(2024, 1, 2), "600000")
    assert converted[6] == 2.0
    assert converted[-1] == 0.25
    assert all(type(value) is float for value in converted[2:])


@pytest.mark.parametrize(
    "change, classification",
    [
        ({0: "2024-1-2"}, "invalid_source_date"),
        ({0: "2024-01-02 00:00:00"}, "invalid_source_date"),
        ({1: "600000.SH"}, "invalid_stock_code"),
        ({2: None}, "invalid_numeric_field_开盘"),
        ({6: 2**53 + 1}, "volume_not_lossless_double"),
        ({6: -1}, "volume_not_lossless_double"),
        ({7: True}, "invalid_numeric_field_成交额"),
        ({7: float("nan")}, "nonfinite_numeric_field_成交额"),
        ({7: float("inf")}, "nonfinite_numeric_field_成交额"),
        ({11: "NaN"}, "invalid_numeric_text_换手率"),
        ({11: " 0.25"}, "invalid_numeric_text_换手率"),
        ({11: ""}, "invalid_numeric_text_换手率"),
    ],
)
def test_invalid_source_values_fail_closed(change: dict[int, object], classification: str) -> None:
    row = list(_source_row())
    for index, value in change.items():
        row[index] = value

    with pytest.raises(transfer.TransferError) as exc_info:
        transfer._normalize_source_row(row)

    assert exc_info.value.classification == classification


def test_target_date_does_not_accept_datetime_or_text() -> None:
    with pytest.raises(transfer.TransferError, match="invalid_target_date_type"):
        transfer._target_date(datetime(2024, 1, 2))
    with pytest.raises(transfer.TransferError, match="invalid_target_date_type"):
        transfer._target_date("2024-01-02")


def test_source_and_target_full_row_hashes_match_after_database_type_conversion() -> None:
    source = transfer._normalize_source_row(_source_row())
    target = transfer._normalize_target_row(source)

    source_count, source_hash = transfer._digest_rows([source])
    target_count, target_hash = transfer._digest_rows([target])

    assert source_count == target_count == 1
    assert source_hash == target_hash


def test_turnover_amount_null_is_preserved_and_hashes_differ_from_real_zero() -> None:
    missing = list(_source_row())
    missing[7] = None
    zero = list(_source_row())
    zero[7] = 0.0

    normalized_missing = transfer._normalize_source_row(missing)
    normalized_zero = transfer._normalize_source_row(zero)
    target_missing = transfer._normalize_target_row(normalized_missing)
    missing_count, missing_hash = transfer._digest_rows([normalized_missing])
    zero_count, zero_hash = transfer._digest_rows([normalized_zero])

    assert normalized_missing[7] is None
    assert target_missing[7] is None
    assert normalized_zero[7] == 0.0
    assert missing_count == zero_count == 1
    assert missing_hash != zero_hash


@pytest.mark.parametrize("index", [2, 3, 4, 5, 6])
def test_required_price_and_volume_fields_still_reject_null(index: int) -> None:
    row = list(_source_row())
    row[index] = None

    with pytest.raises(transfer.TransferError):
        transfer._normalize_source_row(row)


def test_digest_uses_type_and_length_framing_for_every_field() -> None:
    row = transfer._normalize_source_row(_source_row())
    changed = list(row)
    changed[1] = "060000"

    _, original_hash = transfer._digest_rows([row])
    _, changed_hash = transfer._digest_rows([changed])

    assert original_hash != changed_hash
    assert len(original_hash) == 64


def test_nullable_auxiliary_metrics_remain_null() -> None:
    row = list(_source_row())
    row[8:12] = [None, None, None, None]

    converted = transfer._normalize_source_row(row)

    assert converted[8:] == (None, None, None, None)


def test_bigint_volume_at_exact_double_boundary_is_accepted() -> None:
    row = list(_source_row())
    row[6] = 2**53

    assert transfer._normalize_source_row(row)[6] == float(2**53)


def test_database_url_guard_accepts_only_the_reviewed_loopback_pair() -> None:
    source = "mysql+pymysql://reader:secret@127.0.0.1:3306/akshare_data"
    target = "mysql+pymysql://writer:secret@localhost:33565/opendata_c65_legacy_e25963"

    _, _, source_endpoint, target_endpoint = transfer._validate_database_urls(source, target)

    assert source_endpoint.schema == "akshare_data"
    assert target_endpoint.schema == "opendata_c65_legacy_e25963"


@pytest.mark.parametrize(
    "source, target, classification",
    [
        (
            "mysql+pymysql://reader:secret@example.com:3306/akshare_data",
            "mysql+pymysql://writer:secret@localhost:33565/opendata_c65_legacy_x",
            "source_host_not_allowed",
        ),
        (
            "mysql+pymysql://reader:secret@localhost:3307/akshare_data",
            "mysql+pymysql://writer:secret@localhost:33565/opendata_c65_legacy_x",
            "source_port_not_allowed",
        ),
        (
            "mysql+pymysql://reader:secret@localhost:3306/other",
            "mysql+pymysql://writer:secret@localhost:33565/opendata_c65_legacy_x",
            "source_schema_not_allowed",
        ),
        (
            "mysql+pymysql://reader:secret@localhost:3306/akshare_data",
            "mysql+pymysql://writer:secret@localhost:3306/opendata_c65_legacy_x",
            "target_port_not_allowed",
        ),
        (
            "mysql+pymysql://reader:secret@localhost:3306/akshare_data",
            "mysql+pymysql://writer:secret@localhost:33565/opendata_prod",
            "target_schema_not_isolated",
        ),
    ],
)
def test_database_url_guard_rejects_unreviewed_endpoints_without_leaking_secrets(
    source: str, target: str, classification: str
) -> None:
    with pytest.raises(transfer.TransferError) as exc_info:
        transfer._validate_database_urls(source, target)

    assert exc_info.value.classification == classification
    assert "secret" not in str(exc_info.value)
    assert "example.com" not in str(exc_info.value)


def test_database_url_rejects_driver_options_that_could_run_initial_sql() -> None:
    source = (
        "mysql+pymysql://reader:secret@localhost:3306/akshare_data"
        "?init_command=UPDATE%20STOCK_ZH_A_HIST"
    )
    target = "mysql+pymysql://writer:secret@localhost:33565/opendata_c65_legacy_x"

    with pytest.raises(transfer.TransferError) as exc_info:
        transfer._validate_database_urls(source, target)

    assert exc_info.value.classification == "source_url_options_not_allowed"
    assert "secret" not in str(exc_info.value)


def test_source_sql_guard_allows_select_and_blocks_writes() -> None:
    transfer._is_source_read_statement(None, None, "SELECT 1", None, None, False)

    with pytest.raises(transfer.TransferError, match="source_sql_guard_blocked_statement"):
        transfer._is_source_read_statement(None, None, "UPDATE t SET x=1", None, None, False)


def test_target_shape_requires_the_existing_reviewed_table_contract() -> None:
    transfer._require_target_shape(_target_shape())

    invalid = _target_shape()
    with pytest.raises(transfer.TransferError, match="target_primary_key_mismatch"):
        transfer._require_target_shape(
            transfer.TargetShape(
                columns=invalid.columns,
                primary_key=("日期", "股票代码"),
                partitions=invalid.partitions,
                engine=invalid.engine,
            )
        )

    nonnullable_amount = transfer.TargetShape(
        columns=tuple(
            transfer.TargetColumn(
                name=column.name,
                column_type=column.column_type,
                nullable=False if column.name == "成交额" else column.nullable,
                default=column.default,
                extra=column.extra,
            )
            for column in invalid.columns
        ),
        primary_key=invalid.primary_key,
        partitions=invalid.partitions,
        engine=invalid.engine,
    )
    with pytest.raises(transfer.TransferError, match="target_column_schema_mismatch"):
        transfer._require_target_shape(nonnullable_amount)


def test_exact_turnover_null_count_only_accepts_fixed_transfer_tables() -> None:
    class ScalarResult:
        def scalar_one(self) -> int:
            return 3

    class FakeConnection:
        statement: object | None = None

        def execute(self, statement: object) -> ScalarResult:
            self.statement = statement
            return ScalarResult()

    connection = FakeConnection()

    assert transfer._exact_turnover_amount_null_count(connection, transfer.SOURCE_TABLE) == 3
    assert "STOCK_ZH_A_HIST" in str(connection.statement)
    assert "`成交额` IS NULL" in str(connection.statement)
    with pytest.raises(transfer.TransferError, match="null_count_table_not_allowed"):
        transfer._exact_turnover_amount_null_count(connection, "user_supplied_table")


def test_source_and_target_streams_report_missing_amounts_and_keep_null_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [list(_source_row()), list(_source_row())]
    rows[0][0] = "2024-01-02"
    rows[0][7] = None
    rows[1][0] = "2024-01-03"
    rows[1][7] = 0.0

    class FakeResult:
        def __init__(self, values: list[tuple[object, ...]]) -> None:
            self.values = values
            self.closed = False

        def fetchmany(self, _size: int) -> list[tuple[object, ...]]:
            values, self.values = self.values, []
            return values

        def close(self) -> None:
            self.closed = True

    class FakeConnection:
        def __init__(self, values: list[tuple[object, ...]]) -> None:
            self.result = FakeResult(values)

        def execution_options(self, **_options: object) -> FakeConnection:
            return self

        def execute(self, _statement: object) -> FakeResult:
            return self.result

    class FakeTargetConnection:
        def __enter__(self) -> FakeTargetConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeTargetEngine:
        def connect(self) -> FakeTargetConnection:
            return FakeTargetConnection()

    captured: list[dict[str, object]] = []

    def capture_batch(
        _connection: object,
        _table: object,
        batch: list[dict[str, object]],
    ) -> int:
        captured.extend(batch)
        return len(batch)

    monkeypatch.setattr(transfer, "_write_batch", capture_batch)
    source_connection = FakeConnection([tuple(row) for row in rows])
    fetched_at = datetime(2024, 1, 5)
    source_rows, inserted_rows, source_hash, source_missing = transfer._stream_source(
        source_connection,
        FakeTargetEngine(),
        transfer._target_table(),
        fetched_at=fetched_at,
        batch_id="batch-null-v-zero",
        source_count=2,
        progress=lambda _read, _inserted: None,
    )

    normalized_target = [transfer._normalize_source_row(row) for row in rows]
    target_connection = FakeConnection(
        [(*row, transfer.SOURCE, fetched_at, "batch-null-v-zero") for row in normalized_target]
    )
    target_rows, target_hash, target_missing = transfer._stream_target_hash(
        target_connection,
        fetched_at=fetched_at,
        batch_id="batch-null-v-zero",
        target_count=2,
    )

    assert source_rows == inserted_rows == target_rows == 2
    assert source_missing == target_missing == 1
    assert [row["成交额"] for row in captured] == [None, 0.0]
    assert source_hash == target_hash
    assert source_connection.result.closed is True
    assert target_connection.result.closed is True


def _load_turnover_migration():
    """Load the forward migration by its repository path for isolated SQLite checks."""
    repository = Path(__file__).resolve().parents[1]
    migration_path = (
        repository / "alembic_data" / "versions" / "20261028-0016_legacy_turnover_nullable.py"
    )
    spec = spec_from_file_location("test_0016_legacy_turnover_nullable", migration_path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mysql_partition_preflight_checks_method_key_and_each_boundary() -> None:
    migration = _load_turnover_migration()
    rows = [
        ("p2024", "RANGE COLUMNS", "`日期`", "'2025-01-01'"),
        ("p2025", "RANGE COLUMNS", "`日期`", "'2026-01-01'"),
        ("p2026", "RANGE COLUMNS", "`日期`", "'2027-01-01'"),
        ("pmax", "RANGE COLUMNS", "`日期`", "MAXVALUE"),
    ]

    migration._require_mysql_partition_layout(rows)
    invalid_layouts = [
        (0, ("p2024", "RANGE", "`日期`", "'2025-01-01'")),
        (1, ("p2025", "RANGE COLUMNS", "`其他列`", "'2026-01-01'")),
        (0, ("p2024", "RANGE COLUMNS", "`日期`", "'2025-02-01'")),
        (1, ("p2025", "RANGE COLUMNS", "`日期`", "'2026-02-01'")),
        (2, ("p2026", "RANGE COLUMNS", "`日期`", "'2027-02-01'")),
        (3, ("pmax", "RANGE COLUMNS", "`日期`", "'9999-12-31'")),
    ]
    for index, replacement in invalid_layouts:
        changed_rows = list(rows)
        changed_rows[index] = replacement
        with pytest.raises(RuntimeError, match="reviewed ODS partition layout"):
            migration._require_mysql_partition_layout(changed_rows)


def test_sqlite_index_preflight_rejects_unpreserved_index_semantics() -> None:
    migration = _load_turnover_migration()
    ordinary_index = {
        "name": "ix_ods_daily_trade_date",
        "column_names": ["日期"],
        "unique": False,
        "dialect_options": {},
    }
    migration._require_simple_sqlite_index(
        ordinary_index,
        "CREATE INDEX ix_ods_daily_trade_date ON ods_stock_daily_akshare (`日期`)",
    )
    migration._require_simple_sqlite_index(
        {**ordinary_index, "unique": 0},
        "CREATE INDEX ix_ods_daily_trade_date ON ods_stock_daily_akshare (`日期`)",
    )

    for definition in (
        "CREATE INDEX ix_desc ON ods_stock_daily_akshare (`日期` DESC)",
        "CREATE INDEX ix_collation ON ods_stock_daily_akshare (`日期` COLLATE NOCASE)",
        "CREATE INDEX ix_partial ON ods_stock_daily_akshare (`日期`) WHERE `日期` > '2024-01-01'",
    ):
        with pytest.raises(RuntimeError, match="unsupported SQLite index definitions"):
            migration._require_simple_sqlite_index(ordinary_index, definition)

    expression_index = {**ordinary_index, "column_names": [None]}
    with pytest.raises(RuntimeError, match="unsupported SQLite index definitions"):
        migration._require_simple_sqlite_index(
            expression_index,
            "CREATE INDEX ix_expression ON ods_stock_daily_akshare (lower(`股票代码`))",
        )

    for unique in (True, 1, None):
        with pytest.raises(RuntimeError, match="unsupported SQLite index definitions"):
            migration._require_simple_sqlite_index(
                {**ordinary_index, "unique": unique},
                "CREATE INDEX ix_ods_daily_trade_date ON ods_stock_daily_akshare (`日期`)",
            )


def test_turnover_migration_preserves_values_and_refuses_lossy_downgrade() -> None:
    migration = _load_turnover_migration()
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            connection.execute(
                text(
                    "CREATE TABLE ods_stock_daily_akshare ("
                    "`日期` DATE NOT NULL, `股票代码` VARCHAR(64) NOT NULL, "
                    "`开盘` DOUBLE NOT NULL, `收盘` DOUBLE NOT NULL, "
                    "`最高` DOUBLE NOT NULL, `最低` DOUBLE NOT NULL, "
                    "`成交量` DOUBLE NOT NULL, `成交额` DOUBLE NOT NULL, "
                    "`振幅` DOUBLE NULL, `涨跌幅` DOUBLE NULL, `涨跌额` DOUBLE NULL, "
                    "`换手率` DOUBLE NULL, `_source` VARCHAR(32) NOT NULL, "
                    "`_fetched_at` DATETIME NOT NULL, `_batch_id` CHAR(36) NOT NULL, "
                    "PRIMARY KEY (`股票代码`, `日期`))"
                )
            )
            connection.execute(
                text("CREATE INDEX ix_ods_daily_trade_date ON ods_stock_daily_akshare (`日期`)")
            )
            connection.execute(
                text(
                    "INSERT INTO ods_stock_daily_akshare ("
                    "`日期`, `股票代码`, `开盘`, `收盘`, "
                    "`最高`, `最低`, `成交量`, "
                    "`成交额`, `振幅`, `涨跌幅`, `涨跌额`, `换手率`, `_source`, "
                    "`_fetched_at`, `_batch_id`"
                    ") VALUES "
                    "('2024-01-02', '600000', 10, 11, 12, 9, 2, 0.0, NULL, NULL, "
                    "NULL, NULL, 'akshare', '2024-01-02 00:00:00', 'batch-zero'), "
                    "('2024-01-03', '600000', 10, 11, 12, 9, 2, 123.5, NULL, NULL, "
                    "NULL, NULL, 'akshare', '2024-01-03 00:00:00', 'batch-nonzero')"
                )
            )
            connection.commit()
            rows_before = connection.execute(
                text("SELECT * FROM ods_stock_daily_akshare ORDER BY `日期`")
            ).all()

            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

            rows_after = connection.execute(
                text("SELECT * FROM ods_stock_daily_akshare ORDER BY `日期`")
            ).all()
            amount_column = next(
                column
                for column in inspect(connection).get_columns("ods_stock_daily_akshare")
                if column["name"] == "成交额"
            )
            assert amount_column["nullable"] is True
            assert rows_after == rows_before
            assert inspect(connection).get_pk_constraint("ods_stock_daily_akshare")[
                "constrained_columns"
            ] == ["股票代码", "日期"]
            indexes = inspect(connection).get_indexes("ods_stock_daily_akshare")
            assert len(indexes) == 1
            assert indexes[0]["name"] == "ix_ods_daily_trade_date"
            assert indexes[0]["column_names"] == ["日期"]

            connection.execute(
                text(
                    "INSERT INTO ods_stock_daily_akshare ("
                    "`日期`, `股票代码`, `开盘`, `收盘`, "
                    "`最高`, `最低`, `成交量`, "
                    "`成交额`, "
                    "`_source`, `_fetched_at`, `_batch_id`) VALUES "
                    "('2024-01-04', '600000', 10, 11, 12, 9, 2, NULL, "
                    "'akshare', '2024-01-04 00:00:00', 'batch-null')"
                )
            )
            connection.commit()
            with (
                Operations.context(MigrationContext.configure(connection)),
                pytest.raises(RuntimeError, match="downgrade is refused"),
            ):
                migration.downgrade()

            rows_after_refused_downgrade = (
                connection.execute(
                    text("SELECT `成交额` FROM ods_stock_daily_akshare ORDER BY `日期`")
                )
                .scalars()
                .all()
            )
            assert rows_after_refused_downgrade == [0.0, 123.5, None]
            assert inspect(connection).get_pk_constraint("ods_stock_daily_akshare")[
                "constrained_columns"
            ] == ["股票代码", "日期"]
    finally:
        engine.dispose()


def test_core_insert_is_mysql_insert_ignore() -> None:
    statement = transfer.insert(transfer._target_table()).prefix_with("IGNORE", dialect="mysql")

    assert str(statement.compile(dialect=mysql.dialect())).startswith(
        "INSERT IGNORE INTO ods_stock_daily_akshare"
    )


def test_dry_run_never_creates_engines_or_reads_database_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(transfer.SOURCE_URL_ENV, "not-a-url")
    monkeypatch.setenv(transfer.TARGET_URL_ENV, "also-not-a-url")
    monkeypatch.setattr(
        transfer,
        "_new_engine",
        lambda _url: pytest.fail("dry-run attempted to create a database engine"),
    )

    report = transfer.run_transfer(apply=False)

    assert report["status"] == "DRY_RUN"
    assert report["connection_attempted"] is False
    serialized = json.dumps(report)
    assert "not-a-url" not in serialized
    assert "also-not-a-url" not in serialized


def test_apply_without_environment_urls_fails_before_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(transfer.SOURCE_URL_ENV, raising=False)
    monkeypatch.delenv(transfer.TARGET_URL_ENV, raising=False)
    monkeypatch.setattr(
        transfer,
        "_new_engine",
        lambda _url: pytest.fail("apply connected without required environment URLs"),
    )

    report = transfer.run_transfer(apply=True)

    assert report["status"] == "FAIL"
    assert report["error_classification"] == "required_database_environment_missing"
    assert report["connection_attempted"] is False


def test_rejected_cli_url_argument_is_not_echoed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_url = "mysql+pymysql://reader:super-secret@localhost:3306/akshare_data"

    result = transfer.main(
        [
            "--output",
            str(tmp_path / "never-created.json"),
            "--source-url",
            secret_url,
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "super-secret" not in captured.out + captured.err
    assert "never-created.json" not in captured.out + captured.err


def test_report_is_new_file_mode_0600_and_never_overwrites(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    transfer._write_report(report_path, {"status": "DRY_RUN"})

    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert json.loads(report_path.read_text(encoding="utf-8")) == {"status": "DRY_RUN"}
    with pytest.raises(FileExistsError):
        transfer._write_report(report_path, {"status": "FAIL"})


class _MigrationResult:
    def __init__(
        self,
        *,
        rows: list[tuple[object, ...]] | None = None,
        scalar: object | None = None,
        scalar_rows: list[object] | None = None,
    ) -> None:
        self._rows = rows or []
        self._scalar = scalar
        self._scalar_rows = scalar_rows or []

    def all(self) -> list[tuple[object, ...]]:
        return self._rows or self._scalar_rows

    def scalars(self) -> _MigrationResult:
        return self

    def scalar_one_or_none(self) -> object | None:
        return self._scalar


class _MigrationBind:
    def __init__(
        self,
        *,
        triggers: list[object] | None = None,
        table_sql: str | None = None,
        indexes: list[tuple[object, object]] | None = None,
        partitions: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.triggers = triggers or []
        self.table_sql = table_sql or (
            "CREATE TABLE ods_stock_daily_akshare (`日期` DATE NOT NULL)"
        )
        self.indexes = indexes or []
        self.partitions = partitions or [
            ("p2024", "RANGE COLUMNS", "`日期`", "'2025-01-01'"),
            ("p2025", "RANGE COLUMNS", "`日期`", "'2026-01-01'"),
            ("p2026", "RANGE COLUMNS", "`日期`", "'2027-01-01'"),
            ("pmax", "RANGE COLUMNS", "`日期`", "MAXVALUE"),
        ]

    def execute(self, statement: object, _parameters: object = None) -> _MigrationResult:
        sql = str(statement)
        if "PARTITION_NAME, PARTITION_METHOD" in sql:
            return _MigrationResult(rows=self.partitions)
        if "type = 'trigger'" in sql:
            result = _MigrationResult(scalar_rows=[])
            result._scalar_rows = self.triggers
            return result
        if "SELECT sql FROM sqlite_master" in sql:
            return _MigrationResult(scalar=self.table_sql)
        if "SELECT name, sql FROM sqlite_master" in sql:
            return _MigrationResult(rows=self.indexes)
        raise AssertionError("unexpected migration preflight statement")


class _MigrationInspector:
    def __init__(self, migration: object) -> None:
        self.exists = True
        self.columns = [
            {
                "name": name,
                "type": column_type,
                "nullable": nullable,
                "default": None,
                "computed": None,
                "identity": None,
            }
            for name, (column_type, nullable) in migration._COLUMNS.items()
        ]
        self.primary_key = ["股票代码", "日期"]
        self.foreign_keys: list[object] = []
        self.unique_constraints: list[object] = []
        self.check_constraints: list[object] = []
        self.indexes: list[dict[str, object]] = []
        self.options: dict[str, object] = {"mysql_engine": "InnoDB"}

    def has_table(self, _table: str) -> bool:
        return self.exists

    def get_columns(self, _table: str) -> list[dict[str, object]]:
        return self.columns

    def get_pk_constraint(self, _table: str) -> dict[str, object]:
        return {"constrained_columns": self.primary_key}

    def get_foreign_keys(self, _table: str) -> list[object]:
        return self.foreign_keys

    def get_unique_constraints(self, _table: str) -> list[object]:
        return self.unique_constraints

    def get_check_constraints(self, _table: str) -> list[object]:
        return self.check_constraints

    def get_table_options(self, _table: str) -> dict[str, object]:
        return self.options

    def get_indexes(self, _table: str) -> list[dict[str, object]]:
        return self.indexes


def _install_fake_migration_preflight(
    monkeypatch: pytest.MonkeyPatch,
    migration: object,
    *,
    inspector: _MigrationInspector | None = None,
    bind: _MigrationBind | None = None,
) -> tuple[_MigrationInspector, _MigrationBind]:
    fake_inspector = inspector or _MigrationInspector(migration)
    fake_bind = bind or _MigrationBind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: fake_bind)
    monkeypatch.setattr(migration, "inspect", lambda _bind: fake_inspector)
    return fake_inspector, fake_bind


def test_revision_0016_supported_target_version_guard_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_turnover_migration()
    accepted = [
        (False, "sqlite", False, None, "sqlite"),
        (False, "mysql", False, (8, 0, 16), "mysql"),
        (False, "mysql", False, (8, 4, 0), "mysql"),
    ]
    for offline, name, mariadb, version, expected in accepted:
        context = SimpleNamespace(
            as_sql=offline,
            dialect=SimpleNamespace(
                name=name,
                is_mariadb=mariadb,
                server_version_info=version,
            ),
        )
        monkeypatch.setattr(migration.op, "get_context", lambda context=context: context)
        assert migration._require_supported_target() == expected

    rejected = [
        (True, "mysql", False, (8, 4, 0), "online connection"),
        (False, "mysql", True, (10, 6, 0), "not MariaDB"),
        (False, "postgresql", False, None, "SQLite and MySQL"),
        (False, "mysql", False, None, "MySQL 8.0.16"),
        (False, "mysql", False, (8, 0), "MySQL 8.0.16"),
        (False, "mysql", False, (8, 0, 15), "MySQL 8.0.16"),
        (False, "mysql", False, (9, 0, 0), "MySQL 8.0.16"),
        (False, "mysql", False, (8, 0, True), "MySQL 8.0.16"),
    ]
    for offline, name, mariadb, version, message in rejected:
        context = SimpleNamespace(
            as_sql=offline,
            dialect=SimpleNamespace(
                name=name,
                is_mariadb=mariadb,
                server_version_info=version,
            ),
        )
        monkeypatch.setattr(migration.op, "get_context", lambda context=context: context)
        with pytest.raises(RuntimeError, match=message):
            migration._require_supported_target()


@pytest.mark.parametrize(
    "mutation, mode, message",
    [
        ("missing", "sqlite", "ods_stock_daily_akshare to exist"),
        ("column", "sqlite", "column shape"),
        ("default", "sqlite", "server defaults"),
        ("computed", "sqlite", "computed or identity"),
        ("identity", "sqlite", "computed or identity"),
        ("primary_key", "sqlite", "primary key"),
        ("foreign_key", "sqlite", "foreign keys"),
        ("unique", "sqlite", "unique constraints"),
        ("check", "sqlite", "check constraints"),
        ("mysql_engine", "mysql", "InnoDB"),
        ("mysql_partition", "mysql", "partition layout"),
        ("sqlite_trigger", "sqlite", "triggers"),
        ("sqlite_without_rowid", "sqlite", "table options"),
        ("sqlite_strict", "sqlite", "table options"),
        ("sqlite_collate", "sqlite", "table options"),
        ("sqlite_conflict", "sqlite", "table options"),
        ("sqlite_index", "sqlite", "index definitions"),
    ],
)
def test_revision_0016_legacy_shape_guard_rejects_unsupported_layouts(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    mode: str,
    message: str,
) -> None:
    migration = _load_turnover_migration()
    inspector = _MigrationInspector(migration)
    bind = _MigrationBind()
    if mutation == "missing":
        inspector.exists = False
    elif mutation == "column":
        inspector.columns.pop()
    elif mutation == "default":
        inspector.columns[0]["default"] = "0"
    elif mutation == "computed":
        inspector.columns[0]["computed"] = {"sqltext": "1"}
    elif mutation == "identity":
        inspector.columns[0]["identity"] = {"always": True}
    elif mutation == "primary_key":
        inspector.primary_key = ["日期", "股票代码"]
    elif mutation == "foreign_key":
        inspector.foreign_keys = [{"name": "unexpected_fk"}]
    elif mutation == "unique":
        inspector.unique_constraints = [{"name": "unexpected_unique"}]
    elif mutation == "check":
        inspector.check_constraints = [{"name": "unexpected_check"}]
    elif mutation == "mysql_engine":
        inspector.options = {"mysql_engine": "MyISAM"}
    elif mutation == "mysql_partition":
        bind.partitions[2] = ("p2026", "RANGE COLUMNS", "`日期`", "'2027-02-01'")
    elif mutation == "sqlite_trigger":
        bind.triggers = ["unexpected_trigger"]
    elif mutation == "sqlite_without_rowid":
        bind.table_sql = "CREATE TABLE ods_stock_daily_akshare (x INTEGER) WITHOUT ROWID"
    elif mutation == "sqlite_strict":
        bind.table_sql = "CREATE TABLE ods_stock_daily_akshare (x INTEGER) STRICT"
    elif mutation == "sqlite_collate":
        bind.table_sql = "CREATE TABLE ods_stock_daily_akshare (x TEXT COLLATE NOCASE)"
    elif mutation == "sqlite_conflict":
        bind.table_sql = "CREATE TABLE ods_stock_daily_akshare (x INTEGER ON CONFLICT IGNORE)"
    elif mutation == "sqlite_index":
        inspector.indexes = [
            {
                "name": "ix_unsafe",
                "column_names": ["日期"],
                "unique": False,
                "dialect_options": {},
            }
        ]
        bind.indexes = [
            ("ix_unsafe", "CREATE INDEX ix_unsafe ON ods_stock_daily_akshare (`日期` DESC)")
        ]
    _install_fake_migration_preflight(monkeypatch, migration, inspector=inspector, bind=bind)

    with pytest.raises(RuntimeError, match=message):
        migration._require_legacy_table(mode)


def test_revision_0016_accepts_only_reviewed_mysql_metadata_without_a_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_turnover_migration()
    _install_fake_migration_preflight(monkeypatch, migration)

    migration._require_legacy_table("mysql")

    with pytest.raises(RuntimeError, match="full MySQL partition metadata"):
        migration._normalize_mysql_partition_rows([("p2024", "RANGE COLUMNS", "日期")])


def test_revision_0016_mysql_upgrade_issues_only_nullable_turnover_alter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_turnover_migration()
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(migration, "_require_supported_target", lambda: "mysql")
    monkeypatch.setattr(migration, "_require_legacy_table", lambda mode: calls.append((mode,)))
    monkeypatch.setattr(
        migration.op,
        "alter_column",
        lambda *args, **kwargs: calls.append((*args, kwargs)),
    )

    migration.upgrade()

    assert calls[0] == ("mysql",)
    assert calls[1][0:2] == ("ods_stock_daily_akshare", "成交额")
    assert calls[1][2]["existing_type"].__class__ is migration.Double
    assert calls[1][2]["existing_nullable"] is False
    assert calls[1][2]["nullable"] is True


def test_source_schema_inspection_checks_every_mapped_field_without_row_reads() -> None:
    class Result:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self.rows = rows

        def fetchall(self) -> list[tuple[object, ...]]:
            return self.rows

    class Connection:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self.rows = rows
            self.statement: object | None = None
            self.parameters: object | None = None

        def execute(self, statement: object, parameters: object) -> Result:
            self.statement = statement
            self.parameters = parameters
            return Result(self.rows)

    rows = [
        (name, column_type, "YES" if name == "成交额" else "NO")
        for name, column_type in transfer.SOURCE_COLUMN_TYPES.items()
    ]
    connection = Connection(rows)

    schema = transfer._source_schema(connection)

    assert tuple(schema) == transfer.SOURCE_COLUMNS
    assert schema["成交额"] == ("double", True)
    assert connection.parameters == {
        "schema": transfer.SOURCE_SCHEMA,
        "table": transfer.SOURCE_TABLE,
    }
    assert "information_schema.COLUMNS" in str(connection.statement)

    malformed = list(rows)
    malformed[0] = ("日期", "datetime", "NO")
    with pytest.raises(transfer.TransferError, match="source_column_schema_mismatch"):
        transfer._source_schema(Connection(malformed))


def test_target_shape_inspection_rejects_missing_table_and_reports_nullable_amount() -> None:
    class Result:
        def __init__(
            self,
            *,
            rows: list[tuple[object, ...]] | None = None,
            scalar: object | None = None,
        ) -> None:
            self.rows = rows or []
            self.scalar = scalar

        def fetchall(self) -> list[tuple[object, ...]]:
            return self.rows

        def scalar_one_or_none(self) -> object | None:
            return self.scalar

    class Connection:
        def __init__(self, missing: bool = False) -> None:
            self.missing = missing

        def execute(self, statement: object, _parameters: object = None) -> Result:
            sql = str(statement)
            if "SELECT ENGINE" in sql:
                return Result(scalar=None if self.missing else "InnoDB")
            if "COLUMN_NAME, COLUMN_TYPE" in sql:
                return Result(
                    rows=[
                        (name, column_type, "YES" if nullable else "NO", None, "")
                        for name, (column_type, nullable) in transfer.TARGET_COLUMN_TYPES.items()
                    ]
                )
            if "KEY_COLUMN_USAGE" in sql:
                return Result(rows=[("股票代码",), ("日期",)])
            if "PARTITION_NAME" in sql:
                return Result(rows=[(partition,) for partition in transfer.EXPECTED_PARTITIONS])
            raise AssertionError("unexpected target shape query")

    shape = transfer._target_shape(Connection())
    transfer._require_target_shape(shape)
    amount = next(column for column in shape.columns if column.name == "成交额")
    assert amount.nullable is True
    assert shape.primary_key == transfer.TARGET_KEY

    with pytest.raises(transfer.TransferError, match="target_table_missing"):
        transfer._target_shape(Connection(missing=True))


def test_target_migration_head_guard_rejects_missing_or_stale_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(
            self,
            scalar: object | None = None,
            rows: list[tuple[object, ...]] | None = None,
        ):
            self.scalar = scalar
            self.rows = rows or []

        def scalar_one_or_none(self) -> object | None:
            return self.scalar

        def fetchall(self) -> list[tuple[object, ...]]:
            return self.rows

    class Connection:
        def __init__(self, exists: bool, heads: list[tuple[str, ...]]) -> None:
            self.exists = exists
            self.heads = heads
            self.read_version = False

        def execute(self, statement: object) -> Result:
            if "alembic_version_data" in str(statement) and "SELECT TABLE_NAME" in str(statement):
                return Result(scalar="alembic_version_data" if self.exists else None)
            self.read_version = True
            return Result(rows=self.heads)

    monkeypatch.setattr(
        transfer,
        "_expected_data_heads",
        lambda: ("0016_legacy_turnover_nullable",),
    )
    missing = Connection(False, [])
    with pytest.raises(transfer.TransferError, match="target_migration_version_table_missing"):
        transfer._target_migration_heads(missing)
    assert missing.read_version is False

    stale = Connection(True, [("0015_source_attempt_gate",)])
    with pytest.raises(transfer.TransferError, match="target_migration_head_stale"):
        transfer._target_migration_heads(stale)
    assert stale.read_version is True

    current = Connection(True, [("0016_legacy_turnover_nullable",)])
    assert transfer._target_migration_heads(current) == ("0016_legacy_turnover_nullable",)


def test_fixed_table_row_and_null_count_queries_reject_arbitrary_identifiers() -> None:
    class Result:
        def __init__(self, scalar: object) -> None:
            self.value = scalar

        def scalar_one(self) -> object:
            return self.value

    class Connection:
        def __init__(self, values: list[object]) -> None:
            self.values = values
            self.statements: list[str] = []

        def execute(self, statement: object) -> Result:
            self.statements.append(str(statement))
            return Result(self.values.pop(0))

    connection = Connection([617_371, 2, 0, 2])
    assert transfer._exact_row_count(connection, transfer.SOURCE_TABLE) == 617_371
    assert transfer._exact_row_count(connection, transfer.TARGET_TABLE) == 2
    assert transfer._exact_turnover_amount_null_count(connection, transfer.SOURCE_TABLE) == 0
    assert transfer._exact_turnover_amount_null_count(connection, transfer.TARGET_TABLE) == 2
    assert "STOCK_ZH_A_HIST" in connection.statements[0]
    assert "ods_stock_daily_akshare" in connection.statements[1]
    assert "`成交额` IS NULL" in connection.statements[2]
    with pytest.raises(transfer.TransferError, match="row_count_table_not_allowed"):
        transfer._exact_row_count(connection, "dynamic_table")
    with pytest.raises(transfer.TransferError, match="null_count_table_not_allowed"):
        transfer._exact_turnover_amount_null_count(connection, "dynamic_table")


@pytest.mark.parametrize("invalid_count", [True, -1, 1.0, None])
def test_turnover_null_count_rejects_non_integer_or_negative_database_values(
    invalid_count: object,
) -> None:
    class Result:
        def scalar_one(self) -> object:
            return invalid_count

    class Connection:
        def execute(self, _statement: object) -> Result:
            return Result()

    with pytest.raises(transfer.TransferError, match="turnover_amount_null_count_invalid"):
        transfer._exact_turnover_amount_null_count(Connection(), transfer.SOURCE_TABLE)


@pytest.mark.parametrize(
    "warnings, rowcount, expected, classification",
    [
        ([], 1, 1, None),
        ([("Warning", 1265, "truncated")], 1, None, "target_insert_warnings_present"),
        ([], 0, None, "target_insert_count_mismatch"),
    ],
)
def test_write_batch_commits_only_clean_exact_count_and_rolls_back_failures(
    warnings: list[tuple[object, ...]],
    rowcount: int,
    expected: int | None,
    classification: str | None,
) -> None:
    class Transaction:
        def __init__(self) -> None:
            self.committed = False
            self.rolled_back = False

        def __enter__(self) -> Transaction:
            return self

        def __exit__(self, exc_type: object, _exc: object, _traceback: object) -> bool:
            self.rolled_back = exc_type is not None
            self.committed = exc_type is None
            return False

    class Result:
        def __init__(self, count: int) -> None:
            self.rowcount = count

        def fetchall(self) -> list[tuple[object, ...]]:
            return warnings

    class Connection:
        def __init__(self) -> None:
            self.transaction = Transaction()

        def begin(self) -> Transaction:
            return self.transaction

        def execute(self, _statement: object, _batch: object) -> Result:
            return Result(rowcount)

        def exec_driver_sql(self, statement: str) -> Result:
            assert statement == "SHOW WARNINGS"
            return Result(rowcount)

    connection = Connection()
    table = transfer._target_table()
    batch = [{"股票代码": "600000", "日期": date(2024, 1, 2)}]
    if classification is None:
        assert transfer._write_batch(connection, table, batch) == expected
        assert connection.transaction.committed is True
        assert connection.transaction.rolled_back is False
    else:
        with pytest.raises(transfer.TransferError, match=classification):
            transfer._write_batch(connection, table, batch)
        assert connection.transaction.committed is False
        assert connection.transaction.rolled_back is True


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, True),
        (1, True),
        ("1", True),
        ("ON", True),
        ("true", True),
        (b"1", True),
        (False, False),
        (0, False),
        ("off", False),
        (b"false", False),
        (2, False),
        (None, False),
    ],
)
def test_driver_read_only_flags_are_normalized_without_truthiness_coercion(
    value: object, expected: bool
) -> None:
    assert transfer._driver_flag_is_true(value) is expected


def test_source_session_sets_read_only_repeatable_read_and_closes_driver_cursor() -> None:
    class Cursor:
        def __init__(self, value: object) -> None:
            self.value = value
            self.statements: list[str] = []
            self.closed = False

        def execute(self, statement: str) -> None:
            self.statements.append(statement)

        def fetchone(self) -> tuple[object]:
            return (self.value,)

        def close(self) -> None:
            self.closed = True

    class RawConnection:
        def __init__(self, cursor: Cursor) -> None:
            self._cursor = cursor
            self.committed = False

        def cursor(self) -> Cursor:
            return self._cursor

        def commit(self) -> None:
            self.committed = True

    cursor = Cursor(b"ON")
    raw = RawConnection(cursor)
    connection = SimpleNamespace(connection=SimpleNamespace(driver_connection=raw))

    transfer._configure_source_session(connection)

    assert cursor.statements == [
        "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "SET SESSION TRANSACTION READ ONLY",
        "SELECT @@session.transaction_read_only",
    ]
    assert cursor.closed is True
    assert raw.committed is True

    missing = SimpleNamespace(connection=SimpleNamespace(driver_connection=None))
    with pytest.raises(transfer.TransferError, match="source_driver_connection_missing"):
        transfer._configure_source_session(missing)

    unconfirmed_cursor = Cursor(0)
    unconfirmed_raw = RawConnection(unconfirmed_cursor)
    unconfirmed = SimpleNamespace(connection=SimpleNamespace(driver_connection=unconfirmed_raw))
    with pytest.raises(transfer.TransferError, match="source_read_only_not_confirmed"):
        transfer._configure_source_session(unconfirmed)
    assert unconfirmed_cursor.closed is True


@pytest.mark.parametrize(
    "state, classification",
    [
        ((1, "REPEATABLE-READ"), None),
        ((0, "REPEATABLE-READ"), "source_snapshot_not_readonly_repeatable_read"),
        ((1, "READ-COMMITTED"), "source_snapshot_not_readonly_repeatable_read"),
    ],
)
def test_readonly_source_snapshot_always_removes_sql_guard(
    monkeypatch: pytest.MonkeyPatch,
    state: tuple[object, object],
    classification: str | None,
) -> None:
    class Result:
        def one(self) -> tuple[object, object]:
            return state

    class Transaction:
        def __enter__(self) -> Transaction:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def begin(self) -> Transaction:
            return Transaction()

        def execute(self, _statement: object) -> Result:
            return Result()

    connection = Connection()
    engine = SimpleNamespace(connect=lambda: connection)
    event_calls: list[tuple[str, object, object, object]] = []
    monkeypatch.setattr(
        transfer.event,
        "listen",
        lambda target, name, listener: event_calls.append(("listen", target, name, listener)),
    )
    monkeypatch.setattr(
        transfer.event,
        "remove",
        lambda target, name, listener: event_calls.append(("remove", target, name, listener)),
    )
    monkeypatch.setattr(transfer, "_configure_source_session", lambda _connection: None)

    if classification is None:
        with transfer._readonly_source_snapshot(engine) as yielded:
            assert yielded is connection
    else:
        with (
            pytest.raises(transfer.TransferError, match=classification),
            transfer._readonly_source_snapshot(engine),
        ):
            pytest.fail("invalid read-only state was yielded")

    assert [call[0] for call in event_calls] == ["listen", "remove"]
    assert event_calls[0][1:] == event_calls[1][1:]


class _StreamResult:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.closed = False

    def fetchmany(self, size: int) -> list[tuple[object, ...]]:
        batch, self.rows = self.rows[:size], self.rows[size:]
        return batch

    def close(self) -> None:
        self.closed = True


class _StreamConnection:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.result = _StreamResult(rows)
        self.committed = False

    def execution_options(self, **_options: object) -> _StreamConnection:
        return self

    def execute(self, _statement: object) -> _StreamResult:
        return self.result

    def __enter__(self) -> _StreamConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        self.committed = True


class _StreamEngine:
    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.rows = rows or []
        self.disposed = False

    def connect(self) -> _StreamConnection:
        return _StreamConnection(list(self.rows))

    def dispose(self) -> None:
        self.disposed = True


@pytest.mark.parametrize(
    "rows, source_count, classification",
    [
        ([_source_row()], 2, "source_stream_count_mismatch"),
        ([_source_row(), _source_row()], 2, "source_business_key_not_unique_or_sorted"),
        ([("2024-01-02", "600000")], 1, "source_row_width_mismatch"),
    ],
)
def test_source_stream_failure_paths_close_result_without_silent_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[tuple[object, ...]],
    source_count: int,
    classification: str,
) -> None:
    source_connection = _StreamConnection(rows)
    monkeypatch.setattr(transfer, "_write_batch", lambda _conn, _table, batch: len(batch))

    with pytest.raises(transfer.TransferError, match=classification):
        transfer._stream_source(
            source_connection,
            _StreamEngine(),
            transfer._target_table(),
            fetched_at=datetime(2024, 1, 5),
            batch_id="synthetic-batch",
            source_count=source_count,
            progress=lambda _read, _inserted: None,
        )

    assert source_connection.result.closed is True


@pytest.mark.parametrize(
    "rows, target_count, classification",
    [
        ([], 1, "target_stream_count_mismatch"),
        (
            [(*transfer._normalize_source_row(_source_row()), "wrong", datetime(2024, 1, 5), "b")],
            1,
            "target_transfer_metadata_mismatch",
        ),
        (
            [
                (
                    *transfer._normalize_source_row(_source_row()),
                    transfer.SOURCE,
                    datetime(2024, 1, 5),
                    "b",
                ),
                (
                    *transfer._normalize_source_row(_source_row()),
                    transfer.SOURCE,
                    datetime(2024, 1, 5),
                    "b",
                ),
            ],
            2,
            "target_business_key_not_unique_or_sorted",
        ),
    ],
)
def test_target_stream_failure_paths_close_result_and_reject_bad_rows(
    rows: list[tuple[object, ...]],
    target_count: int,
    classification: str,
) -> None:
    connection = _StreamConnection(rows)
    with pytest.raises(transfer.TransferError, match=classification):
        transfer._stream_target_hash(
            connection,
            fetched_at=datetime(2024, 1, 5),
            batch_id="b",
            target_count=target_count,
        )
    assert connection.result.closed is True


def _run_fake_apply(
    monkeypatch: pytest.MonkeyPatch,
    *,
    corrupt_null_to_zero: bool = False,
    reported_source_nulls: int | None = None,
) -> tuple[dict[str, object], list[tuple[object, ...]], _StreamEngine, _StreamEngine, str | None]:
    first = list(_source_row())
    first[7] = None
    second = list(_source_row())
    second[0] = "2024-01-03"
    second[7] = 0.0
    source_rows = [tuple(first), tuple(second)]
    target_rows: list[tuple[object, ...]] = []
    source_connection = _StreamConnection(source_rows)
    source_engine = _StreamEngine()
    target_engine = _StreamEngine()

    def connect_target() -> _StreamConnection:
        return _StreamConnection(list(target_rows))

    target_engine.connect = connect_target
    engines = iter((source_engine, target_engine))
    monkeypatch.setattr(transfer, "_new_engine", lambda _url: next(engines))
    monkeypatch.setattr(transfer, "EXPECTED_SOURCE_ROWS", len(source_rows))
    monkeypatch.setattr(transfer, "_target_migration_heads", lambda _connection: ("0016",))
    monkeypatch.setattr(transfer, "_target_shape", lambda _connection: _target_shape())
    monkeypatch.setattr(
        transfer,
        "_source_schema",
        lambda _connection: {
            name: (column_type, name == "成交额")
            for name, column_type in transfer.SOURCE_COLUMN_TYPES.items()
        },
    )
    monkeypatch.setattr(
        transfer,
        "_exact_row_count",
        lambda _connection, table: len(source_rows)
        if table == transfer.SOURCE_TABLE
        else len(target_rows),
    )
    monkeypatch.setattr(
        transfer,
        "_exact_turnover_amount_null_count",
        lambda _connection, table: (
            reported_source_nulls
            if table == transfer.SOURCE_TABLE and reported_source_nulls is not None
            else sum(row[7] is None for row in source_rows)
            if table == transfer.SOURCE_TABLE
            else sum(row[7] is None for row in target_rows)
        ),
    )

    @contextmanager
    def source_snapshot(_engine: object) -> Iterator[_StreamConnection]:
        yield source_connection

    monkeypatch.setattr(transfer, "_readonly_source_snapshot", source_snapshot)

    def write_batch(
        _connection: object,
        _table: object,
        batch: list[dict[str, object]],
    ) -> int:
        for batch_row in batch:
            values = [batch_row[column] for column in transfer.SOURCE_COLUMNS]
            if corrupt_null_to_zero and values[transfer.TURNOVER_AMOUNT_INDEX] is None:
                values[transfer.TURNOVER_AMOUNT_INDEX] = 0.0
            target_rows.append(
                (
                    *values,
                    batch_row["_source"],
                    batch_row["_fetched_at"],
                    batch_row["_batch_id"],
                )
            )
        return len(batch)

    monkeypatch.setattr(transfer, "_write_batch", write_batch)
    state = transfer._empty_state(apply=True)
    source_url = transfer.URL.create(
        "mysql+pymysql",
        username="reader",
        password="synthetic",
        host="127.0.0.1",
        port=transfer.SOURCE_PORT,
        database=transfer.SOURCE_SCHEMA,
    )
    target_url = transfer.URL.create(
        "mysql+pymysql",
        username="writer",
        password="synthetic",
        host="localhost",
        port=transfer.TARGET_PORT,
        database=f"{transfer.TARGET_SCHEMA_PREFIX}synthetic",
    )
    error: str | None = None
    try:
        transfer._perform_apply(state, source_url, target_url)
    except transfer.TransferError as exc:
        error = exc.classification
    return state, target_rows, source_engine, target_engine, error


def test_fake_apply_streams_null_and_zero_and_verifies_full_source_target_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, target_rows, source_engine, target_engine, error = _run_fake_apply(monkeypatch)

    assert error is None
    assert [row[transfer.TURNOVER_AMOUNT_INDEX] for row in target_rows] == [None, 0.0]
    assert state["source_turnover_amount_null_rows"] == 1
    assert state["target_turnover_amount_null_rows"] == 1
    assert state["source_sha256"] == state["target_sha256"]
    assert state["full_stream_hash_match"] is True
    assert state["full_source_transfer_verified"] is True
    assert source_engine.disposed is True
    assert target_engine.disposed is True


def test_fake_apply_detects_null_count_drift_and_disposes_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, target_rows, source_engine, target_engine, error = _run_fake_apply(
        monkeypatch,
        corrupt_null_to_zero=True,
    )

    assert error == "source_target_turnover_amount_null_count_mismatch"
    assert len(target_rows) == 2
    assert target_rows[0][transfer.TURNOVER_AMOUNT_INDEX] == 0.0
    assert state["source_turnover_amount_null_rows"] == 1
    assert state["target_turnover_amount_null_rows"] == 0
    assert source_engine.disposed is True
    assert target_engine.disposed is True


def test_fake_apply_reconciles_stream_null_count_to_source_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _state, _target_rows, source_engine, target_engine, error = _run_fake_apply(
        monkeypatch,
        reported_source_nulls=0,
    )

    assert error == "source_turnover_amount_null_count_mismatch"
    assert source_engine.disposed is True
    assert target_engine.disposed is True
