# C16 轮：修巡检探针参数面（AC-4 / AC-19）

> C15 轮留档时顺手量出来的一件事：**每跑一轮每日健康巡检，巡检本身会把 12 条已经验证过的腿标成不可用**。
> 本轮修的就是这一件事——只修探针怎么发问，不新增能力、不改任何 provider 的取数逻辑。全程数仓**只读**。

## 1. 缺陷是什么，为什么不是「报个错」那么简单

- 机制（C15 留档 `../C15/patrol-probe-audit.txt`）：`patrol()` 取参是 `PROBE_PARAMS.get(domain, {})`，
  **键只有域名、不含 source**。于是两类腿必抛：
  1. 7 条 ths 腿所在的域根本不在表里 ⇒ 空参数 ⇒ `symbol` / `asset_type` 必填字段缺失；
  2. `economy_*` 一组喂的是 fred 口径的 `series_id`，而 imf 要 `indicator`、oecd 要 `series_key`
     ⇒ 同域不同源、字段名不同，一条参数喂不了三家。
- 后果不是「日志里多一行」：探测抛错即 `mark_unavailable(fetcher)`，而 `source="auto"` 恰好跳过不可用的
  fetcher ⇒ **巡检在削弱它刚验过的路由**（进程内、非持久，但一旦巡检排在增量任务之前就是当天降级路由）；
  同时 `scripts/ops/health_patrol.py` 的「失败累计即非零退出」天天报 12 条**假故障**，真故障被埋在里面。
- 基线：注册表 19 条 verified 能力，探针可过的 **7** 条，必抛 **12** 条。本轮后：**19 / 19**。

## 2. 改动面（一处取参规则 + 三态失败语义 + 两条守卫）

| 改动 | 内容 | 为什么这样改 | 落点 |
| --- | --- | --- | --- |
| 参数键 | `domain` → `(domain, source)`，17 条静态 | 同域不同源的字段名本就不同，按域取参是把三家口径混成一家 | `PROBE_PARAMS` |
| 会腐烂的两条腿 | 2 条按**探测日**解析（`PROBE_RESOLVERS`） | 期货/期权的合约月份会滚掉，钉死标的的探针跟被替换掉的那批一样会烂 | `rolling_futures_params` / `rolling_option_params` |
| 缺参数 | 新增 `PatrolProbeConfigError`，报告但不 `mark_unavailable` | 「patrol 没配探针」是 patrol 自己的缺口，标源不可用是**错误归因**，且会污染路由 | `patrol()` 的 except 分支 |
| 空结果 | `_probe_fetcher` 返回行数，0 行即 `PatrolProbeError` | 每条探针点名的都是在该窗口内已发数的标的，0 行意味着源不答而不是「没话说」 | `_probe_fetcher` |
| 可观测 | `PatrolResult.rows` 新字段；HTTP `/health/patrol` 与命令行逐条打 `N rows, Xms` | 只报 ok/fail 看不出「过是过了，但只回了 2 行」 | `api/pipeline.py`、`scripts/ops/health_patrol.py` |
| 超时 | 10s → 30s | 实测最慢的 verified 源是 IMF（两轮 8.7–11.6s），10s 天花板把健康腿报成挂 | `PROBE_TIMEOUT` |
| 守卫 | 两条按**注册表参数化**的用例（见 §6） | 上一轮的病根就是「参数面和已验能力面各写各的」，只有从注册表反推才能自动跟着长 | `tests/test_patrol.py::TestProbeParamCoverage` |

## 3. 参数怎么定：三条规则，不是 19 个魔法值

- **规则一｜发过数的窗口钉死。** 只要源上已有历史数据，就用固定历史区间而不是「最近 N 天」：
  固定区间永不过期，而「最近 N 天」会把周末/节假日变成每天的假故障。日线类统一用
  `2024-09-02 ~ 2024-09-06`（周一到周五，无休市日）。
- **规则二｜标的会到期，就不能钉死标的。** 期货按日期推合约月（`CU` 铜，取 `today` **之后**第 2 个月 ⇒
  `2026-09-25` 得 `CU2611.SHF`），窗口取 21 天尾随（合约存续期远长于此，节假日周也不会 0 根）；
  期权拿不到可拼的代号（`thscode` 是不透明数字），只能从标的目录里挑「还活着的最长存续合约」。
- **规则三｜源自己有滚动覆盖窗的，就发空参。** 交易日历端点只发**近一年**（实测
  `2025-09-25 ~ 2026-09-24`），钉死 2024 年窗口必抛 `THS_CALENDAR_OUT_OF_COVERAGE`；
  该端点无必填参数 ⇒ 探针发空参数，让源自己决定它能答什么。

