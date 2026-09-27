"""C45 棘轮面（AC-17|03）的反事实：每一条判据都要在**真实工作树**上红一次.

判据原文只说「债务不高于基线快照，只降不升」和「触碰即达标」。C45 量出来的事实是：
棘轮的读数完全由三张配置表决定（``[tool.ruff].exclude``、``[tool.mypy].exclude``、
``bandit.yaml`` 的 ``exclude_dirs``），而在其中任意一张加一行，**存量债务会自己下降**——
一行代码都不用改。所以「只降不升」必须连带把「被测范围只降不升」一起量出来。

八条反事实（七条各打一面，最后一条是显式登记的「不咬」）：

1. 正控制：什么都不改 ⇒ 棘轮 exit 0、探针 proven；
2. ``ceiling-lowered``：把快照上限改低一格 ⇒ 棘轮 exit 1、探针 gap（§判据 ①「改低一格必须红」）；
3. ``debt-planted``：往自研文件植入一条未使用 import ⇒ 棘轮 exit 1、探针 gap（「植入债务必须红」）；
4. ``ruff-exclude-nested``：往 ``[tool.ruff].exclude`` 加回一个自研子目录 ⇒ 债务读数**变好**、
   棘轮 exit 0，只有平面普查会红；
5. ``bandit-exclude``：往 ``bandit.yaml`` 的 ``exclude_dirs`` 加一条 ⇒ 同上，只有普查会红；
6. ``mypy-exclude-nested``：往 ``[tool.mypy].exclude`` 加一个没被点名的自研子目录 ⇒ 同上；
7. ``mypy-exclude-whole-root``：让 mypy 的 exclude 吃掉整个被测根 ⇒ mypy 自己硬失败（exit 2），
   棘轮 exit 1 且探针拒绝出读数；
8. ``ruff-exclude-top-level-inert``（显式不咬）：往 ``[tool.ruff].exclude`` 加一个**根级**条目，
   棘轮与探针都**不会**红——门禁是按根把目录传给 ruff 的，ruff 对自己显式收到的根不套 exclude。
   留这一条是为了说清平面普查看得到什么、看不到什么，而不是假装看得到。

安全：每条只临时改它声明过的那几个文件，跑完按字节还原并核对 sha256；不与 ``make gate`` 并发。

用法::

    python docs/evidence/C45/ratchet_counterfactuals.py
"""

from __future__ import annotations

import hashlib
import re
import subprocess  # nosec B404  # only ever called with a literal argv and shell disabled
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypeAlias

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
RATCHET = "scripts/quality/ratchet.py"
PROBE = "scripts/quality/acceptance_item_probe.py"
ITEM = "AC-17|03"

#: ``(相对路径, 原文片段, 替换后的片段)``。片段必须恰好出现一次，否则拒绝改动工作树。
Edit: TypeAlias = tuple[str, str, str]

RUFF_ANCHOR: Final = '    "frontend/",\n    ".venv",\n'
MYPY_ANCHOR: Final = 'exclude = [\n    "^opendata_http/",\n'
BANDIT_ANCHOR: Final = '  - "node_modules"\n'


