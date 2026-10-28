# Fuyao 财务采集的持久化意图与崩溃恢复设计

**状态：设计草案，尚未实现，尚未验收。** 本文记录下一阶段建议，不代表当前服务已经具有崩溃恢复、自动重放、生产调度或数据完整性能力。本文没有更改生产配置或数据。

## 目标与边界

目标是在进程或主机于“原始响应已写入本地文件”之后、财务观察事实与 RunLedger 成功状态提交之前退出时，能够依据持久证据判断该做什么。证据完整时复用已捕获响应或已准备的 PreparedCapture，不再请求上游；证据不足或相互冲突时停止发布并保留待审状态。

文件系统和 SQL 数据库没有共同事务。不能把对象文件、manifest、准备好的 capture、财务表行和 RunLedger 状态包装成一个原子提交，也不能对上游 HTTP 请求承诺 exactly-once。设计采用可恢复的分步状态机：提前写数据库意图；原始文件按内容寻址、不可覆盖地发布；把已验证的 PreparedCapture 编码持久化；最后在一个 SQL 事务中同时写财务事实、意图终态和 RunLedger 成功状态。每一步都能被重试或明确隔离。

本设计不证明 Fuyao 的来源真实性、数据授权、单位语义、历史完整性、PIT 可还原性或供应商是否处理了请求。授权和单位声明必须继续标记为未验证；历史覆盖必须由独立的股票生命周期与逐期证据评估。

## 当前实现与恢复缺口

当前 disabled Fuyao job 在子进程内抓取响应、先写 raw evidence，再生成 PreparedCapture codec 并通过有界进程结果返回。父进程解码和校验后，在 RunLedger 的 fenced callback 中写 archive facts；该事实写入和 RunLedger 成功状态处于同一数据库事务。实现没有持久 capture intent 或 crash recovery，相关限制已写在 opendata/pipeline/fuyao_financial_run_job.py 和 opendata/pipeline/fuyao_observation_capture.py。

RunLedger 的 fencing token 会在每次 claim 时递增，过期的 running run 可以由新 worker 接手；当前只有 commit_result 等 fenced 操作，没有 intent 的分段持久化接口。若 worker 在 raw 写入后丢失 lease，旧 worker 的 SQL commit 会被拒绝，但 raw 文件仍可能留下；新 worker 目前无法从数据库知道这些文件属于哪次 run。

RawResponseManifest v1 记录 source、request-summary SHA-256、captured_at、HTTP 状态、响应体 SHA-256 和字节数，以及调用方声明的 retention/history 标签。它没有 capture intent ID、source attempt ID、观察时间、单位配置 pin 或 run snapshot 关联。PreparedCapture codec v1 对存储 DTO 做有界、规范 JSON 编码，但不包含外层运行意图，也不会扫描或脱敏 DTO 字符串。Fuyao 进程路径当前把 codec payload 限制在 5 MiB。

SourceAttemptGate 的持久记录证明本地 gate 曾预留或拒绝一次 attempt；它不证明请求确实发送、响应来自供应商或供应商已处理请求。其当前实现只支持本地 file-backed SQLite，多主机锁与 HTTP 端到端关联尚未验证。当前 attempt trace 与 caller declaration 的匹配明确属于 local_trace_matched_unverified。不能把它提升为来源认证。

此外，当前 canonical RunIdentity 包含 job 与 business request，但不包含 deployment bundle、source policy 或单位映射 pin。相同 run_key 不能默默套用不同配置；启动采集前必须保存并比较完整、非秘密的配置指纹。

## 建议的数据模型

先在 warehouse 数据库增加一条新的 alembic_data 迁移。当前版本目录截至 0018；实施前由集成负责人重新核对迁移编号和分支头。迁移只新增结构，不回填历史数据。

### od_financial_capture_intents

