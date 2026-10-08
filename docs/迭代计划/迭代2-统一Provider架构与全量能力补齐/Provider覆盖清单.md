# Provider覆盖清单

日期：2026-10-07。以固定基线32源/350条模型身份为准，加AkShare、THS共34个目标provider。下表“当前能力/verified”来自现有能力映射与只读源码盘点，不能换算成上游模型完成率。逐模型等价判断尚未执行。

## 0. 修订记录

| 版本 | 日期 | 来源与处置 | 落点 |
| --- | --- | --- | --- |
| 1.0 | 2026-10-07 | 固定32/350与自有2源 | §1—5 |
| 1.1 | 2026-10-07 | R2-01/06/08/11、F-03/04/09；身份不变，CSV字段扩展 | §4字段定义、阶段/凭据/候选族分母 |

## 1. 32个固定上游来源

| Provider | 固定模型条目 | 当前能力 / verified | 阶段 | 模型名称示例 | 凭据字段（固定上游声明） |
| --- | ---: | ---: | --- | --- | --- |
| alpha_vantage | 3 | 0 / 0 | 2C：商业/聚合 | EquityHistorical, HistoricalEps, EtfHistorical | api_key |
| benzinga | 4 | 0 / 0 | 2D：其他来源 | AnalystSearch, CompanyNews, WorldNews, PriceTarget | api_key |
| biztoc | 1 | 0 / 0 | 2D：其他来源 | WorldNews | api_key |
| bls | 2 | 0 / 0 | 2B：其他宏观 | BlsSearch, BlsSeries | api_key |
| cboe | 11 | 0 / 0 | 2D：其他来源 | AvailableIndices, EquityHistorical, EquityQuote, EquitySearch | 未声明必需字段 |
| cftc | 2 | 0 / 0 | 2D：其他来源 | COT, COTSearch | app_token |
| congress_gov | 8 | 0 / 0 | 2D：其他来源 | CongressBills, CongressBillInfo, CongressBillText, CongressAmendments | api_key |
| deribit | 5 | 0 / 0 | 2D：其他来源 | FuturesCurve, FuturesHistorical, FuturesInfo, FuturesInstruments | 未声明必需字段 |
| ecb | 3 | 3 / 3 | 2B：既有源补齐 | BalanceOfPayments, CurrencyReferenceRates, YieldCurve | 未声明必需字段 |
| econdb | 8 | 0 / 0 | 2B：其他宏观 | AvailableIndicators, CountryProfile, EconomicIndicators, ExportDestinations | api_key |
| eia | 2 | 0 / 0 | 2B：其他宏观 | PetroleumStatusReport, ShortTermEnergyOutlook | api_key |
| famafrench | 6 | 0 / 0 | 2B：其他宏观 | FamaFrenchBreakpoints, FamaFrenchCountryPortfolioReturns, FamaFrenchFactors, FamaFrenchInternationalIndexReturns | 未声明必需字段 |
| federal_reserve | 13 | 0 / 0 | 2B：其他宏观 | CentralBankHoldings, FederalFundsRate, FomcDocuments, InflationExpectations | 未声明必需字段 |
| finra | 2 | 0 / 0 | 2D：其他来源 | OTCAggregate, EquityShortInterest | 未声明必需字段 |
| finviz | 7 | 0 / 0 | 2D：其他来源 | CompareGroups, EtfPricePerformance, EquityInfo, EquityScreener | 未声明必需字段 |
| fmp | 69 | 0 / 0 | 2C：商业/聚合 | AnalystEstimates, AvailableIndices, BalanceSheet, BalanceSheetGrowth | api_key |
| fred | 36 | 3 / 3 | 2B：既有源补齐 | BalanceOfPayments, BondIndices, CommoditySpotPrices, ConsumerPriceIndex | api_key |
| government_us | 6 | 0 / 0 | 2D：其他来源 | CommodityPsdData, CommodityPsdReport, TreasuryAuctions, TreasuryPrices | 未声明必需字段 |
| imf | 8 | 3 / 3 | 2B：既有源补齐 | AvailableIndicators, ConsumerPriceIndex, DirectionOfTrade, EconomicIndicators | 未声明必需字段 |
| intrinio | 38 | 0 / 0 | 2C：商业/聚合 | BalanceSheet, CalendarIpo, CashFlowStatement, CompanyFilings | api_key |
| multpl | 1 | 0 / 0 | 2B：其他宏观 | SP500Multiples | 未声明必需字段 |
| nasdaq | 9 | 0 / 0 | 2D：其他来源 | CalendarDividend, CalendarEarnings, CalendarIpo, CompanyFilings | api_key |
| oecd | 9 | 2 / 2 | 2B：既有源补齐 | CompositeLeadingIndicator, ConsumerPriceIndex, CountryInterestRates, GdpNominal | 未声明必需字段 |
| sec | 24 | 0 / 0 | 2D：其他来源 | BalanceSheet, BalanceSheetGrowth, CashFlowStatement, CashFlowStatementGrowth | 未声明必需字段 |
| seeking_alpha | 3 | 0 / 0 | 2C：商业/聚合 | CalendarEarnings, ForwardEpsEstimates, ForwardSalesEstimates | 未声明必需字段 |
| stockgrid | 1 | 0 / 0 | 2D：其他来源 | ShortVolume | 未声明必需字段 |
| tiingo | 7 | 0 / 0 | 2C：商业/聚合 | EquityHistorical, EtfHistorical, CompanyNews, WorldNews | token |
| tmx | 24 | 0 / 0 | 2D：其他来源 | AvailableIndices, BondPrices, CalendarEarnings, CompanyFilings | 未声明必需字段 |
| tradier | 5 | 0 / 0 | 2D：其他来源 | EquityHistorical, EtfHistorical, EquityQuote, EquitySearch | api_key, account_type |
| tradingeconomics | 1 | 0 / 0 | 2B：其他宏观 | EconomicCalendar | api_key |
| wsj | 3 | 0 / 0 | 2D：其他来源 | ETFGainers, ETFLosers, ETFActive | 未声明必需字段 |
| yfinance | 29 | 1 / 1 | 2B：既有源补齐 | AvailableIndices, BalanceSheet, CashFlowStatement, CompanyNews | 未声明必需字段 |

