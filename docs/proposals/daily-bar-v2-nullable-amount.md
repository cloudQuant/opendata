# Daily bar v2: versioned nullable turnover contract

**Status:** design proposal for review; implementation has not started.
**Date:** 2026-10-27
**Scope:** Sina AkShare futures, options, and convertible-bond daily bars whose recorded source rows do not contain turnover.

## Decision proposed

Keep the existing `Bar` contract and all current v1 domains unchanged. Add a separate, versioned daily-bar contract for the affected domains. In that contract, `amount` is a required field whose value may be `null`:

- `amount: 0` means the source reported an actual zero.
- `amount: null` means the source did not report a turnover observation.
- Every v2 output row always contains the `amount` key. A versioned source contract may explicitly declare that its upstream schema has no turnover column; for that declared source shape the adapter emits `amount: null`. An undeclared missing column is source-schema drift and fails closed. An upstream key whose value is explicitly null also maps to null.

Do not estimate turnover from price, volume, contract multipliers, or another provider. Do not fill a missing amount from another source. Preserve OHLC and volume as delivered by the selected source, with source identity and source-specific unit semantics available to consumers.

This is additive versioning. It does **not** make `Bar.amount` nullable, change the v1 REST response, alter an existing DWD table, or rewrite an existing migration. It also does not repair the known v1 false zeros or existing query results; those remain explicit follow-up work.

## Evidence from saved source fixtures

The recorded Sina fixtures show that turnover is absent from the source schemas for all three affected adapters. This is evidence about these captured responses and symbols, not a claim that every Sina response or future endpoint version has the same schema.

| Source function | Captured request | Rows | Recorded fields | Evidence |
|---|---|---:|---|---|
| `futures_zh_daily_sina` | `symbol=RB0` | 4,251 | `date, open, high, low, close, volume, hold, settle` | [`meta.json`](../../tests/fixtures/upstream/futures_daily_sina/meta.json), [`reference.csv.gz`](../../tests/fixtures/upstream/futures_daily_sina/reference.csv.gz), [`responses.json.gz`](../../tests/fixtures/upstream/futures_daily_sina/responses.json.gz) |
| `option_sse_daily_sina` | `symbol=10003889` | 23 | `日期, 开盘, 最高, 最低, 收盘, 成交量` | [`meta.json`](../../tests/fixtures/upstream/option_daily_sina/meta.json), [`reference.csv.gz`](../../tests/fixtures/upstream/option_daily_sina/reference.csv.gz), [`responses.json.gz`](../../tests/fixtures/upstream/option_daily_sina/responses.json.gz) |
| `bond_zh_hs_cov_daily` | `symbol=sh010107` | 4,806 | `date, open, high, low, close, volume` | [`meta.json`](../../tests/fixtures/upstream/bond_daily_sina/meta.json), [`reference.csv.gz`](../../tests/fixtures/upstream/bond_daily_sina/reference.csv.gz), [`responses.json.gz`](../../tests/fixtures/upstream/bond_daily_sina/responses.json.gz) |

All three fixture metadata files record capture date `2026-09-24` and upstream commit `c4f6a631c259783dbc2507b6b27d179b3e88079d`. Their reference CSV headers contain no amount/turnover column. The saved response transcripts are the raw-response evidence; the reference CSVs are the normalized source rows.

The wider C24 source comparison independently found amount missing from all 15 AkShare futures rows and all 15 AkShare option rows in its 22-day comparison window. It also found the option volume values were not comparable with the THS leg (relative difference about 0.9999); the futures volumes were close in that sample. See [`C24 source comparison`](../evidence/C24/akshare-fallback-cross-check.txt) and [`C24 review`](../evidence/C24/README.md). The bond fixture establishes its missing field shape, but C24 did not establish cross-provider bond equivalence. None of these observations justifies promoting AkShare to automatic failover.

## Current behavior and unresolved data quality

The current `Bar` in `opendata/data/models/market.py` requires numeric `amount`. The Sina adapters in:

- `opendata/data/providers/akshare/models/futures_daily.py`
- `opendata/data/providers/akshare/models/option_daily.py`
- `opendata/data/providers/akshare/models/bond_daily.py`

