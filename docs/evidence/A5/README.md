# A5 消费方验证（前置） — 验收证据索引

> 迭代 1A / 里程碑 A5（对应验收文档 AC-14 首次打通）；设计 §10.3 / §10.4
> 日期：2026-09-23
> 环境：macOS（Apple Silicon）| Python 3.13.5（conda env `py313`）| MySQL 9.4.0 | uvicorn 0.38.0
> 复算方式：所有命令均可在仓库根直接执行；本地验证在 Python 3.13 与 CI 孪生环境（3.11 + 全新依赖）双跑。
> **本目录覆盖 A5 的降级验收**：消费方 API Key、最小客户端、以及按验收文档 §0 预案执行的"示例脚本模拟消费方"（降级，显式声明）。A5.1 的消费方仓库改动（`backtrader_web`）与真实全市场日线数据仍未开始，见 §3。

## 1. 已完成的前置

| # | 交付物 | 提交 | 结果 |
|---|--------|------|------|
| A5-P1 | 消费方 API Key（设计 §10.3） | `09f5a2e` | `api_keys` 表 + 迁移 0003；`ApiKeyService`（明文 `od-`+`token_urlsafe(32)`、只存 `sha256(pepper+key)`、等值索引 + `hmac.compare_digest`、过期/吊销/轮换、scope 白名单 fail-closed）；`get_current_principal` 支持 `Authorization: Bearer od-<key>` 与 `X-API-Key` 并与 JWT 共存；`/api/v1/keys` 增删查改（创建/轮换返回一次明文）；数据端点接入 scope 校验（403 先于 404，目录按 scope 过滤）；失败统一延迟防枚举 |
| A5-P2 | 最小客户端（设计 §10.4） | `d2cd04e` | `opendata_client/`（独立 pyproject，git 依赖安装）；`stock_daily(symbols, start, end, adjust=...)` 级调用、分页迭代、`catalog`/`freshness`/`diff_report`、API Key/JWT 双认证、HTTP 状态到类型化异常映射 |
| A5-P3 | 消费方示例脚本（设计 §10.4 `examples/`） | 见本节下方 | `examples/consumer_handoff.py`：经 `opendata_client` 取数 → 组装回测就绪帧 → 输出对接记录（调用次数/行数/区间/耗时）→ 可选跑 backtrader 均线交叉回测 |
| A5-P4 | **消费方真实接入（provider 适配器）** | 消费方仓库 `ai-for-investor` 提交 `50b6a54b`（分支 `feature/a5-opendata-provider`） | 在消费者的 `market_data` 子系统新增 opendata provider：契约（`opendata_contracts.py`，route `opendata-stock-daily-v1`）+ 适配器（`opendata_provider.py`，httpx 直连、分页、信封、稳定错误码、provenance）+ 凭据（`opendata_credentials.py`，`OPENDATA_API_KEY`/`OPENDATA_BASE_URL`）+ 契约白名单扩展 + settings 开关 `MARKET_DATA_OPENDATA_ENABLED` 与 source policy 路由（fail-closed）；26 单测 |
| — | 零依赖/门禁修复 | `f830d6e` | 移除 keys 路由里失效的 noqa（A2 ruff RUF100） |

### AC-14 降级验收声明（验收文档 §0 预案）

验收文档 §0 前置条件第 2 条写明：`backtrader_web` 项目方配合**无书面承诺**时，降级为 `opendata_client` 示例脚本模拟消费方，并**在本证据中显式声明为降级验收**。本目录即按该预案执行：

| 证据 | 内容 |
|------|------|
| `consumer-handoff.txt`（本目录） | 真实 HTTP 对接记录：`calls=1`、`rows=40`、区间 `2024-01-02..2024-02-10`、耗时 `0.14s`、回测终值 `100,001.15`（说明数据确实进入回测引擎） |
| `consumer-provider-e2e.txt`（本目录） | **消费方 provider 适配器真机联调**：本地 opendata uvicorn + 真机 API Key + `dwd_stock_daily` 探针，`OpendataProvider.fetch` 取回 3 条 observation（`event_at` 归一化为交易日 UTC midnight、`amount→turnover` 映射正确、provenance 记录分页与列信息） |
| 数据口径 | `dwd_stock_daily` 探针 40 根日线（非真实全市场；em 网络阻塞，见 §3）；服务为本地 uvicorn（127.0.0.1:8123），凭据为真机 `api_keys` 行，验证后已清理 |
| 声明 | **这是降级验收**：证明"API Key + 客户端 + REST 取数 + 回测数据准备"链路可用，不等价于 A5.1（消费方仓库真实接入） |

## 2. 门禁与验证

| 证据 | 结果 |
|------|------|
| `gate.txt`（本目录） | `make gate` **PASSED**，exit 0；覆盖率 **85.02%** |
| `pytest.txt`（本目录） | **1822 passed / 3 skipped**（含 e2e），覆盖率 **86.52%** |
| `e2e.txt`（本目录） | API Key 与客户端真机用例 **7 passed**（MySQL 主库 + 数据仓库 + 真实 HTTP 套接字） |
| 双环境 | Python 3.13：`make gate` PASSED；CI 孪生（3.11）：非 e2e 全绿、`a2_check` ok、棘轮「未增加」 |
| 迁移 | `alembic upgrade head`（0003 建表）、`downgrade -1` → `upgrade head` 可重放；MySQL 拒绝单删外键索引（1553），故 downgrade 随表删除 |

## 3. A5.1 仍未完成的部分（需你决策/授权）

| 项 | 现状 | 阻塞 |
|----|------|------|
| 消费方仓库改动（A5.1） | **适配器级已完成**（提交 `50b6a54b`） | 消费者 `ai-for-investor` 已按 provider 形式接入 opendata（26 单测 + adapter 级真机联调）；**平台级路径仍待打通**：真正经消费者查询接口取数还需能力台账 attestation、provider registry 行与 source authorization grant（其 `bootstrap`/台账链路），且本机缺其锁定开发环境（fastapi 0.136 / pytest 8.4），`make check-all` 与 `mypy-ratchet` 未能本地运行 |
| 真实全市场日线数据 | **未开始** | 日线采集源（`push2his/push2delay.eastmoney.com`）对本网络持续拒绝（第 5 次确认），无法拉取真实全市场数据；客户端贯通验证使用 `dwd_stock_daily` 探针行完成 |
| WS 订阅回调（设计 §10.4 "通知 + REST 拉取"） | **未实现** | 属 1B/B4（`/ws/data/subscribe`，AC-11）；当前客户端只有 REST |

## 4. 关键设计取舍

- **scope 空列表 = 全否**（fail closed）：新建 key 必须显式给域；`["*"]` 才代表全量。避免"未配置即全开"的默认放大。
- **403 先于 404**：无权限的域与不存在的域返回一致，避免用状态码枚举域。
- **客户端同步实现**：消费方在同一进程准备回测数据；异步/WS 留给 1B。
- **pepper 缺省回落 `SECRET_KEY`**：生产已强制非默认 `SECRET_KEY`（A0 校验），单独配 `API_KEY_PEPPER` 可再隔离一层。
