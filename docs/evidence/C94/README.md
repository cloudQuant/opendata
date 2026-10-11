# C94 — `flow.fan_out` 的 5 行 credit 逐行重审：可复算 credit 从 5 收到 1

本轮不建 kernel。本轮做的是把 C93 交付的那条预测面（「建 `flow.fan_out` 一步解锁 5 行」）
的 5 行，逐行对着 pinned 上游重读一遍，并把读出来的偏差落进 census、重新 derive 阶梯、
再用一台能把 credit 打回去的装置证明位移是真的而不是我手写的一个数。

结论先行：**5 行里 4 行的 need 集在 `fan_out` 这件事上或之外都是错的，幸存者 1 行
（`OBB2-intrinio-EquityInfo`）**。阶梯 credit 因此从 `step=0 newly=5` 变成 `step=4 newly=4`，
其中本轮审过的行从 5 变成 1。

---

## 1. 五条行的实测（每一条 file:line 本轮都在盘上重取过）

上游树：`/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers/`（只作输出契约
与机制参照，不复制代码）。

### 1.1 `OBB2-deribit-FuturesInfo`：fan-out 成立，但只记了 1 个需求（1 → 10）

`deribit/openbb_deribit/models/futures_info.py`：

- `:204` `symbols = query.symbol.split(",")`；`:206` 每个 symbol 一个 `get_ticker_data(symbol)` 任务；
  `:207` 用 `asyncio.as_completed` 消费；`:215` `sorted(result, key=lambda x: symbols.index(...))`
  把结果**按调用方给定的顺序**重新排回去。
- 这条 `:215` 是本轮新增 spelling `input_order_restoration` 的全部理由，而且它记录的不是「需要一个
  新能力」而是「对将要建的 `fan_out` 接缝提的一条要求」：**顺序不保序的实现是错的实现**，
  即便每一行数据都在。上一版 census 的 notes 里已经写了「and re-sorted」，却没为它命名任何需求
  —— 描述先于判据存在，判据没接住，这是本轮我自己要登记的一条。
- fetcher 没有目录就跑不起来：`symbol` 的 field_validator 自己就在做 I/O —— `:47-48`
  `run_async(get_perpetual_symbols/get_futures_symbols)`，`:52-55` 拒绝不在 roster 里的 token，
  `:56-57` 再用运行期字典**改写** perpetual 短名（`helpers.py:226` 建 `{short: instrument_name}`）。
  这是 `flow.preflight_lookup` + `param.value_transform`，不是「query key 改名」。
- `get_instruments` 带 `@alru_cache(maxsize=64)`（`helpers.py:51-52`）→ 目录是有状态的（`local_cache_store`）。
- 响应侧：`:208-209` `{**data, **stats}` 把嵌套 `stats` 拍平进同一条 —— 组合出一个列，不是 explode；
  `:178` `change_percent` 除以 100 且 falsy 透传；`:172` timestamp 是声明式 datetime；`:71`
  `extra="ignore"` 是一份隐式输出白名单；`:67-70` 两个列名来自别名。

### 1.2 `OBB2-sec-LatestFinancialReports`：**`multi_request_fanout` 被证伪**（这条行根本不发 per-symbol 请求）

`sec/.../latest_financial_reports.py`：`:134-138` 是**同一个 URL 模板**换 offset
（`count=100&from=offset`），`:161` 的循环条件是 `n_hits < total_hits`，而 `total_hits` 来自**第一个响应的**
`hits.total.value`（`:153-154`）；ticker 与 CIK 是从每条 hit **里**拿出来的（`:200-210`），它们不选择请求。
所以这不是 fan-out，是**页数由响应决定**的分页 —— 而现有词汇里也没有命名它的 spelling
（`request.body_pagination` 说的是 POST body）。这条登记为词汇面缺口，不折进任何 present 能力。

响应侧另外三条本轮补上：`:201/:205/:215-216/:230` 把 list **收拢成逗号串**（与 `path.explode`
的基数方向相反 → `list_column_comma_join`）；`:236-248` 只有 `startswith/in` 通过才发
`metadata/financial_report`（条件派生，且 `:238` 的 `report_type.startswith` 没有 None 保护）；
`:252-256` 用 seen set 按派生 url 去重 —— 这是 distinct-row 操作，`rows.filter` 的 op 集
（contains/equals/not_equals/in/prefix）**表达不了**，本轮把它登记在 #87 名下而不是折进 present 能力。

