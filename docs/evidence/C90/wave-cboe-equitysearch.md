# Wave C90 — census row `OBB2-cboe-EquitySearch`

**Verdict: A — the model is declarable, actually declared, and an offline test run proves rows come out; `expressible_today` flipped to `true`.**

## What was checked (declaration face)

- `opendata/data/providers/cboe/specs.py:214` — `EQUITY_SEARCH = ModelSpec(model="EquitySearch", ...)`:
  - `decoder=DecoderSpec(kind="csv")` at `specs.py:248` — the CSV body decoder.
  - `row_filters=(RowFilterSpec(op="contains", ...), RowFilterSpec(op="contains", ...))` at `specs.py:249-266` — the two `is_symbol`-gated `contains` legs (symbol leg `when_value="True"`, name leg `when_value="False"`, both `ignore_case=True`).
  - `static_query=(("download", "csv"),)` at `specs.py:247` — upstream's payload constant, not a caller knob.
  - `columns` with `source_key` spellings ("Stock Symbol", "Company Name", "DPM Name", "Post/Station") at `specs.py:240-243`.
- `opendata/data/providers/cboe/models/equity_search.py:16` — `CboeEquitySearchFetcher = make_http_json_fetcher(SOURCE, EQUITY_SEARCH)`; the module is a pure binding.
- `opendata/data/providers/cboe/provider.py:32-37` — `LazyFetcherBinding("opendata.data.providers.cboe.models.equity_search", "CboeEquitySearchFetcher", canonical_model_ids=("EquitySearch",))`; the model is bound in the catalog.

## Test run face (offline, executed)

Command (verbatim):

```
cd /Users/yunjinqi/Documents/new_projects/opendata && PYTHONPATH=. /Users/yunjinqi/opt/anaconda3/bin/python -m pytest tests/test_cboe_equity_search.py tests/test_cboe_engine_provider.py -q --no-cov
```

rc = 0. Reading (verbatim tail):

```
collected 24 items

tests/test_cboe_equity_search.py ..........                              [ 41%]
tests/test_cboe_engine_provider.py ..............                        [100%]

============================== 24 passed in 0.37s ==============================
```

24 passed, 0 skipped, 0 failed. No live-axis skip: `OPENDATA_ALLOW_LIVE_E2E` was left unset and the test module states "No case touches a socket or a file"; the positive cases hand the engine's delimited decoder a recorded CSV body through the transport seam (`RecordingTransport`) and assert rows come out (`test_the_csv_body_decodes_into_the_published_columns`, `test_a_delimited_row_becomes_the_named_cells`, `test_the_row_filter_drops_the_row_it_names`, `test_the_gated_leg_searches_symbols_not_names`). There was no NOT_RUN to report.

## Upstream contract comparison (cites only; no code copied)

Reference tree: `/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers/cboe/`.

- `csv_decoder` label <- `openbb_cboe/utils/helpers.py:125` (URL `.../equity_index_options/?download=csv`) and `helpers.py:131` (`directory = read_csv(response)`); `from pandas import read_csv` at `helpers.py:123`. Declared by `DecoderSpec(kind="csv")` + `static_query` in our spec.
- `client_side_filter` label <- `openbb_cboe/models/equity_search.py:69-70` (`target = "name" if query.is_symbol is False else "symbol"`; `idx = symbols[target].str.contains(query.query, case=False)`). Declared by the two gated `RowFilterSpec(op="contains", ignore_case=True)`.
- Header rename (`helpers.py:133-140`) is covered by `ColumnSpec.source_key` (`specs.py:240-243`); the spec notes (`specs.py:277-285`) also record the deliberately undeclared upstream facts — `astype(str)` cast (helpers.py:142), NaN cleanup (`equity_search.py:78-84`), and `use_cache` (`equity_search.py:20-23`) — none of which is a blocker: the first two are cosmetic post-read casts upstream applies, and `use_cache` is a caching concern outside the row pipeline. No need label is missing; the existing two labels match what upstream does.

## Census row: before / after

File: `docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/census-yfinance-cboe-finviz.json`

BEFORE (lines 648-667):

```json
  {
   "task_id": "OBB2-cboe-EquitySearch",
   "provider": "cboe",
   "upstream_model": "EquitySearch",
   "shape": "get_csv",
   "http_method": "GET",
   "endpoints": 1,
   "paged": false,
   "response_format": "csv",
   "row_extraction": "flat",
   "join_needed": false,
   "needs_engine_capability": [
    "csv_decoder",
    "client_side_filter"
   ],
   "expressible_today": false,
   "existing_fetcher_class": "",
   "evidence": "providers/cboe/openbb_cboe/utils/helpers.py:125",
   "notes": "Body is a CSV at www.cboe.com/us/options/symboldir/...?download=csv read via read_csv, then substring filtered."
  }
```

AFTER (only `expressible_today`, `evidence`, `notes` touched; every other field verbatim unchanged):

