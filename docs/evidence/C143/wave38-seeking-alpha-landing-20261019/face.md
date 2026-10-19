# wave38：seeking_alpha::CalendarEarnings 落地面（平面 1 337 → 338/350，残差 13 → 12）

判定探针与反例都在本目录 `probes/`，读数在 `runs/`；本文每个数字都指回一份 carrier，没有一个数字是手打的结论。

## 1. 结论先行

- 平面 1（离线验收）：`OFFLINE_ACCEPTED` **337 → 338**，`identity_count` 仍是 **350**，身份集投影
  `5168e1e723999312…` 未变（`runs/w48_calendar_landing_face.txt` 的 `J_generation` 臂）。
- claim 总数 **1710 → 1725**（+15 = 3 行 × 5 相位）：`CalendarEarnings` 新增 5 条 PASS，
  `ForwardEpsEstimates`/`ForwardSalesEstimates` 各新增 5 条 BLOCKED。
- 判定桶位移是**可指名的**，不是总数巧合（`docs/evidence/C101/judge/claims.merged.report.json` 与
  `runs/judge_pre_chain.report.json` 两份报告逐行对比）：
  - `INCOMPLETE` 5 → 7，进入者恰好是 `seeking_alpha::ForwardEpsEstimates`、`seeking_alpha::ForwardSalesEstimates`，无人离开；
  - `UNREVIEWED` 8 → 5，离开者恰好是三行 seeking_alpha：`CalendarEarnings`（被验收）与上面两行（转入 INCOMPLETE）；
  - `STALE` 337 → 0、`OFFLINE_ACCEPTED` 0 → 338 是整树重签本身，不是 337 行新验收。
- 拒绝面没有被藏起来：两行 estimates 的 15 条 BLOCKED claim 与全树 35 条 BLOCKED 同形状
  （`receipt.path` 为空、`exit_code=3`、reason 逐字等于 `no offline witness configured for this model row`），
  判定器对它们报的是 `claim_receipt_path_invalid, claim_status_blocked, command_exit_nonzero, …_pins_missing`
  这一族既有拒绝码，不是本波新造的形状。

## 2. 捕获面：路由协议是被读出来的，不是被假设的

`docs/evidence/C142/wave38-seeking-alpha-calendar-20261019/`，判定探针 `probes/w44_capture_receipt.py`，
读数 `runs/w44_capture_receipt.txt`（`captures=5 of=5 arms=5 arms_passed=5 failed=[]`，
`receipt_sha256=03658660ac5f3ec4`，`w44_capture_receipt_exit=0`）。

| 捕获 | HTTP | 字节 | 信封 |
|---|---|---|---|
| `sa-calendar-second-date`（`filter[selected_date]=2026-10-20`） | 200 | 12389 | 50 条，`meta.size=50`，`total_count=56`，`total_pages=2`，`links.next` 非空 |
| `sa-calendar-page2-api-form`（同上 `page[number]=2`） | 200 | 1645 | 6 条，`meta.size=6`，`links.next=null` |
| `sa-calendar-full-params`（加 `with_rating=false&currency=USD`） | 200 | 6832 | 27 条，`total_count=27`，`total_pages=1` |
| `sa-calendar-page2-full-params` | 200 | 462 | 1 条，`total_count=51` |
| `links.next` 的字面量 URL（`/earnings_calendar/tickers?...`） | **404** | 6654 | 非 JSON |

三条被这条面固定下来的协议事实，直接决定了实现：

1. **分页闭合是被数的**：50 + 6 = 56 = `meta.total_count`（`pagination_closure … closes=True`）。
2. **`links.next` 不可跟随**：它给的是站点根下的 HTML 路径，实测 404，所以游标只能用
   `page[number]`，实现里因此按 `meta.total_pages` 与页码走，而不是按 `links.next` 走。
3. **货币过滤会静默删行**：`filter[currency]=USD` 把 `total_count` 从 56 降到 51，第二页从 6 条降到 1 条
   （`currency_filter_narrowing … narrows=True`）。本波**不发** `filter[currency]` 与 `filter[with_rating]`，
   因为发了就少行，而少行在离线判定里不可见。
