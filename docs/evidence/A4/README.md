# A4 数仓垂直切片 — 验收证据索引

> 迭代 1A / 里程碑 A4（对应验收文档 AC-8、AC-9、AC-13、AC-18（新鲜度）、AC-19（保留策略））
> 日期：2026-09-23
> 环境：macOS（Apple Silicon）| Python 3.13.5（conda env `py313`）| Node 20.20.2 | MySQL 9.4.0
> 复算方式：所有命令均可在仓库根直接执行；本地验证均在 Python 3.13 与 CI 孪生环境（Python 3.11 + 全新依赖集）双跑。
> 数据仓库为**独立 alembic 环境**（`alembic_data/`，版本表 `alembic_version_data`，库 `opendata_data`）。

## 1. A4 DoD 对照（实施计划）

| DoD 项 | 状态 | 证据 |
|--------|------|------|
| A 股日线双源落库 | **部分**：单源（akshare）链路真机贯通；双源待 A3 THS 凭证（见 §4） | A4.2 ods 写入真机、A4.6 dwd 真机合并、A4.9 真机查询；模板已按「有第二源才接校对」接线（A4.7） |
| 校对与 dwd 可查 | **完成** | A4.5 校对 + `dq_diff_report`（真机报表用例）、A4.6 dwd 合并、A4.9 REST 查询（`layer=ods|dwd`） |
| 新鲜度告警生效 | **完成** | A4.8 新鲜度 + 告警矩阵（真机滞后计算、缺表告警、分区缺失、磁盘水位、连续失败） |
| 写入基准记录在案 | **完成** | `write-benchmark.txt`（本目录）：20,000 行 0.64~0.67s ≈ 30k~31k 行/s |

## 2. 任务与交付物

| # | 任务 | 提交 | 结果 |
|---|------|------|------|
| A4.1 | 独立 alembic 环境 + ods/dwd 建表生成器（含分区三件套） | `bbe78b5` | `opendata/pipeline/ddl.py`（ods 源列+`_source/_fetched_at/_batch_id`；dwd 契约派生+`source/_merged_at/_diff_flag/_as_of`；年 RANGE COLUMNS + pmax + 主键含分区键）；12 DDL 测试 + 7 迁移测试（含真机重放） |
| A4.2 | `ods_writer` 通用化（两处缺陷修正 + staging/批量 upsert） | `63e3d6c` | 业务键 upsert 修正无主键/内容哈希缺陷；批 5 万行；未知列忽略、缺键/batch_id fail-closed；9 单测 + 5 真机用例 + 基准 |
| A4.3 | 分区维护任务 + `/tables` 无 id 列兼容 | `5be9974` | `partitions.py`（上界=`current_year+years_ahead`、REORGANIZE、未分区空操作）+ `table_page.py`（id→主键→首列排序）；`opendata/api/tables.py` 债务清偿；19 测试（真机跨年落 `p2027`） |
| A4.4 | DataPipeline 六步编排 + `pipeline_progress` 断点续拉 + 进程内锁 | `5374fe1` | `runner.py` 分片/断点/单标的失败隔离/`PipelineLockedError`/钩子注入；主库迁移 `2026_09_23-0002_pipeline_progress`；10 单测 + 真机中断续跑 |
| A4.5 | 交叉校对（经契约归一化 + 口径映射表）+ `dq_diff_report` + 告警治理 | `fbf64ee` | `mappings/akshare.yaml`（绑定 A2.5 真机样本列名）、`mapping.py`（fail-closed）、`cross_check.py`（键并集分母/缺失语义/重复键与字段集不一致即报错）、`diff_report.py`（键级幂等 + `alembic_data` 0002）、`alerts.py`（白名单/指纹去重/跃升 critical）；39 测试 |
| A4.6 | dwd 合并服务（权威优先/降级填补/留痕/`_diff_flag`/`_as_of`/修订传播/单源直通） | `1b56477` | `dwd_merge.py` + `DwdMergeService.affected_keys` 传播 + 复用 `DwdWriter`；13 测试（真机 `dwd_stock_daily`） |
| A4.7 | P0 模板（双源）+ 显式 `ENABLE_SCHEDULER` + 调度时间真机校准 | `f3c4bef`（部分） | `templates.py`（模板接线 ods→merge→校对）、`schedules.yaml`（4 个内置任务）、`scheduling.py`（显式决策）、配置默认由 `true` 改为未显式 + `main.py` lifespan 接入；15 测试。**真机校准与全市场双源待网络/凭证**（见 §4） |
| A4.8 | 新鲜度检查 + 告警矩阵 | `d9b9480` | `freshness.py`（`freshness_field` 由契约派生、ods 用映射源列名、缺表报 missing 不抛）+ `evaluate_alerts`（freshness/pipeline_failure/consecutive_failures/partition_missing/disk_water）；15 测试 |
| A4.9 | REST 最小集 + 参数白名单 + 注入用例 | `40d6542` | `query.py` + `api/data_query.py`：`GET /api/v1/data/{asset_class}/{domain}`、`/catalog`、`/{domain}/freshness`、`/{domain}/diff-report`；字段白名单（键恒选）、`symbols` 绑定参数、页码/页大小上限、枚举校验；27 测试 |