## 4. 19 条探针逐条留档（参数 + 定它的理由 + 真机实测）

真机一轮 `live-patrol.txt`（`2026-09-25 05:59:31 +08`，`PATROL_EXIT=0`）的行数/耗时就是本表的「实测」列；
收尾时对**全部逻辑改动落地后的代码**又独立跑了一轮（`live-patrol-final.txt`，`09:05:20 +08`），19 条**行数逐条相同**、
`失败 0 项`，只有耗时抖动（该轮 `imf/economy_cpi` 用到 11,551ms —— 正是旧 10s 天花板会误判的那一类）。

| source/domain | 探针参数 | 为什么是它 | 实测 |
| --- | --- | --- | --- |
| ecb/economy_cpi | `ICP/M.U2.N.000000.4.ANR`，2024 全年 | ECB SDW 的 HICP 年率序列码，是 ecb 侧 `series_id` 口径（不是 fred 的 `CPIAUCSL`） | 12 行 / 794ms |
| ecb/economy_gdp | `MNA/Q.N.I9…EUR.LR.N`，2023–2024 | 季度序列码，两年窗留出 8 个观测而不碰未来 | 8 行 / 782ms |
| ecb/economy_rate | `FM/B.U2.EUR.4F.KR.MRR_FR.LEV`，2022–2024 | 主再融资利率（MRR）3 年窗 | 14 行 / 774ms |
| imf/economy_cpi | `PCPIPCH` + `country=USA`，2015–2024 | imf 的字段名是 `indicator`；`PCPIPCH` 即消费者物价年通胀率 | 10 行 / 9,756ms |
| imf/economy_gdp | `NGDP_RPCH` + USA | 实际 GDP 增速 | 10 行 / 10,594ms |
| imf/economy_unemployment | `LUR` + USA | 失业率；三条 imf 腿共用同一 10 年窗 ⇒ 稳定 10 个年度观测 | 10 行 / 10,607ms |
| oecd/economy_cpi | `GBR.M.HICP.CPI.PC.CP09.N.G1`，2023–2024 | oecd 的字段名是 `series_key`（区域/频率/度量都在键里），英国 HICP 月率 24 个月 | 24 行 / 67ms |
| oecd/economy_unemployment | `USA.UNE_RATE.PT_LF_SUB._T.Y15T64.UNE` | 美国青年失业率**半年**频 ⇒ 两年窗只有 2 个观测，这是源的粒度不是缺数 | 2 行 / 79ms |
| ths/stock_daily | `600519`，`2024-09-02 ~ 09-06` | 贵州茅台 + 无休市日的工作周 ⇒ 恰 5 根 | 5 行 / 1,437ms |
| ths/stock_action | `600519`，2023–2024 | 分红送股是**事件流**，两年窗才 guaranteed 有事件（一年窗会看年份脸色） | 4 行 / 1,817ms |
| ths/index_daily | `000300.SH`，同工作周 | **必须带后缀**：裸码 `000300` 在上游对多行指数（见 §8） | 5 行 / 1,124ms |
| ths/index_constituent | `000300.SH` | 沪深 300 成分，一次 300 行 ⇒ 顺带验到「整只指数」而不是抽一行 | 300 行 / 1,447ms |
| ths/financial_statement | `600519` | 该端点无日期必填项，一只票的财务摘要即 52 条指标 | 52 行 / 1,461ms |
| ths/fund_action | `510300`，2023–2025 | **裸码**入参，走 C15 建立的 ETF 目录解析路 ⇒ 探针同时覆盖「解析」这一层 | 3 行 / 1,823ms |
| ths/instrument | `asset_type=a-share` | 目录类无标的参数；A 股全目录即最大面（也是期权腿的依赖，见下一行） | 5,578 行 / 2,115ms |
| ths/futures_daily | **按日期解**：`CU2611.SHF` + 21 天尾随 | 两个月后合约月，规避已摘合约（摘了回 `FUYAO_EMPTY`） | 15 行 / 1,095ms |
| ths/option_daily | **按日期解**：从目录挑 `10011425.SH` + 21 天尾随 | 期权 `thscode` 不可拼；耗时含目录读（约 3.4s），全在超时预算内 | 15 行 / 4,445ms |
| ths/trading_calendar | 空参数 | 规则三：该端点只发近一年滚动窗 | 242 行 / 1,029ms |
| yfinance/stock_daily_overseas | `AAPL`，同工作周 | 海外日线无 key 依赖，工作周 4 根（`09-02` 是美国劳动节休市） | 4 行 / 617ms |