4. 授权边界：这五个请求都是公开未认证的 GET，没有 `Authorization`、没有 API key、没有 Cookie
   （`capture-receipt.json` 的 `authorization_boundary`）。

## 3. 实现面

新增：`opendata/data/models/seeking_alpha.py`（契约模型，9 列）、
`opendata/data/providers/seeking_alpha/`（`_source.py`、`provider.py`、`registration.py`、
`models/__init__.py`、`models/calendar_earnings_public.py`），
改动：`opendata/data/models/__init__.py`（导出）、`opendata/data/providers/catalog.py`
（`seeking_alpha` 从 `_reserved_provider` 变为绑定 provider，实现中来源 30 → 31 of 34）、
`opendata/data/domains.yaml`（新键 `seeking_alpha_calendar_earnings`，`temporal_kind=event`，
`natural_key=[symbol, report_date]`，`storage_mode=transient`）。

护栏与上限都在同一份 adapter 里，且每一条都有反例（`w48` 的 `D_guard_two_sided` 臂）：

- 上限：单响应 4 MiB、窗口 ≤ 8 天、每日 ≤ 25 页、超时 30s；
- 逐列拒绝：布尔进文本列 → `SEEKING_ALPHA_CALENDAR_BAD_TEXT_FIELD`，非数 → `…_BAD_NUMERIC_FIELD`，
  非整 → `…_BAD_INTEGER_FIELD`；
- 信封拒绝：`BAD_ENVELOPE`、`SIZE_MISMATCH`（`meta.size ≠ len(data)`）、`BAD_PAGINATION`
  （声明还有下一页却没有游标——这就是"静默截断"的失败形状）；
- 行拒绝：`BAD_ROW_TYPE`、`BAD_ATTRIBUTES`、`…_DATE_MISMATCH`（单元格的日期部分必须等于请求的那一天）；
- 双面反例（内存里伪造，不落盘）：`slug=True` 被拒、`links.next=null` 被拒、`meta.size=49` 被拒，
  而干净的两页 50 + 6 照常通过 —— 三条伪造全部被拒、干净面全部通过，才算这条臂成立。

`F_contract_comparator` 臂：本仓 9 列与 OpenBB 参考并集（standard 5 + provider 自有 4）**双向相等**
（`landed_minus_reference=[]`、`reference_minus_landed=[]`）。并集里 `eps_consensus`、`eps_previous`
两列这条路由**从不填值**，该事实是打印出来的读数（`never_filled_by_adapter=[…]`），列在模型里可空，
不假装它们有来源。

## 4. 见证与接缝

`scripts/quality/iter2_witnesses/seeking_alpha.py`：`shared_http_client` 接缝，fixture 用捕获里逐字的
两页 JSON 组成 `sequence`（第一页 `meta.size=3/total_count=4/total_pages=2` 且 `links.next` 非空，
第二页 `size=1` 且 `links.next=null`），query 为 `start_date=end_date=2026-10-20`，
tamper 把第一页第一条的 `attributes.slug` 置为 `True`，期望拒绝码 `SEEKING_ALPHA_CALENDAR_BAD_TEXT_FIELD`。

`E_pagination_follows_envelope` 臂（`w48`）：用同一份假客户端走两页得 56 行 / 2 次请求，第二次请求带
`page[number]=2` 而第一次不带；把信封改标成 `total_pages=1` 后只剩 50 行 / 1 次请求 ——
即行数由信封游标决定，不是实现里写死的常数。

`G_phase_receipts` 臂：五行见证的 claim 全 `PASS`，且每条 claim 引用的收据**字节**复核通过
（`receipt_bytes=True`、`machine_execution.exit_code=0` 五个相位）；同模型的两行拒绝 claim 全 `BLOCKED`、
`receipt.path` 全空。这一臂原本要求拒绝行也有一份 `counterfact.json`，那是错的：全树 35 条 BLOCKED
都不带收据，拒绝的记录形状就是 claim 本身，于是改成读 claim 的 (行数, 状态集, 收据路径集, reason) 四元组。

