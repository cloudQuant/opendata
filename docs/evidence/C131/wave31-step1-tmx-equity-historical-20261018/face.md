# wave-31 step 1 —— `tmx::EquityHistorical` 落地，plane 1 285 → 286/350

日期 2026-10-18（本机）。solo：子代理预算 30/30 在 wave-28 前已用尽。判定链一律用仓库自带的
producer → recorder → merge → judge，探针一律 `import` 自带判定器，不重写谓词。

## 1. 结论面（每一条都有载档行号）

| 读数 | 落地前 | 落地后 | 载档 |
|---|---|---|---|
| plane 1 `offline_acceptance_count` | **285** | **286**（/350） | `runs/wave31-step1-pin_drift_pre_production.txt:8`（pre_chain）、`runs/wave31-step1-judge.txt:5`（post_chain） |
| `status_counts` | INCOMPLETE 28 / ACCEPTED 285 / UNREVIEWED 37 | INCOMPLETE 27 / ACCEPTED 286 / UNREVIEWED 37 | 同上两行 |
| 身份集 | `5168e1e723999312` | `5168e1e723999312`（未动） | `runs/wave31-step1-judge.txt:5` |
| `claim_count` | 1565 | 1565（未动） | 同上 |
| `primary_review_count` | 285 | 286 | 同上 |
| tmx 已验收行 | 4 | 5 | `...pin_drift_pre_production.txt:10`、`...judge.txt:7` |
| 残留总数 | 65 | 64 | `runs/wave31-step1-residue_axis_census.txt:82` |
| 提交前的索引面凭据筛（72 枚 staged 路径） | 出厂闸门看不见（它读提交历史） | `findings=0 rc=0 unresolved=0`，另有能咬的 A/B/C 三臂 3/3 | `runs/wave31-step1-precommit_staged_scan.txt:5`、`runs/wave31-step1-payload_scan_falsification.txt`（§3.1） |
| 提交后的出厂闸复测（读 history，HEAD=`86fb93ac9`） | — | `secret_check_exit=0`，425 commits / 288.85 MB，`no leaks found` | `runs/wave31-step1-post_commit_secret_check.txt:4,6`（§3.2） |

`EquityHistorical` 五相全部 `exit=0 status=PASS`：`runs/wave31-step1-producer.txt:30-34`（argv 就在
同一行里打印）。recorder `signed=5 refused=0`，并显式记下 `re-signing: dropped 4 earlier review(s)`
——即 5 枚签署全部落在本代 claims 文档上：`runs/wave31-step1-recorder.txt:4-11`。

链路旁证（都是双臂可复算的，不是"跑过了"）：

- merge 覆盖 27 个 shard，`claims=1565 identities=313 reviews=286 accepted_identities=286`
  （`runs/wave31-step1-merge_take2.txt:32`），两次取的 stable payload 摘要相同：
  `take1=take2=c25afe5904864c51`（`runs/wave31-step1-merge_take2.txt:34`）。
- receipt-pin 面：1430 份 receipt、20463 个 pin 槽，`stale=0 before_ne_after=0`；强制臂把
  1430 个目标槽全部改脏并验 `must_equal_targeted=True`；re-arm 臂 `stale=0`。三臂 3/3
  （`runs/wave31-step1-receipt_pin_consistency.txt:5-8`）。
- pytest 四面 `709 passed, 179 skipped`，同 argv `--collect-only -q` 实测 **888 collected**
  = 709+179（`runs/wave31-step1-pytest_tmx_and_pin_planes.txt:17`）；没有消失的样本。
- 静态面：`ruff_check_exit=0`、`ruff_format_exit=0`、`mypy_exit=0`、`bandit_exit=0`。
- batch5 围栏 dry：`matched=31 stale=0 missing=0`，`verdict: REFUSED - no stale pin to re-base`
  （`runs/wave31-step1-batch5_fence_dry_census.txt:9-10`，`exit=1` 是这条 argv 的设计拒绝，不是红旗）。

## 2. 落地动的是什么（2 处产品码 + 1 处见证项）

`opendata/data/providers/tmx/models/fetchers.py`（新摘要 `sha256:16=6968b9ccdf10e1d4`）：

1. `row_model: ClassVar[type[TmxEquityHistorical]] = TmxEquityHistorical` —— 只声明，值就是
   `contract_model('tmx_equity_historical')` 解出的那个类（`domains.yaml:738-752`）。
