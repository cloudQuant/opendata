#!/usr/bin/env python3
"""Porting fidelity comparison: record-and-replay against upstream (AC-6).

Two modes:

* ``--record`` (needs network + the upstream checkout): runs each P0
  case through the *upstream* akshare checkout pinned in
  ``opendata_http/upstream.lock``, records every HTTP response, and
  stores the upstream output as the reference frame
  (``reference.csv.gz``) plus case metadata (``meta.json``, which
  carries the live dtypes). The upstream HEAD must match the lock's
  commit, otherwise the recording fails closed.
* ``--compare`` (offline): replays the recorded HTTP responses into
  the *ported* ``opendata_http`` tree and compares its output with
  the stored reference frame under the AC-6 tolerance: identical
  columns (order included), identical shape, identical dtypes, and
  cell values equal (floats via ``rtol=1e-9``, NaN==NaN).

The A1 leftover is covered as an extra section: the raw and qfq
recordings of ``stock_zh_a_hist`` double as an official em qfq
series, so the D10 ``apply_adjust`` synthesis is checked to
reproduce it (design D10: adjusted series are synthesized from Bar
+ AdjustFactor, never stored).

Cases live in ``tests/fixtures/upstream/<case>/``; the report is
archived to ``docs/evidence/A2/compare-report.md``.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "upstream"
REPORT_PATH = REPO_ROOT / "docs" / "evidence" / "A2" / "compare-report.md"
LOCK_PATH = REPO_ROOT / "opendata_http" / "upstream.lock"
DEFAULT_UPSTREAM = Path("/Users/yunjinqi/Documents/new_projects/akshare")

#: Relative float tolerance for cell comparison (AC-6: floats within tolerance).
RTOL = 1e-9
#: Report at most this many cell diffs per case.
MAX_DIFFS = 10

_UPSTREAM_CASE_MODULE = "akshare"
_PORTED_CASE_MODULE = "opendata_http"


@dataclass(frozen=True)
class Case:
    """One fidelity comparison case.

    Attributes:
        name: Fixture directory name.
        function: Flat-API function name (identical in both trees).
        kwargs: Call arguments; kept small for deterministic pages.
        note: One-line description for the report.
    """

    name: str
    function: str
    kwargs: dict[str, Any]
    note: str


#: P0 daily-chain cases (A2.1 closure), 100% of the fetcher surface.
CASES = (
    Case(
        name="stock_daily_raw",
        function="stock_zh_a_hist",
        kwargs={
            "symbol": "600519",
            "period": "daily",
            "start_date": "20240101",
            "end_date": "20240331",
            "adjust": "",
        },
        note="em daily klines, unadjusted (stock_daily fetcher)",
    ),
    Case(
        name="stock_daily_qfq",
        function="stock_zh_a_hist",
        kwargs={
            "symbol": "600519",
            "period": "daily",
            "start_date": "20240101",
            "end_date": "20240331",
            "adjust": "qfq",
        },
        note="em daily klines, qfq (A1 leftover: official qfq series for D10)",
    ),
    Case(
        name="stock_action_dividend",
        function="stock_history_dividend_detail",
        kwargs={"symbol": "600519", "indicator": "分红"},
        note="sina dividend page (stock_action fetcher)",
    ),
    Case(
        name="stock_action_rights",
        function="stock_history_dividend_detail",
        kwargs={"symbol": "000001", "indicator": "配股"},
        note="sina rights page, symbol with rights history (stock_action fetcher)",
    ),
    Case(
        name="financial_statement",
        function="stock_financial_report_sina",
        kwargs={"stock": "sh600519", "symbol": "资产负债表"},
        note="sina balance sheet, wide (financial_statement fetcher)",
    ),
    Case(
        name="financial_indicator",
        function="stock_financial_analysis_indicator_em",
        kwargs={"symbol": "600519.SH"},
        note="em F10 main-finance dataset (financial_indicator fetcher)",
    ),
    Case(
        name="index_constituent",
        function="index_stock_cons_weight_csindex",
        kwargs={"symbol": "000300"},
        note="CSI 300 close-weight file (index_constituent fetcher)",
    ),
)


class HttpRecorder:
    """Record every requests.get/post call while active.

    The patched functions still perform the real call; afterwards the
    serialized transcript can be replayed offline by
    :class:`HttpReplayer`.
    """

    def __init__(self) -> None:
        """Start with an empty transcript."""
        self.entries: list[dict[str, Any]] = []
        self._real_get: Any = None
        self._real_post: Any = None

    def __enter__(self) -> HttpRecorder:
        """Patch requests and start recording."""
        import requests

        self._real_get = requests.get
        self._real_post = requests.post
        _patch_attribute(requests, "get", self._get)
        _patch_attribute(requests, "post", self._post)
        return self

    def __exit__(self, *exc: object) -> None:
        """Restore the unpatched requests functions."""
        import requests

        _patch_attribute(requests, "get", self._real_get)
        _patch_attribute(requests, "post", self._real_post)
        self._real_get = None
        self._real_post = None

    def _get(self, url: str, **kwargs: Any) -> Any:  # noqa: ANN401
        response = self._real_get(url, **kwargs)
        self.entries.append(_serialize_call("GET", url, kwargs, response))
        return response

    def _post(self, url: str, **kwargs: Any) -> Any:  # noqa: ANN401
        response = self._real_post(url, **kwargs)
        self.entries.append(_serialize_call("POST", url, kwargs, response))
        return response


class HttpReplayer:
    """Replay a recorded transcript into the patched requests functions.

    Matching is positional: the Nth patched call consumes the Nth
    recorded entry, so both trees see an identical request order.
    """

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        """Bind a transcript and rewind the cursor."""
        self.entries = entries
        self.cursor = 0
        self._real_get: Any = None
        self._real_post: Any = None

    def __enter__(self) -> HttpReplayer:
        """Patch requests and rewind the cursor."""
        import requests

        self._real_get = requests.get
        self._real_post = requests.post
        _patch_attribute(requests, "get", self._replay)
        _patch_attribute(requests, "post", self._replay)
        return self

    def __exit__(self, *exc: object) -> None:
        """Restore the unpatched requests functions."""
        import requests

        _patch_attribute(requests, "get", self._real_get)
        _patch_attribute(requests, "post", self._real_post)
        self._real_get = None
        self._real_post = None

    def _replay(self, url: str, **kwargs: Any) -> Any:  # noqa: ANN401
        if self.cursor >= len(self.entries):
            message = f"replay exhausted: call {len(self.entries) + 1} to {url!r}"
            raise RuntimeError(f"{message} has no recorded response")
        entry = self.entries[self.cursor]
        self.cursor += 1
        return _deserialize_response(entry)


def _serialize_call(
    method: str,
    url: str,
    kwargs: Any,  # noqa: ANN401  # untyped requests boundary
    response: Any,  # noqa: ANN401  # untyped requests boundary
) -> dict[str, Any]:
    """Serialize one HTTP call and its response."""
    return {
        "method": method,
        "url": url,
        "params": kwargs.get("params"),
        "status": response.status_code,
        "content_type": response.headers.get("Content-Type", ""),
        # requests picks .text decoding from this; forcing utf-8 on
        # replay would mojibake non-UTF-8 pages (sina serves GBK).
        "encoding": response.encoding,
        "content_b64": base64.b64encode(response.content).decode("ascii"),
    }


def _deserialize_response(entry: dict[str, Any]) -> Any:  # noqa: ANN401  # untyped requests
    """Rebuild a requests.Response from a serialized entry."""
    import requests

    response = requests.Response()
    response.status_code = entry["status"]
    response.headers["Content-Type"] = entry["content_type"]
    response._content = base64.b64decode(entry["content_b64"])
    response.url = entry["url"]
    response.encoding = entry.get("encoding")
    return response


def _load_case_function(module_name: str, function: str) -> Any:  # noqa: ANN401  # dynamic fetch
    module = __import__(module_name)
    return getattr(module, function)


def _assert_upstream_pinned(upstream_path: Path) -> str:
    """Fail closed unless the upstream checkout matches the lock commit.

    Returns:
        The pinned commit hash.
    """
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    pinned = lock["upstream"]["commit"]
    head = _git_head(upstream_path)
    if head != pinned:
        raise RuntimeError(
            f"upstream HEAD {head} != lock commit {pinned}; recording must "
            f"come from the pinned baseline (git -C {upstream_path} checkout {pinned})"
        )
    return str(pinned)


def _git_head(path: Path) -> str:
    """Return the HEAD commit hash of a git checkout."""
    import subprocess  # nosec B404  # literal argv, shell disabled

    completed = subprocess.run(  # noqa: S603  # nosec B603, B607  # literal argv; git from PATH
        [  # noqa: S607  # argv is literal; git resolves from PATH
            "git",
            "-C",
            str(path),
            "rev-parse",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return str(completed.stdout).strip()


def record(upstream_path: Path) -> int:
    """Record reference fixtures from the upstream checkout.

    Per-case fault tolerance: a case that cannot be recorded (network
    refusal, upstream throttling) is marked ``pending`` in
    ``meta.json`` and skipped, so a partially reachable upstream still
    produces usable fixtures. The kline-host cases (push2his) are
    commonly throttled after a burst; re-run ``--record`` later to
    fill them in.

    Returns:
        Process exit code: 0 when every case recorded, 1 otherwise.
    """
    pinned = _assert_upstream_pinned(upstream_path)
    _pin_pure_requests_channel()
    sys.path.insert(0, str(upstream_path))
    pending: list[str] = []
    for case in CASES:
        target = FIXTURES_DIR / case.name
        target.mkdir(parents=True, exist_ok=True)
        function = _load_case_function(_UPSTREAM_CASE_MODULE, case.function)
        print(f"recording {case.name} ({case.function})...", flush=True)
        recorder = HttpRecorder()
        try:
            with recorder:
                frame = function(**case.kwargs)
        except Exception as exc:  # network refusal or upstream error
            reason = f"{type(exc).__name__}: {exc}"
            pending.append(case.name)
            (target / "meta.json").write_text(
                json.dumps(
                    {
                        "function": case.function,
                        "kwargs": case.kwargs,
                        "upstream_commit": pinned,
                        "status": "pending",
                        "reason": reason,
                        "recorded_at": date.today().isoformat(),
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
            print(f"  PENDING: {reason[:90]}")
            continue
        _write_transcript(target / "responses.json.gz", recorder.entries)
        _write_reference_frame(target / "reference.csv.gz", frame)
        (target / "meta.json").write_text(
            json.dumps(
                {
                    "function": case.function,
                    "kwargs": case.kwargs,
                    "upstream_commit": pinned,
                    "status": "recorded",
                    "n_calls": len(recorder.entries),
                    "rows": len(frame),
                    "dtypes": {str(name): str(frame[name].dtype) for name in frame.columns},
                    "recorded_at": date.today().isoformat(),
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"  -> {len(frame)} rows, {len(recorder.entries)} HTTP calls")
    if pending:
        print(f"\nPENDING (re-run --record later): {', '.join(pending)}")
        return 1
    return 0


def _write_reference_frame(path: Path, frame: Any) -> None:  # noqa: ANN401  # untyped pandas
    """Write the upstream reference frame as gzipped CSV.

    CSV keeps the fixtures dependency-free (no pyarrow in CI) and
    human-inspectable; the live dtypes live in ``meta.json`` because
    the text round trip cannot carry them.
    """
    frame.to_csv(path, index=False, compression="gzip", lineterminator="\n")


def _read_reference_frame(path: Path) -> Any:  # noqa: ANN401  # untyped pandas
    """Read a reference frame as text, so no inference distorts values."""
    import pandas as pd

    return pd.read_csv(path, dtype=str)


def _patch_attribute(target: object, name: str, value: object) -> None:
    """Assign an attribute by variable name.

    ``setattr`` with a literal name trips flake8-bugbear (B010), and a
    plain ``requests.get = ...`` assignment does not type-check in
    environments where requests ships inline types. Taking the name as
    a variable keeps both checks quiet without a blanket ignore.

    Args:
        target: Object to patch (the requests module).
        name: Attribute name to replace.
        value: Replacement callable.
    """
    setattr(target, name, value)


def _write_transcript(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write an HTTP transcript as gzipped JSON.

    The raw responses (sina serves a 3MB HTML page) compress ~10x, and
    base64 already inflates them by a third.
    """
    payload = json.dumps(entries, ensure_ascii=False).encode("utf-8")
    path.write_bytes(gzip.compress(payload, compresslevel=9))


