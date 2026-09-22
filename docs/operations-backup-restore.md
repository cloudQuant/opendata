# 备份与恢复运维手册（AC-19 / NFR-9）

> 版本：v1.0 日期：2026-09-22
> 适用范围：opendata 双库（元数据库 `opendata` + 数据仓库 `opendata_data`）
> 关联：`验收文档.md` AC-19；`需求文档.md` NFR-9；`实施计划.md` A0/A4

## 1. 目标与口径

| 项 | 目标 |
|----|------|
| RPO（可容忍数据丢失） | ≤ 24 小时（每日逻辑备份 + binlog 可进一步收紧） |
| RTO（恢复耗时） | 元数据库分钟级；数据仓库取决于体量，全量回补可由 pipeline 重建 |
| 数据资产特性 | **爬虫历史（尤其冷门区间）可能不可再生**，因此备份优先级高于回放重建 |

数据资产分层与恢复策略：

| 数据 | 可否重建 | 备份策略 |
|------|---------|---------|
| 元数据（用户/任务/执行记录/API Key） | **不可重建** | 每日全量 + binlog |
| `ods_*` / `dwd_*`（行情、财务、日历） | 可经 pipeline 回补，但耗时且依赖上游可用性 | 每日全量 + binlog |
| Parquet 分钟线文件（D13） | 可经 market-dumps 重新拉取 | 文件系统快照/对象存储同步 |
| 缓存（原始响应） | 无需备份 | TTL 过期即弃 |

## 2. 每日备份

脚本：`scripts/ops/backup_mysql.sh`

```bash
# 手动执行
./scripts/ops/backup_mysql.sh

# 自定义输出目录与保留天数
BACKUP_DIR=/mnt/opendata-backups BACKUP_RETENTION_DAYS=60 ./scripts/ops/backup_mysql.sh

# 定时（每日 02:00）
0 2 * * * /opt/opendata/scripts/ops/backup_mysql.sh >> /var/log/opendata-backup.log 2>&1
```

产出：`metadata_<UTC时间戳>.sql.gz`、`warehouse_<UTC时间戳>.sql.gz`，默认保留 30 天。

凭证来源：脚本读取项目根 `.env`（`MYSQL_*` / `DATA_MYSQL_*`），**不打印口令**。

### 附录：binlog（可选，用于收紧 RPO）

在 MySQL 配置中启用：

```ini
[mysqld]
log_bin = /var/lib/mysql/mysql-bin
expire_logs_days = 7
binlog_format = ROW
```

## 3. 恢复步骤

### 3.1 元数据库

```bash
# 1) 停写：停后端（避免恢复期间写入）
sudo systemctl stop opendata

# 2) 重建空库
mysql -h "$MYSQL_HOST" -u "$MYSQL_USER" -p \
  -e "DROP DATABASE IF EXISTS opendata; CREATE DATABASE opendata
      CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

# 3) 导入备份
gunzip -c backups/metadata_<STAMP>.sql.gz | \
  mysql -h "$MYSQL_HOST" -u "$MYSQL_USER" -p opendata

# 4) 校验迁移版本与后端启动
alembic upgrade head
sudo systemctl start opendata
curl -fsS http://localhost:8000/health
```

### 3.2 数据仓库

```bash
mysql -h "$DATA_MYSQL_HOST" -u "$DATA_MYSQL_USER" -p \
  -e "DROP DATABASE IF EXISTS opendata_data; CREATE DATABASE opendata_data
      CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

gunzip -c backups/warehouse_<STAMP>.sql.gz | \
  mysql -h "$DATA_MYSQL_HOST" -u "$DATA_MYSQL_USER" -p opendata_data
```

> 数据仓库 DDL 由独立 alembic 环境管理（`alembic_data/`，迭代 1A-A4 交付）。
> 在 A4 之前，ods/dwd 表结构尚未引入，恢复演练以"元数据库 + 备份/恢复链路可用"为准。
>
> 元数据库迁移现已可用：`alembic upgrade head` 之后用 `alembic check` 可验证"迁移与模型零漂移"。
> A0 阶段重建了迁移基线（原 001–004 与实际模型差两代，无一条链路可执行），详见
> `docs/evidence/A0/restore-drill.txt`。

### 3.3 binlog 时间点恢复（需要时）

```bash
gunzip -c backups/metadata_<STAMP>.sql.gz | mysql ... opendata
mysqlbinlog --start-datetime="<备份完成时刻>" /var/lib/mysql/mysql-bin.* | mysql ... opendata
```

## 4. 恢复演练检查单

演练至少每迭代一次；结果记入 `docs/evidence/<里程碑>/`。

| # | 步骤 | 通过判据 |
|---|------|---------|
| 1 | 执行一次备份，产出两个 `.sql.gz` | 文件非空、`gzip -t` 通过 |
| 2 | 在隔离库（如 `opendata_restore_check`）导入元数据库备份 | 导入无错误 |
| 3 | 比对关键表行数（`users` / `task_executions` / `scheduled_tasks`） | 与备份源一致 |
| 4 | 导入数据仓库备份（A4 之后） | 导入无错误 |
| 5 | 启动后端指向恢复库，`/health` 通过 | HTTP 200 |
| 6 | 记录：耗时、行数、异常与处置 | 演练记录归档 |

## 5. 保留与容量

| 数据 | 保留期 |
|------|--------|
| 逻辑备份 | 30 天（可配 `BACKUP_RETENTION_DAYS`） |
| binlog | 7 天 |
| 日线 / 财务 / 元数据 | 永久 |
| 分钟线（Parquet） | N 年（可配） |
| 缓存（原始响应） | TTL（可配） |
| `dq_diff_report` | 聚合数据 N 天，明细走导出文件 |

容量预算（设计 §8.5）：日线双源 + dwd 约 4.5 GB；分钟线走文件存储约 1.2 TB。
建议为备份单独预留 ≥100 GB，并纳入磁盘水位告警（`FR-14` 告警矩阵）。

## 6. 当前状态

- ✅ 备份脚本已交付：`scripts/ops/backup_mysql.sh`（含依赖前置检查、失败清理、0600 临时凭证文件）
- ✅ 恢复步骤与演练检查单已成文
- ✅ **恢复演练已于 2026-09-22 执行一次并通过**：备份 → 隔离库恢复 → 9/9 对象行数一致 → 应用指向恢复库 `/health` 通过。
  原始输出与发现的问题见 `docs/evidence/A0/restore-drill.txt`
- ⚠️ 该次演练同时暴露并修复了 8 个既有缺陷（含"应用只能处理第一个请求"的连接池缺陷与"启动即崩溃"的
  初始化幂等缺陷），并在 A4 待办中记录了 3 项发现（生产启动仍写库、`ENABLE_SCHEDULER` 未生效等）
- ⏳ 未覆盖：binlog 时间点恢复演练；A4 引入 ods/dwd 后的体量级恢复耗时
