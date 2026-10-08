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
import hashlib
import json
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

if __package__:
    from scripts.quality.source_layout import (
        FIRST_PARTY,
        PORTED,
        VENDOR_ROOT,
        SourceLayoutError,
        historical_identity,
        iter_unique_python_files,
    )
else:  # Running this file directly from scripts/codemod.
    sys.path.insert(0, str(REPO_ROOT))
    from scripts.quality.source_layout import (
        FIRST_PARTY,
        PORTED,
        VENDOR_ROOT,
        SourceLayoutError,
        historical_identity,
        iter_unique_python_files,
    )

FORBIDDEN_ROOTS = ("akshare", "openbb")

# Runtime packages only. Development tooling, tests and docs are out of scope.
# `opendata_providers` used to be listed here and does not exist: it was a name in a
# list, not a directory in the tree, and nothing noticed (see the module docstring).
DEFAULT_TARGETS = (
    "opendata",
    VENDOR_ROOT,
    "opendata_client",
)

BASELINE_PATH = "docs/quality/zero-dep-baseline.json"
BASELINE_VERSION = 2
REFERENCE_POLICY_PATH = "docs/quality/akshare-reference-allowlist.json"
REFERENCE_POLICY_VERSION = 1

# Bump when the scan logic changes, so an old reading can never be compared with a new
# one by accident. Interpreters the baseline has been verified under: CI runs 3.11,
# development runs 3.13; anything else is an unreviewed parser surface.
SCANNER_VERSION = 4
PYTHON_MINORS = ("3.11", "3.13")

REFERENCE_CATEGORIES = frozenset(
    {
        "runtime-integration",
        "source-adapter",
        "mapping",
        "cross-check",
        "build-declaration",
        "operations-tool",
        "test-fixture",
        "documentation",
    }
)

EXPECTED_METADATA_EXCEPTIONS: dict[str, tuple[str, str, str, str, str]] = {
    "openbb_model_lookup": (
        "opendata/data/openbb_map.py",
        "_parse_entry",
        "entry.get",
        "arg:0",
        "openbb",
    ),
    "data_script_source_default": (
        "opendata/models/data_script.py",
        "DataScript.source",
        "mapped_column",
        "keyword:default",
        "akshare",
    ),
    "patrol_key_status_label": (
        "opendata/pipeline/patrol.py",
        "key_status",
        "dict",
        "dict_key",
        "akshare",
    ),
    "akshare_provider_source": (
        "opendata/data/providers/akshare/provider.py",
        "module.PROVIDER",
        "Provider",
        "keyword:source",
        "akshare",
    ),
    "akshare_registration_source": (
        "opendata/data/providers/akshare/registration.py",
        "register",
        "register_provider",
        "arg:0",
        "akshare",
    ),
    "akshare_fetchers_source": (
        "opendata/data/providers/akshare/registration.py",
        "__getattr__:FETCHERS",
        "fetchers_for",
        "arg:0",
        "akshare",
    ),
}

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


class ReferencePolicyError(RuntimeError):
    """The reference allowlist is absent, stale, or structurally invalid."""


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


@dataclass(frozen=True)
class MetadataException:
    """One exact, source-hash-bound metadata literal exemption."""

    kind: str
    scope: str
    callee: str
    slot: str
    literal: str


@dataclass(frozen=True)
class ReferenceEntry:
    """Reviewed path record for a tracked source reference."""

    path: str
    purpose: str
    category: str
    reviewed_date: str
    reviewer: str
    sha256: str
    ast_exceptions: tuple[MetadataException, ...]


@dataclass(frozen=True)
class ReferencePolicy:
    """Strictly parsed C65 per-path reference register."""

    entries: tuple[ReferenceEntry, ...]
    scanner_version: int

    @property
    def by_path(self) -> dict[str, ReferenceEntry]:
        """Entries keyed by their repository-relative path."""
        return {entry.path: entry for entry in self.entries}


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


