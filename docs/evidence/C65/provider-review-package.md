# C65 provider 审查与 active-leg 请求方签认包

日期：2026-09-30（Europe/Madrid）

状态：**用户委托的当前 provider 源码审阅已完成；历史开发声明仍未验证；字段口径问题按当前逐条审阅保留。**

当前结果见 §9 与 `provider-source-review.json`。下文 §1–8 保留前期材料准备快照；其中待人工审查字样及权利“待复核”列已由 C65 后续审阅记录覆盖，不再作为泛化等待用户签字。数据权利以当前 `docs/data-rights-registry.md` 为准。
范围：`akshare`、`ecb`、`fred`、`imf`、`oecd`、`ths`、`yfinance` 七个当前启用 provider；33 个当前已注册的 provider×domain 腿（23 `verified`、10 `registered`）。

**C65 后续确认：**2026-09-30 用户本人回复“cloudQuant负责全部33条，用于量化研究、回测与数据中台”。逐腿场景与请求方已对账并归档 `provider-requester-confirmation.json`；下文来源审查与数据权利仍待各自证据。

本文件只为 C65 验收准备签认输入。本轮未改 provider、映射、需求、台账或测试文件；未运行测试；未发供应商请求、未读取凭据。包摘要取自 2026-09-30 当前工作树，包含当时可见的未提交内容，不代表某个 Git commit。

`docs/evidence/C65/criterion-migration.json` 记录用户批准了 `AC-1|03`、`AC-1|09`、`AC-5|03` 三处判据措辞迁移，其余 127 个判据身份不变。该批准处理的是白名单、模板和依赖扫描的口径冲突；它本身不决定逐腿请求方、数据域优先级或源码审查结论。C65 后续收到用户直接回复，明确 cloudQuant 负责全部 33 条，用于量化研究、回测与数据中台；逐腿记录见 `provider-requester-confirmation.json`。这项确认不代替字段语义、数据来源、数据权利或 OpenBB 源码审查签认。

## 1. 判据与批次依据

| 判据 | 本包对应的可复核输入 | 判定边界 |
|---|---|---|
| AC-5\|02：1A/A2 P0 域与 1B/B1 P1 模块搬运完成 | `docs/迭代计划/迭代1-重构数据中台/需求文档.md` §3 FR-4、§6.2；`实施计划.md` A2.1、B1.1；`upstream.lock`、`docs/port-report.md` | `Capability` / `openbb_map.yaml` 没有搬运优先级字段。需求 §6.2 给数据域优先级，实施计划 B1.1 给 P1 源子模块集合；两种粒度有交叉但不是同一清单。AC-5 分母须按冻结上游 `upstream.lock` 与 FR-4 按需范围逐文件对账，再用 manifest/report 验证；不能以 provider 数、33 条 active leg 或字段存在替代。`futures_derivative` 当前已进入工作树及 report，见 §8；旧 gap 理由“唯一未搬目录”已过期。 |
| AC-6\|02：1B/B1 其余子模块抽样 ≥20% | `需求文档.md` §6.2；`实施计划.md` B1.1/B1.3；`scripts/codemod/compare_with_upstream.py`；`docs/evidence/A2/compare-report.md` | 抽样分母是冻结后归入 B1.1/B1.2 的搬运子模块/文件范围，不是 provider 腿数；报告需列明 eligible manifest、抽样项、覆盖映射、待判案例、差异和容忍度。需求 §6.2 与 B1.1 的 P0/P1 术语不一致，见下节，须并列报告而不能改写原标签。A2 P0 fixture 的 `14 PASS + 4 PENDING` 不能替代 B1 抽样覆盖，也不能把四条待判计作通过。 |
| AC-10\|01：全部启用 provider 注册且对照通过 | `opendata/data/openbb_map.yaml` + 七个 registration/fetcher 声明 + `ProviderRegistry.capabilities()`；`tests/test_openbb_map.py` 是结构守卫 | 当前静态声明为 33 个 active legs：23 `verified`、10 `registered`；`pending` 的 `ths/financial_indicator` 未注册，不计入 33。`registered` 明确表示尚未对照转正。不能以 map 有行、capability 有字段或 provider 级汇总状态代替每腿结果。 |
| AC-10\|02：每 provider 有消费场景与请求方 | `openbb_map.yaml` 的逐腿 `scenario`；`provider-inventory.yaml` 与 `provider-requester-confirmation.json` | C65 用户直接回复并逐腿归档，确认 cloudQuant 负责全部 33 条，用于量化研究、回测与数据中台；这确认请求方/用途/继续启用，不代替字段映射、来源审查或数据权利签认。 |
| AC-10\|03：每 provider 零 OpenBB 源码 | `docs/proposals/openbb-migration/README.md` §1–2/§6、OpenBB 本机只读基线、各 provider 工作树摘要 | 规划文字、文件头/docstring、自报“自研”不是独立代码审查。源码对照结论均待审查人签字；不作“开发时未参照 OpenBB”的无证据历史声明。 |
| 研发设计 §4\|04：口径映射人工复核 | `opendata/data/mappings/akshare.yaml`、`ths.yaml`；官方 Parquet/接口文档与已录制响应 | mapping 文件存在只证明有声明。需请求方对字段、单位、key 与原始响应样本签认；签认不能代替真值来源及权利确认。 |

### 批次口径：分别保留需求数据域优先级、实施搬运批次与 OpenBB 清单批次

现有文档已直接给出可执行的批次边界，不需要为这些已写明的批次重新做产品选择；需要补的是将上游文件/子模块逐项归入 manifest，并把文档间的同名 P0/P1 标签冲突显式列出。不同来源的标签含义不相同：

