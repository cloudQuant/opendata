#!/usr/bin/env python3
"""Independently validate the C65 ODS write benchmark evidence bundle.

This module only reads the benchmark source, matrix, driver transcript, and raw
JSON Lines records. It never imports the benchmark runner or connects to a
database, so a malformed or incomplete evidence bundle fails closed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn

EXPECTED_SOURCE_SHA256 = "4a47f4dcc746ad57d5c36ce379aa6816d337aa275e32f3b88eca932ff6107063"
EXPECTED_SOURCE_ROWS = 10_310_289
PAGE_SIZE = 50_000
TWO_GIB_BYTES = 2 * 1024**3


@dataclass(frozen=True)
class RunPlan:
    """The fixed C65 scales and the evidence file expected for each scale."""

    size: str
    requested_rows: int | None
    limit_rows: int | None
    evidence: str

    @property
    def is_full(self) -> bool:
        """Return whether this run must scan and write the whole source."""
        return self.size == "full"


RUN_PLANS = (
    RunPlan(
        size="100000",
        requested_rows=100_000,
        limit_rows=100_000,
        evidence="docs/evidence/C65/write-benchmark-100000.jsonl",
    ),
    RunPlan(
        size="1000000",
        requested_rows=1_000_000,
        limit_rows=1_000_000,
        evidence="docs/evidence/C65/write-benchmark-1000000.jsonl",
    ),
    RunPlan(
        size="full",
        requested_rows=None,
        limit_rows=None,
        evidence="docs/evidence/C65/write-benchmark-full.jsonl",
    ),
)


@dataclass(frozen=True)
class Issue:
    """A fail-closed finding from one evidence surface."""

    code: str
    message: str
    scale: str | None = None


@dataclass
class ValidationResult:
    """Measured benchmark facts and independent validation findings."""

    facts: dict[str, Any]
    issues: list[Issue]

    @property
    def valid(self) -> bool:
        """Whether all required evidence checks passed."""
        return not self.issues


def _reject_nonfinite(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number {value}")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_nonfinite)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ValueError("JSONL evidence is empty")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ValueError(f"blank JSONL line at {line_number}")
        record = json.loads(line, parse_constant=_reject_nonfinite)
        if not isinstance(record, dict):
            raise ValueError(f"JSONL line {line_number} is not an object")
        records.append(record)
    return records


def _is_int(value: object, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _is_positive_finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _add_issue(issues: list[Issue], code: str, message: str, scale: str | None = None) -> None:
    issues.append(Issue(code=code, message=message, scale=scale))


def _load_required_json(path: Path, issues: list[Issue], code: str) -> object | None:
    try:
        return _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        _add_issue(issues, code, f"cannot read strict JSON evidence: {type(exc).__name__}")
        return None


#: The only edits the live benchmark source is allowed to carry on top of the frozen C65 copy.
#: Each pair is byte-exact and must match exactly once, and applying all of them in order must
#: reproduce the live file, so this register is a whitelist of differences rather than a category
#: of tolerance (comments included). A new legitimate edit has to be itemized here first, which
#: makes re-basing the C65 evidence an auditable human act, not something a drifted working tree
#: can do by itself. The records' own ``source_sha256`` still binds to the untouched frozen copy.
DOCUMENTED_SOURCE_CORRECTIONS: Final = (
    (
        "C65b 初始进度记录的状态由 error 改为 running",
        b'        "status": "error",\n',
        b'        "status": "running",\n',
    ),
    (
        "C81 bandit 例外收口成行级 nosec：subprocess 导入行补 reason",
        b"import subprocess  # nosec B404\n",
        b"import subprocess  # nosec B404  # sole subprocess.run: ps from (/bin/ps,/usr/bin/ps)"
        b" tuple; literal argv\n",
    ),
    (
        "C81 bandit 例外收口成行级 nosec：COUNT(*) 行",
        b'        text(f"SELECT COUNT(*) FROM {_quote_identifier(table)}")  # noqa: S608\n',
        b'        text(f"SELECT COUNT(*) FROM {_quote_identifier(table)}")  # noqa: S608'
        b"  # nosec B608  # table from registry, _quote_identifier\n",
    ),
    (
        "C81 bandit 例外收口成行级 nosec：取数语句行",
        b'    statement = f"SELECT {selected} FROM {_quote_identifier(table)}'
        b' ORDER BY {order}"  # noqa: S608\n',
        b'    statement = f"SELECT {selected} FROM {_quote_identifier(table)}'
        b' ORDER BY {order}"  # noqa: S608  # nosec B608'
        b"  # _quote_identifier on reflected cols/key\n",
    ),
)


def _apply_documented_corrections(frozen: bytes) -> tuple[bytes | None, list[str]]:
    """Rewrite the frozen copy through the register; report the pairs that did not match once."""
    repaired = frozen
    unmatched: list[str] = []
    for label, before, after in DOCUMENTED_SOURCE_CORRECTIONS:
        if repaired.count(before) != 1:
            unmatched.append(label)
            continue
        repaired = repaired.replace(before, after, 1)
    return (None if unmatched else repaired), unmatched


def _source_facts(root: Path, issues: list[Issue]) -> dict[str, Any]:
    frozen_path = root / "docs/evidence/C65/write-benchmark-source-frozen.txt"
    current_path = root / "scripts/ops/benchmark_ods_write.py"
    facts: dict[str, Any] = {
        "expected_sha256": EXPECTED_SOURCE_SHA256,
        "frozen_sha256": None,
        "current_sha256": None,
        "current_matches_frozen": False,
        "allowed_initial_state_status_correction": False,
        "documented_corrections_unmatched": "-",
        "documented_corrections_unmatched_count": 0,
        "documented_corrections_reproduce_current": False,
        "source_identity_verified": False,
    }
    try:
        frozen = frozen_path.read_bytes()
    except OSError as exc:
        _add_issue(issues, "source-frozen-unreadable", type(exc).__name__)
        return facts
    frozen_sha256 = hashlib.sha256(frozen).hexdigest()
    facts["frozen_sha256"] = frozen_sha256
    if frozen_sha256 != EXPECTED_SOURCE_SHA256:
        _add_issue(issues, "source-frozen-sha-mismatch", "frozen source SHA256 differs")

    try:
        current = current_path.read_bytes()
    except OSError as exc:
        _add_issue(issues, "source-current-unreadable", type(exc).__name__)
        return facts
    current_sha256 = hashlib.sha256(current).hexdigest()
    facts["current_sha256"] = current_sha256
    facts["current_matches_frozen"] = current == frozen

    corrected, unmatched = _apply_documented_corrections(frozen)
    corrections_hold = corrected is not None and current == corrected
    facts["documented_corrections_unmatched"] = ",".join(unmatched) or "-"
    facts["documented_corrections_unmatched_count"] = len(unmatched)
    facts["documented_corrections_reproduce_current"] = corrections_hold
    facts["allowed_initial_state_status_correction"] = corrections_hold
    facts["source_identity_verified"] = frozen_sha256 == EXPECTED_SOURCE_SHA256 and (
        current == frozen or corrections_hold
    )
    if current != frozen and not corrections_hold:
        _add_issue(
            issues,
            "source-current-drift",
            "current benchmark source differs beyond the documented correction register",
        )
    return facts


def _validate_record_fields(
    record: dict[str, Any], plan: RunPlan, expected_sha256: str, issues: list[Issue]
) -> None:
    scale = plan.size
    fixed_values: dict[str, Any] = {
        "domain": "stock_daily",
        "source": "ths",
        "table": "ods_stock_daily_ths",
        "business_key": ["thscode", "trade_date"],
        "page_size": PAGE_SIZE,
        "limit_rows": plan.limit_rows,
        "mode": "apply",
        "source_sha256": expected_sha256,
        "current_rss_source": "ps",
        "peak_rss_source": "resource.getrusage",
    }
    for key, expected in fixed_values.items():
        if record.get(key) != expected:
            _add_issue(
                issues,
                f"raw-{key}-mismatch",
                f"raw {record.get('record', 'unknown')} field {key} differs from C65 plan",
                scale,
            )
    if "error_type" in record:
        _add_issue(
            issues,
            "raw-error-type-present",
            f"raw {record.get('record', 'unknown')} record contains error_type",
            scale,
        )
    if record.get("byte_unit") != "bytes" or record.get("rss_unit") != "bytes":
        _add_issue(
            issues,
            "raw-rss-unit-mismatch",
            "raw byte_unit and rss_unit must both be bytes",
            scale,
        )
    for key in ("elapsed_seconds", "current_rss_bytes", "peak_rss_bytes"):
        value = record.get(key)
        if not _is_positive_finite_number(value):
            _add_issue(
                issues,
                f"raw-{key}-invalid",
                f"raw {record.get('record', 'unknown')} field {key} must be finite and positive",
                scale,
            )
        elif key != "elapsed_seconds" and not _is_int(value, minimum=1):
            _add_issue(
                issues,
                f"raw-{key}-invalid",
                f"raw {record.get('record', 'unknown')} field {key} must be positive integer bytes",
                scale,
            )
    for key in (
        "rows_requested",
        "rows_read",
        "rows_written",
        "pages_read",
        "pages_written",
    ):
        if not _is_int(record.get(key)):
            _add_issue(
                issues,
                f"raw-{key}-invalid",
                f"raw {record.get('record', 'unknown')} field {key} must be a non-negative integer",
                scale,
            )
    for key in ("source_rows_exact", "target_rows_before"):
        if not _is_int(record.get(key), minimum=1 if key == "source_rows_exact" else 0):
            _add_issue(
                issues,
                f"raw-{key}-invalid",
                f"raw {record.get('record', 'unknown')} field {key} must be an integer",
                scale,
            )


def _validate_raw_run(
    root: Path,
    plan: RunPlan,
    expected_sha256: str,
    source_identity_verified: bool,
    issues: list[Issue],
) -> tuple[dict[str, Any], bool]:
    path = root / plan.evidence
    run_issue_start = len(issues)
    try:
        records = _read_jsonl(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        _add_issue(
            issues,
            "raw-jsonl-unreadable",
            f"cannot read raw JSONL: {type(exc).__name__}",
            plan.size,
        )
        return {"raw_path": plan.evidence, "raw_final": None}, False

    record_names = [record.get("record") for record in records]
    if record_names[0] != "initial":
        _add_issue(issues, "raw-initial-position", "first raw record must be initial", plan.size)
    if record_names.count("initial") != 1:
        _add_issue(
            issues, "raw-initial-count", "raw JSONL must have exactly one initial record", plan.size
        )
    if record_names.count("final") != 1:
        _add_issue(
            issues, "raw-final-count", "raw JSONL must have exactly one final record", plan.size
        )
    if record_names[-1] != "final":
        _add_issue(
            issues, "raw-final-eof", "final record must be the last raw JSONL record", plan.size
        )
    if any(name not in {"initial", "batch", "final"} for name in record_names):
        _add_issue(
            issues, "raw-record-kind", "raw JSONL contains an unknown record kind", plan.size
        )

    initial = records[0] if record_names[0] == "initial" else {}
    finals = [record for record in records if record.get("record") == "final"]
    final = finals[0] if len(finals) == 1 else None
    requested = plan.requested_rows or EXPECTED_SOURCE_ROWS
    expected_pages = math.ceil(requested / PAGE_SIZE)
    expected_kinds = ["initial", *("batch" for _ in range(expected_pages)), "final"]
    if record_names != expected_kinds:
        _add_issue(
            issues,
            "raw-batch-sequence",
            f"expected initial, {expected_pages} ordered batches, and final at EOF",
            plan.size,
        )

    elapsed_values: list[float] = []
    for record in records:
        _validate_record_fields(record, plan, expected_sha256, issues)
        value = record.get("elapsed_seconds")
        if isinstance(value, (int, float)) and _is_positive_finite_number(value):
            elapsed_values.append(float(value))
    if len(elapsed_values) == len(records) and any(
        later < earlier for earlier, later in zip(elapsed_values, elapsed_values[1:], strict=False)
    ):
        _add_issue(
            issues, "raw-elapsed-regressed", "elapsed_seconds regressed between records", plan.size
        )

    progress_status_errors = 0
    for index, record in enumerate(records):
        kind = record.get("record")
        status = record.get("status")
        allowed = {"running", "error"} if kind in {"initial", "batch"} else set()
        if kind == "final":
            allowed = {"complete"} if plan.is_full else {"limited-complete"}
        if status not in allowed:
            _add_issue(
                issues,
                "raw-status-invalid",
                f"raw {kind} record has an unexpected terminal/progress status",
                plan.size,
            )
        elif status == "error" and kind in {"initial", "batch"}:
            progress_status_errors += 1
        if index == 0:
            if any(
                record.get(key) != value
                for key, value in {
                    "rows_read": 0,
                    "rows_written": 0,
                    "pages_read": 0,
                    "pages_written": 0,
                    "target_rows_before": 0,
                    "target_rows_after": None,
                    "source_scan_complete": False,
                    "full_source_write_verified": False,
                }.items()
            ):
                _add_issue(
                    issues, "raw-initial-state", "initial counters or flags are not zero", plan.size
                )
        elif kind == "batch":
            expected_page = index
            expected_cumulative = min(expected_page * PAGE_SIZE, requested)
            expected_page_rows = expected_cumulative - min(
                (expected_page - 1) * PAGE_SIZE, requested
            )
            if any(
                record.get(key) != value
                for key, value in {
                    "page_number": expected_page,
                    "page_rows": expected_page_rows,
                    "rows_read": expected_cumulative,
                    "rows_written": expected_cumulative,
                    "pages_read": expected_page,
                    "pages_written": expected_page,
                    "rows_requested": requested,
                    "target_rows_before": 0,
                    "target_rows_after": None,
                    "source_scan_complete": False,
                    "full_source_write_verified": False,
                }.items()
            ):
                _add_issue(
                    issues,
                    "raw-batch-counter-sequence",
                    f"batch {expected_page} counters do not match the complete page sequence",
                    plan.size,
                )

    for record in records:
        if record.get("rows_requested") != requested:
            _add_issue(
                issues,
                "raw-requested-count-mismatch",
                "rows_requested differs from scale",
                plan.size,
            )
        if record.get("source_rows_exact") != EXPECTED_SOURCE_ROWS:
            _add_issue(
                issues,
                "raw-source-count-mismatch",
                f"source snapshot count must be {EXPECTED_SOURCE_ROWS}",
                plan.size,
            )

    if final is not None:
        final_expected = {
            "rows_read": requested,
            "rows_written": requested,
            "pages_read": expected_pages,
            "pages_written": expected_pages,
            "rows_requested": requested,
            "source_rows_exact": EXPECTED_SOURCE_ROWS,
            "target_rows_before": 0,
            "target_rows_after": requested,
            "source_scan_complete": plan.is_full,
            "full_source_write_verified": plan.is_full,
            "status": "complete" if plan.is_full else "limited-complete",
        }
        for key, expected in final_expected.items():
            if final.get(key) != expected:
                _add_issue(
                    issues,
                    "raw-final-counter-mismatch",
                    f"final field {key} differs from the complete requested scope",
                    plan.size,
                )
        peaks = [record.get("peak_rss_bytes") for record in records]
        peak_values = [
            peak
            for peak in peaks
            if isinstance(peak, int) and not isinstance(peak, bool) and peak > 0
        ]
        if len(peak_values) == len(peaks) and final.get("peak_rss_bytes") != max(peak_values):
            _add_issue(
                issues,
                "raw-final-peak-mismatch",
                "final peak RSS must equal the maximum observed raw record peak",
                plan.size,
            )

    raw_complete = len(issues) == run_issue_start
    if progress_status_errors:
        if (
            source_identity_verified
            and raw_complete
            and final is not None
            and "error_type" not in final
        ):
            historical_progress_annotation = True
        else:
            historical_progress_annotation = False
            _add_issue(
                issues,
                "raw-historical-status-unproven",
                "error progress status lacks a complete frozen-source success proof",
                plan.size,
            )
            raw_complete = False
    else:
        historical_progress_annotation = False

    summary: dict[str, Any] = {
        "raw_path": plan.evidence,
        "raw_record_count": len(records),
        "raw_initial_identity": initial.get("source_sha256"),
        "raw_final": final,
        "raw_final_identity": final.get("source_sha256") if final else None,
        "source_rows_exact": final.get("source_rows_exact") if final else None,
        "rows_requested": final.get("rows_requested") if final else None,
        "rows_read": final.get("rows_read") if final else None,
        "rows_written": final.get("rows_written") if final else None,
        "pages_read": final.get("pages_read") if final else None,
        "pages_written": final.get("pages_written") if final else None,
        "elapsed_seconds": final.get("elapsed_seconds") if final else None,
        "peak_rss_bytes": final.get("peak_rss_bytes") if final else None,
        "target_rows_before": final.get("target_rows_before") if final else None,
        "target_rows_after": final.get("target_rows_after") if final else None,
        "status": final.get("status") if final else None,
        "historical_progress_status_error_count": progress_status_errors,
        "historical_progress_annotation": historical_progress_annotation,
        "raw_complete": raw_complete,
    }
    return summary, raw_complete


def _expected_command(command: object, plan: RunPlan) -> bool:
    if not isinstance(command, str):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", os.path.basename(words[0])):
        return False
    arguments = [
        "scripts/ops/benchmark_ods_write.py",
        "--source",
        "ths",
        "--page-size",
        str(PAGE_SIZE),
        "--apply",
    ]
    if plan.limit_rows is not None:
        arguments.extend(["--limit-rows", str(plan.limit_rows)])
    return words[1:] == arguments


def _parse_driver(
    path: Path, issues: list[Issue]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    starts: dict[str, dict[str, Any]] = {}
    finishes: dict[str, dict[str, Any]] = {}
    facts: dict[str, Any] = {"round": None, "exit_code": None, "scale_count": 0}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        _add_issue(issues, "driver-unreadable", type(exc).__name__)
        return starts, finishes, facts

    facts["round"] = next(
        (line.split("=", 1)[1] for line in lines if line.startswith("ROUND=")), None
    )
    facts["start_order"] = []
    facts["finish_order"] = []
    if facts["round"] != "C65":
        _add_issue(issues, "driver-round", "driver ROUND must be C65")

    start_pattern = re.compile(r"^BENCHMARK_START size=(\S+) schema=(\S+) command=(.+)$")
    for line in lines:
        if line.startswith("BENCHMARK_START "):
            match = start_pattern.fullmatch(line)
            if match is None:
                _add_issue(issues, "driver-start-malformed", "driver start record is malformed")
                continue
            size, schema, command = match.groups()
            facts["start_order"].append(size)
            if size in starts:
                _add_issue(
                    issues, "driver-start-duplicate", f"duplicate driver start for {size}", size
                )
            starts[size] = {"schema": schema, "command": command}
        elif line.startswith("BENCHMARK_FINISH "):
            payload = line.removeprefix("BENCHMARK_FINISH ")
            try:
                record = json.loads(payload, parse_constant=_reject_nonfinite)
            except (json.JSONDecodeError, ValueError) as exc:
                _add_issue(
                    issues,
                    "driver-finish-malformed",
                    f"driver finish is invalid JSON: {type(exc).__name__}",
                )
                continue
            if not isinstance(record, dict) or not isinstance(record.get("size"), str):
                _add_issue(
                    issues, "driver-finish-shape", "driver finish must be an object with a size"
                )
                continue
            size = record["size"]
            facts["finish_order"].append(size)
            if size in finishes:
                _add_issue(
                    issues, "driver-finish-duplicate", f"duplicate driver finish for {size}", size
                )
            finishes[size] = record

    facts["scale_count"] = len(finishes)
    exit_lines = [line for line in lines if line.startswith("EXIT=")]
    if len(exit_lines) != 1 or not lines or lines[-1] != "EXIT=0":
        _add_issue(issues, "driver-exit", "driver must end with exactly one EXIT=0")
    else:
        facts["exit_code"] = 0
    return starts, finishes, facts


def validate(root: Path) -> ValidationResult:
    """Read and independently validate the fixed C65 benchmark evidence bundle.

    ``root`` is the repository root. The returned facts preserve measurements
    even when another evidence surface has a finding; malformed input becomes
    an issue instead of escaping this read-only validator.
    """
    issues: list[Issue] = []
    facts: dict[str, Any] = {
        "round": "C65",
        "scale_count": len(RUN_PLANS),
        "source": {},
        "matrix": {},
        "driver": {},
        "scales": {},
        "memory_observation": {},
    }
    try:
        repository_root = Path(root)
        facts["source"] = _source_facts(repository_root, issues)
        frozen_sha256 = facts["source"].get("frozen_sha256")
        expected_sha256 = EXPECTED_SOURCE_SHA256
        source_identity_verified = facts["source"].get("source_identity_verified") is True

        matrix_path = repository_root / "docs/evidence/C65/write-benchmark-matrix.json"
        matrix = _load_required_json(matrix_path, issues, "matrix-unreadable")
        if not isinstance(matrix, dict):
            _add_issue(issues, "matrix-shape", "matrix must be a JSON object")
            matrix = {}
        facts["matrix"] = {
            "round": matrix.get("round"),
            "run_count": len(matrix.get("runs", []))
            if isinstance(matrix.get("runs"), list)
            else None,
            "frozen_source_sha256": frozen_sha256,
        }
        if matrix.get("round") != "C65":
            _add_issue(issues, "matrix-round", "matrix round must be C65")
        matrix_runs_value = matrix.get("runs")
        matrix_runs: dict[str, dict[str, Any]] = {}
        if not isinstance(matrix_runs_value, list):
            _add_issue(issues, "matrix-runs-shape", "matrix runs must be a list")
        else:
            for run in matrix_runs_value:
                if not isinstance(run, dict) or not isinstance(run.get("size"), str):
                    _add_issue(
                        issues, "matrix-run-shape", "every matrix run must have a string size"
                    )
                    continue
                size = run["size"]
                if size in matrix_runs:
                    _add_issue(
                        issues, "matrix-run-duplicate", f"duplicate matrix run for {size}", size
                    )
                matrix_runs[size] = run
            if len(matrix_runs_value) != len(RUN_PLANS):
                _add_issue(
                    issues, "matrix-run-count", "matrix must contain exactly three scale runs"
                )
        created_schemas = matrix.get("created_schemas")
        if (
            not isinstance(created_schemas, list)
            or len(created_schemas) != len(RUN_PLANS)
            or any(not isinstance(schema, str) for schema in created_schemas)
            or len(set(created_schemas)) != len(RUN_PLANS)
        ):
            _add_issue(
                issues,
                "matrix-created-schemas",
                "matrix must name three unique created benchmark schemas",
            )
        elif set(created_schemas) != {
            run.get("schema") for run in matrix_runs.values() if isinstance(run.get("schema"), str)
        }:
            _add_issue(
                issues,
                "matrix-created-schema-binding",
                "created schema list differs from the per-scale matrix schemas",
            )

        driver_path = repository_root / "docs/evidence/C65/write-benchmark-matrix-driver.txt"
        driver_starts, driver_finishes, driver_facts = _parse_driver(driver_path, issues)
        facts["driver"] = driver_facts
        if set(driver_starts) != {plan.size for plan in RUN_PLANS}:
            _add_issue(
                issues,
                "driver-start-scales",
                "driver starts must cover exactly the three C65 scales",
            )
        if set(driver_finishes) != {plan.size for plan in RUN_PLANS}:
            _add_issue(
                issues,
                "driver-finish-scales",
                "driver finishes must cover exactly the three C65 scales",
            )
        expected_scale_order = [plan.size for plan in RUN_PLANS]
        if driver_facts.get("start_order") != expected_scale_order:
            _add_issue(issues, "driver-start-order", "driver starts are out of C65 scale order")
        if driver_facts.get("finish_order") != expected_scale_order:
            _add_issue(issues, "driver-finish-order", "driver finishes are out of C65 scale order")

        raw_summaries: dict[str, dict[str, Any]] = {}
        for plan in RUN_PLANS:
            summary, _ = _validate_raw_run(
                repository_root,
                plan,
                expected_sha256,
                source_identity_verified,
                issues,
            )
            raw_summaries[plan.size] = summary

        for plan in RUN_PLANS:
            size = plan.size
            matrix_run = matrix_runs.get(size)
            driver_start = driver_starts.get(size)
            driver_finish = driver_finishes.get(size)
            summary = raw_summaries[size]
            if matrix_run is None:
                _add_issue(issues, "matrix-run-missing", "matrix run is missing", size)
                matrix_run = {}
            if matrix_run.get("evidence") != plan.evidence:
                _add_issue(
                    issues,
                    "matrix-evidence-path",
                    "matrix evidence path differs from expected raw JSONL",
                    size,
                )
            if matrix_run.get("size") != size:
                _add_issue(issues, "matrix-scale-identity", "matrix size identity differs", size)
            if not _is_int(matrix_run.get("exit"), minimum=0) or matrix_run.get("exit") != 0:
                _add_issue(issues, "matrix-exit", "matrix subprocess exit must be 0", size)
            driver_elapsed = matrix_run.get("elapsed_driver_seconds")
            if not _is_positive_finite_number(driver_elapsed):
                _add_issue(
                    issues,
                    "matrix-driver-elapsed",
                    "matrix driver elapsed must be finite and positive",
                    size,
                )

            command = matrix_run.get("command")
            if not _expected_command(command, plan):
                _add_issue(
                    issues,
                    "matrix-command",
                    "matrix command does not name the expected apply scale",
                    size,
                )
            schema = matrix_run.get("schema")
            if not isinstance(schema, str) or not schema.startswith("opendata_c65_bench_"):
                _add_issue(
                    issues,
                    "matrix-schema",
                    "matrix schema must use the isolated C65 benchmark prefix",
                    size,
                )

            matrix_final = matrix_run.get("final")
            raw_final = summary.get("raw_final")
            matrix_final_identity = isinstance(matrix_final, dict) and matrix_final == raw_final
            if not matrix_final_identity:
                _add_issue(
                    issues,
                    "matrix-final-identity",
                    "matrix final body differs from the raw final record",
                    size,
                )
            if (
                isinstance(matrix_final, dict)
                and matrix_final.get("source_sha256") != EXPECTED_SOURCE_SHA256
            ):
                _add_issue(
                    issues,
                    "matrix-source-sha",
                    "matrix final source SHA256 differs from frozen source",
                    size,
                )

            if driver_start is None:
                _add_issue(issues, "driver-start-missing", "driver start is missing", size)
            else:
                if driver_start.get("command") != command or driver_start.get("schema") != schema:
                    _add_issue(
                        issues,
                        "driver-start-identity",
                        "driver start command/schema differs from matrix",
                        size,
                    )
                if not _expected_command(driver_start.get("command"), plan):
                    _add_issue(
                        issues,
                        "driver-start-command",
                        "driver start command does not match expected scale",
                        size,
                    )

            driver_finish_identity = False
            if driver_finish is None:
                _add_issue(issues, "driver-finish-missing", "driver finish is missing", size)
            else:
                for key, expected in {
                    "schema": schema,
                    "command": command,
                    "evidence": plan.evidence,
                    "exit": 0,
                }.items():
                    if driver_finish.get(key) != expected:
                        _add_issue(
                            issues,
                            f"driver-finish-{key}",
                            f"driver finish {key} differs from matrix/plan",
                            size,
                        )
                finish_elapsed = driver_finish.get("elapsed_driver_seconds")
                if (
                    not _is_positive_finite_number(finish_elapsed)
                    or finish_elapsed != driver_elapsed
                ):
                    _add_issue(
                        issues,
                        "driver-finish-elapsed",
                        "driver finish elapsed differs from matrix",
                        size,
                    )
                finish_final = driver_finish.get("final")
                driver_finish_identity = (
                    isinstance(finish_final, dict) and finish_final == raw_final
                )
                if not driver_finish_identity:
                    _add_issue(
                        issues,
                        "driver-final-identity",
                        "driver final body differs from raw final record",
                        size,
                    )

            summary["matrix_exit"] = matrix_run.get("exit")
            summary["driver_exit"] = driver_finish.get("exit") if driver_finish else None
            summary["matrix_final_identity"] = matrix_final_identity
            summary["driver_final_identity"] = driver_finish_identity
            summary["body_final_identity"] = matrix_final_identity and driver_finish_identity
            summary["source_sha256_bound"] = (
                summary.get("raw_initial_identity") == EXPECTED_SOURCE_SHA256
                and summary.get("raw_final_identity") == EXPECTED_SOURCE_SHA256
                and isinstance(matrix_final, dict)
                and matrix_final.get("source_sha256") == EXPECTED_SOURCE_SHA256
            )
            if not summary["source_sha256_bound"]:
                _add_issue(
                    issues,
                    "scale-source-sha-binding",
                    "raw and matrix source identity must bind to frozen SHA",
                    size,
                )
            summary["driver_elapsed_seconds"] = driver_elapsed
            summary["matrix_schema"] = schema
            facts["scales"][size] = summary

        scale_facts = facts["scales"]
        peaks = {
            size: scale_facts[size].get("peak_rss_bytes")
            for size in (plan.size for plan in RUN_PLANS)
        }
        under_2_gib = {
            size: _is_int(peak, minimum=1) and peak < TWO_GIB_BYTES for size, peak in peaks.items()
        }
        full_le_1m = (
            _is_int(peaks.get("full"), minimum=1)
            and _is_int(peaks.get("1000000"), minimum=1)
            and peaks["full"] <= peaks["1000000"]
        )
        facts["memory_observation"] = {
            "peak_rss_bytes_by_scale": peaks,
            "all_scales_below_2_gib": all(under_2_gib.values()),
            "full_peak_le_1000000_peak": full_le_1m,
            "observation_scope": "the three C65 runs at page_size 50000 only",
        }
    except Exception as exc:  # Fail closed even on malformed nested evidence values.
        _add_issue(issues, "validator-exception", f"validation stopped on {type(exc).__name__}")
    facts["valid"] = not issues
    facts["issue_count"] = len(issues)
    return ValidationResult(facts=facts, issues=issues)
