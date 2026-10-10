#!/usr/bin/env python3
"""Does AC-1|05's new blocker prose say anything the gate did not measure?

The round found this cell's judge reason was a zero-digit English template: it named no reading at
all, so a reader could not tell which half of the criterion was red or what to fix. The fix makes
the judge classify its own runtime evidence issues by code and publish the class census. A prose
change is not self-evidently measurement-preserving, so this instrument checks the census against
the validator's own issue list and checks that every number the prose prints is traceable:

``[1]`` the class census recomputed a second time from ``runtime_stack_evidence.validate``, with a
    hand-rolled dict and a set instead of the probe's ``Counter`` -- the totals and per-code counts
    have to agree, and the sum over codes has to equal the number of issues (part/whole identity);
``[2]`` the "最多的三类" string is re-sorted independently by ``(-count, code)`` and compared
    against what the probe published, so the superlative is checked by its own stated sort key;
``[3]`` the identity face can differ: the shipped reading prints 成立, and a forged class sum one
    off prints 不成立. A face that can never say otherwise witnesses nothing;
``[4]`` arm coherence: the repaired reading reaches ``proven`` and every declared break flips it to
    ``gap``, *and* each arm's generated prose must stay arithmetically self-consistent (the count
    the reason cites equals the count the reading cites). The reason now reads four facts, so the
    arms have to set all four together -- otherwise an arm would print "1 条分 9 类";
``[5]`` the digit census: the published reason must contain digits (the defect being fixed was a
    template with none), and every digit run in it must appear in one of that reading's own facts;
``[6]`` the readings and reason verbatim from the shipped judge.

Run: python3 docs/evidence/C86/ac1-05-census-face.py
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from types import ModuleType

REPO: Final = pathlib.Path(__file__).resolve().parents[3]
MODULE_REL: Final = "scripts/quality/acceptance_item_probe.py"
ITEM: Final = "AC-1|05"
CENSUS_KEYS: Final = (
    "runtime_issue_count",
    "runtime_issue_class_count",
    "runtime_issue_class_sum",
    "runtime_issue_top_classes",
)
COUNT_RE: Final = re.compile(r"证据问题 (\d+) 条分 (\d+) 类")
IDENTITY_RE: Final = re.compile(r"各类条数合计 (\d+) = 总条数 (\d+)（(成立|不成立)）")
NUMBER_RE: Final = re.compile(r"\d+")


def load_module() -> ModuleType:
    """Import the shipped probe by path, so its judge/resolvers are the repository's own."""
    path = REPO / MODULE_REL
    spec = importlib.util.spec_from_file_location("acceptance_item_probe", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load probe module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def hand_census() -> dict[str, int]:
    """Re-count the validator's issues by code without Counter and without the probe's helper."""
    sys.path.insert(0, str(REPO))
    try:
        from scripts.quality.runtime_stack_evidence import validate
    finally:
        sys.path.remove(str(REPO))
    tally: dict[str, int] = {}
    for issue in validate(REPO).issues:
        tally[issue.code] = tally.get(issue.code, 0) + 1
    return tally


def published_top3(tally: dict[str, int]) -> str:
    """Render the top three under the sort key the probe states: count desc, then code asc."""
    ordered = sorted(tally.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
    return ", ".join(f"{code}={n}" for code, n in ordered) or "none"


def identity_line(readings: tuple[str, ...]) -> str:
    """The reading that carries the part/whole identity, or an empty string."""
    for text in readings:
        if IDENTITY_RE.search(text):
            return text
    return ""


def prose_checks(reason: str, facts: dict[str, str]) -> list[str]:
    """Require the reason's digits to be measured digits, not a template's."""
    problems: list[str] = []
    digits = NUMBER_RE.findall(reason)
    if not digits:
        problems.append(
            f"reason carries no digits at all ({len(reason)} chars), so it names no "
            "measurement -- the defect shape this round fixed"
        )
    problems += [
        f"reason prints {token}, which appears in none of the reading's facts"
        for token in digits
        if not any(token in value for value in facts.values())
    ]
    return problems


def main() -> int:
    """Re-measure the cell's census two ways and re-run its arms against the shipped probe."""
    module = load_module()
    probe = module.probe_for(ITEM)
    ctx = module.load_context()
    facts: dict[str, str] = dict(probe.measure(ctx))
    resolve_repair = module.resolve_repair
    resolve_break = module.resolve_break
    proven = module.PROVEN
    gap = module.GAP
    problems: list[str] = []

    print(f"[1] census facts as the shipped probe measured them, denominator={len(CENSUS_KEYS)}")
    for key in CENSUS_KEYS:
        print(f"    {key} = {facts.get(key, '<absent>')!r}")

    tally = hand_census()
    hand_total = sum(tally.values())
    hand_classes = len(set(tally))
    print(f"[2] hand recount: {hand_classes} codes, {hand_total} issues")
    for code, n in sorted(tally.items(), key=lambda pair: (-pair[1], pair[0])):
        print(f"    {code} = {n}")
    if facts["runtime_issue_class_count"] != str(hand_classes):
        problems.append(
            f"probe reads {facts['runtime_issue_class_count']} classes, recount reads "
            f"{hand_classes}"
        )
    if facts["runtime_issue_count"] != str(hand_total):
        problems.append(
            f"probe reads {facts['runtime_issue_count']} issues, recount reads {hand_total}"
        )
    if facts["runtime_issue_class_sum"] != str(hand_total):
        problems.append(
            f"the probe's own class sum {facts['runtime_issue_class_sum']} != issues {hand_total}"
        )
    top3 = published_top3(tally)
    print(f"[3] top-3 under (-count, code): {top3}")
    print(f"    probe published:            {facts['runtime_issue_top_classes']}")
    if top3 != facts["runtime_issue_top_classes"]:
        problems.append("the published 最多的三类 is not the top three under its stated sort key")

    verdict: Any = probe.judge(facts)
    live = identity_line(tuple(verdict.readings))
    forged = dict(facts)
    forged["runtime_issue_class_sum"] = str(int(facts["runtime_issue_class_sum"]) + 1)
    broken = identity_line(tuple(probe.judge(forged).readings))
    print("[4] the identity face, two-sided")
    print(f"    as measured: | {live}")
    print(f"    forged +1:   | {broken}")
    if "（成立）" not in live:
        problems.append(f"the measured reading does not print 成立: {live!r}")
    if "（不成立）" not in broken:
        problems.append(
            f"the forged reading does not print 不成立, so the face never bites: {broken!r}"
        )

    clean = resolve_repair(facts, probe.repair)
    clean_verdict: Any = probe.judge(clean)
    print(f"[5] reachability: repaired reading -> {clean_verdict.state} (needs {proven})")
    if clean_verdict.state != proven:
        problems.append(
            f"repaired reading reads {clean_verdict.state}, never passable by real work"
        )
    arms: list[tuple[str, str, Any]] = [(str(clean_verdict.state), "repaired", clean_verdict)]
    for brk in probe.breaks:
        mutated = resolve_break(clean, brk.facts)
        arm: Any = probe.judge(mutated)
        arms.append((str(arm.state), brk.label, arm))
        if arm.state != gap:
            problems.append(f"{brk.label}: still {arm.state}, so the cell is not gated here")
    flipped = sum(1 for state, _, _ in arms[1:] if state == gap)
    print(f"    breaks: {flipped}/{len(probe.breaks)} flip the repaired reading back to gap")
    for state, label, arm in arms:
        reason = str(arm.reason)
        line = identity_line(tuple(arm.readings))
        cited = COUNT_RE.search(reason)
        pair = IDENTITY_RE.search(line)
        shown = f"reason cites {cited.group(1)} 条/{cited.group(2)} 类" if cited else "no census"
        holds = pair[3] if pair else "<none>"
        print(f"    [{state:>5}] {label}: identity prints {holds}; {shown}")
        if pair and pair[1] != pair[2]:
            problems.append(f"{label}: the arm prints a part/whole mismatch: {line}")
        if cited and pair and cited.group(1) != pair[2]:
            problems.append(
                f"{label}: reason says {cited.group(1)} 条 while the reading says 总条数 {pair[2]}"
            )

    print(f"[6] published prose, state={verdict.state} (reason length={len(str(verdict.reason))})")
    for line in list(verdict.readings) + [str(verdict.reason)]:
        print(f"    | {line}")
    if verdict.state == gap:
        problems += prose_checks(str(verdict.reason), facts)

    top3_match = top3 == facts["runtime_issue_top_classes"]
    reason_digits = len(NUMBER_RE.findall(str(verdict.reason)))
    tally_line = (
        f"codes={hand_classes} issues={hand_total} top3_match={top3_match} "
        f"arms={flipped}/{len(probe.breaks)} reason_digits={reason_digits}"
    )
    if problems:
        for problem in problems:
            print(f"PROBLEM {problem}")
        print(f"{tally_line}\nAC1_05_CENSUS_CHECK FAIL problems={len(problems)}")
        return 1
    print(f"{tally_line}\nAC1_05_CENSUS_CHECK PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
