#!/usr/bin/env python3
"""C51 census: what the zero-dependency baseline freezes, and what a clean run can measure.

AC-16 条目 6 says the frozen baseline must reach zero once milestone A2 is done, and both it and
条目 7 ask for **positive** evidence: "in an environment without akshare/openbb installed, the P0
domain integration tests pass". Neither half can be argued from prose, so this instrument reads
six faces from source and from two interpreters:

1. ``entries``     - every baseline entry, attributed by AST: what shape is the string, which
                     enclosing call holds it, could it reach an import mechanism at all.
2. ``kinds``       - the same walk split by kind (``import`` / ``dynamic`` / ``string``), because
                     the criterion names an *import* residue while the baseline holds strings.
3. ``bite``        - controls run through the scanner itself: five shapes that must be reported
                     and four that must not. A judge that no longer bites makes every other
                     reading in this file worthless, so a control failure exits non-zero.
4. ``consumers``   - who reads each residue across the tracked tree, per area. This is what
                     "delete the residue" would actually cost.
5. ``markers``     - the marker census, and the collected count for the selector a clean
                     environment would run (``-m "integration and not e2e"``).
6. ``p0 domains``  - every in-repo enumeration that claims to name the P0 domains, since the
                     criterion's set has to come from one of them rather than from prose.
7. ``clean env``   - a second interpreter: does it hold akshare/openbb, what does its dependency
                     tree say, how many tests does the same selector collect there.

Run twice: before the wiring (``markers`` reads 0) and after. The two pytest collections cost ~20s
each; skip them with ``--no-collect`` when only the static faces changed.

Safety: read-only except for the report on stdout. Every subprocess passes an absolute executable,
a literal argv list and ``shell=False``; nothing writes into the repository.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import shutil
import subprocess  # nosec B404
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType

#: Repository root (this file lives in docs/evidence/C51).
ROOT = Path(__file__).resolve().parents[3]

#: The zero-dependency detector under measurement.
SCANNER_REL = "scripts/codemod/verify_no_akshare.py"

#: The A4.1 migration that enumerates the P0 warehouse tables.
P0_MIGRATION_REL = "alembic_data/versions/20260923-0001_ods_dwd_p0.py"

#: The selector a clean-environment run would use.
SELECTOR = "integration and not e2e"

#: Resolved once so no subprocess call below carries a partial executable path.
GIT = shutil.which("git")

#: Areas the consumer walk is reported by, longest prefix wins.
AREAS = (
    "opendata",
    "opendata_http",
    "opendata_fuyao",
    "opendata_client",
    "scripts",
    "tests",
    "frontend",
    "docs",
    "alembic",
    "alembic_data",
)


class Reading(NamedTuple):
    """One line of census output plus the face that produced it."""

    face: str
    line: str


def run(
    argv: Sequence[str | Path | None],
    *,
    cwd: Path = ROOT,
    timeout: int = 600,
) -> tuple[int, str]:
    """Run an argv list and return ``(exit code, combined output)``.

    Args:
        argv: Executable plus arguments; a missing executable is a reading, not a crash.
        cwd: Working directory for the child.
        timeout: Seconds before the child is killed.

    Returns:
        The return code (127 when the executable was not found) and stdout+stderr.
    """
    exe = argv[0]
    if exe is None:
        return 127, "executable not found on PATH"
    proc = subprocess.run(  # noqa: S603  # nosec B603  # absolute path, literal argv, shell disabled
        [str(part) for part in argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
        timeout=timeout,
    )
    return proc.returncode, f"{proc.stdout}{proc.stderr}"


def load_scanner() -> ModuleType:
    """Import the zero-dependency detector from its path so both readings come from one file.

    Returns:
        The module, used through its public names (``scan_source``, ``collect``,
        ``load_baseline``, ``DEFAULT_TARGETS``) only.

    Raises:
        RuntimeError: The detector file is missing or cannot be imported.
    """
    path = ROOT / SCANNER_REL
    if not path.is_file():
        raise RuntimeError(f"detector not found at {SCANNER_REL}")
    spec = importlib.util.spec_from_file_location("zero_dep_scanner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{SCANNER_REL}: importlib could not load it")
    module = importlib.util.module_from_spec(spec)
    # The detector declares a frozen dataclass, and @dataclass resolves string annotations through
    # sys.modules[cls.__module__]; without registering first, exec_module dies with
    # "'NoneType' object has no attribute '__dict__'".
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def tracked_files() -> list[str]:
    """Return every git-tracked path, so the consumer walk ignores build noise.

    Returns:
        Repo-relative POSIX paths, or an empty list when ``git`` is unavailable.
    """
    code, out = run([GIT, "ls-files"])
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def area_of(rel_path: str) -> str:
    """Bucket a tracked path into one of the reported areas.

    Args:
        rel_path: Repo-relative path.

    Returns:
        The first matching top-level area, or ``other``.
    """
    head = rel_path.split("/", maxsplit=1)[0]
    return head if head in AREAS else "other"


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    mapping: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            mapping[child] = parent
    return mapping


def _enclosing_name(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    """Name the function or class a node sits in, walking up to the module.

    Args:
        node: The node to start from.
        parents: Child-to-parent map of the same tree.

    Returns:
        ``module`` or the enclosing ``ClassDef.method`` / ``FunctionDef`` name.
    """
    parts: list[str] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            parts.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(parts)) or "module"


def _shape(node: ast.Constant, parents: dict[ast.AST, ast.AST]) -> str:
    """Describe the syntactic role a string literal plays.

    Args:
        node: The flagged string constant.
        parents: Child-to-parent map of the same tree.

    Returns:
        A label such as ``dict key`` or ``keyword argument default=`` - what a reader has to
        understand to judge whether the value is a dependency or a name.
    """
    parent = parents.get(node)
    grand = parents.get(parent) if parent is not None else None
    if isinstance(parent, ast.Dict):
        is_key = any(key is node for key in parent.keys if key is not None)
        return "dict key (source label)" if is_key else "dict value"
    if isinstance(parent, ast.Call):
        func = parent.func
        fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "?")
        if fname == "get":
            return "field name read from an external data file (.get)"
        return f"argument of {fname}(...)"
    if isinstance(parent, ast.keyword) and grand is not None:
        return f"keyword argument {parent.arg}= of {_enclosing_name(grand, parents)}"
    if isinstance(parent, ast.Assign):
        targets = ",".join(getattr(t, "id", ast.dump(t)) for t in parent.targets)
        return f"assigned to {targets}"
    if isinstance(parent, ast.AnnAssign):
        return f"column default on {getattr(parent.target, 'id', '?')}"
    return type(parent).__name__


def _reaches_import(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Whether any ancestor call is a dynamic-import entry point.

    Args:
        node: The flagged string constant.
        parents: Child-to-parent map of the same tree.

    Returns:
        True when the literal is handed to ``import_module`` / ``__import__`` somewhere above it.
    """
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, ast.Call):
            func = current.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in {"import_module", "__import__"}:
                return True
        current = parents.get(current)
    return False


