"""Reconcile the frozen port scope against the current lock, manifest, and disk tree.

The scope inventory records why each path belongs to a delivery batch. Its counters and
reconciliation flags are descriptive only: this module derives the path sets and hashes again
from the current files before exposing a batch field to an acceptance probe.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

PORT_ROOT = "opendata_http"
PORT_MANIFEST_REL = f"{PORT_ROOT}/manifest.json"
UPSTREAM_LOCK_REL = f"{PORT_ROOT}/upstream.lock"
SCOPE_INVENTORY_REL = "docs/evidence/C65/port-scope-manifest.json"
IMPLEMENTATION_PLAN_REL = "docs/迭代计划/迭代1-重构数据中台/实施计划.md"
REQUIREMENTS_REL = "docs/迭代计划/迭代1-重构数据中台/需求文档.md"
EXPECTED_UPSTREAM_COMMIT = "c4f6a631c259783dbc2507b6b27d179b3e88079d"
BATCH_FIELD = f"{SCOPE_INVENTORY_REL}#files.batch"

A2_1_FUNCTION_ENTRYPOINTS = (
    ("stock_zh_a_hist", "akshare/stock_feature/stock_hist_em.py"),
    ("stock_zh_a_daily", "akshare/stock/stock_zh_a_sina.py"),
)
A2_1_DEFINITION_IMPORT_CLOSURE = frozenset(
    {
        "stock/cons.py",
        "stock/stock_zh_a_sina.py",
        "stock_feature/stock_hist_em.py",
        "utils/__init__.py",
        "utils/demjson.py",
        "utils/func.py",
        "utils/request.py",
        "utils/tqdm.py",
    }
)
A2_1_FULL_PATHS = frozenset(
    {
        "__init__.py",
        "stock/__init__.py",
        "stock/cons.py",
        "stock/stock_zh_a_sina.py",
        "stock_feature/__init__.py",
        "stock_feature/stock_hist_em.py",
        "utils/__init__.py",
        "utils/cons.py",
        "utils/context.py",
        "utils/demjson.py",
        "utils/func.py",
        "utils/multi_decrypt.py",
        "utils/request.py",
        "utils/token_process.py",
        "utils/tqdm.py",
    }
)
A2_1_MODULE_ROOTS = frozenset({"<root>", "stock", "stock_feature", "utils"})

B1_1_PLAN_MODULE_FILE_COUNTS = {
    "stock_feature": 69,
    "stock_fundamental": 23,
    "futures": 32,
    "index": 27,
    "fund": 27,
    "option": 19,
    "bond": 17,
    "economic": 19,
}
B1_1_EXTRA_D9_MODULES = frozenset({"futures_derivative"})
B1_1_FUTURES_DERIVATIVE_FILE_COUNT = 12
B1_1_RESOURCE_PATHS = frozenset({"stock_feature/ths.js"})
B1_1_PYTHON_FILE_COUNT = 245
B1_1_ARTIFACT_COUNT = 246

EXPECTED_TOTAL_FILES = 327
EXPECTED_PYTHON_FILES = 325
EXPECTED_RESOURCE_FILES = 2
EXPECTED_D9_EXCLUDED_MODULES = frozenset(
    {"movie", "news", "nlp", "air", "fortune", "cost", "article", "tool"}
)
EXPECTED_SCOPE_REQUIREMENTS = (
    "需求文档.md §3 FR-4 / D9",
    "需求文档.md §6.2 priority terminology",
    "实施计划.md A2.1, A2.3, B1.1, B1.2, B1.3",
)

_FOUNDATION_BATCHES = {
    "file_fold/__init__.py": "A1.1_TRADING_CALENDAR_FOUNDATION",
    "file_fold/calendar.json": "A1.1_TRADING_CALENDAR_FOUNDATION",
    "_version.py": "A1_SHARED_FOUNDATION",
    "exceptions.py": "A1_SHARED_FOUNDATION",
    "request.py": "A1_SHARED_FOUNDATION",
    "__init__.py": "A1.6_SHARED_FACADE",
    "datasets.py": "A2.3_RESOURCE_HANDLING",
}
_A2_1_B1_2_OVERLAPS = frozenset({"stock/cons.py", "stock/stock_zh_a_sina.py"})


@dataclass(frozen=True)
class PortScopeInputs:
    """JSON records and the two requirement texts used to audit one inventory."""

    inventory: object
    upstream_lock: object
    port_manifest: object
    implementation_plan: str
    requirements: str


@dataclass(frozen=True)
class PortScopeAudit:
    """Recomputed path and hash status for the frozen port inventory."""

    problems: tuple[str, ...]
    checked_paths: int
    python_paths: int
    resource_paths: int

    @property
    def valid(self) -> bool:
        """Whether every scope, metadata, path, and hash check passed."""
        return not self.problems

    @property
    def batch_field(self) -> str:
        """The field's exact source path, exposed only after a complete validation."""
        return BATCH_FIELD if self.valid else "-"

    @property
    def problem_summary(self) -> str:
        """A short, deterministic sample of the findings for a probe reading."""
        if not self.problems:
            return "-"
        shown = "; ".join(self.problems[:4])
        remaining = len(self.problems) - 4
        return f"{shown}; +{remaining} more" if remaining > 0 else shown


