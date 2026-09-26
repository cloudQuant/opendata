# C42 · 两件独立的事：两个模块的单元面补过 90% 线，元数据骨架落地的「同键后写覆盖」改成显式拒绝

日期：2026-09-27（跨了本地零点，两批量测分别是 00:35 / 01:00）
任务：#53　AC：AC-2|02（加固）、AC-17|06（同一张 face 上的两个模块）
门禁：`docs/evidence/C42/gate-run1.txt`（回填前一遍，`GATE_EXIT=0`，16 个 `===== gate:` 段）

---

## 0. 结论（先说要紧的）

| 项 | 前 | 后 | 判定 |
|----|----|----|------|
| `opendata/pipeline/jobs.py` 单元面 | 86.87% | **97.30%** | 过 90% 线 |
| `opendata/pipeline/trading_calendar.py` 单元面 | 87.93% | **97.41%** | 过 90% 线 |
| `opendata/pipeline/` 三条 face 之一 | 91.89% | **93.08%** | 判据 ≥90%，维持 |
| 全局 `test-cov` | 88.35% | 88.71% | 阈值 84 未动 |
| 同腿重复 dwd key 的行为 | 后写覆盖，计数报两个 | **整组拒绝 + 报告点名** | 反证见 §3 |

- 本轮**没有新增 §2 勾选**，且这不是假设而是检查结果（§6）。
- 本轮**没有**改 `[tool.coverage] exclude_lines`、没有改 `--cov-fail-under=84`、没有加空断言用例；
  两模块剩下的 miss 全部是 `if TYPE_CHECKING:` 里的导入语句，运行时永不执行（§4、§7）。
- ④ 不是「修数据丢失」：九页目录 65,895 行实测 **0 个 symbol 相撞**（§1）。
  本轮把一条**从未被任何代码执行过**的不变量补上守卫，并把「没有受影响行」这个事实同时留档。

---

## 1. ④ 的前置量测：目录的 9 个资产类页到底发不发同一个 symbol

`scripts/ops/metadata_backbone_landing.py` 落地的 instrument 腿把 9 个资产类页的返回拼成一条腿，
dwd 键是 `(symbol,)`。如果两页都发 `000001`（一个 a 股、一个指数），upsert 会让后一行在前一行之上改写，
而 `DwdWriter.write` 返回的计数是 `len(frame)` —— 报告里两个都算数，库里只剩一个。这件事此前无判据。

量测脚本 `docs/evidence/C42/duplicate-symbol-scan.py`（只读：不带 engine、不连主库、无 DDL / INSERT）：

```
pages asked        = 9
accepted rows      = 65895
distinct symbols   = 65895
collided symbols   = 0 (the last row of the leg would win)
rows an upsert swallows = 0
collided and disagreeing = 0
rows the contract refused = 0
```

逐页行数（三批读数逐字一致）：a-share 5,578 / a-share-index 1,431 / fund-etf 1,696 / fund-lof 404 /
fund-otc 30,701 / fund-reits 89 / futures 1,142 / options 24,470 / forex 384 = **65,895**。

档案：
- `duplicate-symbol-scan-run1.txt`（脚本第一版，`EXIT=0`）
- `duplicate-symbol-scan-run2-window.txt`（补窗口打印后重跑，`EXIT=0`）
- `duplicate-symbol-scan-run3-final.txt`（**当前脚本**，`EXIT=0`，逐页行数与前两批逐字一致）

**三批量测里有两批是给量测工具自己收口的**。run1 之后发现档案没法回答一个自然会冒出来的问题——
C41 的 dry-run 档案里日历腿是 28 行，C42 run1 是 27 行，差在哪。脚本量的是「腿是否回答 + 计数」，
没打印它问的是哪个窗口。于是给 `_scan_calendar()` 补两行打印后重跑（量测面不变），
run2 自己就把答案给出了：

```
asked window       = 2026-08-19 .. 2026-09-27
returned span      = 2026-08-19 .. 2026-09-24
accepted rows      = 27
```

窗口端点是 `date.today()`，三批都跑在本地 09-27（00:35 / 01:00 / 01:09），C41 那一批跑在 09-26 23:21
⇒ 窗口整体前滚一天，2026-08-18 掉出窗口、09-25 之后尚未发布；`08-18..09-24` 是 28 个交易日、
`08-19..09-24` 是 27 个。**不是上游改数，也不是本轮引入的行为差**。

