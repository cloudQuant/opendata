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

Scan surface
------------
A reading is only comparable with another reading if both came from the same walk.
``opendata_providers`` sat in the target list for rounds without existing, and the
walker skipped missing directories silently, so a "runtime package" contributed zero
files to a check that reported no regressions. The baseline therefore now freezes the
surface as well as the findings: the package set, the per-package Python file count,
the scanner version and the interpreters it was verified under. Divergence in either
direction fails -- a walk that sees fewer files than the archive records is blindness, a
census left stale by added files makes the archived number fiction -- and so do renamed
packages and unreviewed interpreters, until someone re-freezes deliberately.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN_ROOTS = ("akshare", "openbb")

# Runtime packages only. Development tooling, tests and docs are out of scope.
# `opendata_providers` used to be listed here and does not exist: it was a name in a
# list, not a directory in the tree, and nothing noticed (see the module docstring).
DEFAULT_TARGETS = (
    "opendata",
    "opendata_http",
    "opendata_fuyao",
    "opendata_client",
)

BASELINE_PATH = "docs/quality/zero-dep-baseline.json"
BASELINE_VERSION = 2

# Bump when the scan logic changes, so an old reading can never be compared with a new
# one by accident. Interpreters the baseline has been verified under: CI runs 3.11,
# development runs 3.13; anything else is an unreviewed parser surface.
SCANNER_VERSION = 2
PYTHON_MINORS = ("3.11", "3.13")

DYNAMIC_IMPORT_NAMES = frozenset({"import_module", "__import__"})

BASELINE_MISSING = (
    "baseline is missing (quality spec §0: missing evidence is a failure); "
    "generate it with --update"
)
SCOPE_CHANGED = "scan scope changed silently"
SURFACE_SHRANK = "scan surface shrank"
SURFACE_STALE = "archived file census no longer describes the tree"


class ScanSurfaceError(RuntimeError):
    """A declared scan target is not a walkable package."""


