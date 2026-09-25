"""Do the C24 guards actually bite, or would they pass on a broken build?

A guard test that passes on correct code proves nothing on its own: the usual
failure mode is an assertion that quietly holds for the wrong reason. This
harness puts each fixed defect back - one string edit at a time, reverted
immediately by exact content (never by ``git checkout``, because the same files
carry this round's uncommitted work) - and requires the *named* test to fail.
A mutation that leaves every test green means the guard does not cover what it
claims to, and is reported as ``NOT-CAUGHT`` with a non-zero exit.

Six mutations, one per defect this round removed or pinned:

* ``stock_action``'s akshare leg spelled its period ``event``, so the authority
  row ranked a fallback ``resolve()`` could never take;
* ``index_constituent``'s akshare leg flipped to ``verified`` with no cross-vendor
  reading behind it;
* ``authority.json`` reordered to hide that ``fund_etf_daily``'s first entry is
  not auto-eligible;
* ``resolve()`` ignoring the health flags;
* the sina normalize layer not cropping to the caller's window;
* ``within_window``'s upper bound dropped.

Usage (py313 env):
    python docs/evidence/C24/guard_mutation_proof.py
"""

from __future__ import annotations

import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REPO_RELATIVE = Path(__file__).resolve().relative_to(ROOT)

DEGRADATION = "tests/test_fallback_degradation.py"
WINDOW = "tests/test_sina_window_fidelity.py"


@dataclass(frozen=True)
class Mutation:
    """One defect to put back and the tests that must notice.

    Attributes:
        label: What defect is being reintroduced.
        path: File to edit, relative to the repo root.
        old: The exact text to replace; must appear exactly once.
        new: The defective text.
        suite: Test file to run while the mutation is in place.
        must_fail: Node id suffixes that have to fail for the guard to be real.
    """

    label: str
    path: str
    old: str
    new: str
    suite: str
    must_fail: tuple[str, ...]


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "stock_action 的 akshare 腿 period 退回 event（同行两腿不再同形）",
        "opendata/data/providers/akshare/models/stock_action.py",
        'period="1D",',
        'period="event",',
        DEGRADATION,
        (
            "TestRankedLegsCanCompete::test_only_the_documented_row_ranks_legs_that_never_meet",
            "TestRankedLegsCanCompete::test_stock_action_legs_share_the_verified_legs_shape",
        ),
    ),
    Mutation(
        "index_constituent 的 akshare 腿无跨 vendor 读数即转正",
        "opendata/data/providers/akshare/models/index_constituent.py",
        "verified=False",
        "verified=True",
        DEGRADATION,
        (
            "TestDegradationCoverageIsMeasured::test_degradable_shapes_are_exactly_the_measured_ones",
            "TestDegradationCoverageIsMeasured::test_no_domestic_row_can_degrade_today",
        ),
    ),
    Mutation(
        "authority.json 把 fund_etf_daily 改成 ths 优先（掩盖未转正的行首）",
        "opendata/data/authority.json",
        '"fund_etf_daily": ["akshare", "ths"]',
        '"fund_etf_daily": ["ths", "akshare"]',
        DEGRADATION,
        (
            "TestRowOrderIsTheOrderAutoUses::test_rows_with_an_eligible_leg_start_with_an_eligible_leg",
        ),
    ),
    Mutation(
        "resolve() 绕过健康位（摘掉腿也不换下一条）",
        "opendata/data/registry.py",
        "if self._healthy.get(key, True):",
        "if True:",
        DEGRADATION,
        (
            "TestDegradationIsTaken::test_each_ranked_leg_loses_to_the_next_one_in_its_row",
            "TestDegradationIsTaken::test_the_last_leg_failing_leaves_a_lookup_miss_rather_than_a_guess",
            "TestDegradationIsTaken::test_health_flags_are_restored_after_each_chain",
        ),
    ),
    Mutation(
        "sina 期货日线 normalize 不再按窗口裁剪（退路多答）",
        "opendata/data/providers/akshare/models/futures_daily.py",
        "            if not within_window(trade_date, params.start_date, params.end_date):\n"
        "                continue",
        "            if False:\n                continue",
        WINDOW,
        ("test_a_named_window_crops_the_whole_history[futures_daily]",),
    ),
    Mutation(
        "within_window 的上界失效（只裁下界）",
        "opendata/data/providers/akshare/models/_normalize.py",
        "return not (end is not None and day > end)",
        "return True",
        WINDOW,
        (
            "test_a_named_window_crops_the_whole_history[futures_daily]",
            "test_a_named_window_crops_the_whole_history[stock_action]",
            "test_one_sided_windows_bound_only_the_side_given",
        ),
    ),
)