## 5. 落地面 `w48_calendar_landing_face.py`：10 臂全绿

`runs/w48_calendar_landing_face.txt`：`arms=10 arms_passed=10 failed=[] PASS=True`，
`w48_calendar_landing_face_exit=0`。逐臂读数：`A_judge_row`（判定器进程内复用，338/350/1725，
本行 `OFFLINE_ACCEPTED`、5 相位齐、`row_issues=[]`）、`B_refusal_partition`（bound ∩ refused = ∅、
bound ∪ refused = 清单 3 行、内存注入一行会让分区失真）、`C_capture_backing`（两次 200 + 字面 next 404、
body 摘要与收据一致）、`D_guard_two_sided`、`E_pagination_follows_envelope`、`F_contract_comparator`、
`G_phase_receipts`、`H_census_bucket`（残差 12，本行已从残差 census 消失）、`I_authority_partition`
（复用仓内自己的 authority 谓词）、`J_generation`（身份集/claims/见证/捕获四摘要）。

两处读数是**被修过的仪器**的结果，不是第一次跑出来的：`B_refusal_partition` 起初用 `row["model"]` 读清单，
而清单的列名是 `upstream_model`（`KeyError` 直接杀掉整个面，因此这条面一度不存在）；`C_capture_backing`
起初按 `adapter.API_URL` 裸串去 `source_captures[]` 里找第一页，而收据是用**带查询串的完整 URL** 做键，
找不到就静默退化成 `b""`，于是 `_parse_page` 报 `RESPONSE_TOO_LARGE` —— 那是"空字节"的读数，
不是响应的读数。现在缺失捕获会抛错而不是回退空字节。

## 6. 重签链：半径是被数的，不是被假设的

`probes/w45_run_chain.py` 复用 wave37 已出货的链驱动 `docs/evidence/C141/…/probes/w39_run_chain.py`，
只把落点改到本 bundle。`runs/chain_summary.txt` 的头部是本次驱动的真实 argv 与半径读数：

- 半径：`catalog.py` 被 **1685** 条 claim 以 `source` 组钉住、覆盖 **28** 个有见证的 provider；
  `domains.yaml` 被每个已落地行的 `contract` 组钉住 —— 因此重签半径是**整棵树**，
  驱动跑完 29 个 provider 的 producer 与 recorder，58 个成员 `exit=0`，`nonzero_members=[]`。
- 反例（链前）：判定器读**已提交**的 claim 对着**已改**的树，得 `accepted=0`、`STALE=337`、
  `claims=1710` —— "这些改动需要重签"是被打印出来的，不是被断言的。
- 链后：`accepted=338`、`claims=1725`、桶位移见 §1。
- 合并幂等：`merged_stable_sha_take1 == take2 == 380d20a9bacf5354`，`merge_idempotent_stable_payload=True`，
  且 take1 前的稳定摘要 `79610b71e7149bd8`（wave37 的载荷）被记录为差值。

## 7. batch5 围栏重钉：wave37 的误判在此作废

`probes/w49_batch5_rebase.py`，读数 `runs/w49_batch5_rebase.txt`：`arms=11 arms_passed=11 failed=[] PASS=True`。

**作废声明**：wave37 的面把 4 条 `tests/test_provider_domains.py` 的红判成"两棵树一样红 = 既有失败"。
那句判定在**计数**上成立、在**原因**上错误：对照 worktree 缺 `docs/evidence/C100/**`（被 gitignore），
那 4 条测试在对照树里因**另一个**原因红。真因是 `tests/fixtures/registry_batch5_snapshot.json`
对 `opendata/data/domains.yaml` 的**字节 pin** 被 wave37 移动却没有重钉。本波把围栏重钉之后，
这四个文件一起转绿（§9），这才是那条判定被推翻的方式。

**为什么范围比"一个 fixture"大**：这份围栏被 4 个测试文件读，而且它们把它跟**活注册表**对比 ——
`tests/test_provider_domains.py`（共用 helper `_batch5_identity_snapshot`）、
`tests/test_pipeline_jobs.py::…test_the_registered_census_reaches_the_ods_layer`、
`tests/test_alert_matrix.py`、`tests/test_alert_matrix_model_domains.py`。

