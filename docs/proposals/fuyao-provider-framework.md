# fuyao 客户端与 provider 框架设计提案（研究稿）

> 迭代 1A/1B/1C · FR-6/FR-7 · AC-7/AC-10 · 日期 2026-09-23
> 状态：**研究稿，待决策**（第 7 节开放项未确认前不进入实现）

## 1. 结论摘要

1. **fuyao 与 OpenBB 风格 provider 应共用同一套框架**：`provider 接口类 + QueryParams + Fetcher + 标准化模型 + 插件注册`（FR-7 原文范式），fuyao 是这套框架里的第一个"国内权威源 provider"，OpenBB 旧功能按同一范式逐个移植（自研，零 OpenBB 源码）。
2. **fuyao 的语义事实已经有成熟实现可对齐**：消费者 `ai-for-investor` 的 `market_data/ths_*` 就是同花顺扶摇客户端（base URL `https://fuyao.aicubes.cn`），已实现信封、错误码（4001 → 限流）、10 年窗口切块、`date_ms` → 交易日 UTC midnight、market-dumps 导入、历史 runner、参考数据与调度。这些是**接口事实**，A3 应对齐它们，避免两套 fuyao 语义。
3. **OpenBB 旧功能的清单已经存在**：`bt_api_py/docs/迭代计划/迭代09-历史数据获取` 已给出 32 个 provider 的完整分批（P0 行情与宏观 → P1 商业源与其余宏观 → P2 其余）与 M4/M5 里程碑，并已决策**整体迁到 opendata 落地**（需求文档 §1 D1）。opendata 侧 C1 已收敛为"provider 模板 + compat 对照表 + 按消费场景分批"。
4. **合规是硬约束**：OpenBB 为 AGPL-3.0，只借鉴接口形态与命名，禁止任何源码复制（含"改写变量名"式近似复制）；消费者侧 OpenBB 在线路由已因许可审查未完成被判定 **NO-GO**（`docs/iterations/迭代197.../REQUIREMENTS.md`），这进一步说明"自研 provider"是唯一可行路径。

## 2. 现状盘点（三个事实源 + 一个上游形态）

### 2.1 opendata 侧（实施地，D1）

| 资产 | 位置 | 可复用点 |
|------|------|----------|
| 统一契约模型 | `opendata/data/contracts.py`（Bar/AdjustFactor/CorporateAction/FinancialStatement/FinancialIndicator/IndexConstituent） | 即 FR-7 的"标准化模型"雏形 |
| 能力与注册路由 | `opendata/data/{capability,protocol,registry}.py`（A1：Capability(asset_class, domain, period, market, source) + 进程内注册表 + health） | 即 FR-7 的"插件注册 + 路由" |
| 三段式 fetcher | `opendata/data/providers/akshare/`（`models/` 每域一文件 + `registration.py`；`extract_data` 惰性导入） | 与 OpenBB Fetcher 的 `transform_query / extract_data / transform_data` 一一对应 |
| 零依赖门禁 | `scripts/codemod/verify_no_akshare.py` + `docs/quality/zero-dep-baseline.json`（基线 1，只降不升） | 直接扩展为"零 openbb"断言 |
| A3 任务表 | 实施计划 §3（A3.1 客户端 / A3.2 P0 端点 / A3.3 market-dumps / A3.4 映射注册） | fuyao provider 的分阶段落地路径 |
| C1 任务表 | 实施计划 §3（provider 模板 + compat 对照表 → P0 海外与宏观 → P1 商业源） | OpenBB 旧功能移植的批次与准入（按消费场景） |

### 2.2 消费者侧（`ai-for-investor`，语义事实源）

- **`market_data/ths_*`（= fuyao 客户端）已实现**：`ths_http` / `ths_envelope` / `ths_errors` / `ths_rate_limiter` / `ths_contracts` / `ths_provider` / `ths_history_runner`（778 行）/ `ths_dump*`（market-dumps 导入）/ `ths_reference*` / `ths_scheduler` / `ths_credentials`（`THS_API_KEY`、`THS_API_BASE_URL`，默认 `https://fuyao.aicubes.cn`）。
  关键事实：A 股日线仅 `1d`；历史窗口单块 ≤ 10 年；`date_ms` 为交易日上海零点 → 中台 `event_at` 取 UTC midnight；复权 `unadjusted|qfq|hfq` → `none|forward|backward`；限流错误码 4001；凭据不落库/不进日志。
