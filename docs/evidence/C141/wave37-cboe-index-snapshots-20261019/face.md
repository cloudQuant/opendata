# wave-37 落地面：cboe::IndexSnapshots 从拒绝台账走到平面 1 已验收（336 → 337/350）

一句话：wave-36 的工件请求面把"这 14 行各还差哪一个工件"列成可重测量清单后，本波用一次授权的非鉴权访问拿到
Cboe 官方面板的 `HTTP/2 200` 载荷，据此实现并验收了 `cboe::IndexSnapshots`；平面 1 从 336 走到 **337/350**，
残差从 14 降到 **13**，cboe 的拒绝台账从 2 条降到 1 条。全部读数取自本目录 `runs/` 的载档，行号即该文件行号。

## 1. 仪器与代际对账

判定不手抄：平面 1 走 `scripts/quality/iter2_model_evidence.build_report`（落地面 `_judge_face()`，进程内 import
本仓判定器），捕获归属走 wave-36 那把面自己的 `_endpoint_key` / `_receipt_endpoints`（本面 import 而未重跑它的
`main()`，因为它的载档 `runs/w38_artifact_request_face.txt` 是已提交的，重跑会覆盖），残差桶走 wave-36 拒绝面的
`_census_face()`。

* 落地面 `probes/w40_snapshot_landing_face.py`（397 行，17642 B，sha16 `ab95477128b9e708`）载档
  `runs/w40_snapshot_landing_face.txt`（16 行，2582 B，sha16 `72d0f7dfc021d468`）：
  八臂全绿 `arms=8 arms_passed=8 arms_failed=0 PASS=True` `:14`，出口 `w40_snapshot_landing_face_exit=0` `:16`。
  同目录其余仪器的字节对应产生各代读数的版本：链驱动 `w39_run_chain.py`（234 行，sha16 `57664650c26d1524`）、
  凭证闸门 `w41_staged_credential_scan.py`（224 行，sha16 `bc7ffd9a92a3c044`，它 produce 的是 take2）、
  cboe 单 provider 重签 `w42_cboe_resign.py`（149 行，sha16 `c3b810139a5d4c66`）、
  收口面 `w43_committed_payload_face.py`（389 行，sha16 `4386d8d61b65f636`，它 produce 的是 take3）。
* 代际：身份集 `5168e1e723999312` 与上数代同，`claims_sha256=ee5256aa4108a7b7`，判定报告 sha16 `472c11cef6c0299f`，
  本行 witness 分片 `snapshots.py` sha16 `621f27df74665353`，捕获体 sha16 `9554ad964bcee6ac` `:12`。
  上一代落地面（cboe-only 重跑之前）留档不覆盖：`runs/w40_snapshot_landing_face.take1_superseded.txt`
  （sha16 `a7ae8bc6f9aa972e`，`claims_sha256` 那一代是 `5c6a342c3ec38528`）——两代差在 `historical.py` 的一次
  仅注释换行改写（§6）。

## 2. 新事实一：一行确实因为"拿到工件"而被验收，不是因为我把散文改写

* 逐行判定：`A_judge_row accepted=337 identities=350 claims=1710 row_status=OFFLINE_ACCEPTED
  row_phases=['contract','counterfact','direct','implementation','service'] row_missing=[] row_review=VALID
  row_issues=[] status_counts={'INCOMPLETE':5,'OFFLINE_ACCEPTED':337,'UNREVIEWED':8}` `:6`。
  这一行自己五相齐全、主审 VALID、issues 空——不是我宣布它落地，是判定器逐行给出的状态。
* 拒绝台账：`B_refusal_ledger live_count=1 live_names=['FuturesCurve'] IndexSnapshots_absent=True
  injection_moves_count=True forged_count=2` `:7`。臂判据要求内存注入能把计数从 1 推到 2，所以这条读数不是
  我把 `NOT_DECLARABLE` 读空的结果，是"计数可动"之下的 1。
  被删掉的那条理由原文是 "No official endpoint/response mapping for the fixed regional snapshot identity
  was evidenced in this slice."，本波把它连同它的前提一起撤回，撤回留痕写进
  `opendata/data/providers/cboe/specs.py` 的模块 docstring（不是只写在本文）。
