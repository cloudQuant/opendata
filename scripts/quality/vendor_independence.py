"""Audit the migrated MIT vendor tree for imports across the BSL boundary.

The audit is intentionally independent of the historical C65 scope and security
evidence. It verifies the current vendor manifest, parses every Python source,
checks import targets and lazy facade exports, then exercises selected imports
from a copied vendor-only namespace package in an isolated interpreter.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil

# Used only to run the fixed local interpreter on copied sources with shell execution disabled.
import subprocess  # nosec B404  # one run(): sys.executable -I -S -c, shell=False
import sys
import tempfile
import tokenize
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

VENDOR_REL = Path("opendata/data/providers/akshare/_vendor")
VENDOR_PREFIX = "opendata.data.providers.akshare._vendor"
NAMESPACE_ANCESTORS = (
    "opendata",
    "opendata.data",
    "opendata.data.providers",
    "opendata.data.providers.akshare",
)
LEGACY_ROOTS = ("opendata_http", "opendata_client", "opendata_fuyao", "akshare")
EXPECTED_PYTHON_FILES = 325
EXPECTED_RESOURCE_FILES = 2
UNMANIFESTED_METADATA_FILES = {"manifest.json", "upstream.lock", "LICENSE-AKSHARE"}
ENTRY_IMPORTS = (
    f"{VENDOR_PREFIX}.datasets",
    f"{VENDOR_PREFIX}.stock.cons",
    f"{VENDOR_PREFIX}.utils",
)
CONTROL_IMPORTS = {
    "core_database": "opendata.core.database",
    "sibling_provider": "opendata.data.providers.bls",
    "client_root": "opendata_client",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Crossing:
    """One import or facade reference that crosses the vendor boundary."""

    path: str
    line: int
    kind: str
    target: str
    reason: str


@dataclass
class VendorTreeAudit:
    """Measured source and manifest facts for one vendor root."""

    vendor_root: str
    expected_python_files: int | None
    actual_python_files: int = 0
    manifest_python_files: int = 0
    actual_resource_files: int = 0
    manifest_resource_files: int = 0
    parsed_python_files: int = 0
    vendor_imports: int = 0
    namespace_imports: int = 0
    third_party_imports: int = 0
    facade_module_paths: int = 0
    facade_vendor_module_paths: int = 0
    facade_third_party_module_paths: int = 0
    facade_third_party_targets: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    ast_crossings: list[Crossing] = field(default_factory=list)
    source_pins: dict[str, object] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        """Whether all file, parse, hash, import, and facade checks passed."""
        return not self.issues and not self.ast_crossings


@dataclass
class IsolationAudit:
    """Observed facts from the isolated copied-tree import process."""

    valid: bool
    status: str
    imports: dict[str, object] = field(default_factory=dict)
    controls: dict[str, object] = field(default_factory=dict)
    ancestors: dict[str, object] = field(default_factory=dict)
    loaded_opendata: dict[str, object] = field(default_factory=dict)
    sys_path: list[str] = field(default_factory=list)
    baseline_sys_path: list[str] = field(default_factory=list)
    cwd: str = ""
    repository_path_leaks: list[str] = field(default_factory=list)
    blocked_imports: list[str] = field(default_factory=list)
    network_attempts: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _reject_duplicate_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _safe_relative_py(value: object) -> str | None:
    relative = _safe_relative_file(value)
    if relative is None or PurePosixPath(relative).suffix != ".py":
        return None
    return relative


def _safe_relative_file(value: object) -> str | None:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute():
        return None
    if any(part in {"", ".", ".."} for part in value.split("/")):
        return None
    return path.as_posix()


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _source_module(path: str) -> tuple[str, str, bool]:
    """Return (module, package, is_package) for a vendor-relative Python path."""
    relative = PurePosixPath(path)
    parts = list(relative.parts)
    if parts[-1] == "__init__.py":
        module_parts = parts[:-1]
        is_package = True
    else:
        module_parts = [*parts[:-1], relative.stem]
        is_package = False
    module = ".".join((VENDOR_PREFIX, *module_parts)) if module_parts else VENDOR_PREFIX
    package = module if is_package else module.rpartition(".")[0]
    return module, package, is_package


def _resolve_from_module(package: str, node: ast.ImportFrom) -> tuple[str | None, str | None]:
    """Resolve an ImportFrom base, returning a module and optional error."""
    if node.level == 0:
        if node.module is None:
            return None, "absolute import has no module name"
        return node.module, None

    package_parts = package.split(".") if package else []
    climbs = node.level - 1
    if not package_parts or climbs >= len(package_parts):
        return None, f"relative import level {node.level} escapes the top-level package"
    base_parts = package_parts[: len(package_parts) - climbs]
    if node.module:
        base_parts.extend(node.module.split("."))
    if not base_parts:
        return None, f"relative import level {node.level} resolves to an empty module"
    return ".".join(base_parts), None


def _target_kind(target: str) -> str:
    if target == VENDOR_PREFIX or target.startswith(f"{VENDOR_PREFIX}."):
        return "vendor"
    if target in NAMESPACE_ANCESTORS:
        return "namespace"
    if target == "opendata" or target.startswith("opendata."):
        return "blocked"
    root = target.split(".", maxsplit=1)[0]
    if root in LEGACY_ROOTS or any(root.startswith(f"{legacy}_") for legacy in LEGACY_ROOTS):
        return "blocked"
    if root == "openbb" or root.startswith("openbb_"):
        return "blocked"
    return "third_party"


def _vendor_module_is_manifested(target: str, manifest_paths: set[str]) -> bool:
    """Whether a canonical vendor module maps to a manifest file or package."""
    if target != VENDOR_PREFIX and not target.startswith(f"{VENDOR_PREFIX}."):
        return False
    suffix = target[len(VENDOR_PREFIX) :].lstrip(".")
    relative = suffix.replace(".", "/")
    candidates = (
        f"{relative}.py" if relative else "__init__.py",
        f"{relative}/__init__.py" if relative else "__init__.py",
    )
    return any(candidate in manifest_paths for candidate in candidates)


def _record_target(
    *,
    audit: VendorTreeAudit,
    path: str,
    line: int,
    kind: str,
    target: str,
    manifest_paths: set[str],
    allow_namespace: bool = True,
) -> str:
    target_kind = _target_kind(target)
    if target_kind == "vendor":
        audit.vendor_imports += 1
        if not _vendor_module_is_manifested(target, manifest_paths):
            audit.issues.append(f"vendor import target is not in the manifest: {target}")
    elif target_kind == "namespace" and allow_namespace:
        audit.namespace_imports += 1
    elif target_kind == "third_party":
        audit.third_party_imports += 1
    else:
        audit.ast_crossings.append(
            Crossing(
                path=path,
                line=line,
                kind=kind,
                target=target,
                reason="import is outside the isolated MIT vendor namespace",
            )
        )
    return target_kind


def _inspect_imports(
    tree: ast.AST,
    *,
    path: str,
    package: str,
    audit: VendorTreeAudit,
    manifest_paths: set[str],
) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _record_target(
                    audit=audit,
                    path=path,
                    line=node.lineno,
                    kind="import",
                    target=alias.name,
                    manifest_paths=manifest_paths,
                )
        elif isinstance(node, ast.ImportFrom):
            resolved, error = _resolve_from_module(package, node)
            if error is not None or resolved is None:
                audit.ast_crossings.append(
                    Crossing(
                        path=path,
                        line=node.lineno,
                        kind="from",
                        target=f"level={node.level}; module={node.module or ''}",
                        reason=error or "relative import could not be resolved",
                    )
                )
                continue

            base_kind = _record_target(
                audit=audit,
                path=path,
                line=node.lineno,
                kind="from",
                target=resolved,
                manifest_paths=manifest_paths,
            )
            # Namespace ancestors have no copied Python attributes. Any imported
            # child name would resolve into a BSL subtree unless it remains one
            # of the explicitly allowed namespace ancestors or enters _vendor.
            if base_kind == "namespace":
                for alias in node.names:
                    child = f"{resolved}.{alias.name}" if alias.name != "*" else resolved
                    child_kind = _target_kind(child)
                    if child_kind not in {"namespace", "vendor"}:
                        audit.ast_crossings.append(
                            Crossing(
                                path=path,
                                line=node.lineno,
                                kind="from-child",
                                target=child,
                                reason="imported child leaves the copied namespace ancestors",
                            )
                        )
                    elif child_kind == "vendor" and not _vendor_module_is_manifested(
                        child, manifest_paths
                    ):
                        audit.issues.append(f"vendor import target is not in the manifest: {child}")


def _facade_assignments(tree: ast.Module) -> list[ast.Assign | ast.AnnAssign]:
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and (
            (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "_EXPORTS"
                    for target in node.targets
                )
            )
            or (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "_EXPORTS"
            )
        )
    ]


def _inspect_facade(vendor_root: Path, audit: VendorTreeAudit, manifest_paths: set[str]) -> None:
    facade_path = vendor_root / "__init__.py"
    if not facade_path.is_file():
        audit.issues.append("vendor facade __init__.py is missing")
        return
    try:
        facade_tree = ast.parse(facade_path.read_text(encoding="utf-8"), filename=str(facade_path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        audit.issues.append(f"vendor facade cannot be parsed ({type(exc).__name__})")
        return
    assignments = _facade_assignments(facade_tree)
    if len(assignments) != 1:
        audit.issues.append("vendor facade must contain exactly one literal _EXPORTS assignment")
        return

    assignment = assignments[0]
    value_node = assignment.value
    if not isinstance(value_node, ast.Dict):
        audit.issues.append("vendor facade _EXPORTS is not a literal dictionary")
        return

    seen_keys: set[str] = set()
    for key_node, export_node in zip(value_node.keys, value_node.values, strict=True):
        try:
            key = ast.literal_eval(key_node) if key_node is not None else None
            export = ast.literal_eval(export_node)
        except (ValueError, TypeError, SyntaxError):
            audit.issues.append("vendor facade _EXPORTS contains a non-literal entry")
            continue
        if not isinstance(key, str) or key in seen_keys:
            audit.issues.append("vendor facade _EXPORTS has a non-string or duplicate key")
            continue
        seen_keys.add(key)
        if not isinstance(export, (tuple, list)) or not export or not isinstance(export[0], str):
            audit.issues.append(f"vendor facade export {key!r} has no literal module path")
            continue
        module_path = export[0]
        audit.facade_module_paths += 1
        target_kind = _target_kind(module_path)
        if target_kind == "third_party":
            audit.facade_third_party_module_paths += 1
            audit.facade_third_party_targets.append(module_path)
            continue
        if target_kind != "vendor":
            audit.ast_crossings.append(
                Crossing(
                    path="__init__.py",
                    line=getattr(export_node, "lineno", 1),
                    kind="facade-export",
                    target=module_path,
                    reason="lazy facade module path is outside the vendor namespace",
                )
            )
            continue
        audit.facade_vendor_module_paths += 1
        if not _vendor_module_is_manifested(module_path, manifest_paths):
            audit.issues.append(
                f"vendor facade export {key!r} points to an unmanifested module: {module_path}"
            )


def audit_vendor_sources(
    vendor_root: Path,
    *,
    expected_python_files: int | None = EXPECTED_PYTHON_FILES,
    expected_resource_files: int | None = None,
) -> VendorTreeAudit:
    """Reconcile, hash, parse, and boundary-check every Python file in a vendor root."""
    root = vendor_root.resolve()
    audit = VendorTreeAudit(str(root), expected_python_files)
    manifest_path = root / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(
            manifest_bytes.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_pairs
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        audit.issues.append(f"vendor manifest is missing or invalid ({type(exc).__name__})")
        return audit
    if not isinstance(manifest, dict):
        audit.issues.append("vendor manifest is not a JSON object")
        return audit

    manifest_rows = manifest.get("files")
    if not isinstance(manifest_rows, list):
        audit.issues.append("vendor manifest files is not a list")
        manifest_rows = []
    resource_rows = manifest.get("resources")
    if not isinstance(resource_rows, list):
        audit.issues.append("vendor manifest resources is not a list")
        resource_rows = []

    manifest_hashes: dict[str, str] = {}
    resource_hashes: dict[str, str] = {}
    all_manifest_paths: set[str] = set()
    upstream_paths: set[str] = set()
    for index, raw_row in enumerate(manifest_rows):
        if not isinstance(raw_row, dict):
            audit.issues.append(f"vendor manifest files[{index}] is not an object")
            continue
        relative = _safe_relative_py(raw_row.get("path"))
        digest = raw_row.get("sha256")
        if relative is None or not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            audit.issues.append(f"vendor manifest files[{index}] has an invalid path or sha256")
            continue
        upstream_path = _safe_relative_py(raw_row.get("upstream_path"))
        if upstream_path is None:
            audit.issues.append(f"vendor manifest files[{index}] has an invalid upstream_path")
        elif upstream_path in upstream_paths:
            audit.issues.append(f"vendor manifest repeats upstream identity: {upstream_path}")
        else:
            upstream_paths.add(upstream_path)
        if relative in manifest_hashes:
            audit.issues.append(f"vendor manifest repeats Python identity: {relative}")
            continue
        if relative in all_manifest_paths:
            audit.issues.append(f"vendor manifest repeats file identity: {relative}")
            continue
        all_manifest_paths.add(relative)
        manifest_hashes[relative] = digest
    audit.manifest_python_files = len(manifest_hashes)

    for index, raw_row in enumerate(resource_rows):
        if not isinstance(raw_row, dict):
            audit.issues.append(f"vendor manifest resources[{index}] is not an object")
            continue
        relative = _safe_relative_file(raw_row.get("path"))
        digest = raw_row.get("sha256")
        if (
            relative is None
            or PurePosixPath(relative).suffix == ".py"
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
        ):
            audit.issues.append(f"vendor manifest resources[{index}] has an invalid path or sha256")
            continue
        upstream_path = _safe_relative_file(raw_row.get("upstream_path"))
        if upstream_path is None:
            audit.issues.append(f"vendor manifest resources[{index}] has an invalid upstream_path")
        elif upstream_path in upstream_paths:
            audit.issues.append(f"vendor manifest repeats upstream identity: {upstream_path}")
        else:
            upstream_paths.add(upstream_path)
        if relative in resource_hashes:
            audit.issues.append(f"vendor manifest repeats resource identity: {relative}")
            continue
        if relative in all_manifest_paths:
            audit.issues.append(f"vendor manifest repeats file identity: {relative}")
            continue
        all_manifest_paths.add(relative)
        resource_hashes[relative] = digest
    audit.manifest_resource_files = len(resource_hashes)

    declared_counts = manifest.get("counts")
    if not isinstance(declared_counts, dict) or any(
        type(declared_counts.get(key)) is not int or declared_counts[key] != count
        for key, count in (
            ("py_files", len(manifest_rows)),
            ("resource_files", len(resource_rows)),
            ("total_files", len(manifest_rows) + len(resource_rows)),
        )
    ):
        audit.issues.append("vendor manifest declared file counts do not match its inventories")

    symlinks = sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_symlink()
    )
    for relative in symlinks:
        audit.issues.append(f"vendor tree contains a symlink: {relative}")

    actual_paths = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path.is_file() and not path.is_symlink()
    )
    audit.actual_python_files = len(actual_paths)
    actual_set = set(actual_paths)
    manifest_set = set(manifest_hashes)
    missing = sorted(manifest_set - actual_set)
    extra = sorted(actual_set - manifest_set)
    if missing or extra:
        audit.issues.append(
            f"manifest/disk Python identities differ: missing={missing[:5] or '-'}, "
            f"extra={extra[:5] or '-'}"
        )
    if expected_python_files is not None and audit.actual_python_files != expected_python_files:
        audit.issues.append(
            f"vendor tree has {audit.actual_python_files} Python files; "
            f"expected {expected_python_files}"
        )
    if len(manifest_rows) != len(manifest_hashes):
        audit.issues.append(
            "vendor manifest Python identity count includes invalid or duplicate rows"
        )

    actual_resource_paths = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.suffix != ".py"
        and path.relative_to(root).as_posix() not in UNMANIFESTED_METADATA_FILES
        and not ("__pycache__" in path.relative_to(root).parts and path.suffix == ".pyc")
    )
    audit.actual_resource_files = len(actual_resource_paths)
    actual_resource_set = set(actual_resource_paths)
    manifest_resource_set = set(resource_hashes)
    missing_resources = sorted(manifest_resource_set - actual_resource_set)
    extra_resources = sorted(actual_resource_set - manifest_resource_set)
    if missing_resources or extra_resources:
        audit.issues.append(
            f"manifest/disk resource identities differ: missing={missing_resources[:5] or '-'}, "
            f"extra={extra_resources[:5] or '-'}"
        )
    if (
        expected_resource_files is not None
        and audit.actual_resource_files != expected_resource_files
    ):
        audit.issues.append(
            f"vendor tree has {audit.actual_resource_files} resources; "
            f"expected {expected_resource_files}"
        )
    if len(resource_rows) != len(resource_hashes):
        audit.issues.append(
            "vendor manifest resource identity count includes invalid or duplicate rows"
        )

    source_hasher = hashlib.sha256()
    all_readable = True
    manifest_paths_for_facade = set(manifest_hashes)
    for relative in actual_paths:
        source_path = root / relative
        try:
            content = source_path.read_bytes()
        except OSError as exc:
            all_readable = False
            audit.issues.append(f"vendor source cannot be read: {relative} ({type(exc).__name__})")
            continue
        source_hasher.update(relative.encode("utf-8"))
        source_hasher.update(b"\0")
        source_hasher.update(content)
        source_hasher.update(b"\0")
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(content).readline)
            source = content.decode(encoding)
            tree = ast.parse(source, filename=relative, type_comments=True)
        except (SyntaxError, UnicodeError, LookupError) as exc:
            audit.issues.append(
                f"vendor source cannot be parsed: {relative} ({type(exc).__name__})"
            )
            continue
        audit.parsed_python_files += 1
        module, package, _ = _source_module(relative)
        _inspect_imports(
            tree,
            path=relative,
            package=package,
            audit=audit,
            manifest_paths=manifest_set,
        )

    resource_hasher = hashlib.sha256()
    all_resources_readable = True
    for relative in actual_resource_paths:
        resource_path = root / relative
        try:
            content = resource_path.read_bytes()
        except OSError as exc:
            all_resources_readable = False
            audit.issues.append(
                f"vendor resource cannot be read: {relative} ({type(exc).__name__})"
            )
            continue
        resource_hasher.update(relative.encode("utf-8"))
        resource_hasher.update(b"\0")
        resource_hasher.update(content)
        resource_hasher.update(b"\0")

    _inspect_facade(root, audit, manifest_paths_for_facade)
    upstream = manifest.get("upstream")
    upstream_commit = upstream.get("commit") if isinstance(upstream, dict) else None
    upstream_hasher = hashlib.sha256()
    for relative, digest in sorted(manifest_hashes.items()):
        upstream_hasher.update(relative.encode("utf-8"))
        upstream_hasher.update(b"\0")
        upstream_hasher.update(digest.encode("ascii"))
        upstream_hasher.update(b"\0")
    resource_upstream_hasher = hashlib.sha256()
    for relative, digest in sorted(resource_hashes.items()):
        resource_upstream_hasher.update(relative.encode("utf-8"))
        resource_upstream_hasher.update(b"\0")
        resource_upstream_hasher.update(digest.encode("ascii"))
        resource_upstream_hasher.update(b"\0")
    audit.source_pins = {
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "upstream_commit": upstream_commit if isinstance(upstream_commit, str) else None,
        "manifest_file_identity_sha256": upstream_hasher.hexdigest(),
        "manifest_resource_identity_sha256": resource_upstream_hasher.hexdigest(),
        "source_tree_sha256": source_hasher.hexdigest() if all_readable else None,
        "resource_tree_sha256": (resource_hasher.hexdigest() if all_resources_readable else None),
        "manifest_python_files": audit.manifest_python_files,
        "actual_python_files": audit.actual_python_files,
        "manifest_resource_files": audit.manifest_resource_files,
        "actual_resource_files": audit.actual_resource_files,
    }
    return audit


def copy_vendor_subtree(vendor_root: Path, isolation_root: Path) -> Path:
    """Copy only the vendor subtree beneath PEP 420 namespace ancestors."""
    destination = isolation_root.joinpath(*VENDOR_PREFIX.split("."))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        vendor_root,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return destination


def validate_isolation_namespace_tree(isolation_root: Path) -> list[str]:
    """Reject BSL package files or Python sources outside the copied vendor tree."""
    isolation_root = isolation_root.resolve()
    vendor_root = isolation_root.joinpath(*VENDOR_PREFIX.split("."))
    issues: list[str] = []
    for name in NAMESPACE_ANCESTORS:
        ancestor = isolation_root.joinpath(*name.split("."))
        if ancestor.is_symlink() or not ancestor.is_dir():
            issues.append("isolated namespace ancestor is missing or linked: " + name)
            continue
        init_path = ancestor / "__init__.py"
        if init_path.exists() or init_path.is_symlink():
            issues.append("isolated namespace ancestor contains BSL __init__.py: " + name)
        for child in ancestor.rglob("*"):
            if child.is_symlink():
                issues.append(
                    "isolated namespace ancestor contains a symlink: "
                    + child.relative_to(isolation_root).as_posix()
                )
            elif child.is_file() and child.suffix == ".py" and not _is_under(child, vendor_root):
                issues.append(
                    "isolated namespace ancestor contains non-vendor Python: "
                    + child.relative_to(isolation_root).as_posix()
                )
    return issues


_ISOLATION_CHILD = r"""
import importlib
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

