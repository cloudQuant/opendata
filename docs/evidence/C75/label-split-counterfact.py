#!/usr/bin/env python3
"""C75 split control: prove the 17 -> 3 drop in ``shipped_cover`` came from the label map.

Why this face exists
    C75 moved four census labels off present capabilities and onto four new absent ones. That edit
    changes a verdict -- rows that read "the engine already ships this" now read "engine work
    outstanding" -- so the change needs a refutation surface, not a promise. Two readings settle it:

      * revert *all four* labels to their old spellings and the pre-split face must come back
        exactly (17 cover rows), which says nothing else moved;
      * revert them *one at a time* and each label must move the count on its own, because a split
        that changes nothing is vocabulary churn wearing the costume of a fix.

    Those two are additive only if no row carries two of the four labels, so the sum of the single
    reverts is checked against the whole collapse. The identity either holds or names the rows that
    break it -- it is never silently folded away.

    Nothing here writes the roadmap. ``greedy_cover`` is called directly, on the shipped rows and
    the shipped proof files, so the numbers below are the repo's own judge reading its own disk.

Usage
    python3 docs/evidence/C75/label-split-counterfact.py

    Exit 0 only when all five controls agree; exit 1 prints which one refused.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
PLAN_DIR: Final = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
EXPECTED_CENSUS_ROWS: Final = 150
#: The face the roadmap carried before the split, recorded in roadmap-after-registry-recheck.txt.
PRE_SPLIT_COVER_ROWS: Final = 17
#: label -> the capability it used to be collapsed onto, read from ``git show HEAD`` of the script.
OLD_MAP: Final = {
    "delimited_rows_without_published_header": "decode.delimited",
    "sdmx_dotted_key_path": "path.dotted_pointer",
    "parameterized_rows_pointer": "path.dotted_pointer",
    "xbrl_tag_assembly": "columns.select",
}
#: The entry the two pointer labels need in order to have somewhere to land.
DOTTED_POINTER_CAPABILITY: Final = "path.dotted_pointer"


def _load_module() -> ModuleType:
    """Import ``model_capability_census`` under a name, as the repo's own tests do."""
    path = REPO_ROOT / "scripts/quality/model_capability_census.py"
    spec = importlib.util.spec_from_file_location("mcc_under_test", path)
    if spec is None or spec.loader is None:  # pragma: no cover - importlib contract
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules, so registration comes first.
    sys.modules["mcc_under_test"] = module
    spec.loader.exec_module(module)
    return module


def cover_rows(
    mcc: ModuleType, entries: tuple[object, ...], *, restore: dict[str, str]
) -> tuple[int, int]:
    """Map the shipped census with ``restore`` folded into the label table, and count cover rows.

    Returns ``(shipped_cover, rows_pending)`` as the repo's own ``greedy_cover`` judges them.
    """
    original = dict(mcc.LABEL_TO_CAPABILITY)
    mcc.LABEL_TO_CAPABILITY.update(restore)
    try:
        rows = mcc.load_rows(REPO_ROOT / PLAN_DIR)
        if len(rows) != EXPECTED_CENSUS_ROWS:
            raise SystemExit(f"census rows = {len(rows)}, expected {EXPECTED_CENSUS_ROWS}")
        declarations, _, _ = mcc.count_declarations(entries, REPO_ROOT)
        readings = mcc.verify_registry(entries, REPO_ROOT, declarations)
        roadmap = mcc.greedy_cover(rows, readings)
        return len(roadmap.shipped_cover), roadmap.rows_pending
    finally:
        mcc.LABEL_TO_CAPABILITY.clear()
        mcc.LABEL_TO_CAPABILITY.update(original)


def _capability(entry: object) -> str:
    """The registry id an entry carries, read without assuming the census module's types."""
    return str(getattr(entry, "capability", ""))


