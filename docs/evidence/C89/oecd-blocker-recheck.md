# C89 — OECD 阻塞点复核：ready(17) 这个判定被推翻，以及推推翻之后重新发布的阶梯

本轮的触发点不是新功能，而是我自己发布的一个判定面被打断：C89 早期把
`ready(need set shipped) = 17` 行当作「今天就能声明式实现」的候选集抽出来并派给实现子代理。
第一个子代理（OECD 包）对着钉版上游逐行读完后返回的是 **0 of 9 declarable**，
即那 17 行里有 9 行根本不在这个集合里。本文记录：为什么 17 是错的、错在哪一层、
以及修复后重新发布的阶梯读数。错误的数字保留在文内，不删。

## 1. 结论先说

| 项 | 修复前（本轮早期发布） | 修复后（本轮末，实测） |
| --- | --- | --- |
| census 分母 | 279 | 279（未变） |
| 判定探针读取的 present capabilities | 10 | 10（未变） |
| `shipped_cover_rows`（need 集全被 present 覆盖的 pending 行） | 17 | **8** |
| `rows_unlocked_by_greedy_prefix` | 192 | **183** |
| `rows_still_locked_after_engine_pool` | 82 | **91** |
| registry 条目数 | 45 | **50** |
| `LABEL_TO_CAPABILITY` 键数 | 96 | **102** |
| `--check` 判定 | PASS | PASS |

错不在探针，也不在 `expressible_today` 标志位（九行一直是 `false`）。
错在 **need 集本身被少报了**：census 行里每行只有 `sdmx_dotted_key_path` + `csv_decoder`
两个标签（CPI 行多一个），而这两个标签折叠进的两个 capability
（`flow.composite_key_segment`、`decode.delimited`）都是 present。
`greedy_cover` 的判据是 `row.needs <= covered`，所以少报 need 集 = 直接判成 ready。

这是一个 **over-collapsed label** 缺陷的第二次发作：C75 已经为同类问题拆过一次
（`#40 C75: split four over-collapsed labels out of present capabilities`），
本轮又把 `sdmx_dotted_key_path → flow.composite_key_segment` 这条折叠的覆盖面放得太宽。

## 2. 判定探针（正向 + 反例双臂），carrier: `oecd-pin-witness.txt`

复算命令（本机，无网络、无 DB、无 DDL）：

```bash
PYTHONPATH=. python docs/evidence/C89/oecd-pin-witness.py        # rc=0
PYTHONPATH=. python scripts/quality/model_capability_census.py --check
PYTHONPATH=. python -m pytest tests/test_model_capability_census.py -q --no-cov
```

witness 的每个面都是双臂的（一次通过读数 + 一次必须不同的损坏读数）：

- **D1/D2/D3 — probe-less pin。** `pin` 不是我以为的值，而是用 `ast` 从
  `tests/test_model_capability_census.py` 里把 list 字面量抠出来（pin=9 == measured=9）。
  D2 给一个 probe-less 条目装上 probe → 集合变 8 → pin 必须红；D3 从 pin 里删一个名字 →
  必须红。两臂都与 D1 不同，说明这条 pin 是真的在测字节。
- **E0/E1/E2/E3 — 五个新条目。** E1：五个条目都读 `absent` 且 `anchor_line_number > 0`
  （513 / 119 / 596 / 40 / 62，分别是 `value_transform.py`、`spec.py`、`key_composition.py`
  上真实存在的那行「封口」声明）。E2：把锚点 token 从**副本**里删掉，读数必须从
  `absent` 移到 `drifted`。E3：损坏必须局部。
  E0 是这一臂的前置：`registry_files()` 把整个 registry 引用到的**所有**文件都复制进临时根。
  第一版我只复制被测条目自己的 proof/cross 文件，结果 5 个 E3 全红 ——
  那是我的仪器缺陷（未复制的条目锚点行号读成 0，凭空 `drifted`），不是发现。
  补上 E0 的「不损坏副本必须与树读数完全一致」控制后，E3 全绿。
- **F1/F2/F3 — census 与包内叙述的一致性。** F1 把 `NOT_DECLARABLE` 的 per-row 文本当 oracle：
  每条 note 点名的 capability 都必须出现在该行的 `needs_engine_capability` 里，
  否则「靠删标签把行消音」这条路就被关掉了。F2：六个新标签恰好折到六个 capability。
  F3：九行里没有一行的 need 集被 present 全覆盖。

