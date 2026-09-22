# opendata 项目架构文档

## 项目概述

opendata 是一个多源金融数据中台（FastAPI + Vue3），把各数据源——内化的 akshare 源码（MIT）、同花顺扶摇 API、海外 provider——的获取、落库与查询统一为 REST/WS 服务。

## 技术栈

### 后端
- **框架**: FastAPI 0.104+
- **数据库**: SQLite (开发) / MySQL (生产)
- **ORM**: SQLAlchemy 2.0 (async)
- **认证**: JWT + bcrypt
- **任务调度**: APScheduler (async support)
- **CLI**: Typer
- **测试**: pytest + pytest-asyncio

### 前端
- **框架**: Vue 3.5+
- **构建**: Vite 6+
- **UI**: Element Plus 2.9+
- **状态管理**: Pinia 2.2+
- **路由**: Vue Router 4.5+
- **HTTP**: Axios
- **语言**: TypeScript 5.7+

## 项目结构

```
opendata/
├── opendata/                          # 后端应用
│   ├── __init__.py
│   ├── main.py                   # FastAPI 应用入口
│   ├── cli.py                    # 命令行接口
│   │
│   ├── api/                      # API 路由层
│   │   ├── __init__.py            # API 路由聚合器
│   │   ├── auth.py                # 认证端点 (登录/注册/登出/刷新令牌)
│   │   ├── users.py               # 用户管理端点 (管理员)
│   │   ├── tasks.py               # 定时任务端点
│   │   ├── scripts.py             # 数据脚本端点
│   │   ├── executions.py          # 执行记录端点
│   │   ├── tables.py               # 数据表端点
│   │   ├── data.py                 # 数据下载端点
│   │   ├── interfaces.py           # 接口浏览端点
│   │   ├── settings.py             # 系统设置端点
│   │   ├── dependencies.py         # 依赖注入 (get_db, get_current_user等)
│   │   ├── rate_limit.py           # API 速率限制
│   │   └── schemas.py              # Pydantic 请求/响应模型
│   │
│   ├── core/                     # 核心功能
│   │   ├── config.py               # 配置管理 (环境变量、数据库URL等)
│   │   ├── database.py             # 数据库连接 (引擎、会话工厂、迁移)
│   │   └── security.py             # 安全功能 (哈希、JWT、权限验证)
│   │
│   ├── models/                   # SQLAlchemy 数据模型
│   │   ├── __init__.py             # 模型导出
│   │   ├── user.py                 # User, UserRole 枚举
│   │   ├── task.py                 # ScheduledTask, TaskExecution, TaskStatus 枚举, TriggeredBy 枚举
│   │   ├── data_script.py          # DataScript, ScriptFrequency 枚举
│   │   ├── data_table.py           # DataTable (数据表元信息)
│   │   └── interface.py            # DataInterface, InterfaceCategory, InterfaceParameter, ParameterType 枚举
│   │
│   ├── services/                 # 业务逻辑层
│   │   ├── __init__.py
│   │   ├── scheduler_service.py    # APScheduler 封装
│   │   ├── scheduler.py             # TaskScheduler (任务调度、执行包装)
│   │   ├── script_service.py       # 数据脚本 CRUD
│   │   ├── execution_service.py    # 执行记录 CRUD
│   │   ├── data_acquisition.py     # 数据采集编排
│   │   ├── retry_service.py        # 失败重试逻辑
│   │   └── interface_loader.py     # akshare 接口扫描和注册
│   │
│   ├── data_fetch/               # 数据采集模块
│   │   ├── providers/
│   │   │   └── akshare_provider.py # AkshareProvider 基类
│   ├── scripts/                  # 数据获取脚本 (按类别/频率组织)
│   │   ├── stocks/daily/          # 股票日线数据
│   │   ├── funds/                 # 基金数据
│   │   ├── futures/               # 期货数据
│   │   ├── indexes/               # 指数数据
│   │   ├── options/               # 期权数据
│   │   ├── bonds/                 # 债券数据
│   │   ├── currencies/            # 汇率数据
│   │   ├── cryptos/               # 加密货币数据
│   │   ├── common/                # 通用数据
│   │   └── configs/               # 数据获取配置
│   │
│   └── utils/                    # 工具函数
│       ├── helpers.py             # 辅助函数 (表名生成、列名清理)
│       └── validators.py           # 验证函数 (email、schedule、password)
│
├── frontend/                     # Vue3 前端应用
│   ├── src/
│   │   ├── api/                   # API 客户端封装
│   │   │   ├── auth.ts              # 认证 API
│   │   │   ├── users.ts             # 用户 API
│   │   │   ├── tasks.ts             # 任务 API
│   │   │   ├── scripts.ts           # 脚本 API
│   │   │   ├── executions.ts        # 执行记录 API
│   │   │   ├── tables.ts            # 数据表 API
│   │   ├── data.ts               # 数据下载 API
│   │   ├── settings.ts           # 设置 API
│   │   │   └── interfaces.ts        # 接口 API
│   │   │
│   │   ├── stores/                # Pinia 状态管理
│   │   │   └── auth.ts              # 认证状态 (token, user info, 登录状态)
│   │   │
│   │   ├── router/                # Vue Router 配置
│   │   │   └── index.ts             # 路由定义 + 权限守卫
│   │   │
│   │   ├── views/                 # 页面组件
│   │   │   ├── LoginView.vue         # 登录页
│   │   │   ├── RegisterView.vue     # 注册页
│   │   │   ├── LayoutView.vue       # 主布局 (带导航栏、侧边栏)
│   │   │   ├── HomeView.vue          # 首页仪表板
│   │   │   ├── ScriptsView.vue       # 数据接口列表
│   │   │   ├── ScriptDetailView.vue  # 接口详情/测试接口
│   │   │   ├── TasksView.vue         # 定时任务管理
│   │   │   ├── ExecutionsView.vue    # 执行记录查询
│   │   │   ├── TablesView.vue        # 数据表管理
│   │   │   ├── TableDetailView.vue   # 数据表详情/预览
│   │   │   ├── UsersView.vue         # 用户管理 (管理员)
│   │   │   ├── SettingsView.vue      # 系统设置 (管理员)
│   │   │   ├── NotFoundView.vue      # 404 页面
│   │   │   └── admin/
│   │   │       └── InterfaceManagement.vue  # 接口管理 (管理员)
│   │   │
│   │   ├── components/            # 可复用组件
│   │   │   ├── common/              # 通用组件
│   │   │   └── ...
│   │   │
│   │   ├── types/                 # TypeScript 类型定义
│   │   │   └── index.ts              # 全局类型
│   │   │
│   │   ├── utils/                  # 工具函数
│   │   │   ├── request.ts           # Axios 封装 (拦截器、错误处理)
│   │   │   └── ...
│   │   │
│   │   ├── App.vue                # 根组件
│   │   └── main.ts                # 应用入口
│   │
│   ├── package.json                # 前端依赖
│   ├── vite.config.ts              # Vite 配置
│   └── tsconfig.json               # TypeScript 配置
│   │
├── tests/                        # 测试套件
│   ├── conftest.py                 # pytest 配置和 fixtures
│   ├── test_auth.py                # 认证测试
│   ├── test_api.py                 # API 测试
│   ├── test_utils.py               # 工具函数测试
│   ├── test_core.py                # 核心功能测试
│   ├── test_schemas.py             # Schema 测试
│   ├── test_config.py               # 配置测试
│   ├── test_cli.py                  # CLI 测试
│   ├── test_scheduler.py            # 调度器测试
│   ├── test_retry_service.py        # 重试服务测试
│   ├── test_retry_utils.py          # 重试工具测试
│   └── ...                         # 更多测试文件
│
├── alembic/                       # 数据库迁移
│   ├── versions/                   # 迁移脚本
│   └── env.py                      # Alembic 配置
│
├── akshare/                      # 内嵌的 akshare 源码（迭代 A2 起更名为 opendata_http/）
│   ├── stock/                     # 股票数据
│   ├── fund/                      # 基金数据
│   ├── futures/                   # 期货数据
│   ├── macro/                     # 宏观数据
│   └── ...                        # 其他类别
│
├── requirements.txt               # Python 依赖
├── pytest.ini                     # pytest 配置
├── docker-compose.yml             # Docker 编排
└── README.md                     # 项目说明
```

