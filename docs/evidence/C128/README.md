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

**波 28 五步落了 10 枚，`266 → 276/350`，而每一步的"下一枚在哪"都是仪器读出来的，不是状态桶猜的**
（`docs/evidence/C129/` 依旧不存在）。step 0 先把残差普查那条只会重复自己的 F 臂改成两面控制
（`witnessable_today=0` 从此由"表面 ∪ 实测调用点"现算）；step 1 给 `retarget 24` 那一格定价，价钱是
**2 枚**、两枚都挂在待裁 #57；step 2 落 nasdaq 9 枚（9 行 `row_model` 声明 + 本仓首份该 provider 见证模块，
五相 45 枚全 PASS、0 处 harness 改动，`266 → 275`）；step 3a 把两台波 27 仪器里钉住上一代落地面的常量
追平，并用新驱动证明追平那条臂**仍然能红**（锻造回上一代时两台各只红 `A_landed_report_face`）；
step 3b 是纯仪器轮，把 §94 (b) 支那句谓词读成 `answer=NO` —— 卡住 yfinance 15 枚的是**替换形状**
（产品侧 1 枚站点、`min_positional=1`，runner 装进去的 `lambda _c=client: _c` 元数 `(1,0)` 会把实参原样退回）
而不是假客户端的动词集，改 runner 的价按 pin 格子读 1650。**step 4 落这一波的最后一枚**：
`federal_reserve::YieldCurve` 的行类从宏模型换成域契约类
`FederalReserveYieldCurveObservation`，五相 `exit=0 status=PASS`，`275 → 276/350`，
而这枚动的是 `INCOMPLETE`（`7 → 6`），`UNREVIEWED` 68 一枚没动 —— 我起初按"UNREVIEWED 少一枚"写常量，
驱动立刻红给我看。残差 `75 → 74`、`NEED_DECLARE 28 → 27`、`client_seam` 机制价 `7 → 6`；
五台钉落地面的仪器全部换代并各自被证明仍然扳得动，其中 `yfinance_shard_entry_price.py` 经
`kinds.LANDED_ACCEPTED` 自动追平、重取读 `VOID=no`，那笔 runner 价顺带从 1650 走到 **1656**。step 4 之后另有
三取收尾：**4e** 把第二道 tracked 围栏（batch5 `source_hashes_sha256`）在 step-4 树上重取，`matched=31 stale=0`
与 step 2 那份逐字同代，并另落一份 31 个 pin key 的清单载档把"`federal_reserve` 一个 key 都不在这道围栏里"
从抽查变成集合陈述；**4f** 是字面引用勾出来的 —— 产品与仪器散文把那条 frozen guard 的行号写成 `:382-403`，
实测在 `:392-413`（assert `:406`），而改引用动的是被 claim 钉住的见证分片字节，于是整条
federal_reserve produce→sign→merge→judge 重落一代，六台只读面同代再取，**plane 1 一格没动，仍 `276/350`**；
**4g** 把 step 4 自己改进 handoff 判定的那条 stale 名回退补上反例，六臂 12 格全绿、两个 raise 位实测在 `:379`
与 `:388`，同样一格计数没动。
叙述在 §92–§97。

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
- `probes/ledger_axis_blast.py`（波 28 step 3a 后 222 行、`f2a4bfd8a151491a`；step 4 追平后 223 行、11549 字节、
  `495b95a057f8c92a`；写成的那一代是 206 行、`4568cf22c27db38a`）：把拆开的那条台账轴指到**整个残差**
  （判定报告里状态不是 `OFFLINE_ACCEPTED` 的行 —— nasdaq 落地那一代是 84 枚 = `INCOMPLETE` 7 +
  `UNREVIEWED` 77，波 28 step 4 现测 74 枚 = 6 + 68），量这条缺陷的射程，
  而不只是 yfinance 那一桶。行读数仍由 `incomplete_row_guard_check._row` 原样给出；磁盘普查
  （`opendata/data/providers/*/specs.py` 存在与否）是同一事实的**第二次独立测量**。五臂里两处是它自己的
  第一次红教出来的：`C_conflation_identity` 要求分裂前那条一个字符串的谓词复述得精确
  （`82 = 11 + 71 + 0`，两因同时为真 0 枚），`D_cause_matches_disk` 拆成 `uniform_per_package`
  （台账因是**包**的属性：盘上有目录的 provider 必须整家同因）与 `package_names_are_bounded`
  （盘外名字 —— 本机现测 `biztoc`、`stockgrid` —— 的每一行必须读 `bound=False`，且盘内至少一行的
  `bound=True`，不许靠空集变绿）。第一代把这两条写成一条恒等，红在 `PASS=False`：报告里的 provider
  **名**可以根本没有包目录，两母数不同。`E_pricer_block_inside` 用入库定价器现算 yfinance 那 26 枚，
  与 `runs/wave27-step0-block-axes.txt` 的 `rows=26` / `yfinance_rows=29` 对齐，不等就抛。
  step 3a 只把那条**钉住上一代落地面**的常量追平，并被钉的旧值留在源码里（`LANDED_STATUSES_PREVIOUS`），
  由新面 `A_rebase_from_landed` 逐状态打印 `rebase_delta`（step 3a 那代现测 `OFFLINE_ACCEPTED +9 /
  UNREVIEWED -9`；step 4 落 `federal_reserve::YieldCurve` 之后现测 `{'INCOMPLETE': -1,
  'OFFLINE_ACCEPTED': 1, 'UNREVIEWED': 0}`，见 `runs/wave28-step4b-landed-face-rebase.txt`），和为 0、
  `identity_set_moved=False`，所以"追平"是被比较出来的、不是被我改出来的。
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

- `probes/witness_seam_kind_census.py`（波 28 step 3a 后 416 行、`139f5b6e79d4edc0`；step 4 追平后 417 行、
  20089 字节、`a8d7ef6a7d4ada09`；写成那一代是 411
  行、`88870a6f957d9dba`；同样只追平 `LANDED_ACCEPTED` 并把旧值留在 `LANDED_ACCEPTED_PREVIOUS`，面末追加
  `previous_accepted=275 accepted_delta=1 identity_set_moved=False`；step 3a 那代这一格是
  `previous_accepted=266 accepted_delta=9`）与
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
- `probes/residue_axis_census.py`（波 28 step 3a 后 402 行、`sha16=80f6c70369be8be4`；波 28 step 0 那一代是
  401 行、`5145178f8a586c04`；入库那一代是 374 行、
  `45f2cb2d7d3bfbab`）：step 2b 的主仪器。两支都 `convertible=0`
  之后不再按切片决定，把 `350 - 落地的 accepted` 整个残差（写成那一代 `350 - 266 = 84`；波 28 step 4 现测
  `350 - 276 = 74`，step 3a 那代记的是 `350 - 275 = 75`；step 3a 只改了 docstring 里那句会过期的算术，改成由 `C_population_identity` 现算）
  与三台已落地仪器交叉，逐枚并列打印
  `status bucket verdict seam mechanism witness_module fake_ok name_in_package ruled_out declared witness_entry`
  加这三台**共同**蕴含的第一份工作（`_action` 按梯子取第一格：`new_provider_package` / `ledger_ruled_out` /
  `registry_publish_model` / `implement_missing_model` / `retarget_fetcher_output` / `shard_entry_only` /
  `shard_entry_needs_harness` / `add_code_carrying_guard`）。六面 `1_cross_tab`…`6_registry_name_axis` 是同一
  总体的三种切法，`4_action_totals` 打印 `actions` 与 `nested_sum` 好让最后一面被比较而不是被相信。
  `name_in_package` 那一轴把 32 枚 `UNBOUND` 按"模型名在自家包源码里能否作为**整词**找到"分开（两种活的大小
  不同：类已存在但没注册 vs 没人写过这个模型），`lookup_shape` 测它们在注册表里**怎么**失败而非断言（本代单值
  `{'unknown_provider_model': 32}`）。这台仪器第一版把两半统称 `rebind_fetcher`，名字轴的读数证明那是"未实现"
  被读成"接错线"；撤回写在 `_action` 的文档串与 §91 里，旧读数不抹。
  **第二处缺陷是 F 臂自己**：第一版 `arm_f = len(witnessable) == actions["shard_entry_only"]`，而这两侧在
  同一个循环体里由同一个 `_action` 返回值增补（`actions[action] += 1` 与 `if action == "shard_entry_only":
  witnessable.append(...)`），所以这条臂**永远不可能红**，随它印出的 `witnessable_today=0` 不是测出来的结论。
  波 28 step 0 把它改成两面的：把两枚已入库恒等控制**实测**到的 `verdict / ruled_out / name_in_package` 原样喂
  `_action`，只把"假客户端能不能答"这一根轴强制拨成 True 再拨成 False，要求两侧分别落在
  `shard_entry_only` 与 `shard_entry_needs_harness`，并要求控制自己真实读到的 `fake_answers_all` 为 True
  （于是前一支不是反例而是直接测量）。残差读数仍是 0，多出来的是"这台计数器能数到一"。
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
- `runs/wave28-step0-residue-axis-census.txt`（104 行 / `5152c71602d48c46`，`residue_axis_census_exit=0`，
  stderr 0 字节，`arms=8 arms_passed=8 arms_failed=0`）：F 臂改两面之后的**同代重取**面，头部把与上一代
  共有面的比对写成一行 `face_lines_old=99 face_lines_new=99 drift=2 drift_offenders=[]`（差的两行就是
  `F_witnessable_today` 的新旧两条，符号差集，别的 98 条面逐字未动 ⇒ 这一改没重开任何一格）。新那条逐字：
  `F_witnessable_today count=0 rows=[] counter=0 controls=2 control_actions_on_harness=
  ['shard_entry_only'] control_actions_off_harness=['shard_entry_needs_harness']
  control_real_fake_ok=[True, True] PASS=True`。判定面一字未改：`residue=84`、六格动作表
  `retarget 24 / implement_missing_model 19 / shard_entry_needs_harness 15 / ledger_ruled_out 11 /
  add_code_carrying_guard 13 / new_provider_package 2`、`witnessable_today=0`、`rows_without_module=75`、
  `identity_set=5168e1e723999312`。波 27 那四份（成像面 + 三份中间代）留在原地不覆盖，代际按 sha 辨。
