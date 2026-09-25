# C23 轮：`authority.json` ↔ 活注册表全库对账（`/sources` 不再声明不存在的腿）

日期：2026-09-25｜分支：`dev`｜前置：C22（`a2935d5`）｜对应任务：#30｜判据面：AC-3 / AC-10 / AC-11 / AC-17

C22 把宏观域的权威序补上了，同时**明确写了自身的覆盖面边界**：那轮的守卫只遍历测试里一张
**11 条腿的硬编码清单**，"新域没进表""某行列了不存在的源"这类缺陷它一个也发现不了，
并且留了一句"等 `bond_daily` 接第二条腿时同一行必须补进 `authority.json`，这一条登记在 §8
而不是假装已有用例守着"。本轮就是把那句"人工守着"换成用例：
**把整张表和整个注册表互查一遍，两个方向都不许漂移**，并把 C22 留下的 3 个未登记域按
AC-10 已声明的口径区分成"必需行 / 显式豁免 / 显式保留"三类钉死。

缺陷不是路由错误，而是**对外声明夸大**：`GET /api/v1/data/sources` 把 `authority.json`
**原样**回给调用方（`opendata/api/data.py:59`），本轮实测表内有 **7 对 (域, 源) 在注册表里
没有任何能力** —— 调用方看到"这个域可以用 akshare"，实际取数会 fail-closed 报
`no verified capability registered`。

---

## 1. 实测：7 对假腿，全部有名有姓

| 域 | 表里写的源 | 实际情况（本轮实测） | 处置 |
|----|-----------|--------------------|------|
| `stock_adjust` | `ths, akshare` | 该域**一条能力都没注册**：复权因子不经路由层，走 market-dumps `a_share_adjustment_factors_event_none_all` → ods → `FactorBuilder` → `dwd_stock_adjust`（`opendata_fuyao/endpoint_map.yaml` market-dumps adjustment-factors、`opendata/pipeline/factor_builder.py`） | 整行删除 |
| `futures_fundamentals` | `ths, akshare` | 该域一条能力都没注册：F10 端点是 `client_only`（"端外暂不开放"） | 整行删除 |
| `financial_indicator` | `ths, akshare` | ths 端点 available 但**故意没接**：响应是 `data.abilities`、没有逐行披露日期，而 `FinancialIndicator` 契约要 `announce_date`（C10 实测） | 只留 `akshare` |
| `instrument` | `ths, akshare` | C14 只落了 ths 侧，akshare 从未注册腿 | 只留 `ths` |
| `trading_calendar` | `ths, akshare` | C13 同上 | 只留 `ths` |

表 **17 行 → 15 行**；`_comment` 换成"两条规则 + 每条单源行的理由 + 7 对删除的实测依据"，
并原样保留 C22 的宏观测量记录（1827 天地板、oecd-before-imf 的 17/6 vs 11/0、
USA 0.097pp / JPN 0.174pp / DEU 0.421pp 对 0.3pp 容差、CPI 两条 1M 腿"声明而非实测"、
fred 因未验证而不排序）。

## 2. 判据：两条规则 + 一类显式豁免

`opendata/data/registry.py` 新增纯函数 `reconcile_authority(capabilities, *, baseline=None)`
（**不接进 `resolve()`**：单个 provider 的测试进程只注册一片能力，按"整库判"会在不该红的地方红；
它属于**部署级自检**，跑在门禁里）：

| 规则 | 含义 | 为什么需要 |
|------|------|-----------|
| `empty-row` | 行列了域却没有源 | 读起来像"已登记"，实际什么也不排 |
| `phantom-leg` | 表里出现注册表没有的 (域, 源) | 就是 §1 那 7 对：对外声明夸大 |
| `unranked-leg` | 参与 auto 的源没被该行排序 | C22 的宏观缺陷：赢家退化成注册顺序 |
| `unlisted-domain` | **≥2 源**服务却没有行 | 只在该域"有东西可排"时成立，见下 |

