"""C39 mutation tour: prove the render guard bites in both directions.

C38 counted 13 log calls whose arguments never reach the operator, and counted them
with a *report* - a report cannot stay red. This tour injects each of the 13 defects
back into the fixed tree and requires ``make loguru-check`` to go red on exactly that
file, then neuters seven faces of the guard itself and requires the matching test to go
red. A mutant that leaves the reading green marks a face the guard does not actually
cover, so every row here is a claim about the checker, not about the code style.

Every pytest call carries ``-m "not e2e" --no-cov``: the guard's own test file hosts
no e2e class today, but the flag is the habit that keeps a production warehouse out of
a mutation run (see ``docs/evidence/C38b/README.md`` section 6).
"""

from __future__ import annotations

import hashlib
import platform
import subprocess  # nosec B404
import sys
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

REPO = Path(__file__).resolve().parents[3]
GUARD = "scripts/quality/loguru_render_check.py"
TESTS = "tests/test_loguru_render_check.py"
PYTEST_FLAGS = ["-m", "not e2e", "--no-cov", "-q", "--no-header"]

#: (file, the line C39 shipped, the pre-C39 line, what the mutant claims)
SITE_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "opendata/api/data_query.py",
        '        logger.warning("diff report unavailable for {}: {}", domain, exc)\n',
        '        logger.warning("diff report unavailable for %s: %s", domain, exc)\n',
        "S1 域差异日志降级不可用时参数丢失",
    ),
    (
        "opendata/api/data_query.py",
        '        logger.error("data query failed for {}/{}: {}", asset_class, domain, exc)\n',
        '        logger.error("data query failed for %s/%s: %s", asset_class, domain, exc)\n',
        "S2 数据查询失败日志只剩模板（排障看不出是哪个域）",
    ),
    (
        "opendata/api/data_query.py",
        '        logger.debug("freshness unavailable for {}: {}", table, exc)\n',
        '        logger.debug("freshness unavailable for %s: %s", table, exc)\n',
        "S3 新鲜度降级日志丢表名",
    ),
    (
        "opendata/api/tables.py",
        '        logger.debug("Table {} not in warehouse, skipping: {}", '
        "table.table_name, error)\n",
        '        logger.debug("Table %s not in warehouse, skipping: %s", '
        "table.table_name, error)\n",
        "S4 行数刷新跳过日志丢表名与原因",
    ),
    (
        "opendata/api/websocket.py",
        '            logger.debug("WebSocket ping loop ended: {}", e)\n',
        '            logger.debug("WebSocket ping loop ended: %s", e)\n',
        "S5 WS 心跳退出原因丢失",
    ),
    (
        "opendata/core/token_blacklist.py",
        '            logger.debug("Redis close failed (may already be closed): {}", e)\n',
        '            logger.debug("Redis close failed (may already be closed): %s", e)\n',
        "S6 Redis 关闭失败原因丢失",
    ),
    (
        "opendata/data_fetch/providers/akshare_provider.py",
        '            self.logger.debug(f"Could not get row count for table {table_name}: {e}")\n',
        '            self.logger.debug("Could not get row count for table %s: %s", '
        "table_name, e)\n",
        "S7 属性式接收者（self.logger）——census 看不见的那一条",
    ),
    (
        "opendata/middleware/request_logging.py",
        '                    "{} {} 500 ({:.1f}ms) exception={}",\n',
        '                    "%s %s 500 (%.1fms) exception=%s",\n',
        "S8 500 请求日志丢方法/路径/耗时/异常",
    ),
    (
        "opendata/middleware/request_logging.py",
        '                logger.debug("Metrics record skipped: {}", e)\n',
        '                logger.debug("Metrics record skipped: %s", e)\n',
        "S9 指标上报跳过原因丢失",
    ),
    (
        "opendata/middleware/security.py",
        '            logger.debug("Failed to set security headers: {}", e)\n',
        '            logger.debug("Failed to set security headers: %s", e)\n',
        "S10 安全头写入失败原因丢失",
    ),
    (
        "opendata/services/scheduler.py",
        '                    logger.debug("Task failure notification skipped: {}", e)\n',
        '                    logger.debug("Task failure notification skipped: %s", e)\n',
        "S11 任务失败通知异常丢失",
    ),
    (
        "opendata/services/scheduler.py",
        '            logger.debug("WebSocket broadcast skipped: {}", e)\n',
        '            logger.debug("WebSocket broadcast skipped: %s", e)\n',
        "S12 广播跳过异常丢失",
    ),
    (
        "opendata/services/script_service.py",
        '            logger.debug("Could not read script file {}: {}", file_path, e)\n',
        '            logger.debug("Could not read script file %s: %s", file_path, e)\n',
        "S13 脚本读取失败丢路径",
    ),
]

