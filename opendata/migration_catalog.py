"""Build a read-only, hash-bound crosswalk for legacy data acquisition code.

The catalog scans source bytes and parses Python with :mod:`ast`. It never imports
or executes legacy code, reads environment files, or opens a database connection.
Static provider matches are adapter candidates only; runtime parameter semantics,
permissions, data quality, history coverage, and production ownership stay open.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import os
import re
import tokenize
from dataclasses import asdict, dataclass, field
from pathlib import Path

CATALOG_SCHEMA_VERSION = "1.0"
DEFAULT_LEGACY_ROOT = Path("/home/yun/Documents/ai-for-investor")
DEFAULT_OPENDATA_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE_RELATIVE = Path(
    "docs/iterations/迭代204-OpenData数据中台拆分与五人并行迁移/script_migration_inventory.csv"
)
DEFAULT_OUTPUT_RELATIVE = Path("docs/afi204-migration-catalog.json")
_SOURCE_ROOTS = frozenset(
    {
        "akshare",
        "baostock",
        "ccxt",
        "easyquotation",
        "efinance",
        "jqdatasdk",
        "openbb",
        "rqdatac",
        "tqsdk",
        "tushare",
        "yfinance",
    }
)
_SAFE_TEXT_SUFFIXES = frozenset(
    {
        ".bat",
        ".conf",
        ".cron",
        ".example",
        ".ini",
        ".json",
        ".md",
        ".py",
        ".service",
        ".sh",
        ".timer",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_SENSITIVE_NAME = re.compile(
    r"(api.?key|access.?key|secret|token|password|passwd|credential)", re.I
)
_WRITE_METHODS = frozenset(
    {
        "bulk_save_objects",
        "commit",
        "create_table_if_not_exists",
        "execute",
        "insert",
        "merge",
        "replace",
        "save",
        "save_to_mysql",
        "save_data",
        "to_csv",
        "to_parquet",
        "to_sql",
        "write",
        "writelines",
    }
)
_SCRIPT_RELATIVE = Path("src/backend/app/data_fetch/scripts")
_OPERATIONAL_DIRS = (
    Path("scripts/ops"),
    Path("src/backend/scripts"),
    Path("deploy/systemd"),
)
_SCHEDULER_HINTS = ("scheduler", "controller", "orchestration", "startup")


@dataclass(slots=True)
class ParameterEvidence:
    """One call argument without its potentially sensitive runtime value."""

    name: str
    line: int
    expression_kind: str
    is_literal: bool


@dataclass(slots=True)
class LegacyCall:
    """A statically identified source endpoint invocation."""

    source_root: str
    source_module: str
    function: str
    line: int
    call_style: str
    classification: str = "PENDING_CROSSWALK"
    arguments: list[ParameterEvidence] = field(default_factory=list)
    dynamic_arguments: list[str] = field(default_factory=list)
    vendor_exported: bool = False
    target_models: list[str] = field(default_factory=list)
    compatible_query_fields: list[str] = field(default_factory=list)
    unresolved_runtime_parameters: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LegacyFile:
    """Hash-bound facts and unresolved questions for one legacy Python file."""

    source_path: str
    source_sha256: str
    baseline_sha256: str | None
    baseline_hash_match: bool | None
    entrypoints: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    source_imports: list[str] = field(default_factory=list)
    calls: list[LegacyCall] = field(default_factory=list)
    write_sinks: list[str] = field(default_factory=list)
    unknown_sinks: list[str] = field(default_factory=list)
    dynamic_sources: list[str] = field(default_factory=list)
    unresolved_runtime_parameters: list[str] = field(default_factory=list)
    status: str = "REQUIRES_MANUAL"
    sink_status: str = "NO_STATIC_SINK_FOUND"
    runtime_verification: str = "NOT_RUN"
    manual_reasons: list[str] = field(default_factory=list)
    parse_error: str = ""
    task_reference_paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProviderModel:
    """Provider and fetcher facts extracted without importing runtime code."""

    source: str
    model_class: str
    module_path: str
    registered_by_descriptor: bool
    domain: str
    asset_class: str
    period: str
    market: str
    query_fields: list[str]
    source_functions: list[str]
    vendor_functions: list[str]


@dataclass(slots=True)
class TriggerReference:
    """A source file that statically refers to a legacy script path/name."""

    source_path: str
    source_sha256: str
    reference_line: int
    target_script_paths: list[str]
    relation: str


@dataclass(slots=True)
class AuxiliaryFile:
    """Hash and role for operational scripts and scheduler/controller code."""

    source_path: str
    source_sha256: str
    role: str
    python_parse_status: str
    parse_error: str = ""


@dataclass(slots=True)
class _EndpointSource:
    """Per-script references for one grouped provider endpoint."""

    source_sha256: str
    file_status: str
    call_lines: set[int] = field(default_factory=set)


@dataclass(slots=True)
class MigrationCatalog:
    """Deterministic report for a single legacy source snapshot."""

    schema_version: str
    source_root: str
    opendata_root: str
    baseline_path: str | None
    baseline_status: str
    expected_baseline_file_count: int | None
    files: list[LegacyFile] = field(default_factory=list)
    provider_models: list[ProviderModel] = field(default_factory=list)
    auxiliary_files: list[AuxiliaryFile] = field(default_factory=list)
    trigger_references: list[TriggerReference] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    runtime_task_evidence: list[dict[str, object]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def endpoint_catalog(self) -> list[dict[str, object]]:
        """Group calls by endpoint while preserving each source path/hash join."""
        grouped: dict[tuple[str, str], list[tuple[LegacyFile, LegacyCall]]] = {}
        for record in self.files:
            for call in record.calls:
                grouped.setdefault((call.source_root, call.function), []).append((record, call))
        output: list[dict[str, object]] = []
        for (source_root, function), occurrences in sorted(grouped.items()):
            matched = all(call.target_models for _, call in occurrences)
            parameters_resolved = all(
                not call.unresolved_runtime_parameters for _, call in occurrences
            )
            dynamic_clean = all(not record.dynamic_sources for record, _ in occurrences)
            has_unknown_calls = any(
                call.classification
                in {"UNRESOLVED_SOURCE_MODULE", "PROVIDER_INTERNAL_OR_TRANSPORT_HELPER"}
                for _, call in occurrences
            )
            if matched and parameters_resolved and dynamic_clean and not has_unknown_calls:
                status = "READY_FOR_ADAPTER"
            elif (
                not matched
                and all(call.vendor_exported for _, call in occurrences)
                and not has_unknown_calls
            ):
                status = "MISSING_PROVIDER"
            else:
                status = "REQUIRES_MANUAL"
            sources: dict[str, _EndpointSource] = {}
            for record, call in occurrences:
                source = sources.setdefault(
                    record.source_path,
                    _EndpointSource(record.source_sha256, record.status),
                )
                source.call_lines.add(call.line)
            output.append(
                {
                    "source_root": source_root,
                    "function": function,
                    "status": status,
                    "readiness_scope": (
                        "endpoint and query-parameter names only; file, dataset, sink, "
                        "and task migration remain separate"
                    ),
                    "runtime_verification": "NOT_RUN",
                    "call_count": len(occurrences),
                    "script_count": len(sources),
                    "source_roots": sorted({call.source_root for _, call in occurrences}),
                    "source_modules": sorted({call.source_module for _, call in occurrences}),
                    "vendor_exported": all(call.vendor_exported for _, call in occurrences),
                    "classifications": sorted({call.classification for _, call in occurrences}),
                    "target_models": sorted(
                        {model for _, call in occurrences for model in call.target_models}
                    ),
                    "compatible_query_fields": sorted(
                        {
                            field_name
                            for _, call in occurrences
                            for field_name in call.compatible_query_fields
                        }
                    ),
                    "observed_argument_names": sorted(
                        {argument.name for _, call in occurrences for argument in call.arguments}
                    ),
                    "runtime_argument_names": sorted(
                        {name for _, call in occurrences for name in call.dynamic_arguments}
                    ),
                    "unresolved_runtime_parameters": sorted(
                        {
                            name
                            for _, call in occurrences
                            for name in call.unresolved_runtime_parameters
                        }
                    ),
                    "source_scripts": [
                        {
                            "source_path": source_path,
                            "source_sha256": source.source_sha256,
                            "file_status": source.file_status,
                            "call_lines": sorted(source.call_lines),
                        }
                        for source_path, source in sorted(sources.items())
                    ],
                }
            )
        return output

    def summary(self) -> dict[str, object]:
        """Count actual catalog rows without treating candidates as completed work."""
        by_status: dict[str, int] = {}
        for record in self.files:
            by_status[record.status] = by_status.get(record.status, 0) + 1
        all_calls = [call for record in self.files for call in record.calls]
        endpoint_catalog = self.endpoint_catalog()
        return {
            "legacy_script_count": len(self.files),
            "entrypoint_count": sum(bool(item.entrypoints) for item in self.files),
            "helper_or_unresolved_count": sum(not item.entrypoints for item in self.files),
            "status_counts": dict(sorted(by_status.items())),
            "provider_model_count": len(self.provider_models),
            "registered_provider_model_count": sum(
                item.registered_by_descriptor for item in self.provider_models
            ),
            "unregistered_provider_model_count": sum(
                not item.registered_by_descriptor for item in self.provider_models
            ),
            "legacy_provider_call_count": len(all_calls),
            "vendor_exported_call_count": sum(item.vendor_exported for item in all_calls),
            "registered_fetcher_match_call_count": sum(
                bool(item.target_models) for item in all_calls
            ),
            "shared_writer_reference_file_count": sum(
                any("akshare_to_mysql" in module for module in item.source_imports)
                for item in self.files
            ),
            "shared_writer_endpoint_dispatch_file_count": sum(
                any(call.call_style == "shared_writer_string_dispatch" for call in item.calls)
                for item in self.files
            ),
            "provider_endpoint_count": len(endpoint_catalog),
            "provider_endpoint_status_counts": dict(
                sorted(
                    {
                        status: sum(item["status"] == status for item in endpoint_catalog)
                        for status in {str(item["status"]) for item in endpoint_catalog}
                    }.items()
                )
            ),
            "auxiliary_file_count": len(self.auxiliary_files),
            "trigger_reference_count": len(self.trigger_references),
            "parse_error_count": sum(bool(item.parse_error) for item in self.files)
            + sum(item.python_parse_status == "ERROR" for item in self.auxiliary_files),
            "unknown_sink_file_count": sum(bool(item.unknown_sinks) for item in self.files),
            "dynamic_source_file_count": sum(bool(item.dynamic_sources) for item in self.files),
            "unresolved_runtime_parameter_count": sum(
                len(item.unresolved_runtime_parameters) for item in self.files
            ),
            "baseline_hash_mismatch_count": sum(
                item.baseline_hash_match is False for item in self.files
            ),
            "baseline_unlisted_file_count": sum(
                item.baseline_sha256 is None for item in self.files
            ),
        }

    def to_dict(self) -> dict[str, object]:
        """Return the full stable crosswalk and its static-evidence limitations."""
        return {
            "schema_version": self.schema_version,
            "source_root": self.source_root,
            "opendata_root": self.opendata_root,
            "baseline": {
                "path": self.baseline_path,
                "status": self.baseline_status,
                "expected_file_count": self.expected_baseline_file_count,
            },
            "summary": self.summary(),
            "files": [asdict(item) for item in self.files],
            "provider_endpoint_catalog": self.endpoint_catalog(),
            "opendata_provider_models": [asdict(item) for item in self.provider_models],
            "auxiliary_files": [asdict(item) for item in self.auxiliary_files],
            "trigger_references": [asdict(item) for item in self.trigger_references],
            "runtime_task_evidence": self.runtime_task_evidence,
            "runtime_task_mapping": {
                "status": "NOT_CHECKED_DATABASE_OR_INSTALLED_HOST",
                "required_joins": [
                    "task_id -> source_path and source_sha256",
                    "task_id -> provider function and sanitized runtime parameter names",
                    (
                        "task_id -> schedule expression, timezone, calendar, active state, "
                        "and next/last run"
                    ),
                    "task_id -> source quota, ownership group, sink table/partition, and shard",
                    (
                        "service/timer/cron identity -> executable path, working directory, "
                        "and environment identity"
                    ),
                ],
            },
            "errors": self.errors,
            "limitations": self.limitations,
            "verification_notice": (
                "READY_FOR_ADAPTER means only that static endpoint and parameter names appear "
                "compatible for that endpoint. It does not mean a script, task, dataset, source, "
                "permission, "
                "history, quality rule, sink, or production run has migrated or passed "
                "runtime verification."
            ),
        }


def _hash_file(path: Path) -> str:
    """Hash one source file in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_text(path: Path) -> tuple[str | None, str]:
    """Decode Python source bytes without importing or executing the module."""
    try:
        raw = path.read_bytes()
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        return raw.decode(encoding), ""
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError) as exc:
        return None, f"{exc.__class__.__name__}: {exc}"


