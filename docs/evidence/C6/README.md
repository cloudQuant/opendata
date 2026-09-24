# C6 · ths 侧 P1 域第二源接续（期货日线 + 期权日线转正；ETF 腿按 D10 不接）

对应验收项：**AC-10（1C/C1 provider 扩充）**、AC-5/AC-6（P1 域数据源与 fidelity）、
AC-7 1B（`available → implemented` 推进两条）。

收官口径「尽量多铺 provider」在本轮把 C5 §6 列的三条后续腿做掉两条：`futures_daily`
与 `option_daily` 原本只有 akshare（`verified=false`，不进 `source=auto`），现在各自
有同花顺（fuyao）第二源并通过**跨 vendor 真机对照**转正。第三条 `fund_etf_daily`
经真机测得上游只有**前复权**序列，与 D10「只存不复权」冲突，本轮**不接**并单独归因
留档（§5）。

## 1. 产物

| 文件 | 作用 |
|---|---|
| `opendata_fuyao/endpoints.py` | `FUTURES_PRICES_ENDPOINT` / `OPTIONS_PRICES_ENDPOINT` + `build_period_daily_request`（`time_period=day_1`、无 `adjust`）+ `fetch_period_daily_bars`（行内日期字段 `timestamp`）；`normalize_bars` 增 `date_key` 参数 |
| `opendata/data/providers/ths/models/_client.py` | `resolve_futures_code`（清单精确匹配）/ `resolve_option_code`（要求带后缀透传）+ 共用的 `_resolve_by_listing` |
| `opendata/data/providers/ths/models/futures_daily.py` | `ThsFuturesDailyFetcher`（`asset_class=futures` / `verified=true`） |
| `opendata/data/providers/ths/models/option_daily.py` | `ThsOptionDailyFetcher`（`asset_class=option` / `verified=true`） |
| `opendata/data/providers/ths/registration.py` | `FETCHERS` 3 → 5；文档串记录 ETF 腿被刻意排除的理由 |
| `opendata/data/mappings/ths.yaml` | `futures_daily` / `option_daily` 口径映射（**保留交易所后缀**、`ms_column: timestamp`、无 `scale`） |
| `opendata/data/authority.json` | `futures_daily: [ths, akshare]`（原已在册，本轮补 option）、`option_daily: [ths, akshare]` |
| `opendata/data/openbb_map.yaml` | 期货/期权条目补 ths 腿（`status=verified`）；ETF 条目补 ths 腿 `status=pending` 并写明理由 |
| `opendata_fuyao/endpoint_map.yaml` | 期货/期权日 K 两条 `available → implemented` |
| `scripts/ops/ths_futures_option_cross_check.py` | 真机跨 vendor 对照脚本（7 腿，含标的代码映射规则） |
| `ths-futures-option-cross-check.txt` | 本轮对照报告 |
| `tests/test_fuyao_endpoints.py`、`tests/test_ths_provider.py`、`tests/test_fuyao_endpoint_map.py`、`tests/test_p1_daily_fetchers.py`、`tests/test_data_mapping.py` | 新端点/新解析器/注册与路由转正的回归；后两者另证期货/期权映射回环（后缀保留 + `timestamp` 派生） |
| `pyproject.toml`、`tests/test_quality_gates.py` | 覆盖率阈值判定改为两位小数比较（`precision = 2`）+ 门禁自洽回归，见 §8 |

## 2. 上游实测事实（写代码前先量，非按文档抄）

