# C22 轮：宏观域权威序 + 跨市场 auto 路由收口

日期：2026-09-25｜分支：`dev`｜前置：C21（`567ec21`）｜对应任务：#28｜判据面：AC-3 / AC-10
C21 §7-3 把"宏观三域缺权威序 ⇒ auto 赢家是文件收集顺序的巧合"复现并登记为遗留；本轮就是收那条：
**两层修法各管一件事**——`registry` 补上被丢掉的 `market` 维度（不再用"欧元区的答案"回"美国的问题"），
`authority.json` 补上四条宏观行（赢家由权威表给出，且用变异测试证明这几行确实在吃劲）。
排序依据本身是**量出来的**（失业率）或**明确声明的**（CPI），两者都写进了 JSON 与本文，不留"看起来合理"的空口。

---

## 1. 缺陷一：`resolve_domain` 把 `market` 维度整层丢掉

`opendata/data/registry.py` 的 `resolve_domain(domain, source=...)` 是目录侧（FR-17）唯一的取数入口，
它历史上只把 `domain` 换成 `asset_class` 再转给 `resolve()`，**`period` 与 `market` 两个维度一个都不传**：

```python
return self.resolve(fetcher.capability.asset_class, domain, source=source)   # 修复前
```

而 `economy_cpi` 有两条已转正的腿服务**不同市场**（实测能力表）：

| 域 | 腿 | period | market | verified |
|----|----|--------|--------|----------|
| `economy_cpi` | ecb | `1M` | `eu` | ✅ |
| `economy_cpi` | oecd | `1M` | `eu` | ✅ |
| `economy_cpi` | imf | `1A` | `global` | ✅ |
| `economy_cpi` | fred | `1M` | `us` | ❌（必配 Key，不参与 auto） |
| `economy_gdp` | ecb | `1Q` | `eu` | ✅ |
| `economy_gdp` | imf | `1A` | `global` | ✅ |
| `economy_unemployment` | imf / oecd | `1A` | `global` | ✅✅ |

⇒ `resolve_domain("economy_cpi")` 不带 market 时，`ecb`(eu) 与 `imf`(global) 都是合法候选，
auto 返回**注册顺序里排在前面的那一个**。一个"美国 CPI"的问题于是可能被**欧元区 HICP** 回答，
而且日志、返回值、契约里没有任何一处提示这发生过。

修法分两半，都刻意保持 fail-closed：

1. `resolve_domain(domain, *, source="auto", period=None, market=None)` —— **把两个维度透传下去**，
   目录侧知道"要哪个区"的调用方从此有办法说出口。
2. `ProviderRegistry._reject_cross_market_auto()` —— `market is None` 且候选跨 ≥2 个市场时**抛 `LookupError`**，
   消息点名有几个市场、分别是谁、该怎么办（`pass market= (or an explicit source)`）。
   **只在 `source="auto"` 路径上生效**（显式 source 在上面就 return 了），且**不引入新契约**：
   跨市场时"选哪个区"属于调用方的意图，权威序解决不了——它只能决定"用哪个市场回答"。
   所以这里宁可挂，也不猜。

```
macro/economy_cpi is served by 2 markets (eu, global) and the request named none of them;
source='auto' does not pick across markets - pass market= (or an explicit source)
```

## 2. 缺陷二：宏观域不在 `authority.json`，auto 赢家由收集顺序决定

`authority_baseline()` 读 `opendata/data/authority.json`，未登记的域按 `_authority_rank =
(baseline_index or len(order), registration_index)` 退化到**注册顺序**。此前登记的是
"宏观/海外域在注册时各自声明权威度"——**而 provider 包刻意互不依赖、没有一个声明过**，
那句话是错的（本轮连同 docstring 一并改掉）。后果不是理论上的：

- C21 §7-3 实测复现：同一棵树只换收集顺序，
  `pytest tests/test_oecd_provider.py tests/test_ecb_provider.py tests/test_imf_provider.py -k Registration`
  → **2 failed**；换成 `ecb imf oecd` → 9 passed。这两条断言当时是**顺序巧合在撑着**。
