#!/usr/bin/env python3
"""C50 现场读数：把条目级探针做进门禁成员之前，先量清三件事（全程只读）.

本轮登记要做的两件事都要求先有读数：① 把 `acceptance_item_probe.py` 做成门禁成员；
② 补一面「哪些历史仪器在读产品私有函数」。做成成员之前必须先量的事，就是这台仪器要答的：

1. **成员面**：`make gate` 现在有哪些成员、探针出现在几个成员的命令里、`make quality`
   /`quality-full` 里有没有它 —— 「判定工具不在门禁里」这句话要有计数，不能只是印象。
2. **手工遍次面**：历史档案里有多少遍证明 `--self-test` 真的跑过、门禁日志里出现过几次
   它 —— 反事实条数与「绿色门禁对它无感」之间的比值。
3. **成本面**：每个探针 measure 的墙钟时间与它 spawn 的子进程数，合计多少秒，与本遍 measure
   循环自己的墙钟差多少；另引工具 `--self-test` / `--all` 两种模式的手工遍次实测。这一面决定
   成员怎么设计（一次 measure 同时喂自检与对账，还是两遍各跑一次 ⇒ 差 4 分钟）。
4. **对账面**：每个探针的**实测状态**与台账 `state` 逐格比 —— 现在 `--all` 只在判据原文
   漂字时 exit 1，`台账现状=proven` 与 `VERDICT gap` 是并排打印、从不比较的。这一面读的
   是「如果昨天就有这道闸，会红几格」。
5. **状态依赖面**（AST 传递闭包）：每个探针的 measure 到底碰没碰 `git status`（工作树）/
   `git ls-files`（索引）/ `git show REV:`、`git log`、`git ls-tree`（历史）/ pytest 子进程。
   碰到前三种的读数**随遍次时刻变化**，这类格子做成恒红的门禁成员不是判据、是假红。
6. **仪器依赖面**：`docs/evidence/**/*.py` 与 `scripts/ops/*.py` 里，哪些历史仪器 import 了
   产品的**私有**符号（`_` 前缀），每个符号被几台仪器读，以及该符号最近一次被改是哪一
   个 commit —— 产品形状一变会咬到谁，这一面把名单钉死，而不是等下一次红门禁去发现。

7. **覆盖面**（纯集合对账，不 measure）：台账 130 个条目键里有多少格压根没有探针 ——
   `proven` 而无判定面的格子是一条谁也反驳不了的声称，「门禁每次重量判定面」对它不成立。
   这一面给「proven 必须有判定面」这道闸门一个实测起点，而不是一个印象。

安全边界：全程不写入、不建表、不碰真库（面 6 只读文件与 git 历史），不打印任何密钥值或
`.env` 内容。唯一的进程副作用是**被量对象自己**为量它的探针而 spawn 的子进程（pytest /
git），面 3 逐个计数并如实标注「计数来自本仪器的包装层，不是探针自己的输出」。

两遍留档：`census-run1-instrument-defects.txt` 是本脚本第一版的实测，它把门禁收尾的
`===== gate: PASSED =====` 横幅数成了成员（于是报「成员 17 / 绿时 18 段」），又拿另一遍手工
遍次的 247.5 s 常数去减本遍的 per-measure 合计（于是报出差分 -5.7 s），并且把「gate 日志里出现
探针文件名」当成「跑过判定面」。三处都在本脚本里改掉了；改掉的判词是「gate 日志正文里真跑过
判定面的 = 0 / 46」—— 历史上一遍都没有。修后各遍的读数与遍次之间的取代关系写在本轮的
README 里，因为任何档案都描述不了写下它自己之后的那棵树（C49 已实测过这个不动点）。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess  # nosec B404
import sys
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType

#: Repository root (this file lives in docs/evidence/C50).
ROOT = Path(__file__).resolve().parents[3]

#: Resolved once so the subprocess call never uses a partial executable path.
GIT = shutil.which("git")

#: The judging tool under measurement.
PROBE_REL = "scripts/quality/acceptance_item_probe.py"

#: Where the round archives live.
EVIDENCE_DIR = ROOT / "docs" / "evidence"

#: Trees whose python files the gate's A2 plane sweeps.
INSTRUMENT_GLOBS = ("docs/evidence/**/*.py", "scripts/ops/**/*.py")

#: Self-developed package roots an instrument may legitimately import.
PRODUCT_PREFIX = "opendata"

#: Makefile targets that decide what "in the gate" means.
AGGREGATE_TARGETS = ("gate", "quality", "quality-full", "pre-commit")

#: Wall times of the two manual passes taken earlier in this round, read from `/usr/bin/time -p`
#: of each command (archived as ``manual-probe-timings.txt``). Quoted as facts about the tool's
#: own modes, never subtracted against this run's per-measure sum -- the first pass of this
#: instrument did that across two different遍次 and reported a negative difference.
MANUAL_PASS_SECONDS: tuple[tuple[str, float], ...] = (
    ("--self-test", 247.85),
    ("--all", 247.52),
)

#: Source-level touches a measure closure can have on state that moves with the moment.
#: Matched after normalising quotes to ``"``: ``ast.unparse`` emits single quotes, so
#: writing the markers the way the source reads them would silently match nothing.
SURFACE_MARKERS: tuple[tuple[str, str], ...] = (
    ('"git", "status"', "worktree(git status)"),
    ('"git", "ls-files"', "index(git ls-files)"),
    ("ctx.tracked()", "index(ctx.tracked)"),
    ("self._tracked", "index(ctx.tracked)"),
    ('"git", "show"', "history(git show REV:)"),
    ('"git", "log"', "history(git log)"),
    ('"git", "ls-tree"', "history(git ls-tree)"),
    ("run_pytest(", "pytest 子进程"),
    ("node_outcome(", "pytest 节点单跑"),
)

#: Labels whose presence makes a reading about this moment, not about the repository.
MOMENT_LABELS = ("worktree", "index", "history")


class ProbeReading(NamedTuple):
    """Everything one probe contributes to faces 3-5.

    Attributes:
        item: ``AC-N|NN``.
        summary: What the probe says it decides.
        seconds: Wall time of ``measure``.
        spawns: Subprocesses that measure launched, by surface label.
        state: What the probe reads right now (``proven`` / ``gap`` / ``error...``).
        ledger_state: What the ledger claims for the same grid.
        ticked: Whether the document's box is checked.
        surfaces: External surfaces its measure closure touches.
        drift: Wording-drift string; empty when the criterion still matches.
    """

    item: str
    summary: str
    seconds: float
    spawns: Counter[str]
    state: str
    ledger_state: str
    ticked: bool
    surfaces: tuple[str, ...]
    drift: str


class InstrumentRead(NamedTuple):
    """One archived instrument's import surface.

    Attributes:
        path: Repository-relative path.
        product_imports: Modules it imports from the self-developed packages.
        private_symbols: ``module.symbol`` pairs for underscore-prefixed names.
    """

    path: str
    product_imports: tuple[str, ...]
    private_symbols: tuple[str, ...]


def section(title: str) -> None:
    """Print one readable section banner."""
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def say(body: str) -> None:
    """Print one reading line."""
    print(body)


def run_git(*args: str) -> tuple[int, str]:
    """Run a literal git argv in the repository; a non-zero exit is a reading.

    Args:
        *args: Arguments after ``git``.

    Returns:
        The exit code and the combined output.
    """
    if GIT is None:
        return 127, "git not found on PATH"
    proc = subprocess.run(  # noqa: S603  # nosec B603  # absolute path, literal argv, shell disabled
        [GIT, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    return proc.returncode, f"{proc.stdout}{proc.stderr}"


def load_tool() -> ModuleType:
    """Import the probe module by path without executing its ``__main__`` block.

    Returns:
        The loaded module object.

    Raises:
        RuntimeError: If the module cannot be loaded.
    """
    spec = importlib.util.spec_from_file_location("c50_subject", ROOT / PROBE_REL)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {PROBE_REL}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["c50_subject"] = module
    spec.loader.exec_module(module)
    return module


def tool_attr(tool: ModuleType, name: str) -> Any:  # noqa: ANN401
    """Read one attribute off the dynamically loaded judging tool.

    Args:
        tool: The loaded module.
        name: The attribute name.

    Returns:
        Whatever it holds -- the subject is loaded by path, so nothing here is static.
    """
    return getattr(tool, name)


def set_tool_attr(tool: ModuleType, name: str, value: object) -> None:
    """Write one attribute back onto the dynamically loaded judging tool.

    Args:
        tool: The loaded module.
        name: The attribute name.
        value: What to store under it.
    """
    setattr(tool, name, value)


# --------------------------------------------------------------------------- #
# 面 1：成员面
# --------------------------------------------------------------------------- #


def gate_members(makefile: str) -> list[str]:
    """The member list of the ``gate:`` recipe, in order.

    ``PASSED`` is the recipe's own closing banner, not a member: counting it is how the first
    pass of this instrument reported 17 members and "18 sections when green" for a gate that
    has 16 members and prints 17 (Makefile:184-217, counted section by section).

    Args:
        makefile: Contents of the repository Makefile.

    Returns:
        Every name echoed as ``===== gate: NAME =====`` inside the ``gate:`` recipe, with the
        closing banner dropped.
    """
    members: list[str] = []
    in_gate = False
    for line in makefile.splitlines():
        if line.startswith("gate:"):
            in_gate = True
            continue
        if not in_gate:
            continue
        if line and not line.startswith(("\t", " ")):
            break
        if '"===== gate:' in line:
            name = line.split("===== gate:", 1)[1].split("=====", 1)[0].strip()
            if name != "PASSED":
                members.append(name)
    return members


def target_recipes(makefile: str) -> dict[str, list[str]]:
    """Map each target name to the command lines of its recipe.

    Args:
        makefile: Contents of the repository Makefile.

    Returns:
        Target name to its tab-prefixed recipe lines.
    """
    recipes: dict[str, list[str]] = {}
    current = ""
    for line in makefile.splitlines():
        head = line.split("#", 1)[0]
        if line and not line.startswith(("\t", " ")) and ":" in head:
            current = head.split(":", 1)[0].strip()
            recipes.setdefault(current, [])
        elif line.startswith("\t") and current:
            recipes[current].append(line.strip())
    return recipes


def membership_face(makefile: str, members: list[str]) -> None:
    """Report how many gate members and aggregate targets reach the probe tool."""
    section("1. 成员面：判定工具今天离门禁有多远")
    recipes = target_recipes(makefile)
    named = sorted(t for t, body in recipes.items() if PROBE_REL in " ".join(body))
    say(f"gate 成员数 = {len(members)}（绿时打印 {len(members) + 1} 段 `===== gate:`）")
    say(f"成员名单 = {', '.join(members)}")
    say(f"命令里出现 `{PROBE_REL}` 的 target 数 = {len(named)} -> {named or '无'}")
    for aggregate in AGGREGATE_TARGETS:
        body = recipes.get(aggregate, [])
        hits = sum(1 for line in body if PROBE_REL in line)
        say(f"  聚合 target `{aggregate}`：{len(body)} 行命令，其中提到探针 {hits} 行")
    say(f"成员里名字含 `probe` 的 = {[m for m in members if 'probe' in m] or '无'}")
    newest = sorted(
        (p for p in EVIDENCE_DIR.glob("*/*.txt") if "gate-run" in p.name or "gate_" in p.name),
        key=lambda p: (p.stat().st_mtime, p.name),
    )[-1:]
    for path in newest:
        sections = read_text(path).count("===== gate:")
        say(
            f"交叉核对：最近一份 gate 日志 {path.parts[-2]}/{path.name}"
            f" 正文里 `===== gate:` 段数 = {sections}（成员 {len(members)} + 收尾 PASSED 横幅 1）"
        )


# --------------------------------------------------------------------------- #
# 面 2：手工遍次面
# --------------------------------------------------------------------------- #


def read_text(path: Path) -> str:
    """Read an archive, tolerating the odd undecodable byte.

    Args:
        path: The archive file.

    Returns:
        Its contents.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def is_self_test(path: Path) -> bool:
    """Whether an archive is a probe self-test run rather than a mere mention.

    Args:
        path: The archive file.

    Returns:
        True when the body carries the self-test verdict line.
    """
    body = read_text(path)
    return "probe(s) measured" in body and "counterfact" in body


