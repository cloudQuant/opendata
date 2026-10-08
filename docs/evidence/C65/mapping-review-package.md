# C65 §4|02 口径映射审阅与未解决项

日期：2026-09-30（Europe/Madrid）  
状态：**用户委托的当前 AI 审阅已完成；全量字段映射验收仍 INCOMPLETE。**

当前33腿结论见 §9 与 `mapping-review-results.json`。下文 §1–8 为前期事实材料，保留原始样本及历史待签认提示；不再以等待人工审阅作为缺口理由。审阅人是 Codex 主智能体（AI），不是 cloudQuant 个人签名。  
范围：`opendata/data/mappings/akshare.yaml`、`opendata/data/mappings/ths.yaml` 所列 source/domain 映射，优先呈现两份真实官方 Fuyao Parquet 的 `stock_daily` / `stock_action` 字段和值；已有可信原始响应样本的其他映射列入补充表。

本文件只为 §4|02 的人工审阅准备材料。原始样本中的字段和值与按独立契约/官方文档推导的候选 contract 值分列；“候选相符”不等于运行过逐行映射器或结论 PASS。不得把本文、代码注释、既往负责人信息或空白签字格解释成 cloudQuant 已确认。

## 1. 判据与证据边界

§4|02 原文要求：“字段映射/单位换算/key 规范化经人工确认（抽样比对原始响应）”。缺口关闭条件是具名审阅人签认映射；本材料不改验收台账或历史判据。

独立期望值的依据按层次列示：

1. 对 THS 官方 Parquet，字段语义取本地冻结的 Fuyao 官方文档快照，而不是 `dumps.py` 的列名常量；样本的具体值取已下载 Parquet 实际行。
2. contract 字段、类型及单位取 `opendata/data/models/market.py`、`adjustment.py`、`index.py`、`financial.py`。`ths.yaml` / `akshare.yaml` 是待审映射，只用于并列展示当前声明，不单独充当期望值的独立证明。
3. 时间戳转换行为由 `opendata_fuyao/endpoints.py::millis_to_trading_date` 实现；代码行为只说明当前转换，不替代审阅人对上海日期口径的签认。
4. AKShare 样本的采集日期、上游函数、行数与字段类型取各 fixture 的 `meta.json`；具体值取相应 `reference.csv.gz`（记录响应经过上游函数解析后的帧）或被固定的原始 HTTP 响应。fixture 真实录制不等同于该源已获权利确认，也不保证样本与当前映射的 endpoint 完全一致。

不在本包中猜测：没有可靠原始字段/值的 source/domain 标为 `NOT_AVAILABLE`；无单位依据、键冲突、当前映射与 contract 不一致的字段明确留作问题；所有 cloudQuant 结论与签名栏保持空白。

## 2. 冻结的官方样本与范围

官方文件位于 Git 外路径 `/tmp/opendata-c65-official-parquet/`。`official-parquet-download.json` 记录下载时间、dump ID、字节数、行数和 SHA-256。下列本地 SHA-256 与清单一致。

| 官方源对象 | 本地样本 / 实际范围 | 下载时间（UTC） | 行数 / 标的数 | SHA-256 |
|---|---|---|---:|---|
| `a_share_daily_k_1d_none_10d` | `a_share_daily_k_1d_none_10d.parquet`，近 10 个交易日日 K | 2026-09-30 14:56:28 | 55,552 / 5,566 | `070b957a201f2e76f1ba21cf362430bfac83f4d594de36ba349e16b3acca62fb` |
| `a_share_adjustment_factors_event_none_all` | `a_share_adjustment_factors_event_none_all.parquet`，全量除权除息事件 | 2026-09-30 14:56:28 | 57,530 / 5,435 | `39e1f4cdeca0bad9fd3a4617f2c30d48d3031015f8c6d0e9b8c62426589d553a` |

官方资料快照：`llms-full.txt`，2026-09-30 15:01:20 UTC，SHA-256 `599fe34d0a497535f795cde31f938a7fe8ee6441eedb67882e934b4e58c002c3`；`dump-doc.html`，同日，SHA-256 `1b88ef300a0601f6e6ccd75233c5f233b562201aa9a9fae37c2bf15ad0ae94a7`。均在上述 Git 外目录，下载清单见 `docs/evidence/C65/official-parquet-download.json`。本文不复述预签名下载地址或其他 URL。

