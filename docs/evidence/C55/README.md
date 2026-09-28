# C55 —— AC-11 的入口级（REST）判定面：五条格子、一处生产缺陷、判据点名的 EXPLAIN 补齐、一次工具自己欠的债

本轮把验收文档 §2 的 **AC-11 前五格**（`|01`…`|05`）从「台账里没有判定面」做成
**每条都有实测读数、每条都能被反事实打回 gap** 的条目级判据：由此改掉一处产品缺陷（§2），
并把一格判据**点名的那种**验证方式真的执行了一次（§5：对跑着的 MySQL 发只读 `EXPLAIN`）。
AC-11 的另外七格（`|06`…`|12`：catalog/freshness、WS 订阅、断线补发、full 模式、既有路由回归、
`opendata_client`、旧机制迁移收口）不在本轮，登记见 §9。

## 1. 五条格子的读数（命令与逐字输出见 `probe-ac11-items.txt`）

| 格子 | 判据点名的面 | 实测 | 判定 |
|------|--------------|------|------|
| `AC-11\|01` | 路由/入参/默认值/响应 schema/OpenAPI | 6/6 节点绿；9 个入参缺失 = `-`；`layer=dwd`、`adjust=none` | **proven** |
| `AC-11\|02` | 服务端合成 qfq/hfq **且**与 akshare 官方 qfq 对照 | 11/11 节点绿；合成面 yes；但唯一的官方对照腿读数是 **sina**（留档 run 也记 sina），判据写的是 akshare | **gap** |
| `AC-11\|03` | `fields` 白名单 400 / `layer`+`source`+`adjust` 枚举 / `symbols` 绑定 / CSV 公式转义 | 13/13 节点绿；四面各一个独立读数 | **proven**（本轮补了 `source` 那面，见 §2） |
| `AC-11\|04` | 默认时间窗不触发全分区扫描，**EXPLAIN 验证** | 5/5 节点绿；默认窗 365 天、`LIMIT` 上限 yes；`EXPLAIN` 真发到 MySQL：6 张已落地且带分区的 dwd 表**每张 4 个分区只读到 1～2 个**，全分区扫描 0 张（见 §5） | **proven** |
| `AC-11\|05` | `diff-report` 可查差异报告 | 5/5 节点绿；读的是校对器写的同一张 `dq_diff_report`；scope/batch/limit 三面齐 | **proven** |

五格这一遍是 `census: gap=1, proven=4`。全表 41 个探针一起重算的最终一遍是
`census: gap=11, proven=30`（`probe-all.txt`，本轮共跑了四遍，逐段差异都记在那份档案里）。

`|02` 判红是本轮的结论而不是本轮的进度损失：它的对照腿与判据点名的不是同一家。
**判据原文一字未改**，也没有把「EXPLAIN 验证」读宽成「窗口裁剪正确所以隐含分区裁剪」，
更没有把「akshare 官方 qfq」换成手里现成的 sina 腿。闭合路径写在探针的 `repair` 与台账 `reason` 里。
## 2. 一处生产缺陷：`source` 的枚举门此前不存在（`source-leg-enum-fix.txt`）

判据 `AC-11|03` 原文列了三个枚举面，`layer`/`adjust` 是 `Literal[...]`，而 `source` 是裸
`str = Query("auto")`，全库没有任何一处回答「这条域到底有没有这条腿」。修前的具体形状：

- `layer=dwd&source=eastmoney` → SQL 走合并表、不含 source 谓词 → **200**，并且把调用方要的那条
  不存在的腿名**回显在载荷里**（言行不一致，而且是最容易骗过人的那种）。

处置是改产品：`_validated_query` 里按 `get_registry().capabilities()` 取该域注册过的腿集
（加 `auto`），不在集合内 → `400` 并把可选腿名一起交回调用方。新增入口级用例
`tests/test_data_query_http_warehouse.py::TestParameterSafety::test_a_source_that_is_not_a_registered_leg_is_a_400`
三条状态码各钉一个面：bogus `source` 400、注册腿 `ths` 200、`layer=ods&source=ths` 404
（枚举门在前，ods 表门在后）。爆炸半径复核：仓库里没有任何调用方往这条端点发 `source`
（前端只发 `page`/`page_size`，ops 仪器用的是注册表关键字参数与别的端点）。

`tests/test_data_query_http_warehouse.py` 是本轮新建的模块（23 例，`pytestmark = integration`，
SQLite 真引擎替身 ⇒ 在 `-m "not e2e"` 的门禁面里跑）；它补的是 `test_data_query_api.py`
（不需要仓库）与 `@pytest.mark.e2e`（需要 MySQL 专有 DDL）之间那段两边都没碰的入口级面。

