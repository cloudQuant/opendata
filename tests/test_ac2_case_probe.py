"""AC2's 25 prose rows have to be falsifiable, in both directions, before they are signable.

``scripts/quality/ac2_case_probe.py`` turns §2 of the iteration-2 acceptance document into one probe
per case: a measurement, a judgement over it, the reading that would close the case, and
counter-examples that turn that closing reading back into a gap. A plane like that can rot in three
ways this file watches:

* The denominator. §2 has 25 rows and every row must carry a probe; a row that silently stops
  parsing, or an id that is dropped from ``expected_ids()``, would print a green run over 24 cases.
* The judge. A check that returns ``gap`` for every input passes ``--all`` while proving nothing, so
  the closure arm must reach ``holds_offline`` and every counter-example must flip it back.
* The record. ``docs/quality/ac2-case-ledger.json`` states its own falsification tally; a ledger
  that claims more counter-examples than the code defines is the machine-echo failure wearing a
  new costume.

Synthetic inputs are used wherever a planted row can answer the question, so the parser and the
drift arm are asserted on text this file controls rather than quoted from one favourable run. The
full ``--self-test`` is run once per session because it is the only arm that exercises all 25
judges with their real faces.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from scripts.quality import ac2_case_probe as plane

if TYPE_CHECKING:
    from pathlib import Path

REPO_ROOT: Path = plane.REPO_ROOT
LEDGER_PATH = REPO_ROOT / plane.LEDGER_REL
DOC_TEXT = (REPO_ROOT / plane.DOC_REL).read_text(encoding="utf-8")

#: A §2-shaped row whose criterion cell carries an escaped pipe, and one that carries a bare extra
#: pipe. The first must parse to four cells; the second must be reported, never silently split.
ESCAPED_PIPE_ROW = (
    "| AC2-99 | 操作 | 判据含 A \\| B 两个面 | 边界 |\n"
    "| AC2-98 | 操作 | 判据含 A | B 两个面 | 边界 |\n"
)

STATES = {plane.HOLDS, plane.GAP, plane.NO_FACE, plane.BLOCKED, plane.DRIFT}


@pytest.fixture(scope="session")
def self_test_output() -> tuple[int, str]:
    """Run the plane's own ``--self-test`` once, through its own argv entry point."""
    proc = subprocess.run(  # noqa: S603  # nosec B603  - literal argv, shell disabled
        [sys.executable, str(REPO_ROOT / plane.SELF_REL), "--self-test"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=1800,
    )
    return proc.returncode, proc.stdout + proc.stderr


def tally_ok(row: dict, expected: int) -> bool:
    """Read the ledger's own ``n/n`` falsification claim against the code's break count."""
    parts = str(row["counter_examples"]).split("/")
    return len(parts) == 2 and parts == [str(expected), str(expected)]


def test_denominator_is_the_documents_not_the_authors() -> None:
    cases, malformed = plane.parse_cases(DOC_TEXT)
    assert malformed == []
    assert len(cases) == plane.CASE_COUNT == 25
    assert {case.id for case in cases} == set(plane.expected_ids())
    registered = plane.probes_by_case()
    assert set(registered) == set(plane.expected_ids())
    assert len(registered) == 25, "a case without a probe would leave the denominator on its own"


def test_rows_split_on_escaped_pipes_and_report_bare_ones() -> None:
    cases, malformed = plane.parse_cases(ESCAPED_PIPE_ROW)
    assert [case.id for case in cases] == ["AC2-99"], "the bare-pipe row must not parse into cells"
    assert cases[0].criterion == "判据含 A | B 两个面"
    assert len(malformed) == 1 and "AC2-98" in malformed[0]


def test_every_probe_carries_anchors_closure_and_breaks() -> None:
    for item in plane.probes_by_case().values():
        assert item.anchors, item.case
        assert item.summary, item.case
        # An empty override set is legal: it means the live reading is already the closing one.
        assert item.breaks, f"{item.case}: a judge nobody has falsified"
        assert all(break_.expect == plane.GAP for break_ in item.breaks)
        assert all(isinstance(value, str) for value in item.closure.values())
        assert item.repair, item.case
        for reach, _ in item.requires:
            assert reach in plane.REACH_CLASSES, (item.case, reach)


def test_anchors_are_the_documents_own_words() -> None:
    """The drift arm can only bite because the shipped rows do contain every anchor."""
    by_id = {case.id: case for case in plane.parse_cases(DOC_TEXT)[0]}
    for case_id, item in plane.probes_by_case().items():
        for anchor in item.anchors:
            assert anchor in by_id[case_id].text, (case_id, anchor)


def test_drift_is_a_state_the_evaluate_path_reaches() -> None:
    item = plane.probes_by_case()["AC2-01"]
    reworded = plane.Case(
        id="AC2-01", line=24, operation=item.anchors[0], criterion="换了措辞的判据", boundary=""
    )
    result = plane.evaluate(item, reworded, with_self_test=False)
    assert result.state == plane.DRIFT
    assert "anchors no longer in the row" in result.reason


def test_missing_document_row_is_recorded_as_no_row() -> None:
    item = plane.probes_by_case()["AC2-02"]
    result = plane.evaluate(item, None, with_self_test=False)
    assert result.state == plane.NO_FACE
    assert result.readings == []


def test_self_test_closes_and_every_break_flips(self_test_output: tuple[int, str]) -> None:
    rc, output = self_test_output
    lines = [line for line in output.splitlines() if line.startswith("AC2-")]
    assert len(lines) == 25, output[-400:]
    assert rc == 0 and "SELF_TEST PASS failures=0" in output, output[-400:]
    for line in lines:
        tokens = line.split()[2:]  # ``AC2-01 OK`` carries no face of its own
        parts = dict(token.split("=", 1) for token in tokens if "=" in token)
        assert parts["breaks"] == parts["flipped"], line
        assert parts["closure"] == plane.HOLDS, line


def test_tally_reader_accepts_the_real_form_and_rejects_a_pad() -> None:
    assert tally_ok({"counter_examples": "8/8"}, 8)
    assert not tally_ok({"counter_examples": "8/7"}, 8)
    assert not tally_ok({"counter_examples": "9/9"}, 8)
    assert not tally_ok({"counter_examples": "-"}, 8)


def test_ledger_agrees_with_the_code_that_wrote_it() -> None:
    assert LEDGER_PATH.is_file(), "run the probe with --write-ledger"
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    registered = plane.probes_by_case()
    assert ledger["denominator"] == plane.CASE_COUNT == 25
    assert len(ledger["cases"]) == 25
    assert [row["case"] for row in ledger["cases"]] == list(plane.expected_ids())
    assert sum(ledger["counts"].values()) == 25
    for row in ledger["cases"]:
        item = registered[row["case"]]
        assert row["state"] in STATES
        assert row["criterion"], row["case"]
        assert row["readings"], f"{row['case']}: a verdict with no measured face"
        assert all("=" in reading for reading in row["readings"])
        assert tally_ok(row, len(item.breaks)), row
        assert row["command"].endswith(row["case"])
        if row["state"] in {plane.GAP, plane.BLOCKED, plane.DRIFT}:
            assert row["reason"], row["case"]
        if row["offline_state"] == plane.HOLDS and row["state"] == plane.BLOCKED:
            assert row["missing_legs"], row["case"]
