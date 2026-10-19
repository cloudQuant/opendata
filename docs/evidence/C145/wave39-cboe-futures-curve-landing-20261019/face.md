# wave 39 — cboe::FuturesCurve 落地面与访问轮（2026-10-19）

Bundle：`docs/evidence/C144/wave39-unauthenticated-access-20261019`（访问轮：23 次捕获 + 判定探针）与
`docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019`（落地：全树重签链 + 落地面）。

本轮的唯一判定入口是判定探针，不是本节散文。每个数字都指向一个 carrier 文件与其中一行读数。

## 1. 访问轮的范围是怎么定出来的（不是挑出来的）

`probes/w54_access_round_face.py` 的 K 臂读 `docs/迭代计划/…/模型级任务清单.csv` 的
`credential_fields` 列，把这六行逐行核对为「残差 ∩ 无凭证要求」：
`cboe::FuturesCurve`、`seeking_alpha::ForwardEpsEstimates`、`seeking_alpha::ForwardSalesEstimates`、
`wsj::ETFGainers`、`wsj::ETFLosers`、`wsj::ETFActive`。两条臂让这个数字不只是自证：

- `targets_not_in_residue=[]`，同时打印本轮**没有**处理的 6 行
  （biztoc / econdb×2 / intrinio / stockgrid / yfinance::AvailableIndices）——范围窄了会在这里露出来；
- `targets_credential_free=6 of=6` 的正例是同一列在 350 行里有 **195** 行非空
  （derived_control=`alpha_vantage::EquityHistorical` 等），也就是说这一列不是「读不出非空」才读出的空。

读数面：`runs/w54_access_round_face.txt`，`arms=24 arms_passed=24 PASS=True`，
`w54_access_round_face_exit=0`。

## 2. 本轮最重要的产出是撤回自己的一条结论

第一次取面时我用 `?dt=2026-10-16` 对比无参数请求，字节相同，于是写下「`dt=` 被忽略，历史曲线无证据」。
**这个对照臂没有可能给出不同结果**：无参数请求默认返回的就是最后一个结算日，而那天正是 2026-10-16。
改用默认值给不出的日期重测（C144 捕获 `cboe-futures-settlement-csv-dt-2026-10-09/-15/-17`）：

- 四个日期 = 四份不同的 body（`C` 臂 `distinct_bodies=4 of=4`）；
- 每个响应的 `content-disposition` 文件名自称服务的是哪一天，`asked` 与 `named` 四对全等；
- `2026-10-17`（周六）返回 38 字节：只有表头行，零数据行。

撤回记录写在工件里，不是只写在散文里：`capture-receipt.json` 的 `corrections[]`（`arm="C"`,
`status="CORRECTED_IN_SAME_ROUND"`）与 `history_note`。落地面 `specs.py` 的适配器 docstring 也带同一条撤回。

结论：`cboe::FuturesCurve` 的曲线是**带日期的历史曲线**，比原拒绝理由能承认的更强。

## 3. seeking_alpha 两行：不是被鉴权挡住，而是「空到不可反驳」

`symbol_data/estimates` 的 400 链是活的，逐段点名下一个必需参数（`capture-receipt.json` 的
`progressive_parameter_chain`，四步捕获全在 `sources/`）：`filter[ticker]` → 要 `ticker_ids`；
只有 `ticker_ids` → 要 `relative_periods`；加上 `relative_periods` → 要 data items；
加上 `estimates_data_items` → **200**。

但 200 之后没有可用形状。D 臂读：**11 个满足路由要求、彼此不同的查询返回同一份 31 字节 body**
（`digest=7b1dd4c5afe06f5b`），其中包含一个编造的 data item（`zzz_not_a_real_item`）、一个编造的 ticker
（`ZZZZNOTREAL`）、`relative_periods` 取 0 / -1 / 12、`revisions_data_items` 拼法、非重复键拼法、
带 `scale` / `filter[symbol]` 的追加参数。未知键被静默忽略，所以「空」无法用来区分「你问错了」和
「无账号就没有数据」。

同一秒、同一主机、同一 UA、同一传输的对照：`earnings_calendar` 返回 **12,389 字节**真实数据（E 臂）。
所以空是端点性质，不是客户端性质。这两行的判定仍是「不可声明」，但理由从抄来的散文换成了可复算的读数。
附带读数：`two_bad_requests_share_one_reason=True`——两次不同的错误请求得到同一份 400，
这条臂第一次取面时被我错误地写成「每个拒绝都有自己的 digest」，红的是臂不是捕获。