2. 把窗口蕴含的行上限提为一等函数 `_history_limit(params)`，`extract_data` 用它发 `limit`；
   `transform_data` 在 `list_rows` 之后新增 `len(source_rows) > _history_limit(params)` →
   `TmxProviderError("TMX_BAD_ROWS")`，实测拒绝点 `fetchers.py:277:transform_data`。
   守卫不是新语义：它复用本包 `CompanyFilings` 已写过的同一条裁决
   （`fetchers.py:448:transform_data TMX_BAD_ROWS`），反例臂
   `C_ruling_parity ... shared=[('TMX_BAD_ROWS', 'transform_data')] PASS=True`
   （`runs/wave31-step1-declaration_counterfact.txt:16-17`）。

`scripts/quality/iter2_witnesses/tmx.py`（新摘要 `sha256:16=7b976e79887012e6`）：新增第 5 条 CONFIG
条目 `_equity_historical_document()` / `_history_row()` / `_HISTORY_DAY`+`_PRIOR_DAY` 常量，
tamper 为"一日窗里塞进第二日"，`refusal_code=TMX_BAD_ROWS`；docstring 里"four entries"改
"five entries"，并把 `_OPERATIONS` 的引用从 `client.py:64-116` 更正为 `client.py:40-117`。

守卫带来的可量差：同一行在判定器上从落地前 `raises=7 code_raises=0 counts={}`
（`docs/evidence/C130/plane1-action-map-rebase-20261018/runs/wave30-step3b-retarget_batch_prediction_post_land.txt:3`）
变成落地后 `raises=8 code_raises=1 counts={'transform_data': 1}`
（`runs/wave31-step1-declaration_counterfact.txt:10`）。

## 3. counterfact 与控制臂（为什么这个绿不是自证）

`runs/wave31-step1-equity_historical_prevalidation.txt`（scratch evidence root，不动 C101）：

- `:30` 25 腿相位全过：`passed=25 failed=0 blocked=0`。
- `:32` `CONTROL_LIMIT_ON_THE_WIRE`：一日窗 → `wire_limit=1`，两日窗同一份两行文档 → `wire_limit=2`
  且 `served=2`。这条把守卫从"行数>1 就拒"里区分出来。
- `:33` `CONTROL_COUNT_GUARD_BITES`：控制面 1 行服务，篡改面 `TmxProviderError code=TMX_BAD_ROWS`
  落在 `fetchers.py:277:transform_data`，与 witness 声明的 code 相同。
- `:34-35` `CONTROL_WINDOW_SCALING` / `CONTROL_ADDED_ROW_IS_WELLFORMED`：多塞的那一行单独服务成功
  （`date=2026-10-08 close=121.44`），即被拒的是"计数与窗口不一致"，不是那一行本身畸形。
- `:36` `CONTROL_TWO_BAD_ROWS_SITES ... distinct_frames=2`
  `['client.py:176:list_rows', 'fetchers.py:277:transform_data']`：`list_rows` 的上限拒绝与本行守卫
  是两个不同站点，counterfact 不会退化成"同一个 raise 测两遍"。
- `:37` `CONTROL_TAMPER_BITES` 5/5 `match=True`（其余 4 枚 tmx 行的声明码未被本次编辑改掉）。

`probes/declaration_counterfact.py` 的全部结论由**仓库自带的**四轴判定器
`incomplete_row_guard_check._row` 产出，只是把 `row_model` 在内存里删掉再装回：

- `:10` `A_declared ... verdict=DOCUMENT_REACHABLE declared=True counts={'transform_data': 1}
  trippable=['fetchers.py:277 ...']`，且 `judge_status=OFFLINE_ACCEPTED`。
- `:13-15` `B_declaration_removed verdict=ROW_CLASS_NOT_CONTRACT` → `B_restored DOCUMENT_REACHABLE`
  → `B_MOVES PASS=True`。落地与未落地在这台仪器上是可差的，且差的正是那一条声明。

### 3.1 提交前的索引面凭据闸门（`make secret-check` 看不见的那一刻）

出厂闸门 `make secret-check` 走 `gitleaks detect --source .`，读的是**提交历史**，所以
"已 stage、尚未提交"正是凭据形状还便宜可修、却没有任何出厂闸门看得见的那一刻。本波沿用
wave-28/30 的两台仪器，作用对象是本次 index：

