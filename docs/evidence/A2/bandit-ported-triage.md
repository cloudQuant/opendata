# A2.6 搬运层安全扫描与人工 triage（B 层全量）

- 扫描对象：`opendata_http/`（131 个 py 文件，35,337 行），搬运基线上游 `c4f6a631c259783dbc2507b6b27d179b3e88079d`
- 命令：`make security-ported`（等价 `bandit -r opendata_http -f json -o docs/evidence/A2/bandit-ported.json`）
- 原始结果：`docs/evidence/A2/bandit-ported.json`（328 项）
- 复核触发：上游重同步（`report_port.py` / `manifest.json` 哈希变化）后须重跑本 triage

日常 `make security`（`bandit.yaml`）按设计**排除** `opendata_http`：搬运树与上游逐字节同源，按质量规范 §4 采用「每次同步一次全量扫描 + 人工 triage」，而非逐提交债务记账。新代码的质量门禁由 A2 标准（`make a2-check`）承担。

## 汇总与处置

| 规则 | 数量 | 级别 | 处置 | 依据 |
|------|------|------|------|------|
| B113 request_without_timeout | 267 | MEDIUM | 接受 | 上游原样；新代码统一走 `opendata/data/http_client.py`（令牌桶/熔断/超时），搬运层直连数由棘轮指标 `direct_http_ported` 只降不升跟踪 |
| B307 eval | 19 | MEDIUM | 接受（登记风险） | 见下「remote 文本进入求值器」；7 个文件、19 处均已登记 |
| B501 request_with_no_cert_validation | 18 | HIGH | 接受（登记风险） | 上游对部分接口显式 `verify=False`（SW 研究/回购等）；见下 |
| B112 try_except_continue | 7 | LOW | 接受 | 分页/多候选解析循环的容错控制流，失败项跳过并继续 |
| B105 hardcoded_password_string | 5 | LOW | 误报 | `__token = ""` 类属性、`TOKEN_F_P = "tk.csv"` 文件名、`token` 形参名——无凭证字面量 |
| B110 try_except_pass | 4 | LOW | 接受 | 可选列删除/多格式回退（`stock_zh_a_sina`、`stock_zh_a_tick_163`）的静默容错 |
| B311 random | 3 | LOW | 接受 | 反限流随机抖动（`time.sleep(random.uniform(...))`）与退避，非密码学用途 |
| B603 subprocess_without_shell_equals_true | 3 | MEDIUM | 接受 | `utils/request.py` 的 curl 兜底：argv 为**列表**、未启用 shell、无字符串拼接注入面 |
| B404 import subprocess | 1 | LOW | 接受 | 同上（B603 的 import 侧提示） |
| B107 hardcoded_password_default | 1 | LOW | 误报 | `pro_api(token="")` 默认空串是「未提供」语义 |

无 B608（SQL 拼接）、无 B324（弱哈希）、无 pickle/yaml.load 类反序列化项；`builtin exec` 站点 0。

## 需登记的远程文本求值面（B307）

sina 港股/美股/B 股/科创/摘要页面把因子字典写成 `var x = {...};`，上游用内置 `eval(r.text.split("=")[1])` 直接求值**响应文本**（19 处 / 7 文件，见下表）。这是搬运层唯一「远端内容进入求值器」的类别：

| 文件 | 处数 |
|------|------|
| `stock/stock_hk_sina.py` | 4 |
| `stock/stock_zh_a_sina.py` | 4 |
| `stock/stock_zh_b_sina.py` | 4 |
| `stock/stock_zh_kcb_sina.py` | 4 |
| `stock/stock_summary.py` | 1 |
| `stock/stock_us_sina.py` | 1 |
| `stock/stock_zh_a_tick_tx.py` | 1 |

风险面说明：求值输入来自 HTTPS 响应，攻击者需具备中间人能力才能注入；本轮按「上游保真」接受并登记。后续接入这些域时优先用 `ast.literal_eval`/`json` 解析替换（已在 `stock_summary.py` 侧出现等价 JSON 结构）。**处置：接受 + 登记，列入同步差异复核清单。**

## 关闭证书校验的接口（B501，18 处全部 HIGH）

集中在 `index/index_research_sw.py`、`index/index_research_fund_sw.py`、`stock/stock_repurchase_em.py` 等：上游对这些端点显式 `verify=False`（站点证书链不完整）。按上游保真接受；这些端点均**未**进入 P0 注册表（`verified=false`，见 A2.4），不参与 auto 路由。**处置：接受 + 登记，若后续启用需评估加白名单 CA。**

## JS 执行点登记

机器可读清单：`docs/quality/js-execution-points.json`；复核命令 `make js-points-check`（已接入 `make gate`），任何增删都会失败并标注文件级漂移。

| 类别 | 站点 | 文件数 | 说明 |
|------|------|--------|------|
| 引擎实例 `py_mini_racer.MiniRacer()` | 30 | 22 | 被求值的 JS 载荷是**硬编码字面量**（`stock/cons.py` 的 `hk_js_decode`），远端文本仅作为数据传入 JS 函数 `d(...)`；无远端代码求值 |
| 内置 `eval(` | 19 | 7 | 即上节 B307（sina 因子字典文本） |
| 内置 `exec(` | 0 | 0 | 无 |

引擎站点集中域：sina 系列（`stock_zh_a_sina`、`stock_us_sina`、`stock_hk_sina`、`stock_zh_b_sina`、`index_stock_hk/us_sina/zh`）与 cninfo 系列（`stock_*_cninfo`、`stock_cg_*`、`stock_rank_forecast`）；`utils/multi_decrypt.py` 是多进程 JS 执行助手（作者已标注废弃，仅作参考）。P0 注册的 5 个域**不含任何 JS 执行点**（em 日线、sina 分红/配股与报表、em F10、中证成分文件）。

## 后续（A 层改造时的候选）

1. B307 的 7 个 sina 文件的文本求值替换为 `ast.literal_eval`/JSON 解析（进入对应域注册前完成一次）。
2. B501 端点的 CA 白名单评估（启用相关域前）。
3. B113/B603 随 `http_client` 收口迁移（棘轮 `direct_http_ported` 驱动），搬运树不直接手改，经 `port_module` 人工改动登记。