## 3. 判定工具自己欠的债：`bandit_selfdev` 被本轮抬到 4（`bandit-selfdev-regression.txt`）

`--self-test` 第一遍红的不是 AC-11 的格子，而是两格早已 proven 的 **AC-17|03／AC-17|05**
「没有任何读数能过判定」。往下查是一串真因果：本轮在 AC-11|02 的 `repair` 里写了一个事实键
`run_pass_rows` ⇒ bandit 的 B105 按**名字**里的 `_pass_` 命中 ⇒ `bandit_selfdev 3 → 4` ⇒
棘轮退出 1 ⇒ 那两格把自身可达性挂在棘轮退出码上 ⇒ 自检报「判不到」。

三条省事的路都没走：抬快照上限（正是 `AC-17|03` 判据要防的形状）、加 `# nosec B105`
（读数降了而仓库里那个名字没动，与抬上限同构）、把 `scripts/` 从被测根摘掉（被测根消失是另一种放水）。
走的是**改键名** `run_pass_rows → run_ok_rows`（6 处一起改，展示给读者的文案仍写 `PASS 16 行`），
复验 `bandit_selfdev: 3 (snapshot 3)`、`RATCHET_EXIT=0`、ruff/format 干净。

附带修掉两处本轮自己写的读数缺陷（都属于「判定面读瞎而门禁全绿」那一类）：
`bind_symbols` 原来在找字面量 `":symbol_"`，而 `build_data_select` 是先 `f"symbol_{index}"`
再 `f":{name}"` ⇒ 永不相等、`AC-11|03` 被读成 gap；换成两条按代码形状的读数（绑定字典赋值 +
`":{name}"` 占位）。另一处是 `|04` 的读数文案少了一个 `f` 前缀，把 `{facts['explain_faces']}`
原样打印了出来 —— 文案里出现字面花括号就是探针在说谎，已改并复跑。

## 4. 反事实与面基线

- 五条探针共 **41 条反事实**（`|01` 7／`|02` 7／`|03` 9／`|04` **10**／`|05` 8），全表
  `--self-test` 复跑 **41/41 个探针走到判定、325 条 break 全被施加且每条都把干净读数打回 gap**
  （`self-test.txt`，三遍 278（红）→ 320 → 325，real 397.17 s）。
- 325 条可以逐项对出来：HEAD（`4c0633e`）的 36 个探针有 **283** 条，本轮 AC-11 五格 **+41**，
  AC-16\|07 的「留档正文 `ARCHIVE_ROUND` 必须等于所在目录」**+1**。两版模块各按 item 数
  `len(p.breaks)` 再逐 item diff，只有这 6 行不同 —— 其余 30 个探针的反事实数一条没动。
- 面基线 `docs/quality/acceptance-probe-faces.json` 由 `--sync-faces` 重写：**41 个探针、12 个在读 moment 面、proven 而无探针 7 个**；本轮对它的 diff 只有 15 行插入（五条新 AC-11 探针各自的 `moment_faces: []`，即它们只读文件内容不读 git 时刻面），没有一行删除，也没有把任何历史格子的时刻面改空。
  棘轮只降不升，本轮没有新增「proven 而无探针」的格子。

## 5. `AC-11\|04`：判据点名的验证方式这一轮真的执行了（`window-pruning-explain.txt`）

判据写的是「不带日期区间的查询不会触发全分区扫描（**EXPLAIN 验证**）」。窗口那一半本来就是绿的实测，
本轮补的是另一半：新仪器 `scripts/ops/window_partition_explain.py`（runner `run_window_explain.sh`）。

- 20 个注册域逐个交给 `build_data_select` 现拼 SQL —— **与 REST 端点同一条语句、同样只有绑定参数** ——
  再对跑着的 MySQL 发 `EXPLAIN` 读 `partitions` 列。没落地的 14 个域显式记 `landed=no` 且不进分母，
  也不拿它们凑数。
- 实测：6 张已落地且带分区的 dwd 表每张 `partitions_total=4`（p2024/p2025/p2026/pmax），计划读到
  1～2 个（`stock_daily` = `p2025,p2026`、`type=index`、`key=PRIMARY`、rows 估计 200；
  `stock_adjust` 只读到 `p2026`）；`landed_partitioned_tables=6 pruned_yes=6 pruned_no=0`，
  进程判据是「全部落地表都裁剪才退出 0」。
- 只读保证：这台仪器按构造只发 `EXPLAIN` 与 `information_schema` 查询，没有任何写入或 DDL。

