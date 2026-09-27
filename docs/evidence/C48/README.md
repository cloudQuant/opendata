# C48 — AC-9\|02/04/05「注入差异被捕获 + 告警治理生效 + 告警双通道到达」：三格判据在修前都没有生产读者

本轮把 AC-9 九格里判据原文点名「校对作业捕获 / 告警治理 / 告警双通道」的三格，从「判定函数与渲染函数都存在」做成「有生产触发器、决策真的到达两个通道、每个通道做了什么可报告」。条目级 `0/9 → 3/9`。

三格判据原文（回填前在 `验收文档.md` L173/L175/L176；本轮 v5.20 修订行插入后随之下移一位 ⇒ 现在是 L174/L176/L177）：

| 条目 | 判据原文 |
|------|------|
| AC-9\|02\|ed680e97 | 注入差异样本被校对作业捕获，`dq_diff_report` 记录 domain/源对/key/字段/两源值/偏差/verdict |
| AC-9\|04\|dcfe5ab7 | **告警治理**：白名单容忍项生效、相同差异不重复告警、差异率突增才升级 |
| AC-9\|05\|7ed53e9b | 告警双通道：SMTP 邮件 + WS `data.diff_alert` 事件广播 |

## 〇、修前的真实形状（读自 `git show HEAD:…`，HEAD=`1f5455d`）

| 判据点名的面 | 修前已有的面 | 修前没有的面 |
|------|------|------|
| 「被**校对作业**捕获」 | `CrossCheckService.run/run_hook`、`DiffReportWriter.write`、`dq_diff_report` 表在位（判据点名的 9 列 9/9）、`TemplateKind.FULL_CHECK` 与 `schedules.yaml` 的 `p0-weekly-full-cross-check` 行 | **`FULL_CHECK` 没有执行器**：`EXECUTABLE_KINDS == {freshness, incremental}`，读数面 1 直接打印「声明了却没有执行器的 kind = ['full_check', 'partition_maintenance']」。而 `register_builtin_jobs` 对不可执行的 kind 是 `logger.info(...) + continue`（`jobs.py:618-620`）⇒ 那行 `cron='0 2 * * 0'` **从来没有装进任何调度器**。剩下的生产 step-3 只有增量流水线一条路，`build_stock_daily_pipeline` 要求 `second_source` 才挂 step-3，而 `schedules.yaml` 的每日 payload 实测只有 `['domain','source','window']` ⇒ 真机上 step-3 从未跑过：`dq_diff_report` 100 行全在手工批次 `dual-source-2026h1` 下，形如 `hook_batch_id()` 产出的 `xcheck:` 前缀批次 **0 组 / 1 组**（面 5） |
| 「白名单容忍项生效」 | `AlertPolicy.whitelist` 字段、`decide()` 里的白名单分支、它的单测 | **白名单没有来源**：`opendata/` 全树没有任何治理状态文件，唯一的 `CrossCheckService` 构造点 `templates.py:551` 不传 `policy=`（面 2 的 AST 扫描：生产侧 3 处引用，其中 `cross_check_service.py:81` 是 `AlertPolicy()` 兜底定义本身） |
| 「相同差异不重复告警」「差异率突增才升级」 | 同一实例内 `alerted` 集合与 `last_rate` 基线都工作（面 3 实测：同一实例第 2 次 `alert=False reason='this difference pattern was already alerted'`，`last_rate=0.01 → 0.30` 给 `critical`） | **状态的生命周期**：面 3 实测「同一差异、两个新建策略：alert#1=True alert#2=True」。因为每次比对新建一个策略，去重集只在**一次调用内部**成立——「不重复告警」当时为真的那个意思是「同一批里没重复」，而不是「上一轮报过的这一轮安静」；「突增才升级」同理永远拿不到上一轮的差异率 |
| 「SMTP 邮件 + WS 广播」 | `diff_alert_message()`（渲染 `data.diff_alert`）、`SubscriptionHub.publish_diff_alert()`（按订阅域过滤）、`ws_manager.broadcast`、`NotificationService._smtp_send` | **把 `(summary, decision)` 喂给它们的调用方一个都没有**：修前 `Notifier` 契约签名只收 `summary`（拿不到 level/reason，也就无法解释这条告警为什么存在），`CrossCheckService` 构造时不传 `notifier=` ⇒ 决策算完即丢。渲染函数与投递函数在 `tests/` 有 39 处引用（面 2），生产侧 0 处 |