### 1.3 `OBB2-intrinio-EquityQuote`：两条**假 present 折叠**（4 → 7）

`intrinio/.../equity_quote.py`：

- `per_record_nested_flatten` 折到 `path.explode`（在册 present），但这里**没有任何东西被 explode** ——
  嵌套 `security` 子模型被当成**一个列**留着（`:60-62`），symbol 从它里面提出（callback `:123`）。
  它需要的是列组合，不是行展开。
- `param_value_mapping` 折到 `param.value_mapping`（在册 present），但 `:84-90` 的
  `VENUES.get(v, v)` 映射的是**输出列的值**、带 raw fallthrough；没有任何查询参数被改写
  （`source` 原样进 URL，`:130`）。
- 旧集合漏掉的两条：`:74-82` `sales_conditions` 先正则剥控制字符再把 `''` 映射成 None；
  `:119-120` callback 对任何非 200 返回 `{}` —— 失败的 symbol 会**静默丢行**而不是抛错。
- fan-out 本身成立：`, split` 后每个 symbol 一条 `/securities/{s}/prices/realtime`（`:129-133`），无分页。

假 present 折叠比假 absent 更贵：absent 只是推迟一行，present 会让这一行**看起来已经能被今天的引擎表达**，
于是它在验收面上永远不会被要求重读。这两条正是本仓 #87/#40 那一族的复发。

### 1.4 `OBB2-tiingo-CurrencyHistorical`：机制改名（`param.value_mapping` → `param.value_transform`）

`tiingo/openbb_tiingo/models/currency_historical.py`（本轮逐行重取，`awk` 原文在盘）：

- `:105-107` 把 `interval` 从 query string 里**排除**；`:109-116` 用后缀手术把它重写成一个**名字都不同的**
  参数（`2m → 2min`、`4h → 4hour`、`1d → 1day`，else 分支落 `1day`）；`:124` 再插值进 URL。
  值表不能改 key 的名字，所以机制是 `param.value_transform`，不是 `param.value_mapping`。
- 新增第二条阻塞：`:142-144` `ticker` 被 pop 之后**只在 `len(symbols) > 1` 时才大写加回** ——
  输出列集合取决于调用方给了几个 symbol，schema 是请求基数依赖的（`:58` 的 alias 让 symbol 列无论如何都在）。
- fan-out 保留：`:120` split、每 symbol 一个 `get_one` 任务、`:153` gather、`:151` extend；
  `:100` 单一 base url，所以没有 interval 分支（这点和 equity 不同）。
- 顺带记下的上游事实：`:146-149` date 按 interval 后缀两臂转换；`:174-177` 客户端按 date 排序；
  `:130-138`+`:161-163` 单 symbol 失败被吞进 `messages` 并和局部结果一起 warn；token 走 query string
  （`:99`、`:124`）；`:101-103` 第一个 `get_querystring` 是死码，`:105` 立刻覆盖掉。

### 1.5 `OBB2-intrinio-EquityInfo`：幸存者，也是本轮唯一「审过且仍被 credit」的行

`equity_info.py:63-68` 用 `amake_requests` 对逗号切开的每个 symbol 发一条 `GET /companies/{symbol}`，
每个响应是一个扁平公司 dict；`:29-31` 输出列名从上游 `ticker` 别名来。needs 只有
`symbol_fan_out` + `field_rename_map`（后者折到 present 的 `columns.rename`），
所以它的**唯一** absent 需求就是 `flow.fan_out`。

---

## 2. 词汇面：新增 5 条 spelling、删除 0 条、canonical 面一条没动

写进 `scripts/quality/model_capability_census.py` 的 `LABEL_TO_CAPABILITY`（在
`# ---- not an engine capability at all` 之前）：

| spelling | 折叠目标 | 目标当前状态 |
| --- | --- | --- |
| `list_column_comma_join` | `columns.compose` | absent |
| `code_table_forward_lookup` | `columns.compose` | absent |
| `nested_object_as_column_value` | `columns.compose` | absent |
| `param_rename_and_drop` | `param.value_transform` | absent |
| `input_order_restoration` | `flow.fan_out` | absent |

