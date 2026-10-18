# C136 wave-31 step 7：落地集半径面（landing-set blast radius）

一句话：C135 §9 把"要不要为一行付全树重签"交给 ledger owner，但四个选项当时只有 A 有价格。本面用**两把**仪器把 59 行残差逐行定价，并把"半径"从二值（全树 291 / 本地 0）改成实测的三档（4 棵全树 / 202 棵部分 / 383 棵单点），于是 B 与 D 的价格也能写出来——D 的价格在第一把里是一个空零，第二把仪器（§1b）把它换成有分母的读数。本步不改产品代码、不动 `domains.yaml`、不启动任何重签。

## 1. 仪器与判据来源

`probes/landing_set_blast_radius.py`（615 行，30911 字节，sha256 `7650cc665cc78f545067ed803ba59492d33a30735f34744800d4aae5df2c96d2`）。判定器与 pin 走查全部 **import 仓内既有实现**，不手抄：

* `scripts.quality.iter2_model_evidence`（平面 1 判定器本体，`sha256_bytes` 与 staleness 谓词都取它）；
* `docs/evidence/C135/…/probes/domains_pin_blast_radius.py`（step 6 的 `_pin_slots / _walk_claims / _simulate`）；
* `docs/evidence/C128/plane1-residue-readiness-20261017/probes/residue_axis_census.py`（出货版动作图：arm F 的行分区逐字段取它的 `seam / fake_ok / name_in_package / action / witness_module`，而不是在本文件里打一个类别集合）。

七臂全绿与出口读数（载档 `runs/landing_set_blast_radius.take5_final_bytes.txt`，行号即该文件行号）：

| 读数 | 载档行 |
| --- | --- |
| `landing_set_face arms=7 arms_passed=7 arms_failed=0 identity_set=5168e1e723999312 PASS=True` | `:82` |
| `landing_set_blast_radius_exit=0` | `:83` |
| `population: inventory=350 accepted=291 residue=59 status_counts={'INCOMPLETE': 22, 'OFFLINE_ACCEPTED': 291, 'UNREVIEWED': 37} accepted_claims=1455` | `:26` |

身份摘要 `5168e1e723999312…` 与上两代一致：本面只读，不改身份集。

### 1b. 第二把仪器（由 §7 第 7 条那个空零缺陷逼出来的）

`probes/landing_target_radius.py`（229 行，9938 字节，sha256 `779506ac1174ea2da7dc89826d3edfd79f8e699805c759628d331d6a16f73ca5`）。它不引入新的判定源，而是 `import landing_set_blast_radius as sib` 复用同一份 judge / step6 走查 / `_census_rows` / pin 普查，只把 D 选项的定价对象从"由已验收行派生的闭包"换成"该 provider 磁盘上的文件 × pin 普查"——载档 `runs/landing_target_radius.txt`（头部 26 行写口径，正文 `:27-:53`）：

| 读数 | 载档行 |
| --- | --- |
| `population: accepted=291 accepted_claims=1455 pinned_paths=589 identity_set=5168e1e723999312` | `:27` |
| `provider=yfinance files_globbed=12 pinned=0 max_radius=0 sum_radius=0 top=[]` | `:28` |
| `provider=fmp files_globbed=26 pinned=23 max_radius=69 sum_radius=826` | `:29` |
| `A_population_non_empty yfinance=12 fmp=26 PASS=True` | `:30` |
| `B_pin_reconciliation mismatched={} PASS=True` | `:31` |
| `C_local_target_is_free provider=yfinance pinned=0 max=0 PASS=True` | `:32` |
| `D_control_is_not_free provider=fmp pinned=23 max=69 expected_max=69 expected_max_from_face=G_radius:max_partial_rows PASS=True` | `:33` |
| `E_engine_price_list paths=3 max=26 PASS=True`（逐路径 `:35-:37`） | `:34` |
| `F_row_level_membership rows=12 expected=12 providers=['yfinance'] without_witness_module=12 labels={guard 11, retarget 1} PASS=True`（12 档身份 `:39-:50`） | `:38` |
| `G_generation_untouched accepted=291 claims=1455 PASS=True` | `:51` |
| `landing_target_radius_face arms=7 arms_passed=7 arms_failed=0 failed=[] PASS=True` / `landing_target_radius_exit=0` | `:52` / `:53` |

两面同代：`:27` 的 291/1455/589 与第一把的 `:26`/`:57` 逐字相等，`G_generation_untouched` 是这条同代断言的臂而不是散文。

