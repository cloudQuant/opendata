# 离线股票日线缺口核验

`scripts/ops/reconcile_stock_daily_history.py` 根据调用者显式准备的本地证据，复用股票日线历史覆盖核验器，输出确定性 JSON。它只读文件，不访问数据库、`.env`、供应商、网络或系统时钟，也不创建补采计划、不写业务数据、不触发自动修复或任务派发。

## 目录格式

调用方先把证据复制到一个专用目录：

```text
evidence-bundle/
├── manifest.json
└── artifacts/
    ├── <manifest 中的 sha256>.json
    └── ...
```

manifest 使用严格 UTF-8 JSON，根对象包含 `schema_version`、`registry_ref`、`artifacts` 和 `series`。版本为整数 `1`。每个 `artifacts` 条目恰好包含 `evidence_ref`、`kind`、`sha256`。文件名只由小写 64 位 SHA-256 决定；CLI 不把 manifest 中的 `evidence_ref` 当成路径。

`kind: "instrument_registry"` 对应文件必须是由现有 `encode_instrument_reference_snapshot` 编码的原始 instrument-reference snapshot。`kind: "daily_publication_content"` 是另一个原始 JSON 文件；它严格包含 `rows` 数组，每条记录必须有唯一的规范 `YYYY-MM-DD` 格式 `trade_date`，也可以保留其他业务字段。若记录含 `dataset_id`、`source_id`、`instrument_id` 或 `publication_id`，这些值必须与 publication envelope 的身份完全相符；字段缺省时，原始内容本身不携带这些身份。该文件不套 envelope，manifest 中的 SHA-256 直接绑定整个输入文件字节；若未提供 publication，则不允许留下未引用的 content artifact。

其他 kind 的文件使用 loader 定义的版本化证据 envelope：

```json
{
  "evidence_ref": "evidence:calendar:2024-v1",
  "schema_version": 1,
  "dataset_id": "equity_daily",
  "source_id": "provider_x_daily",
  "instrument_id": "CN:600519",
  "publication_id": null,
  "payload": {}
}
```

`kind: "daily_publication"` 的 envelope `payload` 精确为 `{"content_ref":"evidence:publication-content:2024-v1"}`，其中 `content_ref` 必须指向一个已 pin 的 `daily_publication_content` 文件。不同 publication 不得复用相同的内容字节/SHA，即使使用不同的 `evidence_ref`；此类复用会被拒绝。series 里的 `publication_ref` 与 `publication_id` 必须同时填写；publication envelope 的 ID 必须与 series 一致。报告里的发布内容 SHA-256 指向原始 `daily_publication_content` 文件字节，不是 envelope 的摘要；`scope.publication_content_identity_binding` 会标为 `caller_asserted_by_envelope_content_ref`，对应 artifact 的 `identity_binding` 会标为 `caller_asserted_by_publication_envelope_content_ref`。这表示身份关联来自调用方提供的 envelope 与 `content_ref`，不是原始内容自身认证出的身份；哈希只证明输入字节和 manifest pin 一致，不能证明内容来自某供应商、身份声明真实或已获授权。

`series` 中每条记录精确描述一个 dataset/source/instrument 查询范围，并引用其 registry 生命周期、交易日历、单证券预期观察日以及可选的已发布观察和来源保留范围。字段为：`dataset_id`、`source_id`、`instrument_identifier`、`identity_as_of`、`request_start`、`request_end`、`publication_id`、`publication_ref`、`calendar_ref`、`expectation_ref`、`lifecycle_ref`、`retention_ref`、`delisted_end_ref`；可选项显式填写 `null`。`publication_ref` 非空时，`publication_id` 也必须提供，并与发布文件里的 ID 完全一致；两者都为空才表示没有发布快照。具体 envelope 与 payload 字段由 `opendata.pipeline.stock_daily_gap_evidence_bundle` 的 schema 校验。

日期应保留来源的业务日期。完整历史核验需要独立的上市/起始边界、完整交易日历、逐证券预期观察日、原始发布内容、publication ID 绑定以及来源保留范围证据。来源返回的第一行不能代替上市日；最近一段滚动数据不能代替完整历史。未知边界会继续标为未知，观察日期不会被自动补造。日历与证券市场的匹配关系仍由调用方证据提供，报告明确不表示这层关系已独立核验。

## 运行

从仓库根目录传入绝对路径：

```bash
python scripts/ops/reconcile_stock_daily_history.py \
  --evidence-dir /absolute/path/to/evidence-bundle
```

成功时，报告作为一行紧凑、键序固定的 JSON 写到标准输出；错误时输出不包含本机路径或证据正文的稳定 `blocked` JSON，并返回非零状态。CLI 只打开固定的 `manifest.json`、`artifacts/` 和其中按摘要命名的普通文件；目录和文件均以拒绝符号链接的方式打开，文件以非阻塞模式打开后再确认是普通单链接文件，因此无 writer 的 FIFO 也会被快速拒绝。相对根路径、超限输入和读取期间发生变化的文件都会被阻断。

大小上限为：manifest 64 MiB、普通证据文件（含 publication content）8 MiB、registry snapshot 64 MiB、全部 artifacts 合计 192 MiB、最多 50,000 个 artifact；每个 series 最多 100,000 条日期/发布记录。CLI 不扫描目录中的其他文件；把数据库、`.env`、密钥和无关文件放在证据目录里也不会使它们成为输入。

## 解释报告

报告内 SHA-256 用于确认本次读取字节与 manifest pin 一致，并为相同输入生成稳定报告；它不是数字签名，不证明证据来源、审核人、授权、历史可回溯性或数据完整性。调用者必须独立确认所用材料的来源、许可和统计口径。

报告可以显示所提交的每条证券序列在所给边界下的缺口与未知项，但始终将 `all_a_universe_complete` 和 `universe_census_attested` 标为 `false`。一条证券、一个供应商或一份局部 registry 都不能证明全 A 股（含退市证券及交易所范围）的完整性。报告不会证明历史数据源曾保存当前缺失的快照，也不会声称任何采集已执行或数据已修复。