- `probes/retarget_rowclass_counterfactual.py`（355 行、`sha16=f577fecdaa3f17ea`；py_compile / ruff check /
  ruff format --check / mypy 四道全 0）：波 28 step 1 的**内存反例**定价仪器，对象是上面那张动作表里最大的
  一格 `retarget 24`，`iter2_claims_probe_runner.py` 与任何模型、判定文件都一个字节不动。它把每枚目标行**自己
  实测到的面**重新喂给已落地的 `incomplete_row_guard_check._verdict`，只开一把闸（先 `declared`、再
  `is_contract`），再用已落地的 `residue_axis_census._action` 分类，判定字符串一律从 `axes.VERDICTS` 与
  `ROW_CLASS_NOT_CONTRACT` 常量取 —— 打印出来的每个 verdict/action 是那台分类器对一根我没碰的轴的读数，
  不是我打的标签。总体取自同一个 `readiness_census._residue`，臂 A 把恒等集与
  `runs/wave28-step0-residue-axis-census.txt` 里那 24 枚逐枚对齐。九臂：A 总体对齐；B 惰性重喂（两把闸都不开
  必须仍读 `ROW_CLASS_NOT_CONTRACT`，否则这台仪器判的已经不是这批行）；C 开 `declared` 与开 `is_contract`
  两条路线必须给出同一个判定；D 开闸后 24/24 全部离开守卫；E 三枚非目标对照 = 两枚已入库正例
  （`government_us::CommodityPsdData` / `::CommodityPsdReport`）加一枚负例（`::WeatherBulletinDownload`），
  要求"闸本来已开 ⇒ 翻动是 no-op"，`vacuous=False`；F 反例判定不许离开枚举；G 落地格与 `_action` 嵌套对账
  （`shard_entry_only` 由分类器和由 verdict+fake 两条路各数一次，两侧都要等于 2）；H 用
  `contract_fqn ∈ emitted` 这第二路重推 `is_contract`，不一致集与不可解析集都必须为空；I 是 H 需要的控制臂 ——
  在**内存 deepcopy** 里把 `deribit::FuturesCurve` 的 `is_contract` 取反，要求"未改的那面不被 flag、伪造的那面
  被 flag"。这台同样是只读仪器：除自己的臂失败外退出 0，而
  `empty_emitted_with_ctor_text_in_package` 那一格**只当下界打印**（文本搜索能看见 AST 走查不拥有的构造），
  不参与任何臂。
- `runs/wave28-step1-retarget_rowclass_counterfactual.txt`（64 行 / `d6f0fa9a415490ce`，
  `retarget_rowclass_counterfactual_exit=0`，stderr 0 字节，`arms=9 arms_passed=9 arms_failed=0`）：step 1 的
  当代面，逐枚 `rowcf …` 24 行 + 九臂 + 一条 `rowclass_counterfactual_face`。头部记了三代字节相同
  （builder 自己那取 == take4 == take3，35/35/35 行）。读数
  `cf_verdicts={'DOCUMENT_REACHABLE': 20, 'NO_CODE_SITE': 2, 'QUERY_ONLY': 2}`、
  `cf_actions={'add_code_carrying_guard': 4, 'shard_entry_needs_harness': 18, 'shard_entry_only': 2}`、
  `landing_today=2 landing_needing_edit_only=2 identity_set=5168e1e723999312` —— **这一格的价钱是 2 行而不是 24 行**，
  而那 2 行恰是 `federal_reserve::FomcDocuments` / `::YieldCurve`，已经挂在待裁的 #57 上，并且被
  `tests/test_federal_reserve_iter2_models.py:382-403`（`test_declared_row_models_are_their_domain_contracts`，
  assert 在 :399，要求 `row_model is contract_model(domain)`；**波 28 step 4f 就地更正**：该 guard 当代实测在
  `:392-413`、assert 在 `:406`，`382-403`/`:399` 是写这代时的行号、留着不改，而这两枚的命运此后分开 ——
  `::YieldCurve` 在 step 4 换成域契约类后落地，仍被这条路挡住的只剩 `::FomcDocuments`）挡住，所以"给它们加一行声明"这条路本身不成立。
  其余 22 枚**根本不构造契约类**（`emitted=[]` 而契约存在），第一版 H 臂把这个形状读成"解释不了"是臂自己写错 ——
  已落地的 `_row_class_face` 只走 fetcher 自己 MRO 内的类与同模块 helper，服务路径造普通 dict 就是这种合法形状。
  两份红代（take 1 崩在自己的打印句：`raises` / `code_raises` 是 int 却取了 `len()`，崩在总体已算出之后、
  任何臂打印之前，载档整份丢失；take 2 34 行全印、`arms=8 arms_passed=7`、H 在 22/24 上红）留在
  `runs/wave28-step1-red-generations.txt`（63 行 / `d1f2d692dfe9d8dc`，`red_take2_exit=1`、stderr 0 字节）旁边，
  因为一条从未见过红的臂还算不出"它能红"。载档里那句"emitted 集合 == `cf_action=shard_entry_only` 集合"由
  builder 从这次 stdout 的逐枚行现推（还跟 `shapes` 那格的 2 交叉），不等就**拒绝落盘**，所以它是测出来的等式，
  不是散文里的断言。复算配方：仓库根执行
  `$PY docs/evidence/C128/plane1-residue-readiness-20261017/probes/retarget_rowclass_counterfactual.py`
  （门禁解释器，无管道，`PYTHONPATH` 有意不设 —— 兄弟模块靠脚本目录进 `sys.path`，这台没有测它、也不声称测了）。
  作废条件与"重基线而非放宽"：这批一旦有行落地，`population_residue` 与 `targeted` 都会移动，届时按新总体重取；
  不许动的是 B 臂（不开闸必须仍读 `ROW_CLASS_NOT_CONTRACT`）、E 臂（`vacuous=False`，两枚正例仍要在
  `DOCUMENT_REACHABLE` / `DOCUMENT_TRIPPED`）、F 臂（判定不许离开六值枚举）、I 臂（伪造一次必须被 flag、未改必须不被
  flag）；`landing_needing_edit_only` **允许**长大，但只能靠 G 臂两条路各数一次相等的 `shard_entry_only` 长大。
- `runs/wave28-step1-receipt_pin_consistency.txt`（14 行 / `50689a02b8aa8a94`，`receipt_pin_consistency_exit=0`，
  stderr 0 字节）与 `runs/wave28-step1-batch5_fence_dry_census.txt`（18 行 / `0c5f9fb70d1277d6`，
  `pin_rebase_exit=1` = 设计中的拒绝而不是红）：step 1 那两个 pin 平面的**同代**重取。前者读
  `pin_face slots=19382 stale=0 before_ne_after=0 distinct_pinned_paths=536 arms=3 arms_passed=3`，后者读
  `pin_entries=31 matched=31 stale=0 missing=0` + `verdict: REFUSED - no stale pin to re-base`。新仪器只读不写，
  两面都该读"无差分"，这一条就是它没被钉字节改动的证据；哪天 `stale>0`，说明有人把反例做成了真改动，那时要
  重产补签，而不是重跑反例。
- `runs/wave28-step1-payload-scan.txt`（35 行 / `68c2df076c407aba`）与
  `runs/wave28-step1-precommit-staged-scan.txt`（23 行 / `b701699afc582a77`）：本步入库前的两臂面与索引面。
  母数换代可追：step 0 收在 80（16 `.py` + 52 `.txt` + 12 `.err`）→ 本步 take 1 读 85（+1 探针 +4 载档）→
  take 2 读 86 → 两份扫描载档写完现数 87（= 17 `.py` + 58 `.txt` + 12 `.err`，两份扫描载档各算一份）。
  两臂四格逐字相同、唯一差格是母数自己，臂 B 仍往一次性副本种运行时生成的 token
  （`B_PLANTED_FOUND rule=generic-api-key at=declare_candidates.txt:31`）。索引面取 1 只扫到 6 条：缺的那一条是
  本记录自己 —— §93 那时还不存在、字节仍等于 HEAD，builder 把这条**印出来**（`not_in_diff_cached=[记录]`、
  `bytes_equal_HEAD=True`）而不是默认七条齐全；取 2 扫 8 条，`unresolved=0 findings=0 rc=0` 两代相同。两份载档
  末尾同样写全了自己取不到的字节：README 与本记录在 take 2 之后才补上这些条目，最后一次复核的逐字读数住在
  提交说明里。
- 波 28 step 2（产品轮：nasdaq 9 枚落地，`266 → 275/350`）新增四台仪器，全在 `probes/` 下，argv 逐字写在各自载档头部：
  * `probes/w28s2_run_chain.py`（280 行 / `sha16=2beb0d72a0547559`）：把这一轮的 15 份载档一次落成一条链
    （producer → recorder → merge×2 → judge → readiness → residue → seam → 两个 pin 平面 → ruff / ruff format /
    mypy / bandit / pytest → 两份面），每格自带 `*_exit=<digit>` 与自己的 argv。**这台是写盘链**，起的是已入库
    的判定件，所以它一次跑就换代：重跑之前先想清楚要不要重产 45 枚回执。头部把本步那次事故逐字记在仪器里，
    不只在散文里。
  * `probes/receipt_drift_counterfact.py`（204 行 / `4bd1d10e3d67faa4`；波 28 step 4 追平落地面后 225 行、
    8836 字节 / `56556b389eb6a5e2`；step 4g 再把它 format 重排为 223 行、8822 字节 / `5bfdb04d9ac027d0`，
    见本节末 4g 条目；载档常量 `CARRIER_NAME` 也从 `wave28-step2-…` 改名为
    `wave28-step4c-receipt-drift-counterfact.txt`，免得这台把已提交的 step 2 那份整体重写掉）：回执漂移的三臂反例。A 读绿代；
    B 用 `--output` 指向草稿文件重跑 producer（canonical claims 文档不动）**然后重跑 judge**；C 按代次顺序
    重落 produce→sign→merge→judge。面（19 行 / `113cc6276ee878be`，`receipt_drift_face PASS=True`）：
    `A_pre accepted=275 drift_rows=0` → `B_after_rotate accepted=266 drift_rows=9 nasdaq_statuses=['STALE']` →
    `C_after_reland accepted=275 drift_rows=0`。**机制**：回执路径由 `--evidence-root` 派生
    （`scripts/quality/iter2_claims_producer.py:233` 拼路径、`:262` 落盘），`--output` 管不到它；回执嵌
    `machine_execution.executed_at_utc`（`:251`）而 claim 钉回执字节的 sha（`:282`），判定件
    `iter2_model_evidence.py:461` 的 `_validate_receipt` 在 `:489` 追加 `f"{label}_receipt_hash_drift"` ⇒ 整批
    STALE。所以"只改 `--output` 的复算性重跑"是假安全，复算必须把 `--evidence-root` 一起指走。这台仪器自己还
    红过一次：首跑 `arm_B_expected_266_nine_drift=False`，因为 `read_face()` 只读盘上报告、B 臂没重跑 judge，
    于是印的是 A 的判定回声 —— **不重新测量的臂等于没有臂**，补 `B_rejudge` 之后三臂才各自命中。
  * `probes/w28s2_shard_faces.py`（142 行 / `070724a974349b50`；step 4 只把引用那台漂移仪器的**散文**换代，
    一个计数器没动，现 143 行、6983 字节 / `0841716818c8f6de`）：两份只读面。复算性面
    `claims_array: produced=0b0c3ff3a5619e74 signed=0b0c3ff3a5619e74 equal=True` +
    `envelope_delta keys_only_in_produced=['generated_at_utc', 'generated_by', 'notes']
    reviews_produced=0 reviews_signed=9` + `array_comparison_can_move=True`；一致性面
    `merged_nasdaq_claims=45 identities=9 verdict_agreement produced_eq_signed=True signed_eq_merged=True
    receipt_digest_mismatch_on_disk=0 [] judge_nasdaq_issue_rows=0 review_rows=9 of 9`。这里修掉一条我自己写的
    坏谓词：先前对**整份文档**取哈希判"可复算"，于是 recorder 换信封被读成不一致；有意义的比较是 claims 数组
    加信封差分两行，且必须配一格能红的（翻一枚 verdict 为 FAIL，数组哈希就得动）。跨代那一半按**引用而非重算**
    标注，因为它对照的草稿文档已在比较后被删；同一论断的可重算形式交给上面那台的 C 臂。
  * `probes/w28s2_lint_pass.py`（91 行 / `sha16=b18e5988ee9587be`）：一次性改写器（补 docstring、给
    `subprocess.run(` 加 S603 noqa、把固定 `/tmp` 名换成 `tempfile.mkdtemp()`）。它把 25 条 findings 收到 11 条，
    其余手工收。**它的错也记档**：按"函数名→一句话 docstring"表填法时把两支不同探针的 `main()` 填串了行，
    读回才发现 —— 脚本改文档字符串这件事本身不可信，必须逐支读回。
