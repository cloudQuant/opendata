"""Read-only inventory of legacy data-fetch Python scripts.

The scanner reads source bytes, hashes them, and parses them with :mod:`ast`.
It never imports or executes a discovered file, reads ``.env`` files, or opens a
database connection. Every discovered Python file remains in the output even if
its bytes cannot be decoded or its syntax cannot be parsed.

An optional task map is JSON with either a top-level ``tasks`` list or a list
itself. Each task has a non-empty ``task_id`` and either ``script_path`` or
``script_paths``. Paths are exact POSIX-style paths relative to ``source_root``.
The map is diagnostic: static matches never mark a task or provider as verified.
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
from pathlib import Path, PurePosixPath
from typing import TextIO

DEFAULT_SCRIPTS_RELATIVE_PATH = Path("src/backend/app/data_fetch/scripts")
INVENTORY_SCHEMA_VERSION = "1.0"

_FREQUENCIES: dict[str, str] = {
    "daily": "daily",
    "day": "daily",
    "hourly": "hourly",
    "hour": "hourly",
    "weekly": "weekly",
    "week": "weekly",
    "monthly": "monthly",
    "month": "monthly",
    "quarterly": "quarterly",
    "quarter": "quarterly",
    "yearly": "yearly",
    "year": "yearly",
    "annual": "yearly",
    "annually": "yearly",
    "minute": "minute",
    "minutes": "minute",
    "min": "minute",
    "1min": "minute",
    "5min": "minute",
    "15min": "minute",
    "30min": "minute",
    "realtime": "realtime",
    "real_time": "realtime",
    "snapshot": "snapshot",
    "snapshots": "snapshot",
}

_FAMILY_ALIASES: dict[str, str] = {
    "stock": "stock",
    "stocks": "stock",
    "equity": "stock",
    "equities": "stock",
    "security": "stock",
    "securities": "stock",
    "fund": "fund",
    "funds": "fund",
    "etf": "fund",
    "bond": "bond",
    "bonds": "bond",
    "convertible": "bond",
    "index": "index",
    "indices": "index",
    "indexs": "index",
    "future": "futures",
    "futures": "futures",
    "option": "options",
    "options": "options",
    "forex": "currency",
    "currency": "currency",
    "currencies": "currency",
    "fx": "currency",
    "crypto": "crypto",
    "cryptocurrency": "crypto",
    "bitcoin": "crypto",
    "btc": "crypto",
    "macro": "macro",
    "economic": "macro",
    "economy": "macro",
}

_PROVIDER_IMPORT_ROOTS = frozenset(
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
_SHARED_WRITER_MODULES = frozenset(
    {
        "app.data_fetch.providers.akshare_to_mysql",
        "data_fetch.providers.akshare_to_mysql",
    }
)
_SHARED_WRITER_NAMES = frozenset(
    {"AkshareToMySql", "AkshareToMySQL", "AkShareToMySql", "MysqlBase", "MySQLBase"}
)
_MAIN_GUARD = re.compile(r"^__main__$")


@dataclass(slots=True)
class ScriptRecord:
    """Static facts and explicit unknowns for one discovered Python file."""

    record_type: str
    source_path: str
    source_sha256: str | None
    asset_family: str
    asset_family_evidence: str
    legacy_frequency: str
    frequency_evidence: str
    static_role: str
    entry_symbols: list[str] = field(default_factory=list)
    module_main_call: bool = False
    main_guard: bool = False
    class_names: list[str] = field(default_factory=list)
    direct_source_imports: list[str] = field(default_factory=list)
    shared_writer_refs: list[str] = field(default_factory=list)
    akshare_usage: str = "no_static_akshare_signal"
    owner_candidate: str = "PENDING_REVIEW"
    mapped_task_ids: list[str] = field(default_factory=list)
    mapping_status: str = "TASK_MAP_NOT_PROVIDED"
    migration_route: str = "PENDING_PROVIDER_MAPPING"
    target_dataset: str = "PENDING_DATASET_MAPPING"
    source_verification: str = "NOT_VERIFIED"
    parse_error: str = ""


@dataclass(slots=True)
class InventoryError:
    """A filesystem, decoding, or syntax error retained in the report."""

    record_type: str
    source_path: str
    error_type: str
    error_message: str
    line: int | None = None
    column: int | None = None


@dataclass(slots=True)
class MappingIssue:
    """A mismatch found while comparing the optional task map and file list."""

    record_type: str
    issue: str
    task_id: str = ""
    source_path: str = ""
    detail: str = ""


@dataclass(slots=True)
class InventoryReport:
    """Complete deterministic inventory result."""

    schema_version: str
    source_root: str
    scripts_directory: str
    task_map_status: str
    scripts: list[ScriptRecord] = field(default_factory=list)
    errors: list[InventoryError] = field(default_factory=list)
    mapping_issues: list[MappingIssue] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        """Return counts derived from this report's actual records."""
        return {
            "script_count": len(self.scripts),
            "parse_error_count": sum(bool(item.parse_error) for item in self.scripts),
            "filesystem_error_count": len(self.errors),
            "mapping_issue_count": len(self.mapping_issues),
        }

    @property
    def has_errors(self) -> bool:
        """Whether scanning or mapping found an error requiring follow-up."""
        return bool(self.errors or self.mapping_issues)

    def to_dict(self) -> dict[str, object]:
        """Serialize all records without converting unknowns into successes."""
        return {
            "schema_version": self.schema_version,
            "source_root": self.source_root,
            "scripts_directory": self.scripts_directory,
            "task_map_status": self.task_map_status,
            "summary": self.summary(),
            "scripts": [asdict(item) for item in self.scripts],
            "errors": [asdict(item) for item in self.errors],
            "mapping_issues": [asdict(item) for item in self.mapping_issues],
            "verification_notice": (
                "AST and path evidence are candidates only; no source, provider, task, "
                "dataset, history, or production execution is verified by this report."
            ),
        }


