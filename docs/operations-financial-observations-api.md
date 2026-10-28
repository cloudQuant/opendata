# 财报观察 REST API 运维说明

## 当前能力与启用条件

只读接口路径为 `GET /api/v2/data/financial-statements/archive`，由 v2 路由注册。它从
`od_financial_observations` 查询已经归档的观察值；本接口不采集、不补数、不迁移表，也不
写数据库。归档表和迁移要求见[财报归档 Reader 运维说明](operations-financial-observation-archive-reader.md)。

接口只有在服务进程获得单独配置的 `FINANCIAL_ARCHIVE_DATABASE_URL` 后才会尝试连接归档库。
该设置在 `Settings` 中是 `SecretStr | None`，默认未配置；未配置、URL 不合法或目标不满足
要求时，归档数据读取失败关闭并返回 `503 published_data_unavailable`，不会改从主业务库或
数据仓库查询归档记录。请求认证仍使用 OpenData 现有认证依赖；JWT/API Key 验证可能访问主
业务库，这与归档数据查询目标分开配置。
本地 `.env` 可以保存该设置，但必须保持在 Git 忽略范围内并限制文件权限；生产环境应由部署
平台的密钥管理注入，避免把凭证写入镜像、日志、命令历史或版本库。

本地配置示例（请以 DBA 分配的实际连接信息替换占位值）：

```dotenv
FINANCIAL_ARCHIVE_DATABASE_URL="mysql+pymysql://financial_api_reader:<URL-encoded-password>@archive-db.internal:3306/opendata_financial"
```

用户名和密码中的 URL 保留字符须按 URL 规则编码。服务仅接受显式的 `mysql+pymysql` URL、
主机和数据库名；Unix socket 配置会被拒绝。服务会把归档 URL 与已配置的主业务库和数据仓库
按主机、端口、数据库名做本地规范化比较，拦截已知目标冲突，不执行 DNS 查询，因此无法发现
不同 DNS 名称背后的同一物理实例或代理。上线前，DBA 必须独立确认归档库确实与写入库隔离，
并为 API 配置单独账号，仅授予目标归档 schema 的 `SELECT` 权限；不要复用应用写账号。代码
不会创建迁移或发出写语句，数据库的只读授权仍是最后一道保护。

每个请求使用新建的 SQLAlchemy `NullPool` engine 和短连接。PyMySQL 连接参数当前为连接超时
5 秒、读取超时 15 秒、写入超时 5 秒；接口没有连接池复用。部署代理/客户端超时应覆盖连接和
读取上限并留有余量。数据库不可用、归档表不存在或不满足 Reader 的 MySQL/InnoDB/
`REPEATABLE READ` 要求时，接口返回经过清理的 503；响应不会泄漏 URL 或数据库异常细节。

## 认证与授权

请求须通过 OpenData 现有认证。支持现有 Bearer JWT，也支持 API Key（`X-API-Key` 或
`Authorization: Bearer od-…`）。未认证或凭证无效返回 401。此接口的 `financial_statement`
域检查约束 API Key：Key 必须包含该域，否则返回 403，且在权限检查之前不会创建归档连接。
已认证 JWT principal 的 `scopes` 为 `None`，现有域检查会放行；JWT 沿用现有用户认证规则，
不应用 API Key 的域白名单。API Key 的身份验证、启用状态和限流仍由现有依赖处理；触发限流
时返回 429。不要把数据库只读账号凭证用作 HTTP API 凭证。

## 请求参数与限制

除可选的 `page_size` 外，其他参数都必须恰好出现一次；`page_size` 可以省略或出现一次，
未知参数和任何重复参数都会返回 422。查询必须显式指定：

| 参数 | 取值 |
| --- | --- |
| `source_id` | 小写来源标识，最长 64 字符，例如 `ths_fuyao` |
| `instrument_id` | 单只证券代码：六位数字加 `.SH`、`.SZ` 或 `.BJ` |
| `statement_type` | `income`、`balance` 或 `cashflow` |
| `periodicity` | `annual` 或 `quarterly` |
| `start_report_period` | 起始报告期，ISO 日期，包含边界 |
| `end_report_period` | 结束报告期，ISO 日期，包含边界 |
| `page_size` | 可选，默认 100，允许 1–199 |

起始报告期不得晚于结束报告期，两端日期相差不得超过 3,660 天。接口每次只查一个来源、
一只证券、一种报表和一种频率。HTTP 层最多接受 199 行，内部额外读取一行来判断是否还有
后续结果；若还有更多结果，整页拒绝并返回 413，不会返回部分记录。序列化后的完整响应也
不得超过 256 KiB，超限同样返回不含部分记录的 413。请求 200 行会在执行 SQL 前返回 422。

当前接口不接受 `cursor`、`as_of` 或其他分页/快照参数。每个 HTTP 请求独立开启一个
`REPEATABLE READ` 事务；查询和响应 payload 构造完成后事务结束、连接关闭，时间点早于 HTTP
响应发送。后续请求会建立新读视图，可能看到后来提交的变更。
因此，不要将多次请求拼接后称为同一数据库快照。当前也没有批量多证券接口或全 A 股遍历
协议；客户端逐只请求不能证明证券名单完整或历史覆盖完整。