- 参数表与注册表面**必须相等**：17 条静态 + 2 条解析器 = 19 条，正好是注册表里 19 条 verified 能力；
  多出来的死配置（上一版的 `stock_adjust`）已被守卫用例拒绝。

## 5. 失败语义现在是三态，不是一态

| 情况 | 报告 | 是否动 routing |
| --- | --- | --- |
| 探针有行 | `[ok] … N rows, Xms` + `mark_available` | 恢复可用 |
| 源抛错 / 返回 0 行 / 超 30s | `[FAIL] …` + `mark_unavailable` | 标不可用（这正是 AC-4 要的被动信号） |
| patrol 没配探针 / 目录腿未注册 | `[FAIL] PatrolProbeConfigError: …` + `logger.error` | **不动**（缺口在 patrol，不在源） |

第三态是本轮刻意分开的：把「我没准备好」报成「源挂了」，既误伤路由又让真故障失去可信度。

## 6. 判据与留档

| 面 | 判据 | 留档 | 结果 |
| --- | --- | --- | --- |
| 离线参数面 | 19 条参数在**不连网、不连仓**下逐条过 `transform_query`，且 `probe_params` 的键集与注册表 verified 集相等 | `probe-param-audit.py` / `probe-param-audit.txt` | 19 / 19（C15 基线 7 / 19）；本轮重跑与归档 **diff 为空** |
| 真机面 | 一轮完整巡检 `failed == 0` 且逐条打行数/耗时 | `live-patrol.txt` + 改动落地后的复算轮 `live-patrol-final.txt` | 两轮均 19 条全 `[ok]`、行数逐条相同，`失败 0 项；必配 Key 缺失 1 项`，`PATROL_EXIT=0` |
| 超时的反面证据 | 10s 天花板下同一轮巡检的真实表现（保留失败那次，不覆盖） | `live-patrol-timeout10s.txt` | `imf/economy_cpi: TimeoutError` 假故障 + `失败 1 项`，`PATROL_EXIT=1` |
| 守卫用例是否真吃得住 | 变异测试：把死配置 `stock_adjust` 加回参数表 + 改掉一个 imf 字段名 | 两条守卫用例（`test_probe_tables_match_the_verified_capabilities_exactly` / `test_every_probe_validates_offline`）都按预期挂；文件按 shasum 还原为逐字节一致 | 载荷有效 |
| 单测面 | `tests/test_patrol.py` 23 例（含空结果=故障、缺参数不标故障、期货跨年滚月、期权存续期过滤、目录无标的即抛，以及 5 条「装配探针」的用例） | `tests/test_patrol.py` | 23 passed；`patrol.py` 层内实测 **94.53%**（C15 为 85.00%） |
| 质量门 | `make gate` 全绿（8 个 stage） | `gate.txt`（5,755 行完整未裁剪 + 15 行头部溯源，`GATE_EXIT` 读文件内部） | 见 §6.1 |

### 6.1 `make gate` 关键读数（完整输出在 `gate.txt`，`GATE_EXIT=0`）

| stage | 读数 |
| --- | --- |
| brand-check / zero-dep-check / js-points-check | PASS |
| a2-check | A2 文件 244 条（含本轮两个 `docs/evidence/C16/*.py`）：ruff / format / mypy / bandit 全 `ok` |
| quality-ratchet | 债务未增：`ruff_selfdev 277→274`、`ruff_ported 2145→2144`、`direct_http_ported 1045→1044`（三项改善），mypy 21、bandit 4 持平 |
| public-api-quality | 公共可调用 435 条，docstring 100%、注解 100% |
| test-cov | `2540 passed, 5 skipped in 59.55s`，分支覆盖率 **86.09%**（门槛 84%） |
| 本轮改动文件的实测覆盖 | `opendata/pipeline/patrol.py`：110 语句 / 6 未覆盖 / 18 分支 / 1 partial ⇒ **94.53%**（C15 为 85.00%；未覆盖只剩 28–34 的 `if TYPE_CHECKING` 导入块，属全库同口径而非本层缺口） |
| frontend-lint / typecheck / test | PASS：eslint **0 errors / 48 warnings**（存量 `no-floating-promises` 等）、`vue-tsc --noEmit` 通过、vitest **8 files** 全绿 |

