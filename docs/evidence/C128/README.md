# C128 — 波 23 的前置测量：plane 1 剩下的 90 行，生产运行到底到得了哪几枚

日期：2026-10-17　分支：`dev`　基线提交：`f4967950`（波 22）
解释器：`/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python` CPython 3.13（门禁解释器）

结论先行：**残留 90 行不是"一类工作"，是两类**，而且划分依据不是"有没有人在这家跑过仪器"，是
"跑一遍仪器过不过"。两台探针把这条判据落成五列可读的面：

- `readiness_census.py` 五列 `bound / declared / frozen_module / is_domain_contract / witness_entry`，
  桶序 NEED_BINDING → NEED_DECLARE → DECLARE_NOT_FROZEN → LOOK_ALIKE → NEED_SHARD_ENTRY → PRODUCIBLE_NOW。
  波前读数 `residue=90 bound=58 buckets={'NEED_BINDING':32,'NEED_DECLARE':58}`，四列声明位实测全 0，
  即**没有一枚残留行声明了契约类，也没有一枚有见证条目**；三臂 `readiness_arms=3 passed=3 failed=0`，
  `identity_set=5168e1e723999312 expected_match=True`。臂 B 的"移得动"是对无扰动分桶**实测**的：
  `forced_off_need_declare=58`、`forced_on_need_declare=0`。
- `declare_candidates.py` 只给 NEED_DECLARE 那 58 枚定价：
  `counts={'contract_in_module':56,'emits':35,'witness_entry':0}`，government_us 全 5 枚判
  `DECLARE_AND_WITNESS`。它是**排序**仪器，不判定任何行 —— 判定权在 producer 运行。

本波据此吃掉 government_us 2 枚（`TreasuryAuctions`、`WeatherBulletin`：各补一行 `row_model` 声明 +
首份该 provider 见证模块的两条 `client_seam` 反例），五相 10 枚 claim 一次全 PASS，
plane 1 `260 → 262/350`，残留 `90 → 88`（`UNREVIEWED 83 → 81`、`INCOMPLETE` 仍 7），`identity_set` 未动。
波后同一台 census 读 `residue=88 bound=56 buckets={'NEED_BINDING':32,'NEED_DECLARE':56}`、
`readiness_failed=0`。叙述与归因（含 §84.10 那条 federal_reserve 谓词如何被本波仪器推翻、
以及定价仪器自己的三处假面）在 `开发验收记录.md` §85。

**波 24 复用同一个证据根**（仪器是同一批，只多了一台 seam 表面探针），所以本目录没有 `C129/`：
`docs/evidence/C129/` 不存在，wave-24 的证据在 `plane1-residue-readiness-20261017/runs/wave24-eia.txt`。
那两枚是 eia 的 `PetroleumStatusReport` 与 `ShortTermEnergyOutlook`（1 行 `row_model` 声明 × 2 + 该家首份
168 行见证模块），五相 10 枚 claim 一次全 PASS，plane 1 `262 → 264/350`、残留 `88 → 86`、
`NEED_DECLARE 56 → 54`。§85.8 谓词里 tmx 那一枚被 `probes/seam_surface_match.py` 在**跑之前**判为
`BLOCKED_ON_RUNNER_SURFACE`（tmx 调 `.request()`，共享 runner 的 fake 只实现 `get`），谓词据此收缩成 +2；
叙述在 §86。

**波 25 复用同一个证据根**（`docs/evidence/C129/` 依旧不存在）：吃掉 government_us 的
`CommodityPsdData` 与 `CommodityPsdReport`（各 1 行 `row_model` 声明 + 该家既有见证模块里两条
`client_seam` 反例），五相 20 枚 claim 一次全 PASS，plane 1 `264 → 266/350`、残留 `86 → 84`、
`NEED_DECLARE 54 → 52`、`NEED_BINDING` 仍 32、`UNREVIEWED 79 → 77`、`INCOMPLETE` 仍 7、`identity_set` 未动。
本波的产出重点是**它把第三批候选（`WeatherBulletinDownload`）判成不可见证，而判据不是上一波那台 seam
探针**：那一枚 `seam_reachable=True`（`verbs=['get']`，表面完全可达），但 `models/weather_bulletin_download.py`
里 9 处 `raise` 全是取数前的 URL/形状守卫，篡改一份"响应文档"撞不到任何码 —— 于是 §86.8 那条
"3 枚 / 267"的预测被实测推翻，改成 2 枚 / 266 并就地更正（错的数字留在记录里）。为了让下一波不必再靠
手 grep 撞同一个坑，本波新增两台正交轴的仪器（可达性 vs 可见证性），见下。叙述在 §87。

