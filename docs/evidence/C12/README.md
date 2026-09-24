# C12 轮：交易日期望判据接线（A4.7，AC-13 / AC-18）

本轮做的是 C11 轮留下的那条「谓词仍待补」：跑批窗口和新鲜度 lag 都在问同一个问题——
**哪一天应该有数据？**——而两处此前都回答「今天」。

范围：**只读**。未向数仓写入任何一行，未建表，未执行 DDL。

## 1. 缺陷：`expected = today` 的两处代价

| 位置 | 改前 | 后果 |
| --- | --- | --- |
| `opendata/pipeline/jobs.py::run_incremental_job` | `incremental_window(as_of or date.today())` | 周末/节假日触发的批次去拉一个**从没发生过的日子**，返回 0 行 ⇒ 在跑批记录里表现为「成功而零行」，与 C11a 修掉的静默空帧是同一类假象的上游版本 |
| `opendata/api/data_query.py::_freshness` | `expected = date.today()`（docstring 自陈「Until the trading calendar is populated (A4.7 wires it)」） | 每个周六固定 lag=1、周日固定 lag=2，节假日后第一天 lag=N ⇒ **缺数告警矩阵（AC-9）被日历噪声污染**，真实滞后被淹没 |

## 2. 设计：两层日历，并且每层都自报身份

新增 `opendata/pipeline/trading_calendar.py`（220 行）。两层判据不等价，所以
`TradingCalendar` 带 `tier` 与 `answered_from()`，报告里必须说清是哪一层开的口：

| 层 | 判据 | 能答 | 不能答 |
| --- | --- | --- | --- |
| `warehouse-calendar` | `dwd_trading_calendar` 中 `is_open = 1` 的行 | coverage 内的节假日（**absence 即 closed**，因为该表只存交易日） | coverage 之外的任何日子 |
| `weekday-rule` | 周一..周五 | 周末 | 节假日——它不知道自己不知道 |

四处刻意的口径决定：

1. **不外推未来**。C11 实测上游日历只给「滚动近一年且不含当日」、六个窗口参数全被忽略，
   因此 coverage 右端永远在今天之前；`is_trading_day()` 对 coverage 外的日子逐日回落到
   weekday 层，而不是整层降级——「今天」在两层里都问得出答案，但答案的**可信度不同**。
2. **找不到就不猜**。`expected_data_date()` 往回走 `MAX_LOOKBACK_DAYS = 30` 天仍无交易日 ⇒
   `raise ValueError("… looks broken …")`。静默退化成「拉一个月前的窗口」会把坏数据洗白。
3. **缺表是首跑常态，不是异常**。`warehouse_calendar()` 捕获宽类型异常并 `logger.warning`
   后退到 weekday 层，与它周围的 ods/dwd 读取器同一口径；层级写进报告，不假装读到了日历。
4. **非交易日不入库**这一事实反过来决定了判据形状：只存 `is_open=1` ⇒ 判 closed 只能靠缺席，
   所以「coverage 内 + 不在集合里」= 休市，这是设计 §4.1 字段集的直接推论，不是额外假设。

## 3. 接线点

- `jobs.run_incremental_job(..., calendar: TradingCalendar | None = None)`：窗口端点改由
  `expected_data_date(run_date)` 给出；`JobResult` 新增 **`expectation`** 四元组
  （`run_date` / `expected_data_date` / `calendar_tier` / `decided_by`）并进 `as_dict()`，
  使「窗口为什么选到这天」成为运行记录的一部分而不是事后推断。
- `data_query._expected_data_date(engine, on=…)`：**每请求解析一次**。`/catalog` 对每个已注册
  能力都要测新鲜度，逐表读日历会把一次请求放大成 N 次日历查询；`_freshness()` 的
  `expected` 改为必填注入参数（判据来自调用方=日历，不再来自墙上时钟）。
- `_execute_template` 里那条「A4.7 calibration item」注释按事实改写：cron 仍只管工作日触发，
  **窗口端点交给日历**，所以节假日触发时重新覆盖的是上一个应有数据的日子。

## 4. 真机度量（`calendar-check.txt`，脚本 `scripts/ops/calendar_expectation_check.py`）

判据用两个**互相独立**的事实来验：fuyao 日历（上游声明的开市日）与
`SELECT DISTINCT trade_date FROM dwd_stock_daily`（**有 K 线的日子市场必然开过市**）。

| 度量 | 值 | 读法 |
| --- | --- | --- |
| 上游日历 | **242** 个开市日，coverage **2025-09-25..2026-09-24** | 与 C11 实测语义一致（滚动一年、不含当日） |
| 数仓侧 | coverage 内 **132** 个 distinct `trade_date` | 本部署只回采到这个密度，不是日历问题 |
| **bars-on-closed-day** | **0** | 日历说休市的日子**没有一条**K 线 ⇒ 两个独立事实零冲突，这是本轮最硬的证据 |
| open-day-without-bars | 110 | 记为**回采缺口**（NOTE），不当作日历错误——判据只对自己能证的事负责 |
| weekday 节假日 | **19** 天 | 旧逻辑在这一年里会报 **19 次假 stale**（261 个工作日中的 7.3%），全部落在告警矩阵里 |

