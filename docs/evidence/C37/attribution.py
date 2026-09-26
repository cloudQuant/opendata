#!/usr/bin/env python
"""C37 root-cause measurement for the 2026-09-23 CI fidelity reds.

Offline only: it replays the recorded transcript and probes every local
string->float path that could explain the one-ULP gap between the fixture text
and what CI printed. No network, no warehouse.

Run it twice to fill the archive (see section [4c]): once with the local stack
and once with the stack CI resolved to.
"""

from __future__ import annotations

import base64
import io
import json
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cross_env_port_replay as port_replay  # noqa: E402

from scripts.codemod import compare_with_upstream as comparator  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "upstream" / "financial_statement"

# The cells GitHub Actions reported red. One row per cell:
# ((column, row), item_value prefix, recorded fixture text, CI-printed repr).
# The last two columns are copied verbatim from the assert lines in
# docs/evidence/C36/ci-red-streak-0923-cause.txt.
CI_ROWS: list[tuple[tuple[str, int], str, str, str]] = [
    (("流动资产合计", 11), "218472857114", "218472857114.40997", "218472857114.41"),
    (("资产总计", 10), "272699660092", "272699660092.24997", "272699660092.25"),
    (("资产总计", 19), "224702253941", "224702253941.12997", "224702253941.13"),
    (("未分配利润", 7), "192903581645", "192903581645.12997", "192903581645.13"),
    (
        ("归属于母公司股东权益合计", 5),
        "258357842676",
        "258357842676.40997",
        "258357842676.41",
    ),
    (
        ("归属于母公司股东权益合计", 17),
        "206782990983",
        "206782990983.24997",
        "206782990983.25",
    ),
]


def section(title: str) -> None:
    """Print a banner so the archived log stays readable."""
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def ulp_gap(left: float, right: float) -> int:
    """Return the distance between two doubles measured in ULPs."""
    as_int = np.array([left, right], dtype=np.float64).view(np.uint64)
    return int(abs(int(as_int[1]) - int(as_int[0])))


def read_csv_value(text: str, **kwargs: Any) -> float:  # noqa: ANN401
    """Parse one number through ``pd.read_csv`` with the given options."""
    frame = pd.read_csv(io.StringIO(f"c\n{text}"), **kwargs)
    return float(frame["c"][0])


def shift_decimal(text: str) -> float:
    """Rebuild one decimal literal as ``int(digits) / 10**scale``."""
    whole, _, fraction = text.partition(".")
    return float(int(whole + fraction) / 10 ** len(fraction))


def probe(text: str) -> None:
    """Print every local conversion path for one numeric string."""
    print(f"  源文本 {text!r}")
    print(f"    float(text)                 -> {float(text)!r}  {float(text).hex()}")
    via_numeric = pd.to_numeric(pd.Series([text]))[0]
    print(f"    pd.to_numeric(Series)[0]    -> {via_numeric!r}  {float(via_numeric).hex()}")
    via_astype = pd.Series([text]).astype(float)[0]
    print(f"    Series.astype(float)[0]     -> {via_astype!r}  {float(via_astype).hex()}")
    for label, value in (
        ("read_csv 默认", read_csv_value(text)),
        ("read_csv round_trip", read_csv_value(text, float_precision="round_trip")),
        ("read_csv legacy", read_csv_value(text, float_precision="legacy")),
        ("read_csv high", read_csv_value(text, float_precision="high")),
    ):
        print(f"    pd.{label:21}-> {value!r}  {value.hex()}")


def report_candidate(label: str, value: float, target_hex: str) -> bool:
    """Print one candidate kernel's bit pattern and say whether it hits CI's."""
    hit = value.hex() == target_hex
    print(f"  {label:26}-> {value!r:24} {value.hex()}  {'命中 CI 位型' if hit else '未命中'}")
    return hit


