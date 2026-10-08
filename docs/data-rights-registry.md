# 数据源权利登记表

> 版本：v1.3（迭代2新增 BLS/FMP 专属登记；登记完整性不等于所有用途获准）
> 日期：2026-10-08（C65 既有行复核日期仍为 2026-09-30；首版 2026-09-22）
> 依据：迭代1 决策 D11（数据权利与再分发）、设计 §1.3
> 维护：数据源变更、条款变更、复核结论变化时必须更新本表

## 0. 为什么单独登记

**代码许可证 ≠ 数据权利**。opendata 的模式是"集中获取 → 落库 → 经 REST/WS 向多消费方再分发"，
并允许商业授权——这已构成数据分发/经营行为，与 akshare"工具中立、用户自担"的定位有本质区别。
因此每个数据源都必须明确：条款是否允许本项目落库、是否允许向第三方再分发、是否允许商业使用。

**定位声明**：opendata 的商业授权话术定位为"**软件使用许可**"，不含数据销售、不主张数据权利。

## 1. 登记表

状态取值：`已复核` / `已复核（受限）` / `不适用`。用户授权 Codex 主线程直接审阅，责任人 cloudQuant；这不是供应商授权，也不是冒署 cloudQuant 人工签名。既有行依据和限制见 [C65 审阅记录](evidence/C65/data-rights-review.md)；新增行依据见下文 §1.1。本次没有申请账号、购买订阅或调用数据 API。

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

> 经 akshare 接入的源数量众多（涵盖 46 个类别），上表登记**当前及计划使用的主要源**；
> 新增数据域时必须同步补登记，未登记的源不得进入 `source="auto"` 路由。

### 1.1 迭代2新增行的依据与适用范围

BLS 的 [版权声明](https://www.bls.gov/bls/linksite.htm) 将官方发布资料列为公共领域，受既有版权保护的图片与插图除外。其 [API 条款](https://www.bls.gov/developers/termsOfService.htm) 要求标注访问日期、保留下载后数据与分析不获 BLS 背书的声明、遵守调用限制，并禁止修改内容后仍表示为原始 BLS 内容。第12行据此仅覆盖本轮两个模型读取的官方统计目录和时间序列，不覆盖图像、标志或受限数据。v2 注册/Key 和实测覆盖仍未完成；公开条款复核不能代替 `SOURCE_VERIFIED`。

FMP 的 [订阅条款](https://site.financialmodelingprep.com/terms-of-service) §1.1、§2.1—2.2.2 将数据访问与具体产品/订阅范围关联，并限制个人计划、复制下载、分发及多用户展示。第13行记录当前未获适用许可的两个接口；不以 API Key 或代码实现推定许可，也不代表其余67个固定 FMP 模型已经逐产品审阅。第11行保留 C65 当时的集合登记身份和日期，其“当前未接入”是历史读数；FMP 的本轮状态以后增的第13行为准。Tiingo、Alpha Vantage、Intrinio、Tradier 仍待专属产品登记。

登记行链接只证明来源与审阅状态可追溯；BLS/FMP 新增能力均 `verified=False`，不加入自动选源。本轮源码与 fixture 可以离线验证，没有释放 FMP 的真实采集、落库或对外分发动作。

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
