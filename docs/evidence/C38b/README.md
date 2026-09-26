# C38b · 把 `opendata/pipeline` 单元面补到 ≥90%，以及一次误跑真库 e2e 的事故

判据来源：`docs/迭代计划/迭代1-重构数据中台/验收文档.md` §4 条目 6（核心模块覆盖率 ≥90%）
与 AC-17。任务 #48 的口径是：**新增真实不变量用例把 pipeline 面推到 90% 以上；
不得改 `exclude_lines`／加 `pragma: no cover` 凑数；不得写空断言。**

## 1. 口径与起点（先把"90%"变成可复算的数）

覆盖率那一列是**组合 unit 比率**，不是 `1 − miss/stmts`：
`(语句 − miss + 分支 − partial) / (语句 + 分支)`。度量面 = `opendata/pipeline/*.py`
共 **27 个模块、3256 unit**（每份档案都能重算，见 §4）。

| | covered unit | 读数 |
|---|---|---|
| C38a 门禁（HEAD `911fbe4`，同一规则复算五份档案全部一致） | 2818 / 3256 | **86.55%** |
| ≥90% 那条线要到的数 | ≥2931 | ⇒ 还需 **+113 unit** |
| 本轮结束时（`coverage-probe3.txt`） | 2985 / 3256 | **91.68%**（+167 unit） |

起点 86.55% 不是另跑一遍量的，是从 C38 已有的五份档案
（`gate-c38a.txt` / `gate-c38a-final.txt` / `gate-c38a-post-backfill.txt` /
`stability-run1.txt` / `stability-run2.txt`）逐行复算出来的 —— 五份都给出 2818/3256，
所以这一格不是单次往返。

## 2. 改了什么：82 条用例，0 行产品代码

只动了五个测试文件（`tests/test_notify_unit.py`、`tests/test_ods_writer.py`、
`tests/test_watermark.py`、`tests/test_factor_builder_unit.py`、
`tests/test_pipeline_templates.py`）。

* 这五个文件在 HEAD 上收集到 **50 unit + 18 e2e**；现在 **132 unit + 18 e2e** ⇒ 新增 **82 条单元用例**；
  与全 suite 对照自洽：C38a 门禁 `2922 passed / 6 skipped` → 本轮 `3004 passed / 6 skipped`（+82，skipped 一格没变）。
* **产品代码一行没改**：五个 `opendata/pipeline/*.py` 的 sha256 在变异巡回的
  [0] 与 [终态] 两处逐字节复核相同（`mutation-proof.txt`）。
* 唯一一处"逻辑"改动在测试侧的 e2e fixture 清理条件（事故根因，见 §6）。

补的是哪一类东西：notify／ods_writer／watermark／factor_builder 四个模块的**编排逻辑**
以前只被 `@pytest.mark.e2e` 类覆盖（真 MySQL，门禁从不跑），单元面只看纯函数与 SQL 构造。
所以它们的"先落水位再推送""每块先 TRUNCATE 再 upsert""limit 夹取""闭窗前扩"这些**顺序与边界**
在门禁眼里等于不存在 —— 与 C29/C33/C35 同一个病灶。本轮把它们改成可测的那一半：
一个**记录型假仓**（记语句原文 + 绑定参数 + 调用顺序），MySQL 自己决定的那一半
（`ON DUPLICATE KEY` 语义、临时表生命周期、`seq` 自增）明确留给 e2e 面并写在 docstring 里。

## 3. 五张面

| 模块 | 语句 | 分支 | unit | 改前 | 改后 | 补上的不变量 |
|---|---|---|---|---|---|---|
| `notify.py` | 71 | 12 | 83 | 51.81% | **96.39%** | 先 `begin:record` 后 `publish` 的顺序；count/read 都按 `batch_id` 收敛；推送失败吞掉但水位已落 |
| `ods_writer.py` | 118 | 44 | 162 | 63.58% | **98.15%** | staging 路径 10 条语句的**精确顺序**（create → [truncate→fill→upsert]×N → drop）；NaN→NULL；`reject` 时 drop 仍在 finally 里跑 |
| `watermark.py` | 53 | 6 | 59 | 72.88% | **94.92%** | `bounded = max(1, min(limit, MAX_REPLAY_LIMIT))` 两端；按 `seq` 而非 `created_at` 排序 |
| `factor_builder.py` | 77 | 12 | 89 | 52.81% | **96.63%** | 闭窗按 `LOOKBACK_DAYS` 前扩；全零事件丢弃；qfq/hfq -money 对照（末根 1.0、首根 98.5/100） |
| `templates.py` | 138 | 24 | 162 | 62.35% | **94.44%** | ods 读窗用**源侧字段名**（`日期` 而非 `trade_date`）；窗口外的受影响键不静默丢；writer 收到的是源侧业务键，notify 与 writer 共用同一个 `batch_id` |