payload = json.loads(sys.argv[1])
isolated_root = Path(payload["isolation_root"]).resolve()
vendor_root = isolated_root.joinpath(*payload["vendor_prefix"].split("."))
repository_root = Path(payload["repository_root"]).resolve()
vendor_prefix = payload["vendor_prefix"]
namespace_names = tuple(payload["namespace_ancestors"])
namespace_paths = {
    name: isolated_root.joinpath(*name.split(".")) for name in namespace_names
}
legacy_roots = tuple(payload["legacy_roots"])
blocked_imports = []
network_attempts = []
issues = []

baseline_sys_path = list(sys.path)
sys.path.insert(0, str(isolated_root))

def _within(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except (OSError, ValueError, TypeError):
        return False

class _BoundaryFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in namespace_paths:
            expected = namespace_paths[fullname]
            if not expected.is_dir():
                blocked_imports.append(fullname)
                raise ImportError("isolated namespace directory is missing")
            spec = importlib.machinery.ModuleSpec(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = [str(expected)]
            return spec
        if fullname == vendor_prefix or fullname.startswith(vendor_prefix + "."):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
            if spec is None:
                raise ImportError("vendor module is absent from the copied subtree")
            origin = spec.origin
            if origin is not None and not _within(origin, vendor_root):
                blocked_imports.append(fullname)
                raise ImportError("vendor module origin escaped the copied subtree")
            locations = spec.submodule_search_locations
            if locations is not None and any(
                not _within(location, vendor_root) for location in locations
            ):
                blocked_imports.append(fullname)
                raise ImportError("vendor package search path escaped the copied subtree")
            return spec
        if fullname == "opendata" or fullname.startswith("opendata."):
            blocked_imports.append(fullname)
            raise ImportError("BSL opendata module blocked by vendor isolation")
        root_name = fullname.split(".", 1)[0]
        if any(root_name == root or root_name.startswith(root + "_") for root in legacy_roots):
            blocked_imports.append(fullname)
            raise ImportError("legacy source root blocked by vendor isolation")
        if root_name == "openbb" or root_name.startswith("openbb_"):
            blocked_imports.append(fullname)
            raise ImportError("legacy source root blocked by vendor isolation")
        return None

sys.meta_path.insert(0, _BoundaryFinder())

def _block_network(operation):
    def denied(*args, **kwargs):
        network_attempts.append(operation)
        raise RuntimeError("network disabled in vendor isolation process")
    return denied

for _socket_method in ("connect", "connect_ex", "send", "sendall", "sendto", "sendmsg"):
    setattr(socket.socket, _socket_method, _block_network("socket." + _socket_method))
socket.create_connection = _block_network("socket.create_connection")
socket.getaddrinfo = _block_network("socket.getaddrinfo")
subprocess.run = _block_network("subprocess.run")
subprocess.Popen = _block_network("subprocess.Popen")
subprocess.call = _block_network("subprocess.call")
subprocess.check_call = _block_network("subprocess.check_call")
subprocess.check_output = _block_network("subprocess.check_output")
os.system = _block_network("os.system")
os.popen = _block_network("os.popen")

imports = {}
for module_name in payload["entry_imports"]:
    try:
        module = importlib.import_module(module_name)
        imports[module_name] = {
            "status": "imported",
            "file": getattr(module, "__file__", None),
            "origin": getattr(getattr(module, "__spec__", None), "origin", None),
        }
    except Exception as exc:
        imports[module_name] = {"status": "failed", "error_type": type(exc).__name__}

controls = {}
for label, module_name in payload["controls"].items():
    blocked_before = len(blocked_imports)
    try:
        importlib.import_module(module_name)
    except ImportError as exc:
        new_blocked = blocked_imports[blocked_before:]
        controls[label] = {
            "status": "blocked_import_error" if new_blocked else "unexpected_import_error",
            "error_type": type(exc).__name__,
            "blocked_module": new_blocked[-1] if new_blocked else None,
        }
    except Exception as exc:
        controls[label] = {"status": "unexpected_error", "error_type": type(exc).__name__}
    else:
        controls[label] = {"status": "imported", "error_type": None, "blocked_module": None}

ancestors = {}
for name in namespace_names:
    module = sys.modules.get(name)
    spec = getattr(module, "__spec__", None)
    file_name = getattr(module, "__file__", None)
    locations = list(getattr(spec, "submodule_search_locations", None) or [])
    ancestors[name] = {
        "file": file_name,
        "origin": getattr(spec, "origin", None),
        "search_locations": locations,
        "expected_location": str(namespace_paths[name]),
    }
    if module is None or spec is None or file_name is not None or spec.origin is not None:
        issues.append("namespace ancestor is not a source-free namespace package: " + name)
    if locations != [str(namespace_paths[name])]:
        issues.append("namespace ancestor search path differs from isolated root: " + name)

loaded_opendata = {}
for name, module in sorted(sys.modules.items()):
    if name != "opendata" and not name.startswith("opendata."):
        continue
    spec = getattr(module, "__spec__", None)
    file_name = getattr(module, "__file__", None)
    origin = getattr(spec, "origin", None)
    locations = list(getattr(spec, "submodule_search_locations", None) or [])
    loaded_opendata[name] = {"file": file_name, "origin": origin, "search_locations": locations}
    if name in namespace_names:
        if file_name is not None or origin is not None:
            issues.append("loaded namespace ancestor has a file origin: " + name)
        if locations != [str(namespace_paths[name])]:
            issues.append("loaded namespace ancestor path escaped temporary root: " + name)
    elif name == vendor_prefix or name.startswith(vendor_prefix + "."):
        if file_name is None or not _within(file_name, vendor_root):
            issues.append("loaded vendor file origin escaped copied subtree: " + name)
        if origin is None or not _within(origin, vendor_root):
            issues.append("loaded vendor spec origin escaped copied subtree: " + name)
        if any(not _within(location, vendor_root) for location in locations):
            issues.append("loaded vendor package search path escaped copied subtree: " + name)
    else:
        issues.append("unexpected opendata module loaded: " + name)

cwd = str(Path.cwd().resolve())
repo_path_leaks = []
for entry in sys.path:
    if not entry:
        repo_path_leaks.append(entry)
        continue
    if _within(entry, repository_root):
        repo_path_leaks.append(str(Path(entry).resolve()))
if cwd != str(isolated_root):
    issues.append("isolated process cwd differs from temporary root")
if repo_path_leaks:
    issues.append("repository checkout appears on isolated sys.path")
if network_attempts:
    issues.append("outbound network/process attempt was blocked during imports")
for module_name, result in imports.items():
    if result.get("status") != "imported":
        issues.append("required canonical vendor import failed: " + module_name)
for label, result in controls.items():
    if result.get("status") != "blocked_import_error":
        issues.append("negative control did not fail through the import boundary: " + label)

result = {
    "valid": not issues,
    "status": "passed" if not issues else "failed",
    "imports": imports,
    "controls": controls,
    "ancestors": ancestors,
    "loaded_opendata": loaded_opendata,
    "sys_path": [str(Path(item).resolve()) if item else item for item in sys.path],
    "baseline_sys_path": [
        str(Path(item).resolve()) if item else item for item in baseline_sys_path
    ],
    "cwd": cwd,
    "repository_path_leaks": repo_path_leaks,
    "blocked_imports": blocked_imports,
    "network_attempts": network_attempts,
    "issues": issues,
}
print(json.dumps(result, sort_keys=True))
"""


def _temporary_base(repository_root: Path) -> Path:
    candidate = Path(tempfile.gettempdir()).resolve()
    if _is_under(candidate, repository_root.resolve()):
        candidate = Path.home().resolve()
    if _is_under(candidate, repository_root.resolve()):
        raise OSError("no temporary directory is available outside the repository")
    return candidate


def validate_isolation_report(report: object, repository_root: Path) -> list[str]:
    """Fail closed unless import origins, namespace paths, and controls are proven."""
    if not isinstance(report, dict):
        return ["isolation child result is not a JSON object"]
    issues = list(report.get("issues", [])) if isinstance(report.get("issues"), list) else []
    if report.get("valid") is not True:
        issues.append("isolation child did not report a valid run")
    imports = report.get("imports")
    if not isinstance(imports, dict) or set(imports) != set(ENTRY_IMPORTS):
        issues.append("isolation entry import inventory is incomplete")
    elif any(
        not isinstance(item, dict) or item.get("status") != "imported" for item in imports.values()
    ):
        issues.append("one or more isolated entry imports failed")
    controls = report.get("controls")
    if not isinstance(controls, dict) or set(controls) != set(CONTROL_IMPORTS):
        issues.append("isolation negative-control inventory is incomplete")
    elif any(
        not isinstance(item, dict) or item.get("status") != "blocked_import_error"
        for item in controls.values()
    ):
        issues.append("a negative control did not fail through the import boundary")
    if report.get("network_attempts") != []:
        issues.append("isolation run attempted an outbound operation")
    if report.get("repository_path_leaks") != []:
        issues.append("repository path leaked into isolated sys.path")
    cwd_value = report.get("cwd")
    if not isinstance(cwd_value, str):
        issues.append("isolation child did not report its working directory")
        isolation_root = None
    else:
        isolation_root = Path(cwd_value).resolve()
        if _is_under(isolation_root, repository_root.resolve()):
            issues.append("isolation child ran inside the repository checkout")
    sys_path = report.get("sys_path")
    baseline_sys_path = report.get("baseline_sys_path")
    if (
        isolation_root is None
        or not isinstance(sys_path, list)
        or not isinstance(baseline_sys_path, list)
        or sys_path != [str(isolation_root), *baseline_sys_path]
        or any(not isinstance(entry, str) or not entry for entry in sys_path)
        or any(not isinstance(entry, str) or not entry for entry in baseline_sys_path)
    ):
        issues.append("temporary root is not the only sys.path insertion")
    elif any(
        isinstance(entry, str) and entry and _is_under(Path(entry), repository_root.resolve())
        for entry in [*sys_path, *baseline_sys_path]
    ):
        issues.append("repository checkout appears in reported sys.path")

    ancestors = report.get("ancestors")
    if not isinstance(ancestors, dict) or set(ancestors) != set(NAMESPACE_ANCESTORS):
        issues.append("namespace ancestor origin inventory is incomplete")
    else:
        for name, item in ancestors.items():
            if not isinstance(item, dict):
                issues.append("namespace ancestor evidence is malformed: " + name)
                continue
            if item.get("file") is not None or item.get("origin") is not None:
                issues.append("namespace ancestor has a regular-package origin: " + name)
            expected_location = (
                isolation_root.joinpath(*name.split(".")) if isolation_root is not None else None
            )
            if (
                expected_location is None
                or item.get("expected_location") != str(expected_location)
                or item.get("search_locations") != [str(expected_location)]
            ):
                issues.append("namespace ancestor search path is not isolated: " + name)

    loaded = report.get("loaded_opendata")
    if not isinstance(loaded, dict):
        issues.append("loaded opendata origin inventory is missing")
    else:
        for name, item in loaded.items():
            if not isinstance(item, dict):
                issues.append("loaded opendata origin evidence is malformed: " + name)
                continue
            if name in NAMESPACE_ANCESTORS:
                if item.get("file") is not None or item.get("origin") is not None:
                    issues.append("loaded namespace ancestor came from a regular package: " + name)
                expected_location = (
                    isolation_root.joinpath(*name.split("."))
                    if isolation_root is not None
                    else None
                )
                if item.get("search_locations") != [str(expected_location)]:
                    issues.append("loaded namespace ancestor search path escaped copy: " + name)
            elif name == VENDOR_PREFIX or name.startswith(f"{VENDOR_PREFIX}."):
                expected_vendor_root = (
                    isolation_root.joinpath(*VENDOR_PREFIX.split("."))
                    if isolation_root is not None
                    else None
                )
                for key in ("file", "origin"):
                    value = item.get(key)
                    if (
                        expected_vendor_root is None
                        or not isinstance(value, str)
                        or not _is_under(Path(value), expected_vendor_root)
                    ):
                        issues.append("loaded vendor module has no copied-tree origin: " + name)
                        break
                locations = item.get("search_locations")
                if not isinstance(locations, list) or any(
                    not isinstance(location, str)
                    or expected_vendor_root is None
                    or not _is_under(Path(location), expected_vendor_root)
                    for location in locations
                ):
                    issues.append("loaded vendor package search path escaped copy: " + name)
            else:
                issues.append("loaded BSL opendata module outside vendor: " + name)

    expected_loaded = set(NAMESPACE_ANCESTORS) | set(ENTRY_IMPORTS)
    if not isinstance(loaded, dict) or not expected_loaded <= set(loaded):
        issues.append("isolated import origin inventory omits an ancestor or entry module")
    if isinstance(imports, dict) and isinstance(loaded, dict) and isolation_root is not None:
        expected_vendor_root = isolation_root.joinpath(*VENDOR_PREFIX.split("."))
        for name, item in imports.items():
            if name not in ENTRY_IMPORTS or not isinstance(item, dict):
                continue
            for key in ("file", "origin"):
                value = item.get(key)
                if not isinstance(value, str) or not _is_under(Path(value), expected_vendor_root):
                    issues.append("isolated entry import has no copied-tree origin: " + name)
                    break
            loaded_item = loaded.get(name)
            if not isinstance(loaded_item, dict) or any(
                loaded_item.get(key) != item.get(key) for key in ("file", "origin")
            ):
                issues.append("entry import origin differs from loaded-module evidence: " + name)

    expected_control_modules = {
        "core_database": "opendata.core",
        "sibling_provider": "opendata.data.providers.bls",
        "client_root": "opendata_client",
    }
    if isinstance(controls, dict):
        for label, expected_module in expected_control_modules.items():
            item = controls.get(label)
            if (
                not isinstance(item, dict)
                or item.get("error_type") != "ImportError"
                or item.get("blocked_module") != expected_module
            ):
                issues.append("negative control was not blocked at its expected module: " + label)
    return issues


def run_isolated_import_check(vendor_root: Path, repository_root: Path) -> IsolationAudit:
    """Copy vendor sources and verify imports in a network-disabled ``-I -S`` child."""
    repository_root = repository_root.resolve()
    try:
        temp_base = _temporary_base(repository_root)
        with tempfile.TemporaryDirectory(prefix="vendor-independence-", dir=temp_base) as temp_name:
            isolation_root = Path(temp_name).resolve()
            copy_vendor_subtree(vendor_root, isolation_root)
            namespace_issues = validate_isolation_namespace_tree(isolation_root)
            if namespace_issues:
                return IsolationAudit(
                    valid=False,
                    status="invalid_isolation_namespace_tree",
                    cwd=str(isolation_root),
                    issues=namespace_issues,
                )
            payload = {
                "isolation_root": str(isolation_root),
                "repository_root": str(repository_root),
                "vendor_prefix": VENDOR_PREFIX,
                "namespace_ancestors": NAMESPACE_ANCESTORS,
                "legacy_roots": LEGACY_ROOTS,
                "entry_imports": ENTRY_IMPORTS,
                "controls": CONTROL_IMPORTS,
            }
            # Fixed interpreter, child source, and JSON argv; shell execution is disabled.
            completed = subprocess.run(  # noqa: S603  # nosec B603  # sys.executable, fixed child
                [sys.executable, "-I", "-S", "-c", _ISOLATION_CHILD, json.dumps(payload)],
                cwd=isolation_root,
                env={"PATH": os.defpath},
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
                shell=False,
            )
            try:
                report = json.loads(completed.stdout.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                return IsolationAudit(
                    valid=False,
                    status="invalid_child_output",
                    issues=[
                        "isolated child did not return JSON",
                        f"child_exit={completed.returncode}",
                        f"child_stderr={completed.stderr[-1000:]}",
                    ],
                )
            issues = validate_isolation_report(report, repository_root)
            if completed.returncode != 0:
                issues.append(f"isolated child exited with status {completed.returncode}")
            return IsolationAudit(
                valid=not issues,
                status="passed" if not issues else "failed",
                imports=report.get("imports", {}),
                controls=report.get("controls", {}),
                ancestors=report.get("ancestors", {}),
                loaded_opendata=report.get("loaded_opendata", {}),
                sys_path=report.get("sys_path", []),
                baseline_sys_path=report.get("baseline_sys_path", []),
                cwd=report.get("cwd", ""),
                repository_path_leaks=report.get("repository_path_leaks", []),
                blocked_imports=report.get("blocked_imports", []),
                network_attempts=report.get("network_attempts", []),
                issues=issues,
            )
    except (OSError, shutil.Error, subprocess.SubprocessError, TimeoutError) as exc:
        return IsolationAudit(
            valid=False,
            status="failed_to_run",
            issues=[f"isolated import check failed ({type(exc).__name__})"],
        )


def audit_vendor_boundary(repository_root: Path) -> dict[str, object]:
    """Run the fixed 325-file source audit and isolated import proof for one checkout."""
    repository_root = repository_root.resolve()
    vendor_root = repository_root / VENDOR_REL
    tree = audit_vendor_sources(
        vendor_root,
        expected_python_files=EXPECTED_PYTHON_FILES,
        expected_resource_files=EXPECTED_RESOURCE_FILES,
    )
    isolation = (
        run_isolated_import_check(vendor_root, repository_root)
        if tree.valid
        else IsolationAudit(
            valid=False,
            status="not_run_invalid_source",
            issues=["isolated imports skipped because source audit failed"],
        )
    )
    return {
        "schema_version": 1,
        "valid": tree.valid and isolation.valid,
        "vendor_root": VENDOR_REL.as_posix(),
        "counts": {
            "expected_python_files": tree.expected_python_files,
            "actual_python_files": tree.actual_python_files,
            "manifest_python_files": tree.manifest_python_files,
            "expected_resource_files": EXPECTED_RESOURCE_FILES,
            "actual_resource_files": tree.actual_resource_files,
            "manifest_resource_files": tree.manifest_resource_files,
            "actual_total_files": tree.actual_python_files + tree.actual_resource_files,
            "parsed_python_files": tree.parsed_python_files,
            "vendor_imports": tree.vendor_imports,
            "namespace_imports": tree.namespace_imports,
            "third_party_imports": tree.third_party_imports,
            "facade_module_paths": tree.facade_module_paths,
            "facade_vendor_module_paths": tree.facade_vendor_module_paths,
            "facade_third_party_module_paths": tree.facade_third_party_module_paths,
            "ast_crossings": len(tree.ast_crossings),
            "loaded_opendata_modules": len(isolation.loaded_opendata),
        },
        "ast_crossings": [asdict(crossing) for crossing in tree.ast_crossings],
        "facade_third_party_targets": sorted(set(tree.facade_third_party_targets)),
        "source_pins": tree.source_pins,
        "isolation": asdict(isolation),
        "issues": [*tree.issues, *isolation.issues],
    }


def main(argv: list[str] | None = None) -> int:
    """Emit the vendor boundary audit as JSON and return a machine-readable exit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repository root (defaults to this checkout)",
    )
    args = parser.parse_args(argv)
    result = audit_vendor_boundary(args.root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["valid"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
