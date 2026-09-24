# C13 轮：ths 交易日历腿接入（A4.7 producer 侧，AC-10 / AC-13）

> 日期：2026-09-25 ｜ 分支：`dev` ｜ 真机：fuyao 日历腿 + 数仓只读对照
> 关联：`docs/evidence/C12/`（消费侧判据）——本轮补的是它 §6 登记的第一条遗留「`dwd_trading_calendar` 没有生产者」

## 1. 本轮补的是哪一块

C12 交付的是**期望判据**（`opendata/pipeline/trading_calendar.py`：跑批窗口端点与新鲜度 lag 都问它「这天该不该有数据」），
但它当时实测到一个不能忽视的事实：注册表里 `trading_calendar` 域 **0 条能力**（ths 7 腿 + akshare 10 腿 + fred 3 腿全查过），
且数仓里**根本没有 `dwd_trading_calendar` 表** —— 判据的上游是空的，生产运行只能一直落在 `weekday-rule` 层。
域清单（`domains.yaml:85`，契约 `TradingCalendar`）与端点（`endpoint_map.yaml` 的 `calendar` 段 `status: implemented`、且早已登记消费场景）都在，
缺的只是把端点接成 fetcher。本轮补这一段。

## 2. 上游语义（实测，非文档转述）

`/api/a-share/calendar/trading-days` 的行为在 C11 §6 与 C12 `calendar-check.txt` 两次独立观测中一致，本轮据此实现：

* 返回**滚动近一年**、且**不含当日**：本轮实测 coverage `2025-09-25..2026-09-24`、**242** 个开市日；
* **六个候选窗口参数全部被忽略**（`start_date` / `begin_date` / `start` / `year` / `days` / `limit`）⇒ 适配层**一个都不发**
  （`test_no_window_parameter_is_sent` 断言请求 URL 里没有 `?`）——发一个上去就是假装能定窗；
* 调用方给的窗口用**切片**回答，绝不外推：先按相邻行推 `prev/next`、再切；
* 窗口与 coverage **完全不相交即抛错**（`THS_CALENDAR_OUT_OF_COVERAGE`）而不是返回空元组——
  空元组会被下游读成「这段时间市场关门」，而真实原因是「问了上游没发布的东西」（C11a 的教训）；
* `prev_trade_date` 可由相邻行推出；**最后一行的 `next_trade_date` 留 `None`**，因为它的后继落在上游不发布的区间里，
  按工作日猜一个正好是 C12 判据拒绝做的外推。

## 3. 两个同名类不是一回事

`opendata.data.models.TradingCalendar` 是**契约行**（一天一行，落库形态）；
`opendata.pipeline.trading_calendar.TradingCalendar` 是**期望判据**（一个两层谓词对象）。
本轮新增的是前者的生产者，后者是它的消费者：`warehouse_calendar()` 读的正是这些行落成的表。
适配器 docstring 把这句话写死，避免后续有人把两者当一个东西改。

## 4. `verified=true` 的判据：跨 vendor **双向**一致（`ths-trading-calendar-cross-check.txt`）

日历说自己对没有用，得拿一个**不是日历**的事实来校。用的事实是行情：**有 K 线的日子市场必然开过市**。
一侧经 `get_registry().resolve_domain('trading_calendar', source='ths').fetch()`（路由路径，不是裸 client）取日历，
另一侧从数仓**只读**取 bar 日期，四条腿：

| 参照数据集 | 全表日期数 | 窗口内 | 窗口外 | 休市日有bar | 开市日无bar(自身区段) | result |
|---|---|---|---|---|---|---|
| `ods_stock_daily_akshare` | 131 | 131 | 0 | **0** | **0** | PASS |
| `dwd_stock_daily（source=akshare）` | 110 | 110 | 0 | **0** | 9 | PASS |
| `dwd_stock_daily（source=ths）` | 132 | 132 | 0 | 0 | 46 | OBSERVE |
| `ods_stock_daily_ths` | 2,430 | 242 | 2,188 | 0 | **0** | OBSERVE |