## 载体

- `probes/readiness_census.py`、`probes/declare_candidates.py`：两台仪器，各自带正控制与可动性闸。
- `probes/payload_secret_scan.py`：入库前的凭证扫描双臂仪器（见下）。
- `probes/seam_surface_match.py`：AST 读每家 provider 在 `get_shared_http_client()` 上调的动词，与
  `iter2_claims_probe_runner._FakeSharedClient` 的公开 callable 取交集，判 `WITNESSABLE` /
  `BLOCKED_ON_RUNNER_SURFACE`；两臂各被要求能与对方不同（eia `verbs=['get']` + 2 枚 accepted，
  tmx `verbs=['request']` + 24 枚 UNREVIEWED）。
- `probes/staged_payload_scan.py`：`--paths` 指定的工作树字节拷进临时树后用 shipped `.gitleaks.toml`
  扫（`detect --no-git`），补上 `payload_secret_scan.py` 不收 `.json` 的那半边。`payload_scan_unresolved`
  这一列是本轮加的：原先"路径解析不到"会被静默跳过、集合缩小而照打 `findings=0`，现在逐条打印路径并
  把 `unresolved>0` 读成 FAIL（双臂复测面见 §86.5 第 4 条）。
- `probes/seam_reachability_census.py`：把 `seam_surface_match.py` 的表面判据从"点查三家"扩成**全总体**
  普查（`seam_census_units=35`：32 家 + `_engine` + 两家无包），逐单位打印
  `bucket / mechanism / models_verbs / package_verbs / witness_module / client_seam / rows / accepted /
  residual`，用来把"哪条腿能把行跑起来"和"这行值多少钱"分成两条轴分别测。它自己不带臂，
  可反证性由 `seam_surface_match.py` 的五臂面（`seam_surface_arms=5 seam_surface_failed=0`）与
  两块对照（把 yfinance 武装成 `WITNESSABLE` 必须红、把 government_us 武装成
  `NO_SEAM_VERBS_MEASURED` 必须红）提供。
- `probes/post_response_guard_census.py`：波 25 的第二轴仪器，也是本目录里唯一**因为推翻了自己上一条
  谓词才存在**的探针。它把"文档篡改到底撞不撞得到拒绝码"从手 grep 变成驱动：按所在方法给带码 `raise`
  分桶（`transform_data` 由构造保证可达、`extract_data` 有歧义所以两列并列打印绝不相加、`other` 到不了），
  再解析已接受的 266 行自带的 99 枚拒绝码（工厂码、`ClassVar` 默认码、引擎
  `f"{spec.error_prefix}_SHAPE_INVALID"` 组合码都要能解析，否则 `absent` 会把"解析器不知道"读成"码不存在"）。
  三臂含栽码反证：`C_planted_negative` 要求一个全树不存在的码读成 `absent`，否则 A 臂的 `absent=0`
  是不可反证的零。
- `probes/receipt_pin_consistency.py`：入库前的**过期 pin 闸门**（波 25 第二版，181 行，
  `sha16=954fa541f9d511e4`）。它把"某波改了共享文件之后，早先产出的 receipt 还在钉旧字节"这件事从
  gate member 2 手里抢到波内：逐 slot 用工作树字节重算 `after_sha256`。三臂 —— A 主张 `stale=0`；
  B 把一枚被多处钉住的路径的期望换成另一枚真实文件的 digest，要求 `stale == targeted_slots`
  （本波 `20/20`、全树 `1330/1330`）；C 只在内存里造一枚 slot（路径真在盘上、`after_sha256` 用真
  digest、`before_sha256` 填 64 个 0），要求 `stale=0 before_ne_after=1`，好让 A 臂的
  `before_ne_after=0` 不是断线。`contract_version` 是字符串不是字节 pin，探针打印这一点而不是静默跳过。