同一把仪器在暂存时刻又跑了一遍，留作可复算与非写入见证：`runs/second_probe_rerun_verbatim.txt`（`:24-:26` 是本 run 自己的判据行，`:53/:54` 是 stdout 的两行面读数，`:56` 是该载档出口）——`verbatim_identical=True`（重跑 stdout 与 `landing_target_radius.txt` 正文 27 行逐行相等，两侧 sha16 都是 `8bab37b81cf8521c`）、`porcelain_equal=True`（仓级 `git status --porcelain=v1 -uall` 在跑前/跑后同一回合取两次、对称差为空，即跑这把仪器不改树），`second_probe_rerun_verbatim_exit=0`。第一版这把判据把 `arms_passed` 按位置索引取，取到了 `arms_failed=0`，于是把一个 7/7 的绿 run 签成 `exit=1`——那条红读数和"同一回合内我自己刚建的载档被原地改了一次"都记在 §7 第 9 条。

## 2. 面 1：全树集合的三方对账，且两枚篡改反例现场重测

`A_tree_wide derived=[domains.yaml, catalog.py, iter2_claims_fixtures.py, iter2_claims_probe_runner.py] expected=[…] step6=[…] step6_line_found=True PASS=True`（`:27`）。三个集合逐字相等：本面按 pin 走查派生（`len(rows)==len(accepted)` 的路径即为全树）、我在文件里写的预期四棵、以及 step 6 载档里那行 `tree_wide_paths(rows=291): count=4` 的解析结果。派生口径是"该路径被全部 291 行的 pin 引用"，不是"看起来像共享文件"。

两枚 counterfact 都是**内存里造哈希**（不落盘任何产品文件），失效条件就是它们中任一读到 0：

* `D_local_tamper path=scripts/quality/iter2_witnesses/fmp.py slots=414 stale_rows=69 expected_rows=69 tree=291 PASS=True`（`:47`）——改一棵 provider 本地被钉路径，staleness 落在严格子集上，且子集与闭包普查说的那 69 行**逐行相等**（`d_rows == expected_d`，不是两个数看着一样）。
* `E_tree_wide_tamper path=opendata/data/domains.yaml slots=1455 expected_slots=1455 stale_rows=291 tree=291 PASS=True`（`:48`）——`domains.yaml` 追加一行注释字节，全部 291 行、1455 个 pin 槽同时失效。A 选项的价格就是这行读数，且它是本面重测的，不是引用 step 6。

这两臂的意义：如果半径普查是空的或谓词永不触发，`stale_rows` 会是 0，arm D 的 `0 < stale < tree` 与 arm E 的 `== tree` 都会红。

## 3. 面 2：59 行的价格表（arm F，分区由出货动作图的字段生成）

`F_pricing partition=59 residue=59 wide_unproven=[] empty_classes=[] contradictions=[] PASS=True`（`:49`）。三条合起来才是这张表可用的理由：分区行数与残差行数相等（不漏不重）、任何"声称某棵全树文件是关键前提"的类别都必须能在 arm A 的全树集合里对上（`wide_unproven`）、没有空类别、且没有"动作说要改 harness 但出货普查说这行的假端点是够用的"这种自相矛盾（`contradictions`）。

| 类别 | 行 | 载档行 | 一次落地的重签代价 |
| --- | --- | --- | --- |
| `provider_local_landable`（11 `add_code_carrying_guard` + 1 `retarget_fetcher_output`，全在 yfinance） | 12 | `:52` | 既存重签 0，但**依据不是**第一把的 `:73`（那是空输入的空零，见 §7 第 7 条）；用的是第二把 `landing_target_radius.txt:28/:32`：该 provider 磁盘 12 个 .py 在 pin 普查里 `pinned=0`，fmp 对照臂同谓词读 23/69（`:29/:33`）；先例 n=6（`:80`） |
| `ledger_ruled_out`（seeking_alpha 3 / wsj 3 / cboe 2 / econdb 2 / intrinio 1） | 11 | `:50` | 不适用：已被账本判掉，不进平面 1 |
| `new_model_key_step6`（key=`domains.yaml`；tmx 16 / yfinance 2 / government_us 1） | 19 | `:51` | 291（`:48` 直读） |
| `wide_harness`（key=`iter2_claims_probe_runner.py`；yfinance 15） | 15 | `:54` | 291（同集合成员，`:27`） |
| `wide_catalog`（key=`catalog.py`；biztoc 1 / stockgrid 1） | 2 | `:53` | 291（同集合成员，`:27`） |

