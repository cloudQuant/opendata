"""``golden_millis.py`` 自己是不是空壳：把每一条判据面各破一次，看它会不会红.

复算脚本永远绿就没有信息量（AC-17|08 抽审要防的正是这个）。这里以 ``importlib`` 把复算
脚本载入为独立模块（每例一份新命名空间），猴补丁篡改输入后要求「这一处坏了 ⇒ 非零退出」：

1. 表里某个值被改动一格；
2. 录制档案里某个锚点被改动一格；
3. 声明的线上锚点在档案里查不到；
4. 固定偏移推导写错（+7 小时）；
5. tzdata 口径漂移（上海被换成 UTC）。

用法::

    python docs/evidence/C44/golden_millis_counterfactuals.py
"""

from __future__ import annotations

import datetime as dt
import io
import sys
from contextlib import redirect_stdout
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Callable

HERE: Final = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(REPO_ROOT))


class Verifier(Protocol):
    """``golden_millis.py`` 载入后暴露的判定面——反事实用例改的就是这些属性.

    载入是动态的（``importlib``），mypy 看不见模块属性，所以把面写在这里；具体属性
    名由 :data:`FACES` 在运行时逐个 ``hasattr`` 兜住。复算脚本改了名字而这里没跟上时，
    用例会当场崩掉，而不是对着一个不存在的属性悄悄跳过。
    """

    MILLIS_BY_DAY: dict[str, int]
    WIRE_ANCHOR_MILLIS: dict[str, int]
    FIXED_OFFSET: dt.timezone
    SHANGHAI: ZoneInfo
    recorded_wire_anchors: Callable[[], list[tuple[str, str, dt.date, int]]]
    main: Callable[[], int]


#: 判定面清单：缺一个就报错，而不是带着一棵对不上号的树跑绿。
FACES: Final = (
    "MILLIS_BY_DAY",
    "WIRE_ANCHOR_MILLIS",
    "FIXED_OFFSET",
    "SHANGHAI",
    "recorded_wire_anchors",
    "main",
)


def load_verifier() -> Verifier:
    """把复算脚本作为独立模块载入，便于按例篡改而不污染真身.

    ``MILLIS_BY_DAY`` 来自 ``tests.fuyao_golden``——那模块在 ``sys.modules`` 里是共享的，
    所以要拷一份再交给用例，否则第一例的篡改会漏到后面所有例的基线里。
    """
    spec = spec_from_file_location("golden_millis_under_test", HERE / "golden_millis.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("golden_millis.py cannot be loaded as a module")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    missing = [face for face in FACES if not hasattr(module, face)]
    if missing:
        raise RuntimeError(f"golden_millis.py no longer exposes the judged faces: {missing}")
    verifier = cast("Verifier", module)
    verifier.MILLIS_BY_DAY = dict(verifier.MILLIS_BY_DAY)
    verifier.WIRE_ANCHOR_MILLIS = dict(verifier.WIRE_ANCHOR_MILLIS)
    return verifier


def run(verifier: Verifier) -> tuple[int, str]:
    """跑一次 main()，返回 (退出码, 标准输出)."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = verifier.main()
    return code, buffer.getvalue()


def broken_table(verifier: Verifier) -> None:
    """①篡改表：把第一个交易日往前提 1ms."""
    first = min(verifier.MILLIS_BY_DAY)
    verifier.MILLIS_BY_DAY[first] -= 1


def broken_recording(verifier: Verifier) -> None:
    """②篡改录制件：第一条锚点 +1ms（档案与上线报文不再一致）."""
    anchors = list(verifier.recorded_wire_anchors())
    index = 0
    case, kind, day, millis = anchors[index]
    anchors[index] = (case, kind, day, millis + 1)
    verifier.recorded_wire_anchors = lambda: anchors


def missing_anchor(verifier: Verifier) -> None:
    """③档案里查不到某个声明过的锚点日."""
    anchors = [a for a in verifier.recorded_wire_anchors() if a[2].isoformat() != "1990-01-01"]
    verifier.recorded_wire_anchors = lambda: anchors


def broken_fixed_offset(verifier: Verifier) -> None:
    """④固定偏移推导写成 +7：同一天算不出同一个零点."""
    verifier.FIXED_OFFSET = dt.timezone(dt.timedelta(hours=7))


def broken_zoneinfo(verifier: Verifier) -> None:
    """⑤tzdata 口径漂移：上海时区被换成 UTC，换算与 +8 偏移假设同时该红."""
    verifier.SHANGHAI = ZoneInfo("UTC")


CASES: Final[list[tuple[str, Callable[[Verifier], None]]]] = [
    ("表被篡改一格", broken_table),
    ("录制锚点被篡改一格", broken_recording),
    ("声明的锚点在档案里查不到", missing_anchor),
    ("固定偏移推导写成 +7", broken_fixed_offset),
    ("tzdata 上海被换成 UTC", broken_zoneinfo),
]


def main() -> int:
    """每个 mutation 都必须让复算脚本非零退出，且未破坏前基线必须为绿."""
    problems: list[str] = []
    for label, mutate in CASES:
        module = load_verifier()
        code, out = run(module)
        if code != 0:
            problems.append(f"{label}: 基线未破即红 exit={code}\n{out}")
            continue
        mutate(module)
        code, out = run(module)
        if code == 0:
            problems.append(f"{label}: 篡改后仍然绿（判据是空壳）\n{out}")
        print(f"case={label} baseline=green mutated={'red' if code else 'GREEN'}")

    module = load_verifier()
    ok_code, ok_out = run(module)
    print(f"clean_tree exit={ok_code}")
    print(ok_out.rstrip())
    if ok_code != 0:
        problems.append("篡改泄漏进了干净树：用例之间没有隔离")
    for line in problems:
        print(f"PROBLEM {line}")
    print(
        f"VERDICT counterfactual_cases={len(CASES)} problems={len(problems)} "
        f"exit={'1' if problems else '0'}"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
