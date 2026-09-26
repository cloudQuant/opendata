#!/usr/bin/env python3
r"""Prove the C31 endpoint guard still has teeth against the real working tree.

``tests/test_frontend_endpoint_contract.py`` carries 8 falsifiability cases built on
planted dictionaries; those prove the classifier recognises a shape. This script proves
something narrower and easier to lose: that the same guard, run against *this* tree, goes
red when this tree is broken in four specific ways.

  M1 把本轮删掉的假端点 ``PATCH /tasks/${taskId}/toggle`` 回填进 ``api/tasks.ts`` → NOT_REGISTERED
  M2 新增一个用 ``http.get()`` 写的 api 模块（extractor 只认 ``request``）→ NO_CALLS_READ
  M3 把 axios 的 ``baseURL`` 漂成 ``/api/v0``（后端从未挂过这个前缀）→ 前缀挂载例红
  M4 把 ``api/scripts.ts`` 的 5 个 ``url:`` 键改成 ``path:``（9 条里 5 条隐形）→ PLANE_SHRANK

Each mutation is its own round: snapshot sha256 → mutate → run the guard (complete
output) → restore → compare sha256. M4 是第一遍唯一逃掉的形状，补上 ``MIN_CALLS_PER_FILE``
之后由 PLANE_SHRANK 抓到；第一遍的原始输出留在同名 ``.txt`` 档案里，没有改写。
"""

from __future__ import annotations

import hashlib
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

REPO = Path(__file__).resolve().parents[3]
GUARD = "tests/test_frontend_endpoint_contract.py"
TASKS = REPO / "frontend/src/api/tasks.ts"
SCRIPTS = REPO / "frontend/src/api/scripts.ts"
REQUEST = REPO / "frontend/src/utils/request.ts"
QUIET = REPO / "frontend/src/api/quiet.ts"
WATCHED = (TASKS, SCRIPTS, REQUEST)

PLANTED_TOGGLE = """
  // planted by docs/evidence/C31/endpoint-guard-mutation.py (M1)
  toggle(taskId: number): Promise<unknown> {
    return request({ url: `/tasks/${taskId}/toggle`, method: 'PATCH' })
  }
"""

QUIET_MODULE = """// planted by docs/evidence/C31/endpoint-guard-mutation.py (M2)
import http from '@/utils/request'

export const quietApi = {
  list: () => http.get('/tasks/'),
}
"""


def sha256(path: Path) -> str:
    """Short digest of a file, used to prove the tree came back unchanged."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def run_guard(label: str) -> int:
    """Run the guard and echo its complete output; return pytest's exit code."""
    print(f"\n--- 跑守卫：{label} ---")
    result = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-m", "pytest", GUARD, "-q", "-p", "no:cacheprovider", "--no-cov"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="")
    print(f"{label}: GUARD_RC={result.returncode}")
    return result.returncode


def replace_in(path: Path, old: str, new: str) -> None:
    """Swap every occurrence of ``old`` in a real tree file, and say how many."""
    text = path.read_text(encoding="utf-8")
    if old not in text:
        message = f"{path.name} 里找不到 {old!r}"
        raise RuntimeError(message)
    path.write_text(text.replace(old, new), encoding="utf-8")
    print(f"已在 {path.name} 里把 {old!r} 换成 {new!r}（{text.count(old)} 处）")


def restore(originals: dict[Path, str]) -> None:
    """Write every watched file back and drop the planted module."""
    for path, text in originals.items():
        path.write_text(text, encoding="utf-8")
    QUIET.unlink(missing_ok=True)


def plant_m1() -> None:
    """M1: append the fake endpoint this round deleted."""
    TASKS.write_text(TASKS.read_text(encoding="utf-8") + PLANTED_TOGGLE, encoding="utf-8")
    print("已把 PATCH /tasks/${taskId}/toggle 追加到 api/tasks.ts（本轮删掉的那条假端点原样回填）")


def plant_m2() -> None:
    """M2: add a module whose single call uses a client name the extractor does not know."""
    QUIET.write_text(QUIET_MODULE, encoding="utf-8")
    print("已写入 frontend/src/api/quiet.ts：一条 http.get('/tasks/')，client 名不在词汇表里")


def plant_m3() -> None:
    """M3: drift the prefix the browser prepends."""
    replace_in(REQUEST, "baseURL: '/api/v1'", "baseURL: '/api/v0'")


def plant_m4() -> None:
    """M4: make part of one module invisible to the extractor."""
    replace_in(SCRIPTS, "url: '", "path: '")


def main() -> int:
    """Run the four rounds and report which ones the guard caught."""
    originals = {path: path.read_text(encoding="utf-8") for path in WATCHED}
    baseline = {path: sha256(path) for path in originals}
    print("baseline sha256: " + ", ".join(f"{p.name}={h}" for p, h in baseline.items()))

    if run_guard("基线（未改坏）") != 0:
        print("基线就是红的，本脚本的前提不成立")
        return 2

    plans: tuple[tuple[str, Callable[[], None]], ...] = (
        ("M1", plant_m1),
        ("M2", plant_m2),
        ("M3", plant_m3),
        ("M4", plant_m4),
    )
    outcomes: list[tuple[str, int]] = []
    for label, apply in plans:
        print(f"\n===== {label} =====")
        try:
            apply()
            outcomes.append((label, run_guard(label)))
        finally:
            restore(originals)
            for path, digest in baseline.items():
                state = "RESTORED_OK" if sha256(path) == digest else "RESTORE_FAILED"
                print(f"还原校验 {path.name}: {sha256(path)} vs {digest} → {state}")
            print(f"quiet.ts 是否残留: {QUIET.exists()}")

    outcomes.append(("还原后复跑", run_guard("还原后复跑")))

    caught = [label for label, rc in outcomes if rc != 0]
    after_restore = dict(outcomes)["还原后复跑"]
    print(f"\n读数：4 种改坏里 {len(caught)} 种被抓到 → {caught}")
    print(f"读数：还原后复跑 GUARD_RC={after_restore}（0 才算牙没把树咬坏）")
    ok = len(caught) == 4 and after_restore == 0
    verdict = "0（4 种改坏全被抓到，且还原后回绿）" if ok else "2（有改坏没被抓到，或还原没回绿）"
    print(f"MUTATION_RC={verdict}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