`actions: labels=6 rows=59 {add_code_carrying_guard 11, implement_missing_model 19, ledger_ruled_out 11, new_provider_package 2, retarget_fetcher_output 1, shard_entry_needs_harness 15}`（`:46`）——五个类别是这六个动作标签的并，11+19+12+2+15=59。
`price_face residue=59 rows_behind_a_tree_wide_premise=17 rows_new_model_needing_key=19 rows_landable_provider_local=12 rows_ledger_ruled_out=11 …`（`:56`）：`17 = 15 harness + 2 catalog`，加 19 行 `domains.yaml`，共 36 行落在全树前提之后。
`domains_key_predicate provider_snake_key_absent_rows=19 accepted_counterexamples=67`（`:55`）——"没有 domain 键就不能落地"不是普适律：67 行已验收行没有该键也照样验收了。这条限定写在面里，防止后来人把 step 6 的先例当成必要条件。

## 4. 面 3：半径不是二值（arm G）

`G_radius pinned_paths=589 tree_wide=4 partial=202 singleton=383 max_partial_rows=69 tree=291`（`:57`）。所以"共享前提"不是一个开关：202 棵路径的半径在 2..290 之间（最大实测 69，fmp）。载档 `:58-63` 打了半径最大 6 棵的**样本**（`--partial-lookback` 默认 6，population 是 202，别把它当全集）：`opendata/data/providers/fmp/models/_client.py`、`…/fmp/provider.py`、`…/fmp/registration.py`、`scripts/quality/iter2_witnesses/_parts/fmp/rows_a|b|c.py` 各 69 行。

同一口径在第二把仪器里量出"本地"与"`_engine`"之间也有半径：`E_engine_price_list paths=3 max=26`（`landing_target_radius.txt:34`，逐路径 `:35-:37`）——`opendata/data/providers/_engine/http_json.py` 半径 26，`decoders.py` 与 `row_expansion.py` 各 3。这条是给 D 选项划界用的：D 的免费只在 **provider 自己的前缀内**成立（glob 口径见 §7 第 7 条），伸手进 `_engine` 就落到 26 行的半径上，而 26 正好等于第一把 `:29` 里 cboe 的 `max_local_rows=26`。

`G_precedent landings=25 touching_partial=25 touching_none=0 exposure_min=2 exposure_max=198 corrected_min=0 corrected_max=104 priced_zero=6 tree=291 … PASS=True`（`:81`）。这行是本面真正的结论所在：

* 25 个"落地提交"（先例=只碰 provider 本地文件、不动四棵全树文件的 `feat(witness)` 提交）**全都**至少碰了一棵部分半径文件（`touching_none=0`）。我原打算把"存在一个不碰部分文件的落地"写进判据——实测证明它不可达，于是那条被我删掉了；把它写成"必须为 0"会造出一个数值正确却永远红不出来的假门。
* 但 `exposure_min=2` 的原始读数是**自计数假象**：`exposure sha=0cadc10a5 delta=2 partial_paths=1 union_rows=2 preexisting_paths=0 preexisting=[] wide=[] files=['scripts/quality/iter2_witnesses/eia.py']`（`:74`）——eia 那 2 行是新 witness 模块自己钉自己新落地的行。所以 arm G 把每棵被碰路径拿去问"它在这次提交的父提交里存在吗"（`git ls-tree <sha>^`），只有既存路径才算暴露：`G_selfcreated landings_priced_zero_preexisting=6 of=25 corrected_min=0 corrected_max=104 all_local_paths_new=[0c06cc7a5, 95a12cf38, 7f728b65f, 81e6159e4, 0cadc10a5, d78edd285] ls_tree_calls=75 local_paths_total=107`（`:80`）。
* 6/25 的先例把**既存**行重签为 0。`exposure sha=6a90cba29 … partial_paths=22 union_rows=198 preexisting_paths=13 …`（`:79`，样本另一侧）是反面：同一种"只碰本地文件"的提交，实际重签 198→修正后 104 行。`exposure sha=` 六档是 `--exposure-sample` 默认 3+3 的两端样本，population 是 25；`preexisting=[…]` 只打前两个名字，计数一律看 `preexisting_paths=`。

## 5. 面 4：先例走链（arm C）与它的断链成本

`C_precedent feat_witness_commits=31 no_delta_skipped=6 with_plane1_delta=25 tree_wide_free=22 tree_wide_priced=3 rows_landed_free=86 rows_landed_priced=179 rows_total=265 newest_landing=a70fb295f endpoint_after=291 judge_accepted=291 chain_breaks_outside_population=3 breaks=[(94b0b14c9, 153, 68), (7f728b65f, 256, 257), (a70fb295f, 289, 290)] PASS=True`（`:34`）。

