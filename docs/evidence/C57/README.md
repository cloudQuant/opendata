# C57 —— `partition-maintenance` 那条 cron 行从未有人执行：把执行器补上，然后把真仓库的年度上界面量出来

台账格子：`AC-8|05`（分区：大表按 `RANGE COLUMNS(trade_date)` 年分区 + MAXVALUE 兜底；**分区维护任务**可运行）
与 `AC-8|06`（跨年写入用例：模拟新年度数据写入成功，无 "no partition for value" 错误）。
两格本轮都判 **gap**，判据一字未放宽；gap 的理由点名两件**等用户确认**的事（见 §6）。

## 1. 入口：一条长得像在工作、实际被跳过的声明

`schedules.yaml` 里有 `partition-maintenance` 这一行，`TemplateKind.PARTITION_MAINTENANCE` 也存在，
但 `opendata/pipeline/jobs.py` 的 `_execute_template` 只认三种 kind，其余 kind 在注册阶段被
`EXECUTABLE_KINDS` 挡掉 —— 于是这行 cron 的全部作用是**让人以为分区在维护**。C48 把新鲜度那格
（`freshness`）接进了告警矩阵，分区这半没人接。这不是「等证据」，是缺一个执行器。

## 2. 改了什么〔源码〕

- `opendata/pipeline/jobs.py`
  - `EXECUTABLE_KINDS` 收进 `TemplateKind.PARTITION_MAINTENANCE`；
  - `_execute_template` 多一条派发分支 → `_execute_partition_maintenance`；
  - 新增 `maintain_partition_horizon(engine, *, current_year, years_ahead, tables=None, domains=None)`
    与 `PartitionRun`：**apply 半真的调 `PartitionMaintainer.ensure`**（不是只出 plan），
    然后**逐表重读** `plan_yearly_partitions` 得到 `remaining`。这条设计的目的是让
    「ALTER 发出去了」不能被读成「上界补齐了」——补齐与否由重读决定，不由发送决定。
  - `_execute_partition_maintenance` 的入参就是判据要的那两个：`date.today().year` 与
    模板 payload 的 `years_ahead`（缺省 2），返回 `run.as_dict()` 并落 info 日志。
- `opendata/pipeline/partitions.py`：`ensure()` 的 `partition_key` 参数是只出报告面留下的死参数
  （没人传、也没人读），删掉，签名改成 `ensure(table, *, current_year, years_ahead=2)`。
- `tests/test_pipeline_jobs.py`：新增 `TestPartitionMaintenanceJob`（6 例）——kind 可执行、派发到本体、
  本体调 `ensure` 而非只出 plan、`remaining` 由重读决定、没有执行器的 kind 被跳过而不是半接线；
  两条 shipped-id 断言把 `pipeline_partition-maintenance` 加进去（freshness 挪到 `recorded[3]`，
  分区那条是 `hour='3'`）。
- 读数：`python -m pytest tests/test_pipeline_jobs.py tests/test_partition_maintenance.py`
  → **66 passed / 3 deselected**（3 条 deselected 全是 `@pytest.mark.e2e`，见 §6）。

## 3. 真仓库只读面〔档案 `partition-horizon.txt`，`PARTITION_FACE_EXIT=0`〕

`FACTS tables=20 partitioned=6 gap_tables=5 gap_partitions=5 current_year=2026 years_ahead=2
pmax_rows=0 dispatch_wired=yes`

| 表 | 分区列（`SHOW CREATE TABLE` 原样） | 最高年度上界 | 缺 |
| --- | --- | --- | --- |
| `dwd_financial_indicator` | `RANGE COLUMNS(report_period)` | 2027-01-01 | 1（`p2027`） |
| `dwd_financial_statement` | `RANGE COLUMNS(report_period)` | 2027-01-01 | 1（`p2027`） |
| `dwd_index_constituent` | `RANGE COLUMNS(as_of)` | 2027-01-01 | 1（`p2027`） |
| `dwd_stock_action` | `RANGE COLUMNS(ex_date)` | 2027-01-01 | 1（`p2027`） |
| `dwd_stock_adjust` | `RANGE COLUMNS(trade_date)` | 2029-01-01 | 0 |
| `dwd_stock_daily` | `RANGE COLUMNS(trade_date)` | 2027-01-01 | 1（`p2027`） |

两条读数值得单独说一句：

1. **分区列不全是 `trade_date`**〔档案〕。判据原文写「按 `RANGE COLUMNS(trade_date)` 年分区」，
   仓库实际是**按各自的业务日期列**分区（财务报表用 `report_period`、成分股用 `as_of`、
   除权除息用 `ex_date`）。按域列分区比按 `trade_date` 更对（财务数据的年界就是报告期年界），
   但这条差异以前没人读过，因为根本没有分区面的读数。本轮把它量出来，不改判据措辞（改措辞是产品决定）。
2. **`pmax` 行数为 0**〔档案〕。6 张表的 `pmax` 现在都是空的，所以补 `p2027` 的
   `REORGANIZE PARTITION pmax INTO (p2027, pmax)` 只动元数据、不重写数据行 —— 这是「这次 DDL 贵不贵」
   的代价面，本轮特意先量再问用户。

## 4. 为什么「写成功」不能顶替「分区在工作」〔推断，基于 §3 的档案读数〕

