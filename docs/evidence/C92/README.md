# C92 — `rows.order_limit` 落档：判定探针 + counterfact 反例 + census 修复的两臂对照

- 日期（UTC）：`2026-10-11T17:22:34Z`
- 分支 / 提交：`dev` / `5c8cd61`（本轮代码入提交，`git rev-parse --short HEAD` 现场读数）
- 更正读数：§3 的判定面与 §5.3/§5.4 两条是在 `2026-10-11T17:31Z` 之后用 `git show` 读两棵树量到的，
  当时 `HEAD` 仍是 `5c8cd61`（本档未入提交），所以 §3 那批数与本档前两段那批数出自**同一棵工作树**。
- 前态锚：`5c8cd61^` = `3ba3ba3`（C91 的 census 字节，`census-repair-control.py` 里钉死，不用 `HEAD~1`）
- 网络 / 授权面：全部离线。未发一个请求，未设 `OPENDATA_ALLOW_LIVE_E2E`，无 DDL/DML，未 `git push`。
  `row-order-face.py` 把引擎的真实读路径 `_http_get_json` 换成「一被调用就 AssertionError」的函数，
  所以绿色是结构性的离线绿色，不是我把 URL 注释掉了。

## 1. 本轮改了什么（一句话）

引擎新增 `RowOrderSpec` + `apply_row_order`（`fetch_pages → resolve_rows/_expand_rows →
总量对账 → apply_row_filters → **apply_row_order** → normalize_record`），
`federal_reserve/MoneyMeasures` 成为第一处生产声明；census 里 6 行被逐行审回并把缺的接缝记成
absent canonical，其中一行暴露出一个此前没有任何 label 命名的机制（只截断、不排序）。

## 2. 两台仪器与它们的现场读数

### `row-order-face.py`（判定探针 + 反例臂）

```
$ python docs/evidence/C92/row-order-face.py     # FACE_RC=0
ARM-DECLARE judge count=1 modules=162 carrier={'opendata/data/providers/federal_reserve/specs.py': 1}
ARM-DECLARE registry status=present anchor_found=True cross_anchor_found=True anchor_line=561 entry_claim=False read_from_disk=True
ARM-DECLARE live-object model=MoneyMeasures order_by=RowOrderSpec(column='Time Period', direction='asc', limit=None, limit_param='')
ARM-DECLARE fed carrier_count=1 declared='Time Period' 'asc' limit=None limit_param=''
ARM-PIPELINE published=['MSFT', 'AAPL', 'GE', 'XOM', 'BRK']
ARM-PIPELINE asc=['GE', 'XOM', 'MSFT', 'AAPL', 'BRK'] desc=['AAPL', 'MSFT', 'GE', 'XOM', 'BRK']
ARM-PIPELINE cap-literal-2=['AAPL', 'MSFT'] cap-param-2=['GE', 'XOM']
ARM-COUNTERFACT stage-removed asc=['MSFT', 'AAPL', 'GE', 'XOM', 'BRK'] desc=['MSFT', 'AAPL', 'GE', 'XOM', 'BRK'] directions_collapse=True matches_published=True
ARM-REFUSAL paging-without-order builds; paging+order refused blamed_order_by=True: FaceProbe: order_by 'weight' cannot be honoured across a offset answer: the stage orders the records of one response, so
ARM-PRESENCE real=present forged=absent flipped=True
ARTIFACT round=C92 status=present declarations=1 shipped_cover=0 built_but_unused=['columns.cast', 'columns.rescale', 'decode.archive_member', 'flow.composite_key_segment', 'param.value_mapping'] canonical=52
VERDICT declared=1 status=present pipeline_ordered=True stage_removal_collapses=True paging_cap_refused=True presence_flippable=True failures=0
```

五条臂各自管一件事，缺一不可：

- **ARM-DECLARE** 是两条独立证据叠在一起：本仓判据 `count_declarations` 的 AST 计数（1 处，
  归属文件就是 `federal_reserve/specs.py`），**以及** import 真实声明对象后引擎自己看到的
  `RowOrderSpec(column='Time Period', direction='asc')`。文本 grep 与活对象是两种证人。
- **ARM-PIPELINE** 走的是真 fetcher + 录制 transport，asc/desc/字面 cap/参数 cap 四个读数。
  发布序 `MSFT, AAPL, GE, XOM, BRK` 故意既不正序也不倒序，两个方向才各有一次「必须不同」的目标。
