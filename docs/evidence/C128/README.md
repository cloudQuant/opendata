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

**波 26 一枚也没落地：它在 step 0 就被自己的仪器解散了**（`docs/evidence/C129/` 依旧不存在）。§87.8 的
谓词是按"7 枚 `INCOMPLETE`"这个**判定状态类**武装下一波的，并预测 plane 1 `266 → 273`。一个状态类的枚数
不是工作清单。`probes/incomplete_row_guard_check.py` 把转正条件拆成**四条互相独立的轴**逐枚测
（`bound` / `ruled_out` / `guard` / 本轮新写的 `row_class`），实测 **0 / 7 枚**可由 witness 波转正：
5 枚 `UNBOUND` 各自被本家 `NOT_DECL*` 台账带着（缺的是上游协议证据 = 实现工作，不是 fixture），
2 枚 `ROW_CLASS_NOT_CONTRACT` —— `federal_reserve::FomcDocuments` 与 `::YieldCurve` 前三轴全过
（5、9 条本类 `transform_data` 带码 `raise`），但它 bound 的 fetcher 构造的是
`models/federal_reserve_macro.py` 里的替身类，而 `contract_model(domain)` 解析到的是
`models/federal_reserve_clean_documents.py` 那两个同名/异名类；服务腿
`opendata/services/provider_model_query.py:327` 先比**类身份**再看字段，直取腿过、服务腿拒，而服务腿是
判分五相之一。plane 1 停在 **266/350**，`identity_set` 未动；那两枚移交 task #9/#10 的"冻结判定 vs 新架构"
裁决，三面冲突（`domains.yaml` + `registry_batch5_snapshot` 的 `contract` 字段 /
`tests/test_federal_reserve_iter2_models.py:322-346,382-402` / `b15717b6` 引入的 binding）逐字钉在载体里。
同一份载体还实测出下一波的候选总体：`needs_declare=52` 里只有 28 枚读 `DECLARE_AND_WITNESS`，其余 24 枚
是 retarget/import 工作，而 28 枚里 government_us 那一枚已被波 25 判 `NO_CODE_SITE`、tmx 那一枚堵在
`BLOCKED_ON_RUNNER_SURFACE` —— 剩下的 yfinance 那一块是本波**没有**测第三、四轴的，载体里没有为它主张
任何可转正性。谓词由此从"一个状态类"换成"这块总体"。叙述在 §88。

**波 27 三步都不动 plane 1，但它第一次把"剩下的 84 枚到底是什么活"读成一面**（`docs/evidence/C129/` 依旧
不存在）。step 0 把波 26 那条两成因布尔指到**整个残差**：84 枚里台账真拒绝只有 11 枚、台账沉默 2 枚、
"本家根本没有 `specs.py` 可拒"71 枚（`runs/wave27-step0-ledger-blast.txt`），射程 71 枚而波 26 的裁决
一枚也没被移动。step 1 落 yfinance 15 行 `row_model` 声明并逐枚做身份判定，plane 1 **未动**（`266/350`、
identity set 未变）—— 那 15 枚只是从 `NEED_DECLARE` 移到 `NEED_SHARD_ENTRY`，缺的是见证分片与 harness 表面，
不是声明；同一轮新仪器 `witness_seam_kind_census.py` 否证了 §90.5 谓词后半"见证腿取引擎 `transport`"
（`subject_transport_calls=0`）。step 1b 处理这 15 行撞上的**第二道 tracked 围栏**（batch5
`source_hashes_sha256`，回执 pin 闸门看不见它）。step 2 先给两条分支定价再决定：普查叫 `SEAM_REACHABLE` 的
7 枚与"把共享假客户端加宽到 `request`"能翻开的 29 枚，两支都读 `convertible=0` —— 加宽只翻 deribit 与 tmx
两家、`post_required=False`，即整棵树重签买来 **0 枚**（§90.5 那条"`request` / `post`"规格按此更正）。
于是 step 2b 把 `350 - 266 = 84` 一次跨三台已落地仪器成像：`witnessable_today=0`、`rows_without_module=75`、
`registry_publish_model=0` 而 `implement_missing_model=19`（32 枚 `UNBOUND` 全读
`LookupError: unknown provider model`，且模型名在自家包源码里一个整词都找不到），残差主体是 provider 码
`24 + 19 + 13 = 56` 枚，既不是声明也不是 runner。叙述在 §89–§91。

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
- `probes/incomplete_row_guard_check.py`（623 行、`sha16=96c2ea82968d5474`；波 26 定稿那一代是 606 行、
  `sha16=94ebdba8adb9ea33`，本目录 `runs/wave26-step0-second-axis.txt` 的两代并列处留着那一代的读数）：
  波 26 的 step 0 仪器，把
  "这一枚能不能靠一波 witness 转正"拆成**四条独立轴**逐枚打印：`bound`（注册表解析得到 fetcher）、
  `ruled_out` **与** `no_ledger`（本家 `NOT_DECL*` 台账带着它 —— cboe/intrinio 拼 `NOT_DECLARABLE`、
  econdb 拼 `NOT_DECLARED`，所以按属性名**前缀**匹配，不硬编码一个名字；"这家根本没有 `specs.py`"
  从前被并进同一个 truthy 串，于是 yfinance 那 26 枚读成"台账已拒绝"，实际是"没有台账可拒绝"，
  见 `runs/wave27-step0-block-axes.txt` 的两代对照）、`guard`（篡改文档撞得到本类带的码）、
  `row_class`（bound fetcher 造的类必须**就是** `contract_model(domain)`，不是长得像）。判定枚举六值
  `DOCUMENT_REACHABLE / DOCUMENT_TRIPPED / ROW_CLASS_NOT_CONTRACT / QUERY_ONLY / NO_CODE_SITE / UNBOUND`，
  梯子里 `row_class` 排在 `guard` 之前。六臂里两臂是为了修仪器自己才加的：`E_transport_predicate_discriminates`
  要求运输谓词两值都能读出（同一批 10 个 `extract_data` 站点，改成"动词 **AND** 该模块自己绑给 client
  的接收者"读 `after_counts={True: 3, False: 7}`；按属性名松匹配那一代读成什么，由
  `probes/transport_matcher_delta.py` 现测，不在这条臂里凭记忆写数），
  `F_row_class_identity` 三面要求同名替身读 `look_alike_name`、异名替身读 `no`、自己读 `match`，
  否则"分类器看不见"会被读成"这行不是替身"。
