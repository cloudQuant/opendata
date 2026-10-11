# C93 — `flow.branch` 的 5 行 need 集逐行审完：判定面是「建这个 kernel  ship 审过的 0 行」

- 日期（UTC）：`2026-10-11T18:21Z`（`date -u` 现场读数）
- 分支：`dev`；读数取自以 `dd11d42` 为前态锚的**工作树**（`git rev-parse --short HEAD` 现场读数
  = `dd11d42`，即本档写下时在册的最后一笔提交）。本档每个数都描述这棵树，不描述任何之后的提交。
- 前态锚：`dd11d42` = C92 的 census 字节（`flow-branch-need-face.py` 里钉死这个 rev，不写 `HEAD~1`；
  §4 的 `T-HEAD` 一臂就是这条理由的证人）
- 网络 / 授权面：全部离线。未发一个请求，未设 `OPENDATA_ALLOW_LIVE_E2E`，无 DDL/DML，未 `git push`，
  未跑 gate member 11（probe self-test，约 1300 s），未跑 350 行 live 轴（授权门）。
  所有读数来自本仓判据 `scripts/quality/model_capability_census.py` 与只读 `git show`。

## 1. 本轮改了什么（一句话）

`flow.branch` 在册阶梯上被记成「step 0，一次 ship 5 行」的接缝。本轮把这 5 行逐行对上游复核了一遍，
给它们补了 **14 条**此前未记的 need spelling（3/4/2/4/1；这 14 次提及涉及 12 个 distinct 拼写，
其中 10 个词汇表里本来就有、只是这五行没记，2 个是新拼写，且 `pre_vocab − post_vocab` 实测为空——
本轮一条拼写都没删），其中两条没有在册 canonical 可归，
于是注册了两个新的 gap 条目 `shape.single_record_wrap`（锚 `http_json.py:434`）与
`flow.nearest_published_value`（锚 `http_json.py:764`），两条都是 `claim=False /
read_from_disk=False / declarations=0`。判定的结论不是把 flag 翻一下，而是：**建 `flow.branch`
对这 5 行 ship 0 行**；它现在的 credit 换成了 10 行未审过的行。本轮不建这个 kernel。

## 2. 仪器与它的现场读数