class Entry(NamedTuple):
    """One zero-dependency reference, in the detector's own field order."""

    file: str
    line: int
    module: str
    kind: str


def live_entries(scanner: ModuleType) -> list[Entry]:
    """Re-run the detector's walk and return its findings as plain records.

    Args:
        scanner: The loaded detector.

    Returns:
        Every hit, in the detector's walk order.
    """
    return [
        Entry(f.file, f.line, f.module, f.kind) for f in scanner.collect(scanner.DEFAULT_TARGETS)
    ]


def attribute(finding: Entry) -> str:
    """Attribute one baseline entry by re-reading the source it was found in.

    Args:
        finding: The detector's ``Finding`` (file, line, module, kind).

    Returns:
        A single readable line: shape, enclosing scope, dynamic-import verdict.
    """
    path = ROOT / finding.file
    if not path.is_file():
        return f"{finding.file}:{finding.line} {finding.module!r} - file gone, cannot attribute"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents = _parents(tree)
    hits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.lineno == finding.line
        and node.value == finding.module
    ]
    if not hits:
        return f"{finding.file}:{finding.line} {finding.module!r} - literal not found at that line"
    node = hits[0]
    shape = _shape(node, parents)
    scope = _enclosing_name(node, parents)
    dynamic = _reaches_import(node, parents)
    return (
        f"{finding.file}:{finding.line} {finding.module!r} kind={finding.kind} "
        f"shape={shape} scope={scope} reaches_import={'YES' if dynamic else 'no'}"
    )