## 4. wsj 三行：判定改档为「本机传输不可用」，而不是端点不存在

三次尝试（20s / 40s / 15s）全部 `curl: (28)`，`remote_ip` 为空，0 字节响应头；`-v` 显示
`Host www.wsj.com:443 was resolved` → `Trying 69.63.187.12:443...` 之后没有 `Connected`。
解析器 8.8.8.8 给出的这个地址能不能连上是网络侧的事，不在本轮授权范围内继续追。
F 臂因此**只断言没有状态码可声称**（`chain=[]`、`code=NO_STATUS_LINE`），
`wsj_transport_attempts` 三次齐在 `capture-receipt.json` 里。
这三行的原拒绝理由（散文）与本轮读数是两件事：本轮没有反驳它，也没有确认它。

## 5. 捕获面自身的两处仪器缺陷（报告，不静默修）

1. **`gitleaks dir-mode` 不是 8.30.1 的子命令。** 我第一次扫 `sources/` 用的就是它，空 stdout 被我映射成
   `findings=0`——那个零是命令行参数错误的产物。改用仓内既有的 argv（`dir <root> --config .gitleaks.toml
   --report-path …`，报告文件缺失一律记 `NO_REPORT=-1`）重取，并带伪造 token 的植入臂：
   `runs/w58_sources_credential_scan.txt` 读 `baseline findings=0` / `planted findings=2`
   （`generic-api-key`、`github-pat`），4/4 臂绿。
2. **捕获里有服务端自己下发的 `set-cookie`**（Cloudflare `__cf_bm`）。它是响应头的产物，不是我提供的凭证；
   G 臂对 receipt 的记录 argv 与 URL 做的凭证标记扫描读 `payload_hits=0`，植入臂读 1，
   伪造 token 只在 `/tmp` 副本里存在且从不打印。

## 6. 落地实现的三处判断，以及每处为什么不是更简单的写法

- **`date` 是校验过的请求回显，允许为 null。** 结算表 body 里没有 as-of 单元格，日期在
  `content-disposition` 文件名里；而离线见证缝的假响应只塞 `Content-Type`
  （`scripts/quality/iter2_claims_probe_runner.py:356`），从响应头取日期是不可复算的。
  参照 `DeribitFuturesCurve.date: date | None`（同族可空日期）与 wave 37 的先例
  （面板里没有的列就留 null，绝不填）。时钟戳被明确禁止。
- **`price` 必填非空。** 56/56 行都带数字，非结算日返回零行而不是空价行，所以一个非数字是坏 body；
  这条拒绝正是见证反例可达的那个站点（把某一行价格单元格改成文本 → `CBOE_FUTURES_CURVE_BAD_NUMERIC_FIELD`）。
- **URL 两种拼法各自来自实测**：无参数走 `…/settlement/csv/`（也是路由自己的 `rel="canonical"`），
  带日期走 `…/settlement/csv?dt=YYYY-MM-DD`。日期必须拼进 URL 而不是塞进 `params`，
  因为缝的匹配只看 `url` 实参（同文件 `:302`），`params` 会被丢弃。

反例目标是从捕获字节里解析出来的，不是我打的字符串：第一次烟测我手打了 `VX,VX/V6,2026-11-18,…`，
而 2026-11-18 那行的合同代码是 `VX/X6`，needle 命中 0 字节，于是「反例没有拒绝」是一条关于我探针的假读数。
见证片段的 `path=["responses", 0, "body", 4]` 用的是行索引，并且片段里保留了 2 行 VXM，
这样本地按 `Product` 过滤坏掉时不会碰巧通过。

## 7. 残差 11 行：真正的闸门是「凭证」还是「没测到形状」

`probes/w67_residue_gating_face.py`（`runs/w67_residue_gating_face.txt`，9/9 臂绿）。判定来自两个独立通道：
任务清单 CSV 的 `credential_fields`，以及参照实现自己声明的 `credentials=[...]`（只作契约侧对照）。
两条臂保证这不是把散文换个写法：`credential_bearing=195`（这列读得出非空），同时残差里有 **7** 行读
`<empty>`（同一列在同一样本里读得出两种值）。

| 档位 | 行数 | 行 | 这一档的判定依据 |
| --- | --- | --- | --- |
| `CREDENTIAL_GATED` | 4 | biztoc::WorldNews、econdb::CountryProfile、econdb::ExportDestinations、intrinio::OptionsSnapshots | 两个通道都写 `api_key`；本轮授权边界是「只允许公开无凭证 GET」，所以这四行不由我推进 |
| `PROBED_UNANSWERED` | 5 | seeking_alpha::Forward{Eps,Sales}Estimates、wsj::ETF{Gainers,Losers,Active} | C144 已捕获：前者 200 但常量空信封（§3），后者 TCP never connected（§4） |
| `NOT_PROBED_THIS_ROUND` | 2 | stockgrid::ShortVolume、yfinance::AvailableIndices | 两个通道都**不**要求凭证却本轮没打过；yfinance::AvailableIndices 是实测 0 端点（§8） |