**覆盖边界：**日 K 文件是 `10d` 样本，不是 10 年文件；样本实含 5,566 个不同 `thscode`，不能代表 5,578 个当前在册 A 股目录已全覆盖。事件文件实含 5,435 个不同 `thscode`。这两份文件足以提供真实字段/值审阅样本，不能据此签认全市场、十年覆盖、稳定 schema 或再分发权利。

## 3. 独立 contract 依据与当前映射声明

| source/domain | contract 依据 | 当前映射声明（待审） | 核查边界 |
|---|---|---|---|
| `ths/stock_daily` | `Bar`：`symbol`、`trade_date: date`、OHLC、`volume`（股）、`amount`（CNY）。Fuyao 官方文档称日 K 主键 `(thscode, date_ms)`；`date_ms` 是 Asia/Shanghai 零点毫秒；`adjusted=none`；成交量为股、成交额为原始货币。A 股样本 `currency=CNY`。 | `ths.yaml`：key `[symbol, trade_date]`；`symbol <- thscode` 且 `plain`；`trade_date <- trade_date`，毫秒旁列 `date_ms`；OHLC/volume 原样；`amount <- turnover`；`adjust=unadjusted`。 | 官方 key 与 contract key 字段名不同。官方文档证明 `thscode` 带交易所后缀；plain 去后缀来自当前映射约定，需审阅人确认 contract 是否应使用裸码。样本没有另一个 `ticker` 字段用于日 K 键交叉校验。 |
| `ths/stock_action` | `CorporateAction`：`symbol`、`ex_date: date`、现金分红/送股比例/配股比例/配股价，模型说明均为每股口径、现金金额为 CNY。官方文档主键 `(thscode, ex_date_ms)`，日期为上海零点毫秒；字段分别说明税前现金分红、每股送股比例、配股比例、原始货币配股价。 | `ths.yaml`：key `[symbol, ex_date]`；`symbol <- thscode` 且 `plain`；`ex_date <- ex_date`；四个事件数值直接映射，无 scale。 | 官方样本含重复 `(thscode, ex_date_ms)` 键：3 个键组、6 行；其中有相同重复和同键不同值。必须由审阅人决定重复/冲突口径，不能自行去重或合并后签认。官方说明没有 `event_type` 字段。 |

## 4. THS `stock_daily`：真实官方样本字段和值

样本行取 Parquet 中 `thscode=000001.SZ`、最新 `date_ms`。官方文档定义的毫秒语义与独立 UTC→上海转换如下：

`date_ms=1790697600000` → `2026-09-29 16:00:00 UTC` → `2026-09-30 00:00:00 Asia/Shanghai` → 候选 contract `trade_date=2026-09-30`。

| 官方原始字段 | 实样值 | 候选 contract 字段/值 | 独立类型、单位或转换依据 | 差异 / 待确认 |
|---|---:|---|---|---|
| `thscode` | `000001.SZ` | `symbol="000001"`（按当前 `plain` 规则） | `Bar.symbol: str`；`data/mapping.py` 的 `plain` 定义为去掉第一个 `.` 及其后缀。 | `Bar` 类型本身未规定裸码还是带后缀；是否去后缀须签认。 |
| `date_ms` | `1790697600000` | `trade_date=2026-09-30` | 官方文档：上海零点毫秒；契约为 `date`。以 `millis_to_trading_date` 转换，时区为 `Asia/Shanghai`。 | 官方 Parquet 没有 `trade_date` 列；不能把 `date_ms` 原样当日期。 |
| `open_price` | `11.36` | `open=11.36` | `Bar.open: float`；官方称 OHLC 为原始货币计价，样本 `currency=CNY`。scale=1。 | 样本级候选相等，未运行映射器端到端逐行比较。 |
| `high_price` | `11.65` | `high=11.65` | `Bar.high: float`；CNY，scale=1。 | 同上。 |
| `low_price` | `11.33` | `low=11.33` | `Bar.low: float`；CNY，scale=1。 | 同上。 |
| `close_price` | `11.57` | `close=11.57` | `Bar.close: float`；CNY，scale=1。 | 同上。 |
| `volume` | `104535745.0` | `volume=104535745.0` | `Bar.volume: float`，契约单位股；官方字段说明为股。scale=1。 | 不应套用 AKShare“手→股”×100 的转换。 |
| `turnover` | `1205814857.64` | `amount=1205814857.64` | `Bar.amount: float`，契约单位 CNY；官方称原始货币、样本币种 CNY。scale=1。 | 样本级候选相等，未运行映射器端到端逐行比较。 |
| `currency` | `CNY` | 无直接 Bar 字段 | 官方字段是币种代码；样本为 CNY。 | 是否要求检查并拒绝非 CNY，属于映射/入口校验待签事项。 |
| `interval` | `1d` | 无直接 Bar 字段 | 官方文档与样本均为日 K。 | 应确认非 `1d` 值是否拒绝；本包未测试。 |
| `adjusted` | `none` | 价格口径 `unadjusted` | 官方文档称固定 `none`；`Bar` 仅存不复权价格，查询时才合成复权序列。 | 是否把样本约束纳入验收由审阅人确认。 |