def _safe_relative(path: Path, root: Path) -> str:
    """Render one path relative to its declared repository root."""
    return path.relative_to(root).as_posix()


def _constant(node: ast.AST | None) -> str:
    """Return a string literal from an AST node, or an empty string."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def _literal_names(node: ast.AST | None) -> set[str]:
    """Collect non-secret argument names that are statically visible in a call."""
    names: set[str] = set()
    if isinstance(node, ast.Call):
        names.update(keyword.arg for keyword in node.keywords if keyword.arg)
    return names


def _load_baseline(path: Path | None) -> tuple[dict[str, str], str, int | None]:
    """Read the iteration-204 CSV hashes, retaining malformed input as evidence."""
    if path is None:
        return {}, "NOT_PROVIDED", None
    try:
        lexical = Path(os.path.abspath(path.expanduser()))
        resolved = path.expanduser().resolve(strict=True)
        if lexical.name.casefold().startswith(".env") or resolved.name.casefold().startswith(
            ".env"
        ):
            return {}, "REFUSED_ENVIRONMENT_FILE", None
        with resolved.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not {"source_path", "source_sha256"}.issubset(reader.fieldnames or []):
                return {}, "INVALID_COLUMNS", None
            entries: dict[str, str] = {}
            for row in reader:
                source_path = row.get("source_path", "")
                source_hash = row.get("source_sha256", "")
                if source_path and re.fullmatch(r"[0-9a-f]{64}", source_hash or ""):
                    entries[source_path] = source_hash
            return entries, "LOADED", len(entries)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        return {}, f"ERROR:{exc.__class__.__name__}", None


def _walk_python(
    root: Path, *, exclude_init: bool = True
) -> tuple[list[Path], list[dict[str, str]]]:
    """List Python source files deterministically and avoid following symlinks."""
    files: list[Path] = []
    errors: list[dict[str, str]] = []
    if not root.is_dir():
        return [], [{"path": root.as_posix(), "error": "directory_missing"}]
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in {".git", ".venv", "__pycache__", "node_modules"}
        )
        for name in sorted(file_names):
            path = Path(directory, name)
            if path.is_symlink():
                errors.append({"path": path.as_posix(), "error": "symlink_skipped"})
            elif path.suffix == ".py" and not (exclude_init and name == "__init__.py"):
                files.append(path)
    return sorted(files), errors


def _parse(path: Path) -> tuple[ast.Module | None, str, str]:
    """Read, hash, and parse one Python file while preserving malformed files."""
    try:
        raw = path.read_bytes()
        source_hash = hashlib.sha256(raw).hexdigest()
    except OSError as exc:
        return None, "", f"read error: {exc.__class__.__name__}"
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        text = raw.decode(encoding)
    except (SyntaxError, UnicodeDecodeError, LookupError) as exc:
        return None, source_hash, f"decode error: {exc.__class__.__name__}"
    try:
        return ast.parse(text, filename=path.as_posix()), source_hash, ""
    except (SyntaxError, ValueError, RecursionError) as exc:
        if isinstance(exc, SyntaxError):
            detail = f"syntax error at line {exc.lineno or '?'}: {exc.msg}"
        else:
            detail = f"parse error: {exc.__class__.__name__}"
        return None, source_hash, detail


def _provider_imports(
    tree: ast.Module,
) -> tuple[dict[str, tuple[str, str, str]], list[str], list[str]]:
    """Collect local aliases for source roots and directly imported endpoints."""
    module_aliases: dict[str, tuple[str, str, str]] = {}
    roots: set[str] = set()
    dynamic: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in _SOURCE_ROOTS:
                    local_name = alias.asname or root
                    module_aliases[local_name] = (root, "module", alias.name)
                    roots.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if root in _SOURCE_ROOTS:
                roots.add(node.module)
                for alias in node.names:
                    module_aliases[alias.asname or alias.name] = (root, alias.name, node.module)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"import_module", "__import__"}
            and node.args
        ):
            module_name = _constant(node.args[0])
            root = module_name.split(".", 1)[0]
            if root in _SOURCE_ROOTS and module_name:
                dynamic.add(f"constant_dynamic_import:{module_name}")
                roots.add(module_name)
            elif not module_name:
                dynamic.add(f"dynamic_import_at_line:{node.lineno}")
    return module_aliases, sorted(roots), sorted(dynamic)


def _qualified(node: ast.AST) -> list[str]:
    """Flatten an attribute chain from its root identifier."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [*_qualified(node.value), node.attr]
    return []


