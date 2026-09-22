# opendata API 文档

## 概述

opendata 提供完整的 RESTful API 用于管理金融数据采集任务。所有 API 请求和响应使用 JSON 格式。

**Base URL**: `http://localhost:8000`

**API 前缀**: `/api`

## 认证

### Bearer Token 认证

大多数端点需要在请求头中携带 JWT 访问令牌：

```
Authorization: Bearer <access_token>
```

### 获取令牌

通过登录或注册端点获取访问令牌。

## API 端点

### 认证相关 (`/api/auth`)

#### POST `/api/auth/register`
注册新用户。

**请求体**:
```json
{
  "username": "string",
  "email": "user@example.com",
  "password": "string",
  "password_confirm": "string"
}
```

**响应** (201):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "access_token": "string",
    "refresh_token": "string",
    "token_type": "bearer",
    "expires_in": 1800
  }
}
```

#### POST `/api/auth/login`
用户登录。

**请求体**:
```json
{
  "username": "string",
  "password": "string"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "access_token": "string",
    "refresh_token": "string",
    "token_type": "bearer",
    "expires_in": 1800
  }
}
```

#### POST `/api/auth/logout`
用户登出。

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

#### POST `/api/auth/refresh`
刷新访问令牌。

**请求体**:
```json
{
  "refresh_token": "string"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "access_token": "string",
    "token_type": "bearer",
    "expires_in": 1800
  }
}
```

#### GET `/api/auth/me`
获取当前用户信息。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "id": 1,
    "username": "string",
    "email": "user@example.com",
    "role": "user",
    "is_active": true
  }
}
```

### 用户管理 (`/api/users`) - 管理员

#### GET `/api/users/`
获取用户列表。

**查询参数**:
- `page` (int, 默认: 1) - 页码
- `page_size` (int, 默认: 20) - 每页数量
- `username` (string, 可选) - 用户名过滤
- `email` (string, 可选) - 邮箱过滤
- `role` (string, 可选) - 角色过滤 (user/admin)

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "items": [...],
    "total": 100,
    "page": 1,
    "page_size": 20
  }
}
```

#### GET `/api/users/{user_id}`
获取用户详情。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "id": 1,
    "username": "string",
    "email": "user@example.com",
    "role": "user",
    "is_active": true,
    "created_at": "2024-01-01T00:00:00Z"
  }
}
```

#### PUT `/api/users/{user_id}`
更新用户信息。

**请求体**:
```json
{
  "email": "newemail@example.com",
  "role": "admin",
  "is_active": true
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": { /* 用户对象 */ }
}
```

#### DELETE `/api/users/{user_id}`
删除用户。

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

#### POST `/api/users/{user_id}/reset-password`
重置用户密码。

**请求体**:
```json
{
  "new_password": "string"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

### 数据脚本 (`/api/scripts`)

#### GET `/api/scripts/`
获取数据脚本列表。

**查询参数**:
- `page` (int, 默认: 1) - 页码
- `page_size` (int, 默认: 20) - 每页数量
- `category` (string, 可选) - 分类过滤
- `is_active` (boolean, 可选) - 是否激活

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "items": [
      {
        "id": 1,
        "script_id": "stock_zh_a_hist",
        "script_name": "A股历史行情",
        "category": "stocks",
        "frequency": "daily",
        "is_active": true,
        "description": "获取A股日线数据"
      }
    ],
    "total": 50,
    "page": 1,
    "page_size": 20
  }
}
```

#### GET `/api/scripts/categories`
获取脚本分类列表。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": ["stocks", "funds", "futures", "indexes", "options", "bonds"]
}
```

#### GET `/api/scripts/{script_id}`
获取脚本详情。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "id": 1,
    "script_id": "stock_zh_a_hist",
    "script_name": "A股历史行情",
    "category": "stocks",
    "module_path": "app.data_fetch.scripts.stocks.daily.stock_zh_a_hist",
    "parameters": [
      {
        "name": "symbol",
        "type": "string",
        "required": true,
        "description": "股票代码"
      }
    ]
  }
}
```

