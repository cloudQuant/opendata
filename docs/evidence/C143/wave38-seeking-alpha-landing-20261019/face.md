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

## 9. 测试平面：本波引入 0 条新红，修好 9 条

两份完整平面（同一份 `tests/` 清单，全部 26630/26626 项）：

| 平面 | 项 | 失败 | 跳过 | 用时 | 载体里的退出行 |
|---|---|---|---|---|---|
| 本工作树（wave38 实现 + 重钉后的围栏） | 26630 | 28 | 271 | 709.32s（junit 计数 `time=709.089`） | `runs/test_plane_w38_full.take2.txt`：`test_plane_exit=1` |
| 同一棵工作树的前一次完整跑 | 26630 | 28 | 271 | 653.49s | `runs/test_plane_w38_full.txt`（退出行丢了，见下） |
| 对照：wave37 已提交那一次（`66fe1acc9`）的 detached worktree，且把被 gitignore 的 `docs/evidence/C100/**` 复制进去 | 26626 | 37 | 271 | 646.25s | `runs/test_plane_head37_control.txt`：`ctrl_plane_exit=1` |

第一跑的包装器把退出行打到了控制台而不是载体内（`echo` 写在重定向之后），这条缺口由第二跑补上：
`{ pytest …; echo "test_plane_exit=$?"; } > 载档` 的写法让载体自带退出码，
而两跑的 node-id 集合**逐条相同**（臂 `repeat_run_reproduces_the_set`：
`repeat_run present=True failures=28 same_node_set=True difference=[]`），
所以"这一棵树的红集合"不是抖动出来的。对照平面的退出码也在自己的载档里
（`plane_exit_lines {'worktree': 'test_plane_exit=1', 'control': 'ctrl_plane_exit=1'}`）。

`probes/w51_test_plane_attribution.py` 读这两份 junit 的 node-id **集合**，读数
`runs/w51_test_plane_attribution.txt`：`set_diff worktree=28 control=37 shared=28 new=0 fixed=9`，
`new_red_nodes=[]`，两侧算术都成立（`worktree==shared+new`、`control==shared+fixed`），
并且臂 `no_new_red_arm_can_bite` 用一条伪造的 node id 证明这条臂会翻红 —— 所以 `new=0` 是读数，
不是"扫不出差别"。两份平面各自的退出行也在载体文件里被找到并核过：
  `plane_exit_lines {'worktree': 'test_plane_exit=1', 'control': 'ctrl_plane_exit=1'}`
  （臂 `both_planes_ship_exit_lines`；另一条臂 `neither_plane_had_collection_errors` 核两份平面的
  junit `errors="0"`，即 28 与 37 都不是收集错误的产物）。

**被修好的 9 条为什么算被这条围栏解释**：探针不看测试名，只看它们**在对照树里**的断言字符串，
9/9 都带围栏的标记 —— 7 条是
`AssertionError: opendata/data/domains.yaml assert '<盘上摘要>' == '<fixture 里的钉值 c4005525…>'`
（即 fixture 对 `domains.yaml` 的字节 pin），2 条是
`AssertionError: assert {…} == {…} Extra items in the left set: 'cboe_index_snapshots'`
（即同一份 fixture 的域 id 清单）。反过来，一条伪造的、与围栏无关的断言字符串喂进同一个谓词
必须不匹配（`fence_marker_predicate_can_refuse`）。九条的名字也全部落在围栏的五个消费文件里。
这就是 §7 那句作废声明的证据形状：wave37 把其中 4 条判成"两棵树一样红 = 既有失败"，而对照树里
它们的红其实是这条 pin。

**共享的 28 条**在本波之前就已经红（每一条都在对照集合里，`new=0`），按断言面分五族，
族计数相加 = 28：

