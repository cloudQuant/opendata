"""Port upstream akshare submodules into ``opendata_http`` (design §5.2/§5.6).

The porting source is the *upstream repository tree* pinned by
``opendata_http/upstream.lock`` - never the in-repo legacy copy, which
diverged from upstream long before this project (208 files differ, so
only the locked baseline keeps diff-sync meaningful).

Per file the codemod applies, in order:

1. the §5.5 porting header (below the shebang/coding lines);
2. import prefix rewrites (AST-located, all four ``akshare`` forms);
3. string-constant rewrites for pure module paths (``"akshare.x.y"``);
4. registered manual edits - the upstream credential disposal from
   THIRD_PARTY_NOTICES.md, replayed as structure-matching transforms
   that never contain the credential literals themselves.

Resource files (.js/.json/.dat) are copied verbatim. The run is
idempotent: a file whose expected ported content already matches the
target is skipped, and the lock is updated in place for every file.

CLI::

    python scripts/codemod/port_module.py <submodule> [<submodule> ...] \
        [--upstream-repo DIR] [--init URL] [--dry-run]

``--init`` creates ``upstream.lock`` from the upstream repository's
current HEAD before porting (fail-closed on a dirty tree).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess  # nosec B404  # literal argv, shell disabled
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_PACKAGE = "akshare"
PORTED_PACKAGE = "opendata_http"
PORTED_ROOT = REPO_ROOT / PORTED_PACKAGE
LOCK_PATH = PORTED_ROOT / "upstream.lock"
DEFAULT_UPSTREAM_REPO = REPO_ROOT.parent / "akshare"
RESOURCE_SUFFIXES = frozenset({".js", ".json", ".dat"})
_OS_IMPORT = "import os\n"
_IMPORT_LINE_RE = re.compile(r"^\s*(?:import|from)\s", re.MULTILINE)
_MODULE_PATH_STRING_RE = re.compile(r"(['\"])akshare((?:\.[A-Za-z_][A-Za-z0-9_]*)+)\1")


@dataclass(frozen=True)
class ManualEdit:
    """One registered manual divergence from upstream (§5.2 rule 9).

    Transforms are structure-matching regexes: they must never contain
    the credential literals themselves, so the codemod source stays
    secret-free.
    """

    upstream_path: str
    description: str
    transforms: tuple[tuple[str, str], ...] = ()


#: Registered manual edits (mirrors THIRD_PARTY_NOTICES.md 人工改动登记).
MANUAL_EDITS: tuple[ManualEdit, ...] = (
    ManualEdit(
        upstream_path="akshare/stock/cons.py",
        description="xueqiu token -> XQ_A_TOKEN env var",
        transforms=(
            (r'xq_a_token\s*=\s*"[^"]*"', 'xq_a_token = os.environ.get("XQ_A_TOKEN", "")'),
        ),
    ),
    ManualEdit(
        upstream_path="akshare/bond/bond_convert.py",
        description="jisilu credentials -> JSL_USER/JSL_PASSWORD env vars",
        transforms=(
            (r'user="[^"]*"', 'user=os.environ.get("JSL_USER", "")'),
            (r'password="[^"]*"', 'password=os.environ.get("JSL_PASSWORD", "")'),
        ),
    ),
    ManualEdit(
        upstream_path="akshare/bond/bond_china_money.py",
        description="chinamoney site key -> CHINAMONEY_API_KEY env var",
        transforms=((r'\{"key": "[^"]*"\}', '{"key": os.environ.get("CHINAMONEY_API_KEY", "")}'),),
    ),
    ManualEdit(
        upstream_path="akshare/futures/futures_hf_em.py",
        description="eastmoney site token -> EM_API_TOKEN env var",
        transforms=((r'"token": "[^"]*",', '"token": os.environ.get("EM_API_TOKEN", ""),'),),
    ),
    ManualEdit(
        upstream_path="akshare/option/option_em.py",
        description="eastmoney site token -> EM_API_TOKEN env var",
        transforms=((r'"token": "[^"]*",', '"token": os.environ.get("EM_API_TOKEN", ""),'),),
    ),
)


@dataclass
class PortResult:
    """Outcome of porting one file."""

    upstream_path: str
    ported_path: str
    status: str
    import_rewrites: int = 0
    string_rewrites: int = 0
    manual_edits: bool = False
    upstream_sha256: str = ""
    ported_sha256: str = ""
    todos: list[str] = field(default_factory=list)


@dataclass
class UpstreamLock:
    """The porting baseline lock (§5.5: path + sha256 + upstream_path).

    Attributes:
        url: Upstream repository URL.
        commit: Pinned upstream commit.
        files: Ported-relative path to per-file record
            (upstream_path, pristine upstream sha256, manual_edits).
    """

    url: str
    commit: str
    files: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, path: Path | None = None) -> UpstreamLock:
        """Load and validate the lock file.

        Raises:
            RuntimeError: When the lock is missing or malformed.
        """
        # Resolved at call time: a module-level default would bind early
        # and defeat monkeypatching in tests.
        if path is None:
            path = LOCK_PATH
        if not path.is_file():
            raise RuntimeError(
                f"{path} is missing; run with --init <upstream_url> first "
                "(quality spec §0: missing evidence is a failure)"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError(f"{path} is malformed (not a mapping)")
        upstream = data.get("upstream")
        files = data.get("files")
        if not isinstance(upstream, dict) or not isinstance(files, list):
            raise RuntimeError(f"{path} is malformed (upstream/files)")
        lock = cls(
            url=str(upstream.get("url", "")), commit=str(upstream.get("commit", "")), files={}
        )
        for entry in files:
            if not isinstance(entry, dict) or "path" not in entry:
                raise RuntimeError(f"{path} has a malformed file entry")
            lock.files[str(entry["path"])] = entry
        return lock

    def save(self, path: Path | None = None) -> None:
        """Persist the lock deterministically (sorted by path)."""
        if path is None:
            path = LOCK_PATH
        payload = {
            "version": 1,
            "upstream": {"url": self.url, "commit": self.commit},
            "files": sorted(self.files.values(), key=lambda entry: str(entry.get("path"))),
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def record(self, result: PortResult) -> None:
        """Upsert one ported file's baseline record."""
        rel = result.ported_path.removeprefix(PORTED_PACKAGE + "/")
        self.files[rel] = {
            "path": rel,
            "upstream_path": result.upstream_path,
            "sha256": result.upstream_sha256,
            "manual_edits": result.manual_edits,
        }


