# Fuyao 财报采集配置 bundle

`opendata.pipeline.fuyao_financial_bundle.load_fuyao_financial_bundle` 只负责加载一个部署者指定的
配置文件，并把它解析成 `FuyaoFinancialBundle`。此入口不发请求、不读环境变量、不访问数据库，
也不创建 `SourceAttemptGate` grant。

## 部署输入

部署层必须把绝对文件路径和该文件完整字节的 SHA-256 分开传给加载器：

```python
bundle = load_fuyao_financial_bundle(
    path=deployment_config.fuyao_bundle_path,
    expected_sha256=deployment_config.fuyao_bundle_sha256,
)
```

这两个值必须来自受部署权限保护的配置，不能由 `RunRequest`、HTTP 请求或 bundle 文件自身提供。
SHA-256 pin 绑定具体文件字节；它不是数字签名、来源授权、独立审核或文件声明真实的证明。
配置发布时建议先在同一受控目录写入临时文件，完成内容和权限设置后原子改名，再通过部署配置同步
路径及新 pin。不同步会 fail closed。

`FuyaoFinancialBundle` 的 Python 直接构造会再次验证单证券 registry 和完整单位科目映射，但其
`bundle_sha256` 只是调用者声明的摘要值；不能凭一个直接构造的对象判断其字节曾由加载器读取或经独立审核。
依赖路径权限和外部 pin 的生产入口应使用上述 loader。

加载器要求 POSIX `O_NOFOLLOW`、目录文件描述符及非阻塞打开能力；不支持这些能力的平台拒绝加载。
它逐级打开绝对路径且不跟随 symlink，要求文件为运行用户拥有的单链接普通文件，大小在 1 字节
至 1 MiB 之间，文件和最终父目录必须由运行用户拥有且不得对 group/other 可写。更上层目录也必须由 root 或运行用户拥有，
不得对 group/other 可写；root 拥有且启用 sticky bit 的共享祖先目录例外。打开文件后，加载器对同一个
文件描述符进行有界读取，比较读取前后文件元数据，并在解析前重新核对路径各级仍指向已打开的目录和
文件。遇到权限、替换、类型或 pin 不一致时拒绝加载。

由于配置文件不应承载秘密，加载器允许 group/other 读取；部署时仍建议用 `0600` 文件和 `0700` 父目录，
以限制对证券范围和运维声明的读取。

错误只包含固定错误码，例如 `bundle_pin_mismatch` 或 `bundle_permissions_invalid`，不会回显路径、
文件内容、key 或底层解析异常。调用方可以按 `FuyaoFinancialBundleError.code` 做安全分类。

## Bundle schema v1

文件是严格 UTF-8 JSON，不能有重复键、额外字段、非标准 `NaN`/`Infinity` 值、超限嵌套，
或经 JSON 转义后仍无法编码为 UTF-8 scalar text 的孤立 surrogate。
根对象只接受这些字段：

| 字段 | 内容 |
|---|---|
| `schema_version` | 整数 `1`。 |
| `scope` | 只含一个 `instrument_id`，格式为六位代码加 `.SH`、`.SZ` 或 `.BJ`。 |
| `instrument_reference_snapshot` | 现有 instrument reference snapshot v1 对象，由 `decode_instrument_reference_snapshot` 严格验证。 |
| `unit_profiles` | 仅含 `income`、`balance`、`cashflow` 三个 statement profile。 |

Registry 必须只有一个 lifecycle，不能带 aliases；其 `dataset_id` / `source_id` 必须是
`financial_statement` / `ths`，身份必须与 `scope.instrument_id` 一致，状态不能是 `unknown`。
边界日期及其证据沿用 registry 原值；未知起始边界仍保持未知。此结构只约束配置范围，不证明
listing、状态或生命周期声明真实。Registry snapshot 内部的 `content_sha256` 只是对同一文档内容
重算的一致性摘要，不是独立签名、独立审核或授权凭据；外层 bundle pin 也只固定部署配置指定的
字节，不会把这些声明转化为已审核事实。

`status_as_of` 是状态声明的日期，不代表加载时仍然新鲜。每个消费该 registry 的 job 必须定义
适合其业务日期的明确新鲜度政策（例如最大可接受年龄、未来日期处理和过期后的失败/复核流程），
并在执行前检查。加载器不推断当前证券状态，也不替消费者决定可接受的状态有效期。

每个 unit profile 只接受 `statement_type`、`version`、`native_units`、`mapping_sha256_declared`
和 `evidence_ref`。statement 类型必须与对象 key 一致；科目 key 集合必须精确匹配现有 THS 财报
字段登记，mapping digest 必须通过 `canonical_native_unit_mapping_sha256` 重算。profile 的语义仍是
`operator_declared`：摘要只证明映射内容与声明摘要一致，不证明来源单位或计量语义正确，也不会
转换数值。

Bundle 中不存放 Fuyao API key、grant ID、数据库连接、文件路径或 SHA pin。凭证通过服务端既有
凭证注入单独提供；不得写入 bundle 或错误日志。

## Job 组装和边界

加载结果仅提供 `instrument_id`、经验证的 `instrument_registry`、冻结的 `unit_profiles` 和已读
字节的 `bundle_sha256`。后续 job 必须在构造 fetch request 前显式校验业务输入中的 instrument ID
与 `bundle.instrument_id` 完全相同，并单独校验 statement、periodicity、时间窗口/limit 及已授权的
调用 scope。加载配置不能替 job 代做这些检查。

现有 `ThsFinancialObservationFetcher` 仍要求外部注入凭证、已存在的文件型 SQLite
`SourceAttemptGate`、可信 request scope、instrument registry 和三个单位 profile。`SourceAttemptGate`
的 grant、来源 host seal、quota binding、policy 和 bucket 必须由独立管理流程事先配置；本 loader
不会创建、修改或证明这些 SQL 记录。授权缺失或失效时，gate 应拒绝 transport。

单证券 bundle 不代表全 A 股或全历史覆盖。抓取响应也不证明缺失区间完整、季度值为单季还是累计，
或提供历史 point-in-time 重建。请保留每条观测的来源日期、披露日期、实际观察时刻、单位声明和
source retention 限制，并分别验收这些语义。

## 离线验证

`tests/test_fuyao_financial_bundle.py` 使用 synthetic lifecycle、synthetic unit labels 和临时本地
文件，覆盖外部 pin、严格 schema、单证券边界（包括直接构造对象时的 registry/profile 复验）、
Unicode scalar 校验、权限、symlink、文件替换和错误信息脱敏。fixture 中的证券代码只是满足格式验证
的合成标识，不表示真实上市证券；这些测试不建立数据库连接或调用 Fuyao。
