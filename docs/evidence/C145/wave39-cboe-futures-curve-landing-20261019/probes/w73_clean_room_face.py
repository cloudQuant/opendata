"""Clean-room face: OpenBB is the contract reference, never a dependency.

The iteration-2 brief says the 32 provider packages are this repo's own implementations and that
OpenBB serves only as an output-contract comparison. Nothing in the shipped gate checks that:
``scripts/quality/vendor_independence.py`` audits the MIT-vendored *akshare SDK* across the BSL
boundary, which is a different boundary, so "the gate is green" is not evidence for the clean-room
claim.

Every provider module and contract model is read through ``ast``, so an import hidden in a function
body or a lazy facade is still seen, and three things a grep collapses are separated: a real
``import openbb*`` (a violation), a prose citation of an upstream contract file (the permitted and
required reference use), and the MIT-vendor tree (third-party SDK code outside this claim's scope
by construction, so it is counted and excluded visibly rather than silently).

The arms are the point. A zero-violation reading means nothing until the same detector is shown
reporting one, so a synthesized upstream import is injected into a real module body in memory and
must raise the count. The scan is only a scan if the population is named: files parsed, files
skipped, parse failures, and the provider packages the pinned ledger covers but that do not exist.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
PROVIDERS = REPO / "opendata/data/providers"
MODELS = REPO / "opendata/data/models"
#: The pinned 350-row task ledger; its provider column is the objective's "32 providers" scope.
LEDGER = REPO / ("docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv")
RUNS = REPO / "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019/runs"
VENDOR_MARK = "_vendor"
FORBIDDEN_ROOTS = ("openbb", "openbb_core", "openbb_platform")


def provider_packages() -> list[str]:
    """Provider directories that declare a ``provider.py`` descriptor."""
    return sorted(
        child.name
        for child in PROVIDERS.iterdir()
        if child.is_dir() and (child / "provider.py").is_file()
    )


def parse_one(path: Path) -> tuple[ast.Module | None, str]:
    """Parse one file; a failure comes back as a reason instead of raising out of the scan."""
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path)), ""
    except SyntaxError as exc:
        return None, f"{path.name}:{exc.lineno}"


def scopes_for_missing() -> list[str]:
    """The provider names the pinned inventory covers, read from the ledger."""
    import csv

    with LEDGER.open(encoding="utf-8-sig", newline="") as handle:
        return sorted({str(row["provider"]) for row in csv.DictReader(handle)})


def python_files(root: Path) -> list[Path]:
    """Every tracked Python file under one root."""
    return sorted(p for p in root.rglob("*.py") if p.is_file() and "__pycache__" not in p.parts)


def imported_modules(tree: ast.AST) -> list[str]:
    """Every module name one syntax tree imports, including inside functions."""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


def violations_in(tree: ast.AST) -> list[str]:
    """Import roots that would make OpenBB a dependency of this code."""
    hits: list[str] = []
    for module in imported_modules(tree):
        root_name = module.split(".", 1)[0]
        if root_name in FORBIDDEN_ROOTS:
            hits.append(module)
    return hits


def prose_cites(path: Path) -> int:
    """Mentions of an upstream contract file in text, which is the permitted reference use."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text.count("openbb_") + text.count("OpenBB")


def main() -> int:
    """Scan, count, and run every arm; write the carrier."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--suffix", default="", help="carrier suffix for a second take")
    args = parser.parse_args()
    lines: list[str] = []
    arms: dict[str, bool] = {}

    scopes = {"providers": python_files(PROVIDERS), "models": python_files(MODELS)}
    in_scope = [f for group in scopes.values() for f in group if VENDOR_MARK not in f.parts]
    vendored = [f for group in scopes.values() for f in group if VENDOR_MARK in f.parts]

    parsed: dict[Path, ast.Module] = {}
    failed: list[str] = []
    for path in in_scope:
        tree, reason = parse_one(path)
        if tree is None:
            failed.append(reason)
        else:
            parsed[path] = tree

    offenders: dict[str, list[str]] = {}
    for path, tree in parsed.items():
        hits = violations_in(tree)
        if hits:
            offenders[str(path.relative_to(REPO))] = hits

    cite_files = sum(1 for path in in_scope if prose_cites(path) > 0)
    lines.append(
        f"scope in_scope_files={len(in_scope)} vendored_excluded={len(vendored)} "
        f"parsed={len(parsed)} parse_failures={len(failed)} failed={failed[:5]}"
    )
    lines.append(
        f"openbb_import_violations={len(offenders)} offenders={sorted(offenders)[:5]} "
        f"prose_cite_files={cite_files} provider_packages={len(provider_packages())} "
        f"inventory_without_package={sorted(set(scopes_for_missing()) - set(provider_packages()))}"
    )

    # Detector arms: inject an import into a real module's body and require the count to move.
    sample_path = next(iter(parsed))
    sample = parsed[sample_path]
    injected = ast.parse(
        "from openbb_core.provider.abstract.data import Data\n" + ast.unparse(sample)
    )
    forged_hits = violations_in(injected)
    lines.append(
        f"counterfact file={sample_path.relative_to(REPO)} forged_hits={forged_hits} "
        f"real_hits_on_same_file={violations_in(sample)}"
    )

    # The other direction of leakage: reaching the reference checkout by absolute path at runtime.
    abs_path_hits = [
        str(path.relative_to(REPO))
        for path in in_scope
        if "new_projects/openbb" in path.read_text(encoding="utf-8", errors="replace")
    ]
    lines.append(
        f"absolute_reference_path_in_code_files={len(abs_path_hits)} names={abs_path_hits[:5]}"
    )

    arms["A_every_scanned_file_parsed"] = failed == [] and len(parsed) == len(in_scope) > 300
    arms["B_no_openbb_imports"] = offenders == {}
    arms["C_detector_reports_an_injected_import"] = (
        bool(forged_hits) and violations_in(sample) == []
    )
    arms["D_vendor_scope_is_named_not_silent"] = len(vendored) > 0
    arms["E_contract_references_exist"] = cite_files > 0
    arms["F_no_runtime_path_to_the_reference"] = abs_path_hits == []

    passed = sum(1 for v in arms.values() if v)
    ok = passed == len(arms)
    lines.append(
        f"clean_room_face arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(k for k, v in arms.items() if not v)}"
    )
    text = "\n".join(lines) + "\n"
    text = text + f"w73_clean_room_face_exit={0 if ok else 1}" + "\n"
    RUNS.mkdir(parents=True, exist_ok=True)
    out = RUNS / "w73_clean_room_face.txt"
    if args.suffix:
        out = out.with_name(f"w73_clean_room_face.{args.suffix}.txt")
    out.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"w73_clean_room_face_exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