这与 wave 34/36 的说法有一处实际差别：biztoc 与 econdb/intrinio 曾被并列为「缺 owner 判定」或「缺权利登记」，
而清单与参照实现都写明它们需要 api_key。权利登记（`rights=NOT_REGISTERED`）仍是 owner 的事，但在权利之前
先卡在凭证上——把它描述成「写代码就能落地」是错的。

`stockgrid::ShortVolume` 是这 11 行里唯一「无凭证要求、且尚未被任何一轮访问核过」的一行：
`package_in_this_repo=False`、`upstream_provider_dir=True`、两个通道都无 key。下一轮可以只对它做一次
公开无凭证核测；本轮不猜 URL，因此不下「它也没有证据」的结论。

## 8. 落地这一行到底用了哪些单元格

四个声明列逐一有主：`symbol`←`Product` 列（表里每行自带产品名，所以不是请求回显）、
`expiration`←`Expiration Date`（源端就发 `YYYY-MM-DD`，不必像 yfinance 兄弟那样把月份回退到月初）、
`price`←`Price`（56/56 有数，故必填非空）、`date`←请求回显且可空。
`H` 臂从捕获里重新解析出 VX 的 14 个到期日并断言单调递增，`runs/w57_landing_face.txt` 的
`H capture_vx_rows=14 first=(2026-10-21, 19.9075) last=(2027-06-16, 20.625)` 就是这条读数。

有一件信息是被**故意丢弃**的：表里的合同代码列（`VX/V6` 这种 `Symbol`）不在固定契约里
（参照 `FuturesCurveData` 只有 `date/expiration/price`，本仓同族再加 `symbol`），
所以落地行不发布它。这一点写在适配器 docstring 里而不是藏在代码里，因为「列存在但没声明」与
「列不存在」是两件不同的事，验收时要能分辨。

## 9. 验收面的真实进度：平面 1 与平面 2 不是同一个数（用仓内探针取，不引用旧台账）

`runs/w70_ac2_10_current_reading.txt`（`scripts/quality/ac2_case_probe.py --item AC2-10`，只读，未加
`--write-ledger`；`ac2_10_exit=0`）。本轮实测：

```
dev_done=17  in_progress=13  not_run=332  ledger_rows=350
label_without_witness=0  live_verification_run=0  contract_test_functions=606
```

而 `docs/quality/ac2-case-ledger.json` 里存档的那份是 `head=31acd14`、`dev_done=2` 的旧读数——它已被
本轮的实测推翻，所以这里引的是探针而不是台账。两份读数的差不是同一件事变了两次，而是平面 1（离线验收，
判定探针 + 反例）与平面 2（任务台账的逐行 DEV_DONE）本来就是两个种群。

结论，按探针读数表述：

1. **本轮之后平面 1 = 339/350**（`runs/w57_landing_face.txt` 的 A 臂与 E 臂：`accepted + residue = 350`）。
2. **平面 2 仍为 17/350。** 这与平面 1 的 339 之间约 320 行的差，就是「实现且离线可验收」与「台账逐行
   DEV_DONE」之间的真实差距。`label_without_witness=0` 说明台账里已标的 17 行都有执行过的正反例——
   差的是覆盖面，不是标签的诚实性。
3. `live_verification_run=0` 与 AC2-10 声明的缺失腿 `live_upstream`（真实上游对照需要授权后的 e2e 腿）
   一致：本轮授权边界是公开无凭证 GET，因此不推进 `live_verification_status`，也不把它写成任何暗示
   「跑过真实对照」的值。

因此目标不能在此被判定完成：**「逐条验收」在这套门禁里有两个平面，本轮把平面 1 推到 339/350，平面 2 只有
17/350**。下一步不是把 320 行标签翻过去（那正是本仓记为 `label_without_witness` 要防的形状），而是先量出
「五个阶段全部执行且正反例齐备、台账仍为 NOT_RUN」的确切行集，再按行推进。

## 10. 那道差被逐行量出来了（不是「大概 320 行」）

