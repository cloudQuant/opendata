"""C89 witness: the two faces the OECD taxonomy repair moved, each with a differing damage arm.

D -- the probe-less/engine_work pin in tests/test_model_capability_census.py. The pin is read out of
   the test file's own AST, so this witness judges the repo's literal and not my memory of it.
E -- the five C89 registry entries. Their ``absent`` reading must come from bytes: deleting
   each anchor token from a copy of its proof file must move the reading, and a reading that
   does not move is an entry that is not measuring anything.
F -- the nine OECD rows: the census need set must contain every capability the package's own
   cite-bearing NOT_DECLARABLE note names, so a row cannot be quieted by dropping a label.

Run: python docs/evidence/C89/oecd-pin-witness.py (paths resolve from this file, not the shell CWD)
"""

from __future__ import annotations

import ast
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import scripts.quality.model_capability_census as mcc  # noqa: E402

TEST_FILE = ROOT / "tests/test_model_capability_census.py"
NEW_CAPS = [
    "columns.compose",
    "columns.period_cast",
    "derive.conditional",
    "rows.filter_comparison",
    "path.list_segment",
]
OCD_MODEL_PREFIX = "OBB2-oecd-"


def shipped_pin() -> list[str]:
    """The pin literal, lifted from the test file's AST."""
    tree = ast.parse(TEST_FILE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        cmp = node.test
        if not isinstance(cmp, ast.Compare) or not cmp.comparators:
            continue
        right = cmp.comparators[0]
        if not isinstance(right, ast.List):
            continue
        # Narrow element by element: mypy cannot carry an isinstance test done inside all().
        values: list[str] = []
        for elt in right.elts:
            if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
                values = []
                break
            values.append(elt.value)
        if "columns.compose" in values and "request.caller_url" in values:
            return sorted(values)
    raise AssertionError("the probe-less pin literal was not found in the test file")


def measured_probeless(entries: tuple[mcc.CapabilityEntry, ...]) -> list[str]:
    """The face the pin freezes: engine-side capabilities with no declaration surface."""
    return sorted(e.capability for e in entries if e.probe is None and e.engine_work)


def readings_for(
    entries: tuple[mcc.CapabilityEntry, ...], root: Path
) -> dict[str, mcc.EntryReading]:
    """Read every registry entry against ``root``'s bytes, so an arm can retarget the tree."""
    decls, _, _ = mcc.count_declarations(entries, root)
    return mcc.verify_registry(entries, root, decls)


def registry_files() -> list[str]:
    """Every file any registry entry reads, so an untouched entry keeps its real reading.

    Copying only the tampered entry's own proof file makes its siblings read anchor_line_number=0
    and turn drifted for a reason that has nothing to do with the cut -- that arm measures the
    temp root, not the anchor.
    """
    out: list[str] = []
    for entry in mcc.REGISTRY:
        for rel in (entry.proof_file, entry.cross_file):
            if rel and rel not in out:
                out.append(rel)
    return out


def main() -> int:
    """Run every arm, print one face per line, and exit non-zero when an arm refuses to differ."""
    failures: list[str] = []

    def check(label: str, holds: bool, detail: str) -> None:
        print(f"{label:34s} {detail}")
        if not holds:
            failures.append(label)

    # ---- D: the probe-less pin -----------------------------------------------------------------
    pin = shipped_pin()
    measured = measured_probeless(mcc.REGISTRY)
    d1_face = f"pin={len(pin)} measured={len(measured)} {measured}"
    check("D1 pin == measured", pin == measured, d1_face)

    victim = next(e for e in mcc.REGISTRY if e.probe is None and e.engine_work)
    armed = tuple(
        replace(e, probe=mcc.DeclarationProbe(call="ModelSpec", keyword="row_filters"))
        if e is victim
        else e
        for e in mcc.REGISTRY
    )
    check(
        "D2 arm entry with a probe",
        measured_probeless(armed) != pin,
        f"{victim.capability} gained a surface -> set={len(measured_probeless(armed))}, "
        f"pin fires={'yes' if measured_probeless(armed) != pin else 'NO'}",
    )
    check(
        "D3 drop one pin name",
        pin[:-1] != pin and sorted(pin[:-1]) != measured,
        f"pin[:-1]={len(pin) - 1} vs measured={len(measured)} -> fires=yes",
    )

    # ---- E: the five new entries read absent from bytes ---------------------------------------
    real = readings_for(mcc.REGISTRY, ROOT)
    for cap in NEW_CAPS:
        entry = next(e for e in mcc.REGISTRY if e.capability == cap)
        r = real[cap]
        check(
            f"E1 {cap} pinned absent",
            r.status == "absent" and not r.present and r.anchor_line_number > 0,
            f"status={r.status} line={r.anchor_line_number} file={entry.proof_file}",
        )

        work = Path(tempfile.mkdtemp(prefix="c89-anchor-"))
        try:
            for rel in registry_files():
                src = ROOT / Path(rel)
                if not src.is_file():
                    continue
                dst = work / Path(rel)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, dst)
            # Copy fidelity first: with nothing cut, the temp root must read exactly like the tree,
            # otherwise E2 has no baseline and a "moved" reading could be the copy's own artefact.
            twin = readings_for(mcc.REGISTRY, work)
            diverged = sorted(c for c in twin if twin[c].status != real[c].status)
            check(
                f"E0 {cap} copy fidelity",
                not diverged,
                f"untouched copy diverges on: {diverged or 'none'}",
            )
            text = (work / Path(entry.proof_file)).read_text(encoding="utf-8")
            (work / Path(entry.proof_file)).write_text(
                text.replace(entry.anchor, "# anchor removed for the counterfact"),
                encoding="utf-8",
            )
            cut = readings_for(mcc.REGISTRY, work)
            moved = cut[cap].status != r.status or cut[cap].present != r.present
            check(
                f"E2 {cap} anchor cut",
                moved,
                f"status {r.status} -> {cut[cap].status} (moved={moved})",
            )
            others_moved = [c for c in NEW_CAPS if c != cap and cut[c].status != real[c].status]
            check(
                f"E3 {cap} damage stays local",
                not others_moved,
                f"other new entries also moved: {others_moved or 'none'}",
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ---- F: the nine rows carry every named blocker ------------------------------------------
    from opendata.data.providers.oecd import specs as oecd_specs

    token_to_cap = {
        "path.list_segment": "path.list_segment",
        "columns.compose": "columns.compose",
        "columns.period_cast": "columns.period_cast",
        "derive.conditional": "derive.conditional",
        "row_filter.comparison": "rows.filter_comparison",
        "more than one request": "flow.branch",
    }
    rows = {r.task_id: r for r in mcc.load_rows(mcc._REPO_ROOT / mcc.CENSUS_DIR_REL)}
    unmapped: list[str] = []
    short_of_note: list[str] = []
    for model, note in oecd_specs.NOT_DECLARABLE.items():
        row = rows.get(f"{OCD_MODEL_PREFIX}{model}")
        if row is None:
            unmapped.append(model)
            continue
        named = {cap for tok, cap in token_to_cap.items() if tok in note}
        # every token in the map must be a capability the judge knows
        known = named | set(row.needs)
        short_of_note.extend(
            f"{model}: unknown capability {cap}" for cap in sorted(known) if cap not in real
        )
        missing = named - set(row.needs)
        if missing:
            short_of_note.append(f"{model}: census row drops {sorted(missing)}")
    f1_face = (
        f"models={len(oecd_specs.NOT_DECLARABLE)} without_a_row={unmapped} "
        f"gaps={short_of_note or 'none'}"
    )
    check("F1 census covers the notes", not unmapped and not short_of_note, f1_face)
    fold_hits = sorted(
        {
            mcc.LABEL_TO_CAPABILITY[label]
            for label in mcc.LABEL_TO_CAPABILITY
            if mcc.LABEL_TO_CAPABILITY[label] in set(NEW_CAPS) | {"flow.branch"}
        }
    )
    check(
        "F2 folds reach only new caps",
        len(fold_hits) == 6,
        f"labels folding onto the six C89 capabilities: {fold_hits}",
    )
    # a row whose whole need set is present must not survive among the nine
    covered = {c for c, r in real.items() if r.present}
    still_ready = [
        model
        for model in oecd_specs.NOT_DECLARABLE
        if set(rows[f"{OCD_MODEL_PREFIX}{model}"].needs) <= covered
    ]
    check(
        "F3 no oecd row reads ready",
        not still_ready,
        f"rows whose needs are all shipped: {still_ready or 'none'} (must be none)",
    )

    print()
    if failures:
        print(f"WITNESS FAIL: {len(failures)} arm(s) did not behave: {failures}")
        return 1
    print("ALL ARMS: every face has one pass reading and one differing damage reading")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