* 两尾对账：`endpoint_after=291 == judge_accepted=291`——链的终点与本面的判定器读数是同一个数，否则"先例"与"现状"就来自两代。
* 22 个不碰全树文件的提交带走了 86 行，3 个付了全树代价的提交带走 179 行；`86+179=265`。**这 265 与 `endpoint_after=291` 之间差的 26 行，本面没有归因**（第一代这里写的是"与平面 1 从 26 到 291 的增量对齐，另外 26 行属于 population 之前的历史"——那句是我编的，见 §7 第 8 条）。不 align 的理由在臂自己的定义里：`rows_*` 只累加 `delta>0` 的提交（负 delta 被丢掉，`probes/landing_set_blast_radius.py:356-357`），population 又被 `feat(witness)` 前缀过滤（`no_delta_skipped=6` 是连 delta 都解析不出的），而 3 处断链说明链本身不连续。arm C 不打印这三项各占多少，所以 26 只能记作**未归因残差**，不能记作对齐。
* 3 处断链是"population 被前缀过滤"这个口径的代价，不是判定器的错。`breaks=(sha, 上一档 after, 本档 before)`（`probes/landing_set_blast_radius.py:362-366`），所以 `(94b0b14c9, 153, 68)` 读作"上一档消息说 153，该提交消息说自己从 68 起"——**85 行的下降**发生在两档 `feat(witness)` 之间，即由 population 外的提交造成。我第一代还给它配了一个机制（"行被挪进别的动作类、行数不变"），那个机制同样站不住：`retarget_fetcher_output` 型落地确实行数不变，但行数不变产生不了 153→68 的下降。断链只归因到"population 之外"，不编造具体是哪类提交——仪器自己两处早就是这么写的：模块 docstring 的 arm C 段 `:44-47`（"a plane-1 move recorded by any other commit type shows up as a break"）与 main 里的注释 `:358-359`（"the trajectory is only contiguous over *all* commit types"）。散文第一次盖过了仪器。载档 `:35-42` 是 8 档 `precedent sha=` 样本（`--wide-lookback` 默认 8，population 25），`:43-45` 的 3 档 `priced sha=` 是全集。
* `B_closure providers_residue=10 with_local_closure=5 no_landed_sibling=[biztoc, seeking_alpha, stockgrid, wsj, yfinance] closure_leaks_tree_wide={} PASS=True`（`:28`，逐 provider 读数 `:29-33`、`:64-73` 十档为全集）：10 个残差 provider 里 5 个已有验收行可作闭包参照，5 个没有——yfinance 就在没有的那一组（`:73` `accepted_rows=0 closure_union=0 closure_paths=0`）。**这一行的两个 0 是空输入的空零**：closure 是对"该 provider 的已验收行 × 其 pin"求并，`accepted_rows=0` 时并集必然为空，它对 yfinance 磁盘上的文件一个字也没说。第一版把 D 选项押在这行上，现在撤回（§7 第 7 条），改押 `landing_target_radius.txt:28/:32` 那个有分母的读数。先例厚度这条仍然成立，见 §8。

## 6. §9 四个选项现在带的价格

* **A 为一行付全树重签**：291 行再签 + 1455 pin 槽（`:48` 直读），换 plane 1 `291 → 292`。每行成本 291。
* **B 一批摊薄一次重签**：36 行（19+15+2）落在这四棵全树文件之后（`:51/:53/:54` 加 `:27`）。若同一批一起落地，一次重签周期摊到每行 ≈ 291/36 ≈ 8.1。**"一次周期能同时摊薄这三棵文件"本面未测**——arm E 只篡改了 `domains.yaml` 一棵；这是预测而非读数，验证办法见 §9。C135 §9 的"其余桶是否也落在这四棵上，本步未测"这一条已被本面测掉：是，且只有 12+11 行不在全树前提上。
* **C 摘掉 `domains.yaml` 的 contract 钉**：放宽判定器，把"证据指向的字节确实存在"换成口头保证。不在我的处置权内；arm E 存在的意义就是让这条的代价可读数（291 行的 pin 就是它换来的东西）。
* **D 暂不动共享前提**：12 行 yfinance `add_code_carrying_guard`/`retarget_fetcher_output`（`:52`，12 档逐行身份在第二把 `landing_target_radius.txt:39-:50`）。既存重签 0 的**依据换成有分母的读数**：该 provider 在磁盘上有 12 个 `.py`（glob 口径：provider 包 + `models/<p>*.py` + `iter2_witnesses/{p}.py` + `_parts/{p}/`），其中被 291 行 pin 引用的是 **0 棵**，`max_radius=0`（`:28`/`:32`）；同一谓词在 fmp 上读 26 棵 / 23 棵被钉 / `max_radius=69`（`:29`/`:33`），对照面成立，所以那个 0 是测量而不是死谓词。第一版押的 `:73 closure_union=0` 是空零，已撤回（§7 第 7 条）。代价是平面 1 停在 291 直到这批也落完。

