# wave-31 step 4 —— `federal_reserve::FomcDocuments` retarget 落地面（本仓第 290 枚离线验收行）

日期 2026-10-18（UTC 记录见各载档 `generated_at_utc` 与合并对象时间戳）。本节所有数字都来自 `runs/`
里载档的原文行，引用形式 `runs/<载档>.txt:<行>`；每条载档自己带 argv、`stderr_bytes` 与
`<name>_exit=<数字>` 收尾行。本轮无子代理（预算 30/30 已用尽），无 push，无 live 腿：
`OPENDATA_ALLOW_LIVE_E2E` 未设置，五相里每一腿的 socket 都由构造封死。

## 1. 结论面

| 量 | 落地前 | 落地后 | 载档 |
| --- | --- | --- | --- |
| plane 1 `offline_acceptance_count` | 289 | **290** / 350 | `runs/wave31-step4-judge.txt:5`；落地那一格在 `runs/wave31-step4-fomc_documents_retarget_prevalidation.txt:10` |
| `identity_set_sha256`（前 16） | `5168e1e723999312` | 同一枚，未动 | `runs/wave31-step4-judge.txt:5`，另有 `runs/wave31-step4-readiness_census.txt:10` `expected_match=True` |
| `claim_count` | 1565 | 1565（未动） | `runs/wave31-step4-judge.txt:5`、`runs/wave31-step4-merge_take1.txt:32` |
| `primary_review_count` | 289 | **290** | `runs/wave31-step4-judge.txt:5` |
| `INCOMPLETE` | 24 | **23** | `runs/wave31-step4-judge.txt:5` |
| `UNREVIEWED` | 37 | 37（未动） | `runs/wave31-step4-judge.txt:5` |
| federal_reserve 已验收行 | 12 | **13**（13/13，该 provider 残留清零） | `runs/wave31-step4-judge.txt:6`、`runs/wave31-step4-producer.txt:70` |
| 残留总数 | 61 | **60** = NEED_BINDING 32 + NEED_DECLARE 13 + NEED_SHARD_ENTRY 15 | `runs/wave31-step4-residue_axis_census.txt:78`、`runs/wave31-step4-readiness_census.txt:7` |
| 行动图 `retarget_fetcher_output` | 2 | **1**（只剩 `yfinance::KeyMetrics`） | `runs/wave31-step4-residue_axis_census.txt:66`（计数）与 `:60`（那一行逐轴面） |
| 第二轴 `ROW_CLASS_NOT_CONTRACT` | 1 | **0**（该枚已从 INCOMPLETE 总体消失） | `runs/wave31-step4-incomplete_row_guard_check.txt:63` `verdicts={'NO_CODE_SITE': 1, 'UNBOUND': 22}`；同档全文 `grep federal_reserve` 命中 **0** |
| plane 2 `declared/dev_done/missing_model_rows` | 17/17/19 | **本波未重取**，见 §1.3 | — |

产品码字节面由 `probes/landed_bytes_face.py` 现测（10 臂，`landed_bytes_face.txt:19`
`arms=10 arms_passed=10 all_pass=True`）。四枚落地文件逐枚在 `:4-7`，每枚同时打磁盘字节、
`git show HEAD:` 字节与 producer 写进 shard 的 `after_sha256`：

| 文件 | disk | HEAD | shard pin | 载档 |
| --- | --- | --- | --- | --- |
| `opendata/data/providers/federal_reserve/models/fomc_documents.py` | `3a03c7d4f79a33be` | `5e82b0586d336098` | 追平 | `:4` |
| `scripts/quality/iter2_witnesses/_parts/federal_reserve/rows_a.py` | `e9c4edc8405c7306` | `09c56380b9de5764` | 追平 | `:5` |
| `scripts/quality/iter2_witnesses/federal_reserve.py` | `be380dd643f747de` | `a6f85520279d113b` | 追平 | `:6` |
| `tests/test_federal_reserve_iter2_models.py` | `52218ac2ad608800` | `501b927b5b92e69d` | 不在 pin 总体 | `:7` |

`:8` 是本波**没有**编辑的控制臂 `scripts/quality/iter2_claims_producer.py`
（`e2fdc57e0b30ee48`，disk==head，`H_control_untouched_file` 靠它证明 `moved` 谓词能读 False）。
三枚进 shard 的 `pin_matches_disk=True`（`:12 D_pinned_agree_with_disk agreed=3 of=3`），第四枚测试
文件由 `:11 C_pin_membership` 明列为 unpinned，所以它不进换代税；分母在
`:13 E_denominator this_probe=47 drift_carrier=47 carrier_stale_files=0`。这两个 47 是同一片 shard 的
**去重被钉路径数**，来自两处不同代码：本机 `pinned_paths()` 返回 `path -> {摘要}` 的字典取长度，
驱动 `pin_drift_face()` 打的是 `len(all_paths)`（`w31s4_run_chain.py:235`）。它与全树面
`runs/wave31-step4-receipt_pin_consistency.txt:8` 的 `slots=20692 … distinct_pinned_paths=588`
是两套总体（一片 provider vs 全树；去重路径 vs 钉位），不要互相换算。

这一整段是对本节的**重基**。我先前在这里写的是两枚凭记忆打出来的摘要，而且两枚都错
（`cc1f24a6d8e94e7b` 应为 `3a03c7d4f79a33be`，`2a3e4a58ac3c6f39` 应为 `be380dd643f747de`），
原文与处置记在 §7.9，不改写。

### 1.1 本波实际执行的五相

producer 对 federal_reserve 全部 13 枚 inventory 行重跑五相：
`runs/wave31-step4-producer.txt:4` 是总体行 `provider federal_reserve: 13 inventory row(s) of 350 total`，
`:15-19` 是新行 `FomcDocuments` 的 implementation / direct / service / contract / counterfact 五腿，
逐腿 `exit=0 status=PASS`，并把每腿的完整 argv 打在同一行里（都带
`--evidence-root docs/evidence/C101`）。`:70` 是分母行：
`claims emitted=65 (65 PASS, 0 FAIL, 0 BLOCKED) = 13 row(s) x 5 phases`，同一行还打了
`catalog-resolvable bound identities this wave=318/350` 与 `provider rows covered=13 of
provider inventory rows=13`。本 provider **没有** BLOCKED 腿——13 枚全部绑定且全部有见证条目。
`phases actually executed as subprocesses=65` 这个标签是失配的（它数的是 claim 数），见 §4.7。

主审记录：`runs/wave31-step4-recorder.txt:4` `re-signing: dropped 13 earlier review(s)`，
`:5-17` 十三行 `REVIEW_SIGNED`，`:18` `signed=13 refused=0 reviews_total=13`。
合并：`runs/wave31-step4-merge_take1.txt:4` `shards merged=27`、`:32`
`claims=1565 identities=313 reviews=290 accepted_identities=290`；
`runs/wave31-step4-merge_take2.txt:34` 两次取的稳定载荷 `take1=a49dcd93b88ecdc5
take2=a49dcd93b88ecdc5 identical=True`。判定：`runs/wave31-step4-judge.txt:4`
`350 identities, 290 offline accepted`，`:8` `post_chain this_steps_row_judged=OFFLINE_ACCEPTED`。

### 1.2 分母腿