**单一源的域不需要行**——这是 AC-10 已经写死的口径（`fund_action`、海外日线那两处
"登记成权威序属口径造假"）。本轮第一版把判据写成"表覆盖的域集合 == 有能力的域集合"，
为凑相等给 `bond_daily`/`fund_action`/`stock_daily_overseas` 各补一行（18 行）；**那是在为了
自己的判据去改表**，正好是本模块要防的那类事，故回退（回退过程与两次读数记在
`authority-mutation.txt` 末节，没删）。最终版把"留下的单源行"从隐式缺口变成**显式清单**：
`SINGLE_SOURCE_ROWS`（`economy_rate`、`financial_indicator`、`instrument`、`trading_calendar`）
与 `SINGLE_SOURCE_EXEMPTIONS`（`bond_daily`、`fund_action`、`stock_daily_overseas`）
各自钉死，新增一条单源行必须同时说明理由。

## 3. 新证据形式：改表必须证明"没改变任何一次路由"

本轮改的是元数据，所以最大的风险不是改错，而是**把"我猜没影响"写成结论**。两条实测分开量：

- **B 面等价**（`scripts/ops/authority_reconcile_check.py`，离线、只读）：把 **42 个路由问句**
  （每个 `(asset_class, domain, period, market)` 组合 + 每个域"只给域名"的形态）在
  **生产 `resolve(source="auto")`** 上分别用 HEAD 表与新表各跑一遍，逐条比答案，
  **连 fail-closed 的报错文案一起比**。结果 `答案改变的路由问句数 = 0 / 42`。
  脚本做法：临时把 `registry._AUTHORITY_PATH` 指向两版表各自的副本 + 清 `lru_cache`，
  `finally` 还原，不改仓库文件。
- **A 面差集**：两版表各自逐对查活注册表 ⇒ `HEAD 假声明数 = 7`、`worktree = 0`、
  `本轮新增的假声明 0 对`（防"删了旧的、顺手写进新的"）。
- **C 面判据**：`reconcile_authority(活注册表) = 0 违规`。
  合起来 `AUDIT_EXIT=0`。

**变异测试**（`authority-mutation.txt`，基线 111 passed → 三个变异 → 逐字节还原 → 111 passed）：

| 变异 | 对账违规读数 | 用例红数 | 红了什么 |
|------|------------|---------|---------|
| M1 HEAD 表字节放回 | 7 条 `phantom-leg`（逐条点名） | **13 failed** | 活表 5 条 + 7 条参数化 + **2 条 `/sources` API 用例** |
| M2 删必需行 `option_daily` | 1 条 `unlisted-domain: … served by 2 sources` | **11 failed** | 只碰"少列"方向：2 条 API 用例与逐腿查表那条**保持绿**（删行不可能造成夸大） |
| M3 删单源行 `economy_rate` | **0 条**（规则按口径放行） | **4 failed** | 拦下它的是两层独立钉块：本轮 `SINGLE_SOURCE_ROWS` 2 条 + C22 `test_every_macro_domain_is_listed` 等 2 条 |

M3 是本轮唯一"规则放行、清单拦住"的形态——**如果没有那张钉死的清单，删掉 `economy_rate`
会全绿通过**（因为对账规则本身不管单源域）。规则级用例 `TestReconciliationRules`（7 条）
在三个变异里**一条也没红**：它们用手写夹具各测一条规则、不吃出厂表，这是刻意的分工。

## 4. 影响面：`/sources` 是唯一真正变化的面

| 调用点 | 本轮之后 | 依据 |
|--------|---------|------|
| `GET /api/v1/data/sources` 的 `authority` | **少了 2 个域、3 个源**（对外不再声明 `stock_adjust`/`futures_fundamentals` 与 3 条 akshare/ths 假腿） | B 面只比路由、这一面由 §1 的差集与 2 条 API 用例守 |
| 一切 `resolve` / `resolve_domain` 调用 | **零变化** | B 面 0/42 实测，不是推断 |
| 巡检 `patrol`、调度 `jobs.resolve_fetcher`、对照脚本 | 零变化（都走显式 `source=`，在排序之前就 return） | 沿 C22 §4 的逐调用点核对 |
| `reconcile_authority` 自身 | 新增，**无生产调用方**（门禁 + 审计脚本调用） | 全仓 grep |

真机侧留了一份当日回归：`live-patrol.txt` 20 条 verified 腿逐条 `[ok]`，
`失败 0 项；抖动 0 项；必配 Key 缺失 1 项`（fred），`PATROL_EXIT=0`，
行数与 C22 那跑逐条相同（`ecb 12/8/14`、`imf 10/10/10`、`oecd 24/2`、`ths instrument 5578`、
`trading_calendar 242`、`index_constituent 300`…）⇒ 对账只动了声明面，没动任何一条腿的可路由性。

