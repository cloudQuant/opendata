# Fuyao 单标的手工组装预检

`opendata.pipeline.fuyao_manual_composition.prepare_fuyao_manual_preflight`
提供默认关闭的配置预检，以及一个必须显式 opt-in 的只读 gate grant advisory。
即使返回 `preflight_only` 且 `gate_grant_status=matched`，也不会创建或启用
`ReviewedJob`、提交 run、启动 worker/scheduler、reserve attempt 或发送 Fuyao 请求。
它不能替代一次手工运行的上线验收。

## 输入与检查

调用方显式传入 `FuyaoManualCompositionConfig`，其中包含现有
`FuyaoProcessDeploymentBinding` 和一个有界 `FuyaoManualQuery`。省略配置或保持
`preflight_enabled=False` 时，函数立即返回 `disabled`，不读取路径、不检查凭证，也不做
gate 数据库或路径 I/O；`gate_grant_status` 为 `not_checked`。将 key 放进本机 `.env`
本身不会启用预检或触发 gate 检查。

打开预检后，流程会：

1. 重新验证 deployment binding 的路径、bundle/policy SHA-256、grant ID/revision
   形状及其他有界字段。
2. 通过 `FuyaoCredentials` 检查传入 key 非空且可被凭证对象接受。此项不联系 Fuyao，
   不能证明 key 有效、未过期或具备接口权限。
3. 使用现有严格 bundle loader 校验 bundle 文件权限、实际字节 pin 和单标的范围，
   并要求其证券与手工 query 完全一致。
4. 安全读取 source policy 文件并比较独立 SHA-256 pin，解析唯一的 THS 财报查询声明，
   检查其 operator 声明为 `ALLOWED`、无未解析条件、Fuyao host 在允许范围内且声明
   尚未过期。
5. 用现有 manual payload builder 校验 statement、periodicity、日期/limit 和
   idempotency key。
6. 只有以上本地输入检查全部通过后，才可能进行 grant advisory：配置中必须显式注入
   精确类型的 `SourceAttemptGate`，并通过下文的数据库路径绑定检查。缺少 gate 时，或
   gate 类型、路径绑定不符合要求时，返回 `gate_grant_status=indeterminate`；没有调用
   `inspect_grant`。满足条件时只调用一次只读 `inspect_grant`。

bundle/policy 的 `ALLOWED` 声明及摘要都不证明供应商授权、单位语义或数据真实性。
它们只是对本地配置的结构、字节和 operator 声明进行检查。

## Gate grant 只读预检

只读检查要求调用方同时显式设置 `preflight_enabled=True` 并注入 `SourceAttemptGate`。
只有本地 binding、bundle、policy 和 query 检查均通过后，预检才向 gate 传入请求：grant ID
和 expected revision 来自已验证的 `FuyaoProcessDeploymentBinding`；source=`ths`、
canonical model=`financial_statement`、operation=`query` 及 Fuyao API host 由代码固定；
principal scope 来自同一 binding。预检还要求 gate 类型精确为 `SourceAttemptGate`，且
`binding.gate_database_path` 每一级均不是 symlink、严格解析后的路径等于原路径，并与
gate 构造时固定的主数据库路径一致。路径不匹配时不调用检查，报告只返回固定的
`indeterminate`，不泄露路径。

