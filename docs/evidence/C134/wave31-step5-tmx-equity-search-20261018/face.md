# wave-31 step 5 —— `tmx::EquitySearch` 落地面（本仓第 291 枚离线验收行）

日期 2026-10-18（UTC 记录见各载档 `generated_at_utc` 与合并对象时间戳）。本节所有数字都来自 `runs/`
里载档的原文行，引用形式 `runs/<载档>.txt:<行>`；每条链路载档自己带 argv、`stderr_bytes` 与
`<name>_exit=<数字>` 收尾行，7 把 take-1 例外（§8 逐条列出并给分类）。本轮无子代理（预算 30/30
已用尽），无 push，无 live 腿：`OPENDATA_ALLOW_LIVE_E2E` 未设置，五相每一腿的 socket 由构造封死。

**本波落地两次的不是同一份代码。**第一稿给 `EquitySearchFetcher.transform_data` 加了一枚 list 形状
守卫，并把见证项的 tamper 写成"把整个 payload 置成非 list"；两条都在测量之后撤回（§2.2、§4.2）。
撤回后的落地只剩两件事：一行 `row_model` 声明 + 一条 shard 见证项。因此本波的载档要同时给三代字节
定价：HEAD（无声明）、被撤回的守卫代、 shipped 代（§3.2）。

## 1. 结论面

| 量 | 落地前 | 落地后 | 载档 |
| --- | --- | --- | --- |
| plane 1 `offline_acceptance_count` | 290 | **291** / 350 | `runs/wave31-step5-judge.txt:4-5`；前一格 290 取自 step-4 的 `runs/wave31-step4-judge.txt:4` |
| `identity_set_sha256`（前 16） | `5168e1e723999312` | 同一枚，未动 | `judge.txt:5`，另有 `readiness_census.txt:10` `expected_match=True` |
| `claim_count` | 1565 | 1565（未动） | `judge.txt:5`、`merge_take1.txt:32` |
| `primary_review_count` | 290 | **291** | `judge.txt:5`、`merge_take1.txt:32` `reviews=291` |
| `INCOMPLETE` | 23 | **22** | `judge.txt:5` |
| `UNREVIEWED` | 37 | 37（未动） | `judge.txt:5` |
| tmx 已验收行 | 7 | **8** | `judge.txt:7` |
| 残留总数 | 60 | **59** = NEED_BINDING 32 + NEED_DECLARE 12 + NEED_SHARD_ENTRY 15 | `residue_axis_census.txt:66`、`readiness_census.txt:7` |
| 行动图 `add_code_carrying_guard` | 12 | **11**（tmx 那枚已清零，剩 11 枚全在 yfinance） | `residue_axis_census.txt:77` 与逐行 `grep action=add_code_carrying_guard` |
| 行动图 `NEED_DECLARE`（readiness 列） | 13 | **12** | `readiness_census.txt:7` |
| `rows_without_module` / `rows_with_module` | 37 / 23 | **37 / 22**（首模块数不动，因为有模块的行退了一枚） | `residue_axis_census.txt:75` |
| `F_witnessable_today` | 0 | 0（仍是真零，同一组对照臂） | `residue_axis_census.txt:74` |
| plane 2 `declared/dev_done/missing_model_rows` | 17/17/19（step 0） | **17/17/19，本波重取且逐臂复算** | `plane2_dev_done_axis_census.txt:143` `arms=15 arms_passed=15` |

plane 2 这一格是 step-4 §1.3 挂的那条**未偿项**：那节写"本波没测，也不该假装测过"，并要求下一波按
当代总体重跑那台仪器。本波跑了（15/15 臂绿），并且因此撞上了它预告的副作用——那台仪器把载档写死在
`docs/evidence/C131/.../wave31-step0-plane2-axis-census.txt`，于是老化了 step-0 的历史面。处置见 §4.6。

产品码字节面由 `probes/landed_bytes_face.py` 现测（10 臂，`landed_bytes_face.txt:17`
`arms=10 arms_passed=10 all_pass=True`）。两枚落地文件逐枚同时打磁盘字节、`git show HEAD:` 字节与
producer 写进 shard 的 `after_sha256`：

| 文件 | disk（shipped 代） | HEAD | 被撤回的守卫代 | shard pin | 载档 |
| --- | --- | --- | --- | --- | --- |
| `opendata/data/providers/tmx/models/fetchers.py` | `7b072d074d8d53ba` | `efa3e5bb4cf80d3e` | `ebf50c4dff294794` | 追平 | `landed_bytes_face.txt:4`、`:16` |
| `scripts/quality/iter2_witnesses/tmx.py` | `f0fdc863033b9c6f` | `bb0ffb640c4a751d` | `366b34582a48f0c4` | 追平 | `:5`、`:16` |

`:9` `C_pin_membership pinned=[fetchers.py, tmx.py] unpinned=[]`（两枚都进 shard，所以都欠换代税）；
`:11` `E_denominator this_probe=17 drift_carrier=17 carrier_stale_files=0`；`:15`
`I_head_plane_self_consistent pinned_at_head=2 of=2 agree=2`，即 HEAD 那片的 pin 就是 HEAD 自己的
字节，干净 clone 不需要先重签。`:14` 是本波**没有**编辑的控制臂
`scripts/quality/iter2_claims_producer.py`（`e2fdc57e0b30ee48`，disk==head），`moved` 谓词靠它证明
能读 False。

换代税两面：落地前 `pin_drift_pre_production.txt:5-6` 打两枚 `STALE`（守卫代钉的
`ebf50c4dff294794`/`366b34582a48f0c4` 对上磁盘 shipped 代），`:7` `pinned_files=17 stale_files=2`，
`drifted_rows` 恰好是 tmx 那 8 枚已验收行；链路重生产之后
`pin_drift_pre_production_rerun.txt:5` `pinned_files=17 stale_files=0 drifted_rows=[]`。全树收档面
`receipt_pin_consistency.txt:8` `slots=20744 stale=0 before_ne_after=0 distinct_pinned_paths=589`
`arms=3 arms_passed=3`——这是 step-4 §6 第四条失效条件（"别的 provider 的 pin 被本波之外的编辑换掉"）
的核对面。

### 1.1 本波实际执行的五相

