# C126 · 波 21：sec::InsiderTrading 补齐五相，plane 1 257 → 258/350（2026-10-17）

叙述与判定面在 `docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/开发验收记录.md` §83。本文件只放
读数、载体路径与复算配方。

上一波把这一行留在 `SHARD_ONLY_ROWS` 里并且标了 `preflight=True`：它是 24 条腿的载体，不是六条腿。
本波落下的就是那一枚，另加一条提供方侧的爆炸半径实测 —— **加一片分片只老化它自己那一家**。

## 1. 写集（`sha256` 前 16）

| 载体 | 角色 | 面 |
| --- | --- | --- |
| `scripts/quality/iter2_witnesses/_parts/sec/insider_rows.py` | sec 的第一片分片（406 行 / 18248 字节） | `50128e356054ec52`（新增，未跟踪→入库） |
| `docs/evidence/C101/fixtures/sec/InsiderTrading.json` | 24 腿载体（1118 行 / 78467 字节） | `b134995763ee73e2`（新增） |
| `probes/carrier_falsification.py` | 载体反例仪器，六臂（230 行 / 10622 字节） | `45766f9690a21695` |
| `probes/acceptance_delta_and_phase_floor.py` | plane 1 增量与「五相 + review 缺一不可」仪器，三臂（273 行 / 11788 字节） | `5dc1533c463b16c6` |
| `runs/phase_produce_sec.txt` | sec 全量 120 相逐相日志 | 见 §2 |
| `runs/record_sec.txt` | 24 枚 review 逐名签名日志 | 见 §2 |
| `runs/merge_and_judge.txt` | merge + judge 两步面 | 见 §2 |
| `runs/carrier_falsification.txt` | 六臂判定面 | 见 §3 |
| `runs/plane1_delta_and_floor.txt` | 三臂判定面 | 见 §4 |
| `runs/precheck_five_phase_scratch.txt` | 一次性试跑的五行面（含那条与判定无关的 exit=1） | 见 §5.4 |
| `runs/next_wave_predicate.txt` | §C125 谓词在本波之后的重读 | 见 §6 |
| `probes/next_wave_bucket_simulation.py` | §9 桶面预言的仪器（分片 218 行 / 9669 字节） | `4c0a7f6064de7b6a` |
| `runs/next_wave_bucket_precedence.txt` | 三臂判定面 + 臂 C 的第一版红面 | 见 §9 |
| `runs/lint_readings.txt` | 三枚 `.py` 的 ruff check/format 读数（改前 + 改后） | 见 §5.5 |
| `runs/secret_scan_payload.txt` | 提交前凭证两臂 + 两侧对照 | 见 §8 |

`docs/evidence/C101/` 下本波动过的文件按角色拆开是 **143 枚 tracked 重写 + 12 枚新增**：

- 重写（143 = 115+23+2+1+2）：`receipts/sec/**` 115（23 枚已验收行的 5 相 × 23，收据里带时间戳与再算摘要）、
  `primary-reviews/sec__*.review.json` 23、`sec/claims.json` + `sec/claims.reviewed.json` 2、
  `claims.merged.json` 1、`judge/claims.merged.report.{json,md}` 2；
- 新增：`captures/sec/InsiderTrading/**` 5、`receipts/sec/InsiderTrading/**` 5、
  `fixtures/sec/InsiderTrading.json` 1、`primary-reviews/sec__InsiderTrading.review.json` 1。

**没有任何非 sec 的 provider 路径出现在 `git diff --name-only HEAD` 里** —— 143 枚逐名都在 sec 或
merge/judge 的顶层文件上。sec 的 115 枚 tracked 捕获也一枚没改（它们的内容是逐相 stdout，重跑同为
PASS 就同字节；改过的话 porcelain 必列）。这就是 §5.1 那条爆炸半径读数的入库面。

## 2. 四步链读数

