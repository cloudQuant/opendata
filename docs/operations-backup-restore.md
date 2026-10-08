# 备份与恢复运维手册（AC-19 / NFR-9）

> 版本：v1.1 日期：2026-09-30
> 适用范围：opendata 双库（元数据库 `opendata` + 数据仓库 `opendata_data`）
> 关联：`验收文档.md` AC-19；`需求文档.md` NFR-9；`实施计划.md` A0/A4

## 1. 目标与口径

| 项 | 目标 |
|----|------|
| RPO（可容忍数据丢失） | MySQL：≤ 24 小时（每日逻辑备份；binlog 只在同一数据库主机）；分钟归档：尚未建立 |
| RTO（恢复耗时） | 元数据库分钟级；数据仓库取决于体量，全量回补可由 pipeline 重建 |
| 数据资产特性 | 上游历史数据可能不可再生；分钟归档须启用配对快照，生产部署与 RPO 另行验收 |

数据资产分层与恢复策略：

| 数据 | 可否重建 | 备份策略 |
|------|---------|---------|
| 元数据（用户/任务/执行记录/API Key） | **不可重建** | 每日全量 + binlog |
| `ods_*` / `dwd_*`（行情、财务、日历） | 可经 pipeline 回补，但耗时且依赖上游可用性 | 每日全量 + binlog |
| Parquet 分钟线文件（D13） | 不假定可从上游重建 | 配对快照包含双库 dump、全部分钟文件和 SHA-256 清单；异机复制另行部署 |
| 缓存（原始响应） | 无需备份 | TTL 过期即弃 |

分钟线现由本地 CSV 导入器写入 `DATA_DIR/minute_archive`，元数据索引位于主库；仓库没有
自动抓取分钟线的 provider。设置 `BACKUP_MINUTE_ARCHIVE_DIR` 后，备份脚本在归档全局
排他锁内生成双库 dump 和分钟 tar，校验完整清单后原子发布整个目录。Compose 备份 profile
默认启用该路径，并挂载与后端相同的分钟目录。MySQL binlog 仍不包含文件内容；生产每日
调度、异机副本、容量和恢复耗时尚须部署验证，不能据本地演练声明 RPO 已达标。
原始响应缓存无需恢复；`CACHE_DIR` 的其它目录不会被 raw-response 清理触碰。

## 2. 每日备份

脚本：`scripts/ops/backup_mysql.sh`。运行每日任务的可选 Compose 服务
使用 `scripts/ops/Dockerfile.backup` 和仓库脚本本身，不依赖宿主机 cron。

```bash
# 手动执行
./scripts/ops/backup_mysql.sh

# 自定义输出目录与保留天数
BACKUP_DIR=/mnt/opendata-backups BACKUP_RETENTION_DAYS=60 ./scripts/ops/backup_mysql.sh

# 启用可选服务（每日 02:00 UTC 自动运行；默认 profile 不会启动它）
docker compose --profile backup up -d --build backup

# 查看服务状态与最近日志
docker compose --profile backup ps backup
docker compose --profile backup logs --tail=100 backup
```

未设置分钟路径时，保持双库 SQL gzip 输出，默认保留 30 天。需要 D13 备份时运行：

```bash
BACKUP_MINUTE_ARCHIVE_DIR="$PWD/data/minute_archive" \
  BACKUP_PYTHON=/Users/yunjinqi/opt/anaconda3/bin/python \
  ./scripts/ops/backup_mysql.sh
```

配对产出为 `snapshot_<UTC时间戳>_<随机标识>/`，含 metadata/warehouse SQL gzip、
`minute_archive.tar.gz`、`manifest.json`。清单记录每个 payload 和分钟文件的大小及 SHA-256。
快照不含锁文件；任一步失败只清理本次 staging，完整校验后才发布。保留清理仅删除名称、
清单和全部 payload 均验证通过的过期配对目录，损坏或外来目录留存供操作员处理。

归档读、写与保留清理持有共享全局锁；备份独占 `.snapshot.lock`，整个 dump/复制期间
这些操作会等待。完整文件复制和清单占用随归档体量增长，须按实际体量安排备份窗口。
只覆盖同一宿主机本地 POSIX 文件系统，未经验证的跨主机共享文件系统不得套用本结论。
该锁保护分钟文件与主库分钟索引配对；两套数据库其它业务写入的跨库事务一致性仍需应用停写。
备份目录必须位于分钟根目录之外。只恢复主库或文件之一都会破坏查询，binlog 不能补回 Parquet。

### MySQL 备份账号权限

脚本保留 `--single-transaction --quick --routines --triggers`，并使用 `mysqldump` 默认
表空间输出。每个数据库使用独立的备份账号：账号仅连接对应的 metadata 或 warehouse 库，
Compose 通过 `BACKUP_MYSQL_USER` / `BACKUP_MYSQL_PASSWORD` 和
`BACKUP_DATA_MYSQL_USER` / `BACKUP_DATA_MYSQL_PASSWORD` 绑定这两组凭证；手动运行时可将
这些账号分别映射到脚本的 `MYSQL_USER` / `MYSQL_PASSWORD` 与
`DATA_MYSQL_USER` / `DATA_MYSQL_PASSWORD` 环境变量。

