# C41 · 把 AC-2|02 的"文档 claim"改成代码事实：两条元数据契约真的被回补/窗口逻辑引用

输入是 C40 登记的一条**真实开发缺口**，不是新需求。AC-2 条目 2 的判据写的是
「`Instrument` 与 `TradingCalendar` 模型就位，**且被全市场回补与增量窗口逻辑引用**」，
C40 逐条复量时前半句绿（两模型继承 `ContractModel`、5 个字段集/语义/往返节点 5/5 passed）、
后半句实测 **0**：`opendata/pipeline/{runner,jobs,templates,scheduling,partitions}.py`、
`api/pipeline.py`、`services/data_acquisition.py` 里没有任何一处 import 这两个契约，
全库唯一消费者是 `patrol.py`；而 `metadata.py` 的模块文档却写着「回补以刷新
`Instrument` 为第 0 步、增量窗口取自 `TradingCalendar` 的 `prev_trade_date`」——
**文档 claim 与代码事实相反**。

C40 自定的翻正门槛，本轮原样执行、未放宽：**`importers >= 2` 且 5 个单元节点全绿
且 `models = yes`**。不做的事也先说清：不往判据模块里塞一句 `import Instrument` 凑
`importers` 数（那只会把"文档 claim 与代码相反"换成"判据与代码相反"）。

---

## 1. 三处接线：哪一行真的承重

| 面 | 位置 | 承重的问题 |
|----|------|-----------|
| producer（第 0 步） | `opendata/pipeline/templates.py:223 refresh_metadata_backbone` + `:180 BACKBONE_CONTRACTS` | 两条 provider 腿的每一行**必须过 contract 校验**才能进 `dwd_*`；不合格的行走 refuse 分支并计数，不入库 |
| consumer A（增量窗口） | `opendata/pipeline/trading_calendar.py:200 calendar_from_contracts`，由 `:259 warehouse_calendar` 调用 | 读 `dwd_trading_calendar` 时逐行 `TradingCalendar.model_validate`，把 `is_open` 折叠成开/闭市集合，并把契约自己发布的 `prev_trade_date` 留作**独立见证** |
| consumer B（标的宇宙） | `opendata/pipeline/jobs.py:220 landed_instruments` → `:306 derive_universe` → `:455 run_incremental_job` | 读 `dwd_instrument` 时逐行 `Instrument.from_frame`，用它判断一个码是"已不存在"还是"暂时没有行" |

三处各有一个**不引入契约就写不出来**的判据，这是本轮"承重"的口径，不是 import 语句本身：

* **见证判据**（consumer A）：`CalendarView.expected_data_date()` 先按开/闭市集合回走，
  再拿 `prev_trade_dates[day]` 校对（`_with_witness`）。两答不一致即
  `raise ValueError("… disagrees with itself on …")`——一张 `is_open` 与
  `prev_trade_date` 互相矛盾的表是**内部坏了**， quietly 选一个答案正好把它藏起来。
  旧实现只有 `open_days` 一个集合，这一列在 `TradingCalendar` 契约里存在但没人读。
* **弱成员判据**（consumer B）：`drop_inactive_symbols()` 只在**已知日期**排除窗口时才丢弃
  一个码；`list_date`/`delist_date` 为空是**未知**，不是"未上市"（C14/C19 实测
  a-share 目录 36 次读里有 22 次 `list_date` 整列为空、ETF 恒空）。
  裸码跨资产类撞车（`000001` 既是股票也是指数），所以同一裸码在目录里对应多行时
  **原样保留**并计入 `ambiguous_codes`，不拿错那一行的日期去判。
* **不假装知道**（consumer B）：`derive_universe()` 返回 `(symbols, provenance)`，
  `JobResult` 新增 `universe` 字段并入 `as_dict()`——「这批的标的表是怎么来的」
  （`from=caller` / `from=dwd_<domain>`、`catalog=absent|dwd_instrument`、
  `dropped_inactive`、`ambiguous_codes`）从此和 C12 的 `expectation` 一样是运行记录的一部分。
  非空 `symbols=` 按调用方给的**原样用**：修复性跑批要这个码，正是因为派生宇宙不含它。

