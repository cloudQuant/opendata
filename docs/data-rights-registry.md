# 数据源权利登记表

> 版本：v1.7（迭代2新增 Benzinga / EconDB / Tiingo 三条专属登记（§1 #17—19）；v1.6 新增 Intrinio 专属登记；v1.5 新增美联储理事会专属登记；登记完整性不等于所有用途获准）
> 日期：2026-10-12（第17—19行本轮审阅；第16行为 2026-10-11；第15行为 2026-10-09；第12—14行为 2026-10-08；C65 既有行复核日期仍为 2026-09-30；首版 2026-09-22）
> 依据：迭代1 决策 D11（数据权利与再分发）、设计 §1.3
> 维护：数据源变更、条款变更、复核结论变化时必须更新本表

## 0. 为什么单独登记

**代码许可证 ≠ 数据权利**。opendata 的模式是"集中获取 → 落库 → 经 REST/WS 向多消费方再分发"，
并允许商业授权——这已构成数据分发/经营行为，与 akshare"工具中立、用户自担"的定位有本质区别。
因此每个数据源都必须明确：条款是否允许本项目落库、是否允许向第三方再分发、是否允许商业使用。

**定位声明**：opendata 的商业授权话术定位为"**软件使用许可**"，不含数据销售、不主张数据权利。

## 1. 登记表

状态取值：`已复核` / `已复核（受限）` / `待复核（本迭代已接入，条款原文未取得）`（2026-10-12 新增：本机客户端只拿到 HTTP 403、条款原文未取回时不得记「已复核」，见 §1.1）/ `不适用`。用户授权 Codex 主线程直接审阅，责任人 cloudQuant；这不是供应商授权，也不是冒署 cloudQuant 人工签名。既有行依据和限制见 [C65 审阅记录](evidence/C65/data-rights-review.md)；新增行依据见下文 §1.1。本次没有申请账号、购买订阅或调用数据 API。