六个真实代码的键和值抽样；日期戳都为 `1790697600000`，上海交易日都是 `2026-09-30`。候选 contract key 仅按当前 mapping 的 `plain` + 上海日期转换推导：

| 官方 `thscode` | 候选 contract `symbol` | raw `date_ms` | 候选 `trade_date` | raw OHLC（原始货币） | raw `volume`（股） | raw `turnover`（CNY） |
|---|---|---:|---|---|---:|---:|
| `000001.SZ` | `000001` | 1790697600000 | 2026-09-30 | 11.36 / 11.65 / 11.33 / 11.57 | 104535745 | 1205814857.64 |
| `000002.SZ` | `000002` | 1790697600000 | 2026-09-30 | 3.80 / 4.40 / 3.67 / 4.26 | 1309443983 | 5158052403.74 |
| `600519.SH` | `600519` | 1790697600000 | 2026-09-30 | 1239.53 / 1268.00 / 1236.05 / 1258.62 | 3833098 | 4797246635.88 |
| `600000.SH` | `600000` | 1790697600000 | 2026-09-30 | 9.22 / 9.49 / 9.16 / 9.48 | 147484820 | 1386209937.38 |
| `300750.SZ` | `300750` | 1790697600000 | 2026-09-30 | 290.00 / 292.70 / 285.80 / 291.11 | 29699471 | 8613929784.25 |
| `601318.SH` | `601318` | 1790697600000 | 2026-09-30 | 52.68 / 53.49 / 52.41 / 53.29 | 55884199 | 2960017331.02 |

本样本日 K 官方 key `(thscode, date_ms)` 共 55,552 行，未观察到重复官方 key；5,566 个 raw code 去后缀后仍为 5,566 个不同代码。该读数只覆盖这份 10d Parquet，不证明十年全量唯一性，也不代替对日 K contract key 的人工签认。

## 5. THS `stock_action`：真实官方事件样本与业务键问题

六个不同标的的真实事件；raw key 是官方定义的 `(thscode, ex_date_ms)`，候选 contract key 是 `(symbol, ex_date)`。日期均按官方上海零点语义换算。映射 scale 当前为 1。

| raw `thscode` | raw `ticker`（展示字段） | raw `ex_date_ms` → 上海日 | `dividend_per_share` → `cash_dividend`（每股 CNY） | `per_share_bonus` → `stock_dividend`（每股比例） | `allotment_ratio` → `rights_shares`（比例） | `allotment_price` → `rights_price`（CNY） | 候选 contract key |
|---|---|---|---:|---:|---:|---:|---|
| `000001.SZ` | `000001` | 1790179200000 → 2026-09-24 | 0.249 | 0 | 0 | 0 | (`000001`, 2026-09-24) |
| `000002.SZ` | `000002` | 1692892800000 → 2023-08-25 | 0.68 | 0 | 0 | 0 | (`000002`, 2023-08-25) |
| `600519.SH` | `600519` | 1782403200000 → 2026-06-26 | 28.02423 | 0 | 0 | 0 | (`600519`, 2026-06-26) |
| `600000.SH` | `600000` | 1784131200000 → 2026-07-16 | 0.42 | 0 | 0 | 0 | (`600000`, 2026-07-16) |
| `300750.SZ` | `300750` | 1786291200000 → 2026-08-10 | 1.411 | 0 | 0 | 0 | (`300750`, 2026-08-10) |
| `601318.SH` | `601318` | 1788969600000 → 2026-09-10 | 0.98 | 0 | 0 | 0 | (`601318`, 2026-09-10) |