## 7. 撤回与更正留痕（九条，逐条留数，不静默覆盖）

1. **23 → 12**：take3 的 `rows_landable_provider_local=23` 把动作图里 11 行 `ledger_ruled_out` 也算进了"provider 本地可落地"。原因是 arm F 的 `else` 兜底桶名字叫 `provider_local`。修正方式不是改数字而是改分类：`provider_local_landable` 独立成类、`ledger_ruled_out` 独立成类、每个类别打印自己的动作构成（`:50-54`）。take2/take3 原样保留：那个 23 是"另一个集合"的真实读数，不是笔误。
2. **命名空间启发式整体废弃**：我曾按路径名猜"本地 vs 共享"，对 `scripts/quality/iter2_witnesses/_parts/<prov>/*.py` 这种分片布局判错；改成用 pin 集合的半径 + 父提交 `ls-tree` 查既存，二者都是磁盘读数。
3. **79 → 75**：take4 头部写"79 git ls-tree children"，那 79 是我算术出来的，不是读出来的。arm G 现在自己打 `ls_tree_calls=75 / local_paths_total=107`（`:80`）——计数器就是修正，之后这个数只可能来自载档。
4. **`git cat-file -e` 的 rc=128**：它把"磁盘上有、父提交里没有"当作 fatal 退出 128，正好是我要问的那种情况，于是 arm G 首版在这里崩。现在判定取自 `ls-tree` 的 stdout 是否有输出，rc 只留给真失败。
5. **A2 空跑（本回合的缺陷，红面留档）**：我第一次用 `--files probes/landing_set_blast_radius.py` 在 bundle 目录里跑 A2，日志里只有一行红（既存的 pinned gitleaks），看起来几乎像"其余都过了"。实际是 `NOTHING CHECKED: 0 of 1 requested paths are A2 candidates`——解析器把显式路径按 `REPO_ROOT` 锚定（`scripts/quality/a2_check.py:488`），ruff/mypy/bandit 一把都没跑在我的文件上。红面留在 `runs/a2_static_plane_slice_relative_path_red.txt`，不删。
6. **bandit 那把 `"-f json"` 被当成一个 argv**：更初代的写法让 bandit 用法错误退出 2、stdout 为空，而我的计数解析打印 `findings=0`——一个"什么都没分析"的运行读成 0。现在解析器对不可解析/无 `results` 一律打 `PARSE_FAILED`，`notes/static_plane.txt` 末尾把这条缺陷写明而不是抹掉。
7. **空零当价格（本回合最重要的自捕缺陷）**：第一版把 D 选项的"既存重签 0 行"押在 `:73 local provider=yfinance accepted_rows=0 closure_paths=0 closure_union=0`。但 closure 是**对已验收行的 pin 求并**得到的，`accepted_rows=0` 时输入集为空、并集必为空——那个 0 与 yfinance 磁盘上有什么文件毫无关系，它测量的东西不存在。修法是加一把仪器而不是改散文：`probes/landing_target_radius.py` 把分母换成"该 provider 的 `.py` 文件 × 同一份 pin 普查"，并自带三件为能的对照——`A_population_non_empty yfinance=12 fmp=26`（`:30`，每个后续 0 都有分母）、`B_pin_reconciliation mismatched={}`（`:31`，glob 集合与该 provider 前缀下的 pin 键互相对账，写错前缀会立刻露）、`D_control_is_not_free`（`:33`，同一谓词在 fmp 读 23 棵被钉 / max 69，且 69 是从第一把 `G_radius:max_partial_rows` 取的，不是手打）。第二把自己的 `landing_target_radius.txt` 头部 :1-:26 就把"为什么要第二把"写在载档里。
8. **两处编造的因果**：§5 的"265 与 26→291 的增量对齐（另外 26 行属于 population 之前的历史）"和"断链是因为行被挪进别的动作类"都不是读数，是我给两个未解释数字配的机制。前者被臂自己的定义否掉（`rows_*` 丢负 delta、前缀过滤掉 6 档、链上有 3 处不连续，26 无法归因），后者被数字否掉（`153 → 68` 是 85 行的**下降**，"行数不变"的机制产生不了下降）。两处已改成"未归因/只归因到 population 之外"，并给出仪器的原话行号。
9. **判据行按位置索引取字段，把绿 run 签成红**：暂存时刻重跑的 `runs/second_probe_rerun_verbatim.txt` 第一版用 `arms.group(0).split()[2]` 去取 `arms_passed`，实际落到 `arms_failed=0`，于是 `rerun_reproducible=0`、出口 `exit=1`，而 run 自己是 `run_exit=0 / arms 7/7 / 正文逐行相等 / 树未动`。这条的形状与 §7 第 6 条同源（一个不该被当作读数的通道被当成读数），但这次红的是**我自己的判据**，不是被试。修法：按字段名解析（现在这版打印 `arms_passed=7 arms_failed=0 failed=[]`，`:24-:26`）。附带披露：那个红版本没有被另立一档——它写在同一把路径 `runs/second_probe_rerun_verbatim.txt` 上、三分钟前由我创建、除本节外无人引用，所以是原地改写；"历史载档不覆盖"这条对**本回合自己刚建的scratch** 放宽了，放宽的边界就写在这里，供后来人按同一情形判断。

