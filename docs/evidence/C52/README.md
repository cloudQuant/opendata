# C52 — AC-9\|01/\|06/\|07「口径映射表覆盖 P0 域 + dwd 合并四问 + 修订传播」：两格做成能红的面并转正，一格量成真缺口

本轮把 §2 AC-9 里最后三个 `unreviewed` 格变成有读数的格子。做法与 C43/C48/C49 一致：先给每格写一条**条目级探针**（`measure` 读事实、`judge` 只对事实判、`Break` 是反事实、`repair` 声明唯一可达的绿），再让台账与 §10 只抄探针的读数。判据原文一字未动（`--item` 的 `drift` 面就是钉这一条的：探针的 `expects` 必须是 §2 那一行的**逐字子串**——本轮被这条绊了一次，见 §七）。

## 〇、修前的真实形状

三格在台账里都是 `{"state": "unreviewed"}`（`docs/quality/acceptance-item-ledger.json` 的 `AC-9|01|e67169e2`／`AC-9|06|53cf809b`／`AC-9|07|96ff13a5`），也就是说：**没有任何代码能回答「这一条今天成不成立」**。§10 本行的状态栏此前写着 `条目级 5/9`，其中 5 是 §2 的勾选数。

`|06`／`|07` 此前也不是完全没有面：`tests/test_dwd_merge.py` 里已有 `TestMergeSourceFrames::test_point_in_time_columns_are_stamped` 与 `TestDwdMergeService::test_affected_keys_extend_the_merge_unit`，但**没有一格服务级断言 `_as_of` 落到了写出去的那一帧**（既有那条只调 `merge_source_frames` 纯函数，看不见 `DwdMergeService.run` 传下去的 `as_of=end`），也**没有任何一条测「值真的换了」**（`test_affected_keys_extend_the_merge_unit` 只证明修订键进了合并单元）。判据点名的恰好是这两面，所以此前是「有一条相关的测试」而不是「判据有面」。

## 一、`|01` 量出来是真缺口，不是判定面缺失

探针读数（`probe-items-ac9-three.txt`）：

- 表在不在：`opendata/data/mapping.py` 有 `load_mapping`/`normalize_frame` = yes，可导出 `mapping_as_json` = yes，源文件 = akshare, ths
- P0 覆盖：**3/5** 个 P0 域在这张表里；没覆盖的是 `financial_indicator`, `financial_statement`
- 六类口径落表：字段映射=yes、单位换算=yes、**复权口径=no、key 规范化=yes、停牌语义=no、差异率分母=no**
- 另两面（只在代码里、不在这张表里时不计入覆盖）：复权在 `opendata/data/adjust.py` = yes，差异率分母在 `opendata/pipeline/cross_check.py` = yes

⇒ `VERDICT AC-9|01: gap`。三个读数细节值得单独记：