仪器不自己重算注册表：它先把 fixture 恢复成 `git show HEAD:` 的字节，再跑仓内**已出货的两个驱动**
`docs/evidence/C101/census-rebase/probes/rebase_batch5_census.py` 与
`rebase_descriptor_census.py`（carrier 里 `driver_exit … =0` 各一行），然后：

- 历史块按 HEAD 字节还原：`iter2_census_rebase`（测试要从
  `registry_census_rebase.approved_delta_conflict` 读数，而第一个驱动会重写这个块）；
  `candidate_counts` 同理还原，**只**重钉其中被拿去跟活的 `models.__all__` 比对的三个导出计数
  （`all_symbols 347→349`、`contract_model_classes_including_base 343→345`、
  `non_base_contract_model_classes 342→344`，`delta_vs_adjudication 17→19`），
  其余 `domain_specs/fixed_bound/fixed_identities/provider_model_legs` 保持 wave33/wave34 留下的冻结值
  （carrier：`candidate_counts_keys_the_driver_moved=4 […]`）。
- 字节 pin：31 条逐条重算，**2** 条变了（`domains.yaml c4005525174644ed→015edd5ca6a409f4`、
  `models/__init__.py 2e7c67651ebab942→ea5071c658d8032d`），29 条从盘上重算且相等。
- 队列位移全部可指名：`domains 356→358`、`qsem 336→338`、`legs 369→371`、`identities 369→371`，
  加入者恰是两行的身份 `(cboe, IndexSnapshots, cboe_index_snapshots)` 与
  `(seeking_alpha, CalendarEarnings, seeking_alpha_calendar_earnings)`，`fields_moved=0`、移除 0。
- 冻结面是**被验证**的冻结：legacy legs `33→33` 且集合相等、仓库表目录 `53→53` 且 ods/dwd 集合相等、
  `model_key_only 22→22`、注册来源数 `34→34`。
- 测试侧字面量：**17** 个派生表达式站点（`len(known)`、`scope.deferred_legs`、
  `len(expected_ods) + deferred_legs` 这类扫不出父字典的写法）按锚点改写，**18** 个
  `["<count>"] == <int>` 站点用一条机械扫描一次不漏地改写（负控制：
  `snapshot["batch4_baseline"]["all_symbols"] == 328` 与 `…["counts"]["all_symbols"] == 330`
  必须**不**被匹配，匹配了就会把 batch5 的裁决数改掉），外加 4 处 sha256 字面量。
  `counts_coverage … uncovered=[]` 说的是：每一个发生变化的计数都至少有一个测试站点接住了它。
- 反例：给 `domains.yaml` 加一个字节，摘要就与钉值不同（`counterfact_one_byte_appended_digest=…
  differs=True`）—— 这条 pin 不是重言式。

一处必须记下的仪器自我矛盾：第一个驱动打印 `moved_fields=369`，而本探针打印 `fields_moved=0`。
差别的来源是驱动在自己的行与"已钉行"比对时，已钉行带着 descriptor 驱动后加的
`canonical_ids_declared` 字段，于是每条行都"不等"；跑完两个驱动、字段补齐之后再比，才是 0。
取数要按**最后一步的状态**读，不能把驱动自己的打印当结论。

## 8. 收口前的载体路径更正

`probes/w50_carrier_path_correction.py`，读数 `runs/w50_carrier_path_correction.txt`：`arms=4 PASS=True`。

事实：`w45` 复用了 wave37 的链驱动，但只重指了 `RUNS`，没重指同样按 bundle 拼出来的
`PRE_CHAIN_OUTPUT`，于是 wave38 的链前判定报告（1710 claims / 0 accepted / 337 STALE）
落到了 wave37 已提交的 `C141/…/runs/judge_pre_chain.report.json|.md` 上。处理与证明：

1. 两份文件按字节搬到本 bundle（`runs/judge_pre_chain.report.json`，sha 前缀 `8d13469fb1a45297`），
   其 summary 与 `chain_summary.txt` 里链跑动时打印的 `pre_chain_stale_reading` **逐项相等**，
   而伪造 +5 的同一谓词必须拒绝（`counterfact_forged … matches_carrier=False`）；