- `probes/declare_block_axes.py`（270 行、`sha16=485b41f8d02c8382`）：波 27 的 step 0 仪器，把上面那四轴
  指向**另一桶**总体 —— 已入库定价器判为 `DECLARE_AND_WITNESS` 的行（默认 `--provider yfinance`，26 枚）。
  它不自己数总体：桶来自 `declare_candidates._residue_needs_declare` + `_tag`，每枚读数由
  `incomplete_row_guard_check._row` 原样导入，运输面那一列由 `seam_surface_match` 的 AST 读提供，
  臂 D 再拿 `runs/wave26-step0-second-axis.txt` 里那条 `declare_face_by_provider=` 逐家对账 ——
  手挑子集冒充"整桶"就是这一臂存在的理由。七臂里 `G_ledger_cause_three_sided` 是上一枚仪器那次两成因
  压布尔的直接后果：一次运行里必须同时看到"台账带着这枚"`(True, False)`、"台账沉默"`(False, False)`、
  "根本没有台账"`(False, True)` 三种组合，否则第二轴又退回一个不能区分的布尔。换 `--provider` 跑一家
  **有** `specs.py` 的包只能出现前两种，这一臂就会红 —— 那是预期读数，不是臂坏了。
- `probes/ledger_axis_blast.py`（206 行、`sha16=4568cf22c27db38a`）：把拆开的那条台账轴指到**整个残差**
  （判定报告里状态不是 `OFFLINE_ACCEPTED` 的 84 枚 = `INCOMPLETE` 7 + `UNREVIEWED` 77），量这条缺陷的射程，
  而不只是 yfinance 那一桶。行读数仍由 `incomplete_row_guard_check._row` 原样给出；磁盘普查
  （`opendata/data/providers/*/specs.py` 存在与否）是同一事实的**第二次独立测量**。五臂里两处是它自己的
  第一次红教出来的：`C_conflation_identity` 要求分裂前那条一个字符串的谓词复述得精确
  （`82 = 11 + 71 + 0`，两因同时为真 0 枚），`D_cause_matches_disk` 拆成 `uniform_per_package`
  （台账因是**包**的属性：盘上有目录的 provider 必须整家同因）与 `package_names_are_bounded`
  （盘外名字 —— 本机现测 `biztoc`、`stockgrid` —— 的每一行必须读 `bound=False`，且盘内至少一行的
  `bound=True`，不许靠空集变绿）。第一代把这两条写成一条恒等，红在 `PASS=False`：报告里的 provider
  **名**可以根本没有包目录，两母数不同。`E_pricer_block_inside` 用入库定价器现算 yfinance 那 26 枚，
  与 `runs/wave27-step0-block-axes.txt` 的 `rows=26` / `yfinance_rows=29` 对齐，不等就抛。