1. **分母换过了，而且必须换**。C48 那句「15 个权威域里 14 个两侧 mapping 不齐」读的是 `authority.json` 的域表，判据括号里点名的却是 **P0 域**——本轮分母取 A4.1 迁移里的 `_DWD_TABLES`（探针用 AST 直读，不抄清单），得 5 个：`stock_daily`/`stock_action`/`financial_statement`/`financial_indicator`/`index_constituent`。5 比 15 严还是宽是次要的，**取哪一个决定了这一格能不能被「权威表里恰好有两行没配上」这种读数糊过去**，所以要写明。
2. **`adjust`/`suspension`/`denominator` 三个 `no` 不是「没实现」**。`opendata/data/adjust.py` 有 `apply_adjust`，`cross_check.py` 的差异率分母就在 `compared_keys` 的注释里（"Size of the key union (rate denominator)"）。判据要的是「口径映射表**存在且覆盖**」，而 `_FIELD_KEYS = frozenset({"from", "scale", "normalize", "ms_column"})` 让这张表**结构上装不下**这三类 ⇒ 口径仍然散在代码里。探针因此把「代码里有」单列成两条附加读数，不计入覆盖。停牌语义这一类更彻底：全仓除了本轮探针自己的字面量与 `tests/test_fuyao_endpoints.py:1202` 的一句注释（「停牌 0 价行」）之外，没有任何声明面——既没有取值规则，也没有读它的代码。
3. **缺的两个 P0 域不是「往 yaml 里填两行」就能补的**。两份 yaml 今天是 akshare = `stock_daily`+`index_constituent`、ths = `stock_daily`+`stock_action`+`index_daily`+`futures_daily`+`option_daily`（⇒ P0 五域里表内只有 3 个，akshare 侧连 `stock_action` 都没有）。而 `financial_statement` 域的两条腿**当前不共享 merge key**：ths 侧入库用契约英文码（`net_profit`/`income`），参照侧（移植的 sina 腿）存中文条目名与中文报表类型——`opendata/data/providers/ths/models/financial_statement.py` 的段首自己写明「normalizing ~50 Chinese line items per statement is a 口径 mapping that needs human review (AC-4), not something to guess here」。⇒ 补这一域要先拿到那份对照的人工复核，或者把它显式登记成用户决策面；**这一格不能靠单方面编一张对照表变绿**。

缺口的两半都已登记成开发任务 #66（补两个 financial 域的 mapping + 让三类口径成为表级字段并被 `cross_check`/`apply_adjust` 读，含上面第 3 条的阻塞事实），**没有**靠改判据、改分母或把 `expects` 写宽来让这一格变绿。

## 二、`|06`：四问逐问有面，缺的那一面是服务层的 `_as_of`

新增 `tests/test_dwd_merge.py::TestDwdMergeService::test_service_writes_all_four_point_in_time_faces` 与 `::test_service_degrades_per_key_and_keeps_the_source_evidence`。第一条走 `DwdMergeService.run`（不是纯函数），断言写出去的那一帧：`stats.rows == len(written[0]) == 2`、`set(frame["_as_of"]) == {AS_OF}`（`AS_OF` 就是窗口末）、`set(frame["source"]) == {"ths"}`、`_merged_at` 只有一个值、两条分歧键的 `_diff_flag` 都是 `1`。第二条把权威源 `ths` 少给一行，断言 `stats.degraded_rows == 1` 且**降级那一行的 `source` 留在帧里**（`000001 → akshare`、`600519 → ths`）——判据的「`source` 留痕正确」在这里是可指的，不是"列存在"。

判定读数：`服务层与合并层节点: 3/3 passed`，四面（权威缺失才降级 / source 留痕 / `_diff_flag` 打标 / `_as_of` 写入）逐个 yes，`as_of=end` 与 `TRACE_COLUMNS` 四列同定义也各一条。

**真机那一面：仪器早就在，只是不跑**（本轮把它写明而不是含糊过去）。§10 本行证据列里 C4 那份 dwd 双源合并（719,314 行、降级填补 1,612、`_diff_flag` 615,610）是本轮之前落的读数，它覆盖权威/降级/打标三问，但那份档案**没有**逐行覆盖 `_as_of`。覆盖 `_as_of` 的真机断言其实存在——`tests/test_dwd_merge.py::TestDwdWriteAgainstMysql::test_merge_writes_rows_with_trace_columns_and_is_idempotent`（`tests/test_dwd_merge.py:757`）对真库写 dwd 后 `SELECT symbol, close, source, _diff_flag, _as_of` 并断言 `rows[0][4] == AS_OF` 且重跑幂等；它带 `@pytest.mark.e2e`，被门禁与本遍的 `-m 'not e2e'` 挡在外面，**跑它 = 一次生产数仓写入，须经用户确认**。所以 `|06` 这一格今天的判据面是「单元 + 服务 + 代码」三面（与判据原文「写入/留痕/打标」的动词一致），真机那一面登记为**已有仪器、待用户放行**，而不是"没有面"。