| 事实 | 实测值 | 落点 |
|---|---|---|
| 周期参数名 | 股票/指数用 `interval=1d`，期货/期权**必须** `time_period=day_1`；传 `interval` 直接 `FUYAO_REQUEST: 请求的标的或字段不受支持` | `build_period_daily_request` |
| 行内日期字段 | 股票/指数 `date_ms`，期货/期权 `timestamp` | `normalize_bars(date_key=...)` + 映射 `ms_column` |
| 窗口上限 | 期货/期权 10.7 年窗口**不被拒**（股票/指数为 10 年）→ 本路径不切块 | `fetch_period_daily_bars` 单请求 |
| 缺参行为 | 只给 `start` 不给 `end` → 报「标的或字段不受支持」（**文案误导**，实为参数不成对） | 构造器强制 `start`/`end` |
| 默认根数 | 不给窗口只回**最后 100 根** | 同上，构造器总是带窗口 |
| 标的目录 | 期货 **1,139** 条、期权 **24,402** 条（分页 `limit=10000` 取尽；单页 `limit` 上限 10,000） | 解析器按清单精确匹配 |
| 空数据两种形态 | 目录内标的但窗口内无成交 → `data.item` 为**空数组**；标的根本不在目录（如已到期 `CU2609.SHF`）→ `FUYAO_EMPTY` | provider 侧 `THS_EMPTY_RESPONSE` 失败关闭 |
| `turnover` 空值 | 商品指数类标的（`850002.TI`）上游把 `turnover` 回成 `null`，而契约 `Bar.amount` 非空 | `fetch_period_daily_bars` 失败关闭（不归零、不补估），单测 `test_period_bars_reject_a_null_turnover` |

期权标的代码有两种形态，决定了 `option_daily` 不做裸码解析：商品/股指期权的
`thscode` 就是可读合约码加后缀（`L2611-P-7200.DCE`、`CU2611P102000.SHF`、
`MO2612-C-7600.CFE`），而 **ETF 期权的 `thscode` 是不透明序号**——上交所是 8 位期权代码
（`10011514.SH`），深交所是 `9000xxxx.SZ`，可读码只出现在 `ticker` 字段
（`159922P2612M003800`）。目录 24,402 条且分页有上限，靠 `ticker` 反查要翻完全部页，
故 `resolve_option_code` 要求调用方给带后缀代码并原样透传，缺后缀即
`THS_OPTION_SYMBOL_QUALIFIED_REQUIRED` 失败关闭。

## 3. 期货/期权的 symbol 键为什么保留交易所后缀

`stock_daily`/`index_daily` 的映射用 `normalize: plain` 剥掉后缀（`000001.SH → 000001`）。
期货与期权**不剥**：`RB2610` 在上期所/大商所/郑商所各自语义下才唯一，且交割后同一裸码
会被 recycled；剥后缀会造出无意义且可碰撞的主键。故 `ths.yaml` 里两域的
`symbol: {from: thscode}` 不带 `normalize`，主键 `(symbol, trade_date)`。价格无单位
换算（不设 `scale`），`amount` 取 `turnover`。

## 4. 真机对照（跨 vendor，非同源自证）

```
python scripts/ops/ths_futures_option_cross_check.py --start 2026-06-01 --end 2026-08-31
```

对照面：`get_registry().resolve_domain(domain, source="ths").fetch(...)`（调用方真实路由
路径）↔ sina `futures_zh_daily_sina` / `option_commodity_hist_sina` /
`option_cffex_zz1000_daily_sina` / `option_sse_daily_sina`（另一 vendor、另一聚合链）。
7 条腿覆盖 4 个交易所族：上期所/中金所/大商所期货 + 大商所/上期所商品期权 + 中金所股指
期权 + 上交所 ETF 期权。`RB2610` 故意传**裸码**，顺带证明清单解析器。

| 域 | ths 代码 | sina 代码 | 天数 | 重叠 | OHLC 最大相对偏差 | 成交量最大相对偏差 | 结论 |
|---|---|---|---|---|---|---|---|
| futures_daily | `RB2610` | `RB2610` | 65 | 1.000 | **0** | 4.65e-04 | PASS |
| futures_daily | `CU2610.SHF` | `CU2610` | 65 | 1.000 | **0** | 7.71e-05 | PASS |
| futures_daily | `IF2612.CFE` | `IF2612` | 65 | 1.000 | **0** | 0 | PASS |
| option_daily | `L2611-P-7200.DCE` | `L2611P7200` | 15 | 1.000 | **0** | 0 | PASS |
| option_daily | `CU2611P102000.SHF` | `CU2611P102000` | 48 | 1.000 | **0** | 2.50e-01 | PASS（成交量观测） |
| option_daily | `MO2612-C-7600.CFE` | `mo2612C7600` | 65 | 1.000 | **0** | 1.27e-01 | PASS（成交量观测） |
| option_daily | `10011514.SH` | `10011514` | 65 | 1.000 | **0** | n/a | PASS |

