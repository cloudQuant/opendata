# C65 frozen AkShare port-scope review

**State:** `INVENTORY_ONLY` · as of 2026-09-30  
**Machine-readable inventory:** [port-scope-manifest.json](port-scope-manifest.json)

This review freezes path-level scope for the current AkShare port baseline. It reads the locked local checkout, `upstream.lock`, `manifest.json`, `docs/port-report.md`, source imports, and existing recorded comparison fixtures. It does not run a replay, test, provider request, or manifest/report generator.

## Frozen-set reconciliation

The locked source is commit `c4f6a631c259783dbc2507b6b27d179b3e88079d`. The local upstream checkout is at that exact commit, and its current content SHA matches all 327 `upstream.lock` rows.

| Surface | Rows | Reconciliation |
|---|---:|---|
| `upstream.lock` | 327 | 325 Python files + 2 resources |
| `manifest.json` | 327 | 325 `files` + 2 `resources`; joined by path, every path and upstream path matches the lock |
| Current `opendata_http/` tree | 327 | Every current target SHA matches its corresponding manifest SHA; all 325 Python line counts also match |
| `docs/port-report.md` | 327 | Every path/upstream path/manual-edit flag matches; stored report has 327 `✓` rows |

The 327 rows include 9 `manual_edits=true` entries. The report records 531 import rewrites and 2 string rewrites in total. These are the values already present in the current report; no report replay was performed to refresh or independently prove them. The manifest contains both the frozen source SHA and manifest/current port SHA per row, plus each corresponding port-report row’s rewrite counts and stored replay status.

The two manifests separate Python files and resources, so reconciliation is by path rather than row order. The full path-level joins, hashes, classifications, and reasons are in the JSON inventory.

## A2.1: stock daily function closure

The A2.1 requirement is “stock daily chain closure + utils”; it does not make the entire `stock/` package 1A. The actual public function definitions are:

| Public function | Definition file | Local import dependencies traced from that file |
|---|---|---|
| `stock_zh_a_hist` | `stock_feature/stock_hist_em.py` | `utils/func.py`, `utils/request.py` |
| `stock_zh_a_daily` | `stock/stock_zh_a_sina.py` | `stock/cons.py`, `utils/demjson.py`, `utils/tqdm.py` |

The function-definition import closure is 8 Python files. Expanding this to the complete `utils/` package as A2.1 specifies, and including the package initializers and flat `opendata_http` facade needed for the public `opendata_http.<function>` spelling, yields **15 unique frozen file paths across 4 path roots**: root facade; `stock/` (3); `stock_feature/` (2); `utils/` (9). The per-path rows mark the two `stock_feature/` files as also belonging to B1.1, because B1.1 names that whole package. `stock/cons.py` and `stock/stock_zh_a_sina.py` also contain support for APIs outside the daily chain; those remaining APIs stay on-demand.

There is a separate import-width caveat: a static local-import walk from the current `opendata_http/__init__.py` reaches **307 packaged Python modules**. That eager flat-facade reach is much wider than the 15-file function-specific scope. The inventory records this as a packaging/import concern and does not relabel every eagerly imported package as 1A. This is static source analysis, not a runtime import test.

## B1.1 and the explicit futures-derivative group

The eight packages named by B1.1 are kept under the original `B1.1` batch tag. `futures_derivative` is a **separate 1B group** because FR-4/D9 names it explicitly; it is not equated with the `futures_daily` capability.

| 1B module group | Python files | Recorded fixture hit |
|---|---:|---|
| `stock_feature` | 69 | No recorded case; the `stock_zh_a_hist` cases are pending |
| `stock_fundamental` | 23 | Yes |
| `futures` | 32 | Yes |
| `index` | 27 | Yes |
| `fund` | 27 | Yes |
| `option` | 19 | Yes |
| `bond` | 17 | Yes |
| `economic` | 19 | No |
| `futures_derivative` *(separate D9 group)* | 12 | No |
| **Total** | **245** | **6 of 9 groups hit** |

The B1.1 package paths also contain one resource, `stock_feature/ths.js`. Thus the B1.1 inventory has 245 replay-eligible Python files plus that resource; the resource is not counted as a callable AC-6 sample file. `stock_feature/stock_hist_em.py` is one path, included once in the 245-file denominator even though its A2.1 function is separately in the 1A function scope.

Files outside the two-function A2.1 scope, B1.1 named groups, and shared A1/A2.3 support are tagged `B1.2_ON_DEMAND`. This follows B1.2’s “remaining submodules” rule; it does not assert that every such file is non-financial, and it does not change any original P0/P1 label. The residual on-demand inventory is 62 paths: 58 other `stock/` Python files and 4 `pro/` Python files. The D9-excluded `movie`, `news`, `nlp`, `air`, `fortune`, `cost`, `article`, and `tool` packages do not appear in this 327-path frozen port set.