#### POST `/api/scripts/scan`
扫描并注册新的数据脚本（管理员）。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "scanned": 10,
    "registered": 5,
    "updated": 3
  }
}
```

#### POST `/api/scripts/{script_id}/test`
测试脚本执行。

**请求体**:
```json
{
  "parameters": {
    "symbol": "000001"
  }
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "status": "success",
    "rows_affected": 100,
    "execution_time": 1.23
  }
}
```

#### PATCH `/api/scripts/{script_id}/toggle`
切换脚本激活状态。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "id": 1,
    "is_active": false
  }
}
```

### 定时任务 (`/api/tasks`)

#### GET `/api/tasks/`
获取定时任务列表。

**查询参数**:
- `page` (int, 默认: 1) - 页码
- `page_size` (int, 默认: 20) - 每页数量
- `is_active` (boolean, 可选) - 是否激活

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "items": [
      {
        "id": 1,
        "name": "每日A股数据",
        "script_id": "stock_zh_a_hist",
        "schedule_type": "cron",
        "schedule_expression": "0 15 * * 1-5",
        "is_active": true,
        "last_execution_at": "2024-01-01T15:00:00Z",
        "next_execution_at": "2024-01-02T15:00:00Z"
      }
    ],
    "total": 10,
    "page": 1,
    "page_size": 20
  }
}
```

#### POST `/api/tasks/`
创建定时任务。

**请求体**:
```json
{
  "name": "每日A股数据",
  "script_id": "stock_zh_a_hist",
  "schedule_type": "cron",
  "schedule_expression": "0 15 * * 1-5",
  "parameters": {
    "symbol": "000001"
  },
  "retry_on_failure": true,
  "max_retries": 3
}
```

**响应** (201):
```json
{
  "code": 0,
  "message": "success",
  "data": { /* 任务对象 */ }
}
```

#### GET `/api/tasks/{task_id}`
获取任务详情。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "id": 1,
    "name": "每日A股数据",
    "script_id": "stock_zh_a_hist",
    "schedule_type": "cron",
    "schedule_expression": "0 15 * * 1-5",
    "is_active": true,
    "retry_on_failure": true,
    "max_retries": 3,
    "created_at": "2024-01-01T00:00:00Z"
  }
}
```

#### PUT `/api/tasks/{task_id}`
更新任务。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": { /* 更新后的任务对象 */ }
}
```

#### DELETE `/api/tasks/{task_id}`
删除任务。

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

#### POST `/api/tasks/{task_id}/execute`
手动触发任务执行。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "execution_id": "exec_123",
    "status": "pending"
  }
}
```

#### POST `/api/tasks/{task_id}/pause`
暂停任务。

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

#### POST `/api/tasks/{task_id}/resume`
恢复任务。

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

#### GET `/api/tasks/{task_id}/executions`
获取任务执行记录。

**查询参数**:
- `page` (int, 默认: 1) - 页码
- `page_size` (int, 默认: 20) - 每页数量
- `status` (string, 可选) - 状态过滤

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "items": [ /* 执行记录数组 */ ],
    "total": 100
  }
}
```

### 执行记录 (`/api/executions`)

#### GET `/api/executions/`
获取执行记录列表。

**查询参数**:
- `page` (int, 默认: 1) - 页码
- `page_size` (int, 默认: 20) - 每页数量
- `task_id` (int, 可选) - 任务ID过滤
- `status` (string, 可选) - 状态过滤

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "items": [
      {
        "execution_id": "exec_123",
        "task_id": 1,
        "status": "completed",
        "start_time": "2024-01-01T15:00:00Z",
        "end_time": "2024-01-01T15:01:00Z",
        "rows_affected": 100,
        "error_message": null
      }
    ],
    "total": 500,
    "page": 1,
    "page_size": 20
  }
}
```

