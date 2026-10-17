# C125 · 波 20：fred::SeniorLoanOfficerSurvey 补齐五相，plane 1 256 → 257/350（2026-10-17）

叙述与判定面在 `docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/开发验收记录.md` §82。本文件只放
读数、载体路径与复算配方。

本波真正落下的不是一行，而是一条结构事实：**离线载体能应答几个请求，必须由载体自己说**。
§81.8 把这两行叫「最便宜」，判据里没有这一列，于是Forecast 在正确的方向上错了 —— 详见 §82.2。

## 1. 写集（`sha256` 前 16）

| 载体 | 角色 | 面 |
| --- | --- | --- |
| `scripts/quality/iter2_claims_probe_runner.py` | 共享仪器：`_carrier_sends` 从载体推导请求额度 | HEAD `39bd675457d15934`（27951 B）→ worktree `6f73b11a14b8c81d`（29560 B，685 行） |
| `scripts/quality/iter2_witnesses/_parts/fred/sloos_rows.py` | 见证分片（182 行 / 8883 字节） | `57d244333c6a2515` |
| `docs/evidence/C101/fixtures/fred/SeniorLoanOfficerSurvey.json` | 六腿载体（195 行 / 5813 字节） | `ebbafa9ae7594428` |
| `probes/limit_and_grant_counterfact.py` | 三相反例仪器（168 行 / 7459 字节） | `46eba0f17da03c27` |
| `probes/next_wave_predicate.py` | 带 grant 列的下波谓词（194 行 / 9227 字节） | `9c1a3ceb7c234a1a` |
| `probes/resign_all_pins.sh` | 重签驱动（48 行 / 2394 字节） | `0e632ead5b3adb31` |
| `runs/limit_and_grant_counterfact.txt` | 判定运行面 | 见 §3 |
| `runs/next_wave_predicate.txt` | 谓词运行面 | 见 §6 |
| `runs/resign_tally.txt` | 22 家逐家 emit/signed 面 | 22 行 |
| `runs/{fred_produce,merge,judge}.txt` | 四步链运行面 | 见 §2 |
| `runs/resign_{finra,multpl}.txt` | 两小域试签逐相日志 | 13 行 / 8 行 |
| `runs/secret_scan_payload.txt` | 提交前凭证两臂 + 两侧对照 | 见 §8 |

`docs/evidence/C101/` 下改动 **1348 枚 tracked 文件 + 259 枚新增**：22 家 provider 的
`{p}/claims.json`、`{p}/claims.reviewed.json`、`receipts/{p}/**`、`captures/{p}/**` 全部随重签重写。
新增 259 枚按角色拆开是 `primary-reviews/` 248、`fixtures/fred/SeniorLoanOfficerSurvey.json` 1、
`captures/fred/SeniorLoanOfficerSurvey/**` 5、`receipts/fred/SeniorLoanOfficerSurvey/**` 5
（`git status --porcelain` 把后两组各折成 1 条目录项，所以 porcelain 面是 251 条而文件面是 259 枚）。
那 248 枚 review **不是本波造的**：`git ls-tree -r HEAD` 面只有 9 枚（ecb 3 + famafrench 6），
其余已验收行的逐行 review 记录一直没入库，本波重签把它们一并写出。磁盘上 review 总数 257 =
merge 的 `reviews=257` = 判定的 `offline accepted=257`，一枚一行，没有多写也没有漏写。
**本波不是单 provider 波**：一台共享仪器改了字节，钉它的每一家都得重产重签（§5）。

## 2. 四步链读数

- producer（fred 域，`--output docs/evidence/C101/fred/claims.json`）：
  `claims emitted=180 (180 PASS, 0 FAIL, 0 BLOCKED) = 36 row(s) x 5 phases`，
  `PRODUCER_exit=0`；`denominators: inventory total=350 (pinned identity set 5168e1e7…)`,
  `provider rows covered=36 of provider inventory rows=36`。
- recorder（fred 域）：`recorder_exit=0 signed=36 refused=0 reviews_total=36`。
  同一条命令在修 `--output` 之前跑过一次，那次读数是 `signed=0 refused=36`，理由逐相 `stale` ——
  两半都在 §4/§5 记着。