**一致性核对**：coverage 365 天里有 **261** 个工作日，而 **242 开市 + 19 休市 = 261** 逐日无遗漏
⇒ 上游日历在 coverage 内是**完备**的（既没漏开市日也没漏休市日），因此「absence 即 closed」这条
推断在本轮数据上是安全的，而不只是一个假设。

节假日拉回（同一个 `expected_data_date`，两层各答一次）：

| 参考日 | `warehouse-calendar` 层 | `weekday-rule` 层 | 判定 |
| --- | --- | --- | --- |
| 2025-10-01（国庆） | **2025-09-30** | 2025-10-01 | PASS |
| 2026-02-17（春节假期内） | **2026-02-13** | 2026-02-17 | PASS |
| 2026-09-24（coverage 内开市日） | 2026-09-24 | 2026-09-24 | PASS |
| 2026-09-25（当日，coverage 外） | 2026-09-25（由 weekday 层答） | 2026-09-25 | PASS + NOTE「unverifiable here」 |

**度量脚本自身的一处自我更正**（值得留档，因为它就是 A4.7 语义的回响）：初版硬判据写成
「`expected_data_date(today)` 必须是上游开市日」，脚本第一次真机跑就 **FAIL** 了。
复查后确认是**判据错而不是代码错**——上游 coverage 天然不含当日，当日答案按契约只能出自
weekday 层，它**不可被上游验证**。改为：只在 coverage 内做硬断言，coverage 外打 NOTE。
这条 NOTE 的作用是把「无法验证」和「验证通过」在输出里分开，避免脚本自己成为假证据源。

## 5. 测试与门禁

- 新增 `tests/test_trading_calendar.py` **12 例**（`TestWeekdayTier` / `TestWarehouseTier` /
  `TestWarehouseRead` / `TestQueryExpectation`）+ `tests/test_pipeline_jobs.py` **+2 例**
  （周六跑批窗口 = 2026-09-25 且 `expectation` 四元组逐项断言；节假日日历把窗口拉回 2025-09-30）。
- 测试日期全部**钉死**（周五 2026-09-25 / 国庆 2025-10-01..10-08），不用 `date.today()`——
  否则同一条断言在周末跑与在工作日跑含义不同。
- 数仓日历夹具用 SQLAlchemy Core `Table` + `insert`，不用 `text()`：sqlite 经 `text()` 读 DATE 列
  回的是 ISO **字符串**、MySQL 回 `date`，且 `text()` 绑 `date` 会撞 S608。生产侧 `_as_date()`
  因此同时接受三种形态（`datetime` / `date` / ISO 串）。
- 定向复算：calendar + jobs + data_query + freshness **78 passed**。
- `make gate` 全绿（`EXIT=0`，`gate.txt` **5,581 行完整未裁剪**）：
  **2,467 passed / 5 skipped**（2,453 → 2,467，+14 与新增用例数完全对上）、
  覆盖率 **85.77%**（阈值 84% 未动；较上轮 85.75% 的上浮是 `-n 8` 并行下一条分支归属的抖动，
  非本轮代码抬高——TOTAL 语句数 **10,218 未变**，missing 1,283 → 1,282）、
  `opendata/pipeline/trading_calendar.py` 自身 **88.66%**、
  a2-check **234 文件**（+3：新模块、新测试、新度量脚本）四项全 ok、public API **417/417 双 100%**（+8）、
  棘轮 `ruff_selfdev: 277 (snapshot 277)` 持平、`ruff_ported: 2144`（-1，改善，未冻结）。
- 本轮自我修正两处：①`warehouse_calendar()` 的降级最初只按 SQLAlchemy 异常设防，被 `object()`
  引擎夹具打出未捕获的 `AttributeError` ⇒ 按 §2.3 的口径改为宽捕获（**是设计口径写漏，不是打补丁**：
  缺列、驱动怪癖与不可达引擎都要落到同一个「这里没有日历」的分支）；
  ②`_as_date(row)` 传的是整行 Row 而非 `row[0]`，导致数仓层静默退化为 weekday 层——
  若不写 `test_landed_open_days_raise_the_tier` 这条断言层级的用例，该缺陷不会被发现。

## 6. 遗留（明确不做 / 做不了，含原因）

- **`dwd_trading_calendar` 没有生产者**：实测三源注册表（ths 7 腿 + akshare 10 + fred 3）里
  `trading_calendar` 域 **0 能力**，域清单里它存在（`domains.yaml:85`，契约 `TradingCalendar`）、
  上游端点也在（`endpoint_map.yaml` 的 `calendar` 段 `status: implemented`），但没有人取数落库。
  因此**生产运行今天仍走 weekday 层**，warehouse 层只有在日历入库后才生效。补这一腿需要
  ①注册 fetcher（可做，见 C13）与 ②写数仓（**属生产写入 + 可能建表，需明确确认后才做**）。
- **`next_trade_date` 算不出来**：上游只回滚动一年且不含当日，未来的下一交易日没有事实来源；
  契约字段保留 `None`，不做外推。
- **AC-13「调度时间业务校准」那条 box 不勾**：本轮消除了「节假日/周末的假 stale」与
  「拉一个没发生的日子」，但**当日收盘时点是否真能拿到当日 K**仍需在真实交易日的 17:30
  触发后多点续测（C11 已证 15:00 时点当日 K 未返回），一个观测点不足以勾 box。