def _patched(path: str, old: str, new: str) -> str | None:
    """Apply one mutation, returning the original file text.

    Args:
        path: File to edit, relative to the repo root.
        old: Text to replace; the edit is skipped unless it is unique.
        new: The defective text.

    Returns:
        The pre-mutation content, or ``None`` when the target was ambiguous.
    """
    target = ROOT / path
    original = target.read_text(encoding="utf-8")
    if original.count(old) != 1:
        print(f"  !! 目标串出现 {original.count(old)} 次（需要恰好 1 次），跳过本条")
        return None
    target.write_text(original.replace(old, new), encoding="utf-8")
    return original


def _restore(path: str, original: str) -> None:
    """Write a file back and confirm the mutation is gone."""
    target = ROOT / path
    target.write_text(original, encoding="utf-8")
    if target.read_text(encoding="utf-8") != original:
        print(f"  !! 回写后内容不一致：{path} 需要人工核对")


def _pytest_result(node_ids: tuple[str, ...]) -> tuple[int, str]:
    """Run pytest over explicit node ids.

    Args:
        node_ids: Fully qualified test selectors.

    Returns:
        The process exit code and the tail of its report.
    """
    completed = subprocess.run(  # literal argv, this interpreter  # noqa: S603  # nosec B603
        [sys.executable, "-m", "pytest", *node_ids, "-q", "--no-cov", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    tail = [line for line in completed.stdout.splitlines() if line.strip()][-4:]
    return completed.returncode, " / ".join(tail)


def run_mutation(mutation: Mutation) -> tuple[bool, list[str]]:
    """Put one defect back and check that the named guards fail.

    Args:
        mutation: The defect and the tests that must notice it.

    Returns:
        ``(caught, problems)`` - ``caught`` is True only when every named test
        failed while the suite's other tests stayed green.
    """
    original = _patched(mutation.path, mutation.old, mutation.new)
    if original is None:
        return False, ["变异未施加（目标串不唯一）"]

    problems: list[str] = []
    for suffix in mutation.must_fail:
        code, detail = _pytest_result((f"{mutation.suite}::{suffix}",))
        print(f"  期望失败 {suffix} -> exit={code}")
        if code == 0:
            problems.append(f"{suffix} 在缺陷回灌后仍然通过：守卫没有覆盖它")
        print(f"      {detail}")

    code, detail = _pytest_result((mutation.suite,))
    print(f"  整文件复跑 {mutation.suite} -> exit={code}（{detail}）")
    _restore(mutation.path, original)
    restored_code, restored = _pytest_result((mutation.suite,))
    print(f"  回写后复跑 -> exit={restored_code}（{restored}）")
    if restored_code != 0:
        problems.append("回写后用例未恢复绿色，需要人工核对工作树")
    return not problems, problems


def main() -> int:
    """Walk every mutation and report whether its guard bit.

    Returns:
        ``0`` when all guards bit, ``1`` when any did not.
    """
    print("===== C24 守卫变异证明（改回缺陷 -> 期望具名用例失败 -> 精确回写）=====")
    print(f"cwd={ROOT}  python={sys.version.split()[0]}  复现命令=python {REPO_RELATIVE}\n")

    uncaught: list[str] = []
    for index, mutation in enumerate(MUTATIONS, start=1):
        print(f"##### M{index} {mutation.label}")
        print(f"  文件={mutation.path}")
        caught, problems = run_mutation(mutation)
        print(f"  判定={'CAUGHT' if caught else 'NOT-CAUGHT'}")
        for problem in problems:
            print(f"    - {problem}")
            uncaught.append(f"M{index}: {problem}")
        print()

    print("===== 汇总 =====")
    print(f"  变异 {len(MUTATIONS)} 条，守卫全部命中 = {not uncaught}")
    for line in uncaught:
        print(f"  未命中 {line}")
    return 1 if uncaught else 0


if __name__ == "__main__":
    sys.exit(main())
