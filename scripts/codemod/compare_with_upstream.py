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

The D10 adjustment claim is checked as an extra section over every
recorded ``raw``/official-``qfq`` pair, by the same falsifiable judges the
gate runs (``scripts/codemod/qfq_chain_checks.py``): the factor chain must
step on the dates and by the amounts the dividend endpoint says, and one
scalar per day must also fit open/high/low. An unrecorded pair is reported
as unchecked rather than passed.

Cases live in ``tests/fixtures/upstream/<case>/``; the report is
archived to ``docs/evidence/A2/compare-report.md``.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import sys
from collections.abc import Sequence  # noqa: TC003 - no future annotations in this script
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.codemod import qfq_chain_checks as checks  # noqa: E402 - needs REPO_ROOT on sys.path

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
            "end_date": "20240701",
            "adjust": "",
        },
        note=(
            "em daily klines, unadjusted (stock_daily fetcher); C27 widened the window to "
            "straddle 600519's 2024-06-19 ex-date so the qfq factor chain has a step "
            "inside the recorded range instead of being one constant"
        ),
    ),
    Case(
        name="stock_daily_qfq",
        function="stock_zh_a_hist",
        kwargs={
            "symbol": "600519",
            "period": "daily",
            "start_date": "20240101",
            "end_date": "20240701",
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
    # --- B1.3 P1 sampling: one case per registered P1 daily-bar domain ---
    Case(
        name="futures_daily_sina",
        function="futures_zh_daily_sina",
        kwargs={"symbol": "RB0"},
        note="sina commodity futures daily line (futures_daily fetcher)",
    ),
    Case(
        name="option_daily_sina",
        function="option_sse_daily_sina",
        kwargs={"symbol": "10003889"},
        note="sina SSE stock-option daily line (option_daily fetcher)",
    ),
    Case(
        name="bond_daily_sina",
        function="bond_zh_hs_cov_daily",
        kwargs={"symbol": "sh010107"},
        note="sina convertible-bond daily line (bond_daily fetcher)",
    ),
    Case(
        name="index_daily_em",
        function="index_zh_a_hist",
        kwargs={
            "symbol": "000300",
            "period": "daily",
            "start_date": "20240101",
            "end_date": "20240229",
        },
        note="em China index daily klines (index_daily fetcher)",
    ),
    Case(
        name="fund_etf_daily_em",
        function="fund_etf_hist_em",
        kwargs={
            "symbol": "510300",
            "period": "daily",
            "start_date": "20240101",
            "end_date": "20240229",
            "adjust": "",
        },
        note="em on-exchange ETF daily klines (fund_etf_daily fetcher)",
    ),
    # --- sina-channel twins for the em-blocked cases (em 502s from this
    # network; the sina routes hit the same registered domains) ---
    Case(
        name="stock_daily_sina_raw",
        function="stock_zh_a_daily",
        kwargs={
            "symbol": "sh600519",
            "start_date": "20240101",
            "end_date": "20240131",
            "adjust": "",
        },
        note="sina A-share daily line, unadjusted (stock_daily domain, sina channel)",
    ),
    Case(
        name="stock_daily_sina_qfq",
        function="stock_zh_a_daily",
        kwargs={
            "symbol": "sh600519",
            "start_date": "20240101",
            "end_date": "20240131",
            "adjust": "qfq",
        },
        note="sina A-share daily line, official qfq (stock_daily domain, sina channel)",
    ),
    Case(
        name="index_daily_sina",
        function="stock_zh_index_daily",
        kwargs={"symbol": "sh000300"},
        note="sina index daily line (index_daily domain, sina channel)",
    ),
    Case(
        name="fund_etf_daily_sina",
        function="fund_etf_hist_sina",
        kwargs={"symbol": "sh510300"},
        note="sina on-exchange ETF daily line (fund_etf_daily domain, sina channel)",
    ),
    # --- C27: the sina twins of the widened em window, added so the em/sina qfq
    # factor chains can be compared over dates that contain an ex-date. The narrow
    # January twins above stay exactly as recorded; a window with no ex-date inside
    # it cannot say anything about a factor chain (both chains are one constant).
    Case(
        name="stock_daily_sina_raw_wide",
        function="stock_zh_a_daily",
        kwargs={
            "symbol": "sh600519",
            "start_date": "20240101",
            "end_date": "20240701",
            "adjust": "",
        },
        note="sina A-share daily line, unadjusted, window straddling 2024-06-19 (C27)",
    ),
    Case(
        name="stock_daily_sina_qfq_wide",
        function="stock_zh_a_daily",
        kwargs={
            "symbol": "sh600519",
            "start_date": "20240101",
            "end_date": "20240701",
            "adjust": "qfq",
        },
        note="sina A-share daily line, official qfq, same widened window (C27)",
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


def record(upstream_path: Path, *, only: Sequence[str] | None = None) -> int:
    """Record reference fixtures from the upstream checkout.

    Per-case fault tolerance: a case that cannot be recorded (network
    refusal, upstream throttling) is marked ``pending`` in
    ``meta.json`` and skipped, so a partially reachable upstream still
    produces usable fixtures. The kline-host cases (push2his) are
    commonly throttled after a burst; re-run ``--record`` later to
    fill them in.

    Args:
        upstream_path: Checkout of the pinned upstream.
        only: Restrict the run to these case names, leaving the other
            fixtures (and their ``meta.json`` status) untouched.

    Returns:
        Process exit code: 0 when every attempted case recorded, 1 otherwise.
    """
    pinned = _assert_upstream_pinned(upstream_path)
    _pin_pure_requests_channel()
    sys.path.insert(0, str(upstream_path))
    pending: list[str] = []
    for case in CASES:
        if only is not None and case.name not in only:
            continue
        target = FIXTURES_DIR / case.name
        target.mkdir(parents=True, exist_ok=True)
        function = _load_case_function(_UPSTREAM_CASE_MODULE, case.function)
        print(f"recording {case.name} ({case.function})...", flush=True)
        recorder = HttpRecorder()
        reason: str | None
        try:
            with recorder:
                frame = function(**case.kwargs)
        except Exception as exc:  # network refusal or upstream error
            reason = f"{type(exc).__name__}: {exc}"
        else:
            if frame is None or len(frame) == 0:
                # An empty reference frame is not evidence: replaying it
                # would compare zero rows and pass vacuously, so the case
                # stays pending (fail closed, re-run --record later).
                reason = "EmptyReferenceFrame: upstream returned 0 rows"
            else:
                reason = None
                _write_transcript(target / "responses.json.gz", recorder.entries)
                _write_reference_frame(target / "reference.csv.gz", frame)
                _write_case_meta(
                    target,
                    case,
                    pinned,
                    status="recorded",
                    extra={
                        "n_calls": len(recorder.entries),
                        "rows": len(frame),
                        # The text round trip cannot carry dtypes, so the
                        # live ones are pinned here and asserted on replay.
                        "dtypes": {str(name): str(frame[name].dtype) for name in frame.columns},
                    },
                )
                print(f"  -> {len(frame)} rows, {len(recorder.entries)} HTTP calls")
        if reason is not None:
            pending.append(case.name)
            _write_case_meta(target, case, pinned, status="pending", extra={"reason": reason})
            print(f"  PENDING: {reason[:90]}")
    if pending:
        print(f"\nPENDING (re-run --record later): {', '.join(pending)}")
        return 1
    return 0


def _write_case_meta(
    target: Path,
    case: Case,
    pinned: str,
    *,
    status: str,
    extra: dict[str, Any],
) -> None:
    """Write one case's ``meta.json`` (recorded or pending).

    Args:
        target: Fixture directory of the case.
        case: The case being recorded.
        pinned: Upstream commit the recording is pinned to.
        status: ``"recorded"`` or ``"pending"``.
        extra: Status-specific fields (row count, failure reason).
    """
    payload: dict[str, Any] = {
        "function": case.function,
        "kwargs": case.kwargs,
        "upstream_commit": pinned,
        "status": status,
        **extra,
        "recorded_at": date.today().isoformat(),
    }
    (target / "meta.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )


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
        notes: list[str] = []
        diffs = _compare_frames(reference, frame, meta.get("dtypes"), notes=notes)
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
                "tolerated": notes,
                "ok": not diffs,
            }
        )
        status = "PASS" if not diffs else "FAIL"
        print(
            f"{status} {case.name}: {len(frame)} rows, {len(diffs)} diff(s), "
            f"{len(notes)} 处文本不同而浮点在 rtol={RTOL} 内一致"
        )
        for diff in diffs[:MAX_DIFFS]:
            print(f"     {diff}")
        for note in notes[:MAX_DIFFS]:
            print(f"     ~ {note}")
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
    notes: list[str] | None = None,
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
        notes: Optional collector for differences that AC-6 tolerates, so a
            representation-only drift stays visible instead of silent.

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
            diffs.extend(_text_cell_diffs(column, reference[column], ported[column], notes=notes))
    return diffs[:MAX_DIFFS]


def _float_pair(left: object, right: object) -> tuple[float, float] | None:
    """Return both cells as floats only when one side is a real float.

    The recorded reference frame is read with ``dtype=str``, so a computed
    float reaches the comparison as ``str(value)`` on one side and as the
    fixture's text on the other. Requiring an actual float instance keeps
    genuine text columns (codes like ``000001``) on the textual rule: a
    numeric *looking* pair is not the same as a numeric pair.

    Args:
        left: Reference cell, raw from the frame.
        right: Ported cell, raw from the frame.

    Returns:
        The two values as floats, or ``None`` when the pair is not float-bearing.
    """
    import numpy as np

    floats = (float, np.float64, np.floating)
    if not (isinstance(left, floats) or isinstance(right, floats)):
        return None
    try:
        pair = (float(left), float(right))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if any(np.isnan(value) for value in pair):
        return None
    return pair


def _text_cell_diffs(
    column: object,
    reference: Any,  # noqa: ANN401  # untyped pandas series
    ported: Any,  # noqa: ANN401  # untyped pandas series
    notes: list[str] | None = None,
) -> list[str]:
    """Diff one text column, honouring AC-6's float tolerance.

    AC-6 asks for "取值一致（浮点在容忍度内）", but an ``object`` column can
    hold floats that the upstream code *computed* (a balance-sheet total, for
    instance). Judging those by ``repr`` makes the verdict depend on how the
    same double happens to print, so a summation change in a newer pandas
    reddens a numerically faithful replay (this is the shape of the 2026-09-23
    CI failures: ``'218472857114.40997' != '218472857114.41'``). Text that is
    not float-bearing still has to match character for character.

    Representation-only agreements are never silent: each one is appended to
    ``notes`` so a fixture drifting away from what the code computes stays
    visible in the report.

    Args:
        column: Column label, for the message.
        reference: Recorded column (text).
        ported: Replayed column (live objects).
        notes: Optional collector for tolerated differences.

    Returns:
        Diff lines for this column.
    """
    import numpy as np

    found: list[str] = []
    for index, (raw_left, raw_right) in enumerate(
        zip(reference.tolist(), ported.tolist(), strict=True)
    ):
        text_left = _cell(raw_left, numeric=False)
        text_right = _cell(raw_right, numeric=False)
        if text_left == text_right:
            continue
        pair = _float_pair(raw_left, raw_right)
        if pair is None:
            found.append(f"cell {column}[{index}]: {text_left!r} != {text_right!r}")
            continue

        left_value, right_value = pair
        delta = abs(right_value - left_value)
        relative = delta / abs(left_value) if left_value else delta
        if bool(np.isclose(left_value, right_value, rtol=RTOL)):
            if notes is not None:
                notes.append(
                    f"cell {column}[{index}]: 文本不同但浮点在 rtol={RTOL} 内一致"
                    f"（{text_left} -> {text_right}，abs={delta:.3g}，rel={relative:.3g}）"
                )
            continue
        found.append(
            f"cell {column}[{index}]: {text_left!r} != {text_right!r}"
            f"（超出 rtol={RTOL}：abs={delta:.6g}，rel={relative:.3g}）"
        )
    return found


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
    """Run the D10 factor-chain checks over every raw/qfq pair replayed this run.

    The expectation is not taken from the qfq series itself: the step dates and
    heights are read off the dividend endpoint, and the synthesis is judged on
    open/high/low plus the volume/amount passthrough (see
    ``scripts/codemod/qfq_chain_checks.py``). Pairs still pending a recording are
    listed as unchecked, never as passed.

    Args:
        results: One entry per case that was replayed in this run.

    Returns:
        ``{"failed": bool, "lines": [...], "diffs": [...]}`` for the report.
    """
    by_name = {entry["case"].name: entry for entry in results}
    dividends = checks.cash_dividends(
        _read_reference_frame(FIXTURES_DIR / "stock_action_dividend" / "reference.csv.gz")
    )
    lines: list[str] = []
    diffs: list[str] = []
    for label, raw_case, qfq_case in checks.ADJUST_PAIRS:
        if raw_case not in by_name or qfq_case not in by_name:
            lines.append(f"{label}: 未判读（夹具还没录到，本轮没有回放这一对）")
            continue
        if not (by_name[raw_case]["ok"] and by_name[qfq_case]["ok"]):
            diffs.append(f"{label}: 上游帧比对已失败，复权链不再判读")
            continue
        raw = _normalized_frame(raw_case)
        qfq = _normalized_frame(qfq_case)
        found = checks.check_factor_steps(raw, qfq, dividends) + checks.check_synthesis(raw, qfq)
        lines.append(f"{label}: {'FAIL' if found else 'PASS'}（{len(raw)} 天）")
        diffs.extend(f"{label} {diff}" for diff in found[:MAX_DIFFS])

    summary, window_diffs = _check_sina_window_consistency()
    if summary:
        lines.append(summary)
        diffs.extend(f"窗口自洽 {diff}" for diff in window_diffs[:MAX_DIFFS])

    return {"failed": bool(diffs), "lines": lines, "diffs": diffs[:MAX_DIFFS]}


def _normalized_frame(case_name: str) -> Any:  # noqa: ANN401 - untyped pandas frame
    """Read one recorded reference frame with the em/sina columns unified."""
    return checks.normalize(_read_reference_frame(FIXTURES_DIR / case_name / "reference.csv.gz"))


def _check_sina_window_consistency() -> tuple[str, list[str]]:
    """Is the widened sina pair still the same series as the short one it came from?

    Returns:
        ``(summary line, diff lines)``; both empty while any of the four fixtures
        is still pending, so an unrecorded window is never read as a passed check.
    """
    cases = [*checks.NARROW_PAIR, *checks.WIDE_PAIR]
    if not all((FIXTURES_DIR / case / "reference.csv.gz").exists() for case in cases):
        return "", []
    return checks.check_window_consistency(*[_normalized_frame(case) for case in cases])


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
        "| case | function | rows | http calls | result | 文本不同而浮点一致 |",
        "|------|----------|------|-----------|--------|--------------------|",
    ]
    for entry in results:
        case = entry["case"]
        lines.append(
            f"| {case.name} | `{case.function}` | {entry['rows']} | {entry['calls']} | "
            f"{'PASS' if entry['ok'] else 'FAIL'} | {len(entry.get('tolerated', ()))} |"
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
            meta_path = FIXTURES_DIR / name / "meta.json"
            reason = "not recorded"
            if meta_path.exists():
                reason = json.loads(meta_path.read_text(encoding="utf-8")).get("reason", reason)
            lines.append(f"| {name} | {reason} |")
    lines += ["", "## D10 qfq factor chain (checks shared with the gate)", ""]
    report_lines = d10_report.get("lines", [])
    if report_lines:
        lines += [f"- {line}" for line in report_lines]
    else:
        lines.append("- 未判读：本轮没有可跑的 raw/qfq 对")
    if d10_report["failed"]:
        lines += ["", "失败项：", ""] + [f"- {diff}" for diff in d10_report.get("diffs", [])]
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
    parser.add_argument(
        "--only",
        action="append",
        metavar="CASE",
        help="record only these case names (repeatable); other fixtures are left untouched",
    )
    args = parser.parse_args(argv)
    if args.record == args.compare:
        parser.error("exactly one of --record / --compare is required")
    if args.record:
        unknown = set(args.only or ()) - {case.name for case in CASES}
        if unknown:
            parser.error(f"unknown case name(s): {', '.join(sorted(unknown))}")
        return record(args.upstream, only=args.only)
    return compare()


if __name__ == "__main__":
    raise SystemExit(main())