## 三、`|07`：新增一条「值真的换了」的单测，并把它证伪

新增 `TestDwdMergeService::test_revision_of_an_existing_key_changes_the_dwd_row`：reader 是**按窗口取数**的（`in_window | restated`），修订键 `("600519", 2023-12-29)` 落在第一遍窗口内、第二遍窗口之外。第一遍跑完 `close == 5.0`；把 `ths` 那一帧的该键重述成 `6.5` 后跑第二遍（窗口 2024-01-01..01-31，`affected_keys={revised_key}`），断言 `stats.rows == 2`、该行 `close == 6.5`、`source == "ths"`、`_as_of == AS_OF`；再跑一遍**不带** `affected_keys`，断言该键**根本不在**输出里——这一句是「传播面不是白拿的」：读者若把 `affected_keys` 下传去掉，第二遍就取不到那一行。

判定读数：`两条单测: 2/2 passed`＋传播链五段全 yes（runner 交出被改的键 / `run_hook` 经 `_contract_keys` 把 `600519.SH` 拼法重拼成契约键 / reader 收到这批键 / 键进 `extra_diff_keys` 参与差异重算 / 写侧 `build_upsert_sql` 按业务键 upsert）。

## 四、证伪遍（判定面不是读不出反例的读数器）

`falsification-dwd-merge.txt`（仪器 = 同目录 `falsify_dwd_merge.py`）：对 `opendata/pipeline/dwd_merge.py` 做四处**单点**临时改写（每处锚点必须**恰好出现一次**，否则脚本拒绝改），每次改完在**全新解释器**里跑受影响的节点 + 相应条目级探针，`finally` 里逐字节还原：

| 反事实 | 期望变红的面 | 实测 |
| --- | --- | --- |
| M1 `as_of=end` → `as_of=start` | 四盖面服务测 + `AC-9\|06` 探针 | `1 failed`，探针 `as_of_is_window_end = no` ⇒ gap |
| M2 `if rank > 0:` → `if rank < 0:`（降级不再计数） | 降级留痕服务测 + `AC-9\|06` 探针 | `assert 0 == 1`，探针 `权威缺失才降级 = no` ⇒ gap |
| M3 删掉 `record["_as_of"] = as_of` | 四盖面服务测 + `AC-9\|06` 探针 | `assert {nan, nan} == {date(2024,1,31)}`，探针 `_as_of 写入 = no` ⇒ gap |
| M4 `set(affected_keys)` → `set()`（修订键不下传 reader） | 值同步单测 + `AC-9\|07` 探针 | `assert 1 == 2`，探针 `reader 收到这批键 = no` ⇒ gap |

四处全部按预期变红，每处末行 `--- 还原后与改写前逐字节相同 = True`，收尾 `opendata/pipeline/dwd_merge.py 逐字节回到起点: True`，脚本 exit=0。M1 与 M3 打的是同一个节点、却扳动探针的两条不同面（`as_of_is_window_end` vs `as_of_stamped`），这正是「四面各自可红」的意思。仪器第一版为什么必须换成子进程，见 §七。

## 五、反事实与全表面