producer 对 tmx 全部 24 枚 inventory 行重跑五相：`producer.txt:45-49` 是 `EquitySearch` 的
implementation / direct / service / contract / counterfact 五腿，逐腿 `exit=0 status=PASS`，每腿的完整
argv 打在同一行里（都带 `--evidence-root docs/evidence/C101`）。`:125` 分母行原文：
`claims emitted=120 (40 PASS, 0 FAIL, 80 BLOCKED) = 24 row(s) x 5 phases; phases actually executed as
subprocesses=120`。同一行还有 `catalog-resolvable bound identities this wave=318/350` 与
`provider rows covered=24 of provider inventory rows=24, of which bound now=8/318 resolvable`。
最后那个标签与它数的东西不符（120 是 claim 条数，真跑过的子进程是 40 条 PASS 腿；80 条 BLOCKED 腿在
逐相行里打 `<not run>`），登记为 #69，本波不修，见 §4.4。

主审记录：`recorder.txt:9` `REVIEW_SIGNED: tmx::EquitySearch`，`:13`
`signed=8 refused=0 reviews_total=8`——roster 是从重生产后的 shard 里"五相全 PASS"的行推出来的，
不是手打的（`w31s5_run_chain.py:witnessed_models()`）。合并：`merge_take1.txt:32`
`claims=1565 identities=313 reviews=291 accepted_identities=291`；`merge_take2.txt:34`
`take1=94b746e42053f4c2 take2=94b746e42053f4c2 identical=True`。判定：`judge.txt:4`
`350 identities, 291 offline accepted`。判定行本身逐字段核过：五相 `claim_status: PASS`、
counterfact `expected == observed`（`TmxProviderError` / `TMX_BAD_ROW`）、review
`decision ACCEPT / status VALID / evidence_snapshot_status MATCHED`，
`issues`/`missing_material`/`missing_phases` 全空。

### 1.2 分母腿

`runs/wave31-step5-pytest_tmx_and_pin_planes.txt:17` `709 passed, 179 skipped`；
`runs/wave31-step5-pytest_collect_denominator.txt:914` `888 tests collected`，709 + 179 = 888 闭合，
没有样本消失进 skip。逐模块拆法取自同一把载档的树（不是控制台）：
`test_provider_model_contracts.py` 812、`test_iter2_model_evidence.py` 52、
`test_tmx_provider.py` 18、`test_provider_domains.py` 6，相加 888。

812 那格与 step-4 的 812 是**同一个总体**（同一份契约判定器的参数化表），所以这里可以写差值：0。
机制上也闭合：本行是手写 fetcher，不进 `engine_declared_models()` 的声明式总体；
`tests/test_tmx_provider.py` 的参数化表（`:112`、`:355`、`:405`）是测试模块自己的字面表，而本波
**没有改 `tests/` 任何字节**（`git status --porcelain tests/` 空），所以 18 那格由构造不变。
step-4 §7.7 撤过的那条"collected 涨是 mover"错误读法，本波不再重复：没有同总体的前代面就不写差值。

### 1.3 静态平面

`ruff_check.txt` `All checks passed!` `ruff_check_exit=0`；`ruff_format.txt` `5 files already formatted`
`ruff_format_exit=0`；`mypy.txt` `Success: no issues found in 2 source files` `mypy_exit=0`；
`bandit.txt` `bandit_exit=0`。mypy 的 roster 只有两枚（见证模块与 fetchers），沿用 step-4 §4.6 定下的
口径：`tests/` 与 `scripts/quality/` 不在出厂 typecheck 平面（`Makefile:26`）的总体里，把它们点进来
是这台链自己造红。

## 2. 产品码与见证项

### 2.1 落地的那两件事

`opendata/data/providers/tmx/models/fetchers.py` 的整份 diff 是**一行**：
`EquitySearchFetcher` 在 `canonical_model` 与 `capability` 之间插入
`row_model: ClassVar[type[TmxEquitySearch]] = TmxEquitySearch`。没有新守卫，没有新拒绝码，
`transform_data` 一个字节都没动。

`scripts/quality/iter2_witnesses/tmx.py`（+55 行）加两枚 fixture 函数与一条见证项：

- `_equity_search_payload()`：两行 `findTmxCompaniesByKeywords` 形状的公司行（`SHOP`/`SAP`，
  四列 `symbol`/`name`/`exchange`/`instrumentType`）。
- `_equity_search_document()`：包进 `_document(...)` 的 TMX GraphQL 信封。
- `CONFIG["models"]["EquitySearch"]`：`client_seam=_SEAM`、`query={"keywords": "shopify"}`、
  `tamper={"op": "drop", "path": [responses, 0, body, data, findTmxCompaniesByKeywords, 0, symbol]}`、
  `refusal_class=TmxProviderError`、`refusal_code=TMX_BAD_ROW`，`source_files` 把
  `fetchers.py` 追加在该 provider 出厂源文件表之前。

拒绝链（ shipped 代实际依赖的机制）：`client.list_rows` 对非 list payload 打
`TMX_BAD_ROWS`；逐行的 `_TmxEquitySearchSourceRow`（`extra="forbid"`、四个
`StrictStr | None` 且**无默认值**）让"少一个键"成为 `TMX_BAD_ROW`，由
`_equity_search_rows` 从 fetchers 模块帧抛出。见证项钉的是后者。

### 2.2 两条撤回，都有测量

1. **list 形状守卫**：第一稿在 `transform_data` 里对非 list payload 抛 `TMX_BAD_ROWS`。撤回理由不是
   风格，是死码测量：`consumed_control_rearm.txt:9` `C_no_dead_code inline_raise_lines=[]
   via_mapper=(TmxProviderError,TMX_BAD_ROWS,opendata.data.providers.tmx.models.client)
   via_client=(TmxProviderError,TMX_BAD_ROWS)`——AST 臂在该类该方法体内数**内联 raise 行**得 0，
   而经由 mapper 调用与直接调 `tmx_client.list_rows("not_a_list")` 都从 **client 模块帧**拿到同一枚
   拒绝码。守卫的那段分支永远不可能被走到。