- `probes/transport_matcher_delta.py`（209 行、`sha16=c90fcffbeafcad76`）：**只替换那条不一致的规则**的
  差分仪器。它 import 已入库探针的站点枚举、行总体与所有 helper，只把 `_transport_line` 换成 draft 2 的
  松规则（动词命中即算，接收者不查），所以两代读数只差一条规则。臂 A 先自证能复算已入库面
  （10 站点 `{True: 3, False: 7}`），臂 B 给松规则的面 `{True: 7, False: 3}`，臂 C 是差值 `flipped=4`，
  臂 D 是方向检查（松规则只会加站点），臂 E 把同一对规则搬到总体的 15 个 `transform_data` 站点上
  （draft 2 那四条假阳性真正所在的位置：strict `{True: 0, False: 15}` vs loose `{True: 4, False: 11}`）。
  八条 flip 逐行打印两代**实际匹配到的接收者**，从 AST 现取：
  strict 侧 `_client.get_json` / `_client.get_bytes`，loose 侧 `os.environ.get(...).strip` 与
  `REPORT_FILES.get`。接收者里的字符串常量一律替换成 `'...'` —— 那几个 provider 行的原文形状会被
  gitleaks 记到**本载体**头上；构建器用同一条正则两侧对测（捕获面 0 命中、被替换的那两行源码 1 命中），
  任一移动就抛而不是写进载体。
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
- `runs/wave26-step0-second-axis.txt`（608 行、`sha16=77f74c74be1c0bd4`）：波 26 step 0 只读载体，也是
  本目录里唯一**没有落地任何一行**的波次载体。三代探针 stdout 并置且各自带现算 digest：draft 1 那代
  被推翻的臂（`A_landed_positive government_us::CommodityPsdReport verdict=DOCUMENT_ADJACENT … PASS=False`，
  它错在只认 `transform_data`，而这一枚在 `extract_data` 的 `_client.get_bytes` **之后**拒绝）、
  draft 2 那代四条假阳性（其面里只有 `after_transport=True`，draft 2 **没有**打印过传输行号；
  到底是哪个调用让它们读成 True 由 `transport_matcher_delta.py` 臂 E 现测：
  `loose_matched=anchor.get@111`、strict 侧 `no_transport_call@0`）、draft 3 入库面逐字 58 行
  （`sha16=df2aec232c4ee006`；`shasum -a 256` 取管道外的 stdin 现算，`lines=` 用 `splitlines()` 与
  `wc -l` 同定义）。它的前一代 `4e1a8fa25ec96075`（57 行）故意留在同一载体的"就地更正"一节：那一代的
  `ruled_out` 列把"本家 `NOT_DECL*` 台账拒绝"与"本家根本没有 `specs.py` 台账"两种相反的成因压成同一个
  `True`（`NO_SPECS_MODULE:ModuleNotFoundError` 字符串是 truthy），修法是拆成 `ruled_out` + `no_ledger`
  两列而不是改口径。波 26 的裁决没有移动 —— `second_axis_face … convertible=0 arms_passed=6` 在两代之间
  逐字节相同，行的读数只有 `government_us::WeatherBulletinDownload` 一枚变了，载体里
  `rows_ruled_out_true before=6 after=5 ledger_and_cause_both_set=0` 就是这个差分的现算面（构建器不满足
  就抛，不打印）。
  裁决与三面冲突写在正文里，服务腿是**产品代码**摘录（`provider_model_query.py:208-212 / 316-320 / 322-328`）
  而不是测试 docstring 的转述；第一轴的"引擎必然把解码载荷交给 `transform_data`"也改成产品代码逐字摘录
  （`opendata/data/protocol.py:312-317`，并注明异步入口 `_fetch_async` 的两条腿 `:477→:486`、
  `extract_data_async :540→:559` 是同一对调用 —— 先前写的 `_engine/http_json.py:1462` 是本仓没有的文件，
  已按实路径重指）；两对替身类的字段面由载档构建器直接从四个类体 AST 里取
  （`fomc_pair_shared_fields=['url']`、`yield_curve_pair_shared_fields=['date', 'maturity', 'rate']`），
  不是手抄。下一波候选总体也在同一份载体里实测：`needs_declare=52` 中只有 28 枚读
  `DECLARE_AND_WITNESS`，且 `reconcile=28+24==52` 是构建器算的（不满足就抛，不打印）。
  末尾"复算配方"四条命令均可直接执行，且**本代已按顺序重跑过一遍**：每条的 `exit` 由运行自己重定向到
  文件、载档读盘（四条全 0、stderr 全 0 字节），四个捕获与载档所钉逐字节相同
  （`df2aec232c4ee006`、`9f61e0692da0a006`、`7b009c9374472c4d`、`c0bc8c8ff727cea6`）。同一轮还把它自己
  先前写的那句"declare 那次没记 `$?`，所以不主张退出码"撤了 —— 撤的办法是重跑一次拿到退出码，不是改口。
- `runs/wave27-step0-block-axes.txt`（367 行、`sha16=e00f1c5cb1b3b616`）：波 27 step 0 只读载体，本目录里
  第二份**没有落地任何一行**的波次载体。它把四轴指向 `DECLARE_AND_WITNESS` 那桶（yfinance 26 枚），面是
  `rows=26 verdicts={'DOCUMENT_REACHABLE': 15, 'NO_CODE_SITE': 10, 'QUERY_ONLY': 1} convertible=15`
  `seam_reachable=False`；可转的 15 枚 `is_contract=True` 15/15、`declared_is_contract=False` 15/15，
  所以缺的是 `row_model` 声明与见证模块（波 22 / 波 25 的形状），不是新的 transform 代码。它同时留了三样
  仪器自己的账：两代对照（分裂前 233 行 `6a24d7eff054deb9`、分裂后 263 行 `36804bf6460fb0d2`，
  `verdict_moves=0` 而 `ruled_out_moves=26`，即改的是仪器的成因列、不是任何一枚行的裁决；那一代 `$?`
  没落盘，所以载体明写 `pre_fix_exit_recorded=False` 而不主张退出码）、构建器自己的第一次红（把
  `lines_and_bytes` 的 `(行, 字节)` 按 `(字节, 行)` 解包，被波 26 那枚 measurer pin 当场拒掉，逐字原文在
  载体里）、以及"本步不重跑 judge"所依据的字节不变式（报告 `sha16=73f4ed196e73aef5` 仍被
  `runs/precommit-staged-scan.wave25.txt` 钉着，`350 / 266 OFFLINE_ACCEPTED / 7 INCOMPLETE`、
  `identity_set=5168e1e723999312` 与 yfinance 29 枚全 `UNREVIEWED` 都是从那份报告现算的）。
  构建器里的数字一律现算：`convertible_rows` 那一度被 `\S+` 在第一个空格截断成 1 枚，修成整段列表后
  载体的 `convertible=` 与列表长度、可撞 verdict 之和三者必须相等才写盘。
