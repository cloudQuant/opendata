# C25 轮：巡检字段级 canary（C18/C19/C20 三轮登记的「行数量级看不见列塌陷」缺口，AC-19）

日期：2026-09-25｜分支：`dev`｜前置：C24（`a460d5a` 的 akshare 国内腿逐域转正复核）
本轮把 C18 量出、C19 设计、C20/C21 两轮挂账的那条监控面缺口做完：**巡检现在会读「某一列填
没填」，而不只数「这一页有几行」**。塌陷的列被单独打出来并翻 cron 退出码，但**绝不动路由健康位**。

---

## 1. 缺口从哪来：三次登记，一次比一次具体

| 轮次 | 登记的原文 | 本轮如何处置 |
|------|-----------|-------------|
| C18 | `[ok] ths/instrument: 5578 rows` 里 `list_date` **5,578 行全空** ⇒ 行数量级判据对字段塌陷是盲的（`docs/evidence/C18/README.md` §4/§5） | 实装列级读数，且把「满/散/空」三种形态分开判 |
| C19 | 不做「单次读可判」的字段级 canary：a 股 `list_date` 在 36 次读里 14 次满 / 22 次全空，且随信封毫秒标签走；**且** patrol 健康模型是二值的，标一次 unhealthy 就把整个 ths 源从 `auto` 摘掉 | 允许集合按 (类型, 形态) 钉；偏差**只报告不路由**（D2 变异测试钉住这条） |
| C20 | 「本轮未承接，继续挂账而不是顺带勾掉」 | 承接 |

## 2. 判据面：只有量过的形态才能进表

`opendata/pipeline/patrol.py:316 FIELD_CANARIES` 的四条，**每一条都来自两份已归档测量的一致结论**：
C19 的 36 次翻面复采（`docs/evidence/C19/catalog-flip-rate-all-types.txt`）与本轮的
**10 次 × 4 类 × 8 列**重复读（`field-canary-measure.txt`，分类走生产 `evaluate_canary`，
所以归档与每日报表同源）。

| 列 | C25 实测（10 次读） | C19 实测（24 次读） | 钉进表里的允许形态 | 为什么 |
|----|-------------------|-------------------|------------------|-------|
| `a-share/list_date` | `full ×10`，缺 **8**/5,578 | 满 ×14 / 全空 ×22 | `full` \| `hollow` | 两份目录构建之差是**上游本来就有的两面**；第三种形态才是新闻（A2/A2b 两面都验过） |
| `a-share/name` | `full ×10`，缺 0 | 未测（本轮新增） | `full` | 名字是目录能被 join 的载荷本身；它塌了这条域就没用了 |
| `a-share-index/list_date` | `full ×10`，缺 0/1,431 | 24/24 满 | `full` | 34 次读零例外 ⇒ 这是新的真机底线 |
| `futures/list_date` | `partial ×10`，恒缺 **265**/1,142 | 24/24 稳定 877/1,142 | `partial` | `7777`/`8888`/`9999` 合成系列无上市日；形态一变即告警 |

**刻意不钉的列**（写进 `FIELD_CANARIES` 上方注释，避免后人「顺手补全」）：

- `board`：四类页面今天**全空**。钉成 `hollow` 就等于宣布「以后填上值 = 事故」——那是改进不是故障。
- `delist_date`：a 股/ETF/指数全空、期货恒散值——同一个理由。
- `fund-etf/list_date`：24/24（C19）+ 10/10（本轮）都是全空 ⇒ 该源**不发** ETF 上市日，
  C19 已把它摘出 verified 依据；钉 `hollow` 只会把「哪天上游开始发日期」报成故障。
- 身份列 `symbol`/`exchange`/`status`/`currency`：本轮实测 40/40 满，但它们塌了的话**行都出不来**
  （`Instrument` 契约里这四个是必填 str，`transform_data` 还会因重复码整表拒），
  属"行数探针必然一起挂"的那一类，再加一条列级告警只是重复喊话。

## 3. 机制：四个形状、三态判读、两条永不越界的线

- 形态：`full`（缺 ≤ 20，沿用 C14 已验收的 9/5,578 现场）/ `partial` / `hollow` / `no-reading`。
  `shape_of`（`patrol.py:346`）**先判 hollow 再判容差**——先判容差会把任何 ≤20 行的小页读成满值
  （D1 变异测试的靶子）。
- 三态判读：真读数（可判偏差）／**读不到那一页**（`no-reading`，只打「未判读」不算偏差）／
  空页（0 行属行数探针的业务，`evaluate_canary` 回 `no-reading` 而不是 `hollow`）。
- 两条边界，各有一条具名用例与一次变异证明：
  1. **偏差 ≠ 源不可用**：`patrol()` 只在探针失败时 `mark_unavailable`；列塌陷走 `logger.warning`
     + 报表 + 退出码，路由健康位一个字不动（否则 C19 量到的那两面会天天摘掉九条健康腿）。
  2. **读不到 ≠ 数据坏了**：`no-reading` 不进 `field_deviations`（否则一次网络抖动就是一条字段塌陷告警）。