#: (guard 的哪一面, 现在的写法, 摘掉后的写法, 期望失败的用例)
CLASSIFIER_MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "属性式接收者解析（self.<attr> 的绑定）",
        "        family = UNRESOLVED\n"
        '        if isinstance(base, ast.Name) and base.id == "self":\n'
        "            family = attributes.get(receiver_node.attr, UNRESOLVED)\n",
        "        family = UNRESOLVED\n",
        "C1 把 self.logger 退回 unresolved：census 的老盲区重新变成盲区",
        f"{TESTS}::TestAttributeFormFace",
    ),
    (
        "裸名接收者解析（模块级 logger 的绑定）",
        "    if isinstance(receiver_node, ast.Name):\n"
        "        name = receiver_node.id\n"
        "        family = names.get(name) or _family_of_name(name, origins, names)\n"
        "        if family is not None:\n"
        "            return name, family\n"
        "        return (name, UNRESOLVED) if _is_logger_name(name) else None\n",
        "    if isinstance(receiver_node, ast.Name):\n        return None\n",
        "C2 裸名一律不当日志调用：12 条那一面被摘掉",
        f"{TESTS}::TestBindingAwareJudgement::test_arguments_with_no_placeholder_are_lost_on_both",
    ),
    (
        "verdict_of 全部判定",
        "    if not site.literal:\n        return None\n",
        "    return None\n    if not site.literal:\n        return None\n",
        "C3 判定器恒返回“没问题”：只报不判",
        f"{TESTS}::TestBindingAwareJudgement::test_loguru_percent_template_loses_its_arguments",
    ),
    (
        "verdict_of 的 stdlib 方向",
        "    if site.family == STDLIB:\n"
        "        if has_percent:\n"
        "            return None\n"
        '        return "{} args on a logging logger" if has_brace else "no % spec for the args"\n',
        "    if site.family == STDLIB:\n        return None\n",
        "C4 只判 loguru 一侧：另一条静默方向（loud 的那条）不再覆盖",
        f"{TESTS}::TestBindingAwareJudgement::test_stdlib_brace_template_loses_its_arguments",
    ),
    (
        "插值消息只豁免“不带参数”的那一半",
        "    template_node = call.args[index]\n    if isinstance(template_node, ast.Constant):\n",
        "    template_node = call.args[index]\n"
        "    if isinstance(template_node, ast.JoinedStr):\n"
        "        return None\n"
        "    if isinstance(template_node, ast.Constant):\n",
        "C5 f-string 重新被当成无条件安全：带参数的插值写法又会被静默放过",
        f"{TESTS}::TestAttributeFormFace"
        "::test_finished_message_that_also_passes_arguments_is_disclosed",
    ),
    (
        "run() 的阻断（exit code）",
        "    if sweep.findings and not args.report:\n",
        "    if False and sweep.findings and not args.report:\n",
        "C6 只打印不置红：门禁里的成员退化成报告",
        f"{TESTS}::TestEnforcementWiring::test_gate_mode_fails_on_a_finding",
    ),
    (
        "CHECK_ROOTS 覆盖面",
        'CHECK_ROOTS: Final = (\n    "opendata",\n    "opendata_http",\n',
        'CHECK_ROOTS: Final = (\n    "opendata",\n    # "opendata_http",\n',
        "C7 悄悄缩范围：搬运层 313 个文件退出扫描面",
        f"{TESTS}::TestShippedTree::test_every_declared_root_sweeps_files",
    ),
]


