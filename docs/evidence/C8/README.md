# C8 · yfinance 海外日线：两处存储口径缺陷的实测与转正（AC-10）

对应验收项：**AC-10（1C/C1 provider 扩充）**、AC-5（P1/P0 域数据源与 fidelity）、
AC-17（覆盖率与门禁复跑）。

C1 轮把 `stock_daily_overseas` 以 `verified=false` 登记，理由是「上游是 Yahoo，
SDK 自己说 `auto_adjust` 就是复权开关」。**这句话按字面信下来就是本次要收的债**：
实测下来该适配层会往库里写两类错价，且第二类是**不可由窗口自证**的。本轮先量后改，
改完用跨 vendor 真机对照转正。

## 1. 产物

| 文件 | 作用 |
|---|---|
| `opendata/data/providers/yfinance/models/_sdk.py` | 唯一的 SDK 触点：`auto_adjust=False` + `end` 右移一天 + `require_split_history`（取**全量**拆分史，不是窗口内）；缺 SDK / 拆分史请求失败均 fail-closed |
| `opendata/data/providers/yfinance/models/stock_daily.py` | `split_events`（两路拆分来源对账）+ `lookahead_factors`（只看**严格更晚**的拆分）+ `as_traded_bars`（价×f、量÷f）+ `settled_rows`（未定盘即丢）；`YfinanceStockDailyFetcher.capability.verified=true` |
| `scripts/ops/yfinance_overseas_cross_check.py` | 真机跨 vendor 对照（9 腿，含 `--counterfactual` 反事实模式与 `KNOWN_DEVIATIONS` 例外登记） |
| `yfinance-overseas-cross-check.txt` | 转正判据：9 腿 PASS |
| `auto-adjust-counterfactual.txt` | 反事实表：修复前两套写法各自会存成什么 |
| `gate.txt` | 完整未裁剪门禁日志（5,444 行，含分支/HEAD/脏文件清单抬头） |
| `opendata/data/openbb_map.yaml` | `stock_daily_overseas` 腿 `status: registered → verified` |
| `tests/test_yfinance_provider.py` | 12 → **27** 项离线单测（合成上游帧 + fake SDK 模块 + 三条 fail-closed 码全打） |

## 2. 两条存储口径缺陷（先实测，再改代码）

### 缺陷一：`auto_adjust` 默认把**分红**折进 OHLC

SDK 默认 `auto_adjust=True`，官方语境的「adjusted」含分红。以 sina 的**不复权**收盘为
基准，窗口 2026-08-03..2026-09-22 实测：

| 腿 | 窗口内分红 | 若按默认（`auto_adjust=True`）存储，最大相对偏差 |
|---|---|---|
| AAPL | 0.27 | 8.62e-04 |
| KO | 0.53 | **5.93e-03** |
| MSFT | 0.91 | 1.88e-03 |
| 0005.HK | 0.78 | **4.86e-03** |
| 0700.HK | 0.00 | 9.16e-09（该窗无分红 ⇒ 不受影响，正是对照组） |

偏差量级随窗口内现金额变化、无分红腿干净到浮点表示位——这就是「折进了分红」而非
「别的什么噪声」。D10 规定库内只存不复权（复权序列在查询期由 `Bar + 复权因子` 合成），
故 seam 显式 `auto_adjust=False`。复算命令见 §5。

### 缺陷二：`auto_adjust=False` 之后，价格里**仍然藏着拆分调整**，且该调整是全局的

`auto_adjust=False` 只关掉分红那条链。OHLC 仍被**窗口之后**的每一次拆分反向除过
（`Volume` 被乘过），而除的依据是**该标的的完整拆分史**，不是本次响应里的行：

```
请求 AAPL 2020-06-01 .. 2020-07-14   ← 窗口内没有任何拆分事件
返回 2020-07-14 close = 97.0575      ← 当日实际成交 388.23（÷4）
响应内 Stock Splits 列               ← 全窗口无一行提到 4:1（拆分在 2020-08-31）
```

