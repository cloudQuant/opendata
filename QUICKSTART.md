# opendata 快速开始

opendata 提供双库数据中台、Web 数据目录、REST 查询及 WebSocket 更新通知。
源码采用 BSL 1.1；搬运子树的许可与数据使用限制见 [LICENSE](LICENSE)、
[数据权利登记](docs/data-rights-registry.md)。

## 1. 安装并配置双库

前提：Python 3.10 以上、Node.js，以及两个已经创建的 MySQL 8 兼容数据库。
元数据库存用户、任务、执行记录和索引；仓库数据库存 ODS/DWD。
对已有数据库应用迁移前，按[备份恢复手册](docs/operations-backup-restore.md)准备备份。

在本仓库根目录执行。本机 Python 命令统一使用 Anaconda `base`：

```bash
/Users/yunjinqi/opt/anaconda3/bin/conda run -n base python -m pip install -e . -e ./opendata_client
cp -n .env.example .env
```

编辑 `.env`，配置这些实际生效的键；已有 `.env` 应保留并核对，不能覆盖现有凭据。

| 配置 | 本地设置 |
|---|---|
| `MYSQL_HOST/PORT/USER/PASSWORD/DATABASE` | 元数据库连接 |
| `DATA_MYSQL_HOST/PORT/USER/PASSWORD/DATABASE` | 独立仓库连接 |
| `APP_ENV` | `development` |
| `ENABLE_SCHEDULER` | `false`，先完成查询验证再启用调度 |
| `WORKERS` | `1` |
| `SECRET_KEY` | 自行生成的随机密钥 |
| `REDIS_URL` | 单进程本地可留空 |

完整键名、默认值及生效方式见[配置项清单](docs/配置项清单.md)。
数据库 URL 由上述字段构造，配置 `DATABASE_URL` 不会替代双库字段。

```bash
/Users/yunjinqi/opt/anaconda3/bin/conda run -n base python -m opendata.cli generate-secret
/Users/yunjinqi/opt/anaconda3/bin/conda run -n base python -m alembic upgrade head
/Users/yunjinqi/opt/anaconda3/bin/conda run -n base python -m alembic -c alembic_data.ini upgrade head
/Users/yunjinqi/opt/anaconda3/bin/conda run -n base python -m opendata.cli create-admin --user local_admin
```

`create-admin` 交互输入密码；没有固定的默认登录密码。不要使用旧文档中的 `admin123`。

## 2. 启动 API 和前端

```bash
/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
  python -m uvicorn opendata.main:app --host 127.0.0.1 --port 8000
```

另开终端，在 `frontend` 目录运行：

```bash
npm ci
npm run dev -- --host 127.0.0.1
```

前端默认地址为 `http://127.0.0.1:5173`，API 文档为 `http://127.0.0.1:8000/docs`。
用上一步创建的 `local_admin` 登录，进入“数据目录”查看各域的标的、时间范围和数据源状态。
调度关闭时 `/health` 可以显示 scheduler degraded，这个读数不等于数据库连接失败。

所有新调用使用 `/api/v1`。JSON 登录请求如下，密码填交互创建时选择的值：

```http
POST /api/v1/auth/login
Content-Type: application/json

{"username":"local_admin","password":"<your chosen password>"}
```

迁移只创建结构，不会自动生成历史金融数据。空目录应先按
[DWD 回填手册](docs/operations-dwd-backfill.md)准备已经落地的 ODS，
再使用明确的数据源与日期窗口回填；分钟线按[分钟归档手册](docs/operations-minute-archive.md)导入。

## 3. 消费方 golden path：Key → 客户端 → 查询 → 回测输入

用登录获得的 JWT 调用下列接口，创建仅允许股票日线的消费方 Key：

```http
POST /api/v1/keys/
Authorization: Bearer <login JWT>
Content-Type: application/json

{"name":"local-consumer","scopes":["stock_daily"],"rate_limit":100}
```

响应 `key` 只出现一次；消费方使用该值，不能使用供应商的 FUYAO/FRED Key。
域权限为空列表表示全部拒绝，`["*"]` 才表示全部域。
用 `Authorization: Bearer <consumer key>` 或 `X-API-Key: <consumer key>` 查询：

```http
GET /api/v1/data/equity/stock_daily?symbols=600519&start=2024-01-01&end=2024-03-31&adjust=none&page_size=500
X-API-Key: <consumer key>
```

日期窗口应覆盖已经落地的 DWD 行。403 表示域权限不足；401 表示认证失效；
429 表示消费方 Key 达到请求限额，应按 `Retry-After` 等待后重试。

将 Key 放入当前终端的 `OPENDATA_API_KEY` 环境变量，运行客户端示例：

```bash
export OPENDATA_BASE_URL=http://127.0.0.1:8000
/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
  python examples/consumer_handoff.py --symbol 600519 --start 2024-01-01 --end 2024-03-31 --adjust none --no-backtest
```

示例输出调用次数、行数、日期范围和耗时；空数据会返回非零退出码。
若需要运行示例回测，先安装 `backtrader`，然后去掉 `--no-backtest`。
[examples/consumer_handoff.py](examples/consumer_handoff.py) 将客户端结果整理为回测输入，
默认 `none` 保留不复权日线。qfq/hfq 的业务口径及正式官方对照仍须按迭代验收条目处理。

这条示例链路属于验收文档 §0 允许的降级消费方验证。
真实 `backtrader_web` 项目的适配、全市场数据与平台回测验收仍需单独证据，不能由示例通过推定。
历史 A5 降级记录见 [A5 说明](docs/evidence/A5/README.md)，本轮状态见 [C64 验收](docs/evidence/C64/README.md)。

## 4. 运维入口

- [流水线任务](docs/operations-pipeline-tasks.md)：Admin 创建 pipeline 任务，支持域/数据源/标的配置，任务从数据库重载。
- [断点续跑](docs/operations-pipeline-resume.md)：ODS 水位、分片进度和下游步骤检查点。
- [巡检](docs/operations-provider-patrol.md)：默认关闭，P0 范围显式登记，18:00 UTC 尚待业务校准。
- [原始响应缓存](docs/operations-raw-response-cache.md)：默认关闭，缓存不充当水位。
- [查询与 CSV](docs/operations-data-query.md)：ODS 原生字段、DWD 契约字段和有界查询。
- [部署](DEPLOYMENT.md)、[备份恢复](docs/operations-backup-restore.md)：生产调度需显式配置；MySQL 备份不包含分钟 Parquet。

本轮完整质量检查和隔离数据库验收的命令见
[C64 本地复现](docs/evidence/C64/reproduce-local-acceptance.md)。