def sha256_text(text: str) -> str:
    """Hash text content with sha256 (hex)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Hash binary content with sha256 (hex)."""
    return hashlib.sha256(data).hexdigest()


def rewrite_imports(source: str) -> tuple[str, int]:
    """Rewrite ``akshare`` import prefixes to ``opendata_http``.

    Uses the AST to locate import statements, so mentions of akshare
    inside comments or docstrings are left untouched.

    Args:
        source: The upstream module source.

    Returns:
        The rewritten source and the number of import lines changed.
    """
    lines = source.splitlines(keepends=True)
    targets: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            touched = node.module == UPSTREAM_PACKAGE or (
                node.module is not None and node.module.startswith(UPSTREAM_PACKAGE + ".")
            )
        elif isinstance(node, ast.Import):
            touched = any(
                alias.name == UPSTREAM_PACKAGE or alias.name.startswith(UPSTREAM_PACKAGE + ".")
                for alias in node.names
            )
        else:
            continue
        if touched:
            targets.add(node.lineno)
    changed = 0
    for lineno in targets:
        index = lineno - 1
        new_line = re.sub(rf"\b{UPSTREAM_PACKAGE}\b", PORTED_PACKAGE, lines[index])
        if new_line != lines[index]:
            lines[index] = new_line
            changed += 1
    return "".join(lines), changed


def rewrite_strings(source: str) -> tuple[str, int]:
    """Rewrite pure module-path string constants of the old package.

    ``"akshare.utils.demjson"`` becomes ``"opendata_http.utils.demjson"``;
    a bare ``"akshare"`` label is not a module path and stays as-is
    (the zero-dep scanner applies the same distinction).

    Args:
        source: The module source after import rewrites.

    Returns:
        The rewritten source and the number of strings changed.
    """
    count = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}{PORTED_PACKAGE}{match.group(2)}{match.group(1)}"

    return _MODULE_PATH_STRING_RE.sub(_sub, source), count