currently construct rows with `amount=0.0` despite the recorded source frames having no amount field. This document does not change those adapters or the existing v1 route. They can therefore continue to produce the known false zero until the later cutover/remediation stage is approved and implemented.

The THS `normalize_bars` path in `opendata/data/providers/ths/endpoints.py` currently rejects `turnover: null` because it converts the value to `float`; that remains the v1 behavior. A future v2 normalizer may preserve an explicitly returned null, while continuing to reject a missing `turnover` key as source-shape drift.

The v1 query API can also return previously stored DWD rows. No warehouse was inspected for this proposal. If old `source=akshare` rows were persisted, their fabricated zeros may still be returned by v1 queries. This proposal does not inspect, clean, hide, convert, or otherwise repair such rows.

## Version and storage boundary

Use a new contract class such as `BarV2` (final name subject to review), with these fields: `symbol`, `trade_date`, `open`, `high`, `low`, `close`, `volume`, and `amount`. Keep the v1 `Bar` type and its annotations byte-for-byte unchanged. In v2, `amount` is required but nullable (`float | None` with no default); `volume` is retained as the source-provided numeric value without an unverified unit conversion. Each serialized row must carry the amount key with either a number or explicit `null`.

Give the new products separate domain identities, for example `futures_daily_v2`, `option_daily_v2`, and `bond_daily_v2`. They must resolve to separate DWD/ODS table names and a clearly versioned API surface such as `/api/v2/data/{asset_class}/{domain-alias}`. The existing `/api/v1/data/...` routes continue to address the v1 domains. The current domain resolver only understands one version of a `rest_path`; v2 therefore needs explicit API-version metadata and version-aware alias resolution rather than overlapping aliases or silently changing the existing domain's contract.

The current `/api/v2` router serves published datasets and has no `/data/...` route. A v2 data route is an additive API surface, but its response schema must be documented explicitly: `amount` is present and has type `number | null`; `0` and `null` have the meanings above. The current generic `APIResponse.data` shape does not by itself communicate this row-level contract.

Keep v2 source semantics conservative:

- Sina v2 adapters preserve its source-provided OHLC and volume values. For the recorded Sina schema, they emit `amount: null` because the known source contract has no amount field. If a later recorded source version adds the field, accept only a valid finite number or explicit null; reject malformed values.
- THS v2 accepts an explicit `turnover: null` as null, preserves numeric zero, and fails closed when the `turnover` key is absent or malformed.
- Do not compare or merge source-native volumes across providers until their units have been measured and declared compatible. The source/provenance value must remain available with each DWD result. An `auto` route may choose a whole source under the reviewed authority policy; it must never fill only the missing amount from a different source.
- Do not state that all v2 amount values are CNY or that all volume values are shares unless each source/domain's unit semantics have been confirmed. v1's existing unit contract is not broadened by this proposal.

## Two-stage landing plan

### Stage 1 — additive v2 path, isolated from v1

1. Freeze the v1 contract and current API behavior for compatibility. Do not change `Bar`, existing v1 domain registrations, v1 route behavior, or old Alembic revisions.
2. Add the new nullable contract and v2-specific domain identities, source capabilities, mappings, and adapters. Keep the Sina AkShare v1 adapters and THS v1 normalizer unchanged during this stage; this avoids a surprise break for current consumers. The known v1 false-zero defect remains open and must be called out in release notes/operations tracking.
3. Add an explicit v2 data route and version-aware route/domain resolution. Do not make v2 the v1 default and do not enable automatic cross-source substitution. Start with explicit-source access; automatic selection requires a separate source-authority and field-equivalence review.
4. Design and review a forward-only Alembic migration that creates **new** v2 storage tables with nullable amount columns. Do not alter existing v1 DWD tables and do not edit old migration revisions `0001` or `0006`. Do not copy v1 rows into v2 merely by changing their zero amount to null.
5. Validate the isolated v2 path against the saved fixtures and raw-response provenance before publishing it to consumers. The implementation must keep native OHLC/volume values unchanged, serialize absent amount as explicit null, retain actual zero, and reject unexpected schema drift.

### Stage 2 — consumer migration, coverage, and v1 contamination cleanup

