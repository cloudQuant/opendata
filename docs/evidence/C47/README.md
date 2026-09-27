# C47 — AC-13|07「告警矩阵生效：pipeline 失败 / 连续失败 / 分区缺失 / 磁盘水位」：四类里三类从来没有生产者

本轮把 AC-13 八格里判据原文自己点名四类的 `|07` 从「规则存在」做成「四类各有生产者、各有判定行、各被调度执行器真喂输入」，`条目级 0/8 → 1/8`。

## 〇、修前的真实形状（读自 `git show HEAD:…`，HEAD=`29f2b3a`）

C46 轮刚建出 `opendata/pipeline/alert_matrix.py`（本轮唯一被大改的生产模块）。它的 `run_alert_matrix` 在修前是这样调判定引擎的：

```python
alerts = evaluate_alerts(
    settings=settings or AlertSettings(),
    freshness=reports,
    failures=(),          # ← 写死的空
    partition_plans={},   # ← 写死的空
    disk=None,            # ← 写死的空
)
```

| 判据原文点名的类 | 修前已有的面 | 修前没有的面 |
|------|------|------|
| `pipeline 失败` | `freshness.py::_failure_alerts()` 判定函数、`PipelineFailure` 数据类、`Alert(rule="pipeline_failure")` 的字符串 | **`opendata/` 全树没有任何文件产生过一条 `PipelineFailure`**：`collect_failures` 不存在，调用点传的是字面量 `()` |
| `连续失败` | 同上（`_failure_alerts` 里按 `consecutive_failure_critical` 升级） | 同上，且**没有任何东西数过「连续」**——`pipeline_progress` 的分片行从未被按 run 聚合过 |
| `分区缺失` | `partitions.py::PartitionMaintainer`（能读 `information_schema.PARTITIONS`、能出 `reorganize_sql`）、`_partition_alerts()`、以及现成的汇合函数 `freshness.py::partition_plans_for` | 矩阵侧**没有生产者**：`collect_partitions` 不存在，调用点传的是 `{}`；而 `partition_plans_for` 在 HEAD 的 `opendata/` 全树**一个生产调用方都没有**（`git grep partition_plans_for HEAD` 只命中定义 `freshness.py:306` 与测试 `tests/test_freshness.py:301,316`，`PartitionMaintainer` 只多一个自己的测试 `tests/test_partition_maintenance.py`，`scripts/` 下零命中）⇒ 它 docstring 里那句 "so the freshness task and the alert matrix share one definition of partition missing" 是一句**没有生产读者的话** |
| `磁盘水位` | `_disk_alerts()`、`DiskUsage` 数据类 | `collect_disk` 不存在，调用点传的是 `None`；`disk_usage` 这九个字符在 HEAD 的 `opendata/` 里**一次都不出现**（`git grep -n "shutil\." HEAD -- opendata` 只有 `retention.py:312` 的 `shutil.rmtree`）⇒ 判定函数能吃 `DiskUsage`，但全仓没有任何代码去量一个卷 |

三点结构性事实（不是措辞问题，是可达性问题）：

1. **签名里就没有入口**。修前 `run_alert_matrix(engine, *, expected, settings, domains, broadcast)`——三个参数都不存在，所以调度执行器**无从**把它们喂进去；`_execute_freshness` 当时只传 `engine/expected/domains/broadcast` 四个。
2. **写死的空参让「零告警」看起来像「健康」**。判定函数是纯函数、单测里四类都跑过（C38b 补的覆盖），生产调用点却永远交空集合 ⇒ 矩阵在真机上**只可能**产出 `freshness` 一类。这就是「规则存在」与「矩阵生效」的差别。
3. **`consecutive_failures` 与 `pipeline_failure` 是同一份读数的两个阈值**，所以它们共用一个生产者；但判据把它们算两格要求，探针也照两格要求（见 §三）。

## 一、先量：四类各自能不能读到东西，是设计选择的前提

