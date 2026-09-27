"""C52 counterfact pass for the dwd merge faces (AC-9|06 / AC-9|07).

The two cells this round flipped to ``proven`` rest on four service-level judgement
faces (authority fallback counted, ``source`` kept, ``_diff_flag`` decided, ``_as_of``
stamped at the window end) and on a five-segment propagation chain. A reading that
could not go red is not a judgement face, so each mutation below is a single-point
edit to ``opendata/pipeline/dwd_merge.py`` that must turn the corresponding test node
*and* the corresponding item probe red; the file is then restored byte-for-byte in a
``finally`` block, and the run refuses to touch an anchor that does not appear exactly
once - an ambiguous anchor could rewrite something the round never meant to change.

Run it from the repository root::

    python docs/evidence/C52/falsify_dwd_merge.py

Nothing here writes to a warehouse: the probes select ``-m "not e2e"`` legs only.
"""

from __future__ import annotations

import contextlib
import io
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path

# docs/evidence/C52/<this file> -> the repository root is three levels up.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "quality"))

from acceptance_item_probe import main as probe_main

REPO_ROOT = Path(__file__).resolve().parents[3]
TARGET = "opendata/pipeline/dwd_merge.py"
PYTEST_ARGV = ("-p", "no:cacheprovider", "--no-cov", "-m", "not e2e")

FOUR_FACES = (
    "tests/test_dwd_merge.py::TestDwdMergeService::test_service_writes_all_four_point_in_time_faces"
)
DEGRADE_EVIDENCE = (
    "tests/test_dwd_merge.py::TestDwdMergeService::"
    "test_service_degrades_per_key_and_keeps_the_source_evidence"
)
REVISION_VALUE = (
    "tests/test_dwd_merge.py::TestDwdMergeService::"
    "test_revision_of_an_existing_key_changes_the_dwd_row"
)


@dataclass(frozen=True)
class Mutation:
    """One single-point edit, the nodes it must turn red, and the item it must flip.

    Attributes:
        label: What the mutation claims to break.
        needle: Anchor text; the run stops unless it occurs exactly once.
        replacement: Text that takes its place (``""`` deletes the line).
        nodes: Test node ids expected to fail.
        item: ``AC-N|NN`` whose probe must read a gap while the edit stands.
    """

    label: str
    needle: str
    replacement: str
    nodes: tuple[str, ...]
    item: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "M1 _as_of 写的不是窗口末（as_of=end -> as_of=start）",
        "as_of=end,",
        "as_of=start,",
        (FOUR_FACES,),
        "AC-9|06",
    ),
    Mutation(
        "M2 权威缺失不再计数（if rank > 0 -> if rank < 0）",
        "if rank > 0:",
        "if rank < 0:",
        (DEGRADE_EVIDENCE,),
        "AC-9|06",
    ),
    Mutation(
        "M3 不再写 _as_of（删掉那一行盖章）",
        '            record["_as_of"] = as_of\n',
        "",
        (FOUR_FACES,),
        "AC-9|06",
    ),
    Mutation(
        "M4 affected_keys 不再下传 reader（窗口外的修订取不到数）",
        "self._reader(source)(start, end, set(affected_keys))",
        "self._reader(source)(start, end, set())",
        (REVISION_VALUE,),
        "AC-9|07",
    ),
)


def run_pytest(nodes: tuple[str, ...]) -> tuple[int, str]:
    """Run the selected nodes in a fresh interpreter and return their exit code and output.

    In-process ``pytest.main`` was tried first and lied: the mutated module stays cached in
    ``sys.modules`` after the first leg, so every later mutation was measured against the
    previous leg's bytecode.
    """
    proc = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
        [sys.executable, "-m", "pytest", *PYTEST_ARGV, *nodes],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return int(proc.returncode), proc.stdout + proc.stderr


def run_probe(item: str) -> tuple[str, str]:
    """Re-measure one item while the mutation stands and return its verdict state.

    ``--item`` exits 0 whatever the verdict says (only a wording-drift moves the exit
    code), so the reading that matters is the ``VERDICT`` line itself.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        probe_main(["--item", item])
    output = buffer.getvalue()
    prefix = f"VERDICT {item}:"
    states = [
        line.removeprefix(prefix).strip() for line in output.splitlines() if line.startswith(prefix)
    ]
    verdict = states[0].split(" —")[0] if len(states) == 1 else "unreadable"
    return verdict, output


def main() -> int:
    """Apply, measure, restore every mutation; report what refused to go red."""
    path = REPO_ROOT / TARGET
    if not path.is_file():
        print(f"FAIL: 读不到 {TARGET}（REPO_ROOT 解析为 {REPO_ROOT}）")
        return 2
    original = path.read_bytes()
    failures: list[str] = []
    for mutation in MUTATIONS:
        text = original.decode("utf-8")
        print(f"\n===== {mutation.label} =====")
        if text.count(mutation.needle) != 1:
            failures.append(
                f"{mutation.label}: 锚点出现 {text.count(mutation.needle)} 次，不是 1 次"
            )
            print(f"SKIP: {failures[-1]}（不改写，避免误伤）")
            continue
        try:
            path.write_text(
                text.replace(mutation.needle, mutation.replacement, 1), encoding="utf-8"
            )
            code, output = run_pytest(mutation.nodes)
            print(f"--- pytest exit={code}（期望非 0）---")
            print(output.rstrip())
            if code == 0:
                failures.append(f"{mutation.label}: 节点仍然通过，判定面不咬")
            state, probe_output = run_probe(mutation.item)
            print(f"--- probe {mutation.item} verdict={state}（期望 gap）---")
            print(probe_output.rstrip())
            if state != "gap":
                failures.append(f"{mutation.label}: 探针现在判 {state}，判定面不咬")
        finally:
            path.write_bytes(original)
            restored = path.read_bytes() == original
            print(f"--- 还原后与改写前逐字节相同 = {restored}")
            if not restored:
                failures.append(f"{mutation.label}: 文件没有还原")
    untouched = path.read_bytes() == original
    print("\n===== 收尾 =====")
    print(f"{TARGET} 逐字节回到起点: {untouched}")
    if failures:
        print("FAIL: 反证遍没把该红的都扳红:")
        for line in failures:
            print(f"  {line}")
        return 1
    print(f"OK: {len(MUTATIONS)} 处单点改写逐处把对应节点与条目级探针扳红，文件逐字节还原。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