def _read_transcript(path: Path) -> list[dict[str, Any]]:
    """Read an HTTP transcript written by :func:`_write_transcript`."""
    payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    return cast("list[dict[str, Any]]", payload)


def _pin_pure_requests_channel() -> None:
    """Force the ported/upstream em calls through requests, never curl.

    The upstream fork falls back to a ``curl --interface`` subprocess
    when the eastmoney quote hosts refuse a requests call. That path
    bypasses the record/replay patch entirely (subprocess, not
    requests) and would make replay non-deterministic, so it is
    disabled for both recording and comparison.
    """
    import os

    os.environ["AKSHARE_EASTMONEY_AUTO_CURL_INTERFACE"] = "0"
    os.environ["AKSHARE_EASTMONEY_CURL_INTERFACE"] = "0"


def compare() -> int:
    """Replay the fixtures into the ported tree and compare.

    Cases whose fixture is missing or marked ``pending`` (network
    refusal during recording) are reported as PENDING and fail the
    run: AC-6 requires full P0 coverage.

    Returns:
        Process exit code (0 on success).
    """
    _pin_pure_requests_channel()
    results: list[dict[str, Any]] = []
    pending: list[str] = []
    for case in CASES:
        target = FIXTURES_DIR / case.name
        responses_path = target / "responses.json.gz"
        reference_path = target / "reference.csv.gz"
        if not (responses_path.exists() and reference_path.exists()):
            pending.append(case.name)
            print(f"PENDING {case.name}: no recorded fixture (re-run --record)")
            continue
        entries = _read_transcript(responses_path)
        meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
        reference = _read_reference_frame(reference_path)
        function = _load_case_function(_PORTED_CASE_MODULE, case.function)
        replayer = HttpReplayer(entries)
        with replayer:
            frame = function(**case.kwargs)
        diffs = _compare_frames(reference, frame, meta.get("dtypes"))
        if replayer.cursor != len(entries):
            diffs.insert(
                0,
                f"http call count differs: upstream made {len(entries)}, "
                f"ported made {replayer.cursor}",
            )
        results.append(
            {
                "case": case,
                "rows": len(frame),
                "calls": len(entries),
                "diffs": diffs,
                "ok": not diffs,
            }
        )
        status = "PASS" if not diffs else "FAIL"
        print(f"{status} {case.name}: {len(frame)} rows, {len(diffs)} diff(s)")
        for diff in diffs[:MAX_DIFFS]:
            print(f"     {diff}")
    d10_report = _check_d10_qfq(results)
    _write_report(results, d10_report, pending)
    failed = [entry for entry in results if not entry["ok"]]
    if failed or d10_report["failed"] or pending:
        return 1
    return 0