- **耗时口径不破**：`probe_ms` 在跑 canary 之前冻结（`patrol.py:527`），`latency_ms` 仍是
  「探针耗时」，跨轮归档可比——本轮真机 instrument 腿 `2,391ms`，与 C21 的 `2,643ms`、
  C20 的 `3,610ms` 同量级；canary 额外读的 3 页不计入该数（用例 `test_the_probe_latency_excludes_the_canary_reads`）。
- 一页只读一次：`_run_canaries` 按 `dict.fromkeys` 去重资产类型，所以 `a-share` 的
  `list_date` 与 `name` 出自**同一次应答**（用例断言 `fetcher.calls == 3`）。

## 4. 证据清单

| 文件 | 内容 | 判据 / 退出码 |
|------|------|-------------|
| `field-canary-measure.py` / `.txt` | 判据底稿：4 类页面 × 10 次重复读 × 8 列，逐列形态分布 + 可钉/多形态两个清单（分类器就是生产的 `evaluate_canary`） | `可钉 32 列 / 多形态 0 列 / 未判读 0 次`，`MEASURE_EXIT=0`；**格式化之后重跑过，两次读数逐条相同** |
| `live-patrol.txt` | 真机全量巡检（20 条 verified 腿）+ instrument 腿四行列级读数 | `[ok] ths/instrument: 5578 rows, 2391ms`、`失败 0 项；抖动 0 项；字段级偏差 0 项（未判读 0 项）；必配 Key 缺失 1 项`、`PATROL_EXIT=0`（同为格式化后重跑，读数与第一跑一致） |
| `canary-injection-attribution.py` / `.txt` | **真机注入归因**：只在 `ThsInstrumentFetcher.transform_data` 的返回值上动手（把真目录某一列清空/半空/抽掉一页/整腿打死），patrol 判定、registry 健康位、`FIELD_CANARIES` 实测集合、`health_patrol.main()` 报表与退出码全走生产路径 | A1 `a-share-index/list_date` 全清空 ⇒ `ok=True rows=5578 偏差=1 退出码=1 auto 仍可达` + 报表 `[CANARY]`；A2 `a-share/list_date` 全清空 ⇒ **不告警**、退出码 0（反向对照）；A2b 半空 ⇒ 第三种形态告警；A3 futures 读不到 ⇒ 只打「未判读」；A4 整腿不可达 ⇒ `[FAIL]` + 标不可达 + 退出码 1，与 A1 严格可分。五面判定全 PASS，`EXIT=0` |
| `mutation-proof.py` / `.txt` | 判据吃得住性证明：把 D1–D5 五个「能写出来但当时会被测试漏掉」的错法逐个装回 `patrol.py`，要求各自的具名用例变红，再按字节还原并 sha256 校验 | 五个具名用例全部 `1 failed`、五次 `还原校验=True`、还原后 `49 passed`，`EXIT=0` |
| `gate.txt` | 修复后重跑的门禁完整未裁剪输出（6,027 行，抬头含首跑挂点与归档覆盖缺口的说明） | `GATE_EXIT=0`，`2663 passed, 5 skipped in 49.82s`，coverage `86.42%`（阈值 84%），前端 8 文件 / 79 测试 |
| `format-only-fix-proof.txt` | 首跑挂点（`ruff format --check`）的**性质证明**：修复只动空白、判定面没有收窄、门禁配置一个字没改。含首跑 FAIL 读数的**摘录**（那份未裁剪归档被重跑覆盖，见 §5） | `ruff format --diff` 空输出、`ruff check` 通过、`mypy` 2 files Success、`bandit -n 3` 退出 0、三个文件仍在 A2 清单 266 个里、`PROOF_EXIT=0` |

## 5. 门禁与测试数字

- 门禁：`GATE_EXIT=0`。分段读数——a2-check **266 个文件**（C24 为 263，+3 是本轮三个 `.py` 证据脚本）
  全部满足 ruff + format + mypy + bandit；quality-ratchet `debt did not increase`
  （`ruff_selfdev 274`/snapshot 277 improved、`ruff_ported 2144`/2145、`direct_http_ported 1044`/1045、
  `mypy 21`、`bandit 4`；**没有**跑 `--update` 把新低冻进快照）；public-api **450 个**公开可调用对象
  docstring 与标注均 **100%**；`pytest -n 8 -m "not e2e"` **2,663 passed / 5 skipped**（C24 为 2,645，+18 全在本轮新用例）；
  前端 eslint 0 errors / 48 warnings、vue-tsc 干净、vitest 8 文件 / 79 测试。
- 覆盖率：`TOTAL 10706 语句 / 1282 未覆 / 2608 分支 / 306 部分 = 86.42%`（阈值 84%，C24 为 86.34%）。
  本轮改动的读数取自 `gate.txt` 的表格本身：**`opendata/pipeline/patrol.py` 202 语句 / 6 未覆 / 38 分支 / 1 部分 = 97.08%**，
  未覆区间只有 `42-48`（`if TYPE_CHECKING` 导入块，与 C8 起声明的全库同口径一致）。