2. `git diff --name-only HEAD -- docs/evidence/C141/` 为空，且两份 C141 文件的 worktree 摘要
   等于 `git show HEAD:` 的摘要（`restore … equal=True` ×2）——即 wave37 的载体已按字节复原；
3. 两代读数不同（1705/336/9 对 1710/337/8），所以复原不是把同一份字节复制回去；
4. `w45` 现在同时重指 `RUNS` 与 `PRE_CHAIN_OUTPUT`，并且新加的写点守卫对本 bundle 返回
   `outside_bundle=[]`、对伪造的外部 bundle 返回 `['PRE_CHAIN_OUTPUT', 'RUNS']` ——
   守卫能被触发，所以"已加守卫"这句才有内容。

## 10. 出货闸门：静态四件在本波载荷上为绿

`scripts/quality/a2_check.py --files <本波 25 个载荷路径>`，读数
`runs/a2_static_plane_slice_w38_payload.take4.txt`（`A2 files (complete resolver): 25`，
`a2_take4_exit=1`）：

- `ok ruff check`、`ok ruff format --check`、`ok mypy (18 unique target(s))`（18 个 mypy 目标逐个
  `exit=0`；`tests/` 按仓内配置被 mypy 排除，所以 25 个载荷里只有 18 个进 mypy）、`ok bandit`；
- 两条红的都在 gitleaks 家族上，且都是**按构造而红**：`FAIL pinned Gitleaks file scan` 与
  `FAIL: A2 gate failed on: immutable evidence secret scan`，报的全部是
  `docs/evidence/C100/**/inputs/pristine/akshare/*.py` 里那批 akshare 上游源文件自带的
  `leaks found`，与本波载荷无关（wave37 的同一面形态一致）；
- 载荷内没有凭证：`secret_scan_check` 与提交后的载荷扫描在 §11 里单独取面。

三次取面被作废的原因也记在这里，因为它们说明的是仪器的毛病而不是代码的毛病：
take1 红在 `ruff check / ruff format / mypy / bandit` 四件，全部是同一个原因——我在 take1 运行中途
删掉了 `w46_landing_face.py`（一份从未产出载体的草稿探针，它的臂被 `w48` 全部覆盖），工具读到
`No such file or directory`；take2 红在 `ruff format --check`（`tests/test_provider_catalog.py`
我改完没格式化）与 `mypy`（`w49` 里我复用了 `value` 这个变量名，数字循环把它绑成 `int`，
十六进制循环又要往里赋 `str`）；take3 四件齐绿但清单里还没有 `w51`，take4 才是覆盖 25 个路径的一次。
`w52` 晚于 take4，它按同一套工具单独核过（`ruff check`/`ruff format`/`mypy`/`bandit` 全绿），
提交前还会按最终清单再取一次面。

## 12. 残差 12 行：逐行的档位与它究竟缺哪一件东西

`probes/w52_residue_ladder.py`，读数 `runs/w52_residue_ladder.txt`
（`arms=7 arms_passed=7 failed=[] PASS=True`，`w52_residue_ladder_exit=0`）。

残差这个数是被**两条独立路径**同时给出的：判定器（进程内复用出货判定器）给
`identities=350`、`accepted=338` ⇒ 残差 12；出货的残差 census 探针逐行打印 12 行、
其嵌套合计 `sums=12` 亦为 12；两条相等才构成臂（`rows_equal_independent_residue`）。
census 自身的臂读数也被核：`green=['A_landed_positive','C_population_identity']`、
`red=['B_landed_negative']` —— 唯一那条红的正是"被消耗掉的负控制"
（`yfinance::BalanceSheet` 已在 wave32 落地，`is_residue=False`，负控制不可能再翻），
臂 `census_failure_is_the_known_one` 要求红的**恰好只有它**，别的臂一变红本面就停。