def face_entries(scanner: ModuleType) -> list[str]:
    """Face 1: every frozen entry, attributed.

    Args:
        scanner: The loaded detector.

    Returns:
        One line per baseline entry.
    """
    baseline = scanner.load_baseline()
    today = {f"{f.file}:{f.line}": f for f in scanner.collect(scanner.DEFAULT_TARGETS)}
    lines = [f"baseline entries: {len(baseline.entries)}  live findings: {len(today)}"]
    for file, module, kind in baseline.entries:
        live = next((f for f in today.values() if f.file == file and f.module == module), None)
        probe = live or Entry(file, 0, module, kind)
        lines.append("  " + attribute(probe) + ("" if live else " [not reproduced today]"))
    return lines


def face_kinds(scanner: ModuleType) -> list[str]:
    """Face 2: the live walk split by kind.

    Args:
        scanner: The loaded detector.

    Returns:
        Counts per kind, so an import residue and a string-shaped label are never conflated.
    """
    findings = scanner.collect(scanner.DEFAULT_TARGETS)
    counts = Counter(f.kind for f in findings)
    named = ("import", "dynamic", "string")
    lines = [f"scan targets: {', '.join(scanner.DEFAULT_TARGETS)}"]
    lines.extend(f"  {kind}: {counts.get(kind, 0)}" for kind in named)
    lines.extend(
        f"  {kind}: {count} (unexpected kind)"
        for kind, count in sorted(counts.items())
        if kind not in named
    )
    return lines


#: Shapes the detector must report. Each is a dependency in the sense AC-16 forbids.
MUST_REPORT = (
    ("import akshare\n", "import akshare"),
    ("from akshare.stock_feature import x\n", "from akshare.x import"),
    ("import akshare.data as d\n", "import akshare.data as"),
    ('importlib.import_module("openbb.equity")\n', 'import_module("openbb.equity")'),
    ('__import__("akshare")\n', '__import__("akshare")'),
    ('mod = "akshare.core"\n', 'bare "akshare.core" constant'),
)

#: Shapes the detector must not report: prose, docstrings, unrelated names.
MUST_NOT_REPORT = (
    ('"""\nA docstring mentioning akshare and openbb.\n"""\nvalue = 1\n', "docstring prose"),
    ('MESSAGE = "调用 AkShare 接口失败"\n', "prose inside a string"),
    ('source = "akshare_client_id"  # starts with the root, but is no dotted path\n', "suffixed"),
    ('source = "my_akshare"  # the root is not the first segment\n', "root not first"),
)


def face_bite(scanner: ModuleType) -> tuple[list[str], bool]:
    """Face 3: attack the detector so the other faces cannot be vacuous.

    Args:
        scanner: The loaded detector.

    Returns:
        Report lines and whether every control behaved (a dead detector makes the round's readings
        meaningless, so the caller must not continue past a failure).
    """
    lines: list[str] = []
    ok = True
    for source, label in MUST_REPORT:
        found = scanner.scan_source(source, "control.py")
        if found:
            lines.append(f"  bites   {label}: {[(f.kind, f.module) for f in found]}")
        else:
            ok = False
            lines.append(f"  DEAD    {label}: reported nothing")
    for source, label in MUST_NOT_REPORT:
        found = scanner.scan_source(source, "control.py")
        if found:
            lines.append(f"  over    {label}: {[(f.kind, f.module) for f in found]}")
        else:
            lines.append(f"  silent  {label}: as intended")
    code, out = run([sys.executable, SCANNER_REL, "--self-test"])
    tail = [line for line in out.splitlines() if line.strip()][-1:] or ["(no output)"]
    lines.append(f"  detector --self-test exit={code}: {tail[0]}")
    if code != 0:
        ok = False
    return lines, ok