这 5 条**全部**折到本轮已 absent 的 canonical 上，所以没有任何一条能靠「新增词汇」把某行抬成可发布 ——
这条约束不是我的意图陈述，它由装置的第 6 臂量出来：canonical `54 → 54`、rows `279 → 279`、
spellings `106 → 111`、removed 必须为空集。若哪天有人加了一条折到 present canonical 的 spelling，
ARM-VOCAB 就会变红。

`input_order_restoration` 折到 `flow.fan_out` 是有意的：它不是第六个需求，而是**对这条接缝的序关系要求**，
把它单独命名是为了让 deribit 那行在 kernel 建成时无法被「行数对了」验收放过。

---

## 3. 阶梯位移与算术闭合（装置读数，逐字取自本轮运行）

```
PRE_REV e2ed7b2 resolves_to=e2ed7b2 census_files=20

ARM-CREDIT pre  flow.fan_out step=0 newly=5 ['OBB2-deribit-FuturesInfo', 'OBB2-intrinio-EquityInfo', 'OBB2-intrinio-EquityQuote', 'OBB2-sec-LatestFinancialReports', 'OBB2-tiingo-CurrencyHistorical']
ARM-CREDIT post flow.fan_out step=4 newly=4 ['OBB2-intrinio-EquityInfo', 'OBB2-intrinio-EtfInfo', 'OBB2-intrinio-IndexHistorical', 'OBB2-intrinio-KeyMetrics']
ARM-CREDIT audited rows credited pre=5 ['OBB2-deribit-FuturesInfo', 'OBB2-intrinio-EquityInfo', 'OBB2-intrinio-EquityQuote', 'OBB2-sec-LatestFinancialReports', 'OBB2-tiingo-CurrencyHistorical']
ARM-CREDIT audited rows credited post=1 ['OBB2-intrinio-EquityInfo']
ARM-ARITH judge mentions 1192->1207 delta=15 | per-row need delta sum=15 | equal=True
ARM-ARITH canonical 54->54 | raw spellings 106->111 | rows 279->279
ARM-VOCAB added=['code_table_forward_lookup', 'input_order_restoration', 'list_column_comma_join', 'nested_object_as_column_value', 'param_rename_and_drop'] removed=[] (removed must be empty)
FACE present capabilities n=13: ['columns.cast', 'columns.derive', 'columns.rename', 'columns.rescale', 'columns.select', 'decode.archive_member', 'decode.delimited', 'flow.composite_key_segment', 'flow.url_template', 'param.value_mapping', 'path.explode', 'rows.filter', 'rows.order_limit']
FACE rows_expressible_today=7 pending=272 prefix=189 still_locked=83
VERDICT credit_moved=True forge_up=True forge_down=True refusal_bites=True arith_identity=True vocab_only_additive=True all=True
```

算术闭合是**独立于 credit 叙述**的一条：judge 的 mention 增量 15 必须等于 5 行 per-row need 增量之和 15。
它把「我改了 notes 但没改判据」「我改了判据但 judge 数到别处」这两种走偏都拦住了。

`census_files=20` 与 `files=19` 不矛盾，本轮对过盘：`census-*` 前缀共 21 个文件，其中
`census-fred-bls-ecb-face.txt` 不是 json，`census-audit.json` 没有 `rows` 键，带 rows 的 json 是 19 个、
rows 合计 279。（上一轮我把这个数当成 21 写在草稿里，那是错的，按盘上重取改掉了。）

## 4. 四条反例臂：每台都必须能不同

```
ARM-FORGE-UP sec row reverted to pre bytes ['per_record_nested_flatten', 'derived_columns', 'multi_request_fanout'] -> newly=4 audited=['OBB2-intrinio-EquityInfo', 'OBB2-sec-LatestFinancialReports'] sec_back_in_credit=True
ARM-FORGE-UP set vs post: gained=['OBB2-sec-LatestFinancialReports'] lost=['OBB2-intrinio-KeyMetrics'] (newly is a cover position, not a count: 4 -> 4)
ARM-FORGE-DOWN EquityInfo += code_table_reverse_lookup (columns.compose, outside the step-4 prefix) -> newly=9 step=6 equityinfo_left_credit=True
ARM-REFUSAL unmapped spelling -> RoadmapError: unmapped census labels on OBB2-tiingo-CurrencyHistorical: ['c94_unmapped_probe_label']
```