`matrix_census.py`（只读：两库 SELECT/聚合 + `information_schema` + `shutil.disk_usage`；脚本自带**非只读语句闸门**，任何首个关键字不在 `{SELECT, SHOW, SET, BEGIN, COMMIT, ROLLBACK, PRAGMA}` 的语句直接 `exit 1`）跑在活库上，完整读数见 `matrix-census-live.txt`（带跑前 provenance 抬头）。五条读数：

1. **失败面该问哪张表**——`pipeline_progress` 有 1 行 / 1 个 run / 1 条腿 / 1 个窗口，分片状态普查 `{'DONE': 1}`、`FAILED=0`；`task_executions` 有 5 行、状态普查 `{'COMPLETED': 5}`。**两张表都拿不到失败样本**，所以「选 `pipeline_progress`」不是从「哪张表历史上红过」推出来的，而是从**键形**推出来的：`pipeline_progress` 按 `(domain, source, window_end)` 成键（正是「一条腿的连续失败」需要的粒度），`task_executions` 按 `task_id/script_id` 成键（答的是"某个任务跑没跑"，答不出"某条腿第几个窗口"）。读数：`collect_failures(engine, run_limit=1000)` 在活 schema 上 `failures=0 legs_with_history=1`。
2. **分区面**：范围内 53 张数仓表，`partitioned_tables=9`、`tables_with_gaps=8`，缺口**全部是 `p2027 < 2028-01-01`**（`dwd_financial_indicator`、`dwd_financial_statement`、`dwd_index_constituent`、`dwd_stock_action`、`dwd_stock_daily`、`ods_stock_action_ths`、`ods_stock_daily_akshare`、`ods_stock_daily_ths`）。⇒ 这一类**真机立刻可证**，不是只能造夹具。
3. **磁盘面**：`settings.data_dir` 配置的是**相对路径** `PosixPath('data')`，实测落到卷 `'.'`，`used=766204305408 / total=994662584320` ⇒ `ratio=0.7703`（落在 0.8 告警线以下，所以这一遍**没有** `disk_water` 告警——是读数，不是缺面）。
4. **同一条调度运行的生产物**（`expected` 走日历、`broadcast=None`）：`scope {'domains': 20, 'source_legs': 7, 'unmapped_legs': 26, 'failure_legs': 1, 'partitioned_tables': 9, 'disk_path': '.'}`、`reports=27 alerts=34`、**`alerts by rule: {'freshness': 26, 'partition_missing': 8}`**、`delivered=0`。
   - `delivered=0` **不是投递失败**：测量脚本刻意给 `broadcast=None`（只读测量不该去 page 人）。投递面由 AC-18|01 与本轮的单元面负责。
   - `source_legs=7 / unmapped_legs=26` 与 C46 轮独立普查同数 ⇒ 两轮问的是同一份注册表真值。
5. **只读性**：数仓引擎 170 条语句全 `SELECT`，控制库引擎 9 条（`SELECT` 8 + `SHOW` 1，来自 `information_schema`），`non-readonly statements issued = 0`，末行 `OK: all four faces measured; no DDL, no writes`。

这一遍踩到一次**读数是假的**：第一次跑出来 `source_legs=0 / unmapped_legs=0`——因为脚本没先 `register_providers()`，注册表是空的，53 张表里只剩 20 张 dwd 参与分区面。修的是脚本（现在 `main()` 里显式注册并写了为什么），然后**重跑**取 §一 那组数。一次没注册的普查会同时把两类读数读歪，这正是"先量"要在"改代码"前面的原因。

## 二、四类各补的生产者，以及「测不到」为什么不能被写成「健康」