- `tests/test_patrol.py` **31 → 49 条**（+18）：`TestFieldCanary` 10 条（形态四分类、容差与 hollow 的先后、
  空页属 `no-reading`、空格串算缺失、未测过的形态即告警、canary 表不能描述不可路由的腿、
  被盯列必须是契约字段、必须覆盖 C18 那次塌陷现场）+ `TestPatrolCanaryWiring` 7 条（塌陷只报告不动健康位、
  一页两列只读一次、没探针的腿不给读数、耗时不含 canary 读、读不到不算告警、干净列零偏差）+ API 1 条
  （`/health/patrol` 把健康腿上的空列报成 `field_deviations` 而不是失败）。
- **本轮流程缺口（不遮掩）**：首跑在 a2-check 的 `ruff format --check` 上失败（`GATE_EXIT=2`，329 行——
  `docs/evidence/C25/field-canary-measure.py`、`scripts/ops/health_patrol.py`、`tests/test_patrol.py`
  三个文件仅空白待格式化，a2-check 失败即止所以 pytest 没跑到）。我把修复后的重跑写回同名 `gate.txt`，
  **那份 329 行未裁剪原始输出因此被覆盖，没有留下独立归档**——C21 的先例是另存 `gate-before-tz-fix.txt`，
  本轮没做到。首跑读数只能以摘录形式留在 `format-only-fix-proof.txt` 里；同理，`field-canary-measure.txt`
  与 `live-patrol.txt` 也是格式化后脚本的重跑件（两份的判定读数与第一跑一致：32/0/0 汇总、
  20 条 `[ok]` 腿与四行 canary 形态；逐条 `latency_ms` 本就有自然波动，不作为对照项）。

## 6. 判据未放宽的四处声明

1. **格式挂点是修代码，不是收判定面**：三个文件按 ruff format 落空白，`ruff format --diff` 现在为空输出；
   三个文件仍全部在 A2 清单（266 个）里；`pyproject.toml` / `bandit.yaml` / `docs/quality/` / `scripts/quality/`
   的 `git status` 为空——没有加豁免、没有缩扫描面、没有调阈值。
2. **形态集合来自测量而非愿望**：四条 `FIELD_CANARIES` 的允许形态都由 10×重复读 + C19 的 24 次读共同背书；
   今天全空的 `board`/`delist_date`/ETF `list_date` **没有**被钉成 `hollow`（钉了就等于把「上游哪天开始发值」报成事故）。
3. **告警行为来自带标签的真机注入，不是自然现场**：本轮两次自然巡检都是 `字段级偏差 0 项`，
   A1/A2/A2b/A3/A4 五面判定都在 `canary-injection-attribution.py` 里显式标注
   `【C25 证据注入，非上游真实行为】`，没有把注入包装成真实故障。
4. **判据本身可反证**：D1–D5 五个已知错法逐个装回生产文件，各自让**唯一**一条具名用例变红，
   还原走 sha256 字节比对（本轮 `patrol.py` 改动尚未提交，`git checkout` 会把本轮改动一起清掉，
   所以没有用它做还原）；还原后整条 `tests/test_patrol.py` 重新 `49 passed`。

## 7. 遗留（明确不做 / 做不了，含原因）

1. **canary 只覆盖 `instrument` 一条腿的四列**。价格/财务/日历域各有自己的字段塌陷形态（如
   `financial_statement` 的 `报告日期`、`stock_daily` 的成交量列），本轮没有把面铺开——
   每加一列都要先有一份"10 次重复读"的底稿，否则就是把猜测钉进告警面。
2. **canary 每轮多读 3 页目录**（`a-share` / `a-share-index` / `futures`），巡检耗时与上游配额因此略升；
   本轮实测 instrument 腿 `2,391ms`（C21 为 2,643ms），仍在噪声范围内，但配额面（AC-19 的
   Key 配额/到期/封禁主动监控）**仍未做**，AC-19 整体保持"部分完成"。
3. **`mutation-proof` 只覆盖 canary 判读面**（`patrol.py` 一个文件、五条不变量）。
   行级 `full` 判定里 `CANARY_MISSING_TOLERANCE = 20` 这个常数本身没有独立用例证明"20 是对的"，
   它沿用 C14 已验收的 9/5,578 现场口径。
4. **bandit 报一条无害 WARNING**（`Test in comment: argv is not a test name or id, ignoring`）：
   `mutation-proof.py` 里 `# nosec B603` 后面跟了中文说明，bandit 把尾部词当 id 解析。
   抑制本身生效（`-n 3` 退出码 0、a2-check 通过），后续把说明移到标记之前即可，不影响本轮判定。
5. **仓内落库仍未动**：`dwd_instrument` 等表的落库、AC-15 旧表 DROP（用户决策：只保留只读）继续等确认。
6. **fred 必配 Key 仍缺失**（`必配 Key 缺失 1 项`），与 C21 同状态，不是本轮引入。
7. **首跑归档被覆盖**已记于 §5。后续轮次把"失败现场另存 `-before-<缺陷>.txt`"当硬步骤执行，
   重跑前先看目标文件是否已存在。