每个逻辑采集槽一行，以 run_key 与 slot_key 唯一。单股票试点的 slot_key 可由 instrument、statement、periodicity、请求窗口和 schema 版本的规范摘要组成；未来 fan-out 时仍使用稳定的逐证券槽位。

字段建议：

| 字段 | 用途 |
| --- | --- |
| capture_intent_id | 随机、不含业务含义的 ID，关联 filesystem manifest 和 attempt |
| run_key、slot_key | 关联不可变 RunLedger 请求和逻辑采集槽 |
| snapshot_sha256、request_summary_sha256 | 固定请求范围与实际声明的请求摘要 |
| config_fingerprint_sha256 | bundle、source policy、单位映射、job/decoder、store schema、raw store 逻辑 ID 等非秘密 pin 的规范摘要 |
| bundle_sha256、lockfile_sha256、runtime_digest、source_policy_sha256/revision | 固定不可变发布制品、依赖锁文件、实际运行时镜像/环境 digest 和 source policy；制品必须可按 digest 找回，grant ID 仅作受控关联字段 |
| unit_mapping_version、unit_mapping_sha256 | 保留操作员声明的精确版本与摘要，不表示单位已独立验证 |
| provider/source/dataset/operation、instrument/statement/periodicity/window | 限制恢复器能够接受的精确作用范围 |
| retention_class、source_history_limit | 原始声明；不能解释成法律许可或完整历史保证 |
| state、row_revision、active_fencing_token、worker | 实现 CAS 转换和当前 owner 记录 |
| active_attempt_id、selected_attempt_id | `active_attempt_id` 唯一指向当前允许推进的 attempt；`selected_attempt_id` 唯一指向已验证并选作发布输入的 attempt。两者可为空，但语义不同，不能从“最新 attempt”推导 |
| manifest_sha256、prepared_codec/version/SHA-256 | 指向已验证的 raw manifest 和 PreparedCapture 编码 |
| prepared_payload | 可选的有界 BLOB；若使用，沿用当前 5 MiB 运行上限，明确数据库包大小与备份开销 |
| published_capture_id、timestamps、fixed_error_code | 记录终态、重放结果和不含敏感内容的失败分类 |

不要保存 API key、Authorization header、完整 URL、原始响应体或任意异常文本。query 可以从不可变 RunLedger snapshot 还原；intent 仅重复必要的摘要和恢复 pin。

### od_financial_capture_attempts

每个物理请求单独一行，按 intent_id 与 attempt_no 唯一。至少保留预先分配的 attempt_id、request_id、对应 run_key/intent、分配时 fencing token、完整 gate 查询 scope、gate grant 与 policy revision、gate lookup 结果/reserved_at、observed_at_utc 及其客户端时钟声明、manifest/body digest 和 attempt 状态。attempt 至少使用 ATTEMPT_ALLOCATED、GATE_RESERVED、OUTCOME_UNKNOWN、SUPERSEDED_UNRESOLVED、RAW_COMMITTED、PREPARED、PUBLISHED、DENIED、NEEDS_REVIEW 等明确状态；可另存 `send_marker_at` 作为诊断信息，但它只能表示程序写过一个本地标记，不能证明网络请求已发送、供应商已收到或已处理。`SUPERSEDED_UNRESOLVED` 仍是可能迟到完成的未决状态，不得等同于已取消、已拒绝或已解决；只有完整迟到响应被持久验证后，attempt 才可转为 RAW_COMMITTED，但它仍作为候选证据保留，不会因此自动获选或被忽略。日志与表内错误都使用固定错误码。attempt 关联状态仍需使用“本地 trace 匹配，未验证”等准确标签。

