# C39 · 把"日志里少了什么"从一次性普查变成门禁成员

本轮的输入不是新需求，是 C38 §9 留下的那一格：那 13 条 loguru 调用**参数根本到不了日志**，
C38 只普查、只登记、没修，理由是"不在本轮判据面上且逐条要读绑定"。本轮做三件事：
①把这 13 条按实测语义逐条修掉；②把 C38 普查工具自己的两个盲区（属性式接收者、只报不判）补上，
做成会置红的门禁成员 `make loguru-check`；③用一次 20 个变异的巡回同时证明
"13 条老写法逐个注回去都会被点名"和"守卫的每一个判定面都在承重"。

---

## 1. 前提不是引用文档，是实测（沿用 C38 的探针，本轮重跑）

两个方向的失败形态**不对称**，这正是缺陷能活下来的原因：

| 写法 | 渲染器 | 实测后果 |
|------|--------|----------|
| `logger.debug("%s …", x)` | loguru | `_logger.py:2055` `message.format(*args)` ⇒ `%s` 模板原样输出，**参数被静默丢掉**，运维读到的是没有值的模板 |
| `logger.error("{} …", x)` | stdlib `logging` | `msg % args` 抛 `TypeError` ⇒ `handleError` 吞掉，**这一条根本不进 handler**，只在 stderr 响一下 |

loguru 侧的机制读源码可确认（它按 `str.format` 语义渲染），stdlib 侧走 `handleError`。
所以"grep 一下 `%s`"不能判定任何一条调用：**必须先解析接收者属于哪一族**。
守卫测试里把那两条渲染路径各自跑成真日志（`tests/test_loguru_render_check.py` 的
`test_arguments_with_no_placeholder_are_lost_on_both` 等），判据落在实测输出上而不是文档措辞上。

## 2. 普查读数（修前 / 修后）

C38 的普查：全树 **12 条裸名 + 1 条属性式** = 13 条参数丢失。
本轮守卫（`scripts/quality/loguru_render_check.py`，606 行）在**修后的树**上的读数：

```
swept   opendata: 191   opendata_http: 313   opendata_fuyao: 9
swept   opendata_client: 2   scripts: 37   tests: 162      （合计 714 文件）
loguru-render-check: calls-with-args=20 findings=0 unjudged=4
OK: every logger call passes arguments its renderer can substitute.
```

`calls-with-args` 的定义是"带模板又传了参数的调用"（`Sweep.calls_with_args` 的字段注释）。
修后的 20 条 = 本轮改对的 12 条 `{}` 写法 + 8 条本来就与所属族一致的写法；
第 13 条（S7）改成插值后**不再传参**，所以它不在这 20 条里 —— 这一点被 S7 的变异读数反向证实：
注回旧写法时那条读数从 20 变 **21**，其余 12 条站点变异都是 20（本来就传参，只是模板错了）。
**判据不是"20 条"这种自增计数，而是 `findings` 必须为 0** ⇒ 任何一条回退到错族模板，
`make gate` 直接红。

## 3. 残面：`unjudged=4` 全在守卫自己身上，运行包内为 0

`--report` 那四行逐条抄在这里（不折算成"应该没问题"）：

```
note  scripts/quality/loguru_render_check.py:212 logger.error: template built at runtime
note  scripts/quality/loguru_render_check.py:246 stdlib.error: template built at runtime
note  tests/test_loguru_render_check.py:185 logger.error: template built at runtime
note  tests/test_loguru_render_check.py:216 box.error: template built at runtime
```

四条都是**探针自己**：`:212/:185` 是 `--probe` 从 `PROBE_CASES` 循环变量里取模板（那正是要测的实验），
`:246/:216` 是 stdlib 那一侧的同一形状。⇒ **`opendata` / `opendata_http` / `opendata_fuyao` /
`opendata_client` 四个运行包内的未判面为 0**，这一句是按根目录分账读出来的，不是推断。
守卫对"读不出来"的接收者和运行时构造的模板**计数并打印**而不是当它不存在，
所以"这一格看不见"的大小始终在输出里。