官方文档没有显式 `event_type`。以下分类仅按官方数值字段的非零模式选样，不是源端标签：

| 观察类别（本文描述） | raw key | raw 事件字段 | 候选 contract 数值 | 独立文档依据 |
|---|---|---|---|---|
| 仅现金 | `000001.SZ / 940176000000`（上海日 1999-10-18） | cash=0.6, bonus=0, allotment=0, price=0 | `cash_dividend=0.6`，其余为 0 | 官方 `dividend_per_share` 是税前每股现金分红；currency=`CNY`。 |
| 仅送股 | `000001.SZ / 833126400000`（上海日 1996-05-27） | cash=0, bonus=1.0, allotment=0, price=0 | `stock_dividend=1.0`，其余为 0 | 官方 `per_share_bonus` 是每股送股比例；文档例示 0.1 表示 10 送 1。 |
| 仅配股 | `000001.SZ / 773683200000`（上海日 1994-07-09） | cash=0, bonus=0, allotment=0.1, price=5.0 | `rights_shares=0.1`, `rights_price=5.0` | 官方定义配股比例及原始货币配股价；A 股样本币种 CNY。 |

### 官方 key 重复与冲突（必须由审阅人决定）

对全文件按官方 key 分组得到 **3 个重复 key 组、6 行**，即比每个 key 保留一行多出 3 行。候选 contract key 会继承相同冲突；本材料不去重、不求和、不挑选优先行。

| raw `thscode` | `ex_date_ms` → 上海日 | 两条实样值（cash / bonus / allotment / price） | 观察 |
|---|---|---|---|
| `000601.SZ` | `878486400000` → 1997-11-03 | `(0, 0.4, 0, 0)`；`(0, 0.4, 0, 0)` | 同 key 完全重复。 |
| `000812.SZ` | `906393600000` → 1998-09-22 | `(0.2, 0, 0, 0)`；`(0, 0.1, 0, 0)` | 同 key 不同事件字段。 |
| `603883.SH` | `1719417600000` → 2024-06-27 | `(0.16, 0, 0, 0)`；`(0.5, 0.3, 0, 0)` | 同 key 不同现金/送股值。 |

因此 `stock_action` 的候选 key **尚不能签为唯一**。请 cloudQuant 明确：是否接受源端同日多事件，若接受应采用什么契约键/合并规则，及 `ticker` 是否仅作展示。空白决定栏见 §8。

## 6. 其他映射 source/domain 的真实样本与缺口

下表只列 fixture 元数据标记为 `recorded` 的真实响应，或已有的历史真机比较档案。响应 gzip 的 SHA 是仓内原始录制文件 SHA；`reference.csv.gz` 是响应经上游搬运函数解析后的参考帧。fixture 例子不会被冒充为 synthetic-independent 的官方 Parquet，也不自动证明其字段语义或权利。

