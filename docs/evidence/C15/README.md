# C15 轮：ths 基金分红（`fund_action` 域）腿接入 + 与新浪累计分红逐笔对照（AC-10 / AC-5 / AC-7 / AC-13）

> 本轮接的是 D10 欠了四轮的那块**换算键**：ETF 的前复权序列要换回不复权，需要「哪一天、每份派多少」。
> 这条腿发的是事件流，不是价格。全程数仓**只读**——不建表、不写入。

## 1. 本轮补的是哪一块

- C6 轮实测：ths 的 ETF 日线端点 `/api/fund/market/historical` **只发前复权序列**（`adjust` 连请求参数都不接受，
  信封里回的是回声值），而 D10 规定入库不得存复权价 ⇒ `fund_etf_daily` 从 C6 到本轮一直留在 akshare 侧、
  `verified=false`。
- 缺的那一块是**因子来源**而不是第二个价格源。本轮接 `/api/fund/corporate-actions/dividends`，新建域
  `fund_action`（`asset_class=fund`、契约 `CorporateAction`、`rest_path=fund/actions`）。
- 前提不是假设而是本轮实测坐实的恒等式（§5 判据 E）：`新浪不复权收盘 − ths 前复权收盘 = 该日之后分红之和`——
  510300 在 `2025-01-02` 那根上是 `3.909 − 3.698 = 0.211 = 0.088 + 0.123`。**换算键可用**由此得证；
  价格腿本身仍未接（§9），因为它要等的是本地换算这一步，不是这条腿。

## 2. 上游语义（2026-09-25 实测，非文档转述）

| 观测项 | 实测 | 落地 |
| --- | --- | --- |
| 入参 | 只有 `thscode`，且**必须带后缀**（裸码回 `code=1002`） | `build_fund_dividends_request` 无点即 `fund_symbol_qualified` 拒；裸码先经 `resolve_fund_code` 解析 |
| 裸码怎么解析 | ETF 目录（`asset_type=fund-etf`）一次 1,696 行、沪深裸码唯一 | **不走** `resolve_code`：它的后缀过滤是股票口径，而 6 位裸码跨资产类重复（`000001` 同时是股票 / 指数 / 场外基金），搜索命中会让股票答案冒充基金答案；场外基金码不在 ETF 宇宙里 ⇒ 必须合格码直传，否则 `THS_FUND_SYMBOL_UNRESOLVED` |
| 日期窗 | **无窗口参数**，一次给自成立至今的全部 | 窗口在 `transform_data` 里按除息日切片，不二次调用 |
| 无分红基金 | `dividend_count: 0` 且 `item` 里躺着**一行十字段全 `null`**（159915.SZ、512880.SH 实测），不是 `item: []` | `_is_placeholder_dividend` 丢占位行；空事件流是**事实**不是失败（与 A 股 `stock_action` 的空即抛刻意不同：那边「空」是取数失败信号，这边「从未分红」是常态） |
| `progress` | 实测恒为数字码 `"2"`，文档示例是汉字「实施」 | `IMPLEMENTED_PROGRESS = {"2"}`；未知码 `fund_dividend_progress` 拒（不猜别名） |
| 金额口径 | `per_ten_cash_before_tax` 是**每 10 份**，契约要**每份** | `/10.0`；`cash <= 0` 即 `fund_dividend_cash` 拒（全零行是脏行不是零分红） |
| 自带自校验 | 信封 `data` 里有 `dividend_count` 与 `dividend_total`（后者是**每份元**、不是每十份） | 归一化后逐笔对账：笔数不等 `fund_dividend_count_mismatch`、合计超 `1e-6` `fund_dividend_total_mismatch`（实测 `0.8800000000000001` vs `Σ 0.88` 是 float 表示位，不是数据分歧） |
| 契约放不下的字段 | `publish_date_ms` / `registration_date_ms` / `payment_date_ms` / `in_dividend_date_ms` / `profit_base_date_ms` / `reinvestment_date_ms` 六个日期 + `per_ten_cash_after_tax` | **不硬塞**（同 C14 对 `last_delivery_date` 的处置）；`reinvestment_date_ms` 是「红利再投」选项的日期而非份额，上游也没有份额分红列 ⇒ 契约 `stock_dividend` 恒 0 属**该端点无此字段**，不是漏映射 |