- producer（sec 域，`--output docs/evidence/C101/sec/claims.json`）：
  `claims emitted=120 (120 PASS, 0 FAIL, 0 BLOCKED) = 24 row(s) x 5 phases`，
  `phases actually executed as subprocesses=120`，`PRODUCE_EXIT=0`；
  `denominators: inventory total=350 (pinned identity set 5168e1e7…)`,
  `catalog-resolvable bound identities this wave=318/350`,
  `provider rows covered=24 of provider inventory rows=24, of which bound now=24/318 resolvable`。
  逐相单模型实测 5.60 s user / 26.3 s wall（`runs/precheck_five_phase_scratch.txt` 那一次），
  全域 120 相从第一条 claim 的 `executed_at_utc` 到最后一条是 **138.2 s**
  （`04:21:17.555964Z → 04:23:35.799372Z`，取自 `docs/evidence/C101/sec/claims.json` 本身）。
- recorder（sec 域）：`recorder_exit=0 signed=24 refused=0 reviews_total=24`，
  逐名 `REVIEW_SIGNED: sec::InsiderTrading` 在其中。
- merge：`claims=1335 identities=267 reviews=258 accepted_identities=258`，
  `claims_by_status` 实测 `{PASS: 1290, BLOCKED: 45}` —— HEAD 是 `{PASS: 1285, BLOCKED: 50}`，
  5/5 对得上（本行 5 枚 BLOCKED → 5 枚 PASS），总数 1335 不变，所以这不是「多产了 5 枚」而是同一批
  身份换了状态；`identity_set_sha256[0:16]=5168e1e723999312` 与 §79/§81/§82 同值 → 零重基。
- judge：`350 identities, 258 offline accepted`，状态普查
  `{OFFLINE_ACCEPTED: 258, UNREVIEWED: 83, INCOMPLETE: 9}`（上一代 257/83/10）。
  `summary.primary_review_count=258 = merge.reviews`，`invalid_claims=[]`、`global_issues=[]`。

## 3. 载体六臂反例面（`probes/carrier_falsification.py`，`probe_exit=0`）

```
A_control: PASS seam_legs=24 rows=11 carrier_sends=24 preflight_demands=24 grant_equals_preflight=True
B_grant: PASS carrier_sends=23 refused_with=SecRequestAttemptLimitError/SEC_REQUEST_BUDGET_EXCEEDED
C_page_shape: PASS page_rows=[100, 100, 100] refused_with=SecProviderError/SEC_INSIDER_WINDOW_INCOMPLETE
D1_needle_census: PASS declared_archive_needles=20 urls_the_fetcher_builds=20 needles_equal_urls=True borrowed_url_carried=False
D2_live_arm: PASS raised=SecProviderError code=SEC_TRANSPORT_ERROR seam_message_swallowed=True
E_pairing_census: PASS rows=11 distinct_accessions=10 carried_filings=10 title_names_own_accession=True security_type={'non_derivative': 10, 'derivative': 1}
carrier_arms=6 carrier_passed=6 carrier_failed=0
```

臂的分工：A 是控制（把 `seam_requests=24` 报成读数而不是断言）；B 是「24 不是巧合」的那一侧 —— 少一
条腿就掉到预flight 之下，源在第一个请求之前拒绝；C 是页面形状那一侧 —— 第三页补足 100 行后没有一页
能结束翻页，源的 `SEC_INSIDER_WINDOW_INCOMPLETE` 就是「第三页真的被请求过」的证据；D1 是把 20 枚档案
针与 `_filing_urls` + `resolve_primary_xml_url` 现算出来的 20 个 URL 逐名对齐，并断言「借用邻家文件名」
那个 URL 不在针集里；D2 是同一件事的活臂；E 是逐行配对普查。

**D2 那面有一处提供方表面值得写清而不是抹平**：假客户端对没有载体的 URL 抛的是
`WitnessFailure`（消息点名那个 URL），但 `fetch_bytes` 的 governed-client 分支把这一支里除预算之外的
每个异常都收成 `SecProviderError("SEC_TRANSPORT_ERROR")`。所以被证伪的是「借用来的文档进不了行集」，
而可读回来的后果是：**一个声明错的载体只能以 transport 码归责，拿不到缝自己的那句话**。本波没有为此
改产品代码，只把断言面改成源真正报的那个码，并把 `seam_message_swallowed=True` 一并打出来。

## 4. plane 1 增量与地板三臂（`probes/acceptance_delta_and_phase_floor.py`，`delta_exit=0`）