- 换代面（step 2 的载档，全部同代）：producer `wave28-step2-producer.txt`（52 行 / `12d232330c7f452d`，
  `claims emitted=45 (45 PASS, 0 FAIL, 0 BLOCKED) = 9 row(s) x 5 phases`、45 条子进程逐枚 `exit=0 status=PASS`）；
  recorder（15 行 / `984b8d6e6bf954fc`，`signed=9 refused=0 reviews_total=9`，先印 `re-signing: dropped 9 earlier
  review(s)`）；merge 两取（`3dc44471f501a6c3` / `c2df71864efe1575`，`shards merged=25 claims=1410 identities=282
  reviews=275 accepted_identities=275`，幂等格 `take1=8027e19a441638fa take2=… identical=True`）；judge
  （5 行 / `81372ec2c1a2dcb6`，`350 identities, 275 offline accepted`）。换代用两把尺核：committed 侧
  `git show HEAD:` 的报告是 `accepted=266 status_counts={'INCOMPLETE': 7, 'OFFLINE_ACCEPTED': 266,
  'UNREVIEWED': 77}` 且 nasdaq 9 枚 `['UNREVIEWED']`；工作树侧 `accepted=275 … 'UNREVIEWED': 68` 且 9 枚
  `['OFFLINE_ACCEPTED']`、`issue_rows=0` —— `77-68=9`、`275-266=9`、"9 枚换状态"三处对齐，`INCOMPLETE` 仍 7。
- 两个 pin 平面在漂移反例**之后**同代复核（C 臂会重产回执，这步不是可选）：
  `runs/wave28-step2-receipt_pin_consistency.txt`（9 行 / `89cb6e499ff14c8f`）读 `slots=19850 stale=0
  distinct_pinned_paths=558`、`receipt_files=1375`；`runs/wave28-step2-batch5_fence_dry_census.txt`
  （11 行 / `07ed6b5a0aa67c8f`）读 `pin_entries=31 matched=31 stale=0 missing=0` + `verdict: REFUSED`
  （`batch5_fence_dry_census_exit=1` 是 `--apply` 有意不传时的设计拒绝）。与 step 1 面做差：
  `slots 19382 → 19850`、`distinct_pinned_paths 536 → 558`、`receipt_files 1330 → 1375` —— 最后这一格差的
  **正好是 45**，等于本波新增的回执数，所以它是测出来的而非抄来的。落地之后我又把 pin 面重取一遍，逐格与
  载档相同，才算"反例留在绿代"。B 臂 `substitute=scripts/quality/iter2_claims_probe_runner.py
  stale=1375 targeted_slots=1375` 是"改这台 runner 会脏 1375 枚"的可红控制；本波 write-set 里 runner 与
  harness 文件 0 处，所以 §93 给 nasdaq 那 9 枚标的 `shard_entry_needs_harness`（"落地要先加宽接缝"）被这批
  自己的落地否证 —— 走的是已入库的 `shared_http_client` 接缝。
- 波 28 step 3a（把两台波 27 仪器里那条**钉住上一代落地面**的常量追平，被判定量一个没动）：
  `probes/landed_face_rebase.py`（278 行、`5db7f6d97a5083e4`；step 4 换代后 282 行、13196 字节、
  `86844e72941374b5`）与它的载档
  `runs/wave28-step3a-landed-face-rebase.txt`（62 行 / `sha16=70edc11b36ef2102`；第 24 行是本次运行的
  `chain_exit=0`，其后是四次取的账）。
  它 import 那两台 subject 并在**同一进程里各跑三遍**同一份报告字节：出库常量 → 把常量锻造回波 27
  那一代 → 复原；两台 subject 的追平编辑发生在本步、run 1 之前，而驱动这一趟自己一个字都不写它们，
  `setattr` 只作用在已导入模块的内存对象上。六臂两枚 subject 全绿
  （`subjects=2 arms=12 arms_passed=12 arms_failed=0 PASS=True`），关键是**回锻造那一遍必须红**：
  两台各自只红一臂 `forged_failed_arms=['A_landed_report_face']`，其余五臂照常，
  `exits first=0 forged=1 restored=0` —— 追平之后的那条臂仍然能红，所以它不是一句"我改了数字"。
  两臂之间被报告决定的计数器逐字节不动（`residue=75/75`、`identity=5168e1e723999312/…`、
  `accepted=275/275`、dict 型 subject 的 `statuses` 两侧相同），这就是 `D_measurement_unchanged`；
  追平的差由 `A_rebase_from_landed` 现算：`rebase_delta={'INCOMPLETE': 0, 'OFFLINE_ACCEPTED': 9,
  'UNREVIEWED': -9}`、`delta_sums_to_zero=True`、`identity_set_moved=False`。
  `E_restored_green_and_aging_named` 教出一条本目录此前没写进臂判据的仪器属性：
  `witness_seam_kind_census` 第一遍会把 fixture token 种进 `os.environ`，第三遍读
  `granted=none`，所以同一进程里 run1 与 run3 的 stdout **必然差两行**，这一臂判的是"差的每一行都
  是 grant 行"（`aging_lines=2 all_grant_lines=True`），而不是"输出全等"。写载档这件事本身也红过一次：
  take 1 `arms_passed=11` 红在 `D_measurement_unchanged=False`，坏的是驱动自己（它向 dict 型落地面
  要一个裸 `accepted=` 计数器，读到 `MISSING`），而那一遍已经显示回锻造只红 `A_landed_report_face`；
  take 2 绿、take 3 只因自己载档第 3 行两处被隐式字符串拼接吞掉的空格重取，两次绿取 stdout 只差
  载档字节数一行（3753 → 3755）；take 4 是**探针树自己的卫生闸门**逼出来的：`ruff check` 对本文件读
  2 枚 E741（`l` 是歧义变量名）、`ruff format --check` 要重排它（其余 21 个文件本就干净），改法是两处
  推导变量改名 + 格式化器重排，**一个臂、一个计数器都没动**，重跑的 stdout 与改前那趟绿取**逐字节相同**
  （diff 退出 0）。逐字红面与四次取的账都追加在那份载档末尾，载档里也写明了"驱动会整体重写本文件"这个
  滞后。同代另落 `runs/wave28-step3a-probes-hygiene.txt`（54 行 / `sha16=8578da2b67e19a46`）：红取与
  绿取两份都在里面，母数 `probe_py_files=22` 由 glob 与 ruff 各数一次相符（step 2 那份记的是 21）。
  同代还重取了 pin 面（`runs/wave28-step3a-receipt_pin_consistency.txt`，10 行 /
  `sha16=991839ac9e643165`，`receipt_pin_consistency_exit=0`、stderr 0 字节、1.774 s 墙钟）：
  `pin_face slots=19850 stale=0
  before_ne_after=0 arms=3 arms_passed=3 PASS=True`，B 臂
  `B_forced stale=1375 targeted_slots=1375 substitute=scripts/quality/iter2_claims_probe_runner.py
  must_equal_targeted=True` —— 本步 write-set 里没有 runner 与 harness，所以这 1375 又是一次没付的价。
  入库前的两臂扫描落在 `runs/wave28-step3a-payload-scan.txt`（7 行 / `sha16=11f974b0e37e61ff`）：母数换代
  可追，step 2 那代记的是 113（21 `.py` + 80 `.txt` + 12 `.err`），本代 **118** = 22 + 84 + 12，三个母数
  各用 `ls` 数一次再相加核对，差值是本步的 1 台驱动 + 4 份载档。`payload_roots=2` 仍是 probes+runs 两棵树，
  `docs/` 的两份叙事不进这个母数（那由 `staged_payload_scan.py` 的索引面负责）。判定四格逐字照旧：
  `arm_a_findings=0 arm_a_rc=0`、`arm_b_findings=1 arm_b_rc=1`、`PASS=True`、`scan_exit=0`；臂 B 种的
  是运行时生成的 token，只进临时目录里 `declare_candidates.txt` 的副本
  （`B_PLANTED_FOUND rule=generic-api-key at=declare_candidates.txt:31`），仓库字节一个不改。
  一处自扫描的构造要知道：载档由 shell 重定向创建，`_stage` 复制树时它已经在盘上（0 字节），所以
  `payload_files=118` **含载档自己** —— 与 :274 那条"45 = 44 枚在册 + 载档自己"同尺，不是漏数。
  本步末把这台原样重取了一次（stdout 写往 scratch，不动入库那份），与上面 7 行 `diff` 退出 0。
  本步最后一份载档（下一条索引面）加入同一棵树之后又把这台重取了一次，母数读 **119** = 22 `.py` +
  85 `.txt` + 12 `.err`（`ls` 三个母数各数一次相加相符，差 1 就是它自己），四格里
  `arm_a_findings=0` 与 `payload_secret_face PASS=True` 逐字不动。
