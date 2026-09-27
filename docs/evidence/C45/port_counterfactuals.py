"""C45 搬运层（AC-17|05）的反事实：判据原文的三段都要在**真实工作树**上红一次.

判据原文：「搬运代码（``opendata_http/``）：仅 E/F 检查、未做 format/isort 重排（与上游可
diff）；pre-commit 含 ``exclude: opendata_http/``；lint 债务不高于棘轮快照」。三段各有反证：

1. 正控制：什么都不改 ⇒ 棘轮 exit 0、重放 exit 0、探针 proven；
2. ``select-widened``：把棘轮对搬运树的 ``--select E,F`` 改成按自研规则集跑 ⇒ 棘轮 exit 1
   （2144 → 9709），探针 gap——「仅 E/F」这条一旦被人加宽，读数会当场爆；
3. ``verbs-narrowed``：把 ``HTTP_VERBS`` 里的 ``request`` 去掉 ⇒ 直连数自己下降，棘轮 exit 0，
   只有「动词集合是否完整」这一面会红（两处读数同源，行为对照看不见）；
4. ``hand-edit-unregistered``：往一个搬运文件加一行没登记的手改 ⇒ 重放工具 exit 1、
   探针 gap（「与上游可 diff」被破坏）；
5. ``format-applied``：对搬运文件真的跑一次 ``ruff format`` ⇒ 重放不一致、探针 gap。
   「未做 format 重排」这一树级读数不会红（209 个文件→208 个，仍然 >0），红的是这一文件
   与上游的逐行 diff——它和「手改」是不同的破坏方式，各自的读数也不同；
6. ``register-drift``：只在 ``upstream.lock`` 里把某文件标成有人工改动（文档与 codemod 都没标）
   ⇒ 三处登记不一致、探针 gap；
7. ``precommit-exclude-dropped``：删掉 ``.pre-commit-config.yaml`` 里 ruff-format 对搬运树的
   ``exclude`` ⇒ 比 HEAD 少一条排除、探针 gap（下一次 hook 就会把整棵树重排掉）。

安全：每条只临时改它声明过的那几个文件，跑完按字节还原并核对 sha256；不与 ``make gate`` 并发。

用法::

    python docs/evidence/C45/port_counterfactuals.py
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
ITEM = "AC-17|05"
PORTED_FILE = "opendata_http/bond/bond_cb_sina.py"
#: A ported file ``ruff format`` genuinely rewrites (104 of 313 are already format-clean).
PORTED_FORMAT_FILE = "opendata_http/bond/bond_china.py"

#: ``(相对路径, 原文片段, 替换后的片段)``。片段必须恰好出现一次，否则拒绝改动工作树。
Edit: TypeAlias = tuple[str, str, str]

RATCHET_SELECT: Final = '        "ruff_ported": count_ruff(PORTED_PATHS, select="E,F"),\n'
RATCHET_VERBS: Final = (
    'HTTP_VERBS = frozenset({"get", "post", "put", "delete", "head", "patch", "request"})\n'
)
LOCK_ANCHOR: Final = (
    '      "path": "_version.py",\n'
    '      "upstream_path": "akshare/_version.py",\n'
    '      "sha256": "aabe70222b650cd3bd59acd84ae8e31bf5b88d841e9d6a3a2a688aa6b24ffaf2",\n'
    '      "manual_edits": false\n'
)
CODING_ANCHOR: Final = "# -*- coding:utf-8 -*-\n"
HOOK_ANCHOR: Final = "      - id: ruff-format\n        exclude: ^opendata_http/\n"


def _sha(path: Path) -> str:
    """The sha256 of a file's bytes, used to prove the work tree came back unchanged."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _touched(edits: Sequence[Edit], watches: Sequence[str] = ()) -> list[str]:
    return sorted({rel for rel, _old, _new in edits} | set(watches))