- **FORGE-UP**（把被证伪的归因塞回去）第一版是失败的：只往 sec 行**追加**一个 `multi_request_fanout`
  读起来全是 False —— 因为该行本轮另外还多了 3 个 absent 需求，一个 label 复原不了一行。
  改成**按字节整行回滚到 pre-rev**，并把 gained/lost 两个集合打出来，它才真的能不同。
- **FORGE-DOWN** 第一版也是失败的：给 EquityInfo 加 `columns.null_sentinel`，而它落在 step-4 前缀**之内**
  （step 0 就建了），加一个已覆盖的需求不可能把行从 credit 里踢出去；换成 `code_table_reverse_lookup`
  （step 25，前缀之外）才成立。这两条都是「对比臂自己表达不了差异」的旧错形的复发，本轮靠把臂跑红才发现。
- 顺带把 `newly` 的语义钉死在这台装置的输出里：**它是贪心 cover 的位置，不是「需要这个能力的行数」**。
  FORGE-UP 让 4 → 4 而成员集合换了一行，这就是证据。

## 5. 分母面：`fan_out` 的参照面是异质的，所以不给「杠杆」叙述

```
ARM-HETEROGENEITY fan_out reference surface (working tree):
    rows=130
    paged_true=20
    join_needed_true=58
    hardcoded_symbol_roster=29
    input_order_restoration=1
    multi_request_concat=12
    multi_request_fanout=46
    per_item_fanout=35
    symbol_fan_out=46
ARM-HETEROGENEITY pre surface rows=131
```

130 行里只有 29 行是「硬编码 symbol roster 驱动」，58 行还要 join、20 行还要分页。
一个 caller-symbol fan-out 内核直接对上的只是其中一个子集。上一轮我差点把「130 行引用面」当杠杆写完，
本轮把它按机制切成三块登记，并据此**不**给出任何「建一步解锁 N 行」的传播性说法。
（`pre surface rows=131 → post 130` 的 1 是 sec 行被摘掉 `multi_request_fanout` 的结果，可复算。）

## 6. 预测面（带 void 条件与读数解释）

```
PREDICT building flow.fan_out alone flips n=1 ['OBB2-intrinio-EquityInfo']
PREDICT void condition: any of these rows gaining one absent need, or a step-0..3 capability shipping first (then the credit list is a different set: post newly=4)
```

怎么读这条预测：**单独**建 `flow.fan_out`（不动 steps 0-3）今天只翻 1 行。
post 面里另外 3 行（`EtfInfo`/`IndexHistorical`/`KeyMetrics`）共享同一个 credit，但它们本轮**没被逐行审过**，
所以它们是下一轮的复审对象，不是本轮的产出。void 条件写在同一节里：
只要这些行里任何一行拿到一个新的 absent 需求，或者 step 0-3 的某个能力先落地，credit 集合就会换成另一批 ——
届时这条预测作废、必须重derive，而不是继续沿用 n=1。

## 7. 这台装置看不到的东西

```
FLOOR this device cannot see:
    rows sharing the credit that this round did NOT audit: ['OBB2-intrinio-EtfInfo', 'OBB2-intrinio-IndexHistorical', 'OBB2-intrinio-KeyMetrics']
    it reads label folds and set cover, never engine behaviour; no request was made
```

它读的是 label 折叠与贪心 cover，**完全不读引擎行为**，也没发过任何请求。
所以本轮全部证据只到「need 集与阶梯位移是真的」为止；「本仓引擎建出来能不能真的表达这 1 行」不在这台装置的
能力范围内，必须等 kernel 落地后由它自己的 acceptance probe 来判。离线、无网络、无 DDL/DML 的约束本轮照旧。

## 8. 本轮决策：不建 `flow.fan_out` 内核

理由是可复算的，不是手感：**今天它只翻 1 行审过的**，另外 3 行要先落地 steps 0-3 的能力才轮得到它。
这和 C92（`rows.order_limit` 实测 unlock 0 行）与 C93（`flow.branch` 5 行审下来只剩机制各异的碎片）
是同一族判断：先交付**修正后的 need 集 + 一台可重跑的预测装置**，kernel 排在它自己的审计之后。

