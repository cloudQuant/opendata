# C95 — 词汇面重修：4 个新 canonical、1 条被证伪的 credit、折叠改判 14 行，阶梯总额不变而前 8 步重排

本轮不建 kernel。C94 交付的阶梯把 `param.relative_default` 排在 step 0（3 行），但在动手之前我
先停下来重读了一件事：`expressible_today` 在 census 里是**存储字段**，不是从 need 集推导出来的，
而上一轮逐行审回时反复出现的偏差是「need 集少记了一条机制」。少记一条，行就会被判成「引擎今天
就能写」；这种行进了 kernel 的解锁名单，验收时才发现写不出来。所以本轮的判定对象是**词汇表本身**：
把 12 行对着 pinned 上游重读，缺什么命名什么，再把折叠改回来。

结论先行：**12 行改了（+15 条 spelling 提及、−1 条被证伪的 credit），267 行未动；新增 4 个
canonical，全部 anchored-absent；3 条 spelling 从 `rows.filter`（present）改判到
`rows.filter_comparison`（absent）；阶梯总额 189/7/83 一格未动，但 build order 从 34 步变成 38 步，
前 8 步全部换位。**

---

## 1. 为什么「折叠改判」必须先于建 kernel

被改判的三条 spelling 原本折到 `rows.filter`，而 `rows.filter` 是**在册 present** 的能力：一条行
只要其它需求都在，它就会被 greedy cover 当成「只差一步」收进解锁名单。`RowFilterOp` 支持的是
等值/包含这一类比较，日期窗口那种「两边界 + 有序比较」在声明面里没有对应物 —— 这就是
映射债（任务 #87）：label 盖住了两种机制，present 的能力替不存在的能力作保。

折叠改判的面（`F4b`，`78710d0` 的 credit 列表一个字不动，只换折叠表）：

```
F4b fold-only: same=265 grew=14 shrank=0 total=279
```

**只变严，不变松**：14 行的 absent-blocker 集合因为折叠而**增大**，0 行缩小。这条是本档里唯一
能把「我放宽了门禁」这种错误当场抓住的读数，所以它配了自己的反例臂 `C5`（见 §5）。

## 2. 12 行的逐行实测（每条 file:line 本轮都在盘上重取）

上游树：`/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers/`（只作输出契约
与机制参照，不复制代码）。`F3` 的 19 条 cite 全部命中 carrier 行的 token，`missing=0`。

| 行 | 净变化 | spelling | 上游 file:line | 机制 |
|---|---|---|---|---|
| `OBB2-tmx-EtfSearch` | 2→5 | `filter_needle_normalization` + | `tmx/.../etf_search.py:243` `.capitalize()` | 过滤针被改写后才拿去比对 |
| | | `parameter_selected_sort_column` + | `:246` `sort_values(by=query.sort_by)` | 排序列由**调用方**点名，静态列名接不住 |
| | | `all_null_row_drop` + | `:261` `dropna(how=` | 整行为空才丢，非逐列条件 |
| | | `sentinel_value_nulling` + | （既有 spelling） | 哨兵值置空 |
| | | `symbol_normalization` − | `:216` `return TmxEtfSearchQueryParams(**params)` | **被证伪**，见 §3 |
| `OBB2-tmx-EtfHoldings` | 6→7 | `dual_source_spelling_rename` + | `etf_holdings.py:127` `"shareChange"` | 同一列在源侧有两种拼写，多对一改名 |
| `OBB2-tmx-EtfSectors` | 6→7 | `filter_needle_normalization` + | `etf_sectors.py:68` `.replace(".TO"` | 同上 |
| `OBB2-tmx-EtfCountries` | 6→7 | `filter_needle_normalization` + | `etf_countries.py:71` `.replace(".TO"` | 同上 |
| `OBB2-tmx-EtfInfo` | 5→6 | `filter_needle_normalization` + | `etf_info.py:223` `.replace("-", ".")` | 同上 |
| `OBB2-tmx-EquitySearch` | 3→4 | `multi_request_concat` + | `tmx/.../utils/helpers.py:455` `get_tmx_tickers("tsxv"` | 两个 exchange 码表并发取回再拼接 |
| `OBB2-sec-CikMap` | 2→4 | `filter_needle_normalization` + | `sec/.../utils/helpers.py:167` `lstrip("0")` | CIK 去零后再比对 |
| | | `source_order_cap` + | `:171` `.iloc[0]` | 取第一条 = 先排序再截断 |
| `OBB2-sec-SymbolMap` | 2→4 | `filter_needle_normalization` + | `helpers.py:137` `.upper().replace(".", "-")` | 针与行同形才匹配 |
| | | `source_order_cap` + | `:144` `.iloc[0]` | 同上 |
| `OBB2-econdb-PortVolume` | 5→5 | `series_pivot` → `long_to_wide_group_pivot` | `econdb/.../port_volume.py:148` `columns="measure"` | 旧 spelling 折到 `path.explode`，盖不住「按组长转宽」 |
| `OBB2-benzinga-PriceTarget` | 4→4 | `epoch_ms_to_date_conversion` → `epoch_s_to_date_conversion` | `benzinga/.../price_target.py:155` `int(v.timestamp())` | 时间戳是**秒**不是毫秒，单位写错就是一位数量级的偏移 |
| `OBB2-tradier-EquityQuote` | 5→7 | `code_table_forward_lookup` + | `tradier/.../equity_quote.py:258` `OPTIONS_EXCHANGES.get` | 值查表合成一个新列 |
| | | `single_object_array_wrap` + | `:243` `isinstance(data, list)` | 单对象与数组两种响应收敛成一行 |
| `OBB2-intrinio-InsiderTrading` | 3→4 | `relative_date_default` + | `intrinio/.../insider_trading.py:135` `relativedelta(years=5)` | 默认值是「相对今天」而非常量 |