@contextmanager
def mutated(edits: Sequence[Edit], watches: Sequence[str] = ()) -> Iterator[None]:
    """Apply ``edits`` to the work tree, then restore every touched file byte for byte.

    ``watches`` names files a :attr:`Case.runs_after` command rewrites in place: they
    carry no anchor, but they still have to come back exactly as they were found.

    Raises:
        AssertionError: if an anchor is missing or ambiguous; nothing is written in that case.
    """
    targets = _touched(edits, watches)
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
        probe: Expected probe verdict.
        needs: Strings that must appear in one of the two outputs.
        note: Extra explanation printed under the case.
        runs_after: Extra argv to run once the edits are staged (e.g. ``ruff format``).
        watches: Files restored byte for byte that carry no anchor edit.
    """

    name: str
    face: str
    edits: tuple[Edit, ...]
    ratchet: int
    probe: str
    needs: tuple[str, ...] = field(default=())
    note: str = ""
    runs_after: tuple[list[str], ...] = ()
    watches: tuple[str, ...] = ()


CASES: Final = (
    Case(
        name="baseline",
        face="正控制：什么都不改",
        edits=(),
        ratchet=0,
        probe="proven",
    ),
    Case(
        name="select-widened",
        face="搬运树的 --select E,F 被加宽成自研规则集",
        edits=(
            (
                RATCHET,
                RATCHET_SELECT,
                RATCHET_SELECT.replace(', select="E,F"', ""),
            ),
        ),
        ratchet=1,
        probe="gap",
        needs=("quality debt increased", "ruff_ported: 2144 -> 9709"),
    ),
    Case(
        name="verbs-narrowed",
        face="直连计数的动词集合少一个（request）",
        edits=(
            (
                RATCHET,
                RATCHET_VERBS,
                RATCHET_VERBS.replace('"patch", "request"', '"patch"'),
            ),
        ),
        ratchet=0,
        probe="gap",
        needs=("完整 = no",),
        note="两处读数都调用同一个 count_direct_http，行为对照看不出集合被改窄，只能核对声明",
    ),
    Case(
        name="hand-edit-unregistered",
        face="搬运文件里加一行没登记的手改",
        edits=(
            (
                PORTED_FILE,
                CODING_ANCHOR,
                CODING_ANCHOR + "# C45 planted an unregistered hand edit (reverted)\n",
            ),
        ),
        ratchet=0,
        probe="gap",
        needs=("待办 1 条", "逐字一致 314 行"),
    ),
    Case(
        name="format-applied",
        face="对搬运文件真跑一次 ruff format（被重排过，而不是被手改）",
        edits=(),
        ratchet=0,
        probe="gap",
        needs=("逐字一致 314 行", "待办 1 条", "「未做 format/isort 重排」：208 个文件"),
        watches=(PORTED_FORMAT_FILE,),
        runs_after=([sys.executable, "-m", "ruff", "format", "--quiet", PORTED_FORMAT_FILE],),
        note="「未做 format 重排」这一面本身不红（209→208 仍然 >0），红的只有逐行 diff",
    ),
    Case(
        name="register-drift",
        face="只在 upstream.lock 里说这是人工改动",
        edits=(
            (
                "opendata_http/upstream.lock",
                LOCK_ANCHOR,
                LOCK_ANCHOR.replace('"manual_edits": false', '"manual_edits": true'),
            ),
        ),
        ratchet=0,
        probe="gap",
        needs=("不一致 1 条",),
        note="重放按 MANUAL_EDITS 复现、读者看 THIRD_PARTY_NOTICES.md，锁单独改口就三处不等",
    ),
    Case(
        name="precommit-exclude-dropped",
        face="pre-commit 少一条针对搬运树的 exclude",
        edits=((".pre-commit-config.yaml", HOOK_ANCHOR, "      - id: ruff-format\n"),),
        ratchet=0,
        probe="gap",
        needs=("比 HEAD 少的 1 个",),
        note="下一次 hook 运行会把整棵搬运树重排掉，「与上游可 diff」当场失效",
    ),
)


def run_case(case: Case) -> list[str]:
    """Apply one case, read both tools, and report whether the judge bit."""
    watched = _touched(case.edits, case.watches)
    before = {rel: _sha(REPO_ROOT / rel) for rel in watched}
    lines: list[str] = []
    with mutated(case.edits, case.watches):
        for argv in case.runs_after:
            sh(argv)
        wrote = {rel: _sha(REPO_ROOT / rel) for rel in before}
        ratchet_code, ratchet_out = sh([sys.executable, RATCHET])
        probe_code, probe_out = sh([sys.executable, PROBE, "--item", ITEM])
    bad = [rel for rel in before if _sha(REPO_ROOT / rel) != before[rel]]
    state = "还原=sha256✓" if not bad else f"还原失败 {bad}"
    changed = [rel for rel in before if wrote[rel] != before[rel]]
    if case.runs_after and not changed:
        lines.append(f"FAIL break[{case.name}] 附加命令没有改动任何声明的文件：{case.runs_after}")
        return lines
    got = verdict_of(probe_out)
    combined = ratchet_out + probe_out
    ok = ratchet_code == case.ratchet and got == case.probe
    readings: list[str] = []
    for needle in case.needs:
        hit = needle in combined
        ok = ok and hit
        readings.append(f"      读数「{needle}」: {finding(combined, needle)}")
    lines.insert(
        0,
        f"{'ok  ' if ok else 'FAIL'} break[{case.name}] 面={case.face} "
        f"棘轮 exit={ratchet_code}(期望 {case.ratchet}) 探针={got}(期望 {case.probe}) {state}",
    )
    if case.note:
        lines.insert(1, f"      说明 {case.note}")
    lines.extend(readings)
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