`probes/w71_plane2_gap_census.py`，读数 `runs/w71_plane2_gap_census.txt`（5/5 臂绿，
`w71_plane2_gap_census_exit=0`）。它只读 `git show HEAD:` 的已提交工件，所以能在签名链正在重写
worktree 的同时跑：

```
populations claims=1725 rows_in_claims=345 accepted=338 ledger_rows=350 joined=338
evidence_complete=338   accepted_but_unjoined=[]
gap rows_with_full_witness_but_not_dev_done=333   by_status={'NOT_RUN': 320, 'IN_PROGRESS': 13}
counterfact_legs total=345 sampled=12 path_and_digest_recorded=12 receipt_tracked_at_head=12
```

三条臂各自顶住一个假读数：`A_join_is_not_silent`（338 行全部能在 claim 侧接上；连接键拼错会打印
`accepted_but_unjoined=[...]` 并把「差 = 0」变成可见的红）；`C_predicate_can_refuse_a_broken_row`
（把一行 counterfact 改成 FAIL 后，五阶段谓词必须不再认它是完整的）；`D`（每条反例腿都记了
`docs/evidence/C101/receipts/…` 路径与 64 位摘要，且抽样 12 条在 HEAD 都查得到——反例工件是入库的）。
`E` 声明差集是 accepted 的子集。

所以「逐条验收」的真实待办被量化成一句可核对的话：**333 行已经有执行过的五阶段正反例、平面 1 已接受，
但任务台账仍写 NOT_RUN(320) 或 IN_PROGRESS(13)。** 推进它们要逐行做，且 `live_verification_status`
保持 NOT_RUN（`live_verification_run=0`，AC2-10 自己声明缺 `live_upstream` 腿）。这是 wave 40+ 的工作单，
不是本轮可以顺手翻的标签。

## 11. 目标里「32 个 provider」这一条，被两台独立仪器收到同一片缺口

`probes/w72_provider_coverage_census.py`（`runs/w72_provider_coverage_census.txt`，5/5 臂绿）与
`probes/w73_clean_room_face.py`（`runs/w73_clean_room_face.txt`，6/6 臂绿）。前者从判定报告按 provider
数行数，后者用 `ast` 走全部 445 个在范围内的模块数包目录，两者给出的名单完全一致：

```
w72  providers_in_ledger=32  accepted=338(HEAD)  at_least_one_accepted=29
     zero=['biztoc', 'stockgrid', 'wsj']
w73  in_scope_files=445  parse_failures=0  openbb_import_violations=0
     provider_packages=31  inventory_without_package=['biztoc', 'stockgrid', 'wsj']
     prose_cite_files=49  vendored_excluded=325  absolute_reference_path_in_code_files=0
```

对目标的两条硬结论：

1. **「32 个 provider 的本仓自研实现」目前成立到 29/32。** biztoc、stockgrid、wsj 三个连包目录都没有
   （`opendata/data/providers/<name>/provider.py` 不存在），因此也谈不上任何一行被验收。这不是本轮引入的
   ——HEAD 上就是这个状态，本轮只是第一次把它按两台仪器量出来，而不是用「28/32 已落地」这类旧叙述。
2. **clean-room 这条此前没有仪器覆盖。** 仓里的 `vendor_independence.py` 审的是 MIT 采购进来的 akshare SDK
   跨 BSL 边界的 import，跟「不依赖 OpenBB」不是一条边界，所以门禁绿不构成这条的证据。w73 补上：445 个
   模块（含函数体内的延迟 import）里 `openbb*` 根为 0，注入一条 `openbb_core...` 后同一探测器必须报 1
   （`forged_hits` 反例臂），325 个 vendor 文件被显式计数并排除，49 个文件以文字引用上游契约文件（这是
   被允许且必要的用法），代码里也没有指向参照检出的绝对路径。

这两条读数还各自挡住了一个假绿法：w72 的 `D_zero_classifier_can_bite` 要求「把某 provider 抹掉后零覆盖
名单必须变长」，w73 的 `C_detector_reports_an_injected_import` 要求「注入的 import 必须被同一谓词抓住」。
一台的 0 与另一台的 0 只有在各自能报出非 0 时才互相印证。

## 12. 「子代理总数 ≤30」这条被记成违反，而不是记成我记得的数

`probes/w74_dispatch_ledger_face.py`，读数 `runs/w74_dispatch_ledger_face.txt`（7/7 臂绿）。过程性条款不能按
代理自己的记账来评，只能按会话记录里真实存在的派发块数出来：

```
transcript_dir files=7 primary=6973912e primary_raw=34 primary_deduped=34 primary_lines=15218
cap=30 this_thread_total=34 over_by=4 within_cap=False project_dir_all_threads=313
subagent_types_seen={"general-purpose": 249, "Explore": 63, "<unset>": 1}
```