（读数取 `docs/evidence/C38/gate-c38a-final.txt` 与 `coverage-probe3.txt` 的同名行，
五个模块的 unit 合计 555；"改前"那一列里 `ods_writer` 是 40 miss + 7 partial，`templates` 是 46 + 7，
本轮之后只剩 `37-39`、`35-41` 那几行 `TYPE_CHECKING` 导入。）

没被 100% 关掉的最后一格是 `notify.py:38-40`（`TYPE_CHECKING` 块里的导入语句）。
它测不到是因为运行期不存在，不是因为没人管 —— **没有**用 `exclude_lines`/`no cover` 把它排掉。

## 4. 三次量化（三份完整档案）

| 档案 | 用例数 | pipeline 面 | 距 90% 线（2931 unit） |
|---|---|---|---|
| `coverage-probe1.txt` | 2934 passed / 6 skipped | 2853 → **87.62%** | −78 |
| `coverage-probe2.txt` | 2982 passed / 6 skipped | 2941 → **90.33%** | **+10** |
| `coverage-probe3.txt` | 3004 passed / 6 skipped | 2985 → **91.68%** | **+54** |

probe2 已经过线，但余量只有 10 个 unit（0.33 个点）—— 那一读是"贴着线"，
一次 `-n 8` 抖动就能掉回去。所以没有在那里收手，把 `templates.py`（162 unit，与 `ods_writer` 同为最大的一张）
也补了，余量抬到 54 unit。这三个读数都是**单元面**（`-m "not e2e"`）；
门禁那一遍的读数在 §7，按同一规则从门禁日志复算。

## 5. 非恒真性：10 个变异，10 个红

`mutation-proof.txt`（`MUTATION_SUMMARY cases=10 unexpected=0`）。每个变异按字符串唯一命中
（锚点不是 1 次就报 BROKEN 不注入），日志同时打印删/增的每一行，跑完按 sha256 还原并复核。

M1 record 挪到 publish 之后 ⇒ 2 红；M2 `finally` 的 DROP 变 `pass` ⇒ 2 红；
M3 删掉每块 TRUNCATE ⇒ 1 红；M4/M5 limit 不再夹取 ⇒ 3/2 红；M6 按 `created_at` 排序 ⇒ 1 红；
M7 闭窗不前扩 ⇒ 2 红；M8 全零事件不丢 ⇒ 1 红；M9 窗口过滤用契约字段名 ⇒ 1 红；
M10 只返回窗口内的行 ⇒ 1 红。

**这一节的跑法本身是被修过的**，见 §6：上一次运行漏了 `-m "not e2e"`。
现在日志头部固定写着这行命令，并且基线那一节的读数带 `deselected` 计数
（`test_ods_writer.py 30 passed, 5 deselected`、`test_watermark.py 24 passed, 13 deselected`）
—— 这就是"e2e 面这一遍没跑"的现场证据，不是我说它没跑。

**这一节跑了两遍，因为第一遍的脚本没进档案**：`mutation-proof-runA-tmp-runner.txt` 出自 `/tmp` 里的草版 runner
（判据读数有效，但没人能重跑它）。本轮把 runner 收进 `mutation-proof.py` 并原样重跑，两份日志逐行对照在
`mutation-proof-two-runs-diff.txt`：**20 行差异全是 pytest 的耗时小数**，`cases=10 unexpected=0`、10 次 RED、
0 次意外 GREEN、5 个文件 sha256 还原一致、`deselected` 计数 5/13 —— 两边逐项相同。
归档脚本相对草版只改了三处（函数文档字符串、`REPO` 改由 `__file__` 推导、版本打印抽成 helper 以过 PERF203），
**十个变异的锚点与 pytest 旗标一字未动**。

## 6. 事故：变异巡回的第一次运行误跑了真库 e2e

**机制**：runner 逐个测试文件调 pytest，命令里**没有** `-m "not e2e"`。
每个文件同时挂着单元面和 e2e 面，于是 `TestNotifierAgainstMysql`、`TestLiveUpsert`、
`TestWatermarkAgainstMysql` 都真跑了。证据就在第一次那份日志自己身上
（`mutation-proof-run1-e2e-polluted.txt`，原样存档没改）：

