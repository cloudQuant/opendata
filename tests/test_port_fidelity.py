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
import os
import time

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
def test_ported_output_matches_upstream(case: comparator.Case) -> None:
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


def test_recorded_fixtures_are_pinned_to_upstream_lock() -> None:
    """Every recorded fixture names the lock commit it came from."""
    lock = json.loads(comparator.LOCK_PATH.read_text(encoding="utf-8"))
    pinned = lock["upstream"]["commit"]
    for case in comparator.CASES:
        meta_path = FIXTURES_DIR / case.name / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["upstream_commit"] == pinned


def test_d10_qfq_synthesis_matches_official_series() -> None:
    """A1 leftover: apply_adjust reproduces the official em qfq series."""
    recorded, reason = _fixture_status("stock_daily_raw")
    if not recorded:
        pytest.skip(f"kline fixtures pending: {reason}")

    from opendata.data.adjust import apply_adjust
    from opendata.data.models import AdjustFactor, Bar

    raw = comparator._read_reference_frame(FIXTURES_DIR / "stock_daily_raw" / "reference.csv.gz")
    qfq = comparator._read_reference_frame(FIXTURES_DIR / "stock_daily_qfq" / "reference.csv.gz")
    assert len(raw) == len(qfq) and not raw.empty

    def _bars(frame: pd.DataFrame) -> list[Bar]:
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


def test_financial_statement_update_dates_ignore_the_machine_timezone() -> None:
    """AC-6 reproducibility: sina's ``更新日期`` is Beijing wall time, not host time.

    The fixture was recorded on a UTC+8 host. Rendering the upstream epoch
    in the host's own zone moved every cell by the zone difference -
    measured on a UTC+3 host: 10 of 10 rows came back 5 h early - so the
    replay is only reproducible while that conversion stays pinned.
    """
    case = next(item for item in comparator.CASES if item.name == "financial_statement")
    recorded, reason = _fixture_status(case.name)
    if not recorded:
        pytest.skip(f"recording pending for {case.name}: {reason}")

    reference = comparator._read_reference_frame(FIXTURES_DIR / case.name / "reference.csv.gz")
    function = comparator._load_case_function("opendata_http", case.function)
    comparator._pin_pure_requests_channel()
    entries = comparator._read_transcript(FIXTURES_DIR / case.name / "responses.json.gz")
    expected = list(reference["更新日期"])
    assert expected, "the recorded frame must carry the column this test is about"

    previous_zone = os.environ.get("TZ")
    rendered: dict[str, list[str]] = {}
    try:
        for zone in ("Asia/Shanghai", "Etc/UTC", "America/Los_Angeles"):
            os.environ["TZ"] = zone
            time.tzset()
            replayer = comparator.HttpReplayer(entries)
            with replayer:
                frame = function(**case.kwargs)
            assert replayer.cursor == len(entries)
            rendered[zone] = list(frame["更新日期"])
    finally:
        if previous_zone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_zone
        time.tzset()

    for zone, column in rendered.items():
        assert column == expected, f"{zone} rendered 更新日期 differently from the recording"