- merge：`claims=1335 identities=267 reviews=257 accepted_identities=257`，
  `claims_by_status={PASS: 1285, BLOCKED: 50}`（波 19 是 `PASS 1280 / BLOCKED 55`：sloos 那一行的
  5 枚 BLOCKED 转成 5 枚 PASS，5/5 对得上，非总数巧合），
  `identity_set_sha256[0:16]=5168e1e723999312` —— 与 §79/§81 同值 → 本波零重基、身份集零变动。
- judge：`350 identities, 257 offline accepted`，
  状态普查 `{OFFLINE_ACCEPTED: 257, UNREVIEWED: 83, INCOMPLETE: 10}`（波 19：256/83/11）。

**回归双臂（重签是否行为不变）**：把 HEAD 的报告与工作树报告按身份名逐名比对，
`HEAD accepted=256 / worktree accepted=257`，`ADDED=1 ['fred::SeniorLoanOfficerSurvey']`，
`REMOVED=0 []`，`per_provider_accepted_delta={'fred': 1}`。22 家重产后只剩这一枚移动，
即重签是字节再老化，不是判定改写。

## 3. 三相反例面（`probes/limit_and_grant_counterfact.py`，exit 0）

```
fixture=docs/evidence/C101/fixtures/fred/SeniorLoanOfficerSurvey.json pages=6 limits=10000,9998,9996,9994,9992,9990
GRANT derived=6 expected=6 rows=12 seam_requests=6 code=None expected_code=None
LEGACY_GRANT sends=3 rows=0 seam_requests=0 code=FRED_QUERY_LIMIT expected=FRED_QUERY_LIMIT
CONSTANT_LIMIT grant=6 rows=0 seam_requests=0 code=FRED_PAGINATION_ERROR expected=FRED_PAGINATION_ERROR
EXPECTED_LIMITS=10000,9998,9996,9994,9992,9990 distinct=True
LIMIT_AND_GRANT_PROVEN=True
```

三臂都跑在真 fetcher 上（`models/sloos.py` 绑定的 `FredSeniorLoanOfficerSurveyFetcher`）， sockets
进程级拒绝，仪器只调用 runner 自己的 `_run_direct_with_seam` / `_grant_context` / `_client_seam` /
`_typed_provider_refusal`：

- `GRANT`：额度 6 由载体推出，12 行 6 请求 —— 这一臂就是验收链上跑通的那条路径。
- `LEGACY_GRANT`：同一载体、`sends=3`（修前的默认）→ 预检在 I/O 之前拒 `FRED_QUERY_LIMIT`，
  `seam_requests=0` 即「一个字节都没读就拒了」。这是 §82.2 那条 Forecast 的正面对照。
- `CONSTANT_LIMIT`：把六页头部的 `limit` 全改成第一腿的值（人写分片的常态形状）→
  `FRED_PAGINATION_ERROR`。递减 `remaining_records`（`sloos.py:216,230`）与页头相等断言
  （`sloos.py:148`）是活守卫，不是摆设。

counterfact 相另有 `expected == observed` 全等面（`refusal_class=FredProviderError`,
`code=FRED_BAD_OBSERVATION`）与未篡改对照臂 12 行；该行不声明 `expected_*` 面，因为
`FredProviderError` 只带码不带字段归因 —— 空归因面是契约推出来的，不是跑出来的。

## 4. 我这轮先做错的两件事（都在这里记账）

1. **`--output` 漏了。** producer 的 `--output` 默认是 `<evidence_root>/claims.json`，而 recorder 读的是
   `<provider>/claims.json`。我两次 fred 重产只写了前者，于是 recorder 读的是昨天的那一份，
   逐相回 `stale`；`docs/evidence/C101/claims.json`（tracked 的上一次 scope 残件，HEAD 里是 benzinga
   20 枚）被 fred 的 180 枚覆盖。两枚都被我 `git checkout HEAD --` 复位后按正确 argv 重跑。