* 捕获归属：`C_capture_backing endpoint_key=cdn.cboe.com|api index_named_by=<abs>/docs/evidence/C140/wave37-unauthenticated-access-20261019/capture-receipt.json
  index_size=13 receipt_url_exact_matches=1 receipt_http=['200'] status_line_chain=['HTTP/2 307 ','HTTP/2 200 ']
  body_digest_and_path_match=[True] body_bytes=358262 same_host_coarser_key=cdn.cboe.com|api
  same_host_also_present=True foreign_control_absent=True` `:8`。三条要分开读：
  (a) **文档级**证据是 receipt 自述的 `url` 字面等于 fetcher 的 `SNAPSHOT_URL`、`http=200`、`body_sha256` 与磁盘字节
  一致且 `body_file` 解析到同一档；
  (b) wave-36 那把面的 `_endpoint_key` 只到 `host|首段`，所以它对本行与"同 host 同前缀的另一份文档"**给的是同一个键**
  （读数里 `same_host_also_present=True`），这一层的"有捕获"不能当文档级证据用；
  (c) `foreign_control_absent=True` 是那条必须能不同的臂——同一谓词对一个从未捕获的 host 返回缺失。
  `index_size=13` 等于 wave-36 取到工件之后那一代（`docs/evidence/C139/wave34-tmxyfinance-rest-20261018/runs/
  w38_artifact_request_face.take2_after_access.txt:22`），本波的落地步骤**没有**新增任何捕获：捕获是 C140
  那一代记的，本波只是让这一行从"有捕获但无实现"走到"有捕获、有实现、有验收"。

## 3. 新事实二：这一行的实现面读数（16 列、3 列永不填、caret 与 0 的语义）

* 契约对照：`E_contract_comparator reference_own=12 reference_base=12 reference_union=16 landed_columns=16
  landed_minus_reference=[] reference_minus_landed=[] never_filled_by_fetcher=['currency','name','status']` `:10`。
  两列集合是分别从参考 checkout 的两个 AST 里读出的注解名（`IndexSnapshotsData` 12 个 +
  `CboeIndexSnapshotsData` 自身 12 个，并集 16），落地模型 `CboeIndexSnapshots.model_fields` 恰好 16 个、
  两个方向差集都为空；`name/currency/status` 是官方面板这份文档里**没有对应单元格**的三列，
  读数直接取 fetcher 产出的行键集合的补集，所以"永不填"是被测出来的，不是我在 docstring 里声明的。
* 数值守卫两面：`D_guard_two_sided clean_rows=810 tampered_refused_code=CBOE_INDEX_SNAPSHOTS_BAD_NUMERIC_FIELD
  selected_rows=1 selected_symbol=^VIX` `:9`。同一 `_parse_panel`：未篡改的官方载荷读出 810 行（这是分母），
  把 `current_price` 改成 `True` 之后按码拒绝。零→null 与百分数÷100 的规则形状来自参考实现 `df.replace(0, None)`
  与 `round(x/100, 6)`；本波把它们改成**先归一化再判零**，于是 `price_change_percent` 的 0 也读成 null
  （实测 810 行里 33 行的该列为 null，含 31 个源端 0）。caret：面板里 810 个 symbol **全部**带 `^` 前缀，
  所以 `symbol="VIX"` 映射到 `^VIX`（`selected_symbol=^VIX`），而 `["VIX","^VIX"]` 这种重复指认在
  query 层就拒（`symbol list names one panel index twice`）。
* 相收据：`F_phase_receipts present=5 of=5 readings={五个都是 'exit=0 status=PASS'} control_row_receipts=0
  refusal_class=CboeIndexSnapshotsError refusal_code=CBOE_INDEX_SNAPSHOTS_BAD_NUMERIC_FIELD
  expected_equals_observed=True control_note_on_disk=True` `:11`。`control_row_receipts=0` 是同一 glob 施加在
  未落地行 `cboe::FuturesCurve` 上的对照——收据臂能数出 5 也能数出 0。反例对象是从磁盘上的 counterfact 收据里
  读出的 `expected == observed`，不是我复述探针 stdout。

