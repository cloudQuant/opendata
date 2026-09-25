# C24 轮：把「auto 会自动换退路」从注释量成判据（AC-3 / AC-10 / AC-11 / AC-17，任务 #31）

日期：2026-09-25｜分支：`dev`｜前置：C23（`cdfe100`）｜对应任务：#31｜判据面：AC-3 / AC-10 / AC-11 / AC-17

C23 证明了 `authority.json` 里**每一条腿都真的注册了能力**（7 对假腿清零）。但"注册了"和
"换得动"是两件事：AC-3 写的是"主源不可用时 auto 换下一条"，而 `resolve()` 换腿要同时过三道
条件 —— 两条腿**同形状**、退路那条**够资格**（`participates_in_auto()`）、摘掉前一条后
**真的落到**后一条。本轮把这三道逐条量，量出来的结论是：

> **全注册表 15 行里只有 2 个 (域, period, market) 形状今天能降级，且都在宏观域；
> 国内 8 行一条也做不到。** AC-3 的"自动退路"在国内核心域上目前是声明，不是行为。

因此本轮**不翻任何一条 `verified` 位**（翻它需要跨 vendor 等价读数，而两个候选的加宽读数
一个不符、一个口径未定），交付的是：两个真实缺陷的修复 + 一套把退路面量出来并钉死的守卫 +
一份逐域真机复核 + 一份证明守卫吃得住的变异记录。

---

## 1. 三道条件逐条量（`tests/test_fallback_degradation.py`，13 条，离线可跑）

| 条件 | 判据 | 今天的结果 |
| --- | --- | --- |
| ①同形状才可换 | `_matches()` 先按 `(period, market)` 过滤，再看健康位与顺序 ⇒ 一行里两条腿只有共享一个形状才可能互为退路 | 表 15 行中"行内两腿永不相见"的只有 `economy_gdp`（1Q/eu vs 1A/global，C22 有意保留，只为覆盖守卫不退化）。**本轮实测出一个非有意的那类：`stock_action`** |
| ②行首就是 auto 的行首 | 行内有合格腿时，合格腿的第一个必须等于表里第一个 | 唯一 offender = `fund_etf_daily`：C20 按实测把 akshare 排在 ths 前（ths 只答滚动 1827 天），但 akshare 腿至今未转正 ⇒ 这一行的顺序 auto 不认 |
| ③摘掉前一条必须落到后一条 | 前 k 条 `mark_unavailable` 后 `resolve(auto)` 必须给出第 k+1 条 | **只有 2 个形状做得到**：`(macro, economy_cpi, 1M, eu)` = `ecb`→`oecd`、`(macro, economy_unemployment, 1A, global)` = `oecd`→`imf`。国内 8 行 0 个 |

外加一条读数：`financial_indicator` 行内**一条合格腿都没有**（唯一来源 akshare 未转正）⇒
`source="auto"` 今天服务不了这个域。这正是任务 #2「P1 候选以 `verified=false` 注册」的本义，
钉成用例（`NO_AUTO_LEG_ROWS`）而不是留在注释里。

**国内 8 行**（表里 11 条 ≥2 源的行中 `market=cn` 的那些）：`stock_daily`、`index_daily`、
`fund_etf_daily`、`stock_action`、`financial_statement`、`index_constituent`、`futures_daily`、
`option_daily`。它们**每行都只有一条合格腿**（一律是 ths），所以缺陷不是"行没排"，是
"退路那条还没挣到被 auto 用上的资格"——这一点钉成
`test_the_domestic_gap_is_eligibility_not_a_missing_row`，防止将来有人靠**删行**让
"没有退路"这个结论变绿。

## 2. 修掉的两个真实缺陷

1. **sina 系 4 条腿对调用窗口视而不见**（新增 `_normalize.py:67 within_window()` + 4 个
   `normalize()`：`futures_daily.py:98`、`option_daily.py:98`、`bond_daily.py:96`、
   `stock_action.py:102`/`:120`）。
   sina 的日线/分红页**没有窗口参数**，整张历史一次给回，而 `normalize()` 以前照单全收：
   要 22 天答 5000 天。这不是"多给点数据"的小事 —— `Bar` 的行集就是契约返回值，
   退路答的已经不是调用方问的那个问题。现在 4 条腿（`futures_daily` / `option_daily` /
   `bond_daily` / `stock_action`）在 `normalize()` 里按窗口裁剪，两端闭区间、缺界即开放。
