# Fuyao 财务观察采集切片：当前运行边界

本文描述迭代 204 中 Fuyao 单标的 ReviewedJob 开发切片及其当前隔离验收结果。它用于说明当前代码的边界，不代表生产验收、数据完整性确认或上线批准。应结合代码版本和该版本对应的验收证据阅读。

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

仓库普通 settings 可以从项目根目录的 `.env` 读取 `FUYAO_API_KEY`。但本 ReviewedJob 的 factory 和 handler 不读取 settings 或 dotenv，也不自动查找该 key；它们要求调用方显式提供 `FuyaoProcessDeploymentBinding`。当前还没有可信的生产装配入口，因此仅仅存在 `.env` 或其中有 `FUYAO_API_KEY`，都不能说明这个 ReviewedJob 已接通。固定 Fuyao executor 把 deployment binding 中的 API key 放入专用子进程环境变量，把其他绑定作为有界配置传入，并为子进程设置私有 `HOME`、工作目录（`cwd`）和固定的 `USER`。这些设置没有切换操作系统用户身份；子进程仍以调用方相同的操作系统权限运行。进程隔离用于限制生命周期和 IPC 边界，不是操作系统安全沙箱。

当前代码没有生产配置装配入口。这里列出的字段是代码接口边界，不是生产启用步骤或定时任务操作说明。数据包摘要用于固定所读文件内容，不等同于签名、来源真实性证明或数据授权证明。

## 每次 fetch 的 HTTP 发送上限

`ThsFinancialObservationFetcher` 接受显式的 `max_attempts`，只允许精确整数 `1` 到 `3`，默认值为 `3`。它限制一次 `fetch()` 中最多发生的物理 HTTP 发送次数；启用来源尝试 gate 的请求在每次物理发送前都必须完成一次新的 gate reservation，重试也会单独预留并记录本地 attempt trace。

本批已完成隔离验收的部署绑定实现会沿固定父子进程路径传递此限制：旧版 v1 **绑定输入**按兼容规则使用默认值 `3`；新版 v2 绑定输入必须显式携带 `transport_max_attempts`，并严格校验为 `1` 到 `3` 的整数（布尔值不接受）。这是绑定输入格式的兼容，不代表父子进程协议可以跨版本混用；父进程和固定子进程必须使用匹配版本。父进程只从受信部署绑定取值，固定子进程据此构造 fetcher；业务 run payload 没有覆盖这个上限的字段。子进程结果会回报绑定值，父进程在继续处理准备数据前要求它与自身绑定中的值完全相等。

该回报只证明父子进程对绑定配置值达成一致，不是 HTTP 物理发送次数的证据。物理发送仍须由发送前 gate reservation 和 attempt trace 等独立证据核对。默认部署值仍为 `3`；目前没有接入 reviewed one-shot factory、生产配置装配或真实 Fuyao 采集，因此不能将当前 ReviewedJob 描述为每个 run 只发一次 HTTP 请求。

这个限制依赖受信任的进程装配。fetcher 会检查自身预期上限与私有 HTTP client 设置的一致性，但这些仍是可变的 Python 进程状态；同一进程内的代码若同时篡改两处状态，可以绕过检查。它也不提供上游 exactly-once 保证；超时或进程故障仍可能使发送结果未知。

本批父子绑定传递的隔离验收共 **292 项通过**；`process_executor.py`、`fuyao_financial_process.py`、`fuyao_financial_run_job.py` 的 statement+branch combined 覆盖率依次为 **93.75%**、**90.67%**、**93.63%**，三个模块均为 `excluded_lines=0`。测试中的 preflight 与 HTTPX `MockTransport` 只证明本地校验、绑定传递和模拟响应路径，未向 Fuyao 发送请求，也不构成真实采集证据。此前 fetcher 上限实现的离线隔离证据位于 `/tmp/od204-fuyao-attempt-cap-20261029-5bb7/evidence`：两个目标测试文件共 **170 项合成测试通过**，目标模块 statement+branch combined 覆盖率 **97.00%**，`excluded_lines=0`。该快照同样使用 HTTPX `MockTransport`、临时 SQLite gate 和合成数据，没有访问 Fuyao 实源。

本批测试运行版本为 pytest **8.3.4**、pytest-asyncio **1.0.0**、pytest-cov **6.2.1**、coverage **7.9.2**；仓库 `requirements.txt` 固定 pytest **8.3.3**、pytest-asyncio **0.24.0**、pytest-cov **6.0.0**，因此本次通过不能替代锁定依赖版本下的复验。此前失败的执行记录仍保留，最终通过数不覆盖这些历史失败。完整 canonical A2 门禁未能运行，所需的 `docs/evidence/C100/quality-scope-audit/immutable-wave9-exact5055/classification.json` 不存在。mypy 仍报告 3 项已有诊断：`financial_observation_store.py:544` 的冗余 cast，以及 `run_service.py` 中冗余 bytes cast 和 decode 配对类型不匹配；这些诊断不应记作本批通过项。本批结果不代表生产装配、真实采集、全 A 股历史缺口核对或生产验收。

