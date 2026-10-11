"""Prove the C86 label split moves counts, by reverting it one faithful fold at a time.

C86 found seven raw labels folded onto canonical capabilities that do not describe them, and two of
those folds -- ``param_value_mapping`` and ``symbol_normalization`` onto ``columns.rename`` -- were
manufacturing rows the roadmap called declarable today with an engine that cannot express them. The
split re-pointed the seven labels and added five canonical registry entries.

No number is claimed here; the arms reproduce the pre-split face and print what moved. The pre-split
map is read out of git's blob at the pinned parent commit (emitted to
``label-map-before-split.json`` by ``--emit-carrier``, so no arm execs a shell), and the folds are
*derived* as the label assignments the live map disagrees with. Each fold is then applied to a copy
of the live map and the repo's own roadmap is re-run through ``model_capability_census.run``.

Two discoveries shaped this file and stay in it as arms rather than being smoothed over:

* Folding a label onto a capability that nothing else names leaves the registry entry the split
  added unnameable, and the live ``_check_registry`` guard hard-stops that. A fold is therefore only
  a *revert* when it also drops the registry entry the split added, and the guard arm shows the
  refusal on one side and the clean run on the other.
* Not every fold moves a ladder counter. ``branching_base_url`` re-points the row's need set between
  two capabilities that are both absent, so the roadmap reads identically. That is why the fifth
  pinned counter is the number of rows whose need set the fold rewrites: it gives an
  attribution-only fold a face instead of letting it pass as a measurement.

Usage::

    git show 912f59b:scripts/quality/model_capability_census.py > /tmp/c86_pre_split.py
    python docs/evidence/C86/label-split-probe.py --emit-carrier /tmp/c86_pre_split.py
    python docs/evidence/C86/label-split-probe.py --probe
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from types import ModuleType

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
PLAN_DIR: Final = REPO_ROOT / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
INSTRUMENT: Final = REPO_ROOT / "scripts/quality/model_capability_census.py"
CARRIER: Final = Path(__file__).resolve().parent / "label-map-before-split.json"
# The commit whose LABEL_TO_CAPABILITY still folded the seven labels. Pinned by sha rather than
# "HEAD^" so the carrier keeps naming the map the split reverted even after the split is committed.
SPLIT_PARENT: Final = "912f59b"
BLOB_REL: Final = "scripts/quality/model_capability_census.py"
LADDER_COUNTERS: Final = (
    "canonical_capabilities",
    "rows_expressible_today",
    "shipped_cover_rows",
    "greedy_steps",
)
COUNTERS: Final = LADDER_COUNTERS + ("rows_need_set_rewritten",)
MAP_KEYS: Final = ("commit", "blob", "pre_split_sha256_16", "label_to_capability")


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def _load_census_module() -> ModuleType:
    """Import the roadmap instrument by path so this probe reads the live judge, not a copy."""
    spec = importlib.util.spec_from_file_location("mcc_for_split_probe", INSTRUMENT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {INSTRUMENT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["mcc_for_split_probe"] = module
    spec.loader.exec_module(module)
    return module


def _extract_map(path: Path) -> tuple[dict[str, str], str]:
    """Pull the ``LABEL_TO_CAPABILITY`` literal out of a census file by AST, not by regex."""
    source = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        value, name = None, ""
        if isinstance(node, ast.Assign):
            value = node.value
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            name = names[0] if names else ""
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            value, name = node.value, node.target.id
        if value is not None and name == "LABEL_TO_CAPABILITY":
            mapping = ast.literal_eval(value)
            return {str(k): str(v) for k, v in mapping.items()}, _digest(source.encode())
    raise RuntimeError(f"LABEL_TO_CAPABILITY not found in {path}")


def emit_carrier(presplit_file: Path, commit: str) -> int:
    """Write the pre-split map to the carrier this probe reads, pinning the bytes it came from."""
    mapping, digest = _extract_map(presplit_file)
    payload = {
        "commit": commit,
        "blob": BLOB_REL,
        "pre_split_sha256_16": digest,
        "label_to_capability": dict(sorted(mapping.items())),
    }
    CARRIER.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"carrier: wrote {CARRIER.name} labels={len(mapping)} sha256[:16]={digest}")
    return 0


def _face(mcc: ModuleType, folds: Mapping[str, str], drop: Iterable[str]) -> dict[str, object]:
    """Run the repo's roadmap under one fold, dropping the registry entries the split added."""
    live_map = dict(mcc.LABEL_TO_CAPABILITY)
    dropped = frozenset(drop)
    entries = tuple(e for e in mcc.REGISTRY if e.capability not in dropped)
    try:
        mcc.LABEL_TO_CAPABILITY.clear()
        mcc.LABEL_TO_CAPABILITY.update(live_map)
        mcc.LABEL_TO_CAPABILITY.update(folds)
        report = mcc.run(round_id="C86", root=REPO_ROOT, census_dir=PLAN_DIR, entries=entries)
        needs = {row.task_id: row.needs for row in mcc.load_rows(PLAN_DIR)}
        cover = report.roadmap.shipped_cover
        return {
            "canonical_capabilities": report.canonical_count,
            "rows_expressible_today": report.roadmap.rows_expressible_today,
            "shipped_cover_rows": len(cover),
            "greedy_steps": len(report.roadmap.build_order),
            "needs_by_task": needs,
            "shipped_cover_ids": sorted(f"{row.provider}-{row.upstream_model}" for row in cover),
        }
    finally:
        mcc.LABEL_TO_CAPABILITY.clear()
        mcc.LABEL_TO_CAPABILITY.update(live_map)