## 8. 披露的口径边界

* `:58-63`（6/202）、`:74-79`（6/25）、`:35-42`（8/25）是**样本**，默认值来自 CLI（`--partial-lookback 6 / --exposure-sample 3 / --wide-lookback 8`）；全集读数只在带 `=` 的计数字段里。`:64-73`（10 个 provider）、`:43-45`（3 个 priced 提交）、`:29-33`（5 个有闭包的 provider）、`:50-54`（5 个类别）是全集。第二把仪器没有样本臂：`landing_target_radius.txt:35-:37`（3 棵 `_engine`）、`:39-:50`（12 行 yfinance 身份）与 `:28/:29`（两 provider 的 glob 全量）都是全集，这是它比第一把窄的地方，也是它能给 12 行逐行身份的原因。
* arm F 不打逐行身份，类别只到 provider 计数；逐行身份由第二把的 arm F 补（`landing_target_radius.txt:38-:50`）。可复算路径两条都在：`docs/evidence/C128/plane1-residue-readiness-20261017/probes/residue_axis_census.py` 是第一把 arm F 的数据源（in-process 执行，本面的 59 行就是它的当前读数）；它在 C128 里最后一把载档是 wave28 那代（`residue=74`），对本代已 stale，所以本面不引用那把载档的数字。
* D 选项的先例厚度：`zero_accepted_providers=[biztoc, seeking_alpha, stockgrid, wsj, yfinance]`（`:81`）里 yfinance 是残差方而非落地方——**没有任何一行 yfinance 落地过**。n=6 的零重签先例来自 eia/ecb/government_us 等 provider，把它们外推到 yfinance 是解释，不是读数。第二把仪器把"半径 0"这条从解释升成读数，但没有、也无法补上"yfinance 落过一次"这个先例。
* 本面对 §9 只做定价，没有选择、没有执行；平面 1 仍是 291/350，`domains.yaml` 未被改动。

## 9. 预测判据（下一面必须读到）

**若选 D 的一批（yfinance 12 行，逐行身份在 `landing_target_radius.txt:39-:50`）**：plane 1 `291 → 303`（增量 12，来自 `:52`），**既存 291 行重签 `stale=0`**——这条的依据是 `landing_target_radius.txt:28/:32`（12 个磁盘文件、0 棵被 pin、max 半径 0）而不是第一把 `:73` 的空零；它的适用条件是这批的改动**留在 provider 前缀内**，越界的价由 `:34-:37` 现场给出（伸进 `opendata/data/providers/_engine/http_json.py` 就撞 26 行）。身份摘要仍 `5168e1e723999312…`，且 `G_radius` 的 `partial` 数会因新增 witness 分片而上升（`tree_wide` 必须仍是 4——若变成 5，说明这批把某棵本地文件写成了共享文件，本面的分类作废）。落地后重跑第二把仪器即可自查：`provider=yfinance` 那行的 `pinned` 应从 0 变成非零，而 `max_radius` **必须 ≤ 12**——12 是本批自己的行数上限，任何大于 12 的读数都说明既存行被卷进了这批的 pin（我不预测它恰等于 12，因为那取决于这批切成几把 witness 分片，而分片数还没定）。`D_control_is_not_free` 臂仍须对 fmp 读到 69（若它跟着变，说明判定代际动了，不是这批动）。失效条件：`tree_wide != 4`；或 `max_radius > 12`（既存行被卷进来的读数形式，回到 `landing_target_radius.txt:31` 的 `B_pin_reconciliation` 看是哪个前缀把两批混在一起）；或落地后对 yfinance 的 `:47` 型篡改臂读到 `stale_rows > 12`。