## 3. 税前 / 税后：口径必须显式写出来

- 上游一行为同一笔分红给**两列**金额（税前 / 税后），契约只有 `cash_dividend` 一个金额字段 ⇒ 落**税前**，
  与 A 股 `normalize_adjustment_factors` 同口径，并写进 `Capability.notes`（`"pre-tax"`）。
- 实测 4 只基金里可比的两只（510300 / 510050）共 32 笔**税前与税后逐笔相等**（§5 表 D 最后一列）。
  登记这个事实不等于把它当假设：两者不等时是**分红税**问题而非数据错误，下游若把契约值当税后值二次扣税就会重复减免，
  所以口径写在能力声明里而不是注释里。

## 4. 两处上游形状缺陷写成判据而不是注释

- **全零现金行必须挂**：`normalize_fund_dividends` 收到 `per_ten_cash_before_tax = 0` 的行即
  `fund_dividend_cash` 失败（单测 `test_fund_dividends_reject_unusable_rows` 与对照脚本 A 段的反例列同一口径）。
- **全 null 占位行必须落成空事件流**：`test_fund_dividends_skip_the_all_null_placeholder` +
  真机 `test_fund_without_any_dividend_comes_back_empty` + 对照脚本 D 段「占位」列三处同时坐实。
- 另有「上游自带汇总与逐笔不等即不发事件流」（`test_fund_dividends_reconcile_against_the_upstream_totals`，
  含 float 噪声容差边界）、「声明笔数非整数即拒」（`test_fund_dividend_declared_count_must_be_an_integer`，
  `bool` 也算非整数）、「未实施进度码不进事件流」（`test_fund_dividends_only_published_events_become_rows`）。

## 5. `verified=true` 的判据：A–E 五条，其中三条的参照**不是 fuyao**

`scripts/ops/ths_fund_action_cross_check.py`（`EXIT=0`，数仓只读；上游拒绝连发 ⇒ 12 秒节流 + 5×20 秒退避，
把「跑不通」与「数据不对」分开记），输出留档 `fund-action-cross-check.txt`。
样本 4 只：`510300.SH`（**裸码 `510300` 入参**，走解析路）、`510050.SH`、`159915.SZ`、`512880.SH`
（后两只是**无分红**样本，用来坐实「空是事实」而不是「空是没判」）。ths 侧一律经
`resolve_domain('fund_action', source='ths')` **路由**取数，并另走一条传输层通道与之对账。

| 判据 | 内容 | 参照方 | 实测 |
| --- | --- | --- | --- |
| A | 契约形状自证（除息日严格降序唯一、每份现金落在 0～1 元、非现金字段恒 0、搬运层与契约层一致）+ 两条**必须挂**的反例 | 自证 | 4 只全合格；反例按预期失败 |
| B | 除息日集合**双向**对照 | **新浪**累计分红阶跃日（`fund_etf_dividend_sina`） | 14/14、18/18、0/0、0/0，两个方向差集都是 0 |
| C | 逐笔每份现金对照 | **新浪** Δ累计分红 | 32 笔逐位相符（容差 1e-4 只给新浪三位小数留，实测差额 0） |
| D | 上游自带 `dividend_count` / `dividend_total` / `progress` 对账 + 税前=税后 | 自证（上游第二路证据） | 0.880 / 0.797 与 Σ 相符；无分红两只回的是 `dividend_count: 0` + 1 行占位 |
| E | **价格恒等式**：不复权 − 前复权 = 其后分红之和 | **新浪**不复权收盘（`fund_etf_hist_sina`）× ths 前复权（`/api/fund/market/historical`） | 510300 `2025-12-29`：`4.763 − 4.640 = 0.123` =其后分红；510050 `2025-11-26`：`3.114 − 3.034 = 0.080`；其后无分红的两根差值为 0 |

