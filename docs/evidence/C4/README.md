# C4 验收收口 — 1B 服务化证据索引（AC-11 核心块）

> 迭代 1B / 里程碑 B4 + B5.1（AC-11、AC-12 收口）
> 日期：2026-09-24
> 环境：macOS（Apple Silicon）| conda env `py313`（Python 3.13.5）| MySQL
> 复算：所有命令均在仓库根直接执行

## 1. 本里程碑交付（AC-11 / AC-12 / AC-19）

| 交付物 | 证据 |
|--------|------|
| **WS `/ws/data/subscribe`**：首帧认证、subscribe/unsubscribe、meta 一等公民、`since_batch_id` 断线补发、full 分帧（5000 行/2MB，>10 万行降级 meta+truncated）、心跳/空闲超时、积压降级 | `tests/test_data_subscribe.py` 51 项（含 socket 真协议 + 实时投递） |
| **批次水位表 `batch_watermark`**（迁移 0004）+ 管线第 5 步 notify 钩子（先落水位后推送） | `tests/test_watermark.py` 23 项（含 MySQL 真机） |
| **`/ws/executions` 挂载修复**：nginx 代理 `/ws/` 而 FastAPI 挂 `/api/v1` 下——既有 WS 路径在生产不可达的缺陷已修复（挂根路径），vite 代理同步修正 | `tests/test_data_subscribe.py::TestWebSocketMounts` |
| **REST 导出**：`GET /api/v1/data/{asset_class}/{domain}/export` CSV 流式导出 + **公式注入转义**（`=`,`+`,`-`,`@` 前缀中性化，OWASP 单引号法） | `tests/test_data_query_api.py` 27 项（含注入样本 e2e）+ `tests/test_csv_serialization.py` 13 项 |
| **`adjust=qfq|hfq` 服务端合成**：`dwd_stock_adjust` 因子表（迁移 0005）+ 复权因子累乘（`factors.py`，qfq 以最新日锚定 1.0 / hfq 以最早日锚定 1.0，fail-closed）+ 因子构建器（`factor_builder.py`，ods→dwd 幂等 upsert） | `tests/test_factors.py` 14 项（手算样例）+ `tests/test_factor_builder_e2e.py` 4 项（MySQL 真机：qfq=10×9/10=9 精确断言）；**修复了 `apply_adjust_to_rows` 日期键类型不匹配的潜伏缺陷** |
| **默认时间窗分区裁剪** | `explain-default-window.txt`（本目录）：带默认窗口仅访问 p2025,p2026，不带则全分区 |
| **客户端 WS 订阅**：`opendata_client.subscribe()`（会话式：首帧认证 + 迭代更新 + `pull=True` 自动 REST 拉取；`websockets` 为可选 extra） | `tests/test_opendata_client.py` 32 项（含真机 socket e2e） |
| **B5.1 interface_loader 迁移收口**：移除 `interface_scan_source` 开关与 akshare 反射扫描（约 250 行遗留删除），目录=注册表单一事实源 | `tests/test_interface_loader*.py` + `tests/test_routing_seam.py` |
| **C2 上游同步工具** + 演练 | `docs/evidence/C2/` |
| **AC-19 配置项清单** | `docs/配置项清单.md`（63 项） |

## 2. 门禁

`make gate` 全绿（本目录 gate 输出）：2161 passed / 3 skipped，覆盖率 **84.38%**，A2 零容忍 + 棘轮 + 零依赖 + 前端三项全过。

## 3. 待办（网络阻塞 / 范围外，显式声明）

| 事项 | 阻塞 | 声明 |
|------|------|------|
| adjust qfq 与 **akshare 官方 qfq 序列对照**（AC-2/AC-11 容忍度比对） | eastmoney 持续拒连（AC-6 已多次确认） | 因子数学已用手算样例 + MySQL 真机验证；官方序列对照待网络恢复，与 AC-6 kline 用例同挂起 |
| B5.2 前端 Tables/Tasks 页 ods/dwd 分层适配 + 数据目录页（B4.5） | 前端工作块 | 见 `docs/evidence/B4/`（未开始） |
| Playwright e2e 纳入 `make gate` | 前端 | 待 B4.6 |

## 4. 关键缺陷修复记录（本里程碑发现并修复）

1. **`/ws/executions` 生产不可达**：FastAPI 挂在 `/api/v1/ws/...`，nginx 代理 `/ws/` 直通后端——挂载路径修复 + 回归测试。
2. **`adjust` 合成日期键类型不匹配**：查询行返回 ISO 字符串、因子行为 `date` 对象，键永不相等——`_factor_rows` 与 `apply_adjust_to_rows` 双侧归一化。
3. **`/data/capabilities` 返回裸数组**：客户端 `_get` 按信封解析会崩——兼容非信封响应。
4. **WS 认证解析器/水位引擎不可注入**：凭证在首帧而非握手，依赖注入不可达——以模块级 seam 使协议可测。
