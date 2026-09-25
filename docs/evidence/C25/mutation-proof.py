r"""C25 判据的反向验证：把五个已知缺陷逐个装回 ``patrol``，看具名用例是否变红.

验收文档要的是「这条判据真的在守 something」。canary 的五条不变量各自都有一个
写得出来、但当时会被测试漏掉的错法；本脚本把每个错法装回 ``opendata/pipeline/patrol.py``
（唯一的动作是字符串替换），跑它对应的那一条具名用例，要求它**红**，然后按字节还原并用
sha256 验证还原无误。五个缺陷：

- D1 「tolerance 先判、hollow 后判」：≤20 行的小页会被读成满值。
- D2 「字段塌陷顺手把源标成不可达」：C19 量过的那两面会天天摘掉九条健康腿。
- D3 「canary 读页算进探针耗时」：跨轮 ``latency_ms`` 对比的含义被换掉。
- D4 「读不到那一页也算偏差」：一次网络抖动变成一条字段塌陷告警。
- D5 「空页量成 hollow」：0 行的页已经被行数探针判死了，再对一列从没看过的数据下结论。

每一段替换都要求锚点在文件里**只出现一次**；锚点没命中（代码变了）直接报错退出，
而不是把「未安装」的变异读成「装了也没红」。

运行（输出重定向到同名 ``.txt``）::

    python docs/evidence/C25/mutation-proof.py
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
TARGET = REPO / "opendata" / "pipeline" / "patrol.py"
SUITE = "tests/test_patrol.py"


@dataclass(frozen=True)
class Defect:
    """One wrong-but-plausible implementation and the test that owns it.

    Attributes:
        label: What the defect is, printed in the archive.
        old: The exact production snippet the mutation replaces.
        new: The wrong version, installed for one test run.
        test: Node id of the named test that must go red.
    """

    label: str
    old: str
    new: str
    test: str


DEFECTS: tuple[Defect, ...] = (
    Defect(
        label="D1 tolerance 先判、hollow 后判（小页全空被读成满值）",
        old="""    if missing >= rows:
        return SHAPE_HOLLOW
    return SHAPE_FULL if missing <= tolerance else SHAPE_PARTIAL""",
        new="""    if missing <= tolerance:
        return SHAPE_FULL
    return SHAPE_HOLLOW if missing >= rows else SHAPE_PARTIAL""",
        test=f"{SUITE}::TestFieldCanary::test_a_small_page_cannot_be_rescued_by_the_tolerance",
    ),
    Defect(
        label="D2 字段塌陷顺手 mark_unavailable（C19 的两面之差会摘掉健康腿）",
        old="""                    f"{reading.shape} (measured: {sorted(reading.allowed)})"
                )
        results.append(""",
        new="""                    f"{reading.shape} (measured: {sorted(reading.allowed)})"
                )
        if any(reading.deviates for reading in canaries):
            registry.mark_unavailable(fetcher)
        results.append(""",
        test=(
            f"{SUITE}::TestPatrolCanaryWiring::test_a_field_collapse_never_moves_the_routing_mark"
        ),
    ),
    Defect(
        label="D3 canary 读页算进探针耗时（跨轮 latency_ms 不再是探针耗时）",
        old="""        probe_ms = (time.perf_counter() - started) * 1000
        canaries = await _run_canaries(fetcher, capability) if ok else ()""",
        new="""        canaries = await _run_canaries(fetcher, capability) if ok else ()
        probe_ms = (time.perf_counter() - started) * 1000""",
        test=f"{SUITE}::TestPatrolCanaryWiring::test_the_probe_latency_excludes_the_canary_reads",
    ),
    Defect(
        label="D4 读不到那一页也当偏差（一次抖动 = 一条字段塌陷告警）",
        old="        return self.shape != SHAPE_NO_READING and self.shape not in self.allowed",
        new="        return self.shape not in self.allowed",
        test=f"{SUITE}::TestPatrolCanaryWiring::test_a_canary_that_cannot_be_read_is_not_an_alarm",
    ),
    Defect(
        label="D5 空页量成 hollow（对从没看过的列下了结论）",
        old="""            shape=SHAPE_NO_READING,
            rows=0,
            missing=None,
            allowed=canary.allowed,
            error="canary read returned no rows",""",
        new="""            shape=SHAPE_HOLLOW,
            rows=0,
            missing=0,
            allowed=canary.allowed,
            error="canary read returned no rows",""",
        test=f"{SUITE}::TestFieldCanary::test_an_empty_page_is_unmeasurable_rather_than_hollow",
    ),
)


def _run(node_ids: Sequence[str]) -> int:
    """Run pytest on the given node ids and return its exit code.

    Args:
        node_ids: Test ids to run (relative to the repo root).

    Returns:
        The pytest exit code.
    """
    cmd = [sys.executable, "-m", "pytest", "-q", "--no-cov", "-p", "no:cacheprovider", *node_ids]
    proc = subprocess.run(  # noqa: S603  # nosec B603 固定 argv，无外部输入
        cmd, cwd=REPO, capture_output=True, text=True
    )
    tail = [line for line in proc.stdout.splitlines() if line.strip()][-3:]
    for line in tail:
        print(f"      | {line}")
    return int(proc.returncode)


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


def main() -> int:
    """Install each defect, prove its test goes red, restore, prove the suite is green.

    Returns:
        0 when every defect was caught by its named test and the file came back
        byte-identical, 1 otherwise.
    """
    original = TARGET.read_text(encoding="utf-8")
    original_digest = hashlib.sha256(original.encode()).hexdigest()
    print(f"[0] 被变异文件 sha256={original_digest}")
    verdicts: dict[str, bool] = {}
    try:
        for defect in DEFECTS:
            print(f"## {defect.label}")
            print(f"  具名用例：{defect.test}")
            TARGET.write_text(_install(defect, original), encoding="utf-8")
            mutated = _run([defect.test])
            restored = _restore(original, original_digest)
            verdicts[defect.label] = mutated != 0 and restored
            print(f"  变异下退出码={mutated}（要求非 0）；还原校验={restored}")
            print(f"  判定：{'PASS' if verdicts[defect.label] else 'FAIL'}")
    finally:
        _restore(original, original_digest)
    print(f"## 还原后整条 canary 套件（{len(DEFECTS)} 次变异之后）")
    green = _run([SUITE])
    print(f"  套件退出码={green}（要求 0）")
    print("## 判定汇总")
    for label, passed in verdicts.items():
        print(f"  {'PASS' if passed else 'FAIL'} {label}")
    print(f"  {'PASS' if green == 0 else 'FAIL'} 全部还原后 canary 套件重新转绿")
    return 0 if green == 0 and all(verdicts.values()) else 1


def _restore(original: str, expected_digest: str) -> bool:
    """Write the untouched text back and check it landed byte-identical.

    Args:
        original: The production file text captured before any mutation.
        expected_digest: sha256 of :data:`original`.

    Returns:
        True when the file on disk now hashes to that digest.
    """
    TARGET.write_text(original, encoding="utf-8")
    actual = hashlib.sha256(TARGET.read_bytes()).hexdigest()
    if actual != expected_digest:
        print(f"  还原失败：sha256={actual} != {expected_digest}")
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