| 需要的动作 | 行数 | 行 | 机制读数 |
|---|---|---|---|
| `new_provider_package` | 2 | `biztoc::WorldNews`、`stockgrid::ShortVolume` | 盘上没有这两个来源的包（census 的 `empty_blobs=['biztoc','stockgrid']`），裁决粒度是**整包**而非行 |
| `ledger_ruled_out` | 9 | `cboe::FuturesCurve`、`intrinio::OptionsSnapshots`、`seeking_alpha::ForwardEpsEstimates`、`seeking_alpha::ForwardSalesEstimates`（`client_seam` 可达）；`econdb::CountryProfile`、`econdb::ExportDestinations`、`wsj::ETFActive`、`wsj::ETFGainers`、`wsj::ETFLosers`（`engine_transport` 从未被使用） | 接缝在盘上可达或传输面从未用上，但账本已判否；两行 seeking_alpha 的判否理由是本轮实测到的 403/400，见 §2 |
| `implement_missing_model` | 1 | `yfinance::AvailableIndices` | 唯一还可能是"写代码"的一行；机制读数是 `engine_transport`，`witness_module=True` 而 `name_in_package=False`，捕获面显示该来源没有可用端点 |

因此本波之后仍然剩 12 行，并且**其中 11 行等的是裁决，不是实现**：两行 seeking_alpha 需要
"是否投入凭证"的裁决，五行 ledger 判否需要"判否是否维持"的裁决，两行整包缺包需要"是否建包"的裁决，
一行是"0 端点的来源要不要留名"的裁决；只有 `yfinance::AvailableIndices` 一行在形式上还挂在一个
实现问题上，而它缺的是端点本身。

## 9. 测试平面：红被按 node-id 集合归因，而不是按数量

两份完整平面（同一份清单，`tests/` 全目录）：

| 平面 | tests | failures | skipped | 用时 | 载体退出行 |
|---|---|---|---|---|---|
| 本工作树（wave38 代码 + 重钉后的围栏） | 26630 | 28 | 271 | 709.09s | `runs/test_plane_w38_full.take2.txt`：`test_plane_exit=1` |
| 对照：`66fe1acc9`（wave37 已提交，wave38 之前）的 detached worktree，`docs/evidence/C100/**` 就位 | 26626 | 37 | 271 | 646.25s | `runs/test_plane_head37_control.txt`：`ctrl_plane_exit=1` |

`probes/w51_test_plane_attribution.py` 读这两份 junit，读数
`runs/w51_test_plane_attribution.txt`（`arms=8 arms_passed=8 failed=[] PASS=True`）：

- `set_diff worktree=28 control=37 shared=28 **new=0** fixed=9`；集合减法两面都算过：
  `worktree==shared+new=True`、`control==shared+fixed=True` —— 本波**没有引入任何一条新红**；
- 被修好的 9 条红，其在**对照树里**的断言面逐条核过：7 条打印的是
  `AssertionError: opendata/data/domains.yaml assert '<活摘要>' == 'c4005525…'`
  —— 正是围栏对 `domains.yaml` 的字节 pin；2 条打印的是
  `Extra items in the left set: 'cboe_index_snapshots'` —— 正是同一份围栏的域 id 清单。
  于是 §7 那句"真因是这条字节 pin"不是叙事，是断言面上的字符串；
- 两条臂本身也带反例：伪造一条本工作树没有的 node id 会让 `no_new_red` 臂翻红
  （`no_new_red_arm_can_bite`），把 `FORGED_MESSAGE` 喂给围栏标记谓词必须返回不匹配
  （`fence_marker_predicate_can_refuse`）——能失败的臂才算臂；
- 对照树的身份也被核：它自报 `commit=66fe1acc9086…`，`merge-base --is-ancestor` 判定它是本工作树
  HEAD 的祖先（`control_is_a_pre_wave_ancestor`）。

**这 28 条共享的红不是本波的**，但也不是同一件事，按断言面分成五族（族计数之和 = 28）：

