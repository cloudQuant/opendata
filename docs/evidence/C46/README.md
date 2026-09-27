# C46 — AC-18|01「缺失真的触发告警」与 AC-18|02「目录五个读数各就各位」：把「接口有」和「读数量得出」分成两件事

本轮闭合 AC-18 三格里能自己挣的两格（`条目级 0/3 → 2/3`）。两格判据原文各自的前半早就有面、后半从来没有：

| 条目 | 判据原文（回填前 line 233 / 234 / 235） | 修前已有的面 | 修前没有的面 |
|------|------|------|------|
| AC-18\|01 | **新鲜度接口与告警**：各域最新数据日期与滞后天数可查；**缺失触发告警**（迭代 1A） | `/domains/{domain}/freshness` 路由、`freshness.evaluate_alerts()` 判定函数、A4.8 矩阵的单测 | ①**没有任何生产代码调用告警矩阵**（判定函数只在测试里跑）；②**没有任何东西把告警投到通道上**；③`freshness` 模板**不在 `EXECUTABLE_KINDS`** ⇒ `register_builtin_jobs` 直接 continue，调度声明是死的；④门的 `no data` 分支不报基准日（滞后天数无从核对） |
| AC-18\|02 | **数据目录页/接口**：按数据域组织，展示**覆盖标的数、时间范围、各源最近更新、新鲜度、质量标记**（迭代 1B） | `/data/catalog` 端点、`/data` 页面、按域一行的 `latest/lag_days/status` | ①五个读数里**四个从未被后端构造**（`data_catalog` 只发 11 个键）；②「各源最近更新」用 **dwd 表 + mapping 源列**，两者不同源 ⇒ 每条腿必然 `missing`；③页面无覆盖/时间范围/质量列；④质量标记把「表不存在」与「表存在且为 0」混成同一个读数 |
| AC-18\|03 | 前端"数据接口"页与数据目录**合并**（目录为默认视图，函数级明细下钻） | 两条路由两个视图；接口清单按 §11.1 已以域名命名（`data_interfaces.name == capability.domain`） | 页面合并本身——而合并必然要让一个导航项消失 ⇒ **本轮判 gap，不判 proven**（§六） |

三条判据现在各有一个探针（`scripts/quality/acceptance_item_probe.py`），**23 条探针 / 162 条反事实**全表 self-test `exit=0`，AC-18 自己的 **23 条反事实**逐条把 clean 读数打回 gap。

---

## 一、先量：同一份脚本跑两棵树，差集就是本轮做过的事

`before_census.py` 只做静态与注册表读数（AST + registry + 路由），`--root` 指哪棵树就读哪棵。跑在 `git archive HEAD` 解出的树上 = 动手前（`before-census-head.txt`），跑在工作树上 = 动手后（`after-census-worktree.txt`）。四条读数：

1. **目录键面**：动手前 `data_catalog` 及其调用闭包只构造 **11 个键**，判据五项**缺 4 项**（覆盖标的数／时间范围／各源最近更新／质量标记一个都没有）；动手后 **29 个键**、**缺 0 项**。
   - 闭包必须是**名字引用**而不是调用点：`_coverage_facts` 把真正的行计数交给 `asyncio.to_thread(_read_coverage, …)`，第一版按 `ast.Call` 找边 ⇒ `symbols/start/end` 全看不见，读数停在「缺 2 项」。**一个漏看一半键的判据面不是判据面**，所以修的是闭包定义。
2. **源腿 mapping 面**：33 条注册源腿里 **26 条无字段映射**（`ods_freshness` 必 `LookupError`），只有 7 条取得到源列——这是**多数**而不是例外，所以它被钉成一条用例（`test_a_leg_with_no_field_mapping_is_the_measured_majority`）而不是写在文档里。动手前目录问新鲜度用的是 `dwd_table(domain)`、列名却按 `mapping` 的源列取 ⇒ **每条腿只能读成 `missing`**；动手后 `_source_leg` 走 `ods_table(domain, source)`。
3. **告警生产者面**：动手前引用 `evaluate_alerts` 的非测试文件只有 `opendata/pipeline/freshness.py` 自己（定义处），`jobs.py::_execute_freshness` **不存在**，`_execute_template` 既不引用 `FRESHNESS` 也不引用 `broadcast`。动手后生产者是 `alert_matrix.py + freshness.py + jobs.py`，投递文件含 `alert_matrix.py` 与 `jobs.py`。
   - 判据原文「缺失触发告警」要的是**边**，不是**点**：判定函数与 WS 通道各自都存在、各自都有单测，中间那一段（谁调用、投给谁）从来没有人写过。矩阵此前**只在自己的测试里跑过**。
