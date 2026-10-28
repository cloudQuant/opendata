# 财报归档 Reader 运维说明

## 当前范围

`opendata.data.financial_observation_archive_reader.read_financial_observation_page`
是一个只读、有限范围的 SQLAlchemy Core 查询 helper。调用方必须显式传入已经建立的
`Connection`；模块不会读取项目配置或 `.env`，不会创建 engine、打开连接、管理事务、调度
任务或提供 REST 路由。它读取已归档的 `od_financial_observations`，不负责采集、写入、
补数、完整性核验或 PIT（point-in-time）重建。

Reader 已通过一次本地隔离 MySQL 8.4.0 引擎上的合成数据验收，适用范围见下文。它没有
可供外部调用的 HTTP 分页协议，也没有生产数据覆盖、负载或生产环境验收。

## 查询范围与分页

每次查询必须明确给出一个来源、一个交易所限定的股票代码、一个报表类型、一种频率和
起止报告期。支持 `income`、`balance`、`cashflow`，以及 `annual`、`quarterly`。股票代码
格式为六位数字加 `.SH`、`.SZ` 或 `.BJ`。起止日期都包含在内，范围最多 3,660 天；每页
只能请求 1 到 200 条记录。

结果按以下键升序排列并使用 keyset continuation：

1. `report_period`
2. `item_code`
3. `observed_at_utc`
4. `observation_id`

Reader 保留 `value_decimal_text` 的原始文本或 `NULL`，不转成浮点数。报告期、源披露日期和
OpenData 观察时刻分别返回，不能把披露日或观察时刻当作报告期。观察时刻在归档表中使用
naive UTC `DATETIME`，Reader 返回带 UTC 时区的值。

返回对象的 `completeness` 固定为 `unknown`，`point_in_time_reconstruction` 固定为
`unavailable`。页面行数、最后一页或某张表中存在记录，都不能证明历史连续、证券范围
完整或来源数据完整。

## 连接和事务要求

调用方负责整个连接和事务生命周期，并须按下列顺序使用：

1. 取得目标数据库的 SQLAlchemy Core `Connection`，显式设置隔离级别为 MySQL
   `REPEATABLE READ`。
2. **在启动事务之前**完成隔离级别设置，然后开启一个事务。
3. 在同一 `Connection`、同一活动事务中串行读取所有页面。分页期间禁止其他协程、线程或
   调用方代码并发使用该连接，也不要改变隔离级别或事务状态。
4. 遍历结束后，由调用方提交或关闭事务和连接。Reader 不提交、不回滚，也不关闭连接。

Reader 会检查数据库方言、MySQL 版本、当前活动事务、SQLAlchemy 报告的隔离级别，以及
归档表的存储引擎。仅接受 MySQL 8.0.16 或更高的 MySQL 8 版本和 InnoDB；MariaDB、
SQLite、其他数据库和非 InnoDB 归档表会被拒绝。每页都会查询
`information_schema.tables` 确认表引擎，再读取归档表。

这些检查只能提供有限保证。`Connection.get_isolation_level()` 报告 SQLAlchemy 连接当前
看到的隔离设置，不能证明调用方在事务开始前就设置了 `REPEATABLE READ`，也不能证明事务
开始后没有更改隔离设置。因此调用方必须遵守上述顺序和独占使用要求；helper 无法验证
已经开启的事务实际采用了何种 MVCC 语义。本地隔离验收在受控的事务开始前设置了
`REPEATABLE READ`，并验证了该场景下的数据库行为；这不改变 helper 本身的验证边界。

MySQL 的一致性读视图由事务中的第一次归档表读取建立。如果调用方在进入 Reader 之前已经
执行过一致性读，分页会沿用那个更早的读视图。Reader 不分配、不返回数据库快照时间，
也不验证之前的读取是否采用了预期语义。隔离设置检查本身不构成数据库集成验收。

## Cursor 的边界

Continuation cursor 是进程内 Python 对象，持有原连接和根事务的对象引用，并绑定原查询
范围。后续页必须使用完全相同的来源、股票、报表类型、频率、日期范围、连接对象和事务
对象；换连接、换事务或改范围会被拒绝。

Cursor 不是可序列化的 token、身份凭证或 REST 分页参数。它不能跨进程保存、重启恢复、
传给另一个 worker，或由 HTTP 客户端安全持有。不得把它编码进 URL、响应体或缓存后假设
可以恢复同一数据库快照。REST 接口需要单独设计可验证的分页协议及其快照生命周期，不能
直接暴露这个对象。

## 完整性和 PIT 限制

Reader 返回归档里当前存在的观察，不会生成应有报告期，不会对账扫描清单、公告边界、来源
保留期限或隔离记录，也不会判定缺失期是确认缺数还是未知。因此即使查询正常结束且返回
了若干行，完整性仍是未知。

每条观察的策略为 `current_as_observed_only`。归档中没有足够的版本历史和可信披露时间
证据来还原某个过去交易时点当时可见的财报值；Reader 不应被用于声称无前视偏差或 PIT
准确。需要 PIT 能力时，必须另行解决版本留存、源披露时间、修订关系和历史覆盖，并以
证据验证，不能从当前页面推断。

## 索引和容量注意事项

