# C127 —— intrinio 两条 forward 行的 row_model 声明与五行相见证（迭代2 / 波 22）

日期：2026-10-17。判定口径：仓库自带 judge（`scripts/quality/iter2_model_evidence.py`）在
`docs/evidence/C101/claims.merged.json` 上跑出的 350 恒等式读数，不是任何手算子集。
本波面：**plane 1 258 → 260/350**，冻结信号 `identity_set_sha256[0:16] = 5168e1e723999312` 不变。

## 1. 本波改了什么（产品面 2 行，见证面 2 行）

迭代2 的 runner 在两处要求"名字与实发类型同一"，都在见证之前就已存在：
`_run_direct_with_seam` 断言 `type(row) is fetcher_type.row_model`
（[iter2_claims_probe_runner.py:302](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_claims_probe_runner.py#L302)、
[:332](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_claims_probe_runner.py#L332)，
`direct` 与 `service` 两相共用这台仪器），`contract` 相另在
[:542](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_claims_probe_runner.py#L542)
重assert 一次，并在
[:537](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_claims_probe_runner.py#L537)
要求这个类的 `__module__` 落在 `opendata.data.models.` 之下 —— 即声明的必须是冻结契约模块里的类，
不是 provider 本地的类。这两个门合起来才是"声明不能是贴标签"的机器形式。

实测三个类的定义模块（决定哪扇门会拦谁）：

```
IntrinioForwardSalesEstimate    __module__ = opendata.data.models.intrinio_financials        ← 契约面
IntrinioForwardEbitdaEstimate   __module__ = opendata.data.models.intrinio_financials        ← 契约面
_SalesEstimateRow               __module__ = opendata.data.providers.intrinio.models.iter2_financials  ← provider 本地
```

所以 Sales 那行在本波之前过了不了 `direct`/`service` 的门：`type(row) is row_model` 读 False
（实发的是契约类，声明的是线类）。这一条不是推理，是臂 D 量出来的 —— 它把 `row_model` 在内存里打回
`_SalesEstimateRow`，runner 必须抛 `WitnessFailure`（读数见 §4 的 D 行）。
另有一条按 `:537` 的**条件**可读出、本波未单独测量：线类不在 `opendata.data.models.` 下，
所以就算反过来把实发类型改去迁就声明，`contract` 相也会拒绝。两个方向都堵着，
"声明"这件事才不能靠贴标签完成。

| 恒等式 | 本波之前 | 本波之后 | 位置 |
| --- | --- | --- | --- |
| `intrinio::ForwardSalesEstimates` | `row_model = _SalesEstimateRow`（wire 类，非契约类） | `row_model = IntrinioForwardSalesEstimate` | [iter2_financials.py:1446](file:///Users/yunjinqi/Documents/new_projects/opendata/opendata/data/providers/intrinio/models/iter2_financials.py#L1446) |
| `intrinio::ForwardEbitdaEstimates` | `row_model = None`（什么都不发布） | `row_model = IntrinioForwardEbitdaEstimate` | [iter2_financials.py:1533](file:///Users/yunjinqi/Documents/new_projects/opendata/opendata/data/providers/intrinio/models/iter2_financials.py#L1533) |

Sales 那一行是本波最容易被误判的一种：它**不是**缺声明，是**声明错了类** —— 它把 wire 类
`_SalesEstimateRow` 当作发布类型，而 `_map_estimate` 明明已经把每个格子抄进了
`IntrinioForwardSalesEstimate`。所以本波买到的不是"多了一个标签"，是标签追上当前的真实发射类型。

见证分片 `scripts/quality/iter2_witnesses/_parts/intrinio/financials.py` 从 8 行增到 10 行：
两行都带 `client_seam`、`query {"symbol":"AAPL","period":"annual","limit":1}`、
五行相（`implementation`/`direct`/`service`/`contract`/`counterfact`）与一条 tamper。

## 2. 五行相矩阵（10 条 claim，全部 exit=0）

`docs/evidence/C101/intrinio/claims.json` 里这两行各 5 相，逐条读 `status` 与 `exit_code`：

```
ForwardSalesEstimates  implementation PASS 0 | direct PASS 0 | service PASS 0 | contract PASS 0 | counterfact PASS 0
ForwardEbitdaEstimates implementation PASS 0 | direct PASS 0 | service PASS 0 | contract PASS 0 | counterfact PASS 0
```

provider 全量重产后：`intrinio` 190 条 claim = **185 PASS + 5 BLOCKED**，0 FAIL。
5 条 BLOCKED 属 `OptionsSnapshots`（`bound=False`，注册面未绑定，本波之前就是红的，见 §6 残留表），
本波没有把它一起报成绿。

merged 载体：1335 条 claim、260 条 primary review、267 个有 claim 的恒等式。
judge：350 恒等式，`offline_acceptance_count=260`，
`status_counts={INCOMPLETE: 7, OFFLINE_ACCEPTED: 260, UNREVIEWED: 83}`（和为 350），`global_issues=[]`。

## 3. 两个 counterfact 面（逐字取自回执，不是取自我写的分片草稿）

载体：`docs/evidence/C101/receipts/intrinio/<Model>/counterfact.json` 的 `capture.stdout_excerpt`。
两行都是「先跑未篡改对照（`observed == expected` 的前半），再跑篡改」。

### 3.1 Sales：`mean` 写成美分 → `INTRINIO_FINANCIALS_CONTRACT_INVALID`

篡改格：`responses[0].body.estimates[0].mean = 415_300_000_000.5`（`op=set`，`column=mean`）。

```
expected = observed = {refusal_class: IntrinioFinancialError,
                       refusal_code: INTRINIO_FINANCIALS_CONTRACT_INVALID,
                       status: null, rejected: [], missing: [], invalid: []}
```

理由句（回执原文）：wire 页用 `_SalesEstimateRow` 解码，它的 `mean` 是 `FinancialValue`，
任何有限 JSON 数都收；而 `_map_estimate` 把这个格子抄进
`IntrinioForwardSalesEstimate.mean` —— 一个 `FinancialAmount`，只收 JSON 整数或 null。
所以一份以美分标价的共识均值是**先解码、再由输出阶段拒绝**，而不是被静默四舍五入成一美元。
这条反例之所以咬得住，正因为契约面严格窄于线面 —— 也正因为如此，§1 里那次
「把声明从 wire 类改成契约类」是同一件事的两半。

### 3.2 Ebitda：`consensus_type` 改成 `ebit` → `INTRINIO_FINANCIALS_CONSENSUS_TYPE_MISMATCH`

篡改格：`responses[0].body.ebitda_consensus[0].consensus_type = "ebit"`（`op=set`）。

```
expected = observed = {refusal_class: IntrinioFinancialError,
                       refusal_code: INTRINIO_FINANCIALS_CONSENSUS_TYPE_MISMATCH,
                       status: null, rejected: [], missing: [], invalid: []}
```

理由句：`_EbitdaEstimateQuery.estimate_type` 命名的是请求所要的那**一个** consensus
（`type=ebitda` 进了查询串），`extract_data` 在年/季过滤之前先把每行的 `consensus_type`
与它比对。所以一份用 EBIT 共识来回答 EBITDA 请求的正文是
`CONSENSUS_TYPE_MISMATCH`，而不是「在 ebitda 列里顺手发布了第二个指标」。
这个码在本波之前只存在于代码里，没有任何测试或证据面见过它 —— 本波是它第一次有见证。

这个码在波前的分布是量出来的，不是回忆的：`git grep -n CONSENSUS_TYPE_MISMATCH HEAD` 命中 **1 处**，
在 [iter2_financials.py:1558](file:///Users/yunjinqi/Documents/new_projects/opendata/opendata/data/providers/intrinio/models/iter2_financials.py#L1558)
的那一次 `raise`；测试面与证据面都没有它。本波把它从「代码里有、证据里没有」转成「证据里有」。

## 4. 四臂判定探针

`probes/plane1_delta_row_model.py`（sha16 `3e59d9d95bfe36be`），读数载体
`runs/plane1_arms.txt`（sha16 `3a155492a1951eee`），末行 `plane1_arms=4 plane1_passed=4 plane1_failed=0`、
`probe_exit=0`。四臂都 import 仓库自己的装载器与 judge/runner，不重新实现判定。

* **A_delta_census**：`accepted_old=258 accepted_new=260 ADDED=[两条] REMOVED=[] populations=350/350
  identity_set=5168e1e723999312 unchanged=True`。ADDED/REMOVED 双向都打，因为这是一条**集合**断言：
  +1 若是靠别处一次标签翻转变来的，只有 REMOVED 列表能暴露。
* **B_pin_census**：`shard_declared_rows=10 shard_row_claims=50 row_claims_pinning_shard=50
  distinct_shard_digests=1 on_disk_sha256=50b40998089fd53a digest_matches_disk=True statuses=['PASS']`。
* **C_phase_floor**：六个载体过仓库 judge 子进程 ——
  `control 260 / sales_no_claims 259 / sales_no_review 259 / ebitda_no_claims 259 /
  ebitda_no_review 259 / both_no_claims 258`，`wave_rows=2/1/1/1/1/0`，
  reviews `260→259` 只在接受方被摘掉时动，population 恒为 350。
  即：一行需要**claim 与 review 两半**才成立，claims 齐全的 PASS 只是候选。
* **D_row_model_identity**：`contract_model(domain) is fetcher.row_model` 且实发行
  `type(row) is row_model`，两行各 `rows=1/1`；随后**内存里 re-arm**：把 `row_model` 打回
  波前的 wire 类必须让 runner 抛 `WitnessFailure`，打回 `None` 必须让
  `witness_implementation` 拒绝，恢复后重新证绿 `green_after_restore=True`。
  这条臂是给「仪器自己坏掉」用的：如果 D 只是复述当前事实，它无法区分
  「产品是对的」与「仪器不再检查」。

### 4.1 臂 B 第一版自己的红，以及为什么它是分母而不是判据

第一版把门写成「全 provider 里 pin 这个分片的 claim 数 == 5 × len(MODELS)」，读到
**185**，臂直接 FAIL。真实形状是：producer 把一个 provider 的**每个** part 文件 pin 在该
provider 的**每条** claim 上。于是一枚分片编辑的爆炸半径是 provider 级的，不是行级的 ——
185 条 claim 的名字里都出现了这一枚文件（那 5 条 BLOCKED 不带 probe pin，190−5=185 对得上）。

改法：门收窄到分片自己的行（population 由分片 `MODELS` 键集导出，50 条），185 作为
**fan-out 分母打印**、进门条件之外。这也解释了为什么本波只加分片不必跑 22 家全量重签，
而改共享仪器时必须跑。

### 4.2 一处预测被读否证：波前标签是 INCOMPLETE，不是 UNREVIEWED

我预测这两行在 HEAD 里读 `UNREVIEWED`（无 claim ⇒ 未评审）。臂 A 打的是
`old_statuses=['INCOMPLETE', 'INCOMPLETE']`。judge 的优先级是
STALE → 无 claim 则 UNREVIEWED → 缺相/非 CANDIDATE 则 INCOMPLETE → REJECT → ACCEPT；
这两行在波前各有 5 条 BLOCKED claim，**有** claim，所以走的是 INCOMPLETE 分支。
本波面里凡引用这两行的波前状态，一律写 INCOMPLETE。这条更正的形状与既往教训同源：
登记的判断必须是读数，不能是对判定优先级的复述。

## 5. 下一波形状普查（本波新落的一条结构性读数）

`probes/next_wave_producer_coverage_census.py`（sha16 `a7acb73790759659`），载体
`runs/next_wave_producer_coverage_census.txt`（sha16 `1db45d3b86cca689`），`census_exit=0`。

把 90 条未接受恒等式与**实测的 producer 覆盖**（`docs/evidence/C101/*/claims.json` 是否存在，
从文件系统读，不手打）join，等式两边精确对齐：

```
produced_providers=22  never_produced=10
UNREVIEWED=83  其中属于 never-produced provider 的=83  属于 produced 的=0
INCOMPLETE=7   其中属于 produced provider 的=7      属于 never-produced 的=0
residue_by_kind={'never_produced': 83, 'produced': 7}   83+7=90
```

所以 plane 1 剩下的 90 行是**两类互不相干的工作**，不是一类：

* 83 行在 10 个从未跑过 producer 的 provider 里：
  `biztoc, deribit, eia, government_us, nasdaq, seeking_alpha, stockgrid, tmx, wsj, yfinance`。
  在那里任何见证之前，先要有 provider 级的生产运行。把它们统称「还差 witness」会低估工作量。
* 7 行在已生产的 provider 里（cboe 2、econdb 2、federal_reserve 2、intrinio 1），
  加分片就能触及 —— 这才是本波那种波形可以直接复制的部分。

臂 `B_control_flip` 证明这条 join 不是常量：只动覆盖集、不动状态与恒等式，
从 produced 里摘掉 `cboe` ⇒ 2 条 INCOMPLETE 被误归为未生产（P2 红、P1 仍绿）；
把 `biztoc` 加进 produced ⇒ 1 条 UNREVIEWED 被误归为已生产（P1 红、P2 仍绿）。
`drop_reds_only_p2=True add_reds_only_p1=True`，未扰动那一跑必须仍绿。
两个「另一侧仍绿」子句在 arm A 前提下是结构必然，打印出来是为了让读者看见扰动落在分割的哪一侧。

`intrinio` 剩下的那 1 行是 `OptionsSnapshots`，`bound=False`：它需要注册面绑定，
那是冻结面，不是一枚分片。本波不把它说成「下一波顺手能收」。

## 6. 预测与复算配方（含失效条件与失败读数）

波 21 的谓词（`docs/evidence/C125/sloos-witness-20261017/probes/next_wave_predicate.py`）在本波前
预测：加这 2 行 ⇒ accepted 258→260、residue 92→90、`PREFLIGHT_RESIDUE` 2→0、
intrinio 残留 3→1。本波实测（载体 `runs/next_wave_predicate.txt`，sha16 `08028e82c7120017`，
`predicate_exit=0`）逐条对上：`identity_count=350 accepted=260 residue=90`、
`PREFLIGHT_RESIDUE=0`、`SHARD_ONLY_ROWS=0`、`intrinio residue= 1`、`NEXT_WAVE_BUCKETS_OK=True`，
正向对照 `fred::SeniorLoanOfficerSurvey grant=6 expected_grant=6` 仍在（仪器必须看得见本波跑过的那次 grant）。
残留桶直方图只剩两种形状：`bound=True row_model=False witness=False preflight=False grant=NO_WITNESS` 58 行，
`bound=False ...` 32 行。**58 + 32 = 90**，且 90 行全部 `grant=NO_WITNESS`。

下一波（波 23）的谓词与失效条件：

* 若 §5 的两类划分仍成立（重跑普查即知），最小的本波形波是
  **已生产 provider 里的 7 行**；其中 `bound=True row_model=False` 的 2 行（federal_reserve）
  与波 16/22 同形 = 声明 + 分片。`bound=False` 的 5 行（cboe 2、econdb 2、intrinio 1）
  需要注册绑定，不属同形，别把它们混进一个波。
* 谓词的空化条件：一旦某 provider 补跑过 producer，它的行就从 UNREVIEWED 侧消失，
  §5 的 83/7 划分随之漂移 —— 那一行数字是一次读数，不是长期事实。
* 失败读数：若某波 `accepted` 增加但 `REMOVED` 非空，臂 A 直接红，说明 +N 里含一次标签翻转；
  若 `digest_matches_disk=False` 而计数仍绿，说明用的是陈签名（臂 B 的失效形状）。

复算配方（`$PY = /Users/yunjinqi/opt/anaconda3/envs/py313/bin/python`，仓库根，`PYTHONPATH=.`，
每步独立取 exit，不要用管道吞返回码）：

```bash
cd /Users/yunjinqi/Documents/new_projects/opendata && export PYTHONPATH=.
$PY scripts/quality/iter2_claims_producer.py --provider intrinio \
    --evidence-root docs/evidence/C101 --output docs/evidence/C101/intrinio/claims.json
$PY scripts/quality/iter2_primary_review_recorder.py \
    --claims docs/evidence/C101/intrinio/claims.json \
    --output docs/evidence/C101/intrinio/claims.reviewed.json \
    --provider intrinio --decision ACCEPT --reviewer-type primary_model \
    --reviewer-id iter2-acceptance-reviewer --reviewer-model qoder-cli-agent \
    --reviewer-role acceptance_reviewer
$PY scripts/quality/iter2_claims_merge.py
$PY scripts/quality/iter2_model_evidence.py \
    --inventory "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv" \
    --claims docs/evidence/C101/claims.merged.json \
    --output docs/evidence/C101/judge/claims.merged.report.json
$PY docs/evidence/C127/intrinio-forward-witness-20261017/probes/plane1_delta_row_model.py
$PY docs/evidence/C127/intrinio-forward-witness-20261017/probes/next_wave_producer_coverage_census.py
$PY docs/evidence/C125/sloos-witness-20261017/probes/next_wave_predicate.py
```

臂 D 里对 `row_model` 的打回只发生在内存（`ClassVar` 赋值后恢复），**不写产品文件**；
配方跑完 `git diff` 里不该出现 `iter2_financials.py` 的变化 —— 出现了就是探针自己写了盘。
本波之后 `.w22-scratch/` 已删除（一次性根，不是证据）。

## 7. lint 面与产品面静态检查

载体 `runs/lint_readings.txt`（append 本波第二枚探针后 sha16 `011b6561fa85338e`；
追加前为 `d76efaaba15a59a2`）：三枚文件各 `check_exit=0`/`format_exit=0`，
`py_compile_exit=0`，mypy 产品文件
`Success: no issues found in 1 source file`、`error_lines=0`。
ruff 0.15.20 与 judge 同解释器。

一处仪器口径记在这里免得重复踩：`awk 'length>100'` 把 CJK 清单路径那行报成 119 字节，
ruff 按码点读是 ≤100 —— 行长判据是 ruff，不是字节数。

## 8. 载体与摘要

| 载体 | sha16 |
| --- | --- |
| `probes/plane1_delta_row_model.py` | `3e59d9d95bfe36be` |
| `probes/next_wave_producer_coverage_census.py` | `a7acb73790759659` |
| `runs/plane1_arms.txt` | `3a155492a1951eee` |
| `runs/next_wave_predicate.txt` | `08028e82c7120017` |
| `runs/next_wave_producer_coverage_census.txt` | `1db45d3b86cca689` |
| `runs/lint_readings.txt`（追加后） | `011b6561fa85338e` |
| `scripts/quality/iter2_witnesses/_parts/intrinio/financials.py` | `50b40998089fd53a` |
| `opendata/data/providers/intrinio/models/iter2_financials.py` | `3c6630fb9ebdcb77` |

凭证面另立一文（`runs/secret_scan_payload.txt`）：两臂 + 一条能不为零的对照臂，
token 一律运行时生成、只种进载荷副本、看完删。