另外把 `http_json.py:434` 那条 resolve_rows 拒绝面的 cite 从 docstring 散文改成**拒绝语句本身**
（`not isinstance(current, list)`）；`F3` 里它是唯一一条 repo 侧 carrier。

## 3. 一条 credit 是被证伪的，不是被删掉的

`OBB2-tmx-EtfSearch` 原先 credit `symbol_normalization`（折到 `param.value_transform`，意即
「发出去之前要把 symbol 改写」）。盘上的事实是 `etf_search.py:216` 直接
`return TmxEtfSearchQueryParams(**params)` —— 改写发生在响应侧，压根不上线。所以这条不是
「本轮选择不记」，是**它描述的机制不在这条行上**。`F3b` 把两面都打印出来：

```
F3b refuted credit OBB2-tmx-EtfSearch/symbol_normalization: passthrough carrier :216=True still_credited=False
```

`param.value_transform` 的提及数因此 12 → 11。这一行也解释了 `F4` 里那个唯一的 `shrank=1`：
本轮只允许**有 carrier 的证伪**让一行的 blocker 集合变小（`unexpected=0` 就是这条配额）。

## 4. 词汇表与阶梯的实测面

新增 4 个 canonical，全部 `claim=False, engine_work=True, probe=None`，锚点选在**今天盘上真实
存在**的 token（锚不到就是 `drifted`，drifted 条目会静默离开 roadmap 池 —— `F1 drifted=0` 防的就是这个）：

```
F1 registry: present=13 drifted=0
   rows.needle_transform / rows.order_by_parameter / shape.long_to_wide_pivot / columns.many_to_one_rename  status=absent -> OK
```

canonical 提及数位移（只列变动的；一个行内多条 spelling 折到同一 canonical 只算一次）：
`rows.needle_transform 0→6`、`rows.order_by_parameter 0→1`、`shape.long_to_wide_pivot 0→1`、
`columns.many_to_one_rename 0→1`、`rows.filter 56→42`、`rows.filter_comparison 13→28`、
`rows.cap_source_order 1→3`、`columns.compose 12→13`、`columns.null_sentinel 43→44`、
`param.relative_default 6→7`、`param.value_transform 12→11`、`flow.fan_out 130→131`、
`shape.single_record_wrap 1→2`。taxonomy：canonical 54→58、labels_supported 112→118、
raw_labels 111→117、raw mentions 1207→1222（+15，与逐行净增量恒等 —— `F5`）。