def sha256(path: Path) -> str:
    """Fingerprint used to prove every mutant was reverted byte-exactly."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inject(rel: str, current: str, mutant: str) -> str | None:
    """Write one mutant, returning the original text to restore (``None`` if unanchored).

    Args:
        rel: Repo-relative path of the file to mutate.
        current: The exact text the shipped tree holds.
        mutant: The injected text.

    Returns:
        The pre-injection contents, or ``None`` when the anchor is not unique -
        an ambiguous anchor is not injected, because a mutant that lands twice
        proves nothing about the site it names.
    """
    path = REPO / rel
    original = path.read_text(encoding="utf-8")
    if original.count(current) != 1:
        return None
    path.write_text(original.replace(current, mutant, 1), encoding="utf-8")
    return original


def command(label: str, argv: list[str]) -> tuple[int, str]:
    """Run a command, echoing it and its last non-blank lines for the log."""
    print(f"    $ {' '.join(argv)}")
    proc = subprocess.run(  # noqa: S603  # nosec B603
        argv, cwd=REPO, capture_output=True, text=True, check=False
    )
    body = f"{proc.stdout}{proc.stderr}"
    kept = [line for line in body.splitlines() if line.strip()]
    for line in kept[-6:]:
        print(f"    | {line}")
    print(f"    rc={proc.returncode}")
    return proc.returncode, body


def guard_run() -> tuple[int, list[str]]:
    """Run the guard in gate mode and return its exit code plus the FINDING rows."""
    rc, body = command("guard", [sys.executable, GUARD])
    rows = [line for line in body.splitlines() if line.startswith("FINDING ")]
    return rc, rows


def test_run(target: str) -> tuple[int, str]:
    """Run one test target with the marker filter that keeps e2e out of the process."""
    rc, body = command("pytest", [sys.executable, "-m", "pytest", target, *PYTEST_FLAGS])
    rows = [line for line in body.splitlines() if line.strip()]
    return rc, rows[-1] if rows else "(no output)"


def header() -> dict[str, str]:
    """Print the provenance block and return the pre-tour fingerprints of the tree."""
    print("# C39 变异巡回（mutation proof）：loguru/logging 参数渲染门禁")
    print(f"# 生成时间: {datetime.now().astimezone().isoformat(timespec='seconds')}")
    print(f"# python={platform.python_version()} 机器={platform.machine()}")
    for name in ("coverage", "loguru", "pytest", "pytest-asyncio", "pytest-cov", "pytest-xdist"):
        print(f"# {name}=={version_of(name)}")
    for name, argv in (
        ("分支", ["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        ("HEAD", ["git", "rev-parse", "--short", "HEAD"]),
    ):
        out = subprocess.run(  # noqa: S603  # nosec B603
            argv, cwd=REPO, capture_output=True, text=True, check=False
        )
        print(f"# {name}: {out.stdout.strip()}")
    print(f"# pytest 命令（每次调用固定）: python -m pytest <target> {' '.join(PYTEST_FLAGS)}")
    print("# 说明：`make loguru-check` 就是门禁成员（Makefile gate 段），")
    print("#       站点变异用 `python scripts/quality/loguru_render_check.py` 直接跑同一份判定。")

    files = sorted({rel for rel, *_ in SITE_MUTATIONS} | {GUARD})
    baseline = {rel: sha256(REPO / rel) for rel in files}
    print("# 涉及文件 sha256（变异前基线，巡回结束后逐字节复核）")
    for rel, digest in baseline.items():
        print(f"#   {digest}  {rel}")
    return baseline


def version_of(name: str) -> str:
    """Package version as installed here, or a marker when the package is absent."""
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "缺失"


def site_tour() -> int:
    """Inject each of the 13 shipped defects and require the guard to name it.

    Returns:
        The number of rows that did not behave as claimed.
    """
    broken = 0
    for index, (rel, current, mutant, label) in enumerate(SITE_MUTATIONS, start=1):
        print(f"[S{index}] {label}")
        print(f"    文件: {rel}")
        print(f"    现有: {current.strip()}")
        print(f"    注入: {mutant.strip()}")
        original = inject(rel, current, mutant)
        if original is None:
            print("    结果: 锚点不唯一，注入未执行 ⇒ BROKEN")
            broken += 1
            continue
        try:
            rc, rows = guard_run()
        finally:
            (REPO / rel).write_text(original, encoding="utf-8")
        named = [row for row in rows if row.startswith(f"FINDING {rel}:")]
        if rc != 0 and len(named) == 1:
            print(f"    结果: RED 且只点名本文件 ⇒ 判据咬住了：{named[0]}")
        else:
            print(f"    结果: rc={rc} findings={len(rows)} 本文件={len(named)} ⇒ BROKEN")
            broken += 1
    return broken


def classifier_tour() -> int:
    """Neuter each judged face of the guard and require its test to go red.

    Returns:
        The number of rows that did not behave as claimed.
    """
    broken = 0
    for face, current, mutant, label, target in CLASSIFIER_MUTATIONS:
        print(f"{label}")
        print(f"    面: {face}")
        print(f"    期望失败: {target}")
        original = inject(GUARD, current, mutant)
        if original is None:
            print("    结果: 锚点不唯一，注入未执行 ⇒ BROKEN")
            broken += 1
            continue
        print(f"    摘掉: {mutant.strip().replace(chr(10), ' / ')}")
        try:
            rc, summary = test_run(target)
        finally:
            (REPO / GUARD).write_text(original, encoding="utf-8")
        if rc != 0:
            print(f"    结果: RED ⇒ 这一面确实在承重：{summary}")
        else:
            print(f"    结果: GREEN ⇒ 这一面没有测试守着：{summary}")
            broken += 1
    return broken


def make_check() -> int:
    """Run the gate member exactly as ``make gate`` does."""
    return command("make", ["make", "--no-print-directory", "loguru-check"])[0]


def guard_check() -> int:
    """Run the guard directly in gate mode."""
    return guard_run()[0]


def tests_check() -> int:
    """Run the guard's own test file."""
    return test_run(TESTS)[0]