2. **"把 payload 置成非 list"的 tamper**：撤回理由与上一条同源——那条 tamper 证明的是 client 层的
   屏幕，却会被读成 mapper 的屏幕。换成 `op=drop` 单键之后，
   `consumed_control_rearm.txt:10` `D_tamper_bites` 逐臂给出：`drop_symbol` →
   `('TmxProviderError', 'TMX_BAD_ROW', '...fetchers')`（帧在 fetchers，即在映射层），
   而 `set_null_symbol` → `('SERVED', 'rows=2')`。后者是这条见证项**在价格式什么**的正面对撞：
   把 `symbol` 置 null 照样出两行，因为发布的 `TmxEquitySearch` 契约里 `symbol` 是可选的，所以本项
   价格的是回包列集，不是"命中可寻址性"。`pristine_after=('SERVED', 'rows=2')` 同时证明
   `_apply_tamper` 自己深拷贝（原始文档没被前一条 tamper 污染）。
   `tamper_reason` 里这段是实测散文，不是设计意图。

撤回的字节代留档不删：`*_take1_withdrawn_guard.txt` 7 把（§8），并由
`landed_bytes_face.txt:16` `J_intermediate_generation ... is_head=False is_today=False` 指名它属于
哪一代。

## 3. 反例与控制臂（下表 7 行、8 台仪器、60 臂，全部 `arms_passed=arms`）

下表逐行臂数相加 `4+10+11+6+8+15+3+3=60`（最后一行是两台共用一行的 3+3），这个和是把表里的格子
现加出来的，不是沿用小节标题里原来那枚"54"——那枚只数到了前六行，属于我先前的一处自相矛盾，
按 step-4 §7 的规矩原文留档在这里并当场改正。凭证预扫那台不在这 60 臂里，见下一段与 §3.5。

| 仪器 | 臂 | 载档 | 收口行 |
| --- | --- | --- | --- |
| `consumed_control_rearm.py` | 4 | `consumed_control_rearm.txt` | `:12 arms=4 arms_passed=4`，`:13 exit=0` |
| `landed_bytes_face.py` | 10 | `landed_bytes_face.txt` | `:17 all_pass=True` |
| `c131_carrier_restore.py` | 11 | `c131_carrier_restore.txt` | `:11 arms=11 arms_passed=11`，`:12 exit=0` |
| `incomplete_row_guard_check.py`（C128，出厂） | 6 | `incomplete_row_guard_check.txt` | `:59 arms=6 arms_passed=6`，`:60 exit=0` |
| `residue_axis_census.py`（C128，出厂） | 8 | `residue_axis_census.txt` | `:77 arms=8 arms_passed=8`，`:78 exit=0` |
| `plane2_dev_done_axis_census.py`（C131，出厂） | 15 | `plane2_dev_done_axis_census.txt` | `:143 arms=15 arms_passed=15`，`exit=0` |
| `readiness_census.py` / `receipt_pin_consistency.py`（C128，出厂） | 3 + 3 | 同名载档 | `readiness_census.txt:11`、`receipt_pin_consistency.txt:8` |

`probes/staged_payload_scan.py` 的臂在 §3.5 单列：它跑两趟，index 那趟早于本文件定稿，head 那趟
晚于第一枚提交，所以任何一趟的取面时刻都与本文件不同代。

### 3.1 被消耗的对照控制臂：`consumed_control_rearm.py`（4 臂）

这台仪器是为 §4.5 那条仪器缺陷造的。四臂：

- `A_consumed_control`（`:7`）：把**旧**控制标签 `tmx::EquitySearch` 喂进出厂分类器的同一谓词，读
  `verdict=NO_CODE_SITE judge_status=OFFLINE_ACCEPTED`，并同时打两条子句的独立读数
  `verdict_only_arm_b=True membership_arm_b=False`——旧那条"verdict 不在 TRIPPABLE"仍然为绿，
  新加的"必须在残留里"为红。这就是新子句**能咬**的证明，不需要前后两代对照。
- `B_rearmed_control`（`:8`）：新控制 `yfinance::BalanceSheet` 读 `NO_CODE_SITE`、`bound=True`、
  `judge_status=UNREVIEWED`、`in_residue=True`，且 `class_td=0 package_td=30`。
- `C_no_dead_code`（`:9`）：§2.2 第 1 条的测量。
- `D_tamper_bites`（`:10`）：§2.2 第 2 条的测量。

`:11` `CONTROLS_DIFF old=...:NO_CODE_SITE/OFFLINE_ACCEPTED new=...:NO_CODE_SITE/UNREVIEWED
verdict_moved=False`——被消耗并没有移动 verdict，移动的是 judge 标签与残留归属，这条是 §4.5 的根。
`:6` 总体行 `report_rows=350 judged=350 … identity_set=5168e1e723999312 old_in_residue=False
new_in_residue=True`，且 `identity_set_is_pin` 那枚门是直接比 `iter2_model_evidence.EXPECTED_IDENTITY_SHA256`
（出厂常量），不是我抄的字符串。

### 3.2 字节面：`landed_bytes_face.py`（10 臂）

见 §1 的表。这一台是 step-4 §3.4 立的规矩的第二次执行：任何"我手打的摘要"都必须由仪器逐枚并排打
磁盘 / `git show HEAD:` / shard `after_sha256` 三条通道。本波多加的第 J 臂专门给**第三代字节**命名
（被撤回的守卫代），所以读者能区分"HEAD 没声明"与"声明过又撤回"这两种 `moved=True`。

### 3.3 出厂仪器的当代面

`incomplete_row_guard_check.txt:59` `second_axis_face incomplete=22 verdicts={'UNBOUND': 22}
convertible=0`——本波之后，22 枚 INCOMPLETE **全是 UNBOUND**（step-4 那代还有 1 枚 `NO_CODE_SITE`
与 1 枚 `ROW_CLASS_NOT_CONTRACT`；那两枚是 bound 行，仍都在残留里，只是不再落在 INCOMPLETE 桶）。
`:55` `C_class_scoped yfinance::BalanceSheet class_td=0 package_td=30 PASS=True` 是重定向后的控制臂
在出厂仪器里同样绿的证明；`:57` `E_transport_predicate_discriminates after_counts={True: 3, False: 5}`
说明该谓词两侧都读得到。

`residue_axis_census.txt:69` `A_landed_positive controls=2 PASS=True`、`:70`
`B_landed_negative yfinance::BalanceSheet verdict=NO_CODE_SITE is_residue=True PASS=True`（新子句
两个合取都打出来）、`:71` `C_population_identity residue=59 … nested_sum=59`、`:74`
`F_witnessable_today count=0 … controls=2 control_actions_on_harness=['shard_entry_only']
control_actions_off_harness=['shard_entry_needs_harness'] control_real_fake_ok=[True, True]`。
`:66` `4_action_totals actions=6 nested_sum=59 residue=59`。

