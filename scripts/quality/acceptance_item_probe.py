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
import collections
import fnmatch
import hashlib
import importlib.util
import inspect
import io
import json
import math
import os
import re
import shutil
import subprocess  # nosec B404  # run_argv/_run_streams/secret-scan: literal list argv, no shell
import sys
import tempfile
import time
import xml.etree.ElementTree as ET  # nosec B405  # pytest-generated private temp report
import zipfile
from contextlib import redirect_stdout, suppress
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final, Protocol, TypedDict, cast
from urllib.parse import urlsplit

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


class PortScopeAuditResult(Protocol):
    """The small result surface the AC-5|02 probe reads from the scope auditor."""

    valid: bool
    checked_paths: int
    python_paths: int
    resource_paths: int
    problems: tuple[str, ...]
    batch_field: str
    problem_summary: str
    inventory_rel: str


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
            code, out = run_argv(["git", "ls-files", "-z"], cwd=self.root)
            if code != 0:
                raise ProbeError(f"git ls-files -z failed: {out.strip()[:120]}")
            self._tracked = sorted(path for path in out.split("\x00") if path)
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


def run_argv(argv: Sequence[str], *, cwd: Path = REPO_ROOT) -> tuple[int, str]:
    """Run a literal command in the repository and return ``(exit, output)``.

    No shell, so a pattern containing a ``;`` stays a pattern. A non-zero exit is a reading,
    not an exception: several probes exist precisely to report a red tool.

    Args:
        argv: Executable and its arguments.
        cwd: Repository directory to run from; defaults to the repository root.

    Returns:
        The exit code and stdout+stderr concatenated.
    """
    try:
        proc = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
            list(argv),
            cwd=cwd,
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


def resolve_break(facts: Facts, break_facts: tuple[tuple[str, str], ...]) -> Facts:
    """Apply literal counterfacts or a strict ``*fact_key+1`` mutation.

    The relative form keeps an upper-bound counterfact one unit above the snapshot measured in
    this run. It deliberately accepts only that exact spelling and a non-negative decimal fact;
    malformed or missing references fail explicitly instead of silently becoming a literal.
    """
    mutated = dict(facts)
    for key, value in break_facts:
        if not value.startswith("*") or "+" not in value:
            mutated[key] = value
            continue
        match = re.fullmatch(r"\*([A-Za-z][A-Za-z0-9_]*)\+1", value)
        if match is None:
            raise ValueError(f"invalid relative break value for {key}: {value!r}")
        reference = match.group(1)
        referenced = facts.get(reference)
        if referenced is None:
            raise ValueError(f"relative break for {key} references missing fact {reference!r}")
        if re.fullmatch(r"[0-9]+", referenced) is None:
            raise ValueError(
                f"relative break for {key} requires a non-negative integer fact "
                f"{reference!r}, got {referenced!r}"
            )
        mutated[key] = str(int(referenced) + 1)
    return mutated


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
        if p.endswith(".py")
        and not p.startswith("docs/")
        # The ported tree moved *inside* ``opendata/``, so a root-name literal would no longer
        # exclude it: 325 MIT files would be counted as our own code. The classifier answers the
        # same question the old prefix did -- is this first-party? -- whatever the tree is called.
        and _LAYOUT.classify_path(p) != _LAYOUT.PORTED
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


#: Every spelling the document has ever used for the ported tree's root.
_PORTED_ROOT_TOKENS: Final = ("akshare/", "akshare", "opendata_http/", "opendata_http")


def _ported_bucket(bare: str) -> str:
    """Map a whitelist token naming the ported tree onto wherever it lives today.

    The item has named that root three times in its life: ``akshare/``, then ``opendata_http/``,
    then the provider package. Keeping the superseded spellings as their own buckets would leave a
    dead prefix in the whitelist while the real tree sat outside it, so every historical name
    collapses onto the current one instead of covering nothing.
    """
    for token in _PORTED_ROOT_TOKENS:
        if bare == token.rstrip("/") or bare.startswith(token):
            rest = bare[len(token) :].lstrip("/")
            return f"{PORTED_ROOT}/{rest}" if rest else f"{PORTED_ROOT}/"
    return bare


def whitelist_buckets(text: str) -> list[str]:
    """Read only complete backticked paths from the item's whitelist wording."""
    buckets: list[str] = []
    for token in BACKTICK.findall(text):
        bare = token.strip().rstrip("、,。")
        if not _is_complete_reference_path(bare):
            continue
        # The item's whitelist names the upstream root ``akshare/``; the instrument has to answer
        # "which files are the ported copy?" against wherever that copy lives today, because the
        # tree was relocated into the provider package. Reading the literal root name instead
        # would leave all 327 ported files outside the whitelist and re-litigate a decision the
        # item already closed.
        normalized = _ported_bucket(bare)
        if normalized not in buckets:
            buckets.append(normalized)
    return buckets


def _is_complete_reference_path(value: str) -> bool:
    """Accept directory paths and filename-shaped tokens, excluding concepts and commands."""
    if not value or "\\" in value or ":" in value or value.startswith("/"):
        return False
    directory = value.endswith("/")
    path_text = value[:-1] if directory else value
    path = PurePosixPath(path_text)
    if (
        not path.parts
        or path.as_posix() != path_text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        return False
    return directory or bool(path.suffix) or path.name.startswith("LICENSE-")


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


def bare_token_paths(root: Path, paths: Iterable[str]) -> tuple[set[str], list[str]]:
    """Return tracked files with a bare token and paths that could not be read."""
    pattern = re.compile(rb"\bakshare\b")
    hits: set[str] = set()
    unreadable: list[str] = []
    for rel in paths:
        path = root / rel
        if not path.is_file():
            unreadable.append(rel)
            continue
        try:
            content = path.read_bytes()
        except OSError:
            unreadable.append(rel)
            continue
        if pattern.search(content):
            hits.add(rel)
    return hits, unreadable


def reference_policy_audit(
    ctx: Context,
    *,
    actual_hit_paths: set[str],
    excluded_paths: set[str],
) -> Facts:
    """Audit path metadata, source hashes and both sides of the hit/register join."""
    rel = "docs/quality/akshare-reference-allowlist.json"
    raw: object = None
    parse_error = ""
    policy_module = script_module("scripts/codemod/verify_no_akshare.py")
    try:
        raw = json.loads(ctx.read(rel))
        policy = policy_module.parse_reference_policy(raw)
    except (ProbeError, json.JSONDecodeError, policy_module.ReferencePolicyError) as exc:
        policy = None
        parse_error = str(exc)

    problems: list[str] = []
    if policy is not None:
        problems = policy_module.reference_policy_problems(
            raw,
            ctx.root,
            actual_hit_paths=actual_hit_paths,
            excluded_paths=excluded_paths,
        )
    unregistered = [problem for problem in problems if problem.startswith("unregistered hit path:")]
    stale_paths = [problem for problem in problems if problem.startswith("stale registered path:")]
    sha_mismatch = [problem for problem in problems if problem.startswith("stale sha256:")]
    metadata_problems = [
        problem
        for problem in problems
        if problem not in unregistered
        and problem not in stale_paths
        and problem not in sha_mismatch
    ]
    entries = raw.get("entries") if isinstance(raw, dict) else None
    entry_count = len(entries) if isinstance(entries, list) else 0
    exception_count = (
        sum(len(entry.ast_exceptions) for entry in policy.entries) if policy is not None else 0
    )
    invalid = policy is None or bool(problems)
    return {
        "policy_valid": flag(not invalid),
        "policy_error": (parse_error or "; ".join(problems))[:180] or "-",
        "policy_entries": count(entry_count),
        "metadata_exceptions": count(exception_count),
        "metadata_expected": count(len(policy_module.EXPECTED_METADATA_EXCEPTIONS)),
        "unregistered": count(len(unregistered)),
        "unregistered_sample": ", ".join(
            problem.removeprefix("unregistered hit path: ") for problem in unregistered[:10]
        ),
        "stale_paths": count(len(stale_paths)),
        "sha_mismatch": count(len(sha_mismatch)),
        "metadata_invalid": count(int(policy is None or bool(metadata_problems))),
    }


def measure_ac1_03(ctx: Context) -> Facts:
    """Ask where the bare word ``akshare`` still appears and whether the item lists those places."""
    item = ctx.item("AC-1|03")
    buckets = whitelist_buckets(item.text)
    tracked = ctx.tracked()
    hit_paths, unreadable = bare_token_paths(ctx.root, tracked)
    excluded = {rel for rel in hit_paths if covered_by(rel, buckets)}
    policy_facts = reference_policy_audit(
        ctx,
        actual_hit_paths=hit_paths,
        excluded_paths=excluded,
    )
    unregistered = int(policy_facts["unregistered"])
    vendored = sorted(rel for rel in hit_paths if rel.startswith(f"{PORTED_ROOT}/"))
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
        "hit_files": count(len(hit_paths)),
        "vendored": count(len(vendored)),
        "outside": count(unregistered),
        "outside_sample": policy_facts["unregistered_sample"],
        "outside_more": count(max(0, unregistered - 10)),
        "unreadable": count(len(unreadable)),
        **policy_facts,
        "integration_frozen": count(len(integration)),
        "integration_files": ", ".join(integration),
    }


def judge_ac1_03(facts: Facts) -> Verdict:
    """``AC-1|03``: old paths stay named and every other hit has a current reviewed entry."""
    ok = (
        facts["outside"] == "0"
        and facts["unreadable"] == "0"
        and facts["policy_valid"] == "yes"
        and facts["stale_paths"] == "0"
        and facts["sha_mismatch"] == "0"
        and facts["metadata_invalid"] == "0"
        and facts["metadata_exceptions"] == facts["metadata_expected"]
    )
    readings = (
        f"whitelist read back out of the item ({facts['buckets']} entries): "
        f"{facts['bucket_names']}",
        f"tracked files carrying the bare word `akshare` = {facts['hit_files']}, of which the "
        f"vendored tree = {facts['vendored']}",
        f"unregistered hits outside that whitelist = {facts['outside']} "
        f"(+{facts['outside_more']} unshown): "
        f"{facts['outside_sample'] or '-'}",
        f"path register valid = {facts['policy_valid']} ({facts['policy_entries']} entries; "
        f"stale paths={facts['stale_paths']}, stale SHA={facts['sha_mismatch']}, "
        f"invalid metadata/schema={facts['metadata_invalid']}, "
        f"unreadable tracked files={facts['unreadable']})",
        f"exact AST metadata exceptions = {facts['metadata_exceptions']}"
        f"/{facts['metadata_expected']}",
        f"AC-16's frozen integration-layer findings = {facts['integration_frozen']} "
        f"({facts['integration_files']})",
    )
    reasons: list[str] = []
    if facts["outside"] != "0":
        reasons.append(f"{facts['outside']} unregistered hit path(s) remain")
    if facts["unreadable"] != "0":
        reasons.append(f"{facts['unreadable']} tracked path(s) could not be read")
    if facts["policy_valid"] != "yes":
        reasons.append(f"path register is invalid ({facts['policy_error']})")
    if facts["stale_paths"] != "0":
        reasons.append(f"{facts['stale_paths']} registered path(s) are no longer current hits")
    if facts["sha_mismatch"] != "0":
        reasons.append(f"{facts['sha_mismatch']} registered path SHA value(s) are stale")
    if facts["metadata_invalid"] != "0":
        reasons.append("path register metadata is missing or invalid")
    if facts["metadata_exceptions"] != facts["metadata_expected"]:
        reasons.append(
            f"AST exception bindings differ from the scanner's {facts['metadata_expected']} "
            "approved contexts"
        )
    reason = "; ".join(reasons) if not ok else ""
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
    """Read four database-name surfaces and validate saved C65 runtime evidence."""
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
    root_entry = str(ctx.root.resolve())
    inserted_root = root_entry not in sys.path
    if inserted_root:
        sys.path.insert(0, root_entry)
    try:
        from scripts.quality.runtime_stack_evidence import validate as validate_runtime_stack

        runtime_result = validate_runtime_stack(ctx.root)
    finally:
        if inserted_root:
            with suppress(ValueError):
                sys.path.remove(root_entry)
    return {
        **surfaces,
        "consistent": consistent,
        "runtime": runtime_result.state,
        "runtime_valid": flag(runtime_result.valid),
        "runtime_issue_count": count(len(runtime_result.issues)),
        "runtime_issue_summary": ",".join(issue.code for issue in runtime_result.issues[:8])
        or "none",
        "runtime_endpoint": runtime_result.facts.get("endpoint", ""),
        "runtime_image": runtime_result.facts.get("image_id", ""),
        "runtime_source": runtime_result.facts.get("source_identity", ""),
        "runtime_health_database": runtime_result.facts.get("health_database", "unknown"),
        "runtime_static_asset_count": runtime_result.facts.get("static_asset_count", "0"),
        "runtime_frontend_identity": runtime_result.facts.get("frontend_identity", "unverified"),
    }


def judge_ac1_05(facts: Facts) -> Verdict:
    """``AC-1|05``: the names agree in four places -- and the stack serving is a second claim."""
    static_ok = facts["consistent"] == "yes"
    runtime_ok = (
        facts["runtime"] == "validated"
        and facts["runtime_valid"] == "yes"
        and facts["runtime_issue_count"] == "0"
    )
    ok = static_ok and runtime_ok
    expected = f"{MAIN_DB} / {WAREHOUSE_DB}"
    readings = (
        f"config.py defaults, .env.example, docker-compose.yml, init.sql all read {expected} = "
        f"{facts['consistent']}",
        f"  config.py = {facts['config']}",
        f"  .env.example = {facts['env_example']}",
        f"  docker-compose.yml = {facts['compose']}",
        f"  init.sql = {facts['init_sql']}",
        f"backend start / frontend login / GET /health = {facts['runtime']} "
        f"(evidence issues {facts['runtime_issue_count']}: {facts['runtime_issue_summary']})",
        f"  isolated endpoint = {facts['runtime_endpoint']}; image/source = "
        f"{facts['runtime_image']} / {facts['runtime_source']}",
        f"  database = {facts['runtime_health_database']}; static assets = "
        f"{facts['runtime_static_asset_count']}; frontend identity = "
        f"{facts['runtime_frontend_identity']}",
    )
    reason = (
        ""
        if ok
        else "the four configuration surfaces are half the item; the other half requires current "
        "apply evidence for the isolated stack, a healthy connected database, validated static "
        "assets, and authenticated dashboard/catalog pages. The runtime reading names any failed "
        "or missing evidence"
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


def _parse_rights_registration_table(text: str) -> tuple[bool, list[str], list[dict[str, str]]]:
    """Parse only the single, well-formed registration table in section 1."""
    lines = text.splitlines()
    section_heads = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"##\s+1\.\s*登记表\s*", line.strip())
    ]
    if len(section_heads) != 1:
        return False, [], []

    start = section_heads[0] + 1
    end = next(
        (index for index in range(start, len(lines)) if re.match(r"^##\s+", lines[index])),
        len(lines),
    )
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines[start:end]:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            current.append(stripped)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    if len(blocks) != 1 or len(blocks[0]) < 3:
        return False, [], []

    def cells(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip("|").split("|")]

    header = cells(blocks[0][0])
    separator = cells(blocks[0][1])
    required = (
        "#",
        "数据源",
        "条款链接",
        "允许本项目落库",
        "允许再分发",
        "允许商业使用",
        "复核日期",
        "责任人",
    )
    if (
        len(header) != len(separator)
        or len(set(header)) != len(header)
        or any(name not in header for name in required)
        or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator)
    ):
        return False, header, []

    records: list[dict[str, str]] = []
    row_ids: set[str] = set()
    for line in blocks[0][2:]:
        values = cells(line)
        if (
            len(values) != len(header)
            or not re.fullmatch(r"\d+", values[0])
            or values[0] in row_ids
        ):
            return False, header, []
        row_ids.add(values[0])
        records.append(dict(zip(header, values, strict=True)))
    return bool(records), header, records


def measure_ac1_08(ctx: Context) -> Facts:
    """Measure only the section-1 registration table and its required fields."""
    try:
        text = ctx.read("docs/data-rights-registry.md")
    except ProbeError:
        text = ""
    table_valid, cells, records = _parse_rights_registration_table(text)
    if not table_valid:
        records = []

    def unspecified(value: str) -> bool:
        normalized = value.strip().casefold()
        if normalized in {"", "-", "—", "n/a", "na", "none", "tbd", "unknown"}:
            return True
        return any(
            marker in normalized
            for marker in ("待确认", "未指定", "未填写", "待分配", "unspecified")
        )

    def has_http_url(value: str) -> bool:
        if not value or re.search(r"\s", value):
            return False
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    dated = 0
    undecided = 0
    unlinked = 0
    responsible_missing = 0
    uses = ("允许本项目落库", "允许再分发", "允许商业使用")
    for record in records:
        review_date = record["复核日期"]
        with suppress(ValueError):
            dated += int(date.fromisoformat(review_date).isoformat() == review_date)
        undecided += int(any(unspecified(record[name]) for name in uses))
        unlinked += int(not has_http_url(record["条款链接"]))
        responsible_missing += int(unspecified(record["责任人"]))

    named = {record["数据源"] for record in records}
    inventory_text = ctx.read(RIGHTS_INVENTORY)
    cited: set[str] = set()
    for line in inventory_text.splitlines():
        found = re.match(r"\s+rights_rows:\s*\[(.*)\]", line)
        if found:
            cited |= {c.strip().strip("'\"") for c in found.group(1).split(",") if c.strip()}
    known = [row for row in named if row]
    uncovered = sorted(c for c in cited if not any(c in row or row.startswith(c) for row in known))
    return {
        "table_valid": "yes" if table_valid else "no",
        "rows": count(len(records)),
        "columns": ", ".join(cells),
        "dated": count(dated),
        "undecided": count(undecided),
        "unlinked": count(unlinked),
        "responsible_missing": count(responsible_missing),
        "cited": count(len(cited)),
        "uncovered": count(len(uncovered)),
        "uncovered_sample": ", ".join(uncovered[:6]),
    }


def judge_ac1_08(facts: Facts) -> Verdict:
    """``AC-1|08``: section 1 is complete for all cited sources and names reviewers."""
    ok = (
        facts["table_valid"] == "yes"
        and int(facts["rows"]) > 0
        and facts["dated"] == facts["rows"]
        and facts["undecided"] == "0"
        and facts["unlinked"] == "0"
        and facts["responsible_missing"] == "0"
        and facts["uncovered"] == "0"
    )
    readings = (
        f"§1 table valid = {facts['table_valid']}; rows = {facts['rows']}; "
        f"columns = {facts['columns']}",
        f"rows carrying a real 复核日期 = {facts['dated']}/{facts['rows']}",
        f"rows still answering a permitted use with 待确认 = {facts['undecided']}/{facts['rows']}",
        f"rows whose 条款链接 is prose instead of a URL = {facts['unlinked']}/{facts['rows']}",
        f"rows with an empty or unspecified 责任人 = "
        f"{facts['responsible_missing']}/{facts['rows']}",
        f"registry rows cited by {RIGHTS_INVENTORY} = {facts['cited']}, citations with no row = "
        f"{facts['uncovered']}"
        + (f" ({facts['uncovered_sample']})" if facts["uncovered_sample"] else ""),
    )
    reason = (
        "§1 is missing, malformed, or empty"
        if facts["table_valid"] != "yes"
        else "one or more source rows lacks a complete clause URL, permitted-use decision, "
        "review date, responsible party, or cited-source match"
        if not ok
        else ""
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def classify_secret_paths(paths: Iterable[str]) -> tuple[list[str], list[str], list[str]]:
    """Return forbidden env files, non-root templates, and forbidden generated paths."""
    env_files: list[str] = []
    templates: list[str] = []
    generated: list[str] = []
    for path in paths:
        parts = Path(path).parts
        name = parts[-1] if parts else ""
        if name == ".env.example":
            templates.append(path)
        elif re.match(r"^\.env(?:$|\.)", name):
            env_files.append(path)
        if ".idea" in parts or name.endswith(".pid"):
            generated.append(path)
    return sorted(env_files), sorted(templates), sorted(generated)


def config_has_no_global_path_exemption(config: str) -> bool:
    """Require the scanner config to leave every path, including `.env.example`, eligible."""
    try:
        parsed = tomllib.loads(config)
    except tomllib.TOMLDecodeError:
        return False
    allowlist = parsed.get("allowlist")
    if allowlist is None:
        return True
    if not isinstance(allowlist, dict):
        return False
    paths = allowlist.get("paths", [])
    return isinstance(paths, list) and not paths


def _safe_finding_summary(records: object) -> tuple[int, str]:
    """Render only count and rule/file/line; never serialize match or secret fields."""
    if not isinstance(records, list):
        return 0, "report-shape-invalid"
    locations: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        rule = record.get("RuleID")
        path = record.get("File")
        line = record.get("StartLine")
        if isinstance(rule, str) and isinstance(path, str) and isinstance(line, int):
            locations.append(f"{rule}@{path}:{line}")
    return len(records), ", ".join(locations[:10]) or "-"


def scan_env_template(
    template_text: str, config: str, *, tool: str = "gitleaks"
) -> tuple[int, str]:
    """Run a redacted no-git scan of only a temporary copy of the env template."""
    if not config_has_no_global_path_exemption(config):
        return 2, "global-path-exemption-present-or-config-invalid"
    with tempfile.TemporaryDirectory(prefix="opendata-env-template-scan-") as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        (source / ".env.example").write_text(template_text, encoding="utf-8")
        config_path = root / ".gitleaks.toml"
        config_path.write_text(config, encoding="utf-8")
        report_path = root / "report.json"
        argv = [
            tool,
            "detect",
            "--no-git",
            "--source",
            str(source),
            "--config",
            str(config_path),
            "--redact",
            "--report-format",
            "json",
            "--report-path",
            str(report_path),
        ]
        try:
            result = subprocess.run(  # noqa: S603 -- argv uses only pinned scanner config.
                argv,
                capture_output=True,
                text=True,
                check=False,
            )  # nosec B603 -- fixed argv; no shell and only a repository-pinned scanner.
        except FileNotFoundError:
            return 127, "scanner-not-installed"
        if not report_path.is_file():
            return result.returncode or 2, "report-missing"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return result.returncode or 2, "report-invalid"
        finding_count, locations = _safe_finding_summary(report)
        return result.returncode, f"findings={finding_count}; locations={locations}"


def gitleaks_leak_count(output: str) -> str:
    """The scanner's own leak count, read off the line it prints for it.

    The pinned ``scan_argv`` writes no report file, so this log line is the only number the run
    itself produces. Counting ``RuleID:`` blocks instead invents a clean zero next to a failing
    exit: 441 history findings read as ``findings=0`` on the AC-1|09 face while the scan was red,
    which is a machine-readable echo that contradicts its own verdict.
    """
    found = re.search(r"leaks found:\s*(\d+)", output)
    if found:
        return found.group(1)
    if "no leaks found" in output:
        return "0"
    return "(unreadable)"


def redacted_gitleaks_summary(output: str, exit_code: int) -> str:
    """Extract safe location metadata without echoing finding text or secret values."""
    rule = "unknown-rule"
    path = "unknown-file"
    line = "?"
    locations: list[str] = []
    for text in output.splitlines():
        found_rule = re.match(r"^\s*RuleID:\s*(.+?)\s*$", text)
        found_path = re.match(r"^\s*File:\s*(.+?)\s*$", text)
        found_line = re.match(r"^\s*(?:StartLine|Line):\s*(\d+)\s*$", text)
        if found_rule:
            if rule != "unknown-rule" or path != "unknown-file" or line != "?":
                locations.append(f"{rule}@{path}:{line}")
            rule, path, line = found_rule.group(1), "unknown-file", "?"
        if found_path:
            path = found_path.group(1)
        if found_line:
            line = found_line.group(1)
    if rule != "unknown-rule" or path != "unknown-file" or line != "?":
        locations.append(f"{rule}@{path}:{line}")
    return (
        f"exit={exit_code}; leaks={gitleaks_leak_count(output)}; "
        f"locations={', '.join(locations[:10]) or '-'}"
    )


GENERIC_API_KEY_ALLOWED_REGEXES: Final = (
    r"^[A-Z0-9]{1,12}(\.[A-Z0-9_]{1,14}){3,}$",
    r"^API_KEY_FAILURE_DELAY_SECONDS=0\.05$",
    # C75: an exact literal, not a shape -- the only silenced value in the tree is the engine
    # field assignment `rows_pointer=refRates` that generic-api-key read off the word `keys`.
    r"^rows_pointer=refRates$",
    # C75: the eleven H.15 maturity series identifiers opendata declares as ColumnSpec.source_key.
    # The alternation enumerates those eleven ids one by one, so `source_key="<a real key>"` stays
    # reported; docs/evidence/C75/gitleaks-counterfactual.py measures that boundary both ways.
    r"^RIFLGFC(M01|M03|M06|Y01|Y02|Y03|Y05|Y07|Y10|Y20|Y30)_N\.B$",
)


def public_gitleaks_rule_shapes(rule_map: dict[str, Any]) -> bool:
    """Accept only the two reviewed public-rule patterns, with no broadened additions."""
    if len(rule_map) != 2 or set(rule_map) != {"curl-auth-header", "generic-api-key"}:
        return False
    curl_rule = rule_map.get("curl-auth-header")
    api_rule = rule_map.get("generic-api-key")
    if not isinstance(curl_rule, dict) or not isinstance(api_rule, dict):
        return False
    curl_allow = curl_rule.get("allowlist")
    api_allow = api_rule.get("allowlist")
    if not isinstance(curl_allow, dict) or not isinstance(api_allow, dict):
        return False
    return (
        curl_allow.get("paths") == [r"\.md$"]
        and api_allow.get("regexTarget") == "secret"
        and api_allow.get("regexes") == list(GENERIC_API_KEY_ALLOWED_REGEXES)
    )


def measure_ac1_09(ctx: Context) -> Facts:
    """Split tracked paths, exact allowlist shape and actual scanner runs."""
    tracked = ctx.tracked()
    literal = [p for p in tracked if re.search(r"\.env|\.idea|\.pid", p)]
    env_files, templates, generated = classify_secret_paths(tracked)
    config = ctx.read(".gitleaks.toml")
    parsed = tomllib.loads(config)
    allowlist = parsed.get("allowlist", {})
    allow_paths = allowlist.get("paths", []) if isinstance(allowlist, dict) else []
    raw_rules = parsed.get("rules", [])
    rule_blocks = raw_rules if isinstance(raw_rules, list) else []
    rule_map = {
        rule.get("id"): rule
        for rule in rule_blocks
        if isinstance(rule, dict) and isinstance(rule.get("id"), str)
    }
    global_paths = [str(path) for path in allow_paths] if isinstance(allow_paths, list) else []
    exact_paths = not global_paths
    rule_shapes = public_gitleaks_rule_shapes(cast("dict[str, Any]", rule_map))
    # The audit names these files by the path they carried when a person read them. The tree has
    # since moved into the provider package, so the credential-literal grep runs against the
    # current identity of the same five files, and a mapping that no longer resolves on disk is
    # counted: a grep that silently skips absent paths measures nothing, which is how this face
    # read green while the relocation was in flight.
    reviewed = (
        "opendata_http/stock/cons.py",
        "opendata_http/bond/bond_convert.py",
        "opendata_http/bond/bond_china_money.py",
        "opendata_http/futures/futures_hf_em.py",
        "opendata_http/option/option_em.py",
    )
    files = tuple(_LAYOUT.historical_identity(name) for name in reviewed)
    audit = ctx.read("docs/evidence/A0/secret-audit.txt")
    registered = [f for f in files if f.removeprefix(f"{PORTED_ROOT}/") in audit]
    present = [f for f in files if (ctx.root / f).is_file()]
    cred_shape = re.compile(
        r"(?:token|api_?key|password|pwd|secret)[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_.\-]{16,}[\"']",
        re.IGNORECASE,
    )
    live = grep_files(present, cred_shape)
    code, out = run_argv([sys.executable, "scripts/quality/secret_scan_check.py"])
    template_text = ctx.read(".env.example") if (ctx.root / ".env.example").is_file() else ""
    template_config_ok = config_has_no_global_path_exemption(config)
    manifest = json.loads(ctx.read("docs/quality/secret-scan.json"))
    scan_argv = manifest.get("scan_argv", [])
    tool = str(scan_argv[0]) if isinstance(scan_argv, list) and scan_argv else "gitleaks"
    template_rc, template_summary = scan_env_template(template_text, config, tool=tool)
    return {
        "literal": count(len(literal)),
        "literal_sample": ", ".join(literal[:5]),
        "strict": count(len(env_files) + len(generated)),
        "env_paths": count(len(env_files)),
        "env_sample": ", ".join(env_files[:5]),
        "template_paths": count(len(templates)),
        "template_names": ", ".join(templates),
        "generated_paths": count(len(generated)),
        "generated_sample": ", ".join(generated[:5]),
        "allow_paths": count(len(global_paths)),
        "allow_names": ", ".join(global_paths),
        "exact_paths": flag(exact_paths),
        "non_template": count(len(global_paths)),
        "non_template_names": ", ".join(global_paths),
        "rule_blocks": count(len(rule_blocks)),
        "rule_shapes": flag(rule_shapes),
        "registered": count(len(registered)),
        "upstream_files": count(len(files)),
        "upstream_present": count(len(present)),
        "upstream_absent_names": ", ".join(sorted(set(files) - set(present))[:5]),
        "live_shapes": count(len(live)),
        "gitleaks_rc": count(code),
        "scan_leaks": gitleaks_leak_count(out),
        "secret_check_line": redacted_gitleaks_summary(out, code),
        "template_config_ok": flag(template_config_ok),
        "template_scan_rc": count(template_rc),
        "template_scan_summary": template_summary,
    }


def judge_ac1_09(facts: Facts) -> Verdict:
    """``AC-1|09``: real env/IDE/pid paths fail, and the sole template is scanned."""
    ok = (
        facts["strict"] == "0"
        and facts["template_paths"] == "1"
        and facts["template_names"] == ".env.example"
        and facts["exact_paths"] == "yes"
        and facts["allow_paths"] == "0"
        and facts["non_template"] == "0"
        and facts["rule_blocks"] == "2"
        and facts["rule_shapes"] == "yes"
        and facts["registered"] == facts["upstream_files"]
        and facts["upstream_present"] == facts["upstream_files"]
        and facts["live_shapes"] == "0"
        and facts["gitleaks_rc"] == "0"
        and facts["scan_leaks"] == "0"
        and facts["template_config_ok"] == "yes"
        and facts["template_scan_rc"] == "0"
    )
    readings = (
        f"legacy path grep finds {facts['literal']} paths because the allowed template is named; "
        f"real `.env`/`.env.*`, `.idea` components, or `.pid` files = {facts['strict']}"
        + (
            f" ({facts['env_sample']}; {facts['generated_sample']})"
            if facts["strict"] != "0"
            else ""
        ),
        f"tracked env templates = {facts['template_paths']} ({facts['template_names']}); "
        f"global path exemptions = {facts['allow_paths']} (none required = {facts['exact_paths']})",
        f"global path exemption names = {facts['non_template']} "
        + (f"({facts['non_template_names']})" if facts["non_template_names"] else "-"),
        f"per-rule allowlist blocks = {facts['rule_blocks']}; "
        f"exact public-rule exceptions preserved = "
        f"{facts['rule_shapes']}",
        "upstream credential files registered in docs/evidence/A0/secret-audit.txt = "
        f"{facts['registered']}/{facts['upstream_files']}; mapped identities still on disk = "
        f"{facts['upstream_present']}"
        + (
            f" (missing: {facts['upstream_absent_names']})"
            if facts["upstream_absent_names"]
            else ""
        )
        + f"; cred-shaped literals left in them = {facts['live_shapes']}",
        f"$ python scripts/quality/secret_scan_check.py -> exit {facts['gitleaks_rc']} "
        f"(scanner-reported leaks = {facts['scan_leaks']}): {facts['secret_check_line']}",
        f"isolated `.env.example` scan (unchanged config, no path exclusions) -> "
        f"exit {facts['template_scan_rc']}: {facts['template_scan_summary']}",
    )
    reason = (
        "a real environment/IDE/pid path, stale or broadened allowlist, changed public rule "
        "exception, unregistered or missing upstream file, a leak the history scanner counts, or "
        "an unscanned/credential-bearing template remains"
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
    # The synthetic name must be a dotless, slash-free identifier. ``ModuleSpec.parent`` is
    # ``name.rpartition(".")[0]``, and ``__package__`` is that parent, so naming the module after
    # its path (``...verify_no_akshare.py``) hands a top-level script a non-empty ``__package__``.
    # A script that branches on ``if __package__:`` to choose between package-relative imports and
    # ``sys.path.insert(0, REPO_ROOT)`` then takes the package branch and dies with
    # ``ModuleNotFoundError: No module named 'scripts.quality'`` -- under ``make``, whose
    # ``sys.path[0]`` is ``scripts/quality``, not the repository root.
    spec = importlib.util.spec_from_file_location(
        f"opendata_script_{rel.replace('/', '_').removesuffix('.py')}",
        path,
    )
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

#: The ported tree's one and only identity source. ``source_layout`` is what the ratchet, the
#: ledger checker and the codemod already agree on; a probe that spelled the root a third way
#: would read a directory that no longer exists and call the silence a passing grade.
_LAYOUT: Final = script_module("scripts/quality/source_layout.py")
PORTED_ROOT: Final = str(_LAYOUT.VENDOR_ROOT)
#: The dotted form, i.e. what an interpreter has to import and what an ``import a.b.c`` binds.
PORTED_MODULE: Final = PORTED_ROOT.replace("/", ".")
#: The name ``import opendata.data.providers.akshare._vendor`` puts in the caller's namespace.
PORTED_BIND_NAME: Final = PORTED_MODULE.rpartition(".")[2]


def ported_module_identity(dotted: str) -> str:
    """Translate a historical dotted module to the dotted name it has today.

    A criterion that names a ported module by its pre-relocation dotted path stops being
    satisfiable once the file moves: the dispatch table resolves to the new name, so the equality
    can never hold and the item's gap could not be closed by any amount of real work.
    """
    canonical: str = _LAYOUT.historical_identity(dotted.replace(".", "/"))
    return canonical.replace("/", ".")


def current_target_identity(target: str) -> str:
    """Re-express a recorded ``module:function`` binding in today's module names.

    Only the archive side of the comparison is translated: the live dispatch table has to name an
    importable module *as the tree stands now*, so a table that still carries a pre-relocation
    path reads as the gap it is. A run that really bound a different leg still lands on a
    different module, and a mapping the layout cannot name raises rather than passing quietly.
    """
    module, separator, function = target.partition(":")
    if not separator:
        return target
    return f"{ported_module_identity(module)}:{function}"


#: The frozen ceiling, the replay archive, the ported tree's own manifest, and the licence
#: document that is supposed to name every deviation inside it.
RATCHET_SNAPSHOT: Final = "docs/quality/ratchet.json"
PORT_REPORT_ARCHIVE: Final = "docs/port-report.md"
UPSTREAM_LOCK: Final = f"{PORTED_ROOT}/upstream.lock"
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
PORTED_PROBE_PATH: Final = f"{PORTED_ROOT}/stock/cons.py"


def run_split_stderr(argv: Sequence[str]) -> tuple[int, str, str]:
    """Run a literal command and return ``(exit, stdout, stderr)``.

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
    return proc.returncode, proc.stdout, proc.stderr


def run_split(argv: Sequence[str]) -> tuple[int, str]:
    """Run a literal command and return ``(exit, stdout)``, keeping stderr out of the payload."""
    code, out, _stderr = run_split_stderr(argv)
    return code, out


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


def first_party_py_files_under(root: str) -> list[str]:
    """The ``.py`` paths under one root that the layout authority calls first-party.

    The vendored tree nests *inside* ``opendata/``, so a plain walk of the selfdev roots counts
    every ported file as first-party debt. Those files are AC-17|05's population, and leaving them
    in this one pins ``ruff_dark`` and ``mypy_dark_outside_legacy`` at 325 permanently: the red
    would read as "an exclude swallowed a first-party root" while the real cause is that two
    different populations were added together, and no amount of real work turns it green.
    """
    return [
        name for name in py_files_under(root) if _LAYOUT.classify_path(name) == _LAYOUT.FIRST_PARTY
    ]


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


def _census_key(name: str) -> str:
    """One census key in current source identity, or itself when it has no single identity."""
    try:
        identity: str = _LAYOUT.historical_identity(name)
    except _LAYOUT.SourceLayoutError:
        return name
    return identity


def canonical_counts(counts: dict[str, int]) -> dict[str, int]:
    """Snapshot census keys expressed in today's source identity.

    The ported root was renamed under recorded controlled events (upstream ``akshare`` ->
    ``opendata_http`` in A2, then into the vendor package in C66). Comparing census keys by literal
    name made each rename read as a scan scope that quietly disappeared, which is the opposite of
    what the face is for; mapping through the layout authority's own history function keeps a
    renamed root matched to the key that replaced it, while a root that is genuinely gone still has
    no counterpart.
    """
    return {_census_key(name): value for name, value in counts.items()}


def census_of(payload: dict[str, object], roots: Sequence[str]) -> int:
    """How many files a snapshot says were scanned under ``roots`` -- the growth a raise needs."""
    counts = canonical_counts(section_ints(payload, "file_counts"))
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
    """Census keys that left the list with nowhere for their files to have gone.

    Two relations must fail before a key is called gone. Names are matched under
    :func:`canonical_counts`, so a root renamed by a recorded controlled event lines up with the
    key that replaced it. And a key with no single identity -- the Fuyao directory, whose nine
    ``.py`` files were distributed into the THS provider instead of moving as a block -- is
    forgiven when the newer census counts at least as many files as it lost: the population is
    still measured, it only changed parent. An exclude that swallows a root leaves the total
    short by exactly that root's count, which is the contraction this face exists to catch.
    """
    canon_new, canon_old = canonical_counts(newer), canonical_counts(older)
    kept = sum(value for key, value in canon_old.items() if key in canon_new)
    reappearing = sum(canon_new.values()) - kept
    return tuple(
        key for key in sorted(set(canon_old) - set(canon_new)) if canon_old[key] > reappearing
    )


def scope_reparented(newer: dict[str, int], older: dict[str, int]) -> tuple[str, ...]:
    """Keys that left the census list while their file count reappeared under another root.

    Reported rather than quietly forgiven: a re-parenting is still a scope change a reader of the
    snapshot should see, and printing it is what keeps "the files moved" distinguishable from
    "the files stopped being measured" in the next round's reading.
    """
    gone = set(scope_vanished(newer, older))
    canon_new, canon_old = canonical_counts(newer), canonical_counts(older)
    return tuple(key for key in sorted(set(canon_old) - set(canon_new)) if key not in gone)


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
    reparented: list[str] = []
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
        older_counts = canonical_counts(section_ints(older, "file_counts"))
        newer_counts = canonical_counts(section_ints(newer, "file_counts"))
        reparented += [
            f"{old_label}->{new_label} {root}({older_counts[root]})"
            for root in scope_reparented(newer_counts, older_counts)
        ]
        gone = scope_vanished(newer_counts, older_counts)
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
        "reparented": count(len(reparented)),
        "reparented_detail": "; ".join(reparented),
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

    disk_selfdev = {path for root in selfdev for path in first_party_py_files_under(root)}
    walked = ruff_walked(selfdev)
    disk_mypy = {path for root in mypy_paths for path in first_party_py_files_under(root)}
    targets = mypy_targets(mypy_paths)
    disk_bandit = {path for root in bandit_paths for path in first_party_py_files_under(root)}
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
            f"{facts['vanish_recent']}，更早历史 {facts['vanish_older']}；从名单退出但文件计数"
            f"在他处回来的 {facts['reparented']} 个；存量上限轨迹 {facts['trend']}。"
            "棘轮只比较工作区与快照，抬高上限这一步此前无人计量",
            "; ".join(
                listing
                for listing in (
                    facts["raise_detail"],
                    facts["vanish_recent_detail"],
                    facts["vanish_older_detail"],
                    facts["reparented_detail"],
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
            f"{RECENT_TRANSITIONS} 次转换消失的被测根 {facts['vanish_recent']}、"
            f"从名单退出但文件计数在他处回来的 {facts['reparented']} 个。"
            f"判据原文只要求「债务不高于棘轮快照」，故抬高只作待复核登记；轨迹 "
            f"{facts['trend']}",
            "; ".join(
                listing
                for listing in (
                    facts["flat_raise_detail"],
                    facts["vanish_older_detail"],
                    facts["reparented_detail"],
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
CATALOG_LAYOUT_REL: Final = "frontend/src/views/LayoutView.vue"
AC18_03_E2E_TITLES: Final = {
    "merged_catalog": (
        "merged catalog is the default at both catalog URLs and filters by market and dataset"
    ),
    "function_detail": (
        "catalog function drilldown reaches legacy list and an actual script detail route"
    ),
}

#: 「各域最新数据日期与滞后天数可查」：一条门的读法一个节点，缺一条就是少一问。
FRESHNESS_QUERY_NODES: Final = (
    "tests/test_data_path_aliases.py::TestPathAliasHttpBehavior::"
    "test_alias_freshness_uses_canonical_domain_and_table",
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
    ("覆盖标的数", 'label="覆盖（域总计）"', "标的", "标的", "标的"),
    ("时间范围", 'label="时间范围（域总计）"', "~", "~", "~"),
    ("各源最近更新", 'label="各源最近更新"', "已验证", "已验证", "已验证"),
    ("新鲜度", 'label="新鲜度（域总计）"', "滞后", "滞后", "滞后"),
    ("质量标记", 'label="质量（域总计）"', "未测量", "未测量", "未测量"),
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


def _normalized_pytest_file(value: str) -> str | None:
    """Return a repo-relative test file from a pytest JUnit ``file`` attribute."""
    path = Path(value.replace("\\", "/"))
    if path.is_absolute():
        try:
            return path.resolve(strict=False).relative_to(REPO_ROOT).as_posix()
        except ValueError:
            return None
    try:
        return (REPO_ROOT / path).resolve(strict=False).relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return None


def _junit_node_id(testcase: ET.Element) -> str | None:
    """Reconstruct a pytest node id only when its JUnit location is unambiguous."""
    file_name = testcase.get("file")
    class_name = testcase.get("classname")
    test_name = testcase.get("name")
    if not file_name or not class_name or not test_name:
        return None

    normalized_file = _normalized_pytest_file(file_name)
    if normalized_file is None or not normalized_file.endswith(".py"):
        return None
    module_path = Path(normalized_file).with_suffix("")
    if module_path.name == "__init__":
        module_path = module_path.parent
    module_name = module_path.as_posix().replace("/", ".")
    if class_name == module_name:
        class_path = ""
    elif class_name.startswith(f"{module_name}."):
        class_path = class_name[len(module_name) + 1 :]
    else:
        return None

    parts = [normalized_file]
    if class_path:
        parts.extend(class_path.split("."))
    parts.append(test_name)
    return "::".join(parts)


def _junit_case_status(testcase: ET.Element) -> str:
    """Map a single JUnit testcase to the conservative status used by the probe."""
    child_tags = {child.tag.rsplit("}", 1)[-1] for child in testcase}
    if "error" in child_tags:
        return "error"
    if "failure" in child_tags:
        return "failed"
    if "skipped" in child_tags:
        return "skipped"
    return "passed"


def _junit_node_outcomes(nodes: Sequence[str], report_path: Path, exit_code: int) -> dict[str, str]:
    """Match requested node ids to exact JUnit cases; ambiguity never passes."""
    if exit_code != 0:
        return dict.fromkeys(nodes, f"runner-exit={exit_code}")
    try:
        # The report is created by the local pytest process in this private temp directory.
        root = ET.parse(report_path).getroot()  # noqa: S314  # nosec B314  # probe's own junit
    except (ET.ParseError, OSError):
        return dict.fromkeys(nodes, "missing-report")

    case_nodes: list[tuple[str, ET.Element]] = []
    for testcase in root.iter():
        if testcase.tag.rsplit("}", 1)[-1] != "testcase":
            continue
        node_id = _junit_node_id(testcase)
        if node_id is not None:
            case_nodes.append((node_id, testcase))

    request_counts: dict[str, int] = {}
    for node in nodes:
        request_counts[node] = request_counts.get(node, 0) + 1
    requested_ids = set(nodes)

    results: dict[str, str] = {}
    for node in dict.fromkeys(nodes):
        if request_counts[node] > 1:
            results[node] = "ambiguous"
            continue

        matching = [case for case_node, case in case_nodes if case_node == node]
        if len(matching) > 1:
            results[node] = "ambiguous"
            continue

        requested_parent, separator, requested_name = node.rpartition("::")
        if not separator:
            results[node] = "ambiguous"
            continue
        requested_family = requested_name.partition("[")[0]
        siblings = [
            case_node
            for case_node, _case in case_nodes
            if case_node.rpartition("::")[0] == requested_parent
            and case_node.rpartition("::")[2].partition("[")[0] == requested_family
            and case_node != node
            and case_node not in requested_ids
        ]
        if siblings:
            results[node] = "ambiguous"
        elif len(matching) == 1:
            results[node] = _junit_case_status(matching[0])
        else:
            results[node] = "missing"
    return results


def outcomes(nodes: Sequence[str]) -> dict[str, str]:
    """Run one pytest process for the node group and key exact results by test name."""
    if not nodes:
        return {}

    unique_nodes = list(dict.fromkeys(nodes))
    output_key_counts: dict[str, int] = {}
    for node in nodes:
        key = node.split("::")[-1]
        output_key_counts[key] = output_key_counts.get(key, 0) + 1

    with tempfile.TemporaryDirectory(prefix="acceptance-probe-nodes-") as temporary:
        report_path = Path(temporary) / "junit.xml"
        argv = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "--no-cov",
            "-m",
            "not e2e",
            "-o",
            "junit_family=xunit1",
            f"--junitxml={report_path}",
            *unique_nodes,
        ]
        exit_code, _output = run_argv(argv)
        node_results = _junit_node_outcomes(unique_nodes, report_path, exit_code)

    results: dict[str, str] = {}
    for node in unique_nodes:
        key = node.split("::")[-1]
        if output_key_counts[key] > 1:
            results[key] = "ambiguous"
        else:
            results[key] = node_results.get(node, "missing")
    return results


def bad_of(results: dict[str, str]) -> str:
    """The nodes that did not pass, or ``-`` when every one of them did."""
    return ", ".join(f"{name}={seen}" for name, seen in results.items() if seen != "passed") or "-"


def measure_ac18_01(ctx: Context) -> Facts:
    """Run the freshness door and the alert faces, then read how the scheduled job wires them."""
    query = outcomes(FRESHNESS_QUERY_NODES)
    alert = outcomes(FRESHNESS_ALERT_NODES)
    api = ctx.read(DATA_QUERY_REL)
    public_door = function_body(api, "domain_freshness")
    door = function_body(api, "_domain_freshness")
    jobs = ctx.read(PIPELINE_JOBS_REL)
    return {
        "query_runs": count(len(query)),
        "query_passed": count(sum(1 for seen in query.values() if seen == "passed")),
        "query_bad": bad_of(query),
        "alert_runs": count(len(alert)),
        "alert_passed": count(sum(1 for seen in alert.values() if seen == "passed")),
        "alert_bad": bad_of(alert),
        "door_route": flag("/domains/{domain}/freshness" in api),
        "door_delegates": flag("return await _domain_freshness(" in public_door),
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
        and facts["door_delegates"] == "yes"
        and facts["door_readings"] == "yes"
        and number(facts["door_baseline"]) >= 2
        and facts["job_wired"] == "yes"
        and facts["job_broadcasts"] == "yes"
        and facts["job_executable"] == "yes"
    )
    readings = (
        f"新鲜度门 faces: {facts['query_passed']}/{facts['query_runs']} nodes passed"
        + (f"; not green: {facts['query_bad']}" if facts["query_bad"] != "-" else ""),
        f"{DATA_QUERY_REL}::domain_freshness route = {facts['door_route']}, delegates to "
        f"_domain_freshness = {facts['door_delegates']}, carries "
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


def playwright_outcomes(output: str, titles: Mapping[str, str], exit_code: int) -> dict[str, str]:
    """Read named leaf outcomes from Playwright's JSON reporter output."""
    try:
        report = json.loads(output)
    except json.JSONDecodeError:
        return dict.fromkeys(titles, f"runner-exit={exit_code}")

    found: dict[str, list[str]] = {}
    stack = list(report.get("suites", []))
    while stack:
        suite = stack.pop()
        if not isinstance(suite, dict):
            continue
        stack.extend(suite.get("suites", []))
        candidates: list[tuple[str, dict[str, object]]] = []
        for test in suite.get("tests", []):
            if not isinstance(test, dict):
                continue
            for key, title in titles.items():
                if test.get("title") == title:
                    candidates.append((key, test))
        for spec in suite.get("specs", []):
            if not isinstance(spec, dict):
                continue
            for key, title in titles.items():
                if spec.get("title") != title:
                    continue
                spec_tests = spec.get("tests", [])
                candidates.extend(
                    (key, test)
                    for test in (spec_tests if spec_tests else [spec])
                    if isinstance(test, dict)
                )
        for key, test in candidates:
            results = test.get("results", [])
            last_result = results[-1] if isinstance(results, list) and results else None
            last_status = (
                last_result.get("status") if isinstance(last_result, dict) else "missing-result"
            )
            if test.get("status") == "expected" and last_status == "passed":
                found.setdefault(key, []).append("passed")
            elif test.get("status") == "skipped":
                found.setdefault(key, []).append("skipped")
            else:
                found.setdefault(key, []).append("failed")
    return {
        key: seen[0] if len(seen) == 1 else "missing" if not seen else "ambiguous"
        for key in titles
        for seen in (found.get(key, []),)
    }


def run_catalog_e2e() -> dict[str, str]:
    """Run the two named browser cases that cover the merged catalog and function detail."""
    titles = AC18_03_E2E_TITLES
    grep = "|".join(re.escape(title) for title in titles.values())
    code, output = run_argv(
        [
            "./node_modules/.bin/playwright",
            "test",
            "e2e/scripts.spec.ts",
            "--reporter=json",
            "--workers=1",
            "--retries=0",
            "--grep",
            grep,
        ],
        cwd=REPO_ROOT / "frontend",
    )
    return playwright_outcomes(output, titles, code)


def measure_ac18_03(ctx: Context) -> Facts:
    """Run named browser cases for the merged catalog and its function-level drill-down."""
    router = ctx.read(ROUTER_REL)
    layout = ctx.read(CATALOG_LAYOUT_REL)
    merged = re.search(
        r"path: 'scripts',\s*\n\s*name: '[^']+',\s*\n\s*component: \(\) => "
        r"import\('([^']+)'\)",
        router,
    )
    catalog_nav = re.findall(r"\{\s*index:\s*'/data',\s*name:\s*t\('nav\.catalog'\)", layout)
    detail_route = re.search(
        r"path: 'scripts/:id',\s*\n\s*name: '[^']+',\s*\n\s*component: \(\) => "
        r"import\('@/views/ScriptDetailView\.vue'\)",
        router,
    )
    e2e = run_catalog_e2e()
    return {
        "page_titles": "nav.catalog -> /data" if catalog_nav else "-",
        "nav_entries": count(len(catalog_nav)),
        "route_is_catalog": flag(
            merged is not None and merged.group(1).endswith("DataCatalogView.vue")
        ),
        # This named browser case asserts the visible registered-function panel,
        # including a concrete fetcher, endpoint, and required parameter.
        "detail_in_catalog": flag(e2e["merged_catalog"] == "passed"),
        "detail_route": flag(detail_route is not None and e2e["function_detail"] == "passed"),
        "merged_e2e": e2e["merged_catalog"],
        "function_detail_e2e": e2e["function_detail"],
    }


def judge_ac18_03(facts: Facts) -> Verdict:
    """``AC-18|03``: 数据接口 must be the catalog page itself, with 函数级明细 as its drill-down."""
    ok = (
        facts["nav_entries"] == "1"
        and facts["route_is_catalog"] == "yes"
        and facts["detail_in_catalog"] == "yes"
        and facts["detail_route"] == "yes"
        and facts["merged_e2e"] == "passed"
        and facts["function_detail_e2e"] == "passed"
    )
    readings = (
        f"目录导航项 = {facts['nav_entries']} ({facts['page_titles']})",
        f"`/scripts` 是否渲染目录视图 = {facts['route_is_catalog']}",
        f"Playwright `{AC18_03_E2E_TITLES['merged_catalog']}` = {facts['merged_e2e']} "
        "（/scripts 与 /data 默认目录、市场/数据集筛选、注册函数明细面）",
        f"目录内函数明细控件及其可见用例 = {facts['detail_in_catalog']}",
        f"Playwright `{AC18_03_E2E_TITLES['function_detail']}` = "
        f"{facts['function_detail_e2e']}；真实脚本详情路由 = {facts['detail_route']}",
    )
    reason = (
        ""
        if ok
        else "合并判据尚未由这两个具名浏览器用例全部证明：分别检查 /scripts 与 /data 的目录默认页、"
        "市场和数据集筛选、目录内注册函数明细，以及旧函数列表到真实脚本详情页的下钻；"
        f"本轮结果为 merged_catalog={facts['merged_e2e']}、"
        f"function_detail={facts['function_detail_e2e']}"
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

#: 修订传播同时覆盖普通 reader 与生产 scoped reader 的窗口、None 水位及改值链路。
DWD_REVISION_NODES: Final = (
    "tests/test_dwd_merge.py::TestDwdMergeService::test_revision_of_an_existing_key_changes_the_dwd_row",
    "tests/test_dwd_merge.py::TestDwdMergeService::test_affected_keys_extend_the_merge_unit",
    "tests/test_pipeline_templates.py::TestScopedOdsReader::"
    "test_run_hook_merges_scoped_old_revision_and_excludes_other_rows",
    "tests/test_pipeline_templates.py::TestScopedOdsReader::"
    "test_multisource_run_hook_reads_only_affected_rows_when_symbol_window_is_none",
    "tests/test_pipeline_templates.py::TestScopedOdsReader::"
    "test_scoped_reader_without_a_symbol_window_uses_requested_range_and_affected_date",
    "tests/test_pipeline_templates.py::TestScopedOdsReader::"
    "test_unscoped_composite_financial_key_reads_only_the_affected_old_item",
    "tests/test_pipeline_templates.py::TestScopedOdsReader::"
    "test_unscoped_financial_indicator_key_uses_its_report_period_index",
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
PARTITION_CENSUS_REL: Final = "docs/evidence/C58/ods-face.txt"
PARTITION_APPLY_REL: Final = "docs/evidence/C57/partition-apply.txt"


def _facts_line(source: str) -> dict[str, str]:
    """Parse the ``FACTS k=v`` line a live face printed, if there is one."""
    line = next((row for row in source.splitlines() if row.startswith("FACTS ")), "")
    return dict(token.split("=", 1) for token in line.split()[1:] if "=" in token)


def partition_census_archive_reading(ctx: Context) -> str:
    """Disclose the historical C57 subset beside the later, larger C58 census."""
    horizon = ctx.read(PARTITION_FACE_REL)
    full_census = ctx.read(PARTITION_CENSUS_REL)
    horizon_facts = _facts_line(horizon)
    census_facts = _facts_line(full_census)
    horizon_round = re.search(r"^ARCHIVE_ROUND=(\S+)", horizon, re.M)
    census_round = re.search(r"^ARCHIVE_ROUND=(\S+)", full_census, re.M)
    horizon_date = re.search(r"^date:\s*(.+)$", horizon, re.M)
    census_date = re.search(r"^date:\s*(.+)$", full_census, re.M)
    return (
        f"{horizon_round.group(1) if horizon_round else 'unknown'} historical subset "
        f"({horizon_date.group(1) if horizon_date else 'date unknown'}): "
        f"tables={horizon_facts.get('tables', '(absent)')} / "
        f"partitioned={horizon_facts.get('partitioned', '(absent)')} / "
        f"gap_tables={horizon_facts.get('gap_tables', '(absent)')}; later full census "
        f"{census_round.group(1) if census_round else 'unknown'} "
        f"({census_date.group(1) if census_date else 'date unknown'}): "
        f"registered={census_facts.get('registered', '(absent)')} / "
        f"partitioned={census_facts.get('partitioned', '(absent)')} / "
        f"gap_tables={census_facts.get('gap_tables', '(absent)')}; both are archived reads, "
        "not a fresh production census"
    )


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
        "census_archives": partition_census_archive_reading(ctx),
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
        f"历史分区面（{PARTITION_FACE_REL}）：注册表 {facts['live_tables']} 张 / 已分区 "
        f"{facts['live_partitioned']} 张 / 当时年度上界缺 {facts['live_gap_tables']} 张；"
        f"档案里有分区列读数 = {facts['live_keys']}",
        facts["census_archives"],
        f"经确认的 apply 留档（{PARTITION_APPLY_REL}）存在 = {facts['applied_face']}",
    )
    reason = (
        ""
        if ok
        else "分区面两头都要有：接线（yaml 行 + kind 可执行 + 派发到真的 ``ensure``）与仓库现状"
        "（分区列点名、上界不再落后、apply 后复跑留档）。C57/C58 是历史档案，"
        "不是本轮新做的生产读数；``REORGANIZE`` 是生产仓库的 DDL，仍缺经确认的 apply 与新读数"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def exact_partition_placement_assertions(source: str) -> tuple[str, ...]:
    """Return partitions whose exact row counts are queried and asserted in the cross-year case."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()

    test_method: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "TestCrossYearWrite":
            continue
        test_method = next(
            (
                child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == "test_maintenance_extends_partitions_and_new_year_rows_land_alone"
            ),
            None,
        )
        break
    if test_method is None:
        return ()

    assigned_partitions: dict[str, str] = {}
    for ast_node in ast.walk(test_method):
        if not isinstance(ast_node, ast.Assign) or len(ast_node.targets) != 1:
            continue
        target = ast_node.targets[0]
        if not isinstance(target, ast.Name) or not isinstance(ast_node.value, ast.Call):
            continue
        scalar = ast_node.value
        if not isinstance(scalar.func, ast.Attribute) or scalar.func.attr != "scalar_one":
            continue
        execute = scalar.func.value
        if not isinstance(execute, ast.Call) or not isinstance(execute.func, ast.Attribute):
            continue
        if execute.func.attr != "execute" or not execute.args:
            continue
        text_call = execute.args[0]
        if not isinstance(text_call, ast.Call) or not isinstance(text_call.func, ast.Name):
            continue
        if text_call.func.id != "text" or len(text_call.args) != 1:
            continue
        sql_node = text_call.args[0]
        if not isinstance(sql_node, ast.Constant) or not isinstance(sql_node.value, str):
            continue
        sql = " ".join(sql_node.value.split())
        match = re.fullmatch(
            r"SELECT COUNT\(\*\) FROM `_probe_partition_maintenance` "
            r"PARTITION \((p2027|pmax)\)",
            sql,
            re.I,
        )
        if match:
            assigned_partitions[target.id] = match.group(1).lower()

    expected_rows = {"p2027": 1, "pmax": 0}
    proven: set[str] = set()
    for ast_node in ast.walk(test_method):
        if not isinstance(ast_node, ast.Assert) or not isinstance(ast_node.test, ast.Compare):
            continue
        comparison = ast_node.test
        if len(comparison.ops) != 1 or not isinstance(comparison.ops[0], ast.Eq):
            continue
        if len(comparison.comparators) != 1:
            continue
        left, right = comparison.left, comparison.comparators[0]
        if isinstance(left, ast.Name) and isinstance(right, ast.Constant):
            name, value = left.id, right.value
        elif isinstance(right, ast.Name) and isinstance(left, ast.Constant):
            name, value = right.id, left.value
        else:
            continue
        partition = assigned_partitions.get(name)
        if partition is not None and type(value) is int and value == expected_rows[partition]:
            proven.add(partition)
    return tuple(partition for partition in ("p2027", "pmax") if partition in proven)


def measure_ac8_06(ctx: Context) -> Facts:
    """The cross-year write case: does it exist, does it assert placement, has it run."""
    spec = ctx.read(PARTITION_TESTS_REL)
    face = ctx.read(PARTITION_FACE_REL)
    live = _facts_line(face)
    placements = exact_partition_placement_assertions(spec)
    return {
        "case_file": flag("_probe_partition_maintenance" in spec),
        "inserts_new_year": flag(
            "'2027-03-01'" in spec and "INSERT INTO `_probe_partition_maintenance`" in spec
        ),
        "asserts_placement": count(len(placements)),
        "placement_partitions": ",".join(placements) or "-",
        "marked": "e2e" if "@pytest.mark.e2e" in spec else "no",
        "fallback_gap": live.get("gap_tables", "(absent)"),
        "ran_live": flag("PARTITION_E2E_EXIT=0" in face),
        "census_archives": partition_census_archive_reading(ctx),
    }


def judge_ac8_06(facts: Facts) -> Verdict:
    """``AC-8|06``: a new-year row lands in its own partition instead of erroring."""
    ok = (
        facts["case_file"] == "yes"
        and facts["inserts_new_year"] == "yes"
        and facts["asserts_placement"] == "2"
        and facts["placement_partitions"] == "p2027,pmax"
        and facts["marked"] == "e2e"
        and facts["fallback_gap"] == "0"
        and facts["ran_live"] == "yes"
    )
    readings = (
        f"用例在 {PARTITION_TESTS_REL}：自建 ``_probe_*`` 表 = {facts['case_file']}，"
        f"插入新年度那一行（2027-03-01 进 probe 表）= {facts['inserts_new_year']}，"
        f"精确 placement 断言 = {facts['placement_partitions']} "
        f"({facts['asserts_placement']} 条；p2027=1、pmax=0)",
        f"标记 = {facts['marked']}（``make gate`` 不跑 e2e，所以这一面必须另留档才算跑过）",
        f"C57 历史面当时还缺年度分区的表 = {facts['fallback_gap']} 张 —— 缺的那一年会落进 "
        "``pmax``：写入不报错，但年分区形同不存在，所以「写成功」必须有落点断言",
        facts["census_archives"],
        f"本轮留档里有一次真跑 = {facts['ran_live']}",
    )
    reason = (
        ""
        if ok
        else "跨年写入这条判据要有「跑过」的证据而不是「有用例」：用例是 e2e 面（门禁不跑），"
        "且需要 fresh scoped production evidence。C57 是较早子集、C58 是较晚完整档案；"
        "两份都不是本轮 fresh production read，也没有本轮获准的用例运行留档"
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


def _ac9_07_call_chain_facts(source: str) -> Facts:
    """Read affected-key arguments from the service call graph with AST nodes."""
    no_chain = {
        "run_delegates_affected_keys": "no",
        "batch_reader_gets_keys": "no",
        "batch_merge_gets_keys": "no",
        "normal_reader_gets_keys": "no",
        "scoped_reader_gets_keys": "no",
        "reader_gets_keys": "no",
        "keys_extend_diffs": "no",
        "partition_hook_resells_keys": "no",
        "context_hook_resells_keys": "no",
        "hook_resells_keys": "no",
    }
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return no_chain
    service_classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DwdMergeService"
    ]
    if len(service_classes) != 1:
        return no_chain
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in service_classes[0].body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in {"run", "_run_batch", "run_hook", "_read_source"}:
            continue
        if node.name in methods:
            return no_chain
        methods[node.name] = node

    def self_method_calls(node: ast.AST | None, name: str) -> list[ast.Call]:
        if node is None:
            return []
        return [
            call
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
            and call.func.attr == name
        ]

    def keyword_value(call: ast.Call, name: str) -> ast.expr | None:
        matches = [keyword.value for keyword in call.keywords if keyword.arg == name]
        return matches[0] if len(matches) == 1 else None

    def is_name(node: ast.expr | None, name: str) -> bool:
        return isinstance(node, ast.Name) and node.id == name

    def collection_of(node: ast.expr | None, wrapper: str, item: str) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == wrapper
            and len(node.args) == 1
            and not node.keywords
            and is_name(node.args[0], item)
        )

    def contract_keys_of(node: ast.expr | None, item: str) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr == "_contract_keys"
            and len(node.args) == 1
            and not node.keywords
            and is_name(node.args[0], item)
        )

    run_method = methods.get("run")
    batch_method = methods.get("_run_batch")
    hook_method = methods.get("run_hook")
    delegation_calls = self_method_calls(run_method, "_run_batch")
    run_delegates = (
        len(delegation_calls) == 1
        and is_name(keyword_value(delegation_calls[0], "affected_keys"), "affected_keys")
        and run_method is not None
        and any(
            isinstance(statement, ast.Return)
            and isinstance(statement.value, ast.Await)
            and statement.value.value is delegation_calls[0]
            for statement in run_method.body
        )
    )

    reader_calls = self_method_calls(batch_method, "_read_source")
    batch_reader_gets_keys = (
        len(reader_calls) == 1
        and len(reader_calls[0].args) >= 4
        and collection_of(reader_calls[0].args[3], "set", "affected_keys")
    )
    merge_calls = [
        call
        for call in (ast.walk(batch_method) if batch_method is not None else [])
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "merge_source_frames"
    ]
    batch_merge_gets_keys = len(merge_calls) == 1 and collection_of(
        keyword_value(merge_calls[0], "extra_diff_keys"), "frozenset", "affected_keys"
    )
    source_reader_method = methods.get("_read_source")

    def returned_local_reader_gets_keys(name: str) -> bool:
        if source_reader_method is None:
            return False
        calls = [
            statement.value
            for statement in ast.walk(source_reader_method)
            if isinstance(statement, ast.Return)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == name
        ]
        return (
            len(calls) == 1
            and len(calls[0].args) >= 3
            and is_name(calls[0].args[2], "affected_keys")
        )

    normal_reader_gets_keys = returned_local_reader_gets_keys("reader")
    scoped_reader_gets_keys = returned_local_reader_gets_keys("scoped_reader")

    def is_partition_test(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Attribute)
            and isinstance(node.left.value, ast.Name)
            and node.left.value.id == "context"
            and node.left.attr == "partition_contexts"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.IsNot)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value is None
        )

    partition_ifs = [
        node
        for node in (ast.walk(hook_method) if hook_method is not None else [])
        if isinstance(node, ast.If) and is_partition_test(node.test)
    ]
    partition_hook_resells = False
    context_hook_resells = False
    if len(partition_ifs) == 1 and hook_method is not None:
        partition_if = partition_ifs[0]
        partition_calls: list[ast.Call] = []
        for nested_node in (
            nested for statement in partition_if.body for nested in ast.walk(statement)
        ):
            if not isinstance(nested_node, ast.For) or not is_name(nested_node.target, "partition"):
                continue
            iterator = nested_node.iter
            if not (
                isinstance(iterator, ast.Call)
                and isinstance(iterator.func, ast.Attribute)
                and isinstance(iterator.func.value, ast.Name)
                and iterator.func.value.id == "context"
                and iterator.func.attr == "partition_contexts"
            ):
                continue
            partition_calls.extend(self_method_calls(nested_node, "_run_batch"))
        partition_hook_resells = len(partition_calls) == 1 and contract_keys_of(
            keyword_value(partition_calls[0], "affected_keys"), "partition"
        )
        try:
            branch_index = hook_method.body.index(partition_if)
        except ValueError:
            branch_index = -1
        if branch_index >= 0:
            for statement in hook_method.body[branch_index + 1 :]:
                if not isinstance(statement, ast.Return) or not isinstance(
                    statement.value, ast.Await
                ):
                    continue
                call = statement.value.value
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "self"
                    and call.func.attr == "run"
                ):
                    continue
                context_hook_resells = contract_keys_of(
                    keyword_value(call, "affected_keys"), "context"
                )
                break

    return {
        "run_delegates_affected_keys": flag(run_delegates),
        "batch_reader_gets_keys": flag(batch_reader_gets_keys),
        "batch_merge_gets_keys": flag(batch_merge_gets_keys),
        "normal_reader_gets_keys": flag(normal_reader_gets_keys),
        "scoped_reader_gets_keys": flag(scoped_reader_gets_keys),
        "reader_gets_keys": flag(
            run_delegates
            and batch_reader_gets_keys
            and normal_reader_gets_keys
            and scoped_reader_gets_keys
        ),
        "keys_extend_diffs": flag(run_delegates and batch_merge_gets_keys),
        "partition_hook_resells_keys": flag(partition_hook_resells),
        "context_hook_resells_keys": flag(context_hook_resells),
        "hook_resells_keys": flag(partition_hook_resells and context_hook_resells),
    }


def measure_ac9_07(ctx: Context) -> Facts:
    """Run revision nodes including scoped ODS reads, then trace corrected keys."""
    nodes = outcomes(DWD_REVISION_NODES)
    merge = ctx.read(DWD_MERGE_REL)
    call_chain = _ac9_07_call_chain_facts(merge)
    runner = ctx.read(RUNNER_REL)
    writer = method_body(merge, "DwdWriter", "write")
    return {
        "runs": count(len(nodes)),
        "passed": count(sum(1 for seen in nodes.values() if seen == "passed")),
        "bad": bad_of(nodes),
        **call_chain,
        "runner_publishes_keys": flag("affected_keys" in runner and "_affected_keys" in runner),
        "writer_upserts": flag("build_upsert_sql" in writer),
    }


def judge_ac9_07(facts: Facts) -> Verdict:
    """``AC-9|07``: a key corrected in ods re-writes its dwd row, and the unit test says so."""
    faces = (
        "run_delegates_affected_keys",
        "batch_reader_gets_keys",
        "batch_merge_gets_keys",
        "normal_reader_gets_keys",
        "scoped_reader_gets_keys",
        "reader_gets_keys",
        "keys_extend_diffs",
        "partition_hook_resells_keys",
        "context_hook_resells_keys",
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
        f"修订行为节点: {facts['passed']}/{facts['runs']} passed"
        + (f"; not green: {facts['bad']}" if facts["bad"] != "-" else "")
        + " —— 覆盖窗口外改值、生产 scoped reader、None 水位、多源和无关行排除",
        "传播链: runner 交键 = "
        f"{facts['runner_publishes_keys']}、run 委托 batch 并传键 = "
        f"{facts['run_delegates_affected_keys']}、batch reader 收键 = "
        f"{facts['batch_reader_gets_keys']}、普通/scoped reader 收键 = "
        f"{facts['normal_reader_gets_keys']}/{facts['scoped_reader_gets_keys']}、batch diff 收键 = "
        f"{facts['batch_merge_gets_keys']}、partition hook 重拼契约键 = "
        f"{facts['partition_hook_resells_keys']}、context hook 重拼契约键 = "
        f"{facts['context_hook_resells_keys']}、写侧按业务键 upsert = {facts['writer_upserts']}",
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
REFERENCE_POLICY_REL: Final = "docs/quality/akshare-reference-allowlist.json"
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

#: ``AC-16|07`` runs its P0 selection in a child interpreter whose top-level ``akshare``/``openbb``
#: are refused, so "an environment without these packages" is a property of the run rather than a
#: property of whatever the developer happens to have installed. The hook first asks for each named
#: package and must be turned down (positive control), then the whole selection runs with zero
#: refused attempts and zero upstream modules left in ``sys.modules``. ``import_module`` is patched
#: alongside ``__import__`` because it bypasses the builtin; ``find_spec`` is left alone so a
#: package-probe that decides a skip still returns instead of raising. The child echoes one
#: ``BLOCKFACE {json}`` line so the tally and the block face come from the same subprocess.
AC16_BLOCKED_PYTEST: Final = (
    "import builtins, importlib, importlib.util, json, sys\n"
    "pkgs = sys.argv[1].split(',')\n"
    "selector = sys.argv[2]\n"
    "installed = [p for p in pkgs if importlib.util.find_spec(p) is not None]\n"
    "blocked = []\n"
    "real_import = builtins.__import__\n"
    "def guard(name, *args, **kwargs):\n"
    "    if name.partition('.')[0] in pkgs:\n"
    "        blocked.append(name)\n"
    "        raise ImportError('intentional top-level block: ' + name)\n"
    "    return real_import(name, *args, **kwargs)\n"
    "builtins.__import__ = guard\n"
    "real_import_module = importlib.import_module\n"
    "def import_module(name, package=None):\n"
    "    if name.partition('.')[0] in pkgs:\n"
    "        blocked.append(name)\n"
    "        raise ImportError('intentional top-level block: ' + name)\n"
    "    return real_import_module(name, package)\n"
    "importlib.import_module = import_module\n"
    "for pkg in pkgs:\n"
    "    try:\n        __import__(pkg)\n    except ImportError:\n        pass\n"
    "control_hits = len(blocked)\nblocked.clear()\n"
    "import pytest\n"
    "code = int(pytest.main(['tests', '-m', selector, '--no-header', '-q', '--no-cov',\n"
    "                        '-p', 'no:cacheprovider']))\n"
    "leaks = sorted(m for m in sys.modules if m.partition('.')[0] in pkgs)\n"
    "face = {'pkgs': len(pkgs), 'control_hits': control_hits, 'attempted': len(blocked),\n"
    "        'attempted_names': sorted(set(blocked))[:8], 'leaks': len(leaks),\n"
    "        'installed': len(installed), 'installed_names': installed,\n"
    "        'leak_names': leaks[:8], 'exit': code}\n"
    "print('BLOCKFACE ' + json.dumps(face))\n"
    "sys.exit(code)\n"
)


def zero_dep_snapshot() -> tuple[ModuleType, list[Any], Facts]:
    """Read the current policy, scan findings and baseline without hiding stale evidence."""
    scanner = script_module(ZERO_DEP_SCANNER)
    policy_error = ""
    policy = None
    try:
        policy = scanner.load_reference_policy()
        policy_valid = "yes"
        findings = scanner.collect(scanner.DEFAULT_TARGETS)
    except scanner.ReferencePolicyError as exc:
        policy_valid = "no"
        policy_error = str(exc)
        findings = scanner.collect_raw(scanner.DEFAULT_TARGETS)

    try:
        baseline_raw = json.loads((REPO_ROOT / scanner.BASELINE_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        baseline_raw = {}
    baseline_findings = baseline_raw.get("findings", []) if isinstance(baseline_raw, dict) else []
    if not isinstance(baseline_findings, list):
        baseline_findings = []
    baseline_current = (
        isinstance(baseline_raw, dict)
        and baseline_raw.get("scanner_version") == scanner.SCANNER_VERSION
        and baseline_raw.get("version") == scanner.BASELINE_VERSION
    )
    return (
        scanner,
        findings,
        {
            "policy_valid": policy_valid,
            "policy_error": policy_error[:150] or "-",
            "metadata_exceptions": count(sum(len(entry.ast_exceptions) for entry in policy.entries))
            if policy is not None
            else "0",
            # Read from the scanner rather than pinned here: the scanner is the artifact that
            # decides which AST metadata exceptions are approved, and it rejects a policy that
            # binds them any way but exactly-once-each. A literal count in this probe was a
            # second copy of that decision and went stale the moment the scanner version moved.
            "metadata_expected": count(len(scanner.EXPECTED_METADATA_EXCEPTIONS)),
            "baseline_current": flag(baseline_current),
            "frozen": count(len(baseline_findings)),
        },
    )


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
    scanner, findings, policy_facts = zero_dep_snapshot()
    kinds = {
        kind: sum(1 for finding in findings if finding.kind == kind)
        for kind in ("import", "dynamic", "string")
    }
    try:
        frozen_raw = json.loads((REPO_ROOT / scanner.BASELINE_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        frozen_raw = {}
    baseline_entries = frozen_raw.get("findings", []) if isinstance(frozen_raw, dict) else []
    if not isinstance(baseline_entries, list):
        baseline_entries = []
    frozen_keys = {
        (entry.get("file"), entry.get("module"), entry.get("kind"))
        for entry in baseline_entries
        if isinstance(entry, dict)
    }
    update_body = function_body(ctx.read(ZERO_DEP_SCANNER), "update")
    refuses_growth = "refusing to grow the baseline" in update_body
    return {
        "detector_exit": str(code),
        "detector_ok_line": first_capture(out, r"^(OK: .*)$"),
        "self_test_exit": str(self_code),
        **policy_facts,
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
        and facts["policy_valid"] == "yes"
        and facts["baseline_current"] == "yes"
        and facts["metadata_exceptions"] == facts["metadata_expected"]
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
        f"新增即失败 = {facts['no_new_reference']}，只降不升的门禁在位 = {facts['only_down']}，"
        f"引用策略有效 = {facts['policy_valid']}、"
        f"基线 scanner 版本有效 = {facts['baseline_current']}）："
        f"{facts['string_live']} 条是名字面量，落在 {facts['string_files']}",
        f"扫描器自己怎么说：{facts['detector_ok_line'] or '-'}",
    )
    reason = (
        ""
        if ok
        else "引用策略或其 SHA/AST 绑定不成立，或冻结基线版本/清零面未通过；"
        "不能以未登记路径或旧扫描器版本"
        "宣称清零。另「基线清零」是这条需原样保持的判定面，而要把它抹平只有两条路，两条都要改判据本身：①把扫描器"
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
    """Run the P0 selection where upstream top-level imports are refused, then read the archive."""
    ini = ctx.read("pytest.ini")
    units = integration_units_now()
    modules = sorted({unit.split("::")[0] for unit in units})
    text = "".join(ctx.read(rel) for rel in modules)
    domains = p0_domains_in_migration()
    missing = [domain for domain in domains if domain not in text]
    code, out, err = run_split_stderr(
        [
            sys.executable,
            "-B",
            "-c",
            AC16_BLOCKED_PYTEST,
            ",".join(UPSTREAM_PACKAGES),
            P0_INTEGRATION_SELECTOR,
        ]
    )
    face_line = next((line for line in out.splitlines() if line.startswith("BLOCKFACE ")), "")
    if not face_line:
        tail = [line for line in err.splitlines() if line.strip()]
        raise ProbeError(
            f"blocked pytest run printed no BLOCKFACE line (exit {code}): "
            f"{tail[-1] if tail else '(no output)'}"
        )
    face = cast("dict[str, Any]", json.loads(face_line[len("BLOCKFACE ") :]))
    # tally() reads pytest's own last line, so the face line the child appends after the run
    # has to come out of the text first -- otherwise the summary counts read as zero.
    pytest_out = "\n".join(line for line in out.splitlines() if not line.startswith("BLOCKFACE "))
    counts = tally(pytest_out)
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
        "block_pkgs": count(int(face.get("pkgs", 0))),
        "block_control_hits": count(int(face.get("control_hits", 0))),
        "block_attempted": count(int(face.get("attempted", 0))),
        "block_attempt_names": ", ".join(face.get("attempted_names") or []) or "-",
        "block_leaks": count(int(face.get("leaks", 0))),
        "block_leak_names": ", ".join(face.get("leak_names") or []) or "-",
        "block_installed": count(int(face.get("installed", 0))),
        "block_installed_names": ", ".join(face.get("installed_names") or []) or "-",
        # 判定读的是「这一遍跑在没有顶层上游包的解释器里」：钩子先把两个包各拒一次（正向对照），
        # 整套选择式跑完后既没有一次被拒的记录、sys.modules 里也没留下上游模块。
        "blocked_here": flag(
            int(face.get("control_hits", 0)) == len(UPSTREAM_PACKAGES)
            and int(face.get("attempted", 0)) == 0
            and int(face.get("leaks", 0)) == 0
            and int(face.get("exit", -1)) == code
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
        and facts["blocked_here"] == "yes"
        and facts["block_pkgs"] == count(len(UPSTREAM_PACKAGES))
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
        f"本机这一遍跑在顶层上游包被强制拦走的子解释器里（判定 = {facts['blocked_here']}）："
        f"钩子先主动 import 这 {facts['block_pkgs']} 个包，"
        f"被拒 {facts['block_control_hits']} 次（正向对照），"
        f"随后整套选择式跑完：被尝试 {facts['block_attempted']} 次"
        f"（{facts['block_attempt_names']}）、sys.modules 里留下 {facts['block_leaks']} 个"
        f"（{facts['block_leak_names']}）；exit={facts['run_exit']}，"
        f"{facts['passed']} passed / {facts['failed']} failed / {facts['skipped']} skipped；"
        f"另有 {facts['deselected']} 条被选择式挡在外面（含全部仓库 e2e）",
        f"本机其实装着这 {facts['block_pkgs']} 个上游包中的 {facts['block_installed']} 个"
        f"（{facts['block_installed_names']}）—— 所以这一遍的缺席是被强制的，不是环境巧合",
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
        "点到），要么这一遍没全绿、或者跳过了（skip 不等于通过），要么这一遍跑的解释器并没有把"
        "顶层上游包拒掉（钩子没生效、跑的过程中又被尝试、或 sys.modules 里留下了上游模块 —— "
        "那样这份绿分不清用的是搬运层还是真包），要么干净环境的留档不再覆盖现在这组标记模块"
        "（刻意的棘轮：给 P0 集成面加一个模块，就得重跑一次那个只装声明依赖的 venv 并重留档），"
        "要么那份留档只是从上一轮拷来的（正文声明的轮次标号对不上所在目录）"
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
QFQ_OFFICIAL_RUN: Final = "docs/evidence/C65/qfq-official-akshare-current.txt"

#: The module AC-11|02 names when it says "akshare 官方 qfq": the ported akshare fetcher, in the
#: dotted form it carries on disk today. A leg that resolves anywhere else is some other vendor's
#: chain wearing the criterion's word.
QFQ_AKSHARE_MODULE: Final = ported_module_identity("opendata_http.stock_feature.stock_hist_em")

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
        "run_official_target": current_target_identity(first_capture(run, r"^official: \w+ (\S+)")),
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
        for rel in first_party_py_files_under(root)
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

#: The ported root, its manifest and its dataset register all come from ``PORTED_ROOT`` above,
#: which is read off :mod:`scripts.quality.source_layout` -- the tree moved into the akshare
#: provider package, and a literal here would be a second, drifting answer to where it lives.
PORTED_MANIFEST: Final = f"{PORTED_ROOT}/manifest.json"
GEN_MANIFEST_TOOL: Final = "scripts/codemod/gen_manifest.py"
DATASETS_REL: Final = f"{PORTED_ROOT}/datasets.py"
REQ_DOC_REL: Final = "docs/迭代计划/迭代1-重构数据中台/需求文档.md"
#: First-party roots, walked through the shared classifier so the ``_vendor`` subtree nested
#: inside ``opendata/`` is counted as ported and never as first-party call sites.
FIRST_PARTY_TREES: Final = ("opendata", "opendata_client", "scripts", "tests")
SECURITY_PORTED_TARGET: Final = "security-ported"
BANDIT_CONFIG: Final = "bandit.yaml"


#: Imported in a child process so the callable face of the flat API is read from the same
#: interpreter AC-16|07 proves carries no ``akshare``: an attribute the aggregator does not
#: re-export is a function a first-party leg would fail to reach at routing time.
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

    ``import opendata.data.providers.akshare._vendor as ak`` is as much a claim on the
    aggregator's surface as ``_vendor.stock_zh_a_hist()``, so the alias is resolved per file
    before the attributes under it are counted; a leg that reaches for a name the facade does
    not re-export is a routing failure waiting for the next ``fetch``.

    The walk goes through the shared classifier rather than a bare ``rglob``: the ported subtree
    now lives inside ``opendata/``, so an unclassified walk would count 325 MIT files as
    first-party call sites and quietly widen this denominator.
    """
    found: set[str] = set()
    files = _LAYOUT.iter_unique_python_files(
        REPO_ROOT,
        FIRST_PARTY_TREES,
        layers=frozenset({_LAYOUT.FIRST_PARTY}),
    )
    for source in files:
        path = REPO_ROOT / source.identity
        try:
            parsed = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        bound = {PORTED_BIND_NAME}
        for node in ast.walk(parsed):
            if isinstance(node, ast.Import):
                bound.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == PORTED_MODULE
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
    """The flat API names the ported aggregator publishes, in either of its two shapes.

    Before the move the aggregator spelled every endpoint as a static ``from opendata_http.stock
    import ...``; the migrated facade is lazy and keeps its surface in a module-level ``_EXPORTS``
    register resolved through ``__getattr__``. An AST scan that only knew the static form would
    read an empty surface and report a perfectly reachable flat API as dead, so both shapes count
    here. A register entry is accepted only when the module it points at is inside the ported
    package: a name resolved out of a first-party BSL module is not part of the MIT surface, and
    dropping it turns the claim ``every called name is published`` back into a red light.
    """
    text = (REPO_ROOT / PORTED_ROOT / "__init__.py").read_text(encoding="utf-8", errors="replace")
    names: set[str] = set()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if str(node.module or "").startswith(PORTED_MODULE) or node.level > 0:
            names.update(alias.asname or alias.name for alias in node.names)
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "_EXPORTS" for t in statement.targets):
            continue
        if not isinstance(statement.value, ast.Dict):
            continue
        for key, value in zip(statement.value.keys, statement.value.values, strict=True):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            elements = value.elts if isinstance(value, ast.Tuple) else []
            origin = elements[0].value if isinstance(elements[0], ast.Constant) else ""
            if str(origin).startswith(PORTED_MODULE):
                names.add(key.value)
    return frozenset(names)


@lru_cache(maxsize=1)
def port_scope_audit() -> PortScopeAuditResult:
    """Reconcile the frozen port-batch inventory against the live lock, manifest, and disk."""
    module = script_module("scripts/quality/port_scope.py")
    audit_factory = module.__dict__.get("audit_port_scope")
    if not callable(audit_factory):
        raise ProbeError("port_scope.py has no audit_port_scope function")
    audit = cast("Callable[[Path], PortScopeAuditResult]", audit_factory)
    return audit(REPO_ROOT)


def ported_batch_field() -> str:
    """Return the manifest batch field only when its complete path inventory validates."""
    return str(port_scope_audit().batch_field)


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
    unlocked = cast("frozenset[str]", script_module(PORT_MODULE_TOOL).UNLOCKED_METADATA)
    disk_other = sorted(
        path.relative_to(base).as_posix()
        for path in base.rglob("*")
        if path.is_file()
        and path.suffix != ".py"
        and "__pycache__" not in path.parts
        and path.name not in unlocked
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
    scope_audit = port_scope_audit()
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
        "port_scope_valid": flag(bool(scope_audit.valid)),
        "port_scope_checked": count(int(scope_audit.checked_paths)),
        "port_scope_python": count(int(scope_audit.python_paths)),
        "port_scope_resources": count(int(scope_audit.resource_paths)),
        "port_scope_problems": count(len(scope_audit.problems)),
        "port_scope_problem_summary": str(scope_audit.problem_summary),
        "scope_inventory_rel": str(scope_audit.inventory_rel),
        "tier_field": str(scope_audit.batch_field),
        "tier_field_ok": flag(str(scope_audit.batch_field) != "-"),
    }


def judge_ac5_02(facts: Facts) -> Verdict:
    """``AC-5|02``: the ported breadth is used by our legs, and the batch split is readable."""
    ok = (
        facts["has_calls"] == "yes"
        and facts["called_missing"] == "0"
        and facts["excluded_present"] == "0"
        and facts["scope_missing"] == "0"
        and facts["port_scope_valid"] == "yes"
        and facts["tier_field_ok"] == "yes"
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
        f"逐路径锁/manifest/磁盘核验 = {facts['port_scope_valid']}（核对 "
        f"{facts['port_scope_checked']} 条：{facts['port_scope_python']} py + "
        f"{facts['port_scope_resources']} 资源；问题 {facts['port_scope_problems']}："
        f"{facts['port_scope_problem_summary']}）",
        f"能读出 1A/1B 批次的机器字段 = {facts['tier_field']}"
        f"（核验读的是 {facts['scope_inventory_rel']} 那一份档案）",
    )
    reason = (
        ""
        if ok
        else (
            "AC-5|02 的搬运范围证据不完整：首方调用必须都有聚合导出；"
            "D9 点名和排除的子模块必须与清单一致，"
            "并且本轮批次清单要逐路径匹配当前 upstream.lock、port manifest、磁盘路径与 sha256；"
            f"当前问题为 {facts['port_scope_problem_summary']}。"
            "批次按路径组描述实施范围，不推导或改写数据域优先级。"
        )
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac5_03(ctx: Context) -> Facts:
    """Read the report's pending-TODO face, then run the frozen AST scan over it."""
    report = ctx.read(PORT_REPORT_ARCHIVE)
    code, out = run_argv([sys.executable, ZERO_DEP_SCANNER])
    self_code, _ = run_argv([sys.executable, ZERO_DEP_SCANNER, "--self-test"])
    scanner, findings, policy_facts = zero_dep_snapshot()
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
        "detector_note": f"exit={code}; findings={len(findings)} (scanner output omitted)",
        "self_test_exit": str(self_code),
        **policy_facts,
        "import_live": count(kinds["import"]),
        "dynamic_live": count(kinds["dynamic"]),
        "string_live": count(kinds["string"]),
        "string_files": ", ".join(sorted({f.file for f in findings if f.kind == "string"})) or "-",
    }


def judge_ac5_03(facts: Facts) -> Verdict:
    """``AC-5|03``: the diff report is out of TODOs and the AST scan sees no upstream reference."""
    ok = (
        facts["todo_reported"] == "0"
        and facts["todo_section_ok"] == "yes"
        and facts["detector_exit"] == "0"
        and facts["self_test_exit"] == "0"
        and facts["policy_valid"] == "yes"
        and facts["baseline_current"] == "yes"
        and facts["metadata_exceptions"] == facts["metadata_expected"]
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
        f"动态导入 {facts['dynamic_live']}、字符串常量 {facts['string_live']}；"
        f"reference policy valid={facts['policy_valid']} with "
        f"{facts['metadata_exceptions']} exact exceptions",
        f"扫描器冻着的基线 {facts['frozen']} 条 "
        f"（scanner version current={facts['baseline_current']}），"
        f"字符串形态落在 {facts['string_files'] or '-'}",
        f"扫描器原话：{facts['detector_note']}",
    )
    reason = (
        ""
        if ok
        else "引用策略/基线版本/AST 清零面未通过，或判据原文的零引用口径仍有 findings；"
        "不能用过期白名单"
        "或过期基线声称通过。基线里剩余的名字面量为 "
        f"{facts['string_live']} 条（{facts['string_files']}）。"
        "抹平它只有两条路，"
        "两条都要改判据本身：①把扫描器字符串规则改窄（等于放宽已勾的 AC-16|05「AST 口径」）；"
        "②改掉这三处真实数据源名的拼写（正是 C35 收口过的门禁空转）。同一道题 AC-16|06 已经"
        "登记为用户/产品决策（task #64），这里不重复改判、也不换个说法再判一次"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac5_04(ctx: Context) -> Facts:
    """Check the old root is gone, then reach the flat API from an interpreter without akshare.

    The absence is *enforced* here rather than observed: this developer venv does have akshare
    installed, so a face that merely asked ``find_spec`` was reporting whichever packages the
    person running the gate happened to have, and it read the parent interpreter rather than the
    one that imports the ported tree.
    """
    root_dir = (REPO_ROOT / "akshare").is_dir()
    code, out = run_argv(["git", "ls-files", "akshare/"])
    tracked = sorted(line for line in out.splitlines() if line)
    names = ported_call_targets()
    witness = runtime_witness("vendor_no_akshare", *names)
    leaks = witness_list(witness, "leaks")
    missing = witness_list(witness, "missing")
    port_error = str(witness["port_import_error"])
    archive_rel, archive_dir = newest_round_archive(CLEAN_RUN_BASENAME)
    evidence = ctx.read(archive_rel) if archive_rel != "-" else ""
    declared = first_capture(evidence, r"^ARCHIVE_ROUND=(\S+)$")
    return {
        "root_dir": flag(root_dir),
        "tracked_under_root": count(len(tracked)),
        "tracked_names": ", ".join(tracked[:4]) or "-",
        "akshare_blocked": flag(
            witness.get("hook_fired") is True and int(witness["control_hits"]) > 0
        ),
        "akshare_reached": count(int(witness["port_import_hits"])),
        "akshare_leaks": count(len(leaks)),
        "akshare_leak_names": ", ".join(leaks[:4]) or "-",
        "interpreter_has_akshare": flag(witness.get("spec_present") is True),
        "import_exit": "0" if port_error == "" else "1",
        "import_note": port_error or "(import 成功)",
        "call_targets": count(int(witness["targets"])),
        "callable_missing": count(len(missing)),
        "callable_missing_names": ", ".join(missing[:4]) or "-",
        "clean_archive": archive_rel,
        "archive_round": declared or "-",
        "archive_self_written": flag(bool(declared) and declared == archive_dir),
        "archive_absent": flag(
            "akshare: absent" in evidence and "akshare: present" not in evidence
        ),
    }


def judge_ac5_04(facts: Facts) -> Verdict:
    """``AC-5|04``: no root/index ``akshare``, and the legs resolve without top-level akshare."""
    ok = (
        facts["root_dir"] == "no"
        and facts["tracked_under_root"] == "0"
        and facts["akshare_blocked"] == "yes"
        and facts["akshare_reached"] == "0"
        and facts["akshare_leaks"] == "0"
        and facts["import_exit"] == "0"
        and number(facts["call_targets"]) > 0
        and facts["callable_missing"] == "0"
    )
    readings = (
        f"根目录 `akshare/` 在磁盘上 = {facts['root_dir']}，git 索引里该前缀下 "
        f"{facts['tracked_under_root']} 个文件：{facts['tracked_names']}",
        "执行见证：import hook 拦得住顶层 `akshare`"
        f"（主动 import 被拒 = {facts['akshare_blocked']}），"
        f"搬运层 `import {PORTED_MODULE}` exit={facts['import_exit']}（{facts['import_note']}），"
        f"首方代码取用的 {facts['call_targets']} 个端点函数逐个 `callable(getattr(...))`，"
        f"取不到的 {facts['callable_missing']} 个：{facts['callable_missing_names']}",
        f"搬运层导入期间顶层 `akshare` 被尝试 {facts['akshare_reached']} 次、"
        f"留在 sys.modules 里 {facts['akshare_leaks']} 个：{facts['akshare_leak_names']}；"
        f"本解释器其实装了 akshare = {facts['interpreter_has_akshare']}，"
        "所以这份缺席是被强制的、不是环境巧合",
        f"干净 venv 侧证（{facts['clean_archive']}，ARCHIVE_ROUND={facts['archive_round']}，"
        f"轮次自洽 = {facts['archive_self_written']}）里 akshare 报 absent = "
        f"{facts['archive_absent']}；侧证只作旁证，判定读的是上面同解释器里的那条见证",
    )
    reason = (
        ""
        if ok
        else "「根目录 akshare/ 已删除；未安装 akshare 的环境中 P0 域函数可用」"
        "要的是这两句同时成立："
        "根目录既不在磁盘也不在索引里，而一个顶层 akshare 被证明拦得住的解释器里，"
        "搬运层能导入、首方代码取用的端点逐个可达、全程没有回落到顶层包"
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


def measure_ac5_07(ctx: Context) -> Facts:
    """Bind current Bandit scan and risk-preserving triage to the ported source tree."""
    recipes = make_recipes(ctx.read("Makefile"))
    # make 的 recipe 里 `@#` 与 `#` 都是注释；把它们算进命令会让 target_ok 被注释养活，
    # 也会让读数只打印注释、看不见真正执行的命令
    ported_recipe = " ".join(
        line
        for line in recipes.get(SECURITY_PORTED_TARGET, [])
        if not line.lstrip("@").startswith("#")
    )
    listed_py = {entry_path(entry) for entry in manifest_entries(ported_manifest(), "files")}
    ported_now = set(py_files_under(PORTED_ROOT))
    exclude_dirs = [
        str(item)
        for item in ((yaml.safe_load(ctx.read(BANDIT_CONFIG)) or {}).get("exclude_dirs") or [])
    ]
    root_entry = str(ctx.root.resolve())
    inserted_root = root_entry not in sys.path
    if inserted_root:
        sys.path.insert(0, root_entry)
    try:
        from scripts.quality.ported_security_evidence import SCAN_REL, TRIAGE_REL, validate

        security = validate(ctx.root)
    finally:
        if inserted_root:
            with suppress(ValueError):
                sys.path.remove(root_entry)
    scan_rel = str(SCAN_REL)
    triage_rel = str(TRIAGE_REL)
    scan_doc = json.loads(ctx.read(scan_rel)) if (ctx.root / scan_rel).is_file() else {}
    triage_doc = json.loads(ctx.read(triage_rel)) if (ctx.root / triage_rel).is_file() else {}
    carried = triage_doc.get("carried_from")
    recorded_round = str(scan_doc.get("archive_round", "")) or "-"
    archive_dir = scan_rel.split("/")[-2]
    measured = security.facts
    issue_codes = security.issues
    manifest_paths = {f"{PORTED_ROOT}/{path}" for path in listed_py}
    surface_covered = listed_py == {path.removeprefix(f"{PORTED_ROOT}/") for path in ported_now}
    findings = int(measured.get("finding_count", 0))
    high_count = int(measured.get("high_count", 0))
    triaged_count = int(measured.get("triaged_findings", 0))
    rule_count = int(measured.get("rule_count", 0))
    scan_files = int(measured.get("scan_files", 0))
    issue_count = int(measured.get("issue_count", len(issue_codes)))
    return {
        "target_ok": flag(bool(ported_recipe) and PORTED_ROOT in ported_recipe),
        "target_recipe": ported_recipe[:140] or "-",
        "daily_excludes_ported": flag(PORTED_ROOT in {d.rstrip("/") for d in exclude_dirs}),
        "bandit_exclude_dirs": ", ".join(exclude_dirs) or "-",
        "ported_files": count(len(ported_now)),
        "archive": scan_rel,
        "archive_round": recorded_round,
        "archive_round_in_dir": flag(recorded_round != "-" and recorded_round == archive_dir),
        "carried_from_round": str((carried or {}).get("prior_round", "")) or "-",
        "security_evidence_valid": flag(security.valid),
        "security_issue_count": count(issue_count),
        "security_issue_summary": ", ".join(issue.code for issue in issue_codes[:8]) or "-",
        "scan_source_tree_valid": flag(bool(measured.get("source_tree_valid"))),
        "scan_manifest_ok": flag(bool(measured.get("source_tree_valid"))),
        "scan_python_files": count(int(measured.get("python_files", 0))),
        "scan_source_files_verified": count(int(measured.get("source_files_verified", 0))),
        "scan_source_hash": str(measured.get("source_sha256", "")) or "-",
        "scan_source_files_saved": count(int(measured.get("source_files_saved", 0))),
        "archive_produced_by": str(measured.get("scanner_version", "")) or "-",
        "archive_generated_at": str(measured.get("generated_at", "")) or "-",
        "scan_errors": str(measured.get("scan_errors", "invalid")),
        "scanner_exit": str(measured.get("scanner_exit", "unknown")),
        "scanner_exit_consistent": flag(bool(measured.get("scanner_exit_consistent"))),
        "scan_findings": count(findings),
        "scan_high_findings": count(high_count),
        "scan_files": count(scan_files),
        "scan_rules": count(rule_count),
        "scan_rule_names": str(measured.get("rule_names", "")) or "-",
        "scan_b_only": flag(bool(measured.get("all_rules_bandit"))),
        "scan_manifest_matches_tree": flag(manifest_paths == ported_now),
        "scan_findings_covered": flag(bool(measured.get("finding_coverage"))),
        "scan_stray_names": "-",
        "triage_rules": count(rule_count),
        "triage_findings": count(triaged_count),
        "findings_untriaged": count(int(measured.get("untriaged_findings", 0))),
        "untriaged_high": count(int(measured.get("untriaged_high", 0))),
        "triage_rule_review_valid": flag(bool(measured.get("rule_review_valid"))),
        "triage_high_reviewed": flag(bool(measured.get("high_reviewed"))),
        "triage_deser_reviewed": flag(bool(measured.get("deser_reviewed"))),
        "triage_rce_retained": flag(bool(measured.get("rce_findings_retained"))),
        "triage_reviewer_authorized": flag(bool(measured.get("reviewer_authorized"))),
        "triage_review_complete": flag(bool(measured.get("review_complete"))),
        "triage_security_fixed": flag(bool(measured.get("security_fixed"))),
        "triage_unresolved_risks": flag(bool(measured.get("unresolved_risks"))),
        "surface_covered": flag(surface_covered),
    }


def judge_ac5_07(facts: Facts) -> Verdict:
    """``AC-5|07``: the ported tree's bandit run is on record and the triage covers all of it."""
    ok = all(
        (
            facts["target_ok"] == "yes",
            facts["security_evidence_valid"] == "yes",
            facts["security_issue_count"] == "0",
            facts["scan_source_tree_valid"] == "yes",
            facts["scanner_exit_consistent"] == "yes",
            facts["scan_errors"] == "0",
            facts["archive"] != "-",
            facts["archive_round_in_dir"] == "yes",
            facts["scan_b_only"] == "yes",
            facts["scan_manifest_matches_tree"] == "yes",
            number(facts["scan_findings"]) > 0,
            facts["scan_findings_covered"] == "yes",
            facts["findings_untriaged"] == "0",
            facts["untriaged_high"] == "0",
            facts["triage_rule_review_valid"] == "yes",
            facts["triage_high_reviewed"] == "yes",
            facts["triage_deser_reviewed"] == "yes",
            facts["triage_rce_retained"] == "yes",
            facts["triage_reviewer_authorized"] == "yes",
            facts["triage_review_complete"] == "yes",
            facts["triage_security_fixed"] == "no",
            facts["triage_unresolved_risks"] == "yes",
            facts["surface_covered"] == "yes",
        )
    )
    readings = (
        f"搬运层专用扫描目标 `Makefile:{SECURITY_PORTED_TARGET}` 在位且打的是 {PORTED_ROOT}/ = "
        f"{facts['target_ok']}（recipe：{facts['target_recipe']}）；日常 `make security` 按 "
        f"{BANDIT_CONFIG} 的 exclude_dirs（{facts['bandit_exclude_dirs']}）把搬运层排出去，"
        f"所以「含 B 层」的全量复核只能由这个专用目标承担 = {facts['daily_excludes_ported']}"
        "（质量规范 §4 的「每次同步一次」口径，不是逐提交记账）",
        f"本轮 {facts['archive_round']} 全量扫描与分组风险审阅证据 {facts['archive']}"
        "（处置逐条结转自 "
        f"{facts['carried_from_round']}）由 validator 独立绑定 = "
        f"{facts['security_evidence_valid']}（issue={facts['security_issue_count']}，"
        f"{facts['security_issue_summary']}）；Bandit {facts['archive_produced_by']}，"
        f"generated_at={facts['archive_generated_at']}，exit={facts['scanner_exit']} "
        "且与 findings 一致 = "
        f"{facts['scanner_exit_consistent']}，errors={facts['scan_errors']}，记录 "
        f"{facts['scan_findings']} 项 / {facts['scan_files']} 个有 finding 的文件 / "
        f"{facts['scan_rules']} 条 B 规则（{facts['scan_rule_names']}），"
        "错误流为空且结果形状有效 = "
        f"{facts['scan_b_only']}；退出码 1 表示本轮发现了 finding，不等同于扫描执行失败",
        f"扫描源绑定当前搬运树：清单每文件 hash 已验证 {facts['scan_source_files_verified']}/"
        f"{facts['scan_python_files']} 个，保存条目 {facts['scan_source_files_saved']} 个，"
        f"path/NUL/content/NUL SHA256={facts['scan_source_hash']}，source tree valid = "
        f"{facts['scan_source_tree_valid']}，搬运 manifest 与磁盘路径集合相同 = "
        f"{facts['scan_manifest_matches_tree']}，当前 findings 与 triage identity 完整覆盖 = "
        f"{facts['scan_findings_covered']}（未处置 {facts['findings_untriaged']} 项，"
        f"其中未处置 HIGH {facts['untriaged_high']} 项）",
        f"当前分组 triage 完成 {facts['triage_findings']} 项 / {facts['triage_rules']} 条规则，"
        f"规则 disposition、basis、follow_up 与 finding 一致 = "
        f"{facts['triage_rule_review_valid']}；"
        f"实际 HIGH finding {facts['scan_high_findings']} 项，"
        f"未处置 HIGH {facts['untriaged_high']} 项，"
        f"HIGH 审阅覆盖 = {facts['triage_high_reviewed']}，AI 审阅的直接用户授权 = "
        f"{facts['triage_reviewer_authorized']}，审阅完成 = {facts['triage_review_complete']}",
        f"反序列化风险组 B301/B403 有明确审阅 = {facts['triage_deser_reviewed']}；"
        f"B301/B307 RCE finding 均保留为风险 = {facts['triage_rce_retained']}；"
        f"security_fixed={facts['triage_security_fixed']}、unresolved_risks="
        f"{facts['triage_unresolved_risks']}，不将审阅表述为修复或零风险",
        f"搬运 manifest 源文件与磁盘 py 文件覆盖一致 = {facts['surface_covered']} "
        f"（磁盘 {facts['ported_files']} 个 py 文件）",
    )
    reason = (
        ""
        if ok
        else "当前验收要求本轮扫描、源树、逐 finding triage 和保留风险结论能够相互核对："
        f"security evidence valid={facts['security_evidence_valid']}（issues="
        f"{facts['security_issue_count']} {facts['security_issue_summary']}），"
        f"source tree={facts['scan_source_tree_valid']}，"
        f"coverage={facts['scan_findings_covered']}，"
        f"untriaged={facts['findings_untriaged']} / HIGH={facts['untriaged_high']}，"
        f"rule review={facts['triage_rule_review_valid']}，deserialization review="
        f"{facts['triage_deser_reviewed']}，RCE retained={facts['triage_rce_retained']}，"
        f"security_fixed={facts['triage_security_fixed']}，unresolved risks="
        f"{facts['triage_unresolved_risks']}"
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
# An evidence file carrying one of the probe's own reading markers is a machine echo, not a human
# record -- and an echo names every provider, so a round would credit itself for measuring itself.
PROBE_ECHO_MARKERS: Final = ("VERDICT ", "判据原文：", "本探针：", "台账现状：")

#: Where a per-provider clean-room record has to live: one tracked file per package, named for it.
#: Path-gating is the whole point. Measured on C77's tree, the prose-grep face credited 7 packages
#: over 8 lines from exactly three files -- ``C64/remaining-items-audit.md`` (two lines whose text
#: is ``提交说明带「无 OpenBB 源码参照」的 0 个``, i.e. a statement that the record is *missing*)
#: and two ``C65/item-readings-*.{json,txt}`` dumps of this probe's own readings. Neither carries
#: ``PROBE_ECHO_MARKERS`` string on its face, so the marker filter let both through, and the item's
#: ``recorded_pkgs`` was the probe crediting itself for having printed a gap.
CLEAN_ROOM_RECORD_ROOT: Final = "docs/evidence/clean-room"

#: The line that states the conclusion, and the fields that make the statement checkable.
RECORD_CONCLUSION_MARK: Final = "审查结论"
RECORD_FIELDS: Final = ("审查日期", "审查人", "覆盖面", "代码面摘要", "方法与反证")

#: A conclusion line carrying one of these is a disclosure, not a declaration. The phrase survives
#: negation by substring, so without this guard 「无法证明 akshare 无 OpenBB 源码参照」 would be
#: read as a record of akshare having none.
RECORD_NEGATIONS: Final = ("无法", "未能", "不能证明", "未审查", "待补", "缺口", "冒充", "不构成")

#: The shipped packages -- the only surface 「全库无 OpenBB 源码」 can be measured on. C66 folded
#: the ported akshare tree and the THS transport *inside* ``opendata``, so the two names the
#: relocation removed are gone from this tuple rather than silently matching nothing: a root that
#: no path starts with shrinks the census without changing a single printed number.
RUNTIME_PY_ROOTS: Final = ("opendata", "opendata_client")


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
    """Tracked ``.py`` paths under the shipped roots that are present on disk.

    ``git ls-files`` reports the *index*, so a file deleted in the worktree is still listed while
    it is no longer shipped. The AC-16 face walks the shipped packages' import graph, and an
    unreadable path there is not a finding about OpenBB -- it is a walker reading a directory that
    is gone. Presence is filtered here, at the population, so every consumer of this census (the
    import map, the name scan) measures the same tree rather than one that crashes halfway through.
    """
    return sorted(
        rel
        for rel in ctx.tracked()
        if rel.endswith(".py")
        and rel.split("/")[0] in RUNTIME_PY_ROOTS
        and (ctx.root / rel).is_file()
    )


def runtime_py_files_with_optional(ctx: Context, optional_paths: Sequence[str]) -> list[str]:
    """Add named worktree runtime files without changing the global tracked-file surface."""
    present = {rel for rel in optional_paths if (ctx.root / rel).is_file()}
    return sorted(set(runtime_py_files(ctx)) | present)


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
        first_party_py_files_under(SELFDEV_PROVIDER_ROOT),
        re.compile(re.escape(CODE_CLEAN_ROOM_PHRASE), re.IGNORECASE),
    )
    covered = set(names)
    return sorted({rel.split("/")[3] for rel in hits if rel.split("/")[3] in covered})


@lru_cache(maxsize=1)
def record_bodies(tracked: tuple[str, ...]) -> tuple[str, ...]:
    """Texts under ``docs/evidence/`` that state both halves of the clean-room criterion.

    The requirement documents restate the rule for every provider at once, so a restatement
    there cannot count as a per-provider record; evidence files are where a record lives.

    Two exclusions are load-bearing. A file that carries the probe's own reading markers is a
    machine echo, not a human record -- and the echo names every provider, so without it a round
    would credit itself for the text it wrote while measuring itself (C62 measured exactly that:
    a run whose 留档 face read 7/7 because this round's own archive quoted the phrase).
    """
    bodies: list[str] = []
    for rel in tracked:
        if not rel.startswith("docs/evidence/"):
            continue
        try:
            body = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if CLEAN_ROOM_PHRASE not in body or "审查" not in body:
            continue
        if any(marker in body for marker in PROBE_ECHO_MARKERS):
            continue
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


def provider_py_files(ctx: Context, name: str) -> list[str]:
    """One package's own tracked ``.py`` paths that are present on disk."""
    prefix = f"{SELFDEV_PROVIDER_ROOT}/{name}/"
    return sorted(
        rel
        for rel in ctx.tracked()
        if rel.startswith(prefix) and rel.endswith(".py") and (ctx.root / rel).is_file()
    )


def provider_py_digest(root: Path, files: tuple[str, ...]) -> str:
    """A digest over the exact bytes a review looked at, so a record cannot outlive its subject.

    Uncached on purpose: a memo keyed on the file *names* would hand back the digest of the bytes
    read earlier in the same process, so a package whose code moved mid-run would still certify the
    stale review -- the one face here that has to be a fresh reading of disk.
    """
    hasher = hashlib.sha256()
    for rel in files:
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update((root / rel).read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def record_field_value(body: str, field: str) -> str:
    """What the record wrote after ``字段：``, from the first line carrying that field."""
    marker = f"{field}："
    for line in body.splitlines():
        if marker in line:
            return line.split(marker, 1)[1].strip().strip("`* ").rstrip("。")
    return ""


def canonical_clean_room_records(
    ctx: Context, names: Sequence[str]
) -> tuple[list[str], int, list[str]]:
    """``(credited packages, conclusion lines, per-package refusals)``.

    A record is one tracked file at ``docs/evidence/clean-room/<package>.md`` that carries an
    ISO 审查日期, a human 审查人, a 覆盖面 count equal to that package's measured ``.py`` census, a
    代码面摘要 equal to the digest of those bytes, a written 方法与反证, and a conclusion line
    binding the package name to 「无 OpenBB 源码参照」 with 「审查结论」 on it and no negation
    token.

    Every one of those is a refusal with a printed reason rather than a silent skip, because the
    old face's failure was silence: a quote of the gap was credited as the record, and nothing in
    the reading said so. A package whose bytes move after the review stops being credited on
    digest mismatch, which is the point of pinning them.
    """
    credited: list[str] = []
    binds = 0
    refusals: list[str] = []
    tracked = set(ctx.tracked())
    for name in names:
        rel = f"{CLEAN_ROOM_RECORD_ROOT}/{name}.md"
        if rel not in tracked:
            refusals.append(f"{name}: 没有逐包档案（{CLEAN_ROOM_RECORD_ROOT}/{name}.md 未跟踪）")
            continue
        try:
            body = (ctx.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            refusals.append(f"{name}: 档案读不到（{exc}）")
            continue
        if any(marker in body for marker in PROBE_ECHO_MARKERS):
            refusals.append(f"{name}: 档案带探针读数同形标记，是机器回声不是人工留档")
            continue
        files = provider_py_files(ctx, name)
        checks: list[str] = []
        reviewed_date = record_field_value(body, "审查日期")
        if not re.fullmatch(r"20\d\d-\d\d-\d\d", reviewed_date):
            checks.append(f"审查日期不是 ISO 日（读到 {reviewed_date!r}）")
        reviewer = record_field_value(body, "审查人")
        if not reviewer or any(tok in reviewer for tok in ("探针", "probe", "自动", "无人")):
            checks.append(f"审查人缺位或是自指（读到 {reviewer!r}）")
        claimed = record_field_value(body, "覆盖面")
        claimed_count = re.match(r"(\d+)", claimed)
        if claimed_count is None or int(claimed_count.group(1)) != len(files):
            checks.append(f"覆盖面写 {claimed!r}，实测该包 {len(files)} 个 py 文件")
        pinned = record_field_value(body, "代码面摘要")
        if not re.fullmatch(r"[0-9a-f]{64}", pinned):
            checks.append(f"代码面摘要不是 64 位十六进制（读到 {pinned!r}）")
        elif pinned != provider_py_digest(ctx.root, tuple(files)):
            checks.append("代码面摘要与该包当前字节不符（档案审的不是现在这份代码）")
        if len(record_field_value(body, "方法与反证")) < 12:
            checks.append("方法与反证一栏没写实")
        bound = [
            line
            for line in body.splitlines()
            if RECORD_CONCLUSION_MARK in line
            and CLEAN_ROOM_PHRASE in line
            and re.search(rf"\b{re.escape(name)}\b", line)
            and not any(token in line for token in RECORD_NEGATIONS)
        ]
        if not bound:
            checks.append(
                f"没有一行同时含「{RECORD_CONCLUSION_MARK}」+ 包名 + 「{CLEAN_ROOM_PHRASE}」，"
                "否定式披露不算声明"
            )
        if checks:
            refusals.append(f"{name}: " + "；".join(checks))
            continue
        credited.append(name)
        binds += len(bound)
    return credited, binds, refusals


def legacy_prose_record_face(ctx: Context, names: Sequence[str]) -> tuple[list[str], int]:
    """The pre-C77 口径, kept only as a printed contrast: any docs/evidence line that says both.

    Whole-file matching was never enough, and same-line matching turned out not to be enough
    either: a line reporting that the record is missing quotes the phrase and names every package,
    so this face credits the gap for having been described. Measured on C77's tree it returns
    7 packages over 8 lines from three files -- one C64 audit restatement and two dumps of this
    probe's own readings. It is no longer in any judge.
    """
    bodies = record_bodies(tuple(ctx.tracked()))
    named: set[str] = set()
    binds = 0
    for body in bodies:
        for line in body.splitlines():
            if CLEAN_ROOM_PHRASE not in line:
                continue
            hits = [name for name in names if re.search(rf"\b{re.escape(name)}\b", line)]
            if hits:
                binds += 1
                named.update(hits)
    return sorted(named), binds


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
    records, binds, refusals = canonical_clean_room_records(ctx, names)
    legacy_named, legacy_binds = legacy_prose_record_face(ctx, names)
    doc = ctx.read(CLEAN_ROOM_DOC_REL)
    return {
        "provider_pkgs": count(len(names)),
        "provider_list": ", ".join(names) or "-",
        "rule_written": flag(CLEAN_ROOM_PHRASE in doc and "留痕要求" in doc),
        "rule_extends_ast": flag("零 openbb" in doc and "AST 断言" in doc),
        "declared_pkgs": count(len(declared)),
        "declared_list": ", ".join(declared) or "-",
        "recorded_pkgs": count(len(records)),
        "recorded_binds": count(binds),
        "recorded_list": ", ".join(records) or "-",
        "record_refusals": count(len(refusals)),
        "record_refusal_detail": " | ".join(refusals) or "-",
        "legacy_record_pkgs": count(len(legacy_named)),
        "legacy_record_binds": count(legacy_binds),
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
        and number(facts["recorded_binds"]) >= want
        and number(facts["record_refusals"]) == 0
    )
    readings = (
        f"自研 provider 包 {facts['provider_pkgs']} 个（git 跟踪清单里带 registration.py 的"
        f"目录数）：{facts['provider_list']}",
        f"合规规则写在 {CLEAN_ROOM_DOC_REL}：留痕要求里点着「{CLEAN_ROOM_PHRASE}」= "
        f"{facts['rule_written']}；同文档 §6 把 AST 断言从「零 akshare」扩展到「零 openbb」= "
        f"{facts['rule_extends_ast']}",
        f"提交说明带这句声明的包 {facts['declared_pkgs']} 个（{facts['declared_list']}），"
        f"没带的 {facts['undeclared_list']}",
        f"逐包档案（{CLEAN_ROOM_RECORD_ROOT}/<包名>.md：一行同时含「{RECORD_CONCLUSION_MARK}」+"
        f"包名+「{CLEAN_ROOM_PHRASE}」且不含否定式标记，另有审查日期/审查人/方法与反证三栏写实，"
        f"覆盖面与代码面摘要按该包 py 文件实测重算）覆盖 {facts['recorded_pkgs']} 个包、绑定行 "
        f"{facts['recorded_binds']} 行：{facts['recorded_list']}",
        f"被拒 {facts['record_refusals']} 个；拒因：{facts['record_refusal_detail']}",
        f"旧口径（docs/evidence/ 散文同行匹配）会记 {facts['legacy_record_pkgs']} 个包 / "
        f"{facts['legacy_record_binds']} 行；C77 实测其来源是 C64 的缺口复述与 C65 的探针读数转储，"
        "即「把『记录不存在』那句话说成记录」，故 C77 起只作对照、不进判定",
        f"另有 {facts['code_declared_pkgs']} 个包把「{CODE_CLEAN_ROOM_PHRASE}」写进了自己模块的"
        " docstring —— 那是代码里的自声明，不是逐包审查记录，本条按字面不认",
    )
    reason = (
        ""
        if ok
        else "「审查留档」这一面从没成文：规则写在合规文档里（提交说明声明 + 对照表记"
        "上游事实来源），"
        f"但 {facts['provider_pkgs']} 个自研 provider 里提交说明带「{CLEAN_ROOM_PHRASE}」的只有 "
        f"{facts['declared_pkgs']} 个（缺：{facts['undeclared_list']}），"
        f"{CLEAN_ROOM_RECORD_ROOT}/ 下通过同行声明与字段核对的逐包档案只有 "
        f"{facts['recorded_pkgs']} 个（被拒 {facts['record_refusals']} 个；拒因："
        f"{facts['record_refusal_detail']}）。旧口径读到的 "
        f"{facts['legacy_record_pkgs']} 个包 / {facts['legacy_record_binds']} 行是探针自己的读数"
        "转储与缺口复述在冒充记录，C77 已把它请出判定；模块 docstring 里的自声明（"
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
    records, binds, refusals = canonical_clean_room_records(ctx, names)
    legacy_named, legacy_binds = legacy_prose_record_face(ctx, names)
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
        "recorded_pkgs": count(len(records)),
        "recorded_binds": count(binds),
        "record_refusals": count(len(refusals)),
        "record_refusal_detail": " | ".join(refusals) or "-",
        "legacy_record_pkgs": count(len(legacy_named)),
        "legacy_record_binds": count(legacy_binds),
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
        and number(facts["recorded_binds"]) >= number(facts["recorded_pkgs"])
        and number(facts["record_refusals"]) == 0
    )
    readings = (
        f"运行时发货面（{', '.join(RUNTIME_PY_ROOTS)}，搬运层自 C66 起在 opendata 之内）"
        f"跟踪的 py 文件 {facts['runtime_py_files']} 个，"
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
        f"{facts['declared_pkgs']}/{facts['provider_pkgs']} 个；{CLEAN_ROOM_RECORD_ROOT}/ 下"
        f"通过同行声明 + 字段核对（覆盖面与代码面摘要按实测重算）的逐包人工抽查留档覆盖 "
        f"{facts['recorded_pkgs']}/{facts['provider_pkgs']} 个（同一行绑定 "
        f"{facts['recorded_binds']} 行，被拒 {facts['record_refusals']} 个）",
        f"留档拒因：{facts['record_refusal_detail']}",
        f"旧口径（docs/evidence/ 散文同行匹配，C77 起只作对照）会记 "
        f"{facts['legacy_record_pkgs']}/{facts['provider_pkgs']} 个包 / "
        f"{facts['legacy_record_binds']} 行，其来源实为缺口复述与探针读数转储",
    )
    if ok:
        reason = ""
    elif walked != tracked:
        reason = (
            f"AST 走查只成功解析 {walked}/{tracked} 个运行时文件，剩下的文件被静默跳过，"
            "「0 个 openbb import 根」就不是全库读数，不能当否定证据；先让尺子覆盖完整"
            "再谈这条"
        )
    elif number(facts["recorded_binds"]) < number(facts["recorded_pkgs"]):
        reason = (
            f"留档计数 {facts['recorded_pkgs']} 个包却没有同行的绑定行（读到 "
            f"{facts['recorded_binds']} 行）—— 那说明计数来自整份文档的一次性引用，"
            "或来自探针自己写下的档案，不是逐包人工比对记录"
        )
    else:
        reason = (
            "「全库无 OpenBB 源码或其近似复制」的否定面成立（AST 走查 0 个 openbb import 根，"
            "扫描器禁止清单含 openbb 且自带会响的违规样本），但判据括号里点名的两项核查都没做过："
            f"提交声明核查 {facts['declared_pkgs']}/{facts['provider_pkgs']}，人工抽查留档 "
            f"{facts['recorded_pkgs']}/{facts['provider_pkgs']}（被拒 "
            f"{facts['record_refusals']} 个：{facts['record_refusal_detail']}）。"
            "「近似复制」只能靠逐包人工比对判定，"
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


AC16_VENDOR_TREE = Path("opendata/data/providers/akshare/_vendor")
AC16_EXPECTED_PYTHON_FILES = 325
AC16_EXPECTED_RESOURCE_FILES = 2
AC16_VENDOR_PREFIX = "opendata.data.providers.akshare._vendor"
AC16_NAMESPACE_NAMES = (
    "opendata",
    "opendata.data",
    "opendata.data.providers",
    "opendata.data.providers.akshare",
)
AC16_ENTRY_SOURCES = {
    "opendata.data.providers.akshare._vendor.datasets": "datasets.py",
    "opendata.data.providers.akshare._vendor.stock.cons": "stock/cons.py",
    "opendata.data.providers.akshare._vendor.utils": "utils/__init__.py",
}
AC16_CONTROL_MODULES = {
    "core_database": "opendata.core",
    "sibling_provider": "opendata.data.providers.bls",
    "client_root": "opendata_client",
}
AC16_BLOCKED_IMPORTS = frozenset(AC16_CONTROL_MODULES.values())
AC16_HASH_PINS = (
    "manifest_sha256",
    "manifest_file_identity_sha256",
    "manifest_resource_identity_sha256",
    "source_tree_sha256",
    "resource_tree_sha256",
)


def _ac16_json(value: object) -> str:
    """Encode one audit detail as a stable fact, including malformed/missing values."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return "null"


def _ac16_decode(value: str) -> object:
    """Decode an audit detail fact without letting malformed evidence raise in the judge."""
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _ac16_path_within(candidate: object, root: object) -> bool:
    """Whether a reported filesystem path resolves at or below the reported root."""
    if not isinstance(candidate, str) or not candidate or not isinstance(root, str) or not root:
        return False
    try:
        path = Path(candidate).resolve()
        base = Path(root).resolve()
        return path == base or base in path.parents
    except (OSError, RuntimeError, ValueError):
        return False


def _ac16_namespace_ok(records: object, cwd: str) -> bool:
    """Verify all namespace ancestors are source-free and rooted in the isolated copy."""
    if not isinstance(records, dict) or set(records) != set(AC16_NAMESPACE_NAMES) or not cwd:
        return False
    for name in AC16_NAMESPACE_NAMES:
        record = records.get(name)
        if not isinstance(record, dict):
            return False
        expected = Path(cwd).joinpath(*name.split("."))
        location = record.get("expected_location")
        locations = record.get("search_locations")
        if (
            record.get("file", "missing") is not None
            or record.get("origin", "missing") is not None
            or not isinstance(location, str)
            or Path(location).resolve() != expected.resolve()
            or not isinstance(locations, list)
            or len(locations) != 1
            or not isinstance(locations[0], str)
            or Path(locations[0]).resolve() != expected.resolve()
        ):
            return False
    return True


def _ac16_loaded_modules_ok(records: object, cwd: str) -> bool:
    """Verify every loaded ``opendata`` module came from the isolated vendor copy."""
    if not isinstance(records, dict) or not cwd:
        return False
    required = set(AC16_NAMESPACE_NAMES) | set(AC16_ENTRY_SOURCES)
    if not required <= set(records):
        return False
    vendor_root = str(Path(cwd) / AC16_VENDOR_TREE)
    for name, record in records.items():
        if name != "opendata" and not name.startswith("opendata."):
            return False
        if not isinstance(record, dict):
            return False
        if name in AC16_NAMESPACE_NAMES:
            if (
                record.get("file", "missing") is not None
                or record.get("origin", "missing") is not None
            ):
                return False
            locations = record.get("search_locations")
            expected = str(Path(cwd).joinpath(*name.split(".")))
            if not isinstance(locations, list) or locations != [expected]:
                return False
            continue
        if name != AC16_VENDOR_PREFIX and not name.startswith(f"{AC16_VENDOR_PREFIX}."):
            return False
        for key in ("file", "origin"):
            if not _ac16_path_within(record.get(key), vendor_root):
                return False
        locations = record.get("search_locations", [])
        if not isinstance(locations, list) or any(
            not _ac16_path_within(path, vendor_root) for path in locations
        ):
            return False
    return True


def _ac16_entry_imports_ok(records: object, cwd: str) -> bool:
    """Require the three declared entry imports and pin their origins to the copied tree."""
    if not isinstance(records, dict) or set(records) != set(AC16_ENTRY_SOURCES):
        return False
    vendor_root = Path(cwd) / AC16_VENDOR_TREE
    for name, relative in AC16_ENTRY_SOURCES.items():
        record = records.get(name)
        if not isinstance(record, dict) or record.get("status") != "imported":
            return False
        expected = (vendor_root / relative).resolve()
        for key in ("file", "origin"):
            value = record.get(key)
            if not isinstance(value, str) or Path(value).resolve() != expected:
                return False
    return True


def _ac16_controls_ok(records: object) -> bool:
    """Require all deliberate boundary-crossing controls to fail with ImportError."""
    if not isinstance(records, dict) or set(records) != set(AC16_CONTROL_MODULES):
        return False
    for name, blocked_module in AC16_CONTROL_MODULES.items():
        record = records.get(name)
        if (
            not isinstance(record, dict)
            or record.get("blocked_module") != blocked_module
            or record.get("status") != "blocked_import_error"
            or record.get("error_type") != "ImportError"
        ):
            return False
    return True


def measure_ac16_04(ctx: Context) -> Facts:
    """Audit current disk sources, then import the copied vendor tree in isolation."""
    boundary = script_module("scripts/quality/vendor_independence.py")
    report = boundary.audit_vendor_boundary(ctx.root)
    if not isinstance(report, dict):
        raise ProbeError("vendor boundary audit returned a non-object report")
    counts = report.get("counts")
    pins = report.get("source_pins")
    isolation = report.get("isolation")
    crossings = report.get("ast_crossings")
    if not isinstance(counts, dict):
        counts = {}
    if not isinstance(pins, dict):
        pins = {}
    if not isinstance(isolation, dict):
        isolation = {}
    if not isinstance(crossings, list):
        crossings = []

    vendor_root = ctx.root / AC16_VENDOR_TREE
    try:
        relative_py = sorted(
            path.relative_to(vendor_root).as_posix()
            for path in vendor_root.rglob("*.py")
            if path.is_file()
        )
    except OSError:
        relative_py = []
    registrations = [name for name in relative_py if Path(name).name == "registration.py"]
    providers = [name for name in relative_py if "providers" in Path(name).parts]
    crossing_names = sorted(
        str(item.get("path", "(unknown)")) for item in crossings if isinstance(item, dict)
    )
    imports = isolation.get("imports")
    controls = isolation.get("controls")
    ancestors = isolation.get("ancestors")
    loaded = isolation.get("loaded_opendata")
    sys_path = isolation.get("sys_path")
    baseline_sys_path = isolation.get("baseline_sys_path")
    network_attempts = isolation.get("network_attempts")
    repository_path_leaks = isolation.get("repository_path_leaks")
    isolation_issues = isolation.get("issues")
    audit_issues = report.get("issues")
    cwd = isolation.get("cwd")
    actual_hash_pins = all(
        isinstance(pins.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", pins[key])
        for key in AC16_HASH_PINS
    )
    upstream_commit = pins.get("upstream_commit")
    pins_valid = (
        actual_hash_pins
        and isinstance(upstream_commit, str)
        and re.fullmatch(r"[0-9a-f]{40}", upstream_commit) is not None
    )
    namespace_ok = _ac16_namespace_ok(ancestors, cwd if isinstance(cwd, str) else "")
    loaded_ok = _ac16_loaded_modules_ok(loaded, cwd if isinstance(cwd, str) else "")
    imports_ok = _ac16_entry_imports_ok(imports, cwd if isinstance(cwd, str) else "")
    controls_ok = _ac16_controls_ok(controls)
    controls_count = len(controls) if isinstance(controls, dict) else 0
    import_count = (
        sum(
            1
            for value in imports.values()
            if isinstance(value, dict) and value.get("status") == "imported"
        )
        if isinstance(imports, dict)
        else 0
    )
    namespace_count = len(ancestors) if isinstance(ancestors, dict) else 0
    loaded_count = len(loaded) if isinstance(loaded, dict) else 0
    isolation_ok = isolation.get("valid") is True
    controls_blocked = controls_ok
    standalone_ok = isolation_ok and imports_ok and controls_ok
    malformed_crossings = [item for item in crossings if not isinstance(item, dict)]

    def audit_count(key: str) -> str:
        value = counts.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return count(value)
        return "(absent)"

    result: Facts = {
        # Compatibility facts used by existing AC16 consumers and counterfacts.
        "ported_py": audit_count("actual_python_files"),
        "reg_inside": count(len(registrations)),
        "reg_inside_names": ", ".join(registrations[:3]) or "-",
        "prov_inside": count(len(providers)),
        "prov_inside_names": ", ".join(providers[:3]) or "-",
        "crossings": audit_count("ast_crossings"),
        "crossing_names": ", ".join(crossing_names[:3]) or "-",
        "standalone_exit": count(0 if isolation.get("status") == "passed" else 1),
        "control_blocked": flag(controls_blocked),
        "standalone_failed": "; ".join(
            str(item)
            for item in [
                *(audit_issues if isinstance(audit_issues, list) else []),
                *(isolation_issues if isinstance(isolation_issues, list) else []),
            ]
        )[:500]
        or "(absent)",
        "imported_modules": count(import_count),
        "standalone_ok": flag(standalone_ok),
        # Current source, manifest, parser, and inventory evidence.
        "audit_root": str(ctx.root.resolve()),
        "vendor_root": AC16_VENDOR_TREE.as_posix(),
        "vendor_valid": flag(report.get("valid") is True),
        "manifest_present": flag((vendor_root / "manifest.json").is_file()),
        "expected_python_files": audit_count("expected_python_files"),
        "manifest_python_files": audit_count("manifest_python_files"),
        "vendor_python_files": audit_count("actual_python_files"),
        "parsed_python_files": audit_count("parsed_python_files"),
        "expected_resource_files": audit_count("expected_resource_files"),
        "manifest_resource_files": audit_count("manifest_resource_files"),
        "vendor_resource_files": audit_count("actual_resource_files"),
        "vendor_total_files": audit_count("actual_total_files"),
        "vendor_imports": audit_count("vendor_imports"),
        "namespace_imports": audit_count("namespace_imports"),
        "third_party_imports": audit_count("third_party_imports"),
        "facade_module_paths": audit_count("facade_module_paths"),
        "facade_vendor_module_paths": audit_count("facade_vendor_module_paths"),
        "facade_third_party_module_paths": audit_count("facade_third_party_module_paths"),
        "facade_third_party_targets_json": _ac16_json(report.get("facade_third_party_targets")),
        "source_pins_valid": flag(pins_valid),
        **{key: str(pins.get(key, "")) for key in AC16_HASH_PINS},
        "upstream_commit": str(upstream_commit or ""),
        "ast_crossings_json": _ac16_json(crossings),
        "audit_issues_json": _ac16_json(audit_issues),
        "audit_issue_count": count(len(audit_issues)) if isinstance(audit_issues, list) else "-1",
        "malformed_crossing_count": count(len(malformed_crossings)),
        # Isolated-import evidence. JSON facts retain the complete tool-returned inventories.
        "isolation_valid": flag(isolation_ok),
        "isolation_status": str(isolation.get("status", "")),
        "isolation_cwd": str(cwd or ""),
        "namespace_count": count(namespace_count),
        "namespace_sources_clean": flag(namespace_ok),
        "namespace_json": _ac16_json(ancestors),
        "imports_json": _ac16_json(imports),
        "controls_json": _ac16_json(controls),
        "loaded_opendata_count": count(loaded_count),
        "loaded_opendata_json": _ac16_json(loaded),
        "loaded_modules_isolated": flag(loaded_ok),
        "sys_path_json": _ac16_json(sys_path),
        "baseline_sys_path_json": _ac16_json(baseline_sys_path),
        "repository_path_leaks_json": _ac16_json(repository_path_leaks),
        "blocked_imports_json": _ac16_json(isolation.get("blocked_imports")),
        "network_attempts_json": _ac16_json(network_attempts),
        "isolation_issues_json": _ac16_json(isolation_issues),
        "entry_imports_valid": flag(imports_ok),
        "control_count": count(controls_count),
        "controls_complete": flag(controls_ok),
    }
    return result


def judge_ac16_04(facts: Facts) -> Verdict:
    """Require complete current-source and isolated-import evidence for AC16|04."""

    def get(key: str) -> str:
        return facts.get(key, "")

    namespace = _ac16_decode(get("namespace_json"))
    imports = _ac16_decode(get("imports_json"))
    controls = _ac16_decode(get("controls_json"))
    loaded = _ac16_decode(get("loaded_opendata_json"))
    sys_path = _ac16_decode(get("sys_path_json"))
    baseline_sys_path = _ac16_decode(get("baseline_sys_path_json"))
    leaks = _ac16_decode(get("repository_path_leaks_json"))
    blocked = _ac16_decode(get("blocked_imports_json"))
    network = _ac16_decode(get("network_attempts_json"))
    facade_targets = _ac16_decode(get("facade_third_party_targets_json"))
    audit_issues = _ac16_decode(get("audit_issues_json"))
    isolation_issues = _ac16_decode(get("isolation_issues_json"))
    crossings = _ac16_decode(get("ast_crossings_json"))
    cwd = get("isolation_cwd")
    repo_root = get("audit_root")
    pins_valid = (
        all(re.fullmatch(r"[0-9a-f]{64}", get(key)) is not None for key in AC16_HASH_PINS)
        and re.fullmatch(r"[0-9a-f]{40}", get("upstream_commit")) is not None
    )
    isolated_root_ok = (
        (
            Path(cwd).is_absolute()
            and not _ac16_path_within(cwd, repo_root)
            and not _ac16_path_within(repo_root, cwd)
        )
        if cwd and repo_root
        else False
    )
    paths_do_not_leak = (
        isinstance(sys_path, list)
        and isinstance(baseline_sys_path, list)
        and all(isinstance(path, str) and path for path in [*sys_path, *baseline_sys_path])
        and sys_path == [cwd, *baseline_sys_path]
        and all(not _ac16_path_within(path, repo_root) for path in sys_path)
        and isinstance(leaks, list)
        and not leaks
    )
    blocked_imports_ok = (
        isinstance(blocked, list)
        and len(blocked) == len(AC16_BLOCKED_IMPORTS)
        and frozenset(blocked) == AC16_BLOCKED_IMPORTS
    )
    no_attempts_or_issues = (
        isinstance(network, list)
        and not network
        and isinstance(audit_issues, list)
        and not audit_issues
        and isinstance(isolation_issues, list)
        and not isolation_issues
    )
    facade_ok = (
        number(get("facade_module_paths")) > 0
        and number(get("facade_vendor_module_paths")) > 0
        and number(get("facade_third_party_module_paths")) >= 0
        and isinstance(facade_targets, list)
        and all(isinstance(target, str) and target for target in facade_targets)
        and facade_targets == sorted(set(facade_targets))
        and number(get("facade_third_party_module_paths")) >= len(facade_targets)
        and number(get("facade_module_paths"))
        == number(get("facade_vendor_module_paths"))
        + number(get("facade_third_party_module_paths"))
    )
    counts_ok = (
        get("manifest_present") == "yes"
        and number(get("expected_python_files")) == AC16_EXPECTED_PYTHON_FILES
        and number(get("manifest_python_files")) == AC16_EXPECTED_PYTHON_FILES
        and number(get("vendor_python_files")) == AC16_EXPECTED_PYTHON_FILES
        and get("ported_py") == str(AC16_EXPECTED_PYTHON_FILES)
        and number(get("parsed_python_files")) == AC16_EXPECTED_PYTHON_FILES
        and number(get("expected_resource_files")) == AC16_EXPECTED_RESOURCE_FILES
        and number(get("manifest_resource_files")) == AC16_EXPECTED_RESOURCE_FILES
        and number(get("vendor_resource_files")) == AC16_EXPECTED_RESOURCE_FILES
        and number(get("vendor_total_files"))
        == AC16_EXPECTED_PYTHON_FILES + AC16_EXPECTED_RESOURCE_FILES
        and number(get("crossings")) == 0
        and isinstance(crossings, list)
        and not crossings
        and get("malformed_crossing_count") == "0"
        and get("vendor_valid") == "yes"
        and get("source_pins_valid") == "yes"
        and pins_valid
        and facade_ok
    )
    ancestors_ok = (
        get("namespace_count") == str(len(AC16_NAMESPACE_NAMES))
        and get("namespace_sources_clean") == "yes"
        and _ac16_namespace_ok(namespace, cwd)
    )
    imports_ok = (
        get("imported_modules") == str(len(AC16_ENTRY_SOURCES))
        and get("entry_imports_valid") == "yes"
        and _ac16_entry_imports_ok(imports, cwd)
    )
    controls_ok = (
        get("control_count") == str(len(AC16_CONTROL_MODULES))
        and get("controls_complete") == "yes"
        and get("control_blocked") == "yes"
        and _ac16_controls_ok(controls)
        and blocked_imports_ok
    )
    loaded_ok = (
        positive(get("loaded_opendata_count"))
        and get("loaded_modules_isolated") == "yes"
        and isinstance(loaded, dict)
        and number(get("loaded_opendata_count")) == len(loaded)
        and _ac16_loaded_modules_ok(loaded, cwd)
    )
    isolation_ok = (
        get("isolation_valid") == "yes"
        and get("isolation_status") == "passed"
        and get("standalone_exit") == "0"
        and get("standalone_ok") == "yes"
        and isolated_root_ok
        and ancestors_ok
        and imports_ok
        and controls_ok
        and loaded_ok
        and paths_do_not_leak
        and no_attempts_or_issues
    )
    ok = (
        number(get("reg_inside")) == 0
        and number(get("prov_inside")) == 0
        and counts_ok
        and isolation_ok
    )
    readings = (
        f"当前磁盘 {get('vendor_root')}：Python {get('vendor_python_files')}/"
        f"{get('manifest_python_files')}（expected {get('expected_python_files')}，parsed "
        f"{get('parsed_python_files')}），资源 {get('vendor_resource_files')}/"
        f"{get('manifest_resource_files')}；总计 {get('vendor_total_files')}；静态跨界 "
        f"{get('crossings')}；facade module paths {get('facade_module_paths')} = "
        f"vendor {get('facade_vendor_module_paths')} + external "
        f"{get('facade_third_party_module_paths')}",
        f"Manifest/source pins：manifest {get('manifest_sha256')}；file identity "
        f"{get('manifest_file_identity_sha256')}；resource identity "
        f"{get('manifest_resource_identity_sha256')}；source tree {get('source_tree_sha256')}；"
        f"resource tree {get('resource_tree_sha256')}；upstream commit {get('upstream_commit')}",
        f"真实复制树隔离导入：status={get('isolation_status')}，入口 "
        f"{get('imported_modules')}/{len(AC16_ENTRY_SOURCES)}，控制拦截 "
        f"{get('control_count')}/{len(AC16_CONTROL_MODULES)}，namespace ancestors "
        f"{get('namespace_count')}/{len(AC16_NAMESPACE_NAMES)}，加载模块 "
        f"{get('loaded_opendata_count')}；network attempts "
        f"{get('network_attempts_json')}，checkout leaks {get('repository_path_leaks_json')}",
        f"vendor 子树内自研文件：registration.py {get('reg_inside')} 个 "
        f"({get('reg_inside_names')})，providers 路径 {get('prov_inside')} 个 "
        f"({get('prov_inside_names')})；legacy import count={get('imported_modules')}，"
        f"standalone exit={get('standalone_exit')}，失败={get('standalone_failed')}",
    )
    reason = (
        ""
        if ok
        else "AC-16|04 只有在当前磁盘 manifest 与 325 个可解析 Python 文件、2 个资源、"
        "逐文件 source pins、完整静态 import/facade 检查，以及临时复制树中的真实入口导入、"
        "namespace 来源、三项拒绝控制、无网络尝试和无 checkout/sys.path 泄漏全部齐备时才通过"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def _ac16_clean_repair_facts() -> Facts:
    """A complete hypothetical clean reading used only by the judge counterfact harness."""
    isolated = "/isolated/ac16-boundary"
    repo = "/workspace/opendata"
    isolated_vendor = str(Path(isolated) / AC16_VENDOR_TREE)
    ancestors: dict[str, object] = {}
    loaded: dict[str, object] = {}
    for name in AC16_NAMESPACE_NAMES:
        location = str(Path(isolated).joinpath(*name.split(".")))
        ancestors[name] = {
            "expected_location": location,
            "file": None,
            "origin": None,
            "search_locations": [location],
        }
        loaded[name] = {"file": None, "origin": None, "search_locations": [location]}
    imports: dict[str, object] = {}
    for name, relative in AC16_ENTRY_SOURCES.items():
        path = str(Path(isolated_vendor) / relative)
        imports[name] = {"file": path, "origin": path, "status": "imported"}
        loaded[name] = {"file": path, "origin": path, "search_locations": []}
    controls = {
        name: {
            "blocked_module": module,
            "error_type": "ImportError",
            "status": "blocked_import_error",
        }
        for name, module in AC16_CONTROL_MODULES.items()
    }
    return {
        "ported_py": str(AC16_EXPECTED_PYTHON_FILES),
        "reg_inside": "0",
        "reg_inside_names": "-",
        "prov_inside": "0",
        "prov_inside_names": "-",
        "crossings": "0",
        "crossing_names": "-",
        "standalone_exit": "0",
        "control_blocked": "yes",
        "standalone_failed": "(absent)",
        "imported_modules": str(len(AC16_ENTRY_SOURCES)),
        "standalone_ok": "yes",
        "audit_root": repo,
        "vendor_root": AC16_VENDOR_TREE.as_posix(),
        "vendor_valid": "yes",
        "manifest_present": "yes",
        "expected_python_files": str(AC16_EXPECTED_PYTHON_FILES),
        "manifest_python_files": str(AC16_EXPECTED_PYTHON_FILES),
        "vendor_python_files": str(AC16_EXPECTED_PYTHON_FILES),
        "parsed_python_files": str(AC16_EXPECTED_PYTHON_FILES),
        "expected_resource_files": str(AC16_EXPECTED_RESOURCE_FILES),
        "manifest_resource_files": str(AC16_EXPECTED_RESOURCE_FILES),
        "vendor_resource_files": str(AC16_EXPECTED_RESOURCE_FILES),
        "vendor_total_files": str(AC16_EXPECTED_PYTHON_FILES + AC16_EXPECTED_RESOURCE_FILES),
        "vendor_imports": "238",
        "namespace_imports": "0",
        "third_party_imports": "1153",
        "facade_module_paths": "987",
        "facade_vendor_module_paths": "986",
        "facade_third_party_module_paths": "1",
        "facade_third_party_targets_json": '["akqmt"]',
        "source_pins_valid": "yes",
        **dict.fromkeys(AC16_HASH_PINS, "a" * 64),
        "upstream_commit": "a" * 40,
        "ast_crossings_json": "[]",
        "audit_issues_json": "[]",
        "audit_issue_count": "0",
        "malformed_crossing_count": "0",
        "isolation_valid": "yes",
        "isolation_status": "passed",
        "isolation_cwd": isolated,
        "namespace_count": str(len(AC16_NAMESPACE_NAMES)),
        "namespace_sources_clean": "yes",
        "namespace_json": _ac16_json(ancestors),
        "imports_json": _ac16_json(imports),
        "controls_json": _ac16_json(controls),
        "loaded_opendata_count": str(len(loaded)),
        "loaded_opendata_json": _ac16_json(loaded),
        "loaded_modules_isolated": "yes",
        "sys_path_json": _ac16_json([isolated, "/python/lib/python311.zip"]),
        "baseline_sys_path_json": _ac16_json(["/python/lib/python311.zip"]),
        "repository_path_leaks_json": "[]",
        "blocked_imports_json": _ac16_json(sorted(AC16_BLOCKED_IMPORTS)),
        "network_attempts_json": "[]",
        "isolation_issues_json": "[]",
        "entry_imports_valid": "yes",
        "control_count": str(len(AC16_CONTROL_MODULES)),
        "controls_complete": "yes",
    }


def _ac16_namespace_source_counterfact_json() -> str:
    """Represent a copied namespace ancestor accidentally resolving to BSL source."""
    records = _ac16_decode(_ac16_clean_repair_facts()["namespace_json"])
    if not isinstance(records, dict) or not isinstance(records.get("opendata"), dict):
        return "{}"
    records["opendata"]["file"] = "/workspace/opendata/opendata/__init__.py"
    records["opendata"]["origin"] = "/workspace/opendata/opendata/__init__.py"
    return _ac16_json(records)


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
# AC-19 运维保障（备份 / 恢复演练 / 保留策略 / Key 健康 / 配置项清单）
# --------------------------------------------------------------------------- #

AC19_BACKUP_REL: Final = "scripts/ops/backup_mysql.sh"
AC19_COMPOSE_REL: Final = "docker-compose.yml"
AC19_BACKUP_DOCKERFILE_REL: Final = "scripts/ops/Dockerfile.backup"
AC19_BACKUP_RUNNER_REL: Final = "scripts/ops/backup_runner.sh"
AC19_BR_DOC_REL: Final = "docs/operations-backup-restore.md"
AC19_DRILL_REL: Final = "docs/evidence/A0/restore-drill.txt"
AC19_A0_README_REL: Final = "docs/evidence/A0/README.md"
AC19_RETENTION_REL: Final = "opendata/pipeline/retention.py"
AC19_MAINTENANCE_REL: Final = "opendata/pipeline/maintenance.py"
AC19_KEYHEALTH_REL: Final = "opendata/pipeline/key_health.py"
AC19_KEYNOTIFY_REL: Final = "opendata/pipeline/key_health_notifications.py"
AC19_PATROL_REL: Final = "opendata/pipeline/patrol.py"
AC19_CONFIG_REL: Final = "opendata/core/config.py"
AC19_CONFIG_DOC_REL: Final = "docs/配置项清单.md"
#: 同档另有两张从 `| 1 |` 起头的表（§4 缺陷 8 行、§5 移交 3 行）；只有这张头是检查单。
AC19_DRILL_HEADER: Final = "| # | 步骤 | 通过判据 | 结果 |"
#: 清单文档的在用表头；第十节「已被移除的配置」只有两列，不计入在配置项。
AC19_LIVE_HEADER: Final = "| 键名 | 含义 | 默认值 | 改动影响 |"
AC19_RETENTION_FUNCS: Final = (
    "purge_expired_rows",
    "purge_diff_report",
    "purge_minute_archives",
    "purge_raw_response_cache",
)
AC19_RETENTION_KEYS: Final = (
    "CACHE_DIR",
    "CACHE_TTL_SECONDS",
    "RETENTION_DIFF_REPORT_DAYS",
    "RETENTION_MINUTE_YEARS",
)
AC19_RETENTION_NODES: Final = (
    "tests/test_pipeline_jobs.py::TestExecuteTemplate::"
    "test_retention_job_is_report_only_unless_explicitly_enabled",
)
AC19_OBSERVATION_NODES: Final = (
    "tests/test_key_health_notifications.py::TestRecentObservationStore::"
    "test_ttl_expiry_is_unknown_and_is_not_a_confirmed_recovery",
    "tests/test_key_health_notifications.py::TestKeyHealthNotifier::"
    "test_ttl_expiry_event_is_not_labeled_as_recovery",
    "tests/test_key_health_notifications.py::TestKeyHealthNotifier::"
    "test_unchanged_alert_is_not_sent_twice_and_quota_stays_unknown",
)
#: key_health.py:119 自己列出的四类：没有主动探测就只是未验证。
AC19_KEY_CLASSES: Final = ("key-validity", "key-expiry", "revocation-or-ban", "quota-left")
AC19_SCAN_SUFFIXES: Final = (".py", ".sh", ".yml", ".yaml", ".json", ".toml", ".ini", ".cfg")
AC19_SCAN_SKIP_PREFIXES: Final = ("tests/", "docs/", "web/", "frontend/")
#: 仪器自己也算被扫面：C62 的机读回声——探针里的路径字符串不是仓库里的调度定义。
PROBE_SELF_REL: Final = "scripts/quality/acceptance_item_probe.py"
#: 只认实测取值（`log_bin = ON` / `log_bin: OFF`）。`ON|OFF` 是格式串里的备选，不是读数。
BINLOG_VALUE: Final = re.compile(r"log_bin\s*[:=]\s*(?:ON|OFF)(?!\|)", re.I)
#: 「实测取值」的语料面：档案目录与结论面都不能当语料。档案是每轮的过程留痕（本轮 README 里举的
#: 反例形状就是一例 —— 任何一轮写过一次取值形状，后面每一轮都会被它污染），台账与验收文档写的
#: 正是本轮的结论。取值该落在运维手册这类长期事实面里，这也是 |01 的 `repair` 落点指向手册的原因。
ATTESTATION_SKIP_PREFIXES: Final = ("docs/evidence/", "docs/quality/", "docs/迭代计划/")


def code_lines(text: str) -> list[str]:
    """Non-comment lines of a file, so prose never counts as a rule."""
    keep: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keep.append(line)
    return keep


def mysqldump_invocations(script: str) -> int:
    r"""Real ``mysqldump`` invocations, not the lines that merely name the tool.

    The script's preflight loop and its two error echoes all contain the word; only
    ``if ! mysqldump \\`` runs it. Counting the word would read 4 and call a broken script fine.
    """
    return sum(1 for line in code_lines(script) if re.match(r"^(?:if ! )?mysqldump\b", line))


def ac19_tracked(ctx: Context) -> list[str]:
    r"""Tracked paths with git's octal quoting undone, read from ``git ls-files -z``.

    Plain ``git ls-files`` prints ``"docs/\351\205\205..."`` for a Chinese file name, so a test
    against the real path can never be true and a ``docs/`` prefix filter never matches either:
    AC-19|05 read 0 key rows out of a tracked 115-line document until this was measured.
    """
    code, out = run_argv(["git", "ls-files", "-z"])
    if code != 0:
        raise ProbeError(f"git ls-files -z failed: {out.strip()[:120]}")
    return sorted(part for part in out.split("\x00") if part)


def schedule_sites_for(ctx: Context, paths: list[str], needle: str) -> list[str]:
    """Tracked files that actually invoke ``needle`` on a non-comment line.

    AC-19|01 asks for 每日备份, so only a live trigger counts: a crontab example inside the
    script's own header is documentation, and prose under docs/ is a runbook, not a scheduler.
    The instrument's own file is excluded too -- its ``AC19_BACKUP_REL`` literal is a non-comment
    line holding the needle, which is C62's machine echo in a new costume.
    """
    hits: list[str] = []
    for rel in paths:
        if rel in {AC19_BACKUP_REL, PROBE_SELF_REL} or rel.startswith(AC19_SCAN_SKIP_PREFIXES):
            continue
        leaf = rel.rsplit("/", 1)[-1]
        if leaf != "Makefile" and not rel.endswith(AC19_SCAN_SUFFIXES):
            continue
        if any(needle in line for line in code_lines(ctx.read(rel))):
            hits.append(rel)
    return hits


def measure_ac19_01(ctx: Context) -> Facts:
    """Read script, both-library dumps, the daily trigger, the RPO claim, binlog readings.

    Five faces are read separately so a gap names which one is missing.
    """
    paths = ac19_tracked(ctx)
    script = ctx.read(AC19_BACKUP_REL) if AC19_BACKUP_REL in set(paths) else ""
    doc = ctx.read(AC19_BR_DOC_REL)
    compose = yaml.safe_load(ctx.read(AC19_COMPOSE_REL))
    services = compose.get("services") if isinstance(compose, dict) else None
    backup_service = services.get("backup") if isinstance(services, dict) else None
    build = backup_service.get("build") if isinstance(backup_service, dict) else None
    profiles = backup_service.get("profiles", []) if isinstance(backup_service, dict) else []
    backup_profile = flag(isinstance(profiles, list) and "backup" in profiles)
    backup_image_src = ctx.read(AC19_BACKUP_DOCKERFILE_REL)
    backup_runner = ctx.read(AC19_BACKUP_RUNNER_REL)
    dockerfile = build.get("dockerfile") if isinstance(build, dict) else ""
    backup_image = flag(
        dockerfile == AC19_BACKUP_DOCKERFILE_REL
        and "COPY scripts/ops/backup_mysql.sh" in backup_image_src
        and "COPY scripts/ops/backup_runner.sh" in backup_image_src
        and 'ENTRYPOINT ["/opendata/scripts/ops/backup_runner.sh"]' in backup_image_src
    )
    runner_lines = code_lines(backup_runner)
    daily_runner = flag(
        any(line == "while true; do" for line in runner_lines)
        and any("read -r hour minute second" in line and "date -u" in line for line in runner_lines)
        and any(
            line == "now_seconds=$((10#$hour * 3600 + 10#$minute * 60 + 10#$second))"
            for line in runner_lines
        )
        and any(line == "target_seconds=$((2 * 3600))" for line in runner_lines)
        and any(
            line == "wait_seconds=$(((target_seconds - now_seconds + 86400) % 86400))"
            for line in runner_lines
        )
        and any(line == "if ((wait_seconds == 0)); then" for line in runner_lines)
        and any(line == "wait_seconds=86400" for line in runner_lines)
        and any(line == 'sleep "$wait_seconds"' for line in runner_lines)
        and any(line == "/opendata/scripts/ops/backup_mysql.sh" for line in runner_lines)
    )
    schedule_chain = flag(
        backup_profile == "yes" and backup_image == "yes" and daily_runner == "yes"
    )
    labels = re.findall(
        r'^dump_one\s+"[^"]+"\s+"[^"]+"\s+"[^"]+"\s+(metadata|warehouse)\b',
        script,
        re.M,
    )
    schedule_paths = sorted(set(paths) | {AC19_BACKUP_RUNNER_REL})
    sites = schedule_sites_for(ctx, schedule_paths, "backup_mysql")
    # 「binlog 生效」要一次实测取值：文档第 49 行 `log_bin = /var/lib/...` 是配置片段，格式串
    # `ON|OFF` 也不是取值；语料再剔掉档案面与结论面（见 ATTESTATION_SKIP_PREFIXES）—— 复算时
    # 本轮自己写的「0 份档案」那句话会被读成 4 份。
    attested = [
        rel
        for rel in paths
        if rel.startswith("docs/")
        and not rel.startswith(ATTESTATION_SKIP_PREFIXES)
        and BINLOG_VALUE.search(ctx.read(rel))
    ]
    doc_lines = doc.splitlines()
    isolated = ctx.read("docs/evidence/C64/backup-restore.txt")
    deployment_path = "docs/evidence/C64/backup-deployment-live.json"
    try:
        deployed = (
            json.loads(ctx.read(deployment_path)) if (ctx.root / deployment_path).is_file() else {}
        )
    except json.JSONDecodeError:
        deployed = {}
    if not isinstance(deployed, dict):
        deployed = {}
    deployment_age = deployed.get("last_successful_backup_age_hours")
    return {
        "scan_population": count(len(paths)),
        "attestation_excluded": ", ".join(ATTESTATION_SKIP_PREFIXES),
        "script_tracked": flag(bool(script)),
        "dump_calls": count(len(re.findall(r"^dump_one\s", script, re.M))),
        "dump_labels": ", ".join(labels) or "-",
        "dump_sites": count(mysqldump_invocations(script)),
        "schedule_sites": count(len(sites)),
        "schedule_list": ", ".join(sorted(set(sites))[:4]) or "-",
        "backup_profile": backup_profile,
        "backup_image": backup_image,
        "daily_runner": daily_runner,
        "schedule_chain": schedule_chain,
        "doc_rpo": flag(any("RPO" in ln and "24" in ln for ln in doc_lines)),
        "doc_binlog": flag(any("log_bin" in ln or "ROW binlog" in ln for ln in doc_lines)),
        "binlog_optional": flag(any("binlog" in ln.lower() and "可选" in ln for ln in doc_lines)),
        "binlog_attested": count(len(attested)),
        "binlog_attested_list": ", ".join(sorted(set(attested))[:3]) or "-",
        "isolated_binlog": flag(
            "REVIEW_EXIT=0" in isolated
            and "'log_bin': 'ON'" in isolated
            and "'binlog_format': 'ROW'" in isolated
        ),
        "deployment_observed": flag(
            deployed.get("scope") == "independent-live-backup-deployment"
            and deployed.get("backup_service_running") is True
            and deployed.get("log_bin") == "ON"
            and isinstance(deployment_age, (int, float))
            and not isinstance(deployment_age, bool)
            and 0 <= deployment_age <= 24
            and bool(deployed.get("delivery_evidence"))
        ),
    }


def judge_ac19_01(facts: Facts) -> Verdict:
    """``AC-19|01``: daily backups are triggered from the repo and binlog is measured."""
    ok = (
        facts["script_tracked"] == "yes"
        and facts["dump_calls"] == "2"
        and number(facts["dump_sites"]) >= 1
        and number(facts["schedule_sites"]) >= 1
        and facts["backup_profile"] == "yes"
        and facts["backup_image"] == "yes"
        and facts["daily_runner"] == "yes"
        and facts["schedule_chain"] == "yes"
        and facts["doc_rpo"] == "yes"
        and facts["doc_binlog"] == "yes"
        and number(facts["binlog_attested"]) >= 1
        and facts["deployment_observed"] == "yes"
    )
    readings = (
        f"备份脚本在跟踪清单 = {facts['script_tracked']}；dump_one 调用 "
        f"{facts['dump_calls']} 次（{facts['dump_labels']}），mysqldump 真实调用 "
        f"{facts['dump_sites']} 处（前置检查那行不计）",
        f"扫描人口 {facts['scan_population']} 个跟踪文件（tests/docs/web、脚本自身与探针自身已"
        "排除，且读 `git ls-files -z`：默认输出会把中文文件名八进制转义）；每日触发点 "
        f"{facts['schedule_sites']} 处（{facts['schedule_list']}）；"
        "脚本头部注释里那行 `0 2 * * *` 不算触发点（被调方不能当自己的证据）",
        f"Compose `backup` profile = {facts['backup_profile']}（按需启动）；镜像链 = "
        f"{facts['backup_image']}；runner 每日 02:00 UTC 循环 = {facts['daily_runner']}；"
        f"完整接线 = {facts['schedule_chain']}（源码配置不证明当前部署已启动）",
        f"RPO ≤24h 的声明面 = {facts['doc_rpo']}；binlog 写进手册 = {facts['doc_binlog']}，"
        f"手册把它列为可选附录 = {facts['binlog_optional']}",
        f"实测过的 `log_bin` 取值（只认 ON/OFF，格式串里的 `ON|OFF` 备选不算）"
        f"{facts['binlog_attested']} 份档案（{facts['binlog_attested_list']}）；语料剔除面 "
        f"{facts['attestation_excluded']} —— 档案面与结论面都不算实测，探针离线不连库，"
        "配置片段不算读数",
        f"C64 isolated MySQL ROW binlog measured={facts['isolated_binlog']}; "
        f"actual scheduled deployment and backup freshness={facts['deployment_observed']}; "
        "isolated restoration does not establish production RPO",
    )
    if ok:
        reason = ""
    elif number(facts["schedule_sites"]) < 1:
        reason = (
            "判据要「每日备份」，现场是脚本、手册、一次真演练都在位，而**每日触发点 0 处**："
            f"仓库里没有任何调度定义或 make 目标去跑 {AC19_BACKUP_REL}，只有它自己头部注释里"
            "一行 crontab 示例和手册里同一行。dump "
            f"{facts['dump_calls']} 次（{facts['dump_labels']}）、RPO 声明 {facts['doc_rpo']}、"
            f"binlog 手册 {facts['doc_binlog']}（列为可选 = {facts['binlog_optional']}）"
        )
    elif number(facts["binlog_attested"]) < 1 or facts["deployment_observed"] != "yes":
        reason = (
            "每日触发源码和隔离双库恢复已验证，隔离 ROW binlog 取值="
            f"{facts['isolated_binlog']}；正式部署中每日任务运行、备份新鲜度及 binlog "
            f"实际生效证据={facts['deployment_observed']}，生产 RPO ≤24h 尚未验收"
        )
    else:
        reason = (
            f"脚本 {facts['script_tracked']}、dump {facts['dump_calls']} 次"
            f"（{facts['dump_labels']}）、真实调用 {facts['dump_sites']} 处、触发点 "
            f"{facts['schedule_sites']} 处、RPO {facts['doc_rpo']}、binlog 手册 "
            f"{facts['doc_binlog']}、实测 {facts['binlog_attested']} —— 其中一项不成立"
        )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def drill_checklist(body: str) -> list[str]:
    """Rows of the drill checklist table only.

    The same archive later numbers two more tables from ``| 1 |`` (8 defects, 3 hand-offs); counting
    alone read 15 rows and made a six-step checklist look unproven.
    """
    lines = body.splitlines()
    head = -1
    for index, line in enumerate(lines):
        if line.startswith(AC19_DRILL_HEADER):
            head = index
            break
    if head < 0:
        return []
    rows: list[str] = []
    for line in lines[head + 2 :]:
        if not line.startswith("|"):
            break
        rows.append(line)
    return rows


def measure_ac19_02(ctx: Context) -> Facts:
    """Read the drill archive: checklist rows, restore targets, and its own age.

    The record must predate this round -- C62's machine-readable echo lesson.
    """
    body = ctx.read(AC19_DRILL_REL) if AC19_DRILL_REL in set(ac19_tracked(ctx)) else ""
    rows = drill_checklist(body)
    code, out = run_argv(
        ["git", "log", "--diff-filter=A", "--format=%H %ad", "--date=short", "--", AC19_DRILL_REL]
    )
    first = out.strip().splitlines()[0] if code == 0 and out.strip() else "-"
    stamp = first.rsplit(" ", 1)[-1] if first != "-" else "-"
    a0 = ctx.read(AC19_A0_README_REL)
    return {
        "drill_tracked": flag(bool(body)),
        "checklist_found": flag(bool(rows)),
        "drill_commit": first.split(" ", 1)[0][:12] if first != "-" else "-",
        "drill_added": stamp,
        "drill_preexisting": flag(stamp not in {"-", ""} and stamp < time.strftime("%Y-%m-%d")),
        "steps": count(len(rows)),
        "steps_passed": count(sum(1 for row in rows if "✅" in row)),
        "isolated_dbs": count(
            sum(
                1
                for name in ("opendata_restore_check", "opendata_data_restore_check")
                if name in body
            )
        ),
        "rowcmp": flag("对象一致" in body),
        "health": flag("status=healthy" in body),
        "in_a0_dod": flag("restore-drill" in a0 or "恢复演练" in a0),
    }


def judge_ac19_02(facts: Facts) -> Verdict:
    """``AC-19|02``: one restore drill is recorded, and the record is not this round's."""
    ok = (
        facts["drill_tracked"] == "yes"
        and facts["checklist_found"] == "yes"
        and facts["drill_preexisting"] == "yes"
        and facts["steps"] == "6"
        and facts["steps_passed"] == "6"
        and facts["isolated_dbs"] == "2"
        and facts["rowcmp"] == "yes"
        and facts["health"] == "yes"
        and facts["in_a0_dod"] == "yes"
    )
    readings = (
        f"演练档案 {AC19_DRILL_REL} 在跟踪清单 = {facts['drill_tracked']}，检查单表头读得到 = "
        f"{facts['checklist_found']}（同档另有两张从 `| 1 |` 起头的表 —— §4 缺陷 8 行、"
        "§5 移交 3 行；判定只看这张表头之下的行）",
        f"首次入库 {facts['drill_added']}（提交 {facts['drill_commit']}），早于本轮 = "
        f"{facts['drill_preexisting']} —— 一轮不能给自己的演练记录背书（C62 的机读回声教训）",
        f"检查单 {facts['steps']} 步，带 ✅ 标记 {facts['steps_passed']} 步",
        f"两个隔离恢复库均可指认 {facts['isolated_dbs']}/2；关键表行数比对 = {facts['rowcmp']}，"
        f"起栈后 /health 读到 status=healthy = {facts['health']}",
        f"A0 DoD 里点着这份档案 = {facts['in_a0_dod']}",
    )
    reason = (
        ""
        if ok
        else (
            f"「完成一次并记录」现在读到 {facts['steps_passed']}/{facts['steps']} 步带通过标记、"
            f"检查单表头 {facts['checklist_found']}、恢复库 {facts['isolated_dbs']}/2、行数比对 "
            f"{facts['rowcmp']}、健康检查 {facts['health']}、DoD 归属 {facts['in_a0_dod']}、"
            f"档案首次入库 {facts['drill_added']}（早于本轮 = {facts['drill_preexisting']}）"
        )
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def retention_callers(ctx: Context) -> list[str]:
    """Runtime modules (never tests/) that actually call one of the purge executors."""
    hits: list[str] = []
    runtime = runtime_py_files_with_optional(ctx, (AC19_MAINTENANCE_REL,))
    for rel in runtime:
        if rel == AC19_RETENTION_REL:
            continue
        body = ctx.read(rel)
        if any(re.search(rf"\b{name}\(", body) for name in AC19_RETENTION_FUNCS):
            hits.append(rel)
    return hits


def measure_ac19_03(ctx: Context) -> Facts:
    """Read policy, executors, config keys and call sites as four separate faces.

    Only the call-site face says the policy is *implemented* rather than declared.
    """
    src = ctx.read(AC19_RETENTION_REL)
    cfg = ctx.read(AC19_CONFIG_REL)
    jobs = ctx.read(PIPELINE_JOBS_REL)
    schedule = yaml.safe_load(ctx.read(SCHEDULES_REL))
    templates = schedule.get("templates") if isinstance(schedule, dict) else None
    rows = templates if isinstance(templates, list) else []
    schedule_row = flag(
        any(
            isinstance(row, dict)
            and row.get("name") == "retention-maintenance"
            and row.get("kind") == "retention"
            for row in rows
        )
    )
    executable = flag("TemplateKind.RETENTION" in set_literal_members(jobs, "EXECUTABLE_KINDS"))
    dispatcher = function_body(jobs, "_execute_template")
    dispatched = flag("TemplateKind.RETENTION" in dispatcher and "_execute_retention" in dispatcher)
    executor = function_body(jobs, "_execute_retention")
    dry_run_default = flag(
        bool(
            re.search(
                r"retention_execution_enabled:\s*bool\s*=\s*Field\(\s*default=False",
                cfg,
            )
        )
        and "run_retention" in executor
        and "dry_run=not settings.retention_execution_enabled" in executor
    )
    retention_results = outcomes(AC19_RETENTION_NODES)
    defs = [name for name in AC19_RETENTION_FUNCS if re.search(rf"^def {name}\(", src, re.M)]
    callers = retention_callers(ctx)
    return {
        "policy_kinds": count(
            sum(1 for marker in ("永久", "keep_years", "ttl_", "diff_report") if marker in src)
        ),
        "executors": count(len(defs)),
        "executor_list": ", ".join(defs) or "-",
        "config_keys": count(sum(1 for key in AC19_RETENTION_KEYS if key in cfg)),
        "prod_callers": count(len(callers)),
        "caller_list": ", ".join(sorted(callers)[:4]) or "-",
        "schedule_row": schedule_row,
        "kind_executable": executable,
        "dispatcher": dispatched,
        "report_only_default": dry_run_default,
        "report_only_test": bad_of(retention_results),
    }


def judge_ac19_03(facts: Facts) -> Verdict:
    """``AC-19|03``: the retention policy is declared *and* reachable from runtime code."""
    ok = (
        facts["policy_kinds"] == "4"
        and facts["executors"] == "4"
        and facts["config_keys"] == "4"
        and number(facts["prod_callers"]) >= 1
        and facts["schedule_row"] == "yes"
        and facts["kind_executable"] == "yes"
        and facts["dispatcher"] == "yes"
        and facts["report_only_default"] == "yes"
        and facts["report_only_test"] == "-"
    )
    readings = (
        f"策略四类读到 {facts['policy_kinds']}/4（日线永久 / 分钟线 N 年 / 缓存 TTL / "
        f"差异明细导出），执行器 {facts['executors']}/4（{facts['executor_list']}），"
        f"四个配置键 {facts['config_keys']}/4",
        f"生产侧调用点 {facts['prod_callers']} 处（{facts['caller_list']}）—— "
        "tests/ 里的引用不算调用点，retention.py 自己的内部转调也不算",
        f"调度行 = {facts['schedule_row']}，kind 可执行 = {facts['kind_executable']}，"
        f"派发到执行器 = {facts['dispatcher']}；默认 report-only = "
        f"{facts['report_only_default']}（RETENTION_EXECUTION_ENABLED 默认 false，"
        f"显式开启才删除）；命名行为用例失败项 = {facts['report_only_test']}（- 表示全部通过）",
    )
    reason = (
        ""
        if ok
        else (
            f"策略 {facts['policy_kinds']}/4、执行器 {facts['executors']}/4、配置键 "
            f"{facts['config_keys']}/4、运行时调用点 {facts['prod_callers']}；还要求调度行、"
            "可执行 kind、jobs 派发以及默认 report-only 行为都成立。细项读数见上方各面"
        )
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def key_expiry_signal(src: str) -> bool:
    """Does anything classify or look up an *upstream* Key's expiry?

    ``opendata/services/api_key_service.py:is_expired`` and ``core/token_blacklist.py`` are about
    Keys this platform issues to its own clients, so they are excluded rather than counted: the
    criterion names 权威源 Key.
    """
    if re.search(r'CLASS_[A-Z_]+\s*=\s*"[^"]*expir', src):
        return True
    return bool(re.search(r"^def \w*(expir|expiry)\w*\(", src, re.M))


def status_class_rules(src: str) -> tuple[bool, bool]:
    """Are 401/403→credential-rejected and 429→quota-exhausted present as *statements*?

    ``_class_for_status``'s docstring spells the same numbers out in prose, so matching the whole
    function body would read the comment as the rule. Only ``if``/``return`` lines are paired.
    """
    body: list[str] = []
    inside = False
    for line in src.splitlines():
        if line.startswith("def _class_for_status"):
            inside = True
            continue
        if inside:
            if line.startswith(("def ", "@", "class ")):
                break
            body.append(line.strip())
    stmts = [line for line in body if line.startswith(("if ", "elif ", "return "))]
    rejected = quota = False
    for index, stmt in enumerate(stmts[:-1]):
        nxt = stmts[index + 1]
        if stmt.startswith("if status in (401, 403)") and nxt == "return CLASS_CREDENTIAL_REJECTED":
            rejected = True
        if stmt == "if status == 429:" and nxt == "return CLASS_QUOTA_EXHAUSTED":
            quota = True
    return rejected, quota


def measure_ac19_04(ctx: Context) -> Facts:
    """Read the four Key classes, the wire rules that can fire, and where the grade lands."""
    src = ctx.read(AC19_KEYHEALTH_REL)
    rejected, quota = status_class_rules(src)
    # patrol.py 是 credential_health 的定义处，不能当自己的告警落点。
    own = {AC19_KEYHEALTH_REL, AC19_PATROL_REL}
    runtime = runtime_py_files_with_optional(ctx, (AC19_KEYNOTIFY_REL,))
    graded = [rel for rel in runtime if rel not in own and "credential_health(" in ctx.read(rel)]
    notified = [
        rel
        for rel in runtime
        if rel != AC19_KEYHEALTH_REL
        and re.search(r"credential_health|KeyReport", ctx.read(rel))
        and re.search(r"build_notify_hook|notify\(", ctx.read(rel))
    ]
    observation_results = outcomes(AC19_OBSERVATION_NODES)
    live_path = "docs/evidence/C64/key-monitoring-live.json"
    live_source = ctx.read(live_path) if (ctx.root / live_path).is_file() else ""
    return {
        "classes_declared": count(sum(1 for name in AC19_KEY_CLASSES if f'"{name}"' in src)),
        "rejected_rule": flag(rejected),
        "quota_rule": flag(quota),
        "expiry_signal": flag(key_expiry_signal(src)),
        "presence_only": flag("presence-only" in src),
        "disclosure": flag(
            "publishes remaining quota or revocation status" in src
            and "not an active validity check" in src
        ),
        **key_monitoring_observations(live_source),
        "alert_sites": count(len(graded)),
        "alert_list": ", ".join(sorted(graded)[:4]) or "-",
        "notify_sites": count(len(notified)),
        "notify_list": ", ".join(sorted(notified)[:4]) or "-",
        "observation_runs": count(len(observation_results)),
        "observation_passed": count(
            sum(1 for seen in observation_results.values() if seen == "passed")
        ),
        "observation_bad": bad_of(observation_results),
    }


def judge_ac19_04(facts: Facts) -> Verdict:
    """``AC-19|04``: each Key plane has a signal that can fire and a channel that can tell."""
    ok = (
        facts["classes_declared"] == "4"
        and facts["rejected_rule"] == "yes"
        and facts["quota_rule"] == "yes"
        and facts["expiry_signal"] == "yes"
        and number(facts["alert_sites"]) >= 1
        and number(facts["notify_sites"]) >= 1
        and facts["disclosure"] == "yes"
        and facts["observation_runs"] == "3"
        and facts["observation_passed"] == facts["observation_runs"]
        and facts["observation_bad"] == "-"
        and facts["issuer_expiry_observed"] == "yes"
        and facts["quota_remaining_observed"] == "yes"
        and facts["delivery_observed"] == "yes"
    )
    readings = (
        f"四类健康类别在位 {facts['classes_declared']}/4（key-validity / key-expiry / "
        "revocation-or-ban / quota-left）",
        f"线上可触发的分类：401/403→credential-rejected = {facts['rejected_rule']}，"
        f"429→quota-exhausted = {facts['quota_rule']}；到期分类或主动探测 = "
        f"{facts['expiry_signal']}（本平台自签 Key 的 is_expired 不算，判据点名的是权威源）",
        f"分级落点 {facts['alert_sites']} 处运行时模块调用 credential_health"
        f"（{facts['alert_list']}，定义处 patrol.py 已排除）；"
        f"带外告警通道 {facts['notify_sites']} 处（{facts['notify_list']}）—— "
        "只有 /health 可拉取 ≠ 告警生效",
        f"operator-supplied expiry metadata supported = {facts['expiry_signal']}；"
        f"独立 observation TTL 与通知测试 "
        f"{facts['observation_passed']}/{facts['observation_runs']} 通过 "
        f"({facts['observation_bad']})：过期只报告 observation-expired、不会被当作恢复；"
        "余量未知时保持 None，不补造配额",
        f"independent live evidence: issuer expiry={facts['issuer_expiry_observed']}, "
        f"remaining quota={facts['quota_remaining_observed']}, "
        f"notification delivery={facts['delivery_observed']}; "
        "metadata and mocks do not prove these",
        f"模块自己的边界声明在位 = {facts['disclosure']}，未观测只报 "
        f"{facts['presence_only']}（这句被删而探测仍未做 ⇒ 判 gap）",
    )
    reason = (
        ""
        if ok
        else (
            f"判据要类别、可触发信号、分级落点、通知通道与观察状态测试共同成立；当前读到 "
            f"{facts['classes_declared']}/4 类，operator expiry support={facts['expiry_signal']}，"
            f"分级落点={facts['alert_sites']}、通知通道={facts['notify_sites']}，"
            f"observation tests={facts['observation_passed']}/{facts['observation_runs']}。"
            "观察 TTL 过期不能替代 key 恢复，quota 余量未知时也不能填入数值；"
            "还缺供应商到期/余量和实际通知送达的独立现场证据"
        )
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def key_monitoring_observations(source: str) -> Facts:
    """Read independent live measurements; operator metadata alone stays unverified."""
    try:
        record = json.loads(source)
    except (json.JSONDecodeError, TypeError):
        record = {}
    if not isinstance(record, dict):
        record = {}
    live = (
        record.get("scope") == "independent-live-key-monitoring"
        and record.get("provider") in {"ths", "fred"}
        and isinstance(record.get("observed_at"), str)
        and bool(record.get("observed_at"))
    )
    remaining = record.get("remaining_quota")
    return {
        "issuer_expiry_observed": flag(
            live
            and record.get("expiry_provenance") == "issuer"
            and isinstance(record.get("expiry_date"), str)
            and bool(record.get("expiry_date"))
        ),
        "quota_remaining_observed": flag(
            live
            and isinstance(remaining, (int, float))
            and not isinstance(remaining, bool)
            and remaining >= 0
        ),
        "delivery_observed": flag(
            live
            and record.get("notification_delivered") is True
            and record.get("notification_channel") in {"websocket", "smtp"}
            and bool(record.get("delivery_evidence"))
        ),
    }


def filled_cells(line: str) -> int:
    """How many non-empty Markdown cells a table row has (0 when any cell is blank)."""
    parts = [part.strip() for part in line.strip().strip("|").split("|")]
    return len(parts) if all(parts) else 0


def config_doc_rows(doc: str) -> tuple[list[str], list[str]]:
    """Live key rows versus rows under a differently shaped table (第十节 已移除项)."""
    live: list[str] = []
    other: list[str] = []
    header = ""
    for line in doc.splitlines():
        if line.startswith("| 键名"):
            header = line.strip()
            continue
        if not re.match(r"^\|\s*`[A-Z][A-Z0-9_]*`", line):
            continue
        (live if header == AC19_LIVE_HEADER else other).append(line)
    return live, other


def measure_ac19_05(ctx: Context) -> Facts:
    """Read row coverage, how 生效方式 is carried, and whether code agrees with the doc."""
    paths = ac19_tracked(ctx)
    doc = ctx.read(AC19_CONFIG_DOC_REL) if AC19_CONFIG_DOC_REL in set(paths) else ""
    cfg = ctx.read(AC19_CONFIG_REL)
    live, other = config_doc_rows(doc)
    complete = [line for line in live if filled_cells(line) == 4]
    effect_headers = sum(
        1 for line in doc.splitlines() if line.startswith("| 键名") and "生效方式" in line
    )
    clause = any("生效方式" in line and "重启" in line for line in doc.splitlines())
    marked = sum(1 for line in live if "热加载" in line)
    percall = [
        rel
        for rel in runtime_py_files(ctx)
        if rel != AC19_CONFIG_REL and "get_settings()" in ctx.read(rel)
    ]
    return {
        "doc_tracked": flag(bool(doc)),
        "live_rows": count(len(live)),
        "rows_complete": count(len(complete)),
        "removed_rows": count(len(other)),
        "effect_col_headers": count(effect_headers),
        "global_clause": flag(clause),
        "hotload_marked": count(marked),
        "settings_cached": flag(bool(re.search(r"@lru_cache\ndef get_settings", cfg))),
        "percall_reads": count(len(percall)),
        "percall_list": ", ".join(sorted(percall)[:3]) or "-",
    }


def judge_ac19_05(facts: Facts) -> Verdict:
    """``AC-19|05``: the config inventory lists every live key and how a change takes effect."""
    # 生效方式允许只以全局条款承载，但条款必须与代码事实自洽：
    # 若 get_settings 不再被缓存（逐请求重读 ⇒ 不重启即生效），逐行标注就必须存在。
    ok = (
        facts["doc_tracked"] == "yes"
        and number(facts["live_rows"]) >= 30
        and facts["rows_complete"] == facts["live_rows"]
        and (number(facts["effect_col_headers"]) >= 1 or facts["global_clause"] == "yes")
        and (number(facts["hotload_marked"]) >= 1 or facts["settings_cached"] == "yes")
    )
    readings = (
        f"在用键名行 {facts['live_rows']} 条，四列齐且无空单元格 {facts['rows_complete']} 条"
        f"（另有 {facts['removed_rows']} 条属第十节「已被移除的配置」两列表，不计入在配置项）",
        f"表头里带「生效方式」列的 {facts['effect_col_headers']} 处；全局条款"
        "（改后需重启后端进程）在位 = "
        f"{facts['global_clause']}；逐行标注「热加载」{facts['hotload_marked']} 条",
        f"代码侧 {facts['percall_reads']} 个运行时模块在调用点读 get_settings()"
        f"（{facts['percall_list']}）；get_settings 仍被 @lru_cache 冻结 = "
        f"{facts['settings_cached']} —— 去掉缓存而逐行无标注 ⇒ 文档在骗人，判 gap",
    )
    reason = (
        ""
        if ok
        else (
            f"四项里「生效方式」只以第 5 行一句全局条款存在（逐行列 "
            f"{facts['effect_col_headers']}、逐行「热加载」标注 {facts['hotload_marked']} 条，"
            f"而那句写着「除标注热加载者外」），在用行 "
            f"{facts['rows_complete']}/{facts['live_rows']} 列齐、"
            f"全局条款 {facts['global_clause']}、"
            f"get_settings 缓存 {facts['settings_cached']} —— 其中一项不成立"
        )
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


# --------------------------------------------------------------------------- #
# C64 remaining implementation: fresh named behavior checks and bounded evidence
# --------------------------------------------------------------------------- #


C64_HTTP_NODES: Final = (
    "tests/test_http_client.py::TestSuccessPath::test_governance_kwargs_passed_to_transport",
    "tests/test_http_client.py::TestCircuitBreaker::test_429_retried_within_request",
    "tests/test_http_client.py::TestRateLimiting::test_burst_allowed_then_wait",
    "tests/test_http_client.py::test_host_concurrency_limit_is_shared_across_thread_local_sessions",
    "tests/test_fuyao_transport.py::TestRateLimiter::test_cooldown_blocks_until_retry_after",
)
C64_LOG_NODES: Final = (
    "tests/test_macro_http_governance.py::test_structured_events_correlate_retries_and_redact_credentials",
    "tests/test_http_client.py::test_transport_events_redact_url_credentials_parameters_and_exception_text",
    "tests/test_fuyao_transport.py::TestHttpClient::test_structured_event_redacts_credentials_and_parameter_values",
    "tests/test_fuyao_transport.py::TestHttpClient::test_transport_event_does_not_log_exception_text",
)
C64_CREDENTIAL_NODES: Final = (
    "tests/test_provider_registry.py::TestCredentialRouting::test_auto_skips_missing_ths_credentials_and_warns_safely",
    "tests/test_provider_registry.py::TestCredentialRouting::test_auto_fails_clearly_when_only_credentialed_source_is_verified",
    "tests/test_provider_registry.py::TestCredentialRouting::test_key_added_after_registry_creation_enables_fred",
)
C64_MINUTE_NODES: Final = (
    "tests/test_minute_archive.py::test_microsecond_timestamp_survives_index_and_parquet_round_trip",
    "tests/test_minute_archive.py::test_multi_day_multi_symbol_pages_are_sorted_without_duplicates",
    "tests/test_minute_data_api.py::test_minute_query_reads_only_main_index_and_local_parquet",
)
C64_CACHE_NODES: Final = (
    "tests/test_raw_response_cache.py::test_cache_key_normalizes_order_and_separates_response_dimensions",
    "tests/test_raw_response_cache.py::test_ttl_and_only_successful_statuses_are_cached",
    "tests/test_raw_response_cache.py::test_corrupted_entry_is_a_warning_and_safe_miss",
    "tests/test_raw_response_cache.py::test_ac13_05_actual_dwd_adjustment_route_never_caches_synthetic_series",
)
C64_PATROL_NODES: Final = (
    "tests/test_scheduled_patrol.py::TestPatrolFailureStore::test_two_failures_cross_threshold_once_and_survive_store_recreation",
    "tests/test_scheduled_patrol.py::TestPatrolFailureStore::test_threshold_notification_retries_after_failed_delivery_and_store_restart",
    "tests/test_scheduled_patrol.py::TestPatrolFailureStore::test_recovery_notification_retries_after_failed_delivery_and_store_restart",
    "tests/test_scheduled_patrol.py::TestScheduledPatrolExecution::test_disabled_default_reports_without_registry_or_provider_access",
    "tests/test_scheduled_patrol.py::TestScheduledPatrolExecution::test_only_explicit_verified_p0_capabilities_are_probed",
    "tests/test_pipeline_jobs.py::TestAttachBuiltinJobs::test_registers_the_cron_jobs_on_the_raw_scheduler",
)


def _c64_behavior(facts: Facts) -> Verdict:
    """Accept fresh named checks only when the implementation binding is present."""
    ok = plane_is_green(facts, "c64") and facts["binding"] == "yes"
    return Verdict(
        PROVEN if ok else GAP,
        (
            plane_reading(facts, "c64", "C64 fresh named behavior checks"),
            f"runtime binding={facts['binding']}; scope={facts['scope']}",
        ),
        "" if ok else "A required named behavior check or its runtime binding is missing.",
    )


def measure_c64_ac4_01(ctx: Context) -> Facts:
    """Check timeout, 429 retry, bucket rate and shared host concurrency behavior."""
    source = ctx.read("opendata/data/http_client.py")
    return {
        **node_plane_facts("c64", C64_HTTP_NODES),
        "binding": flag("with self._semaphore_for(host)" in source and "max_attempts" in source),
        "scope": "offline actual transport mocks; no real supplier ban or uptime claim",
    }


def _read_historical_source(ctx: Context, historical_path: str) -> str:
    """Read the canonical current file for a retained historical source identity.

    ``Context.read`` intentionally stays a literal current-tree read. This narrow adapter uses
    the shared source-layout map for the handful of probes whose names predate the current
    provider tree; it never falls back to reading the old path.
    """
    layout = script_module("scripts/quality/source_layout.py")
    return ctx.read(layout.historical_identity(historical_path))


def measure_c64_ac4_02(ctx: Context) -> Facts:
    """Check request correlation and safe structured failure events in both transports."""
    sources = (
        ctx.read("opendata/data/http_client.py"),
        _read_historical_source(ctx, "opendata_fuyao/http_client.py"),
    )
    fields = (
        "source",
        "endpoint",
        "parameter_summary",
        "elapsed_seconds",
        "request_id",
        "failure_category",
    )
    return {
        **node_plane_facts("c64", C64_LOG_NODES),
        "binding": flag(all(all(f'"{field}"' in source for field in fields) for source in sources)),
        "scope": "offline structured-event behavior and credential redaction",
    }


def measure_c64_ac7_06(ctx: Context) -> Facts:
    """Check missing-key auto exclusion, visible warning and no unverified promotion."""
    source = ctx.read("opendata/data/registry.py")
    return {
        **node_plane_facts("c64", C64_CREDENTIAL_NODES),
        "binding": flag("AUTO_ROUTE_CREDENTIAL_MISSING" in source and "logger.warning" in source),
        "scope": "current registry resolution; no supplier availability claim",
    }


def measure_c64_minute(ctx: Context) -> Facts:
    """Check actual file query behavior and mainDB metadata versus warehouse rows."""
    source = ctx.read("opendata/data/minute_archive.py")
    api = ctx.read("opendata/api/minute_data.py")
    archive = ctx.read("docs/evidence/C64/minute-mysql-api.txt")
    return {
        **node_plane_facts("c64", C64_MINUTE_NODES),
        "binding": flag(
            "parquet" in source
            and "query_minute_archive" in api
            and "REVIEW_EXIT=0" in archive
            and "no minute table in warehouse" in archive
        ),
        "scope": (
            "fresh temporary file/SQLite checks plus C64 isolated MySQL/Parquet REST; "
            "no deployment or file RPO claim"
        ),
    }


def measure_c64_cache(ctx: Context) -> Facts:
    """Check actual factor-backed qfq/hfq routes leave raw HTTP cache unchanged."""
    cache = ctx.read("opendata/data/raw_response_cache.py")
    transport = ctx.read("opendata/data/http_client.py")
    return {
        **node_plane_facts("c64", C64_CACHE_NODES),
        "binding": flag(
            "get_configured_raw_response_cache" in transport
            and "raw_responses" in cache
            and "_MAX_BODY_BYTES" in cache
        ),
        "scope": (
            "fresh raw-byte cache and SQLite factor-backed REST qfq/hfq checks; "
            "deleting cache preserves ODS watermark and computed queries; no formula change"
        ),
    }


def measure_c64_patrol(ctx: Context) -> Facts:
    """Check daily P0 selection, persistent transitions and failed delivery retry."""
    jobs = ctx.read("opendata/pipeline/jobs.py")
    patrol = ctx.read("opendata/pipeline/scheduled_patrol.py")
    schedules = yaml.safe_load(ctx.read("opendata/pipeline/schedules.yaml"))
    rows = schedules if isinstance(schedules, list) else []
    # The YAML schema may wrap the schedule rows; never assume an empty list passed.
    if isinstance(schedules, dict):
        rows = schedules.get("templates", [])
    configured = (
        any(
            isinstance(row, dict)
            and row.get("kind") == "scheduled_patrol"
            and row.get("cron") == "0 18 * * *"
            and row.get("timezone") == "UTC"
            for row in rows
        )
        if isinstance(rows, list)
        else False
    )
    return {
        **node_plane_facts("c64", C64_PATROL_NODES),
        "binding": flag(
            configured
            and "execute_scheduled_patrol(template)" in jobs
            and "acknowledge" in patrol
            and "notification_id" in patrol
        ),
        "scope": (
            "fresh offline P0/persistent-outbox checks and actual UTC CronTrigger; "
            "disabled by default; business time and live suppliers remain unverified"
        ),
    }


def measure_c64_gb_query(ctx: Context) -> Facts:
    """Read the independently run GB-table query with its explicit bounded scope."""
    source = ctx.read("docs/evidence/C64/warehouse-gb-query.txt")
    records: list[dict[str, object]] = []
    for line in source.splitlines():
        if line.startswith("{"):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                records.append(entry)
    entry = records[-1] if records else {}
    plan = entry.get("explain")
    scans = plan if isinstance(plan, list) else []
    return {
        "dated_evidence": flag(
            "Date: 2026-09-30" in source and "REVIEW_EXIT=0" in source and "HEAD: d552c08" in source
        ),
        "gb_table": flag(
            isinstance(entry.get("bytes"), int) and int(str(entry["bytes"])) >= 1_000_000_000
        ),
        "http_rows": flag(
            entry.get("http_query_status") == 200
            and entry.get("http_csv_status") == 200
            and isinstance(entry.get("rows"), int)
            and int(str(entry["rows"])) > 0
            and entry.get("rows") == entry.get("csv_rows")
        ),
        "indexed": flag(
            bool(scans)
            and all(
                isinstance(row, dict)
                and row.get("type") in {"range", "ref", "eq_ref", "const"}
                and bool(row.get("key"))
                for row in scans
            )
        ),
        "bounded_time": flag(
            isinstance(entry.get("elapsed_seconds"), (int, float))
            and 0 < float(str(entry["elapsed_seconds"])) < 10
        ),
        "read_only": flag(
            entry.get("writes") == 0 and "READ ONLY on every transaction" in str(entry.get("scope"))
        ),
        "scope": (
            "one indexed stock_daily THS ODS table; 15-day/one-symbol/12-row query; "
            "no all-table or P95 claim"
        ),
    }


def judge_c64_gb_query(facts: Facts) -> Verdict:
    """Require every recorded GB-table measurement face; absence cannot be green."""
    fields = ("dated_evidence", "gb_table", "http_rows", "indexed", "bounded_time", "read_only")
    ok = all(facts[field] == "yes" for field in fields)
    return Verdict(
        PROVEN if ok else GAP,
        tuple(f"{field}={facts[field]}" for field in fields) + (facts["scope"],),
        "" if ok else "GB-table HTTP/CSV/index/elapsed/read-only measurement is incomplete.",
    )


# --------------------------------------------------------------------------- #
# Executed runtime-binding witnesses
# --------------------------------------------------------------------------- #

#: Three binding faces used to grep a module's source for ``register_x()`` tokens. That reads how
#: the code is *written*, not what it does: C74 made the provider facade lazy and all three greps
#: flipped red while registration behaved identically, and a registration call that the runtime
#: never needed could be deleted with the grep still green. Each witness instead runs the real
#: registration in a fresh interpreter and reports what the registry ended up holding.
_RUNTIME_WITNESSES: Final[dict[str, str]] = {
    "registry": (
        "import json\n"
        "from opendata.data.providers import catalog\n"
        "from opendata.data.registry import ProviderRegistry\n"
        "registry = ProviderRegistry()\n"
        "catalog.register_providers(registry)\n"
        "caps = registry.capabilities()\n"
        "sources = sorted({cap.source for cap in caps})\n"
        "implemented = sorted(p.source for p in catalog.PROVIDERS if p.is_implemented)\n"
        "order = sorted(p.source for p in catalog.registration_order())\n"
        "print(json.dumps({"
        "'capabilities': len(caps), 'sources': sources, 'implemented': implemented,"
        "'uncovered': sorted(set(implemented) - set(sources)),"
        "'phantom': sorted(set(sources) - set(implemented)),"
        "'never_registered': sorted(set(implemented) - set(order))}))\n"
    ),
    "ths": (
        "import json\n"
        "from opendata.data.providers import catalog\n"
        "from opendata.data.registry import ProviderRegistry\n"
        "descriptor = catalog.get_provider('ths')\n"
        "registry = ProviderRegistry()\n"
        "catalog.register_provider('ths', registry)\n"
        "caps = [cap for cap in registry.capabilities() if cap.source == 'ths']\n"
        "routed, misses = 0, []\n"
        "for cap in caps:\n"
        "    try:\n"
        "        fetcher = registry.resolve_domain(\n"
        "            cap.domain, source='ths', period=cap.period, market=cap.market\n"
        "        )\n"
        "        routed += 1 if fetcher.capability == cap else 0\n"
        "    except Exception as exc:\n"
        "        misses.append(f'{cap.domain}:{type(exc).__name__}')\n"
        "print(json.dumps({"
        "'declared_bindings': len(descriptor.fetcher_bindings), 'registered': len(caps),"
        "'verified': sum(1 for cap in caps if cap.verified), 'routed': routed,"
        "'misses': sorted(misses)}))\n"
    ),
    "vendor_no_akshare": (
        "import builtins, importlib.util, json, sys\n"
        "blocked = []\n"
        "real_import = builtins.__import__\n"
        "def block_akshare(name, *args, **kwargs):\n"
        "    if name == 'akshare' or name.startswith('akshare.'):\n"
        "        blocked.append(name)\n"
        "        raise ImportError('intentional top-level akshare block')\n"
        "    return real_import(name, *args, **kwargs)\n"
        "builtins.__import__ = block_akshare\n"
        "hook_fired = False\n"
        "try:\n"
        "    import akshare\n"
        "except ImportError:\n"
        "    hook_fired = True\n"
        "control_hits = len(blocked)\n"
        "blocked.clear()\n"
        "port_import_error = ''\n"
        "ak = None\n"
        "try:\n"
        f"    import {PORTED_MODULE} as ak\n"
        "except Exception as exc:\n"
        "    port_import_error = f'{type(exc).__name__}: {exc}'\n"
        "missing = [n for n in sys.argv[1:] if not callable(getattr(ak, n, None))]\n"
        "leaks = sorted(m for m in sys.modules if m == 'akshare' or m.startswith('akshare.'))\n"
        "print(json.dumps({"
        "'hook_fired': hook_fired, 'control_hits': control_hits,"
        "'port_import_hits': len(blocked), 'port_import_error': port_import_error,"
        "'targets': len(sys.argv[1:]), 'missing': missing, 'leaks': leaks,"
        "'spec_present': importlib.util.find_spec('akshare') is not None}))\n"
    ),
    "sdk": (
        "import builtins, json\n"
        "blocked = []\n"
        "real_import = builtins.__import__\n"
        "def block_sdk(name, *args, **kwargs):\n"
        "    if name == 'yfinance' or name.startswith('yfinance.'):\n"
        "        blocked.append(name)\n"
        "        raise ImportError('intentional optional-SDK block')\n"
        "    return real_import(name, *args, **kwargs)\n"
        "builtins.__import__ = block_sdk\n"
        "sdk_absent = False\n"
        "try:\n"
        "    import yfinance\n"
        "except ImportError:\n"
        "    sdk_absent = True\n"
        "from opendata.data.providers import catalog\n"
        "from opendata.data.registry import ProviderRegistry\n"
        "registry = ProviderRegistry()\n"
        "catalog.register_providers(registry)\n"
        "caps = registry.capabilities()\n"
        "implemented = sorted(p.source for p in catalog.PROVIDERS if p.is_implemented)\n"
        "legs = [cap for cap in caps if cap.source == 'ecb']\n"
        "routed = 0\n"
        "for cap in legs:\n"
        "    try:\n"
        "        registry.resolve_domain(cap.domain, source='ecb', period=cap.period,"
        " market=cap.market)\n"
        "        routed += 1\n"
        "    except Exception:\n"
        "        pass\n"
        "print(json.dumps({"
        "'sdk_absent': sdk_absent, 'blocked_hits': len(blocked), 'capabilities': len(caps),"
        "'implemented': implemented,"
        "'uncovered': sorted(set(implemented) - {cap.source for cap in caps}),"
        "'sdk_legs': sum(1 for cap in caps if cap.source == 'yfinance'),"
        "'optional_declared': sorted("
        "p.source for p in catalog.PROVIDERS if p.is_implemented and p.optional_dependencies),"
        "'other_legs': len(legs), 'other_routed': routed}))\n"
    ),
}
_RUNTIME_WITNESS_RESULTS: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}


def runtime_witness(name: str, *args: str) -> dict[str, Any]:
    """Run one registration witness in a fresh interpreter, once per measurement pass.

    ``args`` arrive in the child as ``sys.argv[1:]``, which is how a witness is handed the very
    same name list the parent measured instead of re-deriving it on the other side of a process
    boundary.
    """
    key = (name, args)
    cached = _RUNTIME_WITNESS_RESULTS.get(key)
    if cached is not None:
        return cached
    code, out, err = run_split_stderr([sys.executable, "-B", "-c", _RUNTIME_WITNESSES[name], *args])
    payload = next((line for line in reversed(out.splitlines()) if line.startswith("{")), "")
    if not payload:
        tail = [line for line in err.splitlines() if line.strip()]
        raise ProbeError(
            f"witness {name} printed no JSON (exit {code}): {tail[-1] if tail else '(no output)'}"
        )
    parsed = cast("dict[str, Any]", json.loads(payload))
    _RUNTIME_WITNESS_RESULTS[key] = parsed
    return parsed


def witness_list(witness: dict[str, Any], key: str) -> list[str]:
    """One list field of a witness payload, named in a reading only when it is not empty."""
    rows = cast("list[str]", witness.get(key, []))
    return [str(row) for row in rows]


def measure_c64_registry(ctx: Context) -> Facts:
    """Measure bundled provider registration from current named behavior and binding."""
    witness = runtime_witness("registry")
    uncovered = witness_list(witness, "uncovered")
    phantom = witness_list(witness, "phantom")
    never_registered = witness_list(witness, "never_registered")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_p0_providers.py::TestRegistration::test_registers_the_p0_capabilities_plus_b1_domains",
                "tests/test_ths_provider.py::TestRegistration::test_registers_the_verified_fuyao_capabilities",
                "tests/test_openbb_map.py::TestShippedMap::test_enabled_capabilities_are_covered",
            ),
        ),
        "binding": flag(
            int(witness.get("capabilities", 0)) > 0
            and not uncovered
            and not phantom
            and not never_registered
        ),
        "scope": (
            "executed witness: "
            f"{witness.get('capabilities')} capabilities over {witness.get('sources')}; "
            f"自研却无能力的源={uncovered or '-'}、有能力的非自研源={phantom or '-'}、"
            f"没进注册顺序的源={never_registered or '-'}"
        ),
    }


def measure_c64_auto(ctx: Context) -> Facts:
    """Measure authority and health fallback from current named behavior and binding."""
    source = ctx.read("opendata/data/registry.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_provider_registry.py::TestAutoRouting::test_auto_prefers_authority_source",
                "tests/test_provider_registry.py::TestAutoRouting::test_auto_degrades_to_next_when_unhealthy",
                "tests/test_provider_registry.py::TestAutoRouting::test_auto_fails_when_all_unhealthy",
            ),
        ),
        "binding": flag(
            all(token in source for token in ("mark_unavailable", "participates_in_auto"))
        ),
        "scope": "offline actual registry resolution",
    }


def measure_c64_explicit(ctx: Context) -> Facts:
    """Measure precise explicit routing from current named behavior and binding."""
    source = ctx.read("opendata/data/registry.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_provider_registry.py::TestExplicitRouting::test_explicit_source_routes_directly",
                "tests/test_provider_registry.py::TestExplicitRouting::test_explicit_source_no_fallback",
                "tests/test_provider_registry.py::TestExplicitRouting::test_period_and_market_filters",
            ),
        ),
        "binding": flag(
            all(token in source for token in ('source != "auto"', "no registered capability"))
        ),
        "scope": "offline explicit routing and rejection; no live supplier request",
    }


def measure_c64_reserved(ctx: Context) -> Facts:
    """Measure unverified and reserved notes exclusion from current named behavior and binding."""
    source = ctx.read("opendata/data/capability.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_provider_registry.py::TestAutoRouting::test_unverified_excluded_from_auto",
                "tests/test_provider_registry.py::TestAutoRouting::test_reserved_notes_excluded_from_auto[on-demand]",
                "tests/test_provider_registry.py::TestAutoRouting::test_reserved_notes_excluded_from_auto[upstream-pending]",
            ),
        ),
        "binding": flag(
            all(token in source for token in ("participates_in_auto", "self.verified"))
        ),
        "scope": "offline verified/notes eligibility, both reserved note values",
    }


def measure_c64_cap_api(ctx: Context) -> Facts:
    """Measure capability and source registry APIs from current named behavior and binding."""
    source = ctx.read("opendata/api/data.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_p0_providers.py::TestCapabilitiesEndpoint::test_capabilities_api_lists_p0_capabilities",
                "tests/test_p0_providers.py::TestCapabilitiesEndpoint::test_sources_api_exposes_authority_and_registered",
                "tests/test_p0_providers.py::TestCapabilitiesEndpoint::test_capabilities_requires_authentication",
                "tests/test_p0_providers.py::TestCapabilitiesEndpoint::test_sources_requires_authentication",
            ),
        ),
        "binding": flag(
            all(token in source for token in ("get_registry().capabilities()", "authority"))
        ),
        "scope": "offline authenticated read-only API and current registry contents",
    }


def measure_c64_unverified(ctx: Context) -> Facts:
    """Measure incomplete comparison cannot enter auto from current named behavior and binding."""
    source = ctx.read("opendata/data/registry.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_p0_providers.py::TestRegistration::test_capabilities_declared_unverified",
                "tests/test_p0_providers.py::TestRegistration::test_auto_routing_excludes_unverified",
                "tests/test_provider_registry.py::TestAutoRouting::test_unverified_excluded_from_auto",
            ),
        ),
        "binding": flag(all(token in source for token in ("participates_in_auto",))),
        "scope": "offline unverified exclusion; no supplier comparison claim",
    }


def measure_c64_fuyao_transport(ctx: Context) -> Facts:
    """Measure fuyao envelope auth and backoff from current named behavior and binding."""
    source = _read_historical_source(ctx, "opendata_fuyao/http_client.py")
    smoke = ctx.read("docs/evidence/A3/fuyao-live-smoke.txt")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_fuyao_transport.py::TestHttpClient::test_successful_get_sends_the_key_header_and_parses",
                "tests/test_fuyao_transport.py::TestHttpClient::test_auth_error_is_not_retried",
                "tests/test_fuyao_transport.py::TestHttpClient::test_http_429_enters_cooldown_and_is_retried",
                "tests/test_fuyao_transport.py::TestErrorClassification::test_codes_map_to_stable_categories[4001-rate_limited-True]",
            ),
        ),
        "binding": flag(
            all(token in source for token in ("max_attempts", "retryable"))
            and "4 passed, 26 deselected" in smoke
            and "https://fuyao.aicubes.cn" in smoke
        ),
        "scope": "fresh offline HTTP mocks; historical live smoke A3 separately retained",
    }


def measure_c64_fuyao_errors(ctx: Context) -> Facts:
    """Measure business error messages and remedies from current named behavior and binding."""
    source = _read_historical_source(ctx, "opendata_fuyao/error_messages.yaml")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_fuyao_transport.py::TestErrorMessages::test_every_registered_code_has_an_entry",
                "tests/test_fuyao_transport.py::TestErrorMessages::test_every_transport_key_has_an_entry",
                "tests/test_fuyao_transport.py::TestErrorMessages::test_entries_declare_category_message_and_advice",
            ),
        ),
        "binding": flag(all(token in source for token in ("category:", "message:", "advice:"))),
        "scope": "current complete error-table validation",
    }


def measure_c64_fuyao_map(ctx: Context) -> Facts:
    """Measure official endpoint map coverage from current named behavior and binding."""
    source = _read_historical_source(ctx, "opendata_fuyao/endpoint_map.yaml")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_fuyao_endpoint_map.py::test_every_doc_section_is_mapped",
                "tests/test_fuyao_endpoint_map.py::test_every_doc_endpoint_is_mapped",
                "tests/test_fuyao_endpoint_map.py::test_coverage_totals_reconcile",
            ),
        ),
        "binding": flag(all(token in source for token in ("version:", "sections:"))),
        "scope": "official archived document inventory and bidirectional map checks",
    }


def measure_c64_ths_contract(ctx: Context) -> Facts:
    """Measure THS contract registration from current named behavior and binding."""
    witness = runtime_witness("ths")
    misses = witness_list(witness, "misses")
    declared = int(witness.get("declared_bindings", 0))
    registered = int(witness.get("registered", 0))
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_ths_provider.py::TestRegistration::test_registers_the_verified_fuyao_capabilities",
                "tests/test_ths_provider.py::TestRegistration::test_fetchers_declare_the_registered_domains",
                "tests/test_ths_provider.py::TestRegistration::test_every_verified_domain_auto_routes_to_ths",
            ),
        ),
        "binding": flag(
            declared > 0
            and registered == declared
            and int(witness.get("verified", 0)) == registered
            and int(witness.get("routed", 0)) == registered
            and not misses
        ),
        "scope": (
            "executed witness: ths 描述符声明 "
            f"{declared} 条绑定，注册器装了 {registered} 条、verified "
            f"{witness.get('verified')} 条，逐个 resolve_domain(source='ths') 命中 "
            f"{witness.get('routed')} 条，落空={misses or '-'}"
        ),
    }


def measure_c64_openbb_map(ctx: Context) -> Facts:
    """Measure enabled provider compatibility map from current named behavior and binding."""
    source = ctx.read("opendata/data/openbb_map.yaml")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_openbb_map.py::TestShippedMap::test_enabled_capabilities_are_covered",
                "tests/test_openbb_map.py::TestShippedMap::test_confirmed_models_are_never_guesses",
            ),
        ),
        "binding": flag(all(token in source for token in ("version:", "entries:"))),
        "scope": "naming interoperability only; no cleanroom rights assertion",
    }


def measure_c64_freshness_api(ctx: Context) -> Facts:
    """Measure catalog and freshness routes from current named behavior and binding."""
    source = ctx.read("opendata/api/data_query.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_data_catalog.py::TestFiveReadings::test_a_populated_domain_carries_every_reading",
                "tests/test_data_catalog.py::TestTheFreshnessDoor::test_the_two_doors_agree_on_the_same_domain",
                "tests/test_data_catalog.py::TestTheFreshnessDoor::test_the_ods_door_measures_that_sources_own_table",
            ),
        ),
        "binding": flag(all(token in source for token in ("freshness", "catalog"))),
        "scope": "temporary SQLite handler checks; HTTP route separately covered",
    }


def measure_c64_replay(ctx: Context) -> Facts:
    """Measure missed batch meta replay from current named behavior and binding."""
    source = ctx.read("opendata/api/data_subscribe.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_data_subscribe.py::TestSocketReplay::test_since_batch_id_replays_the_missed_batches",
                "tests/test_data_subscribe.py::TestSocketReplay::test_replayed_batches_of_other_layers_are_filtered_out",
            ),
        ),
        "binding": flag(all(token in source for token in ("since_batch_id", "replay"))),
        "scope": "local websocket protocol and persisted batch fixtures",
    }


def measure_c64_frames(ctx: Context) -> Facts:
    """Measure full message ceilings and fallback from current named behavior and binding."""
    source = ctx.read("opendata/pipeline/subscription.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_data_subscribe.py::TestFraming::test_payload_over_the_row_ceiling_is_chunked",
                "tests/test_data_subscribe.py::TestFraming::test_payload_over_the_total_ceiling_degrades_to_meta",
                "tests/test_data_subscribe.py::TestFraming::test_truncated_meta_explains_the_remedy",
            ),
        ),
        "binding": flag(all(token in source for token in ("truncated",))),
        "scope": "offline bounded framing and remedy assertions",
    }


def measure_c64_routes(ctx: Context) -> Facts:
    """Measure existing routes regression from current named behavior and binding."""
    source = ctx.read("opendata/api/__init__.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_data_subscribe.py::TestWebSocketMounts::test_both_sockets_are_mounted_at_the_root",
                "tests/test_data_subscribe.py::TestWebSocketMounts::test_executions_socket_still_refuses_a_missing_token",
                "tests/test_api_tables_full.py::TestListTables::test_list_tables",
                "tests/test_api_users_full.py::TestListUsers::test_list_users_as_admin",
                "tests/test_api_tasks_full.py::TestListTasks::test_list_tasks_as_user",
                "tests/test_api_executions_full.py::TestGetExecutions::test_get_executions",
            ),
        ),
        "binding": flag(
            all(
                token in source
                for token in (
                    "include_router(tables_router",
                    "include_router(tasks_router",
                    "include_router(users_router",
                    "include_router(executions_router",
                )
            )
        ),
        "scope": "offline main-database fixture REST routes and root websocket mounts",
    }


def measure_c64_client(ctx: Context) -> Facts:
    """Measure minimal REST and websocket client from current named behavior and binding."""
    source = ctx.read("opendata_client/opendata_client/client.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_opendata_client.py::TestPagination::test_stock_daily_all_collects_every_page",
                "tests/test_opendata_client.py::TestRequestContract::test_stock_daily_sends_the_documented_parameters",
                "tests/test_opendata_client.py::TestSubscriptionProtocol::test_events_yield_updates_and_raise_on_error_frames",
            ),
        ),
        "binding": flag(all(token in source for token in ("stock_daily_all", "since_batch_id"))),
        "scope": "mock REST/protocol; separate local socket evidence",
    }


def measure_c64_lock(ctx: Context) -> Facts:
    """Measure upstream lock provenance and hashes from current named behavior and binding."""
    source = _read_historical_source(ctx, "opendata_http/upstream.lock")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_fetch_upstream.py::TestLockVerification::test_lock_that_matches_reports_nothing",
                "tests/test_fetch_upstream.py::TestLockVerification::test_drifted_hash_is_reported",
            ),
        ),
        "binding": flag(
            all(
                token in source
                for token in ("commit", "sha256", "https://github.com/cloudQuant/akshare")
            )
        ),
        "scope": "real two-commit temporary git fixture; shipped lock contains full hash inventory",
    }


def measure_c64_sync(ctx: Context) -> Facts:
    """Measure upstream inventory and signature diff from current named behavior and binding."""
    source = ctx.read("docs/evidence/C2/sync-drill-report.md")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_fetch_upstream.py::TestChangedFiles::test_inventory_lists_add_modify_delete",
                "tests/test_fetch_upstream.py::TestFunctionSignatures::test_function_diff_reports_added_removed_changed",
            ),
        ),
        "binding": flag(all(token in source for token in ("147", "fcdbf25", "c4f6a631c259"))),
        "scope": "fresh local git fixture and historical 147-file commit-interval rehearsal",
    }


def measure_c64_scheduled_pipeline(ctx: Context) -> Facts:
    """Measure scheduled six-step pipeline task from current named behavior and binding."""
    source = ctx.read("opendata/models/task.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_scheduled_pipeline_tasks.py::test_pipeline_executor_dispatches_allowlisted_six_step_arguments",
                "tests/test_scheduled_pipeline_tasks.py::test_pipeline_task_create_requires_admin_and_list_handles_null_script",
                "tests/test_pipeline_templates.py::TestPipelineFactory::test_wires_the_six_step_services",
                "tests/test_pipeline_metadata.py::test_refreshes_ods_and_dwd_metadata_idempotently",
            ),
        ),
        "binding": flag(all(token in source for token in ("task_kind", "parameters"))),
        "scope": "temporary DB dispatch, factory and ODS/DWD metadata; separate migration evidence",
    }


def measure_c64_resume(ctx: Context) -> Facts:
    """Measure completed-shard resume and partial-failure retry."""
    source = ctx.read("opendata/pipeline/runner.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_pipeline_runner.py::TestPipelineRun::test_completed_shards_are_skipped_on_resume",
                "tests/test_pipeline_runner.py::TestPipelineRun::test_partial_shard_failure_is_retried_without_duplicate_ods_keys",
                "tests/test_pipeline_runner.py::TestPipelineRun::test_different_symbol_universes_do_not_resume_each_other",
            ),
        ),
        "binding": flag(all(token in source for token in ("symbol_windows", "resume"))),
        "scope": "SQLite control restart and writer fixtures; separate MySQL resume evidence",
    }


def measure_c64_ban_mechanism(ctx: Context) -> Facts:
    """Measure bounded rate-limit retry mechanism from current named behavior and binding."""
    source = _read_historical_source(ctx, "opendata_fuyao/http_client.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_fuyao_transport.py::TestHttpClient::test_http_429_enters_cooldown_and_is_retried",
                "tests/test_fuyao_transport.py::TestRateLimiter::test_cooldown_blocks_until_retry_after",
            ),
        ),
        "binding": flag(all(token in source for token in ("retryable", "record_rate_limit"))),
        "scope": "offline 429 substitute; no live ban or quota claim",
    }


def measure_c64_steps(ctx: Context) -> Facts:
    """Measure hook checkpoint re-entry, including metadata."""
    source = ctx.read("opendata/pipeline/runner.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_pipeline_runner.py::TestPipelineRun::test_failed_hook_resumes_at_that_step_with_rebuilt_keys",
                "tests/test_pipeline_runner.py::TestPipelineRun::test_meta_hook_failure_retries_without_repeating_prior_hooks",
                "tests/test_pipeline_runner.py::TestPipelineRun::test_new_ods_write_invalidates_hook_steps_before_writer_can_crash",
                "tests/test_pipeline_runner.py::TestPipelineRun::test_resume_false_reexecutes_completed_hook_checkpoints",
            ),
        ),
        "binding": flag(all(token in source for token in ("self.meta", "PipelineStepCheckpoint"))),
        "scope": "SQLite step re-entry; notification is at-least-once",
    }


def measure_c64_reload(ctx: Context) -> Facts:
    """Measure persistent scheduler reload from current named behavior and binding."""
    source = ctx.read("opendata/services/scheduler.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_scheduled_pipeline_tasks.py::test_scheduler_restart_reloads_pipeline_task_from_temporary_database",
                "tests/test_scheduled_pipeline_tasks.py::test_script_executor_path_still_runs_and_records_script_id",
            ),
        ),
        "binding": flag(all(token in source for token in ("task_kind", "pipeline"))),
        "scope": "file-backed SQLite restart; actual task reload and legacy script path",
    }


def c64_node_probe(
    item: str, expects: str, summary: str, measure: Callable[[Context], Facts], runs: int
) -> Probe:
    """Declare strict named-node planes with a counterfact for each failure mode."""
    return Probe(
        item=item,
        expects=expects,
        summary=summary,
        measure=measure,
        judge=_c64_behavior,
        breaks=(
            Break("named behavior check fails", (("c64_exit", "1"),), GAP),
            Break("named node is absent", (("c64_absent", "missing"),), GAP),
            Break("a required node is skipped", (("c64_skipped", "1"),), GAP),
            Break("runtime binding removed", (("binding", "no"),), GAP),
        ),
        repair={
            "c64_runs": str(runs),
            "c64_exit": "0",
            "c64_passed": str(runs),
            "c64_failed": "0",
            "c64_skipped": "0",
            "c64_absent": "-",
            "binding": "yes",
        },
    )


def judge_c64_archive(facts: Facts) -> Verdict:
    """Require the actual recorded observations and their stated provenance."""
    ok = facts["observations"] == "yes" and facts["provenance"] == "yes"
    return Verdict(
        PROVEN if ok else GAP,
        (
            f"recorded observations={facts['observations']}; provenance={facts['provenance']}",
            facts["scope"],
        ),
        "" if ok else "Required archived observation or provenance is missing.",
    )


def c64_archive_probe(
    item: str, expects: str, summary: str, measure: Callable[[Context], Facts]
) -> Probe:
    """Keep historical evidence criteria falsifiable without rerunning live suppliers."""
    return Probe(
        item=item,
        expects=expects,
        summary=summary,
        measure=measure,
        judge=judge_c64_archive,
        breaks=(
            Break("recorded observations absent", (("observations", "no"),), GAP),
            Break("record provenance absent", (("provenance", "no"),), GAP),
        ),
        repair={"observations": "yes", "provenance": "yes"},
    )


def measure_c64_p0_comparison(ctx: Context) -> Facts:
    """Read complete P0 replay cases with their floating tolerance and pending disclosure."""
    report = ctx.read("docs/evidence/A2/compare-report.md")
    cases = [row for row in report.splitlines() if row.startswith("| ") and "| PASS |" in row]
    functions = (
        "stock_history_dividend_detail",
        "stock_financial_report_sina",
        "stock_financial_analysis_indicator_em",
        "index_stock_cons_weight_csindex",
        "futures_zh_daily_sina",
        "option_sse_daily_sina",
        "stock_zh_a_daily",
        "stock_zh_index_daily",
        "fund_etf_hist_sina",
    )
    return {
        "observations": flag(
            bool(cases) and all(any(name in row for row in cases) for name in functions)
        ),
        "provenance": flag(
            "recorded from the upstream checkout pinned in upstream.lock" in report
            and "rtol=1e-09" in report
            and "Pending (network)" in report
        ),
        "scope": (
            f"A2 replay: {len(cases)} PASS cases; Sina alternatives; "
            "four Eastmoney cases pending; no P1 coverage claim"
        ),
    }


def measure_c64_fuyao_live_archive(ctx: Context) -> Facts:
    """Read the live P0 smoke and one actual all-market dump import, retaining their limits."""
    smoke = ctx.read("docs/evidence/A3/fuyao-live-smoke.txt")
    dump = ctx.read("docs/evidence/A3/fuyao-dump-import.txt")
    return {
        "observations": flag(
            "4 passed, 26 deselected" in smoke
            and "dump 55510 行" in dump
            and "写入 55510" in dump
            and "57441" in dump
        ),
        "provenance": flag(
            "fuyao P0" in smoke
            and "https://fuyao.aicubes.cn" in smoke
            and "真机导入结果" in dump
            and "未导入 10 年全量 dump" in dump
        ),
        "scope": "A3 live smoke and ten-day dump; no current Key or ten-year claim",
    }


def measure_c64_maintenance_budget(ctx: Context) -> Facts:
    """Read the documented maintenance budget and concrete abandonment workflow."""
    plan = ctx.read("docs/迭代计划/迭代1-重构数据中台/实施计划.md")
    process = ctx.read("docs/evidence/C2/README.md")
    return {
        "observations": flag(
            "10~15% 人力" in plan
            and "## 3. 失效即弃流程" in process
            and all(
                token in process
                for token in (
                    "fetch_upstream.py --diff",
                    "port_module.py",
                    "compare_with_upstream.py",
                    "健康标记降级",
                )
            )
        ),
        "provenance": flag("每迭代预留" in plan and "2026-09-24" in process),
        "scope": "documented budget/process; no actual staffing allocation claim",
    }


def measure_c64_dual_comparison_archive(ctx: Context) -> Facts:
    """Read measured differences and attribution, retaining legacy-source disclosure."""
    report = ctx.read("docs/evidence/B1/dual-source-cross-check.txt")
    return {
        "observations": flag(
            all(
                token in report
                for token in (
                    "compared_keys=719314",
                    "deviation_count=1989684",
                    "missing_count=103555",
                    "per_field=",
                    "差异归因（人工复核结论）",
                    "未发现程序性错误",
                )
            )
        ),
        "provenance": flag(
            "2026-09-24" in report and "ths vs akshare" in report and "旧库迁移" in report
        ),
        "scope": (
            "B1 2026-01-05..07-21: 719314 compared keys; "
            "akshare leg from legacy migration, no fresh dual capture"
        ),
    }


def measure_c64_gold_archive(ctx: Context) -> Facts:
    """Read independent-vendor index samples and tolerate only the declared comparison window."""
    report = ctx.read("docs/evidence/C5/ths-index-cross-check.txt")
    live = [row for row in report.splitlines() if "| live |" in row]
    return {
        "observations": flag(
            bool(live)
            and all("| PASS |" in row for row in live)
            and "0 failing legs" in report
            and "tolerance:" in report
            and "overlap >= 0.95" in report
        ),
        "provenance": flag(
            "official: sina stock_zh_index_daily -- different vendor" in report
            and "window: 2026-01-05 .. 2026-09-24" in report
        ),
        "scope": (
            f"C5 index_daily: {len(live)} live legs plus one fixture; "
            "no all-domain or current-provider claim"
        ),
    }


def measure_c64_restore_archive(ctx: Context) -> Facts:
    """Read this round's actual dual-database dump and complete row-count restoration."""
    report = ctx.read("docs/evidence/C64/backup-restore.txt")
    return {
        "observations": flag(
            (
                "metadata PASS: gzip validated, separate credential used, "
                "11 tables restored; exact row counts 10"
            )
            in report
            and (
                "warehouse PASS: gzip validated, separate credential used, "
                "18 tables restored; exact row counts 22"
            )
            in report
        ),
        "provenance": flag(
            "Date: 2026-09-30" in report
            and "HEAD: d552c081950edffce167e3dfc0be74f37a6950a1" in report
            and "REVIEW_EXIT=0" in report
            and "owned Docker backup image" in report
        ),
        "scope": "C64 isolated MySQL restore; no production activation or file RPO claim",
    }


C64_RETRY_E2E_TITLES: Final = {
    "retry": "retry posts and reloads the failed-shard list",
    "export": "failed-shard export downloads the visible JSON response",
    "empty": "empty failed-shard list is visible and exports an empty JSON array",
    "export_error": "failed-shard export errors are visible to the operator",
    "load_error": "failed-shard load errors are visible and disable export",
}


@lru_cache(maxsize=1)
def run_c64_retry_e2e() -> dict[str, str]:
    """Execute the named retry/export browser cases once per measurement process."""
    titles = C64_RETRY_E2E_TITLES
    grep = "|".join(re.escape(title) for title in titles.values())
    code, output = run_argv(
        [
            "./node_modules/.bin/playwright",
            "test",
            "e2e/scripts.spec.ts",
            "--reporter=json",
            "--workers=1",
            "--retries=0",
            "--grep",
            grep,
        ],
        cwd=REPO_ROOT / "frontend",
    )
    return playwright_outcomes(output, titles, code)


def measure_c64_key_limit(ctx: Context) -> Facts:
    """Check key lifecycle, scope rejection and actual shared rolling-limit dependency."""
    source = ctx.read("opendata/api/dependencies.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_api_key_rate_limit.py::test_local_limit_allows_n_then_returns_positive_retry_after",
                "tests/test_api_key_rate_limit.py::test_local_window_rolls_over_at_sixty_seconds",
                "tests/test_api_key_rate_limit.py::test_local_concurrent_requests_never_admit_more_than_limit",
                "tests/test_api_key_rate_limit.py::test_api_key_limit_is_shared_across_current_principal_endpoints_and_not_testing_bypassed",
                "tests/test_api_key_rate_limit.py::test_scopes_and_invalid_key_lifecycle_are_checked_before_limiter",
                "tests/test_api_key_rate_limit.py::test_configured_redis_failure_is_503_without_local_fallback_or_secret_logging",
                "tests/test_api_keys.py::TestApiKeyEndpoints::test_creation_returns_the_plaintext_once",
                "tests/test_api_keys.py::TestApiKeyEndpoints::test_revoke_and_rotate_lifecycle",
                "tests/test_api_keys.py::TestApiKeyService::test_expired_key_stops_authenticating",
            ),
        ),
        "binding": flag(
            "api_key_rate_limiter.check(record.id, record.rate_limit)" in source
            and '"Retry-After"' in source
        ),
        "scope": "local key checks; Redis fake, deployment unverified",
    }


def measure_c64_ods_cache_rerun(ctx: Context) -> Facts:
    """Run one actual incremental job again after actual raw-cache invalidation."""
    source = ctx.read("opendata/pipeline/jobs.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_pipeline_jobs.py::TestRunIncrementalJob::test_ods_watermark_controls_reruns_after_raw_cache_invalidation",
                "tests/test_ods_watermark.py::test_reads_latest_mapped_ods_date_for_each_requested_symbol",
            ),
        ),
        "binding": flag("read_ods_watermarks(warehouse, domain, feed" in source),
        "scope": "actual job + SQLite ODS + raw-cache deletion; no provider requests",
    }


def measure_c64_retry_browser(ctx: Context) -> Facts:
    """Measure shard isolation and the browser's POST/reload/retry path."""
    browser = run_c64_retry_e2e()
    source = ctx.read("frontend/src/views/ExecutionsView.vue")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_pipeline_runner.py::TestPipelineRun::test_single_symbol_failure_does_not_block_its_shard",
                "tests/test_pipeline_retry.py::TestPipelineApi::test_retry_endpoint_is_idempotent_and_reports_the_count",
            ),
        ),
        "binding": flag(all(value == "passed" for value in browser.values()) and "retry" in source),
        "scope": f"local browser API fixtures {browser}; actual backend retry with SQLite controls",
    }


def measure_c64_export_browser(ctx: Context) -> Facts:
    """Measure actual JSON download, empty/error paths and backend retry idempotence."""
    browser = run_c64_retry_e2e()
    source = ctx.read("frontend/src/views/ExecutionsView.vue")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_pipeline_retry.py::TestPipelineApi::test_retry_endpoint_is_idempotent_and_reports_the_count",
                "tests/test_pipeline_retry.py::TestListFailures::test_lists_only_failed_shards",
            ),
        ),
        "binding": flag(
            all(value == "passed" for value in browser.values()) and "JSON.stringify" in source
        ),
        "scope": f"local browser downloads/errors {browser}; backend SQLite retry/list checks",
    }


def measure_c64_legacy_frontend(ctx: Context) -> Facts:
    """Measure legacy loader removal and load-bearing frontend quality/e2e targets."""
    makefile = ctx.read("Makefile")
    archive = ctx.read("docs/evidence/C64/frontend-checks.txt")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_interface_loader.py::TestInterfaceLoader::test_legacy_reflection_surface_is_gone",
                "tests/test_api_tasks_full.py::TestListTasks::test_list_tasks_as_user",
                "tests/test_api_tables_full.py::TestListTables::test_list_tables",
            ),
        ),
        "binding": flag(
            all(
                name in gate_members(makefile)
                for name in (
                    "frontend-lint",
                    "frontend-typecheck",
                    "frontend-collection",
                    "frontend-test",
                    "frontend-e2e",
                )
            )
            and "21 passed" in archive
        ),
        "scope": "current loader/route regression; frontend targets are actual gate members",
    }


def measure_c64_sdk_isolation(ctx: Context) -> Facts:
    """Measure optional SDK provider isolation."""
    witness = runtime_witness("sdk")
    uncovered = witness_list(witness, "uncovered")
    ecb_legs = int(witness.get("other_legs", 0))
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_iteration01_integration_contracts.py::test_missing_yfinance_sdk_does_not_block_ecb_registration_routing_or_fetch",
            ),
        ),
        "binding": flag(
            witness.get("sdk_absent") is True
            and int(witness.get("blocked_hits", 0)) > 0
            and int(witness.get("capabilities", 0)) > 0
            and not uncovered
            and int(witness.get("sdk_legs", 0)) > 0
            and ecb_legs > 0
            and int(witness.get("other_routed", 0)) == ecb_legs
        ),
        "scope": (
            "executed witness: 本机装着 yfinance，见证用 import hook 强制它缺席且真被拦下 "
            f"{witness.get('blocked_hits')} 次；注册仍得 {witness.get('capabilities')} 条能力"
            f"（可选方 yfinance 自己也有 {witness.get('sdk_legs')} 条在册、"
            f"缺席源面={uncovered or '-'}），"
            f"ECB {witness.get('other_routed')}/{ecb_legs} 条逐条路由成功；"
            "offline actual transport mocks，不含真实行情请求"
        ),
    }


def measure_c64_pipeline_socket(ctx: Context) -> Facts:
    """Measure ODS commit to actual local websocket notification."""
    source = ctx.read("opendata/pipeline/notify.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_iteration01_integration_contracts.py::test_pipeline_commits_ods_then_notifies_a_real_websocket_subscriber",
                "tests/test_data_subscribe.py::TestSocketAuth::test_first_frame_must_be_auth",
                "tests/test_data_subscribe.py::TestSocketDelivery::test_unsubscribed_domain_stops_receiving",
                "tests/test_data_subscribe.py::TestSocketSubscription::test_ping_gets_a_pong",
            ),
        ),
        "binding": flag(all(token in source for token in ("hub.publish", "record_batch"))),
        "scope": "real local WS chain; three SQLite adapters; separate MySQL writer evidence",
    }


def measure_c64_lifespan(ctx: Context) -> Facts:
    """Measure actual production lifespan scheduler ownership."""
    source = ctx.read("opendata/main.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_iteration01_integration_contracts.py::test_production_lifespan_rejects_unset_scheduler_before_scheduler_start",
                "tests/test_iteration01_integration_contracts.py::test_production_lifespan_starts_and_shuts_down_with_scheduler_explicitly_disabled",
            ),
        ),
        "binding": flag(
            all(token in source for token in ("scheduler_decision(", "settings.enable_scheduler"))
        ),
        "scope": "actual lifespan with safe DB/bootstrap seams; no deployment activation",
    }


def measure_c64_consumer_backtest(ctx: Context) -> Facts:
    """Measure degraded local consumer handoff and real backtest."""
    source = ctx.read("examples/consumer_handoff.py")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_iteration01_integration_contracts.py::test_consumer_handoff_example_reads_seeded_rows_from_local_api",
                "tests/test_iteration01_integration_contracts.py::test_consumer_handoff_example_runs_tiny_backtrader_case_when_installed",
            ),
        ),
        "binding": flag(all(token in source for token in ("OpendataClient", "cerebro.run()"))),
        "scope": "section0 fallback: local HTTP/Key/40 bars/backtrader; no platform claim",
    }


def measure_c64_consumer_golden(ctx: Context) -> Facts:
    """Measure fresh documented consumer script reproduction."""
    source = ctx.read("QUICKSTART.md")
    return {
        **node_plane_facts(
            "c64",
            (
                "tests/test_iteration01_integration_contracts.py::test_consumer_handoff_example_reads_seeded_rows_from_local_api",
            ),
        ),
        "binding": flag(
            all(
                token in source
                for token in ("OPENDATA_API_KEY", "examples/consumer_handoff.py", "--no-backtest")
            )
        ),
        "scope": "current script/local HTTP: 40 bars/calls/time/range; section0 fallback",
    }


BENCHMARK_EVIDENCE_REL = "scripts/quality/write_benchmark_evidence.py"
BENCHMARK_SCALE_NAMES = ("100000", "1000000", "full")
OPENBB_MAP_REL = "opendata/data/openbb_map.yaml"
REQUESTER_CONFIRMATION_REL = "docs/evidence/C65/provider-requester-confirmation.json"
EXPECTED_REQUESTER = "cloudQuant"
EXPECTED_REQUEST_PURPOSE = "量化研究、回测与数据中台"
EXPECTED_DIRECT_REQUESTER_REPLY = "cloudQuant负责全部33条，用于量化研究、回测与数据中台"
COMPARE_SCRIPT_REL = "scripts/codemod/compare_with_upstream.py"
AC6_B1_1_PLAN_GROUP_COUNTS: Final = {
    "stock_feature": 69,
    "stock_fundamental": 23,
    "futures": 32,
    "index": 27,
    "fund": 27,
    "option": 19,
    "bond": 17,
    "economic": 19,
}
AC6_B1_1_EXTRA_GROUPS: Final = frozenset({"futures_derivative"})
AC6_B1_1_EXPECTED_GROUPS: Final = frozenset({*AC6_B1_1_PLAN_GROUP_COUNTS, *AC6_B1_1_EXTRA_GROUPS})
AC6_B1_1_GROUP_NAMES: Final = ", ".join(sorted(AC6_B1_1_EXPECTED_GROUPS))
PORTED_MANIFEST_REL = "opendata_http/manifest.json"
UPSTREAM_LOCK_REL = "opendata_http/upstream.lock"


def _fact(value: object) -> str:
    """Serialize a measurement as a stable string for the probe framework."""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "-" if value is None else str(value)


def _positive_number(value: str) -> bool:
    """Whether one serialized measurement is finite and strictly positive."""
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(numeric) and numeric > 0


def _benchmark_facts(ctx: Context) -> Facts:
    """Read the benchmark evidence validator into string-only item facts."""
    facts: Facts = {
        "benchmark_valid": "no",
        "benchmark_issue_count": "1",
        "benchmark_issue_codes": "validator unavailable",
        "source_identity_verified": "no",
        "source_frozen_sha256": "-",
        "source_current_sha256": "-",
        "source_matches_frozen": "no",
        "source_initial_status_correction": "no",
        "benchmark_scale_count": "0",
        "benchmark_scales_present": "no",
        "memory_all_scales_below_2_gib": "no",
        "memory_full_peak_le_1m": "no",
        "memory_observation_scope": "-",
    }
    try:
        validator_module = script_module(BENCHMARK_EVIDENCE_REL)
        validate = validator_module.__dict__.get("validate")
        if not callable(validate):
            return facts
        result = validate(ctx.root)
        raw_facts = getattr(result, "facts", {})
        if not isinstance(raw_facts, dict):
            raw_facts = {}
        issues = getattr(result, "issues", ())
        if not isinstance(issues, (list, tuple)):
            issues = ()
        issue_codes = [str(getattr(issue, "code", "unknown")) for issue in issues]
        source = raw_facts.get("source_identity", raw_facts.get("source", {}))
        if not isinstance(source, dict):
            source = {}
        if isinstance(raw_facts.get("source_identity_verified"), bool):
            source_verified = raw_facts["source_identity_verified"]
        else:
            source_verified = source.get("source_identity_verified") is True
        scales = raw_facts.get("scales", {})
        if not isinstance(scales, dict):
            scales = {}
        memory = raw_facts.get("memory_observation", {})
        if not isinstance(memory, dict):
            memory = {}

        facts.update(
            benchmark_valid=_fact(getattr(result, "valid", False) is True),
            benchmark_issue_count=str(len(issues)),
            benchmark_issue_codes=", ".join(issue_codes[:8]) or "-",
            source_identity_verified=_fact(source_verified),
            source_frozen_sha256=_fact(source.get("frozen_sha256", source.get("expected_sha256"))),
            source_current_sha256=_fact(source.get("current_sha256")),
            source_matches_frozen=_fact(source.get("current_matches_frozen")),
            source_initial_status_correction=_fact(
                source.get("allowed_initial_state_status_correction")
            ),
            benchmark_scale_count=str(raw_facts.get("scale_count", len(scales))),
            benchmark_scales_present=_fact(set(scales) == set(BENCHMARK_SCALE_NAMES)),
            memory_all_scales_below_2_gib=_fact(memory.get("all_scales_below_2_gib")),
            memory_full_peak_le_1m=_fact(memory.get("full_peak_le_1000000_peak")),
            memory_observation_scope=_fact(memory.get("observation_scope")),
        )

        for scale in BENCHMARK_SCALE_NAMES:
            record = scales.get(scale, {})
            if not isinstance(record, dict):
                record = {}
            final = record.get("raw_final", {})
            if not isinstance(final, dict):
                final = {}
            prefix = f"{scale}_"
            raw_fields = {
                "rows_requested": record.get("rows_requested"),
                "source_rows_exact": record.get("source_rows_exact"),
                "rows_read": record.get("rows_read"),
                "rows_written": record.get("rows_written"),
                "pages_read": record.get("pages_read"),
                "pages_written": record.get("pages_written"),
                "target_rows_before": record.get("target_rows_before"),
                "target_rows_after": record.get("target_rows_after"),
                "elapsed_seconds": record.get("elapsed_seconds"),
                "current_rss_bytes": final.get("current_rss_bytes"),
                "peak_rss_bytes": record.get("peak_rss_bytes"),
                "status": record.get("status"),
                "raw_record_count": record.get("raw_record_count"),
                "historical_progress_status_error_count": record.get(
                    "historical_progress_status_error_count"
                ),
                "source": final.get("source"),
                "domain": final.get("domain"),
                "table": final.get("table"),
            }
            boolean_fields = {
                "raw_complete": record.get("raw_complete"),
                "source_sha256_bound": record.get("source_sha256_bound"),
                "body_final_identity": record.get("body_final_identity"),
                "historical_progress_annotation": record.get("historical_progress_annotation"),
            }
            facts.update({prefix + key: _fact(value) for key, value in raw_fields.items()})
            facts.update({prefix + key: _fact(value) for key, value in boolean_fields.items()})
            facts[prefix + "elapsed_positive"] = _fact(
                _positive_number(facts[prefix + "elapsed_seconds"])
            )
            facts[prefix + "current_rss_positive"] = _fact(
                _positive_number(facts[prefix + "current_rss_bytes"])
            )
            facts[prefix + "peak_rss_positive"] = _fact(
                _positive_number(facts[prefix + "peak_rss_bytes"])
            )
            facts[prefix + "rows_equal"] = _fact(
                facts[prefix + "rows_requested"]
                == facts[prefix + "rows_read"]
                == facts[prefix + "rows_written"]
            )
            facts[prefix + "source_rows_match"] = _fact(
                facts[prefix + "rows_requested"] == facts[prefix + "source_rows_exact"]
            )
            facts[prefix + "pages_equal"] = _fact(
                facts[prefix + "pages_read"] != "-"
                and facts[prefix + "pages_read"] == facts[prefix + "pages_written"]
            )
            facts[prefix + "target_rows_equal"] = _fact(
                facts[prefix + "target_rows_before"] == "0"
                and facts[prefix + "target_rows_after"] == facts[prefix + "rows_written"]
            )

        facts["all_scales_complete"] = _fact(
            facts["benchmark_scales_present"] == "yes"
            and all(
                facts[f"{scale}_{field}"] == "yes"
                for scale in BENCHMARK_SCALE_NAMES
                for field in (
                    "raw_complete",
                    "source_sha256_bound",
                    "body_final_identity",
                    "rows_equal",
                    "pages_equal",
                    "target_rows_equal",
                    "elapsed_positive",
                    "current_rss_positive",
                    "peak_rss_positive",
                )
            )
        )
        facts["full_peak_le_1m"] = facts["memory_full_peak_le_1m"]
        return facts
    except Exception as exc:
        facts["benchmark_issue_codes"] = f"validator raised {type(exc).__name__}"
        return facts


def judge_ac8_04(facts: Facts) -> Verdict:
    """Require a complete single-source full-run time and memory record."""
    ok = (
        facts["benchmark_valid"] == "yes"
        and facts["benchmark_issue_count"] == "0"
        and facts["source_identity_verified"] == "yes"
        and facts["full_raw_complete"] == "yes"
        and facts["full_source_sha256_bound"] == "yes"
        and facts["full_body_final_identity"] == "yes"
        and facts["full_status"] == "complete"
        and facts["full_rows_equal"] == "yes"
        and facts["full_source_rows_match"] == "yes"
        and facts["full_pages_equal"] == "yes"
        and facts["full_target_rows_equal"] == "yes"
        and facts["full_elapsed_positive"] == "yes"
        and facts["full_current_rss_positive"] == "yes"
        and facts["full_peak_rss_positive"] == "yes"
        and facts["full_source"] == "ths"
        and facts["full_domain"] == "stock_daily"
        and facts["full_table"] == "ods_stock_daily_ths"
    )
    readings = (
        f"benchmark validator = {facts['benchmark_valid']}，issues = "
        f"{facts['benchmark_issue_count']} ({facts['benchmark_issue_codes']})；"
        f"冻结源码身份 = {facts['source_identity_verified']}（current SHA exact match = "
        f"{facts['source_matches_frozen']}，已接受单行初始状态修正 = "
        f"{facts['source_initial_status_correction']}），当前 SHA = "
        f"{facts['source_current_sha256']}，冻结 SHA = {facts['source_frozen_sha256']}",
        f"full run: source={facts['full_source']} / {facts['full_domain']} / "
        f"{facts['full_table']}，rows requested/read/written/source = "
        f"{facts['full_rows_requested']}/{facts['full_rows_read']}/"
        f"{facts['full_rows_written']}/{facts['full_source_rows_exact']}，"
        f"pages read/written = {facts['full_pages_read']}/{facts['full_pages_written']}，"
        f"requested rows = total source rows = {facts['full_source_rows_match']}",
        f"full run status={facts['full_status']}，elapsed={facts['full_elapsed_seconds']} s，"
        f"current RSS={facts['full_current_rss_bytes']} bytes，"
        f"peak RSS={facts['full_peak_rss_bytes']} bytes；原始记录完整 = "
        f"{facts['full_raw_complete']}，matrix/driver/source 身份一致 = "
        f"{facts['full_body_final_identity']}/{facts['full_source_sha256_bound']}",
    )
    reason = (
        ""
        if ok
        else "AC-8|04 需要冻结源码下可逐项复核的单源全量行数、页数、耗时及 RSS 记录；"
        f"当前 validator issue={facts['benchmark_issue_count']}，full raw_complete="
        f"{facts['full_raw_complete']}。"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def judge_section5_01(facts: Facts) -> Verdict:
    """Require the measured three-scale memory observation without a fixed ratio threshold."""
    ok = (
        facts["benchmark_valid"] == "yes"
        and facts["benchmark_issue_count"] == "0"
        and facts["source_identity_verified"] == "yes"
        and facts["benchmark_scale_count"] == "3"
        and facts["benchmark_scales_present"] == "yes"
        and facts["all_scales_complete"] == "yes"
        and facts["full_peak_le_1m"] == "yes"
    )
    scale_readings = "; ".join(
        f"{scale}: rows={facts[f'{scale}_rows_read']}, pages={facts[f'{scale}_pages_read']}, "
        f"time={facts[f'{scale}_elapsed_seconds']} s, current/peak RSS="
        f"{facts[f'{scale}_current_rss_bytes']}/{facts[f'{scale}_peak_rss_bytes']} bytes"
        for scale in BENCHMARK_SCALE_NAMES
    )
    readings = (
        f"三档完整性 = {facts['all_scales_complete']}，scale count = "
        f"{facts['benchmark_scale_count']}，source identity = {facts['source_identity_verified']}；"
        f"validator valid/issues = {facts['benchmark_valid']}/{facts['benchmark_issue_count']}",
        scale_readings,
        f"实测 full peak={facts['full_peak_rss_bytes']} bytes，1m peak="
        f"{facts['1000000_peak_rss_bytes']} bytes，full <= 1m = {facts['full_peak_le_1m']}；"
        f"所有 scale <2GiB 观察值 = {facts['memory_all_scales_below_2_gib']}"
        f"（仅展示，不作为本项阈值；范围={facts['memory_observation_scope']}）",
    )
    reason = (
        ""
        if ok
        else "§5|01 需要三个规模均有冻结源码绑定的完整实测记录，并且本轮 full peak RSS "
        "不高于 1m peak；"
        f"当前三档完整={facts['all_scales_complete']}，full<=1m={facts['full_peak_le_1m']}。"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac10_02(ctx: Context) -> Facts:
    """Reconcile the requester's direct reply against every current active provider/domain leg."""
    problems: list[str] = []
    map_pairs: list[tuple[str, str]] = []
    map_scenarios: dict[tuple[str, str], str] = {}
    active_statuses = {"registered", "verified"}
    try:
        mapping = yaml.safe_load(ctx.read(OPENBB_MAP_REL))
        entries = mapping.get("entries") if isinstance(mapping, dict) else None
        if not isinstance(entries, list):
            problems.append("openbb map entries are missing")
            entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                problems.append("openbb map entry is not an object")
                continue
            scenario = entry.get("scenario")
            legs = entry.get("ours")
            if not isinstance(legs, list):
                problems.append("openbb map ours list is missing")
                continue
            for leg in legs:
                if not isinstance(leg, dict):
                    problems.append("openbb map leg is not an object")
                    continue
                status = leg.get("status")
                if status == "pending":
                    continue
                if status not in active_statuses:
                    problems.append("openbb map has an unknown active status")
                    continue
                provider = leg.get("provider")
                domain = leg.get("domain")
                pair = (provider, domain)
                if not all(isinstance(part, str) and part.strip() for part in pair):
                    problems.append("openbb map has an incomplete provider/domain pair")
                    continue
                canonical_pair = (str(provider), str(domain))
                map_pairs.append(canonical_pair)
                if not isinstance(scenario, str) or not scenario.strip():
                    problems.append("openbb map has an empty scenario")
                else:
                    map_scenarios[canonical_pair] = scenario
        map_pair_set = set(map_pairs)
        map_duplicates = len(map_pairs) - len(map_pair_set)
        if map_duplicates:
            problems.append("openbb map repeats an active provider/domain pair")
    except Exception as exc:
        mapping = {}
        map_pair_set = set()
        map_duplicates = 0
        problems.append(f"openbb map could not be read ({type(exc).__name__})")

    evidence: object = {}
    try:
        evidence = json.loads(ctx.read(REQUESTER_CONFIRMATION_REL))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ProbeError) as exc:
        problems.append(f"requester confirmation could not be read ({type(exc).__name__})")
    if not isinstance(evidence, dict):
        evidence = {}
        problems.append("requester confirmation is not an object")
    evidence_rows = evidence.get("legs")
    if not isinstance(evidence_rows, list):
        evidence_rows = []
        problems.append("requester confirmation legs are missing")

    evidence_pairs: list[tuple[str, str]] = []
    requester_missing = requester_mismatch = 0
    scenario_missing = scenario_mismatch = 0
    purpose_missing = purpose_mismatch = 0
    activation_unconfirmed = malformed_pairs = 0
    for row in evidence_rows:
        if not isinstance(row, dict):
            malformed_pairs += 1
            continue
        provider = row.get("provider")
        domain = row.get("domain")
        if not all(isinstance(part, str) and part.strip() for part in (provider, domain)):
            malformed_pairs += 1
        else:
            pair = (str(provider), str(domain))
            evidence_pairs.append(pair)
            scenario = row.get("scenario")
            if not isinstance(scenario, str) or not scenario.strip():
                scenario_missing += 1
            elif pair in map_scenarios and scenario != map_scenarios[pair]:
                scenario_mismatch += 1
        requester = row.get("requester")
        if not isinstance(requester, str) or not requester.strip():
            requester_missing += 1
        elif requester != EXPECTED_REQUESTER:
            requester_mismatch += 1
        purpose = row.get("purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            purpose_missing += 1
        elif purpose != EXPECTED_REQUEST_PURPOSE:
            purpose_mismatch += 1
        if row.get("continued_activation_confirmed") is not True:
            activation_unconfirmed += 1
    evidence_pair_set = set(evidence_pairs)
    evidence_duplicates = len(evidence_pairs) - len(evidence_pair_set)
    missing_pairs = map_pair_set - evidence_pair_set
    extra_pairs = evidence_pair_set - map_pair_set
    direct_reply = evidence.get("user_reply")
    reply_match = isinstance(direct_reply, str) and direct_reply == EXPECTED_DIRECT_REQUESTER_REPLY
    reply_count_match = (
        re.fullmatch(
            r"cloudQuant负责全部(\d+)条，用于量化研究、回测与数据中台",
            direct_reply,
        )
        if isinstance(direct_reply, str)
        else None
    )
    declared_reply_count = reply_count_match.group(1) if reply_count_match else "-"
    provider_count = len({provider for provider, _domain in map_pair_set})
    declared_count = evidence.get("count")
    declared_provider_count = evidence.get("provider_count")
    map_source_matches = evidence.get("source_map") == OPENBB_MAP_REL
    direct_human = (
        evidence.get("confirmation_kind") == "direct human user reply in current Codex chat"
        and reply_match
        and evidence.get("criterion") == "AC-10|02"
        and evidence.get("round") == "C65"
    )
    pair_set_equal = (
        map_pair_set == evidence_pair_set
        and map_duplicates == 0
        and evidence_duplicates == 0
        and malformed_pairs == 0
    )
    count_matches = (
        isinstance(declared_count, int)
        and not isinstance(declared_count, bool)
        and declared_count == len(map_pairs)
        and declared_reply_count == str(len(map_pairs))
        and len(evidence_rows) == len(map_pairs)
    )
    provider_count_matches = (
        isinstance(declared_provider_count, int)
        and not isinstance(declared_provider_count, bool)
        and declared_provider_count == provider_count
    )
    field_issue_count = sum(
        (
            requester_missing,
            requester_mismatch,
            scenario_missing,
            scenario_mismatch,
            purpose_missing,
            purpose_mismatch,
            activation_unconfirmed,
        )
    )
    problems.extend(
        summary
        for failed, summary in (
            (not pair_set_equal, "requester leg set differs from active map"),
            (not map_source_matches, "requester evidence source map differs"),
            (not direct_human, "direct human confirmation is missing or differs"),
            (not count_matches, "declared count differs from current active map"),
            (not provider_count_matches, "declared provider count differs from active map"),
            (field_issue_count > 0, "requester/scenario/purpose/activation fields are incomplete"),
        )
        if failed
    )
    return {
        "requester_evidence_valid": flag(not problems),
        "requester_problem_count": count(len(problems)),
        "requester_problem_summary": "; ".join(dict.fromkeys(problems)) or "-",
        "direct_human_confirmation": flag(direct_human),
        "direct_reply_matches": flag(reply_match),
        "reply_declared_count": declared_reply_count,
        "requester_source_map_matches": flag(map_source_matches),
        "active_map_leg_count": count(len(map_pairs)),
        "active_map_unique_pairs": count(len(map_pair_set)),
        "active_provider_count": count(provider_count),
        "map_duplicate_pairs": count(map_duplicates),
        "evidence_leg_count": count(len(evidence_rows)),
        "evidence_unique_pairs": count(len(evidence_pair_set)),
        "evidence_duplicate_pairs": count(evidence_duplicates),
        "missing_pairs": count(len(missing_pairs)),
        "extra_pairs": count(len(extra_pairs)),
        "pair_set_equal": flag(pair_set_equal),
        "declared_count_matches": flag(count_matches),
        "declared_provider_count_matches": flag(provider_count_matches),
        "scenario_missing": count(scenario_missing),
        "scenario_mismatch": count(scenario_mismatch),
        "requester_missing": count(requester_missing),
        "requester_mismatch": count(requester_mismatch),
        "purpose_missing": count(purpose_missing),
        "purpose_mismatch": count(purpose_mismatch),
        "activation_unconfirmed": count(activation_unconfirmed),
        "proof_scope": "requester/purpose/activation only; not copyright/source/field mapping",
    }


def judge_ac10_02(facts: Facts) -> Verdict:
    """Require direct requester confirmation for every current active provider/domain leg."""
    ok = (
        facts["requester_evidence_valid"] == "yes"
        and facts["direct_human_confirmation"] == "yes"
        and facts["direct_reply_matches"] == "yes"
        and facts["requester_source_map_matches"] == "yes"
        and facts["pair_set_equal"] == "yes"
        and facts["declared_count_matches"] == "yes"
        and facts["declared_provider_count_matches"] == "yes"
        and facts["missing_pairs"] == "0"
        and facts["extra_pairs"] == "0"
        and facts["map_duplicate_pairs"] == "0"
        and facts["evidence_duplicate_pairs"] == "0"
        and facts["scenario_missing"] == "0"
        and facts["scenario_mismatch"] == "0"
        and facts["requester_missing"] == "0"
        and facts["requester_mismatch"] == "0"
        and facts["purpose_missing"] == "0"
        and facts["purpose_mismatch"] == "0"
        and facts["activation_unconfirmed"] == "0"
    )
    readings = (
        f"当前活跃 openbb_map legs = {facts['active_map_leg_count']} 条 / "
        f"{facts['active_provider_count']} providers；确认记录 = "
        f"{facts['evidence_leg_count']} 条，pair set 精确相等 = {facts['pair_set_equal']} "
        f"（missing={facts['missing_pairs']}，extra={facts['extra_pairs']}，"
        f"duplicates={facts['map_duplicate_pairs']}/{facts['evidence_duplicate_pairs']}）",
        f"直接人类回复与请求方 = {facts['direct_human_confirmation']}/"
        f"{facts['direct_reply_matches']}；空场景/请求方/用途 = "
        f"{facts['scenario_missing']}/{facts['requester_missing']}/{facts['purpose_missing']}；"
        f"用途错配={facts['purpose_mismatch']}，activation 未确认="
        f"{facts['activation_unconfirmed']}",
        f"确认范围：{facts['proof_scope']}；issues={facts['requester_problem_count']}："
        f"{facts['requester_problem_summary']}",
    )
    reason = (
        ""
        if ok
        else "AC-10|02 需要请求方对当前每个 active provider/domain leg 的场景、用途和"
        "持续启用作直接确认；"
        f"当前逐项核对问题={facts['requester_problem_summary']}。"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac10_03(ctx: Context) -> Facts:
    """Validate the current provider source review and its bounded positive findings."""
    root_entry = str(ctx.root.resolve())
    inserted_root = root_entry not in sys.path
    if inserted_root:
        sys.path.insert(0, root_entry)
    try:
        from scripts.quality import provider_source_review_evidence as provider_review

        result = provider_review.validate(ctx.root)
    finally:
        if inserted_root:
            with suppress(ValueError):
                sys.path.remove(root_entry)

    measured = result.facts
    issue_codes = [issue.code for issue in result.issues]
    nearest_review: dict[str, Any] = {}
    try:
        nearest_review_value = json.loads(ctx.read(provider_review.NEAREST_REVIEW_REL.as_posix()))
        if isinstance(nearest_review_value, dict):
            nearest_review = nearest_review_value
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ProbeError):
        pass

    provider_names = measured.get("provider_names")
    provider_names_text = (
        ", ".join(str(name) for name in provider_names) if isinstance(provider_names, list) else "-"
    )
    reviewed_provider_names = measured.get("reviewed_provider_names")
    reviewed_provider_names_text = (
        ", ".join(str(name) for name in reviewed_provider_names)
        if isinstance(reviewed_provider_names, list)
        else "-"
    )
    current_hashes = measured.get("current_source_files_sha256")
    source_hash_count = len(current_hashes) if isinstance(current_hashes, dict) else 0
    bundle_paths = set(current_hashes) if isinstance(current_hashes, dict) else set()
    bundle_file_counts = measured.get("provider_file_counts")
    bundle_counts = (
        {str(name): int(count) for name, count in bundle_file_counts.items()}
        if isinstance(bundle_file_counts, dict)
        else {}
    )

    # The review population, counted a second way. ``_discover_sources`` walks the worktree with
    # ``rglob``; this walks the git index and keeps only paths that are still on disk. Agreement
    # between the two is the face that a literal package count could never be: an untracked file
    # dropped into a provider package, or a package registered without its review row, moves one
    # side and reads as a diff instead of silently shrinking or growing a hardcoded number.
    registry_packages = provider_packages(ctx)
    registry_files = {name: provider_py_files(ctx, name) for name in registry_packages}
    registry_total = sum(len(paths) for paths in registry_files.values())
    registry_paths = {rel for paths in registry_files.values() for rel in paths}
    names_text = ", ".join(registry_packages)
    registry_only_names = sorted(set(registry_packages) - set(bundle_counts))
    bundle_only_names = sorted(set(bundle_counts) - set(registry_packages))
    count_mismatch_packages = sorted(
        name
        for name in set(registry_files) & set(bundle_counts)
        if len(registry_files[name]) != bundle_counts[name]
    )
    registry_only_paths = sorted(registry_paths - bundle_paths)
    bundle_only_paths = sorted(bundle_paths - registry_paths)
    source_faces = (
        number(str(measured.get("python_files", "-"))),
        source_hash_count,
        number(str(measured.get("reviewed_python_files", "-"))),
        number(str(measured.get("similarity_manifest_python_files", "-"))),
    )

    # The baseline side stays a *pinned* scope (a separate open question from the local census), so
    # what the judge can require is that the artifact compared the packages the harness declares --
    # derived here from the manifest paths, not from the artifact's own provider list.
    baseline_names: list[str] = []
    try:
        similarity_value = json.loads(ctx.read(provider_review.SIMILARITY_REL.as_posix()))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ProbeError):
        similarity_value = None
    if isinstance(similarity_value, dict):
        manifests = similarity_value.get("source_manifests")
        rows = manifests.get("baseline_files_sha256") if isinstance(manifests, dict) else None
        seen: set[str] = set()
        for row in rows if isinstance(rows, list) else []:
            parts = str(row.get("path", "")).split("/") if isinstance(row, dict) else []
            if len(parts) > 2 and parts[0] == "openbb_platform" and parts[1] == "providers":
                seen.add(parts[2])
        baseline_names = sorted(seen)
    file_set_or_hash_issues = any(
        code.endswith("-hash-mismatch") or code.endswith("-file-set-mismatch")
        for code in issue_codes
    )
    nearest_rows = nearest_review.get("nearest_pair_reviews")
    nearest_results = (
        [row.get("result") for row in nearest_rows if isinstance(row, dict)]
        if isinstance(nearest_rows, list)
        else []
    )
    nearest_positive_result = (
        isinstance(nearest_rows, list)
        and len(nearest_rows) == 3
        and len(nearest_results) == 3
        and all(
            value == provider_review.EXPECTED_NEAREST_REVIEW_RESULT for value in nearest_results
        )
    )
    package_reviews_complete = (
        isinstance(provider_names, list)
        and isinstance(reviewed_provider_names, list)
        and provider_names == reviewed_provider_names
        and len(reviewed_provider_names) == number(str(measured.get("provider_count", "-")))
    )
    return {
        "provider_review_valid": flag(result.valid),
        "provider_review_issue_count": count(len(result.issues)),
        "provider_review_issue_codes": ", ".join(issue_codes[:8]) or "-",
        "provider_scope": str(measured.get("scope", "-")),
        "provider_count": str(measured.get("provider_count", "-")),
        "provider_names": provider_names_text,
        "reviewed_provider_names": reviewed_provider_names_text,
        "registry_provider_names": names_text,
        "registry_source_file_count": str(registry_total),
        "providers_missing_from_review": ", ".join(registry_only_names) or "-",
        "reviewed_providers_not_registered": ", ".join(bundle_only_names) or "-",
        "per_package_file_count_mismatches": ", ".join(count_mismatch_packages) or "-",
        "registry_files_outside_review": count(len(registry_only_paths)),
        "review_files_outside_registry": count(len(bundle_only_paths)),
        "review_population_matches_registry": flag(
            registry_total > 0
            and not registry_only_names
            and not bundle_only_names
            and not count_mismatch_packages
            and not registry_only_paths
            and not bundle_only_paths
            and provider_names_text == names_text
        ),
        "source_faces_match_registry": flag(
            registry_total > 0 and all(value == registry_total for value in source_faces)
        ),
        "baseline_provider_names": ", ".join(baseline_names) or "-",
        "baseline_providers_pinned": ", ".join(sorted(provider_review.EXPECTED_BASELINE_PROVIDERS)),
        "baseline_scope_matches_pinned": flag(
            bool(baseline_names)
            and set(baseline_names) == set(provider_review.EXPECTED_BASELINE_PROVIDERS)
        ),
        "openbb_baseline_expected": provider_review.EXPECTED_OPENBB_COMMIT,
        "current_source_file_count": str(measured.get("python_files", "-")),
        "source_hash_entry_count": count(source_hash_count),
        "reviewed_source_file_count": str(measured.get("reviewed_python_files", "-")),
        "similarity_source_file_count": str(measured.get("similarity_manifest_python_files", "-")),
        "baseline_source_file_count": str(measured.get("baseline_manifest_python_files", "-")),
        "current_source_hashes_bound": flag(not file_set_or_hash_issues),
        "provider_sources_parsed": flag(measured.get("all_current_provider_python_parsed") is True),
        "openbb_import_count": str(measured.get("openbb_import_count", "-")),
        "zero_openbb_imports": flag(measured.get("zero_openbb_imports") is True),
        "reviewer_authorized": flag(measured.get("reviewer_authorized") is True),
        "openbb_baseline_commit": str(measured.get("openbb_baseline_commit", "-")),
        "candidate_count": str(measured.get("candidate_count", "-")),
        "unreviewed_candidates": str(measured.get("unreviewed_candidates", "-")),
        "package_reviews_complete": flag(package_reviews_complete),
        "nearest_review_complete": flag(nearest_review.get("review_complete") is True),
        "review_complete": flag(
            package_reviews_complete and nearest_review.get("review_complete") is True
        ),
        "nearest_review_count": str(measured.get("nearest_review_count", "-")),
        "nearest_positive_result": flag(nearest_positive_result),
        "artifact_sha_bindings_match": flag(measured.get("artifact_sha_bindings_match") is True),
    }


def judge_ac10_03(facts: Facts) -> Verdict:
    """Require the review to cover the registry's own census, file for file.

    Nothing here is a package or file count typed into the judge. The local population is two
    independent measurements that have to agree (the git-index census the AC-16 face builds, and the
    worktree walk the evidence validator does), and the baseline population is the provider set the
    harness pins, re-derived from the manifest paths the artifact actually compares. Registering a
    provider without reviewing it, leaving a file out of the bundle, or reviewing a package that is
    no longer registered each move one side of an equality, so the item is closeable by real work on
    a 12-package tree exactly as on a 7-package one.
    """
    ok = all(
        (
            facts["provider_review_valid"] == "yes",
            facts["provider_review_issue_count"] == "0",
            facts["provider_scope"] == "current_registered_provider_packages",
            facts["review_population_matches_registry"] == "yes",
            facts["source_faces_match_registry"] == "yes",
            facts["current_source_hashes_bound"] == "yes",
            facts["provider_sources_parsed"] == "yes",
            facts["openbb_import_count"] == "0",
            facts["zero_openbb_imports"] == "yes",
            facts["reviewer_authorized"] == "yes",
            facts["openbb_baseline_commit"] == facts["openbb_baseline_expected"],
            facts["baseline_scope_matches_pinned"] == "yes",
            number(facts["baseline_source_file_count"]) > 0,
            facts["candidate_count"] == "0",
            facts["unreviewed_candidates"] == "0",
            facts["package_reviews_complete"] == "yes",
            facts["nearest_review_complete"] == "yes",
            facts["review_complete"] == "yes",
            facts["nearest_review_count"] == "3",
            facts["nearest_positive_result"] == "yes",
            facts["artifact_sha_bindings_match"] == "yes",
        )
    )
    readings = (
        f"注册面（git index 普查）= {facts['registry_provider_names']}"
        f"（{facts['registry_source_file_count']} 个 py）；审阅档案发现面 = "
        f"{facts['provider_names']}（{facts['provider_count']} 包 / "
        f"{facts['current_source_file_count']} py）；两包面差 = 缺档 "
        f"{facts['providers_missing_from_review']} / 多余 "
        f"{facts['reviewed_providers_not_registered']}，逐包文件数错配 = "
        f"{facts['per_package_file_count_mismatches']}，文件集差 = 档案外 "
        f"{facts['registry_files_outside_review']} / 注册外 "
        f"{facts['review_files_outside_registry']}；包面/文件面对齐 = "
        f"{facts['review_population_matches_registry']}",
        f"源码/SHA 台账/审阅/相似度四份面 = {facts['current_source_file_count']}/"
        f"{facts['source_hash_entry_count']}/{facts['reviewed_source_file_count']}/"
        f"{facts['similarity_source_file_count']}，全部等于注册普查 = "
        f"{facts['source_faces_match_registry']}；源 SHA 绑定完整 = "
        f"{facts['current_source_hashes_bound']}，源码可解析 = {facts['provider_sources_parsed']}，"
        f"OpenBB import = {facts['openbb_import_count']}（零导入={facts['zero_openbb_imports']}）",
        f"基线固定 = {facts['openbb_baseline_commit']}（期望 "
        f"{facts['openbb_baseline_expected']}），"
        f"基线包面 = {facts['baseline_provider_names']}（钉住 "
        f"{facts['baseline_providers_pinned']}，对齐={facts['baseline_scope_matches_pinned']}），"
        f"基线 py = {facts['baseline_source_file_count']} 个；"
        f"审阅授权 = {facts['reviewer_authorized']}",
        f"候选/未审候选 = {facts['candidate_count']}/{facts['unreviewed_candidates']}，审阅完成 = "
        f"{facts['review_complete']}（packages={facts['package_reviews_complete']}，"
        f"nearest={facts['nearest_review_complete']}）；nearest review = "
        f"{facts['nearest_review_count']} 项，正向结论 = {facts['nearest_positive_result']}，"
        f"档案 SHA 互绑 = {facts['artifact_sha_bindings_match']}；"
        f"validator valid/issues = {facts['provider_review_valid']}/"
        f"{facts['provider_review_issue_count']}（{facts['provider_review_issue_codes']}）",
    )
    reason = (
        ""
        if ok
        else "AC-10|03 需要审阅档案覆盖当前注册 provider 的普查面（包名、逐包文件数、文件集、四份"
        "文件计数互相对齐），并与固定的 OpenBB 基线包面与提交绑定，保留零导入、候选清零与已完成的"
        "正向审阅结论；"
        f"当前缺档={facts['providers_missing_from_review']}，档案外文件="
        f"{facts['registry_files_outside_review']}，注册外文件="
        f"{facts['review_files_outside_registry']}，基线包面对齐="
        f"{facts['baseline_scope_matches_pinned']}，问题摘要={facts['provider_review_issue_codes']}。"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


def _case_source_mapping(
    ctx: Context,
    compare_module: ModuleType,
    scope_rows: dict[str, dict[str, Any]],
    manifest_rows: dict[str, dict[str, Any]],
    lock_rows: dict[str, dict[str, Any]],
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Bind each compare case's runtime function to a hashed, scope-labelled source path."""
    case_modules: dict[str, tuple[str, str]] = {}
    problems: list[str] = []
    loader = compare_module.__dict__.get("_load_case_function")
    package_name = compare_module.__dict__.get("_PORTED_CASE_MODULE")
    cases = compare_module.__dict__.get("CASES")
    if not callable(loader) or not isinstance(package_name, str) or not isinstance(cases, tuple):
        return {}, ["compare module CASES/source loader is unavailable"]
    try:
        layout = script_module("scripts/quality/source_layout.py")
        canonical_root_identity = layout.historical_identity("opendata_http")
        repo_root = ctx.root.resolve()
        port_root = (repo_root / canonical_root_identity).resolve(strict=True)
        port_root.relative_to(repo_root)
        if not port_root.is_dir():
            raise ValueError("canonical vendor root is not a directory")
    except (OSError, RuntimeError, ValueError) as exc:
        return {}, [f"canonical vendor root unavailable ({type(exc).__name__})"]
    for case in cases:
        name = getattr(case, "name", None)
        function_name = getattr(case, "function", None)
        if not isinstance(name, str) or not isinstance(function_name, str):
            problems.append("compare case has no name/function")
            continue
        try:
            function = loader(package_name, function_name)
            source = inspect.getsourcefile(function)
            if source is None:
                raise ValueError("function source is unavailable")
            source_file = Path(source).resolve()
            relative = source_file.relative_to(port_root).as_posix()
            scope_row = scope_rows.get(relative)
            manifest_row = manifest_rows.get(relative)
            lock_row = lock_rows.get(relative)
            if scope_row is None or manifest_row is None or lock_row is None:
                raise ValueError("source path is not present in lock/manifest/scope rows")
            if not source_file.is_file():
                raise ValueError("source file is not on disk")
            source_sha = hashlib.sha256(source_file.read_bytes()).hexdigest()
            scope_sha = scope_row.get("sha256")
            scope_sha = scope_sha if isinstance(scope_sha, dict) else {}
            module = PurePosixPath(relative).parts[0]
            batch = scope_row.get("batch")
            expected_batch = "B1.1_FUTURES_DERIVATIVE" if module == "futures_derivative" else "B1.1"
            if not isinstance(batch, list) or not all(isinstance(name, str) for name in batch):
                raise ValueError("source batch tags are not a string list")
            if module in AC6_B1_1_EXPECTED_GROUPS:
                batch_matches_group = expected_batch in batch
            else:
                batch_matches_group = not {
                    "B1.1",
                    "B1.1_FUTURES_DERIVATIVE",
                }.intersection(batch)
            if (
                scope_row.get("module") != module
                or scope_row.get("kind") != "python"
                or not batch_matches_group
                or not isinstance(manifest_row.get("sha256"), str)
                or manifest_row.get("sha256") != source_sha
                or lock_row.get("sha256") != scope_sha.get("upstream_lock")
                or scope_sha.get("manifest_ported_snapshot") != source_sha
            ):
                raise ValueError("source path/module/hash differs from validated port scope")
            case_modules[name] = (relative, module)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            problems.append(f"{name}: {type(exc).__name__}")
    return case_modules, problems


def measure_ac6_02(ctx: Context) -> Facts:
    """Replay recorded fixtures offline and derive actual 1B module-group coverage."""
    problems: list[str] = []
    scope_valid = False
    expected_groups = set(AC6_B1_1_EXPECTED_GROUPS)
    scope_rows: dict[str, dict[str, Any]] = {}
    manifest_rows: dict[str, dict[str, Any]] = {}
    lock_rows: dict[str, dict[str, Any]] = {}
    b1_python_paths: set[str] = set()
    scope_groups: set[str] = set()
    scope_groups_valid = False
    try:
        scope_module = script_module("scripts/quality/port_scope.py")
        audit_port_scope = scope_module.__dict__.get("audit_port_scope")
        audit = audit_port_scope(ctx.root) if callable(audit_port_scope) else None
        scope_valid = getattr(audit, "valid", False) is True
        if not scope_valid:
            problems.append(str(getattr(audit, "problem_summary", "port scope unavailable")))
        raw_plan_counts = getattr(scope_module, "B1_1_PLAN_MODULE_FILE_COUNTS", {})
        raw_extra_groups = getattr(scope_module, "B1_1_EXTRA_D9_MODULES", ())
        raw_python_file_count = getattr(scope_module, "B1_1_PYTHON_FILE_COUNT", None)
        port_scope_definition_valid = (
            isinstance(raw_plan_counts, dict)
            and raw_plan_counts == AC6_B1_1_PLAN_GROUP_COUNTS
            and set(raw_extra_groups) == set(AC6_B1_1_EXTRA_GROUPS)
            and raw_python_file_count == 245
        )
        if not port_scope_definition_valid:
            problems.append("port_scope B1.1 definitions differ from the fixed 9-group denominator")
        inventory_rel = str(getattr(audit, "inventory_rel", "-"))
        if inventory_rel == "-":
            problems.append("no archived port scope inventory could be resolved")
            inventory: dict[str, Any] = {}
        else:
            inventory = json.loads(ctx.read(inventory_rel))
        manifest = json.loads(_read_historical_source(ctx, PORTED_MANIFEST_REL))
        lock = json.loads(_read_historical_source(ctx, UPSTREAM_LOCK_REL))
        for row in inventory.get("files", []):
            if isinstance(row, dict) and isinstance(row.get("path"), str):
                if row["path"] in scope_rows:
                    problems.append("duplicate path in port scope inventory")
                scope_rows[row["path"]] = row
        for row in manifest.get("files", []) + manifest.get("resources", []):
            if isinstance(row, dict) and isinstance(row.get("path"), str):
                if row["path"] in manifest_rows:
                    problems.append("duplicate path in port manifest")
                manifest_rows[row["path"]] = row
        for row in lock.get("files", []):
            if isinstance(row, dict) and isinstance(row.get("path"), str):
                if row["path"] in lock_rows:
                    problems.append("duplicate path in upstream.lock")
                lock_rows[row["path"]] = row
        if not isinstance(inventory.get("files"), list):
            problems.append("port scope inventory files are not a list")
        for path, row in scope_rows.items():
            batch = row.get("batch")
            module = PurePosixPath(path).parts[0]
            if not isinstance(batch, list):
                continue
            if (
                ("B1.1" in batch or "B1.1_FUTURES_DERIVATIVE" in batch)
                and row.get("kind") == "python"
                and path.endswith(".py")
            ):
                b1_python_paths.add(path)
                scope_groups.add(module)
        scope_groups_valid = (
            scope_valid and port_scope_definition_valid and scope_groups == expected_groups
        )
        if not scope_groups_valid:
            problems.append(
                "validated B1.1 scope does not contain exactly the nine required groups"
            )
    except Exception as exc:
        scope_groups_valid = False
        problems.append(f"port scope inputs unavailable ({type(exc).__name__})")

    case_modules: dict[str, tuple[str, str]] = {}
    case_path_problems: list[str] = []
    # token门禁状态枚举非凭据：PASS/FAIL/PENDING 表示比较结果状态。
    case_status_counts = {"PASS": 0, "FAIL": 0, "PENDING": 0}  # nosec B105  # gate status enum
    pass_groups: set[str] = set()
    pass_files: set[str] = set()
    fail_case_names: list[str] = []
    status_valid = False
    report_written = False
    compare_exit: int | None = None
    rtol = "-"
    d10_failed: bool | None = None
    case_count = fail_count = pending_count = 0
    pending_names: list[str] = []
    try:
        compare_module = script_module(COMPARE_SCRIPT_REL)
        cases = compare_module.__dict__.get("CASES")
        if not isinstance(cases, tuple):
            raise ValueError("compare CASES is not a tuple")
        case_count = len(cases)
        rtol = str(getattr(compare_module, "RTOL", "-"))
        case_modules, case_path_problems = _case_source_mapping(
            ctx, compare_module, scope_rows, manifest_rows, lock_rows
        )
        compare_namespace = compare_module.__dict__
        original_report_path = compare_namespace["REPORT_PATH"]
        original_fixture_dir = compare_namespace["FIXTURES_DIR"]
        original_writer = compare_namespace["_write_report"]
        original_env = {
            key: os.environ.get(key)
            for key in (
                "AKSHARE_EASTMONEY_AUTO_CURL_INTERFACE",
                "AKSHARE_EASTMONEY_CURL_INTERFACE",
            )
        }
        capture: dict[str, Any] = {}

        def capture_report(
            results: list[dict[str, Any]], d10_report: dict[str, Any], pending: list[str]
        ) -> None:
            capture["results"] = list(results)
            capture["d10"] = dict(d10_report)
            capture["pending"] = list(pending)
            original_writer(results, d10_report, pending)

        try:
            with tempfile.TemporaryDirectory(prefix="opendata-c65-compare-") as temp_dir:
                temporary_report = Path(temp_dir) / "compare-report.md"
                compare_namespace["REPORT_PATH"] = temporary_report
                compare_namespace["FIXTURES_DIR"] = ctx.root / "tests" / "fixtures" / "upstream"
                compare_namespace["_write_report"] = capture_report
                with redirect_stdout(io.StringIO()):
                    compare_exit = compare_module.compare()
                report_written = (
                    temporary_report.is_file()
                    and temporary_report.parent.resolve() == Path(temp_dir).resolve()
                )
        finally:
            compare_namespace["REPORT_PATH"] = original_report_path
            compare_namespace["FIXTURES_DIR"] = original_fixture_dir
            compare_namespace["_write_report"] = original_writer
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        results = capture.get("results", [])
        pending_value = capture.get("pending", [])
        if isinstance(pending_value, list):
            pending_names = pending_value
        else:
            pending_names = []
            problems.append("offline compare pending result shape is invalid")
        result_by_name: dict[str, dict[str, Any]] = {}
        duplicate_results = False
        for result in results if isinstance(results, list) else []:
            case = result.get("case") if isinstance(result, dict) else None
            name = getattr(case, "name", None)
            if not isinstance(name, str) or name in result_by_name:
                duplicate_results = True
                continue
            result_by_name[name] = result
        pending_set = set(pending_names) if isinstance(pending_names, list) else set()
        case_names = [getattr(case, "name", None) for case in cases]
        expected_names = {name for name in case_names if isinstance(name, str)}
        status_names = set(result_by_name) | pending_set
        status_valid = (
            len(case_names) == len(expected_names)
            and not duplicate_results
            and len(pending_set) == len(pending_names)
            and set(result_by_name).isdisjoint(pending_set)
            and status_names == expected_names
        )
        if not status_valid:
            problems.append(
                "offline compare results do not classify every unique Case exactly once"
            )
        for case in cases:
            name = getattr(case, "name", None)
            if not isinstance(name, str):
                continue
            result = result_by_name.get(name)
            if result is not None:
                status = "PASS" if result.get("ok") is True else "FAIL"
                if status == "FAIL":
                    fail_case_names.append(name)
            elif name in pending_set:
                status = "PENDING"
            else:
                continue
            case_status_counts[status] += 1
            case_modules_for_case = case_modules.get(name)
            if status == "PASS" and case_modules_for_case is not None:
                path, module = case_modules_for_case
                scope_row = scope_rows.get(path, {})
                batch = scope_row.get("batch", [])
                if module in expected_groups and isinstance(batch, list):
                    expected_batch = (
                        "B1.1_FUTURES_DERIVATIVE" if module == "futures_derivative" else "B1.1"
                    )
                    if expected_batch in batch:
                        pass_groups.add(module)
                        pass_files.add(path)
        fail_count = case_status_counts["FAIL"]
        pending_count = case_status_counts["PENDING"]
        d10 = capture.get("d10")
        if isinstance(d10, dict) and isinstance(d10.get("failed"), bool):
            d10_failed = d10["failed"]
        expected_exit = int(fail_count > 0 or pending_count > 0 or d10_failed is True)
        exit_consistent = compare_exit == expected_exit
        if not exit_consistent:
            problems.append("compare process exit does not match FAIL/PENDING/D10 findings")
    except Exception as exc:
        case_path_problems.append(f"compare replay failed ({type(exc).__name__})")
        exit_consistent = False
        problems.append(f"offline compare unavailable ({type(exc).__name__})")

    pass_group_count = len(pass_groups)
    group_total = len(expected_groups)
    required_groups = math.ceil(group_total * 0.2) if group_total else 0
    file_denominator = len(b1_python_paths)
    file_coverage = (100 * len(pass_files) / file_denominator) if file_denominator else 0.0
    group_coverage = (100 * pass_group_count / group_total) if group_total else 0.0
    case_paths_valid = (
        bool(case_modules) and not case_path_problems and len(case_modules) == case_count
    )
    comparison_pass = case_status_counts["PASS"]
    comparison_pending = case_status_counts["PENDING"]
    comparison_fail = case_status_counts["FAIL"]
    return {
        "port_scope_valid": flag(scope_valid),
        "port_scope_problem_summary": "; ".join(problems[:5]) or "-",
        "b1_scope_groups_valid": flag(scope_groups_valid),
        "b1_scope_group_count": count(group_total),
        "b1_scope_group_names": AC6_B1_1_GROUP_NAMES,
        "b1_python_file_count": count(file_denominator),
        "case_path_mapping_valid": flag(case_paths_valid),
        "case_path_problem_count": count(len(case_path_problems)),
        "case_path_problem_summary": "; ".join(case_path_problems[:5]) or "-",
        "case_count": count(case_count),
        "case_statuses_complete": flag(
            case_count > 0 and status_valid and sum(case_status_counts.values()) == case_count
        ),
        "case_pass_count": count(comparison_pass),
        "case_fail_count": count(comparison_fail),
        "case_pending_count": count(comparison_pending),
        "pending_excluded": "yes",
        "compare_exit_code": _fact(compare_exit),
        "compare_exit_consistent": flag(exit_consistent),
        "compare_report_temporary": flag(report_written),
        "compare_rtol": rtol,
        "d10_failed": _fact(d10_failed),
        "passing_groups": count(pass_group_count),
        "passing_group_names": ", ".join(sorted(pass_groups)) or "-",
        "required_groups": count(required_groups),
        "group_coverage_percent": f"{group_coverage:.2f}",
        "pass_files": count(len(pass_files)),
        "file_coverage_percent": f"{file_coverage:.2f}",
        "pending_case_names": ", ".join(str(name) for name in pending_names[:8]) or "-",
        "fail_case_names": ", ".join(fail_case_names[:8]) or "-",
        "sample_scope_note": "module-group denominator; file percentage is disclosed only",
    }


def judge_ac6_02(facts: Facts) -> Verdict:
    """Require actual offline PASS cases to cover at least 20% of the validated B1.1 groups."""
    try:
        enough_groups = int(facts["passing_groups"]) >= int(facts["required_groups"])
        valid_denominator = int(facts["b1_scope_group_count"]) == 9
        file_denominator_valid = int(facts["b1_python_file_count"]) == 245
        pass_files_positive = int(facts["pass_files"]) > 0
    except (KeyError, ValueError):
        enough_groups = valid_denominator = file_denominator_valid = pass_files_positive = False
    ok = (
        facts["port_scope_valid"] == "yes"
        and facts["b1_scope_groups_valid"] == "yes"
        and facts["b1_scope_group_names"] == AC6_B1_1_GROUP_NAMES
        and facts["case_path_mapping_valid"] == "yes"
        and facts["case_statuses_complete"] == "yes"
        and facts["compare_report_temporary"] == "yes"
        and facts["compare_rtol"] == "1e-09"
        and facts["case_fail_count"] == "0"
        and facts["pending_excluded"] == "yes"
        and facts["compare_exit_consistent"] == "yes"
        and facts["d10_failed"] == "no"
        and valid_denominator
        and file_denominator_valid
        and enough_groups
        and pass_files_positive
    )
    readings = (
        f"当前 Compare Case = {facts['case_count']}：PASS/PENDING/FAIL = "
        f"{facts['case_pass_count']}/{facts['case_pending_count']}/{facts['case_fail_count']}；"
        f"原 compare exit={facts['compare_exit_code']}（与未完成项一致="
        f"{facts['compare_exit_consistent']}），D10 failed={facts['d10_failed']}",
        f"经 port_scope 逐路径验证的B1.1组：PASS覆盖 {facts['passing_groups']}/"
        f"{facts['b1_scope_group_count']}（{facts['group_coverage_percent']}%），"
        f"最低组数={facts['required_groups']}；覆盖组={facts['passing_group_names']}",
        f"PASS source paths={facts['pass_files']}/{facts['b1_python_file_count']} py "
        f"（{facts['file_coverage_percent']}%，披露用，不作为20%分母）；"
        f"Pending={facts['pending_case_names']}，不计入PASS；rtol={facts['compare_rtol']}，"
        f"报告仅写入临时目录={facts['compare_report_temporary']}",
        f"source paths/hashes与批次核对 = {facts['case_path_mapping_valid']}；"
        f"scope问题={facts['port_scope_problem_summary']}；路径问题="
        f"{facts['case_path_problem_summary']}",
    )
    reason = (
        ""
        if ok
        else "AC-6|02 需要以真实离线对照 PASS 覆盖至少20%的已核验B1.1模块组，且任何 FAIL、"
        "虚假来源路径/批次或容忍度改动都不能通过；PENDING不计分子。"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


# --------------------------------------------------------------------------- #
# C79 -- planes for the quality-layer cells the ledger called proven with no probe
# --------------------------------------------------------------------------- #

FRONTEND_GATE_TARGETS: Final = (
    "frontend-lint",
    "frontend-typecheck",
    "frontend-collection",
    "frontend-test",
    "frontend-e2e",
)
A2_CHECK_REL: Final = "scripts/quality/a2_check.py"
FIDELITY_TESTS_REL: Final = "tests/test_port_fidelity.py"
RUFF_IGNORE_FLAG: Final = re.compile(r"--(?:extend-)?ignore|--per-file-ignores")
NOQA_F821_LINE: Final = re.compile(r"#\s*noqa\b[^#\n]*F821")


def _names_f821(names: Iterable[str]) -> bool:
    """Whether a ruff ignore list reaches F821, including the F82 checks that hide it."""
    return any(name == "F821" or name.startswith("F82") for name in names)


def measure_ac17_04(ctx: Context) -> Facts:
    """Read the three routes by which ``F821`` could be back on the ignored side."""
    ruff = tomllib.loads(ctx.read("pyproject.toml"))["tool"]["ruff"]
    lint = ruff.get("lint", {})
    select = [str(name) for name in lint.get("select", [])]
    ignore = [str(name) for name in lint.get("ignore", [])]
    per_file = lint.get("per-file-ignores") or ruff.get("per-file-ignores") or {}
    per_file_hits = sorted(
        str(glob)
        for glob, names in per_file.items()
        if _names_f821(names if isinstance(names, list) else [names])
    )
    cli_lines = [
        line.strip()
        for line in (ctx.read(MAKEFILE_REL) + "\n" + ctx.read(A2_CHECK_REL)).splitlines()
        if RUFF_IGNORE_FLAG.search(line) and "F821" in line
    ]
    tracked_py = [rel for rel in ctx.tracked() if rel.endswith(".py")]
    noqa_sites: list[str] = []
    unreadable = 0
    for rel in tracked_py:
        try:
            body = (ctx.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            unreadable += 1
            continue
        for lineno, line in enumerate(body.splitlines(), 1):
            if NOQA_F821_LINE.search(line):
                noqa_sites.append(f"{rel}:{lineno}")
    return {
        "select": ", ".join(select) or "-",
        "select_covers_f": flag("F" in select),
        "ignore": ", ".join(ignore) or "-",
        "ignore_has_f821": flag(_names_f821(ignore)),
        "per_file_globs": count(len(per_file)),
        "per_file_f821": count(len(per_file_hits)),
        "per_file_f821_sample": ", ".join(per_file_hits[:3]) or "-",
        "cli_f821_suppression": count(len(cli_lines)),
        "cli_sample": " | ".join(cli_lines[:2]) or "-",
        "tracked_py": count(len(tracked_py)),
        "unreadable_py": count(unreadable),
        "noqa_f821_sites": count(len(noqa_sites)),
        "noqa_f821_sample": ", ".join(noqa_sites[:3]) or "-",
        "doc_names_f821": flag("F821" in ctx.item("AC-17|04").text),
    }


def judge_ac17_04(facts: Facts) -> Verdict:
    """``AC-17|04``: F821 stays named, on none of the three suppression routes."""
    ok = (
        facts["select_covers_f"] == "yes"
        and facts["ignore_has_f821"] == "no"
        and facts["per_file_f821"] == "0"
        and facts["cli_f821_suppression"] == "0"
        and facts["noqa_f821_sites"] == "0"
        and facts["unreadable_py"] == "0"
    )
    readings = (
        f"ruff lint.select = {facts['select']}（覆盖 F 族 = {facts['select_covers_f']}），"
        f"lint.ignore = {facts['ignore']}（含 F821 = {facts['ignore_has_f821']}）；"
        f"{facts['per_file_globs']} 条 per-file-ignores 里点名 F821 的 = {facts['per_file_f821']}"
        + (f"（{facts['per_file_f821_sample']}）" if facts["per_file_f821_sample"] != "-" else ""),
        f"命令行侧（{MAKEFILE_REL} + {A2_CHECK_REL}）用 --ignore/--per-file-ignores 重新塞进 "
        f"F821 的行 = {facts['cli_f821_suppression']}"
        + (f"（{facts['cli_sample']}）" if facts["cli_sample"] != "-" else ""),
        f"逐行侧 {facts['tracked_py']} 个跟踪 .py 里行内忽略点名 F821 的 = "
        f"{facts['noqa_f821_sites']}"
        + (f"（{facts['noqa_f821_sample']}）" if facts["noqa_f821_sample"] != "-" else "")
        + f"；读不动的文件 = {facts['unreadable_py']}（不为 0 时这一格是无读数而不是零违例）；"
        f"判据原文点名 F821 = {facts['doc_names_f821']}",
    )
    reason = "" if ok else "F821 从三条路里某一条又回到了忽略面（配置值、命令行、逐行 noqa）"
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_ac17_09(ctx: Context) -> Facts:
    """Read whether the five frontend members are gate members that run their own tool."""
    text = ctx.read(MAKEFILE_REL)
    recipes = make_recipes(text)
    members = gate_members(text)
    in_gate = [name for name in FRONTEND_GATE_TARGETS if name in members]
    missing = [name for name in FRONTEND_GATE_TARGETS if name not in members]
    empty = [name for name in FRONTEND_GATE_TARGETS if name in members and not recipes.get(name)]
    recipe_text = {name: " ".join(recipes.get(name, [])) for name in FRONTEND_GATE_TARGETS}
    tools = {
        "eslint": flag("eslint" in recipe_text["frontend-lint"]),
        "vue_tsc": flag("vue-tsc" in recipe_text["frontend-typecheck"]),
        "vitest": flag("vitest" in recipe_text["frontend-test"]),
        "playwright": flag("playwright" in recipe_text["frontend-e2e"]),
        "collector": flag("frontend_test_collection" in recipe_text["frontend-collection"]),
    }
    doc = ctx.item("AC-17|09").text
    named = [action for action in ("lint", "typecheck", "test") if action in doc]
    lint_recipe = recipe_text["frontend-lint"]
    return {
        "members_total": count(len(members)),
        "in_gate": count(len(in_gate)),
        "in_gate_list": ", ".join(in_gate),
        "missing": ", ".join(missing) or "-",
        "empty_recipe": count(len(empty)),
        "empty_recipe_sample": ", ".join(empty) or "-",
        "tools": ", ".join(f"{name}={value}" for name, value in tools.items()),
        "tools_all": flag(all(value == "yes" for value in tools.values())),
        "doc_named": ", ".join(named),
        "doc_named_count": count(len(named)),
        "check_only": flag("npx eslint ." in lint_recipe and " --fix" not in lint_recipe),
        **{f"tool_{name}": value for name, value in tools.items()},
    }


def judge_ac17_09(facts: Facts) -> Verdict:
    """``AC-17|09``: the frontend is in the gate, and each member really invokes its tool."""
    ok = (
        facts["in_gate"] == str(len(FRONTEND_GATE_TARGETS))
        and facts["missing"] == "-"
        and facts["empty_recipe"] == "0"
        and facts["tools_all"] == "yes"
        and facts["doc_named_count"] == "3"
        and facts["check_only"] == "yes"
    )
    readings = (
        f"门禁 {facts['members_total']} 个成员里前端五项在位的 = {facts['in_gate']}"
        f"（{facts['in_gate_list']}），缺席 = {facts['missing']}",
        f"配方为空的成员 = {facts['empty_recipe']}"
        + (f"（{facts['empty_recipe_sample']}）" if facts["empty_recipe_sample"] != "-" else "")
        + f"；工具面 {facts['tools']}",
        f"判据原文点名的三个动作 = {facts['doc_named']}；lint 只检查不改写（无 --fix）= "
        f"{facts['check_only']}——门禁里跑格式化会把「检查」变成「改源码」",
    )
    reason = "" if ok else "前端五项没有全部进 gate，或某项配方不再调用它点名的工具"
    return Verdict(PROVEN if ok else GAP, readings, reason)


def measure_s4_03(ctx: Context) -> Facts:
    """Read the tolerance constant, the gate that applies it, and the tests that keep it honest."""
    src = ctx.read(COMPARE_SCRIPT_REL)
    tests = ctx.read(FIDELITY_TESTS_REL)
    literal = re.search(r"^RTOL\s*=\s*([0-9eE.-]+)", src, re.MULTILINE)
    applied = len(re.findall(r"rtol\s*=\s*RTOL", src))
    line = next(
        (index for index, row in enumerate(src.splitlines(), 1) if row.startswith("RTOL = ")), 0
    )
    case_names = re.findall(
        r"^def (test_\w*(?:toler|object_column|value_change|non_numeric|drift_is_reported)"
        r"\w*)\(",
        tests,
        re.MULTILINE,
    )
    return {
        "rtol_literal": literal.group(1) if literal else "(absent)",
        "rtol_at_line": count(line),
        "isclose_sites": count(applied),
        "float_guard": flag("pair is None" in src and "_float_pair(" in src),
        "note_reports_count": flag("处文本不同而浮点在 rtol=" in src),
        "tolerance_tests": count(len(case_names)),
        "tolerance_test_list": ", ".join(case_names[:6]),
        "mutation_control": flag("def test_value_change_in_object_column_still_fails" in tests),
        "silent_drift_control": flag("def test_toleranced_drift_is_reported_not_silent" in tests),
    }


def judge_s4_03(facts: Facts) -> Verdict:
    """``§4|03``: the float tolerance is configured, gated, disclosed, and mutation-controlled."""
    ok = (
        facts["rtol_literal"] == "1e-9"
        and positive(facts["isclose_sites"])
        and facts["float_guard"] == "yes"
        and facts["note_reports_count"] == "yes"
        and facts["tolerance_tests"] == "5"
        and facts["mutation_control"] == "yes"
        and facts["silent_drift_control"] == "yes"
    )
    readings = (
        f"{COMPARE_SCRIPT_REL} 的 RTOL = {facts['rtol_literal']}（第 {facts['rtol_at_line']} 行），"
        f"以 rtol=RTOL 参与判读的调用点 {facts['isclose_sites']} 处",
        "只在两侧真是浮点时才用容忍度（``_float_pair`` 返回 None 即按不等处理） = "
        f"{facts['float_guard']}；被放过的差异进 notes 并记条数 = {facts['note_reports_count']}",
        f"阈值用例 {facts['tolerance_tests']} 条（{facts['tolerance_test_list']}）；"
        f"放宽容忍度立刻红的变异控制 = {facts['mutation_control']}，"
        f"「放过的差异必须可见」控制 = {facts['silent_drift_control']}",
    )
    reason = "" if ok else "容忍度不再是 1e-9，或失去浮点闸门/变异控制/用例中的一样"
    return Verdict(PROVEN if ok else GAP, readings, reason)


ZERO_DEP_BASELINE_REL: Final = "docs/quality/zero-dep-baseline.json"
DOC_NAMED_SCAN_ROOTS: Final = (
    "opendata",
    "opendata_http",
    "opendata_fuyao",
    "opendata_providers",
    "opendata_client",
)
SELFTEST_COUNTS: Final = re.compile(r"\((\d+) violations detected, (\d+) compliant samples clean")
# The criterion names three ways a dependency can enter a runtime package, so the samples the
# self-test attacks must cover all three; one kind alone proves only that one branch.
SELFTEST_REQUIRED_KINDS: Final = ("dynamic", "import", "string")


def measure_ac16_05(ctx: Context) -> Facts:
    """Re-read the pinned zero-dependency surface against today's tree and the gate recipe."""
    scanner = script_module(ZERO_DEP_SCANNER)
    raw = json.loads(ctx.read(ZERO_DEP_BASELINE_REL))
    loaded = scanner.load_baseline()
    recorded = {str(root): int(value) for root, value in dict(loaded.files).items()}
    census = {str(root): int(value) for root, value in scanner.file_census(tuple(recorded)).items()}
    problems = list(scanner.surface_problems(loaded, census))
    # Execute the scanner's own self test instead of reading its source: sample text proves nothing
    # while the detector's catch/no-false-positive claim is not being watched.
    selftest_exit, selftest_output = run_argv(
        [sys.executable, str(ctx.root / ZERO_DEP_SCANNER), "--self-test"], cwd=ctx.root
    )
    reported = SELFTEST_COUNTS.search(selftest_output)
    violations = tuple(getattr(scanner, "_VIOLATION_SAMPLES", ()))
    compliant = tuple(getattr(scanner, "_COMPLIANT_SAMPLES", ()))
    kinds = tuple(sorted({str(kind) for kind, _ in violations}))
    argv = [
        line.strip()
        for line in make_recipes(ctx.read(MAKEFILE_REL)).get("zero-dep-check", [])
        if ZERO_DEP_SCANNER in line
    ]
    present = {root: (ctx.root / root).is_dir() for root in DOC_NAMED_SCAN_ROOTS}
    return {
        "scope": ", ".join(sorted(recorded)),
        "scope_entries": count(len(recorded)),
        "recorded_total": count(sum(recorded.values())),
        "census_total": count(sum(census[root] for root in recorded if root in census)),
        "census_missing_roots": count(len([root for root in recorded if root not in census])),
        "per_scope_mismatch": count(
            len([root for root in recorded if recorded[root] != census.get(root)])
        ),
        "surface_problems": count(len(problems)),
        "surface_sample": " | ".join(str(problem)[:60] for problem in problems[:2]) or "-",
        "loader_scope_matches": flag(
            sorted(str(root) for root in raw.get("scope", [])) == sorted(recorded)
        ),
        "loader_files_match": flag(dict(raw.get("files", {})) == recorded),
        "loader_minors_match": flag(
            sorted(str(minor) for minor in raw.get("python_minors", []))
            == sorted(str(minor) for minor in loaded.python_minors)
        ),
        "recorded_findings": count(len(raw.get("findings", []))),
        "version_recorded": str(raw.get("scanner_version")),
        "version_code": str(scanner.SCANNER_VERSION),
        "version_matches": flag(str(raw.get("scanner_version")) == str(scanner.SCANNER_VERSION)),
        "minors": ", ".join(str(minor) for minor in loaded.python_minors),
        "running_minor_allowed": flag(
            f"{sys.version_info.major}.{sys.version_info.minor}" in list(scanner.PYTHON_MINORS)
        ),
        "invocations": count(len(argv)),
        "self_test_first": flag(bool(argv) and "--self-test" in argv[0]),
        "full_run_after": flag(len(argv) > 1 and "--self-test" not in argv[-1]),
        "selftest_exit": count(selftest_exit),
        "selftest_report": selftest_output.strip().splitlines()[0][:110]
        if selftest_output.strip()
        else "-",
        "violation_samples": count(len(violations)),
        "compliant_samples": count(len(compliant)),
        "violation_kinds": ", ".join(kinds),
        "violation_kinds_full": flag(kinds == SELFTEST_REQUIRED_KINDS),
        "reported_counts_match": flag(
            reported is not None
            and int(reported.group(1)) == len(violations)
            and int(reported.group(2)) == len(compliant)
        ),
        "doc_named_present": ", ".join(
            f"{root}={'yes' if value else 'no'}" for root, value in present.items()
        ),
        "doc_named_absent_roots": ", ".join(root for root, value in present.items() if not value)
        or "-",
    }


def judge_ac16_05(facts: Facts) -> Verdict:
    """``AC-16|05``: the pinned scan surface is today's surface, read twice by the gate."""
    ok = (
        positive(facts["scope_entries"])
        and facts["census_missing_roots"] == "0"
        and facts["per_scope_mismatch"] == "0"
        and facts["surface_problems"] == "0"
        and facts["loader_scope_matches"] == "yes"
        and facts["loader_files_match"] == "yes"
        and facts["loader_minors_match"] == "yes"
        and facts["version_matches"] == "yes"
        and facts["recorded_findings"] == "0"
        and facts["invocations"] == "2"
        and facts["self_test_first"] == "yes"
        and facts["full_run_after"] == "yes"
        and facts["selftest_exit"] == "0"
        and positive(facts["violation_samples"])
        and positive(facts["compliant_samples"])
        and facts["violation_kinds_full"] == "yes"
        and facts["reported_counts_match"] == "yes"
    )
    readings = (
        f"入库扫描面 {facts['scope_entries']} 个根（{facts['scope']}），入库普查 "
        f"{facts['recorded_total']} 个文件；今日走查 {facts['census_total']} 个，"
        f"走查里查不到的根 = {facts['census_missing_roots']}，逐根不符 = "
        f"{facts['per_scope_mismatch']}；工具自己的两面判定（缩水/失配/新根不入库）读出 "
        f"{facts['surface_problems']}"
        + (f"（{facts['surface_sample']}）" if facts["surface_sample"] != "-" else ""),
        f"入库 JSON 与载入结果逐项相等：scope = {facts['loader_scope_matches']}、files = "
        f"{facts['loader_files_match']}、python_minors = {facts['loader_minors_match']}"
        f"（解释器次版本入库 {facts['minors']}，当前解释器在允许集内 = "
        f"{facts['running_minor_allowed']}）",
        f"入库 findings = {facts['recorded_findings']}；扫描器版本 入库 "
        f"{facts['version_recorded']} == 代码 {facts['version_code']} = "
        f"{facts['version_matches']}",
        f"门禁配方调用 {facts['invocations']} 次：先 --self-test = {facts['self_test_first']}，"
        f"之后再全量跑一次 = {facts['full_run_after']}",
        f"自测真跑了一次（退出 {facts['selftest_exit']}）：违例样本 "
        f"{facts['violation_samples']} 条，覆盖种类 {facts['violation_kinds']}"
        f"（判据点名的三种齐全 = {facts['violation_kinds_full']}），"
        f"合规样本 {facts['compliant_samples']} 条（防误报的那一面）；"
        f"工具自己报的计数与结构逐字对上 = {facts['reported_counts_match']}。"
        f"它打印：{facts['selftest_report']}",
        f"判据原文点名的目录在树上的存在性：{facts['doc_named_present']}；树里已经没有的："
        f"{facts['doc_named_absent_roots']} —— 文字陈旧，口径以入库 scope 为准，不是漏扫",
    )
    reason = (
        ""
        if ok
        else "扫描面与树不符 / 入库 JSON 与载入结果不同源 / 版本入库与代码不一致 / "
        "自测与全量两次调用缺一次 / 扫描器自测跑红或样本面缺一种入口形状"
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


SCANNER_REL = "scripts/quality/secret_scan_check.py"
CONFIG_REL = ".gitleaks.toml"
ITEM = "AC-16|09"
PAIR_SAMPLES = 12
MAX_FILE_BYTES = 3_000_000
HEXDIGITS = "0123456789abcdef"

#: Tokens that would turn a history scan into a narrower reading of the same tree. Counted out
#: of the pinned ``scan_argv``, not asserted: the criterion says 全历史扫描.
SCOPE_LIMITS = ("--no-git", "--staged", "--uncommitted", "--head", "--log-opts", "--branch")

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LEAK_WORDS = re.compile(r"leaks found: (\d+)")
COMMIT_WORDS = re.compile(r"(\d+) commits scanned")
BYTE_WORDS = re.compile(r"scanned ~(\d+) bytes")


def _plain(text: str) -> str:
    """Scanner logs without colour codes, so one regex reads one number."""
    return ANSI.sub("", text)


def _word(text: str, pattern: re.Pattern) -> str:
    """The tool's own word for a population, or a sentinel that cannot be read as a count."""
    match = pattern.search(text)
    return match.group(1) if match else "(unread)"


def _leak_words(text: str) -> str:
    """Findings as gitleaks states them: ``no leaks found`` is 0, ``leaks found: N`` is N."""
    if "no leaks found" in text:
        return "0"
    return _word(text, LEAK_WORDS)


def _report_items(path: Path) -> str:
    """Length of the JSON report the tool wrote, or a sentinel when there is nothing to count."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "(unread)"
    return count(len(raw)) if isinstance(raw, list) else "(unread)"


def _report_names(path: Path) -> list[str]:
    """Basenames the report attributes its findings to, for the control's per-fixture counts."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [Path(str(entry.get("File", ""))).name for entry in raw if isinstance(entry, dict)]


def _global_allowlist(config: dict) -> list[dict]:
    """The ``[[allowlists]]`` blocks: exemptions that apply to every rule in the tree."""
    return [block for block in config.get("allowlists", []) if isinstance(block, dict)]


def _rule_allowlist(config: dict) -> list[dict]:
    """The ``[rules.allowlist]`` blocks, each scoped to one rule id."""
    return [
        rule["allowlist"]
        for rule in config.get("rules", [])
        if isinstance(rule.get("allowlist"), dict)
    ]


def _split_pair(line: str) -> tuple[str | None, str]:
    """The quoted ``key": "value`` pair a line opens with, or ``(None, "")``.

    The key is whatever sits between the quote that the split consumed and the quote before it, so a
    line like ``      "alembic/env.py": "95ea…",`` yields ``("alembic/env.py", "95ea…")``.
    """
    if '": "' not in line:
        return None, ""
    left, right = line.split('": "', 1)
    parts = left.split('"')
    if len(parts) < 2:
        return None, ""
    key, value = parts[-1], right.split('"')[0]
    return (key, value) if key else (None, "")


def _parse_pair(ctx: Context, line: str) -> tuple[str, str] | None:
    """Read one ``"<path>": "<sha256>"`` pair out of a line WITHOUT the config's regex.

    Independent, reality-based definition: the key must name a file that exists in this tree and
    the value must be that file's own sha256, recomputed here. A hand-written shape, a stale
    reference or a credential sitting next to such a pair all read as ``None``, which is what
    makes the exempted population something the allowlist can be judged against rather than
    agreed with.
    """
    key, value = _split_pair(line)
    if key is None or "/" not in key or "." not in key.rsplit("/", 1)[-1]:
        return None
    if len(value) != 64 or any(ch not in HEXDIGITS for ch in value):
        return None
    target = ctx.root / key
    if not target.is_file():
        return None
    try:
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return None
    return (key, value) if digest == value else None


def _shape_variants(key: str, value: str) -> list[str]:
    """The same line with the pair shape broken ways; none of them is a path->digest pair.

    Each arm breaks exactly one condition and keeps the rest: 63 hex digits, uppercase hex, no
    directory separator, and a key whose final component no longer ends in ``.<extension>`` (the
    trailing ``-`` is what an ``ext`` shaped key does not have). Deliberately *shape* arms, not
    digest-content arms: ``.gitleaks.toml`` states as a measured boundary that any 64-lowercase-hex
    value under a quoted ``path.ext`` key is exempt, so a flipped hex character stays exempt --
    judging that would be judging the documented design, not drift.
    """
    directory, sep, leaf = key.rpartition("/")
    stem = leaf.rsplit(".", 1)[0] if "." in leaf else leaf
    return [
        f'"{key}": "{value[:-1]}"',  # 63 hex digits, not 64
        f'"{key}": "{value.upper()}"',  # uppercase hex
        f'"{leaf}": "{value}"',  # no directory separator
        f'"{directory}{sep}{stem}-": "{value}"',  # no trailing .extension
    ]


class _PairCensus(TypedDict):
    """The one-pass census: counts of the exempted population plus the verified pair sample."""

    exempted: int
    files_read: int
    files_skipped: int
    stale: int
    changed: int
    pairs: list[tuple[str, str]]


def _pair_census(ctx: Context, patterns: list[str], limit: int) -> _PairCensus:
    """One pass over the tracked files: the exempted population, and the verified pair sample.

    ``exempted`` is counted with the shipped config's own global regexes, so the number is the
    device's; the ``pairs`` sample is collected by :func:`_parse_pair`, which never reads the
    config. ``stale``/``changed`` split the exempted lines by whether the path they name still
    exists with the recorded digest -- archived manifests legitimately point at files that have
    since moved.
    """
    regs = [re.compile(pattern) for pattern in patterns]
    exempted = files_read = files_skipped = stale = changed = 0
    pairs: list[tuple[str, str]] = []
    for rel in ctx.tracked():
        path = ctx.root / rel
        if not path.is_file():
            files_skipped += 1
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            files_skipped += 1
            continue
        if len(text) > MAX_FILE_BYTES:
            files_skipped += 1
            continue
        files_read += 1
        for line in text.splitlines():
            pair = None
            if '": "' in line:
                pair = _parse_pair(ctx, line)
            if regs and any(reg.search(line) for reg in regs):
                exempted += 1
                if pair is None:
                    key, value = _split_pair(line)
                    if (
                        key is None
                        or len(value) != 64
                        or any(ch not in HEXDIGITS for ch in value)
                        or not (ctx.root / key).is_file()
                    ):
                        stale += 1
                    else:
                        changed += 1
            if pair is not None and len(pairs) < limit and pair not in pairs:
                pairs.append(pair)
    return {
        "exempted": exempted,
        "files_read": files_read,
        "files_skipped": files_skipped,
        "stale": stale,
        "changed": changed,
        "pairs": pairs,
    }


def _control_scan(mod: ModuleType, root: Path, tmp: Path) -> Facts:
    """Run the pinned device over a throwaway directory that holds a fabricated credential.

    Two arms, one config each: the shipped ``.gitleaks.toml`` and the same file with everything from
    its first ``[[allowlists]]`` block onward removed. The planted token must be reported under both
    (the instrument is live and the exemption cannot swallow a credential), while the genuine
    path->digest pair line must be reported only under the stripped one (the exemption still
    bites on exactly the shape it claims). Everything lives under ``tempfile.TemporaryDirectory``.
    """
    fixtures = tmp / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    token = "ghp_" + hashlib.sha256(b"c80-fabricated-control-token").hexdigest()[:36]
    pair_key = "scripts/quality/secret_scan_check.py"
    pair_digest = hashlib.sha256((root / pair_key).read_bytes()).hexdigest()
    (fixtures / "leak_fixture.txt").write_text(f"Authorization: Bearer {token}\n", encoding="utf-8")
    (fixtures / "pair_fixture.json").write_text(
        json.dumps({"sha256": {pair_key: pair_digest}}, indent=2) + "\n", encoding="utf-8"
    )
    shipped = root / CONFIG_REL
    text = shipped.read_text(encoding="utf-8")
    cut = text.find("[[allowlists]]")
    stripped = tmp / "no-allowlists.toml"
    stripped.write_text(text if cut < 0 else text[:cut], encoding="utf-8")
    out: dict[str, str] = {"allowlist_blocks": flag(cut >= 0)}
    for arm, cfg in (("shipped", shipped), ("stripped", stripped)):
        report = tmp / f"report-{arm}.json"
        argv = [str(item) for item in mod.load_manifest().scan_argv]
        argv += [
            "--no-git",
            "--source",
            str(fixtures),
            "--config",
            str(cfg),
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ]
        exit_code, _ = run_argv(argv, cwd=root)
        names = _report_names(report)
        out[f"control_{arm}_exit"] = count(exit_code)
        out[f"control_{arm}_items"] = _report_items(report)
        out[f"control_{arm}_token"] = count(sum(1 for name in names if name == "leak_fixture.txt"))
        out[f"control_{arm}_pair"] = count(sum(1 for name in names if name == "pair_fixture.json"))
    return out


def measure_ac16_09(ctx: Context) -> Facts:
    """Read the device, run the pinned full-history scan, run the control; decide nothing."""
    mod = script_module(SCANNER_REL)
    manifest = mod.load_manifest()
    found = mod.local_tool_version(manifest)
    problems = mod.check_ci_wiring(manifest)
    argv = [str(item) for item in manifest.scan_argv]
    config = tomllib.loads(ctx.read(CONFIG_REL))
    globals_ = _global_allowlist(config)
    rules = _rule_allowlist(config)
    global_regexes = [str(rx) for block in globals_ for rx in block.get("regexes", [])]

    census = _pair_census(ctx, global_regexes, PAIR_SAMPLES)
    pairs = census["pairs"]
    pair_lines = [f'"{key}": "{value}"' for key, value in pairs]
    variants = [line for key, value in pairs for line in _shape_variants(key, value)]
    matched = sum(
        1 for line in pair_lines if all(re.compile(rx).search(line) for rx in global_regexes)
    )
    unjudged = sum(
        1
        for block in globals_
        if not list(block.get("regexes", []))
        or not all(
            re.compile(str(rx)).search(line)
            for rx in block.get("regexes", [])
            for line in pair_lines
        )
    )
    near_miss = sum(1 for rx in global_regexes for line in variants if re.compile(rx).search(line))

    with tempfile.TemporaryDirectory(prefix="ac16-09-control-") as tmpname:
        control = _control_scan(mod, ctx.root, Path(tmpname))
    with tempfile.TemporaryDirectory(prefix="ac16-09-scan-") as tmpname:
        report = Path(tmpname) / "report.json"
        scan_argv = argv + ["--report-format", "json", "--report-path", str(report)]
        scan_exit, scan_out = run_argv(scan_argv, cwd=ctx.root)
        logs = _plain(scan_out)
        findings = _report_items(report)
    code_head, head_out = run_argv(["git", "rev-list", "--count", "HEAD"], cwd=ctx.root)
    code_all, all_out = run_argv(["git", "rev-list", "--count", "--all"], cwd=ctx.root)
    return {
        "scanner_pin": manifest.gitleaks_version,
        "scanner_found": found or "(absent)",
        "scanner_matches_pin": flag(found is not None and found == manifest.gitleaks_version),
        "device_ok": flag(not problems),
        "ci_wiring_problems": count(len(problems)),
        "ci_wiring_detail": "; ".join(problems)[:160] or "-",
        "argv_scope_limits": count(sum(1 for item in argv if item in SCOPE_LIMITS)),
        "argv_tokens": " ".join(argv)[:120],
        "reads_scanner_root": flag(ctx.root == mod.REPO_ROOT),
        "commits_scanned": _word(logs, COMMIT_WORDS),
        "bytes_scanned": _word(logs, BYTE_WORDS),
        "git_commits_head": head_out.strip().splitlines()[-1].strip()
        if code_head == 0
        else "(unread)",
        "git_commits_all": all_out.strip().splitlines()[-1].strip()
        if code_all == 0
        else "(unread)",
        "scan_exit": count(scan_exit),
        "report_items": findings,
        "stderr_leaks": _leak_words(logs),
        "allowlist_global": count(len(globals_)),
        "allowlist_global_regexes": count(len(global_regexes)),
        "allowlist_global_path_scoped": count(sum(1 for block in globals_ if block.get("paths"))),
        "allowlist_rules": count(len(rules)),
        "allowlist_rule_regexes": count(sum(len(block.get("regexes", [])) for block in rules)),
        "pair_samples": count(len(pairs)),
        "pair_variants": count(len(variants)),
        "pair_matched": count(matched),
        "entries_unjudged": count(unjudged),
        "near_miss_matched": count(near_miss),
        "tree_pair_lines": count(census["exempted"]),
        "tree_files_read": count(census["files_read"]),
        "tree_files_skipped": count(census["files_skipped"]),
        "tree_pair_stale": count(census["stale"]),
        "tree_pair_changed": count(census["changed"]),
        "doc_names_history": flag("全历史" in ctx.item(ITEM).text),
        **control,
    }


def judge_ac16_09(facts: Facts) -> Verdict:
    """AC-16|09: the pinned device walked the whole history git reports right now and came back.

    with nothing outside the allowlist, the allowlist still only speaks the tree's real
    path->digest shape, and a planted credential is still reported.
    """
    commits, head = number(facts["commits_scanned"]), number(facts["git_commits_head"])
    report, words, exit_code = (
        number(facts["report_items"]),
        number(facts["stderr_leaks"]),
        number(facts["scan_exit"]),
    )
    shipped_items = number(facts["control_shipped_items"])
    stripped_items = number(facts["control_stripped_items"])
    shipped_findings = number(facts["control_shipped_token"]) + number(
        facts["control_shipped_pair"]
    )
    stripped_findings = number(facts["control_stripped_token"]) + number(
        facts["control_stripped_pair"]
    )
    detail = facts["ci_wiring_detail"]
    ok = (
        facts["scanner_matches_pin"] == "yes"
        and facts["device_ok"] == "yes"
        and number(facts["ci_wiring_problems"]) == 0
        and number(facts["argv_scope_limits"]) == 0
        and commits > 0
        and commits == head
        and number(facts["bytes_scanned"]) > 0
        and report == 0
        and words == report
        and (exit_code == 0) == (report == 0)
        and number(facts["allowlist_global_path_scoped"]) == 0
        and positive(facts["allowlist_global_regexes"])
        and positive(facts["pair_samples"])
        and number(facts["pair_matched"]) == number(facts["pair_samples"])
        and number(facts["entries_unjudged"]) == 0
        and number(facts["near_miss_matched"]) == 0
        and positive(facts["tree_pair_lines"])
        and facts["allowlist_blocks"] == "yes"
        and number(facts["control_shipped_token"]) >= 1
        and number(facts["control_stripped_token"]) >= 1
        and number(facts["control_shipped_pair"]) == 0
        and number(facts["control_stripped_pair"]) >= 1
        and shipped_items == shipped_findings
        and stripped_items == stripped_findings
        and stripped_items > shipped_items
        and facts["doc_names_history"] == "yes"
    )
    readings = (
        f"设备：{SCANNER_REL} 钉住 gitleaks {facts['scanner_pin']}，"
        f"PATH 上报 {facts['scanner_found']}"
        f"（一致 = {facts['scanner_matches_pin']}）；CI 接线问题 {facts['ci_wiring_problems']} 条"
        + ("-" if detail == "-" else f"（{detail}）")
        + f"；命令 = manifest 的 scan_argv = {facts['argv_tokens']}，把扫描缩回工作树/单提交的开关 "
        f"{facts['argv_scope_limits']} 处；scanner 模块自带的 REPO_ROOT 就是本次读的树 = "
        f"{facts['reads_scanner_root']}（它的 main() 只读那个常量，"
        f"探针因此按 manifest 的 argv 在 ctx.root 重跑）",
        f"全历史群体取自 history 而非工作树：工具自报 {facts['commits_scanned']} commits scanned、"
        f"{facts['bytes_scanned']} 字节；同一棵树此刻 git rev-list "
        f"--count HEAD = {facts['git_commits_head']}"
        f"（--all = {facts['git_commits_all']}）——两个数不等就是「悄悄缩小了扫描」",
        f"未豁免的凭证：报告 {facts['report_items']} 条、"
        f"日志口径 {facts['stderr_leaks']} 条、退出码 "
        f"{facts['scan_exit']}；三个口径互为印证（退出码为 0 当且仅当报告为空、且与日志行数相同），"
        f"任一口径读不出来（(unread)）都判 gap",
        f"豁免面：全局 [[allowlists]] {facts['allowlist_global']} 块 / "
        f"{facts['allowlist_global_regexes']} 条正则"
        f"（其中按位置豁免 {facts['allowlist_global_path_scoped']} 条），"
        f"规则级 allowlist {facts['allowlist_rules']} 块 / "
        f"{facts['allowlist_rule_regexes']} 条；"
        f"树内被全局豁免盖住的行 {facts['tree_pair_lines']} 行"
        f"（扫过 {facts['tree_files_read']} 个跟踪文件、跳过 {facts['tree_files_skipped']}；"
        f"其中引用路径已不在树里或形状不对 "
        f"{facts['tree_pair_stale']} 行、"
        f"路径仍在但档案摘要与当前文件已不符 {facts['tree_pair_changed']} 行）",
        f"形状判读（不用配置的正则来找样本）：从树里现取现算 sha256 校验过的真 path->digest 成对行 "
        f"{facts['pair_samples']} 条，被每一条全局豁免全部盖住的 "
        f"{facts['pair_matched']} 条，判读不通过的豁免条目 "
        f"{facts['entries_unjudged']} 条；"
        f"把这 {facts['pair_samples']} 条各拆成 4 种非 path->digest 形状"
        f"（63 位摘要 / 大写十六进制 / 无目录分隔 / 无扩展名，"
        f"共 {facts['pair_variants']} 条变体）后仍被盖住的 "
        f"{facts['near_miss_matched']} 条",
        f"活体对照（tempdir 目录扫描，用完即删）：植入的合成口令 "
        f"shipped 配置报 {facts['control_shipped_token']} 条、"
        f"剥掉 [[allowlists]] 报 {facts['control_stripped_token']} 条；"
        f"真实 path->digest 成对行 shipped 报 "
        f"{facts['control_shipped_pair']} 条、剥离后报 {facts['control_stripped_pair']} 条"
        f"（总数 {facts['control_shipped_items']}/{facts['control_stripped_items']}，退出码 "
        f"{facts['control_shipped_exit']}/{facts['control_stripped_exit']}）——"
        f"headline 的 0 是一支还咬得动、"
        f"且豁免面边界量得出来的探测器给的",
    )
    reason = ""
    if not ok:
        if commits != head or commits <= 0:
            reason = (
                f"扫描群体与 git 此刻的 HEAD 不再相同（工具自报 {facts['commits_scanned']} 个提交，"
                f"rev-list 说 {facts['git_commits_head']}）"
                f"，或扫描字节数 {facts['bytes_scanned']} 读不出来"
            )
        elif report != 0 or words != report or (exit_code == 0) != (report == 0):
            reason = (
                f"全历史扫描报出凭证，或报告/日志/退出码三个口径不再互相印证"
                f"（{facts['report_items']}/{facts['stderr_leaks']}/{facts['scan_exit']}）"
            )
        elif facts["scanner_matches_pin"] != "yes" or facts["device_ok"] != "yes":
            reason = (
                f"扫描设备不再是钉住的那一支：PATH 上报 {facts['scanner_found']}，接线问题 {detail}"
            )
        elif number(facts["argv_scope_limits"]) != 0:
            reason = "scan_argv 里出现了把工作树/单提交当成全历史的开关"
        elif number(facts["control_shipped_token"]) < 1 or facts["allowlist_blocks"] != "yes":
            reason = "植入的合成凭证没有被报出来：这个 0 是一支哑仪器给的"
        elif (
            number(facts["control_stripped_pair"]) < 1 or number(facts["control_shipped_pair"]) > 0
        ):
            reason = "对照量不出豁免面的边界（成对行在带豁免与不带豁免两种配置下读数相同）"
        elif (
            shipped_items != shipped_findings
            or stripped_items != stripped_findings
            or stripped_items <= shipped_items
        ):
            reason = "对照的两臂计数不再等于各 fixture 之和，或剥掉豁免后并没有报得更多"
        elif (
            number(facts["entries_unjudged"])
            or number(facts["near_miss_matched"])
            or number(facts["allowlist_global_path_scoped"])
            or number(facts["pair_matched"]) != number(facts["pair_samples"])
            or not positive(facts["allowlist_global_regexes"])
            or not positive(facts["pair_samples"])
        ):
            reason = (
                "全局豁免条目与树里真实的 path->digest 形状不再一一对得上"
                "（或它开始按位置吞发现、或不再咬住成对行）"
            )
        elif not positive(facts["tree_pair_lines"]):
            reason = "被豁免的群体在树里读不到了（普查 0 行，豁免面在掩护什么无从判读）"
        else:
            reason = "扫描设备、历史群体、发现口径、豁免形状或活体对照之中有一项读不出来"
    return Verdict("proven" if ok else "gap", readings, reason)


AC16_09 = Probe(
    item=ITEM,
    expects="凭证不在版本库中（含全历史扫描）",
    summary=(
        "钉住的全历史扫描走完 git 此刻报出的全部提交、"
        "报告/日志/退出码三个口径同为 0 条凭证；豁免面只覆盖树里"
        "校验过的 path->digest 成对行，且植入合成凭证的对照照样报得出"
    ),
    measure=measure_ac16_09,
    judge=judge_ac16_09,
    repair={
        "scanner_found": "*scanner_pin",
        "scanner_matches_pin": "yes",
        "device_ok": "yes",
        "ci_wiring_problems": "0",
        "ci_wiring_detail": "-",
        "argv_scope_limits": "0",
        "commits_scanned": "*git_commits_head",
        "bytes_scanned": "*bytes_scanned",
        "scan_exit": "0",
        "report_items": "0",
        "stderr_leaks": "0",
        "allowlist_global_path_scoped": "0",
        "pair_matched": "*pair_samples",
        "entries_unjudged": "0",
        "near_miss_matched": "0",
        "tree_pair_lines": "*tree_pair_lines",
        "allowlist_blocks": "yes",
        "control_shipped_token": "1",  # nosec B105  # face name for the planted token's hit count, not a credential
        "control_shipped_pair": "0",
        "control_shipped_items": "1",
        "control_stripped_token": "1",  # nosec B105  # face name for the planted token's hit count, not a credential
        "control_stripped_pair": "1",
        "control_stripped_items": "2",
        "doc_names_history": "yes",
    },
    breaks=(
        Break("扫描只走了 12 个提交（范围被悄悄缩小）", (("commits_scanned", "12"),), "gap"),
        Break("工具不再自报提交数（版式换了）", (("commits_scanned", "(unread)"),), "gap"),
        Break("git 的 HEAD 群体变成 0（浅克隆/空仓）", (("git_commits_head", "0"),), "gap"),
        Break("扫描字节数为 0（仪器空转）", (("bytes_scanned", "0"),), "gap"),
        Break(
            "scan_argv 加了 --no-git（历史扫描退化成工作树）", (("argv_scope_limits", "1"),), "gap"
        ),
        Break(
            "PATH 上的扫描器漂到 8.21.2",
            (("scanner_found", "8.21.2"), ("scanner_matches_pin", "no")),
            "gap",
        ),
        Break(
            "CI 接线问题又回来一条",
            (
                ("ci_wiring_problems", "1"),
                ("device_ok", "no"),
                ("ci_wiring_detail", "fetch-depth 没了"),
            ),
            "gap",
        ),
        Break(
            "全历史里报出一条凭证",
            (("report_items", "1"), ("stderr_leaks", "1"), ("scan_exit", "1")),
            "gap",
        ),
        Break("报告与日志口径分叉（findings 被漏读）", (("stderr_leaks", "3"),), "gap"),
        Break(
            "红扫描被 echo 成绿（退出码与 findings 不符）",
            (("scan_exit", "0"), ("report_items", "2"), ("stderr_leaks", "2")),
            "gap",
        ),
        Break("退出码不再读（127：gitleaks 没了）", (("scan_exit", "127"),), "gap"),
        Break("全局豁免开始按路径位置吞发现", (("allowlist_global_path_scoped", "1"),), "gap"),
        Break("全局豁免条目与真实成对行对不上（无人判读）", (("entries_unjudged", "1"),), "gap"),
        Break("豁免宽到非 path->digest 形状也被盖住", (("near_miss_matched", "1"),), "gap"),
        Break(
            "成对行不再全被豁免盖住（条目漂走）",
            (("pair_samples", "20"), ("pair_matched", "19")),
            "gap",
        ),
        Break(
            "全局豁免被删空", (("allowlist_global_regexes", "0"), ("allowlist_global", "0")), "gap"
        ),
        Break("树里的被豁免群体普查为 0", (("tree_pair_lines", "0"),), "gap"),
        Break(
            "植入的合成凭证没被报出（仪器是哑的）",
            (("control_shipped_token", "0"), ("control_shipped_items", "0")),
            "gap",
        ),
        Break(
            "剥掉豁免也没有多报（豁免不再咬住成对行）",
            (("control_stripped_pair", "0"), ("control_stripped_items", "1")),
            "gap",
        ),
        Break(
            "成对行带着豁免也被报出（豁免面失效）",
            (("control_shipped_pair", "1"), ("control_shipped_items", "2")),
            "gap",
        ),
        Break("对照两臂计数与 fixture 之和不再相等", (("control_stripped_items", "5"),), "gap"),
        Break("配置里已经没有 [[allowlists]] 块", (("allowlist_blocks", "no"),), "gap"),
        Break("判据原文不再点名全历史", (("doc_names_history", "no"),), "gap"),
    ),
)


# --------------------------------------------------------------------------- #
# AC-17|02 -- A2's four planes really execute, and every bandit exemption is
# line-level, rule-named, reasoned, and shown to suppress a real finding.
# --------------------------------------------------------------------------- #

A2_REL: Final = "scripts/quality/a2_check.py"
BANDIT_REL: Final = "bandit.yaml"
NOSEC_PAT = re.compile(r"#\s*nosec\b(.*)$")


def _census_roots(c: Context) -> list[str]:
    """The roots bandit is pointed at, read from the Makefile that names them."""
    line = next(
        (row for row in c.read(MAKEFILE_REL).splitlines() if row.startswith("PY_BANDIT")),
        "",
    )
    return line.split(":=", 1)[1].split() if ":=" in line else []


def _yaml_block_list(text: str, key: str) -> list[str]:
    """Read one top-level YAML list by its item lines -- bandit.yaml is YAML, not TOML."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if re.match(rf"^{key}\s*:", line):
            inside = True
            continue
        if inside:
            m = re.match(r"^\s*-\s*(\S.*?)\s*$", line)
            if m:
                out.append(m.group(1).split("#")[0].strip().strip("\"'"))
            elif line.strip() and not line.startswith((" ", "\t", "#")):
                inside = False
    return out


def _exclude_dirs(c: Context) -> list[str]:
    """_exclude_dirs: see the AC-17|02 measurement it serves."""
    text = (c.root / BANDIT_REL).read_text(encoding="utf-8")
    return _yaml_block_list(text, "exclude_dirs")


def _excluded(rel: str, excludes: list[str]) -> bool:
    """_excluded: see the AC-17|02 measurement it serves."""
    parts = rel.split("/")
    for item in excludes:
        token = item.strip("/")
        if not token:
            continue
        if token in parts or token in rel:
            return True
    return False


def _bandit_census(
    c: Context, extra: tuple[str, ...]
) -> tuple[str, str, str, collections.Counter, str]:
    """Run the census into a report file.

    ``run_argv`` merges stderr, and bandit logs a warning on stderr for every nosec comment, so
    stdout is not a parsable document.
    """
    argv = [
        sys.executable,
        "-m",
        "bandit",
        "-c",
        BANDIT_REL,
        "-f",
        "json",
        "-q",
        *extra,
    ]
    with tempfile.TemporaryDirectory(prefix="c81-bandit-") as tmp:
        report = Path(tmp) / "report.json"
        rc, _out = run_argv([*argv, "-o", str(report), "-r", *_census_roots(c)], cwd=c.root)
        try:
            doc = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return str(rc), "no", "-", collections.Counter(), "-"
    rows = doc.get("results", [])
    keys: collections.Counter = collections.Counter()
    for row in rows:
        name = re.sub(r"^\./", "", str(row.get("filename", "")))
        keys[(name, str(row.get("test_id", "")))] += 1
    ids = collections.Counter(str(row.get("test_id", "")) for row in rows)
    shape = ", ".join(f"{k}x{v}" for k, v in sorted(ids.items())) or "-"
    totals = doc.get("metrics", {}).get("_totals", {})
    return str(rc), "yes", shape, keys, str(totals.get("skipped_tests", "-"))


def _nosec_rows(c: Context, roots: list[str], excludes: list[str]) -> list[tuple[str, int, str]]:
    """_nosec_rows: see the AC-17|02 measurement it serves."""
    rows: list[tuple[str, int, str]] = []
    for root in roots:
        base = c.root / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = str(path.relative_to(c.root))
            if _excluded(rel, excludes):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                m = NOSEC_PAT.search(line)
                if m:
                    rows.append((rel, i, m.group(1).strip()))
    return rows


SUPPRESSION_MARKERS = re.compile(
    r"(?:#\s*)?(?:noqa|nosec|type\s*:)\s*:?\s*(?:[A-Z]\d+(?:[,\s]*[A-Z]\d+)*)?",
    re.IGNORECASE,
)


def _has_reason(rest: str) -> bool:
    """A reason is prose, not another suppression marker: ``# noqa: S314`` justifies nothing."""
    body = re.sub(r"B\d+", " ", rest)
    body = SUPPRESSION_MARKERS.sub(" ", body)
    body = re.sub(r"^[^0-9A-Za-z一-鿿]+", "", body)
    return len(re.sub(r"[^0-9A-Za-z一-鿿]", "", body)) >= 4


def _reason_predicate_controls() -> int:
    """Give the discriminator itself a two-sided control.

    A bare ``# noqa`` must NOT read as a reason and real prose must; one-sided here means the
    whole 理由 face is unproven.
    """
    accepts = _has_reason("B608  # the table name is a module literal")
    rejects_noqa = not _has_reason("B314  # noqa: S314")
    rejects_id_only = not _has_reason("B105")
    rejects_punctuation = not _has_reason("B105  # -----")
    return sum((accepts, rejects_noqa, rejects_id_only, rejects_punctuation))


def _bandit_body(tree: ast.Module) -> ast.FunctionDef | None:
    """_bandit_body: see the AC-17|02 measurement it serves."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_bandit":
            return node
    return None


def _stream_concat(fn: ast.FunctionDef) -> list[str]:
    """_stream_concat: see the AC-17|02 measurement it serves."""
    hits: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            leaves = [
                ".".join(n for n in (getattr(x, "value", ""), getattr(x, "attr", "")) if n)
                for x in (node.left, node.right)
            ]
            joined = " ".join(str(item) for item in leaves)
            if "stdout" in joined and "stderr" in joined:
                hits.append(f"line {node.lineno}: {' + '.join(leaves)}")
    return hits


def measure_ac17_02(c: Context) -> Facts:
    """measure_ac17_02: see the AC-17|02 measurement it serves."""
    src = c.read(A2_REL)
    tree = ast.parse(src)
    fn = _bandit_body(tree)
    concat = _stream_concat(fn) if fn else ["(no _bandit function)"]
    raw_cfg = tomllib.loads((c.root / "pyproject.toml").read_text(encoding="utf-8"))
    mypy_cfg = raw_cfg["tool"].get("mypy", {})
    strict = {
        "disallow_untyped_defs": bool(mypy_cfg.get("disallow_untyped_defs")),
        "disallow_incomplete_defs": bool(mypy_cfg.get("disallow_incomplete_defs")),
        "check_untyped_defs": bool(mypy_cfg.get("check_untyped_defs")),
        "warn_return_any": bool(mypy_cfg.get("warn_return_any")),
        "strict_equality": bool(mypy_cfg.get("strict_equality")),
        "no_implicit_optional": bool(mypy_cfg.get("no_implicit_optional")),
        "warn_unused_ignores": bool(mypy_cfg.get("warn_unused_ignores")),
    }
    skips = _yaml_block_list(c.read(BANDIT_REL), "skips")
    roots = _census_roots(c)
    excludes = _exclude_dirs(c)
    rows = _nosec_rows(c, roots, excludes)
    declared: collections.Counter = collections.Counter()
    for rel, _i, rest in rows:
        for tid in set(re.findall(r"B\d+", rest)):
            declared[(rel, tid)] += 1
    no_id = [f"{rel}:{i}" for rel, i, rest in rows if not re.search(r"B\d{2,3}", rest)]
    no_reason = [f"{rel}:{i}" for rel, i, rest in rows if not _has_reason(rest)]

    kept_rc, kept_json, kept_ids, kept_keys, kept_suppressed = _bandit_census(c, ())
    raw_rc, raw_json, raw_ids, raw_keys, _raw_suppressed = _bandit_census(c, ("--ignore-nosec",))
    kept_total = sum(kept_keys.values())
    raw_total = sum(raw_keys.values())
    try:
        suppressed_witness = int(kept_suppressed)
    except ValueError:
        suppressed_witness = -1
    decorative = sorted(
        f"{name}:{tid} 声明 {n} 处 / 原始违例 {raw_keys.get((name, tid), 0)}"
        for (name, tid), n in declared.items()
        if n > raw_keys.get((name, tid), 0)
    )
    decorative_sites = sum(
        max(0, n - raw_keys.get((name, tid), 0)) for (name, tid), n in declared.items()
    )
    unexempted = sum(
        max(0, n - declared.get((name, tid), 0)) for (name, tid), n in raw_keys.items()
    )
    unreadable_is_fail = False
    if fn:
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
                elts = node.value.elts
                if len(elts) >= 2 and isinstance(elts[0], ast.Constant) and elts[0].value is False:
                    unreadable_is_fail = True
    return {
        "plane_ruff_check": flag('"ruff", "check"' in src),
        "plane_ruff_format": flag('"ruff", "format", "--check"' in src),
        "plane_mypy": flag('"mypy"' in src),
        "plane_bandit": flag('"bandit"' in src),
        "bandit_config_bound": flag('"-c", BANDIT' in src or '"-c", "bandit.yaml"' in src),
        "bandit_json_forced": flag('"-f", "json"' in src),
        "bandit_unreadable_is_fail": flag(unreadable_is_fail),
        "stream_concat_sites": count(len(concat)),
        "stream_concat_sample": " | ".join(concat[:2]) or "-",
        "mypy_strict_flags": count(sum(strict.values())),
        "mypy_strict_names": ", ".join(k for k, v in strict.items() if v),
        "fileset_from_git": flag("git" in src and ("diff" in src or "ls-files" in src)),
        "bandit_global_skips": count(len(skips)),
        "bandit_skip_list": ", ".join(skips) or "-",
        "census_roots": ", ".join(roots),
        "census_root_missing": count(len([r for r in roots if not (c.root / r).is_dir()])),
        "census_exit": kept_rc,
        "census_json_ok": kept_json,
        "census_findings": count(kept_total),
        "census_finding_ids": kept_ids,
        "raw_exit": raw_rc,
        "raw_json_ok": raw_json,
        "raw_findings": count(raw_total),
        "raw_finding_ids": raw_ids,
        "suppressed_by_nosec": kept_suppressed,
        "suppression_gap": count(raw_total - kept_total),
        "suppression_identity": flag(suppressed_witness == raw_total - kept_total),
        "nosec_sites": count(len(rows)),
        "nosec_without_rule_id": count(len(no_id)),
        "nosec_no_id_sample": ", ".join(no_id[:3]) or "-",
        "nosec_without_reason": count(len(no_reason)),
        "nosec_no_reason_sample": ", ".join(no_reason[:3]) or "-",
        "decorative_exemptions": count(decorative_sites),
        "reason_predicate_controls": count(_reason_predicate_controls()),
        "decorative_sample": " | ".join(decorative[:2]) or "-",
        "unexempted_findings": count(unexempted),
    }


def judge_ac17_02(facts: Facts) -> Verdict:
    """judge_ac17_02: see the AC-17|02 measurement it serves."""
    ok = (
        facts["plane_ruff_check"] == "yes"
        and facts["plane_ruff_format"] == "yes"
        and facts["plane_mypy"] == "yes"
        and facts["plane_bandit"] == "yes"
        and facts["bandit_config_bound"] == "yes"
        and facts["bandit_json_forced"] == "yes"
        and facts["bandit_unreadable_is_fail"] == "yes"
        and facts["stream_concat_sites"] == "0"
        and facts["mypy_strict_flags"] == "7"
        and facts["fileset_from_git"] == "yes"
        and facts["bandit_global_skips"] == "0"
        and facts["census_root_missing"] == "0"
        and facts["census_exit"] == "0"
        and facts["census_json_ok"] == "yes"
        and facts["census_findings"] == "0"
        and facts["raw_exit"] == "1"
        and facts["raw_json_ok"] == "yes"
        and positive(facts["raw_findings"])
        and facts["suppression_identity"] == "yes"
        and facts["nosec_without_rule_id"] == "0"
        and facts["nosec_without_reason"] == "0"
        and facts["decorative_exemptions"] == "0"
        and facts["reason_predicate_controls"] == "4"
    )
    readings = (
        f"四个面真的被调用：ruff check={facts['plane_ruff_check']}、ruff format --check="
        f"{facts['plane_ruff_format']}、mypy={facts['plane_mypy']}、bandit={facts['plane_bandit']}；"
        f"bandit 绑 {BANDIT_REL}={facts['bandit_config_bound']}、"
        f"强制 -f json={facts['bandit_json_forced']}",
        f"`_bandit` 的 AST 里把 stdout 与 stderr 拼起来再 parse 的表达式 = "
        f"{facts['stream_concat_sites']}"
        + (f"（{facts['stream_concat_sample']}）" if facts["stream_concat_sample"] != "-" else "")
        + f"；读不到 JSON 时返回 False 的路径 = {facts['bandit_unreadable_is_fail']}",
        f"mypy 严格旗标 {facts['mypy_strict_flags']} 项生效（{facts['mypy_strict_names']}）；"
        f"A2 文件集来自 git = {facts['fileset_from_git']}",
        f"例外面：{BANDIT_REL} 的全局 skips = {facts['bandit_global_skips']}"
        + (
            f"（{facts['bandit_skip_list']}）—— 全局跳过与「例外行级精确」是相反的形状"
            if facts["bandit_skip_list"] != "-"
            else ""
        )
        + f"；逐行 nosec {facts['nosec_sites']} 处，未点名规则号 {facts['nosec_without_rule_id']}"
        + (f"（{facts['nosec_no_id_sample']}）" if facts["nosec_no_id_sample"] != "-" else "")
        + f"，点名了但没写理由 {facts['nosec_without_reason']} 处"
        + (
            f"（{facts['nosec_no_reason_sample']}）"
            if facts["nosec_no_reason_sample"] != "-"
            else ""
        ),
        f"真跑普查 {facts['census_roots']}：nosec 生效时 rc={facts['census_exit']}、"
        f"JSON 可读={facts['census_json_ok']}、剩余违例 {facts['census_findings']}"
        + (f"（{facts['census_finding_ids']}）" if facts["census_finding_ids"] != "-" else ""),
        f"反向臂 --ignore-nosec：rc={facts['raw_exit']}、原始违例 {facts['raw_findings']} 条"
        f"（{facts['raw_finding_ids']}）—— 这一臂为 0 就说明规则根本没在跑，"
        "上面的 0 是空判而不是干净",
        f"bandit 自报被 nosec 压住 {facts['suppressed_by_nosec']} 条，与两臂差额 "
        f"{facts['suppression_gap']} 相等 = {facts['suppression_identity']}；"
        "例外不生效时这个等式会断，而不是静悄悄变成 0",
        f"例外与违例逐文件对齐：压住的 {facts['unexempted_findings']} 条差额、"
        f"理由判别式自己的双侧对照 {facts['reason_predicate_controls']}/4（散文算、裸 noqa 不算）；"
        f"什么都没压住的装饰性 nosec = {facts['decorative_exemptions']}"
        + (f"（{facts['decorative_sample']}）" if facts["decorative_sample"] != "-" else ""),
    )
    reason = (
        ""
        if ok
        else (
            "A2 四面缺一个 / bandit 未绑配置或未强制 JSON / 反向臂不报违例 / "
            "普查仍有剩余违例或命令本身跑挂 / 例外仍是全局 skips / "
            "例外没有行级规则号或没有理由 / 有 nosec 什么都没压住"
        )
    )
    return Verdict(PROVEN if ok else GAP, readings, reason)


AC17_02 = Probe(
    item="AC-17|02",
    expects="bandit 全量（例外行级精确 + 理由）",
    summary="bandit 全量真跑两臂：剩余违例为 0 而反向臂报违例，例外逐行带规则号与理由",
    measure=measure_ac17_02,
    judge=judge_ac17_02,
    repair={
        "plane_ruff_check": "yes",
        "plane_ruff_format": "yes",
        "plane_mypy": "yes",
        "plane_bandit": "yes",
        "bandit_config_bound": "yes",
        "bandit_json_forced": "yes",
        "bandit_unreadable_is_fail": "yes",
        "stream_concat_sites": "0",
        "mypy_strict_flags": "7",
        "fileset_from_git": "yes",
        "bandit_global_skips": "0",
        "census_root_missing": "0",
        "census_exit": "0",
        "census_json_ok": "yes",
        "census_findings": "0",
        "raw_exit": "1",
        "raw_json_ok": "yes",
        "raw_findings": "160",
        "suppression_identity": "yes",
        "nosec_without_rule_id": "0",
        "nosec_without_reason": "0",
        "decorative_exemptions": "0",
        "reason_predicate_controls": "4",
    },
    breaks=(
        Break("bandit.yaml 保留一条全局 skip", (("bandit_global_skips", "1"),), GAP),
        Break("全量普查今天真的报出违例", (("census_findings", "1"),), GAP),
        Break(
            "普查命令跑挂，输出被当成 0 条", (("census_exit", "2"), ("census_findings", "0")), GAP
        ),
        Break("反向臂空转：--ignore-nosec 也报 0 条", (("raw_findings", "0"),), GAP),
        Break("nosec 一条都没压住（等式断掉）", (("suppression_identity", "no"),), GAP),
        Break("复制来的 nosec 什么都没压住", (("decorative_exemptions", "1"),), GAP),
        Break("一行例外不点名规则号", (("nosec_without_rule_id", "1"),), GAP),
        Break("例外只写了规则号没写理由", (("nosec_without_reason", "1"),), GAP),
        Break(
            "理由判别式掉了一条边（裸 noqa 被当成理由）", (("reason_predicate_controls", "3"),), GAP
        ),
        Break("bandit 不再绑项目配置", (("bandit_config_bound", "no"),), GAP),
        Break("mypy 严格旗标掉到 6 项", (("mypy_strict_flags", "6"),), GAP),
        Break("_bandit 又把 stdout 和 stderr 拼起来", (("stream_concat_sites", "1"),), GAP),
        Break("ruff check 面被摘掉", (("plane_ruff_check", "no"),), GAP),
        Break("普查根被改名，扫到的面缩水", (("census_root_missing", "1"),), GAP),
    ),
)


# --------------------------------------------------------------------------- #
# AC-17|06: coverage of the A2 new-code population, judged on a stamped archive
# --------------------------------------------------------------------------- #

COV_LIVE_REL: Final = "coverage.xml"
COV_STAMP_DIR: Final = "docs/evidence/C85"
COV_ARCHIVE_XML: Final = f"{COV_STAMP_DIR}/coverage-final.xml"
COV_ARCHIVE_HTML: Final = f"{COV_STAMP_DIR}/coverage-final-html.zip"
COV_STAMP_REL: Final = f"{COV_STAMP_DIR}/coverage-final-stamp.json"
COV_STAMP_TOOL: Final = "scripts/quality/coverage_archive_stamp.py"
COV_COND_RE: Final = re.compile(r"\((\d+)/(\d+)\)")
COV_NAMED_ROOTS: Final = ("opendata/data", "pipeline", "opendata_fuyao")
COV_NEW_CODE_MIN: Final = 85.0
COV_ROOT_MIN: Final = 90.0
COV_DRIFT_MAX: Final = 0.5
COV_SHA_RE: Final = re.compile(r"^[0-9a-fA-F]{7,40}$")
#: The report's four counters and two rates, recomputed from the per-line data.
COV_XML_IDENTITY_FACES: Final = 6
COV_STAMP_AGGREGATE_FACES: Final = 5
COV_STAMP_POPULATION_FACES: Final = 8
COV_FORMULA_CONTROL_FACES: Final = 4
#: Two readings of the same face, one over the floor and one a hundredth short of it.
COV_ROOT_PCT_OK: Final = "opendata/data=90.16, pipeline=90.02, opendata_fuyao=93.79"
COV_ROOT_PCT_BELOW: Final = "opendata/data=90.16, pipeline=88.84, opendata_fuyao=93.79"
COV_ROOT_PCT_EDGE: Final = "opendata/data=89.99, pipeline=90.02, opendata_fuyao=93.79"


class _CovUnit(TypedDict):
    """One measured file: statements, missing statements, covered arcs, total arcs."""

    stmts: int
    miss: int
    arc_cov: int
    arc_total: int


class _CovGroup(TypedDict):
    """One aggregated population and its statement+branch ratio."""

    files: int
    stmts: int
    miss: int
    arc_cov: int
    arc_total: int
    pct: float


def _cov_ratio(keys: Iterable[str], rows: Mapping[str, _CovUnit]) -> _CovGroup:
    """_cov_ratio: aggregate the way coverage.py does -- (stmt-cov + arc-cov) / (stmt + arc)."""
    picked = [rows[k] for k in keys if k in rows]
    stmts = sum(u["stmts"] for u in picked)
    miss = sum(u["miss"] for u in picked)
    arc_cov = sum(u["arc_cov"] for u in picked)
    arc_total = sum(u["arc_total"] for u in picked)
    den = stmts + arc_total
    return {
        "files": len(picked),
        "stmts": stmts,
        "miss": miss,
        "arc_cov": arc_cov,
        "arc_total": arc_total,
        "pct": 100.0 * (stmts - miss + arc_cov) / den if den else 0.0,
    }


def _cov_formula_controls() -> int:
    """_cov_formula_controls: four arms that pin the ratio, one of them the formula it replaces."""
    rows: dict[str, _CovUnit] = {
        "a.py": {"stmts": 100, "miss": 10, "arc_cov": 8, "arc_total": 10},
        "b.py": {"stmts": 50, "miss": 0, "arc_cov": 0, "arc_total": 0},
    }
    both = _cov_ratio(("a.py", "b.py"), rows)
    only_b = _cov_ratio(("b.py",), rows)
    empty = _cov_ratio((), rows)
    covered = 150 - 10 + 8
    return sum(
        (
            abs(both["pct"] - 100.0 * covered / 160) < 1e-9,
            # A denominator of statements+2*arcs would read 84.375: one arc is one unit, not two.
            abs(both["pct"] - 100.0 * covered / 180) > 1e-9,
            only_b["pct"] == 100.0,
            empty["files"] == 0 and empty["pct"] == 0.0,
        )
    )


def _cov_relative(filename: str, sources: Sequence[str], root: Path) -> str:
    """_cov_relative: resolve a Cobertura ``filename`` (relative to ``<source>``) to the repo."""
    for source in sources:
        with suppress(ValueError):
            return str((Path(source) / filename).resolve().relative_to(root))
    return filename


def _cov_read(xml_path: Path, root: Path) -> tuple[dict[str, _CovUnit], dict[str, Any]]:
    """_cov_read: parse one Cobertura report into per-file units plus its own counters.

    Args:
        xml_path: The report to read.
        root: Repository root to resolve file names against.

    Returns:
        Repo-relative path -> unit, and the report's aggregate attributes.

    Raises:
        ProbeError: When the report declares no ``<source>`` root to resolve names against.
    """
    node = ET.parse(xml_path).getroot()  # noqa: S314  # nosec B314  # our own stamped report
    sources = [src.text or "" for src in node.findall("./sources/source")]
    if not sources:
        raise ProbeError(f"{xml_path} declares no <source> root")
    rows: dict[str, _CovUnit] = {}
    for cls in node.iter("class"):
        rel = _cov_relative(cls.get("filename") or "", sources, root)
        unit: _CovUnit = {"stmts": 0, "miss": 0, "arc_cov": 0, "arc_total": 0}
        for line in cls.iter("line"):
            unit["stmts"] += 1
            if not int(line.get("hits") or 0):
                unit["miss"] += 1
            pair = COV_COND_RE.search(line.get("condition-coverage") or "")
            if pair:
                unit["arc_cov"] += int(pair.group(1))
                unit["arc_total"] += int(pair.group(2))
        rows[rel] = unit
    agg = {
        "lines_valid": int(node.get("lines-valid") or 0),
        "lines_covered": int(node.get("lines-covered") or 0),
        "branches_valid": int(node.get("branches-valid") or 0),
        "branches_covered": int(node.get("branches-covered") or 0),
        "line_rate": float(node.get("line-rate") or 0.0),
        "branch_rate": float(node.get("branch-rate") or 0.0),
    }
    return rows, agg


def _cov_identity(rows: Mapping[str, _CovUnit], agg: Mapping[str, Any]) -> int:
    """_cov_identity: how many of the report's four counters and two rates the recompute matches."""
    lines_valid = sum(u["stmts"] for u in rows.values())
    lines_covered = sum(u["stmts"] - u["miss"] for u in rows.values())
    arcs_valid = sum(u["arc_total"] for u in rows.values())
    arcs_covered = sum(u["arc_cov"] for u in rows.values())
    return sum(
        (
            lines_valid == int(agg["lines_valid"]),
            lines_covered == int(agg["lines_covered"]),
            arcs_valid == int(agg["branches_valid"]),
            arcs_covered == int(agg["branches_covered"]),
            (round(lines_covered / lines_valid, 4) if lines_valid else 0.0)
            == round(float(agg["line_rate"]), 4),
            (round(arcs_covered / arcs_valid, 4) if arcs_valid else 0.0)
            == round(float(agg["branch_rate"]), 4),
        )
    )


def _cov_config(c: Context) -> tuple[list[str], list[str]]:
    """_cov_config: the coverage source roots and omit globs this repository runs with."""
    cfg = tomllib.loads((c.root / "pyproject.toml").read_text(encoding="utf-8"))
    run = cfg["tool"]["coverage"]["run"]
    return list(run.get("source") or []), list(run.get("omit") or [])


def _cov_a2_split(
    c: Context, rows: Mapping[str, _CovUnit], a2: Sequence[str]
) -> tuple[Facts, list[str]]:
    """_cov_a2_split: account for every A2 file against the report, so none can go missing."""
    source_roots, omits = _cov_config(c)
    py = [f for f in a2 if f.endswith(".py")]
    under = [f for f in py if any(f == r or f.startswith(f"{r}/") for r in source_roots)]
    omitted = [f for f in under if any(fnmatch.fnmatch(f, o) for o in omits)]
    reported = sorted(f for f in under if f in rows)
    unreported = [f for f in under if f not in rows and f not in omitted]
    basenames = {k.rsplit("/", 1)[-1] for k in rows}
    naive = [f for f in a2 if f in basenames]
    return (
        {
            "a2_total": count(len(a2)),
            "a2_python": count(len(py)),
            "a2_under_source": count(len(under)),
            "a2_config_omitted": count(len(omitted)),
            "a2_in_report": count(len(reported)),
            "a2_unreported": count(len(unreported)),
            "a2_unreported_sample": ", ".join(unreported[:2]) or "-",
            "a2_outside_source": count(len([f for f in py if f not in under])),
            "a2_accounting": flag(len(under) == len(reported) + len(omitted) + len(unreported)),
            "join_resolved": count(len(reported)),
            "join_naive_basename": count(len(naive)),
            "join_resolution_matters": flag(len(reported) > len(naive)),
        },
        reported,
    )


def _cov_retired_files(token: str) -> tuple[list[str], str]:
    """_cov_retired_files: enumerate what a retired package held, from its deleting commit."""
    # ``--`` is load-bearing: the path is gone from the working tree, and without the separator
    # git parses it as a revision and exits 128, which would read as "no such history".
    rc, out = run_argv(["git", "log", "--format=%H", "--diff-filter=D", "-n", "1", "--", token])
    if rc != 0:
        return [], f"{token}: 查询删除提交失败（git rc={rc}）"
    sha = out.splitlines()[0].strip() if out.splitlines() else ""
    if not COV_SHA_RE.match(sha):
        return [], f"{token}: 没有删除它的提交"
    rc2, out2 = run_argv(
        ["git", "show", "--name-only", "--format=", "--diff-filter=D", sha, "--", token]
    )
    files = [line.strip() for line in out2.splitlines() if line.strip()]
    return files, f"{token} 由 {sha[:9]} 删除，{len(files)} 个历史文件"


def _cov_historical_map(rel: str) -> str | None:
    """_cov_historical_map: follow one retired path forward, or say the record has no entry."""
    try:
        return str(_LAYOUT.historical_identity(rel))
    except _LAYOUT.SourceLayoutError:
        return None


def _cov_root_keys(
    token: str, rows: Mapping[str, _CovUnit]
) -> tuple[list[str], str, list[str], list[str]]:
    """_cov_root_keys: resolve one named root to measured files.

    Returns:
        The matched paths, how they were matched, the historical names the layout map cannot
        resolve, and the resolved Python files that carry no coverage row.
    """
    direct = sorted(k for k in rows if k == token or k.startswith(f"{token}/"))
    if direct:
        return direct, "目录前缀", [], []
    component = sorted(k for k in rows if token in PurePosixPath(k).parts)
    if component:
        head = PurePosixPath(component[0]).parts
        return component, f"路径成分（{'/'.join(head[:-1])}/）", [], []
    historical, note = _cov_retired_files(token)
    mapped: list[str] = []
    unmapped: list[str] = []
    for rel in historical:
        moved = _cov_historical_map(rel)
        if moved is None:
            unmapped.append(rel)
        else:
            mapped.append(moved)
    pys = [m for m in mapped if m.endswith(".py")]
    return (
        sorted(k for k in rows if k in pys),
        f"历史身份映射（{note}）",
        unmapped,
        sorted(m for m in pys if m not in rows),
    )


def _cov_stamp(c: Context) -> dict[str, Any]:
    """_cov_stamp: read the record that claims which commit this archive measured."""
    path = c.root / COV_STAMP_REL
    if not path.is_file():
        return {}
    with suppress(json.JSONDecodeError, OSError):
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return loaded
    return {}


def _cov_stamp_agreements(stamp: Mapping[str, Any], group: _CovGroup) -> int:
    """_cov_stamp_agreements: five equalities between the stamp and the archive as it sits now."""
    agg = stamp.get("aggregates")
    if not isinstance(agg, dict) or not agg:
        return 0
    return sum(
        (
            int(agg.get("lines_valid", -1)) == group["stmts"],
            int(agg.get("lines_covered", -1)) == group["stmts"] - group["miss"],
            int(agg.get("branches_valid", -1)) == group["arc_total"],
            int(agg.get("branches_covered", -1)) == group["arc_cov"],
            str(agg.get("combined_pct")) == f"{group['pct']:.2f}",
        )
    )


def _cov_stamp_population_agreements(
    stamp: Mapping[str, Any], pops: Mapping[str, _CovGroup]
) -> int:
    """_cov_stamp_population_agreements: each judged population's size and ratio, in stamp."""
    recorded = stamp.get("populations")
    if not isinstance(recorded, dict) or not recorded:
        return 0
    hits = 0
    for name, group in pops.items():
        entry = recorded.get(name)
        if isinstance(entry, dict):
            hits += int(entry.get("files", -1)) == group["files"]
            hits += str(entry.get("combined_pct")) == f"{group['pct']:.2f}"
    return hits


def _cov_tree_identity(commit: str, sources: Sequence[str]) -> tuple[bool, str]:
    """_cov_tree_identity: do the measured roots hold identical git trees at the stamp and HEAD?"""
    if not COV_SHA_RE.match(commit):
        return False, "-"
    notes: list[str] = []
    for src in sources:
        rc_at, at = run_argv(["git", "rev-parse", f"{commit}:{src}"])
        rc_head, head = run_argv(["git", "rev-parse", f"HEAD:{src}"])
        if rc_at or rc_head:
            return False, f"{src}: 取不到源码树"
        same = at.strip() == head.strip()
        notes.append(f"{src} {at.strip()[:12]} {'==' if same else '!='} {head.strip()[:12]}")
        if not same:
            return False, " | ".join(notes)
    return True, " | ".join(notes)


def _cov_ancestor(commit: str) -> bool:
    """_cov_ancestor: is the stamped commit on HEAD's history rather than on a side branch?"""
    if not COV_SHA_RE.match(commit):
        return False
    rc, _out = run_argv(["git", "merge-base", "--is-ancestor", commit, "HEAD"])
    return rc == 0


def _cov_html_archive(root: Path) -> tuple[int, int, bool]:
    """_cov_html_archive: pages inside the archived zip, its size, and whether the index is in."""
    path = root / COV_ARCHIVE_HTML
    if not path.is_file():
        return 0, 0, False
    size = path.stat().st_size
    with suppress(zipfile.BadZipFile, OSError), zipfile.ZipFile(path) as book:
        names = book.namelist()
        pages = [n for n in names if n.endswith(".html")]
        return len(pages), size, any(n.rsplit("/", 1)[-1] == "index.html" for n in names)
    return 0, size, False


def measure_ac17_06(c: Context) -> Facts:
    """measure_ac17_06: see the AC-17|06 measurement it serves."""
    arch = c.root / COV_ARCHIVE_XML
    live = c.root / COV_LIVE_REL
    a2 = list(script_module(A2_CHECK_TOOL).resolve_files(None) or [])
    facts: Facts = {
        "formula_controls": count(_cov_formula_controls()),
        "archive_xml_exists": flag(arch.is_file()),
        "archive_xml_bytes": count(arch.stat().st_size if arch.is_file() else 0),
        "live_xml_exists": flag(live.is_file()),
        "live_xml_bytes": count(live.stat().st_size if live.is_file() else 0),
        "stamp_exists": flag((c.root / COV_STAMP_REL).is_file()),
        "stamp_tool": flag((c.root / COV_STAMP_TOOL).is_file()),
    }
    source_roots, _omits = _cov_config(c)
    facts["coverage_sources"] = ", ".join(source_roots) or "-"
    tracked = set(c.tracked())
    facts["archive_tracked"] = flag(
        COV_ARCHIVE_XML in tracked and COV_ARCHIVE_HTML in tracked and COV_STAMP_REL in tracked
    )
    pages, html_bytes, index_in_zip = _cov_html_archive(c.root)
    facts["archive_html_pages"] = count(pages)
    facts["archive_html_bytes"] = count(html_bytes)
    facts["archive_html_index"] = flag(index_in_zip)

    if not arch.is_file():
        split, _none = _cov_a2_split(c, {}, a2)
        facts.update(split)
        facts.update(
            {
                "archive_identity": "0",
                "archive_reported_files": "0",
                "stamp_aggregates": "0",
                "stamp_populations": "0",
                "stamp_commit": "(无戳记)",
                "stamp_ancestor": "no",
                "tree_identity": "no",
                "tree_identity_sample": "-",
                "new_code_pct": "0.00",
                "new_code_files": "0",
                "new_code_units": "-",
                "root_pct": "-",
                "root_empty_pop": count(len(COV_NAMED_ROOTS)),
                "root_resolution": "-",
                "root_unmapped": "0",
                "root_unmapped_sample": "-",
                "root_absent": "0",
                "root_absent_sample": "-",
                "live_a2_in_report": "-",
                "drift_max": "-",
                "drift_within": "no",
            }
        )
        return facts

    rows, agg = _cov_read(arch, c.root)
    facts["archive_identity"] = count(_cov_identity(rows, agg))
    facts["archive_reported_files"] = count(len(rows))
    split, new_code = _cov_a2_split(c, rows, a2)
    facts.update(split)

    pops: dict[str, _CovGroup] = {"new_code": _cov_ratio(new_code, rows)}
    resolutions: list[str] = []
    empty_pops = 0
    unmapped: list[str] = []
    absent: list[str] = []
    for token in COV_NAMED_ROOTS:
        keys, how, miss_map, miss_rows = _cov_root_keys(token, rows)
        pops[token] = _cov_ratio(keys, rows)
        resolutions.append(f"{token}={len(keys)}（{how}）")
        empty_pops += not keys
        unmapped += miss_map
        absent += miss_rows

    stamp = _cov_stamp(c)
    commit = str(stamp.get("commit") or "")
    facts["stamp_aggregates"] = count(_cov_stamp_agreements(stamp, pops["new_code"]))
    facts["stamp_populations"] = count(_cov_stamp_population_agreements(stamp, pops))
    facts["stamp_commit"] = commit[:9] or "(无)"
    facts["stamp_ancestor"] = flag(_cov_ancestor(commit))
    same, sample = _cov_tree_identity(commit, source_roots)
    facts["tree_identity"] = flag(same)
    facts["tree_identity_sample"] = sample

    new = pops["new_code"]
    facts["new_code_pct"] = f"{new['pct']:.2f}"
    facts["new_code_files"] = count(new["files"])
    facts["new_code_units"] = (
        f"覆盖 {new['stmts'] - new['miss']}+{new['arc_cov']} / 总量 "
        f"{new['stmts']}+{new['arc_total']}（语句+分支弧）"
    )
    facts["root_pct"] = ", ".join(f"{t}={pops[t]['pct']:.2f}" for t in COV_NAMED_ROOTS)
    facts["root_empty_pop"] = count(empty_pops)
    facts["root_resolution"] = " | ".join(resolutions)
    facts["root_unmapped"] = count(len(unmapped))
    facts["root_unmapped_sample"] = ", ".join(unmapped[:2]) or "-"
    facts["root_absent"] = count(len(absent))
    facts["root_absent_sample"] = ", ".join(absent[:2]) or "-"

    if live.is_file():
        live_rows, _live_agg = _cov_read(live, c.root)
        live_split, live_new = _cov_a2_split(c, live_rows, a2)
        deltas = [abs(_cov_ratio(live_new, live_rows)["pct"] - new["pct"])]
        for token in COV_NAMED_ROOTS:
            keys, _h, _m, _a = _cov_root_keys(token, live_rows)
            deltas.append(abs(_cov_ratio(keys, live_rows)["pct"] - pops[token]["pct"]))
        facts["live_a2_in_report"] = live_split["a2_in_report"]
        facts["drift_max"] = f"{max(deltas):.2f}"
        facts["drift_within"] = flag(max(deltas) <= COV_DRIFT_MAX)
    else:
        facts["live_a2_in_report"] = "-"
        facts["drift_max"] = "-"
        facts["drift_within"] = "no"
    return facts


def judge_ac17_06(facts: Facts) -> Verdict:
    """judge_ac17_06: see the AC-17|06 measurement it serves."""
    checks: tuple[tuple[str, bool], ...] = (
        ("公式四臂自证未全", facts["formula_controls"] != str(COV_FORMULA_CONTROL_FACES)),
        (
            "归档报告重算与 coverage.py 自报不一致",
            facts["archive_identity"] != str(COV_XML_IDENTITY_FACES),
        ),
        ("戳记聚合与归档现状不一致", facts["stamp_aggregates"] != str(COV_STAMP_AGGREGATE_FACES)),
        (
            "戳记分总体与归档现状不一致",
            facts["stamp_populations"] != str(COV_STAMP_POPULATION_FACES),
        ),
        ("A2 源内计数 ≠ 报告+排除+未报告", facts["a2_accounting"] != "yes"),
        ("源内有 A2 文件没进报告", facts["a2_unreported"] != "0"),
        ("联接未过 <source>，总体可与报告脱钩", facts["join_resolution_matters"] != "yes"),
        (
            f"A2 新代码 {facts['new_code_pct']}% 未达 {COV_NEW_CODE_MIN:.0f}%",
            float(facts["new_code_pct"]) < COV_NEW_CODE_MIN,
        ),
        ("有点名根解析为空总体", facts["root_empty_pop"] != "0"),
        ("退役目录有历史身份映射不上", facts["root_unmapped"] != "0"),
        ("映射到的文件在报告里没有行", facts["root_absent"] != "0"),
        (
            f"点名根未达 {COV_ROOT_MIN:.0f}%：{facts['root_pct']}",
            not _roots_over_floor(facts["root_pct"]),
        ),
        (
            "归档 xml 缺失或为空",
            facts["archive_xml_exists"] != "yes" or not positive(facts["archive_xml_bytes"]),
        ),
        ("归档 xml/戳记未被 git 跟踪", facts["archive_tracked"] != "yes"),
        (
            "戳记或生成工具缺失",
            facts["stamp_exists"] != "yes" or facts["stamp_tool"] != "yes",
        ),
        (
            "html 归档缺失或为空",
            not positive(facts["archive_html_bytes"]) or not positive(facts["archive_html_pages"]),
        ),
        ("html 归档没有 index", facts["archive_html_index"] != "yes"),
        ("戳记 commit 不在 HEAD 的历史上", facts["stamp_ancestor"] != "yes"),
        ("被测源码树与 HEAD 不同树", facts["tree_identity"] != "yes"),
        ("现盘 coverage.xml 缺失", facts["live_xml_exists"] != "yes"),
        (f"现盘与归档漂移超 ±{COV_DRIFT_MAX} 个百分点", facts["drift_within"] != "yes"),
    )
    ok = not any(fired for _label, fired in checks)
    readings = (
        f"判定总体=A2 新代码里被覆盖率工具计量的那部分：A2 {facts['a2_total']} 个文件，Python "
        f"{facts['a2_python']}，在 coverage source（{facts['coverage_sources']}）内 "
        f"{facts['a2_under_source']}，配置显式排除 {facts['a2_config_omitted']}，进了报告 "
        f"{facts['a2_in_report']}，源内却没进报告 {facts['a2_unreported']}"
        + (f"（{facts['a2_unreported_sample']}）" if facts["a2_unreported_sample"] != "-" else "")
        + f"，源外 {facts['a2_outside_source']}；对账等式={facts['a2_accounting']}",
        f"联接键必须过 <source>：解析后命中 {facts['join_resolved']}，"
        f"拿 Cobertura 原始名直接对只命中 {facts['join_naive_basename']}；解析起作用="
        f"{facts['join_resolution_matters']}——不对这一层，总体会静悄悄地变成空集然后 0/0",
        f"公式四臂自证 {facts['formula_controls']}/4（一条臂专门否掉「分母把弧算两遍」）；"
        f"归档报告逐行重算 vs coverage.py 自报计数与比率 = {facts['archive_identity']}/"
        f"{COV_XML_IDENTITY_FACES}（报告里 {facts['archive_reported_files']} 个文件）",
        f"A2 新代码 statement+branch = {facts['new_code_pct']}%（门 {COV_NEW_CODE_MIN:.0f}%），"
        f"{facts['new_code_files']} 个文件，单位 {facts['new_code_units']}",
        f"三个点名根 {facts['root_pct']}（门 {COV_ROOT_MIN:.0f}%），"
        f"空总体 {facts['root_empty_pop']}；解析方式 {facts['root_resolution']}",
        f"opendata_fuyao 是退役目录：历史映射解析失败 {facts['root_unmapped']}"
        + (f"（{facts['root_unmapped_sample']}）" if facts["root_unmapped_sample"] != "-" else "")
        + f"，映射到了但没有覆盖数据 {facts['root_absent']}"
        + (f"（{facts['root_absent_sample']}）" if facts["root_absent_sample"] != "-" else "")
        + "——名字对不上就判不成立，而不是当成空集通过",
        f"报告归档：{COV_ARCHIVE_XML} {facts['archive_xml_bytes']} 字节、git 跟踪="
        f"{facts['archive_tracked']}；html zip {facts['archive_html_bytes']} 字节 / "
        f"{facts['archive_html_pages']} 页 / index={facts['archive_html_index']}",
        f"戳记 commit={facts['stamp_commit']}，在 HEAD 历史上={facts['stamp_ancestor']}，"
        f"被测源码树与 HEAD 同树={facts['tree_identity']}"
        + (f"（{facts['tree_identity_sample']}）" if facts["tree_identity_sample"] != "-" else "")
        + f"；戳记自合 {facts['stamp_aggregates']}/{COV_STAMP_AGGREGATE_FACES}、"
        f"分总体 {facts['stamp_populations']}/{COV_STAMP_POPULATION_FACES}",
        f"现盘 {COV_LIVE_REL} 存在={facts['live_xml_exists']}"
        f"（{facts['live_xml_bytes']} 字节，A2 命中 {facts['live_a2_in_report']}），"
        f"与归档最大漂移 {facts['drift_max']} 个百分点，在 ±{COV_DRIFT_MAX} 内="
        f"{facts['drift_within']}",
    )
    reason = "" if ok else " / ".join(label for label, fired in checks if fired)
    return Verdict(PROVEN if ok else GAP, readings, reason)


def _roots_over_floor(root_pct: str) -> bool:
    """_roots_over_floor: every named root must be present in the reading and at or over 90."""
    chunks = [c for c in root_pct.split(", ") if "=" in c]
    if len(chunks) != len(COV_NAMED_ROOTS):
        return False
    for chunk in chunks:
        _name, _, value = chunk.partition("=")
        try:
            if float(value) < COV_ROOT_MIN:
                return False
        except ValueError:
            return False
    return True


AC17_06 = Probe(
    item="AC-17|06",
    expects="报告（xml+html）非空归档",
    summary="覆盖率判定挂在带戳记的归档报告上：总体逐一对账，公式与报告自证同值，点名根各自解析",
    measure=measure_ac17_06,
    judge=judge_ac17_06,
    repair={
        "formula_controls": "4",
        "archive_identity": "6",
        "stamp_aggregates": "5",
        "stamp_populations": "8",
        "a2_accounting": "yes",
        "a2_unreported": "0",
        "join_resolution_matters": "yes",
        "new_code_pct": "88.09",
        "root_empty_pop": "0",
        "root_unmapped": "0",
        "root_absent": "0",
        "root_pct": COV_ROOT_PCT_OK,
        "archive_xml_exists": "yes",
        "archive_xml_bytes": "1214502",
        "archive_tracked": "yes",
        "stamp_exists": "yes",
        "stamp_tool": "yes",
        "archive_html_bytes": "2002747",
        "archive_html_pages": "321",
        "archive_html_index": "yes",
        "stamp_ancestor": "yes",
        "tree_identity": "yes",
        "live_xml_exists": "yes",
        "drift_within": "yes",
    },
    breaks=(
        Break("A2 新代码掉到 84.9%", (("new_code_pct", "84.90"),), GAP),
        Break("pipeline 实测 88.84，未到 90", (("root_pct", COV_ROOT_PCT_BELOW),), GAP),
        Break("opendata/data 只差 0.01 个点", (("root_pct", COV_ROOT_PCT_EDGE),), GAP),
        Break("一个点名根解析成空总体", (("root_empty_pop", "1"),), GAP),
        Break("退役目录少一条历史身份映射", (("root_unmapped", "1"),), GAP),
        Break("映射到的文件在报告里没有行", (("root_absent", "1"),), GAP),
        Break("源内有个 A2 文件没进报告", (("a2_unreported", "1"),), GAP),
        Break("A2 总体对账等式断裂", (("a2_accounting", "no"),), GAP),
        Break("联接不过 <source>，总体与报告脱钩", (("join_resolution_matters", "no"),), GAP),
        Break("重算与 coverage.py 自报计数不一致", (("archive_identity", "4"),), GAP),
        Break("公式四臂断一条（弧被算两遍）", (("formula_controls", "3"),), GAP),
        Break("戳记与归档文件不再一致", (("stamp_aggregates", "4"),), GAP),
        Break("分总体戳记少对一项", (("stamp_populations", "7"),), GAP),
        Break("测量之后 opendata 源码树又动了", (("tree_identity", "no"),), GAP),
        Break("戳记 commit 不在 HEAD 的历史上", (("stamp_ancestor", "no"),), GAP),
        Break("html 归档是空的", (("archive_html_pages", "0"),), GAP),
        Break("归档没被 git 跟踪", (("archive_tracked", "no"),), GAP),
        Break("现盘报告与归档漂移超限", (("drift_within", "no"),), GAP),
    ),
)


# --------------------------------------------------------------------------- #
# The probe table
# --------------------------------------------------------------------------- #

PROBES: Final[tuple[Probe, ...]] = (
    c64_node_probe(
        "AC-10|04",
        "不影响其它 provider",
        "optional SDK provider isolation",
        measure_c64_sdk_isolation,
        1,
    ),
    c64_node_probe(
        "AC-11|07",
        "首帧认证",
        "ODS commit to actual local websocket notification",
        measure_c64_pipeline_socket,
        4,
    ),
    c64_node_probe(
        "AC-13|02",
        "生产未配置时启动即失败",
        "actual production lifespan scheduler ownership",
        measure_c64_lifespan,
        2,
    ),
    c64_node_probe(
        "AC-14|02",
        "成功运行回测",
        "degraded local consumer handoff and real backtest",
        measure_c64_consumer_backtest,
        2,
    ),
    c64_node_probe(
        "AC-14|03",
        "QUICKSTART 黄金路径",
        "fresh documented consumer script reproduction",
        measure_c64_consumer_golden,
        1,
    ),
    c64_node_probe(
        "AC-14|01", "基础限速", "consumer Key lifecycle and rate limit", measure_c64_key_limit, 9
    ),
    c64_node_probe(
        "AC-13|04",
        "水位以 ods 为准",
        "ODS truth after raw-cache deletion",
        measure_c64_ods_cache_rerun,
        2,
    ),
    c64_node_probe(
        "AC-13|06",
        "一键重试",
        "single-symbol isolation and browser retry",
        measure_c64_retry_browser,
        2,
    ),
    c64_node_probe(
        "§6|03",
        "失败清单导出",
        "browser failed-list download and retry",
        measure_c64_export_browser,
        2,
    ),
    c64_node_probe(
        "AC-11|12",
        "旧机制迁移收口",
        "legacy loader and frontend gate membership",
        measure_c64_legacy_frontend,
        3,
    ),
    c64_archive_probe(
        "AC-6|01", "录制回放对照", "archived P0 replay observations", measure_c64_p0_comparison
    ),
    c64_archive_probe(
        "AC-7|03",
        "真机拉取一个全市场 dump",
        "actual historical fuyao smoke and dump",
        measure_c64_fuyao_live_archive,
    ),
    c64_archive_probe(
        "AC-12|03",
        "10~15%",
        "maintenance budget and abandonment process",
        measure_c64_maintenance_budget,
    ),
    c64_archive_probe(
        "§4|01",
        "差异人工复核",
        "historical dual-source measured differences",
        measure_c64_dual_comparison_archive,
    ),
    c64_archive_probe(
        "§4|05", "第三方基准", "historical independent index sample", measure_c64_gold_archive
    ),
    c64_archive_probe(
        "§6|04",
        "备份恢复演练",
        "current isolated dual-database restore",
        measure_c64_restore_archive,
    ),
    c64_node_probe(
        "AC-3|01", "能力均注册", "bundled provider registration", measure_c64_registry, 3
    ),
    c64_node_probe(
        "AC-3|02", "权威度+可用性", "authority and health fallback", measure_c64_auto, 3
    ),
    c64_node_probe("AC-3|03", "显式 source", "precise explicit routing", measure_c64_explicit, 3),
    c64_node_probe(
        "AC-3|04", "不参与 auto", "unverified and reserved notes exclusion", measure_c64_reserved, 3
    ),
    c64_node_probe(
        "AC-3|05", "只读接口", "capability and source registry APIs", measure_c64_cap_api, 4
    ),
    c64_node_probe(
        "AC-6|03",
        "未完成对照",
        "incomplete comparison cannot enter auto",
        measure_c64_unverified,
        3,
    ),
    c64_node_probe(
        "AC-7|01", "429 与 4001", "fuyao envelope auth and backoff", measure_c64_fuyao_transport, 4
    ),
    c64_node_probe(
        "AC-7|02", "中文文案", "business error messages and remedies", measure_c64_fuyao_errors, 3
    ),
    c64_node_probe("AC-7|04", "100%", "official endpoint map coverage", measure_c64_fuyao_map, 3),
    c64_node_probe(
        "AC-7|05", "source=ths", "THS contract registration", measure_c64_ths_contract, 3
    ),
    c64_node_probe(
        "AC-10|05",
        "openbb_map.yaml",
        "enabled provider compatibility map",
        measure_c64_openbb_map,
        2,
    ),
    c64_node_probe(
        "AC-11|06", "freshness", "catalog and freshness routes", measure_c64_freshness_api, 3
    ),
    c64_node_probe("AC-11|08", "断线补发", "missed batch meta replay", measure_c64_replay, 2),
    c64_node_probe(
        "AC-11|09", "full 模式", "full message ceilings and fallback", measure_c64_frames, 3
    ),
    c64_node_probe("AC-11|10", "路由回归", "existing routes regression", measure_c64_routes, 6),
    c64_node_probe(
        "AC-11|11", "REST 分页", "minimal REST and websocket client", measure_c64_client, 3
    ),
    c64_node_probe(
        "AC-12|01", "每文件哈希", "upstream lock provenance and hashes", measure_c64_lock, 2
    ),
    c64_node_probe(
        "AC-12|02", "函数 diff", "upstream inventory and signature diff", measure_c64_sync, 2
    ),
    c64_node_probe(
        "AC-13|01", "六步", "scheduled six-step pipeline task", measure_c64_scheduled_pipeline, 4
    ),
    c64_node_probe(
        "AC-13|03",
        "断点续拉",
        "resume completed shards and retry partial failures",
        measure_c64_resume,
        3,
    ),
    c64_node_probe(
        "§5|03",
        "429 有退避记录",
        "bounded rate-limit retry mechanism",
        measure_c64_ban_mechanism,
        2,
    ),
    c64_node_probe(
        "§6|01",
        "任一步失败",
        "step checkpoints resume hooks including metadata",
        measure_c64_steps,
        4,
    ),
    c64_node_probe("§6|02", "重载", "persistent scheduler reload", measure_c64_reload, 2),
    Probe(
        item="AC-4|03",
        expects="巡检",
        summary="daily P0 patrol, persisted streaks and retryable notification delivery",
        measure=measure_c64_patrol,
        judge=_c64_behavior,
        breaks=(
            Break("named behavior check fails", (("c64_exit", "1"),), GAP),
            Break("named node is absent", (("c64_absent", "missing"),), GAP),
            Break("a required node is skipped", (("c64_skipped", "1"),), GAP),
            Break("runtime binding removed", (("binding", "no"),), GAP),
        ),
        repair={
            "c64_runs": "6",
            "c64_exit": "0",
            "c64_passed": "6",
            "c64_failed": "0",
            "c64_skipped": "0",
            "c64_absent": "-",
            "binding": "yes",
        },
    ),
    Probe(
        item="AC-13|05",
        expects="缓存只存原始响应",
        summary="raw-response TTL cache and actual adjusted-query isolation",
        measure=measure_c64_cache,
        judge=_c64_behavior,
        breaks=(
            Break("named behavior check fails", (("c64_exit", "1"),), GAP),
            Break("named node is absent", (("c64_absent", "missing"),), GAP),
            Break("a required node is skipped", (("c64_skipped", "1"),), GAP),
            Break("runtime binding removed", (("binding", "no"),), GAP),
        ),
        repair={
            "c64_runs": "4",
            "c64_exit": "0",
            "c64_passed": "4",
            "c64_failed": "0",
            "c64_skipped": "0",
            "c64_absent": "-",
            "binding": "yes",
        },
    ),
    Probe(
        item="AC-4|01",
        expects="统一超时",
        summary="timeout / 429 retry / per-host rate / concurrency actual mocks",
        measure=measure_c64_ac4_01,
        judge=_c64_behavior,
        breaks=(
            Break("named behavior check fails", (("c64_exit", "1"),), GAP),
            Break("named node is absent", (("c64_absent", "missing"),), GAP),
            Break("a required node is skipped", (("c64_skipped", "1"),), GAP),
            Break("runtime binding removed", (("binding", "no"),), GAP),
        ),
        repair={
            "c64_runs": "5",
            "c64_exit": "0",
            "c64_passed": "5",
            "c64_failed": "0",
            "c64_skipped": "0",
            "c64_absent": "-",
            "binding": "yes",
        },
    ),
    Probe(
        item="AC-4|02",
        expects="失败分类",
        summary="structured safe transport failure events",
        measure=measure_c64_ac4_02,
        judge=_c64_behavior,
        breaks=(
            Break("named behavior check fails", (("c64_exit", "1"),), GAP),
            Break("named node is absent", (("c64_absent", "missing"),), GAP),
            Break("a required node is skipped", (("c64_skipped", "1"),), GAP),
            Break("runtime binding removed", (("binding", "no"),), GAP),
        ),
        repair={
            "c64_runs": "4",
            "c64_exit": "0",
            "c64_passed": "4",
            "c64_failed": "0",
            "c64_skipped": "0",
            "c64_absent": "-",
            "binding": "yes",
        },
    ),
    Probe(
        item="AC-7|06",
        expects="Key 缺失",
        summary="missing credentials exclusion and visible warning",
        measure=measure_c64_ac7_06,
        judge=_c64_behavior,
        breaks=(
            Break("named behavior check fails", (("c64_exit", "1"),), GAP),
            Break("named node is absent", (("c64_absent", "missing"),), GAP),
            Break("a required node is skipped", (("c64_skipped", "1"),), GAP),
            Break("runtime binding removed", (("binding", "no"),), GAP),
        ),
        repair={
            "c64_runs": "3",
            "c64_exit": "0",
            "c64_passed": "3",
            "c64_failed": "0",
            "c64_skipped": "0",
            "c64_absent": "-",
            "binding": "yes",
        },
    ),
    Probe(
        item="§5|05",
        expects="分钟线查询走文件存储",
        summary="file-backed minute query with isolated mainDB index",
        measure=measure_c64_minute,
        judge=_c64_behavior,
        breaks=(
            Break("named behavior check fails", (("c64_exit", "1"),), GAP),
            Break("named node is absent", (("c64_absent", "missing"),), GAP),
            Break("a required node is skipped", (("c64_skipped", "1"),), GAP),
            Break("runtime binding removed", (("binding", "no"),), GAP),
        ),
        repair={
            "c64_runs": "3",
            "c64_exit": "0",
            "c64_passed": "3",
            "c64_failed": "0",
            "c64_skipped": "0",
            "c64_absent": "-",
            "binding": "yes",
        },
    ),
    Probe(
        item="§5|04",
        expects="GB 级表",
        summary="one real GB ODS table bounded REST/CSV and EXPLAIN evidence",
        measure=measure_c64_gb_query,
        judge=judge_c64_gb_query,
        breaks=(
            Break("missing dated_evidence", (("dated_evidence", "no"),), GAP),
            Break("missing gb_table", (("gb_table", "no"),), GAP),
            Break("missing http_rows", (("http_rows", "no"),), GAP),
            Break("missing indexed", (("indexed", "no"),), GAP),
            Break("missing bounded_time", (("bounded_time", "no"),), GAP),
            Break("missing read_only", (("read_only", "no"),), GAP),
        ),
        repair={
            "dated_evidence": "yes",
            "gb_table": "yes",
            "http_rows": "yes",
            "indexed": "yes",
            "bounded_time": "yes",
            "read_only": "yes",
        },
    ),
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
                "实际命中有一条没有登记",
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
            Break("登记表缺失或空表", (("policy_valid", "no"),), GAP),
            Break("登记表 SHA 与文件内容不相等", (("sha_mismatch", "1"),), GAP),
            Break("登记中多出一条当前没有命中的旧路径", (("stale_paths", "1"),), GAP),
            Break(
                "purpose/category/review date/reviewer 缺失或不合规",
                (("metadata_invalid", "1"),),
                GAP,
            ),
            Break("新增的 AST 例外不绑定唯一获准上下文", (("metadata_exceptions", "2"),), GAP),
            Break("本轮 tracked hit 文件有路径不可读", (("unreadable", "1"),), GAP),
        ),
        repair={
            "outside": "0",
            "outside_sample": "",
            "unreadable": "0",
            "policy_valid": "yes",
            "stale_paths": "0",
            "sha_mismatch": "0",
            "metadata_invalid": "0",
            # The scanner's approved-context register is the denominator, so the clean reading
            # copies it instead of pinning yesterday's number: a literal went stale the day the
            # sixth context was added and the repaired face stopped looking like a real pass.
            "metadata_exceptions": "*metadata_expected",
        },
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
            Break("运行时正常但仓库名漂移", (("runtime", "validated"), ("consistent", "no")), GAP),
            Break(
                "四处一致但当前运行证据失败", (("runtime", "failed"), ("consistent", "yes")), GAP
            ),
            Break("四处一致但栈没有启动", (("runtime", "not-started"), ("consistent", "yes")), GAP),
            Break(
                "运行状态声称validated但仍有证据问题",
                (("runtime", "validated"), ("runtime_issue_count", "1"), ("consistent", "yes")),
                GAP,
            ),
        ),
        repair={
            "consistent": "yes",
            "runtime": "validated",
            "runtime_valid": "yes",
            "runtime_issue_count": "0",
            "runtime_issue_summary": "none",
        },
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
        summary="只读§1登记表：每个已引来源须有条款、用途、日期与责任人",
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
            Break("登记表缺失、畸形或被清空", (("table_valid", "no"), ("rows", "0")), GAP),
            Break("一行没写复核日期", (("dated", "0"),), GAP),
            Break("条款链接列写成散文", (("unlinked", "5"),), GAP),
            Break("责任人列为空或未指定", (("responsible_missing", "1"),), GAP),
        ),
        repair={
            "table_valid": "yes",
            "dated": "*rows",
            "undecided": "0",
            "unlinked": "0",
            "responsible_missing": "0",
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
                "真实 .env 或 .env.* 文件进库",
                (
                    ("strict", "1"),
                    ("env_paths", "1"),
                    ("env_sample", ".env.production"),
                ),
                GAP,
            ),
            Break(".idea 目录或 .pid 工件进库", (("strict", "1"), ("generated_paths", "1")), GAP),
            Break("出现额外 .env.example 模板路径", (("template_paths", "2"),), GAP),
            Break(
                "全局路径豁免重新引入",
                (("exact_paths", "no"), ("allow_paths", "1"), ("template_config_ok", "no")),
                GAP,
            ),
            Break(
                "公共规则级例外被扩宽、移除或增加额外 regex",
                (("rule_shapes", "no"),),
                GAP,
            ),
            Break("上游 5 文件漏登记一条", (("registered", "4"),), GAP),
            Break(
                "上游文件的历史路径映射不到树上的现存文件",
                (
                    ("upstream_present", "4"),
                    ("upstream_absent_names", f"{PORTED_ROOT}/stock/cons.py"),
                ),
                GAP,
            ),
            Break("搬运代码里还留着凭证形状字面量", (("live_shapes", "2"),), GAP),
            Break("全历史扫描器变红", (("gitleaks_rc", "1"),), GAP),
            Break(
                "扫描器自报的泄漏数不是 0（读数与 exit 码互相矛盾的那一面）",
                (("scan_leaks", "441"),),
                GAP,
            ),
            Break(".env.example 只依赖路径豁免而未扫描", (("template_config_ok", "no"),), GAP),
            Break("模板实际含有凭证形状", (("template_scan_rc", "1"),), GAP),
        ),
        repair={
            "strict": "0",
            "env_paths": "0",
            "generated_paths": "0",
            "template_paths": "1",
            "template_names": ".env.example",
            "exact_paths": "yes",
            "allow_paths": "0",
            "allow_names": "",
            "non_template": "0",
            "non_template_names": "",
            "rule_blocks": "2",
            "rule_shapes": "yes",
            "registered": "*upstream_files",
            "upstream_present": "*upstream_files",
            "upstream_absent_names": "",
            "live_shapes": "0",
            "gitleaks_rc": "0",
            "scan_leaks": "0",
            "template_config_ok": "yes",
            "template_scan_rc": "0",
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
            Break("ruff_ported 高于快照", (("cur_ruff_ported", "*snap_ruff_ported+1"),), GAP),
            Break(
                "direct_http_ported 高于快照",
                (("cur_direct_http_ported", "*snap_direct_http_ported+1"),),
                GAP,
            ),
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
            Break("路由不再调用实际读数实现", (("door_delegates", "no"),), GAP),
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
            "door_delegates": "yes",
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
            Break("merged catalog 可见行为用例变红或缺失", (("merged_e2e", "failed"),), GAP),
            Break(
                "旧函数列表到真实脚本详情用例变红或缺失", (("function_detail_e2e", "failed"),), GAP
            ),
        ),
        repair={
            "nav_entries": "1",
            "page_titles": "数据接口（合并页）",
            "route_is_catalog": "yes",
            "detail_in_catalog": "yes",
            "detail_route": "yes",
            "merged_e2e": "passed",
            "function_detail_e2e": "passed",
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
            Break(
                "pmax 溢出断言被删除或改成非零",
                (("asserts_placement", "1"), ("placement_partitions", "p2027")),
                GAP,
            ),
            Break("e2e 标记被摘（于是门禁里悄悄不跑）", (("marked", "no"),), GAP),
            Break("真仓库那一年仍缺", (("fallback_gap", "1"),), GAP),
            Break("没有一次真跑的留档", (("ran_live", "no"),), GAP),
        ),
        repair={
            "case_file": "yes",
            "inserts_new_year": "yes",
            "asserts_placement": "2",
            "placement_partitions": "p2027,pmax",
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
        summary=(
            "修订节点全绿（含生产 scoped ODS reader）+ run/batch/hook 的 affected_keys 传播链完整"
        ),
        measure=measure_ac9_07,
        judge=judge_ac9_07,
        breaks=(
            Break("修订传播节点变红（值不再同步）", (("passed", "1"), ("bad", "x=exit=1")), GAP),
            Break("节点被改名（两条里少一条就不算链上有人看过）", (("runs", "1"),), GAP),
            Break(
                "run 不再把 affected_keys 委托给 batch（后续 reader 与 diff 链均断开）",
                (("run_delegates_affected_keys", "no"),),
                GAP,
            ),
            Break(
                "batch 不再把 affected_keys 交给 _read_source（窗口外的修订取不到数）",
                (("batch_reader_gets_keys", "no"),),
                GAP,
            ),
            Break(
                "普通 reader 不再收到 affected_keys",
                (("normal_reader_gets_keys", "no"),),
                GAP,
            ),
            Break(
                "scoped reader 不再收到 affected_keys",
                (("scoped_reader_gets_keys", "no"),),
                GAP,
            ),
            Break("batch 不再把修订键传入差异重算", (("batch_merge_gets_keys", "no"),), GAP),
            Break(
                "partition 或普通 context hook 不再把 ods 拼法重拼成契约键 "
                "（600519.SH 对不上 600519）",
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
            "run_delegates_affected_keys": "yes",
            "batch_reader_gets_keys": "yes",
            "batch_merge_gets_keys": "yes",
            "normal_reader_gets_keys": "yes",
            "scoped_reader_gets_keys": "yes",
            "reader_gets_keys": "yes",
            "keys_extend_diffs": "yes",
            "partition_hook_resells_keys": "yes",
            "context_hook_resells_keys": "yes",
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
        "引用策略和 scanner 版本有效、冻结基线条数为零",
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
            Break("引用策略缺失、SHA 过期或 AST 描述不匹配", (("policy_valid", "no"),), GAP),
            Break("旧 scanner 基线仍被当成当前版本", (("baseline_current", "no"),), GAP),
            Break(
                "精确元数据例外计数不等于 scanner 获准的上下文数",
                (("metadata_exceptions", "2"),),
                GAP,
            ),
            Break("基线还在（本轮实际卡住的那一格：3 条没清零）", (("frozen", "3"),), GAP),
        ),
        repair={
            "detector_exit": "0",
            "self_test_exit": "0",
            "import_live": "0",
            "dynamic_live": "0",
            "no_new_reference": "yes",
            "only_down": "yes",
            "policy_valid": "yes",
            "baseline_current": "yes",
            "metadata_exceptions": "*metadata_expected",
            "frozen": "0",
        },
    ),
    Probe(
        item="AC-16|07",
        expects="在未安装 akshare/openbb 的干净环境中 P0 域集成测试通过",
        summary="选择式有可指的名字（marker 注册 + 标记单元 + P0 域全覆盖），"
        "本机这一遍跑在顶层上游包被 import hook 强制拒掉的子解释器里并全绿，"
        "留档的干净 venv 也全绿且是最新一轮自己写的、覆盖当前标记集",
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
            Break(
                "拦截钩子根本没拒（正向对照失败，干净面是假的）",
                (("block_control_hits", "0"), ("blocked_here", "no")),
                GAP,
            ),
            Break(
                "这一遍跑的过程中顶层上游包又被尝试了",
                (("block_attempted", "3"), ("blocked_here", "no")),
                GAP,
            ),
            Break(
                "顶层上游包留在 sys.modules 里（这份绿分不清用的是搬运层还是真包）",
                (("block_leaks", "1"), ("blocked_here", "no")),
                GAP,
            ),
            Break(
                "上游包清点面变小（判据要求的是这两个包）",
                (("block_pkgs", "1"),),
                GAP,
            ),
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
            "blocked_here": "yes",
            "block_pkgs": "*block_pkgs",
            "block_control_hits": "*block_pkgs",
            "block_attempted": "0",
            "block_leaks": "0",
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
                (("leg_module", ported_module_identity("opendata_http.stock.stock_zh_a_sina")),),
                GAP,
            ),
            Break("解析到的模块不再声明 akshare 出身", (("leg_is_ported_akshare", "no"),), GAP),
            Break("留档那次 run 走的不是当场默认腿", (("run_official_leg", "sina"),), GAP),
            Break(
                "仪器改了、留档还是别的腿那次 run",
                (
                    (
                        "run_official_target",
                        current_target_identity(
                            "opendata_http.stock.stock_zh_a_sina:stock_zh_a_daily"
                        ),
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
            Break("搬运路径/批次清单未通过逐项核验", (("port_scope_valid", "no"),), GAP),
            Break("没有任何机器字段能说 1A/1B 批次", (("tier_field_ok", "no"),), GAP),
        ),
        repair={
            "has_calls": "yes",
            "package_count": "13",
            "package_names": (
                "bond, economic, file_fold, fund, futures, futures_derivative, index, option, "
                "pro, stock, stock_feature, stock_fundamental, utils"
            ),
            "manifest_files": "325",
            "leg_modules": "11",
            "first_party_calls": "11",
            "called_missing": "0",
            "called_missing_names": "-",
            "scope_named": "11",
            "scope_named_raw": (
                "stock、stock_feature、stock_fundamental、futures、futures_derivative、index、"
                "fund、option、bond、economic、utils"
            ),
            "excluded_present": "0",
            "excluded_named": "8",
            "excluded_present_names": "-",
            "scope_missing": "0",
            "scope_missing_names": "-",
            "port_scope_valid": "yes",
            "port_scope_checked": "327",
            "port_scope_python": "325",
            "port_scope_resources": "2",
            "port_scope_problems": "0",
            "port_scope_problem_summary": "-",
            "tier_field_ok": "yes",
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
            Break("reference policy 缺失或 SHA/AST 绑定过期", (("policy_valid", "no"),), GAP),
            Break("零依赖基线还是旧 scanner 版本", (("baseline_current", "no"),), GAP),
            Break(
                "精确元数据例外计数不等于 scanner 获准的上下文数",
                (("metadata_exceptions", "2"),),
                GAP,
            ),
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
            "policy_valid": "yes",
            "baseline_current": "yes",
            "metadata_exceptions": "*metadata_expected",
            "frozen": "0",
        },
    ),
    Probe(
        item="AC-5|04",
        expects="环境中 P0 域函数可用",
        summary="根目录与索引都没有 akshare，顶层 akshare 被证明拦得住的解释器里搬运层与端点全可达",
        measure=measure_ac5_04,
        judge=judge_ac5_04,
        breaks=(
            Break("根目录 akshare/ 又回到磁盘上", (("root_dir", "yes"),), GAP),
            Break("git 索引里该前缀下还有文件", (("tracked_under_root", "1"),), GAP),
            Break("拦不住顶层 akshare，见证没生效", (("akshare_blocked", "no"),), GAP),
            Break("搬运层导入期回落到顶层 akshare", (("akshare_reached", "1"),), GAP),
            Break("顶层 akshare 留在了 sys.modules 里", (("akshare_leaks", "1"),), GAP),
            Break("搬运层 import 失败", (("import_exit", "1"),), GAP),
            Break("首方代码一处都不取用搬运层", (("call_targets", "0"),), GAP),
            Break("有一个端点函数取不到", (("callable_missing", "1"),), GAP),
        ),
        repair={
            "root_dir": "no",
            "tracked_under_root": "0",
            "akshare_blocked": "yes",
            "akshare_reached": "0",
            "akshare_leaks": "0",
            "import_exit": "0",
            "callable_missing": "0",
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
        summary="搬运层专用 Bandit 全量扫描绑定当前源码，逐 finding 审阅并保留已知风险",
        measure=measure_ac5_07,
        judge=judge_ac5_07,
        breaks=(
            Break("没有搬运层专用扫描目标", (("target_ok", "no"),), GAP),
            Break(
                "当前扫描或分组风险审阅证据缺失或无效", (("security_evidence_valid", "no"),), GAP
            ),
            Break("本轮扫描不再绑定当前源码", (("scan_source_tree_valid", "no"),), GAP),
            Break("triage 缺少当前 finding 或身份不匹配", (("scan_findings_covered", "no"),), GAP),
            Break("HIGH finding 未完整审阅", (("untriaged_high", "1"),), GAP),
            Break(
                "规则审阅缺 basis/disposition/follow-up", (("triage_rule_review_valid", "no"),), GAP
            ),
            Break("没有直接授权 AI 审阅", (("triage_reviewer_authorized", "no"),), GAP),
            Break("反序列化组没有明确风险审阅", (("triage_deser_reviewed", "no"),), GAP),
            Break("B301/B307 RCE finding 被删除或免罪", (("triage_rce_retained", "no"),), GAP),
            Break("人工 triage 尚未完成", (("triage_review_complete", "no"),), GAP),
            Break("审阅报告错误声称已修复", (("triage_security_fixed", "yes"),), GAP),
            Break("审阅报告隐去未解决风险", (("triage_unresolved_risks", "no"),), GAP),
            Break("扫描记录含错误或无 finding", (("scan_errors", "1"),), GAP),
            Break("扫描没有发现可审阅的结果", (("scan_findings", "0"),), GAP),
            Break(
                "当前搬运 manifest 与源文件集合不一致", (("scan_manifest_matches_tree", "no"),), GAP
            ),
            Break("manifest 覆盖范围小于磁盘搬运树", (("surface_covered", "no"),), GAP),
            Break("档案标注的轮次与档案所在目录不一致", (("archive_round_in_dir", "no"),), GAP),
        ),
        repair={
            "target_ok": "yes",
            "security_evidence_valid": "yes",
            "security_issue_count": "0",
            "scan_source_tree_valid": "yes",
            "scanner_exit_consistent": "yes",
            "scan_errors": "0",
            "scan_findings": "*scan_findings",
            "archive": "*archive",
            "archive_round_in_dir": "yes",
            "scan_manifest_ok": "yes",
            "scan_b_only": "yes",
            "findings_untriaged": "0",
            "untriaged_high": "0",
            "scan_findings_covered": "yes",
            "triage_rule_review_valid": "yes",
            "triage_high_reviewed": "yes",
            "triage_deser_reviewed": "yes",
            "triage_rce_retained": "yes",
            "triage_reviewer_authorized": "yes",
            "triage_review_complete": "yes",
            "triage_security_fixed": "no",
            "triage_unresolved_risks": "yes",
            "scan_manifest_matches_tree": "yes",
            "surface_covered": "yes",
        },
    ),
    Probe(
        item="AC-16|01",
        expects="代码审查记录",
        summary="自研 provider 包逐个查「提交说明声明 + 逐包审查档案」，档案按代码字节钉死",
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
            Break(
                "留档计数没有同行绑定行（文档一次性引用或探针自己的档案冒充记录）",
                (
                    ("declared_pkgs", "*provider_pkgs"),
                    ("recorded_pkgs", "*provider_pkgs"),
                    ("recorded_binds", "0"),
                    ("undeclared_list", "-"),
                ),
                GAP,
            ),
            Break(
                "档案钉住的代码字节与当前该包不符（审的不是现在这份）",
                (
                    ("declared_pkgs", "*provider_pkgs"),
                    ("recorded_pkgs", "*provider_pkgs"),
                    ("recorded_binds", "*provider_pkgs"),
                    ("undeclared_list", "-"),
                    ("record_refusals", "1"),
                    (
                        "record_refusal_detail",
                        "fred: 代码面摘要与该包当前字节不符（档案审的不是现在这份代码）",
                    ),
                ),
                GAP,
            ),
            Break(
                "档案覆盖面写的文件数与该包实测不一致",
                (
                    ("declared_pkgs", "*provider_pkgs"),
                    ("recorded_pkgs", "*provider_pkgs"),
                    ("recorded_binds", "*provider_pkgs"),
                    ("undeclared_list", "-"),
                    ("record_refusals", "1"),
                    ("record_refusal_detail", "ths: 覆盖面写 '1 个 py 文件'，实测 26 个"),
                ),
                GAP,
            ),
            Break(
                "逐包档案缺字段（无人署名、没有审查日期或没写反证）",
                (
                    ("declared_pkgs", "*provider_pkgs"),
                    ("recorded_pkgs", "*provider_pkgs"),
                    ("recorded_binds", "*provider_pkgs"),
                    ("undeclared_list", "-"),
                    ("record_refusals", "12"),
                    ("record_refusal_detail", "akshare: 审查人缺位或是自指（读到 ''）"),
                ),
                GAP,
            ),
            Break("适配层目录整块不在跟踪清单里（判据面为空）", (("provider_pkgs", "0"),), GAP),
        ),
        repair={
            "rule_written": "yes",
            "rule_extends_ast": "yes",
            "declared_pkgs": "*provider_pkgs",
            "recorded_pkgs": "*provider_pkgs",
            "recorded_binds": "*provider_pkgs",
            "record_refusals": "0",
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
            Break(
                "留档计数没有同行绑定行（文档一次性引用或探针自己的档案冒充记录）",
                (
                    ("openbb_imports", "0"),
                    ("declared_pkgs", "*provider_pkgs"),
                    ("recorded_pkgs", "*provider_pkgs"),
                    ("recorded_binds", "0"),
                ),
                GAP,
            ),
            Break(
                "逐包档案与该包当前字节脱钩（代码面摘要对不上）",
                (
                    ("openbb_imports", "0"),
                    ("declared_pkgs", "*provider_pkgs"),
                    ("recorded_pkgs", "*provider_pkgs"),
                    ("recorded_binds", "*provider_pkgs"),
                    ("record_refusals", "1"),
                    ("record_refusal_detail", "sec: 代码面摘要与该包当前字节不符"),
                ),
                GAP,
            ),
        ),
        repair={
            "openbb_imports": "0",
            "forbidden_covers_openbb": "yes",
            "declared_pkgs": "*provider_pkgs",
            "recorded_pkgs": "*provider_pkgs",
            "recorded_binds": "*provider_pkgs",
            "record_refusals": "0",
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
        summary="当前磁盘 325 Python + 2 resources 与 source pins 完整，并在隔离复制树真实导入；"
        "namespace 来源、网络尝试和 checkout 泄漏均受检查",
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
            Break("vendor manifest 缺失", (("manifest_present", "no"),), GAP),
            Break(
                "manifest/磁盘资源 inventory 少一个文件",
                (("manifest_resource_files", "1"),),
                GAP,
            ),
            Break(
                "namespace ancestor 意外加载仓库 BSL 源文件",
                (
                    ("namespace_sources_clean", "no"),
                    ("namespace_json", _ac16_namespace_source_counterfact_json()),
                ),
                GAP,
            ),
            Break(
                "隔离导入期间出现出站网络尝试",
                (("network_attempts_json", '["socket.connect:127.0.0.1:443"]'),),
                GAP,
            ),
            Break(
                "隔离解释器路径泄漏回当前 checkout",
                (
                    ("repository_path_leaks_json", '["/workspace/opendata/opendata"]'),
                    (
                        "sys_path_json",
                        '["/tmp/ac16-boundary-isolation","/workspace/opendata/opendata"]',
                    ),
                ),
                GAP,
            ),
        ),
        repair=_ac16_clean_repair_facts(),
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
    Probe(
        item="AC-19|01",
        expects="每日备份 + binlog",
        summary="备份脚本 + 两库 dump + 仓库内的每日触发点 + RPO/binlog 声明与一次实测取值",
        measure=measure_ac19_01,
        judge=judge_ac19_01,
        breaks=(
            Break("没有正式部署运行与备份新鲜度证据", (("deployment_observed", "no"),), GAP),
            Break(
                "备份脚本不在跟踪清单里",
                (("script_tracked", "no"), ("dump_calls", "0"), ("dump_sites", "0")),
                GAP,
            ),
            Break("脚本存在但只 dump 一个库（元数据或仓库漏一半）", (("dump_calls", "1"),), GAP),
            Break(
                "dump_one 调用还在、mysqldump 的真实调用被摘掉（只剩前置检查那行）",
                (("dump_sites", "0"),),
                GAP,
            ),
            Break(
                "每日触发点为零：只剩脚本头部注释里那行 crontab 示例",
                (("schedule_sites", "0"), ("schedule_list", "-")),
                GAP,
            ),
            Break("Compose 备份服务不再位于可选 backup profile", (("backup_profile", "no"),), GAP),
            Break("备份 runner 镜像不再安装并执行", (("backup_image", "no"),), GAP),
            Break("每日 02:00 UTC runner 时钟或调用路径失效", (("daily_runner", "no"),), GAP),
            Break("profile、镜像和 runner 的完整链路断开", (("schedule_chain", "no"),), GAP),
            Break("RPO ≤24h 的声明面被删", (("doc_rpo", "no"),), GAP),
            Break(
                "binlog 只有「可选」附录、没有一次实测取值",
                (("binlog_attested", "0"), ("binlog_attested_list", "-")),
                GAP,
            ),
        ),
        repair={
            "script_tracked": "yes",
            "dump_calls": "2",
            "dump_labels": "metadata, warehouse",
            "dump_sites": "1",
            "schedule_sites": "1",
            "schedule_list": "scripts/ops/backup_runner.sh",
            "backup_profile": "yes",
            "backup_image": "yes",
            "daily_runner": "yes",
            "schedule_chain": "yes",
            "doc_rpo": "yes",
            "doc_binlog": "yes",
            "binlog_optional": "no",
            "binlog_attested": "1",
            "isolated_binlog": "yes",
            "deployment_observed": "yes",
            # 取值要落在**长期事实面**上：整个档案面（`docs/evidence/`）与结论面都在被剔之列 ——
            # 一轮不能给自己的读数背书，写进任何一轮档案或台账的那一份正是判据自己排除掉的那一份，
            # 补了也还是 0。
            "binlog_attested_list": "docs/operations-backup-restore.md",
        },
    ),
    Probe(
        item="AC-19|02",
        expects="恢复演练完成一次",
        summary="A0 演练档案：早于本轮存在 + 六步检查单逐条通过 + 两个隔离恢复库 + 归进 DoD",
        measure=measure_ac19_02,
        judge=judge_ac19_02,
        breaks=(
            Break(
                "演练档案不在跟踪清单里（写了但没入库）",
                (("drill_tracked", "no"), ("steps", "0"), ("steps_passed", "0")),
                GAP,
            ),
            Break(
                "检查单表头被改写，六步行读不到",
                (("checklist_found", "no"), ("steps", "0"), ("steps_passed", "0")),
                GAP,
            ),
            Break(
                "档案是本轮新写的：一轮不能给自己的演练记录背书（C62 的机读回声）",
                (("drill_preexisting", "no"),),
                GAP,
            ),
            Break(
                "检查单第 3 步（比对关键表行数）没有通过标记",
                (("steps_passed", "5"), ("rowcmp", "no")),
                GAP,
            ),
            Break("只指认得出一个恢复库", (("isolated_dbs", "1"),), GAP),
            Break("恢复后 /health 不再读得到 status=healthy", (("health", "no"),), GAP),
            Break("A0 DoD 不再点这份档案（纳入 DoD 那半掉了）", (("in_a0_dod", "no"),), GAP),
        ),
        repair={
            "drill_tracked": "yes",
            "checklist_found": "yes",
            "drill_commit": "3f0d1c2b9a44",
            "drill_added": "2026-09-22",
            "drill_preexisting": "yes",
            "steps": "6",
            "steps_passed": "6",
            "isolated_dbs": "2",
            "rowcmp": "yes",
            "health": "yes",
            "in_a0_dod": "yes",
        },
    ),
    Probe(
        item="AC-19|03",
        expects="保留策略声明并实现",
        summary="四类策略 + 四个执行器 + 四个配置键，再加运行时包里的真调用点",
        measure=measure_ac19_03,
        judge=judge_ac19_03,
        breaks=(
            Break("四类策略少一类（差异明细导出被摘掉）", (("policy_kinds", "3"),), GAP),
            Break("四个执行器少一个（缓存 TTL 腿没实现）", (("executors", "3"),), GAP),
            Break("四个配置键少一个", (("config_keys", "3"),), GAP),
            Break(
                "声明/执行器/配置全在位，但运行时四包无人调用（现场形状）",
                (("prod_callers", "0"), ("caller_list", "-")),
                GAP,
            ),
            Break("保留策略模板行被删或改名", (("schedule_row", "no"),), GAP),
            Break("retention kind 不再可执行", (("kind_executable", "no"),), GAP),
            Break("jobs dispatcher 不再转给 retention executor", (("dispatcher", "no"),), GAP),
            Break("默认从 report-only 改成执行清理", (("report_only_default", "no"),), GAP),
            Break(
                "report-only 与显式开启的行为用例变红",
                (
                    (
                        "report_only_test",
                        "test_retention_job_is_report_only_unless_explicitly_enabled=exit=1",
                    ),
                ),
                GAP,
            ),
        ),
        repair={
            "policy_kinds": "4",
            "executors": "4",
            "executor_list": ", ".join(AC19_RETENTION_FUNCS),
            "config_keys": "4",
            "prod_callers": "1",
            "caller_list": "opendata/pipeline/maintenance.py",
            "schedule_row": "yes",
            "kind_executable": "yes",
            "dispatcher": "yes",
            "report_only_default": "yes",
            "report_only_test": "-",
        },
    ),
    Probe(
        item="AC-19|04",
        expects="配额/到期/封禁监控告警生效",
        summary="四类 Key 健康的分类规则 + 到期信号 + 分级落点与带外告警通道",
        measure=measure_ac19_04,
        judge=judge_ac19_04,
        breaks=(
            Break("四类健康类别少一类", (("classes_declared", "3"),), GAP),
            Break(
                "401/403 不再归到 credential-rejected（封禁面失去唯一信号）",
                (("rejected_rule", "no"),),
                GAP,
            ),
            Break(
                "429 不再归到 quota-exhausted（配额面失去唯一信号）", (("quota_rule", "no"),), GAP
            ),
            Break("没有运维登记到期时间的分类能力", (("expiry_signal", "no"),), GAP),
            Break("没有供应商到期的实际证据", (("issuer_expiry_observed", "no"),), GAP),
            Break("没有供应商余量的实际证据", (("quota_remaining_observed", "no"),), GAP),
            Break("通知通道只有 mock 没有实际送达", (("delivery_observed", "no"),), GAP),
            Break(
                "没有任何运行时模块调用 credential_health，分级无处可读",
                (("alert_sites", "0"), ("alert_list", "-")),
                GAP,
            ),
            Break(
                "分级只进 /health 拉取面，没有带外告警通道（现场形状）",
                (("notify_sites", "0"), ("notify_list", "-")),
                GAP,
            ),
            Break(
                "模块删掉「没有源发布到期/余量/吊销状态」那句自述，探测却仍未做",
                (("disclosure", "no"),),
                GAP,
            ),
            Break(
                "过期 observation 被当成成功恢复，或 TTL 用例变红",
                (
                    (
                        "observation_bad",
                        "test_ttl_expiry_is_unknown_and_is_not_a_confirmed_recovery=exit=1",
                    ),
                ),
                GAP,
            ),
            Break(
                "通知把 observation 过期伪装成 key 恢复",
                (
                    (
                        "observation_bad",
                        "test_ttl_expiry_event_is_not_labeled_as_recovery=exit=1",
                    ),
                ),
                GAP,
            ),
            Break(
                "未测 quota 被伪装成数值",
                (
                    (
                        "observation_bad",
                        "test_unchanged_alert_is_not_sent_twice_and_quota_stays_unknown=exit=1",
                    ),
                ),
                GAP,
            ),
        ),
        repair={
            "classes_declared": "4",
            "rejected_rule": "yes",
            "quota_rule": "yes",
            "expiry_signal": "yes",
            "issuer_expiry_observed": "yes",
            "quota_remaining_observed": "yes",
            "delivery_observed": "yes",
            "presence_only": "yes",
            "disclosure": "yes",
            "alert_sites": "2",
            "alert_list": "opendata/api/pipeline.py, opendata/pipeline/key_health_notifications.py",
            "notify_sites": "1",
            "notify_list": "opendata/pipeline/key_health_notifications.py",
            "observation_runs": "3",
            "observation_passed": "3",
            "observation_bad": "-",
        },
    ),
    Probe(
        item="AC-19|05",
        expects="《配置项清单》文档存在",
        summary="清单逐行四列覆盖数 + 生效方式承载形态 + 全局条款与代码事实是否自洽",
        measure=measure_ac19_05,
        judge=judge_ac19_05,
        breaks=(
            Break(
                "清单文档不在跟踪清单里",
                (("doc_tracked", "no"), ("live_rows", "0"), ("rows_complete", "0")),
                GAP,
            ),
            Break("有行没列：四列齐的少于在用键名行总数", (("rows_complete", "53"),), GAP),
            Break(
                "逐行生效方式列没有，全局条款也被摘掉",
                (("effect_col_headers", "0"), ("global_clause", "no")),
                GAP,
            ),
            Break(
                "get_settings 不再缓存（逐请求重读）却仍无一行标注热加载",
                (("settings_cached", "no"), ("hotload_marked", "0")),
                GAP,
            ),
        ),
        repair={
            "doc_tracked": "yes",
            "live_rows": "54",
            "rows_complete": "54",
            "removed_rows": "1",
            "effect_col_headers": "0",
            "global_clause": "yes",
            "hotload_marked": "0",
            "settings_cached": "yes",
            "percall_reads": "4",
            "percall_list": "opendata/data/providers/fred/models/_client.py",
        },
    ),
    Probe(
        item="AC-8|04",
        expects="写入基准测试",
        summary=(
            "C65 frozen-source full daily-write run with rows/pages/time/RSS bound across raw, "
            "matrix, and driver"
        ),
        measure=_benchmark_facts,
        judge=judge_ac8_04,
        breaks=(
            Break(
                "benchmark evidence has a validation issue",
                (("benchmark_valid", "no"), ("benchmark_issue_count", "1")),
                GAP,
            ),
            Break(
                "source identity is no longer verified", (("source_identity_verified", "no"),), GAP
            ),
            Break("full raw record is incomplete", (("full_raw_complete", "no"),), GAP),
            Break(
                "full requested/read/written/total-source row counts no longer agree",
                (("full_rows_equal", "no"), ("full_source_rows_match", "no")),
                GAP,
            ),
            Break("full page totals no longer agree", (("full_pages_equal", "no"),), GAP),
            Break(
                "full run is missing a measured time or RSS value",
                (("full_peak_rss_positive", "no"),),
                GAP,
            ),
        ),
        repair={
            "benchmark_valid": "yes",
            "benchmark_issue_count": "0",
            "source_identity_verified": "yes",
            "full_raw_complete": "yes",
            "full_source_sha256_bound": "yes",
            "full_body_final_identity": "yes",
            "full_status": "complete",
            "full_rows_equal": "yes",
            "full_source_rows_match": "yes",
            "full_pages_equal": "yes",
            "full_target_rows_equal": "yes",
            "full_elapsed_positive": "yes",
            "full_current_rss_positive": "yes",
            "full_peak_rss_positive": "yes",
            "full_source": "ths",
            "full_domain": "stock_daily",
            "full_table": "ods_stock_daily_ths",
        },
    ),
    Probe(
        item="§5|01",
        expects="写入基准",
        summary=(
            "Three frozen-source write scales with complete rows/pages/time/RSS and an observed "
            "full-to-1m peak comparison"
        ),
        measure=_benchmark_facts,
        judge=judge_section5_01,
        breaks=(
            Break(
                "benchmark evidence or source validation is invalid",
                (("benchmark_valid", "no"),),
                GAP,
            ),
            Break(
                "fewer than all three scale runs are present",
                (("benchmark_scales_present", "no"),),
                GAP,
            ),
            Break(
                "scale count no longer contains exactly three runs",
                (("benchmark_scale_count", "2"),),
                GAP,
            ),
            Break("at least one scale record is incomplete", (("all_scales_complete", "no"),), GAP),
            Break("full peak RSS exceeds the 1m peak", (("full_peak_le_1m", "no"),), GAP),
        ),
        repair={
            "benchmark_valid": "yes",
            "benchmark_issue_count": "0",
            "source_identity_verified": "yes",
            "benchmark_scale_count": "3",
            "benchmark_scales_present": "yes",
            "all_scales_complete": "yes",
            "full_peak_le_1m": "yes",
        },
    ),
    Probe(
        item="AC-10|02",
        expects="消费场景 + 请求方",
        summary=(
            "Direct requester confirmation reconciled against every current active "
            "provider/domain pair"
        ),
        measure=measure_ac10_02,
        judge=judge_ac10_02,
        breaks=(
            Break(
                "confirmation pair set misses or adds an active mapping leg",
                (("pair_set_equal", "no"), ("missing_pairs", "1"), ("extra_pairs", "1")),
                GAP,
            ),
            Break(
                "the confirmation repeats a provider/domain pair",
                (("evidence_duplicate_pairs", "1"), ("pair_set_equal", "no")),
                GAP,
            ),
            Break("the direct human reply is absent", (("direct_human_confirmation", "no"),), GAP),
            Break("one active leg has no scenario", (("scenario_missing", "1"),), GAP),
            Break(
                "one active leg omits its requester or purpose",
                (("requester_missing", "1"), ("purpose_missing", "1")),
                GAP,
            ),
            Break("continued activation is not confirmed", (("activation_unconfirmed", "1"),), GAP),
        ),
        repair={
            "requester_evidence_valid": "yes",
            "requester_problem_count": "0",
            "direct_human_confirmation": "yes",
            "direct_reply_matches": "yes",
            "reply_declared_count": "33",
            "requester_source_map_matches": "yes",
            "active_map_leg_count": "33",
            "active_map_unique_pairs": "33",
            "active_provider_count": "7",
            "map_duplicate_pairs": "0",
            "evidence_leg_count": "33",
            "evidence_unique_pairs": "33",
            "evidence_duplicate_pairs": "0",
            "missing_pairs": "0",
            "extra_pairs": "0",
            "pair_set_equal": "yes",
            "declared_count_matches": "yes",
            "declared_provider_count_matches": "yes",
            "scenario_missing": "0",
            "scenario_mismatch": "0",
            "requester_missing": "0",
            "requester_mismatch": "0",
            "purpose_missing": "0",
            "purpose_mismatch": "0",
            "activation_unconfirmed": "0",
            "proof_scope": "requester/purpose/activation only; not copyright/source/field mapping",
        },
    ),
    Probe(
        item="AC-10|03",
        expects="每 provider 零 OpenBB 源码",
        summary=(
            "Current registered provider sources have a complete, user-authorized, hash-bound "
            "review against the pinned OpenBB baseline"
        ),
        measure=measure_ac10_03,
        judge=judge_ac10_03,
        breaks=(
            Break(
                "current source SHA inventory is missing or no longer matches the review",
                (
                    ("current_source_file_count", "70"),
                    ("source_hash_entry_count", "70"),
                    ("source_faces_match_registry", "no"),
                    ("current_source_hashes_bound", "no"),
                ),
                GAP,
            ),
            Break(
                "a provider package is registered on disk with no review row behind it",
                (
                    ("providers_missing_from_review", "sec"),
                    ("review_population_matches_registry", "no"),
                ),
                GAP,
            ),
            Break(
                "a provider source file appears in the census after the bundle was built",
                (
                    ("registry_files_outside_review", "1"),
                    ("review_population_matches_registry", "no"),
                    ("source_faces_match_registry", "no"),
                ),
                GAP,
            ),
            Break(
                "the similarity artifact compares an OpenBB provider set other than the pinned one",
                (("baseline_scope_matches_pinned", "no"),),
                GAP,
            ),
            Break(
                "an OpenBB source import appears in a provider package",
                (("openbb_import_count", "1"), ("zero_openbb_imports", "no")),
                GAP,
            ),
            Break(
                "the current review lacks direct user authorization",
                (("reviewer_authorized", "no"),),
                GAP,
            ),
            Break(
                "a new source candidate has not passed the review",
                (("candidate_count", "1"), ("unreviewed_candidates", "1")),
                GAP,
            ),
            Break(
                "a nearest-pair review reports shared complex implementation",
                (("nearest_positive_result", "no"),),
                GAP,
            ),
            Break(
                "the evidence validator reports a missing or stale binding",
                (("provider_review_valid", "no"), ("provider_review_issue_count", "1")),
                GAP,
            ),
        ),
        repair={
            "provider_review_valid": "yes",
            "provider_review_issue_count": "0",
            "provider_review_issue_codes": "-",
            "provider_scope": "current_registered_provider_packages",
            "provider_names": "*registry_provider_names",
            "reviewed_provider_names": "*registry_provider_names",
            "providers_missing_from_review": "-",
            "reviewed_providers_not_registered": "-",
            "per_package_file_count_mismatches": "-",
            "registry_files_outside_review": "0",
            "review_files_outside_registry": "0",
            "review_population_matches_registry": "yes",
            "current_source_file_count": "*registry_source_file_count",
            "source_hash_entry_count": "*registry_source_file_count",
            "reviewed_source_file_count": "*registry_source_file_count",
            "similarity_source_file_count": "*registry_source_file_count",
            "source_faces_match_registry": "yes",
            "current_source_hashes_bound": "yes",
            "provider_sources_parsed": "yes",
            "openbb_import_count": "0",
            "zero_openbb_imports": "yes",
            "reviewer_authorized": "yes",
            "openbb_baseline_commit": "*openbb_baseline_expected",
            "baseline_provider_names": "*baseline_providers_pinned",
            "baseline_scope_matches_pinned": "yes",
            "candidate_count": "0",
            "unreviewed_candidates": "0",
            "package_reviews_complete": "yes",
            "nearest_review_complete": "yes",
            "review_complete": "yes",
            "nearest_review_count": "3",
            "nearest_positive_result": "yes",
            "artifact_sha_bindings_match": "yes",
        },
    ),
    Probe(
        item="AC-6|02",
        expects="子模块抽样 ≥20% 对照通过",
        summary=(
            "Offline recorded-fixture compare PASS measured against the validated 9-group "
            "B1.1 module denominator"
        ),
        measure=measure_ac6_02,
        judge=judge_ac6_02,
        breaks=(
            Break(
                "the port scope or its B1.1 group proof is invalid",
                (("port_scope_valid", "no"),),
                GAP,
            ),
            Break(
                "a compare case no longer maps to a validated source path and batch",
                (("case_path_mapping_valid", "no"),),
                GAP,
            ),
            Break("any compare case fails", (("case_fail_count", "1"),), GAP),
            Break("PASS groups fall below ceil(9 × 20%)", (("passing_groups", "1"),), GAP),
            Break("the compare tolerance changes", (("compare_rtol", "1e-06"),), GAP),
            Break("PENDING cases are no longer excluded", (("pending_excluded", "no"),), GAP),
        ),
        repair={
            "port_scope_valid": "yes",
            "port_scope_problem_summary": "-",
            "b1_scope_groups_valid": "yes",
            "b1_scope_group_count": "9",
            "b1_scope_group_names": AC6_B1_1_GROUP_NAMES,
            "b1_python_file_count": "245",
            "case_path_mapping_valid": "yes",
            "case_path_problem_count": "0",
            "case_path_problem_summary": "-",
            "case_count": "18",
            "case_statuses_complete": "yes",
            # token门禁状态枚举非凭据：这是 PASS 比较结果数量。
            "case_pass_count": "14",  # nosec B105  # face name for a case count, not a secret
            "case_fail_count": "0",
            "case_pending_count": "4",
            "pending_excluded": "yes",
            "compare_exit_code": "1",
            "compare_exit_consistent": "yes",
            "compare_report_temporary": "yes",
            "compare_rtol": "1e-09",
            "d10_failed": "no",
            "passing_groups": "6",
            "passing_group_names": "bond, fund, futures, index, option, stock_fundamental",
            "required_groups": "2",
            "group_coverage_percent": "66.67",
            # token门禁状态枚举非凭据：这是通过的源路径数量。
            "pass_files": "7",  # nosec B105  # face name for a file count, not a secret
            "file_coverage_percent": "2.86",
            "pending_case_names": (
                "stock_daily_raw, stock_daily_qfq, index_daily_em, fund_etf_daily_em"
            ),
            "fail_case_names": "-",
            "sample_scope_note": "module-group denominator; file percentage is disclosed only",
        },
    ),
    Probe(
        item="AC-17|04",
        expects="不再被全局忽略",
        summary="三条能重新忽略 F821 的路都空着：配置值、ruff 命令行、逐行 noqa",
        measure=measure_ac17_04,
        judge=judge_ac17_04,
        breaks=(
            Break("全局 lint.ignore 放回 F821", (("ignore_has_f821", "yes"),), GAP),
            Break("某条 per-file-ignores 点名 F821", (("per_file_f821", "1"),), GAP),
            Break("select 去掉 F 族（连 F821 都不再运行）", (("select_covers_f", "no"),), GAP),
            Break("门禁命令行 --ignore F821", (("cli_f821_suppression", "1"),), GAP),
            Break("源码里出现一行 noqa: F821", (("noqa_f821_sites", "1"),), GAP),
            Break(
                "普查里有读不动的 .py（零违例退化成无读数）",
                (("unreadable_py", "3"), ("noqa_f821_sites", "0")),
                GAP,
            ),
        ),
        repair={
            "select_covers_f": "yes",
            "ignore_has_f821": "no",
            "per_file_f821": "0",
            "cli_f821_suppression": "0",
            "noqa_f821_sites": "0",
            "unreadable_py": "0",
        },
    ),
    Probe(
        item="AC-17|09",
        expects="lint/typecheck/test 纳入门禁",
        summary="前端五项（含采集与 e2e 两个守卫）都在 gate 配方里且各自调用点名的工具",
        measure=measure_ac17_09,
        judge=judge_ac17_09,
        breaks=(
            Break(
                "把 frontend-e2e 从 gate 摘掉",
                (("in_gate", "4"), ("missing", "frontend-e2e")),
                GAP,
            ),
            Break("typecheck 配方被清空", (("empty_recipe", "1"),), GAP),
            Break("vitest 换成 echo", (("tools_all", "no"), ("tool_vitest", "no")), GAP),
            Break("lint 加上 --fix（门禁改源码）", (("check_only", "no"),), GAP),
            Break("判据原文不再点名 test", (("doc_named_count", "2"),), GAP),
        ),
        repair={
            "in_gate": "5",
            "missing": "-",
            "empty_recipe": "0",
            "tools_all": "yes",
            "doc_named_count": "3",
            "check_only": "yes",
        },
    ),
    Probe(
        item="§4|03",
        expects="浮点阈值用例",
        summary="RTOL 仍是 1e-9、只作用于真浮点、放过的差异进 notes，且有放宽就红的变异控制",
        measure=measure_s4_03,
        judge=judge_s4_03,
        breaks=(
            Break("M2 变异：容忍度放宽到 1e-2", (("rtol_literal", "1e-2"),), GAP),
            Break("RTOL 常量被删", (("rtol_literal", "(absent)"),), GAP),
            Break("isclose 不再传 rtol（等于全局默认）", (("isclose_sites", "0"),), GAP),
            Break("浮点闸门拆掉（文本格也走容忍度）", (("float_guard", "no"),), GAP),
            Break("放过的差异不再记条数", (("note_reports_count", "no"),), GAP),
            Break("删掉变异控制用例", (("mutation_control", "no"),), GAP),
            Break("删掉「静默放过」控制用例", (("silent_drift_control", "no"),), GAP),
            Break("阈值用例只剩 3 条", (("tolerance_tests", "3"),), GAP),
        ),
        repair={
            "rtol_literal": "1e-9",
            "isclose_sites": "2",
            "float_guard": "yes",
            "note_reports_count": "yes",
            "tolerance_tests": "5",
            "mutation_control": "yes",
            "silent_drift_control": "yes",
        },
    ),
    Probe(
        item="AC-16|05",
        expects="零依赖断言（分层 AST 口径）",
        summary="入库 scope/普查/版本与今日走查逐项相等，门禁先自测再全量且两面齐全",
        measure=measure_ac16_05,
        judge=judge_ac16_05,
        breaks=(
            Break("新增一个扫描根但不入库", (("surface_problems", "1"),), GAP),
            Break("某个根的文件数漂了", (("per_scope_mismatch", "1"),), GAP),
            Break("扫描器升版没重发普查", (("version_matches", "no"),), GAP),
            Break(
                "只保留 --self-test 一次调用",
                (("invocations", "1"), ("full_run_after", "no")),
                GAP,
            ),
            Break("自测的违例样本被删空", (("violation_samples", "0"),), GAP),
            Break("自测没有合规样本（防不了误报那一面）", (("compliant_samples", "0"),), GAP),
            Break(
                "违例样本只剩 import 一种入口",
                (("violation_kinds", "import"), ("violation_kinds_full", "no")),
                GAP,
            ),
            Break("扫描器自测真跑一次跑红", (("selftest_exit", "1"),), GAP),
            Break("工具报的样本计数与结构不符", (("reported_counts_match", "no"),), GAP),
            Break("入库 findings 变成 2", (("recorded_findings", "2"),), GAP),
            Break("入库 scope 被清空", (("scope_entries", "0"),), GAP),
            Break(
                "self-test 不再是第一条",
                (("self_test_first", "no"), ("invocations", "2"), ("full_run_after", "yes")),
                GAP,
            ),
            Break("入库 JSON 的 files 与载入结果不同源", (("loader_files_match", "no"),), GAP),
            Break("走查里少了一个根（扫描面静默缩水）", (("census_missing_roots", "1"),), GAP),
        ),
        repair={
            "scope_entries": "3",
            "census_missing_roots": "0",
            "per_scope_mismatch": "0",
            "surface_problems": "0",
            "loader_scope_matches": "yes",
            "loader_files_match": "yes",
            "loader_minors_match": "yes",
            "version_matches": "yes",
            "recorded_findings": "0",
            "invocations": "2",
            "self_test_first": "yes",
            "full_run_after": "yes",
            "selftest_exit": "0",
            "violation_samples": "13",
            "compliant_samples": "4",
            "violation_kinds": "dynamic, import, string",
            "violation_kinds_full": "yes",
            "reported_counts_match": "yes",
        },
    ),
    AC16_09,
    AC17_02,
    AC17_06,
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
            group = None if section.group(1) == "10" else f"§{section.group(1)}"
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
            try:
                mutated = resolve_break(clean, brk.facts)
            except ValueError as exc:
                failures.append(f"{probe.item} / {brk.label}: invalid counterfact: {exc}")
                continue
            applied += 1
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