- **ARM-COUNTERFACT** 把 `apply_row_order` 换成直通：两个方向同时塌回发布序。这条臂的存在
  意味着 ARM-PIPELINE 的绿色不是「断言恰好与发布序相同」——若两臂读数相同，仪器自己判 FAIL。
- **ARM-REFUSAL** 是双臂：不带 `order_by` 的合法分页 spec **必须构造成功**，带 `order_by` 才拒，
  且拒绝信息必须点名 `order_by`。只跑「拒了」的一臂会把自己骗过去：第一版我就是这么错的
  （见 §5）。
- **ARM-PRESENCE** 把 `rows.order_limit` 条目的锚换成一个文件里不存在的 token，`verify_registry`
  必须改读 `absent`。它证明 `present` 是从磁盘读的，不是我 `claim=True` 喊出来的
  （该条目 `entry_claim=False`、`read_from_disk=True` 也一并打印）。

`ARM-DECLARE judge count` 的判据写成 `>= 1` 而不是 `== 1`：下一轮再有模型声明排序，这个数就会涨，
一个轮内常量只会变成下一轮必须修的 pin（§47 记过 C90 那台仪器的 13 条这种债）。

### `census-repair-control.py`（census 修复的两臂对照）

```
$ python docs/evidence/C92/census-repair-control.py   # CTRL_RC=0
PIN pre-repair rev=5c8cd61^ resolves_to=3ba3ba3
ARM-PRE census_files=20 shipped_cover=6 ids=['OBB2-econdb-AvailableIndicators', 'OBB2-federal_reserve-FederalFundsRate', 'OBB2-federal_reserve-OvernightBankFundingRate', 'OBB2-federal_reserve-SOFR', 'OBB2-intrinio-SearchAttributes', 'OBB2-tiingo-CryptoHistorical']
ARM-PRE need-count={'OBB2-econdb-AvailableIndicators': 4, 'OBB2-intrinio-SearchAttributes': 2, 'OBB2-federal_reserve-FederalFundsRate': 2, 'OBB2-federal_reserve-OvernightBankFundingRate': 2, 'OBB2-tiingo-CryptoHistorical': 4}
ARM-POST shipped_cover=0 ids=[]
CONTROL pre-reproduces-defect=True post_empties_bucket=True
VERDICT arm-pre=6 arm-post=0 movement=6 failures=0
```

两臂只换 census 目录，registry / 声明扫描 / 判据全是当前树的同一份：`shipped_cover` 6 → 0。
这条面量的是「内核注册之后、行级复核之前」那个中间态（前态 census 字节 × 当前树判据）：它一度把
6 行判成已覆盖待发，而本轮的行级修复才是清空它的原因——反例臂就是前态字节今天仍复现 6。
**而两份在册 artifact 自己读到的 `shipped_cover_rows` 都是 0**（C91 那棵树里 `rows.order_limit`
仍是 absent，6 行因此挂在 pending 上、不在这个桶里），所以这个 6 不是「在册文件 0 → 6 → 0」的
一段轨迹，别那样读它。

## 3. 六行 census 的逐行修复（字段级 before → after）

从 `git show 5c8cd61^` 与工作树字节逐行比出来的读数；`notes` 每行都加了 cite 句，故只记长度。

| task_id | needs（前态） | needs（现态） | 新增/替换的接缝 |
| --- | --- | --- | --- |
| `OBB2-federal_reserve-SOFR` | `published_value_rescale, client_side_sort` | `+ sentinel_value_nulling, relative_date_default` | `sofr.py:44-52` 的哨兵串转 None；`:62-70` 按今天算窗口 |
| `OBB2-federal_reserve-FederalFundsRate` | 同上 | `+ sentinel_value_nulling, relative_date_default` | `federal_funds_rate.py:55-62` / `:95-105` |
| `OBB2-federal_reserve-OvernightBankFundingRate` | 同上 | `+ relative_date_default` | `overnight_bank_funding_rate.py:56-62`；哨兵那条我**没加**，见 §5 |
| `OBB2-tiingo-CryptoHistorical` | `nested_array_explode, client_side_sort, param_value_mapping, date_format` | `+ relative_date_default` | `crypto_historical.py:100-110` 条件式起始日 |
| `OBB2-econdb-AvailableIndicators` | `csv_decoder, derived_columns, client_side_row_filter, client_side_sort` | `+ sentinel_value_nulling` | `econdb/utils/helpers.py:476-484` 把 `"NA"` 类值置空 |
| `OBB2-intrinio-SearchAttributes` | `client_side_sort_limit, field_rename_map` | `client_side_sort_limit` → **`source_order_cap`** | `search_attributes.py:60-66` `data['tags'][:query.limit]` 只截断不排序 |