```
tests/test_ods_writer.py   rc=0 35 passed in 1.30s      ← 35 = 30 unit + 5 e2e
tests/test_watermark.py    rc=0 37 passed in 18.69s     ← 37 = 24 unit + 13 e2e（18 秒就是真库往返）
```

**违反了哪条既有规则**：本项目的"不得临时跑 warehouse e2e"（历史上这么跑过一次，
删掉了生产 `dwd_stock_daily`，并让 app lifespan 对生产 MySQL 做过 DDL）。这是本轮最严重的部分。

**实际发生了什么（能证明的 / 只能推断的，分开写）**：

* 已测（跑后只读复核，与修正后的第二次运行前后一致）：`batch_watermark` 总行数 = **0**、
  `MAX(seq)` 为空、库内 `_stg_%` 临时/探针表残留 = **0**；
  `dwd_stock_daily` = 719,316 行、`ods_stock_daily_ths` = 10,310,289 行。
  ⇒ **价格数据面没有被本轮 e2e 写过**：那三个 e2e 类只写 `batch_watermark` 和它自己建的
  `_probe_ods_writer` / `test_notify_probe`（fixture 里 create/drop 同名表）。这一句是**机制读码 + 现状读数**，
  因为我没有本轮之前的行数快照，不能写成"未变"的证据。
* 只能推断：两个 warehouse fixture 会执行 `alembic upgrade head`。现在
  `alembic_version_data = 0005_dwd_stock_adjust`（head），而 0004/0005 两个迁移建于 2026-09-23、
  其表结构早已在库上，所以 `upgrade head` 应是**不改 schema 的 no-op**。**这是推断，不是直接证据**
  （没有运行前的版本读数）。
* 真实损失面：`TestNotifierAgainstMysql` 的 teardown 执行
  `DELETE FROM batch_watermark WHERE domain = 'stock_daily'` —— 它删的是**整个域**，
  不是自己写的那几行。该表 `AUTO_INCREMENT = 107` 说明历史上累计写入过约 106 个 seq，
  但由此**无法判断**这次是否删掉了属于真实批次的行（没有跑前快照）。
  `batch_watermark` 因此不是可依赖的审计面 —— 登记为 §8 的后续项，本轮不声称它"没丢东西"。

**根因修法（已改，在产品代码之外）**：`tests/test_watermark.py` 的那个 fixture 原先按 domain 全删；
现在 setup 先读 `COALESCE(MAX(seq),0)`，teardown 只删 `WHERE seq > :from_seq`，
即只回收本次运行追加的行，注释写明"按序号界定清理范围，永不按 domain"。

**为什么这次会漏**：门禁那条命令一直带标记，而新写的 runner 是我自己手拼的 pytest 命令。
防复发不是"下次记得"，是把命令原文打进档案头部、并让 `deselected` 计数出现在读数里（§5）。

**那份日志的文件名本轮被改过，内容没改**：`mutation-proof-run1-含e2e污染.txt` → `mutation-proof-run1-e2e-polluted.txt`
（84 行、sha256 `cc19a403e1f03dc9…b5c1381`，改前改后逐字节同一）。原因是量出来的：`git ls-files` 默认
`core.quotepath=true` 会把非 ASCII 路径转义成 `"…\345\220\253…"` 输出，而 `acceptance_ledger_check._tracked()`
拿它的行**逐字比对** evidence ⇒ 一个中文名证据文件即使已经 staged 也会被判"not tracked by git"。
这条登记成 **checker 自己的可读面缺口**（坏方向是红、不是假绿），收口方式应是 `git ls-files -z`；本轮不顺手改门禁成员的代码，
只把档案名换成 ASCII 并把这一格写在这里。

## 7. 门禁