## 5. 证据清单

| 文件 | 内容 | 判据 / 退出码（**读自文件内部**） |
|------|------|--------------------------|
| `authority-reconcile-audit.txt`（+ `scripts/ops/authority_reconcile_check.py`） | A 面差集 / B 面 42 个路由问句两版表逐条等价 / C 面判据，全程离线、只读、不打印密钥 | 假声明 `7 → 0`、新增假声明 0、`答案改变的路由问句数 = 0 / 42`、`违规数 = 0`、`AUDIT_EXIT=0`（144 行未裁剪） |
| `authority-mutation.txt` | §3 的变异测试：基线 / M1 / M2 / M3 / 逐字节还原 + sha256 校验 / 复跑，五段 pytest **全量未裁剪**，末尾附"被回退的 18 行做法" | `BASELINE_EXIT=0`（111 passed）、`MUTANT1_EXIT=1`（13 failed）、`MUTATE2_EXIT=1`（11 failed）、`MUTATE3_EXIT=1`（4 failed）、`SHA_CHECK_EXIT=0`（`OK`）、`RESTORED_EXIT=0`（111 passed）（431 行） |
| `live-patrol.txt` | 改表后的真机全腿回归 | `失败 0 项；抖动 0 项；必配 Key 缺失 1 项`，`PATROL_EXIT=0` |
| `gate.txt` | `make gate` 完整未裁剪输出（5,928 行；头部 5 行运行环境含 `本机时区偏移=+0300`、`branch=dev HEAD=a2935d5（本轮改动未提交）`、`node=v25.1.0` 与工作树表 sha256，随后 5 条脏文件清单） | `GATE_EXIT=0`，`2621 passed, 5 skipped in 49.58s`，coverage `86.30%`（阈值 84%），`A2 files: 258`，public API **445/445 双 100%**，前端 8 文件 / 79 测试 / eslint 0 errors |

归档后的落地顺序与 C22 同口径：晚于 `gate.txt` 的只有本 README 与验收文档（`a2_check.py` 只收
`.py`，`check_brand.py:72` 把 `docs/evidence` 列入 `IGNORED_PATHS`）⇒ 归档已覆盖最终代码树。
本轮**没有**为重跑而重跑：三份离线归档全部在最终表（sha256 `cddd4083…ce86`）之上生成，
早先针对 18 行版本生成的那批（记 `748c8ca8…`）已作废并被覆盖，作废原因写在
`authority-mutation.txt` 末节。

## 6. 测试与门禁数字

- **`tests/test_authority_reconciliation.py` 新增 22 条**（门禁里逐条 `PASSED` 计数 22，与之一致）：
  `TestLiveTableMatchesLiveRegistry` 13 条（对账为空／逐腿查表不夸大／表 == 必需行 ∪ 钉死清单／
  没行的域恰好是那 3 个豁免／`domains.yaml` 的域要么有腿要么进 `UNSERVED_DOMAINS`／
  **反硬编码**：同时摘一条国内行与一条宏观行必须一次报出两条，用来收掉 C22 §4 那条
  "守卫只遍历 11 条腿"的边界／7 条参数化"每对假腿各自加回来必须各自报一条"）、
  `TestReconciliationRules` 7 条（4 条规则各自只咬自己那个方向 + 单源域不需要行 +
  未验证腿可免排序（fred）+ 判定与注册顺序无关）、
  `TestSourcesApiAgreesWithRegistry` 2 条（`/sources` 的 `authority` 必须是 `registered` 视图的
  **子集**而非相等——相等会把 §2 的口径倒退成 18 行；以及那 2 个域、3 条假腿在对外响应里确实没了）。
- 门禁：`2599 → 2621 passed`（+22 = 本轮新文件全部），`5 skipped` 不变（仍全部来自
  `tests/test_port_fidelity.py`，前轮已逐条归因）；
  coverage `TOTAL 10612/1283/2576/307 = 86.28% → 10630/1283/2584/307 = 86.30%`
  ——**未覆行 1283 与部分分支 307 一字未动**，语句 +18、分支 +8 全部覆到。
