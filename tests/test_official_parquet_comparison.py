"""Synthetic Arrow contract tests for the offline official Parquet comparator."""

from __future__ import annotations

import io
import json
from datetime import date
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from opendata.data.models import Bar, CorporateAction
from scripts.ops import compare_official_parquet as compare

if TYPE_CHECKING:
    from pathlib import Path

DAILY_SCHEMA = pa.schema(
    [
        pa.field("thscode", pa.string()),
        pa.field("currency", pa.string()),
        pa.field("interval", pa.string()),
        pa.field("adjusted", pa.string()),
        pa.field("date_ms", pa.int64()),
        pa.field("open_price", pa.float64()),
        pa.field("high_price", pa.float64()),
        pa.field("low_price", pa.float64()),
        pa.field("close_price", pa.float64()),
        pa.field("volume", pa.float64()),
        pa.field("turnover", pa.float64()),
    ]
)

ADJUSTMENT_SCHEMA = pa.schema(
    [
        pa.field("thscode", pa.string()),
        pa.field("ticker", pa.string()),
        pa.field("ex_date_ms", pa.int64()),
        pa.field("dividend_per_share", pa.float64()),
        pa.field("per_share_bonus", pa.float64()),
        pa.field("allotment_ratio", pa.float64()),
        pa.field("allotment_price", pa.float64()),
        pa.field("currency", pa.string()),
    ]
)

DAILY_ROWS = [
    {
        "thscode": "600519.SH",
        "currency": "CNY",
        "interval": "1d",
        "adjusted": "none",
        "date_ms": 1704124800000,
        "open_price": 100.25,
        "high_price": 102.5,
        "low_price": 99.75,
        "close_price": 101.5,
        "volume": 123456.0,
        "turnover": 12500000.25,
    },
    {
        "thscode": "000001.SZ",
        "currency": "CNY",
        "interval": "1d",
        "adjusted": "none",
        "date_ms": 1704211200000,
        "open_price": 10.1,
        "high_price": 10.5,
        "low_price": 10.0,
        "close_price": 10.25,
        "volume": 98765.0,
        "turnover": 1012500.5,
    },
]

ADJUSTMENT_ROWS = [
    {
        "thscode": "600519.SH",
        "ticker": "600519",
        "ex_date_ms": 1704124800000,
        "dividend_per_share": 1.25,
        "per_share_bonus": 0.1,
        "allotment_ratio": 0.02,
        "allotment_price": 8.5,
        "currency": "CNY",
    },
    {
        "thscode": "000001.SZ",
        "ticker": "000001",
        "ex_date_ms": 1704211200000,
        "dividend_per_share": 0.25,
        "per_share_bonus": 0.0,
        "allotment_ratio": 0.0,
        "allotment_price": 0.0,
        "currency": "CNY",
    },
]


def _write_parquet(
    tmp_path: Path,
    name: str,
    rows: list[dict[str, object]],
    schema: pa.Schema,
) -> Path:
    path = tmp_path / name
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)
    return path


def _issue_codes(report: dict[str, object]) -> set[str]:
    issues = report["issues"]
    assert isinstance(issues, list)
    return {str(issue["code"]) for issue in issues}


def test_daily_parser_mapping_is_checked_against_each_raw_row(tmp_path: Path) -> None:
    path = _write_parquet(tmp_path, "synthetic-daily.parquet", DAILY_ROWS, DAILY_SCHEMA)

    report = compare.compare_file(path, "daily")
    parsed = compare.read_daily_k_dump(path)

    assert report["status"] == "PASS"
    assert len(report["mapping"]) == 11
    comparison = report["comparison"]
    assert comparison["value_diff_count"] == 0
    assert comparison["missing_parsed_key_count"] == 0
    assert comparison["max_absolute_error"] == 0.0
    assert report["official_api_example_comparison"]["status"] == "NOT_COMPARABLE"

    first = parsed[0]
    assert first == Bar(
        symbol="000001.SZ",
        trade_date=date(2024, 1, 3),
        open=10.1,
        high=10.5,
        low=10.0,
        close=10.25,
        volume=98765.0,
        amount=1012500.5,
    )
    mapping = {item["source_column"]: item for item in report["mapping"]}
    assert mapping["date_ms"]["transform"] == "Unix milliseconds to Asia/Shanghai date"
    assert mapping["volume"]["unit"] == "shares"
    assert mapping["turnover"]["parsed_field"] == "amount"
    assert mapping["turnover"]["unit"] == "CNY"