| 口径 | 原文依据 | 可直接得到的范围 | 使用边界 |
|---|---|---|---|
| 需求数据域优先级：1A / P0 | `需求文档.md` §6.2；`实施计划.md` A2.1 明确 P0 stock 日线链路闭包 + utils，A2.5 要求 P0 100% 对照 | `stock_daily` 及其搬运闭包；具体源文件由冻结上游 manifest 列全 | 不扩张为整个 `stock/` 目录；A2.1 点名 stock 日线链路与 utils |
| 需求数据域优先级：1B / P0 | `需求文档.md` §6.2：“核心四件套 + 期货日线与基本面”中，1B 是“其余三件套 + 期货” | active domains：`stock_action`、`financial_statement`、`financial_indicator`、`index_constituent`、`futures_daily`；`futures_fundamentals` 当前并非 active leg | 这是 §6.2 的数据域 priority，不等同于 B1.1 的源模块标签 |
| 实施搬运批次：1B / B1.1 标注“P1 域子模块” | `实施计划.md` B1.1：`stock_feature/stock_fundamental/futures/index/fund/option/bond/economic`；B1.3 对其余域抽样 ≥20% | 当前 `manifest.json` 中这 8 个顶层包合计 233 个 `.py`；FR-4 D9 另点名 `futures_derivative`，增加 12 个 `.py`，其搬运纳入 1B futures 组的依据是 D9+B1.1 | B1.1 对某些文件称 P1，与 §6.2 中把财务/指数成分/期货列 P0 存在术语冲突；245 是源文件数，不应擅自当成 B1.3 的抽样单位/20%分母 |
| 需求数据域优先级：1B / P1 | `需求文档.md` §6.2 | A 股分钟线（按 D13 走 Parquet） | 该类数据不在当前 33 条 provider×domain active legs 中；它仍属于 §6.2 的 P1/1B 需求 |
| 需求数据域优先级：1C / P1 | `需求文档.md` §6.2、§7；`实施计划.md` C1 | `stock_daily_overseas`、`economy_cpi`、`economy_gdp`、`economy_unemployment`、`economy_rate` | OpenBB provider 清单另有自身移植 batch，不能替代需求 P1 标签 |
| 需求数据域优先级：P2 | `需求文档.md` §6.2；D9 按需接入清单 | `fund_etf_daily`、`fund_action`、`option_daily` 以及其余 provider/冷门模块 | `fund_action` 同时是 D10 ETF 复权还原依赖，依赖关系不改变其 P2 标签 |
| OpenBB provider 移植 batch | `docs/proposals/openbb-migration/README.md` §5、`provider-inventory.yaml` | `ecb/fred/imf/oecd/yfinance` 标为 provider batch P0；`akshare` 与 `ths` 在 `local_providers` | 这是 OpenBB 移植顺序，不等于迭代需求中的数据域优先级 |

对 33 条逐腿表，除明确标注 `B1.1` 模块视角外，“已有迭代批次依据”按 `需求文档.md` §6.2 的数据域口径填写。`index_daily`、`bond_daily` 的源模块分别落在 B1.1 的 `index/`、`bond/` 1B 搬运范围，但 §6.2 没有给它们独立的数据域优先级；`instrument`、`trading_calendar` 是 FR-2/A1 契约骨架并由 A3.2 列入 `/api/meta/*` P0 端点，不是 §6.2 的市场数据域等级。它们的搬运阶段可照已批准实施计划归批，无需因归批再次询问产品；文件/函数到接口或 provider 腿的对应关系仍要进 manifest 和报告。`futures_derivative` 是 FR-4/D9 范围且 B1.1 `futures/` 搬运组的当前源目录；其 12 个文件不可一概等同于 `futures_daily`，须由完整文件级 manifest 记录。`index_daily`、`bond_daily`、`instrument`、`trading_calendar` 不应再写成“待请求方确认批次”；请求方签认与批次分类是两项独立工作。

真实搬运总体分母在当前快照中可复算为：`opendata_http/upstream.lock` 的 327 个冻结源记录；`opendata_http/manifest.json` 明列 325 个 Python 文件 + 2 个资源（总 327）；`docs/port-report.md` 逐文件表也有 327 行。B1.1 明列的 8 个模块有 233 个 `.py`，加上 D9 点名且现被纳入 futures 搬运组的 `futures_derivative` 12 个 `.py`，源文件清单为 245 个。A2.1 的 1A 口径是“stock 日线链路闭包 + utils”，不是整个 `stock/` 目录，必须从所需公开函数/端点的源码依赖闭包形成逐文件清单。AC-6 的百分比抽样单位仍要沿用判据/实施计划所用的“其余域/子模块”并在报告中明示；`docs/迭代计划/迭代1-重构数据中台/验收文档.md` 的历史回填记有 `3/5 P1 域`，应保留为原验收证据，但不能用它替代当前 245 文件计数或冻结后的 domain/module 清单。以上数字是从当前已有 lock/manifest/report 和模块路径统计得出；本包只读检查了清单数，没有运行 `gen_manifest.py --check` 或刷新 report。

## 2. 当前机器声明与真实分母

### 2.1 33 条 active leg 的得数

当前静态源码声明与 `openbb_map.yaml` 对账如下。需要取得严格的运行时现值时，分母定义应是服务启动并调用 provider registrations 之后，`ProviderRegistry.capabilities()` 的 `(domain, source)` 唯一键与 map 中 `status ∈ {registered, verified}` 的逐键交集；本包未启动服务，也未调用 `/api/v1/data/capabilities`。源代码入口是 `opendata/data/registry.py::ProviderRegistry.capabilities`、`opendata/api/data.py::list_capabilities`；AC-10 的映射守卫是 `tests/test_openbb_map.py`。

| provider | 已注册腿 | 当前 `verified=true` | 当前 `registered`（`verified=false`） | provider 清单 status |
|---|---:|---:|---:|---|
| `akshare` | 10 | 0 | 10 | `已实现未对照`（local provider） |
| `ecb` | 3 | 3 | 0 | `已对照转正` |
| `fred` | 3 | 3 | 0 | `已对照转正` |
| `imf` | 3 | 3 | 0 | `已对照转正` |
| `oecd` | 2 | 2 | 0 | `已对照转正` |
| `ths` | 11 | 11 | 0 | `已对照转正`（local provider） |
| `yfinance` | 1 | 1 | 0 | `已对照转正` |
| **合计** | **33** | **23** | **10** | 7 个 provider |