即：**只看窗口自身永远发现不了少了一个 4 倍**，也无法从同一次响应里自证还原是否正确。
所以 `extract_data` 必须成对返回「窗口帧 + 全量拆分史」（`YfinanceHistory`），
还原因子只能来自后者。缺陷二比缺陷一危险：一个 6 月的窗口能错到 75%（§5 反事实列
`max dev window-local = 7.50e-01`），而价格看起来仍是「合理的小数」。

两路拆分来源（帧内列 / `Ticker.splits`）在 `split_events` 里做**对账**而非择一：
帧内那列与价格同请求，若二者不一致说明其中一路被截断或过期，猜哪一路都会污染还原，
故失败关闭 `YFINANCE_SPLITS_UNAVAILABLE`（负比例 → `YFINANCE_BAD_SPLIT_RATIO`）。

## 3. 还原规则与上游行为清单

```
f(t)   = ∏ ratios{ 拆分事件 e : e.date > t }        # 严格更晚；除权日当天已按新价成交，f=1
price  = price_upstream × f        volume = volume_upstream / f
```

| 上游行为 | 实测 | 落点 |
|---|---|---|
| 除权日归属 | AAPL 4:1 的 ratio 行落在 **2020-08-31**（当天已是新价基准），故 `searchsorted(side="right")` | `lookahead_factors` |
| `end` 开区间 | 直接透传 `end` 会**丢掉窗口最后一个交易日**（同腿 35 根 vs 36 根） | seam 内 `end + 1 day`，契约侧 `end_date` 仍按闭区间语义 |
| 未定盘的 bar | 进行中/前一日重发的 bar 以 `NaN` 价格 + 部分成交量返回（AAPL 实测约 1.5M 股，且日期标的是**上一交易日**） | `settled_rows` 丢弃（不入库、也不让它触发校验失败） |
| 拆分调整范围 | 全量史，跨窗口边界生效（§2 缺陷二） | 成对返回 `YfinanceHistory` |
| 无 turnover | 上游只有成交量、没有成交额 | `OverseasBar.amount=None`（该契约本就允许空） |

## 4. 真机跨 vendor 对照（转正判据）

```
python scripts/ops/yfinance_overseas_cross_check.py
```

对照面：`get_registry().resolve_domain('stock_daily_overseas',
source='yfinance').fetch(...)`（调用方真实路由路径，即适配层还原后的输出）↔ sina
`stock_us_daily` / `stock_hk_daily` 且显式 `adjust=""`（另一 vendor、另一聚合链、
不复权口径）。9 条腿**按缺陷选点**而非随机：2 个含分红的普通窗口 + 2 个拆分事件各
取「跨除权日」与「全在除权日之前」两侧。

| 腿 | yfinance | sina | 窗口 | 天数 | 重叠 | 收盘最大偏差 | O/H/L 最大偏差 | 成交量比值 yf/sina | 结论 |
|---|---|---|---|---|---|---|---|---|---|
| US 2026-08 | `AAPL` | `AAPL` | 2026-08-03..09-22 | 36 | 1.000 | 4.43e-08 | 1.66e-05 | 0.999999..1.000004 | PASS |
| US 2026-08 div | `KO` | `KO` | 同上 | 35 | 1.000 | 4.08e-08 | 5.81e-05 | 0.999427..1.000019 | PASS |
| US 2026-08 | `MSFT` | `MSFT` | 同上 | 36 | 1.000 | 5.70e-08 | 1.05e-05 | 0.999454..1.000008 | PASS |
| HK 2026-08 | `0700.HK` | `00700` | 同上 | 37 | 1.000 | 9.16e-09 | 2.24e-03\* | 1.000000..1.000000 | PASS |
| HK 2026-08 | `0005.HK` | `00005` | 同上 | 37 | 1.000 | 2.44e-08 | 3.85e-08 | 1.000000..1.000000 | PASS |
| AAPL 4:1 straddle | `AAPL` | `AAPL` | 2020-08-20..09-04 | 12 | 1.000 | 5.46e-08 | 4.27e-08 | 0.996576..1.000001 | PASS |
| **AAPL 4:1 before** | `AAPL` | `AAPL` | 2020-06-01..07-14 | 31 | 1.000 | 7.32e-04\* | 4.52e-08 | 0.997193..1.000002 | PASS |
| CVNA 5:1 straddle | `CVNA` | `CVNA` | 2026-04-27..05-15 | 15 | 1.000 | 4.63e-08 | 1.46e-06 | 0.999867..1.000058 | PASS |
| **CVNA 5:1 before** | `CVNA` | `CVNA` | 2026-04-06..04-24 | 15 | 1.000 | 5.18e-08 | 1.29e-06 | 0.999912..1.000062 | PASS |