2. **`stock_action` 的 akshare 腿把 period 写成 `event`**，同域 ths 腿写 `1D`。因 `_matches()`
   先过滤 `(period, market)`，`authority.json` 的 `["ths", "akshare"]` 那一行排的是一条
   `resolve()` 永远取不到的退路。现对齐为 `1D`（`stock_action.py:45`，上方 40-44 行写明理由）。
   **对齐不是放宽判据**：全仓库（含 `domains.yaml` / 前端 / 映射表）没有任何调用方以
   `period="event"` 提问，本轮 grep 过；对齐后 20 条巡检腿逐条 `[ok]`、行数与 C22/C23 相同。

## 3. 真机逐域同问题同答案（`scripts/ops/akshare_fallback_cross_check.py`）

判据三分，**"量不出来"与"数据不对"严格分开**：
`PASS`（同问题同答案）/ `MISMATCH`（量出来了、答的不是同一个问题）/
`NO_READING`（网络或上游拒绝本机、或返回 0 行 ⇒ 既不算通过也不算失败）。
价格类相对容差 `5e-5`、volume 相对容差 `5e-3`，`amount=0` 视为未发布。

| 域 | 判定 | 读数 |
| --- | --- | --- |
| `stock_daily` / `index_daily` / `fund_etf_daily` | `NO_READING` | 东财 K 线通道对本机 502 / 4 个 secid 候选全失败 ⇒ 这三条腿的转正**今天量不出来**，不是量出来不合格 |
| `futures_daily` | `MISMATCH` | 15/15 键相同、OHLC 偏差 0、volume 相对差 2.9e-4；退路 `amount` 15/15 全缺（sina 不发布成交额）|
| `option_daily` | `MISMATCH` | 同价（OHLC 偏差 0），但 volume 相对差 **0.9999**（≈1e4 倍且无稳定比值：主源是合约手数、退路的量纲拼法不同）+ `amount` 全缺 |
| `stock_action` | `PASS` | 探针窗口 731 天，4/4 事件四个字段逐个相同 |
| `index_constituent` | `PASS` | 300/300 成员相同，退路**多给** `weight`（多给不改变答案，只改列形） |
| `financial_statement` | `MISMATCH` | 键集 ths=13 / akshare=83、共同 0：退路返回未筛选的全科目长表，主源返回的是选定科目 ⇒ 不是数值错，是**行的口径**不同 |

脚本把 8 个域的判定与 `CASES` 里**归档时写死的期望**逐条对比，任何一条不一致就打印 `DRIFT`
并以 `EXIT=1` 收场（首轮就是这样暴露出 `stock_action` 归档 pin=`MISMATCH` 而窗口修复后实测
`PASS`）。本轮重跑：`8 个域的判定与 CASES 归档逐条相同`、`EXIT=0`
（`docs/evidence/C24/akshare-fallback-cross-check.txt`）。

## 4. 探针窗口太窄 ⇒ 加宽样本再判转正（`docs/evidence/C24/fallback_generality_sweep.py`）

`stock_action` 在探针窗口里 PASS，但 4 个事件不足以支撑"换源不换答案"。加宽到
**6 只发行主体 × 2015-01-01..2024-12-31（76 个事件）**，逐字段相对容差 `1e-4`：

- **601318 的 2018-06-07 `cash_dividend`：ths=1.2／akshare=1.0，相对差 16.7%** ⇒ 不符 1/6 符号。
  `stock_action` 的 akshare 腿**保持 `verified=false`**。
- `index_constituent` 扩到 4 个指数（300/50/500/1000 成员）：**成员集合与权重 0/4 不符**，
  但两侧时间戳口径不同（ths `as_of=2026-09-26` 是**生效日**，akshare `as_of=2026-08-31` 是
  指数公司的**月末文件日**）。这一条属"同一份成分、两种 as_of"，是口径判定而不是数据判定 ⇒
  登记给 AC-19 的字段级判据评审，**本轮同样不翻**。