- 判据 E 的意义：恒等式成立即说明**这条腿的事件流足以把上游发布的前复权序列换回不复权序列**，
  这正是 D10 入库前必须做的那一步。容差 0.005 元是给「两边收盘价各自进过一次」留的（ETF 最小变动价位 0.001）。
- B/C 用新浪、E 用「新浪 × ths 两条独立抓取链」，都不是 fuyao 自己 ⇒ 不属自我印证。
- 无分红样本进判据：`159915.SZ` / `512880.SH` 两侧笔数都必须为 0，否则「空事件流」这一路从未被执行过。

## 6. 本轮否决的候选：期货交易日历腿

`/api/futures/calendar/trading-schedule` 是 C13 遗留的下一条日历候选，本轮实测后**不接**，
证据脚本与输出留档 `futures-calendar-probe.py` / `futures-calendar-rejected.txt`（可重放）。两条独立判据：

1. **参数面**：9 种参数写法（含不带参数、`start_date`/`end_date` 的四种命名与格式、带 `exchange`、
   单日 `date`、`year`）全部被上游以 `FUYAO_REQUEST 请求参数缺失或格式非法` 拒绝 ⇒ 文档未标该端点的必填参数名，
   本轮接不进来（猜参数名属"编一个能跑通的调用"，不做）。
2. **信息量**：改从**已转正**的期货日线腿取交易日集合（`CU2610.SHF` 上期所商品 + `IF2612.CFE` 中金所股指，
   对照窗口 `2026-06-01～2026-09-24`），与 A 股交易日历逐日对照 ⇒
   窗口内 A 股开市日 **82** 天，两条合约各 **82** 根日线，**两个方向差集都是 0**（既没有期货独有的交易日，
   也没有日历说了开市而期货没盘的日期）。即便接通该端点，也只是多得到一份与 A 股日历逐日相同的副本。
   夜盘属**日内**时段而非额外交易日，故不构成差异（登记为观察，不作判据）。

处置：该端点状态维持 `available`，映射表消费场景不变（AC-7 计数不受影响）。

## 7. 测试与门禁

- **离线新增 21 个测试函数 / 23 条用例**（`httpx.MockTransport` 合成上游帧，无网络）：
  `tests/test_fuyao_endpoints.py` 9 函数 / 9 例（请求构造 2、归一化 6、取数 1）+
  `tests/test_ths_provider.py` 11 函数 / 13 例（代码解析 4 函数→6 例（畸形帧参数化 3 例）、适配层 7 函数→7 例）+
  `tests/test_main_coverage.py` 1 函数 / 1 例（§8-4 的启动面守卫）；
  另 `tests/test_provider_conformance.py` 按 fetcher 参数化使新腿**零新增用例纳管**（+4）
  ⇒ 门禁用例数 2,499 → **2,526**（22 + 1 + 4 = 27 完全对上）。**同一条算式的反面就是 §8-4 的证据**：
  启动面守卫那 1 例加入之前，本轮两次全量跑都是 `2,524 passed / 5 skipped / 1 error`（收集数
  2,499 + 22 + 4 = 2,525，其中 1 例因 setup ERROR 不进 passed）⇒ 计数与「新增 26 例 + 一条既有真机用例
  被打回 ERROR」两件事同时吻合，没有第三条解释。
- **真机 3 例新增 + 4 例首次真跑**（`live-provider-tests.txt`，`-m e2e` 只读取数）：见 §8-1。
- **`make gate` 全绿**：`GATE_EXIT=0`（读自 `gate.txt` 文件内部，非后台任务的退出码），完整未裁剪日志
  `docs/evidence/C15/gate.txt` **5,735 行** = 26 行运行环境头（跑前采集：时刻 / `branch dev` / HEAD /
  `git status --porcelain` 脏文件清单 / Python 3.13.5 / node v25.1.0）+ 5,709 行原始输出。
  分项：a2-check **242 文件**（C14 为 238，+4 = 新适配层 + 新对照脚本 + §6 的探测脚本 +
  本轮被改动因而被拉进零容忍面的 `tests/test_main_coverage.py`）、public API **431/431** 双 100%
  （C14 为 424）、覆盖率 **85.99%**（阈值 84% 与 `precision = 2` 未动；TOTAL 语句 10,321 → **10,408**，
  +87 全属本轮新增）、前端 8 files / **79 tests**。