- 本轮补 4 行：`economy_cpi: [ecb, oecd, imf]`、`economy_gdp: [ecb, imf]`、`economy_rate: [ecb]`、
  `economy_unemployment: [oecd, imf]`。`economy_rate` 与 `economy_gdp` 每个 (period, market) 组只有
  一条已转正腿，列出来是为了让覆盖守卫 `test_every_macro_domain_is_listed` **不许再静默退化**。
  同理 `economy_cpi` 里的 `imf` 只在 `(1A, global)` 独占组、永远不参与 `ecb`/`oecd` 的竞争——
  **一行只排"共用同一个 (period, market) 的那几条腿"**，这一点写进了 JSON 的 `_comment`。

### 这几行的先后是按什么定的（不是按"看着顺眼"）

| 组 | 序 | 依据 | 实测数字 |
|----|----|------|---------|
| `economy_unemployment` / `1A` / `global` | **oecd → imf** | 量出来的：把窗口右端推到 2031，看谁会交预测值 | IMF **17 行、6 行晚于最后完整自然年 2025**（2026-2030 混进同一个 `value` 字段）；OECD **11 行、0 行** ⇒ 不外溢预测值的排前面 |
| `economy_cpi` / `1M` / `eu` | **ecb → oecd** | 声明的，**不是量出来的**：这个组不存在 value 级裁判 | ECB `ICP/M.U2.N.000000.4.ANR` 120 行均值 **2.394**；OECD `GBR.M.HICP…G1` 120 行均值 **0.223**；按日期对齐 120 点最大偏差 **10.181** ⇒ 一个是欧元区总量、一个是英国国别，量纲/区域都不同 ⇒ 按"该区域的发布方口径"声明 ECB 在前 |
| `economy_gdp` / `economy_rate` | ecb 优先 / 单腿 | 每个组只有一条腿，列出仅为覆盖守卫 | 巡检实测 ecb gdp 8 行、rate 14 行 |

**声明式判据要写清自己不是量出来的**——否则下一轮会把"ecb 在 CPI 排第一"误读成一个实测结论。

## 3. 新证据形式：变异测试证明这几行是"决定者"而非"冗余"

前两轮（C20/C21）的守卫只证明"顺序换了答案不变"，那还不够——如果 `authority.json` 与注册顺序
恰好同向，删掉表也照样绿。本轮直接**摘行**（`domains` 里 pop 掉 4 条 `economy_*`）再跑离线守卫：

- 摘行 ⇒ **7 failed, 65 passed, 6 deselected**（`MUTANT_EXIT=1`），其中
  `economy_unemployment/1A/global` 组的答案**由 `oecd` 翻成 `imf`**（`assert 'imf' == 'oecd'`），
  `economy_cpi/1M/eu` 组跨 4 种注册顺序给出 `{'ecb', 'oecd'}` 两个赢家；
- 装回 ⇒ **72 passed, 6 deselected**（`RESTORED_EXIT=0`），且 `authority.json` 与变异前
  **逐字节相同**（`shasum -a 256 -c` → `OK`，sha256 `8b8d109a…a72c`）。
- 两次跑都带 `-m 'not e2e'` ⇒ 该证据不依赖公网；6 deselected 是各 provider 套件里的真机用例。

这份归档**写了三次**才成立，两次失败都写在文件里：第一版被 `tail -12` 裁过（属"把证据裁短"）；
第二版把 pytest 文件列表放进 shell 变量，zsh 不对无引号变量分词 ⇒ 两次跑都是 usage error
（EXIT=4、`collected 0 items`），归档记录的是"一次没跑起来的命令"。另有第一次变异跑漏了
`-m 'not e2e'`，2 条 `TestLive` 因与并行的巡检抢公网而 `httpx.ReadTimeout`——该次读数已从所有结论里排除。

## 4. 影响面：守卫今天不改变任何一条生产路径（逐个调用点核对，不是推断）

| 调用点 | 传什么 | 会不会碰到守卫 |
|--------|--------|--------------|
| `opendata/pipeline/patrol.py:266`（巡检主循环） | `resolve(..., source=capability.source)` 显式源 | 否——显式源在此之前已 return |
| `opendata/pipeline/patrol.py:474`（期权探针取目录腿） | `resolve(..., source=OPTION_PROBE_CATALOG[1])` 显式源 | 否，同上 |
| `opendata/pipeline/jobs.py:144 resolve_fetcher` | `source` 是**必填位置参数**、无 `auto` 默认（调度批次"不许换腿"），`:162` 原样传下去 | 否 |
| `opendata/services/data_acquisition.py:193` | `resolve_domain(interface.name)` = auto，**生产代码里唯一的市场盲调用点**（全仓 grep `resolve_domain(` 复核） | 名称来自 `DataInterface` 表（SQLAlchemy 模型，`opendata/models/interface.py:64`），**目录里没有任何 `economy_*` 接口**；万一有了，`LookupError` 被同函数 `except` 接住 ⇒ 回落 legacy akshare 路径，**不会拿一个随机市场作答** |
| REST（`api/data.py`、`api/data_query.py`） | 只调 `capabilities()`，不路由 | 否 |
| `scripts/ops/*_cross_check.py`（14 处对照取腿） | 一律 `source=` 显式 | 否 |