- `runs/wave27-step0-ledger-blast.txt`（146 行、`sha16=930fac41fa640308`）：波 27 step 0 的第二份只读载体，
  记 `ledger_axis_blast.py` 在**整个残差**上的射程：84 枚残差里 `bound=True` 52 枚，台账真拒绝只有
  **11** 枚（cboe 2 / econdb 2 / intrinio 1 / seeking_alpha 3 / wsj 3），台账沉默 2 枚（federal_reserve），
  没有台账模块可读 **71** 枚；分裂前那条两成因谓词主张的排除数是 82，也就是**多报 71 枚**，
  而其中第一轴还活着的（"没有台账**且**解析得到 fetcher"）是 **50** 枚。包普查那一面同时写清：
  盘上 32 家 provider 包里 18 家没有 `specs.py`，其中只有 5 家在残差里带着行。这台仪器自己的
  第一次红逐字留在载体里（第一代 `arms=5 arms_passed=4`、`D_cause_matches_disk … PASS=False`），
  红因是它把"报告里的 provider 名"与"盘上的包目录"当成同一母数：`biztoc`、`stockgrid` 两个名字在
  `opendata/data/providers/` 下没有目录，两枚行都读 `UNBOUND`。两次运行 stdout 逐字节相同
  （20 行 / 2656 字节 / `sha16=34dd136ed868c5b2`）、stderr 两次 0 字节、`$?` 从磁盘读回 0，
  闸门 `ruff format --check` / `ruff check` / `mypy` 三次退出码都是 0；构建器把每个 prose 数与两面
  对齐（逐家 `rows=`/`bound_true=` 相加、`82 = 11 + 71 + 0`、与 step-0 载档的 `rows=26` /
  `yfinance_rows=29` 交叉），任一不等就抛而不出载档。
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
  这份 README 与 precommit 载档定稿后再读是 `21`（8 + 13），其余四行逐字不变；波 26 的 step 0 探针与载体
  落盘后目录里是 10 个 `.py` + 14 个 `.txt`（`probes/__pycache__/` 的 `.pyc` 不在后缀总体内）；波 27 的
  step 0 落了**两**枚探针与**两**份载体（`declare_block_axes.py` + `ledger_axis_blast.py`，
  `wave27-step0-block-axes.txt` + `wave27-step0-ledger-blast.txt`），本机现数是 12 个 `.py` + 16 个 `.txt`，当代读数
  以入库前那次扫描自己打印的 `payload_files` 为准，不在这里替它填数。波 27 step 1 落一枚探针与五份载体、
  step 1b 再落七份载体之后，本机 `ls | wc -l` 现读是 **13 个 `.py` + 32 个 `.txt`**，两臂自扫描那一代打印
  `payload_files=45`（44 枚在册 + 载档自己）；入库前扫索引那一次读的是**文件表**（24 条路径），与这两个母数
  互不替换。波 27 step 2 再落**三**枚探针与**十五**份载档（13 份仪器载档 + 本节末尾那两条扫描载体）：
  同一台扫描取 1 读 **`payload_files=73`** = 16 个 `.py` + 45 个 `.txt` + 12 个 `.err`，取 2 读 **75**
  = 16 + 47 + 12，差的两枚就是这两份扫描载档自己（这台收两根下的**全部**文件，只排
  `__pycache__` 与 `.json`，所以 `.err` 在母数里 —— 那 59 字节的 yfinance 警告因此**是被扫过的**，
  `arm_a_findings=0` 就是它的惰性证明），而 `.err` 兄弟文件不进本波提交。
  上一代那份载档记到取 3、取 4 跑过但故意不写进来（载档不能扫自己最后一行）；波 27 step 2 的同一件事
  按本 README 末尾两条扫描载体那节逐代写明。
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

- `probes/witness_seam_kind_census.py`（411 行、`sha16=88870a6f957d9dba`）与
  `runs/wave27-step1-seam-kinds.txt`（44 行 / `624ddf9ea0a7ebee`）：波 27 step 1 的新仪器与它的七臂面。
  它否证了 §89.5 谓词后半"见证腿取引擎 `transport`"：臂 A 四枚**已入库**、无 `client_seam` 的行逐枚读
  `rows>0 transport_calls=1`（引擎 seam 本身活的），臂 B 对 `yfinance::EquityQuote` 读
  `transport_calls=0` + `YfinanceProviderError:YFINANCE_REQUEST_FAILED`，臂 C 对本家两个 seam 属性的读取数是
  (0, 0) 而 `http_transport_holders=5 shared_client_holders=6` 都非零，臂 F 从包里读出
  `_sdk` 的 12 个调用点最小 1 个位置参数。产品面 `E_declaration_face declared=15 expected=15
  row_model_is_contract=15 PASS=True` 走的是入库仪器 `incomplete_row_guard_check._row`，不是手抄清单。
  这台仪器自己崩过两次（臂 B 之前 `WitnessBlocked` 把载体压到 3 行；臂 A 漏了 runner 的 `credential_env`
  注入读到 `BENZINGA_CREDENTIAL_MISSING`），两次红连同修法逐字留在 §90.3。