def with_dotted_pointer(mcc: ModuleType, entries: tuple[object, ...]) -> tuple[object, ...]:
    """Return the shipped registry plus the ``path.dotted_pointer`` entry C75 retired."""
    if any(_capability(entry) == DOTTED_POINTER_CAPABILITY for entry in entries):
        return entries
    entry = mcc.CapabilityEntry(
        capability=DOTTED_POINTER_CAPABILITY,
        claim=True,
        engine_work=True,
        proof_file=mcc.HTTP_JSON_REL,
        anchor='pointer.split(".")',
        cross_file=mcc.SPEC_REL,
        cross_anchor='rows_pointer: str = ""',
        why="reconstructed for the counterfact only: the walker this entry named is still on disk.",
        probe=mcc.DeclarationProbe(call="ModelSpec", keyword="rows_pointer", non_empty=True),
    )
    return (*entries, entry)


def main(argv: list[str]) -> int:
    """Print the five controls and exit 1 if any of them refuses."""
    del argv  # the probe takes no arguments by design
    mcc = _load_module()
    shipped = mcc.REGISTRY

    cover_now, pending_now = cover_rows(mcc, shipped, restore={})
    collapsed_entries = with_dotted_pointer(mcc, shipped)
    cover_all, pending_all = cover_rows(mcc, collapsed_entries, restore=OLD_MAP)
    entry_only, _ = cover_rows(mcc, collapsed_entries, restore={})

    print(
        f"shipped registry ({len(shipped)} entries)     cover_rows={cover_now} "
        f" pending={pending_now}"
    )
    print(f"+ dotted_pointer entry, labels unchanged   cover_rows={entry_only}")
    print(
        f"all four labels reverted, entry added      cover_rows={cover_all}  pending={pending_all}"
    )

    singles: dict[str, int] = {}
    for label, capability in OLD_MAP.items():
        restored, _ = cover_rows(mcc, collapsed_entries, restore={label: capability})
        singles[label] = restored
        print(f"  revert only {label:<40} cover_rows={restored}  delta=+{restored - cover_now}")

    deltas = {label: value - cover_now for label, value in singles.items()}
    total_delta = sum(deltas.values())
    expected_delta = cover_all - cover_now

    print("\ncontrols")
    notes: list[str] = []
    if cover_now != 3:
        notes.append(f"C1 FAIL: shipped cover rows read {cover_now}, the roadmap says 3")
    else:
        notes.append("C1 ok: the shipped map reproduces the roadmap's cover face (3)")
    if cover_all != PRE_SPLIT_COVER_ROWS:
        notes.append(
            f"C2 FAIL: reverting all four labels reads {cover_all}, pre-split face was "
            f"{PRE_SPLIT_COVER_ROWS} -- the split is not the whole difference"
        )
    else:
        notes.append(
            f"C2 ok: the old label map reproduces the recorded pre-split face ({cover_all})"
        )
    if entry_only != cover_now:
        notes.append(
            f"C3 FAIL: adding one registry entry moved cover from {cover_now} to {entry_only} -- "
            "entries, not labels, would be driving the verdict"
        )
    else:
        notes.append("C3 ok: the retired entry alone changes nothing; the verdict is the label map")
    decorative = sorted(label for label, delta in deltas.items() if delta == 0)
    if decorative:
        notes.append(f"C4 FAIL: these splits move no row and are vocabulary churn: {decorative}")
    else:
        notes.append(f"C4 ok: all {len(OLD_MAP)} splits carry at least one row")
    if total_delta != expected_delta:
        notes.append(
            f"C5 FAIL: single reverts sum to +{total_delta} but the collapse is +{expected_delta}; "
            "a row must carry two of these labels, so the per-label attribution is not additive"
        )
    else:
        notes.append(
            f"C5 ok: attribution is additive (+{total_delta} == +{expected_delta}), so each of the "
            f"{expected_delta} rows is charged to exactly one label"
        )
    for note in notes:
        print(f"  {note}")
    failed = [note for note in notes if "FAIL" in note]
    print(
        f"\nSELF-TEST: {len(notes) - len(failed)}/{len(notes)} controls agreed"
        if not failed
        else "SELF-TEST FAILED"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