**两处必须如实登记的仪器形状**：

1. 判据点名的语句在库里的实体是 `"EXPLAIN " + sql` 这种**拼接**，不是 `EXPLAIN SELECT` 字面量
   （SQL 必须来自 `build_data_select` 才谈得上「这个端点的查询」）。所以探针的代理正则
   `EXPLAIN_STATEMENT` 本轮从「只认整句字面量」扩到「也认拼接前缀」—— 扩的是**怎么算执行过
   EXPLAIN** 的识别面，不是判据；两种形状都在读数行里点名，命中数与文件名一起交出
   （第二遍正是这条代理把 `|04` 读成 gap：411 个代码文件里命中 0 处，因为当时库里还没有仪器；仪器落地后同一读数变成「412 个文件、命中 1 处」）。
2. 留档按**查询层摘要**钉住：正文 `query_module_sha` 必须等于当下 `opendata/pipeline/query.py`
   的 sha256 前 12 位（本次 `41eee85c2bae`），`ARCHIVE_ROUND` 必须等于留档所在目录。
   改了拼 SQL 的代码、或把旧留档拷进新目录，`|04` 当场读回 gap（两条都是本轮新加的反事实）。

仪器自己的一次返工：第一版把读数写成 `dict[str, object]`，`",".join(reading["partitions_used"])`
被 mypy 判 `arg-type` ⇒ `make a2-check` 由绿转红 ⇒ 探针 `AC-17|03`（「修改过的文件按 A2 标准达标」）
在普查第 3 遍里从 proven 掉回 gap。**没有去放宽 `AC-17|03`，而是把读数改成 `TypedDict`**，
`a2-check` 复绿后重跑仪器（档案是修好之后那版代码跑出来的）再重算全表。这条因果留在
`probe-all.txt` 的第 3／4 遍对账里。

## 6. 门禁第 3 遍：红在三条用例，三条都是本轮自己造的（`gate-run3-red-guard.txt`）

第 11 个成员 `acceptance-probe-check` 在 staged 树上跑通之后，第 12 个成员 `test-cov` 用全量测试
把这轮真正的形状读了出来：`3 failed, 3362 passed, 6 skipped`（`GATE_EXIT=2`）。三个失败分成两处因果：

- **新的入口级模块进了选择器，reviewed 清单没跟上**（两条红）。`tests/test_data_query_http_warehouse.py`
  带 `pytestmark = integration`，于是 `-m "integration and not e2e"` 的集合多了一个成员，而
  `P0_INTEGRATION_MODULES` 那份「加一条是评审动作」的清单里没有它 ——
  `test_the_marked_set_is_the_reviewed_set` 与 `test_the_judge_and_the_guard_module_count_the_same_surface`
  当场把「同一面被两台仪器量出两种结果」读出来。处置是**评审后加进清单**（干净环境留档已经点名它 48 次），
  不走摘掉标记那条路（那是把 23 条用例从 `AC-16|07` 的分母里拿掉），也不把相等比较改成 `<=`。
- **留档定位改名，守卫用例还按旧常量读**（一条红）。本轮把留档定位从固定路径 `CLEAN_RUN_EVIDENCE`
  换成按轮次扫描的 `newest_round_archive(CLEAN_RUN_BASENAME)`（`|04` 与 `AC-16|07` 共用这把尺子），
  守卫用例 `test_the_clean_environment_archive_the_judge_reads_is_shipped` 因此 `AttributeError`。
  处置是让钉的对象跟着判据走：钉「判据实际读到的那一份」自己声明的 `ARCHIVE_ROUND=`，
  其余四条钉子（`CLEAN_SECTION`/`CLEAN_RUN_EXIT=0`/两个上游包 absent/选择器 ≥3 次）一条不少；
  `docs/evidence/C51/clean-env-integration-run.txt` 一字未动。没有把 helper 退回固定路径，
  也没有在用例里 `getattr(..., None)` 兜底 —— 那两件事都能让这一遍变绿而把判据读瞎。

这一遍值得留在档案里的不是那三个红，而是**为什么门禁会红而 `--gate-check` 全绿**：条目级探针读的是
选择器此刻的集合，守卫用例读的是「谁声明了这个集合」，两者不一致时红的是后者。这正是 C50/C51 把
两台仪器对上才肯翻判定的那个理由。

## 7. 门禁第 4 遍：全绿，绿的读数是「同一条分母的两台仪器现在一致」（`gate-run4-green.txt`）

