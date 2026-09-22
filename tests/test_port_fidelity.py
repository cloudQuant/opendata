"""Porting fidelity replay tests (A2.5 / AC-6).

Each case replays the HTTP transcript recorded from the upstream
checkout (``scripts/codemod/compare_with_upstream.py --record``) into
the ported ``opendata_http`` tree and asserts the output matches the
recorded upstream frame (identical columns, shape, dtypes and cell
values within the AC-6 float tolerance). Cases without a recording
(network refusal at record time) are skipped with the recorded
reason; re-run the recorder to fill them in.

No network access happens here: every request comes from the
transcript, and a call-count mismatch is a failure too.
"""

import json

import pandas as pd
import pytest

from scripts.codemod import compare_with_upstream as comparator

FIXTURES_DIR = comparator.FIXTURES_DIR


def _fixture_status(case_name: str) -> tuple[bool, str]:
    """Return (recorded, reason) for a case fixture."""
    meta_path = FIXTURES_DIR / case_name / "meta.json"
    responses = FIXTURES_DIR / case_name / "responses.json.gz"
    reference = FIXTURES_DIR / case_name / "reference.csv.gz"
    if not meta_path.exists():
        return False, "not recorded (run --record)"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("status") != "recorded" or not (responses.exists() and reference.exists()):
        return False, str(meta.get("reason", "no recorded fixture"))
    return True, ""


@pytest.mark.parametrize("case", comparator.CASES, ids=lambda case: case.name)
def test_ported_output_matches_upstream(case):
    recorded, reason = _fixture_status(case.name)
    if not recorded:
        pytest.skip(f"recording pending for {case.name}: {reason}")

    entries = comparator._read_transcript(FIXTURES_DIR / case.name / "responses.json.gz")
    meta = json.loads((FIXTURES_DIR / case.name / "meta.json").read_text(encoding="utf-8"))
    reference = comparator._read_reference_frame(FIXTURES_DIR / case.name / "reference.csv.gz")
    function = comparator._load_case_function("opendata_http", case.function)

    comparator._pin_pure_requests_channel()
    replayer = comparator.HttpReplayer(entries)
    with replayer:
        frame = function(**case.kwargs)

    diffs = comparator._compare_frames(reference, frame, meta.get("dtypes"))
    assert not diffs, "\n".join(diffs)
    assert replayer.cursor == len(entries), (
        f"http call count differs: upstream {len(entries)}, ported {replayer.cursor}"
    )


def test_recorded_fixtures_are_pinned_to_upstream_lock():
    """Every recorded fixture names the lock commit it came from."""
    lock = json.loads(comparator.LOCK_PATH.read_text(encoding="utf-8"))
    pinned = lock["upstream"]["commit"]
    for case in comparator.CASES:
        meta_path = FIXTURES_DIR / case.name / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["upstream_commit"] == pinned


def test_d10_qfq_synthesis_matches_official_series():
    """A1 leftover: apply_adjust reproduces the official em qfq series."""
    recorded, reason = _fixture_status("stock_daily_raw")
    if not recorded:
        pytest.skip(f"kline fixtures pending: {reason}")

    from opendata.data.adjust import apply_adjust
    from opendata.data.models import AdjustFactor, Bar

    raw = comparator._read_reference_frame(FIXTURES_DIR / "stock_daily_raw" / "reference.csv.gz")
    qfq = comparator._read_reference_frame(FIXTURES_DIR / "stock_daily_qfq" / "reference.csv.gz")
    assert len(raw) == len(qfq) and not raw.empty

    def _bars(frame):
        return [
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
            for _, row in frame.iterrows()
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

    synthesized = apply_adjust(_bars(raw), factors, "qfq")

    for index, bar in enumerate(synthesized):
        official = float(qfq.iloc[index]["收盘"])
        assert abs(bar.close - official) <= abs(official) * comparator.RTOL