| 族 | 行数 | 断言面 |
|---|---|---|
| 注册表普查漂移 | 13 | `assert 371 == 342`（catalog bindings）、`oecd assert 11 == 2`、congress_gov `assert 8 == 0` 与 `assert True is False`（`is_implemented`）、ecb `assert 371 == 62`、federal_reserve `'federal_reserve_yield_curve' not in {…}`、first_model_bindings 注册顺序 `At index 16 diff: 'fred' != 'imf'` |
| 产品运行面缺陷 | 2 | `test_data_catalog` 报 `NameError: name '_EconomicCalendarQuery' is not defined`；`test_congress_cboe_iter2[CongressAmendments]` 报 pydantic `number Input should be a valid integer` |
| 权限/权利登记面 | 4 | `test_openbb_inventory_guard`：`KeyError: 'rights_rows'`、`IndexError`、`assert (1, 1) == (1, 0)`、`assert ['RIGHTS LINK…'] == []`（多出 24 条） |
| 机器本地依赖 | 4 | Docker `image-inspect return_code=1`（owned_mysql 那条）、被同一事实触发的 `test_e2e_opt_in_guard`、`ReplayRequestMismatchError`（port_fidelity bond_daily_sina）、`test_scheduled_patrol` 的 `all(<generator>)` 为假 |
| 证据自计数漂移 | 5 | `assert 5055 == 172`（仓库清单）、`assert '13' == '14'`、`breaks=5 flipped=5 …`、两条集合差 `Extra items in the left set: 'tests/test_financial_pit_api.py'` |

两族值得单独说，因为它们不是"数字没更新"这么简单：

* 普查漂移那 13 条与 §7 修掉的围栏是**同一类缺陷**——每个 provider 自己钉的绑定数/域清单被逐波
  放宽却从未跟着重钉。本波只重钉了 batch5 那一份（它的消费面最广），其余的要先由 owner 决定
  "这一族数字到底是几"，不是我能替它定的数；
* 运行面那 2 条里，`_EconomicCalendarQuery` 是一条**已验收行**的模块在运行时缺名字
  （`opendata/data/providers/tradingeconomics/models/economic_calendar.py:41` 把该类型定义在
  `TYPE_CHECKING` 下，`:265` 起的签名/局部标注在运行时被求值）。平面 1 的 `OFFLINE_ACCEPTED`
  不覆盖这条解析路径，所以这条读数写在这里当作交付面的一部分，而不是当作别人的偶发失败。

## 10. 出货闸门：静态四件在本波载荷上为绿

`scripts/quality/a2_check.py --files <本波 26 个载荷路径>`，读数
`runs/a2_static_plane_slice_w38_payload.take5.txt`（`A2 files (complete resolver): 26`，
`a2_take5_exit=1`）：

- `ok ruff check`、`ok ruff format --check`、`ok mypy (19 unique target(s))`（19 个 mypy 目标逐个
  `exit=0`；`tests/` 按仓内配置被 mypy 排除，所以 26 个载荷里只有 19 个进 mypy）、`ok bandit`；
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
十六进制循环又要往里赋 `str`）；take3 四件齐绿但清单里还没有 `w51`，take4 覆盖了 25 个路径但还没有 `w52`；
take5 是覆盖最终 26 个载荷的那一次，也就是上面引用的那一份。

## 11. 凭证面：载荷 0 命中，且三套规则都报得出被投毒的 token