class BaselineError(RuntimeError):
    """The frozen baseline is absent or describes a different device."""


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
        files: Per-package Python file count the freeze walked.
        python_minors: Interpreters the freeze was verified under.
    """

    scope: tuple[str, ...]
    entries: tuple[tuple[str, str, str], ...]
    files: dict[str, int]
    python_minors: tuple[str, ...]


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


def target_files(target: str) -> list[Path]:
    """Every Python file of one runtime package.

    A declared target that is absent or empty is an error rather than a skip: the
    scanner once carried ``opendata_providers`` for rounds while the directory did not
    exist, and "0 files walked" looked identical to "nothing to report".
    """
    base = REPO_ROOT / target
    if not base.is_dir():
        raise ScanSurfaceError(f"{target}: declared scan target is not a directory")
    files = [path for path in sorted(base.rglob("*.py")) if "__pycache__" not in path.parts]
    if not files:
        raise ScanSurfaceError(f"{target}: scan target holds no Python file")
    return files


def iter_target_files(targets: tuple[str, ...]) -> list[Path]:
    """Return every Python file under the target packages."""
    return [path for target in targets for path in target_files(target)]


def file_census(targets: tuple[str, ...]) -> dict[str, int]:
    """Per-package file counts, i.e. how much of the tree this reading actually saw."""
    return {target: len(target_files(target)) for target in targets}


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


def _parse_baseline(raw: object) -> Baseline:
    """Validate one decoded baseline document, or say precisely why it is unusable.

    Every rejection names the field: "baseline is missing" and "baseline was frozen by
    a different scanner" call for opposite actions, and one message for both invites
    whoever is red to just re-run --update.
    """
    if not isinstance(raw, dict):
        raise BaselineError("baseline is not a JSON object")
    version = raw.get("version")
    if version != BASELINE_VERSION:
        raise BaselineError(
            f"baseline version is {version!r}, this scanner writes {BASELINE_VERSION!r}; "
            "re-freeze with --update only after reviewing the diff"
        )
    scanner = raw.get("scanner_version")
    if scanner != SCANNER_VERSION:
        raise BaselineError(f"baseline was frozen by scanner {scanner!r}, not {SCANNER_VERSION!r}")
    scope = _as_str_tuple(raw.get("scope"))
    if scope is None:
        raise BaselineError("`scope` must be a list of package names")
    census = raw.get("files")
    if not isinstance(census, dict) or not all(
        isinstance(key, str) and isinstance(value, int) for key, value in census.items()
    ):
        raise BaselineError("`files` must map each package to its walked file count")
    python_minors = _as_str_tuple(raw.get("python_minors"))
    if python_minors is None:
        raise BaselineError("`python_minors` must list the verified interpreters")
    raw_findings = raw.get("findings")
    if not isinstance(raw_findings, list):
        raise BaselineError("`findings` must be a list")
    entries: list[tuple[str, str, str]] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            raise BaselineError("every finding must be an object")
        file, module, kind = item.get("file"), item.get("module"), item.get("kind")
        if not (isinstance(file, str) and isinstance(module, str) and isinstance(kind, str)):
            raise BaselineError("finding needs string file, module and kind")
        entries.append((file, module, kind))
    return Baseline(
        scope=scope,
        entries=tuple(entries),
        files=dict(census),
        python_minors=python_minors,
    )


def load_baseline() -> Baseline:
    """Read the frozen baseline.

    Raises:
        BaselineError: The file is absent or describes a different device.
    """
    path = REPO_ROOT / BASELINE_PATH
    if not path.is_file():
        raise BaselineError(BASELINE_MISSING)
    return _parse_baseline(json.loads(path.read_text(encoding="utf-8")))


def count_by_identity(findings: list[Finding]) -> Counter[tuple[str, str, str]]:
    """Count findings by line-insensitive identity."""
    return Counter(finding.key() for finding in findings)


def surface_problems(baseline: Baseline, census: dict[str, int]) -> list[str]:
    """Ways in which today's walk is not the walk the baseline describes."""
    problems: list[str] = []
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    if running not in PYTHON_MINORS:
        problems.append(f"interpreter {running} is not among the reviewed {list(PYTHON_MINORS)}")
    if sorted(baseline.python_minors) != sorted(PYTHON_MINORS):
        problems.append(
            f"baseline froze {list(baseline.python_minors)} but the code allows "
            f"{list(PYTHON_MINORS)}"
        )
    for target, expected in sorted(baseline.files.items()):
        actual = census.get(target)
        if actual is None:
            problems.append(f"{target}: baseline walked {expected} file(s), today none")
        elif actual < expected:
            problems.append(f"{SURFACE_SHRANK}: {target} {expected} -> {actual} file(s)")
        elif actual > expected:
            problems.append(
                f"{SURFACE_STALE}: {target} archived {expected} but walks {actual} file(s)"
            )
    problems.extend(
        f"{target}: on the scan surface but missing from the baseline"
        for target in sorted(set(census) - set(baseline.files))
    )
    return problems