1. After Stage 1 acceptance, populate v2 from retained raw source evidence or a new source fetch. Reconcile history by symbol and source business key, covering the actual available source history and interior gaps. Do not treat a table-wide maximum date or row count as proof of coverage.
2. Migrate consumers to v2 deliberately. AFI currently has an OpenData stock-only contract (`opendata.stock.daily.contract-v1`); it has no registered OpenData futures/option/bond clients. Add AFI v2 contracts only when those products are consumed. Mark turnover optional/nullable, preserve null through normalization, and make any strategy or metric requiring turnover treat null as unavailable rather than zero. The frontend `MarketHistoryRow.turnover` is already optional and nullable, but new API types and displays still need end-to-end review.
3. Monitor v2 null/zero rates, source provenance, failure rates, and per-symbol coverage. Enable `source=auto` only after source priority, volume semantics, and field-level equivalence have evidence. Never mix fields across sources to conceal missingness.
4. Only after consumer cutover, plan v1 retirement or a narrowly scoped compatibility guard. Stop new v1 writes that fabricate zero. Audit existing v1 rows with `source=akshare`; quarantine or retain them for audit, and rebuild only from source evidence. Do not rewrite those historical zeros to null without evidence tying each row to the old adapter behavior. Communicate any v1 query restriction before enabling it.

## Forward migration and rollback requirements

No database change or migration execution is part of this proposal. A future migration must be additive and create v2 tables; it must not modify `dwd_stock_daily` or existing v1 daily tables. Since old Alembic revisions call the DDL builder dynamically, leaving `Bar` unchanged is necessary to preserve their replayed `NOT NULL` amount semantics. New v2 DDL should be generated from the v2 model and pinned by a reviewed migration test.

Rollback must be possible without converting data: v2 routes can be disabled while v2 tables and raw evidence remain intact. Do not make v1 depend on v2 tables. Any table drop or v1 cleanup requires a separate retention decision and approved migration.

## Acceptance evidence required before Stage 1 release

- Contract/schema tests prove v1 `Bar.amount` remains required and non-null and v2 amount is required-but-nullable.
- Saved Sina fixtures prove each v2 adapter emits the exact OHLC/volume values and `amount: null`; test a valid source-reported zero and ensure it remains zero.
- THS v1 continues to reject null turnover; THS v2 maps explicit null to null, preserves zero, and still rejects an absent key and malformed values.
- The old `0001` and `0006` Alembic revisions replay with their original v1 DDL semantics; current DDL snapshots for the v1 Bar domains remain pinned and unchanged. New v2 DDL permits null only in the new tables. Old migration source files are not edited.
- API tests prove v1 routes retain their existing schema and v2 routes return explicit null without silently selecting another provider. The published OpenAPI contract must describe the v2 row shape.
- A later v2 backfill review proves per-symbol requested/observed bounds, interior gaps, source units, provenance, and null/zero counts before any AFI consumer cutover.

## Deferred items and explicit non-claims

- The existing v1 AkShare adapters still emit fabricated zero; the fix is **not implemented**.
- Existing v1 warehouse rows and query output have not been inspected or repaired; contaminated rows may still be served.
- The v2 contract, provider adapters, routes, DWD/ODS tables, migration, AFI client contracts, and frontend handling are **not implemented**.
- No claim is made that Sina is a safe automatic fallback. C24 measured futures and option mismatches; bond equivalence remains unmeasured.
- No live/upstream source fetch, database inspection, test-suite execution, or Git operation was performed for this document. I read only the already-saved fixture and C24 evidence cited above.

## Proposed implementation ownership

When implementation is separately authorized, keep work partitioned around these file families:

| Owner | Scope |
|---|---|
| OD contract/registry | New v2 model and exports; version-specific domain metadata and IDs; no edit to v1 `Bar` |
| OD providers | v2 Sina/THS adapters and mappings; v1 behavior unchanged until cutover approval |
| OD API/storage | v2 route/schema and isolated DWD/ODS table migration; do not edit or execute a database migration during design review |
| AFI consumer | New versioned derivatives provider contracts and null-aware downstream behavior only when v2 is ready for consumption |
| Integrator | Fixture-backed acceptance, history-coverage evidence, OpenAPI snapshot coordination, and cutover/contamination plan |

The existing published OpenAPI/coverage files are outside this proposal's ownership. Any eventual `/api/v2/data` OpenAPI additions should be coordinated with their current owner as a separate contract change.
