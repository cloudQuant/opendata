# C90 wave — intrinio 2 rows (CalendarIpo, HistoricalDividends)

Verdicts (each row decided from this tree + an offline test run + upstream contract cites; the two
rows landed on the same verdict but were checked independently):

- `OBB2-intrinio-CalendarIpo` — **VERDICT A** (flag was stale): declared as `specs.CALENDAR_IPO`
  with a `RecordRepointPlan`, offline tests prove flattened rows come out; flipped
  `expressible_today` to `true`, no label added or removed.
- `OBB2-intrinio-HistoricalDividends` — **VERDICT A** (flag was stale): declared as
  `specs.HISTORICAL_DIVIDENDS` with a `column_derives` `DerivePlan(source_kind="parameter")`,
  offline tests prove symbol-stamped rows come out; flipped `expressible_today` to `true`, no
  label added or removed.

## Face 1 — the test run (offline, no network, no DB, OPENDATA_ALLOW_LIVE_E2E never set)

Command:

```
cd /Users/yunjinqi/Documents/new_projects/opendata && PYTHONPATH=. /Users/yunjinqi/opt/anaconda3/bin/python -m pytest tests/test_intrinio_engine_provider.py -q --no-cov
```

rc = 0. Verbatim tail:

```
collected 23 items

tests/test_intrinio_engine_provider.py .......................           [100%]

============================== 23 passed in 0.37s ==============================
```

Subset over exactly the two model classes,
`... pytest tests/test_intrinio_engine_provider.py -q --no-cov -k "HistoricalDividends or CalendarIpo"`:
rc = 0, `11 passed, 12 deselected` (TestHistoricalDividends 5 tests, TestCalendarIpo 6 tests).
No test skipped: the suite runs against `FixedResponseTransport` fixtures, so nothing here is a
NOT_RUN. What the subset proves per row:

- `TestHistoricalDividends::test_the_feed_addresses_the_path_and_stamps_the_symbol` — 2 rows out,
  `[row.symbol for row in rows] == ["AAPL", "AAPL"]` (the parameter-valued column lands).
- `TestHistoricalDividends::test_the_three_published_keys_are_renamed...` — `date/dividend/
  dividend_currency` gone, `ex_dividend_date/amount/currency` out; `test_the_symbol_is_not_also_sent...`
  asserts `page_size == "100"` on the wire (the alias leg).
- `TestCalendarIpo::test_the_calendar_addresses_the_endpoint_and_aliases_the_query` — wire params
  `ticker/offer_amount_greater_than/offer_amount_less_than/page_size`, 2 rows out.
- `TestCalendarIpo::test_each_row_carries_the_flattened_contract` — 35 columns, nested
  `company`/`security` objects projected flat and gone (`company_name`, `security_composite_ticker`
  present).

## Face 2 — the declarations (carrier check, not trust)

- `opendata/data/providers/intrinio/specs.py`: `CALENDAR_IPO` (l.311) declares
  `row_expansion=(RecordRepointPlan(...))` over the 35 `(column, kind, record-pointer)` triples of
  `CALENDAR_IPO_FIELDS` (l.271), with dotted pointers `company.*`/`security.*`; `HISTORICAL_DIVIDENDS`
  (l.171) declares `column_derives=(DerivePlan(column="symbol", source_kind="parameter",
  source="symbol"),)` (l.219-228). Bindings: `models/calendar_ipo.py:16` and
  `models/historical_dividends.py:15` both build fetchers via `make_http_json_fetcher`.
- Carriers verified to exist and be routed in the engine (read-only):
  `spec.py:486` `row_expansion` / `spec.py:489` `column_derives` fields, `spec.py:728` validates a
  parameter-source against declared params; `value_transform.py:569` `DerivePlan` accepts
  `source_kind="parameter"` (l.603); `http_json.py:468` routes `source_kind == "parameter"` per row;
  `row_expansion.py:346` `RecordRepointPlan` / `:196` `FieldMapping`, applied at `http_json.py:386+`.
- `specs.NOT_DECLARABLE` is empty (specs.py:399) — the row's old "withheld" note text was stale.
- Labels are inside `LABEL_TO_CAPABILITY` (`scripts/quality/model_capability_census.py`):
  `per_record_nested_flatten→path.explode` (:135), `derived_columns→columns.derive` (:190),
  `field_rename_map→columns.rename` (:201), `param_value_mapping→param.value_mapping` (:206).
  `load_rows` re-imported cleanly after the edits (see Face 4).

## Face 3 — upstream contract comparison (cites only; no upstream code read for implementation)

CalendarIpo (`.../openbb_intrinio/models/calendar_ipo.py`):
- :25-30 `__alias_dict__` — symbol→ticker, limit→page_size, min_value→offer_amount_greater_than,
  max_value→offer_amount_less_than ⇒ `param_value_mapping` (verified by reading those lines).
- :149-155 — `company: IntrinioCompany | None` / `security: IntrinioSecurity | None` nested objects
  ⇒ `per_record_nested_flatten`. :183 — rows under `initial_public_offerings` (evidence line).

HistoricalDividends (`.../openbb_intrinio/models/historical_dividends.py`):
- :36-40 alias dict ex_dividend_date←date, amount←dividend, currency←dividend_currency ⇒
  `field_rename_map`.