## 2. 先量出来的一个真缺陷：`_legs` 停在了 `extract_data`

新脚本 `scripts/ops/metadata_backbone_landing.py` 的第一次真机 dry-run 是**错的**，
而且错得不会自己报告：

```
$ python scripts/ops/metadata_backbone_landing.py --asset-type a-share --asset-type a-share-index --days 40
#   dwd_trading_calendar: would_land 240 row(s)     ← 请求 40 天，回了一整张发布表
```

根因：`_legs()` 直接调了 `extract_data`，只走完「取上游原始帧」这一半，
**日历的邻居日期推导与窗口裁剪都在 normalize 阶段**（先推导、后裁剪），
目录页的空表/重复 `thscode`/缺快照日三项形状检查也在 normalize。
所以那种读法会落地一批**没有 `prev_trade_date` 见证**的行——正是 consumer A 判据要吃的那一列。
改为走 `Fetcher.fetch(ctx=…, …)`（`opendata/data/protocol.py:81`，文档化的调用方入口）之后：

```
#   dwd_trading_calendar: would_land 28 row(s), refused 0
  returned span = 2026-08-18 .. 2026-09-24 | inside window = True
  prev_trade_date None count = 0 | next_trade_date None count = 1（末行）
  row[0].prev_trade_date < window.start = True   ← 证明推导发生在裁剪之前
  neighbour chain self-consistent = True
```

`--days 40` 从「无效果」变成「28 行且全在窗内」，同时窗口首行的 `prev` 落在窗口外——
这两条一起构成修复的证据，逐字读数见 `metadata-backbone-dryrun.txt`。

## 3. 门禁在 `a2-check`（第 7 个成员）抓到 4 个 mypy 缺陷（改代码，不改判据）

回填前 23:25 那一遍门禁**红了**：`a2-check`（对本轮改动文件跑 ruff + format + mypy + bandit，
含 `scripts/`）报 4 个类型错。这里更正我自己一个此前的错误认知：曾以为
「mypy 只覆盖 `opendata/`」，实际 a2-check 会把我新写的 `scripts/ops/*.py` 与
被改的 `templates.py` 一起送进 mypy。

四处都是「类型面没跟上真实语义」，不是写法问题：

* `engine` 声明与实际不符 → 签名改为 `engine: Engine | None`，并把「`engine=None` 且没有
  `land` 覆盖」做成 fail-closed `raise ValueError`（此前会一路走到 `_land_backbone(None)`）。
* `to_frame(list[object])` → 给 `BACKBONE_CONTRACTS` 定成 `dict[str, type[ContractModel]]`，
  `_backbone_rows` 用 `BackboneModelT` TypeVar 把「传进来的模型类」与「返回的行类型」绑在一起。
* `dict(row: object)` → 先 `isinstance(row, BaseModel)` 再 `model_dump()`，
  否则 `dict(cast("Mapping[str, object]", row))`；非 Mapping 且非模型的行走 refuse 分支
  （并补了一条用例打这条分支）。
* 未定型的 `merged_at` → `datetime.now(timezone.utc).replace(tzinfo=None)`（与 C35 登记的
  `requires-python >= 3.10` 口径一致，不用 `datetime.UTC`）。

**没有放宽任何一项检查**：没加 `# type: ignore`、没改 `exclude_lines`、没动
`--cov-fail-under`、没给棘轮 `--update`。失败那一遍的完整日志按原样留档
（`gate-run1-aborted-a2-mypy.txt`，`GATE_EXIT=2`，末尾附四处归因与处置）。
类型收口只改写法不改行为：同一命令在同一棵树上重跑，dry-run 读数逐字不变
（`metadata-backbone-dryrun.txt` 末段复核块）。

## 4. 判定面复算（不假设类型改动不影响 importer 正则）

探针的 importer 判据是一条正则 `from opendata\.data\.models import ([^\n(]+)`。
第 3 节改了 `templates.py` 的 import 写法 ⇒ 「数不数得到两条腿」是一格**必须重测**的读数。
23:25 那次采集于 mypy 修复之前，23:37 在 `importers>=2` 门槛下重跑同一命令（探针文件未改）：