两点都是这轮实测纠正出来的，不是复述：

1. **总体选错过一次。** 第一版把项目目录下 7 份会话记录全加起来，得到 **313** ——那会把别的线程（其中一份
   就有 183 次派发）算进本目标的帽子里。帽子的主语是本目标线程，所以正文数取 primary 文件的 34，
   313 只作为 `project_dir_all_threads` 的读数保留；`B_thread_scope_changes_the_number` 这条臂就是钉这个
   选择本身（两个数必须不同，否则说明根本没做范围区分）。
2. **我原先假设「续接的会话会重复吐同一条 tool_use」，因而上过一条「去重必须改变计数」的臂——实测为假**：
   单文件内 id 本来就是唯一的（`primary_raw=34 primary_deduped=34`）。那条臂被换成
   `B2_ids_are_unique_within_the_thread`（断言线程内 id 唯一）与 `D/E`（读的是活文件、分母非空）。

判定：这条约束**在本线程被违反 4 次**（34 > 30），因此本轮及之后的波次不再派发子代理，所有证据面都由主会话
仪器产出（`w72`/`w73`/`w74`/`w57`/`w60`/`w67`/`w68`/`w69`/`w71` 全在 `probes/` 下，可复算）。

## 13. 「验收必须落档」这条，对全部 339 行逐行核过

`probes/w75_witness_completeness_face.py`，读数 `runs/w75_witness_completeness_face.txt`（10/10 臂绿）。
它不看判定报告的数字，看数字背后的东西——对每一个 `OFFLINE_ACCEPTED` 身份逐行核四件事：五段 claim 齐、
每段 claim 记的 receipt 路径与 64 位摘要、那个 receipt 文件的字节确实哈希成那个摘要、反例段的 receipt 里
真的写了被拒的 `refusal_class` + `refusal_code` 并且它的原始 capture 在盘上。

```
population accepted=339 rows_in_claims=345 claims_total=1725
rows_missing_a_phase=0  receipt_digest_mismatches=0  counterfact_without_refusal_text=0
stale_pin_files_across_accepted_rows=0
controls wrong_digest_detected=True absent_receipt_path_detected=True real_receipt_detected=True
refusal_code=ALPHA_VANTAGE_BAD_RESPONSE
```

顺手得到一条独立的新鲜度结论：**已接受行的 pin 没有一个失效**——每份 receipt 里 `before_sha256 ==
after_sha256` 的被钉文件，其当前字节仍等于记录的摘要（`stale_pin_files=0`）。这就是「可复算」的实际含义：
树如果没有再动，落档的证据面今天仍能重放。

四条控制臂保证这些 0 不是谓词恒真：喂一个假摘要必须被抓到（`D`）、指向不存在的 receipt 必须被抓到（`E`）、
真 receipt 必须被认（`F`）、把真 receipt 的 `refusal_code` 抹掉之后谓词必须不再认（`C2`）。这条检查只能在
没有签名链在跑的时候做——它按 worktree 字节算哈希，链正在重写 receipt 时跑会变成我自己的仪器在制造红。

## 14. 一处没人管的计划工件：capability-roadmap 的 cboe 覆盖清单不随落地轮换

`docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/capability-roadmap.json` 的
`roadmap.shipped_cover_rows` 里 **cboe 一条都没有**；cboe 只出现在 `roadmap.greedy_build_order` 各步的
`newly_unlocked_rows` 里，列的是
`['EquityHistorical', 'EquityQuote', 'IndexHistorical', 'IndexSnapshots', 'OptionsChains']`。
也就是说 wave 37 落地的 `IndexSnapshots` 到今天还挂在「待建池」里，而本轮落地的 `FuturesCurve` 从一开始
就没进过这张计划的任何一列。

这不是判定探针，所以先说清楚为什么它不红：读这份 roadmap 的两个测试是
`tests/test_sec_engine_provider.py` 与 `tests/test_congress_gov_engine_provider.py`，两者按**自己的
provider** 过滤条目（两文件里 `cboe` 出现次数都是 0），因此 cboe 条目的陈旧不会在任何测试里暴露。
结论是「这份计划对 cboe 已经过期且无人核」，而不是一处回归；要修的是给 roadmap 的覆盖列补一次轮换
（或明确它是一次性历史快照并停止按 provider 引用它），这属于计划工件的整理，不影响本轮任何验收读数。
本轮只把它记下来，因为 C99 那条「按 roadmap 的 shipped_cover_rows 与 greedy_build_order 给波次定量」
的做法正依赖这份文件——依赖一条没人核的数,就是下一个错判的入口。

