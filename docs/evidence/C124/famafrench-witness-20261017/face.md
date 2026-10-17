# C124 · 波 19：famafrench 六行补齐五相，plane 1 250 → 256/350（2026-10-17）

叙述与判定面在 `docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/开发验收记录.md` §81。本文件只放
读数、载体路径与复算配方，供下一次重取时逐条比对。

## 1. 写集（`sha256` 前 16）

| 载体 | 角色 | 面 |
| --- | --- | --- |
| `opendata/data/providers/famafrench/models/fetchers.py` | Leg A：6 枚 `row_model` 声明 | HEAD `def72ffd345a7d4f` → worktree `c2a600d5ab19ad20` |
| `scripts/quality/iter2_witnesses/_parts/famafrench/official_archive_rows.py` | 见证分片（433 行 / 18668 字节） | `997de8d4b5530c85` |
| `scripts/quality/iter2_witnesses/famafrench.py` | 合并壳（39 行 / 2303 字节） | `a28a572ea7765ed7` |
| `probes/verbatim_slice_census.py` | 判定仪器（164 行 / 7050 字节） | `d098ed359bc24730` |
| `probes/next_wave_predicate.py` | 谓词仪器（165 行 / 8331 字节） | `1a3fa640ae24d678` |
| `runs/verbatim_slice_census-postlintfix.txt` | 判定运行面 | `76baacad2ffaa2f5` |
| `runs/next_wave_predicate.txt` | 谓词运行面 | `d50f1a37409020bd` |

`docs/evidence/C101/` 下 74 枚：`captures/famafrench/**` 30、`receipts/famafrench/**` 30、
`fixtures/famafrench/*.json` 6、`primary-reviews/famafrench__*.review.json` 6、
`famafrench/claims.{json,reviewed.json}` 2。tracked 改动 4 枚（上表第一行 + `claims.merged.json` +
`judge/claims.merged.report.{json,md}`）。

## 2. 四步链读数

- producer：`famafrench/claims.json` 30 枚全 `PASS` = 6 行 × 5 相
  （相名实测 `implementation/contract/counterfact/direct/service`）。
- recorder：`recorder_exit=0 signed=6 refused=0 reviews_total=6`。
- merge：`claims=1335 identities=267 reviews=256 accepted_identities=256`，
  `claims_by_status={PASS: 1280, BLOCKED: 55}`，`identity_set_sha256[0:16]=5168e1e723999312`
  （与 §79 同值 → 本波零重基、零钉变动）。
- judge：`identity_count=350`、`offline_acceptance_count=256`、
  `status_counts={OFFLINE_ACCEPTED: 256, UNREVIEWED: 83, INCOMPLETE: 11}`。

mover 归因：`famafrench_accepted=6`；famafrench 在 inventory 里 6 行，6 行全 `phases=5 / issues=0 /
missing_material=[]`；250 + 6 = 256 逐名对上，非总数巧合。

## 3. verbatim 判定面（`probes/verbatim_slice_census.py --stage /tmp/french_stage`，exit 0）

`models_censused=6 VERBATIM_ALL_MATCH=True`；每行四枚面
（`archive_sha256_ok` / `slice_verbatim_match` / `carrier_member_match` / `url_contains_match`）全
`True`。母本 `sha256` 前 16、成员、成员总行、切片行、`slice_offset`：

```
bec083a0d5c4ee4f  ME_Breakpoints.csv                     total=1213  slice=5  offset=0
593f4fbef03181bc  F-F_Research_Data_Factors.csv          total=1311  slice=9  offset=0
82b66ccd6a4dff10  6_Portfolios_2x3.csv                   total=8995  slice=6  offset=14
a0871d77732fcb80  Developed_6_Portfolios_ME_BE-ME.csv    total=1852  slice=6  offset=19
6041ff104726d6aa  UK.Dat                                 total=2802  slice=6  offset=1233
6292f7eadfbd7f7d  Ind_all.Dat                            total=2796  slice=6  offset=1233
```

## 4. counterfact 面：六码一对一，每码带未篡改对照

`expected == observed` 逐字段全等（含 `refusal_class=FrenchProviderError`）：

```
FamaFrenchBreakpoints                  FRENCH_BREAKPOINT_COLUMN_COUNT   control_rows=3
FamaFrenchFactors                      FRENCH_FACTOR_HEADER_MISSING     control_rows=4
FamaFrenchUSPortfolioReturns           FRENCH_PORTFOLIO_COLUMN_COUNT    control_rows=24
FamaFrenchRegionalPortfolioReturns     FRENCH_BAD_NUMERIC_VALUE         control_rows=24
FamaFrenchCountryPortfolioReturns      FRENCH_DAT_HEADER_MISMATCH       control_rows=3
FamaFrenchInternationalIndexReturns    FRENCH_DAT_TABLE_NOT_FOUND       control_rows=3
```

## 5. 回归与静态面

- `tests/test_famafrench_provider.py tests/test_provider_model_contracts.py
  tests/test_engine_row_expansion_spec.py tests/test_engine_value_transform.py`
  → **743 passed, 179 skipped, 0 failed**（`-m "not e2e"`、`-o addopts= -p no:cacheprovider`）。
  这四套是按 `row_model` 取项的参数化面，声明落地会放宽它们的判定项，所以一起跑。
- `tests/test_pipeline_jobs.py tests/test_provider_model_contracts.py`
  → **699 passed, 179 skipped**（batch5 的两枚字节钉 `test_provider_domains.py` 6 passed、
  `test_pipeline_jobs.py` 同码在位，本波的 `fetchers.py` 改动没有把钉打 stale）。