```
  - both classes subclass ContractModel in opendata/data/models/metadata.py = yes
  - the five field-set/semantics/round-trip nodes = 5/5 passed
  - backfill / incremental-window modules importing the contracts = 2 (opendata/pipeline/jobs.py, opendata/pipeline/templates.py)
  - modules that only mention the names (docstrings, or a same-named local class) = 0 (-)
VERDICT AC-2|02: proven
```

两次读数**逐字一致**。值得单独记的是 `mention_only = 0`：C40 那一格里它是 2 个
（`jobs.py` 里那个与契约同名不同种的 `TradingCalendar` dataclass、加上文档串里的提及），
本轮 dataclass 改名为 **`CalendarView`** 并明确它是契约的 *projection*——
名字不再冒充契约，"看起来引用了"这一类就不存在了。完整日志：`probe-ac2-02.txt`（含加性更正块）。

判定面在档案落定后又被读了两次，各自收口不同的树：第 4 遍门禁（`gate-post-commit.txt` 末尾）
读的是头两笔 commit 那棵树；本轮 closure（`probe-ac2-02-post-commit.txt`）读的是第三笔 commit
（`7d5c75d`）的树——运行前 `git status --porcelain` 为空，所以这一次对应的确实是**提交里那棵树**
而不是任何一刻的工作树。留档的四次读数（23:25:11、23:37:02、提交后、closure）四个判定节点逐字一致
（`yes / 5/5 passed / importers=2 / mention_only=0 ⇒ proven`）；`probe-ac2-02.txt` 头部还记着
23:24:53 的一次先跑，只作读数确认、未单独留档，所以这里说"四次"而不是"五次"。
另一格会让读者对不上、因此写明：closure 这次探针打印的 `document line` 是 109 而不是前三次的 108——
不是判据变了，是回填 commit 在 §0 多插了一行修订记录、条目本身往下挪一行（文档总行数 378 → 379）。

再往下的那一笔 commit 只把这份 closure 档案与 §4/§8 的文案落库（`opendata/`、`tests/`、`scripts/` 一字未动，
`git show --stat` 可核），所以「proven」所对应的**被测代码树**仍是第 4 遍门禁验过的那一棵——
本轮到此为止，不再用"为最后一笔 commit 再跑一次"制造无限递归。

## 5. 真机只读 dry-run 的 8 条读数

全部来自 `metadata-backbone-dryrun.txt`（完整未裁剪，两条命令 + 两段解释性测量 + 一段更正 + 一段复核）：

1. `--days 40` 生效（240 → 28 行），`inside window = True`。
2. `prev_trade_date` 28/28 行有值、邻居链自洽、首行的 `prev` 在窗口外。
3. `next_trade_date` 只有 1 行为 `None`，在发布覆盖面末行——与适配器 docstring 逐字一致（不猜后继）。
4. 本腿只发布开市日（240 行全 `is_open=True`）；coverage 区间内判闭市依赖「表只存开市日」这一设计事实：
   **259 个工作日 − 19 个未发布工作日 = 240**，读数自洽。
5. **新发现（不掩盖）**：发布尾端在落后。本地钟 2026-09-26（周六，对应北京 09-27 05:2x）读到
   末行 2026-09-24，而彼时"最后一个已完成的交易日"应为 2026-09-25 ⇒ docstring 里
   「ending at the last completed trading day」只是 C13 那一次读数成立、**不是一条规则**。
   后果：09-25/09-26 落在 coverage 之外、由 weekday tier 回答，witness 面没有行可比——
   不报错也不误判，但那句话不再成立，**已回写到 docstring**（本轮唯一一处"文档追代码"）。
6. instrument 两页都通过形状检查、按 symbol 升序、**跨页裸码交集 = 0** ⇒ `(symbol,)` 单键 upsert 不互相覆盖；
   a-share 5,578 行 `list_date` 仅 8 行为 null（C19 曾见整表全空），a-share-index 1,431 行 0 null；两页 status 全 active。