⇒ 守卫是**把一条会答错的路变成会挂的路**，今天没有调用方走它。本轮实测把两层效果分开计：

| 效果 | 实测涉及面（**从注册表实枚举**，不是推断） |
|------|------------------------------|
| 市场盲 auto 会挂（跨多市场） | 只有 `economy_cpi`（`eu` vs `global`）、`economy_gdp`（`eu` vs `global`）两个域；其余域候选市场唯一 ⇒ `len(markets) < 2` 直接返回 |
| 权威序真正决定赢家（同一 `(period, market)` 组内 ≥2 条腿） | 只有两组：`economy_cpi/1M/eu` = `[ecb, oecd]`、`economy_unemployment/1A/global` = `[imf, oecd]`——正是 §3 变异测试里翻掉的那两组 |

`test_ambiguity_detection_is_not_vacuous` 把第二行钉成断言（对 `test_macro_routing.py` 那 11 条宏观腿
枚举出的歧义组必须恰好等于这两组），否则"顺序换了答案不变"可以靠"根本只有一个候选"作弊通过。

**覆盖面的边界要说准，不能写成"全库登记完毕"**：`authority.json` 现有 **17 行**，
注册表的 18 个域里仍 **3 个不在表里**——`bond_daily`（只有 akshare 一条 `verified=false` 腿，
本来就没有 auto 候选）、`fund_action` 与 `stock_daily_overseas`（各只有一条已转正腿，顺序无从改变答案）；
表里另有 2 行（`stock_adjust`、`futures_fundamentals`）指向**当前没有腿服务**的域。
⇒ 本轮实际把住的是"§3 那两类真会翻面的组必须有权威行"。**守卫自身的范围也要写清**：
`TestBaselineCoversMacro` 只遍历 `test_macro_routing.py` 里那张 **11 条腿的硬编码清单**，
它保证的是"这 4 个宏观域不许退回收集顺序"，**不会**自动发现"新接入的宏观域没进表"——
后者目前靠人工（本轮 `economy_rate` 是自愿进表的，不是被迫的）。等 `bond_daily` 接第二条腿时，
同一行必须补进 `authority.json`，这一条登记在 §8 而不是假装已有用例守着。

## 5. 证据清单

| 文件 | 内容 | 判据 / 退出码（读自文件内部） |
|------|------|--------------------------|
| `macro-authority-cross-check.txt`（+ `scripts/ops/macro_authority_cross_check.py`） | 真机跨 vendor 对照，**只读**、不写数仓、不打印任何密钥：J1 失业率定义一致性（3 参考区域，按日期对齐）、J2 同一未来窗口的发布形态、J3 CPI 两条 1M 腿的量纲可比性 | USA 10 点 / **0.097pp**、JPN 10 点 / **0.174pp**、DEU 10 点 / **0.421pp**（超容差年份 2015 `4.4 vs 4.721`、2016 `3.9 vs 4.213`、2017 `3.5 vs 3.829`、2020 `3.6 vs 4.021`），6 次取数 `attempts` 全为 1（无一需要重试）；判定 **PASS×4 + WARN×1**（DEU 超容差按"读出来的差异"报 WARN 并登记，容差 0.3pp **没有放宽**），`EXIT=0` / `PROBE_EXIT=0` |
| `authority-mutation.txt` | §3 的变异测试：摘行 / 跑守卫 / 逐字节还原 / 再跑，四段**全量未裁剪**，含两次作废版本的自我更正 | `MUTATE_EXIT=0`、`MUTANT_EXIT=1`（7 failed）、`RESTORE_EXIT=0` + `shasum` `OK`、`RESTORED_EXIT=0`（72 passed） |
| `live-patrol.txt` | 守卫与权威序落地后的真机全量巡检（16:39 `+03:00`），20 条 verified 腿逐条 `[ok]` + 凭证配置面 | `失败 0 项；抖动 0 项；必配 Key 缺失 1 项`（fred），`PATROL_EXIT=0`；行数与 C21 两跑逐条相同（`ecb 12/8/14`、`imf 10/10/10`、`oecd 24/2`、`ths instrument 5578`…）⇒ 本轮没有改动任何一条腿的可路由性 |
| `gate.txt` | `make gate` 完整未裁剪输出（5,887 行；15 行运行环境头含 `本机时区偏移=+0300`、`branch=dev HEAD=567ec21（本轮改动未提交）` 与 9 条脏文件清单） | `GATE_EXIT=0`，`2599 passed, 5 skipped in 46.64s`，coverage `86.28%`（阈值 84%），`A2 files: 256`，public API `444/444` 双 100%，前端 8 文件 / 79 测试 |

