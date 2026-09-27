#!/usr/bin/env python3
"""Recompute acceptance criteria one item at a time, with a counterfact per face.

Why this file exists
--------------------
``docs/迭代计划/迭代1-重构数据中台/验收文档.md`` keeps two books: the §10 table, which says
``完成`` per acceptance criterion, and the ``- [ ]`` checklist inside each ``### AC-N`` block,
which is the item-level face of that same claim. ``acceptance_ledger_check.py`` reconciles the
two books; it cannot say whether one criterion is *true*. Until round C39, 119 of the 130 items
read ``unreviewed`` -- which means "nobody looked", and is not the statement "passed".

This is the instrument that looks. One probe per item, keyed the way the ledger keys it
(``AC-N|NN``), each split into four parts:

* ``measure`` -- reads the repository (file contents, ``git``, the tool or test the item names)
  and returns string facts. It decides nothing.
* ``judge`` -- a pure function over those facts that returns ``proven`` or ``gap``.
* ``repair`` -- the reading the judge has to accept as ``proven``, which states the item's pass
  condition separately from whatever the tree happens to look like today. For a gap it doubles
  as "what would close it", and it is what the counterfacts are applied to.
* ``breaks`` -- fact mutations that must each turn that clean reading back into a gap. A judge
  no mutation can move is a decoration, and a judge nothing can satisfy is a permanent red light;
  ``--self-test`` fails on either. C27 is the reason: that round found a criterion that could
  not fail.

Two deliberate limits. It never writes the ledger or ticks a box -- a ``proven`` verdict here is
an *input* to a ledger entry, which also has to name tracked evidence files. And it treats each
criterion's wording as a premise: ``expects`` is a substring that must still appear in the
document, so a re-worded item strands its probe loudly (the ledger keys are digests of that same
text, which the ledger check already enforces) instead of quietly measuring something else.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Final

import tomllib
import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_REL: Final = "docs/迭代计划/迭代1-重构数据中台/验收文档.md"
LEDGER_REL: Final = "docs/quality/acceptance-item-ledger.json"

PROVEN: Final = "proven"
GAP: Final = "gap"

MAIN_DB = "opendata"
WAREHOUSE_DB = "opendata_data"

#: Machine surfaces AC-1|04 names as having been renamed. No historical exemption applies here:
#: a compose file or a hook regex that still says ``akshare_*`` is a live identifier.
AC1_04_MACHINE_SURFACES: Final = (
    "docker-compose.yml",
    ".env.example",
    "init.sql",
    "opendata/core/config.py",
    "Makefile",
    ".pre-commit-config.yaml",
    "Dockerfile",
)

#: Root documents AC-1|04 names. AC-1|03 grants these files 沿革引用, so a hit is only a finding
#: when the line does not mark itself as history -- an imperative that still *uses* the old
#: container name in a root document must not pass.
AC1_04_ROOT_DOCS: Final = ("README.md", "CODE_QUALITY.md", "ARCHITECTURE.md", "QUICKSTART.md")

#: Phrases that mark a line as talking *about* the predecessor rather than *using* it. Deliberately
#: multi-character: a bare 由 or 原 would forgive any instruction that happened to contain them.
PROVENANCE_MARKER: Final = (
    r"继承|遗产|沿革|适配自|重构而来|拷贝|原名|前身|predecessor|renamed|migrat"
)

#: What a hand-copied tree tends to leave behind, none of which belongs in git.
RESIDUE_PATTERNS: Final = (
    r"(^|/)\.DS_Store$",
    r"\.pid$",
    r"^\.idea/",
    r"__pycache__/",
    r"\.egg-info/",
    r"^app/",
    r"^akshare/",
    r"(^|/)[^/]*[ (](copy|副本)[ )]",
    r"(^|/)\.env$",
    r"\.pypirc$",
    r"\.pem$",
    r"^frontend/dist/",
    r"^node_modules/",
)

#: The six P0 models AC-2|01 names.
P0_MODELS: Final = (
    "Bar",
    "AdjustFactor",
    "CorporateAction",
    "FinancialStatement",
    "IndexConstituent",
    "FuturesFundamentals",
)

#: Where the "full-market backfill and incremental window" logic actually lives.
BACKFILL_MODULES: Final = (
    "opendata/pipeline/runner.py",
    "opendata/pipeline/jobs.py",
    "opendata/pipeline/templates.py",
    "opendata/pipeline/scheduling.py",
    "opendata/pipeline/partitions.py",
    "opendata/api/pipeline.py",
    "opendata/services/data_acquisition.py",
)

RIGHTS_INVENTORY = "docs/proposals/openbb-migration/provider-inventory.yaml"

#: What the first migration commit has to contain for "首次完整提交" to mean a complete import
#: rather than a fragment. Deliberately about *shape*: the tree grows every round afterwards, so a
#: judge that compared the first commit with today's file list would decay on its own. The
#: vendored tree is absent on purpose: it was still under its old directory name at import time,
#: and its rename belongs to AC-1|02/03 rather than to this commit.
FIRST_COMMIT_ESSENTIALS: Final = ("pyproject.toml", "README.md", "opendata/__init__.py")
FIRST_COMMIT_CATEGORIES: Final = ("opendata/", "scripts/", "tests/", "docs/")

AC_HEADING = re.compile(r"^### (AC-\d+)")
SECTION_HEADING = re.compile(r"^## (\d+)[.、]? ")
ITEM_LINE = re.compile(r"^- \[([ xX])\] (.*)$")
BACKTICK = re.compile(r"`([^`]+)`")
PYTEST_TALLY = re.compile(r"(\d+) (passed|failed|errors?|skipped|deselected)")

#: Facts stay strings so a counterfact is one ``update()`` away from any measurement.
Facts = dict[str, str]


class ProbeError(RuntimeError):
    """A surface a probe needs is missing, which is a finding rather than a skip."""


@dataclass(frozen=True)
class Verdict:
    """What one item's facts add up to.

    Attributes:
        state: ``proven`` or ``gap``.
        readings: The measured facts, in the order a reader should see them.
        reason: Why a gap is a gap; empty when the verdict is ``proven``.
    """

    state: str
    readings: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class DocItem:
    """One checklist criterion as the document currently states it.

    Attributes:
        key: ``AC-N|NN|digest``, identical to the ledger's key for the same wording.
        group: ``AC-N``.
        index: 1-based position inside that block.
        line: 1-based line number, for messages.
        text: Criterion text with the checkbox prefix removed.
        ticked: Whether the box is checked in the document.
    """

    key: str
    group: str
    index: int
    line: int
    text: str
    ticked: bool


@dataclass(frozen=True)
class Break:
    """A fact mutation and the verdict it must produce.

    Attributes:
        label: What the mutation stands for; printed by ``--self-test``.
        facts: Overrides applied to the *repaired* facts, so each break isolates one face.
        expect: The state the judge must return afterwards; always a gap.
    """

    label: str
    facts: tuple[tuple[str, str], ...]
    expect: str


@dataclass(frozen=True)
class Probe:
    """One item: how to measure it, how to judge it, how to prove the judge bites.

    Attributes:
        item: ``AC-N|NN``, the ledger key without its text digest.
        expects: Substring that must still appear in the criterion's wording.
        summary: One line saying what this probe actually decides.
        measure: Reads the repository into string facts.
        judge: Pure function from facts to a verdict.
        breaks: Mutations with the verdict each must produce.
        repair: The reading the judge has to accept as ``proven``. Applied on top of the measured
            facts before any break, so a judge that can never be satisfied -- or one whose gaps
            are invisible because several faces fail at once -- is a self-test failure rather
            than a comfortable red light. For a gap this doubles as "what would close it".
    """

    item: str
    expects: str
    summary: str
    measure: Callable[[Context], Facts]
    judge: Callable[[Facts], Verdict]
    breaks: tuple[Break, ...]
    repair: Facts


class Context:
    """Shared measurements, so fifteen probes do not shell out to git fifteen times.

    Attributes:
        root: Repository root.
        doc_items: Every checklist item the document currently holds.
        ledger: Ledger entries, keyed the same way.
    """

    def __init__(
        self, root: Path, doc_items: tuple[DocItem, ...], ledger: dict[str, Facts]
    ) -> None:
        """Store the parsed inputs with no measurements taken yet."""
        self.root = root
        self.doc_items = doc_items
        self.ledger = ledger
        self._tracked: list[str] | None = None

    def read(self, rel: str) -> str:
        """Read a repository file, or say which one could not be read."""
        path = self.root / rel
        if not path.is_file():
            raise ProbeError(f"{rel} is missing")
        return path.read_text(encoding="utf-8", errors="replace")

    def tracked(self) -> list[str]:
        """Every path git would ship, measured once."""
        if self._tracked is None:
            code, out = run_argv(["git", "ls-files"])
            if code != 0:
                raise ProbeError(f"git ls-files failed: {out.strip()[:120]}")
            self._tracked = sorted(line for line in out.splitlines() if line)
        return list(self._tracked)

    def item(self, item: str) -> DocItem:
        """The document's current wording for ``AC-N|NN``."""
        group, _, index = item.partition("|")
        for doc in self.doc_items:
            if doc.group == group and doc.index == int(index):
                return doc
        raise ProbeError(f"{item} has no item block in {DOC_REL}")

    def ledger_entry(self, item: str) -> Facts:
        """The ledger entry for an item, which also proves both parsers agree on its key."""
        doc = self.item(item)
        entry = self.ledger.get(doc.key)
        if entry is None:
            raise ProbeError(f"{doc.key} is absent from {LEDGER_REL}: the two parsers drifted")
        return entry


# --------------------------------------------------------------------------- #
# Measurement primitives
# --------------------------------------------------------------------------- #


def run_argv(argv: Sequence[str]) -> tuple[int, str]:
    """Run a literal command in the repository and return ``(exit, output)``.

    No shell, so a pattern containing a ``;`` stays a pattern. A non-zero exit is a reading,
    not an exception: several probes exist precisely to report a red tool.

    Args:
        argv: Executable and its arguments.

    Returns:
        The exit code and stdout+stderr concatenated.
    """
    try:
        proc = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
            list(argv),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
    except FileNotFoundError:
        return 127, f"(not installed): {argv[0]}"
    return proc.returncode, f"{proc.stdout}{proc.stderr}"


def run_pytest(targets: Sequence[str], keyword: str = "") -> tuple[int, str]:
    """Run named tests in a second interpreter, without coverage.

    ``-m 'not e2e'`` is not decoration: the warehouse e2e face talks to a live MySQL, and a
    probe that reaches for it while the gate runs would be a hazard, not a measurement.

    Args:
        targets: File paths or exact node ids.
        keyword: Optional ``-k`` expression.

    Returns:
        The exit code and the combined output.
    """
    argv = [
        sys.executable,
        "-m",
        "pytest",
        *targets,
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        "--no-cov",
        "-m",
        "not e2e",
    ]
    if keyword:
        argv += ["-k", keyword]
    code, out = run_argv(argv)
    return code, out


def node_outcome(node: str) -> str:
    """Run exactly one node id and report what it did.

    A node id that no longer exists is reported as ``missing``, which is how a probe notices a
    criterion's unit face was renamed rather than concluding it never existed.
    """
    code, out = run_pytest([node])
    if "no tests ran" in out or "ERROR: not found" in out or "errors" in out.splitlines()[-1]:
        return "missing"
    if code == 0 and "1 passed" in out:
        return "passed"
    if "1 skipped" in out:
        return "skipped"
    return f"exit={code}"


def tally(output: str) -> dict[str, int]:
    """Read pytest's own summary counts."""
    counts: dict[str, int] = {}
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return counts
    for value, label in PYTEST_TALLY.findall(lines[-1]):
        counts[label.rstrip("s")] = int(value)
    return counts


def grep_files(paths: Iterable[str], pattern: re.Pattern[str]) -> dict[str, int]:
    """Count pattern hits per path, skipping what cannot be read as text."""
    hits: dict[str, int] = {}
    for rel in paths:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found = len(pattern.findall(text))
        if found:
            hits[rel] = found
    return hits


def parse(target: str) -> ast.Module:
    """Parse a repository source file into a syntax tree."""
    return ast.parse((REPO_ROOT / target).read_text(encoding="utf-8"), filename=target)