- `runs/wave27-step1-declare-candidates.txt`（42 / `bb1acde0012fb212`）、
  `runs/wave27-step1-diff-face.txt`、`runs/wave27-step1-receipt-pins.txt`（6 / `3de420aed0e68f10`）、
  `runs/wave27-step1-block-axes-rerun.txt`（75 / `bfe05f3be880a7b2`）：同一轮另外四面。定价面
  `NEED_DECLARE 52 → 37`、桶 `DECLARE_AND_WITNESS 28 → 13`、逐家 5+2+1+9+8+12=37；四轴那台对本家改读
  `rows=11 convertible=0 arms_passed=6 arms_failed=1`，那一格**按设计是红的**（它的 `CARRIER` 常量指向
  上一代载档，红且 drift 只指 yfinance 一格才是"15 枚离开桶"的读数）；回执 pin 闸门仍 `slots=19382 stale=0`，
  而它的 B 臂在内存里把 runner 摘要换成占位值读到 `targeted_slots=1330` —— 任何 runner 编辑的代价是实测量出来的。
- `runs/wave27-step1-fullsuite-worktree.txt` 与 `runs/wave27-step1-fullsuite-head.txt`：全量归因两面
  （工作树 `35 failed, 26244 passed, 271 skipped in 590.09s`；HEAD 那份是 35 枚 id 的重跑 `35 failed in 10.98s`）。
  **这两行的数目相等不是集合相等** —— 由它们得出的"本轮没弄坏任何测试"已被 §90.8 的三面对照撤回。
- `runs/wave27-step1b-pin-rebase.txt`（73 / `1d4093d5a001dae4`）：那 15 行动了**另一道 tracked
  围栏**（`tests/fixtures/registry_batch5_snapshot.json` 的 31 条 `source_hashes_sha256`，判定者
  `tests/test_alert_matrix.py:39` + `tests/test_provider_domains.py:1363/:1368`；回执闸门看不见它）。
  重基走产出器 `docs/evidence/C103/batch5-source-pins/probes/pin_rebase.py --apply`：`matched=29 stale=2`、
  两枚 `iter2.py`/`market_iter2.py` 摘要按盘上现算写回、载荷摘要 `d434b9137178e0df… -> bcdca191aa39064a…`
  并重钉两处字面量站点，`bytes_delta=0` 且 candidate/counts/identities 六面不变；判定簇四模块 `117 passed`。
  载体里还留了这一步我自己的**三次仪器故障**（`-m ruff format` 是 zsh 词切分错的产物、`mypy` 被我在
  `^tests/` 暗区外指路径上无中生有 93 errors、把 JSON 夹具交给 ruff 导致 720 KB 喷进载档草稿）。
- `runs/wave27-step1b-judge-cluster.txt`（4）、`runs/wave27-step1b-fence-counterfact.txt`（6）、
  `runs/wave27-step1b-failed35-rerun.txt`（7）、`runs/wave27-step1b-seven-rebased-ids.txt`（10）、
  `runs/wave27-step1b-head-seven-reproducers.txt`（35 / `98180e493c957b91`）、
  `runs/wave27-step1b-refusal-gate.txt`（12 / `336e6cf122367d27`）、
  `runs/wave27-step1b-payload-scan.txt`：同一代的两面扫描 —— 两臂自扫描 `payload_files=45 payload_roots=2`
  `arm_a_findings=0 arm_b_findings=1 PASS=True scan_exit=0`（B 臂是在 `/tmp` 的**影子副本**里把一枚运行时生成的
  token 种进同一份 `declare_candidates.txt` 的第 31 行再扫 —— 仓内字节一个不动，这一臂只是为了证明闸门不是哑的），入库前扫索引 `24 files / 24 roots / unresolved=0 / findings=0 rc=0` 取 1 与取 2
  stdout 逐字相同（`identical_stdout=True`）。这七份合起来是重基后的面：判定簇四模块 `117 passed`；反例是活的 ——
  `stale_before_arms=0`、arm_A_forged `exit=1 4 failed, 2 passed`、arm_B_inert `exit=0 6 passed` 且它打印的
  `fixture_sha256` 与重基后夹具逐字节相同；三面对照证明"本轮弄坏 7 枚、修回 7 枚"（工作树重基后 `7 passed`；
  HEAD 干净树 `7 failed` 但原因是 `FileNotFoundError`；HEAD + 补齐那 15 枚被 ignore 的钉文件 `7 passed`）；
  产出器在 settled 之后读 `matched=31 stale=0` + `verdict: REFUSED` + `pin_rebase_exit=1`，那是拒写门不是判定红。