阶梯前 8 步（HEAD artifact → 本轮 artifact）：

```
step 0  columns.null_sentinel(4)      ->  param.relative_default(3)
step 1  param.relative_default(5)     ->  columns.null_sentinel(5)
step 2  param.value_transform(5)      ->  request.serialised_param(4)
step 3  path.object_keys(4)           ->  decode.tabular_file(3)
step 4  flow.fan_out(4)               ->  request.body_pagination(3)
step 5  flow.branch(13)               ->  request.body(9)
step 6  flow.join(15)                 ->  flow.fan_out(8)
step 7  flow.preflight_lookup(11)     ->  flow.branch(14)
```

`189 + 7 + 83 = 279` 一格未动（`F6`），步数 34 → 38：多出来的 4 步就是 4 个新 canonical，
而前 8 步换位是因为 `rows.filter_comparison` 现在背着 28 条提及，greedy 提前把它收了。
**本轮不建 kernel 的判定依据正是这张表**：`flow.fan_out` 的 credit 从 4 变成 8，`flow.branch`
从 13 变成 14 —— 这两行级的位移如果按旧词汇表建 kernel，交付的就是一个照着错的解锁名单实现的核。

## 5. 仪器（`vocabulary-repair-face.py`，`VERDICT: PASS` / `FACE_EXIT=0`）

判定用的是**仓内那台 judge**（`import model_capability_census`，before 侧用 `git show <rev>:` 取字节
再 `exec_module` 载入，不是第二台我手写的判据）。before 侧的 rev 是**写死的 `BASE_REV = "78710d0"`**
而不是 `HEAD`：这一版仪器原本读的正是 `git show HEAD:`，而 `57abd20` 把修复合进去之后 HEAD 就变成
了修复后的树 —— 于是 before/after 两侧成了同一棵树，`F4 monotonicity` 打成 `same=279 grew=0 shrank=0`、
`F4b` 打成 `same=279 grew=0`、`F5` 打成 `delta=0 rows_changed=0`（看着全绿，其实是空比较），
`C5` 直接 `IndexError` 崩在打印 verdict 之前。改成钉死 rev 之后这三面恢复到修复当轮的实际读数。

```
F2 census: rows=279 expected=279 unique_labels=117 judge_accepted=True
F2b fold labels no row cites: work=1 head=1 new=[]        # 唯一死条目 csv_header_offset，是既有的，本轮没新增
F3 cite carriers: checked=19 missing=0
F4 present-set: head=13 work=13 identical=True newly_present=[] baseline=78710d0   # 没有任何能力被翻成 present
F4 monotonicity: same=255 grew=23 shrank=1 unexpected=0
F4b fold-only: same=265 grew=14 shrank=0 total=279
F5 arithmetic: sum(per-row delta)=15 artifact mention delta=15 rows_changed=12
F6 ladder: 189+7+83=279 census=279
F7 ladder recompute: steps=38 reproducible=True first_divergence=None
F7b dispatch brief: clusters=4 front_matches=True rows_match=True round=True present_match=True
F8 projection target: param.relative_default credit rows=3
   AS-IS               prefix=189 expressible=7 locked=83 sum=279 front=param.relative_default
   register-only       prefix=189 expressible=7 locked=83 sum=279 front=columns.null_sentinel
   register+re-derive  prefix=186 expressible=10 locked=83 sum=279 front=columns.null_sentinel
```

`F8` 是这一轮补的**预测面**：它不改盘上一个字节，只把「注册 present」和「把行重 derive」两种动作
各喂一遍仓内 `greedy_cover`，把三个 bucket 读数取回来。`expressible_today` 是 census 里的**存储字段**
（`rows_expressible_today = len(rows) - len(pending)`），所以只注册不动行的话，bucket 一格都不会动，
只有 ladder 前缀换位；§6 原来那段预测把两件事混在一起了，这一面就是把它拆开 measurable。

