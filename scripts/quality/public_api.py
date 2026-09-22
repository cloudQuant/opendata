#!/usr/bin/env python3
"""Public API quality gate (quality spec §8).

Every **new** public callable must ship with:

* a docstring (Google style), and
* a type annotation on every parameter and on the return value.

Scope is the A2 layer only — the contract/provider/pipeline packages plus the
new tooling directories. Legacy A1 modules are intentionally excluded: they are
brought up to standard as they get touched (quality spec §1, "触碰即达标"),
not by relaxing this gate.

Exit code is non-zero unless coverage is 100% / 100%.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# A2 scope: only paths that exist now or are created by iteration 1. Missing
# directories are skipped silently, which is safe because the list is fixed and
# a directory cannot disappear without a scope change being reviewed here.
A2_SCOPE = (
    "opendata/data",
    "opendata/pipeline",
    "opendata_fuyao",
    "opendata_providers",
    "opendata_client",
    "scripts/quality",
    "scripts/codemod",
)

DOCSTRING_MISSING = "missing docstring"
ANNOTATION_MISSING = "missing annotation"


@dataclass(frozen=True)
class Symbol:
    """A public callable that must satisfy the public-API standard.

    Attributes:
        file: Repo-relative POSIX path.
        line: 1-based line number.
        qualname: Dotted name within the module.
        has_docstring: Whether a docstring is present.
        has_full_annotations: Whether every parameter and the return are annotated.
    """

    file: str
    line: int
    qualname: str
    has_docstring: bool
    has_full_annotations: bool

    def problems(self) -> tuple[str, ...]:
        """Return the list of standard violations for this symbol."""
        issues: list[str] = []
        if not self.has_docstring:
            issues.append(DOCSTRING_MISSING)
        if not self.has_full_annotations:
            issues.append(ANNOTATION_MISSING)
        return tuple(issues)


def _is_public(name: str) -> bool:
    return not name.startswith("_")


def _has_docstring(
    node: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    return ast.get_docstring(node, clean=False) is not None


def _params_annotated(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    positional = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    for index, arg in enumerate(positional):
        is_self = index == 0 and arg.arg in {"self", "cls"}
        if is_self:
            continue
        if arg.annotation is None:
            return False
    for extra in (node.args.vararg, node.args.kwarg):
        if extra is not None and extra.annotation is None:
            return False
    return True


def _function_symbol(
    node: ast.FunctionDef | ast.AsyncFunctionDef, file: str, qualname: str
) -> Symbol:
    return Symbol(
        file=file,
        line=node.lineno,
        qualname=qualname,
        has_docstring=_has_docstring(node),
        has_full_annotations=_params_annotated(node) and node.returns is not None,
    )


def collect_symbols(source: str, file: str) -> list[Symbol]:
    """Collect every public callable declared in the given module source."""
    tree = ast.parse(source, filename=file)
    symbols: list[Symbol] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_public(node.name):
            symbols.append(_function_symbol(node, file, node.name))
        elif isinstance(node, ast.ClassDef) and _is_public(node.name):
            if not _has_docstring(node):
                symbols.append(
                    Symbol(
                        file=file,
                        line=node.lineno,
                        qualname=node.name,
                        has_docstring=False,
                        has_full_annotations=True,
                    )
                )
            symbols.extend(
                _function_symbol(child, file, f"{node.name}.{child.name}")
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and _is_public(child.name)
            )

    return symbols


def iter_files() -> list[Path]:
    """Return every Python file in the A2 scope."""
    files: list[Path] = []
    for entry in A2_SCOPE:
        base = REPO_ROOT / entry
        if base.is_dir():
            files.extend(
                path for path in sorted(base.rglob("*.py")) if "__pycache__" not in path.parts
            )
    return files


def report() -> int:
    """Check the A2 public API and print a coverage summary."""
    symbols: list[Symbol] = []
    for path in iter_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        symbols.extend(collect_symbols(path.read_text(encoding="utf-8"), rel))

    if not symbols:
        print("OK: no A2 public callables in scope yet (nothing to check).")
        return 0

    with_docstring = [symbol for symbol in symbols if symbol.has_docstring]
    with_annotations = [symbol for symbol in symbols if symbol.has_full_annotations]
    doc_pct = 100.0 * len(with_docstring) / len(symbols)
    ann_pct = 100.0 * len(with_annotations) / len(symbols)

    violations = [symbol for symbol in symbols if symbol.problems()]
    print(f"A2 public callables : {len(symbols)}")
    print(f"docstring coverage  : {doc_pct:.1f}% ({len(with_docstring)}/{len(symbols)})")
    print(f"annotation coverage : {ann_pct:.1f}% ({len(with_annotations)}/{len(symbols)})")

    if violations:
        print("FAIL: public API standard is 100% / 100%:", file=sys.stderr)
        for symbol in violations:
            issues = ", ".join(symbol.problems())
            print(f"  {symbol.file}:{symbol.line} {symbol.qualname} — {issues}", file=sys.stderr)
        return 1

    print("OK: docstring and annotation coverage are both 100%.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the public API check from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