## 4. 全树重签链：先证明"树是旧的"，再把它签新

`probes/w39_run_chain.py` 的 `runs/chain_summary.txt`（95 行，sha16 `dfecd0780f2a3620`）：

* 反证在前：`pre_chain_stale_reading={'pre_chain_identities':350,'pre_chain_accepted':0,'pre_chain_claims':1705,
  'pre_chain_status_counts':{'INCOMPLETE':5,'STALE':336,'UNREVIEWED':9}}` `:93`，载档
  `runs/judge_pre_chain.txt` 尾行 `350 identities, 0 offline accepted`，`chain_judge_pre_chain_exit=0`。
  也就是说：改了 `domains.yaml` 而没有重签，判定器把 336 行全读成 STALE——"这次改动要不要全树重签"不是我的判断，
  是这一代读数。
* 签后：`post_chain_reading={'post_chain_identities':350,'post_chain_accepted':337,'post_chain_claims':1710,
  'status_counts':{'INCOMPLETE':5,'OFFLINE_ACCEPTED':337,'UNREVIEWED':8}}` `:93`。claims 1705 → 1710（+5 = 新行五相），
  UNREVIEWED 9 → 8（正是这一行从"有声明无主审"变成"已验收"），accepted 336 → 337。
* 28 个 provider 的 producer 与 recorder 全部 exit=0，`nonzero_members=[]`，`w37_chain_exit=0` `:94-95`；
  merge 两次的稳定载荷同值 `merged_stable_sha_take1=merged_stable_sha_take2=6e2bb016e042b6a0`，
  `merge_idempotent_stable_payload=True` `:93`。
* producer 自己给的 denominators（`runs/producer_cboe.txt:64`）：
  `inventory total=350 (pinned identity set 5168e1e7…); catalog-resolvable bound identities this wave=337/350;
  provider rows covered=11 of provider inventory rows=11, of which bound now=10/337 resolvable`。
  cboe 的 11 行里绑定数是 10——唯一没绑的是 `FuturesCurve`，与 §2 的 `live_count=1` 同代同数。

## 5. cboe-only 重跑的判据（为什么不必再走一次 36 分钟全树）

`runs/chain_summary.take2.txt:32` 记 `pin_owners={'cboe':50,'total_claims':1710}`：两份 cboe witness 分片
（`_parts/cboe/historical.py`、`_parts/cboe/snapshots.py`）在合并后的 claims 里被 **50 条 claim** 引用，全部
`provider=cboe`。这正是 `iter2_claims_fixtures` 声明的 provider-atomic 归属：别的 provider 不读这两档字节。
于是 `probes/w42_cboe_resign.py`（import 本波链驱动器、复用它的 `run()` 与 `report_face()`，所以每个载档仍自带
argv 与出口）只重跑 producer_cboe → recorder_cboe → merge×2 → judge：五个成员 exit 全 0、
`merged_stable_sha_take2_first=79610b71e7149bd8 == merged_stable_sha_take2_second`、
`post_resign_reading` 仍是 350/337/1710。落地面随后重跑为 take2，`claims_sha256` 由 `5c6a342c3ec38528` 走到
`ee5256aa4108a7b7`（§1），两代载档都在。

## 6. 静态平面：take1 抓到的是我自己继承来的债

`runs/a2_static_plane_slice_w37_payload.take1.txt`（133 行，sha16 `cda9c06cdfc80706`，
`A2 files (complete resolver): 12` `:4`，其下列出 12 条候选 `:5-16`，`real 230.43`）：四个工具成员里
`ok ruff format --check` `:86`、
`ok mypy (10 unique target(s))` `:87`、`ok bandit` `:129` 全绿，两条红是
`FAIL pinned Gitleaks file scan` `:20`（25 档命中，路径全在 `docs/evidence/C100/**/inputs/pristine/akshare/*.py`）
与 **`FAIL ruff check` `:71`**：
`E501 Line too long (110 > 100) --> scripts/quality/iter2_witnesses/_parts/cboe/historical.py:18:101`。
那一行既存于已提交的 wave-8 分片里（`contract_model('cboe_index_historical') -> …CboeIndexHistorical`。那条引用），
我本波只改了它下面的 docstring 段落——但 A2 的规则是"碰它就要达标"，所以它归我修。
修法是**仅换行、不改引用文本**；改完这档是 pinned 输入，于是走 §5 的 cboe-only 重签。