```
$ python docs/evidence/C93/flow-branch-need-face.py      # FACE_RC=0（两次运行字节相同）
PIN pre-repair rev=dd11d42 resolves_to=dd11d42 (HEAD=dd11d42)
ARM-VOCAB flow.nearest_published_value: status=absent anchor_found=True anchor_line=764 claim=False read_from_disk=False declarations=0
ARM-VOCAB shape.single_record_wrap: status=absent anchor_found=True anchor_line=434 claim=False read_from_disk=False declarations=0
ARM-VOCAB ModelSpec fields n=26 probe_keywords=['nearest_from_response', 'wrap_single_record'] probe_names_a_field=False
ARM-DEVICE real-anchor=['present', 'present'] forged-anchor=['absent', 'absent']
ARM-LADDER materialized census files=20 from dd11d42
ARM-PRE  rows=279 unique=279 guard=False files=19 steps=28 pending=272 expressible=7 shipped_cover=0 canonical=54 labels=104 mentions=1178
ARM-PRE  flow.branch: step=0 unlocks=5 audited_in_it=['OBB2-cboe-IndexSnapshots', 'OBB2-federal_reserve-CentralBankHoldings', 'OBB2-fred-FredRegional', 'OBB2-tradier-EquitySearch', 'OBB2-tradingeconomics-EconomicCalendar']
ARM-POST rows=279 unique=279 guard=False files=19 steps=30 pending=272 expressible=7 shipped_cover=0 canonical=54 labels=106 mentions=1192
ARM-POST flow.branch: step=1 unlocks=10 audited_in_it=[]
ARM-PRE flow.branch ids=['OBB2-cboe-IndexSnapshots', 'OBB2-federal_reserve-CentralBankHoldings', 'OBB2-fred-FredRegional', 'OBB2-tradier-EquitySearch', 'OBB2-tradingeconomics-EconomicCalendar']
ARM-POST flow.branch ids=['OBB2-alpha_vantage-EquityHistorical', 'OBB2-alpha_vantage-EtfHistorical', 'OBB2-congress_gov-CongressBills', 'OBB2-federal_reserve-PrimaryDealerFails', 'OBB2-federal_reserve-PrimaryDealerPositioning', 'OBB2-intrinio-ForwardPeEstimates', 'OBB2-tiingo-EquityHistorical', 'OBB2-tiingo-EtfHistorical', 'OBB2-tradier-EquityHistorical', 'OBB2-tradier-EtfHistorical']
ARM-POST credited step of the audited five={'OBB2-cboe-IndexSnapshots': 'columns.null_sentinel', 'OBB2-federal_reserve-CentralBankHoldings': 'flow.nearest_published_value', 'OBB2-fred-FredRegional': 'rows.filter_comparison', 'OBB2-tradier-EquitySearch': 'LOCKED (no single step)', 'OBB2-tradingeconomics-EconomicCalendar': 'columns.compose'}
CONTROL prefix union pre=181 post=180 left=['OBB2-tradier-EquitySearch'] entered=[]
ARM-POST tradier: in_locked_pool=True needs=['columns.rename', 'derive.conditional', 'flow.branch', 'shape.single_record_wrap'] absent_needs=['derive.conditional', 'flow.branch', 'shape.single_record_wrap'] n_absent=3 (pre arm in_locked_pool=False)
CONTROL ladder composition steps 28->30 entered=['columns.compose', 'flow.nearest_published_value'] left=[] promoted_by_audited=['columns.compose', 'flow.nearest_published_value']
ARM-IDENTITY per-row added spellings={'OBB2-fred-FredRegional': 3, 'OBB2-federal_reserve-CentralBankHoldings': 4, 'OBB2-tradier-EquitySearch': 2, 'OBB2-tradingeconomics-EconomicCalendar': 4, 'OBB2-cboe-IndexSnapshots': 1} total=14 judge mention delta=14 distinct-spelling delta=2 registry_shared_across_arms=True (canonical=54 both arms)
ARM-IDENTITY artifact canonical at dd11d42=52 -> tree=54 delta=2 | artifact registry size=52 -> tree registry size=54
ARM-IDENTITY closes=True two_new_canonicals=True
ARM-REFUSAL spellings-only   aborted: "registry names capabilities no census label maps to: ['flow.nearest_published_value', 'shape.single_record_wrap']"
ARM-REFUSAL spellings+entries aborted: "unmapped census labels on OBB2-federal_reserve-CentralBankHoldings: ['nearest_published_date_snap']"
ARM-REFUSAL restored rows=279/279 labels_supported=107/107 fully_restored=True
VERDICT pre_branch_unlocks_audited=5 post_branch_unlocks=10 post_branch_unlocks_audited=0 device_two_sided=True refused=True tradier_absent_needs=3 failures=0
```

五条臂各管一件事：

- **ARM-VOCAB** 两个新条目必须 `absent` 且锚在盘上找得到（434 / 764 是 `verify_registry` 报的行号），
  `declarations=0`；再加一条独立证人：import 活的 `ModelSpec`（26 个字段），两个声明探针
  `wrap_single_record` / `nearest_from_response` 都不是字段名。若哪天有人真把字段建出来，
  `probe_names_a_field=True` 就把这条臂拉红——`absent` 不许是喊出来的。
- **ARM-DEVICE** 双臂：把两个条目改挂到一个**真在盘上**的锚（`spec.py` 的
  `order_by: RowOrderSpec | None = None`，C92 那轮 ship 的字段）必须翻成 `present`；
  挂到一个文件里不存在的伪造锚必须翻回 `absent`。只读一侧的 `absent` 不是测量。
- **ARM-LADDER** 只换 census 目录：`dd11d42` 的 census 字节 vs 工作树字节，registry / 声明扫描 /
  判据是同一份。population 面（rows 279 / unique 279 / guard False / files 19 / pending 272 /
  expressible 7 / shipped_cover 0）两臂相同 ⇒ 阶梯的移动只可能来自 need 集。
  `materialized census files=20` 是 glob 命中的字节数，判据读的是其中 19 个行文件
  （`census-audit.json` 按名字排除），两个数都印出来是防「20 个文件都当成行文件」这种误读。