def is_probe_run(path: Path) -> bool:
    """Whether an archive holds a probe reading rather than merely the probe's file name.

    Args:
        path: The archive file.

    Returns:
        True when the body carries a per-item verdict line the judging tool prints.
    """
    return "VERDICT AC-" in read_text(path)


def handrun_face(members: list[str]) -> None:
    """Count how often the self test was ever run, and whether a gate log holds it."""
    section("2. 手工遍次面：反事实条数今天靠哪些遍次成立")
    archives = sorted(EVIDENCE_DIR.glob("*/*.txt"))
    gate_logs = [p for p in archives if "gate-run" in p.name or "gate_" in p.name]
    self_tests = sorted({p.parts[-2] for p in archives if is_self_test(p)})
    real_runs = sorted({p.parts[-2] for p in archives if is_probe_run(p)})
    gate_with_probe = [p for p in gate_logs if PROBE_REL in read_text(p)]
    gate_real_runs = [p for p in gate_logs if is_probe_run(p)]
    say(f"round 档案目录数 = {len({p.parts[-2] for p in archives})}")
    say(f"txt 档案总数 = {len(archives)}；其中 gate 日志 = {len(gate_logs)}")
    say(f"正文含 `probe(s) measured` 判词的 round = {len(self_tests)} 个：{', '.join(self_tests)}")
    say(f"正文含 `VERDICT AC-` 逐格判词的 round = {len(real_runs)} 个：{', '.join(real_runs)}")
    say(
        f"gate 日志正文里出现探针文件名的 = {len(gate_with_probe)} / {len(gate_logs)}"
        "（含 a2-check 的文件清单，只证明这个文件存在过，不证明判定面被跑过）"
    )
    say(
        f"gate 日志正文里真跑过判定面的 = {len(gate_real_runs)} / {len(gate_logs)}"
        f"：{', '.join(p.parts[-2] for p in gate_real_runs) or '无'}"
    )
    said = "没有一个是它" if not any("probe" in m for m in members) else "已有含 probe 名字的成员"
    say(f"门禁成员 {len(members)} 个，{said} ⇒ 每一遍绿色 gate 都不重新量一次判定面")


