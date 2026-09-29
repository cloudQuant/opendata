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

``--gate-check`` is the mode that makes the rest of this file load-bearing: it measures every
probe once and reads that single pass five ways -- the counterfact self-test, criterion wording
drift, a ledger<->reading reconciliation, the frozen moment/plane baseline in
``docs/quality/acceptance-probe-faces.json``, and its own membership in the ``gate:`` recipe.
Before round C50 the tool existed but nothing ran it between a ledger flip and the commit, and
``--all`` compared nothing, so three cells the ledger called ``proven`` read ``gap`` while exiting
0. The reconciliation is the delicate one: cells whose reading depends on this *moment* (uncommitted
work, the index, which commit HEAD is) turn red on any round that leaves evidence behind, so those
are judged only on a quiet tree and named as ``deferred`` otherwise. ``--sync-faces`` is the only
writer of the baseline, so a probe declaring itself stable becomes a diff a reviewer signs off.
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
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Final

import tomllib
import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_REL: Final = "docs/迭代计划/迭代1-重构数据中台/验收文档.md"
LEDGER_REL: Final = "docs/quality/acceptance-item-ledger.json"

PROVEN: Final = "proven"
GAP: Final = "gap"

#: Where the frozen moment/coverage baseline lives. Written only by ``--sync-faces``, read by
#: ``--gate-check``: a moment-dependent probe becoming "stable" has to be a diff a reviewer saw.
FACES_REL: Final = "docs/quality/acceptance-probe-faces.json"

#: The gate member this tool becomes. Its absence from the ``gate:`` recipe is a finding the
#: recipe itself cannot hide, because ``tests/test_acceptance_probe_gate.py`` reads the recipe.
GATE_MEMBER: Final = "acceptance-probe-check"

#: What AC-17|01 names as the developer view: a whole-tree static check is a permanent red light
#: on a debt-carrying tree, so these belong to the ratchet instead of to the gate.
DEV_VIEW_TARGETS: Final = ("lint", "typecheck", "security", "format")

#: Source markers whose presence in a measure's call closure means the reading is about *this
#: moment* (uncommitted work, the index, or which commit HEAD happens to be) rather than about the
#: repository. Measured in ``docs/evidence/C50/census-run2-final-readings.txt`` faces 4 and 5, and
#: demonstrated by the same-commit pair in ``docs/evidence/C50/moment-pair-same-head.txt``: those
#: three cells read ``gap`` with this round's archives uncommitted and ``proven`` at the same
#: commit with a clean tree, which is why they are judged only when the tree is quiet.
MOMENT_MARKERS: Final = (
    ('"git", "status"', "worktree(git status)"),
    ('"git", "ls-files"', "index(git ls-files)"),
    ("ctx.tracked()", "index(ctx.tracked)"),
    ("self._tracked", "index(ctx.tracked)"),
    ('"git", "show"', "history(git show REV:)"),
    ('"git", "log"', "history(git log)"),
    ('"git", "ls-tree"', "history(git ls-tree)"),
)
MOMENT_SURFACES: Final = frozenset(label for _, label in MOMENT_MARKERS)

#: One line of the gate recipe that launches a member in its own sub-make. The ``$(MAKE)`` is
#: already expanded in a ``make -n`` listing, so both faces count the same shape.
MAKE_SUBCALL = re.compile(r"--no-print-directory\s+([A-Za-z0-9._-]+)")
GATE_BANNER = re.compile(r"===== gate: ([A-Za-z0-9._-]+) =====")

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


def map_entry_field(source: str, name: str, key: str, field: str) -> str:
    """One keyword value of an entry in a module-level ``NAME = {...}`` table.

    AST again rather than a regex, for the reason :func:`set_literal_members` records: one more
    entry rewraps the literal, and a pattern written against one line then reports a leg that is
    in the table as missing -- which an item judge would read as the dispatch being absent.
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
        if not any(isinstance(item, ast.Name) and item.id == name for item in targets):
            continue
        if not isinstance(literal, ast.Dict):
            continue
        for entry_key, entry_value in zip(literal.keys, literal.values, strict=True):
            if not (isinstance(entry_key, ast.Constant) and entry_key.value == key):
                continue
            if not isinstance(entry_value, ast.Call):
                continue
            for keyword in entry_value.keywords:
                if keyword.arg == field and isinstance(keyword.value, ast.Constant):
                    return str(keyword.value.value)
    return "(absent)"


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
# Makefile and call-closure primitives (AC-17|01, --gate-check, --sync-faces)
# --------------------------------------------------------------------------- #

TARGET_LINE = re.compile(r"^([A-Za-z0-9._-]+):(?:[^=]|$)")


def make_recipes(text: str) -> dict[str, list[str]]:
    """Map each Makefile target to its recipe lines, tabs included.

    Only the shape a reader needs to say whether an item can fail: the commands under a target,
    and whether that list is empty. A target declared without commands is a no-op, which is the
    one thing a gate member must never be.
    """
    recipes: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        if not line.startswith(("\t", " ")):
            named = TARGET_LINE.match(line)
            current = named.group(1) if named else ""
            if named:
                recipes.setdefault(current, [])
            continue
        if current and line.strip():
            recipes.setdefault(current, []).append(line.strip())
    return recipes


def gate_recipe(text: str) -> list[str]:
    """The ``gate:`` target's own recipe lines."""
    return make_recipes(text).get("gate", [])


def launched_by_line(line: str) -> list[str]:
    """Every member a single gate recipe line launches (more than one is a masking shape)."""
    return MAKE_SUBCALL.findall(line)


def gate_members(text: str) -> list[str]:
    """The gate's members, in the order the recipe runs them."""
    return [name for line in gate_recipe(text) for name in launched_by_line(line)]


def gate_banners(text: str) -> list[str]:
    """The section banners the recipe prints, in order.

    Each member is announced by the line right before its sub-make, and the recipe ends with a
    ``PASSED`` banner that make can only reach if nothing aborted. So the expected shape is
    ``banners == members + 1`` with the last one named ``PASSED`` -- a member that stopped being
    announced is how a red item reaches the summary unnamed.
    """
    return [name for line in gate_recipe(text) for name in GATE_BANNER.findall(line)]


def banner_pairing(members: Sequence[str], banners: Sequence[str]) -> tuple[bool, int]:
    """Whether every member is announced in order and the run closes with ``PASSED``.

    Returns:
        ``(paired, unpaired)``: the shape holds overall, plus how many member positions disagree.
    """
    shape = len(banners) == len(members) + 1 and bool(banners) and banners[-1] == "PASSED"
    unpaired = sum(
        1
        for position, name in enumerate(members)
        if position >= len(banners) or banners[position] != name
    )
    return shape and not unpaired, unpaired


def masking_lines(text: str) -> tuple[list[str], list[str], list[str]]:
    """Split the gate recipe into the three shapes that let a sub-failure reach the summary.

    Returns:
        ``(ignore_prefix, echo_mask, chained)``: lines whose failure make is told to ignore, lines
        that swallow a non-zero exit behind ``||``, and lines launching more than one member.
    """
    ignore: list[str] = []
    masked: list[str] = []
    chained: list[str] = []
    for line in gate_recipe(text):
        bare = line.lstrip("-@+ ")
        if line.startswith("-"):
            ignore.append(bare[:70])
        if "||" in bare and re.search(r"\|\|\s*(echo|true|:)\b", bare):
            masked.append(bare[:70])
        if len(launched_by_line(bare)) > 1:
            chained.append(bare[:70])
    return ignore, masked, chained


def top_level_defs(source: str) -> dict[str, ast.FunctionDef]:
    """Index a module's top-level function definitions by name."""
    found: dict[str, ast.FunctionDef] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef):
            found.setdefault(node.name, node)
    return found


def surfaces_in_closure(measure_name: str, source: str) -> tuple[str, ...]:
    """Which moment-bearing surfaces a measure reaches, walked over source rather than luck.

    The walk covers the module-level helpers the measure calls, so a probe that reaches a git
    reading through a shared helper is classified the same as one that shells out inline. Reading
    source and not this run's behaviour is the point: a face that touches ``git status`` is
    moment-dependent even on the pass that happened to see a clean tree.

    Args:
        measure_name: Name of the probe's measure function.
        source: This module's own source text.

    Returns:
        Sorted surface labels; empty when the closure only reads file contents.
    """
    defs = top_level_defs(source)
    seen: set[str] = set()
    queue = [measure_name]
    parts: list[str] = []
    while queue:
        current = queue.pop()
        node = defs.get(current)
        if node is None or current in seen:
            continue
        seen.add(current)
        parts.append(ast.unparse(node))
        for ref in ast.walk(node):
            reached = ref.id if isinstance(ref, ast.Name) else getattr(ref, "attr", "")
            if reached in defs and reached not in seen:
                queue.append(reached)
    body = "\n".join(parts).replace("'", '"')
    return tuple(sorted({label for marker, label in MOMENT_MARKERS if marker in body}))


def probe_surfaces(probe: Probe, source: str) -> tuple[str, ...]:
    """The moment-bearing surfaces of one probe."""
    name = getattr(probe.measure, "__name__", "")
    return surfaces_in_closure(name, source)


def own_source() -> str:
    """This file's source, which is what the surface walk reads."""
    return Path(__file__).read_text(encoding="utf-8", errors="replace")


def worktree_quiet() -> tuple[bool, str]:
    """Whether git sees nothing but committed work, plus what it says when it does not.

    ``--gate-check`` uses this to decide whether the moment-dependent cells may be judged at all.
    A round is by construction a tree with something in flight, so judging those cells there would
    red-light the round for the act of leaving evidence behind.
    """
    code, out = run_argv(["git", "status", "--porcelain", "--untracked-files=all"])
    if code != 0:
        return False, f"git status exit {code}"
    lines = [line for line in out.splitlines() if line.strip()]
    if not lines:
        return True, ""
    return False, f"{len(lines)} entry(ies), first: {lines[0]}"


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
#: The shape ``report_port.py`` uses for one row per ported file, and the only shape ``AC-17|05``
#: counts. Any other table in the same report that starts its rows with this prefix is silently
#: added to that file count, so ``AC-5|06`` measures the collision instead of assuming it away.
REPLAY_ROW_PREFIX: Final = "| `"
#: The section ``report_port.py`` derives from ``datasets.py``'s raise statements -- the register
#: half of ``AC-5|06``'s second branch.
RESOURCE_REGISTER_MARKER: Final = "内置资源不可用登记"
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
    rows = [line for line in text.splitlines() if line.startswith(REPLAY_ROW_PREFIX)]
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
# AC-17|01 -- the gate judged as a gate: members that can each actually fail
# --------------------------------------------------------------------------- #

#: The recipe AC-17|01 is about, and the two commands its "green in a reproducible environment"
#: clause is decided by. ``make -n`` resolves the recipe without running it, so this face asks
#: make itself -- not this file's own text -- how many members the gate actually launches.
MAKEFILE_REL: Final = "Makefile"
GATE_DRY_RUN: Final = ("make", "-n", "gate")
IMPORT_PROBE: Final = "import opendata, sys; print(opendata.__file__)"


def measure_ac17_01(ctx: Context) -> Facts:
    """Read the gate recipe for the shapes that decide whether a member can bite."""
    text = ctx.read(MAKEFILE_REL)
    recipes = make_recipes(text)
    members = gate_members(text)
    banners = gate_banners(text)
    paired, unpaired = banner_pairing(members, banners)
    without = [name for name in members if not recipes.get(name)]
    ignore, masked, chained = masking_lines(text)
    dev_view = [name for name in members if name in DEV_VIEW_TARGETS]
    named = [name for name in BACKTICK.findall(ctx.item("AC-17|01").text) if name in members]
    dry_code, dry_out = run_argv(list(GATE_DRY_RUN))
    dry_members = [name for line in dry_out.splitlines() for name in MAKE_SUBCALL.findall(line)]
    import_code, import_out = run_argv([sys.executable, "-c", IMPORT_PROBE])
    where = import_out.strip().splitlines()[-1] if import_out.strip() else ""
    return {
        "members": count(len(members)),
        "member_list": ", ".join(members),
        "banners": count(len(banners)),
        "banner_match": flag(paired),
        "banner_unpaired": count(unpaired),
        "banner_closing": banners[-1] if banners else "-",
        "no_recipe": count(len(without)),
        "no_recipe_sample": ", ".join(without[:5]),
        "ignore_prefix": count(len(ignore)),
        "echo_mask": count(len(masked)),
        "chained": count(len(chained)),
        "mask_sample": " | ".join((ignore + masked + chained)[:2]),
        "dev_view": count(len(dev_view)),
        "dev_view_sample": ", ".join(dev_view),
        "dry_rc": count(dry_code),
        "dry_members": count(len(dry_members)),
        "import_rc": count(import_code),
        "import_where": where[:90],
        "import_under_root": flag(bool(where) and Path(where).resolve().is_relative_to(REPO_ROOT)),
        "self_member": flag(GATE_MEMBER in members),
        "doc_named": count(len(named)),
        "doc_unnamed": ", ".join(name for name in members if name not in named),
        "doc_unnamed_count": count(len([name for name in members if name not in named])),
    }