## 3. 门禁与覆盖率

| 证据 | 结果 |
|------|------|
| `gate.txt`（本目录） | `make gate` **PASSED**，exit 0；含 a2-check（96 个 A2 文件）、棘轮、零依赖、JS 执行点、前端 lint |
| `pytest.txt`（本目录） | **1742 passed / 3 skipped**（含真机 e2e），覆盖率 **86.46%**（门槛 84%） |
| 双环境 | Python 3.13：`make gate` PASSED；CI 孪生（3.11 + 全新依赖）：非 e2e 全绿 + `a2_check` ok + 棘轮「未增加」 |
| 真机 e2e 分布 | `test_ods_writer` / `test_partition_maintenance` / `test_warehouse_migrations` / `test_pipeline_runner` / `test_diff_report` / `test_dwd_merge` / `test_data_query_api` 各含 MySQL 标测用例 |
| `write-benchmark.txt`（本目录） | 写入基准原始输出（复算命令见文件头） |

## 4. 挂起项（需网络或上游凭证，均非代码问题）

| 项 | 阻塞原因 | 解除后动作 |
|----|----------|------------|
| AC-8「A 股日线**双源**（ths + akshare）全市场落 ods 两表」 | 缺 A3 同花顺 fuyao 凭证（当前仅 akshare 单源）；单源链路已真机贯通，模板在第二源存在时自动接入 A4.5 校对 | 凭证到位后跑 A4.7 模板全市场 dry run，双表落库并生成 `dq_diff_report` |
| AC-13「调度时间业务校准」 | 需真机观测「当日日线何时可拉」，而 `push2his/push2delay.eastmoney.com` 对本网络持续 `RemoteDisconnected`（sina、em datacenter、中证站点正常） | 恢复后实测调窗；当前 cron 为占位值（`schedules.yaml` 已注明） |
| A2.5 两个 kline 保真用例 | 同上网络原因 | `python scripts/codemod/compare_with_upstream.py --record` 一条命令补齐（`--compare` 存在 pending 时返回非零，不会静默通过） |
| AC-19「保留策略声明并实现」（计划将该项划入 A4） | A4 任务表（A4.1~A4.9）未列该任务，**计划与 AC 映射不一致**；当前仅有既有的执行记录保留（`retention="30 days"`），数仓侧（日线永久/分钟线 N 年/缓存 TTL/差异明细导出）未实现 | 待决策：补 A4.10 任务实现，或明确改由 1B/B3 承接 |

## 5. 本里程碑修复的缺陷与门禁收益

| 缺陷 | 影响 | 处置 |
|------|------|------|
| A4.4 e2e 用例误用真实表名并 DROP | 真机 `ods_stock_daily_akshare` 被测试 teardown 删除，新鲜度用例随之失败 | 改为探针表 `_probe_pipeline_runner`（提交 `f3c4bef`）；本地仓库以 `downgrade base → upgrade head` 复原 |
| 调度归属靠 `workers/redis` 推断 | 生产多 worker 无 Redis 会静默禁用；开发默认值与真实意图脱节 | 显式 `ENABLE_SCHEDULER` 决策：生产未显式配置启动即失败（AC-13 第二项） |
| 零依赖门禁捕获 13 处源标签字面量 | 新模板模块把源标识写成代码字面量，违反 AC-16 | 模板移入 `schedules.yaml`，源与权威序由映射/`authority.json` 派生 |
| `ruff check --fix` 全树误伤（A2 期教训） | 曾改写 37 个无关文件 | 本里程碑内所有 lint 修正均限定显式文件路径 |