- 归档：`docs/evidence/C16/gate.txt` 共 **5,755 行**＝**15 行跑前采集的运行环境头**（时刻、分支、`HEAD 63fd13d`、
  `git status --porcelain` 脏文件清单 8 条、Python 3.13.5 / node v25）+ **5,740 行完整未裁剪原始输出**；
  `GATE_EXIT=0` 与 `===== gate: PASSED =====` 都读自文件内部（后台任务的包装层退出码不代表门禁结果）。

- **5 条 skipped 逐条归因**（不是"未知跳过"）：`test_port_fidelity.py` 的
  `test_ported_output_matches_upstream[stock_daily_raw / stock_daily_qfq / index_daily_em / fund_etf_daily_em]`
  4 条 + `test_d10_qfq_synthesis_matches_official_series` 1 条，全部等的是同一批 em 通道夹具（AC-6 停在
  12/16 的那四条，task #14）。它们**不是**本轮新引入的跳过，也不被本轮当成绿灯证据。

## 7. 附带发现：ths 指数腿的裸码解析自 C9 起就是死的（另立 C18，本轮不修）

- 实测（`index-bare-code-defect.py` / `.txt`，真机只读）：指数目录 1,431 行里有 **206 行** `thscode` 与
  `ticker` 不等（`991001.TI` ↔ `1C0003` 上证指数(旧)、`970006.SZ` ↔ `988006` 创业板指(港币)(CNH)），
  而 `normalize_instruments` 的 `_codes_agree` 规则是**整表**拒（`ticker_code_mismatch`）⇒
  `resolve_index_code("000300" / "000905" / "932000")` 一律抛，裸码这条路从 C9 起就没通过。
- 本轮不修的理由是**不混验收面**：那是一条域实现的规则决策（哪种不等算合法——旧代码 vs 港币双列是两类问题，
  放宽规则还是只丢旧行要定），还要配一次「谁在传裸指数码」的调用方扫描；塞进 C16 就会让「探针参数面」的
  结论依赖一个未验的语义决策。本轮的处置是绕开：探针一律带后缀（`000300.SH`），并把它登记为 C18（task #24）。
- 附带约束：这条腿将来修好，`index_daily` 探针可以改回裸码以顺带覆盖解析路——但那是 C18 的验收面。

## 8. 边界：本轮**没有**做什么

- 不新增 capability、不动任何 provider 的取数/归一化逻辑；改动只落在 `opendata/pipeline/patrol.py`、
  `opendata/api/pipeline.py`（多一个 `rows` 字段）、`scripts/ops/health_patrol.py`（打印行数）与测试。
- 数仓**只读**：不建表、不写入。`dwd_fund_action` / `dwd_instrument` / `dwd_trading_calendar` 的落地仍等
  明确确认（生产写入不在任何一轮的默认授权里）。
- 健康标记仍是**进程内、非持久**；本轮不引入跨进程/跨重启的健康面。巡检也**不排班**——
  AC-4 要的是「可跑的每日巡检」这条能力，接入调度器不在本轮判据内。
- 必配 Key 仍缺 1 项：`fred_api_key` 未配置。真机留档如实打印 `fred: 缺失（必须=True）`，本轮不代为配 key，
  也没有把 fred 的 verified 能力（无）塞进探针面来让报表好看。
- 探针覆盖的是 **verified** 能力集；`verified=false` 的 P1 腿不探测（与既有语义一致），守卫用例按注册表
  现状自动跟随——以后转正一条腿，就必须同时补它的探针参数，否则守卫用例当场挂。
- 两件留在门外、本轮只如实记录的事：
  1. **AC-6 夹具的失败模式变了**：`tests/fixtures/upstream/{stock_daily_raw,stock_daily_qfq}/meta.json`
     由录制流程在每次跑测试时重写，本轮从 `HTTPError: 502 Bad Gateway` 变成
     `ConnectionError: … Failed to resolve 'push2delay.eastmoney.com'`（DNS 都拿不到）。
     这不改变 AC-6 的判定（仍 12/16、`status: pending`），只是把 task #14 的阻塞原因从「服务端 5xx」
     收紧为「该主机出网不可达」。本轮**没有**把这两个自动重写的文件塞进提交，也不借它降 AC-6 的门槛。
  2. 巡检**没有**接进调度器：AC-4 要的是「每日健康巡检」这条能力面（`patrol()` + `run_patrol()` +
     `/health/patrol` + `scripts/ops/health_patrol.py` 四把刀都在，且真机跑通），排班本身属于部署面。