- 波 28 step 3a 的**索引面**：`runs/wave28-step3a-staged-scan.txt`（29 行 / `sha16=6eae968bdb9d9248`）。
  人群从工作树换成索引：`git -c core.quotepath=off diff --cached --name-only -z | xargs -0 $PY
  probes/staged_payload_scan.py --paths`，前置控制是 `git diff --name-only` 为空（本机现读 0 个文件，
  于是"扫工作树字节"就是"扫要提交的字节"）。两取都在档：take 1 在索引只有 10 条路径时读
  `payload_scan_files=10 roots=10 unresolved=0 findings=0 rc=0` + `payload_scan=PASS 10 file(s) clean`、
  `take1_staged_exit=0`、stderr 0 字节；把这份载档 add 进索引之后 take 2 读 **11** 条路径同格全 0 +
  `PASS 11 file(s) clean`。为什么两格都要看：这台仪器自己的第一次红（`precommit-staged-scan.wave25.txt`）
  是 `STAGED_exit=1` 配 0 字节 stdout —— `xargs` 只会把文件表接到命令尾部，`--paths` 必须留在前缀里，
  只看退出码分不清用法错与扫过且干净，`unresolved` 那一列就是为了让这类空跑现形。
  载档末段还写了一条**自指滞后**：两取扫的都是这份载档追加自己末段之前的字节，所以第 13–29 行不被
  索引面覆盖；母数 119 在追加之后重数过（文件数不随内容变），覆盖这 17 行的是本步收尾那两取，
  它们的读数写在**提交说明正文**里 —— 那是唯一装得下对自己最后字节的读数的载体。
- 波 28 step 3b（§94 (b) 支的门，纯仪器轮：plane 1 原地不动 275/350，一枚行也没落）新增一台
  `probes/yfinance_shard_entry_price.py`（602 行 / 28468 字节 / `sha16=23efc3a86a087171`；
  `arms=11 arms_passed=11 arms_failed=0 failed=[] identity_set=5168e1e723999312 rows=15`）与载档
  `runs/wave28-step3b-shard-entry-price.txt`（91 行 / 9483 字节 / `sha16=94a587ca70e3ced2`；绿取 3 次
  逐字节相同 + 一枚锻造红取同档并置）。它答的谓词逐字是 §94 末条那句"动这 15 枚之前先造一支反例式仪器，
  证明 yfinance 的 15 枚能被 `kind=shared_http_client` 服务而无需改 runner"，**answer=NO**，而 blocker 钉在
  **替换形状**上、不是假客户端的动词集（`blocker_not=fake_verb_set`）：`F_shape_gap` 的第四读
  `zero_arg_get outcome=answered calls=1 guard=clean` 证明 verb 面够用，而 `G_call_site_shapes` 读到产品侧
  `governed_yfinance_session` 只有 1 枚站点且 `min_positional=1 / with_wrapped=1`，落地面
  `get_shared_http_client` 是 693 个文件里 39 枚站点、`min_positional=0 / with_wrapped=0`，runner 装进去的
  `lambda _c=client: _c` 自己元数是 `(1,0)` ⇒ 把调用方的实参**原样退回**（`positional outcome=passthrough
  calls=0`），连拒绝都到不了，只有 `_seam_guard` 在零调用上抛 `WitnessFailure`。分类器推不动：
  `H_classifier` 用出厂谓词读 `as_measured={'shard_entry_needs_harness': 15}`，把假面从 `['get']` 加宽到
  `['get', 'request']` 仍是 15（`rows_flipped_by_widening=0`，因为 yfinance 的 `package_verbs` 是空集），
  只有把 `fake_ok` 直接锻造掉才动（红取 `forged={'shard_entry_only': 15}`、
  `predicate_disagreements=15`、`arms_failed=1 failed=['H_classifier_cannot_be_moved_by_widening']`、退出 1）。
  改 runner 的价按 slot 读：`J_runner_edit_price receipt_files=1375 pin_slots=19850 runner_pinned_slots=1650
  pin_exit=0 clean_stale=0 forced_stale=1375 forced_targeted_slots=1375 forced_matches_targeted=True`
  —— 1650 是 pin 到 `iter2_claims_probe_runner.py` 的格子，1375 是 B 臂那一格（走器改脏的是
  `ARM_B_TARGET_KIND = "probe"` 且路径以 `iter2_claims_fixtures.py` 结尾的 slot），**两个总体不许合并成一个数**。
  臂 A 自带换代闸（350 / 275 / `5168e1e723999312` 任一移动就 `VOID=yes` 退出 1），臂 K 在内存里锻造一代
  证明这台闸扳得动（`K_gate_is_trippable live_void=False forged_void=True live_accepted=275
  forged_accepted=274 accepted_delta=-1 forged_identity_set=0000000000000000 status_rows_flipped=1`）；
  tmx 按机制单列（`I_tmx_is_a_different_cause … pooled=False`），所以 15 与 24 不收进同一桶。
  这台是**只读**的，而"只读"这条是测出来的不是声称的：为验它又原样重取了一趟绿取（载档外记为 take 6，即本探针
  的第 6 次运行、第三趟绿取；载档内入库的绿取只有 take3 与 take5），跑之前之后
  `git -c core.quotepath=off status --porcelain` 两份逐字节相同（各 226 行、`diff` 退出 0），
  `find . -name '*.pyc' -newer <跑前的标记文件>` 读 **0**（这一趟连字节码都没重写，模块缓存是热的），
  take 6 与入库 take3 的 stdout `diff` 退出 0。它只在内存里 `setattr` 再复原；take 6 的 stdout 只在会话 scratch、
  没有入库载档，所以那一行按**不具复现性**记账，可复现的绿取是载档里的 take3 与 take5。lint 绿面这轮带**正控制**：
  把探针复制成 `_lint_control_tmp.py`
  并注入一枚失效的 `# noqa: BLE001` 与一条 138 字符长行，读 `control_ruff_check_exit=1`（RUF100 + N806 +
  E501，found=3）与 `control_ruff_format_exit=1`（`1 file would be reformatted, 23 files already formatted`），
  删掉之后才读回 `ruff_check_exit=0` / `ruff_format_exit=0`；控制文件 sha256 记在载档里、被点名的源码行不贴，
  免得载档自己成为下一条 finding。入库前两臂面 `runs/wave28-step3b-payload-scan.txt`（38 行 / 2635 字节 /
  `sha16=284b0134f1117006`）：取 1 读 `payload_files=121`（= 23 `.py` + 86 `.txt` + 12 `.err`，三个母数各数
  一次相加核对）、写完本载档后取 2 读 **122** = 23 + 87 + 12，`diff` 实测只有母数一格动，四格
  `arm_a_findings=0 arm_a_rc=0 / arm_b_findings=1 arm_b_rc=1 / PASS=True / scan_exit=0` 逐字相同；
  两枚扫描载档都写完后的第三取读 **123** = 23 + 88 + 12，四格仍逐字相同、`scan_exit=0`、stdout 254 字节。
  索引面 `runs/wave28-step3b-staged-scan.txt`（32 行 / 2847 字节 / `sha16=ae1e1b0eb596cbaf`，同一台
  `staged_payload_scan.py --paths`，本步未改它一个字节）：前置控制 `git_diff_name_only_files=0`（worktree==index，
  所以"扫 worktree 字节"就是"扫将要提交的字节"），取 1 读 `payload_scan_files=5 payload_scan_roots=5
  payload_scan_unresolved=0 payload_scan_findings=0 payload_scan_rc=0`（`PASS 5 file(s) clean`，stdout 144 字节、
  stderr 0），把本载档 stage 后取 2 读 **6**，`diff` 实测只有两格计数器动、三条 0 与进程退出不变；
  `roots == files` 那格保证 CJK 路径没被静默丢掉。覆盖 README 与本条目这些字节的最后一次读数是 stage 之后再取的
  那一次，逐字住在本次提交说明里（两枚载档头部的 `lag_statement` 记的是同一句账）。