### 1.1 顺手把「没测到」和「测到没有」分开（run2→run3 的差别）

写 run2 的时候看清了另一件事：`_scan_catalog()` 对失败的页只打印 `page <type>: LEG_FAILED ...`
然后 `continue`，汇总里 `collided symbols   = 0` 照打，`main()` 无条件 `return 0`。
也就是说**一页都不回答的跑法（无凭证 / 网络故障）会打出一份和真相逐字同形的读数**，
还以 0 退出。这正是本轮 ④ 在生产的落地上要修的那类东西，量测工具自己也有一份。

改法：`CatalogReading` 带上 `asked`/`answered`/`failed`，`main()` 在任一页没回答时打印
`SCAN INCOMPLETE: n page(s) did not answer (...)` 并 `return 1`；日历腿回答 0 行同样 `return 1`
（它没回答时的 `collided keys = 0` 是同一个洞）。

`docs/evidence/C42/scan-failure-face.py` → `scan-failure-face.txt` 把这件事证出来：三次读数驱动
同一个 `main()`，只替换它的两个取数辅助函数（**不发任何网络请求、不读任何凭证**）：

| 读数 | 页 | 日历腿 | scan exit |
|------|----|--------|-----------|
| A 每页都不回答（无凭证会看到的样子） | `LEG_FAILED ×9`，`pages answered = 0` | 健康 | 期望 1 ⇒ **1 ok** |
| B 页回答、日历腿 0 行 | 9/9 回答 | `accepted rows = 0` | 期望 1 ⇒ **1 ok** |
| C 对照：两条腿都回答 | 9/9 回答 | 27/27 | 期望 0 ⇒ **0 ok** |

A 与 C 打印的 `collided symbols   = 0` 一字不差 —— 把两者分开的只有退出码，
而退出码在 run2 之前不存在。脚本自身对三次读数都 assert，任何一次不符就 `return 1`。

结论：④ 的缺陷面在当前数据上是 **0 行**，所以本轮的措辞（脚本 docstring、守卫 docstring、用例 docstring
三处一致）是「给一条没有守卫的不变量补守卫」，不是「找回丢掉的行」。


---

## 2. ④ 的改动：拒绝什么、报告什么

`opendata/pipeline/templates.py` 新增 `_refuse_colliding_keys()` / `_key_signature()`，
接在 `refresh_metadata_backbone` 的每条腿过完契约之后、交给写手之前：

- 一条腿里凡是 dwd 键出现了 ≥2 次的，**这一组一行都不落**（不是留最后一行，也不是留第一行）；
- 每条碰撞进 `BackboneReport.rejected[domain]` 一条消息，格式：
  `key symbol=000001.SZ: instrument published it on 2 rows and disagrees on name, delist_date; none landed`；
  全等时是 `with identical values`；
- `landed[domain]` 因此只数真落下的行，计数不再包含被覆盖的那一行。

不打印行号是刻意的：碰撞组的下标是「已过契约的行」里的位置，契约拒绝消息的下标是「原始返回」里的位置，
两套编号印在同一个数组里会误导读者。改成点名键值 + 携带行数 + 哪些字段不一致。

**为什么「拒绝」是更安全的方向**（不是顺手选的保守策略）：被拒的 symbol 对 `jobs.drop_inactive_symbols`
是「目录没有回答」，该函数对未知代码的处理是**留在宇宙里**（`opendata/pipeline/jobs.py:288-290`，
`rows is None → kept`）；而后写覆盖给出的是「目录回答过一次，用的是错的那一页的字段」——
`name`/`board`/`status`/`delist_date` 会被另一资产类的同名行改写，增量窗口就会按错的上市区间裁标的。

落地脚本 `scripts/ops/metadata_backbone_landing.py` **一行都没改**就能把新面打出来：
它读的是 `report.rejected[domain]`，逐条打印（前 20 条 + `... N more refusal(s) not shown`），
并把总数汇进 `METADATA_BACKBONE ... refused=` 那行。

---

## 3. ④ 的反证：把守卫摘掉，同一条链路给出什么