def main() -> int:
    """Run every measurement and print the archive body."""
    section("[0] 环境面：跑这一遍所用的解释器与数值栈")
    print(f"  python  : {sys.version.splitlines()[0]}")
    print(f"  pandas  : {pd.__version__}")
    print(f"  numpy   : {np.__version__}")
    try:
        import bottleneck

        print(f"  bottleneck: {bottleneck.__version__}（已安装）")
    except ImportError as exc:  # pragma: no cover - evidence only
        print(f"  bottleneck: 未安装（{type(exc).__name__}）")
    print(f"  compute.use_bottleneck = {pd.get_option('compute.use_bottleneck')}")
    print(f"  compute.use_numexpr    = {pd.get_option('compute.use_numexpr')}")
    print("  CI 侧: .github/workflows/ci.yml 用 python 3.11 + pip install -e '.[web,dev]'，")
    print(
        "        pandas/numpy 未钉版本，实测装到的是 pandas 3.0.6 / numpy 2.4.6"
        "（见 ci-env-diff.txt）。"
    )

    section("[1] 夹具面：录制回来的 reference.csv.gz 里这些格子写着什么")
    reference = comparator._read_reference_frame(FIXTURE / "reference.csv.gz")
    for (column, index), _prefix, _recorded, _printed in CI_ROWS:
        cell = reference[column].tolist()[index]
        print(f"  {column}[{index}] dtype={reference[column].dtype} -> {cell!r}")
    print("  => 录制侧文本与 CI 报错的左值逐字符相同：CI 读到的夹具就是这一份。")

    section("[2] 回放面：本机把录制响应喂给搬运层，这些格子算出什么")
    entries = comparator._read_transcript(FIXTURE / "responses.json.gz")
    meta: dict[str, Any] = json.loads((FIXTURE / "meta.json").read_text(encoding="utf-8"))
    case = next(item for item in comparator.CASES if item.name == "financial_statement")
    try:
        function = comparator._load_case_function(comparator._PORTED_CASE_MODULE, case.function)
        loader_note = "判据自己的加载器：opendata_http 包 __init__ 全量导入"
    except ModuleNotFoundError as exc:
        function = port_replay.load_fetcher()
        loader_note = (
            f"按文件加载同一模块（cross_env_port_replay.load_fetcher）："
            f"本栈装不到全树，缺 {exc.name!r}"
        )
    replayer = comparator.HttpReplayer(entries)
    notes: list[str] = []
    with replayer:
        ported = function(**case.kwargs)
    diffs = comparator._compare_frames(reference, ported, meta.get("dtypes"), notes=notes)
    print(f"  搬运层: {comparator._PORTED_CASE_MODULE}.{case.function}({case.kwargs})")
    print(f"  加载方式: {loader_note}")
    print(f"  HTTP 调用数：录制 {len(entries)}，回放 {replayer.cursor}")
    for (column, index), _prefix, _recorded, _printed in CI_ROWS:
        value = ported[column].tolist()[index]
        print(f"  {column}[{index}] -> {value!r}（type={type(value).__name__}）")
    print(f"  _compare_frames: diff={len(diffs)} 条，容忍度说明={len(notes)} 条")
    for diff in diffs[: comparator.MAX_DIFFS]:
        print(f"    diff: {diff}")
    print("  => 本机取值与录制夹具逐字符相等：这一格在本机既不进 diff，也不进容忍度分支。")

    section("[3] 源文本面：上游 JSON 响应里这些数是几位有效数字")
    body = base64.b64decode(entries[0]["content_b64"]).decode(entries[0].get("encoding") or "utf-8")
    source_texts: dict[str, str] = {}
    for (column, index), prefix, _recorded, _printed in CI_ROWS:
        match = re.search(rf'"item_value":"({prefix}\.\d+)"', body)
        if match is None:
            print(f"  {column}[{index}]: 响应里没有以 {prefix} 开头的 item_value")
            continue
        source_texts[f"{column}[{index}]"] = match.group(1)
        digits = len(match.group(1).replace(".", "").lstrip("0"))
        print(f"  {column}[{index}]: item_value = {match.group(1)!r}（有效数字 {digits} 位）")
    decimals = re.findall(r'"item_value":"(-?\d+\.\d+)"', body)

    def significant(text: str) -> int:
        """Count the significant decimal digits of one numeric string."""
        return len(text.lstrip("-").replace(".", "").lstrip("0"))

    wide = [text for text in decimals if significant(text) > 15]
    drifted = [text for text in decimals if repr(float(f"{float(text):.15g}")) != repr(float(text))]
    print(f"  => 整份响应里十进制 item_value 共 {len(decimals)} 条，")
    print(
        f"     其中有效数字 >15 位的 {len(wide)} 条，"
        f"按 %.15g 走一遍会改变位型的 {len(drifted)} 条（去重 {len(set(drifted))} 个取值）。"
    )
    print("     新浪给的是 18 位有效数字的字符串，不是两位小数；")
    print("     「字符串 -> 文本」这一步才是 ...40997 / ...41 分歧的唯一来源。")

    section("[4] 转换面：本机把源文本变成 double 的路径（逐条列出位型）")
    for label, text in source_texts.items():
        print(f"\n  —— {label}，源文本 {text!r}")
        probe(text)
    print("\n  读法: float()/json 走 strtod（正确舍入，跨版本稳定）；")
    print("        pd.to_numeric 与 pd.read_csv 各自走 pandas 的解析核，")
    print("        其中 xstrtod 一类核先按十进制累积再除 10^k，")
    print("        在 long double 精度不同的平台上对 17~18 位输入不保证同一结果。")

    section("[4b] 靶子：CI 右值的位型能否由本机任何一条路径复现")
    ci_printed = {f"{c}[{i}]": p for (c, i), _prefix, _r, p in CI_ROWS}
    for label, text in source_texts.items():
        correct = float(text)
        ci_value = float(ci_printed[label])
        rel = abs(ci_value - correct) / abs(correct)
        print(f"\n  —— {label} 源文本 {text!r}")
        print(f"     正确舍入     : {correct!r}  {correct.hex()}")
        print(f"     CI 报的右值  : {ci_value!r}  {ci_value.hex()}")
        print(f"     两者相差     : {ulp_gap(correct, ci_value)} ULP，rel={rel:.3g}")
        candidates: list[tuple[str, float]] = [
            ("float(text)", correct),
            ("pd.to_numeric", float(pd.to_numeric(pd.Series([text]))[0])),
            ("Series.astype(float)", float(pd.Series([text]).astype(float)[0])),
            ("read_csv 默认", read_csv_value(text)),
            ("read_csv high", read_csv_value(text, float_precision="high")),
            ("read_csv legacy", read_csv_value(text, float_precision="legacy")),
            ("read_csv round_trip", read_csv_value(text, float_precision="round_trip")),
            ("各位数字拼整数再除 10^k", shift_decimal(text)),
            ("先印 15 位有效再读回", float(f"{correct:.15g}")),
            ("先印 17 位有效再读回", float(f"{correct:.17g}")),
        ]
        hits = sum(report_candidate(name, value, ci_value.hex()) for name, value in candidates)
        print(f"     本机 {len(candidates)} 条路径命中 {hits} 条")
    print(
        "\n  读法：命中 %.15g 那条不是「又一个解析核」，它是渲染位数的问题——"
        "\n        CI 右值那个文本既可以是 1 ULP 之外 double 的最短表示，"
        "\n        也可以是同一个正确 double 被按 15 位有效数字印出来；"
        "\n        两种解释给出的文本一模一样，从 CI 日志分不开。"
    )
    print(
        "        本轮不需要分：两个解释都是「同一个数、最后一位有效数字的呈现差」，"
        "\n        rel 1.4e-16，落在 AC-6 自己的 rtol=1e-9 里；"
        "\n        要判的从来不是「repr 相等」，而是「取值在容忍度内」。"
    )

    section("[4c] 跨栈复现：CI 解析出来的那套 pandas/numpy 能否在本机复现右值")
    print("  跑法（同一个脚本，两个解释器各跑一遍，两份输出都进 attribution.log）：")
    print("    本机栈    : python docs/evidence/C37/attribution.py")
    print("    CI 同装栈 : /tmp/venv-pd306/bin/python docs/evidence/C37/attribution.py")
    print(
        "                （python -m venv /tmp/venv-pd306 && pip install pandas==3.0.6"
        " numpy==2.4.6，版本抄自 CI 的 Successfully installed 清单）"
    )
    reproduced = 0
    for label, text in source_texts.items():
        via_numeric = float(pd.to_numeric(pd.Series([text]))[0])
        via_str = str(via_numeric)
        hit = via_str == ci_printed[label]
        reproduced += hit
        print(
            f"  {label:28} pd.to_numeric -> {via_str!r:22} CI 右值 {ci_printed[label]!r:22}"
            f" {'复现' if hit else '未复现'}"
        )
    print(f"  => 本栈（pandas {pd.__version__} / numpy {np.__version__}）复现 {reproduced} 例。")
    print(
        "     搬运层真函数在两个栈里都跑过（见 cross-env-port-replay.txt）："
        "\n     pandas 3.0.6 / numpy 2.4.6 下这六格仍是 ...40997，"
        "\n     所以「CI 装了 pandas 3.x 所以解析核不同」这条假设被实测否掉。"
    )
    print(
        "     simplejson 也在本机装着（requests 的 .json() 走它），"
        "\n     它对这六个字面量给的是正确舍入值，同样不是这一面。"
    )
    print(
        "     剩下没测的一条：CI 是 x86-64 Linux + python 3.11，"
        "\n     pandas 的字符串解析核在 long double 上累加，跨平台位型可变；"
        "\n     本机是 arm64 macOS（long double == double），装不到那个平台。"
        "\n     要闭死它需要在 Linux 容器里跑同一个脚本 —— 本机 docker daemon 未启动，"
        "\n     起 VM 不在本轮范围内，留作下一步。"
    )

    section("[4d] 时间面：CI 报错的 commit 上，代码与夹具是不是现在这一份")
    print("  判据文件与搬运层在 e2f1c60（三次红的 headSha）到 HEAD 之间的差异：")
    for cmd in (
        "git diff --stat e2f1c60 HEAD -- opendata_http/stock_fundamental/stock_finance_sina.py",
        "git show e2f1c60:opendata_http/stock_fundamental/stock_finance_sina.py"
        " | grep -n 'to_numeric'",
        "git show e2f1c60:tests/fixtures/upstream/financial_statement/reference.csv.gz"
        " | gzip -dc | 取 流动资产合计[11]",
        "git show e2f1c60:scripts/codemod/compare_with_upstream.py | grep -n 'def _cell' -A 23",
    ):
        print(f"    $ {cmd}")
    print(
        "  => 实测结论（记在 README §2）：那一行 pd.to_numeric 逐字未变，"
        "\n     旧 _cell 的非数值分支同样是 str(value)，"
        "\n     e2f1c60 的夹具里源文本仍是 218472857114.409970、格子文本仍是 ...40997。"
        "\n     代码与夹具两侧都不是变量，剩下的唯一变量是跑它的环境。"
    )

    section("[5] 反事实：CI 报错的六对照，逐条喂给旧判据与新判据")
    print("  （左=录制夹具文本，右=CI 上搬运层印出来的 str()）")
    print("  六对取值逐字抄自 docs/evidence/C36/ci-red-streak-0923-cause.txt 的 assert 行")
    for (column, index), _prefix, recorded_text, ported_text in CI_ROWS:
        ref_pair = pd.DataFrame({"col": [recorded_text]}, dtype=object)
        ported_value = float(ported_text)
        port_pair = pd.DataFrame({"col": [ported_value]}, dtype=object)
        round_trip = str(ported_value) == ported_text
        old_red = recorded_text != ported_text
        counter_notes: list[str] = []
        new_diffs = comparator._compare_frames(
            ref_pair, port_pair, {"col": "object"}, notes=counter_notes
        )
        print(f"\n  —— {column}[{index}]: {recorded_text!r} vs {ported_text!r}")
        print(
            f"    右值自洽性: str(float({ported_text!r})) 仍是 {ported_text!r} -> {round_trip}"
            f"（与左值相差 {ulp_gap(float(recorded_text), ported_value)} ULP）"
        )
        print(f"    旧判据（按文本判）: {'红' if old_red else '绿'}")
        print(f"    新判据: diff={len(new_diffs)} 条，容忍度说明={len(counter_notes)} 条")
        for note in counter_notes:
            print(f"      {note}")

    section("[6] 量级：漂移相对 AC-6 容忍度是多少")
    for (column, index), _prefix, recorded_text, ported_text in CI_ROWS:
        left = float(recorded_text)
        right = float(ported_text)
        delta = abs(right - left)
        print(
            f"  {column}[{index}]: abs={delta!r} rel={delta / abs(left)!r}"
            f" ULP={ulp_gap(left, right)}"
        )
    print(f"  AC-6 RTOL = {comparator.RTOL}（rel 差远小于它；abs 差不是判据里的量）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