take2 `runs/a2_static_plane_slice_w37_payload.take2.txt`（126 行，sha16 `94363d8fc4eb6ff8`，
`A2 files (complete resolver): 13` `:4`，13 = 12 + 新驱动器 `w42_cboe_resign.py`）：
`ok ruff check` `:72`、`ok ruff format --check` `:73`、`ok mypy (11 unique target(s))` `:74`、`ok bandit` `:122`
四成员全绿，唯一红仍是既存 gitleaks `:21` 与 `FAIL: A2 gate failed on: immutable evidence secret scan` `:120`，
`real 231.14` `:123`，`a2_take2_exit=1` `:126`。C141 在这份载档里只出现在候选清单与逐 target 读数两处，
都不是命中。

## 7. 测试平面：既存红、本波重钉、以及"控制树不算控制"的 9 档

`runs/test_plane_w37_payload.txt`（98 行，sha16 `c847f104d4da5e30`）是两面控制：本树跑同样九个文件得
**7 红 / 1636 通过**（`test_plane_w37_exit=1`），detach 到 HEAD 的控制树得 16 红 / 1623 通过
（`control_pytest_exit=1`）。两个集合的关系按 node id 逐档对账：

* 7 档在两树**同名同态**，即本波之前就在红：`test_provider_catalog.py::test_catalog_contains_34_sources_and_only_66_fetcher_bindings`
  （钉 `len(bindings) == 342`；两个计数都是实测的，用 `--tb=line` 单独跑该 node id 得到
  控制树 `assert 369 == 342`、本树 `assert 370 == 342`，差 1 正是本波新增的那一条 binding）、
  `::test_registration_and_legacy_fetchers_project_the_same_capabilities`
  （按 provider 名钉手写数量，报错点 `oecd`）、`test_provider_domains.py` 的四档（同一把
  `tests/fixtures/registry_batch5_snapshot.json` 对 `opendata/data/domains.yaml` 的**字节 pin**，
  `assert … == expected_hash, relative_path` 在 `:1368`；该 fixture 的 `snapshot_status` 自述
  `MANUAL_EXPECTED_FIXTURE_NOT_ACCEPTANCE`）、以及 `test_congress_cboe_iter2.py::…[CongressAmendments]`。
  本波不改它们：把 342 推到实测值要把 28 个前波的计数差逐档归因（本波只解释得了其中 1 个），
  改字节 pin 等于把历史快照当成活的契约，两者都不是"落地一行"该顺手做的事。
* 本波自己的注册使 3 档枚举变宽，已重钉：`tests/test_cboe_engine_provider.py` 的 `fetcher_bindings == 9 → 10`
  与九域字面量补 `cboe_index_snapshots`、`tests/test_congress_cboe_iter2_cboe.py` 的十行集合补
  `IndexSnapshots` 并把 `NOT_DECLARABLE` 从 2 改 1，而 `len(fetcher_dict)+len(NOT_DECLARABLE)==11` 这个
  固定和**不变**（10+1）。这三档在本树绿。
* 控制树多出的 9 档全在 `tests/test_cboe_index_historical_public.py`，成因是那九档读
  `docs/evidence/C100/congress-cboe/captures/vix-history-20261012.csv` 与两份 C100 JSON（该测试档 `:33-39`），
  而 C100 自 C113 起被 gitignore，fresh worktree 里根本没有这些字节。**所以 HEAD worktree 不是这 9 档的字节控制**，
  它们既不算既存红、也不算本波修复——载档末尾把这句写死，以免下一个读者把 7 与 16 之差读成"修了 9 个"。

## 8. 残差 13 行（逐行 action，来自出货 census 自己的 row 行）