- `runs/readiness_census.txt`：波前面（`census_exit=0`、`readiness_arms=3 readiness_passed=3`、stderr 0 字节）。
- `runs/declare_candidates.txt`：58 枚定价面（64 行，末三行依次是 `declare_arms=2 declare_passed=2
  declare_failed=0`、`declare_exit=0`、`--- stderr bytes=0 (0 means no traceback) ---`）。**列序**是
  `row <identity> by_name= output_model= flags= contract_module= fetcher= canonical= contract=`：
  原来的 `row <identity>: fetcher=<类名>` 形状会让 gitleaks `generic-api-key` 把公开类名当凭证
  （命中 `KeyExecutives` 那一行），而放宽 `.gitleaks.toml` 会撞上
  `tests/test_acceptance_item_probe.py:707-731` 冻结的 allowlist 常量 —— 判定与豁免都不动，只改本轮
  仪器自己的列序，8 个字段一个不少。
- `runs/readiness_census_after_batch.txt`：本波入库面（`census_after_exit=0`、stderr 0 字节）。
- `runs/payload_secret_scan.txt`：列序改动没把扫描器改哑的证据面 —— 臂 A `arm_a_findings=0`，臂 B 往
  **同一份改过列序的**载体里插一枚运行时生成的 Slack 形状 token，`arm_b_findings=1`
  （`payload_secret_face PASS=True`、`scan_exit=0`、stderr 0 字节；假 token 只在临时目录，不入库）。
- `runs/wave24-eia.txt`（65 行）：波 24 全链载体 —— producer `claims_emitted=10` 与两行 `phases=5 pass=5
  fail=0 blocked=0`、10 条 `receipt`（每相首行 stdout + 字节数 + sha16）、两条 `COUNTERFACT_JSON`
  （expected==observed，各带未扰动正控制行数）、`reviews_signed=2`、merge（`claims=1355 identities=271
  reviews=264` + `merge_idempotent=True` + 前后同 sha `7eeb5739ec725f69`）、judge（`350 identities, 264
  offline accepted`、`invalid_claims=0`）、census 重取（`residue=86 bound=54`）与 seam 探针面。
  每个 `*_exit` 都是**无管道**取的；末尾三行指向 `post_batch_payload_scan.txt`。
- `runs/wave25-step0-reachability.txt`：波 25 的 step 0（只读），六个 block 各带自己的
  `blockN_exit`（全部无管道取）—— ① `seam_surface_arms=5 failed=0`；②/③ 两块对照各读
  `seam_surface_failed=2` / `=1` 且 `block2_exit=1` / `block3_exit=1`（这两条**期望为红**，因为它们是
  "臂必须能不同"的对照：把 yfinance 武装成 `WITNESSABLE`、把 government_us 武装成
  `NO_SEAM_VERBS_MEASURED`）；④ 全总体可达性普查 `seam_census_units=35` 与
  `seam_census_residual_by_bucket` 86 枚的机制划分；⑤ 波前定价面 `column_census: residue=86
  bound=54 buckets={'NEED_BINDING': 32, 'NEED_DECLARE': 54}`、`readiness_arms=3 passed=3`；
  ⑥ 波前定价 `needs_declare=54 counts={'contract_in_module': 52, 'emits': 31, 'witness_entry': 0}`。
- `runs/wave25-government_us.txt`（330 行）：波 25 全链载体 —— 谓词命中七项、五相 20 枚 claim 的
  `seam_requests=5` 推导（`max(_DEFAULT_SENDS=3, answerable)`，`iter2_claims_probe_runner.py:85-107`）、
  两条 `COUNTERFACT_JSON`（`USDA_BAD_COMMODITY_ROW` / `USDA_REPORT_NOT_PDF`，expected==observed）、
  `recorder_exit=0 signed=4 refused=0`、merge（`claims=1365 identities=273 reviews=266
  accepted_identities=266`）、judge（`350 identities, 266 offline accepted`、`invalid_claims=[]`）、
  波后 census `residue=84 bound=52 buckets={'NEED_BINDING': 32, 'NEED_DECLARE': 52}`，以及第三代的
  24 个 pin 槽"三代之辨"。§86.8 那条 3 枚 / 267 的预测在本载体里被实测推翻并就地更正。