`--check verdict: PASS`，`tests/test_model_capability_census.py` **21 passed**。

## 3. 标签集是从叙述生成的，不是手打的

九行的新 need 集由 `opendata/data/providers/oecd/specs.py` 的 `NOT_DECLARABLE` 文本派生
（token → raw label → canonical 三跳都做前向断言 `LABEL_TO_CAPABILITY[lab] == cap`）。
两次 abort 都是断言救的：

1. 反查 `LABEL_TO_CAPABILITY` 求 `flow.branch` 的唯一标签时断言失败 —— `flow.branch` 有
   5 个标签。写死单个标签的假设不成立；改成前向校验后，异常发生在写文件**之前**，
   census JSON 一个字节都没被改动。
2. 我在 dry-run 里手打过 `period_key_cast`，而折叠表里是 `period_key_parse`。
   若按 dry-run 写下去，`load_rows` 会因为未映射标签整轮 abort（fail-closed 生效），
   但那是「靠探针报错」而不是「靠生成正确」。派生写法把这类不一致压到 0。

净效果：9 行、每行只改 `needs_engine_capability` 一个字段、共 40 个标签追加。
逐行 added 实测：ConsumerPriceIndex / GdpForecast / GdpNominal / HousePriceIndex 各 5（need
从 3→8、2→7、2→7、2→7），CompositeLeadingIndicator / CountryInterestRates / GdpReal /
SharePriceIndex / Unemployment 各 4（need 从 2→6）。4×5 + 5×4 = 40。
`git show HEAD` 与当前文件逐行对比实测 added=40 / dropped=0。field-level diff 打印过：
`changed_keys=['needs_engine_capability']` 九次，别的字段一个没动。

## 4. 一个新发现的、关于判定本身的限制（不是 OECD 的问题，是阶梯的）

`path.list_segment` 我差点写错。子代理说「`+` join 多个 country code」，我先去测的是
`CompositeKeySpec(separator="+")` —— **它今天就能渲染**（实测 `M+USA`），因为分隔符白名单只禁
`/ ? # \r \n`。真正的阻塞是另一回事：上游把多个 code 塞进**一个 position**，
该 position 的文本里带 `+`，而 `sdmx_code` charset 是 `[A-Za-z0-9_\-@]+\Z`，
整段匹配 → 实测 `KeyCompositionError reason=charset_invalid component='area'`。
如果我没做这个实测，`path.list_segment` 会被错误地折进 present 的
`flow.composite_key_segment`，那就是把同一个 bug 再犯一次。
这条区分写进了 entry 的 `why`（"A composite joining on '+' as its SEPARATOR does render —
measured, not assumed — but that changes the key's position count"）。

顺带量到阶梯的方法学边界（值得后续轮次知道，不是本轮的债）：
`greedy_cover` 每次只加**一个** capability，而这九行每行都还需要 **4~5 个** absent capability，
所以任何单步都无法解锁它们。实测 pool=34、steps=28、
never-picked = `['columns.compose','columns.period_cast','derive.conditional',
'param.preset_merge','path.list_segment','rows.filter_comparison']`。
这正是它们在面上落到 `rows_still_locked_after_engine_pool`（91 = 82 非引擎 + 9 OECD）的原因：
不是被 non-engine 挡住，是被「一次一步」的贪心法挡住。要按行推进就得允许一次上一个
capability **组合**，那是路线图工具的下一次改动。

## 5. 我自己写错并被本轮删掉/改掉的话

- `LABEL_TO_CAPABILITY` 上方注释原写这些条目「turns present only when the work that widens it
  lands」。假的：`claim=False` + 锚点命中 ⇒ 只能是 `absent`，锚点不命中 ⇒ `drifted`，
  **没有任何路径自己翻成 present**（`verify_registry` 866-880 行）。改成明说要人工重指 entry，
  锚点行号是给 reviewer 核对旧限制是否真的挪了的。
- 折叠注释里写 `flow.branch` 是 "shipped-named"。`flow.branch` 实测 `absent`（spec.py:463），
  不存在 ship 一说；改成 "the already-named `flow.branch`, whose own claim is that one path
  template cannot branch on a status"。
- `oecd/specs.py` 顶部叙述原写九行 "needing `sdmx_dotted_key_path` + `csv_decoder`"，
  这在我改完 census 后就是假话；已改为记录 C89 追加后的状态。