三点结构性事实（不是措辞问题，是可达性问题）：

1. **kind 声明与执行器是两个集合，调度登记按后者过滤**。所以「schedules.yaml 里有一行周末全量校对」在修前是一句配置愿望，不是一条会在周日 02:00 跑的东西；`evidence-traceability` 之类的门禁看文件看不到这一点。
2. **不传 `policy=` 让两条治理判据变成同义反复**：新实例的 `alerted` 永远从空开始，于是「不重复告警」只在单次调用内可测，「突增」没有基线可比。
3. **顺带实测到一个落库面的缺陷**（不据此翻 `AC-9|06`）：面 4 末段量出两条腿的 ods 键拼写与合并键**都不同形**（ths `('thscode','trade_date')` vs 契约 `('symbol','trade_date')`；akshare `('股票代码','日期')` 同理），而 step-4 的 `run_hook` 把 `context.affected_keys`（源拼写）原样交给 `extra_diff_keys` ⇒ 那个「额外打标集」在真机上永远匹配不上，只有合并自己算出的不一致键被标记。本轮修了它（`_contract_keys`），判据 `AC-9|06` 的「`_diff_flag` 打标正确」还要求真机 dwd 对账，仍不翻。

## 一、先量：六个面，跑在活库上

`cross_check_census.py`（只读：两库 SELECT/`information_schema`，19 条语句全部 `SELECT`，脚本自带非只读语句闸门；`command=set -a; . ./.env; set +a; python docs/evidence/C48/cross_check_census.py`）完整读数见 `cross_check-census-live10.txt`（`CENSUS_EXIT=0`，抬头带 run_at / branch / HEAD / worktree / python / command）。六个面：