- **ARM-IDENTITY** 词汇闭合：逐行 diff 两棵 census 字节得到的新增 spelling 总数 14，必须等于判据
  自己报的 `raw_label_mention_count` 差；distinct spelling 差 2 必须等于 canonical 差 2，而 canonical
  差是对**钉住的 pre artifact 字节**量的（52 → 54），不是对两臂量的——两臂共享同一份 registry，
  那条臂在那里结构上不可能翻面（§7 第 2 条记了这个错）。
- **ARM-REFUSAL** 两条：内存里删掉两条 spelling，判据必须拒跑；把 spelling 与 registry 条目一起删，
  也必须拒（换成 unmapped-label 守卫）。事后恢复的 run 必须与 ARM-POST 读同一个面
  （rows 279/279、labels_supported 107/107），否则这轮仪器就污染了在册判据。

## 3. 判定面 diff：两棵树的在册 artifact

```
git show dd11d42:docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/capability-roadmap.json \
  | python -c 'import json,sys,collections; a=json.load(sys.stdin); r=a["roadmap"]; \
      print(a["round_id"], len(a["capability_registry"]), \
            collections.Counter(v["status"] for v in a["capability_registry"].values()), \
            a["taxonomy"], [(s["capability"], s["rows_newly_unlocked"]) for s in r["greedy_build_order"][:2]], \
            r["rows_unlocked_by_greedy_prefix"], len(r["rows_still_locked_after_engine_pool"]))'
# C92 52 Counter({'present': 13, 'absent': 38, 'unanchored': 1}) canonical=52 labels=105 raw=104 mentions=1178
#    [('flow.branch', 5), ('flow.fan_out', 15)] prefix=181 locked=91
# C93 54 Counter({'present': 13, 'absent': 40, 'unanchored': 1}) canonical=54 labels=107 raw=106 mentions=1192
#    [('flow.fan_out', 5), ('flow.branch', 10)] prefix=180 locked=92
```

| 面 | dd11d42（C92） | 工作树（C93） | 差 |
|---|---|---|---|
| registry 条目 / canonical | 52 | 54 | +2 |
| status present | 13 | 13 | 0 |
| status absent | 38 | 40 | +2 |
| status unanchored | 1 | 1 | 0（`outside.source_absent`，本轮未动） |
| labels_supported | 105 | 107 | +2 |
| raw_label_count | 104 | 106 | +2 |
| raw_label_mention_count | 1178 | 1192 | **+14**（= 逐行 diff 的和） |
| greedy steps | 28 | 30 | +2，`entered=[columns.compose, flow.nearest_published_value]` `left=[]` |
| flow.branch 的位置 / credit | step 0 / 5 行（全是本轮审过的） | step 1 / **10 行（审过的 0 行）** | 判定面 |
| rows_unlocked_by_greedy_prefix | 181 | 180 | −1：只有 tradier 走不出去 |
| rows_still_locked_after_engine_pool | 91 | 92 | +1：tradier 进锁池 |
| shipped_cover_rows | 0 | 0 | 0（C92 清掉的桶没有回流） |
| built_but_unused | 5 | 5 | 同一份 5 条 |
| 行面 | rows 279 / unique 279 / guard False / files 19 | 同上 | 0 |

阶梯重排不是我改的顺序：`flow.fan_out` 现在以 step 0（newly=5）排在前面，`flow.branch` 落到 step 1
（newly=10、cum=15）。`columns.compose`（step 26，newly=1 = te 那一行）与
`flow.nearest_published_value`（step 28，newly=1 = fed CBH 那一行）是本轮新进阶梯的两个接缝；
审过的 5 行里剩下的两行被已有阶梯成员接走（cboe → `columns.null_sentinel` step 3 newly=9，
fred → `rows.filter_comparison` step 21 newly=4）。仪器把 `entered == promoted_by_audited` 当成一条
恒等式判：加 need 不可能让一个 step 退役，所以 `left` 必须是空集。

在册 artifact 的字节可复算：`model_capability_census.py --json … --round-id C93` 重生成 170978 字节，
与树里那份 `cmp` 相同（两次）；`--check --round-id C93` → `verdict: PASS`（rc=0）。

## 4. 反例面：把「被量的东西」改一行，仪器必须翻红

