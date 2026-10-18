# C130 —— 动作图追平到 281/69 一代之后 tmx 4 枚落地，并付三台落地面仪器的追平（迭代2 / 波 30）

日期：2026-10-18。波前 HEAD：`9c2c9a35`（波 29）。判定口径：仓库自带 judge
（`scripts/quality/iter2_model_evidence.py`）在 `docs/evidence/C101/claims.merged.json` 上的 350 恒等式读数，
不是任何手算子集。
本波面：**plane 1 281 → 285/350**，残留 69 → **65** = `INCOMPLETE 28` + `UNREVIEWED 37`；
逐状态差 `{INCOMPLETE: +20, OFFLINE_ACCEPTED: +4, UNREVIEWED: -24}`；
冻结信号 `identity_set_sha256[0:16] = 5168e1e723999312` 未动。

## 1. 本波改了什么（一张写集表）

| 文件 | 改动 | 为什么必须是它 |
| --- | --- | --- |
| [opendata/data/providers/tmx/models/fetchers.py](file:///Users/yunjinqi/Documents/new_projects/opendata/opendata/data/providers/tmx/models/fetchers.py) | +4 行 `row_model: ClassVar` 声明（[:293](file:///Users/yunjinqi/Documents/new_projects/opendata/opendata/data/providers/tmx/models/fetchers.py#L293)、[:347](file:///Users/yunjinqi/Documents/new_projects/opendata/opendata/data/providers/tmx/models/fetchers.py#L347)、[:397](file:///Users/yunjinqi/Documents/new_projects/opendata/opendata/data/providers/tmx/models/fetchers.py#L397)、[:554](file:///Users/yunjinqi/Documents/new_projects/opendata/opendata/data/providers/tmx/models/fetchers.py#L554)），614 行 / 24185 字节 / `50ca1c466a16fd6a` | runner 的 `direct`/`service`/`contract` 三相要求 `type(row) is fetcher_type.row_model`；这四枚 fetcher 早已 bound 且实发契约类，缺的只是声明 |
| [scripts/quality/iter2_witnesses/tmx.py](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_witnesses/tmx.py) | 本仓该 provider 的第一枚见证模块，323 行 / 15723 字节 / `5311773e92c4eace`；`CONFIG["models"]` 4 枚条目（`CompanyFilings`/`EquityInfo`/`EquityQuote`/`PriceTargetConsensus`），每枚带 9 个必填键与同一条 `_SEAM` | 见证面。tmx 自己持有传输（GraphQL POST），没有一条腿读 `http_transport` ⇒ 只能走 `client_seam`：`modules=["opendata.data.providers.tmx.models.client"]`、`attr="get_shared_http_client"`（[:74-75](file:///Users/yunjinqi/Documents/new_projects/opendata/scripts/quality/iter2_witnesses/tmx.py#L74)） |
| `docs/evidence/C101/tmx/{claims.json,claims.reviewed.json}` + `receipts/tmx/`（20 份）+ `fixtures/tmx/`（4 份）+ `captures/tmx/`（20 份）+ `primary-reviews/tmx__{EquityQuote,EquityInfo,CompanyFilings,PriceTargetConsensus}.review.json`（4 份） | 新落的分片证据 | 五相回执与真实 review，不是标签翻转 |
| [docs/evidence/C101/claims.merged.json](file:///Users/yunjinqi/Documents/new_projects/opendata/docs/evidence/C101/claims.merged.json)（7657865 字节 / `39090adeb9628bd7`）与 judge 两面 `claims.merged.report.{json,md}`（json 1087073 字节 / `275d7ff063bb878a`） | merge + judge 重写 | 落地面唯一权威来源 |
| 四台 C128 只读仪器：`seam_surface_match.py`、`ledger_axis_blast.py`、`witness_seam_kind_census.py`、`landed_face_rebase.py` | 落地面常量换代 + 散文重写，判定口径一个没动 | 见 §4 |
| [probes/retarget_batch_prediction.py](file:///Users/yunjinqi/Documents/new_projects/opendata/docs/evidence/C130/plane1-action-map-rebase-20261018/probes/retarget_batch_prediction.py)（744 行 / 32016 字节 / `22b94f36e029c37a`）、[probes/tmx_witness_prevalidation.py](file:///Users/yunjinqi/Documents/new_projects/opendata/docs/evidence/C130/plane1-action-map-rebase-20261018/probes/tmx_witness_prevalidation.py)（250 行 / 11870 字节 / `86fc79dc22fb4ac6`） | 前者 arm G 换到本波选出的探针行；后者是落地前的五相预验证 | 见 §4、§5 |
| [probes/payload_scan_falsification.py](file:///Users/yunjinqi/Documents/new_projects/opendata/docs/evidence/C130/plane1-action-map-rebase-20261018/probes/payload_scan_falsification.py)（208 行 / 8908 字节 / `1edac3d7bdbedfc6`） | 本波新增：给提交前的 staged 扫凭据配一条能咬的反例面 | 见 §5.9、§5.10、§7 |
| `runs/*.txt` 30 份载档 | 逐环 stdout + 各自退出读数 | 26 份 `.err`  sibling 按约定**不入库** |

## 2. 落地的四枚与桶语义：为什么 accepted 只走 4，而 UNREVIEWED 走 24

链条四环的 face 行逐字取自各自载档：

```
2a producer   claims emitted=120 (20 PASS, 0 FAIL, 100 BLOCKED) = 24 row(s) x 5 phases
              inventory total=350 (pinned identity set 5168e1e7…); catalog-resolvable bound
              identities this wave=318/350
2b recorder   recorder_exit=0 signed=4 refused=20 reviews_total=4
              output=docs/evidence/C101/tmx/claims.reviewed.json
2c merge      claims=1565 identities=313 reviews=285 accepted_identities=285
2d judge      350 identities, 285 offline accepted
```

`120 = 24 × 5` 是本波最容易被读错的一格：tmx 在残差里有 **24** 枚行，producer 对每一枚都记了五相 claim，
其中 20 枚的状态是 `BLOCKED`，载档里每一枚的 reason 逐字是 `BLOCKED reason=no model witness`
（见证模块 `CONFIG["models"]` 只有 4 枚条目，其余 20 枚没有条目可跑），只有 4 枚 `PASS`。
于是 judge 那边 `UNREVIEWED` 走 24 枚，`OFFLINE_ACCEPTED` 只走 4 枚，`INCOMPLETE` 走 20 枚 ——
**桶和移动 24 > accepted 移动 4**。`BLOCKED` 是一种状态，不是"缺一片 shard"，把它读成后者就会得出
"tmx 还缺 20 枚分片"的假结论。三台落地面仪器现在都把这格现算出来（`A_rebase_from_landed` 的
`rebase_delta={'INCOMPLETE': 20, 'OFFLINE_ACCEPTED': 4, 'UNREVIEWED': -24}`、
`delta_sums_to_zero=True`），读者不必记住这个机制。

`bound identities` 那一格本波没动：`318/350` 与波 27/28 落在 C128 载档里的五次读数逐字相同
（`grep -rho "catalog-resolvable bound identities this wave=[0-9]*/350"` 在 `docs/evidence/C128` +
`docs/evidence/C129` 上只出一个值）。这印证了本波的机制判断 —— 声明 `row_model` 不改变 catalog 绑定，
改变的是三相能不能通过。

`recorder refused=20` 那一侧不是失败：20 行 `REVIEW_REFUSED` 逐行打印了
`claim_status_blocked,command_exit_nonzero,contract_pins_missing,fixture_pins_missing,probe_pins_missing,
source_pins_missing`（`counterfact` 相还多一条 `counterfact_receipt_path_invalid`），
即"BLOCKED 的 claim 没有回执可签"。这与 §5 那条 producer 显示标签缺陷是两件事。

## 3. 落地之后，同一代的残差与谓词

```
residue_axis_census_face residue=65 witnessable_today=0 rows_without_module=37 cross_distinct=5
  actions={'add_code_carrying_guard': 13, 'implement_missing_model': 19, 'ledger_ruled_out': 11,
           'new_provider_package': 2, 'retarget_fetcher_output': 5, 'shard_entry_needs_harness': 15}
  identity_set=5168e1e723999312 arms=8 arms_passed=8 arms_failed=0
```

六桶之和 13+19+11+2+5+15 = 65 = 残差本身（臂 `D_bucket_reconciles` 判的就是这个等式，
`status_drift=[] bucket_drift=[]`）。载档 `wave30-step3a-*.txt` 与 `wave30-step5a-*_finalgen.txt`
**字节相同**（都是 80 行 / 20066 字节 / `9f303baa162a6651`）：落地后重取与仪器文本改完之后重取之间，
这台普查的一个计数都没动 —— 这一格是"编辑只碰散文没碰判据"的可复算证明，不是我的自称。

`retarget_fetcher_output` 5 枚的逐枚裁决（`wave30-step5b-*.txt`，16 行 / 6118 字节 / `ce46163ebd83c513`）：

```
retarget_batch_face candidates=5 declare_only=[]
  declare_plus_guard=['tmx::EquityHistorical','tmx::HistoricalDividends','tmx::IndexConstituents']
  declare_unreachable=[] needs_ruling=['federal_reserve::FomcDocuments']
  shape_unknown=['yfinance::KeyMetrics']
```

## 4. 复算仪器：三台落地面追平，逐台证明追平之后仍然能红

本波的追平面有**两台**仪器同时钉住落地面（这个结构自波 28 step 3a 起就在，不是本波新造的），
所以由一台驱动统一证明
（`landed_face_rebase.py` 285 行 / 13494 字节 / `594b39052edf1479`，2 个 subject × 6 臂 = 12 臂）。
驱动在同一进程里把每台 subject 的 `main()` 叫三遍：出厂常量 → 锻造回上一代 → 复原，
`setattr` 只落在已导入的模块对象上，subject 源码字节一个不写。

| subject | 常量换代 | 追平前（红） | 追平后（绿） |
| --- | --- | --- | --- |
| `ledger_axis_blast.py`（229 行 / 12076 字节 / `64baf65db0ca7103`） | `LANDED_STATUSES {8,281,61} → {28,285,37}`，旧值留在 `LANDED_STATUSES_PREVIOUS` | `arms=5 arms_passed=4 arms_failed=1`（`wave30-step6b-*.txt`） | `arms=5 arms_passed=5 arms_failed=0`（`wave30-step6f-*.txt`，19 行 / 2657 字节 / `5dbee6feff0a2224`） |
| `witness_seam_kind_census.py`（417 行 / 20093 字节 / `665b35b18a0afb00`） | `LANDED_ACCEPTED 281 → 285`，旧值留在 `LANDED_ACCEPTED_PREVIOUS` | 与上一行同一次运行：step 6a 那 4 枚红**恰好**是两个 subject 各自的 `A_unmodified_green=False` 与 `F_delta_matches_committed_face=False`（其余 4 臂两 subject 全 True） | `arms=7 arms_passed=7 arms_failed=0`（`wave30-step6f-*.txt`，40 行 / 5520 字节 / `001c3c101b6ab4e9`） |
| 驱动自身两面 | `EXPECTED_ACCEPTED=285`、`EXPECTED_DELTA={20,4,-24}`、`CARRIER` 改名到 wave-30 | `arms=12 arms_passed=8 arms_failed=4 PASS=False chain_exit=1`（`wave30-step6a-*.txt` stdout 与 `wave30-step6c-*.txt` 驱动写的那一代） | `arms=12 arms_passed=12 arms_failed=0 PASS=True chain_exit=0`（`wave30-step6e-*.txt` stdout + 驱动写成的 `C128/.../runs/wave30-step6e-landed-face-rebase.txt`，24 行 / 3760 字节 / `dd66625f257e9516`） |

驱动的六个臂都要求"锻造必须红"：`A_unmodified_green`、`B_forged_previous_red`、
`C_only_the_landed_arm_moves`（红的那一行必须恰好是 `A_landed_report_face`，两个 subject 都是）、
`D_measurement_unchanged_by_the_forge`（`residue=65/65`、`identity=5168e1e723999312/…` 逐字相同，
dict 型 subject 比的是 `statuses=` 两边同为 `{28,285,37}`，int 型比的是 `accepted=285/285`）、
`E_restored_green_and_aging_named`（`ledger_axis_blast aging_lines=0`、
`witness_seam_kind_census aging_lines=2 all_grant_lines=True` —— 后者第一遍把 fixture token 种进
`os.environ`，同一进程第三遍读 `granted=none`，所以这一臂判的是"差出来的每一行都是 grant 行"）、
`F_delta_matches_committed_face`（`rebase_delta` 与 `accepted_delta=4` 两形各判一次，且锻造那一遍
**报**的仍是 285：`summary_accepted=285` 与 `previous_accepted=281` 并排打印，
`identity_set_moved=False`）。

第四台 `seam_surface_match.py`（234 行 / 11085 字节 / `14cf90d13bf461b0`）追平的不是计数，而是**期望判定**：

```
wave30-step4a seam_surface_arms=3 flipped_by_widening=['bls','finra','tmx'] control=eia
  seam_surface_failed=0 identity_set=5168e1e723999312
wave30-step0a（红，留档）seam_surface_provider=tmx verbs=['request'] missing=[]
  verdict=WITNESSABLE expected=BLOCKED_ON_RUNNER_SURFACE judge_statuses={'UNREVIEWED': 24}
  seam_surface_arms=2 seam_surface_failed=1 step0a_exit probe=seam_surface_match rc=1
```

红色那一代证明旧的 `expected=BLOCKED_ON_RUNNER_SURFACE` 是**已落地面值烤进臂里的期望** —— 波 29 把 runner
的假客户端加宽到 `get`+`request` 之后，同一枚 provider 在同一台仪器上就换判定。三个判定如今都有载主：
`WITNESSABLE` 由活的 eia/tmx 载，`BLOCKED_ON_RUNNER_SURFACE` 只能在**加宽前**的表面上演示（锻造臂把
surface 换回 `frozenset({"get"})`，逐家比对 `live_v=WITNESSABLE` 与 `pre_v=BLOCKED_ON_RUNNER_SURFACE`），
`NO_SEAM_VERBS_MEASURED` 由活的 deribit 载（它自己持有传输，接缝动词集为空）。
派生而不是点名：被加宽翻过来的那一家家由 `_transport_verbs(p) - pre` 现算，
`flipped` 为空时打印 `flipped=NONE PASS=False` 走非零，对照格 `control` 同样派生并允许为空。
我最初在这里写过 `REQUEST_CARRYING = ("bls", "finra", "tmx")` —— 那正是本波在追平的那一类落地面常量，
已删除，形如 §5 第 2 条。

## 5. 缺陷与真实发现（都留字，不抹平）

1. **驱动写死了上一代载档路径，把已提交的证据重写了一次。** 追平前的那次红色运行（step 6a）落在
   `CARRIER = runs/wave29-step2d-landed-face-rebase.txt` —— 那是 C129 §9 逐字节引用过的档案，
   而驱动写盘方式是**整体重写**。发现后按顺序处理：新字节复制成
   `wave30-step6c-landed_face_rebase_written_by_driver.txt`（24 行 / 3771 字节 / `7af4b645dc24fadb`）保留红代，
   再对**那一个路径** `git checkout HEAD --` 复原（复原后 sha16 回到 `76c67441c9c8c41c`，与 C129 的引用一致），
   最后才把 `CARRIER` 改名到 `wave30-step6e-landed-face-rebase.txt`。
   这条与 C128 README 里"整体重写会吞掉追加的历史块"是同一家族的滞后：**换代时必须同时改载档常量**，
   否则下一次运行就会覆盖上一波已提交的档案。这一步没有任何被判定面被改写：`git status --porcelain`
   在本波写集之外只多出被复原的那一份，且复原后与 HEAD 逐字节相同。
2. **锻造臂里点名 provider = 又造一枚落地面常量**（见 §4 末），改成派生集合 + 空集合走失败路径。
3. **`arm G` 的期望必须换代，否则红的是仪器不是产品。** 时序是四代，红读数都留着：
   step 0b（6 臂 6/6，`candidates=9`）→ step 0c（加了 G/H 两臂，8/8，探针行派生死值
   `probe_row=tmx::CompanyFilings real_job=declare_only forged_missing_required=['filing_date']`）
   → **落地后 step 3b 红**：`probe_row=NONE real_job=None forged_without_contract_class=NO_PROBE_ROW
   PASS=False`、`arms=8 arms_passed=7 arms_failed=1`。红的原因是那四枚 `declare_only` 行刚变成 accepted、
   离开 `retarget_fetcher_output` 桶，而探针行选择器（"built 里有契约类且 payload 非空的第一枚"）
   在剩下的 5 枚里找不到载主 —— 这一臂不能反证就不能算复算仪器，所以 step 3c 把选择器改成从测量里
   取（docstring 里写明"探针行由测量选，不由本代标签点名"），选出的新载主是
   `tmx::HistoricalDividends`（`real_job=declare_plus_guard`），两个方向各反证一次：
   把契约类从 built 集合去掉 ⇒ `shape_unknown`；把必填字段 `amount` 从 payload 丢掉 ⇒
   `forged_missing_required=['amount']`。两条"不能被叫成声明就能落地"的对照仍在位：
   `fomc_refuses_declare_label=True`、`yfinance_refuses_declare_label=True`。
   **谓词被结果对上了一次**：step 0c 的 `declare_only=['tmx::CompanyFilings','tmx::EquityInfo',
   'tmx::EquityQuote','tmx::PriceTargetConsensus']` 逐字就是 step 1 落地的四枚，
   落地后 `candidates` 由 9 变 5、accepted 由 281 变 285，两个数都是本波自己量的。
   `step3c`/`step3d`/`step5b` 三份载档 sha16 相同（`ce46163ebd83c513`）：G 换代之后又做了纯 lint
   散文挪行，一个计数没动。
4. **`test_provider_catalog.py` 的两枚红不是本波的 mover**，归因用的是未改动的载档而非记忆：
   [wave27-step1-failed-ids.txt:28-29](file:///Users/yunjinqi/Documents/new_projects/opendata/docs/evidence/C128/plane1-residue-readiness-20261017/runs/wave27-step1-failed-ids.txt#L28)
   与
   [whole-suite-before-56-faces-derived.txt:56-57](file:///Users/yunjinqi/Documents/new_projects/opendata/docs/evidence/C103/batch5-scenario-census/runs/whole-suite-before-56-faces-derived.txt#L56)
   里同名的两枚早于本波就在红名单上（波 27 之前、整个套件那一代就在）。所以
   `wave30-step4c-*.txt` 的 `2 failed, 173 passed` 读作**存量红**，不记在本波账上，也没有为它放宽任何判定。
5. **producer 的显示标签缺陷仍在**（任务 #69）：那条
   `phases actually executed as subprocesses=120` 数的是 claim 条数，不是子进程数。本波没碰它。
6. 两代预验证载档（`wave30-step1-*.txt` 与 `wave30-step1b-*.txt`）`diff` 只差第一行的临时根目录名
   （`w30-tmx-preval-ohhuspq_` vs `…-cn300ff8`），其余 30 行逐字相同，两代同判
   `PREVALIDATION_DONE phase_failures_or_blocked=0`。这一句是按 diff 的读数写的，不是"重跑必然一致"的推断。
7. 我自己读错过一次仪器形状：`iter2_witnesses/tmx.py` 的 `CONFIG["models"]` 是 **dict 以模型名为键**，
   不是 list。按 list 索引取 `x['model']` 直接 `TypeError: string indices must be integers`。
   散文里那行"4 枚条目"读的是 `len(sorted(mods))` 与 `sorted(mods)` 的实际输出。
8. 两条 shell 侧仪表缺陷（都会造出假零，已按读数处理而不是当结论）：zsh 不展 `--include=*.py`
   （报 `no matches found`，不是命中 0）；`&&` 链在 `ruff check` 红处断掉，导致其后赋值与运行根本没跑。
9. **一台新仪器把两个对照臂读成了假绿**（提交前扫凭据那一步，见 §7）：
   `payload_scan_falsification.py` 首版把 `REPO_ROOT` 写成 `parents[4]`，指到 `docs/` 而不是仓库根，
   于是 `--config docs/.gitleaks.toml` 不存在、gitleaks 以 rc=1 退出且不写报告，而 `_scan` 把
   「没有报告」返回成 `[]` —— 臂 B/C 各自读一条 0 并自称 True，臂 A 同一条 0 才让它 False。
   一条坏前提同时制造两条假绿和一条偶然正确的红，这是本波最像"结论"的形状。修法是三处：`parents[5]`、
   无报告返回 `None` 并让该臂直接 False 且打印 `stderr` 尾部、`main` 里加 `config_exists`/`hosts`
   前提检查（任一不成立就 `premise_broken=1 PASS=False falsification_exit=1` 且**一臂都不跑**）。
   同一条根错还解释了那次 `hosts=0`：staged 路径是相对仓库根拼的，根错了整份清单就解析不到文件。
10. **第一枚反例根本不构成反例**：我种的凭据体是 40 个相同字符，gitleaks 报 0 条。原因不在规则的正则
    而在出厂的熵检查（那串字符熵 0.0000）。所以第二版把每个体的熵打印进载档
    （`ghp=4.7255 akia=4.0000 flat40=0.0000`），并且臂 A 的期望写成 `>=1` 而不是我一开始想写的 `==2`；
    实测只有 `github-pat` 一条规则咬住，`AKIA` 形状那一行没单独成条 —— 边界写在这里，而不是把屏幕说强。

## 6. pin 闸门：一次共享仪器编辑有没有把回执弄旧

```
wave30-step6d-receipt_pin_consistency.txt（5 行 / 538 字节 / `185c21b749552344`）
  pin_face slots=20411 stale=0 before_ne_after=0 distinct_pinned_paths=581
         arms=3 arms_passed=3 arms_failed=0 PASS=True
  B_forced: slots=20411 stale=1425 targeted_slots=1425
         substitute=scripts/quality/iter2_claims_probe_runner.py must_equal_targeted=True
  C_rearm: stale=0 before_ne_after=1 expected=stale=0/before_ne_after=1 PASS=True
```

`stale=0` 不是"没有可弄旧的东西"的空读数：总体是 20411 个 slot / 581 条被钉路径，
B 臂在内存里把 runner 换成未加宽的那一代伪造 ⇒ 恰好 1425 个 slot 变旧，
C 臂复原。零是在这条能红的对照旁边读出来的。

## 7. 静态面、提交前的索引扫凭据与本波付掉的两次格式化

四台 C128 仪器 + C130 三台探针：`ruff check` 与 `ruff format --check` 全 0（`landed_face_rebase.py`
在追平散文里两次踩线 100 列，都是把散文挪行，一个计数没动 —— 见 §3 里那两对相同 sha16），
三台 subject `py_compile` 0。`pytest -k tmx` 面 `51 passed, 26499 deselected in 2.59s`
（`wave30-step4b-*.txt`）。参数化套件见 §5.4。
**`make gate` 本波没跑**，所以这里不声称任何 gate 绿；17 成员链是另一件事（见 C128/C129 的 gate 条目）。

提交这一步自己也有闸：`make secret-check` 走的 `gitleaks detect --source .` 读**提交历史**，
看不见"已 stage、尚未提交"的那一刻，而那一刻正是凭据形状还便宜可修的时候。
`probes/staged_payload_scan.py`（C128 根，127 行 / 4890 字节 / `f5402c4e39098d93`，
与波 28 载档记录的三个数逐字相等 ⇒ 本波一个字节没改）对本波 staged 清单跑两次，读数逐字：

```
take 1  payload_scan_files=92 payload_scan_roots=92 payload_scan_unresolved=0 payload_scan_findings=0 payload_scan_rc=0  PASS
take 2  payload_scan_files=94 payload_scan_roots=94 payload_scan_unresolved=0 payload_scan_findings=0 payload_scan_rc=0  PASS
diff    1,2c1,2 —— 动的只有两格分母（92 → 94），三条 0 与第二行除 N 之外逐字相同，stdout 两份都是 147 字节
```

`findings=0` 只有在这个屏幕能咬的时候才是读数，所以配了反例面
（`probes/payload_scan_falsification.py`，载档 `wave30-step8b-*.txt`）：把 staged 清单前 3 枚的字节抄进
mktemp 树并种一枚 LCG 生成的假凭据，三臂 `arms=3 arms_passed=3 arms_failed=0 PASS=True`，
臂 A `findings=1 rules=['github-pat'] gitleaks_rc=1`（rc=1 在这里的出厂语义就是"检出"，不是崩溃），
臂 B `findings=0`，臂 C `findings=0`（拼接是 `.gitleaks.toml:128-129` 记着的边界）。
这一屏的强度是**一枚规则咬住**这一对形状，不是两枚：`AKIA` 那一行没单独成条，见 §5.10。
两枚新文件都进了 take 2 之后的清单；覆盖最终字节的那一次（take 3）住在提交说明里，
载档 `wave30-step8a-*.txt` 不写它自己的读数（写进来就又是下一格 lag）。

## 8. 下一波谓词、射程与失效条件

谓词（本代读数，`wave30-step5a`）：残差 65 里 **`witnessable_today=0`** —— 按当前 harness 表面，
没有一枚行可以只靠写分片就翻成 accepted。所以"下一波还能落地吗"这个问题换轴才有答案：

- `shard_entry_needs_harness` **15**：要先动 harness（接缝/runner）才谈得上落地，属于共享仪器编辑，
  代价按波 29 那一轮实测（25 份重生产 + 26 份重签）。
- `implement_missing_model` **19**：产品侧缺模型/缺包，属于实现活，不是证据活。
- `add_code_carrying_guard` **13** 与 `ledger_ruled_out` **11**、`new_provider_package` **2**：
  前两桶已在波 27/28 分别定过价（台账那一轴真拒绝只有 11 枚，`ledger_axis_blast.C_conflation_identity`
  现算 `overstated_by=53`），第三桶要新建 provider 包。
- `retarget_fetcher_output` **5**：其中 3 枚 tmx（`EquityHistorical`/`HistoricalDividends`/`IndexConstituents`）
  是 `declare_plus_guard` —— 声明之外还要一条真的守卫；
  `federal_reserve::FomcDocuments` 是 `needs_ruling`（任务 #68：7 枚 macro 字段对 5 枚契约字段的裁决），
  `yfinance::KeyMetrics` 是 `shape_unknown`（任务 #33）。
  **这两枚在没有产品裁决之前不可转换**，我不会为了让数字变绿而替它发明裁决；
  臂 `H_declare_calibration` 也专门拒绝把 `shape_unknown` 当默认判定
  （`shape_unknown_not_defaulted_in=['yfinance::KeyMetrics']`）。

失效条件：只要 `OFFLINE_ACCEPTED ≠ 285` 或 `identity_set[0:16] ≠ 5168e1e723999312`，
本文所有计数（含 §2 的 delta、§3 的六桶、§4 的三张红/绿表）全部作废，须先重取
`wave30-step6b/6e` 两代再谈新数；这正是被追平的三台仪器自己的判定条件，不是附加的口头约定。
失败读数形态：若下一波只落声明不加守卫，预期是 tmx 那三枚仍留 `declare_plus_guard`
而 plane 1 不动（`verdict=ROW_CLASS_NOT_CONTRACT` 在 `sim_after_retarget=NO_CODE_SITE` 那一支
根本不会翻，见 `wave30-step5b` 第 3、5 行）。

复算配方（argv 逐字，`py313` 是唯一判定解释器）：

```
R=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
D=docs/evidence/C128/plane1-residue-readiness-20261017/probes
K=docs/evidence/C130/plane1-action-map-rebase-20261018

PYTHONPATH=. $R $D/landed_face_rebase.py          # 12 臂，直接跑，不要过管道
PYTHONPATH=. $R $D/ledger_axis_blast.py           # 5 臂
PYTHONPATH=. $R $D/witness_seam_kind_census.py    # 7 臂
PYTHONPATH=. $R $D/seam_surface_match.py          # 3 臂 + 派生 flipped/control
PYTHONPATH=. $R $K/probes/retarget_batch_prediction.py   # 8 臂，无 argv（main() 不收参数）
PYTHONPATH=. $R $K/probes/tmx_witness_prevalidation.py   # 五相预验证，只在临时根里物化 fixture
PYTHONPATH=. $R $K/probes/payload_scan_falsification.py  # 3 臂；只在 index 非空且有 .gitleaks.toml 时绿
```

## 9. 载体与摘要

| 载档（`runs/` 下，全部逐字 stdout） | 行 / 字节 | sha16 |
| --- | --- | --- |
| `wave30-step0a-readiness_census.txt` | 9 / 2030 | `510b999be9e61454` |
| `wave30-step0a-residue_axis_census.txt` | 85 / 21241 | `b66cbaf39f44b38a` |
| `wave30-step0a-seam_reachability_census.txt` | 38 / 7196 | `551217b7c799c9bd` |
| `wave30-step0a-seam_surface_match.txt`（**红**，旧期望） | 4 / 478 | `4fa7f30f2cc7910d` |
| `wave30-step0b-retarget_batch_prediction.txt`（6 臂代，`candidates=9`） | 18 / 6022 | `8e649effa91c39c4` |
| `wave30-step0c-retarget_batch_prediction.txt`（8 臂 8/8，§5.3 那条 `declare_only` 谓词出在这份） | 20 / 9409 | `9f09da28dfd64906` |
| `wave30-step1-tmx_witness_prevalidation.txt` | 31 / 2653 | `786ffe07f7cfd335` |
| `wave30-step1b-tmx_witness_prevalidation.txt` | 31 / 2653 | `77681b575f81b849` |
| `wave30-step2a-producer.txt` | 123 / 13819 | `259b48e79ea43acf` |
| `wave30-step2b-recorder.txt` | 25 / 17849 | `63e3b544690db7f3` |
| `wave30-step2c-merge.txt` | 30 / 1628 | `ab53c2bf66741b9f` |
| `wave30-step2d-judge.txt` | 1 / 148 | `f5649adb5755af98` |
| `wave30-step3a-residue_axis_census.txt` | 80 / 20066 | `9f303baa162a6651` |
| `wave30-step3b-retarget_batch_prediction_post_land.txt`（**红** 7/8，`probe_row=NONE`） | 16 / 6048 | `32422bb03f4e1c97` |
| `wave30-step3c-retarget_batch_prediction_rebasedG.txt`（8/8；与下两份、与 `step5b` **字节相同**） | 16 / 6118 | `ce46163ebd83c513` |
| `wave30-step3d-retarget_batch_prediction_post_format.txt` | 16 / 6118 | `ce46163ebd83c513` |
| `wave30-step4a-seam_surface_match_rebased.txt` | 8 / 1265 | `3aa099280c139bb5` |
| `wave30-step4b-pytest_tmx.txt` | 2 / 117 | `1e5ba1acf28e4e38` |
| `wave30-step4c-pytest_parametrized_suites.txt` | 151 / 8374 | `79abe210f0fe0aad` |
| `wave30-step5a-residue_axis_census_finalgen.txt` | 80 / 20066 | `9f303baa162a6651` |
| `wave30-step5b-retarget_batch_prediction_finalgen.txt` | 16 / 6118 | `ce46163ebd83c513` |
| `wave30-step6a-landed_face_rebase_prefix.txt`（**红** 8/12） | 13 / 1856 | `80afb5022227c55a` |
| `wave30-step6b-ledger_axis_blast_measured_face.txt`（**红** 4/5） | 19 / 2657 | `ffaee640b14343de` |
| `wave30-step6c-landed_face_rebase_written_by_driver.txt`（红代驱动写的 24 行） | 24 / 3771 | `7af4b645dc24fadb` |
| `wave30-step6d-receipt_pin_consistency.txt` | 5 / 538 | `185c21b749552344` |
| `wave30-step6e-landed_face_rebase_rebased.txt`（**绿** 12/12） | 13 / 1852 | `47dfea1a4228f256` |
| `wave30-step6f-ledger_axis_blast-finalgen.txt`（绿 5/5） | 19 / 2657 | `5dbee6feff0a2224` |
| `wave30-step6f-witness_seam_kind_census-finalgen.txt`（绿 7/7） | 40 / 5520 | `001c3c101b6ab4e9` |
| `wave30-step8b-payload-scan-falsification.txt`（绿 3/3；**先于 8a 跑**，8a 的反例面引用它） | 10 / 1135 | `1a2c189714b22714` |
| `wave30-step8a-precommit-staged-scan.txt`（take 1=92 / take 2=94，两条 0 与两格分母的 diff 逐字在内；take 3 按 lag 约定住提交说明） | 101 / 8798 | `1829aae3bbd309bc` |

驱动自己写的载档在 C128 根：`docs/evidence/C128/plane1-residue-readiness-20261017/runs/
wave30-step6e-landed-face-rebase.txt`（24 行 / 3760 字节 / `dd66625f257e9516`）。
同目录上一代 `wave29-step2d-landed-face-rebase.txt` 被本波误写过一次并已按 §5.1 复原，
复原后 `76c67441c9c8c41c` 与 C129 §9 的引用逐字相同。
26 份 `.err` sibling 与 `runs/.step4f.rcs` 按用户裁定不进本波提交。