def judge_ac17_01(facts: Facts) -> Verdict:
    """``AC-17|01``: the gate lists members, and every listed member can actually fail."""
    ok = (
        positive(facts["members"])
        and facts["banner_match"] == "yes"
        and facts["banner_unpaired"] == "0"
        and facts["banner_closing"] == "PASSED"
        and facts["no_recipe"] == "0"
        and facts["ignore_prefix"] == "0"
        and facts["echo_mask"] == "0"
        and facts["chained"] == "0"
        and facts["dev_view"] == "0"
        and facts["dry_rc"] == "0"
        and facts["dry_members"] == facts["members"]
        and facts["import_rc"] == "0"
        and facts["import_under_root"] == "yes"
        and facts["self_member"] == "yes"
    )
    readings = (
        f"gate members = {facts['members']}（{facts['member_list']}）；每项一个 sub-make，"
        f"前一项非零即中止；横幅 {facts['banners']} 段 = 成员数 + 收尾那一段，逐项配对 = "
        f"{facts['banner_match']}（错位的成员位 {facts['banner_unpaired']} 个，最后一段写的是 "
        f"{facts['banner_closing']}）",
        f"能被汇总掩盖的三种形状：`-` 前缀 {facts['ignore_prefix']} 行、`|| echo` "
        f"{facts['echo_mask']} 行、一行串两个成员 {facts['chained']} 行；没有配方的成员 "
        f"{facts['no_recipe']}"
        + (f"（{facts['no_recipe_sample']}）" if facts["no_recipe_sample"] else "")
        + (f"；样本 {facts['mask_sample']}" if facts["mask_sample"] else ""),
        f"make 自己的答案：`{' '.join(GATE_DRY_RUN)}` exit {facts['dry_rc']}，数出 "
        f"{facts['dry_members']} 个 sub-make —— 配方文本与 make 实际启动的成员必须相等，否则被量的"
        "是这份文件而不是那道门禁",
        f"开发者视图不得进门禁：成员里出现 `{'/'.join(DEV_VIEW_TARGETS)}` 的个数 = "
        f"{facts['dev_view']}"
        + (f"（{facts['dev_view_sample']}）" if facts["dev_view_sample"] else ""),
        f"可复现环境（C36 加性更正）：`{IMPORT_PROBE}` exit {facts['import_rc']}，来自 "
        f"{facts['import_where']}，在本树内 = {facts['import_under_root']}",
        f"判定成员自证：`{GATE_MEMBER}` 在成员名单里 = {facts['self_member']}；判据原文点名了 "
        f"{facts['doc_named']} 项，未被点名的 {facts['doc_unnamed_count']} 项 = "
        f"{facts['doc_unnamed']} —— 点名的少一个不是缺陷：条目文本是台账的内容哈希键，把成员数写进"
        "判据会让每一次加成员都改写判据",
    )
    reason = (
        ""
        if ok
        else "「逐项显式阻断」说的是每一项都能让门禁中止：成员为空、target 没有配方、"
        "`-` 前缀、`|| echo`、一行串两项、开发者视图被放进来、make 自己数出的成员数与配方不符、"
        "环境 import 不在这棵树里、或判定成员被移出 gate —— 任何一种都让「全绿」重新变成一句汇总"
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
# AC-9 条目 1/6/7：口径映射表的覆盖面，与 dwd 合并、修订传播的服务层面（C52）
# --------------------------------------------------------------------------- #

CALIBER_TABLE_REL: Final = "opendata/data/mapping.py"
CALIBER_DIR_REL: Final = "opendata/data/mappings"
CROSS_CHECK_REL2: Final = "opendata/pipeline/cross_check.py"
ADJUST_MODULE_REL: Final = "opendata/data/adjust.py"
RUNNER_REL: Final = "opendata/pipeline/runner.py"

#: 判据括号里点名的六类口径，以及每张表里承载它的那个键名。
CALIBER_KEYS: Final = (
    ("字段映射", "from"),
    ("单位换算", "scale"),
    ("复权口径", "adjust"),
    ("key 规范化", "normalize"),
    ("停牌语义", "suspension"),
    ("差异率分母", "denominator"),
)

#: dwd 合并四问在服务层的落点：只有跑这几条节点才知道那一格真被执行过。
DWD_SERVICE_NODES: Final = (
    "tests/test_dwd_merge.py::TestDwdMergeService::test_service_writes_all_four_point_in_time_faces",
    "tests/test_dwd_merge.py::TestDwdMergeService::test_service_degrades_per_key_and_keeps_the_source_evidence",
    "tests/test_dwd_merge.py::TestMergeSourceFrames::test_point_in_time_columns_are_stamped",
)

#: 修订传播的两条单测：一条证明 key 被重算进合并单元，一条证明那一行的值真的换了。
DWD_REVISION_NODES: Final = (
    "tests/test_dwd_merge.py::TestDwdMergeService::test_revision_of_an_existing_key_changes_the_dwd_row",
    "tests/test_dwd_merge.py::TestDwdMergeService::test_affected_keys_extend_the_merge_unit",
)


def caliber_yaml_texts(ctx: Context) -> dict[str, str]:
    """Every source's mapping file, keyed by stem, fail closed on an empty table."""
    directory = ctx.root / CALIBER_DIR_REL
    texts = {
        path.stem: path.read_text(encoding="utf-8") for path in sorted(directory.glob("*.yaml"))
    }
    if not texts:
        raise ProbeError(f"no mapping files under {CALIBER_DIR_REL}")
    return texts


def domains_in_yaml(text: str) -> tuple[str, ...]:
    """The domain names declared under ``domains:`` (two-space indent)."""
    return tuple(re.findall(r"^  ([a-z_]+):$", text, re.MULTILINE))


PARTITIONS_REL: Final = "opendata/pipeline/partitions.py"
PARTITION_TESTS_REL: Final = "tests/test_partition_maintenance.py"
PARTITION_FACE_REL: Final = "docs/evidence/C57/partition-horizon.txt"
PARTITION_APPLY_REL: Final = "docs/evidence/C57/partition-apply.txt"


def _facts_line(source: str) -> dict[str, str]:
    """Parse the ``FACTS k=v`` line a live face printed, if there is one."""
    line = next((row for row in source.splitlines() if row.startswith("FACTS ")), "")
    return dict(token.split("=", 1) for token in line.split()[1:] if "=" in token)


def measure_ac8_05(ctx: Context) -> Facts:
    """Wiring read in source, horizon read from the live archive."""
    from pathlib import Path

    jobs_src = ctx.read(PIPELINE_JOBS_REL)
    yaml_src = ctx.read(SCHEDULES_REL)
    ddl_src = ctx.read(WAREHOUSE_DDL_REL)
    face = ctx.read(PARTITION_FACE_REL)
    live = _facts_line(face)
    executable_block = re.search(r"EXECUTABLE_KINDS = frozenset\((.*?)\n\)", jobs_src, re.S)
    dispatch = re.search(r"async def _execute_template\(.*?\n\n\n", jobs_src, re.S)
    apply_body = re.search(r"def maintain_partition_horizon\(.*?\n\n\n", jobs_src, re.S)
    applied = Path(ctx.root) / PARTITION_APPLY_REL
    # ``m.group(...) if m else ""`` instead of ``bool(m) and m.group(...)``: mypy reads the
    # second form as dereferencing ``Match | None``. An absent match still reads False, because
    # a token is never ``in ""``.
    executable_text = executable_block.group(1) if executable_block else ""
    dispatch_text = dispatch.group(0) if dispatch else ""
    apply_text = apply_body.group(0) if apply_body else ""
    return {
        "row_declared": flag("kind: partition_maintenance" in yaml_src),
        "kind_executable": flag("PARTITION_MAINTENANCE" in executable_text),
        "body_dispatched": flag(
            "PARTITION_MAINTENANCE" in dispatch_text
            and "_execute_partition_maintenance" in dispatch_text
        ),
        "apply_half": flag(".ensure(" in apply_text and "plan_yearly_partitions" in apply_text),
        "ddl_layout": flag(
            "PARTITION BY RANGE COLUMNS" in ddl_src and "VALUES LESS THAN (MAXVALUE)" in ddl_src
        ),
        "live_partitioned": live.get("partitioned", "(absent)"),
        "live_tables": live.get("tables", "(absent)"),
        "live_gap_tables": live.get("gap_tables", "(absent)"),
        "live_keys": flag("分区列" in face and live.get("keys") is not None),
        "applied_face": flag(applied.is_file()),
    }


def judge_ac8_05(facts: Facts) -> Verdict:
    """``AC-8|05``: yearly partitions with a MAXVALUE fallback, and a maintenance task that runs."""
    ok = (
        facts["row_declared"] == "yes"
        and facts["kind_executable"] == "yes"
        and facts["body_dispatched"] == "yes"
        and facts["apply_half"] == "yes"
        and facts["ddl_layout"] == "yes"
        and positive(facts["live_partitioned"])
        and facts["live_gap_tables"] == "0"
        and facts["live_keys"] == "yes"
        and facts["applied_face"] == "yes"
    )
    readings = (
        f"``schedules.yaml`` 的 ``partition-maintenance`` 行 = {facts['row_declared']}；"
        f"kind 在 ``EXECUTABLE_KINDS`` 里 = {facts['kind_executable']}（不在则注册时被跳过，"
        f"cron 行只剩外观），``_execute_template`` 派发到本体 = {facts['body_dispatched']}",
        f"本体做 apply 半 = {facts['apply_half']}（调 ``ensure`` 而不是只出 plan —— "
        "C48 的量法在告警矩阵里，那一面按设计不动 DDL）",
        f"``ddl.py`` 渲染 ``RANGE COLUMNS`` 年分区 + ``MAXVALUE`` 兜底 = {facts['ddl_layout']}",
        f"真仓库读数（{PARTITION_FACE_REL}）：注册表 {facts['live_tables']} 张 / 已分区 "
        f"{facts['live_partitioned']} 张 / 年度上界仍缺 {facts['live_gap_tables']} 张；"
        f"档案里有分区列读数 = {facts['live_keys']}",
        f"经确认的 apply 留档（{PARTITION_APPLY_REL}）存在 = {facts['applied_face']}",
    )
    reason = (
        ""
        if ok
        else "分区面两头都要有：接线（yaml 行 + kind 可执行 + 派发到真的 ``ensure``）与仓库现状"
        "（分区列点名、上界不再落后、apply 后复跑留档）——本轮只有接线与落后读数，"
        "``REORGANIZE`` 是生产仓库的 DDL，等一次经确认的执行"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac8_06(ctx: Context) -> Facts:
    """The cross-year write case: does it exist, does it assert placement, has it run."""
    spec = ctx.read(PARTITION_TESTS_REL)
    face = ctx.read(PARTITION_FACE_REL)
    live = _facts_line(face)
    return {
        "case_file": flag("_probe_partition_maintenance" in spec),
        "inserts_new_year": flag(
            "'2027-03-01'" in spec and "INSERT INTO `_probe_partition_maintenance`" in spec
        ),
        "asserts_placement": count(
            sum(
                1
                for line in spec.splitlines()
                if line.lstrip().startswith("assert ") and "placements ==" in line
            )
        ),
        "marked": "e2e" if "@pytest.mark.e2e" in spec else "no",
        "fallback_gap": live.get("gap_tables", "(absent)"),
        "ran_live": flag("PARTITION_E2E_EXIT=0" in face),
    }


def judge_ac8_06(facts: Facts) -> Verdict:
    """``AC-8|06``: a new-year row lands in its own partition instead of erroring."""
    ok = (
        facts["case_file"] == "yes"
        and facts["inserts_new_year"] == "yes"
        and positive(facts["asserts_placement"])
        and facts["marked"] == "e2e"
        and facts["fallback_gap"] == "0"
        and facts["ran_live"] == "yes"
    )
    readings = (
        f"用例在 {PARTITION_TESTS_REL}：自建 ``_probe_*`` 表 = {facts['case_file']}，"
        f"插入新年度那一行（2027-03-01 进 probe 表）= {facts['inserts_new_year']}，"
        f"落点分区名被断言 = {facts['asserts_placement']} 行",
        f"标记 = {facts['marked']}（``make gate`` 不跑 e2e，所以这一面必须另留档才算跑过）",
        f"真仓库当下还缺年度分区的表 = {facts['fallback_gap']} 张 —— 缺的那一年会落进 "
        "``pmax``：写入不报错，但年分区形同不存在，所以「写成功」必须有落点断言",
        f"本轮留档里有一次真跑 = {facts['ran_live']}",
    )
    reason = (
        ""
        if ok
        else "跨年写入这条判据要有「跑过」的证据而不是「有用例」：用例是 e2e 面（门禁不跑），"
        "而真仓库的年度上界还落后一年 —— 补这一年是生产仓库的 DDL，等一次经确认的执行"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


# --------------------------------------------------------------------------- #
# AC-8 条目 1/2/3/7：ods 形状、DDL 归属、key 级幂等与无 id 分页（C58）
# --------------------------------------------------------------------------- #

#: |01 的命名与形状：唯一的派生函数、渲染 DDL 的生成器、以及真仓库的当场读数。
DOMAINS_REL: Final = "opendata/data/domains.py"
ODS_DDL_TESTS_REL: Final = "tests/test_warehouse_ddl.py"
ODS_DOMAIN_TESTS_REL: Final = "tests/test_domains.py"
ODS_FACE_REL: Final = "docs/evidence/C58/ods-face.txt"

#: |02 的两套 alembic 环境，与「启动不建表」的三个单元面。
DATABASE_REL: Final = "opendata/core/database.py"
MAIN_REL: Final = "opendata/main.py"
ALEMBIC_INI_REL: Final = "alembic.ini"
ALEMBIC_DATA_INI_REL: Final = "alembic_data.ini"
ALEMBIC_DATA_ENV_REL: Final = "alembic_data/env.py"
DB_OWNERSHIP_TESTS_REL: Final = "tests/test_database_functions.py"
MIGRATION_TESTS_REL: Final = "tests/test_warehouse_migrations.py"

#: |03 的写入层与 |07 的分页面。
ODS_WRITER_REL: Final = "opendata/pipeline/ods_writer.py"
ODS_WRITER_TESTS_REL: Final = "tests/test_ods_writer.py"
TABLE_PAGE_REL: Final = "opendata/pipeline/table_page.py"
TABLES_API_REL: Final = "opendata/api/tables.py"
TABLE_PAGE_TESTS_REL: Final = "tests/test_table_page.py"
TABLES_API_TESTS_REL: Final = "tests/test_api_tables_lifecycle.py"

#: |01：命名派生、DDL 形状、注册表 census 真到得了 ods 层——三条都要跑绿才算形状面成立。
AC8_01_NODES: Final = (
    f"{ODS_DOMAIN_TESTS_REL}::TestDerivations::test_ods_table",
    f"{ODS_DDL_TESTS_REL}::TestOdsDdl::test_contains_source_columns_and_metadata_trio",
    "tests/test_pipeline_jobs.py::TestPartitionMaintenanceJob::"
    "test_the_registered_census_reaches_the_ods_layer",
)

#: |02：四个归属单元面 + 仓库迁移链的两个只读面（图可解、离线渲染与生成器一致）。
AC8_02_NODES: Final = (
    f"{DB_OWNERSHIP_TESTS_REL}::TestWarehouseDdlOwnership::"
    "test_control_metadata_names_no_warehouse_table",
    f"{DB_OWNERSHIP_TESTS_REL}::TestWarehouseDdlOwnership::"
    "test_create_tables_never_touches_the_warehouse_engine",
    f"{DB_OWNERSHIP_TESTS_REL}::TestWarehouseDdlOwnership::"
    "test_startup_creates_tables_only_off_the_production_branch",
    f"{DB_OWNERSHIP_TESTS_REL}::TestWarehouseDdlOwnership::"
    "test_the_warehouse_has_its_own_alembic_environment",
    f"{MIGRATION_TESTS_REL}::TestMigrationGraph::test_single_head_with_resolvable_chain",
    f"{MIGRATION_TESTS_REL}::TestOfflineReplay::test_offline_render_matches_the_generator",
)

#: |03：判据原文要的就是「单测」这一面——语句形状 + 落库序列，全部在门禁选择式内。
AC8_03_NODES: Final = (
    f"{ODS_WRITER_TESTS_REL}::TestSqlBuilders::test_upsert_uses_business_key_and_alias_form",
    f"{ODS_WRITER_TESTS_REL}::TestSqlBuilders::"
    "test_upsert_with_only_key_columns_keeps_a_valid_no_op_update",
    f"{ODS_WRITER_TESTS_REL}::TestStagingWrite::"
    "test_metadata_columns_are_written_and_the_key_is_never_updated",
    f"{ODS_WRITER_TESTS_REL}::TestDirectWrite::"
    "test_direct_issues_one_upsert_per_chunk_and_no_staging",
    "tests/test_dwd_merge.py::TestRecomputeIdempotence::"
    "test_landing_the_same_key_twice_is_one_row_written_in_place",
)

#: 「价格修正不产生重复行」在真库那一格是 e2e：门禁与探针的选择式都不跑，读数只做披露。
AC8_03_LIVE_NODE: Final = (
    f"{ODS_WRITER_TESTS_REL}::TestLiveUpsert::"
    "test_corrected_value_updates_in_place_without_duplicating"
)

#: |07：无 id 表的排序回落、SQL 形状、SQLite 真查询，以及端点自己那一格。
AC8_07_NODES: Final = (
    f"{TABLE_PAGE_TESTS_REL}::TestOrderColumns::test_falls_back_to_the_business_key",
    f"{TABLE_PAGE_TESTS_REL}::TestPageSql::test_orders_by_business_key_and_binds_pagination",
    f"{TABLE_PAGE_TESTS_REL}::TestTableShapeIntegration::test_shape_of_a_table_without_id",
    f"{TABLE_PAGE_TESTS_REL}::TestTableShapeIntegration::"
    "test_paging_a_table_without_id_returns_every_row_once",
    f"{TABLES_API_TESTS_REL}::TestGetTableData::test_get_data_from_a_table_without_id_column",
)


def _ac8_node_facts(prefix: str, nodes: Sequence[str]) -> Facts:
    """Run one cell's node set and tally it the way every other node-judged cell does."""
    decided = outcomes(nodes)
    return {
        f"{prefix}_runs": count(len(decided)),
        f"{prefix}_passed": count(sum(1 for seen in decided.values() if seen == "passed")),
        f"{prefix}_bad": bad_of(decided),
    }


def measure_ac8_01(ctx: Context) -> Facts:
    """Naming, trio, business key: read where each is decided, then in the warehouse."""
    nodes = _ac8_node_facts("n", AC8_01_NODES)
    domains = ctx.read(DOMAINS_REL)
    ddl = ctx.read(WAREHOUSE_DDL_REL)
    builder = function_body(ddl, "_table_ddl")
    ods_builder = function_body(ddl, "ods_table_ddl")
    live = _facts_line(ctx.read(ODS_FACE_REL))
    return {
        **nodes,
        "naming_derived": flag(
            'return f"ods_{domain}_{source}"' in function_body(domains, "ods_table")
        ),
        "trio_appended": flag("[*columns, *ODS_METADATA_COLUMNS]" in ods_builder),
        "trio_declared": count(
            sum(1 for name in ("_source", "_fetched_at", "_batch_id") if f'Column("{name}"' in ddl)
        ),
        "key_fails_closed": flag("needs a business primary key" in builder),
        "pk_from_key": flag("PRIMARY KEY ({key_list})" in builder),
        "no_auto_increment": flag("AUTO_INCREMENT" not in ddl),
        "live_ods": live.get("live_ods", "(absent)"),
        "ods_named": live.get("ods_named", "(absent)"),
        "ods_trio": live.get("ods_trio", "(absent)"),
        "ods_key_pk": live.get("ods_key_pk", "(absent)"),
        "ods_autokey": live.get("ods_autokey", "(absent)"),
        "census_ods": live.get("ods_registered", "(absent)"),
    }


def judge_ac8_01(facts: Facts) -> Verdict:
    """``AC-8|01``: named by derivation, carrying raw columns + the trio, keyed by business key."""
    built = positive(facts["live_ods"])
    ok = (
        facts["n_passed"] == facts["n_runs"]
        and number(facts["n_runs"]) == len(AC8_01_NODES)
        and facts["n_bad"] == "-"
        and facts["naming_derived"] == "yes"
        and facts["trio_appended"] == "yes"
        and facts["trio_declared"] == "3"
        and facts["key_fails_closed"] == "yes"
        and facts["pk_from_key"] == "yes"
        and facts["no_auto_increment"] == "yes"
        and built
        and facts["ods_named"] == facts["live_ods"]
        and facts["ods_trio"] == facts["live_ods"]
        and facts["ods_key_pk"] == facts["live_ods"]
        and facts["ods_autokey"] == "0"
    )
    readings = (
        f"单元面 {facts['n_passed']}/{facts['n_runs']} passed"
        + (f"; not green: {facts['n_bad']}" if facts["n_bad"] != "-" else ""),
        f"命名只有一处派生（``domains.ods_table`` 返回 ``ods_<domain>_<source>``）= "
        f"{facts['naming_derived']}；三元组无条件追加 = {facts['trio_appended']}，"
        f"声明齐 {facts['trio_declared']}/3",
        f"主键由业务 key 渲染 = {facts['pk_from_key']}，key 缺失 fail closed = "
        f"{facts['key_fails_closed']}，生成器不再出自增 id = {facts['no_auto_increment']}",
        f"真仓库（{ODS_FACE_REL}）：ods 表 {facts['live_ods']} 张 —— 命名合规 "
        f"{facts['ods_named']}、三元组齐全 {facts['ods_trio']}、业务 key 主键 "
        f"{facts['ods_key_pk']}、带自增 id {facts['ods_autokey']}；注册表里应有 "
        f"{facts['census_ods']} 条 ods 腿（差额是 |08 的落库覆盖面，不是本格的形状判据）",
    )
    reason = (
        ""
        if ok
        else "ods 形状三条（命名 / 源原始列+三元组 / 业务 key 主键）要在派生点、渲染点和真仓库三处"
        "同时成立：任何一处松开，落库就会长出自己的列名与主键"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac8_02(ctx: Context) -> Facts:
    """Who owns the warehouse DDL, and what startup is allowed to do."""
    nodes = _ac8_node_facts("n", AC8_02_NODES)
    ini = ctx.read(ALEMBIC_INI_REL)
    data_ini = ctx.read(ALEMBIC_DATA_INI_REL)
    env = ctx.read(ALEMBIC_DATA_ENV_REL)
    control_env = ctx.read("alembic/env.py")
    app_src = ctx.read(MAIN_REL)
    lifespan = function_body(app_src, "lifespan")
    create = function_body(ctx.read(DATABASE_REL), "create_tables")
    face = ctx.read(ODS_FACE_REL)
    live = _facts_line(face)
    return {
        **nodes,
        "locations_split": flag(
            "script_location = alembic_data" in data_ini and "script_location = alembic\n" in ini
        ),
        "no_autogen": flag("target_metadata = None" in env),
        "warehouse_url": flag("settings.data_database_url" in env),
        "version_table_split": flag(
            'VERSION_TABLE = "alembic_version_data"' in env and "VERSION_TABLE" not in control_env
        ),
        "control_cannot_reach_data_url": flag("data_database_url" not in control_env),
        "startup_guarded": flag(
            lifespan.count("await create_tables()") == 1
            and "settings.is_production" in lifespan
            and "Production mode: skipping create_tables" in lifespan
        ),
        "create_tables_control_only": flag("create_all" in create and "data_engine" not in create),
        "ctrl_warehouse_tables": live.get("ctrl_warehouse_tables", "(absent)"),
        "version_tables_in_face": flag(
            "alembic_version_data（schema=opendata_data）" in face
            and "alembic_version（schema=opendata）" in face
        ),
    }


def judge_ac8_02(facts: Facts) -> Verdict:
    """``AC-8|02``: the warehouse schema is the data env's, and startup builds nothing."""
    ok = (
        facts["n_passed"] == facts["n_runs"]
        and number(facts["n_runs"]) == len(AC8_02_NODES)
        and facts["n_bad"] == "-"
        and facts["locations_split"] == "yes"
        and facts["no_autogen"] == "yes"
        and facts["warehouse_url"] == "yes"
        and facts["version_table_split"] == "yes"
        and facts["control_cannot_reach_data_url"] == "yes"
        and facts["startup_guarded"] == "yes"
        and facts["create_tables_control_only"] == "yes"
        and facts["ctrl_warehouse_tables"] == "0"
        and facts["version_tables_in_face"] == "yes"
    )
    readings = (
        f"单元面 {facts['n_passed']}/{facts['n_runs']} passed"
        + (f"; not green: {facts['n_bad']}" if facts["n_bad"] != "-" else "")
        + " —— 含「启动建表碰不到仓库 engine」（engine 换成会炸的桩）与「Base.metadata 里"
        "一张仓库表都没有」两格，迁移链另有图/离线渲染两格",
        f"两套环境分开：script_location 分叉 = {facts['locations_split']}，仓库 env 不挂 "
        f"target_metadata（无从 generate 出 ORM 表）= {facts['no_autogen']}，URL 取 "
        f"``data_database_url`` = {facts['warehouse_url']}，版本表各自一张 = "
        f"{facts['version_table_split']}，控制 env 里出现仓库 URL = "
        f"{'no' if facts['control_cannot_reach_data_url'] == 'yes' else 'yes'}",
        f"启动面：``lifespan`` 里 ``create_tables()`` 只有一处且在 ``is_production`` 分支之外 = "
        f"{facts['startup_guarded']}；``create_tables`` 只做 ``Base.metadata.create_all``、"
        f"不引 ``data_engine`` = {facts['create_tables_control_only']}",
        f"真仓库侧（{ODS_FACE_REL}）：控制库里的仓库表 {facts['ctrl_warehouse_tables']} 张，"
        f"两套版本表各自在当前版本 = {facts['version_tables_in_face']}",
    )
    reason = (
        ""
        if ok
        else "「独立 alembic 环境 + 启动不建表」要的是通路而不是意图：配置分叉、版本表分叉、"
        "仓库 URL 只出现在仓库 env、启动建表在生产分支之外且摸不到仓库 engine —— "
        "任何一条断开，仓库表就会在无人审批的进程里被建出来"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac8_03(ctx: Context) -> Facts:
    """Key-level idempotence: the unit plane the item asks for, plus the live node it does not."""
    nodes = _ac8_node_facts("n", AC8_03_NODES)
    writer = ctx.read(ODS_WRITER_REL)
    direct = function_body(writer, "build_upsert_sql")
    staging = function_body(writer, "build_staging_upsert_sql")
    return {
        **nodes,
        "alias_form": flag("AS new " in direct and "ON DUPLICATE KEY UPDATE" in direct),
        "key_left_alone": flag(
            "if column not in set(key)]" in direct and "if column not in set(key)]" in staging
        ),
        "fails_closed": count(
            direct.count('raise ValueError("cannot build a')
            + staging.count('raise ValueError("cannot build a')
        ),
        "staging_key_clause": flag("ON DUPLICATE KEY UPDATE" in staging),
        "live_reading": node_outcome(AC8_03_LIVE_NODE),
    }


def judge_ac8_03(facts: Facts) -> Verdict:
    """``AC-8|03``: re-running a write updates the keyed row instead of appending a twin."""
    ok = (
        facts["n_passed"] == facts["n_runs"]
        and number(facts["n_runs"]) == len(AC8_03_NODES)
        and facts["n_bad"] == "-"
        and facts["alias_form"] == "yes"
        and facts["key_left_alone"] == "yes"
        and number(facts["fails_closed"]) >= 2
        and facts["staging_key_clause"] == "yes"
    )
    readings = (
        f"单测面（判据原文点名的这一面）{facts['n_passed']}/{facts['n_runs']} passed"
        + (f"; not green: {facts['n_bad']}" if facts["n_bad"] != "-" else "")
        + " —— 两条 builder 形状、staging/direct 两条落库路径的语句序列，以及"
        "「同一 key 落两次还是一行、值就地改」那一格",
        f"upsert 走 ``AS new`` 别名式 = {facts['alias_form']}；两条路径都把业务 key 挡在 "
        f"UPDATE 赋值之外（改 key 就不是修同一行）= {facts['key_left_alone']}；空列表 fail "
        f"closed 计数 = {facts['fails_closed']}（>=2 才覆盖两条 builder）",
        f"真库那一格（e2e，{AC8_03_LIVE_NODE.split('::')[-1]}）本轮读数 = "
        f"{facts['live_reading']}：``-m 'not e2e'`` 的选择式不跑它，本格判定落在上面那一面，"
        "「修正后不产生重复行」要在真库上跑一次才算跑过 —— 那是需要确认的仓库写",
    )
    reason = (
        ""
        if ok
        else "key 级幂等只有一个意思：同一批 key 再写一遍，表里还是那些行、值被就地更新。"
        "这要求语句是 upsert 而不是 insert、且 UPDATE 段绝不碰 key 列 —— 少一条，重跑就变成追加"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac8_07(ctx: Context) -> Facts:
    """Paging without ``id``: where the ORDER BY comes from, and who calls it."""
    nodes = _ac8_node_facts("n", AC8_07_NODES)
    page = ctx.read(TABLE_PAGE_REL)
    order = function_body(page, "order_columns")
    sql = function_body(page, "page_sql")
    api = ctx.read(TABLES_API_REL)
    endpoint = function_body(api, "get_table_data")
    return {
        **nodes,
        "falls_back_to_key": flag(
            '"id" in columns' in order and "return list(key_columns)" in order
        ),
        "first_column_still_deterministic": flag("return list(columns[:1])" in order),
        "offset_bound": flag("LIMIT :limit OFFSET :offset" in sql),
        "keyless_fails_closed": flag("has no columns to order by" in sql),
        "endpoint_reads_the_table_shape": flag(
            "table_shape(" in endpoint and "page_sql(" in endpoint
        ),
        "no_hardcoded_order": flag("ORDER BY id" not in api),
    }


def judge_ac8_07(facts: Facts) -> Verdict:
    """``AC-8|07``: ``/tables`` pages an ods table that has no surrogate id."""
    ok = (
        facts["n_passed"] == facts["n_runs"]
        and number(facts["n_runs"]) == len(AC8_07_NODES)
        and facts["n_bad"] == "-"
        and facts["falls_back_to_key"] == "yes"
        and facts["first_column_still_deterministic"] == "yes"
        and facts["offset_bound"] == "yes"
        and facts["keyless_fails_closed"] == "yes"
        and facts["endpoint_reads_the_table_shape"] == "yes"
        and facts["no_hardcoded_order"] == "yes"
    )
    readings = (
        f"单元/集成面 {facts['n_passed']}/{facts['n_runs']} passed"
        + (f"; not green: {facts['n_bad']}" if facts["n_bad"] != "-" else "")
        + " —— 含 SQLite 真表翻页（三行两页，每行只出现一次）与端点那一格 HTTP 请求",
        f"排序列回落链 ``id`` → 业务主键 → 首列 = {facts['falls_back_to_key']} / "
        f"{facts['first_column_still_deterministic']}；limit/offset 走绑定参数 = "
        f"{facts['offset_bound']}；无列可排 fail closed = {facts['keyless_fails_closed']}",
        f"端点 ``/tables/{'{id}'}/data`` 先读表形状再拼分页 SQL = "
        f"{facts['endpoint_reads_the_table_shape']}；模块里残留硬编码 ``ORDER BY id`` = "
        f"{'no' if facts['no_hardcoded_order'] == 'yes' else 'yes'}",
    )
    reason = (
        ""
        if ok
        else "ods 表按业务 key 建，没有自增 id：分页 SQL 的 ORDER BY 必须由表形状推出来，"
        "否则整个 /tables 数据页对 ods 层都是 500 或乱序翻页"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac9_01(ctx: Context) -> Facts:
    """Read which P0 domains the 口径 table declares and which calibers it can carry."""
    p0 = p0_domains_in_migration()
    texts = caliber_yaml_texts(ctx)
    declared = {name for text in texts.values() for name in domains_in_yaml(text)}
    missing = sorted(set(p0) - declared)
    table = ctx.read(CALIBER_TABLE_REL)
    admitted = set(re.findall(r'"([a-z_]+)"', function_body(table, "_parse_domain"))) | set(
        re.findall(r"_FIELD_KEYS = frozenset\(\{([^}]*)\}\)", table)[0].split('"')[1::2]
    )
    payload_keys = {
        key for text in texts.values() for key in re.findall(r"(\w+):", re.sub(r"#.*", "", text))
    }
    carried = {
        token: "yes" if (token in admitted or token in payload_keys) else "no"
        for _, token in CALIBER_KEYS
    }
    cross = ctx.read(CROSS_CHECK_REL2)
    return {
        "p0_total": count(len(p0)),
        "p0_mapped": count(len(set(p0) & declared)),
        "p0_missing": ", ".join(missing) or "-",
        "sources": ", ".join(sorted(texts)),
        "table_loads": flag("def load_mapping" in table and "def normalize_frame" in table),
        "exportable": flag("def mapping_as_json" in table),
        **{f"cat_{token}": value for token, value in carried.items()},
        "adjust_in_code": flag("def apply_adjust" in ctx.read(ADJUST_MODULE_REL)),
        "denominator_in_code": flag("key union (rate denominator)" in cross),
    }


def judge_ac9_01(facts: Facts) -> Verdict:
    """``AC-9|01``: the caliber table exists, covers every P0 domain, carries all six calibers."""
    categories = tuple(f"cat_{token}" for _, token in CALIBER_KEYS)
    covered = number(facts["p0_mapped"]) == number(facts["p0_total"])
    ok = (
        covered and facts["table_loads"] == "yes" and all(facts[key] == "yes" for key in categories)
    )
    uncarried = ", ".join(name for (name, token) in CALIBER_KEYS if facts[f"cat_{token}"] == "no")
    readings = (
        f"表在不在: {CALIBER_TABLE_REL} 有 load_mapping/normalize_frame = {facts['table_loads']}，"
        f"可导出 mapping_as_json = {facts['exportable']}，源文件 = {facts['sources']}",
        f"P0 覆盖: {facts['p0_mapped']}/{facts['p0_total']} 个 P0 域在这张表里"
        + (f"；没覆盖的是 {facts['p0_missing']}" if facts["p0_missing"] != "-" else ""),
        "六类口径落表: "
        + "、".join(f"{name}={facts[f'cat_{token}']}" for name, token in CALIBER_KEYS),
        f"另两面（只在代码里、不在这张表里时不计入覆盖）: 复权在 {ADJUST_MODULE_REL} = "
        f"{facts['adjust_in_code']}，差异率分母在 {CROSS_CHECK_REL2} = "
        f"{facts['denominator_in_code']}",
    )
    reason = (
        ""
        if ok
        else "判据两半都要成立：这张表得覆盖全部 P0 域，且括号里点名的六类口径都能由它承载。"
        + (
            f"未覆盖的 P0 域：{facts['p0_missing']}（分母取 A4.1 迁移的 _DWD_TABLES，"
            f"共 {facts['p0_total']} 个）"
            if not covered
            else ""
        )
        + (f"；不能承载的口径：{uncarried}" if uncarried else "")
        + " —— 复权/分母即使代码里有实现，不写进这张表就仍然是『口径散在代码里』"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac9_06(ctx: Context) -> Facts:
    """Run the service-level merge nodes, then read the four stamping faces in the merge layer."""
    nodes = outcomes(DWD_SERVICE_NODES)
    merge = ctx.read(DWD_MERGE_REL)
    body = function_body(merge, "merge_source_frames")
    run = method_body(merge, "DwdMergeService", "run")
    return {
        "runs": count(len(nodes)),
        "passed": count(sum(1 for seen in nodes.values() if seen == "passed")),
        "bad": bad_of(nodes),
        "authority_fallback": flag("if rank > 0:" in body and "degraded += 1" in body),
        "source_traced": flag("record[SOURCE_COLUMN] = source" in body),
        "diff_flagged": flag('record["_diff_flag"] = 1 if biz_key in flagged else 0' in body),
        "as_of_stamped": flag('record["_as_of"] = as_of' in body),
        "as_of_is_window_end": flag("as_of=end," in run),
        "trace_columns": flag(
            'TRACE_COLUMNS = ("source", "_merged_at", "_diff_flag", "_as_of")' in merge
        ),
    }


def judge_ac9_06(facts: Facts) -> Verdict:
    """``AC-9|06``: authority wins, gaps degrade and fill, and all four trace columns land."""
    faces = (
        "authority_fallback",
        "source_traced",
        "diff_flagged",
        "as_of_stamped",
        "as_of_is_window_end",
        "trace_columns",
    )
    ok = (
        number(facts["runs"]) == len(DWD_SERVICE_NODES)
        and facts["passed"] == facts["runs"]
        and facts["bad"] == "-"
        and all(facts[face] == "yes" for face in faces)
    )
    readings = (
        f"服务层与合并层节点: {facts['passed']}/{facts['runs']} passed"
        + (f"; not green: {facts['bad']}" if facts["bad"] != "-" else ""),
        "四问各自的代码面: 权威缺失才降级 = "
        f"{facts['authority_fallback']}、source 留痕 = {facts['source_traced']}、"
        f"_diff_flag 打标 = {facts['diff_flagged']}、_as_of 写入 = {facts['as_of_stamped']}",
        "_as_of 取的是窗口末而不是别的日期: service.run 传 as_of=end = "
        f"{facts['as_of_is_window_end']}；"
        f"四列同时是 dwd 的留痕列定义 = {facts['trace_columns']}",
    )
    reason = (
        ""
        if ok
        else "这一格要的是四件事都发生在服务层的那一次 run 里：有权威取权威、没权威降级填补、"
        "source 留下是谁供的、_diff_flag 只在不一致时为 1、_as_of 是这一窗的窗口末 —— "
        "少一面就少一条判据"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac9_07(ctx: Context) -> Facts:
    """Run the two revision nodes, then read how a corrected key travels down the chain."""
    nodes = outcomes(DWD_REVISION_NODES)
    merge = ctx.read(DWD_MERGE_REL)
    run = method_body(merge, "DwdMergeService", "run")
    hook = method_body(merge, "DwdMergeService", "run_hook")
    runner = ctx.read(RUNNER_REL)
    writer = method_body(merge, "DwdWriter", "write")
    return {
        "runs": count(len(nodes)),
        "passed": count(sum(1 for seen in nodes.values() if seen == "passed")),
        "bad": bad_of(nodes),
        "reader_gets_keys": flag("self._reader(source)(start, end, set(affected_keys))" in run),
        "keys_extend_diffs": flag("extra_diff_keys=frozenset(affected_keys)" in run),
        "hook_resells_keys": flag("affected_keys=self._contract_keys(context)" in hook),
        "runner_publishes_keys": flag("affected_keys" in runner and "_affected_keys" in runner),
        "writer_upserts": flag("build_upsert_sql" in writer),
    }


def judge_ac9_07(facts: Facts) -> Verdict:
    """``AC-9|07``: a key corrected in ods re-writes its dwd row, and the unit test says so."""
    faces = (
        "reader_gets_keys",
        "keys_extend_diffs",
        "hook_resells_keys",
        "runner_publishes_keys",
        "writer_upserts",
    )
    ok = (
        number(facts["runs"]) == len(DWD_REVISION_NODES)
        and facts["passed"] == facts["runs"]
        and facts["bad"] == "-"
        and all(facts[face] == "yes" for face in faces)
    )
    readings = (
        f"两条单测: {facts['passed']}/{facts['runs']} passed"
        + (f"; not green: {facts['bad']}" if facts["bad"] != "-" else "")
        + " —— 一条测「修正后的 key 被重算进合并单元」，一条测「那一行的值真的换了」",
        "传播链五段: runner 交出被改的键 = "
        f"{facts['runner_publishes_keys']}、run_hook 把 ods 拼法重拼成契约键 = "
        f"{facts['hook_resells_keys']}、reader 收到这批键 = {facts['reader_gets_keys']}、"
        f"键进入差异重算 = {facts['keys_extend_diffs']}、"
        f"写侧按业务键 upsert = {facts['writer_upserts']}",
    )
    reason = (
        ""
        if ok
        else "「修订传播」是一条链：ods 改了的键要交出来、要换成契约键、要进 reader 的取数窗口、"
        "要参与差异重算、最后按业务键 upsert 回同一行 —— 链上任一段断掉，dwd 留的就不是修订后的值"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


# --------------------------------------------------------------------------- #
# AC-16 条目 6/7：零依赖基线的清零面与干净环境的 P0 集成面（C51）
# --------------------------------------------------------------------------- #

ZERO_DEP_SCANNER: Final = "scripts/codemod/verify_no_akshare.py"
P0_INTEGRATION_SELECTOR: Final = "integration and not e2e"
P0_MIGRATION_REL: Final = "alembic_data/versions/20260923-0001_ods_dwd_p0.py"

#: Where a clean-environment run is archived, one per round that rebuilt it.
CLEAN_RUN_BASENAME: Final = "clean-env-integration-run.txt"
CLEAN_SECTION: Final = "===== B. clean venv ====="

#: ``AC-11|04``: an archived live EXPLAIN, one per round that re-ran the plan check.
WINDOW_EXPLAIN_BASENAME: Final = "window-pruning-explain.txt"

#: The line the EXPLAIN instrument writes so an archive says which SQL it planned.
QUERY_DIGEST_KEY: Final = "query_module_sha"

#: The two packages the criterion requires to be absent.
UPSTREAM_PACKAGES: Final = ("akshare", "openbb")


def marker_names_in(node: ast.AST) -> set[str]:
    """Every ``pytest.mark.<name>`` spelled inside a node, however it is nested."""
    found: set[str] = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Attribute)
            and isinstance(child.value.value, ast.Name)
            and child.value.value.id == "pytest"
            and child.value.attr == "mark"
        ):
            found.add(child.attr)
    return found


def registered_markers_in_ini(ini_text: str) -> set[str]:
    """The names under ``pytest.ini``'s ``markers =`` block, which ``--strict-markers`` enforces."""
    if "markers =" not in ini_text:
        return set()
    names: set[str] = set()
    for line in ini_text.split("markers =", 1)[1].splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            break
        names.add(line.strip().split(":")[0].strip())
    return names


def integration_units_now() -> tuple[str, ...]:
    """Every test unit the selector reaches right now, as ``tests/x.py`` or ``tests/x.py::Class``.

    Read off the filesystem rather than ``git ls-files``: this is the set ``pytest`` collects, and
    an uncommitted marked module is collected too.
    """
    units: list[str] = []
    for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        if "integration" not in path.read_text(encoding="utf-8"):
            continue
        rel = str(path.relative_to(REPO_ROOT))
        for node in parse(rel).body:
            marks = marker_names_in(node)
            if (
                isinstance(node, ast.Assign)
                and "integration" in marks
                and any(getattr(t, "id", "") == "pytestmark" for t in node.targets)
            ):
                units.append(rel)
            if isinstance(node, ast.ClassDef) and "integration" in marks:
                units.append(f"{rel}::{node.name}")
    return tuple(units)


def p0_domains_in_migration() -> tuple[str, ...]:
    """The P0 domains as the A4.1 migration names them."""
    for node in parse(P0_MIGRATION_REL).body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Dict)
            and any(getattr(t, "id", "") == "_DWD_TABLES" for t in node.targets)
        ):
            return tuple(
                ast.unparse(key).strip("'\"") for key in node.value.keys if key is not None
            )
    raise ProbeError(f"_DWD_TABLES is not a mapping in {P0_MIGRATION_REL}")


def measure_ac16_06(ctx: Context) -> Facts:
    """Run the frozen zero-dependency assertion, then split what it still finds by shape."""
    code, out = run_argv([sys.executable, ZERO_DEP_SCANNER])
    self_code, _ = run_argv([sys.executable, ZERO_DEP_SCANNER, "--self-test"])
    scanner = script_module(ZERO_DEP_SCANNER)
    findings = scanner.collect(scanner.DEFAULT_TARGETS)
    frozen = scanner.load_baseline()
    kinds = {
        kind: sum(1 for finding in findings if finding.kind == kind)
        for kind in ("import", "dynamic", "string")
    }
    frozen_keys = {entry[0:3] for entry in frozen.entries}
    update_body = function_body(ctx.read(ZERO_DEP_SCANNER), "update")
    refuses_growth = "refusing to grow the baseline" in update_body
    return {
        "detector_exit": str(code),
        "detector_ok_line": first_capture(out, r"^(OK: .*)$"),
        "self_test_exit": str(self_code),
        "frozen": count(len(frozen.entries)),
        "live": count(len(findings)),
        "import_live": count(kinds["import"]),
        "dynamic_live": count(kinds["dynamic"]),
        "string_live": count(kinds["string"]),
        "string_files": ", ".join(sorted({f.file for f in findings if f.kind == "string"})),
        "no_new_reference": flag(all((f.file, f.module, f.kind) in frozen_keys for f in findings)),
        "only_down": flag(refuses_growth and "force" in update_body),
        "scope": ", ".join(scanner.DEFAULT_TARGETS),
    }


def judge_ac16_06(facts: Facts) -> Verdict:
    """``AC-16|06``: the frozen baseline may only shrink, and it has to reach zero."""
    ok = (
        facts["detector_exit"] == "0"
        and facts["self_test_exit"] == "0"
        and facts["only_down"] == "yes"
        and facts["no_new_reference"] == "yes"
        and facts["import_live"] == "0"
        and facts["dynamic_live"] == "0"
        and facts["frozen"] == "0"
    )
    readings = (
        f"零依赖断言（门禁那两条命令原样）：check exit={facts['detector_exit']}，"
        f"--self-test exit={facts['self_test_exit']}；扫描面 = {facts['scope']}",
        f"判据原文要清的是「集成层的 `import akshare` 残留」：AST 走查 import 形态 = "
        f"{facts['import_live']}，动态 import 形态 = {facts['dynamic_live']} —— 这一项已经是零",
        f"基线还冻着 {facts['frozen']} 条（现场走查 {facts['live']} 条，"
        f"新增即失败 = {facts['no_new_reference']}，只降不升的门禁在位 = {facts['only_down']}）："
        f"{facts['string_live']} 条是名字面量，落在 {facts['string_files']}",
        f"扫描器自己怎么说：{facts['detector_ok_line'] or '-'}",
    )
    reason = (
        ""
        if ok
        else "「基线清零」是这条唯一没到的面，而要把它抹平只有两条路，两条都要改判据本身：①把扫描器"
        "的字符串规则改窄（等于回头放宽已勾的 AC-16|05「AST 口径」，本轮拒绝）；②改掉这三处产品事实"
        "的拼写 —— `openbb_map.py` 的 `openbb` 是 FR-7 对照表自己的字段名，`data_script.py` 的 "
        "`akshare` 是 `DataScript.source` 的溯源默认值（搬运脚本与 P0 迁移同一写法在写它），"
        "`patrol.py` 的是 `key_status()` 的数据源标签；把它们改成 `AKShare` 之类只是给扫描器做"
        "伪装，正是 C35 刚收口的那种门禁空转。属于用户/产品决策，不是再读一遍能解决的"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def round_archives(basename: str) -> tuple[str, ...]:
    """Every ``docs/evidence/C<n>/<basename>``, oldest round first.

    Resolved off the filesystem because these archives are ratchets on *this moment*: naming one
    fixed round would let anything added after it stay unmeasured forever.
    """
    found: list[tuple[int, str]] = []
    for path in (REPO_ROOT / "docs" / "evidence").glob(f"C*/{basename}"):
        round_stem = path.parent.name
        if round_stem[1:].isdigit():
            found.append((int(round_stem[1:]), str(path.relative_to(REPO_ROOT))))
    return tuple(rel for _, rel in sorted(found))


def newest_round_archive(basename: str) -> tuple[str, str]:
    """``(archive path, its round stem)``, or ``("-", "-")`` when nothing is archived.

    The stem comes back separately because a round can only claim its own archive: writing the
    round number into the body and copying the file sideways is caught by comparing the two.
    """
    archives = round_archives(basename)
    if not archives:
        return "-", "-"
    latest = archives[-1]
    return latest, latest.split("/")[2]


def measure_ac16_07(ctx: Context) -> Facts:
    """Run the P0 domain selection here, then read the newest archived clean-venv run."""
    ini = ctx.read("pytest.ini")
    units = integration_units_now()
    modules = sorted({unit.split("::")[0] for unit in units})
    text = "".join(ctx.read(rel) for rel in modules)
    domains = p0_domains_in_migration()
    missing = [domain for domain in domains if domain not in text]
    code, out = run_argv(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-m",
            P0_INTEGRATION_SELECTOR,
            "--no-header",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ]
    )
    counts = tally(out)
    archive_rel, archive_dir = newest_round_archive(CLEAN_RUN_BASENAME)
    evidence = ctx.read(archive_rel) if archive_rel != "-" else ""
    section = evidence.split(CLEAN_SECTION, 1)[1] if CLEAN_SECTION in evidence else evidence
    archived_modules = {
        line.split("::")[0].strip()
        for line in section.splitlines()
        if line.startswith("tests/") and "::" in line
    }
    declared_round = first_capture(evidence, r"^ARCHIVE_ROUND=(\S+)$") or "-"
    return {
        "registered": flag("integration" in registered_markers_in_ini(ini)),
        "strict": flag("--strict-markers" in ini),
        "units": count(len(units)),
        "unit_names": ", ".join(units),
        "domains": count(len(domains)),
        "domains_missing": count(len(missing)),
        "domain_names": ", ".join(missing) or "-",
        "selector": P0_INTEGRATION_SELECTOR,
        "run_exit": str(code),
        "passed": count(counts.get("passed", 0)),
        "failed": count(counts.get("failed", 0)),
        "skipped": count(counts.get("skipped", 0)),
        "deselected": count(counts.get("deselected", 0)),
        "clean_here": flag(
            all(importlib.util.find_spec(name) is None for name in UPSTREAM_PACKAGES)
        ),
        "clean_archive": archive_rel,
        "archive_round": declared_round,
        # 留档必须是自己写的那一份：把旧留档拷到新轮次目录下能骗过模块名对账，
        # 骗不过这一条 —— 正文声明的轮次标号会和对不上的目录名撞车。
        "archive_self_written": flag(declared_round == archive_dir and archive_dir != "-"),
        "archive_exit": first_capture(evidence, r"^CLEAN_RUN_EXIT=(\S+)$"),
        "archive_summary": first_capture(section, r"=+ (\d+ passed.*) =+"),
        "archive_modules": count(len(archived_modules)),
        "archive_blind": count(len([rel for rel in modules if rel not in archived_modules])),
        "archive_absent": flag(
            all(f"{name}: absent" in evidence for name in UPSTREAM_PACKAGES)
            and not any(f"{name}: present" in evidence for name in UPSTREAM_PACKAGES)
        ),
    }


def judge_ac16_07(facts: Facts) -> Verdict:
    """``AC-16|07``: a *named* P0 integration surface, passing with neither package installed."""
    ok = (
        facts["registered"] == "yes"
        and facts["strict"] == "yes"
        and number(facts["units"]) > 0
        and facts["domains_missing"] == "0"
        and facts["run_exit"] == "0"
        and number(facts["passed"]) > 0
        and facts["failed"] == "0"
        and facts["skipped"] == "0"
        and facts["clean_here"] == "yes"
        and facts["archive_exit"] == "0"
        and facts["archive_blind"] == "0"
        and facts["archive_self_written"] == "yes"
        and facts["archive_absent"] == "yes"
    )
    readings = (
        f"选择式 `-m '{facts['selector']}'` 有名字了：marker 注册 = {facts['registered']}，"
        f"--strict-markers = {facts['strict']}，标记单元 {facts['units']} 个 = "
        f"{facts['unit_names']}",
        f"P0 域覆盖：A4.1 迁移的 {facts['domains']} 个域全部被这组用例点到，缺 = "
        f"{facts['domains_missing']}（{facts['domain_names']}）",
        f"本机这一遍（解释器内 akshare/openbb 均不可导入 = {facts['clean_here']}）："
        f"exit={facts['run_exit']}，{facts['passed']} passed / {facts['failed']} failed / "
        f"{facts['skipped']} skipped；另有 {facts['deselected']} 条被选择式挡在外面"
        "（含全部仓库 e2e）",
        f"干净 venv 留档（读的是最新那一份：{facts['clean_archive']}，正文声明轮次 "
        f"{facts['archive_round']}，与所在目录一致 = {facts['archive_self_written']}）："
        f"CLEAN_RUN_EXIT={facts['archive_exit']}，"
        f"{facts['archive_summary'] or '(absent)'}；留档里两个上游包都记为 absent = "
        f"{facts['archive_absent']}；留档点到过 {facts['archive_modules']} 个模块，"
        f"当前标记集里没被留档覆盖的 = {facts['archive_blind']}",
    )
    reason = (
        ""
        if ok
        else "「P0 域集成测试」要么没有可指的名字（marker 没注册/没人用/某条 P0 域没被这组用例"
        "点到），要么这一遍没全绿、或者跳过了（skip 不等于通过），要么当前解释器里上游包又变得可"
        "导入，要么干净环境的留档不再覆盖现在这组标记模块（刻意的棘轮：给 P0 集成面加一个"
        "模块，就得重跑一次那个只装声明依赖的 venv 并重留档），要么那份留档只是从上一轮"
        "拷来的（正文声明的轮次标号对不上所在目录）"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


# --------------------------------------------------------------------------- #
# AC-11 -- the REST data plane, judged on the faces each item names
# --------------------------------------------------------------------------- #

#: Where the query/export/diff-report endpoints and the SQL they generate live.
PIPELINE_QUERY_REL: Final = "opendata/pipeline/query.py"
HTTP_WAREHOUSE_REL: Final = "tests/test_data_query_http_warehouse.py"
API_CONTRACT_REL: Final = "tests/test_data_query_api.py"
ADJUST_UNIT_REL: Final = "tests/test_data_query.py"
QFQ_OFFICIAL_REL: Final = "scripts/ops/qfq_official_check.py"
QFQ_OFFICIAL_RUN: Final = "docs/evidence/C56/qfq-official-akshare.txt"

#: The module AC-11|02 names when it says "akshare 官方 qfq": the ported akshare fetcher. A
#: leg that resolves anywhere else is some other vendor's chain wearing the criterion's word.
QFQ_AKSHARE_MODULE: Final = "opendata_http.stock_feature.stock_hist_em"

#: The knobs AC-11|01 enumerates, plus the pagination pair the same clause asks for.
QUERY_KNOBS: Final = ("symbols", "start", "end", "source", "layer", "adjust", "fields")
PAGINATION_KNOBS: Final = ("page", "page_size")

#: A node id that no longer exists aborts the whole run and names itself in this line.
ABSENT_NODE = re.compile(r"ERROR: (?:not found|file or directory not found): (\S+)")

#: An executed plan check, not prose about one: the keyword either introduces a statement written
#: out in full, or it is the prefix of a concatenation the code then executes. The second shape is
#: what an honest instrument has to write - the SQL has to come from ``build_data_select`` for the
#: plan to be about the endpoint's query, so it can never be a literal ``EXPLAIN SELECT``.
EXPLAIN_STATEMENT = re.compile(
    r"\bEXPLAIN\s+(?:ANALYZE\s+)?(?:SELECT|FORMAT)\b|\"EXPLAIN \"\s*\+",
    re.IGNORECASE,
)

#: Where a plan check would have to live to be a face rather than a sentence.
EXPLAIN_ROOTS: Final = ("tests", "scripts", "opendata", "alembic_data")

#: ``AC-11|01``: the documented route, its enumerated knobs, and the merged default view.
AC11_ROUTE_NODES: Final = (
    f"{API_CONTRACT_REL}::TestOpenApi::test_routes_are_published",
    f"{API_CONTRACT_REL}::TestOpenApi::test_query_parameters_are_enumerated",
    f"{HTTP_WAREHOUSE_REL}::TestQuerySchema::test_the_defaults_are_the_merged_unadjusted_view",
    f"{HTTP_WAREHOUSE_REL}::TestQuerySchema::"
    "test_a_row_carries_exactly_the_selected_contract_columns",
    f"{HTTP_WAREHOUSE_REL}::TestQuerySchema::"
    "test_pagination_walks_the_business_key_without_repeating_a_row",
    f"{HTTP_WAREHOUSE_REL}::TestQuerySchema::"
    "test_an_explicit_window_is_the_date_column_between_its_bounds",
)

#: ``AC-11|02``: synthesis through the HTTP handler, the factor arithmetic, and the judge.
AC11_ADJUST_NODES: Final = (
    f"{HTTP_WAREHOUSE_REL}::TestServerSideAdjust::"
    "test_qfq_scales_the_prices_and_leaves_the_volume_alone",
    f"{HTTP_WAREHOUSE_REL}::TestServerSideAdjust::"
    "test_hfq_scales_by_the_other_leg_of_the_same_factor_row",
    f"{HTTP_WAREHOUSE_REL}::TestServerSideAdjust::"
    "test_a_bar_without_its_factor_row_is_a_400_not_a_half_adjusted_series",
    f"{HTTP_WAREHOUSE_REL}::TestServerSideAdjust::"
    "test_the_absent_factor_table_is_a_501_that_names_the_missing_table",
    f"{HTTP_WAREHOUSE_REL}::TestServerSideAdjust::"
    "test_an_empty_selection_does_not_claim_an_adjusted_series",
    f"{ADJUST_UNIT_REL}::TestAdjust::test_qfq_scales_by_the_factor",
    f"{ADJUST_UNIT_REL}::TestAdjust::test_missing_factor_fails_closed",
    "tests/test_qfq_official_check.py::TestCompare::test_matching_series_passes",
    "tests/test_qfq_official_check.py::TestCompare::"
    "test_constant_anchor_offset_is_a_level_difference_not_a_failure",
    "tests/test_qfq_official_check.py::TestCompare::test_shape_divergence_fails",
    "tests/test_qfq_official_check.py::TestCompare::"
    "test_the_over_count_is_the_population_and_not_the_listing_cap",
    "tests/test_qfq_official_check.py::TestCompare::test_no_common_dates_is_a_failure",
    "tests/test_qfq_official_check.py::TestOfficialLegMap::"
    "test_the_map_offers_akshare_and_keeps_sina",
    "tests/test_qfq_official_check.py::TestOfficialLegMap::"
    "test_default_leg_is_akshare_as_the_criterion_names",
    "tests/test_qfq_official_check.py::TestOfficialLegMap::"
    "test_akshare_leg_resolves_to_the_ported_module",
    "tests/test_qfq_official_check.py::TestOfficialLegMap::"
    "test_the_resolved_akshare_module_declares_its_provenance",
    "tests/test_qfq_official_check.py::TestOfficialLegMap::"
    "test_each_leg_fetcher_is_the_function_its_target_names",
    "tests/test_qfq_official_check.py::TestOfficialLegMap::"
    "test_unknown_leg_fails_closed_instead_of_defaulting",
    "tests/test_qfq_official_check.py::TestOfficialLegMap::"
    "test_cli_refuses_a_leg_that_is_not_in_the_map",
    "tests/test_qfq_official_check.py::TestOfficialLegMap::"
    "test_akshare_leg_gets_the_bare_code_and_chinese_columns",
    "tests/test_qfq_official_check.py::TestOfficialLegMap::"
    "test_prefixed_leg_gets_the_exchange_symbol",
    "tests/test_qfq_official_check.py::TestOfficialLegMap::test_a_flaky_leg_retries_and_says_so",
    "tests/test_qfq_official_check.py::TestOfficialLegMap::"
    "test_exhausted_leg_raises_instead_of_comparing_nothing",
)

#: ``AC-11|03``: the four safety faces the criterion lists, on the HTTP entry point.
AC11_SAFETY_NODES: Final = (
    f"{HTTP_WAREHOUSE_REL}::TestParameterSafety::"
    "test_a_fields_value_that_is_not_a_column_is_a_400_and_nothing_ran",
    f"{HTTP_WAREHOUSE_REL}::TestParameterSafety::"
    "test_a_source_that_is_not_a_registered_leg_is_a_400",
    f"{HTTP_WAREHOUSE_REL}::TestParameterSafety::"
    "test_a_symbol_that_looks_like_sql_stays_a_bound_literal",
    f"{HTTP_WAREHOUSE_REL}::TestParameterSafety::test_export_and_diff_report_are_authenticated_too",
    f"{HTTP_WAREHOUSE_REL}::TestCsvExport::"
    "test_formula_prefixed_cells_are_neutralized_in_the_export",
    f"{HTTP_WAREHOUSE_REL}::TestCsvExport::test_a_negative_price_stays_a_number_in_the_export",
    f"{HTTP_WAREHOUSE_REL}::TestCsvExport::"
    "test_the_export_is_a_named_attachment_of_the_selected_columns",
    f"{HTTP_WAREHOUSE_REL}::TestCsvExport::test_a_bad_field_is_answered_400_before_the_body_starts",
    f"{API_CONTRACT_REL}::TestValidationPaths::test_bad_layer_is_a_422",
    f"{API_CONTRACT_REL}::TestValidationPaths::test_bad_adjust_is_a_422",
    f"{API_CONTRACT_REL}::TestValidationPaths::test_ods_layer_requires_an_explicit_source",
    f"{ADJUST_UNIT_REL}::TestBuildDataSelect::test_injection_attempt_stays_a_literal_parameter",
    f"{ADJUST_UNIT_REL}::TestFieldWhitelist::test_unknown_field_is_rejected",
)

#: ``AC-11|04``: an unbounded request still gets a window, at the handler and at the SQL.
AC11_WINDOW_NODES: Final = (
    f"{HTTP_WAREHOUSE_REL}::TestDefaultWindow::"
    "test_an_unbounded_request_still_does_not_reach_outside_the_window",
    f"{HTTP_WAREHOUSE_REL}::TestDefaultWindow::"
    "test_the_generated_select_predicates_on_the_partition_key_and_caps_rows",
    f"{ADJUST_UNIT_REL}::TestWindowBounds::test_default_window_is_applied_when_no_range_is_given",
    f"{ADJUST_UNIT_REL}::TestWindowBounds::test_explicit_range_is_honoured",
    f"{ADJUST_UNIT_REL}::TestWindowBounds::test_reversed_range_fails_closed",
)

#: ``AC-11|05``: the diff report door, and its route in the published document.
AC11_DIFF_NODES: Final = (
    f"{HTTP_WAREHOUSE_REL}::TestDiffReport::"
    "test_the_report_answers_the_sampled_differences_of_the_domain",
    f"{HTTP_WAREHOUSE_REL}::TestDiffReport::test_batch_id_selects_one_cross_check_run",
    f"{HTTP_WAREHOUSE_REL}::TestDiffReport::test_the_limit_is_bounded_like_the_query_page_size",
    f"{HTTP_WAREHOUSE_REL}::TestDiffReport::"
    "test_an_unmigrated_report_table_reads_empty_not_as_an_error",
    f"{API_CONTRACT_REL}::TestOpenApi::test_routes_are_published",
)


def parameter_faces(source: str, name: str) -> dict[str, tuple[str, str]]:
    """One handler's parameters mapped to their declared annotation and default.

    Read from the AST because AC-11|01's claim is about what the route *declares*: a docstring can
    promise ``layer=dwd`` while the signature serves ``ods``, and the default FastAPI actually
    hands the handler is the first positional argument of ``Query(...)``.

    Args:
        source: Module source text.
        name: Handler function name.

    Returns:
        ``(annotation, default)`` per parameter; empty when this module has no such function.
    """
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != name:
            continue
        faces: dict[str, tuple[str, str]] = {}
        positional = [*node.args.posonlyargs, *node.args.args]
        defaults: list[ast.expr | None] = [None] * (len(positional) - len(node.args.defaults))
        defaults += list(node.args.defaults)
        for param, value in zip(positional, defaults, strict=True):
            faces[param.arg] = _annotation_default(param, value)
        for param, value in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
            faces[param.arg] = _annotation_default(param, value)
        return faces
    return {}


def _annotation_default(param: ast.arg, default: ast.expr | None) -> tuple[str, str]:
    """The annotation text and the unwrapped default of one parameter."""
    annotation = ast.unparse(param.annotation) if param.annotation is not None else ""
    if default is None:
        return annotation, ""
    value = default.args[0] if isinstance(default, ast.Call) and default.args else default
    return annotation, ast.unparse(value).strip("'\"")


def node_plane_facts(prefix: str, nodes: Sequence[str]) -> Facts:
    """Run one item's named nodes in a single pytest process and read the counts off that run.

    One process per item rather than one per node id: every node here lives behind the same
    fixtures, so the cheaper reading still answers the falsifiable question -- an id that was
    renamed aborts the invocation and names itself, which lands in ``{prefix}_absent`` instead of
    quietly shrinking the plane to whatever still resolves.

    Args:
        prefix: Fact-name prefix, so an item can hold two planes.
        nodes: Exact node ids.

    Returns:
        Facts ``{prefix}_runs/_exit/_passed/_failed/_skipped/_absent``.
    """
    code, out = run_pytest(list(nodes))
    counts = tally(out)
    absent = [found.rsplit("::", 1)[-1].rstrip(",.") for found in ABSENT_NODE.findall(out)]
    return {
        f"{prefix}_runs": count(len(nodes)),
        f"{prefix}_exit": str(code),
        f"{prefix}_passed": count(counts.get("passed", 0)),
        f"{prefix}_failed": count(counts.get("failed", 0)),
        f"{prefix}_skipped": count(counts.get("skipped", 0)),
        f"{prefix}_absent": ", ".join(absent) or "-",
    }


def plane_is_green(facts: Facts, prefix: str) -> bool:
    """Whether a node plane ran, collected every id it names, and passed every one of them."""
    return (
        positive(facts[f"{prefix}_runs"])
        and facts[f"{prefix}_exit"] == "0"
        and facts[f"{prefix}_passed"] == facts[f"{prefix}_runs"]
        and facts[f"{prefix}_failed"] == "0"
        and facts[f"{prefix}_skipped"] == "0"
        and facts[f"{prefix}_absent"] == "-"
    )


def plane_reading(facts: Facts, prefix: str, label: str) -> str:
    """One line describing a node plane, naming whatever did not pass."""
    if facts[f"{prefix}_absent"] != "-":
        tail = f"; ids the run could not find: {facts[f'{prefix}_absent']}"
    elif facts[f"{prefix}_passed"] != facts[f"{prefix}_runs"]:
        tail = f"; exit={facts[f'{prefix}_exit']} summary={facts[f'{prefix}_passed']} passed / "
        tail += f"{facts[f'{prefix}_failed']} failed / {facts[f'{prefix}_skipped']} skipped"
    else:
        tail = ""
    return f"{label}: {facts[f'{prefix}_passed']}/{facts[f'{prefix}_runs']} nodes passed{tail}"


def measure_ac11_01(ctx: Context) -> Facts:
    """Run the route plane, then read the knobs and defaults the handler itself declares."""
    api = ctx.read(DATA_QUERY_REL)
    params = parameter_faces(api, "query_domain_data")
    body = function_body(api, "query_domain_data")
    missing = [knob for knob in (*QUERY_KNOBS, *PAGINATION_KNOBS) if knob not in params]
    facts: Facts = {
        **node_plane_facts("route", AC11_ROUTE_NODES),
        "route_shape": flag('"/{asset_class}/{domain}"' in api),
        "knobs_missing": ", ".join(missing) or "-",
        "layer_default": params.get("layer", ("", ""))[1] or "(absent)",
        "adjust_default": params.get("adjust", ("", ""))[1] or "(absent)",
        "payload_shape": flag('"columns"' in body and '"rows"' in body),
    }
    return facts


def judge_ac11_01(facts: Facts) -> Verdict:
    """``AC-11|01``: the documented route carries every named knob and the declared defaults."""
    ok = (
        plane_is_green(facts, "route")
        and facts["route_shape"] == "yes"
        and facts["knobs_missing"] == "-"
        and facts["layer_default"] == "dwd"
        and facts["adjust_default"] == "none"
        and facts["payload_shape"] == "yes"
    )
    readings = (
        plane_reading(facts, "route", "路由/契约面（OpenAPI 两条 + 替身仓库 HTTP 四条）"),
        f"路由形状 ``/{{asset_class}}/{{domain}}`` = {facts['route_shape']}，"
        f"载荷同时交回 columns 与 rows（响应 schema 可对契约） = {facts['payload_shape']}",
        f"判据点名的 9 个入参都在处理器的签名里：缺 = {facts['knobs_missing']}"
        "（AST 读数，注释里写什么不算）",
        f"声明的默认值：layer = {facts['layer_default']}，adjust = {facts['adjust_default']}；"
        "这两条是契约的一部分，改一个就等于改了默认口径",
    )
    reason = (
        ""
        if ok
        else "「支持 symbols/start/end/source/layer/adjust/fields/分页，默认 layer=dwd、"
        "adjust=none，响应 schema 与契约一致，OpenAPI 文档生成」是四问：路由与文档在、入参齐、"
        "默认值就是那两个、返回的行真的按契约列交出来——任何一条从签名或这一遍里掉出去都只剩接口存在"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac11_02(ctx: Context) -> Facts:
    """Run the adjust plane, then read which official series the cross-source check resolves to.

    Four independent readings replace the old "first ``stock_zh_a_*`` inside the fetcher": with a
    dispatch table in place that capture is a proxy for whichever module the table names, and a
    string could then be made to read as akshare without anything resolving to it.
    """
    api = ctx.read(DATA_QUERY_REL)
    checker = ctx.read(QFQ_OFFICIAL_REL)
    target = map_entry_field(checker, "OFFICIAL_LEGS", "akshare", "target")
    module = target.partition(":")[0]
    leg_path = REPO_ROOT / f"{module.replace('.', '/')}.py"
    header = (
        "".join(leg_path.read_text(encoding="utf-8").splitlines(keepends=True)[:6])
        if leg_path.is_file()
        else ""
    )
    run = ctx.read(QFQ_OFFICIAL_RUN)
    facts: Facts = {
        **node_plane_facts("adj", AC11_ADJUST_NODES),
        "synthesis": flag("apply_adjust_to_rows" in function_body(api, "_adjusted_rows")),
        "default_leg": first_capture(checker, r'DEFAULT_OFFICIAL_LEG[^A-Za-z]*"(\w+)"'),
        "leg_target": target,
        "leg_module": module,
        "leg_is_ported_akshare": flag("# Ported from akshare" in header),
        "run_official_leg": first_capture(run, r"^official: (\w+)"),
        "run_official_target": first_capture(run, r"^official: \w+ (\S+)"),
        "run_ok_rows": count(len(re.findall(r"\|\s*PASS\s*\|", run))),
        "run_fail_rows": count(len(re.findall(r"\|\s*FAIL\s*\|", run))),
        "run_error_rows": count(len(re.findall(r"\|\s*ERROR\b", run))),
    }
    return facts


def judge_ac11_02(facts: Facts) -> Verdict:
    """``AC-11|02``: qfq/hfq are computed server-side *and* checked against official qfq."""
    unmet: list[str] = []
    if not plane_is_green(facts, "adj"):
        unmet.append("复权/对照节点面没跑齐")
    if facts["synthesis"] != "yes":
        unmet.append("服务端合成接线不成立（参数读一遍就丢）")
    if (
        facts["default_leg"] != "akshare"
        or facts["leg_module"] != QFQ_AKSHARE_MODULE
        or facts["leg_is_ported_akshare"] != "yes"
    ):
        unmet.append(
            f"官方对照腿没解析到 akshare 搬运模块（默认腿 {facts['default_leg']} → "
            f"{facts['leg_module']}，akshare 出身={facts['leg_is_ported_akshare']}）"
        )
    if facts["run_official_leg"] != facts["default_leg"]:
        unmet.append(
            f"留档那次 run 走的腿是 {facts['run_official_leg']}，与当场默认腿 "
            f"{facts['default_leg']} 不一致"
        )
    if facts["run_official_target"] != facts["leg_target"]:
        unmet.append(
            f"留档那次 run 记的 module:function 是 {facts['run_official_target']}，与分派表现值 "
            f"{facts['leg_target']} 不一致（仪器改过，run 没重跑）"
        )
    if facts["run_error_rows"] != "0":
        unmet.append(
            f"留档那次 run 有 {facts['run_error_rows']} 行 ERROR —— 官方端点没答上来等于没对照"
        )
    if facts["run_fail_rows"] != "0" or not positive(facts["run_ok_rows"]):
        unmet.append(
            f"真机对照未过：FAIL {facts['run_fail_rows']} 行 / PASS {facts['run_ok_rows']} 行"
            "（容差 2e-3 不放宽，见档案里的 over_tolerance 与 ratio 平台分布）"
        )
    readings = (
        plane_reading(facts, "adj", "复权面（HTTP 五条 + 因子算术两条 + 判定与对照腿十五条）"),
        f"服务端合成 = {facts['synthesis']}（``_adjusted_rows`` 真的调 ``apply_adjust_to_rows``，"
        "不是把参数读一遍就丢掉）",
        f"官方对照腿：``{QFQ_OFFICIAL_REL}`` 的 ``DEFAULT_OFFICIAL_LEG`` = {facts['default_leg']}，"
        f"分派表 akshare 条目 target = {facts['leg_target']}，该模块头部声明 akshare 出身 = "
        f"{facts['leg_is_ported_akshare']}（判据点名的就是 ``{QFQ_AKSHARE_MODULE}``）",
        f"留档真机 run（{QFQ_OFFICIAL_RUN}）：头部记的腿 = {facts['run_official_leg']}，"
        f"module:function = {facts['run_official_target']}；PASS {facts['run_ok_rows']} 行 / "
        f"FAIL {facts['run_fail_rows']} 行 / ERROR {facts['run_error_rows']} 行",
    )
    reason = (
        ""
        if not unmet
        else "；".join(unmet)
        + " —— 合成与对照的判定口径不变（逐日 shape dev 对 2e-3），要改的是证据不是判据"
    )
    return Verdict(PROVEN if not unmet else GAP, readings, reason)


def measure_ac11_03(ctx: Context) -> Facts:
    """Run the safety plane, then read which of its four faces the code really enforces."""
    api = ctx.read(DATA_QUERY_REL)
    sql = ctx.read(PIPELINE_QUERY_REL)
    select_body = function_body(sql, "build_data_select")
    params = parameter_faces(api, "query_domain_data")
    validated = function_body(api, "_validated_query")
    unenumerated = [
        name for name in ("layer", "adjust") if "Literal" not in params.get(name, ("", ""))[0]
    ]
    facts: Facts = {
        **node_plane_facts("safety", AC11_SAFETY_NODES),
        "deny_literal": flag("open;DROP TABLE" in ctx.read(HTTP_WAREHOUSE_REL)),
        "enum_params": ", ".join(unenumerated) or "-",
        "source_leg_check": flag("not a registered leg" in validated),
        "bind_symbols": flag(re.search(r"params\[[^\]]+\]\s*=\s*symbol", select_body) is not None),
        "symbol_placeholder": flag(re.search(r'":\{\w+\}', select_body) is not None),
        "csv_escape": flag("serialize_for_csv" in function_body(api, "_csv_stream")),
    }
    return facts


def judge_ac11_03(facts: Facts) -> Verdict:
    """``AC-11|03``: whitelist 400, three enums, bound symbols, CSV escaping — four named faces."""
    ok = (
        plane_is_green(facts, "safety")
        and facts["deny_literal"] == "yes"
        and facts["enum_params"] == "-"
        and facts["source_leg_check"] == "yes"
        and facts["bind_symbols"] == "yes"
        and facts["symbol_placeholder"] == "yes"
        and facts["csv_escape"] == "yes"
    )
    readings = (
        plane_reading(facts, "safety", "参数安全面（HTTP 13 条，全部走真路由）"),
        f"判据那条字面载荷 ``fields=open;DROP TABLE`` 出现在用例里 = {facts['deny_literal']}；"
        "这条断言除了 400 还回读表里的行数，所以「没执行」是被量的而不是被说的",
        f"枚举校验：layer/adjust 的注解里还有不是 Literal 的吗 = {facts['enum_params']}；"
        f"source 按注册腿校验 = {facts['source_leg_check']}"
        "（判据列了三个枚举面，少一个就只做到两个）",
        f"symbols 的取值进的是绑定字典 = {facts['bind_symbols']}，SQL 文本里出现的只有 "
        f"``:{{name}}`` 占位 = {facts['symbol_placeholder']}；导出公式转义发生在 handler 里 = "
        f"{facts['csv_escape']}（``_csv_stream`` 调 ``serialize_for_csv``）",
    )
    reason = (
        ""
        if ok
        else "判据把四个面点名列出：fields 白名单（并给出 ``fields=open;DROP TABLE`` 这条载荷）、"
        "layer/source/adjust 三个枚举、symbols 绑定、导出 CSV 公式转义。任何一面从代码里退回去"
        "（比如 source 又变成自由字符串），这一条就只是用例绿而不是参数安全"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac11_04(ctx: Context) -> Facts:
    """Run the window plane, then read the live execution plan the criterion names as its proof."""
    sql = ctx.read(PIPELINE_QUERY_REL)
    scan = [
        rel
        for root in EXPLAIN_ROOTS
        for rel in py_files_under(root)
        if not rel.endswith("acceptance_item_probe.py")
    ]
    hits = grep_files(scan, EXPLAIN_STATEMENT)
    archive_rel, archive_dir = newest_round_archive(WINDOW_EXPLAIN_BASENAME)
    body = ctx.read(archive_rel) if archive_rel != "-" else ""
    declared_round = first_capture(body, r"^ARCHIVE_ROUND=(\S+)$") or "-"
    archived_digest = first_capture(body, rf"^{QUERY_DIGEST_KEY}=([0-9a-f]+)") or "-"
    digest_now = hashlib.sha256((REPO_ROOT / PIPELINE_QUERY_REL).read_bytes()).hexdigest()[:12]
    landed = first_capture(body, r"landed_partitioned_tables=(\d+)") or "0"
    facts: Facts = {
        **node_plane_facts("window", AC11_WINDOW_NODES),
        "window_days": first_capture(sql, r"^DEFAULT_WINDOW_DAYS = (\d+)"),
        "bounded_default": flag("DEFAULT_WINDOW_DAYS" in function_body(sql, "window_bounds")),
        "row_cap": flag("LIMIT :limit" in function_body(sql, "build_data_select")),
        "explain_faces": count(sum(hits.values())),
        "explain_paths": ", ".join(sorted(hits)) or "-",
        "scanned_code_files": count(len(scan)),
        "explain_archive": archive_rel,
        "explain_archive_round": declared_round,
        "explain_archive_in_place": flag(archive_dir != "-" and declared_round == archive_dir),
        "explain_archive_digest": archived_digest,
        "explain_query_digest": digest_now,
        "explain_archive_is_current": flag(
            archived_digest != "-" and archived_digest == digest_now
        ),
        "explain_tables_landed": count(int(landed)),
        "explain_pruned_yes": first_capture(body, r"pruned_yes=(\d+)") or "-",
        "explain_pruned_no": first_capture(body, r"pruned_no=(\d+)") or "-",
    }
    return facts


def judge_ac11_04(facts: Facts) -> Verdict:
    """``AC-11|04``: an unbounded query lands in a window, and a live plan says it prunes."""
    ok = (
        plane_is_green(facts, "window")
        and positive(facts["window_days"])
        and facts["bounded_default"] == "yes"
        and facts["row_cap"] == "yes"
        and number(facts["explain_faces"]) > 0
        and number(facts["explain_tables_landed"]) > 0
        and facts["explain_pruned_no"] == "0"
        and facts["explain_archive_is_current"] == "yes"
        and facts["explain_archive_in_place"] == "yes"
    )
    readings = (
        plane_reading(facts, "window", "时间窗面（HTTP 两条 + 边界单元三条）"),
        f"默认窗 = {facts['window_days']} 天，由 ``window_bounds`` 在无区间时补上 = "
        f"{facts['bounded_default']}；``build_data_select`` 的 ``LIMIT :limit`` 行数上限 = "
        f"{facts['row_cap']}",
        f"判据点名的验证方式：在 {facts['scanned_code_files']} 个代码文件里找真发出去的 EXPLAIN"
        '（整句字面量 ``EXPLAIN [ANALYZE] SELECT|FORMAT`` 与拼接前缀 ``"EXPLAIN " +`` 两种形状，'
        f"仪器必须拼 SQL 才谈得上「这个端点的查询」），命中 {facts['explain_faces']} 处"
        f"（{facts['explain_paths']}）",
        f"执行计划留档（读的是最新那一份：{facts['explain_archive']}，正文声明轮次 "
        f"{facts['explain_archive_round']}，与所在目录一致 = "
        f"{facts['explain_archive_in_place']}）："
        f"已落地且带分区的 dwd 表 {facts['explain_tables_landed']} 张，其中计划只读到部分分区的 "
        f"{facts['explain_pruned_yes']} 张、读到全部分区的 {facts['explain_pruned_no']} 张",
        f"留档属于哪一版拼 SQL 的代码：正文 ``{QUERY_DIGEST_KEY}="
        f"{facts['explain_archive_digest']}`` 对当下 ``{PIPELINE_QUERY_REL}`` = "
        f"{facts['explain_query_digest']} → {facts['explain_archive_is_current']}",
    )
    reason = ""
    if not ok:
        missing: list[str] = []
        if number(facts["explain_faces"]) == 0:
            missing.append("全库没有一处执行过的 EXPLAIN，只有散文里那句话")
        if number(facts["explain_tables_landed"]) == 0:
            missing.append("留档没量到任何一张已落地的分区表")
        if facts["explain_pruned_no"] != "0":
            missing.append(f"留档里有 {facts['explain_pruned_no']} 张表的计划读到了全部分区")
        if facts["explain_archive_is_current"] != "yes":
            missing.append(
                "留档正文的 query_module_sha 和当下的查询层代码对不上"
                "（拼 SQL 的代码改过，那份计划已经不是现在这一条）"
            )
        if facts["explain_archive_in_place"] != "yes":
            missing.append("留档正文声明的轮次与它所在目录不一致（像是从上一轮拷来的）")
        reason = (
            "窗口本身是实测绿的（不带区间也给边界、行数有上限、生成的 SELECT 以分区键为谓词），"
            "判据点名的验证方式是 EXPLAIN —— 缺的是：" + "；".join(missing)
            if missing
            else "时间窗面不再全绿，或默认窗／行数上限被摘掉：那一条 SQL 已经不是被量过的那一条"
        )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac11_05(ctx: Context) -> Facts:
    """Run the diff-report plane, then read the door's route, gate, filter and cap."""
    api = ctx.read(DATA_QUERY_REL)
    body = function_body(api, "domain_diff_report")
    facts: Facts = {
        **node_plane_facts("diff", AC11_DIFF_NODES),
        "route": flag('"/domains/{domain}/diff-report"' in api),
        "table_constant": first_capture(api, r'^DIFF_TABLE = "([^"]+)"'),
        "reads_constant_table": flag(
            "FROM `dq_diff_report`" in body or "FROM `{DIFF_TABLE}`" in body
        ),
        "auth_gate": flag("require_domain_access" in body),
        "batch_filter": flag("batch_id" in body),
        "limit_bounded": flag("le=MAX_PAGE_SIZE" in body),
    }
    return facts


def judge_ac11_05(facts: Facts) -> Verdict:
    """``AC-11|05``: the diff-report door answers with the domain's sampled differences."""
    ok = (
        plane_is_green(facts, "diff")
        and facts["route"] == "yes"
        and facts["table_constant"] == "dq_diff_report"
        and facts["reads_constant_table"] == "yes"
        and facts["auth_gate"] == "yes"
        and facts["batch_filter"] == "yes"
        and facts["limit_bounded"] == "yes"
    )
    readings = (
        plane_reading(facts, "diff", "差异报告面（HTTP 四条 + OpenAPI 一条）"),
        f"路由 ``/domains/{{domain}}/diff-report`` 已注册并进 OpenAPI = {facts['route']}，"
        f"读的是 {facts['table_constant']}（与校对器写的明细表同一个常量） = "
        f"{facts['reads_constant_table']}",
        f"门禁 = {facts['auth_gate']}（按域的 scope 先判），``batch_id`` 只取一次校对 = "
        f"{facts['batch_filter']}，``limit`` 与查询页同带上限 = {facts['limit_bounded']}",
        "四条 HTTP 用例分别钉住：差异行真的交出来、按 batch_id 选一次跑、limit 有上限、"
        "表还没迁移时读出空而不是 500",
    )
    reason = (
        ""
        if ok
        else "「可查差异报告」要的是这个门真的从 dq_diff_report 取数并受同一套 scope/上限约束："
        "路由没了、表名和校对器写的明细表分家了、或者未迁移的表开始抛 500，这条就退回未验收"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


# --------------------------------------------------------------------------- #
# AC-5: the ported tree (manifest, package breadth, headers, resources, security)
# --------------------------------------------------------------------------- #

PORTED_ROOT: Final = "opendata_http"
PORTED_MANIFEST: Final = f"{PORTED_ROOT}/manifest.json"
GEN_MANIFEST_TOOL: Final = "scripts/codemod/gen_manifest.py"
DATASETS_REL: Final = f"{PORTED_ROOT}/datasets.py"
REQ_DOC_REL: Final = "docs/迭代计划/迭代1-重构数据中台/需求文档.md"
A2_TRIAGE_REL: Final = "docs/evidence/A2/bandit-ported-triage.md"
PORTED_BANDIT_BASENAME: Final = "ported-bandit-scan.json"
FIRST_PARTY_TREES: Final = ("opendata", "opendata_client", "opendata_fuyao", "scripts", "tests")
SECURITY_PORTED_TARGET: Final = "security-ported"
BANDIT_CONFIG: Final = "bandit.yaml"

#: Imported in a child process so the callable face of the flat API is read from the same
#: interpreter AC-16|07 proves carries no ``akshare``: an attribute the aggregator does not
#: re-export is a function a first-party leg would fail to reach at routing time.
PORTED_CALL_SNIPPET: Final = (
    "import sys, opendata_http as ak\n"
    "missing = [n for n in sys.argv[1:] if not callable(getattr(ak, n, None))]\n"
    "print('\\n'.join(f'MISSING:{n}' for n in missing))\n"
)


def manifest_entries(payload: dict[str, object], key: str) -> list[dict[str, object]]:
    """One of the manifest's entry lists, or a failure naming the shape that moved."""
    raw = payload.get(key)
    if not isinstance(raw, list):
        raise ProbeError(f"{PORTED_MANIFEST} has no {key} list")
    return [entry for entry in raw if isinstance(entry, dict)]


def manifest_mapping(payload: dict[str, object], key: str) -> dict[str, object]:
    """One of the manifest's sub-mappings (``counts`` / ``upstream``), empty when it moved."""
    raw = payload.get(key)
    return dict(raw) if isinstance(raw, dict) else {}


@lru_cache(maxsize=1)
def ported_manifest() -> dict[str, object]:
    """The ported tree's own manifest, read once per run."""
    path = REPO_ROOT / PORTED_MANIFEST
    if not path.is_file():
        raise ProbeError(f"{PORTED_MANIFEST} is missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ProbeError(f"{PORTED_MANIFEST} is not a mapping")
    return payload


def entry_path(entry: dict[str, object]) -> str:
    """A manifest entry's path, relative to the ported root."""
    return str(entry.get("path", ""))


@lru_cache(maxsize=1)
def ported_call_targets() -> tuple[str, ...]:
    """Every name first-party code calls on the ported flat API, import aliases included.

    ``import opendata_http as ak`` is as much a claim on the aggregator's surface as
    ``opendata_http.stock_zh_a_hist()``, so the alias is resolved per file before the attributes
    under it are counted; a leg that reaches for a name the facade does not re-export is a routing
    failure waiting for the next ``fetch``.
    """
    found: set[str] = set()
    for tree in FIRST_PARTY_TREES:
        base = REPO_ROOT / tree
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                parsed = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            bound = {PORTED_ROOT}
            for node in ast.walk(parsed):
                if isinstance(node, ast.Import):
                    bound.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name == PORTED_ROOT
                    )
            for node in ast.walk(parsed):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                receiver = node.func.value
                if isinstance(receiver, ast.Name) and receiver.id in bound:
                    found.add(node.func.attr)
    return tuple(sorted(found))


@lru_cache(maxsize=1)
def ported_reexports() -> frozenset[str]:
    """The flat API names the ported aggregator re-exports from its submodules."""
    text = (REPO_ROOT / PORTED_ROOT / "__init__.py").read_text(encoding="utf-8", errors="replace")
    names: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.ImportFrom) and str(node.module or "").startswith(PORTED_ROOT):
            names.update(alias.asname or alias.name for alias in node.names)
    return frozenset(names)


@lru_cache(maxsize=1)
def ported_batch_field() -> str:
    """Which machine-readable field states a domain's 1A/1B batch, or ``-`` when none does."""
    payload = yaml.safe_load((REPO_ROOT / "opendata/data/domains.yaml").read_text(encoding="utf-8"))
    domains = payload.get("domains") if isinstance(payload, dict) else None
    rows = list(domains.values()) if isinstance(domains, dict) else list(domains or [])
    keys = {str(key) for row in rows if isinstance(row, dict) for key in row}
    for wanted in ("batch", "priority", "tier"):
        if wanted in keys:
            return f"opendata/data/domains.yaml#{wanted}"
    capability = (REPO_ROOT / "opendata/data/capability.py").read_text(encoding="utf-8")
    for wanted in ("batch", "priority", "tier"):
        if re.search(rf"^    {wanted}: ", capability, re.MULTILINE):
            return f"opendata/data/capability.py#{wanted}"
    return "-"


def measure_ac5_01(ctx: Context) -> Facts:
    """Recompute every manifest entry against the disk instead of trusting its numbers."""
    code, out = run_argv([sys.executable, GEN_MANIFEST_TOOL, "--check"])
    payload = ported_manifest()
    files = manifest_entries(payload, "files")
    resources = manifest_entries(payload, "resources")
    base = REPO_ROOT / PORTED_ROOT
    mismatch, absent = [], []
    for entry in files + resources:
        path = base / entry_path(entry)
        if not path.is_file():
            absent.append(entry_path(entry))
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != str(entry.get("sha256")):
            mismatch.append(entry_path(entry))
    disk_py = sorted(
        path.relative_to(base).as_posix()
        for path in base.rglob("*.py")
        if "__pycache__" not in path.parts
    )
    disk_other = sorted(
        path.relative_to(base).as_posix()
        for path in base.rglob("*")
        if path.is_file()
        and path.suffix != ".py"
        and "__pycache__" not in path.parts
        and path.name not in ("manifest.json", "upstream.lock")
    )
    listed_py = sorted(entry_path(entry) for entry in files)
    listed_res = sorted(entry_path(entry) for entry in resources)
    unlisted = sorted(set(disk_py) - set(listed_py))
    stale = sorted(set(listed_py) - set(disk_py))
    res_unregistered = sorted(set(disk_other) - set(listed_res))
    res_stale = sorted(set(listed_res) - set(disk_other))
    lines_now = sum(
        len((base / name).read_text(encoding="utf-8", errors="replace").splitlines())
        for name in listed_py
        if (base / name).is_file()
    )
    counts = manifest_mapping(payload, "counts")
    lock_paths = {str(entry.get("path", "")) for entry in lock_records(ctx)}
    return {
        "check_exit": str(code),
        "check_note": (out.strip().splitlines() or ["(no output)"])[-1][:120],
        "listed_py": count(len(listed_py)),
        "listed_res": count(len(listed_res)),
        "listed_total": count(len(files) + len(resources)),
        "disk_py": count(len(disk_py)),
        "disk_other": count(len(disk_other)),
        "sha_checked": count(len(files) + len(resources) - len(absent)),
        "sha_mismatch": count(len(mismatch)),
        "sha_absent": count(len(absent)),
        "unlisted_on_disk": count(len(unlisted)),
        "unlisted_names": ", ".join(unlisted[:4]) or "-",
        "stale_entries": count(len(stale)),
        "stale_names": ", ".join(stale[:4]) or "-",
        "res_unregistered": count(len(res_unregistered)),
        "res_unregistered_names": ", ".join(res_unregistered[:4]) or "-",
        "res_stale": count(len(res_stale)),
        "count_py_ok": flag(str(counts.get("py_files")) == str(len(listed_py))),
        "count_res_ok": flag(str(counts.get("resource_files")) == str(len(listed_res))),
        "count_total_ok": flag(str(counts.get("total_files")) == str(len(files) + len(resources))),
        "count_lines_ok": flag(str(counts.get("total_lines")) == str(lines_now)),
        "lines_recomputed": count(lines_now),
        "lock_paths_ok": flag(lock_paths == set(listed_py) | set(listed_res)),
        "lock_records": count(len(lock_paths)),
    }


def judge_ac5_01(facts: Facts) -> Verdict:
    """``AC-5|01``: the manifest is recomputed file by file, and its counts are derived."""
    ok = (
        facts["check_exit"] == "0"
        and facts["sha_mismatch"] == "0"
        and facts["sha_absent"] == "0"
        and facts["unlisted_on_disk"] == "0"
        and facts["stale_entries"] == "0"
        and facts["res_unregistered"] == "0"
        and facts["res_stale"] == "0"
        and facts["lock_paths_ok"] == "yes"
        and facts["count_py_ok"] == "yes"
        and facts["count_res_ok"] == "yes"
        and facts["count_total_ok"] == "yes"
        and facts["count_lines_ok"] == "yes"
    )
    readings = (
        f"清单 {facts['listed_py']} 个 py + {facts['listed_res']} 个资源 = "
        f"{facts['listed_total']} 条，逐条现算 sha256（{facts['sha_checked']} 条能读到磁盘内容）："
        f"对不上 {facts['sha_mismatch']} 条、磁盘上没有 {facts['sha_absent']} 条",
        f"磁盘反查：py {facts['disk_py']} 个（清单没登记的 {facts['unlisted_on_disk']}："
        f"{facts['unlisted_names']}）、非 py 文件 {facts['disk_other']} 个（未登记为资源 "
        f"{facts['res_unregistered']}：{facts['res_unregistered_names']}），清单里过时条目 "
        f"{facts['stale_entries']}：{facts['stale_names']}",
        f"四个计数逐个与现算相等：py_files={facts['count_py_ok']}、resource_files="
        f"{facts['count_res_ok']}、total_files={facts['count_total_ok']}、total_lines="
        f"{facts['count_lines_ok']}（现算行数 {facts['lines_recomputed']}）；"
        f"upstream.lock 与 manifest 的文件集相等 = {facts['lock_paths_ok']}"
        f"（{facts['lock_records']} 条）",
        f"仪器原话（{GEN_MANIFEST_TOOL} --check，exit {facts['check_exit']}）："
        f"{facts['check_note']}",
    )
    reason = (
        ""
        if ok
        else "「逐项校验通过」要的是每一条 sha 都对得上磁盘、磁盘上没有清单未登记的搬运文件、"
        "四个计数都拿现算值复核过 —— 少任何一项，验收依据就退回「写死的数字」"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac5_02(ctx: Context) -> Facts:
    """Ask what the ported tree actually carries and whether the 1A/1B split is readable."""
    files = manifest_entries(ported_manifest(), "files")
    packages = sorted(
        {
            Path(str(entry.get("upstream_path", ""))).parts[1]
            for entry in files
            if len(Path(str(entry.get("upstream_path", ""))).parts) > 2
        }
    )
    exported = ported_reexports()
    called = ported_call_targets()
    missing = sorted(name for name in called if name not in exported)
    scope = ctx.read(REQ_DOC_REL)
    named = first_capture(
        scope, r"搬运范围（D9）\*\*：本迭代搬运 P0/P1 数据域涉及的子模块（([^）]*)）"
    )
    excluded = first_capture(scope, r"无关的子模块（([^）]*)）移出本迭代")
    named_packages = [
        name
        for name in (re.sub(r"\s*等\s*$", "", part).strip() for part in named.split("、"))
        if name and name != "等"
    ]
    excluded_packages = [part.strip() for part in excluded.split("/") if part.strip()]
    named_absent = sorted(set(named_packages) - set(packages))
    excluded_present = sorted(set(excluded_packages) & set(packages))
    leg_modules = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "opendata/data/providers/akshare/models").glob("*.py")
        if path.name != "__init__.py"
    )
    return {
        "package_count": count(len(packages)),
        "package_names": ", ".join(packages),
        "manifest_files": count(len(files)),
        "leg_modules": count(len(leg_modules)),
        "first_party_calls": count(len(called)),
        "has_calls": flag(len(called) > 0),
        "called_missing": count(len(missing)),
        "called_missing_names": ", ".join(missing[:4]) or "-",
        "scope_named": count(len(named_packages)),
        "scope_named_raw": named[:80],
        "scope_missing": count(len(named_absent)),
        "scope_missing_names": ", ".join(named_absent) or "-",
        "excluded_named": count(len(excluded_packages)),
        "excluded_present": count(len(excluded_present)),
        "excluded_present_names": ", ".join(excluded_present) or "-",
        "tier_field": ported_batch_field(),
    }


def judge_ac5_02(facts: Facts) -> Verdict:
    """``AC-5|02``: the ported breadth is used by our legs, and the batch split is readable."""
    ok = (
        facts["has_calls"] == "yes"
        and facts["called_missing"] == "0"
        and facts["excluded_present"] == "0"
        and facts["scope_missing"] == "0"
        and facts["tier_field"] != "-"
    )
    readings = (
        f"搬运清单 {facts['manifest_files']} 个 py 文件，来自上游 {facts['package_count']} 个子包："
        f"{facts['package_names']}",
        f"首方代码以 ``opendata_http.<name>`` 取用 {facts['first_party_calls']} 个端点函数，"
        f"聚合层没导出的 {facts['called_missing']} 个：{facts['called_missing_names']}"
        f"（akshare 腿模块 {facts['leg_modules']} 个）",
        f"需求 D9 点名的范围内 {facts['scope_named']} 个子包（原文：{facts['scope_named_raw']}），"
        f"清单里没有的 {facts['scope_missing']} 个：{facts['scope_missing_names']}；"
        f"点名移出的 {facts['excluded_named']} 个非金融子包混进来的 "
        f"{facts['excluded_present']} 个：{facts['excluded_present_names']}",
        f"能读出 1A/1B 批次的机器字段 = {facts['tier_field']}",
    )
    reason = (
        ""
        if ok
        else "「P0 域子模块搬运完成 / P1 域子模块搬运完成」缺一个机器可读的分母：`domains.yaml` 与 "
        "`Capability` 都不带 batch/priority/tier 字段，需求 D9 与实施计划 B1.1 的子模块清单都写在"
        "「…等」这类散文里，所以「完成」today 无法从树上判。已量的两半是实的：首方代码取用的端点"
        "全部导出、被移出本迭代的非金融子包一个都没混进来；散文点名的 "
        f"{facts['scope_missing_names']} 是否属于 P0/P1 也要同一份字段来定。钉出这个字段是产品/接口"
        "决策（谁声明哪个域属哪一批），本轮不替它编"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac5_03(ctx: Context) -> Facts:
    """Read the report's pending-TODO face, then run the frozen AST scan over it."""
    report = ctx.read(PORT_REPORT_ARCHIVE)
    code, out = run_argv([sys.executable, ZERO_DEP_SCANNER])
    self_code, _ = run_argv([sys.executable, ZERO_DEP_SCANNER, "--self-test"])
    scanner = script_module(ZERO_DEP_SCANNER)
    findings = scanner.collect(scanner.DEFAULT_TARGETS)
    kinds = {
        kind: sum(1 for finding in findings if finding.kind == kind)
        for kind in ("import", "dynamic", "string")
    }
    section = report.split("## 人工待办清单", 1)[1] if "## 人工待办清单" in report else ""
    body = [line.strip() for line in section.splitlines() if line.strip()]
    return {
        "todo_reported": first_capture(report, r"人工待办：\*\*(\d+)\*\*"),
        "todo_section_ok": flag(
            bool(body)
            and body[0].startswith("（无")
            and not any(line.startswith("- [ ]") for line in body)
        ),
        "todo_section_head": body[0][:60] if body else "(空)",
        "detector_exit": str(code),
        "detector_note": (out.strip().splitlines() or ["(no output)"])[-1][:120],
        "self_test_exit": str(self_code),
        "import_live": count(kinds["import"]),
        "dynamic_live": count(kinds["dynamic"]),
        "string_live": count(kinds["string"]),
        "string_files": ", ".join(sorted({f.file for f in findings if f.kind == "string"})) or "-",
        "frozen": count(len(scanner.load_baseline().entries)),
    }


def judge_ac5_03(facts: Facts) -> Verdict:
    """``AC-5|03``: the diff report is out of TODOs and the AST scan sees no upstream reference."""
    ok = (
        facts["todo_reported"] == "0"
        and facts["todo_section_ok"] == "yes"
        and facts["detector_exit"] == "0"
        and facts["self_test_exit"] == "0"
        and facts["import_live"] == "0"
        and facts["dynamic_live"] == "0"
        and facts["string_live"] == "0"
        and facts["frozen"] == "0"
    )
    readings = (
        f"差异报告（{PORT_REPORT_ARCHIVE}）自报人工待办 {facts['todo_reported']} 条，"
        f"清单段落首行「{facts['todo_section_head']}」且没有挂着的复选项 = "
        f"{facts['todo_section_ok']}",
        f"AST 级静态扫描（{ZERO_DEP_SCANNER}，exit {facts['detector_exit']} / "
        f"--self-test exit {facts['self_test_exit']}）：import 形态 {facts['import_live']}、"
        f"动态导入 {facts['dynamic_live']}、字符串常量 {facts['string_live']}",
        f"扫描器冻着的基线 {facts['frozen']} 条，字符串形态落在 {facts['string_files'] or '-'}",
        f"扫描器原话：{facts['detector_note']}",
    )
    reason = (
        ""
        if ok
        else "判据原文把「含字符串常量与动态导入」写进了零引用口径，基线里剩下的就是那 "
        f"{facts['string_live']} 条产品事实的名字面量（{facts['string_files']}）。"
        "抹平它只有两条路，"
        "两条都要改判据本身：①把扫描器字符串规则改窄（等于放宽已勾的 AC-16|05「AST 口径」）；"
        "②改掉这三处真实数据源名的拼写（正是 C35 收口过的门禁空转）。同一道题 AC-16|06 已经"
        "登记为用户/产品决策（task #64），这里不重复改判、也不换个说法再判一次"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac5_04(ctx: Context) -> Facts:
    """Check the old root is gone, then reach the flat API from an interpreter without akshare."""
    root_dir = (REPO_ROOT / "akshare").is_dir()
    code, out = run_argv(["git", "ls-files", "akshare/"])
    tracked = sorted(line for line in out.splitlines() if line)
    spec = importlib.util.find_spec("akshare")
    import_code, import_out = run_argv([sys.executable, "-c", f"import {PORTED_ROOT}"])
    names = ported_call_targets()
    call_code, call_out = run_argv([sys.executable, "-c", PORTED_CALL_SNIPPET, *names])
    absent = sorted(
        line.removeprefix("MISSING:")
        for line in call_out.splitlines()
        if line.startswith("MISSING:")
    )
    archive_rel, archive_dir = newest_round_archive(CLEAN_RUN_BASENAME)
    evidence = ctx.read(archive_rel) if archive_rel != "-" else ""
    declared = first_capture(evidence, r"^ARCHIVE_ROUND=(\S+)$")
    return {
        "root_dir": flag(root_dir),
        "tracked_under_root": count(len(tracked)),
        "tracked_names": ", ".join(tracked[:4]) or "-",
        "spec": "absent" if spec is None else "present",
        "import_exit": str(import_code),
        "import_note": (import_out.strip().splitlines() or ["(no output)"])[-1][:100],
        "call_targets": count(len(names)),
        "call_probe_exit": str(call_code),
        "callable_missing": count(len(absent)),
        "callable_missing_names": ", ".join(absent[:4]) or "-",
        "clean_archive": archive_rel,
        "archive_round": declared or "-",
        "archive_self_written": flag(bool(declared) and declared == archive_dir),
        "archive_absent": flag(
            "akshare: absent" in evidence and "akshare: present" not in evidence
        ),
    }


def judge_ac5_04(facts: Facts) -> Verdict:
    """``AC-5|04``: no ``akshare`` at the root or in the interpreter, and the legs still resolve."""
    ok = (
        facts["root_dir"] == "no"
        and facts["tracked_under_root"] == "0"
        and facts["spec"] == "absent"
        and facts["import_exit"] == "0"
        and number(facts["call_targets"]) > 0
        and facts["callable_missing"] == "0"
        and facts["archive_absent"] == "yes"
    )
    readings = (
        f"根目录 `akshare/` 在磁盘上 = {facts['root_dir']}，git 索引里该前缀下 "
        f"{facts['tracked_under_root']} 个文件：{facts['tracked_names']}",
        f"本解释器 `importlib.util.find_spec('akshare')` = {facts['spec']}；"
        f"`import {PORTED_ROOT}` exit={facts['import_exit']}（{facts['import_note']}）",
        f"首方代码取用的 {facts['call_targets']} 个端点函数逐个 `callable(getattr(...))`，"
        f"取不到的 {facts['callable_missing']} 个：{facts['callable_missing_names']}"
        f"（探针 exit {facts['call_probe_exit']}）",
        f"干净 venv 侧证（{facts['clean_archive']}，ARCHIVE_ROUND={facts['archive_round']}，"
        f"本轮自写 = {facts['archive_self_written']}）里 akshare 报 absent = "
        f"{facts['archive_absent']}",
    )
    reason = (
        ""
        if ok
        else "「根目录 akshare/ 已删除；未安装 akshare 的环境中 P0 域函数可用」"
        "要的是这两句同时成立："
        "根目录既不在磁盘也不在索引里、解释器也没有这个包，而搬运层的平面对外接口还能被腿解析到"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac5_05(ctx: Context) -> Facts:
    """Read the two banner lines of every manifest-listed ported file and pin them to the lock."""
    payload = ported_manifest()
    files = manifest_entries(payload, "files")
    resources = manifest_entries(payload, "resources")
    upstream = manifest_mapping(payload, "upstream")
    base = REPO_ROOT / PORTED_ROOT
    mit_missing: list[str] = []
    source_missing: list[str] = []
    url_missing: list[str] = []
    commits: set[str] = set()
    walked = 0
    for entry in files:
        path = base / entry_path(entry)
        if not path.is_file():
            mit_missing.append(f"{entry_path(entry)}(absent)")
            source_missing.append(f"{entry_path(entry)}(absent)")
            continue
        head = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[:6])
        walked += 1
        if "MIT License" not in head:
            mit_missing.append(entry_path(entry))
        found = re.search(r"# Ported from (\S+) \((\S+)\) @ ([0-9a-f]{40})", head)
        if not found:
            source_missing.append(entry_path(entry))
            continue
        commits.add(found.group(3))
        if found.group(2) != str(upstream.get("url")):
            url_missing.append(entry_path(entry))
    notices = ctx.read("THIRD_PARTY_NOTICES.md")
    return {
        "listed_py": count(len(files)),
        "walked": count(walked),
        "walk_ok": flag(walked == len(files) and len(files) > 0),
        "mit_missing": count(len(mit_missing)),
        "mit_missing_names": ", ".join(mit_missing[:4]) or "-",
        "source_missing": count(len(source_missing)),
        "source_missing_names": ", ".join(source_missing[:4]) or "-",
        "banner_commits": count(len(commits)),
        "lock_commit": str(upstream.get("commit", "(absent)")),
        "commit_ok": flag(
            len(commits) == 1 and next(iter(commits), "") == str(upstream.get("commit"))
        ),
        "url_missing": count(len(url_missing)),
        "url_missing_names": ", ".join(url_missing[:4]) or "-",
        "lock_url": str(upstream.get("url", "(absent)")),
        "notices_ok": flag("akshare" in notices and "MIT" in notices.upper()),
        "resource_count": count(len(resources)),
    }


def judge_ac5_05(facts: Facts) -> Verdict:
    """``AC-5|05``: every ported file keeps the MIT notice and a source line pinned to the lock."""
    ok = (
        facts["walk_ok"] == "yes"
        and facts["mit_missing"] == "0"
        and facts["source_missing"] == "0"
        and facts["url_missing"] == "0"
        and facts["banner_commits"] == "1"
        and facts["commit_ok"] == "yes"
        and facts["notices_ok"] == "yes"
    )
    readings = (
        f"清单里 {facts['listed_py']} 个 py 文件逐个读前 6 行（走查 {facts['walked']} 个，"
        f"与清单相等 = {facts['walk_ok']}）：缺 MIT 声明 {facts['mit_missing']} 个"
        f"（{facts['mit_missing_names']}）、缺来源标注 {facts['source_missing']} 个"
        f"（{facts['source_missing_names']}）",
        f"来源标注里的 commit 只有 {facts['banner_commits']} 个取值，与 manifest/upstream.lock 的 "
        f"{facts['lock_commit'][:12]} 相等 = {facts['commit_ok']}；URL 与 lock_url "
        f"（{facts['lock_url']}）逐字不等的 {facts['url_missing']} 个："
        f"{facts['url_missing_names']}",
        f"许可证台账（THIRD_PARTY_NOTICES.md）同时说得到 akshare 与 MIT = {facts['notices_ok']}",
        f"清单里 {facts['resource_count']} 个资源文件（json/js）不带这类头部 —— 判据指的是搬运的"
        "源文件，资源由 manifest.json 与许可证台账各自登记，本轮只披露不判定",
    )
    reason = (
        ""
        if ok
        else "「每文件头保留 MIT 版权声明 + 来源标注」要的是搬运树里每一个 py 文件都带上这两行，"
        "并且来源钉的是 upstream.lock 里那一个 commit —— 多个 commit 就说明有文件是另一次同步的残留"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def _datasets_accessor_outcome(module: ModuleType, name: str) -> str:
    """Which branch one resource accessor actually takes when it is called.

    ``datasets.py`` says an unavailable built-in in the message it raises, so the exception is
    the reading rather than a failure to handle; anything else comes back as its class name.
    """
    try:
        getattr(module, name)()
    except Exception as exc:
        return "marked" if "unavailable" in str(exc) else type(exc).__name__
    return "runnable"


def measure_ac5_06(ctx: Context) -> Facts:
    """Run the resource accessors here, then ask whether the register actually lists them."""
    path = REPO_ROOT / DATASETS_REL
    if not path.is_file():
        raise ProbeError(f"{DATASETS_REL} is missing")
    source = path.read_text(encoding="utf-8", errors="replace")
    parsed = ast.parse(source)
    modules = [
        node
        for node in parsed.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    ]
    names = [node.name for node in modules]
    spec = importlib.util.spec_from_file_location("ported_datasets_probe", path)
    if spec is None or spec.loader is None:
        raise ProbeError(f"{DATASETS_REL} cannot be loaded for a call test")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    outcomes = [(node.name, _datasets_accessor_outcome(module, node.name)) for node in modules]
    runnable = [name for name, kind in outcomes if kind == "runnable"]
    marked = [name for name, kind in outcomes if kind == "marked"]
    other = [f"{name}:{kind}" for name, kind in outcomes if kind not in ("runnable", "marked")]
    points = [
        name
        for name in marked
        if PORT_REPORT_ARCHIVE in ast.unparse(next(node for node in modules if node.name == name))
    ]
    report = ctx.read(PORT_REPORT_ARCHIVE)
    section = (
        report.split(RESOURCE_REGISTER_MARKER, 1)[1] if RESOURCE_REGISTER_MARKER in report else ""
    )
    # 首列是函数名的行才是数据行：表头首列是中文、分隔行是连字符。按首列形状认行，本判据就
    # 不依赖某个具体的 Markdown 写法 —— 也才看得见「同形」这件事（见 shape_clash 读数）。
    row_names = [
        cell
        for cell in (
            line.split("|")[1].strip()
            for line in section.splitlines()
            if line.startswith("| ") and len(line.split("|")) > 1
        )
        if re.fullmatch(r"[A-Za-z_]\w*", cell)
    ]
    clash = sum(1 for line in section.splitlines() if line.startswith(REPLAY_ROW_PREFIX))
    base = REPO_ROOT / PORTED_ROOT
    resources = manifest_entries(ported_manifest(), "resources")
    present = sum(1 for entry in resources if (base / entry_path(entry)).is_file())
    return {
        "functions": count(len(names)),
        "function_names": ", ".join(names) or "-",
        "runnable": count(len(runnable)),
        "runnable_names": ", ".join(runnable) or "-",
        "marked": count(len(marked)),
        "marked_names": ", ".join(marked) or "-",
        "raised_other": count(len(other)),
        "raised_other_names": ", ".join(other) or "-",
        "points_at_register": flag(len(points) == len(marked)),
        "points_names": ", ".join(points) or "-",
        "register_heading": flag(bool(section)),
        "register_rows": count(len(row_names)),
        "register_row_names": ", ".join(row_names) or "-",
        "register_lists_marked": flag(sorted(row_names) == sorted(marked) and bool(marked)),
        "shape_clash": count(clash),
        "resource_listed": count(len(resources)),
        "resource_present": count(present),
        "resources_present_ok": flag(present == len(resources) and len(resources) > 0),
    }


def judge_ac5_06(facts: Facts) -> Verdict:
    """``AC-5|06``: the accessors either resolve or are marked unavailable *and* registered."""
    resolves = facts["runnable"] == facts["functions"] and number(facts["functions"]) > 0
    registered = (
        number(facts["functions"]) > 0
        and facts["raised_other"] == "0"
        and facts["marked"] == facts["functions"]
        and facts["points_at_register"] == "yes"
        and facts["register_heading"] == "yes"
        and facts["register_lists_marked"] == "yes"
    )
    ok = (
        facts["resources_present_ok"] == "yes"
        and facts["shape_clash"] == "0"
        and (resolves or registered)
    )
    readings = (
        f"{DATASETS_REL} 里 {facts['functions']} 个资源访问函数（{facts['function_names']}）"
        f"当场调用：返回路径的 {facts['runnable']} 个（{facts['runnable_names']}）、"
        f"raise 里明说不可用的 {facts['marked']} 个（{facts['marked_names']}）、"
        f"报别的异常的 {facts['raised_other']} 个：{facts['raised_other_names']}",
        f"标注不可用的 {facts['marked']} 个函数的 raise 文本都回指登记文件 {PORT_REPORT_ARCHIVE} = "
        f"{facts['points_at_register']}（{facts['points_names']}）；登记段落在位 = "
        f"{facts['register_heading']}，表体 {facts['register_rows']} 行"
        f"（{facts['register_row_names']}）、逐个点名这些函数且不多不少 = "
        f"{facts['register_lists_marked']}",
        f"登记表的行与重放表行同形（行首为 {REPLAY_ROW_PREFIX!r}）的 {facts['shape_clash']} 行 —— "
        f"不为 0 时 {PORT_REPORT_ARCHIVE} 的「每文件一行」计数会把登记行算进搬运清单，"
        f"于是 {PORT_REPORT_TOOL} 自己生成的这一节判倒 AC-17|05",
        f"登记处由报告仪器（{PORT_REPORT_TOOL}）从 `datasets.py` 的 raise 语句与 upstream.lock "
        "派生，不是手写段落",
        f"清单登记的 {facts['resource_listed']} 个内置资源在磁盘齐备 = "
        f"{facts['resources_present_ok']}（{facts['resource_present']}/{facts['resource_listed']}）",
    )
    reason = (
        ""
        if ok
        else "判据给了两条支路：函数可运行，或「明确标注不可用并登记」。调用实测是 raise，"
        "那就只看第二条 —— 标注在函数体里，登记必须有地方接住它（`datasets.py` 的 raise 文本"
        f"回指 {PORT_REPORT_ARCHIVE}），没有登记段落就是只标注、没登记；"
        f"登记段的形状也不能与同一份报告里的重放表同形，否则这一节的行数会顶掉 AC-17|05 "
        "对搬运清单行数的等式"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def bandit_triaged_rules(ctx: Context) -> frozenset[str]:
    """The rule ids the A2 manual triage disposes of, read from its summary table."""
    return frozenset(re.findall(r"^\|\s*(B\d{3})", ctx.read(A2_TRIAGE_REL), re.MULTILINE))


def measure_ac5_07(ctx: Context) -> Facts:
    """Compare the ported security scan on record against the tree as it stands now."""
    recipes = make_recipes(ctx.read("Makefile"))
    # make 的 recipe 里 `@#` 与 `#` 都是注释；把它们算进命令会让 target_ok 被注释养活，
    # 也会让读数只打印注释、看不见真正执行的命令
    ported_recipe = " ".join(
        line
        for line in recipes.get(SECURITY_PORTED_TARGET, [])
        if not line.lstrip("@").startswith("#")
    )
    listed_py = {entry_path(entry) for entry in manifest_entries(ported_manifest(), "files")}
    ported_now = len(py_files_under(PORTED_ROOT))
    exclude_dirs = [
        str(item)
        for item in ((yaml.safe_load(ctx.read(BANDIT_CONFIG)) or {}).get("exclude_dirs") or [])
    ]
    archive_rel, archive_dir = newest_round_archive(PORTED_BANDIT_BASENAME)
    findings: list[dict[str, object]] = []
    declared, produced_by, generated_at = "", "", ""
    if archive_rel != "-":
        payload = json.loads(ctx.read(archive_rel))
        if isinstance(payload, dict):
            declared = str(payload.get("archive_round", ""))
            produced_by = str(payload.get("produced_by", ""))
            generated_at = str(payload.get("generated_at", ""))
            scan = payload.get("scan") if isinstance(payload.get("scan"), dict) else payload
            raw = scan.get("results") if isinstance(scan, dict) else None
            findings = [item for item in (raw or []) if isinstance(item, dict)]
    rules = sorted({str(item.get("test_id")) for item in findings})
    triaged = bandit_triaged_rules(ctx)
    untriaged = [item for item in findings if str(item.get("test_id")) not in triaged]
    scanned = sorted(
        {str(item.get("filename", "")).split(f"{PORTED_ROOT}/")[-1] for item in findings}
    )
    stray = sorted(name for name in scanned if name not in listed_py)
    triage = ctx.read(A2_TRIAGE_REL)
    absolution = first_capture(triage, r"^(无 B\d{3}[^。\n]*)。")
    cleared = sorted(set(re.findall(r"B\d{3}", absolution)))
    contradicted = sorted(rule for rule in cleared if rule in set(rules))
    deser = sorted(
        {
            f"{item.get('test_id')}({item.get('test_name')})"
            for item in findings
            if any(word in str(item.get("test_name", "")).lower() for word in ("pickle", "yaml"))
        }
    )
    claimed_files = first_capture(triage, r"扫描对象：`" + PORTED_ROOT + r"/`（(\d+) 个 py 文件")
    return {
        "target_ok": flag(bool(ported_recipe) and PORTED_ROOT in ported_recipe),
        "target_recipe": ported_recipe[:140] or "-",
        "daily_excludes_ported": flag(PORTED_ROOT in exclude_dirs),
        "bandit_exclude_dirs": ", ".join(exclude_dirs) or "-",
        "ported_files": count(ported_now),
        "archive": archive_rel,
        "archive_round": declared or "-",
        "archive_produced_by": produced_by[:120] or "-",
        "archive_generated_at": generated_at or "-",
        "archive_self_written": flag(bool(declared) and declared == archive_dir),
        "scan_findings": count(len(findings)),
        "scan_files": count(len(scanned)),
        "scan_rules": count(len(rules)),
        "scan_rule_names": ", ".join(rules) or "-",
        "scan_b_only": flag(bool(rules) and all(rule.startswith("B") for rule in rules)),
        "scan_manifest_ok": flag(not stray and bool(findings)),
        "scan_stray_names": ", ".join(stray[:4]) or "-",
        "triage_rules": count(len(triaged)),
        "findings_untriaged": count(len(untriaged)),
        "untriaged_detail": ", ".join(
            f"{item.get('test_id')}@{str(item.get('filename', '')).split(f'{PORTED_ROOT}/')[-1]}"
            f":{item.get('line_number')}"
            for item in untriaged[:6]
        )
        or "-",
        "triage_files_claimed": claimed_files,
        "triage_findings_claimed": first_capture(triage, r"bandit-ported\.json`（(\d+) 项）"),
        "triage_absolution": absolution[:80],
        "cleared_rules": ", ".join(cleared) or "-",
        "cleared_present": count(len(contradicted)),
        "cleared_present_names": ", ".join(contradicted) or "-",
        "deser_present": count(len(deser)),
        "deser_names": ", ".join(deser) or "-",
        "surface_covered": flag(claimed_files == count(ported_now)),
    }


def judge_ac5_07(facts: Facts) -> Verdict:
    """``AC-5|07``: the ported tree's bandit run is on record and the triage covers all of it."""
    ok = (
        facts["target_ok"] == "yes"
        and facts["archive"] != "-"
        and facts["archive_self_written"] == "yes"
        and facts["scan_manifest_ok"] == "yes"
        and facts["scan_b_only"] == "yes"
        and number(facts["scan_findings"]) > 0
        and facts["findings_untriaged"] == "0"
        and facts["cleared_present"] == "0"
        and facts["deser_present"] == "0"
        and facts["surface_covered"] == "yes"
    )
    readings = (
        f"搬运层专用扫描目标 `Makefile:{SECURITY_PORTED_TARGET}` 在位且打的是 {PORTED_ROOT}/ = "
        f"{facts['target_ok']}（recipe：{facts['target_recipe']}）；日常 `make security` 按 "
        f"{BANDIT_CONFIG} 的 exclude_dirs（{facts['bandit_exclude_dirs']}）把搬运层排出去，"
        f"所以「含 B 层」的全量复核只能由这个专用目标承担 = {facts['daily_excludes_ported']}"
        "（质量规范 §4 的「每次同步一次」口径，不是逐提交记账）",
        f"本轮全量扫描留档 {facts['archive']}（自报 archive_round={facts['archive_round']}，"
        f"本轮自写 = {facts['archive_self_written']}，"
        f"generated_at={facts['archive_generated_at']}，"
        f"命令={facts['archive_produced_by']}）：{facts['scan_findings']} 项 / "
        f"{facts['scan_files']} 个有 finding 的文件 / {facts['scan_rules']} 条规则"
        f"（{facts['scan_rule_names']}），全是 B 层 = {facts['scan_b_only']}；"
        f"文件全在搬运清单内 = {facts['scan_manifest_ok']}（清单外：{facts['scan_stray_names']}）",
        f"人工 triage（{A2_TRIAGE_REL}）处置 {facts['triage_rules']} 条规则，自己声明扫的是 "
        f"{facts['triage_files_claimed']} 个 py 文件 / {facts['triage_findings_claimed']} 项，"
        f"而搬运树今天是 {facts['ported_files']} 个 py 文件 —— "
        f"覆盖面相等 = {facts['surface_covered']}",
        f"同一份 triage 的免罪句「{facts['triage_absolution']}」点名的规则有 "
        f"{facts['cleared_present']} 条在本轮实测里出现了：{facts['cleared_present_names']}；"
        f"它说「无 pickle/yaml.load 类反序列化项」，本轮按 bandit 自己的测试名匹配到 "
        f"{facts['deser_present']} 条：{facts['deser_names']}",
        f"今天这次扫描里落在未处置规则上的还有 {facts['findings_untriaged']} 项："
        f"{facts['untriaged_detail']}",
    )
    reason = (
        ""
        if ok
        else "「安全扫描完成并人工 triage 留档」缺的是留档与今天的树对得上："
        "A2 那份 triage 声明它扫了 "
        f"{facts['triage_files_claimed']} 个 py 文件（{facts['triage_findings_claimed']} 项），"
        f"搬运树已长到 {facts['ported_files']} 个（本轮全量 {facts['scan_findings']} 项、"
        f"{facts['scan_rules']} 条规则），其中 {facts['findings_untriaged']} 项属于 triage "
        f"从未处置过的规则（{facts['untriaged_detail']}）；那句「"
        f"{facts['triage_absolution']}」描述的是当初那棵树，"
        "本轮实测里 triage 点名「无」的规则出现了 "
        f"{facts['cleared_present']} 条（{facts['cleared_present_names']}）、"
        "pickle/yaml 类反序列化 "
        f"{facts['deser_present']} 条（{facts['deser_names']}）。逐条人工处置是这件事的正当收口，"
        "本轮不给它补假处置"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


# --------------------------------------------------------------------------- #
# AC-16 licensing and permission-boundary probes (C62)
# --------------------------------------------------------------------------- #

#: Where the self-developed provider packages live; every one of them ships a registration module.
SELFDEV_PROVIDER_ROOT: Final = "opendata/data/providers"

#: The document that spells out what a clean-room record has to look like.
CLEAN_ROOM_DOC_REL: Final = "docs/proposals/openbb-migration/README.md"
CLEAN_ROOM_PHRASE: Final = "无 OpenBB 源码参照"
CODE_CLEAN_ROOM_PHRASE: Final = "no OpenBB code was consulted"

#: First-party roots under the BSL licence -- what the MIT subtree may not import.
BSL_IMPORT_ROOTS: Final = ("opendata", "opendata_fuyao", "opendata_client")

#: The four shipped packages -- the only surface 「全库无 OpenBB 源码」 can be measured on.
RUNTIME_PY_ROOTS: Final = ("opendata", "opendata_http", "opendata_fuyao", "opendata_client")

#: Entry points the MIT subtree has to reach on its own.
STANDALONE_MODULES: Final = (
    "opendata_http.datasets",
    "opendata_http.stock.cons",
    "opendata_http.utils",
)

#: If importing this raises nothing the standalone run proved nothing.
STANDALONE_CONTROL_MODULE: Final = "opendata.core.database"

#: Run in its own interpreter: the BSL roots are made unimportable, then the MIT subtree is
#: imported for real. Written as one string so the probe adds no second ``subprocess`` site.
STANDALONE_PROBE_CODE: Final = """
import importlib, sys
BLOCKED = ("opendata", "opendata_fuyao", "opendata_client")
class _BslBlocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("blocked BSL root: " + name)
        return None
sys.meta_path.insert(0, _BslBlocker())
control = "not-blocked"
try:
    importlib.import_module("opendata.core.database")
except ImportError:
    control = "blocked"
MODS = ("opendata_http.datasets", "opendata_http.stock.cons", "opendata_http.utils")
try:
    for name in MODS:
        importlib.import_module(name)
except BaseException as exc:
    print("CONTROL=" + control)
    print("FAILED=" + type(exc).__name__ + ": " + str(exc)[:120])
    raise SystemExit(1)
print("CONTROL=" + control)
print("IMPORTED=" + str(len(MODS)))
""".strip()


@lru_cache(maxsize=4096)  # one entry per shipped file, which is fewer
def import_roots(rel: str) -> frozenset[str] | None:
    """Top-level import roots one tracked source file pulls in, or ``None`` when unparsable.

    Cached because the counterfact run measures the same tree several times in one process, and
    a re-parse of the shipped packages is not a new measurement.
    """
    try:
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"), filename=rel)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return frozenset(found)


def provider_packages(ctx: Context) -> list[str]:
    """The self-developed provider packages: tracked dirs shipping a registration module."""
    prefix = SELFDEV_PROVIDER_ROOT + "/"
    names: set[str] = set()
    for rel in ctx.tracked():
        if rel.startswith(prefix) and rel.endswith("/registration.py"):
            names.add(rel[len(prefix) :].split("/")[0])
    return sorted(names)


def runtime_py_files(ctx: Context) -> list[str]:
    """Tracked ``.py`` paths under the four shipped roots."""
    return sorted(
        rel
        for rel in ctx.tracked()
        if rel.endswith(".py") and rel.split("/")[0] in RUNTIME_PY_ROOTS
    )


@lru_cache(maxsize=1)
def openbb_import_map(runtime: tuple[str, ...]) -> tuple[tuple[str, ...], int, int]:
    """``(files importing openbb or openbb-*, files parsed, distinct import roots)``.

    The third reading is the positive control: a walker that finds no import at all would also
    find no ``openbb``, and that zero would mean nothing. Cached on the file set, so the
    counterfact run measures the shipped tree once per process.
    """
    hits: list[str] = []
    parsed = 0
    roots: set[str] = set()
    for rel in runtime:
        found = import_roots(rel)
        if found is None:
            continue
        parsed += 1
        roots.update(found)
        if any(top == "openbb" or top.startswith("openbb-") for top in found):
            hits.append(rel)
    return tuple(hits), parsed, len(roots)


@lru_cache(maxsize=16)  # one entry per provider package
def commit_declares(name: str) -> bool:
    """Whether one provider's history carries the declared phrase (one git walk per package)."""
    code, out = run_argv(["git", "log", "--format=%s%n%b", "--", f"{SELFDEV_PROVIDER_ROOT}/{name}"])
    return code == 0 and CLEAN_ROOM_PHRASE in out


def clean_room_commits(names: Sequence[str]) -> list[str]:
    """Which provider packages have a commit message carrying the declared phrase."""
    return [name for name in names if commit_declares(name)]


def clean_room_in_code(names: Sequence[str]) -> list[str]:
    """Which provider packages state the same thing inside their own module docstrings."""
    hits = grep_files(
        py_files_under(SELFDEV_PROVIDER_ROOT),
        re.compile(re.escape(CODE_CLEAN_ROOM_PHRASE), re.IGNORECASE),
    )
    covered = set(names)
    return sorted({rel.split("/")[3] for rel in hits if rel.split("/")[3] in covered})


@lru_cache(maxsize=1)
def record_bodies(tracked: tuple[str, ...]) -> tuple[str, ...]:
    """Texts under ``docs/evidence/`` that state both halves of the clean-room criterion.

    The requirement documents restate the rule for every provider at once, so a restatement
    there cannot count as a per-provider record; evidence files are where a record lives.
    """
    bodies: list[str] = []
    for rel in tracked:
        if not rel.startswith("docs/evidence/"):
            continue
        try:
            body = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if CLEAN_ROOM_PHRASE in body and "审查" in body:
            bodies.append(body)
    return tuple(bodies)


@lru_cache(maxsize=1)
def openbb_named_in(runtime: tuple[str, ...]) -> tuple[str, ...]:
    """Runtime files whose text says ``openbb`` at all, in any casing."""
    return tuple(
        rel
        for rel in runtime
        if re.search(
            "openbb",
            (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace"),
            re.IGNORECASE,
        )
    )


def clean_room_records(ctx: Context, names: Sequence[str]) -> list[str]:
    """Which providers a留档 file actually names."""
    bodies = record_bodies(tuple(ctx.tracked()))
    named: set[str] = set()
    for body in bodies:
        named.update(name for name in names if re.search(rf"\b{re.escape(name)}\b", body))
    return sorted(named)


def stale_ledger_paths(ctx: Context) -> list[str]:
    """Paths the licensing ledger points at that do not exist on disk.

    ``THIRD_PARTY_NOTICES.md`` is the document a lawyer reads first; a pointer to a package that
    was never created makes the rest of it unverifiable, so it is measured rather than assumed.
    A citation stops at ``::``, so ``file.py::SYMBOL`` is checked as the file it names.
    """
    text = ctx.read(NOTICES_DOC)
    cited = set(
        re.findall(
            r"`((?:opendata|scripts|docs|tests|frontend|alembic)[A-Za-z0-9_]*/[A-Za-z0-9_./-]+)",
            text,
        )
    )
    tracked = set(ctx.tracked())
    return sorted(rel for rel in cited if not (REPO_ROOT / rel).exists() and rel not in tracked)


def measure_ac16_01(ctx: Context) -> Facts:
    """Read the rule, then ask how many providers have the record the rule demands."""
    names = provider_packages(ctx)
    declared = clean_room_commits(names)
    records = clean_room_records(ctx, names)
    doc = ctx.read(CLEAN_ROOM_DOC_REL)
    return {
        "provider_pkgs": count(len(names)),
        "provider_list": ", ".join(names) or "-",
        "rule_written": flag(CLEAN_ROOM_PHRASE in doc and "留痕要求" in doc),
        "rule_extends_ast": flag("零 openbb" in doc and "AST 断言" in doc),
        "declared_pkgs": count(len(declared)),
        "declared_list": ", ".join(declared) or "-",
        "recorded_pkgs": count(len(records)),
        "recorded_list": ", ".join(records) or "-",
        "code_declared_pkgs": count(len(clean_room_in_code(names))),
        "undeclared_list": ", ".join(n for n in names if n not in declared) or "-",
    }


def judge_ac16_01(facts: Facts) -> Verdict:
    """``AC-16|01``: every self-developed provider has a clean-room record, not just a rule."""
    want = number(facts["provider_pkgs"])
    ok = (
        want > 0
        and facts["rule_written"] == "yes"
        and facts["rule_extends_ast"] == "yes"
        and number(facts["declared_pkgs"]) == want
        and number(facts["recorded_pkgs"]) == want
    )
    readings = (
        f"自研 provider 包 {facts['provider_pkgs']} 个（git 跟踪清单里带 registration.py 的"
        f"目录数）：{facts['provider_list']}",
        f"合规规则写在 {CLEAN_ROOM_DOC_REL}：留痕要求里点着「{CLEAN_ROOM_PHRASE}」= "
        f"{facts['rule_written']}；同文档 §6 把 AST 断言从「零 akshare」扩展到「零 openbb」= "
        f"{facts['rule_extends_ast']}",
        f"提交说明带这句声明的包 {facts['declared_pkgs']} 个（{facts['declared_list']}），"
        f"没带的 {facts['undeclared_list']}",
        f"留档（docs/evidence/ 里同时写得到「{CLEAN_ROOM_PHRASE}」与「审查」并按名字"
        f"点名该包的文件）覆盖 {facts['recorded_pkgs']} 个包：{facts['recorded_list']}",
        f"另有 {facts['code_declared_pkgs']} 个包把「{CODE_CLEAN_ROOM_PHRASE}」写进了自己模块的"
        " docstring —— 那是代码里的自声明，不是逐包审查记录，本条按字面不认",
    )
    reason = (
        ""
        if ok
        else "「审查留档」这一面从没成文：规则写在合规文档里（提交说明声明 + 对照表记"
        "上游事实来源），"
        f"但 {facts['provider_pkgs']} 个自研 provider 里提交说明带「{CLEAN_ROOM_PHRASE}」的只有 "
        f"{facts['declared_pkgs']} 个（缺：{facts['undeclared_list']}），docs/evidence/ 下"
        f"逐包点名且有"
        f"「审查」字样的留档覆盖 {facts['recorded_pkgs']} 个。模块 docstring 里的自声明（"
        f"{facts['code_declared_pkgs']} 个包）与 THIRD_PARTY_NOTICES.md 里那句「本仓库不含"
        f"任何 OpenBB "
        "源码或其近似复制」都是**一次性全库断言**，不是「全部 provider 均有」的逐包记录。"
        "历史提交说明补不回来（不改写历史），要收口得由一次真实的人工审查按包留档，本轮不代拟"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac16_02(ctx: Context) -> Facts:
    """Walk the shipped packages' import graph, then check the two halves the item names."""
    runtime = tuple(runtime_py_files(ctx))
    hits, parsed, roots = openbb_import_map(runtime)
    names = provider_packages(ctx)
    declared = clean_room_commits(names)
    named_in_runtime = openbb_named_in(runtime)
    scanner_src = (REPO_ROOT / ZERO_DEP_SCANNER).read_text(encoding="utf-8", errors="replace")
    forbidden = literal_str_tuple(parse(ZERO_DEP_SCANNER), "FORBIDDEN_ROOTS")
    return {
        "runtime_py": count(parsed),
        "runtime_py_files": count(len(runtime)),
        "import_roots_seen": count(roots),
        "openbb_imports": count(len(hits)),
        "openbb_import_names": ", ".join(hits[:4]) or "-",
        "forbidden_covers_openbb": flag("openbb" in forbidden),
        "forbidden_roots": ", ".join(forbidden) or "-",
        "scanner_self_test_lines": count(len(re.findall(r"openbb", scanner_src))),
        "code_openbb_names": count(len(named_in_runtime)),
        "code_openbb_list": ", ".join(named_in_runtime[:4]) or "-",
        "declared_pkgs": count(len(declared)),
        "provider_pkgs": count(len(names)),
        "recorded_pkgs": count(len(clean_room_records(ctx, names))),
    }


def judge_ac16_02(facts: Facts) -> Verdict:
    """``AC-16|02``: no OpenBB source in the shipped code, with both named checks on record."""
    walked = number(facts["runtime_py"])
    tracked = number(facts["runtime_py_files"])
    ok = (
        walked > 0
        and walked == tracked
        and number(facts["import_roots_seen"]) > 0
        and facts["openbb_imports"] == "0"
        and facts["forbidden_covers_openbb"] == "yes"
        and number(facts["declared_pkgs"]) == number(facts["provider_pkgs"])
        and number(facts["recorded_pkgs"]) == number(facts["provider_pkgs"])
    )
    readings = (
        f"运行时四包（{', '.join(RUNTIME_PY_ROOTS)}）跟踪的 py 文件 "
        f"{facts['runtime_py_files']} 个，"
        f"逐个 AST 解析成功 {facts['runtime_py']} 个，共读到 {facts['import_roots_seen']} 个不同的"
        f"顶层 import 根 —— 走查不是空转（正向对照）；其中 import 根为 openbb / openbb-* 的 "
        f"{facts['openbb_imports']} 个：{facts['openbb_import_names']}",
        f"零依赖扫描器 {ZERO_DEP_SCANNER} 的 FORBIDDEN_ROOTS = （{facts['forbidden_roots']}），"
        f"含 openbb = {facts['forbidden_covers_openbb']}；该文件正文里 openbb 出现 "
        f"{facts['scanner_self_test_lines']} 处（含 --self-test 的故意违规样本，即这条断言"
        "自己是会响的）",
        f"运行时代码里字面出现 openbb（大小写不敏感）的文件 {facts['code_openbb_names']} 个："
        f"{facts['code_openbb_list']} —— 前者是 FR-7 对照表加载器（接口命名属事实性信息），"
        "后者是 provider 模块 docstring 里的 clean-room 自声明，都不是源码",
        f"判据括号里的两项核查：提交说明带「{CLEAN_ROOM_PHRASE}」的 provider 包 "
        f"{facts['declared_pkgs']}/{facts['provider_pkgs']} 个；逐包人工抽查留档覆盖 "
        f"{facts['recorded_pkgs']}/{facts['provider_pkgs']} 个",
    )
    if ok:
        reason = ""
    elif walked != tracked:
        reason = (
            f"AST 走查只成功解析 {walked}/{tracked} 个运行时文件，剩下的文件被静默跳过，"
            "「0 个 openbb import 根」就不是全库读数，不能当否定证据；先让尺子覆盖完整"
            "再谈这条"
        )
    else:
        reason = (
            "「全库无 OpenBB 源码或其近似复制」的否定面成立（AST 走查 0 个 openbb import 根，"
            "扫描器禁止清单含 openbb 且自带会响的违规样本），但判据括号里点名的两项核查都没做过："
            f"提交声明核查 {facts['declared_pkgs']}/{facts['provider_pkgs']}，人工抽查留档 "
            f"{facts['recorded_pkgs']}/{facts['provider_pkgs']}。「近似复制」只能靠逐包人工比对判定，"
            "本轮不以「import 为零」冒充它已被抽查过"
        )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac16_03(ctx: Context) -> Facts:
    """Reuse the per-file banner walk, then read the licensing ledger's own citations."""
    facts = dict(measure_ac5_05(ctx))
    notices = ctx.read(NOTICES_DOC)
    stale = stale_ledger_paths(ctx)
    commit = str(facts["lock_commit"])
    return {
        **facts,
        "notices_repo": flag("github.com" in notices),
        "notices_commit": flag(len(commit) == 40 and commit in notices),
        "notices_mit": flag("MIT" in notices.upper()),
        "stale_paths": count(len(stale)),
        "stale_path_names": ", ".join(stale[:4]) or "-",
    }


def judge_ac16_03(facts: Facts) -> Verdict:
    """``AC-16|03``: banners on every ported file, and a ledger that points at real things."""
    ok = (
        facts["walk_ok"] == "yes"
        and facts["mit_missing"] == "0"
        and facts["source_missing"] == "0"
        and facts["url_missing"] == "0"
        and facts["banner_commits"] == "1"
        and facts["commit_ok"] == "yes"
        and facts["notices_repo"] == "yes"
        and facts["notices_commit"] == "yes"
        and facts["notices_mit"] == "yes"
        and facts["stale_paths"] == "0"
    )
    readings = (
        f"搬运树 {facts['listed_py']} 个清单内 py 文件逐个读前 6 行（走查 {facts['walked']} 个 = "
        f"{facts['walk_ok']}）：缺 MIT 版权声明 {facts['mit_missing']} 个"
        f"（{facts['mit_missing_names']}），缺来源标注 {facts['source_missing']} 个"
        f"（{facts['source_missing_names']}）",
        f"来源标注里的 commit 只有 {facts['banner_commits']} 个取值、与 lock 的 "
        f"{facts['lock_commit'][:12]} 相等 = {facts['commit_ok']}；URL 与 lock 逐字不等的 "
        f"{facts['url_missing']} 个（{facts['url_missing_names']}）",
        f"{NOTICES_DOC} 写得到来源仓库（github.com 链接）= {facts['notices_repo']}、"
        f"写得到 lock 那个完整 40 位 commit = {facts['notices_commit']}、"
        f"写得到 MIT = {facts['notices_mit']}",
        f"同一份台账里以反引号点名的仓库内路径，磁盘/跟踪清单上不存在的 {facts['stale_paths']} 个："
        f"{facts['stale_path_names']}",
    )
    reason = (
        ""
        if ok
        else "「MIT 声明完整 + 台账（来源仓库 + commit）完整」要求台账点名的路径也真在：既要有"
        "逐文件的声明行与 lock 相等的 commit，也要台账指向的对照表/包路径实际存在"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac16_04(ctx: Context) -> Facts:
    """Boundary read twice: statically from the import graph, then by actually running it."""
    ported_py = [p for p in ctx.tracked() if p.startswith(PORTED_ROOT + "/") and p.endswith(".py")]
    crossings: list[str] = []
    blocked = set(BSL_IMPORT_ROOTS)
    for rel in ported_py:
        found = import_roots(rel)
        if found and found & blocked:
            crossings.append(rel)
    inside_reg = [
        rel
        for rel in ctx.tracked()
        if rel.startswith(PORTED_ROOT + "/") and rel.endswith("/registration.py")
    ]
    inside_prov = [
        rel
        for rel in ctx.tracked()
        if rel.startswith(PORTED_ROOT + "/") and "/providers/" in "/" + rel
    ]
    code, out = run_argv([sys.executable, "-c", STANDALONE_PROBE_CODE])
    return {
        "ported_py": count(len([p for p in ctx.tracked() if p.endswith(".py")])),
        "reg_inside": count(len(inside_reg)),
        "reg_inside_names": ", ".join(inside_reg[:3]) or "-",
        "prov_inside": count(len(inside_prov)),
        "prov_inside_names": ", ".join(inside_prov[:3]) or "-",
        "crossings": count(len(crossings)),
        "crossing_names": ", ".join(crossings[:3]) or "-",
        "standalone_exit": count(code),
        "control_blocked": flag("CONTROL=blocked" in out),
        "standalone_failed": first_capture(out, r"^FAILED=(.*)$"),
        "imported_modules": first_capture(out, r"^IMPORTED=(\d+)$"),
        "standalone_ok": flag(code == 0 and "CONTROL=blocked" in out and "IMPORTED=" in out),
    }


def judge_ac16_04(facts: Facts) -> Verdict:
    """``AC-16|04``: the MIT subtree is separable, and that is demonstrated by running it."""
    ok = (
        facts["reg_inside"] == "0"
        and facts["prov_inside"] == "0"
        and facts["crossings"] == "0"
        and facts["standalone_ok"] == "yes"
        and facts["control_blocked"] == "yes"
        and number(facts["imported_modules"]) > 0
    )
    readings = (
        f"搬运树里带 registration.py 的文件 {facts['reg_inside']} 个"
        f"（{facts['reg_inside_names']}）、落在 */providers/* 下的 {facts['prov_inside']} 个"
        f"（{facts['prov_inside_names']}）—— 判据点名的「registration.py 等自研代码不在 "
        f"{PORTED_ROOT}/ 内」；{PORTED_ROOT}/ 的 py 文件里 import 根命中 BSL 侧"
        f"（{', '.join(BSL_IMPORT_ROOTS)}）的 {facts['crossings']} 个：{facts['crossing_names']}",
        f"另一面是真的跑一遍：起一个把 {', '.join(BSL_IMPORT_ROOTS)} 全部拒于 meta_path 的"
        f"解释器，先自证闸门会响（import {STANDALONE_CONTROL_MODULE} 被拦 = "
        f"{facts['control_blocked']}），再 import {len(STANDALONE_MODULES)} 个 {PORTED_ROOT} "
        f"入口（datasets / stock.cons / utils）：IMPORTED={facts['imported_modules']}，"
        f"退出码 {facts['standalone_exit']}，失败读数为 {facts['standalone_failed']}",
    )
    reason = (
        ""
        if ok
        else "「混合许可边界可验证」两半都要：MIT 子树里没有自研注册代码、没有指向 BSL 侧"
        "的 import，"
        "且把 BSL 侧真的禁掉之后搬运树仍 import 得起来（闸门不自证就什么都证明不了）"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac16_08(ctx: Context) -> Facts:
    """Read the licence text and the two places a data disclaimer is supposed to live."""
    license_text = ctx.read("LICENSE")
    readme = ctx.read("README.md")
    registry = ctx.read("docs/data-rights-registry.md")
    fields = ("Licensor:", "Additional Use Grant:", "Change Date:", "Change License:")
    return {
        "license_line1": license_text.splitlines()[0].strip() if license_text else "-",
        "bsl_fields": count(sum(1 for field in fields if field in license_text)),
        "bsl_field_names": ", ".join(f.rstrip(":") for f in fields if f in license_text) or "-",
        "missing_fields": ", ".join(f.rstrip(":") for f in fields if f not in license_text) or "-",
        "disclaimer_heading": flag("数据免责声明" in readme),
        "disclaimer_body_ok": flag("不构成任何投资建议" in readme),
        "registry_disclaimer": flag("数据免责声明" in registry),
        "registry_body_ok": flag("不构成任何投资建议" in registry),
    }


def judge_ac16_08(facts: Facts) -> Verdict:
    """``AC-16|08``: the code licence is BSL 1.1 with all four clauses, and a disclaimer exists."""
    ok = (
        facts["license_line1"] == "Business Source License 1.1"
        and facts["bsl_fields"] == "4"
        and facts["disclaimer_heading"] == "yes"
        and facts["disclaimer_body_ok"] == "yes"
        and facts["registry_disclaimer"] == "yes"
        and facts["registry_body_ok"] == "yes"
    )
    readings = (
        f"LICENSE 第一行逐字为「{facts['license_line1']}」；BSL 的四个必填条款"
        f"（Licensor / Additional Use Grant / Change Date / Change License）读到 "
        f"{facts['bsl_fields']}/4：{facts['bsl_field_names']}，缺 {facts['missing_fields']}",
        f"数据免责声明：README.md 有该小节标题 = {facts['disclaimer_heading']}，正文含"
        f"「不构成任何投资建议」= {facts['disclaimer_body_ok']}；"
        "docs/data-rights-registry.md 有同名小节 = "
        f"{facts['registry_disclaimer']}，正文同句 = {facts['registry_body_ok']}",
    )
    reason = (
        ""
        if ok
        else "「LICENSE 为 BSL 1.1 + 数据免责声明存在」四要素与两处声明都要读到：标题写 BSL 1.1、"
        "四个条款齐备、README 与数据权利登记各有一段真免责正文（只留标题不算声明）"
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
    Probe(
        item="AC-8|05",
        expects=(
            "**分区**：大表按 `RANGE COLUMNS(trade_date)` 年分区 + MAXVALUE 兜底；"
            "**分区维护任务**可运行"
        ),
        summary="接线四读（yaml 行 / kind 可执行 / 派发到本体 / 本体真的调 ensure）+ DDL 渲染面 + "
        "真仓库年度上界只读档案；apply 半是生产 DDL，等一次经确认的执行才翻正",
        measure=measure_ac8_05,
        judge=judge_ac8_05,
        breaks=(
            Break("yaml 里那一行被删了", (("row_declared", "no"),), GAP),
            Break(
                "kind 掉出 EXECUTABLE_KINDS（行还在，注册时被跳过）",
                (("kind_executable", "no"),),
                GAP,
            ),
            Break(
                "派发分支摘掉（kind 可执行但没有本体）",
                (("body_dispatched", "no"),),
                GAP,
            ),
            Break(
                "本体退成只出 plan 不 apply（C48 的告警矩阵量法搬进 job）",
                (("apply_half", "no"),),
                GAP,
            ),
            Break(
                "DDL 不再渲染 MAXVALUE 兜底",
                (("ddl_layout", "no"),),
                GAP,
            ),
            Break("仓库里一张分区表都没有", (("live_partitioned", "0"),), GAP),
            Break("年度上界仍落后", (("live_gap_tables", "1"),), GAP),
            Break("档案没记分区列（口径只剩源码推断）", (("live_keys", "no"),), GAP),
            Break("没有经确认的 apply 留档", (("applied_face", "no"),), GAP),
        ),
        repair={
            "row_declared": "yes",
            "kind_executable": "yes",
            "body_dispatched": "yes",
            "apply_half": "yes",
            "ddl_layout": "yes",
            "live_partitioned": "*live_partitioned",
            "live_tables": "*live_tables",
            "live_gap_tables": "0",
            "live_keys": "yes",
            "applied_face": "yes",
        },
    ),
    Probe(
        item="AC-8|06",
        expects='**跨年写入用例**：模拟新年度数据写入成功（无"no partition for value"错误）',
        summary="用例三读（自建 probe 表 / 新年度插入被断言 / 落点分区名被断言）+ 它是 e2e 面所以"
        "门禁不跑 + 真仓库那年还缺一张分区；「写成功」要有跑过的留档",
        measure=measure_ac8_06,
        judge=judge_ac8_06,
        breaks=(
            Break("用例不再自建 probe 表", (("case_file", "no"),), GAP),
            Break("新年度那行没被插入", (("inserts_new_year", "no"),), GAP),
            Break("落点分区名没被断言（只赌写入不报错）", (("asserts_placement", "0"),), GAP),
            Break("e2e 标记被摘（于是门禁里悄悄不跑）", (("marked", "no"),), GAP),
            Break("真仓库那一年仍缺", (("fallback_gap", "1"),), GAP),
            Break("没有一次真跑的留档", (("ran_live", "no"),), GAP),
        ),
        repair={
            "case_file": "yes",
            "inserts_new_year": "yes",
            # A literal target, not ``*asserts_placement``: today's tree asserts the landing
            # partition, so copying the reading would make "someone deleted the assertion" fail
            # the self-test instead of reading as a gap on this cell.
            "asserts_placement": "1",
            "marked": "e2e",
            "fallback_gap": "0",
            "ran_live": "yes",
        },
    ),
    Probe(
        item="AC-8|01",
        expects=(
            "ods 表命名规范 `ods_<domain>_<source>`；**含源原始列 + `_source/_fetched_at/"
            "_batch_id` 元数据列**；业务 key 主键"
        ),
        summary="形状三面：派生点（ods_table 一处返回模板）+ 渲染点（三元组无条件追加、主键由 key "
        "渲染、不出自增 id）+ 真仓库当场逐表读数；注册 33 条 ods 腿与真 3 张表的差额披露给 |08",
        measure=measure_ac8_01,
        judge=judge_ac8_01,
        breaks=(
            Break("命名模板被改址（不再由 ods_table 唯一派生）", (("naming_derived", "no"),), GAP),
            Break("三元组不再无条件追加", (("trio_appended", "no"),), GAP),
            Break("元数据列少一列（_batch_id 退出声明）", (("trio_declared", "2"),), GAP),
            Break(
                "空 key 不再 fail closed（能建出没主键的表）",
                (("key_fails_closed", "no"),),
                GAP,
            ),
            Break("PRIMARY KEY 不再由业务 key 渲染", (("pk_from_key", "no"),), GAP),
            Break("生成器重新出自增 id（业务 key 退位）", (("no_auto_increment", "no"),), GAP),
            Break("仓库里一张 ods 表都没有（形状面只在源码里）", (("live_ods", "0"),), GAP),
            Break("真表里有一张命名不合规", (("ods_named", "2"),), GAP),
            Break("真表里有一张缺元数据列", (("ods_trio", "2"),), GAP),
            Break("真表里有一张用自增 id 当主键", (("ods_autokey", "1"),), GAP),
            Break(
                "形状单元面有一格不绿",
                (("n_passed", "2"), ("n_bad", "test_ods_table=exit=1")),
                GAP,
            ),
        ),
        repair={
            "n_passed": "*n_runs",
            "n_bad": "-",
            "naming_derived": "yes",
            "trio_appended": "yes",
            "trio_declared": "3",
            "key_fails_closed": "yes",
            "pk_from_key": "yes",
            "no_auto_increment": "yes",
            "live_ods": "*live_ods",
            "ods_named": "*live_ods",
            "ods_trio": "*live_ods",
            "ods_key_pk": "*live_ods",
            "ods_autokey": "0",
        },
    ),
    Probe(
        item="AC-8|02",
        expects="**数据仓库 DDL 由独立 alembic 环境管理**；应用启动不建表（单测/集成验证）",
        summary="通路而不是意图：配置/URL/版本表三处分叉 + 启动建表点唯一且在生产分支之外 + "
        "仓库 engine 在单测里换成会炸的桩 + 真仓库两套版本表各自在当前版本",
        measure=measure_ac8_02,
        judge=judge_ac8_02,
        breaks=(
            Break("两套 alembic 共用一个 script_location", (("locations_split", "no"),), GAP),
            Break("仓库 env 挂上 ORM metadata", (("no_autogen", "no"),), GAP),
            Break("仓库 env 不再取 data_database_url", (("warehouse_url", "no"),), GAP),
            Break("版本表合并回 alembic_version", (("version_table_split", "no"),), GAP),
            Break("控制 env 也能连到仓库 URL", (("control_cannot_reach_data_url", "no"),), GAP),
            Break("启动建表点不再唯一／失去生产分支守卫", (("startup_guarded", "no"),), GAP),
            Break("create_tables 引到了 data_engine", (("create_tables_control_only", "no"),), GAP),
            Break("控制库里长出了 ods/dwd 表", (("ctrl_warehouse_tables", "3"),), GAP),
            Break("档案没记两套版本表", (("version_tables_in_face", "no"),), GAP),
            Break(
                "归属单元面有一格不绿",
                (
                    ("n_passed", "5"),
                    ("n_bad", "test_create_tables_never_touches_the_warehouse_engine=exit=1"),
                ),
                GAP,
            ),
        ),
        repair={
            "n_passed": "*n_runs",
            "n_bad": "-",
            "locations_split": "yes",
            "no_autogen": "yes",
            "warehouse_url": "yes",
            "version_table_split": "yes",
            "control_cannot_reach_data_url": "yes",
            "startup_guarded": "yes",
            "create_tables_control_only": "yes",
            "ctrl_warehouse_tables": "0",
            "version_tables_in_face": "yes",
        },
    ),
    Probe(
        item="AC-8|03",
        expects="upsert **key 级**幂等（重复执行无副作用；价格修正场景下不产生重复行，单测）",
        summary="判据原文点名的单测面：五格节点（builder 形状 + 两条落库路径 + 同键两次还是一行）"
        "加语句三面（AS new 别名式 / UPDATE 段不碰 key / 空列表 fail closed）；真库那格是 e2e，"
        "读数只披露",
        measure=measure_ac8_03,
        judge=judge_ac8_03,
        breaks=(
            Break(
                "幂等单元面有一格不绿",
                (
                    ("n_passed", "4"),
                    ("n_bad", "test_landing_the_same_key_twice=exit=1"),
                ),
                GAP,
            ),
            Break("upsert 退回普通 INSERT（重跑就是追加）", (("alias_form", "no"),), GAP),
            Break("UPDATE 段开始改写业务 key（改到别的行上去）", (("key_left_alone", "no"),), GAP),
            Break("空列表不再 fail closed（静默生成无列语句）", (("fails_closed", "0"),), GAP),
            Break("staging 路径不走 key 级 upsert", (("staging_key_clause", "no"),), GAP),
        ),
        repair={
            "n_passed": "*n_runs",
            "n_bad": "-",
            "alias_form": "yes",
            "key_left_alone": "yes",
            "fails_closed": "2",
            "staging_key_clause": "yes",
        },
    ),
    Probe(
        item="AC-8|07",
        expects="无 `id` 列的 ods 表在 `/tables` 分页可用",
        summary="ORDER BY 由表形状推出：回落链 id→业务主键→首列、limit/offset 走绑定参数、无列可排 "
        "fail closed、端点真的读 table_shape 再拼 SQL、模块里不回潮硬编码 ORDER BY id；"
        "五格节点含 SQLite 真翻页与端点那一格 HTTP",
        measure=measure_ac8_07,
        judge=judge_ac8_07,
        breaks=(
            Break(
                "分页单元面有一格不绿",
                (
                    ("n_passed", "4"),
                    ("n_bad", "test_get_data_from_a_table_without_id_column=exit=1"),
                ),
                GAP,
            ),
            Break("无 id 表没有确定序（翻页会重复/漏行）", (("falls_back_to_key", "no"),), GAP),
            Break("无主键表不再回落首列", (("first_column_still_deterministic", "no"),), GAP),
            Break("limit/offset 改成拼接（同时丢掉注入防线）", (("offset_bound", "no"),), GAP),
            Break("无列可排不再 fail closed", (("keyless_fails_closed", "no"),), GAP),
            Break(
                "端点绕过 table_shape 自己拼 SQL",
                (("endpoint_reads_the_table_shape", "no"),),
                GAP,
            ),
            Break("硬编码 ORDER BY id 回潮", (("no_hardcoded_order", "no"),), GAP),
        ),
        repair={
            "n_passed": "*n_runs",
            "n_bad": "-",
            "falls_back_to_key": "yes",
            "first_column_still_deterministic": "yes",
            "offset_bound": "yes",
            "keyless_fails_closed": "yes",
            "endpoint_reads_the_table_shape": "yes",
            "no_hardcoded_order": "yes",
        },
    ),
    Probe(
        item="AC-9|01",
        expects="覆盖 P0 域（字段映射/单位换算/复权口径/key 规范化/停牌语义/差异率分母）",
        summary="表在（load_mapping/normalize_frame）+ P0 五域逐个可指名 + 六类口径由表承载",
        measure=measure_ac9_01,
        judge=judge_ac9_01,
        breaks=(
            Break("映射目录空了（口径表不再存在）", (("table_loads", "no"),), GAP),
            Break(
                "P0 少了一个域的映射",
                (("p0_mapped", "4"), ("p0_missing", "financial_indicator")),
                GAP,
            ),
            Break("单位换算不再写在表里（scale 键消失）", (("cat_scale", "no"),), GAP),
            Break("key 规范化退回各源自己拼（normalize 键消失）", (("cat_normalize", "no"),), GAP),
            Break("字段映射这一列名都不在表里了", (("cat_from", "no"),), GAP),
            Break("复权口径只在代码里、不在这张表里", (("cat_adjust", "no"),), GAP),
            Break("停牌语义无处声明", (("cat_suspension", "no"),), GAP),
            Break("差异率分母不进表（分母由实现决定）", (("cat_denominator", "no"),), GAP),
        ),
        repair={
            "table_loads": "yes",
            "p0_mapped": "*p0_total",
            "p0_missing": "-",
            "cat_from": "yes",
            "cat_scale": "yes",
            "cat_adjust": "yes",
            "cat_normalize": "yes",
            "cat_suspension": "yes",
            "cat_denominator": "yes",
        },
    ),
    Probe(
        item="AC-9|06",
        expects="dwd 合并：权威源存在则取权威、缺失降级填补",
        summary="服务层三条节点全绿 + 权威降级/source/_diff_flag/_as_of 四面代码面在位",
        measure=measure_ac9_06,
        judge=judge_ac9_06,
        breaks=(
            Break(
                "服务层节点变红（四问里有一问的服务面没了）",
                (("passed", "2"), ("bad", "x=exit=1")),
                GAP,
            ),
            Break("节点被改名（跑不满三条就不算测过）", (("runs", "2"),), GAP),
            Break("降级不再计数（权威缺失就丢行而不是填补）", (("authority_fallback", "no"),), GAP),
            Break("source 留痕列不再写（那一行是谁供的查不出来）", (("source_traced", "no"),), GAP),
            Break("_diff_flag 变成恒 0（打标不再由不一致决定）", (("diff_flagged", "no"),), GAP),
            Break("_as_of 不再写进合并结果", (("as_of_stamped", "no"),), GAP),
            Break(
                "_as_of 写的不是窗口末（版本列失去『这一版到哪一天』的含义）",
                (("as_of_is_window_end", "no"),),
                GAP,
            ),
            Break("四列不再一起是留痕列定义", (("trace_columns", "no"),), GAP),
        ),
        repair={
            "passed": "*runs",
            "bad": "-",
            "authority_fallback": "yes",
            "source_traced": "yes",
            "diff_flagged": "yes",
            "as_of_stamped": "yes",
            "as_of_is_window_end": "yes",
            "trace_columns": "yes",
        },
    ),
    Probe(
        item="AC-9|07",
        expects="ods 中已存在 key 被修正后，dwd 对应行同步更新（单测）",
        summary="两条修订节点全绿 + 传播链五段（交键/重拼契约键/下传 reader/键级 upsert）",
        measure=measure_ac9_07,
        judge=judge_ac9_07,
        breaks=(
            Break("修订传播节点变红（值不再同步）", (("passed", "1"), ("bad", "x=exit=1")), GAP),
            Break("节点被改名（两条里少一条就不算链上有人看过）", (("runs", "1"),), GAP),
            Break(
                "affected_keys 不再下传 reader（窗口外的修订取不到数）",
                (("reader_gets_keys", "no"),),
                GAP,
            ),
            Break("修订键不再参与差异重算", (("keys_extend_diffs", "no"),), GAP),
            Break(
                "run_hook 不再把 ods 拼法重拼成契约键（600519.SH 对不上 600519）",
                (("hook_resells_keys", "no"),),
                GAP,
            ),
            Break(
                "runner 不再交出被改的键（ods 写侧与合并侧断开）",
                (("runner_publishes_keys", "no"),),
                GAP,
            ),
            Break(
                "写侧退回追加（同一键重跑就多一行，同步无从谈起）", (("writer_upserts", "no"),), GAP
            ),
        ),
        repair={
            "passed": "*runs",
            "bad": "-",
            "reader_gets_keys": "yes",
            "keys_extend_diffs": "yes",
            "hook_resells_keys": "yes",
            "runner_publishes_keys": "yes",
            "writer_upserts": "yes",
        },
    ),
    Probe(
        item="AC-17|01",
        expects="逐项显式阻断",
        summary="成员名单、横幅与成员一一配对、三种掩盖形状为零、make 自己数出的成员数与配方相符、"
        "可复现环境、判定成员在名单里",
        measure=measure_ac17_01,
        judge=judge_ac17_01,
        breaks=(
            Break("门禁成员被清空（逐项阻断无从谈起）", (("members", "0"),), GAP),
            Break(
                "一个成员没有配方（target 存在却 0 行命令，永远绿）",
                (("no_recipe", "1"), ("no_recipe_sample", "a2-check")),
                GAP,
            ),
            Break(
                "成员调用被 `-` 前缀吞掉退出码",
                (
                    ("ignore_prefix", "1"),
                    ("mask_sample", "-@$(MAKE) --no-print-directory a2-check"),
                ),
                GAP,
            ),
            Break("成员后面接 `|| echo` 把失败说成通过", (("echo_mask", "1"),), GAP),
            Break("两个成员串在同一行", (("chained", "1"),), GAP),
            Break(
                "开发者视图的全树静态检查被放进门禁",
                (("dev_view", "1"), ("dev_view_sample", "lint")),
                GAP,
            ),
            Break("横幅数与成员数不配对（有一项不被显式宣告）", (("banner_match", "no"),), GAP),
            Break("某个成员的名字与它自己的宣告段错位", (("banner_unpaired", "1"),), GAP),
            Break(
                "收尾的 PASSED 段被移走（绿色横幅再也不能证明逐项跑完了）",
                (("banner_closing", "frontend-e2e"),),
                GAP,
            ),
            Break(
                "make 自己数出的 sub-make 为 0（配方列了成员却没有一个被启动）",
                (("dry_members", "0"),),
                GAP,
            ),
            Break("`make -n gate` 自己失败", (("dry_rc", "2"),), GAP),
            Break("解释器 import opendata 失败（可复现环境不成立）", (("import_rc", "1"),), GAP),
            Break(
                "import 到的 opendata 不在这棵树里（量的不是这个仓库）",
                (("import_under_root", "no"),),
                GAP,
            ),
            Break("判定成员自己被移出 gate 配方", (("self_member", "no"),), GAP),
        ),
        repair={
            "members": "*members",
            "banners": "*banners",
            "banner_match": "yes",
            "banner_unpaired": "0",
            "banner_closing": "PASSED",
            "no_recipe": "0",
            "ignore_prefix": "0",
            "echo_mask": "0",
            "chained": "0",
            "dev_view": "0",
            "dry_rc": "0",
            "dry_members": "*members",
            "import_rc": "0",
            "import_under_root": "yes",
            "self_member": "yes",
        },
    ),
    Probe(
        item="AC-16|06",
        expects="A2 搬运完成后基线清零",
        summary="零依赖断言两条门禁命令都绿、只降不升的门禁在位、import/动态形态为零、"
        "冻结基线条数为零",
        measure=measure_ac16_06,
        judge=judge_ac16_06,
        breaks=(
            Break("扫描器 CLI 变红（扫描面或基线被移动）", (("detector_exit", "1"),), GAP),
            Break(
                "检测器自测不咬了（漏检的仪器，读数为零不代表没有）",
                (("self_test_exit", "1"),),
                GAP,
            ),
            Break(
                "集成层又出现 import akshare（判据原文点名的形态）",
                (("import_live", "1"), ("frozen", "4")),
                GAP,
            ),
            Break("动态 import 绕过面回来了", (("dynamic_live", "1"), ("frozen", "4")), GAP),
            Break("新增引用不再被拒（现场比冻结多一条也算过）", (("no_new_reference", "no"),), GAP),
            Break("--update 不再拒绝长大（只降不升被拆）", (("only_down", "no"),), GAP),
            Break("基线还在（本轮实际卡住的那一格：3 条没清零）", (("frozen", "3"),), GAP),
        ),
        repair={
            "detector_exit": "0",
            "self_test_exit": "0",
            "import_live": "0",
            "dynamic_live": "0",
            "no_new_reference": "yes",
            "only_down": "yes",
            "frozen": "0",
        },
    ),
    Probe(
        item="AC-16|07",
        expects="在未安装 akshare/openbb 的干净环境中 P0 域集成测试通过",
        summary="选择式有可指的名字（marker 注册 + 9 个标记单元 + 5 个 P0 域全覆盖），"
        "本机和留档的干净 venv 都全绿，且留档是最新一轮自己写的、覆盖当前标记集",
        measure=measure_ac16_07,
        judge=judge_ac16_07,
        breaks=(
            Break(
                "integration marker 不再注册（--strict-markers 下选择式直接报错）",
                (("registered", "no"),),
                GAP,
            ),
            Break(
                "strict markers 被摘掉（未注册的 marker 退成一条警告）",
                (("strict", "no"),),
                GAP,
            ),
            Break(
                "标记集被清空（选中 0 条也算「通过」）",
                (("units", "0"), ("passed", "0")),
                GAP,
            ),
            Break("某条 P0 域不再被这组用例点到", (("domains_missing", "1"),), GAP),
            Break("这一遍有用例变红", (("run_exit", "1"), ("failed", "1")), GAP),
            Break("有用例改成 skip（skip 不是通过）", (("skipped", "3"),), GAP),
            Break("本机解释器又能 import 上游包（干净面没了）", (("clean_here", "no"),), GAP),
            Break("干净 venv 那一遍本身是红的", (("archive_exit", "1"),), GAP),
            Break(
                "标记集加了新模块，留档却没重跑（证据不再覆盖判据）",
                (("archive_blind", "1"),),
                GAP,
            ),
            Break(
                "把旧留档拷进新轮次目录（正文声明的轮次和目录对不上）",
                (("archive_self_written", "no"),),
                GAP,
            ),
            Break("留档里上游包又被装上了", (("archive_absent", "no"),), GAP),
        ),
        repair={
            "registered": "yes",
            "strict": "yes",
            "units": "*units",
            "domains_missing": "0",
            "run_exit": "0",
            "passed": "*passed",
            "failed": "0",
            "skipped": "0",
            "clean_here": "yes",
            "archive_exit": "0",
            "archive_blind": "0",
            "archive_absent": "yes",
        },
    ),
    Probe(
        item="AC-11|01",
        expects="symbols/start/end/source/layer/**adjust**/fields/分页",
        summary="路由与 OpenAPI 在、判据点名的 9 个入参真在签名里、默认层与复权就是 dwd/none、"
        "响应按契约列交出",
        measure=measure_ac11_01,
        judge=judge_ac11_01,
        breaks=(
            Break("默认层不再是合并视图", (("layer_default", "ods"),), GAP),
            Break("默认复权口径被改成前复权", (("adjust_default", "qfq"),), GAP),
            Break("判据点名的入参少了一个", (("knobs_missing", "fields"),), GAP),
            Break("路由形状变了（域名不再挂在资产类下面）", (("route_shape", "no"),), GAP),
            Break("载荷不再同时回 columns/rows（无法对契约）", (("payload_shape", "no"),), GAP),
            Break("OpenAPI 那两条里有一条变红", (("route_passed", "5"),), GAP),
            Break(
                "某个节点 id 被改名（面缩了而不是少跑一条）",
                (("route_absent", "test_query_parameters_are_enumerated"),),
                GAP,
            ),
        ),
        repair={
            "route_exit": "0",
            "route_passed": "*route_runs",
            "route_failed": "0",
            "route_skipped": "0",
            "route_absent": "-",
            "route_shape": "yes",
            "knobs_missing": "-",
            "layer_default": "dwd",
            "adjust_default": "none",
            "payload_shape": "yes",
        },
    ),
    Probe(
        item="AC-11|02",
        expects="同一标的 qfq 序列与 akshare 官方 qfq 对照一致",
        summary="qfq/hfq 由服务端按因子表合成（缺因子 400、缺表 501）；官方对照腿是分派表条目，"
        "默认 akshare 并解析到搬运模块，留档一次真机 run 的腿与 module:function 都要与现值一致",
        measure=measure_ac11_02,
        judge=judge_ac11_02,
        breaks=(
            Break("合成接线被摘掉（参数读一遍就丢）", (("synthesis", "no"),), GAP),
            Break("默认腿又回到 sina", (("default_leg", "sina"),), GAP),
            Break(
                "分派表里 akshare 那条被删了",
                (("leg_target", "(absent)"), ("leg_module", "(absent)")),
                GAP,
            ),
            Break(
                "akshare 条目指的是 sina 模块（换个键名自称 akshare）",
                (("leg_module", "opendata_http.stock.stock_zh_a_sina"),),
                GAP,
            ),
            Break("解析到的模块不再声明 akshare 出身", (("leg_is_ported_akshare", "no"),), GAP),
            Break("留档那次 run 走的不是当场默认腿", (("run_official_leg", "sina"),), GAP),
            Break(
                "仪器改了、留档还是旧的那次 run",
                (
                    (
                        "run_official_target",
                        "opendata_http.stock.stock_zh_a_sina:stock_zh_a_daily",
                    ),
                ),
                GAP,
            ),
            Break("留档那次 run 里一半标的没答上来（ERROR 行）", (("run_error_rows", "9"),), GAP),
            Break("留档那次 run 里有 FAIL 行", (("run_fail_rows", "3"),), GAP),
            Break("留档那次 run 一行都没对照（空跑）", (("run_ok_rows", "0"),), GAP),
            Break("复权 HTTP 面少一条", (("adj_passed", "9"),), GAP),
            Break(
                "对照仪器的判定节点不见了",
                (("adj_absent", "test_matching_series_passes"),),
                GAP,
            ),
        ),
        repair={
            "adj_exit": "0",
            "adj_passed": "*adj_runs",
            "adj_failed": "0",
            "adj_skipped": "0",
            "adj_absent": "-",
            "synthesis": "yes",
            "default_leg": "akshare",
            "leg_target": "*leg_target",
            "leg_module": "*leg_module",
            "leg_is_ported_akshare": "yes",
            "run_official_leg": "*default_leg",
            "run_official_target": "*leg_target",
            # Not ``*run_ok_rows``: this run compared 0 rows, and a repair that copies that
            # back declares a gap nobody can close. The closed reading is the same 5 symbols
            # x 2 methods with every pair compared and none of them over tolerance.
            "run_ok_rows": "10",
            "run_fail_rows": "0",
            "run_error_rows": "0",
        },
    ),
    Probe(
        item="AC-11|03",
        expects="`fields` 白名单校验（`fields=open;DROP TABLE` 返回 400）",
        summary="判据点名的四个安全面各自有实测：白名单 400 且不执行、三个枚举、符号绑定、"
        "导出公式转义发生在 handler 里",
        measure=measure_ac11_03,
        judge=judge_ac11_03,
        breaks=(
            Break("source 又变成自由字符串", (("source_leg_check", "no"),), GAP),
            Break("layer 的枚举校验被摘掉", (("enum_params", "layer"),), GAP),
            Break("adjust 的枚举校验被摘掉", (("enum_params", "adjust"),), GAP),
            Break("那条字面载荷被换成别的写法（判据面没了）", (("deny_literal", "no"),), GAP),
            Break("符号不再进绑定字典，而是拼进 SQL", (("bind_symbols", "no"),), GAP),
            Break(
                "SQL 文本里开始出现符号值的形状（不再是占位符）",
                (("symbol_placeholder", "no"),),
                GAP,
            ),
            Break("导出转义退回「调用方自己记得转义」", (("csv_escape", "no"),), GAP),
            Break("安全面有一条变红", (("safety_failed", "1"), ("safety_exit", "1")), GAP),
            Break(
                "安全面少一条用例",
                (("safety_absent", "test_a_source_that_is_not_a_registered_leg_is_a_400"),),
                GAP,
            ),
        ),
        repair={
            "safety_exit": "0",
            "safety_passed": "*safety_runs",
            "safety_failed": "0",
            "safety_skipped": "0",
            "safety_absent": "-",
            "deny_literal": "yes",
            "enum_params": "-",
            "source_leg_check": "yes",
            "bind_symbols": "yes",
            "symbol_placeholder": "yes",
            "csv_escape": "yes",
        },
    ),
    Probe(
        item="AC-11|04",
        expects="不带日期区间的查询不会触发全分区扫描（EXPLAIN 验证）",
        summary="无区间请求仍被默认窗与行数上限夹住（实测绿），且判据点名的 EXPLAIN 真的发到了"
        "跑着的 MySQL 上：每张已落地的分区表都只读到部分分区，留档按查询层摘要钉住",
        measure=measure_ac11_04,
        judge=judge_ac11_04,
        breaks=(
            Break("执行计划面又归零（判据的验证方式）", (("explain_faces", "0"),), GAP),
            Break("默认窗被改成不限（0 天）", (("window_days", "0"),), GAP),
            Break("无区间不再补默认窗", (("bounded_default", "no"),), GAP),
            Break("SELECT 的行数上限被摘掉", (("row_cap", "no"),), GAP),
            Break("时间窗面少一条", (("window_passed", "4"),), GAP),
            Break(
                "窗口边界单元面被改名",
                (("window_absent", "test_reversed_range_fails_closed"),),
                GAP,
            ),
            Break("仓库里没有一张已落地的分区表可量", (("explain_tables_landed", "0"),), GAP),
            Break("留档里有表的计划读到了全部分区", (("explain_pruned_no", "1"),), GAP),
            Break(
                "改了拼 SQL 的代码却没重跑计划（留档属于另一版查询层）",
                (("explain_archive_is_current", "no"),),
                GAP,
            ),
            Break(
                "把旧留档拷进新轮次目录（正文声明的轮次和目录对不上）",
                (("explain_archive_in_place", "no"),),
                GAP,
            ),
        ),
        repair={
            "window_exit": "0",
            "window_passed": "*window_runs",
            "window_failed": "0",
            "window_skipped": "0",
            "window_absent": "-",
            "window_days": "*window_days",
            "bounded_default": "yes",
            "row_cap": "yes",
            "explain_faces": "1",
            "explain_tables_landed": "*explain_tables_landed",
            "explain_pruned_no": "0",
            "explain_archive_is_current": "yes",
            "explain_archive_in_place": "yes",
        },
    ),
    Probe(
        item="AC-11|05",
        expects="可查差异报告",
        summary="diff-report 门按域取 dq_diff_report 的样本差异行，受同一套 scope/batch/limit 约束",
        measure=measure_ac11_05,
        judge=judge_ac11_05,
        breaks=(
            Break("路由不再注册", (("route", "no"),), GAP),
            Break("门读的表与校对器写的明细表分家", (("table_constant", "dq_diffs"),), GAP),
            Break("处理器不再 FROM 那张表", (("reads_constant_table", "no"),), GAP),
            Break("门不再按域 scope 判", (("auth_gate", "no"),), GAP),
            Break("batch_id 过滤被摘掉（一次跑与全部跑没区别）", (("batch_filter", "no"),), GAP),
            Break("limit 上限被摘掉", (("limit_bounded", "no"),), GAP),
            Break("报告面有一条变红", (("diff_exit", "1"), ("diff_failed", "1")), GAP),
            Break(
                "报告面少一条用例",
                (("diff_absent", "test_batch_id_selects_one_cross_check_run"),),
                GAP,
            ),
        ),
        repair={
            "diff_exit": "0",
            "diff_passed": "*diff_runs",
            "diff_failed": "0",
            "diff_skipped": "0",
            "diff_absent": "-",
            "route": "yes",
            "table_constant": "dq_diff_report",
            "reads_constant_table": "yes",
            "auth_gate": "yes",
            "batch_filter": "yes",
            "limit_bounded": "yes",
        },
    ),
    Probe(
        item="AC-5|01",
        expects="逐项校验通过",
        summary="manifest 的每条 sha/计数全部对着磁盘现算，且与 upstream.lock 的文件集相等",
        measure=measure_ac5_01,
        judge=judge_ac5_01,
        breaks=(
            Break("仪器 --check 不通过", (("check_exit", "1"),), GAP),
            Break("清单有一条 sha 对不上磁盘内容", (("sha_mismatch", "1"),), GAP),
            Break("清单登记的文件磁盘上已经没有", (("sha_absent", "1"),), GAP),
            Break("搬运树里有一个清单未登记的 py 文件", (("unlisted_on_disk", "1"),), GAP),
            Break("清单里有一条磁盘上不存在的路径", (("stale_entries", "1"),), GAP),
            Break("非 py 资源没有进资源清单", (("res_unregistered", "1"),), GAP),
            Break("资源清单里有一条已经不在了", (("res_stale", "1"),), GAP),
            Break("counts.total_lines 与现算行数分家", (("count_lines_ok", "no"),), GAP),
            Break("counts.total_files 与实际条目数分家", (("count_total_ok", "no"),), GAP),
            Break("upstream.lock 与 manifest 的文件集分家", (("lock_paths_ok", "no"),), GAP),
        ),
        repair={
            "check_exit": "0",
            "sha_mismatch": "0",
            "sha_absent": "0",
            "unlisted_on_disk": "0",
            "stale_entries": "0",
            "res_unregistered": "0",
            "res_stale": "0",
            "lock_paths_ok": "yes",
            "count_py_ok": "yes",
            "count_res_ok": "yes",
            "count_total_ok": "yes",
            "count_lines_ok": "yes",
        },
    ),
    Probe(
        item="AC-5|02",
        expects="P0 域子模块搬运完成",
        summary="搬运面的实际使用与 D9 范围对得上，且 1A/1B 批次有机器可读的声明字段",
        measure=measure_ac5_02,
        judge=judge_ac5_02,
        breaks=(
            Break("首方代码一处都不取用搬运层", (("has_calls", "no"),), GAP),
            Break("有一个被取用的端点聚合层没导出", (("called_missing", "1"),), GAP),
            Break("点名移出本迭代的子包混进了清单", (("excluded_present", "1"),), GAP),
            Break("D9 点名的子包有没搬到的", (("scope_missing", "1"),), GAP),
            Break("没有任何机器字段能说 1A/1B 批次", (("tier_field", "-"),), GAP),
        ),
        repair={
            "has_calls": "yes",
            "called_missing": "0",
            "excluded_present": "0",
            "scope_missing": "0",
            "tier_field": "opendata/data/domains.yaml#batch",
        },
    ),
    Probe(
        item="AC-5|03",
        expects="人工待办清零",
        summary="差异报告待办为零，且 AST 扫描在 import/动态/字符串三个形态与冻结基线上都清零",
        measure=measure_ac5_03,
        judge=judge_ac5_03,
        breaks=(
            Break("报告自报人工待办不为零", (("todo_reported", "1"),), GAP),
            Break("待办清单段落里还挂着复选项", (("todo_section_ok", "no"),), GAP),
            Break("扫描器实跑失败", (("detector_exit", "1"),), GAP),
            Break("扫描器反事实自检失败", (("self_test_exit", "1"),), GAP),
            Break("有一个 import 形态的上游引用", (("import_live", "1"),), GAP),
            Break("有一处动态导入取上游模块", (("dynamic_live", "1"),), GAP),
            Break("有一处字符串常量指向上游", (("string_live", "1"),), GAP),
            Break("冻结基线没有清零", (("frozen", "1"),), GAP),
        ),
        repair={
            "todo_reported": "0",
            "todo_section_ok": "yes",
            "detector_exit": "0",
            "self_test_exit": "0",
            "import_live": "0",
            "dynamic_live": "0",
            "string_live": "0",
            "frozen": "0",
        },
    ),
    Probe(
        item="AC-5|04",
        expects="环境中 P0 域函数可用",
        summary="根目录与索引与解释器三面都没有 akshare，而首方代码取用的端点全部可达",
        measure=measure_ac5_04,
        judge=judge_ac5_04,
        breaks=(
            Break("根目录 akshare/ 又回到磁盘上", (("root_dir", "yes"),), GAP),
            Break("git 索引里该前缀下还有文件", (("tracked_under_root", "1"),), GAP),
            Break("解释器里装了 akshare", (("spec", "present"),), GAP),
            Break("搬运层 import 失败", (("import_exit", "1"),), GAP),
            Break("首方代码一处都不取用搬运层", (("call_targets", "0"),), GAP),
            Break("有一个端点函数取不到", (("callable_missing", "1"),), GAP),
            Break("干净 venv 侧证不再报 absent", (("archive_absent", "no"),), GAP),
        ),
        repair={
            "root_dir": "no",
            "tracked_under_root": "0",
            "spec": "absent",
            "import_exit": "0",
            "callable_missing": "0",
            "archive_absent": "yes",
        },
    ),
    Probe(
        item="AC-5|05",
        expects="MIT 版权声明",
        summary="搬运树每个 py 文件都带 MIT 声明与来源行，来源 commit/URL 与 lock 逐字相等",
        measure=measure_ac5_05,
        judge=judge_ac5_05,
        breaks=(
            Break("有一个清单文件读不到头部", (("mit_missing", "1"), ("walk_ok", "no")), GAP),
            Break("有一个文件缺 MIT 声明", (("mit_missing", "1"),), GAP),
            Break("有一个文件缺来源标注", (("source_missing", "1"),), GAP),
            Break("来源 URL 与 lock 不等", (("url_missing", "1"),), GAP),
            Break("banner 里出现第二个 commit", (("banner_commits", "2"),), GAP),
            Break("commit 与 lock 里那个不相等", (("commit_ok", "no"),), GAP),
            Break("许可证台账不再同时提到 akshare 与 MIT", (("notices_ok", "no"),), GAP),
        ),
        repair={
            "walk_ok": "yes",
            "mit_missing": "0",
            "source_missing": "0",
            "url_missing": "0",
            "banner_commits": "1",
            "commit_ok": "yes",
            "notices_ok": "yes",
        },
    ),
    Probe(
        item="AC-5|06",
        expects="资源访问函数可运行",
        summary="datasets.py 的资源访问函数当场调用：或返回路径，或 raise 且回指仪器派生的登记段",
        measure=measure_ac5_06,
        judge=judge_ac5_06,
        breaks=(
            Break("有一个函数报的不是「不可用」", (("raised_other", "1"), ("marked", "1")), GAP),
            Break("raise 文本不回指登记文件", (("points_at_register", "no"),), GAP),
            Break("报告里没有不可用登记段落", (("register_heading", "no"),), GAP),
            Break("登记段落没有逐个点名这些函数", (("register_lists_marked", "no"),), GAP),
            Break(
                "登记行与重放表同形，会顶掉 AC-17|05 的行数等式",
                (("shape_clash", "2"),),
                GAP,
            ),
            Break("清单登记的内置资源磁盘上缺", (("resources_present_ok", "no"),), GAP),
        ),
        repair={
            "raised_other": "0",
            "marked": "*functions",
            "points_at_register": "yes",
            "register_heading": "yes",
            "register_lists_marked": "yes",
            "shape_clash": "0",
            "resources_present_ok": "yes",
        },
    ),
    Probe(
        item="AC-5|07",
        expects="人工 triage 留档",
        summary="搬运层专用 bandit 目标 + 本轮全量留档，triage 覆盖面等于今天的树且免罪句仍成立",
        measure=measure_ac5_07,
        judge=judge_ac5_07,
        breaks=(
            Break("没有搬运层专用扫描目标", (("target_ok", "no"),), GAP),
            Break("本轮没有全量扫描留档", (("archive", "-"), ("archive_self_written", "no")), GAP),
            Break("留档不是本轮自写的", (("archive_self_written", "no"),), GAP),
            Break("留档扫到的文件在搬运清单外", (("scan_manifest_ok", "no"),), GAP),
            Break("留档里没有 B 层规则", (("scan_b_only", "no"),), GAP),
            Break("有一项落在 triage 未处置的规则上", (("findings_untriaged", "1"),), GAP),
            Break("triage 说「无」的规则又出现", (("cleared_present", "1"),), GAP),
            Break("出现 pickle/yaml 类反序列化项", (("deser_present", "1"),), GAP),
            Break("triage 声明的覆盖面小于今天的树", (("surface_covered", "no"),), GAP),
        ),
        repair={
            "target_ok": "yes",
            "archive_self_written": "yes",
            "scan_manifest_ok": "yes",
            "scan_b_only": "yes",
            "findings_untriaged": "0",
            "cleared_present": "0",
            "deser_present": "0",
            "surface_covered": "yes",
            "triage_files_claimed": "*ported_files",
        },
    ),
    Probe(
        item="AC-16|01",
        expects="代码审查记录",
        summary="七个自研 provider 包逐个查「提交说明声明 + 逐包审查留档」，规则文本在位不算数",
        measure=measure_ac16_01,
        judge=judge_ac16_01,
        breaks=(
            Break("合规文档里的留痕要求不见了", (("rule_written", "no"),), GAP),
            Break(
                "规则没把 AST 断言扩展到零 openbb",
                (("rule_written", "yes"), ("rule_extends_ast", "no")),
                GAP,
            ),
            Break(
                "有一个 provider 的提交说明不带声明",
                (("declared_pkgs", "6"), ("undeclared_list", "ths")),
                GAP,
            ),
            Break(
                "有包没有逐包审查留档",
                (("declared_pkgs", "*provider_pkgs"), ("recorded_pkgs", "5")),
                GAP,
            ),
            Break("适配层目录整块不在跟踪清单里（判据面为空）", (("provider_pkgs", "0"),), GAP),
        ),
        repair={
            "rule_written": "yes",
            "rule_extends_ast": "yes",
            "declared_pkgs": "*provider_pkgs",
            "recorded_pkgs": "*provider_pkgs",
            "undeclared_list": "-",
        },
    ),
    Probe(
        item="AC-16|02",
        expects="全库无 OpenBB 源码或其近似复制",
        summary="运行时四包逐个 AST 解析找 openbb import 根（带正向对照），再核两项人工核查",
        measure=measure_ac16_02,
        judge=judge_ac16_02,
        breaks=(
            Break("运行时包里出现一个 openbb import 根", (("openbb_imports", "1"),), GAP),
            Break(
                "AST 走查读到 0 个 import 根（尺子失效，零不构成证据）",
                (("import_roots_seen", "0"), ("runtime_py", "0")),
                GAP,
            ),
            Break(
                "一个运行时文件解析不了，走查面比跟踪面小",
                (("runtime_py", "516"), ("runtime_py_files", "517")),
                GAP,
            ),
            Break(
                "零依赖扫描器的禁止清单不含 openbb",
                (("openbb_imports", "0"), ("forbidden_covers_openbb", "no")),
                GAP,
            ),
            Break("提交声明核查缺一个包", (("openbb_imports", "0"), ("declared_pkgs", "6")), GAP),
            Break(
                "逐包人工抽查留档没做过",
                (
                    ("openbb_imports", "0"),
                    ("declared_pkgs", "*provider_pkgs"),
                    ("recorded_pkgs", "0"),
                ),
                GAP,
            ),
        ),
        repair={
            "openbb_imports": "0",
            "forbidden_covers_openbb": "yes",
            "declared_pkgs": "*provider_pkgs",
            "recorded_pkgs": "*provider_pkgs",
        },
    ),
    Probe(
        item="AC-16|03",
        expects="每文件 MIT 版权声明",
        summary="搬运树逐文件 MIT 头 + 来源 commit 与 lock 相等，外加台账点名的路径真存在于磁盘",
        measure=measure_ac16_03,
        judge=judge_ac16_03,
        breaks=(
            Break("有一个搬运文件缺 MIT 声明行", (("mit_missing", "1"),), GAP),
            Break("有一个搬运文件缺来源标注行", (("source_missing", "1"),), GAP),
            Break("来源标注的 commit 与 lock 不等", (("commit_ok", "no"),), GAP),
            Break(
                "搬运树里出现两个 commit 取值（另一次同步的残留）",
                (("banner_commits", "2"), ("commit_ok", "no")),
                GAP,
            ),
            Break("台账写不出来源仓库", (("notices_repo", "no"),), GAP),
            Break("台账不写 lock 那个 40 位 commit", (("notices_commit", "no"),), GAP),
            Break("台账点名的路径在磁盘上不存在", (("stale_paths", "1"),), GAP),
        ),
        repair={
            "walk_ok": "yes",
            "mit_missing": "0",
            "source_missing": "0",
            "url_missing": "0",
            "banner_commits": "1",
            "commit_ok": "yes",
            "notices_repo": "yes",
            "notices_commit": "yes",
            "notices_mit": "yes",
            "stale_paths": "0",
        },
    ),
    Probe(
        item="AC-16|04",
        expects="混合许可边界可验证",
        summary="MIT 子树里没有自研 registration.py 也不 import BSL 侧，再禁掉 BSL 侧跑一遍导入",
        measure=measure_ac16_04,
        judge=judge_ac16_04,
        breaks=(
            Break("搬运树里出现一个 registration.py", (("reg_inside", "1"),), GAP),
            Break(
                "自研 provider 目录被搬进 MIT 子树",
                (("reg_inside", "0"), ("prov_inside", "1")),
                GAP,
            ),
            Break(
                "搬运树里有一个文件 import 了 BSL 侧",
                (("crossings", "1"), ("standalone_ok", "no"), ("control_blocked", "yes")),
                GAP,
            ),
            Break(
                "禁掉 BSL 侧之后搬运树起不来",
                (("standalone_ok", "no"), ("control_blocked", "yes")),
                GAP,
            ),
            Break(
                "闸门拦不住 BSL 侧，这一遍什么都没验证",
                (("control_blocked", "no"), ("standalone_exit", "1")),
                GAP,
            ),
        ),
        repair={
            "reg_inside": "0",
            "prov_inside": "0",
            "crossings": "0",
            "control_blocked": "yes",
            "standalone_ok": "yes",
            "standalone_exit": "0",
            "imported_modules": "3",
            "standalone_failed": "(absent)",
        },
    ),
    Probe(
        item="AC-16|08",
        expects="LICENSE 为 BSL 1.1",
        summary="LICENSE 首行逐字 BSL 1.1 + 四个必填条款齐备，README 与数据权利登记各有免责正文",
        measure=measure_ac16_08,
        judge=judge_ac16_08,
        breaks=(
            Break("LICENSE 首行不是 BSL 1.1", (("license_line1", "Apache License 2.0"),), GAP),
            Break("BSL 四条款少一个（Change License 没写）", (("bsl_fields", "3"),), GAP),
            Break("README 没有免责声明小节", (("disclaimer_heading", "no"),), GAP),
            Break(
                "README 有标题但正文没写免责那句",
                (("disclaimer_heading", "yes"), ("disclaimer_body_ok", "no")),
                GAP,
            ),
            Break("数据权利登记里没有免责声明小节", (("registry_disclaimer", "no"),), GAP),
        ),
        repair={
            "license_line1": "Business Source License 1.1",
            "bsl_fields": "4",
            "missing_fields": "-",
            "disclaimer_heading": "yes",
            "disclaimer_body_ok": "yes",
            "registry_disclaimer": "yes",
            "registry_body_ok": "yes",
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


def measured_readings(ctx: Context) -> tuple[dict[str, Facts], dict[str, str]]:
    """Measure every probe once, keeping a raised probe as a finding instead of a crash.

    One pass feeds the counterfacts, the ledger comparison and the face bookkeeping. That sharing
    is the whole cost argument for ``--gate-check``: two hand runs of this tool cost 495 s, while a
    pass that measures once and reads five ways costs 252 s
    (``docs/evidence/C50/census-run2-final-readings.txt``, face 3).

    Args:
        ctx: Loaded document and ledger.

    Returns:
        ``(readings, unmeasured)``: facts by item id for the probes that could be measured, and the
        failure message for each that could not. A drifting probe is left out of both, because its
        drift is already the finding and measuring it would describe a different claim.
    """
    readings: dict[str, Facts] = {}
    unmeasured: dict[str, str] = {}
    for probe in PROBES:
        if wording_drift(ctx, probe):
            continue
        try:
            readings[probe.item] = probe.measure(ctx)
        except (ProbeError, KeyError, ValueError) as exc:
            unmeasured[probe.item] = f"{probe.item}: cannot measure: {exc}"
    return readings, unmeasured


def self_test_findings(
    ctx: Context, readings: Mapping[str, Facts], unmeasured: Mapping[str, str]
) -> tuple[list[str], str]:
    """Apply every counterfact to a *clean* reading and collect the ones that do not bite.

    This checks the judges, not the measurements. Two properties have to hold per item:

    * ``proven`` is reachable -- the declared ``repair`` reading makes the judge pass. A judge
      that cannot be satisfied is how a red light becomes a permanent one.
    * every counterfact flips that clean reading back to a gap. A break applied to an item that
      is already red proves nothing, which is why the measured facts are not the start point.

    Args:
        ctx: Loaded document and ledger.
        readings: Measured facts by item id, from one shared pass.
        unmeasured: Items whose measure raised, with the message that says so.

    Returns:
        ``(findings, reading)``: one string per broken judge (empty when every judge bites), and
        the count of what was actually applied -- a gate log that says "self test passed" without
        a denominator cannot be told apart from one that checked nothing.
    """
    failures: list[str] = []
    reached = 0
    applied = 0
    for probe in PROBES:
        drift = wording_drift(ctx, probe)
        if drift:
            failures.append(drift)
            continue
        if probe.item in unmeasured:
            failures.append(unmeasured[probe.item])
            continue
        facts = readings[probe.item]
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
        reached += 1
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
            applied += 1
            mutated = dict(clean)
            mutated.update(brk.facts)
            if probe.judge(mutated).state != GAP:
                failures.append(
                    f"{probe.item} / {brk.label}: the judge still says proven, so the item is not "
                    "really gated on this face"
                )
    reading = (
        f"反事实面：{reached}/{len(PROBES)} 个探针走到了判定，"
        f"{applied} 条 break 各被施加一次、每条都要求把干净读数打回 gap"
    )
    return failures, reading


def self_test(ctx: Context) -> int:
    """Run the judges against their own counterfacts and report the ones that failed to bite."""
    readings, unmeasured = measured_readings(ctx)
    failures, reading = self_test_findings(ctx, readings, unmeasured)
    print(f"  - {reading}")
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


# --------------------------------------------------------------------------- #
# The gate pass -- one measure read five ways
# --------------------------------------------------------------------------- #


def item_of(key: str) -> str:
    """The ``AC-N|NN`` a ledger key's first two segments spell.

    Ledger keys carry the hash of the criterion's wording while probes are registered by item id,
    so the plane ratchet compares item ids: re-wording a criterion moves its key, but not the
    question of whether anything re-measures it.
    """
    parts = key.split("|")
    return "|".join(parts[:2]) if len(parts) >= 2 else key


@dataclass(frozen=True)
class FaceBook:
    """What one source pass claims about faces: who reads a moment, and which cells read nothing.

    Attributes:
        moment: Item id -> the git-moment surfaces that probe's measure reaches (empty = stable).
        unplumbed: Item ids the ledger calls proven with no probe registered to re-measure them.
    """

    moment: dict[str, list[str]]
    unplumbed: list[str]


def faces_of(entry: object) -> list[str] | None:
    """The moment faces one baseline record states, or ``None`` when that record cannot be read."""
    if not isinstance(entry, dict):
        return None
    raw = entry.get("moment_faces")
    if not isinstance(raw, list):
        return None
    return sorted(str(face) for face in raw)


def face_book(ctx: Context, source: str) -> FaceBook:
    """What ``--sync-faces`` freezes: each probe's moment faces and the cells with no plane.

    Nothing here comes from the working tree, so the same source always yields the same book.
    """
    covered = {probe.item for probe in PROBES}
    return FaceBook(
        moment={
            probe.item: sorted(probe_surfaces(probe, source))
            for probe in sorted(PROBES, key=lambda p: p.item)
        },
        unplumbed=sorted(
            item_of(key)
            for key, entry in ctx.ledger.items()
            if entry.get("state") == PROVEN and item_of(key) not in covered
        ),
    )


def read_face_baseline() -> FaceBook | None:
    """The committed face baseline as a book, or ``None`` when it is absent or malformed."""
    path = REPO_ROOT / FACES_REL
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    probes = raw.get("probes")
    cells = raw.get("proven_without_plane")
    if not isinstance(probes, dict) or not isinstance(cells, list):
        return None
    moment: dict[str, list[str]] = {}
    for item, entry in probes.items():
        faces = faces_of(entry)
        if faces is None:
            return None
        moment[str(item)] = faces
    return FaceBook(moment, sorted(str(cell) for cell in cells))


def face_diff(saved: FaceBook | None, book: FaceBook) -> tuple[list[str], str]:
    """Compare a frozen face baseline with today's recomputation, and report both.

    Two rules, one per direction of cheating:

    * The moment set is recomputed from source every run. A probe that reads ``git status`` today
      and claims to read nothing tomorrow has weakened the ledger's stale-proof protection without
      anyone saying so, so any difference is red until ``--sync-faces`` writes it down as a diff.
    * ``proven_without_plane`` may only shrink. It lists the cells nothing re-measures, and
      recording it is honest about a gap rather than pretending completeness
      (``docs/evidence/C50/census-run2-final-readings.txt``, face 7: 8 proven cells had no plane).

    Args:
        saved: The committed baseline, or ``None`` when there is nothing to compare against.
        book: Today's recomputation.

    Returns:
        ``(findings, reading)``: what is wrong, and one sentence of what was compared.
    """
    moment_count = sum(1 for faces in book.moment.values() if faces)
    reading = (
        f"面基线：{len(book.moment)} 个探针里 {moment_count} 个在读 moment 面；"
        f"proven 而无探针 {len(book.unplumbed)} 个"
        + (f"，基线 {len(saved.unplumbed)} 个" if saved else "，基线读不出来")
        + "（只降不升）"
    )
    if saved is None:
        return (
            [
                f"{FACES_REL} 缺失或形状不对：moment 面与无探针格子都没有可比的东西，"
                "跑 `--sync-faces` 生成后连同 diff 一起进账"
            ],
            reading,
        )

    moved: list[str] = []
    for item in sorted(set(saved.moment) | set(book.moment)):
        was, now = saved.moment.get(item), book.moment.get(item)
        if was is None:
            moved.append(f"{item}: 基线里没有这个探针，现在读出 {now or '稳定面'}")
        elif now is None:
            moved.append(f"{item}: 探针已经不在了，基线还留着它的面 {was or '稳定面'}")
        elif was != now:
            moved.append(f"{item}: {was or '稳定面'} -> {now or '稳定面'}")
    if moved:
        return (
            [
                f"{FACES_REL} 与源码重算的 moment 面不符：{'；'.join(moved)} —— "
                "一个读 `git status` 的探针自称只读文件，就是在没有工作树的时候替台账放行；"
                "要改就 `--sync-faces`，让这件事成为有人看过的 diff"
            ],
            reading,
        )

    grew = sorted(set(book.unplumbed) - set(saved.unplumbed))
    if grew:
        return (
            [
                f"被判 proven 却没有探针的格子变多了：{'、'.join(grew)} —— "
                "这个集合只能靠加探针来减去格子；变多只能说明 proven 是翻上去的，而不是量出来的"
            ],
            reading,
        )
    return [], reading


def reconcile_state(ledger_state: str, reading: str, faces: Sequence[str], quiet: bool) -> str:
    """Which of the five reconciliation labels one cell carries.

    ``stale-proof`` is the only red one, and deliberately the narrowest: the ledger calls the cell
    proven while its probe now reads a gap, on a face that is about the repository rather than about
    this moment. Moment faces are judged on a quiet tree and deferred on any other, because a round
    is by construction a tree with evidence in flight -- the same commit read twice said gap with
    three archives uncommitted and proven once they were committed
    (``docs/evidence/C50/moment-pair-same-head.txt``).

    Args:
        ledger_state: What the ledger records today.
        reading: What the probe's judge says now.
        faces: The moment faces this probe's measure reaches; empty means the reading is stable.
        quiet: Whether the work tree has nothing in flight.

    Returns:
        ``agrees``, ``unflipped``, ``open``, ``deferred`` or ``stale-proof``.
    """
    if ledger_state == reading:
        return "agrees"
    if reading == PROVEN:
        return "unflipped"
    if ledger_state != PROVEN:
        return "open"
    return "deferred" if faces and not quiet else "stale-proof"


def reconcile(
    ctx: Context, readings: Mapping[str, Facts], book: FaceBook, quiet: bool
) -> tuple[dict[str, list[str]], str]:
    """Compare every probe reading with the ledger and group the cells by label.

    Args:
        ctx: Loaded document and ledger.
        readings: Measured facts by item id.
        book: Today's recomputed faces, so the classification comes from source and not from the
            baseline this pass is checking.
        quiet: Whether the work tree has nothing in flight.

    Returns:
        ``(cells, reading)``: item ids grouped under each label, plus one printed sentence.
    """
    cells: dict[str, list[str]] = {
        "agrees": [],
        "unflipped": [],
        "open": [],
        "deferred": [],
        "stale-proof": [],
    }
    for probe in sorted(PROBES, key=lambda p: p.item):
        facts = readings.get(probe.item)
        if facts is None:
            continue
        label = reconcile_state(
            ctx.ledger_entry(probe.item).get("state", "?"),
            probe.judge(facts).state,
            book.moment.get(probe.item, []),
            quiet,
        )
        cells[label].append(probe.item)
    counts = ", ".join(f"{label}={len(names)}" for label, names in cells.items())
    stale = cells["stale-proof"]
    reading = (
        f"台账↔读数：{counts}（共 {sum(len(names) for names in cells.values())} 格有读数）"
        " —— 只有 stale-proof 是红灯：台账记 proven 而探针现在读出 gap，且读的那一面与这一刻无关"
        + (f"；红格子 {'、'.join(stale)}" if stale else "")
    )
    return cells, reading


def stale_cell_finding(item: str, faces: Sequence[str]) -> str:
    """The sentence naming why one cell flipped from proven back to a gap."""
    named = ", ".join(faces) if faces else "文件内容，与这一刻无关"
    return (
        f"台账把 {item} 记成 proven，探针现在读出 gap（被读的面：{named}）—— 台账翻上去之后没有"
        "任何东西再量它，这一遍就是那个东西"
    )


def verdict_lines(readings: Mapping[str, Facts]) -> list[str]:
    """One ``VERDICT`` line per reading, so a gate log says which items it really re-measured.

    Before this pass, 0 of 46 archived gate logs contained a judging plane at all
    (``docs/evidence/C50/census-run2-final-readings.txt``, face 2); the census counts these lines,
    so printing them from inside the gate is what makes that reading non-zero from now on.
    """
    lines: list[str] = []
    for probe in sorted(PROBES, key=lambda p: p.item):
        facts = readings.get(probe.item)
        if facts is None:
            continue
        verdict = probe.judge(facts)
        suffix = f" — {verdict.reason}" if verdict.reason else ""
        lines.append(f"VERDICT {probe.item}: {verdict.state}{suffix}")
    return lines


def membership_findings(ctx: Context) -> list[str]:
    """Red when this tool is not a member of ``gate:`` -- a judge nobody runs judges nothing."""
    members = gate_members(ctx.read(MAKEFILE_REL))
    if GATE_MEMBER in members:
        return []
    return [
        f"{GATE_MEMBER} 不在 {MAKEFILE_REL} 的 `gate:` 配方里（配方启动的成员 {len(members)} 个："
        f"{'、'.join(members) or '无'}）—— 「逐项显式阻断」要的是每一项都能让门禁中止，而判定成员"
        "自己先不在场"
    ]


def gate_check(ctx: Context) -> int:
    """Run this tool as a gate member: one measure pass, five finding families, one exit code.

    The five faces are the ones C50 measured as missing: nothing re-measured an item between the
    ledger flip and the commit, ``--all`` compared nothing so three stale cells exited 0, and eight
    proven cells had no plane at all. The measured cost is ~252 s of the ~8 min gate
    (``docs/evidence/C50/census-run2-final-readings.txt``, face 3).

    Args:
        ctx: Loaded document and ledger.

    Returns:
        0 when every face holds, 1 with a printed list of the ones that do not.
    """
    started = time.perf_counter()
    readings, unmeasured = measured_readings(ctx)
    wall = time.perf_counter() - started
    quiet, git_says = worktree_quiet()
    book = face_book(ctx, own_source())

    print(f"\n### gate-check  ({len(PROBES)} 个探针，一遍 measure)")
    print(
        f"  - 本遍墙钟 = {wall:.1f} s，读到事实的探针 {len(readings)}/{len(PROBES)}，"
        f"测不出来的：{', '.join(sorted(unmeasured)) or '无'}"
    )
    print(
        f"  - 工作树：{'干净' if quiet else git_says} —— worktree/index/history 面只在干净时判，"
        "否则记为 deferred 并点名"
    )
    for line in verdict_lines(readings):
        print(line)

    cells, reconcile_reading = reconcile(ctx, readings, book, quiet)
    face_findings, face_reading = face_diff(read_face_baseline(), book)
    not_a_member = membership_findings(ctx)
    judge_findings, counterfact_reading = self_test_findings(ctx, readings, unmeasured)
    findings: list[str] = []
    findings += judge_findings
    findings += [
        stale_cell_finding(item, book.moment.get(item, [])) for item in cells["stale-proof"]
    ]
    findings += face_findings
    findings += not_a_member

    print(f"  - {counterfact_reading}")
    print(f"  - {reconcile_reading}")
    if cells["deferred"]:
        print(f"  - deferred（时刻面，等工作树干净再判）：{'、'.join(cells['deferred'])}")
    print(f"  - {face_reading}")
    print(f"  - 成员自证：`{GATE_MEMBER}` 在 `gate:` 配方里 = {flag(not not_a_member)}")

    if findings:
        print("FAIL: acceptance-probe gate:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    print(
        f"OK: {len(readings)} 个条目级判定在门禁里跑了一遍（{wall:.1f} s）；判据漂字、反事实、"
        "台账↔读数、面基线、成员自证五面全过。"
    )
    return 0


def sync_faces(ctx: Context) -> int:
    """Freeze today's face book into the baseline file, for a human to review as a diff.

    Deliberately a separate flag: the baseline is what turns "this probe stopped reading the work
    tree" from a silent drift into a change someone has to look at.
    """
    book = face_book(ctx, own_source())
    payload = {
        "written_by": "python scripts/quality/acceptance_item_probe.py --sync-faces",
        "meaning": (
            "probes.AC-N|NN.moment_faces = 该探针的 measure 调用闭包里读到的 git 时刻面"
            "（worktree/index/history），空数组表示只读文件内容；proven_without_plane = "
            "台账记 proven 而没有任何探针重测它的格子（AC-N|NN），只降不升"
        ),
        "probes": {item: {"moment_faces": faces} for item, faces in book.moment.items()},
        "proven_without_plane": book.unplumbed,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path = REPO_ROOT / FACES_REL
    path.write_text(text, encoding="utf-8")
    moment = sum(1 for faces in book.moment.values() if faces)
    print(
        f"wrote {FACES_REL}：{len(book.moment)} 个探针，{moment} 个在读 moment 面，"
        f"proven 而无探针 {len(book.unplumbed)} 个"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the probes as a command-line tool."""
    parser = argparse.ArgumentParser(description="Recompute individual acceptance items")
    parser.add_argument("--list", action="store_true", help="print the probe table and exit")
    parser.add_argument("--all", action="store_true", help="run every probe")
    parser.add_argument("--item", action="append", default=[], help="run one AC-N|NN (repeatable)")
    parser.add_argument("--self-test", action="store_true", help="prove every judge bites")
    parser.add_argument(
        "--gate-check",
        action="store_true",
        help=f"run as the {GATE_MEMBER} gate member: one pass, five faces, one exit code",
    )
    parser.add_argument(
        "--sync-faces",
        action="store_true",
        help=f"rewrite {FACES_REL} from the source, for a reviewer to diff",
    )
    parser.add_argument("--json", metavar="PATH", help="also write the readings as JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.list:
        for probe in PROBES:
            print(f"{probe.item}  {probe.summary}")
        return 0
    if not (args.all or args.item or args.self_test or args.gate_check or args.sync_faces):
        parser.error("choose --all, --item AC-N|NN, --self-test, --gate-check, or --sync-faces")

    try:
        ctx = load_context()
    except (ProbeError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if args.sync_faces:
        return sync_faces(ctx)
    if args.gate_check:
        return gate_check(ctx)

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