7. 落地面列名/键/追溯列就是 `dwd_merge` 那一份：contract 列 + `source/_merged_at/_diff_flag/_as_of`，
   追溯列 null 计数 = 0，键 = `(symbol,)` 与 `(exchange, date)`。
8. **全程只读**：`engine=None`、`land` 回调只做 `len(frame)`；无 DDL、无 INSERT/UPSERT、未连主库。
   `FUYAO_API_KEY` 只由 `opendata_fuyao` 客户端自行读取，本轮日志不打印其值/长度/前缀。

## 6. 门禁七份日志（五遍计入判定）+ AC-17 覆盖面

| 采集时点（运行前） | 对应哪棵树 | 结果 |
|------|------|------|
| 22:55:35 | `_legs` 修复**之前**的代码树（pipeline 侧接线已完成，ops 脚本还走 `extract_data`） | **PASS**：`GATE_EXIT=0`、16 段标记、`3052 passed / 6 skipped`、`TOTAL 88.28%` → `gate-run1-code-before-legs-fix.txt`。**这份绿色已被本轮自己取代**：它证明的是当时那棵树，而 §2 的缺陷在那之后才修、21→29 的用例也是之后才补全的；按原样保留不删，但不计入判定 |
| 23:25:57 | 回填前（mypy 收口之前） | **FAIL**：`a2-check` mypy 4 错，`GATE_EXIT=2` → `gate-run1-aborted-a2-mypy.txt`。停在第 7 个成员（它自己已打印段头），前 6 个成员全绿 ⇒ 日志里正好 7 段标记 |
| 23:30:35 | 回填前 | **PASS**：`GATE_EXIT=0`，16 个门禁段标记（15 成员 + PASSED）→ `gate-run1.txt` |
| 23:44:28 | 第 2 遍：台账/文档回填、证据 `git add` 之后 | **PASS**：`GATE_EXIT=0`（读自文件内部）、16 标记 → `gate-run2.txt`；`ledger-check` 读数 `proven=17 → 18 / gap=8 → 7 / ticked=18`，两本书同时认下这条翻转 |
| 23:48:58 | 第 3 遍：第 2 遍之后又改了 3 处 markdown 文案（README §6/§9、验收文档 v5.13 行的第 2 遍读数） | **PASS**：`GATE_EXIT=0`、16 标记、`census … proven=18 gap=7 ticked=18 → OK`、`3060 passed / 6 skipped`、`TOTAL 88.35%` → `gate-run3-final.txt`。跑完整门禁而不是只跑 `ledger-check`，是因为 `ledger-check` 只证明文档可解析，其余 14 成员同样要对最终树成立 |
| 23:55:40 | 第 4 遍：**两笔 commit 之后**（`037bb0b` 代码面 + `6c9a8d1` 回填面） | **PASS**：`GATE_EXIT=0`、读数与第 2/3 遍逐字一致 → `gate-post-commit.txt`；同一棵已提交树上另复算了一次 AC-2\|02 判定面，`proven` 不变。这一遍是为了让"绿色"对应**提交里那棵树**，而不是对应我本地某一刻的工作树 |
| 09-27 00:11:07 | 第 5 遍：§6/§8 改成按采集时点列全七份日志、验收文档 v5.13 行与 §10 行的「第 1 遍」改成具体时点之后（改动全部已 `git add`） | **PASS**：`GATE_EXIT=0`、16 标记、`3060 passed, 6 skipped in 61.07s`、`TOTAL 11079 1127 2732 288 88.35%`、`ledger-check` `items=130 proven=18 gap=7 unreviewed=105 ticked=18 → OK … 22 group(s) and 19 §10 row(s) reconcile` → `gate-run5-markdown-final.txt`。**跨零点**：本遍整体落在本地 09-27，而台账/文档里的 `date=2026-09-26` 指的是**测量采集日**（dry-run、probe、前四遍都在 09-26）——两个事实分开写 |