# --------------------------------------------------------------------------- #
# 面 3 + 4 + 5：一次 measure 同时喂成本、对账与状态依赖
# --------------------------------------------------------------------------- #


def classify_spawn(argv: Sequence[str]) -> str:
    """Name the surface a spawned command touches.

    Args:
        argv: The literal argument vector.

    Returns:
        A stable label for the surface it reads.
    """
    words = [str(arg) for arg in argv]
    if "pytest" in words[:4]:
        return "pytest 子进程"
    if "git" in words:
        tail = words[words.index("git") + 1 :]
        verb = tail[0] if tail else "?"
        if verb == "show" and len(tail) > 1:
            return f"history(git show {tail[1].split(':', 1)[0]})"
        return f"git:{verb}"
    return words[0] if words else "(empty)"


def measure_once(tool: ModuleType, probe: Any, ctx: Any) -> ProbeReading:  # noqa: ANN401
    """Run one probe's measure, timing it and counting what it spawns.

    Args:
        tool: The loaded probe module, so the counter can wrap its own runner.
        probe: The registered probe.
        ctx: The shared measurement context.

    Returns:
        The reading for this probe.
    """
    original = tool_attr(tool, "run_argv")
    spawns: Counter[str] = Counter()

    def counting(argv: Sequence[str]) -> tuple[int, str]:
        """Pass through to the real runner while labelling what it launched."""
        spawns[classify_spawn(list(argv))] += 1
        code, out = original(list(argv))
        return int(code), str(out)

    set_tool_attr(tool, "run_argv", counting)
    started = time.perf_counter()
    try:
        facts = probe.measure(ctx)
        state = str(probe.judge(facts).state)
    except Exception as exc:  # a measure that raises is a reading, not a census crash
        state = f"error({type(exc).__name__})"
    finally:
        set_tool_attr(tool, "run_argv", original)
    seconds = time.perf_counter() - started
    try:
        doc = ctx.item(probe.item)
        entry = ctx.ledger_entry(probe.item)
        ledger_state, ticked, drift = (
            str(entry.get("state", "?")),
            bool(doc.ticked),
            str(tool_attr(tool, "wording_drift")(ctx, probe)),
        )
    except Exception as exc:  # same: a parse failure is reported, not raised out of the census
        ledger_state, ticked, drift = f"error({type(exc).__name__})", False, ""
    return ProbeReading(
        item=probe.item,
        summary=probe.summary,
        seconds=seconds,
        spawns=spawns,
        state=state,
        ledger_state=ledger_state,
        ticked=ticked,
        surfaces=closure_surfaces(probe.measure),
        drift=drift,
    )