- 本轮三条探针各带反事实：`|01` 8 条、`|06` 8 条、`|07` 7 条 = **23 条**。
- 全表：`python scripts/quality/acceptance_item_probe.py --self-test` → `OK: 36 probe(s) measured; every judge is reachable from its declared repair, and all 283 counterfact(s) flip a clean reading back to a gap.`（C51 后是 33 探针 / 260 条，本轮 36／283，差的正好是新探针）
- 整文件复算：`python -m pytest tests/test_dwd_merge.py -q --no-header -p no:cacheprovider --no-cov -m 'not e2e'` → `29 passed, 1 deselected`（`pytest-dwd-merge-file.txt`；被 deselect 的那条是 `TestDwdWriteAgainstMysql::test_merge_writes_rows_with_trace_columns_and_is_idempotent`，它对真库写，门禁与本轮取证遍都不跑）。本轮新增的三条用例都在这个文件里，整文件没被改坏。
- 面基线 `docs/quality/acceptance-probe-faces.json` 由 `--sync-faces` 重写，`git diff` 只有新增三条 `"AC-9|01"/"|06"/"|07": {moment_faces: []}`，**没有一条既有探针的面被读瞎**。
- 回填前的全表逐条读数与 `--self-test` 同一遍留档：`probe-selftest-and-all-prebackfill.txt`（396 行，`PROBE_EXIT=0`，`census: gap=11, proven=25`）。**这一遍的 `--all` 不判台账↔读数的对账**：那条对账自 C50 起是门禁成员 11 每遍重跑的事。

## 六、门禁遍次

两遍 `make gate`（17 个成员 / 18 段 `===== gate:` 横幅；成员 11 = `acceptance-probe-check`，是唯一判「台账 ↔ 探针读数」对账的那一个）。分工：

- **遍 1 跑在回填之前**——三条新探针已进门禁，台账却还是 `unreviewed`，所以这一遍要读到的是「探针会拦」而不是「探针已达标」；未达标格数（`unflipped`）是这一遍的判据面。
- **遍 2 跑在回填与提交落地之后的干净树上**，作为转正后的读数（`工作树：干净`、`unflipped=0`）。

| 遍次 | 命令 | 读数 |
|------|------|------|
| 遍 1（回填前） | `make gate` | 正文见同目录 `gate-run1-prebackfill.txt`（7,503 行 = 7 行溯源头 + 7,496 行完整未裁剪正文，`GATE_EXIT=0` 读自档案自身末行，`real 9:01.16`）。逐字读数：成员 11 `本遍墙钟 = 324.3 s，读到事实的探针 36/36，测不出来的：无`、`反事实面：36/36 个探针走到了判定，283 条 break 各被施加一次`、`台账↔读数：agrees=32, unflipped=2, open=1, deferred=1, stale-proof=0（共 36 格有读数）`——**这一遍的判据面就在这里**：两个 `unflipped` 正是 `AC-9\|06`/`AC-9\|07`（探针已读 proven 而台账还写着 `unreviewed`），`open=1` 是 `AC-9\|01` 读出 gap 而台账还没记 gap，`deferred=1` 是时刻面格 `AC-1\|10` 因为 `工作树：6 entry(ies)`（本目录六份已 `git add` 未提交的档案）被让判；同遍 `VERDICT AC-9\|06: proven`、`VERDICT AC-9\|07: proven`、`VERDICT AC-9\|01: gap`。成员 12 `3281 passed, 6 skipped in 103.30s`（C51 是 3278 ⇒ 本轮 +3 = 新增的三条服务层用例）、`TOTAL 11636 1086 2874 288 89.28%`；成员 2 `OK: 517 file(s) walked …（frozen baseline: 3）`；成员 5 `files census = 555 (gate logs: 99)`。**这一遍不是定稿遍**：它跑时台账与验收文档还是回填前的样子，且本 README 的这段文字尚不存在 |
| 遍 2（回填后、干净树） | `make gate` | 正文见同目录 `gate-run2-clean-final.txt`。**本行不引它自己的读数**——它必须跑在含本 README 全文、台账与验收文档回填的**已提交**树上，而描述它的那段文字只能晚于它。要核对就打开档案读末行 `GATE_EXIT=`、成员 11 的 `工作树：干净` 与 `台账↔读数` 那一行（遍 1 的 `unflipped=2, open=1` 在这里应当归零、`agrees` 相应增到 35）；遍 2 的逐字读数由它之后的一次提交追加在本表下面一行 |

