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

折叠改判的面（`F4b`，HEAD 的 credit 列表一个字不动，只换折叠表）：

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

判定用的是**仓内那台 judge**（`import model_capability_census`，HEAD 侧用 `git show` 取字节
再 `exec_module` 载入，不是第二台我手写的判据），before 侧一律来自 `git show HEAD:`。

```
F2 census: rows=279 expected=279 unique_labels=117 judge_accepted=True
F2b fold labels no row cites: work=1 head=1 new=[]        # 唯一死条目 csv_header_offset，是既有的，本轮没新增
F3 cite carriers: checked=19 missing=0
F4 present-set: head=13 work=13 identical=True newly_present=[]   # 没有任何能力被翻成 present
F4 monotonicity: same=255 grew=23 shrank=1 unexpected=0
F5 arithmetic: sum(per-row delta)=15 artifact mention delta=15 rows_changed=12
F7 ladder recompute: steps=38 reproducible=True first_divergence=None
F7b dispatch brief: clusters=4 front_matches=True rows_match=True round=True present_match=True
```

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
```

`C5` 专门对着 `F4b`：把那 14 行里第一行的折叠**逐条还原成 HEAD 的折叠表**，它的 blocker 集合必须
缩回去。不缩就说明 `grew=14` 不是折叠造成的，`F4b` 那面也就不能用来给「只变严」作证。

## 6. 下一轮的预测（可证伪形式）

按修好的阶梯，`param.relative_default` 建核并注册 present 之后：

- **谓词**：`greedy_build_order[0].capability == "param.relative_default"` 且该行集
  `{OBB2-intrinio-HistoricalDividends, …}`（快照 step 0 的 3 行）从 pending 移入
  `rows_unlocked_by_greedy_prefix`，prefix 189 → 189（这一步本身不解锁，因为它解锁的是 3 行
  pending 里的 3 行 → 189 是「已解锁前缀」，实际读数应为 `rows_unlocked_by_greedy_prefix` 不变、
  `rows_expressible_today` 7 → 7 不变、locked 83 → 80）。
- **失效条件**：若 `param.relative_default` 注册后 locked 只降到 81 或 82，说明这 3 行里还有行
  带着第二条未命名的 blocker —— 那就不是 kernel 的问题，是词汇表又欠了一轮。
- **反例臂**：注册前先跑一次「把 3 行里任意一行的 `relative_date_default` 摘掉」的 derive，
  prefix/locked 必须按 1 的步长移动；不动就是 `param.relative_default` 的 anchor 或 need 集有问题。

复算命令：

```
export PATH="$HOME/opt/anaconda3/bin:$PATH"; export PYTHONPATH=.
python docs/evidence/C95/vocabulary-repair-face.py
python scripts/quality/model_capability_census.py --round-id C96 --check
```