def _expr_kind(node: ast.AST) -> tuple[str, bool]:
    """Classify an argument expression without serializing its value."""
    if isinstance(node, ast.Constant):
        return type(node.value).__name__, True
    if isinstance(node, ast.Name):
        return "name", False
    if isinstance(node, ast.Attribute):
        return "attribute", False
    if isinstance(node, (ast.List, ast.Tuple, ast.Dict, ast.Set)):
        return node.__class__.__name__.lower(), all(
            isinstance(child, ast.Constant)
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, ast.Load)
        )
    if isinstance(node, ast.Call):
        return "call", False
    return node.__class__.__name__.lower(), False


def _call_candidate(
    node: ast.Call,
    aliases: dict[str, tuple[str, str, str]],
    vendor_names: set[str],
) -> tuple[str, str, str] | None:
    """Resolve a provider function call through known import aliases."""
    chain = _qualified(node.func)
    if chain:
        root = aliases.get(chain[0])
        if root:
            provider, imported_name, _module = root
            if imported_name == "module" and len(chain) >= 2:
                return provider, chain[-1], "qualified_import"
            if imported_name != "module" and len(chain) == 1:
                return provider, imported_name, "direct_import"
    if isinstance(node.func, ast.Name) and node.func.id in aliases:
        provider, imported_name, _module = aliases[node.func.id]
        if imported_name != "module":
            return provider, imported_name, "direct_import"
    if chain and chain[-1] in vendor_names:
        return "unknown_static_source", chain[-1], "unqualified_name_match"
    return None


