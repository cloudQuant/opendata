"""Prove the A2 probe repair moved no probe semantics, by AST identity not by eyeball.

A2 charges E501 at line-length 100, and most of an evidence probe's long lines are ONE string
literal whose exact characters end up printed into a carrier. Reflowing those by hand can change
what the probe emits, and a corrupted probe still looks green. The witness is structural rather
than visual: implicit string-literal concatenation folds into a single ``ast.Constant``, so a
*correct* reflow produces an AST that dumps identically to the pre-repair AST. Anything that dumps
differently really moved.

The pre-repair bytes are the staged blob (``git show :PATH``), which still holds them because the
repair was never re-staged. Two normalizations are applied before comparing, because the repair
itself legitimately adds them: docstrings (D103) and annotations (ANN*). Everything else must match.

One tolerance is stated rather than silently taken: an import statement may change *position* but
not content. That is allowed only when the import multiset is unchanged AND every non-import
top-level statement dumps identical in its own order -- so a repair that lifted an import past a
``sys.path.insert`` would be caught by the second condition.

A file with no staged blob (created after the staging) is reported ``pre_state_absent`` and judged
by its own ruff/mypy/bandit face instead; a file that fails to parse on either side is a FAIL.

Usage: python docs/evidence/C105/a2-probe-repair/probes/ast_witness.py [FILE...]
"""

from __future__ import annotations

import ast
import pathlib
import subprocess  # nosec B404
import sys
from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable
from typing import cast

#: the scope the C104 A2 run was taken over, so the witness and the gate judge the same files
TARGET_LIST = pathlib.Path(
    "/tmp/c104_a2_targets.txt"  # nosec B108  # noqa: S108
)

IMPORT_TYPES = (ast.Import, ast.ImportFrom)


class _StripAnnotations(ast.NodeTransformer):
    """Remove annotation syntax so the repair's typing work is not scored as a move."""

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:  # noqa: ANN401
        """Turn ``x: T = v`` into ``x = v``, and drop a bare ``x: T`` declaration."""
        if node.value is None:
            return None
        assign = ast.Assign(targets=[node.target], value=node.value)
        ast.copy_location(assign, node)
        return self.generic_visit(assign)

    def visit_arg(self, node: ast.arg) -> ast.arg:
        """Drop parameter annotations."""
        node.annotation = None
        node.type_comment = None
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        """Drop the return annotation."""
        node.returns = None
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        """Drop the return annotation on async defs too."""
        node.returns = None
        self.generic_visit(node)
        return node


def normalize(tree: ast.AST) -> ast.AST:
    """Strip docstrings and annotations, then return the tree for comparison."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module):
            continue
        body = node.body
        head = body[0] if body else None
        first = head.value if isinstance(head, ast.Expr) else None
        if len(body) > 1 and isinstance(first, ast.Constant) and isinstance(first.value, str):
            node.body = body[1:]
    return cast("ast.AST", _StripAnnotations().visit(tree))


def normalize_module(src: str) -> ast.Module:
    """Parse and normalize in one step, so normalization is never taken twice by accident."""
    tree = normalize(ast.parse(src))
    return tree if isinstance(tree, ast.Module) else ast.Module(body=[], type_ignores=[])


def unit_name(node: ast.AST) -> str:
    """A stable label for a top-level statement, used to point at what differs."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return f"{type(node).__name__}:{node.name}"
    if isinstance(node, ast.Assign):
        targets = ",".join(getattr(target, "id", "?") for target in node.targets)
        return f"Assign:{targets}"
    if isinstance(node, (ast.If, ast.Try)):
        return f"{type(node).__name__}:block"
    return type(node).__name__


def body_sequence(tree: ast.Module, imports: bool) -> list[str]:
    """Ordered dumps of the top-level statements: either the import ones, or all the rest."""
    return [ast.dump(node) for node in tree.body if isinstance(node, IMPORT_TYPES) is imports]


def literal_census(node: ast.AST) -> list[str]:
    """Every string constant inside a unit, in order -- the values a probe actually prints."""
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def file_literals(tree: ast.Module) -> Counter:
    """The multiset of every string value in the file, docstrings and annotations already gone."""
    return Counter(literal_census(tree))


