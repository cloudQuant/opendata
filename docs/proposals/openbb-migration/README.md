# OpenBB 功能级全量内化迁移规划

> 迭代 1C（C1 扩展）· FR-7 · AC-10 · 日期 2026-09-23
> 状态：**规划稿**（实现按批次推进；工作量与迭代边界见 §5，需确认）

## 1. 决策记录（本轮确认）

| # | 决策 | 结论 |
|---|------|------|
| D-A | 迁移方式 | **功能级全量内化**：自研实现全部功能面，**零 OpenBB 源码复制**，仓库保持 BSL 1.1（不引入 AGPL 传染） |
| D-B | 覆盖范围 | **全仓功能面**：core（provider 抽象/路由/standard models/组装）+ providers（32）+ extensions（命令层）+ cli；desktop 见 §3 的准入判定 |
| D-C | 批次 | **P0/P1/P2 全量纳入规划与实现**（不再只做 P0） |
| D-D | 兼容形态 | 采用 OpenBB 的**接口形态与命名**（Provider / QueryParams / Fetcher 四方法 / 标准化模型 / 插件注册），并提供上游对照表；**不提供源码级兼容层** |
| D-E | 框架落点 | 新包 `opendata_providers/`；传输层与 provider 层分离（`opendata_fuyao/`、`opendata_http/` 负责传输，provider 负责模型/契约/路由） |

前置事实：OpenBB 仓库 LICENSE 为 **整仓 AGPL-3.0**（"All files in this repository…"），因此**任何**源码搬运（含逐行改写）都会污染本仓许可；上游基线 `openbb_platform @ 3e071fcc2cd9f891cac6040ae60296dba76dab46`。

## 2. 洁净室规则（自研的合规操作口径）

**允许读取**（事实性信息）：
- OpenBB 的**公开文档**（`openbb-docs`）与命令行/接口**名称**、provider 名、模型名清单；
- 各上游数据源自己的官方文档与 API 规范（yfinance / FRED / ECB / IMF / OECD / FMP …）；
- OpenBB 本仓的**包结构、pyproject 依赖名、Provider 构造函数签名与 fetcher_dict 键名**（本次清单即由此机械提取，`provider-inventory.yaml` 头部记录了生成方式与上游 commit）。

**禁止**：
- 把 OpenBB 源码（含 tests/样例）复制或逐行改写进本仓；
- 依赖任何 `openbb-*` 发行包（含 `openbb-platform-api`：部分 provider 的 pyproject 依赖它，自研时必须改为直连 HTTP）；
- 以"等价实现"为由照抄实现细节（重试/解析/字段拼装的具体写法须独立设计与留痕）。

**留痕要求**：每个 provider 的提交说明声明"无 OpenBB 源码参照"；对照表条目记录上游 API 事实来源（官方文档链接 + 抓取日期）。

## 3. 范围与分层映射

| OpenBB 部分 | 规模（本次实测） | 我们的落点 | 处置 |
|-------------|------------------|------------|------|
| `core/openbb_core/provider`（抽象/Fetcher/模型基类） | core 351 py / 3.4 万行 | `opendata_providers/core/`：Provider 接口类、QueryParams、Fetcher 四方法、注册（entry points） | 自研子集（只实现我们路由需要的部分） |
| `core` 其余（api/app/build/env/路由组装） | 同上 | 由 `opendata/data/registry.py` + `opendata_providers/core/` 承担；REST 走现有 FastAPI（A4.9） | 形态对齐，不照搬 |
| `providers/*`（32 个） | **348 个 fetcher 模型** | `opendata_providers/<provider>/`，逐个自研 | 见 `provider-inventory.yaml` 与 §5 批次 |
| `extensions/*`（命令层 equity/crypto/economy…） | 189 py | 统一的 domain 路由（现有 Capability：asset_class/domain/period/market/source）+ REST/WS | 以"能力面等价"验收，不逐命令照搬 |
| `cli` | — | `opendata/cli.py` 扩展 provider 子命令（列能力/取数/自检） | 覆盖主要工作流 |
| `desktop`（Electron 前端） | — | 无 | **建议移出**：与数据中台定位无关且无消费场景（AC-10 准入：标不出场景即移出本迭代）；如需"桌面体验"，由消费方前端承接 |
| `obbject_extensions` | 1 | 无（结果对象容器，属 OpenBB 运行时的内部机制） | 移出 |

## 4. 接口契约（自研，命名对照）