**七条腿的价格字段（开/高/低/收）相对偏差全部为 0**，重叠 7/7 腿均为 1.000，
即两家在 388 个「标的 × 交易日」观测点上四个价格逐位相同——这同时证明了标的身份
（代码映射对了）与数值口径（没有复权、没有单位换算）。

成交量分三档处置，不是一刀切放宽阈值：

- **期货腿为硬判据**（上界 `VOLUME_TOLERANCE=1e-3`）：`IF2612` 逐日全等，`CU2610` 最大
  7.71e-05（10 手/13 万手），`RB2610` 最大 4.65e-04（278 手/59.8 万手）且比值恒 ≥1。
  一手之差在几千手的合约上就是 2.4e-05，所以 1e-6 那种「与价格同量级」的界在这里
  是错的标准；实测把界定在 1e-3 并**在报告里打印比值区间**，越界即 FAIL。
- **商品/股指期权腿只观测**：`CU2611P102000` 最大 2.50e-01、`MO2612-C-7600` 最大
  1.27e-01，全部是**单日几手**之差（5 vs 4、71 vs 63）且比值恒 ≥1——薄合约上一手
  就是几十个百分点，属两侧成交聚合/交易所修正范围差异，不是程序换算错误（同窗口
  四个价格字段逐日全等）。这类腿在报告里标 `PASS (volume observed)` 并逐条打印偏差行。
- **上交所 ETF 期权腿不对照成交量**：sina 该路由第 6 列与 ths 成交量**无恒定比例**
  （实测 ths/sina 在 0.0002 .. 0.0207 之间无规律漂移；既不是 1 张=10,000 份的台阶，
  也不是标的 ETF 自身的成交量），列语义不可确认，故该腿 fields 收窄到 OHLC 并在
  报告 §成交量口径说明里点名。ths 侧自洽：`turnover / (volume × close)` 中位数
  10,028 ≈ 该合约 10,000 份/张的乘数，即 `volume` 单位是**张**、`amount` 是元。

## 5. ETF 腿为什么本轮不接（D10 冲突，已量化）

`/api/fund/market/historical` 忽略 `adjust` 参数且返回**前复权**序列。真机
2021-10-08 .. 2026-09-24（1,207 个重叠交易日，未传 `adjust`）与 sina 不复权收盘比：

| 标的 | 比值区间 | 逐年比值（min..max） |
|---|---|---|
| `510300.SH` | 0.9035 .. 1.0000 | 2021 0.9143..0.9188 / 2022 0.9035..0.9286 / 2023 0.9129..0.9334 / 2024 0.9149..0.9522 / 2025 0.9429..0.9747 / 2026 0.9746..1.0000 |
| `510050.SH` | 0.9096 .. 1.0000 | 2021 0.9214..0.9371 → 2026 恒 1.0000 |
| `159915.SZ` | 0.9633 .. 1.0000 | 仅两个取值，2024 出现一处 0.9633 台阶（该年分红） |

比值恒 ≤1、**右端收敛到 1、按除息日阶跃**——正是前复权的定义特征。D10 规定库内只存
不复权、复权序列在查询期由 `Bar + 复权因子` 合成；把这条序列直接入 `dwd_stock_daily`
同构的 `fund_etf_daily` 表，等于把一次不可逆的复权写进底表。本轮因此**不接**该腿，
`openbb_map.yaml` 里登记为 `status: pending` 并把上述测量写进注释。要转正需要上游
给出不复权通道或给出逐日复权因子，二者当前都没有。

ETF 日 K 另有独立的窗口上限：实测**恰好 5.00 自然年**（起日 2021-09-25/2021-09-24 可，
2021-09-20 被拒），而报错文案写「单次不超过 10 年」——接入时不能照抄文案。