`runs/wave31-step4-pytest_federal_reserve_and_pin_planes.txt:17` `740 passed, 179 skipped`；
`runs/wave31-step4-pytest_collect_denominator.txt:11` `collected 919 items`，740 + 179 = 919 闭合，
没有样本消失进 skip。919 的逐模块拆法是从同一把载档的树里数出来的（不来自控制台）：
`test_provider_model_contracts.py` 812、`test_iter2_model_evidence.py` 52、
`test_federal_reserve_iter2_models.py` 37、`test_federal_reserve_clean_documents.py` 12、
`test_provider_domains.py` 6，相加 919。**这 919 与 step-3 的 879 不是同一个总体**（那五枚文件是
government_us 的 roster），所以这里不写差值——规矩是 step-3 §7.3 自己立的：没有同总体的前代面就
不许写差值。本行的落地为什么不动这五枚文件的 collected，也有可核的机制：
`tests/test_provider_model_contracts.py:220-226` 的 `MODELS` 总体来自
`:96 engine_declared_models()`（声明式 ModelSpec）加 probe 声明，而 `FomcDocuments` 是手写 fetcher，
不进入 `MODELS`；载档里出现的两枚 `[FomcDocuments]` 参数化 id
（`runs/wave31-step4-pytest_collect_denominator.txt:24` 与 `:39`）属于
`test_federal_reserve_iter2_models.py` 的既有逐模型表，本波只改了那表里 `FomcDocuments` 一格的
字段拼写，条目数未变。

### 1.3 plane 2：本波没测，也不该假装测过

step-3 的链路里有一把 plane-2 面（`declared/dev_done/missing_model_rows = 17/17/19`）。本波**没有**
重取它，理由分两层：机制上，plane 2 的总体是声明式 ModelSpec 行，本行是手写 fetcher，结构上不在
总体里；但"结构上不在"这句我本波没有重新量化（没有重跑那台仪器），所以它只能算一条机制推理，
不是面。要把它升成面，下一波应把 step-3 §4.5 那台 plane-2 仪器按当代总体重跑一次并按固定路径归档
（那台仪器写死 `docs/evidence/C131/.../wave31-step0-plane2-axis-census.txt`，覆盖它会老化 step-0 的
历史面——step-3 §7.5 已经为此错过两次手）。

## 2. 产品码与见证项

`opendata/data/providers/federal_reserve/models/fomc_documents.py`（本波 diff：35 行）做四件事，
其中**没有一件是加新守卫**：

- 导入换模块：`FederalReserveFomcDocument` 现在从
  `opendata.data.models.federal_reserve_clean_documents` 导入（`:8-10` 区），不再从
  `federal_reserve_macro`。这就是 retarget 本体。
- `:88` `row_model: ClassVar[type[ContractModel]] = FederalReserveFomcDocument`，配 `:14`
  `TYPE_CHECKING` 下的 `ContractModel` 导入。注释里写明 `federal_reserve_macro` 发布同名 7 字段
  替身，而这里的五枚是 `domains.yaml` 声明的发布列。
- `transform_data`（`:104`）的构造体从 7 个关键字换成 4 个：
  `date=document_date`、`doc_type=…`、`doc_format=…`、`url=…`；`release_date` 是契约里的可选列，
  索引页不陈述它，所以**不构造**、保持未设（`runs/wave31-step4-fomc_documents_retarget_prevalidation.txt:9`
  的 `added_by_retarget=[date, doc_format, doc_type, release_date]` 是"契约列集合"，不是"构造参数
  列表"，别把它读成四个都传了）。排序键随之换成 `item.date`。
- 删掉 `_meeting_range()`：`meeting_range`、`title`、`year`、`document_type`、`format`、
  `document_date` 这六枚替身字段不再出现在行里
  （`dropped_by_retarget=[document_date,document_type,format,meeting_range,title,year]`，同载档 `:9`），
  并且那一臂还定价了反向不可逆：`macro_only_field_reaccepted=REFUSED`（`contract_extra_policy=forbid`），
  所以"顺手把 macro 字段留着"这条路是被契约拒的，不是被我拒的。

被见证的那枚拒绝码 `FEDERAL_RESERVE_BAD_FOMC_DOCUMENT_DATE` 在 `:129`，**它是既有码**（本波 diff
没新增任何 raise 站点）。本波做的事是让这枚既有守卫进入 shard 的 tamper 面。

见证条目 `scripts/quality/iter2_witnesses/_parts/federal_reserve/rows_a.py:677-688`：
`client_seam=_ITER2_CLIENT_SEAM`（`:452`，指向本 provider 自己的
`opendata.data.providers.federal_reserve._iter2_client`）、
`fixture_document=_fed_fomc_documents_envelope`（一段 4 行 HTML 索引切片的信封，
`content_type="text/html"`）、`query={"year": 2024}`、恰好一枚 tamper
`{op: set, path: [responses, 0, body, 2], value: _fomc_statement_link("20241340"), column: "date"}`
——把 statement 锚点 URL 里的 8 位文档日期改成 `20241340`（月 13），日期解析因此拒绝；
`refusal_class="FederalReserveSourceError"`、`refusal_code=FEDERAL_RESERVE_BAD_FOMC_DOCUMENT_DATE`。
切片里第 3 行才是被改写的那枚锚点（`:683` 的 `body, 2` 是 0 起下标）。

见证模块 `scripts/quality/iter2_witnesses/federal_reserve.py:34` 从"七枚 further bound rows"改成
**八枚**，`:41-43` 换掉了"只有 `FomcDocuments` 未见证"那段旧裁决，并写明它是这片 shard 里唯一吃
`text/html` 载体的行、其 tamper 腐蚀的是 URL 里的日期而不是某个单元格。

`tests/test_federal_reserve_iter2_models.py`（本波 diff：50 行，864 → 874 行）是 handoff 判定的
重基：`"FomcDocuments"` 那格从 `("document_date", ("url",))` 换成
`("date", ("date", "doc_type", "doc_format", "url"))`，并把"用手提的类名去查契约表"这类比较换成
两条对活注册表的断言——`:391` `assert contracts_by_name[shipped.__name__] is shipped, domain`
（同名≠同类）与 `:398` `assert set(semantic_fields) <= set(shipped.model_fields), domain`。
退役掉的那次比较（HEAD 字节 `:388` `assert set(semantic_fields) <= contract_fields`）我在 §3.2 定价。

## 3. 反例与控制臂（六台仪器、50 臂，全部 `arms_passed=arms`）

`arms=` 是从六把载档原文里取的，不是我汇总的：6 + 7 + 6 + 12 + 10 + 9 = 50。§3.1-3.3 是落地前、代价、
落地后三台（19 臂），第四台 `take1_lint_preexistence.py`（12 臂）在 §4.5，第五台
`landed_bytes_face.py`（10 臂）在 §3.4，第六台 `staged_payload_scan.py`（9 臂）在 §3.5。

总体的边界要说清：**六台 = 本波 `probes/` 里会打 `arms=` 的那六枚**（目录里第七枚 `.py` 是驱动
`w31s4_run_chain.py`，它不造臂）。`runs/` 里打 `arms=` 的面其实有 10 把、共 73 臂，多出的四把不在这
六台之内：`fomc_retarget_cost_face_take1_REFUTED`（6 臂，是被判否的 take 1，§4.4）与三把从 C128 借来
现跑的探针 `incomplete_row_guard_check`（6）、`receipt_pin_consistency`（3）、`residue_axis_census`（8）
——它们的 argv 头部第二行各自写着 `docs/evidence/C128/plane1-residue-readiness-20261017/probes/…`，
所以字节归属不在本波目录，本波的 FORMAT leg 也就管不到它们。