def _collect_legacy_calls(
    tree: ast.Module,
    aliases: dict[str, tuple[str, str, str]],
    vendor_names: set[str],
) -> tuple[list[LegacyCall], list[str], list[str], list[str]]:
    """Find source calls, dynamic source usage, and write operations."""
    calls: list[LegacyCall] = []
    dynamic_sources: set[str] = set()
    write_sinks: set[str] = set()
    unknown_sinks: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        candidate = _call_candidate(node, aliases, vendor_names)
        call_name = _qualified(node.func)[-1] if _qualified(node.func) else ""
        if call_name == "fetch_ak_data":
            endpoint = _constant(node.args[0]) if node.args else ""
            if not endpoint:
                dynamic_sources.add(f"dynamic_shared_writer_endpoint_at_line:{node.lineno}")
            else:
                args: list[ParameterEvidence] = []
                dynamic_names: list[str] = []
                for index, arg in enumerate(node.args[1:]):
                    kind, literal = _expr_kind(arg)
                    name = f"positional_{index}"
                    args.append(ParameterEvidence(name, node.lineno, kind, literal))
                    if not literal:
                        dynamic_names.append(name)
                for keyword in node.keywords:
                    kind, literal = _expr_kind(keyword.value)
                    name = keyword.arg or "**kwargs"
                    safe_name = "[redacted-name]" if _SENSITIVE_NAME.search(name) else name
                    args.append(ParameterEvidence(safe_name, node.lineno, kind, literal))
                    if not literal:
                        dynamic_names.append(safe_name)
                calls.append(
                    LegacyCall(
                        source_root="akshare",
                        source_module="app.data_fetch.providers.akshare_to_mysql",
                        function=endpoint,
                        line=node.lineno,
                        call_style="shared_writer_string_dispatch",
                        arguments=args,
                        dynamic_arguments=sorted(set(dynamic_names)),
                    )
                )
        if candidate:
            provider, function, style = candidate
            chain = _qualified(node.func)
            source_module = aliases[chain[0]][2] if chain and chain[0] in aliases else ""
            call_args: list[ParameterEvidence] = []
            call_dynamic_names: list[str] = []
            for index, arg in enumerate(node.args):
                kind, literal = _expr_kind(arg)
                name = f"positional_{index}"
                call_args.append(ParameterEvidence(name, node.lineno, kind, literal))
                if not literal:
                    call_dynamic_names.append(name)
            for keyword in node.keywords:
                kind, literal = _expr_kind(keyword.value)
                name = keyword.arg or "**kwargs"
                safe_name = "[redacted-name]" if _SENSITIVE_NAME.search(name) else name
                call_args.append(ParameterEvidence(safe_name, node.lineno, kind, literal))
                if not literal:
                    call_dynamic_names.append(safe_name)
            calls.append(
                LegacyCall(
                    source_root=provider,
                    source_module=source_module,
                    function=function,
                    line=node.lineno,
                    call_style=style,
                    arguments=call_args,
                    dynamic_arguments=sorted(set(call_dynamic_names)),
                )
            )
        chain = _qualified(node.func)
        if call_name in _WRITE_METHODS:
            qualified = ".".join(chain)
            if call_name in {
                "to_sql",
                "execute",
                "insert",
                "merge",
                "save_to_mysql",
                "commit",
                "to_csv",
                "to_parquet",
                "write",
                "writelines",
            }:
                write_sinks.add(qualified)
            elif call_name in {"save", "replace", "bulk_save_objects"}:
                unknown_sinks.add(qualified)
            elif call_name in {"save_data", "create_table_if_not_exists"}:
                write_sinks.add(qualified)
        if call_name == "getattr" and len(node.args) >= 2:
            root_chain = _qualified(node.args[0])
            root_alias = aliases.get(root_chain[0]) if root_chain else None
            dynamic_member = _constant(node.args[1])
            if root_alias and not dynamic_member:
                dynamic_sources.add(f"dynamic_endpoint_at_line:{node.lineno}")
            elif root_alias and dynamic_member:
                calls.append(
                    LegacyCall(
                        source_root=root_alias[0],
                        source_module=root_alias[2],
                        function=dynamic_member,
                        line=node.lineno,
                        call_style="getattr_constant",
                        dynamic_arguments=["dynamic_dispatch"],
                    )
                )
        if chain and call_name in {"AkshareToMySql", "AkshareToMySQL", "MysqlBase", "MySQLBase"}:
            write_sinks.add("legacy_shared_writer:" + call_name)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(
            name in {"AkshareToMySql", "AkshareToMySQL", "MysqlBase", "MySQLBase"}
            for base in node.bases
            for name in _qualified(base)
        ):
            write_sinks.add("legacy_shared_writer_subclass:" + node.name)
    calls.sort(key=lambda item: (item.line, item.source_root, item.function, item.call_style))
    return calls, sorted(dynamic_sources), sorted(write_sinks), sorted(unknown_sinks)


