# C96 — `param.relative_default` 端到端落地：1 行接线、2 行进 shipped_cover、阶梯 (189,7,83) → (188,8,83)

本轮建的是一个**请求侧的界**：`ParamSpec.relative_default` 携带一个相对偏移，调用方点名就用自己的值、
没点名就用解析出来的那一天。判定对象不是「我加没加字段」，而是**在册 present 是不是从盘上读出来的**：
`read_from_disk=True` + `anchor` + `cross_anchor` + `DeclarationProbe`，四件都在仪器里，缺一件就是
把 present 写成一句话说服自己。

结论先行：**present 集 13 → 14；production declarations = 2，逐字落在
`opendata/data/providers/intrinio/specs.py`；阶梯 (189, 7, 83) → (188, 8, 83)，step 0 前缀
`param.relative_default`(+3) → `columns.null_sentinel`(+5)；build order 38 步 → 37 步；
shipped_cover 0 行 → 2 行；blocker 集合缩小的行 7 行，缩小掉的 blocker 只有
`param.relative_default` 一个，0 行增大。**

---

## 1. 引擎面做了什么，fail-closed 规则在哪

声明面 `opendata/data/providers/_engine/spec.py`：

- `RelativeDefault`（`spec.py:117`，`resolve()` 在 `spec.py:161`）只带**整日历单位、只往回**。
  `__post_init__` 拒正数（`points forward`）、拒非整数与 `bool`（`whole number`）；
  `resolve()` 拒 `datetime`（只接裸 `date`，`datetime.date`），月份溢出按当月最后一天钳位
  （`2024-03-31 - 1 month = 2024-02-29`），越出日历范围拒。
- `ParamSpec._check_relative_default` 拒四种自相矛盾：`kind != "date"`（`its kind is`）、
  同时带字面 `default`（`two answers`）、`required`（`no omission`）、不是 `RelativeDefault`
  （`must be a RelativeDefault`）。
- `ModelSpec` 的姓名解析拒三处行侧facet 瞄准相对界：`row_filters` 的 `value_param`/`when_param`、
  `column_derives` 的 `source_kind="parameter"`、`order_by.limit_param`。理由是**静默空读**：
  行侧读的是校验后的 query，省略的参数在那里是 `None`，而请求已经带着解析出来的日期出去了。
  path 占位那条同时放宽了「可选且无默认值不能定位资源」——相对界**算一个界**（`OvernightBankFundingRate`
  那种 `{asof}` 场景在 `tests/test_intrinio_not_declarable.py` 里有臂）。

执行面 `opendata/data/providers/_engine/http_json.py`：`resolve_bound`（`:224`）读一次时钟，
`bound_request` 把每个参数摆成请求侧视图，`fetch_pages` 在任何一次读之前把 query 重绑成那个视图，
所以一个窗口的两条界不会跨午夜各读一次。**没有回退到字面量这条路**：省略 + 无载体 = 不发这个键，
和 C95 之前一样；`C5` 臂实测的就是这件事（见 §5）。

上游依据（本轮逐行重读，只作机制参照、不复制代码）：
`providers/intrinio/openbb_intrinio/models/historical_dividends.py:74-78`（`:74` 取请求日、
`:75-76` `start = now - relativedelta(year=1)`、`:77-78` `end = now`）；
`providers/federal_reserve/openbb_federal_reserve/models/overnight_bank_funding_rate.py:78-82`
（`start` 是**绝对字面量** `datetime(2016, 3, 1).date()`，只有 `end` 相对）与 `:96-98`（两个键进 query）；
`providers/tiingo/openbb_tiingo/models/crypto_historical.py:103-108`（两条界都由 `now` 起，
`start` 是 `relativedelta(years=1)`）。

## 2. 在册注册：present 是被量出来的

`scripts/quality/model_capability_census.py` 的 `REGISTRY["param.relative_default"]`：
`anchor = "relative_default: RelativeDefault | None = None"`（实测 `spec.py:227`）、
`cross_file = HTTP_JSON_REL` + `cross_anchor = "def resolve_bound("`（实测 `http_json.py:224`）、
`read_from_disk = True`、
`probe = DeclarationProbe(call="ParamSpec", keyword="relative_default", non_empty=True)`。
`--check verdict: PASS`，`drifted=[]`，`unanchored=['outside.source_absent']`。

`tests/test_model_capability_census.py::test_registry_shape_is_anchored_or_declared_unproven`
两张名单同步：read-from-disk 名单 8 → 9 行（补进 `param.relative_default`），
probe-less engine-work 名单 15 → 14（它离开了名单）。