- 总体筛：`docs/evidence/C128/plane1-residue-readiness-20261017/probes/staged_payload_scan.py`
  直调（不经 xargs/管道，所以 exit 是这条命令自己的）——
  `runs/wave31-step1-precommit_staged_scan.txt:5`
  `payload_scan_files=72 payload_scan_roots=72 payload_scan_unresolved=0 payload_scan_findings=0 payload_scan_rc=0`。
  自指边界（不能消除，只能写出来）：这一把的总体就是提交清单的 72 枚路径，而它自己的载档是这把
  之后落盘的，本节也是——所以"72 枚干净"覆盖的是当时的字节，最后两份文件由我逐字看过、不含凭据形状。
  `non_ascii_staged_paths=0` 现测于本清单，`quotepath=off` 仍留在 argv 里，因为它在别的日子会咬。
- 这枚 0 配了能咬的臂（波 30 的 `payload_scan_falsification.py` 移植到本波目录，
  载体文件名/标签/临时前缀三处随之改名，机制一字未动）：
  `runs/wave31-step1-payload_scan_falsification.txt` A 臂 `findings=1 rules=['github-pat']`、
  B 臂干净副本 `findings=0`、C 臂拼接体 `findings=0`，`arms=3 arms_passed=3 PASS=True`。
  诚实边界：A 臂的宿主取 staged 清单前 3 枚（`captures/tmx/*/contract-exit0.txt` 等，载档 `:4`
  `hosts=3`），它证明的是**屏会咬**，不是"这 3 枚是总体"——总体由上一条的 72 覆盖。
  （这一句上一版写的是"67"。67 在任何载档里都不存在，是我凭 wave-30 的印象打的一个数；
  现按 `precommit_staged_scan.txt:5` 改回现测的 72。见 §7 第 5 条。）
- 我这一步自己的错见 §7 第 4 条（首版 lint 总体漏掉 4 枚探针）。

### 3.2 提交之后的出厂闸复测（把自指的那一段补上）

索引面那一把扫的是 stage 那一刻的字节；§3.1 与 face.md 在那之后又被改过，所以"72 枚干净"
不覆盖最终留在历史里的那份 payload。提交后跑出厂闸的那个成员（`scripts/quality/secret_scan_check.py`
→ `gitleaks detect --source . --config .gitleaks.toml --redact`，读的是**提交历史**，HEAD 即本波提交）：

- `runs/wave31-step1-post_commit_secret_check.txt:4` `head=86fb93ac9 secret_check_exit=0`，
  `:6` `gitleaks_inf=425 commits scanned. | scanned ~288847332 bytes (288.85 MB) in 1m43s | no leaks found`。
- 这一把的载档自身又在这把之后落盘，所以它是这条链条上唯一没有被关闭的自指：它只是散文，
  不含凭据形状，且它已经在下一把的历史里（下一波提交前的索引面会扫到它）。

## 4. 两枚判定器伪影（发布，不动判定器）

两枚都在本次落地路径上，都会被下一波的选行读错，所以必须带控制面公开，而不是绕开：

1. **AST 构造读取看不见 tmx 的间接构造。** `incomplete_row_guard_check._row_class_face` 用
   `declare_candidates._constructed` 扫类体内的 dataclass 构造，只展开一层 helper；tmx 走
   `_row(model, ...)`，于是 `emitted=[] is_contract=False`
   （`runs/wave31-step1-declaration_counterfact.txt:20`），而同一行的 runtime 读数
   `runtime_is=True`（`:12`），五枚 tmx 行全部如此
   （`runs/wave31-step1-equity_historical_prevalidation.txt:31` `runtime_identity=True`×5）。
   同一行的两台仪器互不相让：wave-30 的预测探针在
   `docs/evidence/C130/plane1-action-map-rebase-20261018/runs/wave30-step3b-retarget_batch_prediction_post_land.txt:3`
   打印 `shipped_emitted=[]`（与伪影同源）却同时打印
   `builder_built=['opendata.data.models.tmx.TmxEquityHistorical'] builder_sites=['fetchers.py:285 builder']
   contract_constructed=True`，即构造确实落在契约类上，只是换了一条 helper 通道。