## 15. 撤回 §9 的框架：AC2-10 的 DEV_DONE 不是落后 333 行，而是已经打满它自己的定义

§9 把 `dev_done=17 / 350` 读成「平面 2 落后一大截，按行往前推台账标签就行」。读了判定它的代码之后，这个
框架是错的，撤回并留在这里而不是悄悄改掉。

`scripts/quality/ac2_case_probe.py:1267 witnessed_dev_done()` 的定义是两个条件同时成立：

1. `key in engine_declared_keys()` —— 该行是**声明式引擎**登记的模型（`catalog.engine_declared_models()`）；
2. `passing_contract_faces()` 给这一行的 positive>0 且 refusal>0 —— 而这份 faces 是**当场跑契约套件**得来的，
   不是读测试文件名（其 docstring 明确拒绝「只证明被提到」）。

实测这三个数（`opendata.data.providers.catalog` 直接问运行时，加判定报告对照）：

```
engine_declared_models = 17      declared_and_accepted = 17      两条件齐 = dev_done = 17
plane1_accepted = 339            accepted_but_not_engine_declared = 322
top handwritten providers: fmp=69 fred=36 intrinio=32 yfinance=28 tmx=24 sec=24
```

所以 `dev_done=17` 是这条谓词**当前的上限**，不是缺口：17 个引擎声明行全部有执行过的正反例契约面，
一行不缺；剩下 322 行是手写适配器，在这个定义下**永远不会**被计入 `dev_done`。

真正的图景是三件事，不是一条进度条：

* **平面 1（339 行）**：五阶段见证链，由 `w75` 逐行核过摘要与反例文本——这是手写适配器population 的验收机制；
* **AC2-10 的 `dev_done`（17 行）**：只覆盖声明式引擎 population，且已饱和；
* **台账的 `implementation_task_status`（5 DEV_DONE / 13 IN_PROGRESS / 332 NOT_RUN）**：第三套记账，
  目前没有任何验收机制读它来判定落地。

因此「把 333 行标签推到 DEV_DONE」是一个错误待办——那只会让台账标签与两套真实机制进一步脱钩。正确的下一步
是二选一并写进计划：要么为手写 population 定义与之相称的第二平面谓词（例如让 `witnessed_dev_done` 也接受
平面 1 的五阶段 receipt），要么明确 AC2-10 只度量声明式行、把 350 行的验收结论统一交给平面 1 与 `w75` 这条链。
`label_without_witness=0` 与 `dev_done_label=5` 这两个数也说明：现在标签比机制更保守，没有虚标。

## 16. 签完之后又被静态面改了字节：撤销半径是量出来的，而我第一版的面读了一个不存在的字段

时间线是这一节的全部内容：`w56_run_chain.py` 在 18:23 签完整棵树并把平面 1 抬到 339；A2 静态面随后在 18:37
把两个**被 pin 的输入**里的注释折行改掉（`opendata/data/providers/cboe/models/futures_settlement_public.py`
一处 E501、`scripts/quality/iter2_witnesses/_parts/cboe/futures.py` 三处 E501），外加
`opendata/data/models/__init__.py` 的 I001 导入顺序。改动里没有一行断言、字面量或控制流——但 sha256 是对
字节的承诺，字节变了承诺就失效。

半径必须由测量决定，不能由「我改的是 cboe」决定。`w76_cboe_resign.py` 走一遍
`docs/evidence/C101/claims.merged.json`：

```
被观察的 pin 站点 = 71   （shard 66 + adapter 5 + models/__init__.py 0）
落在 cboe 的 claim 上 = 55 条   foreign_provider = 0
models/__init__.py 被 0 条 claim pin —— I001 那次改动不需要任何重签
```

只有 cboe 拥有这些站点，而 witness 归属按设计是 provider-atomic 的，所以**只重签 cboe** 就是完整修复，
不需要第二次全树遍历（全树 ≈ 45 分钟）。

这一节真正要留档的是我自己的仪器缺陷。take 1 的失效判定读的是 `entry["sha256"]` / `entry["digest"]`，
而 producer 写的字段叫 `after_sha256`（`before_sha256` 在它旁边）。取到空串的两条后果是致命的：每一条被
观察的 pin 都「失效」，而且 before 与 after 一模一样——71 / 71。**一个不可能产生不同读数的面不是测量**，
它只是把同一个字符串比较了两遍。仓内自己的读者 `w75_witness_completeness_face.py` 早就在读正确字段，
我没有 import 它而是自己猜了字段名，这就是代价。