## 4. 改了什么

### 4a. 13 条日志调用（语义：把参数交给渲染器之前先插值完）

`data_query.py:182/234/593`、`tables.py:524`、`websocket.py:176`、`token_blacklist.py:168`、
`akshare_provider.py:498`、`request_logging.py:39/62`、`security.py:30`、
`scheduler.py:300/454`、`script_service.py:380`。修法**按接收者分两种**，不是一刀切：

* **12 条接收者能判死为 loguru 的**：模板从 `%s` 改成 `{}`，参数原样保留 ⇒
  例如 `logger.warning("diff report unavailable for {}: {}", domain, exc)`，
  这样仍是"模板 + 参数"的写法，但这次渲染器认得它；
* **1 条接收者判不死的（S7，`akshare_provider.py:498`）**：改成 f-string 插值且**不传参数** ——
  `self.logger = logger or _default_logger` 一侧 stdlib、一侧 loguru，
  守卫对它的判据不是"猜一个族"，而是 `receiver family not settled: interpolate the message at the call site`
  ⇒ 插值写法在两族下都正确，这一条现在被门禁钉住。

两种修法的共同点：**输出的字符串内容变多了，控制流与日志级别一处没动**。

### 4b. 门禁成员（Makefile + CODE_QUALITY.md §9）

`make loguru-check`（`Makefile:79`）跑同一份判定，并作为 gate 段的一员（`Makefile:180-181`）
⇒ 门禁成员 **14 → 15**，完整日志的 `===== gate:` 标记数 **15 → 16**。CI 跑 `make gate`，无需另改。

### 4c. A2 连带清理（6 个文件，见 §7）

本轮 9 个改动文件里有 6 个是**旧文件**，按 `scripts/quality/a2_check.py` 的口径
（A2 集合 = 自基线提交以来新增或改动的文件 + 未跟踪文件），**改到它就把它的全部历史债务拖进零容忍集**。
门禁第一跑因此红（`gate-run1.txt`，`GATE_EXIT=2`）。修法是把这些文件本身提到 A2 标准，
不是排除、不是放宽规则：

* `datetime.UTC`（3.11+ API，而 `requires-python = ">=3.10"`）→ `timezone.utc`
  （`scheduler.py`、`websocket.py`、`script_service.py`；CPython 里 `UTC is timezone.utc`，**无行为差**，
  只是把版本下限谎称去掉）；
* `scheduler.py` 缺类型面：`self.scheduler_service` 与四处 `db` 参数按 `TYPE_CHECKING` +
  `AsyncSession` 补注（`SchedulerService` 只在注解里用，按 TC003 移到 `TYPE_CHECKING` 块，
  并且实测过：函数内的**属性目标**注解在 3.13 运行时不求值，所以前向引用安全，
  这也正是 ruff UP037 要求不加引号的那个前提）；
* 补 4 处 D 系列 docstring、`ruff format`、把 109 字符的 housekeeping f-string 拆开、删掉死赋值 `task_name`；
* 两处 `except` 在循环里（PERF203）与 `token_type="access"`（S106/B106 硬编码密码**误报**，
  它是类型标签不是密钥）**各留一行带理由的 `noqa`/`nosec`**：全树本轮净增 3 条
  （`websocket.py:56`、`websocket.py:131`、`scheduler.py:94`），三条都在 §4c 这一张表里可见，
  没有"豁免了就看不见了"。

**没有任何一项靠改配置、加排除目录或放宽规则关掉。**

## 5. 变异巡回（`loguru-mutation.txt`，`TOUR_EXIT=0`，BROKEN=0）

20 个变异：13 个站点变异 + 7 个判定面变异。站点变异要求**门禁置红且只点名被改的那个文件**；
判定面变异要求**对应用例翻红**（摘掉那一面后，守卫自己的测试必须有例站不住）。