**若选 B 的一批（36 行）**：预测 plane 1 `291 → 327`，一次重签周期后 `E_tree_wide_tamper` 型臂对 `domains.yaml` 读到 `stale=0`（327 行、槽数 = 327×5 = 1635），且 `domains_total` 只随**键数**增长——新增行数 K 与新增键数不一定相等，二者之差要显式列出（C135 §9 同一条警告在此仍然有效）。若一批里三棵全树文件都改了，"一次周期摊薄"这个未测预测的检验是：只跑**一轮** producer→recorder→merge→judge 就得到 `stale=0`；若读到 `stale>0` 且残差集中在 `catalog.py` 或 `probe_runner` 的 pin 槽，则说明周期摊薄不成立、B 的价格是"每棵文件各一轮"，我要按 3×291 重算并把本节改成读数。

读数口径（与 step 6 相同）：291/1455/589/202 的分母是"已验收行的 claim 与其 pin 槽"，不是全部 1565 条 claim；`residue=59` 里只有 22 行带 claim（`INCOMPLETE`），`UNREVIEWED` 的 37 行没有 claim 参与半径计算——这一点决定了 arm G 的 `union_rows` 不会超过 291 行的 pin 集。

## 10. 静态平面与提交载荷

A2 切片，单探针那代（第一把的出货字节）：`runs/a2_static_plane_slice_final.txt` —— ruff check ok / ruff format --check ok / mypy ok（1 个 target，`exit=0`）/ bandit ok；唯一红是既存的 `pinned Gitleaks file scan`，25 档全部落在 `docs/evidence/C100/**/inputs/pristine/akshare/*.py`，C136 与 C135 下 0 档（`a2_static_plane_slice_final_exit=1`，`real 218.29s` 是 gitleaks 走 C100，不是我的文件）。
A2 切片，**两把探针这一代**（载荷真正被 `git add` 前的那一次）：`runs/a2_static_plane_slice_two_probes_final.txt` —— 解析器 `A2 files (complete resolver): 2`（`:33`，两把都被收进候选集，不是上次那种"0 of 1"的空跑）；ruff check ok `:90` / ruff format --check ok `:91` / mypy ok `:92`（2 个 target，逐 target `exit=0`、`stdout_chars=164`＝只有 pyproject 的 unused-section 提示，无诊断）/ bandit ok `:104`；唯一红仍在 `:39-:89` 那个 gitleaks 成员，五个数与单探针代**逐项相同**：`returned_lines=25 distinct_files=25 under_C100=25 under_C136_or_C135=0 sum_leaks_found=30`，所以加入第二把探针让红档数动了 0。出口读数取自 harness 的任务输出行 `a2_two_exit=1`（载档头部把这条写明），并把 `a2_static_plane_slice_two_probes_final_exit=1` 签在本载档最后一行。这一代**没有**计时读数：命令没套 `/usr/bin/time`，218.29s 只属于上一代。对照控制：`docs/evidence/C135/wave31-step6-domains-pin-blast-radius-20261018/runs/a2_static_plane_control_untouched_probe.txt`（"红与是否新增本 bundle 无关"）。
独立平面读数：`notes/static_plane.txt` —— 第一把（§1-§6）：ruff 0 violations、format clean、bandit 用出货配置（argv 取自 `scripts/quality/a2_check.py:456`）读到 0 命中，两把为能的对照臂把它从 0 推到 2（B603+B607：删掉新 helper 那行的行内 `nosec`）与 1（B602：植入 `shell=True`）。第二把（§7）：ruff 0 / format clean / bandit 0（stdout 814 B），为能对照臂推到 2（B404+B602，植入 `subprocess`+`shell=True` 的 `/tmp` 副本），运行面 7/7。植入件只在 `/tmp`、不入库。§7.4 里"第二把比第一把快 3.53s"这代数字是本回合刚改的——第一代写的是"1.4s not 12.5s"，那个 1.4 是我编的。

载荷清点口径：`git -c core.quotepath=off status --porcelain=v1 -uall -- <bundle>`，不是 `find`、不是散文自数。本节的读数按代并列（每代都是当时从磁盘测的真数，不是笔误）：`untracked=10 by_suffix={'.txt': 9, '.py': 1} by_dir={'notes': 1, 'probes': 1, 'runs': 8}` = 第二把仪器与其载档写入之前；`untracked=13` = 重跑见证载档 `runs/second_probe_rerun_verbatim.txt` 写入之前；`untracked=14 by_suffix={'md': 1, 'txt': 11, 'py': 2} by_dir={'face.md': 1, 'notes': 1, 'probes': 2, 'runs': 10}` = 凭证扫描第一代（下面那段）物化 payload 之前。`probes/__pycache__/` 由 `.gitignore:2`（规则 `__pycache__/`，`git check-ignore -v` 现场验）覆盖、不入载荷。14 档那次还跑了逐档出口检查：11 把 .txt 全部命中 `\b\w*_(exit|rc)\s*=\s*-?\d+`（`missing_exit_line=[]`），且逐档**末个非空行**整行匹配该式（11/11），即没有一把载档需要本节替它签出口读数。**本节不再自数档数**：权威载荷档数由最后一代扫描载档自己的 `staged_files=` 给，即 `runs/staged_payload_credential_scan.take2_final_index.txt`，它与 HEAD 的字节一致性由 `runs/commit_time_payload_at_head.txt` 逐档 digest 对账。