9 腿 254 个「标的 × 交易日」联合观测点，重叠比 **9/9 均为 1.000**；未登记日的
收盘偏差上界 5.70e-08，即还原后的价与另一 vendor 的价**逐位相同**（残差是 float64
表示位）。两条 `before` 腿是缺陷二的判据：它们的因子完全来自窗口之外。

\* 两处为**已登记的跨供应商单日修订**，见 §6。

### 容差为什么分三档（而不是一刀切）

| 字段 | 界 | 依据 |
|---|---|---|
| `close` | 1e-06 | 未登记日实测 ≤5.70e-08；这里是「只留浮点表示余量」，不是放宽。收盘是两处缺陷都会显形的字段，界必须紧 |
| `open/high/low` | 1e-04 | 实测上界 5.81e-05（KO）——sina 美股盘中价以低于一分的增量发布，两家舍入面不同；两家**收盘**逐位一致而盘中差到 5e-05，说明是发布精度而非口径 |
| `volume` | 5e-03 | 实测比值 0.996576..1.000062；2020 年的 AAPL 腿成交量差 3.4e-03（sina 侧聚合范围与交易所修正），价格同腿仍为 4e-08。按价格口径卡成交量会把源侧聚合差当成 bug 追 |

## 5. 反事实：修复前两套写法各自会存成什么

```
python scripts/ops/yfinance_overseas_cross_check.py --counterfactual
```

`auto_adjust` 列＝沿用 SDK 默认；`window-local` 列＝C8 修复前的实现（raw 模式，但
因子只取窗口内的拆分行）。偏差＝`max |value / sina 不复权收盘 - 1|`：

| 腿 | 标的 | 窗口 | 窗口内分红 | max dev `auto_adjust` | max dev `window-local` |
|---|---|---|---|---|---|
| US 2026-08 | AAPL | 2026-08-03..09-22 | 0.27 | 8.62e-04 | 4.43e-08 |
| US 2026-08 div | KO | 同上 | 0.53 | 5.93e-03 | 4.08e-08 |
| US 2026-08 | MSFT | 同上 | 0.91 | 1.88e-03 | 5.70e-08 |
| HK 2026-08 | 0700.HK | 同上 | 0.00 | 9.16e-09 | 9.16e-09 |
| HK 2026-08 | 0005.HK | 同上 | 0.78 | 4.86e-03 | 2.44e-08 |
| AAPL 4:1 straddle | AAPL | 2020-08-20..09-04 | 0.00 | 7.58e-01 | 5.46e-08 |
| **AAPL 4:1 before** | AAPL | 2020-06-01..07-14 | 0.00 | 7.58e-01 | **7.50e-01** |
| CVNA 5:1 straddle | CVNA | 2026-04-27..05-15 | 0.00 | 8.00e-01 | 4.63e-08 |
| **CVNA 5:1 before** | CVNA | 2026-04-06..04-24 | 0.00 | 8.00e-01 | **8.00e-01** |

读法：`window-local` 在**跨除权日**的腿上是正确的（5.46e-08），只在**全在除权日之前**
的腿上错到 75%/80%——这正是缺陷二的签名，也是为什么必须有 `before` 型腿：只有跨
两侧的腿才能把「窗口内因子」和「全量史因子」区分开。而 `auto_adjust` 在任何拆分腿上
都是 7.5e-01 级别，因为它同时含着两条链。

## 6. 例外登记会**过期即失败**

两处超过硬判据的日值，逐条写进脚本的 `KNOWN_DEVIATIONS`（键为 `(标的, 日期, 字段)`）
而不是放宽阈值：

- `0700.HK 2026-09-04 high`：yf 447.60 / sina 446.60，+1.00 港元（2.24e-03）。该腿其余
  36 日 high ≤2.9e-08，**非乘性**、非整天平移 ⇒ 单日盘中最高价修订。