| 步 | 摘掉/注回的面 | 读数 |
|----|----------------|------|
| S1…S13 | 13 条已修调用逐个注回 `%s`+参数旧写法 | **13/13 RED 且只点名本文件**（S7 点名的判据文案是 `receiver family not settled`） |
| C1 | `self.<attr>` 退回 `UNRESOLVED`（C38 的老盲区） | RED：`TestAttributeFormFace` 2 failed（`IndexError`，用例确实依赖那一面） |
| C2 | 裸名一律不当日志调用 | RED：`test_arguments_with_no_placeholder_are_lost_on_both` |
| C3 | `verdict_of` 恒回"没问题"（只报不判） | RED：`test_loguru_percent_template_loses_its_arguments` |
| C4 | 只判 loguru 一侧（丢掉另一个方向） | RED：`test_stdlib_brace_template_loses_its_arguments` |
| C5 | f-string 重新被当成**无条件**安全 | RED：`test_finished_message_that_all_passes_arguments_is_disclosed`（见 §6） |
| C6 | `if sweep.findings and not args.report:` → 只打印不置红 | RED：`test_gate_mode_fails_on_a_finding` |
| C7 | 从 `CHECK_ROOTS` 里悄悄摘掉搬运层 313 文件 | RED：`test_every_declared_root_sweeps_files` |
| 还原 | 10 个文件 sha256 逐字节对比基线 | 全部 `OK`，复跑门禁成员 + 守卫 + 27 例全绿 |

巡回脚本自己（`loguru-mutation.py`，402 行）也在 A2 清扫面内（`docs/evidence/**/*.py`），
所以它按同一套工具跑过 `a2_check`。

## 6. 巡回抓到的不是变异，是守卫的一处**没被钉住的面**

第一跑（`loguru-mutation-run1-unpinned-face.txt`）里 **C5 是 GREEN**：
守卫对 `ast.JoinedStr`（f-string）模板**无条件早退**，理由是"插值写法没有参数可丢"。
写判据时这句话是对的**一半**——`logger.debug(f"…{x}", exc)` 这种"消息已插值、却又传了参数"的写法，
loguru 侧会把 `exc` 丢掉（`format` 无占位符），stdlib 侧会抛 `TypeError`。
当时的树上有 **0 条**这种调用 ⇒ C5 摘不掉任何东西 ⇒ 巡回报 GREEN。

处理方式是承认这是**判据漏洞而不是变异选错**：

1. 先量（`/tmp` 一次性脚本，跑完删除）：全树 `JoinedStr` + 传参的调用确实为 0 条 ⇒ 现有代码不受影响；
2. 收紧 `_site_of`：f-string 不再无条件早退，只有"没有参数可丢"才放过；
   带参数的插值消息按 `template built at runtime` **计入未判面并打印**（不猜哪一族会怎么错）；
3. 补一条 witness 用例钉住这一格（`test_finished_message_that_also_passes_arguments_is_disclosed`），
   并把它写成 C5 的新靶；
4. 重跑 ⇒ 20/20 全 RED（`loguru-mutation.txt`）。

两跑的逐行差异留在 `loguru-mutation-runs.diff`。
**这一格值得单独记：一次"证明判据有牙"的巡回，产出不只是"判据有牙"，还包含它自己漏了什么。
GREEN 的变异在这里是发现，不是失败。**

## 7. 门禁与单测

* `unit-suite.txt`：`python -m pytest -m "not e2e" -n 8 --no-cov -q` ⇒
  **3031 passed / 6 skipped in 46.85s**，`SUITE_EXIT=0`（读自文件内部）。
  3031 = C38b 的 3004 **+27**，与本轮新增的守卫测试文件条目数完全对上；
  `6 skipped` 与 C38b 逐格相同（skip 面未动，归因沿用那一份，未新增豁免）。