修正后的面带双臂，都在 `chain_summary.take2.txt` 里：

* 真实读数 `stale_sites_total=0`、`pin_entries_without_digest=0`；
* 内存伪造读数：把一条 `after_sha256` 换成 64 个 0（不落盘，deepcopy），同一谓词读出
  `stale_sites_total=1`、`stale_groups={"cboe:fixture": 1}`、`forge_applied=true`。

A–E 五个 arm 全 True；五个成员 exit 0；merge 幂等 `9be13bc6d7f9c1f5` == `9be13bc6d7f9c1f5`；重签后判定
报告仍是 350 身份 / 339 `OFFLINE_ACCEPTED` / 1725 claims（`INCOMPLETE` 6、`UNREVIEWED` 5）。伪造臂之所以
必须存在：修复已经把它本来要指认的缺陷吃掉了，现场只剩「已经修好」这一种读数。

因此有一件事**不能**声称：「18:37 那 71 处当时确实失效了」。它已不可复算——重签覆盖了描述它们的载体，而
take 1 的 71/71 因为字段名错而无效（载体留在 `notes/w76_cboe_resign_take1_vacuousface.txt`，
`chain_summary.take2.w76_first_vacuousface.txt` 是被取代的那一份）。可以声称的只有机械事实：那三个文件
的字节在签名之后被改过（`ruff format` 的 diff 就是证据），以及现在整棵树对最终字节是自洽的。

## 17. 测试平面：三台取数、一个字节 pin、以及 +4 个 collected 的归属

平面在同一棵树上跑了三台，外加一台 pre-wave 控制面。控制面是 `4071a4aa2`（wave 38 的 docs 提交）的 detached
worktree，并且先把 gitignore 掉的 `docs/evidence/C100/**` 复制进去——不复制的话它的 fence red 会因为「输入
缺失」这个**别的原因**变红，而 wave 37 正是把两个不同缺陷造成的相同计数说成了 pre-existing。

判定读数（`w69_test_plane_attribution.py`，9 个 arm 全 True，exit 0）：

```
final   = 28 failed / 26335 passed / 271 skipped   collected=26634
control = 28 failed / 26331 passed / 271 skipped   collected=26630
node-id 集合差 shared=28  new=0  fixed=0
```

计数相等本身不算证据，集合相等才算：`new=0` 与 `fixed=0` 同时成立，才说明这 28 条是**同一批** pre-existing
red，这一波没有引入回归、也没有顺手修掉谁。伪造臂在场（往集合里注入一条 `tests/synthetic.py::test_injected`
→ `new` 立刻读出 1），所以 `new=0` 不是空谓词。

collected 从 26630 抬到 26634 必须归属，不能留成没人解释的移动者。逐 id 相减读出 5 个新增、1 个消失：

* 4 个是 `tests/test_provider_conformance.py` 里 `[cboe10]` 参数实例——注册第 11 个 binding 会让每个
  parametrized 判定各多遍历一项（注册面会放大参数化判定，这是同一族现象）；
* 第 5 个新增配 1 个消失是改名：`test_pilot_records_the_one_model_without_reviewed_source_protocols`
  → `test_pilot_records_no_model_without_reviewed_source_protocols`（`NOT_DECLARABLE` 这轮被清空，见 §2）。

三台主树平面之间的差，是我自己制造的竞态，留档而不是抹平：A2 静态面在 10:37:36Z 重折了两个被 pin 文件的
注释（§16），而 take1 从 10:28:21Z 起跑，跑到 10:38:10Z——按文件顺序在 10:37:36 之前跑完的 fence 测试还是绿的，
之后的变红。take2（10:38:10Z 起）整台都在改后跑，fence 全红；final（11:07Z 起）在重签与重钉之后跑。实测每台
落在四个 fence 文件上的 red 数：

```
fence reds   take1=0   take2=7   final=0        take2_minus_take1 = 恰好那 7 个 node
```

7 个 red 只有一个原因。`tests/fixtures/registry_batch5_snapshot.json` 的 31 个字节 pin 里，
`opendata/data/models/__init__.py` 记的是 `3b5162b2…`，而 I001 修复把该文件变成 `4429da05…`；四个消费者共用
`_registry_snapshot()`，它逐条 assert 全部 31 个 digest 并把失败的 rel 打出来。单独跑那四个文件的隔离测量
（10:46Z）读出 `7 failed / 110 passed`，7 条全部死在这一个 helper 上。