### 3.1 落地前：`probes/fomc_documents_retarget_prevalidation.py`（6 臂）

`runs/wave31-step4-fomc_documents_retarget_prevalidation.txt:12` `arms=6 arms_passed=6`、`:13 exit=0`。

1. `:5` `A_contract_face`：域解析到 `federal_reserve_clean_documents.FederalReserveFomcDocument`，
   `contract_fields=5 contract_required=4 time_field=date natural_key=(date,doc_type,doc_format,url)
   filter_dims=(doc_type,doc_format) semantic_missing=[]`。
2. `:6` `B_emitted_face`：同一时刻实发类型是 `federal_reserve_macro` 那一枚，`emitted_fields=7`，
   `classifier_is_contract=False classifier_look_alike=True`——"同名不同模块"在这里是量出来的。
3. `:7` `C_verdict_prediction`：`now=ROW_CLASS_NOT_CONTRACT → sim_after_retarget=DOCUMENT_REACHABLE`，
   `readings_differ=True`，`flipped_keys=declared,is_contract,declared_is_contract`。
4. `:8` `D_guard_arms`：`shipped_declares_nothing=True`，而伪造两个读者各回到自己的一侧
   （`forged_macro_holds=False forged_contract_holds=True`），`guard_declaring_fetchers=12`、
   `this_row_in_declaring=False`。
5. `:9` `E_row_buildability`（见 §2）。
6. `:10` `F_population`：`report_rows=350 judged=350 incomplete=24 accepted=289
   this_row_status=INCOMPLETE`。**这就是 §1 的落地前一格**，为什么不能用链路自己的 `pre_chain`，
   见 §4.3。

`:11` `RULING=published_face=5_contract_fields emitted_face=7 verdict_move=…
reason=the_service_serializes_against(contract_model(domain))`。

### 3.2 代价面：`probes/fomc_retarget_cost_face.py` take 2（7 臂）

`runs/wave31-step4-fomc_retarget_cost_face.txt:19` `arms=7 arms_passed=7`、`:20 exit=0`。这台仪器回答
"retarget 要赔几枚既有测试"，全程只在内存里换 `_MODELS`，`finally` 复原，磁盘零补丁。

1. `:11` `A_guard_today`：逐模型守卫表那台测试**不**因本行变红（`exit_ok=True`，
   `this_row_declaring=False` 是落地前读数）。
2. `:12` `B_guard_macro`：`shipped_row_model=None`、实发类是 macro 那一枚，
   `forged_look_alike_predicate=False`。这一臂的两个布尔容易被读成自相矛盾，它们的真实谓词是
   探针源码 `:123` `emitted_is_contract = isinstance(emitted, type) and issubclass(emitted, ContractModel)`
   与 `:124` `emitted_is_not_contract = emitted is not contract`——即"属于契约家族、但不是 shard 绑定的
   那一枚"。字段名起得糟，读数不糟；我按源码把这两行引在这里，而不是替它们辩解。
3. `:13` `B2_forge_control`：把 `row_model` 伪成 `list` 时， shipped 谓词同样读 `False`，
   所以"谓词变红"本身分不开替身与非类，真正承重的是 `:123` 的类门。这条臂的存在理由见 §4.4。
4. `:14` `C_guard_contract`：伪成契约类时谓词读 `True`——控制臂能反向。
5. `:15` `D_handoff_control`：落地前 handoff 测试对 macro 条目是绿的（`exit_ok=True`）。
6. `:16` `E_handoff_after_swap`：只把表条目搬到契约类、不动测试，它立刻红在
   `assert set(semantic_fields) <= contract_fields`（**HEAD 字节**的 `:388`）。所以 retarget 的真实
   代价是**一**枚测试重基，不是两枚：`:18` `COST_RULING=handoff_test_red_after_retarget=True
   guard_test_red_after_retarget=False`。
7. `:17` `F_staleness_attributed`：`handoff_entry_names=[document_date,document_type,format,url]`
   里三枚在契约上不存在（`stale_in_handoff=[document_date,document_type,format]`），而
   `published_missing_from_contract=[]`；`handoff_file_tracked_rc=1` 说明那枚 C100 handoff 工件不在
   跟踪里，因此它的拼写是历史读数而非可解析的键。

### 3.3 落地后：`probes/handoff_judge_counterfact.py`（6 臂）

重基之后的判定器必须**每枚它能拒的失败都咬一口**，否则"绿"没有含义。
`runs/wave31-step4-handoff_judge_counterfact.txt:16` `arms=6 arms_passed=6`、`:17 exit=0`：

- `:9` `A_shipped_green` 当代绿。
- `:10` `B_look_alike_reddens`：把 `row_model` 伪回 macro 后红在 `:391` 的 `is shipped` 同名门。
- `:11` `C_semantic_drift_reddens`：篡改 `federal_reserve_fomc_documents` 的 `time_field` 后红在
  `:398`，且同臂打印未篡改对照 `control_untampered_exit_ok=True`、`table_entries=7`、
  `domains_in_handoff=7`（对照臂的总量与分母都在场）。
- `:12` `D_population_pinned`：从表里删一枚条目后红在 `:382` 的总体等式——它证明这判定不是
  `all([])` 那种真空绿。
- `:13` `E_retired_comparison_recorded`：退役那次比较在替身时代
  `missing_on_look_alike=[]`、在当代契约上 `missing_on_shipped_contract=[document_date,
  document_type, format]`，`reading=the retired check was green only while the table imported the
  look-alike, so a green there measured the defect and not the contract`。
- `:14` `F_restored_green`：复原后重新绿（`swap_undone=True`），伪造就没留在树上。

`:15` `JUDGE_RULING=live_faces_priced=imported_class_is_resolved_contract+shipped_semantics_are_contract_fields
retired_face=handoff_spellings_vs_resolved_class all_arms_pass=True`。

### 3.4 字节面：`probes/landed_bytes_face.py`（10 臂）

这台仪器是因为 §1 我写下的两枚摘要是凭记忆打的才存在的——它的作用是让本节**不需要任何人回忆**任何
摘要。每枚文件同时走三条通道（磁盘 `sha256:16`、`git show HEAD:` 字节的 `sha256:16`、producer 写进
该片 claims 的 `after_sha256`），逐枚原文在 `:4-8`，门在 `:19`：

- `A_every_git_show_returned_zero`：五次 `git show` 的 exit 全为 0（`:4-8` 各行的 `head_exit=0`）。
  如果我把某枚文件的路径写错，HEAD 会退非 0 而不是安静地给出空字节。
- `B_all_four_moved count=4`（`:10`）：本波四枚文件每一枚的 disk 摘要都不同于 HEAD；它给
  `moved` 这个谓词一个"四枚都能读 True"的正总体。
- `H_control_untouched`（`:16`）：同一谓词对未编辑的 producer 读 `moved=False`——两面都在，B 那个
  `count=4` 才是真数。
- `C_pin_membership`（`:11`）：pinned 三枚、unpinned 一枚（测试文件），打印的是成员而不是布尔，
  所以"测试文件欠不欠换代税"这一格不必靠推断。
- `D_pinned_agree_with_disk agreed=3 of=3`（`:12`）+ `E_denominator this_probe=47 drift_carrier=47`
  （`:13`）：本机自己走 shard 得到的分母与漂移载档的 `pinned_files` 相等，两把尺不同源。