def test_adjustment_mapping_preserves_suffix_units_and_numeric_values(tmp_path: Path) -> None:
    path = _write_parquet(
        tmp_path, "synthetic-adjustment.parquet", ADJUSTMENT_ROWS, ADJUSTMENT_SCHEMA
    )

    report = compare.compare_file(path, "adjustment")
    parsed = compare.read_adjustment_factors_dump(path)

    assert report["status"] == "PASS"
    assert len(report["mapping"]) == 8
    assert report["comparison"]["value_diff_count"] == 0
    assert report["comparison"]["max_relative_error"] == 0.0
    first = next(item for item in parsed if item.symbol == "600519.SH")
    assert first == CorporateAction(
        symbol="600519.SH",
        ex_date=date(2024, 1, 2),
        cash_dividend=1.25,
        stock_dividend=0.1,
        rights_shares=0.02,
        rights_price=8.5,
    )
    mapping = {item["source_column"]: item for item in report["mapping"]}
    assert mapping["thscode"]["transform"] == "identity; preserve exchange suffix"
    assert mapping["per_share_bonus"]["transform"] == "numeric identity; preserve ratio"
    assert mapping["allotment_ratio"]["unit"] == "shares per share ratio"


def test_documented_beijing_exchange_suffix_is_accepted(tmp_path: Path) -> None:
    row = {**DAILY_ROWS[0], "thscode": "430047.BJ"}
    path = _write_parquet(tmp_path, "synthetic-beijing.parquet", [row], DAILY_SCHEMA)

    report = compare.compare_file(path, "daily")
    parsed = compare.read_daily_k_dump(path)

    assert report["status"] == "PASS"
    assert parsed[0].symbol == "430047.BJ"


def test_unknown_exchange_suffix_is_rejected(tmp_path: Path) -> None:
    row = {**DAILY_ROWS[0], "thscode": "600519.XX"}
    path = _write_parquet(tmp_path, "synthetic-unknown-exchange.parquet", [row], DAILY_SCHEMA)

    report = compare.compare_file(path, "daily")

    assert report["status"] == "FAIL"
    assert "invalid_symbol_suffix" in _issue_codes(report)


def test_unit_scale_or_parser_value_drift_is_reported_exactly(tmp_path: Path) -> None:
    path = _write_parquet(tmp_path, "synthetic-daily.parquet", DAILY_ROWS[:1], DAILY_SCHEMA)

    def distorted_parser(_: Path) -> tuple[Bar, ...]:
        return (
            Bar(
                symbol="600519.SH",
                trade_date=date(2024, 1, 2),
                open=100.25,
                high=102.5,
                low=99.75,
                close=101.5,
                volume=123456.0,
                amount=12500000.25 * 100,
            ),
        )

    report = compare.compare_file(path, "daily", parser_override=distorted_parser)
    difference = report["comparison"]["value_diffs"][0]

    assert report["status"] == "FAIL"
    assert difference["source_column"] == "turnover"
    assert difference["parsed_field"] == "amount"
    assert difference["expected_from_raw"] == 12500000.25
    assert difference["actual_parsed"] == 1250000025.0
    assert difference["relative_error"] == pytest.approx(99.0)
    assert "parsed_value_differences" in _issue_codes(report)


def test_first_duplicate_occurrence_value_drift_is_reported(tmp_path: Path) -> None:
    second_row = {**DAILY_ROWS[0], "open_price": 100.75}
    path = _write_parquet(
        tmp_path,
        "synthetic-duplicate-daily.parquet",
        [DAILY_ROWS[0], second_row],
        DAILY_SCHEMA,
    )
    parsed = compare.read_daily_k_dump(path)
    first = parsed[0]
    corrupted_first = Bar(
        symbol=first.symbol,
        trade_date=first.trade_date,
        open=999.0,
        high=first.high,
        low=first.low,
        close=first.close,
        volume=first.volume,
        amount=first.amount,
    )

    report = compare.compare_file(
        path,
        "daily",
        parser_override=lambda _: (corrupted_first, parsed[1]),
    )

    differences = report["comparison"]["value_diffs"]
    assert report["status"] == "FAIL"
    assert report["comparison"]["value_diff_count"] == 1
    assert differences[0]["source_column"] == "open_price"
    assert differences[0]["occurrence_index"] == 0
    assert differences[0]["raw_row_index"] == 0
    assert differences[0]["expected_from_raw"] == 100.25
    assert differences[0]["actual_parsed"] == 999.0
    assert "duplicate_raw_keys" in _issue_codes(report)
    assert "duplicate_parsed_keys" in _issue_codes(report)


