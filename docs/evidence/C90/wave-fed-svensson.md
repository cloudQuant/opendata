# C90 — OBB2-federal_reserve-SvenssonYieldCurve: repair the census row and re-derive the ladder

**Verdict: A — `expressible_today: false` was STALE; the model is declared, carried and proven by offline tests, so the flag is now `true`, and the row needs NO new label.**

## Face 1 — the spec and the model file (read)

- `opendata/data/providers/federal_reserve/models/svensson_yield_curve.py:10` —
  `FederalReserveSvenssonYieldCurveFetcher = make_http_json_fetcher(SOURCE, SVENSSON_YIELD_CURVE)`; pure declaration, no hand-written composition.
- `opendata/data/providers/federal_reserve/specs.py:275-298` — `SVENSSON_YIELD_CURVE = ModelSpec(...)` declares
  `decoder=DecoderSpec(kind="csv")` (csv_decoder), `row_expansion=(WideTablePivotPlan(timestamp_key="Date", series_keys=<99 headers>), SeriesPivotPlan())`
  (series_pivot carried as data), and 100 `ColumnSpec`s whose `source_key`s are the published headers.
  Its own `notes` (specs.py:299-310) say the census blocker was `series_pivot` alone and that `csv_decoder`/`param_value_mapping`
  "were already present capabilities".
- Engine carriers measured on disk (both registry entries are `read_from_disk=True` in `scripts/quality/model_capability_census.py`):
  `opendata/data/providers/_engine/spec.py:127` `value_map: tuple[tuple[str, str], ...] = ()` and
  `opendata/data/providers/_engine/http_json.py:349` `def _apply_value_map(...)` (param.value_mapping);
  `opendata/data/providers/_engine/http_json.py:369` `def _expand_rows(...)` (path.explode).
- Provider binding: `opendata/data/providers/federal_reserve/provider.py` exposes the fetcher; asserted by tests (face 2).

## Face 2 — offline test runs (all --no-cov, no network; the row-expansion suite monkeypatches the socket shut)

1. `cd /Users/yunjinqi/Documents/new_projects/opendata && PYTHONPATH=. /Users/yunjinqi/opt/anaconda3/bin/python -m pytest tests/test_engine_row_expansion_spec.py -q --no-cov`
   → rc 0, verbatim: `============ 12 passed in 0.33s ============`
   Includes `test_the_wide_body_becomes_one_row_per_timestamp` and `test_each_field_carries_the_value_its_own_header_published`,
   which fetch a canned Svensson wide body through the *production* declaration via `FixedResponseTransport` with
   `_no_socket` autouse fixture ("row-expansion witnesses must never reach the governed client") — rows come out, offline, named by value.
2. `... pytest tests/test_provider_federal_reserve.py -q --no-cov` → rc 0, `36 passed in 0.35s` (provider binds `SvenssonYieldCurve`, domain `federal_reserve_svensson_yield_curve`).
3. `... pytest tests/test_federal_reserve_domains.py -q --no-cov` → rc 0, `25 passed in 0.35s`.
4. No federal_reserve test skipped for `OPENDATA_ALLOW_LIVE_E2E` in these files — every live axis is a NOT_RUN (see last section).

## Face 3 — row labels vs what upstream actually does

Upstream tree (contract only): `/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers/federal_reserve/openbb_federal_reserve/models/svensson_yield_curve.py`

- **csv_decoder** — `:143` downloads `.../data/yield-curve-tables/feds200628.csv` as text; `:1149` finds the `Date,` header line; `:1157` `csv.DictReader(StringIO(csv_content))`. Present: `decode.delimited` (registry `claim=True`, anchor `csv.reader`).
- **series_pivot — the C89 question, settled.** Upstream performs **neither a long melt nor a per-series transpose of the storage shape**:
  `:1157-1189` iterates the wide DictReader and `results.append(...)` at `:1189` emits **one record per CSV row**, columns renamed positionally-by-name
  through the inverted `__alias_dict__` (`:247-352` pairs, inverted at `:1118-1120`). The *output contract* is one row per date carrying one column per
  series (99 headers) — exactly what `WideTablePivotPlan`+`SeriesPivotPlan` reproduce, which is why the label survives as the name of the engine stage
  (`series_pivot` → `path.explode`, present via `row_expansion` + `_expand_rows`), while the old census note "rows are a per-series pivot ... **not records**"
  was the false half: they **are** records; the melt belongs to `YieldCurve` (yield_curve.py:88-115, already in `NOT_DECLARED`). No label removed, no label added.
