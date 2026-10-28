# 财报 PreparedCapture 的 Runner Sidecar

本文描述 `RunService` 与 `financial_prepared_codec` 当前提供的窄接口。它们为已审阅任务预留了一个有界、仅进程内传递的提交 sidecar；目前还没有注册真实财报任务使用这条路径。

## 当前数据路径

`ReviewedJob` 可选择声明完整的 sidecar 契约：载荷类型必须是精确的 `bytes`，必须提供一个显式的同步 validator 函数，并设置每个任务的最大字节数。缺少任何一项都视为未注册。启用该契约后，每次运行都必须返回载荷；未声明 sidecar 的任务则必须返回 `None`。

Runner 收到 normalize 结果后，先要求结果类型是精确的 `NormalizedBatch`，再检查 sidecar 的精确类型和大小，随后执行结果校验以及注册的语义 validator。以上步骤发生在调用 ledger 的 `commit_result`、进入发布回调和 SQL 提交事务之前。validator 必须同步返回字面值 `True`；抛异常、返回 `False` 或其他值都会拒绝本次运行。Runner 不会把 sidecar 写入 run 请求快照或结果 ledger，也不会在任务间持久化它；它只随当前进程中的这次批次传给发布回调。

`opendata.pipeline.financial_prepared_codec` 为归档存储的不可变 `PreparedCapture` DTO 提供版本化 JSON 编解码。编码和解码都要求精确的 `PreparedCapture` 类型，并重新调用归档存储已有的完整 DTO 校验。codec 不访问供应商、文件系统或数据库，也不创建来源证明或执行认证。

## 有界格式与顺序

Sidecar 的 runner 上限是每任务 16 MiB；codec 对编码结果和输入载荷也使用 16 MiB 上限。它限制的是序列化 payload 字节数，不是进程峰值内存：编码、UTF-8 转换、解码及 JSON 对象构建期间会有额外副本和开销。较大的 capture 会被拒绝，不会自动拆批。codec 还限制 JSON 嵌套深度为 8、单个容器条目数为 2,048、JSON 总条目数为 140,000，并拒绝重复对象键、浮点数和非标准 JSON 常量。

归档 DTO 当前还受存储层行数预算约束：每个 capture 最多 64 条源财报行，每行最多 16 个 item code（因此最多 1,024 条 observation 和对应 link），隔离行最多 64 条；SQL 写语句预算也会随这些数量校验。SQL 字段还各自有类型、长度和数值范围限制，文本字段上限最高为 65,536 个字符，具体列可能更小。Runner 普通结果另有独立的 50,000 条记录及 16 MiB JSON 上限；这些限额不扩大 sidecar 或存储 DTO 的容量。

codec 使用紧凑 UTF-8 JSON、固定 envelope、排序后的对象键和显式标量标签。数组和行列二元组按 DTO 中的顺序编码与还原，codec 不会重新排序它们。现有归档准备逻辑会按自己的规则整理 observation、link group 和 quarantine 行；这属于 DTO 的准备阶段行为。

## 内容与兼容边界

Sidecar 是原样字节通道，不经过普通结果 JSON 的字段检查。codec 保留字符串原文，不扫描任意文本中的 API key、密码或其他凭证，也不做脱敏。因此，调用方不得把凭证或不应进入归档的数据放进 DTO 字符串；不能把 codec 校验当成凭证扫描或内容审核。

Runner 现在拒绝 `NormalizedBatch` 子类，因为它检查的是精确类型。这会影响依赖子类扩展该对象的仓外集成；此类调用方需要返回标准 `NormalizedBatch` 并通过已声明 sidecar 传递附加载荷，或在采用前另行处理兼容性。

## 文件、SQL 与故障恢复

原始响应文件和数据库事务彼此不具备原子性。文件发布或回读成功不代表 SQL 已提交，SQL 回滚也不会撤销原始文件。当前 runner sidecar 和 `PreparedCapture` 只在本次进程执行中存在；没有持久化 capture intent、跨进程恢复队列或崩溃后的自动对账。因此，进程在文件发布之后、SQL 结果明确之前崩溃时，状态可能需要人工核对。不要仅凭原始文件存在、ledger 状态或行数推断两侧一致，也不要自动删除可能成为孤儿的原始证据。现有两阶段存储调用和故障边界见[财报归档调用说明](operations-financial-observation-capture-service.md)。

## 验收范围

目前没有真实财报 `ReviewedJob` 将 fetch、decode、prepare、codec validator 和 SQL publisher 串成端到端任务，也没有为该路径启用财报定时任务。因此这项能力仍是通用 runner sidecar 与纯数据 codec，不能视作供应商集成、生产部署或运行验收。

此前隔离运行的 144 个纯合成测试记录了 `run_service` 的 statement+branch 综合覆盖率 91.59%，以及 `financial_prepared_codec` 的 statement+branch 综合覆盖率 98.89%；这两个数字分别对应模块，不是 statement 与 branch 两种覆盖率各自的读数。它们只覆盖相应的本地代码路径，不是数据库集成、真实供应商响应、真实财报任务、生产运行、历史完整性或 PIT（point-in-time）重建验收。本次文档变更没有重新运行这些测试。