def _literal_assignment(tree: ast.Module, name: str) -> object | None:
    """Return a literal module assignment when safe to evaluate as Python data."""
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                if node.value is None:
                    return None
                try:
                    literal_value: object = ast.literal_eval(node.value)
                    return literal_value
                except (ValueError, TypeError, SyntaxError):
                    return None
    return None


def _provider_descriptors(opendata_root: Path) -> set[tuple[str, str, str]]:
    """Read registered fetcher targets from provider descriptors using AST only."""
    result: set[tuple[str, str, str]] = set()
    provider_root = opendata_root / "opendata/data/providers"
    for path in provider_root.rglob("provider.py"):
        if any(part in {"_vendor", "__pycache__"} for part in path.parts):
            continue
        tree, _, error = _parse(path)
        if tree is None:
            continue
        source = ""
        bindings: list[tuple[str, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            chain = _qualified(node.func)
            if chain and chain[-1] == "Provider":
                for keyword in node.keywords:
                    if keyword.arg == "source":
                        source = _constant(keyword.value)
                    elif keyword.arg == "fetcher_bindings" and isinstance(
                        keyword.value, (ast.Tuple, ast.List)
                    ):
                        for element in keyword.value.elts:
                            if not isinstance(element, ast.Call):
                                continue
                            module = _constant(element.args[0]) if element.args else ""
                            class_name = _constant(element.args[1]) if len(element.args) > 1 else ""
                            if module and class_name:
                                bindings.append((module, class_name))
        if not source:
            source_file = path.parent / "_source.py"
            source_tree, _, _ = _parse(source_file)
            if source_tree:
                source_value = _literal_assignment(source_tree, "SOURCE")
                source = source_value if isinstance(source_value, str) else ""
        for module, class_name in bindings:
            result.add((source, module, class_name))
    return result


def _query_fields(tree: ast.Module) -> set[str]:
    """Collect declared query DTO fields plus base protocol fields."""
    fields = {"source", "market", "symbol", "start_date", "end_date"}
    query_class_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name.endswith("Query"):
            query_class_names.add(node.name)
            for member in node.body:
                if isinstance(member, (ast.AnnAssign, ast.Assign)):
                    targets = (
                        [member.target] if isinstance(member, ast.AnnAssign) else member.targets
                    )
                    fields.update(target.id for target in targets if isinstance(target, ast.Name))
    if not query_class_names:
        return fields
    return fields


def _provider_models(opendata_root: Path) -> list[ProviderModel]:
    """Extract fetcher capabilities and source function calls without importing."""
    registered = _provider_descriptors(opendata_root)
    provider_root = opendata_root / "opendata/data/providers"
    models: list[ProviderModel] = []
    for path in sorted(provider_root.rglob("models/**/*.py")):
        if any(part in {"_vendor", "__pycache__"} for part in path.parts):
            continue
        tree, _, error = _parse(path)
        if tree is None:
            continue
        relative = path.relative_to(opendata_root).as_posix()
        parts = relative.split("/")
        provider_folder = parts[3] if len(parts) > 3 else ""
        source_file = provider_root / provider_folder / "_source.py"
        source_tree, _, _ = _parse(source_file)
        source_value = _literal_assignment(source_tree, "SOURCE") if source_tree else None
        source = source_value if isinstance(source_value, str) else provider_folder
        source_aliases, _, _ = _provider_imports(tree)
        imported_source_aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith("._vendor") or alias.name.endswith(
                        "._vendor as opendata_http"
                    ):
                        imported_source_aliases[alias.asname or alias.name.split(".")[-1]] = (
                            "vendor"
                        )
            elif isinstance(node, ast.ImportFrom) and node.module and "._vendor" in node.module:
                for alias in node.names:
                    imported_source_aliases[alias.asname or alias.name] = "vendor"
        del source_aliases
        for klass in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            capability: dict[str, str] = {}
            vendor_calls: set[str] = set()
            source_calls: set[str] = set()
            for node in ast.walk(klass):
                if isinstance(node, ast.Call):
                    chain = _qualified(node.func)
                    if chain:
                        if chain[0] in imported_source_aliases:
                            vendor_calls.add(chain[-1])
                            source_calls.add(chain[-1])
                        elif chain[0] in {"requests", "httpx", "urllib", "pd"}:
                            source_calls.add(".".join(chain))
                    if chain and chain[-1] == "Capability":
                        capability = {
                            item.arg: _constant(item.value)
                            for item in node.keywords
                            if item.arg and isinstance(item.value, ast.Constant)
                        }
            if not capability:
                continue
            model_module = "opendata." + ".".join(parts[1:-1]) + "." + path.stem
            registry_key = (source, model_module, klass.name)
            models.append(
                ProviderModel(
                    source=source,
                    model_class=klass.name,
                    module_path=relative,
                    registered_by_descriptor=registry_key in registered,
                    domain=capability.get("domain", "UNKNOWN"),
                    asset_class=capability.get("asset_class", "UNKNOWN"),
                    period=capability.get("period", "UNKNOWN"),
                    market=capability.get("market", "UNKNOWN"),
                    query_fields=sorted(_query_fields(tree)),
                    source_functions=sorted(source_calls),
                    vendor_functions=sorted(vendor_calls),
                )
            )
    return sorted(
        models, key=lambda item: (item.source, item.domain, item.model_class, item.module_path)
    )


def _vendor_exports(opendata_root: Path) -> set[str]:
    """Read the vendored compatibility facade's declared export names statically."""
    facade = opendata_root / "opendata/data/providers/akshare/_vendor/__init__.py"
    tree, _, _ = _parse(facade)
    if tree is None:
        return set()
    value = _literal_assignment(tree, "_EXPORTS")
    return set(value) if isinstance(value, dict) else set()


def _file_entrypoints(tree: ast.Module) -> tuple[list[str], list[str]]:
    """List common entrypoint function symbols and declared classes."""
    functions = sorted(
        {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"fetch_data", "run", "main", "execute"}
        }
    )
    classes = sorted(node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
    return functions, classes


def _provider_crosswalk(
    record: LegacyFile,
    models: list[ProviderModel],
    vendor_exports: set[str],
) -> None:
    """Match provider endpoints and compare dynamic names to query fields."""
    for call in record.calls:
        call.vendor_exported = call.function in vendor_exports
        candidates = [
            item
            for item in models
            if item.registered_by_descriptor and call.function in item.vendor_functions
        ]
        call.target_models = sorted(
            f"{item.source}:{item.domain}:{item.model_class}" for item in candidates
        )
        if candidates:
            call.classification = "REGISTERED_FETCHER_MATCH"
        elif call.vendor_exported:
            call.classification = "EXPORTED_SOURCE_ENDPOINT_WITHOUT_FETCHER"
        elif call.source_root == "unknown_static_source":
            call.classification = "UNRESOLVED_SOURCE_MODULE"
        elif ".utils." in call.source_module or call.function.startswith("_"):
            call.classification = "PROVIDER_INTERNAL_OR_TRANSPORT_HELPER"
        else:
            call.classification = "SOURCE_CALL_WITHOUT_FETCHER_MATCH"
        if not candidates:
            call.unresolved_runtime_parameters = sorted(set(call.dynamic_arguments))
        else:
            common_fields = set.intersection(*(set(item.query_fields) for item in candidates))
            supplied = {
                argument.name for argument in call.arguments if argument.name != "[redacted-name]"
            }
            runtime = set(call.dynamic_arguments)
            compatible = sorted((supplied | runtime) & common_fields)
            unresolved = sorted(runtime - common_fields)
            call.compatible_query_fields = compatible
            call.unresolved_runtime_parameters = unresolved
        record.unresolved_runtime_parameters.extend(
            f"{call.function}@{call.line}:{name}" for name in call.unresolved_runtime_parameters
        )
    record.unresolved_runtime_parameters = sorted(set(record.unresolved_runtime_parameters))
    if record.parse_error:
        record.manual_reasons.append("python_source_unparseable")
        return
    if record.dynamic_sources:
        record.manual_reasons.append("dynamic_source_dispatch")
    if record.unknown_sinks:
        record.sink_status = "UNKNOWN_SINK_REQUIRES_REVIEW"
    elif record.write_sinks:
        record.sink_status = "LEGACY_SINK_REQUIRES_OPENDATA_DATASET_MAPPING"
    else:
        record.sink_status = "NO_STATIC_SINK_FOUND"
    if record.write_sinks or record.unknown_sinks:
        record.manual_reasons.append("legacy_sink_mapping_not_proven")
    if not record.calls:
        record.manual_reasons.append("no_static_source_endpoint_call")
        return
    unmatched_endpoints = [
        call
        for call in record.calls
        if not call.target_models and call.classification != "PROVIDER_INTERNAL_OR_TRANSPORT_HELPER"
    ]
    if unmatched_endpoints:
        record.manual_reasons.append("no_registered_opendata_fetcher_for_endpoint")
        record.status = "MISSING_PROVIDER"
        return
    has_internal_helpers = any(
        call.classification == "PROVIDER_INTERNAL_OR_TRANSPORT_HELPER" for call in record.calls
    )
    if has_internal_helpers:
        record.manual_reasons.append("provider_internal_or_transport_helper_requires_review")
    if any(call.unresolved_runtime_parameters for call in record.calls):
        record.manual_reasons.append("runtime_parameters_not_in_fetcher_query_contract")
    if (
        record.dynamic_sources
        or record.unresolved_runtime_parameters
        or record.unknown_sinks
        or has_internal_helpers
    ):
        record.status = "REQUIRES_MANUAL"
    else:
        record.status = "READY_FOR_ADAPTER"


def _scan_script_files(
    legacy_root: Path,
    baseline: dict[str, str],
    models: list[ProviderModel],
    vendor_exports: set[str],
) -> tuple[list[LegacyFile], list[dict[str, str]]]:
    """Catalog every non-init legacy script and retain syntax/read failures."""
    scripts_root = legacy_root / _SCRIPT_RELATIVE
    paths, errors = _walk_python(scripts_root)
    records: list[LegacyFile] = []
    for path in paths:
        relative = _safe_relative(path, legacy_root)
        tree, source_hash, parse_error = _parse(path)
        record = LegacyFile(
            source_path=relative,
            source_sha256=source_hash,
            baseline_sha256=baseline.get(relative),
            baseline_hash_match=(source_hash == baseline[relative])
            if relative in baseline
            else None,
            parse_error=parse_error,
        )
        if tree is None:
            record.status = "REQUIRES_MANUAL"
            record.manual_reasons.append("parse_error_preserved_in_catalog")
            records.append(record)
            continue
        record.entrypoints, record.classes = _file_entrypoints(tree)
        aliases, record.source_imports, dynamic_imports = _provider_imports(tree)
        legacy_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and "akshare_to_mysql" in node.module
        }
        legacy_imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if "akshare_to_mysql" in alias.name
        )
        record.source_imports = sorted(set(record.source_imports) | legacy_imports)
        record.dynamic_sources.extend(dynamic_imports)
        calls, dynamic_calls, sinks, unknown_sinks = _collect_legacy_calls(
            tree, aliases, vendor_exports
        )
        record.calls = calls
        record.dynamic_sources = sorted(set(record.dynamic_sources + dynamic_calls))
        record.write_sinks = sinks
        record.unknown_sinks = unknown_sinks
        _provider_crosswalk(record, models, vendor_exports)
        records.append(record)
    records.sort(key=lambda item: item.source_path)
    return records, errors