2. **recorder 全拒仍然写盘。** `signed=0 refused=36` 那次照样写了 `fred/claims.reviewed.json`，把
   HEAD 里 35 枚真评审换成了 0 枚。判据只读 `recorder_exit=0` 会看不出损伤 —— 必须读它自己那行
   `signed=/refused=`。复位后面是 `HEAD claims=180 reviews=35`。

## 5. 钉面 blast radius（这台仪器改一行的代价，实测）

`iter2_model_evidence.py:621-627` 会把每枚 `pins.<group>[i].after_sha256` 与工作树重算哈希逐一对比，
不同则 `<group>_pin_N_current_hash_drift` → `stale`。我在改之前读了 `_validate_receipt`（471-500）就下
结论「plane 1 不重哈希钉面」，那是错的函数。改完之后实测：

```
runner_disk=6f73b11a14b8
merged_claims=1335  identities_with_runner_pin_drift=256
TOTALS drift_claims=1280 current_claims=0 no_runner_pin_claims=55
22 家 provider 全部命中（fmp 345 / intrinio 175 / fred 175 / sec 115 / congress_gov 40 …）
```

`no_runner_pin=55` 是那些根本没有见证（BLOCKED）的 claim：它们不钉这台仪器。
代价 = 22 家逐家 produce + record，即 `probes/resign_all_pins.sh`；provider 群体取自 merge 自己的
glob `docs/evidence/C101/*/claims.reviewed.json`（22 家），不写死名单。

## 6. 下波谓词（`probes/next_wave_predicate.py`，exit 0）

正面对照：`POSITIVE_CONTROL fred::SeniorLoanOfficerSurvey status=OFFLINE_ACCEPTED grant=6
expected_grant=6` —— 仪器看得见它刚用过的额度，否则它印的桶没有价值。

```
identity_count=350 accepted=257 residue=93
  bound=True  row_model=False witness=False preflight=False grant=NO_WITNESS   58
  bound=False row_model=False witness=False preflight=False grant=NO_WITNESS   32
  bound=True  row_model=False witness=False preflight=True  grant=NO_WITNESS    2
  bound=True  row_model=True  witness=False preflight=True  grant=NO_WITNESS    1
SHARD_ONLY_ROWS=1 ['sec::InsiderTrading']
PREFLIGHT_RESIDUE=3
  intrinio::ForwardEbitdaEstimates@grant=NO_WITNESS@row_model=ABSENT
  intrinio::ForwardSalesEstimates@grant=NO_WITNESS@row_model=ABSENT
  sec::InsiderTrading@grant=NO_WITNESS@row_model=SecInsiderTransaction
NEXT_WAVE_BUCKETS_OK=True
```

新列第一次跑出来是 `PREFLIGHT_RESIDUE=0`：我把它 keyed 在「已有载体」的行上，而 93 枚残行全都没有
载体 —— 真值但零信息。改成 keyed 在 fetcher 之后是 3 枚，正好点名「写载体时必须先把额度填满」的行。
谓词残量分布未变的两家：`yfinance` 29（27 bound 无 `row_model` + 2 未绑定）、`tmx` 24（16 未绑定 +
8 bound 无声明）—— 这两家是 Leg A 声明与真实绑定的活，不是分片写作。

`sec::InsiderTrading` 现在是 `SHARD_ONLY_ROWS` 里唯一一枚，且带 `preflight=True`：它的载体要按
`_common.py` 的目录 + ≤3 页 feed + 每份 filing 的 index/XML 两腿来数请求数，不能照抄本页六腿的形状。

## 7. 复算配方

`$PY = /Users/yunjinqi/opt/anaconda3/envs/py313/bin/python`，仓库根执行，`PYTHONPATH=.`；
每步独立 exit，不要用管道吞掉返回码（zsh 读 `$pipestatus`，不是 `$PIPESTATUS`）：

