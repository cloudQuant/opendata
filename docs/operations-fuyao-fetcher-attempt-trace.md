# Fuyao fetcher attempt trace 运维说明

本文说明 `FinancialObservationFetchResult.attempt_trace` 如何把成功 transport response 中的本地尝试轨迹带到 Fuyao 财报 fetch result。该字段用于进程内关联受控请求与返回结果；它不是供应商回执，也不是独立认证的审计记录。

## 返回与兼容行为

成功的 `ThsFinancialObservationFetcher.fetch()` 会把 `FuyaoHttpResponse.attempt_trace` 复制到 `FinancialObservationFetchResult.attempt_trace`。轨迹包含 transport 本地生成的请求标识、attempt 标识与序号、门禁预留信息，以及客户端观察到的响应状态或失败结果。若一个逻辑请求经历可重试失败后成功，结果携带该次 transport 返回的轨迹序列。

该字段有默认值 `()`，因此旧代码按原有参数创建 `FinancialObservationFetchResult` 时仍兼容；旧调用方手工构造或重建的 DTO 也可能保留空 tuple。当前 `ThsFinancialObservationFetcher` 要求注入 `SourceAttemptGate`，此处的默认值是结果 DTO 的兼容行为，不表示 fetcher 支持绕过 gate 的成功请求。空轨迹不能证明没有发送请求。

结果构造时先执行原有字段校验，再校验新增轨迹字段，保留既有字段错误优先级。例如，若 `source_request_id`、`status_code`、正文或解码引用原本不合法，即使轨迹也有问题，仍先报告旧字段校验错误。轨迹校验要求它是不可变 tuple，最多包含三次同一 transport 请求中按序递增的本地 attempt；每条记录须符合本地预留与结果字段约束。

成功结果的 `status_code` 仍只接受 HTTP 200 或 201。非空轨迹的最后一项必须表示观察到了成功响应，且其 `http_status` 与结果的 `status_code` 一致；`failure_category` 必须为空。最后一项的 `source_request_id` 可以为空；若提供，则必须与结果中的 `source_request_id` 完全一致。结果中的来源请求标识仍须是非空、首尾无空格的字符串。它是响应侧标识，与 transport 本地的 `transport_request_id` 分开保存。

## 证据边界与失败路径

轨迹是普通进程内数据对象。当前校验可以检查对象类型、字段格式、顺序和与成功结果的字段关联，但不能证明轨迹来自真实 HTTPX 调用；持有进程代码可自行构造一组符合约束的 trace。`RESERVED_NEW` 与 `request_budget_reserved` 表示调用方记录的本地门禁和预算状态，不能证明请求字节已离开本机、抵达 Fuyao 或被 Fuyao 处理。`response_observed` 只表示客户端观察到了状态码，不代表业务数据完整或语义已验收。

终止 transport 异常不会生成 `FinancialObservationFetchResult`，原异常也不携带该次累积轨迹。即使 transport 已收到响应，若随后出现观察时间、响应体、解码、绑定或请求范围校验失败，fetcher 仍会抛错而不返回结果，因此调用方无法从 result 读取这次 trace。此时 outcome 可能未知；不能把缺少成功 result 解释为“未发送”或“上游未处理”。SQLite gate 预留及可用的结构化日志需要分别核对，它们不能自动补成完整审计链。

## Capture bridge 当前状态

现有 `prepare_fuyao_observation_capture()` 仍要求调用方另外提供 `FuyaoSourceAttemptDeclaration`，并把关联状态标为 `caller_declared_unverified`。它只校验调用者声明的 attempt/reservation 形状与预期范围，不能认证该声明属于这个 fetch result。桥接代码目前不自动使用 `fetch_result.attempt_trace`；现有归档 schema 也不持久化该 caller declaration 或新 trace。因此，fetcher 暴露轨迹不等于原始响应 manifest、准备对象、数据库记录、REST API 或调度器已经保存或使用它。

若后续要将这条轨迹接入桥接或审计持久化，应另行设计明确的版本化字段和可信来源边界，并覆盖完全失败请求的记录路径；不要只因在成功 DTO 中增加了 trace 就把它当作持久审计证明。

## 本次验证记录

验收记录中，r1 的 fetcher 回归为 **148 passed、4 failed**；失败来自新增 trace 校验先于旧字段校验，改变了已有错误优先级，因此该轮未通过。调整为旧字段校验优先后，r2 为 **152 passed**，fetcher 目标模块 statement 与 branch 合并覆盖率为 **97%**。归档桥接回归为 **32 passed**。这些是隔离合成输入的离线验证，不证明真实来源发送/接收、全 A 股覆盖、历史完整性、数据库生产持久化或 trace 的独立真实性。

验收日志归档位置：`var/reports/afi204/2026-10-28-fuyao-fetcher-attempt-trace/`。