- 波 28 step 4（产品轮：`federal_reserve::YieldCurve` 把行类换成域契约类并落地，**275 → 276/350**）：
  这一枚动的是 `INCOMPLETE` 那一格，不是 `UNREVIEWED` —— 换代前后
  `{INCOMPLETE: 7→6, OFFLINE_ACCEPTED: 275→276, UNREVIEWED: 68 不动}`，残差 `75 → 74`，
  桶 `NEED_DECLARE 28 → 27`，机制价 `client_seam 7 → 6`（桶侧 `SEAM_REACHABLE 7 → 6` 同步，两份载档同一格）。
  我起初按上一代的错误印象把常量写成 `{'INCOMPLETE': 7, 'OFFLINE_ACCEPTED': 276, 'UNREVIEWED': 67}`、差写成
  `{0, +1, -1}`，驱动立刻红在 `A_unmodified_green=False` 与 `F_delta_matches_committed_face=False` 并把实测
  `{6, 276, 68}` 打出来 —— **红的是我的假设而不是仪器**。被否证的那个值留在这里，不进任何仪器；这一趟红取的
  stdout 已不在盘上（这台驱动**整体重写**自己的载档，就是本目录末段记过的那条自指滞后），所以它按
  **不可复现**记账，可复现的两面证据是 4b 的 `B_forged_previous_red=True`（出厂那代常量被锻造回上一代时
  确实变红）与 4c 的 `measured_A=276 moved=True`（漂移反例的期望值确实跟着落地面走）。
  * 换代链五份载档：producer `wave28-step4-producer.txt`（72 行 / 17054 字节 / `9a757cea541bd185`）读
    `claims emitted=65 (60 PASS, 0 FAIL, 5 BLOCKED) = 13 row(s) x 5 phases`，五枚 BLOCKED 全是
    `FomcDocuments … <not run> reason=no model witness`（第 16–20 行，待裁 #68），落地那枚的五个动词在
    第 66–70 行逐枚 `YieldCurve <phase> exit=0 status=PASS`。**一条要记的读数缺陷**：同一行末的
    `phases actually executed as subprocesses=65` 数的是 claims 条数而不是子进程数（本载档现数
    `status=PASS` 60 行 + `<not run>` 5 行 = 65），标签比算术多算 5；它只住在这台产品的展示行里、
    没有任何判定读它，本波**没改** `iter2_claims_producer.py` 一个字节（改共享件要把 pin 面重走一遍），
    所以按"读数如实、缺陷另立"记账。recorder `wave28-step4-recorder.txt`（18 行 / `6159f16afa371d96`）
    先印 `re-signing: dropped 11 earlier review(s)` 再 `signed=12 refused=0 reviews_total=12` —— 11 是上一代
    在册那 11 枚，第 12 枚就是这轮落地的；merge 两取 `wave28-step4-merge_take1/2.txt`（各 32 行 /
    `63a5977cd22884e7`、`7f78e58d0268a6a3`）第 31 行 `claims=1410 identities=282 reviews=276
    accepted_identities=276`，两份**除我自己那格派生退出行**（`merge1_exit` / `merge2_exit`）之外逐字节相同，
    共同面 1520 字节 / `0cddcb9640002c50`，`diff` 从第 5 行起退出 0；judge `wave28-step4-judge.txt`
    （5 行 / `3735930db50dbd63`）`350 identities, 276 offline accepted`，其 argv 里那条 CJK 清单路径是从
    producer 模块自己的 `DEFAULT_INVENTORY` 现取的，不是我抄的。
  * 新增一台 `probes/fed_h15_official_shape_price.py`（346 行 / 15453 字节 / `f2044e2788678f65`）与载档
    `runs/wave28-step4a-fed-h15-official-shape-price.txt`（24 行 / 3889 字节 / `7221e0db232bdd42`；
    `arms=A,B,C,D,E,F,G arms_failed=0 failed=[] bodies=5 contract_class=FederalReserveYieldCurveObservation`、
    `h15_probe_exit=0`）。它把"官方 H.15 到底是什么形状"从散文变成七臂读数：A 给五枚 body 各自的
    `padded_fifth / header_cells / data_rows`（3 枚带第 5 格填充 `Unique Identifier: `，其中含 1050989 字节的
    源捕获），B 逐 body 并置**出厂头**与**工作树头**两种读法并要求 `moved` 那格只在该动的 body 上动，C 把
    preamble 标签各改一字符仍必须读 `REFUSED:FEDERAL_RESERVE_BAD_CSV_PREAMBLE`（两枚 `tampered_sha16` 现算），
    D 读 `rows=22 expected=22 rate_types=['float'] nulls=0`，E/F 是对**待裁 #57 的更正**：
    `names_equal=False field_face_equal=True` —— 宏模型与契约类**名字不同、字段面相同**，此前我写成"字段面
    待裁"是过强的；`F_row_model_guard passes=True look_alike_forged_fails=True restored_after=True` 证明
    加了 `row_model` 之后那枚假声明仍然被守卫扳住。G 开出一个新待裁：包体把 **13** 格头发布出来而这个读者只
    声明 12 格，差的那一格是 `RIFSGFSW04_N.B`（`columns_the_reader_does_not_declare=['RIFSGFSW04_N.B']`），
    与 §Fed 汇率裁决"不改 C100 钉住的契约"同源，**本波不擅自补声明**。载档头部另写了一条消歧：
    A/B 里 tag 作 `materialized_fixture` 的那枚 body 是 C100 那份**单行**staged `h15.csv`
    （`…/product-patch-staging-20261013T111344Z/fixtures/h15.csv`，`data_rows=1`、`PARSED:11`），由探针自己的
    `CAPTURES` 表命名，**不是**本波三行分片 fixture（那枚住 `docs/evidence/C101/fixtures/federal_reserve/
    YieldCurve.json`、由五相回执见证）—— 不写这句，`PARSED:11` 会读成与见证的 `rows=33` 矛盾。
  * 落地面追平 **五台**，并逐台证明追平之后仍然能红：`ledger_axis_blast.py` 223 行 / `495b95a057f8c92a`、
    `witness_seam_kind_census.py` 417 行 / `a8d7ef6a7d4ada09`、`landed_face_rebase.py` 282 行 /
    `86844e72941374b5`、`receipt_drift_counterfact.py` 225 行 / `56556b389eb6a5e2`（step 4g 重排后 223 行 /
    `5bfdb04d9ac027d0`；那一代没有重跑它，见本节末 4g 条目）、
    `w28s2_shard_faces.py` 143 行 / `0841716818c8f6de`（最后一台只换散文里那处指向漂移反例的引用，一个计数器
    没动）。驱动载档 `runs/wave28-step4b-landed-face-rebase.txt`（28 行 / 4538 字节 / `27df9dd6b7379d2e`）读
    `subjects=2 arms=12 arms_passed=12 arms_failed=0 PASS=True` / `chain_exit=0`，形状与 step 3a 同：
    每台 subject 在同一进程里跑三遍（出厂常量 → 锻造回 step 3 那一代 → 复原），锻造那一遍两台**各只红一臂**
    `forged_failed_arms=['A_landed_report_face']`、`exits first=0 forged=1 restored=0`，而被报告决定的计数器
    两侧逐字不动（`residue=74/74`、`identity=5168e1e723999312/…`、`accepted=276/276`）；
    `D_measurement_unchanged` 这一臂在 step 4 还多教一条：**探针不打印的计数器要报 `MISSING` 并判红**，
    不许读成"未变"（第一遍就是因为向 dict 型落地面要裸 `accepted=` 而读到 `MISSING`）。
    `yfinance_shard_entry_price.py` 不在追平名单里 —— 臂 A 读的是 `kinds.LANDED_ACCEPTED` 而不是自己的常量，
    所以它随普查换代自动追平；本轮重取一次证明这句不是自称：
    `runs/wave28-step4d-yfinance-entry-price.txt`（17 行 / 2926 字节 / `80126002277ecf1c`；
    `A_void_gate … accepted=276 expected_accepted=276 … VOID=no`、`arms=11 arms_passed=11 arms_failed=0`、
    `K_gate_is_trippable live_accepted=276 forged_accepted=275 accepted_delta=-1`、`entry_price_exit=0`、
    stderr 0 字节）。这台顺带把 §94 那笔报价换了代：`J_runner_edit_price receipt_files=1380
    pin_slots=19918 runner_pinned_slots=1656` —— **1650 → 1656（+6）**、`1375 → 1380 枚回执`，两个总体仍不合并。
  * 漂移反例 `runs/wave28-step4c-receipt-drift-counterfact.txt`（23 行 / 3329 字节 / `5ef996e869ef3bf6`）：
    三臂数值各不相同才算数 —— `A_pre accepted=276 drift_rows=0` → `B_after_rotate accepted=267 drift_rows=9
    nasdaq_statuses=['STALE'] status_counts={…, 'STALE': 9, …}` → `C_after_reland accepted=276 drift_rows=0`，
    `arm_A_expected_276_no_drift=True / arm_B_expected_267_with_9_drift=True /
    arm_C_restores_arm_A_face=True / receipt_drift_face PASS=True`。267 与 9 都由
    `LANDED_ACCEPTED - len(MODELS)` **现算**而不是我打出来的，所以换代时这一臂不会悄悄变成恒真；
    面末那格 `landed_face_rebase previous=275 shipped=276 measured_A=276 moved=True` 把"这台仪器的期望值确实
    跟着落地面走"钉成读数。它重写了 canonical nasdaq 回执（B 臂就是那次事故的可复现形态），C 臂再按
    produce→sign→merge→judge 落回绿代，载档末行写明"本波提交的就是 C 那一代"。
  * 五台只读面同代重取（`wave28-step4-{readiness_census,residue_axis_census,seam_reachability_census,
    receipt_pin_consistency,pytest_planes}.txt`，12 / 93 / 41 / 9 / 19 行，`43bc7d1ae926eca3` /
    `5804de51d5331c03` / `ec05317232fe4286` / `920a58ed4b19915a` / `a04ece988fd8abad`）：
    `residue=74 buckets={'NEED_BINDING': 32, 'NEED_DECLARE': 27, 'NEED_SHARD_ENTRY': 15} sums_to_residue=True
    readiness_arms=3 readiness_passed=3`；`residue_axis_census_face residue=74 … witnessable_today=0
    rows_without_module=66 arms=8 arms_passed=8`（`retarget_fetcher_output` 那 14 枚里现在带
    `federal_reserve: 1`，即剩下那枚 `FomcDocuments`，其 `seam=SEAM_REACHABLE mechanism=client_seam
    witness_module=True fake_ok=True` —— 它缺的只是字段面裁决，不是接缝）；
    `seam_census_residual_by_bucket` 里 `SEAM_REACHABLE` 的残差 `7 → 6`、`accepted_total=276
    accepted_plus_residual=350`；`pin_face slots=19918 stale=0 before_ne_after=0 distinct_pinned_paths=561
    arms=3 arms_passed=3 PASS=True`，B 臂 `stale=1380 targeted_slots=1380` 仍是一笔没付的价；
    `collected=919 (…'919 tests collected in 0.60s') passed=740 skipped=179 sum_equals_collected=True`
    —— 母数由另一趟 `--collect-only` 现取，加法闭合才算这 919 没被静默丢掉。
    两台 step-2 面在这代**逐字复现**（不经写盘，直接调 `reproducibility()` / `coherence()`）：
    `claims_array: produced=0b0c3ff3a5619e74 signed=… equal=True` 与
    `receipt_digest_mismatch_on_disk=0 [] judge_nasdaq_issue_rows=0 review_rows=9 of 9` 一字未动，
    说明 C 臂那次重产没有将 claims 数组搬代。
  * 一条留在档里、**没有判定臂**的观察（写下来是为了下一波不再重猜）：`wave28-step4b-…` 的
    `run_record` 捕获到 stderr **59 字节**（`$AAPL: possibly delisted; no price data found (period=5d)`，
    那台驱动在**进程内**跑普查时打出来的），而我用 shell 重定向单独重取的那五台 `wave28-step4-*.err`
    **全 0 字节**（`find runs -name '*.err' -size +0` 现数全树仍只有波 27 那一份非空）。两种取像的判定面
    相同（`subject_transport_calls=0` 两侧都在），所以这不是"某个 stderr 被吞成了干净的 0"那一类缺陷；
    但**机制未测**，本波不给解释。要结算这一格就同一条命令跑两遍（一遍带 `--providers` 分片、一遍不带），
    把 stderr 各自落盘再看哪一侧出现那一行 —— 若将来某个臂改成读 stderr，这一格必须先结算，
    否则会出现"同一台仪器两种宿主两种字节数"的不可复现面。