- 本轮我自己造过一个**假零**：清点 census 标签用词频率时写了
  `Counter(l for r in rows for l in r.needs)`，而 `needs` 是**归一化后的 capability**、
  原始标签在 `raw_labels` 上。结果 `sentinel_value_nulling`、`derived_columns`、
  `per_item_fanout` 等 7 个标签全数报 0 —— 差点推出「这些标签没人用，所以不能给 TMX 用」
  的结论。用 `raw_labels` 重测才是真读数：101 个标签被引用、共 1150 次，
  `sentinel_value_nulling` 28 次、`derived_columns` 28 次、`multi_request_fanout` 47 次，
  整个映射表里**只有 1 个标签没有任何行用过**（`csv_header_offset`）。
  一个「全是 0」的计数在写出来时就必须带上最小可达值论证，这次是仪器错，不是发现。
- 本文 §7 第 1 条的 `13 failed / 604 passed` 是在两个子代理**正在写文件**的间隙取的：
  同一条 `test_empty_record_list_is_zero_rows_not_an_error` 的 traceback 先指向 687 行的
  `fetch(...)`，四分钟后再读同一行已经是 docstring —— 字节在我两次读之间变了。
  所以这个 13 是「半落地过程中」的读数，不是终态；终态要等子代理落完重测。

## 6. 子代理删掉的一段 provenance，已恢复

OECD 子代理重写 `oecd/specs.py` docstring 时，把 **CPI / Unemployment 两个声明为什么被撤回**
那段整段删了（`declaration_provenance.py` 实测的 `COLUMN_MISSING`/`COLUMN_EXTRA`、
`PARAM_UNBACKED`、`PATH_UNBACKED`，以及「那是在给本仓自研的 `models/_client` 读者换一个
OpenBB 身份重新登记 —— mislabeled port，不是更小的 port」）。
那段是防止后续轮次把同一个发明重新登记的唯一记录，已恢复并在末尾补一句
"That retraction stands: nothing below re-opens it"。子代理的新内容（四个 C75 阻塞点已被
后续 kernel 消解、两条 C75 主张维持被驳）都保留。

## 7. 本轮结束时仍然打开的面（不含糊过去）

1. **13 failed / 604 passed**，命令：
   `pytest tests/test_provider_model_contracts.py tests/test_provider_catalog.py tests/test_openbb_map.py tests/test_domains.py tests/test_provider_domains.py tests/test_contract_models.py -q --no-cov`。
   全部由 `intrinio::CalendarIpo` 半落地引起：binding 已注册、domain 未注册
   （`intrinio::CalendarIpo names unregistered domain intrinio_calendar_ipo`）、
   `ContractModel` 类不存在、catalog `assert 55 == 54`、以及 5 个 falsifier 层红。
   注意：roadmap 的 `shipped_cover_rows` 现在**包含** `OBB2-intrinio-CalendarIpo`
   （need = `param.value_mapping` + `path.explode`，两者都 present），所以这张面在当前树上
   描述的是一个还不能路由的 binding。已派两个子代理并行修：一个补 domain/contract model/map 行/
   catalog 重derive，一个修 `amend_first_record` 打在 `resolve_rows()` 副本上
   （falsifier 损坏的字节引擎根本不读）这一层。**不得放宽任何 judge**。
2. `UNPROVEN: ['outside.source_absent']` —— 设计如此（文件系统事实，故意不锚定）。
3. **`columns.cast` / `columns.rescale` 反向漂移，本轮实测到「探针指向不存在的字段」这一层**：
   两条 entry 的 probe 是 `DeclarationProbe(call="ColumnSpec", keyword="format")` 和
   `keyword="scale"`，而 `ColumnSpec`（`_engine/spec.py:205-210`）的字段只有
   `name / source_key / required / nullable / units` —— **`format` 和 `scale` 都不存在**，
   所以这两条 probe 在生产声明里永远数到 0。kernel 其实早就落地了，只是在另一个类上：
   `DeriveStage`（`value_transform.py:517`）带 `kind` / `cast: CastSpec | None` /
   `scale: ScaleSpec | None`（`:531-533`），`STAGE_SEQUENCE = ("cast","scale")`（`:513`），
   epoch 毫秒/秒转换与强制时区在 `:163-174`，`input_format` 的 strptime 重读在 `:119`，
   缩放的方向与因子在 `:54`。把两条 entry 改成 `read_from_disk=True` 并锚到
   `cast: CastSpec | None = None` / `scale: ScaleSpec | None = None` 后实测：
   present `10 -> 12`，`shipped_cover_rows` `8 -> 11`，新进入「今天可声明」的三行是
   `OBB2-benzinga-AnalystSearch`、`OBB2-federal_reserve-YieldCurve`、
   `OBB2-government_us-TreasuryAuctions`。**本轮不发布这个放宽**，原因是第 2 行与本仓
   已有的撤回冲突（`federal_reserve/specs.py` 记录 YieldCurve 的 long melt 不可声明，
   见任务 #30）——即这三行的 need 集很可能和 OECD 一样少报，放宽 present 集而不先逐行
   审计 need 集，正好会把 C89 要消灭的那张假面重新造出来。审计已派子代理，返回后再改
   registry、重derive 并配双臂 witness。