def _sha(path: Path) -> str:
    """The sha256 of a file's bytes, used to prove the work tree came back unchanged."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _touched(edits: Sequence[Edit]) -> list[str]:
    return sorted({rel for rel, _old, _new in edits})


@contextmanager
def mutated(edits: Sequence[Edit]) -> Iterator[None]:
    """Apply ``edits`` to the work tree, then restore every touched file byte for byte.

    Raises:
        AssertionError: if an anchor is missing or ambiguous; nothing is written in that case.
    """
    targets = _touched(edits)
    originals = {rel: (REPO_ROOT / rel).read_text(encoding="utf-8") for rel in targets}
    staged: list[tuple[str, str]] = []
    try:
        for rel, old, new in edits:
            source = originals[rel]
            if source.count(old) != 1:
                raise AssertionError(f"anchor for {rel} appears {source.count(old)} times, not 1")
            staged.append((rel, source.replace(old, new, 1)))
        for rel, text in staged:
            (REPO_ROOT / rel).write_text(text, encoding="utf-8")
        yield
    finally:
        for rel in targets:
            (REPO_ROOT / rel).write_text(originals[rel], encoding="utf-8")


def restore_check(edits: Sequence[Edit], before: dict[str, str]) -> str:
    """Confirm every touched file came back with the hash it went in with."""
    bad = [rel for rel in _touched(edits) if _sha(REPO_ROOT / rel) != before[rel]]
    return "还原=sha256✓" if not bad else f"还原失败 {bad}"


def sh(argv: list[str]) -> tuple[int, str]:
    """Run a literal argv in the repository and merge its two streams."""
    proc = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
        argv,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def verdict_of(out: str) -> str:
    """``proven`` / ``gap`` / ``error`` out of one probe run."""
    want = re.compile(rf"VERDICT {re.escape(ITEM)}: (\w+)")
    for line in out.splitlines():
        found = want.match(line)
        if found:
            return found.group(1)
    return "error"


def finding(out: str, needle: str) -> str:
    """The first line carrying ``needle``, trimmed so the log stays readable."""
    for line in out.splitlines():
        if needle in line:
            return line.strip()[:230]
    return f"(没有含 {needle!r} 的行)"


@dataclass(frozen=True)
class Case:
    """One counterfactual: what to break, and what the two tools must then say.

    Attributes:
        name: Case id, printed in the log.
        face: Which judgement face the case is aimed at.
        edits: The text substitutions to stage.
        ratchet: Expected ``ratchet.py`` exit code.
        probe: Expected probe verdict (``error`` means it refuses to produce a reading).
        needs: Strings that must appear in one of the two outputs.
        note: Extra explanation printed under the case.
    """

    name: str
    face: str
    edits: tuple[Edit, ...]
    ratchet: int
    probe: str
    needs: tuple[str, ...] = field(default=())
    note: str = ""


CASES: Final = (
    Case(
        name="baseline",
        face="正控制：什么都不改",
        edits=(),
        ratchet=0,
        probe="proven",
    ),
    Case(
        name="ceiling-lowered",
        face="快照上限被改低一格（反证 ①）",
        edits=(("docs/quality/ratchet.json", '  "ruff_selfdev": 242,', '  "ruff_selfdev": 241,'),),
        ratchet=1,
        probe="gap",
        needs=("quality debt increased", "ruff_selfdev: 241 -> 242"),
    ),
    Case(
        name="debt-planted",
        face="自研文件里植入一条未使用 import（反证 ②）",
        edits=(
            (
                RATCHET,
                'if __name__ == "__main__":\n    raise SystemExit(main())\n',
                'if __name__ == "__main__":\n    raise SystemExit(main())\n\nimport zlib\n',
            ),
        ),
        ratchet=1,
        probe="gap",
        needs=("quality debt increased", "ruff_selfdev"),
    ),
    Case(
        name="ruff-exclude-nested",
        face="ruff 的 exclude 加回一个自研子目录（债务读数会自己变好）",
        edits=(
            (
                "pyproject.toml",
                RUFF_ANCHOR,
                RUFF_ANCHOR.replace(
                    '    "frontend/",\n', '    "opendata/pipeline/",\n    "frontend/",\n'
                ),
            ),
        ),
        ratchet=0,
        probe="gap",
        needs=("看不见 27",),
        note="只有平面普查红：ruff_selfdev 掉到 242 以下，棘轮不会拦",
    ),
    Case(
        name="bandit-exclude",
        face="bandit.yaml 的 exclude_dirs 加一条自研子串",
        edits=(("bandit.yaml", BANDIT_ANCHOR, BANDIT_ANCHOR + '  - "pipeline"\n'),),
        ratchet=0,
        probe="gap",
        needs=("bandit 221/251，被它漏掉的 30 个",),
    ),
    Case(
        name="mypy-exclude-nested",
        face="mypy 的 exclude 加一个没被点名的自研子目录",
        edits=(("pyproject.toml", MYPY_ANCHOR, MYPY_ANCHOR + '    "^opendata/pipeline/",\n'),),
        ratchet=0,
        probe="gap",
        needs=("27 个是没人声明的 exclude",),
    ),
    Case(
        name="mypy-exclude-whole-root",
        face="mypy 的 exclude 吃掉整个被测根",
        edits=(("pyproject.toml", MYPY_ANCHOR, MYPY_ANCHOR + '    "^alembic_data/",\n'),),
        ratchet=1,
        probe="error",
        needs=("no .py[i] files",),
        note="mypy 对显式传进来的空目录硬失败（exit 2），棘轮与探针同时拒绝读数",
    ),
    Case(
        name="ruff-exclude-top-level-inert",
        face="ruff 的 exclude 加一个根级条目（显式登记为不咬）",
        edits=(
            (
                "pyproject.toml",
                RUFF_ANCHOR,
                RUFF_ANCHOR.replace(
                    '    "frontend/",\n', '    "alembic_data/",\n    "frontend/",\n'
                ),
            ),
        ),
        ratchet=0,
        probe="proven",
        note="门禁按根传目录，ruff 不对自己显式收到的根套 exclude，所以这一条是惰性的；"
        "平面普查量的是走到的文件数，不是配置表里写了什么",
    ),
)


def run_case(case: Case) -> list[str]:
    """Apply one case, read both tools, and report whether the judge bit."""
    before = {rel: _sha(REPO_ROOT / rel) for rel in _touched(case.edits)}
    with mutated(case.edits):
        ratchet_code, ratchet_out = sh([sys.executable, RATCHET])
        probe_code, probe_out = sh([sys.executable, PROBE, "--item", ITEM])
    state = restore_check(case.edits, before) if case.edits else "还原=未改动"
    got = verdict_of(probe_out)
    ok = ratchet_code == case.ratchet and got == case.probe
    combined = ratchet_out + probe_out
    lines: list[str] = []
    for needle in case.needs:
        hit = needle in combined
        ok = ok and hit
        lines.append(f"      读数「{needle}」: {finding(combined, needle)}")
    lines.insert(
        0,
        f"{'ok  ' if ok else 'FAIL'} break[{case.name}] 面={case.face} "
        f"棘轮 exit={ratchet_code}(期望 {case.ratchet}) 探针={got}(期望 {case.probe}) {state}",
    )
    if case.note:
        lines.insert(1, f"      说明 {case.note}")
    if not ok:
        lines.append("      原始尾部: " + " / ".join(combined.strip().splitlines()[-3:])[:230])
        lines.append(f"      探针 exit={probe_code}")
    return lines


def main() -> int:
    """Run every counterfactual and exit non-zero when a face does not bite."""
    failed = 0
    for case in CASES:
        lines = run_case(case)
        for line in lines:
            print(line, flush=True)
        failed += sum(1 for line in lines if line.startswith("FAIL"))
    print(
        f"VERDICT counterfactuals={len(CASES)} failed={failed} "
        f"exit={'1' if failed else '0'}（每条都核对棘轮 exit、{ITEM} 探针判定与字节还原）"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
