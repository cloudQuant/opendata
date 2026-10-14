"""Give the offline ths fetch sites a finite fixture grant.

``Fetcher.fetch`` (``opendata/data/protocol.py``) refuses a canonical model whose
budget carries no matching grant before any provider I/O. The 37 offline cases in
``tests/test_ths_provider.py`` call ``fetcher.fetch(...)`` with no context, so the
shared zero budget denies them and the adapter code under test never runs. This
tool inserts one keyword per failing site:

    ctx=_offline_context("<canonical model>")

The model literal is **derived**, not typed by hand: each site's receiver is
resolved to the fetcher class that test constructs (an instance expression, an
assigned name, a helper method's return annotation, or the domain string a
registry route was resolved with), and that class's own ``canonical_model``
declaration supplies the value. A site that cannot be resolved aborts the run
instead of being guessed.

Modes:
    --dry    print the plan and the denominators, write nothing (default)
    --apply  write, but only after every staged file re-parses

Exit code is 1 when any node is uncovered or any site is unresolved, so the
instrument cannot report success for a partial repair.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[4]
TEST_FILE = REPO / "tests/test_ths_provider.py"
NODE_FILE = REPO / "docs/evidence/C102/ths-grants/runs/failing-nodes-before.txt"
MODEL_DIR = REPO / "opendata/data/providers/ths/models"

#: Marker for a site whose grant identity can only be read off the live fetcher.
RUNTIME_IDENTITY = "<runtime>"


def _class_models() -> dict[str, str]:
    """Map every ths fetcher class name to its declared ``canonical_model``."""
    models: dict[str, str] = {}
    for path in sorted(MODEL_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for statement in node.body:
                target = None
                value = None
                if isinstance(statement, ast.AnnAssign):
                    target, value = statement.target, statement.value
                elif isinstance(statement, ast.Assign):
                    target, value = statement.targets[0], statement.value
                if isinstance(target, ast.Name) and target.id == "canonical_model":
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        models[node.name] = value.value
    return models


def _receiver_model(
    receiver: ast.expr,
    function: ast.FunctionDef,
    class_node: ast.ClassDef,
    models: dict[str, str],
    *,
    depth: int = 0,
) -> str:
    """Resolve one ``.fetch(...)`` receiver to a canonical model, or raise LookupError.

    Resolution only ever looks inside the one test function and the one class that
    contains it: a sibling method's ``fetcher = ThsXFetcher()`` binding is not
    evidence about this method's receiver.
    """
    if depth > 3:
        raise LookupError("receiver resolution nested too deeply")

    if isinstance(receiver, ast.Call):
        func = receiver.func
        if isinstance(func, ast.Name) and func.id in models:
            return models[func.id]
        if isinstance(func, ast.Attribute) and func.attr.lstrip("_").startswith(("patch", "fetcher")):
            # A test helper: its return annotation names the fetcher class.
            for node in class_node.body:
                if isinstance(node, ast.FunctionDef) and node.name == func.attr:
                    annotation = node.returns
                    if isinstance(annotation, ast.Name) and annotation.id in models:
                        return models[annotation.id]
            raise LookupError(f"helper {func.attr} has no resolvable return annotation")
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "resolve_domain"
            and receiver.args
            and isinstance(receiver.args[0], ast.Constant)
        ):
            # Registry-routed: the domain string *is* the grant identity.
            return str(receiver.args[0].value)
        raise LookupError(f"unsupported call receiver {ast.unparse(receiver)[:60]!r}")

    if isinstance(receiver, ast.Name):
        parameters = [arg.arg for arg in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)]
        if receiver.id in parameters:
            # Parametrized fetcher: one literal cannot serve every arm, so the
            # call passes the live object and the grant tracks it per arm.
            return RUNTIME_IDENTITY
        for statement in ast.walk(function):
            if not isinstance(statement, ast.Assign):
                continue
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id == receiver.id:
                    return _receiver_model(statement.value, function, class_node, models, depth=depth + 1)
        raise LookupError(f"name {receiver.id!r} has no resolvable binding in this test")

    raise LookupError(f"unsupported receiver {ast.unparse(receiver)[:60]!r}")



def _line_offsets(source: str) -> list[int]:
    offsets = [0]
    for index, character in enumerate(source):
        if character == "\n":
            offsets.append(index + 1)
    return offsets


def _open_paren(source: str, offsets: list[int], func_end: tuple[int, int]) -> int:
    """Return the offset just past the ``(`` of a call, given the end of its func.

    Located by an ASCII needle rather than by ``col_offset``: the AST measures
    columns in bytes, and this file's Chinese docstrings would push a
    byte-indexed offset past the character it names.
    """
    lineno, _ = func_end
    line_start = offsets[lineno - 1]
    line_end = offsets[lineno] if lineno < len(offsets) else len(source)
    position = source.find(".fetch(", line_start)
    if position < 0 or position >= line_end:
        raise AssertionError(f"no '.fetch(' on source line {lineno}")
    return position + len(".fetch(")


def _node_parts(node_id: str) -> tuple[str, str]:
    """Split ``path.py::Class::method[param]`` into the class and method names."""
    _, _, tail = node_id.partition("::")
    *classes, method = tail.split("::")
    return "::".join(classes), method.split("[", 1)[0]


def _planned(source: str, nodes: list[str], models: dict[str, str]) -> tuple[list[tuple[int, str, str]], list[str], list[str], int]:
    """Return (insertions, unresolved messages, uncovered node ids, already-granted sites)."""
    wanted: dict[str, set[str]] = {}
    for node_id in nodes:
        cls, name = _node_parts(node_id)
        wanted.setdefault(cls, set()).add(name)

    tree = ast.parse(source)
    offsets = _line_offsets(source)
    insertions: list[tuple[int, str, str]] = []
    unresolved: list[str] = []
    already_granted = 0
    covered: set[tuple[str, str]] = set()

    for class_node in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
        methods = wanted.get(class_node.name)
        if not methods:
            continue
        functions = [
            statement
            for statement in class_node.body
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for function in functions:
            if function.name not in methods:
                continue
            hits = 0
            for call in ast.walk(function):
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "fetch"
                ):
                    continue
                hits += 1
                if any(keyword.arg == "ctx" for keyword in call.keywords):
                    # A site this round already granted.  Re-inserting here would put a second
                    # ``ctx=`` on the same call, and ``ast.parse`` alone does not refuse a repeated
                    # keyword -- only ``compile`` does (see ths_idempotence_arm.py).
                    already_granted += 1
                    continue
                try:
                    model = _receiver_model(call.func.value, function, class_node, models)
                except LookupError as exc:
                    unresolved.append(f"{class_node.name}::{function.name}: {exc}")
                    continue
                opening = _open_paren(source, offsets, (call.func.end_lineno, call.func.end_col_offset))
                separator = ", " if (call.args or call.keywords) else ""
                identity = (
                    ast.unparse(call.func.value)
                    if model == RUNTIME_IDENTITY
                    else repr(model)
                )
                insertions.append(
                    (opening, f"ctx=_offline_context({identity}){separator}", model)
                )
            if hits:
                covered.add((class_node.name, function.name))

    missing = [node_id for node_id in nodes if _node_parts(node_id) not in covered]
    return insertions, unresolved, missing, already_granted


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the staged file")
    args = parser.parse_args(argv)

    source = TEST_FILE.read_text(encoding="utf-8")
    nodes = [line.strip() for line in NODE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    models = _class_models()
    insertions, unresolved, missing, already_granted = _planned(source, nodes, models)

    used = sorted({model for _, _, model in insertions})
    print(
        "face: failing_nodes="
        f"{len(nodes)} fetcher_classes={len(models)} insertions={len(insertions)} "
        f"unique_insertions={len(set((offset, text) for offset, text, _ in insertions))} "
        f"unresolved={len(unresolved)} uncovered_nodes={len(missing)} already_granted={already_granted}"
    )
    print(f"models_used={len(used)}:{','.join(used)}")
    for offset, _, model in sorted(insertions):
        line = source.count("\n", 0, offset) + 1
        print(f"  site line={line} model={model}")
    for message in unresolved:
        print(f"  UNRESOLVED {message}")
    for node_id in missing:
        print(f"  UNCOVERED {node_id}")

    if unresolved or missing:
        print("verdict: refused (every failing node needs one resolvable fetch site)")
        return 1

    if not args.apply:
        print("verdict: dry run, nothing written")
        return 0

    staged = source
    for offset, text, _ in sorted(insertions, reverse=True):
        staged = staged[:offset] + text + staged[offset:]
    try:
        compile(staged, str(TEST_FILE), "exec")
    except SyntaxError as exc:
        print(f"verdict: refused, staged file does not compile ({exc})")
        return 1
    if not insertions:
        print("verdict: nothing to insert (every node is granted, unresolved or uncovered)")
        return 1
    TEST_FILE.write_text(staged, encoding="utf-8")
    print(f"verdict: applied bytes_before={len(source)} bytes_after={len(staged)} added={len(staged) - len(source)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
