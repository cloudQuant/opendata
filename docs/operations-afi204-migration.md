# AFI 204 首批迁移工具和运行基础

2026-10-26。当前交付是隔离分支上的代码与离线验证，尚未迁移生产数据、切换调度器或开放新的生产 API。总任务分解位于 ai-for-investor 的迭代204文档，包含87个子任务；本批完成其中6个基础子任务。

## 代码入口

| 模块 | 已实现 | 边界 |
| --- | --- | --- |
| `opendata.migration_inventory` | 只读AST盘点、文件哈希、JSON/CSV、可选任务映射双向检查 | 默认只扫描AFI的 `src/backend/app/data_fetch/scripts`，不是全部生产任务/进程清单 |
| `opendata.pipeline.history_coverage` | 单数据集/来源/标的的日粒度预期与实际日期对账 | 不生成交易日历，不认定证据真实性，不查询仓库或数据源；intraday未支持 |
| `opendata.pipeline.run_identity` | 版本化Job/Run合同、UTC自然槽、幂等键与独立请求快照hash | 不派发任务，不持久化；默认任务disabled |
| `opendata.pipeline.run_ledger` | 显式Engine注入、SQL持久运行状态、CAS、租约、心跳、终态和取消 | 未接Alembic/现有worker；token只保护运行账本，不保护仓库事实写入 |
| `opendata_client` | v1严格分页、显式行/页预算、查询响应流式读取及16MiB解码体上限 | v1仍是latest-view，跨页不保证固定版本；大规模研究需后续snapshot/export |

配套的 ai-for-investor REST 适配器已增加逐行symbol精确校验、严格业务日期、分页回显检查、重复页检查、流式响应及累计字节预算。仍只登记当前股票日线合同，不扩大来源/频率，不将当前采集时间解释成历史PIT时间。

## 静态盘点

在opendata仓库根目录运行，输入仓库只读：

```bash
python scripts/ops/inventory_legacy_scripts.py \
  --source-root /home/yun/Documents/ai-for-investor \
  --format json \
  --output /tmp/afi204-scripts.json
```

输出已存在时默认拒绝覆盖；需要重建报告可显式加 `--overwrite`。扫描脚本树、task-map输入、`.env`命名路径及其符号链接别名始终受保护，不能作为报告输出。语法错误/不可读文件不会静默消失，报告保留错误并以非零状态退出。脚本不被import或执行。

可选 `--task-map /path/to/tasks.json`，形态为：

```json
{"tasks":[{"task_id":"legacy-task-1","script_path":"src/backend/app/data_fetch/scripts/stocks/daily/stock_zh_a_hist.py"}]}
```

这是已脱敏的离线输入，不会自动连接数据库导出任务。路径必须是相对source-root的精确路径；多文件任务可用 `script_paths`。工具会报告无脚本的任务、无映射的入口候选和重复身份。辅助候选保留人工审查，静态匹配不会升级source/task/dataset验证状态。

本批实际扫描1,066个源文件，集合及SHA-256与迭代204起始CSV一致，语法/文件系统错误为0；来源验证状态全部保持 `NOT_VERIFIED`。systemd/crontab、独立worker、其他脚本目录、动态服务调用和真实DB任务仍在后续盘点范围。

## 运行身份与账本

`build_run_identity(TaskDefinition, RunRequest)` 输出：

- `idempotency_key`：运行槽的稳定键。scheduled按job版本、UTC occurrence、分片和显式partition区分；manual/backfill需要显式请求键。
- `payload_hash`：完整业务请求快照的SHA-256。
- `canonical_json`：有界、规范化、可重放的声明式参数快照。

同一运行槽参数变化保持相同key、产生不同快照，`RunLedger.enqueue(key, canonical_json)` 会拒绝冲突。任务版本变化必须显式登记，不能利用参数漂移绕开自然槽幂等。凭据通过独立secret引用注入，不能放入query/partition；验证会拒绝凭据类字段和明显token形态，但不宣称能识别任意伪装字符串。

RunLedger构造器不进行数据库I/O，也不创建表。后续由唯一迁移owner将 `RUN_LEDGER_TABLE` 纳入metadata Alembic，并以受控运行服务注入Engine。禁止通过生产启动时的 `create_all` 绕开迁移。

本批仅在owned SQLite数据库验证重启持久、竞争领取、租约过期、旧token拒绝、取消及终态；MySQL/PostgreSQL分支使用行锁和CAS，但目标数据库行为尚需独立验收。仓库写fence、跨库发布与现有APScheduler/CLI/systemd的统一接线仍待后续子任务。

## 日粒度历史覆盖

调用者提供dataset/source/series、请求范围、独立起点证据、带身份/版本/覆盖范围的可信日历以及实际观察日期。评估器输出head/interior/tail、重复/意外日期及覆盖状态。

日历范围未知或截断、独立起点未知时不能得到完整历史认证。源保留边界只限制尚缺失的日期，已经归档的早期真实记录仍可计入覆盖。snapshot-only必须有匹配的归档范围与证据，单日快照也不能自动证明历史。输出的COMPLETE只说明输入证据支持的日粒度键覆盖，不证明字段值正确、许可有效或源端真实完整；这些需要独立DQ和真实来源验证。

## 下一波接线顺序

1. 冻结首个股票日线样板的数据/API合同和黄金fixture。
2. 将RunLedger纳入唯一Alembic序列，连接统一Run service与受限worker。
3. 将逐标的coverage接入真实主数据/日历和仓库读取，分别验证来源与字段语义。
4. 扩展publication/snapshot/export，再迁移业务消费者和管理界面。
5. 完成逐域对账、自然周期和回滚演练后，再实施生产单写者切换。

本批没有修改运行中的scheduler、数据库、systemd、源Key或API开关。离线通过不能替代完整 `make gate`、目标数据库、真实源及部署验收。

验证收据见 [afi204-offline-validation.json](afi204-offline-validation.json)：442项组合回归及75项AFI回归通过，独立wheel安装测试通过；本批受检模块综合覆盖率92.18%。整仓门禁和生产验收不由本收据代替。