## 3. 接了线的行 vs 留在原地的行

| 行 | 状态 | 本轮之后剩下的东西 |
|---|---|---|
| `OBB2-intrinio-HistoricalDividends` | **已接线**，`expressible_today false → true` | 无 absent 能力；两条界都在声明里 |
| `OBB2-federal_reserve-OvernightBankFundingRate` | **未接线**，进 `shipped_cover` | need 集全 present；缺的是 `overnight_bank_funding_rate` 未注册进 `domains.yaml` 的 ModelSpec + binding（package 动作，不是能力） |
| `OBB2-tiingo-CryptoHistorical` | **未接线**，进 `shipped_cover` | need 集全 present；`opendata/data/providers/tiingo/` 不存在，没有 spec 模块能承载声明 |
| `OBB2-intrinio-InsiderTrading` | 未接线，仍 pending | `columns.null_sentinel` |
| `OBB2-federal_reserve-FederalFundsRate` / `-SOFR` | 未接线，仍 pending | `columns.null_sentinel` |
| `OBB2-tradingeconomics-EconomicCalendar` | 未接线，仍 pending | `columns.compose`, `columns.null_sentinel`, `flow.branch` |

**tiingo 那行不接线、不假装有**：没有 tiingo 包，就没有声明承载点。federal_reserve 那行同理：
`MoneyMeasures`/`TreasuryRates`/`SvenssonYieldCurve` 三个已声明模型的窗口是上游在 pandas 里补的
（`money_measures.py:51-56` 填、`:72-75` 用空 `&from=&to=` 拼 URL、`:87-90` 事后过滤），
在这里声明请求侧的界会发出 oracle 从不发出的窗口 —— 那是造假，不是补齐。

## 4. 两条 intrinio 红的重测（不是放宽）

`TestEveryRefusalIsAMeasurement` 的两条按**当下测得的**改数，断言本身一条没删：

- `test_the_documented_histogram_is_the_measured_one`：`param.relative_default 2` 从直方图里消失，
  换成 `derive.conditional 1` + `columns.null_sentinel 1`；
  实测 `{flow.fan_out:14, flow.branch:8, param.value_transform:3, flow.preflight_lookup:3,
  flow.join:2, derive.conditional:1, columns.null_sentinel:1, rows.cap_source_order:1, unnamed:1}`
  = 34，并加了一条 `assert "param.relative_default" not in histogram`。
- `test_the_blocker_set_names_a_need_its_census_row_records_except_one` → `..._except_two`：
  实测 silent = `['CompanyFilings', 'WorldNews']`，32/34 对得上。
  **`WorldNews` 是一条发现，不是笔误**：它是唯一一条 `primary_blocker` 为 `None` 的拒绝
  （带 `GAP_MARKER`，只给「nearest: `rows.order_limit`」），BLOCKERS 交集**按构造恒为空**，
  它的 census 行记的是 `rows.order_limit`/`rows.filter`（两个都 present）—— 所以它既不能被这些
  已出货的能力拒掉，也没有 canonical 名字可拒。新增 `gap_models == ["WorldNews"]` 与
  `len(named) == 32` 两条臂，把这个结构性事实钉住。

## 5. 面与反例臂

`python docs/evidence/C96/relative-default-face.py` → `VERDICT: PASS`，`FACE_EXIT=0`。
判定仪一律从 `scripts.quality.model_capability_census` import（`count_declarations` /
`verify_registry` / `greedy_cover` / `run` / `serialize`），本档不重写任何一个 judge。

- `F1` present/`read_from_disk`/双 anchor 行号/declaration scan `2 {'.../intrinio/specs.py': 2}`
- `F2` 从活对象读出两条界：`[('HistoricalDividends','start_date',{'years':-1,...})]`、
  `[('HistoricalDividends','end_date',{0,0,0})]`；仪器计数 == 树上路数
- `F3` 同一棵树两次 `serialize` 逐字节相同（179449 bytes）且 == 盘上 artifact
- `F4` 阶梯：after `(188, 8, 83)` / step0 `columns.null_sentinel +5` / 37 步 / shipped_cover 2 行；
  **把能力在册面上退回 C95 形状，同一个 judge 复现 `(189, 7, 83)`、step0
  `param.relative_default +3`、present 13、shipped_cover 0 —— 与 C95 公布数字逐字相同**