`H_census_bucket row=cboe::IndexSnapshots action=ABSENT_FROM_CENSUS_ROWS census_exit=1
detail=residue=13 actions={'implement_missing_model':1,'ledger_ruled_out':10,'new_provider_package':2}
red_arms=['B_landed_negative']` `:13`。本行**不再出现在残差行集里**，这是它离开残差的独立读数；
census 自身仍因 wave-28 那个被消耗掉的负面对照在 arm C 之后终止（`census_exit=1`，`B_landed_negative`），
本波不改判定器。按同一 instrument 的 `row … action=` 行逐档列出 13 行：

| 行 | action |
| --- | --- |
| biztoc::WorldNews | new_provider_package |
| cboe::FuturesCurve | ledger_ruled_out |
| econdb::CountryProfile | ledger_ruled_out |
| econdb::ExportDestinations | ledger_ruled_out |
| intrinio::OptionsSnapshots | ledger_ruled_out |
| seeking_alpha::CalendarEarnings | ledger_ruled_out |
| seeking_alpha::ForwardEpsEstimates | ledger_ruled_out |
| seeking_alpha::ForwardSalesEstimates | ledger_ruled_out |
| stockgrid::ShortVolume | new_provider_package |
| wsj::ETFActive | ledger_ruled_out |
| wsj::ETFGainers | ledger_ruled_out |
| wsj::ETFLosers | ledger_ruled_out |
| yfinance::AvailableIndices | implement_missing_model |

`ledger_ruled_out` 由 11 降到 10 的唯一原因就是我删掉了 cboe 拒绝台账里的 `IndexSnapshots` 那一条（§2 的
`live_count=1`）。这 10 行现在分三档：

* **已经有 200 载荷但缺 provider 包**：`seeking_alpha::CalendarEarnings`（C140 已捕获
  `earnings_calendar/tickers` 的 200 与两次参数链 400）。落地它需要新建 `seeking_alpha` 包 + `catalog.py`
  注册（树级前提）+ 新 domains 键，代价与本波同量级。另两行 `ForwardEpsEstimates`/`ForwardSalesEstimates`
  的捕获是 `symbol_data/estimates` 的 200 与 `relative_periods=12` 的 400 链——参数形状还没答完。
* **仍无端点级证据**：三条 wsj（参考 checkout 里连模型模块都解析不到，wave-36 §13.2 的 `B_upstream_resolved`
  读数就是它仨）、`cboe::FuturesCurve`、`econdb` 两行、`intrinio::OptionsSnapshots`。
* **不是本臂能测的**：`biztoc::WorldNews`、`stockgrid::ShortVolume`（`new_provider_package`，等 rights 注册），
  `yfinance::AvailableIndices`（`implement_missing_model`，等"参考输入"机制）。

## 9. 撤回与更正留痕

1. **一次"检查失败"被我读成"检查过了"**：第一次写 `runs/test_plane_w37_payload.txt` 时我用
   `pytest … | tail -22; echo "test_plane_exit=$?"`，`$?` 是 `tail` 的 0，载档会写下
   `test_plane_exit=0` 而 pytest 真值是 1。这不是笔误级的问题——它正是本仓记忆里"退出码穿过管道"那一型。
   处置：载档重写为分别捕获两树的真退出（`worktree_pytest_exit=1`、`control_pytest_exit=1`），
   并且把控制树也重跑成同样的九个文件（先前我只跑了五个文件，得 7 红，那次对 9 文件集合是无据的）。
2. **`_receipt_endpoints` 的"空目录对照"是我猜出来的假对照**：我一度写 `empty_index` 作为"臂能不同"的证据，
   但那个函数**总是** walk `docs/evidence` 再追加参数，所以传空列表不会得到空索引——这条臂永远"能不同"，
   因而什么都没测。改成两个真读数：同一 host 同前缀的另一个键**也在**（暴露 `_endpoint_key` 的粒度不足以做
   文档级断言，于是文档级断言换到 receipt 的 `url` 字面 + `body_sha256` + 路径同一），以及一个未捕获 host 的
   键**不在**。
3. **第一版落地面把状态读成 `model` 键**：判定器 `ModelResult` 用的是 `upstream_model` / `phases` /
   `primary_review`，不是 `model` / `claims` / `reviews`。写错时 A 臂直接红在 `row_status=None` 上——
   是我的键集合错，不是判定器错；现在臂判据要求 `row_phases` 恰为五相的排序集合且 `row_missing == []`。