* **棘轮已冻结到新低点**（`docs/quality/ratchet.json`，`ratchet.py --update`）：
  `ruff_selfdev 277→243`、`mypy_selfdev 21→11`、`bandit_selfdev 4→3`、
  `ruff_ported 2145→2144`、`direct_http_ported 1045→1044`；
  逐根文件数 `opendata 174→191`、`opendata_fuyao 8→9`、`scripts 15→37`、`tests 139→162`。
  冻结的理由：这五格的下探是本轮 §4c 清理挣来的，不冻的话下一次编辑可以把债务涨回旧上限而不触红。
* `gate-run1.txt`（3,966 行完整未裁剪，`GATE_EXIT=2` 读自文件内部）：
  **红在 `a2-check`** —— 31 条 ruff + mypy + bandit，全部是 6 个被本轮改到的旧文件的历史债务（见 §4c）。
  这份红档**保留不删**：它是"A2 集合按改动计算"这一口径的实际代价，也是 §4c 那些改动的来由。
* `gate.txt`（回填前的最终代码树，6,912 行完整未裁剪）：**16 个 `===== gate:` 标记**
  （15 个成员 + `PASSED`，上一轮是 15），`GATE_EXIT=0` 读自文件末行而非后台任务退出码。逐成员读数：

| 成员读数 | 本轮 | 上一轮（C38b） |
|----------|------|----------------|
| `loguru-render-check` | `calls-with-args=20 findings=0 unjudged=4` | **该成员不存在** |
| 单测 | `3031 passed / 6 skipped in 57.75s` | 3004 / 6 |
| 覆盖率 | `TOTAL 10945 1119 2694 289 88.30%`，`Required test coverage of 84% reached` | 10942 1118 2692 288 88.31% |
| AC-17 条目 6 判据面（`opendata/pipeline` 逐行相加） | **2985/3256 = 91.68%** | 2985/3256 = 91.68%（一字未变） |
| `a2-check` | `A2 files: 329` → `OK: A2 files meet the full A2 standard (ruff + format + mypy + bandit).` | 318 |
| `quality-ratchet` | 五项**全部等于快照**（`243 / 11 / 3 / 2144 / 1044`），无 `improved` 也无 NOTE | 五项全部"(improved)"+ 冻结提示 |
| `public-api-quality` | `530` 个 callable，docstring `100.0% (530/530)`、annotation `100.0% (530/530)` | 522 |
| `ledger-check` | `census: items=130 proven=8 gap=3 unreviewed=119 ticked=8` | 同（**本轮不翻勾**） |
| `secret-check` | `no leaks found` | 同 |
| 前端 e2e | `18 passed (6.5s)` | 同 |

**为什么跑了第二遍**（`gate-post-backfill.txt`）：`ledger-check`／`brand-check` 两个成员的输入
就是验收文档本身，而 `gate.txt` 跑在 v5.10 的文档上 ⇒ "门禁绿"只描述它跑那一刻的文档面。
第二遍（v5.11 回填＋本档案写入之后）：6,916 行、**16 个标记逐个在位**、`GATE_EXIT=0`、
`3031 passed / 6 skipped in 65.86s`、`TOTAL 10945 1119 2694 289 88.30%`、`A2 files: 329`、
`census: items=130 proven=8 gap=3 unreviewed=119 ticked=8`、前端 `18 passed`、`no leaks found` ——
**两遍的整张覆盖率表逐行 `diff=0`（0 行差异）** ⇒ 两遍之间的改动只落在文档面，没挪动判据面。
本 README 定稿后的字句更正不再触发第三遍（它不是任何成员的输入；`.py` 档案才是，而那些已在
`gate.txt` 之前定稿）。

**为什么全局掉了 0.01 个百分点**（不是"顺手少测了一条"）：`TOTAL` 的 +3 语句 / +1 miss /
+2 branch / +1 partial **全部落在 `opendata/services/scheduler.py` 一格**（238→241 / 60→61 /
64→66 / 4→5，该文件 74.17%→73.94%），来自 §4c 为满足 mypy 而加的
`if TYPE_CHECKING:` 块 —— 块里那行导入运行时永不执行（miss +1），该分支的 True 弧也永不走
（partial +1）。类型面挣到的、覆盖率面付了账，这一格如实留在 §8 的边界里。
`opendata/api/websocket.py` 80 语句不变（只换了导入写法），未覆盖行号整体 +1 位移。