`F7` 是本轮新加的一面：把 judge 在**当前盘上的 census + registry** 上重跑一遍，拿它产出的 greedy
ladder 和 artifact 里存的那份逐步比对（38 步全等），再拿 `ladder-front-steps0-3.json` 这份**派包
快照**去对同一份 artifact。上一版快照是修复前生成的（step 0 还是 `columns.null_sentinel`），
如果 F7b 不做，下一轮的 builder 会照着旧前缀去建核。快照由
`python docs/evidence/C95/make_ladder_front.py` 重新生成，文件里没有一个数是手打的。

反例臂（每条都必须把判决打移动，否则那一面是装饰）：

```
C1 forged blocker-removal on OBB2-tmx-EtfSectors: lost=['rows.needle_transform'] -> True
C2 the judge refuses an unmapped spelling: RoadmapError
C3 cite moved one line off .../etf_search.py:243-1 -> token_absent=True
C4 forged credit-list on OBB2-intrinio-HistoricalDividends (dropped param.relative_default):
   front_moved=True step0=('param.relative_default', 3)->('request.body_pagination', 3)
C5 fold reverted on OBB2-alpha_vantage-EquityHistorical: blockers
   ['flow.branch','flow.fan_out','rows.filter_comparison'] -> ['flow.branch','flow.fan_out'] shrank=True
C6 re-derive one row (OBB2-federal_reserve-OvernightBankFundingRate) instead of all 3:
   prefix 189->188 expressible 7->8 differs_from_full_arm=True
```

`C5` 专门对着 `F4b`：把那 14 行里第一行的折叠**逐条还原成 78710d0 的折叠表**，它的 blocker 集合必须
缩回去。不缩就说明 `grew=14` 不是折叠造成的，`F4b` 那面也就不能用来给「只变严」作证。

`C6` 对着 `F8`：只翻 step 0 credit 名单里的**一行**而不是三行，读数必须按 1 的步长移动
（`189→188`、`7→8`），而不是把三行的结果照抄一遍。不动就说明 `F8` 那一面打印的是常量。

## 5b. 注册表 roster 面：本轮的 4 个新 canonical 撞红在 member 12

`make test-cov` 第一跑 `MEMBER[test-cov]_EXIT=2`，红的**不是**覆盖率闸门（`Total coverage: 89.92%`，
闸门 `--cov-fail-under=84`），而是一条硬编码结论：

```
1 failed, 22493 passed, 148 skipped in 498.19s
FAILED tests/test_model_capability_census.py::test_registry_shape_is_anchored_or_declared_unproven
  At index 1 diff: 'columns.many_to_one_rename' != 'columns.null_sentinel'
  Left contains 4 more items, first extra item: 'rows.filter_comparison'
```

那条断言钉的是「引擎侧还没有声明面（probe）的能力清单」，清单在 78710d0 上是 11 条；本轮注册的
4 条正是清单该长的那 4 条 —— 每一条都由一行被重审的 census 行点名，不是我推出来的能力：

| capability | 点名它的行 | 为什么写不出 probe |
| --- | --- | --- |
| `rows.needle_transform` | 6 行 pending（sec CikMap/SymbolMap + tmx EtfSearch/EtfSectors/EtfCountries/EtfInfo） | 过滤的比较数**原样**取自 query，上游在比较前先改写它（tmx 剥 `.TO`、把 `-` 换成 `.`），`RowFilterSpec` 没有承载改写的字段 |
| `rows.order_by_parameter` | `OBB2-tmx-EtfSearch` | 排序面能声明 limit 的来源（`limit_param`），`RowOrderSpec.column` 却是静态串，**排序键本身**来自调用方就声明不了 |
| `shape.long_to_wide_pivot` | `OBB2-econdb-PortVolume` | 两个在册 pivot 都读静态 `series_keys` 且方向是 wide→long（`row_expansion.py:313`），这行是 long→wide |
| `columns.many_to_one_rename` | `OBB2-tmx-EtfHoldings` | 一个输出列只能命名一个 `source_key`，上游两种拼写（`sharechange` / `shareChange`）收敛到同一列时第二条被静默丢掉 |

我**没有**放宽那条断言（它仍然要求清单逐字相等），改的是清单本身和它的原因注释，并给它补了
一台反例机 `roster-tripwire-counterfact.py`：四臂都是驱动**仓内那个 test 函数本身**
（`import test_model_capability_census` + `mock.patch.object(mcc, "REGISTRY", …)`，不是我重打一遍
断言），判决必须随篡改移动：