- **`collect_failures(engine, run_limit=FAILURE_RUN_LIMIT)`** —— 回答 `pipeline 失败` 与 `连续失败`。**按 run 在 SQL 侧聚合**（`GROUP BY pipeline_id, domain, source, window_end` + `sum(case(status=FAILED))` + `min(case(...error))`），一个分组就是一个 (域, 源, 窗口) 的 run（`pipeline_id` 本身就是这三者的确定性拼合，见 `opendata/models/pipeline.py` 模块 docstring："the deterministic `pipeline_id` (domain, source and window)"，所以多带那三列只是让 `select` 在 `ONLY_FULL_GROUP_BY` 下合法，不会把一个 run 拆成两组）；**任一分片失败即整个 run 失败**（丢了一只票的那一窗没交付，无论其余多少只成功）。连续数是**run 的 streak 而不是分片计数**，上限 `FAILURE_RUN_WINDOW=10`（从没工作过的腿报"整个窗口都在失败"，不报无穷）。
  - **本轮自己否掉的第一版**：第一版按 `row_limit` 读分片行，再给边界窗口打 `partial` 标记——理由是"半个窗口只能确认失败、不能确认健康"。量过之后这是**缺陷而不是保守**：一条腿若**唯一**被读到的 run 恰好被切断，真失败会被丢掉（漏报）；而把所有边界 run 一律标记为不可判，又会凭空造出一条告警（误报）。改成 SQL 侧按 run 聚合后，`LIMIT` 落在 run 之间，**结构上不可能有半个窗口**，`_shard_failed()` 与 `partial` 一并删掉。
  - `ERROR` 文本进告警体前截到 200 字符；失败但没记 error 的腿仍要报（`"no error recorded"`），**不能因为缺文本就把这一类吃掉**。
- **`warehouse_tables()` + `collect_partitions(engine, current_year, years_ahead=2, domains)`** —— 回答 `分区缺失`：先用 `PartitionMaintainer.is_partitioned` 把范围收成**已分区表**（不给未分区表造分区告警，那是迁移不是运维），再用 `partition_plans_for` 出缺口年——那个函数从本轮起**有了生产调用方**（§〇 表格里那格登记的正是它修前没有）。**只报不改**：`ensure`/`apply` 一步都不做——判据要的是"分区缺失要有人报警"，而不是"读告警的这个动作顺手改了生产表结构"。离线面用 `before_cursor_execute` 语句间谍钉住「零条 ALTER/REORGANIZE」（`test_a_missing_year_alerts_without_the_matrix_touching_ddl`），并把 `PartitionMaintainer.ensure` 打桩成 `raise AssertionError` 来证明真的没去修（`test_the_partition_face_never_asks_for_a_repair`）；活库面由 §一 5 的 170 条 SELECT 独立互证。
- **`collect_disk(path)`** —— 回答 `磁盘水位`：`shutil.disk_usage`，先走到**最近的已存在祖先**（配置里的目录还没建，不该等于"这一类不测"），并把**实际量的那个卷**回报成 `disk_path`；无路径 ⇒ `(None, None)`，即"不声称任何事"。
- **`MatrixScope` 三个新字段 `failure_legs: int | None` / `partitioned_tables: int` / `disk_path: str | None`** —— 这是本轮最重要的一条设计约束，而不是装饰：
  - `failure_legs` 没有控制库时是 **`None`** 而不是 `0`。`0` 的读法是"每条腿上一轮都成功了"，`None` 的读法是"这一类根本没测"。**一把答不了题的笔不能写出一个 0**。
  - `legs_with_history`（第二个返回值）把"checkpoint 表是空的"与"跑批是健康的"分开——两者在告警面上长得一模一样。
  - 分区面没给年份 ⇒ `({}, 0)`，且 `partitioned_tables=0` 会照实进 scope；磁盘面 ⇒ `disk_path=None`。
- **`alert_message(alert, expected)`** —— 每种 rule 有自己的帧：`data.<rule>_alert` 统一信封（`rule`/`severity`/`subject`/`detail`/`created_at`），`pipeline_failure`/`consecutive_failures` 把 `domain`/`source` 从 subject 拆开，分区/磁盘帧**故意没有 `domain` 与 `expected`**（一个卷没有域），freshness 继续用它自己的富帧。理由是 C46 立下的：subject 在新鲜度帧里是 `domain:source`，在磁盘帧里什么也不是——**同一个字符串在不同类上不是一个 claim**。
- **`jobs.py`**：新增 `control_engine()`（`settings.database_url_sync` + `NullPool`，`lru_cache(1)`），`_execute_freshness` 把 `control_engine=control_engine()` / `current_year=date.today().year` / `years_ahead=payload.get("years_ahead", 2)` / `disk_path=settings.data_dir` 四个真输入喂进矩阵，并把 `run.as_dict()`（含 scope）打进日志——调度日志里因此能看出这一轮**测了哪些面**。

