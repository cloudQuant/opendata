# C10 · ths 财务报表腿：三张报表接上、按「分」逐科目对照新浪转正（AC-10）

> 日期：2026-09-25（真机对照与门禁均在本轮实测）
> 结论：`/api/a-share/financials/{income-statements, balance-sheets, cash-flow-statements}`
> 三条端点由 `available` → **`implemented`**，域 `financial_statement` 的 ths 能力
> `verified=false` → **`verified`**。判据是**跨 vendor 逐科目分位相等**：9 条腿共
> **281 个「标的 × 报告期 × 科目」判据单元格、值不符 0、披露日不符 0**，
> `result: PASS (0 failing legs)`、`EXIT=0`。

---

## 1. 产物

| 文件 | 作用 |
|---|---|
| `opendata_fuyao/endpoints.py` | 三条报表端点常量、`FINANCIAL_STATEMENT_ITEMS`（13/7/6 个英文科目）、`FINANCIAL_PERIOD_END`、`build_financial_statements_request`、`_financial_report_period`、`normalize_financial_statements`、`fetch_financial_statements` |
| `opendata/data/providers/ths/models/financial_statement.py` | `ThsFinancialStatementQuery` + `ThsFinancialStatementFetcher`（中文披露名别名、行身份对账、长表排序） |
| `opendata/data/providers/ths/registration.py` | ths fetchers 6 → **7** |
| `opendata_fuyao/endpoint_map.yaml` / `.py` | 3 行 `available` → `implemented`（映射表 **implemented 12 → 15 / available 79 → 76**，登记总数 102 不变），`implemented_paths_in_code()` 同步 |
| `opendata/data/openbb_map.yaml` | ths `financial_statement` `pending` → **`verified`**；ths `financial_indicator` 保持 `pending` 并把实测阻塞点写成注释 |
| `scripts/ops/ths_financial_statement_cross_check.py` | 跨 vendor 判定脚本（9 腿 + 1 别名腿、节流退避、例外双向过期、映射写错守卫） |
| `tests/test_fuyao_endpoints.py` | +14 个测试函数（请求构造 3 / 归一化 8 / 取数 3，其中端点参数化 ×3 ⇒ 16 个用例） |
| `tests/test_ths_provider.py` | +7 个适配层测试函数；注册表三条清单补 `financial_statement` |
| `docs/evidence/C10/ths-financial-statement-cross-check.txt` | 真机对照报告（本轮判据） |
| `docs/evidence/C10/gate.txt` | `make gate` 完整未裁剪输出（5,541 行，含分支/HEAD/脏文件/Python/node 头部） |

---

## 2. 上游语义实测清单（先量，再写代码）

不照抄文档，逐条真机确认：

1. **响应是宽行**：一行一个报告期、一列一个科目。契约 `FinancialStatement` 是长表
   （一科目一行），摊长的动作在 `normalize_financial_statements` 里做，适配层不重复实现。
2. **`period` 文档写「必需，默认 annual」**——实测省略即 `code=1001`「请求参数缺失或
   格式非法」，故本地**总是显式发出**该参数（单测 `test_financial_request_always_sends_period` 钉住）。
3. **裸码 / 逗号多码 / 错误后缀 / 未知 `period` 一律 `code=1002`**。股票后缀只有
   `SH`/`SZ`/`BJ`，但本地**不猜**——裸码在 `build_financial_statements_request` 即拦
   （`financial_symbol_qualified`），带后缀与否交给 provider 层的 `resolve_code`。
4. **`limit` 与 `start`/`end` 互斥、`start`/`end` 须成对**：上游对这两种非法组合回的文案是
   「请求的标的或字段不受支持」，**与真实原因无关**。照抄文案会把调用方引去查科目名，
   故本地先拦（`financial_mode` / `financial_window`）。
5. **`limit` 上界 20**；`annual` 默认只回**最近 4 期**，而新浪同标的同表有 99~123 期
   ——期数差是**取数模式**差，不是数据缺失（对照因此按「我们有的每一期」判定）。
