# CSI500 Fuyao 单次采样 Gate 设计

状态：离线技术设计，尚未实现、测试或启用。本文只定义单个受控 SQLite source-gate 文件内的发送上限；它不证明数据供应商授权，也不证明上游已收到请求。

## 目标与边界

CSI500 样本拟使用一条 THS/Fuyao 历史行情请求，最多发起一次受 gate 管理的 HTTP 请求。持久上限必须独立于 token bucket：等待 refill、追加 grant、更换 attempt ID 或重启进程都不能重新取得同一采样任务的发送许可。

保证范围是同一持久 SQLite gate 文件、同一固定 one-shot 配置和通过受控 THS HTTP client 的请求。绕过 gate 的网络调用、另建 gate 文件、恢复发送前的备份、直接修改 SQLite 文件或主机上其他独立进程发出的请求不在此保证范围内。需要跨进程、跨文件或跨主机的全局上限时，应改用唯一的中心 gate 服务和统一出站入口。

“最多一次”不是“恰好一次”。进程可能在提交发送许可后、真正发送前崩溃，也可能在上游收到请求后没有拿到响应。无法证明结果时必须牺牲自动恢复，将本次采样标为结果未知并停止重试。只有供应商提供且本端到端验证过幂等键语义后，才可重新讨论安全重试。

## 迁移与锁定顺序

1. 先完成并审阅 source-attempt scope V3，按 `sg0001 → sg0002 → sg0003` 顺序迁移，并让 runtime schema validator 精确识别 V3。V3 用不可变 sidecar 记录 rights reference、method、host、path 和请求摘要；它本身**不**提供单次计数。
2. 在此基础上增加 one-shot migration（建议 `sg0004`），建立不可变的 one-shot 配置与单次消耗账本，并将其 DDL、索引、触发器纳入 schema drift 校验。迁移只扩展 schema，不回填历史 reservation。若无法确认当前精确 lineage，必须拒绝配置。
3. 准备一个专用、持久且权限收紧的 SQLite gate 文件。所有拥有该文件写权限的进程都必须可信。文件只接受一个已密封的精确 THS `index_daily/query` grant。先确定本次业务日期、CSI500 标的、Fuyao HTTPS endpoint、允许的查询字段、rights reference 和 quota policy pins；校验这些内容后，才在一个事务中写入 quota、source binding 和 one-shot 配置。配置行提交后即为**锁定点**，不可更新、撤销后重开或删除。
4. 锁定配置必须固定唯一的样本运行 ID 和 `capture_intent_id`，并固定 grant ID/revision、principal、rights evidence reference、source/model/operation、host、method/path、业务日期对应的 query digest，以及 budget binding/policy revision/hash。调用方不能在重试或重启后生成新的 intent 来绕过这条配置。
5. 计划新增 `BEFORE INSERT ON od_source_grants` 触发器：one-shot 配置锁定后拒绝任何新 grant。配置后如需更换样本日期、grant、路由或 policy，应终止并归档这个 gate，按新审批创建新的采样任务；不能把新任务伪装成旧任务的 retry。

本方案要求 gate 文件的所有写入者都是可信进程。SQLite 触发器用于阻止受控 API 或旧版程序意外走错路径；它们不能抵御拥有相同文件写权限的进程直接执行 SQL、删除/替换数据库或还原旧备份。

目前 CSI quota provisioner 仍按 V2 fresh-gate 契约检查 schema 和 V2 sidecar。实现前必须将 provisioner 与 V3 lineage 对齐，并确认 V3 sidecar 表无历史行；不能在 V2-only 契约上配置 V3 样本。

## 持久单次上限

实现应新增 one-shot 固定配置表和单次 permit 表。配置表用 `config_id = 1` 的 CHECK 约束及唯一主键保证至多一个配置，并对样本运行 ID 和 `capture_intent_id` 加唯一约束；`BEFORE UPDATE`、`BEFORE DELETE` 触发器必须拒绝修改或删除已锁定配置，避免正常程序路径删除后重新锁定。permit 以配置 ID 为唯一主键，并对 attempt ID、capture intent 加唯一约束；需有 `BEFORE UPDATE` 和 `BEFORE DELETE` 触发器拒绝修改或删除 permit。token bucket 继续负责速率限制，但不参与“最多一次”的证明。grant 有效期短于 refill 周期只能作为附加限制，不能代替消耗账本。

实现应在 one-shot 配置锁定后增加以下校验：

- `BEFORE INSERT ON od_source_attempt_reservations`：当新行的 `outcome = 'RESERVED'` 时，必须存在本事务创建的 permit；permit 必须匹配锁定配置 ID、固定样本/运行 ID、attempt ID、grant、source/model/operation、principal、host 和 scope digest。不存在匹配 permit 就中止插入。
- `BEFORE INSERT ON od_source_attempt_scope_v3`：必须找到同一个 attempt ID 的 permit，且 `NEW.scope_sha256` 与 permit 中记录的 scope digest 完全相同。否则中止 sidecar 插入。
- `BEFORE INSERT ON od_source_grants`：锁定配置存在时拒绝新增 grant。permit 唯一键和 append-only 触发器阻止新 attempt、grant 或 refill 获得第二个许可。