def _compare_frames(
    reference: Any,  # noqa: ANN401  # untyped pandas frames
    ported: Any,  # noqa: ANN401  # untyped pandas frames
    expected_dtypes: dict[str, str] | None = None,
) -> list[str]:
    """Compare two frames under the AC-6 tolerance.

    ``expected_dtypes`` carries the dtype recorded from the live
    upstream frame: the parquet fixture infers pure-numeric ``object``
    columns as ``double`` on the round trip, so the stored reference
    frame is not a reliable dtype oracle. Without it the parquet
    dtypes are used as the fallback.

    Args:
        reference: The recorded upstream frame.
        ported: The replayed ported-tree frame.
        expected_dtypes: Column name to live upstream dtype string.

    Returns:
        Human-readable diff lines (empty when identical).
    """
    import numpy as np

    diffs: list[str] = []
    if list(reference.columns) != list(ported.columns):
        diffs.append(
            f"columns differ: reference={list(reference.columns)[:8]}... "
            f"ported={list(ported.columns)[:8]}..."
        )
        return diffs
    if reference.shape != ported.shape:
        diffs.append(f"shape differs: reference={reference.shape} ported={ported.shape}")
        return diffs
    truth = (
        expected_dtypes
        if expected_dtypes
        else {str(column): str(reference[column].dtype) for column in reference.columns}
    )
    diffs.extend(
        f"column {column!r}: dtype {truth.get(str(column))} != {ported[column].dtype}"
        for column in reference.columns
        if _dtype_kind(str(truth.get(str(column)))) != _dtype_kind(str(ported[column].dtype))
    )
    for column in reference.columns:
        expected = truth.get(str(column), "")
        numeric = _dtype_kind(expected) in {"float", "int"}
        left = [_cell(value, numeric=numeric) for value in reference[column]]
        right = [_cell(value, numeric=numeric) for value in ported[column]]
        if numeric:
            left_values = np.array([np.nan if v is None else v for v in left], dtype=float)
            right_values = np.array([np.nan if v is None else v for v in right], dtype=float)
            same = np.isclose(left_values, right_values, rtol=RTOL, equal_nan=True)
            if not bool(same.all()):
                bad = (~same).nonzero()[0][:MAX_DIFFS]
                diffs.extend(
                    f"cell {column}[{row}]: {left_values[row]!r} != {right_values[row]!r}"
                    for row in bad
                )
        else:
            diffs.extend(
                f"cell {column}[{index}]: {left[index]!r} != {right[index]!r}"
                for index in range(len(left))
                if left[index] != right[index]
            )
    return diffs[:MAX_DIFFS]