- `F_shard_part_agrees_with_both_carriers`（`:14`）：落地前那把 `pinned=378e1b9a619078d9`、落地前
  `disk=e9c4edc8405c7306`、今天 `disk=e9c4edc8405c7306`、复取 `rerun_stale_files=0` 同臂并打，
  所以 §4.3 的"pin 已追平、且中间代另有其主"是读出来的。
- `G_self_lint bytes=12452 codes={}`（`:15`）：`take1_lint_preexistence.ruff_over()` 对这台探针
  自己的当代字节跑同一条 ruff 通道（§4.5 的通道对它自己也收费），所以它没有靠 §4.8 的豁免活着。
- `I_head_plane_self_consistent`（`:17`）与 `J_intermediate_generation`（`:18`）：见 §4.3。

`G` 这一臂是这台仪器里唯一"两面都可能不绿"的门：它的读数随探针字节移动，改探针不重跑就会红。

### 3.5 凭证预扫：`probes/staged_payload_scan.py`（9 臂）

这台仪器存在的理由是一句规矩：**提交前必须扫描本次 commit 真正新增的那批文件**，而"扫过了"不能写成散文。
它扫的对象是 **index 里的字节**（`git show :<path>`，二进制读出，不走文本模式所以 CRLF 不会被翻译），
不是工作树——这样任何"只改了工作树、忘了 `git add`"的旧字节都无法躲在绿面后面。门在 `:17`
`arms=9 arms_passed=9 all_pass=True`，`:18 staged_payload_scan_exit=0`。字母标签只有 8 个（A-H），
第 9 把门是 `D` 带了两条（`D_strip_actually_changed_config` 与 `D_allowlist_free_clean`），这一点
我在标题里按门数而非字母数计。

- `:4` `TOOL`：`gitleaks_version=8.30.1 repo_config_present=True`。工具版本与"本仓确实有一份
  `.gitleaks.toml`"是读出来的，不是我记住的。
- `:5-8` 四枚落地码文件的 `index=… worktree=… same=True`：这既是 index==工作树的证据，也是 §1 那四枚
  摘要的第二条通道（本机重新取一遍，与 `landed_bytes_face.py` 不同源）。
- `:9` `A_staged_roster count=134 code_files_in_roster=True missing=[]`：分母是
  `git -c core.quotepath=off diff --cached --name-only` 的实际条数，四枚码文件必须在册；`missing`
  打印的是缺失的**名字**而不是布尔，所以"少一枚"会直接指名。134 的分解是 4 枚码文件 + 91 枚
  `docs/evidence/C101` 收档 + 39 枚本波目录（含这台仪器的探针与它自己的载档）。
- `:10` `B_blobs_from_index copied=134 of=134`：落进临时树的文件数等于在册数——扫描没被静默截断。
- `:11` `C_scan_shipped findings=0`：按仓库现行配置（allowlist 生效）扫整个 payload。
- `:12` `D_scan_allowlist_free findings=0 raw_lines=185 stripped_lines=15 lines_dropped=167
  rules_kept=2 config_actually_changed=True`：这一把是 C 的对照臂。本仓 `.gitleaks.toml` 有九成是放行表，
  所以只报 C 的 0 可能只是"放行表把东西吞了"。我把 `[rules.allowlist]`/`[[allowlists]]` 两类表整段剥掉
  再扫，并**先证明配置真的变了**（`config_actually_changed=True`）——这条是第二把门，理由见下面。
- `:13` `E_scan_builtin findings=0`：完全不给配置文件，用 gitleaks 内置规则集扫。
- `:14` `F_planted_control findings=1 files=['POSITIVE_CONTROL.txt']`：三把扫都读 0，那 0 是不是"扫描器
  根本没在跑"？我在临时树里种一枚运行期生成的假 key（`AKIA` + 随机 base32），同一套 argv 立刻读到 1。
  **这把门救过一次假绿**：第一版剥表用的 awk 起始模式没匹配到任何一个表头，于是 `raw_lines` 与
  `stripped_lines` 相等（185 = 185），"放行表已移除"这个断言根本不可能为真，`D` 臂会永远绿着扫一份
  放行表全开的配置。修模式后 185 → 15 才让这把门有可能红。这与 §7.3 是同一形状的缺陷（控制臂没有能力
  不同），只是这次发生在**我为了收口 §7 那类错误而新造的仪器**里，所以另记一条 §7.12。
- `:15` `G_planted_literal_not_printed leaked=False`：种下去的字面量不许进载档——否则我这台"防泄漏"
  的仪器自己就成了泄漏点。它扫的是本机内存里的 `planted` 是否出现在 `path.read_text()` 里。
- `:16` `H_self_lint bytes=10879 codes={}`：同 §3.4 的 `G` 臂，用 `take1_lint_preexistence.ruff_over()`
  对自己当代字节跑同一条通道。

自指的限制要说尽，不要留给读者猜：这台仪器的载档写在 `runs/` 里，而它自己也是 payload 的成员。我没有把
这个循环当成缺陷绕过，而是把它测成了一个**不动点**：先把 134 枚（含探针与上一把载档）全部暂存，再跑这台
仪器，于是 `:9` 的 `count=134` 就是最终提交的总体大小，`A`/`B`/`C`/`D`/`E` 读的都是暂存区里那 134 枚的
index 字节，其中包含载档自己上一代的字节；跑完只重新暂存这把载档。此时"被扫的那代载档字节"与"落盘的那代
载档字节"是否同一代，不需要任何人相信散文——判定式是

```
PYTHONPATH=. python docs/evidence/C133/wave31-step4-federal-reserve-fomc-documents-retarget-20261018/probes/staged_payload_scan.py
git -c core.quotepath=off diff --name-only        # 必须为空
```

重跑写回的字节若与 index（commit 之后是 HEAD）不同，`diff --name-only` 会立刻列出这把载档，谓词为假；
为空就是不动点成立。我在提交前实测它为空，所以"载档覆盖了自己的字节"这条是可复算的，而不是我读出来的
一枚摘要。void 条件：这条判定只在总体不再变化时成立——commit 之后若又改了 payload 里任何一枚文件，
`count` 会变，这把面必须重取，否则 §3.5 的所有读数都成了上一代。


## 4. 判定器伪影与本波发现

### 4.1 "同名"是这个 provider 藏了六波的原因，不是巧合

`federal_reserve_macro` 与 `federal_reserve_clean_documents` 都发布
`FederalReserveFomcDocument`。任何用**类名**做解析键的判定（旧 handoff 正是如此，§3.2 第 5-6 臂）
都能被替身满足，因此它绿的时候测的是缺陷。修法是只允许身份比较（`:391` 的 `is`）加上一句
"名字必须在表里"的存活检查。`runs/wave31-step4-fomc_retarget_cost_face.txt:13` 的 B2 臂就是这条
规则的代价说明：光看谓词红不红分不开替身与 `list`。

### 4.2 C100 handoff 工件的历史拼写不能当键

三枚 `document_date/document_type/format` 只存在于 handoff 文件的记录时刻。我没有去改那个工件
（它未被跟踪，`handoff_file_tracked_rc=1`），而是把测试改成"从活注册表读语义"，并把退役比较留在
§3.3 第 5 臂里。这与 #60（batch5 围栏钉住 15 条 gitignored 路径）是同族的复现性缺陷，那条仍然待修。