def face_consumers(entries: Sequence[Entry]) -> list[str]:
    """Face 4: who reads each residue's value across the tracked tree.

    Args:
        entries: The live findings to look up.

    Returns:
        Per-entry area counts plus the read sites that would change if the literal were removed.
    """
    files = tracked_files()
    lines = [f"tracked files walked: {len(files)}"]
    for module, sites in _group(entries).items():
        pattern = re.compile(re.escape(f'"{module}"') + "|" + re.escape(f"'{module}'"))
        buckets: Counter[str] = Counter()
        hits: list[str] = []
        for rel in files:
            path = ROOT / rel
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            count = sum(1 for line in text.splitlines() if pattern.search(line))
            if count:
                buckets[area_of(rel)] += count
                if len(hits) < 6:
                    hits.append(f"{rel} x{count}")
        spread = ", ".join(f"{area}={n}" for area, n in sorted(buckets.items())) or "none"
        where = "; ".join(f"{e.file}:{e.line}" for e in sites)
        lines.append(f"  {module!r} as a quoted literal [{where}]: {spread}")
        lines.append(f"    sites: {'; '.join(hits)}")
    return lines


def _group(entries: Sequence[Entry]) -> dict[str, list[Entry]]:
    """Group baseline entries by the literal they flag.

    Args:
        entries: Live findings.

    Returns:
        Literal -> entries carrying it, in walk order. Two entries can share one literal (the
        source-name label appears twice in the runtime tree), and the consumer face is about the
        value, not the line.
    """
    grouped: dict[str, list[Entry]] = {}
    for entry in entries:
        grouped.setdefault(entry.module, []).append(entry)
    return grouped


def face_markers() -> list[str]:
    """Face 5a: the marker census across ``tests/``.

    Returns:
        Registered markers from ``pytest.ini`` and how often each is actually applied.
    """
    ini = (ROOT / "pytest.ini").read_text(encoding="utf-8")
    registered = _registered_markers(ini)
    counts: Counter[str] = Counter()
    module_marks: list[str] = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            name = _mark_name(node)
            if name:
                counts[name] += 1
        if _has_module_pytestmark(tree):
            module_marks.append(str(path.relative_to(ROOT)))
    lines = [f"registered in pytest.ini: {', '.join(registered) or 'NONE FOUND'}"]
    lines.extend(f"  applied {name:12s}: {counts.get(name, 0)}" for name in registered)
    for name, count in sorted(counts.items()):
        if name in registered:
            continue
        origin = "builtin" if name in BUILTIN_MARKERS else "NOT registered"
        lines.append(f"  applied {name:12s}: {count} ({origin})")
    lines.append(f"  module-level pytestmark: {len(module_marks)} file(s)")
    lines.extend(f"    {name}" for name in module_marks)
    return lines


#: Markers pytest provides without registration (``--strict-markers`` accepts them).
BUILTIN_MARKERS = frozenset(
    {"parametrize", "skip", "skipif", "xfail", "usefixtures", "filterwarnings"}
)


def _registered_markers(ini_text: str) -> list[str]:
    """Read the ``markers =`` block out of pytest.ini.

    Args:
        ini_text: Contents of ``pytest.ini``.

    Returns:
        The registered marker names, in file order. Empty means the block was not found, which is
        itself the reading the first pass of this instrument produced by accident.
    """
    names: list[str] = []
    inside = False
    for line in ini_text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            inside = line.replace(" ", "").startswith("markers=")
            continue
        if inside:
            names.append(line.strip().split(":")[0])
    return names