凭据列仅为固定注册元数据，不是部署环境变量名或当前端点授权结论。Key是否必需、适用模型、免费/付费访问与支持版本，应在对应模型契约卡中向底层数据源官方文档核对。尤其EIA的PetroleumStatusReport与ShortTermEnergyOutlook的Key需求不同；旧库存漏记凭据应修正。

## 2. 项目自有来源

| Provider | 当前能力 / verified | 迭代2范围 |
| --- | ---: | --- |
| akshare | 10 / 0 | 迁入_vendor；保留金融搬运函数；统一Provider定义、模型包装与服务入口；10条能力逐腿对照，补期货等迭代1域欠项 |
| ths | 11 / 11 | 传输/端点/dump归同一provider；保留当前已验证能力；补财务指标与官方端点矩阵中可实现的迭代1欠项 |

AkShare、THS未计入350分母，它们的能力/端点清单独立建账。THS financial_indicator当前在openbb_map.yaml为pending且未注册；没有公告时点证据前不推造日期或提前转正。

## 3. 分阶段规模

| 阶段 | 来源数 | 固定模型条目 | 解释 |
| --- | ---: | ---: | --- |
| 2A | 34个描述符目标 | 不按模型计完成 | 统一核心/目录/依赖/工具链；当前7源行为先回归 |
| 2B | 5个既有 + 7个其他宏观 | 118 | 既有5源对应85条固定模型，7个其他宏观33条；这些数字不是剩余开发数量 |
| 2C | 5 | 120 | FMP69、Intrinio38等是主要工作量；无Key不阻止契约/实现/离线验证 |
| 2D | 15 | 112 | 监管、衍生品、新闻和其他市场来源 |
| 合计 | 32个上游源 | 350 | 202个不同模型名；另有2个自有来源 |