- 波 28 step 4e/4f/4g（step 4 的收尾三取，**plane 1 一格没动，仍 `276/350`，identity set `5168e1e723999312` 未变**）：
  * 4e 第二道 tracked 围栏换代复核。仪器住在 `docs/evidence/C103/batch5-source-pins/probes/pin_rebase.py`
    （178 行 / 6967 字节 / `76ef703eb4d0d54d`，本步没动它一个字节）。我起初按
    `.../probes/batch5_fence_dry_census.py` 去跑，退出 2、stderr 228 字节"文件不存在"，真实路径是从
    `runs/wave28-step2-batch5_fence_dry_census.txt` 自己的 `argv:` 行读回来的而不是猜的。
    `runs/wave28-step4e-batch5_fence_dry_census.txt`（11 行 / 931 字节 / `e45a3c14c0d48bff`，stderr 0 字节，
    `batch5_fence_dry_census_exit=1`）读 `pin_entries=31 matched=31 stale=0 missing=0` + `verdict: REFUSED`，
    那六个 `face:` 字段与 step 2 那份**逐字同代**（含
    `snapshot_payload_sha_before=bcdca191aa39064a8fc7040651c6694a572f5137d9c1d0868916b39ac284924d`），
    所以 step 4 动的 4 个产品/测试文件没有一个在这道围栏里；`exit=1` 仍是"无过期 pin 时拒绝重写正在动的 digest"
    那个设计门，不是判定红。为了把这句变成集合陈述而不是抽查，另落
    `runs/wave28-step4e-batch5_fence_pin_keys.txt`（42 行 / 2969 字节 / `fb968dce1a9e32b2`，
    `fence_pin_keys_exit=0`）：31 个 key 逐条列出，`federal_reserve_keys=0`、`yield_curve_in_pins=False`、
    `provider_keys_under_opendata_data=['finviz', 'fred', 'yfinance']`，它自己数出的
    `snapshot_sha16=bcdca191aa39064a` 与上面那台印出的整枚 sha256 前 16 位相同（两台独立仪器对同一份快照取像一致）。
    **这道围栏与回执 pin 闸门是两个总体**：4f 撞上的正是前者看不见的后者。
  * 4f 一行错误引用勾出整条重落。当时那句"`382-403` 这个字面 grep 现数 7 处"**有两个毛病，本步把它撤下**：
    一是它自己那串枚举相加就不是 7 —— 散文 5 处（`_parts/federal_reserve/rows_a.py` 两行、
    `iter2_witnesses/federal_reserve.py` 一行、本目录 `probes/declare_candidates.py` 与
    `probes/readiness_census.py` 各一行）加 README 1、开发验收记录 1、载档 1，共 **8**；
    二是那条 grep 的 argv 没进载档，今天无法复算，所以 **7 与 8 都按不可复现处理**。
    当代换成能重跑的集合陈述，配方见 §97，读数 `literal_382_403_hits=9 distinct_files=4`，按文件角色拆开：
    产品/仪器散文 **0**（那 5 处本步已全改 `392-413`）、本 README **5**（`:94` 那句报告、`:460` 历史句、`:462`
    它的更正，这三处在本次编辑之前就按这三个号命中，另两处在**本条自己**里面——它们随本条行长漂移，所以只报角色不报号，
    这正是"读法"那句说的事）、开发验收记录 **2**（历史句与本步更正句）、
    历史载档 **2**（`runs/wave28-step1-retarget_rowclass_counterfactual.txt:60`，以及本步才第一次看见的
    `docs/evidence/C100/C120-gate-chain/member8_a2_check.txt:4414`——那枚 gate 原始输出逐字抄下了 `rows_a.py`
    改前的 docstring，**不可改也不该改**）。**读法**：引用一处错误行号时把原句逐字抄进报告，报告自己就变成下一个
    命中；这类普查要么按文件角色拆，要么只报 `file:line` 不抄原文。真实位置由行首缩进推出的读者量出：
    `guard_def_line=392 guard_body_last_line=413 next_item_line=416`，assert 在 `:406`。**写 `382-403`/`:399`
    那一代这两个数是对的**（此后该测试文件在它上面长过行），所以只有声称"现在在哪"的 docstring 改成 `392-413`，
    历史面留旧值并把更正并排写。
  * 代价是实测的而不是估的：`rows_a.py` 与 `federal_reserve.py` 都在 claim 的 `probe` 钉集合里
    （`iter2_claims_producer.py:203-207` 把 `witnesses.witness_module_rels(provider)` 摊进 `probe_files`），
    所以一个纯 docstring 改动就必须重产该 provider 的五相，否则 pin 闸门读 stale。重落链五份载档
    `wave28-step4f-{producer,recorder,merge_take1,merge_take2,judge}.txt`
    （72 / 20 / 33 / 33 / 5 行，`e26e9126bdf9c72b` / `7d8d5442a93cf012` / `33f64747b10eebc1` /
    `02514745bb251135` / `5d7960a4d0cf1acc`，stderr 全 0 字节，`producer_exit=0`、`recorder_exit=0`、
    `merge1_exit=0`、`merge2_exit=0`、`judge_exit=0`）：producer `claims emitted=65 (60 PASS, 0 FAIL, 5 BLOCKED)
    = 13 row(s) x 5 phases`（5 枚 BLOCKED 是 `FomcDocuments` 那条没跑的相，逐字 `<not run>`，本机现数
    `status=PASS` 60 行、`<not run>` 5 行），recorder `signed=12 refused=0`（`FomcDocuments` 仍不签，归 #68），
    merge 两取**逐字节相同**（`merge_takes_identical=True bytes=1520/1520 sha16=0cddcb9640002c50/0cddcb9640002c50
    lines=28/28 first_diff_line=None`），judge `350 identities, 276 offline accepted`。同代再取六台只读面
    `wave28-step4f-{receipt_pin_consistency,readiness_census,residue_axis_census,seam_reachability_census,declare_candidates,pytest_planes}.txt`
    （9 / 12 / 93 / 41 / 35 / 19 行，`1aad4f401d9f0c14` / `1dbcda6f71328d25` / `ba65eb895c5c8a2e` /
    `4976bdd450cca3b0` / `a539857287c7106c` / `acf313186a61c89a`，stderr 全 0 字节）：
    `pin_face slots=19918 stale=0 before_ne_after=0 distinct_pinned_paths=561 arms=3 arms_passed=3 PASS=True`、
    `residue=74 buckets={'NEED_BINDING': 32, 'NEED_DECLARE': 27, 'NEED_SHARD_ENTRY': 15} sums_to_residue=True
    readiness_arms=3 readiness_passed=3`、`residue_axis_census_face residue=74 … arms=8 arms_passed=8`、
    `residual_total=74 accepted_total=276 accepted_plus_residual=350`、`declare_arms=2 declare_passed=2`、
    `740 passed, 179 skipped`（`pytest_face passed=740 skipped=179 sum=919 sum_equals_collected=True`）。
    **一个计数器都没动** —— 这一代的产物是"字节换代而面不变"本身，也顺带第二次 reproducer 了 step 4 记过的那个
    产出器分母错标（`phases actually executed as subprocesses=65` 数的还是 claims 条数，实测子进程 60）。
  * 我自己在这一步红过一次，且红得有价值：12 个 `--identity` 拼进一个 shell 变量再**不加引号**展开，
    zsh 不做词拆分 ⇒ argparse 收到一整块、`exit=2`、stderr 1440 字节；那一份 stdout/stderr 被成功取代就地覆盖，
    上面这句加 `wave28-step4f-recorder.txt` 末段是它唯一的记录。后果是同批的 merge+judge 读到没重签的评审钉，
    plane 1 印出 **264 = 276 − 12**；按 produce→sign→merge→judge 生成序重跑才恢复 276。读法：这不是产品缺陷，
    是我在本波复刻了 §91 已经记过的同一件事（变量展开不拆分），而那批 264 的载档我没有留下 —— 下次"错 argv 把面
    打移动"的取证要先把失败载档改名保留再重跑，不要就地覆盖。
  * step 2 那两台只读面在这代仍然对得上，但**取像方式换了**：不调 `main()`（它会把
    `runs/wave28-step2-claims_reproducibility.txt` 与 `runs/wave28-step2-shard_coherence.txt` 这两份已入库载档
    整体重写，`w28s2_shard_faces.py:132-135`），而是把 `reproducibility()` / `coherence()` 两个纯函数在进程内各调
    一次（`main_not_called=True`，两份载档字节一个没动）。`coherence()` 与入库那份**逐字节相同**
    （355 / 355，两侧 sha16 `eb9b21fef21d79ab`，`receipt_digest_mismatch_on_disk=0`、`review_rows=9 of 9`）；
    `reproducibility()` 与入库那份**不同**：前 17 行逐字同（含第 7 行
    `claims_array: produced=0b0c3ff3a5619e74 signed=0b0c3ff3a5619e74 equal=True`），从第 18 行起分叉，
    20 → 21 行、1412 → 1426 字节（+14）。差的那一行是 step 4 把这台引用的漂移载档从
    `wave28-step2-receipt_drift_counterfact.txt` 改指到 `wave28-step4c-…` 之后的**散文换代**，不是计数移动。
    这一格把上面 step 2 条目里"只动我手工重排的散文换行"那半句在这代**否证**了（已就地标注）；
    "每个计数行不变"这半句是测出来的，可以照引。
  * 4g 给 step 4 自己改进的那条 judge 分支补反例。判定改动要自带反例，而 step 4 往
    `tests/test_federal_reserve_iter2_models.py` 的 handoff 判定里加的是 stale 名回退 `:374-380`，
    它一直欠一枚 counterfact。仪器 `probes/handoff_fallback_counterfact.py`（283 行 / 12817 字节 /
    `63a1bcdc9552bd2f`）在同一进程里 `spec_from_file_location` 载入那支测试模块、把模块全局的
    `contract_model` 换掉再换回来，**盘上文件一个都不编辑**。载档
    `runs/wave28-step4g-handoff-fallback-counterfact.txt`（36 行 / 4674 字节 / `cbf3c0a4b5b3083c`，
    stderr 0 字节，`handoff_fallback_counterfact_exit=0`）读 `arms=12 arms_passed=12 arms_failed=0 verdict=GREEN`。
    总体先摆出来：`handoff_domains=7 stale_named_domains=1 ['federal_reserve_yield_curve']`，
    该格 `spec_contract=FederalReserveYieldCurve` 对 `shipped=opendata.data.models.federal_reserve_clean_documents.FederalReserveYieldCurveObservation`。
    六臂读法：A/D 未改与复原都绿；B 锻造回同名不同模组的 `federal_reserve_macro.FederalReserveYieldCurve`
    （`forged_registered=False`）⇒ 红在 `:379` 那行 `assert shipped.__name__ in contracts_by_name`，
    载档同时印出 `raise_text=` 逐字与 `calls=[('federal_reserve_yield_curve', 'FederalReserveYieldCurve')]`，
    后者证明补丁只碰到那一格 domain（`B_patch_touched_only_target=True`）；C 锻造回**在册但错**的
    `FederalReserveCentralBankHolding`（`forged_registered=True`，名字那一关放行）⇒ 只能红在 `:388`
    `assert set(semantic_fields) <= contract_fields`，`semantic_missing_from_forged={'FederalReserveCentralBankHolding': ['maturity']}`，
    跑之前先量 `C_can_differ=True`（在册 7 类里 6 个候选的字段面确实不含该 domain 的语义字段）——
    跑不红的臂不算证据；E 把回退删掉（直接拿 handoff 的名查表）⇒ `KeyError` 落在同一格、其余 6 格绿，
    `E_fallback_is_load_bearing=True`，这条才把"分支承重、不是装饰"说成测量；F 复原后在同模块里再跑一遍
    姊妹 guard `test_declared_row_models_are_their_domain_contracts`（绿，`contract_model_identity_restored=True`），
    防"补丁泄漏冒充绿判定"。两个 raise 位由 `ast` 从被测文件自己读出来（`judge_assert_sites=4` 连表达式全表印出），
    载档不硬写行号，`raise_line=379 expect=379` 是两边对照的结果。
  * 4g 里我自己红过两处，都留在记录里。第一处是臂的极性：`flags["B_absent_name_reddens"]` 我直接接了
    `_run()` 的第一个返回值，而那个位置是"green"，语义反了 ⇒ 首跑 `arms=12 arms_passed=10 arms_failed=2
    verdict=RED`，红的两格是我的臂，判定件那两行照样在 `:379`/`:388` 红在预期位。改极性后重取才是上面这份，
    首跑那份 stdout 落在 /tmp、没有改名留档。第二处是 lint 总体：`ruff check` 在这台新仪器上数出 22 条
    （15 N806 / 2 C416 / 1 D103 / 2 ANN401 / 1 TC003 / 1 RUF100），全部由本步引入、逐条清掉后整棵探针树
    `All checks passed!` + `25 files already formatted`。其中一处值得记：**C416 与 C402 在同一个表达式上互相指**——
    `{marker: lineno for marker, lineno in (…)}` 被 C416 要求改写成 `dict(…)`，改完 `dict(…)` 又被 C402
    要求改回 dict 推导；收口写法是直接对 `MARKERS` 写 dict 推导，两条规则同时闭嘴。
    顺带查出 `receipt_drift_counterfact.py` 自 step 4c 之后没再 format（`--check` 会 red），本步把它重排
    −2 行 / −14 字节，现 223 行 / 8822 字节 / `5bfdb04d9ac027d0`。本目录那两处旧钉已就地更正，一处在本文件
    "波 28 step 2 新增四台仪器"条目里它自己的那行、一处在 step 4"落地面追平五台"清单里；**这两处只报角色不报行号**，
    因为本条每长一行它们就挪一格 —— 上一版这里写的是两个行号，写完本条就已失效，那正是 §96 末段说的那件事。
    **没有重跑它**：它就是 step 4c 那条写盘链，重跑等于在 live canonical 回执上再旋转一遍 B 臂，
    所以"重排不改语义"这一句在这里是按 formatter 的合同**断言**的、不是测出来的；能测的两格是
    `py_compile` 过、以及没有任何载档钉过这台仪器的字节（`runs/wave28-step4c-…` 只记 argv）。
    重取条件：下一次真要动这台仪器时，先按 step 4c 的生成序 produce→sign→merge→judge 备好了再跑。
  * 4g 的本代母数面另落一份 `runs/wave28-step4g-payload-face.txt`（72 行 / 4601 字节 / `66afab111e010fa1`）：
    `payload_secret_scan.py`（131 行 / 5451 字节 / `6277dcf7f61fc9df`，本步没动它一个字节）取 1 读
    `payload_files=181` = `probes/*.py` 25 + `runs/*.txt` 118 + `runs/*.err` 37 + **1** 枚 `runs/.step4f.rcs`
    （4f 那次就地覆盖留下的唯一实物，所以这里单列一个奇数成员而不是折进 37），本载档存在之后取 2 读 **182**
    （`runs/*.txt` 那一格正是 119，多的就是这份载档自己），两取 stdout 254/254 字节、`diff` 只动分母那一行，
    判定四格与 `scan_exit` 逐字相同；`.err` 37 枚不入库那句用第二条路数核过
    （`git ls-files | grep '^docs/evidence/C128/' | grep -c '\.err$'` → 0）。"只读"照旧是测的：跑前跑后
    `git -c core.quotepath=off status --porcelain` 两份 426 行逐字节相同、`diff` 退出 0，stderr 0 字节。
    **这份载档自己的 argv 起初写错了一格**（头部写 `/tmp/w28s4g.take1.out`，那一趟真正落在 `/tmp/take1.out`，
    前者没有任何工具结果指向），已按真实文件名更正并把这句话留在载档头部里 —— 规矩收严：argv 从工具结果里抄，不补写。
  * 4g 的索引面 `runs/wave28-step4g-staged-scan.txt`（定稿 60 行 / 4862 字节 / `5563a120a439422e`）：
    走器是 `staged_payload_scan.py`（127 行 / 4890 字节 / `f5402c4e39098d93`，未改）。取 1 对 index 里那 187 枚
    （= 142 枚 tracked 修改 + 45 枚精选未跟踪，`awk` 分桶现读 `c101=12 c128_probes=2 c128_runs=31`）读
    `payload_scan_files=187 roots=187 unresolved=0 findings=0 rc=0`；把这份载档 stage 之后取 2 读 **188**，
    `diff` 退出 1、报 `1,2c1,2`，动的只有两格计数器，三条 0 与 `rc` 逐字相同，stdout 两取都 150 字节、stderr 都 0。
    取 1 的 31 枚载档**不含本文件**，所以 187→188 的唯一理由就是这份载档自己 —— 若 `findings` 跟着母数一起动，
    那说明 stage 进来的是 finding 而不是文档，这一格是这台仪器存在的目的。plane-1 面同代取自 **index blob**
    （`git show :docs/evidence/C101/judge/claims.merged.report.json`）：`276/350`、`claim_count=1410`、
    `identity_set=5168e1e723999312`、`integrity=PINNED_EXACT_SET`、`global_issues=0 invalid_claims=0`，
    同一份 blob 里 `federal_reserve::YieldCurve` 是 `OFFLINE_ACCEPTED issues=[]`、`FomcDocuments` 仍 `INCOMPLETE`。
    覆盖最终字节的取 3 按 lag 约定住在提交说明里，本文件不写它自己的读数；配方与完整读数见 §97 的索引面条目。