def cost_and_reconcile(tool: ModuleType) -> tuple[list[ProbeReading], float]:
    """Measure every probe once, in registration order.

    Args:
        tool: The loaded probe module.

    Returns:
        One reading per probe, and the wall time of the whole measure loop.
    """
    started = time.perf_counter()
    ctx = tool_attr(tool, "load_context")()
    readings = [measure_once(tool, probe, ctx) for probe in tool_attr(tool, "PROBES")]
    return readings, time.perf_counter() - started


def top_level_functions(source: str) -> dict[str, ast.FunctionDef]:
    """Index a module's top-level function definitions by name.

    Args:
        source: The module source.

    Returns:
        Function name to its definition node.
    """
    found: dict[str, ast.FunctionDef] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef):
            found.setdefault(node.name, node)
    return found


def closure_surfaces(measure: Any) -> tuple[str, ...]:  # noqa: ANN401
    """Walk a measure function and the module-level helpers it calls, collecting touches.

    The walk is over source, not over the moment's readings, so the classification cannot
    be argued with: a face that shells out to ``git status`` is moment-dependent whether or
    not this particular run happened to see a clean tree.

    Args:
        measure: The probe's measure callable.

    Returns:
        Sorted surface labels, or one note when the closure only reads file contents.
    """
    name = getattr(measure, "__name__", "")
    defs = top_level_functions((ROOT / PROBE_REL).read_text(encoding="utf-8", errors="replace"))
    seen: set[str] = set()
    queue = [name]
    parts: list[str] = []
    while queue:
        current = queue.pop()
        node = defs.get(current)
        if node is None or current in seen:
            continue
        seen.add(current)
        parts.append(ast.unparse(node))
        for ref in ast.walk(node):
            reached = ""
            if isinstance(ref, ast.Name):
                reached = ref.id
            elif isinstance(ref, ast.Attribute):
                reached = ref.attr
            if reached in defs and reached not in seen:
                queue.append(reached)
    body = "\n".join(parts).replace("'", '"')
    found = {label for marker, label in SURFACE_MARKERS if marker in body}
    return tuple(sorted(found)) or ("(闭包里只读到文件内容)",)