def _string_value(node: ast.AST, values: dict[str, str]) -> str | None:
    """Resolve a literal string or a simple name/concatenation without execution."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return values.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _string_value(node.left, values)
        right = _string_value(node.right, values)
        if left is not None and right is not None:
            return left + right
    return None


def _static_string_names(tree: ast.AST) -> dict[str, str]:
    """Resolve names assigned one consistent static string value in the file."""
    assignments: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        targets: list[ast.expr]
        value: ast.expr | None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                assignments.setdefault(target.id, []).append(value)

    resolved: dict[str, str] = {}
    for _ in range(len(assignments) + 1):
        changed = False
        for name, candidates in assignments.items():
            values = [_string_value(candidate, resolved) for candidate in candidates]
            known = [value for value in values if value is not None]
            if (
                len(known) == len(candidates)
                and known
                and len(set(known)) == 1
                and resolved.get(name) != known[0]
            ):
                resolved[name] = known[0]
                changed = True
        if not changed:
            break
    return resolved


def _dynamic_callable_names(tree: ast.AST) -> set[str]:
    """Find direct and aliased dynamic-import call names without importing modules."""
    names = {"__import__"}
    module_aliases: set[str] = set()
    assignments: list[ast.Assign | ast.AnnAssign] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    module_aliases.add(alias.asname or "importlib")
                elif alias.name == "builtins":
                    module_aliases.add(alias.asname or "builtins")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if (module == "importlib" and alias.name == "import_module") or (
                    module == "builtins" and alias.name == "__import__"
                ):
                    names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            assignments.append(node)

    loader_names = {
        f"{module}.{loader}" for module in module_aliases for loader in DYNAMIC_IMPORT_NAMES
    }
    while True:
        previous_size = len(names)
        for node in assignments:
            value = node.value
            if value is None:
                continue
            dotted = _dotted_name(value)
            source_name = value.id if isinstance(value, ast.Name) else None
            if dotted not in loader_names and source_name not in names:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(target.id for target in targets if isinstance(target, ast.Name))
        if len(names) == previous_size:
            break
    return names


def _is_dynamic_import_call(call: ast.Call, dynamic_names: set[str]) -> bool:
    """Recognize direct imports, importlib aliases and statically aliased loaders."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in dynamic_names
    dotted = _dotted_name(func)
    return bool(dotted and dotted.rsplit(".", maxsplit=1)[-1] in DYNAMIC_IMPORT_NAMES)