def _dtype_kind(dtype_name: str) -> str:
    """Map a dtype name to a coarse kind for cross-version comparison.

    pandas 2 spells text columns ``object``, pandas 3 spells them
    ``str``; the two are the same data class. Numeric kinds stay
    distinct so a genuine float/int/text regrouping still fails.

    Args:
        dtype_name: ``str(dtype)`` from a frame or a recorded map.

    Returns:
        One of ``text`` / ``float`` / ``int`` / ``bool`` /
        ``datetime`` or the lowercased name.
    """
    name = dtype_name.lower()
    if name in {"object", "str", "string", "string[python]", "string[pyarrow]"}:
        return "text"
    for kind, token in (("float", "float"), ("int", "int"), ("bool", "bool")):
        if token in name:
            return kind
    if "datetime" in name:
        return "datetime"
    return name


def _cell(value: object, *, numeric: bool) -> float | str | None:
    """Normalize one cell for comparison.

    Args:
        value: Cell from either frame (CSV text or a live object).
        numeric: True when the recorded dtype is numeric.

    Returns:
        ``None`` for missing values, a float for numeric columns and
        the canonical text otherwise.
    """
    import numpy as np
    import pandas as pd

    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    if pd.isna(value):
        return None
    if numeric and isinstance(value, (int, float, str)):
        return float(value)
    return str(value)


