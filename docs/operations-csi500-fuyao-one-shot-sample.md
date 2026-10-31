# CSI 500 Fuyao 单日样本：本地授权与候选准备

这组命令只创建和操作一个程序新建的隔离 SQLite target。它只初始化 source-gate schema，明确保持 `warehouse_schema_status=NOT_APPLIED`；它不运行 OpenData warehouse/data migrations，不创建 ODS/DWD 表，也不下载或发布行情数据。命令可在 target 内记录一次本地、自我声明的 Fuyao 使用 grant 和 quota bucket，并准备一个确定性的单日请求候选。它不会读取 `.env` 或 API key，不会请求 Fuyao，不会锁定 one-shot scope、排队、启动 worker 或执行采集。现有 V1 job 路径不由这些命令改动。

Fuyao 使用依据始终标记为 `USER_DECLARED_UNVERIFIED`。本地 grant 记录操作者声明，不独立验证供应商合同、账号授权、历史保留期限、存储或再分发权。Calendar manifest 的 SHA-256 只证明文件字节与 manifest 一致，不证明来源或审核人身份。

## 命令顺序

先创建 target：

```bash
python scripts/ops/run_csi500_one_shot_sample.py init
```

保存输出中的 `target_directory`。它必须是命令返回的 `/tmp/opendata-csi500-sample-<32位UUID>` 目录；不能复制、重命名或传入其他 SQLite 文件。新 target 的 manifest 会固定 `target_profile=csi500-source-gate-authorization-only-v1` 和 `warehouse_schema_status=NOT_APPLIED`。

对新 target 执行 source-gate 初始化：

```bash
python scripts/ops/run_csi500_one_shot_sample.py migrate \
  --target /tmp/opendata-csi500-sample-<uuid>
```

该命令只对程序创建的空 SQLite 文件运行 source-gate Alembic 链，并要求读取到真实 `alembic_version_source_gate` head `sg0004_source_one_shot_v4` 及对应的完整 source-gate schema。它同时核对没有 `alembic_version_data`、`alembic_version` 或 warehouse 表。成功后输出 `source_gate_initialized` 和 `warehouse_schema_status=NOT_APPLIED`；这不表示任何行情数据迁移或采集已经发生。迁移结果或版本确认不确定时，target 会封存；不要在同一 target 上重跑迁移。

之前创建的双迁移链 target 和候选会保持原样。当前 source-gate-only 命令会只读拒绝旧 `schema_version=1` manifest，不会迁移、重标、准备或清理旧 target。需要新的隔离样本时，创建全新的 target；不要将旧候选视作已完成的新流程。

新 target 的数据库还必须在迁移预检和写锁复验时都没有任何 schema 对象。即使其中已有完整、有效的 source-gate V1 schema 或 migration marker，命令也会在 revision DDL 前拒绝它；该 target 会封存为 `migration_uncertain`，数据库字节、schema 和数据必须保持原样。不要对这个 target 重试或尝试采用已有 schema，需检查后另建新 target。

迁移子进程使用固定环境白名单，不继承调用者的数据库 URL、凭据或宿主 HOME；它把 HOME 固定到经过身份和权限校验的私有 target 目录，供配置中的用户目录路径安全解析。Alembic 配置读取显式使用宿主机 locale 编码，所以 source-gate ini 保持 ASCII 内容，确保 C locale 也能读取；子进程的 Python UTF-8 模式不会覆盖 Alembic 对 locale 编码的显式选择。

准备一份离线冻结并经人工复核的 SSE 开市日证据。manifest 至少包含 `schema_version`、`exchange`、`calendar_version`、`source_reference`、`retrieved_at_utc`、`reviewed_by`、`artifact_path`、`artifact_sha256` 和 `open_sessions`。然后为该 target 显式声明预期 caller principal、操作者及内部用途：

```bash
python scripts/ops/run_csi500_one_shot_sample.py authorize \
  --target /tmp/opendata-csi500-sample-<uuid> \
  --calendar-manifest /absolute/path/to/calendar-manifest.json \
  --business-date YYYY-MM-DD \
  --actor local-operator \
  --principal-scope local-research-service \
  --confirm-user-declared-internal-use
```

`authorize` 只接受当前用户创建、身份未变、source-gate head 和 schema 均匹配、且没有 warehouse schema 的 target。它把授权 intent 先记入 manifest，再在同一隔离数据库中写入 `ths/index_daily/query`、Fuyao 固定 host、单个 principal 的短期本地 grant 和 quota bucket。成功前会读回 grant、host seal/audit、quota policy/binding/bucket、target identity 与有效 grant 状态；仅在这些实际行与 manifest pins 相符后才记录 `authorized`。

grant 的有效期为 15 分钟。quota 是容量 1 token、每秒补充 1 microtoken 的 bucket；它不是持久的一次请求上限。CLI 的结果只说明本地声明和配置已记录，输出不会声称供应商权利已核实。用户取消、进程中断、grant/quota commit 或 manifest 写入结果不明确时，不要盲目重跑 `authorize`；该 target 会留在 `authorization_started` 或其他未确认状态供检查，需使用新 target 前先处理旧状态。

保存日历 manifest 后，可以准备一个候选 intent：

```bash
python scripts/ops/run_csi500_one_shot_sample.py prepare \
  --target /tmp/opendata-csi500-sample-<uuid> \
  --calendar-manifest /absolute/path/to/calendar-manifest.json \
  --business-date YYYY-MM-DD
```

`prepare` 会只读核对 target 的实际 source-gate marker 与完整 schema、warehouse schema 未应用、活动 grant、host 封存、审计行、quota 配置、数据库设备号/inode 和 V4 one-shot 状态。它要求候选 scope 仍为 `UNCONFIGURED`，之后才原子写入 manifest。已有 `intent_prepared` 结果也要重新通过这些核对，并与重算的完整 intent pins 完全一致，才会返回同一个候选。任何不匹配或状态不确定都 fail-closed。

候选固定为 `ths/index_daily/query`、`000905.SH`、一个确切交易日、HTTPS GET `fuyao.aicubes.cn/api/a-share-index/prices/historical`，transport 和 task attempt 上限均为 1。manifest 中的 `CANDIDATE_NOT_LOCKED` 不是 gate receipt、send permit、任务回执或数据存在证明。

## 预览和当前能力边界

无需 target 或 calendar 可查看固定请求形状：

```bash
python scripts/ops/run_csi500_one_shot_sample.py preview --business-date YYYY-MM-DD
```

预览不检查该日期是否为真实 SSE 交易日。当前 CLI 只提供 `init`、`migrate`、`authorize`、`prepare` 和 `preview`；它不提供 preflight、submit、execute、verify 或 cleanup 命令。V4 transport、worker、任务排队、one-shot scope lock、成功核验及自动清理尚未接通。不要把本地 grant、quota 或 `intent_prepared` 当成采集成功，也不要据此声称仓库数据或真实采集已经完成。

target 目录权限为 `0700`，数据库和 manifest 为 `0600`。manifest 中的 `target_id` 必须与私有 target 目录 UUID 一致；加载时会拒绝错配。operation lock 协调这组 CLI 的并发调用；同一 OS 用户仍可绕过 CLI 直接改本地 SQLite，因此它不是防御特权用户或主机管理员的安全边界。当前不提供自动删除 target 的命令；保留 target 供检查，需清理时应先确认授权与候选状态已妥善处理。任何目标都只适用于这个本地 target，不代表跨备份、跨文件或多主机的一次性限制。