- 残差重定价面（step 2 同代）：`readiness_census`（12 行 / `20d35f19e40c64f9`）
  `residue=75 buckets={'NEED_BINDING': 32, 'NEED_DECLARE': 28, 'NEED_SHARD_ENTRY': 15} sums_to_residue=True
  readiness_arms=3 readiness_passed=3 identity_set=5168e1e723999312 expected_match=True`；
  `residue_axis_census`（94 行 / `dfe317f9729168ce`）`witnessable_today=0 rows_without_module=66
  arms=8 arms_passed=8`（step 0 那份是 `rows_without_module=75`，本波 9 枚有了模块 ⇒ 75−9=66，两代同尺）；
  臂 F 本代现读 `count=0 rows=[]` 而两面控制仍在（`control_actions_on_harness=['shard_entry_only']` /
  `control_actions_off_harness=['shard_entry_needs_harness']`），所以 §92 那扇门落地之后仍然关着。
  `seam_reachability_census`（41 行 / `fde2d5b502095459`）给出机制价格
  `residual_by_mechanism={'client_seam': 7, 'client_seam_other_module': 5, 'engine_transport': 37,
  'new_provider_package': 2, 'runner_surface_widening': 24} residual_total=75 accepted_total=275
  accepted_plus_residual=350`，并印出本目录那台分类器的第二枚缺陷：落地后的 nasdaq 仍读
  `bucket=SEAM_NEVER_USED mechanism=engine_transport` 而 `witness_module=True client_seam=True rows=9
  accepted=9 residual=0` —— 与 §93 已记的 `_row_class_face`（只看 fetcher 自己 MRO，服务路径造普通 dict 就
  `emitted=[]`）同族：两台都拿"现在没在用"当"不可能用"。**引用这一格时必须带着这句**，否则会把桶读成可达性。
- 质量面与仪器卫生：`ruff_check` / `ruff_e501` / `mypy` / `bandit` 四档全 0（范围 = 见证模块 + 9 枚模型），
  `pytest_nasdaq_and_pin_planes`（`748 passed, 179 skipped in 2.80s`，`pytest_nasdaq_and_pin_planes_exit=0`）。
  `runs/wave28-step2-probes-lint.txt` 是本目录自己的卫生面：`probe_py_files=21`、`ruff_check_probes_exit=0`、
  `ruff_format_probes_exit=0`、`21 files already formatted`（母数由 glob 与 ruff 各数一次相符）。**范围如实标**：
  Makefile 的 `PY_SELFDEV = opendata scripts tests alembic alembic_data` 不含 `docs/evidence`，所以探针树不在
  17 员 gate 的任何一档里，这道面一直是自课的、不是 gate 读数。格式化之后必须证行为不变：重跑只读面探针
  `w28s2_shard_faces.py`（exit 0），`shard_coherence` 载档逐字节相同、`claims_reproducibility` 只动我手工重排的
  散文换行 5 行而每个计数行不变，另加四支 `py_compile=0`。**波 28 step 4f 更正这半句**：这台一跑 `main()` 就把
  那两份已入库的 step 2 载档整体重写（`w28s2_shard_faces.py:132-135`），所以当代复核改在进程内调
  `reproducibility()` / `coherence()` 两个纯函数；`coherence()` 仍逐字节相同，而 `reproducibility()` 的差
  已不是换行 —— 前 17 行逐字同、从第 18 行起分叉（20 → 21 行、+14 字节），那一行是 step 4 把引用改指到
  `wave28-step4c-…` 之后的散文换代。详见本节末 step 4e/4f/4g 条目。
- `runs/wave28-step2-payload-scan.txt`：入库前两臂面。母数换代可追：step 0 收在 80（16 `.py` + 52 `.txt` +
  12 `.err`）→ step 1 take 1 读 85 → take 2 读 86 → 两份写完 87 → **step 2 take 1 读 111（= 探针 21 + 载档 90，
  两个母数各数一次再相加核对）、take 2 读 112**（本载档自己加入），`diff` 实测只有 `payload_files` 一格动、
  判定四格逐字相同（`arm_a_findings=0 arm_a_rc=0 / arm_b_findings=1 arm_b_rc=1 / PASS=True / scan_exit=0`），
  臂 B 仍往 `runs/declare_candidates.txt` 的一次性副本种运行时生成的 token（`B_PLANTED_FOUND
  rule=generic-api-key at=declare_candidates.txt:31`），仓库字节不变。写完本 README 条目与 §94、stage 之后取的
  索引面在 `runs/wave28-step2-precommit-staged-scan.txt`，最后那一次复核的逐字读数住在提交说明里。