1. **调度面**：`TemplateKind` 四个成员、`EXECUTABLE_KINDS` 只有两个、四条调度行的 payload 键与 cron（见 §〇 表）。
2. **生产调用面**：AST 扫 `opendata/` + `scripts/`（`tests/` 单列）里 `AlertPolicy()` / `CrossCheckService(...)` / `Notifier` / `run_alert_matrix` / `diff_alert_message` / `publish_diff_alert` 的调用点。生产 3 处，`scripts/` 0 处，测试 39 处。
3. **策略状态面**：默认 `whitelist=set() alerted=set() last_rate=None`；同一差异两个新实例都告警、同一实例第二个不告警；突增给 `critical`；白名单命中/未覆盖两种读法。
4. **映射与权威面对账**：6 个域有 mapping、**两侧都有 mapping 的域只有 1 个**（`stock_daily`）；`authority.json` 声明权威序的 15 个域逐个数可对照侧，14 个 ⇒「校对 fail closed」（`futures_daily/index_daily/option_daily/stock_action/index_constituent` 只有 ths 侧，其余连一侧都没有）。
5. **真库读数**：`opendata_data` 12 张表；`dq_diff_report` 100 行、1 个批次（`dual-source-2026h1`）、`xcheck:` 前缀 0 组、field × verdict 五格各 20 行、`biz_key` 样例 `000001|2026-01-05`；`dwd_stock_daily` 719,316 行，`_diff_flag` 普查 `{1: 615610, 0: 103706}`，按 source 拆到 ths 615,610/102,094 与 akshare 0/1,612，`_as_of` 2026-07-21 .. 2026-09-24；ods 两条腿 `ths 10,310,289 行 / 2016-09-23..2026-09-24`、`akshare 617,371 行 / 2026-01-05..2026-07-21`。
6. **打标对账**（本轮新增的归因面）：取 akshare 腿最满的一天 2026-07-03（ths 5,512 行、akshare 5,839 行、同键 5,512），**用生产 reader `ods_frame_reader` 读**（即 akshare `成交量` 声明的 `scale: 100` 已应用），再调合并自己的 `_index/_value_columns/_disagreeing_keys` 重算：判为不一致 **5,377** 键，dwd 该日存 `_diff_flag=1` **5,377** 行，**交集 5,377** ⇒ 86% 的打标率不是打标逻辑坏了，是输入真的不一致。逐字段判差与比值分布：

   | 字段 | 同键可比 | 数值判差 | ths/ak（归一后）比值 top3 |
   |------|------|------|------|
   | open | 5,512 | 22 | `{1.0: 5490, 0.998: 4, 1.002: 3}` |
   | high | 5,512 | 1 | `{1.0: 5511, 0.95: 1}` |
   | low | 5,512 | 8 | `{1.0: 5504, 1.001: 2, 1.003: 2}` |
   | close | 5,512 | 0 | — |
   | volume | 5,512 | 792 | `{1.0: 4862, 0.01: 608, 0.999: 24}`，偏差幅度中位 0.9900 |
   | amount | 5,512 | 5,188 | `{1.0: 324}`（只有 324 个可比值），零值 5,515 / 5,839 行 |

   归因（写进治理文件，别写成"单位口径差异"）：**`scale: 100` 是对的**（4,862 键归一后正好 1.00、close 判差 0），仍有 608 键的原始 `成交量` 本来就是股数（⇒ 单位混在 akshare 这一条腿**内部**）、24 键 0.999 是手/股取整粒度、其余 160 键散布在近单位比值；`amount` 的 5,515 个零值是**缺数据**（324 个非零值与 ths 逐分一致）。两者都登记为 C49 的数据缺陷修复，**不进白名单**——白名单是拿来容忍口径差异的，拿来掩盖"退路的成交额是 0"就把唯一的信号掐了。

读数遍次披露（不粉饰）：`cross_check-census-attempts.txt` 是第 1、2 遍（本脚本自身缺陷：`main` 仍是 `async def` 未 await ⇒ 零读数；`Item.tick` 写错应为 `ticked`）；`cross_check-census-false-reading.txt` 是第 3 遍的**假读数**（集合推导 `for (r,) in rows` 之后又取 `r[0]`，把表名首字母当成表名 ⇒ 报「warehouse 只有 4 张表、`dq_diff_report` 不存在」，与真机 SHOW FULL TABLES 直接复核 contradictory）；`cross_check-census-live4-sql-gap.txt` 是第 4 遍（一条 SQL 串接漏空格 `GROUP BY _diff_flagORDER BY` 直接报错）；第 5 遍首次跑通全部六面；**第 6-10 遍逐遍取代**（6：修 §0 台账查询 + 补 `dq_diff_report` 形状/biz_key 样例 + dwd source×flag + key 拼写；7：加第 6 面差异归因抽样；8：第 6 面改成打标对账；9：补偏差幅度 + 比值分布；10：补两条腿 amount/volume 的零值/空值计数）。归档的是第 10 遍，前五遍（live5-9）是同一脚本的严格子集，未归档但原因逐条在此。

## 二、改了什么（逐条对着判据）

