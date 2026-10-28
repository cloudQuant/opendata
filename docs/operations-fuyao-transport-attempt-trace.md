# Fuyao transport attempt trace 运维说明

本文说明 `FuyaoHttpClient` 成功响应对象中的本地 `attempt_trace`。它记录受控调用在本机为物理请求预留了什么，以及客户端观察到了什么 HTTP 响应；它不是供应商回执，也不是完整审计日志。

## 何时产生

只有进入带 `RequestExecutionScope` 且配置了 `SourceAttemptGate` 的 THS 请求路径，才会形成这类轨迹。每个物理发送前，客户端先消耗本地 scope 的 attempt 预算，再通过文件型 SQLite gate 写入一次性预留。只有 gate 提交后返回 `RESERVED_NEW`，请求才允许继续执行发送前检查；重复、拒绝或存储结果不确定都会阻止该次发送。预留成功仍不是发送成功的证明。

逻辑请求有一个本地 `transport_request_id`；每个成功完成 gate 预留的物理尝试使用新的 `attempt_id` 和对应的 `attempt_no`。发送前检查失败的重试可能没有 gate 记录。客户端在调用 HTTPX `send` 前设置本地 `send_started` 标记。这个标记仅表示程序进入发送调用附近，无法证明请求字节离开进程、抵达供应商或被处理。

轨迹只作为 `FuyaoHttpResponse.attempt_trace` 随最终成功返回的响应对象提供。若一次调用先发生可重试失败、随后成功，返回对象会包含已累积的预留及响应观察。若最后以异常结束，轨迹不会附在原异常上；终止失败的轨迹因此不能从该次调用结果中恢复。进入相应日志分支的传输事件会经过 logger 输出，但其保留取决于部署日志配置，不能补足完整审计链；gate 或 budget 早退未必会进入该日志分支。

## 字段含义

| 字段 | 含义与解释边界 |
| --- | --- |
| `transport_request_id` | 本地逻辑请求标识，同一次调用的重试共用。 |
| `attempt_id` / `attempt_no` | 本地单次发送预留标识及逻辑重试序号。每次物理尝试使用新标识。 |
| `reserved_at_us` / `gate_status` | SQLite gate 返回的预留时间和状态；成功轨迹应对应 `RESERVED_NEW`。这只证明本地 gate 预留。 |
| `request_budget_reserved` | 表示本地执行 scope 的预算已消耗；不表示远端收到请求。 |
| `http_status` | 客户端观察到的 HTTP 状态码；`None` 表示没有观察到状态码。 |
| `outcome` | 有状态码时为 `response_observed`，没有状态码时为 `outcome_unknown`。`response_observed` 不代表 HTTP 2xx、业务信封有效或数据可用。 |
| `failure_category` | 客户端对该次响应或传输失败的本地分类；没有识别到该次失败时为空。 |
| `source_request_id` | 响应中提供的来源请求标识，与本地请求及 attempt ID 分开保存。只接受有限长度的可打印 ASCII 值，并设置 `repr=False`。 |

## 处理 `outcome_unknown`

例如，请求超时且没有收到可读取的 HTTP 状态码时，轨迹将其标为 `outcome_unknown`。此时本地 attempt 预算和持久 gate 预留可能已经消耗，但无法判断供应商是否收到或处理了请求。不要把它改记为“未发送”或“供应商失败”，也不要仅凭缺少状态码断定远端没有副作用。任何再次请求都必须走新的、仍有效的本地授权与预算；旧的 `attempt_id` 不能复用。

成功结果没有 `attempt_trace` 也不能反推“没有发生请求”。非 scoped 调用、未配置 gate 的路径，以及无法返回响应对象的失败调用，都不提供这类轨迹。排障时应把调用方的异常、保留下来的结构化请求日志和 SQLite gate 记录分开核对，并保留“远端结果未知”的表述，直到出现独立证据。

## 安全与完整性边界

- Gate 记录本机的一次性预留，不是网络发送确认。进程可能在预留提交后、传输前退出；HTTPX 调用开始后也可能在收到状态码前超时。
- 本地 gate 的决策不证明 Fuyao 已授权该次采集、保存或内部再分发，也不证明数据使用符合供应商条款。授权范围需要由独立的合同或书面证据确认。
- `SourceAttemptGate` 当前仅支持本地文件型 SQLite 路径；它不提供多主机锁、抵抗有权限操作者篡改的审计存储，或完整的 token bucket 状态重放。
- `source_request_id` 是来源返回的独立标识，不是上游签名或可信回执。虽然它在对象 `repr` 中隐藏，调用方仍可直接读取字段；不要把响应对象、原始响应体或标识整体写入普通日志。
- 该轨迹未接入财报 fetcher 的结果持久化、capture bridge、REST API、调度器或财报归档表；不会因生成轨迹而保存业务响应或产生数据库归档记录。

## 验收记录

集成方归档的离线合成回归记录为 **152 passed**，并有 **2 条 unknown-mark warnings**；warnings 来自选择两项缓存测试时收集测试模块所产生的标记告警。`opendata/data/providers/ths/transport/http_client.py` 的 statement 与 branch 合并覆盖率为 **97%**。较早的一次运行虽有 **60 passed**，覆盖率只有 **62%**，未达到门槛；该次结果仍保留，不能视为通过。本文作者未另行运行测试。

证据目录：`var/reports/afi204/2026-10-28-fuyao-transport-attempt-trace/`。上述离线验证说明受控合成输入下的本地代码路径，不证明真实供应商授权、实际发送或接收、生产运行、财报归档或历史覆盖。