**归档覆盖范围（与 C21 的差异要写清）**：C21 之所以重跑门禁，是因为它的探针脚本 `update_time-tz-pin.py`
落在 a2-check 扫面内、且晚于第一份归档。本轮晚于 `gate.txt` 落地的只有本 README 与验收文档——
`scripts/quality/a2_check.py` 只收 `.py`，`check_brand.py` 把 `docs/evidence` 整目录列进
`IGNORED_PATHS`（`scripts/quality/check_brand.py:72`），两者都不扫 markdown ⇒ 归档已覆盖最终代码树，
**没有靠重跑去找一个不同的读数**。

## 6. 测试与门禁数字

- **`tests/test_macro_routing.py` 新增 13 条**（11 个用例、跨市场组按 `(domain, period, market)` 参数化展开）：
  `TestMarketDimension`（市场盲 auto 必须挂且消息点名 `eu`+`global`／给出 market 就有答案／
  `resolve_domain ≡ resolve` 的透传等价／`economy_rate` 单腿域仍可路由／美国 CPI 只有未验证腿 ⇒
  报 `no verified capability` 而不是退到欧元区／反空转：歧义组枚举必须恰好等于 §4 那两组）、
  `TestAuthorityDecidesNotRegistrationOrder`（每组 4 种注册顺序 ⇒ 赢家唯一，且赢家 == 权威表里第一个列出的源；
  另 1 条把首选腿标成不可达 ⇒ 接上的是**另一条腿**，`mark_available` 复位后首选重新胜出。
  这条只断言"不是同一条"、没有直接断言"是 `imf`"——它证明排序在降级后仍生效，
  第二名的具体人选由前面那条参数化用例给出）、
  `TestBaselineCoversMacro`（每个宏观域都在表里、表里列的源都真服务该域）。
- **4 个 provider 套件的注册用例改为显式 `period`/`market`**（合计 +4 条：`ecb` +2、`imf` +1、`fred` +1、`oecd` 净 0）：
  原来那两条顺序巧合断言（`test_verified_capability_is_auto_routed`、`test_auto_routing_prefers_a_verified_source`）
  换成有主语的用例——`test_the_euro_area_publisher_answers_for_the_eu_cpi_leg`、
  `test_quarterly_euro_area_gdp_routes_to_ecb`、`test_auto_routing_reaches_the_global_leg_when_the_market_is_named`、
  `test_eu_cpi_auto_routes_to_ecb_and_degrades_to_oecd`、`test_global_annual_unemployment_routes_to_oecd`，
  外加 3 条 `test_market_blind_auto_fails_closed` 与 1 条
  `test_explicit_source_reaches_the_unverified_leg`（fred：显式源照样能取，不参与 auto 不等于取不到）。
  各套件 fixture 统一改为调用 `register_providers()`，**per-file 隔离下也不再依赖收集顺序**。
- 门禁：`2582 → 2599 passed`（+17 = 13 + 2 + 1 + 1，与两份 gate.txt 的逐文件 PASSED 计数对账一致），
  `5 skipped` 不变；coverage `TOTAL 10603/1282/2572/307 = 86.27% → 10612/1283/2576/307 = 86.28%`
  ——**部分分支 307 一字未动**，未覆行 +1 只来自 `if TYPE_CHECKING` 里新增的一行导入。
- `opendata/data/registry.py` **77 语句/4 未覆/28 分支/3 部分 = 93.33% → 86/5/32/3 = 93.22%**：
  分支 +4（`market is None` 与 `len(markets) < 2` 两个新 `if`）全部被覆到，部分分支仍是前轮那 3 条；
  未覆区间 `24-27`（TYPE_CHECKING 导入块，与 C8 起全库同口径）+ `57, 59`
  （前轮既有的 malformed-authority `raise`，本轮未触碰）。