def _refusal(mcc: ModuleType, folds: Mapping[str, str]) -> str:
    """Fold without dropping, and report the live registry guard's refusal verbatim."""
    try:
        _face(mcc, folds, ())
    except mcc.RoadmapError as exc:
        return f"{type(exc).__name__}: {exc}"
    return "no refusal"


def _int(value: object) -> int:
    """Read a pinned counter back as an int; the faces are ints, this file just stores objects."""
    if isinstance(value, (int, float)):
        return int(value)
    return int(str(value))


def _seq(value: object) -> list[str]:
    """Read a pinned id-list face back as a list, refusing a face of the wrong shape."""
    if not isinstance(value, list):
        raise TypeError(f"expected a list face, saw {type(value).__name__}")
    return [str(item) for item in value]


def _moved(before: Mapping[str, object], after: Mapping[str, object]) -> list[str]:
    """Name every pinned counter the fold moved, plus how many rows it rewrote the need set of."""
    out = [
        f"{name} {before[name]}->{after[name]}"
        for name in LADDER_COUNTERS
        if str(before[name]) != str(after[name])
    ]
    live_needs: dict[str, frozenset[str]] = before["needs_by_task"]  # type: ignore[assignment]
    after_needs: dict[str, frozenset[str]] = after["needs_by_task"]  # type: ignore[assignment]
    rewritten = sum(1 for task_id, need in live_needs.items() if after_needs.get(task_id) != need)
    return [*out, f"rows_need_set_rewritten {rewritten}"]


def _fail(reason: str) -> int:
    print(f"REFUSE {reason}")
    return 2


