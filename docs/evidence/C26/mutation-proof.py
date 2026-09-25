r"""C26 判据的反向验证：把七个已知错法逐个装回生产代码，看具名用例是否变红.

C26 的判据每一条都至少有一个「写得出来、当时会被测试漏掉」的错法，可见化那一条有
两个方向（该说时不说 / 不该说时乱说），所以这里是七段。本脚本把每个错法装进它所在的
生产文件（唯一的动作是字符串替换），跑它对应的那一条具名用例，要求它**红**，然后按
字节还原并用 sha256 校验还原无误。七个错法：

- D1 配股价按每 10 股再 ÷10（本轮修掉的那个单位错，判据来自可对账配股事件逐条对上东方财富）。
- D2 ``配股方案`` 只认 ``"10配N"`` 写法，live 的数字单元格落回 0.0（修复前的形状）。
- D3 无除息日的预案行悄悄丢掉，一条都不说（本轮之前的行为）。
- D4 窗口裁剪与全零行也 WARNING（契约要求的例行丢弃把告警面淹掉）。
- D5 预案行拿公告日期当除息日补上（本轮明确拒绝的做法：那是伪造，不是修复）。
- D6 主腿从没发过的 ``allotment_ratio`` 列「读出」配股（文档曾经这么写，实测字段面没这一列）。
- D7 与 D3 反向：无日期行**一律**报警，连 ``进度=不分配`` 这种没有金额的行也占用
  WARNING（分级判据失效；上游整页都是这种行时告警面即失去意义）。

每段替换都要求锚点在文件里**只出现一次**；锚点没命中（代码变了）直接报错退出，
而不是把「未安装」的变异读成「装了也没红」。

运行（输出重定向到同名 ``.txt``）::

    python docs/evidence/C26/mutation-proof.py
"""

from __future__ import annotations

import hashlib
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO = Path(__file__).resolve().parents[3]
PROVIDER = REPO / "opendata" / "data" / "providers" / "akshare" / "models" / "stock_action.py"
FUYAO = REPO / "opendata_fuyao" / "endpoints.py"
P0_SUITE = "tests/test_p0_providers.py"
FUYAO_SUITE = "tests/test_fuyao_endpoints.py"


@dataclass(frozen=True)
class Defect:
    """One wrong-but-plausible implementation and the test that owns it.

    Attributes:
        label: What the defect is, printed in the archive.
        target: The production file the mutation is installed into.
        old: The exact production snippet the mutation replaces.
        new: The wrong version, installed for one test run.
        test: Node id of the named test that must go red.
    """

    label: str
    target: Path
    old: str
    new: str
    test: str


DEFECTS: tuple[Defect, ...] = (
    Defect(
        label="D1 配股价格再除以 10（每股价被当成每 10 股价）",
        target=PROVIDER,
        old='                rights_price = _price(record.get("配股价格"))',
        new='                rights_price = _per_share(record.get("配股价格"))',
        test=f"{P0_SUITE}::TestStockActionFetcher::test_maps_rights_price_as_published",
    ),
    Defect(
        label="D2 配股方案只认字符串写法，live 的 float 单元格读成 0",
        target=PROVIDER,
        old=(
            "        return float(match.group(1)) / 10 if match else 0.0\n"
            "    return _per_share(plan)"
        ),
        new=("        return float(match.group(1)) / 10 if match else 0.0\n    return 0.0"),
        test=f"{P0_SUITE}::TestStockActionFetcher::test_maps_rights_ratio_per_share",
    ),
    Defect(
        label="D3 无除息日的预案行静默丢弃（本轮之前的行为）",
        target=PROVIDER,
        old="    if undated_dividends or undated_rights:",
        new="    if False and (undated_dividends or undated_rights):",
        test=f"{P0_SUITE}::TestStockActionFetcher::test_undated_plan_drop_warns_with_the_amount",
    ),
    Defect(
        label="D4 契约要求的窗口/全零丢弃也上 WARNING（告警面被淹）",
        target=PROVIDER,
        old='    logger.debug(\n        f"stock_action {symbol}: {outside_window} row(s)',
        new='    logger.warning(\n        f"stock_action {symbol}: {outside_window} row(s)',
        test=f"{P0_SUITE}::TestStockActionFetcher::test_contract_drops_are_counted_not_warned",
    ),
    Defect(
        label="D5 预案行拿公告日期补一个除息日（伪造，不是修复）",
        target=PROVIDER,
        old="""                if ex_date is None:
                    if cash != 0.0 or stock != 0.0:
                        undated_dividends.append(f"{record.get('公告日期')}:{cash}+{stock}")
                    continue""",
        new="""                if ex_date is None:
                    ex_date = as_date(record.get("公告日期"))""",
        test=f"{P0_SUITE}::TestStockActionFetcher::test_undated_plan_drop_warns_with_the_amount",
    ),
    Defect(
        label="D6 主腿从不上报 allotment_ratio，却去读它",
        target=FUYAO,
        old='                    stock_dividend=float(row.get("per_share_bonus") or 0.0),',
        new=(
            '                    stock_dividend=float(row.get("per_share_bonus") or 0.0),\n'
            '                    rights_shares=float(row.get("allotment_ratio") or 0.0),'
        ),
        test=(f"{FUYAO_SUITE}::TestNormalizers::test_adjustment_events_carry_no_rights_columns"),
    ),
    Defect(
        label="D7 无日期行一律报警（分级判据失效：不分配/空白预案也占用 WARNING）",
        target=PROVIDER,
        old="    if undated_dividends or undated_rights:",
        new="    if True:",
        test=f"{P0_SUITE}::TestStockActionFetcher::test_undated_rows_without_economics_stay_silent",
    ),
)