## 9. C93 装置在 C94 树上的老化读数（不换成 pre-C94 的那笔绿）

```
$ python docs/evidence/C93/flow-branch-need-face.py      # C93_FACE_RC=1，FAIL 8 条
ARM-POST rows=279 unique=279 guard=False files=19 steps=34 pending=272 expressible=7 shipped_cover=0 canonical=54 labels=111 mentions=1207
ARM-POST flow.branch: step=5 unlocks=13 audited_in_it=['OBB2-cboe-IndexSnapshots']
ARM-POST tradier: in_locked_pool=False needs=[] absent_needs=[] n_absent=0 (pre arm in_locked_pool=False)
FAIL flow.branch still unlocks audited rows after the repair: ['OBB2-cboe-IndexSnapshots']
FAIL tradier is one ladder step away although its locked need set reads 0 absent kernels
FAIL tradier's locked need set reads 0 absent kernel(s), so a single step could unlock it and the LOCKED attribution is wrong
FAIL the two kernels this round added to tradier are not both absent blockers: []
FAIL the prefix movement is not the single row: left=['OBB2-deribit-FuturesInfo'] entered=[9 个 oecd 行]
FAIL the ladder gained [...] but the audited rows are newly credited to ['columns.compose', 'flow.nearest_published_value', 'shape.single_record_wrap']
FAIL added spellings 14 != judge mention delta 29
FAIL expected +2 canonicals against the pinned artifact and +2 distinct spellings between the census arms, read 2/7
VERDICT pre_branch_unlocks_audited=5 post_branch_unlocks=13 post_branch_unlocks_audited=1 device_two_sided=True refused=True tradier_absent_needs=0 failures=8
```

8 条分成两类，本轮把类别**判到能指认 face 为止**：

1. **`tradier` 那 3 条是我自己那台 C93 装置的静默默认，不是 census 回归。** C93 的
   `flow-branch-need-face.py:290` 写的是 `post["locked_needs"].get(TRADIER, [])`，而 `:136-137` 的
   `locked_needs` 只从 `rows_still_locked_after_engine_pool` 里取行 —— C94 把 `fan_out` 从 step 0 挪到
   step 4、整个前缀/locked 划分随之重partition，tradier 被 step 上的 `shape.single_record_wrap` credit 走了
   （见上面 `ARM-POST credited step of the audited five`），于是它不在这个池里，`.get` 交出 `[]`。
   盘上真相：`OBB2-tradier-EquitySearch` 的 needs 有 4 条
   （`conditional_endpoint_branch`、`field_rename_map`、`single_object_array_wrap`、`conditional_value_derive`），
   `expressible_today=false`。另外本轮全表扫过：**needs 为空且 `expressible_today=false` 的行 = 0 条**，
   所以 census 侧没有「无原因的 pending 行」这一缺陷。
2. **其余 5 条是生成期 pin 的自然老化**：那些断言把 mention 增量、spellings 增量、canonical 增量、
   前缀成员集合都钉在 C93 那一代（pre-rev 与本仓 working tree 的字节对）上；C94 必然改动 +5 spelling、
   +15 mention、并移动阶梯，所以它们红的方向和 census 是否退化无关。

这条读数按「它描述的那棵树」发布：C93 的 README §9 原先承诺 `FACE_RC=0`，本轮已在原处补上 C94 树的重读，
并把 pre-C94 那笔绿保留为它自己那一代的记录（不删、不改写成今天的数）。

## 10. 我自己本轮错的几条（登记，不追认成结论）

1. **census 目录造了个不存在的子路径** `…/census/`，装置跑了个空输出。判据是 `mcc.CENSUS_DIR_REL`，
   census json 是平铺的。空结果当仪器 bug 处理是对的，但代价是一次白跑。
2. **`REGISTRY` 当 dict 用了**（`mcc.REGISTRY.get(...)` → `AttributeError`）；它是 54 条 `CapabilityEntry` 的 tuple。
3. **产物键猜了两次**（`roadmap["shipped_cover"]`），真名是 `shipped_cover_rows`（int）。第三次改成先把实际键打出来。
4. **两条对比臂第一版都跑不出来**（FORGE-UP 只补 1 个 label、FORGE-DOWN 加的是前缀内的需求），
   属于「反例臂本身不可能不同」的老错形，本轮是**靠把它们跑红**才发现的，不是靠想。