4. **`first line of .headers` 是错的读法**：官方那次请求是 307 → 200，一档 `.headers` 里装着整条链，
   取首行会读出 `HTTP/2 307`。臂改成"链上全部状态行 + 终态必须是 200"，读数把两行都打出来
   （`status_line_chain=['HTTP/2 307 ','HTTP/2 200 ']`）。
5. **收据字段名我猜过一次**：`F` 臂最初读 `receipt['exit_code']` 与 `receipt['verdict']`，实际形状是
   `machine_execution.exit_code` 加顶层 `status`，而反例在 `counterfact.expected/observed` 里；
   改成按被试自述字段读，并把 `expected == observed` 与 "untampered control produced" 那句话的存在性写进臂。
6. **caret 归一化不是我的发明**：`symbol="VIX"` 之所以映射成 `^VIX`，是因为捕获文档里 810 个 symbol
   全部带前缀（该计数在检查脚本里逐次打出来）；我没有让 `^VIX` 之外的拼法成立，`["VIX","^VIX"]` 在 query 层
   作为同一面板行的重复指认被拒。
7. **索引条目数的两代我一开始混成了一句**：正确对照是
   `w38_artifact_request_face.txt:22` 的 `receipt_endpoint_keys=9`（未访问那一代）与
   `w38_artifact_request_face.take2_after_access.txt:22` 的 `13`（拿到工件那一代）——+4 个
   `host|首段` 键；而本面读到的 13 与后一代同值，因为本波没再发请求。
   把"行数 1 → 5"与"键数 9 → 13"写成同一件事会把两个不同测量混成一句话，尤其 `ForwardEps/ForwardSales`
   两行共用同一个 `seekingalpha.com|api` 键。

## 10. 预测判据（下一面必须读到）

* **若下一波落 `seeking_alpha::CalendarEarnings`**（新建包 + `catalog.py` 注册 + 新 domains 键）：
  平面 1 从 337 起按行数增；落地面必须读到 `A_judge_row accepted>337`、
  `H_census_bucket` 里 `seeking_alpha::CalendarEarnings` 不再是残差行、
  `C_capture_backing` 对该端点 `receipt_url_exact_matches=1` 且终态 `HTTP/2 200`（我已有的那份 200 载荷可以直接用），
  并且 `runs/headers_census` 那一代要出现 `providers_with_captures` 含 `seeking_alpha`。
  **失效条件**：新捕获只是 `docs.openbb.co` 或 `server: GitHub.com` 的合同页——那读成 `model_contract_page`，
  等于没回答"这个源能被访问"。
* **若 owner 把某行改判"接受"而不需要代码**：平面 1 按行数增，但 `domains.yaml` 不动时不必全树重签——
  本波读到的成本边界是：**只有 pinned 输入变了才欠重签**（§5 的 50/1710 归属读数就是这条判据的仪器）。
* **若下一波把 342 那个钉推正**：`test_provider_catalog.py:170` 的 `assert len(bindings) == 342` 需要 +28 的
  逐档归因（本树实测 370，控制树实测 369，本波只解释得了其中 1 档）；只把常量抬到 370 是让判定器闭嘴，
  不是测量。
* **若本行的捕获被替换成更新的一份**：`capture_sha256` 与 `body_digest_and_path_match` 会同时移动；
  只动其一说明我引的字节与 receipt 说的字节不是同一份，那必须读成代际脱节而不是"刷新了一次捕获"。

## 11. 收口面：对已提交字节的三条读数，以及它第一代的红

`probes/w43_committed_payload_face.py` 对落地下提交 `b2e7421e7`（父 `a4a4ce2ad3b73c52`）做三件事，
每一代的载档都留着：

* **逐档字节对账**（take3 载档 `runs/w43_committed_payload_face.take3.txt:5`）：
  `reconcile commit_paths=2252 worktree_drift_vs_commit=0 moved_vs_parent=2252 missing_in_commit=0`，
  分档 `docs/evidence/C101: 2144, docs/evidence/C141: 83, docs/evidence/C140: 13, opendata: 6,
  docs/evidence/C139: 2, scripts: 2, tests: 2` `:7`。
  "提交带的就是我读到的字节"这条断言的**反证孪臂**是同谓词对父提交读出的 `moved_vs_parent=2252` —— 一个
  只能读到 0 的比较器什么也没测。