```
A_delta_census: PASS accepted_old=257 accepted_new=258 ADDED=['sec::InsiderTrading'] REMOVED=[] populations=350/350 identity_set=5168e1e723999312 unchanged=True sec::InsiderTrading_old=INCOMPLETE
B_pin_census: PASS sec_claims=120 claims_pinning_shard=120 distinct_shard_digests=1 on_disk_sha256=50128e356054ec52 digest_matches_disk=True sec_statuses=['PASS']
C_phase_floor: PASS judge_control=258 judge_without_claims=257 judge_without_review=257 reviews=258->257 row_accepted(control/no_claims/no_review)=1/0/0 populations=350/350/350
plane1_arms=3 plane1_passed=3 plane1_failed=0
```

- A 与 §2 的 merge/judge 是两台仪器：这里按 `model_identity` 逐名比 HEAD 报告与工作树报告，
  两个方向都打（`ADDED` 与 `REMOVED`）。+1 若是靠别处翻标签换来的，`REMOVED` 一定非空。
  旧身份状态是 `INCOMPLETE`（不是 UNREVIEWED）：那一行此前只有 5 枚 BLOCKED  claim，缺的是相，不是评。
- B 把「入库的那片分片字节」与「120 枚 sec claim 钉住的那枚摘要」绑成同一枚：
  `distinct_shard_digests=1` 且 `digest_matches_disk=True`。**这条就是本波 lint 之后必须重产的机检理由**
  —— 分片本身是钉住对象，改字节不重产，臂 B 立刻红。
- C 是「五相 + 一枚 review 缺一不可」的地板：同一份 merge 载体裁两刀（只丢 5 枚 claim / 只丢那枚
  ACCEPT review），各自喂给仓自己的 judge（subprocess 调 `iter2_model_evidence.py`，不在探针里重写判定），
  读数 `258 / 257 / 257`，身份母数三臂同为 350。裁出来的临时载体写在 `.w21-scratch/counterfact/` 下，
  由探针自己删；入库面是这段打印 + 探针本身。

## 5. 本波的自纠（每一条都在跑过之后才知道）

1. **爆炸半径不是「全树」**：上一波改了共享仪器（`_carrier_sends`），代价是 22 家全量重签；本波只是
   给 sec 加一片分片，`witness_module_rels('sec')` 从 1 条变 2 条，于是只有 sec 的 120 枚 claim 再老化。
   实测面：`witness_module_rels_sec= [sec.py, _parts/sec/insider_rows.py]`，写集里非 sec 路径 0 枚（§1）。
2. **`_atom_page` 的第一版会把翻页走早**：页数按 `MAX_FEED_PAGE_SIZE` 与尾页行数算，写错时会给出
   5 行的小页，第一页就结束翻页，24 条腿里 11 条没人问 —— 在跑之前按源的 `iter2_insider.py:680-684`
   断点改回 100/100/40。上面 C 臂就是把这条形状钉住的那一侧。
3. **E 臂先红后绿**：`_parse_index_xml_name` 之外，配对普查要求衍生表那一行也带 accession；
   第一次读数是 `title_names_own_accession=False`，载体补了 `Non-Qualified Stock Option, accession …`
   才绿。红的那次没有入库为「通过」。
4. **一次与判定无关的 exit=1**：试跑把 `--output` 指到仓库外（`/tmp/...`），producer 的五相全 PASS 之后
   在最后那行 `output_path.relative_to(REPO_ROOT)` 显示时崩了，exit=1。这是**退出码不等于判定**的又一例，
   也是本波之后一律把 producer 的 `--output` 留在仓内的原因；那次五行面整份留在
   `runs/precheck_five_phase_scratch.txt`，包括 traceback。
5. **lint 是钉住对象的改动**：分片先 `I001`（导入块未排序）后 format 再折一条 f-string，两刀都在 producer
   跑之前落完，所以 §4-B 的摘要与入库字节同源。探针自身先读 `18 RUF100 + 2 E501 + 1 D103 + 1 F401`，
   改后八枚读数全 0（`runs/lint_readings.txt`：分片 + 三枚探针，各 check/format 一条）。顺带把平面的
   归属分清：`make lint` 走 `PY_SELFDEV := opendata scripts tests alembic alembic_data`（Makefile:25），
   但它是 Makefile:44 自己标的 A1 开发视图（"full tree; A1 debt is expected here"）；闸门那条是
   `a2-check`（Makefile:41，A2「碰到才算数」）。`docs/evidence/**` 不在 `PY_SELFDEV` 里，所以三枚探针
   是我自己要求才干净的，不是闸门要求的。提交前把 A1 全树也量了：`make_lint_exit=2`、157 条分布在
   50 枚 HEAD 就有的文件，本波载荷与它们的交集为 0 —— 这条欠量不是本波的，也不因为本波干净就消失。
