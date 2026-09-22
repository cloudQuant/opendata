# opendata

多源金融数据中台：把各数据源的**获取 → 落库 → 服务化**收敛为一套统一能力，以 REST + WebSocket
对外提供数据服务。

opendata 为 backtrader / ai-for-investor / bt_api_py 等项目提供统一数据支持：消费方只经 API 取数，
不再各自维护取数脚本。

## 当前状态

本仓库由 `akshare_web` 完整拷贝重构而来，正按迭代计划分批交付：

| 迭代 | 目标 | 状态 |
|------|------|------|
| **1A** 地基 + 垂直切片 | A 股日线一条链路端到端可用，消费方取数成功 | 进行中（A0 地基已完成） |
| **1B** 国内数据全覆盖 + 服务化 | A 股四件套 + 期货双源落库，REST/WS + 客户端对外可用 | 未开始 |
| **1C** 海外与宏观 + 验收收口 | 海外/宏观数据可用，上游同步就绪，端到端验收 | 未开始 |

设计文档与验收标准见 [`docs/迭代计划/迭代1-重构数据中台/`](docs/迭代计划/迭代1-重构数据中台/)。

## 三个数据源

| 来源 | 形态 | 许可证 | 本项目采取的动作 |
|------|------|--------|------------------|
| [akshare](https://github.com/akfamily/akshare) | HTTP 爬取各站点 | MIT | **按需源码级搬运**，保留版权声明 |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | provider 插件体系 | AGPL-3.0 | **仅借鉴架构，实现全部自研**（零源码复制） |
| 同花顺扶摇 API | REST（`X-api-key`） | 商业服务 | 自建客户端 `opendata_fuyao/` |

向 akshare 与 OpenBB 的致谢与许可证边界见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

## 功能特性

### 数据中台能力（迭代 1A/1B 交付）

- **统一数据契约**：与数据源无关的标准化模型（行情 / 复权 / 财务 / 指数成分 / 标的元数据 / 交易日历 / 宏观 …）
- **provider 注册与路由**：`source="auto"` 按权威度与可用性自动选择并可降级；未验证能力不参与自动路由
- **数仓分层**：`ods_<domain>_<source>`（逐源原始保真）→ 多源交叉校对 → `dwd_<domain>`（契约统一）
- **跨源校对**：经契约模型归一化后逐字段比对，差异落 `dq_diff_report`，配套白名单/去重/分级告警
- **跑批调度**：数据域 pipeline（取数 → 落库 → 校对 → 合并 → 推送）作为调度单元，支持断点续拉与失败重入
- **服务化**：REST 查询（分页/过滤/导出/复权口径）+ WebSocket 订阅增量 + 最小 Python 客户端
- **新鲜度与告警**：每日检查各域是否含最近交易日数据，缺失即告警

### 既有平台能力（延续可用）

- 数据脚本管理：按分类/频率组织，自动扫描注册，支持自定义脚本
- 定时任务：Cron / Interval 调度，启用/暂停/删除，指数退避自动重试
- 执行记录：状态追踪、行数统计、错误与堆栈、耗时与重试次数、统计趋势
- 用户权限：管理员/普通用户角色、基于角色的访问控制、JWT 认证
- 数据表管理：表列表、表结构、数据预览（适配 ods/dwd 分层）
- Web 界面：Vue3 + Element Plus，响应式，暗色/亮色主题
- 高可用工程：速率限制、结构化日志、健康检查、指标接口

## 快速开始

### Docker（推荐）

```bash
cp .env.example .env
# 必填：MYSQL_ROOT_PASSWORD / MYSQL_PASSWORD / WAREHOUSE_ROOT_PASSWORD /
#       WAREHOUSE_PASSWORD / SECRET_KEY（缺失时 compose 会直接拒绝启动）
docker compose up -d
```

- Web 界面：http://localhost
- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 本地开发

```bash
pip install -e ".[web,dev]"
cp .env.example .env          # 填写数据库与密钥
alembic upgrade head          # 元数据库迁移
python -m opendata.cli init-db
uvicorn opendata.main:app --reload
```

前端：

```bash
cd frontend && npm install && npm run dev   # http://localhost:5173
```

更完整的步骤、生产部署与排障见 [QUICKSTART.md](QUICKSTART.md) 与 [DEPLOYMENT.md](DEPLOYMENT.md)。

## API 使用示例

```bash
# 登录
curl -X POST "http://localhost:8000/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<your-password>"}'

# 数据能力清单（注册表内容）
curl "http://localhost:8000/api/v1/data/capabilities"

# 按数据域查询（迭代 1A 交付，含复权口径）
curl "http://localhost:8000/api/v1/data/stock/daily?symbols=600519&start=2024-01-01&end=2024-12-31&adjust=qfq"

# 创建定时任务
curl -X POST "http://localhost:8000/api/v1/tasks" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"A股日线增量","schedule_type":"daily","schedule_expression":"17:30"}'
```

完整接口清单见 [API.md](API.md)。

## 项目结构

```
opendata/
├── opendata/                  # 主服务包（自研）
│   ├── api/                   # REST / WS 路由
│   ├── core/                  # 配置、数据库、安全
│   ├── models/  services/     # ORM 模型与业务逻辑
│   ├── data/                  # ★ 统一契约与 provider 路由（迭代 1A-A1）
│   ├── pipeline/              # ★ 数据域 pipeline（迭代 1A-A4）
│   └── data_fetch/            # 既有脚本框架（向 provider 体系迁移）
├── akshare/                   # 内嵌 akshare 源码（迭代 A2 起更名为 opendata_http/）
├── opendata_fuyao/            # ★ 同花顺扶摇客户端（迭代 1A-A3）
├── opendata_providers/        # ★ 自研 provider（迭代 1C）
├── opendata_client/           # ★ 最小 Python 客户端（迭代 1B）
├── frontend/                  # Vue3 + TypeScript 前端
├── alembic/                   # 元数据库迁移
├── alembic_data/              # ★ 数据仓库独立迁移环境（迭代 1A-A4）
├── scripts/                   # 运维与 codemod 工具
├── tests/                     # pytest 测试
└── docs/                      # 设计、部署与验收文档
```

`★` 标记的目录在对应迭代里程碑中创建。

## 开发与代码质量

```bash
make check              # ruff + mypy + bandit
make test               # pytest -n 8（默认跳过 e2e）
make test-cov           # 覆盖率门禁 + 报告归档
make quality-ratchet    # 存量债务棘轮检查（只降不升）
make public-api-quality # 公共 API docstring/注解覆盖率
make frontend-lint frontend-typecheck frontend-test
make gate               # 全部门禁聚合，逐项阻断（CI 与本地验收统一入口）
```

质量分层口径（存量棘轮 / 新增零容忍 / 搬运保真）见 [CODE_QUALITY.md](CODE_QUALITY.md)。

## 许可证

本项目采用 **[BSL 1.1](LICENSE)**（Business Source License 1.1）：

- **个人与非商业用途**：免费使用、修改、分发。
- **机构内部使用与商业用途**：需获得 Licensor 书面授权。
- **Change Date**：发布日起 4 年后自动转为 MIT License。

内嵌的 akshare 源码为 MIT 许可，其版权声明与全文见
[`LICENSE-AKSHARE`](LICENSE-AKSHARE) 与 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)；
该部分代码他人仍可按 MIT 条款取用。

商务授权联系：`cloud@example.com`（占位，发布前替换为正式联系方式）。
授权范围为**软件使用许可**，不含数据再分发。

### 数据免责声明

opendata 本身不生产数据。所有数据均来源于第三方站点与商业 API，本项目不主张任何数据权利，
亦不对数据的准确性、完整性与时效性作出保证。消费方须自行确认其数据使用方式符合数据来源方的
条款与适用法律。本项目不构成任何投资建议。