| 遍次 | 档案 | 行数 | `GATE_EXIT` | 用例 | pipeline 面（同一规则复算） |
|---|---|---|---|---|---|
| 第一遍（回填前的最终代码树） | `gate-run1.txt` | 6,818（15 个 `===== gate:` 标记齐全） | `0`（读自文件末行，非后台任务退出码） | `3004 passed, 6 skipped in 58.12s`；`TOTAL 10942 1118 2692 288 88.31%`；`Required test coverage of 84% reached` | **2985/3256 = 91.68%**（`A2 files: 318`、ratchet `OK`、public-api `522/522`、secret `no leaks found`、`census: items=130 proven=7 gap=4 unreviewed=119 ticked=7`＝回填前） |
| 第二遍（v5.10 回填＋台账翻勾＋档案 staged 之后） | `gate-run2.txt` | 6,831（15 个标记齐全） | `0` | `3004 passed, 6 skipped in 56.87s`；`TOTAL 10942 1118 2692 288 88.31%` | **91.68%**（与第一遍**整张覆盖率表逐行 `diff=0`**）；`A2 files: 318`、`census: items=130 proven=8 gap=3 unreviewed=119 ticked=8` |
| 第三遍（最终树：两份档案脚本进了 a2-check 清扫面） | `gate.txt` | 6,841（15 个标记齐全） | `0` | `3004 passed, 6 skipped in 60.25s`；`TOTAL 10942 1118 2692 288 88.31%`；`A2 files: 320`（＋2 ＝ 本轮两份脚本）；`census: items=130 proven=8 gap=3 unreviewed=119 ticked=8`；前端 `Test Files 14 passed (14)`／playwright `18 passed (6.4s)` | **91.68%**，且**三遍的整张覆盖率表（各 202 行）两两 unified-diff `diff_lines=0`**（`coverage-faces-across-runs.txt` [2]） |

第三遍不是"多跑一遍好看"：本轮在第二遍之后往 `docs/evidence/C38b/` 里放了 `coverage-face-recompute.py`（把三张面的复算变成可重跑命令）
和它的输出 `coverage-faces-across-runs.txt`，而 `a2-check` 的清扫面含 `docs/evidence/**/*.py` ⇒ 第二遍那句"门禁绿"
描述的是它跑那一刻的树。三遍之间的 `opendata/`、`tests/`、`scripts/` 一字未动。

**这一节的读数有一个已修正的生成错误**（不是排版，是读数）：`[2]` 那一节第一次生成时用 shell 变量拼文件名，
三个 `sed` 都报 `No such file or directory`，于是"两两 diff=0"是**空文件对空文件**的空diff。改成脚本按
`Name`→`TOTAL` 区间取表后重算，`diff_lines=0` 这次是真的。原始那三行假读数没有留在档案里，
但**这一格错误的形状登记在此处而不是抹掉**：`diff=0` 这种"看起来最干净"的读数，恰恰是空输入最容易给出的结果。

## 8. 登记，不声称

* **AC-17 条目 6（核心模块覆盖 ≥90%）已翻勾，判据面是门禁自己那一遍的读数**：`gate-run1.txt` 打印的 term 表按
  `(stmts−miss+branches−partial)/(stmts+branches)` 复算出 pipeline **2985/3256 = 91.68%**，同一遍的 `coverage.xml`
  独立复算同一张面得同一读数、27 个文件逐个四元组 **0 处不符**（`coverage-report-receipt-run1.txt` [2]）。
  探针那三次跑只是**支持证据**（用来说明余量），不是判据。
  这一勾吃三个风险，逐条写清而不是含糊过去：
  ①"报告（xml+html）**非空归档**"按"报告产出了、非空、可指纹核对"来读（`coverage.xml` 500,564 B、
  `htmlcov/index.html` 80,533 B），而 `.gitignore:40/:49` 让**本体不入仓** ⇒ 若验收读法要求仓内有本体，
  这半句按那个口径未闭合，**该勾应退回**；
  ②"A2 新代码 ≥85%"的分母是本轮定义的（A2 文件集 ∩ 覆盖率面文件 = 148/318 ⇒ 90.95%），
  它是**可复算的口径**而不是 `a2_check` 自己产出的数，换一种分母（例如只算本轮新增行）读数会变；
  ③覆盖率读数在 `-n 8` 下的语句级可复现性只到 C38a 那个强度（五遍一个读数 ≠ 已证确定）。
* pipeline 仍有 **20 个模块没关满，共 250 unit** 可回收
  （按可回收量：`jobs.py` 31、`dwd_merge.py` 28、`retention.py` 18、`dump_import.py` 17、
  `cross_check.py` 16、`freshness.py` 15、`runner.py` 14、`partitions.py` 14、`ddl.py` 12、
  `cross_check_service.py` 12、`trading_calendar.py` 11、`subscription.py` 11、`diff_report.py` 10、
  `retry.py` 8、`query.py` 8、`patrol.py` 7、`factors.py` 6、`key_health.py` 5、`alerts.py` 4、
  `table_page.py` 3）。本轮没补它们，也没为凑数写空断言。
