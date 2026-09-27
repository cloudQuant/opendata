"""C44 修复面的行为反事实：每条改完必须能红，改回来必须能绿.

普查面（``shell_audit.py``）证明的是「空壳没了」，但把空壳换成行为断言之后，
新断言本身也可能是恒真的。这里逐条把**生产侧**（或测试侧的一处关键行）改掉，
跑对应用例期望它失败，然后原样还原并核对校验和——所以这份脚本既是证据，
也是一次可重跑的自检。

用法（工作树必须干净到能安全临时改写这几个文件，脚本自己负责还原）::

    python docs/evidence/C44/behaviour_counterfactuals.py

退出码：任何一条「改坏不红 / 还原不绿 / 还原后校验和不一致」都返回 1。
"""

from __future__ import annotations

import hashlib
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Case:
    """One falsifiable claim: a production edit that must break a named test."""

    label: str
    source: str
    old: str
    new: str
    nodes: tuple[str, ...]


#: 「生产侧动一下，这条用例就该红」的清单。前 9 条是 C44 把空壳换掉后新增的行为面，
#: 最后 2 条是本轮同时改过的 T1 黄金向量与调度器状态面。
CASES: Final[tuple[Case, ...]] = (
    Case(
        "契约族 extra=forbid",
        "opendata/data/models/base.py",
        'ConfigDict(extra="forbid", allow_inf_nan=False)',
        'ConfigDict(extra="ignore", allow_inf_nan=False)',
        (
            "tests/test_contract_models.py::TestSemantics::test_every_contract_model_refuses_an_undeclared_field",
        ),
    ),
    Case(
        "ods 默认批次粒度",
        "opendata/pipeline/ods_writer.py",
        "DEFAULT_BATCH_SIZE = 50_000",
        "DEFAULT_BATCH_SIZE = 100_000",
        (
            "tests/test_ods_writer.py::TestDirectWrite::test_batch_size_defaults_to_the_design_granularity",
        ),
    ),
    Case(
        "退避天花板",
        "opendata/services/retry_service.py",
        "MAX_RETRY_DELAY = 3600",
        "MAX_RETRY_DELAY = 7200",
        (
            "tests/test_retry_service.py::TestRetryService::test_backoff_ladder_plateaus_at_the_ceiling",
        ),
    ),
    Case(
        "角色字面量判权",
        "opendata/models/user.py",
        'ADMIN = "admin"',
        'ADMIN = "administrator"',
        (
            "tests/test_models_extended.py::TestUserModel::test_stored_role_strings_are_what_the_checker_uses",
        ),
    ),
    Case(
        "差异报告表名",
        "opendata/pipeline/diff_report.py",
        'DQ_DIFF_REPORT_TABLE = "dq_diff_report"',
        'DQ_DIFF_REPORT_TABLE = "dq_diff_report_v2"',
        (
            "tests/test_diff_report.py::TestReportRows::test_report_writer_and_retention_share_the_named_table",
        ),
    ),
    Case(
        "dump 源标签决定落库表",
        "opendata/pipeline/dump_import.py",
        'FUYAO_SOURCE = "ths"',
        'FUYAO_SOURCE = "iwc"',
        (
            "tests/test_dump_import.py::TestImportValidation::test_source_label_decides_the_ods_table",
        ),
    ),
    Case(
        "app_name 进 OpenAPI 标题",
        "opendata/core/config.py",
        'app_name: str = Field(default="opendata"',
        'app_name: str = Field(default="opendata-x"',
        (
            "tests/test_config.py::TestSettings::test_app_name_is_the_name_the_service_shows_the_operator",
        ),
    ),
    Case(
        "app_name 进 CLI 报告",
        "opendata/core/config.py",
        'app_name: str = Field(default="opendata"',
        'app_name: str = Field(default="opendata-x"',
        ("tests/test_core.py::TestCoreConfig::test_settings_app_name_reaches_the_cli_report",),
    ),
    Case(
        "yfinance 路由标签",
        "opendata/data/providers/yfinance/models/stock_daily.py",
        "source=SOURCE,",
        'source="yf",',
        ("tests/test_yfinance_provider.py::TestRegistration::test_source_label_routes_the_leg",),
    ),
    Case(
        "fuyao 毫秒戳黄金向量",
        "opendata_fuyao/endpoints.py",
        ".timestamp() * 1000)",
        ".timestamp() * 1000 + 1)",
        (
            "tests/test_fuyao_recorded_envelopes.py::TestGoldenVectors::test_prices_request_equals_precomputed_millis",
        ),
    ),
    Case(
        "默认下载上限容得下真机 dump",
        "opendata_fuyao/dumps.py",
        "DEFAULT_MAX_DUMP_BYTES = 512 * 1024 * 1024",
        "DEFAULT_MAX_DUMP_BYTES = 1024 * 1024",
        ("tests/test_fuyao_dumps.py::TestDownload::test_default_cap_admits_the_real_dump_size",),
    ),
    Case(
        "调度器 shutdown 状态翻转",
        "tests/test_scheduler_details.py",
        "        await asyncio.sleep(0)\n",
        "",
        (
            "tests/test_scheduler_details.py::TestSchedulerServiceLifecycle::test_scheduler_shutdown",
        ),
    ),
)


def _digest(path: Path) -> str:
    """sha256 of a file (the restore check)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pytest(nodes: tuple[str, ...]) -> tuple[int, str]:
    """Run the given node ids and return ``(exit_code, untrimmed output)``."""
    completed = subprocess.run(  # noqa: S603 - literal argv, no shell  # nosec B603
        [sys.executable, "-m", "pytest", "-p", "no:randomly", "--no-cov", "-q", *nodes],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


def run_case(case: Case) -> list[str]:
    """Baseline green -> production edit -> expected red -> verified restore."""
    path = REPO_ROOT / case.source
    original = path.read_text(encoding="utf-8")
    if original.count(case.old) != 1:
        return [f"FAIL {case.label}: 锚点在 {case.source} 里不是唯一命中"]
    before = _digest(path)

    lines: list[str] = []
    code, output = _pytest(case.nodes)
    lines.append(f"--- baseline ({case.label}) exit={code}")
    lines.append(output)
    if code != 0:
        lines.append(f"FAIL {case.label}: 修复后的基线就不绿")
        return lines

    path.write_text(original.replace(case.old, case.new, 1), encoding="utf-8")
    try:
        code, output = _pytest(case.nodes)
        lines.append(f"--- mutated ({case.label}) exit={code}")
        lines.append(output)
        lines.append(
            f"{'ok  ' if code != 0 else 'FAIL'} break[{case.label}] 改坏后 "
            f"{'红（exit ' + str(code) + '）' if code else '仍然绿——这条面是假的'}"
        )
    finally:
        path.write_text(original, encoding="utf-8")
        restored = _digest(path) == before
        lines.append(
            f"{'ok  ' if restored else 'FAIL'} restore[{case.label}] {case.source} 校验和一致"
        )
        if not restored:
            lines.append("FATAL: 还原失败，请手工 checkout 该文件")
    return lines


def main() -> int:
    """Run every behaviour counterfactual and report the tally."""
    results: list[str] = []
    for case in CASES:
        results += run_case(case)
    for line in results:
        print(line, end="" if line.endswith("\n") else "\n")
    failed = sum(1 for line in results if line.startswith("FAIL") or line.startswith("FATAL"))
    broke = sum(1 for line in results if line.startswith("ok   break["))
    verdict = "1" if failed else "0"
    tally = f"counterfactual_cases={len(CASES)} broke={broke} problems={failed} exit={verdict}"
    print(f"VERDICT {tally}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