| 面 | 改动 | 挣到哪一格 |
|------|------|------|
| 生产触发器 | `jobs.py`：`EXECUTABLE_KINDS` 收 `FULL_CHECK`，新增 `_execute_full_check`（payload 只允许 `domain` + `window: full`，窗口用 `cross_check_window` 现算，批次 id 走 `batch_id_for`，结果带 `compared_keys/deviations/missing/diff_rate/verdict/per_field/deliveries`） | \|02（"被校对作业捕获"里的作业现在存在，且和流水线 step-3 同一个批次键空间） |
| 窗口从哪来 | `templates.py`：`ods_leg_span`（有界 `MIN/MAX`，`probe_days=400`，空腿 `ValueError`）+ `cross_check_window`（两腿**交集**，再从最新共有日往回截 `max_days=31`，不重叠即 `ValueError`） | \|02（不这么做的后果是实测可见的：两条腿分别到 2026-09-24 和 2026-07-21，按日历开窗会把整段缺口报成差异） |
| 生产装配点 | `templates.build_cross_check(...)`：源对默认取 `authority_baseline()` 前两条、mapping/reader 各自成对（reader 用 **raw** 的 `ods_raw_reader`，归一是 `CrossCheckService` 自己的事）、`write_report=DiffReportWriter(engine).write`、`notifier=default_notifier()`、`policy=shared_policy()`；不足两条腿 ⇒ `LookupError` | \|02 + \|04 + \|05（一个"建出来就会把决策丢掉"的构造点被收掉：`build_stock_daily_pipeline` 现在也走这个工厂） |
| 治理状态 | 新增 `diff_alerts.py::load_governance` + `diff_governance.json`：`known_differences` 每条必须带 `reason`，文件坏了 `RuntimeError`（fail closed），文件不存在 = 空状态（合法）；`shared_policy()` 进程内单例 + `refresh=` 重读白名单 | \|04（白名单有了来源与"生效"的可证伪面） |
| 状态生命周期 | `AlertPolicy` 由进程持有：`CrossCheckService` 不再各自新建；`Notifier` 契约改成 `(summary, decision)`（`alerts.py`），`_notify` **总是**把决策交给 notifier（含被抑制的那次） | \|04（"上一轮报过"这一轮才安静；突增有了基线） |
| 双通道投递 | `DiffAlertDispatcher.notify(summary, decision)`：决策不放行 ⇒ 一个通道都不碰；放行 ⇒ `ws_manager.broadcast` + `SubscriptionHub.publish_diff_alert` + `smtp_mail_sender`（复用 A1 的 `_smtp_send`，不另开邮箱路径），每通道结果记进 `Delivery`（含 `ws_error`/`hub_error`/`mail_errors`/`mail_skipped`） | \|05 |
| 投递可见 | `default_notifier()` 懒装配（建流水线不 import API 层，真要告警时才装配）并逐条 log；`_execute_full_check` 把 `deliveries` 放进作业结果 | \|05（"没有订阅者在听"与"通道抛了异常"是两种读数，不再都读成"已告警"） |
| 载荷 | `subscription.diff_alert_message(..., level=, reason=)`：WS 事件带上治理判定 | \|04 + \|05 |
| 落库键拼写 | `dwd_merge.run_hook` 先 `_contract_keys(context)`（用随行的 mappings 把源拼写换回契约拼写；没有 mapping 可换算时**原样透传**而不是猜） | 本轮不据此翻格（\|06 还要真机对账） |

## 三、判定面：单元侧每个断言都能被反证