计入判定的五遍成功日志（23:30 / 23:44 / 23:48 / 23:55 / 00:11）原始 summary 逐字可比：
`3060 passed, 6 skipped`（用时分别 62.01 / 60.85 / 60.74 / 60.57 / 61.07s）与
`TOTAL 11079 1127 2732 288 88.35%`（同一行五遍完全相同）。三个覆盖面由同一份 C38b 复算脚本
分别从 `gate-run1.txt`、`gate-run2.txt`、`gate-run5-markdown-final.txt` 重算，读数逐字相同
（`coverage-faces.txt` 的 run2 与 run5 两个补测块），说明本轮接线没有引入并行归属抖动。
被取代的那一遍（22:55）读数不同且应当不同：`3052 passed`、`TOTAL 11015 … 88.28%`
——它跑在 `_legs` 修复与后 8 个用例之前，正是"绿色对应哪一棵树"这句话的意义所在。

计入判定第一遍（23:30）的读数：`3060 passed / 6 skipped`、全量覆盖率 `TOTAL 88.35%`（阈值 84% 未动）、
a2-check **331 文件**四检全 ok、quality-ratchet 五项等于快照
（`ruff_selfdev 243 / mypy_selfdev 11 / bandit_selfdev 3 / ruff_ported 2144 / direct_http_ported 1044`）、
public API **596/596** 双 100%、`ledger-check` `items=130 proven=17 gap=8 unreviewed=105 ticked=17`、
前端 14 files / 106 tests + e2e 18 passed。

用例增量可全额对上：C40 回填后是 `3031 passed`，本轮 **+29**，
逐文件 = `test_pipeline_templates.py 39→49 (+10)`、`test_pipeline_jobs.py 20→32 (+12)`、
`test_trading_calendar.py 12→19 (+7)`；6 个 skip 与 C40 同一批（全部 `push2his/push2delay` em 阻塞域），未新增 skip。

AC-17 三面（`coverage-faces.txt`，沿用 C38b 那份复算脚本、口径逐字不变）：
`opendata/data/ 92.49%`、**`opendata/pipeline/ 91.89%`**（基线 91.68%，+0.21）、`opendata_fuyao/ 95.76%`。

## 7. 已知边界（不要把本轮读成"元数据骨架已落库"）

* **落库动作本轮没有执行**。`--write` 与它需要的 `dwd_instrument` / `dwd_trading_calendar`
  建表是**仓库写操作**，属运维决策面 ⇒ 已并入任务 #52 等用户拍板。
  在此之前 consumer A/B 两条读路如实报告"骨架缺失"（`catalog=absent`、weekday tier），
  这是**设计里的正常态**，不是被绕过的失败。
* **默认 `DwdWriter` 路径只由桩走过一遍**。逐文件核验里 `frame` 的列名/键/追溯列是实测的，
  但真库 upsert 语义（MySQL `ON DUPLICATE KEY`）本轮未跑——与 C41 之前的所有轮次同样受限，如实登记。
* **AC-17 的面与逐文件是两件事**：`opendata/pipeline/` 面 91.89% 达标，
  而本轮改动的两个文件自身在 90% 以下——`jobs.py 86.10%`、`trading_calendar.py 82.76%`
  （`templates.py 95.87%`、`metadata.py 100.00%`）。判据（AC-17 条目 6）量的是**面**，
  本轮**没有**为了逐文件好看去写空断言；这两个文件的缺口登记为后续项。
* **`acceptance_item_probe.py` 的 `BACKFILL_MODULES` 不含 `opendata/pipeline/trading_calendar.py`**
  ——而它恰是真正读日历契约的那个模块（consumer A）。本轮**故意不改探针**：一个 verdict
  依赖的判据文件不能由同一轮顺手改掉。当前结论是在**更窄的清单**上挣到的（`jobs.py` + `templates.py`
  已在清单内且都真引用），所以"importers=2"没有靠扩清单得到；扩清单留作独立一轮。
* 目录读到的 `status` 全 active ⇒ 「目录只含当前在市标的」（C14/C19 结论）在本轮读数里再次成立，
  也因此 `drop_inactive_symbols` 本轮在真机数据上**不会**丢任何码；
  它的有效性目前只由用例证明（含"已知 `delist_date` 排除窗口即丢""裸码多行不丢"两条）。
