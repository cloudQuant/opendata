# C65 数据权利公开条款直接审阅

日期：2026-09-30（Europe/Madrid）  
责任人：cloudQuant；审阅者：Codex 主线程（AI，用户授权“你帮我直接审阅”）。  
状态：**11 行公开条款已直接复核并同步登记；附条件许可与未获许可的用途分别记录。**  
范围：`docs/data-rights-registry.md` §1 的 11 行和 `docs/proposals/openbb-migration/provider-inventory.yaml` 的 `rights_rows`。本记录不冒署人工签名、不生成供应商合同，也不自动批准对外数据服务。

## 0. 本轮审阅决定（替代原待签认状态）

用户确认拟用源包括 THS/扶摇、AkShare 等，并认为免费数据无需合同。对具有适用公开许可的源，可以用公开条款完成登记，不要求另签合同；免费访问、开源客户端或 API key 本身仍不能证明某项具体数据用途获得许可。[AKShare 特别说明](https://akshare.akfamily.xyz/special.html)与 [yfinance 文档](https://ranaroussi.github.io/yfinance/)均要求使用者自行遵守实际数据来源条件。

Codex 已复核下面的官方条款和已有源路径证据，登记为明确的条件/不批准结论，日期 2026-09-30。ECB 的已发布 ESCB 统计以及 OECD Data 可以附条件复用和商用，第三方数据、数据集特别条件与来源要求仍适用。[ECB 复用政策](https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html)、[OECD 条款](https://www.oecd.org/en/about/terms-conditions.html)。IMF 统计数据特殊条款允许下载和分发，但同页还限制未经许可的自动批量下载，并要求潜在商业复用申请许可；不能只摘取“free reuse”作为全量商业抓取许可。[IMF 条款](https://www.imf.org/en/about/copyright-and-terms)。FRED 须逐系列核对版权、来源与服务用途限制，不能把公共领域的某个系列推及所有第三方系列。[FRED 条款](https://fred.stlouisfed.org/legal/)。

THS/扶摇官方产品介绍明确面向研究、回测和 Fintech，且提供全市场 Parquet 导出；本次没有找到覆盖本项目长期留存与多客户再分发的专门许可。因此登记为“公开证据未授予该具体用途，暂不批准”，不把通用同花顺协议强行当作扶摇专属合同。[扶摇介绍](https://fuyao.aicubes.cn/docs/introduction/)。国内网页来源和 Yahoo 亦未获得本项目整体再分发/商业服务许可。本轮没有新读取生产流量、合同、账单或供应商账号，也没有删除已有数据或改动生产路由。

验收边界：完成权利登记与审阅可以支撑“登记表完整”条目；它不支撑“所有源获准所有用途”、生产权利控制已经实施或所有其他验收项通过。33 条能力请求方确认也只确认用途与负责人，不是许可。

以下 §1–§4 保留最初材料编写时的证据边界。其“建议提交/尚缺”指未获覆盖的用途依据，不再表示缺少审阅者；FRED 原“未对照”状态为材料编写快照，后续技术 JSON/CSV 对照通过也不改变这里的数据权利边界。

## 1. 复核边界与现状

- 登记表明确本项目形态为集中获取、落库，再通过 REST/WS 面向多消费方提供数据；软件许可与金融数据权利是两件事。MIT 等源码许可证不授予网站或数据提供方的数据权利。
- 本次只读源码、provider inventory 和官方网页。对源码的判断是“仓库中存在该路径”，不代表本轮或生产环境实际请求过该源；没有读取生产流量、供应商账号或合同。
- inventory 将 `ths` 标成“已对照转正”，将 `akshare` 标成“已实现未对照”，并写明 akshare 10 条能力全部 `verified=false`、`source="auto"` 会跳过它们，因此国内 auto 路由只有 THS。代码路径存在和 inventory 的状态都不能证明获得授权或证明生产使用。见 `provider-inventory.yaml:331-350`。
- `rights_rows` 中 ECB/FRED/IMF/OECD/Yahoo 等链接的是数据来源条款或源名；inventory 中的 provider 状态仅描述登记过的能力。状态“已对照转正”不等于数据权利批准，也不覆盖未实现的上游 fetcher。商业 provider 行均标“待实现”，未接入不代表将来可用。
- 除能确认的明文条款外，以下均记作“本次未找到/未取得授权依据”，不将网页可访问、公开数据、技术 API key、数据接口文档或其他公司的授权名单解释为本项目的许可证。

### 使用状态与证据边界

| 登记行 | 仓库/inventory 能支持的状态 | 实际生产调用证据 |
|---|---|---|
| #1 THS | 本地 provider 已注册且标“已对照转正”，scenario 记为主源；具备参与 `source="auto"` 的条件（`provider-inventory.yaml:331-340`）。 | 未读取生产日志或供应商账单；是否在生产实际调用、调用数据集与频率均未知。 |
| #2–#7 国内 HTTP 源 | 有源码路径；`akshare` 标“已实现未对照”，10 条注册能力均 `verified=false`，不会进入 `source="auto"`（`provider-inventory.yaml:341-350`）。 | 生产调用未知；显式指定源的本地调用不能由 inventory 排除，也无本次运行证据。 |
| #8 Yahoo/yfinance | 标“已对照转正”仅覆盖登记子集；inventory 注明上游 29 个 fetcher，本仓只注册一条海外日线能力（`provider-inventory.yaml:70-79` 及文件头注释）。 | 生产调用未知；不能据状态推断 29 个 fetcher 全部实现或被调用。 |
| #9 FRED | 标“已实现未对照”；inventory 列上游 fetcher 数和模型名，不等于已逐系列审阅（`provider-inventory.yaml:40-49`）。 | 生产调用及实际 series ID 未核对。 |
| #10 ECB/IMF/OECD | 各 provider 标“已对照转正”，但仅描述已注册能力；inventory 的 fetcher 数分别是 3/8/9（`provider-inventory.yaml:29-69`）。 | 生产调用、实际数据集和第三方数据占比未核对。 |
| #11 商业 provider | inventory 明列的 Alpha Vantage、FMP、Intrinio、Tiingo、Tradier 均为“待实现”；相应条目没有 `rights_rows`（`provider-inventory.yaml:80-88, 134-151, 170-178, 305-313`）。 | 按 inventory 状态未接入当前 provider；无生产调用证据。省略号中的其他名称须逐源登记。 |

表中的“生产调用未知”是本审阅边界，不表示源未被使用。要把状态改为“已实际使用”或“未使用”，需另查脱敏调用日志、部署配置和相应订单/账单；本文件没有访问这些材料。

## 2. 逐源审阅

访问日期均为 **2026-09-30**。每条引文为官方页面短引；对未找到可引用授权条款的来源，会明确标出。条款适用范围仍需以具体产品、数据集、签约主体、账户计划和合同为准。

### 登记表 #1：同花顺扶摇 API

- **代码/状态证据：** inventory 的 `local_providers.ths` 标为“已对照转正”、rights row 为“同花顺扶摇 API”（`provider-inventory.yaml:331-339`）。扶摇 API 快速开始说明以 X-api-key 调用；这是鉴权说明，不是数据许可。[接口介绍](https://fuyao.aicubes.cn/docs/introduction/)短引：“面向 AI Agent、量化研究与 Fintech 应用”。
- **实际审阅条款：** [同花顺金融信息服务使用许可协议](https://news.10jqka.com.cn/clientinfo/protocol.html)短引：“自用的、非商业性使用”。该通用产品协议还说收费类产品/信息须遵守专门协议；它不能被当成扶摇 API 的数据合同。[扶摇 API 文档入口](https://fuyao.aicubes.cn/docs/api-reference/overview/)和介绍已读取，但本次未找到其中授予本项目数据库持久化、备份、REST/WS 下游再分发或商业软件服务的专门条款。
- **目前能证明：** 有官方 API 服务与 key 鉴权；有通用同花顺产品许可。
- **尚不能证明：** API 响应原始字段/衍生字段落库和备份期限、客户通过本项目 REST/WS 访问、商业软件授权内的数据服务边界、面向多客户/公开用户分发、终止服务后数据删除要求。
- **建议提交的凭证：** 扶摇 API 适用的已签订单/合同或官方书面许可，逐项涵盖数据集、字段、留存/备份、衍生结果、客户/API/WS 展示与分发、商业使用、地域、授权期限和终止后的处置。

### 登记表 #2：东方财富（网页/接口）

- **代码/状态证据：** 示例接口见 [fund_etf_em.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/fund/fund_etf_em.py:20)（第 28 行请求 `88.push2.eastmoney.com`）；源文件表明仓库有该路径。`akshare` 当前未对照、不会进 auto，不能据此断言生产已用。
- **实际审阅条款：** [法律声明](https://about.eastmoney.com/home/disclaimer)短引：“未经东方财富或相关权利人事先书面许可”。该页禁止对站点内容进行复制、传播等；特别声明还指出行情信息涉及交易所权利。[服务协议](https://about.eastmoney.com/home/protocol)提醒用户未经交易所书面同意，不得复制或向机构/个人提供行情数据。
- **授权边界：** 本次未发现允许 opendata 批量落库、对外 REST/WS 或商业展示的许可。东方财富站点内容权利与底层交易所数据权利应分别核验。
- **建议：** 若仍保留该路由，取得东方财富及相应底层数据权利人对采集、保存、备份、派生与客户交付方式的书面许可；否则维持非 auto，并按登记表 §3 的限制处理。

### 登记表 #3：新浪财经

- **代码/状态证据：** [stock_zh_a_sina.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/stock/stock_zh_a_sina.py:361) 请求新浪行情接口；[macro_china.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/economic/macro_china.py:3540) 还请求新浪的央行统计展示接口。静态实现不代表本轮生产使用。
- **实际审阅条款：** [新浪财经用户协议](https://finance.sina.com.cn/roll/2021-05-12/doc-ikmxzfmm2033220.shtml)短引：“未经新浪公司书面许可”。页面标注本版本发布日期/生效日期为 2020-09-23（网页发布时间 2021-05-12），因此其是否仍为当前适用版本待确认。§6.1 限制复制/读取/采用服务内容用于商业用途及在来源页面外展示；§6.2 还要求遵守书面许可范围，并限制销售、商业使用及向第三方提供。
- **授权边界：** 未找到覆盖本项目数据库持久化、商业 REST/WS、客户/第三方交付的许可。新浪上展示的底层交易所或政府统计数据仍有独立来源权利，新浪条款不能替代底层授权。
- **建议：** 在任何外部服务前取得新浪对明确数据集、采集方式、持久化/缓存、再分发渠道和商业场景的书面许可，并单独核验底层源。

### 登记表 #4：腾讯财经

- **代码/状态证据：** [stock_zh_a_tx.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/stock/stock_zh_a_tx.py:15) 使用 `proxy.finance.qq.com` 请求股票行情。
- **实际审阅条款：** [腾讯服务协议](https://edu.tencent.com/agreement.html)是腾讯通用协议，短引：“本服务仅为您个人非商业性质的使用”。它提示具体产品可有单独协议；本次未找到适用于上述 Finance.qq 接口的产品专属数据许可。
- **授权边界：** 通用协议不能确定是否适用于该金融接口，也未证明自动化采集、数据库落库、REST/WS 再分发或商业使用被授权。
- **建议：** 获取腾讯财经/实际数据权利方针对接口和本项目用途的现行条款或书面许可；核实后再决定路由状态。

### 登记表 #5：雪球

- **代码/状态证据：** [stock_xq.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/stock/stock_xq.py:32) 使用雪球行情接口；第 53-62 行可选地传入 `xq_a_token` 并请求股票 quote。代码接收 token 不代表有权自动化提取或转售数据。
- **实际审阅条款：** [雪球服务协议](https://www.xueqiu.com/about/faq/1/0)（2024-07-26 生效）短引：“股票、基金等行情信息”。该协议说明服务包括行情，但本次读到的通用协议未发现授予本项目采集、数据库使用或对外 API 分发的许可。
- **授权边界：** 未证明登录/token 可用于服务端自动抓取、持续存储、商业用途或第三方/客户分发；也不据此断言条款明确禁止全部这些用途。
- **建议：** 取得雪球对自动化访问及数据服务用途的明确书面许可；若无，维持按需/非对外状态。

### 登记表 #6：交易所官网（拆为可审阅子源）

inventory 将该行合并为“交易所官网”，但源码覆盖多家证券和期货交易所。以下是本次静态检索确认到的交易所路径；未列出的交易所/数据域不能由这份审阅推定为已复核。

#### 6a. 上海证券交易所（SSE）

- **代码证据：** [fund_etf_sse.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/fund/fund_etf_sse.py:15) 请求 SSE ETF 规模接口；[stock_summary.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/stock/stock_summary.py:208) 还有 SSE 统计接口。
- **官方条款：** [SSE 法律声明](https://www.sse.com.cn/home/legal/)短引：“基于非商业目的浏览、下载”。页面限定了可非商业浏览/下载，并说明以出售牟利为目的的复制、下载、存储、抓取、发送、转载等需书面许可。[业务办理页](https://www.sse.com.cn/transparency/services/index.shtml)列出 Level-1/Level-2 行情等申请产品。
- **尚缺：** 公开浏览下载不构成本项目批量落库、对外 REST/WS 或商业使用许可；本项目没有本次可审的 SSE 授权合同。

#### 6b. 深圳证券交易所（SZSE）

- **代码证据：** [fund_scale_szse.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/fund/fund_scale_szse.py:29) 请求 SZSE 基金规模报表；[stock_summary.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/stock/stock_summary.py:31) 也有 SZSE 接口。
- **官方条款：** [用户服务协议](https://www.szse.cn/application/register/protocol/)短引：“未经运营方和相关权利人许可”。第 6 条列出复制、转载、抓取等受限制行为；站点条款和数据内容的底层权利可能属于不同权利人。
- **尚缺：** 没有本项目落库、衍生/长期缓存、向客户 REST/WS 交付或商业用途的书面许可。

#### 6c. 北京证券交易所（BSE）/全国股转系统

- **代码证据：** [stock_info.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/stock/stock_info.py:196) 请求 BSE 上市公司/证券列表；[stock_share_hold.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/stock/stock_share_hold.py:254) 请求董监高持股变动。
- **官方许可规则：** [境内行情授权指南](https://www.bse.cn/application/guide.html)（页面涵盖 BSE 和全国股转系统）短引：“未经授权不得使用”。[许可单位公示](https://www.bse.cn/application/Licensing_unit.html)列有获得特定产品/终端查询许可的单位；他方的特定许可不转授给本项目。
- **尚缺：** 本项目未提交/未发现行情使用许可申请或许可合同。BSE 当前路由的参考信息、持股变动和行情产品边界需按实际数据集分别确认。

#### 6d. 中国金融期货交易所（CFFEX）

- **代码证据：** [futures_settle.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/futures/futures_settle.py:137) 请求结算参数；[cot.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/futures/cot.py:720) 实现 CFFEX 会员持仓排名。
- **官方规则：** [中国金融期货交易所交易规则](https://www.cffex.com.cn/u/cms/www/202003/27155731wtng.pdf)短引：“交易所管理和发布信息，有权收取相应费用”。该句说明交易所的信息管理/收费权，不是对本项目的再分发许可。
- **尚缺：** 适用于上述结算/排名产品的许可、储存期限和下游用户授权范围。

#### 6e. 上海期货交易所（SHFE）

- **代码证据：** [futures_settle.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/futures/futures_settle.py:271) 请求 SHFE 结算参数；[cot.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/futures/cot.py:277) 实现会员排名。
- **官方规则：** [SHFE 交易规则（2026）](https://www.shfe.cn/regulation/exchangerules/rules/202606/t20260603_831934.html)短引：“未经交易所同意，任何单位和个人不得发布期货交易行情”。[信息管理办法（2025）](https://www.shfe.cn/regulation/exchangerules/otherrules/202508/t20250807_828526.html)进一步规定交易所信息业务、传播及增值开发需书面授权并遵守协议。
- **尚缺：** 本项目未提供 SHFE 信息服务协议/授权；排名、结算与行情产品是否同一许可类型要由授权文件具体说明。

#### 6f. 上海国际能源交易中心（INE）

- **代码证据：** [futures_settle.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/futures/futures_settle.py:311) 请求 INE 结算参数；[futures_daily_bar.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/futures/futures_daily_bar.py:279) 请求 INE 日频数据；[futures_contract_info_ine.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/futures_derivative/futures_contract_info_ine.py:15) 请求交易参数。
- **官方信息服务页面：** [“我是信息商”](https://www.ine.cn/specialtopic/marketquotients/)列出“行情展示应用申请指引”“行情转发商名单”。这是申请/名单入口，不是对本项目的授权。具体许可协议本次未找到。
- **尚缺：** 本项目是否属于授权信息商、可收取哪些数据、可保存/转发给哪些用户及期限均未证明。

#### 6g. 大连商品交易所（DCE）

- **代码证据：** [cot.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/futures/cot.py:517) 请求 DCE 会员成交/持仓排名所需合约列表。
- **官方页面：** [DCE 对外门户](https://extportal.dce.com.cn/file/pc/index.html)介绍“数据服务”，但本次访问未找到具体行情/结算数据的再分发条款或向本项目授予权利的协议。
- **尚缺：** 适用的数据许可条款、存储和外部分发权利、许可主体与产品范围。不能把 DCE 接口公开可访问解释为授权。

#### 6h. 郑州商品交易所（CZCE）

- **代码证据：** [futures_settle.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/futures/futures_settle.py:182) 请求 CZCE 结算参数；[cot.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/futures/cot.py:367) 含 CZCE 数据解析。
- **官方规则：** [CZCE 期货交易管理办法（官方 PDF）](https://www.czce.com.cn/cn/uploadfile/2022/08/31/20220831170528656.pdf)短引：“未经交易所授权许可”。该 PDF 版本日期较早，不能单独确认 2026-09-30 的当前适用版本；本次打开[官方 2026 规则 PDF](https://www.czce.com.cn/cn/content_file/flfg/zcjywgz/zcjjygz/2026/3/68c0dce01f984475be768ed93afd9b4f.pdf)超时，未引用其正文。
- **尚缺：** 当前有效的规则/信息经营许可协议和本项目授权范围；旧版条款只作为风险提示，不作为当前签认依据。

#### 6i. 广州期货交易所（GFEX）

- **代码证据：** [futures_settle.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/futures/futures_settle.py:221) 请求 GFEX 结算参数；[cot.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/futures/cot.py:164) 包含 GFEX 排名源。
- **官方许可指引：** [行情授权申请指引](https://www.gfex.com.cn/gfex/jbhqzlxz/202212/7743c29d05a241e28d0220fc2cd08ffe/files/a2fdd96a79e845ec9716790f3a19f85a.pdf)短引：“通过审批后双方签署行情经营许可协议”。指引要求许可用途和传播范围按协议执行；本项目没有提供已签协议。
- **尚缺：** 申请、审批、数据集/产品范围、许可费及允许传播单位等材料。

### 登记表 #7：国家统计局与中国人民银行（拆分）

#### 7a. 国家统计局（NBS）

- **代码证据：** [macro_china_nbs.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/economic/macro_china_nbs.py:27) 直接访问 `data.stats.gov.cn`；[macro_china.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/economic/macro_china.py:326) 也有国家统计局失业率路径。
- **官方条款：** [国家统计局服务条款](https://www.stats.gov.cn/wzgl/202302/t20230217_1912857.html)短引：“下载和使用国家统计局发布的统计数据”。该条款允许下载/使用公开统计数据并要求引用来源；对具体数据集的长期数据库持久化、以本项目 REST/WS 面向客户再分发、商业软件服务，以及国家数据发布库中其他部门/第三方数据的权利范围，本次页面没有逐项说明。
- **建议：** 可考虑按官方直接发布、非第三方权利的数据系列建立可审阅候选集；逐系列记录来源、引用、字段、抓取频率、保存/备份和 API 输出形态，再决定是否纳入服务范围。不要把“下载和使用”扩大解释成无条件的商业数据再分发许可。

#### 7b. 中国人民银行（PBoC）及显示为央行统计的数据

- **代码/来源层证据：** 本次在 `opendata_http` 中未找到直连 `pbc.gov.cn` 的请求；PBoC 官方网站有[统计数据栏目](https://www.pbc.gov.cn/diaochatongjisi/116219/116319/index.html)，但本次未找到该栏目对第三方数据库/API 再分发的明确许可。源码中若干可能与央行统计相关的路径，实际上请求的是中间服务：Sina “央行货币当局资产负债”见 [macro_china.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/economic/macro_china.py:3540)，金十“中国央行决议报告”见 [macro_bank.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/economic/macro_bank.py:137)，中债收益率见 [bond_china.py](/Users/yunjinqi/Documents/new_projects/opendata/opendata_http/bond/bond_china.py:144)。其中 Sina 已单独列在登记表 #3；金十和中债采集服务没有列入现有 `rights_rows`。
- **金十官方条款：** [金十数据用户协议](https://flash.jin10.com/agreement)短引：“未经公司书面授权”。其第 2.4、2.6、6 条限制未授权访问、信息内容复制/抓取/存储、商业使用及向第三方提供；存在明确的服务源约束。
- **中债官方说明：** [中债收益率曲线法律说明](https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/more?locale=cn_ZH)短引：“需向中债估值中心提出书面申请”。页面针对中债信息产品开发/指数和衍生产品写明申请要求。代码注释中的 `/pbc/` 路径属于 `chinabond.com.cn`，不能据此认定直接连到 PBoC。
- **授权边界：** 要分开处理“原始统计数据发布方（如 PBoC）”和“实际服务/采集站点（Sina、Jin10、中债）”。公开显示一组数据不证明允许通过该中间方持续采集并落入本项目数据库，也不证明下游商业 REST/WS 权利。Jin10 相关数据应受其服务协议约束；中债需要针对具体曲线/产品和拟用方式核验许可。
- **建议：** 由负责人确认哪些字段确属 PBoC 发布、哪些取自 Jin10/中债/Sina；把继续保留的采集方与官方源分别加入 source mapping/rights review 流程。未取得具体服务条款或许可前不对外提供这些第三方路径。

### 登记表 #8：Yahoo Finance（`yfinance`）

- **代码/状态证据：** `provider-inventory.yaml` 将 `yfinance` 标记为“已对照转正”，但注释说明该状态仅覆盖已注册能力；上游 29 fetchers 不代表全部已实现。inventory 的已注册范围为海外股票日线。
- **实际审阅条款：** [Yahoo Terms of Service（美国站点）](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html?ncid=mbr_idnedulnk00000001)短引：“you may not access or reuse the Services for any commercial purpose”，并限制以 Yahoo 数据创建竞争数据库/数据 feed。[Yahoo Developer API Terms](https://legal.yahoo.com/us/en/yahoo/terms/product-atos/apiforydn/index.html)也已查看；它适用于 Yahoo Developer Network API，但当前 inventory 登记的是 `yfinance`，未证明这条路径使用了该 YDN API。
- **授权边界：** 通用 TOS 未授予本项目商业数据服务权；yfinance 是客户端/库名，不是 Yahoo 对项目的许可。自动化抓取、长期存储、数据库/行情 feed 和第三方 REST/WS 输出都未获证明。
- **建议：** 确认实际端点、源站条款及数据系列权属；若需商业或多用户服务，取得 Yahoo/数据提供方许可。registry 当前引用的 YDN API 条款不能直接视为 yfinance 抓取许可。

### 登记表 #9：FRED

- **代码/状态证据：** inventory 将 FRED 标为“已实现未对照”、36 个上游 fetchers；该数量不是当前实现范围。已登记具体系列及其来源/版权状态未在本材料中枚举。
- **实际审阅条款：** [FRED Services Terms](https://fred.stlouisfed.org/legal/)短引：“For any other use, you must obtain permission from the copyright holder”。[FRED API Terms](https://fred.stlouisfed.org/docs/api/terms_of_use.html)说明数据系列可能由第三方拥有，API 本身不覆盖其版权限制；API 应用还须展示条款链接并绑定用户条款。
- **授权边界：** FRED 全站服务条款限制将其内容持久化、缓存、归档或纳入其他数据库/向第三方提供；少数标为 Public Domain/Citation requested 或 Copyrighted/Citation required 的系列有不同条件，某些内部商业使用/报告显示可带来源引用。任何一类都不自动授权整个 FRED 数据库进入本项目的外部 REST/WS。第三方系列还需原始数据权利人许可。
- **建议：** 逐系列列出 FRED series ID、版权标签、原始来源、所用 API、拟存储/缓存期限、删除/回收规则和外部交付内容，再取得适用许可或剔除受限系列。

### 登记表 #10：ECB / IMF / OECD（拆为三项）

#### 10a. 欧洲中央银行（ECB）

- **代码/状态证据：** inventory `ecb` 标“已对照转正”，标注 3 个上游 fetchers；只描述注册子集。
- **官方条款：** [ESCB 统计数据再使用政策](https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html)短引：“All publicly available ESCB statistics may be reused free of charge”。需引用来源，保持统计数据（含 metadata）不被修改；第三方数据须先获原始权利人许可，且不包含机密数据。
- **授权边界与建议：** 这是三项中最清晰的公开统计再使用许可候选，但仍须给出 exact dataset、来源/第三方标志、格式是否改动和引用位置；未登记第三方限制项不得自动继承此许可。按 ECB 数据集清单逐项勾选后可由负责人决定是否同意相应使用范围。

#### 10b. 国际货币基金组织（IMF）

- **代码/状态证据：** inventory `imf` 标“已对照转正”，标注 8 个上游 fetchers；只描述注册子集。
- **官方条款：** [IMF Copyright and Usage](https://www.imf.org/en/about/copyright-and-terms)短引：“The IMF prohibits the bulk download of information by automated technology without explicit permission”。页面对特定由 IMF 生产/整理并明确以 IMF 为来源的公开统计数据另设再使用条款，允许满足条件的下载、复制、衍生、发布和分发并要求准确归因；包含第三方信息的数据集受额外条款约束。页面同时要求潜在商业再使用联系 IMF 申请许可。
- **授权边界与建议：** 不能用一条一般数据复用条款证明当前批量自动采集、落库或商业 REST/WS 已授权。需要核实每个 API/数据门户、数据集是否为“IMF-produced/curated Data”、自动下载是否获 explicit permission、第三方组成和归因模板。

#### 10c. 经济合作与发展组织（OECD）

- **代码/状态证据：** inventory `oecd` 标“已对照转正”，标注 9 个上游 fetchers；只描述注册子集。
- **官方条款：** [OECD Terms & Conditions](https://www.oecd.org/en/about/terms-conditions.html)短引：“for any purpose, even for commercial use”。在额外限制未适用时，官方条款允许下载、复制、改编、分发、分享/嵌入数据并要求适当归因；第三方数据及数据集附加限制例外。
- **授权边界与建议：** 这是可支持商业使用的条件许可，不是全站所有数据的无条件授权。需导出实际 dataset 的 metadata/source tab，核实第三方来源与补充限制，并把 OECD 的指定引用文本保留到服务输出/文档中。

### 登记表 #11：商业源（逐项拆分；当前均未实现）

inventory 中以下 provider 均为“待实现”，没有对应的 `rights_rows`，不属于已实现数据路径。条款只用于未来准入评估，不代表已有 key、订阅或授权。

#### 11a. Alpha Vantage

- **官方条款：** [Terms of Service](https://www.alphavantage.co/terms_of_service/)短引：“If you are interested in using the Alpha Vantage Platform for commercial purposes, please contact us”。
- **边界/建议：** 商业用途须另行联系；尚无写明数据库保存、第三方 REST/WS 分发或商业产品访问的许可。保持“待实现”；未来启用前获取覆盖实际场景的合同/书面许可。

#### 11b. Financial Modeling Prep（FMP）

- **官方条款：** [FMP Terms of Service](https://site.financialmodelingprep.com/terms-of-service)短引：“Without the prior written approval of FMP, the Customer may not distribute, publicly perform or display”。Terms 标示更新时间 2023-08-01；[Pricing / licensing page](https://site.financialmodelingprep.com/developer/docs/pricing)另明确显示/再分发需要 Data Display and Licensing Agreement。
- **边界/建议：** 个人计划只供个人非商业使用；多用户/内部组织或外部平台数据展示也需特定协议。账号/订阅不自动包含数据展示、缓存或分发权。当前未实现，继续维持待实现，若决策启用应先签明确范围的许可。

#### 11c. Tiingo

- **官方条款：** [Tiingo Terms of Use](https://app.tiingo.com/tos/)短引：“Redistribution is only available upon special request and permission”。Terms 说明 API 数据供内部消费；商业账户不等同于再分发授权，再分发需单独许可及费用。
- **边界/建议：** 当前未实现、无 `rights_rows`。未来如作为上游需明确 internal vs customer data delivery、用户是否自带 token、保留时间与许可费用；否则不路由。

#### 11d. Intrinio

- **官方条款：** [Intrinio Terms of Service](https://about.intrinio.com/terms)（2026-06-03 生效）短引：“solely for your own internal purposes”。第三方展示/再分发/商业用途需要明确的已签 Order Form，并按其列出的数据集和交付方式限定。
- **边界/建议：** 当前未实现、无 `rights_rows`。未来订阅之前，须让 Order Form 覆盖数据落库、REST/WS API、商业受众、衍生结果和期限。

#### 11e. Tradier

- **官方条款：** [Tradier API FAQ](https://docs.tradier.com/docs/faq)短引：“Tradier APIs are entitled for personal use only”。页面说明应用分发需要 Tradier Partner 身份；FAQ 不等于本项目 partner 合同。
- **边界/建议：** 当前未实现、无 `rights_rows`。未来须取得合作方协议/书面确认，列明可用 market data、储存/缓存和客户接口范围。

#### 11f. 省略号中的其他商业源

登记表 #11 的“…”不是授权名单。本材料只审阅 provider inventory 中明确列出的五项；其他已列为待实现的 provider（例如 BLS、Nasdaq、SEC、Cboe、EIA、Seeking Alpha 等）没有因本次审阅而获权利核准。应在决定实现时逐个登记实际法律实体、数据集、官方条款、账户计划和输出场景，不能继承相邻 provider 的许可。

## 3. 建议决策与执行边界

下列建议沿用 `docs/data-rights-registry.md` §3 既有处置规则，不修改验收判据：

1. **持久化与对外分发分开决策。** 每个 source×dataset 单独确认原始落库、备份/缓存/保留期限、可派生处理、DWD 范围、REST/WS 用户、客户可下载/转存能力、商业软件许可边界和终止授权后的删除义务。一个“允许使用”复选框不足以覆盖这些用途。
2. **无明确许可时按既有 §3 处理。** 未复核数据不得进入对外 DWD/客户 API 或商业授权范围；对于条款已限制缓存/抓取/商业用途的源，需再判断是否连内部持续落库/自动采集都允许。不要用源代码许可证、API key、服务可访问性、公开页面或其他公司的授权名单替代本项目授权。
3. **条件公开数据单独列候选。** NBS、ECB、部分 OECD、部分 IMF 数据页面有公开下载/引用或数据再使用条款，但须先形成 exact dataset allowlist，逐项标明数据生产者、引用、第三方限制、转换方式与对外交付。IMF 自动批量获取和商业再使用需特别核实；FRED 的各 series 权利不可按整体推定。
4. **修复登记清单的粒度/溯源。** 原 #6 至少按 SSE/SZSE/BSE/CFFEX/SHFE/INE/DCE/CZCE/GFEX 拆审；原 #7 分开 NBS、PBoC 直接发布与 Sina/Jin10/ChinaBond 中间服务；原 #10 分拆 ECB/IMF/OECD；原 #11 将五个命名商业 provider 与“其他待登记源”分开。上述建议留待主线程另行维护登记表，本文件不改写原表。
5. **明确使用状态。** 每项最终标注“已获本项目许可（附凭证）/仅允许列明的公共数据范围/仅内部验证/暂停采集与落库/未实现、保持禁用/需供应商书面许可”，不把“条件不明”记为授权通过。

## 4. cloudQuant 待签认栏

以下栏位目前全部待填写。负责人名称来自用户输入，不代表已完成审查或签字；没有代签。

### 审阅人确认

- 审阅人：cloudQuant
- 公开身份/联系入口：用户提供 `https://yunjinqi.top`、`https://github.com/cloudQuant`；本文件未独立核验账号归属，也未向其发送消息。
- 身份/组织：____________________
- 签认日期：____________________
- 对本文源名拆分与“代码存在≠已使用/已授权”界定：□同意　□修改后同意　□不同意
- 对“明确公开数据条款须按数据集/第三方来源具体适用，不得整体继承”的处理：□同意　□修改后同意　□不同意
- 附件/凭证编号（合同、授权邮件、数据集许可、官方来源标记）：____________________

### 每个 source×dataset 使用范围决定（复制此块逐项填写）

- 原登记行 / 子源：____________________
- provider、数据集/series、字段/端点、底层来源：____________________
- 授权证据链接/合同条款编号/签约主体：____________________
- 本项目是否可自动采集：□是　□否　□需另行许可
- 是否可持续落库/缓存及期限：□是，期限________　□否　□待书面确认
- 是否可备份及恢复后继续使用：□是，期限________　□否　□待书面确认
- 是否可生成哪些衍生字段/汇总：____________________
- 是否可进入 DWD 与 REST/WS：□内部　□指定客户　□公开　□均不可；具体范围________
- 商业软件/付费用户范围；原始数据或衍生输出是否可下载/重发：____________________
- 必须展示的来源、引用、版权/免责声明：____________________
- 授权终止后的停采、停服、删除/归档规则：____________________
- 决定：□批准上述限定范围　□仅内部验证　□先取得书面许可　□暂停/移出　□补充审阅
- 签名/签认凭证与日期：____________________

**签认结果记录：待填。** 在签认人与证据补齐之前，不将本材料视为已批准的数据权利结论，也不据此改写 `docs/data-rights-registry.md`、`README.md`、`THIRD_PARTY_NOTICES.md` 或验收台账。