1. **注册表普查漂移 14 条**：`test_provider_catalog`(`assert 371 == 342`、`oecd assert 11 == 2`)、
   `test_oecd_engine_provider` ×3(`11 == 2`、`DID NOT RAISE LookupError`、集合多出 `economy_oecd_gdp_nominal`)、
   `test_congress_gov_engine_provider` ×5(`8 == 0`、`assert True is False` 等)、
   `test_ecb_reference_registration`(`371 == 62`)、`test_first_model_bindings`(注册顺序 index 16
   `'fred' != 'imf'`)、`test_federal_reserve_domains`(`'federal_reserve_yield_curve' not in {…}`)、
   `test_scheduled_patrol`(`all(<generator>)` 为假)。这些是**同一类围栏**：每个 provider 自己钉的
   绑定数/域清单，被 wave32-34 与 wave37-38 逐波放宽却从未跟着重钉。本波只重钉了 batch5 那一条
   （§7），其余每条都需要按 §7 同样的"活测量 + 打印差值 + 反例"流程单独走一波，
   且其中若干条要先有 owner 裁决（例如 oecd/congress_gov 的绑定数该是多少）。
2. **产品运行面缺陷 2 条**：`test_data_catalog` 报
   `NameError: name '_EconomicCalendarQuery' is not defined` ——
   `opendata/data/providers/tradingeconomics/models/economic_calendar.py:41` 把查询模型定义在
   `TYPE_CHECKING` 块里，而 `:265-266` 的函数签名在运行时被解析；`test_congress_cboe_iter2[CongressAmendments]`
   报 `pydantic_core.ValidationError: number Input should be a valid integer`。两条都在已验收的行上，
   也就是**平面 1 的 OFFLINE_ACCEPTED 不等于这条运行路径可用**——这一条我按读数上报，不替它辩护。
3. **权限登记面 4 条**：`test_openbb_inventory_guard`(`KeyError: 'rights_rows'`、`IndexError`、
   `assert (1, 1) == (1, 0)`、24 条 `RIGHTS LINK` 未被记录)。这就是 akshare/ths 那族 owner 裁决，
   判定侧与登记侧对不上，不是实现缺口。
4. **证据清单自计数 5 条**：`test_immutable_evidence`(`5055 == 172`)、`test_c65_evidence_probes`
   (`'13' == '14'`)、`test_ac2_case_probe`、`test_acceptance_probe_gate`、`test_p0_integration_surface`
   (后两条同一原因：集合里多出 `tests/test_financial_pit_api.py`)。计数随证据树增长而漂移。
5. **机器本地依赖 3 条**：`test_script_download_backend_iter2[…owned_mysql]`
   (`DockerCommandError … image-inspect return_code=1`，本机没有那个镜像)、
   `test_e2e_opt_in_guard`(同一条被守卫点名，因为它在活平面之外建了引擎)、
   `test_port_fidelity[bond_daily_sina]`(`ReplayRequestMismatchError: request mismatch before replay
   cursor advance at call 1`)。

## 11. 凭证面

索引面在提交前取：`probes/w41b_staged_credential_scan.py`，读数
`runs/w41b_staged_credential_scan.txt`——`staged_files=2252`、
`staged_outside_wave=0`（这条臂第一次跑时是红的：它自己的清单里没有围栏重钉那 5 个路径，
把 5 条补进去才绿，说明这条臂真的在看路径）、
`worktree_differs_from_index_files=0`、载荷物化后的索引摘要 `payload_index_sha256=b258952e002e1b9c…`。
三套规则各跑两遍（基线与投毒）：
`A_shipped baseline=0 / planted=2`、`B_allowlist_stripped baseline=0 / planted=2`、
`C_builtin baseline=0 / planted=2`，且投毒命中的规则集是
`{'github-pat': 1, 'generic-api-key': 1}` —— 载荷本身 0 命中，而伪造 token 一定被读到，
这才是"0 发现"的资格。伪造 token 只在运行期拼装、从不打印，投毒落在 `/tmp` 下的副本里，
`plant_files_inside_staged_payload=0` 证明它没混进被扫的载荷。

提交后的 git-mode 与 dir-mode 两面由 `probes/w47_closure_face.py --commit <feat sha>` 取，
随收口面一起入档；它同时把逐档字节对账（`git show <sha>:<path>` 与工作树字节）与
父提交漂移孪生面放在一起。