5. **`MYPY_RC=0` 是管道读数**：`| tail` 之后 `$?` 已经不是 mypy 的。重定向到文件后真实读数是 `Found 1 error`。
   同一轮里还留了一行 `if False else None` 死码和一个与返回不符的 `-> None` 注解。
6. C94 草稿里把 census 文件数写成 21，按盘重取是 19（带 rows 的 json）。
7. 承 C93 那条仍未清的债：**每次写完仪器都要立刻跑一次 lint/type**，本轮又是 ruff 9 条 + mypy 1 条才开始对。
8. **新仪器入库前只跑了 ruff+mypy，没跑 bandit**，于是 `import subprocess`（`:35`）少了本仓约定的
   `# nosec B404`，`make a2-check` 直接读 `A2_RC=2 / FAIL bandit`。补上 C92/C93 两台同名仪器都带的
   一行标记后 `A2_RC=0`（`ok ruff check / ok ruff format --check / ok mypy / ok bandit`）。
   这条和第 7 条是同一个缺口的两面：**a2 的四项都要在提交前跑全**，而不是按「这次只改了注释」猜。
   补记时顺手验了改动的影响面：那次 nosec 编辑是纯注释，重跑装置与 `c94_face_final.txt` 仍逐字节相同。

## 11. 复算指引

```
python docs/evidence/C94/fan-out-credit-face.py                       # FACE_RC=0，VERDICT all=True
python scripts/quality/model_capability_census.py --json /tmp/r94.json --round-id C94
cmp /tmp/r94.json docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/capability-roadmap.json
python scripts/quality/model_capability_census.py --check --round-id C94     # verdict: PASS
python docs/evidence/C92/census-repair-control.py                     # CTRL_RC=0，arm-pre=6 arm-post=0
python docs/evidence/C92/row-order-face.py                            # FACE_RC=0，ARTIFACT round=C94 canonical=54
python docs/evidence/C93/flow-branch-need-face.py                     # 预期 RC=1 failures=8（见 §9）
python -m ruff check scripts/quality/model_capability_census.py docs/evidence/C94/fan-out-credit-face.py
python -m mypy scripts/quality/model_capability_census.py docs/evidence/C94/fan-out-credit-face.py
```

本轮在 C94 树上重取到的读数（`/tmp` 未参与产物比较，产物字节由在册 census 重derive）：

```
C94_FACE_RC=0                     # 与本轮前一次运行 diff 为空（确定性：两次逐字节相同）
GEN_RC=0 / CMP_RC=0               # 重derive 173042 字节，与在册那份 cmp 返回 0
CHECK_RC=0 verdict: PASS          # 唯一 UNPROVEN 是既有的 outside.source_absent（无锚点，设计如此）
C92_CTRL_RC=0 / C92_ORDER_RC=0    # C92 两台在 C94 树上仍绿，且自动跟随轮次标签 round=C94
C93_FACE_RC=1 failures=8          # §9，按它描述的那棵树发布
```

装置不依赖任何未提交的临时状态；`PRE_REV = "e2ed7b2"` 是**钉死**的 C93 修复前那一笔，
不是 `HEAD~1` —— 用 `HEAD~1` 的臂会随下一笔提交漂移，那正是本轮 §9 里 C93 装置教的东西。

## 12. 提交前的凭据扫描面（`staged-blob-secret-scan.py`）

本轮要动台账，所以「无凭据」这个 0 必须先证明扫描器是活的。三张面：

- **INVARIANT**：逐个 staged blob 从 index 里取（`git show :path`），
  并要求 `scanned_bytes == sum(git cat-file -s :path)` **逐文件且总量**相等 —— 一条没解析成功的路径
  不能躲在 0 hits 后面。台账的字节数按既有约定不发布（`bytes=WITHHELD`），只发布它被扫到、0 hits。
- **REAL**：`STAGED files=11 scanned_bytes=868652 cat_file_bytes=868652`、`REAL hits=0`。
- **CONTROL（双向）**：
  (a) PLANT —— 往最小的那个 blob **内存里**追加一条合成 token，同一台扫描器 `0 → 1`（盘上字节不动）；
  (b) ALLOW —— AWS **文档示例键对**必须读 0，而同一个 pattern 对非示例的 `AKIA…` 仍然要咬。
  没有 (b)，那个 0 就是靠放宽 allowlist 买来的，而不是靠点名一条字面量。