当前 store 元数据把范围索引声明为
`ix_od_financial_observations_scope(source_id, instrument_id, statement_type, periodicity, report_period)`；
归档迁移中相同列序的索引名则是 `ix_od_fin_observation_history`。这是名称不一致，需在
真实迁移后的库中核对实际索引，不能只凭 Python 元数据推断。该列序支持固定范围过滤；
Reader 的排序键还包括 `item_code`、`observed_at_utc` 和 `observation_id`，这些列不在该
索引中。下方 `acceptance-result.json` 摘要记录两种查询使用迁移创建的
`ix_od_fin_observation_history` 做 range 访问，并估计每次扫描 8 行。但验收 runner 在
缺少 `using_filesort` 时会把它默认为 `false`，且没有保存原始 EXPLAIN 或检查
`ordering_operation`；因此摘要中的 `false` 不能证实实际计划没有 filesort。该库有六条
目标观察和 2,500 条不同查询范围的合成填充记录；填充记录不构成同范围负载，因此这些
计划摘要不能推断真实数据量下的排序、回表、响应时间或大规模 API 性能。

在接入批量或外部 API 之前，应在隔离的 MySQL 8 InnoDB 环境中检查两种分页 SQL 的
`EXPLAIN`/实际执行计划，用代表性行数和选择度测量扫描行数、排序、响应时间和并发影响；
同时评估复合索引成本、连接池上限、请求超时和事务持续时间。长时间保持一致性读事务会
延长旧版本的保留时间并增加 InnoDB undo/purge 压力。任何索引变更都应通过独立迁移和
真实 MySQL 验收，不在此 Reader 文档中假定已经完成。

## 验收记录和待办

离线合成测试记录位于
[`var/reports/afi204/2026-10-28-financial-archive-reader`](../var/reports/afi204/2026-10-28-financial-archive-reader)：

- `pytest-initial.log`：16 项通过，但 statement+branch 覆盖率为 89.45%，低于该模块
  90% 门槛；这次运行不算覆盖门槛通过，日志保留作为失败记录。
- `pytest-final.log`：33 项离线测试通过，statement+branch 覆盖率为 98.49%。测试使用合成
  连接和固定行，不连接数据库。

离线 Mock 测试只验证合成连接上的参数、排序、cursor 绑定和字段映射；它们不能证明真实
MySQL 行为。另有一次本地隔离 MySQL 8.4.0 合成数据验收，记录归档于
[`mysql-acceptance/`](../var/reports/afi204/2026-10-28-financial-archive-reader/mysql-acceptance)：

- `acceptance-result.json`：机器可读的最终结果和断言摘要；`controller.log`：对应控制器
  日志文件，本次为空。验收使用独立隔离的 PID/网络命名空间，mysqld 禁止网络，在新空
  schema 上执行 `financial_archive_0001` 迁移；没有连接生产数据库。
- 验收在事务开始前设置 `REPEATABLE READ`，对六条目标观察以每页两行读取。另一个事务在
  读取期间提交两条新观察并更新一条旧观察；原事务续页仍看不到新行、保留旧值且没有
  缺失或重复，新事务能看到已提交的变更。把旧 cursor 交给替换后的事务，会在读取归档表
  前被拒绝。
- 测试断言保留了十进制原文和 `NULL`、报告期、披露日期及 UTC 微秒。该结果验证的是受控
  的隔离事务场景，不能证明调用方任意事务时序或并发使用连接都安全。
- JSON 摘要记录两个 EXPLAIN 结果均使用迁移索引 `ix_od_fin_observation_history` 做
  range 访问，估计扫描 8 行。摘要还写了 `using_filesort=false`，但 runner 在字段缺失时
  默认填入 `false`，且未保存原始计划或检查 `ordering_operation`，所以是否发生 filesort
  尚未由这份证据确认。2,500 条填充记录均属于不同查询范围，并未模拟目标范围内的大型
  数据集，也不是容量或性能验收。
- 控制器结果记录的 mysqld 清理方式为 `pidfd_sigterm_exit`、退出码为 0；这是受审脚本的
  结果和 server lifecycle 日志证据，不是独立的 syscall trace。`mysqld-foreground.log`
  记录可选组件 `component_reference_cache.so` 加载失败信息；MySQL error log 另记录了
  `--initialize-insecure` 临时 root 空密码和自签名 CA 告警。服务器之后报告 ready 并正常
  关闭；这些启动和清理信息均保留为验收环境背景，不代表生产环境配置。

此前几次 MySQL 初始化失败作为独立记录保留，不计入本次最终 PASS。初始离线覆盖率未达标
的记录也继续单独保留。上述隔离 MySQL 结果补充了明确的引擎和快照场景，但没有覆盖生产
数据、生产环境、完整历史或高负载。Reader 仍将完整性报告为 `unknown`、PIT 重建标记为
`unavailable`，也没有 REST 分页 API。因此不应把 Reader 标记为生产可用的历史财报 API。

在更广泛的服务使用前，仍须确认真实迁移库中的索引与 store 元数据一致性，并使用目标
数据规模评估查询计划、延迟和并发；同时验证 API 的事务边界、连接池、超时、连接独占与
长事务影响。任何索引变更都应通过单独迁移和验收。