## 核心模块说明

### 1. API 路由层 (`opendata/api/`)

| 文件 | 功能 | 端点前缀 |
|------|------|----------|
| `auth.py` | 用户认证 | `/api/auth` |
| `users.py` | 用户管理 | `/api/users` |
| `tasks.py` | 定时任务 | `/api/tasks` |
| `scripts.py` | 数据脚本 | `/api/scripts` |
| `executions.py` | 执行记录 | `/api/executions` |
| `tables.py` | 数据表 | `/api/tables` |
| `data.py` | 数据下载 | `/api/data` |
| `interfaces.py` | 接口浏览 | `/api/interfaces` |
| `settings.py` | 系统设置 | `/api/settings` |

### 2. 数据模型 (`opendata/models/`)

| 模型 | 说明 |
|------|------|
| `User` | 用户表 (username, email, hashed_password, role, is_active) |
| `DataScript` | 数据脚本表 (script_id, script_name, category, frequency, module_path) |
| `DataTable` | 数据表元信息 (table_name, display_name, row_count, size_bytes, schema_info) |
| `DataInterface` | 数据接口定义 (name, display_name, category, parameters) |
| `InterfaceCategory` | 接口分类 (name, description, icon, sort_order) |
| `ScheduledTask` | 定时任务 (name, script_id, schedule_type, schedule_expression, is_active, retry_on_failure) |
| `TaskExecution` | 执行记录 (execution_id, task_id, status, start_time, end_time, result, error_message) |