2. **`CLIENT_MODULE_SEGMENT = "_client"` 不匹配 tmx 的 `models/client.py`。** 于是
   `client_names=[]`、`doc_sites_after=[False]`（同一载档 `:20-21`），transport 之后的 raise 被静音；
   对照件是这台仪器自己用来 gate 的两枚 government_us 行：`client_names=['_client']` 且
   `doc_sites_after` 里出现 `True`（`:22-23`）。

读法：tmx 行的 `ROW_CLASS_NOT_CONTRACT` 只能读作"未声明 `row_model`"，不能读作"发了相似类"；
`after=False` 不能读作"raise 在 transport 之前"。本次落地没有用这两条伪影换任何绿色：#1 由
`declared=True` 绕过（verdict 阶梯先看 declared），#2 未参与（counterfact 的 code 与站点由
prevalidation 直接量 traceback 得出）。修仪器不在本步范围：它有 124+256 行 byte-pin 的前科，
需要单独一代重签。

## 5. 对 wave-31 step 0 §4 定价的更正（这步是我的错，原读数留在档里）

step 0 §4 写的是："19 行 `implement_missing_model` 里，`government_us::TreasuryPrices` 与 16 枚 tmx 行
是 `SEAM_REACHABLE`、`module=True`……最便宜的一枚先在这 19 行里量成本。" 实测两头都不成立：

- **本波真正落地的行不在这 19 行里。** `tmx::EquityHistorical` 落地前的 action 是
  `retarget_fetcher_output`（`docs/evidence/C130/plane1-action-map-rebase-20261018/runs/wave30-step5a-residue_axis_census_finalgen.txt:19`），
  落地后该桶 5 → 4（`runs/wave31-step1-residue_axis_census.txt:82`），`add_code_carrying_guard`
  仍 13、`implement_missing_model` 仍 19。
- **19 行逐行都是 `verdict=UNBOUND`**，即注册表解不出 fetcher：16 枚 tmx 见
  `runs/wave31-step1-residue_axis_census.txt`（逐行 `bucket=NEED_BINDING verdict=UNBOUND`），
  `government_us::TreasuryPrices`、`yfinance::AvailableIndices`、`yfinance::FuturesCurve` 在
  `bucket/verdict` 上同形（但机制不同：tmx 16 枚与 TreasuryPrices 是
  `seam=SEAM_REACHABLE mechanism=client_seam`，yfinance 两枚是 `seam=SEAM_NEVER_USED
  mechanism=engine_transport`，见 `runs/wave31-step1-residue_axis_census.txt:10/39/57`）。
  所以"声明 + 见证"这条链对这 19 行**完全无效**，它们要的是代码本身。
- 错因是单位错配：`SEAM_REACHABLE` 是 seam census 的 **provider 级**读数
  （`runs/wave31-step1-seam_reachability_census.txt:22`：
  `seam_census_provider=government_us bucket=SEAM_REACHABLE ... rows=6 accepted=4 residual=2`），
  而同一条里 residual=2 的**两枚行不同类**：`government_us::TreasuryPrices` 是
  `bucket=NEED_BINDING verdict=UNBOUND action=implement_missing_model`，
  `government_us::WeatherBulletinDownload` 是 `bucket=NEED_DECLARE verdict=NO_CODE_SITE
  action=add_code_carrying_guard`（已绑定：`name_in_package=True`），见
  `runs/wave31-step1-residue_axis_census.txt:10-11`。provider 级读数既不给行定价，也不给行分类。
- 一条已量的硬约束（不是猜测）：治理客户端只发 JSON body ——
  `opendata/data/http_client.py:426-436`（`request(..., json_body=...)`）、`:753-754`
  （`request_kwargs["json"] = json_body`），整个文件没有表单 body 通道。因此这 19 行里任何需要
  非 JSON body 的端点，在拓宽引擎之前连"可服务的缝"都不存在。**逐行 body 形状本步未测
  （NOT MEASURED）**，留给下一波按行量，而不是按 provider 量。

## 6. 残留 64 行的行动图与下一波谓据

`runs/wave31-step1-readiness_census.txt:7`：`residue=64 bound=32
buckets={'NEED_BINDING': 32, 'NEED_DECLARE': 17, 'NEED_SHARD_ENTRY': 15}`（列和=总数，臂 3/3）。
`runs/wave31-step1-residue_axis_census.txt:82`：actions 六桶
19/15/13/11/4/2 = 64，`witnessable_today=0`，臂 8/8。
`runs/wave31-step1-seam_reachability_census.txt:40`：按机制
`client_seam 25 + engine_transport 37 + new_provider_package 2 = 64`，`accepted 286 + 64 = 350`。

