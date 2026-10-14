"""Idempotence arm for the C102 grant repair.

The rewriter's population is the 37 node ids captured *before* the repair, so re-running it on the
repaired file still prints ``failing_nodes=37``. This arm answers the two questions that reading
that number would otherwise leave open:

1. does every one of those 37 fetch sites now actually carry a ``ctx=`` grant, and
2. what happens if someone trusts the stale count and runs ``--apply`` a second time.

Question 2 is answered in memory only: the staged text is re-parsed and nothing is written.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
TARGET = ROOT / "tests/test_ths_provider.py"
PROBE = ROOT / "docs/evidence/C102/probes/ths_grant_repair.py"


def fetch_sites(tree: ast.AST) -> list[ast.Call]:
    """Every ``<expr>.fetch(...)`` / ``.fetch_async(...)`` call in the module."""
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"fetch", "fetch_async"}
        ):
            found.append(node)
    return found


def main() -> int:
    source = TARGET.read_text(encoding="utf-8")
    tree = ast.parse(source)
    sites = fetch_sites(tree)
    with_ctx = [c for c in sites if any(k.arg == "ctx" for k in c.keywords)]
    without_ctx = [c for c in sites if not any(k.arg == "ctx" for k in c.keywords)]
    offline_calls = source.count("ctx=_offline_context(")

    print(f"face: fetch_sites={len(sites)} with_ctx={len(with_ctx)} without_ctx={len(without_ctx)}")
    print(f"face: ctx_offline_context_call_sites={offline_calls}")
    live_lines = []
    for call in without_ctx:
        owner = enclosing_class(tree, call.lineno)
        live_lines.append((owner, call.lineno))
        print(f"  NO_CTX line={call.lineno} class={owner}")
    owners = {owner for owner, _ in live_lines}
    print(f"face: ctx_less_site_classes={sorted(owners)}")

    # Arm 2: a second --apply would put a second ``ctx=`` on the same call.  The language refuses a
    # repeated keyword at compile time, not at ast.parse -- which is why the probe's write guard now
    # compiles instead of merely parsing, and why this arm says so out loud.
    try:
        ast.parse("fetch(ctx=1, ctx=2)")
        print("arm_repeated_keyword: ast.parse ACCEPTS it (a parse-only guard would not stop corruption)")
    except SyntaxError as exc:
        print(f"arm_repeated_keyword: ast.parse refused ({exc.msg})")
    try:
        compile("fetch(ctx=1, ctx=2)", "<arm>", "exec")
        second_apply_guard = "UNGUARDED"
    except SyntaxError as exc:
        second_apply_guard = f"compile_refused ({exc.msg})"
    print(f"arm_second_apply: duplicate_keyword={second_apply_guard}")
    guard_text = PROBE.read_text(encoding="utf-8")
    compiles_staged = 'compile(staged, str(TEST_FILE), "exec")' in guard_text
    skips_granted = 'keyword.arg == "ctx" for keyword in call.keywords' in guard_text
    print(f"arm_probe_guard: compiles_staged={compiles_staged} skips_already_granted={skips_granted}")

    healthy = (
        len(with_ctx) == 37
        and len(without_ctx) == 7
        and offline_calls == 37
        and owners == {"TestAgainstTheLiveFuyaoApi"}
        and second_apply_guard.startswith("compile_refused")
        and compiles_staged
        and skips_granted
    )
    print("verdict: " + ("PASS - 37 offline sites granted, the 7 e2e sites carry no fixture grant"
                         if healthy else "DISAGREED"))
    return 0 if healthy else 1


def enclosing_class(tree: ast.AST, lineno: int) -> str:
    """Innermost ClassDef covering ``lineno``."""
    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and node.lineno <= lineno
        and (node.end_lineno is None or lineno <= node.end_lineno)
    ]
    if not candidates:
        return "?"
    return max(candidates, key=lambda node: node.lineno).name



if __name__ == "__main__":
    raise SystemExit(main())
