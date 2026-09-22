"""Generate the porting diff report (design §5.6, FR-5).

Verifies every ported file against its upstream baseline and writes
``docs/port-report.md``. The report's pending-TODO section is the A1.6
acceptance surface (待办清零):

* **import gaps** - a ported module importing ``opendata_http.*`` that
  is not (yet) ported; the import closure is incomplete;
* **manual drift** - a ported file whose content differs from the
  deterministic codemod replay of its upstream source (an unregistered
  hand edit);
* **lock mismatches** - upstream files whose pristine sha256 no longer
  matches the lock (upstream moved under us).

Exit code 1 when any TODO is pending, so CI/Makefile can gate on it.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.codemod.port_module import (  # noqa: E402
    _INIT_UPSTREAM_PATH,
    DEFAULT_UPSTREAM_REPO,
    PORTED_PACKAGE,
    PORTED_ROOT,
    UpstreamLock,
    _subset_init,
    port_source,
    sha256_text,
)

REPORT_PATH = Path("docs/port-report.md")


def _module_exists(ported_root: Path, module: str) -> bool:
    """Whether an ``opendata_http.*`` module path exists in the tree."""
    if module == PORTED_PACKAGE:
        return ported_root.is_dir()
    if not module.startswith(PORTED_PACKAGE + "."):
        return True
    rest = module.removeprefix(PORTED_PACKAGE + ".").replace(".", "/")
    return (ported_root / f"{rest}.py").is_file() or (ported_root / rest).is_dir()


def collect_import_gaps(ported_root: Path) -> list[str]:
    """Find ported modules whose intra-package imports are unported.

    Args:
        ported_root: The ``opendata_http`` directory.

    Returns:
        Human-readable gap descriptions, one per broken import.
    """
    gaps: list[str] = []
    for path in sorted(ported_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ported_root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            gaps.append(f"{rel}: syntax error in ported file: {exc}")
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith(PORTED_PACKAGE + ".")
            ):
                if not _module_exists(ported_root, node.module):
                    gaps.append(f"{rel}: imports {node.module} which is not ported (closure gap)")
            elif isinstance(node, ast.Import):
                broken = [
                    alias.name
                    for alias in node.names
                    if alias.name.startswith(PORTED_PACKAGE + ".")
                    and not _module_exists(ported_root, alias.name)
                ]
                gaps.extend(
                    f"{rel}: imports {name} which is not ported (closure gap)" for name in broken
                )
    return gaps


def collect_drift(
    ported_root: Path, lock: UpstreamLock, upstream_repo: Path
) -> tuple[list[str], list[dict[str, object]]]:
    """Compare ported content against the deterministic replay.

    Args:
        ported_root: The ``opendata_http`` directory.
        lock: The baseline lock.
        upstream_repo: Clean upstream clone at the locked commit.

    Returns:
        Pending drift TODOs and the per-file verification table.
    """
    todos: list[str] = []
    table: list[dict[str, object]] = []
    for rel, record in sorted(lock.files.items()):
        ported = ported_root / rel
        if not ported.is_file():
            todos.append(f"{rel}: recorded in lock but missing from the ported tree")
            continue
        upstream_path = str(record["upstream_path"])
        pristine = upstream_repo / upstream_path
        if not pristine.is_file():
            todos.append(f"{rel}: upstream file {upstream_path} disappeared upstream")
            continue
        pristine_bytes = pristine.read_bytes()
        pristine_sha = sha256_text(pristine_bytes.decode("utf-8", errors="replace"))
        if pristine_sha != str(record["sha256"]):
            todos.append(
                f"{rel}: upstream sha256 drifted from the lock "
                f"(lock={str(record['sha256'])[:12]}, now={pristine_sha[:12]}); re-pin?"
            )
            continue
        if ported.suffix != ".py":
            # Resources are copied verbatim: ported == upstream.
            matches = sha256_text(ported.read_text(encoding="utf-8")) == pristine_sha
            if not matches:
                todos.append(f"{rel}: resource differs from the upstream original")
            table.append(
                {
                    "path": rel,
                    "upstream_path": upstream_path,
                    "import_rewrites": 0,
                    "string_rewrites": 0,
                    "manual_edits": record.get("manual_edits", False),
                    "replay_matches": matches,
                }
            )
            continue
        replay_text = pristine_bytes.decode("utf-8")
        if upstream_path == _INIT_UPSTREAM_PATH:
            # The aggregator is ported with subset filtering; the replay
            # must apply the same deterministic filter.
            ported_modules = {rel.removesuffix(".py") for rel in lock.files}
            replay_text, _kept, _dropped = _subset_init(replay_text, ported_modules)
        expected, result = port_source(replay_text, upstream_path, lock.url, lock.commit)
        actual = ported.read_text(encoding="utf-8")
        matches = sha256_text(actual) == result.ported_sha256 == sha256_text(expected)
        if not matches:
            todos.append(f"{rel}: ported content differs from the codemod replay (manual edit?)")
        table.append(
            {
                "path": rel,
                "upstream_path": upstream_path,
                "import_rewrites": result.import_rewrites,
                "string_rewrites": result.string_rewrites,
                "manual_edits": record.get("manual_edits", False),
                "replay_matches": matches,
            }
        )
    return todos, table


def build_report(
    lock: UpstreamLock, upstream_repo: Path, report_path: Path = REPORT_PATH
) -> tuple[str, list[str]]:
    """Render the markdown report.

    Args:
        lock: The baseline lock.
        upstream_repo: Clean upstream clone at the locked commit.
        report_path: Output markdown path.

    Returns:
        The markdown text and the pending TODO list.
    """
    gaps = collect_import_gaps(PORTED_ROOT)
    drift, table = collect_drift(PORTED_ROOT, lock, upstream_repo)
    todos = gaps + drift

    lines: list[str] = [
        "# 搬运差异报告（A1.5/A1.6，FR-5）",
        "",
        f"- 上游基线：`{lock.url}` @ `{lock.commit}`",
        f"- 已搬运文件：{len(lock.files)}（含资源）",
        f"- 人工待办：**{len(todos)}**（A1.6 验收要求为 0）",
        "",
        "## 逐文件对照（改写计数与重放一致性）",
        "",
        "| 文件 | 上游路径 | import 改写 | 字符串改写 | 人工改动 | 重放一致 |",
        "|------|---------|------------|-----------|---------|---------|",
    ]
    lines.extend(
        f"| `{row['path']}` | `{row['upstream_path']}` | {row['import_rewrites']} "
        f"| {row['string_rewrites']} | {row['manual_edits']} | "
        f"{'✓' if row['replay_matches'] else '✗'} |"
        for row in table
    )
    lines += ["", "## 人工待办清单", ""]
    if todos:
        lines.extend(f"- [ ] {todo}" for todo in todos)
    else:
        lines.append("（无 —— 待办清零）")
    lines.append("")
    return "\n".join(lines), todos


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; exit code 1 when TODOs are pending.

    Raises:
        SystemExit: With the exit code.
    """
    parser = argparse.ArgumentParser(description="Generate docs/port-report.md.")
    parser.add_argument("--upstream-repo", type=Path, default=DEFAULT_UPSTREAM_REPO)
    parser.add_argument(
        "--report-path", type=Path, default=REPORT_PATH, help="output markdown path"
    )
    args = parser.parse_args(argv)

    try:
        lock = UpstreamLock.load()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    text, todos = build_report(lock, args.upstream_repo, args.report_path)
    args.report_path.write_text(text, encoding="utf-8")
    print(f"wrote {args.report_path} ({len(lock.files)} files, {len(todos)} pending TODO(s))")
    if todos:
        for todo in todos:
            print(f"  TODO {todo}", file=sys.stderr)
        return 1
    print("OK: no pending TODOs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