下一波谓据（写在这里，不写完再补测）：

1. **最便宜的两枚仍是 tmx 的 `NEED_DECLARE / ROW_CLASS_NOT_CONTRACT` 行**：
   `tmx::HistoricalDividends` 已带 6 个有码站点（`fetchers.py:476/484/486/490/493/496`，
   `TMX_BAD_PAGE`×4、`TMX_PAGE_LIMIT`、`TMX_BAD_ROWS`，见
   `runs/wave31-step1-incomplete_row_guard_check.txt` 的 `doc_site` 行），所以它只需要
   声明 + 见证项 + 一个既有码的 tamper；`tmx::IndexConstituents` 是 `raises=7 code_raises=0`，
   要先写新裁决，成本高一档。谓词（两枚都落地后，按总体分别给界）：plane 1 286 → 288；
   `incomplete_row_guard_check` 的 27 枚 INCOMPLETE 总体
   `ROW_CLASS_NOT_CONTRACT` 3 → 1（`runs/wave31-step1-incomplete_row_guard_check.txt:95`，
   现存 3 枚 = FomcDocuments / HistoricalDividends / IndexConstituents）；
   `residue_axis_census` 的 64 枚残留总体 4 → 2（`runs/wave31-step1-residue_axis_census.txt:68`
   的 `('NEED_DECLARE','ROW_CLASS_NOT_CONTRACT'): 4`；多出的第 4 枚 `yfinance::KeyMetrics`
   状态是 UNREVIEWED，不在前一个总体里，这两枚 tmx 落地不会让它动）。
2. **失效条件**：若某枚落地后 judge 不动，先按本波
   `runs/wave31-step1-declaration_counterfact.txt:12` 的 `runtime_is` 测法确认 served row 是否
   真是 `contract_model(domain)`；若真是而 verdict 仍不动，则 §4 伪影 #1 已把该行错档，属修仪器，
   不属改判定器。
3. **失败读法**：producer 里该模型任一相 `exit≠0` 时，先查 `row_model` 与 payload 键集
   （service 腿要求键 ⊆ `row_model.model_fields`），**不要**放宽 counterfact 的声明码。
4. `yfinance` 15 枚 `NEED_SHARD_ENTRY` 已 `declared=True verdict=DOCUMENT_REACHABLE`，缺的是见证项
   本身，属见证成本；11 枚 `ledger_ruled_out` 与 2 枚 `new_provider_package` 本波不动。

## 7. 我错的部分（保留在档，不改写）

- 上一段会话我说 `EquityHistorical` 是 `NO_CODE_SITE`；shipped judge 落地前实测
  `verdict=ROW_CLASS_NOT_CONTRACT`。我把 C130 的 `sim_after_retarget=NO_CODE_SITE`
  （`docs/evidence/C130/plane1-action-map-rebase-20261018/runs/wave30-step5b-retarget_batch_prediction_finalgen.txt:3`）
  当成了当前臂读数。按探针源码 `retarget_batch_prediction.py:407`，那一臂是
  `axes._verdict({**face, "declared": True, "is_contract": True})`，即**只模拟"补上声明"**之后的读数
  （当时 `counts={}`、`trippable_sites=0`，所以补声明只到 `NO_CODE_SITE`）；同一行
  `sim_unflipped=ROW_CLASS_NOT_CONTRACT` 是 `declared/is_contract` 都不翻的对照臂，
  `job=declare_plus_guard` 才是本行要做的活。也就是说这两枚读数一致预言了我本波的做法
  （声明 + `transform_data` 计数守卫），是我没读定义就引用了标签。
- witness docstring 里 `client.py:64-116` 从未覆盖 `getCompanyPriceHistory` 的选择列，正确区间
  `40-117`；已在本波更正（散文引用要按总体扫，不是抽查）。