def _operation_paths(legacy_root: Path) -> tuple[list[tuple[Path, str]], list[dict[str, str]]]:
    """List in-repository scripts, service files, and scheduler/controller modules."""
    found: dict[Path, str] = {}
    errors: list[dict[str, str]] = []
    data_fetch_root = legacy_root / "src/backend/app/data_fetch"
    for path in data_fetch_root.rglob("*.py") if data_fetch_root.is_dir() else []:
        if "scripts" not in path.relative_to(data_fetch_root).parts:
            found[path] = "data_fetch_provider_or_runtime"
    for relative in _OPERATIONAL_DIRS:
        root = legacy_root / relative
        for directory, directory_names, file_names in (
            os.walk(root, followlinks=False) if root.is_dir() else []
        ):
            directory_names[:] = sorted(
                name for name in directory_names if name not in {".git", "__pycache__"}
            )
            for name in sorted(file_names):
                path = Path(directory, name)
                if path.is_symlink():
                    errors.append(
                        {"path": _safe_relative(path, legacy_root), "error": "symlink_skipped"}
                    )
                elif path.suffix.lower() in _SAFE_TEXT_SUFFIXES:
                    found[path] = "operational_script_or_service_config"
    app_root = legacy_root / "src/backend/app"
    for path in app_root.rglob("*.py") if app_root.is_dir() else []:
        relative = path.relative_to(app_root)
        if any(hint in relative.as_posix().lower() for hint in _SCHEDULER_HINTS):
            found[path] = "scheduler_controller_or_startup"
    return sorted(found.items()), errors


