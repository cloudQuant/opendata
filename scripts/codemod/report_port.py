"""Generate the bundled AKShare vendor report (design §5.6, FR-5).

Verifies every ported file against its upstream baseline and writes
``docs/port-report.md``. The report's pending-TODO section is the A1.6
acceptance surface (待办清零):

* **import gaps** - a ported module importing the bundled vendor namespace that
  is not (yet) ported; the import closure is incomplete;
* **manual drift** - a ported file whose content differs from the
  deterministic codemod replay of its upstream source (an unregistered
  hand edit);
* **lock mismatches** - upstream files whose pristine sha256 no longer
  matches the lock (upstream moved under us).

The report also carries the built-in resource register (``AC-5|06``): the
``datasets.py`` accessors that raise instead of resolving a path are listed
from their own ``raise`` statements, so the "标注不可用并登记" branch has a
generated place to live rather than a hand-typed one.

Exit code 1 when any TODO is pending, so CI/Makefile can gate on it.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.codemod.migrate_provider_layout import (  # noqa: E402
    MigrationConfig,
    _lazy_vendor_init,
)
from scripts.codemod.port_module import (  # noqa: E402
    _INIT_UPSTREAM_PATH,
    DEFAULT_UPSTREAM_REPO,
    PORTED_PACKAGE,
    PORTED_ROOT,
    UpstreamLock,
    port_source,
    sha256_text,
)

REPORT_PATH = Path("docs/port-report.md")
RESOURCE_REGISTER_HEADING = "## 内置资源不可用登记（A2.3，AC-5|06）"
_DATASETS_MODULE = "datasets.py"
_UNLOCKED_METADATA = frozenset({"upstream.lock", "manifest.json", "LICENSE-AKSHARE"})


def _module_exists(ported_root: Path, module: str) -> bool:
    """Whether a bundled vendor module path exists in the tree."""
    if module == PORTED_PACKAGE:
        return ported_root.is_dir()
    if not module.startswith(PORTED_PACKAGE + "."):
        return True
    rest = module.removeprefix(PORTED_PACKAGE + ".").replace(".", "/")
    return (ported_root / f"{rest}.py").is_file() or (ported_root / rest).is_dir()


def collect_import_gaps(ported_root: Path) -> list[str]:
    """Find ported modules whose intra-package imports are unported.

    Args:
        ported_root: The bundled AKShare vendor directory.

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


def collect_unlocked_files(ported_root: Path, lock: UpstreamLock) -> list[str]:
    """Fail closed when the recursive vendor inventory contains unpinned files."""
    discovered = {
        path.relative_to(ported_root).as_posix()
        for path in ported_root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.relative_to(ported_root).as_posix() not in _UNLOCKED_METADATA
    }
    return [
        f"{rel}: file exists in the vendor tree but is absent from upstream.lock"
        for rel in sorted(discovered - set(lock.files))
    ]


def _message_parts(raise_stmt: ast.Raise) -> list[ast.expr]:
    """The interpolated pieces of a raised message, so the register can quote it verbatim."""
    exc = raise_stmt.exc
    if not isinstance(exc, ast.Call) or not exc.args:
        return []
    first = exc.args[0]
    return list(first.values) if isinstance(first, ast.JoinedStr) else [first]


def _message_text(raise_stmt: ast.Raise) -> str:
    """Quote the raised message with its interpolation slots kept visible, one line, no pipe."""
    pieces: list[str] = []
    for part in _message_parts(raise_stmt):
        if isinstance(part, ast.Constant):
            pieces.append(str(part.value))
        elif isinstance(part, ast.FormattedValue):
            pieces.append("{" + ast.unparse(part.value) + "}")
    text = " ".join(" ".join(pieces).split())
    return text.replace("|", "\\|") or "(无可读文本)"


def collect_resource_register(ported_root: Path, lock: UpstreamLock) -> list[dict[str, object]]:
    """Read the ported resource accessors and record which ones declare themselves unavailable.

    Args:
        ported_root: The bundled AKShare vendor directory.
        lock: The baseline lock, used to say whether the resource exists somewhere in the tree.

    Returns:
        One row per resource accessor that raises instead of resolving a path.
    """
    path = ported_root / _DATASETS_MODULE
    if not path.is_file():
        return []
    ported_names = {Path(rel).name for rel in lock.files}
    rows: list[dict[str, object]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.FunctionDef):
            continue
        raised = [
            stmt
            for stmt in node.body
            if isinstance(stmt, ast.Raise) and "unavailable" in ast.unparse(stmt)
        ]
        if not raised:
            continue
        default = next((d for d in node.args.defaults if isinstance(d, ast.Constant)), None)
        resource = str(getattr(default, "value", ""))
        rows.append(
            {
                "function": node.name,
                "resource": resource or "(无默认值)",
                "in_tree": "是" if resource in ported_names else "否",
                "reason": _message_text(raised[0]),
            }
        )
    return rows


def _sha256_bytes(data: bytes) -> str:
    """Hash exact file bytes so binary resources retain their provenance."""
    return hashlib.sha256(data).hexdigest()


