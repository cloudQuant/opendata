"""Shared source-tree classification and unique file iteration.

The first-party AkShare adapter lives beside a nested copy of upstream AkShare.
All quality scanners must classify the exact ``_vendor`` prefix before walking
``opendata/`` so first-party adapter code remains visible and ported code keeps
its separate audit surface.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]

FIRST_PARTY = "first_party"
PORTED = "ported"
VENDOR_ROOT = "opendata/data/providers/akshare/_vendor"

_FIRST_PARTY_ROOTS = frozenset(
    {"opendata", "opendata_client", "scripts", "tests", "alembic", "alembic_data"}
)
_VENDOR_PREFIXES = (VENDOR_ROOT, "opendata_http")
_FUYAO_PATHS = {
    "__init__.py": "opendata/data/providers/ths/transport/__init__.py",
    "credentials.py": "opendata/data/providers/ths/transport/credentials.py",
    "dumps.py": "opendata/data/providers/ths/dumps.py",
    "endpoint_map.py": "opendata/data/providers/ths/endpoint_map.py",
    "endpoint_map.yaml": "opendata/data/providers/ths/endpoint_map.yaml",
    "endpoints.py": "opendata/data/providers/ths/endpoints.py",
    "envelope.py": "opendata/data/providers/ths/transport/envelope.py",
    "error_messages.yaml": "opendata/data/providers/ths/transport/error_messages.yaml",
    "errors.py": "opendata/data/providers/ths/transport/errors.py",
    "http_client.py": "opendata/data/providers/ths/transport/http_client.py",
    "rate_limiter.py": "opendata/data/providers/ths/transport/rate_limiter.py",
}

PathClassifier = Callable[[str], str | None]


class SourceLayoutError(RuntimeError):
    """Raised when a declared source root cannot be fully inspected."""


@dataclass(frozen=True)
class SourceFile:
    """One source file with its canonical repository identity and audit layer.

    Attributes:
        path: Resolved path used to read the file.
        identity: Canonical repository-relative POSIX path.
        layer: The source layer returned by the configured classifier.
    """

    path: Path
    identity: str
    layer: str


def _normalized_relative_path(value: str | PurePath) -> str | None:
    """Normalize a safe repository-relative path, rejecting ambiguous forms."""
    raw = value.as_posix() if isinstance(value, PurePath) else value
    if not raw or raw.startswith("/") or "\\" in raw or "\x00" in raw:
        return None
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return PurePosixPath(raw).as_posix()


def _has_prefix(path: str, prefix: str) -> bool:
    """Return whether *path* is the exact prefix or a descendant of it."""
    return path == prefix or path.startswith(f"{prefix}/")


def classify_path(relative_path: str | PurePath) -> str | None:
    """Classify one repository-relative path as first-party, ported, or unknown.

    ``_vendor`` is a component-bounded prefix: similarly named adapter paths
    remain first-party. The old root names remain recognized for translating
    historical scan identities only.
    """
    normalized = _normalized_relative_path(relative_path)
    if normalized is None:
        return None
    if any(_has_prefix(normalized, prefix) for prefix in _VENDOR_PREFIXES):
        return PORTED
    first = normalized.split("/", 1)[0]
    if first in _FIRST_PARTY_ROOTS or first == "opendata_fuyao":
        return FIRST_PARTY
    return None


def historical_identity(relative_path: str | PurePath) -> str:
    """Map a historical source path to its current canonical path identity.

    Ported files retain their relative path below the nested vendor package.
    Fuyao transport files were merged into the THS provider; its flat historical
    filenames therefore use an explicit map rather than an ambiguous prefix.
    """
    normalized = _normalized_relative_path(relative_path)
    if normalized is None:
        raise SourceLayoutError(f"invalid historical source path: {relative_path!s}")
    if normalized == "opendata_http":
        return VENDOR_ROOT
    if _has_prefix(normalized, "opendata_http"):
        suffix = normalized.removeprefix("opendata_http/")
        return f"{VENDOR_ROOT}/{suffix}"
    if normalized == "opendata_fuyao":
        raise SourceLayoutError("the historical Fuyao directory has no single file identity")
    if _has_prefix(normalized, "opendata_fuyao"):
        suffix = normalized.removeprefix("opendata_fuyao/")
        mapped = _FUYAO_PATHS.get(suffix)
        if mapped is None:
            raise SourceLayoutError(f"no historical identity mapping for {normalized}")
        return mapped
    return normalized


def iter_unique_files(
    root: Path,
    roots: Iterable[str],
    *,
    suffixes: tuple[str, ...] = (".py",),
    layers: frozenset[str] | None = None,
    classifier: PathClassifier = classify_path,
) -> list[SourceFile]:
    """Walk declared roots once and return files keyed by canonical identity.

    Each declared root must exist and contain at least one matching file after
    classification. Nested or repeated roots are deduplicated by resolved path,
    while files outside the allowed layers are omitted.
    """
    repo_root = root.resolve()
    if not repo_root.is_dir():
        raise SourceLayoutError(f"source root is not a directory: {root}")
    declared_roots = tuple(roots)
    if not declared_roots:
        raise SourceLayoutError("no source roots were declared")

    selected: dict[str, SourceFile] = {}
    for relative_root in declared_roots:
        normalized_root = _normalized_relative_path(relative_root)
        if normalized_root is None:
            raise SourceLayoutError(f"invalid source root: {relative_root!r}")
        base = repo_root / PurePosixPath(normalized_root)
        if not base.is_dir():
            raise SourceLayoutError(f"{normalized_root}: declared source root is not a directory")

        root_matches = 0
        for candidate in sorted(base.rglob("*")):
            if not candidate.is_file() or not candidate.name.endswith(suffixes):
                continue
            if "__pycache__" in candidate.parts:
                continue
            try:
                resolved = candidate.resolve(strict=True)
                identity = resolved.relative_to(repo_root).as_posix()
            except (OSError, ValueError) as exc:
                raise SourceLayoutError(
                    f"{normalized_root}: source file resolves outside the repository"
                ) from exc
            layer = classifier(identity)
            if layer is None or (layers is not None and layer not in layers):
                continue
            root_matches += 1
            selected.setdefault(identity, SourceFile(resolved, identity, layer))
        if root_matches == 0:
            raise SourceLayoutError(f"{normalized_root}: declared source root has no matching file")

    return [selected[key] for key in sorted(selected)]


def iter_unique_python_files(
    root: Path,
    roots: Iterable[str],
    *,
    layers: frozenset[str] | None = None,
    classifier: PathClassifier = classify_path,
) -> list[SourceFile]:
    """Return unique Python source files across the declared roots."""
    return iter_unique_files(root, roots, layers=layers, classifier=classifier)