## 4. 完整模型任务的使用方式

模型级任务清单.csv有350行，task_id由provider与模型名构成。既有域列只是候选，coverage_status=NOT_ASSESSED；implementation_task_status=NOT_RUN表示本迭代的逐条开发/复用验收尚未执行，不能解释为所有旧能力不存在。

每条后续应绑定：本地契约卡、模块/Fetcher、domain/capability、参数/字段/单位/业务键/时间支持、官方依据、离线案例、真实样本与独立核对、存储/服务入口、实现版本、证据状态和限制。需要拆分或共享时保留原task_id，不减分母。

| 字段/轴 | 本轮初值与使用约束 |
| --- | --- |
| batch | 保留旧库存的历史分组P0=85/P1=153/P2=112，不作为当前优先级、阶段或验收放行条件 |
| phase | 本轮机读阶段2B=118/2C=120/2D=112；P1横跨2B的33宏观与2C的120商业，禁止用batch替代phase |
| requester / scenario / scenario_status | requester=cloudQuant；scenario留空、NOT_ASSESSED，待逐模型确认具体场景；空值不能通过AC2-08 |
| contract_family_candidate / contract_family_status / provider_delta_ref | 候选为upstream_model；NOT_ASSESSED且差异证据引用留空；202名/68重名/216行只是组织草稿的依据 |
| rights_status / rights_review_task_id / rights_evidence_ref | 新27源NOT_REGISTERED，既有5源EXISTING_SOURCE_REVIEW_MODEL_PENDING；RIGHTS2-source是前置任务身份，非已批准许可；空依据不能放行live/落库/分发 |
| credential_fields / credential_requirement_status | 固定上游字段分号分隔；逐模型必需性NOT_ASSESSED，不把声明字段一律当必需，也不把未声明当无授权要求 |
| key_decision_status / access_decision_ref / live_target | 15个声明凭据来源PENDING_DECISION，其余17源PENDING_ACCESS_ASSESSMENT；ACCESS2-source引用待办清单；默认目标SOURCE_VERIFIED_WHEN_AUTHORIZED，不代表Key已申请 |
| async_mode | NOT_ASSESSED；开发后逐模型取native_async/bounded_thread/unsupported，并匹配AC2-16适用面 |

真实运行状态三列保持原NOT_ASSESSED/NOT_RUN/NOT_RUN。日后决定不申请Key时仅将live_target改为DEV_DONE_ONLY_PLANNED并保留理由，live_verification_status记实际NOT_RUN/BLOCKED，不能改为PASS或减分母。权利详细三项结论仍以唯一rights登记表为准，CSV状态必须由引用证据派生。

凭据暴露15源/195行=2B49+2C117+2D29，seeking_alpha虽属商业阶段但未声明凭据；访问决策在2B之前开始，按底层API逐产品核实。权利和Key决策表见[前置条件与决策清单.md](前置条件与决策清单.md)，静态归并见基线快照review_metrics。

不因缺Key把条目写成已完成或从计划删除；不因同一provider已有一条verified能力把整行模型数转正；不因某个上游模型名相同就认定不同provider输出可互换。

## 5. 版本和盘点修复

旧provider-inventory.yaml自报348，EIA行写0/空模型/空凭据；冻结源码同一注册声明明确有2个模型。15行省略号还隐藏了完整名称。迭代2工作包WP2-00应以本计划的完整固定元数据为依据更新正式库存和判据，保留旧文件/证据版本用于追溯。

OpenBB V5已经调整来源范围与路由形态，见[官方provider文档](https://docs.openbb.co/odp/python/extensions/providers)。本项目继续自研固定基线全部功能，查底层源当前API；jodi及其他新功能作为明确新增范围记录。

本文件为规划清单，不是开发或来源验收通过清单。