- 一条**先前就在、本轮既不制造也不修**的缺陷（task #60）：这道围栏 31 条母数里 `pinned_but_untracked=15`，
  全部在 `.gitignore:249 /docs/evidence/C100/**` 之下、`of_those_absent_in_the_head_worktree=15`。它按路径从盘上
  重算，所以本机绿、干净克隆红 —— 现在它是机器本地的围栏。`wave27-step1b-head-seven-reproducers.txt` 三面里
  第二面就是这条的形状。
- `probes/seam_reachable_axes.py`（225 行、`sha16=a6efe3f0b1f80f36`）：波 27 step 2a 的**乙支**仪器，把已落地的
  四轴测量器指向**可达性普查自己的总体**（`bucket=SEAM_REACHABLE mechanism=client_seam` 那 7 枚）而不是自己重挑
  一行。它不重打桶规则：分母是 subprocess 跑 `seam_reachability_census.py` 的 stdout，逐行列来自
  `incomplete_row_guard_check._row` 的原样 import。`C_population_reconciles` 要求"按状态逐枚选出的数"与"普查按家
  聚合的 `residual=` 之和"相等（两个来源，任何一侧悄悄缩小都是红，不是批次变小）；`D_selector_can_differ` 要求
  这台选择器能给非零（`blocked_rows=24 blocked_sample=['bls', 'finra', 'tmx'] overlap=0`）；
  `G_two_instruments_agree` 逐枚对两台仪器，`contradictions=0` 才是"两台说的是同一枚行"。
- `probes/counterfactual_runner_widening.py`（261 行、`sha16=e9e7c1dab90f8df3`）：step 2b 的**甲支**仪器，
  **在编辑 runner 之前**给加宽定价，一个被钉字节都不碰。要加的动词 = provider 包在共享客户端上调过的并集，
  且经已落地的受体规则（`_client_names` / `_is_client_receiver`）过滤，所以 BeautifulSoup 的 `tag.get(...)`
  冒充不了接缝调用；包里没有调过的动词**不会被提议**（波 24 那句备忘写的是"`request` / `post`"，这一台打印
  `post` 到底有没有被测到：`post_required=False`）。翻开哪几家、各带多少残差走普查自己的桶算术；每枚翻开后的行
  仍过 `_row`，因为四轴都不依赖假客户端 —— 今天读 `UNBOUND` / `ROW_CLASS_NOT_CONTRACT` 的枚加宽之后还是不可转。
- `probes/residue_axis_census.py`（374 行、`sha16=45f2cb2d7d3bfbab`）：step 2b 的主仪器。两支都 `convertible=0`
  之后不再按切片决定，把 `350 - 266 = 84` 整个残差与三台已落地仪器交叉，逐枚并列打印
  `status bucket verdict seam mechanism witness_module fake_ok name_in_package ruled_out declared witness_entry`
  加这三台**共同**蕴含的第一份工作（`_action` 按梯子取第一格：`new_provider_package` / `ledger_ruled_out` /
  `registry_publish_model` / `implement_missing_model` / `retarget_fetcher_output` / `shard_entry_only` /
  `shard_entry_needs_harness` / `add_code_carrying_guard`）。六面 `1_cross_tab`…`6_registry_name_axis` 是同一
  总体的三种切法，`4_action_totals` 打印 `actions` 与 `nested_sum` 好让最后一面被比较而不是被相信。
  `name_in_package` 那一轴把 32 枚 `UNBOUND` 按"模型名在自家包源码里能否作为**整词**找到"分开（两种活的大小
  不同：类已存在但没注册 vs 没人写过这个模型），`lookup_shape` 测它们在注册表里**怎么**失败而非断言（本代单值
  `{'unknown_provider_model': 32}`）。这台仪器第一版把两半统称 `rebind_fetcher`，名字轴的读数证明那是"未实现"
  被读成"接错线"；撤回写在 `_action` 的文档串与 §91 里，旧读数不抹。
- `runs/wave27-step2a-seam-reachable-axes.txt`（19 行 / `1a71df1c34df0c29`）与
  `runs/wave27-step2b-counterfactual-widening.txt`（43 行 / `950bf73b3b0619d2`）：两条分支的面，
  `seam_reachable_axes_face population=7 … convertible=0 witness_only=0 arms=7 arms_passed=7` 与
  `counterfactual_widening_face flipped_providers={'deribit': 5, 'tmx': 24} new_verbs=['request']
  post_required=False population=29 … convertible=0 arms=7 arms_passed=7`，两支 `identity_set` 与仓内已定面相同。
  前者另带 `1_declaration_axis … inside_NEED_DECLARE=3 outside=4 contradictions=0`，后者另带
  `G_declaration_axis inside=13 contradictions=0`。