`docs/evidence/C42/guard-removal-reading.py` 在同一次进程里跑两遍 `refresh_metadata_backbone`
（`engine=None` + 注入 `land`，不连任何库），第二遍把守卫换成透传函数并在 `finally` 里换回原函数。
三条 instrument 行：两条 `000001.SZ`（`name`/`delist_date` 不同）、一条 `600519.SH`。

```
-- guard SHIPPED (C42 behaviour)
   symbols = ['600519.SH']
   frame_rows = 1 / distinct_keys = 1 / landed = 1 / count_lies = False
   refused = ('key symbol=000001.SZ: instrument published it on 2 rows and disagrees on name, delist_date; none landed',)
-- guard REMOVED (= behaviour before C42)
   symbols = ['000001.SZ', '000001.SZ', '600519.SH']
   frame_rows = 3 / distinct_keys = 2 / landed = 3 / count_lies = True
   refused = ()
```

`count_lies` 的定义是 `landed != distinct_keys`：摘掉守卫后报告说落了 3 行，写手拿到的帧里只有 2 个键，
第三条（`000001.SZ` 的另一行）在同一个 `INSERT ... ON DUPLICATE KEY UPDATE` 里被自己盖掉。
脚本对两次读数各assert四条性质，任一不再成立就 `return 1` —— 档案红了自己会喊。
归档：`guard-removal-reading.txt`（`EXIT=0`）。

补一句可执行性事实（免得后来人以为可以拿 SQLite 复现这段）：`build_upsert_sql`
（`opendata/pipeline/ods_writer.py:142`）发的是 MySQL `INSERT ... AS new ON DUPLICATE KEY UPDATE`，
SQLite 上根本跑不动，所以「静默后写」只能靠**移除守卫**来证明，不能靠一次仓库写入证明。

用例侧（真正的牙，进 `test-cov`）：
- `tests/test_pipeline_templates.py::TestMetadataBackbone::test_two_rows_on_one_key_are_refused_instead_of_the_last_one_winning`
- `tests/test_pipeline_templates.py::TestMetadataBackbone::test_a_calendar_leg_that_publishes_one_day_twice_is_refused_too`

两条都在注入的 `land` 里断言 `len(frame) == frame[list(key)].drop_duplicates().shape[0]`
（写手拿到的帧自身必须无重复键），第二条专门测日历腿的复合键 `(exchange, date)`，
并断言 `trading_calendar` 压根没进写手。

---

## 4. ③ 的入口读数：两个来源，都 <90%

任务描述里写的是 `jobs.py 86.10%` / `trading_calendar.py 82.76%`。那是一遍 **partial run**
（只跑三个测试文件）的读数；门禁档案里同一棵树的读数是 **86.87% / 87.93%**
（`docs/evidence/C41/gate-post-commit.txt`，与 run1/run2/run3/run5 逐字一致）。

差异归因：partial run 排除了其余测试文件，那些文件覆盖到的语句照旧记 miss；
门禁是 `pytest -n 8 -m "not e2e"` 全量。两个读数都不是造假（同一棵树、同一份 coverage 配置），
差别只在分母包含多少用例。本轮把 before 档取在**门禁档案**（86.87 / 87.93），并在
`coverage-faces.txt` 里把两个都留档 —— 判定用哪个都不改变结论：两个模块此前都在 90% 线以下。

顺带把 C41 之前那一段也标出来：同两个模块在 162/73 stmts 的旧形状上是 83.33% / 88.66%
（C30…C40 二十来份档案逐字相同），C41 给它们加了 53/15 条语句（契约接入 + 窗口裁剪），
新增的那部分当时就是没测的面。

复算：`coverage-faces.txt`（脚本 `docs/evidence/C38b/coverage-face-recompute.py`，只读档案不重跑）。

---

## 5. ③ 补的 16 例，逐条说它凭什么能让门禁变红

`tests/test_pipeline_jobs.py` +9 例、`tests/test_trading_calendar.py` +5 例、
`tests/test_pipeline_templates.py` +2 例（④ 的那两条）。③ 的 14 例：