四条单行补丁应用到 /tmp 的副本上（在册文件的字节不动；副本只额外把 `REPO_ROOT`
那行钉成绝对路径——那是管道路不是判据，所以逐条打印改动行）。判据全部走
`flow-branch-need-face.py` 自己的 `main()`，退出码就是判定。

```
$ python /tmp/c93_tamper_harness.py                      # TAMPER_RC=0（四例都读到了期望的颜色）
BASELINE rc=0 rc_is_verdict_line=True

T-HEAD expected=green rc=0 went_red=False as_expected=True
  patch=PRE_REPAIR_REV: Final = "HEAD"
  fail_lines=0
T-C91 expected=red rc=1 went_red=True as_expected=True
  patch=PRE_REPAIR_REV: Final = "3ba3ba3"
  fail_lines=5
  || FAIL an arm reads a non-empty shipped-cover bucket; the over-claim face is back
  || FAIL the prefix movement is not the single row: left=['OBB2-tradier-EquitySearch'] entered=['OBB2-econdb-AvailableIndicators', 'OBB2-federal_reserve-FederalFundsRate', 'OBB2-federal_reserve-
  || FAIL the ladder gained ['columns.compose', 'flow.nearest_published_value', 'rows.cap_source_order'] but the audited rows are newly credited to ['columns.compose', 'flow.nearest_published_val
T-FORGE expected=red rc=1 went_red=True as_expected=True
  patch=FORGED_ANCHOR: Final = "order_by: RowOrderSpec | None = None"
  fail_lines=1
  || FAIL a forged anchor still reads present: the reading is assertable, not measured
T-DTAU expected=red rc=1 went_red=True as_expected=True
  patch=mention_delta = post["mentions"] - pre["mentions"] + 1
  fail_lines=1
  || FAIL added spellings 14 != judge mention delta 15

TAMPER SUMMARY baseline_rc=0 cases=4 unexpected=0
```

`T-C91` 把前态锚再往前挪两棵树（C91 的 census 字节，`3ba3ba3`），一次翻红 5 条：shipped-cover 桶
回流、prefix 移动不止一行、阶梯多收了 `rows.cap_source_order`。这条是「钉锚」的正面对照——
锚错了，本轮的主张就不成立，仪器自己会拒绝点头。

### 4.1 提交后复跑：`T-HEAD` 按 §7.4 的预测翻红了

`e14c4bd` 把 `HEAD` 从 `dd11d42` 挪走之后，同一台仪器与同一套四例反例在新树上各重跑一次。
**判定仪器仍绿，且与提交前那次的输出只差第 1 行的 `HEAD` 标签**（`diff` 只报 `1c1`：
`(HEAD=dd11d42)` → `(HEAD=e14c4bd)`，其余 22 行（全文共 23 行）逐字相同）——这正是钉 rev 的作用：测量面不随 HEAD 漂移。

```
$ python docs/evidence/C93/flow-branch-need-face.py      # FACE_RC=0（提交后复跑）
$ python /tmp/c93_tamper_harness.py                      # TAMPER_RC=1，unexpected=1
T-HEAD expected=green rc=1 went_red=True as_expected=False
  patch=PRE_REPAIR_REV: Final = "HEAD"
  fail_lines=3
  || FAIL the pre-repair bytes no longer credit flow.branch with exactly the audited five, they read ['OBB2-alpha_vantage-EquityHistorical', …]
  || FAIL the prefix movement is not the single row: left=[] entered=[]
  || FAIL expected +2 canonicals against the pinned artifact and +2 distinct spellings between the census arms, read 0/0
T-C91 expected=red rc=1 went_red=True as_expected=True
  fail_lines=5
T-FORGE expected=red rc=1 went_red=True as_expected=True
  fail_lines=1
T-DTAU expected=red rc=1 went_red=True as_expected=True
  fail_lines=1
TAMPER SUMMARY baseline_rc=0 cases=4 unexpected=1
PROBLEM T-HEAD expected green, read rc=1
```