def _path_tokens(path: Path) -> list[str]:
    """Split path components into comparable lowercase tokens."""
    tokens: list[str] = []
    for part in path.parts:
        tokens.extend(token for token in re.split(r"[^a-z0-9]+", part.lower()) if token)
    return tokens


def _family_candidate(relative_path: Path) -> tuple[str, str]:
    """Infer an asset family candidate from path and filename words."""
    directory_tokens = _path_tokens(relative_path.parent)
    filename_tokens = _path_tokens(relative_path.with_suffix(""))
    root_directory = relative_path.parts[0].lower() if len(relative_path.parts) > 1 else ""

    if root_directory == "funds" and "option" in filename_tokens:
        return "options", "filename token: option under funds directory"

    root_family = _FAMILY_ALIASES.get(root_directory)
    if root_family:
        return root_family, f"top-level directory: {root_directory}"

    for token in directory_tokens:
        family = _FAMILY_ALIASES.get(token)
        if family:
            return family, f"directory token: {token}"

    for token in filename_tokens:
        family = _FAMILY_ALIASES.get(token)
        if family:
            return family, f"filename token: {token}"

    if "common" in directory_tokens:
        return "other", "common directory fallback"
    return "unknown", "no recognized path token"


def _frequency_candidate(relative_path: Path) -> tuple[str, str]:
    """Infer a frequency candidate from directory names only."""
    candidates: list[tuple[str, str]] = []
    for part in relative_path.parent.parts:
        normalized = part.lower().replace("-", "_")
        frequency = _FREQUENCIES.get(normalized)
        if frequency and (frequency, part) not in candidates:
            candidates.append((frequency, part))

    if not candidates:
        return "unspecified", "no recognized frequency directory"
    labels = sorted({frequency for frequency, _ in candidates})
    evidence = ", ".join(part for _, part in candidates)
    if len(labels) > 1:
        return "multiple_candidates", evidence
    return labels[0], evidence


def _import_module_names(node: ast.AST) -> list[str]:
    """Return module names declared by a static import node."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [node.module]
    return []


def _qualified_name(node: ast.AST) -> str:
    """Render a static name or attribute chain without evaluating it."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _is_main_guard(node: ast.AST) -> bool:
    """Whether a comparison checks the conventional module entry guard."""
    if not isinstance(node, ast.Compare) or len(node.comparators) != 1:
        return False
    values: set[str] = set()
    for operand in (node.left, node.comparators[0]):
        qualified = _qualified_name(operand)
        if qualified:
            values.add(qualified)
        if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
            values.add(operand.value)
    return "__name__" in values and any(_MAIN_GUARD.match(value) for value in values)