提交前凭证扫描（扫的是 index 里的 staged blob，不是工作树）：`runs/staged_payload_credential_scan.take1_c136_payload.txt`。仪器不复制、不改写——`importlib` 载入出货版 `docs/evidence/C135/wave31-step6-domains-pin-blast-radius-20261018/probes/staged_payload_credential_scan.py`，只把 `BUNDLE_REL` 覆成本 bundle、`SCRATCH_ROOT` 覆成 `/tmp/w31s7_scan`，覆成本身在载档 `:29-:31` 打印。读数：`staged_files=14 staged_outside_bundle=0 outside_list=[]`（`:32`）、`worktree_differs_from_index_files=0 list=[]`（`:35`，所以提交携带的字节就是被读过的那 14 档，没有"`git add` 之后又改"的那类缺陷）、`payload_index_sha256=27d4a71…59306e`（`:34`）；三种配置同一 payload 的 baseline 全 0 且植入各读 2（`generic-api-key` 1 + `github-pat` 1，`:36-:38`），六把 gitleaks 运行全 `exit=0`（`:41`），`allowlist_suppressed_in_this_payload=0`（`:39`，无阈值，只打印），植入件隔离 `plant_files_inside_staged_payload=0 names=[]`（`:40`）；面 `credential_scan_face … arms=13 arms_passed=13 arms_failed=0 failed=[] PASS=True`（`:42`）、`staged_payload_credential_scan_exit=0`（`:43`）。植入的 token 是运行时拼出来的假值，仪器从不打印，所以它不可能进入本载档。自指范围一档一档往前闭合，不靠本节声称覆盖：第一代扫 14 档、它自己不在内；第二代 `take2_final_index.txt` 把第一代载档一并扫进去，扫前工作树 census 读 15（14 + 第一代载档），这 15 是**本节对扫描读数的预测**，它自己的 `staged_files=` 才是读数——读到 `!= 15` 即本节这半句作废、按载档改正，读到 `worktree_differs_from_index_files != 0` 则说明本节之后又动过文件，属提交前停止项。take2 自己则由提交后的 `commit_time_payload_at_head.txt` 覆盖：那把载档对 HEAD 逐档 `git show HEAD:path | shasum` 与工作树 digest 对账，并带一次针对 HEAD 的 gitleaks 读数；它对本节来说是**尚未存在的前向引用**，落在那之后紧跟的第二把证据提交里（两把都只动 docs/evidence，产品代码与身份集不为它们改变）。顺序约束在这里是实质的：本节在 take2 运行前就定稿，所以本节**故意不打印 take2 的行号级引用**——那样做会重开刚被扫过的那份字节，正是上面 `worktree_differs_from_index_files=0` 要挡的形状。take2 的逐档读数只写在 take2 自己的头部与体内，提交说明里带它的 `staged_files=` 与 `PASS=`。

## 11. 交给 ledger owner 的最小可执行结论

1. 全树重签的价格与 K 无关：`E_tree_wide_tamper` 说 `domains.yaml` 的任何字节变化都吃 291 行（`:48`）。**所以单行落地（A）是 36 行里最贵的买法**；D 是那批唯一"既存重签 0"读数的批次——读数是 `landing_target_radius.txt:28/:32`（yfinance 磁盘 12 个 .py、0 棵被 pin），不是第一把 `:73` 的空零（§7 第 7 条），且免费只覆盖 provider 前缀内，`_engine/http_json.py` 的 26 行半径（`:34-:37`）就是边界。
2. 36 行按前提文件分三桶（19/15/2，`:51/:54/:53`）。若要摊薄，应按桶凑批；本面未测跨桶摊薄，预测判据与失效读法在 §9。
3. 半径非二值（`:57`：4/202/383）意味着"只碰 provider 本地文件"这句话本身还需要第二个问句：**碰到的那棵本地文件是不是既存**（`:80` 的 `preexisting_paths`）。`corrected_max=104` 就是"名字上是本地、实际重签了 104 行"的上界先例。