- `runs/post_response_guard_census.txt`（204 行）：第二轴仪器的入库面。头部记
  `GUARD_LANDED_exit=0`、`stderr_bytes=0`、`probe_sha16=b6d18160fd2c8985`、
  `output_sha16=e8db1735d7603cfe`，并给了**内容锚定**的复算配方（`provider_screen:` 到 `guard_face`
  那 18 行 stdout 用 awk 切片重算，行号漂移不会让配方变哑）。中间是逐家 `guard_provider=…` 面与三臂，
  末尾 `--- pre-fix readings ---` 块逐字留了解析器四代 + 分块一代的原始 stdout（各带 `/tmp` 路径、
  mtime、字节数、shasum16），因为那些临时文件不可久存。
- `runs/declare_candidates.post-batch.txt`（61 行、54 条 `row `）：**波 24 那一代**的定价面，与波前面
  `runs/declare_candidates.txt`（58 枚）不同代，别把两者混比；波 25 之后总体已是 52 枚，当代读数在
  `runs/wave25-step0-reachability.txt` block ⑥（波前 54）与 `runs/wave25-government_us.txt` 的波后
  census（84 / NEED_DECLARE 52）。臂 A 的已入库正控制读
  `intrinio::ForwardSalesEstimates` 四列全 True。
- `runs/post_batch_payload_scan.txt`：波 24 入库前 payload 扫描面，`--paths` 清单逐行记在里面。
- `runs/post_batch_payload_scan.wave25.txt`（135 行）：波 25 入库前 payload 扫描面，23 根逐行列出并写明
  **故意不收**的根（`docs/evidence/C100/**` —— 其 `.md` 被 C113 的 `!*.md` 反选重新纳入版本控制视野、
  由 gate member 5 普查而不入库，`C108/`、`quality-source-repairs/`、`uv.lock`、根目录三份 `LICENSE*.md`
  与 `DISCLAIMER.md`）。取 1 `64 files / 23 roots / unresolved=0 / findings=0`，取 2 与取 3
  在含载档自己的字节上读到同一不变式 `65 / 24 / 0 / 0`；两臂控制 `payload_files=17`（当时 `probes/` 7 个
  `.py` + `runs/` 10 个 `.txt`）`arm_a_findings=0 arm_b_findings=1 payload_secret_face PASS=True`。
  这台只收那两个根，所以总体是**代际敏感**的：pin 闸门落地后重跑读 `payload_files=20`（8 + 12），
  这份 README 与 precommit 载档定稿后再读是 `21`（8 + 13），其余四行逐字不变。
  取 4 跑过但故意不写进来（载档不能扫自己最后一行）。
- `runs/precommit-staged-scan.wave25.txt`：**提交那一刻**的入库面，人群换成索引本身 ——
  `git -c core.quotepath=off diff --cached --name-only -z | xargs -0 … staged_payload_scan.py --paths`
  读 `55 files / 55 roots / unresolved=0 / findings=0 rc=0`，前置控制是 `git diff --name-only` 为空
  （index == 工作树，于是"扫工作树"就是"扫要提交的字节"）。同一代里两臂扫描 `payload_files=20`、
  pin 闸门两个 scope 的 stdout 与入库载档 `cmp` 逐字节相同、judge 重跑到 `/tmp` 的报告与已 staged 那份
  同 sha（`73f4ed196e73aef5`、`350 identities, 266 offline accepted`）、ruff 两关 exit 0。
  它还留了这台仪器自己的**第一次红**：`STAGED_exit=1`、stdout 0 字节、stderr 138 字节
  （`xargs` 只会把文件表追加在命令尾部，`--paths` 必须写进命令前缀）—— 只看退出码不区分"用法错"与
  "扫过且干净"，而 `unresolved` 那一列就是为了让这类空跑现形。