- :25-30 `__alias_dict__ = {"limit": "page_size"}` with default 100 ⇒ `param_value_mapping`.
- :102-104 `[{"symbol": query.symbol, **item} for item in data]` ⇒ `derived_columns`.
- :95 `get_data_many(url, "stock_price_adjustments")`, :92 symbol excluded from querystring —
  matches the declaration's path placeholder + rows_pointer (evidence line).

No label was misattributed on either row, so none was removed; no blocker remained, so nothing was
appended.

## Face 4 — JSON before/after + reload proof

Before (verbatim fields, `census-intrinio-1of3.json`, CalendarIpo):
`"expressible_today": false`, notes ended `"...no next-page loop, page_size only caps size."`
After: `"expressible_today": true`; notes now name the upstream line each label comes from
(:149-155 for the flatten via `RecordRepointPlan`, :25-30 for the aliases) and cite
`tests/test_intrinio_engine_provider.py::TestCalendarIpo`; `evidence` unchanged
(`providers/intrinio/openbb_intrinio/models/calendar_ipo.py:183`).

Before (`census-intrinio-2of3.json`, HistoricalDividends): `"expressible_today": false`, notes ended
`"...the engine has no carrier for a parameter-valued column, so the model is withheld in
specs.NOT_DECLARABLE rather than published with a null symbol."`
After: `"expressible_today": true`; notes name :36-40 (field_rename_map), :25-30
(param_value_mapping), :102-104 (derived_columns, carried by `column_derives`) and cite
`TestHistoricalDividends`; `evidence` unchanged (`.../historical_dividends.py:95`). All other fields
untouched in both rows.

Reload proof command (exact):

```
PYTHONPATH=. /Users/yunjinqi/opt/anaconda3/bin/python -c "import importlib.util,sys,pathlib; p=pathlib.Path('scripts/quality/model_capability_census.py'); s=importlib.util.spec_from_file_location('mcc',p); m=importlib.util.module_from_spec(s); sys.modules['mcc']=m; s.loader.exec_module(m); rows=m.load_rows(pathlib.Path('docs/迭代计划/迭代2-统一Provider架构与全量能力补齐')); print('TOTAL', len(rows)); [print(r.task_id, r.expressible_today, sorted(r.needs)) for r in rows if r.task_id in ('OBB2-intrinio-CalendarIpo','OBB2-intrinio-HistoricalDividends')]"
```

rc = 0. Verbatim output:

```
TOTAL 279
OBB2-intrinio-CalendarIpo True ['param.value_mapping', 'path.explode']
OBB2-intrinio-HistoricalDividends True ['columns.derive', 'columns.rename', 'param.value_mapping']
```

No `.py` file was modified, so ruff/mypy were not required and not run.

## What remains unverified

- The real endpoint's wire behavior: no body of `/companies/ipos` or
  `/securities/{symbol}/prices/adjustments` has ever been captured in this repo, so the offline
  witnesses certify the shape contract, not live data. In particular whether every adjustment kind
  publishes a `dividend` value (split-only rows) is unverified, and specs.py records that reading.
- Upstream's 1-year relative default window (historical_dividends.py:74-78) and the case-folded
  symbol validator have no declaration carrier; both divergences are recorded in the spec notes,
  and the census notes now state the window omission for HistoricalDividends.
- `CalendarIpoRow` is synthesized by `build_row_model` because domain `intrinio_calendar_ipo` is
  not in `domains.yaml` (packages may not edit it here); a published contract class is pending.
- The remaining greedy-cover false rows outside these two are other packages' work.


---

## 复核结论（主代理 C91，2026-10-11）— **两行里一行站住（带一条证据路径更正）、一行被 revert**

- `OBB2-intrinio-CalendarIpo` — **Verdict A 站住**（`expressible_today: true` 保留）。
  但**它成立的原因不是本行记的那个 label**：`param_value_mapping` 的 canonical 是
  `param.value_mapping`，锚 `ParamSpec.value_map`（`_engine/spec.py:127`），本轮 AST 走查全生产树
  **0 处声明**（仍在 `built_but_unused` 榜上）；`CALENDAR_IPO` 的 7 个 `ParamSpec` 里是
  **4 个带 `query_key`（`ticker`/`offer_amount_greater_than`/`offer_amount_less_than`/`page_size`）、
  0 个带 `value_map`**。别名机制本行就在用、引擎也支持，所以判定不改，但 need label 指错了字段。
  仓里没有任何 label 命名「调用方名↔上游名别名」（任务 #86），本轮不动词汇表。
- `OBB2-intrinio-HistoricalDividends` — **Verdict A 被反驳，`expressible_today` 已 revert 回 `false`。**
  上游在请求时按今天算一年窗口（`historical_dividends.py:74-78` 的 `transform_query`），
  而 `ParamSpec.default` 只装字面量（锚 `_engine/spec.py:125 'default: Any = None'`）：
  声明发不出这个边界，只会把答案放宽成「无边界」。本轮新增 absent canonical
  `param.relative_default` 承载它，need 集变
  `["field_rename_map","param_value_mapping","derived_columns","relative_date_default"]`，
  `census-intrinio-2of3.json` 15101→15596 字节。该行距「就绪」只差这一个接缝，不是一个都不是。

逐行理由与全部复算读数见 `开发验收记录.md` §47。本回执正文一字未删。