| 用例 | 补的弧 | 能被什么改动弄红 |
|------|--------|------------------|
| `TestResolveFetcher` 3 例 | `jobs.resolve_fetcher` 的注册表查不到时的两条 `LookupError` 分支 | 把 LookupError 换成 return None／换消息文案（`match=` 锁了文案子串） |
| `TestWindowRowCount` 3 例 | `jobs._window_row_count` 的 in-window／wide-window／表不存在三条路 | 把 `WHERE` 端点从闭区间改成开区间（窗口边界那 2 行就不对了）；把「表不存在」改成抛异常 |
| `TestFreshnessReadings::test_the_reports_are_flattened_to_iso_dates` | `jobs._freshness` 的 `None` 与有值两条出口 | 把 iso 格式化改成 `str()` 之外的形态、把 `None` 折成空串 |
| `..._an_unmapped_source_is_reported_instead_of_raising` | `jobs._freshness` 捕获 `LookupError` 的那条 except | 去掉 try ⇒ 一个未映射源把整个巡检读数打崩 |
| `TestInstrumentCatalog::test_derive_universe_says_which_codes_it_refused_to_judge` | `derive_universe` 的 `dropped 为空` + `ambiguous=1` 两条件合取（`jobs.py:350→355` 与 356） | 让 ambiguous 计数不进日志、或把「不判断」的码也 drop |
| `TestDriverDateShapes` 5 例 | `trading_calendar._as_date` 的 datetime 归一／date 透传／ISO 文本两种／显式坏格式／整行不是日期 | 把 `datetime` 分支删掉（MySQL 驱动给的正是 datetime）、把 `str` 分支放宽成 `date.fromisoformat` 不捕获 ValueError |

`_as_date` 那 5 例是本轮**途中量到的**才补的：`SELECT MAX(trade_date)` 在 MySQL 下回 `date`、
在 SQLite 下回 ISO **文本**，而 `opendata/pipeline/freshness.py:480 _as_date` 只认
`datetime`/`date` ⇒ 一个填了数据的 SQLite 表会被报成 `missing`。

**这条不是生产缺陷**：`settings.data_database_url` 是 MySQL，`grep` 全 `opendata/` 没有任何 sqlite URL；
它是本轮**测试夹具侧**的方言不对称。第一版用例假设「往 SQLite 里落几行就能读出 ISO 日期」，
实测给出 `{'ods:ths': None, ...}`，于是拆成两条：一条 patch 读取器、注入真的 `FreshnessReport` 对象
（这样读到的 `None`／有值都是代码自己的判断，不是夹具的形状），一条不 patch、用无表的 SQLite 引擎
专门走 `LookupError` 那条 except。方言不对称写在类 docstring 里，没有拿去断言一个会误导人的读数。

---

## 6. 判定面：本轮一条 §2 勾都不新增（这是查过的）

- **AC-2 条目级 5/5**（`AC-2|02` 由 C41 翻 proven）。④ 改的是 AC-2|02 落地面的一条行为，
  不新增可勾条目：AC-2 剩下 0 条 gap，本轮不动台账状态。
- **AC-17 条目级 5/10**：proven 是 |01|02|04|06|09，未评审是 **|03（A1 存量棘轮+触碰即达标）／
  |05（搬运代码仅 E/F + pre-commit exclude）／|07（新增公开 callable docstring+注解 100%）／
  |08（反空壳抽审）／|10（里程碑证据可追溯）**。本轮逐条读了判据原文（`验收文档.md:259-268`）：
  ③ 是 |06 已 proven 的那张 face 内部把两个模块补过线（face 从 91.89→93.08，本来就过线），
  ④ 是落地面行为，不落在 |03/|05/|07/|08/|10 任何一条的文本上 ⇒ **不翻任何一条**。
  想翻 |03/|07/|08/|10 得各自造判定面（C40 给 AC-1/AC-2 做的那种 measure/judge/repair/breaks 四件套），
  那是另一轮的工作量，已登记在 §8。**§8 第 4 条同时更正一处我本轮查早了的说法**：
  |07 并不是"没有面"，`public-api-quality` 一直在量它（run2：596/596 与 596/596 都是 100%），
  缺的是"新增 ⊂ A2 集"的包含关系论证与那个面自身的反证。