def moment_dependent(reading: ProbeReading) -> bool:
    """Whether a reading is about this moment rather than about the repository.

    Args:
        reading: One probe's reading.

    Returns:
        True when its closure touches worktree, index or history state.
    """
    return any(surface.startswith(MOMENT_LABELS) for surface in reading.surfaces)


def cost_face(readings: list[ProbeReading], loop_wall: float) -> None:
    """Report the per-probe cost attribution and reconcile it with this run's own measure loop."""
    section("3. 成本面：探针各自的 measure 要多少秒、各 spawn 几个子进程")
    total = sum(reading.seconds for reading in readings)
    say(f"探针数 = {len(readings)}；per-measure 合计 = {total:.1f} s")
    say(
        f"本遍 measure 循环（含 load_context 解析文档与台账）墙钟 = {loop_wall:.1f} s；"
        f"循环内未计入各 measure 的部分 = {loop_wall - total:.1f} s"
    )
    for mode, seconds in MANUAL_PASS_SECONDS:
        say(f"工具自己的 `{mode}` 手工遍次 = {seconds} s")
    say("两个数字都读自本轮更早的 /usr/bin/time 遍次，见 manual-probe-timings.txt")
    say(
        f"两种手工遍次合计 = {sum(s for _, s in MANUAL_PASS_SECONDS):.1f} s —— "
        "成员化后一遍 measure 同时喂自检与对账，就只要 "
        f"{total:.1f} s"
    )
    by_surface: Counter[str] = Counter()
    for reading in readings:
        by_surface.update(reading.spawns)
    spawns = sum(by_surface.values())
    say(f"子进程 spawn 合计 = {spawns}；归因 = {json.dumps(dict(by_surface), ensure_ascii=False)}")
    say("（spawn 计数来自本仪器的包装层，不是探针自己的输出）")
    say("最贵的 8 个：")
    for reading in sorted(readings, key=lambda r: -r.seconds)[:8]:
        say(
            f"  {reading.item:<12} {reading.seconds:6.1f} s  "
            f"spawns={sum(reading.spawns.values()):3d}  {reading.summary[:40]}"
        )
    cheap = [r for r in readings if r.seconds < 1.0]
    say(f"measure < 1 s 的探针数 = {len(cheap)}（纯文件/AST 面，成员化后几乎不改变门禁时长）")