`seam_reachability_census.txt:40` `residual_total=59 accepted_total=291
accepted_plus_residual=350 identity_set=5168e1e723999312`，残留侧分桶
`SEAM_REACHABLE 20 / SEAM_NEVER_USED 37 / NO_PACKAGE_ON_DISK 2`（step-4 那代是 21/37/2：本波把一枚
REACHABLE 落地成了验收行）；`:39` `witness_modules=27 client_seam_modules=12`——本波没有新建模块，
所以 27 未动。

`batch5_fence_dry_census.txt:9` `matched=31 stale=0 missing=0`，`:10` 是设计内拒绝
`REFUSED - no stale pin to re-base`，`:11` `exit=1`。这条**不是**红检查：`--apply` 故意没传。

### 3.4 plane 2：本波重取，且读数与 step 0 同格

`plane2_dev_done_axis_census.txt:143`
`declared=17 dev_done=17 dev_done_label=5 label_without_witness=0 accepted_declared=17
missing_model_rows=19 rows_in_declaring_provider=0 PASS=True`。与 step-0 的 17/17/19 逐格相同，
所以本波对 plane 2 的净影响是 0，机制与 step-4 §1.3 的推理一致（本行是手写 fetcher，
不在声明式 ModelSpec 总体里）——区别是这次那句话**有面**，不再是纯推理。

代价也一并记下：这台仪器没有 `--output`，写死路径，于是覆盖了 step-0 的载档；§4.6 是给这件事的。

### 3.5 凭证预扫：`probes/staged_payload_scan.py`

出厂 `.gitleaks.toml` 里大半是放行表文本，所以一次绿 `findings=0` 有两种成因。这台仪器把同一份暂存
载荷扫三遍（出厂配置 / 剥掉全部放行表 / 内置规则集），再往同一棵树的一份副本里种一枚运行时生成的假
key，要求同一套 argv 从 0 变 1；假字面量本身绝不打印（`G_planted_literal_not_carried`）。
它跑两趟（index 模式在提交前、head 模式在提交后），取面时刻都早于或晚于本文件，所以：

- `runs/wave31-step5-staged_payload_scan.txt` 是 index 模式面。**本节不转录它的 `count=`**，
  因为那一格的总体是"写本文件之前"的暂存区；读者按该载档 `A_staged_roster` 那行原样读。
  本波有意留在工作树、不进 index 的只有 `runs/chain_stdout.log`（`.gitignore:61 *.log`，
  `git check-ignore -v` 实测命中该行）。
- commit 之后重跑 `--source head`（总体取自 `git diff-tree … HEAD`、字节取自 `git show HEAD:`），
  写 `runs/wave31-step5-committed_payload_scan.txt`，那把才是含本文件与全部载档的收口面；它按
  step-3/step-4 的既有处置归档在**下一枚**提交里（一台仪器扫不到自己之后落盘的字节）。
  已执行：`committed_payload_scan_exit=0`，`A_committed_roster count=112`（`:14`）与
  `git diff-tree --no-commit-id --name-only -r a70fb295f | wc -l` 实测的 112 枚对上，
  `B_blobs_from_HEAD copied=112 of=112`（`:15`），`C/D/E findings=0`（`:16-18`），
  `F_planted_control findings=1`（`:19`），`I` 臂仍读 `in_roster=False` 且
  `head=worktree=022bd5186aa69c81`（`:22`），收口 `arms=10 arms_passed=10 all_pass=True`（`:23`）。
  这一趟的总体是 `a70fb295f` 的字节，所以它扫到了上一趟扫不到的两样东西：它自己的载档
  `staged_payload_scan.txt`，与改过之后的 `face.md`；两者都读 0。而它自己的落盘字节又落到下一枚提交，
  这个递归是仪器定义的边界，不是缺陷：任何一趟的 `count=` 只能等于"该趟之前的那一代 payload"。
- step-4 §7.12/§7.13 的两条教训都已编成门：`D_strip_actually_changed_config`（配置真被改过，否则
  "allowlist-free"是空话）与 `A_roster_nonempty_and_covers_code`（空总体上的相等是空真）。
  本波另加 `I_restored_carrier_clean_and_absent`：被还原的 C131 step-0 载档必须是 HEAD 字节且
  **不得**出现在本枚 payload 里——它同时是那台老化工具的自检（§4.6）。

实测面（`runs/wave31-step5-staged_payload_scan.txt:14-22` 是 A–I 九臂，`:23` 是收口行，
`:24` 是它自己的退出行；扫描器版本在 `:4`）：

| 臂 | 读数 |
| --- | --- |
| `A_staged_roster` | `count=111 covers_code=True missing=[]` |
| `B_blobs_from_index` | `copied=111 of=111`（扫的是 index 字节） |
| `C_scan_shipped` | `findings=0 exit=0` |
| `D_scan_allowlist_free` | `findings=0 raw_lines=185 stripped_lines=15 lines_dropped=167 rules_kept=2 config_actually_changed=True` |
| `E_scan_builtin` | `findings=0 exit=0` |
| `F_planted_control` | `findings=1 files=['POSITIVE_CONTROL.txt']` |
| `G_planted_literal_not_printed` | `leaked=False` |
| `H_self_lint` | `codes={} n_codes=0 bytes=13930` |
| `I_restored_carrier_clean_and_absent` | `in_roster=False head=022bd5186aa69c81 worktree=022bd5186aa69c81 worktree_is_head=True head_exit=0` |
| 收口 | `prescan_face=arms=10 arms_passed=10 all_pass=True`，`staged_payload_scan_exit=0` |

命中的规则 id（`aws-access-token`）只出现在同一趟运行的控制台行里，那行落在未提交的
`runs/chain_stdout.log`；载档的 `out` 数组有意只存计数与文件名，所以这条不记作载档读数。
`D` 那行的 `rules_kept=2` 要说清总体：出厂配置只声明 2 条 `[[rules]]`，"剥掉放行表"这条臂能改的
是放行表而非规则数，所以它是对出厂 2 条规则的**加强**，不是第三套规则集；真正把内置规则集也压上来
的是 `E`（`findings=0`）。两臂各自的 0 都有 `F` 的 0→1 兜住，所以没有"空规则集上的绿"这一形状。