- `AAPL 2020-07-09 close`：yf 383.01 / sina 382.73，+0.28 美元（7.32e-04）。同腿 31 日
  开/高/低全部 ≤4.6e-08（且已按 ×4 还原）⇒ 拆分因子无涉，属单日收盘修订。

对照脚本双向判定：出现**未登记**的越界 ⇒ FAIL；**已登记**日重新落回界内 ⇒ 也 FAIL。
例外因此不能只增不减地积成长期豁免，上游一旦对齐就会立刻暴露。

## 7. 代码标识与上游缺口的实测

| 事项 | 实测 | 处置 |
|---|---|---|
| 港股代码形态 | Yahoo 用 `0005.HK`（去前导零 + 后缀），sina 港股通道要 **5 位零填充** `00005` | 每条腿同时写出两侧形态，`0005.HK → 00005` 在报告抬头里声明 |
| sina 传错码的后果 | 传 `0005` 时该通道对 404 不返回空帧而是 `KeyError: 'date'` | 脚本按「取数异常 ⇒ 该腿 FAIL」处理，不改在仓链路（属 B 层搬运代码，非本轮范围） |
| KO 35 根 vs AAPL/MSFT 36 根 | Yahoo 侧 2026-09-22 **没有 KO 行**（sina 有），属上游缺行 | 重叠比按「我方天数」计，仍为 1.000；缺日在表里体现为天数差，不做插补、也不为此放宽 `MIN_OVERLAP` |

## 8. 注册事实变化（复算口径）

| 口径 | 本轮前 | 本轮后 |
|---|---|---|
| 注册表能力 / 数据域 | 27 / 15 | 27 / **15**（无新域） |
| 其中 `verified` | 13 | **14**（`stock_daily_overseas` 转正） |
| `verified` 覆盖的数据域 | 9 | **10** |
| yfinance fetchers | 1 | 1（同一域，改口径不改数量） |
| `openbb_map.yaml` `ours` 状态分布 | verified 13 / registered 14 / pending 4 | **verified 14** / registered 13 / pending 4 |
| 落库权威序 `authority.json` | 不含 `stock_daily_overseas` | **不变**（见下） |

`authority.json` 本轮**不动**，并说明理由：该表是双源落库合并的权威序（dwd 层
`ths > akshare`），而海外日线目前是**单 provider** 域、也不在本迭代 dwd 合并链路内。
把「跨 vendor 验收对照」误登记成「落库双源权威」会是口径造假——对照只在
`scripts/ops/` 的判据里生效，`tests/test_domains.py` 的「权威序只引用已注册域」不变式
同时钉住这张表的边界。

## 9. 离线单测与 yfinance 层覆盖率

27 项全部**无网络**（合成上游帧、`monkeypatch.setitem(sys.modules, "yfinance", fake)`
替换 SDK），按类分组：注册与 auto 路由（4）、查询校验（1）、拆分因子规则（6）、
normalize 阶段（5）、extract 阶段（3）、`_sdk` seam 翻译（5）、`OverseasBar` 契约（3）。

钉住的实测值（不是自造数）：2020-06-02 开盘 80.1875 → **320.75**、收盘
80.835003 → **323.34**、成交量 87,642,800 → **21,910,700**（sina 该日实发
320.75 / 323.34 / 21,910,704），除权日 2020-08-31 当天 f=1，两次更晚拆分复合为
×8/×2/×1，未定盘 NaN bar 被丢，`as_traded_bars` 不改入参。三条 fail-closed
码（`YFINANCE_SDK_MISSING` / `YFINANCE_EMPTY_RESPONSE` / `YFINANCE_COLUMNS_MISSING`）
与两路拆分来源对账失败（`YFINANCE_SPLITS_UNAVAILABLE`）、负比例
（`YFINANCE_BAD_SPLIT_RATIO`）逐条断言稳定码本身。

门禁报告里的本层实测（`gate.txt` 内 `--cov` 表原行）：

