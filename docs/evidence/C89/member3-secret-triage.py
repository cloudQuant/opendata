"""C89: what gate member 3's single credential finding really was, and what closed it.

Run:  python docs/evidence/C89/member3-secret-triage.py     (read-only, offline, writes nothing)

Member 3 pins ``gitleaks detect --source . --config .gitleaks.toml --redact`` over the whole
history. At gate pass 4 it reported 1 finding, in this round's own evidence archive: an
identifier-list repr that put three census row ids on one line, where the name of the second
one contains ``Key``. ``generic-api-key`` read that as a key name and the next list entry as
its value. Nothing in the tree is a credential, so the question this instrument answers is
which of the two available remedies is honest:

* widen ``.gitleaks.toml`` with a sixth regex for the captured literal, or
* change the print so no two long identifiers share a line, so there is nothing to exempt.

The remedy chosen is the second, and the arms below are what make that a measurement rather
than a preference: the old bytes report the finding under the shipped config AND under the
default ruleset with every allowlist removed, the new bytes report none under either (so no
exemption is doing the work), and a fabricated credential dropped into the same one-per-line
face is still reported. The flagged line itself is never written into this file or into any
committed artifact -- it is recomputed from the staged census JSON at run time, because
committing the pair again would re-open the finding this capture exists to close.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess  # nosec B404  # read-only git argv, shell disabled
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
HERE = REPO / "docs/evidence/C89"
ARCHIVE = HERE / "census-staged-112.txt"
STAGED_JSON = HERE / "census-staged-112.json"
SHAPE = re.compile(r"""Key[A-Za-z]*['"],\s*['"][A-Za-z0-9=_\-]{14,}""")
SCANNED_SUFFIXES = frozenset({".txt", ".json", ".md", ".py", ".yaml", ".yml"})


def load_scanner() -> ModuleType:
    """Import this round's gitleaks differential so its scanner and tally reader are shared."""
    path = HERE / "gitleaks-counterfactual.py"
    spec = importlib.util.spec_from_file_location("c89_gitleaks_cf", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the shared scanner from {path}")
    module = importlib.util.module_from_spec(spec)
    # register before exec: its dataclasses resolve their own class through sys.modules
    sys.modules["c89_gitleaks_cf"] = module
    spec.loader.exec_module(module)
    return module


def old_format_line() -> str:
    """Rebuild the flagged print from the staged census data, never from a transcription."""
    rows = json.loads(STAGED_JSON.read_text(encoding="utf-8"))
    ids = [str(row["task_id"]) for row in rows if not row.get("needs_engine_capability")]
    return f"staged rows with an empty need set: {len(ids)} {ids}"


def shape_hits(text: str) -> int:
    """Count lines of ``text`` carrying the key-name-then-long-value shape."""
    return sum(1 for line in text.splitlines() if SHAPE.search(line))


def tracked_shape_census(git: str) -> tuple[int, list[str]]:
    """Every tracked line that carries the shape, counted off ``git ls-files``."""
    listing = subprocess.run(  # nosec B603  # noqa: S603
        [git, "ls-files"], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    hits: list[str] = []
    for rel in listing:
        path = REPO / rel
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if shape_hits(text):
            hits.append(rel)
    return len(hits), hits


def main() -> int:
    """Score the three faces: the finding, the fix, and the credential the fix cannot hide."""
    cf = load_scanner()
    shipped: str = cf.SHIPPED
    baseline: str = cf.BASELINE_CONFIG
    old_line = old_format_line()
    new_bytes = ARCHIVE.read_text(encoding="utf-8")

    print("== the finding (recomputed from the staged census JSON, not transcribed) ==")
    print(f"  old print: {len(old_line)} chars, carries the shape: {bool(SHAPE.search(old_line))}")
    print(f"  new archive carries the shape on: {shape_hits(new_bytes)} line(s)")
    old_shipped: set[str] = cf.rules_for({"e.txt": old_line}, shipped)
    old_default: set[str] = cf.rules_for({"e.txt": old_line}, baseline)
    print(f"  old bytes, shipped config : rules={sorted(old_shipped)}")
    print(f"  old bytes, no allowlists  : rules={sorted(old_default)}")

    print("\n== the fix (the regenerated archive's bytes under both rulesets) ==")
    new_shipped: set[str] = cf.rules_for({"e.txt": new_bytes}, shipped)
    new_default: set[str] = cf.rules_for({"e.txt": new_bytes}, baseline)
    print(f"  new bytes, shipped config : rules={sorted(new_shipped)}")
    print(f"  new bytes, no allowlists  : rules={sorted(new_default)}")

    print("\n== the damage arm (a real-shaped credential in the same one-per-line face) ==")
    damaged: set[str] = cf.rules_for({"e.txt": f"{new_bytes}\n- {cf.FAKE_TOKEN}\n"}, shipped)
    print(f"  new bytes + fabricated token, shipped: rules={sorted(damaged)}")

    print("\n== tree-wide exposure of the shape (git ls-files, per file) ==")
    total, where = tracked_shape_census(str(cf.GIT))
    print(f"  tracked files with such a line: {total} {where[:5]}")
    print(f"  positive control, the recomputed old line: {len(SHAPE.findall(old_line))} hit(s)")

    scan_path = REPO / "docs/quality/secret-scan.json"
    argv = [str(a) for a in json.loads(scan_path.read_text(encoding="utf-8"))["scan_argv"]]
    run = subprocess.run(  # nosec B603  # noqa: S603
        argv, cwd=REPO, capture_output=True, text=True
    )
    try:
        leaks, tally = cf.parse_leak_tally(run.stdout + run.stderr)
    except LookupError as exc:
        print(f"SCAN FAIL: the live scanner printed no tally: {exc}")
        return 1
    print("\n== the pinned gate command over the real repo ==")
    print(f"  rc={run.returncode} leaks_found={leaks} :: {tally}")

    problems: list[str] = []
    if not old_default:
        problems.append("old bytes report nothing even with every allowlist removed: vacuous")
    if old_shipped != old_default:
        problems.append("the finding depends on an exemption, so this pair does not isolate format")
    if new_shipped or new_default:
        problems.append("the new bytes still report a finding under one of the two rulesets")
    if not damaged:
        problems.append("the one-per-line face hides a fabricated credential: a silence, not a fix")
    if total:
        problems.append(f"{total} tracked file(s) still carry the shape")
    if leaks or run.returncode:
        problems.append("the live history scan still reports a credential (run after committing)")
    for line in problems:
        print(f"  TRIAGE FAIL {line}")
    if problems:
        return 1
    print(
        "VERDICT: the finding was this round's own print format. Closed with no exemption --"
        " old bytes report 1 under both rulesets, new bytes report 0 under both, and a"
        f" fabricated credential in the same face is still reported by {sorted(damaged)}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