6. **上一轮口述的一处更正**：我在续接摘要里把 `SHARD_ONLY_ROWS` 记成「仓里不存在的名字」。它存在 ——
   在 C125 那台谓词探针里（`probes/next_wave_predicate.py`），不在产品代码里。本波 §6 用同一台仪器
   重读，读数从 `1 ['sec::InsiderTrading']` 变 `0 []`，两侧都在档案里。
7. **凭证面上沿用的两句理由都被普查否证**（`runs/secret_scan_payload.txt` §1/§2）：
   (a) C125 §1 说本类载荷「绝大多数就是那条 line-targeted 豁免的形状」—— 普查 `docs/evidence/C101`
   全树 4671 枚 / 259828 行含引号 64hex，命中该豁免正则的行 **0**，本波 167 枚 214351 行同样 0。
   两臂同为 0 的结论没变，变的是它凭什么成立：豁免在这份证据写法下根本没被触发（producer 一键一行，
   路径与摘要分行），第二臂是保险不是判据。能力仍在，用合成人测出来了（同行 → shipped 0 / bare 1）。
   (b) C125 §2 登记的假控制「`ghp_` + 40 字符 → 0 findings，因为 body 定长 36 落不进规则」重跑读 **2**
   （`generic-api-key` + `github-pat`）。长度普查 30/35 → 只 generic，36 → 只 github-pat，37/40 → 两条都出。
   规则是 `ghp_[0-9A-Za-z]{36}` 的前缀无关锚定子串匹配，长 body 含可匹配的 36 位前缀。
   登记的「假控制」本身也得是一条读数，不能是一条推理。
8. **差分臂第一轮打出四条 `findings=0`，扫的是不存在的目录**：脚本把合成人 `cp` 进两个尚未创建的
   目标路径，`cp` 失败被 `2>/dev/null` 咽掉，四个臂照样各报一行 0。那四条 0 全部作废、不入库，
   改为写入后用 `[ -s file ]` 证真实存在再扫（`runs/secret_scan_payload.txt` §2 末段）。臂的 0 只有
   在「扫的东西确实在那里」时才是读数 —— 与「沉默的默认值伪造 0」同族。

## 6. 谓词重读（同一台 C125 仪器，`predicate_exit=0`）

```
POSITIVE_CONTROL fred::SeniorLoanOfficerSurvey status=OFFLINE_ACCEPTED grant=6 expected_grant=6 -> the instrument must see the grant this wave ran on
identity_count=350 accepted=258 residue=92
SHARD_ONLY_ROWS=0 []
PREFLIGHT_RESIDUE=2 (these fetchers consult the request budget, so their carrier must answer every leg or the runner refuses before transport):
  intrinio::ForwardEbitdaEstimates@grant=NO_WITNESS@row_model=ABSENT
  intrinio::ForwardSalesEstimates@grant=NO_WITNESS@row_model=ABSENT
NEXT_WAVE_BUCKETS_OK=True
```

`runs/next_wave_predicate.txt` 是逐字面全量（含 `truth_table` 三行与 `per_provider_residue` 十四行）。

残量按 provider 普查：`yfinance 29 / tmx 24 / nasdaq 9 / government_us 6 / deribit 5 / intrinio 3 /
seeking_alpha 3 / wsj 3 / cboe 2 / econdb 2 / eia 2 / federal_reserve 2 / biztoc 1 / stockgrid 1`。
把 14 家加起来是 **92**；仪器自己那行也印 `identity_count=350 accepted=258 residue=92`，两台都指同一批
身份，而 350 − 258 = **92** 是判定侧的数。`truth_table` 的 58 + 32 + 2 同样是 92 —— 一条只数得出自己桶的
仪器给不出这三个 92 同时成立。谓词的上一代（C125 §6）在同名桶上是 `residue=93` 与 `SHARD_ONLY_ROWS=1`，
本波差的正是那一枚。


## 7. 复算配方

