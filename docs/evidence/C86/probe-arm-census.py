#!/usr/bin/env python3
"""Census the shipped gate instrument's own declared counterfact arms, item by item.

``--self-test`` prints one aggregate line (C86: 130 probes / 939 counterfacts) and this round's
notes need per-cell numbers -- how many arms AC-17|02 has, whether the seven newly instrumented
cells each carry arms at all. Typing them from the aggregate is exactly the kind of number that
rots, so this instrument counts the same objects the self-test walks (``PROBES`` and each probe's
declared ``Break`` list) and ties its total to the self-test's printed figure with
``--expect-arms``, printing MATCH or MISMATCH and exiting non-zero on the mismatch. A total that
disagrees means this census is not the self-test's expression, and the per-cell numbers below it
are then worthless rather than silently plausible.

The distribution face is the reachable-minimum argument for the zero in ``probes_without_arms``:
``min_arms_per_probe`` is the smallest arm count among the probes, printed beside it, and any probe
with no arm at all is named one per line rather than folded into the count.

Run: python3 docs/evidence/C86/probe-arm-census.py --expect-arms 939
     python3 docs/evidence/C86/probe-arm-census.py --against 00dd736 --expect-arms 939
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import pathlib
import shutil
import subprocess  # nosec B404  # literal argv: git show of one known path, never a shell string
import sys
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from types import ModuleType

REPO: Final = pathlib.Path(__file__).resolve().parents[3]
MODULE: Final = REPO / "scripts/quality/acceptance_item_probe.py"
#: The one module-level sibling import the probe body performs; it is copied next to the snapshot
#: so a historical revision can be loaded from a scratch tree without touching the repository's own
#: ``scripts/`` directory (where it would join AC-17|02's own census population).
SIBLING: Final = "scripts/quality/source_layout.py"
TOUCHED: Final = (
    "AC-17|02",
    "§4|02",
    "§4|04",
    "§5|02",
    "AC-8|08",
    "AC-13|08",
    "AC-15|01",
    "AC-15|02",
)


def load(name: str, path: pathlib.Path) -> ModuleType:
    """Import ``path`` under ``name``, registering it before execution."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_probes(name: str, path: pathlib.Path) -> list[Any]:
    """Return the shipped probe module's PROBES list."""
    return list(load(name, path).PROBES)


def snapshot(rev: str, scratch: pathlib.Path) -> pathlib.Path:
    """Write ``rev`` of the probe module into a scratch tree at the depth its body expects."""
    done = subprocess.run(  # noqa: S603  # nosec B603 B607  # literal argv, git show only
        ["git", "show", f"{rev}:{MODULE.relative_to(REPO)}"],  # noqa: S607  # git by PATH
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    target = scratch / "scripts/quality/probe_snapshot.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(done.stdout, encoding="utf-8")
    shutil.copyfile(REPO / SIBLING, scratch / SIBLING)
    return target


def census(probes: list[Any]) -> tuple[int, int, int, dict[str, int]]:
    """Return (probe count, total declared arms, arm floor, per-item arm counts)."""
    per = {str(p.item): len(p.breaks) for p in probes}
    return len(per), sum(per.values()), (min(per.values()) if per else -1), per


def main() -> int:
    """Print the arm census, tie the total to the self-test figure, and name any armless probe."""
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--expect-arms", type=int, default=0, help="the --self-test printed total")
    parser.add_argument("--against", default="", help="revision to count declared arms against")
    args = parser.parse_args()
    probes = load_probes("shipped_probe", MODULE)
    count, total, floor, per = census(probes)
    armless = sorted(item for item, arms in per.items() if arms == 0)
    digest = hashlib.sha256(MODULE.read_bytes()).hexdigest()[:16]
    print(f"module={MODULE.relative_to(REPO)} sha256[:16]={digest}")
    print(f"probes={count} declared_arms={total} min_arms_per_probe={floor}")
    print(f"probes_without_arms={len(armless)}")
    for item in armless:
        print(f"  ARMLESS {item}")
    print("per-item declared arms for the cells this round touched:")
    for item in TOUCHED:
        print(f"  {item}: {per.get(item, '(no probe)')}")
    if args.against and not tie_to_revision(args.against, per, total):
        print("CLOSURE BROKEN: the printed components do not add up to the delta")
        return 1
    if args.expect_arms:
        match = "MATCH" if total == args.expect_arms else "MISMATCH"
        print(f"TIE total={total} self_test={args.expect_arms} -> {match}")
        return 0 if total == args.expect_arms else 1
    print("NOTE: no --expect-arms given, so the total above is untied to the self-test reading")
    return 0


def tie_to_revision(rev: str, disk_per: dict[str, int], disk_total: int) -> bool:
    """Print the revision-vs-disk arm delta with its closure arithmetic; close means True."""
    scratch = REPO / "docs/evidence/C86/.scratch_head"
    try:
        head_probes = load_probes("head_probe", snapshot(rev, scratch))
        head_count, head_total, _, head_per = census(head_probes)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"against={rev} head_probes={head_count} head_arms={head_total}")
    print(f"delta probes={len(disk_per) - head_count:+d} arms={disk_total - head_total:+d}")
    new_items = sorted(set(disk_per) - set(head_per))
    new_arms = sum(disk_per[item] for item in new_items)
    moved = {
        item: disk_per[item] - head_per[item]
        for item in sorted(set(disk_per) & set(head_per))
        if disk_per[item] != head_per[item]
    }
    closed = disk_total - head_total == new_arms + sum(moved.values())
    print(f"items with a probe only on disk: {len(new_items)} -> {','.join(new_items) or '-'}")
    print(f"new_item_arms={new_arms} existing_item_arms={moved or '-'}")
    print(
        f"closure {new_arms}+{sum(moved.values())}={new_arms + sum(moved.values())} "
        f"vs {disk_total}-{head_total}={disk_total - head_total} -> "
        f"{'HOLDS' if closed else 'BROKEN'}"
    )
    return closed


if __name__ == "__main__":
    sys.exit(main())