`active_attempt_id` 和 `selected_attempt_id` 必须通过 intent 行的 `row_revision`、期望旧值与当前 fence 做 CAS 更新。一个 intent 最多只有一个 active attempt；attempt 创建/切换不得覆盖历史 attempt 记录。旧 active attempt 只有在 DENIED、明确完成，或经审计的人工作出明确风险决策后，才能将其从 OUTCOME_UNKNOWN 转为 SUPERSEDED_UNRESOLVED 并切换到新的 active attempt。该状态仍须计入“可能迟到完成”的未决集合，保存固定 reason、授权审计引用及人工接受重复请求/冲突风险的记录；它不表示旧 HTTP 请求已取消或不会返回。普通 lease 接管仅更新 owner/fence，不得自动创建新 attempt 或更换 active_attempt_id。attempt 已迟到完成时只能在它自己的 attempt 行和独立 raw 地址登记；旧 fence 不能改 intent 指针。

selected_attempt_id 不要求等于 active_attempt_id：前者表示发布所选证据，后者表示当前获准推进的工作 attempt。只有一个候选 attempt 的 raw manifest/body、配置 pins 与 PreparedCapture 全部验证通过，且不存在其他仍可能迟到完成的 OUTCOME_UNKNOWN 或 SUPERSEDED_UNRESOLVED attempt 时，当前 fence 才能 CAS 设置 selected_attempt_id 并 stage codec；经过明确人工裁决的旧 attempt 也可以被选中，但必须记录审计理由。授权重试不会把旧 attempt 从未决集合中移除。发布事务必须再次确认 selected_attempt_id 与 payload 来源一致。已有 selected attempt 不得静默切换；如发现迟到数据或冲突候选，立即停止发布并进入人工审查，保留原 selection 供审计，通过显式审计决策和 CAS 清除/替换 selection。PUBLISHED 后 selection 不可改写。多个 attempt 均有可用候选，或同一 attempt 有多个候选 manifest 时，无论时间先后或 body hash 是否相同，都不得自动选择，必须人工审查并记录裁决。

SourceAttemptGate 位于独立数据库，未来实现必须提供按完整 scope 查询的权威、只读 lookup 接口，结果严格区分 RESERVED、DENIED、ABSENT、INDETERMINATE。scope 至少含 gate reservation/request ID、attempt_id、run_key、capture_intent_id、provider/source、operation、request 摘要、source-policy revision 和 gate grant/budget 维度。只能在确认查询到权威 gate 数据库、查询成功且完整 scope 精确匹配时返回 ABSENT；超时、数据库不可用、查询异常、部分 scope、重复或冲突记录、无法确认读到最新已提交状态，都必须返回 INDETERMINATE，绝不能把失败或空结果降级为 ABSENT。RESERVED/DENIED 的记录也必须验证 scope 完整匹配。该合同是未来需求，当前 file-backed SQLite gate 尚未提供已验证的多主机权威查询保证。

建议接口形状为 `lookup(scope: GateScope) -> GateLookupResult`，其中 `GateLookupResult` 是带固定字段的 tagged union：`RESERVED(reservation_id, scope_digest, policy_revision, reserved_at)`、`DENIED(scope_digest, policy_revision, reason_code)`、`ABSENT(scope_digest)` 或 `INDETERMINATE(reason_code)`。不把原始异常、凭证或响应正文塞入结果。gate 的 reservation 操作以同一个 attempt/request ID 幂等；若 reserve 调用超时，必须先用只读 lookup 对完整 scope 复核，RESERVED 才可继续，DENIED/INDETERMINATE 必须停止，权威 ABSENT 才可按相同 ID 重试 reservation。对 ledger 与 gate 的调用不得构成跨数据库事务；恢复完全依赖此 lookup 契约及其一致性验收。

PreparedCapture payload 是派生财务数据，必须采用受限数据库访问和明确的数据保留策略。若保留为 BLOB，最终成功提交时可以在同一事务中清除 payload、保留 codec SHA 和审计摘要；非终态证据在授权和保留规则明确前不得自动删除。数据库 BLOB 方案的大小、备份和恢复成本需经实测决定；若改用文件 sidecar，必须另外定义其原子发布、哈希寻址和恢复协议。

