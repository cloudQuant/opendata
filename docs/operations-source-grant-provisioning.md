# 手动签发本地 source grant

本文描述 `opendata.data.source_grant_provisioning` 当前提供的内部 Python API。它把本地操作者填写的授权声明写进一个已存在的 source gate SQLite 文件，并为一个精确主机封存 scope。它不会创建采集任务、发送请求或启动 Fuyao。

此流程只记录操作者自声明。它不核验供应商合同、授权材料、数据权利、配额、来源策略或文件证据；证据引用和 SHA-256 是操作者输入的元数据，SHA-256 只校验格式，不读取文件或重新计算。`operator_confirms_authorized_use=True` 是明确的本地自声明，不是独立审批，也不能证明声明真实。SQLite 审计数据只适用于受本机账户和目录权限保护的本地控制面，不是抵御特权用户篡改的审计系统。

## 请求范围

创建 `SourceGrantProvisionRequest` 时必须给出以下字段：

| 字段 | 当前约束 |
| --- | --- |
| `grant_id` | 有界不透明标识；每个 ID 只能签发一次 |
| `revision` | 新 grant 必须是 `1` |
| `source`、`canonical_model`、`principal_scope`、`attested_by` | 有界标识，不能包含凭证形态文本 |
| `operation` | 只接受 `query`、`store`、`export`、`subscribe` |
| `host` | 一个精确 DNS 主机名；规范化为 IDNA ASCII、小写并去掉尾点；拒绝 scheme、端口、路径、通配符和 userinfo |
| `not_before`、`expires_at` | 必须是带时区的 `datetime`；统一转成 UTC 微秒。`not_before` 可早于或晚于签发时的 gate clock；`expires_at` 必须晚于 `not_before`，并且签发时，`expires_at` 必须晚于 source gate 的有效时钟。未来生效的 grant 可以写入，但生效前检查会拒绝它 |
| `rights_evidence_reference` | 1–512 个字符的非凭证元数据；不会跟随引用或打开文件 |
| `rights_evidence_sha256` | 64 位小写十六进制字符串；只做格式验证 |
| `operator_confirms_authorized_use` | 必须明确传入布尔值 `True` |
| `operator_attestation_note` | 必填、非凭证文字；最多 900 字符，连同系统添加的自声明前缀不得超过 1024 字符 |
| `conditions` | 必须是空 tuple；当前 gate 不解释非空条件 |

此 scope 只绑定来源、规范数据产品名、操作、主体、单一主机和有效期。它**不绑定证券代码、市场范围、交易日/财报期、字段清单或行数**，也不会验证 `canonical_model` 是否真的对应某个数据产品。因此它不能单独表达或强制“只可采集全 A 股某段历史数据”之类的细粒度授权。需要这种限制时，调用链还必须有独立、可验证的任务级和请求级策略。

请求和结果对象默认 `repr` 会隐藏操作者文字、证据引用及主体/主机等字段。输入也会拒绝常见密钥格式、凭证字段、URI userinfo 和 `.env` 字样；不要把密钥、令牌或合同内容塞进任一字段。

## SQLite 前置条件与提交过程

调用者必须先显式选择并构造 `SourceAttemptGate`。它只接受一个已经存在的普通文件型 SQLite 数据库，不接受内存库或其他数据库。Provisioner 会在写事务中再次校验绑定文件上的完整 gate schema、UTF-8 编码、外键状态和独立 v2 迁移标记 `sg0002_source_attempt_scope_v2`；schema 必须与当前固定契约相符。缺表、部分迁移、漂移、错误时钟或文件身份变化都会 fail closed。迁移是独立运维步骤，不会由应用启动、这个 API 或调度器自动执行。

成功路径在 SQLite `BEGIN IMMEDIATE` 写锁内推进持久化高水位时钟、检查 grant ID 未使用、写入 `od_source_grants` 的 `ALLOWED` 自声明记录、插入唯一主机到 `od_source_grant_hosts`，再写入 `od_source_grant_host_sets` 的单主机封存行。现有 SQLite trigger 会在同一事务内写入 `HOSTS_SEALED` 事件。提交前实现会回读并逐项核验 grant、主机封存和审计事件；任何提交前失败都不应留下部分 grant 或高水位时钟更新。封存记录及相关审计是持久记录；不得手工改表来重试或“修复”一个不确定结果。

数据库路径由调用者传入，模块不会猜测路径、加载应用设置或读取 `.env`。当前接口是内部 Python 方法，没有 CLI、REST 路由、自动 scheduler、自动 Fuyao 任务装配或默认启动行为。使用 SQLite 文件身份检查也不能阻止同一用户进程在检查间替换路径；应保护本地账户和 gate 文件目录。

## 仅供说明的合成示例

