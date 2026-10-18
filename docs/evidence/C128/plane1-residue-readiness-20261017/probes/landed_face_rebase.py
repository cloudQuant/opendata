"""Re-base the landed-face constants, and show the re-based arm can still go red.

Wave-28 step 4 landed one row -- ``federal_reserve::YieldCurve``, after its reader was retargeted
onto the class ``opendata/data/domains.yaml`` declares -- so the landed plane-1 face moved
``275 -> 276`` accepted and ``7 -> 6`` INCOMPLETE (the row's claims were BLOCKED, so UNREVIEWED
held at 68) while the identity set stayed ``5168e1e723999312``. Two wave-27
instruments assert that face (``ledger_axis_blast.A_landed_report_face``,
``witness_seam_kind_census.A_landed_report_face``) and both read ``arms_failed=1`` for it until
their constants moved. Re-basing an expected value is only safe if the arm still discriminates, so
this driver calls each probe's ``main()`` three times in one process over the same report bytes:
with the shipped constant, with the constant forged back to the previous generation, then with it
restored. A re-base that leaves an arm incapable of failing is how a stale judge survives its own
correction, which is the failure mode being guarded.

Arms, six per subject, all of which can fail:

``A_unmodified_green``
    the probe returns 0 and its own face line reports ``arms_failed=0``.
``B_forged_previous_red``
    with the landed constant replaced by the previous generation it returns non-zero and its face
    line no longer reports zero failed arms -- the arm is alive, not decorative.
``C_only_the_landed_arm_moves``
    exactly one ``PASS=False`` line appears in the forged run and it names the landed-face arm, so
    the forge reddens the face gate rather than breaking the probe.
``D_measurement_unchanged_by_the_forge``
    the report-derived counters are identical across the shipped and forged runs -- residue size and
    identity set for both subjects, plus the landed count in whatever shape that subject prints (the
    ``statuses`` dict for one, the bare ``accepted`` integer for the other). A counter the probe
    does not publish is reported MISSING and fails the arm rather than reading as "unchanged", which
    is how take 1 caught its own false negative. The red is therefore attributable to the asserted
    constant, not to the data moving underneath.
``E_restored_green_and_aging_is_named``
    the third run returns 0 again, and every stdout line that differs between run 1 and run 3 is a
    credential-grant line: the probe's first run installs its fixture tokens into ``os.environ``,
    so ``granted=`` is the one field that legitimately ages inside a single process. Comparing whole
    stdout would have read that aging as a broken re-base.
``F_delta_matches_the_committed_face``
    the delta the re-based probe prints is exactly the step-4 move (one row out of INCOMPLETE and
    into OFFLINE_ACCEPTED, summing to zero) and the forged run still *reports* the real accepted
    count while asserting the old one, which is what tells a reader which side was wrong.

The previous value is taken from each probe module's own ``*_PREVIOUS`` constant, so this driver
cannot disagree with the instrument it is testing. Nothing here writes into ``opendata`` or
``scripts``; the only file written is the carrier this reading goes into.

    PY=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
    $PY docs/evidence/C128/plane1-residue-readiness-20261017/probes/landed_face_rebase.py

Run straight, never through a pipe. The subject probes install the runner's socket refusal, so no
outbound socket is reachable from this process.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import importlib
import inspect
import io
import re
import sys
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROBE_DIR))

RUNS = PROBE_DIR.parent / "runs"
CARRIER = RUNS / "wave29-step2d-landed-face-rebase.txt"

EXPECTED_ACCEPTED = 281
EXPECTED_DELTA = {"INCOMPLETE": 2, "OFFLINE_ACCEPTED": 5, "UNREVIEWED": -7}
SUBJECTS = (
    {
        "module": "ledger_axis_blast",
        "constant": "LANDED_STATUSES",
        "previous": "LANDED_STATUSES_PREVIOUS",
        "face_prefix": "ledger_blast_face",
        "landed_line": "A_landed_report_face",
    },
    {
        "module": "witness_seam_kind_census",
        "constant": "LANDED_ACCEPTED",
        "previous": "LANDED_ACCEPTED_PREVIOUS",
        "face_prefix": "seam_kind_face",
        "landed_line": "A_landed_report_face",
    },
)
ACCEPTED_RE = re.compile(r"\baccepted=(\d+)")
RESIDUE_RE = re.compile(r"\bresidue=(\d+)")
IDENTITY_RE = re.compile(r"\bidentity_set=([0-9a-f]{16})")
DELTA_RE = re.compile(r"rebase_delta=(\{[^}]*\})")
ACCEPTED_DELTA_RE = re.compile(r"\baccepted_delta=(-?\d+)")
STATUSES_RE = re.compile(r"\bstatuses=(\{[^}]*\})")
LANDED_DICT_RE = re.compile(r"\blanded=\d+\s+(\{[^}]*\})")
PASS_RE = re.compile(r"^(?P<name>\S+).*PASS=(?P<ok>True|False)$")


def _call(main: Any) -> tuple[int, list[str]]:  # noqa: ANN401  # probe main() shape varies
    """Run one probe's main in-process and return its exit code plus its captured stdout lines."""
    buffer = io.StringIO()
    argv = [] if "argv" in inspect.signature(main).parameters else None
    with contextlib.redirect_stdout(buffer):
        code = int(main(argv) if argv is not None else main())
    return code, buffer.getvalue().splitlines()