- `F5` 单调性：缩小 7/279，`blockers dropped` 恰为 `['param.relative_default']`，增大 0 行，
  仍按这条能力锁住的行 0 行
- `F6` cite-carrier：3 行改动、upstream cite 命中 11 条、engine-plane 5 条、dead 0、
  provider 越界 0；逐行 date-bound carrier `{fed-OBFR:2, intrinio-HD:4, tiingo-Crypto:3}`
- 反例臂（每条都必须挪动读数）：
  `C1` 退回 C95 条目 → `present=False declarations=0`；
  `C2` anchor 换成盘上不存在的 token → `status=absent anchor_found=False`；
  `C3` cross-anchor 换成 `def resolve_bounds_nobody(` → `status=absent`；
  `C4` 把唯一接线行退回 `expressible_today=false` → `(188, 8, 83) → (189, 7, 83)`；
  `C5` 从声明里剥掉 `relative_default=` → 请求发的界 `['end_date','start_date'] → []`
  （证明「没有载体就不发」这条静默窄化路径仍然存在、没被字面量偷偷替换）；
  `C6` C95 的预测三元组：只有把**三条**行全打 `expressible_today=true` 才得到 `(186, 10, 83)`，
  其中 2 条没有任何包声明它们；
  `C7` 伪造 `historical_dividends.py:99999` → `dead=[... past 107 lines]`，
  指到非日期行 → `carried=0`。

## 6. C95 那条可证伪预测的处置（原文照录）

C95 写的是：把 credited 的 3 行连同能力一起重 derive，阶梯从
`(189, 7, 83)` 到 `(186, 10, 83)`（和 279），且 build-order step 0 从
`param.relative_default` 挪到 `columns.null_sentinel`。

**本轮实测：前缀那一半兑现了**——step 0 现在逐字是 `columns.null_sentinel`（+5 行）。
**三元组那一半按原写法不可测**：C95 的基线 `(189, 7, 83)` 在本轮开始前就已经被上一波挪走了
（`OBB2-intrinio-HistoricalDividends` 的 flag 一翻，树读到的是 `(188, 8, 83)`），
所以「从 (189,7,83) 到 (186,10,83)」这段的**前提在树下被抽掉了**，不是被证伪。
`F4` 用 in-memory 回退把 `(189, 7, 83)` 原样复现出来，`C6` 再在这棵树上量出：
只有把 fed 与 tiingo 两行也打成 `expressible_today=true` 才得到 `(186, 10, 83)` —— 而那两行
**没有任何包声明它们**，所以那个数字今天不能登在 artifact 里，C95 的预测就此**落空为不可测**。
`locked=83` 三处读数一格未动。C95 的档与 ledger 第 51 节未改。

## 7. 本轮顺手抓到的三条过期陈述（都已按重读改正）

1. `census-tiingo.json` 的 C96 note 原本写 `crypto_historical.py:103-108` 是「start now-90d」，
   实际 `:105` 是 `relativedelta(years=1)` —— 90 天被这一行的原文证伪，carrier 应为
   `RelativeDefault(years=-1)`。已改。
2. `census-intrinio-2of3.json` 的 C96 note 原本把 `RelativeDefault` 记在 `spec.py:60-130`，
   类实际在 `spec.py:117`（`resolve` 在 `:161`）。已改。
3. 同一条 note 翻转了 `expressible_today`，却没有留任何 **upstream** cite（只有 repo 侧
   `spec.py`/`http_json`），`F6` 当场读成 `carried=0` 变红。补上 `historical_dividends.py:74-78`
   的逐行重读后才绿。这条是装置在起作用，不是文案在起作用。

## 8. 没做完的事

- `OBB2-federal_reserve-OvernightBankFundingRate` 与 `OBB2-tiingo-CryptoHistorical` 仍是
  `shipped_cover`：前者要 domain 注册 + ModelSpec + binding，后者要整个 tiingo 包。都不是引擎能力。
- 本轮**不宣布任何 model 为 DEV_DONE/accepted**：验收要执行见证，是另一波。
- 全量 `make gate` / `test-cov` / `acceptance-probe-check` 未跑（按任务约束）；离线，未发一个请求，
  `OPENDATA_ALLOW_LIVE_E2E` 未设。

复算命令：

```
python scripts/quality/model_capability_census.py --round-id C96 \
  --json docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/capability-roadmap.json
python scripts/quality/model_capability_census.py --round-id C96 --check
python docs/evidence/C96/relative-default-face.py
```
