#!/usr/bin/env python3
"""Compare Fuyao market-dump parser output with rows in supplied Parquet files.

This is an offline, read-only check. It never downloads data or writes a report
unless ``--out`` is supplied. Official schema and example provenance is pinned
below to the public documentation snapshot recorded for C65.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from opendata.data.providers.ths.dumps import (  # noqa: E402
    read_adjustment_factors_dump,
    read_daily_k_dump,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from opendata.data.models import Bar, CorporateAction

RTOL = 1e-9
SHANGHAI = ZoneInfo("Asia/Shanghai")
CODE_RE = re.compile(r"^(?P<ticker>\d{6})\.(?P<exchange>SH|SZ|BJ)$")

DOC_SNAPSHOT = {
    "url": "https://fuyao.aicubes.cn/llms-full.txt",
    "retrieved_at": "2026-09-30T15:01:20.590097+00:00",
    "sha256": "599fe34d0a497535f795cde31f938a7fe8ee6441eedb67882e934b4e58c002c3",
    "capture_record": "docs/evidence/C65/official-parquet-doc-fetch.json",
}

OFFICIAL_FILE_RECORD: dict[str, Any] = {
    "retrieved_at": "2026-09-30T14:56:28.484940+00:00",
    "capture_record": "docs/evidence/C65/official-parquet-download.json",
    "daily": {
        "file_name": "a_share_daily_k_1d_none_10d.parquet",
        "download_endpoint": "https://fuyao.aicubes.cn/api/dump/market-dumps/daily-k-10d/download-url",
        "bytes": 1078277,
        "rows": 55552,
        "sha256": "070b957a201f2e76f1ba21cf362430bfac83f4d594de36ba349e16b3acca62fb",
    },
    "adjustment": {
        "file_name": "a_share_adjustment_factors_event_none_all.parquet",
        "download_endpoint": "https://fuyao.aicubes.cn/api/dump/market-dumps/adjustment-factors/download-url",
        "bytes": 298457,
        "rows": 57530,
        "sha256": "39e1f4cdeca0bad9fd3a4617f2c30d48d3031015f8c6d0e9b8c62426589d553a",
    },
}


@dataclass(frozen=True, slots=True)
class FieldMapping:
    """Documented raw field, parser target, conversion, unit, and type family."""

    source: str
    target: str | None
    transform: str
    unit: str
    expected_family: str


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """One documented Parquet schema and the corresponding local parser."""

    dump_id: str
    doc_section: str
    key_columns: tuple[str, str]
    date_column: str
    fields: tuple[FieldMapping, ...]
    parser: Callable[[Path], Sequence[Bar | CorporateAction]]

    @property
    def columns(self) -> tuple[str, ...]:
        """Return documented source columns in their published order."""
        return tuple(field.source for field in self.fields)


DAILY_SPEC = DatasetSpec(
    dump_id="a_share_daily_k_1d_none_10d",
    doc_section="market-dumps: daily-k-10d",
    key_columns=("thscode", "date_ms"),
    date_column="date_ms",
    fields=(
        FieldMapping("thscode", "symbol", "identity; preserve exchange suffix", "ticker", "string"),
        FieldMapping("currency", None, "validate constant CNY", "currency code", "string"),
        FieldMapping("interval", None, "validate constant 1d", "interval", "string"),
        FieldMapping("adjusted", None, "validate constant none", "adjustment mode", "string"),
        FieldMapping(
            "date_ms",
            "trade_date",
            "Unix milliseconds to Asia/Shanghai date",
            "milliseconds",
            "integer",
        ),
        FieldMapping(
            "open_price", "open", "numeric identity; no rescaling", "CNY per share", "numeric"
        ),
        FieldMapping(
            "high_price", "high", "numeric identity; no rescaling", "CNY per share", "numeric"
        ),
        FieldMapping(
            "low_price", "low", "numeric identity; no rescaling", "CNY per share", "numeric"
        ),
        FieldMapping(
            "close_price", "close", "numeric identity; no rescaling", "CNY per share", "numeric"
        ),
        FieldMapping(
            "volume",
            "volume",
            "numeric identity; no shares-to-lots conversion",
            "shares",
            "numeric",
        ),
        FieldMapping("turnover", "amount", "numeric identity; no rescaling", "CNY", "numeric"),
    ),
    parser=read_daily_k_dump,
)

ADJUSTMENT_SPEC = DatasetSpec(
    dump_id="a_share_adjustment_factors_event_none_all",
    doc_section="market-dumps: adjustment-factors",
    key_columns=("thscode", "ex_date_ms"),
    date_column="ex_date_ms",
    fields=(
        FieldMapping("thscode", "symbol", "identity; preserve exchange suffix", "ticker", "string"),
        FieldMapping(
            "ticker",
            None,
            "validate equals thscode without exchange suffix",
            "display ticker",
            "string",
        ),
        FieldMapping(
            "ex_date_ms",
            "ex_date",
            "Unix milliseconds to Asia/Shanghai date",
            "milliseconds",
            "integer",
        ),
        FieldMapping(
            "dividend_per_share",
            "cash_dividend",
            "numeric identity; no rescaling",
            "CNY per share",
            "numeric",
        ),
        FieldMapping(
            "per_share_bonus",
            "stock_dividend",
            "numeric identity; preserve ratio",
            "shares per share ratio",
            "numeric",
        ),
        FieldMapping(
            "allotment_ratio",
            "rights_shares",
            "numeric identity; preserve ratio",
            "shares per share ratio",
            "numeric",
        ),
        FieldMapping(
            "allotment_price",
            "rights_price",
            "numeric identity; no rescaling",
            "CNY per share",
            "numeric",
        ),
        FieldMapping("currency", None, "validate constant CNY", "currency code", "string"),
    ),
    parser=read_adjustment_factors_dump,
)

SPECS = {"daily": DAILY_SPEC, "adjustment": ADJUSTMENT_SPEC}

OFFICIAL_ADJUSTMENT_EXAMPLES: tuple[dict[str, Any], ...] = (
    {
        "thscode": "600519.SH",
        "ex_date_ms": 1766073600000,
        "dividend_per_share": 23.957,
        "per_share_bonus": 0.0,
    },
    {
        "thscode": "600519.SH",
        "ex_date_ms": 1437062400000,
        "dividend_per_share": 4.374,
        "per_share_bonus": 0.1,
    },
)


def _arrow_family(data_type: pa.DataType) -> str:
    if pa.types.is_string(data_type) or pa.types.is_large_string(data_type):
        return "string"
    if pa.types.is_integer(data_type):
        return "integer"
    if pa.types.is_floating(data_type) or pa.types.is_decimal(data_type):
        return "numeric"
    if pa.types.is_boolean(data_type):
        return "boolean"
    if pa.types.is_binary(data_type) or pa.types.is_large_binary(data_type):
        return "binary"
    return "other"


def _json_value(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _file_facts(path: Path) -> dict[str, Any]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "file_name": path.name,
            "bytes": path.stat().st_size,
            "sha256": digest.hexdigest(),
        }
    except Exception as exc:  # the report must not expose exception text
        return {"file_name": path.name, "file_error_type": type(exc).__name__}


def _date_from_ms(value: object) -> date | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).astimezone(SHANGHAI).date()
    except (OverflowError, OSError, ValueError):
        return None


def _validate_special_values(
    spec: DatasetSpec, rows: list[dict[str, Any]], issues: list[dict[str, Any]]
) -> None:
    for row_index, row in enumerate(rows):
        symbol = row.get("thscode")
        match = CODE_RE.fullmatch(symbol) if isinstance(symbol, str) else None
        if match is None:
            issues.append({"code": "invalid_symbol_suffix", "row_index": row_index})
        elif spec is ADJUSTMENT_SPEC:
            ticker = row.get("ticker")
            if ticker != match.group("ticker"):
                issues.append({"code": "ticker_suffix_mismatch", "row_index": row_index})

        if spec is DAILY_SPEC:
            for column, expected in (("currency", "CNY"), ("interval", "1d"), ("adjusted", "none")):
                if row.get(column) != expected:
                    issues.append(
                        {
                            "code": "metadata_value_mismatch",
                            "column": column,
                            "row_index": row_index,
                            "expected": expected,
                            "actual": _json_value(row.get(column)),
                        }
                    )
        elif row.get("currency") != "CNY":
            issues.append(
                {
                    "code": "metadata_value_mismatch",
                    "column": "currency",
                    "row_index": row_index,
                    "expected": "CNY",
                    "actual": _json_value(row.get("currency")),
                }
            )

        stamp = row.get(spec.date_column)
        if _date_from_ms(stamp) is None:
            issues.append({"code": "invalid_millisecond_date", "row_index": row_index})
        elif isinstance(stamp, int) and not isinstance(stamp, bool):
            moment = datetime.fromtimestamp(stamp / 1000.0, tz=timezone.utc).astimezone(SHANGHAI)
            if moment.time().isoformat() != "00:00:00":
                issues.append({"code": "date_not_shanghai_midnight", "row_index": row_index})

        for field in spec.fields:
            if field.expected_family != "numeric":
                continue
            value = row.get(field.source)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                issues.append(
                    {
                        "code": "invalid_numeric_value",
                        "column": field.source,
                        "row_index": row_index,
                    }
                )
            elif not math.isfinite(float(value)):
                issues.append(
                    {
                        "code": "nonfinite_numeric_value",
                        "column": field.source,
                        "row_index": row_index,
                    }
                )


def _row_key(row: dict[str, Any], spec: DatasetSpec) -> tuple[Any, Any] | None:
    first, second = (row.get(name) for name in spec.key_columns)
    if first is None or second is None:
        return None
    return first, second


def _parsed_key(parsed: Bar | CorporateAction, spec: DatasetSpec) -> tuple[Any, Any]:
    if spec is DAILY_SPEC:
        bar = cast("Bar", parsed)
        return bar.symbol, bar.trade_date
    event = cast("CorporateAction", parsed)
    return event.symbol, event.ex_date


def _compare_values(
    spec: DatasetSpec,
    raw_rows_by_model_key: dict[tuple[Any, Any], list[tuple[int, dict[str, Any]]]],
    parsed_rows: Sequence[Bar | CorporateAction],
) -> dict[str, Any]:
    parsed_by_key: dict[tuple[Any, Any], list[tuple[int, Bar | CorporateAction]]] = {}
    duplicate_parsed_keys: Counter[tuple[Any, Any]] = Counter()
    for parsed_row_index, parsed in enumerate(parsed_rows):
        key = _parsed_key(parsed, spec)
        duplicate_parsed_keys[key] += 1
        parsed_by_key.setdefault(key, []).append((parsed_row_index, parsed))

    duplicate_keys = [
        {"key": [_json_value(part) for part in key], "count": count}
        for key, count in duplicate_parsed_keys.items()
        if count > 1
    ]
    raw_keys = set(raw_rows_by_model_key)
    parsed_keys = set(parsed_by_key)
    missing_keys = sorted(raw_keys - parsed_keys, key=lambda key: (str(key[0]), str(key[1])))
    unexpected_keys = sorted(parsed_keys - raw_keys, key=lambda key: (str(key[0]), str(key[1])))
    occurrence_deltas = []
    missing_parsed_occurrences = 0
    unexpected_parsed_occurrences = 0
    for key in sorted(raw_keys | parsed_keys, key=lambda item: (str(item[0]), str(item[1]))):
        raw_count = len(raw_rows_by_model_key.get(key, []))
        parsed_count = len(parsed_by_key.get(key, []))
        missing_count = max(raw_count - parsed_count, 0)
        unexpected_count = max(parsed_count - raw_count, 0)
        if missing_count or unexpected_count:
            missing_parsed_occurrences += missing_count
            unexpected_parsed_occurrences += unexpected_count
            occurrence_deltas.append(
                {
                    "key": [_json_value(part) for part in key],
                    "raw_count": raw_count,
                    "parsed_count": parsed_count,
                    "missing_parsed_count": missing_count,
                    "unexpected_parsed_count": unexpected_count,
                }
            )
    differences: list[dict[str, Any]] = []
    max_absolute_error = 0.0
    max_relative_error = 0.0
    relative_undefined = 0

    for key in sorted(raw_keys & parsed_keys, key=lambda item: (str(item[0]), str(item[1]))):
        raw_occurrences = raw_rows_by_model_key[key]
        parsed_occurrences = parsed_by_key[key]
        for occurrence_index, ((raw_row_index, raw), (parsed_row_index, parsed)) in enumerate(
            zip(raw_occurrences, parsed_occurrences, strict=False)
        ):
            for field in spec.fields:
                if field.target is None:
                    continue
                expected = raw.get(field.source)
                actual = getattr(parsed, field.target)
                expected_after_transform = (
                    _date_from_ms(expected) if field.source == spec.date_column else expected
                )
                mismatch = False
                diff: dict[str, Any] = {
                    "key": [_json_value(key[0]), _json_value(key[1])],
                    "occurrence_index": occurrence_index,
                    "raw_row_index": raw_row_index,
                    "parsed_row_index": parsed_row_index,
                    "source_column": field.source,
                    "parsed_field": field.target,
                    "expected_from_raw": _json_value(expected),
                    "expected_after_transform": _json_value(expected_after_transform),
                    "actual_parsed": _json_value(actual),
                    "unit": field.unit,
                }
                if field.source == spec.date_column:
                    mismatch = _json_value(expected_after_transform) != _json_value(actual)
                elif field.expected_family == "numeric":
                    expected_number = _finite_number(expected)
                    actual_number = _finite_number(actual)
                    if expected_number is None or actual_number is None:
                        mismatch = True
                    else:
                        absolute_error = abs(actual_number - expected_number)
                        if expected_number == 0.0:
                            relative_error = 0.0 if absolute_error == 0.0 else None
                            if relative_error is None:
                                relative_undefined += 1
                        else:
                            relative_error = absolute_error / abs(expected_number)
                            max_relative_error = max(max_relative_error, relative_error)
                        max_absolute_error = max(max_absolute_error, absolute_error)
                        diff["absolute_error"] = absolute_error
                        diff["relative_error"] = relative_error
                        mismatch = absolute_error > RTOL * abs(expected_number)
                else:
                    mismatch = _json_value(expected) != _json_value(actual)
                if mismatch:
                    differences.append(diff)

    return {
        "parsed_rows": len(parsed_rows),
        "raw_row_occurrences": sum(len(rows) for rows in raw_rows_by_model_key.values()),
        "raw_rows_with_unique_model_key": len(raw_rows_by_model_key),
        "duplicate_parsed_keys": duplicate_keys,
        "missing_parsed_key_count": len(missing_keys),
        "missing_parsed_keys": [[_json_value(part) for part in key] for key in missing_keys],
        "unexpected_parsed_key_count": len(unexpected_keys),
        "unexpected_parsed_keys": [[_json_value(part) for part in key] for key in unexpected_keys],
        "missing_parsed_occurrence_count": missing_parsed_occurrences,
        "unexpected_parsed_occurrence_count": unexpected_parsed_occurrences,
        "occurrence_count_deltas": occurrence_deltas,
        "value_diff_count": len(differences),
        "value_diffs": differences,
        "rtol": RTOL,
        "absolute_tolerance": 0.0,
        "max_absolute_error": max_absolute_error,
        "max_relative_error": max_relative_error,
        "relative_error_undefined_for_zero_expected_count": relative_undefined,
    }


def _official_adjustment_examples(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_key = {
        (row.get("thscode"), row.get("ex_date_ms")): row
        for row in rows
        if row.get("thscode") is not None and row.get("ex_date_ms") is not None
    }
    checks: list[dict[str, Any]] = []
    for example in OFFICIAL_ADJUSTMENT_EXAMPLES:
        key = (example["thscode"], example["ex_date_ms"])
        row = by_key.get(key)
        if row is None:
            checks.append({"key": list(key), "status": "NOT_FOUND_IN_PROVIDED_FILE", "diffs": []})
            continue
        diffs = []
        for column in ("dividend_per_share", "per_share_bonus"):
            expected = float(example[column])
            actual = row.get(column)
            actual_number = _finite_number(actual)
            if actual_number is None:
                differs = True
            else:
                differs = abs(actual_number - expected) > RTOL * abs(expected)
            if differs:
                diffs.append(
                    {
                        "column": column,
                        "expected_from_official_doc": expected,
                        "actual_in_provided_file": _json_value(actual),
                    }
                )
        checks.append(
            {
                "key": list(key),
                "status": "MISMATCH" if diffs else "MATCH",
                "checked_columns": ["dividend_per_share", "per_share_bonus"],
                "diffs": diffs,
            }
        )
    return {
        "documentation_url": "https://fuyao.aicubes.cn/docs/api-reference/corporate-actions/",
        "source_snapshot": DOC_SNAPSHOT["sha256"],
        "status": "MISMATCH"
        if any(item["status"] == "MISMATCH" for item in checks)
        else "RECORDED",
        "rows": checks,
        "scope_note": (
            "Official per-ticker API documentation example; partial fields, compared only "
            "when the same key is present in the supplied dump."
        ),
    }


def compare_file(
    path: Path,
    kind: str,
    *,
    parser_override: Callable[[Path], Sequence[Bar | CorporateAction]] | None = None,
) -> dict[str, Any]:
    """Compare one local Parquet file against the project's public parser."""
    spec = SPECS[kind]
    file_facts = _file_facts(path)
    report: dict[str, Any] = {
        "kind": kind,
        "dump_id": spec.dump_id,
        "doc_section": spec.doc_section,
        "file": file_facts,
        "source_provenance": {
            "download_record": OFFICIAL_FILE_RECORD["capture_record"],
            "retrieved_at": OFFICIAL_FILE_RECORD["retrieved_at"],
            "source_endpoint": OFFICIAL_FILE_RECORD[kind]["download_endpoint"],
            "recorded_official_sample": OFFICIAL_FILE_RECORD[kind]["file_name"],
            "recorded_bytes": OFFICIAL_FILE_RECORD[kind]["bytes"],
            "recorded_rows": OFFICIAL_FILE_RECORD[kind]["rows"],
            "recorded_sha256": OFFICIAL_FILE_RECORD[kind]["sha256"],
            "input_sha256_matches_recorded_sample": (
                file_facts.get("sha256") == OFFICIAL_FILE_RECORD[kind]["sha256"]
            ),
            "input_file_name_matches_record": (
                file_facts.get("file_name") == OFFICIAL_FILE_RECORD[kind]["file_name"]
            ),
            "short_lived_presigned_url": "omitted",
        },
        "status": "FAIL",
        "issues": [],
        "mapping": [
            {
                "source_column": field.source,
                "parsed_field": field.target,
                "transform": field.transform,
                "unit": field.unit,
                "expected_type_family": field.expected_family,
            }
            for field in spec.fields
        ],
        "value_compare_kind": ("project parser output vs each row of the supplied parquet"),
        "duplicate_key_alignment": (
            "compare every occurrence per normalized key in original source order; "
            "duplicate keys still fail validation"
        ),
        "official_documentation_values": (
            "partial API examples are recorded separately; no dump-row example is "
            "asserted unless its key is documented"
        ),
    }
    issues = report["issues"]
    try:
        table = pq.read_table(path)
    except Exception as exc:  # do not echo file contents or raw exception text
        issues.append({"code": "parquet_read_error", "exception_type": type(exc).__name__})
        return report

    column_names = tuple(table.column_names)
    expected_columns = spec.columns
    missing_columns = [name for name in expected_columns if name not in column_names]
    extra_columns = [name for name in column_names if name not in expected_columns]
    if missing_columns:
        issues.append({"code": "missing_columns", "columns": missing_columns})
    if extra_columns:
        issues.append({"code": "unexpected_columns", "columns": extra_columns})
    if column_names != expected_columns:
        issues.append(
            {
                "code": "column_order_mismatch",
                "expected": list(expected_columns),
                "actual": list(column_names),
            }
        )

    mappings_by_name = {field.source: field for field in spec.fields}
    schema = []
    null_counts: dict[str, int] = {}
    for column in table.schema:
        field_map = mappings_by_name.get(column.name)
        family = _arrow_family(column.type)
        expected_family = field_map.expected_family if field_map else "unexpected"
        schema.append(
            {
                "name": column.name,
                "arrow_type": str(column.type),
                "arrow_type_family": family,
                "nullable": column.nullable,
                "expected_type_family": expected_family,
            }
        )
        if field_map is not None and family != expected_family:
            issues.append(
                {
                    "code": "wrong_type_family",
                    "column": column.name,
                    "expected": expected_family,
                    "actual": family,
                    "arrow_type": str(column.type),
                }
            )
        null_count = table.column(column.name).null_count
        null_counts[column.name] = null_count
        if null_count:
            issues.append({"code": "null_values", "column": column.name, "count": null_count})

    rows = table.to_pylist()
    _validate_special_values(spec, rows, issues)
    nonnull_keys = [_row_key(row, spec) for row in rows]
    raw_key_counts: Counter[tuple[Any, Any]] = Counter(
        key for key in nonnull_keys if key is not None
    )
    duplicate_raw_keys = [
        {"key": [_json_value(part) for part in key], "count": count}
        for key, count in raw_key_counts.items()
        if count > 1
    ]
    null_key_count = sum(key is None for key in nonnull_keys)
    if null_key_count:
        issues.append({"code": "null_key_rows", "count": null_key_count})
    if duplicate_raw_keys:
        issues.append({"code": "duplicate_raw_keys", "groups": duplicate_raw_keys})

    row_map: dict[tuple[Any, Any], list[tuple[int, dict[str, Any]]]] = {}
    for row_index, row in enumerate(rows):
        key = _row_key(row, spec)
        date_value = _date_from_ms(row.get(spec.date_column))
        if key is None or date_value is None:
            continue
        row_map.setdefault((key[0], date_value), []).append((row_index, row))

    report["raw"] = {
        "row_count": table.num_rows,
        "column_names": list(column_names),
        "schema": schema,
        "null_counts": null_counts,
        "key_columns": list(spec.key_columns),
        "null_key_row_count": null_key_count,
        "duplicate_key_group_count": len(duplicate_raw_keys),
        "duplicate_key_groups": duplicate_raw_keys,
    }
    report["source_provenance"]["input_row_count_matches_record"] = (
        table.num_rows == OFFICIAL_FILE_RECORD[kind]["rows"]
    )

    if spec is ADJUSTMENT_SPEC:
        official_examples = _official_adjustment_examples(rows)
        report["official_api_example_comparison"] = official_examples
        if official_examples["status"] == "MISMATCH":
            issues.append({"code": "official_api_example_mismatch"})
    elif spec is DAILY_SPEC:
        report["official_api_example_comparison"] = {
            "status": "NOT_COMPARABLE",
            "documentation_url": "https://fuyao.aicubes.cn/docs/api-reference/prices/",
            "reason": (
                "The documented daily API example requests adjust=forward; the supplied "
                "dump is adjusted=none."
            ),
        }

    try:
        parsed = (parser_override or spec.parser)(path)
    except Exception as exc:  # report only the type; parser messages can contain paths/data
        issues.append({"code": "project_parser_error", "exception_type": type(exc).__name__})
        report["comparison"] = {"parsed_rows": None, "value_diff_count": None, "value_diffs": []}
        return report

    comparison = _compare_values(spec, row_map, parsed)
    report["comparison"] = comparison
    if comparison["duplicate_parsed_keys"]:
        issues.append({"code": "duplicate_parsed_keys"})
    if comparison["missing_parsed_key_count"]:
        issues.append({"code": "missing_parsed_keys"})
    if comparison["unexpected_parsed_key_count"]:
        issues.append({"code": "unexpected_parsed_keys"})
    if comparison["missing_parsed_occurrence_count"]:
        issues.append({"code": "missing_parsed_occurrences"})
    if comparison["unexpected_parsed_occurrence_count"]:
        issues.append({"code": "unexpected_parsed_occurrences"})
    if comparison["value_diff_count"]:
        issues.append({"code": "parsed_value_differences"})
    report["status"] = "PASS" if not issues else "FAIL"
    return report