修完 §6 的两处之后重跑整条链，`GATE_EXIT=0`，`real 663.93`，17 个成员全部跑到（上一遍红在第 12 个，
第 13–17 个前端成员因此从未被执行）。两遍的测试行并排读：

- 第 3 遍（红）：`3 failed, 3362 passed, 6 skipped`
- 第 4 遍（绿）：`3365 passed, 6 skipped`

`3362 + 3 = 3365`、skipped 仍是 6 —— 全量的分母一个字节没动，被修好的是那三条本轮自己写坏的判定，
没有靠 deselect、`skip` 或缩小集合将红换成绿。覆盖率读数为
`Required test coverage of 84% reached. Total coverage: 90.07%`（与第 3 遍逐字相同：两处修复只碰了
测试与清单，产品代码面没有新增未覆盖行）。

第 11 个成员这一遍的读数（逐字见 `gate-run4-green.txt` 的 §1）：`41 个探针，一遍 measure`、墙钟
`385.8 s`、41/41 读到事实、反事实 `325 条 break 各被施加一次`、
`agrees=40, unflipped=0, open=0, deferred=1, stale-proof=0`、面基线 `12 个在读 moment 面；
proven 而无探针 7 个，基线 7 个（只降不升）`。AC-11 五格翻成 `|01|03|04|05 proven` +
`|02 gap`（官方对照腿仍取 sina，记在 §9）。

档案里 §0 只声明了六项数字取自正文（成员清单、两条时钟、耗时、退出码、汇总、覆盖率），
分支/HEAD、staged 计数与 `git status` 行数是组装时跑的 `git`，正文里没有 —— 把它们标出来而不是
混进「全部来自正文」，是因为这份档案要说的那句话是「跑这一遍的树 = 现在这个树」，而这一遍的树里
**还没有它自己**（未跟踪行正是本档案）。所以本轮按两遍收口：这一遍绿之后把档案 `git add`，
再跑第 5 遍，让「含全部档案的树」也拿到一次绿（见 §10 档案表）。

第 5 遍（`gate-run5-green-final.txt`）跑的正是含第 4 遍档案的那棵树：`GATE_EXIT=0`，
`real 687.11`，17 个成员全跑到，`3365 passed, 6 skipped`，`Total coverage: 90.07%`，
`make: ***` 行数 0 —— 三处读数与第 4 遍逐字相同（只有墙钟 663.93 → 687.11 是机器负载），
这一句才是「新增的那份日志没有改变任何判定」的证据。两遍之后剩下的自我指涉只有一处：
第 5 遍的档案写于第 5 遍之后，它自己不可能被第 5 遍覆盖；这一处用第 6 项处理 ——
`evidence-traceability` 成员（唯一读 `docs/evidence/` 的那台仪器）在**含这两份文档改动的最终树**上
单跑一遍，读数记在下一段，不冒充整条门禁。

## 8. 最终树上单跑的 `evidence-traceability`

第 5 遍之后的树上的差只有两份文档（`README.md` 与 `gate-run5-green-final.txt`），而全链 17 个成员里
唯一读 `docs/evidence/` 内容的是 `evidence-traceability`，所以收口方式是在**含这两处改动的最终树**上
把它单独跑一遍，其余六个只读文档之外内容的静态成员一并复跑（读的是代码，本轮没有再动代码）：

- `evidence-traceability`：`rounds = 65`、`files census = 604 (gate logs: 109)`、
  `dates in gate logs = header 103/109, printed by the run 6/109`、
  `narrative/date/identity/command/exit/untracked gaps` 六面全 `0`，
  `OK: evidence archive matches the frozen baseline (0 legacy entr(ies)).`
- `brand-check` / `zero-dep-check` / `secret-check` / `ledger-check` / `js-points-check` /
  `loguru-check`：各一遍通过 —— 扫描器自证 `8 violations detected, 4 compliant samples clean`；
  密钥面 `scanned ~83847839 bytes (83.85 MB)`、`no leaks found`；
  台账 `items=130 proven=39 gap=9 unreviewed=82 ticked=39 across 22 group(s) and 19 §10 row(s)`；
  JS 执行点 `engine 76, eval 35, exec 0` 未变。
- 没重跑的十个成员（`a2-check`、`quality-ratchet`、`public-api-quality`、`acceptance-probe-check`、
  `test-cov` 与五个前端成员）读的是代码与前端，本轮那两处改动没有触碰它们；把这两件事分开写，
  是为了不让「六台仪器绿」被读成「整条链在最终树上绿」—— 后者只有第 5 遍那一次，而它不含本档案。

## 9. 本轮没做的事（不静默、不借上一轮的证据）

