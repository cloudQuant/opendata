from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.ops import legacy_stock_daily_inventory as inventory
from scripts.ops import migrate_legacy_stock_daily as transfer

_SERVER_UUID = "11111111-2222-4333-8444-555555555555"
_UUID_WITH_HEX_LETTERS = "abcdefab-cdef-4abc-8def-abcdefabcdef"
_REVIEWED_COLUMNS = (
    ("R_ID", "int", "NO"),
    ("symbol", "varchar(50)", "YES"),
    ("name", "varchar(100)", "YES"),
    ("data_date", "date", "YES"),
    ("created_at", "datetime", "YES"),
    ("updated_at", "datetime", "YES"),
    ("日期", "text", "YES"),
    ("开盘", "double", "YES"),
    ("收盘", "double", "YES"),
    ("最高", "double", "YES"),
    ("最低", "double", "YES"),
    ("成交量", "bigint", "YES"),
    ("成交额", "double", "YES"),
    ("涨跌额", "double", "YES"),
    ("涨跌幅", "double", "YES"),
    ("振幅", "double", "YES"),
    ("换手率", "text", "YES"),
    ("股票代码", "text", "YES"),
)


class _Result:
    def __init__(
        self,
        rows: list[tuple[object, ...]],
        *,
        fetch_error: bool = False,
        close_error: bool = False,
        oversized_page: bool = False,
        on_close: object | None = None,
    ) -> None:
        self._rows = rows
        self._offset = 0
        self.closed = False
        self.fetch_error = fetch_error
        self.close_error = close_error
        self.oversized_page = oversized_page
        self.on_close = on_close

    def one(self) -> tuple[object, ...]:
        assert len(self._rows) == 1
        return self._rows[0]

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows

    def fetchmany(self, size: int) -> list[tuple[object, ...]]:
        if self.fetch_error:
            raise RuntimeError("private fetch failure")
        if self.oversized_page and self._offset == 0:
            self._offset = min(size + 1, len(self._rows))
            return [self._rows[0]] * (size + 1)
        rows = self._rows[self._offset : self._offset + size]
        self._offset += len(rows)
        return rows

    def close(self) -> None:
        self.closed = True
        if callable(self.on_close):
            self.on_close()
        if self.close_error:
            raise RuntimeError("private close failure")


