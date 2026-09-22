# A1 契约与骨架 — 验收证据索引

> 迭代 1A / 里程碑 A1
> 日期：2026-09-22
> 环境：macOS（Apple Silicon）| Python 3.13.5（conda env `py313`）| Node 20.20.2 | MySQL 9.4.0
> 复算方式：所有命令均可在仓库根直接执行；本地验证均在 Python 3.13 与 CI 孪生环境（Python 3.11 + 全新依赖集）双跑。

## 1. A1 DoD 对照（实施计划）

| DoD 项 | 状态 | 证据 |
|--------|------|------|
| 契约可注册/可路由 | **完成** | `opendata/data/registry.py`（ProviderRegistry + resolve/resolve_domain + 权威度 + 健康探测）；`tests/test_provider_registry.py`（27 项：注册/路由/降级/显式源报错/未验证排除） |
| codemod 可用 | **完成** | `scripts/codemod/port_module.py` + `gen_manifest.py` + `report_port.py`；试点跑通（utils/file_fold/pro） |
| 基线冻结 | **完成** | `opendata_http/upstream.lock`（上游 c4f6a63 + 逐文件 sha256 + manual_edits）+ `manifest.json`；`docs/port-report.md` 待办清零 |
| `opendata/data` 覆盖率 ≥90% | **完成** | ~96%（见下） |

## 2. 门禁与覆盖率

| 证据 | 结果 |
|------|------|
| `gate.txt`（本目录） | `make gate` **PASSED**，exit 0；**1522 passed**（A1 新增 213 项），覆盖率 85.89% |
| `opendata/data` 包覆盖率 | ~96%（models 100%×8、domains 96%、registry 91%、http_client 96%、adjust 90%、protocol 87%） |
| A2 零容忍 | 触碰的 43 个文件全部通过 ruff 全集 + format + mypy + bandit（`gate.txt` a2-check 段） |
| 棘轮 | 全指标改善：ruff 404→385、mypy 35→34、bandit 6→5 |
| 零依赖断言 | `opendata_http/` 新搬运树 0 命中，基线维持 6 |

## 3. AC-2 / AC-3 逐项对照（验收文档）

### AC-2 统一契约与数据模型（1A / A1）

- [x] P0 域标准化模型（Bar/AdjustFactor/CorporateAction/FinancialStatement/FinancialIndicator/IndexConstituent/FuturesFundamentals）DataFrame/pydantic 双形态（单测：`tests/test_contract_models.py` 22 项）
- [x] `Instrument` 与 `TradingCalendar` 模型就位（字段集按设计 §4.1 钉死）；**被全市场回补与增量窗口引用**——pipeline 属 §9（1B/A4），引用随 pipeline 落地
- [x] `Bar` 只含不复权 OHLC（字段集守护测试）；复权序列由 `Bar`+`AdjustFactor` 合成（`tests/test_adjust.py` 10 项，数学精确性 + fail-closed）；**与 akshare 官方 qfq 序列对照**——待录制 fixture（登记为 A1 遗留项，A2 搬运保真阶段一并录制）
- [x] 三段式协议基类（QueryParams/Fetcher/Capability）就位，`normalize()`（transform_data）承担映射的唯一归属（docstring 钉死 §4.2 语义）
- [x] `domains.yaml` 注册表存在，表名/REST/WS 由它派生（`tests/test_domains.py` 23 项含交叉一致性：authority 域必须已注册）

### AC-3 注册、能力与路由（1A / A1）

- [x] **框架**：ProviderRegistry + Capability + 权威度基线（authority.json）+ 健康状态；`source="auto"` 路由与降级（单测：权威源故障→次选接管）
- [x] `verified=false` / `on-demand` / `upstream-pending` 不参与 auto；显式源精确路由、失败不降级
- [ ] opendata_http / opendata_fuyao / opendata_providers **真实能力注册**——分别属 A2.4 / 1B / 1C（注册框架与接缝已就位并被 `test_routing_seam.py` 验证）

### A1.7（旧机制改造起步）

- [x] 会话路由修正：下载写入走数据仓库会话（`data_db`），元数据走主库；仓库事务先提交（幂等）——FR-17 缺陷修复
- [x] 取数路径经路由（`_fetch_interface_data` → `resolve_domain`，legacy 兜底隔离在 `_call_akshare_function`）
- [x] interface_loader 兼容开关（`interface_scan_source: legacy|registry`，未知值 fail-closed）
- [x] 现有数据下载功能回归通过（83 项 acquisition 路径测试 + 13 项路由接缝测试）

## 4. 交付物索引

| 模块 | 提交 | 说明 |
|------|------|------|
| `opendata/data/models/` | `47b86d9` | 9 个契约模型 + ContractModel 双形态 + 调整合成 |
| `opendata/data/protocol.py`、`capability.py`、`registry.py`、`authority.json` | `b4a0348` | 三段式协议 + 注册路由（权威度表入数据文件——零依赖门禁拦截代码内 "akshare" 字符串的实证） |
| `opendata/data/domains.yaml`、`domains.py` | `d6d85db` | domain 注册表 + 四处派生 + fail-closed 校验 |
| `opendata/data/http_client.py` | `62bbd1e` | §5.3 请求治理（超时/退避/限流/熔断/失败分类），全 seam 可注入 |
| `scripts/codemod/port_module.py` 等 | `38cbe5d` | 搬运工具链 + 试点（utils/file_fold/pro）+ 基线冻结 + 待办清零 |
| `opendata/services/data_acquisition.py`、`interface_loader.py`、`api/data.py`、`cli.py` | `9fe31b8`、`424acaa` | 会话路由修复 + 取数经路由 + 兼容开关 |

## 5. A1 遗留与移交

| # | 项 | 去向 |
|---|----|------|
| 1 | akshare 官方 qfq 序列对照 fixture | A2.5 搬运保真（录制回放样本）一并录制 |
| 2 | 旧主库数据表（会话修复前创建）迁移评估 | 设计 §8.4（验收阶段） |
| 3 | 根目录 `akshare/` 与 `opendata_http/` 并存 | A2.2 删除（ratchet 文件数守卫需 `--force-update`，受控事件） |
| 4 | 库内 `akshare/` 与上游 208 文件漂移 | 已用 upstream.lock（上游基线）绕开；库内树 A2.2 整体删除 |
