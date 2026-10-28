# Fuyao 财报 Observation Capture Bridge 运维说明

本文说明 `opendata.pipeline.fuyao_observation_capture.prepare_fuyao_observation_capture` 的当前边界。它把**调用方已取得**的 `FinancialObservationFetchResult` 校验、归档为原始响应证据，并准备交给财报归档 store 的不可变 SQL DTO。它不是一个采集入口，也不代表数据已写入数据库或进入生产服务。

## 当前能力和调用顺序

调用方先独立取得一个 Fuyao 财报 fetch result，再向 bridge 显式传入与结果匹配的单位 profile、来源/留存声明、source-attempt 声明，以及绑定到明确本地证据目录的 `RawResponseArtifactWriter`。bridge 不读取凭证、不选择证券或报表、不调用 fetcher，也不读取环境变量。

在首次文件写入前，bridge 会重新校验 fetch result、profile、来源声明和 attempt DTO；检查请求摘要存在且是 SHA-256 格式；将响应正文重新解码，并比对正文哈希、来源请求 ID、观察行、隔离行、单位映射版本/摘要和可用性标签。它还检查证券代码形状、响应大小不超过 8 MiB，以及 `observed_at` 不晚于本机当前时钟。解码及上述输入校验通过后，才进入文件归档。

文件阶段先由 `RawResponseArtifactWriter` 保存 HTTPX 返回的 entity bytes 和 manifest。然后 bridge 用既有 `RawResponseArtifactReader` 重读并校验来源、请求摘要、HTTP 状态和正文，调用 `prepare_financial_observation_capture` 生成 `PreparedCapture`。成功返回的 `PreparedFuyaoObservationCapture` 含该 DTO、manifest 摘要和仅在当前进程内保留的 attempt 声明。这里的原始字节是 HTTPX 已缓冲并按内容编码解压的 entity bytes，不是压缩后的 wire bytes。

bridge 固定 `source_id` 为 `ths_fuyao`，原始来源标签限定为 `ths / ths_financial_statement / {statement_type}_{periodicity}`。这个来源三元组是本 bridge 的本地约定；仓库没有登记它为通用生产来源合同，已有测试夹具使用相同标签也不构成生产合同。依赖 manifest 来源查询的消费者必须认识这组标签，后续如要改名需安排兼容迁移。

## 声明与证据边界

bridge 对 `request_summary_sha256` 的存在和格式做检查，并将 fetch result 中的值原样交给 raw evidence。该摘要表达的是客户端侧的参数摘要声明；它不能独立证明请求确已发送、发送参数被供应商接收，或响应确由供应商生成。fetch result 可以带有本地 `attempt_trace`，但 trace 和来源 attempt DTO 都是可由调用方构造的 Python 值，bridge 不验证它们来自实际 transport 或 SQLite gate。

`FuyaoSourceAttemptDeclaration` 是调用方提供的 DTO。bridge 会检查 attempt 的声明字段是否符合 Fuyao scope，且 reservation 声明为新的成功预留、没有拒绝码或重试等待值、预留时间不晚于观察时间；但它无法证明该 attempt 与这份响应来自同一次物理发送，也不重新访问或验证 SQLite `SourceAttemptGate`。

trace 中的 `transport_request_id` 是客户端本地生成的 transport 请求标识，同一次 transport 调用的多次 attempt 共用它；`source_request_id` 是响应 envelope 中返回的上游标识。两者用途和命名空间不同，不应相等。bridge 只比较最后一条 trace 的 `source_request_id` 与 fetch result 的上游 `source_request_id`，不会将本地 `transport_request_id` 与上游 ID 对比。

- `attempt_trace` 为空时，bridge 保留旧行为并返回 `attempt_association_status="caller_declared_unverified"` 与 `source_request_id_match_status="not_checked_no_trace"`。空 trace 既不证明没有发送请求，也不证明没有终止失败。终止 transport 失败不会形成可供 bridge 归档的成功 `FinancialObservationFetchResult`；失败结果若没有 trace，不能由本 bridge 保存为成功捕获。
- `attempt_trace` 非空时，bridge 只比较最后一条本地 trace 与调用方 attempt/reservation 声明及成功 fetch result 的 `status_code`、上游来源请求 ID：trace 的 `http_status` 必须与 `status_code` 相同，`outcome` 必须为 `response_observed`，`failure_category` 必须为空。此处比较的是状态与 attempt 元数据，不是响应正文。相符时返回 `attempt_association_status="local_trace_matched_unverified"`。这只表示这些内存中的声明值彼此相符，不能证明 SQLite gate 作出了预留、trace 来自实际发送，或 Fuyao 收到/处理了请求。
- 若最后一条 trace 没有 `source_request_id`，返回 `source_request_id_match_status="unavailable_unverified"`；有值且与成功 fetch result 相同，则为 `matched_unverified`。未提供 trace 时为 `not_checked_no_trace`。这些状态均不构成独立来源回执。

trace tuple 在 fetch result 和 bridge 返回对象的 `repr` 中隐藏，attempt/reservation 声明中的字段也隐藏。trace 只在内存中保留；当前 SQL archive schema 不持久化 trace、gate 证明或来源请求 ID 匹配状态。不得把任何状态字段或声明作为独立调用审计证明。