def _collect(node_ids: Sequence[str]) -> tuple[int, str]:
    """Ask pytest which node ids it can actually resolve.

    Args:
        node_ids: Test ids to collect (relative to the repo root).

    Returns:
        The exit code and the collected node ids, one per line. ``-qq`` is
        required, not stylistic: ``pytest.ini`` adds ``-v``, so a single ``-q``
        cancels back to normal verbosity and ``--collect-only`` then prints an
        object tree with no node id in it - every id would read as missing.
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-qq",
        "--no-cov",
        "-p",
        "no:cacheprovider",
        "--collect-only",
        *node_ids,
    ]
    proc = subprocess.run(  # noqa: S603  # nosec B603 固定 argv，无外部输入
        cmd, cwd=REPO, capture_output=True, text=True
    )
    return int(proc.returncode), proc.stdout


def _preflight() -> bool:
    """Prove every named test exists before using its red-ness as evidence.

    A typo in a node id makes pytest run nothing, which would read exactly like
    "the mutation was caught". Collecting first turns that failure mode into an
    aborted run, and matching whole lines keeps a substring (a file path that
    happens to appear in the report) from passing for a resolved test.

    Returns:
        True when every defect's test collects.
    """
    code, output = _collect([defect.test for defect in DEFECTS])
    collected = {line.strip() for line in output.splitlines()}
    missing = [defect.test for defect in DEFECTS if defect.test not in collected]
    if code != 0 or missing:
        print(f"  预检失败：collect 退出码={code}，取不到的 node id={missing}")
        return False
    unique = {defect.test for defect in DEFECTS}
    print(f"[0] 预检：{len(DEFECTS)} 段变异对应 {len(unique)} 个具名 node id，全部逐行可 collect")
    return True


def _run(node_ids: Sequence[str]) -> tuple[int, str]:
    """Run pytest on the given node ids.

    Args:
        node_ids: Test ids to run (relative to the repo root).

    Returns:
        The exit code and pytest's whole report, so the caller can look for a
        verdict anywhere in it (which test ran, and whether it failed). The last
        non-blank lines are echoed here to keep the archive readable.
    """
    cmd = [sys.executable, "-m", "pytest", "-q", "--no-cov", "-p", "no:cacheprovider", *node_ids]
    proc = subprocess.run(  # noqa: S603  # nosec B603 固定 argv，无外部输入
        cmd, cwd=REPO, capture_output=True, text=True
    )
    for line in [line for line in proc.stdout.splitlines() if line.strip()][-3:]:
        print(f"      | {line}")
    return int(proc.returncode), proc.stdout


def _install(defect: Defect, source: str) -> str:
    """Apply one mutation to the source text.

    Args:
        defect: The mutation to install.
        source: The current production file text.

    Returns:
        The mutated text.

    Raises:
        RuntimeError: The anchor is missing or ambiguous, which means the file
            no longer looks like the code this proof was written against.
    """
    hits = source.count(defect.old)
    if hits != 1:
        raise RuntimeError(f"anchor hit {hits} times for {defect.test}; expected exactly 1")
    return source.replace(defect.old, defect.new, 1)


def _restore(target: Path, original: str, expected_digest: str) -> bool:
    """Write the untouched text back and check it landed byte-identical.

    Args:
        target: The file to restore.
        original: The text captured before any mutation.
        expected_digest: sha256 of :data:`original`.

    Returns:
        True when the file on disk now hashes to that digest.
    """
    target.write_text(original, encoding="utf-8")
    actual = hashlib.sha256(target.read_bytes()).hexdigest()
    if actual != expected_digest:
        print(f"  还原失败：sha256={actual} != {expected_digest}")
        return False
    return True


def main() -> int:
    """Install each defect, prove its test goes red, restore, prove the suites are green.

    Returns:
        0 when every defect was caught by its named test and both files came
        back byte-identical, 1 otherwise.
    """
    originals = {path: path.read_text(encoding="utf-8") for path in {d.target for d in DEFECTS}}
    digests = {path: hashlib.sha256(text.encode()).hexdigest() for path, text in originals.items()}
    for path, digest in digests.items():
        print(f"[0] 被变异文件 {path.relative_to(REPO)} sha256={digest}")
    if not _preflight():
        return 1
    verdicts: dict[str, bool] = {}
    try:
        for defect in DEFECTS:
            print(f"## {defect.label}")
            print(f"  具名用例：{defect.test}")
            defect.target.write_text(_install(defect, originals[defect.target]), encoding="utf-8")
            mutated, report = _run([defect.test])
            restored = _restore(defect.target, originals[defect.target], digests[defect.target])
            # 「跑红了」必须是这一条用例红了：收集失败/内部错误也是非 0 退出码。
            caught = mutated != 0 and "1 failed" in report
            verdicts[defect.label] = caught and restored
            print(f"  变异下退出码={mutated}（要求非 0 且报告含 1 failed）；还原校验={restored}")
            print(f"  判定：{'PASS' if verdicts[defect.label] else 'FAIL'}")
    finally:
        for path, text in originals.items():
            _restore(path, text, digests[path])
    print(f"## 还原后两套具名套件（{len(DEFECTS)} 次变异之后）")
    p0_code, _ = _run([P0_SUITE])
    fuyao_code, _ = _run([FUYAO_SUITE])
    green = max(p0_code, fuyao_code)
    print(f"  套件退出码={green}（要求 0）")
    print("## 判定汇总")
    for label, passed in verdicts.items():
        print(f"  {'PASS' if passed else 'FAIL'} {label}")
    print(f"  {'PASS' if green == 0 else 'FAIL'} 全部还原后两套具名套件重新转绿")
    return 0 if green == 0 and all(verdicts.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