4. 4 条 TMX 行**从未审计**，而 `shipped_cover_rows` 的 8 行里 **4 行是 TMX**
   （`EtfHoldings`/`EtfSectors`/`EtfCountries`/`EtfInfo`）——剩下的 8 行里一半未经复核。
   本文初稿在这里写的是「没有 tmx 包，所以无法复核」，**那句是我没测就写的**：
   实测 `openbb_tmx` 确实没装进 site-packages（`importlib.util.find_spec('openbb_tmx')` 返回
   `None`，而 `openbb_intrinio`/`openbb_sec`/`openbb_fmp` 都在），但上游 monorepo 检出在本机
   可读：`/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers/tmx/openbb_tmx/models/`
   （`etf_holdings.py` 146 行、`etf_sectors.py` 99、`etf_countries.py` 101、`etf_info.py` 239），
   而且本仓 C65 的证据档案本来就用 `openbb_platform/providers/...` 作 baseline 路径
   （`docs/evidence/C65/provider-nearest-review.json`），所以这条 cite 是可解析的、不是悬空的。
   已经 grep 到字节层的事实：`etf_holdings.py:132-133` `.replace("NA", None).replace("N/A", None)`
   与 `etf_info.py:226` `.fillna("N/A").replace("N/A", None)`、`etf_sectors.py:79`
   `.replace({float("nan"): None})` —— 这正是 registry 里 `columns.null_sentinel`（absent，
   标签 `sentinel_value_nulling`，别的行已用 28 次）；`etf_sectors.py:65-68` 与
   `etf_countries.py:63-69`、`etf_info.py:183-219` 的 `query.symbol.split(",")` + `for symbol`
   循环，同包 6 行用 `multi_request_fanout` 标同一形状，这 4 行一个都没标；
   `etf_holdings.py:89,105` 的 `.replace(".TO","")` / `.upper()` 是同包
   `OBB2-tmx-EtfSearch` 已经用 `symbol_normalization` 标过的形状。
   待子代理给出逐行 file:line 的完整阻塞表后，按 OECD 的做法补 need 集、重derive、
   配双臂 witness 再发布。**注意不要把这些循环错标成 `flow.fan_out`**：
   `flow.fan_out` 的 `why` 说的是「没有 per-symbol **请求集**」，而这几个循环是在
   **一份已取回的文档**上按 symbol 反复选取，真正的缺口是「把循环变量写成输出列」
   （`etf_sectors.py:76` `target["symbol"] = symbol.upper()`），那是另一码事，必须实测再定标签。
5. 146 行 / 22 包的实现波、350 行 live 轴（授权闸门外）仍未开始。

## 8. 复现所需的具体常量（便于任何人重跑对表）

present capabilities（10，未变）：`columns.derive`、`columns.rename`、`columns.select`、
`decode.archive_member`、`decode.delimited`、`flow.composite_key_segment`、`flow.url_template`、
`param.value_mapping`、`path.explode`、`rows.filter`。
五个新条目的锚点：`columns.compose`→`value_transform.py:513`
（`STAGE_SEQUENCE: tuple[DeriveStageKind, ...] = ("cast", "scale")`）、
`columns.period_cast`→`:119`（`input_format: str = ""`）、`derive.conditional`→`:596`
（`stages: tuple[DeriveStage, ...] = ()`）、`rows.filter_comparison`→`spec.py:40`
（`RowFilterOp = Literal[...]`）、`path.list_segment`→`key_composition.py:62`
（`SEGMENT_CHARSETS: dict[str, re.Pattern[str]] = {`）。
`read_from_disk` 集合仍是 5 个（未受本轮影响，pin 无需改）；
probe-less + engine_work 集合从 4 涨到 9。