三条硬判据：

1. **独立 vendor 的 bar 日期落在日历休市日的数量 == 0**。akshare 链 131 个日期（`dwd` 的 akshare 切片 110 个）无一落在休市日 ⇒ 日历没有「把开市日说成休市」；
2. **每条判定腿在 coverage 内至少 20 个日期**，否则记 **FAIL（判不了）** 而不是 PASS ——
   两侧没有交集时「0 冲突」是空集撑起来的假证据，这条是本轮把「跑不通」与「数据不对」分开的写法（`MIN_JUDGABLE_DAYS`）；
3. **契约行自证**：严格递增、无重复日期、全部 `is_open`、`exchange` 单一标签，且每行 `prev/next` 必须等于相邻行 ——
   这两个字段是本层推导的、上游不给，所以必须单独判（`check_shape()`，任一行不符即失败并**报出条数**，不只给第一条）。

**双向才是证据**：只判「休市日有没有 bar」只能证明日历**不多关**一天；反方向（日历说开市而该区段一根 bar 都没有）才证明它**不多开**。
akshare 链在自身区段 `2026-01-05..2026-07-21` 内是 **131 个日期 ↔ 131 个开市日、两个方向都为 0**，
即与日历**集合相等**——两家不同 vendor 在同一区段上逐日对齐，这是本轮转正的依据。

反方向**只观测、不计入 result**，原因如实写进脚本输出：某开市日没有任何 bar，既可能是日历多报了一个开市日，也可能只是搬运缺日
（`dwd` 的 akshare 切片 9 个、ths 切片 46 个就是后者——同一 vendor 的 dwd 比 ods 少，属合并侧缺口而非日历侧），单凭 bar 分不开这两种解释。

`ods_stock_daily_ths` 与 `dwd（source=ths）` 两条腿是**同 vendor**（日历也来自 fuyao）：它们的一致属自我印证，
因此列成 OBSERVE、不参与判定（沿用 C9 里「同花顺自定板块没有独立发布方，只观测」的处置）。
其中 `ods_stock_daily_ths` 在 coverage 内恰好 242 个日期且两个方向都为 0 —— 记为「日历与搬运链自洽」，不记为证据。

## 5. 测试与门禁

**离线新增 9 例**（`TestTradingCalendarAdapter`，全部走 `httpx.MockTransport` 合成的上游帧）：邻接交易日由相邻行推出（含「中间那个空档不解释」）、
**一个窗口参数都不发**（断言 URL 里没有 `?`）、默认交易所标签、别的标签 fail-closed、双端窗口切片、单端窗口从两侧各切一次、
窗口整体在 coverage 之外（只给 `start` 与只给 `end` 两个方向各一条）fail-closed、空日历 fail-closed。
一致性套件 `tests/test_provider_conformance.py` 按注册表 fetcher 参数化 ⇒ 新腿**零新增用例自动纳管**（+4）。
**真机 3 例**（`-m e2e`，只读取数，`live-fuyao-runs.txt`）：日线、公司行为、以及本轮新加的日历腿
（断言 242 行以上、全部 `is_open`、最左行 `prev_trade_date is None`、最右行 `next_trade_date is None`）。