def scan_source(source: str, filename: str) -> list[Finding]:
    """Scan Python source text and return every forbidden reference."""
    tree = ast.parse(source, filename=filename)
    parents = _parents(tree)
    findings: list[Finding] = []
    string_names = _static_string_names(tree)
    dynamic_names = _dynamic_callable_names(tree)

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
            if not _looks_like_module_path(node.value):
                continue
            parent = parents.get(node)
            is_import_argument = (
                isinstance(parent, ast.Call)
                and _is_dynamic_import_call(parent, dynamic_names)
                and (
                    any(argument is node for argument in parent.args)
                    or any(keyword.value is node for keyword in parent.keywords)
                )
            )
            if not is_import_argument:
                findings.append(Finding(filename, node.lineno, node.value, "string"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_dynamic_import_call(node, dynamic_names):
            continue
        argument: ast.expr | None = node.args[0] if node.args else None
        if argument is None:
            argument = next(
                (keyword.value for keyword in node.keywords if keyword.arg in {"name", "module"}),
                None,
            )
        value = _string_value(argument, string_names) if argument is not None else None
        if value is not None and any(root in value for root in FORBIDDEN_ROOTS):
            findings.append(Finding(filename, node.lineno, value, "dynamic"))

    return findings


def _required_policy_text(record: dict[str, object], key: str, context: str) -> str:
    """Read one required nonempty string from a policy record."""
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ReferencePolicyError(f"{context}: {key} must be a non-empty string")
    return value


def parse_reference_policy(raw: object) -> ReferencePolicy:
    """Parse and validate the C65 reference policy, failing closed on every mismatch."""
    if not isinstance(raw, dict):
        raise ReferencePolicyError("policy must be a JSON object")
    document: dict[str, object] = raw
    if document.get("schema_version") != REFERENCE_POLICY_VERSION:
        raise ReferencePolicyError(f"schema_version must be {REFERENCE_POLICY_VERSION}")
    if document.get("scanner_version") != SCANNER_VERSION:
        raise ReferencePolicyError(f"scanner_version must be {SCANNER_VERSION}")
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ReferencePolicyError("entries must be a non-empty list")

    entries: list[ReferenceEntry] = []
    paths: set[str] = set()
    exception_kinds: list[str] = []
    for index, value in enumerate(raw_entries):
        if not isinstance(value, dict):
            raise ReferencePolicyError(f"entries[{index}] must be an object")
        record: dict[str, object] = value
        expected_keys = {
            "path",
            "purpose",
            "category",
            "reviewed_date",
            "reviewer",
            "sha256",
            "ast_exceptions",
        }
        if set(record) != expected_keys:
            raise ReferencePolicyError(f"entries[{index}] fields do not match the schema")
        path = record.get("path")
        if (
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or "\\" in path
            or ".." in Path(path).parts
            or Path(path).as_posix() != path
        ):
            raise ReferencePolicyError(f"entries[{index}].path must be a safe POSIX repo path")
        if path in paths:
            raise ReferencePolicyError(f"duplicate policy path: {path}")
        paths.add(path)
        purpose = record.get("purpose")
        category = record.get("category")
        reviewer = record.get("reviewer")
        if not isinstance(purpose, str) or not purpose.strip() or _unspecified(purpose):
            raise ReferencePolicyError(f"{path}: purpose is empty or unspecified")
        if not isinstance(category, str) or category not in REFERENCE_CATEGORIES:
            raise ReferencePolicyError(f"{path}: category is not an allowed category")
        if not isinstance(reviewer, str) or not reviewer.strip() or _unspecified(reviewer):
            raise ReferencePolicyError(f"{path}: reviewer is empty or unspecified")
        reviewed_date = record.get("reviewed_date")
        if not isinstance(reviewed_date, str):
            raise ReferencePolicyError(f"{path}: reviewed_date must be YYYY-MM-DD")
        try:
            parsed_date = date.fromisoformat(reviewed_date)
        except ValueError as exc:
            raise ReferencePolicyError(f"{path}: reviewed_date must be YYYY-MM-DD") from exc
        if parsed_date.isoformat() != reviewed_date:
            raise ReferencePolicyError(f"{path}: reviewed_date must be canonical YYYY-MM-DD")
        digest = record.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ReferencePolicyError(f"{path}: sha256 must be 64 lowercase hexadecimal digits")
        raw_exceptions = record.get("ast_exceptions")
        if not isinstance(raw_exceptions, list):
            raise ReferencePolicyError(f"{path}: ast_exceptions must be a list")
        exceptions: list[MetadataException] = []
        for exception_index, exception_value in enumerate(raw_exceptions):
            if not isinstance(exception_value, dict):
                raise ReferencePolicyError(
                    f"{path}: ast_exceptions[{exception_index}] must be an object"
                )
            exception: dict[str, object] = exception_value
            if set(exception) != {"kind", "scope", "callee", "slot", "literal"}:
                raise ReferencePolicyError(
                    f"{path}: ast_exceptions[{exception_index}] fields do not match the schema"
                )
            context = f"{path}: ast_exceptions[{exception_index}]"
            kind = _required_policy_text(exception, "kind", context)
            scope = _required_policy_text(exception, "scope", context)
            callee = _required_policy_text(exception, "callee", context)
            slot = _required_policy_text(exception, "slot", context)
            literal = _required_policy_text(exception, "literal", context)
            expected = EXPECTED_METADATA_EXCEPTIONS.get(kind)
            if expected is None or (path, scope, callee, slot, literal) != expected:
                raise ReferencePolicyError(f"{path}: unapproved or misbound AST exception {kind!r}")
            exception_kinds.append(kind)
            exceptions.append(MetadataException(kind, scope, callee, slot, literal))
        entries.append(
            ReferenceEntry(
                path=path,
                purpose=purpose,
                category=category,
                reviewed_date=reviewed_date,
                reviewer=reviewer,
                sha256=digest,
                ast_exceptions=tuple(exceptions),
            )
        )

    if set(exception_kinds) != set(EXPECTED_METADATA_EXCEPTIONS) or len(exception_kinds) != len(
        EXPECTED_METADATA_EXCEPTIONS
    ):
        raise ReferencePolicyError(
            f"policy must bind each of the {len(EXPECTED_METADATA_EXCEPTIONS)} "
            "approved AST exceptions once"
        )
    return ReferencePolicy(tuple(entries), SCANNER_VERSION)


def _unspecified(value: str) -> bool:
    """Whether a policy field is a placeholder rather than a reviewed value."""
    normalized = value.strip().casefold()
    if normalized in {"", "-", "—", "n/a", "na", "none", "tbd", "unknown"}:
        return True
    return any(marker in normalized for marker in ("未指定", "待确认", "unspecified", "todo"))


def reference_policy_problems(
    raw: object,
    root: Path,
    *,
    actual_hit_paths: set[str] | None = None,
    excluded_paths: set[str] | None = None,
) -> list[str]:
    """Validate metadata, file hashes and bidirectional hit/register reconciliation."""
    try:
        policy = parse_reference_policy(raw)
    except ReferencePolicyError as exc:
        return [str(exc)]
    problems: list[str] = []
    valid_exception_paths: set[str] = set()
    for entry in policy.entries:
        path = root / entry.path
        if not path.is_file():
            problems.append(f"stale registered path: {entry.path}")
            continue
        try:
            content = path.read_bytes()
        except OSError:
            problems.append(f"unreadable registered path: {entry.path}")
            continue
        digest = hashlib.sha256(content).hexdigest()
        hash_is_current = digest == entry.sha256
        if not hash_is_current:
            problems.append(f"stale sha256: {entry.path}")
        if entry.ast_exceptions:
            try:
                source = content.decode("utf-8")
            except UnicodeDecodeError:
                problems.append(f"{entry.path}: metadata source is not valid UTF-8")
                continue
            exception_problems = _metadata_exception_problems(
                entry.path, source, entry.ast_exceptions
            )
            problems.extend(exception_problems)
            if hash_is_current and not exception_problems:
                # The only no-hit registrations accepted by reconciliation are exact,
                # current-hash metadata exceptions on their fixed approved paths.
                valid_exception_paths.add(entry.path)
    if actual_hit_paths is not None:
        registered = set(policy.by_path)
        excluded = excluded_paths or set()
        eligible = (actual_hit_paths - excluded) | valid_exception_paths
        problems.extend(
            f"unregistered hit path: {hit_path}" for hit_path in sorted(eligible - registered)
        )
        problems.extend(
            f"stale registered path: {stale_path}" for stale_path in sorted(registered - eligible)
        )
    return problems


def load_reference_policy(root: Path = REPO_ROOT) -> ReferencePolicy:
    """Load a structurally valid, current-hash reference policy."""
    path = root / REFERENCE_POLICY_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferencePolicyError(f"{REFERENCE_POLICY_PATH} is missing or malformed") from exc
    problems = reference_policy_problems(raw, root)
    if problems:
        raise ReferencePolicyError("; ".join(problems))
    return parse_reference_policy(raw)


def _has_ancestor(node: ast.AST, parents: dict[ast.AST, ast.AST], name: str) -> bool:
    """Whether a node is lexically nested under a function or class of this name."""
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (
            current.name == name
        ):
            return True
        current = parents.get(current)
    return False


class _ScopeBindings(ast.NodeVisitor):
    """Collect names bound in one scope while skipping nested function and class bodies."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        self.names.update(
            alias.asname or alias.name.split(".", maxsplit=1)[0] for alias in node.names
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.names.update(
            "*" if alias.name == "*" else alias.asname or alias.name for alias in node.names
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)
        self._visit_definition_header(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)
        self._visit_definition_header(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)
        for expression in (*node.decorator_list, *node.bases):
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expression in (*node.args.defaults, *node.args.kw_defaults):
            if expression is not None:
                self.visit(expression)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.names.add(node.name)
        if node.type is not None:
            self.visit(node.type)
        for statement in node.body:
            self.visit(statement)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name is not None:
            self.names.add(node.name)
        if node.pattern is not None:
            self.visit(node.pattern)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name is not None:
            self.names.add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest is not None:
            self.names.add(node.rest)
        for pattern in node.patterns:
            self.visit(pattern)

    def _visit_definition_header(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for expression in (*node.decorator_list, *node.args.defaults):
            self.visit(expression)
        for default_expression in node.args.kw_defaults:
            if default_expression is not None:
                self.visit(default_expression)
        arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        if node.args.vararg is not None:
            arguments += (node.args.vararg,)
        if node.args.kwarg is not None:
            arguments += (node.args.kwarg,)
        for argument in arguments:
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if node.returns is not None:
            self.visit(node.returns)


def _scope_bound_names(statements: tuple[ast.stmt, ...] | list[ast.stmt]) -> set[str]:
    visitor = _ScopeBindings()
    for statement in statements:
        visitor.visit(statement)
    return visitor.names


def _module_only_binding(tree: ast.Module, name: str, allowed: ast.stmt) -> bool:
    """Require one module binding for ``name``, in the supplied statement only."""
    return all(
        statement is allowed or not ({name, "*"} & _scope_bound_names([statement]))
        for statement in tree.body
    )


def _has_unshadowed_local_import(tree: ast.Module, module: str, name: str) -> bool:
    """Require one direct known import binding with no module-level rebinding."""
    matches: list[tuple[ast.ImportFrom, ast.alias]] = []
    for statement in tree.body:
        if (
            not isinstance(statement, ast.ImportFrom)
            or statement.level
            or statement.module != module
        ):
            continue
        matches.extend(
            (statement, alias)
            for alias in statement.names
            if alias.name == name and alias.asname in {None, name}
        )
    if len(matches) != 1:
        return False

    import_statement, imported_alias = matches[0]
    if not _module_only_binding(tree, name, import_statement):
        return False
    return not any(
        alias is not imported_alias and (alias.name == "*" or (alias.asname or alias.name) == name)
        for alias in import_statement.names
    )


def _function_has_shadow(function: ast.FunctionDef, name: str) -> bool:
    """Whether a function binds a name locally, including parameters and nested defs."""
    parameters = (
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    )
    if any(argument.arg == name for argument in parameters):
        return True
    if function.args.vararg is not None and function.args.vararg.arg == name:
        return True
    if function.args.kwarg is not None and function.args.kwarg.arg == name:
        return True
    return _function_has_local_write(function, name)


def _function_has_local_write(function: ast.FunctionDef, name: str) -> bool:
    """Whether a function body assigns, deletes, imports, or defines the given name."""
    return name in _scope_bound_names(function.body)


def _top_level_functions(tree: ast.Module, name: str) -> list[ast.FunctionDef]:
    return [
        statement
        for statement in tree.body
        if isinstance(statement, ast.FunctionDef) and statement.name == name
    ]


def _call_argument_zero_is(call: ast.Call, literal: str) -> ast.Constant | None:
    if not call.args or not isinstance(call.args[0], ast.Constant):
        return None
    first = call.args[0]
    return first if first.value == literal else None


def _call_keyword_is(call: ast.Call, keyword_name: str, literal: str) -> ast.Constant | None:
    matches = [keyword.value for keyword in call.keywords if keyword.arg == keyword_name]
    if (
        len(matches) == 1
        and not any(keyword.arg is None for keyword in call.keywords)
        and isinstance(matches[0], ast.Constant)
        and matches[0].value == literal
    ):
        return matches[0]
    return None


def _metadata_sites(source: str) -> dict[str, list[ast.Constant]]:
    """Find the six approved metadata contexts in their exact AST shapes."""
    tree = ast.parse(source)
    parents = _parents(tree)
    sites: dict[str, list[ast.Constant]] = {kind: [] for kind in EXPECTED_METADATA_EXCEPTIONS}

    provider_assignments = [
        statement
        for statement in tree.body
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == "PROVIDER"
        )
    ]
    if len(provider_assignments) == 1:
        assignment = provider_assignments[0]
        assigned_value = assignment.value
        if (
            _module_only_binding(tree, "PROVIDER", assignment)
            and _has_unshadowed_local_import(tree, "opendata.data.provider", "Provider")
            and isinstance(assigned_value, ast.Call)
            and isinstance(assigned_value.func, ast.Name)
            and assigned_value.func.id == "Provider"
        ):
            literal = _call_keyword_is(assigned_value, "source", "akshare")
            if literal is not None:
                sites["akshare_provider_source"].append(literal)

    register_functions = _top_level_functions(tree, "register")
    if (
        len(register_functions) == 1
        and _module_only_binding(tree, "register", register_functions[0])
        and _has_unshadowed_local_import(
            tree, "opendata.data.providers.catalog", "register_provider"
        )
        and not _function_has_shadow(register_functions[0], "register_provider")
    ):
        for statement in register_functions[0].body:
            if (
                isinstance(statement, ast.Return)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Name)
                and statement.value.func.id == "register_provider"
            ):
                literal = _call_argument_zero_is(statement.value, "akshare")
                if literal is not None:
                    sites["akshare_registration_source"].append(literal)

    getattr_functions = _top_level_functions(tree, "__getattr__")
    if (
        len(getattr_functions) == 1
        and _module_only_binding(tree, "__getattr__", getattr_functions[0])
        and _has_unshadowed_local_import(tree, "opendata.data.providers.catalog", "fetchers_for")
        and not _function_has_shadow(getattr_functions[0], "fetchers_for")
        and not _function_has_local_write(getattr_functions[0], "name")
        and getattr_functions[0].args.args
        and getattr_functions[0].args.args[0].arg == "name"
    ):
        for statement in getattr_functions[0].body:
            if not isinstance(statement, ast.If) or not (
                isinstance(statement.test, ast.Compare)
                and isinstance(statement.test.left, ast.Name)
                and statement.test.left.id == "name"
                and len(statement.test.ops) == 1
                and isinstance(statement.test.ops[0], ast.Eq)
                and len(statement.test.comparators) == 1
                and isinstance(statement.test.comparators[0], ast.Constant)
                and statement.test.comparators[0].value == "FETCHERS"
            ):
                continue
            for branch_statement in statement.body:
                if (
                    isinstance(branch_statement, ast.Return)
                    and isinstance(branch_statement.value, ast.Call)
                    and isinstance(branch_statement.value.func, ast.Name)
                    and branch_statement.value.func.id == "fetchers_for"
                ):
                    literal = _call_argument_zero_is(branch_statement.value, "akshare")
                    if literal is not None:
                        sites["akshare_fetchers_source"].append(literal)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = _dotted_name(node.func)
            if (
                callee == "entry.get"
                and _has_ancestor(node, parents, "_parse_entry")
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "openbb"
            ):
                sites["openbb_model_lookup"].append(node.args[0])
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "mapped_column"
                and _has_ancestor(node, parents, "DataScript")
            ):
                parent = parents.get(node)
                if (
                    isinstance(parent, ast.AnnAssign)
                    and isinstance(parent.target, ast.Name)
                    and parent.target.id == "source"
                    and parent.value is node
                ):
                    defaults = [
                        keyword.value for keyword in node.keywords if keyword.arg == "default"
                    ]
                    if (
                        len(defaults) == 1
                        and isinstance(defaults[0], ast.Constant)
                        and defaults[0].value == "akshare"
                    ):
                        sites["data_script_source_default"].append(defaults[0])
        elif isinstance(node, ast.Dict) and _has_ancestor(node, parents, "key_status"):
            sites["patrol_key_status_label"].extend(
                key for key in node.keys if isinstance(key, ast.Constant) and key.value == "akshare"
            )
    return sites


def _metadata_exception_problems(
    filename: str,
    source: str,
    exceptions: tuple[MetadataException, ...],
) -> list[str]:
    """Verify that each registered exemption still names its exact source AST node."""
    try:
        sites = _metadata_sites(source)
        findings = scan_source(source, filename)
    except SyntaxError as exc:
        return [f"{filename}: metadata source is not valid Python at line {exc.lineno}"]

    problems: list[str] = []
    for exception in exceptions:
        candidates = sites.get(exception.kind, [])
        if len(candidates) != 1:
            problems.append(f"{filename}: {exception.kind} must match exactly one AST site")
            continue
        node = candidates[0]
        matching = [
            finding
            for finding in findings
            if finding.line == node.lineno
            and finding.module == exception.literal
            and finding.kind == "string"
        ]
        if len(matching) != 1:
            problems.append(f"{filename}: {exception.kind} no longer matches one string finding")
    return problems


def filter_metadata_findings(
    filename: str,
    source: str,
    findings: list[Finding],
    entry: ReferenceEntry | None,
) -> list[Finding]:
    """Suppress only a policy-bound exact metadata literal and nothing nearby."""
    if entry is None or not entry.ast_exceptions:
        return findings
    problems = _metadata_exception_problems(filename, source, entry.ast_exceptions)
    if problems:
        raise ReferencePolicyError(problems[0])
    sites = _metadata_sites(source)
    remaining = list(findings)
    for exception in entry.ast_exceptions:
        candidates = sites.get(exception.kind, [])
        node = candidates[0]
        matching = [
            finding
            for finding in remaining
            if finding.file == filename
            and finding.line == node.lineno
            and finding.module == exception.literal
            and finding.kind == "string"
        ]
        remaining.remove(matching[0])
    return remaining


def target_files(target: str) -> list[Path]:
    """Every Python file of one runtime package.

    A declared target that is absent or empty is an error rather than a skip: the
    scanner once carried ``opendata_providers`` for rounds while the directory did not
    exist, and "0 files walked" looked identical to "nothing to report".
    """
    layer = PORTED if target == VENDOR_ROOT else FIRST_PARTY
    try:
        sources = iter_unique_python_files(
            REPO_ROOT,
            (target,),
            layers=frozenset({layer}),
        )
    except SourceLayoutError as exc:
        message = str(exc)
        if "not a directory" in message:
            raise ScanSurfaceError(f"{target}: declared scan target is not a directory") from exc
        raise ScanSurfaceError(f"{target}: scan target holds no Python file") from exc
    return [source.path for source in sources]


def iter_target_files(targets: tuple[str, ...]) -> list[Path]:
    """Return every Python file under the target packages."""
    try:
        sources = iter_unique_python_files(
            REPO_ROOT,
            targets,
            layers=frozenset({FIRST_PARTY, PORTED}),
        )
    except SourceLayoutError as exc:
        raise ScanSurfaceError(str(exc)) from exc
    return [source.path for source in sources]


def file_census(targets: tuple[str, ...]) -> dict[str, int]:
    """Per-package file counts, i.e. how much of the tree this reading actually saw."""
    return {target: len(target_files(target)) for target in targets}


def collect(targets: tuple[str, ...]) -> list[Finding]:
    """Collect findings for every target package."""
    policy = load_reference_policy()
    entries = policy.by_path
    findings: list[Finding] = []
    for path in iter_target_files(targets):
        rel = path.relative_to(REPO_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        findings.extend(
            filter_metadata_findings(rel, source, scan_source(source, rel), entries.get(rel))
        )
    return findings


def collect_raw(targets: tuple[str, ...]) -> list[Finding]:
    """Collect unsuppressed AST findings for diagnostics and policy counterfactuals."""
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
        try:
            identity = historical_identity(file)
        except SourceLayoutError as exc:
            raise BaselineError(f"finding path has no historical identity mapping: {file}") from exc
        entries.append((identity, module, kind))
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
    try:
        actual = count_by_identity(collect(targets))
    except ReferencePolicyError as exc:
        print(f"FAIL: reference policy is unusable: {exc}", file=sys.stderr)
        return 1
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
    try:
        current = collect(targets)
    except ReferencePolicyError as exc:
        print(f"FAIL: refusing to freeze an unusable reference policy: {exc}", file=sys.stderr)
        return 1
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
    (
        "dynamic",
        'import importlib as loader\nroot = "akshare"\nname = root + ".data"\n'
        "loader.import_module(name)\n",
    ),
    (
        "dynamic",
        "from importlib import import_module as load\n"
        'module_name = "akshare.option"\nload(module_name)\n',
    ),
    (
        "dynamic",
        "from builtins import __import__ as import_alias\n"
        'name = "openbb.providers"\nimport_alias(name)\n',
    ),
    ("string", 'MODULE = "akshare"\n'),
    ("string", 'MODULE = "akshare.data.daily"\n'),
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
    shrunk = surface_problems(frozen, {**frozen.files, VENDOR_ROOT: 9})
    if not any(SURFACE_SHRANK in line for line in shrunk):
        failures.append(f"a one-file shrink was not caught: {shrunk}")
    stale = surface_problems(frozen, {**frozen.files, VENDOR_ROOT: 11})
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