def check(targets: tuple[str, ...]) -> int:
    """Compare the current walk and findings against the frozen baseline."""
    try:
        baseline = load_baseline()
    except (BaselineError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    try:
        census = file_census(targets)
    except ScanSurfaceError as exc:
        print(f"FAIL: scan surface is broken: {exc}", file=sys.stderr)
        return 1
    if baseline.scope != targets:
        print(
            f"FAIL: {SCOPE_CHANGED}.\n  baseline: {baseline.scope}\n  current:  {targets}",
            file=sys.stderr,
        )
        return 1

    problems = surface_problems(baseline, census)
    if problems:
        print("FAIL: the scan surface moved:", file=sys.stderr)
        for line in problems:
            print(f"  - {line}", file=sys.stderr)
        print(
            "  Re-freeze with --update only when the move is intended; a package that "
            "lost files is a package that stopped being looked at, and a census that no "
            "longer matches the tree is a number nobody can read as evidence.",
            file=sys.stderr,
        )
        return 1

    expected = Counter(baseline.entries)
    actual = count_by_identity(collect(targets))
    regressions = actual - expected
    improvements = expected - actual

    if regressions:
        print(f"FAIL: {sum(regressions.values())} new upstream reference(s):", file=sys.stderr)
        for (file, module, kind), count in sorted(regressions.items()):
            print(f"  {file}: [{kind}] {module} x{count}", file=sys.stderr)
        return 1

    print(
        f"OK: {sum(census.values())} file(s) walked "
        f"({', '.join(f'{k}={v}' for k, v in sorted(census.items()))}), "
        f"python {sys.version_info.major}.{sys.version_info.minor}, "
        f"scanner {SCANNER_VERSION}; "
        f"no new upstream references (frozen baseline: {sum(expected.values())})."
    )
    if improvements:
        print(
            f"NOTE: {sum(improvements.values())} frozen reference(s) are gone — "
            "run --update to tighten the baseline."
        )
    return 0


def update(targets: tuple[str, ...], *, force: bool) -> int:
    """Rewrite the baseline, refusing to grow it unless ``force`` is set."""
    try:
        census = file_census(targets)
    except ScanSurfaceError as exc:
        print(f"FAIL: refusing to freeze a broken scan surface: {exc}", file=sys.stderr)
        return 1
    try:
        existing = load_baseline()
    except BaselineError as exc:
        path = REPO_ROOT / BASELINE_PATH
        if not path.is_file() and str(exc) == BASELINE_MISSING:
            existing = None
        elif not force:
            print(
                f"FAIL: the existing baseline is unusable ({exc}) and --update would "
                "replace it silently; pass --force-update once that is what you reviewed.",
                file=sys.stderr,
            )
            return 1
        else:
            existing = None
    current = collect(targets)
    actual = count_by_identity(current)
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
        "scanner_version": SCANNER_VERSION,
        "python_minors": list(PYTHON_MINORS),
        "scope": list(targets),
        "files": dict(sorted(census.items())),
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
    print(
        f"Wrote {BASELINE_PATH}: {sum(actual.values())} frozen reference(s) across "
        f"{sum(census.values())} file(s)."
    )
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


def _surface_guards() -> list[str]:
    """Attack the scan-surface guards, so they cannot pass by being unreachable."""
    failures: list[str] = []
    frozen = Baseline(
        scope=DEFAULT_TARGETS,
        entries=(("opendata/x.py", "akshare", "import"),),
        files=dict.fromkeys(DEFAULT_TARGETS, 10),
        python_minors=PYTHON_MINORS,
    )

    if surface_problems(frozen, dict.fromkeys(DEFAULT_TARGETS, 10)):
        failures.append("a matching surface was reported as moved")
    shrunk = surface_problems(frozen, {**frozen.files, "opendata_http": 9})
    if not any(SURFACE_SHRANK in line for line in shrunk):
        failures.append(f"a one-file shrink was not caught: {shrunk}")
    stale = surface_problems(frozen, {**frozen.files, "opendata_http": 11})
    if not any(SURFACE_STALE in line for line in stale):
        failures.append(f"a stale census (10 recorded, 11 walked) was not caught: {stale}")
    if not surface_problems(frozen, {k: v for k, v in frozen.files.items() if k != "opendata"}):
        failures.append("a scan target that vanished from the census was not caught")

    # A missing package must be an error, not a silent zero.
    try:
        target_files("opendata_providers")
        failures.append("opendata_providers resolved as a package; update this guard")
    except ScanSurfaceError as exc:
        if "not a directory" not in str(exc):
            failures.append(f"wrong reason for the missing package: {exc}")

    empty = Path(tempfile.mkdtemp(prefix="zero-dep-empty-"))
    (empty / "pkg").mkdir()
    previous_root = globals()["REPO_ROOT"]
    try:
        globals()["REPO_ROOT"] = empty
        try:
            target_files("pkg")
            failures.append("an empty target directory was accepted")
        except ScanSurfaceError as exc:
            if "no Python file" not in str(exc):
                failures.append(f"wrong reason for the empty target: {exc}")
    finally:
        globals()["REPO_ROOT"] = previous_root
        (empty / "pkg").rmdir()
        empty.rmdir()

    v1 = {"version": 1, "scope": list(DEFAULT_TARGETS), "findings": []}
    try:
        _parse_baseline(v1)
        failures.append("a v1 baseline parsed as v2")
    except BaselineError as exc:
        if "version" not in str(exc):
            failures.append(f"v1 rejection must name the version, got: {exc}")
    no_census = {
        **v1,
        "version": BASELINE_VERSION,
        "scanner_version": SCANNER_VERSION,
        "python_minors": list(PYTHON_MINORS),
    }
    try:
        _parse_baseline(no_census)
        failures.append("a baseline without a file census parsed")
    except BaselineError as exc:
        if "files" not in str(exc):
            failures.append(f"census rejection must name `files`, got: {exc}")
    return failures


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

    failures.extend(_surface_guards())

    if failures:
        print("FAIL: scanner self-test:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(
        f"OK: scanner self-test passed "
        f"({len(_VIOLATION_SAMPLES)} violations detected, "
        f"{len(_COMPLIANT_SAMPLES)} compliant samples clean, "
        "scan-surface guards bite in both directions)."
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