- `tests/test_diff_alerts.py`（36 条，新增）：治理文件缺省=空状态 / 五类坏文件各自 `RuntimeError`（`malformed`、根不是对象、`known_differences` 不是列表、条目缺 `reason`、`email_recipients` 不是字符串列表）/ 写出的状态能 round-trip 且 `reason_for` 命中 / **shipped 文件本身是一等公民**（每条白名单都得有 reason）；`shared_policy()` 同一实例、白名单来自文件、**第二次同差异被抑制**、`refresh` 认新白名单、`last_rate` 跨比对（1% → 4% 给 `critical`）、`reset_policy()` 后重新告警；被抑制的决策 **三个 sink 一次都没被调用**（`channels.touched is False`）且 `ws_error/mail_skipped` 都是 `None`（什么都没跳过，因为什么都不该发）；未装配的通道读成 `"no WS broadcast channel wired"` / `"SMTP not configured (no sender)"` / `"no diff-alert recipients configured"`；通道抛异常 ⇒ 记 `RuntimeError: client gone`、其余通道继续、`alerted` 仍为真；部分邮件失败 ⇒ `mail_sent=1` 且 `mail_errors=("bad@example.com: ConnectionError",)`；hub 没有 `publish_diff_alert` ⇒ 报出来；`render_diff_email` 的 subject/10 行正文/样本 ≤5 行/缺行样本渲染；`smtp_mail_sender()` 对 `smtp_host`/`smtp_user` 两道闸 + 真的把 `MIMEMultipart` 交给 A1 的 `_smtp_send`；`default_notifier()` 首用才装配、二用复用；`production_dispatcher()` 真的接上 `ws_manager` 与 `subscription.hub`。
- `tests/test_pipeline_templates.py`（+11 条）：`ods_leg_span` 的 SQL 逐字符（`SELECT MIN(\`日期\`), MAX(\`日期\`) … WHERE \`日期\` >= :floor`）与 `floor` 绑定、text 回读成 date、空腿 `ValueError`；`cross_check_window` 交集优先、超宽按 `max_days` 从最新共有日回截、不重叠与空腿两条 fail-closed；`build_cross_check` 默认源对来自权威 baseline、`trading_calendar` ⇒ `LookupError`、**默认 notifier 非空且 `policy is shared_policy()`**、注入优先、`build_stock_daily_pipeline(second_source=…)` 的 step-3 就是这个工厂（单源不给 `cross_check`）。
- `tests/test_pipeline_jobs.py`（+5 条 `TestFullCheckExecutor` 等）：比对 + 落报告 + 告警一条不漏（`per_field == {"close": 1}`、`batch_id == "xcheck:stock_daily:…"`、`deliveries` 整字典相等）、两次运行不重复告警、两腿一致时 WS 一条都不发、非 `full` 的 window token 直接拒、无可对照源对的域被拒（`trading_calendar` / `index_daily`）。
- `tests/test_cross_check_service.py`：白名单命中时**notifier 仍然收到**这次判定（否则"生效"只是"没投递"的同义反复）。
- `tests/test_dwd_merge.py`（+2 条）：源拼写的 affected key 经 `_contract_keys` 后**真的落进**合并键空间并被打上 `_diff_flag=1`（两条腿值相同 ⇒ 除额外打标集没有别的东西能解释这个 1），断言 `"600888.SH" not in flags`；无 mapping 时原样透传。
- `tests/test_data_subscribe.py`：`diff_alert_message` 的 level/reason 上行，以及调用方不传时 `reason is None`（不猜一个默认理由）。

## 四、门禁与档案

**回填前一遍（pre-backfill）**：`gate-run1-prebackfill.txt` —— 完整未裁剪原始输出 7,357 行，抬头 33 行跑前 provenance（run_at / branch / HEAD / python / node / `git status --porcelain` 全文），末行 `GATE_EXIT=0` 是同一条后台命令里读自 `make` 的真实退出状态、非推导。读数：16 个门禁成员 + 收尾 `PASSED` ⇒ 文件内 **17 段 `===== gate:`**；`3222 passed, 6 skipped in 80.04s`；总覆盖率 `TOTAL 11618 1098 2866 291 89.14%`；前端单元 14 files / 109 tests，Playwright `18 passed`。零依赖普查面在这一遍之前刚按规矩重冻过一次（见 §六 披露）。

**判定面读数（回填前，活树）**：`probe-all-prebackfill.txt` —— `python scripts/quality/acceptance_item_probe.py --all` 全 302 行原样归档，27 个条目级探针对着 `docs/quality/acceptance-item-ledger.json` 重算，收尾 `census: gap=8, proven=19` / `PROBE_EXIT=0`（退出码只在「台账说 proven 而探针说 gap」时非 0）。本轮三格 `AC-9|02 / |04 / |05` 全部读 `proven`，逐面读数见该文件 L270-L298（探针打印的 `document line` 是回填前的行号）。

