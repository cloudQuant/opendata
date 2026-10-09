"""Differential: the shipped builder's AST block census vs the archived artifact's method.

Same 121 files, same pinned commit, all 121 digests verified against disk here. The
question is not which number is "right" but where two descriptions of one method diverge.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import pathlib
import sys
import time
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

REPO = pathlib.Path("/Users/yunjinqi/Documents/new_projects/opendata")
OPENBB = pathlib.Path("/Users/yunjinqi/Documents/new_projects/OpenBB")
INVENTORY = REPO / "docs/evidence/C65/provider-similarity-inventory.json"
BUILDER = REPO / "scripts/quality/provider_source_review_evidence_build.py"


def load_builder() -> ModuleType:
    """Import the shipped builder as a module so the screen calls its real enumerator."""
    spec = importlib.util.spec_from_file_location("bld_diff", BUILDER)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {BUILDER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["bld_diff"] = module
    spec.loader.exec_module(module)
    return module


def node_count(statements: list) -> int:
    """The archived method's wording: raw AST nodes including nested descendants."""
    return sum(1 for statement in statements for _ in ast.walk(statement))


def reference_blocks(tree: ast.Module, floor: int) -> list[tuple[str, int]]:
    """Enumerate blocks exactly as the archived artifact's `method` field describes them.

    "Each function body and each nonempty nested body/orelse/finalbody/handlers/cases statement
    list; blocks smaller than 41 AST nodes excluded." Read literally, **every** nonempty nested
    ``body`` list counts, so a ``ClassDef.body`` is a block here; whether the shipped builder agrees
    is what this arm is for, and that is why the two are not written from the same template.
    """
    out: list[tuple[str, int]] = []

    def emit(statements: list[ast.stmt], kind: str) -> None:
        if not statements or not all(isinstance(s, ast.stmt) for s in statements):
            return
        count = node_count(statements)
        if count >= floor:
            out.append((kind, count))

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                emit(list(child.body), "function_body")
            elif isinstance(child, ast.ClassDef):
                emit(list(child.body), "ClassDef.body")
            else:
                for field in ("body", "orelse", "finalbody"):
                    value = getattr(child, field, None)
                    if isinstance(value, list) and value:
                        emit(value, f"{type(child).__name__}.{field}")
            for handler in getattr(child, "handlers", []) or []:
                emit(list(handler.body), "Try.handlers")
            for case in getattr(child, "cases", []) or []:
                emit(list(case.body), "Match.cases")
            walk(child)

    walk(tree)
    return out


def main() -> None:
    """Print the four readings: tree identity, two censuses, kind histogram, verdict."""
    builder = load_builder()
    inventory = json.loads(INVENTORY.read_text())
    archived = inventory["counts"]["baseline_blocks_over_40_nodes"]
    rows = inventory["source_manifests"]["baseline_files_sha256"]

    print("=== 0. the baseline tree is the reviewed tree (record-vs-disk, not HEAD-vs-disk) ===")
    drift, missing = [], []
    for row in rows:
        path = OPENBB / row["path"]
        if not path.is_file():
            missing.append(row["path"])
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            drift.append(row["path"])
    print(f"  archived manifest entries = {len(rows)}")
    print(f"  digest match on disk      = {len(rows) - len(drift) - len(missing)}")
    print(f"  digest drift              = {len(drift)}  {drift[:3]}")
    print(f"  missing on disk           = {len(missing)}  {missing[:3]}")
    head_path = OPENBB / ".git" / "HEAD"
    resolved = "unresolved"
    if head_path.is_file():
        head_ref = head_path.read_text().strip()
        if head_ref.startswith("ref: "):
            ref_path = OPENBB / ".git" / head_ref[5:]
            packed = OPENBB / ".git" / "packed-refs"
            if ref_path.is_file():
                resolved = ref_path.read_text().strip()
            elif packed.is_file():
                needle = f" {head_ref[5:]}"
                lines = packed.read_text().splitlines()
                resolved = next(
                    (line.split()[0] for line in lines if line.endswith(needle)),
                    head_ref,
                )
            else:
                resolved = head_ref
        else:
            resolved = head_ref
    print(f"  checkout HEAD             = {resolved} (read from .git, not shelled)")
    print(f"  archived baseline_commit  = {inventory['comparison']['baseline_commit']}")
    print(f"  equal                     = {resolved == inventory['comparison']['baseline_commit']}")

    print("\n=== 1. both enumerators over the identical 121 files ===")
    floor = builder.MIN_BLOCK_NODES
    shipped_hist: Counter[str] = Counter()
    reference_hist: Counter[str] = Counter()
    shipped_total = 0
    reference_total = 0
    per_file_diffs: list[tuple[str, int, int]] = []
    started = time.monotonic()
    for row in rows:
        path = OPENBB / row["path"]
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=row["path"])
        shipped = builder._blocks_and_imports(row["path"], tree)[0]
        ref = reference_blocks(tree, floor)
        shipped_hist.update(block.block_kind for block in shipped)
        reference_hist.update(kind for kind, _ in ref)
        shipped_total += len(shipped)
        reference_total += len(ref)
        if len(shipped) != len(ref):
            per_file_diffs.append((row["path"], len(shipped), len(ref)))
    print(f"  shipped builder floor (MIN_BLOCK_NODES) = {floor}")
    print(f"  shipped total   = {shipped_total}")
    print(f"  reference total = {reference_total}")
    print(f"  archived count  = {archived}")
    print(f"  parse time      = {time.monotonic() - started:.1f} s")

    print("\n=== 2. block-kind histograms, side by side ===")
    for kind in sorted(set(shipped_hist) | set(reference_hist)):
        s, r = shipped_hist.get(kind, 0), reference_hist.get(kind, 0)
        flag = "" if s == r else f"   <-- delta {r - s:+d}"
        print(f"  {kind:<28} shipped={s:<6} reference={r:<6}{flag}")

    print("\n=== 3. files where the two disagree (first 10) ===")
    for rel, s, r in per_file_diffs[:10]:
        print(f"  {rel}  shipped={s} reference={r}")
    print(f"  files disagreeing = {len(per_file_diffs)} of {len(rows)}")

    print("\n=== 4. control: does either enumerator reproduce the archived number? ===")
    verdict_reference = "YES" if reference_total == archived else "no"
    verdict_shipped = "YES" if shipped_total == archived else "no"
    print(f"  reference == archived : {reference_total} vs {archived}  -> {verdict_reference}")
    print(f"  shipped   == archived : {shipped_total} vs {archived}  -> {verdict_shipped}")
    print(
        "  note: the shipped builder's own measure_baseline() reported 1105 in the candidate "
        "screen; this section counts blocks straight out of _blocks_and_imports, so a match here "
        "and a mismatch there would locate the difference in aggregation rather than enumeration"
    )

    print("\nVERDICT :", end=" ")
    if shipped_total == reference_total:
        print(
            "the shipped enumerator and the documented method agree on this tree; "
            f"the archived {archived} was produced by code that is no longer in the tree"
        )
    else:
        print(
            "the shipped enumerator does not reproduce the method the archived artifact documents "
            f"(delta {reference_total - shipped_total:+d} blocks)"
        )
    print("DIFFERENTIAL_EXIT=0")


if __name__ == "__main__":
    main()