def _field(lines: list[str], pattern: re.Pattern[str]) -> str:
    """First value of ``pattern`` in the run, or MISSING -- a silent zero is not a measurement."""
    for line in lines:
        match = pattern.search(line)
        if match:
            return match.group(1)
    return "MISSING"


def _face_line(lines: list[str], prefix: str) -> str:
    """The probe's own summary line, which carries its arms tally."""
    return next((line for line in lines if line.startswith(prefix)), "")


def _dict_field(lines: list[str], pattern: re.Pattern[str]) -> dict[str, int]:
    """Parse a printed dict field into one, reading the printed text rather than the constant."""
    raw = _field(lines, pattern)
    return dict(ast.literal_eval(raw)) if raw != "MISSING" else {}


def _failed_passes(lines: list[str]) -> list[str]:
    """Names of the printed arm lines whose ``PASS=`` reading is False."""
    failed: list[str] = []
    for line in lines:
        match = PASS_RE.match(line)
        if match and match.group("ok") == "False":
            failed.append(match.group("name"))
    return failed


def _statuses_pair(lines: list[str]) -> tuple[dict[str, int], dict[str, int]]:
    """(measured, asserted) status dicts read off one landed-face line; empty when not printed."""
    for line in lines:
        measured = STATUSES_RE.search(line)
        asserted = LANDED_DICT_RE.search(line)
        if measured and asserted:
            return (
                dict(ast.literal_eval(measured.group(1))),
                dict(ast.literal_eval(asserted.group(1))),
            )
    return {}, {}


def _aging_lines(first: list[str], last: list[str]) -> list[str]:
    """Lines that differ between two runs of the same probe in the same process."""
    if len(first) != len(last):
        return [f"LINE_COUNT {len(first)} -> {len(last)}"]
    return [f"{a!r} != {b!r}" for a, b in zip(first, last, strict=True) if a != b]


