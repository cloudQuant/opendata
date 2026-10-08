from __future__ import annotations

import json
import stat
from datetime import date, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.dialects import mysql

from scripts.ops import migrate_legacy_stock_daily as transfer

if TYPE_CHECKING:
    from pathlib import Path


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
        ({7: float("nan")}, "nonfinite_numeric_field_成交额"),
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