## 状态与提交协议

建议 intent 状态：PLANNED → ATTEMPT_ALLOCATED → GATE_RESERVED → OUTCOME_UNKNOWN → RAW_COMMITTED → PREPARED → PUBLISHED。每个物理 attempt 有自己的状态和证据关系；`SUPERSEDED_UNRESOLVED` 是 attempt 级状态，不是 intent 已解决的状态；授权重试只在 CAS 切换 active_attempt_id 后让 intent 继续处理新 attempt，不得把 intent 标为完成或消除旧 attempt 的风险。intent 另持有 active_attempt_id 与 selected_attempt_id；gate 明确拒绝时该 attempt 进入 DENIED，证据损坏、配置变更、gate 查询为 INDETERMINATE、缺少 pin 或恢复歧义时进入 NEEDS_REVIEW。终态不能静默回退。所有状态和指针更新都以期望旧状态/旧指针、row_revision 与当前 fence 做 CAS。`OUTCOME_UNKNOWN` 必须在调用 transport 前以当前 fence 持久提交；只有确认该转换成功的 worker 才能继续尝试网络调用。该状态表示从此刻起网络结果可能未知，不表示请求一定已发出。`send_marker_at` 不改变这个语义，也不得用于推断供应商侧事实。

1. **创建意图。** 在任何上游请求之前，通过 RunLedger 的短事务检查 run 仍为 running、worker/fencing token 匹配且 lease 尚未过期，插入或读取 intent。快照、请求范围、job/decoder 版本、不可变 bundle 与 lockfile、实际 runtime digest、policy/unit pins、raw store ID 和配置指纹都须一致；制品需能按 digest 找回。相同 run_key/slot_key 下任何不同值均为冲突，不得覆盖。
2. **分配与 gate 核验。** 先在 warehouse 中预分配唯一 attempt_id、request_id 和递增 attempt_no，写入完整 gate scope 并进入 ATTEMPT_ALLOCATED，再绑定当前 fence。随后在 warehouse 事务之外访问独立 gate 数据库。使用上文权威只读 lookup 解析已有决定；只有精确匹配的 RESERVED 才能推进，DENIED 禁止发送，权威 ABSENT 只允许对同一 attempt_id 执行 gate 的幂等 reservation 流程，INDETERMINATE 一律停止并待核查。任何 lookup/reserve 的结果都要在当前 fence 下落入 warehouse；不得把“gate 调用返回成功”当作请求已发送的证明。Transport 只能在数据库中已持久化精确匹配的 GATE_RESERVED 后，通过当前 fence 将状态 CAS 为 OUTCOME_UNKNOWN；只有 CAS 成功才可调用网络。若进程在 GATE_RESERVED 阶段崩溃，按该调用顺序它尚不具备发送资格；如果状态已是 OUTCOME_UNKNOWN，则绝不自动重发。
3. **发布 raw evidence。** 扩展 manifest 到兼容版本 v2，同时继续读取 v1。v2 增加 capture_intent_id、attempt_id/attempt_no、config fingerprint、bundle/lockfile/runtime digest、客户端接收完整响应体时的 observed_at_utc，以及客户端时钟来源/同步状态/估计不确定度声明；manifest 的规范字节继续由内容 SHA 寻址。observed_at_utc 是采集客户端完成接收响应体时读取的客户端时钟值，不是供应商生成/更新时间、公告时间、财务业务日期或可证明的 PIT 时间。记录时区标准化、时钟来源与可信边界；若无法量化偏差就记录 unknown，不得声称强时间准确性。不得用 captured_at、manifest 写入时间或供应商业务字段推导 observed_at。每个 attempt 使用独立、不可变、no-clobber 的发布地址，迟到的旧 attempt 只能追加到其自己的 attempt 关联下，不能覆盖 active attempt 或 intent 指针。先完整原子发布 body object，再 no-clobber 原子发布 manifest；manifest 是 filesystem raw capture 的可恢复索引点。恢复时必须再次用 RawResponseArtifactReader 验证 manifest 摘要、请求摘要、source、状态、大小和 body 摘要。
4. **持久化准备结果。** 通过现有 PreparedCapture codec 编码，验证编码 SHA、版本与字节上限，再以当前有效 fence 将 payload 或经审查的 sidecar 地址写入 intent，并在唯一候选验证通过时 CAS 设置 selected_attempt_id 后转为 PREPARED。selected attempt 可以与 active attempt 不同，但被选 attempt 的 manifest、所有 attempt/config pins 和 request scope 必须与 intent 精确匹配，且不能有尚可能迟到完成的冲突 attempt；人工裁决的旧 attempt 必须有审计记录。必须确认 PreparedCapture 中的 manifest、自然键、observed_at_utc、时钟声明、单位声明和请求范围与 intent 全部吻合。若需从 raw 重建，必须能够恢复并验证 manifest 指向的不可变 job/decoder 发布制品、依赖 lockfile 和运行时 digest；源码版本标签或当前 checkout 不足以证明相同解码语义。任何制品不可得、摘要不符或单位映射无法复原都进入 NEEDS_REVIEW。不要把 raw 文件读取、provider 请求、codec JSON 解码或 commit 放进 RunLedger 的 SQL callback。
5. **最终发布。** 最终 callback 从同一 warehouse connection 按 run→intent→attempt 锁序读取并锁定当前记录，检查 fence、状态、配置 pins、active_attempt_id、selected_attempt_id 与已 stage payload 的来源一致；重新校验 codec 后写财务 archive rows，将该 attempt 与 intent 改为 PUBLISHED，并由 RunLedger 在同一个事务中将 run 标记 SUCCEEDED。任何异常必须使 facts、attempt/intent 状态和 RunLedger 状态一起回滚。事务成功后不再触发 HTTP。