便宜的单项复算也照例在花钱跑门禁之前做一遍，它们不替代门禁遍：`python scripts/quality/a2_check.py`（`OK: A2 files meet the full A2 standard`）、`python scripts/quality/acceptance_ledger_check.py`（回填前 `items=130 proven=32 gap=8 unreviewed=90 ticked=32`，回填后 `proven=34 gap=9 unreviewed=87 ticked=34`，两次都 `OK: … reconcile`）、`make brand-check`（`OK: no brand residue and no rename leftovers`）。

描述某一遍的那句话永远晚于那一遍：本表两行连同本 README 都在它们各自的遍之后写成，所以本轮的自指按 hash 披露——遍 1 的档案头 `head_commit` 早于含这段文字的提交，遍 2 的档案头 `head_commit` 早于把这份档案放进仓库的那个提交。要核对就读档案本身：末行 `GATE_EXIT=`、成员 11 的 `工作树：` 与 `agrees=/unflipped=/open=/deferred=/stale-proof=` 一行。

## 七、仪器自身缺陷与复跑披露（不粉饰）

1. **反证仪器第一版在说谎，而且差一点就被当成通过。** 第一版用进程内 `pytest.main()` 跑受影响的节点。M1 之后，被改写的 `opendata.pipeline.dwd_merge` 已经以**改写后的字节码**留在 `sys.modules` 里；M2 那一遍的进程内跑因此量的不是 M2 的产物，而打印了 `--- pytest exit=0（期望非 0）---`（节点"通过"）。同一遍里条目级探针那一腿是子进程跑 pytest 的，它如实读到 `test_service_degrades_per_key_and_keeps_the_source_evidence=exit=1` 与 `权威缺失才降级 = no` ⇒ gap。也就是说：**如果仪器只信自己那条错的读数，档案里就会出现一句「四处都扳红」的假话**。现已改成每一腿 `subprocess.run([sys.executable, "-m", "pytest", …])`（`docs/evidence/C52/falsification-dwd-merge.txt` 的正文就是改后重跑的那一遍，四腿 exit=1），并把这条教训写进了 `run_pytest` 的 docstring。
2. **`--item` 的退出码不是判据读数。** `acceptance_item_probe.main()` 只在 `drift` 时返回非 0，`gap` 仍然返回 0。仪器第一版按"探针 exit≠0 才算扳红"来判，会把四处全判成"没扳红"；现改为解析 `VERDICT AC-N|NN: <state>` 行的状态字。这是本目录仪器对被复用的门禁仪器的一条**读法约束**，不是对它的改动。
3. **`expects` 必须逐字，本轮被绊一次。** 三条新探针最初写的是节选的判据文字，`--item` 立刻报 `drift — the criterion no longer contains '口径映射表存在且覆盖 P0 域（六类口径）'`：`doc.text` 只剥 `- [ ] ` 前缀，`**` 与括号都保留。修的是**探针对判据的引用**，判据原文一字未动。
4. **一次格式回潮被 A2 抓到**：`41ba175` 提交时 `scripts/quality/acceptance_item_probe.py` 的 `payload_keys` 推导式没走 `ruff format`（该轮只跑了 `ruff check`），本轮 `python scripts/quality/a2_check.py` 首次报 `FAIL ruff format --check`，随后 `ruff format` 收敛（1 insertion / 3 deletions，纯排版，读数不变）。

## 八、本轮没有做的事

1. `AC-9|01` 的两半（#66）。
2. `AC-9|08` 仍 `gap`：判据后一半要「表里真的有行」，落 dwd 须经用户确认；`dwd_futures_daily`/`dwd_index_daily`/`dwd_option_daily` 三张表不存在且 `dwd_table_ddl` 生产无调用点。本轮未触碰其判据面。
3. 本轮不引入任何生产数仓写入，也不改 `authority.json`、两份 mapping yaml、`_FIELD_KEYS`、`opendata/pipeline/dwd_merge.py`（四腿反证逐字节还原）。