* 脚本对跨资产类重复 `symbol` 是**静默后写覆盖**（本轮两页实测交集 0，未出事）。
  这是登记项而非本轮修的对象——见 §8 后续。

## 8. 归档清单

| 文件 | 内容 |
|------|------|
| `metadata-backbone-dryrun.txt` | 真机只读 dry-run + 逐行核验 + 发布覆盖面测量 + 一处标注的算法更正 + mypy 收口后复核（完整未裁剪，带运行前环境头） |
| `probe-ac2-02.txt` | AC-2\|02 判定面复算（门槛写在头部 + 23:25 与 23:37 两次读数） |
| `probe-ac2-02-post-commit.txt` | closure 那次：判定面在第三笔 commit（工作树 clean）上的复读，`document line` 108→109 的归因写在头部 |
| `gate-run1-code-before-legs-fix.txt` | 22:55:35｜**不计入判定**：`_legs` 修复之前的代码树，`GATE_EXIT=0` 却读作 `3052 passed / TOTAL 88.28%`；已被取代，按原样保留不删 |
| `gate-run1-aborted-a2-mypy.txt` | 23:25:57｜第 1 遍首跑**失败**原文（`GATE_EXIT=2`，7 段标记停在 `a2-check`）+ 四处 mypy 归因与处置 |
| `gate-run1.txt` | 23:30:35｜第 1 遍（mypy 收口后重跑、回填前）完整日志（`GATE_EXIT=0`，16 标记，`3060 passed`） |
| `gate-run2.txt` | 23:44:28｜第 2 遍：回填后完整日志（`git add` 之后跑，供 `ledger-check` 检查已跟踪路径；`GATE_EXIT=0`） |
| `gate-run3-final.txt` | 23:48:58｜第 3 遍：文案定稿后的完整日志（`GATE_EXIT=0`，读数与第 2 遍逐字一致） |
| `gate-post-commit.txt` | 23:55:40｜第 4 遍：**两笔 commit 之后**对已提交那棵树的完整日志（`GATE_EXIT=0`；末尾附同一树上的 AC-2\|02 判定面复算：`importers=2 / mention_only=0 / VERDICT proven`，并披露本遍跨零点）。文件名不叫 `gate-run4` 是因为它的用途写在名字里——对应提交里那棵树 |
| `gate-run5-markdown-final.txt` | 09-27 00:11:07｜第 5 遍：遍次表述定稿后的完整日志（`GATE_EXIT=0`，16 标记，读数与前四遍计入判定者逐字一致；末尾附 §6 那处文案改动之后重跑的三个读文档成员） |
| `coverage-faces.txt` | AC-17 三面复算（run1 / run2 / run5 三份日志各算一次）+ 本轮改动模块逐文件读数 + C38b 基线对照 |
| `README.md` | 本文件 |

后续登记（不在本轮做）：①`dwd_instrument`/`dwd_trading_calendar` 建表与 `--write`（用户拍板）；
②把 `pipeline/trading_calendar.py`、`pipeline/freshness.py` 纳入探针的 `BACKFILL_MODULES`
（须由不依赖该判据的一轮来做）；③`jobs.py`/`trading_calendar.py` 自身补到 ≥90%；
④落地脚本对重复 `symbol` 由静默覆盖改为显式拒绝或分键。

## 9. 本轮自己的五处记录错误（已更正并披露）

**（一）provenance 头的 HEAD 一栏写成转述** 而不是逐字输出：

```
HEAD : 063a6e4  docs(acceptance): v5.12 C40 条目级对账与台账回填（AC-1/AC-2 15 条）   ← 凭记忆转述，错
```