```
C0 as-is registry: roster=15 of which new=4 -> passed
C1 drop rows.needle_transform from roster: -> AssertionError added=[] dropped=['rows.needle_transform'] bare_claims=[]
C2 append a probe-less engine-work entry: -> AssertionError added=['shape.forged_unrostered'] dropped=[] bare_claims=[]
C3 promote probe-less rows.needle_transform to claim=True: -> AssertionError added=[] dropped=[] bare_claims=['rows.needle_transform'] named_by_helper=['rows.needle_transform']

VERDICT: PASS   ROSTER_EXIT=0
```

`C1`/`C2` 打的是 roster 面（一条能力悄悄消失 / 一条新能力没上名册），`C3` 打的是**另一条**断言
（没有 probe 却声明 present），所以它的 roster delta 是空、红色归因写在 `bare_claims=` 里 ——
一行 `AssertionError added=[] dropped=[]` 如果不带这个字段，读起来就是一次空的红，那是仪器的问题
不是判决。复算：`python docs/evidence/C95/roster-tripwire-counterfact.py`。

## 6. 下一轮的预测（可证伪形式，读数由 `F8` 算出）

**先记一条被自己证伪的预测**：本节上一版写的是「注册 present 之后
`rows_expressible_today` **7 → 10**、`rows_unlocked_by_greedy_prefix` **189 不变**」。这两句放在一起
是**不可能的**：`F6` 那面要求 `prefix + expressible + locked = 279`，而 `locked` 是 83，
`189 + 10 + 83 = 282 ≠ 279`。上一版没有跑过投影，是手推的。`F8` 补上之后，两臂的实际读数是：

| 动作 | prefix | expressible | locked | 三者和 | ladder step 0 |
|---|---|---|---|---|---|
| AS-IS（盘上现状） | 189 | 7 | 83 | 279 | `param.relative_default` |
| 只把 `param.relative_default` 注册成 present | **189** | **7** | 83 | 279 | → `columns.null_sentinel` |
| 注册 **并且**把 step 0 那 3 行的 `expressible_today` 翻成 `true` | **186** | **10** | 83 | 279 | → `columns.null_sentinel` |

那 3 行是 `OBB2-federal_reserve-OvernightBankFundingRate`、`OBB2-intrinio-HistoricalDividends`、
`OBB2-tiingo-CryptoHistorical`（`F8` 从 artifact 自己的 credit 名单里取，不是我抄的）。

下一轮 `param.relative_default` 建核之后，**必须**看到：

- `taxonomy.canonical_capability_count` 58 不变，`capability_registry["param.relative_default"].status`
  `absent → present`，registry 里 `present` 数 13 → 14（`F4 present-set` 的 `newly_present` 会点名，
  只应该出现这一个）；
- 只注册不重 derive 的话是**第一行**：bucket 一格都不动、只有前缀换位 —— 因为
  `expressible_today` 是存储字段，judge 不从 need 集推导它；
- 要把 `rows_expressible_today` 推到 10，`rows_unlocked_by_greedy_prefix` 必须同时掉到 **186**；
  打印出 `expressible=10` 而 `prefix=189` 就说明有人在 artifact 里手打了数字。
- **失效条件**：若重 derive 之后 `expressible` 不是 `7 + 3`，说明还有第 4 行的 need 集折到
  `param.relative_default` 而它的 `expressible_today` 与 need 集不一致（词汇表又欠一轮），
  `F5` 的行集合会先一步变红；若 `locked` 从 83 动了，说明注册动作顺带翻了别的能力。
- **反例臂**：`C6` 已经在仪器里 —— 只翻 1 行必须读出 `188 / 8`；`C4` 必须让前缀离开
  `param.relative_default`。两条都不动就是 `F8` 装饰。

复算命令：

```
export PATH="$HOME/opt/anaconda3/bin:$PATH"; export PYTHONPATH=.
python docs/evidence/C95/vocabulary-repair-face.py     # F8 的两臂 + C6，VERDICT: PASS / FACE_EXIT=0
python scripts/quality/model_capability_census.py --round-id C96 --check
```