### 4.3 pin 换代税：一枚 retarget 让 13 枚行全部重签，且 `pre_chain` 因此退化

`runs/wave31-step4-pin_drift_pre_production.txt:5` 在链路开跑前读到
`STALE …/rows_a.py pinned=378e1b9a619078d9 disk=e9c4edc8405c7306`，`:6`
`pinned_files=47 stale_files=1 rows_with_pins=13 drifted_rows=[全部 13 枚]`。链路之后我按驱动自己的
设计复取了同一条 argv：`runs/wave31-step4-pin_drift_pre_production_rerun.txt:5`
`pinned_files=47 stale_files=0 rows_without_pins=[] drifted_rows=[]`（`generated_at_utc` 不同：
`:4` 是 `2026-10-18T08:22:15…`，落地前那把是 `:4` `08:15:56…`）。全树收档面
`runs/wave31-step4-receipt_pin_consistency.txt:8` `slots=20692 stale=0 … PASS=True`。

`378e1b9a619078d9` 这枚摘要一度是无法归因的：它既不是 HEAD 的 `rows_a.py` 字节
（`09c56380b9de5764`），也不是今天的磁盘字节（`e9c4edc8405c7306`）。我没有用散文解释它，而是把
归因做成两臂：`landed_bytes_face.txt:17 I_head_plane_self_consistent pinned_at_head=2 of=4 agree=2`
证明 **HEAD 的 claims 文档钉住的每一枚文件都与 HEAD 自己的字节相符**（HEAD 钉了两枚，另两枚
`fomc_documents.py`/`test_federal_reserve_iter2_models.py` 根本不在 HEAD 的 pin 集合里，因为 HEAD
那片 shard 对本行没有条目）；`:18 J_intermediate_generation named_by_pre_landing_carrier=378e1b9a619078d9
is_head=False is_today=False` 则把那枚摘要定位为**本波自己造出来的中间代**——漂移探针读的是工作树
里的 claims 文档，而本波 run 1 已经重写过它，所以"落地前"那把 `pinned=` 记的是一枚从未提交的字节代。
结论：这条 face 的 `stale_files=1` 是真的（工作树 pin vs 工作树字节），但它不是"上一代提交里有个坏
pin"，而是"我自己在这条链路之前已经动过钉住的文档"。

诚实记录：`runs/wave31-step4-pin_drift_pre_production.txt:7` 的 `pre_chain` 已经读到
`offline_acceptance_count=290`，因为这条 argv 在链路**之前**跑过一次（run 1 已把行落下），run 2 的
"落地前"读数因此是退化的。所以 §1 的 289 前一格取自 `:10` 的 `F_population`（那把探针只在落地前跑过
一次），而不是取自链路自身的 `pre_chain`。

### 4.4 take-1 的控制臂咬不动，于是它自己成了发现

`runs/wave31-step4-fomc_retarget_cost_face_take1_REFUTED.txt` 是留档的反例：take 1 用
`type(transform_data(...)[0])` 取实发类型，得到 `emitted=builtins.list`（`type(list_of_rows[0])`
会被读成 `type(list_of_rows)[0]` 的同族歧义，pydantic 行有 `__iter__`），于是"替身"从来没被伪造出来，
那一臂**因错而绿**。载档 `:8` 起把这几点写在头部。同一族的还有 `probes/take1_lint_preexistence.py`
的第一枚控制臂：我伪造的是 `#` + 119 个短横线的注释，ruff 的 E501 对它 `E501_delta=0`
（`runs/wave31-step4-take1_lint_preexistence.txt` 的 `B_forged B1_token_run_comment`），而伪造一条 116
列的长字符串语句 `E501_delta=1`。两条臂因此拆成 B1（记录豁免形状）与 B2（用**被计费的那个形状**
做对照），gate `B2_detector_live_on_the_charged_shape` 才是让 arm A 的那个零成为真零的东西。

### 4.5 本波自己引入的 lint 债只有两枚，且有载档

`runs/wave31-step4-ruff_check_take1_PREFIXES.txt` 是本波第一手红面（`D205@1:1`、`PERF401@252:9`、
两枚 `E501`），修完的绿面 `runs/wave31-step4-ruff_check.txt:4` `All checks passed!`、`:5 exit=0`。
"这两枚 E501 是我写的、另两枚不是我写的"这句一度只是散文，现在是面：
`probes/take1_lint_preexistence.py` 用 `git show HEAD:<target>` 取上一代字节、把两份字节都交给同一条
ruff argv（`--stdin-filename` 保持规则作用域一致），再做集合差：
`runs/wave31-step4-take1_lint_preexistence.txt:14`
`PREEXISTENCE_RULING head_pre_existing=2 introduced_by_this_wave=2 cleared_here=4 take1_total=4
worktree_now=0`，`:15` `arms=12 arms_passed=12 all_pass=True`。
HEAD 与磁盘的差由 gate `H_head_and_worktree_are_different_bytes` 把守（637 行 vs 704 行，
`E_after_state`），`git show` 退 0 由 `I_git_show_retired_zero` 把守，每台 ruff 调用的"我解析出的条数
== ruff 自己报的条数"由 `J_every_parse_agrees_with_ruffs_own_count` 把守（含本探针自身那份字节）。

### 4.6 mypy 花名册是我自己造的红

`runs/wave31-step4-mypy_take1_ROSTER_REFUTED.txt` 把 handoff 测试放进了 roster，于是 22 枚错误全部
落在一台 gate 从不 typecheck 的文件里（产品面是 `mypy $(PY_MYPY)` = `opendata alembic alembic_data`，
`Makefile:26`；`tests/` 不在其中）。这 22 枚不是本波造成的，把它计成本波的债就是"链造的红"。
修正后的 roster 只有两枚文件，`runs/wave31-step4-mypy_retake.txt` 打
`Success: no issues found in 2 source files`、`mypy_exit=0`。**同时**：这条 leg 的第一把载档
`runs/wave31-step4-mypy.txt` 头部散文写着三枚文件、argv 只有两枚——那是我从旧 roster 搬过来时漏改
的注释，所以本波的面里留了一把头部与 argv 不一致的载档，`mypy_retake.txt` 才是当代面，旧那把不删
是为了让这条错误可查。bandit 面：`runs/wave31-step4-bandit.txt:4 bandit_exit=0`。

### 4.7 producer 的分母标签仍与它数的东西不符（登记为 #69，本波不修）

`runs/wave31-step4-producer.txt:70` 的 `phases actually executed as subprocesses=65` 与该行的
`claims emitted=65` 恒等，因为它统计的是 claim 记录数，不是 subprocess 数。数字不错（13×5=65），
标签错。改它要动 `iter2_claims_producer.py` 这个被大量收档 pin 住的共享件，属于另一波。

### 4.8 本波探针目录里剩 17 枚 ruff 计费项，其中 16 枚是**故意**不修的

产品的 ruff 面是 `PY_SELFDEV := opendata scripts tests alembic alembic_data`，`docs/evidence` 不在
总体里；但本链路自己的 FORMAT leg 把 `probes/` 目录放进了 argv（`ruff_format_retake.txt:2` 的 argv
可见），于是"格式必须过、规则不过"这个不对称是我这台链造的。当代实测（`ruff check --output-format
concise` 指该目录）：`fomc_documents_retarget_prevalidation.py` 8 枚（1 枚 E501 + 7 枚 B009）、
`fomc_retarget_cost_face.py` 8 枚（7 枚 E501 + 1 枚 S607）、`w31s4_run_chain.py` 1 枚 E501。