### 3. 服务层 (`opendata/services/`)

| 服务 | 功能 |
|------|------|
| `SchedulerService` | APScheduler 封装，提供任务增删改查 |
| `TaskScheduler` | 高级调度器，集成任务执行、重试逻辑 |
| `ScriptService` | 数据脚本 CRUD 操作 |
| `ExecutionService` | 执行记录 CRUD 和统计 |
| `DataAcquisitionService` | 数据采集编排，管理数据获取和存储 |
| `RetryService` | 失败重试逻辑，指数退避 |
| `InterfaceLoader` | akshare 接口扫描和自动注册 |

### 4. 前端路由 (`frontend/src/router/`)

| 路径 | 组件 | 权限要求 |
|------|------|----------|
| `/login` | LoginView.vue | 公开 |
| `/register` | RegisterView.vue | 公开 |
| `/` | LayoutView (children) | 需登录 |
| `/` (home) | HomeView.vue | 需登录 |
| `/scripts` | ScriptsView.vue | 需登录 |
| `/scripts/:id` | ScriptDetailView.vue | 需登录 |
| `/tasks` | TasksView.vue | 需登录 |
| `/executions` | ExecutionsView.vue | 需登录 |
| `/tables` | TablesView.vue | 需登录 |
| `/tables/:name` | TableDetailView.vue | 需登录 |
| `/users` | UsersView.vue | 需登录+管理员 |
| `/admin/interfaces` | InterfaceManagement.vue | 需登录+管理员 |
| `/settings` | SettingsView.vue | 需登录+管理员 |

## 数据流

### 数据采集流程

```
1. ScriptService 创建 DataScript 记录
2. SchedulerService 根据 schedule_type/schedule_expression 创建 APScheduler Job
3. 到达执行时间，TaskScheduler._execute_task_wrapper 被调用
4. ExecutionService.create_execution() 创建 TaskExecution 记录
5. ScriptService.get_script() 获取脚本信息
6. DataAcquisitionService.execute_download() 执行数据获取
   6.1. AkshareProvider.fetch_data() 调用 akshare 函数
   6.2. 数据处理和清洗
   6.3. 创建/更新 DataTable 记录
7. 更新 TaskExecution 状态 (completed/failed)
8. 失败时触发 RetryService 进行重试
```

### 认证流程

```
1. 用户提交用户名/密码到 /api/auth/login
2. Core.security.verify_password() 验证密码
3. Core.security.create_access_token() 和 create_refresh_token() 生成 JWT
4. 前端存储 token 到 Pinia store
5. 后续请求在 Header 中携带 Authorization: Bearer <token>
6. API 中间件验证 token (dependencies.get_current_user)
```

## API 端点汇总

### 认证相关 (`/api/auth`)
- `POST /register` - 用户注册
- `POST /login` - 用户登录
- `POST /logout` - 用户登出
- `POST /refresh` - 刷新访问令牌
- `GET /me` - 获取当前用户信息

### 用户管理 (`/api/users`) - 管理员
- `GET /` - 获取用户列表 (分页、筛选)
- `GET /{user_id}` - 获取用户详情
- `PUT /{user_id}` - 更新用户
- `DELETE /{user_id}` - 删除用户
- `POST /{user_id}/reset-password` - 重置密码

### 定时任务 (`/api/tasks`)
- `GET /` - 获取任务列表
- `POST /` - 创建任务
- `GET /{task_id}` - 获取任务详情
- `PUT /{task_id}` - 更新任务
- `DELETE /{task_id}` - 删除任务
- `POST /{task_id}/execute` - 手动触发执行
- `POST /{task_id}/pause` - 暂停任务
- `POST /{task_id}/resume` - 恢复任务
- `GET /{task_id}/executions` - 获取任务执行记录

### 数据脚本 (`/api/scripts`)
- `GET /` - 获取脚本列表
- `POST /scan` - 扫描并注册新脚本
- `GET /{script_id}` - 获取脚本详情
- `PUT /{script_id}` - 更新脚本
- `DELETE /{script_id}` - 删除脚本
- `POST /{script_id}/test` - 测试脚本
- `PATCH /{script_id}/toggle` - 切换启用状态

