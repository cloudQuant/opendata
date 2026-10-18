# Wave 33 — `implement_missing_model` (19) + `new_provider_package` (2): per-row scoping note

Carrier: `docs/evidence/C138/wave33-implement-missing-model-20261018/notes/per_row_scoping.md`
Measurement-only. Nothing under `opendata/**` or `scripts/quality/**` was touched; no claim,
receipt, judge output or gate was written by this note (scratch lives in `/tmp/w33scope/`).

Repo state at measurement: branch `dev`, HEAD `ad7aa2a2c`, identity_set `5168e1e723999312`.

## 0. Populations and denominators (printed, never assumed)

| face | reading |
|---|---|
| shipped action census (`C128/plane1-residue-readiness-20261017/probes/residue_axis_census.py`) re-run this session | `residue=33` of `report_rows=350`, actions `{implement_missing_model: 19, ledger_ruled_out: 11, new_provider_package: 2, shard_entry_needs_harness: 1}`, nested_sum=33, identity_set=5168e1e723999312, `arms=8 arms_passed=7 arms_failed=1` (failing arm `B_landed_negative`, the known `yfinance::BalanceSheet` blind spot — exit 1), `F_witnessable_today count=0` |
| my per-file radius instrument (`/tmp/w33scope/radius.py`, imports the wave-31 step-7 pin walker `landing_set_blast_radius` + `landing_target_radius`) | `DENOM accepted=317/350 accepted_claims=1585 pinned_paths=629` |
| control on that instrument | `CONTROL_fmp files=26 pinned=23 max=69 PASS=True` — reproduces `landing_target_radius.txt` `D_control_is_not_free` exactly, so the pin walker is live and not a dead predicate |
| inventory ledger columns for the 21 rows (`模型级任务清单.csv`, `csv.DictReader`) | `DENOM inventory_rows=350`, `MATCHED=21 of expected 21` |
| `opendata/data/domains.yaml` | `indent-2 domain keys = 338` over 4440 lines; `tmx_* = 8` (exactly the 8 accepted tmx rows), `yfinance-adjacent = 27`, no `government_us_treasury_prices` |

Correction to the brief's premise, measured: the user's note said "only `tmx_index_constituents`
and `government_us_treasury_auctions` are present from that neighbourhood". Actual: **8** `tmx_*`
keys are present, one per landed row (`tmx_company_filings`, `tmx_equity_historical`,
`tmx_equity_info`, `tmx_equity_quote`, `tmx_equity_search`, `tmx_historical_dividends`,
`tmx_index_constituents`, `tmx_price_target_consensus`). The conclusion still holds — none of the
16 target rows has a key — but the pattern is stronger than stated: **every accepted row owns
exactly one domain key**, so 17 new keys are required for the 17 rows this wave can serve
(16 tmx + 1 government_us), and the landed keys are the template.

## 1. The 21 rows

