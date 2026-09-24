#!/usr/bin/env python3
"""Upstream sync drill (AC-12 / C2): diff the locked commit vs a new ref.

The ported tree is frozen to the commit recorded in
``opendata_http/upstream.lock`` (design §12). When upstream moves, the
sync flow is ``diff → 评估 → port_module 重跑 → 对照 → 提交``; this
tool is the first step - it answers "what changed upstream" without
touching the ported tree:

* **file inventory** - every file added, modified or deleted between
  the locked commit and the target ref;
* **function-signature diff** - for modified ``.py`` files, the
  top-level functions added, removed or re-signatured, so the评估 can
  see at a glance whether a change is a rename, a new endpoint or a
  breaking signature change;
* **lock verification** - the per-file sha256 of the lock is checked
  against the upstream tree at the locked commit, so the report either
  proves the lock still matches the frozen tree or says which files
  drifted (a lock that cannot be reproduced must not be trusted).

The target ref defaults to the upstream's HEAD; the acceptance drill
(§7.4 / AC-12) uses a historical commit to simulate an upstream move.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess  # nosec B404  # git is invoked with literal argv
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import ast

# The ported tree lives next to this script's package parent.
REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = REPO_ROOT / "opendata_http" / "upstream.lock"


@dataclass(frozen=True)
class FileChange:
    """One changed file between the two commits.

    Attributes:
        status: ``A`` (added), ``M`` (modified) or ``D`` (deleted).
        path: Repository path of the file.
    """

    status: str
    path: str


class UpstreamSyncError(RuntimeError):
    """The upstream repository or the lock could not be read."""


def _git_exe() -> str:
    """Return a verified git executable path (S607).

    Returns:
        The git path found on PATH.
    """
    found = shutil.which("git")
    if found is None:
        raise UpstreamSyncError("git is not installed or not on PATH")
    return found


def load_lock(path: Path = LOCK_PATH) -> dict[str, object]:
    """Load the upstream lock.

    Args:
        path: Lock file location.

    Returns:
        The parsed lock.

    Raises:
        UpstreamSyncError: The lock is missing or malformed.
    """
    if not path.is_file():
        raise UpstreamSyncError(f"upstream lock not found: {path}")
    try:
        lock: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UpstreamSyncError(f"upstream lock {path} is not valid JSON: {exc}") from exc
    if "upstream" not in lock or "files" not in lock:
        raise UpstreamSyncError(f"upstream lock {path} is missing required sections")
    return lock


def resolve_repository(url: str, upstream_path: str | None) -> Path:
    """Return a local checkout of the upstream repository.

    A local path (the reference clone) is used as-is; a URL is cloned
    into a temporary directory that the caller must clean up.

    Args:
        url: Upstream URL from the lock.
        upstream_path: Optional local clone path.

    Returns:
        The repository root.

    Raises:
        UpstreamSyncError: The path is not a git repository.
    """
    if upstream_path:
        root = Path(upstream_path)
        if not (root / ".git").is_dir():
            raise UpstreamSyncError(f"{root} is not a git repository")
        return root
    try:
        temp = tempfile.mkdtemp(prefix="opendata-upstream-")
        clone = Path(temp)
        _git(clone, "clone", "--quiet", url, str(clone / "repo"))
        return clone / "repo"
    except (subprocess.CalledProcessError, OSError) as exc:
        raise UpstreamSyncError(f"cannot clone {url}: {exc}") from exc


def _git(cwd: Path, *args: str) -> str:
    """Run git and return its stdout.

    Args:
        cwd: Repository root.
        *args: Git arguments.

    Returns:
        The command's stdout (stripped).

    Raises:
        UpstreamSyncError: git failed.
    """
    result = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, no shell
        [_git_exe(), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        raise UpstreamSyncError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def changed_files(repo: Path, base: str, head: str) -> list[FileChange]:
    """List the files changed between two commits.

    Args:
        repo: Upstream repository.
        base: Base commit (the locked one).
        head: Target commit (the simulated upstream move).

    Returns:
        The changes, ``A``/``M``/``D`` with their paths.
    """
    output = _git(repo, "diff", "--name-status", base, head)
    changes: list[FileChange] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            changes.append(FileChange(status=parts[0][0], path=parts[1]))
    return changes


def function_signatures(source: str) -> dict[str, tuple[str, ...]]:
    """Extract the top-level function signatures of a Python file.

    Args:
        source: The file's text.

    Returns:
        ``{name: (parameter names)}`` for top-level functions and
        methods of top-level classes.
    """
    import ast

    tree = ast.parse(source)
    signatures: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signatures[node.name] = _parameters(node)
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    signatures[f"{node.name}.{member.name}"] = _parameters(member)
    return signatures


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    """Return a function's positional parameter names.

    Args:
        node: The function node.

    Returns:
        Parameter names, ``self`` included, defaults excluded.
    """
    args = node.args
    names = [arg.arg for arg in args.posonlyargs + args.args]
    names.extend(arg.arg for arg in args.kwonlyargs)
    if args.vararg is not None:
        names.append(f"*{args.vararg.arg}")
    if args.kwarg is not None:
        names.append(f"**{args.kwarg.arg}")
    return tuple(names)


def function_diff(repo: Path, base: str, head: str, path: str) -> dict:
    """Diff the top-level function signatures of one file.

    Args:
        repo: Upstream repository.
        base: Base commit.
        head: Target commit.
        path: Repository path of the file.

    Returns:
        ``{"added": {...}, "removed": {...}, "changed": [...]}``.
    """
    before = _git(repo, "show", f"{base}:{path}")
    after = _git(repo, "show", f"{head}:{path}")
    before_sigs = function_signatures(before)
    after_sigs = function_signatures(after)
    added = {name: sig for name, sig in after_sigs.items() if name not in before_sigs}
    removed = {name: sig for name, sig in before_sigs.items() if name not in after_sigs}
    changed = [
        {"function": name, "before": before_sigs[name], "after": after_sigs[name]}
        for name in sorted(before_sigs.keys() & after_sigs.keys())
        if before_sigs[name] != after_sigs[name]
    ]
    return {"added": added, "removed": removed, "changed": changed}


def verify_lock(repo: Path, lock: dict) -> list[str]:
    """Check the lock's per-file hashes against the upstream tree.

    Args:
        repo: Upstream repository.
        lock: The parsed lock.

    Returns:
        The lock entries whose sha256 no longer matches the frozen
        commit (empty when the lock reproduces exactly).
    """
    base = lock["upstream"]["commit"]
    drifted: list[str] = []
    for entry in lock["files"]:
        upstream_path = entry.get("upstream_path") or f"akshare/{entry['path']}"
        try:
            blob = _git(repo, "show", f"{base}:{upstream_path}")
        except UpstreamSyncError:
            drifted.append(f"{upstream_path}: missing at {base}")
            continue
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        if digest != entry["sha256"]:
            drifted.append(
                f"{upstream_path}: sha256 {digest[:16]}... != locked {entry['sha256'][:16]}..."
            )
    return drifted


def render_report(
    lock: dict,
    changes: list[FileChange],
    base: str,
    head: str,
    repo: Path,
    *,
    include_functions: bool = True,
) -> str:
    """Render the markdown sync report.

    Args:
        lock: The parsed lock.
        changes: The file inventory.
        base: Base commit.
        head: Target commit.
        repo: Upstream repository.
        include_functions: Whether to diff function signatures.

    Returns:
        The report text.
    """
    upstream = lock["upstream"]
    if not isinstance(upstream, dict):
        raise UpstreamSyncError("upstream lock section must be an object")
    url = str(upstream.get("url", ""))
    lines = [
        f"# 上游同步差异报告（{base[:12]} → {head[:12]}）",
        "",
        f"- 上游仓库：{url}",
        f"- 锁定基线：`{base}`",
        f"- 目标提交：`{head}`",
        "",
        f"## 变更清单（{len(changes)} 个文件）",
        "",
        "| 状态 | 文件 |",
        "|------|------|",
    ]
    counts = {"A": 0, "M": 0, "D": 0}
    for change in sorted(changes, key=lambda item: item.path):
        counts[change.status] = counts.get(change.status, 0) + 1
        label = {"A": "新增", "M": "修改", "D": "删除"}.get(change.status, change.status)
        lines.append(f"| {label} | `{change.path}` |")
    lines.append("")
    if include_functions:
        lines.append("## 关键函数签名差异")
        lines.append("")
        for change in sorted(changes, key=lambda item: item.path):
            if change.status != "M" or not change.path.endswith(".py"):
                continue
            diff = function_diff(repo, base, head, change.path)
            if not (diff["added"] or diff["removed"] or diff["changed"]):
                continue
            lines.append(f"### `{change.path}`")
            if diff["added"]:
                lines.append("- 新增：")
                lines.extend(
                    f"  - `{name}({', '.join(sig)})`" for name, sig in diff["added"].items()
                )
            if diff["removed"]:
                lines.append("- 移除：")
                lines.extend(
                    f"  - `{name}({', '.join(sig)})`" for name, sig in diff["removed"].items()
                )
            lines.extend(
                f"- 签名变更：`{item['function']}` "
                f"({', '.join(item['before'])}) → ({', '.join(item['after'])})"
                for item in diff["changed"]
            )
            lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the sync drill.

    Args:
        argv: CLI arguments (None reads ``sys.argv``).

    Returns:
        Exit code.
    """
    parser = argparse.ArgumentParser(description="Produce the upstream sync diff report")
    parser.add_argument("--diff", action="store_true", help="produce the diff report")
    parser.add_argument("--upstream", help="local upstream clone (else the URL is cloned)")
    parser.add_argument("--ref", help="target commit/branch (default: upstream HEAD)")
    parser.add_argument(
        "--out",
        help="write the report to this path (else print to stdout)",
    )
    parser.add_argument("--no-functions", action="store_true", help="skip the signature diff")
    args = parser.parse_args(argv)

    if not args.diff:
        parser.error("--diff is required")
    lock = load_lock()
    upstream = lock["upstream"]
    if not isinstance(upstream, dict):
        raise UpstreamSyncError("upstream lock section must be an object")
    base = str(upstream["commit"])
    repo = resolve_repository(str(upstream["url"]), args.upstream)
    try:
        head = args.ref or _git(repo, "rev-parse", "HEAD").strip()
        changes = changed_files(repo, base, head)
        drifted = verify_lock(repo, lock)
        report = render_report(
            lock,
            changes,
            base,
            head,
            repo,
            include_functions=not args.no_functions,
        )
        report += "\n## 锁校验\n\n"
        if drifted:
            report += f"**{len(drifted)} 个锁定文件与冻结提交不一致**：\n"
            report += "\n".join(f"- {line}" for line in drifted)
            report += "\n"
        else:
            report += "锁定文件的 sha256 与冻结提交完全一致。\n"
        if args.out:
            Path(args.out).write_text(report, encoding="utf-8")
            print(f"report written to {args.out}")
        else:
            print(report)
        return 1 if drifted else 0
    finally:
        if args.upstream is None:
            shutil.rmtree(repo.parent, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
