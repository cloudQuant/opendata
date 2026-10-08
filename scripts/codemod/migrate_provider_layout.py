"""Prepare and apply the bounded provider source-layout migration.

The default command is read-only. Applying changes requires a separate JSON
report path so source hashes and per-file rollback instructions survive an
interrupted run. Only runtime Python imports and the frozen move maps are
rewritten; docs, evidence, and generated build trees are left alone.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import tempfile
import tokenize
from bisect import bisect_left
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable


DEFAULT_AKSHARE_SUFFIX_COUNTS = (
    (".py", 325),
    (".js", 1),
    (".json", 2),
    (".lock", 1),
)
DEFAULT_AKSHARE_FILE_COUNT = 329
EXPECTED_AKSHARE_SOURCE_IMPORTS = (166, 532)
AKSHARE_RESOURCE_PATHS = frozenset({"file_fold/calendar.json", "stock_feature/ths.js"})
AKSHARE_METADATA_PATHS = frozenset({"manifest.json", "upstream.lock"})

# The Fuyao package is split across the THS provider API and transport.
FUYAO_MOVE_MAP = {
    "__init__.py": "transport/__init__.py",
    "credentials.py": "transport/credentials.py",
    "envelope.py": "transport/envelope.py",
    "errors.py": "transport/errors.py",
    "http_client.py": "transport/http_client.py",
    "rate_limiter.py": "transport/rate_limiter.py",
    "error_messages.yaml": "transport/error_messages.yaml",
    "endpoints.py": "endpoints.py",
    "dumps.py": "dumps.py",
    "endpoint_map.py": "endpoint_map.py",
    "endpoint_map.yaml": "endpoint_map.yaml",
}
FUYAO_TRANSPORT_MODULES = frozenset(
    {"credentials", "envelope", "errors", "http_client", "rate_limiter"}
)
FUYAO_TRANSPORT_RESOURCES = frozenset({"error_messages.yaml"})
FUYAO_ROOT_RESOURCES = frozenset({"endpoint_map.yaml"})

_EXCLUDED_DIRS = frozenset(
    {".git", ".venv", "venv", "build", "dist", "node_modules", "__pycache__"}
)
_CONTROL_FILES = frozenset(
    {
        "scripts/codemod/migrate_provider_layout.py",
        "tests/test_provider_layout_migration.py",
    }
)
_NAMESPACE_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")


class MigrationError(RuntimeError):
    """Raised when the migration cannot prove a safe, complete transition."""


@dataclass(frozen=True)
class MigrationConfig:
    """Injectable repository paths and package namespaces."""

    repo_root: Path
    akshare_source: Path = Path("opendata_http")
    akshare_target: Path = Path("opendata/data/providers/akshare/_vendor")
    fuyao_source: Path = Path("opendata_fuyao")
    ths_target: Path = Path("opendata/data/providers/ths")
    transport_target: Path | None = None
    akshare_old_namespace: str = "opendata_http"
    akshare_new_namespace: str = "opendata.data.providers.akshare._vendor"
    fuyao_old_namespace: str = "opendata_fuyao"
    ths_new_namespace: str = "opendata.data.providers.ths"
    transport_new_namespace: str = "opendata.data.providers.ths.transport"
    expected_akshare_file_count: int | None = DEFAULT_AKSHARE_FILE_COUNT
    expected_akshare_suffix_counts: tuple[tuple[str, int], ...] | None = (
        DEFAULT_AKSHARE_SUFFIX_COUNTS
    )
    expected_source_imports: tuple[int, int] | None = EXPECTED_AKSHARE_SOURCE_IMPORTS


@dataclass(frozen=True)
class TextEdit:
    """One exact source-span replacement produced from parsed syntax."""

    start: int
    end: int
    replacement: str
    kind: str
    before: str
    after: str
    line: int


@dataclass(frozen=True)
class _DynamicModuleArgument:
    """A supported literal import/resource argument with its source node."""

    constant: ast.Constant
    call: ast.Call
    value: str
    resource_name: str | None
    canonical: str


class SourceIndex:
    """Precomputed source offsets used by AST and tokenizer positions."""

    def __init__(self, source: str) -> None:
        """Index source lines once for AST and tokenizer positions."""
        self.lines = source.splitlines(keepends=True)
        self.line_starts = [0]
        for line in self.lines:
            self.line_starts.append(self.line_starts[-1] + len(line))

    def ast_char(self, line: int, utf8_column: int) -> int:
        """Translate an AST UTF-8 byte column to a Python string offset."""
        if line < 1 or line > len(self.lines):
            raise MigrationError(f"AST position is outside source: line={line}")
        prefix = self.lines[line - 1].encode("utf-8")[:utf8_column].decode("utf-8")
        return self.line_starts[line - 1] + len(prefix)

    def token_char(self, position: tuple[int, int]) -> int:
        """Translate a tokenizer line and character column to an offset."""
        line, column = position
        return self.line_starts[line - 1] + column


class TokenIndex:
    """Token index that avoids rescanning every token for each import node."""

    def __init__(self, source: str, tokens: list[tokenize.TokenInfo]) -> None:
        """Build sorted token spans for fast per-node lookup."""
        self.source_index = SourceIndex(source)
        self.items = [
            (
                self.source_index.token_char(token.start),
                self.source_index.token_char(token.end),
                token,
            )
            for token in tokens
        ]
        self.starts = [item[0] for item in self.items]

    def within(self, start: int, end: int) -> list[tuple[int, int, tokenize.TokenInfo]]:
        """Return token spans within the requested source interval."""
        index = bisect_left(self.starts, start)
        result = []
        while index < len(self.items) and self.items[index][0] < end:
            result.append(self.items[index])
            index += 1
        return result


def _as_repo_path(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise MigrationError(f"migration paths must stay under repo_root: {relative}")
    return root / relative


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _module_in_namespace(module: str | None, namespace: str) -> bool:
    if module is None:
        return False
    return module == namespace or module.startswith(namespace + ".")


def _validate_config(config: MigrationConfig) -> MigrationConfig:
    root = config.repo_root.expanduser().resolve()
    for name in (
        "akshare_old_namespace",
        "akshare_new_namespace",
        "fuyao_old_namespace",
        "ths_new_namespace",
        "transport_new_namespace",
    ):
        value = getattr(config, name)
        if not _NAMESPACE_RE.fullmatch(value):
            raise MigrationError(f"invalid Python namespace for {name}: {value!r}")
    for name in ("akshare_source", "akshare_target", "fuyao_source", "ths_target"):
        _as_repo_path(root, Path(getattr(config, name)))
    if config.transport_target is not None:
        _as_repo_path(root, Path(config.transport_target))
    if config.akshare_source == config.akshare_target:
        raise MigrationError("AKShare source and destination paths must differ")
    if config.fuyao_source == config.ths_target:
        raise MigrationError("Fuyao source and THS destination paths must differ")
    return MigrationConfig(
        repo_root=root,
        akshare_source=Path(config.akshare_source),
        akshare_target=Path(config.akshare_target),
        fuyao_source=Path(config.fuyao_source),
        ths_target=Path(config.ths_target),
        transport_target=(
            Path(config.transport_target) if config.transport_target is not None else None
        ),
        akshare_old_namespace=config.akshare_old_namespace,
        akshare_new_namespace=config.akshare_new_namespace,
        fuyao_old_namespace=config.fuyao_old_namespace,
        ths_new_namespace=config.ths_new_namespace,
        transport_new_namespace=config.transport_new_namespace,
        expected_akshare_file_count=config.expected_akshare_file_count,
        expected_akshare_suffix_counts=config.expected_akshare_suffix_counts,
        expected_source_imports=config.expected_source_imports,
    )


def _walk_regular_files(root: Path) -> tuple[dict[str, Path], list[str]]:
    if not root.is_dir():
        raise MigrationError(f"required source directory is missing: {root}")
    files: dict[str, Path] = {}
    cache_files: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise MigrationError(f"refusing symlink in migration source: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if "__pycache__" in path.relative_to(root).parts:
            if path.suffix not in {".pyc", ".pyo"}:
                raise MigrationError(f"unexpected non-bytecode cache file: {path}")
            cache_files.append(relative)
            continue
        files[relative] = path
    return files, cache_files


def _expected_akshare_files(config: MigrationConfig) -> dict[str, Path]:
    source_root = _as_repo_path(config.repo_root, config.akshare_source)
    files, _ = _walk_regular_files(source_root)
    if config.expected_akshare_file_count is not None and (
        len(files) != config.expected_akshare_file_count
    ):
        raise MigrationError(
            "AKShare source inventory changed: "
            f"expected {config.expected_akshare_file_count} non-cache files, "
            f"found {len(files)}"
        )
    if config.expected_akshare_suffix_counts is not None:
        actual = Counter(Path(name).suffix for name in files)
        expected = dict(config.expected_akshare_suffix_counts)
        if actual != expected:
            raise MigrationError(
                f"AKShare source suffix inventory changed: expected {expected}, "
                f"found {dict(actual)}"
            )
    missing_resources = sorted(AKSHARE_RESOURCE_PATHS.difference(files))
    if missing_resources:
        raise MigrationError(f"AKShare resources are missing: {missing_resources}")
    missing_metadata = sorted(AKSHARE_METADATA_PATHS.difference(files))
    if missing_metadata:
        raise MigrationError(f"AKShare provenance files are missing: {missing_metadata}")
    if (
        config.expected_akshare_file_count == DEFAULT_AKSHARE_FILE_COUNT
        and sum(name.endswith(".py") for name in files) != 325
    ):
        raise MigrationError("AKShare Python inventory is not the frozen 325-file set")
    return files


def _fuyao_files(config: MigrationConfig) -> dict[str, Path]:
    source_root = _as_repo_path(config.repo_root, config.fuyao_source)
    files, _ = _walk_regular_files(source_root)
    expected = set(FUYAO_MOVE_MAP)
    if set(files) != expected:
        missing = sorted(expected.difference(files))
        extra = sorted(set(files).difference(expected))
        raise MigrationError(f"Fuyao source inventory mismatch; missing={missing}, extra={extra}")
    if sum(Path(name).suffix == ".py" for name in files) != 9:
        raise MigrationError("Fuyao source must contain the frozen 9 Python files")
    if sum(Path(name).suffix == ".yaml" for name in files) != 2:
        raise MigrationError("Fuyao source must contain the frozen 2 YAML resources")
    return files


def _verify_license(config: MigrationConfig) -> str:
    license_path = config.repo_root / "LICENSE-AKSHARE"
    if not license_path.is_file():
        raise MigrationError(f"root MIT license is missing: {license_path}")
    data = license_path.read_bytes()
    first_line = data.decode("utf-8", errors="replace").splitlines()[:1]
    if not first_line or first_line[0].strip() != "MIT License":
        raise MigrationError("LICENSE-AKSHARE must retain the upstream MIT license")
    if b"Business Source License" in data or b"BSL" in data[:200]:
        raise MigrationError("refusing to migrate AKShare under a BSL license header")
    return _sha256(data)


def _source_package_import_baseline(
    files: dict[str, Path], namespace: str, source_prefix: str
) -> tuple[int, int, list[dict[str, Any]]]:
    matching_files: set[str] = set()
    records: list[dict[str, Any]] = []
    for relative, path in files.items():
        if not relative.endswith(".py"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            raise MigrationError(f"cannot parse source Python file {path}: {exc}") from exc
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                line_number = node.lineno
                modules = [
                    alias.name
                    for alias in node.names
                    if _module_in_namespace(alias.name, namespace)
                ]
            elif (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and _module_in_namespace(node.module, namespace)
            ):
                line_number = node.lineno
                modules = [node.module or ""]
            else:
                continue
            if modules:
                matching_files.add(relative)
                records.append(
                    {
                        "path": f"{source_prefix}/{relative}",
                        "line": line_number,
                        "kind": type(node).__name__,
                        "modules": modules,
                    }
                )
    records.sort(key=lambda item: (item["path"], item["line"], item["kind"]))
    return len(matching_files), len(records), records


def _module_target(
    module: str | None,
    config: MigrationConfig,
    *,
    resource_name: str | None = None,
) -> str | None:
    if not module:
        return None
    if _module_in_namespace(module, config.akshare_old_namespace):
        return config.akshare_new_namespace + module[len(config.akshare_old_namespace) :]
    if module == config.fuyao_old_namespace:
        if resource_name in FUYAO_TRANSPORT_RESOURCES:
            return config.transport_new_namespace
        if resource_name in FUYAO_ROOT_RESOURCES:
            return config.ths_new_namespace
        return config.transport_new_namespace
    if _module_in_namespace(module, config.fuyao_old_namespace):
        suffix = module[len(config.fuyao_old_namespace) + 1 :]
        first, dot, rest = suffix.partition(".")
        if first in FUYAO_TRANSPORT_MODULES:
            target = config.transport_new_namespace + "." + first
        else:
            target = config.ths_new_namespace + "." + first
        return target + (dot + rest if dot else "")
    return None


def _fuyao_transport_exports(source_init: Path) -> set[str]:
    try:
        tree = ast.parse(source_init.read_text(encoding="utf-8"), filename=str(source_init))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise MigrationError(f"cannot parse Fuyao package exports: {exc}") from exc
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 1
            and node.module in FUYAO_TRANSPORT_MODULES
        ):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _fuyao_root_target(name: str, transport_exports: set[str], config: MigrationConfig) -> str:
    if name == "*" or name in transport_exports or name in FUYAO_TRANSPORT_MODULES:
        return config.transport_new_namespace
    return config.ths_new_namespace


def _fuyao_destination(config: MigrationConfig, source_relative: str) -> Path:
    mapped = Path(FUYAO_MOVE_MAP[source_relative])
    if mapped.parts[0] == "transport":
        transport_root = config.transport_target or config.ths_target / "transport"
        return transport_root / Path(*mapped.parts[1:])
    return config.ths_target / mapped


def _dotted_expression(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_expression(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _call_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    aliases[alias.asname or "importlib"] = "importlib"
                elif alias.name == "importlib.resources":
                    aliases[alias.asname or "resources"] = "importlib.resources"
                elif alias.name == "importlib.util":
                    aliases[alias.asname or "util"] = "importlib.util"
                elif alias.name == "pkg_resources":
                    aliases[alias.asname or "pkg_resources"] = "pkg_resources"
                elif alias.name == "builtins":
                    aliases[alias.asname or "builtins"] = "builtins"
        elif isinstance(node, ast.ImportFrom):
            if node.module == "importlib":
                for alias in node.names:
                    if alias.name == "import_module":
                        aliases[alias.asname or alias.name] = "importlib.import_module"
                    elif alias.name == "resources":
                        aliases[alias.asname or alias.name] = "importlib.resources"
                    elif alias.name == "util":
                        aliases[alias.asname or alias.name] = "importlib.util"
            elif node.module == "importlib.resources":
                for alias in node.names:
                    if alias.name in {
                        "files",
                        "path",
                        "read_binary",
                        "read_text",
                        "open_binary",
                        "open_text",
                    }:
                        aliases[alias.asname or alias.name] = "importlib.resources." + alias.name
            elif node.module == "importlib.util":
                for alias in node.names:
                    if alias.name == "find_spec":
                        aliases[alias.asname or alias.name] = "importlib.util.find_spec"
            elif node.module == "builtins":
                for alias in node.names:
                    if alias.name == "__import__":
                        aliases[alias.asname or alias.name] = "builtins.__import__"
            elif node.module == "pkg_resources":
                for alias in node.names:
                    if alias.name == "resource_filename":
                        aliases[alias.asname or alias.name] = "pkg_resources.resource_filename"
    return aliases


def _canonical_call_name(node: ast.expr, aliases: dict[str, str]) -> str | None:
    dotted = _dotted_expression(node)
    if dotted is None:
        return None
    root, separator, suffix = dotted.partition(".")
    canonical = aliases.get(root, root)
    return canonical + ("." + suffix if separator else "")


def _resource_filename_from_parent(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> str | None:
    if len(call.args) > 1 and isinstance(call.args[1], ast.Constant):
        value = call.args[1].value
        if isinstance(value, str):
            return Path(value).name
    parent = parents.get(call)
    if (
        isinstance(parent, ast.BinOp)
        and isinstance(parent.op, ast.Div)
        and parent.left is call
        and isinstance(parent.right, ast.Constant)
        and isinstance(parent.right.value, str)
    ):
        return Path(parent.right.value).name
    attribute = parents.get(call)
    if (
        isinstance(attribute, ast.Attribute)
        and attribute.attr == "joinpath"
        and attribute.value is call
    ):
        parent_call = parents.get(attribute)
        if isinstance(parent_call, ast.Call) and parent_call.func is attribute:
            components = [
                arg.value
                for arg in parent_call.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            if components:
                return Path(*components).name
    return None


def _dynamic_module_arguments(
    tree: ast.AST,
) -> list[_DynamicModuleArgument]:
    aliases = _call_aliases(tree)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    dynamic_names = {
        "importlib.import_module",
        "__import__",
        "builtins.__import__",
        "importlib.util.find_spec",
    }
    resource_names = {
        "importlib.resources.files",
        "importlib.resources.path",
        "importlib.resources.read_binary",
        "importlib.resources.read_text",
        "importlib.resources.open_binary",
        "importlib.resources.open_text",
        "pkg_resources.resource_filename",
    }
    results: list[_DynamicModuleArgument] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        canonical = _canonical_call_name(node.func, aliases)
        if canonical not in dynamic_names and canonical not in resource_names:
            continue
        argument = node.args[0]
        if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
            continue
        resource_name = (
            _resource_filename_from_parent(node, parents) if canonical in resource_names else None
        )
        results.append(
            _DynamicModuleArgument(
                constant=argument,
                call=node,
                value=argument.value,
                resource_name=resource_name,
                canonical=canonical or "",
            )
        )
    return results


def _tokenize_source(source: str, filename: str) -> list[tokenize.TokenInfo]:
    try:
        return list(tokenize.generate_tokens(StringIO(source).readline))
    except tokenize.TokenError as exc:
        raise MigrationError(f"cannot tokenize {filename}: {exc}") from exc


def _importfrom_module_span(
    node: ast.ImportFrom, token_index: TokenIndex, source_index: SourceIndex
) -> tuple[int, int]:
    node_start = source_index.ast_char(node.lineno, node.col_offset)
    node_end = source_index.ast_char(
        node.end_lineno or node.lineno, node.end_col_offset or node.col_offset
    )
    local = [
        (start, end, token)
        for start, end, token in token_index.within(node_start, node_end)
        if token.type
        not in {
            tokenize.ENCODING,
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.COMMENT,
            tokenize.ENDMARKER,
        }
    ]
    from_index = next(
        (
            index
            for index, token in enumerate(local)
            if token[2].type == tokenize.NAME and token[2].string == "from"
        ),
        None,
    )
    if from_index is None:
        raise MigrationError(f"cannot locate from token at line {node.lineno}")
    import_index = next(
        (
            index
            for index in range(from_index + 1, len(local))
            if local[index][2].type == tokenize.NAME and local[index][2].string == "import"
        ),
        None,
    )
    if import_index is None or import_index == from_index + 1:
        raise MigrationError(f"cannot locate module token at line {node.lineno}")
    return local[from_index + 1][0], local[import_index - 1][1]


def _statement_edit(
    source: str,
    node: ast.stmt,
    statements: list[str],
    *,
    source_index: SourceIndex,
    kind: str,
    before: str,
    after: str,
) -> TextEdit:
    start = source_index.ast_char(node.lineno, node.col_offset)
    end = source_index.ast_char(
        node.end_lineno or node.lineno,
        node.end_col_offset or node.col_offset,
    )
    line_start = source_index.line_starts[node.lineno - 1]
    prefix = source[line_start:start]
    return TextEdit(
        start,
        end,
        ("\n" + prefix).join(statements),
        kind,
        before,
        after,
        node.lineno,
    )


def _replace_string_token(
    source: str,
    node: ast.Constant,
    old_value: str,
    new_value: str,
    old_namespace: str,
    source_index: SourceIndex,
) -> tuple[int, int, str]:
    start = source_index.ast_char(node.lineno, node.col_offset)
    end = source_index.ast_char(
        node.end_lineno or node.lineno,
        node.end_col_offset or node.col_offset,
    )
    original = source[start:end]
    match = re.match(r"(?i)^([rub]*)(\"\"\"|'''|\"|')", original)
    if match:
        prefix, quote = match.groups()
        if original.endswith(quote):
            body = original[len(prefix) + len(quote) : -len(quote)]
            suffix = old_value[len(old_namespace) :]
            new_prefix = new_value[: -len(suffix)] if suffix else new_value
            if old_namespace in body:
                body = body.replace(old_namespace, new_prefix, 1)
                return start, end, prefix + quote + body + quote
    return start, end, repr(new_value)


def _rewrite_source(
    source: str,
    *,
    filename: str,
    config: MigrationConfig,
    transport_exports: set[str],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise MigrationError(f"cannot parse Python file {filename}: {exc}") from exc
    tokens = _tokenize_source(source, filename)
    source_index = SourceIndex(source)
    token_index = TokenIndex(source, tokens)
    edits: list[TextEdit] = []
    import_records: list[dict[str, Any]] = []
    dynamic_records: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            old_aliases = [
                alias for alias in node.names if _module_target(alias.name, config) is not None
            ]
            if not old_aliases:
                continue
            before_modules = [alias.name for alias in old_aliases]
            after_modules = [
                _module_target(alias.name, config) or alias.name for alias in old_aliases
            ]
            statements: list[str] = []
            imported_roots: set[str] = set()
            for alias in node.names:
                mapped = _module_target(alias.name, config)
                if mapped is None:
                    start = source_index.ast_char(alias.lineno, alias.col_offset)
                    end = source_index.ast_char(
                        alias.end_lineno or alias.lineno,
                        alias.end_col_offset or alias.col_offset,
                    )
                    statements.append("import " + source[start:end])
                    continue
                if alias.asname:
                    statements.append(f"import {mapped} as {alias.asname}")
                    continue
                old_root = alias.name.split(".", 1)[0]
                new_root = _module_target(old_root, config) or mapped
                if alias.name == old_root:
                    statements.append(f"import {new_root} as {old_root}")
                    imported_roots.add(old_root)
                    continue
                # A bare dotted import binds the original top-level package.
                if old_root not in imported_roots:
                    statements.append(f"import {new_root} as {old_root}")
                    imported_roots.add(old_root)
                parent, _, leaf = mapped.rpartition(".")
                statements.append(f"from {parent} import {leaf}")
            edits.append(
                _statement_edit(
                    source,
                    node,
                    statements,
                    source_index=source_index,
                    kind="ast_import",
                    before=", ".join(before_modules),
                    after=", ".join(after_modules),
                )
            )
            import_records.append(
                {
                    "line": node.lineno,
                    "kind": "Import",
                    "modules_before": before_modules,
                    "modules_after": after_modules,
                    "names": [alias.asname or alias.name for alias in old_aliases],
                }
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mapped = _module_target(node.module, config)
            if mapped is None:
                continue
            if node.module == config.fuyao_old_namespace:
                groups: dict[str, list[ast.alias]] = {}
                for alias in node.names:
                    target = _fuyao_root_target(alias.name, transport_exports, config)
                    groups.setdefault(target, []).append(alias)
                if len(groups) > 1:
                    statements = []
                    for target_module, aliases in groups.items():
                        rendered = [
                            alias.name + (f" as {alias.asname}" if alias.asname else "")
                            for alias in aliases
                        ]
                        statements.append(f"from {target_module} import " + ", ".join(rendered))
                    edits.append(
                        _statement_edit(
                            source,
                            node,
                            statements,
                            source_index=source_index,
                            kind="ast_importfrom_split",
                            before=node.module,
                            after=", ".join(groups),
                        )
                    )
                    import_records.append(
                        {
                            "line": node.lineno,
                            "kind": "ImportFrom",
                            "modules_before": [node.module],
                            "modules_after": list(groups),
                            "names": [alias.name for alias in node.names],
                        }
                    )
                    continue
                if groups:
                    mapped = next(iter(groups))
            start, end = _importfrom_module_span(node, token_index, source_index)
            edits.append(
                TextEdit(
                    start,
                    end,
                    mapped,
                    "ast_importfrom",
                    node.module,
                    mapped,
                    node.lineno,
                )
            )
            import_records.append(
                {
                    "line": node.lineno,
                    "kind": "ImportFrom",
                    "modules_before": [node.module],
                    "modules_after": [mapped],
                    "names": [alias.name for alias in node.names],
                }
            )

    for argument in _dynamic_module_arguments(tree):
        constant = argument.constant
        old_value = argument.value
        resource_name = argument.resource_name
        canonical = argument.canonical
        if (
            (
                canonical.startswith("importlib.resources.")
                or canonical == "pkg_resources.resource_filename"
            )
            and old_value == config.fuyao_old_namespace
            and resource_name is None
        ):
            raise MigrationError(
                f"cannot determine Fuyao resource destination in {filename} line {constant.lineno}"
            )
        dynamic_target = _module_target(old_value, config, resource_name=resource_name)
        if dynamic_target is None or dynamic_target == old_value:
            continue
        old_namespace = next(
            namespace
            for namespace in (
                config.akshare_old_namespace,
                config.fuyao_old_namespace,
            )
            if _module_in_namespace(old_value, namespace)
        )
        start, end, replacement = _replace_string_token(
            source, constant, old_value, dynamic_target, old_namespace, source_index
        )
        edits.append(
            TextEdit(
                start,
                end,
                replacement,
                "dynamic_module_string",
                old_value,
                dynamic_target,
                constant.lineno,
            )
        )
        dynamic_records.append(
            {
                "line": constant.lineno,
                "call": canonical,
                "before": old_value,
                "after": dynamic_target,
                "resource_name": resource_name,
            }
        )

    edits.sort(key=lambda edit: (edit.start, edit.end), reverse=True)
    last_start = len(source) + 1
    rewritten = source
    for edit in edits:
        if edit.end > last_start:
            raise MigrationError(f"overlapping AST edits in {filename} near line {edit.line}")
        rewritten = rewritten[: edit.start] + edit.replacement + rewritten[edit.end :]
        last_start = edit.start
    try:
        ast.parse(rewritten, filename=filename)
    except SyntaxError as exc:
        raise MigrationError(f"AST rewrite produced invalid Python in {filename}: {exc}") from exc
    import_records.sort(key=lambda item: (item["line"], item["kind"]))
    dynamic_records.sort(key=lambda item: item["line"])
    return rewritten, import_records, dynamic_records


def _module_path_exists(source_root: Path, module: str, namespace: str) -> bool:
    if module == namespace:
        return (source_root / "__init__.py").is_file()
    suffix = module[len(namespace) + 1 :]
    path = source_root.joinpath(*suffix.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def _lazy_vendor_init(old_init: Path, config: MigrationConfig) -> tuple[bytes, dict[str, Any]]:
    try:
        source_bytes = old_init.read_bytes()
        source = source_bytes.decode("utf-8")
        tree = ast.parse(source, filename=str(old_init))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise MigrationError(f"cannot parse AKShare root export table: {exc}") from exc
    source_header_lines = source_bytes.splitlines(keepends=True)[:2]
    if len(source_header_lines) != 2 or any(
        not line.lstrip().startswith(b"#") for line in source_header_lines
    ):
        raise MigrationError("AKShare root must begin with its two-line upstream provenance header")
    source_header = b"".join(source_header_lines)
    entries: dict[str, tuple[str, str, bool]] = {}
    duplicate_names: list[str] = []
    nodes = sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    for node in nodes:
        mapped_module: str | None = None
        if node.level == 1:
            relative = node.module or ""
            mapped_module = config.akshare_new_namespace + ("." + relative if relative else "")
        elif node.level == 0 and _module_in_namespace(node.module, config.akshare_old_namespace):
            mapped_module = _module_target(node.module, config)
        elif node.level == 0 and node.module == "akqmt":
            mapped_module = "akqmt"
        else:
            continue
        if mapped_module is None:
            raise MigrationError(f"cannot map package-level export module {node.module!r}")
        for alias in node.names:
            if alias.name == "*":
                raise MigrationError(
                    "AKShare root contains star exports; lazy map is not enumerable"
                )
            export = alias.asname or alias.name
            if export in entries:
                duplicate_names.append(export)
            entries[export] = (mapped_module, alias.name, node.module == "akqmt")

    for export, (module, _symbol, optional) in entries.items():
        if optional:
            continue
        if not module.startswith(config.akshare_new_namespace):
            raise MigrationError(f"unexpected non-vendored root export {export!r} from {module!r}")
        source_module = config.akshare_old_namespace + module[len(config.akshare_new_namespace) :]
        if not _module_path_exists(old_init.parent, source_module, config.akshare_old_namespace):
            raise MigrationError(
                f"root export {export!r} points to missing source module {source_module!r}"
            )

    rendered_entries = ",\n".join(
        f"    {name!r}: ({module!r}, {symbol!r}, {optional!r})"
        for name, (module, symbol, optional) in entries.items()
    )
    public_names = [name for name in entries if not name.startswith("_")]
    rendered_public = ",\n".join(f"    {name!r}" for name in public_names)
    text = (
        "# Migration note: lazy compatibility facade; "
        "license text remains in root LICENSE-AKSHARE.\n"
        '"""Lazy flat compatibility facade for the migrated AKShare source tree."""\n'
        "from importlib import import_module as _import_module\n\n"
        "_EXPORTS = {\n"
        f"{rendered_entries}\n"
        "}\n"
        "__all__ = (\n"
        f"{rendered_public}\n"
        ")\n\n"
        "def __getattr__(name):\n"
        "    try:\n"
        "        module_name, symbol_name, optional = _EXPORTS[name]\n"
        "    except KeyError:\n"
        "        raise AttributeError("
        f'f"module {{__name__!r}} has no attribute {{name!r}}") from None\n'
        "    try:\n"
        "        module = _import_module(module_name)\n"
        "    except ImportError as exc:\n"
        "        if optional:\n"
        "            raise AttributeError("
        f'f"module {{__name__!r}} has no attribute {{name!r}}") from exc\n'
        "        raise\n"
        "    value = getattr(module, symbol_name)\n"
        "    globals()[name] = value\n"
        "    return value\n\n"
        "def __dir__():\n"
        "    return sorted(set(globals()) | set(_EXPORTS))\n"
    )
    ast.parse(text)
    return source_header + text.encode("utf-8"), {
        "export_count": len(entries),
        "public_export_count": len(public_names),
        "duplicate_export_names": sorted(set(duplicate_names)),
        "preserved_source_header_line_count": len(source_header_lines),
        "preserved_source_header_sha256": _sha256(source_header),
        "preserved_source_header_bytes_hex": source_header.hex(),
        "exports": {
            name: {"module": module, "symbol": symbol, "optional": optional}
            for name, (module, symbol, optional) in entries.items()
        },
    }


def _python_files_for_rewrite(config: MigrationConfig) -> list[Path]:
    result: list[Path] = []
    for path in sorted(config.repo_root.rglob("*.py")):
        relative = path.relative_to(config.repo_root).as_posix()
        parts = Path(relative).parts
        if any(part in _EXCLUDED_DIRS for part in parts):
            continue
        if parts and parts[0] == "docs":
            continue
        if relative in _CONTROL_FILES or not path.is_file():
            continue
        result.append(path)
    return result


def _count_import_nodes(
    files: Iterable[tuple[str, str]], config: MigrationConfig
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for relative, source in files:
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError as exc:
            raise MigrationError(f"cannot parse in-scope Python file {relative}: {exc}") from exc
        for node in ast.walk(tree):
            found: list[str] = []
            if isinstance(node, ast.Import):
                found = [
                    alias.name
                    for alias in node.names
                    if _module_target(alias.name, config) is not None
                ]
            elif (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and _module_target(node.module, config) is not None
            ):
                found = [node.module or ""]
            for old in found:
                row = rows.setdefault(old.split(".", 1)[0], {"files": set(), "nodes": 0})
                row["files"].add(relative)
                row["nodes"] += 1
    return {
        namespace: {
            "files": len(row["files"]),
            "import_nodes": row["nodes"],
        }
        for namespace, row in rows.items()
    }


def _inventory_record(files: dict[str, Path]) -> dict[str, Any]:
    return {
        "non_cache_file_count": len(files),
        "suffix_counts": dict(sorted(Counter(Path(name).suffix for name in files).items())),
        "resource_sha256": {
            name: _sha256(path.read_bytes())
            for name, path in sorted(files.items())
            if name in AKSHARE_RESOURCE_PATHS
        },
    }


def _embedded_provenance(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"format": "non-json", "declared_file_count": None}
    files = value.get("files")
    declared: dict[str, str] = {}
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                digest = item.get("sha256")
                if isinstance(digest, str):
                    declared[item["path"]] = digest
    upstream = value.get("upstream")
    return {
        "format": "json",
        "declared_file_count": len(files) if isinstance(files, list) else None,
        "declared_upstream_commit": (
            upstream.get("commit") if isinstance(upstream, dict) else None
        ),
        "declared_upstream_sha256_by_path": declared,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _load_report(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"cannot read prior migration report {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MigrationError("prior migration report is not a JSON object")
    return value


def _config_fingerprint(config: MigrationConfig) -> dict[str, Any]:
    return {
        "repo_root": str(config.repo_root),
        "akshare_source": config.akshare_source.as_posix(),
        "akshare_target": config.akshare_target.as_posix(),
        "fuyao_source": config.fuyao_source.as_posix(),
        "ths_target": config.ths_target.as_posix(),
        "transport_target": (
            config.transport_target.as_posix() if config.transport_target is not None else None
        ),
        "akshare_old_namespace": config.akshare_old_namespace,
        "akshare_new_namespace": config.akshare_new_namespace,
        "fuyao_old_namespace": config.fuyao_old_namespace,
        "ths_new_namespace": config.ths_new_namespace,
        "transport_new_namespace": config.transport_new_namespace,
    }


def _verify_complete_report(config: MigrationConfig, prior: dict[str, Any]) -> dict[str, Any]:
    if prior.get("status") != "complete":
        raise MigrationError(
            "a prior migration report is incomplete; inspect its rollback map before retrying"
        )
    if prior.get("config") != _config_fingerprint(config):
        raise MigrationError("prior report belongs to a different migration configuration")
    if _verify_license(config) != prior.get("root_mit_license", {}).get("sha256"):
        raise MigrationError("root LICENSE-AKSHARE changed since the migration report")
    for root_rel in (config.akshare_source, config.fuyao_source):
        root = _as_repo_path(config.repo_root, root_rel)
        if root.exists():
            raise MigrationError(
                f"complete report conflicts with a still-present source directory: {root}"
            )
    for row in prior.get("moves", []):
        destination = config.repo_root / row["destination"]
        if not destination.is_file():
            raise MigrationError(f"migrated destination is missing: {destination}")
        if _sha256(destination.read_bytes()) != row.get("migrated_sha256"):
            raise MigrationError(f"migrated destination changed since the report: {destination}")
    for row in prior.get("rewrites", {}).get("external_python_files_changed", []):
        destination = config.repo_root / row["path"]
        if not destination.is_file():
            raise MigrationError(f"rewritten Python file is missing: {destination}")
        if _sha256(destination.read_bytes()) != row.get("migrated_sha256"):
            raise MigrationError(f"rewritten Python file changed since the report: {destination}")
    _verify_after_apply(config)
    return {**prior, "idempotent": True}


def _build_plan(
    config: MigrationConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_root = _as_repo_path(config.repo_root, config.akshare_source)
    fuyao_root = _as_repo_path(config.repo_root, config.fuyao_source)
    akshare_files = _expected_akshare_files(config)
    fuyao_files = _fuyao_files(config)
    license_sha = _verify_license(config)
    _, ak_caches = _walk_regular_files(source_root)
    _, fu_caches = _walk_regular_files(fuyao_root)

    akshare_target_root = _as_repo_path(config.repo_root, config.akshare_target)
    if akshare_target_root.exists():
        raise MigrationError(
            f"AKShare destination already exists; refusing collision: {akshare_target_root}"
        )
    akshare_moves = [
        (
            path.relative_to(config.repo_root).as_posix(),
            (config.akshare_target / relative).as_posix(),
            path,
        )
        for relative, path in sorted(akshare_files.items())
    ]
    fuyao_moves = []
    for relative, path in sorted(fuyao_files.items()):
        destination_rel = _fuyao_destination(config, relative)
        destination = _as_repo_path(config.repo_root, destination_rel)
        if destination.exists():
            raise MigrationError(f"Fuyao destination collision; refusing overwrite: {destination}")
        fuyao_moves.append(
            (path.relative_to(config.repo_root).as_posix(), destination_rel.as_posix(), path)
        )

    source_import_files, source_import_nodes, source_import_records = (
        _source_package_import_baseline(
            akshare_files,
            config.akshare_old_namespace,
            config.akshare_source.as_posix(),
        )
    )
    if (
        config.expected_source_imports is not None
        and (source_import_files, source_import_nodes) != config.expected_source_imports
    ):
        raise MigrationError(
            "AKShare source import baseline changed: "
            f"expected {config.expected_source_imports[0]} files/"
            f"{config.expected_source_imports[1]} AST nodes, found "
            f"{source_import_files}/{source_import_nodes}"
        )
    transport_exports = _fuyao_transport_exports(fuyao_files["__init__.py"])

    candidate_files = _python_files_for_rewrite(config)
    before_sources = [
        (path.relative_to(config.repo_root).as_posix(), path.read_text(encoding="utf-8"))
        for path in candidate_files
    ]
    before_counts = _count_import_nodes(before_sources, config)

    source_to_destination: dict[Path, Path] = {}
    for _source_rel, destination_relative, source in akshare_moves + fuyao_moves:
        destination = _as_repo_path(config.repo_root, Path(destination_relative))
        if destination.exists():
            raise MigrationError(
                f"migration destination collision; refusing overwrite: {destination}"
            )
        source_to_destination[source] = destination

    planned_writes: dict[Path, bytes] = {}
    moved_records: list[dict[str, Any]] = []
    import_records: list[dict[str, Any]] = []
    dynamic_records: list[dict[str, Any]] = []
    lazy_export_report: dict[str, Any] | None = None
    lazy_init_source = akshare_files.get("__init__.py")

    for source_path, destination_path in sorted(
        source_to_destination.items(), key=lambda pair: str(pair[0])
    ):
        source_rel = source_path.relative_to(config.repo_root).as_posix()
        if source_path == lazy_init_source:
            content, lazy_export_report = _lazy_vendor_init(source_path, config)
            _unused_rewrite, imports, dynamic = _rewrite_source(
                source_path.read_text(encoding="utf-8"),
                filename=source_rel,
                config=config,
                transport_exports=transport_exports,
            )
            imports = [
                {**record, "replacement_strategy": "generated_lazy_facade"} for record in imports
            ]
        elif source_path.suffix == ".py":
            rewritten, imports, dynamic = _rewrite_source(
                source_path.read_text(encoding="utf-8"),
                filename=source_rel,
                config=config,
                transport_exports=transport_exports,
            )
            content = rewritten.encode("utf-8")
        else:
            content = source_path.read_bytes()
            imports = []
            dynamic = []
        planned_writes[destination_path] = content
        source_rel_in_package = (
            source_rel.removeprefix(config.akshare_source.as_posix() + "/")
            if source_rel.startswith(config.akshare_source.as_posix() + "/")
            else source_rel.removeprefix(config.fuyao_source.as_posix() + "/")
        )
        kind = "python" if source_path.suffix == ".py" else "resource"
        if source_rel_in_package in AKSHARE_METADATA_PATHS:
            kind = "provenance_metadata"
        source_bytes = source_path.read_bytes()
        moved_records.append(
            {
                "source": source_rel,
                "destination": destination_path.relative_to(config.repo_root).as_posix(),
                "kind": kind,
                "source_sha256": _sha256(source_bytes),
                "migrated_sha256": _sha256(content),
                "source_mode": oct(source_path.stat().st_mode & 0o777),
                "upstream_sha256_claims_preserved": (kind == "provenance_metadata"),
                "rollback": {
                    "restore_source_from_destination": True,
                    "remove_destination_only_if_sha256_matches": _sha256(content),
                },
            }
        )
        import_records.extend({"path": source_rel, **record} for record in imports)
        dynamic_records.extend({"path": source_rel, **record} for record in dynamic)

    moved_sources = set(source_to_destination)
    external_records: list[dict[str, Any]] = []
    for source_path in candidate_files:
        if source_path in moved_sources:
            continue
        source_text = source_path.read_text(encoding="utf-8")
        relative = source_path.relative_to(config.repo_root).as_posix()
        rewritten, imports, dynamic = _rewrite_source(
            source_text,
            filename=relative,
            config=config,
            transport_exports=transport_exports,
        )
        if rewritten == source_text:
            continue
        content = rewritten.encode("utf-8")
        planned_writes[source_path] = content
        import_records.extend({"path": relative, **record} for record in imports)
        dynamic_records.extend({"path": relative, **record} for record in dynamic)
        external_records.append(
            {
                "path": relative,
                "source_sha256": _sha256(source_text.encode("utf-8")),
                "migrated_sha256": _sha256(content),
                "rollback": {
                    "restore_original_content_if_sha256_matches": _sha256(content),
                    "original_sha256": _sha256(source_text.encode("utf-8")),
                },
            }
        )

    metadata: dict[str, Any] = {}
    for name in sorted(AKSHARE_METADATA_PATHS):
        path = akshare_files[name]
        destination = _as_repo_path(config.repo_root, config.akshare_target / name)
        metadata[name] = {
            "source_file_sha256": _sha256(path.read_bytes()),
            "migrated_file_sha256": _sha256(planned_writes[destination]),
            "embedded_provenance": _embedded_provenance(path),
        }

    report = {
        "schema_version": 1,
        "status": "planned",
        "idempotent": False,
        "config": _config_fingerprint(config),
        "inventory": {
            "akshare": _inventory_record(akshare_files),
            "fuyao": {
                "non_cache_file_count": len(fuyao_files),
                "python_file_count": 9,
                "yaml_file_count": 2,
                "resource_sha256": {
                    name: _sha256(path.read_bytes())
                    for name, path in sorted(fuyao_files.items())
                    if path.suffix == ".yaml"
                },
            },
        },
        "root_mit_license": {
            "path": "LICENSE-AKSHARE",
            "sha256": license_sha,
            "header": "MIT License",
            "copied_to_vendor_tree": False,
        },
        "legacy_imports_before": {
            "all_in_scope": before_counts,
            "akshare_source_package": {
                "files": source_import_files,
                "import_nodes": source_import_nodes,
                "nodes": source_import_records,
            },
        },
        "rewrites": {
            "ast_import_nodes": import_records,
            "dynamic_module_strings": dynamic_records,
            "external_python_files_changed": external_records,
        },
        "lazy_vendor_facade": lazy_export_report,
        "manifest_and_lock_provenance": metadata,
        "moves": moved_records,
        "coverage_identity_mapping": {
            "pre_migration_first_party_baseline": {
                "total_python_file_identities": 213,
                "opendata": 204,
                "fuyao": 9,
                "ported": 0,
            },
            "fuyao_first_party_preserved": [
                {
                    "source": row["source"],
                    "destination": row["destination"],
                    "source_sha256": row["source_sha256"],
                    "migrated_sha256": row["migrated_sha256"],
                    "coverage_scope": "first_party_provider",
                }
                for row in moved_records
                if row["kind"] == "python"
                and row["source"].startswith(config.fuyao_source.as_posix() + "/")
            ],
            "akshare_vendor_separate_audit_excluded_from_first_party": [
                {
                    "source": row["source"],
                    "destination": row["destination"],
                    "source_sha256": row["source_sha256"],
                    "migrated_sha256": row["migrated_sha256"],
                    "coverage_scope": "vendored_dependency_separate_audit",
                }
                for row in moved_records
                if row["kind"] == "python"
                and row["source"].startswith(config.akshare_source.as_posix() + "/")
            ],
        },
        "rollback_map": [
            {
                "source": row["source"],
                "destination": row["destination"],
                "source_sha256": row["source_sha256"],
                "migrated_sha256": row["migrated_sha256"],
                "action": (
                    "restore source bytes; remove destination only when migrated_sha256 matches"
                ),
            }
            for row in moved_records
        ]
        + [
            {
                "source": row["path"],
                "destination": row["path"],
                "source_sha256": row["source_sha256"],
                "migrated_sha256": row["migrated_sha256"],
                "action": ("restore original bytes only when migrated_sha256 matches"),
            }
            for row in external_records
        ],
        "cache_files_removed_on_apply": {
            "opendata_http": ak_caches,
            "opendata_fuyao": fu_caches,
        },
        "removed_source_paths": [],
    }
    return report, {
        "writes": planned_writes,
        "source_files": akshare_files,
        "fuyao_files": fuyao_files,
        "source_to_destination": source_to_destination,
        "external_records": external_records,
    }


def _verify_after_apply(config: MigrationConfig) -> dict[str, Any]:
    candidates = _python_files_for_rewrite(config)
    sources = [
        (path.relative_to(config.repo_root).as_posix(), path.read_text(encoding="utf-8"))
        for path in candidates
    ]
    counts = _count_import_nodes(sources, config)
    if counts:
        raise MigrationError(f"legacy AST imports remain after apply: {counts}")
    return counts


def _remove_moved_sources(
    config: MigrationConfig,
    source_files: dict[str, Path],
    fuyao_files: dict[str, Path],
) -> list[str]:
    removed: list[str] = []
    for files in (source_files, fuyao_files):
        for path in files.values():
            path.unlink()
            removed.append(path.relative_to(config.repo_root).as_posix())
    for relative_root in (config.akshare_source, config.fuyao_source):
        root = _as_repo_path(config.repo_root, relative_root)
        _, caches = _walk_regular_files(root)
        for relative in caches:
            path = root / relative
            path.unlink()
            removed.append(path.relative_to(config.repo_root).as_posix())
        for directory in sorted(
            (path for path in root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            if not any(directory.iterdir()):
                directory.rmdir()
        if root.exists() and not any(root.iterdir()):
            root.rmdir()
        if root.exists():
            raise MigrationError(f"source directory remains after move: {root}")
    return removed


def migrate_repository(
    config: MigrationConfig,
    *,
    apply: bool = False,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Plan the migration or apply it to the injected repository root."""
    config = _validate_config(config)
    if apply and report_path is None:
        raise MigrationError("--apply requires --report for recovery evidence")
    if report_path is not None:
        report_path = report_path.expanduser().resolve()
        if report_path.exists():
            prior = _load_report(report_path)
            if prior.get("status") == "complete":
                return _verify_complete_report(config, prior)
            raise MigrationError(
                "an earlier report exists but is not complete; inspect its rollback map"
            )
        if apply:
            if report_path.suffix == ".py":
                raise MigrationError("migration report path cannot be a Python source file")
            for relative in (
                config.akshare_source,
                config.akshare_target,
                config.fuyao_source,
                config.ths_target,
            ):
                protected = _as_repo_path(config.repo_root, relative).resolve()
                if report_path == protected or protected in report_path.parents:
                    raise MigrationError(
                        f"migration report must be outside protected tree {protected}"
                    )

    report, plan = _build_plan(config)
    if not apply:
        if report_path is not None:
            _write_report(report_path, report)
        return report

    if report_path is None:
        raise MigrationError("--apply requires --report for recovery evidence")
    report["status"] = "prepared"
    _write_report(report_path, report)
    stage = Path(tempfile.mkdtemp(prefix=".provider-layout-stage-", dir=config.repo_root))
    try:
        staged: dict[Path, Path] = {}
        for destination, content in plan["writes"].items():
            relative = destination.relative_to(config.repo_root)
            staged_path = stage / relative
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            staged_path.write_bytes(content)
            staged[destination] = staged_path
        for row in report["moves"]:
            source = config.repo_root / row["source"]
            destination = config.repo_root / row["destination"]
            staged[destination].chmod(source.stat().st_mode & 0o777)

        akshare_destination = _as_repo_path(config.repo_root, config.akshare_target)
        staged_akshare = stage / config.akshare_target
        akshare_destination.parent.mkdir(parents=True, exist_ok=True)
        if akshare_destination.exists():
            raise MigrationError(
                f"destination appeared during apply; refusing overwrite: {akshare_destination}"
            )
        os.replace(staged_akshare, akshare_destination)

        for row in report["moves"]:
            destination_rel = Path(row["destination"])
            if destination_rel.is_relative_to(config.akshare_target):
                continue
            destination = config.repo_root / destination_rel
            staged_path = staged[destination]
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise MigrationError(
                    f"destination appeared during apply; refusing overwrite: {destination}"
                )
            os.replace(staged_path, destination)

        for row in plan["external_records"]:
            destination = config.repo_root / row["path"]
            staged_path = staged[destination]
            if _sha256(destination.read_bytes()) != row["source_sha256"]:
                raise MigrationError(
                    f"Python source changed during migration; refusing overwrite: {destination}"
                )
            os.replace(staged_path, destination)

        for row in report["moves"]:
            destination = config.repo_root / row["destination"]
            if _sha256(destination.read_bytes()) != row["migrated_sha256"]:
                raise MigrationError(f"destination hash mismatch after write: {destination}")
        for row in plan["external_records"]:
            destination = config.repo_root / row["path"]
            if _sha256(destination.read_bytes()) != row["migrated_sha256"]:
                raise MigrationError(f"rewritten file hash mismatch after write: {destination}")

        report["status"] = "moving_sources"
        _write_report(report_path, report)
        for row in report["moves"]:
            source = config.repo_root / row["source"]
            if not source.is_file():
                raise MigrationError(
                    f"source disappeared during apply; refusing partial cleanup: {source}"
                )
            if _sha256(source.read_bytes()) != row["source_sha256"]:
                raise MigrationError(f"source changed during apply; preserving user work: {source}")
        report["removed_source_paths"] = _remove_moved_sources(
            config, plan["source_files"], plan["fuyao_files"]
        )
        report["legacy_imports_after"] = _verify_after_apply(config)
        report["status"] = "complete"
        _write_report(report_path, report)
        return report
    except Exception as exc:
        report["status"] = "interrupted"
        report["failure"] = f"{type(exc).__name__}: {exc}"
        _write_report(report_path, report)
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(
            f"migration interrupted; inspect rollback_map in {report_path}: {exc}"
        ) from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or apply the frozen AKShare/Fuyao provider-layout migration."
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--akshare-source", type=Path, default=Path("opendata_http"))
    parser.add_argument(
        "--akshare-target",
        type=Path,
        default=Path("opendata/data/providers/akshare/_vendor"),
    )
    parser.add_argument("--fuyao-source", type=Path, default=Path("opendata_fuyao"))
    parser.add_argument("--ths-target", type=Path, default=Path("opendata/data/providers/ths"))
    parser.add_argument(
        "--transport-target",
        type=Path,
        help="override transport destination (default: <ths-target>/transport)",
    )
    parser.add_argument("--akshare-old-namespace", default="opendata_http")
    parser.add_argument(
        "--akshare-new-namespace",
        default="opendata.data.providers.akshare._vendor",
    )
    parser.add_argument("--fuyao-old-namespace", default="opendata_fuyao")
    parser.add_argument("--ths-new-namespace", default="opendata.data.providers.ths")
    parser.add_argument(
        "--transport-new-namespace",
        default="opendata.data.providers.ths.transport",
    )
    parser.add_argument("--report", type=Path, help="write the JSON plan/report here")
    parser.add_argument("--apply", action="store_true", help="apply changes under repo-root")
    parser.add_argument(
        "--allow-nonfrozen-inventory",
        action="store_true",
        help="disable frozen 329-file and 166/532 assertions for synthetic trees",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Parse CLI options and run a plan or isolated apply."""
    args = _parse_args(argv)
    if args.apply and args.report is None:
        raise SystemExit("--apply requires --report")
    strict = not args.allow_nonfrozen_inventory
    config = MigrationConfig(
        repo_root=args.repo_root,
        akshare_source=args.akshare_source,
        akshare_target=args.akshare_target,
        fuyao_source=args.fuyao_source,
        ths_target=args.ths_target,
        transport_target=args.transport_target,
        akshare_old_namespace=args.akshare_old_namespace,
        akshare_new_namespace=args.akshare_new_namespace,
        fuyao_old_namespace=args.fuyao_old_namespace,
        ths_new_namespace=args.ths_new_namespace,
        transport_new_namespace=args.transport_new_namespace,
        expected_akshare_file_count=(DEFAULT_AKSHARE_FILE_COUNT if strict else None),
        expected_akshare_suffix_counts=(DEFAULT_AKSHARE_SUFFIX_COUNTS if strict else None),
        expected_source_imports=(EXPECTED_AKSHARE_SOURCE_IMPORTS if strict else None),
    )
    report = migrate_repository(config, apply=args.apply, report_path=args.report)
    if args.report is None:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            json.dumps(
                {
                    "status": report.get("status"),
                    "idempotent": report.get("idempotent", False),
                    "akshare_files": report.get("inventory", {})
                    .get("akshare", {})
                    .get("non_cache_file_count"),
                    "fuyao_files": report.get("inventory", {})
                    .get("fuyao", {})
                    .get("non_cache_file_count"),
                    "rewrite_nodes": len(report.get("rewrites", {}).get("ast_import_nodes", [])),
                    "report": str(args.report),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