* **git 模式扫这一条提交**（`:12`）：`gitleaks_git_mode commit=b2e7421e7 rc=0 findings=0`，
  植入对照 `planted_control rc=0 findings=2 arm_can_move=True`（对照体在 `/tmp` 里的一个 scratch 仓库，
  用同样的 argv 与同样的 `--log-opts=-1`，因为 gitleaks 从工作目录解析仓库）。
  臂 `head_is_landing_commit` 要求 `git rev-parse HEAD` 就是这条提交，否则 `-1` 扫的是别的。
* **dir 模式只扫提交带的那 96 档 bundle 字节**（`:13-14`）：
  `dir_mode_tree copied_from_commit=96 local_only_files=6 …`、
  `gitleaks_dir_mode bundles rc=0 findings=0 planted findings=2 arm_can_move=True`；
  出货闸门 `secret_scan_check rc=0 … 452 commits scanned … no leaks found` `:15`。
  终读 `arms=8 arms_passed=8 PASS=True` `:18`，出口 `:20`。

第一代与第二代各有一条红，两条都记下来而不是删掉载档：

1. take1（`runs/w43_committed_payload_face.txt:13,17,19`，`arms=7 arms_passed=6`）红在
   `dir_mode_clean_and_armed`：`findings=2 rules=['github-pat','github-pat']`。
   定位（redact 后按路径报）落在
   `docs/evidence/C141/…/probes/__pycache__/{w41_staged_credential_scan,w43_committed_payload_face}.cpython-313.pyc`。
   原因是我自己造成的一个真实风险，而且形状值得记：**CPython 把 `"ghp_" + ("0123456789" * 3 + "abcdefghij")`
   在编译期折叠成常量**，所以我为了武装闸门而写的"运行时拼合、永不打印"的假凭证，被 `py_compile` 以字面量
   形式写进了探针旁边的 `.pyc`。`.gitignore` 第 2 行 `__pycache__/` 让它进不了提交
   （实测 `git show --name-only b2e7421e7 | grep -c __pycache__ = 0`），git 模式因而读 0；
   而 dir 模式扫的是**工作树副本**，于是读到 2。教训的形状：载荷扫描的树必须由提交构造，不能 copy 目录。
2. take2（`runs/w43_committed_payload_face.take2.txt:18`，`arms=8 arms_passed=7`，出口 `:20`）把 dir 树改成由
   `git ls-tree <sha>` 构造后，扫描本身读 0，但红在**我新加的一条簿记臂**
   `local_only_bytecode_named`——它要求"跳过的本地文件里还有 `.pyc`"，而我在跑 take2 之前已经把这些缓存删了。
   也就是说这条臂在拿一个**我自己刚改过的状态**当判据。take3 把它改成断言"扫描树比工作树窄、且差集被命名"
   （`local_only_files_named`，实测 `local_only_files=6`，样本打进 `:13`），`.pyc` 是否还在盘上只作为
   一个读数打印（`bytecode_still_present=0 (take 1 read 2 here)`），不再当判据。

## 12. 撤回清单补两条（同一型错误的第二次与第三次出现）

8. **`local_only_bytecode_named` 是一条会随我自己的清理动作失效的臂**（§11 第 2 点）。写臂时问一句
   "这条臂的前提会不会在我记录证据的过程中消失"——会的话，把它降级成读数，判据只留可复算的关系。
   这与 §13.4 第 5 条（被消耗掉的负面对照把整个 census 拖红）是同一型：判据依赖一个已经不存在的事件。
9. **"永不打印假凭证"不等于"假凭证不落盘"**：折叠后的常量进了 `.pyc`。以后任何武装扫描的探针都要
   (a) 用 `PYTHONDONTWRITEBYTECODE=1` 跑（本波 take3 起就是这么执行的），
   (b) 扫描树从提交构造，而不是从目录复制。