```json
  {
   "task_id": "OBB2-cboe-EquitySearch",
   "provider": "cboe",
   "upstream_model": "EquitySearch",
   "shape": "get_csv",
   "http_method": "GET",
   "endpoints": 1,
   "paged": false,
   "response_format": "csv",
   "row_extraction": "flat",
   "join_needed": false,
   "needs_engine_capability": [
    "csv_decoder",
    "client_side_filter"
   ],
   "expressible_today": true,
   "existing_fetcher_class": "",
   "evidence": "providers/cboe/openbb_cboe/models/equity_search.py:70",
   "notes": "csv_decoder comes from helpers.py:131 (read_csv over the ?download=csv body pinned at helpers.py:125); client_side_filter comes from equity_search.py:70 (str.contains leg gated on is_symbol at :69)."
  }
```

The evidence/notes shape follows the already-true rows in the same directory (e.g. `OBB2-cboe-IndexSearch`, `expressible_today: true` with `evidence: "providers/cboe/openbb_cboe/models/index_search.py:94"`, and the intrinio `CurrencyPairs` exemplar whose notes name the upstream line per label). Both labels stay inside `mcc.LABEL_TO_CAPABILITY` (`csv_decoder` at model_capability_census.py:110, `client_side_filter` at :180).

## Reload proof (write landed)

Command (verbatim):

```
PYTHONPATH=. /Users/yunjinqi/opt/anaconda3/bin/python -c "import importlib.util,sys,pathlib; p=pathlib.Path('scripts/quality/model_capability_census.py'); s=importlib.util.spec_from_file_location('mcc',p); m=importlib.util.module_from_spec(s); sys.modules['mcc']=m; s.loader.exec_module(m); rows=m.load_rows(pathlib.Path('docs/迭代计划/迭代2-统一Provider架构与全量能力补齐')); print('TOTAL', len(rows)); r=[x for x in rows if x.task_id=='OBB2-cboe-EquitySearch'][0]; print('FLAG', r.expressible_today, 'NEEDS', sorted(r.needs))"
```

rc = 0. Output (verbatim):

```
TOTAL 279
FLAG True NEEDS ['decode.delimited', 'rows.filter']
```

`TOTAL 279` — the row-count invariant holds (no row added or dropped). `FLAG True` and the two needs resolving to `decode.delimited` / `rows.filter` are printed from the reloaded JSON on disk, not from memory: this proves the write landed and the JSON still parses. `mcc.load_rows` fails closed on an unmapped label (`RoadmapError: unmapped census labels on ...`, model_capability_census.py:723-725), so a typo'd label here would have aborted the load rather than silently passing — the clean rc=0 is itself evidence the label vocabulary survived.

## Linters

No `.py` file was modified (only the census JSON and this report), so `ruff check`, `ruff format --check` and `mypy` were not applicable per the task constraints.

## What remains unverified after this change

This wave verified the *declared-shape* axis only: the engine's offline decoder and row-filter pipeline produce the four published columns and honour both gated `contains` legs from a recorded CSV body. What is still unverified is the *live* axis — no request was made, so neither the live body's exact header spellings nor the leading-space quirks that `specs.py:280-285` itself flags ("Neither spelling is yet SOURCE_VERIFIED against a live body") were checked against `www.cboe.com`; a live drift in the header text would surface as `HEADER_MISMATCH` refusals, which the test suite does cover as refusal cases but cannot cover with real bytes. Also unverified: upstream's `astype(str)`/NaN-cast cosmetics and `use_cache` behavior are deliberately not declared (documented in the spec notes), and the greedy-cover recomputation over the other 278 rows was not re-run beyond `load_rows` — this report only asserts the single row's flag/needs face. One incidental observation: during this session `git diff` showed pre-existing working-tree edits in `docs/evidence/C90/cast-rescale-anchor-honesty.py` and `docs/evidence/C90/roadmap-face-diff.py` (nosec comment tweaks); neither was touched by this wave and both were left as found.


---

## 复核结论（主代理 C91，2026-10-11）— **Verdict A 站住，且三件举证齐全**

主代理本轮按「翻转必须同时有 (i) 能力 present、(ii) 本行或同包 spec 有生产声明、(iii) 离线执行测试」
复算，本行三条都过，`expressible_today: true` 保留：

- `csv_decoder → decode.delimited` `status=present`，`prod_decl=4`（`cboe/specs.py` 1 + `federal_reserve/specs.py` 3）；
- `client_side_filter → rows.filter` `status=present`，`prod_decl=3`（`cboe/specs.py` 2 + `intrinio/specs.py` 1）；
- 两处生产声明里有 **cboe 自己的**，不是拿别的包的声明替本行举证。

随附一条本轮查出的相邻债，供 #85/#87 使用：同目录里 `rows.filter` 与
`rows.filter_comparison` 是两个不同的 canonical，`RowFilterOp` 只有
`contains/equals/not_equals/in/prefix`，**没有序比较算子**；本行的 `str.contains`
leg 落在前者是正确的，但把 `date_window_filter` 记到 `rows.filter` 的那些行需要逐行重看。
详见 `开发验收记录.md` §47。本回执正文一字未删。