**反事实自检**：`probe-self-test.txt` —— `python scripts/quality/acceptance_item_probe.py --self-test`，`OK: 27 probe(s) measured; every judge is reachable from its declared repair, and all 208 counterfact(s) flip a clean reading back to a gap.` / `SELF_TEST_EXIT=0`。本轮新增的 36 条 `Break` 都在其中：每条都得把一份「已补齐到可判」的读数打回 `gap`，打不红即该命令失败。判据原文、阈值、`expects` 子串一个字没动。

**回填后一遍（post-backfill）**：`gate-run2-postbackfill.txt` —— 完整未裁剪原始输出 **7,364 行** = 42 行跑前 provenance 抬头 + 7,322 行 `make gate` 原始输出；拼接前后逐字节核对（`2200 + 698701 = 700901`），行数、段数与 `GATE_EXIT` 是在**仓库内的副本**上重新数过的，不信 `/tmp` 往返。读数：17 段 `===== gate:`（16 成员 + 收尾 `PASSED`），末行 `GATE_EXIT=0` 由同一条后台命令读自 `make` 的真实退出状态、非推导；`3222 passed, 6 skipped in 75.61s`；`TOTAL 11618 1098 2866 291 89.14%`；`OK: items=130 proven=29 gap=8 unreviewed=93 ticked=29 across 22 group(s) and 19 §10 row(s) reconcile.`（本轮三个 proven 第一次被门禁自己读到并放行）；`OK: A2 files meet the full A2 standard (ruff + format + mypy + bandit).`；`OK: quality debt did not increase.`；前端 `Test Files 14 passed (14)`、Playwright `18 passed (7.0s)`。

两遍之间**生产码与测试码逐字未变**（抬头里的 `git status --porcelain` 面即证据：`opendata/pipeline/*` 与 `tests/*` 的 M/A 集合与第一遍同形），变的是判定工具 `scripts/quality/acceptance_item_probe.py`、`docs/evidence/C48/` 的档案与本 README、验收文档 §0/§2/§10 与台账 —— 这正是第二遍要证明的那件事。两遍的覆盖率读到**完全同一串** `11618 1098 2866 291 89.14%` 不是巧合，而是一条可证伪的旁证：`--cov` 只测 `opendata` 与 `opendata_fuyao`，判定工具落在 `scripts/quality/` ⇒ 探针怎么改都不该动这个数；动了就说明有生产码被顺手改了。墙钟从 80.04s 到 75.61s 是唯一该变的量。

台账行的 `evidence` 只引用回填前就已归档并 stage 的档案：本轮第二遍的日志不能反过来为它自己刚做的翻正背书。

**最终树一遍（final-tree）**：`gate-run3-finaltree.txt` —— 这一段是**跑之前**写的，且跑完不回填。原因是一条定点：任何 gate 日志描述的都是它运行时的那棵树，而把它的数字抄进本 README 就已经改了树 ⇒ 只要「登记数字」这一步存在，就没有任何一遍能描述包含它自己的最终树。处置是把最后一遍的数字留在文件内自证（末行 `GATE_EXIT=`、`grep -c '^===== gate:'` = 17、`TOTAL` 行、pytest 收尾行），本段只声明它的用途与与前一遍的差别：**差别只有档案与本 README 这些 markdown**，生产码、测试码、判定工具、验收文档、台账自第二遍起逐字未动；抬头里逐条列出跑前 `git status --porcelain`，并说明为何要再跑一遍（第二遍之后新增了 `gate-run2-postbackfill.txt` 且本节落了字）。跑完后只补 `git add` 与本节存在性的披露，不改写数字。