RunLedger 需要提供短时、fenced 的 intent transition API。所有可能同时触及 run 与 intent 的事务使用统一锁序：先锁定/验证 RunLedger run 行，再锁定/验证 capture intent 行，随后锁 attempt 行；任何路径都不得反向取得这些锁。每次数据库写都基于 warehouse 数据库时钟重新检查 running status、worker、fencing token 与 lease expiry；不要在 DB lock/transaction 中执行 gate 查询、网络或文件 I/O。当前 RunLedger 的多主机生产配置应使用 database clock。旧 owner 即使仍运行也不能改写已被新 fence 接管的 SQL intent。

## Lease 过期与恢复决策

新 owner 先 claim 过期 run，拿到递增 fence，再检查已有 intent。它只能用当前有效 fence CAS 接管状态；旧 worker 不能发布 SQL。Filesystem artifacts 不会因为 fence 过期就自动删除。

| 已发现状态 | 恢复动作 |
| --- | --- |
| PLANNED，且没有已分配 attempt | 对比 snapshot 与所有配置 pins 后，可以在当前 fence 下分配新 attempt |
| ATTEMPT_ALLOCATED，权威 gate lookup 为 ABSENT | 仅对同一 attempt ID 幂等地执行 gate reservation；持久记录并核对 RESERVED 后才可进入 GATE_RESERVED |
| ATTEMPT_ALLOCATED，权威 gate lookup 为 DENIED | 该 attempt 进入 DENIED；不发送网络请求 |
| gate lookup 为 INDETERMINATE，或 RESERVED/DENIED scope 不完整/不匹配 | 保持待核查/NEEDS_REVIEW；不能按 ABSENT 处理、不能发送，也不能更换 attempt ID 绕过未决预算 |
| GATE_RESERVED，尚未转为 OUTCOME_UNKNOWN | 在协议保证“只有 OUTCOME_UNKNOWN CAS 成功后才能调用 transport”的前提下，可由新 fence 接手并执行该转换；旧 owner 的转换失败后必须停止 |
| OUTCOME_UNKNOWN，没有可验证 manifest | 无法判断请求尚未发送、已发送、供应商已处理或响应丢失；禁止自动重发。send marker 不能消除不确定性 |
| 经审计的人工作出明确风险决策并授权重试 | 旧 attempt 转为 SUPERSEDED_UNRESOLVED 并保留在可能迟到完成集合；新请求使用新的 attempt ID 且重新经过 gate。该授权只允许启动新 attempt，不解决旧 attempt 的结果，也不允许自动忽略后来到达的旧证据 |
| SUPERSEDED_UNRESOLVED 收到迟到结果 | 按旧 attempt 独立保存和验证；不得自动更新 selection 或发布，即使它是当前唯一候选也须由审计的人工作出明确风险决策后处理。如新旧 attempt 都有候选或已存在 selection，则进入 NEEDS_REVIEW；不得因新 attempt 已成功而自动丢弃旧结果 |
| v2 manifest 可验证、无 codec | 仅在精确 intent metadata、客户端接收时刻及其时钟声明、job/decoder semantic version、bundle/lockfile/runtime/unit pins 都可得且匹配时，以已验证 body 确定性重建 DTO；否则 NEEDS_REVIEW |
| codec 已 stage，raw manifest/body 和所有 pins 匹配 | 当前 owner 可 CAS 接管并提交 PreparedCapture；不重新请求上游 |
| raw、manifest、codec 缺失/损坏，manifest 对应多个 intent/attempt 候选，attempt 或配置不匹配 | NEEDS_REVIEW/QUARANTINED；不发布，不猜测，不覆盖 |
| 旧 fence/旧 attempt 在 lease 接管后迟到写入 raw artifact | 只写入旧 attempt 的独立不可变地址并保留旧 attempt 关联；不得改变 active_attempt_id、selected_attempt_id 或覆盖既有 codec。由当前 fence 的 reconciler 单独验证。发现 selection 之后出现的竞争候选时，CAS 停止发布并置 NEEDS_REVIEW，保留既有指针供审计；多个可验证候选无法按固定业务键唯一确定时必须人工审查，禁止按时间、目录顺序或 body hash 猜选。PUBLISHED 后不得静默替换已发布选择，应转入独立审查/更正流程 |
| PUBLISHED 与 RunLedger SUCCEEDED 不一致 | 按数据库原子提交不变量视为严重损坏，停止自动处理并调查 |