下面的请求数据是虚构占位内容，不代表供应商、真实产品或授权证据。它只展示请求形状；此示例不构造数据库连接、不创建授权，也未在生产 gate 上运行。实际调用还需要一个由运维人员显式选定且已完成迁移的本地 gate 对象。

```python
from datetime import datetime, timezone

from opendata.data.source_grant_provisioning import (
    SourceGrantProvisionRequest,
    SourceGrantProvisioner,
)
from opendata.data.source_attempt_gate import SourceAttemptGate

request = SourceGrantProvisionRequest(
    grant_id="synthetic-grant-204",
    revision=1,
    source="synthetic-source",
    canonical_model="synthetic-daily-bars",
    operation="query",
    principal_scope="synthetic-local-service",
    host="api.example.invalid",
    not_before=datetime(2030, 1, 1, tzinfo=timezone.utc),
    expires_at=datetime(2030, 2, 1, tzinfo=timezone.utc),
    rights_evidence_reference="synthetic://example.invalid/evidence/204",
    rights_evidence_sha256="0" * 64,
    operator_confirms_authorized_use=True,
    operator_attestation_note="Synthetic documentation example; no supplier rights asserted.",
    attested_by="synthetic-operator",
    conditions=(),
)

# `gate` 必须由调用者用已存在、已核验并已迁移的本地 SQLite 文件显式构造。
# result = SourceGrantProvisioner(gate).provision(request)
```

## 返回值、重复和过期

成功返回 `SourceGrantProvisionResult`，状态为 `PROVISIONED`，带有 grant ID、revision、规范化主机、有效时间和 `attested_by`。重复提交同一个 `grant_id` 会抛出 `SourceGrantProvisioningConflictError`；它不是幂等的“返回原结果”，不会覆盖、续期或修订既有记录。当前新建请求只允许 revision 1。过期时间已不晚于 gate 有效时钟的请求会在写入前拒绝；已经成功写入的 grant 过期后仍是历史记录，`SourceAttemptGate.inspect_grant()` 对它返回拒绝结果 `GRANT_EXPIRED`，不会自动续期。

三类清理/提交错误必须分别处理，不可盲目重放：

| 异常 | 含义 | 运维处理 |
| --- | --- | --- |
| `SourceGrantProvisioningCommitUncertainError` | commit 开始后未能确认提交，或提交确认丢失 | 用原请求的完整精确 scope（grant ID、revision、source、model、operation、principal、host）调用只读 `inspect_grant()` 对账。`MATCHED` 表示 grant 已存在，禁止重试；`INDETERMINATE`、scope 不匹配或其他异常状态需人工查明，不能据此推定未提交。只有确认原写入不会再完成且同一 ID 确实不存在后，才评估是否重试。 |
| `SourceGrantProvisioningCleanupError` | commit 尚未开始，但事务/连接清理未能确认 | 先解决本地 gate 的连接和事务状态；在清理状态明确前不要重试。不要直接删除或修改 gate 表。 |
| `SourceGrantProvisioningCommittedCleanupError` | commit 已明确确认，之后连接关闭失败 | 不要重试该 grant。保留异常记录；如需确认持久状态，用完整 scope 做只读检查。 |

`inspect_grant()` 的 `MATCHED` 仅表示本地 grant 与本次检查 scope 及封存证据相符；它不是供应商授权结论、HTTP 发送许可或 Fuyao 调度许可。真正发送请求还需要调用方在每次物理 HTTP 尝试前单独走原子 attempt reservation，并检查其结果；本 provisioning API 不会替调用者执行这一步。

## 验收记录与限制

v5 是集成测试中的 `QUOTA_KEY` 改为 `quota-test` 之前的历史隔离快照：**115 项通过**，模块 statement + branch 覆盖率 **98.49%**，excluded 为 **0**。该次运行环境使用 pytest **8.3.4**、pytest-asyncio **1.0.0**、pytest-cov **6.2.1**；仓库 `requirements.txt` 固定为 **8.3.3**、**0.24.0**、**6.0.0**。在合成 `quota-test` 版本上的 v6 隔离验收已完成：**115 项通过**，模块 statement + branch 覆盖率 **98.49%**，excluded 为 **0**；证据目录为 `/tmp/od204-source-grant-v6-20261029-cc4836/evidence`。v6 的测试运行环境版本应以该证据包记录为准，不沿用 v5 的版本信息。完整 canonical A2 门禁未运行。以上均为隔离测试证据；没有创建、运行或接入生产 source grant，也没有执行真实 Fuyao 采集。

本说明依据 `opendata/data/source_grant_provisioning.py` SHA-256 `a18bc6fd5bb4f786bd0195167727654c032709e68246b0b177a7d6b48980dfac` 编写。文档更新本身未运行测试或 production provisioning。