两个候选都不翻，理由不同：一个是数据不等价（实测证伪），一个是时间戳口径未定（实测未覆盖）。
这轮没有"翻正几条"的产出，这就是产出：**不退让判据的话，这两个候选都不该翻**。

## 5. 守卫吃得住吗 —— 6 条变异全部 CAUGHT

`docs/evidence/C24/guard_mutation_proof.py`（→ `mutation-proof.txt`）。每条变异的做法是
**把缺陷原样灌回去 → 要求具名用例失败 → 按原文精确回写 → 复跑必须恢复绿色**。回写刻意不用
`git checkout`：同一批文件上还压着本轮未提交的改动。

| # | 灌回的缺陷 | 必须红的具名用例 | 实测（具名 / 整文件） |
| --- | --- | --- | --- |
| M1 | `stock_action` period 退回 `event` | 形状普查 2 条 | 2 / 2 failed |
| M2 | `index_constituent` 无跨 vendor 读数即 `verified=True` | 覆盖度量 2 条（`degradable` 集合 + 「国内零退路」） | 2 / 2 failed |
| M3 | 表序改成 ths 优先（掩盖 C20 的实测行首） | 行首可执行性 1 条 | 1 / 1 failed |
| M4 | `resolve()` 绕过健康位（`if True:`） | 降级真被采用 3 条（含"全部摘掉必须 LookupError 而不是硬给"） | 3 / 3 failed |
| M5 | sina 期货日线不再裁窗口 | 窗口保真 futures 1 条 | 1 / **2 failed**（多红的那条是单边窗口用例 ⇒ 覆盖面比钉的更宽） |
| M6 | `within_window` 上界失效 | 3 条（futures + stock_action 具名 + 单边窗口） | 3 / **6 failed**（闭区间与另两条腿一并红） |

汇总：`变异 6 条，守卫全部命中 = True`、`EXIT=0`，每条"回写后复跑"都是 `13 passed`／
`11 passed`（工作树无残留，逐串复核 `NO_RESIDUE`）。M2 是本轮最有价值的一条：**"退路存在"
的读数是量出来的**，谁在没有跨 vendor 读数的情况下把某条腿翻正，覆盖守卫立刻红。

## 6. 门禁与真机回归

（门禁数字见 §7 与 `gate.txt`；巡检见下。）真机巡检 `PATROL_EXIT=0`：**20 条 verified 腿逐条
`[ok]`**，`失败 0 项；抖动 0 项；必配 Key 缺失 1 项`，行数与 C22/C23 逐条相同
（`ths/stock_action: 4 rows`、`ths/futures_daily: 15 rows`、`ths/option_daily: 15 rows` 等）。
巡检按注册表能力展开并用**显式 source** 请求，所以它证明的是"取数面没被本轮改坏"；
"退路换得动"由 §1 的三道条件与 §3 的同问题同答案负责 —— 两份都要在，缺一份就只剩半句话。

## 7. 门禁（`gate.txt`，`GATE_EXIT=0`，读自文件内部；5,985 行完整未裁剪）

| 项 | C23 | C24 | 说明 |
| --- | --- | --- | --- |
| `pytest -n 8 -m "not e2e"` | 2,599 → 2,621 passed | **2,645 passed / 5 skipped** | +24 = 本轮新增 13 + 11，逐条对上；`5 skipped` 全部来自 `tests/test_port_fidelity.py`（既有归因） |
| 覆盖率（阈值 84% 未动） | 86.30% | **86.34%** | TOTAL 语句 10,630 → **10,640**（+10，全属本轮新增）、missing 1,283 → **1,282**、部分分支 307 → **306** ⇒ 未覆面没有增加 |
| 本轮改动面 | — | `_normalize.py` 84.21%（分支 10/部分 3）、`futures_daily.py` 94.59%、`option_daily.py` 94.12%、`bond_daily.py` 96.08%、`stock_action.py` 83.33%、`registry.py` 94.44%（与 C23 持平，本轮未改） | `within_window()` 与 4 处窗口守卫的分支全部被 §1/§2 的用例走到 |
| a2-check | 258 文件 | **263 文件**，ruff / format / mypy / bandit 四项全 ok | +5 = 1 个 `scripts/ops` 脚本 + 2 个测试文件 + 2 个 `docs/evidence/C24/*.py` 度量脚本 |
| public-api-quality | 444 → 445 | **446 / 446 双 100%** | +1 是新增的公共纯函数 `within_window`（docstring + 完整标注） |
| 棘轮 | — | `ruff_selfdev 274`(快照 277)／`mypy_selfdev 21`／`bandit_selfdev 4`／`ruff_ported 2144`(2145)／`direct_http_ported 1044`(1045)，三项改善且**未 `--update` 冻结** | 只降不升 |
| 前端 | 8 files / 79 tests | 8 files / 79 tests，eslint 0 errors | 本轮无前端改动 |