- `runs/wave27-step2b-residue-axis-census.txt`（100 行 / `5613d9417fc0c446`，`residue_axis_census_exit=0`，
  `arms=8 arms_passed=8 arms_failed=0`）：整残差成像面。
  `2_actions … sums=84 registry_publish_model=0`、`4_action_totals actions=6 nested_sum=84 residue=84`、
  `5_needs_first_module … rows=75 of=84`、`6_registry_name_axis unbound={'absent': 21, 'present': 11} total=32`、
  `witnessable_today=0`。同一根探针的三份中间代留在旁边，代际可辨：`.v1-6arms`（96 行 / `86003dc768b1328f`）、
  `.v2-7arms`（98 行 / `fdcccf9c1358dd07`）、`.v3-armH-red`（100 行 / `8a963a5a99b6f0c3`，
  `residue_axis_census_exit=1`）。红那一份是"这条臂能红"的**唯一**证据：`H_name_test_identity` 的恒等式漏掉
  `NO_PACKAGE_ON_DISK` 那 2 枚（`unbound=32` vs `actions_sum=30`），又把两家本来就没有包的 `empty_blobs` 非空
  判成失败 —— 两条都是臂自己写错，不是行读数错。
- `runs/wave27-step2b-receipt_pin_consistency.txt`（10 行 / `f96636cd09d028cf`，
  `receipt_pin_consistency_exit=0`，stderr 0 字节）：**step 2b 之后**重取的回执 pin 面
  （`pin_face slots=19382 stale=0 before_ne_after=0 distinct_pinned_paths=536 arms=3 arms_passed=3`，
  B 臂 `stale=1330 targeted_slots=1330 substitute=scripts/quality/iter2_claims_probe_runner.py`）。
  它存在的理由是把"甲支加宽只算在内存里、被钉字节一个没动"钉成**同代**读数，而不是引 step 2a 那份更早的面。
- step 2a 把六台**已落地**仪器在同一条只读链上重跑，各成一份载档，用来先固定分母再谈新仪器：
  `wave27-step2a-seam_reachability_census.txt`（38 / `a3c93020cae34c4c`，`seam_census_units=35`、
  `residual_by_mechanism_sum=84`、`accepted_plus_residual=350`、`disk_only=['_engine', 'akshare', 'ths']`）、
  `wave27-step2a-readiness_census.txt`（9 / `aadf0418535c9af2`，`residue=84 bound=52
  buckets={'NEED_BINDING': 32, 'NEED_DECLARE': 37, 'NEED_SHARD_ENTRY': 15}`，声明位 `declared=15` 而
  `witness_entry=0` —— step 1 那 15 枚只是从"没声明"移到"没有见证分片"；这一份**没有** `.err` 兄弟文件，
  所以不主张它的 stderr 字节数，退出码只认载档自己的 `readiness_census_exit=0`）、
  `wave27-step2a-declare_candidates.txt`（42 / `d303e4983265e911`，
  `needs_declare=37 counts={'contract_in_module': 35, 'emits': 14, 'witness_entry': 0}`，逐家 tag 面
  `declare_face_by_provider` 里 `NEEDS_RETARGET` 22 / `DECLARE_AND_WITNESS` 13 / `NEEDS_IMPORT` 1 /
  `NEEDS_IMPORT_AND_RETARGET` 1，四面相加 37 与桶读数一致）、
  `wave27-step2a-declare_block_axes.txt`（75 / `243a04b5d8ed8fe4`，`declare_block_axes_exit=1`：红的仍是
  `D_population_reconciles here=11 carrier=26 drift=['yfinance:DECLARE_AND_WITNESS carrier=26 here=11']` 那一格
  —— 它的 `CARRIER` 常量还钉着上一代载档，**按设计是红的**，且 drift 只指 yfinance 一格正是"15 枚离开桶"的读数）、
  `wave27-step2a-receipt_pin_consistency.txt`（6 / `a8a570a80bdc7c80`，`slots=19382 stale=0`，本波没把任何 pin 弄旧）、
  `wave27-step2a-witness_seam_kind_census.txt`（43 / `843ab627634d862d`，`residue=84 subject_transport_calls=0`，
  stderr 单独落盘 59 字节，内容是 yfinance 那条 delisted 警告，逐字见 §91 仪器事故第 3 条）。
  这一代 step 2 十三次运行里有十二次把 stderr 重定向到同名 `.err`（本机现数 12 份，只有刚提到那一份非空），
  上面单列出来的 `wave27-step2a-readiness_census.txt` 是那一次缺的；**`.err` 不进本波提交** —— 所以
  "stderr 0 字节"这类句子读的是工作树属性，不是载档属性；要复算就按 §91 的配方自己重定向。
  载档里能自证的只有逐字 stdout 与末尾那一条 `<name>_exit=<digit>`。
- `runs/wave27-step2-payload-scan.txt`（29 行 / `3d7b2c0b1d1ed3eb`）：step 2 入库前的**两臂**扫描面，
  两次取像的母数是 73 → 75，差的正是这两份扫描载档自己（这台收 `probes/` 与 `runs/` 两根下的**全部**
  文件、只排 `__pycache__` 与 `.json`，所以 12 份 `.err` 兄弟文件也在母数内），而
  `arm_a_findings=0 arm_b_findings=1` 两次逐字不动：臂 B 把运行时生成的 token 种进
  `runs/declare_candidates.txt` 第 31 行的一份**一次性副本**，仓库字节不改，所以那个零不是哑计数器。