- `AC-11|06…|12` 七格仍 `unreviewed`。已经量过的形状：WS 的「query token 不被承认」这一面
  **没有任何用例拨过** `?token=`（`data_subscribe.py` 从不读 `ws.query_params`，读它的是旧的
  `websocket.py`）；`opendata_client` 的 `SubscriptionSession.__enter__`（`websockets.sync.client.connect`
  那条懒 import 缝）只有被 `-m "not e2e"` deselect 的 e2e 用例走过；full 模式的字节上限
  （`MAX_FRAME_BYTES`/`MAX_FULL_BYTES`）从未是触发条件。这些留作 C56 的开发区加判定面。
- `AC-11|02` 的官方对照腿仍是 sina，akshare 官方序列那一面没有补上 ⇒ 本轮不翻正。
- 对数仓只发了只读 `EXPLAIN` 与 `information_schema` 查询；没有任何数仓写入或 DDL；
  AC-15 旧表仍只读未 DROP；未读取或打印任何密钥值或 `.env` 内容；
  探针与取证跑一律 `-m "not e2e" --no-cov`；`docs/evidence/B4/qfq-official-check.txt` 是只读引用，
  历史轮次的档案一件未动。

## 10. 档案表

| 文件 | 是什么 |
|------|--------|
| `probe-ac11-items.txt` | 五条 AC-11 格子的逐字读数（三遍：改名后 37 条反事实那一遍 → EXPLAIN 面补齐后两遍） |
| `probe-all.txt` | 全表 41 个探针的普查四遍 + 逐段比对（第 3 遍量出 `\|04` 翻正与 `AC-17\|03` 因仪器 mypy 返红，第 4 遍复原） |
| `self-test.txt` | 反事实自检三遍（41/41、278 → 320 → 325 条全咬）与差额逐项归因 |
| `a2-check-after-typefix.txt` | 仪器 mypy 返工前后各一遍 `make a2-check` 的完整输出（修后四面全 ok；这一格是 `AC-17\|03` 复原的直接证据，也是普查第 3 遍那次红的归因） |
| `window-pruning-explain.txt` | 真仓库只读 `EXPLAIN` 留档：20 个域、6 张落地分区表逐表计划、`query_module_sha` 摘要 |
| `run_window_explain.sh` | 上面那份留档的来源头（round/head/命令/双钟时刻）与「只发只读语句」的说明 |
| `source-leg-enum-fix.txt` | `source` 枚举门的生产 diff、新用例源码、pytest 读数、爆炸半径复核 |
| `bandit-selfdev-regression.txt` | `bandit_selfdev 3→4→3` 的现象、归因、处置与单点复现 |
| `clean-env-build.txt` / `clean-env-integration-run.txt` / `run_p0_integration_surface.sh` | AC-16\|07 棘轮要求的「只装声明依赖的干净 venv」重建与 177 例集成跑（两个解释器各一遍） |
| `gate-check-final.txt` | 门禁成员 `acceptance-item-probe` 对最终工作树的完整一遍（41/41 走到判定、325 条反事实全咬、`stale-proof=0`、AC-1\|10 记 deferred） |
| `gate-run1-red-traceability.txt` | 门禁第 1 遍（红在第 5 个成员 `evidence-traceability`：本轮档案目录还没有 narrative） |
| `gate-run3-red-guard.txt` | 门禁第 3 遍（红在第 12 个成员 `test-cov`：`3 failed`，两条红来自新增 `integration` 模块与 reviewed 清单不一致、一条红来自留档定位改名后守卫用例没跟上；含 FAILURES 段逐字、修复 `git diff` 与两个守卫模块整文件复跑读数，见 §6） |
| `gate-run4-green.txt` | 门禁第 4 遍（`GATE_EXIT=0`，17 个成员全跑到，`3365 passed, 6 skipped`，覆盖率 90.07%；未裁剪整份日志 + §0 出处头，见 §7） |
| `gate-run5-green-final.txt` | 门禁第 5 遍（含第 4 遍档案的那棵树：`GATE_EXIT=0`，`real 687.11`，`3365 passed, 6 skipped`，覆盖率 90.07%，`make: ***` 0 条 —— 与第 4 遍逐字相同的那三处读数就是「多出来的日志没有改变判定」的证据，见 §7） |
| `gate-run2-red-ac16-staleproof.txt` | 门禁第 2 遍（红在 `acceptance-item-probe`：新增 `integration` 模块 ⇒ `archive_blind=1` ⇒ `AC-16\|07` 留档过期，按设计咬人） |