为什么这两枚测量探针不修：它们的载档是**落地前一面的唯一载体**，而探针读的是当时活树。
`prevalidation` 的 `F_population` 现在只会打 `accepted=290`（`incomplete_row_model` 那枚状态早已
移动），`cost_face` 的 `A_guard_today` 现在只会打 `this_row_declaring=True`、
`D_handoff_control` 现在只会打 `exit_ok=False`（测试已重基）。改字节再跑，就等于销毁本波唯一的
落地前读数。留在 `probes/` 里的那 1 枚驱动 E501 更具体：它落在一段会被写进载档头部的散文里
（`w31s4_run_chain.py:247`，`pin_drift_*` 载档第 1 行就是它），改它等于改历史载档的头部，所以我按
同一理由留着。驱动注释块里原本的另外 4 枚 E501 与本次无关（纯散文），已经修掉并重取
（`ruff_format_retake.txt:4` 现值 `11 files already formatted`）。11 = 花名册里显式的 4 枚 + `probes/`
目录里现存 7 枚 `.py`（`ls -1 probes/*.py | wc -l` 实测；这台链路的 FORMAT leg 把**目录本身**放进 argv，
所以新探针会自动入册，不需要我改常量）。逐文件债目在第七枚探针（`staged_payload_scan.py`）落地后**一格没动**：
目录级仍是 17 = E501 9 + B009 7 + S607 1，非零文件仍是三枚（`fomc_documents_retarget_prevalidation.py`
8、`fomc_retarget_cost_face.py` 8、`w31s4_run_chain.py` 1），其余四枚为 0——新探针那两枚各自用 `G`/`H`
臂对自己的当代字节跑过同一条通道（`codes={}`），所以"17 枚"这个分母没有靠豁免长大。
有载档的绿面只有两代：`ruff_format.txt:4` 数到 8 枚（当时目录里只有 4 枚 `.py`，
`take1_lint_preexistence.py` 与 `landed_bytes_face.py` 都还没写），以及现在这把 11 枚。中间的 9 与 10
两代**都没有载档留下来**：9 只出现在当时的控制台，10 是 `ruff_format_retake.txt` 的上一个字——这台仪器
把每一代写在自己的固定路径上，我后来加第七枚探针再取时把它原地覆盖了。两代因此按 §7.5 的同一分类标为
**不可再现面**，并且这个覆盖行为本身就是 §7.12 记的那条缺陷的同族。
红的那把 `ruff_format_take1.txt:4-7` 明列**四**枚待重排文件（三枚探针 + 驱动本身），`:8` 是同一条
argv 的总数行 `4 files would be reformatted, 4 files already formatted`，`:9 ruff_format_exit=1`。
这也反证了当时目录里只有 4 枚 `.py`：8 = 显式 4 + 目录 4。

这里有个值得单独记的名字陷阱：`runs/` 同时存在 `wave31-step4-ruff_format.txt`（本波早期那把，8 枚
文件）与 `wave31-step4-ruff_format_retake.txt`（现取的，11 枚）。我第一次核对本节数字时
`cat` 的是**没有 `_retake` 后缀**那把，于是把 8 读成了"花名册少算了 2 枚文件"，并据此怀疑 ruff 的
目录发现行为。真原因是我读了近名的旧面；用同一条 argv 重跑得的是当时那代 10，与显式列 10 枚文件的跑法
一致（那两次读的都是当时的控制台，显式列文件那把没有载档；有载档的只有目录 argv 这把，现值 11）。
教训是 `runs/` 里 `_<后缀>` 载档与无后缀载档是**不同代**，
引用时必须先看 `ls -t` 的首行，不能靠文件名推断新旧。

`take1_lint_preexistence.py` 不在此列，它自带一枚臂来证明这条通道对它自己也有效：
`G_self_lint bytes=11122 codes={}`，gate `G_probe_itself_rule_clean`。

## 5. 对 step-3 谓据的验证：四条全部命中

step-3 的 §6 在只定价不动手的条件下写了四条谓据。实测：

| 谓据 | 预测 | 实测 | 载档 |
| --- | --- | --- | --- |
| plane 1 | 289 → 290 | 290 | `judge.txt:5` |
| `retarget_fetcher_output` | 2 → 1 | 1 | `residue_axis_census.txt:66` |
| `NEED_DECLARE` | 14 → 13 | 13 | `readiness_census.txt:5,7` |
| federal_reserve 残留 | 1 → 0 | 0（该 provider 不在 `buckets_by_provider` 里） | `readiness_census.txt:9` |

step-3 列的四条失效条件逐条核过：`accepted + residue = 290 + 60 = 350`
（`runs/wave31-step4-seam_reachability_census.txt:40` `accepted_plus_residual=350`）；身份集仍是
`5168e1e723999312`（`readiness_census.txt:10`）；arm F 的读法见 §6（它**不是**"无可落地"的意思）；
federal_reserve 其余 12 枚行的 pin 已由本波 producer 重生产并追平（`stale=0`，§4.3）。

另外两条是本波新读到的轴：`residue_axis_census.txt:78` 打 `witnessable_today=0`，
`seam_reachability_census.txt:39` 打 `witness_modules=27 client_seam_modules=12`、残留侧
`SEAM_REACHABLE 21 / SEAM_NEVER_USED 37 / NO_PACKAGE_ON_DISK 2`（`:40`）。

## 6. 残留 60 行的行动图与下一波谓据

`runs/wave31-step4-residue_axis_census.txt:78`：
`actions={'add_code_carrying_guard': 12, 'implement_missing_model': 19, 'ledger_ruled_out': 11,
'new_provider_package': 2, 'retarget_fetcher_output': 1, 'shard_entry_needs_harness': 15}`，
`:67` `4_action_totals actions=6 nested_sum=60 residue=60`，`:72` `C_population_identity … PASS=True`。
按 provider 拆（`:68` 与 `:69`）：`add_code_carrying_guard` = tmx 1 + yfinance 11；
`implement_missing_model` = government_us 1 + tmx 16 + yfinance 2；
`rows_without_module=37 of=60`（`:68` 那行 `5_needs_first_module`），`G_census_coverage`
（`:76`）打 `absent=[] rows_without_module=37 rows_with_module=23`。

**arm F 的口径必须先说清，否则那个 0 会被读成"什么都落不了地"**：
`residue_axis_census.py:43-44` 定义 `F_witnessable_today` 只数"仅缺 shard 条目 + 一次复核"的行
（`action == shard_entry_only`）。所以 `:75` 的 `count=0 rows=[]` 说的是"60 枚里没有一枚是纯登记问题"，
不是"本波这类落地不可行"——本波刚落地一枚，靠的是 retarget（代码侧）+ shard 条目。这条臂同时打
`controls=2 control_actions_on_harness=[shard_entry_only]
control_actions_off_harness=[shard_entry_needs_harness] control_real_fake_ok=[True, True]`，
即它被证明**能**读非零，所以那个 0 是真零而不是仪器瞎了。

下一波谓据（按代价排序，本波只定价、不动手）：