4. **调度 kind 面**：动手前 `EXECUTABLE_KINDS = ['INCREMENTAL']`，`schedules.yaml` 声明的 4 个 kind 里 **3 个（FRESHNESS/FULL_CHECK/PARTITION_MAINTENANCE）没有执行器**；动手后 `FRESHNESS` 进集合，剩 **2 个**（`full_check`/`partition_maintenance`，本轮不做，§十登记）。
   - 这一面差点读错：`kind: incremental`（yaml 小写）↔ `TemplateKind.INCREMENTAL`（代码大写），第一版按字面比对把 `incremental` 也算成 stranded ⇒ 读数「4 个 kind 全没有执行器」是假的。大写归一后才是 3 个与 2 个。

## 二、|01 的处置：补上「判定」与「投递」之间那段空白，而不是给判定函数再写一个单测

新增 `opendata/pipeline/alert_matrix.py`（343 行，本轮唯一的新生产模块）：

- `registered_legs()` —— 域→源腿的**注册表真值**，含「注册了却一条腿都没有」的域（空 tuple），所以目录/矩阵/探针问的是同一份腿清单，不再各数各的（§一 的分支标注就是为了暴露两种数法）。
- `collect_freshness(engine, expected, domains)` —— **两层都测**：`dwd_<domain>` 答「合并后的数据新不新」，`ods_<domain>_<source>` 答「这个源到底有没有到货」，后者才是把上游故障与回补没跑分开的读数。返回 `(reports, MatrixScope)`，scope 里 `unmapped_legs` 单独计数。
- **`unmapped` 不冒充 `missing`**：源没注册字段映射时 `_ods_leg` 返回 `None` 并被计数，而不是转成一条谁都无从处置的 `missing` 告警。「量不出」与「量出来是空的」是两个 claim。
- `run_alert_matrix(..., broadcast=...)` —— 判定完真的投递；通道拒收时 `logger.warning` 并保留告警（`test_a_channel_that_refuses_the_frame_does_not_lose_the_alerts`），不让一次投递失败吃掉判据。
- `expected` **由调用方给**：模块内不读墙上时钟（`test_the_verdict_follows_the_supplied_expectation_not_the_wall_clock`），周末不 page 人——这条是 C12 的日历判据在告警侧的落点。
- `jobs.py`：`_execute_freshness` 执行器 + `TemplateKind.FRESHNESS` 进 `EXECUTABLE_KINDS` + 把 `ws_manager.broadcast` 注入进去；`schedules.yaml` 的 `freshness-check` payload 从 `{domains: p0}` 改成 `{domains: all}`（只测 p0 的话，20 个注册域里 18 个空表永远不会成为一次告警，「缺失触发告警」在配置面上是空的）。
- `freshness.py::_as_date` 接受字符串：`MAX(<date列>)` 在不给聚合结果做类型的驱动上回的是文本（SQLite 就是），**有行的表不能因为驱动的类型习惯被读成 `missing`**——那会为一个类型去 page 人。同理 `data_query._iso()` 不再假设值必有 `isoformat`（mypy `no-any-return` 的根因），改为显式 `getattr` + `callable` 检查、拿不到就 `TypeError`。

**判定面**：门 6 节点（`TestTheFreshnessDoor` 4 + 404 校验 1 + 日历基准 1）、告警/执行器 9 节点（`TestCollection` 2 + `TestDelivery` 5 + `TestFreshnessExecutor` 2）、加四个静态接线事实（路由在、交出 `latest+lag_days`、**两条分支都报 `expected_data_date`**、调度三件）。
「门只在成功分支报基准日 ⇒ gap」是本轮自己加的一条反事实：滞后天数离开它被量出来的那一天就没有意义，`no data` 分支不报基准日的接口，读起来像能用、实际无从核对。

## 三、|02 的处置：五个读数各有出处，且「列名／形状／单元断言／真机断言」四面齐了才认