`unexpected=1` 是**反例装置自己的期望过期**，不是判据变红：`T-HEAD` 那一例的 `expected=green` 写在
`HEAD` 仍等于 `dd11d42` 的树上，`HEAD` 一漂移，pre 臂就读成了 post 臂（三条 FAIL 全是「两臂塌成同一批字节」
的面目），于是它翻成本轮真正应有的颜色。这条读数就是 §7.4 那条自指的实测兑现：**下一轮如果再用这台仪器，
`PRE_REPAIR_REV` 必须换成新的 rev，`T-HEAD` 一例会从「绿色的证人」变成「红色的反例」**——两种颜色都在替
「不许用 `HEAD~1` 当锚」这条规则作证。上面 §4 那批 `cases=4 unexpected=0` 的读数描述 `dd11d42` 的树，
这批描述 `e14c4bd` 的树，两个值都在册、不互相替换。

## 5. 逐行 before → after（need 集与上游 cite）

cite 全部落在 `providers/<pkg>/openbb_<pkg>/…`（上游对照树，只读，不抄代码）。
spelling → canonical 的归一由判据的 `LABEL_TO_CAPABILITY` 做，括号里给在册 canonical。

1. **`OBB2-cboe-IndexSnapshots`**（`index_snapshots.py:101`）3 → 4 行
   `branching_base_url, percent_column_scaling, column_drop` **+ `sentinel_value_nulling`**
   （→ `columns.null_sentinel`，absent）。region 分支 `:100-103` 是两个整块字面 URL、路径根不同，
   所以是 `flow.branch` 不是 `flow.url_template`；`:121-129` 除 100 取 6 位；漏掉的是中间那段
   `:130-136` 的 `df.replace(0, None).replace('', None).dropna(how='all', axis=1).fillna('N/A')
   .replace('N/A', None)`——发布的 0、空串、经占位符往返的值都成 null，且整列空就整列消失。
2. **`OBB2-federal_reserve-CentralBankHoldings`**（`central_bank_holdings.py:317`）1 → 5 行
   `conditional_endpoint_branch` **+ `preflight_directory_lookup, nearest_published_date_snap,
   sentinel_value_nulling, published_value_rescale`**。分支只是最小的那道阻塞：每次调用先取有效
   as-of 名册（`ny_fed_api.py:358-365`）再由日期建 URL（`:467`），然后把调用方的 `as_of`
   **换成最近published 日期**（`:311-319` 的 abs-diff argmin，`:454-456` 调用，未传时 `:457-458`
   取 `dates[0]`）——这一条就是本轮注册的 `flow.nearest_published_value`。响应侧还有哨兵
   （`:283-291` 三种发布写法 → None）与百分数重标定（`:277-281`），两个 validator 都在 Data 类上
   （类 `:92`，Fetcher 从 `:294` 才开始）。另有一处**无 canonical**：`query.cusip` 在 `:332-335`
   被逗号并成单值（list 值的 QUERY 参数，`path.list_segment` 说的是路径段），记在该行 notes 里，
   归到 #86 的词汇债。
3. **`OBB2-fred-FredRegional`**（`openbb_fred/__init__.py:91`）3 → 6 行
   `conditional_endpoint_branch, branching_base_url, param_value_mapping` **+ `rows_from_object_keys,
   derived_columns, comparison_row_filter`**。`:239` 之后打开才发现真正的阻塞是取行面目：
   `data['meta']['data']` 是**按日期键的对象**（`:268-269`），`list(_data.keys())` 再 `_data[key]`
   （`:271-272`）→ `path.object_keys`（正是 `resolve_rows` 那句 `if not isinstance(current, list)`
   拒绝的东西）；member 键与 units 被盖进每条记录（`:272-275` → `derived_columns`）；`end_date`
   切分是对解析后的键做**有序比较** `<=`（`:276-279`）→ `comparison_row_filter`，不是 `rows.filter`
   （`RowFilterOp` 没有比较算子，#87）。另有一处**无 canonical**：模型返回
   `AnnotatedResult(result=results, metadata=meta)`（`:267,:282`），词汇表里没有任何 label 命名
   「metadata 通道」。
4. **`OBB2-tradier-EquitySearch`**（`equity_search.py:98`）2 → 4 行
   `conditional_endpoint_branch, field_rename_map` **+ `single_object_array_wrap,
   conditional_value_derive`**。单个 security 对象被包成 `[data]`（`:106-109`）是独立接缝
   （`path.object_keys` 说的是记录从哪来，不是有几条）；交易所码翻译按 `d['type']` 选表
   （`:122-127`）归 `derive.conditional`——本轮把该行原先的 `UNMAPPED_SUGGESTION:
   column_value_translation` **撤回**，写在 notes 里而不是留着。还有两处骑在 `flow.branch` 上、
   记下但不另立 label：`BASE_URL` 由凭证 sandbox 位在分支之前选出（`:84-92`），`:100` 与 `:102`
   两个查询参数各自只在分裂的一侧存在。这行现在在锁池里，need 集
   `['columns.rename', 'derive.conditional', 'flow.branch', 'shape.single_record_wrap']`，
   其中 3 条 status=absent（`columns.rename` 是 present，46 处生产声明）。