- 台账 `docs/quality/acceptance-item-ledger.json` 本轮**只加不改状态**：`AC-2|02|0526af4d` 的
  `round` 记为 C42、`command` 换成 C42 树上的重算命令、`evidence` 追加 12 条 C42 档案
  （含 `tests/test_pipeline_templates.py`、`tests/test_trading_calendar.py`），
  `note` 末尾追加一段把 C41 那句"缺口登记为后续项"读掉；**状态仍是 proven**，
  重算读数见 `probe-recheck.txt`（条目级与 `--all` census 两次都 proven，import 侧计数 = 2）。
  证据只引用 run1 及更早的档案：本轮自己的最终门禁日志按定义晚于台账写入，不能被它引用。

---

## 7. 门禁

- `gate-run1.txt`：回填前的完整未裁剪一遍，16 个 `===== gate:` 段（15 成员 + PASSED），
  `GATE_EXIT=0`，全局 `TOTAL 11097 1090 2738 286 88.71%`。
- run1 之后又动了 `docs/evidence/C42/` 里的量测脚本两次（§1.1：窗口打印、`SCAN INCOMPLETE` 判定面），
  并新增 `scan-failure-face.py`。`a2-check`（第 7 个成员）会把 `docs/evidence/**/*.py` 扫进来，
  所以 run1 对**最终树**不再是充分证据；回填后再跑一遍并单独归档，头部写明与上一遍的差。
  run1 已经证明的部分不作废：run1 的 `test-cov` 段读的是 `opendata/` + `tests/` 的最终形状，
  那之后这两个目录一行都没改。
- 中途一次 `ruff`/`mypy` 清理（未计入门禁遍次，都是 `make gate` 之前的自查）：
  `B905`（zip 缺 `strict=`）、`PERF203`+`C416`（脚本里的 try-in-loop 与集合推导）、
  `D415`（中文句号结尾的首行 docstring）；mypy 两处 `Argument 1 ... incompatible type "object"`
  与 `Type argument "object" of "Fetcher" must be a subtype of "QueryParams"`。
  全部按「改代码」处理，**没有加 `# type: ignore`**，**没有豁免规则**。
- `gate-run2-final.txt`：**回填后最终树**那一遍，`GATE_EXIT=0`（读自文件内部，不是后台包装器的状态）、
  16 个 `===== gate:` 段、7,018 行完整未裁剪，头部逐条写明与 run1 的五处差别。
  覆盖率行与 run1 **逐字相同**：`TOTAL 11097 1090 2738 286 88.71%`——这就是"代码面没再动"的证据，
  不必靠叙述。`ledger-check` 段读到 `items=130 proven=18 gap=7 unreviewed=105 ticked=18`，
  即 §10 的两行扩写与台账 `AC-2|02` 的 C42 追加都在同一遍里被核对过。
- 还有一处顺序要说明白：`README.md` 本节与 `probe-recheck.txt` 的补写发生在 run2 **之后**，
  都是 `.md`/`.txt`，不被任何成员的判定面读取（`a2-check` 只扫 `**/*.py`），
  但 `brand-check` 是全树 `rglob`，所以补写后单跑了 `make brand-check` 与 `make ledger-check`
  这两个成员，各自读到 `OK:`（前者 `no brand residue and no rename leftovers`，
  后者 `census: items=130 proven=18 gap=7 unreviewed=105 ticked=18`）。
  这一遍门禁对**代码树**是最终证据，对本 README 的最后几段只是"早于它"，如实标注在此。

---

## 8. 已知边界与遗留登记

1. **元数据骨架仍未落库**：④ 改的是落地路径的行为，`--write` 仍未执行（需要用户确认，任务 #52）。
   C42 的九页扫描顺手给出了落库规模：65,895 行 / 9 页 + 27 个交易日。
2. **`_as_date` 全库有四份副本**，支持的形状集合互不相同（`freshness.py:480` 只认 datetime/date、
   `trading_calendar.py` 认 ISO 文本、两个 ops 脚本各一份）。本轮只给 `trading_calendar` 那份补了面，
   **没有合并**——合并会改变 `freshness` 的行为，那是 AC-18 的新鲜度判据面，该单独一轮做。