## 8. 登记，不声称

* **台账本轮不翻任何条目**：§2–§6 的 130 条里没有任何一条判据描述"日志参数必须到达渲染器"，
  本轮挣到的是一格**新的门禁成员**（AC-17 的"一票否决"面在 §10 说明里如实加一句），
  不是把某条 ☐ 变成 ☑。故 AC 条目级计数一字不动（AC-17 仍 5/10、AC-16 仍 2/9）。
* **`datetime.UTC` 还留着 10 个文件**：`models/{task,user,interface,data_table,data_script}.py`、
  `api/{auth,tasks}.py`、`services/{execution,notification,retry}_service.py` 仍在
  `from datetime import UTC`（3.11+），而 `requires-python = ">=3.10"` ⇒ **声称支持 3.10 的包
  在 3.10 上会 `ImportError`**。本轮只修了自己碰到的 3 个文件（A2 口径的连带，不是全树清扫），
  这 10 个登记为**开放缺陷**：真正的收口要么全树改，要么把 `requires-python` 提到 `>=3.11`
  并让 `pyproject.toml` 与代码一致——后者动依赖声明，需评审。
* **一处用覆盖率换类型面**：§4c 给 `scheduler.py` 加的 `if TYPE_CHECKING:` 块带来
  1 条运行时永不执行的语句与 1 条永不走的分支 ⇒ 全局 88.31% → **88.30%**（该文件 74.17% → 73.94%）。
  本轮**不**为把它抹平而写空断言，也不改 `exclude_lines`；AC-17 条目 6 的判据面（pipeline 包）
  逐行相加仍是 2985/3256 = 91.68%，未受影响。
* 守卫**不判**"这行日志该不该打"，只判"传进去的参数到不到得了渲染器"。
  本轮也不改任何日志级别、不改控制流（§4a 那 13 条改完，输出的字符串内容变多了，行为面没动）。
* `calls-with-args=20` 这个数会随代码自然增长；判据始终是 `findings=0`，
  所以这个数**不是**阈值、也不会被拿来当成绩。
* 本轮全程离线：未连数仓、未执行 DDL／写入、未读取或打印任何密钥值（`--probe` 只操作
  本进程 loguru 的全局 sink，跑完恢复 `sys.stderr`）。

## 9. 档案清单

| 文件 | 内容 |
|------|------|
| `loguru-mutation.py` | 20 变异巡回驱动脚本（A2 集合内，`a2_check` 已过） |
| `loguru-mutation-run1-unpinned-face.txt` | 第一跑：C5 GREEN ⇒ 暴露守卫未钉住的那一面 |
| `loguru-mutation.txt` | 第二跑（最终树）：20/20 RED、BROKEN=0、sha256 还原全 OK，`TOUR_EXIT=0` |
| `loguru-mutation-run2-pre-a2-cleanup.txt` | §4c 清理**之前**的那一跑（基线哈希与最终树不同，故另存） |
| `loguru-mutation-runs.diff` | 两跑逐行差异（含 C5 由 GREEN 变 RED 的位置） |
| `unit-suite.txt` | 全量单测完整未裁剪 + 环境头（`3031 passed / 6 skipped`） |
| `gate-run1.txt` | 门禁第一跑，红在 `a2-check`（`GATE_EXIT=2`），3,966 行 |
| `gate.txt` | 回填前最终代码树门禁，6,912 行、16 标记、`GATE_EXIT=0`，完整未裁剪 + 环境头 |
| `gate-post-backfill.txt` | v5.11 回填后的确认遍，6,916 行；与 `gate.txt` 的覆盖率表逐行 `diff=0` |

相关：C38 §9 那一格是本轮的输入；`docs/evidence/C38/loguru-census.txt` 是修前的普查底稿。