修复交给仓内自己的仪器，而不是我手改常量：重跑 wave 38 的 rebaser（`w60_batch5_rebase.py` take2），它打印

```
pins entries=31 changed=2
pin opendata/data/domains.yaml            from=015edd5ca6a409f4 to=87e02b3da5277a28
pin opendata/data/models/__init__.py      from=ea5071c658d8032d to=4429da0506fc7b88
```

第一行是本波新增域键的正常移动，第二行才是这次折行的代价；它同时改写四个消费者文件里的字面量。我原本准备
手改 `tests/test_provider_domains.py:1475` 的那串 digest，脚本自带的断言 `old_literal_occurrences == 1` 读到
0 并拒绝写入——因为仪器已经改完了。这个拒绝就是「值来自生产者而不是来自我」的证据。之后四个文件
`117 passed`。

最后，`test_plane_w39_worktree_final_exit=1` 与 `test_plane_w39_control_exit=1` 都是 by construction：平面里
有 28 条 pre-existing red，exit 0 从来不是这一节的判定，集合差才是。

## 18. 出货闸门与被我改坏的两台仪器

`w83_a2_slice_wave39_retake.txt` 对本波全部 31 个 Python 载荷（5 个产品文件、1 个见证分片、7 个测试文件、
15 个 C145 探针、3 个 C144 探针）的读数：

```
ok   ruff check          ok   ruff format --check
ok   mypy (24 targets)   ok   bandit
FAIL pinned Gitleaks file scan        ← by construction
flagged=25  under_C100_pristine=25  outside_C100=0
```

被标记的 25 个文件全部是 `docs/evidence/C100/**/inputs/pristine/akshare/*.py`（第三方不可变捕获），
本波写入集里一个都没有——这三个数（25 / 25 / 0）是 grep 出来的，不是记忆的。

第一处仪器缺陷在 `w62_staged_credential_scan.py`。它 import wave 38 的扫描器并 re-point 它的 `CARRIER`，
但那个扫描器把 carrier 写在 **自己的 `__main__` 分支**里：被 import 后只调 `main()`，它把 face 打印出来、
一个字节都不落盘。于是 12:15Z 那次真实扫描（15 个 staged 文件，11/11 arms PASS，baseline 0、planted 2）
只有 /tmp 里的 stdout，没有档案——而 `w63` 收口面把这份扫描当前提。修法在包装器里，不动 wave 38 已提交的
仪器：包装器自己 tee stdout 并按本波 bundle 名落档。修好后 carrier 1417 字节、
`w62_staged_credential_scan_exit=0`，feat 载荷那一份另存为 `w62_staged_credential_scan.feat_payload.txt`。

第二处在 `w63_closure_face.py` 的下游：wave 38 的收口面在 `SCRATCH/"bundles"` 上做 dir-mode，而那个目录是
由 `_copy_committed()` 逐个文件顺带创建的。feat 提交只带 15 个代码路径、0 个 bundle 路径，于是 copied=0、
目录不存在，plant 步直接抛 `FileNotFoundError: /tmp/w63_closure/bundles/plant.txt`（carrier 留在
`w63_closure_face.feat_commit.stdout.txt`，红的那一次就摆在旁边）。这不是要改判定：它说明 dir-mode 这一路
**只对携带 bundle 的提交有意义**，所以收口面对象改成本波的 `docs(evidence)` 提交（2133 个 C101 + 58 个 C144
+ 127 个 C145），而 feat 提交的字节由同一个面的逐档对账一起覆盖（它也在 HEAD 里）。

四处 mypy 修正（`w57` 的 object 比较与两处 `Match|None`、`w71` 复用 `sample` 名字、`w74` 复用
`raw/deduped/scanned`、`w75` 的 `extend` vs `append`）都只动类型与变量名，改完把四台面重跑一遍证明读数没变：
`w75 10/10`、`w71 5/5`、`w74 7/7`、`w57 25/25`，全部 PASS。

`w75` 那一处值得单独说：`stale` 声明是 `list[list[str]]`（每行一组），而代码用 `stale.extend(gen)` 往里塞
**单个字符串**。这一轮读数没错，因为每一行的 stale 都是空——空生成器 extend 什么都不加，两种写法等价。
但一旦真有一条失效 pin，两种读法就分岔了：`flat_stale` 会去遍历字符串的**字符**，计数变成 pin 名的字符数；
arm H 的 `group == []` 则拿 str 跟 list 比，永远 False，于是那条 arm 会以「红」的方式告诉我们一个本来只是
数字错了的事实。mypy 把它报出来了，就改成 `append([...])`，不要再留「只在缺陷出现时才正确」的写法。