#### GET `/api/executions/{execution_id}`
获取执行详情。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "execution_id": "exec_123",
    "task_id": 1,
    "status": "completed",
    "start_time": "2024-01-01T15:00:00Z",
    "end_time": "2024-01-01T15:01:00Z",
    "rows_before": 1000,
    "rows_after": 1100,
    "rows_affected": 100,
    "result": {
      "message": "success"
    },
    "error_message": null,
    "retry_count": 0,
    "triggered_by": "scheduler"
  }
}
```

#### POST `/api/executions/{execution_id}/cancel`
取消执行。

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

#### POST `/api/executions/{execution_id}/retry`
重试执行。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "execution_id": "exec_124",
    "status": "pending"
  }
}
```

#### GET `/api/executions/stats`
获取执行统计信息。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "total_count": 1000,
    "success_count": 950,
    "failed_count": 50,
    "running_count": 0,
    "success_rate": 95.0
  }
}
```

### 数据表 (`/api/tables`)

#### GET `/api/tables/`
获取数据表列表。

**查询参数**:
- `page` (int, 默认: 1) - 页码
- `page_size` (int, 默认: 20) - 每页数量
- `category` (string, 可选) - 分类过滤

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "items": [
      {
        "table_name": "ak_stock_zh_a_hist",
        "display_name": "Stock Zh A Hist",
        "row_count": 100000,
        "size_bytes": 10485760,
        "last_updated_at": "2024-01-01T15:00:00Z"
      }
    ],
    "total": 50
  }
}
```

#### GET `/api/tables/{table_name}`
获取表详情。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "table_name": "ak_stock_zh_a_hist",
    "display_name": "Stock Zh A Hist",
    "row_count": 100000,
    "size_bytes": 10485760,
    "schema_info": {
      "columns": [
        {
          "name": "id",
          "type": "BIGINT",
          "nullable": false
        },
        {
          "name": "date",
          "type": "DATE",
          "nullable": true
        }
      ]
    },
    "last_updated_at": "2024-01-01T15:00:00Z"
  }
}
```

#### GET `/api/tables/{table_name}/schema`
获取表结构。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "columns": [ /* 列定义数组 */ ]
  }
}
```

#### GET `/api/tables/{table_name}/data`
获取表数据（分页）。

**查询参数**:
- `page` (int, 默认: 1) - 页码
- `page_size` (int, 默认: 100) - 每页数量
- `sort_by` (string, 可选) - 排序字段
- `sort_order` (string, 可选) - 排序方向 (asc/desc)

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "items": [ /* 数据行数组 */ ],
    "total": 100000,
    "page": 1,
    "page_size": 100
  }
}
```

#### GET `/api/tables/{table_name}/export`
导出表数据。

**查询参数**:
- `format` (string, 默认: csv) - 导出格式 (csv/excel)
- `limit` (int, 可选) - 限制行数

### 数据下载 (`/api/data`)

#### POST `/api/data/download`
手动下载数据。

**请求体**:
```json
{
  "interface_id": 1,
  "parameters": {
    "symbol": "000001",
    "start_date": "20240101"
  }
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "execution_id": "exec_123",
    "status": "pending"
  }
}
```

#### GET `/api/data/download/{execution_id}/status`
获取下载状态。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "execution_id": "exec_123",
    "status": "running",
    "progress": 50,
    "rows_affected": 500
  }
}
```

#### GET `/api/data/download/{execution_id}/result`
获取下载结果。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "status": "completed",
    "rows_affected": 1000,
    "table_name": "ak_stock_zh_a_hist",
    "execution_time": 5.23
  }
}
```

### 接口浏览 (`/api/interfaces`)

#### GET `/api/interfaces/`
获取接口列表。

**查询参数**:
- `page` (int, 默认: 1) - 页码
- `page_size` (int, 默认: 20) - 每页数量
- `category` (string, 可选) - 分类过滤
- `keyword` (string, 可选) - 关键词搜索

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "items": [
      {
        "id": 1,
        "name": "stock_zh_a_hist",
        "display_name": "A股历史行情",
        "category": "stocks",
        "description": "获取A股日线数据",
        "parameter_count": 5
      }
    ],
    "total": 1000
  }
}
```