- 后端：`_coverage_facts`→`_read_coverage`（`rows`／`symbols`（主键列去重、不含日期列）／`start`／`end`／`symbol_columns`）、`_quality`（`diff_flagged`／`diff_report_rows`／`flag ∈ clean|flagged|unmeasured`）、`_source_leg`（`status ∈ fresh|stale|missing|unmapped` + `reason`）、抬头 `expected_data_date`／`domains_total`／`source_legs_total`。
- **`quality` 只在 `coverage is None` 时才是 `None`**；`unmeasured` 只用于 `_diff_flag` 列本身不存在的情况。`_diff_report_counts` 把「表没迁移」（`None`）与「表在、这个域 0 条」（`{}` 里没有该域）分开（`test_a_missing_diff_report_table_is_not_reported_as_zero`）——把前者读成 0 就是把「没有对账面」报成「对账干净」。
- 前端 `DataCatalogView.vue`：9 列 → 数据域／域标识／资产类别／**覆盖**／**时间范围**／**新鲜度**／**各源最近更新**／**质量**／操作；`基准日 {expected_data_date} · {domains_total} 域 · {source_legs_total} 源腿` 抬头；`滞后 {n} 天`、`已验证|未验证`、`未映射`、`一致|有差异|未测量` 各自是页面上读得出的文案，下钻预览报 `共 {count} 行（最近 20 行预览）· 表 {entry.table}`。
- `frontend/src/api/catalog.ts` 的 `CatalogEntry` 从 9 字段到 12（补 `coverage/quality/sources` 等），载荷类型与页面显示的是**同一次量出来的东西**。
- **五读矩阵的判据**：每个判据点名的名词必须同时有①列名（`label="覆盖"` 等）②页面形状（`formatCoverage/formatRange/formatLatest/formatLeg/QUALITY_LABEL`）③单元断言行（`expect(…标的/~/已验证/滞后/未测量…`）④真机断言行（`asserted_lines(e2e, …) > 0`）——四面齐才记到位。少任一面都是「看起来在工作」：只有列名 = 渲染过但没人断言；只有断言 = 断言的字段页面上没有。
- 单元面 8 例（`catalog.test.ts`）+ 信封 5 例（`api/envelope.test.ts`，走真实 `@/utils/request` 的响应拦截器）+ 真机 18 例（`frontend/e2e/scripts.spec.ts`，本轮 `make gate` 的 `frontend-e2e` 段真跑：`Running 18 tests` → `18 passed`）。
- 采集面：`collector_ok` 复算 `vite.config.ts` 的 `parse_test_excludes` 与 `frontend_test_collection.ALLOWED_EXCLUDES`，未授权排除即 gap（C29 那条规则在目录面上重新被咬了一次）。

## 四、判据面自己必须能红：23 条反事实，其中两条是「不咬」教训换来的

`|01` 10 条 / `|02` 9 条 / `|03` 4 条。标签见探针，几条值得一提：

- **两条一开始不咬**：`门的节点被改名` 与 `缺失不再触发告警` 植入后 verdict 纹丝不动——judge 根本没读 `query_bad`/`alert_bad`。修法是给 judge 加门（`query_bad == "-"`、`alert_bad == "-"`），并给 `|02` 补一条对称的 `bad == "-"` 门与反事实。**反事实暴露的是判据的洞，处置是补判据而不是删反事实。**
- `目录节点被改名（那一读再也没有人测）`、`覆盖标的数只剩列名（断言被删）`、`真机页不再逐读数断言`、`载荷类型不再带 coverage/quality 字段`、`页面不再显示滞后基准日`、`单元面被采集器的排除规则吞掉`：每条都对应 §三 四面里的某一面被单独摘掉，四面不是互相冗余。
- 仪器侧缺陷（会**假绿/假红**的那种）：`function_body()` 只认 `ast.FunctionDef`，于是所有 `async def` 被读成「不存在」——`domain_freshness`/`_execute_template`/`_execute_freshness` 三个都是 async，第一版 `|01` 因此报出 3 个「事实为 no」的 gap。**用修 helper 的方式解决，而不是给 async 网开一面**（此前唯一的调用点 `normalize_frame` 是同步函数，改动无 retro 影响）。

## 五、门禁：三遍，第一遍红在自家新增文件上