`git log -1 --format=%s` 的真实输出是
`docs(acceptance): v5.12 回填 C40 AC-1/AC-2 条目级台账（9 条 proven、6 条 gap，条目级 5/10 与 4/5）`。
哈希 `063a6e4` 一直正确 ⇒ **commit 身份未受影响**，但"provenance 头是运行前采集的真实读数"
这条口径被一行手打的字符串破坏了，而这正是本轮用来判别人读不读得到证据的那条纪律。
归档前把 4 处统一改成逐字输出（`gate-run1.txt`、`gate-run1-aborted-a2-mypy.txt`、
`metadata-backbone-dryrun.txt`、`probe-ac2-02.txt`），并把错误本身记在这里而不是悄悄改掉；
`gate-run2.txt` 的头改由命令注入 `git log -1 --format='%h %s'`，不再手打。

**（二）归档日志的 footer 自己污染了自己。** 我在 `gate-run2.txt` / `gate-run3-final.txt`
末尾写的读数列里，为了描述"16 个门禁段标记"把这串标记**按原样打了进去**，
于是「数段标记的那条 grep」（pattern = 5 个等号紧接 `gate:`）对这两个文件读出 **17**——一条为了证明绿色的计数
被写日志的人改成不绿色的数。已把两处散文里的字面串改成 `=====` + `gate:` 的分写形式，
现在逐行首匹配（同一串 pattern 加 `^` 锚）的计数在七份门禁日志里分别是
16 / 16 / 16 / 16 / 16 / 16 / 7（失败那遍停在 `a2-check`，7 段即其真实进度）；
本轮其余三份 `.txt`（dry-run、probe、coverage 复算）不跑门禁，计数为 0。
教训与前一条同型：**provenance 与读数只能由命令注入，不能由人复述**——
无论是复述一个 commit 标题，还是复述一段本应被机器数的标记。

**（三）用 shell 生成头/尾注时，双引号里的反引号被当成命令替换执行了。** 两次：
`gate-run3-final.txt` 的头里"而 \`ledger-check\` 会解析验收文档"那一格被吞成空，
`gate-post-commit.txt` 的尾注里"最后的 \`gate: PASSED\` 段"被吞掉、且 shell 还报了一次
`command not found: gate:`。两处已按原意补回（前者在文件里留了一句"这一行第一次生成时被吞过"
以免读者以为是伪造的头），教训是：**带反引号的 markdown 一律走 heredoc（`<<'EOF'`）或文件写入，
不要放进双引号的内联命令**。

**（四）把"第几遍"当索引用，结果三处计数都写错了。** 本轮最初只按"回填前/回填后/文案定稿/提交后"
四遍叙述，于是：①§3 标题写"`make gate` 第 1 成员抓到 mypy 缺陷"——`a2-check` 实际是第 **7** 个成员，
失败那遍日志里正好 7 段标记；②验收文档 v5.13 行的证据清单写"一遍失败原文 + 三遍成功原文 + 提交后一遍"
= 5 份，漏了 22:55 那份**被自己取代**的日志（它也是绿的，`GATE_EXIT=0`，但读作 `3052 passed / 88.28%`，
跑在 `_legs` 修复与后 8 个用例之前）；③§9(二) 先写"四份日志 16/16/16/7"、改后写"五份"，
而实测是七份。三处都在补第 5 遍时靠**逐份 grep 计数 + 逐份读采集时间戳**发现，而不是靠记忆。
教训仍是同一条：**遍次、份数、标记数这类要用来判别的量，只能由命令当场数出来**；
本轮因此把 §6 的索引从"第 N 遍"改成"运行前的采集时点"（22:55:35 / 23:25:57 / 23:30:35 / 23:44:28 /
23:48:58 / 23:55:40 / 00:11:07），这样"绿色对应哪一棵树"与"哪一遍在先"都不再依赖叙述。

**（五）`coverage-faces.txt` 的 C38b 基线块是一次截断粘贴。** 该节把 `C38b/coverage-faces-across-runs.txt`
尾部按行抄过来，抄到第三块时停在第 8 行——读者看到的就是同一组三面重复三遍且最后一遍缺行，
像是复制贴错。读数本身没有错（C38b 三遍的面两两 diff=0，都是 92.49 / 91.68 / 95.76），
但呈现方式让人无法判断它是一次测量还是三次。已改为「三遍 diff=0」的事实汇总一次并写明出处，
同文件里另附 run1 / run2 / run5 三份日志各自复算的完整读数。