def collect_drift(
    ported_root: Path, lock: UpstreamLock, upstream_repo: Path
) -> tuple[list[str], list[dict[str, object]]]:
    """Compare ported content against the deterministic replay.

    Args:
        ported_root: The bundled AKShare vendor directory.
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
        pristine_sha = (
            sha256_text(pristine.read_text(encoding="utf-8"))
            if pristine.suffix == ".py"
            else _sha256_bytes(pristine_bytes)
        )
        if pristine_sha != str(record["sha256"]):
            todos.append(
                f"{rel}: upstream sha256 drifted from the lock "
                f"(lock={str(record['sha256'])[:12]}, now={pristine_sha[:12]}); re-pin?"
            )
            continue
        ported_bytes = ported.read_bytes()
        ported_sha = _sha256_bytes(ported_bytes)
        if ported.suffix != ".py":
            # Resources are copied verbatim: ported == upstream.
            matches = ported_bytes == pristine_bytes
            if not matches:
                todos.append(f"{rel}: resource differs from the upstream original")
            table.append(
                {
                    "path": rel,
                    "upstream_path": upstream_path,
                    "upstream_sha256": pristine_sha,
                    "ported_sha256": ported_sha,
                    "import_rewrites": 0,
                    "string_rewrites": 0,
                    "manual_edits": record.get("manual_edits", False),
                    "replay_matches": matches,
                }
            )
            continue
        if upstream_path == _INIT_UPSTREAM_PATH:
            expected_facade, _facade_report = _lazy_vendor_init(
                pristine,
                MigrationConfig(
                    repo_root=_REPO_ROOT,
                    akshare_new_namespace=PORTED_PACKAGE,
                ),
            )
            matches = ported_bytes == expected_facade
            if not matches:
                todos.append(f"{rel}: lazy facade differs from the deterministic export map")
            table.append(
                {
                    "path": rel,
                    "upstream_path": upstream_path,
                    "upstream_sha256": pristine_sha,
                    "ported_sha256": ported_sha,
                    "import_rewrites": 0,
                    "string_rewrites": 0,
                    "manual_edits": record.get("manual_edits", False),
                    "replay_matches": matches,
                }
            )
            continue
        replay_text = pristine.read_text(encoding="utf-8")
        expected, result = port_source(replay_text, upstream_path, lock.url, lock.commit)
        expected_sha = sha256_text(expected)
        matches = ported_sha == result.ported_sha256 == expected_sha
        if not matches:
            todos.append(f"{rel}: ported content differs from the codemod replay (manual edit?)")
        table.append(
            {
                "path": rel,
                "upstream_path": upstream_path,
                "upstream_sha256": pristine_sha,
                "ported_sha256": ported_sha,
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
    gaps = collect_import_gaps(PORTED_ROOT) + collect_unlocked_files(PORTED_ROOT, lock)
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
        "| 文件 | 上游路径 | 上游 SHA-256 | 搬运 SHA-256 | import 改写 | 字符串改写 | "
        "人工改动 | 重放一致 |",
        "|------|---------|------------|-------------|------------|-----------|---------|---------|",
    ]
    lines.extend(
        f"| `{row['path']}` | `{row['upstream_path']}` "
        f"| `{str(row['upstream_sha256'])[:12]}` | `{str(row['ported_sha256'])[:12]}` "
        f"| {row['import_rewrites']} "
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

    register = collect_resource_register(PORTED_ROOT, lock)
    lines += ["", RESOURCE_REGISTER_HEADING, ""]
    if register:
        lines += [
            "登记来源：本节由 `scripts/codemod/report_port.py` 读 `datasets.py` 的 `raise` 语句"
            "与 `upstream.lock` 派生，不手写；`AC-5|06` 取判据的第二条支路"
            "「明确标注不可用并登记」，这里就是登记处。",
            "",
            "| 资源访问函数 | 声明的资源 | 同名文件在搬运清单内 | raise 文本（插值槽位原样） |",
            "|--------------|-----------|--------------------|---------------------------|",
        ]
        lines.extend(
            # 不带反引号是承重的：`AC-17|05` 把以 "| `" 开头的行当作重放表行数，
            # 本节若是同形就会被计进去，把两条登记行算进 315 的文件行数里。
            f"| {row['function']} | {row['resource']} | {row['in_tree']} | {row['reason']} |"
            for row in register
        )
        lines += [
            "",
            "- 「同名文件在搬运清单内 = 是」只说明该文件作为资源被搬进来了、"
            "路径与上游 `akshare.data` 约定不同，不代表函数可运行：两条 `raise` 都在函数体"
            "第一句。",
            "- 把这两个函数改成读搬运树内的路径需要改 "
            "`opendata/data/providers/akshare/_vendor/datasets.py` 正文，"
            "那会让该文件与 `upstream.lock` 的确定性重放不再一致（`AC-17|05` 的重放面），"
            "属搬运基线决策，不在本报告口径内。",
        ]
    else:
        lines.append("（`datasets.py` 里没有标注不可用的资源访问函数）")
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
