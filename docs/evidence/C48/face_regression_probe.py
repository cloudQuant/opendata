"""Reproduce the C48 finding: judge faces that read the codebase through its line shape.

Three faces were blind before being fixed, and all three went blind for a reason worth keeping:

1. ``EXECUTABLE_KINDS`` was matched by a regex assuming ``frozenset({`` sits on one line.
   Adding ``TemplateKind.FULL_CHECK`` pushed the literal past the line-length limit, the
   formatter wrapped it, and *both* items reading that face went to ``no`` - including
   AC-13|07, which was already ``proven``. The shipped face reads the AST instead
   (``set_literal_members``), so no amount of rewrapping can hide a member.
2/3. ``AlertPolicy.decide`` and ``CrossCheckService._notify`` were read with
   ``function_body``, which walks module-level definitions only. A method is not module
   level, so both bodies read as empty strings and every flag inside them read ``no``.
   The shipped face reads them with ``method_body``.

Run: ``PYTHONPATH=. python docs/evidence/C48/face_regression_probe.py``
"""

from __future__ import annotations

import re
from pathlib import Path

JOBS = "opendata/pipeline/jobs.py"
ALERTS = "opendata/pipeline/alerts.py"
SERVICE = "opendata/pipeline/cross_check_service.py"

# 探针改前那两条正则，逐字保留（含它们的缺陷：\\(\\{ 要求同行）。
OLD_FRESHNESS = r"EXECUTABLE_KINDS\s*=\s*frozenset\(\{[^}]*FRESHNESS"
OLD_FULL_CHECK = r"EXECUTABLE_KINDS\s*=\s*frozenset\(\{[^}]*FULL_CHECK"


def surface() -> None:
    """Print the three readings: what the old faces saw next to what the new ones see."""
    jobs = Path(JOBS).read_text(encoding="utf-8")
    lines = jobs.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("EXECUTABLE_KINDS"))
    print(f"--- {JOBS} 实况（第 {start + 1} 行起）---")
    for i in range(start, start + 3):
        print(f"  {i + 1}| {lines[i]}")
    print()
    print("--- 改前正则面：同一条源码，\\(\\{ 的同行假设 ----")
    old_faces = (("FRESHNESS(AC-13|07)", OLD_FRESHNESS), ("FULL_CHECK(AC-9|02)", OLD_FULL_CHECK))
    for label, pat in old_faces:
        print(f"  {label:22} 匹配={re.search(pat, jobs) is not None}")
    print()

    # 延迟导入：这份档案只在复现时读探针助手，不在模块导入期依赖工作树。
    from scripts.quality.acceptance_item_probe import (
        function_body,
        method_body,
        set_literal_members,
    )

    alerts = Path(ALERTS).read_text(encoding="utf-8")
    service = Path(SERVICE).read_text(encoding="utf-8")
    print("--- 模块级读取 vs 方法读取：长度 0 = 判据里每个 flag 都读 no ---")
    reads = [
        ("function_body(alerts, 'decide')", len(function_body(alerts, "decide"))),
        (
            "method_body(alerts, 'AlertPolicy', 'decide')",
            len(method_body(alerts, "AlertPolicy", "decide")),
        ),
        ("function_body(service, '_notify')", len(function_body(service, "_notify"))),
        (
            "method_body(service, 'CrossCheckService', '_notify')",
            len(method_body(service, "CrossCheckService", "_notify")),
        ),
    ]
    for label, length in reads:
        print(f"  {label:53} 长度={length}")
    print()
    print("--- 收口后的 kind 面：AST 直读成员，行形无关 ---")
    members = set_literal_members(jobs, "EXECUTABLE_KINDS")
    print(f"  set_literal_members(jobs, 'EXECUTABLE_KINDS') = {members}")


surface()