class PortScopePathError(ValueError):
    """A historical source identity has no safe current filesystem path."""


@lru_cache(maxsize=1)
def _source_layout_module() -> ModuleType:
    """Load the shared source identity map without depending on script import paths."""
    repo_root = Path(__file__).resolve().parents[2]
    layout_path = repo_root / "scripts/quality/source_layout.py"
    if not layout_path.is_file():
        raise PortScopePathError("scripts/quality/source_layout.py is missing")
    module_name = (
        "opendata_port_scope_source_layout_"
        + hashlib.sha256(str(layout_path).encode("utf-8")).hexdigest()[:16]
    )
    spec = importlib.util.spec_from_file_location(module_name, layout_path)
    if spec is None or spec.loader is None:
        raise PortScopePathError("scripts/quality/source_layout.py cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise PortScopePathError(f"scripts/quality/source_layout.py failed to load: {exc}") from exc
    return module


def _current_source_path(root: Path, historical_path: str) -> Path:
    """Resolve one historic identity to a safe current path under ``root``."""
    mapper = getattr(_source_layout_module(), "historical_identity", None)
    if not callable(mapper):
        raise PortScopePathError("source layout has no historical_identity mapper")
    try:
        current_identity = mapper(historical_path)
    except Exception as exc:
        raise PortScopePathError(f"cannot map historical path {historical_path!r}: {exc}") from exc
    if not isinstance(current_identity, str) or _safe_relative_path(current_identity) is None:
        raise PortScopePathError(
            f"historical path {historical_path!r} mapped to an unsafe current identity"
        )
    candidate = root / PurePosixPath(current_identity)
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise PortScopePathError(
            f"historical path {historical_path!r} escaped root via {current_identity!r}"
        ) from exc
    return candidate


def current_port_root(root: Path) -> Path:
    """Return the current vendor directory for the historic ``opendata_http`` identity."""
    return _current_source_path(root, PORT_ROOT)


def _current_ported_file(port_root: Path, historical_relative_path: str) -> Path:
    """Resolve one frozen port-relative path below the mapped current vendor directory."""
    safe = _safe_relative_path(historical_relative_path)
    if safe is None:
        raise PortScopePathError(
            f"invalid historical port-relative path: {historical_relative_path!r}"
        )
    candidate = port_root / PurePosixPath(safe)
    try:
        candidate.resolve().relative_to(port_root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise PortScopePathError(
            f"historical port-relative path escaped current root: {historical_relative_path!r}"
        ) from exc
    return candidate


def load_port_scope_inputs(root: Path) -> PortScopeInputs:
    """Read the current evidence, upstream lock, port manifest, and scope documents."""

    def read_json(relative: str) -> object:
        return json.loads((root / relative).read_text(encoding="utf-8"))

    def read_current_source_json(historical_path: str) -> object:
        current_path = _current_source_path(root, historical_path)
        if not current_path.is_file():
            current_identity = current_path.relative_to(root).as_posix()
            raise PortScopePathError(
                f"historical source {historical_path!r} maps to canonical "
                f"{current_identity!r}, but that file is missing"
            )
        return json.loads(current_path.read_text(encoding="utf-8"))

    upstream_lock = read_current_source_json(UPSTREAM_LOCK_REL)
    port_manifest = read_current_source_json(PORT_MANIFEST_REL)
    inventory = read_json(SCOPE_INVENTORY_REL)
    return PortScopeInputs(
        inventory=inventory,
        upstream_lock=upstream_lock,
        port_manifest=port_manifest,
        implementation_plan=(root / IMPLEMENTATION_PLAN_REL).read_text(encoding="utf-8"),
        requirements=(root / REQUIREMENTS_REL).read_text(encoding="utf-8"),
    )


def audit_port_scope(root: Path) -> PortScopeAudit:
    """Read and reconcile the live checkout, returning findings rather than trusting claims."""
    try:
        inputs = load_port_scope_inputs(root)
    except PortScopePathError as exc:
        return PortScopeAudit(
            problems=(f"scope source path mapping failed: {exc}",),
            checked_paths=0,
            python_paths=0,
            resource_paths=0,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return PortScopeAudit(
            problems=(f"scope inputs could not be read ({type(exc).__name__})",),
            checked_paths=0,
            python_paths=0,
            resource_paths=0,
        )
    return validate_port_scope(root, inputs)


def validate_port_scope(root: Path, inputs: PortScopeInputs) -> PortScopeAudit:
    """Validate supplied inventory data against the current lock, manifest, and disk paths."""
    problems: list[str] = []
    inventory = _as_mapping(inputs.inventory)
    lock = _as_mapping(inputs.upstream_lock)
    port_manifest = _as_mapping(inputs.port_manifest)
    if inventory is None:
        problems.append("C65 scope inventory is not a JSON object")
    if lock is None:
        problems.append("upstream.lock is not a JSON object")
    if port_manifest is None:
        problems.append("ported manifest is not a JSON object")
    if inventory is None or lock is None or port_manifest is None:
        return _audit_result(problems, 0, 0, 0)

    lock_rows = _rows(lock.get("files"), UPSTREAM_LOCK_REL, problems)
    port_files = _rows(port_manifest.get("files"), f"{PORT_MANIFEST_REL}#files", problems)
    port_resources = _rows(
        port_manifest.get("resources"), f"{PORT_MANIFEST_REL}#resources", problems
    )
    evidence_rows = _rows(inventory.get("files"), f"{SCOPE_INVENTORY_REL}#files", problems)
    port_rows = dict(port_files)
    for path, row in port_resources.items():
        if path in port_rows:
            problems.append(f"ported manifest repeats path across file/resource sections: {path}")
        else:
            port_rows[path] = row

    lock_paths = set(lock_rows)
    port_paths = set(port_rows)
    evidence_paths = set(evidence_rows)
    try:
        port_root = current_port_root(root)
    except PortScopePathError as exc:
        return _audit_result([f"current port root mapping failed: {exc}"], 0, 0, 0)
    disk_paths = _disk_paths(port_root)
    for label, actual in (
        ("ported manifest", port_paths),
        ("scope inventory", evidence_paths),
        ("disk tree", disk_paths),
    ):
        if actual != lock_paths:
            missing = sorted(lock_paths - actual)[:4]
            extra = sorted(actual - lock_paths)[:4]
            problems.append(
                f"{label}/upstream.lock path sets differ: missing={missing or '-'}, "
                f"extra={extra or '-'}"
            )

    if len(lock_paths) != EXPECTED_TOTAL_FILES:
        problems.append(f"upstream.lock has {len(lock_paths)} unique paths; expected 327")
    python_paths = {path for path in lock_paths if PurePosixPath(path).suffix == ".py"}
    resource_paths = lock_paths - python_paths
    if len(python_paths) != EXPECTED_PYTHON_FILES:
        problems.append(f"upstream.lock has {len(python_paths)} Python paths; expected 325")
    if len(resource_paths) != EXPECTED_RESOURCE_FILES:
        problems.append(f"upstream.lock has {len(resource_paths)} resource paths; expected 2")

    lock_upstream = _as_mapping(lock.get("upstream")) or {}
    port_upstream = _as_mapping(port_manifest.get("upstream")) or {}
    lock_commit = lock_upstream.get("commit")
    port_commit = port_upstream.get("commit")
    evidence_commit = inventory.get("upstream_commit")
    if not (lock_commit == port_commit == evidence_commit == EXPECTED_UPSTREAM_COMMIT):
        problems.append("upstream commit differs across lock, port manifest, and C65 inventory")
    if lock.get("version") != 1 or port_manifest.get("version") != 1:
        problems.append("upstream.lock or port manifest version is not 1")
    if inventory.get("schema_version") != 1:
        problems.append("C65 scope inventory schema_version is not 1")

    expected_batch_modules = _requirement_batch_modules(
        inputs.implementation_plan, inputs.requirements, problems
    )
    _validate_scope_basis(inventory.get("scope_basis"), expected_batch_modules, problems)
    _validate_rows(
        port_root=port_root,
        lock_rows=lock_rows,
        port_rows=port_rows,
        port_file_paths=set(port_files),
        evidence_rows=evidence_rows,
        problems=problems,
    )
    return _audit_result(
        problems, len(lock_paths & disk_paths), len(python_paths), len(resource_paths)
    )


def _as_mapping(value: object) -> Mapping[str, object] | None:
    """Return a string-keyed JSON object when the shape is valid."""
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        return None
    return value


def _rows(value: object, label: str, problems: list[str]) -> dict[str, Mapping[str, object]]:
    """Index records by a safe, unique relative path and report every structural defect."""
    if not isinstance(value, list):
        problems.append(f"{label} is not a list")
        return {}
    indexed: dict[str, Mapping[str, object]] = {}
    for index, candidate in enumerate(value):
        row = _as_mapping(candidate)
        if row is None:
            problems.append(f"{label}[{index}] is not an object")
            continue
        path = _safe_relative_path(row.get("path"))
        if path is None:
            problems.append(f"{label}[{index}] has an invalid relative path")
            continue
        if path in indexed:
            problems.append(f"{label} contains a duplicate path: {path}")
            continue
        indexed[path] = row
    return indexed


def _safe_relative_path(value: object) -> str | None:
    """Reject absolute, traversing, platform-specific, and non-normalized paths."""
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        return None
    return value


def _disk_paths(port_root: Path) -> set[str]:
    """List actual port payload files, excluding only the two inventory metadata files."""
    if not port_root.is_dir():
        return set()
    ignored = {"manifest.json", "upstream.lock"}
    return {
        path.relative_to(port_root).as_posix()
        for path in port_root.rglob("*")
        if path.is_file() and path.name not in ignored and "__pycache__" not in path.parts
    }


def _requirement_batch_modules(
    implementation_plan: str, requirements: str, problems: list[str]
) -> frozenset[str]:
    """Derive B1.1 directories from the plan and the explicit D9 futures extension."""
    plan_line = next(
        (line for line in implementation_plan.splitlines() if re.match(r"^\|\s*B1\.1\s*\|", line)),
        "",
    )
    plan_match = re.search(r"(?:\(|（)([^()（）]*)(?:\)|）)", plan_line)
    if plan_match is None:
        problems.append("implementation plan has no B1.1 directory list")
        plan_modules: set[str] = set()
    else:
        plan_modules = {
            re.sub(r"\s*等\s*$", "", part).strip()
            for part in plan_match.group(1).split("/")
            if part.strip() and part.strip() != "等"
        }
    expected_plan_modules = set(B1_1_PLAN_MODULE_FILE_COUNTS)
    if plan_modules != expected_plan_modules:
        problems.append(
            "implementation plan B1.1 directories differ from the fixed eight-module scope"
        )

    d9_line = next((line for line in requirements.splitlines() if "搬运范围（D9）" in line), "")
    d9_match = re.search(r"搬运范围（D9）[^（]*（([^）]*)）", d9_line)
    if d9_match is None:
        problems.append("requirements D9 has no module list")
        d9_modules: set[str] = set()
    else:
        d9_modules = {
            part.strip()
            for part in d9_match.group(1).split("、")
            if part.strip() and part.strip() != "等"
        }
    if not d9_modules >= B1_1_EXTRA_D9_MODULES:
        problems.append("requirements D9 no longer explicitly names futures_derivative")
    return frozenset(expected_plan_modules | B1_1_EXTRA_D9_MODULES)


def _validate_scope_basis(
    raw_basis: object, batch_modules: frozenset[str], problems: list[str]
) -> None:
    """Compare scope claims to fixed requirements and paths, never their claimed totals alone."""
    basis = _as_mapping(raw_basis)
    if basis is None:
        problems.append("scope_basis is not an object")
        return

    _compare_path_list(
        basis.get("a2_1_definition_import_closure_files"),
        A2_1_DEFINITION_IMPORT_CLOSURE,
        "A2.1 definition-import closure",
        problems,
    )
    _compare_path_list(
        basis.get("a2_1_full_utils_and_public_package_path_files"),
        A2_1_FULL_PATHS,
        "A2.1 full path set",
        problems,
    )
    _compare_path_list(
        basis.get("a2_1_module_roots"), A2_1_MODULE_ROOTS, "A2.1 module roots", problems
    )
    entrypoints = basis.get("a2_1_function_entrypoints")
    actual_entrypoints: set[tuple[str, str]] = set()
    if isinstance(entrypoints, list):
        for item in entrypoints:
            row = _as_mapping(item)
            if row is None:
                continue
            function = row.get("public_function")
            path = row.get("upstream_path")
            if isinstance(function, str) and isinstance(path, str):
                actual_entrypoints.add((function, path))
    if (
        actual_entrypoints != set(A2_1_FUNCTION_ENTRYPOINTS)
        or not isinstance(entrypoints, list)
        or len(entrypoints) != len(A2_1_FUNCTION_ENTRYPOINTS)
    ):
        problems.append(
            "A2.1 public function entrypoints differ from the two fixed daily-line functions"
        )
    if basis.get("a2_1_source_file_count") != len(A2_1_FULL_PATHS):
        problems.append("A2.1 source_file_count differs from its 15-path set")

    groups = basis.get("b1_1_groups")
    group_counts: dict[str, int] = {}
    if isinstance(groups, list):
        for item in groups:
            row = _as_mapping(item)
            if row is None:
                continue
            module = row.get("module")
            file_count = row.get("python_file_count")
            if (
                isinstance(module, str)
                and isinstance(file_count, int)
                and not isinstance(file_count, bool)
            ):
                if module in group_counts:
                    problems.append(f"scope_basis repeats B1.1 group {module}")
                group_counts[module] = file_count
    expected_counts = dict(B1_1_PLAN_MODULE_FILE_COUNTS)
    expected_counts["futures_derivative"] = B1_1_FUTURES_DERIVATIVE_FILE_COUNT
    if (
        group_counts != expected_counts
        or not isinstance(groups, list)
        or len(groups) != len(expected_counts)
    ):
        problems.append(
            "scope_basis B1.1 group names/counts differ from the required nine directories"
        )
    if basis.get("b1_1_group_count") != len(batch_modules):
        problems.append("scope_basis B1.1 group_count differs from the nine required directories")
    if basis.get("b1_1_python_file_count") != B1_1_PYTHON_FILE_COUNT:
        problems.append("scope_basis B1.1 Python count differs from the recomputed 245-path scope")
    _compare_path_list(
        basis.get("b1_1_resource_paths"),
        B1_1_RESOURCE_PATHS,
        "B1.1 resource paths",
        problems,
    )
    if basis.get("b1_1_artifact_count_including_resources") != B1_1_ARTIFACT_COUNT:
        problems.append(
            "scope_basis B1.1 artifact count differs from 245 Python files plus one resource"
        )
    _compare_path_list(
        basis.get("d9_nonfinancial_modules_absent_from_frozen_port"),
        EXPECTED_D9_EXCLUDED_MODULES,
        "D9 excluded modules",
        problems,
    )
    if (
        not isinstance(basis.get("b1_2_on_demand_reason"), str)
        or not str(basis.get("b1_2_on_demand_reason", "")).strip()
    ):
        problems.append("scope_basis is missing the B1.2 on-demand rationale")
    _compare_path_list(
        basis.get("requirements"),
        EXPECTED_SCOPE_REQUIREMENTS,
        "scope_basis requirements",
        problems,
    )


def _compare_path_list(
    actual: object,
    expected: set[str] | frozenset[str] | tuple[str, ...],
    label: str,
    problems: list[str],
) -> None:
    """Compare a JSON list to a fixed path set, rejecting duplicates and malformed values."""
    if not isinstance(actual, list) or not all(isinstance(value, str) for value in actual):
        problems.append(f"scope_basis {label} is not a string list")
        return
    values = [str(value) for value in actual]
    if len(values) != len(set(values)) or set(values) != set(expected):
        missing = sorted(set(expected) - set(values))[:4]
        extra = sorted(set(values) - set(expected))[:4]
        problems.append(
            f"scope_basis {label} differs: missing={missing or '-'}, extra={extra or '-'}"
        )


def _expected_batches(path: str, kind: str, batch_modules: frozenset[str]) -> frozenset[str]:
    """Derive the allowed batch labels solely from the path and fixed scope rules."""
    batches: set[str] = set()
    foundation = _FOUNDATION_BATCHES.get(path)
    if foundation is not None:
        batches.add(foundation)
    if path in A2_1_FULL_PATHS:
        batches.add("A2.1")
    root_module = PurePosixPath(path).parts[0]
    if kind == "python" and root_module in batch_modules:
        batches.add("B1.1_FUTURES_DERIVATIVE" if root_module == "futures_derivative" else "B1.1")
    if path in B1_1_RESOURCE_PATHS:
        batches.add("B1.1")
    if path in _A2_1_B1_2_OVERLAPS:
        batches.add("B1.2_ON_DEMAND")
    if kind == "python" and not batches:
        batches.add("B1.2_ON_DEMAND")
    return frozenset(batches)


def _validate_rows(
    *,
    port_root: Path,
    lock_rows: Mapping[str, Mapping[str, object]],
    port_rows: Mapping[str, Mapping[str, object]],
    port_file_paths: set[str],
    evidence_rows: Mapping[str, Mapping[str, object]],
    problems: list[str],
) -> None:
    """Recompute per-path ownership, reason, hash, and batch labels."""
    batch_modules = frozenset(B1_1_PLAN_MODULE_FILE_COUNTS) | B1_1_EXTRA_D9_MODULES
    b1_python_counts = dict.fromkeys(batch_modules, 0)
    for path in lock_rows:
        if path in port_file_paths and PurePosixPath(path).suffix == ".py":
            module = PurePosixPath(path).parts[0]
            if module in b1_python_counts:
                b1_python_counts[module] += 1
    expected_group_counts = dict(B1_1_PLAN_MODULE_FILE_COUNTS)
    expected_group_counts["futures_derivative"] = B1_1_FUTURES_DERIVATIVE_FILE_COUNT
    if b1_python_counts != expected_group_counts:
        problems.append(
            "current lock/manifest B1.1 path counts differ from the nine required groups"
        )
    b1_python_total = sum(b1_python_counts.values())
    if b1_python_total != B1_1_PYTHON_FILE_COUNT:
        problems.append(
            f"current lock/manifest B1.1 Python paths total {b1_python_total}; expected 245"
        )
    present_b1_resources = set(lock_rows) & B1_1_RESOURCE_PATHS
    if present_b1_resources != B1_1_RESOURCE_PATHS:
        problems.append("current lock is missing the B1.1 ths.js resource path")

    for path in sorted(set(lock_rows) | set(port_rows) | set(evidence_rows)):
        lock_row = lock_rows.get(path)
        port_row = port_rows.get(path)
        evidence_row = evidence_rows.get(path)
        if lock_row is None or port_row is None or evidence_row is None:
            continue
        kind = "python" if path in port_file_paths else "resource"
        expected_kind = "python" if PurePosixPath(path).suffix == ".py" else "resource"
        if kind != expected_kind:
            problems.append(f"ported manifest assigns the wrong section to {path}")
        if evidence_row.get("kind") != kind:
            problems.append(f"scope inventory assigns the wrong kind to {path}")

        lock_upstream_path = lock_row.get("upstream_path")
        if not isinstance(lock_upstream_path, str) or not lock_upstream_path.startswith("akshare/"):
            problems.append(f"upstream.lock has an invalid source path for {path}")
        for label, row in (("ported manifest", port_row), ("scope inventory", evidence_row)):
            if row.get("upstream_path") != lock_upstream_path:
                problems.append(f"{label} upstream_path differs from upstream.lock for {path}")
        manual_edits = lock_row.get("manual_edits")
        if not isinstance(manual_edits, bool):
            problems.append(f"upstream.lock manual_edits is not boolean for {path}")
        if kind == "python" and port_row.get("manual_edits") != manual_edits:
            problems.append(f"ported manifest manual_edits differs from upstream.lock for {path}")
        for label, row in (("scope inventory", evidence_row),):
            if row.get("manual_edits") != manual_edits:
                problems.append(f"{label} manual_edits differs from upstream.lock for {path}")

        reason = evidence_row.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"scope inventory rationale is empty for {path}")
        batch = evidence_row.get("batch")
        if not isinstance(batch, list) or not all(isinstance(label, str) for label in batch):
            problems.append(f"scope inventory batch is not a string list for {path}")
        else:
            labels = [str(label) for label in batch]
            expected = _expected_batches(path, kind, batch_modules)
            if len(labels) != len(set(labels)) or set(labels) != set(expected):
                problems.append(
                    f"scope inventory batch differs for {path}: "
                    f"expected={sorted(expected)}, actual={sorted(set(labels))}"
                )

        ported_sha = port_row.get("sha256")
        lock_sha = lock_row.get("sha256")
        scope_sha = _as_mapping(evidence_row.get("sha256")) or {}
        inventory_lock_sha = scope_sha.get("upstream_lock")
        inventory_port_sha = scope_sha.get("manifest_ported_snapshot")
        if not _is_sha256(ported_sha):
            problems.append(f"ported manifest sha256 is invalid for {path}")
        if not _is_sha256(lock_sha):
            problems.append(f"upstream.lock sha256 is invalid for {path}")
        if inventory_lock_sha != lock_sha:
            problems.append(f"scope inventory upstream hash differs from upstream.lock for {path}")
        if inventory_port_sha != ported_sha:
            problems.append(f"scope inventory ported hash differs from manifest for {path}")

        try:
            disk_path = _current_ported_file(port_root, path)
        except PortScopePathError as exc:
            problems.append(f"ported path has no safe current identity: {path}: {exc}")
            continue
        if not disk_path.is_file():
            problems.append(f"ported path is missing from disk: {path}")
            continue
        try:
            disk_sha = hashlib.sha256(disk_path.read_bytes()).hexdigest()
        except OSError:
            problems.append(f"ported path could not be read from disk: {path}")
            continue
        if disk_sha != ported_sha:
            problems.append(f"disk hash differs from port manifest for {path}")
        if disk_sha != inventory_port_sha:
            problems.append(f"disk hash differs from C65 inventory for {path}")


def _is_sha256(value: object) -> bool:
    """Whether a value is a lowercase SHA-256 hex digest."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _audit_result(
    problems: list[str], checked_paths: int, python_paths: int, resource_paths: int
) -> PortScopeAudit:
    """Return a stable unique finding list and recomputed path counts."""
    return PortScopeAudit(
        problems=tuple(dict.fromkeys(problems)),
        checked_paths=checked_paths,
        python_paths=python_paths,
        resource_paths=resource_paths,
    )