class _Connection:
    def __init__(
        self,
        rows: list[tuple[object, ...]] | None = None,
        *,
        database: str = "akshare_data",
        server_uuid: object = _SERVER_UUID,
        read_only: object = 1,
        isolation: str = "REPEATABLE-READ",
        engine: str = "InnoDB",
        columns: tuple[tuple[str, str, str], ...] = _REVIEWED_COLUMNS,
        primary_key: tuple[str, ...] = ("R_ID",),
        active: bool = True,
        identity_row: tuple[object, ...] | None = None,
        table_row: tuple[object, ...] | None = None,
        column_rows: tuple[tuple[object, ...], ...] | None = None,
        schema_query_error: str | None = None,
        scan_query_error: bool = False,
        scan_fetch_error: bool = False,
        scan_close_error: bool = False,
        scan_oversized_page: bool = False,
        active_after_scan_close: bool = True,
        read_only_after_scan: object | None = None,
    ) -> None:
        self.dialect = SimpleNamespace(name="mysql")
        self._rows = rows if rows is not None else [_source_row()]
        self.database = database
        self.server_uuid = server_uuid
        self.read_only = read_only
        self.isolation = isolation
        self.engine = engine
        self.columns = columns
        self.primary_key = primary_key
        self.active = active
        self.identity_row = identity_row
        self.table_row = table_row
        self.column_rows = column_rows
        self.schema_query_error = schema_query_error
        self.scan_query_error = scan_query_error
        self.scan_fetch_error = scan_fetch_error
        self.scan_close_error = scan_close_error
        self.scan_oversized_page = scan_oversized_page
        self.active_after_scan_close = active_after_scan_close
        self.read_only_after_scan = read_only_after_scan
        self.statements: list[str] = []
        self.results: list[_Result] = []
        self.source_scan_result: _Result | None = None

    def in_transaction(self) -> bool:
        return self.active

    def execution_options(self, **_options: object) -> _Connection:
        return self

    def execute(self, statement: object, _params: object = None) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        if self.schema_query_error is not None and self.schema_query_error in sql:
            raise RuntimeError("private metadata failure")
        if "SELECT DATABASE()" in sql:
            rows = (
                [self.identity_row]
                if self.identity_row is not None
                else [(self.database, self.server_uuid, self.read_only, self.isolation)]
            )
        elif "FROM information_schema.TABLES" in sql:
            rows = [self.table_row or ("BASE TABLE", self.engine)]
        elif "FROM information_schema.COLUMNS" in sql:
            rows = (
                list(self.column_rows)
                if self.column_rows is not None
                else [
                    (name, column_type, nullable, ordinal)
                    for ordinal, (name, column_type, nullable) in enumerate(self.columns, start=1)
                ]
            )
        elif "FROM information_schema.KEY_COLUMN_USAGE" in sql:
            rows = [(column,) for column in self.primary_key]
        elif "FROM `STOCK_ZH_A_HIST`" in sql:
            if self.scan_query_error:
                raise RuntimeError("private scan failure")
            rows = self._rows
        else:
            raise AssertionError(f"unexpected SQL: {sql}")
        result = _Result(
            rows,
            fetch_error=self.scan_fetch_error and "FROM `STOCK_ZH_A_HIST`" in sql,
            close_error=self.scan_close_error and "FROM `STOCK_ZH_A_HIST`" in sql,
            oversized_page=self.scan_oversized_page and "FROM `STOCK_ZH_A_HIST`" in sql,
            on_close=(
                lambda: setattr(self, "active", self.active_after_scan_close)
                if "FROM `STOCK_ZH_A_HIST`" in sql
                else None
            ),
        )
        self.results.append(result)
        if "FROM `STOCK_ZH_A_HIST`" in sql:
            self.source_scan_result = result
            if self.read_only_after_scan is not None:
                self.read_only = self.read_only_after_scan
        return result


def _source_row(
    trade_date: str = "2024-01-02",
    turnover_amount: object = 123.5,
) -> tuple[object, ...]:
    return (
        trade_date,
        "600000",
        10.0,
        11.0,
        12.0,
        9.0,
        2,
        turnover_amount,
        30.0,
        10.0,
        1.0,
        "0.25",
    )


def _inventory(connection: _Connection) -> inventory.LegacyStockDailyInventory:
    return inventory.inventory_source(connection, expected_server_uuid=_SERVER_UUID)  # type: ignore[arg-type]


def test_inventory_scans_before_unbound_post_scan_schema_observation() -> None:
    rows = [_source_row(), _source_row("2024-01-03", None)]
    connection = _Connection(rows)

    facts = _inventory(connection)

    assert facts.source_schema == "akshare_data"
    assert facts.source_table == "STOCK_ZH_A_HIST"
    assert facts.source_server_uuid == _SERVER_UUID
    assert facts.observed_after_scan_source_engine == "innodb"
    assert facts.observed_after_scan_source_primary_key == ("R_ID",)
    assert facts.schema_observation_scope == "post_scan_unbound_metadata_observation"
    assert facts.business_key == ("股票代码", "日期")
    assert facts.row_count == facts.distinct_business_key_count == 2
    assert facts.first_business_date is not None
    assert facts.last_business_date is not None
    assert facts.first_business_date.isoformat() == "2024-01-02"
    assert facts.last_business_date.isoformat() == "2024-01-03"
    assert dict(facts.null_counts)["成交额"] == 1
    assert facts.observed_session_read_only_setting is True
    assert facts.observed_session_isolation_setting == "repeatable-read"
    assert facts.inventory_scope == "single_streaming_select_observation"
    assert connection.active is True
    assert all(
        statement.lstrip().upper().startswith("SELECT") for statement in connection.statements
    )
    scan_index = next(
        index
        for index, statement in enumerate(connection.statements)
        if "FROM `STOCK_ZH_A_HIST`" in statement
    )
    first_shape_index = next(
        index
        for index, statement in enumerate(connection.statements)
        if "FROM information_schema.TABLES" in statement
    )
    assert scan_index < first_shape_index
    assert connection.source_scan_result is not None
    assert connection.source_scan_result.closed is True