def _auxiliary_inventory(
    legacy_root: Path,
) -> tuple[list[AuxiliaryFile], list[Path], list[dict[str, str]]]:
    """Hash operational code/config and return searchable text sources."""
    paths, errors = _operation_paths(legacy_root)
    records: list[AuxiliaryFile] = []
    searchable: list[Path] = []
    for path, role in paths:
        try:
            digest = _hash_file(path)
        except OSError as exc:
            errors.append(
                {"path": _safe_relative(path, legacy_root), "error": exc.__class__.__name__}
            )
            continue
        if path.suffix.lower() not in _SAFE_TEXT_SUFFIXES or path.stat().st_size > 8 * 1024 * 1024:
            continue
        searchable.append(path)
        parse_status, parse_error = "NOT_PYTHON", ""
        if path.suffix == ".py":
            tree, _, parse_error = _parse(path)
            parse_status = "PARSED" if tree is not None else "ERROR"
        records.append(
            AuxiliaryFile(
                source_path=_safe_relative(path, legacy_root),
                source_sha256=digest,
                role=role,
                python_parse_status=parse_status,
                parse_error=parse_error,
            )
        )
    records.sort(key=lambda item: item.source_path)
    return records, searchable, errors


def _trigger_references(
    legacy_root: Path,
    records: list[LegacyFile],
    searchable: list[Path],
) -> list[TriggerReference]:
    """Join repo config/code references back to paths and hashes, never snippets."""
    by_name: dict[str, list[str]] = {}
    by_class: dict[str, list[str]] = {}
    by_path: dict[str, str] = {}
    for record in records:
        base = Path(record.source_path).name
        by_name.setdefault(base, []).append(record.source_path)
        by_path[record.source_path] = record.source_path
        for class_name in record.classes:
            by_class.setdefault(class_name, []).append(record.source_path)
    references: dict[tuple[str, int, tuple[str, ...]], TriggerReference] = {}
    path_token = re.compile(r"[A-Za-z0-9_./\\-]+\.py")
    identifier_token = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
    for path in searchable:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = _safe_relative(path, legacy_root)
        lines = text.splitlines()
        direct_matches: dict[int, set[str]] = {}
        for line_number, line in enumerate(lines, start=1):
            targets: set[str] = set()
            for token in path_token.findall(line):
                normalized = token.replace("\\", "/").lstrip("./")
                exact = by_path.get(normalized)
                if exact:
                    targets.add(exact)
                else:
                    targets.update(by_name.get(Path(normalized).name, []))
            if not targets:
                for token in identifier_token.findall(line):
                    targets.update(by_class.get(token, []))
            if targets:
                direct_matches[line_number] = targets
        if not direct_matches:
            continue
        digest = _hash_file(path)
        for line_number, targets in direct_matches.items():
            target_tuple = tuple(sorted(targets))
            line = lines[line_number - 1]
            normalized_line = line.replace("\\", "/")
            relation = (
                "exact_path"
                if any(target in normalized_line for target in target_tuple)
                else "basename_or_class_reference"
            )
            references[(relative, line_number, target_tuple)] = TriggerReference(
                source_path=relative,
                source_sha256=digest,
                reference_line=line_number,
                target_script_paths=list(target_tuple),
                relation=relation,
            )
            for target in target_tuple:
                record = next(item for item in records if item.source_path == target)
                record.task_reference_paths.append(relative)
    for record in records:
        record.task_reference_paths = sorted(set(record.task_reference_paths))
    return sorted(
        references.values(),
        key=lambda item: (item.source_path, item.reference_line, item.target_script_paths),
    )