- `opendata/data/registry.py` **86/5/32/3 = 93.22% → 104/5/40/3 = 94.44%**：
  分支 +8（4 条规则 + 两个方向的集合差）全覆，未覆区间仍是既有的
  `24-27`（`if TYPE_CHECKING` 导入块）与 `57, 59`（前轮 malformed-authority `raise`），本轮未触碰。
- a2-check 扫面 `256 → 258 文件`（新增 `scripts/ops/authority_reconcile_check.py`、
  `tests/test_authority_reconciliation.py`），ruff / format / mypy / bandit 四件 `ok`；
  `public-api-quality` **444 → 445**（新公共函数 `reconcile_authority`），
  docstring 与类型标注覆盖仍 **100% (445/445)**。
- 棘轮五项**只降不升**：`ruff_selfdev 274`（快照 277，improved）、`mypy_selfdev 21`（基线不变，
  `registry.py` 与本轮脚本单独 mypy 均 clean）、`bandit_selfdev 4`、
  `ruff_ported 2144`（快照 2145，improved）、`direct_http_ported 1044`（快照 1045，improved）。
- 前端 8 文件 / 79 测试全过，`vue-tsc` 无错，eslint 0 errors / 48 warnings（既有告警，本轮未触碰前端）。

## 7. 判据未放宽的三处声明

1. **没有为了判据去改表**：第一版"域集合相等"的判据逼出 3 行假权威序，被回退；
   回退方向是**放宽判据以匹配 AC-10 已声明的口径**，并在变异里为这条边界专门留了 M3。
   这与"放大容差换 EXIT=0"是同一类动作的反面。
2. **单源行没有悄悄消失**：`economy_rate` 这类"规则不管、但前轮自愿登记"的行现在有两层独立用例守着，
   M3 证明撤掉任一层另一层仍红。
3. **"没影响"是量出来的**：42 个路由问句两版表逐条对答案（含 fail-closed 文案），
   而不是"这只是元数据，应该没影响"。

## 8. 遗留（明确不做 / 做不了，含原因）

1. **`reconcile_authority` 未接进任何生产启动路径**（既不 fail-fast 启动，也不进巡检）。
   理由见 §2：它是部署级判据，`resolve()` 侧按部分注册表判会误红。
   若要"启动即自检"，得先决定挂在哪（lifespan？`/sources`？巡检头部？）——本轮不替调用方选，
   现在它由门禁与审计脚本回答。
2. **`financial_indicator` 只有一条 akshare 腿**：ths 侧端点 available 但缺 `announce_date`
   （C10 实测），本轮只是不再宣称它有腿，**没有**解决契约缺口。
3. **`bond_daily` 仍是 `verified=false` 的单腿**：它进入 `SINGLE_SOURCE_EXEMPTIONS`，
   一旦接第二条腿，本轮的守卫会立刻以 `unlisted-domain` 要求补行（这正是 C22 §8 那句"人工守着"的落地）。
4. **`stock_adjust` / `futures_fundamentals` 两个域仍在 `domains.yaml` 里**（有契约、有表、
   没有路由腿）。本轮把它们从 `UNSERVED_DOMAINS` 钉成显式清单而不是删域——派生层与端外能力是真实的，
   只是不经 auto 路由。
5. 沿前轮未收口的：IMF↔OECD 失业率 DEU 0.421pp 定义差（C22 §8-1）、OECD CPI 腿 `market` 标签偏松
   （C22 §8-2）、`docs/evidence/C21/flaky-retry-attribution.py:79` 重跑须补 `market=`、
   宏观数据无落库面、`push2delay.eastmoney.com` DNS 仍失败 ⇒ 任务 #14 的 4 个 em 用例继续 `pending`、
   搬运层两处按本机时区渲染（`akshare/stock/stock_xq.py:27`、`stock_feature/stock_info.py:167,200`）、
   akshare 10 条腿 `verified=false` 的逐域复核、`dwd_fund_etf_daily` / `dwd_fund_action` /
   `dwd_instrument` / `dwd_trading_calendar` 落库需明确确认、AC-15 旧表按决定**只读不 DROP**、
   AC-19 字段级巡检 canary 待评审、AC-13 校准 box 仍不勾。