- **OpenBB 侧是"合规模块"而非依赖**：`openbb_runner`（独立 JSON 子进程协议、四段命令、`--protocol-self-check`、receipt 校验）+ `openbb_subprocess_provider`（fail-closed）+ `openbb_runtime_permit_manifest.json`（`supported_runner_providers: ["yfinance"]`、`runtime_route_permits: []`）。即：**在线路由为空**，OpenBB 实际不取数。
- 平台侧已有 provider 契约/台账（`provider_contracts`、`source_policy`、`capability_ledger`），本次 A5.1 已按其范式接入 opendata provider（消费者提交 `50b6a54b`）。

### 2.3 bt_api_py 迭代09（OpenBB 旧功能清单的来源）

- FR-6：为 OpenBB 现有 **32 个 provider** 各建包，接口范式 `provider 接口类 + QueryParams + Fetcher + 标准化模型 + 插件注册`；提供上游对照表；第三方 SDK 声明为**该包可选依赖**。
- 分批：**P0 行情与宏观** → **P1 商业源与其余宏观** → **P2 全部剩余**；里程碑 M4（P0/P1：provider 模板 + yfinance + 宏观 + 商业源）、M5（P2 全量 + 同步工具 + 验收）。
- 该规划**未实施**，经 D1 决策整体迁入 opendata。

### 2.4 OpenBB 上游形态（仅形态事实，见 `/Users/yunjinqi/Documents/new_projects/OpenBB`）

```python
# provider 入口：注册 fetcher_dict（模型名 → Fetcher 类）
Provider(name="fuyao", description=..., website=..., credentials=["api_key"],
         fetcher_dict={"EquityHistorical": FuyaoEquityHistoricalFetcher})

class Fetcher(Generic[Q, R]):
    def transform_query(params: dict) -> Q                  # 参数 → QueryParams
    async def aextract_data(query, credentials) -> Any      # 取数（异步）
    def extract_data(query, credentials) -> Any             # 取数（同步）
    def transform_data(query, data, **kwargs) -> R          # 原始 → 标准化模型
```

要点：`credentials` 由 provider 名派生环境变量名（`fuyao_api_key` 风格）；扩展经 Python entry point 注册；标准化模型是 pydantic `Data` 子类；`QueryParams` 负责参数校验。

## 3. 目标框架设计（自研，零 OpenBB 源码）

### 3.1 分层（关键取舍：transport 与 provider 分离）

```
opendata/pipeline/*            数仓（已有）
opendata/data/                 契约 + Capability + Registry + 路由（已有）
opendata_fuyao/                fuyao transport：认证 / 信封 / 错误文案表 / 限流退避 / market-dumps（A3）
opendata_providers/            provider 框架 + 各 provider 子包（新，FR-7）
  ├── core/                    Provider 接口类 / QueryParams / Fetcher / 注册（entry points）
  ├── fuyao/                   同花顺 provider（消费 opendata_fuyao transport）
  └── yfinance/ fred/ ecb/ …   OpenBB 旧功能移植（逐个 provider 一个子包）
openbb_map.yaml                OpenBB 接口名 ↔ 本项目接口名 对照表（只描述契约）
```

理由：消费者已有经验表明 transport（HTTP/鉴权/限流/错误码）与 provider（模型/契约/路由）是两类变更节奏；A4.7 的模板接线也印证了"传输层稳定、provider 层多变"。若强行合并，会让每个 provider 重复实现限流与错误码。

### 3.2 接口契约草案（自研，命名可对照但无源码参照）

| 概念 | 本项目 | 对照 OpenBB | 说明 |
|------|--------|-------------|------|
| provider 入口类 | `ProviderSpec(name, description, website, credential_env, fetchers)` | `Provider(name, description, website, credentials, fetcher_dict)` | 只声明元数据与 fetcher 列表 |
| 参数模型 | `DomainQueryParams`（pydantic，每域一个） | `QueryParams` | 复用现有契约字段做校验 |
| 取数器 | `DomainFetcher`：`prepare_query / fetch_sync / fetch_async / normalize` | `Fetcher.transform_query/extract_data/aextract_data/transform_data` | 现有三段式 `extract_data` 直接落位 `fetch_*` |
| 标准化模型 | 现有 `opendata/data/contracts.py` 模型（Bar 等） | `standard_models/*` | 不新增重复模型 |
| 插件注册 | 现有 `ProviderRegistry`（能力键 `asset_class:domain:period:market:source`）+ entry points | extension entry point | 保持 A1 路由语义不变 |