def test_inventory_digest_matches_existing_transfer_contract() -> None:
    rows = [_source_row(), _source_row("2024-01-03", None)]
    connection = _Connection(rows)

    facts = _inventory(connection)
    normalized = [transfer._normalize_source_row(row) for row in rows]
    expected_count, expected_sha256 = transfer._digest_rows(normalized)

    assert facts.row_count == expected_count
    assert facts.source_sha256 == expected_sha256
    assert facts.digest_algorithm == "sha256_typed_length_prefixed_v1"
    assert len(facts.observed_after_scan_schema_sha256) == 64


def test_inventory_preserves_null_as_distinct_from_numeric_zero() -> None:
    missing = _inventory(_Connection([_source_row(turnover_amount=None)]))
    zero = _inventory(_Connection([_source_row(turnover_amount=0.0)]))

    assert dict(missing.null_counts)["成交额"] == 1
    assert dict(zero.null_counts)["成交额"] == 0
    assert missing.source_sha256 != zero.source_sha256


@pytest.mark.parametrize(
    "connection, pin, classification",
    [
        (_Connection(database="other"), _SERVER_UUID, "source_schema_identity_mismatch"),
        (
            _Connection(server_uuid="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
            _SERVER_UUID,
            "source_server_uuid_pin_mismatch",
        ),
        (_Connection(active=False), _SERVER_UUID, "caller_transaction_required"),
        (_Connection(), "not-a-uuid", "reviewed_server_uuid_pin_invalid"),
    ],
)
def test_inventory_rejects_identity_or_connection_precondition_failures(
    connection: _Connection,
    pin: str,
    classification: str,
) -> None:
    with pytest.raises(inventory.LegacyStockDailyInventoryError) as exc_info:
        inventory.inventory_source(connection, expected_server_uuid=pin)  # type: ignore[arg-type]

    assert exc_info.value.classification == classification


def test_session_settings_are_reported_without_claiming_transaction_mode() -> None:
    facts = _inventory(_Connection(read_only=0, isolation="READ-COMMITTED"))

    assert facts.observed_session_read_only_setting is False
    assert facts.observed_session_isolation_setting == "read-committed"
    assert facts.inventory_scope == "single_streaming_select_observation"
    assert facts.schema_observation_scope == "post_scan_unbound_metadata_observation"


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, True),
        (False, False),
        (b"On", True),
        (b"OFF", False),
        ("true", True),
        ("0", False),
    ],
)
def test_mysql_boolean_setting_accepts_documented_driver_shapes(
    value: object,
    expected: bool,
) -> None:
    assert inventory._mysql_boolean(value) is expected


@pytest.mark.parametrize(
    "value",
    [None, 2, "yes", b"unknown"],
)
def test_mysql_boolean_setting_rejects_ambiguous_values(value: object) -> None:
    with pytest.raises(inventory.LegacyStockDailyInventoryError) as exc_info:
        inventory._mysql_boolean(value)

    assert exc_info.value.classification == "source_read_only_setting_unavailable"


@pytest.mark.parametrize(
    "connection, classification",
    [
        (
            SimpleNamespace(dialect=SimpleNamespace(name="sqlite"), in_transaction=lambda: True),
            "mysql_connection_required",
        ),
        (
            SimpleNamespace(dialect=SimpleNamespace(name="mysql"), in_transaction=True),
            "caller_transaction_required",
        ),
        (SimpleNamespace(in_transaction=lambda: True), "mysql_connection_required"),
    ],
)
def test_inventory_rejects_non_mysql_or_non_connection_objects(
    connection: object,
    classification: str,
) -> None:
    with pytest.raises(inventory.LegacyStockDailyInventoryError) as exc_info:
        inventory.inventory_source(
            connection,  # type: ignore[arg-type]
            expected_server_uuid=_SERVER_UUID,
        )

    assert exc_info.value.classification == classification