| # | 数据源 | 接入方式 | 条款链接 | 允许本项目落库 | 允许再分发 | 允许商业使用 | 复核日期 | 责任人 | 状态 |
|---|--------|---------|---------|--------------|-----------|------------|---------|--------|------|
| 1 | 同花顺扶摇 API | `opendata/data/providers/ths/transport` 直连（X-api-key） | https://fuyao.aicubes.cn/docs/api-reference/overview/ | API 支持研究/回测及全市场导出；本项目长期留存权限未获公开条款证明 | 无本项目下游分发许可依据，暂不批准 | 产品介绍支持 Fintech 场景，不能代替具体商业数据许可，暂不批准 | 2026-09-30 | cloudQuant / Codex | 已复核（受限） |
| 2 | 东方财富（网页/接口） | 经 akshare 搬运代码 | https://about.eastmoney.com/home/disclaimer | 复制与批量持久化须核对权利人许可；当前无依据，暂不批准 | 未经相关权利人书面许可不得复制传播，暂不批准 | 涉及东方财富及底层交易所权利，当前无商业许可依据 | 2026-09-30 | cloudQuant / Codex | 已复核（受限） |
| 3 | 新浪财经 | 经 akshare 搬运代码 | https://finance.sina.com.cn/roll/2021-05-12/doc-ikmxzfmm2033220.shtml | 公开协议限个人非商业使用；批量落库无明确许可，暂不批准 | 未授权复制传播与第三方提供受限，暂不批准 | 须书面许可；公开版本生效于 2020-09-23，本项目无许可依据 | 2026-09-30 | cloudQuant / Codex | 已复核（受限） |
| 4 | 腾讯财经 | 经 akshare 搬运代码 | https://edu.tencent.com/agreement.html | 通用协议限个人非商业；财经接口专属持久化许可无依据 | 无财经接口下游分发许可依据，暂不批准 | 通用协议不能证明财经接口商业服务许可，暂不批准 | 2026-09-30 | cloudQuant / Codex | 已复核（受限） |
| 5 | 雪球 | 经 akshare 搬运代码（需 token） | https://www.xueqiu.com/about/faq/1/0 | token 不授予批量采集持久化权；当前无依据，暂不批准 | 无本项目数据服务分发许可依据，暂不批准 | 无本项目商业数据服务许可依据，暂不批准 | 2026-09-30 | cloudQuant / Codex | 已复核（受限） |
| 6 | 交易所官网（上交所/深交所/中金所等） | 经 akshare 搬运代码 | https://www.sse.com.cn/home/legal/ | 上交所允许非商业浏览下载；不能覆盖全部交易所与批量落库，按 §2 逐所限制 | 行情/增值传播须按具体交易所授权，当前暂不批准 | 各所条款分别适用；上交所营利使用与部分期货信息业务需授权 | 2026-09-30 | cloudQuant / Codex | 已复核（受限） |
| 7 | 国家统计局 / 中国人民银行 | 经 akshare 搬运代码 | https://www.stats.gov.cn/wzgl/202302/t20230217_1912857.html | 统计局直接发布数据可下载使用并注明来源；央行及实际中间站点须分别核对 | 统计局下载使用声明不能覆盖新浪/金十/中债的分发，暂不批准整体服务 | 政府统计与中间站点条款分别适用；整体商业服务无许可依据 | 2026-09-30 | cloudQuant / Codex | 已复核（受限） |
| 8 | Yahoo Finance（yfinance） | `opendata_providers`（迭代 1C） | https://ranaroussi.github.io/yfinance/ | yfinance 明示研究教育、Yahoo API 个人使用；本项目集中留存无适用许可依据 | 无多客户再分发许可依据，暂不批准 | 无商业数据服务许可依据，暂不批准 | 2026-09-30 | cloudQuant / Codex | 已复核（受限） |
| 9 | FRED（圣路易斯联储） | `opendata_providers`（迭代 1C） | https://fred.stlouisfed.org/legal/ | 可研究/使用 API 子集，逐系列版权与抓取限制适用；不能据此批准全部数据镜像 | 公共领域/仅需引用系列须注明来源；第三方专有内容商业分发须书面许可 | 按系列版权附条件；服务条款另有限制软件/机器学习开发用途，本项目该用途不批准 | 2026-09-30 | cloudQuant / Codex | 已复核（受限） |
| 10 | ECB / IMF / OECD | `opendata_providers`（迭代 1C） | https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html | ECB/OECD 已发布统计可附条件复用；IMF 统计数据可下载，但自动批量提取另需许可；三套条款见 §2 | ECB/OECD 附来源与数据集限制；IMF 需准确引用、说明转换及传达条件；第三方数据另核对 | ECB/OECD 可附条件商用；IMF 条款要求潜在商业复用申请许可，当前无该许可依据 | 2026-09-30 | cloudQuant / Codex | 已复核（受限） |
| 11 | 商业源（FMP/Tiingo/Alpha Vantage/Intrinio/Tradier…） | 当前迭代未启用 | https://site.financialmodelingprep.com/terms-of-service | 当前未接入；接入前逐产品登记订阅条款，现不批准落库 | 当前未接入；不批准任何转授或分发 | 当前未接入；免费计划或 API key 不授予其他供应商商业许可 | 2026-09-30 | cloudQuant / Codex | 不适用（本迭代未启用） |
| 12 | BLS（美国劳工统计局） | 自研 `opendata/data/providers/bls`；survey bulk 目录与 timeseries v1/v2 | https://www.bls.gov/developers/termsOfService.htm | 官方公共领域数据可附条件使用并注明 BLS 来源；保留访问日期与版本，不含受版权保护图片或受限微观数据 | 附来源、访问日期和下载后数据/分析免责声明；不冒用 BLS 标识，不将修改内容表示为 BLS 原始发布 | 公共领域声明未限制上述统计的商业用途；仍遵守 API 限额及内容真实性条件 | 2026-10-08 | cloudQuant / Codex | 已复核（受限） |
| 13 | FMP（EquityHistorical / EquityQuote） | 自研 `opendata/data/providers/fmp`；stable historical-price-eod/full 与 quote；仅离线原型已接线 | https://site.financialmodelingprep.com/terms-of-service | 未取得本项目产品订阅及下载/集中留存适用许可证明，暂不批准 | 多用户应用、组织内部展示及第三方服务需相应协议；本项目未取得，暂不批准 | 个人计划不得用于本项目商业服务；未取得适用商业数据协议，暂不批准 | 2026-10-08 | cloudQuant / Codex | 已复核（受限） |
| 14 | Cboe（cdn.cboe.com 公开接口） | 自研 `opendata/data/providers/cboe`；`cdn.cboe.com` 公开延迟指数 JSON，无凭据；AvailableIndices / IndexConstituents / IndexSearch / EquitySearch 四条模型描述符已接线（2026-10-09 注册面复测为三条，2026-10-11 复测为四条：cboe::EquitySearch 随 C88 落地；接线条数变化不改变本行三项结论） | https://www.cboe.com/terms | 条款要求事先取得 Cboe 书面同意方可复制或以电子检索系统存储，fair use 例外不覆盖全量留存；未取得同意，暂不批准 | 声明未经书面许可不得修改、复制或分发，暂不批准 | 材料按 AS IS 提供、明示不担保准确性与及时性且声明非交易用途；无商业数据许可依据，暂不批准 | 2026-10-08 | cloudQuant / Codex | 已复核（受限） |
| 15 | 美联储理事会（Board of Governors） | 自研 `opendata/data/providers/federal_reserve`；`www.federalreserve.gov/datadownload/Output.aspx` 公开包下载（rel=H6、rel=H15），无凭据；Board 侧 MoneyMeasures / TreasuryRates / SvenssonYieldCurve 三条已接线（2026-10-09 注册面复测为两条，2026-10-11 复测为三条：federal_reserve::SvenssonYieldCurve 随 C89 落地；接线条数变化不改变本行三项结论。发布表仍是 H.6 与 H.15 两张，Svensson 走 H.15 的 holdout 表）。同包另注册 FederalFundsRate / SOFR / OvernightBankFundingRate 三条，读的是 `markets.newyorkfed.org`，2026-10-12 注册面复测该包共 6 条能力；这三条不在本行覆盖内，逐源条款见 §2 待办第 7 项 | https://www.federalreserve.gov/disclaimer.htm | 免责声明把 Board 网站信息列为"Unless otherwise indicated, information on Board's website is in the public domain and may be copied and distributed without permission"，并要求"Please cite to the Board as the source of the information"；本行只覆盖这两张发布表下载包里的统计数值 | 可复制与分发，须注明美联储理事会为来源；同页另禁止把局徽与官方标志用于暗示背书，他方来源的版权材料须向原始权利人取得许可 | 公开领域声明未对上述信息另设商业用途限制；"unless otherwise indicated" 要求逐发布表核对是否有另示，本轮两张表未见另示 | 2026-10-09 | cloudQuant / Codex | 已复核（受限） |
| 16 | Intrinio | 自研 `opendata/data/providers/intrinio`；`api-v2.intrinio.com` 凭据直连（环境变量 `INTRINIO_API_KEY`，本轮未持有密钥、未发出请求）；CurrencyPairs / EquitySearch / HistoricalDividends / CalendarIpo / InsiderTrading 五条 ModelSpec 已接线（2026-10-11 注册面复测为四条，2026-10-12 复测为五条：intrinio::InsiderTrading 随 C99 落地；本轮无扣留项：`providers/intrinio/specs.py` 的 `NOT_DECLARABLE` 已清空，`HistoricalDividends` 由 `ModelSpec.column_derives` 承接上游逐行注入的 `symbol`；接线条数变化不改变本行三项结论） | https://about.intrinio.com/terms | 条款把数据使用限定为 "solely for your own internal purposes"；本项目未获书面 Order Form，长期留存与集中镜像的权限依据不足，暂不批准 | 第三方展示、再分发与商业用途须有已签 Order Form 并按其数据集与交付方式限定；本项目未取得，暂不批准 | 同上，无适用商业数据许可依据，暂不批准 | 2026-10-11 | cloudQuant / Codex | 已复核（受限） |
| 17 | Benzinga（Benzinga Pro API） | 自研 `opendata/data/providers/benzinga`；`api.benzinga.com/api/v2.1/calendar/ratings` 凭据直连（环境变量 `BENZINGA_API_KEY`，以查询参数 `token` 送出；本轮未持有密钥、未发出请求）；PriceTarget 一条 ModelSpec 已接线（2026-10-12 C99 注册面复测为一条） | https://www.benzinga.com/ | 本机客户端（curl 与 WebFetch）对 www.benzinga.com 返回 HTTP 403，未取得可引用的条款原文；Pro 数据接口按订阅协议授权，本项目未签订任何 Benzinga 协议，落库依据不足，暂不批准 | 同上，无书面协议依据即不得向第三方提供，暂不批准 | 同上，商业用途须由订阅协议授予，本项目未取得，暂不批准 | 2026-10-12 | cloudQuant / Codex | 待复核（本迭代已接入，条款原文未取得） |
| 18 | EconDB（econdb.com 静态名册 CSV） | 自研 `opendata/data/providers/econdb`；`www.econdb.com/static/help/main_tickers.csv` 公开静态文件，无凭据、无分页；AvailableIndicators 一条 ModelSpec 已接线（2026-10-12 C99 注册面复测为一条） | https://www.econdb.com/ | 本机客户端对 www.econdb.com 返回 HTTP 403，站内是否公开条款页未能核实；该文件是各官方统计机构序列的名册索引，数值权利属于发布机构，须逐系列向来源（BLS、ECB、SCB 等）核对，本项目未逐系列核对，暂不批准 | 名册本身不含数值，但再分发会连带指向他方序列；未取得逐系列许可与 EconDB 许可依据，暂不批准 | 同上，暂不批准 | 2026-10-12 | cloudQuant / Codex | 待复核（本迭代已接入，条款原文未取得） |
| 19 | Tiingo（api.tiingo.com） | 自研 `opendata/data/providers/tiingo`；`api.tiingo.com/tiingo/crypto/prices` 凭据直连（环境变量 `TIINGO_TOKEN`，以查询参数 `token` 送出；本轮未持有令牌、未发出请求）；CryptoHistorical 一条 ModelSpec 已接线（2026-10-12 C99 注册面复测为一条） | https://app.tiingo.com/tos | §1.6(a) 对免费与试用计划明文禁止持久化："If you use a Starter Plan or any free or paid trial plan (each, a \"Trial Plan\"), you may not write, save, archive, back up, or otherwise retain Tiingo Data in any persistent or durable storage."；付费计划存续期内 "you may persist Tiingo Data in storage solely to the extent permitted by that Paid Plan and these Terms"，降级或终止后 "you must promptly and permanently delete all Tiingo Data"；本项目未持有适用 Paid Plan，落库依据不足，暂不批准 | §7.3 "All data via the API is for internal consumption only."，且 "Redistribution is only available upon special request and permission, and comes with additional fees."，获准后仍须附 "Data sourced by Tiingo" 并链接 tiingo.com；本项目未取得该特别许可，暂不批准 | §7.3 要求代表组织或业务者签 Commercial 计划："if you are representing an organization or business, you must sign up for a Commercial plan."；本项目未签订，暂不批准 | 2026-10-12 | cloudQuant / Codex | 已复核（受限） |