def probe() -> int:
    """Revert each split fold in turn, then all at once, and publish what each one moved."""
    if not CARRIER.is_file():
        return _fail(f"{CARRIER.name} absent; run --emit-carrier first")
    carrier = json.loads(CARRIER.read_text(encoding="utf-8"))
    missing = [key for key in MAP_KEYS if key not in carrier]
    if missing:
        return _fail(f"{CARRIER.name} is missing keys {missing}")
    pre = {str(k): str(v) for k, v in carrier["label_to_capability"].items()}
    mcc = _load_census_module()
    live_map = {str(k): str(v) for k, v in mcc.LABEL_TO_CAPABILITY.items()}
    divergent = sorted(set(pre) ^ set(live_map))
    print(
        f"carrier: commit={carrier['commit']} blob={carrier['blob']} "
        f"sha256[:16]={carrier['pre_split_sha256_16']} pre_split_labels={len(pre)} "
        f"live_labels={len(live_map)} label_spelling_differences={len(divergent)}"
    )
    if divergent:
        return _fail(
            f"the label spellings themselves moved ({divergent}); the carrier no longer "
            "describes a re-pointing of the live map, so a fold arm would not be a revert"
        )
    folds = {label: old for label, old in pre.items() if live_map[label] != old}

    # A canonical only this label holds disappears when the label folds away, and that is exactly
    # the registry entry the split added; every other fold lands on a capability somebody still
    # names. Both sides of the identity are computed rather than written down.
    holders: dict[str, int] = {}
    for value in live_map.values():
        holders[value] = holders.get(value, 0) + 1
    unique_labels = sorted(label for label in folds if holders.get(live_map[label], 0) == 1)
    drop_all = sorted({live_map[label] for label in unique_labels})
    live = _face(mcc, {}, ())
    print(f"live face: {json.dumps({k: live[k] for k in LADDER_COUNTERS}, sort_keys=True)}")
    print(f"live shipped_cover rows: {json.dumps(live['shipped_cover_ids'])}")
    print(
        f"folds derived from the carrier, not typed here: {len(folds)} "
        f"{json.dumps(folds, sort_keys=True)}"
    )
    print(
        f"registry entries the split added, derived from the folds: {len(drop_all)} "
        f"{json.dumps(drop_all)}"
    )

    failures = 0
    print("GUARD ARM (a fold that leaves its registry entry unnameable must be refused)")
    for label in unique_labels[:1]:
        # ``old`` is what the carrier says the label used to fold onto; ``new`` is the canonical the
        # split gave it and the registry entry that carries it. Reverting the fold means folding to
        # ``old`` *and* dropping ``new`` -- folding to ``new`` is a no-op that tests nothing.
        old, new = folds[label], live_map[label]
        refused = _refusal(mcc, {label: old})
        kept_refuses = new in refused and refused.startswith("RoadmapError")
        dropped = _face(mcc, {label: old}, [new])
        accepted_moves = (
            _int(dropped["canonical_capabilities"]) == _int(live["canonical_capabilities"]) - 1
        )
        verdict = "PASS" if kept_refuses and accepted_moves else "FAIL"
        failures += verdict == "FAIL"
        print(f"  [{verdict}] fold {label} -> {old} with the {new} entry kept: {refused}")
        print(
            f"  [{verdict}] same fold with {new} dropped: runs, "
            f"canonical_capabilities {live['canonical_capabilities']}->"
            f"{dropped['canonical_capabilities']}"
        )

    print("SINGLE-FOLD ARMS (each must move a ladder counter or rewrite at least one need set)")
    reducing: list[str] = []
    attribution_only: list[str] = []
    for label in sorted(folds):
        old, new = folds[label], live_map[label]
        drop = [new] if label in unique_labels else []
        after = _face(mcc, {label: old}, drop)
        moved = _moved(live, after)
        ladder_moved = [part for part in moved if not part.startswith("rows_need_set_rewritten")]
        if not ladder_moved and moved:
            attribution_only.append(label)
        verdict = "PASS" if moved else "FAIL"
        failures += not moved
        if _int(after["canonical_capabilities"]) == _int(live["canonical_capabilities"]) - 1:
            reducing.append(label)
        reading = ", ".join(moved)
        print(f"  [{verdict}] fold {label} -> {old} drop={json.dumps(drop)}: {reading}")
    identity = "PASS" if reducing == unique_labels else "FAIL"
    failures += identity == "FAIL"
    print(
        f"  [{identity}] canonical-reducing folds measured={len(reducing)} "
        f"{json.dumps(reducing)} derived={len(unique_labels)} {json.dumps(unique_labels)}"
    )
    print(
        f"  attribution-only folds (need set moves, every ladder counter holds): "
        f"{len(attribution_only)} {json.dumps(attribution_only)}"
    )

    print("CONTROL ARM (a fold onto the value the label already holds must move nothing)")
    control = next(label for label in sorted(live_map) if label not in folds)
    moved = _moved(live, _face(mcc, {control: live_map[control]}, ()))
    only_zero = moved == ["rows_need_set_rewritten 0"]
    verdict = "PASS" if only_zero else "FAIL"
    failures += not only_zero
    print(f"  [{verdict}] fold {control} -> itself: {', '.join(moved)}")

    print("ALL-FOLD ARM (the pre-split map with the split's registry entries removed)")
    reverted = _face(mcc, folds, drop_all)
    moved = _moved(live, reverted)
    ladder_moved = [part for part in moved if not part.startswith("rows_need_set_rewritten")]
    verdict = "PASS" if ladder_moved else "FAIL"
    failures += not ladder_moved
    print(f"  [{verdict}] {', '.join(moved)}")
    refuted = {k: reverted[k] for k in LADDER_COUNTERS}
    print(f"  refuted face: {json.dumps(refuted, sort_keys=True)}")
    print(f"  refuted shipped_cover rows: {json.dumps(_seq(reverted['shipped_cover_ids']))}")
    only_old = sorted(
        set(_seq(reverted["shipped_cover_ids"])) - set(_seq(live["shipped_cover_ids"]))
    )
    print(
        f"  rows the pre-split map called declarable today and the split map does not: "
        f"{len(only_old)} {json.dumps(only_old)}"
    )
    print(
        f"SPLIT_PROBE folds={len(folds)} unique_holder_labels={len(unique_labels)} "
        f"counters={len(COUNTERS)} failures={failures}"
    )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """Dispatch the carrier emitter and the fold probe."""
    parser = argparse.ArgumentParser(description="C86 label-split counterfact")
    parser.add_argument("--emit-carrier", type=Path, help="pre-split census instrument file to pin")
    parser.add_argument("--commit", default=SPLIT_PARENT, help="commit the carrier names")
    parser.add_argument("--probe", action="store_true", help="run the fold arms")
    args = parser.parse_args(argv)
    if args.emit_carrier is not None:
        return emit_carrier(args.emit_carrier, args.commit)
    if args.probe:
        return probe()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