#### GET `/api/interfaces/categories`
获取接口分类。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": [
    {
      "name": "stocks",
      "display_name": "股票",
      "description": "股票相关数据接口",
      "interface_count": 395
    }
  ]
}
```

#### GET `/api/interfaces/{interface_id}`
获取接口详情。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "id": 1,
    "name": "stock_zh_a_hist",
    "display_name": "A股历史行情",
    "category": "stocks",
    "description": "获取A股日线数据",
    "parameters": [
      {
        "name": "symbol",
        "type": "string",
        "required": true,
        "default_value": null,
        "description": "股票代码"
      }
    ]
  }
}
```

#### GET `/api/interfaces/{interface_id}/parameters`
获取接口参数。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": [ /* 参数数组 */ ]
}
```

### 系统设置 (`/api/settings`) - 管理员

#### GET `/api/settings/`
获取系统设置。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "max_concurrent_tasks": 5,
    "default_timeout": 300,
    "retry_enabled": true,
    "max_retries": 3
  }
}
```

#### PUT `/api/settings/`
更新系统设置。

**请求体**:
```json
{
  "max_concurrent_tasks": 10,
  "default_timeout": 600
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": { /* 更新后的设置 */ }
}
```

#### GET `/api/settings/database`
获取数据库配置。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "host": "localhost",
    "port": 3306,
    "database": "opendata",
    "username": "root"
  }
}
```

#### PUT `/api/settings/database`
更新数据库配置。

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

#### POST `/api/settings/database/test`
测试数据库连接。

**请求体**:
```json
{
  "host": "localhost",
  "port": 3306,
  "database": "test_db",
  "username": "root",
  "password": "password"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "connected": true,
    "latency_ms": 5
  }
}
```

#### GET `/api/settings/scheduler`
获取调度器状态。

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "running": true,
    "active_jobs": 5,
    "next_run_time": "2024-01-02T09:00:00Z"
  }
}
```

## 错误响应

所有错误响应遵循以下格式：

```json
{
  "code": -1,
  "message": "error description",
  "detail": "detailed error information"
}
```

### HTTP 状态码

| 状态码 | 说明 |
|--------|------|
| 200 | 成功 |
| 201 | 创建成功 |
| 204 | 无内容 |
| 400 | 请求参数错误 |
| 401 | 未认证 |
| 403 | 无权限 |
| 404 | 资源不存在 |
| 422 | 请求体验证失败 |
| 500 | 服务器内部错误 |

### 错误代码

| 代码 | 说明 |
|------|------|
| -1 | 通用错误 |
| 1001 | 认证失败 |
| 1002 | 令牌过期 |
| 1003 | 权限不足 |
| 2001 | 资源不存在 |
| 2002 | 资源已存在 |
| 3001 | 参数验证失败 |
| 3002 | 参数格式错误 |
| 5001 | 数据库错误 |
| 5002 | 调度器错误 |
| 5003 | 数据采集错误 |

## 速率限制

API 实施了速率限制：

- 普通用户: 100 请求/分钟
- 管理员: 1000 请求/分钟

超出限制时返回 `429 Too Many Requests`。

## 分页

所有列表端点支持分页：

- `page` (int, 默认: 1) - 页码，从1开始
- `page_size` (int, 默认: 20) - 每页数量，最大100

响应包含分页信息：

```json
{
  "data": {
    "items": [],
    "total": 100,
    "page": 1,
    "page_size": 20,
    "total_pages": 5
  }
}
```