- 落地前的 pin 税先测为 2 枚 stale（`fetchers.py 50ca1c466a16fd6a → 6968b9ccdf10e1d4`、
  `iter2_witnesses/tmx.py 5311773e92c4eace → 7b976e79887012e6`，
  `runs/wave31-step1-pin_drift_pre_production.txt:5-7`），链路跑完后同 argv 复测
  `pinned_files=14 stale_files=0`（`runs/wave31-step1-pin_drift_pre_production_rerun.txt:5`）。
  如果只重产 `EquityHistorical` 一行、不重产整个 tmx shard，那 4 枚已验收行会立刻变成
  `claim_receipt_hash_drift`，plane 1 会先跌后涨。
- 链路里的 lint 三件（`ruff_check` / `ruff_format` / `mypy`）总体是**两枚代码文件**，4 枚本波探针
  从没进过那个总体，而它们是要提交的。提交前把总体换成 4 枚探针重测，第一把就红：
  1 条 E501（移植时把 wave-30 的长路径标签抄长了）+ 5 条 mypy
  （`w31s1_run_chain.py` 一处 `var-annotated`、`equity_historical_prevalidation.py` 一处
  `unused-ignore` 和三处 `no-any-return`）。红的那把留在档里，绿的是
  `runs/wave31-step1-new_probes_lint.txt`：`lint_face steps=3 failed=0`。
  改完字节后又复测一次判定面（`runs/wave31-step1-post_probe_edit_recheck.txt`）：
  34 枚判定行逐位相同（`differing_positionwise=0`），`PREVALIDATION_DONE
  phase_failures_or_blocked=0`，同面 pin 读数 `pinned_files=14 stale_files=0`——
  这 4 枚探针不在 shard 的 pin 清单里（那份清单是 9 枚仓内源文件 + C101 自身的 fixture/receipt），
  所以改它们不产生重签税。
- §3.1 的诚实边界那句里我写了"总体由上一条的 67 覆盖"。**67 不存在于任何载档**——它是我按
  wave-30 的印象打的一个数，而 wave-30 自己的现测是 92/94
  （`docs/evidence/C130/plane1-action-map-rebase-20261018/runs/wave30-step8a-precommit-staged-scan.txt:23,73`），
  两个都不该被抄进本波。本波总体由 `precommit_staged_scan.txt:5` 现测为 72，已按载档改回。
  同类错误在这一步还有第二处：`post_commit_secret_check` 第一把的载档里 `stderr_inf_lines:` 是空的
  ——gitleaks 的 stderr 带 ANSI，`INF` 后面紧跟 `\x1b[0m` 而非空格，`"INF " in line` 一条也没接住，
  于是写进去一条"看起来像读数的空"。重取时先剥 ANSI 再过滤，并 `assert len(inf)==3` 且其中一条
  含 `no leaks found`；错的那把留在同一载档的自述里。

## 8. 载档清单（每条自带 argv 与 `<name>_exit=<digit>`）

`runs/` 25 份：`producer`(127 行) `recorder` `merge_take1` `merge_take2`(含 idempotency) `judge`(含
pre/post 两代面) `declaration_counterfact` `equity_historical_prevalidation`
`incomplete_row_guard_check`(96 行，臂 6/6) `readiness_census` `residue_axis_census`(83 行)
`seam_reachability_census` `receipt_pin_consistency` `batch5_fence_dry_census` `ruff_check`
`ruff_format` `mypy` `bandit` `pytest_tmx_and_pin_planes` `pin_drift_pre_production`
`pin_drift_pre_production_rerun` `precommit_staged_scan` `payload_scan_falsification`
`new_probes_lint` `post_probe_edit_recheck` `post_commit_secret_check`，共 638 行 / 91330 字节
（`wc -lc runs/*.txt` 现测）。
`probes/` 4 份 / 1113 行：`w31s1_run_chain.py`（链驱动，`--stage pin-drift` 是真参数，可单跑）、
`declaration_counterfact.py`、`equity_historical_prevalidation.py`、
`payload_scan_falsification.py`（波 30 同名仪器移植，改名三处：载档文件名、标签、临时前缀）。

## 9. 未做 / 仍开

17 成员 `make gate` 整跑仍未做过（因此不声明 gate 绿）；plane 2 的 DEV_DONE 对本行未重取
（step 0 已给 19 行那批取了面）；#33 yfinance 声明裁决、#68 FomcDocuments 7-vs-5 字段面、
#69 producer 显示标签、#60 batch5 围栏固定 15 条 gitignored 路径、§4 两枚伪影的仪器代、
以及 286 枚已验收行的 ModelSpec 架构决策，都还没做。无 push 授权：本波只本地提交。