6. **毫秒戳是上海零点**：`period_end_ms` / `report_date_ms` 按 UTC 解释会整体**早一天**
   （实测 `1767110400000` 上海语义 2025-12-31、UTC 语义 2025-12-30）。报告期末同时由
   `fiscal_year` + `fiscal_period` 唯一确定 ⇒ 两路**对账**，不一致即
   `financial_income_period_mismatch` 失败关闭，而不是挑一路信。
7. **`quarterly` 会给 `Q4` 行**，且与同财年 `FY` 行逐位相同（都是 12-31 累计数）——
   季报腿因此同时验 `Q1..Q4` 与 `FY` 的映射表。
8. **`currency` 实测恒为 `CNY`**；契约 `FinancialStatement` **没有币种字段**，出现其它币种
   即失败关闭（`financial_income_currency`）——静默入库会把美元数当人民币数用，且不报错。
9. **`thscode` 与去后缀的 `ticker` 两路对账**（`financial_income_symbol_mismatch`）：
   落库用裸码当合并键，猜错一路不会报错、只会把财务数挂到另一个发行人上。
10. **`announce_date` 取 `report_date_ms`，不本地推导**：实测与新浪「公告日期」逐期一致，
    包括追溯调整造成的错位（茅台 FY2024 与 FY2025 同为 2026-04-17、FY2021 为 2023-03-31）。
    这类「两期同一披露日」正是 PIT 查询的坑，能对齐说明两侧读的是同一件事实。
11. **`revision` 恒为 `1`**：一次响应只给**当前生效**的那版数值，没有历史修订序列，
    故不猜修订号（跨请求的时点差异由 `announce_date` 承载）。
12. **`financial_indicator` 本轮不接（实测阻塞）**：`/api/a-share/financials/financial-indicators`
    的响应是 `data.abilities`（指标名 → **字符串**值），**行内没有披露日**；而契约
    `FinancialIndicator.announce_date` 是必填且是 PIT 的键。不本地造日期 ⇒ 映射表
    `openbb_map.yaml` 记 `pending` 并把这句测量写进注释，端点场景记「待口径定案再接」。
13. **连续调用被直接断连**（约 10~20 秒恢复，C9 已观测）⇒ 对照脚本自带 12 秒节流 +
    5×20 秒退避，把「跑不通」与「数据不对」分开记。

---

## 3. 判据为什么是「按分到」而不是给容差

财务数与日线不同：**披露值是离散事实**（发行人报出的是元级、两位小数的确定数），
两家分歧只可能是「谁抄错了 / 谁取了不同口径的科目」，不存在 1e-6 级可信残差。
实测两侧同科目的相对残差最大 **1.38e-16**（float64 表示位），故判据写成
`round(value, 2)` **逐分相等**——不引入 `1e-6` 这种「看着严格、实则能放过 0.01 元级
真实错值」的阈值。同理：

* **披露日相等**是一等判据（PIT 查询依赖它，值对而日错照样把回测测错）；
* 每腿至少 **12 个判据单元格**（近乎空响应不能算通过）；
* 同一 `(symbol, report_period, item)` 键**不得有两个不同值**；
* 我方有、参照方从未披露的**报告期**即失败；
* 中文 `statement_type` 写法必须与英文名落到**同一批行**（别名腿）。

例外表 `KNOWN_DEVIATIONS` **双向判定**：未登记的越界 FAIL，已登记日重新落回界内也 FAIL
（例外不可只增不减）。本轮该表**为空** ⇒ 没有例外可积。

---

## 4. 一处判据写错并当场改正（自我更正留档）

第一版脚本把「参照方该期没有这一列」一律当成失败，首轮真机 **3 条腿 FAIL**：

* `000001.SZ`（平安银行，利润表）7 个科目、`600036.SH`（招商银行，资产负债表）4 个科目
  报「新浪该期无『所得税费用』/『营业成本』/『货币资金』…列」；
* `688981.SH`（中芯国际）报 1 个（`interest_expenses` → 利息支出）。

逐列打印参照方帧的列名后定性为**报表形态差异，不是数据分歧**：银行利润表**没有**
营业成本/销售费用/管理费用/研发费用/所得税费用这些列（用「业务及管理费用」「减:所得税」），
招行资产负债表用「现金及存放中央银行款项」而非「货币资金」、用
「归属于母公司股东的权益」而非「所有者权益(或股东权益)合计」。把这些登记成
「ths 的错」会污染真值，直接放宽判据又会让映射表写错时静默通过。

