# opendata 快速开始指南

## 项目概述

opendata 是一个多源金融数据中台，提供 Web 界面和 REST/WS API 来统一获取、落库与查询金融数据。

## 技术栈

- **后端**: FastAPI + Pydantic + SQLAlchemy + Alembic
- **前端**: Vue3 + TypeScript + Vite (待开发)
- **数据库**: MySQL (核心), SQLite (开发/测试)
- **任务调度**: APScheduler
- **数据源**: akshare（MIT，源码级内化）、同花顺扶摇 API、海外 provider

## 快速开始

### 1. 安装

```bash
# 克隆项目
cd /Users/yunjinqi/Documents/source_code/opendata

# 运行开发启动脚本
./dev_start.sh
```

### 2. 配置环境

编辑 `.env` 文件：

```bash
# 数据库配置
DATABASE_URL=sqlite+aiosqlite:///./data/opendata.db  # 开发用 SQLite
# DATABASE_URL=mysql+pymysql://root:password@localhost:3306/opendata  # 生产用 MySQL

# 安全配置
SECRET_KEY=your-secret-key-here

# 服务器配置
HOST=0.0.0.0
PORT=8000
```

### 3. 启动应用

```bash
# 方式1: 使用启动脚本
./start_app.sh

# 方式2: 直接运行 uvicorn
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 方式3: 使用 CLI
opendata run
```

### 4. 访问应用

- API 文档: http://localhost:8000/docs
- 健康检查: http://localhost:8000/health
- ReDoc 文档: http://localhost:8000/redoc

## 默认账户

首次启动后，系统会自动创建管理员账户：

- **用户名**: admin
- **密码**: admin123
- **邮箱**: admin@opendata.com

⚠️ **重要**: 生产环境请立即修改默认密码！

## CLI 命令

```bash
# 初始化数据库
opendata init-db

# 创建管理员用户
opendata create-admin

# 生成密钥
opendata generate-secret

# 加载数据能力清单
opendata load-interfaces

# 健康检查
opendata check-health

# 重置数据库（删除所有数据）
opendata reset-db
```

## API 使用示例

### 1. 用户注册/登录

```bash
# 注册
curl -X POST "http://localhost:8000/api/auth/register" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "testuser",
    "email": "test@example.com",
    "password": "password123",
    "full_name": "Test User"
  }'

# 登录
curl -X POST "http://localhost:8000/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "testuser",
    "password": "password123"
  }'
```

### 2. 获取数据接口列表

```bash
curl -X GET "http://localhost:8000/api/data/interfaces?page=1&page_size=20" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

### 3. 创建定时任务

```bash
curl -X POST "http://localhost:8000/api/tasks" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "每日A股数据",
    "description": "每天收盘后获取A股数据",
    "interface_id": 1,
    "schedule_type": "daily",
    "schedule_expression": "15:00",
    "parameters": {},
    "is_active": true
  }'
```

### 4. 手动触发数据下载

```bash
curl -X POST "http://localhost:8000/api/data/download" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "interface_id": 1,
    "parameters": {}
  }'
```

## 项目结构

```
opendata/
├── opendata/
│   ├── api/              # API 路由
│   │   ├── auth.py       # 认证接口
│   │   ├── interfaces.py # 数据接口管理
│   │   ├── tasks.py      # 定时任务管理
│   │   ├── data.py       # 数据下载
│   │   ├── tables.py     # 数据表管理
│   │   └── users.py      # 用户管理
│   ├── core/            # 核心功能
│   │   ├── config.py     # 配置管理
│   │   ├── database.py   # 数据库连接
│   │   └── security.py   # 安全相关
│   ├── models/          # 数据模型
│   │   ├── user.py       # 用户模型
│   │   ├── interface.py  # 接口模型
│   │   ├── task.py       # 任务模型
│   │   └── data_table.py # 数据表模型
│   ├── services/        # 业务逻辑
│   │   ├── scheduler.py      # 任务调度器
│   │   ├── data_acquisition.py  # 数据获取服务
│   │   └── interface_loader.py  # 接口加载服务
│   ├── utils/           # 工具函数
│   ├── main.py          # FastAPI 应用入口
│   └── cli.py           # 命令行接口
├── alembic/             # 数据库迁移
├── tests/               # 测试文件
├── frontend/            # Vue3 前端 (待开发)
├── start_app.sh         # 启动脚本
├── restart_app.sh       # 重启脚本
├── dev_start.sh         # 开发启动脚本
└── pyproject.toml       # 项目配置
```

## 开发

### 运行测试

```bash
# 运行所有测试
pytest

# 运行测试并显示覆盖率
pytest --cov=app --cov-report=html

# 运行特定测试文件
pytest tests/test_auth.py -v
```

### 代码格式化

```bash
# 格式化代码
black opendata/

# 类型检查
mypy opendata/

# Lint 检查
ruff check opendata/
```

### 数据库迁移

```bash
# 创建新迁移
alembic revision --autogenerate -m "描述"

# 应用迁移
alembic upgrade head

# 回滚迁移
alembic downgrade -1
```

## 生产部署

### 1. 使用 Docker

```bash
# 构建镜像
docker build -t opendata .

# 运行容器
docker run -d \
  -p 8000:8000 \
  -e DATABASE_URL=mysql+pymysql://user:pass@host:3306/db \
  -e SECRET_KEY=your-secret-key \
  --name opendata \
  opendata
```

### 2. 使用 systemd

创建 `/etc/systemd/system/opendata.service`:

```ini
[Unit]
Description=opendata Application
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/path/to/opendata
Environment="PATH=/path/to/opendata/venv/bin"
ExecStart=/path/to/opendata/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

## 故障排查

### 数据库连接失败

检查 `.env` 中的 `DATABASE_URL` 是否正确。

### 任务没有执行

检查:
1. 任务是否设置为 `is_active=True`
2. 调度器是否正在运行: 访问 `/health` 端点
3. 查看日志文件 `logs/app.log`

### 接口调用失败

检查:
1. opendata 包是否正确安装（`pip install -e .`）
2. 网络连接是否正常
3. 查看错误日志获取详细信息

## 后续开发计划

- [x] Vue3 前端界面开发
- [ ] MongoDB 支持
- [ ] DolphinDB 支持
- [ ] 数据任务依赖关系
- [ ] 数据共享功能
- [ ] 付费云同步功能

## 生产部署

详细部署文档请参考 [DEPLOYMENT.md](./DEPLOYMENT.md)，包含:
- Docker 部署指南
- Systemd 服务配置
- Nginx 反向代理配置
- 数据库备份策略
- 监控与日志配置
- 安全加固建议

快速部署（Docker）:

```bash
# 使用 docker-compose 部署
docker-compose up -d

# 访问应用
# 前端: http://localhost
# API: http://localhost:8000
# API 文档: http://localhost:8000/docs
```

## 许可证

MIT License