def reconcile_face(readings: list[ProbeReading]) -> None:
    """Compare every live reading with the state the ledger claims."""
    section("4. 对账面：台账说 proven 的格子，判定面此刻还成不成立")
    counts: Counter[str] = Counter()
    for reading in readings:
        if reading.drift:
            kind = "wording-drift"
        elif reading.state == reading.ledger_state:
            kind = "agrees"
        elif reading.ledger_state == "proven" and reading.state == "gap":
            kind = "stale-proof"
        elif reading.ledger_state in ("gap", "unreviewed") and reading.state == "proven":
            kind = "unsynced"
        else:
            kind = f"other({reading.state}|{reading.ledger_state})"
        counts[kind] += 1
        if kind != "agrees":
            say(
                f"  {reading.item:<12} 台账={reading.ledger_state:<10} 实测={reading.state:<8} "
                f"勾选={'x' if reading.ticked else ' '} "
                f"时刻依赖={'是' if moment_dependent(reading) else '否'} "
                f"面={','.join(reading.surfaces)}"
            )
    say(f"合计 = {json.dumps(dict(counts.most_common()), ensure_ascii=False)}")
    stale = [r.item for r in readings if r.ledger_state == "proven" and r.state == "gap"]
    say(
        "今天的 `--all` 对上面这些一律 exit 0（只有判据原文漂字才红）；若门禁里有一道"
        f"「台账 proven 必须仍读 proven」的闸，它会红 {len(stale)} 格：{stale}"
    )


def state_face(readings: list[ProbeReading]) -> None:
    """Report which probes read moment-dependent state."""
    section("5. 状态依赖面：哪些读数随「这一遍跑在什么时刻」而变化")
    moment = [r for r in readings if moment_dependent(r)]
    say(f"闭包读到工作树/索引/历史面的探针数 = {len(moment)} / {len(readings)}")
    for reading in moment:
        faces = ", ".join(s for s in reading.surfaces if s.startswith(MOMENT_LABELS))
        say(f"  {reading.item:<12} 台账={reading.ledger_state:<10} 实测={reading.state:<8} {faces}")
    by_surface: Counter[str] = Counter()
    for reading in readings:
        by_surface.update(reading.surfaces)
    say(f"全量面归因 = {json.dumps(dict(by_surface.most_common()), ensure_ascii=False)}")
    pytest_only = [r for r in readings if not moment_dependent(r) and r.spawns]
    say(
        f"只 spawn 子进程、不读 git 状态的探针数 = {len(pytest_only)}"
        "（pytest / 工具面读的是树内容：它随代码变化而变，不随「这一遍跑在提交前还是提交后」而变，"
        "所以这一类可以进门禁；上面三类不能，它们会在每一个在飞的轮次里假红）"
    )


def coverage_face(tool: ModuleType) -> None:
    """Report which ledger cells have no judging plane at all.

    Face 4 can only compare a reading with a claim where a reading exists. The book holds far
    more cells than the tool has probes, and a ``proven`` cell with no probe is a claim nothing
    can ever contradict -- so the size of that set is a reading in its own right, and the number
    a "proven must have a plane" rule would start from.
    """
    section("7. 覆盖面：台账里的格子有几格根本没有判定面（不 measure，纯集合对账）")
    ledger_rel = str(tool_attr(tool, "LEDGER_REL"))
    entries = json.loads((ROOT / ledger_rel).read_text(encoding="utf-8"))["items"]
    probed = {str(probe.item) for probe in tool_attr(tool, "PROBES")}

    def cell(key: str) -> str:
        group, index, _digest = key.split("|")
        return f"{group}|{index}"

    by_state: dict[str, set[str]] = {"proven": set(), "gap": set()}
    for key, entry in entries.items():
        state = str(entry.get("state", ""))
        if state in by_state:
            by_state[state].add(cell(key))
    unreviewed = len(entries) - sum(len(v) for v in by_state.values())
    say(
        f"{ledger_rel}：条目键 {len(entries)} 个；proven={len(by_state['proven'])} "
        f"gap={len(by_state['gap'])} 未审={unreviewed}"
    )
    unprobed_gap = len(probed - by_state["proven"] - by_state["gap"])
    say(
        f"探针数 = {len(probed)}；台账 proven 有面 {len(probed & by_state['proven'])} 格、"
        f"gap 有面 {len(probed & by_state['gap'])} 格、未审有面 {unprobed_gap} 格"
    )
    no_plane = sorted(by_state["proven"] - probed)
    say(f"台账说 proven 却没有任何判定面的格子 = {len(no_plane)}：{', '.join(no_plane)}")
    say(
        "这些格子的 proven 只能靠台账里那条 command 的留档自证；判定面重跑一次也不会碰它们，"
        "所以『门禁每次重量判定面』这句话对它们不成立 —— 上面这个数就是这条闸门的起点"
    )
    gap_no_plane = sorted(by_state["gap"] - probed)
    say(f"gap 且无判定面 = {len(gap_no_plane)}：{', '.join(gap_no_plane) or '无'}")