- `runs/wave27-step2-precommit-staged-scan.txt`（28 行 / `f0a1ed96e2d050ee`）：人群换成**索引本身**的
  扫描面 —— `git -c core.quotepath=off diff --cached --name-only -z` 的文件表交给
  `probes/staged_payload_scan.py --paths`，字节取自工作树。三代取像读 19 / 21 / 21 条路径，
  `unresolved=0 findings=0 rc=0` 三行逐字相同，`take3_vs_take2_all_five_fields_identical=True`。
  这两份载档都留了**一行取不到的滞后**：载档不能扫自己最后一行，take 3 扫到的 README 还没有本节这两条
  载体、扫到的本载档还是 16 行。take 4 是对"即将提交的那 21 枚字节"的最后一次复核，它的逐字读数放进
  **提交说明**而不是这里 —— 写进这里就会再动一次字节，把同一句"最后一行没被扫过"原样推到下一行。

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

波 26 那台的四条命令**逐字抄在** `runs/wave26-step0-second-axis.txt` 末尾"复算配方"里，第一条已在本机重跑
并重读到同一 `sha16=df2aec232c4ee006`（无管道，`$?` 直接取，stderr 0 字节；分裂前那一代读到的是
`4e1a8fa25ec96075`，重跑对不上它是预期的，不是仪器坏了）。它也是当代仪器：只要不动
`provider.py` / `domains.yaml` / 那两个 `models/federal_reserve_*.py`，重跑应当复现 `verdicts=` 那一行的
`{'ROW_CLASS_NOT_CONTRACT': 2, 'UNBOUND': 5}` 与 `convertible=0`；`identity_set` 必须仍是
`5168e1e723999312`，而 `verdicts` **允许**移动 —— 一旦某枚的 `row_class` 转成 `is_contract=True`，
就是那两枚之一的裁决被实施了，届时按"重基线而非放宽"处理：判定枚举六值不许增删，`F_row_class_identity`
的三面（`match` / `look_alike_name` / `no`）不许有任何一面变成不可达，否则这台仪器的第四轴自己就哑了。
第三台当代仪器是 `receipt_pin_consistency.py`，argv 抄在 `runs/wave25-receipt-pins.txt` 头部两条
（`--providers <本波分片>` 与全树各一次，都是仓库根执行）。它是**每一波入库前该跑的那一台**：
`arms_failed>0` 说明本波把某个共享文件的 pin 弄旧了，补产补签再提交；`C_rearm PASS=False` 是仪器坏了；
`before_ne_after>0` 则推翻上面那条"pin 无差分语义"的实测结论，那时要重基线而不是沿用本波的说法。

波 27 step 2 三台的 argv 逐字抄在 `开发验收记录.md` §91 的"复算配方"里，形状都是仓库根执行
`PYTHONPATH=. <门禁解释器> docs/evidence/C128/plane1-residue-readiness-20261017/probes/<name>.py`；
三台都会 subprocess 起普查（`seam_reachable_axes.py` 与 `residue_axis_census.py` 起 `seam_reachability_census.py`，
后者还起 `readiness_census.py`，`counterfactual_runner_widening.py` 起普查取桶算术），一次 60–90 s。
两台 step 2b 仪器是**反例式**的：它们把加宽算在内存里，不写 `iter2_claims_probe_runner.py` 一个字节，
所以跑完之后重取 pin 闸门仍读 `pin_face slots=19382 stale=0 before_ne_after=0 arms=3 arms_passed=3`
（`runs/wave27-step2b-receipt_pin_consistency.txt`，10 行 / `f96636cd09d028cf`，`receipt_pin_consistency_exit=0`、
stderr 0 字节），而它的 B 臂在内存里把 runner 摘要换成占位值读到 `stale=1330 targeted_slots=1330`
—— 这 1330 枚就是"真去加宽"的代价，本轮一次也没有付。重跑若移动读数，按"重基线而非放宽"：`E_verdict_enum` / `F_verdict_enum`
钉死判定枚举六值、普查桶五值不许增删，`C_widening_can_differ` 要求 flip 集合两侧都能不同
（对**已入库**表面每家至少缺一个动词、对加宽表面缺 0 个，任一侧空集就是反例没被施加），
`D_verbs_are_measured` 把 `new_verbs` 钉成"表面 ∪ 实测调用点"，所以凭猜测给假客户端加一个 `post`
会让这一臂变红而不是让数字变大。

本目录 16 台仪器（`ls probes/*.py` 现数）都只做普查：除自己的臂失败外退出 0，绝不改动任何被判定的面。波 23 起
（readiness、declare、payload 两臂扫描、seam 表面），波 24 收编 staged 扫描那一列，波 25 加两台正交轴
（可达性普查、后响应守卫）与一台入库前 pin 闸门，波 26 加四轴逐枚测量器，波 27 step 0 加台账轴拆分、射程
与那条运输规则的差分三台加 seam 种类普查，step 2 再加三支（乙支 7 枚定价、甲支加宽反例、整残差成像）。
新仪器同样只打印屏幕与臂，不判定任何行；两处例外都不落盘：pin 闸门的 C 臂在**内存里**造一枚假 slot，
step 2b 的甲支在**内存里**加宽假客户端。