| 概念 | 本项目（`opendata_providers/core/`） | 对照 OpenBB | 复用现状 |
|------|-------------------------------------|-------------|----------|
| provider 入口 | `ProviderSpec(name, description, website, credential_env, fetchers)` | `Provider(name, description, website, credentials, fetcher_dict)` | 现有 `Capability` 声明合并 |
| 参数模型 | `DomainQueryParams`（每域 pydantic） | `QueryParams` | 复用 `opendata/data/contracts.py` 字段 |
| 取数器 | `DomainFetcher.prepare_query / fetch_sync / fetch_async / normalize` | `transform_query / extract_data / aextract_data / transform_data` | 现有三段式 `extract_data` 直接落位 |
| 标准化模型 | 现有契约模型（Bar/AdjustFactor/…）+ 新增宏观/海外模型 | `standard_models/*` | 不重复建模 |
| 插件注册 | 现有 `ProviderRegistry`（`asset_class:domain:period:market:source`）+ entry points | extension entry point | 路由语义不变 |
| 对照表 | `openbb_map.yaml`（OpenBB provider+model ↔ 本项目 provider+domain+契约） | 无 | 新增；**只描述契约，不含实现** |

## 5. 批次、工作量与里程碑

清单见 `provider-inventory.yaml`（32 provider / 348 模型 / 逐条含批次、场景、SDK 依赖、粗估人日）。汇总：

| 批次 | 内容 | provider 数 | 粗估人日 |
|------|------|------------|----------|
| P0 | yfinance（海外行情）+ FRED/ECB/IMF/OECD（宏观） | 5 | **32** |
| P1 | 商业源（fmp/intrinio/tiingo/alpha_vantage/seeking_alpha）+ 其余宏观（federal_reserve/bls/eia/multpl/tradingeconomics/famafrench/econdb） | 12 | **59** |
| P2 | 其余（sec/congress/cftc/finra/nasdaq/tmx/deribit/tradier/benzinga/cboe/finviz/government_us/stockgrid/wsj/biztoc） | 15 | **62** |
| — | provider 框架与模板 | — | 3（C1 已列） |
| — | core 子集 + 命令层等价面 + cli | — | 20~40（区间，需细化） |
| — | **合计** | 32 | **≈ 175~195** |

**关键事实：全量实现（P0+P1+P2 + 框架/命令层）约 175~195 人日，远超 1C 现有估算（C1 35~55 人日）。** 建议切分（需你确认迭代边界）：

| 阶段 | 范围 | 里程碑建议 |
|------|------|-----------|
| 1C/C1 | 框架模板 + `openbb_map.yaml` + P0（32 人日） | C1 扩为 ~35 人日（含 3 人日模板） |
| 1C/C1 之后 | P1（59 人日） | 新增里程碑（如 C5）或跨迭代按场景插入 |
| 后续迭代 | P2（62 人日）+ 命令层/cli 收口 | 新增里程碑（如 C6/C7）或按需接入清单逐批 |

## 6. 验收口径（每个 provider）

1. **契约**：注册进 `ProviderRegistry`，`capabilities` 可见；`source` 与 `domain` 映射写入 `openbb_map.yaml`。
2. **对照**：与**上游数据源官方输出**逐字段对照（列名/顺序/类型族/取值，浮点 `rtol=1e-9`；HTTP 调用次数不一致计为差异）——Fixture 与对照工具复用 A2.5 的 `compare_with_upstream.py` 范式（录制回放，绑定真机样本）。
3. **隔离**：不安装该 provider 的 SDK 时，本 package 可导入、路由按能力跳过（单测正反例）。
4. **合规**：提交说明含"无 OpenBB 源码参照"声明；AST 断言把"零 akshare"扩展到**零 openbb / 零 openbb-\***；数据权利登记到 `docs/data-rights-registry.md`。
5. **场景**：`scenario` 字段非空；P2 条目若在实现前仍标不出场景，按 AC-10 移出本迭代并记录到按需接入清单。

## 7. 风险与待决

| # | 风险/待决 | 处置建议 |
|---|-----------|----------|
| R1 | 全量 175~195 人日 vs 1C 估算 35~55 | 需你确认里程碑切分（§5 表）；否则 1C 无法按期收口 |
| R2 | 商业源 Key（fmp/intrinio/tiingo/alpha_vantage） | 有 Key 才联调；无 Key 的 provider 只做"骨架 + 契约"或移出（待你给 Key 清单） |
| R3 | 数据权利：海外行情/宏观/商业源再分发条款 | 每个 provider 在实现前登记 `docs/data-rights-registry.md`（AC-1 门禁） |
| R4 | `openbb-platform-api` 依赖（cftc/imf/nasdaq） | 自研为直连 HTTP；对照表注明该依赖不可引入 |
| R5 | 对照基准的可获得性（部分上游无稳定公开输出） | 采用"官方文档字段 + 真机录制样本"双证据；无法对照者标注 `unverified` 不参与 auto 路由（沿用 A2.4 口径） |
