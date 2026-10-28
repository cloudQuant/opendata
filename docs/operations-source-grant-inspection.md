# 手动检查持久化数据源授权

`SourceAttemptGate.inspect_grant()` 是供运维人员核对**一个已持久化 source gate grant** 的只读检查方法。它是 Python 库 API，不会创建 HTTP 路由、定时任务或 Fuyao 采集流程。调用者必须明确选择已有的 SQLite gate 文件，并提供 grant ID、修订号及要核对的完整作用域。

## 调用方式

下面的值都是虚构示例。示例路径是占位路径，原样运行会在构造 `SourceAttemptGate` 时因文件不存在而报错；替换为真实路径后，调用会只读访问该 gate 库。运行时应从已审核的任务和授权记录中取得准确值。gate 文件必须解析到已经存在的普通本地 SQLite 文件，不能是内存库。`SourceAttemptGate` 不会替调用者选择数据库，也不会从应用设置或 `.env` 中寻找路径。

```python
from sqlalchemy import create_engine

from opendata.data.source_attempt_gate import (
    SourceAttemptGate,
    SourceGrantInspectionRequest,
)

engine = create_engine("sqlite:////absolute/path/to/source-gate.sqlite")
gate = SourceAttemptGate(engine)

request = SourceGrantInspectionRequest(
    grant_id="example-grant-001",
    revision=1,
    source="fuyao",
    canonical_model="financial_statement_annual",
    operation="query",
    principal_scope="example-service",
    host="api.example.invalid",  # 仅主机名；不含 scheme、端口或路径
)
result = gate.inspect_grant(request)
print(result.status.value, result.reason.value)
engine.dispose()
```

`operation` 只接受 `query`、`store`、`export`、`subscribe`。标识字段须符合有界标识格式；`host` 必须是精确 DNS 主机名，不能带通配符、协议、端口或路径。主机名会转为 IDNA ASCII、小写并去掉末尾句点。输入应与要检查的那一条授权记录及请求完全一致；不要把密钥、令牌或其他凭证放入请求字段。

## 如何解释结果

`MATCHED / grant_matched` 只表示：在这次 SQLite 只读快照中，指定 grant 的声明、修订号、数据源、模型、操作、主体范围和有效期与请求相符，且主机清单封存记录通过本地结构与审计事件一致性检查。授权记录中的证据引用和 SHA-256 仅作字段完整性检查；方法不会打开或核验引用文件，也不会据此判定合同内容或实际数据权利。

`REJECTED` 表示本次请求与当前 grant 不符或 grant 当前不可用，例如未找到、未允许、修订号或作用域不同、尚未生效、已过期或主机不匹配。

`INDETERMINATE` 表示无法安全得出匹配结论，例如数据库或 schema 不可用、时钟无效、grant 记录或封存证据无法核验、条件尚未解决。它不应被人工解释成允许；应先查明原因并重新进行受控检查。结果只返回固定状态和原因码，不包含数据库内容或底层诊断。

## 许可边界

- `MATCHED` **不是发送许可**，也不是合同、供应商授权或数据合规结论。它不读取合同正文，不核验配额余额，不检查 source policy 或其 pin，也不证明请求曾发出或供应商已收到请求。
- 该方法**不绑定 Fuyao composition**，不启动采集、worker、scheduler 或任何子流程。它不替代 composition 自己的输入校验、审批及运行控制。
- 每个实际 HTTP 物理尝试（包括每次重试）仍须在发送前单独调用 gate 的原子预留流程，并使用新的 attempt 身份。只有预留事务已确认提交且结果为 `RESERVED_NEW` 时，调用方才可继续发送；检查结果不能复用为预留。对于 v2 scope，应调用 `reserve_attempt_v2()` 并使用完整的 v2 作用域。
- `inspect_grant()` 要求 source gate 的完整 v2 schema 和独立迁移标记均有效。未迁移、迁移不完整或 schema 检查失败会返回 `INDETERMINATE / schema_unavailable`。任何 v2 预留也必须在迁移成功后执行。迁移是显式运维操作，不会随应用启动自动运行；请按[独立 source gate 迁移流程](migrations/source-attempt-gate-v2-standalone.md)备份并检查**准确的目标文件**后操作。

## SQLite 文件与运行边界

检查通过 SQLite URI `mode=ro`、`query_only=ON` 打开单个快照，不写 gate 业务行、不预留配额、不访问网络。不过 SQLite 可能按 journal/WAL 状态和文件系统条件管理锁或 WAL sidecar 文件；“只读检查”不承诺整个目录绝无文件系统副作用。部署目录应按 SQLite 的 WAL 访问要求配置权限；活动 WAL 数据库的备份应采用已审核的 SQLite 备份流程，不能只复制主数据库文件并假定快照完整。

构造 gate 时会先解析输入路径（跟随符号链接），要求解析后的目标是已存在的普通文件，并记录规范化后的主数据库路径和设备/ inode；只读连接打开后会再次复核。符号链接本身并不因其类型被拒绝，检查针对的是其解析目标；gate 保存解析后的路径，因此之后更改原始符号链接不会改变该实例绑定的目标。路径与文件身份检查只能发现常见的替换，不能固定操作系统文件句柄，也无法阻止同一用户在检查之间替换解析后的目标路径或文件再换回。它不是针对同用户进程或特权进程的防篡改边界；应通过本机账户、目录权限、备份与变更控制保护文件。该实现只承诺本地文件型 SQLite 行为，不提供跨主机锁或多主机一致性保证。