`$PY = /Users/yunjinqi/opt/anaconda3/envs/py313/bin/python`，仓库根执行，`PYTHONPATH=.`；
每步独立 exit，不要用管道吞掉返回码（zsh 读 `$pipestatus`，不是 `$PIPESTATUS`）：

```bash
cd /Users/yunjinqi/Documents/new_projects/opendata && export PYTHONPATH=.
$PY docs/evidence/C126/sec-insider-witness-20261017/probes/carrier_falsification.py
$PY scripts/quality/iter2_claims_producer.py --provider sec \
    --evidence-root docs/evidence/C101 --output docs/evidence/C101/sec/claims.json
$PY scripts/quality/iter2_primary_review_recorder.py \
    --claims docs/evidence/C101/sec/claims.json \
    --output docs/evidence/C101/sec/claims.reviewed.json \
    --provider sec --decision ACCEPT --reviewer-type primary_model \
    --reviewer-id iter2-acceptance-reviewer --reviewer-model qoder-cli-agent \
    --reviewer-role acceptance_reviewer
$PY scripts/quality/iter2_claims_merge.py
$PY scripts/quality/iter2_model_evidence.py \
    --inventory "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv" \
    --claims docs/evidence/C101/claims.merged.json \
    --output docs/evidence/C101/judge/claims.merged.report.json
$PY docs/evidence/C126/sec-insider-witness-20261017/probes/acceptance_delta_and_phase_floor.py
$PY docs/evidence/C125/sloos-witness-20261017/probes/next_wave_predicate.py
```

先试跑单模型（不改入库面）：`--models InsiderTrading --evidence-root .w21-scratch
--output .w21-scratch/claims.json`。**`--output` 必须在仓内**，见 §5.4。
本波之后 `.w21-scratch/` 已被删除（它是一次性根，不是证据）；配方第 1/6/7 步不读它。

若之后改的是 `iter2_claims_probe_runner.py` 或 `iter2_claims_fixtures.py` 这类共享仪器，上面第 2/3 步之前
要先 `bash docs/evidence/C125/sloos-witness-20261017/probes/resign_all_pins.sh`（22 家全量重签）；只加分片
的波不需要 —— 臂 B 与 §5.1 就是这条区别的量。

## 8. 提交面与凭证两臂

提交集按「代码 + 精选证据」的口径收成四组：

| 组 | 枚数 | 内容 |
| --- | --- | --- |
| 代码 | 1 | `scripts/quality/iter2_witnesses/_parts/sec/insider_rows.py` |
| 证据·C101 | 155 | §1 的 143 枚重写 + 12 枚新增 |
| 证据·C126 | 14 | `face.md` 1 + probes 3 + runs 10 |
| 叙述 | 1 | `开发验收记录.md` §83（整枚文件入 diff，改动只在 §83） |

合计 171 枚。R1 轮扫的是本文件与 §83 之外的 167 枚（10748640 字节 / 214351 行），清单用
`git -c core.quotepath=off diff --name-only` ∪ `git ls-files --others --exclude-standard` 再按
`^(scripts/quality/|docs/evidence/(C101|C126)/|docs/迭代计划/)` 过滤得到；副本 `cp -p` 到
`/tmp/c126_scan`，`find … -type f | wc -l` 与清单行数同为 167、字节和同为 10748640。

刻意**不入**的未跟踪面：`.w21-scratch/**`（一次性根，删）、`docs/evidence/C100/**`（C113 的裁定）、
`docs/evidence/C108/`、`quality-source-repairs/`、`uv.lock`、`LICENSE-*.md`、`DISCLAIMER.md`。它们没被
扫过也没被提交，本面不主张它们干净。

凭证两臂（同一载荷副本、shipped 配置与去掉全部 allowlist 的 bare 配置）读数记在
`runs/secret_scan_payload.txt`：扫描对象是上面四组的**逐字节载荷**，对照臂把一枚 36 位 body 的
`ghp_` 形 token 种进副本自己的目录，读回 `findings=1 ruleId=github-pat`（副本 167→168 枚、
10748640→10748697 字节）后再删；字面量一律运行时生成，不贴进任何档案（C125 §4 记的就是贴字面量
被下一轮扫出来）。两臂在本波同为 0，但**理由与 C125 写的不是一条**：普查 `docs/evidence/C101` 全树
4671 枚、259828 行含引号 64hex，命中那条 line-targeted 豁免正则的行数为 **0** —— producer 写成
一键一行、路径与摘要分行，结构上落不进那个形状。豁免的能力用一份合成人测过（token 与豁免对同行
→ shipped 0 / bare 1；token 自占一行 → 两臂各 1），结论是第二臂在本波是保险而不是判据。
同一批重测还把 C125 登记的第二条假控制否证了：`ghp_` + 40 位读数不是 0 而是 2
（`generic-api-key` + `github-pat`），真实边界见 `runs/secret_scan_payload.txt` §2 的长度普查。