def _documentary_task_evidence(legacy_root: Path) -> list[dict[str, object]]:
    """Capture the known 203 task-count snapshot without treating it as live state."""
    path = legacy_root / "docs/iterations/迭代203-全量数据补齐与持续调度治理/EXECUTION_STATUS.md"
    if not path.is_file():
        return [{"status": "DOCUMENTARY_SNAPSHOT_NOT_FOUND", "path": path.as_posix()}]
    tree_hash = _hash_file(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    evidence: list[dict[str, object]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if "1,098 active" in line or "1,103" in line or "5个B23 helper仍inactive" in line:
            evidence.append(
                {
                    "path": _safe_relative(path, legacy_root),
                    "sha256": tree_hash,
                    "line": line_number,
                    "evidence_kind": "documented_historical_runtime_snapshot",
                    "extracted_counts": re.findall(
                        r"(?<!\d)(?:1,103|1,098|1,029|69|5)(?!\d)", line
                    ),
                    "live_database_mapping": "NOT_CHECKED",
                }
            )
    return evidence or [
        {
            "path": _safe_relative(path, legacy_root),
            "sha256": tree_hash,
            "status": "DOCUMENT_EXISTS_BUT_NO_EXPECTED_TASK_COUNT_LINE",
            "live_database_mapping": "NOT_CHECKED",
        }
    ]


def build_migration_catalog(
    legacy_root: Path = DEFAULT_LEGACY_ROOT,
    opendata_root: Path = DEFAULT_OPENDATA_ROOT,
    *,
    baseline_path: Path | None = None,
) -> MigrationCatalog:
    """Build a full read-only static crosswalk from the legacy checkout.

    Args:
        legacy_root: Repository containing the original AI-for-Investor scripts.
        opendata_root: Repository containing self-developed Providers and registry descriptors.
        baseline_path: Optional CSV from the starting iteration-204 manifest.

    Returns:
        Hash-bound script, fetcher, scheduler/config-reference, and task-evidence facts.
    """
    legacy = legacy_root.expanduser().resolve()
    opendata = opendata_root.expanduser().resolve()
    baseline_file = baseline_path or legacy / DEFAULT_BASELINE_RELATIVE
    baseline, baseline_status, expected_count = _load_baseline(baseline_file)
    models = _provider_models(opendata)
    vendor_exports = _vendor_exports(opendata)
    files, file_errors = _scan_script_files(legacy, baseline, models, vendor_exports)
    auxiliary, searchable, auxiliary_errors = _auxiliary_inventory(legacy)
    triggers = _trigger_references(legacy, files, searchable)
    limitations = [
        (
            "AST matches are candidates. They do not prove that old and new model semantics, "
            "units, calendar, adjustment, PIT, or entitlement match."
        ),
        (
            "The legacy sink is separately recorded. Even READY_FOR_ADAPTER does not map a "
            "writer, dataset key, warehouse table, publication, or ownership epoch."
        ),
        (
            "The iteration-203 task counts are historical document evidence only. No database "
            "or installed systemd/crontab state was queried."
        ),
        (
            "The crosswalk covers the 1,066-script source tree plus repository-contained "
            "ops/config/runtime references; untracked host-local jobs are outside this scan."
        ),
        (
            "OpenData provider functions are extracted from local registry descriptors and "
            "model AST. They remain runtime-unverified."
        ),
        (
            "Credential values are never emitted; source arguments retain only parameter "
            "names, source line, and expression kind."
        ),
    ]
    errors = [*file_errors, *auxiliary_errors]
    if baseline_status != "LOADED":
        errors.append({"path": str(baseline_file), "error": f"baseline_{baseline_status.lower()}"})
    if expected_count is not None and expected_count != len(files):
        errors.append(
            {
                "path": str(baseline_file),
                "error": f"baseline_count_mismatch:{expected_count}!={len(files)}",
            }
        )
    return MigrationCatalog(
        schema_version=CATALOG_SCHEMA_VERSION,
        source_root=legacy.as_posix(),
        opendata_root=opendata.as_posix(),
        baseline_path=baseline_file.as_posix() if baseline_file.is_file() else None,
        baseline_status=baseline_status,
        expected_baseline_file_count=expected_count,
        files=files,
        provider_models=models,
        auxiliary_files=auxiliary,
        trigger_references=triggers,
        errors=sorted(errors, key=lambda item: (item.get("path", ""), item.get("error", ""))),
        runtime_task_evidence=_documentary_task_evidence(legacy),
        limitations=limitations,
    )


def validate_output_path(
    output: Path,
    *,
    legacy_root: Path,
    input_files: set[Path],
    protected_roots: set[Path] | None = None,
    overwrite: bool = False,
) -> tuple[Path | None, str | None]:
    """Reject unsafe output targets, input aliases, environment files, and silent overwrite."""
    lexical = Path(os.path.abspath(output.expanduser()))
    resolved_parent = lexical.parent.resolve()
    resolved = resolved_parent / lexical.name
    if lexical.is_symlink():
        return None, "output_symlink_refused"
    legacy_resolved = legacy_root.expanduser().resolve()
    if resolved.name.casefold().startswith(".env"):
        return None, "environment_file_output_refused"
    if resolved == legacy_resolved or legacy_resolved in resolved.parents:
        return None, "output_inside_legacy_source_tree"
    for root in protected_roots or set():
        protected_root = root.expanduser().resolve()
        if resolved == protected_root or protected_root in resolved.parents:
            return None, "output_inside_opendata_source_tree"
    protected = {item.expanduser().resolve() for item in input_files}
    if resolved in protected:
        return None, "output_aliases_input_file"
    if not resolved_parent.is_dir():
        return None, "output_parent_directory_missing"
    if resolved.is_dir():
        return None, "output_target_is_directory"
    if resolved.exists() and not overwrite:
        return None, "output_exists_requires_overwrite"
    return resolved, None


def render_json(catalog: MigrationCatalog) -> str:
    """Render stable JSON with one compact row per artifact record."""
    document = catalog.to_dict()
    lines = ["{"]
    items = sorted(document.items())
    for index, (key, value) in enumerate(items):
        if isinstance(value, list):
            if value:
                encoded = (
                    "[\n"
                    + ",\n".join(
                        "    "
                        + json.dumps(
                            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        )
                        for item in value
                    )
                    + "\n  ]"
                )
            else:
                encoded = "[]"
        else:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).replace(
                "\n", "\n  "
            )
        suffix = "," if index < len(items) - 1 else ""
        lines.append(f"  {json.dumps(key, ensure_ascii=False)}: {encoded}{suffix}")
    return "\n".join([*lines, "}", ""])


__all__ = [
    "DEFAULT_BASELINE_RELATIVE",
    "DEFAULT_LEGACY_ROOT",
    "DEFAULT_OPENDATA_ROOT",
    "DEFAULT_OUTPUT_RELATIVE",
    "MigrationCatalog",
    "build_migration_catalog",
    "render_json",
    "validate_output_path",
]