3. **巡检 `BACKFILL_MODULES` 仍不含 `pipeline/trading_calendar.py` / `pipeline/freshness.py`**（C38b 登记）。
4. **`acceptance_item_probe.py` 只实现了 AC-1/AC-2 的条目级判定面**，AC-17 的 |03/|05/|07/|08/|10
   五条没有 per-item 探针——本轮读了判据原文确认挣不到，`--item 'AC-17|06'` 的
   `FAIL: no probe …` 就是这个缺口的读数（`probe-recheck.txt`）。
   **但这里有一处本轮自己纠正的认知，必须写下来**：`|07`（新增公开 callable 的 docstring 与参数注解
   覆盖率 100%）并非"没有判定面"，它早就有一个门禁成员在量——`public-api-quality`
   （`scripts/quality/public_api.py`），run2 读到 `A2 public callables : 596`、
   docstring `100.0% (596/596)`、annotation `100.0% (596/596)`。我为了 C43 先量时用裸 `ast`
   自己扫了一遍 A2 集（prod 547 个"公开 callable"里 22 个无 docstring），一度以为那是缺口；
   抽样归因后 22 个全是 `@abc.abstractmethod …: …` 桩体与工厂函数内的闭包，
   `ruff check --select D` 对这些文件 `All checks passed` —— **是我的口径比 pydocstyle 宽，不是树里有洞**。
   ⇒ 下一轮若要翻 |07，缺的不是量面而是三件事：①「新增 ⊂ A2 集」的包含关系论证，
   ②`public_api.py` 自身的反证（种一个无 docstring 的公开 def，它必须红；否则这条读数恒绿没牙），
   ③条目级探针。|08 后半（T1 模块 错误信封样例 ≥3 + 黄金向量 + normalize 真实报文 ≥3）
   才是真的还没量过的开发面。

5. **扫描脚本不发任何请求的读数不可信，而这条现在是它自己判的**：`duplicate-symbol-scan.py`
   依赖真机 key（`FUYAO_API_KEY` 由客户端自行读取，档案里不打印值／长度／前缀，只有
   「腿是否回答」+ 计数）。任何一页 `LEG_FAILED` ⇒ `SCAN INCOMPLETE` + `exit 1`；
   日历腿回答 0 行 ⇒ 同样 `exit 1`。§1.1 的 `scan-failure-face.txt` 是这个判据的正反两面，
   且它自己不碰网络、不碰凭证。
6. **`scan-failure-face.py` 用的是 `importlib` 动态加载 + `module.__dict__` 打补丁**
   （`docs/evidence/C42/` 的文件名带连字符，不能 `import`）。两个坑都在本轮踩过并留下：
   动态加载的模块不注册进 `sys.modules` 时，被加载方自己的 `@dataclass` 会在 exec 期抛
   `AttributeError: 'NoneType' object has no attribute '__dict__'`；`setattr(mod, "常量名", …)`
   在仓库的 ruff 规则集下是 `B010`，得写成字典赋值。

## 9. 归档清单

| 文件 | 内容 |
|------|------|
| `duplicate-symbol-scan.py` | ④ 前置量测脚本（当前版：带 `pages answered` 与 `SCAN INCOMPLETE` 判定面） |
| `duplicate-symbol-scan-run1.txt` | 第一版脚本的真机读数（`EXIT=0`，65,895 行 / 0 碰撞） |
| `duplicate-symbol-scan-run2-window.txt` | 补窗口打印后重跑（`EXIT=0`，同上 + `asked window`/`returned span`） |
| `duplicate-symbol-scan-run3-final.txt` | **最终脚本**的重跑（`EXIT=0`，逐页行数与前两批逐字一致） |
| `scan-failure-face.py` / `.txt` | ④ 量测工具自身的判定面：A/B→exit 1、C→exit 0，不碰网络不碰凭证 |
| `guard-removal-reading.py` / `.txt` | ④ 反证：守卫在／摘掉两次读数，`EXIT=0` |
| `coverage-faces.txt` | ③ 前后读数：两模块 + 三条 face + 全局 + 残差解释 + 两个入口读数来源 |
| `gate-run1.txt` | 回填前门禁（`GATE_EXIT=0`，16 段） |
| `gate-run2-final.txt` | 回填后最终树门禁（`GATE_EXIT=0`，16 段，7,018 行；头部逐条写明与 run1 的差） |
| `probe-recheck.txt` | 台账翻勾依据的条目级重算：`AC-2\|02` → `proven`（import 侧计数 = 2）、`--all` census `gap=6, proven=9`，以及 `AC-17\|06` 没有条目级探针这一事实的原文读数 |
| `README.md` | 本文件 |
