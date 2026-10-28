# 财报 PreparedCapture 的 Runner Sidecar

本文描述 `RunService`、`financial_prepared_codec` 与 `financial_observation_store` 当前提供的窄接口。它们为已审阅任务提供一个有界、仅进程内传递的提交 sidecar；本次已验收交付范围不包括已启用或生产注册的财报 job，独立 job 实现仍未验收。

## 当前数据路径

`ReviewedJob` 可选择以下 sidecar 契约之一：

- **Validator-only（既有模式）**：声明精确的 `bytes` 类型、显式同步 validator 和每任务最大字节数。每次运行都必须返回载荷；Runner 在 SQL 提交事务前调用 validator，并要求它返回字面值 `True`。此模式仍按原方式工作。
- **PreparedCapture decoder（新增模式）**：声明精确的 `bytes` 类型、每任务最大字节数、固定的 `decode_prepared_capture` 函数和精确的 `PreparedCapture` 输出类型。注册时 Runner 按对象身份校验这个 decoder 与输出类型，因此任务不能注册任意 decoder。每次运行都必须返回载荷；Runner 在调用 ledger 的 `commit_result`、开启 SQL 提交事务之前解码，并再次核对返回对象的精确类型。

两种 sidecar 契约互斥，且声明时必须完整；缺项或混用都会在注册时被拒绝。未声明 sidecar 的既有任务仍可返回 `None`，只有返回非 `None` 载荷时才会被拒绝。

Runner 收到 normalize 结果后，要求结果类型是精确的 `NormalizedBatch`，并拒绝 normalizer 预先填入 Runner 所有的 `commit_context`。随后它检查 sidecar 类型和大小，再按声明执行 validator 或 decoder，最后进行批次结果校验；这些步骤都在调用 ledger 的 `commit_result`、进入发布回调和 SQL 提交事务之前完成。Validator 抛异常、返回 `False` 或任何非字面值 `True` 都会拒绝本次运行。Decoder 模式解码成功后，Runner 交给 publisher 的批次会清空 `commit_payload`（publisher 所见批次不再引用原始 bytes），仅在进程内的 `commit_context` 中携带解码后的 `PreparedCapture`。这不代表 `RunService` 的局部变量等其他引用已释放，也不保证原始 bytes 的内存已被回收。sidecar 字段都不会进入 run 请求快照或结果 ledger，也不会跨进程持久化。

`opendata.pipeline.financial_prepared_codec` 为归档存储的不可变 `PreparedCapture` DTO 提供版本化 JSON 编解码。编码和解码都要求精确的 `PreparedCapture` 类型，并重新调用归档存储已有的完整 DTO 校验。Codec 不访问供应商、文件系统或数据库，也不创建来源证明或执行认证。这个 runner decoder seam 只是固定 DTO 的进程内传递方式；它本身不代表任务已接入真实供应商。

## 有界格式与顺序

Sidecar 的 runner 上限是每任务 16 MiB；codec 对编码结果和输入载荷也使用 16 MiB 上限。它只限制序列化 payload 字节数，不是进程 RSS、峰值内存或总内存上限：编码、UTF-8 转换、解码及 JSON 对象构建期间会有额外副本和开销，且调用方或 Runner 仍可能持有原始 bytes 引用。较大的 capture 会被拒绝，不会自动拆批。codec 还限制 JSON 嵌套深度为 8、单个容器条目数为 2,048、JSON 总条目数为 140,000，并拒绝重复对象键、浮点数和非标准 JSON 常量。

归档 DTO 当前还受存储层行数预算约束：每个 capture 最多 64 条源财报行，每行最多 16 个 item code（因此最多 1,024 条 observation 和对应 link），隔离行最多 64 条；SQL 写语句预算也会随这些数量校验。SQL 字段还各自有类型、长度和数值范围限制，文本字段上限最高为 65,536 个字符，具体列可能更小。Runner 普通结果另有独立的 50,000 条记录及 16 MiB JSON 上限；这些限额不扩大 sidecar 或存储 DTO 的容量。

Decoder 在 SQL 提交事务开启前运行，但存储函数 `persist_prepared_financial_observation_capture` 仍会在调用方事务内对 DTO 做一次有界的 Python 校验与 materialize：它解冻标量行、构建有界的字典/列表结构并重新核对行数与 SQL 语句预算，然后才执行 SQL。这段事务内工作不访问文件、时钟、JSON、供应商、网络或引擎，也不提交事务；但它包含有界 CPU 和内存操作，因此不能描述为“纯 SQL”或“零 CPU”。

codec 使用紧凑 UTF-8 JSON、固定 envelope、排序后的对象键和显式标量标签。数组和行列二元组按 DTO 中的顺序编码与还原，codec 不会重新排序它们。现有归档准备逻辑会按自己的规则整理 observation、link group 和 quarantine 行；这属于 DTO 的准备阶段行为。

## 内容与兼容边界

Sidecar 是原样字节通道，不经过普通结果 JSON 的字段检查。codec 保留字符串原文，不扫描任意文本中的 API key、密码或其他凭证，也不做脱敏。因此，调用方不得把凭证或不应进入归档的数据放进 DTO 字符串；不能把 codec 校验当成凭证扫描或内容审核。

Runner 现在拒绝 `NormalizedBatch` 子类，因为它检查的是精确类型。这会影响依赖子类扩展该对象的仓外集成；此类调用方需要返回标准 `NormalizedBatch` 并通过已声明 sidecar 传递附加载荷，或在采用前另行处理兼容性。

## 文件、SQL 与故障恢复

原始响应文件和数据库事务彼此不具备原子性。文件发布或回读成功不代表 SQL 已提交，SQL 回滚也不会撤销原始文件。当前 runner sidecar 和 `PreparedCapture` 只在本次进程执行中存在；没有持久化 capture intent、跨进程恢复队列或崩溃后的自动对账。因此，进程在文件发布之后、SQL 结果明确之前崩溃时，状态可能需要人工核对。Decoder 提前到事务前运行并未改变此故障边界。不要仅凭原始文件存在、ledger 状态或行数推断两侧一致，也不要自动删除可能成为孤儿的原始证据。现有两阶段存储调用和故障边界见[财报归档调用说明](operations-financial-observation-capture-service.md)。

## 验收范围

本次已验收交付范围不包括已启用或生产注册的财报 job；独立 job 实现仍未验收。因此这项能力仍是 runner seam、纯数据 codec 和存储函数的组合，不能视作供应商集成、生产部署或运行验收。

隔离测试日志 `/tmp/opendata-devhead-run-service-suite-b1krpykq/pytest.stdout.log` 记录 151 个测试全部通过；同一运行的 coverage artifact `/tmp/opendata-devhead-run-service-suite-b1krpykq/work/coverage/coverage.json` 中，`run_service.py` 的 statement+branch 合并覆盖率为 91.37%（562/599 statements 与 232/270 branch arcs 合并计算）。这些测试使用纯合成输入和本地测试替身/回调；它们不包含真实财报任务、真实数据库、生产运行、上游来源认证、真实供应商响应或崩溃后的恢复验收。覆盖率仅说明该隔离套件对 `run_service.py` 代码路径的覆盖情况，不证明业务数据正确、历史完整或 PIT（point-in-time）可重建。本次文档变更没有重新运行测试。