| 文件 | 覆盖 | 未覆盖处 |
|---|---|---|
| `models/stock_daily.py` | **100.00%**（81 语句 / 18 分支，0 partial） | — |
| `models/_sdk.py` | 93.62%（35 语句 / 12 分支 / 2 partial） | 第 23 行是 `if TYPE_CHECKING` 里的导入（全库同口径，见下） |
| `registration.py` | 83.33% | 14–16 行为 `if TYPE_CHECKING` 导入块 |
| `_source.py` / `__init__.py` | 100.00% | — |

两条非 TYPE_CHECKING 的缺口是 `_sdk.py` 的一个分支弧（`99->102`）与
`registration.py` 的 `31->35`（显式传入 registry 的调用形态）。全库口径：
**85.44%**（阈值 84%、`precision = 2`）。`if TYPE_CHECKING:` 块未列入
`[tool.coverage.report] exclude_lines`，故这些行在所有包里都记为未覆盖——本轮不改
该配置（改了会一次性抬高所有包的账面覆盖率、破坏与 C5/C6/C7 各轮的可比性），
在此显式声明而不是把它当成本层的缺口藏起来。

## 10. 自我更正（本轮过程内）

| 项 | 更正 |
|---|---|
| 「`auto_adjust=False` 就是不复权」 | **错**。它只断开分红那条链，拆分调整仍在 OHLC 里且依据全量史（§2 缺陷二）。C1 轮的登记理由正是这句未经验证的话 |
| 第一版对照脚本用**单一** 1e-6 界 | 首轮 9 腿里 8 腿 FAIL。逐日诊断后确认残差是 sina 盘中价的发布精度（≤5.8e-05）与两处真实单日修订，遂改为**按字段分档 + 例外登记**（§4/§6）。若当时直接放宽到 1e-3 收工，缺陷一的 5.93e-03 与缺陷二的 7.5e-01 都还能过 |
| `lookahead_factors` 的日期契约 | 直接调用时传 tz-aware 索引会 `TypeError: Cannot compare tz-naive and tz-aware`（真实帧按交易所 tz 索引）。改为在函数内自做一次 `_naive_dates` 归一，把「调用前须剥 tz」这条隐含前置条件消掉，而不是让每个调用方记住它 |

## 11. 门禁复跑

```
make gate      # EXIT=0，完整未裁剪日志 gate.txt（同目录，5,444 行）
```

| 项 | 本轮实测 |
|---|---|
| 抬头 | `branch dev`、`HEAD bcc5cd7`、门禁启动时脏文件 6 项、Python 3.13.5 / node v25.1.0 |
| brand / zero-dep / js-points | OK（品牌 token 零残留；基线 3 未增；引擎 76 / eval 35 / exec 0） |
| a2-check | **226 文件**（C7 为 225，+1 为本轮新脚本）：ruff / format / mypy / bandit 全 ok |
| quality-ratchet | 五项**等于快照**（未增债） |
| public-api-quality | A2 公共可调用 **397**，docstring 与注解双 **100%** |
| test-cov | **2,399 passed / 5 skipped**（C7 为 2,384，+15 为 yfinance 单测 12 → 27）、覆盖率 **85.44%**（C7 85.21%） |
| 前端 | eslint 0 errors / 48 warnings（存量待清）、`vue-tsc` 通过、vitest **8 files / 79 tests** |

## 12. 边界与待办

- 海外日线**未落库**：本轮只转正取数口径与跨 vendor 判据，`ods_*` / `dwd_*` 侧没有
  新增写入（迭代 1 的落库链路以 A 股 P0 域为范围），故 AC-8/AC-9 的双源校对不含它。
- 单 provider：海外域尚无第二个 C1 源可做权威序，跨 vendor 对照活在验收脚本里而非
  落库合并里（§8）。
- 无成交额、无汇率：`amount=None`；报价币种即标的交易所币种，跨币种对齐属迭代 2 的
  日历/汇率域工作。
- `fund_etf_daily`（ths 侧）仍按 D10 不接（C6 §5 已量化：上游只有前复权）。
- 上游拆分史与帧内列的对账是「同请求列 vs 全量史」级别的自证；若 Yahoo 自身的拆分史
  整体过期（两路同时错），本判据不可见，需第三方拆分源才能再进一层——属演进项，
  本轮不假称已覆盖。
