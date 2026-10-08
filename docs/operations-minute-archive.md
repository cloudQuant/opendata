# 本地分钟线归档运维

## 存储边界

分钟线由操作员导入本地 CSV 后保存为 Parquet，不调用行情 provider。当前支持：

| asset_class | domain |
|-------------|--------|
| `equity` | `stock_daily` |
| `index` | `index_daily` |
| `futures` | `futures_daily` |
| `option` | `option_daily` |

查询记录保存在文件系统；主库 `minute_archive_shards` 表仅保存每个
`domain/symbol/source/period/UTC day` shard 的路径、行数、时间范围和 SHA-256。分钟线不写入
warehouse MySQL。默认根目录为 `DATA_DIR/minute_archive`；Compose 后端的默认挂载位置是
宿主机 `./data/minute_archive`。

导入或查询之前先将主库迁移到 head（包含 `0004`）：

```bash
/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
  alembic upgrade head
```

`minute_archive_shards` 缺表时，导入、查询或启用文件保留清理会报错，不会伪装为空结果。

## CSV 导入

CSV 必须包含且仅包含以下字段，字段顺序固定；时间戳必须包含时区。按 `symbol`、再按
时间升序排列，使工具按一个 symbol/day 提交有限 shard：

```csv
symbol,timestamp,open,high,low,close,volume,amount
AAPL,2026-09-29T09:30:00-04:00,100,102,99,101,1200,121200
```

运行示例：

```bash
/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
  python scripts/ops/import_minute_archive.py \
  --input ./imports/stock-minute.csv \
  --domain stock_daily \
  --source operator-feed-a \
  --period 1m
```

可用 `--archive-root` 为隔离导入指定文件根目录。重复导入同一时间戳是幂等 upsert；更新后
主库指向新的不可变 Parquet 文件，旧版本保留到该 shard 过期，以保护并发读取和较早的
数据库/文件快照。每个 shard 单独原子发布；若 CSV 后续行无效，之前已提交的 shard 仍在，
修正后重跑即可。

文件布局为 `domain/symbol/year/YYYY-MM-DD_source_period_sha256.parquet`。路径标识拒绝
斜杠、`..` 和 `source=auto`；存档根或路径中的符号链接会被拒绝。API、导入器和保留清理
使用相同的本机锁目录 `minute_archive/.locks` 协调同一 shard，同时持有全局
`.snapshot.lock` 的共享锁；配对备份持有全局排他锁。锁使用进程内互斥及 POSIX
文件锁；这覆盖同一宿主机上的 API、导入进程、retention 与备份。归档根应放在单宿主机本地文件
系统上；共享文件系统上的跨主机锁语义尚未验证，也没有分布式锁或多主机写入保证。

## REST 查询

```text
GET /api/v1/data/minute/{asset_class}/{domain}
```

请求需提供重复的 `symbols` 参数、明确的 `source` 和分钟 `period`。`start` / `end` 是含
首尾的 UTC 日历日期，缺省为当前 UTC 日，窗口最多 31 天；最多 20 个不同 symbol。
`fields` 仅接受 `open,high,low,close,volume,amount` 与身份字段
`symbol,timestamp,period,source`；无论投影如何，四个身份字段始终返回。分页从 1 开始，
`page_size` 最大 1000。响应遵循常规 API envelope，`data.layer` 为 `file`，并含
`effective_window`、`columns`、`rows`、总 `count` 和分页值。

示例：

```text
/api/v1/data/minute/equity/stock_daily?symbols=AAPL&symbols=MSFT&source=operator-feed-a&period=1m&start=2026-09-01&end=2026-09-07&fields=close&page=1&page_size=500
```

接口沿用现有用户/API key 认证和 `require_domain_access` 域授权。一次查询按 UTC day 读取
至多一天的 shard，Parquet 读取使用时间谓词和字段投影；全页总数来自逐 shard 的有效记录，
损坏、校验和不匹配或丢失的索引文件作为错误返回，不会计作空结果。

## 保留与备份

`RETENTION_MINUTE_YEARS` 控制分钟 shard 的保留年限。周期任务默认 `RETENTION_EXECUTION_ENABLED=false`，
只报告索引候选；核对结果和配对备份方案后，才显式打开执行。执行时先在主库事务中删除过期
shard 索引，再在相同 shard 锁下删除该日/source/period 的规范 Parquet 版本；不会递归删除
任意年度目录，也不会清理未索引的文件。过期索引缺失、文件路径错误或物理清理失败都会显示
在维护结果中。日线、财务、ODS/DWD 与其它永久表不进入分钟文件清理。

设置 `BACKUP_MINUTE_ARCHIVE_DIR` 后，双库逻辑备份会将 MySQL dump、全部分钟文件及
SHA-256 清单配对发布；Compose 的可选 backup profile 默认使用同一归档目录。
MySQL binlog 不包含文件系统改动。恢复 D13 必须将同一个 snapshot 目录的分钟 tar 和
主库 SQL 成对恢复、核对逐文件清单，并先应用主库迁移，再启动 API。完整备份期间查询、
导入和保留清理会等待全局锁。生产每日运行、异机复制及实际体量恢复尚未验收，RPO 仍未建立。
详见[备份与恢复手册](operations-backup-restore.md)。