| 遍 | 档 | 结果 | 说的是什么树 |
|----|----|------|------|
| attempt-1 | `gate-run0-zero-dep-fail.txt` | **`GATE_EXIT=2`**，停在第 2 个成员 `zero-dep-check` | 回填前的树，`opendata` 包多了一个文件 |
| run1 | `gate-run1-prebackfill.txt`（7,178 行 `wc -l`，17 段 `===== gate:` = 16 成员 + `PASSED`） | `GATE_EXIT=0`，`3140 passed / 6 skipped`，覆盖 **88.91%**（阈值 84%），前端 `109 passed` + e2e `18 passed` | 回填**前**（§2/§10/ledger 一字未改） |
| run2 | `gate-run2-postbackfill.txt`（7,196 行 `wc -l`，17 段） | **`GATE_EXIT=0`**，`3140 passed / 6 skipped`，覆盖 **88.91%**（与 run1 逐数相同），`ledger-check` 打印 `items=130 proven=25 gap=8 unreviewed=97 ticked=25`，`a2-check` 扫 **365** 个文件 | 回填**后**的最终树（台账 |01/|02 翻成 proven、|03 翻成 gap，文档 v5.18，`docs/evidence/C46` 已 staged） |

- **run2 的日志不写进 ledger 的 `evidence`**：`acceptance_ledger_check` 要求一条 proven 引用的每个路径**既存在又已被 git 跟踪**，而 run2 正是来验证这次翻勾的那一遍——把它自己列进去会在 run2 内部先红。所以两处 `evidence` 只引到 `gate-run1-prebackfill.txt`，run2 由本 README 与 §0/§10 的散文引用承载（散文路径不受该校验）。
- **同一时刻 `evidence-traceability` 的 `untracked` 面也容不下它**：run2 跑的时候这份日志还不存在／还没进索引，所以它在 run2 内是一条 `NEW VIOLATION: docs/evidence/C46/gate-run2-postbackfill.txt#untracked`；跑完才 `git add` 并单独复跑该成员转绿（`untracked gaps = 0`）。**run2 日志里没有这条读数，是本档案的固有顺序而非疏漏。**
- **回填本身第一次被 `ledger-check` 扳红，红点值得记**：我按「勾上要写清是哪一轮转正」的直觉给 §2 两条判据行各追加了 `（C46 条目级 \|01 转正）`，结果 `ledger-check` 立刻报 4 条——`AC-18|01|2524a54e` 变成「entry matches no item」，同时冒出 `AC-18|01|aa8b7cbb`「item has no ledger entry」。**台账的键是判据原文的 8 位内容哈希**，改一个字（哪怕只加后缀）就等于换了一条判据。处置是把两条行还原为原文只翻 `- [ ]`→`- [x]`（轮次归属本来就该住在 §0 与 §10），**不是**把哈希键跟着改过去——那等于悄悄重定义判据。

- **attempt-1 的红是设计出来的红**：`verify_no_akshare.py` 的 baseline 把「扫了多少个文件」也冻住，本轮新增 `opendata/pipeline/alert_matrix.py` ⇒ `opendata archived 191 but walks 192`。这条守卫防的是「包少了文件 = 那个包不再被看」，但按同一句话它也拒绝「档案里的数字没人能读成证据」。处置是 **`--update` 有意复冻**（`docs/quality/zero-dep-baseline.json` 的 diff 只有 `"opendata": 191 → 192` 一行，frozen findings 仍 3 条一字未动）+ 在这里写明，**不是**放宽守卫、也不是把新文件排除出扫描面。
- `6 skipped` 逐条归因（与 C45 完全同一组，本轮**没有新增静默 skip**）：`test_port_fidelity.py` 的 `stock_daily_raw`／`stock_daily_qfq`／`index_daily_em`／`fund_etf_daily_em`／`qfq_factor_steps[em]`／`qfq_synthesis[em]` 六条，全部挡在「录制夹具未补录」上，即 task #14 那条挂了 10 轮的 em 面。**它们不是本轮的 AC-18 面**，但按惯例逐个点名，因为「live 面悄悄 skip」是本仓真翻过两次车的地方。
- 回填前先在 A2 面红过三处（`a2-check-first-sweep.txt`）：①我自己的 scratch 目录被当未跟踪文件卷进 A2 集（scratch 挪出仓库，`跑完即删`）；②`tests/test_opendata_client.py` 未格式化；③`data_query.py:870` 的 `no-any-return`。三处都是**改文件**修掉的，`exclude_lines`、阈值、bandit skips 一字未动。
- 本轮另有一处**不在门禁里**的漂移如实登记：`make frontend-format`（prettier `--write src/`）会重写 **46 个文件**。它不是门禁成员，也没有要求本轮的树 prettier-clean，所以**未执行**、只登记为后续项（§十）。