### 执行记录 (`/api/executions`)
- `GET /` - 获取执行记录列表
- `GET /{execution_id}` - 获取执行详情
- `POST /{execution_id}/cancel` - 取消执行
- `POST /{execution_id}/retry` - 重试执行

### 数据表 (`/api/tables`)
- `GET /` - 获取数据表列表
- `GET /{table_name}` - 获取表详情
- `GET /{table_name}/schema` - 获取表结构
- `GET /{table_name}/data` - 获取表数据 (分页)
- `GET /{table_name}/export` - 导出表数据 (csv/excel)

### 数据下载 (`/api/data`)
- `POST /download` - 手动下载数据
- `GET /download/{execution_id}/status` - 获取下载状态
- `GET /download/{execution_id}/result` - 获取下载结果

### 接口浏览 (`/api/interfaces`)
- `GET /` - 获取接口列表
- `GET /categories` - 获取分类列表
- `GET /{interface_id}` - 获取接口详情
- `GET /{interface_id}/parameters` - 获取接口参数

### 系统设置 (`/api/settings`) - 管理员
- `GET /` - 获取系统设置
- `PUT /` - 更新系统设置
- `GET /database` - 获取数据库配置
- `PUT /database` - 更新数据库配置
- `POST /database/test` - 测试数据库连接
- `GET /scheduler` - 获取调度器状态

## 枚举类型

### UserRole (`opendata/models/user.py`)
- `ADMIN` - 管理员
- `USER` - 普通用户

### TaskStatus (`opendata/models/task.py`)
- `PENDING` - 等待执行
- `RUNNING` - 正在执行
- `COMPLETED` - 执行完成
- `FAILED` - 执行失败
- `CANCELLED` - 已取消
- `TIMEOUT` - 超时

### ScheduleType (`opendata/models/task.py`)
- `ONCE` - 一次性
- `DAILY` - 每日
- `WEEKLY` - 每周
- `MONTHLY` - 每月
- `CRON` - Cron表达式
- `INTERVAL` - 间隔执行

### ScriptFrequency (`opendata/models/data_script.py`)
- `MANUAL` - 手动执行
- `HOURLY` - 每小时
- `DAILY` - 每天
- `WEEKLY` - 每周

### TriggeredBy (`opendata/models/task.py`)
- `MANUAL` - 手动触发
- `SCHEDULER` - 调度器触发
- `RETRY` - 重试触发

## 配置项

### 环境变量 (.env)
```bash
# 应用配置
APP_NAME=opendata
DEBUG=false
SECRET_KEY=your-secret-key-change-in-production
ACCESS_TOKEN_EXPIRE_MINUTES=30
REFRESH_TOKEN_EXPIRE_DAYS=7

# 数据库配置
DATABASE_URL=sqlite:///./opendata.db
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_DATABASE=opendata
MYSQL_USER=root
MYSQL_PASSWORD=

# CORS
CORS_ORIGINS=http://localhost:5173
```

## 开发指南

### 添加新的数据获取脚本

1. 在 `opendata/data_fetch/scripts/` 对应分类下创建脚本文件
2. 继承 `AkshareProvider` 基类
3. 实现 `fetch_data(**kwargs)` 方法
4. 返回 pandas DataFrame

### 添加新的 API 端点

1. 在 `opendata/api/` 对应模块中添加路由函数
2. 在 `opendata/api/schemas.py` 中添加请求/响应模型
3. 在 `opendata/api/dependencies.py` 中添加权限依赖

### 添加新的前端页面

1. 在 `frontend/src/views/` 创建 Vue 组件
2. 在 `frontend/src/router/index.ts` 中添加路由
3. 在 `frontend/src/api/` 中添加 API 调用函数
4. 在 LayoutView 侧边栏中添加菜单项

### APScheduler 升级路径 (v3 → v4)

当前使用 APScheduler 3.x，其 `AsyncIOScheduler` 是半异步的（内部仍有同步锁）。
APScheduler 4.x 提供原生 async/await 支持和更好的持久化（支持 SQLAlchemy async job store）。

**升级步骤**（未来考虑）：
1. 升级依赖：`apscheduler>=4.0`
2. 替换 `AsyncIOScheduler` → `AsyncScheduler`
3. 替换 `CronTrigger.from_crontab()` → `CronTrigger()`（API 略有变化）
4. 移除 `AsyncIOExecutor`（v4 不再需要独立 executor）
5. 可选：配置 SQLAlchemy async job store 实现任务持久化
6. 涉及文件：`opendata/services/scheduler_service.py`、`opendata/services/scheduler.py`
