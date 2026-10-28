# Fuyao 财务观察采集切片：当前运行边界

本文描述迭代 204 中待验收的 Fuyao 单标的 ReviewedJob 开发切片。它用于说明当前代码的边界，不代表生产验收、数据完整性确认或上线批准。应结合代码版本和该版本对应的验收证据阅读。

## 当前状态与范围

`build_fuyao_financial_job()` 只构造一个 `ReviewedJob`：任务必须保持 `enabled=False`、`schedule=None`，并且 `reviewed=False`。该工厂本身不注册任务、不启动 worker，也没有生产配置装配入口。因此，这个切片目前是禁用且未评审的开发能力，不能据此推断线上采集已经启用。

一次请求只接受一个交易所限定的股票代码、一个报表类型（利润表、资产负债表或现金流量表）和一个周期（年度或季度）。查询可以按有限条数请求最近期间，或指定有界日期区间；日期端点相距最多 3,650 天。任务不会展开全 A 股，也不会推断上市日或保证覆盖全部历史。代码格式可被请求校验器接受，不表示该证券一定在数据包或来源中可用。覆盖情况在切片中明确标为未评估。

## 调用与写入路径

调用链从显式构造的任务和部署绑定开始：

1. `build_fuyao_financial_job()` 校验固定任务元数据，并生成禁用、未评审的 `ReviewedJob`。
2. `FuyaoFinancialProcessFetchHandler` 校验请求范围，使用运行服务提供的 run key，并通过固定操作标识调用 reviewed process executor。
3. Fuyao 专用子进程接收封闭环境配置，校验绑定的数据包及其 SHA-256、来源策略及其 SHA-256，以及已有的来源尝试授权记录；随后调用 THS/Fuyao 财务报表 fetcher。
4. fetcher 收到的数据写入 raw evidence，并生成有大小限制的 `PreparedCapture`。子进程只返回固定字段的摘要和编码后的准备数据；父进程再次校验结果。
5. normalize 阶段生成带同一运行身份的 `NormalizedBatch`。`RunService` 在进入提交事务前解码受限的准备数据，再由归档 publisher 使用运行 ledger 提供的围栏 SQL 连接写归档数据和运行成功状态。

**文件系统 raw 写入和 SQL 提交不是一个原子事务。** 如果 raw 文件写入后、SQL 归档事务提交前发生失败，可能留下没有对应已提交归档记录的 raw 文件。当前切片没有持久化的 prepared-capture 意图，也没有崩溃恢复或孤儿 raw 对账流程。

## 配置与信任边界

`FuyaoProcessDeploymentBinding` 是显式部署输入，包含 API key、数据包路径及摘要、来源策略路径及摘要、来源尝试 gate 数据库路径和 grant 身份、raw evidence 路径，以及保留期和来源历史限制声明。run payload 只承载受限的业务查询字段，不承载这些部署字段。

仓库普通 settings 可以从项目根目录的 `.env` 读取 `FUYAO_API_KEY`。但本 ReviewedJob 的 factory 和 handler 不读取 settings 或 dotenv，也不自动查找该 key；它们要求调用方显式提供 `FuyaoProcessDeploymentBinding`。当前还没有可信的生产装配入口，因此仅仅存在 `.env` 或其中有 `FUYAO_API_KEY`，都不能说明这个 ReviewedJob 已接通。固定 Fuyao executor 把 deployment binding 中的 API key 放入专用子进程环境变量，把其他绑定作为有界配置传入，并将该子进程的临时目录设为私有目录。进程隔离用于限制生命周期和 IPC 边界；它不是操作系统安全沙箱，子进程仍以调用方相同的操作系统权限运行。

当前代码没有生产配置装配入口。这里列出的字段是代码接口边界，不是生产启用步骤或定时任务操作说明。数据包摘要用于固定所读文件内容，不等同于签名、来源真实性证明或数据授权证明。

## 尚未核实的声明

来源授权状态、单位映射版本与摘要、保留类别和历史范围都可能来自 operator 声明，当前状态应按 `operator_declared_unverified`、`operator_policy_allowed_unverified` 等字段理解。它们不是独立核验过的合同授权、单位语义、来源真实性或历史完整性证明。attempt 与本地 trace 的关联状态也是 `local_trace_matched_unverified`；source request id 匹配状态同样明确标注为未验证或不可用时未验证。

该切片没有全 A 股证券清单覆盖核验、来源保留边界核验、全历史缺口对账、生产装配、持久化提交意图、raw/SQL 跨存储恢复或端到端生产运行证据。不能把单次成功响应、bundle 摘要或运行完成标记解释为数据集完整。

## 合成测试与真实子进程边界

`tests/test_fuyao_financial_run_job.py` 的运行与提交组合用例使用临时 SQLite 文件和 HTTPX `MockTransport`。为聚焦归档与事务边界，这些用例会替换 executor 方法并在测试进程内直接调用 handler；它们**不覆盖真实 `Popen`、封闭子进程环境或真实 IPC**。其中的回滚场景验证 SQL 回滚时已写入的 raw evidence 仍可能保留。

`tests/test_process_executor.py` 单独覆盖进程协议和子进程生命周期边界；Fuyao 专用真实 worker 用例验证缺少配置时在网络请求前失败关闭。该失败路径和 IPC 单元用例不能替代“成功的 Fuyao 请求完整经过真实子进程、来源授权、raw 写入、归档事务”的端到端证据。测试结果及覆盖范围必须以对应代码版本的独立验收记录为准。

## 相关代码

- [`fuyao_financial_run_job.py`](../opendata/pipeline/fuyao_financial_run_job.py)
- [`fuyao_financial_process.py`](../opendata/pipeline/fuyao_financial_process.py)
- [`process_executor.py`](../opendata/pipeline/process_executor.py)
- [`test_fuyao_financial_run_job.py`](../tests/test_fuyao_financial_run_job.py)
- [`test_process_executor.py`](../tests/test_process_executor.py)
