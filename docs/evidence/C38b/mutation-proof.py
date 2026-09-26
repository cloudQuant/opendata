"""C38b mutation proof: inject each mutant, expect RED, restore byte-exactly.

The archived copy of the runner that produced ``mutation-proof.txt``. Every pytest call
carries ``-m "not e2e"`` because each of these test files also hosts warehouse e2e
classes -- the first version of this script omitted the flag and reached production
MySQL; see README section 6 and ``mutation-proof-run1-e2e-polluted.txt``.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess  # nosec B404
import sys
from datetime import datetime
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

#: file, injected-old, injected-new, label, test target
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "opendata/pipeline/notify.py",
        "        await asyncio.to_thread(record_batch, self.engine, watermark)\n"
        "        event = BatchEvent(watermark=watermark, symbols=_symbols_of(context))\n"
        "        payload = None\n"
        "        if hub.wants_full(event):\n"
        "            payload = await asyncio.to_thread(self._read_rows, context, batch_id)\n"
        "        try:\n"
        "            delivered = await hub.publish_batch(event, rows=payload)\n",
        "        event = BatchEvent(watermark=watermark, symbols=_symbols_of(context))\n"
        "        payload = None\n"
        "        if hub.wants_full(event):\n"
        "            payload = await asyncio.to_thread(self._read_rows, context, batch_id)\n"
        "        try:\n"
        "            delivered = await hub.publish_batch(event, rows=payload)\n"
        "            await asyncio.to_thread(record_batch, self.engine, watermark)\n",
        "M1 notify：record 挪到 publish 之后（watermark 先写后 announce 的前提）",
        "tests/test_notify_unit.py",
    ),
    (
        "opendata/pipeline/ods_writer.py",
        "                finally:\n"
        "                    connection.execute(text(build_staging_drop_sql(staging)))\n",
        "                finally:\n                    pass\n",
        "M2 ods_writer：finally 里的 DROP 变成 pass（临时表不再回收）",
        "tests/test_ods_writer.py",
    ),
    (
        "opendata/pipeline/ods_writer.py",
        "                    for chunk in iter_batches(prepared, self.batch_size):\n"
        '                        connection.execute(text(f"TRUNCATE {_quote(staging)}"))\n',
        "                    for chunk in iter_batches(prepared, self.batch_size):\n",
        "M3 ods_writer：删掉每块的 TRUNCATE（上一块的行残留进 upsert）",
        "tests/test_ods_writer.py",
    ),
    (
        "opendata/pipeline/watermark.py",
        "    bounded = max(1, min(limit, MAX_REPLAY_LIMIT))\n"
        "    rows = _fetch(\n"
        "        engine,\n"
        '        "SELECT `batch_id`',
        '    bounded = limit\n    rows = _fetch(\n        engine,\n        "SELECT `batch_id`',
        "M4 watermark：replay 的 limit 不再夹取（0/超大都直接进 SQL）",
        "tests/test_watermark.py",
    ),
    (
        "opendata/pipeline/watermark.py",
        "    bounded = max(1, min(limit, MAX_REPLAY_LIMIT))\n"
        "    rows = _fetch(\n"
        "        engine,\n"
        '        f"SELECT `batch_id`',
        '    bounded = limit\n    rows = _fetch(\n        engine,\n        f"SELECT `batch_id`',
        "M5 watermark：latest 的 limit 不再夹取",
        "tests/test_watermark.py",
    ),
    (
        "opendata/pipeline/watermark.py",
        '        "ORDER BY `seq` ASC LIMIT :limit",',
        '        "ORDER BY `created_at` ASC LIMIT :limit",',
        "M6 watermark：按 created_at 排序而不是 seq（毫秒并列时顺序不稳定）",
        "tests/test_watermark.py",
    ),
    (
        "opendata/pipeline/factor_builder.py",
        "        closes = self._read_closes(_lookback(start), end)",
        "        closes = self._read_closes(start, end)",
        "M7 factor_builder：闭窗不再前扩（首事件无前收 → 整窗失败或漏价）",
        "tests/test_factor_builder_unit.py",
    ),
    (
        "opendata/pipeline/factor_builder.py",
        "                if (\n"
        "                    event.cash_dividend == 0.0\n"
        "                    and event.bonus == 0.0\n"
        "                    and event.allotment_ratio == 0.0\n"
        "                ):\n"
        "                    continue\n"
        "                events.append(event)\n",
        "                events.append(event)\n",
        "M8 factor_builder：全零事件不再被丢",
        "tests/test_factor_builder_unit.py",
    ),
    (
        "opendata/pipeline/templates.py",
        "            set(),\n"
        "            time_column=source_date_column,\n"
        "        )\n"
        "        if not rows:",
        "            set(),\n"
        "            time_column=contract_date_field,\n"
        "        )\n"
        "        if not rows:",
        "M9 templates：窗口过滤改用契约字段名（akshare 表里没有 日期→trade_date）",
        "tests/test_pipeline_templates.py",
    ),
    (
        "opendata/pipeline/templates.py",
        "        return frame[in_window | matches]",
        "        return frame[in_window]",
        "M10 templates：只返回窗口内的行（窗口外的受影响键被静默丢掉）",
        "tests/test_pipeline_templates.py",
    ),
]

TESTS = [
    "tests/test_notify_unit.py",
    "tests/test_ods_writer.py",
    "tests/test_watermark.py",
    "tests/test_factor_builder_unit.py",
    "tests/test_pipeline_templates.py",
]

PYTEST_FLAGS = ["-m", "not e2e", "--no-cov", "-q", "--no-header"]


def sha256(path: Path) -> str:
    """Fingerprint used to prove each mutant was reverted byte-exactly."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_tests(target: str) -> tuple[int, str]:
    """Run one test file with the marker filter that keeps e2e out of the process."""
    cmd = [sys.executable, "-m", "pytest", target, *PYTEST_FLAGS]
    proc = subprocess.run(  # noqa: S603  # nosec B603
        cmd, cwd=REPO, capture_output=True, text=True, check=False
    )
    tail = [line for line in proc.stdout.splitlines() if line.strip()]
    return proc.returncode, tail[-1] if tail else "(no output)"


