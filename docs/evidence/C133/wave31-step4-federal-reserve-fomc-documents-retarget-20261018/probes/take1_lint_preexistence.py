r"""Attribute each of this wave's four take-1 ruff findings to HEAD or to this wave.

The chain's first run went red on four lint findings in
:file:`scripts/quality/iter2_witnesses/_parts/federal_reserve/rows_a.py`, and the face says only
"Found 4 errors". Two readings are possible -- the retarget created them, or two of them were
already sitting in that file and this wave merely paid for them -- and the carrier cannot tell them
apart. This probe prices the difference by running the same detector over bytes it fetches itself:
``git show HEAD:<path>`` for the pre-wave copy and the worktree copy for the after-state, both fed
to ``ruff check --stdin-filename <path> -`` so the project's own rule config applies to each.

Arms:

* A -- findings on HEAD's copy, with rule codes and locations.
* B -- the same detector over HEAD's copy plus a forged overlong line, in two shapes: a long
  string-literal statement (the shape take-1 charged) must raise the ``E501`` count by exactly one,
  which is what makes arm A's zero a real zero rather than a pipe that never carried bytes; and a
  ``#``-only token run of the same length, which the shipped ruff did not charge at all.
* C -- the take-1 carrier's own finding list, parsed out of the committed text.
* D -- the set arithmetic that does the attribution: take-1 minus HEAD must be exactly the two
  ``E501``s, and HEAD minus take-1 must be empty (nothing this wave fixed was invented by it).
* E -- the after-state: zero findings on the worktree copy, so both pre-existing codes were cleared
  here rather than left behind.
* F -- the shipped green carrier agrees with arm E instead of merely being nearby.

Offline, read-only, and no shipped file is written: the only file this probe writes is its own
carrier under this wave's ``runs/``.
"""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404  # the probe's own shipped commands, list argv, shell never enabled
import sys
from collections import Counter
from pathlib import Path

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
PY = "/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python"
STEP4 = "docs/evidence/C133/wave31-step4-federal-reserve-fomc-documents-retarget-20261018"
PROBES = REPO / STEP4 / "probes"
RUNS = REPO / STEP4 / "runs"

sys.path.insert(0, str(PROBES))

from w31s4_run_chain import SHARD_PART, carrier  # noqa: E402

TARGET = SHARD_PART
TAKE1_CARRIER = RUNS / "wave31-step4-ruff_check_take1_PREFIXES.txt"
GREEN_CARRIER = RUNS / "wave31-step4-ruff_check.txt"
# Two forged overlong lines, because the detector charged one shape and exempted the other (both
# readings are printed in the face, not assumed): a ``#``-only comment with no internal space drew
# no E501 at all, while a long string-literal statement -- the shape take-1 charged -- did.
EXEMPT_LINE = "#" + "-" * 119
CODE_LINE = 'A = "' + "x" * 110 + '"'
LOCATION = re.compile(r"^\S+:(\d+):(\d+): ([A-Z]+[0-9]+) ")