### 3.3 compat 对照表（`openbb_map.yaml`）结构草案

```yaml
version: 1
entries:
  - openbb: {provider: yfinance, model: EquityHistorical}
    ours:   {provider: yfinance, domain: stock_daily, contract: Bar}
    endpoint: ...            # 事实性信息
    sdk_extra: yfinance      # 该 provider 的可选依赖
    license: ...             # 上游许可与数据权利登记
    scenario: 海外行情回测数据准备   # FR-7 准入：标不出场景即移出本迭代
    batch: P1
```

## 4. fuyao 的落地路径（A3 → provider 化）

| 阶段 | 内容 | 与消费者 `ths_*` 的关系 |
|------|------|------------------------|
| A3.1 transport | `opendata_fuyao/`：认证（`FUYAO_API_KEY`）、信封解析、**错误业务文案表**、限流退避、市场 dump 下载 | 对齐事实（端点/错误码/窗口/时间语义），**不复制代码**（异仓、异层、许可与维护边界不同）；对齐结论在对照表中留痕 |
| A3.2 端点 | `/api/meta/*`（Instrument/Calendar）、`/api/a-share/prices`、除复权事件 | 同上 |
| A3.3 market-dumps | Parquet 下载解析 + 前端一键导入入口 | 消费者的 `ths_dump_importer` 可作为**行为对照**（字段/分区/幂等） |
| A3.4 provider 化 | 按 3.2 契约注册为 `source=ths` 的国内权威 provider，映射到现有契约模型 | 消费者的 provider 契约/策略层已证明该形态可行 |

## 5. OpenBB 旧功能移植清单与批次

| 批次 | 内容 | 里程碑 | 准入条件 |
|------|------|--------|----------|
| 模板 | provider 模板 + compat 对照表 + 注册 | 1C/C1（3 人日） | 框架契约评审通过 |
| P0 | 海外行情（yfinance）+ 宏观（FRED/ECB/IMF/OECD） | 1C/C1 | 有明确消费场景标注；与上游官方输出逐字段对照 |
| P1 | 商业源（FMP/Tiingo/Alpha Vantage 等） | 1C/C1 | **有 Key + 有消费场景才做**；无 Key 者移出本迭代，进按需接入清单 |
| P2 | 其余 provider（sec/congress/cftc/finra…） | 1B/B1 或更后 | 同上；不追求"32 个全做"作为门槛（AC-10 已明确） |

## 6. 合规与门禁（必须落地，否则框架不成立）

1. **零 OpenBB 源码**：不引入 `openbb` 依赖、不复制源码；接口命名与对照表属事实性信息；每个 provider 提交说明声明"无 OpenBB 源码参照"并留审查记录。
2. **可选依赖**：各 provider 的 SDK（yfinance 等）声明为 extras，不安装不影响其它 provider（单测：不装某 SDK 时该 provider 可导入、路由按能力跳过）。
3. **门禁扩展**：把 `zero-dep-baseline` 的 AST 断言从"零 akshare"扩展到"零 openbb"；`openbb_map.yaml` 纳入校验（每个已启用 provider 必须有条目且场景非空）。
4. **数据权利**：新增 provider 的许可/再分发条款登记到 `docs/data-rights-registry.md`（AC-1 已建）。

## 7. 开放决策项（需确认后再实现）

| # | 决策 | 选项 |
|---|------|------|
| D1 | 框架落点与命名 | (a) 新包 `opendata_providers/`（每 provider 一子包）；(b) 并入 `opendata/data/providers/<source>/` |
| D2 | fuyao 的推进方式 | (a) A3 先 transport，A3.4 再 provider 化；(b) A3 直接按 provider 形式一步到位 |
| D3 | OpenBB 移植本期范围 | (a) 只做 yfinance + 宏观四家（P0）；(b) P0 + 有 Key 的商业源；(c) 追加 P2 |
| D4 | 兼容入口 | (a) 只提供静态对照表（FR-7 原文要求）；(b) 另提供 OpenBB 调用名兼容入口（运行时路由） |
| D5 | 消费者侧角色 | (a) 只保留 provider 契约/台账与适配，不再自建数据源实现；(b) 消费者继续保留自有 provider |