`A` 的 `count=111` 与枚内字节数还差一枚：这台仪器自己的载档是在扫完之后落盘的，所以它扫不到自己的
字节，本枚提交实际 112 个文件（`111 + wave31-step5-staged_payload_scan.txt`）。这条差值由
`--source head` 那一趟收口。同一趟还要收第二项差值：**本文件在预扫之后又改过**（§3.5 这张表、
§7.8、§8 的枚数），所以提交里的 `face.md` 字节不是被扫过的那一枚——四枚代码文件的字节则没有再动，
`git show :<path> | shasum` 与载档 `:5-13` 逐枚相同
（`b2a69d4d729057f2`、`66c255e4bba1e565`、`7b072d074d8d53ba`、`f0fdc863033b9c6f`）。
另有一条扫描外的操作要记：第二把 `git add docs/evidence/C128`（整目录）曾顺带扫进 wave-27/28 留在
`runs/` 的 38 枚 `.err`/`.step4f.rcs` stderr 旁档，我在提交前用 `git restore --staged` 退回它们，
暂存枚数从 150 回到 112。它们属于别的波次，本波没有权限也没有理由替它们决定入库与否。

九枚 `CODE` 行（`:5-13`）逐枚 `index==worktree`、`index_exit=0`，其中产品两枚
`fetchers.py=7b072d074d8d53ba`、`tmx.py=f0fdc863033b9c6f` 与 §3.2 的出厂字节一致，
即预扫之后产品码没再被动过。

## 4. 判定器伪影与本波发现

### 4.1 我造的红：`report_face` 拿身份比模型名

`judge.txt:9` 打 `post_chain this_steps_row_accepted=False`，而 `:10` 同一把载档打
`this_steps_row_judged=OFFLINE_ACCEPTED`。两行互相矛盾，且**第一行是错的**：
`report_face()` 把 `f"{PROVIDER}::{LANDED}"`（`tmx::EquitySearch`）与
`report["models"][*]["upstream_model"]`（`EquitySearch`）比，判定器那侧的键是身份、行名字段是模型名。
处置：修 harness（`w31s5_run_chain.py` 现用 `accepted = rows.get("OFFLINE_ACCEPTED", [])` 再比
`LANDED`），用新加的 `--stage retake --leg judge` 重取 → `judge_retake.txt:9` `=True`。
错那把留在 `judge.txt` 不覆盖——它是这条缺陷的原始面。同一函数里 `tmx_rows_total=24`（`:8`）与
`this_steps_row_judged`（`:10`）从一开始就是对的，所以这条缺陷的形态是"一行假阴性混在一堆真读数里"，
不是整台仪器瞎。

### 4.2 撤回的两条草稿都在仪器里留了面

见 §2.2 与 `consumed_control_rearm.txt:9-10`。值得单独记的是**撤回的理由形态**：守卫不是"没必要"，
是**不可达**（`inline_raise_lines=[]` + 帧在 client）；tamper 不是"不够狠"，是**归错层**（非 list 的
拒绝来自 client 层）。两条都属于"落地看起来对、证据说错了"的类别，所以修正的是声明与散文，
不是判定器。

### 4.3 pin 换代税：一枚声明让该片 8 枚已验收行全部重签

`pin_drift_pre_production.txt:7` `drifted_rows` 是那 8 枚（`CompanyFilings`…`PriceTargetConsensus`），
`rows_without_pins=16` 是 tmx 剩余 16 枚未绑定行。机制是 `fetchers.py` 与 `tmx.py` 同时属于这 8 枚的
`source`/`contract` 钉，所以任何一枚行的落地都要整片重生产。重生产之后 `stale_files=0`（`:5` of
rerun），全树 `slots=20744 stale=0`（§1）。同一条分母在两台仪器里口径不同，不要互相换算：
17 是**这片 shard 去重后的被钉路径数**，20744/589 是**全树收档钉位/去重路径**。

### 4.4 producer 的分母标签仍与它数的东西不符（#69，本波第二次实锤）

`producer.txt:125` `claims emitted=120 (40 PASS, 0 FAIL, 80 BLOCKED) … phases actually executed as
subprocesses=120`。80 条 BLOCKED 腿在逐相行里打 `<not run>`，即真跑过的是 40 条。step-4 §4.7 登记过
同一条，本波因为 tmx 有 BLOCKED 腿而第一次把它打在**同一行里同时有 40 与 120** 的形态——比 step-4
（federal_reserve 全绑定、0 BLOCKED）更能证伪"标签也许是对的"。修法不在本波：那是出厂脚本的显示层，
改它要付一片 pin 重签（`iter2_claims_producer.py` 是全树收档的钉对象）。

### 4.5 出厂仪器的一条臂**没有能力不同**：arm B 在消耗之后仍然绿

`incomplete_row_guard_check.py` 的 `B_landed_negative` 字面写的是"控制行的 verdict 不在 TRIPPABLE"。
本波的落地**不移动 verdict**（§3.1 的 `verdict_moved=False`），只把行移出残留总体，而该仪器只访问
残留——于是这条臂在一个不再被访问的行上永远绿。这不是本行的缺陷，是臂的判据写少了半个条件：
一条喂不进总体的控制臂， certify 的是字符串而不是判定。

处置两条：臂改为同时要求 `verdict not in TRIPPABLE` **与** judge 标签不是 `OFFLINE_ACCEPTED`
（即必须在残留里），并把 `CONTROL_NEGATIVE` 从 `tmx::EquitySearch` 重基到
`yfinance::BalanceSheet`（注释里带全部理由与本行的取证路径）。新子句不是"我说它有用"：
`consumed_control_rearm.txt:7` 的 `verdict_only_arm_b=True membership_arm_b=False` 就是在同一进程里
把旧标签喂进新谓词的证伪面。出厂那台仪器在本波之后 6/6 臂绿（`:59`），
`residue_axis_census.txt:70` 的 `B_landed_negative … is_residue=True PASS=True` 是新合取的当代面。

真实红面也留了档：`residue_axis_census_take1_consumed_control.txt:67`
`B_landed_negative tmx::EquitySearch verdict=DOCUMENT_REACHABLE PASS=False`，`:74` `arms=8
arms_passed=7 arms_failed=1`。要说清它的归因：那一代守卫还在，所以旧标签同时违反**两条**合取，
这把面不能单独算"新子句咬住"的证明——单独证明在 §3.1 的 A 臂里。

### 4.6 一台写死路径的仪器，会让上一波的收口面变老（本波撞实）