def prepend_porting_header(source: str, upstream_url: str, commit: str) -> str:
    """Insert the §5.5 porting header below the shebang/coding lines.

    Idempotent: a source that already carries the header is returned
    unchanged.

    Args:
        source: The upstream module source.
        upstream_url: Upstream repository URL.
        commit: Pinned upstream commit.

    Returns:
        The source with the two header comment lines inserted.
    """
    header = (
        f"# Ported from akshare ({upstream_url}) @ {commit}\n"
        "# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)\n"
    )
    lines = source.splitlines(keepends=True)
    insert_at = 0
    if lines and lines[0].startswith("#!"):
        insert_at = 1
    if len(lines) > insert_at and lines[insert_at].startswith("#") and "coding" in lines[insert_at]:
        insert_at += 1
    if "".join(lines[insert_at : insert_at + 2]) == header:
        return source
    return "".join(lines[:insert_at]) + header + "".join(lines[insert_at:])


def _ensure_import_os(source: str) -> str:
    """Add ``import os`` before the first module-level import if missing."""
    if re.search(r"^import os\b", source, re.MULTILINE):
        return source
    match = _IMPORT_LINE_RE.search(source)
    if match is None:
        return _OS_IMPORT + source
    return source[: match.start()] + _OS_IMPORT + source[match.start() :]


def apply_manual_edits(source: str, upstream_path: str) -> tuple[str, list[str]]:
    """Replay the registered manual edits for one upstream file.

    Args:
        source: The module source after rewrites.
        upstream_path: Repo-relative upstream path (e.g.
            ``akshare/stock/cons.py``).

    Returns:
        The transformed source and a list of human TODOs (a transform
        whose pattern no longer matches is reported, never skipped
        silently).
    """
    todos: list[str] = []
    for edit in MANUAL_EDITS:
        if edit.upstream_path != upstream_path:
            continue
        for pattern, replacement in edit.transforms:
            new_source, count = re.subn(pattern, replacement, source)
            if count == 0:
                todos.append(
                    f"{upstream_path}: manual edit pattern no longer matches "
                    f"({edit.description}); upstream changed? review manually"
                )
                continue
            source = new_source
        source = _ensure_import_os(source)
    return source, todos


def port_source(
    source: str, upstream_path: str, upstream_url: str, commit: str
) -> tuple[str, PortResult]:
    """Apply the full transformation chain to one module's source.

    Args:
        source: The pristine upstream source text.
        upstream_path: Repo-relative upstream path.
        upstream_url: Upstream repository URL for the header.
        commit: Pinned upstream commit for the header.

    Returns:
        The transformed source and its port result.
    """
    result = PortResult(
        upstream_path=upstream_path,
        ported_path=f"{PORTED_PACKAGE}/{upstream_path.removeprefix(UPSTREAM_PACKAGE + '/')}",
        status="ported",
        upstream_sha256=sha256_text(source),
    )
    source = prepend_porting_header(source, upstream_url, commit)
    source, result.import_rewrites = rewrite_imports(source)
    source, result.string_rewrites = rewrite_strings(source)
    source, result.todos = apply_manual_edits(source, upstream_path)
    result.manual_edits = any(edit.upstream_path == upstream_path for edit in MANUAL_EDITS)
    result.ported_sha256 = sha256_text(source)
    return source, result


def _git(repo: Path, *args: str) -> str:
    """Run a literal git command in a repository and return stdout."""
    argv = ["git", "-C", str(repo), *args]
    completed = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
        argv,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def init_lock(upstream_repo: Path, upstream_url: str) -> UpstreamLock:
    """Create upstream.lock from the upstream repository's clean HEAD.

    Args:
        upstream_repo: Local clone of the upstream repository.
        upstream_url: Canonical upstream URL recorded in the lock.

    Returns:
        The new lock.

    Raises:
        RuntimeError: When the upstream tree is dirty or unreadable.
    """
    if _git(upstream_repo, "status", "--porcelain"):
        raise RuntimeError(f"upstream repo {upstream_repo} is dirty; refusing to lock")
    return UpstreamLock(url=upstream_url, commit=_git(upstream_repo, "rev-parse", "HEAD"), files={})