def test_missing_parser_key_is_reported(tmp_path: Path) -> None:
    path = _write_parquet(tmp_path, "synthetic-daily.parquet", DAILY_ROWS, DAILY_SCHEMA)
    incomplete_rows = compare.read_daily_k_dump(path)[:1]

    report = compare.compare_file(
        path,
        "daily",
        parser_override=lambda _: incomplete_rows,
    )

    assert report["status"] == "FAIL"
    assert report["comparison"]["missing_parsed_key_count"] == 1
    assert "missing_parsed_keys" in _issue_codes(report)


def test_schema_drift_reports_missing_extra_and_wrong_types(tmp_path: Path) -> None:
    malformed_schema = pa.schema(
        [
            pa.field("thscode", pa.int64()),
            pa.field("interval", pa.string()),
            pa.field("adjusted", pa.string()),
            pa.field("date_ms", pa.float64()),
            pa.field("open_price", pa.string()),
            pa.field("high_price", pa.float64()),
            pa.field("low_price", pa.float64()),
            pa.field("close_price", pa.float64()),
            pa.field("volume", pa.float64()),
            pa.field("turnover", pa.float64()),
            pa.field("extra", pa.string()),
        ]
    )
    row = {
        "thscode": 600519,
        "interval": "1d",
        "adjusted": "none",
        "date_ms": float(1716134400000),
        "open_price": "100.25",
        "high_price": 102.5,
        "low_price": 99.75,
        "close_price": 101.5,
        "volume": 123456.0,
        "turnover": 12500000.25,
        "extra": "unexpected",
    }
    path = _write_parquet(tmp_path, "bad-schema.parquet", [row], malformed_schema)

    report = compare.compare_file(path, "daily")

    assert report["status"] == "FAIL"
    assert report["raw"]["schema"][0]["arrow_type_family"] == "integer"
    assert report["raw"]["schema"][0]["expected_type_family"] == "string"
    codes = _issue_codes(report)
    assert {
        "missing_columns",
        "unexpected_columns",
        "column_order_mismatch",
        "wrong_type_family",
    } <= codes
    assert report["comparison"]["value_diff_count"] is None
    assert "project_parser_error" in codes
    assert all(
        "exception_type" in issue or issue["code"] != "project_parser_error"
        for issue in report["issues"]
    )


@pytest.mark.parametrize(
    ("rows", "expected_issue"),
    [
        ([{**DAILY_ROWS[0], "thscode": None}], "null_key_rows"),
        ([DAILY_ROWS[0], DAILY_ROWS[0]], "duplicate_raw_keys"),
        ([{**DAILY_ROWS[0], "open_price": float("nan")}], "nonfinite_numeric_value"),
    ],
)
def test_null_duplicate_and_nonfinite_inputs_fail_closed(
    tmp_path: Path,
    rows: list[dict[str, object]],
    expected_issue: str,
) -> None:
    path = _write_parquet(tmp_path, "invalid-daily.parquet", rows, DAILY_SCHEMA)

    report = compare.compare_file(path, "daily")

    assert report["status"] == "FAIL"
    assert expected_issue in _issue_codes(report)


def test_cli_only_writes_a_report_when_out_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_parquet(tmp_path, "synthetic-daily.parquet", DAILY_ROWS[:1], DAILY_SCHEMA)
    output = io.StringIO()
    monkeypatch.setattr("sys.stdout", output)

    exit_code = compare.main(["--daily-parquet", str(path)])

    assert exit_code == 0
    assert json.loads(output.getvalue())["status"] == "PASS"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["synthetic-daily.parquet"]

    report_path = tmp_path / "report.json"
    assert compare.main(["--daily-parquet", str(path), "--out", str(report_path)]) == 0
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "PASS"