def ruff_over(
    source: str, label: str, filename: str = TARGET
) -> tuple[Counter[str], list[str], bool]:
    """Run the shipped ruff over bytes handed in, returning codes, hits and parse agreement."""
    proc = subprocess.run(  # noqa: S603  # nosec B603  # list argv, this probe's own command
        [
            PY,
            "-m",
            "ruff",
            "check",
            "--output-format",
            "concise",
            "--stdin-filename",
            filename,
            "-",
        ],
        input=source,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": "", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    codes: Counter[str] = Counter()
    hits: list[str] = []
    for line in proc.stdout.splitlines():
        match = LOCATION.match(line)
        if match:
            codes[match.group(3)] += 1
            hits.append(f"{match.group(3)}@{match.group(1)}:{match.group(2)}")
    reported = re.search(r"Found (\d+) error", proc.stdout)
    count = int(reported.group(1)) if reported else 0
    agrees = sum(codes.values()) == count
    print(
        f"ruff_over label={label} exit={proc.returncode} fed_bytes={len(source.encode())} "
        f"fed_lines={len(source.splitlines())} codes={dict(sorted(codes.items()))} hits={hits} "
        f"ruff_reported={count} parsed_sum={sum(codes.values())} parser_agrees={agrees} "
        f"stderr_bytes={len(proc.stderr.encode())}",
        flush=True,
    )
    return codes, hits, agrees


def git_show_head(path: str) -> tuple[str, int]:
    """Read the committed copy of ``path`` out of HEAD without touching the worktree."""
    proc = subprocess.run(  # noqa: S603  # nosec B603  # read-only git plumbing, fixed list argv
        ["git", "show", f"HEAD:{path}"],  # noqa: S607  # nosec B607  # a PATH lookup, never input
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    print(
        f"git_show_head path={path} exit={proc.returncode} bytes={len(proc.stdout.encode())} "
        f"lines={len(proc.stdout.splitlines())} stderr_bytes={len(proc.stderr.encode())}",
        flush=True,
    )
    return proc.stdout, proc.returncode


def take1_codes() -> Counter[str]:
    """Parse the committed take-1 carrier for the rule codes it charged this wave."""
    text = TAKE1_CARRIER.read_text(encoding="utf-8")
    codes: Counter[str] = Counter(
        match.group(1) for match in re.finditer(r"^([A-Z]+[0-9]+) ", text, re.MULTILINE)
    )
    print(
        f"take1_codes carrier={TAKE1_CARRIER.name} codes={dict(sorted(codes.items()))} "
        f"count={sum(codes.values())} carries_red_exit={'ruff_check_exit=1' in text}",
        flush=True,
    )
    return codes


def main(argv: list[str]) -> int:
    """Price the four take-1 findings against HEAD and the worktree, and write the carrier."""
    note = (
        "take-1's four ruff findings attributed to HEAD versus introduced by this wave, by running "
        f"the shipped ruff over {TARGET} as git-show bytes and as worktree bytes"
    )
    out: list[str] = []

    head_source, head_rc = git_show_head(TARGET)
    worktree_source = (REPO / TARGET).read_text(encoding="utf-8")
    head_codes, head_hits, head_ok = ruff_over(head_source, "A_head_copy")
    exempt_codes, exempt_hits, exempt_ok = ruff_over(
        head_source + EXEMPT_LINE + "\n", "B1_head_copy_plus_token_run_comment"
    )
    code_line_codes, code_line_hits, code_ok = ruff_over(
        head_source + CODE_LINE + "\n", "B2_head_copy_plus_long_string_statement"
    )
    work_codes, work_hits, work_ok = ruff_over(worktree_source, "E_worktree_copy")
    own_path = Path(__file__)
    self_codes, self_hits, self_ok = ruff_over(
        own_path.read_text(encoding="utf-8"), "G_probe_self_bytes", filename=str(own_path)
    )
    t1_codes = take1_codes()
    green_text = GREEN_CARRIER.read_text(encoding="utf-8")

    gates = {
        "A_head_carries_two_of_four": {"D205", "PERF401"} <= set(head_codes),
        "B2_detector_live_on_the_charged_shape": code_line_codes["E501"] == head_codes["E501"] + 1,
        "B1_other_shape_exemption_recorded": exempt_codes["E501"] == head_codes["E501"],
        "C_take1_has_four_findings": sum(t1_codes.values()) == 4,
        "D_introduced_by_wave_is_exactly_two_E501": dict(t1_codes - head_codes) == {"E501": 2},
        "E_nothing_fixed_was_invented_by_this_wave": not head_codes - t1_codes,
        "F_worktree_cleared_every_code": not work_codes,
        "G_shipped_green_carrier_agrees": "All checks passed!" in green_text
        and "ruff_check_exit=0" in green_text,
        "H_head_and_worktree_are_different_bytes": head_source != worktree_source,
        "G_probe_itself_rule_clean": not self_codes,
        "I_git_show_retired_zero": head_rc == 0,
        "J_every_parse_agrees_with_ruffs_own_count": all(
            (head_ok, exempt_ok, code_ok, work_ok, self_ok)
        ),
    }

    out.append(
        "commands_run: argv-list git show HEAD:<target> (bytes handed to stdin in-process), then "
        f"argv-list {PY} -m ruff check --output-format concise --stdin-filename {TARGET} -; "
        "no shell pipeline is used"
    )
    out.append(f"A_head_findings codes={dict(sorted(head_codes.items()))} hits={head_hits}")
    exempt_delta = exempt_codes["E501"] - head_codes["E501"]
    code_delta = code_line_codes["E501"] - head_codes["E501"]
    out.append(
        f"B_forged B1_token_run_comment chars={len(EXEMPT_LINE)} E501_delta={exempt_delta} "
        f"hits={exempt_hits}"
    )
    out.append(
        f"B_forged B2_long_string_statement chars={len(CODE_LINE)} E501_delta={code_delta} "
        f"hits={code_line_hits}"
    )
    out.append(
        "B_reading=B2 is the shape take-1 charged, so arm A's E501 zero is a real zero; B1 shows a "
        "different overlong shape is exempted, which is why a control must match the charged shape"
    )
    out.append(f"C_take1_face codes={dict(sorted(t1_codes.items()))}")
    out.append(
        f"D_attribution take1_minus_head={dict(sorted((t1_codes - head_codes).items()))} "
        f"head_minus_take1={dict(sorted((head_codes - t1_codes).items()))} "
        f"reading=the E501 pair is this wave's own text; D205 and PERF401 predate it"
    )
    out.append(
        f"E_after_state worktree_codes={dict(sorted(work_codes.items()))} hits={work_hits} "
        f"head_lines={len(head_source.splitlines())} "
        f"worktree_lines={len(worktree_source.splitlines())}"
    )
    out.append(f"F_green_carrier first_line={green_text.splitlines()[0][:58]!r}")
    out.append(
        f"G_self_lint bytes={own_path.stat().st_size} codes={dict(sorted(self_codes.items()))} "
        f"hits={self_hits} reading=the same channel that charges the wave must charge this probe"
    )
    out.append(
        f"PREEXISTENCE_RULING head_pre_existing={sum(head_codes.values())} "
        f"introduced_by_this_wave={sum((t1_codes - head_codes).values())} "
        f"cleared_here={sum((t1_codes - work_codes).values())} "
        f"take1_total={sum(t1_codes.values())} "
        f"worktree_now={sum(work_codes.values())} "
        f"detector=ruff check over git-show bytes (same rule config, same stdin-filename path)"
    )
    out.append(
        f"preexistence_face=arms={len(gates)} arms_passed={sum(gates.values())} gates={gates} "
        f"all_pass={all(gates.values())}"
    )

    body = "\n".join(out)
    exit_code = 0 if all(gates.values()) else 1
    path = RUNS / "wave31-step4-take1_lint_preexistence.txt"
    path.write_text(
        carrier(
            {
                "name": "take1_lint_preexistence",
                "argv": [PY, str(Path(__file__))],
                "pythonpath": ".",
                "exit": exit_code,
                "stderr": "",
                "stdout": body,
                "note": note,
            }
        ),
        encoding="utf-8",
    )
    print(body, flush=True)
    print(f"carrier_written bytes={path.stat().st_size} path={path.relative_to(REPO)}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