def verify_upstream(upstream_repo: Path, lock: UpstreamLock) -> None:
    """Fail closed unless the upstream repo is at the locked commit.

    Raises:
        RuntimeError: On a dirty tree or a commit mismatch.
    """
    if _git(upstream_repo, "status", "--porcelain"):
        raise RuntimeError(f"upstream repo {upstream_repo} is dirty; refusing to port")
    head = _git(upstream_repo, "rev-parse", "HEAD")
    if head != lock.commit:
        raise RuntimeError(
            f"upstream HEAD {head} does not match locked commit {lock.commit}; "
            "re-pin deliberately (design §5.6)"
        )


def port_submodule(
    submodule: str,
    *,
    upstream_repo: Path,
    lock: UpstreamLock,
    dry_run: bool = False,
) -> list[PortResult]:
    """Port one upstream submodule directory into the target package.

    Args:
        submodule: Submodule name under ``akshare/`` (e.g. ``utils``).
        upstream_repo: Clean local clone at the locked commit.
        lock: The baseline lock, updated in place for every file.
        dry_run: Compute results without writing files.

    Returns:
        One PortResult per file.

    Raises:
        ValueError: If the submodule does not exist upstream.
    """
    source_dir = upstream_repo / UPSTREAM_PACKAGE / submodule
    if not source_dir.is_dir():
        raise ValueError(f"unknown submodule {submodule!r}: {source_dir} does not exist")
    results: list[PortResult] = []
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        upstream_path = path.relative_to(upstream_repo).as_posix()
        # Relative to the upstream PACKAGE root, so the submodule path
        # (utils/..., file_fold/...) is preserved under the target.
        target = PORTED_ROOT / path.relative_to(upstream_repo / UPSTREAM_PACKAGE)
        result: PortResult
        if path.suffix in RESOURCE_SUFFIXES:
            data = path.read_bytes()
            result = PortResult(
                upstream_path=upstream_path,
                ported_path=target.relative_to(PORTED_ROOT.parent).as_posix(),
                status="ported",
                upstream_sha256=sha256_bytes(data),
                ported_sha256=sha256_bytes(data),
            )
            payload: bytes | str = data
            existing_bytes = target.read_bytes() if target.exists() else None
            if existing_bytes is not None and sha256_bytes(existing_bytes) == result.ported_sha256:
                result.status = "skipped-identical"
        else:
            text = path.read_text(encoding="utf-8")
            payload, result = port_source(text, upstream_path, lock.url, lock.commit)
            result.ported_path = target.relative_to(PORTED_ROOT.parent).as_posix()
            existing_text = target.read_text(encoding="utf-8") if target.exists() else None
            if existing_text is not None and sha256_text(existing_text) == result.ported_sha256:
                result.status = "skipped-identical"
        results.append(result)
        lock.record(result)
        if dry_run or result.status == "skipped-identical":
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, bytes):
            target.write_bytes(payload)
        else:
            target.write_text(payload, encoding="utf-8")
    return results


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; exit code 1 on any failure or pending TODO."""
    parser = argparse.ArgumentParser(description="Port upstream akshare submodules.")
    parser.add_argument("submodules", nargs="+", help="submodule names under akshare/")
    parser.add_argument("--upstream-repo", type=Path, default=DEFAULT_UPSTREAM_REPO)
    parser.add_argument("--init", metavar="UPSTREAM_URL", help="create upstream.lock first")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        lock = init_lock(args.upstream_repo, args.init) if args.init else UpstreamLock.load()
        if args.init:
            PORTED_ROOT.mkdir(parents=True, exist_ok=True)
            lock.save()
            print(f"initialized {LOCK_PATH} @ {lock.commit[:12]}")
        verify_upstream(args.upstream_repo, lock)
        all_results: list[PortResult] = []
        for submodule in args.submodules:
            all_results.extend(
                port_submodule(
                    submodule, upstream_repo=args.upstream_repo, lock=lock, dry_run=args.dry_run
                )
            )
    except (RuntimeError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if not args.dry_run:
        lock.save()

    for result in all_results:
        print(
            f"{result.status:<18} {result.ported_path} "
            f"(imports={result.import_rewrites} strings={result.string_rewrites} "
            f"manual_edits={result.manual_edits})"
        )
        for todo in result.todos:
            print(f"  TODO {todo}")
    pending = [todo for result in all_results for todo in result.todos]
    if pending:
        print(f"FAIL: {len(pending)} manual TODO(s)", file=sys.stderr)
        return 1
    mode = "dry-run" if args.dry_run else "written"
    print(f"OK: {len(all_results)} file(s) handled ({mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
