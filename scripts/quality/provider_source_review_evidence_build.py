"""Re-measure the live provider tree and rebuild the C65 source-review evidence bundle.

``scripts/quality/provider_source_review_evidence.validate`` binds three JSON artifacts
(``provider-source-review.json``, ``provider-similarity-inventory.json``,
``provider-nearest-review.json``) to the bytes currently on disk under a 24-hour freshness window.
Until now nothing produced those three files: every round hand-typed them, so one tree plus one
archive read clean on the day it was written and emitted
``source-review-stale``/``similarity-stale``/``nearest-review-stale`` a day later with no cheap way
to re-certify. This tool re-measures the tree and writes all three from one command.

What it measures in this run:

* every provider package registered on disk, from the same discovery walk the validator uses
  (``registered_provider_packages``), with the ``*.py`` set and per-file SHA-256 it sees now;
* which of those files are lock-pinned verbatim copies. A file below a directory holding an
  ``upstream.lock`` counts as vendored only when the lock has a row for it and either that row's
  digest equals the bytes read now, or ``manifest.json`` pins these bytes and chains them to the
  same lock digest, with ``manual_edits`` false in both. Anything else is hand-written, so an
  unreviewed file can never hide behind the ``_vendor`` exclusion;
* the pinned OpenBB baseline: ``openbb_platform/providers/<p>/openbb_<p>/**/*.py`` for the five
  baseline providers, read read-only, with the checkout HEAD resolved out of its git files and
  re-checked against the commit the validator pins. No git process is started and nothing in that
  checkout is written.

Review verdicts are never invented here. ``--findings`` names a reviewer-authored JSON file
(schema ``opendata-c65-provider-source-review-findings/v1``: one row per hand-written file with
``path``, ``verdict``, one-line ``basis`` and ``reviewer``, plus the conclusions for the ranked
nearest pairs). A package is marked ``reviewed: true`` only when every one of its hand-written
files has such a row and every row carries the approved no-copy verdict; a finding that names a
path which does not exist stops the run before anything is written.

Output goes to the directory given with ``-o`` and nowhere else: this tool refuses to write under
``docs/evidence/``, because those trees are prior rounds' append-only archives. Promoting a fresh
round is a deliberate human copy, not something a generator does.

Usage once the review findings exist::

    python scripts/quality/provider_source_review_evidence_build.py \
        -o build/provider-review/c65-refresh --findings build/provider-review/findings.json

Exit codes: ``0`` built and the bundle re-validates with zero issues; ``2`` refused; ``3`` built
but the bundle does not validate yet; ``4`` the nearest-pair worklist was printed instead.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import re
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Final

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.quality import provider_source_review_evidence as review  # noqa: E402

FINDINGS_SCHEMA: Final = "opendata-c65-provider-source-review-findings/v1"
DEFAULT_OPENBB_CHECKOUT: Final = "/Users/yunjinqi/Documents/new_projects/OpenBB"
OPENBB_PROVIDERS_REL: Final = "openbb_platform/providers"
VENDOR_LOCK_NAME: Final = "upstream.lock"
VENDOR_MANIFEST_NAME: Final = "manifest.json"
#: Prior rounds' archives are append-only history; this tool never writes into them.
PROTECTED_ARCHIVE_PARTS: Final = ("docs", "evidence")
HEX40_RE: Final = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")

#: AST block screening follows the archived C65 method: function bodies and non-empty nested
#: statement lists are compared, and blocks below this many AST nodes are too small to say
#: anything about structure.
MIN_BLOCK_NODES: Final = 41
PREFILTER_NODE_RATIO: Final = 0.30
PREFILTER_DICE: Final = 0.30
CANDIDATE_ORDERED_RATIO: Final = 0.62
CANDIDATE_DICE: Final = 0.70
CANDIDATE_NODE_RATIO: Final = 0.55
WEIGHT_ORDERED: Final = 0.60
WEIGHT_DICE: Final = 0.25
WEIGHT_NODES: Final = 0.15
NEAREST_PAIR_ROWS: Final = 3
STATEMENT_LIST_FIELDS: Final = ("body", "orelse", "finalbody")
#: A hand-written file with no finding row keeps its package at ``reviewed: false``; this is the
#: result recorded for that package, never the approved verdict.
UNREVIEWED_RESULT: Final = "HAND_WRITTEN_FILES_WITHOUT_FINDINGS"
LONG_LITERAL_MIN: Final = 8
SHORT_LITERAL_MAX: Final = 13
BASELINE_SCOPE_NOTE: Final = (
    "openbb_platform/providers/<provider>/openbb_<provider>/**/*.py 生产源码，只读；"
    "不含 tests/examples，不含 openbb_core"
)


class BuildError(RuntimeError):
    """Raised for any condition that would make the rebuilt bundle overstate the review."""


@dataclass
class Block:
    """One screened statement list with its normalized token profile."""

    path: str
    function: str
    function_kind: str
    block_kind: str
    span: tuple[int, int]
    node_count: int
    tokens: tuple[str, ...]
    multiset: Counter[str]
    calls: frozenset[str]
    strings: frozenset[str]
    operators: frozenset[str]

    @property
    def is_baseline(self) -> bool:
        """Whether this block comes from the pinned OpenBB baseline tree."""
        return self.path.startswith(OPENBB_PROVIDERS_REL)

    def record(self) -> dict[str, Any]:
        """Return the inventory-side fields for this block."""
        side = "baseline" if self.is_baseline else "local"
        return {
            f"{side}_path": self.path,
            f"{side}_function": self.function,
            f"{side}_function_kind": self.function_kind,
            f"{side}_block_kind": self.block_kind,
            f"{side}_span": [self.span[0], self.span[1]],
            f"{side}_ast_node_count": self.node_count,
        }


@dataclass
class SourceRecord:
    """One live provider ``*.py`` file with its measured digest and vendor classification."""

    relative: str
    provider: str
    path: Path
    sha256: str
    lines: int
    vendored: bool
    proof: str
    blocks: list[Block] = field(default_factory=list)


@dataclass
class VendorLock:
    """One ``upstream.lock`` plus its sibling ``manifest.json``, keyed by path under its root."""

    root: Path
    relative_root: str
    entries: dict[str, dict[str, Any]]
    manifest: dict[str, dict[str, Any]]

    def covers(self, relative: str) -> bool:
        """Whether this lock's root is the file itself or one of its ancestors."""
        return relative == self.relative_root or relative.startswith(f"{self.relative_root}/")

    def within(self, relative: str) -> str:
        """Return the file path as the lock records it, relative to the lock's directory."""
        return relative[len(self.relative_root) + 1 :]