`plane2_dev_done_axis_census.py` 的输出常量是模块级的（`:46` `CARRIER = BUNDLE / "runs/
wave31-step0-plane2-axis-census.txt"`，`:486` 由 `_emit` 写），且不解析 argv。把它作为验证腿跑一次，
step-0 的终态载档就被**同长度、异摘要**地覆盖：`c131_carrier_restore.txt:4`
`head=022bd5186aa69c81 … archived_digest=f3933078a25a6a3e … head_bytes=14079 archived_bytes=14079
different=True`。两代各自对自己那份报告都是真值（`:5` `archived_aged=(350, 291, 59)
head_face=(350, 285, 65)`），所以这是"老化"而不是"损坏"——但 C131 `face.md:27` 钉的正是
`141 行 / 14079 B / 022bd5186aa69c81`，被老化的是别人已发布的面。

处置与证明：`probes/c131_carrier_restore.py`（11 臂，`:11 all_pass=True`，重跑第二次仍 11/11）。
它把老化代**移到本波**（`runs/wave31-step5-c131_plane2_face_aged_by_this_chain.txt`，因为那是本链
那一代的面），再把 HEAD 字节写回原路径，并核三件事：
`:8` `E_pin_priced digests_named=['022bd5186aa69c81'] numbers_named=[141, 14079]
head_digest=022bd5186aa69c81`（C131 那句散文的三个数都是 HEAD 的，所以还原是让散文重新为真）；
`:7` `D_no_loss archived_lines=141 absent_from_this_wave_carrier=0`（老化代每一行都在本波自己的腿
载档里， `_emit` 先写文件再回显同一 `lines`，所以删掉工作树副本不丢任何读数）；
`:6` `C_census_probe_untouched disk=e274897d3e7d72eb head=e274897d3e7d72eb moved=False`——
仪器本体一字节没动，这也正是 C131 face.md 钉的那枚摘要（那枚字节约束就是"别改它"的理由）。
`:9` `F_restored … equals_head=True clean=True`：还原后 `git status --porcelain` 对该路径为空，
所以它不会进入本枚 payload（由 §3.5 的 `I` 臂独立复核）。

给下一波的规矩：**凡把写死输出路径的仪器当验证腿跑，链路末尾必须跟一台还原仪器**（或把腿改成
`git show` 读快照、不落工作树）。本波的 `VERIFICATION_LEGS` 里没有这一条，是我把 plane-2 腿加进
roster 时没读 `_emit` 的后果——见 §7.4。

## 5. 对 step-4 谓据的验证

step-4 §6 为"下一枚落地"发布的谓据是按 `yfinance::KeyMetrics` 写的，本波落地的是另一枚行，
所以必须分行独立地核，不能整表打勾：

| step-4 谓据 | 预测 | 实测 | 结论 |
| --- | --- | --- | --- |
| plane 1 | 290 → 291 | 291 | 命中，但由不同行达成（该谓据的行无关形式"任一落地 +1"成立） |
| `NEED_DECLARE` | 13 → 12 | 12 | 命中，且机制属于本行（`readiness_census.txt:7`） |
| `retarget_fetcher_output` | 1 → 0 | **仍 1**（`yfinance::KeyMetrics`） | 未测：本波没碰那枚行 |
| `rows_without_module` | 37 → 36 | **仍 37**（`:75`），`rows_with_module` 23 → 22 | 谓据错在类：那格假设"新建首模块"，本行所在模块已存在 |

step-4 §6 列的失效条件逐条核过：`accepted + residue = 291 + 59 = 350`
（`seam_reachability_census.txt:40`）；身份集仍是 `5168e1e723999312`（`readiness_census.txt:10`）；
`F_witnessable_today` 仍 `count=0` 且对照臂照旧读非零（`residue_axis_census.txt:74`）；
"其他 provider 的 pin 被本波之外的编辑换掉"→ 未发生，全树 `stale=0`
（`receipt_pin_consistency.txt:8`）。

step-4 §6 给 `add_code_carrying_guard` 那条路立的先决条件（"先用 C128 那套四轴仪器逐枚定价 12 枚，
而不是假设本行机制可复用"）**本波只对 1 枚兑现**——就是我落地的那枚 tmx 行。剩余 11 枚（全在
yfinance）仍未逐枚定价，且本波的逐行面给了它们一条不利读数：`seam=SEAM_NEVER_USED`、
`mechanism=engine_transport`、`witness_module=False`、`fake_ok=False`，与 tmx 那枚的
`seam=SEAM_REACHABLE` + 已有模块 + `fake_ok=True` 不同形。所以"照搬 step-5 配方"对那 11 枚不成立。

## 6. 残留 59 行的行动图与下一波谓据

`residue_axis_census.txt:77`：`actions={'add_code_carrying_guard': 11, 'implement_missing_model': 19,
'ledger_ruled_out': 11, 'new_provider_package': 2, 'retarget_fetcher_output': 1,
'shard_entry_needs_harness': 15}`（和 59，`:66`）。按 provider：`readiness_census.txt:9`
`tmx NEED_BINDING 16`、`yfinance NEED_DECLARE 12 + NEED_SHARD_ENTRY 15 + NEED_BINDING 2`、
`seeking_alpha 3 / wsj 3 / biztoc 1 / stockgrid 1 / cboe 2 / econdb 2 / government_us 1 / intrinio 1`。

下一波候选，按"本仓现有机制能不能服务"排（这是可达性轴，不是代价轴）：

1. **tmx 的 16 枚 `implement_missing_model`**（`judge.txt:6` 逐名列出
   `AvailableIndices … TreasuryPrices`，逐行面 `seam=SEAM_REACHABLE mechanism=client_seam
   witness_module=True fake_ok=True declared=False`）。这是唯一一批"配方已在本仓证过两次"的
   （step-1/2 落过 4 枚，本波第 5 枚）：先写实现与模型类，再补 `row_model` 与见证项。
   谓据：每落一枚，plane 1 +1、残留 −1、`implement_missing_model` −1、tmx 已验收行 8→9…、
   `rows_without_module` 不动、`rows_with_module` −1、`SEAM_REACHABLE` 残留 −1。
   失效条件：`accepted + residue ≠ 350`；身份集不再是 `5168e1e723999312`；该行的 `fake_ok`
   在逐枚定价时读 False（则本配方不可用，需要先修 harness 面）；`F_witnessable_today` 变 >0
   （harness 上来了新机制，批次选择要重算）。
   失败读法：任一相 `exit≠0` 先查 `row_model` 与发布键集关系（service 腿要求键 ⊆
   `row_model.model_fields`）；judge 若打 `claim_claim_hash_drift`/receipt 漂移，那是 §4.3 的换代税，
   唯一处置是整片 shard 重生产 + 重签，不是把 pin 追改到磁盘字节。