def _analyze_ast(tree: ast.Module, record: ScriptRecord, source_text: str) -> None:
    """Populate static symbols and conservative call-path indicators."""
    functions: set[str] = set()
    classes: set[str] = set()
    direct_imports: set[str] = set()
    writer_refs: set[str] = set()
    module_main_call = False
    main_guard = False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in {"fetch_data", "run", "main"}:
                functions.add(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.add(node.name)
        elif isinstance(node, ast.Call) and node.args:
            call_name = _qualified_name(node.func).rsplit(".", maxsplit=1)[-1]
            first_argument = node.args[0]
            if (
                call_name in {"import_module", "__import__"}
                and isinstance(first_argument, ast.Constant)
                and isinstance(first_argument.value, str)
            ):
                module_name = first_argument.value
                if module_name.split(".", maxsplit=1)[0] in _PROVIDER_IMPORT_ROOTS:
                    direct_imports.add(f"dynamic:{module_name}")
                if module_name in _SHARED_WRITER_MODULES:
                    writer_refs.add(f"dynamic:{module_name}")

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for module_name in _import_module_names(node):
                root_name = module_name.split(".", maxsplit=1)[0]
                if root_name in _PROVIDER_IMPORT_ROOTS:
                    direct_imports.add(module_name)
                if module_name in _SHARED_WRITER_MODULES:
                    writer_refs.add(module_name)
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in _SHARED_WRITER_NAMES:
                        module_name = node.module or ""
                        writer_refs.add(f"{module_name}.{alias.name}".strip("."))

    def inspect_module_code(node: ast.AST) -> None:
        """Find calls and guards in module execution code, skipping definitions."""
        nonlocal main_guard, module_main_call
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            main_guard = True
        if isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id == "main")
            or _qualified_name(node.func).endswith(".main")
        ):
            module_main_call = True
        for child in ast.iter_child_nodes(node):
            inspect_module_code(child)

    for statement in tree.body:
        inspect_module_code(statement)

    record.entry_symbols = sorted(functions)
    record.module_main_call = module_main_call
    record.main_guard = main_guard
    record.class_names = sorted(classes)
    record.direct_source_imports = sorted(direct_imports)
    record.shared_writer_refs = sorted(writer_refs)

    role_parts = [
        f"entrypoint_{symbol}" for symbol in ("fetch_data", "run", "main") if symbol in functions
    ]
    if module_main_call:
        role_parts.append("module_main_call")
    if main_guard:
        role_parts.append("__main___guard")
    record.static_role = ";".join(role_parts) if role_parts else "helper_review_required"

    if any(
        item.removeprefix("dynamic:").split(".", maxsplit=1)[0] == "akshare"
        for item in direct_imports
    ):
        record.akshare_usage = "direct_akshare_import"
    elif writer_refs:
        record.akshare_usage = "shared_akshare_to_mysql"
    elif re.search(r"\bakshare\b", source_text, flags=re.IGNORECASE):
        record.akshare_usage = "text_only"

    if record.static_role == "helper_review_required":
        record.owner_candidate = "PENDING_REVIEW"
    elif record.asset_family in {"stock", "fund", "bond", "index"}:
        record.owner_candidate = "B"
    elif record.asset_family in {"futures", "options", "currency", "crypto", "macro", "other"}:
        record.owner_candidate = "C"