5. **`OBB2-tradingeconomics-EconomicCalendar`**（`economic_calendar.py:250`）2 → 6 行
   `conditional_endpoint_branch, param_value_mapping` **+ `relative_date_default,
   code_table_reverse_lookup, sentinel_value_nulling, date_format`**。`url_generator.py:33-36`
   补日期缺一半：`end_date` 取今天（`:34`）、`start_date` 抄 `end_date`（`:35-36`）——两条规则不同
   但只有一个 carrier 接得住（`param.relative_default`），折叠在此显式命名（#86 的形状）；
   响应侧 `:180-185` 把发布的 1/2/3 读回单词 → `columns.compose`（与请求侧 `:121-126` 的
   `param_value_mapping` 是双向的两半，只有第二半是响应侧）；`:206-217` 空串清成 None →
   `columns.null_sentinel`；`:187-204` 三个日期的 to_datetime/微秒归零/截到 date → `columns.cast`。

## 6. 决策：本轮不建 `flow.branch` kernel

判据给出的度量（§2、§3）：

- 建它对**本轮审过的 5 行 ship 0 行**（`post_branch_unlocks_audited=0`）。这 5 行各自的最后一道
  阻塞被记成别的接缝，tradier 那一行需要 3 个 absent 接缝、一次 step 出不来。
- 它现在的 10 行 credit（alpha_vantage×2、congress_gov×1、fed PrimaryDealer×2、intrinio×1、
  tiingo×2、tradier×2）**全部是没做过这种逐行上游复核的行**。C92 已经量过一次未审 credit 的虚高
  形态（`shipped_cover` 6 行，全靠少报 need 集），所以我不打算用同样未验证的口径去换一个
  「10 行 ready」的口头判定。
- 阶梯顺序也变了：step 0 现在是 `flow.fan_out`（newly=5），`flow.branch` 在 step 1。

下一轮要建它的话，前置动作是这 10 行的 need 集复核（与 #85 同族的逐行审），把
`audited_in_it` 从空集填起来；本轮的交付是更正后的 need 集 + 一个可复算的预测面，不是 flag 翻转。
相关未修缺陷：#92（`RowOrderSpec.column` 静默 no-op）、#93（`rows.cap_source_order` kernel 与 probe）、
#85/#86/#87（词汇与映射债），以及本轮新增的两处无 canonical 面（fed 的逗号并值 query 参数、
fred 的 `AnnotatedResult` metadata 通道）。

## 7. 本轮我错的五条，留档不抹

1. **「tradier 需要两个 absent kernel」是我写死的 prose。** 仪器原先那条 FAIL 文字这么写；实测
   `absent_needs=3`（`derive.conditional / flow.branch / shape.single_record_wrap`），只有 2 个是本轮
   新加的。已改成从在册 registry status 现场量，并把 `tradier_absent_needs=3` 打进 VERDICT。
2. **第一版 ARM-IDENTITY 的「+2 canonical」放在两臂之间，那条臂结构上不可能翻面。** 两个 census 臂
   共享同一份 registry（这是实验设定，为了证明移动只来自 need 集），canonical 数在两臂必然相等，
   于是它读 0/2 判红。改法是拿钉住的 pre artifact 字节当对照面（52 → 54），并把
   `registry_shared_across_arms=True` 本身作为正面证人印出来。
3. **REFUSAL 第一臂的观察与我原本的说法不符，我没改名。** 只删两条 spelling 时判据先撞
   orphan 守卫（`registry names capabilities no census label maps to: [...]`），不是 unmapped-label
   守卫。补了第二臂（spelling + registry 条目一起删）才量到后者，两条分别打印、分别判。