- **棘轮出现「improved」但快照未 `--update`**：`ruff_selfdev 274 (snapshot 277)`（§8-5 那次 `ruff format`
  顺带消掉 3 条存量 `E501`）、`ruff_ported 2144 (snapshot 2145)`、`direct_http_ported 1044 (snapshot 1045)`，
  `mypy_selfdev 21` / `bandit_selfdev 4` 持平——按「只降不升」口径改善不冻结，留作下一轮的基线上界。
- **本轮改动面的实测覆盖率**（与 C14 同表对比，不用「大概没降」）：
  `providers/ths/models/fund_action.py` **93.33%**（26 语句 / 4 分支，唯一未覆行 40 = `TYPE_CHECKING`
  块内的 `FetchContext` 导入，与 C6~C14 全库同口径）、`ths/models/_client.py` 58 → **63** 语句、97.44% → **97.65%**
  （新增 `resolve_fund_code` 后仍只缺同一行）、`opendata_fuyao/endpoints.py` 327 → **375** 语句、
  94.93% → **95.03%**（未覆盖行全属其它端点分支）、`opendata/main.py` 155 → **162** 语句、
  67.36% → **70.10%**（§8-4 的守卫没有留下未覆行：语句 +7 而 missing 47 → **46**）。


## 8. 自我更正（都被写进代码或输出，不是事后合理化）

1. **同一个夹具坑第二次踩到，且这次影响面更大**。C13 轮修的是 `tests/test_ths_provider.py` 里
   `TestAgainstTheLiveFuyaoApi` 的凭证门（只看 `FuyaoCredentials.from_environment()`，而适配层实际经
   `settings`/`.env` 解析）；**`tests/test_fuyao_endpoints.py` 里同名的真机类没一起改**，它的 `client` 夹具
   仍然只看进程环境变量 ⇒ 该类 **4 条既有真机冒烟**（日线半开窗口、交易日历、标的检索、复权因子）
   **从建立起一直在 skip**。本轮补上同一入口后 4 条首次真跑并全过（`live-provider-tests.txt`，11 passed）。
   口径订正：C13/C14 记的「真机 3 例 / 4 例通过」都是 provider 侧（注册表路由）那一个类，
   **端点层的真机冒烟此前从未真正执行过**——受影响的是「端点层真机也验过」这一印象，
   转正依据（各轮 `scripts/ops/*_cross_check.py` 的真机输出）不受影响。
2. **`FUND_DIVIDENDS_ENDPOINT` 漏登记在映射表漂移守卫的「代码侧已落地端点」清单里** ⇒
   `tests/test_fuyao_endpoint_map.py::test_implemented_entries_match_the_shipped_constants` 本轮 `make gate`
   首跑即 FAIL（映射表 16 条 vs 代码 15 条）。这不是测试写错，是那条断言的**设计目的**：新增端点必须显式登记两处，
   少登记一处就会把「文档里有这条端点」冒充成「代码里真的调它」。修法是把它加进
   `endpoint_map.py::implemented_paths_in_code()` 的常量清单并把计数从 15 改为 16（13 REST + 3 dump），
   **不是**放宽断言。
3. **一处文档串日期写错**：`fund_action.py` docstring 初版把恒等式的实测 bar 日期写成 `2025-01-03`，
   实测帧是 `2025-01-02`（差值 0.211 不变），已改正——留档是因为这类日期一旦被后人当取样依据就会取错一根。