def _read_and_parse(path: Path, record: ScriptRecord) -> tuple[ast.Module | None, str]:
    """Read one file's raw bytes, hash them, and parse without importing it."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        record.parse_error = f"read failed: {exc.__class__.__name__}: {exc}"
        return None, ""

    record.source_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        source_text = raw.decode(encoding)
    except (SyntaxError, UnicodeDecodeError, LookupError) as exc:
        record.parse_error = f"decode failed: {exc.__class__.__name__}: {exc}"
        return None, ""

    try:
        tree = ast.parse(source_text, filename=record.source_path)
    except (SyntaxError, ValueError, RecursionError) as exc:
        if isinstance(exc, SyntaxError):
            location = f"line {exc.lineno or '?'}"
            if exc.offset is not None:
                location += f", column {exc.offset}"
            record.parse_error = f"syntax error at {location}: {exc.msg}"
        else:
            record.parse_error = f"parse failed: {exc.__class__.__name__}: {exc}"
        return None, source_text
    return tree, source_text


def _scan_files(root: Path) -> tuple[list[Path], list[InventoryError]]:
    """Walk Python files deterministically without following symlinks."""
    files: list[Path] = []
    errors: list[InventoryError] = []

    def visit(directory: Path) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            errors.append(
                InventoryError(
                    "scan_error",
                    directory.as_posix(),
                    exc.__class__.__name__,
                    str(exc),
                )
            )
            return

        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root)
            try:
                if entry.is_symlink():
                    errors.append(
                        InventoryError(
                            "scan_error",
                            relative.as_posix(),
                            "SymlinkSkipped",
                            "symbolic links are not followed by the read-only scanner",
                        )
                    )
                    continue
                if entry.is_dir(follow_symlinks=False):
                    visit(path)
                elif (
                    entry.is_file(follow_symlinks=False)
                    and path.suffix == ".py"
                    and path.name != "__init__.py"
                ):
                    files.append(path)
            except OSError as exc:
                errors.append(
                    InventoryError(
                        "scan_error",
                        relative.as_posix(),
                        exc.__class__.__name__,
                        str(exc),
                    )
                )

    visit(root)
    files.sort(key=lambda item: item.relative_to(root).as_posix())
    errors.sort(key=lambda item: (item.source_path, item.error_type, item.error_message))
    return files, errors


def _normalize_task_path(value: object) -> str | None:
    """Normalize an exact source-root-relative path or reject unsafe values."""
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("\\", "/")
    pure_path = PurePosixPath(normalized)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        return None
    normalized_path = pure_path.as_posix()
    return normalized_path[2:] if normalized_path.startswith("./") else normalized_path


def _load_task_map(path: Path) -> tuple[str, list[dict[str, object]], list[MappingIssue]]:
    """Load a task map, retaining malformed input as mapping issues."""
    try:
        resolved_path = path.expanduser().resolve()
    except OSError as exc:
        return "ERROR", [], [MappingIssue("mapping_issue", "task_map_read_error", detail=str(exc))]
    if resolved_path.name == ".env" or resolved_path.name.startswith(".env."):
        return (
            "ERROR",
            [],
            [
                MappingIssue(
                    "mapping_issue",
                    "environment_file_refused",
                    detail="environment files are not accepted as task-map input",
                )
            ],
        )
    try:
        raw = resolved_path.read_text(encoding="utf-8")
    except OSError as exc:
        return "ERROR", [], [MappingIssue("mapping_issue", "task_map_read_error", detail=str(exc))]
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return (
            "ERROR",
            [],
            [
                MappingIssue(
                    "mapping_issue",
                    "task_map_invalid_json",
                    detail=f"line {exc.lineno}, column {exc.colno}: {exc.msg}",
                )
            ],
        )

    tasks: object = decoded.get("tasks") if isinstance(decoded, dict) else decoded
    if not isinstance(tasks, list):
        return (
            "ERROR",
            [],
            [
                MappingIssue(
                    "mapping_issue",
                    "task_map_invalid_shape",
                    detail="expected a JSON list or an object containing a tasks list",
                )
            ],
        )
    valid_tasks = [task for task in tasks if isinstance(task, dict)]
    issues: list[MappingIssue] = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            issues.append(
                MappingIssue(
                    "mapping_issue",
                    "task_record_invalid",
                    detail=f"tasks[{index}] must be an object",
                )
            )
    return ("ERROR" if issues else "LOADED"), valid_tasks, issues


def _apply_task_map(
    report: InventoryReport,
    task_map_path: Path | None,
) -> None:
    """Attach exact task links and emit both-direction mapping issues."""
    if task_map_path is None:
        return

    status, tasks, issues = _load_task_map(task_map_path)
    report.task_map_status = status
    report.mapping_issues.extend(issues)
    scripts_by_path = {record.source_path: record for record in report.scripts}
    task_ids: set[str] = set()

    for index, task in enumerate(tasks):
        raw_task_id = task.get("task_id")
        if not isinstance(raw_task_id, str) or not raw_task_id.strip():
            report.mapping_issues.append(
                MappingIssue(
                    "mapping_issue",
                    "task_id_missing",
                    detail=f"tasks[{index}] requires a non-empty task_id",
                )
            )
            continue
        task_id = raw_task_id.strip()
        if task_id in task_ids:
            report.mapping_issues.append(
                MappingIssue(
                    "mapping_issue",
                    "duplicate_task_id",
                    task_id=task_id,
                    detail="task_id must be unique in the supplied task map",
                )
            )
        task_ids.add(task_id)

        raw_paths = task.get("script_paths", task.get("script_path"))
        if isinstance(raw_paths, str):
            path_values: list[object] = [raw_paths]
        elif isinstance(raw_paths, list):
            path_values = raw_paths
        else:
            report.mapping_issues.append(
                MappingIssue(
                    "mapping_issue",
                    "task_script_path_missing",
                    task_id=task_id,
                    detail="task requires script_path or script_paths",
                )
            )
            continue

        if not path_values:
            report.mapping_issues.append(
                MappingIssue(
                    "mapping_issue",
                    "task_script_path_missing",
                    task_id=task_id,
                    detail="script_paths cannot be empty",
                )
            )
            continue

        seen_paths: set[str] = set()
        for raw_path in path_values:
            source_path = _normalize_task_path(raw_path)
            if source_path is None:
                report.mapping_issues.append(
                    MappingIssue(
                        "mapping_issue",
                        "task_script_path_invalid",
                        task_id=task_id,
                        detail=f"unsafe or invalid script path: {raw_path!r}",
                    )
                )
                continue
            if source_path in seen_paths:
                report.mapping_issues.append(
                    MappingIssue(
                        "mapping_issue",
                        "duplicate_task_script_path",
                        task_id=task_id,
                        source_path=source_path,
                    )
                )
                continue
            seen_paths.add(source_path)
            record = scripts_by_path.get(source_path)
            if record is None:
                report.mapping_issues.append(
                    MappingIssue(
                        "mapping_issue",
                        "task_references_unknown_script",
                        task_id=task_id,
                        source_path=source_path,
                    )
                )
                continue
            record.mapped_task_ids.append(task_id)

    for record in report.scripts:
        record.mapped_task_ids.sort()
        if record.parse_error:
            record.mapping_status = "UNRESOLVED_PARSE"
            continue
        if record.mapped_task_ids:
            record.mapping_status = "MAPPED"
            continue
        if record.static_role == "helper_review_required":
            record.mapping_status = "UNMAPPED_HELPER_REVIEW"
            continue
        record.mapping_status = "UNMAPPED_ENTRYPOINT"
        report.mapping_issues.append(
            MappingIssue(
                "mapping_issue",
                "entrypoint_without_task",
                source_path=record.source_path,
                detail="entrypoint candidate has no exact task-map reference",
            )
        )

    report.mapping_issues.sort(
        key=lambda item: (item.issue, item.task_id, item.source_path, item.detail)
    )


def build_inventory(
    source_root: Path,
    *,
    scripts_relative_path: Path = DEFAULT_SCRIPTS_RELATIVE_PATH,
    task_map_path: Path | None = None,
) -> InventoryReport:
    """Scan Python files in a legacy repository without executing their code.

    Args:
        source_root: Root of the repository containing the legacy scripts.
        scripts_relative_path: Script tree relative to ``source_root``.
        task_map_path: Optional JSON file containing task-to-script links.

    Returns:
        A deterministic report. Parse and filesystem errors remain represented
        in the returned records and are not silently omitted.
    """
    root = source_root.expanduser().resolve()
    scripts_root = (root / scripts_relative_path).resolve()
    try:
        scripts_root.relative_to(root)
    except ValueError:
        error = InventoryError(
            "scan_error",
            scripts_relative_path.as_posix(),
            "PathOutsideSourceRoot",
            "script directory resolves outside the supplied source root",
        )
        return InventoryReport(
            INVENTORY_SCHEMA_VERSION,
            root.as_posix(),
            scripts_relative_path.as_posix(),
            "NOT_PROVIDED" if task_map_path is None else "ERROR",
            errors=[error],
        )

    report = InventoryReport(
        INVENTORY_SCHEMA_VERSION,
        root.as_posix(),
        scripts_relative_path.as_posix(),
        "NOT_PROVIDED" if task_map_path is None else "PENDING",
    )
    if not scripts_root.is_dir():
        report.errors.append(
            InventoryError(
                "scan_error",
                scripts_relative_path.as_posix(),
                "ScriptsDirectoryMissing",
                "script directory does not exist or is not a directory",
            )
        )
        _apply_task_map(report, task_map_path)
        return report

    files, scan_errors = _scan_files(scripts_root)
    report.errors.extend(scan_errors)
    if not files:
        report.errors.append(
            InventoryError(
                "scan_error",
                scripts_relative_path.as_posix(),
                "NoPythonScriptsFound",
                "no non-init Python scripts were discovered in the requested directory",
            )
        )
    for path in files:
        source_path = path.relative_to(root).as_posix()
        relative_path = path.relative_to(scripts_root)
        family, family_evidence = _family_candidate(relative_path)
        frequency, frequency_evidence = _frequency_candidate(relative_path)
        record = ScriptRecord(
            record_type="script",
            source_path=source_path,
            source_sha256=None,
            asset_family=family,
            asset_family_evidence=family_evidence,
            legacy_frequency=frequency,
            frequency_evidence=frequency_evidence,
            static_role="helper_review_required",
        )
        tree, source_text = _read_and_parse(path, record)
        if tree is not None:
            _analyze_ast(tree, record, source_text)
        if record.parse_error:
            if record.parse_error.startswith("syntax error"):
                error_type = "SyntaxError"
            elif record.parse_error.startswith("parse failed"):
                error_type = "ParseError"
            else:
                error_type = "ReadOrDecodeError"
            line_match = re.search(r"line (\d+)", record.parse_error)
            report.errors.append(
                InventoryError(
                    "parse_error",
                    source_path,
                    error_type,
                    record.parse_error,
                    int(line_match.group(1)) if line_match else None,
                )
            )
        report.scripts.append(record)

    report.errors.sort(key=lambda item: (item.source_path, item.record_type, item.error_message))
    _apply_task_map(report, task_map_path)
    return report


_CSV_FIELDS = (
    "record_type",
    "source_path",
    "source_sha256",
    "asset_family",
    "asset_family_evidence",
    "legacy_frequency",
    "frequency_evidence",
    "static_role",
    "entry_symbols",
    "module_main_call",
    "main_guard",
    "class_names",
    "direct_source_imports",
    "shared_writer_refs",
    "akshare_usage",
    "owner_candidate",
    "mapped_task_ids",
    "mapping_status",
    "migration_route",
    "target_dataset",
    "source_verification",
    "parse_error",
    "error_type",
    "error_message",
    "line",
    "column",
    "issue",
    "task_id",
    "detail",
)


def _json_cell(value: object) -> str:
    """Serialize a structured cell as stable compact JSON."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_csv(report: InventoryReport, stream: TextIO) -> None:
    """Write a flat CSV that retains script, scan-error, and mapping rows."""
    writer = csv.DictWriter(stream, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for script_record in report.scripts:
        row = asdict(script_record)
        for key in (
            "entry_symbols",
            "class_names",
            "direct_source_imports",
            "shared_writer_refs",
            "mapped_task_ids",
        ):
            row[key] = _json_cell(row[key])
        writer.writerow(row)
    for scan_error in report.errors:
        writer.writerow(asdict(scan_error))
    for mapping_issue in report.mapping_issues:
        writer.writerow(asdict(mapping_issue))


def render_json(report: InventoryReport) -> str:
    """Render a stable, UTF-8-friendly JSON report."""
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Run the inventory CLI and return non-zero for incomplete evidence."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="legacy repository root; source files are only read and parsed",
    )
    parser.add_argument(
        "--scripts-relative-path",
        type=Path,
        default=DEFAULT_SCRIPTS_RELATIVE_PATH,
        help="script directory relative to --source-root",
    )
    parser.add_argument("--task-map", type=Path, help="optional JSON task-to-script map")
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    parser.add_argument("--output", type=Path, help="output file; defaults to stdout")
    args = parser.parse_args(argv)

    report = build_inventory(
        args.source_root,
        scripts_relative_path=args.scripts_relative_path,
        task_map_path=args.task_map,
    )
    if args.format == "json":
        rendered = render_json(report)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
    else:
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("w", encoding="utf-8", newline="") as output:
                write_csv(report, output)
        else:
            write_csv(report, sys.stdout)

    if report.has_errors:
        summary = report.summary()
        print(
            "inventory incomplete: "
            f"{summary['parse_error_count']} parse errors, "
            f"{summary['filesystem_error_count']} filesystem errors, "
            f"{summary['mapping_issue_count']} mapping issues",
            file=sys.stderr,
        )
        return 1
    return 0