def build_report(daily_path: Path | None, adjustment_path: Path | None) -> dict[str, Any]:
    """Build the sanitized report for whichever local dump paths were supplied."""
    files: list[dict[str, Any]] = []
    if daily_path is not None:
        files.append(compare_file(daily_path, "daily"))
    if adjustment_path is not None:
        files.append(compare_file(adjustment_path, "adjustment"))
    report = {
        "schema_version": 1,
        "sources": {
            "market_dump_documentation": {
                **DOC_SNAPSHOT,
                "section_url": "https://fuyao.aicubes.cn/docs/api-reference/market-dumps/",
                "facts": [
                    "daily key (thscode, date_ms); adjustment key (thscode, ex_date_ms)",
                    "timestamps are Unix milliseconds interpreted in Asia/Shanghai",
                    "daily volume is shares; prices and turnover are CNY",
                    "adjustment amounts and ratios are per-share values, with no "
                    "percent conversion",
                ],
            },
            "official_file_download_record": "docs/evidence/C65/official-parquet-download.json",
        },
        "files": files,
        "status": "PASS" if files and all(item["status"] == "PASS" for item in files) else "FAIL",
    }
    return report


def main(argv: list[str] | None = None) -> int:
    """Run the offline comparator CLI and optionally save an explicit report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily-parquet", type=Path)
    parser.add_argument("--adjustment-parquet", type=Path)
    parser.add_argument("--out", type=Path, help="optional explicit JSON report path")
    args = parser.parse_args(argv)
    if args.daily_parquet is None and args.adjustment_parquet is None:
        parser.error("supply --daily-parquet and/or --adjustment-parquet")

    report = build_report(args.daily_parquet, args.adjustment_parquet)
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.out is None:
        sys.stdout.write(serialized)
    else:
        try:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(serialized, encoding="utf-8")
        except Exception as exc:  # do not echo output paths or raw exception data
            sys.stderr.write(
                json.dumps({"status": "ERROR", "exception_type": type(exc).__name__}) + "\n"
            )
            return 2
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