## 一次性部署声明快照（非执行契约）

`opendata/pipeline/fuyao_one_shot_contract.py` 新增纯数据声明：它把固定的 THS/Fuyao 财务报表查询范围（Fuyao 主机、`financial_statement` 模型、`query` 操作、单次 HTTP 尝试、预算绑定版本）与调用方提供的 bundle、来源策略、grant、预算绑定/策略及凭证修订声明组成封闭、不可变的快照，并对规范 JSON 计算带域分隔的 SHA-256 指纹。快照记录的来源和验证标签也是调用方的声明；输入摘要只做格式校验，并不从外部重新读取或核实对应内容。

该 fingerprint 只绑定快照中的部署声明，**不绑定请求参数、capture intent 或 run**，也不等同于执行凭据。它不能证明预算 quota key 或预算绑定对应实际 gate 实体、请求主体或有效额度，不能证明 source grant 的授权范围、有效性或真实性，也不能证明数据完整性。模块不会创建、启用或调度 job，不会调用 provider、读取 key 或发起 Fuyao 请求；因此这份快照不构成真实的一次性执行能力或生产就绪证明。

快照没有 API key、secret、password 等秘密字段，构造器拒绝额外字段；自由标识字段只接受受限格式并拒绝一小组已知凭证标记。这只是有限的输入防误用检查，不能识别所有伪装成普通标识的密钥。调用方必须确保 `grant_id`、`grant_principal`、`source_policy_id`、`source_policy_revision` 和 `budget_quota_key` 等标识不包含任何秘密；不得把 API key 或其他密钥放进这些字段，也不得把它们作为摘要输入。

正常导入路径 `opendata.pipeline.fuyao_one_shot_contract` 会先执行既有 `opendata.pipeline` 包初始化；该初始化导入 runner 和 ORM 模型，后续数据库模块会加载应用 settings 并构造 SQLAlchemy engine。因此，这个纯模块的隔离单测使用 namespace-package 快照，省略了 `opendata/pipeline/__init__.py`，以避免导入应用配置和创建 engine。该方式没有验证正常包导入、应用启动或生产组合；后续集成时仍需单独审查并处理这项导入副作用。

针对该契约的隔离验收中，86 项合成测试通过；模块 statement+branch combined 覆盖率为 **273/275（99.27%）**，`excluded_lines=0`。证据来自 `/tmp/od204-fuyao-contract-run-20261029-92N1qghO/evidence`，测试只构造内存中的合成声明，没有访问网络、provider、数据库、API key、应用 settings 或真实 grant。该运行使用 CPython **3.13.5**、pytest **8.3.4**、pytest-cov **6.2.1** 和 coverage **7.9.2**；与仓库固定的 pytest **8.3.3**、pytest-cov **6.0.0** 不同。结果仅验收该 namespace-package 快照中的纯契约模块，不是正常包导入、全量质量门禁、生产验收或真实 Fuyao 调用的证据。

## 尚未核实的声明

来源授权状态、单位映射版本与摘要、保留类别和历史范围都可能来自 operator 声明，当前状态应按 `operator_declared_unverified`、`operator_policy_allowed_unverified` 等字段理解。它们不是独立核验过的合同授权、单位语义、来源真实性或历史完整性证明。attempt 与本地 trace 的关联状态也是 `local_trace_matched_unverified`；source request id 匹配状态同样明确标注为未验证或不可用时未验证。

该切片没有全 A 股证券清单覆盖核验、来源保留边界核验、全历史缺口对账、生产装配、持久化提交意图、raw/SQL 跨存储恢复或端到端生产运行证据。不能把单次成功响应、bundle 摘要或运行完成标记解释为数据集完整。

## 合成测试与真实子进程边界

`tests/test_fuyao_financial_run_job.py` 的运行与提交组合用例使用临时 SQLite 文件和 HTTPX `MockTransport`。为聚焦归档与事务边界，这些用例会替换 executor 方法并在测试进程内直接调用 handler；它们**不覆盖真实 Fuyao 请求或真实数据采集**。其中的回滚场景验证 SQL 回滚时已写入的 raw evidence 仍可能保留。绑定传递的 preflight 和 mock 用例也只证明本地路径。

`tests/test_process_executor.py` 单独覆盖进程协议和子进程生命周期边界；Fuyao 专用真实 worker 用例验证缺少配置时在网络请求前失败关闭。该失败路径和 IPC 单元用例不能替代“成功的 Fuyao 请求完整经过真实子进程、来源授权、raw 写入、归档事务”的端到端证据。测试结果及覆盖范围必须以对应代码版本的独立验收记录为准。

## 相关代码

- [`fuyao_financial_run_job.py`](../opendata/pipeline/fuyao_financial_run_job.py)
- [`fuyao_financial_process.py`](../opendata/pipeline/fuyao_financial_process.py)
- [`process_executor.py`](../opendata/pipeline/process_executor.py)
- [`test_fuyao_financial_run_job.py`](../tests/test_fuyao_financial_run_job.py)
- [`test_process_executor.py`](../tests/test_process_executor.py)