- 唯一一枚 `retarget_fetcher_output` 是 `yfinance::KeyMetrics`（`:60` 逐轴行：
  `status=UNREVIEWED bucket=NEED_DECLARE verdict=ROW_CLASS_NOT_CONTRACT
  seam=SEAM_NEVER_USED mechanism=engine_transport witness_module=False fake_ok=False
  name_in_package=True declared=False witness_entry=False`）。**它的门不是映射裁决**：与
  `FomcDocuments` 不同，这里 `seam=SEAM_NEVER_USED` 且该 provider 没有见证模块，所以照搬本波配方
  不够——retarget 之后还需要为 yfinance 首建 witness 模块。谓据：若下一波做这枚，plane 1
  290 → 291、`retarget_fetcher_output 1 → 0`、`NEED_DECLARE 13 → 12`、`rows_without_module 37 → 36`。
- 更便宜的批量在 `add_code_carrying_guard`（12 枚，tmx 1 + yfinance 11），但 `fake_ok` 与
  `witness_module` 两轴我**没有**在本波逐枚重测（本波只测了 `yfinance::KeyMetrics` 那一枚的逐轴行），
  所以那条路的先决条件是先用 C128 那套四轴仪器对 12 枚逐枚定价，而不是假设本行的机制可复用。
- 复算配方（下一条落地前必须自己跑一遍，不要引用本节数字）：
  `PYTHONPATH=. <py313> scripts/quality/iter2_claims_producer.py --provider <provider>
  --output docs/evidence/C101/<provider>/claims.json` → recorder 覆写该 provider 全部已见证身份 →
  `iter2_claims_merge.py` → `iter2_model_evidence.py --inventory <清单 csv> --claims
  docs/evidence/C101/claims.merged.json --output docs/evidence/C101/judge/claims.merged.report.json`。
- 失效条件（任一成立则本节作废，需重取行动图）：`accepted + residue ≠ 350`；身份集不再是
  `5168e1e723999312`；`F_witnessable_today` 从 `count=0` 变成 >0（说明 harness 面上来了新机制，批次
  选择要重算）；本波 13 枚 federal_reserve 已验收行里有任一枚被本波之外的编辑换了 pin。
- 失败读法：producer 里该模型任一相 `exit≠0` 时，先查 `row_model` 与发布键集的关系（service 腿要求
  键 ⊆ `row_model.model_fields`），**不要**放宽 counterfact 的声明码；若 judge 把别的行打成
  `claim_receipt_hash_drift`，那是 §4.3 的重签名税，唯一正确处置是整片 shard 重生产 + 重签，不是把
  pin 追改到磁盘字节。

## 7. 我错的部分（原文留在档，不改写）

1. **`mypy` leg 的头部散文与它自己的 argv 不符**。修 roster 时我只改了 `MYPY` 常量，没改
   `VERIFICATION_LEGS` 里那句 note，于是 `runs/wave31-step4-mypy.txt:1` 声称检三枚文件、`:2` 的 argv
   只有两枚。读者若按头部引用就会得到一个磁盘上不存在的 argv。处置：note 重写 +
   `--stage retake --leg mypy` 取 `mypy_retake.txt`，旧那把留着不删。
2. **"D205/PERF401 是前代遗留、两枚 E501 才是本波引入"这句一开始只是散文**。它没有承载物时，
   我就等于在用自己的话给自己的 diff 脱罪。处置：新建 `probes/take1_lint_preexistence.py`，用
   `git show HEAD:` 的字节 + 同一条 ruff argv + 集合差把这句话变成 12 臂面（§4.5）。
3. **我的第一枚控制臂根本没有能力咬**。伪造注释用 `#` + 119 个短横线，E501 不涨，gate 当场退 1
   （探针 `exit=1`，runs/ 里因此没有那把半成品）。真正的原因不是我"线太短"，而是 ruff 对那一形状
   豁免、对长字符串语句计费——现在 B1/B2 两条臂各自成门，豁免这条也没被写成机制解释，只写成实测
   差值（`E501_delta=0` vs `E501_delta=1`）。
4. **驱动头部承诺的 `stale_files=0` 在读到之前没有载档**。`pin_drift` 那条 note 从写下来就在预言
   "链路之后应读 stale_files=0"，而本波目录里只有落地前那把。处置：按 note 自己声明的可复跑性质
   复取，写成旁文件 `pin_drift_pre_production_rerun.txt`（不覆盖历史那把），面见 §4.3。
5. **本探针草稿自带 4 枚 E501，那代字节已经不在了**。修它们之前的草稿红面只存在于当时的
   stdout，没有载档，所以这条只能标为**不可再现面**；可再现的是它的两个后继：`ruff_format_retake`
   的 argv 与 `take1_lint_preexistence` 对 `git show HEAD:` 的读数（HEAD 字节永久可取）。
   同理要说明：`ruff_check_take1_PREFIXES.txt`、`mypy_take1_ROSTER_REFUTED.txt`、
   `fomc_retarget_cost_face_take1_REFUTED.txt`、`ruff_format_take1.txt` 这四把红面数的是**已被取代的
   工作树代**，重跑同一 argv 现在只会得到绿面——它们留档的作用是记录"当时长什么样"，不是"今天还能
   复算"。
6. **我按猜的模块路径跑了一次 `engine_declared_models`**，`from opendata.data.engine import …` 直接
   `ModuleNotFoundError`。真路径写在被引文件里：`tests/test_provider_model_contracts.py:90` 是
   `from opendata.data.providers.catalog import PROVIDERS, engine_declared_models`。这条错误的后果值得
   单列：那次导入本意是给 §1.2 造一个"本行不进 812 总体"的数，而我打算把控制台读数当证据。改成从
   已归档的 `pytest_collect_denominator.txt` 树里数 `<Module>` 之后，919 的逐模块拆分才进入可核范围，
   §1.2 引的是那把载档。
7. **我把 collected 从 879 涨到 919 读成了"本波 +40 的 mover"**，并对它做了逐文件归因。错了：879 是
   step-3 那五枚 government_us 文件的总体，本波这五枚是 federal_reserve/契约/域的 roster，两把不是
   同一个总体，40 里绝大部分是 roster 差，与落地无关。差值声明已撤回，只保留同总体内的闭合
   （740 + 179 = 919）与 §1.2 那条机制性说明（`MODELS` 来自声明式注册表）。
8. **`fomc_retarget_cost_face.txt:12` 那两个同时为 True 的布尔，我第一反应是"面自相矛盾"**。真原因是
   字段名起得糟：`:123` 与 `:124` 是两条不同谓词。我没有改探针（§4.8 的冻结理由），而是在 §3.2
   把定义原文引上来。这条记在这里，因为"看到矛盾就怀疑仪器"和"看到矛盾就替仪器辩护"是同一个错误
   的两面，正确动作是把源码谓词摊开。
9. **§1 里我写下的两枚 `sha256:16` 是回忆出来的，而且两枚都错**。原文是
   `cc1f24a6d8e94e7b`（fetcher）与 `2a3e4a58ac3c6f39`（见证模块），实测分别是
   `3a03c7d4f79a33be`、`be380dd643f747de`（`landed_bytes_face.txt:4`、`:6`）；第三枚
   `e9c4edc8405c7306` 侥幸记对了。这一类错误比数字本身更糟的地方在于：那两枚摘要当时**看起来**有
   来源（同段引了 `receipt_pin_consistency.txt:8`），而 `stale=0` 只证明收档与磁盘一致，根本不证明
   我手打的摘要就是那枚磁盘字节。处置不是改两个字符，而是造第五台仪器（§3.4），把
   磁盘 / `git show HEAD:` / shard `after_sha256` 三条通道逐枚并排打出来，让本节不再有"需要有人记得"
   的数。错值留在本节上方与这里，不删。