- `ruff check` exit 0 / `ruff format --check` exit 0 / `mypy`（3 源文件）exit 0。

## 6. 本波抓到的三处自身仪器假面

1. `verbatim_slice_census.py` 的 docstring 许诺 `carrier_rebuild_match=True`，而探针从未打印过该字段
   （它合取的是 `:158` 的四枚面）。修文案，不修探针去补一行假打印；修后 grep 该词 0 命中。
2. `next_wave_predicate.py` 第一版按 `_parts` 文本扫引号键判定 witness，把
   `sec::InsiderTrading`、`fred::SeniorLoanOfficerSurvey` 读成 shard-resident，而它们的
   `claims.json` 记着 `no offline witness configured for this model row`（`exit_code=3`）。
   改成调 `iter2_claims_fixtures.provider_config(provider)["models"]` 后 `READY` 1→0、
   `NEEDS_LEG_A` 34→0：两个桶本是我造出来的。
3. 真值表第一版给 unbound 行打印 `row_model=True`（无从测量），修为
   `has_row = bound and row_model != "ABSENT"`。

## 7. 下一波谓词（`probes/next_wave_predicate.py`，exit 0）

`identity_count=350 accepted=256 residue=94`，`NEXT_WAVE_BUCKETS_OK=True`（三桶之和 == 94）：

```
bound=True  row_model=False witness=False   60   先 Leg A 再分片
bound=False row_model=False witness=False   32   先要有 binding（LookupError: unknown provider model）
bound=True  row_model=True  witness=False    2   只缺分片
SHARD_ONLY_ROWS=2 ['fred::SeniorLoanOfficerSurvey', 'sec::InsiderTrading']
```

- 谓词：为那 2 行各补一份 witness entry → producer 应产 10 枚 `PASS`，judge 256 → **258**，
  `identity_set_sha256[0:16]` 仍为 `5168e1e723999312`。
- void 条件：任一行的 `implementation` 相仍打 `no offline witness configured` → entry 键名不等于
  inventory 的 `upstream_model`，本波作废，改用 `provider_config()` 重测键名。
- 60 桶谓词：重跑探针后 `bound=True row_model=False` 60 → 0 且 `row_model=True` 桶 2 → 62。
  失败读法：60 不动而 `row_model=True` 只涨几枚 = 声明打在了分片键名而非 `resolve_model` 解析到的
  fetcher 类上。
- 32 桶不是见证问题：binding 落地前任何 witness 波产不出第一枚 claim。
- 仪器生死：`POSITIVE_CONTROL famafrench::FamaFrenchBreakpoints row_model=FamaFrenchBreakpoint
  witness_configured=True` 两枚面必须同真，否则打 `INSTRUMENT_DEAD` 并 exit 1。

## 8. 复算配方

`$PY = /Users/yunjinqi/opt/anaconda3/envs/py313/bin/python`，七条各有独立 exit：

```bash
cd /Users/yunjinqi/Documents/new_projects/opendata
$PY docs/evidence/C124/famafrench-witness-20261017/probes/verbatim_slice_census.py --stage /tmp/french_stage
$PY docs/evidence/C124/famafrench-witness-20261017/probes/next_wave_predicate.py
$PY scripts/quality/iter2_claims_producer.py --provider famafrench --evidence-root docs/evidence/C101
$PY scripts/quality/iter2_primary_review_recorder.py \
    --claims docs/evidence/C101/famafrench/claims.json \
    --output docs/evidence/C101/famafrench/claims.reviewed.json \
    --provider famafrench --decision ACCEPT --reviewer-type primary_model \
    --reviewer-id iter2-acceptance-reviewer --reviewer-model qoder-cli-agent \
    --reviewer-role acceptance_reviewer
$PY scripts/quality/iter2_claims_merge.py
$PY scripts/quality/iter2_model_evidence.py \
    --inventory "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv" \
    --claims docs/evidence/C101/claims.merged.json \
    --output docs/evidence/C101/judge/claims.merged.report.json
```

第 6 条读数应为 `350 identities, 256 offline accepted`。

**可复算性分两半，这里把话说死。** 版本库里没有六份母本归档：全仓 tracked 的 `.zip` 只有
`docs/evidence/C64/coverage-final-html.zip` 与 `docs/evidence/C85/coverage-final-html.zip` 两枚覆盖率
产物，`docs/evidence/C101/captures/**` 存的是 stdout 文本（5.0 MB）而不是二进制，本轮不把 2.7 MB 的
官方归档塞进这个约定之外。因此：

- **fixture 侧永久可复算**：六份 `fixtures/famafrench/<Model>.json` 内嵌 base64 zip，切片字节就在版本
  库里，`next_wave_predicate.py` 与 producer/recorder/merge/judge 四步不需要网络。
- **母本侧只对「本轮」可复算**：`verbatim_slice_census.py` 的 `slice_verbatim_match` 那半必须重下
  六个归档到 `--stage` 目录才能重取；重下后的第一道判定是探针自带的
  `archive_sha256_ok`（六枚摘要字面量钉在 `CAPTURES` 里），它不真就整行红并 exit 1。母本缺失时探针
  打 `capture_absent` 且 `VERBATIM_ALL_MATCH=False` —— 那是「未测量的切片不算通过的切片」，不是回归。
- 主机名 `mba.tuck.dartmouth.edu` 已写进分片的 `allowed_hosts` 与 `url_contains`，所以重下配方没有自由度：
  探针的 `url_contains_match` 会拒绝任何不是那份归档的来源。