对 OUTCOME_UNKNOWN 的重试必须先由审计的人工作出明确风险决策，说明接受重复请求、供应商副作用和迟到结果冲突的风险。授权后将旧 attempt 标记为 SUPERSEDED_UNRESOLVED（不得将其视为已解决），新 attempt 分配新的 attempt_no/attempt_id 并重新向 gate 预留预算。旧 attempt 在完整迟到响应被持久验证前始终计入可能迟到完成的未决集合；验证后虽可转为 RAW_COMMITTED，仍作为候选证据保留。在旧 attempt 未决或仍有竞争候选时，不得仅凭新 attempt 成功自动选择、发布或清理证据。任何选择或排除都要由审计的人工作出明确裁决并记录依据。该操作不是同一次 HTTP 请求的幂等重放。供应商若提供经确认的幂等请求机制，应另行设计和验收。

Lease fencing 保护 warehouse SQL，不会原子控制跨进程 HTTP send 或本地文件写。未来协议必须先持久化 OUTCOME_UNKNOWN，再允许当前 fence worker 调用 transport；该顺序把发送前崩溃也保守地纳入未知状态。需要在进程请求边界实现可观察的 lease heartbeat/取消，并在发送前再次核验当前 fence；旧进程使用每次 attempt 独立的不可变文件地址。仍须承认：在 CAS 与 HTTP send 间，fence 无法原子控制网络；状态只表示可能产生外部副作用，不能证明发送事实。旧 attempt 的迟到 raw 作为独立候选留存，当前 intent 仅能由新 fence 依据精确关联决定是否采用；同一 intent/attempt 若出现多个候选 manifest，或多个 attempt 的捕获均可能符合请求而业务规则不能唯一选择，均须人工审查。不要自动并发补发未知 attempt。对当前 file-backed SQLite SourceAttemptGate，不得宣称多主机协调已完成；多主机启用前应迁移为中心化、经过并发和权威 lookup 验收的 gate，或明确约束为单主机运行。