> 经 akshare 接入的源数量众多（涵盖 46 个类别），上表登记**当前及计划使用的主要源**；
> 新增数据域时必须同步补登记，未登记的源不得进入 `source="auto"` 路由。

### 1.1 迭代2新增行的依据与适用范围

BLS 的 [版权声明](https://www.bls.gov/bls/linksite.htm) 将官方发布资料列为公共领域，受既有版权保护的图片与插图除外。其 [API 条款](https://www.bls.gov/developers/termsOfService.htm) 要求标注访问日期、保留下载后数据与分析不获 BLS 背书的声明、遵守调用限制，并禁止修改内容后仍表示为原始 BLS 内容。第12行据此仅覆盖本轮两个模型读取的官方统计目录和时间序列，不覆盖图像、标志或受限数据。v2 注册/Key 和实测覆盖仍未完成；公开条款复核不能代替 `SOURCE_VERIFIED`。

FMP 的 [订阅条款](https://site.financialmodelingprep.com/terms-of-service) §1.1、§2.1—2.2.2 将数据访问与具体产品/订阅范围关联，并限制个人计划、复制下载、分发及多用户展示。第13行记录当前未获适用许可的两个接口；不以 API Key 或代码实现推定许可，也不代表其余67个固定 FMP 模型已经逐产品审阅。第11行保留 C65 当时的集合登记身份和日期，其“当前未接入”是历史读数；FMP 的本轮状态以后增的第13行为准。Tiingo 已在本轮单列第19行；Alpha Vantage、Tradier 仍待专属产品登记；Intrinio 已在本轮单列第16行。

Cboe 的第14行来自本轮对 [网站使用条款](https://www.cboe.com/terms)（"Terms and Conditions for Use of Cboe Websites"）与 [全球免责声明](https://www.cboe.com/global-disclaimers/) 的公开条款审阅，逐句出处与拉取方式见 [C72 Cboe 权利审阅](evidence/C72/cboe-rights-review.md)。条款把站点材料列为受版权/商标保护，并要求复制、修改或以电子检索系统存储须先取得 Cboe 书面同意（仅保留美国版权法 fair use 例外）；材料按 "AS IS" 提供，明示不担保准确性、完整性与及时性，且声明仅供信息/教育用途、不用于交易。第14行只覆盖本轮接线并读取的两个公开延迟指数接口（`/api/global/us_indices/definitions/all_indices.json` 与 `/api/global/european_indices/constituent_quotes/{symbol}.json`）；2026-10-09 注册面复测显示 cboe 已接线三条模型描述符，其中 `IndexSearch` 复用前一条 `all_indices.json` 接口做目录内筛选，不新增被读取的接口，因此接口覆盖不变、描述符计数由 2 变 3。本行不覆盖 cboe 其余 8 个上游模型（清单实测 11 行 = 3 DEV_DONE + 8 NOT_RUN），也不推定 Cboe 指数数据的授权许可——指数数据另有独立的许可链条，接入前须另行逐产品登记。

美联储理事会的第15行来自本轮对 [Disclaimer](https://www.federalreserve.gov/disclaimer.htm) 的公开条款审阅：该页把 Board 网站信息列为 "Unless otherwise indicated, information on Board's website is in the public domain and may be copied and distributed without permission"，并要求 "Please cite to the Board as the source of the information"；同一页面另把局徽与官方标志列为受联邦法律保护、未经书面许可不得使用或复制，并把带版权的他方材料指向原始权利人（站点页脚的 [Website Linking Policies](https://www.federalreserve.gov/website-linking-policies.htm) 只谈外链与他方版权，不构成本行依据）。第15行只覆盖本轮 Board 侧模型读取的 `datadownload/Output.aspx` 发布表下载包（rel=H6 货币供应量、rel=H15 国债收益率），不覆盖 Board 站点其余出版物，也不代替对具体发布表 "otherwise indicated" 的逐表核对；2026-10-12 注册面复测该包共 6 条已注册能力，其中 MoneyMeasures / TreasuryRates / SvenssonYieldCurve 三条落在本行的两张发布表内，FederalFundsRate / SOFR / OvernightBankFundingRate 三条读 `markets.newyorkfed.org`，不在本行覆盖内（见下文纽约联储段与 §2 待办第 7 项）；模型清单实测 federal_reserve 共 13 行（2 DEV_DONE + 11 NOT_RUN），本行不覆盖其余 11 行 NOT_RUN 的上游模型；公开条款复核不改变 Board 侧三条能力的 `verified=False`，不能代替 `SOURCE_VERIFIED`。

Intrinio 的第16行依据是 [C65 逐源审阅 §11d](evidence/C65/data-rights-review.md)（该节记录 [Intrinio Terms of Service](https://about.intrinio.com/terms) 于 2026-06-03 生效，短引 "solely for your own internal purposes"，并把第三方展示/再分发/商业用途指向须签署的 Order Form）。本轮没有重新抓取该页面，只做 in-tree 复核并把接入面更新到实测数字：模型清单与能力清点各 38 行（`OBB2-intrinio-*`），2026-10-11 注册面复测 intrinio 已接线四条 ModelSpec 描述符，2026-10-12 复测为五条（CurrencyPairs、EquitySearch、HistoricalDividends、CalendarIpo、InsiderTrading）。前三条是单 GET、行读嵌套键、列改名与 `page_size` 参数别名；`CalendarIpo` 另带四个查询别名与把条目标量及其嵌套 `company`/`security` 字段摊平的一行，`InsiderTrading` 走 `{symbol}` 路径、按 `transaction_filings` 逐笔展开并把 filing 级标量复制到每笔交易行（`providers/intrinio/models/calendar_ipo.py:1-16`、`insider_trading.py:1-16`）。`providers/intrinio/specs.py` 的 `NOT_DECLARABLE` 本轮为空、无扣留项。`HistoricalDividends` 曾因上游把查询参数 `symbol` 逐行注入输出（上游 `openbb_intrinio/models/historical_dividends.py:102-104`，非本仓文件）而缺少参数到行的载体，本轮由 `ModelSpec.column_derives` 承接该载体后声明并接线（本仓落点 `providers/intrinio/specs.py:267-276`）；`census-intrinio-2of3.json` 对应行仍写 `expressible_today=false`，那是清点面的待更新读数，不改变本行三项结论。第16行不覆盖其余 33 个 intrinio 上游模型（清点 38 行 − 已接线 5 行），不代表已申请账号、购买订阅或调用过数据 API；公开条款复核不改变五条能力的 `verified=False`，不能代替 `SOURCE_VERIFIED`。

第17、18、19 行随 C99 的 benzinga/econdb/tiingo 三个包接入而补登，取法与读数全部留档可复算：2026-10-12 用浏览器 UA 的 curl 与 WebFetch 各跑一遍，`https://www.tiingo.com/tos` 返回 301 落到 `https://app.tiingo.com/tos/`（HTTP 200，正文 673,557 字节，页首写 "Tiingo ™ Terms of Use / Version 1 Effective Date: January 16th, 2022 / Last Updated Date: October 6th, 2026"），而 `https://www.benzinga.com/terms-and-conditions` 与 `https://www.econdb.com/` 均返回 HTTP 403。403 只证明该主机拒绝本机客户端，不能证明条款内容，也不足以支撑一次「已复核」，故第17、18 行状态写「待复核（本迭代已接入，条款原文未取得）」、三项用途一律按「无许可依据 → 暂不批准」处置，并把浏览器人工复核列入 §2 待办第 6 项。第19 行是对已取回的原文逐句核对的结果：§1.6(a) 禁止 Starter/试用计划做任何持久化，§1.6 付费计划段把留存限定在 "solely to the extent permitted by that Paid Plan"，并要求降级或终止后 "promptly and permanently delete all Tiingo Data"，§1.4(e) 另禁爬虫抓取站点页面；本项目未持有适用 Paid Plan、未签 Commercial 计划、未取得再分发特别许可，三项均不获准。登记行链接列必须是可解析的 http(s) URL（`measure_ac1_08` 的 `unlinked` 判据），因此第17、18 行填的是本包 `provider.py` 声明的站点根址，条款页路径与取回方式记在本段而不是塞进那一列。三条能力均 `verified=False`，不进 `source="auto"`；本轮离线接线没有释放 Benzinga 或 EconDB 或 Tiingo 的真实采集、落库或对外分发动作。

纽约联储的三个参考利率（FederalFundsRate、SOFR、OvernightBankFundingRate）读的是 `markets.newyorkfed.org`，不属于第15行的 `www.federalreserve.gov` 发布表接口，因此不落在第15行的覆盖声明内；美联储理事会与其纽约联储分行是两个发布主体，条款页也不同，逐源条款行列入 §2 待办第 7 项，本行不为其背书。

登记行链接只证明来源与审阅状态可追溯；BLS/FMP/Cboe/美联储/Intrinio 的新增能力均 `verified=False`，不加入自动选源。本轮源码与 fixture 可以离线验证，没有释放 FMP 或 Cboe 或美联储或 Intrinio 的真实采集、落库或对外分发动作。Benzinga、EconDB、Tiingo 三行同此，公开条款复核不改变第17、18、19 行能力的 `verified=False`，也不构成对本项目落库、再分发或商业使用的许可。

## 2. 待办与责任

第 6、7、10、11 行是保留既有 `rights_rows` 身份的集合登记；§1 的主链接不代替集合内各源条款。逐所和中间站点链接见 [C65 逐源审阅](evidence/C65/data-rights-review.md)。第 10 行分别依据 [ECB](https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html)、[IMF](https://www.imf.org/en/about/copyright-and-terms)、[OECD](https://www.oecd.org/en/about/terms-conditions.html)。第 11 行主链接不能当作其他供应商许可；本轮 FMP 两个离线绑定另列第13行，其余商业源接入前仍须拆分登记。

本轮已完成公开条款审阅；无需把有适用公开许可的数据一律改成签合同。缺少的只是公开条款没有授予的具体用途依据。以下后续工作不改变本轮已复核状态，也不表示全部数据可对外服务。

| # | 事项 | 责任 | 期限 | 状态 |
|---|------|------|------|------|
| 1 | **向同花顺索取书面条款副本或授权**，明确"内部使用 / 衍生数据对外服务 / 再分发 / 商业变现"四项边界 | 产品 | 迭代 1A 内 | 未开始 |
| 2 | 复核经 akshare 接入各源的条款，确认落库与再分发边界 | 开发 | 迭代 1A 内 | 未开始 |
| 3 | 引入法务视角复核（含商业授权话术与免责声明措辞） | 产品 | 迭代 1B 前 | 未开始 |
| 4 | 内嵌第三方资源（4 个站点 JS + calendar.json）权利状态核查 | 开发 | 迭代 1A 内 | 未开始（见 `THIRD_PARTY_NOTICES.md` §2） |
| 5 | 建立条款变更巡检机制（源条款页变更即触发复核） | 开发 | 迭代 1B | 未开始 |
| 6 | **用浏览器人工复核 Benzinga 与 EconDB 的条款页**：本机客户端（curl、WebFetch）对两站点返回 HTTP 403，逐句核对前第17、18 行只能记「待复核（本迭代已接入，条款原文未取得）」，三项用途一律暂不批准 | 产品 | 迭代 2 内 | 未开始（取法与 403 读数见 §1.1） |
| 7 | **为纽约联储单列逐源条款行**：`markets.newyorkfed.org` 的三个参考利率（FederalFundsRate、SOFR、OvernightBankFundingRate）不在第15行的 `www.federalreserve.gov` 发布表覆盖内，须另行核对分行站点条款 | 开发 | 迭代 2 内 | 未开始 |

## 3. 处置规则

- **未复核的源**：可落库用于内部验证，但**不得对外再分发**（即不进 `dwd_` 对外查询、不参与商业授权范围）。
- **明确禁止落库或再分发的源**：移出路由（登记 `notes="rights-blocked"`），已落库数据按结论删除或归档。
- **条款变更**：复核状态回退为"待复核"，同时评估已落库数据的处置方式。
- 本表的任何结论变化必须同步更新 `README.md` 免责声明与 `THIRD_PARTY_NOTICES.md`。
- **可追溯性（C33 起有判据）**：`docs/proposals/openbb-migration/provider-inventory.yaml` 里凡是
  注册表上已有腿的源，必须在 `rights_rows:` 写出本表 §1 的**行名原文**；行名写错一个字、或 serving 源
  干脆不写，都会红（`scripts/quality/openbb_inventory_plane.py` 的 `RIGHTS LINK` 判据，跑在 `make gate`）。
  只认 §1 登记表，不认 §2 待办表——把「向同花顺索取书面条款副本」这类待办句子当成数据源，
  这条规则就再也 falsify 不掉了。

## 4. 数据免责声明（与 README 同步维护）

opendata 本身不生产数据。所有数据均来源于第三方站点与商业 API，本项目不主张任何数据权利，
亦不对数据的准确性、完整性与时效性作出保证。消费方须自行确认其数据使用方式符合数据来源方的
条款与适用法律。本项目不构成任何投资建议。