## 三、判定面：一类一个节点，接线面独立成组

单元面 `tests/test_alert_matrix.py` 15 → **37 例**（+22），`tests/test_pipeline_jobs.py` 46 → **48 例**（+2）。新增五个类：`TestFailureSignal`（8 例：一只分片失败即整 run 失败／streak 数 run 不数分片／恢复打断 streak 而 `legs` 仍为 1／`FAILURE_RUN_WINDOW` 上限／`run_limit=2` 与全量读的差／error 截到 200／失败无 error 仍报／空表 `= ((), 0)`）、`TestDiskAndPartitionSignals`（7 例：水位报出自己量的卷名／`collect_disk(None) == (None, None)`／95% 出 `data.disk_water_alert` 且体内含 `95.0%`／p2027 缺口**零条 ALTER**／`ensure` 打桩即炸／未分区不冒充完备／地平线够着则安静且 `partitioned == 1`）、`TestMatrixScopeHonesty`（2 例：没喂的输入读成 `None`/0 而不是健康／给了控制库才多出 `pipeline_failure`+`consecutive_failures` 两类）、`TestAlertFrames`（4 例：每类一个可路由 `type`／pipeline 帧拆 `domain`/`source`／分区与磁盘帧**没有** `domain`/`expected`／freshness 保留自己的富帧）、`TestAllFourKindsAtOnce`（1 例：一次 run 五类全出，`delivered == len(alerts) == len(frames)`，scope 带 `partitioned_tables=1/failure_legs=1/disk_path`）。

**覆盖率读数**（`--cov=opendata.pipeline --cov-report=term-missing`，两文件 85 passed）：`opendata/pipeline/alert_matrix.py` **96.76%**（166 stmts / miss 5，br 50 / part 2），`opendata/pipeline/jobs.py` **97.30%**。剩余行是 `if TYPE_CHECKING:` 导入块与 `collect_disk` 里"走到文件系统根仍无目录"的防御分支（POSIX 上 `/` 必存在，该分支实际不可达）——**不为凑数写空断言**，也不动 `exclude_lines`。

探针 `AC-13|07`（`scripts/quality/acceptance_item_probe.py`，本轮新增，**+260 行**）七个面：

| 面 | 读数 | 为什么这一面要单独成一条 |
|------|------|------|
| 四类节点 8/8 | 一类一个 producer→判定→出帧 的节点，加一条「四类同跑」 | 少一类就少一个节点，而不是少一段文档 |
| 接线节点 4/4 | 未喂输入读成 `None`、给了控制库才亮失败行、执行器喂三类、年份默认值 | 与四类面**分开**才能各自变红 |
| 判据原文四类 | `kinds_named = 4/4` | 判据措辞是前提：文档不再点名某类，探针要响亮地拒绝而不是悄悄少测一类 |
| 判定引擎产出的 rule | `consecutive_failures, disk_water, freshness, partition_missing, pipeline_failure` ⇒ `4/4` | AST 取 `Alert(...)` 第一个位置参数，读的是**引擎能产出什么**，不是文档写了什么 |
| 生产者 | `4/4`（四类→三个函数，失败两类同源） | `opendata/` 全树 grep 得到才算存在；写死空参的那一版这里会读成缺 |
| 执行器喂入 | `inputs_fed = 3 / inputs_declared = 3` | **本轮判定的核心**：只声明不传参 ⇒ 那一类永远读成零告警。读的是调用点关键字，不是签名 |
| 范围诚实字段 | `scope_fields = 3/3` | 测不到要记进范围，空读数不能伪装成健康 |

**反事实 10 条**全部把 clean 读数打回 gap；全表 `--self-test` 读数 **24 条探针 / 172 条反事实 `exit=0`**（C46 是 23/162），见 `self-test.txt`。单条读数 `probe-ac13-07.txt`（当时 `state=unreviewed`、文档 line 218 未勾 ⇒ `VERDICT: proven` 是**输入**，不是勾）。