## 6. 注册事实变化（复算口径）

| 口径 | 本轮前 | 本轮后 |
|---|---|---|
| 对照表条目 / `ours` 行 | 14 / 28 | 14 / **31** |
| 已启用能力 `(provider, domain)` | 25 | **27** |
| 其中 `verified` | 11 | **13** |
| 对照表覆盖数据域 | 15 | **15**（新增 `futures_daily`/`option_daily` 的 ths 腿，两域早已在册） |
| ths fetchers | 3 | **5**（+`futures_daily`、+`option_daily`） |
| fuyao 端点映射：`implemented` / `available` | 9 / 82 | **11 / 80**（另 `client_only 6` / `planned 5`，共 102 条） |
| `ours` 行状态分布 | verified 11 / registered 14 / pending 3 | **verified 13 / registered 14 / pending 4** |

路由行为变化：`option_daily` 从「显式 `source=akshare` 才可路由」变为**参与
`source=auto`**（命中 ths 权威源），`tests/test_p1_daily_fetchers.py` 的
`_AUTO_ROUTABLE_DOMAINS` 随之以 `{index_daily, option_daily}` 分叉：这两域断言 auto
落在 ths，其余 P1 日线域（`bond_daily`/`fund_etf_daily` 等）仍断言 auto 抛
`LookupError`。`futures_daily` 不在该参数化用例里，其转正由
`tests/test_ths_provider.py` 新增的
`test_every_verified_domain_auto_routes_to_ths` 覆盖——逐条 `FETCHERS` 断言 auto 路由
落在 ths，把「verified 即进 auto」这条不变式钉住，后续加腿不必再改测试。

## 7. 边界与待办

- 期权目录 24,402 条且 `limit` 单页封顶 10,000，**未按 `ticker` 建裸码→序号索引**：
  ETF 期权只能用带后缀的 `thscode` 直取。若后续要按可读合约码查询，需另建一张
  期权合约维表（新数据域 + 契约模型），另轮处理。
- 期货快照、期权链（greek/行权价列表）、期货持仓排名等仍是 `available`。
- 商品指数类标的（`.TI`）因 `turnover: null` 无法进 `Bar`，本轮按失败关闭处理；若确需
  指数序列，应新建域而不是复用 `Bar` 的 `amount` 非空约束。
- 落库链路（`ods_*_ths` → `dwd_*`）本轮同样只以**离线映射回环**
  （`tests/test_data_mapping.py`）证明，未向生产仓库写入期货/期权数据。

## 8. 门禁口径修正：覆盖率阈值原来会被四舍五入放行

C6 归档门禁日志时发现的缺陷，与期货/期权腿无关但影响**所有**历史「门禁全绿」结论：

```
FAIL Required test coverage of 84% not reached. Total coverage: 83.96%
======================= 2344 passed, 5 skipped in 45.62s =======================
===== gate: frontend-lint =====        ← 继续往下跑
===== gate: PASSED =====               ← 退出码 0
```

原因是 coverage.py 判定阈值前先把总量按 `precision` 四舍五入
（`coverage/results.py::should_fail_under` → `round(total, precision) < fail_under`），
而 `precision` 默认 0：`round(83.96) = 84`，`84 < 84` 为假 → 放行；`pytest-cov` 的终端
摘要（`plugin.py::pytest_terminal_summary`）用**未取整**的原值打印，于是出现
「同一行既写 FAIL 又退出 0」。取 `--cov-fail-under=85`（实际 84.36%）时
`round(84.36)=84 < 85` 仍会失败，所以这个洞**只在阈值落在整数带上时**表现，
常规回归测试测不到——本次是靠逐行读归档日志才发现的。

处置（不改阈值、不降标准）：