2. **`yfinance::KeyMetrics`**（唯一一枚 `retarget_fetcher_output`）：门不是映射裁决，而是
   `seam=SEAM_NEVER_USED` + yfinance 无见证模块，所以 retarget 之后还要为该 provider 首建模块。
   谓据：若做，`rows_without_module` 才真的 37→36。
3. **`shard_entry_needs_harness` 15 枚**：`F_witnessable_today=0` 已说明这 15 枚不是纯登记问题；
   先决条件是出厂 fake 能服务它们要的调用形，属于 harness 面的工程，不属于逐行落地。

复算配方（下一条落地前自己跑一遍，不要引用本节数字）：
`PYTHONPATH=. <py313> scripts/quality/iter2_claims_producer.py --provider <provider> --output
docs/evidence/C101/<provider>/claims.json` → recorder 覆写该 provider 全部已见证身份（roster 从
"五相全 PASS"推）→ `iter2_claims_merge.py` → `iter2_model_evidence.py --inventory <清单 csv>
--claims docs/evidence/C101/claims.merged.json --output docs/evidence/C101/judge/claims.merged.report.json`。
**并把 `c131_carrier_restore.py`（或等价还原）挂在链路末尾**（§4.6）。

## 7. 我错的部分（原文留在档，不改写）

1. **`report_face` 那条自相矛盾的 `this_steps_row_accepted=False`**（§4.1）。它是本波第一次让
   `judge.txt` 里两行互相打脸，而我没有先怀疑自己比错了字段。处置是修 harness + `retake` 新面，
   错面留在原路径。
2. **我给 `consumed_control_rearm.py` 写过一次多余的 `copy.deepcopy`**。出厂
   `_apply_tamper` 自己深拷贝（`iter2_claims_probe_runner.py:429`），我那层包裹是死码；现在这条
   主张不再靠我读源码，而由 `D_tamper_bites` 的 `pristine_after=('SERVED', 'rows=2')` 打出来。
3. **`inline_raises` 第一版含一枚死推导式**（`r.lineno > member.end_lineno`，与它要筛的区间互补，
   恒为空）和一条不存在的 `# noqa: B027` 编码。两处都在自检（`H_self_lint`）里被 ruff 拒掉。
4. **把 plane-2 腿加进 `VERIFICATION_LEGS` 时我没读它的 `_emit`**，于是本波第一手面老化了 C131
   step-0 的载档（§4.6）。这台仪器 `face.md:27` 明写钉那枚摘要，而我在添加腿时只核了"它读什么"，
   没核"它写什么、写到谁的路径上"。规矩由此改写进 §6 的复算配方。
5. **还原仪器第一版是一次性的**：门 `A_aged_not_head` 读的是当时的**工作树**状态，所以还原成功后
   重跑必然变红。修法是把老化代移到本波、让每条门都比"归档代 vs HEAD"，然后用第二次运行
   （仍 11/11）证明它可重跑。这类缺陷的形状值得单列：仪器的总体一旦写成"此刻的工作树"，
   它的绿就只属于一次。
6. **`consumed_control_rearm.py` 早期版本从 `report.get("identity_set_sha256")` 取身份摘要**——
   判定器报告里没有这个顶层键（真路径是
   `report["inventory"]["expected_identity_set_sha256"]`），所以它会打 `n/a` 并让
   `identity_set_is_pin` 变成"None != 常量"式的假红或空真。现在两条都比：报告里的值，和出厂常量
   `iter2_model_evidence.EXPECTED_IDENTITY_SHA256`。
7. **一次 shell 伪影**：我用 `… | tail -3` 之后读 `${PIPESTATUS[0]}` 取重跑退出码，zsh 里没有这个
   变量（只有小写 `pipestatus`），于是 `rerun_exit=` 打了空值。这条与 §4.5 的"面要有自己的退出行"
   同族：读不到的数不要写成有数。本波所有 exit 读数因此改为不经管道直接取，或由载档自己的
   `<name>_exit=` 行承载。
8. **一批无法归属的 receipt 重复代**。暂存前 `git status` 在 `docs/evidence/C101/receipts/tmx/`
   下露出 8 枚 `tmx__<Model>.review.json`，与本 provider 的分阶段载档混在同一片目录里。核下来是
   一枚早代的 recorder 落盘：它们的 mtime 是 17:55:46（本地），而权威那 8 枚在
   `docs/evidence/C101/primary-reviews/` 的 mtime 是 18:23:52，8 对 8 逐枚摘要都不同；判定链读的
   是后者——`claims.merged.json` 里 8 条 tmx review 的 `receipt.path` 全部指向
   `primary-reviews/…`，逐枚 `pin==disk` 全 True，而出厂 recorder 的
   `--receipt-dir` 默认就是 `docs/evidence/C101/primary-reviews`
   （`scripts/quality/iter2_primary_review_recorder.py:202`）。合并器的分片总体是
   `docs/evidence/C101/*/claims.reviewed.json`（`scripts/quality/iter2_claims_merge.py:27`），
   压根不读 receipts 目录，所以这 8 枚是**没有读者的重复代**：全树两个总体各扫一遍字符串
   `receipts/tmx/tmx__`——ripgrep（遵循 `.gitignore`）与 `grep -rl --exclude-dir=.git`（连被忽略的
   文件也扫）——在本节写下之前都命中 0 文件，此后唯一的命中就是这段记载自己。
   **我指不出是哪一次运行写的**——本波 `runs/`
   里没有任何载档的 argv 带 `--receipt-dir`（grep `receipt-dir|receipt_dir` 命中 0），那是一把我
   把 recorder 手动敲在链路外的 argv，写盘进了 provider 的 receipts 目录。处置是不删不移祸：
   把 8 枚原字节搬进本波
   `artifacts/superseded_receipts/`（逐枚搬运前后摘要相同，见下表），让 `receipts/tmx/` 回到
   "只有五相载档"这一形状，同时不销毁那一代。留下的教训与 §4.6 同族，只是方向相反：§4.6 是
   我的仪器写脏了**别人**的载档，这一条是我链路外的手敲 argv 写脏了**自己的**证据目录，而且
   因为没走载档，连 argv 都没留下——凡是绕开链路手敲的写盘命令，都要当作一代无主证据来处置。

   | 模型 | 早代（搬到 `artifacts/superseded_receipts/`） | 权威 `primary-reviews/` |
   | --- | --- | --- |
   | CompanyFilings | `23fcc98e368b2695` | `1f29605839e95c9a` |
   | EquityHistorical | `540ad4ec313c543a` | `a32b8d765fd66808` |
   | EquityInfo | `615b94cc03773c4d` | `dab4b2c5b3e6c806` |
   | EquityQuote | `c3bbbd169ab57133` | `3814e62c6f3a5518` |
   | EquitySearch | `a941b6b410dacfe4` | `ede4161f2f6c4d74` |
   | HistoricalDividends | `b96d03fe9b0d8e0a` | `6f6f7bbe31d07b8c` |
   | IndexConstituents | `153e082d77e68f2a` | `7da204cda6dba512` |
   | PriceTargetConsensus | `a4dff51643a63634` | `1e595f7de795d0c3` |