翻转之后再跑一遍同一条命令（`probe-ac13-07-postbackfill.txt`，`PROBE_EXIT=0` 捕获自 `$?`）：七个读数与修前那一遍**逐字相同**，变的只有两处——探针自己打印的「台账现状」跟上了（`state=proven`、`文档勾选=True`），以及 `document line 218 → 219`（§0 插入 v5.19 那一行把整张表往下推一行；条目原文一字未动，动了 `item_key` 的摘要就会把这格孤立成"发现"）。这一遍证明的是"勾与判定面对得上"，不是"多挣了一格"。

## 四、门禁与档案

四遍，读数都**读自各自档案文件内部**（不是凭记忆转述），抬头里的时间戳是跑前写进去的：

| 遍 | 档案 | 行数 / 段标记 | `GATE_EXIT` | 关键读数 |
|---|---|---|---|---|
| 1（回填前，失败） | `gate-run1-prebackfill-attempt.txt` | 93 / 5 | **2** | 前四个成员（brand / zero-dep / secret / ledger）在这一遍已是绿的且一字未改地留档；红在 `evidence-traceability` 的 `narrative` 面：`NEW VIOLATION: docs/evidence/C47/#narrative`（当时这个目录里只有读数，没有 `.md`） |
| 2（回填前） | `gate-run2-prebackfill.txt` | 7,239 / 17 | **0** | `3164 passed, 6 skipped in 73.93s`；`TOTAL 11373 1085 2812 289 89.02%`；`A2 files: 366`（ruff + format + mypy + bandit 四项 ok）；public-api `668/668` 双 100%；zero-dep `516 file(s) walked`；ledger census `items=130 proven=25 gap=8 unreviewed=97 ticked=25`（＝**回填前**那一刻的树）；traceability 六面 gaps 全 0（`files census = 484`、`gate logs: 81`）；前端 14 files / 109 tests + e2e 18 passed |
| 3（回填后） | `gate-run3-postbackfill.txt` | 7,243 / 17 | **0** | `3164 passed, 6 skipped in 76.77s`；`TOTAL 11373 1085 2812 289 89.02%`（与第 2 遍**逐字相同**）；`A2 files: 366`；public-api `668/668` 双 100%；ledger census **`items=130 proven=26 gap=8 unreviewed=96 ticked=26`**——这一遍与第 2 遍的唯一差就是本勾的翻转 ⇒ **翻转一格不改动任何判定面读数** |
| 4（最终树） | `gate-run4-final.txt` | 7,247 / 17 | 0（**由标记推导**，见下） | `3164 passed, 6 skipped in 72.56s`；`TOTAL 11373 1085 2812 289 89.02%`；`A2 files: 366`；public-api `668/668`；zero-dep `516 file(s) walked`；gitleaks `no leaks found`；ledger census `items=130 proven=26 gap=8 unreviewed=96 ticked=26`；traceability 六面 gaps 全 0（`files census = 486`、`gate logs: 83`）；前端 18 passed ⇒ 与第 2、3 遍的差只有"回填 + 三处文档改动"，判定面一字未动 |

第三遍之后量出 §0 新增那一行里有 5 个**没转义**的竖线（`int`／`str` 的可选类型写法与 `AC-13` 条号、key），会让该行从 3 列渲染成 5 列；修的是文档措辞层，但**文档是 `ledger-check`／`brand-check` 的输入** ⇒ 描述最终树的日志必须在修完之后重跑，所以有了第 4 遍。第 3 遍的档案保留、并在自己抬头里标明**已被第 4 遍取代**（不删、不改写）。

第 4 遍的 `GATE_EXIT` 是**推导**而不是捕获：那一遍用 `nohup … &` 分离启动，包装 shell 已退出 ⇒ `$?` 拿不到，我不凭印象写 0。可交叉的两条都在原始输出里：`make gate` 的 16 个成员各是一次 `$(MAKE)` 子调用，任一失败即中止 ⇒ 第 17 段 `===== gate: PASSED =====` 的存在即"全部成员绿"；且全文 `make…***…Error` 行数 = 0。流程教训就地记下：**每一遍都该用 `…; echo "GATE_EXIT=$?" >> 暂存件`**（第 1～3 遍都这么写），第 4 遍是本轮唯一一处降级，写在这里而不是抹掉。