六行的 `expressible_today` 一个都没翻（前态与现态都是 `False`），总量面上
`rows_expressible_today 7 → 7`、`rows_pending 272 → 272`——本轮清空的是**过度声明的桶**，
不是发布了一行新的可表达能力。判定面：`git show` 读 `3ba3ba3`（= `5c8cd61^`，仪器里钉死的不是
`HEAD~1`）与 `HEAD` 两份在册 `capability-roadmap.json`，同一台判据、不同棵树：

```
round C91 -> C92；capability_registry 条目 51 -> 52
present 12 -> 13: 新增 = ['rows.order_limit']
absent 38 -> 38: + ['rows.cap_source_order'] - ['rows.order_limit']
drifted 0 -> 0、unanchored 1 -> 1（两者都只有 outside.source_absent）
canonical 51 -> 52、labels_supported 104 -> 105、raw_label_count 103 -> 104
raw_label_mention_count 1171 -> 1178（+7），逐 label 的 mention 面同向闭合：
  null_sentinel 35->38（+3）、param.relative_default 1->5（+4）、rows.order_limit 39->38（-1）、
  rows.cap_source_order 无->1（+1）＝ +7；per-capability 总量 1080 -> 1087
declaration 面：rows.order_limit 无 key（判据读 0）-> {'federal_reserve/specs.py': 1}
                rows.cap_source_order 无 key -> 无 key（0 声明；注册即 built_but_unused 的前置态）
built_but_unused：5 名不变；rows.order_limit 因本轮有了生产声明而**离开**这张榜
shipped_cover_rows 0 -> 0（在册两棵树都是 0；6 只出现在 §2 的两臂对照里，见 §5.3）
row_count 279 == 279、unique_task_id 279、guard_fired False、census files 19 -> 19
greedy steps 28 -> 28，但 step0 换了身份（见 §5.3）：
  前态 step0=rows.order_limit newly=6 cum=6 / step1=flow.branch 5 / step2=flow.fan_out 15
  现态 step0=flow.branch newly=5 cum=5 / step1=flow.fan_out 15 / step2=flow.join 12
rows_unlocked_by_greedy_prefix 181 -> 181（阶梯重排，总量未动）
modules_scanned 162、rows_outside_engine 82、rows_still_locked_after_engine_pool 91、
rows_with_no_engine_work_possible 12、rows_with_empty_need_set 0
```

这批数**不依赖本档那两台仪器**，也不依赖我当时的临时装置——在册 artifact 本身就在仓里，读它就行。
复算口径（把 `3ba3ba3` 换成 `HEAD` 即读现态；解释器 `/Users/yunjinqi/opt/anaconda3/bin/python`）：

```
git show 3ba3ba3:docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/capability-roadmap.json \
  | python -c 'import json,sys; a=json.load(sys.stdin); r=a["roadmap"]; \
      print(a["round_id"], len(r["shipped_cover_rows"]), \
            [(s["capability"], len(s["newly_unlocked_rows"]), s["cumulative_rows_unlocked"]) \
             for s in r["greedy_build_order"][:3]])'
# C91 0 [('rows.order_limit', 6, 6), ('flow.branch', 5, 11), ('flow.fan_out', 15, 26)]
# C92 0 [('flow.branch', 5, 5), ('flow.fan_out', 15, 20), ('flow.join', 12, 32)]
```

## 4. 反例臂与测试面

```
$ python -m pytest tests/test_model_capability_census.py tests/test_engine_row_order.py \
      tests/test_provider_federal_reserve.py tests/test_engine_row_expansion_spec.py \
      tests/test_upstream_model_specs.py --no-cov -q        # T_RC=0 → 119 passed
$ make test-cov    # TESTCOV_RC=0 → 22494 passed, 148 skipped in 531.87s (0:08:51)
                   # Required test coverage of 84% reached. Total coverage: 89.92%
```

`22494 = 22459 (§47) + 35`：新档 `tests/test_engine_row_order.py` 34 例 + fed 的 1 例。
测试档内的反例是 `TestSequenceChanges::test_counterfact_a_skipped_stage_cannot_pass_either_direction`，
它与 §2 那条 ARM-COUNTERFACT 是同一条主张的两个载体（ pytest 侧 / 归档侧）。