def literal_str_tuple(tree: ast.Module, name: str) -> tuple[str, ...]:
    """Read a module-level tuple or list of string literals out of a syntax tree."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        value = node.value
        elts = value.elts if isinstance(value, (ast.Tuple, ast.List)) else ()
        return tuple(
            str(elt.value)
            for elt in elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        )
    raise ProbeError(f"{name} is not a module-level literal in the scanned file")


def literal_names(tree: ast.Module, name: str) -> tuple[str, ...]:
    """Read a module-level sequence of *class references* as the names they spell.

    ``ALL_MODELS = [Bar, ...]`` is a list of objects, not of strings; the parametrization ids
    in the test are ``m.__name__``, so the names have to come out of the syntax tree this way
    for "is every P0 model covered" to be measurable at all.
    """
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        value = node.value
        elts = value.elts if isinstance(value, (ast.Tuple, ast.List)) else ()
        names: list[str] = []
        for elt in elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                names.append(elt.value)
            elif isinstance(elt, ast.Name):
                names.append(elt.id)
            elif isinstance(elt, ast.Attribute):
                names.append(elt.attr)
        return tuple(names)
    raise ProbeError(f"{name} is not a module-level sequence in the scanned file")


@lru_cache(maxsize=1)
def brand_tokens() -> tuple[str, ...]:
    """The pre-rename identifiers, read out of the checker that enforces them.

    Deliberately not copied into this file: AC-1|02 asks for ``check_brand.py`` to be green over
    the whole tree, and that scanner walks untracked files too, so a local duplicate of the
    token list would be a finding of its own.
    """
    return literal_str_tuple(parse("scripts/quality/check_brand.py"), "BRAND_TOKENS")


@lru_cache(maxsize=1)
def brand_token_pattern() -> re.Pattern[str]:
    """One alternation over :func:`brand_tokens`, longest first so prefixes do not shadow."""
    ordered = sorted(brand_tokens(), key=len, reverse=True)
    return re.compile("|".join(re.escape(token) for token in ordered))


def function_body(source: str, name: str) -> str:
    """The source of one module-level function, so a judge can read what it actually does.

    ``AsyncFunctionDef`` counts: an API route or a job executor that is ``async def`` is the
    same function to a judge, and reading it as absent would report a real body as an empty
    string -- a gap that no work could close.
    """
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    return ""


def set_literal_members(source: str, name: str) -> tuple[str, ...]:
    """The element source of a module-level ``NAME = frozenset({...})`` constant.

    Read from the AST and not a regex: adding one member makes the formatter rewrap the
    literal across lines, and a pattern that assumes ``frozenset({`` sits on one line then
    reports a present kind as missing. That is exactly how a live executor door went unread.
    """
    tree = ast.parse(source)
    for node in tree.body:
        targets: Sequence[ast.expr] = ()
        literal: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, literal = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, literal = (node.target,), node.value
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        if literal is None:
            continue
        value: ast.expr = literal
        if isinstance(value, ast.Call) and value.args:
            value = value.args[0]
        if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            return tuple(ast.get_source_segment(source, elt) or "" for elt in value.elts)
    return ()


def count(value: int) -> str:
    """A counter as a fact."""
    return str(value)


def flag(value: bool) -> str:
    """A boolean as a fact, spelled the way the judges read it."""
    return "yes" if value else "no"


def resolve_repair(facts: Facts, repair: Facts) -> Facts:
    """Apply a probe's declared clean reading to a measurement.

    A repair value of ``*key`` copies the measured value of ``key`` instead of a literal, for the
    gates that compare two faces with each other (``passed == runs``, ``dated == rows``). Writing
    today's numbers there would make the judge unreachable the day a test is added, and an
    unreachable judge cannot be counterfact-tested at all.
    """
    clean = dict(facts)
    for key, value in repair.items():
        if value.startswith("*"):
            referenced = value[1:]
            clean[key] = facts.get(referenced, facts.get(key, ""))
        else:
            clean[key] = value
    return clean


def first_capture(text: str, pattern: str) -> str:
    """One capture out of a file, or a marker saying that shape moved."""
    found = re.search(pattern, text, re.MULTILINE)
    if not found:
        return "(absent)"
    return re.sub(r"[:}\s]+$", "", found.group(1))


# --------------------------------------------------------------------------- #
# AC-1 -- brand, licence, configuration, credentials
# --------------------------------------------------------------------------- #


def measure_ac1_01(ctx: Context) -> Facts:
    """Read the project's own name, version and package layout out of ``pyproject.toml``."""
    data = tomllib.loads(ctx.read("pyproject.toml"))
    project = data.get("project", {})
    include = (
        data.get("tool", {})
        .get("setuptools", {})
        .get("packages", {})
        .get("find", {})
        .get("include", [])
    )
    tracked = ctx.tracked()
    return {
        "name": str(project.get("name", "")),
        "version": str(project.get("version", "")),
        "opendata_pkg": flag("opendata/__init__.py" in tracked),
        "app_pkg_files": count(len([p for p in tracked if p.startswith("app/")])),
        "first_include": str(include[0]) if include else "(none)",
    }


def judge_ac1_01(facts: Facts) -> Verdict:
    """``AC-1|01``: the deliverable is opendata 0.1.0 and ``opendata/`` is its package."""
    ok = (
        facts["name"] == "opendata"
        and facts["version"] == "0.1.0"
        and facts["opendata_pkg"] == "yes"
        and facts["app_pkg_files"] == "0"
        and facts["first_include"].startswith("opendata")
    )
    readings = (
        f"pyproject project.name = {facts['name']}",
        f"pyproject project.version = {facts['version']}",
        f"opendata/__init__.py tracked = {facts['opendata_pkg']}",
        f"tracked files under the old app/ layout = {facts['app_pkg_files']}",
        f"first setuptools find include = {facts['first_include']}",
    )
    reason = (
        ""
        if ok
        else "the identity surface does not read opendata / 0.1.0 / opendata/ as its package"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac1_02(ctx: Context) -> Facts:
    """Compare the brand tokens the item names with the ones the checker enforces, then run it."""
    doc_tokens = sorted(
        {
            token
            for token in BACKTICK.findall(ctx.item("AC-1|02").text)
            if token.startswith(("akshare_", "akshare-"))
        }
    )
    script_tokens = sorted(
        literal_str_tuple(parse("scripts/quality/check_brand.py"), "BRAND_TOKENS")
    )
    code, out = run_argv([sys.executable, "scripts/quality/check_brand.py"])
    app_ref = re.compile(r"^\s*(?:from app[.\s]|import app[.\s])", re.MULTILINE)
    own = [
        p
        for p in ctx.tracked()
        if p.endswith(".py") and not p.startswith(("opendata_http/", "docs/"))
    ]
    hits = grep_files(own, app_ref)
    lines = [line for line in out.splitlines() if line.strip()]
    return {
        "doc_tokens": count(len(doc_tokens)),
        "script_tokens": count(len(script_tokens)),
        "sets_equal": flag(doc_tokens == script_tokens),
        "checker_exit": count(code),
        "checker_line": (lines[-1] if lines else "(no output)")[:110],
        "app_ref_files": count(len(hits)),
        "app_ref_sample": ", ".join(sorted(hits)[:4]),
        "app_ref_scope": count(len(own)),
    }


def judge_ac1_02(facts: Facts) -> Verdict:
    """``AC-1|02``: the item's own token list, a green scan, and no ``app.`` import left."""
    ok = (
        facts["sets_equal"] == "yes"
        and int(facts["doc_tokens"]) >= 2
        and facts["checker_exit"] == "0"
        and facts["app_ref_files"] == "0"
    )
    readings = (
        f"tokens enumerated by the item = {facts['doc_tokens']}; enforced by check_brand.py = "
        f"{facts['script_tokens']}; identical = {facts['sets_equal']}",
        f"$ python scripts/quality/check_brand.py -> exit {facts['checker_exit']}: "
        f"{facts['checker_line']}",
        f"independent grep for `from app.` / `import app.` over {facts['app_ref_scope']} own .py "
        f"files -> {facts['app_ref_files']} file(s)"
        + (f": {facts['app_ref_sample']}" if facts["app_ref_sample"] else ""),
    )
    reason = (
        ""
        if ok
        else "the enforced list must equal the item's, the scanner must exit 0, the grep "
        "must come back empty on its own"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def whitelist_buckets(text: str) -> list[str]:
    """The path whitelist an item enumerates, read back out of its own wording."""
    buckets: list[str] = []
    for token in BACKTICK.findall(text):
        bare = token.strip().rstrip("、,。")
        if " " in bare or bare.startswith(("from ", "import ")):
            continue
        if "/" in bare or bare.endswith(".md") or bare.startswith("LICENSE"):
            buckets.append("opendata_http/" if bare == "akshare/" else bare)
    return buckets


def covered_by(path: str, buckets: Sequence[str]) -> bool:
    """Whether one hit path falls under a whitelisted entry.

    A bucket without a trailing slash names one file at the repository root, which is how the
    item writes ``README.md``; a bucket with one is a prefix.
    """
    for bucket in buckets:
        if bucket.endswith("/"):
            if path.startswith(bucket):
                return True
        elif "/" not in bucket and path == bucket:
            return True
    return False


def measure_ac1_03(ctx: Context) -> Facts:
    """Ask where the bare word ``akshare`` still appears and whether the item lists those places."""
    item = ctx.item("AC-1|03")
    buckets = whitelist_buckets(item.text)
    pattern = re.compile(r"\bakshare\b")
    tracked = ctx.tracked()
    hits = grep_files(tracked, pattern)
    vendored = [rel for rel in hits if rel.startswith("opendata_http/")]
    outside = [
        rel for rel in hits if not covered_by(rel, buckets) and not rel.startswith("opendata_http/")
    ]
    baseline = json.loads(ctx.read("docs/quality/zero-dep-baseline.json"))
    integration = sorted(
        {
            str(f.get("file", ""))
            for f in baseline.get("findings", [])
            if isinstance(f, dict) and f.get("module") == "akshare"
        }
    )
    return {
        "buckets": count(len(buckets)),
        "bucket_names": ", ".join(buckets) or "(none)",
        "hit_files": count(len(hits)),
        "vendored": count(len(vendored)),
        "outside": count(len(outside)),
        "outside_sample": ", ".join(sorted(outside)[:10]),
        "outside_more": count(max(0, len(outside) - 10)),
        "integration_frozen": count(len(integration)),
        "integration_files": ", ".join(integration),
    }


def judge_ac1_03(facts: Facts) -> Verdict:
    """``AC-1|03``: every bare-token hit sits somewhere the item itself names."""
    ok = facts["outside"] == "0"
    readings = (
        f"whitelist read back out of the item ({facts['buckets']} entries): "
        f"{facts['bucket_names']}",
        f"tracked files carrying the bare word `akshare` = {facts['hit_files']}, of which the "
        f"vendored tree = {facts['vendored']}",
        f"hits outside that whitelist = {facts['outside']} (+{facts['outside_more']} unshown): "
        f"{facts['outside_sample'] or '-'}",
        f"AC-16's frozen integration-layer findings = {facts['integration_frozen']} "
        f"({facts['integration_files']})",
    )
    reason = (
        ""
        if ok
        else "the item's enumerated whitelist does not describe the tree: the word is in "
        "build and operational surfaces it never listed. Closing this needs a wording decision "
        "(what the whitelist is supposed to cover after A2/B5), not a re-read"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac1_04(ctx: Context) -> Facts:
    """Check the four renamed surfaces as surfaces: containers, DB user, root docs, hook wiring."""
    tracked = ctx.tracked()
    compose = ctx.read("docker-compose.yml")
    names = re.findall(r"container_name:\s*(\S+)", compose)
    foreign = [n for n in names if not n.startswith("opendata_")]
    networks = re.findall(r"^\s+name:\s*(\S+)", compose, re.MULTILINE)
    user_surfaces = {
        rel: ctx.read(rel) for rel in (".env.example", "docker-compose.yml", "init.sql")
    }
    missing_user = [rel for rel, text in user_surfaces.items() if "opendata_user" not in text]
    token_pattern = brand_token_pattern()
    machine_hits = grep_files([p for p in AC1_04_MACHINE_SURFACES if p in tracked], token_pattern)
    live_lines: list[str] = []
    historical = 0
    for rel in AC1_04_ROOT_DOCS:
        if rel not in tracked:
            continue
        for number, line in enumerate(ctx.read(rel).splitlines(), start=1):
            if not token_pattern.search(line):
                continue
            if re.search(PROVENANCE_MARKER, line):
                historical += 1
            else:
                live_lines.append(f"{rel}:{number}")
    stale_docs = [p for p in tracked if "/" not in p and token_pattern.search(p)]
    referenced: set[str] = set()
    for line in ctx.read("Makefile").splitlines():
        for cand in re.findall(
            r"(?:\./)?((?:scripts|docs|tests|opendata|opendata_fuyao|frontend|init\.sql)"
            r"/?[A-Za-z0-9_./-]*)",
            line,
        ):
            cand = cand.rstrip(":,.)").lstrip("/")
            if "*" not in cand and "/" in cand:
                referenced.add(cand)
    missing = sorted(p for p in referenced if not (REPO_ROOT / p).exists())
    precommit = ctx.read(".pre-commit-config.yaml")
    patterns = [
        p.strip("'\"")
        for p in re.findall(r"^\s*(?:files|exclude):\s*([^;\n]+)", precommit, re.MULTILINE)
    ]
    stale: list[str] = []
    for pat in patterns:
        try:
            rx = re.compile(pat)
        except re.error:
            stale.append(f"(unparseable) {pat}")
            continue
        if not any(rx.search(p) for p in tracked):
            stale.append(pat)
    return {
        "containers": ", ".join(names),
        "foreign": count(len(foreign)),
        "networks": ", ".join(networks),
        "user_missing": ", ".join(missing_user) or "-",
        "machine_files": count(len(machine_hits)),
        "machine_sample": ", ".join(sorted(machine_hits)[:5]),
        "doc_live": count(len(live_lines)),
        "doc_live_sample": ", ".join(live_lines[:6]),
        "doc_historical": count(historical),
        "stale_docs": count(len(stale_docs)),
        "makefile_paths": count(len(referenced)),
        "missing_paths": count(len(missing)),
        "missing_sample": ", ".join(missing[:6]),
        "hook_patterns": count(len(patterns)),
        "stale_patterns": count(len(stale)),
        "stale_sample": ", ".join(stale[:6]),
    }


def judge_ac1_04(facts: Facts) -> Verdict:
    """``AC-1|04``: containers, the DB user, root documents and Makefile/hook wiring all moved."""
    ok = (
        facts["foreign"] == "0"
        and facts["user_missing"] == "-"
        and facts["machine_files"] == "0"
        and facts["doc_live"] == "0"
        and facts["stale_docs"] == "0"
        and facts["missing_paths"] == "0"
        and facts["stale_patterns"] == "0"
    )
    readings = (
        f"compose container_name set: {facts['containers']} -> not opendata_* = {facts['foreign']}",
        f"compose named networks: {facts['networks']}",
        f"`opendata_user` missing from: {facts['user_missing']} "
        f"(checked .env.example, compose, init.sql)",
        f"pre-rename tokens in the {len(AC1_04_MACHINE_SURFACES)} machine surfaces = "
        f"{facts['machine_files']}"
        + (f" ({facts['machine_sample']})" if facts["machine_sample"] else ""),
        f"root documents: lines that still *use* a token = {facts['doc_live']}"
        + (f" ({facts['doc_live_sample']})" if facts["doc_live_sample"] else "")
        + f"; lines that name it as history = {facts['doc_historical']}"
        f" (沿革引用, allowed by AC-1|03 over {len(AC1_04_ROOT_DOCS)} files)",
        f"root-level file *names* still carrying a token = {facts['stale_docs']}",
        f"Makefile path references = {facts['makefile_paths']}, absent = {facts['missing_paths']}"
        + (f" ({facts['missing_sample']})" if facts["missing_sample"] else ""),
        f"pre-commit files:/exclude: regexes = {facts['hook_patterns']}, matching no tracked file "
        f"= {facts['stale_patterns']}"
        + (f" ({facts['stale_sample']})" if facts["stale_sample"] else ""),
    )
    reason = (
        ""
        if ok
        else "a renamed surface is still wrong or still wired to something that no longer exists: "
        "the reading above names which one"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac1_05(ctx: Context) -> Facts:
    """Read the two database names from four configuration surfaces, then ask about runtime."""
    config = ctx.read("opendata/core/config.py")

    def default(field: str) -> str:
        found = re.search(rf'{field}: str = Field\(\s*default="([^"]+)"', config)
        return found.group(1) if found else "(absent)"

    env = ctx.read(".env.example")
    compose = ctx.read("docker-compose.yml")
    # Python 3.10 forbids a backslash inside an f-string expression, so these two captures are
    # taken outside the literals rather than inlined.
    compose_main = first_capture(compose, r"MYSQL_DATABASE: \$\{MYSQL_DATABASE:-(.+)\}")
    compose_wh = first_capture(compose, r"MYSQL_DATABASE: \$\{WAREHOUSE_DATABASE:-(.+)\}")
    surfaces = {
        "config": f"{default('mysql_database')} / {default('data_mysql_database')}",
        "env_example": f"{first_capture(env, r'^MYSQL_DATABASE=(.+)')} / "
        f"{first_capture(env, r'^DATA_MYSQL_DATABASE=(.+)')}",
        "compose": f"{compose_main} / {compose_wh}",
    }
    created = re.findall(r"CREATE DATABASE IF NOT EXISTS `([^`]+)`", ctx.read("init.sql"))
    surfaces["init_sql"] = f"{created[0] if created else '(absent)'} / "
    surfaces["init_sql"] += f"{created[1] if len(created) > 1 else '(absent)'}"
    expected = f"{MAIN_DB} / {WAREHOUSE_DB}"
    consistent = flag(all(text == expected for text in surfaces.values()))
    rc, out = run_argv(["docker", "info", "--format", "{{.ServerVersion}}"])
    note = (out.strip().splitlines() or ["(no output)"])[-1][:80]
    return {
        **surfaces,
        "consistent": consistent,
        "docker_rc": count(rc),
        "docker_note": note,
        "runtime": "no-engine" if rc != 0 else "not-started",
    }


def judge_ac1_05(facts: Facts) -> Verdict:
    """``AC-1|05``: the names agree in four places -- and the stack serving is a second claim."""
    static_ok = facts["consistent"] == "yes"
    ok = static_ok and facts["runtime"] == "ok"
    expected = f"{MAIN_DB} / {WAREHOUSE_DB}"
    readings = (
        f"config.py defaults, .env.example, docker-compose.yml, init.sql all read {expected} = "
        f"{facts['consistent']}",
        f"  config.py = {facts['config']}",
        f"  .env.example = {facts['env_example']}",
        f"  docker-compose.yml = {facts['compose']}",
        f"  init.sql = {facts['init_sql']}",
        f"backend start / frontend login / GET /health = {facts['runtime']} "
        f"(docker info exit {facts['docker_rc']}: {facts['docker_note']})",
    )
    reason = (
        ""
        if ok
        else "the four configuration surfaces are half the item; the other half is that the stack "
        "actually serves -- backend up, a frontend login, /health answering. It needs the "
        "stack started, which is the user's call, so the runtime face is reported as no-engine or "
        "not-started rather than assumed to work"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac1_06(ctx: Context) -> Facts:
    """Ask which services the default ``docker compose up`` set really contains."""
    compose = ctx.read("docker-compose.yml")
    blocks = re.split(r"^  (\w+):\s*$", compose, flags=re.MULTILINE)
    profiled: dict[str, str] = {}
    for index in range(1, len(blocks) - 1, 2):
        name, body = blocks[index], blocks[index + 1]
        found = re.search(r"profiles:\s*\[?([^\]\n]+)", body)
        if found:
            profiled[name] = found.group(1).strip().rstrip("]")
    rc, out = run_argv(["docker", "compose", "config", "--services"])
    listed = [line.strip() for line in out.splitlines() if re.fullmatch(r"\w+", line.strip())]
    return {
        "profiled": ", ".join(f"{k}={v}" for k, v in sorted(profiled.items())) or "(none)",
        "warehouse_profiled": flag("mysql_warehouse" in profiled),
        "cli_rc": count(rc),
        "cli_listed": count(len(listed)),
        "in_cli_set": (flag("mysql_warehouse" in listed) if listed else "n/a"),
        "cli_note": (out.strip().splitlines() or ["(no output)"])[-1][:80],
    }


def judge_ac1_06(facts: Facts) -> Verdict:
    """``AC-1|06``: the warehouse container needs no ``--profile`` flag to come up."""
    ok = facts["warehouse_profiled"] == "no" and facts["in_cli_set"] in {"yes", "n/a"}
    readings = (
        f"services sitting behind a profile: {facts['profiled']}",
        f"mysql_warehouse requires --profile = {facts['warehouse_profiled']}",
        f"`docker compose config --services` resolved {facts['cli_listed']} service(s) and lists "
        f"the warehouse = {facts['in_cli_set']} (exit {facts['cli_rc']}: {facts['cli_note']})",
    )
    reason = (
        ""
        if ok
        else "the warehouse service is still behind a profile, or compose will not resolve "
        "start it in the default set"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac1_07(ctx: Context) -> Facts:
    """Read the licence parameter block, the notices file and README's three claims."""
    license_text = ctx.read("LICENSE")
    params = dict(
        re.findall(
            r"^(Licensor|Licensed Work|Additional Use Grant|Change Date|Change License):"
            r"[ \t]*(.+)$",
            license_text,
            re.MULTILINE,
        )
    )
    missing = {"Licensor", "Additional Use Grant", "Change Date", "Change License"} - set(params)
    tracked = ctx.tracked()
    notices = ctx.read("THIRD_PARTY_NOTICES.md")
    readme = ctx.read("README.md")
    contact = re.search(r"商务授权联系[：:]|商务联系[：:]", readme)
    contact_line = ""
    if contact:
        rest = readme[contact.start() :]
        contact_line = rest.splitlines()[0].strip()
    placeholder = any(mark in contact_line for mark in ("example.com", "占位", "your-"))
    return {
        "bsl": flag(license_text.startswith("Business Source License 1.1")),
        "params": ", ".join(sorted(params)),
        "missing": count(len(missing)),
        "change_license": params.get("Change License", "(absent)").strip(),
        "akshare_license": flag(
            "LICENSE-AKSHARE" in tracked and "MIT" in ctx.read("LICENSE-AKSHARE")
        ),
        "notices_sections": count(len(re.findall(r"^## \d\.", notices, re.MULTILINE))),
        "resource_registry": flag("内嵌第三方资源" in notices),
        "license_notice": flag("许可" in readme or "授权" in readme),
        "disclaimer": flag("数据免责声明" in readme),
        "contact": contact_line[:70] or "(absent)",
        "placeholder": flag(placeholder or not contact_line),
    }


def judge_ac1_07(facts: Facts) -> Verdict:
    """``AC-1|07``: BSL 1.1 with its four elements, the vendored licence, README's three claims."""
    ok = (
        facts["bsl"] == "yes"
        and facts["missing"] == "0"
        and facts["change_license"].startswith("MIT")
        and facts["akshare_license"] == "yes"
        and facts["notices_sections"] != "0"
        and facts["resource_registry"] == "yes"
        and facts["license_notice"] == "yes"
        and facts["disclaimer"] == "yes"
        and facts["placeholder"] == "no"
    )
    readings = (
        f"LICENSE opens with Business Source License 1.1 = {facts['bsl']}",
        f"parameter block = {facts['params']} (missing of the four = {facts['missing']}); "
        f"Change License = {facts['change_license']}",
        f"LICENSE-AKSHARE present and MIT = {facts['akshare_license']}",
        f"THIRD_PARTY_NOTICES.md has {facts['notices_sections']} sections; embedded-resource "
        f"rights block = {facts['resource_registry']}",
        f"README 授权/许可说明 = {facts['license_notice']}; 数据免责声明 = {facts['disclaimer']}",
        f"README 商务联系方式 = {facts['contact']} (placeholder = {facts['placeholder']})",
    )
    reason = (
        "the item asks for a business contact; README publishes the placeholder it labels as such "
        "(`发布前替换`). That is the user's text to supply, so this stays a gap rather than a tick"
        if facts["placeholder"] == "yes"
        else ""
        if ok
        else "one of the licence / notices / README markers is not where the item says it is"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac1_08(ctx: Context) -> Facts:
    """Parse the rights registry and ask whether it is filled in, not merely present."""
    text = ctx.read("docs/data-rights-registry.md")
    rows = [line for line in text.splitlines() if re.match(r"^\|\s*\d+\s*\|", line)]
    header = next((line for line in text.splitlines() if line.startswith("| #")), "")
    cells = [c.strip() for c in header.strip("|").split("|")]

    def column(row: str, name: str) -> str:
        for index, cell in enumerate(cells):
            if name in cell:
                parts = [c.strip() for c in row.strip("|").split("|")]
                if index < len(parts):
                    return parts[index]
        return ""

    dated = [r for r in rows if re.search(r"\d{4}-\d{2}-\d{2}", column(r, "复核日期"))]
    uses = ("允许本项目落库", "允许再分发", "允许商业使用")
    undecided = [r for r in rows if any("待确认" in column(r, u) for u in uses)]
    unlinked = [r for r in rows if not column(r, "条款链接").startswith("http")]
    named = {column(r, "数据源") for r in rows}
    inventory_text = ctx.read(RIGHTS_INVENTORY)
    cited: set[str] = set()
    for line in inventory_text.splitlines():
        found = re.match(r"\s+rights_rows:\s*\[(.*)\]", line)
        if found:
            cited |= {c.strip().strip("'\"") for c in found.group(1).split(",") if c.strip()}
    known = [row for row in named if row]
    uncovered = sorted(c for c in cited if not any(c in row or row.startswith(c) for row in known))
    return {
        "rows": count(len(rows)),
        "columns": ", ".join(cells),
        "dated": count(len(dated)),
        "undecided": count(len(undecided)),
        "unlinked": count(len(unlinked)),
        "cited": count(len(cited)),
        "uncovered": count(len(uncovered)),
        "uncovered_sample": ", ".join(uncovered[:6]),
    }


def judge_ac1_08(facts: Facts) -> Verdict:
    """``AC-1|08``: the registry exists, covers every cited source, and has been reviewed."""
    ok = (
        int(facts["rows"]) > 0
        and facts["dated"] == facts["rows"]
        and facts["undecided"] == "0"
        and facts["unlinked"] == "0"
        and facts["uncovered"] == "0"
    )
    readings = (
        f"rows = {facts['rows']}; columns = {facts['columns']}",
        f"rows carrying a real 复核日期 = {facts['dated']}/{facts['rows']}",
        f"rows still answering a permitted use with 待确认 = {facts['undecided']}/{facts['rows']}",
        f"rows whose 条款链接 is prose instead of a URL = {facts['unlinked']}/{facts['rows']}",
        f"registry rows cited by {RIGHTS_INVENTORY} = {facts['cited']}, citations with no row = "
        f"{facts['uncovered']}"
        + (f" ({facts['uncovered_sample']})" if facts["uncovered_sample"] else ""),
    )
    reason = (
        "the registry is a shell waiting on a legal/product review: no row carries a review date, "
        "every permitted-use cell says 待确认, and some clause links are prose. Filling it in is a "
        "decision this round cannot make for the user"
        if not ok
        else ""
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac1_09(ctx: Context) -> Facts:
    """Split the clause into git's answer, the scanner's exemptions, and upstream files."""
    tracked = ctx.tracked()
    literal = [p for p in tracked if re.search(r"\.env|\.idea|\.pid", p)]
    strict = [p for p in tracked if re.search(r"(^|/)\.env$|(^|/)\.idea(/|$)|\.pid$", p)]
    config = ctx.read(".gitleaks.toml")
    head = config.split("[[rules]]")[0]
    allow_paths = re.findall(r"'''([^'\n]+)'''", head)
    rule_blocks = config.count("[[rules]]")
    non_template = [p for p in allow_paths if "env" not in p and "example" not in p]
    files = (
        "opendata_http/stock/cons.py",
        "opendata_http/bond/bond_convert.py",
        "opendata_http/bond/bond_china_money.py",
        "opendata_http/futures/futures_hf_em.py",
        "opendata_http/option/option_em.py",
    )
    audit = ctx.read("docs/evidence/A0/secret-audit.txt")
    registered = [f for f in files if f.split("opendata_http/", 1)[1] in audit]
    cred_shape = re.compile(
        r"(?:token|api_?key|password|pwd|secret)[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_.\-]{16,}[\"']",
        re.IGNORECASE,
    )
    live = grep_files([f for f in files if (REPO_ROOT / f).is_file()], cred_shape)
    code, out = run_argv([sys.executable, "scripts/quality/secret_scan_check.py"])
    lines = [line for line in out.splitlines() if line.strip()]
    return {
        "literal": count(len(literal)),
        "literal_sample": ", ".join(literal[:5]),
        "strict": count(len(strict)),
        "allow_paths": count(len(allow_paths)),
        "allow_names": ", ".join(allow_paths),
        "non_template": count(len(non_template)),
        "non_template_names": ", ".join(non_template),
        "rule_blocks": count(rule_blocks),
        "registered": count(len(registered)),
        "upstream_files": count(len(files)),
        "live_shapes": count(len(live)),
        "gitleaks_rc": count(code),
        "secret_check_line": (lines[-1] if lines else "(no output)")[:110],
    }


def judge_ac1_09(facts: Facts) -> Verdict:
    """``AC-1|09``: nothing secret is tracked, and exemptions are only docs/templates."""
    ok = (
        facts["literal"] == "0"
        and facts["strict"] == "0"
        and facts["non_template"] == "0"
        and facts["rule_blocks"] == "0"
        and facts["registered"] == facts["upstream_files"]
        and facts["live_shapes"] == "0"
        and facts["gitleaks_rc"] == "0"
    )
    readings = (
        f"`git ls-files | grep -E '\\.env|\\.idea|\\.pid'` -> {facts['literal']} path(s)"
        + (f" ({facts['literal_sample']})" if facts["literal_sample"] else ""),
        f"the same grep as real files (`(^|/)\\.env$`, `.idea/`, `*.pid`) -> {facts['strict']}",
        f".gitleaks.toml global allowlist = {facts['allow_paths']} ({facts['allow_names']}); "
        f"entries that are neither a doc nor a template = {facts['non_template']}"
        + (f" ({facts['non_template_names']})" if facts["non_template_names"] else ""),
        f"per-rule allowlist blocks = {facts['rule_blocks']} (C36 added a shape-scoped "
        f"generic-api-key exemption, whose widening C36 measured by counterfact)",
        f"upstream credential files registered in docs/evidence/A0/secret-audit.txt = "
        f"{facts['registered']}/{facts['upstream_files']}; cred-shaped literals left in them = "
        f"{facts['live_shapes']}",
        f"$ python scripts/quality/secret_scan_check.py -> exit {facts['gitleaks_rc']}: "
        f"{facts['secret_check_line']}",
    )
    reason = (
        "the literal grep the item names hits `.env.example`, the template AC-1|03 permits; the "
        "allowlist is not only docs/templates -- it carries a generated-artifact path plus a "
        "shape-scoped rule exemption. Both need a wording or config decision, and the counterfact "
        "evidence in docs/evidence/C36 shows the exemption is not free"
        if not ok
        else ""
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac1_10(ctx: Context) -> Facts:
    """Look for copy residue, for files git never saw, and for the first opendata commit's size."""
    tracked = ctx.tracked()
    residue = [p for p in tracked if any(re.search(rx, p) for rx in RESIDUE_PATTERNS)]
    rc_status, status = run_argv(["git", "status", "--porcelain", "--untracked-files=all"])
    untracked = [line[3:] for line in status.splitlines() if line.startswith("??")]
    rc_log, first = run_argv(
        ["git", "log", "--reverse", "--format=%H", "--", "opendata/__init__.py"]
    )
    sha = first.splitlines()[0] if first.splitlines() else ""
    rc_tree, listing = run_argv(["git", "ls-tree", "-r", "--name-only", sha]) if sha else (2, "")
    tree_files = [line for line in listing.splitlines() if line]
    rc_sub, subject = (
        run_argv(["git", "log", "-1", "--format=%h %ad %s", "--date=short", sha])
        if sha
        else (2, "")
    )
    modified = [
        line[3:] for line in status.splitlines() if line.strip() and not line.startswith("??")
    ]
    present = set(tree_files)
    absent = [p for p in FIRST_COMMIT_ESSENTIALS if p not in present]
    absent += [
        f"{c}* (no file)"
        for c in FIRST_COMMIT_CATEGORIES
        if not any(p.startswith(c) for p in tree_files)
    ]
    return {
        "residue": count(len(residue)),
        "residue_sample": ", ".join(residue[:8]),
        "untracked": count(len(untracked)),
        "untracked_sample": ", ".join(untracked[:8]),
        "dirty": count(len(modified)),
        "dirty_sample": ", ".join(modified[:8]),
        "first_sha": sha[:8],
        "first_subject": subject.strip()[:80],
        "first_tree": count(len(tree_files)),
        "essentials_absent": ", ".join(absent) or "-",
        "tracked_now": count(len(tracked)),
        "ratio": f"{len(tree_files) / len(tracked) * 100:.1f}%"
        if tracked and tree_files
        else "n/a",
        "git_rc": count(max(rc_status, rc_log, rc_tree, rc_sub)),
    }


def judge_ac1_10(facts: Facts) -> Verdict:
    """``AC-1|10``: the copy left no residue, and the migration landed in one complete commit."""
    ok = (
        facts["residue"] == "0"
        and facts["untracked"] == "0"
        and facts["dirty"] == "0"
        and facts["git_rc"] == "0"
        and facts["first_tree"] != "0"
        and facts["essentials_absent"] == "-"
    )
    readings = (
        f"tracked paths matching a copy-residue shape = {facts['residue']}"
        + (f" ({facts['residue_sample']})" if facts["residue_sample"] else ""),
        f"untracked-and-not-ignored files = {facts['untracked']}"
        + (f" ({facts['untracked_sample']})" if facts["untracked_sample"] else ""),
        f"tracked files with uncommitted edits = {facts['dirty']}"
        + (f" ({facts['dirty_sample']})" if facts["dirty_sample"] else ""),
        f"first commit touching opendata/__init__.py = {facts['first_sha']} "
        f'"{facts["first_subject"]}"',
        f"its tree = {facts['first_tree']} files, holding every part of the project it should "
        f"(missing: {facts['essentials_absent']}), against {facts['tracked_now']} tracked today "
        f"({facts['ratio']}; the rest arrived with later rounds, which is growth)",
    )
    reason = (
        ""
        if ok
        else "residue, files git has never seen, uncommitted edits, or a first commit that left "
        "part of the project out -- 'first complete commit' has to mean the whole move landed"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


# --------------------------------------------------------------------------- #
# AC-2 -- the contract layer, judged by the tests the item names
# --------------------------------------------------------------------------- #


def measure_ac2_01(ctx: Context) -> Facts:
    """Every named P0 model must be in the parametrization, with a passing node of its own."""
    rel = "tests/test_contract_models.py"
    source = ctx.read(rel)
    listed = literal_names(parse(rel), "ALL_MODELS")
    covered = [model for model in P0_MODELS if model in listed]
    nodes = [f"{rel}::TestRoundTrip::test_frame_round_trip[{m}]" for m in P0_MODELS]
    nodes += [f"{rel}::TestFailClosed::test_missing_required_column_raises[{m}]" for m in P0_MODELS]
    outcomes = {node.split("::")[-1]: node_outcome(node) for node in nodes}
    return {
        "param": count(len(listed)),
        "covered": count(len(covered)),
        "missing": ", ".join(m for m in P0_MODELS if m not in covered) or "-",
        "runs": count(len(nodes)),
        "passed": count(sum(1 for v in outcomes.values() if v == "passed")),
        "bad": ", ".join(f"{k}={v}" for k, v in outcomes.items() if v != "passed") or "-",
        "dual": flag("to_frame" in source and "from_frame" in source and "model_dump" in source),
    }


def judge_ac2_01(facts: Facts) -> Verdict:
    """``AC-2|01``: the six P0 models carry both forms, per model, in a running test."""
    ok = (
        facts["covered"] == count(len(P0_MODELS))
        and facts["passed"] == facts["runs"]
        and facts["runs"] == count(2 * len(P0_MODELS))
        and facts["dual"] == "yes"
    )
    readings = (
        f"ALL_MODELS parametrizes {facts['param']} model(s); the six P0 names are covered for "
        f"{facts['covered']} (missing: {facts['missing']})",
        f"round-trip + fail-closed nodes run = {facts['runs']}, passed = {facts['passed']}"
        + (f"; not green: {facts['bad']}" if facts["bad"] != "-" else ""),
        f"the file exercises both forms (DataFrame to_frame/from_frame, model_dump) = "
        f"{facts['dual']}",
    )
    reason = "" if ok else "each named model needs its own passing round-trip and fail-closed node"
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac2_02(ctx: Context) -> Facts:
    """Are the metadata contracts real, tested -- and imported by backfill/window code?"""
    metadata = ctx.read("opendata/data/models/metadata.py")
    nodes = (
        "tests/test_contract_models.py::TestFieldSets::test_instrument_field_set_matches_design",
        "tests/test_contract_models.py::TestFieldSets::"
        "test_trading_calendar_field_set_matches_design",
        "tests/test_contract_models.py::TestSemantics::test_calendar_non_trading_day",
        "tests/test_contract_models.py::TestRoundTrip::test_frame_round_trip[Instrument]",
        "tests/test_contract_models.py::TestRoundTrip::test_frame_round_trip[TradingCalendar]",
    )
    outcomes = {node.split("::")[-1]: node_outcome(node) for node in nodes}
    importers: list[str] = []
    mentioning: list[str] = []
    names = re.compile(r"\b(?:Instrument|TradingCalendar)\b")
    for rel in BACKFILL_MODULES:
        if not (REPO_ROOT / rel).is_file():
            continue
        text = ctx.read(rel)
        direct = re.findall(r"from opendata\.data\.models import ([^\n(]+)", text)
        if any("Instrument" in line or "TradingCalendar" in line for line in direct):
            importers.append(rel)
        elif names.search(text):
            mentioning.append(rel)
    return {
        "models": flag(
            "class Instrument(ContractModel)" in metadata
            and "class TradingCalendar(ContractModel)" in metadata
        ),
        "runs": count(len(nodes)),
        "passed": count(sum(1 for v in outcomes.values() if v == "passed")),
        "bad": ", ".join(f"{k}={v}" for k, v in outcomes.items() if v != "passed") or "-",
        "importers": count(len(importers)),
        "importer_names": ", ".join(importers) or "-",
        "mention_only": count(len(mentioning)),
        "mention_names": ", ".join(Path(p).name for p in mentioning) or "-",
    }


def judge_ac2_02(facts: Facts) -> Verdict:
    """``AC-2|02``: the models are real and green; the open half is whether code uses them."""
    ok = facts["models"] == "yes" and facts["passed"] == facts["runs"] and facts["importers"] != "0"
    readings = (
        "both classes subclass ContractModel in "
        f"opendata/data/models/metadata.py = {facts['models']}",
        f"the five field-set/semantics/round-trip nodes = {facts['passed']}/{facts['runs']} passed"
        + (f"; not green: {facts['bad']}" if facts["bad"] != "-" else ""),
        f"backfill / incremental-window modules importing the contracts = {facts['importers']} "
        f"({facts['importer_names']})",
        f"modules that only mention the names (docstrings, or a same-named local class) = "
        f"{facts['mention_only']} ({facts['mention_names']})",
    )
    reason = (
        "the metadata models exist and their unit face is green, but the full-market backfill and "
        "window code never import them: 'the backfill refreshes Instrument' is that module's "
        "docstring claim, not something the pipeline does -- patrol is the only consumer found"
        if not ok
        else ""
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac2_03(ctx: Context) -> Facts:
    """Bar's fields, the synthesis, and whether a recorded official qfq series gets compared."""
    bar = node_outcome(
        "tests/test_contract_models.py::TestFieldSets::test_bar_is_unadjusted_ohlcv_only"
    )
    rc_adjust, out_adjust = run_pytest(["tests/test_adjust.py"])
    adjust = tally(out_adjust)
    rc_official, out_official = run_pytest(
        ["tests/test_port_fidelity.py"], keyword="qfq_synthesis or factor_steps"
    )
    official = tally(out_official)
    _, collected = run_pytest(
        ["tests/test_port_fidelity.py", "--collect-only"], keyword="qfq_synthesis"
    )
    cases = sorted(
        set(re.findall(r"test_qfq_synthesis_reproduces_the_official_series\[(.*?)\]", collected))
    )
    return {
        "bar": bar,
        "adjust_passed": count(adjust.get("passed", 0)),
        "adjust_failed": count(adjust.get("failed", 0) + adjust.get("error", 0)),
        "adjust_rc": count(rc_adjust),
        "collected": count(len(cases)),
        "case_list": ", ".join(cases) or "-",
        "official_passed": count(official.get("passed", 0)),
        "official_skipped": count(official.get("skipped", 0)),
        "official_failed": count(official.get("failed", 0) + official.get("error", 0)),
        "official_rc": count(rc_official),
    }


def judge_ac2_03(facts: Facts) -> Verdict:
    """``AC-2|03``: an unadjusted-only Bar, and a synthesis meeting a recorded qfq series."""
    ok = (
        facts["bar"] == "passed"
        and facts["adjust_failed"] == "0"
        and int(facts["adjust_passed"]) >= 10
        and facts["official_failed"] == "0"
        and int(facts["official_passed"]) >= 2
    )
    readings = (
        f"Bar field-set node (no adjusted price may exist on the model) = {facts['bar']}",
        f"tests/test_adjust.py (synthesising from Bar + AdjustFactor) = {facts['adjust_passed']} "
        f"passed, {facts['adjust_failed']} failed (exit {facts['adjust_rc']})",
        f"official-series face: {facts['collected']} case(s) collected [{facts['case_list']}], "
        f"{facts['official_passed']} passed / {facts['official_skipped']} skipped / "
        f"{facts['official_failed']} failed (exit {facts['official_rc']})",
    )
    reason = (
        ""
        if ok
        else "a skipped fixture proves nothing: at least one collected official-qfq "
        "comparison has to run for the synthesis claim to have an outside witness"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac2_04(ctx: Context) -> Facts:
    """The three-stage base classes, and whether normalize() really carries the three jobs."""
    rel = "opendata/data/protocol.py"
    body = Path(REPO_ROOT / rel).read_text(encoding="utf-8")
    tree = ast.parse(body)
    classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    stages = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name in {"transform_query", "extract_data", "transform_data"}
    }
    mapping = ctx.read("opendata/data/mapping.py")
    normalize = function_body(mapping, "normalize_frame")
    nodes = (
        "tests/test_provider_registry.py::TestFetcherPipeline::test_fetch_runs_stages_in_order",
        "tests/test_provider_registry.py::TestFetcherPipeline::test_fetch_validates_query_params",
        "tests/test_data_mapping.py::TestNormalizeFrame::"
        "test_renames_converts_units_and_normalizes_keys",
        "tests/test_data_mapping.py::TestNormalizeFrame::test_missing_source_column_fails_closed",
        "tests/test_data_mapping.py::TestDenormalizeFrame::"
        "test_contract_rows_return_to_the_source_columns_with_units_undone",
    )
    outcomes = {node.split("::")[-1]: node_outcome(node) for node in nodes}
    return {
        "bases": ", ".join(sorted(classes & {"QueryParams", "Fetcher", "FetchContext"})),
        "capability": flag(
            "class Capability(BaseModel)" in ctx.read("opendata/data/capability.py")
        ),
        "stages": count(len(stages)),
        "rename": flag("source_column" in normalize),
        "units": flag(".scale" in normalize),
        "keynorm": flag('"plain"' in normalize or "_plain_value" in normalize),
        "runs": count(len(nodes)),
        "passed": count(sum(1 for v in outcomes.values() if v == "passed")),
        "bad": ", ".join(f"{k}={v}" for k, v in outcomes.items() if v != "passed") or "-",
    }


def judge_ac2_04(facts: Facts) -> Verdict:
    """``AC-2|04``: three stages exist and normalize() demonstrably maps, scales, normalizes."""
    ok = (
        facts["bases"].count(",") == 2
        and facts["capability"] == "yes"
        and facts["stages"] == "3"
        and facts["rename"] == "yes"
        and facts["units"] == "yes"
        and facts["keynorm"] == "yes"
        and facts["passed"] == facts["runs"]
    )
    readings = (
        f"protocol bases in opendata/data/protocol.py = {facts['bases']}; Capability model = "
        f"{facts['capability']}",
        f"Fetcher stage methods = {facts['stages']} "
        "(transform_query / extract_data / transform_data)",
        f"normalize_frame() bodies the three jobs itself: field mapping = {facts['rename']}, "
        f"unit conversion = {facts['units']}, key normalization = {facts['keynorm']}",
        f"named unit nodes = {facts['passed']}/{facts['runs']} passed; not green: {facts['bad']}",
    )
    reason = (
        ""
        if ok
        else "base classes, normalize()'s three jobs and their unit face must all read present"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac2_05(ctx: Context) -> Facts:
    """The registry file, its derivation tests, and a witness that no name is declared twice."""
    text = ctx.read("opendata/data/domains.yaml")
    block = text.split("domains:", 1)[1] if "domains:" in text else ""
    domains = re.findall(r"^  ([A-Za-z][\w-]*):\s*$", block, re.MULTILINE)
    rest_paths = re.findall(r"^    rest_path:\s*(\S+)", block, re.MULTILINE)
    contracts = re.findall(r"^    contract:\s*(\S+)", block, re.MULTILINE)
    declared = re.findall(r"(ods_\w+|dwd_\w+|data\.\w+)", text)
    rc, out = run_pytest(["tests/test_domains.py"])
    suite = tally(out)
    nodes = (
        "tests/test_domains.py::TestDerivations::test_ods_table",
        "tests/test_domains.py::TestDerivations::test_dwd_table",
        "tests/test_domains.py::TestDerivations::test_rest_path",
        "tests/test_domains.py::TestDerivations::test_ws_push_event",
        "tests/test_domains.py::TestRegistry::test_every_domain_derives_all_names",
    )
    outcomes = [node_outcome(node) for node in nodes]
    return {
        "domains": count(len(domains)),
        "rest": count(len(rest_paths)),
        "contracts": count(len(contracts)),
        "unique": flag(len(set(rest_paths)) == len(rest_paths)),
        "declared": count(len(declared)),
        "declared_sample": ", ".join(sorted(set(declared))[:6]),
        "suite_passed": count(suite.get("passed", 0)),
        "suite_failed": count(suite.get("failed", 0) + suite.get("error", 0)),
        "suite_rc": count(rc),
        "runs": count(len(nodes)),
        "derived_passed": count(outcomes.count("passed")),
    }


def judge_ac2_05(facts: Facts) -> Verdict:
    """``AC-2|05``: domains.yaml is the one source of table, REST and WS names."""
    ok = (
        int(facts["domains"]) >= 5
        and facts["rest"] == facts["domains"]
        and facts["contracts"] == facts["domains"]
        and facts["unique"] == "yes"
        and facts["declared"] == "0"
        and facts["suite_failed"] == "0"
        and facts["suite_rc"] == "0"
        and facts["derived_passed"] == facts["runs"]
    )
    readings = (
        f"domains.yaml declares {facts['domains']} domain(s): {facts['rest']} rest_path and "
        f"{facts['contracts']} contract entries, rest paths unique = {facts['unique']}",
        f"table/WS names written into the registry instead of derived = {facts['declared']}"
        + (f" ({facts['declared_sample']})" if facts["declared_sample"] else ""),
        f"tests/test_domains.py = {facts['suite_passed']} passed, {facts['suite_failed']} failed "
        f"(exit {facts['suite_rc']})",
        f"derivation nodes = {facts['derived_passed']}/{facts['runs']} passed",
    )
    reason = (
        ""
        if ok
        else "the registry, its three derivation faces and the tests that pin them must line up; a "
        "declared ods_/dwd_ literal in the yaml would mean a second source of the same name"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


# --------------------------------------------------------------------------- #
# AC-17 -- the quality gate judged on its own faces
# --------------------------------------------------------------------------- #

#: The two tools these items are *about*. Both are run the way the gate runs them rather than
#: reimplemented here: an AC-17 probe with its own copy of the rules would keep reporting green
#: after the tool it stands for drifted, which is the failure mode the item exists to prevent.
PUBLIC_API_TOOL: Final = "scripts/quality/public_api.py"
A2_CHECK_TOOL: Final = "scripts/quality/a2_check.py"
TRACEABILITY_TOOL: Final = "scripts/quality/evidence_traceability.py"

#: Where the traceability archive lives, and where its frozen ceiling is recorded.
EVIDENCE_DIR: Final = "docs/evidence"
TRACE_BASELINE_REL: Final = "docs/quality/evidence-traceability.json"

#: The faces ``evidence_traceability.py`` prints, in the order it prints them. Naming them here
#: means a face added to that tool has to be read here too, or ``--self-test`` stops being able
#: to satisfy this judge.
TRACE_FACES: Final = ("narrative", "date", "identity", "command", "exit", "untracked")

#: Trees that sit in the A2 file set but are nobody's *public API*: the test tree, whose
#: docstring and annotation rules ruff switches off by configuration (and which ``a2_check``
#: exempts from mypy and bandit), and the evidence archive, whose scripts are reproducibility
#: readings rather than shipped callables. Excluding them is a scope decision, so their size is
#: measured and printed alongside the judgement -- a face that drops a population silently is
#: hiding a number, not making a call.
NON_API_TREES: Final = ("tests/", "docs/")


@lru_cache(maxsize=8)
def script_module(rel: str) -> ModuleType:
    """Import one of the repository's own quality scripts by repository-relative path.

    Args:
        rel: Path to the script, relative to the repository root.

    Returns:
        The executed module.

    Raises:
        ProbeError: When the script is not there to be read.
    """
    path = REPO_ROOT / rel
    if not path.is_file():
        raise ProbeError(f"{rel} is missing")
    # ``sys.modules`` first, and only then exec: a ``@dataclass`` in the target resolves its own
    # class through the module registry during processing, so an unregistered module dies with
    # ``AttributeError: 'NoneType' object has no attribute '__dict__'`` -- a confusing way for a
    # probe to fail while the tool it loads is perfectly healthy.
    spec = importlib.util.spec_from_file_location(f"opendata_script_{rel}", path)
    if spec is None or spec.loader is None:
        raise ProbeError(f"{rel} cannot be loaded as a module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def under_any(rel: str, prefixes: Iterable[str]) -> bool:
    """Whether a repo-relative path equals one of the prefixes or sits below it."""
    return any(rel == entry or rel.startswith(f"{entry.rstrip('/')}/") for entry in prefixes)


def number(value: str) -> int:
    """A counted fact as an integer; anything that does not read as one counts as -1.

    Facts stay strings so a counterfact is one ``update()`` away, which means an unparseable
    reading -- a tool that renamed its own output line, say -- has to be judged rather than
    raised. -1 fails both :func:`positive` and any equality, so the verdict becomes a gap that
    names the face instead of a traceback that hides it.
    """
    try:
        return int(value)
    except ValueError:
        return -1


def positive(value: str) -> bool:
    """Whether a counted fact reads as at least one, so 100% of nothing cannot pass."""
    return number(value) > 0


def symbol_census(pub: ModuleType, paths: Sequence[str]) -> tuple[int, int, str]:
    """Walk public callables over the named files with the gate tool's own walker.

    Returns:
        ``(callables, failing, sample)`` -- the sample names up to six offending ``file:line``.
    """
    total = 0
    failing: list[str] = []
    for rel in paths:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for symbol in pub.collect_symbols(source, rel):
            total += 1
            if symbol.problems():
                failing.append(f"{rel}:{symbol.line}")
    return total, len(failing), ", ".join(failing[:6])


def evidence_deletions() -> tuple[str, str]:
    """Evidence archives git added at some point but no longer tracks, and what is tracked now.

    The face-by-face scan can only read files that are present, so an archive that disappears
    takes its command, date and exit readings with it in silence. ``--diff-filter=A`` over the
    whole history against ``git ls-files`` is the difference between "52 rounds archived" and
    "52 rounds archived, one of them since removed".
    """
    code, added_out = run_argv(
        ["git", "log", "--diff-filter=A", "--name-only", "--pretty=format:", "--", EVIDENCE_DIR]
    )
    if code != 0:
        raise ProbeError(f"git log over {EVIDENCE_DIR} failed: {added_out.strip()[:120]}")
    code, tracked_out = run_argv(["git", "ls-files", EVIDENCE_DIR])
    if code != 0:
        raise ProbeError(f"git ls-files over {EVIDENCE_DIR} failed: {tracked_out.strip()[:120]}")
    added = {line.strip() for line in added_out.splitlines() if line.strip()}
    tracked = {line.strip() for line in tracked_out.splitlines() if line.strip()}
    return count(len(added - tracked)), count(len(tracked))


# --------------------------------------------------------------------------- #
# AC-17|03 / AC-17|05 -- the two debt layers, judged on the planes that count them
# --------------------------------------------------------------------------- #

#: The members these two items are *about*, run as the gate runs them. ``ratchet.py`` prints all
#: five metrics in one pass, so each item reads its own faces out of that same run instead of
#: keeping a private copy of its arithmetic -- which is how a probe keeps telling 干净 after the
#: tool it stands for has been changed under it.
RATCHET_TOOL: Final = "scripts/quality/ratchet.py"
PORT_REPORT_TOOL: Final = "scripts/codemod/report_port.py"
PORT_MODULE_TOOL: Final = "scripts/codemod/port_module.py"

#: The frozen ceiling, the replay archive, the ported tree's own manifest, and the licence
#: document that is supposed to name every deviation inside it.
RATCHET_SNAPSHOT: Final = "docs/quality/ratchet.json"
PORT_REPORT_ARCHIVE: Final = "docs/port-report.md"
UPSTREAM_LOCK: Final = "opendata_http/upstream.lock"
NOTICES_DOC: Final = "THIRD_PARTY_NOTICES.md"
PRECOMMIT_CONFIG: Final = ".pre-commit-config.yaml"

#: The A1 metrics AC-17|03 owns (存量自研 debt) and the B-layer ones AC-17|05 owns. Split by item
#: so neither probe gets to borrow the other's green light.
SELFDEBT_METRICS: Final = ("ruff_selfdev", "mypy_selfdev", "bandit_selfdev")
PORTED_METRICS: Final = ("ruff_ported", "direct_http_ported")

#: The verbs ``requests`` exposes, i.e. what a "direct ``requests.<verb>()`` call site" can be.
#: ``ratchet.HTTP_VERBS`` must equal this set for ``direct_http_ported`` to mean anything.
PORTED_HTTP_VERBS: Final = frozenset({"get", "post", "put", "delete", "head", "patch", "request"})

#: A path under the ported tree, put to every hook in the pre-commit config: *would this hook have
#: been handed a ported file?* That is the question the item's ``exclude`` clause answers.
PORTED_PROBE_PATH: Final = "opendata_http/stock/cons.py"


def run_split(argv: Sequence[str]) -> tuple[int, str]:
    """Run a literal command and return ``(exit, stdout)``, keeping stderr out of the payload.

    :func:`run_argv` merges the two streams because a person reading a red tool wants both. The
    census helpers below parse ``--output-format=json``, where one progress line on stderr would
    read as a broken payload rather than a broken build.
    """
    try:
        proc = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
            list(argv),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
    except FileNotFoundError:
        raise ProbeError(f"(not installed): {argv[0]}") from None
    return proc.returncode, proc.stdout


def py_files_under(root: str) -> list[str]:
    """Repository-relative ``.py`` paths sitting on disk under one root, minus tool caches."""
    base = REPO_ROOT / root
    if not base.is_dir():
        return []
    return sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in base.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def ruff_walked(paths: Sequence[str]) -> set[str]:
    """The files ``ruff check`` itself decides to read, asked with its own ``--show-files``.

    Measuring the walk instead of the config is the whole point: ``[tool.ruff].exclude`` applies
    to directory walks, so re-adding an entry shrinks what ruff reads while every count on disk
    stays exactly where it was. C45 found ``alembic/`` and ``alembic_data/`` in that state -- ten
    touched first-party files that no plane read, under a ratchet whose file census still
    counted them.
    """
    code, out = run_split(
        [sys.executable, "-m", "ruff", "check", "--show-files", "--quiet", *paths]
    )
    if code not in (0, 1):
        raise ProbeError(f"ruff --show-files failed (exit {code}): {out.strip()[:120]}")
    walked: set[str] = set()
    for line in out.splitlines():
        name = line.strip()
        if not name.endswith(".py"):
            continue
        walked.add(str(Path(name).resolve().relative_to(REPO_ROOT)))
    return walked


def mypy_targets(paths: Sequence[str]) -> set[str]:
    """The files mypy itself takes as build targets, read out of its own verbose log.

    ``followed=False`` keeps the list to what the walk decided, rather than also counting every
    transitively imported module mypy then parsed.

    ``mypy -v`` writes its ``LOG:`` lines to stderr, so this one helper reads the merged stream
    that :func:`run_argv` returns instead of the stdout-only :func:`run_split`. An empty result
    therefore means the log shape moved rather than that mypy read nothing, and saying so out
    loud beats reporting a quiet gap over a 210-file denominator.
    """
    code, out = run_argv([sys.executable, "-m", "mypy", "-v", "--no-error-summary", *paths])
    if code not in (0, 1):
        raise ProbeError(f"mypy -v failed (exit {code}): {out.strip()[:120]}")
    targets = {
        match.group(1)
        for match in re.finditer(
            r"Found source:\s+BuildSource\(path='([^']+)'[^)]*followed=False\)", out
        )
    }
    if not targets:
        raise ProbeError("mypy -v produced no 'Found source' lines; the log shape moved")
    return targets


def bandit_scanned(paths: Sequence[str]) -> set[str]:
    """The files bandit itself reports metrics for -- its own record of what it opened."""
    code, out = run_split(
        [sys.executable, "-m", "bandit", "-c", "bandit.yaml", "-f", "json", "-q", "-r", *paths]
    )
    if code not in (0, 1):
        raise ProbeError(f"bandit failed (exit {code})")
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError(f"bandit produced unparsable output: {exc}") from exc
    metrics = payload.get("metrics") if isinstance(payload, dict) else None
    if not isinstance(metrics, dict):
        raise ProbeError("bandit output has no metrics section")
    return {str(key) for key in metrics if key != "_totals"}


def ruff_violations(args: Sequence[str]) -> int:
    """Count what one ``ruff check`` invocation finds, from its own JSON payload."""
    code, out = run_split([sys.executable, "-m", "ruff", "check", "--output-format=json", *args])
    if code not in (0, 1):
        raise ProbeError(f"ruff check failed (exit {code}): {out.strip()[:120]}")
    try:
        payload = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ruff produced unparsable output: {exc}") from exc
    return len(payload) if isinstance(payload, list) else -1


def ruff_reformat_hits(paths: Sequence[str]) -> int:
    """How many files ``ruff format`` says it would rewrite, counted from its own diff headers."""
    code, out = run_split([sys.executable, "-m", "ruff", "format", "--diff", *paths])
    if code not in (0, 1):
        raise ProbeError(f"ruff format --diff failed (exit {code})")
    return len({match.group(1) for match in re.finditer(r"^--- (\S+)", out, re.MULTILINE)})


def ratchet_face(out: str) -> str:
    """Which of the ratchet's own failure faces a run hit -- ``none`` when it was green."""
    if "OK: quality debt did not increase." in out:
        return "none"
    if "FAIL: scan scope changed silently" in out:
        return "scope"
    if "FAIL: quality debt increased" in out:
        return "debt"
    if "is missing or invalid" in out:
        return "snapshot"
    return "other"


def ratchet_pair(out: str, name: str) -> tuple[str, str]:
    """``(current, ceiling)`` for one metric, read off the line the ratchet prints for it."""
    found = re.search(rf"^\s+{name}: (\d+) \(snapshot (\d+)\)", out, re.MULTILINE)
    if not found:
        return "(absent)", "(absent)"
    return found.group(1), found.group(2)


def snapshot_at(rev: str) -> dict[str, object]:
    """The ratchet snapshot as one revision held it (``HEAD`` or a historical commit)."""
    code, out = run_argv(["git", "show", f"{rev}:{RATCHET_SNAPSHOT}"])
    if code != 0:
        raise ProbeError(f"{RATCHET_SNAPSHOT} is not readable at {rev}: {out.strip()[:120]}")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"{RATCHET_SNAPSHOT} at {rev} is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProbeError(f"{RATCHET_SNAPSHOT} at {rev} is not an object")
    return payload


def snapshot_revisions() -> list[str]:
    """Every commit that rewrote the ratchet snapshot, newest first.

    The ratchet compares the working tree against the snapshot, so the snapshot is one of the
    judges of 只降不升 rather than one of the things judged -- and a judge nobody reads the history
    of can be lifted in the very commit that pays debt down, which leaves every later round green
    over a tree that got worse. The revision list comes from ``git log`` rather than a fixed
    length, so the sequence check covers whatever the rounds appended.
    """
    code, out = run_argv(["git", "log", "--format=%H", "--", RATCHET_SNAPSHOT])
    if code != 0:
        raise ProbeError(f"git log over {RATCHET_SNAPSHOT} failed: {out.strip()[:120]}")
    revisions = [line.strip() for line in out.splitlines() if line.strip()]
    if not revisions:
        raise ProbeError(f"{RATCHET_SNAPSHOT} has no committed history to compare against")
    return revisions


def section_ints(payload: dict[str, object], key: str) -> dict[str, int]:
    """One integer section (``metrics`` or ``file_counts``) out of a snapshot payload."""
    section = payload.get(key)
    if not isinstance(section, dict):
        return {}
    return {str(name): int(value) for name, value in section.items() if isinstance(value, int)}


def metric_subset(payload: dict[str, object], names: Sequence[str]) -> dict[str, int]:
    """The ceilings one item owns, and nothing else.

    ``ruff_ported``/``direct_http_ported`` cover a tree that grew from 131 files to 313 during
    milestones A1--A2, so their ceilings moved with it; folding them into AC-17|03's 只降不升 check
    would judge the ported layer's growth as if first-party debt had gone up -- and the item's
    subject is 「A1 层（存量）」. Splitting by metric name also keeps the two items from borrowing
    each other's green light.
    """
    ints = section_ints(payload, "metrics")
    return {name: ints[name] for name in names if name in ints}


def snapshot_transitions(
    ctx: Context,
) -> list[tuple[str, str, dict[str, object], dict[str, object]]]:
    """The snapshot sequence as adjacent pairs, newest first, working tree included.

    The first pair compares the working tree with ``HEAD``; the rest walk the committed history.
    Keeping them as *pairs* rather than a flat list is the point: a ceiling that was lifted has to
    be attributable to the transition that lifted it, and a root that vanished has to be
    distinguishable from a root that was renamed in a documented controlled event.

    Returns:
        ``(newer_label, older_label, newer_payload, older_payload)`` per step down the history.
    """
    sequence: list[tuple[str, dict[str, object]]] = [
        ("工作区", json.loads(ctx.read(RATCHET_SNAPSHOT)))
    ]
    sequence += [(revision[:7], snapshot_at(revision)) for revision in snapshot_revisions()]
    return [
        (sequence[index][0], sequence[index + 1][0], sequence[index][1], sequence[index + 1][1])
        for index in range(len(sequence) - 1)
    ]


def census_of(payload: dict[str, object], roots: Sequence[str]) -> int:
    """How many files a snapshot says were scanned under ``roots`` -- the growth a raise needs."""
    counts = section_ints(payload, "file_counts")
    return sum(counts.get(root, 0) for root in roots)


def ceilings_raised(newer: dict[str, int], older: dict[str, int]) -> dict[str, str]:
    """Which ceilings went *up* between two snapshots -- the laundering this item forbids.

    Nothing compared the snapshot against its own history: ``ratchet.py`` only asks whether the
    working tree beats the frozen number, so a commit that pays 20 violations down while lifting
    the ceiling by 30 reads as improvement forever afterwards. Each returned entry is rendered
    ``name: old->new`` so a reading can name the transition instead of just counting it.
    """
    return {
        name: f"{older.get(name, 0)}->{value}"
        for name, value in newer.items()
        if value > older.get(name, 0)
    }


def scope_vanished(newer: dict[str, int], older: dict[str, int]) -> tuple[str, ...]:
    """Measured packages that disappeared from the snapshot's own census list."""
    return tuple(sorted(set(older) - set(newer)))


#: How many snapshot transitions count as "this round's": the working tree against HEAD, and
#: HEAD against the snapshot before it. The scope face is judged over those only -- the ported
#: root was renamed akshare -> opendata_http in milestone A2, which is a recorded controlled
#: event and not something a later round can un-vanish.
RECENT_TRANSITIONS: Final = 2


def detail_of(label: str, detail: str) -> str:
    """``label（detail）`` when there is something to list, and the bare label when there is not.

    A face that measures zero should not print an empty parenthesis; a face that measures
    non-zero has to name the offenders, because "抬高上限的次数 = 1" on its own tells a reader
    nothing they can act on.
    """
    return f"{label}（{detail}）" if detail else label


def ceiling_history(ctx: Context, names: Sequence[str], roots: Sequence[str]) -> Facts:
    """Read the snapshot's own history: which ceilings moved, and whether growth explains it.

    A ceiling raise is only *explainable* by more files under the roots that produce it, so the
    two are compared per transition and a raise that leaves the census flat is listed separately
    as a review face. A flat-census raise is not automatically laundering -- ``opendata_http/
    __init__.py`` growing 15 flat-export lines raised ``ruff_ported`` under ``--select E,F``
    because F401 counts them, without changing the file census -- so this function only reports,
    and each item judges the claim its own text makes.

    Returns:
        Facts for the sequence length, the raises (all, and the flat-census ones), the vanished
        roots split by whether they are recent, and the oldest-to-newest ceiling trend.
    """
    steps = snapshot_transitions(ctx)
    if not steps:
        raise ProbeError(f"{RATCHET_SNAPSHOT} has no transitions to walk")

    raised: list[str] = []
    flat_raises: list[str] = []
    vanished: list[list[str]] = [[], []]
    for index, (new_label, old_label, newer, older) in enumerate(steps):
        flat_census = census_of(newer, roots) == census_of(older, roots)
        moves = ceilings_raised(
            metric_subset(newer, names),
            metric_subset(older, names),
        )
        for name, detail in sorted(moves.items()):
            entry = f"{old_label}->{new_label} {name} {detail}"
            raised.append(entry)
            if flat_census:
                flat_raises.append(entry)
        gone = scope_vanished(
            section_ints(newer, "file_counts"),
            section_ints(older, "file_counts"),
        )
        bucket = vanished[0] if index < RECENT_TRANSITIONS else vanished[1]
        bucket += [f"{old_label}->{new_label} {root}" for root in gone]

    oldest, newest = steps[-1][3], steps[0][2]

    def _at(payload: dict[str, object], name: str) -> int:
        return metric_subset(payload, names).get(name, 0)

    return {
        "hist_steps": count(len(steps)),
        "raises": count(len(raised)),
        "raise_detail": "; ".join(raised),
        "flat_raises": count(len(flat_raises)),
        "flat_raise_detail": "; ".join(flat_raises),
        "vanish_recent": count(len(vanished[0])),
        "vanish_recent_detail": "; ".join(vanished[0]),
        "vanish_older": count(len(vanished[1])),
        "vanish_older_detail": "; ".join(vanished[1]),
        "trend": " / ".join(f"{name} {_at(oldest, name)}→{_at(newest, name)}" for name in names),
    }


def precommit_exclusions(source: str) -> tuple[int, int, set[str]]:
    """Every hook the pre-commit config declares, and which of them exclude the ported tree.

    ``exclude`` is applied the way pre-commit documents it -- a regular expression *searched*
    against the repository-relative filename -- because the binary is not installed in the
    environment this item is judged in, and a probe that needs a tool the gate does not run would
    fail on machines that are otherwise fine. No hook's own behaviour is re-implemented here: only
    the exclusion filter is read, and whether that filter is load-bearing is measured against
    ``ruff format`` itself (see :func:`measure_ac17_05`).

    Returns:
        ``(hook count, excluded hook count, excluded hook ids)``.
    """
    config = yaml.safe_load(source)
    repos = config.get("repos", []) if isinstance(config, dict) else []
    total = 0
    excluded: set[str] = set()
    for repo in repos if isinstance(repos, list) else []:
        hooks = repo.get("hooks", []) if isinstance(repo, dict) else []
        for hook in hooks if isinstance(hooks, list) else []:
            if not isinstance(hook, dict) or not hook.get("id"):
                continue
            total += 1
            pattern = str(hook.get("exclude", ""))
            if pattern and re.search(pattern, PORTED_PROBE_PATH):
                excluded.add(str(hook["id"]))
    return total, len(excluded), excluded


def port_replay() -> tuple[Facts, str]:
    """Run the codemod replay and read the report it renders, without touching the archive.

    ``report_port.py`` compares each ported file against ``port_source(pristine upstream bytes)``
    -- the deterministic replay of the same codemod, with the registered manual edits applied --
    and prints 重放一致 per file. A ``ruff format`` pass, an isort run and an unregistered hand
    edit therefore all land in one reading, which is what 「未做 format/isort 重排（与上游可
    diff）」 needs to be falsifiable. The report is rendered into a temporary directory so the
    measurement cannot rewrite the tracked ``docs/port-report.md`` it compares against later.
    """
    workdir = Path(tempfile.mkdtemp(prefix="acceptance-port-report-"))
    rendered = workdir / "port-report.md"
    try:
        code, out = run_argv([sys.executable, PORT_REPORT_TOOL, "--report-path", str(rendered)])
        text = rendered.read_text(encoding="utf-8") if rendered.is_file() else ""
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    header = re.search(r"wrote .* \((\d+) files, (\d+) pending TODO\(s\)\)", out)
    rows = [line for line in text.splitlines() if line.startswith("| `")]
    return (
        {
            "report_exit": count(code),
            "report_files": header.group(1) if header else "(absent)",
            "report_todos": header.group(2) if header else "(absent)",
            "rows_total": count(len(rows)),
            "rows_replay_ok": count(sum(1 for line in rows if line.rstrip().endswith("✓ |"))),
            "rows_manual": count(sum(1 for line in rows if "| True |" in line)),
        },
        text,
    )


def lock_records(ctx: Context) -> list[dict[str, object]]:
    """The per-file rows of ``opendata_http/upstream.lock`` (paths relative to the ported root)."""
    payload = json.loads(ctx.read(UPSTREAM_LOCK))
    entries = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ProbeError(f"{UPSTREAM_LOCK} has no files list")
    return [entry for entry in entries if isinstance(entry, dict)]


def registered_hand_edits(ctx: Context, pm: ModuleType) -> tuple[int, int, int, int]:
    """The three registers of ported-tree hand edits, and how far apart they are.

    ``THIRD_PARTY_NOTICES.md`` claims every manual edit is in ``port_module.MANUAL_EDITS`` *and*
    flagged in ``upstream.lock``; a replay reproduces the file only from the first, and a reader
    only sees the second. Two of the three going stale is invisible unless the three are compared.

    Returns:
        ``(lock count, notices count, codemod count, paths not agreed on by all three)``.
    """
    lock = {str(entry.get("path")) for entry in lock_records(ctx) if entry.get("manual_edits")}
    codemod = {
        str(edit.upstream_path).removeprefix("akshare/") for edit in getattr(pm, "MANUAL_EDITS", ())
    }
    lines = ctx.read(NOTICES_DOC).splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.startswith("### 人工改动登记")), None
    )
    if start is None:
        raise ProbeError(f"{NOTICES_DOC} has no 人工改动登记 section to read")
    notices: set[str] = set()
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        row = re.match(r"^\| `(akshare/[^`]+\.py)` \|", line)
        if row:
            notices.add(row.group(1).removeprefix("akshare/"))
    agreed = lock & codemod & notices
    return len(lock), len(notices), len(codemod), len((lock | codemod | notices) - agreed)


def measure_ac17_03(ctx: Context) -> Facts:
    """Run the ratchet and the A2 gate, then ask every static plane what *it* decided to read."""
    ratchet = script_module(RATCHET_TOOL)
    a2 = script_module(A2_CHECK_TOOL)
    code, out = run_argv([sys.executable, RATCHET_TOOL])
    facts: Facts = {"ratchet_exit": count(code), "ratchet_face": ratchet_face(out)}
    for name in SELFDEBT_METRICS:
        current, ceiling = ratchet_pair(out, name)
        facts[f"cur_{name}"] = current
        facts[f"snap_{name}"] = ceiling
    facts["printed"] = count(
        sum(1 for name in SELFDEBT_METRICS if facts[f"cur_{name}"] != "(absent)")
    )

    selfdev = tuple(ratchet.SELFDEV_PATHS)
    ported = tuple(ratchet.PORTED_PATHS)
    mypy_paths = tuple(ratchet.MYPY_PATHS)
    bandit_paths = tuple(ratchet.BANDIT_PATHS)
    #: Prefixes for path membership, so a root cannot be matched by a sibling that merely starts
    #: with the same characters (``scripts`` vs a hypothetical ``scripts_legacy``).
    measured = tuple(f"{root}/" for root in (*selfdev, *ported))

    disk_selfdev = {path for root in selfdev for path in py_files_under(root)}
    walked = ruff_walked(selfdev)
    disk_mypy = {path for root in mypy_paths for path in py_files_under(root)}
    targets = mypy_targets(mypy_paths)
    disk_bandit = {path for root in bandit_paths for path in py_files_under(root)}
    scanned = bandit_scanned(bandit_paths)

    baseline_error = a2.BaselineError
    try:
        base = a2._checked_baseline()
        touched = a2.changed_files(base) if base else []
        a2_files = a2.resolve_files(None) or []
    except baseline_error as exc:
        raise ProbeError(str(exc)) from exc
    a2_code, _a2_out = run_argv([sys.executable, A2_CHECK_TOOL])
    a2_set = set(a2_files)
    py_touched = [name for name in touched if name.endswith(".py")]
    dropped = [name for name in py_touched if name not in a2_set]
    #: The gate's own list of "not first-party" roots. A touched file that falls out of A2 for
    #: any *other* reason is the 触碰即达标 rule failing open, which is what this face counts.
    excluded_roots = tuple(f"{name}/" for name in sorted(a2.EXCLUDED_ROOT_DIRS))
    dropped_selfdev = [name for name in dropped if not under_any(name, excluded_roots)]

    tracked_py = [name for name in ctx.tracked() if name.endswith(".py")]
    orphan = [name for name in tracked_py if not under_any(name, measured) and name not in a2_set]

    facts.update(ceiling_history(ctx, SELFDEBT_METRICS, selfdev))
    facts.update(
        {
            "plane_roots": count(len(selfdev)),
            "selfdev_disk": count(len(disk_selfdev)),
            "ruff_walked": count(len(walked & disk_selfdev)),
            "ruff_dark": count(len(disk_selfdev - walked)),
            "mypy_disk": count(len(disk_mypy)),
            "mypy_targets": count(len(targets & disk_mypy)),
            "mypy_dark": count(len(disk_mypy - targets)),
            "mypy_dark_seen_by_ruff": count(len((disk_mypy - targets) & walked)),
            "mypy_legacy_zone": ", ".join(str(name) for name in ratchet.MYPY_LEGACY_ZONE),
            #: The type plane may only drop what the ratchet *names* as its legacy zone. Any other
            #: dark file is an exclude entry nobody declared, and an undeclared exclude can move
            #: ``mypy_selfdev`` on its own -- which is the laundering this census exists to catch.
            "mypy_dark_legacy": count(
                sum(
                    1
                    for name in disk_mypy - targets
                    if under_any(name, tuple(ratchet.MYPY_LEGACY_ZONE))
                )
            ),
            "mypy_dark_outside_legacy": count(
                sum(
                    1
                    for name in disk_mypy - targets
                    if not under_any(name, tuple(ratchet.MYPY_LEGACY_ZONE))
                )
            ),
            "mypy_dark_outside_sample": ", ".join(
                sorted(
                    name
                    for name in disk_mypy - targets
                    if not under_any(name, tuple(ratchet.MYPY_LEGACY_ZONE))
                )[:4]
            ),
            "bandit_disk": count(len(disk_bandit)),
            "bandit_scanned": count(len(scanned & disk_bandit)),
            "bandit_dark": count(len(disk_bandit - scanned)),
            "bandit_dark_seen_by_ruff": count(len((disk_bandit - scanned) & walked)),
            "tracked_py": count(len(tracked_py)),
            "orphan_py": count(len(orphan)),
            "orphan_sample": ", ".join(orphan[:6]),
            "a2_exit": count(a2_code),
            "a2_files": count(len(a2_files)),
            "touched_py": count(len(py_touched)),
            "touched_dropped": count(len(dropped)),
            "touched_dropped_selfdev": count(len(dropped_selfdev)),
            "touched_dropped_sample": ", ".join(dropped_selfdev[:6]),
        }
    )
    return facts


def judge_ac17_03(facts: Facts) -> Verdict:
    """``AC-17|03``: the ceiling only moves down, and every touched first-party file meets A2."""
    metrics_ok = all(
        0 <= number(facts[f"cur_{name}"]) <= number(facts[f"snap_{name}"])
        for name in SELFDEBT_METRICS
    )
    ceiling_ok = (
        number(facts["hist_steps"]) >= RECENT_TRANSITIONS
        and facts["raises"] == "0"
        and facts["vanish_recent"] == "0"
    )
    census_ok = (
        facts["ruff_dark"] == "0"
        and positive(facts["ruff_walked"])
        and facts["mypy_dark"] == facts["mypy_dark_seen_by_ruff"]
        and positive(facts["mypy_targets"])
        and facts["mypy_dark_outside_legacy"] == "0"
        and facts["bandit_dark"] == "0"
        and positive(facts["bandit_scanned"])
        and facts["orphan_py"] == "0"
        and positive(facts["tracked_py"])
    )
    touched_ok = (
        facts["a2_exit"] == "0"
        and positive(facts["a2_files"])
        and positive(facts["touched_py"])
        and facts["touched_dropped_selfdev"] == "0"
    )
    ok = (
        facts["ratchet_exit"] == "0"
        and facts["ratchet_face"] == "none"
        and facts["printed"] == count(len(SELFDEBT_METRICS))
        and metrics_ok
        and ceiling_ok
        and census_ok
        and touched_ok
    )
    debt = ", ".join(
        f"{name} {facts[f'cur_{name}']}(≤{facts[f'snap_{name}']})" for name in SELFDEBT_METRICS
    )
    bandit_both_dark = number(facts["bandit_dark"]) - number(facts["bandit_dark_seen_by_ruff"])
    readings = (
        f"{RATCHET_TOOL} exit {facts['ratchet_exit']}（face={facts['ratchet_face']}，"
        f"printed={facts['printed']}/{len(SELFDEBT_METRICS)}）：存量债务 {debt}",
        detail_of(
            f"上限自己的历史（{facts['hist_steps']} 次转换）：抬高存量上限的次数 "
            f"{facts['raises']}；近 {RECENT_TRANSITIONS} 次转换里消失的被测根 "
            f"{facts['vanish_recent']}，更早历史 {facts['vanish_older']}；"
            f"存量上限轨迹 {facts['trend']}。棘轮只比较工作区与快照，"
            "抬高上限这一步此前无人计量",
            "; ".join(
                listing
                for listing in (
                    facts["raise_detail"],
                    facts["vanish_recent_detail"],
                    facts["vanish_older_detail"],
                )
                if listing
            ),
        ),
        f"平面可见性：{facts['plane_roots']} 个自研根磁盘 {facts['selfdev_disk']} 个 .py，"
        f"ruff 自己走到 {facts['ruff_walked']}（看不见 {facts['ruff_dark']}）；mypy 目标 "
        f"{facts['mypy_targets']}/{facts['mypy_disk']}，被它排除的 {facts['mypy_dark']} 个里 "
        f"{facts['mypy_dark_legacy']} 个在棘轮点名的遗留区（{facts['mypy_legacy_zone']}）之内、"
        f"{facts['mypy_dark_outside_legacy']} 个是没人声明的 exclude（这 {facts['mypy_dark']} 个"
        f"仍全部被 ruff 读到）；bandit {facts['bandit_scanned']}/{facts['bandit_disk']}，"
        f"被它漏掉的 {facts['bandit_dark']} 个（ruff 也看不见的 {bandit_both_dark}）必须为 0："
        "三个平面各自把一份存量计数，exclude 吞掉的根既不产生读数也不产生红灯，"
        "所以谁被排除必须点名，不能只在配置文件里悄悄加一行",
        detail_of(
            f"全体 tracked .py = {facts['tracked_py']}，既不在任何被测根下、也不在 A2 集里的 "
            f"{facts['orphan_py']}",
            facts["orphan_sample"],
        ),
        detail_of(
            f"触碰即达标：自基线起改动 {facts['touched_py']} 个 .py ↔ A2 集 {facts['a2_files']} 个"
            f"（a2-check exit {facts['a2_exit']}），其中按名字被排除出 A2 的 "
            f"{facts['touched_dropped']} 个、排除项里的自研代码 "
            f"{facts['touched_dropped_selfdev']} 个",
            facts["touched_dropped_sample"],
        ),
    )
    reason = (
        ""
        if ok
        else "「只降不升」要求被量的范围本身也只降不升：抬高过一次上限、让一个被测根从快照里消失、"
        "或把一个触碰过的自研文件按名字排除在 A2 零容忍之外，都会让「债务不高于快照」成立而代码"
        "一行没改；平面可见性同理——exclude 吞掉的根不产生任何读数，也就不会产生红灯"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac17_05(ctx: Context) -> Facts:
    """Replay the port, then ask ruff what the ported tree would cost without its exemption."""
    ratchet = script_module(RATCHET_TOOL)
    pm = script_module(PORT_MODULE_TOOL)
    ported = tuple(ratchet.PORTED_PATHS)
    root = ported[0]

    code, out = run_argv([sys.executable, RATCHET_TOOL])
    facts: Facts = {"ratchet_exit": count(code), "ratchet_face": ratchet_face(out)}
    for name in PORTED_METRICS:
        current, ceiling = ratchet_pair(out, name)
        facts[f"cur_{name}"] = current
        facts[f"snap_{name}"] = ceiling
    facts["printed"] = count(
        sum(1 for name in PORTED_METRICS if facts[f"cur_{name}"] != "(absent)")
    )
    facts.update(ceiling_history(ctx, PORTED_METRICS, ported))

    def _debt(select: str | None) -> int:
        """One ruff pass over the ported tree, counted by the ratchet's own function."""
        try:
            return int(ratchet.count_ruff(ported, select=select))
        except ratchet.ToolError as exc:
            raise ProbeError(str(exc)) from exc

    ef = _debt("E,F")
    project = _debt(None)
    facts.update(
        {
            "ef_violations": count(ef),
            "project_violations": count(project),
            "isort_violations": count(_debt("I")),
            "format_would_rewrite": count(ruff_reformat_hits([root])),
            "disk_py": count(len(py_files_under(root))),
            "lock_records": count(len(lock_records(ctx))),
            "select_matches": "yes" if facts["cur_ruff_ported"] == count(ef) else "no",
            "exemption_shown": "yes" if project > ef else "no",
        }
    )
    facts["http_matches"] = (
        "yes" if facts["cur_direct_http_ported"] == count(ratchet.count_direct_http(root)) else "no"
    )
    #: ``count_direct_http`` only counts the verbs it is told about, so dropping one from
    #: ``HTTP_VERBS`` lowers ``direct_http_ported`` with no code change -- the same laundering
    #: device the ruff ``select`` faces, and the behaviour test above cannot see it because both
    #: readings call the same function. The verb list itself is therefore checked against the
    #: seven verbs ``requests`` exposes.
    verbs = sorted(str(verb) for verb in ratchet.HTTP_VERBS)
    facts["verbs_declared"] = ", ".join(verbs)
    facts["verbs_ok"] = "yes" if frozenset(verbs) == PORTED_HTTP_VERBS else "no"

    replay, rendered = port_replay()
    facts.update(replay)
    facts["render_matches_archive"] = (
        "yes" if rendered and rendered == ctx.read(PORT_REPORT_ARCHIVE) else "no"
    )
    recorded = {f"{root}/{entry.get('path')}" for entry in lock_records(ctx)}
    unrecorded = sorted(set(py_files_under(root)) - recorded)
    facts["unrecorded_py"] = count(len(unrecorded))
    facts["unrecorded_sample"] = ", ".join(unrecorded[:4])

    code, head_config = run_argv(["git", "show", f"HEAD:{PRECOMMIT_CONFIG}"])
    if code != 0:
        raise ProbeError(f"{PRECOMMIT_CONFIG} is not readable at HEAD: {head_config[:120]}")
    total, excluded, ids = precommit_exclusions(ctx.read(PRECOMMIT_CONFIG))
    head_total, _head_count, head_ids = precommit_exclusions(head_config)
    lost = sorted(head_ids - ids)
    facts.update(
        {
            "hooks_total": count(total),
            "hooks_total_head": count(head_total),
            "hooks_excluding": count(excluded),
            "hook_ids": ", ".join(sorted(ids)),
            "hooks_lost": count(len(lost)),
            "hooks_lost_detail": ", ".join(lost),
        }
    )
    lock_n, notices_n, codemod_n, disagree = registered_hand_edits(ctx, pm)
    facts.update(
        {
            "edits_in_lock": count(lock_n),
            "edits_in_notices": count(notices_n),
            "edits_in_codemod": count(codemod_n),
            "edits_disagree": count(disagree),
        }
    )
    return facts


def judge_ac17_05(facts: Facts) -> Verdict:
    """``AC-17|05``: the ported tree is E/F-only, unformatted and replayable, and says so."""
    debt_ok = (
        facts["ratchet_exit"] == "0"
        and facts["ratchet_face"] == "none"
        and facts["printed"] == count(len(PORTED_METRICS))
        and facts["select_matches"] == "yes"
        and facts["http_matches"] == "yes"
        and facts["verbs_ok"] == "yes"
        and all(
            0 <= number(facts[f"cur_{name}"]) <= number(facts[f"snap_{name}"])
            for name in PORTED_METRICS
        )
    )
    diffable_ok = (
        facts["report_exit"] == "0"
        and facts["report_todos"] == "0"
        and positive(facts["rows_total"])
        and facts["rows_replay_ok"] == facts["rows_total"]
        and facts["rows_total"] == facts["lock_records"]
        and facts["report_files"] == facts["rows_total"]
        and facts["unrecorded_py"] == "0"
        and facts["render_matches_archive"] == "yes"
        and facts["rows_manual"] == facts["edits_in_lock"]
        and facts["edits_disagree"] == "0"
    )
    exemption_ok = (
        facts["exemption_shown"] == "yes"
        and positive(facts["isort_violations"])
        and positive(facts["format_would_rewrite"])
    )
    hooks_ok = (
        positive(facts["hooks_excluding"])
        and facts["hooks_lost"] == "0"
        and number(facts["hooks_excluding"]) <= number(facts["hooks_total"])
    )
    scope_ok = facts["vanish_recent"] == "0" and number(facts["hist_steps"]) >= 2
    ok = debt_ok and diffable_ok and exemption_ok and hooks_ok and scope_ok
    readings = (
        f"{RATCHET_TOOL} exit {facts['ratchet_exit']}（face={facts['ratchet_face']}）："
        f"搬运债务 "
        + ", ".join(
            f"{name} {facts[f'cur_{name}']}(≤{facts[f'snap_{name}']})" for name in PORTED_METRICS
        )
        + f"；门禁打印的 ruff_ported 与独立跑的 --select E,F 计数一致 = "
        f"{facts['select_matches']}（E/F {facts['ef_violations']}）；直连计数的动词集合 "
        f"{facts['verbs_declared']} 完整 = {facts['verbs_ok']}",
        f"「仅 E/F」豁免挡住的量：同一棵树按自研规则集跑是 {facts['project_violations']} 条、"
        f"按 I 规则 {facts['isort_violations']} 条，E/F 只有 {facts['ef_violations']} 条 —— "
        "豁免若不存在，门禁当场全红，所以它必须被登记而不是被假装没有",
        f"「未做 format/isort 重排」：{facts['format_would_rewrite']} 个文件是 "
        f"``ruff format`` 会重写的、{facts['isort_violations']} 条 import 顺序违例 —— "
        "重排过一次的树不可能与上游逐行 diff",
        detail_of(
            f"重放对账（{PORT_REPORT_TOOL}，需本机有锁定 commit 的上游干净克隆）："
            f"exit {facts['report_exit']}，锁内 {facts['lock_records']} 条记录 / 报告 "
            f"{facts['rows_total']} 行 / 逐字一致 {facts['rows_replay_ok']} 行 / "
            f"待办 {facts['report_todos']} 条；磁盘 {facts['disk_py']} 个 .py，"
            f"其中未登记的 {facts['unrecorded_py']} 个；"
            f"现场渲染与已归档的 {PORT_REPORT_ARCHIVE} 逐字节一致 = "
            f"{facts['render_matches_archive']}",
            facts["unrecorded_sample"],
        ),
        f"人工改动三处登记：{UPSTREAM_LOCK} {facts['edits_in_lock']} 条、"
        f"{NOTICES_DOC} {facts['edits_in_notices']} 条、"
        f"{PORT_MODULE_TOOL}:MANUAL_EDITS {facts['edits_in_codemod']} 条，"
        f"不一致 {facts['edits_disagree']} 条（重放只认 MANUAL_EDITS，读者只看到登记文档）",
        detail_of(
            f"{PRECOMMIT_CONFIG}：共 {facts['hooks_total']} 个 hook（HEAD "
            f"{facts['hooks_total_head']} 个），其中排除搬运树的 {facts['hooks_excluding']} 个，"
            f"比 HEAD 少的 {facts['hooks_lost']} 个；排除的 hook id：{facts['hook_ids']}",
            facts["hooks_lost_detail"],
        ),
        detail_of(
            f"搬运上限历史（{facts['hist_steps']} 次转换）：抬高 {facts['raises']} 次、"
            f"其中搬运文件数未变的 {facts['flat_raises']} 次；近 "
            f"{RECENT_TRANSITIONS} 次转换消失的被测根 {facts['vanish_recent']}。"
            f"判据原文只要求「债务不高于棘轮快照」，故抬高只作待复核登记；轨迹 "
            f"{facts['trend']}",
            "; ".join(
                listing
                for listing in (
                    facts["flat_raise_detail"],
                    facts["vanish_older_detail"],
                )
                if listing
            ),
        ),
    )
    reason = (
        ""
        if ok
        else "搬运层要同时满足三件事：只按 E/F 计量、与上游还能逐行 diff、以及这两件事都写在纸上。"
        "重放一致说明没有被偷偷重排或手改；pre-commit 的 exclude 说明下一次 hook 运行不会把它"
        "重排掉；三处登记一致说明读者看到的和工具认的是同一批改动"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac17_07(ctx: Context) -> Facts:
    """Run the gate's public-API check, then apply its own rules to new files that it misses."""
    pub = script_module(PUBLIC_API_TOOL)
    a2 = script_module(A2_CHECK_TOOL)
    code, out = run_argv([sys.executable, PUBLIC_API_TOOL])
    scope = tuple(str(entry) for entry in pub.A2_SCOPE)
    a2_files = sorted(a2.resolve_files(None) or [])
    a2_set = set(a2_files)
    ported = tuple(f"{name}/" for name in sorted(a2.EXCLUDED_ROOT_DIRS))
    outside = [name for name in a2_files if not under_any(name, scope)]
    widened = [name for name in outside if not under_any(name, NON_API_TREES)]
    non_api = [name for name in outside if under_any(name, NON_API_TREES)]
    legacy = [
        name
        for name in ctx.tracked()
        if name.endswith(".py")
        and name not in a2_set
        and not under_any(name, scope)
        and not under_any(name, NON_API_TREES)
        and not under_any(name, ported)
    ]
    wide_total, wide_fail, wide_sample = symbol_census(pub, widened)
    legacy_total, legacy_fail, legacy_sample = symbol_census(pub, legacy)
    non_total, non_fail, _ = symbol_census(pub, non_api)
    return {
        "tool_exit": count(code),
        "in_scope": first_capture(out, r"A2 public callables\s*:\s*(\d+)"),
        "doc_pct": first_capture(out, r"docstring coverage\s*:\s*([\d.]+)%"),
        "ann_pct": first_capture(out, r"annotation coverage\s*:\s*([\d.]+)%"),
        "vacuous": flag("no A2 public callables in scope" in out),
        "scope_dirs": count(sum(1 for entry in scope if (REPO_ROOT / entry).is_dir())),
        "widened_files": count(len(widened)),
        "widened_callables": count(wide_total),
        "widened_failing": count(wide_fail),
        "widened_sample": wide_sample,
        "legacy_files": count(len(legacy)),
        "legacy_callables": count(legacy_total),
        "legacy_failing": count(legacy_fail),
        "legacy_sample": legacy_sample,
        "non_api_callables": count(non_total),
        "non_api_failing": count(non_fail),
    }


def judge_ac17_07(facts: Facts) -> Verdict:
    """``AC-17|07``: new public callables are documented and fully annotated, on both faces."""
    ok = (
        facts["tool_exit"] == "0"
        and facts["vacuous"] == "no"
        and facts["doc_pct"] == "100.0"
        and facts["ann_pct"] == "100.0"
        and positive(facts["in_scope"])
        and positive(facts["widened_callables"])
        and facts["widened_failing"] == "0"
    )
    readings = (
        f"{PUBLIC_API_TOOL} exit {facts['tool_exit']}: {facts['in_scope']} callable(s) over "
        f"{facts['scope_dirs']} of its fixed scope director(ies) exist; docstring "
        f"{facts['doc_pct']}%, annotation {facts['ann_pct']}%; empty-scope branch taken = "
        f"{facts['vacuous']}",
        f"新增面 outside that fixed list (A2 files that are not tests or evidence) = "
        f"{facts['widened_files']} file(s) / {facts['widened_callables']} callable(s), below "
        f"standard = {facts['widened_failing']}"
        + (f" ({facts['widened_sample']})" if facts["widened_sample"] else ""),
        f"A1 存量 outside the fixed list, gated by 触碰即达标 rather than by this item = "
        f"{facts['legacy_files']} file(s) / {facts['legacy_callables']} callable(s), below "
        f"standard = {facts['legacy_failing']}"
        + (f" ({facts['legacy_sample']})" if facts["legacy_sample"] else ""),
        f"excluded test/evidence trees, disclosed rather than dropped = "
        f"{facts['non_api_callables']} callable(s), below standard = {facts['non_api_failing']}",
    )
    reason = (
        ""
        if ok
        else "the item says 新增: a callable the gate cannot see because its scope list is fixed "
        "still has to meet 100%/100%, and a 100% over an empty or unreachable population is not "
        "a measurement at all"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


#: The shell census this item is about. Run as the gate would run it, for the same reason the
#: two tools above are: a probe that re-implemented the four rules would keep saying 干净 after
#: the census grew a fifth rule.
SHELL_AUDIT_TOOL: Final = "docs/evidence/C44/shell_audit.py"

#: The four 空壳 forms §5.1 names, and the reading each one is printed under.
SHELL_FACES: Final = (
    ("self-reference", "自指"),
    ("vacuous-assert", "恒真断言"),
    ("constant-shell", "定义抄写"),
    ("source-form-check", "源码形式检查"),
)

#: §5.2's T1 floors: ①错误翻译 ≥3 且含成功不抛、②黄金向量有钉值、③normalize ≥3。
T1_FLOORS: Final = (("t1_error_cases", 3), ("t1_success_cases", 1), ("t1_normalize_cases", 3))


def measure_ac17_08(ctx: Context) -> Facts:
    """Run the shell census and re-read §5.1/§5.2 off its own printed faces."""
    code, out = run_argv([sys.executable, SHELL_AUDIT_TOOL])
    on_disk = sorted(
        path.relative_to(REPO_ROOT).as_posix() for path in (REPO_ROOT / "tests").rglob("test_*.py")
    )
    tracked = set(ctx.tracked())
    facts: Facts = {
        "tool_exit": count(code),
        "tool_shells": first_capture(out, r"shells=(\w+)"),
        "tool_t1": first_capture(out, r"t1=(\w+)"),
        "population": count(len(on_disk)),
        "untracked_tests": count(sum(1 for name in on_disk if name not in tracked)),
        "fixture_cases": first_capture(out, r"^fixture_cases = (\S+)$"),
        "fixture_sha_ok": first_capture(out, r"^fixture_sha_ok = (\S+)$"),
        "provenance": first_capture(out, r"^provenance = (\S+)$"),
        "t1_error_cases": first_capture(out, r"^t1_error_cases = (\S+)$"),
        "t1_success_cases": first_capture(out, r"^t1_success_cases = (\S+)$"),
        "t1_normalize_cases": first_capture(out, r"^t1_normalize_cases = (\S+)$"),
        "t1_golden_days": first_capture(out, r"^t1_golden_days = (\S+)$"),
    }
    for face, _ in SHELL_FACES:
        facts[face.replace("-", "_")] = first_capture(out, rf"^shell_count\[{face}\] = (\S+)$")
    facts["files_scanned"] = first_capture(out, r"^files_scanned = (\S+)$")
    return facts


def judge_ac17_08(facts: Facts) -> Verdict:
    """``AC-17|08``: the census is clean over the whole tree and T1 has real faces."""

    def key(face: str) -> str:
        return face.replace("-", "_")

    counts = ", ".join(f"{label}={facts[key(face)]}" for face, label in SHELL_FACES)
    shells_zero = all(facts[key(face)] == "0" for face, _ in SHELL_FACES)
    floors = ", ".join(
        f"{key}={facts[key]}(≥{floor})" for key, floor in (T1_FLOORS + (("t1_golden_days", 1),))
    )
    ok = (
        facts["tool_exit"] == "0"
        and facts["tool_shells"] == "clean"
        and facts["tool_t1"] == "met"
        and positive(facts["population"])
        and facts["files_scanned"] == facts["population"]
        and facts["untracked_tests"] == "0"
        and shells_zero
        and facts["fixture_sha_ok"] == "yes"
        and facts["provenance"] == "yes"
        and positive(facts["fixture_cases"])
        and all(number(facts[key]) >= floor for key, floor in T1_FLOORS)
        and positive(facts["t1_golden_days"])
    )
    readings = (
        f"{SHELL_AUDIT_TOOL} exit {facts['tool_exit']}（shells={facts['tool_shells']} "
        f"t1={facts['tool_t1']}）：{counts}",
        f"population = {facts['population']} 个 test_*.py，census 扫到 {facts['files_scanned']} 个"
        "：全树零必须是「全树」，漏扫的那部分只是把空壳藏在读数之外；未被 git 跟踪的 "
        f"{facts['untracked_tests']} 个不算证据（CI 看不见的面无法复算）",
        f"§5.2 T1 三问：{floors}",
        f"录制夹具 = {facts['fixture_cases']} 例，逐条 sha 复算 = {facts['fixture_sha_ok']}，"
        f"来源可追溯 = {facts['provenance']}：手造「理想报文」在这一问上过不去",
    )
    reason = (
        ""
        if ok
        else "反空壳抽审判的是形态：定义抄写、恒真断言、自指、源码形式检查四类都必须为零，"
        "且 T1 档要拿真实录制信封与预计算黄金向量来答，三问的地板（≥3/含成功/≥3）缺一不可"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac17_10(ctx: Context) -> Facts:
    """Run the traceability gate member and read its census, its faces, its dates and its gaps."""
    code, out = run_argv([sys.executable, TRACEABILITY_TOOL])
    facts: Facts = {
        "tool_exit": count(code),
        "tool_ok": flag("\nOK:" in out),
        "tool_new": flag("NEW VIOLATION:" in out or "\nFAIL:" in out),
        "rounds": first_capture(out, r"rounds\s+= (\d+)"),
        "census": first_capture(out, r"files census\s+= (\d+)"),
        "dated_header": "(absent)",
        "dated_run": "(absent)",
        "logs_total": "(absent)",
    }
    for face in TRACE_FACES:
        facts[f"gap_{face}"] = first_capture(out, rf"{face}\s+gaps = (\d+)")
    dated = re.search(
        r"dates in gate logs = header (\d+)/(\d+), printed by the run (\d+)/(\d+)", out
    )
    if dated:
        facts["dated_header"] = dated.group(1)
        facts["logs_total"] = dated.group(2)
        facts["dated_run"] = dated.group(3)
    gone, tracked = evidence_deletions()
    facts["gone_archives"] = gone
    facts["tracked_archives"] = tracked
    try:
        payload = json.loads(ctx.read(TRACE_BASELINE_REL))
    except (ProbeError, json.JSONDecodeError) as exc:
        facts["baseline_entries"] = f"(unreadable: {exc})"
        return facts
    frozen = payload.get("violations") if isinstance(payload, dict) else None
    facts["baseline_entries"] = count(len(frozen)) if isinstance(frozen, list) else "(not a list)"
    return facts


def judge_ac17_10(facts: Facts) -> Verdict:
    """``AC-17|10``: every milestone archive answers when, from where, by what command and exit."""
    faces_ok = all(facts[f"gap_{face}"] == "0" for face in TRACE_FACES)
    ok = (
        facts["tool_exit"] == "0"
        and facts["tool_ok"] == "yes"
        and facts["tool_new"] == "no"
        and faces_ok
        and positive(facts["rounds"])
        and positive(facts["census"])
        and positive(facts["logs_total"])
        and facts["gone_archives"] == "0"
        and number(facts["dated_header"]) + number(facts["dated_run"])
        == number(facts["logs_total"])
    )
    readings = (
        f"{TRACEABILITY_TOOL} exit {facts['tool_exit']}, says OK = {facts['tool_ok']}, "
        f"reports a new violation = {facts['tool_new']}; frozen ceiling = "
        f"{facts['baseline_entries']} legacy entr(ies)",
        f"archive = {facts['rounds']} round(s), {facts['census']} file(s) in the census, "
        f"{facts['tracked_archives']} tracked now, {facts['gone_archives']} added once and then "
        "removed",
        "gaps by face: " + ", ".join(f"{face}={facts[f'gap_{face}']}" for face in TRACE_FACES),
        f"date answers among gate logs = header {facts['dated_header']} + printed by the run "
        f"{facts['dated_run']} out of {facts['logs_total']}: 日期 is satisfied by either face, "
        "and the two together have to cover every gate log",
    )
    reason = (
        ""
        if ok
        else "可追溯 means each archive answers 命令 / 输出摘要 / 日期 with no reader present: "
        "one gap on any face, an archive deleted after it was added, or a census too small to be "
        "the whole history turns the claim back into a summary"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


# --------------------------------------------------------------------------- #
# AC-18 -- the freshness door and the catalog, judged on the faces a caller reads
# --------------------------------------------------------------------------- #

#: The module AC-18|01/|02 are about, and every frontend face that shows their readings.
DATA_QUERY_REL: Final = "opendata/api/data_query.py"
PIPELINE_JOBS_REL: Final = "opendata/pipeline/jobs.py"
CATALOG_VIEW_REL: Final = "frontend/src/views/DataCatalogView.vue"
CATALOG_API_REL: Final = "frontend/src/api/catalog.ts"
CATALOG_TEST_REL: Final = "frontend/src/__tests__/catalog.test.ts"
CATALOG_E2E_REL: Final = "frontend/e2e/scripts.spec.ts"
ROUTER_REL: Final = "frontend/src/router/index.ts"
VITE_CONFIG_REL: Final = "frontend/vite.config.ts"
FRONTEND_COLLECTOR: Final = "scripts/quality/frontend_test_collection.py"

#: 「各域最新数据日期与滞后天数可查」：一条门的读法一个节点，缺一条就是少一问。
FRESHNESS_QUERY_NODES: Final = (
    "tests/test_data_catalog.py::TestTheFreshnessDoor::test_the_dwd_door_names_its_baseline",
    "tests/test_data_catalog.py::TestTheFreshnessDoor::test_the_two_doors_agree_on_the_same_domain",
    "tests/test_data_catalog.py::TestTheFreshnessDoor::"
    "test_the_ods_door_measures_that_sources_own_table",
    "tests/test_data_catalog.py::TestTheFreshnessDoor::"
    "test_a_leg_with_no_table_still_reports_the_baseline",
    "tests/test_data_query_api.py::TestValidationPaths::test_unknown_freshness_domain_is_a_404",
    "tests/test_data_query_api.py::TestTheExpectationIsTheCalendars::"
    "test_the_baseline_is_taken_through_the_calendar_on_the_pinned_day",
)

#: 「缺失触发告警」：判据要的是告警真的出门，所以投递、静默与通道三条都在。
FRESHNESS_ALERT_NODES: Final = (
    "tests/test_alert_matrix.py::TestCollection::test_both_layers_are_read_for_a_registered_domain",
    "tests/test_alert_matrix.py::TestCollection::"
    "test_a_leg_with_no_field_mapping_is_the_measured_majority",
    "tests/test_alert_matrix.py::TestDelivery::"
    "test_a_missing_table_alerts_critical_and_reaches_the_channel",
    "tests/test_alert_matrix.py::TestDelivery::test_a_stale_reading_warns_on_the_merged_layer",
    "tests/test_alert_matrix.py::TestDelivery::test_a_healthy_warehouse_sends_nothing",
    "tests/test_alert_matrix.py::TestDelivery::"
    "test_the_verdict_follows_the_supplied_expectation_not_the_wall_clock",
    "tests/test_alert_matrix.py::TestDelivery::"
    "test_a_channel_that_refuses_the_frame_does_not_lose_the_alerts",
    "tests/test_pipeline_jobs.py::TestFreshnessExecutor::"
    "test_it_measures_against_the_calendar_not_the_wall_clock",
    "tests/test_pipeline_jobs.py::TestFreshnessExecutor::test_it_delivers_on_the_websocket_channel",
)

#: The catalog faces |02 names, one node each: five readings, the two ways a leg is
#: unmeasurable, and the third way a quality reading is absent.
CATALOG_READING_NODES: Final = (
    "tests/test_data_catalog.py::TestFiveReadings::test_a_populated_domain_carries_every_reading",
    "tests/test_data_catalog.py::TestFiveReadings::"
    "test_the_merged_layer_is_not_measured_with_a_source_column",
    "tests/test_data_catalog.py::TestFiveReadings::"
    "test_a_clean_domain_says_clean_and_an_unmeasurable_one_says_so",
    "tests/test_data_catalog.py::TestFiveReadings::"
    "test_the_totals_describe_the_rows_the_caller_got",
    "tests/test_data_catalog.py::TestUnmeasurableLegs::"
    "test_a_leg_with_no_field_mapping_is_unmapped_not_borrowed",
    "tests/test_data_catalog.py::TestUnmeasurableLegs::"
    "test_an_absent_table_reads_missing_without_losing_the_row",
    "tests/test_data_catalog.py::TestUnmeasurableLegs::"
    "test_a_missing_diff_report_table_is_not_reported_as_zero",
)

#: 判据点名的五个读数，逐个配上：页面列名、页面上只有真测量才会出现的形状、
#: 单元面与真机页各自断言它时用到的字样。少任何一面，该读数就只是列名。
CATALOG_READINGS: Final = (
    ("覆盖标的数", 'label="覆盖"', "标的", "标的", "标的"),
    ("时间范围", 'label="时间范围"', "~", "~", "~"),
    ("各源最近更新", 'label="各源最近更新"', "已验证", "已验证", "已验证"),
    ("新鲜度", 'label="新鲜度"', "滞后", "滞后", "滞后"),
    ("质量标记", 'label="质量"', "未测量", "未测量", "未测量"),
)

#: 载荷里承载这五个读数的字段；接口与页面必须同名，否则页面显示的是另一次测量。
CATALOG_FIELDS: Final = ("coverage", "sources", "lag_days", "quality", "expected_data_date")


def asserted_lines(text: str, token: str) -> int:
    """Count assertion lines naming ``token`` — a reading no assertion reads is not a reading.

    Args:
        text: Source of a spec file.
        token: The exact string the assertion has to contain.

    Returns:
        How many ``expect(`` lines carry the token.
    """
    return sum(1 for line in text.splitlines() if "expect(" in line and token in line)


def outcomes(nodes: Sequence[str]) -> dict[str, str]:
    """Run each node id once and key the result by the test name."""
    return {node.split("::")[-1]: node_outcome(node) for node in nodes}


def bad_of(results: dict[str, str]) -> str:
    """The nodes that did not pass, or ``-`` when every one of them did."""
    return ", ".join(f"{name}={seen}" for name, seen in results.items() if seen != "passed") or "-"


def measure_ac18_01(ctx: Context) -> Facts:
    """Run the freshness door and the alert faces, then read how the scheduled job wires them."""
    query = outcomes(FRESHNESS_QUERY_NODES)
    alert = outcomes(FRESHNESS_ALERT_NODES)
    api = ctx.read(DATA_QUERY_REL)
    door = function_body(api, "domain_freshness")
    jobs = ctx.read(PIPELINE_JOBS_REL)
    return {
        "query_runs": count(len(query)),
        "query_passed": count(sum(1 for seen in query.values() if seen == "passed")),
        "query_bad": bad_of(query),
        "alert_runs": count(len(alert)),
        "alert_passed": count(sum(1 for seen in alert.values() if seen == "passed")),
        "alert_bad": bad_of(alert),
        "door_route": flag("/domains/{domain}/freshness" in api),
        "door_readings": flag("lag_days" in door and "latest" in door),
        "door_baseline": count(door.count('"expected_data_date"')),
        "job_wired": flag("TemplateKind.FRESHNESS" in function_body(jobs, "_execute_template")),
        "job_broadcasts": flag("broadcast=" in function_body(jobs, "_execute_freshness")),
        "job_executable": flag(
            "TemplateKind.FRESHNESS" in set_literal_members(jobs, "EXECUTABLE_KINDS")
        ),
    }


def judge_ac18_01(facts: Facts) -> Verdict:
    """``AC-18|01``: the door answers date + lag per domain, and 缺失 reaches a channel."""
    ok = (
        positive(facts["query_runs"])
        and facts["query_passed"] == facts["query_runs"]
        and facts["query_bad"] == "-"
        and positive(facts["alert_runs"])
        and facts["alert_passed"] == facts["alert_runs"]
        and facts["alert_bad"] == "-"
        and facts["door_route"] == "yes"
        and facts["door_readings"] == "yes"
        and number(facts["door_baseline"]) >= 2
        and facts["job_wired"] == "yes"
        and facts["job_broadcasts"] == "yes"
        and facts["job_executable"] == "yes"
    )
    readings = (
        f"新鲜度门 faces: {facts['query_passed']}/{facts['query_runs']} nodes passed"
        + (f"; not green: {facts['query_bad']}" if facts["query_bad"] != "-" else ""),
        f"{DATA_QUERY_REL}::domain_freshness route = {facts['door_route']}, carries "
        f"latest + lag_days = {facts['door_readings']}, and returns expected_data_date "
        f"{facts['door_baseline']} time(s) — a lag cannot be checked without the date it "
        "was measured against, so both branches have to name it",
        f"告警面: {facts['alert_passed']}/{facts['alert_runs']} nodes passed"
        + (f"; not green: {facts['alert_bad']}" if facts["alert_bad"] != "-" else ""),
        f"调度接线: the template dispatcher mentions FRESHNESS = {facts['job_wired']}, the "
        f"executor is an executable kind = {facts['job_executable']}, it is handed the WS "
        f"broadcast = {facts['job_broadcasts']}",
    )
    reason = (
        ""
        if ok
        else "判据两问都要有人答：可查意味着门交得出日期、滞后与基准日，触发告警意味着缺失真的"
        "出门到通道；任何一条节点变红、门不再报基准日、或执行器没接上 broadcast，都只算接口存在"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac18_02(ctx: Context) -> Facts:
    """Run the catalog faces, then check each reading on the page, the type and the run specs."""
    readings = outcomes(CATALOG_READING_NODES)
    view = ctx.read(CATALOG_VIEW_REL)
    spec = ctx.read(CATALOG_TEST_REL)
    e2e = ctx.read(CATALOG_E2E_REL)
    api = ctx.read(CATALOG_API_REL)
    missing: list[str] = []
    e2e_missing: list[str] = []
    for name, label, shape, unit_token, page_token in CATALOG_READINGS:
        if label not in view or shape not in view or asserted_lines(spec, unit_token) == 0:
            missing.append(name)
        if asserted_lines(e2e, page_token) == 0:
            e2e_missing.append(name)
    collector = script_module(FRONTEND_COLLECTOR)
    excludes, found = collector.parse_test_excludes(ctx.read(VITE_CONFIG_REL))
    return {
        "runs": count(len(readings)),
        "passed": count(sum(1 for seen in readings.values() if seen == "passed")),
        "bad": bad_of(readings),
        "readings_missing": ", ".join(missing) or "-",
        "e2e_missing": ", ".join(e2e_missing) or "-",
        "fields": count(sum(1 for field in CATALOG_FIELDS if field in api)),
        "header": flag("基准日" in view and asserted_lines(spec, "基准日") > 0),
        "collector_ok": flag(found and set(excludes) <= set(collector.ALLOWED_EXCLUDES)),
        "drilldown": flag(
            "预览" in view and "openDetail" in view and asserted_lines(spec, "page_size") > 0
        ),
    }


def judge_ac18_02(facts: Facts) -> Verdict:
    """``AC-18|02``: five readings per domain row, on the door and on the page, each asserted."""
    ok = (
        positive(facts["runs"])
        and facts["passed"] == facts["runs"]
        and facts["bad"] == "-"
        and facts["readings_missing"] == "-"
        and facts["e2e_missing"] == "-"
        and facts["fields"] == count(len(CATALOG_FIELDS))
        and facts["header"] == "yes"
        and facts["collector_ok"] == "yes"
        and facts["drilldown"] == "yes"
    )
    readings = (
        f"目录接口 faces: {facts['passed']}/{facts['runs']} nodes passed"
        + (f"; not green: {facts['bad']}" if facts["bad"] != "-" else ""),
        f"五个读数在页面上有列名且被断言：缺 {facts['readings_missing']}",
        f"真机页（Playwright）逐读数断言：缺 {facts['e2e_missing']}",
        f"载荷类型 {CATALOG_API_REL} 承载 {facts['fields']}/{count(len(CATALOG_FIELDS))} 个字段 "
        f"({'/'.join(CATALOG_FIELDS)})，页面显示的是接口量出来的那一次",
        f"基准日在页面出现并被断言 = {facts['header']}，下钻 = {facts['drilldown']}，"
        f"单元面采集器无未授权排除 = {facts['collector_ok']}",
    )
    reason = (
        ""
        if ok
        else "判据列了五个读数（覆盖标的数/时间范围/各源最近更新/新鲜度/质量标记）：每个都要接口"
        "给得出、页面显示得出、并且有断言真在读它——只有列名的表格是版式，不是目录"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac18_03(ctx: Context) -> Facts:
    """Does one page own both views, with the catalog as its default and detail under it?"""
    router = ctx.read(ROUTER_REL)
    view = ctx.read(CATALOG_VIEW_REL)
    titles = re.findall(r"title: '([^']+)'", router)
    interface_titles = [title for title in titles if "数据接口" in title]
    merged = re.search(
        r"path: 'scripts',\s*\n\s*name: '[^']+',\s*\n\s*component: \(\) => "
        r"import\('([^']+)'\)",
        router,
    )
    return {
        "page_titles": ", ".join(interface_titles) or "-",
        "nav_entries": count(len(interface_titles)),
        "route_is_catalog": flag(
            merged is not None and merged.group(1).endswith("DataCatalogView.vue")
        ),
        "detail_in_catalog": flag("data/interfaces" in view or "接口" in view),
        "detail_route": flag("接口详情" in router),
    }


def judge_ac18_03(facts: Facts) -> Verdict:
    """``AC-18|03``: 数据接口 must be the catalog page itself, with 函数级明细 as its drill-down."""
    ok = (
        facts["nav_entries"] == "1"
        and facts["route_is_catalog"] == "yes"
        and facts["detail_in_catalog"] == "yes"
        and facts["detail_route"] == "yes"
    )
    readings = (
        f"路由标题含「数据接口」的页面 = {facts['nav_entries']} ({facts['page_titles']})",
        f"`/scripts` 是否渲染目录视图 = {facts['route_is_catalog']}",
        f"{CATALOG_VIEW_REL} 是否读到函数级明细（data/interfaces / 接口） = "
        f"{facts['detail_in_catalog']}",
        f"下钻终点（接口详情路由）仍在 = {facts['detail_route']}",
    )
    reason = (
        ""
        if ok
        else "合并要求的是一个页面：目录为默认视图、函数级明细在它下面。今天 /data 与 /scripts 仍是"
        "两条路由两个视图，目录的下钻只预览数据行，从不落到接口/函数那一层；接口清单按 §11.1 已经"
        "以域名命名（data_interfaces.name == domain），join 的料是齐的，缺的是页面合并本身——"
        "而合并必然要让一个导航项消失，属产品决定"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


# --------------------------------------------------------------------------- #
# AC-13 -- the alert matrix, judged on the four rows its own wording names
# --------------------------------------------------------------------------- #

#: The module that owns the matrix, and the rule engine it delegates to.
ALERT_MATRIX_REL: Final = "opendata/pipeline/alert_matrix.py"
FRESHNESS_REL: Final = "opendata/pipeline/freshness.py"

#: The four kinds 「告警矩阵生效」names. ``freshness`` is not one of them: AC-18|01
#: owns that row, and a probe that re-claimed it would hide a missing kind behind a
#: reading that was already paid for elsewhere.
MATRIX_KIND_TOKENS: Final = ("pipeline 失败", "连续失败", "分区缺失", "磁盘水位")
MATRIX_RULE_KINDS: Final = (
    "pipeline_failure",
    "consecutive_failures",
    "partition_missing",
    "disk_water",
)

#: One producer per kind. ``pipeline_failure`` and ``consecutive_failures`` are the
#: same reading with two thresholds, so they share ``collect_failures``.
MATRIX_PRODUCERS: Final = {
    "pipeline_failure": "collect_failures",
    "consecutive_failures": "collect_failures",
    "partition_missing": "collect_partitions",
    "disk_water": "collect_disk",
}

#: The inputs the freshness layer cannot reach on its own, so the scheduled
#: executor has to supply each one -- declared *and* passed.
MATRIX_INPUTS: Final = ("control_engine", "current_year", "disk_path")

#: What one run could not measure. Without these the scope would read "no alerts"
#: for a matrix that was never given the data to decide on.
MATRIX_SCOPE_FIELDS: Final = ("failure_legs", "partitioned_tables", "disk_path")

#: 四类各一条 producer→判定→出帧的节点，加一条四类同跑的节点。
MATRIX_KIND_NODES: Final = (
    "tests/test_alert_matrix.py::TestFailureSignal::"
    "test_one_lost_shard_makes_the_whole_run_a_failure",
    "tests/test_alert_matrix.py::TestFailureSignal::test_the_streak_counts_runs_not_shards",
    "tests/test_alert_matrix.py::TestFailureSignal::test_a_run_that_worked_again_breaks_the_streak",
    "tests/test_alert_matrix.py::TestDiskAndPartitionSignals::"
    "test_a_missing_year_alerts_without_the_matrix_touching_ddl",
    "tests/test_alert_matrix.py::TestDiskAndPartitionSignals::"
    "test_the_partition_face_never_asks_for_a_repair",
    "tests/test_alert_matrix.py::TestDiskAndPartitionSignals::test_a_full_volume_alerts_critical",
    "tests/test_alert_matrix.py::TestAlertFrames::"
    "test_every_rule_gets_a_type_a_subscriber_can_route_on",
    "tests/test_alert_matrix.py::TestAllFourKindsAtOnce::"
    "test_one_run_delivers_every_kind_the_matrix_decides",
)

#: 接线面：执行器真的把三类输入喂进去、没喂的输入要读成「测不到」而不是「健康」。
MATRIX_WIRING_NODES: Final = (
    "tests/test_alert_matrix.py::TestMatrixScopeHonesty::"
    "test_inputs_left_out_read_as_none_rather_than_zero",
    "tests/test_alert_matrix.py::TestMatrixScopeHonesty::"
    "test_the_control_engine_turns_the_failure_rows_on",
    "tests/test_pipeline_jobs.py::TestFreshnessExecutor::"
    "test_it_feeds_the_three_faces_freshness_cannot_reach",
    "tests/test_pipeline_jobs.py::TestFreshnessExecutor::"
    "test_the_horizon_years_default_to_the_design_rule",
)


def module_level_defs(source: str) -> tuple[str, ...]:
    """Names of the module-level functions, so a producer is counted where it lives."""
    tree = ast.parse(source)
    return tuple(
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def parameter_names(source: str, name: str) -> tuple[str, ...]:
    """Every parameter of one module-level function, positional or keyword-only."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            args = node.args
            return tuple(a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs))
    return ()


def call_keywords_in(source: str, function: str, name: str) -> tuple[str, ...]:
    """Keyword arguments one call passes, looked up inside one function's body.

    Reading the call site instead of the signature is the point: a parameter that
    exists but is never passed is a face of the matrix that stays dark while the
    run reports zero alerts.
    """
    body = function_body(source, function)
    if not body:
        return ()
    for node in ast.walk(ast.parse(body)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name:
            return tuple(kw.arg for kw in node.keywords if kw.arg)
    return ()


def class_fields(source: str, name: str) -> tuple[str, ...]:
    """Annotated attribute names of a module-level class."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return tuple(
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            )
    return ()


def alert_rules_emitted(source: str) -> tuple[str, ...]:
    """The ``rule`` values the judge engine can produce (first positional of ``Alert``)."""
    rules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "Alert" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            rules.add(first.value)
    return tuple(sorted(rules))


def measure_ac13_07(ctx: Context) -> Facts:
    """Run the four kinds' nodes, then check each kind has a producer the job feeds."""
    kinds = outcomes(MATRIX_KIND_NODES)
    wiring = outcomes(MATRIX_WIRING_NODES)
    matrix = ctx.read(ALERT_MATRIX_REL)
    text = ctx.item("AC-13|07").text
    defined = module_level_defs(matrix)
    emitted = alert_rules_emitted(ctx.read(FRESHNESS_REL))
    fed = call_keywords_in(ctx.read(PIPELINE_JOBS_REL), "_execute_freshness", "run_alert_matrix")
    declared = parameter_names(matrix, "run_alert_matrix")
    missing_producer = [fn for fn in MATRIX_PRODUCERS.values() if fn not in defined]
    return {
        "kind_runs": count(len(kinds)),
        "kind_passed": count(sum(1 for seen in kinds.values() if seen == "passed")),
        "kind_bad": bad_of(kinds),
        "wiring_runs": count(len(wiring)),
        "wiring_passed": count(sum(1 for seen in wiring.values() if seen == "passed")),
        "wiring_bad": bad_of(wiring),
        "kinds_named": count(sum(1 for token in MATRIX_KIND_TOKENS if token in text)),
        "rules_emitted": ", ".join(emitted) or "-",
        "rules_judged": count(sum(1 for rule in MATRIX_RULE_KINDS if rule in emitted)),
        "producers": count(len(MATRIX_PRODUCERS) - len(missing_producer)),
        "missing_producer": ", ".join(missing_producer) or "-",
        "inputs_declared": count(sum(1 for key in MATRIX_INPUTS if key in declared)),
        "inputs_fed": count(sum(1 for key in MATRIX_INPUTS if key in fed)),
        "input_unfed": ", ".join(key for key in MATRIX_INPUTS if key not in fed) or "-",
        "scope_fields": count(
            sum(1 for field in MATRIX_SCOPE_FIELDS if field in class_fields(matrix, "MatrixScope"))
        ),
    }


def judge_ac13_07(facts: Facts) -> Verdict:
    """``AC-13|07``: each named kind has a producer, is decided, and reaches the run."""
    ok = (
        positive(facts["kind_runs"])
        and facts["kind_passed"] == facts["kind_runs"]
        and facts["kind_bad"] == "-"
        and positive(facts["wiring_runs"])
        and facts["wiring_passed"] == facts["wiring_runs"]
        and facts["wiring_bad"] == "-"
        and facts["kinds_named"] == "4"
        and facts["rules_judged"] == "4"
        and facts["producers"] == "4"
        and facts["missing_producer"] == "-"
        and facts["inputs_declared"] == "3"
        and facts["inputs_fed"] == "3"
        and facts["input_unfed"] == "-"
        and facts["scope_fields"] == "3"
    )
    readings = (
        f"四类节点: {facts['kind_passed']}/{facts['kind_runs']} passed"
        + (f"; not green: {facts['kind_bad']}" if facts["kind_bad"] != "-" else ""),
        f"接线节点: {facts['wiring_passed']}/{facts['wiring_runs']} passed"
        + (f"; not green: {facts['wiring_bad']}" if facts["wiring_bad"] != "-" else ""),
        f"判据原文点名的类数 = {facts['kinds_named']}/4；判定引擎产出的 rule = "
        f"{facts['rules_emitted']}，四类齐 = {facts['rules_judged']}/4",
        f"生产者齐备 = {facts['producers']}/4（缺: {facts['missing_producer']}）；"
        f"执行器喂入的三类输入 = {facts['inputs_fed']}/{facts['inputs_declared']}"
        f"（未喂: {facts['input_unfed']}）",
        f"范围诚实字段 = {facts['scope_fields']}/3 —— 测不到要读成 None/0 而不是健康",
    )
    reason = (
        ""
        if ok
        else "「生效」要求判据点名的四类各有生产者、各有判定行、各被调度执行器真的喂到输入；"
        "任何一类没有生产者、执行器只声明不传参（那一类永远读成零告警）、或测不到的输入不再"
        "记进范围（空读数伪装成健康），都只算规则存在"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


# --------------------------------------------------------------------------- #
# AC-9 -- the cross-check, judged on the three faces its wording names
# --------------------------------------------------------------------------- #

ALERTS_REL: Final = "opendata/pipeline/alerts.py"
DIFF_ALERTS_REL: Final = "opendata/pipeline/diff_alerts.py"
DIFF_GOVERNANCE_REL: Final = "opendata/pipeline/diff_governance.json"
DIFF_REPORT_REL: Final = "opendata/pipeline/diff_report.py"
CROSS_CHECK_SERVICE_REL: Final = "opendata/pipeline/cross_check_service.py"
PIPELINE_TEMPLATES_REL: Final = "opendata/pipeline/templates.py"
SUBSCRIPTION_REL: Final = "opendata/pipeline/subscription.py"
SCHEDULES_REL: Final = "opendata/pipeline/schedules.yaml"

#: 判据 |02 逐字点名的记录内容：domain/源对/key/字段/两源值/偏差/verdict。
#: 「列名在 ``REPORT_COLUMNS`` 里」和「``ReportRow`` 真带着这一列」是两回事：写入走
#: ``as_params()`` 的 ``getattr``，只加列名不加字段，每一次写库都会当场炸。
DIFF_RECORD_COLUMNS: Final = (
    ("domain", "domain"),
    ("源对 a", "source_a"),
    ("源对 b", "source_b"),
    ("key", "biz_key"),
    ("字段", "field"),
    ("两源值 a", "value_a"),
    ("两源值 b", "value_b"),
    ("偏差", "deviation"),
    ("verdict", "verdict"),
)

#: 「注入差异样本被**校对作业**捕获」：调度触发、注入即报即写、逐列落表各有节点。
CAPTURE_NODES: Final = (
    "tests/test_pipeline_jobs.py::TestFullCheckExecutor::"
    "test_the_scheduled_run_compares_reports_and_alerts",
    "tests/test_pipeline_jobs.py::TestFullCheckExecutor::"
    "test_a_second_run_of_the_same_difference_is_not_re_alerted",
    "tests/test_pipeline_jobs.py::TestFullCheckExecutor::test_consistent_legs_produce_no_delivery",
    "tests/test_cross_check_service.py::TestCrossCheckService::"
    "test_injected_difference_is_reported_and_alerted",
    "tests/test_diff_report.py::TestReportRows::test_detail_rows_carry_the_design_columns",
    "tests/test_diff_report.py::TestReportRows::test_insert_sql_binds_every_column",
    "tests/test_diff_report.py::TestReportRows::test_report_writer_and_retention_share_the_named_table",
)

#: 判据 |04 的三则，各两条：白名单、不重复告警、突增才升级。少一条就只剩一则在生效。
GOVERNANCE_NODES: Final = (
    "tests/test_diff_alerts.py::TestSharedPolicy::test_every_caller_gets_the_same_instance",
    "tests/test_diff_alerts.py::TestSharedPolicy::"
    "test_its_whitelist_comes_from_the_governance_file",
    "tests/test_diff_alerts.py::TestSharedPolicy::test_dedupe_survives_a_second_scheduled_run",
    "tests/test_diff_alerts.py::TestSharedPolicy::"
    "test_the_rate_baseline_carries_across_comparisons",
    "tests/test_diff_alerts.py::TestSuppression::test_a_whitelisted_difference_reaches_no_channel",
    "tests/test_diff_alerts.py::TestSuppression::test_every_notify_is_recorded_even_the_quiet_ones",
    "tests/test_cross_check_service.py::TestAlertPolicy::"
    "test_whitelisted_domain_field_is_suppressed",
    "tests/test_cross_check_service.py::TestAlertPolicy::test_repeated_fingerprint_is_deduplicated",
    "tests/test_cross_check_service.py::TestAlertPolicy::test_rate_spike_escalates_to_critical",
)

#: 判据 |05 的两通道：WS 广播、按域过滤的订阅 hub、SMTP 投递，各有断言与失败归因节点。
CHANNEL_NODES: Final = (
    "tests/test_diff_alerts.py::TestChannels::test_an_alerting_decision_reaches_all_three_sinks",
    "tests/test_diff_alerts.py::TestChannels::"
    "test_the_ws_payload_carries_the_decision_not_just_the_numbers",
    "tests/test_diff_alerts.py::TestChannels::"
    "test_an_unwired_channel_is_a_reported_reason_not_a_silent_pass",
    "tests/test_diff_alerts.py::TestChannels::"
    "test_a_channel_that_raises_is_recorded_and_does_not_stop_the_others",
    "tests/test_diff_alerts.py::TestChannels::test_a_hub_without_the_diff_method_is_reported",
    "tests/test_diff_alerts.py::TestMailSenderWiring::test_both_settings_wire_a_sender",
    "tests/test_diff_alerts.py::TestMailSenderWiring::"
    "test_the_sender_hands_the_message_to_the_shared_smtp_transport",
    "tests/test_diff_alerts.py::TestProductionWiring::"
    "test_the_real_dispatcher_reaches_this_deployments_channels",
    "tests/test_data_subscribe.py::TestFraming::test_the_governance_verdict_travels_with_the_alert",
    "tests/test_data_subscribe.py::TestHub::test_diff_alert_reaches_the_domain_subscribers",
)


def method_body(source: str, cls: str, name: str) -> str:
    """The source of one method, so a judge reads what the channel actually calls.

    ``function_body`` only walks module-level definitions, and the whole delivery face lives on
    ``DiffAlertDispatcher``. Reading a method as absent would report real code as an empty body.
    """
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for member in node.body:
                if (
                    isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and member.name == name
                ):
                    return ast.get_source_segment(source, member) or ""
    return ""


def measure_ac9_02(ctx: Context) -> Facts:
    """Run the scheduled comparison's capture faces, then read what the report really records."""
    captured = outcomes(CAPTURE_NODES)
    report = ctx.read(DIFF_REPORT_REL)
    declared = literal_str_tuple(parse(DIFF_REPORT_REL), "REPORT_COLUMNS")
    row_fields = class_fields(report, "ReportRow")
    jobs = ctx.read(PIPELINE_JOBS_REL)
    build = function_body(ctx.read(PIPELINE_TEMPLATES_REL), "build_cross_check")
    full = function_body(jobs, "_execute_full_check")
    return {
        "capture_runs": count(len(captured)),
        "capture_passed": count(sum(1 for seen in captured.values() if seen == "passed")),
        "capture_bad": bad_of(captured),
        "columns_missing": ", ".join(
            f"{label}→{column}" for label, column in DIFF_RECORD_COLUMNS if column not in declared
        )
        or "-",
        "row_fields_missing": ", ".join(
            column for _, column in DIFF_RECORD_COLUMNS if column not in row_fields
        )
        or "-",
        "table_named": flag('"dq_diff_report"' in report),
        "writer_wired": flag("write_report=DiffReportWriter(" in build),
        "job_executable": flag(
            "TemplateKind.FULL_CHECK" in set_literal_members(jobs, "EXECUTABLE_KINDS")
        ),
        "job_dispatched": flag("_execute_full_check" in function_body(jobs, "_execute_template")),
        "job_runs_service": flag("build_cross_check(" in full and ".run(" in full),
        "window_from_data": flag("cross_check_window" in full),
        "schedule_row": flag("kind: full_check" in ctx.read(SCHEDULES_REL)),
    }


def judge_ac9_02(facts: Facts) -> Verdict:
    """``AC-9|02``: a scheduled run captures the difference and writes every named column."""
    ok = (
        number(facts["capture_runs"]) == len(CAPTURE_NODES)
        and facts["capture_passed"] == facts["capture_runs"]
        and facts["capture_bad"] == "-"
        and facts["columns_missing"] == "-"
        and facts["row_fields_missing"] == "-"
        and facts["table_named"] == "yes"
        and facts["writer_wired"] == "yes"
        and facts["job_executable"] == "yes"
        and facts["job_dispatched"] == "yes"
        and facts["job_runs_service"] == "yes"
        and facts["window_from_data"] == "yes"
        and facts["schedule_row"] == "yes"
    )
    readings = (
        f"捕获面: {facts['capture_passed']}/{facts['capture_runs']} nodes passed"
        + (f"; not green: {facts['capture_bad']}" if facts["capture_bad"] != "-" else ""),
        f"{DIFF_REPORT_REL}: 判据点名的列缺 {facts['columns_missing']}，"
        f"``ReportRow`` 少字段 {facts['row_fields_missing']}，表名在位 {facts['table_named']} "
        "—— 列名与字段必须同时在场：写入走 as_params() 的 getattr",
        f"生产触发: full_check 是可执行 kind = {facts['job_executable']}，"
        f"_execute_template 派发它 = {facts['job_dispatched']}，执行器真的建服务并跑 = "
        f"{facts['job_runs_service']}，窗口取自两条腿共有的日子 = "
        f"{facts['window_from_data']}，schedules.yaml 里有这一行 = {facts['schedule_row']}",
        f"落库通路: build_cross_check 交出 write_report=DiffReportWriter = {facts['writer_wired']}",
    )
    reason = (
        ""
        if ok
        else "「被校对作业捕获」要求这一比较有一个真的会响的触发器（kind 可执行、模板派发、"
        "schedules.yaml 里有行），且捕获的结果逐列进得了 dq_diff_report；只有判定函数与写入类、"
        "没有任何调度触发器，或列名/字段少一个，都只算接口存在"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac9_04(ctx: Context) -> Facts:
    """Run the governance faces, then read where the whitelist comes from and who holds state."""
    decided = outcomes(GOVERNANCE_NODES)
    alerts = ctx.read(ALERTS_REL)
    decide = method_body(alerts, "AlertPolicy", "decide")
    diff_alerts = ctx.read(DIFF_ALERTS_REL)
    service = ctx.read(CROSS_CHECK_SERVICE_REL)
    notify = method_body(service, "CrossCheckService", "_notify")
    build = function_body(ctx.read(PIPELINE_TEMPLATES_REL), "build_cross_check")
    state = json.loads(ctx.read(DIFF_GOVERNANCE_REL))
    entries = state.get("known_differences") if isinstance(state, dict) else None
    tolerances = entries if isinstance(entries, list) else []
    reasonless = [
        str(entry)
        for entry in tolerances
        if not isinstance(entry, dict) or not str(entry.get("reason", "")).strip()
    ]
    return {
        "gov_runs": count(len(decided)),
        "gov_passed": count(sum(1 for seen in decided.values() if seen == "passed")),
        "gov_bad": bad_of(decided),
        "file_shape": flag(
            isinstance(state, dict)
            and isinstance(state.get("known_differences"), list)
            and isinstance(state.get("email_recipients"), list)
        ),
        "tolerances": count(len(tolerances)),
        "reason_missing": count(len(reasonless)),
        "whitelist_read": flag("self.whitelist" in decide),
        "dedupe_read": flag("self.alerted" in decide),
        "rate_read": flag("self.last_rate" in decide),
        "escalates": flag("LEVEL_CRITICAL" in decide and "RATE_SPIKE_FACTOR" in alerts),
        "shared_singleton": flag("global _POLICY" in function_body(diff_alerts, "shared_policy")),
        "whitelist_has_source": flag(
            "GOVERNANCE_PATH" in diff_alerts and "load_governance" in diff_alerts
        ),
        "policy_in_check": flag("shared_policy()" in build),
        "quiet_recorded": flag(
            "self._policy.decide" in notify and "self.notifier is not None" in notify
        ),
    }


def judge_ac9_04(facts: Facts) -> Verdict:
    """``AC-9|04``: whitelist tolerated, same difference not re-alerted, only a spike escalates."""
    ok = (
        number(facts["gov_runs"]) == len(GOVERNANCE_NODES)
        and facts["gov_passed"] == facts["gov_runs"]
        and facts["gov_bad"] == "-"
        and facts["file_shape"] == "yes"
        and facts["reason_missing"] == "0"
        and facts["whitelist_read"] == "yes"
        and facts["dedupe_read"] == "yes"
        and facts["rate_read"] == "yes"
        and facts["escalates"] == "yes"
        and facts["shared_singleton"] == "yes"
        and facts["whitelist_has_source"] == "yes"
        and facts["policy_in_check"] == "yes"
        and facts["quiet_recorded"] == "yes"
    )
    readings = (
        f"治理三则节点: {facts['gov_passed']}/{facts['gov_runs']} passed"
        + (f"; not green: {facts['gov_bad']}" if facts["gov_bad"] != "-" else ""),
        f"{DIFF_GOVERNANCE_REL}: 形状合法 = {facts['file_shape']}，容忍项 "
        f"{facts['tolerances']} 条、其中无 reason 的 {facts['reason_missing']} 条"
        "（容忍为空是量出来的结论，不是没配过；无凭据的容忍项即判红）",
        f"判定面: 白名单 {facts['whitelist_read']} / 去重集 {facts['dedupe_read']} / "
        f"差异率基线 {facts['rate_read']} / 突升级到 critical {facts['escalates']}",
        f"状态寿命: 策略是进程级单例 = {facts['shared_singleton']}，白名单有来源 = "
        f"{facts['whitelist_has_source']}，校对服务用的是它而不是每次新建 = "
        f"{facts['policy_in_check']}，被抑制的决定照样交给 notifier 记录 = "
        f"{facts['quiet_recorded']}",
    )
    reason = (
        ""
        if ok
        else "「告警治理生效」的三则各有寿命要求：白名单要有来源并真的被判定读到，去重与差异率"
        "基线要跨过一次以上的比对还成立（每次新建策略就等于没有这两则），被抑制的差异要留痕"
        "（否则「安静」与「没测」同形）"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac9_05(ctx: Context) -> Facts:
    """Run the channel faces, then read which sinks the dispatcher actually calls."""
    delivered = outcomes(CHANNEL_NODES)
    diff_alerts = ctx.read(DIFF_ALERTS_REL)
    subscription = ctx.read(SUBSCRIPTION_REL)
    notify = method_body(diff_alerts, "DiffAlertDispatcher", "notify")
    publish_ws = method_body(diff_alerts, "DiffAlertDispatcher", "_publish_ws")
    publish_hub = method_body(diff_alerts, "DiffAlertDispatcher", "_publish_hub")
    send_mail = method_body(diff_alerts, "DiffAlertDispatcher", "_send_mail")
    hub_filter = method_body(subscription, "SubscriptionHub", "publish_diff_alert")
    as_dict = method_body(diff_alerts, "Delivery", "as_dict")
    mailer = function_body(diff_alerts, "smtp_mail_sender")
    production = function_body(diff_alerts, "production_dispatcher")
    sinks = ("_publish_ws", "_publish_hub", "_send_mail")
    channels = ("broadcast=", "hub=", "mail_send=", "recipients=")
    return {
        "ch_runs": count(len(delivered)),
        "ch_passed": count(sum(1 for seen in delivered.values() if seen == "passed")),
        "ch_bad": bad_of(delivered),
        "event_named": flag('"type": "data.diff_alert"' in subscription),
        "ws_called": flag("await self.broadcast(message)" in publish_ws),
        "hub_called": flag("publish_diff_alert" in publish_hub),
        "hub_filters": flag("sub.domain == domain" in hub_filter),
        "both_channels_in_notify": flag(all(sink in notify for sink in sinks)),
        "mail_rendered": flag("render_diff_email(" in send_mail),
        "mail_transport": flag("_smtp_send" in mailer),
        "settings_gated": count(sum(1 for token in ("smtp_host", "smtp_user") if token in mailer)),
        "production_channels": count(sum(1 for token in channels if token in production)),
        "reported": flag(
            all(token in as_dict for token in ("ws_sent", "hub_delivered", "mail_sent"))
        ),
        "recorded": flag("self.deliveries.append(delivery)" in notify),
    }


def judge_ac9_05(facts: Facts) -> Verdict:
    """``AC-9|05``: SMTP mail and the WS ``data.diff_alert`` broadcast both really carry it."""
    ok = (
        number(facts["ch_runs"]) == len(CHANNEL_NODES)
        and facts["ch_passed"] == facts["ch_runs"]
        and facts["ch_bad"] == "-"
        and facts["event_named"] == "yes"
        and facts["ws_called"] == "yes"
        and facts["hub_called"] == "yes"
        and facts["hub_filters"] == "yes"
        and facts["both_channels_in_notify"] == "yes"
        and facts["mail_rendered"] == "yes"
        and facts["mail_transport"] == "yes"
        and number(facts["settings_gated"]) >= 2
        and number(facts["production_channels"]) == 4
        and facts["reported"] == "yes"
        and facts["recorded"] == "yes"
    )
    readings = (
        f"通道节点: {facts['ch_passed']}/{facts['ch_runs']} passed"
        + (f"; not green: {facts['ch_bad']}" if facts["ch_bad"] != "-" else ""),
        f"WS 面: 事件名 data.diff_alert 在位 = {facts['event_named']}，广播被调用 = "
        f"{facts['ws_called']}，订阅 hub 被调用 = {facts['hub_called']}，hub 按域过滤 = "
        f"{facts['hub_filters']}",
        f"邮件面: 渲染函数被调用 = {facts['mail_rendered']}，走的是共享 SMTP 传输 = "
        f"{facts['mail_transport']}，配置门 settings.smtp_host/user 读到 "
        f"{facts['settings_gated']}/2",
        f"装配与报告: production_dispatcher 交出四条通道 = {facts['production_channels']}/4，"
        f"一次 notify 同时喂三处 = {facts['both_channels_in_notify']}，投递结果逐通道可报告 = "
        f"{facts['reported']}，每次调用留一条记录 = {facts['recorded']}",
    )
    reason = (
        ""
        if ok
        else "「双通道」判的是两条都出得去且各自报告：渲染函数存在不等于广播被调用，邮件模板"
        "存在不等于有人把消息交给 SMTP 传输；任一通道只剩定义、或投递结果不可报告（静默失败"
        "与从未投递同形），这一格都只算接口存在"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


# --------------------------------------------------------------------------- #
# AC-9|03/|08/|09 -- counterexamples, single-source passthrough, recompute
# --------------------------------------------------------------------------- #

#: 判据 |03/|08/|09 各自的实现模块，与本轮 census 的留档读数。
CROSS_CHECK_REL: Final = "opendata/pipeline/cross_check.py"
DWD_MERGE_REL: Final = "opendata/pipeline/dwd_merge.py"
WAREHOUSE_DDL_REL: Final = "opendata/pipeline/ddl.py"
CROSS_CHECK_TESTS_REL: Final = "tests/test_cross_check.py"
DWD_MERGE_TESTS_REL: Final = "tests/test_dwd_merge.py"
C49_CENSUS_REL: Final = "docs/evidence/C49/census-post-fix.txt"

#: 「反例用例」的两问各自要有会响的节点：① 字段名不匹配报错，② 单位未归一 mismatch。
#: 再加 tolerance 配对 —— 一轮判据里两条腿各声明自己的容忍度，谁松谁紧都要同一个答案。
COUNTEREXAMPLE_NODES: Final = (
    "tests/test_cross_check.py::TestComparison::test_unconverted_unit_produces_a_mismatch",
    "tests/test_cross_check.py::TestNormalizationInTheComparison::"
    "test_structural_field_mismatch_fails_closed",
    "tests/test_cross_check.py::TestCounterexamples::"
    "test_a_mapped_column_the_frame_lacks_errors_rather_than_passing",
    "tests/test_cross_check.py::TestCounterexamples::"
    "test_the_tighter_tolerance_decides_whichever_leg_declares_it",
)

#: |08 的判据点：直通把没有歧义的键交出来、输出列就是 dwd 表声明的列、layer=dwd 建得出 SQL。
PASSTHROUGH_NODES: Final = (
    "tests/test_dwd_merge.py::TestMergeSourceFrames::test_single_source_domain_is_passthrough",
    "tests/test_dwd_merge.py::TestAmbiguousKey::"
    "test_the_ambiguous_key_is_refused_and_the_rest_of_the_window_lands",
    "tests/test_dwd_merge.py::TestAmbiguousKey::"
    "test_the_service_reports_the_refusal_it_landed_around",
    "tests/test_dwd_merge.py::TestAmbiguousKey::test_a_missing_key_column_still_fails_closed",
    "tests/test_dwd_merge.py::TestPassthroughQueryFace::"
    "test_passthrough_columns_are_exactly_the_dwd_table_columns",
    "tests/test_dwd_merge.py::TestPassthroughQueryFace::"
    "test_layer_dwd_query_builds_over_the_landed_columns",
)

#: |09 的判据点：同输入重算逐格不动、输入顺序不改变输出、落地是 key 级 upsert、空重算什么都不写。
RECOMPUTE_NODES: Final = (
    "tests/test_dwd_merge.py::TestRecomputeIdempotence::"
    "test_a_second_recompute_of_the_same_input_is_the_same_frame",
    "tests/test_dwd_merge.py::TestRecomputeIdempotence::"
    "test_the_input_dictionary_order_does_not_change_the_output",
    "tests/test_dwd_merge.py::TestRecomputeIdempotence::"
    "test_a_recompute_at_a_later_clock_moves_only_the_audit_stamp",
    "tests/test_dwd_merge.py::TestRecomputeIdempotence::"
    "test_landing_the_same_key_twice_is_one_row_written_in_place",
    "tests/test_dwd_merge.py::TestRecomputeIdempotence::test_an_empty_recompute_writes_nothing",
)

#: 真库那一格幂等断言是 e2e，门禁与探针的选择式（``-m "not e2e"``）都不跑它；读数是披露不是判据。
MYSQL_IDEMPOTENCE_NODE: Final = (
    "tests/test_dwd_merge.py::TestDwdWriteAgainstMysql::"
    "test_merge_writes_rows_with_trace_columns_and_is_idempotent"
)


def measure_ac9_03(ctx: Context) -> Facts:
    """Run the counterexample nodes, then read where the tolerance pairing is decided."""
    decided = outcomes(COUNTEREXAMPLE_NODES)
    engine = ctx.read(CROSS_CHECK_REL)
    compare = function_body(engine, "compare_source_frames")
    stricter = function_body(engine, "_stricter_tolerances")
    witnesses = ctx.read(CROSS_CHECK_TESTS_REL)
    pairing = method_body(
        witnesses,
        "TestCounterexamples",
        "test_the_tighter_tolerance_decides_whichever_leg_declares_it",
    )
    raising = method_body(
        witnesses,
        "TestCounterexamples",
        "test_a_mapped_column_the_frame_lacks_errors_rather_than_passing",
    )
    return {
        "x_runs": count(len(decided)),
        "x_passed": count(sum(1 for seen in decided.values() if seen == "passed")),
        "x_bad": bad_of(decided),
        "entry_is_production": flag(
            "compare_source_frames(" in raising and "compare_source_frames(" in pairing
        ),
        "field_name_raises": flag('match="missing mapped columns' in raising),
        "unit_mismatch": flag('summary.samples[0].field == "volume"' in witnesses),
        "whole_dictionary_gone": flag(
            "mapping_b.tolerances or mapping_a.tolerances" not in compare
            and "_stricter_tolerances(mapping_a, mapping_b)" in compare
        ),
        "tighter_wins": flag("value < current" in stricter),
        "both_orders": flag(
            "compare(tight, loose)" in pairing and "compare(loose, tight)" in pairing
        ),
    }


def judge_ac9_03(facts: Facts) -> Verdict:
    """``AC-9|03``: both counterexamples bite at the job's own entry point."""
    ok = (
        number(facts["x_runs"]) == len(COUNTEREXAMPLE_NODES)
        and facts["x_passed"] == facts["x_runs"]
        and facts["x_bad"] == "-"
        and facts["entry_is_production"] == "yes"
        and facts["field_name_raises"] == "yes"
        and facts["unit_mismatch"] == "yes"
        and facts["whole_dictionary_gone"] == "yes"
        and facts["tighter_wins"] == "yes"
        and facts["both_orders"] == "yes"
    )
    readings = (
        f"反例节点: {facts['x_passed']}/{facts['x_runs']} passed"
        + (f"; not green: {facts['x_bad']}" if facts["x_bad"] != "-" else ""),
        f"①字段名不匹配: 报错面在位 = {facts['field_name_raises']}，且两条反例都走生产入口 "
        f"compare_source_frames = {facts['entry_is_production']}（只测 normalize_frame 不算，"
        "校对作业读的是这个函数）",
        f"②单位未归一: mismatch 断言点名字段 = {facts['unit_mismatch']}",
        f"tolerance 配对面: 一侧整字典交出已移除且逐字段合并 = {facts['whole_dictionary_gone']}，"
        f"取紧的那条 = {facts['tighter_wins']}，正反两个方向都量 = {facts['both_orders']}",
    )
    reason = (
        ""
        if ok
        else "「反例用例」要的是两种错输入各有一个会响的判定：字段名不匹配必须在生产入口报错"
        "（不是在下游 helper），单位没归一必须报 mismatch；tolerance 是逐字段的判断，一侧整"
        "字典交出去就意味着另一侧的紧声明可能从没生效过"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac9_08(ctx: Context) -> Facts:
    """Run the passthrough faces, then read whether the landed table actually has rows."""
    delivered = outcomes(PASSTHROUGH_NODES)
    merge = ctx.read(DWD_MERGE_REL)
    index = function_body(merge, "_index")
    witnesses = ctx.read(DWD_MERGE_TESTS_REL)
    query_witness = method_body(
        witnesses, "TestPassthroughQueryFace", "test_layer_dwd_query_builds_over_the_landed_columns"
    )
    census = ctx.read(C49_CENSUS_REL)
    return {
        "p_runs": count(len(delivered)),
        "p_passed": count(sum(1 for seen in delivered.values() if seen == "passed")),
        "p_bad": bad_of(delivered),
        "refuses_per_key": flag("colliding.add(biz_key)" in index),
        "reported": flag("colliding" in class_fields(merge, "MergeStats")),
        "whole_window_raise_gone": flag("duplicate business key" not in merge),
        "raises_on_missing_column": flag("lacks key columns" in index),
        "query_face": flag("_key(" in query_witness and "build_data_select" in query_witness),
        "landed_rows": _reading_after(census, "dwd 表 dwd_stock_action 实际行数"),
        "passthrough_rows": _reading_after(census, "直通 merge：rows"),
    }


def _reading_after(text: str, marker: str) -> str:
    """The digits following ``marker`` in an archived reading, or ``-``."""
    for line in text.splitlines():
        if marker in line:
            tail = line.split(marker, 1)[1].lstrip("＝=")
            digits = "".join(character for character in tail.split()[0] if character.isdigit())
            return digits if digits else "-"
    return "-"


def judge_ac9_08(facts: Facts) -> Verdict:
    """``AC-9|08``: a single-source domain really delivers a queryable dwd table."""
    ok = (
        number(facts["p_runs"]) == len(PASSTHROUGH_NODES)
        and facts["p_passed"] == facts["p_runs"]
        and facts["p_bad"] == "-"
        and facts["refuses_per_key"] == "yes"
        and facts["reported"] == "yes"
        and facts["whole_window_raise_gone"] == "yes"
        and facts["raises_on_missing_column"] == "yes"
        and facts["query_face"] == "yes"
        and number(facts["landed_rows"]) > 0
    )
    readings = (
        f"直通节点: {facts['p_passed']}/{facts['p_runs']} passed"
        + (f"; not green: {facts['p_bad']}" if facts["p_bad"] != "-" else ""),
        f"歧义键处理: 逐键拒绝（不再整窗 raise）= {facts['refuses_per_key']}，旧的一 raise 就"
        f"什么都不交的形状已移除 = {facts['whole_window_raise_gone']}，拒绝的键要报得出来 = "
        f"{facts['reported']}，列缺失仍然 fail closed = {facts['raises_on_missing_column']}",
        f"查询面: layer=dwd 走生产 _key/build_data_select 建得出 SQL = {facts['query_face']}，"
        "且直通输出列 == dwd 表声明列（落得了这张表）",
        f"{C49_CENSUS_REL} 的真库读数：stock_action/ths 直通交出 {facts['passthrough_rows']} 行，"
        f"而 dwd_stock_action 实际 {facts['landed_rows']} 行 —— 差的那一段是一次未确认的落库写入",
    )
    reason = (
        ""
        if ok
        else "「直通模式可用」判的是这张单源域的表里真的有行、并能按 layer=dwd 读回去：本轮把"
        "整窗 raise 换成逐键拒绝（C49 量到 55,073 行里 1 个键两义，dwd_stock_action 因此一直是"
        "空的），代码面已经通了；剩下的 0 行需要一次经确认的 warehouse 写入（重算落库），"
        "不是再改判定口径"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac9_09(ctx: Context) -> Facts:
    """Run the recompute faces, then read the two invariants that make a re-run not add rows."""
    recomputed = outcomes(RECOMPUTE_NODES)
    merge = ctx.read(DWD_MERGE_REL)
    writer = method_body(merge, "DwdWriter", "write")
    ddl = ctx.read(WAREHOUSE_DDL_REL)
    table_ddl = function_body(ddl, "_table_ddl")
    mysql = node_outcome(MYSQL_IDEMPOTENCE_NODE)
    return {
        "r_runs": count(len(recomputed)),
        "r_passed": count(sum(1 for seen in recomputed.values() if seen == "passed")),
        "r_bad": bad_of(recomputed),
        "keys_sorted": flag("all_keys = sorted(" in function_body(merge, "merge_source_frames")),
        "upsert_builder": flag("build_upsert_sql(table, columns, key)" in writer),
        "pk_is_business_key": flag("PRIMARY KEY" in table_ddl),
        "mysql_reading": mysql,
    }


def judge_ac9_09(facts: Facts) -> Verdict:
    """``AC-9|09``: recomputing the same inputs lands the same rows, not more of them."""
    ok = (
        number(facts["r_runs"]) == len(RECOMPUTE_NODES)
        and facts["r_passed"] == facts["r_runs"]
        and facts["r_bad"] == "-"
        and facts["keys_sorted"] == "yes"
        and facts["upsert_builder"] == "yes"
        and facts["pk_is_business_key"] == "yes"
    )
    readings = (
        f"重算节点: {facts['r_passed']}/{facts['r_runs']} passed"
        + (f"; not green: {facts['r_bad']}" if facts["r_bad"] != "-" else ""),
        f"确定性面: 键集排序 = {facts['keys_sorted']}（不排序则同输入两遍输出顺序可变），"
        f"写侧走 key 级 upsert = {facts['upsert_builder']}，dwd 表的 PRIMARY KEY 就是业务键 = "
        f"{facts['pk_is_business_key']} —— 三者同时成立，重跑才是改同一行而不是多插一行",
        f"真库那一格（e2e，{MYSQL_IDEMPOTENCE_NODE.split('::')[-1]}）本轮读数 = "
        f"{facts['mysql_reading']}，门禁选择式不跑它，所以判定落在上面三条代码面 + 纯函数节点上",
    )
    reason = (
        ""
        if ok
        else "「重算幂等」要的是同一批输入再跑一遍，表里还是那些行：输出必须确定（键排序）、写入"
        "必须是键级 upsert、且这张表的主键确实是业务键 —— 少一条，重算就会变成追加或漂移"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


# --------------------------------------------------------------------------- #
# The probe table
# --------------------------------------------------------------------------- #

PROBES: Final[tuple[Probe, ...]] = (
    Probe(
        item="AC-1|01",
        expects="pyproject.toml",
        summary="项目自我命名与主包（name / version / opendata/）",
        measure=measure_ac1_01,
        judge=judge_ac1_01,
        breaks=(
            Break("项目名不是 opendata", (("name", "pre-rename-name"),), GAP),
            Break("版本号漂移", (("version", "0.0.9"),), GAP),
            Break("主包不再被 git 跟踪", (("opendata_pkg", "no"),), GAP),
            Break("旧的 app/ 布局又出现", (("app_pkg_files", "17"),), GAP),
            Break("打包面把 app 放在第一个", (("first_include", "app*"),), GAP),
        ),
        repair={
            "name": "opendata",
            "version": "0.1.0",
            "opendata_pkg": "yes",
            "app_pkg_files": "0",
            "first_include": "opendata*",
        },
    ),
    Probe(
        item="AC-1|02",
        expects="无旧品牌残留",
        summary="判据列出的 token 清单 == 扫描器执行的清单，且扫描与独立 grep 都为零",
        measure=measure_ac1_02,
        judge=judge_ac1_02,
        breaks=(
            Break("文档与脚本的 token 清单不再相同", (("sets_equal", "no"),), GAP),
            Break(
                "两边都变成空清单（一致但不覆盖）",
                (("doc_tokens", "0"), ("script_tokens", "0")),
                GAP,
            ),
            Break("扫描器变红", (("checker_exit", "1"),), GAP),
            Break(
                "留下一条 `from app.`",
                (
                    ("app_ref_files", "1"),
                    ("app_ref_sample", "opendata/api/x.py"),
                ),
                GAP,
            ),
        ),
        repair={
            "sets_equal": "yes",
            "doc_tokens": "13",
            "checker_exit": "0",
            "app_ref_files": "0",
        },
    ),
    Probe(
        item="AC-1|03",
        expects="裸词",
        summary="裸词 akshare 的白名单是否真的覆盖全部命中面",
        measure=measure_ac1_03,
        judge=judge_ac1_03,
        breaks=(
            Break(
                "白名单外多一个文件",
                (
                    ("outside", "1"),
                    ("outside_sample", "pyproject.toml"),
                ),
                GAP,
            ),
            Break(
                "命中面扩大",
                (
                    ("outside", "40"),
                    ("outside_sample", "Makefile"),
                ),
                GAP,
            ),
        ),
        repair={"outside": "0", "outside_sample": ""},
    ),
    Probe(
        item="AC-1|04",
        expects="改名覆盖补全",
        summary="容器名 / DB 用户 / 根文档 / Makefile·pre-commit 四个面各自成立",
        measure=measure_ac1_04,
        judge=judge_ac1_04,
        breaks=(
            Break("有一个容器名没改", (("foreign", "1"),), GAP),
            Break("init.sql 不再创建 opendata_user", (("user_missing", "init.sql"),), GAP),
            Break(
                "Makefile 引用了不存在的路径",
                (
                    ("missing_paths", "2"),
                    ("missing_sample", "scripts/x.py"),
                ),
                GAP,
            ),
            Break(
                "hook 里有一条命中为零的 pattern",
                (
                    ("stale_patterns", "1"),
                    ("stale_sample", "^akshare/"),
                ),
                GAP,
            ),
            Break(
                "机器面还留着旧 token",
                (
                    ("machine_files", "1"),
                    ("machine_sample", "Makefile"),
                ),
                GAP,
            ),
            Break(
                "根文档用旧容器名下指令（不是沿革引用）",
                (("doc_live", "1"), ("doc_live_sample", "README.md:77"), ("doc_historical", "3")),
                GAP,
            ),
            Break("根文档名字里还留着旧 token", (("stale_docs", "1"),), GAP),
        ),
        repair={
            "foreign": "0",
            "user_missing": "-",
            "machine_files": "0",
            "machine_sample": "",
            "doc_live": "0",
            "doc_live_sample": "",
            "stale_docs": "0",
            "missing_paths": "0",
            "missing_sample": "",
            "stale_patterns": "0",
            "stale_sample": "",
        },
    ),
    Probe(
        item="AC-1|05",
        expects="四处配置面一致",
        summary="双库名四处一致（可静态量）+ 后端/前端/health 运行时（要真机）",
        measure=measure_ac1_05,
        judge=judge_ac1_05,
        breaks=(
            Break("运行时正常但仓库名漂移", (("runtime", "ok"), ("consistent", "no")), GAP),
            Break("四处一致但引擎没起来", (("runtime", "no-engine"), ("consistent", "yes")), GAP),
            Break("引擎在跑但栈没起", (("runtime", "not-started"), ("consistent", "yes")), GAP),
        ),
        repair={"consistent": "yes", "runtime": "ok"},
    ),
    Probe(
        item="AC-1|06",
        expects="默认启动即可用",
        summary="数据仓库容器不再挂在 --profile 上",
        measure=measure_ac1_06,
        judge=judge_ac1_06,
        breaks=(
            Break("仓库容器退回 profile 后面", (("warehouse_profiled", "yes"),), GAP),
            Break("compose 解析出的默认集里没有它", (("in_cli_set", "no"),), GAP),
        ),
        repair={"warehouse_profiled": "no", "in_cli_set": "yes"},
    ),
    Probe(
        item="AC-1|07",
        expects="LICENSE 为 BSL 1.1",
        summary="BSL 四要素 + 搬运许可 + README 三项（含商务联系方式为真实可达）",
        measure=measure_ac1_07,
        judge=judge_ac1_07,
        breaks=(
            Break("四要素少一条", (("missing", "1"),), GAP),
            Break("Change License 不是 MIT", (("change_license", "Apache-2.0"),), GAP),
            Break("README 的免责声明被删", (("disclaimer", "no"),), GAP),
            Break("资源权利小节被删", (("resource_registry", "no"),), GAP),
            Break(
                "商务联系方式换成占位",
                (
                    ("placeholder", "yes"),
                    ("contact", "cloud@example.com（占位）"),
                ),
                GAP,
            ),
        ),
        repair={
            "bsl": "yes",
            "missing": "0",
            "change_license": "MIT License",
            "akshare_license": "yes",
            "notices_sections": "4",
            "resource_registry": "yes",
            "license_notice": "yes",
            "disclaimer": "yes",
            "placeholder": "no",
            "contact": "商务授权联系：ops@opendata.dev",
        },
    ),
    Probe(
        item="AC-1|08",
        expects="数据源权利登记表",
        summary="登记表存在之外：逐行是否真复核过，且覆盖清单引用的每个源",
        measure=measure_ac1_08,
        judge=judge_ac1_08,
        breaks=(
            Break("允许用途列回到待确认", (("undecided", "11"),), GAP),
            Break(
                "某个源在清单里被引用却没有登记行",
                (
                    ("uncovered", "3"),
                    ("uncovered_sample", "tradier"),
                ),
                GAP,
            ),
            Break("一行没写复核日期", (("dated", "15"),), GAP),
            Break("条款链接列写成散文", (("unlinked", "5"),), GAP),
            Break("登记表被清空", (("rows", "0"), ("dated", "0")), GAP),
        ),
        repair={
            "dated": "*rows",
            "undecided": "0",
            "unlinked": "0",
            "uncovered": "0",
            "uncovered_sample": "",
        },
    ),
    Probe(
        item="AC-1|09",
        expects="凭证未入版本库",
        summary="git 面 / 扫描器豁免面 / 上游 5 文件处置 / 全历史扫描器实跑",
        measure=measure_ac1_09,
        judge=judge_ac1_09,
        breaks=(
            Break(
                "字面 grep 命中模板",
                (
                    ("literal", "1"),
                    ("literal_sample", ".env.example"),
                ),
                GAP,
            ),
            Break("真的 .env 进了库", (("strict", "1"),), GAP),
            Break(
                "豁免表里留着非文档路径",
                (
                    ("non_template", "1"),
                    ("non_template_names", "\\.egg-info/"),
                ),
                GAP,
            ),
            Break("规则级豁免又加一条", (("rule_blocks", "1"),), GAP),
            Break("上游 5 文件漏登记一条", (("registered", "4"),), GAP),
            Break("搬运代码里还留着凭证形状字面量", (("live_shapes", "2"),), GAP),
            Break("全历史扫描器变红", (("gitleaks_rc", "1"),), GAP),
        ),
        repair={
            "literal": "0",
            "literal_sample": "",
            "strict": "0",
            "non_template": "0",
            "non_template_names": "",
            "rule_blocks": "0",
            "registered": "*upstream_files",
            "live_shapes": "0",
            "gitleaks_rc": "0",
        },
    ),
    Probe(
        item="AC-1|10",
        expects="拷贝残留已清除",
        summary="残留文件 + 从未进库的文件 + 首次提交的树完整性",
        measure=measure_ac1_10,
        judge=judge_ac1_10,
        breaks=(
            Break(
                "残留一个 .DS_Store",
                (
                    ("residue", "1"),
                    ("residue_sample", ".DS_Store"),
                ),
                GAP,
            ),
            Break("有文件从未进过 git", (("untracked", "3"),), GAP),
            Break(
                "已跟踪文件带着未提交修改",
                (
                    ("dirty", "2"),
                    ("dirty_sample", "Makefile"),
                ),
                GAP,
            ),
            Break("首次提交是空的", (("first_tree", "0"),), GAP),
            Break("首次提交漏掉了测试面", (("essentials_absent", "tests/* (no file)"),), GAP),
        ),
        repair={
            "residue": "0",
            "residue_sample": "",
            "untracked": "0",
            "untracked_sample": "",
            "dirty": "0",
            "dirty_sample": "",
            "git_rc": "0",
            "first_tree": "*tracked_now",
            "essentials_absent": "-",
        },
    ),
    Probe(
        item="AC-2|01",
        expects="P0 域标准化模型",
        summary="六个 P0 模型逐模型双形态 + fail-closed 节点实跑",
        measure=measure_ac2_01,
        judge=judge_ac2_01,
        breaks=(
            Break(
                "一个模型不在参数化清单里",
                (
                    ("covered", "5"),
                    ("missing", "Bar"),
                ),
                GAP,
            ),
            Break("有一条节点不绿", (("passed", "11"),), GAP),
            Break("只跑了一半的节点", (("runs", "6"),), GAP),
            Break("文件里不再两形态都测", (("dual", "no"),), GAP),
        ),
        repair={"covered": "6", "missing": "-", "passed": "*runs", "runs": "12", "dual": "yes"},
    ),
    Probe(
        item="AC-2|02",
        expects="模型就位",
        summary="元数据两模型存在且被测（绿），另一面是被回补/窗口逻辑 import",
        measure=measure_ac2_02,
        judge=judge_ac2_02,
        breaks=(
            Break("单测掉一条", (("passed", "4"),), GAP),
            Break("契约类不在 metadata 里", (("models", "no"),), GAP),
            Break(
                "回补/窗口逻辑一个都不 import 契约",
                (
                    ("importers", "0"),
                    ("importer_names", "-"),
                ),
                GAP,
            ),
        ),
        repair={
            "models": "yes",
            "passed": "*runs",
            "importers": "2",
            "importer_names": "opendata/pipeline/templates.py, opendata/pipeline/runner.py",
        },
    ),
    Probe(
        item="AC-2|03",
        expects="只含不复权 OHLC",
        summary="Bar 字段面 + 合成用例 + 与录制的官方 qfq 序列真对照",
        measure=measure_ac2_03,
        judge=judge_ac2_03,
        breaks=(
            Break("官方对照全部跳过", (("official_passed", "0"), ("official_skipped", "4")), GAP),
            Break("Bar 允许出现复权价", (("bar", "exit=1"),), GAP),
            Break("合成用例失败", (("adjust_failed", "1"),), GAP),
            Break("合成用例只剩一条", (("adjust_passed", "9"),), GAP),
        ),
        repair={
            "bar": "passed",
            "adjust_passed": "10",
            "adjust_failed": "0",
            "official_passed": "2",
            "official_failed": "0",
        },
    ),
    Probe(
        item="AC-2|04",
        expects="三段式协议基类",
        summary="基类三段齐备，且 normalize() 自身承担映射/换算/键规范化",
        measure=measure_ac2_04,
        judge=judge_ac2_04,
        breaks=(
            Break("少一段", (("stages", "2"),), GAP),
            Break("normalize 不做单位换算", (("units", "no"),), GAP),
            Break("键规范化不在这层", (("keynorm", "no"),), GAP),
            Break("有一条单测不绿", (("passed", "4"),), GAP),
            Break("没有 Capability", (("capability", "no"),), GAP),
        ),
        repair={
            "bases": "FetchContext, Fetcher, QueryParams",
            "capability": "yes",
            "stages": "3",
            "rename": "yes",
            "units": "yes",
            "keynorm": "yes",
            "passed": "*runs",
        },
    ),
    Probe(
        item="AC-2|05",
        expects="domains.yaml",
        summary="注册表存在、三处名字由它派生（且无第二处硬写）、派生用例全绿",
        measure=measure_ac2_05,
        judge=judge_ac2_05,
        breaks=(
            Break("rest_path 数量与域数不等", (("rest", "3"),), GAP),
            Break("出现重复 rest_path", (("unique", "no"),), GAP),
            Break(
                "yaml 里又硬写了表名",
                (
                    ("declared", "4"),
                    ("declared_sample", "ods_stock_daily"),
                ),
                GAP,
            ),
            Break("派生用例未全绿", (("derived_passed", "4"),), GAP),
            Break("套件红", (("suite_rc", "1"),), GAP),
        ),
        repair={
            "rest": "*domains",
            "contracts": "*domains",
            "unique": "yes",
            "declared": "0",
            "declared_sample": "",
            "suite_failed": "0",
            "suite_rc": "0",
            "derived_passed": "*runs",
        },
    ),
    Probe(
        item="AC-13|07",
        expects="告警矩阵生效",
        summary="四类各有生产者、各有判定行、各被调度执行器真喂输入，测不到仍记进范围",
        measure=measure_ac13_07,
        judge=judge_ac13_07,
        breaks=(
            Break("pipeline 失败面的一个节点变红", (("kind_passed", "7"),), GAP),
            Break(
                "判据点名的一个节点被改名（那一类再也没人测）",
                (
                    (
                        "kind_bad",
                        "test_a_missing_year_alerts_without_the_matrix_touching_ddl=missing",
                    ),
                ),
                GAP,
            ),
            Break("接线面变红", (("wiring_passed", "3"),), GAP),
            Break("执行器喂入面不再断言", (("wiring_bad", "x=exit=1"),), GAP),
            Break("判据原文不再列全四类", (("kinds_named", "3"),), GAP),
            Break("判定引擎不再产出磁盘水位那一类", (("rules_judged", "3"),), GAP),
            Break(
                "分区缺失没有生产者（分区面只能由调用方手工喂）",
                (("producers", "3"), ("missing_producer", "collect_partitions")),
                GAP,
            ),
            Break(
                "执行器不再喂控制库（pipeline 失败恒读成零告警）",
                (("inputs_fed", "2"), ("input_unfed", "control_engine")),
                GAP,
            ),
            Break(
                "矩阵不再接受年份输入",
                (("inputs_declared", "2"),),
                GAP,
            ),
            Break(
                "范围不再记三类读数（空读数看起来像健康）",
                (("scope_fields", "2"),),
                GAP,
            ),
        ),
        repair={
            "kind_runs": "*kind_runs",
            "kind_passed": "*kind_runs",
            "kind_bad": "-",
            "wiring_runs": "*wiring_runs",
            "wiring_passed": "*wiring_runs",
            "wiring_bad": "-",
            "kinds_named": "4",
            "rules_emitted": "*rules_emitted",
            "rules_judged": "4",
            "producers": "4",
            "missing_producer": "-",
            "inputs_declared": "3",
            "inputs_fed": "3",
            "input_unfed": "-",
            "scope_fields": "3",
        },
    ),
    Probe(
        item="AC-17|03",
        expects="债务不高于基线快照，只降不升",
        summary="棘轮三项存量债务 + 上限自身历史只降不升 + 三个静态平面各读到多少"
        " + 触碰集↔A2 集一一对应",
        measure=measure_ac17_03,
        judge=judge_ac17_03,
        breaks=(
            Break(
                "棘轮自己变红（debt face）", (("ratchet_exit", "1"), ("ratchet_face", "debt")), GAP
            ),
            Break(
                "棘轮以扫描范围变化变红", (("ratchet_exit", "1"), ("ratchet_face", "scope")), GAP
            ),
            Break("三项存量债务有一项没被打印（空读数）", (("printed", "2"),), GAP),
            Break("ruff 存量债务高于快照", (("cur_ruff_selfdev", "243"),), GAP),
            Break("mypy 存量债务高于快照", (("cur_mypy_selfdev", "12"),), GAP),
            Break("bandit 存量债务高于快照", (("cur_bandit_selfdev", "4"),), GAP),
            Break(
                "快照历史上限被抬高过（债务在下一次被洗平）",
                (("raises", "1"), ("raise_detail", "6b780a7->d66dd99 ruff_selfdev 240->260")),
                GAP,
            ),
            Break(
                "近两次转换里有一个被测根从快照里消失",
                (("vanish_recent", "1"), ("vanish_recent_detail", "工作区->6b780a7 alembic")),
                GAP,
            ),
            Break("快照历史短到无从判断只降不升", (("hist_steps", "1"),), GAP),
            Break("一个自研根被 ruff 的 exclude 吞掉", (("ruff_dark", "1"),), GAP),
            Break("ruff 一个自研文件都没走到", (("ruff_walked", "0"),), GAP),
            Break("mypy 排除的文件 ruff 也看不见", (("mypy_dark", "19"),), GAP),
            Break(
                "mypy 的 exclude 排除了一个棘轮没点名的目录",
                (
                    ("mypy_dark", "24"),
                    ("mypy_dark_seen_by_ruff", "24"),
                    ("mypy_dark_legacy", "18"),
                    ("mypy_dark_outside_legacy", "6"),
                    ("mypy_dark_outside_sample", "alembic_data/env.py"),
                ),
                GAP,
            ),
            Break("bandit 的 exclude 漏掉自研文件", (("bandit_dark", "1"),), GAP),
            Break("有一个 tracked .py 不在任何被测面里", (("orphan_py", "1"),), GAP),
            Break("a2-check 自己变红", (("a2_exit", "1"),), GAP),
            Break("A2 零容忍集为空", (("a2_files", "0"),), GAP),
            Break("基线以来没有任何 .py 被触碰（对应面无从成立）", (("touched_py", "0"),), GAP),
            Break(
                "一个触碰过的自研文件被排除出 A2",
                (
                    ("touched_dropped", "1"),
                    ("touched_dropped_selfdev", "1"),
                    ("touched_dropped_sample", "opendata/services/x.py"),
                ),
                GAP,
            ),
        ),
        repair={
            "ratchet_exit": "0",
            "ratchet_face": "none",
            "printed": "3",
            "cur_ruff_selfdev": "*snap_ruff_selfdev",
            "cur_mypy_selfdev": "*snap_mypy_selfdev",
            "cur_bandit_selfdev": "*snap_bandit_selfdev",
            "hist_steps": "*hist_steps",
            "raises": "0",
            "raise_detail": "",
            "vanish_recent": "0",
            "vanish_recent_detail": "",
            "ruff_walked": "*selfdev_disk",
            "ruff_dark": "0",
            "mypy_targets": "*mypy_targets",
            "mypy_dark": "*mypy_dark_seen_by_ruff",
            "mypy_dark_seen_by_ruff": "*mypy_dark_seen_by_ruff",
            "mypy_dark_outside_legacy": "0",
            "bandit_scanned": "*bandit_scanned",
            "bandit_dark": "0",
            "bandit_dark_seen_by_ruff": "0",
            "tracked_py": "*tracked_py",
            "orphan_py": "0",
            "orphan_sample": "",
            "a2_exit": "0",
            "a2_files": "*a2_files",
            "touched_py": "*touched_py",
            "touched_dropped": "0",
            "touched_dropped_selfdev": "0",
            "touched_dropped_sample": "",
        },
    ),
    Probe(
        item="AC-17|05",
        expects="仅 E/F 检查",
        summary="搬运树只按 E/F 计量（豁免量得出来）+ 未重排且逐文件重放一致"
        " + 三处登记与 pre-commit exclude 一致",
        measure=measure_ac17_05,
        judge=judge_ac17_05,
        breaks=(
            Break(
                "棘轮自己变红（debt face）", (("ratchet_exit", "1"), ("ratchet_face", "debt")), GAP
            ),
            Break(
                "棘轮以扫描范围变化变红", (("ratchet_exit", "1"), ("ratchet_face", "scope")), GAP
            ),
            Break("两项搬运债务有一项没被打印", (("printed", "1"),), GAP),
            Break("ruff_ported 高于快照", (("cur_ruff_ported", "2145"),), GAP),
            Break("direct_http_ported 高于快照", (("cur_direct_http_ported", "1045"),), GAP),
            Break(
                "门禁打印的搬运计数不再是 E/F 数（select 被加宽或改窄）",
                (("select_matches", "no"),),
                GAP,
            ),
            Break("门禁的 requests 直连数与独立重数不符", (("http_matches", "no"),), GAP),
            Break(
                "直连计数的动词集合被改窄（少一条就少一批读数）",
                (
                    ("verbs_ok", "no"),
                    ("verbs_declared", "delete, get, head, post, put"),
                ),
                GAP,
            ),
            Break(
                "E/F 豁免不再挡任何事（自研规则集读数不高于 E/F）",
                (("exemption_shown", "no"),),
                GAP,
            ),
            Break(
                "搬运树已被 ruff format 过（与上游不再可 diff）",
                (("format_would_rewrite", "0"),),
                GAP,
            ),
            Break("搬运树 import 顺序已被重排", (("isort_violations", "0"),), GAP),
            Break("重放工具自己退出非 0", (("report_exit", "1"),), GAP),
            Break("重放报告留有待办", (("report_todos", "2"),), GAP),
            Break("报告里有文件重放不一致", (("rows_replay_ok", "0"),), GAP),
            Break("报告表行数与工具自报的文件数不符", (("report_files", "0"),), GAP),
            Break("表行数为 0（没有文件被对照）", (("rows_total", "0"),), GAP),
            Break("磁盘上有一个搬运文件不在上游锁里", (("unrecorded_py", "1"),), GAP),
            Break(
                "现场渲染与已归档报告不一致（归档不可复现）",
                (("render_matches_archive", "no"),),
                GAP,
            ),
            Break("人工改动登记三处各有出入", (("edits_disagree", "1"),), GAP),
            Break("报告里标了人工改动的行数与锁不符", (("rows_manual", "0"),), GAP),
            Break(
                "pre-commit 不再排除搬运树（下一次 hook 会重排它）",
                (("hooks_excluding", "0"),),
                GAP,
            ),
            Break(
                "pre-commit 少了一条 HEAD 里有的排除",
                (("hooks_lost", "1"), ("hooks_lost_detail", "ruff-format")),
                GAP,
            ),
            Break(
                "近两次转换里搬运根从快照里消失",
                (("vanish_recent", "1"), ("vanish_recent_detail", "工作区->6b780a7 opendata_http")),
                GAP,
            ),
        ),
        repair={
            "ratchet_exit": "0",
            "ratchet_face": "none",
            "printed": "2",
            "cur_ruff_ported": "*snap_ruff_ported",
            "cur_direct_http_ported": "*snap_direct_http_ported",
            "select_matches": "yes",
            "http_matches": "yes",
            "verbs_ok": "yes",
            "exemption_shown": "yes",
            "isort_violations": "*isort_violations",
            "format_would_rewrite": "*format_would_rewrite",
            "hist_steps": "*hist_steps",
            "vanish_recent": "0",
            "vanish_recent_detail": "",
            "report_exit": "0",
            "report_todos": "0",
            "report_files": "*lock_records",
            "rows_total": "*lock_records",
            "rows_replay_ok": "*lock_records",
            "rows_manual": "*edits_in_lock",
            "render_matches_archive": "yes",
            "unrecorded_py": "0",
            "unrecorded_sample": "",
            "edits_in_lock": "*edits_in_lock",
            "edits_disagree": "0",
            "hooks_excluding": "*hooks_excluding",
            "hooks_lost": "0",
            "hooks_lost_detail": "",
        },
    ),
    Probe(
        item="AC-17|07",
        expects="docstring 与参数注解覆盖率 100%",
        summary="门禁面（固定目录内 100%/100%）+ 目录外的新增自研文件按同一把尺子再量一遍",
        measure=measure_ac17_07,
        judge=judge_ac17_07,
        breaks=(
            Break("门禁项本身变红", (("tool_exit", "1"),), GAP),
            Break(
                "覆盖面被清空却仍打印 100%（空集合分支）",
                (("in_scope", "0"), ("vacuous", "yes"), ("scope_dirs", "0")),
                GAP,
            ),
            Break("docstring 覆盖率不是 100%", (("doc_pct", "99.8"),), GAP),
            Break("参数注解覆盖率不是 100%", (("ann_pct", "97.0"),), GAP),
            Break(
                "目录外的新增文件里有一个公开 callable 不达标",
                (
                    ("widened_failing", "1"),
                    ("widened_sample", "opendata/services/scheduler.py:88"),
                ),
                GAP,
            ),
            Break(
                "目录外没有任何新增文件被量到（加宽面自己失效）",
                (("widened_files", "0"), ("widened_callables", "0")),
                GAP,
            ),
        ),
        repair={
            "tool_exit": "0",
            "vacuous": "no",
            "doc_pct": "100.0",
            "ann_pct": "100.0",
            "in_scope": "*in_scope",
            "widened_files": "*widened_files",
            "widened_callables": "*widened_callables",
            "widened_failing": "0",
            "widened_sample": "",
        },
    ),
    Probe(
        item="AC-17|08",
        expects="反空壳抽审",
        summary="§5.1 四类空壳全树为零 + §5.2 T1 三问由真实录制信封与预计算黄金向量回答",
        measure=measure_ac17_08,
        judge=judge_ac17_08,
        breaks=(
            Break("自指面又出现一条", (("self_reference", "1"), ("tool_shells", "found")), GAP),
            Break("恒真断言面又出现一条", (("vacuous_assert", "1"), ("tool_shells", "found")), GAP),
            Break("定义抄写面又出现一条", (("constant_shell", "1"), ("tool_shells", "found")), GAP),
            Break(
                "源码形式检查面又出现一条",
                (("source_form_check", "1"), ("tool_shells", "found")),
                GAP,
            ),
            Break(
                "普查只扫了一部分用例文件（其余无人看）",
                (("files_scanned", "140"),),
                GAP,
            ),
            Break(
                "用例文件从未被 git 跟踪（CI 复现不了这一树）",
                (("untracked_tests", "1"),),
                GAP,
            ),
            Break("错误翻译的真实用例不足 3", (("t1_error_cases", "2"), ("tool_t1", "gap")), GAP),
            Break("没有「成功不抛」那一例", (("t1_success_cases", "0"), ("tool_t1", "gap")), GAP),
            Break(
                "normalize 的真实报文不足 3",
                (("t1_normalize_cases", "2"), ("tool_t1", "gap")),
                GAP,
            ),
            Break(
                "黄金向量表被清空",
                (("t1_golden_days", "0"), ("tool_t1", "gap")),
                GAP,
            ),
            Break(
                "夹具被手改过一格",
                (("fixture_sha_ok", "no"), ("tool_t1", "gap")),
                GAP,
            ),
            Break(
                "录制件说不清来源（没有录制器与联调字样）",
                (("provenance", "no"), ("tool_t1", "gap")),
                GAP,
            ),
            Break(
                "一份真实样本都没有",
                (("fixture_cases", "0"), ("tool_t1", "gap")),
                GAP,
            ),
            Break("普查工具自己变红", (("tool_exit", "1"),), GAP),
        ),
        repair={
            "tool_exit": "0",
            "tool_shells": "clean",
            "tool_t1": "met",
            "population": "*population",
            "untracked_tests": "0",
            "files_scanned": "*population",
            "self_reference": "0",
            "vacuous_assert": "0",
            "constant_shell": "0",
            "source_form_check": "0",
            "fixture_cases": "*fixture_cases",
            "fixture_sha_ok": "yes",
            "provenance": "yes",
            "t1_error_cases": "*t1_error_cases",
            "t1_success_cases": "*t1_success_cases",
            "t1_normalize_cases": "*t1_normalize_cases",
            "t1_golden_days": "*t1_golden_days",
        },
    ),
    Probe(
        item="AC-17|10",
        expects="历次里程碑的验证证据",
        summary="evidence-traceability 六个面全零 + 日期两读覆盖全部 gate 日志 + 无档案被删",
        measure=measure_ac17_10,
        judge=judge_ac17_10,
        breaks=(
            Break("追溯工具变红", (("tool_exit", "1"), ("tool_ok", "no")), GAP),
            Break("基线之外出现新违规", (("tool_new", "yes"),), GAP),
            Break("某个里程碑档案缺叙述 README", (("gap_narrative", "1"),), GAP),
            Break("有 gate 日志答不出日期", (("gap_date", "1"),), GAP),
            Break("有 gate 日志答不出身份", (("gap_identity", "1"),), GAP),
            Break("有 gate 日志答不出命令", (("gap_command", "1"),), GAP),
            Break("有 gate 日志没有明确 exit 读数", (("gap_exit", "1"),), GAP),
            Break("有证据文件从未被 git 跟踪", (("gap_untracked", "1"),), GAP),
            Break(
                "日期只数抬头、不再认运行自己打下的时间戳",
                (("dated_run", "0"),),
                GAP,
            ),
            Break(
                "档案被删除后追溯断链（逐面扫描看不见消失）",
                (("gone_archives", "4"),),
                GAP,
            ),
            Break(
                "普查里一个 gate 日志都没有：全零只是空转",
                (("logs_total", "0"), ("dated_header", "0"), ("dated_run", "0")),
                GAP,
            ),
        ),
        repair={
            "tool_exit": "0",
            "tool_ok": "yes",
            "tool_new": "no",
            "gap_narrative": "0",
            "gap_date": "0",
            "gap_identity": "0",
            "gap_command": "0",
            "gap_exit": "0",
            "gap_untracked": "0",
            "gone_archives": "0",
            "rounds": "*rounds",
            "census": "*census",
            "logs_total": "*logs_total",
            "dated_header": "*dated_header",
            "dated_run": "*dated_run",
        },
    ),
    Probe(
        item="AC-18|01",
        expects="新鲜度接口与告警",
        summary="新鲜度门交出日期/滞后/基准日 + 缺失告警真到达通道 + 调度执行器接线",
        measure=measure_ac18_01,
        judge=judge_ac18_01,
        breaks=(
            Break("门的一个节点变红", (("query_passed", "5"),), GAP),
            Break(
                "门的节点被改名（探针再也找不到判据问的那一问）",
                (("query_bad", "x=missing"),),
                GAP,
            ),
            Break("告警投递面变红", (("alert_passed", "8"),), GAP),
            Break("缺失不再触发告警", (("alert_bad", "告警=exit=1"),), GAP),
            Break("新鲜度路由整条消失", (("door_route", "no"),), GAP),
            Break("门不再交出滞后天数", (("door_readings", "no"),), GAP),
            Break("门只在成功分支报基准日，no data 分支不报", (("door_baseline", "1"),), GAP),
            Break("调度模板不再派发新鲜度执行器", (("job_wired", "no"),), GAP),
            Break("执行器不再把告警推到 WS 通道", (("job_broadcasts", "no"),), GAP),
            Break("新鲜度被从可执行模板 kinds 里摘掉", (("job_executable", "no"),), GAP),
        ),
        repair={
            "query_runs": "*query_runs",
            "query_passed": "*query_runs",
            "query_bad": "-",
            "alert_runs": "*alert_runs",
            "alert_passed": "*alert_runs",
            "alert_bad": "-",
            "door_route": "yes",
            "door_readings": "yes",
            "door_baseline": "2",
            "job_wired": "yes",
            "job_broadcasts": "yes",
            "job_executable": "yes",
        },
    ),
    Probe(
        item="AC-18|02",
        expects="数据目录页/接口",
        summary="按域一行五个读数：接口给得出、页面显示得出、单元面与真机页各自断言过",
        measure=measure_ac18_02,
        judge=judge_ac18_02,
        breaks=(
            Break("目录接口面变红", (("passed", "6"),), GAP),
            Break("目录节点被改名（那一读再也没有人测）", (("bad", "x=missing"),), GAP),
            Break("覆盖标的数只剩列名（断言被删）", (("readings_missing", "覆盖标的数"),), GAP),
            Break("质量标记列被摘掉", (("readings_missing", "质量标记"),), GAP),
            Break("真机页不再逐读数断言", (("e2e_missing", "新鲜度"),), GAP),
            Break("载荷类型不再带 coverage/quality 字段", (("fields", "3"),), GAP),
            Break("页面不再显示滞后基准日", (("header", "no"),), GAP),
            Break("单元面被采集器的排除规则吞掉", (("collector_ok", "no"),), GAP),
            Break("下钻查询被摘掉", (("drilldown", "no"),), GAP),
        ),
        repair={
            "runs": "*runs",
            "passed": "*runs",
            "bad": "-",
            "readings_missing": "-",
            "e2e_missing": "-",
            "fields": "5",
            "header": "yes",
            "collector_ok": "yes",
            "drilldown": "yes",
        },
    ),
    Probe(
        item="AC-18|03",
        expects="函数级明细下钻",
        summary="数据接口页是否已并入目录（目录为默认视图，函数级明细在下钻里）",
        measure=measure_ac18_03,
        judge=judge_ac18_03,
        breaks=(
            Break("又出现第二个数据接口页面", (("nav_entries", "2"),), GAP),
            Break("数据接口路由不再渲染目录视图", (("route_is_catalog", "no"),), GAP),
            Break("目录页读不到函数级明细", (("detail_in_catalog", "no"),), GAP),
            Break("下钻终点（接口详情路由）被删", (("detail_route", "no"),), GAP),
        ),
        repair={
            "nav_entries": "1",
            "page_titles": "数据接口（合并页）",
            "route_is_catalog": "yes",
            "detail_in_catalog": "yes",
            "detail_route": "yes",
        },
    ),
    Probe(
        item="AC-9|02",
        expects="注入差异样本被校对作业捕获",
        summary="每周校对有真的触发器，注入的差异被比出来并逐列写进 dq_diff_report",
        measure=measure_ac9_02,
        judge=judge_ac9_02,
        breaks=(
            Break(
                "注入差异没被捕获（节点变红）",
                (("capture_passed", "6"), ("capture_bad", "test_x=exit=1")),
                GAP,
            ),
            Break("捕获面节点被改名（探针再也找不到那一问）", (("capture_runs", "6"),), GAP),
            Break("判据点名的列没进记录面", (("columns_missing", "偏差→deviation"),), GAP),
            Break("列名进了表定义但行对象不带这一列", (("row_fields_missing", "verdict"),), GAP),
            Break("表名从写入代码里消失", (("table_named", "no"),), GAP),
            Break("报告写入不在生产装配路径上", (("writer_wired", "no"),), GAP),
            Break(
                "full_check 不再是可执行 kind（那行 cron 装不上调度器）",
                (("job_executable", "no"),),
                GAP,
            ),
            Break("模板派发不再认识 full_check", (("job_dispatched", "no"),), GAP),
            Break("执行器只建服务不跑比较", (("job_runs_service", "no"),), GAP),
            Break("窗口又按日历形状取（覆盖缺口会被当成差异）", (("window_from_data", "no"),), GAP),
            Break("schedules.yaml 里那一行被删", (("schedule_row", "no"),), GAP),
        ),
        repair={
            "capture_passed": "*capture_runs",
            "capture_bad": "-",
            "columns_missing": "-",
            "row_fields_missing": "-",
            "table_named": "yes",
            "writer_wired": "yes",
            "job_executable": "yes",
            "job_dispatched": "yes",
            "job_runs_service": "yes",
            "window_from_data": "yes",
            "schedule_row": "yes",
        },
    ),
    Probe(
        item="AC-9|04",
        expects="白名单容忍项生效",
        summary="治理三则各有来源与寿命：白名单读得到、去重与差异率基线跨比对、被抑制也留痕",
        measure=measure_ac9_04,
        judge=judge_ac9_04,
        breaks=(
            Break("治理判定节点变红", (("gov_passed", "8"), ("gov_bad", "test_x=exit=1")), GAP),
            Break("治理节点被改名（某一则再也没人答）", (("gov_runs", "8"),), GAP),
            Break("治理文件形状坏了（读出来不是那两个键）", (("file_shape", "no"),), GAP),
            Break("容忍项没有记录凭据", (("reason_missing", "1"),), GAP),
            Break("白名单不再被判定读到", (("whitelist_read", "no"),), GAP),
            Break("去重集不再参与判定", (("dedupe_read", "no"),), GAP),
            Break("差异率基线不再参与判定", (("rate_read", "no"),), GAP),
            Break("突增不再升级到 critical", (("escalates", "no"),), GAP),
            Break(
                "策略退回每次新建（去重与基线只剩一次调用内成立）",
                (("shared_singleton", "no"),),
                GAP,
            ),
            Break("白名单没有来源文件", (("whitelist_has_source", "no"),), GAP),
            Break("校对服务不再用共享策略", (("policy_in_check", "no"),), GAP),
            Break("被抑制的决定不再交给 notifier 记录", (("quiet_recorded", "no"),), GAP),
        ),
        repair={
            "gov_passed": "*gov_runs",
            "gov_bad": "-",
            "file_shape": "yes",
            "reason_missing": "0",
            "whitelist_read": "yes",
            "dedupe_read": "yes",
            "rate_read": "yes",
            "escalates": "yes",
            "shared_singleton": "yes",
            "whitelist_has_source": "yes",
            "policy_in_check": "yes",
            "quiet_recorded": "yes",
        },
    ),
    Probe(
        item="AC-9|05",
        expects="告警双通道",
        summary="一次判定同时到 WS 广播、按域过滤的 hub 与 SMTP 邮件，且每条通道的结果可报告",
        measure=measure_ac9_05,
        judge=judge_ac9_05,
        breaks=(
            Break("投递节点变红", (("ch_passed", "9"), ("ch_bad", "test_x=exit=1")), GAP),
            Break("投递节点被改名", (("ch_runs", "9"),), GAP),
            Break("WS 事件名不再是 data.diff_alert", (("event_named", "no"),), GAP),
            Break("广播函数存在但没人调用", (("ws_called", "no"),), GAP),
            Break("订阅 hub 不再被调用", (("hub_called", "no"),), GAP),
            Break("hub 不再按订阅域过滤", (("hub_filters", "no"),), GAP),
            Break("notify 只喂其中一条通道", (("both_channels_in_notify", "no"),), GAP),
            Break("邮件不再渲染正文", (("mail_rendered", "no"),), GAP),
            Break("邮件不走共享 SMTP 传输", (("mail_transport", "no"),), GAP),
            Break("SMTP 配置门只看一个键", (("settings_gated", "1"),), GAP),
            Break("装配时少交一条通道", (("production_channels", "3"),), GAP),
            Break("投递结果不再逐通道报告", (("reported", "no"),), GAP),
            Break("调用不留投递记录", (("recorded", "no"),), GAP),
        ),
        repair={
            "ch_passed": "*ch_runs",
            "ch_bad": "-",
            "event_named": "yes",
            "ws_called": "yes",
            "hub_called": "yes",
            "hub_filters": "yes",
            "both_channels_in_notify": "yes",
            "mail_rendered": "yes",
            "mail_transport": "yes",
            "settings_gated": "2",
            "production_channels": "4",
            "reported": "yes",
            "recorded": "yes",
        },
    ),
    Probe(
        item="AC-9|03",
        expects="反例用例",
        summary="两种错输入各有一个会响的判定，且都跑在 compare_source_frames 这个生产入口上",
        measure=measure_ac9_03,
        judge=judge_ac9_03,
        breaks=(
            Break("一条反例变红", (("x_passed", "3"), ("x_bad", "test_x=exit=1")), GAP),
            Break("反例节点被改名，判据条数跑不满", (("x_runs", "3"),), GAP),
            Break(
                "字段名不匹配只测下游 helper，不测校对作业真正调的函数",
                (("entry_is_production", "no"),),
                GAP,
            ),
            Break("字段名不匹配不再要求报错", (("field_name_raises", "no"),), GAP),
            Break("单位 mismatch 断言不点名字段", (("unit_mismatch", "no"),), GAP),
            Break(
                "tolerance 配退回一侧整本字典交出（另一侧的紧声明可能从未生效）",
                (("whole_dictionary_gone", "no"),),
                GAP,
            ),
            Break("逐字段合并不再取紧的那条", (("tighter_wins", "no"),), GAP),
            Break("只量 a 紧 b 松，另一个方向没量", (("both_orders", "no"),), GAP),
        ),
        repair={
            "x_passed": "*x_runs",
            "x_bad": "-",
            "entry_is_production": "yes",
            "field_name_raises": "yes",
            "unit_mismatch": "yes",
            "whole_dictionary_gone": "yes",
            "tighter_wins": "yes",
            "both_orders": "yes",
        },
    ),
    Probe(
        item="AC-9|08",
        expects="单源域 dwd 直通模式可用",
        summary="歧义键逐键拒绝（不再整窗不交）+ layer=dwd 走生产查询面 + 表里真的有行",
        measure=measure_ac9_08,
        judge=judge_ac9_08,
        breaks=(
            Break("一条直通节点变红", (("p_passed", "5"), ("p_bad", "test_x=exit=1")), GAP),
            Break("直通节点被改名，判据条数跑不满", (("p_runs", "5"),), GAP),
            Break("歧义键退回整窗拒绝", (("refuses_per_key", "no"),), GAP),
            Break("旧的整窗 raise 文案又出现", (("whole_window_raise_gone", "no"),), GAP),
            Break("拒绝掉的键不再报得出来", (("reported", "no"),), GAP),
            Break("键列缺失不再 fail closed", (("raises_on_missing_column", "no"),), GAP),
            Break("查询面不走生产 _key/build_data_select", (("query_face", "no"),), GAP),
            Break(
                "代码面全通，但 dwd_stock_action 还是 0 行（缺一次经确认的落库写入）",
                (("landed_rows", "0"),),
                GAP,
            ),
        ),
        repair={
            "p_passed": "*p_runs",
            "p_bad": "-",
            "refuses_per_key": "yes",
            "reported": "yes",
            "whole_window_raise_gone": "yes",
            "raises_on_missing_column": "yes",
            "query_face": "yes",
            "landed_rows": "55071",
        },
    ),
    Probe(
        item="AC-9|09",
        expects="dwd 重算幂等",
        summary="同输入重算逐格不动 + 键集排序 + 写侧 key 级 upsert + dwd 主键就是业务键",
        measure=measure_ac9_09,
        judge=judge_ac9_09,
        breaks=(
            Break("一条重算节点变红", (("r_passed", "3"), ("r_bad", "test_x=exit=1")), GAP),
            Break("重算节点被改名，判据条数跑不满", (("r_runs", "3"),), GAP),
            Break("键集不再排序（同输入两遍输出顺序可变）", (("keys_sorted", "no"),), GAP),
            Break("写侧退回追加而不是 key 级 upsert", (("upsert_builder", "no"),), GAP),
            Break(
                "dwd 表主键不再就是业务键（重跑就是多一行）",
                (("pk_is_business_key", "no"),),
                GAP,
            ),
        ),
        repair={
            "r_passed": "*r_runs",
            "r_bad": "-",
            "keys_sorted": "yes",
            "upsert_builder": "yes",
            "pk_is_business_key": "yes",
        },
    ),
)


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def item_key(group: str, index: int, text: str) -> str:
    """Reproduce the ledger's identity rule, so a re-worded item strands its probe."""
    normalized = " ".join(text.split()).lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{group}|{index:02d}|{digest}"


def parse_items(text: str) -> list[DocItem]:
    """Read the checklist out of the acceptance document using the ledger's keying rule."""
    items: list[DocItem] = []
    group: str | None = None
    seen: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        heading = AC_HEADING.match(line)
        if heading:
            group = heading.group(1)
            continue
        section = SECTION_HEADING.match(line)
        if section:
            group = None
            continue
        box = ITEM_LINE.match(line)
        if box and group:
            seen[group] = seen.get(group, 0) + 1
            index = seen[group]
            body = box.group(2).strip()
            items.append(
                DocItem(
                    key=item_key(group, index, body),
                    group=group,
                    index=index,
                    line=number,
                    text=body,
                    ticked=box.group(1).lower() == "x",
                )
            )
    return items


def load_context() -> Context:
    """Parse the document and the ledger once, for every probe that runs afterwards."""
    doc = REPO_ROOT / DOC_REL
    if not doc.is_file():
        raise ProbeError(f"acceptance document not found: {DOC_REL}")
    ledger_path = REPO_ROOT / LEDGER_REL
    if not ledger_path.is_file():
        raise ProbeError(f"ledger not found: {LEDGER_REL}: run the ledger check's --scaffold")
    raw = json.loads(ledger_path.read_text(encoding="utf-8"))
    entries = raw.get("items")
    if not isinstance(entries, dict):
        raise ProbeError(f"`items` in {LEDGER_REL} is not an object")
    ledger = {key: value for key, value in entries.items() if isinstance(value, dict)}
    return Context(
        root=REPO_ROOT,
        doc_items=tuple(parse_items(doc.read_text(encoding="utf-8"))),
        ledger=ledger,
    )


def probe_for(item: str) -> Probe:
    """Look a probe up by its item id."""
    for probe in PROBES:
        if probe.item == item:
            return probe
    raise ProbeError(
        f"no probe for {item} ({len(PROBES)} registered: {', '.join(p.item for p in PROBES)})"
    )


def wording_drift(ctx: Context, probe: Probe) -> str:
    """An error string when a criterion's wording no longer matches what its probe measures."""
    doc = ctx.item(probe.item)
    if probe.expects in doc.text:
        return ""
    return (
        f"{probe.item}: the criterion no longer contains {probe.expects!r}; this probe measures a "
        "different claim than the document now states"
    )


def show(ctx: Context, probe: Probe) -> dict[str, object]:
    """Print one item's reading and return it as a record."""
    doc = ctx.item(probe.item)
    entry = ctx.ledger_entry(probe.item)
    drift = wording_drift(ctx, probe)
    print(f"\n### {probe.item}  (document line {doc.line})")
    print(f"判据原文：{doc.text}")
    print(f"本探针：{probe.summary}")
    print(f"台账现状：state={entry.get('state', '?')}，文档勾选={doc.ticked}")
    if drift:
        print(f"VERDICT {probe.item}: drift — {drift}")
        return {"item": probe.item, "key": doc.key, "state": "drift", "findings": [drift]}
    facts = probe.measure(ctx)
    verdict = probe.judge(facts)
    for reading in verdict.readings:
        print(f"  - {reading}")
    hint = ""
    if verdict.state == GAP:
        hint = closure_hint(probe, facts)
        if hint:
            print(f"  - 补齐到可判：{hint}")
    suffix = f" — {verdict.reason}" if verdict.reason else ""
    print(f"VERDICT {probe.item}: {verdict.state}{suffix}")
    return {
        "item": probe.item,
        "key": doc.key,
        "state": verdict.state,
        "summary": probe.summary,
        "readings": list(verdict.readings),
        "reason": verdict.reason,
        "closure": hint,
        "facts": facts,
    }


def closure_hint(probe: Probe, facts: Facts) -> str:
    """Name the faces whose measured reading differs from the declared clean one."""
    clean = resolve_repair(facts, probe.repair)
    moved = [
        f"{key}: {facts.get(key, '(absent)')} -> {clean[key]}"
        for key in sorted(probe.repair)
        if facts.get(key) != clean[key]
    ]
    return "; ".join(moved)


def self_test(ctx: Context) -> int:
    """Apply every counterfact to a *clean* reading and fail when it does not bite.

    This checks the judges, not the measurements. Two properties have to hold per item:

    * ``proven`` is reachable -- the declared ``repair`` reading makes the judge pass. A judge
      that cannot be satisfied is how a red light becomes a permanent one.
    * every counterfact flips that clean reading back to a gap. A break applied to an item that
      is already red proves nothing, which is why the measured facts are not the start point.
    """
    failures: list[str] = []
    for probe in PROBES:
        drift = wording_drift(ctx, probe)
        if drift:
            failures.append(drift)
            continue
        try:
            facts = probe.measure(ctx)
        except (ProbeError, KeyError, ValueError) as exc:
            failures.append(f"{probe.item}: cannot measure: {exc}")
            continue
        stray_repair = sorted(set(probe.repair) - set(facts))
        if stray_repair:
            failures.append(
                f"{probe.item}: repair states facts no measure produces: {stray_repair}"
            )
            continue
        clean = resolve_repair(facts, probe.repair)
        if probe.judge(clean).state != PROVEN:
            failures.append(
                f"{probe.item}: no reading this probe declares can make the judge pass, so a gap "
                "here could never be closed by real work"
            )
            continue
        for brk in probe.breaks:
            if brk.expect != GAP:
                failures.append(
                    f"{probe.item} / {brk.label}: a counterfact has to promise a gap; the repair "
                    "is what states the proven case"
                )
                continue
            stray = sorted({key for key, _ in brk.facts} - set(facts))
            if stray:
                failures.append(
                    f"{probe.item} / {brk.label}: states facts no measure produces: {stray}"
                )
                continue
            mutated = dict(clean)
            mutated.update(brk.facts)
            if probe.judge(mutated).state != GAP:
                failures.append(
                    f"{probe.item} / {brk.label}: the judge still says proven, so the item is not "
                    "really gated on this face"
                )
    if failures:
        print("FAIL: item-probe self test:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    breaks = sum(len(probe.breaks) for probe in PROBES)
    print(
        f"OK: {len(PROBES)} probe(s) measured; every judge is reachable from its declared repair, "
        f"and all {breaks} counterfact(s) flip a clean reading back to a gap."
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the probes as a command-line tool."""
    parser = argparse.ArgumentParser(description="Recompute individual acceptance items")
    parser.add_argument("--list", action="store_true", help="print the probe table and exit")
    parser.add_argument("--all", action="store_true", help="run every probe")
    parser.add_argument("--item", action="append", default=[], help="run one AC-N|NN (repeatable)")
    parser.add_argument("--self-test", action="store_true", help="prove every judge bites")
    parser.add_argument("--json", metavar="PATH", help="also write the readings as JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.list:
        for probe in PROBES:
            print(f"{probe.item}  {probe.summary}")
        return 0
    if not (args.all or args.item or args.self_test):
        parser.error("choose --all, --item AC-N|NN, or --self-test")

    try:
        ctx = load_context()
    except (ProbeError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if args.self_test:
        rc = self_test(ctx)
        if not (args.all or args.item):
            return rc
        if rc:
            return rc

    selected = [probe_for(item) for item in args.item] if args.item else list(PROBES)
    counts: dict[str, int] = {}
    records: list[dict[str, object]] = []
    for probe in selected:
        record = show(ctx, probe)
        state = str(record["state"])
        counts[state] = counts.get(state, 0) + 1
        records.append(record)

    print("\ncensus: " + ", ".join(f"{state}={n}" for state, n in sorted(counts.items())))
    print(f"（{len(selected)} item(s) recomputed against {LEDGER_REL}）")
    if args.json:
        payload = json.dumps(
            {"env": {"python": sys.version.split()[0]}, "items": records},
            indent=2,
            ensure_ascii=False,
        )
        Path(args.json).write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {args.json}")
    return 1 if counts.get("drift") else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProbeError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