4. **门禁的 `1 error` 不是偶发噪声，本轮修掉了根因**（原登记为 C7 的「另轮处理」观测，提前到本轮）。
   `make gate` 连续两轮（同一棵树跑两次）都在**同一处**失败：
   `tests/test_data_subscribe.py::TestSocketAuth::test_first_frame_must_be_auth` setup ERROR，
   `RuntimeError: Task <... anyio.from_thread.BlockingPortal._call_func ...> got Future <Future pending>
   attached to a different loop` ⇒ 用例数增长改变了 `-n 8` 的分片分布后，它从「偶发」变成了**确定性**。
   **根因链**：`ws_client` 夹具用 `TestClient(app)` ⇒ 跑真实 lifespan ⇒ 非生产分支的 `create_tables()` +
   `init_db()`，外加 `task_scheduler.start()` 里的 `_load_active_tasks()`（读 `scheduled_tasks` 表），
   三者都走**模块级 async engine**（`settings.database_url` 指向真实 MySQL），而 aiomysql 连接绑定在
   打开它的那个 loop 上、每个 `TestClient` 又新起一个 anyio loop ⇒ 复用到上一个 loop 留下的死连接。
   **一处过程性更正**：第一版只挡住 `create_tables`/`init_db`，用更便宜的**三文件串行**复现命令
   （`tests/test_main_coverage.py tests/test_main_app.py tests/test_data_subscribe.py`）验证 ⇒ 仍失败，
   traceback 换到 `opendata/services/scheduler.py:81 _load_active_tasks`。也就是说
   **DDL 只是这个缺陷最先显形的地方，不是缺陷本身**；判据应当是「测试进程连不连数仓」而非
   「DDL 跳没跳过」。补上 scheduler 的 guard 后同一复现命令 **68 passed / 0 error**。
   **另一处坑（本轮只登记不改）**：`tests/conftest.py` 在 `from opendata.main import app` **之后**才设
   `os.environ["TESTING"] = "true"` ⇒ 模块级常量 `main.TESTING` 在测试 worker 里一直是 `False`，
   conftest 注释所说的「用它禁用限流」从未生效。调整导入顺序会让整个套件的限流/请求日志中间件/日志文件
   行为一起翻面，属另一轮范围 ⇒ 本轮改为让 lifespan 经 `_testing_mode()` **在调用时**读环境变量，
   既不动既有中间件口径，又使测试进程不再连数仓。测试面同步钉住两个方向：
   新增 `test_lifespan_skips_the_database_bootstrap_in_a_test_process`（`TESTING=true` ⇒
   `create_tables` / `init_db` / `scheduler.start` 三者均**不被调用**），并把既有
   `test_lifespan_startup_shutdown` 显式改为 `TESTING=false`（它断言的是「非测试进程照常 bootstrap」，
   此前隐式依赖那个导入顺序才成立）。
5. **a2-check 的判定面按「自 A0 基线以来新增或改动的文件」计算** ⇒ 一旦动了
   `tests/test_main_coverage.py`，它**原有**的 3 条 `E501` 立刻进入零容忍判定（gate run #5 即因此挂在
   `FAIL ruff check` + `FAIL ruff format --check`）。处置是 `ruff format` 该文件（行为不变，7 passed），
   **不是**把它移出判定面、也**不是**放宽规则——「动了的文件达到 A2 标准」正是该门禁的设计意图。


## 9. 遗留（明确不做 / 做不了，含原因）

- **`dwd_fund_action` 未落库**：写数仓属生产写入且需建表，按既定纪律**须明确确认后执行**；
  本轮交付的是验收面 + 可路由 producer。
- **`fund_etf_daily` 仍未接**：判据 E 证明的是「换算键够用」，不是「换算已实现」。要做的是
  在入库前用事件流把 ths 的前复权序列换回不复权（并处理 E 段登记的容差来源：两边各自进位过一次），
  那是价格侧的一步，不属这条事件腿。`/api/fund/market/historical` 状态仍 `available`，
  映射表场景文案已改成本轮实测语义（「不发 `adjust`、只回前复权 ⇒ 等本地换算」）。