4. **`T-HEAD` 不是一条反例。** 我本以为把锚换成 `HEAD` 会让两臂塌成同一批字节从而翻红，实测绿色——
   因为此刻 `HEAD` 就是 `dd11d42`，两个名字指同一批字节。这条读数留在这里：它是钉锚理由的证人
   （C93 入提交后 `HEAD` 漂移，pre 臂会读成 post 臂），也是「对照臂必须有可能不同」的一次违反自检。
   **括号里那条预测已经在 §4.1 量到**：`e14c4bd` 之后重跑四例，`T-HEAD` 翻红 3 条、汇总读
   `unexpected=1 / TAMPER_RC=1`（过期的是反例装置里那句 `expected=green`，不是判据），
   同一时刻判定仪器仍 `FACE_RC=0` 且 23 行输出里只有第 1 行的 `HEAD` 标签变了。
5. **ruff 又跑在写完之后**（连续第三轮的同类流程债）。这轮把顺序摆正：写完先 `ruff check` +
   `format --check`，再 mypy / bandit / a2-check，最后才跑仪器与测试。首次 `ruff check` 报出 37 条
   （32 个 E501、2 个未用 noqa、1 个 import 排序、1 个未用变量、1 个 PERF401；计数是从
   `/tmp/c93_ruff.txt` 逐条 rule-code 重新统计得到，32+2+1+1+1 = 37 与文件尾行 `Found 37 errors.` 相符），
   另加 mypy 10 条
   （`dict[str, object]` 的面目取值不当）与 bandit 一条 superfluous-nosec 警告
   （`_git(argv)` 上多余的 `B607`）。清完后：ruff / format / mypy / bandit 全 rc=0，
   `a2_check.py --files` 四个成员 ok，仪器两次运行输出字节相同。

## 8. 测试面：在册判据的字节动过，所以全套重跑

```
$ python -m pytest tests/test_model_capability_census.py tests/test_provider_federal_reserve.py \
      tests/test_fred_provider.py tests/test_fred_generic_models.py tests/test_cboe_equity_search.py \
      tests/test_cboe_engine_provider.py tests/test_upstream_model_specs.py \
      tests/test_engine_row_order.py tests/test_engine_row_expansion_spec.py --no-cov
============================= 223 passed in 6.11s ==============================

$ make test-cov                                              # TESTCOV_RC=0
pytest tests -n 8 -m "not e2e" --cov-branch --cov-report=term-missing \
    --cov-report=xml:coverage.xml --cov-report=html:htmlcov --cov-fail-under=84
TOTAL                                    28074   2415   8736   1067  89.92%
Required test coverage of 84% reached. Total coverage: 89.92%
================ 22494 passed, 148 skipped in 487.22s (0:08:07) ================
```

- 本轮往 `model_capability_census.py` 加了 2 条 spelling 映射与 2 个 gap 条目，动的是 **member 12
  会跑的那份判据**，所以全套必须重跑而不是只跑子集。
- member 12 的目录算术族（台账 §44 / C89 那轮红过 11 条）本轮**一条都没红**：全套 `22494 passed`，
  无失败、无 error，因此本轮没有「把目录计数当再测量清掉」的动作，也不需要
  `ratchet.py --update`（门槛 84%，读数 89.92%，方向是绿的）。
- `148 skipped` 是 e2e 腿（`-m "not e2e"` 加 `OPENDATA_ALLOW_LIVE_E2E` 未设），与 350 行 live 轴
  同样是授权门后的东西。
- `coverage.xml` / `htmlcov` 在册 git-ignore、不入提交，所以这两个数是日志面而非文件面；
  复算命令就是 `make test-cov`（约 490 s）。

## 9. 复算指引

```
python docs/evidence/C93/flow-branch-need-face.py            # FACE_RC=0
python docs/evidence/C92/census-repair-control.py            # CTRL_RC=0，arm-pre=6 arm-post=0
python docs/evidence/C92/row-order-face.py                    # FACE_RC=0，ARTIFACT round=C93 canonical=54
python scripts/quality/model_capability_census.py --json /tmp/recheck.json --round-id C93
cmp /tmp/recheck.json docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/capability-roadmap.json
python scripts/quality/model_capability_census.py --check --round-id C93   # verdict: PASS
python -m ruff check scripts/quality/model_capability_census.py docs/evidence/C93/flow-branch-need-face.py
python -m mypy scripts/quality/model_capability_census.py docs/evidence/C93/flow-branch-need-face.py
```

