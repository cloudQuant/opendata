# A2 单域搬运 — 验收证据索引

> 迭代 1A / 里程碑 A2（对应验收文档 AC-5、AC-6）
> 日期：2026-09-23
> 环境：macOS（Apple Silicon）| Python 3.13.5（conda env `py313`）| Node 20.20.2 | MySQL 9.4.0
> 复算方式：所有命令均可在仓库根直接执行；本地验证均在 Python 3.13 与 CI 孪生环境（Python 3.11 + 全新依赖集）双跑。

## 1. A2 DoD 对照（实施计划）

| DoD 项 | 状态 | 证据 |
|--------|------|------|
| 单域搬运模式验证通过 | **完成** | P0 日线链路闭包搬运（`opendata_http/`，131 py + 1 资源，39,566 行）；未安装 akshare 的环境中测试与门禁全绿（本机 py313 曾装有 PyPI `akshare` 遮蔽，已卸载后复验） |
| 零依赖断言（AST 口径）首次全量生效 | **完成** | `verify_no_akshare.py` 基线由 6 收紧至 **1**（仅 `data_script.py` 的 `source` 数据源名标签，设计 §4.4 语义正确） |

## 2. 任务与交付物

| # | 任务 | 提交 | 结果 |
|---|------|------|------|
| A2.1 | P0 日线链路闭包搬运 + manifest/lock | `b9b10a2` | stock(61)+stock_fundamental+index+datasets/_version/exceptions/request+`stock_feature.stock_hist_em`；132 文件 0 待办；平铺 API 310 个同名导出 |
| A2.2 | 根目录 `akshare/` 删除；pyproject packages 更新 | `2b68225` | 运行时改 `opendata_http`；棘轮换轨 `akshare→opendata_http`（受控 `--force-update`）；卸载 akshare 后 1522 测试通过 |
| A2.3 | 内置资源引用处置（`datasets.py` 断链） | 并入 `b9b10a2` | `RuntimeError` 明确报错并登记为人工改动（`datasets.py` 上游本已断链） |
| A2.4 | P0 域函数注册进 ProviderRegistry（含 `verified`） | `f4d2693` | 5 个三段式 fetcher（`opendata/data/providers/akshare/`）；`GET /api/v1/data/capabilities`、`/sources` 可见；registry 模式接口扫描落库（FR-17） |
| A2.5 | 搬运保真对照（录制回放，P0 域 100%） | `3a92955` | `compare_with_upstream.py` + `tests/test_port_fidelity.py`；**5/7 用例已录并通过，2 个 kline 用例待录**（见 §4） |
| A2.6 | 搬运层安全扫描（含 B 层）+ JS 执行点登记 | `be3c4ce` | 328 项全量 triage 归档；JS 执行点冻结清单 + `js-points-check` 门禁步骤 |

## 3. 门禁与覆盖率

| 证据 | 结果 |
|------|------|
| `gate.txt`（本目录） | `make gate` **PASSED**，exit 0；**1560 passed / 3 skipped**，覆盖率 86.13% |
| `pytest.txt`（本目录） | 同上（含逐文件覆盖率明细） |
| A2 零容忍 | 触碰文件全部通过 ruff 全集 + format + mypy + bandit（`gate.txt` a2-check 段） |
| 棘轮 | 全指标不升且改善：ruff 404→316、mypy 35→29、bandit 6→5；`ruff_ported` 717 / `direct_http_ported` 313（换轨后重冻） |
| 零依赖断言 | 基线 6 → 1（仅数据源名标签）；`opendata_http/` 零 `akshare` 引用 |
| JS 执行点 | `js-points-check`：engine 30 / eval 19 / exec 0，与登记清单一致（已入 `make gate`） |
| 搬运待办 | `docs/port-report.md`：0 待办（闭包缺口/人工漂移/基线漂移三类全清） |

## 4. AC-6 保真对照现状（含一项待网络）

| 用例 | 函数 | 行数 | 结果 |
|------|------|------|------|
| `stock_action_dividend` | `stock_history_dividend_detail`（分红） | 31 | PASS |
| `stock_action_rights` | `stock_history_dividend_detail`（配股） | 3 | PASS |
| `financial_statement` | `stock_financial_report_sina` | 103 | PASS |
| `financial_indicator` | `stock_financial_analysis_indicator_em` | 103 | PASS |
| `index_constituent` | `index_stock_cons_weight_csindex` | 300 | PASS |
| `stock_daily_raw` | `stock_zh_a_hist`（不复权） | — | **PENDING（网络）** |
| `stock_daily_qfq` | `stock_zh_a_hist`（qfq，A1 遗留官方序列） | — | **PENDING（网络）** |

- 对照口径：列名与顺序、形状、dtype 类型族（text/float/int/bool/datetime）、逐单元格取值（浮点 `rtol=1e-9`）；HTTP 调用次数不一致同样计为差异。
- 未完成对照的能力在注册表标注 `verified=false` 且不参与 auto 路由（AC-6 第三项）——5 个 P0 能力当前全部 `verified=false`。
- 两个 kline 用例失败原因为**上游限流**：`push2his/push2delay.eastmoney.com` 在录制后持续以 `RemoteDisconnected` 拒绝请求（sina、em datacenter、中证站点均正常）。恢复后执行 `python scripts/codemod/compare_with_upstream.py --record` 即可补齐；`--compare` 在存在 pending 时返回非零，不会静默通过。

`docs/evidence/A2/compare-report.md` 为对照报告（含 pending 原因与 D10 qfq 合成校验段）。

## 5. A2.6 安全扫描与 JS 执行点

- `bandit-ported-triage.md`：328 项逐类处置（接受/误报/风险登记），含 B307 远程文本求值面与 B501 `verify=False` 端点清单。
- `bandit-ported.json`：原始扫描结果（`make security-ported` 复算）。
- `docs/quality/js-execution-points.json`：JS 执行点逐文件冻结清单（engine 30 / eval 19 / exec 0），由 `make js-points-check` 与 `tests/test_js_execution_points.py` 双重守护；P0 注册域不含任何 JS 执行点。

## 6. 本里程碑修复的环境缺口（保真对照副产品）

| 缺口 | 影响 | 修复 |
|------|------|------|
| `xlrd` 未声明 | 搬运层读取中证 `.xls` 文件在干净环境（CI/孪生）失败 | 声明 `xlrd>=2.0.1` |
| 回放未保留 HTTP 编码 | sina GBK 页面在回放中乱码（`'实施' → 'ʵʩ'`） | transcript 记录并回放 `encoding` |
| requests 新版本自带内联类型 | `http_client` 关联返回值缺窄化，CI 解析新依赖时 mypy 会失败 | 显式 `cast`（typed/untyped 双视图均正确） |
| a2-check 的 `--follow-imports=silent` | 掩盖被跟随文件的 mypy 错误（A2.4 曾掩盖 22 个） | 由棘轮的目录级 mypy 兜底，已在提交说明中登记 |