@pytest.mark.parametrize(
    "pin",
    [None, _UUID_WITH_HEX_LETTERS.upper()],
)
def test_inventory_rejects_noncanonical_operator_uuid_pin(pin: object) -> None:
    with pytest.raises(inventory.LegacyStockDailyInventoryError) as exc_info:
        inventory.inventory_source(
            _Connection(),  # type: ignore[arg-type]
            expected_server_uuid=pin,  # type: ignore[arg-type]
        )

    assert exc_info.value.classification == "reviewed_server_uuid_pin_invalid"


@pytest.mark.parametrize(
    "connection, classification",
    [
        (
            _Connection(identity_row=("akshare_data", _SERVER_UUID, 1)),
            "source_schema_identity_mismatch",
        ),
        (_Connection(server_uuid=None), "source_server_uuid_unavailable"),
        (_Connection(isolation="  "), "source_isolation_setting_unavailable"),
        (_Connection(read_only="unknown"), "source_read_only_setting_unavailable"),
    ],
)
def test_inventory_fails_closed_on_malformed_session_observations(
    connection: _Connection,
    classification: str,
) -> None:
    with pytest.raises(inventory.LegacyStockDailyInventoryError) as exc_info:
        _inventory(connection)

    assert exc_info.value.classification == classification
    assert connection.source_scan_result is None


@pytest.mark.parametrize(
    "connection, classification",
    [
        (_Connection(engine="MyISAM"), "source_table_engine_mismatch"),
        (_Connection(primary_key=("股票代码", "日期")), "source_primary_key_mismatch"),
        (
            _Connection(columns=_REVIEWED_COLUMNS + (("unexpected", "text", "YES"),)),
            "source_column_schema_mismatch",
        ),
        (
            _Connection(
                columns=(
                    ("R_ID", "bigint", "NO"),
                    *_REVIEWED_COLUMNS[1:],
                )
            ),
            "source_column_schema_mismatch",
        ),
    ],
)
def test_inventory_rejects_unreviewed_table_shape(
    connection: _Connection,
    classification: str,
) -> None:
    with pytest.raises(inventory.LegacyStockDailyInventoryError) as exc_info:
        _inventory(connection)

    assert exc_info.value.classification == classification


@pytest.mark.parametrize(
    "connection, classification",
    [
        (_Connection(table_row=("VIEW", "InnoDB")), "source_table_identity_mismatch"),
        (_Connection(table_row=("BASE TABLE",)), "source_table_identity_mismatch"),
        (
            _Connection(
                column_rows=(
                    ("R_ID", "int", "NO", 2),
                    *tuple(
                        (name, column_type, nullable, ordinal)
                        for ordinal, (name, column_type, nullable) in enumerate(
                            _REVIEWED_COLUMNS[1:], start=2
                        )
                    ),
                )
            ),
            "source_column_order_mismatch",
        ),
        (
            _Connection(
                column_rows=(
                    ("R_ID", "int", "MAYBE", 1),
                    *tuple(
                        (name, column_type, nullable, ordinal)
                        for ordinal, (name, column_type, nullable) in enumerate(
                            _REVIEWED_COLUMNS[1:], start=2
                        )
                    ),
                )
            ),
            "source_column_nullability_unknown",
        ),
        (
            _Connection(column_rows=(("R_ID", "int", "NO"),)),
            "source_column_metadata_invalid",
        ),
        (
            _Connection(schema_query_error="information_schema.KEY_COLUMN_USAGE"),
            "source_schema_inspection_failed",
        ),
    ],
)
def test_inventory_rejects_malformed_or_unavailable_schema_metadata(
    connection: _Connection,
    classification: str,
) -> None:
    with pytest.raises(inventory.LegacyStockDailyInventoryError) as exc_info:
        _inventory(connection)

    assert exc_info.value.classification == classification
    assert connection.source_scan_result is not None
    assert connection.source_scan_result.closed is True