缺的那一年（2027）现在会落进 `pmax`：**不报错**。也就是说 AC-8|06 想要的「无 no partition for
value 错误」这一条，用「写入不抛异常」来验永远为真，而年分区事实上已经不起作用了。所以这格的判据
要的是**落点断言**（`assert placements == ["p2027"]`，`tests/test_partition_maintenance.py:147`），
外加「真仓库那一年不缺」。两者缺一格就仍是 gap。

## 5. 两格判据与反事实〔档案 `probe-items-ac8.txt`、`probe-self-test.txt`〕

- `AC-8|05`：9 条 break（yaml 行删掉 / kind 掉出可执行集 / 派发分支摘掉 / 本体退成只出 plan /
  DDL 不再渲染 MAXVALUE / 仓库一张分区表都没有 / 年度上界仍落后 / 档案没记分区列 / 没有经确认的
  apply 留档）。本轮读数：接线四读 + DDL 渲染 + 分区列点名 = yes，
  `live_gap_tables=5`、`applied_face=no` ⇒ **gap**，补齐面点名 `applied_face: no -> yes`、
  `live_gap_tables: 5 -> 0`。
- `AC-8|06`：6 条 break（不再自建 probe 表 / 新年度那行没插 / 落点分区名没断言 / e2e 标记被摘 /
  真仓库那年仍缺 / 没有一次真跑的留档）。本轮读数：前三条 yes（落点断言 1 行），
  `fallback_gap=5`、`ran_live=no` ⇒ **gap**。
- 两件都靠 `--self-test` 证「好了会绿」：本轮全量读数是 **43/43 个探针走到了判定，345 条 break
  各被施加一次且都把干净读数打回 gap，每个 judge 都能从自己声明的 repair 到达**
  （`PROBE_SELF_TEST_EXIT=0`）。其中 `asserts_placement` 在 repair 里写成字面 `1` 而不是
  `*asserts_placement`，因为「有人把断言删了」应该让这一格读成 gap，而不是让仪器自爆不可达。

## 6. 本轮没做的事（两件，都要用户点头，不是我能自行裁定的）

1. **没有 apply**。`REORGANIZE` 是对生产仓库的 DDL。§3 量出代价（`pmax` 空）之后仍然等确认。
   注意副作用：这条 job 现在**真的接上了**，`dev` 启动时 `attach_builtin_jobs()` 会注册
   `pipeline_partition-maintenance`（cron `0 3 * * *`，按本机时区），下一次触发就会自动补这 5 张表。
   要人工执行一次：`bash docs/evidence/C57/run_partition_apply.sh`（本轮**未创建、未执行**，
   留档面 `partition-apply.txt` 因此不存在，也就是 `AC-8|05` 的 `applied_face=no` 读数的来源）。
2. **没有跑 e2e 跨年用例**。它会在工作库里 `CREATE/DROP TABLE _probe_partition_maintenance`，
   也是仓库写操作，同样等确认。`make gate` 不跑 e2e，所以 `ran_live` 只能靠单独留档挣。
3. `AC-8` 另外六格（|01/|02/|03/|04/|07/|08）本轮**没做**，仍是 `unreviewed`：ods 命名与三元组元数据列、
   独立 alembic 仓库环境与「启动不建表」、key 级幂等、写入基准（含内存）、无 `id` 列表的分页、
   真机双源落 ods。它们各自要的料不同（其中 |04 的内存面和 |08 的真机双源落库都要跑一次真写入）。

## 7. 仪器自身的一个缺陷（本轮抓到并修）

`asserted_lines(text, token)` 数的是 `expect(` 行（给 pytest-style 的 AC-11 那批格子用的），
我把它用在 `assert` 风格的 `tests/test_partition_maintenance.py` 上，读数恒为 0 ——
也就是**假 gap**：落点断言其实就在 `:147`。已在 `measure_ac8_06` 换成本文件风格的行数
（`line.lstrip().startswith("assert ")` 且点名 token），没有改共享 helper 的语义（那会牵动别的格子）。
教训与 C40 记的那条同向：读数说「证据不足」时，先怀疑仪器。

## 8. 复算命令

```bash
bash docs/evidence/C57/run_partition_horizon.sh        # 只读面，可反复跑
bash docs/evidence/C57/run_probe_c57.sh items          # AC-8|05 / |06 单格复算
bash docs/evidence/C57/run_probe_c57.sh self-test      # 43 格全量反事实自测
PYTHONPATH=. python -m pytest tests/test_pipeline_jobs.py tests/test_partition_maintenance.py \
  -q --no-cov -m "not e2e"                              # 66 passed / 3 deselected
```

## 9. 档案表

| 文件 | 内容 | 证据档 |
| --- | --- | --- |
| `partition-horizon.txt` | 真仓库分区面上界 + 分区列 + pmax 行数（只读） | 〔档案〕 |
| `run_partition_horizon.sh` | 上面那份读数的生成脚本 | 〔档案〕 |
| `probe-items-ac8.txt` | AC-8\|05 与 AC-8\|06 的单格复算，含两行 VERDICT | 〔档案〕 |
| `run_probe_c57.sh` | 探针复算/自测的 wrapper（`items` / `self-test` 两面） | 〔档案〕 |
| `probe-self-test.txt` | 43 格全量反事实自测 | 〔档案〕 |
| `README.md` | 本文件：源码面为〔源码〕、代价与口径推理为〔推断〕 | — |
