#!/usr/bin/env python3
"""Recompute the AC-1 and AC-2 acceptance criteria one item at a time.

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
import json
import re
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Final

import tomllib

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

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
    """The source of one module-level function, so a judge can read what it actually does."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    return ""


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
    raise ProbeError(f"no probe for {item} (this round covers AC-1 and AC-2 only)")


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
