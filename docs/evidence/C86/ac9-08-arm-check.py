#!/usr/bin/env python3
"""Do AC-9|08's counterfact arms still bite after its reason prose became fact-derived?

The round fixed a defect at its source: the shipped judge used to print a key-collision count that
neither its own measure nor any archived reading produces (the number came from a unit fixture, not
the warehouse census), so the ledger could claim a cause the gate never measured. The fix replaced
that prose with three facts read out of ``docs/evidence/C49/census-post-fix.txt``. A prose-only
change is not self-evidently arm-preserving, so this instrument re-runs the cell's whole
counterfact set against the shipped ``Probe`` object rather than asserting it still works:

``[1]`` each census marker the reason quotes resolves to digits on a named line -- a silent ``-``
    would print as a plausible row count, which is the failure shape the fix is meant to avoid;
``[2]`` the repaired reading reaches ``proven``, i.e. the judge is still passable by real work;
``[3]`` every declared break, applied to that repaired reading, flips it back to ``gap``;
``[4]`` the two facts the judge does *not* read, tampered on the same repaired reading, must leave
    it ``proven``. Without this arm ``[3]`` would be vacuous: if any mutation at all pushed the
    state to ``gap``, "8/8 arms flip" would say nothing about which face gates the cell;
``[5]`` the refuted claim is gone from the module source, scanned by the same routine that returns
    a non-zero count for a phrase the reason really does contain -- a zero needs its non-zero arm;
``[6]`` the reading/reason the cell now publishes, verbatim from the shipped judge.

Run: python3 docs/evidence/C86/ac9-08-arm-check.py
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
CENSUS_REL: Final = "docs/evidence/C49/census-post-fix.txt"
ITEM: Final = "AC-9|08"
REFUTED: Final = "55,073 行里 1 个键两义"
PRESENT: Final = "dwd_stock_action 实测"
MARKERS: Final = {
    "landed_rows": "dwd 表 dwd_stock_action 实际行数",
    "passthrough_rows": "直通 merge：rows",
    "reader_rows": "生产 reader 产出：",
}


def load_module() -> ModuleType:
    """Import the shipped probe by path, so its PROBES/judge/resolvers are the repo's own."""
    path = REPO / MODULE_REL
    spec = importlib.util.spec_from_file_location("acceptance_item_probe", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load probe module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def census_lines(text: str) -> dict[str, tuple[int, str]]:
    """Map each marker to the (1-based line, full line) it first appears on."""
    found: dict[str, tuple[int, str]] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        for key, marker in MARKERS.items():
            if key not in found and marker in line:
                found[key] = (number, line.strip())
    return found


def digits_of(value: str) -> bool:
    """Is this fact a real number rather than the resolver's ``-`` fallback."""
    return bool(re.fullmatch(r"\d+", value))


def scan_source(source: str) -> tuple[int, int]:
    """Count the refuted claim and the present phrase in the shipped module."""
    return source.count(REFUTED), source.count(PRESENT)


def main() -> int:
    """Re-run the cell's arms and its prose faces against the shipped probe."""
    module = load_module()
    probe = module.probe_for(ITEM)
    ctx = module.load_context()
    facts: dict[str, str] = dict(probe.measure(ctx))
    resolve_repair = module.resolve_repair
    resolve_break = module.resolve_break
    proven = module.PROVEN
    gap = module.GAP
    problems: list[str] = []

    census = (REPO / CENSUS_REL).read_text(encoding="utf-8")
    lines = census_lines(census)
    print(f"[1] census {CENSUS_REL} markers, denominator={len(MARKERS)}")
    for key, marker in MARKERS.items():
        value = facts.get(key, "<absent>")
        where = lines.get(key)
        print(f"    {key} = {value!r} from marker {marker!r} at line {where[0] if where else '-'}")
        if where is None:
            problems.append(f"{key}: marker {marker!r} is not in the archived census")
        elif not digits_of(value):
            problems.append(f"{key}: resolves to {value!r}, not digits -- prose would quote a dash")
        elif where[1] and value not in where[1]:
            problems.append(f"{key}: {value!r} does not appear on the census line it cites")

    base = resolve_repair(facts, probe.repair)
    base_state = str(probe.judge(base).state)
    print(f"[2] reachability: repaired reading -> {base_state} (needs {proven})")
    if base_state != proven:
        problems.append(f"repaired reading reads {base_state}, so this cell can never be closed")

    print(f"[3] declared breaks: {len(probe.breaks)} arms, each applied to the repaired reading")
    flipped = 0
    for brk in probe.breaks:
        mutated = resolve_break(base, brk.facts)
        state = str(probe.judge(mutated).state)
        if state == gap:
            flipped += 1
        else:
            problems.append(f"{brk.label}: still {state}, so the cell is not gated on this face")
        keys = ", ".join(f"{k}={v}" for k, v in brk.facts)
        print(f"    {'GAP' if state == gap else state:>4}  {brk.label}  [{keys}]")

    judged = {k for brk in probe.breaks for k, _ in brk.facts} | set(probe.repair)
    unjudged = sorted(set(facts) - judged)
    print(f"[4] null arms: {len(unjudged)} facts no break or repair names, on the repaired reading")
    for key in unjudged:
        mutated = dict(base)
        mutated[key] = "1" if not digits_of(str(facts.get(key, ""))) else "999999"
        state = str(probe.judge(mutated).state)
        if state != proven:
            problems.append(
                f"{key}: a fact no break or repair names still moved the verdict to {state}, "
                f"so arm [3]'s flips are not attributable to the faces they tamper"
            )
        print(f"    {state:>6}  tamper {key} -> {mutated[key]!r}")

    source = (REPO / MODULE_REL).read_text(encoding="utf-8")
    refuted_hits, present_hits = scan_source(source)
    print(f"[5] source scan: refuted={refuted_hits} present={present_hits}")
    if refuted_hits != 0:
        problems.append(f"the refuted claim is still in {MODULE_REL} ({refuted_hits}x)")
    if present_hits < 1:
        problems.append(f"no positive control: {PRESENT!r} absent, so that zero proves nothing")

    verdict: Any = probe.judge(facts)
    reason = str(verdict.reason)
    print(f"[6] published prose, state={verdict.state} (reason length={len(reason)})")
    for line in list(verdict.readings) + [reason]:
        print(f"    | {line}")
    for key in MARKERS:
        token = str(facts.get(key, ""))
        if digits_of(token) and token not in reason and verdict.state == gap:
            problems.append(f"reason quotes none of {key}={token} while still reading gap")

    tally = (
        f"markers={len(lines)}/{len(MARKERS)} repaired->{base_state} "
        f"arms={flipped}/{len(probe.breaks)} null={len(unjudged)} "
        f"source(refuted={refuted_hits},present={present_hits})"
    )
    if problems:
        for problem in problems:
            print(f"PROBLEM {problem}")
        print(f"{tally}\nAC9_08_ARMS_CHECK FAIL problems={len(problems)}")
        return 1
    print(f"{tally}\nAC9_08_ARMS_CHECK PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