改正口径（两条同时成立，缺一即造假）：

1. **判定只覆盖两侧对该标的都发布了这一列的格子**；某标的不发布该列 ⇒ 记为
   「不可比科目」并逐条打印（观测），不判定；
2. 但一个科目若在**同一张报表的所有腿**的参照侧都不存在，即**映射写错**，必然失败
   （`_mapping_misses`）。没有第 2 条，判据集合会随标的选择静默缩小到零。

同轮还修了一处**误导性失败摘要**：把整个 `diffs` 字典印在 `conflicts=` 标签下
（`missing` 被显示成 `conflicts`），排障时会去查一个不存在的键冲突。

---

## 5. 真机跨 vendor 对照（转正判据）

* 我方：`get_registry().resolve_domain('financial_statement', source='ths').fetch(...)`
  ——**路由路径**而非直连函数；
* 参照：`resolve_domain('financial_statement', source='akshare')` ⇒ **新浪三张报表**
  （不同的 vendor、不同的抓取链、中文科目名）。

| 腿 | 标的 / 报表 | 判据单元格 | 值不符 | 披露日不符 | 不可比科目 | 结果 |
|---|---|---|---|---|---|---|
| 茅台 利润表 年报 | 600519.SH | 52 | 0 | 0 | 0 | PASS |
| 茅台 资产负债表 年报 | 600519.SH | 28 | 0 | 0 | 0 | PASS |
| 茅台 现金流量表 年报 | 600519.SH | 24 | 0 | 0 | 0 | PASS |
| 平安银行 利润表 年报 | 000001.SZ | 24 | 0 | 0 | 7（银行科目表） | PASS |
| 宁德时代 现金流量表 年报 | 300750.SZ | 24 | 0 | 0 | 0 | PASS |
| 中芯国际 利润表 年报 | 688981.SH | 48 | 0 | 0 | 1（利息支出） | PASS |
| 招商银行 资产负债表 季报窗口 | 600036.SH | 16 | 0 | 0 | 4（银行科目表） | PASS |
| 茅台 利润表 季报窗口 | 600519.SH | 65 | 0 | 0 | 0 | PASS |
| 茅台 利润表 中文写法 | 600519.SH | 与标准腿逐格一致（52） | - | - | - | PASS |

合计 **281 个「标的 × 报告期 × 科目」判据单元格**、**8 个报告期对齐**（含 2 条季报窗口腿），
0 值不符 / 0 披露日不符 / 0 缺期 / 0 键冲突，`result: PASS (0 failing legs)`。
腿的选择：制造/银行/科创/创业板四种发行人形态 × 三张报表 × 年报与季报窗口两种口径，
使「只在某一家发行人形态上巧合对齐」不成立。

---

## 6. 注册事实变化（复算口径）

| 事实 | C9 | C10 |
|---|---|---|
| 注册能力 | 28 能力 / 15 域 | **29 能力 / 15 域** |
| `verified` 能力 | 15 | **16** |
| ths fetchers | 6 | **7** |
| 端点映射表 | implemented 12 / available 79 | **implemented 15 / available 76**（登记 102 不变） |
| 对照表状态分布 | verified 15 / registered 13 / pending 3 | **verified 16 / registered 13 / pending 2** |
| 能力清单接口 | `financial_statement` → akshare / `verified=false` | **→ ths / `verified=true`** |

**回归翻面（如实记录，不是「测试写错了」）**：`tests/test_p0_providers.py` 的能力清单
用例此前断言 `financial_statement` 来自 akshare 且未验证——那是转正前的真实状态。
ths 腿转正后按权威序（`authority.json` 早已把 ths 排在该域首位）该域自动路由翻到 ths，
断言随之翻面并加注释。**排序先于证据存在**这件事在本轮再次被坐实：C9 已在成分股域
发现同一问题，本轮把该域首位断言一并补齐。

---

## 7. 离线单测与本层覆盖率

新增 **21 个测试函数 / 27 个用例**（2,414 → **2,441 passed**，差额完全对上）：