```bash
cd /Users/yunjinqi/Documents/new_projects/opendata && export PYTHONPATH=.
$PY docs/evidence/C125/sloos-witness-20261017/probes/limit_and_grant_counterfact.py
$PY scripts/quality/iter2_claims_producer.py --provider fred \
    --evidence-root docs/evidence/C101 --output docs/evidence/C101/fred/claims.json
$PY scripts/quality/iter2_primary_review_recorder.py \
    --claims docs/evidence/C101/fred/claims.json \
    --output docs/evidence/C101/fred/claims.reviewed.json \
    --provider fred --decision ACCEPT --reviewer-type primary_model \
    --reviewer-id iter2-acceptance-reviewer --reviewer-model qoder-cli-agent \
    --reviewer-role acceptance_reviewer
$PY scripts/quality/iter2_claims_merge.py
$PY scripts/quality/iter2_model_evidence.py \
    --inventory "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv" \
    --claims docs/evidence/C101/claims.merged.json \
    --output docs/evidence/C101/judge/claims.merged.report.json
$PY docs/evidence/C125/sloos-witness-20261017/probes/next_wave_predicate.py
```

若再改 `iter2_claims_probe_runner.py` 或 `iter2_claims_fixtures.py` 这类共享仪器，上面第 2/3 步之前要先
`bash docs/evidence/C125/sloos-witness-20261017/probes/resign_all_pins.sh`（22 家全量重签），否则第 5 步
的读数不是 257 而是崩到 0 附近 —— §5 的实测就是这个原因。

## 8. 提交面（本波入库什么、凭证两臂读数）

提交集按"代码 + 精选证据"的口径收成四组：

| 组 | 枚数 | 内容 |
| --- | --- | --- |
| 代码 | 2 | `scripts/quality/iter2_claims_probe_runner.py`（额度从载体推导）、`_parts/fred/sloos_rows.py`（新分片） |
| 证据·C101 | 1607 | 1348 枚 tracked 重写 + 259 枚新增（§1 的分解） |
| 证据·C125 | 13 | `face.md` 1 + probes 3 + runs 9（含本节凭证载体） |
| 叙述 | 1 | `开发验收记录.md` §82 |

刻意**不入**的未跟踪面：`docs/evidence/C100/**` 203 条（上一轮遗留，且 `.gitignore:249` 已按 C113
的裁定收口）、`docs/evidence/C108/`、`quality-source-repairs/`、`uv.lock`、`LICENSE-*.md`、
`DISCLAIMER.md`。它们没被扫过也没被提交，本面不主张它们干净。

凭证两臂（同一载荷副本、同一 gitleaks 8.30.1、shipped 配置与去掉全部 allowlist 的 bare 配置）三轮读数是
**0 → 2 → 0**。round 1 两臂 `findings=0 / exit=0 / scanned=38500908 字节`（1620 枚）；round 2 叙述面落完后
两臂各报一条 `github-pat`，报的是我为写清对照而**原样贴进 `runs/secret_scan_payload.txt` 的那枚 token 字面量**
—— 提供方 fixture、证据 JSON、`claims*.json`、`receipts/**`、`captures/**` 一条没报；round 3 把字面量换成
运行时生成后两臂同为 `findings=0 / scanned=38517824 字节`（1623 枚，与手工 `stat` 求和逐字节相等），
对照臂把一枚 36 位 body 的 `ghp_` token 种进**这份副本自己的目录**后仍读 `findings=1 ruleId=github-pat`、
字节面动 +58，随后删除。两条**假控制**（`AKIAIOSFODNN7EXAMPLE` 这类文档示例值、40 位超长 body 都读 0，
是控制写错了形而不是扫描器没跑）与自引用载体的边界一并记在 `runs/secret_scan_payload.txt`。

一处载体缺陷顺手修掉：`resign_all_pins.sh` 把逐家日志写成 `/tmp/resign_$p.log`，我拷进档案时沿用了
`.log` 名，而 `.gitignore:61` 是全局 `*.log` —— 这两枚试签日志在 `git status` 里根本不出现，
等于 face.md 的写集表列了两枚提交不进去的载体。已改名 `runs/resign_{finra,multpl}.txt`。
下一轮如果再拷逐家日志，名字必须落在 `.txt` 上，或者由脚本直接写 `.txt`。

**本波没跑的臂**：`make gate` 全链（含成员 2 的 acceptance-probe-check 与 mypy 平面）。
共享仪器改了字节又全量重签之后，那条链是唯一能说"17 个成员仍绿"的面，它没跑，所以 §2 的 257 只在
plane 1 判定面内成立，不扩展到 gate。