## 六、数仓现场读数：接口给得出、现场量得出，但现场大多是空的（所以这不能作为「达标」的证据）

`warehouse-census-live.txt`（只读：`information_schema` + `COUNT(*)` + 新鲜度 `SELECT`；无 DDL、无写入；不打印连接串）。读数：

- 表面：**dwd 6 张 / ods 3 张 / 其他 3 张**（`alembic_version_data`、`batch_watermark`、`dq_diff_report`）。6 张 dwd 里**只有 2 张有行**：`dwd_stock_daily` 719,316 行、`dwd_stock_adjust` 1,032,323 行；`dwd_financial_indicator`／`dwd_financial_statement`／`dwd_index_constituent`／`dwd_stock_action` 全 0 行。
- 日历面：`dwd_trading_calendar` **不存在** ⇒ `warehouse_calendar` 回落 weekday 日历（`resolve_calendar` 打了一条 WARNING），`expected_data_date(2026-09-27) = 2026-09-25`。**也就是说 A4.7 的权威基准在当前数仓上是由周内规则给的，不是由落地表给的**——判据没要求日历落地，但这一条必须写在明面上（落地需要 task #52 那个 `--write` 决定）。
- 新鲜度面：20 个域 + 7 条源腿出报告（`fresh=1 stale=4`，`missing=22`），26 条腿 unmapped——**26/33 与 §一 的静态读数逐条相等**，静态面与现场面对上了，这才算互证。
  - 现场样本：`stock_daily/-` latest=2026-09-24 lag=1 stale、`stock_daily/ths` 同、`stock_daily/akshare` latest=2026-07-21 **lag=66** stale（退路自 7 月起没跑过）、`stock_action/ths` latest=**2026-10-09** `lag=-14` 判 **fresh**。
  - **负滞后**是本轮量出来、没有改的语义：除息日在上游是**未来日期**的合法记录，`latest > expected` ⇒ `lag_days < 0` ⇒ 状态 fresh。判据只要求「滞后天数可查」，今天它可查且为负；但「滞后 -14 天」在 UI 上读起来是反的，把它做成显式文案（或不参与新鲜度）需要一次口径决定 ⇒ 登记 §十。
  - 这条读数是本轮**唯一一条会让 `fresh` 看起来可疑**的现场样本，写在这里而不是修掉，是因为「谁的数据跑到未来」与「新鲜度判据成不成立」不是同一件事。

## 七、显式登记：本轮没跑、不 credit、不做的事

1. **未跑** 1 条真数仓 e2e 见证：`tests/test_opendata_client.py::TestAgainstTheRunningApi::test_every_catalog_reading_reaches_a_consumer`（`@pytest.mark.e2e`，用 `live_server` + `warehouse` 夹具）。跑它意味着 app lifespan 对配置里的 MySQL 做 DDL——本仓有前科（C17），**没有用户同意不跑**。`|01`/`|02` 的判定面全部离线可算（门的同一张脸在 SQLite 上由 `TestTheFreshnessDoor` 4 节点量），所以**没有任何 verdict 依赖这条未跑的用例**。
2. **不 credit**：探针一律 `-m "not e2e" --no-cov`，因此后端真机面（那条未跑的 e2e）不计入 verdict；前端真机面计的是**断言行是否存在**（`asserted_lines`），其「真跑过」由 `make gate` 的 `frontend-e2e` 段（18/18 通过）承担，两件事分开写。
3. **`|03` 判 gap 而不是「差一步」**：`nav_entries=1`（`/scripts` 标题仍是「数据接口」）、`route_is_catalog=no`、`detail_in_catalog=no`。合并要求的是**一个页面**：目录为默认视图、函数级明细在它下面。join 的料已经齐（§11.1 让 `data_interfaces.name == capability.domain`、`display_name`/`category`/`return_type` 都从域派生），缺的是页面合并本身，而合并必然让一个导航项消失 ⇒ **产品决定**，不在本轮自作。
4. **`AC-11|06`（`GET /api/v1/data/catalog` 与 `/{domain}/freshness` 可用）本轮不勾**：它的见证这一轮确实补齐了（门的 6 节点 + 目录 7 节点 + 客户端面），但那一格**还没有探针**，勾它会重犯 C36 之前那种「按 AC 级证据打条目级勾」的错。登记为后续（§十）。
5. 探针不是门禁成员：`acceptance_item_probe.py` 不进 `make gate`，红绿灯仍由那 16 个成员承担。