`make gate` **全绿**（`EXIT=0`，`gate.txt` **5,609 行完整未裁剪**）：**2,480 passed / 5 skipped**（C12 为 2,467，**+13 与 9 新用例 + 4 条纳管完全对上**）、
覆盖率 **85.79%**（阈值 84% 与 `precision = 2` 未动；TOTAL 语句 10,218 → **10,258**（+40 为 new 模块），missing 1,282 → 1,284 的 **+2 全部是 `if TYPE_CHECKING` 导入行**，
即运行时路径没有新增缺口；同轮加 2 例前后两次测得 85.81% / 85.79%，差值为 `-n 8` 并行下的分支归属抖动）；
新适配层 `providers/ths/models/trading_calendar.py` **95.92%**（39 语句 / 10 分支，唯一未覆行 39 与唯一 partial 都落在 `if TYPE_CHECKING` 守卫上 ⇒ 运行时全覆盖，与全库同口径）、
`registration.py` 89.19%（33-35 同为一类）、`pipeline/trading_calendar.py` 88.66%（C12 原值未动）；
a2-check **236 文件**（+2：新适配层 + 新对照脚本）ruff/format/mypy/bandit 四项全 ok；public API **420/420 双 100%**（C12 为 417，+3）；
棘轮 `ruff_selfdev 277 (snapshot 277)` 持平、`ruff_ported 2144`、`direct_http_ported 1044` 各较快照再降 1（改善，未 `--update` 冻结）；前端 8 files / 79 tests。

**本轮三处自我更正（都被写进代码或输出，不是事后合理化）**：

1. **真机腿此前一直静默 skip**：`TestAgainstTheLiveFuyaoApi` 的夹具只看进程环境变量
   （`FuyaoCredentials.from_environment()`），而适配层实际经 `settings`/`.env` 解析凭证 ⇒ 本机上前者返回 `None`、后者解析成功，
   于是这条「真机」类从未执行过（`live-fuyao-runs.txt` 末两行是同一台机器上两种解析的实测对比）。
   夹具改为与适配层同一入口 `credentials()`，抛错才 skip。**口径澄清**：C5/C6/C9/C10 的转正依据是各轮 `scripts/ops/*_cross_check.py`
   的**真机对照输出**（那些脚本每次都真跑），不受此影响；受影响的是「pytest 真机套件曾经绿过」这一印象。
2. **对照脚本初版只判单向**：仅统计「休市日有没有 bar」会放过一类缺陷——日历**多报**开市日（把关门那天说成开门）在该方向上完全隐形。
   加了反方向（自身区段内「日历说开市而无任何 bar」）并明确**只观测不判定**（搬运缺日与日历多报在 bar 侧不可区分，见 §4）；
   同时加 `MIN_JUDGABLE_DAYS = 20`：判定腿在 coverage 内日期不足即记 **FAIL（判不了）**，防止空交集把「0 冲突」伪装成通过。
3. **`check_shape()` 初版 `break`**：第一条 prev/next 不符就退出、只报一条 ⇒ 若成片错会**欠报规模**。改为统计全部不符行数再报（示例给第一行）。
   另修草稿期一处口径不一致：非 `CN-SSE` 标签最初抛裸 `ValueError`，而 docstring 承诺 fail-closed 码 ⇒ 统一为 `THS_CALENDAR_EXCHANGE_UNSUPPORTED`。

## 6. 遗留（明确不做 / 做不了，含原因）

* **`dwd_trading_calendar` 仍然没有行**：本轮交付的是生产者（fetcher 已注册、可路由、已真机自证），
  **把行写进数仓属生产写入且需要建表**（该表当前不存在），按既定纪律**须明确确认后执行** ⇒
  生产环境的期望判据今天仍落在 `weekday-rule` 层；落库后 `warehouse-calendar` 层才会开始答题（`answered_from()` 会自报层级，可核）。
* `next_trade_date` 在最右端仍为 `None`（上游语义所限，见 §2），不是缺陷也不是可补的字段。
* 期货交易日端点 `/api/futures/calendar/trading-schedule` 仍 `status: available`、未接：
  它属于期货域调度，需要另一套对照面（交易所休市与 A 股不同），本轮不顺手接。
* 未接 akshare 侧日历（`tool_trade_date_hist_sina` 一类）：`opendata_http` 的 52 函数移植面里没有它，
  为一条腿单独扩移植范围不在本轮口径内；本轮的独立性由**已在库的 akshare 行情日期**提供。