仪器自身的反证（**没有正向对照的绿色不算绿色**）：把 `row-order-face.py` 的两条判据分别改掉再跑，
必须翻红，跑完字节还原。

```
CONTROL clean_rc=0 tampered_rc=1 differs=True restored=True
TAMPER FAILS: FAIL top-2 read ['AAPL', 'MSFT'] != ['AAPL', 'MSFT']
            || FAIL presence survived a forged anchor: the entry asserts rather than measures
TAMPER VERDICT: VERDICT declared=1 status=present pipeline_ordered=True stage_removal_collapses=True paging_cap_refused=True presence_flippable=False failures=2
```

（第一条 FAIL 的文字看起来像「两个列表相等却报错」，因为被改的就是那句里的期望值——这是
植入体的文字，不是判据的文字。能翻面就够判，差值登记在此不抹平。）

## 5. 本轮我错的三条 + 本轮修掉的旧错一条，留档不抹

1. **第一版 ARM-REFUSAL 是假臂。** 它写成 `PaginationSpec(kind="page")`，而这个 spec 自身就不合法
   （`page pagination requires limit_key`），拒绝来自分页声明而不是 `order_by` 边界——只读「拒了」
   的绿色会把一条不存在的守卫发表成存在的守卫。补法是把臂做成两条：不带 order_by 的合法分页
   **必须建得起来**，带 order_by 才拒且消息必须点名 `order_by`，否则记 FAIL。
2. **OBFR 的哨兵那条我没加。** 子代理回执说 SOFR/FederalFundsRate/OBFR 三行同形，但
   `overnight_bank_funding_rate.py` 的 `normalize_percent` 只挡 `None`，没有字符串哨兵转空这一步，
   所以 `sentinel_value_nulling` 对 OBFR 是无中生有；那一行只补了 `relative_date_default`。
   回执是线索，判定要自己重跑一遍判据 + 自己读上游。
3. **§3 那两句话我写错了，`git show` 读两棵树的在册 artifact 后翻红，已按实测改写。**
   我发出的原句是「`shipped_cover_rows 6 → 0`」和「greedy steps 28 → 28（**step0 仍是 flow.branch**）」。
   实测：两份在册文件的 `shipped_cover_rows` 都是 **0 → 0**，那个 6 只存在于 §2 对照仪的前态臂
   （前态 census 字节 × 当前树判据）；而 step0 **换了身份**——前态在册阶梯是
   `rows.order_limit(6) → flow.branch(5) → flow.fan_out(15)`，现态是
   `flow.branch(5) → flow.fan_out(15) → flow.join(12)`，`rows_unlocked_by_greedy_prefix` 仍是 181。
   两条错向同一个根因：我把「我自己仪器打出的那个 6」当成了「在册文件轨迹上的一个读数」，
   又在写 step0 时只看了当前树、没回头看前态树。被推翻的数字留在上面不抹。
4. **旧错一条（本轮修掉，不是本轮犯的）**：`rows.order_limit` 在前态树里锚在
   `http_json.py:671 def _page_request`——一个分页函数，不是行序的载体。当时条目是
   `claim=False / read_from_disk=False`，所以判据没被它骗出假 `present`，但它也没量到过任何真的东西：
   一条 absent 断言配一个不相干的锚 = 只有一句叙述。本轮换成 `spec.py:561
   order_by: RowOrderSpec | None = None` 并 `read_from_disk=True`，`cross_anchor=def apply_row_order(`，
   §2 的 ARM-PRESENCE 证明这个 `present` 现在是从磁盘读的（锚换成假 token 即翻 `absent`）。

## 6. 已知缺陷：本轮登记、未修

`RowOrderSpec.column` 的构造期校验接受**输出列名**（如 `month`），而 `apply_row_order` 读的是
normalize 之前的原始 record key。于是声明 `column="month"` 在建模时通过、在运行时看不见任何
cell，所有记录并列 ⇒ 保持发布序 ⇒ 结果看起来「合法」，静默 no-op。本轮只把这条写在这里，
没有改判据：改它要么让校验只认 `source_key`，要么让 stage 双语义可读，两条路都要先量一遍
现有声明的影响面。相关词汇面债仍开着：#85（38 行 `param_value_mapping` 里 36 行未经上游 cite 复核）、
#86（没有 label 命名「调用方名↔上游名别名」）、#87（`date_window_filter → rows.filter` 声称了
`RowFilterOp` 没有的序比较算子）。
