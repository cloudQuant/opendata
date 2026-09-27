#!/usr/bin/env python3
"""Read the evidence archive the way a fresh clone would (验收 AC-17「可追溯」).

Why this file exists
--------------------
``docs/迭代计划/迭代1-重构数据中台/验收文档.md`` line 269 asks for
``历次里程碑的验证证据（命令 + 输出摘要 + 日期）可追溯``. Through round C42 that wording had
never been measured. ``ledger-check`` proves that a ``proven`` entry names files that exist
and are tracked, but it only reads the paths the ledger happens to cite -- the other ~400
files under ``docs/evidence/`` were never asked whether they say *when* they were produced,
*from which* branch and commit, *by which* command, or *with what* exit status.

What this assertion owns
------------------------
* **round**: every ``docs/evidence/<round>/`` carries at least one ``.md`` narrative. A
  directory of bare readings is a pile of output, not a milestone record.
* **gate log**: a text reading that actually contains the make harness's own
  ``===== gate:`` section markers carries a date (in its first ``HEADER_LINES`` lines, or
  printed by the run itself), a commit-ish identity and the command that produced it, plus an
  explicit exit reading somewhere in the file. "The gate was green" has to be checkable from
  the archive itself -- C38b learned the hard way that a backgrounded command's own exit code
  says nothing about the run inside it. A branch name is counted separately and is *not* a
  violation: branches are mutable, only the commit pins the tree the run was made from.
* **presence**: an evidence reading git has never seen is not evidence. Tracked files are
  reported as such; ``*.log`` is ignored by ``.gitignore``, so an archived ``.log`` that was
  never force-added would read as absent here.

Deliberate limits
-----------------
Only gate logs are asked for a header: a milestone's manual probe or test tail has no
make output to carry, so charging it for a missing ``GATE_EXIT=`` line would be a finding
about the file type rather than about the run. Rounds before C14 predate the header
convention, and six of their logs (A2, A4, A5, C4, C9, C13) answer "when" only from a
timestamp the run itself printed -- that is accepted, and git's first-add commit is printed
as a further loose bound for anything still undated. What is *not* done is editing an old log
to add a header: that would make the archive describe a run that no longer happened that way.
Header dates come in two dialects (ISO ``2026-09-27`` and ``ctime`` like
``Fri Sep 25 12:15:10 +08 2026``) and both are accepted -- rejecting the older spelling would
be a formatting preference dressed up as a finding. Whatever gaps survive are frozen in
``docs/quality/evidence-traceability.json`` with the same "only down" semantics as the
quality ratchet: a new violation fails, a repaired one is reported ``(improved)`` until
``--update`` freezes the smaller list. This tool can never make a milestone's evidence true;
it only refuses to let the archive rot quietly.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = "docs/evidence"
BASELINE_PATH = "docs/quality/evidence-traceability.json"
BASELINE_VERSION = 1
GIT: Final = shutil.which("git")

HEADER_LINES = 40
TEXT_SUFFIXES = (".txt", ".log", ".md", ".json", ".py", ".sh", ".yaml", ".snippet", ".diff")
NARRATIVE_SUFFIXES = (".md",)
FACES: Final = ("narrative", "date", "identity", "command", "exit", "untracked")

ISO_DATE = re.compile(r"20\d\d-\d\d-\d\d")
CTIME_DATE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?:Jan|Feb|Mar|Apr|May|Jun|"
    r"Jul|Aug|Sep|Oct|Nov|Dec) \d\d (?:\d\d:)?\d\d:\d\d"
)
# A timestamp the *run itself* printed (loguru lines, npm/pytest clocks) is a stronger claim
# about when the run happened than a header a human typed afterwards. Six archives -- A2, A4,
# A5, C4, C9, C13 -- predate the header convention and were first reported by this tool as
# date gaps; reading them showed each carries its own ``2026-.. ..:..:..`` stamp in the body,
# so the header-only rule was measuring where the date sits rather than whether the archive
# answers "when". That gap is deleted by narrowing the tool, not healed by editing an archive.
RUN_STAMP = re.compile(r"20\d\d-\d\d-\d\d[ T]\d\d:\d\d:\d\d")
BRANCH = re.compile(r"\bbranch\b|分支", re.I)
# A commit pins the tree; a branch name is mutable. Only the commit is charged to the
# archive when a legacy header left the branch out.
COMMIT_REF = re.compile(r"\bHEAD\b|\bcommit\b|\brev\b|提交", re.I)
COMMAND = re.compile(
    r"(?:^|\s)(?:\$ )?(?:make\b|python\b|npm\b|npx\b|pytest\b|git\b)"
    r"|^\s*(?:#+\s*)?(?:command|命令)\s*[:：]",
    re.M,
)
# The run's own verdict, spelled four ways across five weeks of archives: A0 wrote
# ``gate_exit=0``, B4 ``gate exit=0``, later rounds ``GATE_EXIT=0``, and every make run
# ends with the PASSED section marker.
EXIT_MARKER = re.compile(r"\b(?:\w*_)?(?:exit|rc)\s*=\s*-?\d+", re.I)
PASSED_MARKER = re.compile(r"===== gate: PASSED =====")
GATE_SECTION = re.compile(r"^===== gate:", re.M)


class TraceabilityError(RuntimeError):
    """The archive or its baseline is absent or malformed."""


@dataclass(frozen=True)
class Reading:
    """One evidence file, and what a reader can recover from it without asking anyone."""

    rel: str
    round_name: str
    is_gate_log: bool
    in_header: str
    whole: str
    tracked: bool

    @property
    def dated_in_header(self) -> bool:
        """Whether the archive's own first lines say which day it was produced."""
        return bool(ISO_DATE.search(self.in_header) or CTIME_DATE.search(self.in_header))

    @property
    def faces(self) -> set[str]:
        """Which traceability faces this file answers for."""
        missing: set[str] = set()
        if not self.tracked:
            missing.add("untracked")
        if self.is_gate_log:
            if not self.dated_in_header and not RUN_STAMP.search(self.whole):
                missing.add("date")
            if not COMMIT_REF.search(self.in_header):
                missing.add("identity")
            if not COMMAND.search(self.in_header):
                missing.add("command")
            if not (EXIT_MARKER.search(self.whole) or PASSED_MARKER.search(self.whole)):
                missing.add("exit")
        return missing