**翻正后的台账对账**：`ledger-check-postbackfill.txt` —— `make ledger-check` 的完整读数 `OK: items=130 proven=29 gap=8 unreviewed=93 ticked=29 across 22 group(s) and 19 §10 row(s) reconcile.`。这一遍先红过一次并**如实留档**：§10 的 AC-9 状态列第一版写成 `完成 条目级 3/9（\|02/\|04/\|05 逐条达标，其余六格未翻）`，`acceptance_ledger_check` 回 `FAIL: AC-9 (line 376): status claims completion with 6 item(s) unproven and carries no 未逐条达标 marker` —— 一个「组里还有未达标条目却把 3/9 写成达标口吻」的措辞闸。处置是**把措辞改回门禁要求的形状**（`完成 条目级 3/9（未逐条达标：…已达标，其余六格未翻）`），而不是放宽这条规则或把 6 格说成达标；紧接着 `evidence-traceability` 又红一次（`untracked: docs/evidence/C48/ledger-check-postbackfill.txt`，即档案没 stage），补 `git add` 后绿。另一次同类漂移是本轮 §0 修订行插入后，§2 的三条条目行整体下移一位 ⇒ 本 README 与 v5.20 行里原先写的 `L173/L175/L176` 变成 `L174/L176/L177`，两处措辞都已就地标注「档案里的探针读数记的是插入前的行号」。

**附带发现（判定面被本轮自己的生产改动读瞎，见 §六 与 `face-regression-readings.txt`）**：`face_regression_probe.py` 是可原样复跑的复现脚本。

## 五、本轮没有做的事 / 须用户决定的面

1. **真机 `dq_diff_report` 写入没有发生**。往生产数仓写是须用户确认的动作，所以 `AC-9|02` 挣的是「作业存在 + 注入样本被捕获并逐列落库（服务/单元面）+ 真库表形状与 key 落点实测」这三件；周日 02:00 真跑一遍、把 `xcheck:` 批次读回来，仍是一条待办（也说明：修前那句「step-3 在真机上从未写过这张表」至今未被推翻）。
2. **每日增量 payload 仍不带 `second_source`**（面 1 实测 payload 键 `['domain','source','window']`）⇒ 真机上 step-3 只由周报触发。要不要让每条腿每天都双源，是运营决定（成本：第二条腿每天全市场一遍），登记不擅自翻。
3. **`PARTITION_MAINTENANCE` 仍然没有执行器**：它的 apply 半边是对生产分区做 `REORGANIZE`（DDL），按同一条确认规则留给用户点头；本轮只把 `FULL_CHECK` 收口。
4. **告警治理状态只在进程内**：重启后 `alerted`/`last_rate` 归零，「不重复告警」跨重启不成立。要做成控制库一张表（不是本轮发明的面），登记。
5. **`AC-9|01` 的「覆盖 P0 域」还差得远**：面 4 量出 15 个权威域里 14 个两侧 mapping 不齐 ⇒ 校对在这些域 fail closed。这是"铺 provider"的活（task #5），不是告警面的活。
6. **判定阈值里两处已知粗糙**（记录，不动）：`dwd_merge._differs` 用写死的 `1e-4` 而 mapping 的 `tolerances` 只有 OHLC 四项（`volume/amount` 没有 tolerance，零值与单位混合因此只能靠比值分布人肉归因）；`cross_check._deviation` 是相对偏差，双侧皆 0 时判一致。
7. **白名单为空、`email_recipients` 为空**是测量结论不是"忘了填"：§一-6 的两个缺陷归 C49 修数据；没有声明过告警邮箱，所以投递记录诚实写 `mail_sent=0` + 原因。

## 六、本轮 diff 面