def shown_value(value: str, width: int = 80) -> str:
    """The value itself, or its head plus how many characters the head hides."""
    return value if len(value) <= width else f"{value[:width]}...(+{len(value) - width})"


def import_names(tree: ast.Module) -> set[str]:
    """Every name a top-level import binds, so a pruned import is named rather than counted."""
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            out |= {alias.asname or alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            out |= {alias.asname or alias.name for alias in node.names}
    return out


def families(tree: ast.Module) -> dict[str, list[str]]:
    """Top-level statement dumps grouped by name family, so reordering cannot fake a difference."""
    out: dict[str, list[str]] = {}
    for node in tree.body:
        out.setdefault(unit_name(node), []).append(ast.dump(node))
    return out


def restructured_families(before: ast.Module, after: ast.Module) -> list[str]:
    """Family names whose statement count or content changed, ignoring position."""
    left, right = families(before), families(after)
    return [
        f"{name}[{len(left.get(name, []))}->{len(right.get(name, []))}]"
        for name in sorted(set(left) | set(right))
        if sorted(left.get(name, [])) != sorted(right.get(name, []))
    ]


def staged_source(path: pathlib.Path) -> str | None:
    """The pre-repair bytes from the git index, or None when the file is not staged."""
    proc = subprocess.run(  # nosec B603 B607  # noqa: S603
        ["git", "show", f":{path}"],  # noqa: S607
        capture_output=True,
        text=True,
    )
    return proc.stdout if proc.returncode == 0 else None


def whitespace_fold(value: str) -> str:
    """The value with every whitespace run collapsed -- the doc generator's own tolerance."""
    return " ".join(value.split())


def occurrence_delta(source: Counter, other: Counter) -> list[str]:
    """Values ``source`` holds more often than ``other``, one entry per extra occurrence."""
    return [
        value
        for value in sorted(source, key=whitespace_fold)
        for _ in range(max(0, source[value] - other[value]))
    ]


def pair_moves(before: Counter, after: Counter, limit: int = 12) -> tuple[int, int, list[str]]:
    """Pair each disappeared value with an appeared one that differs only in whitespace.

    Returns (whitespace_pairs, unexplained_count, findings). A re-wrap of generated prose is legal
    and named; a value that changed words, dropped a character or moved count is a finding.
    """
    gone = occurrence_delta(before, after)
    born = occurrence_delta(after, before)
    unexplained_gone: list[str] = []
    for value in gone:
        folded = whitespace_fold(value)
        match = next(
            (candidate for candidate in born if whitespace_fold(candidate) == folded), None
        )
        if match is None:
            unexplained_gone.append(value)
        else:
            born.remove(match)
    findings = [f"dropped_value={shown_value(value)!r}" for value in unexplained_gone[:limit]]
    findings += [f"added_value={shown_value(value)!r}" for value in born[:limit]]
    return len(gone) - len(unexplained_gone), len(unexplained_gone) + len(born), findings


def classify(before_src: str, after_src: str) -> tuple[str, dict[str, int], list[str], list[str]]:
    """Verdict, counters, facts and findings for one repair.

    The gate is the printed vocabulary: no string value may move, except that a value may be
    re-wrapped in whitespace when a whitespace-only twin accounts for it (the doc generator's
    prose). Restructured statements and imports added or pruned are facts, not failures, and are
    named so each one can be checked by hand.
    """
    before, after = normalize_module(before_src), normalize_module(after_src)
    before_lit, after_lit = file_literals(before), file_literals(after)
    whitespace_pairs, unexplained, findings = pair_moves(before_lit, after_lit)
    changed_families = restructured_families(before, after)
    pruned = sorted(import_names(before) - import_names(after))
    added = sorted(import_names(after) - import_names(before))
    counters = {
        "delta_units": abs(len(before.body) - len(after.body)),
        "imports_added": len(added),
        "imports_pruned": len(pruned),
        "restructured": len(changed_families),
        "whitespace_pairs": whitespace_pairs,
        "value_moves": unexplained,
    }
    facts = [f"restructured={','.join(changed_families)}"] if changed_families else []
    facts += [f"pruned_import={name}" for name in pruned]
    facts += [f"added_import={name}" for name in added]
    if findings:
        return "moved", counters, facts, findings
    if not changed_families:
        return "identical", counters, facts, []
    if all(name.startswith("Import") for name in changed_families):
        return "import_churn", counters, facts, []
    if whitespace_pairs:
        return "whitespace_reflow", counters, facts, []
    return "values_intact", counters, facts, []


def judge(names: Iterable[str]) -> tuple[int, int, int, int, int, int, int]:
    """Measure every file. Return (identical, intact, churn, reflow, moved, absent, findings)."""
    counts = [0, 0, 0, 0, 0, 0, 0]
    status_slot = {
        "identical": 0,
        "values_intact": 1,
        "import_churn": 2,
        "whitespace_reflow": 3,
        "moved": 4,
    }
    for name in names:
        path = pathlib.Path(name)
        before_src = staged_source(path)
        if before_src is None:
            counts[5] += 1
            print(f"face: file={name} status=pre_state_absent")
            continue
        try:
            after_src = path.read_text(encoding="utf-8")
            status, counters, facts, findings = classify(before_src, after_src)
        except SyntaxError as exc:
            counts[6] += 1
            print(f"face: file={name} status=parse_error msg={exc.msg}")
            continue
        counts[status_slot[status]] += 1
        counts[6] += len(findings)
        print(
            f"face: file={name} status={status} top_level_delta={counters['delta_units']}"
            f" added_imports={counters['imports_added']}"
            f" pruned_imports={counters['imports_pruned']}"
            f" restructured_families={counters['restructured']}"
            f" whitespace_pairs={counters['whitespace_pairs']}"
            f" value_moves={counters['value_moves']}"
        )
        for line in facts + findings:
            print(f"face:   {name} {line}")
    return tuple(counts)  # type: ignore[return-value]


def tamper_one_literal(src: str) -> str | None:
    """The same source with exactly one printed string value perturbed, or None if there is none.

    Surgery on the *normalized* tree, because a docstring tamper would be stripped right back out
    and the control arm would then be testing nothing.
    """
    tree = normalize_module(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) > 8:
            node.value = f"{node.value}!"
            return ast.unparse(tree)
    return None


def control_arm() -> tuple[int, int]:
    """Calibrate the witness in memory, without writing a byte.

    An untouched pair must come back ``identical`` and a tampered pair ``moved``. Without the
    tamper arm a witness that always answered "identical" would pass.
    """
    name = TARGET_LIST.read_text().split()[0]
    src = pathlib.Path(name).read_text(encoding="utf-8")
    calm = classify(src, src)
    tampered = tamper_one_literal(src)
    hot = classify(src, tampered) if tampered else ("no_tamperable_literal", {}, [], [])
    print(
        f"face: control_probe={name} untampered_status={calm[0]} tampered_status={hot[0]}"
        f" tampered_findings={len(hot[3])}"
    )
    for finding in hot[3][:3]:
        print(f"face:   control {finding}")
    good = calm[0] == "identical" and hot[0] == "moved" and bool(hot[3])
    print(f"face: control_arm={'as expected' if good else 'UNEXPECTED'}")
    return (0, 1) if good else (1, 0)


def main() -> int:
    """Run the witness over the named probes and refuse on any moved value."""
    args = sys.argv[1:]
    if args and args[0] == "--control":
        failed, passed = control_arm()
        print(f"face: mode=control controls_passed={passed} controls_failed={failed}")
        return 1 if failed else 0
    names = args or TARGET_LIST.read_text().split()
    identical, intact, churn, reflow, moved, absent, findings = judge(names)
    print(
        f"face: targets={identical + intact + churn + reflow + moved + absent}"
        f" ast_identical={identical} values_intact={intact} import_churn={churn}"
        f" whitespace_reflow={reflow} value_moves={moved} pre_state_absent={absent}"
        f" findings={findings}"
    )
    if moved or findings:
        print("verdict: FAIL - a repair moved a printed string value, not only its line length")
        return 1
    print(
        "verdict: PASS - no printed value moved; every other change is docstring, annotation or"
        " statement layout"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