对第一遍的处置是**补档案（本 README）而不是放宽 `narrative` 规则**——那一面要问的正是"一个轮次目录不能只留下一堆读数而没有人说清它读了什么"，它判得对。

覆盖率两文件读数（`coverage-two-files.txt`，240 行完整未裁剪）里 `PYTEST_EXIT=1` 是**预期的**：那次跑的是 `--cov=opendata.pipeline` 局部面，未覆盖到 `pipeline` 全模块的 90% 门槛，与门禁的全局面（`--cov-fail-under=84`，第 2 遍 `89.02%`）不是同一把尺；留下它是因为它带着 per-file 的两行读数。

## 五、本轮没有做的事 / 须用户决定的面

1. **磁盘水位的口径**（须决策）。现在量的是 `settings.data_dir`，而它配置的是**相对路径 `data`** ⇒ 实测落到进程 CWD 所在卷 `'.'`，`ratio=0.7703` 是**这台机器工作目录的卷**，不是数仓 MySQL 所在卷。生产者、帧、告警阈值都是真的，**唯一不确定的是"该量哪个卷"**：把 `DATA_DIR` 指到真实挂载点、还是新增一个显式的 `WAREHOUSE_DISK_PATH`？本轮**没有**替用户改配置语义（改了等于让一个键同时表达两件事），只把量到的卷名如实回报——那正是 `disk_path` 进 scope 的理由。
2. **8 张表的 `p2027` 缺口不修**（须决策）。分区面按判据只负责"缺了有人报"；补分区是 `ALTER TABLE … REORGANIZE PARTITION pmax INTO (…)`，属**生产 DDL**，未获明确确认不执行。读数与语句计数在 `matrix-census-live.txt`。
3. **告警量**（须评审）。同一条调度运行现在产 **34 条告警**（26 条 `freshness` + 8 条 `partition_missing`），而 26 条里绝大多数是**从未落过地**的表（`unmapped_legs=26` 的域与空 dwd 表）。四类"生效"之后，下一件事是给它分级/收敛（哪些域此刻该被 watch、`unmapped` 要不要单列而不是逐条 stale），否则真信号会被噪声吃掉。本轮**没有**擅自加"只报 P0"之类的过滤——代码里从来没有 P0 分层（C46 就是因为 `{domains: p0}` 这个不存在的集合把默认改成了 `all`）。
4. **交易日历仍是 `weekday-rule` 层**（继承 C12/C13 的登记）：`dwd_trading_calendar` 表还没落，本轮 census 里 `tier=weekday-rule`，`expected=2026-09-25`（周日跑批量周五）。落库属生产写入 + 建表，仍须确认。
5. **连续失败面今天只有"薄历史"**：`pipeline_progress` 全库 1 行、`FAILED=0`，所以真机上这一类**读不出 streak**（`legs_with_history=1` 正是为了把"表是空的"和"都健康"分开）。多窗口连续失败只在单元面用三个失败窗口钉住（`test_the_streak_counts_runs_not_shards`）。这不是本轮能靠改代码补的——它需要真实跑批历史。
6. `full_check` / `partition_maintenance` 两个 kind 的执行器仍缺（C46 登记）；本轮只做了 `freshness` 执行器的三条输入接线。

## 六、本轮 diff 面

`opendata/pipeline/alert_matrix.py` +390、`opendata/pipeline/jobs.py` +30、`scripts/quality/acceptance_item_probe.py` +260、`tests/test_alert_matrix.py` +511、`tests/test_pipeline_jobs.py` +55（合计 1213 insertions / 33 deletions）。**没有新增 `opendata/` 下的文件** ⇒ 零依赖普查（`opendata=192`）无需重冻。数仓与控制库本轮**只读**（SELECT/SHOW，见 §一 5），未执行任何生产写入或 DDL，AC-15 旧表仍只读未 DROP，未读取或打印任何密钥值或 `.env` 内容（census 脚本 `source .env` 只注入环境变量，抬头与正文都不打印连接串与密钥）。