- `runs/wave25-receipt-pins.txt`（106 行）：过期 pin 闸门的入库面，两个 scope 各五行走字面量
  （本波 `slots=228 stale=0 before_ne_after=0 distinct_pinned_paths=17 arms=3 arms_passed=3`；
  全树 `slots=19382 stale=0 … distinct_pinned_paths=536`）。它同时留了两样东西：一个**实测结论**
  （19382 枚 slot 全部 `before_sha256 == after_sha256` ⇒ pin 只有"指向"语义、没有"差分"语义，可反证的
  内容只有 `after_sha256 == 工作树字节`）和一份**代际史**（第一版两臂探针 `0674a7928a87b9c4` 的面原样保留，
  连同那条把一次性 inline 验证器的结论写成仪器说明书的 limit —— slot 数、by_kind、`stale=0` 与两个
  `targeted_slots` 等式在两代之间逐字未变）。

复算配方（仓库根）：

```sh
PYTHONPATH=. /Users/yunjinqi/opt/anaconda3/envs/py313/bin/python \
    docs/evidence/C128/plane1-residue-readiness-20261017/probes/readiness_census.py
```

`runs/declare_candidates.txt` 那份是**波前 58 枚**的面，在当前工作树上重跑只会读到 54 枚（波 23 吃掉 2、
波 24 再吃 2），所以重跑对上的是 `runs/declare_candidates.post-batch.txt` 而不是那份；要复现波前面得在
波 22 状态跑：

```sh
git worktree add --detach /tmp/c128_base f4967950
# 把 probes/declare_candidates.py 放到同相对路径，然后
cd /tmp/c128_base && PYTHONPATH=. <门禁解释器> docs/evidence/C128/plane1-residue-readiness-20261017/probes/declare_candidates.py
```

`payload_secret_scan.py` 无参数，直接 `python docs/evidence/C128/.../probes/payload_secret_scan.py`
（需要 `gitleaks` 在 PATH，否则打印 `SKIPPED=1`）。`seam_surface_match.py` 与
`staged_payload_scan.py` 的 argv 原样抄在 `runs/wave24-eia.txt` 与 `runs/post_batch_payload_scan.txt` 里，
照抄即可复算。波 25 两台的 argv 同样原样在各自载体里：`seam_reachability_census.py` 见
`runs/wave25-step0-reachability.txt` block ④，`post_response_guard_census.py` 见
`runs/post_response_guard_census.txt` 头部（`PYTHONPATH=. <门禁解释器> docs/evidence/C128/plane1-residue-readiness-20261017/probes/post_response_guard_census.py`，
在仓库根执行）。这两台都是**当代**仪器：在当前工作树重跑应当复现入库面；一旦下一波改了模型或码站点，
A 臂的 `td/ed/other/engine` 计数与 `absent` 都可能移动，届时要按"重基线而非放宽"处理 ——
`absent` 必须仍为 0 且 `C_planted_negative` 必须仍读成 `absent`，否则这台仪器自己就哑了。
第三台当代仪器是 `receipt_pin_consistency.py`，argv 抄在 `runs/wave25-receipt-pins.txt` 头部两条
（`--providers <本波分片>` 与全树各一次，都是仓库根执行）。它是**每一波入库前该跑的那一台**：
`arms_failed>0` 说明本波把某个共享文件的 pin 弄旧了，补产补签再提交；`C_rearm PASS=False` 是仪器坏了；
`before_ne_after>0` 则推翻上面那条"pin 无差分语义"的实测结论，那时要重基线而不是沿用本波的说法。

本目录八台仪器都只做普查：除自己的臂失败外退出 0，绝不改动任何被判定的面。波 23 五台
（readiness、declare、payload 两臂扫描、seam 表面），波 24 收编 staged 扫描那一列，波 25 加两台正交轴
（可达性普查、后响应守卫），入库前再加一台 pin 闸门。新仪器同样只打印屏幕与臂，不判定任何行；
唯一例外是 pin 闸门的 C 臂在**内存里**造一枚假 slot —— 不落盘、不写任何证据文件。