- `runs/wave28-step2-precommit-staged-scan.txt`（51 行 / `59c52a3976b37ac4`，取 1/取 2 时代；追加取 3 块后 63 行 / `9e41e3fe0891d4cb`；本条目与 §94 的同款条目又改过字节，载档末尾追了取 4 块（含取 3 块里一句被后继编辑否证后的撤回），终态 76 行 / `aeee6bd7535ca34f`；探针
  `staged_payload_scan.py` 127 行 / `f5402c4e39098d93`，本步没改它一个字节）：**索引面**。母数 = 本步要
  提交的全部路径，`git diff --cached --name-status` 实测 159 = 145 新增 + 14 修改（修改 14 = 9 枚 nasdaq
  模型 + `claims.merged.json` + judge 的 `.json`/`.md` + 本 README + 开发验收记录；新增 145 = 1 见证
  模块 + 4 探针 + 21 载档 + 119 nasdaq 证据）。取 1 读 `files=159 roots=159 unresolved=0 findings=0
  rc=0`，本载档加入索引后取 2 读 **160**，字段逐一对下来只有两格计数器动（`findings 0 -> 0`、
  `rc 0 -> 0`、`unresolved 0 -> 0`）。这一面本步**自己补了一条双面控制**（step 0/1 的索引面没有，缺的
  正是"这台能抓到东西"的证明）：运行时生成的一枚 token 写进
  `docs/evidence/C100/w28s2-scan-control/`（先用 `git check-ignore -v` 证其被 `.gitignore:249` 的
  `/docs/evidence/C100/**` 忽略），单扫读 `findings=1 rc=1`，**追加到真实的 159 之后**再扫读
  `files=160 findings=1 rc=1`：158 条仍干净、那一条被抓到，所以 `findings=0` 不是复制器溢出也不是
  哑计数器；控制文件随后删除，token 字面量不入库也不在载档里复述。取 1 的 argv 是在 shell 命令替换里
  拼的（逐项加引号），控制臂与取 2 用 `subprocess` argv 列表；这处差异如实写在载档头部，没把取 1 美化成
  argv —— 判定靠的是这台会把未解析路径打印出来并强制 FAIL，所以"开发验收记录的 CJK 路径确在扫描集内"
  是被测的。**两个母数互不替换**：`payload_files`（上面那台的 probes+runs 树，step 2 那代记 113 = 21 `.py` +
  80 `.txt` + 12 `.err`；波 28 step 3a 记 **119** = 22 + 85 + 12；step 3b 三取代依次为 **121 → 122 → 123**
  （23 `.py` + 86/87/88 `.txt` + 12 `.err`，动的那一格永远是载档自己），12 枚 `.err`
  是 wave-27 留下的捕获、按约定不入库但仍在这两棵树里，所以如实计入）与 `payload_scan_files`（本台的"本步要提交的路径"，含两份 `docs/` 叙事与
  119 枚 C101 证据、不含 12 `.err`）是两个不同总体，各自的控制臂只证明自己那个总体。取 2 之后 README 与 §94 各补过本条目，载档末尾又追加了取 3、取 4
  两块，取 3 块里“最后一次由取 3 说”那句已被同一步内的后继编辑否证、并在取 4 块里撤回；这些追加
  与本条自身都是它之前各取没扫到的字节，终取的逐字读数住在本次提交说明里。
- `runs/wave28-step0-receipt_pin_consistency.txt`（9 行 / `965eb3f9b9324955`，`receipt_pin_consistency_exit=0`）
  与 `runs/wave28-step0-batch5_fence_dry_census.txt`（15 行 / `600128e34e133c44`，`pin_rebase_exit=1`）：
  改臂之后的**两个 pin 平面**同代重取。前者 `pin_face slots=19382 stale=0 before_ne_after=0
  distinct_pinned_paths=536 arms=3 arms_passed=3`；后者 `pin_entries=31 matched=31 stale=0 missing=0` +
  `verdict: REFUSED - no stale pin to re-base` —— 那枚 1 是设计中的判定而不是红：无过期 pin 时这台拒绝重写
  正在动的 digest，载档头部把这句话写死了，因为只看退出码分不出"用法错"与"刻意拒绝"。
  两台合起来才是 §92 那句"改 C128 里的探针不需要重产回执、也不需要 `resign_all_pins.sh`"的证据；
  第二台住在 `docs/evidence/C103/batch5-source-pins/probes/`，载档按波落在本目录，argv 逐字写在其头部。
- `runs/wave28-step0-payload-scan.txt`（34 行 / `51c9446d46b0ab1c`）与
  `runs/wave28-step0-precommit-staged-scan.txt`（19 行 / `5c86f417d8835fa7`）：本步入库前的两臂面与索引面，
  形状与波 27 那两条一样，母数换代可追：取 1 读 `payload_files=78`，取 2 读 `79`，两份扫描载档都写完之后的
  现数是 `80`（= 16 `.py` + 52 `.txt` + 12 `.err`）。两臂的四格
  `arm_a_findings=0 arm_b_findings=1 payload_secret_face PASS=True scan_exit=0` 两次逐字相同，唯一差格是
  母数自己（`take2_vs_take1_face_fields_identical=False differing=['payload_files']` 是**如实印出**，不是
  失败：这台把新增的载档自己算进总体）。索引面取 1 扫 6 条非扫描路径、取 2 扫 8 条，
  `unresolved=0 findings=0 rc=0` 两代相同。两份载档末尾都把**自己取不到的字节**写全了：不只是最后一块，
  还包括 take 2 之后 README 与本记录各自又补了载体条目 —— 最后那一次复核的逐字读数住在提交说明里。
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
  这一代 step 2 十三次运行里有十二次把 stderr 重定向到同名 `.err`（那一代本机现数 12 份，只有刚提到那一份非空；
  波 28 step 4 之后全树 `ls runs/*.err` 现数 **24** 份，波 28 step 4e/4f 之后 **36** 份（= 24 + 围栏那 1 份
  + step 4f 的 11 份），step 4g 之后 **37** 份（+ 那台 counterfact 自己的 1 份，0 字节），
  `find -name '*.err' -size +0` 现数仍只有那一份非空，即
  `wave27-step2a-witness_seam_kind_census.err`），
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

本目录 24 台仪器（`ls probes/*.py` 现数；step 2 之前 17 台，step 2 那轮 +4，波 28 step 3a 再 +1，step 3b 再 +1，
step 4 再 +1）里
**21 台只读**：除自己的臂失败外退出 0，绝不改动任何被判定的面。**三台会写，写的东西按代次记账**：`w28s2_run_chain.py` 起已入库的
producer→recorder→merge→judge，一次跑就换代（本波 45 枚回执重产），所以它排在链首、其余载档全在同代重取；
`receipt_drift_counterfact.py` 的 B 臂**真的重写 canonical 回执**（那正是它要证明的事），随后由 C 臂把树落回绿代；
`w28s2_lint_pass.py` 改写的是探针自己的文本（docstring / noqa / tempfile），不是任何被判定面。波 23 起
（readiness、declare、payload 两臂扫描、seam 表面），波 24 收编 staged 扫描那一列，波 25 加两台正交轴
（可达性普查、后响应守卫）与一台入库前 pin 闸门，波 26 加四轴逐枚测量器，波 27 step 0 加台账轴拆分、射程
与那条运输规则的差分三台加 seam 种类普查，step 2 再加三支（乙支 7 枚定价、甲支加宽反例、整残差成像），
波 28 step 1 再加一台 `retarget_rowclass_counterfactual.py`（把已落地的守卫与分类器在内存里逐枚开闸，给
`retarget 24` 那一格定价；它经 `census._run` 起已落地的普查取接缝总体，见该文件 :152），step 2 再加四台
（上面那三台，加只读的 `w28s2_shard_faces.py`）。
新仪器同样只打印屏幕与臂，不判定任何行；不写盘的那几处例外都不落盘：pin 闸门的 C 臂在**内存里**造一枚假 slot，
step 2b 的甲支在**内存里**加宽假客户端，step 1 的 I 臂在**内存 deepcopy 里**把一枚行的 `is_contract` 取反，
step 3a 的驱动在**内存里**把两台 subject 的落地面常量锻造回上一代再复原（`setattr` 在已导入模块对象上，
subject 源码字节一个不写）。它与 step 2 那三台同属"自己写载档"那一类，写的只有
`runs/wave28-step3a-landed-face-rebase.txt` 一份（波 28 step 4 换代后 `CARRIER` 常量指向
`runs/wave28-step4b-landed-face-rebase.txt`，step 3a 那份就此冻结成历史，不再被重写）；它写载档的方式是**整体重写**，所以追加在末尾的历史块会被
下一趟吞掉 —— take 3 就吞掉过一次 take 2 追加的那一块，这条滞后连同被吞掉的读数逐字记在载档末段。波 28
step 3b 再加一台 `yfinance_shard_entry_price.py`（只读、11 臂，载档由 shell 重定向写、探针自己不写盘）：
它把 §94 (b) 支的谓词从散文变成读数，手法与上一条同族 —— **在内存里**进 runner 的 `_client_seam` 再复原，
`setattr` 只落在已导入的模块对象上；它跑一趟之后 `git status --porcelain` 两份相同、`-newer` 数出的 `.pyc` 为 0，
所以"只读"这一格在本波是测量而不是自称。波 28 step 4 再加一台 `fed_h15_official_shape_price.py`（只读、七臂，
载档同样由 shell 重定向写）：它唯一落笔的地方是把 HEAD 版的读者源码复制进 `tempfile` 再
`importlib` 加载（为的是在同一进程里并置"出厂头"与"工作树头"两种读法），`finally` 里 `staged.unlink()`
并把 `sys.modules` 那一格弹出（见该文件 :149），所以仓库字节与被判定面一个不动；它不写自己的载档，
因此 step 4 那批 `.err`（本波 12 枚，全树现数 24 枚）里它的 stderr 与其余一样按约定不入库。
写盘仪器的名单在这代没有变宽：`receipt_drift_counterfact.py` 仍是唯一真的重写 canonical 回执的那台，
step 4 只是把它的载档常量改名为 `wave28-step4c-receipt-drift-counterfact.txt`，好让已提交的 step 2 那份
不被这台的整体重写吞掉。