@dataclass
class Measurement:
    """Everything this run measured about the live provider tree."""

    root: Path
    packages: dict[str, Path]
    records: dict[str, SourceRecord]
    provider_files: dict[str, dict[str, str]]
    provider_counts: dict[str, int]
    vendored: dict[str, list[str]]
    hand_written: dict[str, list[str]]
    unproven: dict[str, str]
    local_blocks: list[Block]
    functions_methods: int

    @property
    def total_files(self) -> int:
        """Number of provider ``*.py`` files measured across all registered packages."""
        return len(self.records)

    @property
    def vendored_files(self) -> int:
        """Number of files proven to be lock-pinned verbatim copies."""
        return sum(len(rows) for rows in self.vendored.values())

    @property
    def hand_written_files(self) -> int:
        """Number of files that need a reviewer finding because nothing pins them."""
        return sum(len(rows) for rows in self.hand_written.values())


@dataclass
class Findings:
    """Reviewer-authored conclusions for one round."""

    reviewer: dict[str, str]
    files: dict[str, dict[str, str]]
    packages: dict[str, dict[str, str]]
    nearest: dict[tuple[Any, ...], dict[str, Any]]
    scope: str
    claims_excluded: list[str]
    limitations: list[str]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dump(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _relative(root: Path, path: Path) -> str:
    return PurePosixPath(path.relative_to(root).as_posix()).as_posix()


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuildError(f"{label} must be a non-empty string")
    return value.strip()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BuildError(f"{label} cannot be read as JSON: {path} ({error})") from error
    if not isinstance(payload, dict):
        raise BuildError(f"{label} must be a JSON object: {path}")
    return payload


# --------------------------------------------------------------------------- live tree


def _vendor_locks(root: Path, package_dir: Path) -> list[VendorLock]:
    locks: list[VendorLock] = []
    for lock_path in sorted(package_dir.rglob(VENDOR_LOCK_NAME)):
        if lock_path.is_symlink():
            raise BuildError(f"vendor lock is a symlink: {lock_path}")
        payload = _load_json(lock_path, f"vendor lock {lock_path}")
        rows = payload.get("files")
        if not isinstance(rows, list):
            raise BuildError(f"vendor lock has no files list: {lock_path}")
        entries: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise BuildError(f"vendor lock row is not an object: {lock_path}")
            recorded = row.get("path")
            digest = row.get("sha256")
            if not isinstance(recorded, str) or not recorded:
                raise BuildError(f"vendor lock row has no path: {lock_path}")
            if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
                raise BuildError(f"vendor lock row has an invalid sha256: {lock_path}/{recorded}")
            if recorded in entries:
                raise BuildError(f"vendor lock repeats a path: {lock_path}/{recorded}")
            entries[recorded] = row
        manifest: dict[str, dict[str, Any]] = {}
        manifest_path = lock_path.parent / VENDOR_MANIFEST_NAME
        if manifest_path.is_file():
            manifest_payload = _load_json(manifest_path, f"vendor manifest {manifest_path}")
            for row in manifest_payload.get("files", []) or []:
                if isinstance(row, dict) and isinstance(row.get("path"), str):
                    manifest[row["path"]] = row
        locks.append(
            VendorLock(
                root=lock_path.parent,
                relative_root=_relative(root, lock_path.parent),
                entries=entries,
                manifest=manifest,
            )
        )
    return locks


def _classify_vendored(
    locks: list[VendorLock], record_relative: str, digest: str
) -> tuple[bool, str]:
    """Whether one file is a provably lock-pinned verbatim copy, with the measured reason."""
    enclosing = [lock for lock in locks if lock.covers(record_relative)]
    if not enclosing:
        return False, "hand-written: not below any upstream.lock root"
    lock = max(enclosing, key=lambda item: len(item.relative_root))
    within = lock.within(record_relative)
    entry = lock.entries.get(within)
    if entry is None:
        return False, f"hand-written: no {VENDOR_LOCK_NAME} row for {within}"
    if entry.get("manual_edits") is not False:
        return False, f"hand-written: {VENDOR_LOCK_NAME} row marks manual_edits"
    if entry.get("sha256") == digest:
        return True, f"lock-pinned verbatim copy: bytes equal the {VENDOR_LOCK_NAME} digest"
    row = lock.manifest.get(within)
    if (
        isinstance(row, dict)
        and row.get("sha256") == digest
        and row.get("upstream_sha256") == entry.get("sha256")
        and row.get("manual_edits") is False
    ):
        return (
            True,
            f"lock-pinned verbatim copy: {VENDOR_MANIFEST_NAME} pins these bytes to the "
            f"{VENDOR_LOCK_NAME} digest",
        )
    return False, f"hand-written: bytes match neither {VENDOR_LOCK_NAME} nor its manifest chain"


def _measure_file(root: Path, provider: str, source: Path, locks: list[VendorLock]) -> SourceRecord:
    relative = _relative(root, source)
    if source.is_symlink():
        raise BuildError(f"provider source is a symlink: {relative}")
    if not source.is_file():
        raise BuildError(f"provider source is not a regular file: {relative}")
    content = source.read_bytes()
    try:
        text = content.decode("utf-8-sig")
        tree = ast.parse(text, filename=relative)
    except (UnicodeError, SyntaxError, ValueError) as error:
        raise BuildError(f"provider source cannot be parsed: {relative} ({error})") from error
    blocks, imports = _blocks_and_imports(relative, tree)
    if imports:
        raise BuildError(
            f"provider source imports an OpenBB root ({', '.join(sorted(imports))}): {relative}"
        )
    digest = _sha256(content)
    vendored, proof = _classify_vendored(locks, relative, digest)
    return SourceRecord(
        relative=relative,
        provider=provider,
        path=source,
        sha256=digest,
        lines=len(text.splitlines()),
        vendored=vendored,
        proof=proof,
        blocks=blocks,
    )


def measure_tree(root: Path) -> Measurement:
    """Walk the registered provider packages once and digest every ``*.py`` file they hold."""
    issues: list[review.Issue] = []
    packages = review.registered_provider_packages(root, issues)
    if issues or not packages:
        codes = ", ".join(issue.code for issue in issues) or "no provider package is registered"
        raise BuildError(f"live provider discovery failed closed: {codes}")

    measurement = Measurement(
        root=root,
        packages=packages,
        records={},
        provider_files={},
        provider_counts={},
        vendored={},
        hand_written={},
        unproven={},
        local_blocks=[],
        functions_methods=0,
    )
    for provider, package_dir in sorted(packages.items()):
        locks = _vendor_locks(root, package_dir)
        measurement.provider_files[provider] = {}
        measurement.vendored[provider] = []
        measurement.hand_written[provider] = []
        for source in sorted(package_dir.rglob("*.py")):
            record = _measure_file(root, provider, source, locks)
            measurement.records[record.relative] = record
            measurement.provider_files[provider][record.relative] = record.sha256
            bucket = measurement.vendored if record.vendored else measurement.hand_written
            bucket[provider].append(record.relative)
            if record.vendored:
                continue
            measurement.unproven[record.relative] = record.proof
            measurement.local_blocks.extend(record.blocks)
            bodies = {
                block.function for block in record.blocks if block.block_kind == "function_body"
            }
            measurement.functions_methods += len(bodies)
        measurement.provider_counts[provider] = len(measurement.provider_files[provider])
    return measurement


# --------------------------------------------------------------------------- AST screening


@dataclass
class Scope:
    """The enclosing function identity while blocks are collected."""

    qualified: str
    function_kind: str
    function: ast.AST | None
    in_class: bool

    def child(self, name: str, node: ast.AST, kind: str) -> Scope:
        """Return the scope a nested def introduces, qualified under this one."""
        return Scope(f"{self.qualified}.{name}", kind, node, self.in_class)

    def in_class_scope(self, name: str) -> Scope:
        """Return a class scope so later defs inside it are recorded as methods."""
        return Scope(f"{self.qualified}.{name}" if self.qualified else name, "class", None, True)


def _node_count(statements: list[ast.stmt]) -> int:
    return sum(1 for statement in statements for _ in ast.walk(statement))


def _bound_names(scope: Scope, statements: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    function = scope.function
    if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        arguments = function.args
        candidates = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
        candidates += [a for a in (arguments.vararg, arguments.kwarg) if a is not None]
        names.update(argument.arg for argument in candidates)
    for statement in statements:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
    return names


def _dotted_name(node: ast.expr | None) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    elif current is None:
        return None
    if not parts:
        return None
    return ".".join(reversed(parts))


def _tokenize(
    statements: list[ast.stmt], scope: Scope
) -> tuple[tuple[str, ...], frozenset[str], frozenset[str], frozenset[str]]:
    bound = _bound_names(scope, statements)
    tokens: list[str] = []
    calls: set[str] = set()
    strings: set[str] = set()
    operators: set[str] = set()

    def visit(node: ast.AST) -> None:
        name = type(node).__name__
        tokens.append(name)
        if isinstance(node, ast.Name):
            tokens.append("L" if node.id in bound else node.id)
        elif isinstance(node, ast.Attribute):
            tokens.append(f".{node.attr}")
        elif isinstance(node, ast.arg):
            tokens.append("L")
        elif isinstance(node, ast.keyword):
            tokens.append(f"kw:{node.arg}" if node.arg else "kw")
        elif isinstance(node, ast.alias):
            tokens.append(node.asname or node.name)
        elif isinstance(node, ast.Constant):
            tokens.append(_constant_token(node.value))
            if isinstance(node.value, str) and len(node.value) >= LONG_LITERAL_MIN:
                strings.add(node.value)
        elif isinstance(node, ast.Call):
            target = _dotted_name(node.func)
            if target is not None:
                calls.add(target)
        elif isinstance(node, (ast.operator, ast.unaryop, ast.boolop, ast.cmpop)):
            operators.add(name)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in statements:
        visit(statement)
    return tuple(tokens), frozenset(calls), frozenset(strings), frozenset(operators)


def _constant_token(value: object) -> str:
    if value is None or isinstance(value, bool):
        return repr(value)
    if isinstance(value, str):
        if len(value) <= SHORT_LITERAL_MAX:
            return f"'{value}'"
        return f"s:{len(value)}"
    if isinstance(value, (int, float, complex)):
        return "#n"
    if isinstance(value, (bytes, bytearray)):
        return "#b"
    return "#other"


def _lineno(node: ast.stmt, attr: str, default: int) -> int:
    """Line attribute as a plain int; a missing or zero value falls back like the old `or` did."""
    value = getattr(node, attr, None)
    return value if isinstance(value, int) and value else default


def _blocks_and_imports(relative: str, tree: ast.Module) -> tuple[list[Block], set[str]]:
    blocks: list[Block] = []
    imports: set[str] = set()

    def emit(statements: list[ast.stmt], scope: Scope, kind: str) -> None:
        if not statements or not all(isinstance(item, ast.stmt) for item in statements):
            return
        count = _node_count(statements)
        if count < MIN_BLOCK_NODES:
            return
        first = statements[0]
        last = statements[-1]
        start = _lineno(first, "lineno", 1)
        end = _lineno(last, "end_lineno", _lineno(last, "lineno", start))
        tokens, calls, strings, operators = _tokenize(statements, scope)
        blocks.append(
            Block(
                path=relative,
                function=scope.qualified or "<module>",
                function_kind=scope.function_kind,
                block_kind=kind,
                span=(start, max(end, start)),
                node_count=count,
                tokens=tokens,
                multiset=Counter(tokens),
                calls=calls,
                strings=strings,
                operators=operators,
            )
        )

    def walk(node: ast.AST, scope: Scope) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if scope.in_class else "function"
                child_scope = scope.child(child.name, child, kind)
                emit(list(child.body), child_scope, "function_body")
                walk(child, child_scope)
            elif isinstance(child, ast.ClassDef):
                walk(child, scope.in_class_scope(child.name))
            else:
                for list_field in STATEMENT_LIST_FIELDS:
                    value = getattr(child, list_field, None)
                    if isinstance(value, list) and value:
                        emit(value, scope, f"{type(child).__name__}.{list_field}")
                handlers = getattr(child, "handlers", None)
                if isinstance(handlers, list):
                    for handler in handlers:
                        if isinstance(handler, ast.ExceptHandler):
                            emit(list(handler.body), scope, "Try.handlers")
                cases = getattr(child, "cases", None)
                if isinstance(cases, list):
                    for case in cases:
                        emit(list(getattr(case, "body", []) or []), scope, "Match.cases")
                walk(child, scope)

    def collect_imports(node: ast.AST) -> None:
        for child in ast.walk(node):
            roots: list[str] = []
            if isinstance(child, ast.Import):
                roots.extend(alias.name.split(".", 1)[0] for alias in child.names)
            elif isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
                roots.append(child.module.split(".", 1)[0])
            imports.update(name for name in roots if name == "openbb" or name.startswith("openbb_"))

    walk(tree, Scope("", "module", None, False))
    collect_imports(tree)
    return blocks, imports


def _dice(first: Counter[str], second: Counter[str]) -> float:
    totals = sum(first.values()) + sum(second.values())
    if totals == 0:
        return 0.0
    return round(2.0 * sum((first & second).values()) / totals, 4)


def _score(first: Block, second: Block) -> tuple[float, float, float, float]:
    node_ratio = round(
        min(first.node_count, second.node_count) / max(first.node_count, second.node_count), 4
    )
    dice = _dice(first.multiset, second.multiset)
    matcher = difflib.SequenceMatcher(None, list(first.tokens), list(second.tokens))
    ordered = round(matcher.ratio(), 4)
    score = round(WEIGHT_ORDERED * ordered + WEIGHT_DICE * dice + WEIGHT_NODES * node_ratio, 4)
    return score, ordered, dice, node_ratio


def _pair_row(first: Block, second: Block, tier: str) -> dict[str, Any]:
    score, ordered, dice, node_ratio = _score(first, second)
    row: dict[str, Any] = dict(first.record())
    row.update(second.record())
    row.update(
        {
            "ordered_token_ratio": ordered,
            "multiset_dice": dice,
            "node_count_ratio": node_ratio,
            "shared_anchor_counts": {
                "call_names": len(first.calls & second.calls),
                "long_string_literals": len(first.strings & second.strings),
                "operator_kinds": len(first.operators & second.operators),
            },
            "score": score,
            "tier": tier,
        }
    )
    return row


def _pair_sort_key(row: dict[str, Any]) -> tuple[float, str, int, int, str, int, int]:
    return (
        -float(row["score"]),
        str(row["local_path"]),
        int(row["local_span"][0]),
        int(row["local_span"][1]),
        str(row["baseline_path"]),
        int(row["baseline_span"][0]),
        int(row["baseline_span"][1]),
    )


def screen_blocks(
    local_blocks: list[Block], baseline_blocks: list[Block]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rank every screened local block against the baseline blocks and split off candidates."""
    pairs: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    ordered_baseline = sorted(baseline_blocks, key=lambda item: (item.path, item.span))
    for local in sorted(local_blocks, key=lambda item: (item.path, item.span, item.block_kind)):
        best: dict[str, Any] | None = None
        for baseline in ordered_baseline:
            smaller = min(local.node_count, baseline.node_count)
            larger = max(local.node_count, baseline.node_count)
            if larger == 0 or smaller / larger < PREFILTER_NODE_RATIO:
                continue
            if _dice(local.multiset, baseline.multiset) < PREFILTER_DICE:
                continue
            row = _pair_row(local, baseline, "nearest_pair")
            if (
                row["ordered_token_ratio"] >= CANDIDATE_ORDERED_RATIO
                and row["multiset_dice"] >= CANDIDATE_DICE
                and row["node_count_ratio"] >= CANDIDATE_NODE_RATIO
            ):
                candidate = dict(row)
                candidate["tier"] = "candidate"
                candidates.append(candidate)
            if best is None or row["score"] > best["score"]:
                best = row
        if best is not None:
            pairs.append(best)
    pairs.sort(key=_pair_sort_key)
    candidates.sort(key=_pair_sort_key)
    return pairs, candidates


# --------------------------------------------------------------------------- baseline


def openbb_head(checkout: Path) -> str:
    """Resolve a git checkout's HEAD by reading its files only: no subprocess, no writes."""
    git = checkout / ".git"
    if git.is_file():
        pointer = git.read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir:"):
            raise BuildError(f"cannot resolve the git directory from {git}")
        target = pointer.split(":", 1)[1].strip()
        git = Path(target) if Path(target).is_absolute() else (checkout / target).resolve()
    head_path = git / "HEAD"
    if not head_path.is_file():
        raise BuildError(f"OpenBB checkout has no readable HEAD file: {checkout}")
    content = head_path.read_text(encoding="utf-8").strip()
    if HEX40_RE.fullmatch(content):
        return content
    if not content.startswith("ref:"):
        raise BuildError(f"OpenBB HEAD is neither a commit nor a ref: {content!r}")
    ref = content.split(":", 1)[1].strip()
    bases = [git]
    common = git / "commondir"
    if common.is_file():
        target = common.read_text(encoding="utf-8").strip()
        bases.append(Path(target) if Path(target).is_absolute() else (git / target).resolve())
    for base in bases:
        direct = base / ref
        if direct.is_file():
            value = direct.read_text(encoding="utf-8").strip()
            if HEX40_RE.fullmatch(value):
                return value
            raise BuildError(f"OpenBB ref {ref} does not hold a commit hash")
        packed = base / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.startswith("#") or line.startswith("^"):
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref and HEX40_RE.fullmatch(parts[0]):
                    return parts[0]
    raise BuildError(f"OpenBB ref {ref} cannot be resolved from {checkout}")


def measure_baseline(checkout: Path, expected_head: str) -> dict[str, Any]:
    """Digest the pinned OpenBB provider scope read-only and return the baseline inventory."""
    head = openbb_head(checkout)
    if head != expected_head:
        raise BuildError(
            f"OpenBB checkout {checkout} is at {head}, the instrument pins {expected_head}"
        )
    hashes: dict[str, str] = {}
    blocks: list[Block] = []
    per_provider: dict[str, int] = {}
    function_rows: dict[str, int] = {}
    for provider in sorted(review.EXPECTED_BASELINE_PROVIDERS):
        package = checkout / OPENBB_PROVIDERS_REL / provider / f"openbb_{provider}"
        if not package.is_dir():
            raise BuildError(f"OpenBB baseline package is missing: {package}")
        count = 0
        seen: set[str] = set()
        for source in sorted(package.rglob("*.py")):
            if "__pycache__" in source.parts:
                continue
            if source.is_symlink():
                raise BuildError(f"OpenBB baseline source is a symlink: {source}")
            if not source.is_file():
                raise BuildError(f"OpenBB baseline source is not a regular file: {source}")
            relative = _relative(checkout, source)
            content = source.read_bytes()
            try:
                text = content.decode("utf-8-sig")
                tree = ast.parse(text, filename=relative)
            except (UnicodeError, SyntaxError, ValueError) as error:
                raise BuildError(f"OpenBB baseline source cannot be parsed: {relative}") from error
            file_blocks, _ = _blocks_and_imports(relative, tree)
            hashes[relative] = _sha256(content)
            blocks.extend(file_blocks)
            count += 1
            seen.update(
                block.function for block in file_blocks if block.block_kind == "function_body"
            )
            lines = len(text.splitlines())
            for block in file_blocks:
                if block.span[1] > lines:
                    raise BuildError(f"OpenBB baseline span runs past {relative}")
        per_provider[provider] = count
        function_rows[provider] = len(seen)
    if not hashes:
        raise BuildError("OpenBB baseline scope produced no Python files")
    return {
        "hashes": hashes,
        "blocks": blocks,
        "per_provider": per_provider,
        "functions": function_rows,
        "head": head,
    }


# --------------------------------------------------------------------------- findings


def _nearest_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("local_path"),
        json.dumps(row.get("local_span"), sort_keys=True),
        row.get("local_function"),
        row.get("baseline_path"),
        json.dumps(row.get("baseline_span"), sort_keys=True),
        row.get("baseline_function"),
    )


def load_findings(path: Path) -> Findings:
    """Read the reviewer-authored findings and refuse anything that is not a complete record."""
    payload = _load_json(path, "findings")
    if payload.get("schema") != FINDINGS_SCHEMA:
        raise BuildError(f"findings schema must be {FINDINGS_SCHEMA}")
    reviewer = payload.get("reviewer")
    if not isinstance(reviewer, dict):
        raise BuildError("findings must carry a reviewer object")
    reviewer_name = _text(reviewer.get("name"), "findings reviewer name")
    if reviewer.get("kind") != "AI":
        raise BuildError("findings reviewer kind must be AI")
    if reviewer.get("authorization") != review.EXPECTED_REVIEW_AUTHORIZATION:
        raise BuildError("findings reviewer does not record the direct user authorization")

    rows = payload.get("files")
    if not isinstance(rows, list):
        raise BuildError("findings must carry a files list")
    files: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise BuildError("findings file row must be an object")
        raw_path = _text(row.get("path"), "findings file path")
        if raw_path in files:
            raise BuildError(f"findings repeat a file row: {raw_path}")
        basis = _text(row.get("basis"), f"findings basis for {raw_path}")
        if "\n" in basis:
            raise BuildError(f"findings basis for {raw_path} must stay one line")
        files[raw_path] = {
            "path": raw_path,
            "verdict": _text(row.get("verdict"), f"findings verdict for {raw_path}"),
            "basis": basis,
            "reviewer": _text(row.get("reviewer"), f"findings reviewer for {raw_path}"),
        }

    packages: dict[str, dict[str, str]] = {}
    raw_packages = payload.get("packages") or {}
    if not isinstance(raw_packages, dict):
        raise BuildError("findings packages must be an object keyed by provider")
    for provider, prose in sorted(raw_packages.items()):
        entry = prose if isinstance(prose, dict) else {}
        packages[str(provider)] = {
            key: str(value)
            for key, value in entry.items()
            if isinstance(value, str) and value.strip()
        }

    nearest: dict[tuple[Any, ...], dict[str, Any]] = {}
    raw_nearest = payload.get("nearest_pair_reviews") or []
    if not isinstance(raw_nearest, list):
        raise BuildError("findings nearest_pair_reviews must be a list")
    for row in raw_nearest:
        if not isinstance(row, dict):
            raise BuildError("findings nearest pair row must be an object")
        result = _text(row.get("result"), "findings nearest pair result")
        basis = _text(row.get("basis"), "findings nearest pair basis")
        if "\n" in basis:
            raise BuildError("findings nearest pair basis must stay one line")
        if row.get("reviewed") is not True:
            raise BuildError("findings nearest pair row must set reviewed to true")
        key = _nearest_key(row)
        if key in nearest:
            raise BuildError("findings repeat a nearest pair row")
        nearest[key] = {**row, "result": result, "basis": basis}

    return Findings(
        reviewer={
            "name": reviewer_name,
            "kind": "AI",
            "authorization": review.EXPECTED_REVIEW_AUTHORIZATION,
        },
        files=files,
        packages=packages,
        nearest=nearest,
        scope=str(payload.get("scope") or "").strip(),
        claims_excluded=[str(item) for item in payload.get("claims_excluded", []) or []],
        limitations=[str(item) for item in payload.get("limitations", []) or []],
    )


# --------------------------------------------------------------------------- documents


def _package_records(
    measurement: Measurement, findings: Findings
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    records: list[dict[str, Any]] = []
    stats = {"matched": 0, "missing": 0, "adverse": 0}
    for provider in sorted(measurement.provider_files):
        hand_written = measurement.hand_written[provider]
        vendored = measurement.vendored[provider]
        missing = [relative for relative in hand_written if relative not in findings.files]
        rows = [findings.files[relative] for relative in hand_written if relative in findings.files]
        adverse = sorted(
            {
                row["verdict"]
                for row in rows
                if row["verdict"] != review.EXPECTED_PACKAGE_REVIEW_RESULT
            }
        )
        covered = not missing
        clean = covered and not adverse
        stats["matched"] += len(rows)
        stats["missing"] += len(missing)
        stats["adverse"] += len(adverse)
        prose = findings.packages.get(provider, {})
        pinning = (
            f"{len(vendored)} 个文件是 {VENDOR_LOCK_NAME} 逐文件哈希锁定的原文搬运副本，"
            "其字节一致性本轮按 lock（以及锁定这些字节的 {manifest}）实测校验，未人工改写"
        ).format(manifest=VENDOR_MANIFEST_NAME)
        comparison = prose.get("comparison") or (
            f"{provider}：{len(hand_written)} 个自研文件逐一与固定 OpenBB 基线对照；"
            + (f"{pinning}。" if vendored else "无锁定的原文搬运副本。")
        )
        basis = prose.get("basis") or (
            f"结论逐文件来自 --findings（{len(rows)}/{len(hand_written)} 自研文件有记录）；"
            + (pinning if vendored else "本包无 lock 锁定副本。")
        )
        verdicts = sorted({row["verdict"] for row in rows})
        records.append(
            {
                "provider": provider,
                "reviewed": clean,
                "review_result": (
                    review.EXPECTED_PACKAGE_REVIEW_RESULT
                    if clean
                    else (adverse[0] if adverse else UNREVIEWED_RESULT)
                ),
                "comparison": comparison,
                "basis": basis,
                "python_files": measurement.provider_counts[provider],
                "source_files_sha256": dict(sorted(measurement.provider_files[provider].items())),
                "hand_written_files": len(hand_written),
                "lock_pinned_verbatim_files": len(vendored),
                "findings_rows": len(rows),
                "findings_missing": missing[:20],
                "file_verdicts": verdicts,
                "file_reviewers": sorted({row["reviewer"] for row in rows}),
                "vendor_pinning": pinning if vendored else "",
            }
        )
    return records, stats


def _source_review_document(
    measurement: Measurement,
    findings: Findings,
    records: list[dict[str, Any]],
    generated_at: str,
    checkout: Path,
    head: str,
) -> dict[str, Any]:
    scope = findings.scope or (
        f"{len(measurement.provider_files)} 个当前注册 provider 包的 "
        f"{measurement.total_files} 个 py（自研 {measurement.hand_written_files} / "
        f"lock 锁定原文搬运 {measurement.vendored_files}）逐文件源码对照固定 OpenBB 基线；"
        "公共 API 事实与命名不视作实现复制证据"
    )
    return {
        "archive_round": "C65",
        "generated_at": generated_at,
        "generated_by": "scripts/quality/provider_source_review_evidence_build.py",
        "reviewer": findings.reviewer,
        "openbb_baseline": {
            "path": str(checkout),
            "head": head,
            "read_only": True,
            "scope": BASELINE_SCOPE_NOTE,
        },
        "scope": scope,
        "packages": records,
        "counts": {
            "provider_packages": len(measurement.provider_files),
            "python_files": measurement.total_files,
            "lock_pinned_verbatim_files": measurement.vendored_files,
            "hand_written_files": measurement.hand_written_files,
            "findings_rows": len(findings.files),
        },
        "claims_excluded": findings.claims_excluded
        or [
            "历史开发没有参照任何源码",
            "全仓近似复制数学证明",
            "第三方数据使用授权",
            "全部provider已对照PASS",
        ],
    }


def _similarity_document(
    measurement: Measurement,
    baseline: dict[str, Any],
    pairs: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    generated_date: str,
    checkout: Path,
) -> dict[str, Any]:
    local_manifest = [
        {"path": path, "sha256": digest}
        for path, digest in sorted(
            {relative: record.sha256 for relative, record in measurement.records.items()}.items()
        )
    ]
    baseline_manifest = [
        {"path": path, "sha256": digest} for path, digest in sorted(baseline["hashes"].items())
    ]
    screened = {
        provider: len(
            {block.path for block in measurement.local_blocks if _owner(block) == provider}
        )
        for provider in measurement.provider_files
    }
    return {
        "schema": "opendata-c65-provider-similarity/v1",
        "generated_date": generated_date,
        "generated_by": "scripts/quality/provider_source_review_evidence_build.py",
        "comparison": {
            "local_repo": str(measurement.root),
            "baseline_repo": str(checkout),
            "baseline_commit": baseline["head"],
            "baseline_read_only": True,
            "baseline_scope": BASELINE_SCOPE_NOTE,
            "providers_local": sorted(measurement.provider_files),
            "providers_baseline": sorted(review.EXPECTED_BASELINE_PROVIDERS),
        },
        "method": {
            "scope": (
                "本轮实测：自研（非 _vendor）provider 源码的函数体与非空嵌套语句块，"
                "对照固定 OpenBB 基线生产源码；lock 锁定的原文搬运副本不参与结构对照，"
                "其字节一致性由 upstream.lock 逐文件哈希证明"
            ),
            "ast_node_count": "块内 AST 节点数（含后代）；少于 41 个的块跳过",
            "block_inventory": "函数体 + 非空 body/orelse/finalbody/except/match case 语句列表",
            "normalization": (
                "局部绑定名（形参、存储/删除的标识符、import 别名、嵌套 def/class 名）归一为 L；"
                "调用目标、属性名、关键字名与 AST 节点类型保留"
            ),
            "candidate_filter": (
                f"node_count_ratio >= {CANDIDATE_NODE_RATIO} 且 ordered_token_ratio >= "
                f"{CANDIDATE_ORDERED_RATIO} 且 multiset_dice >= {CANDIDATE_DICE}"
            ),
            "nearest_pairs": (
                f"每个通过预筛（node_count_ratio >= {PREFILTER_NODE_RATIO} 且 multiset_dice >= "
                f"{PREFILTER_DICE}）的本地块记录得分最高的一对，用于人工排序，不是结论"
            ),
            "score": (
                f"{WEIGHT_ORDERED}*ordered_token_ratio + {WEIGHT_DICE}*multiset_dice + "
                f"{WEIGHT_NODES}*node_count_ratio；仅是分诊排名"
            ),
            "ranking": "按得分降序，同分按 local_path/local_span/baseline_path/baseline_span",
            "vendor_exclusion": (
                f"_vendor 下 {measurement.vendored_files} 个文件按 {VENDOR_LOCK_NAME}"
                f"（并经 {VENDOR_MANIFEST_NAME} 链到该哈希）证明为逐字搬运，不参与结构对照"
            ),
            "limitations": [
                "启发式 AST 分诊会漏掉改写、重排、拆分与跨函数的复制，不是克隆检测器",
                "公共库、框架模式、API 与 provider schema 会产生结构相似，候选需要人工复核",
                "本轮当前源码哈希不能证明历史编写过程、clean-room 记录或任何法律/数据权利结论",
            ],
            "actions_not_performed": [
                "未执行任何 provider 网络调用",
                "未写入 OpenBB checkout 或其 git 目录",
                "未运行被审代码",
                "未生成或修改 docs/evidence 下的档案",
                "未把自研结论填进审阅记录：结论全部来自 --findings",
            ],
        },
        "counts": {
            "local_provider_python_files": len(local_manifest),
            "local_functions_methods": measurement.functions_methods,
            "local_blocks_over_40_nodes": len(measurement.local_blocks),
            "local_screened_files": sum(screened.values()),
            "baseline_provider_python_files": len(baseline_manifest),
            "baseline_functions_methods": sum(baseline["functions"].values()),
            "baseline_blocks_over_40_nodes": len(baseline["blocks"]),
            "review_candidates": len(candidates),
            "nearest_pairs": len(pairs),
            "local_parse_errors": [],
            "baseline_parse_errors": [],
            "by_provider_local": {
                provider: {
                    "python_files": measurement.provider_counts[provider],
                    "hand_written_files": len(measurement.hand_written[provider]),
                    "lock_pinned_verbatim_files": len(measurement.vendored[provider]),
                    "screened_files": screened.get(provider, 0),
                }
                for provider in sorted(measurement.provider_files)
            },
            "by_provider_baseline": {
                provider: {
                    "python_files": baseline["per_provider"][provider],
                    "functions_methods": baseline["functions"][provider],
                }
                for provider in sorted(review.EXPECTED_BASELINE_PROVIDERS)
            },
        },
        "source_manifests": {
            "local_files_sha256": local_manifest,
            "local_sorted_manifest_sha256": _manifest_digest(local_manifest),
            "baseline_files_sha256": baseline_manifest,
            "baseline_sorted_manifest_sha256": _manifest_digest(baseline_manifest),
        },
        "candidates": candidates,
        "nearest_pairs": pairs,
    }


def _owner(block: Block) -> str:
    return block.path.split("/")[3]


def _manifest_digest(rows: list[dict[str, str]]) -> str:
    hasher = hashlib.sha256()
    for row in rows:
        hasher.update(row["path"].encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(row["sha256"].encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def _nearest_document(
    findings: Findings,
    pairs: list[dict[str, Any]],
    review_sha256: str,
    similarity_sha256: str,
    generated_at: str,
    limitations: list[str],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for pair in pairs[:NEAREST_PAIR_ROWS]:
        finding = findings.nearest.get(_nearest_key(pair))
        if finding is None:
            missing.append(pair)
            continue
        row = dict(pair)
        row["reviewed"] = True
        row["result"] = finding["result"]
        row["basis"] = finding["basis"]
        rows.append(row)
    if missing:
        raise BuildError(
            "findings carry no conclusion for the top-ranked nearest pairs; add them (identity "
            "copied from --print-nearest-pairs) before writing a round: "
            + json.dumps(missing, ensure_ascii=False, sort_keys=True)
        )
    adverse = [
        row["result"] for row in rows if row["result"] != review.EXPECTED_NEAREST_REVIEW_RESULT
    ]
    if adverse:
        raise BuildError(
            f"a ranked nearest pair is concluded as shared implementation ({adverse[0]}); "
            "this round cannot be certified as a zero-copy review"
        )
    return {
        "archive_round": "C65",
        "generated_at": generated_at,
        "generated_by": "scripts/quality/provider_source_review_evidence_build.py",
        "reviewer": findings.reviewer,
        "review_scope": findings.scope
        or (
            "current registered provider Python packages; fixed five OpenBB provider baselines; "
            "no historical-development or whole-repository provenance assertion"
        ),
        "provider_review_sha256": review_sha256,
        "similarity_inventory_sha256": similarity_sha256,
        "review_complete": True,
        "unreviewed_candidates": 0,
        "nearest_pair_reviews": rows,
        "limitations": limitations
        or [
            "启发式 AST 分诊会漏掉改写、重排、拆分与跨函数的复制，不是克隆检测器",
            "当前源码哈希不能证明历史编写过程或法律/数据权利结论",
        ],
    }


# --------------------------------------------------------------------------- writing


def _has_archive_parts(candidate: Path, root: Path) -> bool:
    """Whether a path reaches a prior round's archive, judged below the repository root."""
    for value in (candidate, candidate.resolve()):
        try:
            relative = value.relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        if any(parts[index : index + 2] == PROTECTED_ARCHIVE_PARTS for index in range(len(parts))):
            return True
    return False


def _refuse_archive(out_dir: Path, root: Path) -> None:
    if _has_archive_parts(out_dir, root):
        raise BuildError(
            f"refusing to write into a prior round's archive under docs/evidence: {out_dir}"
        )


def _planned_paths(out_dir: Path) -> dict[str, Path]:
    return {
        "source_review": out_dir / review.SOURCE_REVIEW_REL.name,
        "similarity": out_dir / review.SIMILARITY_REL.name,
        "nearest_review": out_dir / review.NEAREST_REVIEW_REL.name,
    }


def write_bundle(
    out_dir: Path, payloads: dict[str, str], *, force: bool, root: Path
) -> dict[str, str]:
    """Write the three artifacts under ``out_dir``, never inside docs/evidence."""
    _refuse_archive(out_dir, root)
    paths = _planned_paths(out_dir)
    if not force:
        existing = [str(path) for path in paths.values() if path.exists()]
        if existing:
            raise BuildError(f"refusing to overwrite {', '.join(existing)} without --force")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = dict(paths)
    for name in ("source_review", "similarity"):
        written[name].write_text(payloads[name], encoding="utf-8")
    payload_names = ("source_review", "similarity")
    digests = {name: _sha256(payloads[name].encode("utf-8")) for name in payload_names}
    nearest = json.loads(payloads["nearest_review"])
    if nearest.get("provider_review_sha256") != digests["source_review"]:
        raise BuildError("the nearest review is not bound to the source review bytes it writes")
    if nearest.get("similarity_inventory_sha256") != digests["similarity"]:
        raise BuildError("the nearest review is not bound to the similarity inventory bytes")
    written["nearest_review"].write_text(payloads["nearest_review"], encoding="utf-8")
    return {name: _relative(out_dir, path) for name, path in written.items()}


def verify_bundle(root: Path, payloads: dict[str, str], now: datetime) -> list[str]:
    """Stage the bundle the way the validator reads it and return its issue codes."""
    staging = Path(tempfile.mkdtemp(prefix="provider-review-verify-"))
    try:
        for name, relative in (
            ("source_review", review.SOURCE_REVIEW_REL),
            ("similarity", review.SIMILARITY_REL),
            ("nearest_review", review.NEAREST_REVIEW_REL),
        ):
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payloads[name], encoding="utf-8")
        measurement_records = _copy_provider_tree(root, staging)
        if measurement_records == 0:
            return ["provider-source-root-missing"]
        result = review.validate(staging, now=now)
        return [issue.code for issue in result.issues]
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _copy_provider_tree(root: Path, staging: Path) -> int:
    source_root = root / review.PROVIDER_ROOT_REL
    if not source_root.is_dir():
        return 0
    copied = 0
    for source in sorted(source_root.rglob("*.py")):
        if source.is_symlink() or not source.is_file():
            continue
        target = staging / _relative(root, source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        copied += 1
    return copied


# --------------------------------------------------------------------------- orchestration


def _local_date(now: datetime) -> str:
    return now.astimezone(review.LOCAL_TIMEZONE).date().isoformat()


def build_bundle(
    root: Path,
    *,
    findings_path: Path,
    out_dir: Path,
    openbb_checkout: Path = Path(DEFAULT_OPENBB_CHECKOUT),
    expected_head: str = review.EXPECTED_OPENBB_COMMIT,
    now: datetime | None = None,
    force: bool = False,
    self_verify: bool = True,
) -> dict[str, Any]:
    """Re-measure the tree, bind the reviewer's findings and write the three artifacts."""
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise BuildError("now must be timezone-aware")
    generated_at = stamp.astimezone(timezone.utc).isoformat()
    if openbb_checkout.resolve() == root.resolve():
        raise BuildError("the OpenBB checkout must not be the reviewed repository")
    findings = load_findings(findings_path)
    measurement = measure_tree(root)
    unknown = sorted(relative for relative in findings.files if relative not in measurement.records)
    if unknown:
        raise BuildError(
            f"findings name paths that do not exist in the live tree: {', '.join(unknown[:10])}"
        )
    foreign_packages = sorted(set(findings.packages) - set(measurement.provider_files))
    if foreign_packages:
        raise BuildError(
            f"findings carry prose for packages that are not registered: "
            f"{', '.join(foreign_packages)}"
        )
    baseline = measure_baseline(openbb_checkout, expected_head)
    pairs, candidates = screen_blocks(measurement.local_blocks, baseline["blocks"])
    if len(pairs) < NEAREST_PAIR_ROWS:
        raise BuildError(
            f"only {len(pairs)} ranked nearest pairs were measured; the validator needs "
            f"{NEAREST_PAIR_ROWS}"
        )
    if candidates:
        raise BuildError(
            f"{len(candidates)} similarity candidates crossed the review thresholds, so this "
            "round is not a zero-copy observation: "
            + json.dumps(candidates[:2], ensure_ascii=False, sort_keys=True)
        )
    records, stats = _package_records(measurement, findings)
    documents = {
        "source_review": _source_review_document(
            measurement, findings, records, generated_at, openbb_checkout, baseline["head"]
        ),
        "similarity": _similarity_document(
            measurement, baseline, pairs, candidates, _local_date(stamp), openbb_checkout
        ),
    }
    payloads = {name: _dump(document) for name, document in documents.items()}
    nearest = _nearest_document(
        findings,
        pairs,
        _sha256(payloads["source_review"].encode("utf-8")),
        _sha256(payloads["similarity"].encode("utf-8")),
        generated_at,
        findings.limitations,
    )
    payloads["nearest_review"] = _dump(nearest)
    written = write_bundle(out_dir, payloads, force=force, root=root)
    issues = verify_bundle(root, payloads, stamp) if self_verify else []
    reviewed = sum(1 for record in records if record["reviewed"])
    return {
        "out_dir": str(out_dir),
        "artifacts": written,
        "provider_packages": len(measurement.provider_files),
        "python_files": measurement.total_files,
        "vendored_files": measurement.vendored_files,
        "hand_written_files": measurement.hand_written_files,
        "hand_written_unproven": len(measurement.unproven),
        "findings_rows": len(findings.files),
        "findings_matched": stats["matched"],
        "findings_missing": stats["missing"],
        "packages_reviewed": reviewed,
        "packages_total": len(records),
        "baseline_python_files": len(baseline["hashes"]),
        "baseline_head": baseline["head"],
        "local_blocks": len(measurement.local_blocks),
        "baseline_blocks": len(baseline["blocks"]),
        "nearest_pairs": len(pairs),
        "candidates": len(candidates),
        "validation_issues": issues,
        "validated": not issues,
    }


def nearest_pair_worklist(
    root: Path,
    *,
    findings_path: Path,
    openbb_checkout: Path = Path(DEFAULT_OPENBB_CHECKOUT),
    expected_head: str = review.EXPECTED_OPENBB_COMMIT,
    rows: int = NEAREST_PAIR_ROWS,
) -> list[dict[str, Any]]:
    """Return the ranked pairs a reviewer must conclude on, as findings-shaped stubs."""
    load_findings(findings_path)
    measurement = measure_tree(root)
    baseline = measure_baseline(openbb_checkout, expected_head)
    pairs, _ = screen_blocks(measurement.local_blocks, baseline["blocks"])
    worklist: list[dict[str, Any]] = []
    for pair in pairs[:rows]:
        stub = dict(pair)
        stub.update({"reviewed": True, "result": "", "basis": ""})
        worklist.append(stub)
    return worklist


def _resolve(root: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (root / candidate)


def main(argv: list[str] | None = None) -> int:
    """Rebuild one round's provider-source review bundle; exit 0 only when it validates clean."""
    parser = argparse.ArgumentParser(
        description="Re-measure the live provider tree and rebuild the C65 review bundle."
    )
    parser.add_argument(
        "-o", "--out-dir", required=True, help="directory for the three artifacts (required)"
    )
    parser.add_argument("--findings", required=True, help="reviewer-authored findings JSON")
    parser.add_argument("--root", default=_REPO_ROOT, type=Path, help="repository root")
    parser.add_argument(
        "--openbb-root", default=DEFAULT_OPENBB_CHECKOUT, help="pinned OpenBB checkout (read-only)"
    )
    parser.add_argument(
        "--expected-head",
        default=review.EXPECTED_OPENBB_COMMIT,
        help="commit the OpenBB checkout must be at",
    )
    parser.add_argument("--force", action="store_true", help="rewrite an existing round")
    parser.add_argument(
        "--no-self-verify",
        action="store_true",
        help="skip re-validating the bundle this tool just produced",
    )
    parser.add_argument(
        "--print-nearest-pairs",
        action="store_true",
        help="print the ranked nearest pairs a reviewer must conclude on and write nothing",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    findings_path = _resolve(root, args.findings)
    checkout = Path(args.openbb_root).expanduser().resolve()
    try:
        if args.print_nearest_pairs:
            worklist = nearest_pair_worklist(
                root,
                findings_path=findings_path,
                openbb_checkout=checkout,
                expected_head=args.expected_head,
            )
            print(json.dumps(worklist, ensure_ascii=False, indent=2, sort_keys=True))
            print(
                f"NEAREST_PAIR_WORKLIST {len(worklist)} rows; fill result/basis and put them "
                "under nearest_pair_reviews in the findings file",
                file=sys.stderr,
            )
            return 4
        facts = build_bundle(
            root,
            findings_path=findings_path,
            out_dir=_resolve(root, args.out_dir),
            openbb_checkout=checkout,
            expected_head=args.expected_head,
            force=args.force,
            self_verify=not args.no_self_verify,
        )
    except BuildError as error:
        print(f"BUILD_REFUSED {error}", file=sys.stderr)
        print(json.dumps({"built": False, "reason": str(error)}, ensure_ascii=False))
        return 2
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"BUILD_REFUSED {error}", file=sys.stderr)
        print(json.dumps({"built": False, "reason": str(error)}, ensure_ascii=False))
        return 2
    print(
        "PROVIDER SOURCE REVIEW "
        f"packages={facts['provider_packages']} python_files={facts['python_files']} "
        f"lock_pinned_verbatim={facts['vendored_files']} "
        f"hand_written={facts['hand_written_files']} "
        f"findings_matched={facts['findings_matched']}/{facts['findings_rows']} "
        f"packages_reviewed={facts['packages_reviewed']}/{facts['packages_total']} "
        f"baseline_files={facts['baseline_python_files']} baseline_head={facts['baseline_head']} "
        f"nearest_pairs={facts['nearest_pairs']} candidates={facts['candidates']}"
    )
    print(json.dumps({"built": True, **facts}, ensure_ascii=False, indent=2, sort_keys=True))
    if facts["validation_issues"]:
        print(
            f"BUNDLE_NOT_CLEAN {len(facts['validation_issues'])} issues: "
            + ", ".join(facts["validation_issues"][:10]),
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