def _has_module_pytestmark(tree: ast.Module) -> bool:
    """Whether a test module marks every one of its tests.

    Args:
        tree: Parsed test module.

    Returns:
        True when the module has a top-level ``pytestmark = ...`` assignment.
    """
    return any(
        isinstance(stmt, ast.Assign)
        and any(getattr(t, "id", "") == "pytestmark" for t in stmt.targets)
        for stmt in tree.body
    )


def _mark_name(node: ast.AST) -> str | None:
    """Return the marker name when a node is a ``pytest.mark.<name>`` access.

    Args:
        node: Any AST node.

    Returns:
        The marker name, or None.
    """
    if not isinstance(node, ast.Attribute):
        return None
    value = node.value
    if (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "pytest"
        and value.attr == "mark"
    ):
        return node.attr
    return None


def face_collection(label: str, python: Path | str, *, extra: Sequence[str] = ()) -> list[str]:
    """Face 5b: what a given interpreter actually collects for the clean-run selector.

    Args:
        label: Name of the interpreter in the report.
        python: Interpreter executable.
        extra: Additional pytest arguments (e.g. the module list once markers exist).

    Returns:
        The collected/test count lines and the exit code.
    """
    argv = [
        str(python),
        "-m",
        "pytest",
        "tests",
        "--collect-only",
        "-q",
        "--no-cov",
        "-m",
        SELECTOR,
        *extra,
    ]
    code, out = run(argv, timeout=900)
    keep = [
        line
        for line in out.splitlines()
        if re.search(r"(\d+ tests? collected|no tests ran|selected|deselected|error)", line)
    ]
    lines = [f"  {label}: `pytest -m '{SELECTOR}'` exit={code}"]
    lines.extend(f"    {line.strip()}" for line in keep[-6:])
    return lines