生产：`opendata/pipeline/diff_alerts.py`（新）、`diff_governance.json`（新）、`templates.py`、`jobs.py`、`cross_check_service.py`、`alerts.py`、`dwd_merge.py`、`subscription.py`。测试：`tests/test_diff_alerts.py`（新）+ `test_pipeline_templates.py`、`test_pipeline_jobs.py`、`test_cross_check_service.py`、`test_dwd_merge.py`、`test_data_subscribe.py`。判定工具：`scripts/quality/acceptance_item_probe.py`（+473/−1：三条 AC-9 探针与 36 条反事实，两个新助手 `method_body` / `set_literal_members`）。档案：本 README + `cross_check_census.py` + 四份 census 读数（live10 / attempts / false-reading / live4-sql-gap）+ 探针档案三份（`probe-all-prebackfill.txt` / `probe-self-test.txt` / `face-regression-readings.txt`）+ `face_regression_probe.py` + `ledger-check-postbackfill.txt` + 三遍 gate 完整日志（`gate-run1-prebackfill.txt` / `gate-run2-postbackfill.txt` / `gate-run3-finaltree.txt`）。

**判定面被本轮自己的生产改动读瞎了（披露，附复现）**：写第一版 AC-9 探针时三格里有两格读 `gap`，查下去不是产品缺口，是**判据工具在按行形读代码**，三处同因：

1. `EXECUTABLE_KINDS` 用正则 `EXECUTABLE_KINDS\s*=\s*frozenset\(\{[^}]*<MEMBER>` 匹配，即假设 `frozenset({` 同行。本轮把 `TemplateKind.FULL_CHECK` 加进这个集合，字面量超过 100 列被 formatter 折成三行（`jobs.py:68-70`），两条依赖此面的判据同时读 `no` —— 包括**台账里早已 `proven` 的 `AC-13|07`**。按改前的台账状态重算就是 drift：一次生产改动把一条已转正条目的判定面读成了「没有执行器」。
2. `AlertPolicy.decide` 用 `function_body(alerts, "decide")` 读，而它只走模块级定义 ⇒ 返回空串，白名单 / 去重集 / 差异率基线 / 突升级别四个 flag 全读 `no`。
3. `CrossCheckService._notify` 同上 ⇒ 「被抑制的决定照样交给 notifier 记录」读 `no`。

处置不是把判据改宽：把 kind 面改成 AST 直读集合成员（`set_literal_members`，与折行无关），把两个方法体改成 `method_body` 按类取。改后 `--all` 里 `AC-9|02/|04/|05` 读 `proven` 且 `AC-13|07` 仍 `proven`、`PROBE_EXIT=0`；`--self-test` 208 条反事实全部打回 `gap`。顺带把全部 9 个仍在用 `function_body` 的名字逐个验过是真模块级函数（`_execute_freshness` / `_execute_full_check` / `_execute_template` / `build_cross_check` / `domain_freshness` / `normalize_frame` / `production_dispatcher` / `shared_policy` / `smtp_mail_sender`），没有第二处同类盲区。读数与逐字保留的原正则见 `face-regression-readings.txt`，脚本 `face_regression_probe.py` 可原样复跑。这条同时登记为门禁盲区：探针**不是** `make gate` 的成员，所以面读瞎时门禁全绿 —— 收进 C49。


**零依赖普查面重冻（披露）**：本轮在 `opendata/pipeline/` 下新增了一个生产模块 `diff_alerts.py`，`zero-dep-check` 除了比对上游引用，还冻结「被扫文件数」，于是先红了一次：`FAIL: the scan surface moved: - archived file census no longer describes the tree: opendata archived 192 but walks 193 file(s)`。处置是按规定走 `python scripts/codemod/verify_no_akshare.py --update`，diff 只有一行（`docs/quality/zero-dep-baseline.json`：`"opendata": 192` → `"opendata": 193`，总 517 file(s)），没有排除该文件、没有放宽任何规则、没有偷偷抬 scanner 版本。重冻后的读数：`OK: 517 file(s) walked (opendata=193, opendata_client=2, opendata_fuyao=9, opendata_http=313), python 3.13, scanner 2; no new upstream references (frozen baseline: 3)`。