def _subject(spec: dict[str, str], lines: list[str], verdicts: list[bool]) -> None:
    """Run one subject probe three times, append its face lines and its six arm verdicts."""
    module = importlib.import_module(spec["module"])
    shipped = getattr(module, spec["constant"])
    previous = getattr(module, spec["previous"])

    first_code, first_out = _call(module.main)
    setattr(module, spec["constant"], previous)
    forged_code, forged_out = _call(module.main)
    setattr(module, spec["constant"], shipped)
    third_code, third_out = _call(module.main)

    first_face = _face_line(first_out, spec["face_prefix"])
    forged_face = _face_line(forged_out, spec["face_prefix"])
    failed = _failed_passes(forged_out)
    aging = _aging_lines(first_out, third_out)
    measured, asserted = _statuses_pair(forged_out)
    first_measured, _ = _statuses_pair(first_out)

    arm_a = first_code == 0 and "arms_failed=0" in first_face
    arm_b = forged_code != 0 and "arms_failed=0" not in forged_face
    arm_c = failed == [spec["landed_line"]]
    counters: list[tuple[str, str, str]] = [
        ("residue", _field(first_out, RESIDUE_RE), _field(forged_out, RESIDUE_RE)),
        ("identity", _field(first_out, IDENTITY_RE), _field(forged_out, IDENTITY_RE)),
    ]
    # A dict-shaped landed face publishes its count inside statuses= rather than as a bare accepted=
    # field, so the counter this arm compares has to be the one the subject probe really prints --
    # requiring accepted= from both probes is how take 1 read its own false negative.
    if isinstance(previous, int):
        counters.append(
            ("accepted", _field(first_out, ACCEPTED_RE), _field(forged_out, ACCEPTED_RE))
        )
    else:
        counters.append(("statuses", str(first_measured), str(measured)))
    arm_d = all(before == after and before not in {"MISSING", ""} for _, before, after in counters)
    arm_e = third_code == first_code and all("granted=" in line for line in aging)
    if isinstance(previous, int):
        raw_accepted = _field(forged_out, ACCEPTED_RE)
        reported = int(raw_accepted) if raw_accepted != "MISSING" else -1
        arm_f = (
            reported == EXPECTED_ACCEPTED
            and str(previous) != str(reported)
            and _field(first_out, ACCEPTED_DELTA_RE) == str(EXPECTED_ACCEPTED - int(previous))
        )
    else:
        delta = _dict_field(first_out, DELTA_RE)
        arm_f = (
            delta == EXPECTED_DELTA
            and sum(delta.values()) == 0
            and bool(measured)
            and bool(asserted)
            and measured != asserted
            and measured.get("OFFLINE_ACCEPTED") == EXPECTED_ACCEPTED
            and asserted.get("OFFLINE_ACCEPTED") == previous["OFFLINE_ACCEPTED"]
        )

    label = spec["module"]
    lines.append(f"{label}: constant={spec['constant']} shipped={shipped} forged_to={previous}")
    lines.append(f"{label}: exits first={first_code} forged={forged_code} restored={third_code}")
    lines.append(f"{label}: shipped_face {first_face}")
    lines.append(f"{label}: forged_face {forged_face}")
    lines.append(f"{label}: forged_failed_arms={failed}")
    lines.append(
        f"{label}: counters "
        + " ".join(f"{name}={before}/{after}" for name, before, after in counters)
    )
    if isinstance(previous, int):
        lines.append(
            f"{label}: accepted_delta={_field(first_out, ACCEPTED_DELTA_RE)} "
            f"forged_line={next((row for row in forged_out if row.startswith('A_landed')), 'NA')}"
        )
    else:
        lines.append(
            f"{label}: rebase_delta={_field(first_out, DELTA_RE)} "
            f"forged_measured={measured} forged_asserted={asserted}"
        )
    lines.append(
        f"{label}: aging_lines={len(aging)} "
        f"all_grant_lines={all('granted=' in text for text in aging)} "
        f"sample={aging[:1]}"
    )
    lines.append(
        f"{label}_arms A_unmodified_green={arm_a} B_forged_previous_red={arm_b} "
        f"C_only_landed_arm_moves={arm_c} D_measurement_unchanged={arm_d} "
        f"E_restored_green_and_aging_named={arm_e} F_delta_matches_committed_face={arm_f}"
    )
    verdicts.extend([arm_a, arm_b, arm_c, arm_d, arm_e, arm_f])


def main(argv: list[str] | None = None) -> int:
    """Run each probe three times and gate the shipped/forged/restored comparison."""
    argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0]).parse_args(argv)
    lines: list[str] = [
        "wave-28 step 4b -- landed-face re-base, with the forged arm that proves it still bites.",
        "argv: PYTHONPATH= $PY docs/evidence/C128/plane1-residue-readiness-20261017/probes/"
        "landed_face_rebase.py",
        "runs: each subject probe's main() called three times here over the same report bytes -- "
        "shipped landed constant, then that constant forged back to the wave-28 step-3 "
        "generation, then restored. No product file is written; the only file this probe writes is "
        "its carrier.",
        "self-reference: the two subject probes were edited before run 1, so their line/sha pins "
        "move with this step and are re-pinned in docs/evidence/C128/README.md.",
    ]
    verdicts: list[bool] = []
    for spec in SUBJECTS:
        _subject(spec, lines, verdicts)

    passed = sum(1 for ok in verdicts if ok)
    lines.append(
        f"landed_face_rebase_face subjects={len(SUBJECTS)} arms={len(verdicts)} "
        f"arms_passed={passed} arms_failed={len(verdicts) - passed} "
        f"PASS={passed == len(verdicts)}"
    )
    lines.append("chain_exit=0" if passed == len(verdicts) else "chain_exit=1")
    CARRIER.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines[-12:]:
        print(line)
    print(f"carrier_written bytes={CARRIER.stat().st_size} lines={len(lines)}")
    return 0 if passed == len(verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