## 9. 下一波谓词（含作废条件与失败读法）

- 谓词：残量最小的四家共 8 枚，但它们是**两种形状**（我第一版写成"同一形状"，普查否证）：
  `federal_reserve` 2 + `eia` 2 是 `bound=True row_model=False witness=False preflight=False`，
  每枚只需 Leg A 的 `row_model` 声明 + 分片；`cboe` 2 + `econdb` 2 是
  `bound=False row_model=False witness=False`，先要的是**真实绑定**（provider 侧注册），声明那一步
  之后才谈得上载体。两类混在一波里做，plane 1 应从 **258 到 266**。
- 桶面（这一条我错过一次，改由仪器读）：judge 的 `UNREVIEWED` 不是"有 claim 没评"，而是
  **该身份一枚 claim 都没有**（`iter2_model_evidence.py:1181-1182`，图例同句写在
  `judge/claims.merged.report.md`），有 claim 但不全 `CANDIDATE` 才落 `INCOMPLETE`（1183-1193）。
  按这条实测：6 枚（cboe 2 + econdb 2 + federal_reserve 2）各带 5 枚 BLOCKED claim，现在在
  `INCOMPLETE`；`eia` 2 枚 claims=0，现在在 `UNREVIEWED`。所以波后是
  **`INCOMPLETE` 9→3、`UNREVIEWED` 83→81**，不是我先前写的"9→1"。
  `probes/next_wave_bucket_simulation.py` 用仓里的 judge 量这条规则（合成文档只写 scratch）：
  `A_control: PASS accepted=258 buckets={'INCOMPLETE': 9, 'OFFLINE_ACCEPTED': 258, 'UNREVIEWED': 83}`；
  `B_claims_presence: PASS blocked_claims_dropped=30 INCOMPLETE=9->3 UNREVIEWED=83->89 accepted=258->258`
  （删 claim 而非改 status 才动桶 → 分桶由"claim 是否存在"决定）；
  `C_review_receipt_binding: PASS status=OFFLINE_ACCEPTED->INCOMPLETE
  issues=['primary_review_receipt_decision_mismatch']`（只改合并文档里的 decision 改不动判定 ——
  review 的 `receipt.sha256` 绑着入库的 `primary-reviews/<id>.review.json`）。红的那一版断言
  `REJECTED` 在 `runs/next_wave_bucket_precedence.txt` 里整段留着。
- 母数：这 8 枚取自 §6 那台谓词的桶普查（`NEXT_WAVE_BUCKETS_OK=True` 那一代），不是我从名单里挑的数；
  `UNREVIEWED 83` 那一桶不整体在本谓词内（81 枚需要的是 claim + 评，不是这一波的 8 枚）。
- 作废条件：任一行的 `row_model` 声明要动 C100 已钉的输出契约（`float + null_sentinel` 那类裁定面），
  或者该 provider 只有部分行能绑（参波 19 的 `234 不可达`：那一波可达上界是 232 而不是我预告的 234）。
  任一成立 → 本谓词的 8 改成实测能声明的那几枚，并把差额留在叙述面。同一条也钉住两个不变量：
  `identity_set_sha256[0:16]` 必须仍是 `5168e1e723999312`，`REMOVED` 必须仍空 —— 一动就是重基，
  266 这个数不再由本谓词主张。
- 失败读法：如果五相全 PASS 而 judge 不动，先看 merge 的 `reviews=` 与 `accepted_identities=` 是否
  同步 +1（本波的 `claims=1335 / identities=267 / reviews=258` 就是同一步的读数）；若 `reviews` 没动，
  是 recorder 的身份键或 `--claims` 指错，不是判定放宽。如果臂 B 红，是有人改了分片字节却没重产。