- **param_value_mapping** — `:1123-1145` maps `series_type` values (`zero_coupon`→`sveny01-30`, `par_yield`→`svenpy*`, `forward_instantaneous`→`svenf*`,
  `forward_1y`→`sven1f01/04/09`, `parameters`→`beta*/tau*`) to the published column set. Present capability: `param.value_mapping` measured from disk
  (`spec.py:127` + `http_json.py:349`). The engine declaration answers the default `all` (svensson_yield_curve.py:173 `default="all"`) by publishing all
  100 columns, matching the pinned contract's full column face.

## The row, before / after

Before (verbatim, `census-sec-tmx-fed-gov-finra.json:225-245`):
```json
{
  "task_id": "OBB2-federal_reserve-SvenssonYieldCurve",
  ...
  "needs_engine_capability": ["csv_decoder", "series_pivot", "param_value_mapping"],
  "expressible_today": false,
  "existing_fetcher_class": "",
  "evidence": "providers/federal_reserve/openbb_federal_reserve/models/svensson_yield_curve.py:143",
  "notes": "static feds200628.csv; rows are a per-series pivot of a wide CSV, not records"
}
```
After: same labels, same evidence cite (line 143 is verbatim the feds200628.csv URL line), `"expressible_today": true`, and notes replaced by
one sentence naming the upstream line each label comes from (`:1149-1157`, `:247-352`/`:1118-1120`, `:1123-1145`) plus the flat-records correction.
All other fields (shape `get_csv`, `response_format: csv`, `row_extraction: flat`, endpoints 1, etc.) untouched — they already matched upstream.

## Instrument proof of the write

`PYTHONPATH=. /Users/yunjinqi/opt/anaconda3/bin/python -c "...mcc.load_rows(...)"` → rc 0, verbatim:
```
TOTAL 279
FLAG True NEEDS ['decode.delimited', 'param.value_mapping', 'path.explode']
```

## What remains unverified

The live axis is NOT_RUN: there is no pinned capture/fixture for `feds200628.csv` anywhere in this tree (specs.py docstring records the missing
capture root), so which cells are blank and how many rows the real file has cannot be falsified offline — the row-expansion suite proves
spec-to-row mapping on a hand-typed body, not the release's bytes. No live E2E was attempted (`OPENDATA_ALLOW_LIVE_E2E` never set). Also outside
the offline fixture: upstream's client-side behaviours listed in `DECLARED_GAPS` — `series_type` column *filtering* for non-`all` queries, the
`start_date`/`end_date` window, sentinel-to-null (`''`/`NA`/`ND`/`N/A`/`-999.99`, validator at svensson_yield_curve.py:1062-1082 in the installed
1.6.2) and the header-*search* vs `preamble_rows`-count difference — are carried as gap text, not as declared behaviour; the census row's flag
speaks to the default `all` face the tests exercise. Finally, `load_rows` recomputed the cover from 279 rows but the greedy-ladder re-derivation
across the other three contradictory rows is out of scope for this file.


---

## 复核结论（主代理 C91，2026-10-11）— **本回执的 Verdict A 被反驳，旗标已 revert**

回执第 3 行的判定「`expressible_today: false` was STALE … the flag is now `true`, and the row
needs NO new label」**两条都不成立**，本节按主代理本轮的复算把结论改回原状：

1. **`param_value_mapping` 是无中生有。** 本行的 spec（`opendata/data/providers/federal_reserve/specs.py:275-373`
   的 `SVENSSON_YIELD_CURVE`）**一个 `params` 都不声明**，静态 CSV 端点也不收参数——`grep -n "params=" `
   在该块内读空。`series_type` 的允许名单做的事是**选列**（`svensson_yield_curve.py:1123-1145`），
   不是改写 query 值。所以这行不该记 `param_value_mapping`，而该记
   `param_driven_column_selection → columns.response_dependent_selection`
   （锚 `_engine/http_json.py:520`，本轮实测 `status=absent prod_decl=0`）。
2. **「needs NO new label」是这条判定翻错的根因。** 日期窗口那半截（`:1170-1174`）要的是序比较，
   而 `RowFilterOp = Literal["contains","equals","not_equals","in","prefix"]` 没有序比较算子，
   所以它记 `comparison_row_filter → rows.filter_comparison`（`_engine/spec.py:40`，`absent`）。
   两个 blocker 都是 absent ⇒ `expressible_today` 回 `False`。

最终面：need 集 `["csv_decoder","series_pivot","comparison_row_filter","param_driven_column_selection"]`，
`expressible_today: false`，`census-sec-tmx-fed-gov-finra.json` 76676→77632 字节。
复算读数：`rows.filter_comparison` 的行数 11→12、`columns.response_dependent_selection` 9→10。
判定过程与逐行理由见 `docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/开发验收记录.md` §47。
本回执正文一字未删，留作「代理回执是线索不是结论」的样本。