## 8. 载档清单

`docs/evidence/C134/wave31-step5-tmx-equity-search-20261018/`

- `probes/w31s5_run_chain.py` —— 落地与验证链驱动；`--stage pin-drift` 复取漂移表，
  `--stage retake --leg <name>` 用同一份 `VERIFICATION_LEGS` 重取单腿（§4.1 那条修就是靠它重取的）
- `probes/consumed_control_rearm.py` —— 4 臂 re-arm 面（§3.1）
- `probes/landed_bytes_face.py` —— 10 臂三代字节面（§3.2）
- `probes/c131_carrier_restore.py` —— 11 臂还原与无损面（§4.6），可重跑
- `probes/staged_payload_scan.py` —— 凭证预扫，index 与 `--source head` 两种总体（§3.5）
- `artifacts/superseded_receipts/` —— 8 枚无主早代 tmx receipt（§7.8），逐枚摘要见该节表列
- `runs/`（36 把 `wave31-step5-` 前缀的载档，前缀在下列名字里一律省略；枚数由 `ls *.txt` 与
  名表逐名对差实测，不是我锚 prose 的）：

  链路当代绿面：`producer`、`recorder`、`merge_take1`、`merge_take2`、`judge`、`judge_retake`、
  `pin_drift_pre_production`、`pin_drift_pre_production_rerun`、`consumed_control_rearm`、
  `incomplete_row_guard_check`、`readiness_census`、`residue_axis_census`、`seam_reachability_census`、
  `receipt_pin_consistency`、`plane2_dev_done_axis_census`、`batch5_fence_dry_census`、`ruff_check`、
  `ruff_format`、`mypy`、`bandit`、`pytest_tmx_and_pin_planes`、`pytest_collect_denominator`、
  `landed_bytes_face`、`c131_carrier_restore`、`c131_plane2_face_aged_by_this_chain`、
  `staged_payload_scan`、`committed_payload_scan`（27 把）。

  被取代的守卫代/旧常量代红面（不覆盖、不删）：`producer_take1_withdrawn_guard`、
  `recorder_take1_withdrawn_guard`、`merge_take1_withdrawn_guard`、`judge_take1_withdrawn_guard`、
  `consumed_control_rearm_take1_withdrawn_guard`、`incomplete_row_guard_check_take1_withdrawn_guard`、
  `plane2_dev_done_axis_census_take1_withdrawn_guard`、
  `residue_axis_census_take1_withdrawn_guard`、`residue_axis_census_take1_consumed_control`
  （9 把，`ls` 实测）。

  退出面分类（口径是出厂 `evidence_traceability.py:93` 的 `EXIT_MARKER`，不是我抄的一份正则；
  读数由这段一次性 inline 测量产出，跑在 py313 上，配方原样贴在这里以便复算，它没有独立载档）：

  ```python
  import pathlib, sys
  sys.path.insert(0, "scripts/quality")
  from evidence_traceability import EXIT_MARKER
  R = pathlib.Path("docs/evidence/C134/wave31-step5-tmx-equity-search-20261018/runs")
  carriers = sorted(p for p in R.iterdir() if p.suffix == ".txt")
  with_exit = [p for p in carriers if EXIT_MARKER.search(p.read_text(encoding="utf-8", errors="replace"))]
  print(f"carriers={len(carriers)} with_exit={len(with_exit)} without_exit={len(carriers)-len(with_exit)}")
  ```

  同一把尺跑过两代总体：index 模式那趟之后实测 `carriers=35 with_exit=30 without_exit=5`；
  `--source head` 的收口载档落盘后复跑同一段代码实测 `carriers=36 with_exit=31 without_exit=5`。
  不含退出行的两代都是同样这 5 把——
  `incomplete_row_guard_check_take1_withdrawn_guard`、`judge_take1_withdrawn_guard`、
  `merge_take1_withdrawn_guard`、`residue_axis_census_take1_consumed_control`、
  `residue_axis_census_take1_withdrawn_guard`。这 5 把是 shell 直接重定向的原始 stdout，
  取面时没经过 `carrier()`，因此既没有 argv 头也没有退出行；它们定价的是已被取代的工作树代，
  **不可再现**，与 step-4 §7.5 同一分类。出厂规则只对 gate log 索求 exit 面
  （`evidence_traceability.py:124`，`is_gate_log` 见 `:173`），这些载档不属于该类，所以这不是待修项，
  但引用它们时只能引正文读数、不能引退出码。

  非 0 退出实测 1 把：`batch5_fence_dry_census_exit=1`（设计内拒绝，§3.3）。

**不提交**（有意留在工作树）：`runs/chain_stdout.log` —— 被 `.gitignore:61` 的 `*.log` 覆盖
（`git check-ignore -v` 命中该行），它是链路的控制台回声。两趟扫描的 `A–I` 读数与收口行都在载档里，
但它另有一类行是载档没有的：`run_gitleaks()` 打到 stdout 的 4 行 `SCAN …`（含命中的规则 id 与
gitleaks 自己的 stderr 尾行，见 §3.5 表下那条说明）。所以"回声 ⊂ 载档"这句对这台仪器不成立，
本节只在 §3.5 引用它的规则 id，并标明那条不可复算。