V1/V2 gate 入口在发现 one-shot 配置时应显式拒绝。reservation 触发器再阻止旧版 API 为 `RESERVED` 请求取得许可；这只对仍访问同一数据库的程序有效。V3 调用必须先将完整 scope 与锁定配置比较，再在同一事务创建唯一 permit、reservation 和 sidecar。每个精确 V3 reservation 都消耗 permit，即使 grant/quota 决策最终为 `DENIED`；该结果不发送且不能通过重试复用这次机会。只有新提交且决策为 `RESERVED_NEW` 时才调用 transport。

事务内按以下顺序处理：获取 SQLite `BEGIN IMMEDIATE` 写锁；验证 gate identity、完整 schema、one-shot 配置、V3 scope、固定 grant/rights reference 和 quota pins；确认没有已消耗 permit；插入唯一 permit；评估 quota 并持久化 reservation、V3 sidecar 及必要的 bucket 变更；提交事务。任一步失败则整体回滚，不能留下“permit 已花但 reservation 不存在”或相反的半成品。并发竞争由 SQLite 写锁和唯一键共同序列化；只有赢得唯一消耗记录的调用能取得新发送许可。

Permit 一旦提交，即使进程在调用 `send` 前崩溃也保持消耗。该设计会出现零次实际请求，但不会为了恢复可用性而再次发送。若发送决策被 gate 拒绝，则不调用 HTTP client；不得把 gate 的 `RESERVED` 记录描述为供应商回执。

## 提交不确定与传输重试

Gate 只有在 commit 明确确认后才可返回发送许可。commit 报错、连接断开或提交状态无法确认时，调用方不得调用 `send`，不得盲目重跑 provisioning/reservation；该 intent 标记为终止或状态未知，先由受控离线流程检查持久状态。

CSI one-shot transport 的 `max_attempts` 必须固定为 1，关闭自动退避重试、HTTPX 隐式重试、redirect、proxy 和额外发送路径。当前 THS client 默认允许多次 retry；在 one-shot 模式锁定后须通过显式模式拒绝重试，而不能只依赖 token bucket 令后续 attempt 失败。

reservation commit 后任何 send 期间的 timeout、连接中断、响应体读取失败或进程崩溃，都不能证明上游没有处理请求。将该采样作为结果未知且 permit 已消耗处理；不再次请求，不更换 attempt ID，不追加 grant，不等待 refill。可在本地追加不可变的 send-start/response-observed 事件改善审计，但这些事件仍不能证明对方收到或处理了请求。

## 备份、恢复与运维

Gate 文件必须持久保存并限制写权限。恢复到尚未消耗 permit 的旧备份会回滚一次性账本，从而使同一个外部请求可能再次发送；SQLite 内部无法识别这种回滚。因此 one-shot gate 不能通过旧快照恢复后继续使用。恢复、复制或 inode/文件身份变化后，应将原采样任务标记为可能重放，隔离该文件并人工处置；重新创建的 gate 是新的授权任务，不能声称仍受原 gate 的全局一次性保护。

本机制防范正常并发、旧版 gate API、程序 retry 和进程重启；它不是抵御有权篡改数据库文件的本机管理员的安全边界。若文件被直接编辑、替换或从旧备份还原，必须停止自动发送并重新审核 gate 状态。

## 离线验收矩阵

| 场景 | 验收结果 |
| --- | --- |
| V2 lineage 上尝试配置 one-shot | 拒绝；先迁移到完整 V3，不写 quota/config |
| 精确 V3 scope、grant 和 quota pin 完全匹配 | 单个事务提交一条 permit、reservation 和 sidecar，返回一次 `RESERVED_NEW` |
| 同一 intent 使用新 attempt ID/request ID/attempt number 再申请 | 无第二个 `RESERVED_NEW`；transport send 计数不增加 |
| 修改 intent、run ID、日期、标的、host、path、query digest、rights reference 或 policy pin | 锁定配置校验失败；不创建 permit、不发送 |
| 锁定后追加 grant 或等待 bucket refill | grant 插入被拒绝；即使 refill，已消耗的唯一 permit 仍拒绝第二次发送 |
| one-shot 模式调用 V1 或 V2 reservation 路径 | 不能返回发送许可；数据库触发器也拒绝无匹配 permit 的 `RESERVED` 行 |
| 两个进程同时申请相同样本 | SQLite 写锁与唯一键保证最多一个 permit；至多一个 transport send |
| 权限/配额拒绝或 scope 不符 | 不调用 transport；错误不转成成功样本 |
| permit/reservation 提交前出现错误且 rollback 确认 | 不发送；无部分 one-shot 状态 |
| commit 状态不确定 | 不发送、不自动重试；状态标记需人工离线核实 |
| commit 已确认后 send timeout/断连/响应读取失败 | client 最多调用一次 send；permit 保持消耗，运行结果未知，不 retry |
| 重启后重新运行相同 intent | 已消耗时拒绝第二次许可；不得用新 ID 规避 |
| 恢复发送前备份或换用新 gate 文件 | 记录为超出原 gate 保证范围；不得声称已实现跨备份/跨文件的全局上限 |
| 非 one-shot gate 使用 V1/V2/V3 | 原有行为保持；one-shot 限制仅在锁定配置存在时生效 |

验收需使用全新的临时 SQLite 文件、合成 grant 与 mock transport，并由仓库负责人执行隔离测试。通过离线验收只证明本地 gate/transport 契约，不证明真实供应商权限、线上网络恰好一次送达或全机其他进程的请求总量。