单位 profile 的映射摘要会重算，并与 fetch result 的报表、版本、摘要及 `operator_declared` 类型核对。这只能检测声明不一致，不能证明逐科目单位已经独立验真。profile 的证据引用不会写入当前归档 schema；归档保留的是单位映射版本和摘要。`retention_class` 与 `source_history_limit` 同样是调用方声明，不构成数据授权、来源保留能力或历史覆盖证明。

## 文件写入、SQL 提交和恢复

文件归档先于 SQL 事务。bridge 不创建数据库 engine、不打开 SQL 连接、不启动或提交事务；它只返回供调用方在其自有事务中使用的 DTO。调用方可在持有有效运行租约/围栏状态时调用 `persist_prepared_financial_observation_capture` 并自行提交，同时必须保证被引用的 raw files 在数据库提交前持续存在。

文件写入与 SQL 提交不是一个原子操作。如果 writer 已写入对象和 manifest，而后续 reader、store preparation 或调用方数据库事务失败，磁盘上可能留下没有已提交 SQL 记录关联的文件。bridge 不会自动清理此类孤儿；在建立可审计的孤儿核对和保留策略前，应保留文件并由调用方记录失败阶段，不要按文件年龄直接删除。

同一正文的对象内容寻址可复用对象文件。一次新的 bridge 调用通常会生成新的 manifest 摘要，但 manifest 身份由规范化内容决定；若捕获时间和其他 manifest 字段相同，摘要也可能相同，因此不能保证每次调用都产生不同身份，整体流程也不保证幂等。SQL 层的相同 capture 重放只适用于调用方复用同一个 `PreparedCapture`/capture ID。事务失败后，若调用方仍持有该对象，应在确认事务状态后复用它；重新调用 bridge 会重新执行准备流程。进程重启后，manifest 本身不足以安全重建原始的完整准备上下文；当前没有持久化运行记录或自动恢复流程。bridge 不发起网络重试，也不会重新请求供应商。

## 未接入和未验收的能力

此 bridge 未注册到 Provider registry，未提供 REST 路由、调度任务、默认启用开关或全市场枚举。它每次只准备一份既有抓取结果，不证明沪深北全 A 股范围、上市以来历史、头部/内部/尾部缺口已补齐，也不生成数据覆盖结论。

现有归档策略为 `current_as_observed_only`；没有归档的历史披露版本不能由当前观察值恢复。公开契约尚未解决季度利润表和现金流数据是单季值还是年初至今累计值，披露日期字段也不提供盘中精确披露时间或首发/修订区分。不得由 bridge 的成功返回推断 PIT 可回放、季度语义已确认或来源具备完整历史。

## 验收记录

### 当前本地 trace 变更

当前 trace 对齐变更的隔离合成验收记录为 **47 passed**。归档报告显示目标模块有 **157 statements、2 missed；52 branches、2 partial；statement 与 branch 综合覆盖率为 98%**。

`var/reports/afi204/2026-10-28-fuyao-bridge-local-trace/`

该结果是 branch-inclusive 综合覆盖率；不表示语句覆盖率和分支覆盖率分别都是 98%。本地集成测试 r4 是最终格式化轮次，**1 passed**；r3 是同一个测试用例的前一轮运行，也 **1 passed**，不代表另有一个测试用例。测试使用临时 SQLite 上的实际 `SourceAttemptGate`，经 `MockTransport`、fetcher 到 bridge 验证本地调用链。它没有发起真实 HTTP 或第三方请求，也没有使用生产数据库或证明生产验收。前两轮 r1、r2 各有 **1 failed**，具体原因是测试配置 `source_attempts=4` 超过上限 3；这些失败轮次不计入通过数。四轮日志均已归档在上述证据目录。

### PR #53 的历史验收记录

以下保留 PR #53 对先前 bridge 版本记录的**历史隔离合成验收**，不代表本次 trace 变更的当前结果：

| 检查 | 最终结果 | 范围 |
| --- | --- | --- |
| 合成离线回归 | 32 passed | 仅 `tests/test_fuyao_observation_capture.py`；使用临时目录中的合成报文，无供应商请求，不执行归档 SQL |
| 目标模块 statement + branch coverage | 100%（136 statements，0 missed；42 branches，0 partial） | 仅 `opendata/pipeline/fuyao_observation_capture.py` |

当时的测试在隔离环境运行，网络命名空间关闭，工作目录不含项目 `.env`；结果不代表真实数据库提交、供应商联调或生产验收。该轮 pytest 和 coverage 输出，以及此前未通过的迭代记录，保存在本机私有证据目录：

`var/reports/afi204/2026-10-28-fuyao-capture-bridge/`

该历史开发期间的中间轮次曾出现 **11 passed、目标覆盖率 76%**，以及 **31 passed、1 failed**。这些失败证据与该历史最终通过日志一并保留。本文没有把中间失败轮次计作通过。

上述验收不证明来源数据真实、授权有效、单位口径正确、请求与响应血缘独立可信、SQL 可恢复提交、调度可用、历史完整或 PIT 可用。启用生产链路前，仍需实现并独立验收调用方事务及崩溃恢复、文件/SQL 孤儿对账、持久 attempt 血缘、API 与调度接线、权限和保留策略、单位与季度语义，以及按全 A 股证券和报告期逐项计算的历史覆盖。