10. **我引用载档时读错了近名旧面**。`runs/` 里 `ruff_format.txt`（8 枚）与 `ruff_format_retake.txt`
    （10 枚）是同一条 argv 的两代，我按名字取了没有后缀那把，于是把 10 读成 8，并顺着这个读数去
    怀疑 ruff 的目录发现行为、跑了三轮 shell 才定位到"是我的引用错了不是仪器的行为怪"。同一次核对
    里我还把 `ruff_format_take1.txt` 的四枚待重排文件写成"两枚"。处置：本节所有 `:行` 引用改为
    `sed -n`/`cat -n` 原文读取，§4.8 的花名册世代只留有载档的两代（§4.8）。
11. **一次计数被我自己造的 shell 伪影灌水**。我用 `ruff check --output-format concise <file> | grep -c .`
    做逐文件债目，得 10 + 9 + 1 + 1 + 1 + 2 = 24，与目录级 `Found 17 errors` 冲突。原因是 concise
    格式在每个文件的输出尾部仍打 `Found N errors.` / `[*] M fixable…` / `All checks passed!`，
    `grep -c .` 把这些摘要行一起数了。真目次是 8 + 8 + 1 = 17（§4.8），三枚文件为 0。
    这条记在这里是因为它与 §7.9 是同一形状：**计数必须由被计数对象的行来支撑，不能由"我跑了个命令"来支撑**。
12. **我造来收口凭证扫描的那台仪器，自己的对照臂第一版是死的**。`staged_payload_scan.py` 的 `D` 臂要把
    `.gitleaks.toml` 的两类放行表整段剥掉，我的起始模式没匹配到任何表头，于是 `raw_lines` 与
    `stripped_lines` 都是 185、`lines_dropped=0`——那份"allowlist-free"的扫描其实扫的是放行表全开的
    同一份配置，`findings=0` 永远为真，且**没有任何一条门能读到这一点**。修法是剥表模式对准
    `[rules.allowlist]`/`[[allowlists]]`，并把"配置真的变了"从推断升级成独立门
    `D_strip_actually_changed_config`（现值 `:12` `config_actually_changed=True`，185 → 15、
    `rules_kept=2`）。这一版的缺陷面**不可再现**：这台仪器把载档写在自己的固定路径上，我修完直接重跑，
    第二代把第一代原地覆盖了，`runs/` 里只有绿的那把。所以这条按 §7.5 的同一分类处理——留文字、不留面，
    并且它说明一件比单个 bug 更麻烦的事：**用来防"散文式断言"的仪器，如果它的对照臂只能与自身比较，
    它本身就成了新的散文**。现在的 `F` 臂（种一枚假 key，同一 argv 必须从 0 变 1）是那把仪器的自检。

## 8. 载档清单

`docs/evidence/C133/wave31-step4-federal-reserve-fomc-documents-retarget-20261018/`

- `probes/w31s4_run_chain.py`（487 行）—— 落地与验证链驱动；`--stage pin-drift` 复取漂移表（写旁
  文件），`--stage retake --leg <name>` 用同一份 `VERIFICATION_LEGS` 重取单腿
- `probes/fomc_documents_retarget_prevalidation.py`（304 行）—— 6 臂落地前定价（§3.1），其字节已冻结
- `probes/fomc_retarget_cost_face.py`（317 行）—— 7 臂代价面（§3.2），字节同样冻结
- `probes/handoff_judge_counterfact.py`（235 行）—— 6 臂重基判定器反例（§3.3）
- `probes/take1_lint_preexistence.py`（248 行）—— 12 臂 lint 归属 + 控制臂形状校准（§4.5、§4.8）
- `probes/landed_bytes_face.py`（277 行）—— 10 臂字节面（§3.4），本波唯一"改探针不重跑就会自己变红"
  的仪器；它 import 驱动常量与 `take1_lint_preexistence.ruff_over`，所以 `G` 臂用的还是同一条计费通道
- `probes/staged_payload_scan.py`（282 行）—— 9 臂凭证预扫（§3.5），扫的是 index 字节而不是工作树；
  它自己带一次"对照臂是死的"缺陷与修复，记在 §7.12
- `runs/`（本波提交的 31 把面，`wave31-step4-` 前缀一律省略）：`producer`、`recorder`、
  `merge_take1`、`merge_take2`、`judge`、`pin_drift_pre_production`、
  `pin_drift_pre_production_rerun`、`fomc_documents_retarget_prevalidation`、
  `fomc_retarget_cost_face`（take 2）、`fomc_retarget_cost_face_take1_REFUTED`、
  `handoff_judge_counterfact`、`incomplete_row_guard_check`、`readiness_census`、
  `residue_axis_census`、`seam_reachability_census`、`receipt_pin_consistency`、
  `batch5_fence_dry_census`、`ruff_check`、`ruff_check_take1_PREFIXES`、`ruff_format`、
  `ruff_format_retake`、`ruff_format_take1`、`mypy`、`mypy_retake`、`mypy_take1_ROSTER_REFUTED`、
  `bandit`、`pytest_federal_reserve_and_pin_planes`、`pytest_collect_denominator`、
  `take1_lint_preexistence`、`landed_bytes_face`、`staged_payload_scan`

  31 是把 `runs/*.txt` 数出来的（`ls -1 runs/*.txt | wc -l`），31 这个数字与清单条目数是我分别数的，
  所以对不上时以 shell 那把为准。逐把读尾部 exit 行的实测分类：**27 把 `*_exit=0`，4 把非 0**
  ——`ruff_check_take1_PREFIXES`、`mypy_take1_ROSTER_REFUTED`、`ruff_format_take1` 是本波第一手红面，
  `batch5_fence_dry_census`（`:11 exit=1`）是设计内拒绝。这一轮同时数了"没有 exit 行的载档"：0 把，
  所以 27 + 4 = 31 是闭合的，不是我挑着读。需要单列的是第四把 take-1：
  `fomc_retarget_cost_face_take1_REFUTED` **退 0 而被判否**——它的伪造臂因错而绿（§4.4），所以
  "exit 与结论同向"在这台链上也不是恒等式。
  `ruff_format.txt` 与 `ruff_format_retake.txt` 是同一条 argv 的两代，引用时按 §7.10 取新那把。

**不提交**（有意留在工作树）：`runs/_chain2.log`、`runs/_chain_stdout.log`（被 `.gitignore:61`
的 `*.log` 覆盖，因此它们**不能**充当证据，只是链路控制台流水）；`probes/__pycache__/*.pyc`
（4 份）；`docs/evidence/C100/**` 原始收据；`docs/evidence/C108/`；`quality-source-repairs/`；
`LICENSE-*`/`DISCLAIMER.md`/`uv.lock`。

另有两件事按规矩记在这里而不是当已修：`batch5_fence_dry_census.txt:10` 的
`verdict: REFUSED` 与 `:11 batch5_fence_dry_census_exit=1` 是该 argv 在 `stale=0` 时的设计内拒绝
（`--apply` 故意没给），不是红灯；`#60`（该围栏钉住 15 条 gitignored 路径、从干净 clone 不可复现）
与 `#80`（government_us 9 枚测试里 7 枚依赖被忽略的 C100 fixture 根）仍然待修，本波都没动 `tests/`
里那两枚文件之外的东西。