Other shared files are classified under their explicit plan work: `_version.py`, `exceptions.py`, and `request.py` as shared A1 foundation; `file_fold/__init__.py` and `calendar.json` as TradingCalendar foundation; and `datasets.py` under A2.3’s “repair or explicitly mark unavailable” requirement. The existing port report records the unavailable-resource path for the two `datasets.py` accessors.

## AC-6|02: separate 20% denominators

B1.3 says “remaining domains” but does not make its sampling unit unambiguous. The inventory therefore reports both possible denominators.

| Sampling unit | Denominator | Existing recorded hits | 20% minimum | Current fixture inventory |
|---|---:|---:|---:|---|
| B1.1 module groups, including separate `futures_derivative` | 9 | 6 | 2 groups | `stock_fundamental`, `futures`, `index`, `fund`, `option`, `bond` |
| B1.1 replay-eligible Python files | 245 | 7 | 49 files | 7/245 = 2.86%; short of the file-level threshold |

The seven hit files inside B1.1 are `stock_fundamental/stock_finance_sina.py`, `index/index_cons.py`, `index/index_stock_zh.py`, `futures/futures_zh_sina.py`, `option/option_finance_sina.py`, `bond/bond_zh_cov.py`, and `fund/fund_etf_sina.py`. Four other recorded cases hit `stock/stock_zh_a_sina.py`, which is outside the nine B1.1 groups and does not count toward either B1.1 numerator.

There are **14 existing executable recorded fixtures** with both `responses.json.gz` and `reference.csv.gz`, covering 8 source files overall (7 in B1.1), 7 module roots overall (6 in B1.1), and 10 distinct public functions overall (9 in B1.1). The existing comparison report lists these 14 as `PASS`; that historical result is not a fresh replay against this shared working tree.

| Source module/file | Recorded case names | Public functions |
|---|---|---|
| `stock_fundamental/stock_finance_sina.py` | `stock_action_dividend`, `stock_action_rights`, `financial_statement`, `financial_indicator` | `stock_history_dividend_detail`, `stock_financial_report_sina`, `stock_financial_analysis_indicator_em` |
| `index/index_cons.py` | `index_constituent` | `index_stock_cons_weight_csindex` |
| `index/index_stock_zh.py` | `index_daily_sina` | `stock_zh_index_daily` |
| `futures/futures_zh_sina.py` | `futures_daily_sina` | `futures_zh_daily_sina` |
| `option/option_finance_sina.py` | `option_daily_sina` | `option_sse_daily_sina` |
| `bond/bond_zh_cov.py` | `bond_daily_sina` | `bond_zh_hs_cov_daily` |
| `fund/fund_etf_sina.py` | `fund_etf_daily_sina` | `fund_etf_hist_sina` |
| `stock/stock_zh_a_sina.py` *(outside B1.1)* | `stock_daily_sina_raw`, `stock_daily_sina_qfq`, `stock_daily_sina_raw_wide`, `stock_daily_sina_qfq_wide` | `stock_zh_a_daily` |

The following four cases remain `PENDING` and are excluded from every numerator: `stock_daily_raw` (`stock_zh_a_hist`), `stock_daily_qfq` (`stock_zh_a_hist`), `index_daily_em` (`index_zh_a_hist`), and `fund_etf_daily_em` (`fund_etf_hist_em`). `fuyao_t1_envelopes` is a separate fixture directory, not a `CASES` entry in the AC-6 replay script, so it is excluded as well.

The existing offline replay entry point is:

```sh
/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base python scripts/codemod/compare_with_upstream.py --compare
```

The script’s `--compare` mode replays the recorded cases offline. Its `--only` option applies to `--record`, not `--compare`; the compare run considers the complete case list and reports pending cases. This command was **not run** for this inventory. A module-group denominator would meet the numeric 20% threshold based on current fixture coverage; a file denominator would not. The project’s chosen sampling unit and a fresh current-tree replay remain for the main acceptance decision.

## Acceptance use and limits

The JSON gives AC-5|02 a full 327-path scope map: each frozen source path has a module, batch tag, reason, source SHA, manifest/current target SHAs, and matching report row. All 327 paths reconcile to the frozen lock, manifest, current target, and port-report path set. It is scope-coverage evidence, not a fresh codemod replay or an AC-6 result.

No source, configuration, prior C64 artifact, or database was changed. No tests, report generators, replay, network access, or database access were used. P0/P1 terminology remains as written in the requirement and implementation plan; batch labels here are only the path-level implementation-scope mapping.