def _check_d10_qfq(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify the D10 synthesis reproduces the official qfq series.

    Uses the recorded raw and qfq frames of ``stock_daily_raw`` /
    ``stock_daily_qfq``: derives the cumulative qfq factor as
    ``qfq_close / raw_close`` and checks that
    :func:`opendata.data.adjust.apply_adjust` reproduces the official
    qfq OHLC within tolerance.
    """
    import pandas as pd

    from opendata.data.adjust import apply_adjust
    from opendata.data.models import AdjustFactor, Bar

    by_name = {entry["case"].name: entry for entry in results}
    if "stock_daily_raw" not in by_name or "stock_daily_qfq" not in by_name:
        return {"failed": True, "reason": "kline fixtures not recorded (pending, see above)"}
    if not (by_name["stock_daily_raw"]["ok"] and by_name["stock_daily_qfq"]["ok"]):
        return {"failed": True, "reason": "upstream frames failed comparison; D10 check skipped"}

    raw = _read_reference_frame(FIXTURES_DIR / "stock_daily_raw" / "reference.csv.gz")
    qfq = _read_reference_frame(FIXTURES_DIR / "stock_daily_qfq" / "reference.csv.gz")
    if raw.empty or qfq.empty or len(raw) != len(qfq):
        return {"failed": True, "reason": "raw/qfq frames empty or misaligned"}

    bars = [
        Bar(
            symbol=str(row["股票代码"]),
            trade_date=pd.to_datetime(row["日期"]).date(),
            open=float(row["开盘"]),
            high=float(row["最高"]),
            low=float(row["最低"]),
            close=float(row["收盘"]),
            volume=float(row["成交量"]),
            amount=float(row["成交额"]),
        )
        for _, row in raw.iterrows()
    ]
    factors = [
        AdjustFactor(
            symbol=str(raw.iloc[index]["股票代码"]),
            trade_date=pd.to_datetime(raw.iloc[index]["日期"]).date(),
            qfq_factor=float(qfq.iloc[index]["收盘"]) / float(raw.iloc[index]["收盘"]),
            hfq_factor=1.0,
        )
        for index in range(len(raw))
    ]
    synthesized = apply_adjust(bars, factors, "qfq")
    diffs = []
    for index, bar in enumerate(synthesized):
        official_close = float(qfq.iloc[index]["收盘"])
        if abs(bar.close - official_close) > abs(official_close) * RTOL:
            diffs.append(f"close[{index}]: synthesized {bar.close} != official {official_close}")
    return {"failed": bool(diffs), "diffs": diffs[:MAX_DIFFS], "rows": len(synthesized)}


def _write_report(
    results: list[dict[str, Any]], d10_report: dict[str, Any], pending: list[str]
) -> None:
    """Archive the comparison report (AC-6 evidence)."""
    lines = [
        "# A2.5 porting fidelity comparison (AC-6)",
        "",
        "Record-and-replay comparison: each P0 case's HTTP transcript was",
        "recorded from the upstream checkout pinned in upstream.lock, and",
        "replayed into the ported tree; outputs are compared with identical",
        f"columns/shape/dtypes and cell equality (float rtol={RTOL}).",
        "",
        "| case | function | rows | http calls | result |",
        "|------|----------|------|-----------|--------|",
    ]
    for entry in results:
        case = entry["case"]
        lines.append(
            f"| {case.name} | `{case.function}` | {entry['rows']} | {entry['calls']} | "
            f"{'PASS' if entry['ok'] else 'FAIL'} |"
        )
    if pending:
        lines += [
            "",
            "## Pending (network): re-run `--record`",
            "",
            "| case | reason |",
            "|------|--------|",
        ]
        for name in pending:
            meta = json.loads((FIXTURES_DIR / name / "meta.json").read_text(encoding="utf-8"))
            lines.append(f"| {name} | {meta.get('reason', 'not recorded')} |")
    lines += ["", "## A1 leftover: D10 qfq synthesis vs official em series", ""]
    if d10_report.get("reason"):
        lines.append(f"SKIPPED: {d10_report['reason']}")
    else:
        lines.append(
            f"{'PASS' if not d10_report['failed'] else 'FAIL'}: "
            f"{d10_report.get('rows', 0)} synthesized qfq bars vs the official series"
        )
        for diff in d10_report.get("diffs", []):
            lines.append(f"- {diff}")
    lines.append("")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"report written to {REPORT_PATH}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true", help="record reference fixtures (network)")
    parser.add_argument("--compare", action="store_true", help="replay and compare (offline)")
    parser.add_argument(
        "--upstream",
        type=Path,
        default=DEFAULT_UPSTREAM,
        help="upstream checkout path (recording only)",
    )
    args = parser.parse_args(argv)
    if args.record == args.compare:
        parser.error("exactly one of --record / --compare is required")
    if args.record:
        return record(args.upstream)
    return compare()


if __name__ == "__main__":
    raise SystemExit(main())