- **场外基金未接**：分红端点只认带后缀的场内 `thscode`，而 ETF 目录不含场外代码 ⇒
  场外基金必须合格码直传，裸码一律 `THS_FUND_SYMBOL_UNRESOLVED`（不做名称搜索兜底：
  搜索结果按名字命中会把「一只股票的答案」冒充成「一只基金的答案」）。
- **份额分红 / 红利再投未落**：上游该行没有份额分红列，`reinvestment_date_ms` 是再投选项日期而非份额，
  契约 `stock_dividend` / `rights_*` 因此恒 0（是**无此字段**，不是漏映射）。
- **em 通道 4 例（AC-6）仍挂起**：本轮再次探测 `push2his` / `push2delay` 根路径回 404（TLS/HTTP 可达），
  但 `/api/qt/stock/kline/get` 仍是 `000` 空回（curl 直接观测）⇒ 与 C11 同一结论，属网络侧非代码，
  判据不放宽。
- **AC-13 校准 box 仍不勾**：本轮没有新增任何「当日收盘时点能否拿到当日数据」的观测点。

## 10. 附带发现：巡检探针的参数面按「域」取，12/19 条 verified 能力每轮被误标不可用（本轮只登记不修）

登记原因：本轮把 `fund_action` 注册为 ths 第 10 条腿之后，`opendata/pipeline/patrol.py` 的覆盖面问题从
「偶有一两个域没参数」变成**有半数以上的已验证能力在每轮巡检里必然失败**，属 AC-4 的缺陷而非本轮功能，
故只把判据与量化结果做实、把修复另立一轮（C16）。

审计是离线的（无网络、不连数仓），只跑探测流水线的**第一步** `transform_query`，
参数就用 `patrol.PROBE_PARAMS` 会给的那一份 —— 输出留档 `patrol-probe-audit.txt`（本文件即脚本，可重放）：

| 探针结果 | 条数 | 明细 |
| --- | --- | --- |
| 可过 | **7** | `ecb/economy_{cpi,gdp,rate}`、`ths/stock_daily`、`ths/stock_action`、`ths/trading_calendar`、`yfinance/stock_daily_overseas` |
| 必抛 `ValidationError` | **12** | 7 条 ths 腿（`index_daily`/`index_constituent`/`financial_statement`/`fund_action`/`instrument`/`futures_daily`/`option_daily`）+ `imf/economy_*` ×3 + `oecd/economy_*` ×2 |

两条根因是同一句话——**参数按域取，不按 (域, 源) 取**（`patrol.py:166`：`params = PROBE_PARAMS.get(domain, {})`）：

1. 那 7 条 ths 腿所在的域**根本不在** `PROBE_PARAMS` 里 ⇒ 空参数 ⇒ `symbol` / `asset_type` 必填字段缺失；
2. `economy_*` 一组的参数是 fred/ecb 口径的 `series_id`，而 imf 的 `IndicatorQuery` 要 `indicator`、
   oecd 的 `CpiQuery` / `UnemploymentQuery` 要 `series_key` ⇒ 同域不同源、字段名不同，一份参数喂不了三家。

后果有两层，且**方向相同**：探测抛错即 `registry.mark_unavailable(fetcher)`（`patrol.py:138-141`，进程内、非持久），
而 `source="auto"` 恰好跳过被标不可用的 fetcher ⇒ **巡检每跑一轮就在削弱它自己刚验过的那批路由**；
同时 `scripts/ops/health_patrol.py` 的「失败累计即非零退出」会天天报 **12 条假故障**（告警面被噪声占满，
真故障反而看不出来）。另登记一条：`PROBE_PARAMS` 里的 `stock_adjust` 在当前注册表**没有**对应的 verified 能力 ⇒ 死配置。

C16 的验收面（写成判据，不接受「看起来好了」）：参数键改为 `(domain, source)`；
一条**按注册表参数化**的守卫用例，断言「每条 `verified` 能力都必须有能过 `transform_query` 的探针参数」
（这样以后新增腿忘了配参数会当场 FAIL，而不是等到生产里路由被削弱才发现）；真机跑一轮巡检并留档，
要求 `failed == 0` 且逐条打印 19 条的判定。