def version_of(name: str) -> str:
    """Package pin as installed here, or a marker when the package is absent."""
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "缺失"


def main() -> int:
    """Print the proof log: header, baseline, one section per mutant, restore check."""
    print("# C38b 变异巡回（mutation proof）")
    print(f"# 生成时间: {datetime.now().astimezone().isoformat(timespec='seconds')}")
    print(f"# python={platform.python_version()} 机器={platform.machine()}")
    for name in (
        "coverage",
        "loguru",
        "numpy",
        "pandas",
        "pandas-datareader",
        "pytest",
        "pytest-asyncio",
        "pytest-benchmark",
        "pytest-cov",
        "pytest-picked",
        "pytest-sugar",
        "pytest-timeout",
        "pytest-xdist",
        "SQLAlchemy",
        "SQLAlchemy-Utils",
    ):
        print(f"# {name}=={version_of(name)}")
    for name, cmd in (
        ("分支", ["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        ("HEAD", ["git", "rev-parse", "--short", "HEAD"]),
    ):
        out = subprocess.run(  # noqa: S603  # nosec B603
            cmd, cwd=REPO, capture_output=True, text=True, check=False
        )
        print(f"# {name}: {out.stdout.strip()}")
    print(f"# pytest 命令（每次调用固定）: python -m pytest <test> {' '.join(PYTEST_FLAGS)}")
    print('# 说明：`-m "not e2e"` 是必须的 —— 每个测试文件都同时挂着单元面和真库 e2e 面，')
    print("# 少了这个标记就会对生产 MySQL 执行 e2e 类（本轮上一次运行即因此踩坑，见 README）。")
    files = sorted({f for f, *_ in MUTATIONS})
    baseline = {f: sha256(REPO / f) for f in files}
    print("# 被测源文件 sha256（变异前基线，还原后逐字节复核）")
    for f, digest in baseline.items():
        print(f"#   {digest}  {f}")
    print()
    print("## [0] 基线：五个测试文件全绿")
    unexpected = 0
    for target in TESTS:
        rc, summary = run_tests(target)
        print(f"  {target:<36} rc={rc} {summary}")
        if rc != 0:
            unexpected += 1
    print()
    print("## [1..N] 逐个变异：期望红")
    for index, (rel, old, new, label, target) in enumerate(MUTATIONS, start=1):
        path = REPO / rel
        original = path.read_text(encoding="utf-8")
        print(f"[{index}] {label}")
        print(f"    注入: {rel} → {target}")
        if original.count(old) != 1:
            print(f"    结果: 锚点命中 {original.count(old)} 次，注入未执行 ⇒ BROKEN")
            unexpected += 1
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        for name, block in (("删", old), ("增", new)):
            for line in block.splitlines():
                if line.strip():
                    print(f"    {name}: {line.strip()}")
        try:
            rc, summary = run_tests(target)
        finally:
            path.write_text(original, encoding="utf-8")
        verdict = "RED（判据咬住了）" if rc != 0 else "GREEN ⇒ 恒真断言，判据没咬住"
        if rc == 0:
            unexpected += 1
        print(f"    结果: rc={rc} {summary} ⇒ {verdict}")
        restored = "一致" if sha256(path) == baseline[rel] else "不一致 ⇒ 立即停"
        print(f"    还原: sha256 复核 {restored}")
        if restored != "一致":
            return 2
    print()
    print("## [终态] 全部文件回到基线？")
    for rel in files:
        print(f"  {'一致' if sha256(REPO / rel) == baseline[rel] else '不一致'}  {rel}")
    print()
    print(f"MUTATION_SUMMARY cases={len(MUTATIONS)} unexpected={unexpected}")
    return 0 if unexpected == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