## 八、这一格现在证明什么、不证明什么

- `|01` **证明**：新鲜度门两条分支都交出 `latest`/`lag_days`/`expected_data_date`；缺表→`critical`、合并层滞后→`warning` 的告警**真到达 WS 通道**（投递失败不吞告警）；`FRESHNESS` 是**可执行 kind** 且执行器被交给 `broadcast`；判定基准是注入的日历日，不是墙上时钟。**不证明**：调度真的在跑的进程里跑过（那是 §7 真机场景 / AC-13 的面），也不证明 `full_check`/`partition_maintenance` 两个 kind 有执行器（本轮只接了 freshness）。
- `|02` **证明**：五个读数在后端载荷里构造得出（闭包 29 键）、在页面上有列名与形状、在单元面与真机页各有一条读它的断言、`unmapped` 与 `missing` 与 `unmeasured` 是三个不同读数。**不证明**：数仓里真的有数据可看（§六 恰好证明大多没有）、也不证明 `dq_diff_report` 对账面全库覆盖。
- `|03` 只证明「还没合并」，并给出补齐到可判的三处读数。

## 九、复算

```bash
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"; export PYTHONPATH=.
python docs/evidence/C46/before_census.py --root .                      # 动手后静态读数
git archive HEAD | tar -x -C /tmp/head && python docs/evidence/C46/before_census.py --root /tmp/head   # 动手前
set -a; . ./.env; set +a; python docs/evidence/C46/warehouse_census.py   # 数仓现场（只读）
python scripts/quality/acceptance_item_probe.py --item 'AC-18|01'        # proven
python scripts/quality/acceptance_item_probe.py --item 'AC-18|02'        # proven
python scripts/quality/acceptance_item_probe.py --item 'AC-18|03'        # gap（带 reason）
python scripts/quality/acceptance_item_probe.py --self-test              # 23 探针 / 162 反事实
```

## 十、本目录档案清单

`README.md`｜`before_census.py`（`--root` 可复跑）｜`before-census-head.txt`（动手前，62 行）｜`after-census-worktree.txt`（动手后，61 行）｜`warehouse_census.py`（只读数仓）｜`warehouse-census-live.txt`（84 行，含 loguru 的 stderr）｜`probe-ac18-01.txt`／`-02.txt`／`-03.txt`（`exit=0`）｜`self-test.txt`（`exit=0`）｜`gate-run0-zero-dep-fail.txt`（attempt-1，`GATE_EXIT=2`）｜`zero-dep-census-after-refreeze.txt`（复冻后读数）｜`a2-check-first-sweep.txt`（回填前三处红）｜`gate-run1-prebackfill.txt`／`gate-run2-postbackfill.txt`（完整未裁剪，各带跑前 provenance 抬头）

## 十一、后续登记（本轮不做）

1. **C47 = AC-18|03 真实合并**：`/data` 为唯一入口、`/scripts` 收为目录默认视图，函数级明细经 `/api/v1/data/interfaces`（`name == domain`）进下钻；**需要用户拍板删哪个导航项**。
2. **AC-11|06 探针**：见证已齐，缺判据面；补上才能勾。
3. **负滞后口径**：`latest > expected` 是显式文案（「数据已到未来」/「预告除息日」）还是不参与新鲜度；本轮只登记读数。
4. **`dwd_trading_calendar` 落地**：A4.7 的权威基准目前在数仓上回落 weekday，落地脚本 `metadata_backbone_landing.py --write`（65,895 行 / 9 页）需要同意（同 task #52）。
5. **`full_check` / `partition_maintenance` 两个模板 kind 仍无执行器**（声明在、cron 上不去）。
6. **`make frontend-format` 的 prettier 漂移**：46 个文件，非门禁成员，要不要收（收则单独一轮，避免与判据改动混在一个 diff 里）。
7. `_as_date` 在全库有 4 处同形副本（本轮只修了 freshness 这一处），`_key()` 不含 macro 身份列，26 条 unmapped 腿的 mapping 补齐面。
8. `acceptance_ledger_check._tracked()` 仍受 git `core.quotepath` 影响（正解是 `git ls-files -z`），因此本目录档案一律 ASCII 名。