C92 的两台仪器在 C93 树上重跑都是绿的，且自动跟随轮次标签（`ARTIFACT round=C93 … canonical=54`），
所以本轮没有新增轮内 pin 债。

> **【C94 补记，2026-10-12】上面第 1 行的 `FACE_RC=0` 只对 C93 那一代成立。** C94 把
> `flow.fan_out` 的 credit 从 step 0 移到 step 4（逐行重审了它 predicted 的 5 行，4 行 need 集被证伪/补全，
> 幸存者 1 行），并新增 5 条 spelling、+15 mention，于是本装置在 C94 树上重跑读到的是
> `C93_FACE_RC=1 failures=8`，**这是预期读数，不是 census 回归**。8 条的实质分两类：
>
> ```
> ARM-POST tradier: in_locked_pool=False needs=[] absent_needs=[] n_absent=0 (pre arm in_locked_pool=False)
> FAIL tradier is one ladder step away although its locked need set reads 0 absent kernels
> FAIL tradier's locked need set reads 0 absent kernel(s), so a single step could unlock it and the LOCKED attribution is wrong
> FAIL the two kernels this round added to tradier are not both absent blockers: []
> FAIL flow.branch still unlocks audited rows after the repair: ['OBB2-cboe-IndexSnapshots']
> FAIL the prefix movement is not the single row: left=['OBB2-deribit-FuturesInfo'] entered=[9 个 oecd 行]
> FAIL added spellings 14 != judge mention delta 29
> FAIL expected +2 canonicals against the pinned artifact and +2 distinct spellings between the census arms, read 2/7
> ```
>
> 前 3 条是**本装置自己的静默默认**：`:290` 的 `post["locked_needs"].get(TRADIER, [])`，而 `:136-137` 的
> `locked_needs` 只从 `rows_still_locked_after_engine_pool` 取行；tradier 现在被 step 上的
> `shape.single_record_wrap` credit、离开了那个池，于是 `.get` 交出空表 —— 盘上
> `OBB2-tradier-EquitySearch` 的 needs 是 4 条（`conditional_endpoint_branch`、`field_rename_map`、
> `single_object_array_wrap`、`conditional_value_derive`），且全表扫描「needs 为空且
> `expressible_today=false`」= **0 行**，所以 census 侧没有无原因的 pending。
> 其余 5 条是生成期 pin（mention/spelling/canonical 增量、前缀成员集合）随 C94 位移的老化。
> 判据与两类的完整展开在 `docs/evidence/C94/README.md` §9。
>
> 因此复算本档时的正确期望是：**C94 之后的树 → `RC=1 failures=8`**；要拿回 C93 那笔绿，必须把
> `PRE_REPAIR_REV` 与在册 census/roadmap 一起回退到 C93 那一代字节（本档刻意不回退，也不把今天的数改写成 0）。
> C92 的两台（`census-repair-control.py` / `row-order-face.py`）在 C94 树上仍读 `RC=0`，已重取。

**上面每一条在 `e14c4bd`（本档入提交的那一笔）之后逐条重取过**，读数与 §2/§3 描述的是同一批字节：

```
FACE_RC=0                       # flow-branch-need-face.py；与提交前那次 diff 只差第 1 行的 HEAD 标签（§4.1）
CTRL_RC=0                       # C92/census-repair-control.py：VERDICT arm-pre=6 arm-post=0 movement=6 failures=0
row-order-face rc=0             # C92/row-order-face.py：ARTIFACT round=C93 status=present declarations=1
                                #   shipped_cover=0 canonical=54 / VERDICT failures=0
GEN_RC=0 / CMP_RC=0             # 重derive 170978 字节，与在册那份 cmp 返回 0
CHECK_RC=0                      # --check verdict: PASS
A2_RC=0                         # ok ruff check / ok ruff format --check / ok mypy / ok bandit
```

这些命令不依赖任何未提交的临时状态，也不依赖 `/tmp`：只有 §4 的反例装置 `c93_tamper_harness.py` 是
刻意不入库的一次性脚本（它改的是 /tmp 副本、在册字节不动），它需要的四条单行补丁已逐字贴在 §4 的块里。