Cost classes below are the *implementation* cost, not the re-sign cost (see §2).
"upstream" = `/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers/…`
(path verified to exist; read as an output-contract reference only — no code copied).
The ledger's `upstream_shape_file` for every one of the 21 rows points at the provider's
`__init__.py` registry line, not at a spec file, so the contract had to be read from
`models/<file>.py` per row; all 21 target model files exist upstream (tmx registry
`fetcher_dict` = 24 entries, matching tmx's 24 inventory rows).

`scripts/quality/upstream_model_specs.py` (950 lines) *does* implement the shipped per-model
field-list extraction the brief hoped for, pinned to upstream commit `3e071fcc2cd9`. It was not
run here (it `subprocess`-walks the upstream tree and writes a spec sheet); **no generated spec
sheet exists in the repo** — `find docs -name '*source_review*' -o -name '*upstream*json*'`
returned nothing outside C100, and `docs/evidence/C101/` holds only `claims*.json` + `README.md`.
Finding, not workaround: the field lists in this table are read from the upstream declaration
files directly, at medium confidence, and a pre-wave step should run
`upstream_model_specs.py --output /tmp/…` to make them machine-checkable.

| row | upstream contract | what exists in-repo | files to change | tree-wide among those | endpoints | shape | SDK-dependent? | cost | conf |
|---|---|---|---|---|---|---|---|---|---|
| tmx::CalendarEarnings | yes, `tmx/…/models/calendar_earnings.py` (147 l) | `_OPERATIONS` gql client + `_TypedFetcher` + 8 row classes | `client.py`(+1 op), `fetchers.py`, `models/tmx.py`, `provider.py`, `models/__init__.py`, `iter2_witnesses/tmx.py`, `domains.yaml` | **domains.yaml only** | 1 (app-money GraphQL) | frame | no | **S** | high |
| tmx::CompanyNews | yes, `company_news.py` (136 l) | same GraphQL seam; op not present | same 7 | domains.yaml | 1 | frame | no | **S** | high |
| tmx::EquityGainers | yes, `gainers.py` (174 l) | same | same 7 | domains.yaml | 1 | frame | no | **S** | high |
| tmx::InsiderTrading | yes, `insider_trading.py` (169 l) | same | same 7 | domains.yaml | 1 | frame | no | **S** | high |
| tmx::EtfHistorical | **no own file** — `tmx/__init__.py:58` maps it to `TmxEquityHistoricalFetcher` | `getCompanyPriceHistory` op **already shipped**, `EquityHistoricalFetcher` landed | `fetchers.py`, `models/tmx.py`, `provider.py`, `models/__init__.py`, witness, `domains.yaml` (client.py untouched) | domains.yaml | 1 (reuse) | frame | no | **S** | high |
| tmx::EtfCountries | yes, `etf_countries.py` (101 l) | nothing of the ETF universe | **new loader in `client.py`** (cloudfront JSON GET) + `fetchers.py` ×5 + `models/tmx.py` ×5 + provider + witness ×5 + `domains.yaml` ×5 | domains.yaml | 1 (shared `etfs.json`) | frame | no | **M** | med |
| tmx::EtfSectors | yes, `etf_sectors.py` (99 l) | as above | as above (shares the loader) | domains.yaml | 1 (shared) | frame | no | **M** | med |
| tmx::EtfInfo | yes, `etf_info.py` (239 l) | as above | as above (shares the loader) | domains.yaml | 1 (shared) | frame | no | **M** | med |
| tmx::EtfHoldings | yes, `etf_holdings.py` (146 l) | as above | as above (shares the loader; verify it does not need a per-ETF page) | domains.yaml | 1 (shared) | frame | no | **M** | med-low |
| tmx::EtfSearch | yes, `etf_search.py` (281 l) | as above | as above (shares the loader) | domains.yaml | 1 (shared) | frame | no | **M** | med |
| tmx::AvailableIndices | yes, `available_indices.py` (134 l) | no `tmxinfoservices` grant anywhere | `client.py` new host+loader, `fetchers.py`, `models/tmx.py`, provider, witness, `domains.yaml` | domains.yaml | 1 static JSON (+1 `SQLiteBackend` cache leg upstream) | frame | no (but upstream uses `aiohttp_client_cache`, not serveable here) | **M** | med |
| tmx::IndexSectors | yes, `index_sectors.py` (92 l) | same new host | same (shares AvailableIndices' loader) | domains.yaml | 1 (shared) | frame | no | **M** | med |
| tmx::BondPrices | yes, `bond_prices.py` (218 l) | no `iiroc.ca` grant | `client.py` new host loader, `fetchers.py`, `models/tmx.py`, provider, witness, `domains.yaml` | domains.yaml | 1 (`…/debtip/designatedbonds/list`) | frame, **70–100 K records upstream** vs shipped `MAX_RESPONSE_ROWS=10_000` | no | **M** | med |
| tmx::TreasuryPrices | yes, `treasury_prices.py` (161 l) | same bonds loader | same as BondPrices (one loader, two rows) | domains.yaml | 1 (shared) | frame | no | **M** | med |
| tmx::IndexSnapshots | yes, `index_snapshots.py` (288 l) | needs **all three** families at once: `get_data_from_gql` + `get_data_from_url` + `get_indices_backend` + a `money.tmx.com/en/quote` page | client (×3 transports), fetchers, models, provider, witness, domains | domains.yaml | 3–4 | frame | no, but multi-host + page scrape | **L** | med |
| tmx::OptionsChains | yes, `options_chains.py` (115 l) | no `m-x.ca` grant; no HTML/CSV seam in this provider | new `read_html` + EOD `read_csv` path (binary/table, not a JSON envelope) | domains.yaml | 2 (m-x.ca quotes page + EOD chain file) | frame from HTML table | no SDK, but **not an envelope shape** | **L** | med |
| yfinance::AvailableIndices | yes, `yfinance/…/models/available_indices.py` | 26 accepted rows, `_sdk.py`/`_session.py`/`_budget.py` all radius 26; row not bound in `provider.py` | new fetcher, `models/yfinance_iter2.py`, `provider.py`, `iter2_witnesses/_parts/yfinance/*` | domains.yaml (candidate in ledger: `stock_daily_overseas`) | **0 network endpoints** — upstream builds the frame from the constant `INDICES` dict (`utils/references.py:52`) | frame→records | no | **M** (harness-shape risk) | low |
| yfinance::FuturesCurve | yes, `futures_curve.py` | as above | `_sdk.py`/`_session.py` (radius 26) + fetcher + provider + witness | domains.yaml | multi-leg (symbol resolution + per-expiry chains) | curve (melted frame) | **yes — yfinance SDK session seam** | **L** | med |
| government_us::TreasuryPrices | yes, `government_us/…/models/treasury_prices.py` (161 l) | **`TreasuryPrice` contract class already exists** (`models/government_us.py:155`); `_client.py` grants only `api.fiscaldata.treasury.gov`; row unbound in `provider.py` | new `models/treasury_prices.py`, `models/government_us.py`, `provider.py`, `_client.py` (new host `treasurydirect.gov`), `iter2_witnesses/government_us.py`, `domains.yaml` | domains.yaml | 1 (`/GA-FI/FedInvest/securityPriceDetail`, POST-form + Referer/Origin headers) | frame | no | **M** | med-high |
| biztoc::WorldNews | yes, `biztoc/…/models/world_news.py` | **no package** (`globbed=0`); `catalog.py` holds `_reserved_provider("biztoc", "api_key")` | whole `providers/biztoc/**`, `models/biztoc.py`, `iter2_witnesses/biztoc.py`, **`catalog.py`** | domains.yaml **+ catalog.py** | 2 (news + `/sources`) | frame | needs RapidAPI key (`biztoc.p.rapidapi.com`) | **L** | med |
| stockgrid::ShortVolume | yes, `stockgrid/…/models/short_volume.py` | **no package** (`globbed=0`); `catalog.py` holds `_reserved_provider("stockgrid")` | whole `providers/stockgrid/**`, `models/stockgrid.py`, witness module, **`catalog.py`** | domains.yaml **+ catalog.py** | 1 (`/get_dark_pool_individual_data?ticker=`, app-internal endpoint) | frame | no | **L** | low |

`globbed=0` for biztoc/stockgrid is a **vacuous zero, not a free one** (the same failure shape
wave-31 step-7 documented for yfinance): the packages do not exist, so 0 accepted rows can pin
them; the real price is a whole provider package plus a `catalog.py` edit that is pinned by
`317/317` accepted rows across `28/32` providers.

## 2. Blast radius, measured (files this wave must change)

| file | rows pinned | classification |
|---|---|---|
| `opendata/data/domains.yaml` | **317/317** (`28/32` providers) | tree-wide |
| `opendata/data/providers/catalog.py` | **317/317** | tree-wide |
| `scripts/quality/iter2_claims_fixtures.py` | **317/317** | tree-wide — **not required by any of the 21 rows** (it is the loader; per-model entries live in the provider's witness module) |
| `scripts/quality/iter2_claims_probe_runner.py` | **317/317** | tree-wide — **not required** |
| `opendata/data/http_client.py` | 18 | shared, avoid |
| `opendata/data/request_budget.py` | 1 | shared, avoid |
| `opendata/data/providers/tmx/models/{client,fetchers}.py`, `tmx/provider.py`, `models/tmx.py`, `iter2_witnesses/tmx.py` | **8** each | provider-local (tmx's own accepted rows) |
| `opendata/data/providers/tmx/__init__.py`, `tmx/models/__init__.py` | 0 | free |
| `opendata/data/providers/yfinance/models/{_sdk,_session}.py`, `yfinance/provider.py`, `models/yfinance_iter2.py`, `iter2_witnesses/yfinance.py`, `_parts/yfinance/rows_w32_*.py` | **26** each | provider-local but the largest local price in scope |
| `opendata/data/providers/yfinance/models/{market_iter2}.py` | 22 | provider-local |
| `opendata/data/providers/government_us/models/_client.py`, `government_us/provider.py`, `registration.py`, `models/government_us.py`, `iter2_witnesses/government_us.py` | **5** each | provider-local |
| `opendata/data/providers/government_us/models/<row>.py` | 1 each | per-row free (new file = 0) |
| fmp control | max 69 | (proves the walker returns non-zero) |

Consequence for wave economics: for the 14–15 rows this wave can actually serve, **the only
tree-wide file that must change is `domains.yaml`** — tmx and government_us are already in
`catalog.py`, the claims loader needs no per-row edit, and witnesses are one-provider modules.
So the smallest tree-wide edit set that covers the batch is **one file, 17 keys**, i.e.
**exactly one 20-minute re-sign chain for the whole wave**, and the per-row marginal cost is the
provider-local radius (tmx 8, government_us 5), not 317.

## 3. Recommended wave split

Batch: **15 rows** (13 tmx + government_us::TreasuryPrices + tmx::EtfHistorical is inside the 13).
One `domains.yaml` edit of 17 keys, one re-sign chain, expected plane-1 `317 → 332` (ceiling 339
per the ledger; this wave leaves 7 residue rows: `ledger_ruled_out` 11 and
`shard_entry_needs_harness` 1 are out of plane, plus the 6 vetoed here).

- **W1 — tmx GraphQL family (4 rows, one worker):** CalendarEarnings, CompanyNews, EquityGainers,
  InsiderTrading. One `_OPERATIONS` block edit + 4 fetcher classes over the shipped
  `_TypedFetcher`/`graphql()`/`list_rows` path. Shared file set: `client.py`, `fetchers.py`,
  `models/tmx.py`, `provider.py`, `iter2_witnesses/tmx.py` — must be one owner (radius 8 each).
- **W2 — tmx ETF universe (5 rows, one worker):** EtfCountries, EtfSectors, EtfInfo, EtfHoldings,
  EtfSearch. All 5 ride **one** new loader (`cloudfront …/etfs/etfs.json`, single GET, hand-authored
  body). Do not split: splitting multiplies `client.py` edits with no endpoint saving.
- **W3 — tmx bonds + indices (4 rows, one worker):** BondPrices, TreasuryPrices (one
  `iiroc.ca` loader) and AvailableIndices, IndexSectors (one `tmxinfoservices` loader). Two new
  host grants, four rows. Constraint to design first: the shipped `MAX_RESPONSE_ROWS = 10_000`
  versus a 70–100 K upstream list — filter/limit before frame construction.
- **W4 — tmx::EtfHistorical + government_us::TreasuryPrices (2 rows, one worker):** different
  providers, but each is a single file-owning landing and neither shares a file with W1–W3 beyond
  `domains.yaml`. EtfHistorical needs no `client.py` change (op already shipped).
  If a fifth worker is available, prefer splitting these into two owners: tmx `fetchers.py` is
  also edited by W1–W3, so EtfHistorical's `fetchers.py` edit **conflicts** with W1/W2/W3 —
  safer to fold EtfHistorical into W1 and leave government_us alone as W4.
- **No worker touches** `catalog.py`, `iter2_claims_fixtures.py`, `iter2_claims_probe_runner.py`,
  `http_client.py`, `request_budget.py`, or `_engine/**`.

Because all four workers edit tmx's `fetchers.py`/`models/tmx.py`/`provider.py`/witness module,
this is **not** a 4-way parallel batch for tmx: sequence W1→W2→W3 on the shared files, or give
one worker tmx (13 rows, 4 loaders) and a second worker government_us. The honest read is
**2 owners**, not 4, for the tmx side.

## 4. Do not attempt this wave (6 rows)

1. **tmx::OptionsChains** — L. Measured: transport is `get_current_options` (`read_html`) +
   `download_eod_chains` (`read_csv`) against `www.m-x.ca`, i.e. HTML-table and file download,
   which the `client_seam` GraphQL-envelope witness (`{"data": {root: …}}`) cannot express.
2. **tmx::IndexSnapshots** — L. Measured: the model needs `get_data_from_gql` **and**
   `get_data_from_url` **and** `get_indices_backend` **and** a `money.tmx.com/en/quote` page —
   3–4 legs across 3 hosts; only one of them exists in-repo.
3. **yfinance::FuturesCurve** — L. SDK-shaped (`get_futures_curve` → symbol resolution +
   per-expiry chains, `DataFrame` melt) and every candidate file in that package is radius **26**
   (`_sdk.py`, `_session.py`, `provider.py`, `models/yfinance_iter2.py`, `iter2_witnesses/*`).
   Re-opening the wave-32 SDK seam for one row costs 26 rows of re-sign for the largest local
   radius in scope.
4. **yfinance::AvailableIndices** — feasibility not established, and it is the *cheapest* row in
   the batch (0 endpoints: upstream transposes the constant `INDICES` dict). The blocker is not
   code, it is the acceptance plane: wave-32 `E_seam_kinds` measured all **317** accepted rows in
   exactly three kinds (`shared_http_client` 162, `engine_transport` 129,
   `governed_sdk_session` 26) — every kind is a transport leg, and the census prints this row as
   `seam=SEAM_NEVER_USED mechanism=engine_transport`, `fake_ok=False`. A zero-leg carrier has no
   witnessable precedent. Price it with a one-row probe before assigning it, not in this wave.
5. **biztoc::WorldNews** — L. `NO_PACKAGE_ON_DISK` (`globbed=0`), so it is a full provider landing,
   and `catalog.py` (317/317) must change; upstream host is `biztoc.p.rapidapi.com` with ledger
   `credential_fields=api_key`, `rights_status=NOT_REGISTERED`.
6. **stockgrid::ShortVolume** — L. Same package-less shape (`globbed=0`, `catalog.py` tree-wide),
   and the only upstream leg is the app-internal `get_dark_pool_individual_data?ticker=` endpoint;
   `rights_status=NOT_REGISTERED`. Low confidence even ignoring cost.

Also out of plane, unchanged from the census: `yfinance::KeyMetrics`
(`shard_entry_needs_harness`, blocked by SDK 0.2.66 having no valuation-measures surface —
`H_keymetrics class_has_get_valuation_measures=False`) and the 11 `ledger_ruled_out` rows.

## 5. Measured vs inferred

Measured (run or read this session, reproducible):
- residue 33 / actions {19,11,2,1} / identity_set 5168e1e723999312 — census re-run, exit 1 with
  `arms_failed=1` (`B_landed_negative`), face in `/tmp/w33scope/census.txt`
- accepted 317/350, accepted_claims 1585, pinned_paths 629; per-file radii in §2 (fmp control
  max=69 PASS=True reproduces the shipped face)
- tree-wide radii 317/317 over 28/32 providers for all four named tree-wide files
- domains.yaml: 338 keys / 4440 lines / 8 `tmx_*` / no key for any target row
- 21/21 upstream model files located and sized; tmx `fetcher_dict` = 24 entries;
  `EtfHistorical → TmxEquityHistoricalFetcher` at `tmx/__init__.py:58`
- per-row upstream transport family (grep of the 16 tmx models + `utils/helpers.py` bodies)
- hosts: `app-money.tmx.com/graphql` is the only tmx host in-repo; `iiroc.ca`, `cloudfront`,
  `tmxinfoservices`, `m-x.ca`, `tsx.com`, `treasurydirect.gov` appear in **no** repo file
- `TreasuryPrice` already declared at `opendata/data/models/government_us.py:155`
- yfinance landed surface: 26 bound models in `provider.py`, no AvailableIndices/FuturesCurve
- `biztoc`/`stockgrid` present in `catalog.py` only as `_reserved_provider(...)`
- `upstream_model_specs.py` = 950 lines, pinned upstream commit `3e071fcc2cd9`; **no generated
  spec sheet exists in the repo** (find over `docs` returned nothing outside C100)
- inventory: 350 rows; for all 21 rows `live_target=SOURCE_VERIFIED_WHEN_AUTHORIZED`,
  `contract_family_status=NOT_ASSESSED`, `credential_requirement_status=NOT_ASSESSED`,
  `existing_domain_candidates` empty except the 2 yfinance rows (`stock_daily_overseas`)

Inferred (re-check these):
- the S/M/L classes are *my* reading of "one loader × N rows × edits-per-row", not a shipped
  number; the only shipped cost face is wave-31 step-7's radius buckets
- `MAX_RESPONSE_ROWS=10_000` **conflicts with** the bonds list: the 70–100 K figure is upstream's
  own docstring, not a measured response size
- "17 new domains.yaml keys" assumes the landed convention (one key per accepted row) generalizes;
  measured on tmx (8/8) and government_us (5 keys for 5 rows), **not** on a row of this wave
- `expected 317 → 332` assumes all 15 rows pass the judge on first landing (wave 32 moved 26/26,
  but it had a new seam kind for it)
- OptionsChains/IndexSnapshots being unservable *by the current client seam* is inferred from the
  upstream transport functions; nobody ran a witness attempt. A new seam kind (like wave 32's
  `governed_sdk_session`) might serve them, at harness cost
- yfinance AvailableIndices "no zero-leg precedent" is inferred from `E_seam_kinds` covering
  317/317 accepted rows, which is a statement about landed rows, not a harness prohibition
- biztoc/stockgrid credential and rights friction is read from ledger columns marked
  `NOT_ASSESSED`/`NOT_REGISTERED` — absence of a decision, not a refusal