| source/domain | 本地真实样本与 raw 字段/值 | 按独立 contract 的候选值 | 单位/key/差异与当前审阅状态 |
|---|---|---|---|
| `akshare/index_constituent` | 2026-09-24 录制 `index_stock_cons_weight_csindex`；原始响应归档 `tests/fixtures/upstream/index_constituent/responses.json.gz` SHA-256 `fd2ccc6720c30925f29a6538860eaf7d8963fb43237aeb42f08a2213c60355b9`。解析帧样例：`日期=2026-08-31`，`指数代码=300`，`成分券代码=1`，`权重=0.433`。 | `IndexConstituent` 类型要求日期/字符串代码/float weight；独立模型说明 weight 为百分数。上游 port 在 `opendata_http/index/index_cons.py` 将指数及成分数字码 zfill 到 6 位，候选为 `index_symbol=000300`, `symbol=000001`, `as_of=2026-08-31`, `weight=0.433%`；scale=1。 | 候选 business key `(index_symbol, symbol, as_of)`。录制原始 Excel 列头为双语，映射使用上游解析帧中的中文列名。期望代码补零依赖 port 行为，需审阅人确认；权重百分比由 contract 模型独立支持。 |
| `akshare/financial_statement` | 2026-09-24 录制 `stock_financial_report_sina(stock=sh600519, symbol=资产负债表)`；原始响应 SHA-256 `fd72f5ec732bb17f2fb7b2c757863c5ded216b57a423a2dcd3488a372af4a0b6`。2026-06-30 行：`报告日=20260630`、`公告日期=20260815`、`资产总计=309050784569.31`、`币种=CNY`、`类型=合并期末`。 | 日期候选 `report_period=2026-06-30`、`announce_date=2026-08-15`；`symbol=600519`。值候选 `资产总计=309050784569.31 CNY`，C10 历史跨源判据记载新浪/THS 报表同为元。契约还要求 `statement_type ∈ {balance,income,cashflow}`、normalized `item`、revision 默认 1。 | **存在映射/contract 差异待决**：AKShare 把中文 `statement_type`（本例“资产负债表”）及中文 `item`（本例“资产总计”）passthrough；contract 文档写规范值（如 `balance`、`total_assets`）。不得自行翻译后称已签；历史 C10 PASS 不填补此人工确认。candidate units 为元，scale=1。 |
| `akshare/financial_indicator` | 2026-09-24 录制 `stock_financial_analysis_indicator_em`；原始响应 SHA-256 `9488d301da23c8742e87860165c3ed594b5adc7ef6c9afaf30b5e42f590e394a`。真实 JSON 字段：`SECUCODE=600519.SH`、`REPORT_DATE=2026-06-30 00:00:00`、`NOTICE_DATE=2026-08-15 00:00:00`、`EPSJB=35.57`、`ROEJQ=16.75`。 | 候选 `symbol=600519`，`report_period=2026-06-30`，`announce_date=2026-08-15`；passthrough `indicator=EPSJB/value=35.57` 与 `indicator=ROEJQ/value=16.75`；revision 默认 1。 | 候选 key `(symbol, report_period, indicator)`；**单位不可得**：contract 的 `unit` 可空，但录制源无 `unit` 列，而不同指标单位混合；不得把 35.57 擅自签成元/股或百分数。现 mapping `unit <- unit` 需审阅人处理缺列/空值语义。 |
| `akshare/stock_daily` | 2026-09-24 录制 Sina `stock_zh_a_daily(symbol=sh600519, 2024-01-01..2024-01-31)`；原始响应 SHA-256 `e307aedfcbd2c615770e7aa75282a29280a9bb8571ac9a2526a38f8e18c8ac79`。参考帧 2024-01-02：`date=2024-01-02, open=1715.0, high=1718.19, low=1678.1, close=1685.01, volume=3215644, amount=5440082548.0`。 | 这是一条真实 Sina fallback 样本，非当前 `akshare.yaml` 中文字段声明对应的 Eastmoney raw frame；帧也没有 row-level code 字段。 | **`NOT_AVAILABLE_FOR_DECLARED_MAPPING`**：不能用这条 Sina 样本签 `股票代码/日期/开盘...`、`volume × 100`。仍需与当前映射所指 endpoint 一致的原始字段/值样本；C64 provider 包指出 Eastmoney `stock_daily_raw` 案例为 PENDING。 |
| `akshare/stock_action`（当前 YAML 无此 domain） | 两个真实录制 fixture：`stock_action_dividend`（2026-09-24，`stock_history_dividend_detail(600519, 分红)`，31 行，响应 SHA `0c1c922c41083f91b20a27293b0b9c9e72eba06bb311549b249127c521cdcffe`），样例 `除权除息日=2026-06-26, 派息=280.242, 送股=0, 转增=0`；`stock_action_rights`（同日，`stock_history_dividend_detail(000001, 配股)`，3 行，SHA `bd75aab41102fac34ca1a3c2e1e4a8ec823c553f39814d406a2645c70eb3811f`），样例 `除权日=2000-11-06, 配股方案=3, 配股价格=8`。 | 当前无 AKShare `stock_action` contract mapping；这些值不做目标字段、单位或 key 推导。 | **`NOT_MAPPED`**：样本存在，映射不存在；不能借此补出单位或把 `配股方案=3` 猜成每股/每十股比例。THS 官方 action 样本见 §5。 |
| `ths/index_daily` | C5 历史真机 cross-check 有 2026-01-05..2026-09-24 窗口与标的聚合结果；例如 000001.SH 为 178 天。留档未包含可逐字段提取的 THS 原始响应行。 | `ths.yaml` 有 `symbol/trade_date/OHLC/volume/amount` 映射；本包没有可独立核对的 row-level raw 值。 | `NOT_AVAILABLE_RAW_SAMPLE`：C5 聚合比较不能代替原始字段样本；单位及毫秒字段本次不签。 |
| `ths/futures_daily` | C6 历史比较报告有合约、65 日重叠和聚合最大偏差；没有可复核的逐行 THS 原始响应值/列样本。 | `ths.yaml` 有 `symbol/trade_date/OHLC/volume/amount`；该腿的停牌形态标为 `unmeasured`。 | `NOT_AVAILABLE_RAW_SAMPLE`：不把比较表的聚合结果当作原始字段/单位签认。 |
| `ths/option_daily` | C6 报告留有标准化比较值，例如 `CU2611P102000.SHF` 在 2026-06-04 的成交量 THS=46、Sina=44；该报告明确部分期权成交量来源口径未定。 | 当前可确认的是候选比较字段 `volume`，不是原始 THS 列名/原始行；`ths.yaml` 映射列名按源契约登记。 | `NOT_AVAILABLE_RAW_SAMPLE`：该差异数字来自历史跨源比较而非所需原始响应字段样本；volume 单位不能据此签认。 |
| `ths/financial_statement` | C10 历史真机 cross-check 给出报表期数、判断单元格数及差异计数；归档没有本包可引用的真实 raw row。 | `ths.yaml` 有英文 line-item/key 映射；contract 为 long-form statement rows。 | `NOT_AVAILABLE_RAW_SAMPLE`：C10 聚合 PASS 不等于字段和值的人工签认。 |