* **feat 提交的索引面**（提交前）：`runs/w41b_staged_credential_scan.feat_staging.stdout.txt` ——
  那一次运行时 `w41b` 只往 stdout 打、不写载体，所以这份载体是包装器逐字保存的运行输出
  （头部写明 argv 与它是被扫描的那一次索引），并且 `w47` 读的就是这份文件；
  `staged_files=2262`、`staged_outside_wave=0`、`worktree_differs_from_index_files=0`（即提交的字节
  就是这一面读过的字节）、载荷按 `git show :<path>` 物化后
  `payload_index_sha256=b258952e002e1b9c…`；三套规则各读两遍：
  `A_shipped baseline=0 planted=2`、`B_allowlist_stripped baseline=0 planted=2`、
  `C_builtin baseline=0 planted=2`，`moved=True` 三件齐，命中的规则集是
  `{'github-pat': 1, 'generic-api-key': 1}`；`allowlist_suppressed_in_this_payload=0`、
  `plant_files_inside_staged_payload=0`（伪造 token 只活在 `/tmp` 副本里，且只在运行期拼装、从不打印）。
  `arms=11 arms_passed=11 PASS=True`，`w41b_staged_credential_scan_exit=0`。
  第一次取面时 `scope` 臂是红的：我的波清单里漏了围栏重钉动的 5 个路径
  （`tests/fixtures/registry_batch5_snapshot.json` 与四个消费测试）——那正是这条臂该拦的东西。
  这个"载体没落盘"的缺口已经补掉：`w41b` 现在把本次输出逐字写进
  `runs/w41b_staged_credential_scan.txt`，下面这条 docs 提交用的就是它自己写的载体。
* **docs 提交的索引面**：`runs/w41b_staged_credential_scan.txt`（同一支探针，扫的是收口这一批
  载荷的索引），三套规则的基线仍须为 0、投毒仍须非 0，两臂同时成立才算数。
* **提交后载荷面**：`probes/w47_closure_face.py --commit <feat sha>`，
  见 §13 的收口面读数。

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


## 13. 收口面：对 `dffa64969` 的字节对账与载荷扫描

`probes/w47_closure_face.py --commit dffa64969`（父 `66fe1acc9`），读数
`runs/w47_closure_face.txt`（diff 面，7/8 臂）与包装器输出
`runs/w47_closure_face.feat.stdout.txt`；`--scope tree` 的那一次见 §13.1。

* **逐档字节对账**：`commit_paths=2262`、`moved_vs_parent=2262`（每一条都相对父提交真的变了，
  不是"路径存在而已"）、`missing_in_commit=0`，而 `worktree_drift_vs_commit=2` ——
  两条漂移被逐名打印：`face.md` 与 `w47_closure_face.py` 本身。原因是**先提交载荷、后修仪器**：
  feat 提交之后我又改了这份面和收口探针（`w41b` 补写载体、`w51` 补退出码与复现臂、
  `w47` 补 `--scope tree`）。这条臂因此是红的，`arms=8 arms_passed=7 failed=['worktree_matches_commit']`，
  `w47_closure_face_exit=1`；剩下的 2260 条字节一致。这两条被改的文件随本收口提交入档，
  收口提交之后另取一次树面对账（`--scope tree`），那一面覆盖"波内全部前缀在树里的每条路径"，
  所以它核的是"提交后的树 == 工作树"，而不是只核那一次提交改了什么。
* **git 模式载荷扫描**：`gitleaks_git_mode commit=dffa64969 rc=0 findings=0 rules=[]`，
  同一份 argv 扫一个只往里放伪造 token 的临时仓库得 `findings=2`
  （`arm_can_move=True`）——所以那个 0 是"扫得动之后没有"，不是"扫不动所以没有"。
* **目录模式**：树是从 `git ls-tree <commit>` 建的（`copied_from_commit=2242`），不是工作树副本；
  工作树里多出来的 3563 个本地文件（历史 bundle 与被 gitignore 的输入）被点名而不被扫，
  `bytecode_skipped=3` 记下了本地 `__pycache__`——上一波正是它把常量折叠出来的伪造 token 写进了
  `.pyc`，所以目录面只认提交里的字节。结果同样 `findings=0`，投毒对照 `findings=2`。
* **出货闸门**：仓内自带的 `secret_scan_check` `rc=0`。
* **代次摘要**：`claims_sha256=832268d0eb1dc8e8`、`judge_report_sha256=1ba70159c25af361`
  ——与 §1 的 338/1725 是同一代。