**一处门禁首跑失败与处置**：`make gate` 第一次跑挂在 `FAIL bandit`
（`docs/evidence/C24/guard_mutation_proof.py` 的 `import subprocess` /
`subprocess.run` ⇒ B404、B603）。处置是按仓库既有口径补 `# nosec B404` /
`# nosec B603` 与理由注释（`a2_check.py`、`ratchet.py`、`codemod/*` 都是这个写法），
**不是**把文件移出判定面、也不是往 `bandit.yaml` 里加 skip；改完复跑门禁的同时**重跑了变异
脚本**，让归档与被审文件是同一份内容。

## 8. 未收口（登记，不粉饰）

1. **3 条 akshare K 线腿本机量不出来**（`stock_daily` / `index_daily` / `fund_etf_daily`：
   东财 502 / secid 候选全失败）。它们是 `NO_READING` 而不是不合格；换网络环境需重跑
   §3 脚本再判。
2. **国内零退路**是今天的实测，不是终态。翻正任一条腿需要：同形状 + 加宽样本的跨 vendor
   等价读数 + （`index_constituent` 还要先定 `as_of` 口径）。三条同时满足才动 `verified`，
   而动之后 §1 的 `DEGRADABLE_SHAPES` 必须跟着加成员（M2 会立刻提醒谁忘了改）。
3. **巡检探针窗口偏窄**（`stock_action` 731 天 / 4 个事件）：跨 vendor 判据的样本量必须像
   §4 那样加宽，否则 PASS 是窗口大小的产物。这条应进 AC-19 的字段级判据。
4. **`option_daily` 的 volume 量纲差（≈1e4 倍、无稳定比值）与 `futures_daily` 的 `amount`
   缺失** ⇒ 若真要两条腿互换，需要 AC-19 的字段级判据先定义"退路可以少给字段吗"。
5. **`financial_statement` 的行口径不同**（83 vs 13 键、交集 0）：退路给全科目长表，
   主源给选定科目。不是数值错，但按现在的判据就是 MISMATCH。
6. `financial_indicator` 仍无 auto 合格腿（`announce_date` 缺口，C10 登记）。
7. `bond_daily` 只有一条未转正腿（无行、无退路），窗口裁剪本轮已一并修好，但跨 vendor
   对照还没做。

---

## 附：本轮改了什么

| 文件 | 改动 |
| --- | --- |
| `opendata/data/providers/akshare/models/_normalize.py` | 新增 `within_window()`（两端闭、缺界开放） |
| `.../models/{futures_daily,option_daily,bond_daily,stock_action}.py` | `normalize()` 按调用窗口裁剪；`stock_action` period `event`→`1D`；`option_daily` 文档改成实测结论（volume 与 ths 不可比） |
| `tests/test_sina_window_fidelity.py` | 新增 11 条：4 条腿的窗口保真 + 开放请求仍全量 + 单边窗口只裁给的那一侧 + 闭区间 + 「每条 akshare 腿都要被归类」普查 |
| `tests/test_fallback_degradation.py` | 新增 13 条：三道条件逐条量 + 覆盖清单钉死 + 反空转（不许靠删行让结论成立） |
| `scripts/ops/akshare_fallback_cross_check.py` | 新增：8 域退路逐域复核，PASS/MISMATCH/NO_READING 三分，pin 不一致即 `EXIT=1` |
| `docs/evidence/C24/fallback_generality_sweep.py` | 新增：转正候选的加宽样本复核 |
| `docs/evidence/C24/guard_mutation_proof.py` | 新增：6 条缺陷回灌 → 具名用例必须红 → 精确回写 |

数仓全程**只读**，未打印任何密钥。