- a2-check 扫面 `254 → 256 文件`（新增 `scripts/ops/macro_authority_cross_check.py`、`tests/test_macro_routing.py`），
  ruff / format / bandit 三件 `ok`；mypy 按既有分层口径**只对非 `tests/` 强制**
  （`scripts/quality/a2_check.py:260-262`），本轮额外把 `registry.py`、
  `scripts/ops/macro_authority_cross_check.py`、`tests/test_macro_routing.py` 三个文件
  **逐个按 `--strict` 跑通**（4 个 provider 套件的既有用例体属 C 层渐进类型范围，本轮未扩大改动面）；
  `registry.py` 的 `resolve_domain` 新签名与守卫都补齐了 docstring/类型标注
  （A2 public callables 计数不变 ⇒ 新代码只在私有方法与测试面）。
- 棘轮五项**只降不升**：`ruff_selfdev 274`（快照 277，improved）、`mypy_selfdev 21`、
  `bandit_selfdev 4`、`ruff_ported 2144`（快照 2145，improved）、`direct_http_ported 1044`（快照 1045，improved）。

## 7. 判据未放宽的四处声明

1. **DEU 的 0.421pp 没有靠改容差消化**：容差 0.3pp 是本轮在脚本里声明的常量（`DEFINITION_TOLERANCE_PP`），
   超界区域按"取数成功但定义不可互换"报 **WARN 并登记为未收口项**，与"取不到数"（UNVERIFIABLE，
   会让脚本 `exit 1`）严格分开——既没把差异抹成 PASS，也没把差异夸成取数故障。
2. **没有为了让守卫好看而放宽 auto**：跨市场是 fail-closed（抛错）而不是"默认取权威第一个"。
   后者能让老调用点继续跑，但正是那个行为会把欧元区的答案发给问美国的调用方。
3. **变异测试的还原是逐字节校验的**（sha256 + `shasum -c`），不是"看起来改回去了"；
   两份作废归档的失败原因写在文件里而不是删掉。
4. **J3 的排序依据如实标成"声明而非实测"**，并在 JSON `_comment` 里写明"不存在 value 级裁判"，
   防止后续轮次把它当成已验证的等价性。

## 8. 遗留（明确不做 / 做不了，含原因）

1. **IMF 与 OECD 的失业率在 DEU 上不可互换**（0.421pp > 0.3pp；USA 0.097pp、JPN 0.174pp 一致）：
   这是**定义差**不是取数差，权威序不改变它。需要按区域分别裁决时，得先有"哪一条是 DEU 的口径基准"
   的外部依据（Eurostat/DESTATIS 侧），本轮不猜。
2. **OECD 的 CPI 腿标 `market=eu` 而巡检探针用的是英国序列**（`GBR.M.HICP…`）：OECD 的市场维度实际
   藏在 `series_key` 里（一个请求参数），能力表把它标成常量必然偏松。本轮**不动能力标签**——
   改了要重做跨 vendor 判据；先登记，等宏观落库口径（AC-19 / 任务 #28 之后的落地面）一并处理。
3. **`docs/evidence/C21/flaky-retry-attribution.py:79` 在新守卫下会挂**（它调的是市场盲的
   `resolve("macro", "economy_cpi", source="auto")`）。**不改写前轮证据**：C21 的归档记录的是
   当时生产路径的行为，重写它等于篡改。后续如需重跑 C21 探针，须在其复现命令里显式补 `market=`。
4. **宏观数据没有落库面**：本轮收口的是路由与权威序（AC-3），`dwd_economy_*` 一类表未建、未写，
   与 AC-15（旧表只读）、AC-19（字段级巡检判据）一样等用户确认后再动。
5. 沿前轮未收口的：`push2delay.eastmoney.com` DNS 仍解析失败 ⇒ 任务 #14 的 4 个 em 用例继续 `pending`；
   搬运层另两处"按本机时区渲染"（`akshare/stock/stock_xq.py:27`、`stock_feature/stock_info.py:167,200`）
   仍不在任何 fetcher 调用面上；akshare 10 条腿 `verified=false` 的逐域复核（AC-10）；
   `dwd_fund_etf_daily` / `dwd_fund_action` / `dwd_instrument` / `dwd_trading_calendar` 落库需明确确认。