fixture 来源索引：`tests/fixtures/upstream/<name>/meta.json` 明确 `status=recorded`、采集日期和上游函数；样本值来源分别为 `reference.csv.gz` 或保存的 HTTP 响应。上述 AKShare fixture SHA 仅固定被列出的 `responses.json.gz` 文件；未列出的 fixture 不纳入本包签认。THS C5/C6/C10 是历史比较报告，不是可复用的原始响应 fixture。

## 7. 映射覆盖清点

当前两个 YAML 合计 **10 个 source/domain 映射**。本包能提供与映射端点相符的可读字段/值样本的有：`ths/stock_daily`、`ths/stock_action`、`akshare/index_constituent`、`akshare/financial_statement`、`akshare/financial_indicator`；其中后三项仍存在 §6 所述人工审阅点。`akshare/stock_daily` 只找到另一 Sina fetch route 样本，不足以签当前声明。`ths/index_daily`、`ths/futures_daily`、`ths/option_daily`、`ths/financial_statement` 有历史比较报告但本地归档缺逐行 raw 样本。额外 AKShare stock-action fixture 有真值但没有 YAML 映射。

| YAML source/domain | 样本覆盖 | 审阅状态建议（不是验收判定） | cloudQuant 结论 |
|---|---|---|---|
| `ths/stock_daily` | 官方 Parquet；6 标的键和值；见 §4 | `READY_FOR_REVIEW`；plain 后缀与 date/key 需确认 |  |
| `ths/stock_action` | 官方全量事件 Parquet；6 标的 + 现金/送股/配股示例；见 §5 | `REVIEW_REQUIRED`；官方 key 有 3 组重复，其中 2 组冲突 |  |
| `ths/index_daily` | C5 聚合对照，无 row raw | `NOT_AVAILABLE_RAW_SAMPLE` |  |
| `ths/futures_daily` | C6 聚合对照，无 row raw | `NOT_AVAILABLE_RAW_SAMPLE` |  |
| `ths/option_daily` | C6 有历史标准化成交量差值，无 row raw | `NOT_AVAILABLE_RAW_SAMPLE` |  |
| `ths/financial_statement` | C10 聚合跨源对照，无 row raw | `NOT_AVAILABLE_RAW_SAMPLE` |  |
| `akshare/stock_daily` | Sina 记录样本与当前声明 endpoint/字段不匹配 | `NOT_AVAILABLE_FOR_DECLARED_MAPPING` |  |
| `akshare/index_constituent` | 录制的中证权重响应 | `READY_FOR_REVIEW`；确认 source parse 与 key 类型 |  |
| `akshare/financial_statement` | 录制的 Sina 三大报表 | `REVIEW_REQUIRED`；中文 statement_type/item 与契约规范码有差异 |  |
| `akshare/financial_indicator` | 录制的东方财富 F10 响应 | `REVIEW_REQUIRED`；unit 原始字段缺失且单位混合 |  |