此外 map 中另有 `ths/financial_indicator` 一条 `pending` 注释，但 `ths/registration.py::FETCHERS` 没有该 fetcher，故它既不在机器注册表，也不在 active denominator。它不是第 34 条已启用能力。

`provider-inventory.yaml` 的 provider-level 三态只概括已经注册的那部分：`akshare` 十腿仍未对照；FRED 三腿已在 C65 完成官方 JSON/CSV 同源对照并转为 `verified`。C1 README 中“没有 FRED 独立对照”是 C1 当时的历史事实；C65 新证据见 `fred-official-comparison.json`，不是跨 vendor 或独立统计真值验证。23 条 `verified` 的证据类型与强度并不完全相同；yfinance 的跨 vendor 口径来自 C8，THS 多数转正腿有分域交叉核对档案。签认时须逐腿读取证据类别，不能把 23 直接写成 23 条同规格跨 vendor PASS。

### 2.2 七个包快照与官方资料

摘要算法：每包对 `*.py`（排除 `__pycache__`）按相对路径排序，逐文件 `shasum -a 256`，再对“逐文件 SHA 清单输出”做 `shasum -a 256`。它是工作树快照摘要，不是 Git tree hash。合计 71 个 provider Python 文件。

| Provider / 当前腿数 | 源码包路径 | `.py` 文件数 / 清单摘要 SHA-256 | 官方资料（数据供应方原始资料） | `docs/data-rights-registry.md` 当前权利状态 |
|---|---|---:|---|---|
| `akshare` / 10 | `opendata/data/providers/akshare/` | 15 / `f2e31f15be481fbd7ece06a0059413b6c5bc52cc23fd6c115349fb180c44bb57` | [AKShare 文档](https://akshare.akfamily.xyz/) 与 [上游仓库](https://github.com/akfamily/akshare) 是 wrapper/搬运源资料，**不是**东方财富、Sina、交易所等各底层供应方统一 API 规范。每条实际端点须按 `opendata/data/providers/akshare/models/*.py` 与 `opendata/data/mappings/akshare.yaml` 的 source endpoint 回到各供应方官方页面；部分网页接口没有公开稳定 API schema。 | 行 2–7 多家底层数据方均“待复核” |
| `ecb` / 3 | `opendata/data/providers/ecb/` | 9 / `80302834c9fa3b5329ea23583f782229f2710448ced3984b5e6e3a8c27e37666` | [ECB API overview](https://data.ecb.europa.eu/help/api/overview)、[data query](https://data.ecb.europa.eu/help/api/data)、[content negotiation](https://data.ecb.europa.eu/help/api/content-negotiation) | ECB/IMF/OECD 合并登记行，待复核 |
| `fred` / 3 | `opendata/data/providers/fred/` | 9 / 初稿 `4d4babb104810ca4032335ad204e02be460cdca306a7582dde0d1477a49d3f5b`；C65 当前复算 `3e22d26ac5621fe252c16446eda0afb30b153a0ea757c166ac44256789924a48` | [FRED API overview](https://fred.stlouisfed.org/docs/api/fred/)、[series observations](https://fred.stlouisfed.org/docs/api/fred/series_observations.html)、[API key](https://fred.stlouisfed.org/docs/api/api_key.html)；C65 官方 JSON/CSV 同源对照：80/26/80 行，三腿均 PASS、零差异（`fred-official-comparison.json`） | FRED 行 9 待复核 |
| `imf` / 3 | `opendata/data/providers/imf/` | 9 / `c5c407a5436fad8b30326d2d2988e916c7f11a7cf20f4684d4d7cc6c87f31e5e` | [IMF API](https://data.imf.org/en/Resource-Pages/IMF-API)、[IMF SDMX Central Web Services Guide](https://dsbb.imf.org/content/pdfs/IMFSDMXCentralWebServicesGuide.pdf) | ECB/IMF/OECD 合并登记行，待复核 |
| `oecd` / 2 | `opendata/data/providers/oecd/` | 7 / `dfeccc4a665a03a4e23117ae6945ec3eef4a8b1de945a1b4116f973e607e2326` | [OECD API explainer](https://www.oecd.org/en/data/insights/data-explainers/2024/09/api.html)、[OECD SDMX API v1 Swagger](https://sdmx.oecd.org/public/swagger/index.html?urls.primaryName=v1) | ECB/IMF/OECD 合并登记行，待复核 |
| `ths` / 11 | `opendata/data/providers/ths/` | 16 / `20781c441fcac288c598f0722128d605f4defa2442541819cefbb74244b71f75` | [Fuyao REST API 文档入口](https://fuyao.aicubes.cn/docs/introduction/)、[股票行情](https://fuyao.aicubes.cn/docs/api-reference/prices/)、[公司行动](https://fuyao.aicubes.cn/docs/api-reference/corporate-actions/)、[财务](https://fuyao.aicubes.cn/docs/api-reference/financials/)、[期货行情](https://fuyao.aicubes.cn/docs/api-reference/futures-prices/)、[期权行情](https://fuyao.aicubes.cn/docs/api-reference/options-prices/)、[指数](https://fuyao.aicubes.cn/docs/api-reference/a-share-index/)、[交易日历](https://fuyao.aicubes.cn/docs/api-reference/calendar/)、[标的列表](https://fuyao.aicubes.cn/docs/api-reference/ticker-list/)、[基金分红](https://fuyao.aicubes.cn/docs/api-reference/fund-corporate-actions/)、[官方 llms-full.txt](https://fuyao.aicubes.cn/llms-full.txt) | 同花顺扶摇 API 行 1 待复核 |
| `yfinance` / 1 | `opendata/data/providers/yfinance/` | 6 / `67185554bf46b5f383001fcfbd5001675d2fdfd9829757b127d0caa56838fec4` | Yahoo 官方 [Developer API directory](https://developer.yahoo.com/api/)、[Yahoo API terms](https://legal.yahoo.com/us/en/yahoo/terms/product-atos/apiforydn/index.html)、[Finance subscriptions](https://finance.yahoo.com/subscriptions/) 可用于权利/产品范围复核。审阅到的官方页面中没有找到 Yahoo Finance 历史行情 API 的公开 schema；`yfinance` 是第三方 wrapper，不能把 wrapper 文档或返回结果称为 Yahoo 官方 API 规范。 | Yahoo Finance 行 8 待复核 |

哈希复算命令示例（其余 provider 替换目录名）：

```sh
find opendata/data/providers/akshare -type f -name '*.py' ! -path '*/__pycache__/*' -print0 \
  | sort -z | xargs -0 shasum -a 256 | shasum -a 256
```

初稿中的七个包摘要是当时的工作树快照，不是 Git tree hash；七个初稿 SHA 均保留。本次仅按同一算法新增重算了 FRED 9 个 Python 文件的当前快照（上表），其余六个包没有重算，其初稿 SHA 仍只代表初稿时点，不据此声称当前文件未变。逐包签认时应将 reviewer 实际检查的 path+line / 变更提交范围写入 `审查证据`，不要把 hash 当作审查结果。

## 3. cloudQuant 源码审查清单与近似复制抽样

本机存在 OpenBB checkout：`/Users/yunjinqi/Documents/new_projects/OpenBB`，HEAD 为 `3e071fcc2cd9f891cac6040ae60296dba76dab46`，与 `provider-inventory.yaml` 固定的基线相同；本轮只读其 provider 路径清单，未复制、摘录或修改代码。OpenBB 许可基线按迁移规划登记为 AGPL-3.0。

**审查结论目前统一为 `NOT_REVIEWED / 待签认`。** 下表是应检查的最小抽样路径，不是“相似/不相似”的结论；未完成实质对照前不填 `PASS`。路径存在性检查只能选样，不能作为零复制证明。

| 本仓抽样路径 | pinned OpenBB 对照/最近候选 | 抽样理由与结论栏 |
|---|---|---|
| `akshare/models/stock_daily.py`、`financial_statement.py`、`_normalize.py` | pinned checkout 下 `openbb_platform/providers/akshare/` 不存在；AKShare 是本地 MIT 搬运层 + 自研适配器，不是 `provider-inventory.yaml` 的 OpenBB 上游 provider | 无同名 provider 可直接配对。复核适配器是否仅使用自己的 mapping/契约，并以 Git blame、实际上游 AKShare 来源/许可区分内容来源；不能因无 OpenBB/akshare provider 目录就签零 OpenBB 源码。结果：待审。 |
| `ecb/models/rate.py` | `openbb_platform/providers/ecb/openbb_ecb/models/yield_curve.py`（概念近似，不等于相同 domain） | 检查查询构造、序列键与解析控制流；不得仅因两边都使用 ECB/SDMX 相关名称作相似结论。CPI/GDP 在该 OpenBB provider 文件名清单中没有同名模型。结果：待审。 |
| `fred/models/cpi.py`、`models/_series.py` | `openbb_platform/providers/fred/openbb_fred/models/consumer_price_index.py`、provider `utils/` | 选择一个共通数据模型和本地 series helper，审字段、空值与窗口处理的实现细节是否复制。结果：待审。 |
| `imf/models/cpi.py`、`models/_indicator.py` | `openbb_platform/providers/imf/openbb_imf/models/consumer_price_index.py`、provider `utils/` | 覆盖 indicator code/国家键/年份解析，而非只看 provider 注册样板。结果：待审。 |
| `oecd/models/cpi.py`、`models/unemployment.py`、`models/_client.py` | `openbb_platform/providers/oecd/openbb_oecd/models/consumer_price_index.py`、`models/unemployment.py`、`utils/` | 两个对应模型都有，作为此七包中较高覆盖的模型层抽样。结果：待审。 |
| `ths/models/stock_daily.py`、`models/_client.py`、`models/futures_daily.py` | OpenBB pinned tree 无同一 `ths` provider 路径 | 对其进行独立 API/消费者来源检查；不把“上游 provider 不存在”作为洁净室通过。结果：待审。 |
| `yfinance/models/stock_daily.py`、`models/_sdk.py` | `openbb_platform/providers/yfinance/openbb_yfinance/models/equity_historical.py`、provider `utils/` | 核对日期边界、adjust、拆分/分红处理、未定盘行和异常语义；C8 是行为纠错证据，不替代源码独立性签认。结果：待审。 |

cloudQuant 的逐包审查至少回答以下问题，并记录证据路径/行号、审查人和日期：

1. **来源与许可**：每个非搬运文件由谁编写、参考了哪些供应方文档/响应；是否参考或复用了 OpenBB 实现、tests、录制样本或表达独特的解析/重试/字段组装；仅引用公开 API 名称/字段元数据是否可由官方资料对应。不得用 docstring 或提交信息的自我声明替代核对。
2. **源码和依赖面**：是否存在 OpenBB 代码、测试/样例、依赖、模块导入或复制后改名；仓库的零依赖/AST 静态扫描只证明其各自扫描面，不证明内容独立。搬运的 AKShare MIT 来源与自研 provider 源码必须分开审查。
3. **取数契约**：`transform_query → extract_data → transform_data` 的窗口/分页、请求次数、日期/时区、稳定排序、空帧/缺值、单位、币种、代码/国家/指标键、修订/vintage、复权及 fail-closed 语义是否能追溯到供应方官方资料或批准的人工映射。
4. **请求安全**：FRED/THS 凭证只从配置传输；日志与异常不泄漏凭证；请求超时、限流、重试、错误码、下载体大小、解析边界与依赖隔离是否合规。此包未读取配置/凭据，不对凭证现状作结论。
5. **数据权利**：对照 `docs/data-rights-registry.md` 的落库、再分发、商业使用边界。当前七类源对应登记均为“待复核/待确认”，代码 review PASS 不等于数据授权 PASS。

| Provider | 审查版本（包 SHA 对应本包 §2） | 审查人 | 审查证据（仅填路径/行号/commit，不粘源码） | 源码独立性结论 `PASS/FAIL/BLOCKED` | 差异/后续 |
|---|---|---|---|---|---|
| akshare | 本包快照 | cloudQuant（待签） | 待补 | NOT_REVIEWED | 待补 |
| ecb | 本包快照 | cloudQuant（待签） | 待补 | NOT_REVIEWED | 待补 |
| fred | 本包快照 | cloudQuant（待签） | 待补 | NOT_REVIEWED | 待补 |
| imf | 本包快照 | cloudQuant（待签） | 待补 | NOT_REVIEWED | 待补 |
| oecd | 本包快照 | cloudQuant（待签） | 待补 | NOT_REVIEWED | 待补 |
| ths | 本包快照 | cloudQuant（待签） | 待补 | NOT_REVIEWED | 待补 |
| yfinance | 本包快照 | cloudQuant（待签） | 待补 | NOT_REVIEWED | 待补 |

## 4. 33 条 active leg：请求方确认与状态对账

场景逐字取自 `opendata/data/openbb_map.yaml`；状态取该 map 与 fetcher `Capability.verified` 当前声明的交集。C65 用户直接确认 cloudQuant 负责全部 33 条，用于量化研究、回测与数据中台；`provider-requester-confirmation.json` 已逐腿对账并记录继续启用。此请求方确认不等于请求方已签认每个源字段、单位、key 或业务口径，也不代表数据权利或源码审查完成。

| # | Provider / domain | Machine status | 已有迭代批次依据 | 已登记消费场景 | C65 已确认请求方 | 请求确认依据 |
|---:|---|---|---|---|---|---|
| 1 | `akshare / stock_daily` | registered | P0 · 1A | 行情日线（回测数据准备 / dwd 日线域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 2 | `ths / stock_daily` | verified | P0 · 1A | 行情日线（回测数据准备 / dwd 日线域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 3 | `yfinance / stock_daily_overseas` | verified | P1 · 1C；OpenBB provider 清单批次另标 P0 | 行情日线（回测数据准备 / dwd 日线域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 4 | `ecb / economy_cpi` | verified | P1 · 1C；OpenBB provider 清单批次另标 P0 | 宏观 CPI（AI 研究 / 资产配置） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 5 | `fred / economy_cpi` | verified | P1 · 1C；OpenBB provider 清单批次另标 P0 | 宏观 CPI（AI 研究 / 资产配置） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 6 | `imf / economy_cpi` | verified | P1 · 1C；OpenBB provider 清单批次另标 P0 | 宏观 CPI（AI 研究 / 资产配置） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 7 | `oecd / economy_cpi` | verified | P1 · 1C；OpenBB provider 清单批次另标 P0 | 宏观 CPI（AI 研究 / 资产配置） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 8 | `ecb / economy_gdp` | verified | P1 · 1C；OpenBB provider 清单批次另标 P0 | 宏观 GDP（AI 研究 / 资产配置） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 9 | `fred / economy_gdp` | verified | P1 · 1C；OpenBB provider 清单批次另标 P0 | 宏观 GDP（AI 研究 / 资产配置） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 10 | `imf / economy_gdp` | verified | P1 · 1C；OpenBB provider 清单批次另标 P0 | 宏观 GDP（AI 研究 / 资产配置） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 11 | `fred / economy_unemployment` | verified | P1 · 1C；OpenBB provider 清单批次另标 P0 | 宏观失业率（AI 研究 / 资产配置） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 12 | `imf / economy_unemployment` | verified | P1 · 1C；OpenBB provider 清单批次另标 P0 | 宏观失业率（AI 研究 / 资产配置） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 13 | `oecd / economy_unemployment` | verified | P1 · 1C；OpenBB provider 清单批次另标 P0 | 宏观失业率（AI 研究 / 资产配置） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 14 | `ecb / economy_rate` | verified | P1 · 1C；OpenBB provider 清单批次另标 P0 | 宏观利率（AI 研究 / 资产配置 / 回测融资成本） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 15 | `akshare / futures_daily` | registered | P0 · 1B | 期货日线（期货回测数据准备 / dwd 期货域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 16 | `ths / futures_daily` | verified | P0 · 1B | 期货日线（期货回测数据准备 / dwd 期货域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 17 | `akshare / fund_etf_daily` | registered | P2 · 基金扩展 | ETF 日线（场内基金回测数据准备 / dwd ETF 域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 18 | `ths / fund_etf_daily` | verified | P2 · 基金扩展 | ETF 日线（场内基金回测数据准备 / dwd ETF 域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 19 | `akshare / index_daily` | registered | 1B / B1.1 P1 搬运模块视角（`index/`）；§6.2 未给该 domain 独立优先级 | 指数日线（业绩基准与指数增强回测 / dwd 指数域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 20 | `ths / index_daily` | verified | 1B / B1.1 P1 搬运模块视角（`index/`）；§6.2 未给该 domain 独立优先级 | 指数日线（业绩基准与指数增强回测 / dwd 指数域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 21 | `akshare / option_daily` | registered | P2 · 期权扩展 | 期权合约日线（期权策略回测数据准备 / dwd 期权域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 22 | `ths / option_daily` | verified | P2 · 期权扩展 | 期权合约日线（期权策略回测数据准备 / dwd 期权域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 23 | `akshare / bond_daily` | registered | 1B / B1.1 P1 搬运模块视角（`bond/`）；§6.2 未给该 domain 独立优先级 | 可转债日线（转债轮动策略数据准备 / dwd 债券域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 24 | `ths / stock_action` | verified | P0 · 1B（复权/公司行动） | 除权除息事件（dwd 事件域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 25 | `akshare / stock_action` | registered | P0 · 1B（复权/公司行动） | 除权除息事件（dwd 事件域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 26 | `akshare / financial_statement` | registered | P0 · 1B（财务） | 财务报表（dwd 财务域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 27 | `ths / financial_statement` | verified | P0 · 1B（财务） | 财务报表（dwd 财务域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 28 | `akshare / financial_indicator` | registered | P0 · 1B（财务；FR-2） | 财务指标（dwd 财务域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 29 | `akshare / index_constituent` | registered | P0 · 1B（指数成分） | 指数成分股（dwd 指数域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 30 | `ths / index_constituent` | verified | P0 · 1B（指数成分） | 指数成分股（dwd 指数域） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 31 | `ths / trading_calendar` | verified | FR-2/A1 契约骨架；A3.2 `/api/meta/*` P0 端点；不是 §6.2 独立市场数据域 | 交易日历（跑批窗口端点与新鲜度 lag 的期望判据，A4.7） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 32 | `ths / instrument` | verified | FR-2/A1 契约骨架；A3.2 `/api/meta/*` P0 端点；不是 §6.2 独立市场数据域 | 标的目录（上市区间谓词：空窗是「未上市/已退市」还是丢数的判据；入库前代码消歧） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |
| 33 | `ths / fund_action` | verified | P2 · 基金扩展；D10 ETF 还原依赖 | 基金分红事件（ETF 前复权序列换回不复权的因子来源，D10 前置件） | cloudQuant · 量化研究/回测/数据中台 | 已确认继续启用；见 C65 请求方记录 |

### 逐腿确认的最小签字内容

C65 的直接用户回复已覆盖全部 33 条 active leg，并由 `provider-requester-confirmation.json` 逐腿记录请求方、用途及“继续启用”决定；本包不再把这些腿标作 `WAITING_FOR_REQUESTER`。这项确认没有签认源字段、单位、key、时区、adjusted 语义、消费窗口细则、数据来源/权利或 OpenBB 源码审查结论；这些仍需相应的样本证据和人工审查。

## 5. 现有逐腿对照证据：能证明什么

| 面 | 已有可复核档案 | 可证明 | 不能替代 |
|---|---|---|---|
| AKShare 搬运保真 | `scripts/codemod/compare_with_upstream.py`；`tests/fixtures/upstream/`；`docs/evidence/A2/compare-report.md`；`scripts/codemod/report_port.py` / `docs/port-report.md` | compare 工具可用录制 HTTP transcript 离线 replay upstream 与 `opendata_http`，校验列名/顺序、shape、dtype、值和请求次数；`report_port.py` 对锁定源文件重放改写登记 | 不是 7 个 provider adapter 的官方值对照器，不证明 P1 抽样分母/覆盖，不证明厂商再分发权利 |
| AKShare A2 当前报告 | `docs/evidence/A2/compare-report.md` | 14 个录制案例 PASS | 4 个 Eastmoney 案例目前 PENDING（`stock_daily_raw/qfq`、`index_daily_em`、`fund_etf_daily_em`）；PENDING 不计 PASS，Sina 孪生 fixture 也不替代这些 endpoint 的 Eastmoney 结论 |
| Provider 专用真机核对 | C1 / C5 / C6 / C8 / C9 / C10 / C13 / C14 / C15 / C20 的 README 与逐腿报告；C65 `fred-official-comparison.json` | yfinance、若干 THS domain 已有明确路径与样本的 live/跨 vendor 证据；C65 另对 FRED 三腿完成官方 JSON 与 CSV 同源对照，样本行数为 80/26/80，均零差异并 PASS | C1 README 中“没有 FRED 独立对照”保留为 C1 当时的历史事实；C65 是后续新增证据，仍不是跨 vendor 或独立统计真值比较。已 verified 的 `Capability` 不自动表示 AC-6 每腿对照强度一致 |
| 官方 Fuyao 文档目录 | `scripts/ops/fuyao_endpoint_inventory.py`；`docs/evidence/B2/fuyao-endpoint-inventory.txt`；`opendata_fuyao/endpoint_map.yaml` | 官方 `llms-full.txt` 的 57 页面/97 端点清单被机器抽取；官方资料列出 10 年日 K、10 日增量、复权因子三个 Parquet 导出端点 | B2 抽取项只含页面/端点名称；文档页面不是逐字段 signed schema/value 对照 |
| C65 官方 Parquet 样本与比较 | `official-parquet-download.json`、`official-parquet-comparison.json`、`parquet-comparison-independent.txt`；下载文件在下载记录指明的 Git 外目录 | 两个固定样本的 SHA、footer 与行数见下载记录。现有比较报告中，日线 55,552 行 PASS（缺失/多余 key、重复 key、值差异均为 0）；公司行动样本 57,530 行 FAIL，原始 key 有 3 组重复，整体报告为 FAIL | 样本 footer 和机器比较不能替代长期官方 schema、字段/单位/key/时区/adjusted 语义人工签认或权利确认；公司行动重复键还须裁定预期业务粒度与处理方式 |

可公开引用的 Fuyao 官方材料：[官方 API 文档聚合 `llms-full.txt`](https://fuyao.aicubes.cn/llms-full.txt)、[官方介绍](https://fuyao.aicubes.cn/docs/introduction/)、[市场趋势示例](https://fuyao.aicubes.cn/best-practices/06-marketdb-research/example.html)。公开资料支持端点用途和 dump 类型；C65 已固定两份官方 Parquet 实样并生成结构/值比较报告：日线 55,552 行 PASS，公司行动样本 3 组重复 key 导致 FAIL。报告对 `opendata_fuyao/dumps.py` 的解析结果和样本进行自动核对；实样 schema 仍不等同于长期稳定的官方字段规范。字段、单位、key、adjusted 与时区语义尚无人工签认；不能把 `DAILY_K_COLUMNS` / `ADJUSTMENT_FACTOR_COLUMNS` 等内部常量当作供应方规格。

## 6. §4\|04 自动对照现状与人工签认缺口

不需要新建第二套 provider 框架。C65 已归档两份官方来源 Parquet 样本和 `official-parquet-comparison.json`：日线 55,552 行 PASS；公司行动样本 57,530 行因 3 组重复 key FAIL。比较报告不代表再分发权已确认，也不构成字段语义、单位或 key 的人工签认。该文档修订只更新证据摘要，没有重新运行比较器或测试。

1. **AKShare AC-6 报告**：以冻结 `upstream.lock` 为源文件集合，分别按 A2.1 stock daily closure 和 B1.1 P1 子模块组构造 manifest（源 commit、子模块、文件集合、hash）。将 `compare_with_upstream.py` 已有 fixture/case 与 manifest 项逐项关联，报告全量搬运文件分母、抽样子模块/文件分母与分子、PENDING/不可采样、差异和容忍度。已出现的 `futures_derivative` 12 文件按 `futures/` B1.1 搬运组单列，不因目录名自动把全部函数归入 `futures_daily`。report_port 重放报告证明源码搬运改写的一致性；它不是 provider 官方值对照，也不能单独证明 AC-6 抽样门槛。
2. **Fuyao Parquet 报告审阅**：现有 `official-parquet-comparison.json` 与 `parquet-comparison-independent.txt` 已记录样本 hash、footer schema、解析列、key 对齐、行数与差异。日线全 55,552 行零 key/value 差异且 PASS；公司行动存在 3 组重复 key 并 FAIL。审阅需先判定重复组是否为官方源中的有效多事件还是 key/规范化缺陷，再确认其余字段的来源、单位、key、时区和 `adjusted=none` 语义；不得把机器比较报告等同人工签认。
3. **后续比较器维护**：若未来修改比较器，再为列增删/重排、类型漂移、日期/代码键、价格/成交量/金额单位、`adjusted=none`、空值、重复键、时区与容忍度边界保留离线覆盖。合成 fixture 只验证判定逻辑，不证明官方输出正确。
4. **映射人工确认**：审阅人按 §4|02 检查 `akshare.yaml`/`ths.yaml` 每个字段、单位与 key，引用所用源字段与一条有 provenance 的原始响应；对没有稳定公开 schema 的网页通道显式写 `BLOCKED/NO_OFFICIAL_SCHEMA`，不能用代码现状倒推出“已确认”。

当前自动比较已有报告，但公司行动重复键仍为 FAIL；字段含义、单位、adjusted/key 口径、样本代表性和数据权利的接受结论仍需人工复核。具体报告仍要满足 AC-6 的现有容忍度和记录判据；本包不调整阈值。

## 7. 待签的 provider、请求方和数据权利结论

| 结论 | 当前状态 | 可转正所需证据 |
|---|---|---|
| AC-10\|01 七个启用 provider 每条 active leg 对照 | `INCOMPLETE`：23 条 map/capability 为 `verified`、10 条仍 `registered`；FRED 三腿新增官方 JSON/CSV 同源比较（80/26/80 行，均 PASS），但全量逐腿对照类别及强度尚未统一签认 | provider/source/domain 逐腿证据索引、官方字段/值来源、请求数/窗口、差异及容忍度、reviewer 的每腿结论；或有权请求方逐腿批准移出并转 `on-demand` |
| AC-10\|02 具名消费方 | `CONFIRMED`：用户直接确认 cloudQuant 负责全部 33 条，用于量化研究、回测与数据中台；逐腿记录在 `provider-requester-confirmation.json` | 此用户回复与逐腿对账记录构成请求方/用途/继续启用确认；不扩张为字段、来源、权利或源码审查签认 |
| AC-10\|03 OpenBB 源码独立审查 | `NOT_REVIEWED`：已列出采样路径与核查点，cloudQuant 待逐包签认 | 七个包对应实际审查证据、版本/hash、结论、审查人/日期；不接受 docstring 自称、无 `openbb` import 或“开发时未参照”自述单独作为证明 |
| 数据权利 | `BLOCKED / 待复核`：相关 provider/source 的权利登记仍未确认落库/再分发/商业使用权限；AKShare 还需按底层来源分别审 | 对应供应方合同/条款条文、使用模式审阅人与日期。源码审查及 provider verified 状态不等于授权 |
| §4\|04 Fuyao 官方 Parquet 字段/值对照 | `INCOMPLETE`：自动比较报告已存在；日线 55,552 行 PASS，公司行动 3 组重复 key 导致 FAIL。字段语义/单位/key/时区/adjusted 的人工签认与权利结论未完成 | 复核重复 key 的业务含义与处理方式；由审阅人结合官方资料、样本、mapping 与差异报告签认字段和单位/key/时区/adjusted 语义。数据权利单独审查 |

## 8. AC-5\|02 当前状态更正：`futures_derivative`

需求 `docs/迭代计划/迭代1-重构数据中台/需求文档.md` §3 FR-4 明确点名 `futures_derivative`；实施计划 B1.1 也列 `futures` 等模块；C61/C64 的 gap 文案称它是唯一实际未搬目录。**当前共享工作树不再符合该旧理由**：

| 只读盘点 | 当前观察 |
|---|---|
| MIT 上游仓库 | `/Users/yunjinqi/Documents/new_projects/akshare`，HEAD=`c4f6a631c259783dbc2507b6b27d179b3e88079d`；仓库 `LICENSE` 为 MIT。 |
| 上游 `akshare/futures_derivative/` | 12 个源码 `.py`（不含 `__pycache__`）：`__init__.py`、`cons.py`、6 个交易所合约资料模块、`futures_cot_sina.py`、`futures_hog.py`、`futures_index_sina.py`、`futures_spot_sys.py`。 |
| 当前 `opendata_http/futures_derivative/` | 当前可见 12 个同名 `.py`；目录显示为 untracked。逐文件内容/哈希保真本包未运行 compare。 |
| `opendata_http/upstream.lock` | 当前包含这 12 个 target/source path 配对；lock 显示 modified。 |
| `opendata_http/manifest.json` 与 `docs/port-report.md` | 当前 manifest 统计 325 个 `.py` + 2 个资源 = 327；port report 头部与逐文件表为 327。`futures_derivative` 已出现 12 行，逐行显示 import rewrite 为 0（仅 `futures_index_sina.py` 为 2）、string rewrite 为 0、manual edits 为 False、replay 为 ✓。 |
| 可复用工具 | `scripts/codemod/port_module.py`（对已搬子模块可幂等比对/改写/更新 lock）；`scripts/codemod/gen_manifest.py`；`scripts/codemod/report_port.py`；`scripts/codemod/compare_with_upstream.py` 的 offline `--compare`。report 当前的重放列提供这 12 个文件的现有搬运证据；仍须由主验收流程执行 manifest freshness/全量门禁，并按 AC-6 定义形成文件/域覆盖矩阵。 |
| 静态可见依赖 | 源码 imports 出现 `pandas`、`requests`、`beautifulsoup4`，以及仓内 `opendata_http.utils.demjson`；需对 pyproject/资源/子模块调用链完整核对，不据此断言所有 runtime 依赖已闭合。 |

上述只读事实足以否定“该目录目前完全不存在/无本地许可基线”，也比“目录存在”多一层证据：当前 manifest、lock、port report 的总数均为 327，报告中该目录的 12 行逐项显示 replay ✓。本包未独立运行 manifest freshness、compare、官方输出回放或 bandit triage；因此这些现有报告记录可纳入 AC-5 文件级对账，但不能单独证明 B1.3 的抽样范围覆盖/≥20%、每项验收全部门禁或数据权利。C61/C64 gap 理由应从“唯一未搬目录”更正为：当前分母/范围来源存在于 lock/manifest/report，但尚需按 A2.1 日线闭包与 B1.1 子模块范围建立可机读批次标签及逐项校验，并完成 AC-6 抽样报告。不能再沿用目录缺失这一过期理由，也不能仅凭端目录出现就将 AC-5 或 AC-6 判为通过。

## 9. 用户委托的当前 provider 源码审阅

审阅人：Codex 主智能体（AI）；用户直接授权“你帮我直接审阅”。固定本地 OpenBB 基线 `3e071fcc2cd9f891cac6040ae60296dba76dab46` 只读使用，未复制其实现进入本仓库。当前7包71个Python文件的逐文件SHA、静态入口及比较边界见 `provider-source-review.json`。本轮在对应客户端/模型的源码对照中未观察到复杂实现块复制；公开字段名、API端点及三阶段名称的相同本身不作复制结论。

| provider | 当前比较范围与差异 | 结论 |
|---|---|---|
| akshare | 无同名OpenBB provider；本地adapter调用冻结MIT搬运源；15文件检查注册/查询/映射边界。 | 当前限定范围已审阅；未观察到复杂实现复制；历史开发过程未验证。 |
| ecb | 本地series CSV KEY/TIME_PERIOD/OBS_VALUE 与 /service/data 接口；OpenBB yieldcurve走data-detail-api JSON+期限并行聚合。 | 当前限定范围已审阅；未观察到复杂实现复制；历史开发过程未验证。 |
| fred | 双方公开FRED endpoint及三阶段名称相同；本地单series date/value缺失点保留，OpenBB多series metadata/frequency/realtime与宽表处理不同。 | 当前限定范围已审阅；未观察到复杂实现复制；历史开发过程未验证。 |
| imf | 本地DataMapper values[indicator][country] annual mapping，OpenBB SDMX query builder+数据流metadata不同。 | 当前限定范围已审阅；未观察到复杂实现复制；历史开发过程未验证。 |
| oecd | 本地HICP/LFS_INDIC八维key+CSV重构+手工月/季/年日期；OpenBB DF_PRICES_ALL/DF_IALFS_UNE_M query过滤、国家映射、百分数缩放。 | 当前限定范围已审阅；未观察到复杂实现复制；历史开发过程未验证。 |
| ths | 无同名OpenBB provider；本地11个Fetcher封装Fuyao接口及自身批量/公司行动/ETF还原。 | 当前限定范围已审阅；未观察到复杂实现复制；历史开发过程未验证。 |
| yfinance | 本地Ticker.history auto_adjust=False + 完整splits + 严格窗口/回溯复权还原；OpenBB yf_download多ticker/action/adjusted mode不同。 | 当前限定范围已审阅；未观察到复杂实现复制；历史开发过程未验证。 |

本轮运行注册入口实际读数为33腿、23 verified、10 registered，和逐键清单一致；registered均来自AKShare，不会以人工代码审阅冒充数据对照转正。没有发起供应方请求、写生产库或修改provider源码。模块中的自声明不是历史证据：7包历史commit声明当前为0，AI评审不能替原作者追认开发时的行为，不改写Git历史。AC-16涉及历史声明的缺口仍保留；对应provider当前源码审阅已执行，不再写“无人审阅”。

## 10. 当前源码近似块筛选与主审抽查

2026-10-01追加：`provider-similarity-inventory.json` 对7包71py、146个函数/方法与固定OpenBB五包121py、555个函数/方法进行AST启发式筛选；两侧解析错误均为0。保留调用、字段、字符串和操作符，局部变量名归一化；大于40节点的109个本地块中，无块同时达到记录的三项候选阈值。主线程实际阅读排名最高的三对源码并记录 `provider-nearest-review.json`：基金事件窗口过滤与IMF父链单位继承、指数成分建模与SDMX维度拼装、不同供应商日期请求都没有观察到共享复杂实现。

这是当前逐provider审阅的补充证据；启发式会漏掉重排、拆分或跨函数改写，不证明历史开发过程或全仓库源码来源。AC-10第3条按当前七包审查记录核对；AC-16历史声明与全库断言继续保留缺口。