* **请求构造 3**：`period` 总是显式发出、窗口闭区间 + 上海零点毫秒、11 种非法组合逐条
  失败关闭（含 `limit=True` 这种 bool 伪装 int）；
* **归一化 8**：宽行摊长（含 `(report_period, announce_date, item)` 升序、`revision=={1}`、
  裸码落表）、三种 `statement_type` 各读自己的科目集 + 未知类型即拒、
  `null` 跳过但**键缺失不跳过**、非数值/bool 即拒、财年两路对账（4 种畸形组合逐条）、
  非 CNY 即拒、`thscode`/`ticker` 两路对账（3 种）；
* **取数 3（×5 用例）**：三张报表各打自己的端点、窗口与 `limit` 两种模式落到 query、
  未知类型**在发请求之前**即拒（断言零请求，防静默打到其中一张表）；
* **适配层 7**：中文披露名归一到契约取值、四类非法词表失败关闭、宽行摊长升序、
  裸码先经 `search` 解析（两跳路径逐条断言）、报告期末窗口透传、空响应即拒、
  回显他人发行人即拒。

外加 `tests/test_provider_conformance.py` 按 fetcher 参数化，使新腿
**零新增用例自动纳管**（+4）。

覆盖率（`make gate` 的 `test-cov` 实测）：

* `opendata/data/providers/ths/models/financial_statement.py` **100.00%**（38 语句 / 12 分支 / 0 partial）；
* `opendata_fuyao/endpoints.py` **94.82%**（未覆盖行全属其它端点分支，本轮新增三函数无缺口）；
* 全量 **85.73%**（C9 85.53%，阈值 84% 与 `precision = 2` 均未改动）。

---

## 8. 门禁复跑

`make gate` **全绿**（`GATE_EXIT=0`，完整未裁剪输出 5,541 行见 `gate.txt`）：

| 子门禁 | 结果 |
|---|---|
| brand-check / zero-dep / js-points | PASS |
| a2-check | **230 文件**（C9 228，+2 = 新适配层 + 新脚本）全树 ruff/format/mypy/bandit 达标 |
| quality-ratchet | 五项等于快照（277 / 21 / 4 / 2145 / 1045） |
| public-api-quality | **409/409** 文档串与注解双 100%（C9 403，+6） |
| test-cov | **2,441 passed / 5 skipped**，覆盖率 **85.73%** |
| frontend lint / typecheck / test | PASS，8 files / 79 tests |

同轮门禁抓到一处**与本轮无关但真实存在**的潜伏 flake：
`tests/test_pipeline_jobs.py::test_result_counts_come_from_the_per_source_outcomes`
把「今天」硬编码进期望值（`as_of` 缺省即 `date.today()`），跨过上海零点后必然失败。
改为显式 `as_of=WINDOW.start` 并加注释说明为什么必须钉住——这类「只在写它的那天成立」
的断言不是本轮的错，但留着就是下一个假绿点。

---

## 9. 边界与待办（不声称覆盖）

1. **跨 provider 合并键尚未统一**：ths 腿存契约英文科目名 + 英文 `statement_type`，
   新浪（akshare 搬运）腿存**中文**科目名 + 中文报表类型。二者是同一事实的两个观测，
   但**当前 dwd 双源合并会写成两行而不是同一行的两个来源**。本轮修的是验收面（对照证明
   数一致），落库面的归一需要约 50 个中文科目的逐科目映射，属**人工评审口径**（AC-4），
   不在本轮猜。该域现在按权威序取 ths 单源，不会静默错并。
2. **`revision` 恒为 1**：上游不给历史修订序列，故「首披 vs 追溯重述」的区分只能靠
   `announce_date`（同一报告期不同披露日的多次观测需按日归档才有序列）。
3. **`financial_indicator` 未接**：无披露日 + 字符串值，实测阻塞点已写入映射表注释。
4. 判据覆盖的是 `FINANCIAL_STATEMENT_ITEMS` 里登记的 13/7/6 个科目，**不是**上游响应的
   全部列；扩科目即扩映射表，每加一个名字都要在参照侧存在（§4 第 2 条守卫会盯住）。