## 8. cloudQuant 签认栏

请由 cloudQuant 对每个 source/domain 的字段来源、contract 目标、单位/scale、上海日期转换、后缀规范化及 business key 作出本人结论。缺样本、映射冲突和重复键可以签为有条件/阻塞状态；不要求在本文件中强行得出 PASS。

| source/domain 或问题 | cloudQuant 结论（请本人填写） | 签名 | 日期 |
|---|---|---|---|
| `ths/stock_daily`：单位、`adjusted=none`、`date_ms` 上海时间、plain code、键 |  |  |  |
| `ths/stock_action`：逐股数值单位、上海除权日、plain code、同日重复/冲突键处理 |  |  |  |
| `akshare/index_constituent`：代码补零、weight percent、as_of 语义与 key |  |  |  |
| `akshare/financial_statement`：中文 type/item 到 contract code、元单位与 key |  |  |  |
| `akshare/financial_indicator`：indicator passthrough、unit 缺失和混合单位处理 |  |  |  |
| `akshare/stock_daily`：为当前声明映射补齐同 endpoint 实样或修订映射 |  |  |  |
| `ths/index_daily`、`ths/futures_daily`、`ths/option_daily`、`ths/financial_statement`：是否要求取得 raw 行样本后再签 |  |  |  |

**本包当前结论：**`WAITING_FOR_CLOUDQUANT_REVIEW`。字段和值来自真实本地样本；上述样本/期望并列表不是批准、权利授权、生产对照 PASS 或完整覆盖证明。若签认要求补取额外原始响应，应另行走授权且可审计的取样流程，本包未访问 provider。

## 9. 用户委托的逐腿审阅结论

授权：用户直接回复“你帮我直接审阅”。主智能体已核对33条启用provider×domain腿的当前声明、契约、源码和可用原始样本；10条有这两份YAML显式映射（AKShare4 / THS6），另23条依各adapter源码转换。缺少YAML不等于没有转换，也不证明转换已通过原始字段/单位/key审阅。详表 `mapping-review-results.json` 保留每腿定位、raw proof、具体风险和当前决定。