第一次运行**没有读到 0**，读数发布在这里而不是悄悄改掉：

```
REAL hits=1 (must be 0)
   HIT …/开发验收记录.md aws_access_key offset 325065
VERDICT invariant=True real_zero=False control=True all=False      # SCAN_RC=1
```

查了那条字节到底是什么（掩码回显，只打形状不打值）：它是 `开发验收记录.md:904` 那一条
**「第一次反例臂塞的是 AWS 文档示例对」**里被逐字引用的公开示例凭证对 ——
是台账自己当反例素材写进去的东西，不是泄露。仓内判据 `make secret-check`
（gitleaks 8.30.1，`363 commits`、`~153876239 bytes (153.88 MB)`、`no leaks found`）也是独立的一面。
修法是把这两条字面量**点名**进 `ALLOW_LITERALS`（exact bytes，不让它退化成前缀或正则），
并补上上面那条 (b) 让它必须能反向不同。修后读数：

```
INVARIANT per-file and total scanned==cat-file: True
REAL hits=0 (must be 0)
PLANT blob_bytes=7009 hits_before=0 hits_after=1 -> True
ALLOW aws-docs-example-pair hits=0 -> True; non-example AWS key still bites -> True
VERDICT invariant=True real_zero=True plant=True allow=True allow_two_sided=True all=True   # SCAN_RC=0
```

这条 face 是自指的：总数把**本档自己**也算在内，所以写下数字之后字节就变了 —— `scanned_bytes` 只对「运行那一刻的 index」成立，复算命令会打印当次的总数；不变量（逐文件 `scanned == cat-file`）与 `hits=0` 才是可跨代复算的那一面。

门禁面本轮重取：`A2_RC=0`（`ok ruff check / ok ruff format --check / ok mypy / ok bandit`）、
`make ledger-check` `LEDGERCHECK_RC=0`（`items=130 proven=112 gap=18 unreviewed=0 ticked=112`，
22 group、19 §10 row 全部对齐）。`make evidence-traceability` 在提交前读 `TRACE_RC=2`
（`untracked gaps = 2`：本档与 `fan-out-credit-face.py`），这笔按预期在两笔提交之后重取。

## 13. 这台扫描装置自己的第一条红：提交后重跑抛 ValueError，而不是打印拒绝面

两笔提交落盘后我把同一台装置在**干净工作树**上重跑了一次（此时 index 是空的），读到的是
`SCAN_RC=1` + `ValueError: min() arg is an empty sequence`（`main()` 里 `min(rows, …)` 对空表），
**一条 face 都没打印**。这是我自己仪器的缺陷，不是 census 的：一台会在「调用不满足前提」时崩掉的装置，
分不清「面判红了」和「这次根本不该跑」。

补了前提守卫并**双向验**：空 index 打印 `REFUSED empty index: this device runs BEFORE a commit,
against staged blobs` 并回 `rc=2`（把 2 专门留给前提不成立，与 1=面判红分开）；
再 `git add` 一个文件制造非空 index，装置回到 `SCAN_RC=0 / all=True`。
所以本装置的复算前提是 **index 非空**，即它必须在提交之前跑；提交后想复算就先把待验文件重新 stage。

## 14. 本轮第 10 条自错登记（承 §10 与 §13 的两族）

`fan-out-credit-face.py` 与 `staged-blob-secret-scan.py` 在同一轮里各漏了一次
`import subprocess  # nosec B404`，两次都由 `make a2-check` 才抓到（`A2_RC=2 / FAIL bandit`，
第二次 bandit 指到 `staged-blob-secret-scan.py line 31`）。结论不是「记得加 marker」，
而是：**ruff 与 mypy 过不了的错我会立刻看见，bandit 过的错我不会** —— 新文件的 marker 检查必须并进
「写完就跑 a2 四项」这一步，而不是留给门禁。扫描装置的第一版还把「总数」写进自指集合（§12 已披露），
以及 §13 的崩掉代替拒绝面，都是同一族：**装置的失败模式没有当成 face 来设计**。