示例：

```bash
curl -G "$OPENDATA_BASE_URL/api/v2/data/financial-statements/archive" \
  -H "X-API-Key: $OPENDATA_API_KEY" \
  --data-urlencode 'source_id=ths_fuyao' \
  --data-urlencode 'instrument_id=600519.SH' \
  --data-urlencode 'statement_type=income' \
  --data-urlencode 'periodicity=quarterly' \
  --data-urlencode 'start_report_period=2025-03-31' \
  --data-urlencode 'end_report_period=2025-03-31' \
  --data-urlencode 'page_size=100'
```

## 响应语义

成功响应直接包含 `scope`、`records` 和固定状态字段。每条记录只暴露来源、证券、报表类型、
频率、报告期、源披露日、OpenData 观察时刻、项目代码、十进制值文本和来源原生单位；内部
哈希、源原始元数据和策略字段不会通过此路由返回。

- `report_period` 是财报所属报告期；`source_disclosure_date` 是来源披露日；
  `observed_at_utc` 是 OpenData 观察到该值的时刻，返回带 `Z` 的 UTC 时间戳。这三个时间
  含义不同，不可互换。
- `value_decimal_text` 保留源十进制文本，或为 JSON `null`。消费端应使用十进制类型，不能
  先转二进制浮点数；`null` 表示当前归档值为空，不应自动填零。
- `native_unit` 原样表达存储的单位；接口不统一币种、缩放、会计口径或单位。消费端必须
  连同 `item_code` 和来源契约解释数值。
- `completeness` 固定为 `unknown`，`point_in_time_reconstruction` 固定为 `unavailable`，
  `period_amount_semantics` 固定为 `unknown`。一页有数据或请求成功，都不能证明历史无缺口、
  A 股范围完整、季度金额是单季值或已恢复过去时点可见版本。

常见状态：`200` 查询成功；`401` 未认证；`403` API Key 缺少域权限；`422` 参数无效、未知或
重复；`413` 页面超行数或响应体超限；`429` API Key 限流；`503` 未配置归档库或归档服务不可用。
v2 错误采用统一 `error` 对象，包含稳定错误码、清理后的消息和请求 ID。

## 运维检查

启用前由 DBA/运维逐项核对：

1. 财报归档迁移已在目标专用数据库执行，`od_financial_observations` 存在且使用 InnoDB；
   API 服务账号只有该 schema 的 `SELECT` 权限，没有写入或 DDL 权限。
2. `FINANCIAL_ARCHIVE_DATABASE_URL` 指向已独立核实的目标；不要只依赖应用的 URL 字符串比较
   判断物理隔离。未配置此变量时接口保持关闭。
3. 从服务运行环境使用受限 API Key 验证 401、403、200 和 503 路径；检查代理超时、访问日志
   脱敏、限流和请求 ID。不要在日志中记录 API Key、数据库 URL、完整响应或财报敏感载荷。
4. 使用代表性归档规模和真实库检查查询计划、扫描行数、响应时间、并发连接数及 InnoDB
   undo/purge 影响。当前验收不是容量测试。
5. 观察 503、413、429 和数据库读取延迟。接口没有重试、缓存、跨请求快照或可续传 cursor；
   客户端需按自身调用语义处理临时失败，并避免把多次读取误认为一致批次。

## 验收证据与边界

离线 Mock 回归记录位于
[`var/reports/afi204/2026-10-28-financial-observations-api`](../var/reports/afi204/2026-10-28-financial-observations-api)：
最终 r7 有 45 项通过，API 模块语句和分支合并覆盖率为 98%。历史轮次及失败记录保留在同一
目录，不能把未达标或失败轮次算作验收通过。

另有隔离 MySQL 8.4 合成验收，记录位于
[`mysql-acceptance/`](../var/reports/afi204/2026-10-28-financial-observations-api/mysql-acceptance)。
r2 成功轮次使用很小的合成归档表、临时 MySQL 实例和临时 `SELECT` 账号，使用自定义
TestClient 配置验证路由。仅在测试 worker 中将 TCP 形状的连接配置映射到私有 socket；MySQL
自身禁用网络，因此没有验证真实 TCP 链路。验收覆盖拒绝写入、401/403 在连接前拒绝、三个
`REPEATABLE READ` 场景、十进制文本/NULL/报告期/披露日/UTC 时刻、后续事务观察到已提交更新，
以及超限 413 不返回部分行。MySQL 可选组件加载告警也保留在成功轮次记录中。

该验收没有验证真实 TCP、生产 JWT/API Key 验证链、生产网络或生产数据库，也没有验证全 A 股
范围、完整历史、源保留边界、PIT 重建、真实业务数据准确性或负载容量。历史失败轮次也保留；
失败原因只有在证据确认时才可归因。OpenAPI baseline 已在本 PR 同步更新，且两项 focused
live OpenAPI gate 已通过；这不代表生成客户端或 SDK 契约已验收。

在以上剩余核验完成前，应将该能力视为有边界的归档查询切片，不将 200 响应用于宣称财报
治理、历史覆盖或 PIT 能力已经验收完成。