两账号分别需要目标库对象的 `SELECT`、`SHOW VIEW`（存在视图时）和 `TRIGGER` 权限；
`--single-transaction` 下不需要 `LOCK TABLES`。当前保留的表空间输出需要全局 `PROCESS`；
`--routines` 的定义读取需要全局 `SELECT`，MySQL 8 也可用 `SHOW_ROUTINE` 动态权限限制
routine 可见范围。[mysqldump 权限说明](https://dev.mysql.com/doc/refman/8.0/en/mysqldump.html)
和[存储过程权限说明](https://dev.mysql.com/doc/refman/8.0/en/stored-routines-privileges.html)
列出了这些要求。这些全局权限不会由 Compose 初始化的默认应用账号自动获得；默认应用账号只
有各自数据库的权限，不能据此假定备份 profile 可运行。隔离 MySQL 8 双库备份/恢复验证
使用了分库账号以及 `PROCESS`、`SHOW_ROUTINE`，并保留 routine/trigger dump。

`--no-tablespaces` 会从输出中省略
`CREATE LOGFILE GROUP` / `CREATE TABLESPACE` 语句，并可去掉 `PROCESS` 这一项要求；这会
改变 dump 内容，当前脚本没有启用该选项，需在目标 MySQL 版本重新验证权限及恢复结果后再改。

Compose 服务默认回退到应用数据库账号：元数据库使用 `MYSQL_USER` / `MYSQL_PASSWORD`，
数仓使用 `WAREHOUSE_USER` / `WAREHOUSE_PASSWORD`，并分别连接 `mysql` 和
`mysql_warehouse`。生产启用前应配置前文列出的独立 `BACKUP_*` 账号变量。脚本读取已设置的环境变量，只有调用环境未设置的项才从
项目根 `.env` 补充。可用 `BACKUP_ENV_FILE=/path/to/file` 指定 dotenv 文件，
设置 `BACKUP_ENV_FILE=""` 可完全跳过读取。dotenv 按数据解析，不执行 shell 代码。
口令只写入权限为 `0600` 的临时 MySQL defaults 文件，不进入命令行或日志。
备份服务只在启用 `backup` profile 后运行；停用时执行：

```bash
docker compose --profile backup stop backup
docker compose --profile backup rm -f backup
```

脚本会先完整生成并校验两个 gzip 文件，再发布结果；配对模式同时验证分钟 tar 和清单，
逐一 fsync payload 和 staging，再原子发布目录。任一 dump 或文件校验失败都清理本次临时
文件，不保留可被当作成功的半成品。`BACKUP_RETENTION_DAYS` 必须是正整数。

### 附录：binlog（可选，用于收紧 RPO）

在 MySQL 配置中启用：

```ini
[mysqld]
log-bin = mysql-bin
binlog-expire-logs-seconds = 604800
binlog-format = ROW
```

Compose 中的两个 MySQL 服务均已配置 7 天 ROW binlog。binlog 用于缩短恢复点，
但当前只配置本机保留；异机复制与时间点恢复流程仍需单独部署和演练。

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

若恢复点包含 D13 分钟归档，必须在后端启动前把同一恢复点的完整
`DATA_DIR/minute_archive` 文件快照恢复到配置的归档根目录，然后再恢复 metadata SQL 并执行
`alembic upgrade head`。分钟查询要求 Parquet 文件与 `minute_archive_shards` 索引一致；MySQL
binlog 不覆盖文件内容。配对模式使用同一个 snapshot 目录的 SQL 和 tar，先核对清单大小与
SHA-256，再安全解包到空目录（拒绝绝对路径、`..`、符号链接与非普通文件），核对逐文件
清单后替换归档根目录。修复文件所有权以匹配后端应用账号，再核对索引、行数、修订值和时间戳。

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
当前双库逻辑备份不含分钟 Parquet，分钟归档的文件备份容量、保留期和 RPO 尚未验证。
建议为数据库备份与分钟文件快照分别预算容量并纳入磁盘水位告警（`FR-14` 告警矩阵）。

## 6. 当前状态

- ✅ 仓库内每日触发器已交付：`docker compose --profile backup up -d --build backup`（可选 profile、独立双库凭证、dotenv 环境优先、临时凭证文件和失败清理）
- ⚠️ 脚本只备份 metadata 与 warehouse MySQL。D13 的 `DATA_DIR/minute_archive` 文件快照必须另行配置，并与同一恢复点的 metadata 备份配对；当前未验证分钟文件 RPO。
- ⚠️ Compose 备份服务的默认凭证回退到应用数据库账号；当前命令需要默认应用账号通常没有的全局 `PROCESS` 与 `SHOW_ROUTINE`。配置分库备份账号后再启用 profile。
- ✅ 恢复步骤与演练检查单已成文
- ✅ **恢复演练已于 2026-09-22 执行一次并通过**：备份 → 隔离库恢复 → 9/9 对象行数一致 → 应用指向恢复库 `/health` 通过。
  原始输出与发现的问题见 `docs/evidence/A0/restore-drill.txt`
- ⚠️ 该次演练同时暴露并修复了 8 个既有缺陷（含"应用只能处理第一个请求"的连接池缺陷与"启动即崩溃"的
  初始化幂等缺陷），并在 A4 待办中记录了 3 项发现（生产启动仍写库、`ENABLE_SCHEDULER` 未生效等）
- ⏳ 未覆盖：binlog 时间点恢复演练；A4 引入 ods/dwd 后的体量级恢复耗时