* **新登记的缺口**：`batch_watermark` 不是持久审计面（历史上任何一次 warehouse e2e
  都会按 domain 抹掉它）。修法要么让 notify 的 e2e 用 `test_` 前缀域，要么给它加一张快照/回放表；
  需要单开一轮，本轮只把 fixture 的清理范围收窄。
* **另一条新登记（工具面）**：`acceptance_ledger_check` 的"证据必须被 git 跟踪"用 `git ls-files` 的**行文本**比对，
  而它默认按 `core.quotepath=true` 转义非 ASCII 路径 ⇒ 中文文件名的证据档案即使已 staged 也会被判未跟踪。
  本轮以改名规避（见 §6），收口（`-z`）连同 §6 那条防复发规则一起留给下一轮门禁工具面。
* 事故不抹：第一次那份日志按原样在档案里（`mutation-proof-run1-e2e-polluted.txt`），
  没有改、没有删，也没有回头修改任何往轮档案。
* **第三遍门禁之后还动过验收文档的字句**（把"两遍"改成"三遍"、补第三遍读数、补 §7 那格生成错误的登记）。
  按 C38a 定下的口径，**这不再触发第四遍门禁**，改为逐个复算受影响的成员并把读数落档
  （`post-backfill-members.txt`：`ledger-check`＋`brand-check`＋`a2-check`，三个都在同一棵树上重跑、`rc=0`）。
  这一格的边界要读准：它证明的是**这三个成员**在最终文档树上仍然绿，不等于"整份门禁在最终文档树上重跑过"。

## 9. 档案清单

| 文件 | 内容 |
|------|------|
| `coverage-probe1/2/3.txt` | 三次量化的完整覆盖率表（`pytest tests -m "not e2e"` 那一面，未裁剪） |
| `mutation-proof.py` / `mutation-proof.txt` | 归档版 runner 与它这一遍的日志：头部是固定 pytest 命令 + sha256 基线，基线节带 `deselected` 读数 |
| `mutation-proof-runA-tmp-runner.txt` | 同一巡回的**第一遍**（脚本当时在 `/tmp`、不可重跑）；与归档版逐行对照见 ↓ |
| `mutation-proof-two-runs-diff.txt` | 两遍日志的 diff（20 行差异全是耗时）＋ 计数核对（`cases=10 unexpected=0`／10 RED／还原一致 10／`deselected 5,13`） |
| `mutation-proof-run1-e2e-polluted.txt` | **事故那一次的原始日志**（漏 `-m "not e2e"`，e2e 真跑了），§6 的证据线出自它 |
| `coverage-report-receipt-run1.txt` | 条目 6 三个分句各自的可复算读数：[1] 报告非空＋尺寸＋sha256 ＋ [2] XML↔term 逐文件对账 ＋ [3] 三张面 ≥90% ＋ [4] A2 分母与 90.95% ＋ [5] 声明的边界（本体不入仓、分母口径） |
| `coverage-face-recompute.py` / `coverage-faces-across-runs.txt` | 把"三张面怎么算"变成可重跑命令，并对两份门禁日志各自的表复算（pipeline 面逐文件 `diff=0`、整表 `diff=0`） |
| `warehouse-state-probe.txt` | 误跑之后与修正重跑前后的**只读**数仓读数（`batch_watermark` 行数/`MAX(seq)`/`AUTO_INCREMENT`、`_stg_%` 残留、alembic 版本、两张价格表行数），证据与推断分栏 |
| `gate-run1.txt` / `gate-run2.txt` / `gate.txt` | 三遍 `make gate` 完整未裁剪输出 + 头部溯源（branch/HEAD/`git status --porcelain`/`python -V`/`node -v`）；为什么要三遍见 §7 |
| `post-backfill-members.txt` | 第三遍之后对验收文档的字句更正 ⇒ 受影响成员（`ledger-check`／`brand-check`／`a2-check`）逐个复算的 rc 与读数，并写明「其余 11 个成员没在这一刻重跑」这一格边界；含三遍被测面未变的三条支撑与 5 个测试文件的 sha256 |
| `a2-compliance.txt` | 本轮两份新脚本在 A2 零容忍面上的逐工具复算（[G] `A2_EXIT=0`／`A2 files: 320`，[G2] 脚本级 ruff+format+mypy+bandit，[R] 三条 `nosec` 的留账而不是隐身） |
