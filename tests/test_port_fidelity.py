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
from collections.abc import Sequence

import pandas as pd
import pytest

from scripts.codemod import compare_with_upstream as comparator
from scripts.codemod import qfq_chain_checks as checks

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


#: A ``raw``/official-``qfq`` pair off the same instrument (see ``checks.ADJUST_PAIRS``
#: for why every window there straddles an ex-date).
ADJUST_PAIRS = tuple(
    pytest.param(raw_case, qfq_case, id=label) for label, raw_case, qfq_case in checks.ADJUST_PAIRS
)
NARROW_PAIR, WIDE_PAIR = checks.NARROW_PAIR, checks.WIDE_PAIR


def _frame(case_name: str) -> pd.DataFrame:
    """Load a recorded reference frame with the em/sina column names unified.

    Args:
        case_name: Fixture directory name.

    Returns:
        Frame with ``trade_date/open/high/low/close/volume/amount``, as recorded.
    """
    frame = comparator._read_reference_frame(FIXTURES_DIR / case_name / "reference.csv.gz")
    return checks.normalize(frame)


def _require_recorded(cases: Sequence[str]) -> None:
    """Skip the calling test unless every case here is recorded."""
    for case in cases:
        recorded, reason = _fixture_status(case)
        if not recorded:
            pytest.skip(f"fixture pending for {case}: {reason}")


def _cash_dividends() -> dict:
    """The recorded dividend detail (a different upstream endpoint than the klines)."""
    frame = comparator._read_reference_frame(
        FIXTURES_DIR / "stock_action_dividend" / "reference.csv.gz"
    )
    return checks.cash_dividends(frame)


@pytest.mark.parametrize(("raw_case", "qfq_case"), ADJUST_PAIRS)
def test_qfq_factor_steps_are_the_recorded_ex_dates(raw_case: str, qfq_case: str) -> None:
    """AC-11/AC-6: the qfq chain steps exactly on the recorded ex-dates, by the dividend amount.

    This replaces the A1 leftover, which derived the factor from ``qfq_close /
    raw_close`` and then asserted that ``raw * factor`` reproduces ``qfq_close`` —
    true by construction, since ``apply_adjust`` literally multiplies by that ratio
    (``opendata/data/adjust.py:66``). Here the *expected* move comes from another
    endpoint, and both an unexplained step and a missing step fail.
    """
    _require_recorded((raw_case, qfq_case))
    diffs = checks.check_factor_steps(_frame(raw_case), _frame(qfq_case), _cash_dividends())
    assert not diffs, "\n".join(diffs)


@pytest.mark.parametrize(("raw_case", "qfq_case"), ADJUST_PAIRS)
def test_qfq_synthesis_reproduces_the_official_series(raw_case: str, qfq_case: str) -> None:
    """D10: one scalar factor per day turns the raw series into the official qfq series.

    The factor still comes from the close (that is what ``dwd_stock_adjust`` stores),
    but the assertion moved to the *other three* price fields and to volume/amount:
    whether a single multiplier also fits open/high/low is not implied by how the
    factor was defined, and 成交量/成交额 must pass through untouched.
    """
    _require_recorded((raw_case, qfq_case))
    diffs = checks.check_synthesis(_frame(raw_case), _frame(qfq_case))
    assert not diffs, "\n".join(diffs)


def test_widened_fixture_window_describes_the_same_series() -> None:
    """AC-6: widening the recorded window must not change what the recording says.

    C27 traded a longer window for one ex-date inside it. That is only a longer
    segment of the same series if the unadjusted closes agree on the overlap and the
    qfq anchor does not move with the requested range; if either fails, a factor chain
    recorded over one window is not a restriction of the chain over another and no two
    windows may be compared.
    """
    _require_recorded((*NARROW_PAIR, *WIDE_PAIR))
    summary, diffs = checks.check_window_consistency(
        _frame(NARROW_PAIR[0]),
        _frame(NARROW_PAIR[1]),
        _frame(WIDE_PAIR[0]),
        _frame(WIDE_PAIR[1]),
    )
    assert not diffs, f"{summary}\n" + "\n".join(diffs)


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