def test_inventory_rejects_duplicate_business_keys() -> None:
    connection = _Connection([_source_row(), _source_row()])

    with pytest.raises(inventory.LegacyStockDailyInventoryError) as exc_info:
        _inventory(connection)

    assert exc_info.value.classification == "source_business_key_duplicate"


def test_inventory_rejects_rows_outside_legacy_transfer_contract() -> None:
    connection = _Connection([_source_row()[:-1]])

    with pytest.raises(inventory.LegacyStockDailyInventoryError) as exc_info:
        _inventory(connection)

    assert exc_info.value.classification == "source_row_width_mismatch"
    assert connection.source_scan_result is not None
    assert connection.source_scan_result.closed is True


def test_inventory_translates_digest_contract_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_digest(_digest: object, _values: object) -> None:
        raise transfer.TransferError("unsupported_hash_value_type")

    monkeypatch.setattr(inventory, "_update_row_digest", fail_digest)
    connection = _Connection()

    with pytest.raises(inventory.LegacyStockDailyInventoryError) as exc_info:
        _inventory(connection)

    assert exc_info.value.classification == "unsupported_hash_value_type"
    assert connection.source_scan_result is not None
    assert connection.source_scan_result.closed is True


def test_inventory_rejects_non_date_from_legacy_normalizer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        inventory,
        "_normalize_source_row",
        lambda _row: ("not-a-date", "600000", *([0.0] * 10)),
    )
    connection = _Connection()

    with pytest.raises(inventory.LegacyStockDailyInventoryError) as exc_info:
        _inventory(connection)

    assert exc_info.value.classification == "source_date_normalization_failed"
    assert connection.source_scan_result is not None
    assert connection.source_scan_result.closed is True


def test_inventory_rejects_out_of_order_business_keys() -> None:
    connection = _Connection([_source_row("2024-01-03"), _source_row("2024-01-02")])

    with pytest.raises(inventory.LegacyStockDailyInventoryError) as exc_info:
        _inventory(connection)

    assert exc_info.value.classification == "source_business_key_order_mismatch"


def test_inventory_empty_source_has_no_inferred_date_bounds() -> None:
    facts = _inventory(_Connection([]))

    assert facts.row_count == facts.distinct_business_key_count == 0
    assert facts.first_business_date is None
    assert facts.last_business_date is None
    assert all(count == 0 for _, count in facts.null_counts)


@pytest.mark.parametrize(
    "connection, classification",
    [
        (_Connection(scan_query_error=True), "source_scan_query_failed"),
        (_Connection(scan_fetch_error=True), "source_scan_failed"),
        (_Connection(scan_close_error=True), "source_scan_close_failed"),
        (_Connection(scan_oversized_page=True), "source_fetch_page_too_large"),
        (
            _Connection(active_after_scan_close=False),
            "caller_client_transaction_state_lost_during_inventory",
        ),
        (
            _Connection(read_only_after_scan=0),
            "source_observed_connection_state_changed_during_inventory",
        ),
    ],
)
def test_inventory_reports_stream_and_connection_boundary_failures(
    connection: _Connection,
    classification: str,
) -> None:
    with pytest.raises(inventory.LegacyStockDailyInventoryError) as exc_info:
        _inventory(connection)

    assert exc_info.value.classification == classification
    if connection.source_scan_result is not None:
        assert connection.source_scan_result.closed is True


def test_inventory_does_not_leak_driver_error_text() -> None:
    class _FailingConnection(_Connection):
        def execute(self, statement: object, _params: object = None) -> _Result:
            if "SELECT DATABASE()" in str(statement):
                raise RuntimeError("password=private-value")
            return super().execute(statement, _params)

    with pytest.raises(inventory.LegacyStockDailyInventoryError) as exc_info:
        _inventory(_FailingConnection())

    assert str(exc_info.value) == "source_identity_query_failed"
    assert "private-value" not in str(exc_info.value)