配置指纹不得包含 API key。它至少固定 job/decoder semantic version、请求摘要、不可变发布 bundle SHA、依赖 lockfile SHA、实际 runtime/image digest、source policy SHA/revision、unit mapping version/SHA、archive schema 与 raw store 逻辑 ID。恢复时必须能按这些 digest 找到并校验相同制品；仅有版本字符串、分支名或当前运行环境不能证明可重建。任何制品不可找回或 pin 漂移都停止复用：创建新配置下的新 run generation 或进入人工处理；不把新配置应用到旧 PreparedCapture。source policy 改变/撤销时，如何处理已抓取但未发布的内容必须由授权规则明确，不能由程序推定继续发布。

## 历史文件与数据治理

迁移前已经存在的 v1 orphan manifest 不包含稳定 intent/attempt 关联或 observed_at，也不能从 manifest 的 captured_at、request hash、目录时间或相同响应体推断 run。对它们只能产出只读 inventory，标记 legacy/unassociated；不得自动绑定到某个 RunLedger 行、重建 PreparedCapture 或作为完整性证据。已成功写入 archive 的数据以其 SQL capture references 为准。

新的对账工具初期应只读、默认不删除、不重新请求、不改数据库。输出应区分：manifest/对象完整性、intent 关联、codec 可解码性、配置 pin 匹配、RunLedger/fence 状态、需要人工处理的原因。原始 body 与 prepared payload 不进入 stdout、普通日志或报告。任何 GC 必须另有 retention/授权政策，并先依据 SQL references 和 intent 状态做两阶段隔离；在删除前保留恢复窗口。Raw evidence 的 retention/history 字段属于声明，不是授权证明。

## 分步交付与验收

### 第 1 步：冻结契约并加入 intent 持久层

定义迁移、严格状态机（含 ATTEMPT_ALLOCATED/GATE_RESERVED/OUTCOME_UNKNOWN）、唯一约束、RunLedger fenced CAS 接口、统一 run→intent→attempt 锁序和完整配置指纹。迁移只加表、无历史回填；runtime migration catalog 和迁移文档一起更新。验收覆盖重复建 intent、snapshot/config/artifact 冲突、并发 claim、lease 到期、新 fence 接管、旧 fence 写拒绝、锁序无死锁，以及 MySQL/SQLite 对约束和数据库时钟的行为。

### 第 2 步：raw manifest v2 与可发现索引

实现向后兼容的 v1 reader 和 v2 writer/reader；为 attempt/intent 关联提供确定性查找。测试 canonical bytes、SHA-256、重复/非法字段、客户端 receive observed_at 与时钟声明边界、路径权限、无 clobber、body 先于 manifest 的各类崩溃、dangling object、迟到旧 attempt、重复响应与同请求多 manifest 歧义。验证 active_attempt_id / selected_attempt_id 在并发 CAS、fence 接管、被授权 supersede、迟到完成、selection 后发现冲突、PUBLISHED 后发现冲突时的规则；特别验证授权重试后旧 attempt 仍保持 SUPERSEDED_UNRESOLVED、仍计入候选集合，旧 attempt 单独迟到或新旧结果都出现时均须人工风险裁决，且新 attempt 成功不能自动排除旧 attempt。任何存在多个候选的恢复决策必须转人工审查；v1 orphan 必须保持不可自动关联。

