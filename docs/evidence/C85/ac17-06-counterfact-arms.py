"""Apply one acceptance probe's counterfacts to one measured reading, and say what moved.

Written for AC-17|06 after its pipeline arm was renamed: the arm used to be labelled
``pipeline 实测 88.84``, which reads like a live report figure and is not one -- it is the forged
value the judge has to reject. The rename has to leave every arm still biting, so this tool
re-applies each of them to a clean reading built from the item's own declared repair.

It also counts the label shape that caused the misreading: an arm label that puts the word 实测 next
to a digit. That count is printed for the current table and for the table as it stood at the
revision before the rename, so the zero comes with an arm that returns non-zero. The historical
census is not a regex over module text -- it imports the old bytes and counts the same ``Break``
objects the live census counts, because an uncalibrated pattern is how a zero gets invented.

Nothing here recomputes a measurement: the facts come from
``acceptance_item_probe.py --item AC-17|06 --json PATH``, and the judgement comes from the repo's
own judge, ``resolve_repair`` and ``resolve_break``. Pass that JSON path as the only argument.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts" / "quality"))

from acceptance_item_probe import (  # noqa: E402
    GAP,
    PROBES,
    PROVEN,
    probe_for,
    resolve_break,
    resolve_repair,
)

#: The word that made an arm read like a measurement. A label may name a threshold, a face or a
#: direction; a counterfact cannot be "实测", because the live figure is recomputed every pass.
CLAIMS_MEASURED = "实测"

#: The revision that last moved the probe module before this rename, kept so the counter-census has
#: a named population to run against.
PREVIOUS = "af4c90f"

TARGET = "scripts/quality/acceptance_item_probe.py"
TOOL_DIR = REPO / "scripts" / "quality"


def table_census(probes: Sequence[Any]) -> tuple[int, int, list[str], list[str]]:
    """Count a probe table, its arms, arms claiming 实测, and those that also state a digit."""
    labels = [brk.label for probe in probes for brk in probe.breaks]
    measured = [line for line in labels if CLAIMS_MEASURED in line]
    numbered = [line for line in measured if any(char.isdigit() for char in line)]
    return len(probes), len(labels), measured, numbered


def git_show(revision: str) -> str:
    """Read one file at one revision through git, so a figure has a named carrier."""
    process = subprocess.run(  # nosec B603  # noqa: S603 - no shell, argv is a fixed rev and path
        [shutil.which("git") or "git", "show", f"{revision}:{TARGET}"],
        cwd=REPO,
        capture_output=True,
        check=True,
    )
    return process.stdout.decode("utf-8")


def table_at(revision: str) -> list[Any]:
    """Return the probe table as it stood at one revision, from a byte copy in its own directory.

    The module derives ``REPO_ROOT`` from ``__file__``, so the copy has to sit next to the original
    or the imported table would be measuring a different tree. The copy is removed in a ``finally``
    block, and :func:`show_label_census` prints that nothing was left behind.
    """
    temp = TOOL_DIR / f"_c85_probe_at_{revision[:7]}.py"
    temp.write_text(git_show(revision), encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location(temp.stem, temp)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"no loader for the copy at {temp}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return list(vars(module)["PROBES"])
    finally:
        sys.modules.pop(temp.stem, None)
        temp.unlink(missing_ok=True)


def show_census(label: str, census: tuple[int, int, list[str], list[str]]) -> None:
    """Print one census with its lists, so the counts are read next to what they count."""
    probes, arms, measured, numbered = census
    print(f"{label}: probes={probes} arms={arms}")
    print(f"  labels containing {CLAIMS_MEASURED!r}: {len(measured)}")
    for line in measured:
        print(f"     - {line}")
    print(f"  labels containing {CLAIMS_MEASURED!r} AND a digit: {len(numbered)}")
    for line in numbered:
        print(f"     - {line}")


def show_label_census() -> None:
    """Print the live arm-label census and the same census over the pre-rename table."""
    live = table_census(PROBES)
    show_census("live table", live)
    old = table_census(table_at(PREVIOUS))
    show_census(f"table at {PREVIOUS} (imported copy)", old)
    gone = [line for line in old[2] if line not in live[2]]
    for line in gone:
        print(f"  label no arm carries now: {line}")
    leftovers = sorted(path.name for path in TOOL_DIR.glob("_c85_probe_at_*.py"))
    print(f"same table size across the rename: {live[0] == old[0] and live[1] == old[1]}")
    print(
        f"COUNTER_CENSUS_DIFFERS={'yes' if len(old[3]) != len(live[3]) else 'no'} "
        f"(live={len(live[3])}, {PREVIOUS}={len(old[3])})"
    )
    print(f"digit-bearing labels retired: {len(old[3]) - len(live[3])}")
    print(f"byte copies left in scripts/quality: {len(leftovers)} {leftovers or '[]'}")


def face(facts: dict[str, str], key: str) -> str:
    """Read one face out of the measured facts, or say it was never measured."""
    return facts.get(key, "<not measured>")


def main(argv: list[str]) -> int:
    """Re-apply one item's arms to its own clean reading and print the resulting faces."""
    if len(argv) != 1:
        print("usage: python docs/evidence/C85/ac17-06-counterfact-arms.py PATH/to/item-face.json")
        return 2
    payload: dict[str, Any] = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = list(payload["items"])
    if len(records) != 1:
        print(f"FAIL: expected one item in {argv[0]}, got {len(records)}")
        return 1
    item = str(records[0]["item"])
    facts: dict[str, str] = {str(k): str(v) for k, v in dict(records[0]["facts"]).items()}
    probe = probe_for(item)

    show_label_census()
    print(f"item under test: {item} | arms on this item: {len(probe.breaks)}")
    print(f"live root_pct on this pass = {face(facts, 'root_pct')}")

    clean = resolve_repair(dict(facts), probe.repair)
    repair_state = probe.judge(clean).state
    print(f"declared repair -> judge state = {repair_state} (reachable: {repair_state == PROVEN})")

    bitten = 0
    misaligned: list[str] = []
    for brk in probe.breaks:
        mutated = resolve_break(clean, brk.facts)
        state = probe.judge(mutated).state
        if state == brk.expect:
            bitten += 1
            mark = "BITE "
        else:
            misaligned.append(f"{brk.label} -> {state}")
            mark = "NODBITE"
        forged = ", ".join(f"{key}={value}" for key, value in brk.facts)
        print(f"{mark} | {brk.label} | forges [{forged}] | -> {state}")
    print(f"arms applied = {len(probe.breaks)} | biting = {bitten}")
    for line in misaligned:
        print(f"  not biting: {line}")

    gaps_only = all(brk.expect == GAP for brk in probe.breaks)
    stray = sorted({key for brk in probe.breaks for key, _ in brk.facts} - set(facts))
    stray_repair = sorted(set(probe.repair) - set(facts))
    print(f"every arm promises a gap: {gaps_only}")
    print(f"arms naming a face no measure produces: {stray or '[]'}")
    print(f"repair keys no measure produces: {stray_repair or '[]'}")
    print(f"ALL_ARMS_BITE={'yes' if bitten == len(probe.breaks) and bitten else 'no'}")
    print(f"REPAIR_REACHES_PROVEN={'yes' if repair_state == PROVEN else 'no'}")
    return 0 if (bitten == len(probe.breaks) and repair_state == PROVEN) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