`SourceAttemptGate.inspect_grant` 使用 SQLite 文件只读连接和只读事务快照，不调用
`reserve_attempt`，不会写 attempt tombstone、推进持久时钟或扣减 quota。它按检查时可见的
本地 grant 记录校验允许状态、revision、来源/模型/操作/主体 scope、时间范围、撤销状态、
条件和 host seal 等结构。报告只公开三态 `matched`、`rejected`、`indeterminate`，不会返回
grant 内容或底层 reason。gate 内部 reason 是固定枚举：
`grant_matched`、`grant_not_found`、`grant_not_allowed`、`grant_invalid`、
`grant_revision_mismatch`、`grant_scope_mismatch`、`grant_not_yet_valid`、
`grant_expired`、`grant_evidence_incomplete`、`grant_conditions_invalid`、
`grant_conditions_unresolved`、`grant_hosts_unsealed`、`grant_hosts_invalid`、
`grant_host_mismatch`、`database_unavailable`、`schema_unavailable`、`clock_invalid`、
`grant_record_unverifiable`、`grant_seal_unverifiable`。异常、未知结果和无法确认的状态都
折叠为 `indeterminate`，不将数据库诊断带入报告。`grant_matched` 映射为
`matched`；`grant_not_found`、`grant_not_allowed`、`grant_revision_mismatch`、
`grant_scope_mismatch`、`grant_not_yet_valid`、`grant_expired`、
`grant_conditions_unresolved` 和 `grant_host_mismatch` 映射为 `rejected`；其余固定 reason
映射为 `indeterminate`。

这只是单次只读快照 advisory：`matched` 不证明供应商合同或外部数据权利真实有效，不认证
grant 中所引用证据的内容，也不证明发送时仍有可用额度或速率预算。grant 在检查后可能
变化。**每一个真实发送**仍必须由 transport 在发送前调用 gate 的 `reserve_attempt`，并且
只有收到该次 `RESERVED_NEW` 才能发送；检查结果不能缓存或替代 reserve。

主数据库以只读方式打开，不代表所有文件系统操作都绝对无副作用。SQLite 在 WAL 模式下
可能需要访问现有 `-wal` / `-shm` sidecar；sidecar 不存在、权限或状态不适用时，检查可能
失败关闭。路径和文件身份校验可发现常见的 symlink 或替换情况，但不能防止拥有相同 OS
用户权限的进程在检查与打开之间短暂替换文件再恢复（swap-and-restore TOCTOU）。因此该
advisory 不适合作为对抗同用户恶意进程的安全边界，也不能替代文件权限隔离。

## Gate 和执行边界

不论 gate advisory 返回哪种状态，报告均保持 `dispatch_allowed=False`、
`worker_started=False`、`scheduler_started=False`。检查不会创建或启用 `ReviewedJob`，也
不会提交 run。即使状态为 `matched`，当前预检也仍不能变成可执行 job。

当前财报 job factory 返回 `reviewed=False`、`enabled=False`，ReviewedTaskCatalog 和
RunService 会拒绝它。这个模块不会复制或改写 job 标志来绕过拒绝。现阶段没有生产
composition 能把 preflight report 变成可执行 job。启用手工运行需要另一项经过验收的
部署装配工作：明确 review 当前代码版本，构造只允许手工 dispatch 的 reviewed job，
固定 `api` 与 `worker` 角色且不配置 scheduler，并在实际 transport 时保留 gate 的最终
授权检查。

## 凭证

项目现有 `Settings` 支持从本地 `.env` 读取 `FUYAO_API_KEY`。可以由应用配置层读取后
显式构造 `FuyaoProcessDeploymentBinding`；本预检模块本身不加载 `.env`、不查找环境
变量，也不会在报告或异常中包含 key。`.env` 不应提交，测试只能使用合成凭证。

`tests/test_fuyao_manual_composition.py` 使用合成 key，并只在 `tmp_path` 下写入合成
bundle、policy 和用于 gate 路径 pin 检查的普通占位文件；测试不会连接或查询 gate 数据库、
访问网络，也不直接读取 `.env`。普通 pytest 会加载仓库 `conftest.py`，其导入路径可能读取
项目 `.env`，所以不能仅凭测试代码断言一次常规 pytest 完全不接触 `.env`。隔离执行时，将
所需代码和测试冻结到排除 `.env` 与宿主配置的快照中，从隔离目录使用
`--noconftest -c /dev/null` 运行明确选择的节点。详细验收命令和结果记录在独立验收报告中。