### 第 3 步：PreparedCapture staging 与最终原子发布

将 codec payload 在网络完成后、最终 facts commit 前持久化到 intent。实现只用 verified evidence 恢复 DTO，并将 facts、intent=PUBLISHED 与 RunLedger=SUCCEEDED 绑定到单个 final transaction。MySQL 接受需检查 BLOB/packet 上限、rollback 行为和并发 fence；不能只以 SQLite 单元测试替代。

### 第 4 步：reconciler 与受控运维

新增只读报告模式，核对原始 manifest、object、codec、run snapshot、artifact/lockfile/runtime pins、attempt 状态与独立 gate 数据库的权威只读 lookup；报告必须保留 RESERVED/DENIED/ABSENT/INDETERMINATE 的区分，lookup 错误不得映射成 ABSENT。按上表给出可解释动作。后续若加 apply 模式，默认仅可 stage/recover 已验证的 capture，不允许自动 resend unknown、自动删除 evidence 或绕过人工 hold。必须有审计日志、访问限制和数据保留规则。

### 第 5 步：隔离故障注入与端到端接受

在独立且随机命名的 disposable SQL schema / 独立 raw root 验收以下崩溃点：intent 前；attempt 分配后、gate 预留前；gate 预留后、send 前；send 后、body 写完前；body 完成后、manifest 发布前；manifest 后、codec stage 前；codec stage 后、final transaction 前；final transaction 中途 rollback；final commit 后、worker 返回前。每种情况下验证下一 owner 的决策、HTTP 请求计数、数据行数、RunLedger 状态、intent 状态和证据是否可复算。

还需覆盖 raw/manifest/codec 丢失与篡改、bundle/lockfile/runtime 制品缺失或 digest 漂移、单位声明变更、gate 的四种 lookup 结果及超时/数据库错误/陈旧读/部分 scope/冲突记录、异常不得泄漏 key、过期 lease 与两个 worker 竞态、迟到旧 attempt 与多个候选必须人工审查、重复 reconciler、成功提交后无重复网络请求。专门验证 send marker 存在/缺失均不改变 OUTCOME_UNKNOWN 的恢复决策；授权重试后的旧 attempt 仍可能迟到、不能自动从未决集合移除，且选择/排除迟到证据必须有审计的人工风险裁决。测试要包含真实记录的脱敏 Fuyao envelope 或官方示例，不能以手造“理想数据”替代 T1 合同测试。

新增/触碰代码必须满足 CODE_QUALITY.md 的 A2、Fuyao T1 和 coverage 门槛。集成负责人须先审查精确测试命令与 fixtures/services；数据库验收只能连接已确认隔离的实例与 disposable schema，保留失败证据。任何未跑、跳过或失败的步骤都要明确记录为未验收，不得以历史报告或静态通过代替。

### 并行开发边界

建议先由集成负责人冻结 intent/attempt DTO、状态转换表和迁移编号，再分配互斥文件域：一人负责 schema 与 fenced repository；一人负责 raw evidence v2；一人负责 Fuyao transport/process 的 intent/attempt 关联；一人负责只读 reconciler 与操作说明；集成负责人负责 run-service final transaction、故障注入契约和接受证据。共享迁移、状态机、配置指纹及发布 callback 只能由集成负责人串行整合。各分片应先合入开发分支并完成所声明的验收，再继续依赖它的分片。

## 当前可宣称的结论

该设计文档本身不是实现。当前 Fuyao capture bridge 和 disabled RunLedger job 仍没有持久 intent、manifest v2、staged codec、reconciler 或 restart recovery。现存 v1 orphan 的自动恢复仍不安全。生产注册、调度激活、全 A 股 fan-out、全历史覆盖、实际 retention 授权、多主机 gate 和生产性能接受均未包含在此方案的已完成范围内。