def _git(*args: str) -> str:
    """Run git for a read-only census, or "" when git is unavailable or fails."""
    if GIT is None:
        return ""
    done = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
        [GIT, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    return done.stdout if done.returncode == 0 else ""


def _tracked_files() -> frozenset[str]:
    """Every path under the evidence directory that a fresh clone would contain."""
    listed = _git("ls-files", "--", EVIDENCE_DIR)
    if not listed and GIT is not None:
        raise TraceabilityError(f"git listed no tracked file under {EVIDENCE_DIR}/")
    return frozenset(line for line in listed.splitlines() if line)


def _read_file(path: Path, tracked: frozenset[str]) -> Reading:
    """Load one archive and classify it (gate log or plain reading)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # an archive we cannot open cannot vouch for anything
        raise TraceabilityError(f"cannot read evidence file {path}: {exc}") from exc
    rel = path.relative_to(REPO_ROOT)
    parts = rel.parts  # docs/evidence/<round>/<file>
    return Reading(
        rel=rel.as_posix(),
        round_name=parts[2] if len(parts) > 3 else "",
        # A gate log is one that prints the gate's own section markers, not one whose name
        # happens to contain "gate" -- C20's run-comparison table would otherwise be asked
        # for a header it has no business carrying.
        is_gate_log=path.suffix in {".txt", ".log"} and bool(GATE_SECTION.search(text)),
        in_header="\n".join(text.splitlines()[:HEADER_LINES]),
        whole=text,
        tracked=rel.as_posix() in tracked,
    )


def _rounds(readings: list[Reading]) -> set[str]:
    """Every round the archive holds a file for."""
    return {reading.round_name for reading in readings if reading.round_name}


def _added_dates() -> dict[str, str]:
    """First commit that introduced each evidence path, recovered from git in one pass.

    This is the loose reading of "traceable": an archive whose header predates the header
    convention still carries a date as long as git has seen it. It is reported, never
    charged as a violation, because rewriting a five-week-old log to add one would make the
    archive describe a run that no longer happened that way.
    """
    listed = _git(
        "log",
        "--reverse",
        "--diff-filter=A",
        "--name-only",
        "--date=short",
        "--format=x\t%ad",
        "--",
        EVIDENCE_DIR,
    )
    out: dict[str, str] = {}
    stamp = ""
    for line in listed.splitlines():
        if line.startswith("x\t"):
            stamp = line[2:]
        elif line and stamp and line not in out:
            out[line] = stamp
    return out


def collect() -> tuple[list[Reading], dict[str, list[str]]]:
    """Census the archive: every reading, and every violation keyed for diffing."""
    root = REPO_ROOT / EVIDENCE_DIR
    if not root.is_dir():
        raise TraceabilityError(f"{EVIDENCE_DIR}/ is missing")
    tracked = _tracked_files()
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES)
    readings = [_read_file(p, tracked) for p in files]

    violations: dict[str, list[str]] = {}
    for reading in readings:
        for face in sorted(reading.faces):
            violations.setdefault(face, []).append(reading.rel)
    narratives = {r.round_name for r in readings if Path(r.rel).suffix in NARRATIVE_SUFFIXES}
    for name in sorted(_rounds(readings) - narratives):
        violations.setdefault("narrative", []).append(f"{EVIDENCE_DIR}/{name}/")
    for face in violations:
        violations[face].sort()
    return readings, violations


def _flat(violations: dict[str, list[str]]) -> set[str]:
    """Flatten to ``path#face`` so the baseline is one sorted, diffable list."""
    return {f"{rel}#{face}" for face, names in violations.items() for rel in names}


def _load_baseline() -> list[str] | None:
    """The frozen violation set, or ``None`` when nothing has been frozen at all.

    ``None`` and ``[]`` are different claims and the caller has to tell them apart. C43 froze an
    empty ceiling, so an empty list now means "this repository considers itself clean, and any
    finding is new damage" -- while a missing file means "no ceiling was ever agreed on". Reading
    the two as one made a fresh violation print ``run with --update once the list above is
    reviewed``, which is advice to freeze the damage into the ceiling.
    """
    path = REPO_ROOT / BASELINE_PATH
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TraceabilityError(f"{BASELINE_PATH} is not readable JSON: {exc}") from exc
    if data.get("version") != BASELINE_VERSION:
        raise TraceabilityError(f"{BASELINE_PATH} has version {data.get('version')!r}")
    return [str(item) for item in data.get("violations", [])]


def _write_baseline(flat: set[str]) -> None:
    """Freeze the current reading as the ceiling new rounds may not raise."""
    legacy = (
        "证据可追溯面的存量快照，语义与 quality ratchet 相同：只降不升。"
        "列出的档案是本轮之前留下的，工具不假装它们合规；"
        "任何**新增**违规都会让 make gate 变红，被修掉的会打印 (improved) 直到 --update 冻结。"
    )
    empty = (
        "C43 首次冻结时六个面（narrative/date/identity/command/exit/untracked）读数为 0，"
        "所以这份清单是空的 —— 空表不代表历史上从未有缺口（A3/B3/C1 三份里程碑说明是 C43 "
        "补写的，六份早期 gate 日志的日期靠运行自身打印的时间戳而非表头），"
        "只代表补齐之后仓库此刻没有可归因的缺口。此后任何一条**新增**违规都会让 make gate 变红。"
    )
    payload = {
        "version": BASELINE_VERSION,
        "doc": "docs/迭代计划/迭代1-重构数据中台/验收文档.md",
        "faces": list(FACES),
        "violations": sorted(flat),
        "note": legacy if flat else empty,
    }
    path = REPO_ROOT / BASELINE_PATH
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(update: bool) -> int:
    """Print the census and judge it against the frozen baseline."""
    readings, violations = collect()
    flat = _flat(violations)
    gate_logs = [r for r in readings if r.is_gate_log]
    print(f"# evidence traceability over {EVIDENCE_DIR}/")
    print(f"  rounds        = {len(_rounds(readings))}")
    print(f"  files census  = {len(readings)} (gate logs: {len(gate_logs)})")
    header_dated = sum(1 for r in gate_logs if r.dated_in_header)
    run_dated = sum(1 for r in gate_logs if not r.dated_in_header and RUN_STAMP.search(r.whole))
    print(
        f"  dates in gate logs = header {header_dated}/{len(gate_logs)}, "
        f"printed by the run {run_dated}/{len(gate_logs)}"
    )
    print(
        f"  branch named in header = {sum(1 for r in gate_logs if BRANCH.search(r.in_header))}"
        f"/{len(gate_logs)} (informational; a commit pins the tree, a branch does not)"
    )
    for face in FACES:
        print(f"  {face:<10} gaps = {len(violations.get(face, []))}")
    added_on = _added_dates()
    for face in FACES:
        for rel in violations.get(face, []):
            note = ""
            if face == "date":
                seen = added_on.get(rel)
                note = (
                    f"   (git first saw it on {seen})"
                    if seen
                    else "   (no date, in-file or in git)"
                )
            print(f"    {face}: {rel}{note}")

    if update:
        _write_baseline(flat)
        print(f"\nbaseline frozen at {len(flat)} violation(s) -> {BASELINE_PATH}")
        return 0

    frozen = _load_baseline()
    if frozen is None:
        print("\nFAIL: no baseline yet; run with --update once the list above is reviewed.")
        return 1
    baseline = set(frozen)
    new = sorted(flat - baseline)
    healed = sorted(baseline - flat)
    for item in new:
        print(f"NEW VIOLATION: {item}")
    for item in healed:
        print(f"(improved) {item}")
    if new:
        print(
            f"\nFAIL: {len(new)} new traceability violation(s)"
            f" against a {len(baseline)}-entry baseline."
        )
        return 1
    if healed:
        print(
            f"\nOK: within the frozen ceiling; {len(healed)} entry(ies) healed"
            " -- run --update to freeze the smaller list."
        )
        return 0
    print(f"\nOK: evidence archive matches the frozen baseline ({len(baseline)} legacy entr(ies)).")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="freeze the current reading")
    args = parser.parse_args(argv)
    try:
        return run(update=args.update)
    except TraceabilityError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
