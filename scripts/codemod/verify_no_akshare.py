#!/usr/bin/env python3
"""Zero-upstream-dependency assertion (quality spec §10, acceptance AC-16).

Scans the *runtime* packages with an AST (never text) for references to the
upstream packages ``akshare`` / ``openbb``:

* ``import akshare`` / ``from akshare.x import y``
* string constants that are pure dotted module paths (``"akshare.data"``)
* dynamic imports (``__import__`` / ``importlib.import_module``) whose string
  argument mentions the root name

Comments are invisible to the AST, and docstrings are explicitly skipped, per
the layered AST scope agreed in the acceptance document.

Until milestone A2 the legacy integration layer still imports ``akshare``. Those
occurrences are frozen in a baseline file and may only shrink; any *new*
occurrence fails the check. ``--update`` may only tighten the baseline unless
``--force-update`` is passed explicitly (scope changes need review).

Run the detector's own test suite with ``--self-test``.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN_ROOTS = ("akshare", "openbb")

# Runtime packages only. Development tooling, tests and docs are out of scope.
DEFAULT_TARGETS = (
    "opendata",
    "opendata_http",
    "opendata_fuyao",
    "opendata_providers",
    "opendata_client",
)

BASELINE_PATH = "docs/quality/zero-dep-baseline.json"
BASELINE_VERSION = 1

DYNAMIC_IMPORT_NAMES = frozenset({"import_module", "__import__"})

BASELINE_MISSING = (
    "baseline is missing (quality spec §0: missing evidence is a failure); "
    "generate it with --update"
)
SCOPE_CHANGED = "scan scope changed silently"


@dataclass(frozen=True, order=True)
class Finding:
    """Describe one forbidden reference.

    Attributes:
        file: Repo-relative POSIX path of the offending file.
        line: 1-based line number; informational only.
        module: Offending module name or string literal.
        kind: One of ``import``, ``string`` or ``dynamic``.
    """

    file: str
    line: int
    module: str
    kind: str

    def key(self) -> tuple[str, str, str]:
        """Return the line-insensitive identity used for baseline comparison."""
        return (self.file, self.module, self.kind)


@dataclass(frozen=True)
class Baseline:
    """Parsed contents of the frozen baseline file.

    Attributes:
        scope: Package names the baseline was generated for.
        entries: Frozen finding identities.
    """

    scope: tuple[str, ...]
    entries: tuple[tuple[str, str, str], ...]


def _is_docstring(node: ast.Constant, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    if not isinstance(parent, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    body = getattr(parent, "body", [])
    return bool(body) and body[0] is node


def _dotted_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _root_of(module: str) -> str | None:
    root = module.split(".", maxsplit=1)[0]
    return root if root in FORBIDDEN_ROOTS else None


def _looks_like_module_path(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    parts = stripped.split(".")
    if parts[0] not in FORBIDDEN_ROOTS:
        return False
    return all(part.isidentifier() for part in parts)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    mapping: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            mapping[child] = parent
    return mapping


def _is_dynamic_import_arg(node: ast.Constant, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    if not isinstance(parent, ast.Call):
        return False
    func = parent.func
    name = func.id if isinstance(func, ast.Name) else _dotted_name(func)
    if name is None:
        return False
    return name.rsplit(".", maxsplit=1)[-1] in DYNAMIC_IMPORT_NAMES


def scan_source(source: str, filename: str) -> list[Finding]:
    """Scan Python source text and return every forbidden reference."""
    tree = ast.parse(source, filename=filename)
    parents = _parents(tree)
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            findings.extend(
                Finding(filename, node.lineno, alias.name, "import")
                for alias in node.names
                if _root_of(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _root_of(module):
                findings.append(Finding(filename, node.lineno, module, "import"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _is_docstring(node, parents):
                continue
            if _is_dynamic_import_arg(node, parents):
                if any(root in node.value for root in FORBIDDEN_ROOTS):
                    findings.append(Finding(filename, node.lineno, node.value, "dynamic"))
            elif _looks_like_module_path(node.value):
                findings.append(Finding(filename, node.lineno, node.value, "string"))

    return findings


def iter_target_files(targets: tuple[str, ...]) -> list[Path]:
    """Return every Python file under the existing target packages."""
    files: list[Path] = []
    for target in targets:
        base = REPO_ROOT / target
        if base.is_dir():
            files.extend(
                path for path in sorted(base.rglob("*.py")) if "__pycache__" not in path.parts
            )
    return files


def collect(targets: tuple[str, ...]) -> list[Finding]:
    """Collect findings for every target package."""
    findings: list[Finding] = []
    for path in iter_target_files(targets):
        rel = path.relative_to(REPO_ROOT).as_posix()
        findings.extend(scan_source(path.read_text(encoding="utf-8"), rel))
    return findings


def _as_str_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _parse_baseline(raw: object) -> Baseline | None:
    if not isinstance(raw, dict):
        return None
    version = raw.get("version")
    if version != BASELINE_VERSION:
        return None
    scope = _as_str_tuple(raw.get("scope"))
    if scope is None:
        return None
    raw_findings = raw.get("findings")
    if not isinstance(raw_findings, list):
        return None
    entries: list[tuple[str, str, str]] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            return None
        file, module, kind = item.get("file"), item.get("module"), item.get("kind")
        if not (isinstance(file, str) and isinstance(module, str) and isinstance(kind, str)):
            return None
        entries.append((file, module, kind))
    return Baseline(scope=scope, entries=tuple(entries))


def load_baseline() -> Baseline | None:
    """Load and validate the frozen baseline, or return ``None`` if unusable."""
    path = REPO_ROOT / BASELINE_PATH
    if not path.is_file():
        return None
    return _parse_baseline(json.loads(path.read_text(encoding="utf-8")))


def count_by_identity(findings: list[Finding]) -> Counter[tuple[str, str, str]]:
    """Count findings by line-insensitive identity."""
    return Counter(finding.key() for finding in findings)


def check(targets: tuple[str, ...]) -> int:
    """Compare current findings against the frozen baseline."""
    current = collect(targets)
    baseline = load_baseline()
    if baseline is None:
        print(f"FAIL: {BASELINE_MISSING}", file=sys.stderr)
        return 1
    if baseline.scope != targets:
        print(
            f"FAIL: {SCOPE_CHANGED}.\n  baseline: {baseline.scope}\n  current:  {targets}",
            file=sys.stderr,
        )
        return 1

    expected = Counter(baseline.entries)
    actual = count_by_identity(current)
    regressions = actual - expected
    improvements = expected - actual

    if regressions:
        print(f"FAIL: {sum(regressions.values())} new upstream reference(s):", file=sys.stderr)
        for (file, module, kind), count in sorted(regressions.items()):
            print(f"  {file}: [{kind}] {module} x{count}", file=sys.stderr)
        return 1

    print(f"OK: no new upstream references (frozen baseline: {sum(expected.values())}).")
    if improvements:
        print(
            f"NOTE: {sum(improvements.values())} frozen reference(s) are gone — "
            "run --update to tighten the baseline."
        )
    return 0


def update(targets: tuple[str, ...], *, force: bool) -> int:
    """Rewrite the baseline, refusing to grow it unless ``force`` is set."""
    current = collect(targets)
    actual = count_by_identity(current)
    existing = load_baseline()
    if existing is not None and not force:
        grown = actual - Counter(existing.entries)
        if grown:
            print(
                f"FAIL: refusing to grow the baseline by {sum(grown.values())} entry(ies); "
                "pass --force-update only after review.",
                file=sys.stderr,
            )
            for (file, module, kind), count in sorted(grown.items()):
                print(f"  {file}: [{kind}] {module} x{count}", file=sys.stderr)
            return 1

    payload = {
        "version": BASELINE_VERSION,
        "scope": list(targets),
        "count": sum(actual.values()),
        "findings": [
            {
                "file": finding.file,
                "line": finding.line,
                "module": finding.module,
                "kind": finding.kind,
            }
            for finding in sorted(current)
        ],
    }
    path = REPO_ROOT / BASELINE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {BASELINE_PATH}: {sum(actual.values())} frozen reference(s).")
    return 0


# --------------------------------------------------------------------------- #
# Self test: the detector must catch violations and must not flag docstrings.
# --------------------------------------------------------------------------- #

_VIOLATION_SAMPLES: tuple[tuple[str, str], ...] = (
    ("import", "import akshare\n"),
    ("import", "import akshare as ak\n"),
    ("import", "from akshare.stock import cons\n"),
    ("import", "from opendata.data import http_client\nimport openbb\n"),
    ("string", 'MODULE = "akshare.data"\n'),
    ("string", 'MODULE = "openbb.providers"\n'),
    ("dynamic", '__import__("akshare.stock")\n'),
    ("dynamic", 'importlib.import_module("akshare_stock_helper")\n'),
)

_COMPLIANT_SAMPLES: tuple[str, ...] = (
    '"""Module docstring mentioning akshare and openbb is fine."""\n',
    "from opendata.data import registry\n",
    "# a comment mentioning akshare must be ignored\nVALUE = 1\n",
    'MESSAGE = "调用Akshare失败"\n',
)


def self_test() -> int:
    """Verify the detector catches violations and ignores docstrings and prose."""
    failures: list[str] = []

    for kind, sample in _VIOLATION_SAMPLES:
        hits = scan_source(sample, "<violation>")
        if not any(finding.kind == kind for finding in hits):
            failures.append(f"missed {kind}: {sample.strip()!r} (got {hits})")

    for sample in _COMPLIANT_SAMPLES:
        hits = scan_source(sample, "<compliant>")
        if hits:
            failures.append(f"false positive: {sample.strip()!r} -> {hits}")

    if failures:
        print("FAIL: scanner self-test:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(
        f"OK: scanner self-test passed "
        f"({len(_VIOLATION_SAMPLES)} violations detected, "
        f"{len(_COMPLIANT_SAMPLES)} compliant samples clean)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the zero-dependency assertion as a command-line tool."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="rewrite the frozen baseline")
    parser.add_argument(
        "--force-update",
        action="store_true",
        help="allow the baseline to grow (requires review)",
    )
    parser.add_argument("--self-test", action="store_true", help="run detector self-test")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.update or args.force_update:
        return update(DEFAULT_TARGETS, force=args.force_update)
    return check(DEFAULT_TARGETS)


if __name__ == "__main__":
    raise SystemExit(main())