# --------------------------------------------------------------------------- #
# 面 6：仪器依赖面
# --------------------------------------------------------------------------- #


def imports_of(path: Path) -> InstrumentRead | None:
    """Collect what one instrument imports from the self-developed packages.

    Args:
        path: The instrument file.

    Returns:
        Its reading, or None when the file cannot be parsed.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None
    modules: list[str] = []
    symbols: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith(PRODUCT_PREFIX)
        ):
            modules.append(node.module)
            symbols += [
                f"{node.module}.{alias.name}" for alias in node.names if alias.name.startswith("_")
            ]
        elif isinstance(node, ast.Import):
            modules += [a.name for a in node.names if a.name.startswith(PRODUCT_PREFIX)]
    return InstrumentRead(
        path=str(path.relative_to(ROOT)),
        product_imports=tuple(sorted(set(modules))),
        private_symbols=tuple(sorted(set(symbols))),
    )


def last_change(symbol: str) -> str:
    """The most recent commit that touched a first-party definition of ``symbol``.

    Args:
        symbol: The bare function or class name.

    Returns:
        A one-line commit citation, or a note that no history matched.
    """
    code, out = run_git(
        "log",
        "--format=%h %ad %s",
        "--date=short",
        "-S",
        f"def {symbol}",
        "--max-count=1",
        "--",
        "opendata",
    )
    lines = [line for line in out.splitlines() if line.strip()]
    if code != 0 or not lines:
        return f"(无命中 def {symbol}；git exit={code})"
    return lines[0][:110]


def instrument_face() -> None:
    """List which historical instruments read product private symbols, and who moved lately."""
    section("6. 仪器依赖面：产品私有符号被哪些档案仪器在读")
    reads: list[InstrumentRead] = []
    for pattern in INSTRUMENT_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            read = imports_of(path)
            if read is not None:
                reads.append(read)
    with_private = [r for r in reads if r.private_symbols]
    per_symbol: dict[str, list[str]] = {}
    for read in reads:
        for symbol in read.private_symbols:
            per_symbol.setdefault(symbol, []).append(read.path)
    say(f"被扫仪器文件数 = {len(reads)}；import 了产品私有符号的 = {len(with_private)}")
    say(f"被读的私有符号数 = {len(per_symbol)}")
    for symbol, users in sorted(per_symbol.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        say(f"  {symbol:<46} 被 {len(users)} 台仪器读：{', '.join(users[:3])}")
        say(f"      最近一次定义变化：{last_change(symbol.rsplit('.', 1)[-1])}")
    say(
        "见证（形状变化会咬到谁，上一轮刚实测过一次）：C49 把 `_index` 的返回从 dict 换成 "
        "(indexed, colliding)，门禁第 1 遍即红在 docs/evidence/C48/cross_check_census.py。"
        "上面这张表就是下一次那种红的名单。"
    )


def main() -> int:
    """Run every face and print the readings.

    Returns:
        Zero. This instrument reports; the gate member it describes decides.
    """
    started = time.perf_counter()
    say(f"# census_started={time.strftime('%Y-%m-%dT%H:%M:%S%z')}")
    tool = load_tool()
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8", errors="replace")
    members = gate_members(makefile)
    membership_face(makefile, members)
    handrun_face(members)
    readings, loop_wall = cost_and_reconcile(tool)
    cost_face(readings, loop_wall)
    reconcile_face(readings)
    state_face(readings)
    instrument_face()
    coverage_face(tool)
    say(f"\n# census_wall={time.perf_counter() - started:.1f}s probes={len(readings)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