| provider/domain | 当前决定 | 依据/未解决项 |
|---|---|---|
| akshare/stock_daily | GAP_EVIDENCE_REMAINS | The available C65 sample does not establish the declared endpoint field or volume mapping. |
| ths/stock_daily | QUALIFIED_SAMPLE_REVIEW | Official Parquet supports the listed raw fields and sample values; date is derived before the mapping layer and plain-code/key semantics remain for human review. |
| yfinance/stock_daily_overseas | GAP_EVIDENCE_REMAINS | Source code documents transformation behavior, but this C65 inventory does not support field/unit/key signoff. |
| ecb/economy_cpi | GAP_EVIDENCE_REMAINS | No C65 evidence here for provider-specific field, unit, or key mapping. |
| fred/economy_cpi | GAP_EVIDENCE_REMAINS | Evidence supports sample date/value parity for CPIAUCSL only. |
| imf/economy_cpi | GAP_EVIDENCE_REMAINS | No C65 evidence here for provider-specific field, unit, or key mapping. |
| oecd/economy_cpi | GAP_EVIDENCE_REMAINS | No C65 evidence here for provider-specific field, unit, or key mapping. |
| ecb/economy_gdp | GAP_EVIDENCE_REMAINS | No C65 evidence here for provider-specific field, unit, or key mapping. |
| fred/economy_gdp | GAP_EVIDENCE_REMAINS | Evidence supports sample date/value parity for GDPC1 only. |
| imf/economy_gdp | GAP_EVIDENCE_REMAINS | No C65 evidence here for provider-specific field, unit, or key mapping. |
| fred/economy_unemployment | GAP_EVIDENCE_REMAINS | Evidence supports sample date/value parity for UNRATE only. |
| imf/economy_unemployment | GAP_EVIDENCE_REMAINS | No C65 evidence here for provider-specific field, unit, or key mapping. |
| oecd/economy_unemployment | GAP_EVIDENCE_REMAINS | No C65 evidence here for provider-specific field, unit, or key mapping. |
| ecb/economy_rate | GAP_EVIDENCE_REMAINS | No C65 evidence here for provider-specific field, unit, or key mapping. |
| akshare/futures_daily | GAP_EVIDENCE_REMAINS | Current C65 materials do not establish this leg's field/unit/key mapping. |
| ths/futures_daily | GAP_EVIDENCE_REMAINS | Historical aggregates do not establish raw field/unit mapping. |
| akshare/fund_etf_daily | GAP_EVIDENCE_REMAINS | Current C65 materials do not establish this leg's field/unit/key mapping. |
| ths/fund_etf_daily | GAP_EVIDENCE_REMAINS | Historical adjustment evidence does not establish a current raw field map. |
| akshare/index_daily | GAP_EVIDENCE_REMAINS | Current C65 materials do not establish this leg's field/unit/key mapping. |
| ths/index_daily | GAP_EVIDENCE_REMAINS | Aggregate comparison does not establish raw field/unit mapping. |
| akshare/option_daily | GAP_EVIDENCE_REMAINS | Current C65 materials do not establish this leg's field/unit/key mapping. |
| ths/option_daily | GAP_EVIDENCE_REMAINS | Historical normalized comparison does not establish raw fields or volume unit. |
| akshare/bond_daily | GAP_EVIDENCE_REMAINS | Current C65 materials do not establish this leg's field/unit/key mapping. |
| ths/stock_action | FAIL_CONFLICTING_BUSINESS_KEY | Parquet sample contains key collisions and lacks a source identity field capable of resolving them; REST and Parquet field evidence must remain separate. |
| akshare/stock_action | GAP_EVIDENCE_REMAINS | Raw fixtures exist but no target mapping/unit/key is supported by this review. |
| akshare/financial_statement | GAP_PIT_OR_UNIT_MAPPING | Sample supports the observed fields/value but leaves normalization and PIT key completeness unresolved. |
| ths/financial_statement | GAP_PIT_OR_UNIT_MAPPING | Aggregate C10 comparisons do not establish row-level mapping; PIT key dimensions remain absent from the declared key. |
| akshare/financial_indicator | GAP_PIT_OR_UNIT_MAPPING | Observed raw values do not determine unit; declared unit/key normalization is not established. |
| akshare/index_constituent | GAP_EVIDENCE_REMAINS | Recorded sample supports a candidate list-date/percent mapping but does not itself sign the normalization. |
| ths/index_constituent | GAP_EFFECTIVE_DATE_SEMANTICS | Same key name does not denote the same membership effective date across the two sources. |
| ths/trading_calendar | GAP_EVIDENCE_REMAINS | Existing calendar cross-check evidence is bounded and does not establish a current field map. |
| ths/instrument | GAP_EVIDENCE_REMAINS | Existing universe evidence does not establish a historical instrument field mapping. |
| ths/fund_action | GAP_EVIDENCE_REMAINS | Historical date/amount comparison does not establish a current raw field mapping. |

THS日线：10d官方Parquet抽样可确认股/CNY、未复权、上海零点日期；裸代码去后缀在该文件无冲突，按A股stock_daily契约限定范围认可。日期先经dump导入衍生，不能把Parquet raw行直接当REST或YAML前置行，不能扩大到十年/历史退市标的。

THS公司行动：当前(symbol, ex_date)继承3个重复键组，其中2组冲突。没有官方稳定event ID/type可定义新键，故直接判FAIL并保留问题；不通过数值摘要伪造业务身份、不求和不同事件、不静默挑行。REST缺失配股字段的证据不由Parquet补造。

财务：两源financial_statement和AKSharefinancial_indicator的key省略announce_date/revision，无法据此签PIT修订唯一性；AKShare原始指标缺unit且有混合单位，中文item/statement直接传递也未证明标准化。指数成员：AKShare list_date与THS请求观察日不是同一生效日，不能仅因字段名as_of相同合并认可。日历/标的当前快照不能代替历史覆盖或未来日期。

FRED三series的官方JSON/CSV对照可确认抽样日期/数值一致；单位、vintage和统计真值没有独立完备证明。其余宏观/海外/期货/期权/基金/债券腿沿用具体缺证结论。当前审阅动作已完成，§4|02整体尚未逐条达标；不是等待用户再次审阅或补个人签名。