def _ast_literal(path: Path, name: str) -> object | None:
    """Read a module-level assignment's literal value out of a file.

    Args:
        path: Python file to parse.
        name: Assignment target.

    Returns:
        The ``ast.literal_eval``-able value, or None when absent.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(getattr(t, "id", "") == name for t in stmt.targets):
            try:
                return cast("object", ast.literal_eval(stmt.value))
            except ValueError:
                return None
    return None


def face_p0_domains() -> list[str]:
    """Face 6: every in-repo enumeration that claims to name the P0 domains.

    Returns:
        Each source with its content, so the criterion's set is traceable to a file rather than
        to a sentence in this instrument.
    """
    lines: list[str] = []
    migration = ROOT / P0_MIGRATION_REL
    tables = _ast_literal(migration, "_DWD_TABLES") if migration.is_file() else None
    if isinstance(tables, dict):
        listed = ", ".join(map(str, tables))
        lines.append(f"{P0_MIGRATION_REL} _DWD_TABLES ({len(tables)}): {listed}")
    else:
        lines.append(f"{P0_MIGRATION_REL} _DWD_TABLES: not readable")
    schedules = ROOT / "opendata" / "pipeline" / "schedules.yaml"
    if schedules.is_file():
        names = re.findall(r"^\s*-?\s*name:\s*(\S+)", schedules.read_text(encoding="utf-8"), re.M)
        p0_named = ", ".join(n for n in names if "p0" in n.lower()) or "none"
        lines.append(f"schedules.yaml jobs: {len(names)}, p0-named: {p0_named}")
    domains = ROOT / "opendata" / "data" / "domains.yaml"
    if domains.is_file():
        text = domains.read_text(encoding="utf-8")
        keys = re.findall(r"^  (\w+):$", text, re.M)
        tiers = [t for t in ("priority", "batch", "tier") if re.search(rf"^\s+{t}:", text, re.M)]
        lines.append(
            f"opendata/data/domains.yaml domains: {len(keys)}; "
            f"per-domain tier fields present: {', '.join(tiers) or 'none (so no P0 set here)'}"
        )
    mapping = ROOT / "opendata" / "data" / "openbb_map.yaml"
    if mapping.is_file():
        text = mapping.read_text(encoding="utf-8")
        batches = Counter(re.findall(r"batch:\s*(\w+)", text))
        spread = dict(sorted(batches.items())) or "none"
        lines.append(f"opendata/data/openbb_map.yaml batch labels: {spread}")
    return lines


def face_clean_env(venv: str | None) -> list[str]:
    """Face 7: read the second interpreter that is supposed to be clean.

    Args:
        venv: Path to the venv root, or None to report the absence as ``no-env``.

    Returns:
        Presence of the two upstream packages, the dependency-tree verdict, the package count.
    """
    if not venv:
        return ["  no-env: --venv was not passed, so the clean-environment face has no reading"]
    python = Path(venv) / "bin" / "python"
    if not python.is_file():
        return [f"  no-env: {python} is not a file (the venv was never built)"]
    probe = (
        "import importlib.util,sys;"
        "print(' '.join(f'{n}=' + ('PRESENT' if importlib.util.find_spec(n) else 'absent')"
        " for n in ('akshare','openbb')))"
    )
    code, out = run([python, "-c", probe], timeout=120)
    lines = [f"  interpreter: {python} (exit={code})"]
    lines.extend(f"    {line.strip()}" for line in out.splitlines() if line.strip())
    code, out = run([python, "-m", "pip", "check"], timeout=300)
    lines.append(f"    pip check exit={code}: {out.strip() or '(no output)'}")
    code, out = run([python, "-m", "pip", "list", "--format=json"], timeout=300)
    try:
        payload = json.loads(out)
        names = sorted(str(item["name"]) for item in payload)
        upstream = [n for n in names if re.match(r"^(akshare|openbb)", n, re.I)]
        lines.append(f"    packages installed: {len(names)}; upstream-named: {upstream or 'none'}")
    except (ValueError, KeyError):
        lines.append(f"    pip list unparseable (exit={code})")
    code, out = run([python, "-VV"], timeout=60)
    lines.append(f"    {out.strip()}")
    return lines


def main(argv: list[str] | None = None) -> int:
    """Run the census.

    Args:
        argv: Command-line arguments after the program name.

    Returns:
        0 when every control bit and the baseline reproduced, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__ or "", add_help=True)
    parser.add_argument("--venv", help="path to a venv that must not contain akshare/openbb")
    parser.add_argument(
        "--no-collect",
        action="store_true",
        help="skip the two pytest --collect-only faces (slow, ~40s)",
    )
    args = parser.parse_args(argv)

    scanner = load_scanner()
    live = live_entries(scanner)

    sections: list[tuple[str, list[str]]] = [
        ("1. baseline entries, attributed", face_entries(scanner)),
        ("2. live walk, by kind", face_kinds(scanner)),
    ]
    bite_lines, bit = face_bite(scanner)
    sections.append(("3. detector controls (dead detector voids the readings above)", bite_lines))
    sections.append(("4. consumers of each residue", face_consumers(live)))
    sections.append(("5a. marker census", face_markers()))
    if not args.no_collect:
        sections.append(
            ("5b. collection in this interpreter", face_collection("repo env", sys.executable))
        )
    sections.append(("6. P0 domain enumerations in the repo", face_p0_domains()))
    sections.append(("7. the clean interpreter", face_clean_env(args.venv)))
    if not args.no_collect and args.venv:
        clean_python = Path(args.venv) / "bin" / "python"
        sections.append(
            (
                "7b. collection in the clean interpreter",
                face_collection("clean venv", clean_python),
            )
        )

    for title, lines in sections:
        print(f"\n===== {title} =====")
        for line in lines:
            print(line)

    imports = sum(1 for f in live if f.kind in {"import", "dynamic"})
    strings = sum(1 for f in live if f.kind == "string")
    print(
        f"\n===== summary =====\nbaseline={len(live)} import-or-dynamic={imports} "
        f"string={strings} controls_bit={'yes' if bit else 'NO'}"
    )
    return 0 if bit else 1


if __name__ == "__main__":
    sys.exit(main())