GREEN_READINGS: list[tuple[str, Callable[[], int]]] = [
    ("make loguru-check", make_check),
    ("python scripts/quality/loguru_render_check.py", guard_check),
    (TESTS, tests_check),
]


def final_readings(checks: list[tuple[str, Callable[[], int]]]) -> int:
    """Re-run the green readings after the tour and report any leftover."""
    broken = 0
    for name, check in checks:
        print(f"  {name}")
        if check() != 0:
            broken += 1
    return broken


def main() -> int:
    """Drive the tour and print the tally."""
    baseline = header()
    print()
    print("## [0] 基线：门禁成员绿、守卫测试全绿")
    unexpected = final_readings(GREEN_READINGS)
    if unexpected:
        print("    ⇒ BROKEN：基线本身不绿，后面的读数无意义")
        return 2

    print()
    print("## [1] 站点变异：13 条已修调用逐个注回旧写法，期望门禁点名")
    unexpected += site_tour()

    print()
    print("## [2] 判定面变异：摘掉守卫的某一个判定面，期望对应用例失败")
    unexpected += classifier_tour()

    print()
    print("## [3] 还原复核：逐文件 sha256 对比基线 + 复跑绿读数")
    for rel, digest in sorted(baseline.items()):
        now = sha256(REPO / rel)
        ok = "OK" if now == digest else "MISMATCH ⇒ BROKEN"
        print(f"  {ok}  {rel}")
        if now != digest:
            unexpected += 1
    unexpected += final_readings(GREEN_READINGS)

    print()
    verdict = "全部变异都被咬住（BROKEN=0）" if unexpected == 0 else f"{unexpected} 项未达预期"
    print(f"## 结论：{verdict}")
    return 1 if unexpected else 0


if __name__ == "__main__":
    raise SystemExit(main())