| 变更 | 作用 |
|---|---|
| `pyproject.toml` `[tool.coverage.report] precision = 2` | `pytest-cov` 未显式传 `--cov-precision` 时回落到该配置，判定改为 `round(total, 2) < 84` → 83.96% 现在**真的**会让 `make gate` 非零退出，打印的 FAIL/PASSED 与退出码一致 |
| `tests/test_quality_gates.py` | 门禁自洽回归：从 `pyproject.toml` 读 `fail_under`/`precision`，断言 `should_fail_under(83.96, 84, precision) is True` 且 `(84.36, …) is False`；将来有人删掉 `precision` 即红 |
| 覆盖率补齐 | C6 新增的 ths 适配层原来只有 56–74%：`tests/test_ths_provider.py` 补 `client()` 生命周期、期货清单解析（含裸码/歧义/空目录三条 fail-closed）、期权「裸码即拒」、两条期货/期权 fetch 阶段（`time_period=day_1`、`timestamp` 日期字段、按交易日排序）与空响应关闭。全库覆盖率 **84.36%**（阈值 84%，两位小数下真实达标） |

复算命令：

```
python -m pytest tests -n 8 -m "not e2e" --cov-branch --cov-report=term \
    --cov-fail-under=85 -q ; echo $?      # 1，且打印 FAIL 84.35%
python -m pytest tests -n 8 -m "not e2e" --cov-branch --cov-report=term \
    --cov-fail-under=84 -q ; echo $?      # 0，且打印 reached
```

**口径影响提示**：C1/C4/C5 各轮记录的 `gate.txt` 中「覆盖率 84.x%，门禁 PASSED」的
结论没有造假（当轮实测值确实 ≥84），但在 `precision = 0` 时代，任何把总量压进
**[83.5, 84.0)** 区间的回归都会被静默放行（`round(83.9)=84`），而终端仍会打印 FAIL 行。
本轮之后该窗口不存在：判定按两位小数，`[83.5, 84.0)` 一律非零退出。

## 9. 门禁复跑

```
make gate          # EXIT=0，完整未裁剪日志 gate.txt（同目录，5,354 行）
```

| 项 | 本轮实测 |
|---|---|
| 后端用例 | **2,361 passed / 5 skipped**（47.78s，`-n 8 -m "not e2e"`） |
| 覆盖率 | **84.33%**（阈值 84%，两位小数下达标；`precision=2` 起效后报告本身即判定值） |
| C6 新增适配层 | `futures_daily`/`option_daily`/`index_daily`/`stock_daily` **100%**、`_client.py` 97.44%（余 1 行为 `TYPE_CHECKING` 导入）、`ths/` 层合计 **90.37%** |
| 静态检查 | a2-check（ruff + ruff format --check + mypy + bandit）通过；quality-ratchet、public-api-quality、brand/zero-dep/js-points 全绿 |
| 前端 | eslint 0 errors（48 条存量 warning 待清）+ `vue-tsc --noEmit` + vitest **8 files / 79 tests** |
| 运行环境 | 分支 `dev`、HEAD `d63a774`（C5 提交）、22 个待提交文件、Python 3.13.5、2026-09-24 21:23 CST |

`opendata_fuyao/` 有 40+ 单测（`tests/test_fuyao_endpoints.py` 等）但**不在覆盖率统计
范围内**——`[tool.coverage.run] source = ["opendata"]` 只圈了中台包，AC-17 的
「`opendata_fuyao` ≥90%」这条目前无法机算。本轮未扩大 `source`（会把全库总量拉下
一个台阶，需要单独一轮补测），只在验收文档 AC-17 里把该口径标为**未机算**。

> **C7 轮更正（2026-09-24）**：①上面的归因不完整——真正决定统计范围的是
> `pytest.ini` `addopts` 里的 `--cov=opendata`（命令行优先于 `[tool.coverage.run] source`），
> C7 加 `--cov=opendata_fuyao` 后该包 9 个文件全部入统计、**逐文件 ≥90%、包小计 94.65%**，
> 「未机算」这条待办已消。②本表「`ths/` 层合计 90.37%」在同一份 `.coverage` 上复算为
> **96.84%**，90.37% 复现不出来（疑为补测过程中的中间数据）。细节与复算命令见
> `docs/evidence/C7/README.md` §2、§3。


