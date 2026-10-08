# C70 — Engine capability backlog (offline read of the 44 hand-written fetchers)

Date: 2026-10-09. Branch: `dev`. HEAD: `2c21b68` (`fix(registry): C72 注册顺序改为派生，声明的
模型不可能再被漏掉`). The original read and the ranking pass were both taken at HEAD `3765250`
(`docs(evidence): C71 参数化契约套件的六腿反例`); four commits landed afterwards and moved the
citations (§0.3, §0.4, §0.5). An independent verifier re-checked every load-bearing claim against
`884f9bd` and §8 records the corrections that order produced. The tree then moved a **third** time
while §6–§8 were being written (`884f9bd` → `2c21b68`, plus an uncommitted in-flight diff on
`http_json.py` and `cboe/specs.py`), so §0.5 re-stamps the engine citations once more and records
the offset map for anyone reading a different snapshot.
Nothing was committed, pushed, fetched, or called over the network. No database, no credentials,
no provider HTTP. Only this file and `docs/evidence/C70/README.md` were written; no Python source,
test or migration was touched.

## 0. Inputs (exact, offline, reproducible)

### 0.1 Baseline of the tree at read time

```bash
git status --short
git rev-parse --abbrev-ref HEAD   # dev
git log --oneline -3
git diff --stat                   # in-flight, not mine, untouched
wc -l opendata/data/providers/_engine/spec.py \
      opendata/data/providers/_engine/http_json.py \
      opendata/data/providers/_engine/testing.py \
      tests/test_provider_model_contracts.py
ls docs/evidence/C70/
```

Measured (2026-10-09, this tree; the two right-hand columns are the original read and the
`884f9bd` re-verification of §0.4):

| item | value at the start of this run | value at the original write (`3765250`) | value at the `884f9bd` re-check |
| --- | --- | --- | --- |
| branch / HEAD | `dev` / `802ad6b` (parent `819410d fix(engine): C69 分页三处缺陷`) | `dev` / `3765250` (`802ad6b` → `9077dc7` → `3765250`) | `dev` / `884f9bd` (`3765250` → `6f12c4e` → `884f9bd`) |
| `_engine/spec.py` | 276 lines (HEAD's 262 + 14 uncommitted declaration-rule lines) | 300 lines (+24 uncommitted: `static_headers` — see §0.3) | 300 lines, all committed (`9077dc7` rules + `6f12c4e` `static_headers`) |
| `_engine/http_json.py` | 555 lines | 569 lines (+20 uncommitted: headers plumbed to the transport) | 569 lines, committed by `6f12c4e`; `fetch_pages` at `:402` |
| `_engine/testing.py` | 254 lines (uncommitted +123: `sample_value`, `synthetic_record`, `synthetic_page`, `valid_query_kwargs`, `SequencedResponseTransport`) | 270 lines (test transports now record `headers`) | 278 lines, committed; the shared recorder is `SyntheticTransport.record` at `:172` |
| dirty files | `_engine/spec.py`, `_engine/testing.py` (both in-flight, not mine); `tests/test_provider_model_contracts.py` untracked (712 lines) | `spec.py`, `http_json.py`, `testing.py`, `catalog.py`, `test_provider_model_contracts.py` (722, tracked since `9077dc7`); untracked `scripts/quality/declaration_provenance.py` | `catalog.py` + `tests/test_first_model_bindings.py` modified, untracked `opendata/data/providers/sec/` and this directory (all in-flight, not mine); contracts test 770 lines **tracked** (added by `9077dc7`, extended by `6f12c4e`); `declaration_provenance.py` 425 lines, committed by `884f9bd` |
| registered bindings | **46** | **46** (re-measured, unchanged) | **46** (re-measured at `884f9bd`, unchanged; the in-flight `sec` provider registers 0 fetchers) |
| engine-driven bindings (`model_spec is not None`) | **2** — both cboe: `AvailableIndices`, `IndexConstituents` | **2** (unchanged) | **2** (`catalog.engine_declared_models()`, added by `6f12c4e`, returns the same two) |
| hand-written fetchers | **44** | **44** — per-source: akshare 10, bls 2, ecb 6, fmp 2, fred 7, imf 3, oecd 2, ths 11, yfinance 1 | **44**, same per-source split (re-measured) |
| cboe upstream models recorded but not declared (`NOT_DECLARABLE`) | **9** (`opendata/data/providers/cboe/specs.py:110-122`) | **9** (unchanged) | **9** (re-measured; block is `:110-122`, file is 122 lines) |
| provider×model tasks in the ledger CSV | **350** (`docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv`) | **350** (re-measured) | **350** (re-measured) |
| ledger rows `implementation_task_status=IN_PROGRESS` | **13** | **13** (337 `NOT_RUN`) | **13** (337 `NOT_RUN`, re-measured) |
| ledger rows declaring `async_mode` | **2** (`OBB2-fred-SOFR`, `OBB2-fred-SONIA`); 348 `NOT_ASSESSED` | **2** (re-measured) | **2** (both `bounded_thread`; 348 `NOT_ASSESSED`, re-measured) |
| ledger rows with a `scenario` | **0 / 350** (column empty for every row) | **0 / 350** (re-measured) | **0 / 350** (re-measured) |
| ledger rows `live_verification_status != NOT_RUN` | **0 / 350** | **0 / 350** (re-measured) | **0 / 350** (re-measured; every row is `NOT_RUN`, so the "2 live-verified" figure some notes carry is the `async_mode` pair, not a live run) |
| fetchers whose `Capability.verified` is `True` | not measured at start | **23** (the 11 ths + 12 macro/overseas legacy rows); **23** `False` — see Appendix A | **23** `True` (ths 11, ecb 3, fred 3, imf 3, oecd 2, yfinance 1) and **21** `False` — the earlier "23 `False`" was wrong |
| fetchers with a non-null `canonical_model` | not measured at start | **13** of 46 — the remaining 33 publish only a domain, so the ledger's `upstream_model` cannot be joined to the runtime by name (§9) | **13** of 46 (bls 2, cboe 2, ecb 3, fmp 2, fred 4); 11 of the 44 hand-written ones |


Binding census command (read-only, real output below):

```bash
PYTHONPATH=. python3 - <<'PY'
from opendata.data.providers import catalog
rows = []
for p in catalog.list_providers():
    for f in catalog.fetchers_for(p.source):
        cls = type(f)
        rows.append((p.source, cls.__name__,
                     "ENGINE" if getattr(cls, "model_spec", None) is not None else "HAND"))
print("TOTAL", len(rows),
      "ENGINE", sum(k == "ENGINE" for _, _, k in rows),
      "HAND", sum(k == "HAND" for _, _, k in rows))
PY
```

```
TOTAL 46 ENGINE 2 HAND 44
per-source: akshare 10, bls 2, cboe 2, ecb 6, fmp 2, fred 7, imf 3, oecd 2, ths 11, yfinance 1
```

Files read in full: `_engine/spec.py`, `_engine/http_json.py`, `cboe/specs.py`, all ten
`providers/*/provider.py`, `provider.py`, `providers/catalog.py`. Every other provider module
(`fred/models/_paged.py` 182, `fred/models/_fixed_observation_pages.py` 649,
`ecb/models/_reference_rates.py` 825, `ecb/models/_series_sdmx.py` 473,
`bls/models/_client.py` 447, `ths/endpoints.py` 1583, `ths/transport/http_client.py` 669,
`akshare/models/stock_action.py` 282, `yfinance/models/stock_daily.py` 292, …) was read through
the functions that build requests and normalize rows, with the call sites pinned below.

### 0.2 A tree change happened during this run — record it before reading anything else

The first draft of this report was written against HEAD `819410d` into this same path. Between
writing it and verifying its citations:

- HEAD advanced by one docs-only commit (`819410d` → `802ad6b`; `git merge-base --is-ancestor 819410d HEAD` → yes);
- `docs/evidence/C70/` was deleted externally — both this report file and
  `upstream-boundary-ledger_v0.2_20260724.md` (1070 lines, v12, untracked) are gone
  (`find . -name "upstream-boundary-ledger*" → ∅`, `ls docs/evidence/ | grep C70 → ∅`);
- `docs/wdd/` is gone as a directory, so `de_2026-10-09-02_C70-boundary-ledger-dedup.md` (the D22
  decision, which forbade editing H-A/H-B and required a §6 complexity ranking) cannot be re-read
  either. Its rule is still followed here because this report edits no ledger at all;
- five other working-tree modifications and several untracked review artifacts that were present
  at the start are no longer listed by `git status`.

Consequence, and the reason this section is first: **every engine citation below was re-verified
against the files as they exist now, and the earlier draft's §2 was wrong.** It described an
engine surface with `transport`/`path_template`/`query=`/`data_path`/`column=`/`RowCursorSpec`,
a `ColumnSpec.decode/matches_param/json/datetime` vocabulary, `PaginationSpec.skip_page_count`,
`page_count_param`, `row_cursor_path`, `page_size_param`, `short_page_ends`, a 882-line
`http_json.py` with `paginate()`/`CallBudget`/`page_size_from_request()`, a
`testing.provider_response()/fetch_rows()/fetch_error()` seam and a `build_default_provider_registry()`
entry point. None of it is in this repository, and none of it ever was:

```bash
grep -rn "skip_page_count\|page_count_param\|row_cursor_path\|total_count_path\|page_size_path" --include="*.py" opendata/
# (no output)
git log --all -S "skip_page_count" -- opendata/
# (no output)
grep -n "def paginate\|class CallBudget\|page_size_from_request" opendata/data/providers/_engine/http_json.py
# (no output — the file was 555 lines then, 569 at 884f9bd, and the page driver is
#  fetch_pages(), declared at :402 in this tree)
```

I record this rather than quietly re-issue the text: a report whose citations cannot be re-walked
is worth less than one that admits the walk failed. The capability *inventory* (what the 44
fetchers actually do) survived that re-verification; the engine-side claims did not, and §2 below
is written from the files again. Where the two disagree, §2 wins.

## 0.3 A second tree change, during the writing of §5–§7: the engine grew a header slot

Between the §2 re-verification and the ranking pass, the in-flight diff on the engine grew. It has
since been committed, so the slot is a shipped part of the format rather than a forecast:

```bash
git log --oneline -4      # 802ad6b -> 9077dc7 -> 3765250 -> 6f12c4e
git show --stat 6f12c4e   # spec.py +24, http_json.py +20, testing.py +42, catalog.py +22,
                          # tests/test_provider_model_contracts.py +106
```

`ModelSpec` gained `static_headers: tuple[tuple[str, str], ...] = ()` (`spec.py:207`), validated in
`__post_init__` (`:260-277`) as an RFC 7230 field-name token (`_HEADER_TOKEN`, `:37`), non-blank,
CR/LF-free and not declared twice casefolded; `fetch_pages` now folds it into every page request
(`http_json.py:432`, passed to the transport at `:444-450`) and `_http_get_json` forwards it to
`http_client.get(..., headers=...)` (`:290-318`, whose signature already accepted headers at
`http_client.py:382-418`). The test transports record headers in `calls` through one shared
recorder (`testing.py:172`, appended at `:193`), and the contract suite checks the declaration rule
(`test_provider_model_contracts.py:683`, header rules `:744-769`). `6f12c4e` also moved discovery
behind a single runtime entry, `catalog.engine_declared_models()` (`catalog.py:191`), which returns
exactly the two cboe declarations.

This is the only place where a capability this report proposes has already landed, and it landed
while the report was being written, so §5 K11 is re-scoped rather than quietly kept: a *constant*
header per model is now declarable; a header whose value comes from a credential, varies per page,
or partitions a raw-response cache is not. Concretely, ECB's `Accept` value is a module constant
(`ecb/models/_reference_client.py:30`, sent at `:94`; `ecb/models/_series_client.py:42`, sent at
`:98`), so it is expressible in `static_headers` today and no longer gates anything. The census, the
44-fetcher inventory and every wave count in §7 are unaffected; §5 K11, §6's K16/K11 rows and §7's
waves 5-6 were re-cut to match (see §0.4).

## 0.4 The verifier pass against `884f9bd`

Two commits landed after the original read, and both moved line numbers:

- `6f12c4e feat(engine): C72 声明格式加静态请求头槽位，发现面收进 catalog 单一入口` — `spec.py`
  +24 (the `static_headers` slot and its rules), `http_json.py` +20 (headers built and sent per
  page), `testing.py` +42 (the shared `record()` the three transports now share), `catalog.py` +22
  (`engine_declared_models()`), contract suite +106;
- `884f9bd feat(acceptance): C72 声明来源判定面——列名、参数、host、path 四件事都要有出处` —
  `scripts/quality/declaration_provenance.py` (425 lines), the acceptance plane that asks each
  declared column name, parameter, host and path for its source.

Because the first insert is in the middle of `http_json.py`, every engine citation taken before it
drifted by 6-14 lines, and because the second is a new file, the provenance plane is now part of
"what already exists". An independent verifier re-walked every load-bearing claim against
`884f9bd`; the corrections it ordered are applied in place and listed with the commands that
produced them in §8. What changed as a result:

| claim | before | after |
| --- | --- | --- |
| K11 headers | a capability to build | the constant half **shipped** in `6f12c4e`; only per-request/credential-derived headers remain |
| `fetch_pages` / `transform_data` / `make_http_json_fetcher` | `:414` / `:525-550` / `:478` | `:421` / `:539-564` / `:492` (declarations at `:402` / `:539` / `:492`) |
| `ModelSpec.path`, `_http_get_json` json parse | `:191` / `:297` | `:196` / `:304` |
| fixture seam | "no `transport=` argument on `fetch_pages`" | `transport=` is a keyword at `http_json.py:407`; the class hook is `:518`→`:535` |
| contract suite | 712 lines, untracked, 19 faces at `:587`/`:630` | 770 lines, **tracked** (`9077dc7`/`6f12c4e`), 21 faces at `:627-650`, census at `:615`, rules at `:683`/`:744-769` |
| `AvailableIndices` aliases | "12 `source_key` aliases" | 12 declared columns, **5** of which carry a `source_key` |
| morningstar note | `specs.py:100-105` | `specs.py:46-48` (it is an `AVAILABLE_INDICES` note) |
| `testing.py` seams | `:117`/`:155`/`:191`/`:218` | `:119`/`:157`/`:217`/`:243` |
| K4 sort sites | `bls/series.py:138`, `fmp/…:173`, `ths/endpoints.py:580` | `:111`, `:175`, and `:580` does **not** sort (the sort is `:759`) |
| K5 derived columns | `fred/_series.py:59-63`, `actual_release_date`, "units constant percent change", `futures_daily.py:112` | `:113`; no such field; no such string (grep); `:115` |
| K9 ECB dedupe | `reference_rates.py:152-155` collapses duplicates | the file is 134 lines; `:125-133` *refuses* rows, it does not dedupe — claim withdrawn |
| K12 period grammar | `YYYY-A` vs `YYYY-MM-A` | yield curve is ISO daily (`:97-106`), BPS is `YYYY-MM`/`YYYY-Qn` (`:109-119`) |
| K16 gated count | 5 gated | 6 (FMP historical, BLS series, the four FRED pagers) |
| §6 prose | "ranks 1-5 deliver 3 models" | 8 models |

The right-hand column of that table is the stamp **at `884f9bd`**. §0.5 re-stamps every
`http_json.py` number in it for the tree this file is finally written against, and §8 lists the
corrections together with the commands that produced them.

## 0.5 A third tree change, during the writing of §6–§8

Between the verifier pass and the ranking fix, HEAD moved again and the working tree grew a new
in-flight diff owned by a different work stream:

```bash
git rev-parse --abbrev-ref HEAD && git rev-parse --short HEAD   # dev / 2c21b68
git log --oneline -5
git show --stat 2c21b68
git status --short && git diff --stat
wc -l opendata/data/providers/_engine/http_json.py             # 593
```

- `2c21b68 fix(registry): C72 注册顺序改为派生，声明的模型不可能再被漏掉` touches
  `catalog.py` (+24), `tests/test_first_model_bindings.py` (+60) and the new `sec/` package
  skeleton; it changes **no** file this report cites a line number in. The census was re-run and is
  unchanged: `TOTAL 46 ENGINE 2 HAND 44`, per-source `akshare 10, bls 2, cboe 2, ecb 6, fmp 2,
  fred 7, imf 3, oecd 2, ths 11, yfinance 1`, `verified` 23 `True` / 23 `False` over the 46
  bindings, `canonical_model` non-null on 13 of 46, `engine_declared_models()` a 2-element list.
  `sec` is in `catalog.list_providers()` (34 providers) and registers **0** fetchers, so it is not
  one of the 44.
- `opendata/data/providers/_engine/http_json.py` is **modified in the working tree and
  uncommitted** (593 lines against 569 at `2c21b68`; `git diff --numstat` → 27 added, 3 removed).
  The diff is four inserts — `import sys` (`:21`), the `ProviderEngineError(..., rejected=…)` field
  (`:49-65`), the `_QUERY_INVALID` refusal that now names the rejected keys (`:534-545`) and the
  `EngineFetcher.__module__` stamp (`:589-592`). It is not mine and I did not touch it; it means
  the file a reader opens is 24 lines longer than the committed one. Every `http_json.py` citation
  below is the **working-tree** line, which is what `grep -n` on disk returns today. Anyone holding
  `2c21b68` clean maps committed → working as `:1-20` unchanged, `:21-47` +1, `:49-523` +10,
  `:526-568` +20, `:569` +24. Re-checked against the two files: `fetch_pages` `:402`→`:412`,
  `make_http_json_fetcher` `:492`→`:502`, `transform_data` `:539`→`:559`, `_SAFE_VALUE`
  `:38`→`:39`, `class ProviderEngineError` `:45`→`:46`.
- `opendata/data/providers/cboe/specs.py` is modified in the working tree by the same change set
  (2 lines: `AVAILABLE_INDICES.domain` `cboe_index_catalog` → `instrument`,
  `INDEX_CONSTITUENTS.domain` `cboe_index_constituents` → `index_constituent`). The file is still
  122 lines, so §1's `:110-122` block, its `:114`/`:115` rows and §1's morningstar note at `:46-48`
  are unaffected; only the two domain strings changed, and this report cites neither.
- Nothing else this report cites moved: `spec.py` 300, `testing.py` 278,
  `test_provider_model_contracts.py` 770, `declaration_provenance.py` 425, `cboe/specs.py` 122,
  `ecb/models/_reference_rates.py` 825, `ths/models/option_daily.py` 113 — re-measured with
  `wc -l` in the same pass, and every provider package is clean in `git status`.

One substantive consequence beyond line numbers: the in-flight `_QUERY_INVALID` detail makes §2.2's
"failure ⇒ one opaque code" slightly less true than it was at `884f9bd` (the refusal now names the
rejected **keys**, never their values). That is a diagnostic improvement, not a declaration
capability: nothing in it lets a bound, a separator or a charset be declared, so no K item moves.

## 1. The nine `NOT_DECLARABLE` reasons (`cboe/specs.py:110-122`)

Verbatim, with the engine fact each one points at (all still true against §2's real surface):

| # | model | recorded reason | what the declaration lacks |
| --- | --- | --- | --- |
| 1 | EquityHistorical | "pre-flight index directory decides the URL; date window filtered client-side after a full download" | no model may consume another model's rows before building its URL (`fetch_pages:431` renders the path from the query alone); no client-side window filter (`transform_data:559-584` never consults `params`) |
| 2 | EtfHistorical | "alias of EquityHistorical in the upstream package" | aliasing is not a declaration feature; inherits #1 |
| 3 | EquityQuote | "four requests joined on symbol; percent columns scaled after the call" | one fetcher = one path (`ModelSpec.path:196`); no arithmetic on a value between raw and published (§2.2) |
| 4 | EquitySearch | "CSV download, not JSON" | `_http_get_json:290-318` calls `json.loads(response.text)` unconditionally (`:314`); a CSV body is `CBOE_BAD_RESPONSE` |
| 5 | FuturesCurve | "delegates to the historical and quote fetchers for a VX symbol list" | no composition: `make_http_json_fetcher:502` builds a fetcher that can only issue its own declared request |
| 6 | IndexHistorical | "interval branch selects between two URL roots and drops volume" | one `path` per model; no column set that varies by parameter |
| 7 | IndexSearch | "client-side substring search over the index directory" | no filter predicate anywhere in the engine (§2.3) |
| 8 | IndexSnapshots | "region branch addresses two unrelated paths and drops different columns" | as #6 |
| 9 | OptionsChains | "contract symbol parsed by regex, strike scaled by 1/1000, result published columnar rather than as rows" | `normalize_record:239-277` reads flat keys out of one dict and never derives a field from another field's text; `resolve_rows:209-230` requires a list, so parallel arrays are a shape failure |

Two of the nine are worth stating precisely because they *look* declarable: `IndexHistorical` and
`IndexSnapshots` each fetch one JSON document and trim columns — the trim is the problem, since a
declaration has exactly one `columns` tuple and no way to say "when `interval` is X, publish Y".
`EquitySearch` is the cheapest of the nine: one payload decoder (§6 K1) covers it.

The 2 engine-driven cboe specs are also useful as a format demo, not just as coverage:
`AVAILABLE_INDICES` (`specs.py:19-53`) declares `path` with no placeholder, `rows_pointer=""` (the
document *is* the list), 12 declared columns (`:30-41`) of which **5** carry a `source_key` alias
(`symbol`, `name`, `data_delay`, `open_time`, `close_time` — `:30-31` and `:35-37`) and an empty
`params=()`;
`INDEX_CONSTITUENTS` (`specs.py:56-107`) declares `path="/api/global/european_indices/constituent_quotes/{symbol}.json"`
(one placeholder consumed from the query — `encode_query:150-152` deliberately leaves it out of the
query string), `rows_pointer="data"` and `row_envelope=("symbol","name","currency")`. The two notes
record what upstream does that the declaration deliberately copies: `AVAILABLE_INDICES` notes that
upstream drops every record whose `source` equals `morningstar` and pops
`featured`/`featured_order`/`display` (`specs.py:46-48`), and `INDEX_CONSTITUENTS` notes that
upstream **divides** `price_change_percent` by 100 after the call, so the declaration keeps the
published scale and lets the consumer decide (`specs.py:101-103`). Neither is a thing the engine
does today — both are exactly why those behaviours are client-side.

## 2. What the engine can express today (re-read from the files)

### 2.1 The declaration surface

`ModelSpec` (`spec.py:165-300`) has exactly these slots: `model, domain, asset_class, period,
market, base_url, path, columns, params, rows_pointer, pagination, credential,
credential_query_key, async_mode, scenario, error_prefix, row_envelope, static_headers, notes`.
There is **no** `transport` kind and **no** payload field: the engine speaks JSON over GET and
nothing else (`static_headers`, §0.3, is the first slot added by a *need* other than the two cboe
snapshots, and it is constant-per-model only — the value cannot come from a credential or a page).
`ParamSpec` (`:50-89`) is `name, kind, query_key, required, default, enum, note` — **no numeric or
length bound, no regex, no list separator, no "this parameter also fills a path slot" flag**
(the path consumes a placeholder by name, `encode_query:160-163`), no fan-out to several upstream
keys, no window/mutual-exclusion policy. `ColumnSpec` (`:92-120`) is `name, kind, source_key,
required, nullable, units` — **no decoder, no timezone, no date format, no JSON-in-string, no
constant, no query-derived or join-derived source, no cross-row source, no echo binding**; `units`
is a string kept "verbatim in the review record" and never transforms anything.
`PaginationSpec` (`:123-162`) is `kind(none|offset|page|cursor), limit_key, offset_key, cursor_key,
cursor_field, total_key, max_pages(1..1000, default 50), max_rows(1..1e6, default 100_000)` —
**no page-count suppression, no row-cursor path, no short-page switch, no echo binding, no caps
preflight**.

`__post_init__` does real enforcement, all of it declaration-time: identifier checks
(`:225-230`), `base_url` must be https (`:231-232`), `path` a bare absolute path with no `?`/`#`
(`:233-234`), non-empty and duplicate-free `columns` (`:235-239`), `row_envelope` keys must be declared
columns (`:242-246`), duplicate-free `params` (`:247-249`), `credential` must name an env var and
have a query key (`:250-253`), `async_mode` ∈ {bounded_thread, unsupported} (`:254-255`), non-blank
`scenario` (`:256-257`), upper-case `error_prefix` (`:258-259`), the `static_headers` rules
(`:260-277`, §0.3), brace hygiene and placeholder-declared checks (`:278-290`), and *a path
placeholder must be required or defaulted* (`:291-300`, the rule `9077dc7` added; `static_headers`
and its rules came in `6f12c4e`). `PaginationSpec` enforces its own bounds in `:146-161`
(`max_pages` 1..1000, `max_rows` 1..1e6, cursor needs key+field, offset/page need both
`limit_key` and `offset_key`).

### 2.2 Request side

- Query model: `build_query_model` (`http_json.py:112-129`), pydantic `extra="forbid"`,
  `populate_by_name=False`; enum → `Literal[...]` (`:108`); a declared-but-unrequired, un-defaulted
  parameter becomes `T | None = None` (`:122`). Failure ⇒ `{P}_QUERY_INVALID` (`:538`, `:550`),
  before I/O; the in-flight diff makes that refusal name the rejected keys (`:534-545`, §0.5).
  **Bounds: none exist.** AC2-13/AC2-22-style caps (`limit ≤ 1000`, `offset + limit ≤ total`,
  `vintage_dates ≤ 2000`) have no declarative home at all.
- Encoding: `_encode_value` (`:191-206`) — `date` → `isoformat()`, `bool` → `true`/`false`,
  **list → `",".join` and nothing else** (`:200`), ints/floats → `str(v)`. Values are **not**
  percent-encoded: `_SAFE_VALUE` (`:39`) admits `A-Za-z0-9._~:/?=-+%,\s` and passes them through,
  and raises `{P}_PARAM_UNENCODABLE` for anything else — so a `";"`-joined parameter (FRED
  `tag_names`), a `"+"`-joined one (ECB reference rates), or an `"@"`/`"..."` OECD dataflow id
  cannot be expressed. A date never becomes an epoch integer, so a Shanghai-millis request
  (`ths/endpoints.py:249-250`) is out.
- Path: `render_path` (`:171-188`) substitutes declared placeholders only after checking each value
  against `_PATH_SEGMENT = ^[A-Za-z0-9._~\-]+$` (`:43`). Dots are fine (`SP003M2`), **`/`, `,`,
  `+`, `@` are refused** — a slash-bearing key such as IMF's
  `PCPIP/USA` (`imf/models/_client.py:88`) cannot enter the path.
- Credential: `{P}_CREDENTIAL_MISSING` before I/O (`_credential_value:280-287`), injected as
  `params[credential_query_key or "api_key"]` (`fetch_pages:429-430`). **Query only**: no header,
  no cookie, no body field — `static_headers` (§0.3) is constant-per-model and cannot read the
  environment.
- Transport: `_http_get_json` (`:290-318`) — one GET through
  `get_shared_http_client().get(url, params=…, headers=…, timeout=…, source=…)` (the `headers`
  keyword is `:296` and is forwarded at `:302-308` since `6f12c4e`), then `json.loads(text)`
  (`:314`). The governed client also exposes a JSON body on
  non-GET verbs (`opendata/data/http_client.py:420-455`, `json_body` refused for GET at `:455`), but
  the engine has no route to it: there is no POST path and no body in the declaration format.
- Status mapping `_classify_status` (`:321-339`): 400/422 → `QUERY_REJECTED`, 401/403 →
  `CREDENTIAL_REJECTED`, 404 → `RESOURCE_MISSING`, 429 → `RATE_LIMITED`, ≥500 →
  `UPSTREAM_UNAVAILABLE`, else `HTTP_ERROR`. A non-2xx body is never inspected: after 819410d the
  `HttpFetchError` path (`:309-312`) turns *every* transport-level error into `{SRC}_HTTP_ERROR`
  before `_classify_status` sees it, so for the governed client's own rejections (timeout, TLS,
  429-with-retry-exhausted) the precise codes above are unreachable.
  `ProviderEngineError` (`:46-65`) puts `status` **and `url`** into the message; the in-flight diff
  adds a third attributable field, the rejected parameter **names** (`:55`, `:61`, `:64`).

### 2.3 Response side

- Row selection `resolve_rows` (`:209-230`): dotted pointer through dicts **and list indices**;
  a missing step ⇒ `{P}_SHAPE_INVALID`; `None` ⇒ same; a non-list ⇒ same; an empty list is a
  legitimate zero. A response whose records are a **JSON object keyed by a dimension** — IMF's
  `document["values"][indicator][country] = {year: value}` (`imf/models/_indicator.py:90`) — has no
  representation. There is also no way to point at rows under a path chosen at run time.
- Normalization `normalize_record` (`:239-277`): one flat `record[source_key or name]`
  (`:250-257`), NaN/±Inf refused (`_reject_non_finite:233-236`), absent-and-declared-in-
  `row_envelope` falls back to the **document root** key (`:261-270`), required-and-absent ⇒
  `SHAPE_INVALID`, then the pydantic row model. `build_row_model` (`:132-149`) is
  `extra="forbid", frozen=True`, and `_column_type` (`:87-98`) already refuses a bool for an
  int/float column via `_refuse_bool` (`:80-84`). **Every** conversion failure — a string `"3"` in
  an int column, `"1.5"` in an int, `1` in a date, ISO-8601 with `+0000` instead of `+00:00`,
  `1716105600000` for a date, a value inside a JSON *string* — collapses into one
  `{P}_SHAPE_INVALID` (`:276-277`), which is the wrong shape for AC2-10's "原始缺字段/类型/单位异常"
  judgement. No type-level decision is visible anywhere.
- Ordering: none. `transform_data` (`:559-584`) appends pages in fetch order (`:569-573`) and rows in
  response order; `sorted` appears nowhere in the engine. A provider that must publish ascending
  dates has to sort — which is why `fred/models/_series.py:94`, `ecb/models/_series.py:97`,
  `imf/models/_indicator.py:95`, `oecd/models/cpi.py:106` and `bls/models/series.py:111` all do it
  themselves after normalizing.
- Paging driver `fetch_pages` (`:412-473`): a single `while True` per page; `offset` kind sends
  `offset = collected` (rows so far, `_page_request:357-358`) and `page` kind sends `page_number`
  (`:359-360`); `limit_key` is injected **only if absent from the encoded query** (`:355-356`) using
  `paging_page_size(spec)` (`:364-371`); `cursor` kind reads `cursor_field` **at the document root
  only** (`_next_cursor:399-409`, `document[field_name]`, no dotted path) and sends it as
  `cursor_key` from the *second* request on (`:441-442`). Termination: `len(rows) < page_size`
  with `page_size = effective_page_size(spec, params)` (`:374-396`, the C69 fix — the size the
  request actually carried) or a missing cursor; bounds `collected > max_rows`
  (`:458-459`) and `page_number > max_pages` (`:465`, `:471`) raise `{P}_INCOMPLETE`. Completeness:
  `_declared_totals` (`:490-499`) collects each page's `total_key` (read at the root by
  `_declared_total`, `:476-487`) and
  `transform_data` (`:574-583`) raises `TOTAL_CONFLICT` if the pages disagree or if rows exceed the
  total, `INCOMPLETE` if they fall short. `total_key` **never drives or terminates the loop**.
- There is no cross-page dedupe, no identity or conflict code, no row filter, no window validation
  against `params`, no ordering, no cap preflight ("this query needs N requests before sending 0"),
  no `requests_made`/attempt accounting, **and no deadline or cancellation check inside the loop**
  — `ctx` is read once, for `ctx.timeout` (`:417`), and never again; `FetchContext`'s combined
  deadline machinery (`opendata/data/protocol.py:93-120`) is not consulted. A 50-page loop is
  unbounded in wall-clock time and unbudgeted in attempts.
- Nothing in the engine charges the request budget the fixture hands it:
  `testing.py:119` `fixture_context()` builds a real `RequestBudget` with a `RequestScope` per
  model, and `_http_get_json` → `get_shared_http_client().get(...)` never touches it (AC2-15/AC2-16).
- The `verified` flag is hardcoded: `Capability(asset_class, domain, period, market, source,
  verified=False, notes)` at `http_json.py:496-503`, and `Capability`
  (`opendata/data/capability.py:33-39`) has **no** verification-date field. A declaration cannot
  carry "SOURCE_VERIFIED on <date>", and cannot participate in `source="auto"` routing at all
  (`capability.py:48` gates on `verified`) — so today's 2 engine-driven models are invisible to
  auto-routing by construction, not by review.

### 2.4 The offline seams that exist

Two seams, not one. `fetch_pages` takes a `transport: HttpGet | None` keyword (`http_json.py:407`,
used at `:423`), and `make_http_json_fetcher` exposes a class-level `http_transport:
ClassVar[HttpGet | None]` (`:518`) that the generated `extract_data` hands to it (`:535`) — so a
test can substitute the transport either per call or per generated class.
`_engine/testing.py` (278 lines) provides `RIGHTS_EVIDENCE` (`:32`), the synthetic-value helpers
`sample_value`/`synthetic_record`/`synthetic_page`/`valid_query_kwargs` (`:46-115`),
`fixture_context` (`:119`), `SyntheticTransport` (`:157`), `FixedResponseTransport` (`:217`) and
`SequencedResponseTransport` (`:243`) — the last one raises when the engine asks for a page beyond
the script (`:274`), which is precisely the falsifiability paging cases need. All three now share
one recorder (`record`, `:172`, called at `:208`/`:239`/`:272`), so a request option recorded by one
transport cannot be dropped by another, and `headers` is part of the logged call (`:193`).
`tests/test_provider_model_contracts.py` (770 lines, **tracked** — added by `9077dc7`, extended by
`6f12c4e`) already judges AC2-10's
sub-checks per declaration: identity/capability from the declaration, exact declared columns,
undeclared parameter refused before any send, illegal value refused, required-parameter omission,
credential failure before send, typical-request normalization, shape failure when the document
carries no record list, empty list = zero rows, missing required source key, optional column →
null, boolean-for-numeric refused, non-finite refused, status → declared code, paging drives from
the declaration and stops on a short page, declared total read once and agrees, short of total →
incomplete, disagreeing pages → conflict, path placeholders never reach the wire unresolved,
static headers sent on every page (`:579`), async applicability, and the declaration rules
(`TestDeclarationRules:683`, header rules `:744-769`). It also prints a census
(`test_discovery_accounted_for_every_registered_binding:615`, output at `:621-624`) and a per-face
applicability census (`:653`) over a face table of **21** faces (`:627-650`, the twenty-first being
`static_headers` at `:650`).

**Read that as the floor, not the ceiling:** every one of those judgements runs against the engine's
*own* two declarations plus probes. Nothing in it touches the 44 hand-written fetchers, because
they are not declarations. The backlog below is what has to be added to the format for them to
enter that suite's population.

## 3. What the 44 hand-written fetchers actually do

Grouped by the mechanism they use, with the call sites that a `ModelSpec` cannot express.

### 3.1 Single GET, JSON, query-key credential, list under a pointer (closest to declarable — 3 models)

`fred/models/_client.py:60-143`: one GET, `api_key` injected into the query (`:125`),
`file_type=json` (`:126`), rows at `document["observations"]` (`:140`) — that part **is** today's
engine (`credential_query_key`, `rows_pointer`). Per model (`fred/models/_series.py:52-127`, the
file is 128 lines): `series_id` is the query parameter, passed through unchanged (`:66`, `:91`) and
upper-cased when it is published as a column (`:113`), FRED's missing-value sentinel `"."`/`""`
becomes `None` (`_parse_value:121-127`, the test at `:126`), and the rows are re-sorted by date in
python (`:94`) even though FRED already returns ascending. Two claims the first draft made about
this model are **withdrawn**: `_series.py` declares no `units` and no `output_type` at all
(`grep -n "units\|output_type" fred/models/_series.py` → ∅), and no `"percent change"` constant
exists anywhere in the tree (`grep -rn "percent change" --include="*.py" opendata/` → ∅). The
`output_type` constant 1 is real but belongs to another module — `fred/models/series.py:26` and
`fred/models/_estr_query.py:86` — and `search.py` reads `units` **from the row** (`:141`), so it is
a `source_key` today, not a derived constant.

### 3.2 Single GET with a JSON *object* as the record set (3 models)

`imf/models/_client.py:55-99`: the URL is `{base}/{indicator}/{country}` — a slash-bearing path key
(`:88`) — and the payload is `document["values"][indicator][country]`, i.e. `{year: value}` (`:94`).
`imf/models/_indicator.py:78-95` then iterates the mapping's items, turns each year key into
January 1st (`:116`), derives `series_id = f"{indicator}.{country}"` (`:89`), filters
`start_date`/`end_date` client-side (the docstring at `_client.py:5` records that upstream **ignores**
the `periods` filter, verified live), and sorts.

### 3.3 Single GET returning CSV (5 models)

`ecb/models/_client.py:103-114`: `format=csvdata`, URL `{base}/service/data/{series_key}` (dotted
key in the path, `:108`), `csv.DictReader` over the text (`:112`), and a required-header check
against `{KEY, TIME_PERIOD, OBS_VALUE}` (`:113`). `ecb/models/_series.py:114-133`: empty
`OBS_VALUE` ⇒ `None`, `TIME_PERIOD` widened from `YYYY` / `YYYY-MM` / `YYYY-Qn` to a first-of-period
date, then sorted. `oecd/models/_client.py:135-146`: same shape with `format=csvfile` and a path
containing both a dataflow (`OECD.SDD.TPS,DSD_PRICES@DF_PRICES_HICP` — comma and `@`) and the
series key; `oecd/models/_client.py:27-35` normalizes periods the same way, and
`oecd/models/cpi.py:133-152` builds `series_id` by joining **eight** raw columns with dots.

### 3.4 Governed GET with a strict page-identity loop (4 models)

`fred/models/_paged.py:85-175`: `limit`/`offset` appended per page (`:104-108`), the response's
echoed `offset` bound to the requested one and never repeated (`:121-131`), `count` read once and
compared on every later page (`:140-149`), **exact** page length required —
`len(page) != min(limit, count-offset)` is a pagination failure, not a short page (`:151-153`),
termination by `offset + page == count` (`:170-174`), duplicate rows collapsed by an identity
function with a conflict code when the copies differ (`:160-168`), and caps preflight that names
which cap was crossed (`:16-24`, `:144-147`).
`fred/models/_fixed_observation_pages.py` adds the governed-scope layer: 13 required page fields
bound against the query (`:31-47`, `:348-382`), scope authorization and per-page liveness re-check
(`:133-161`), JSON-depth pre-scan (`:176-197`), serialized-page byte budget (`:248-266`),
duplicate-JSON-key refusal (`:273-279`), request params that always carry `limit`+`offset`
(`:544-560`). Its consumers are `_sofr_client.py`, `_sonia_client.py` (and the unbound
`_iorb_client.py`); the model layer still adds its own work: the sentinel `"."` decode plus
`native_units` from a lookup keyed by the query (`sofr.py:18-24,88-104`), the SONIA parameter↔series
consistency check (`sonia.py:82-84`), the strict window assertion (`sofr.py:107-112`), and query
re-validation at *both* stages (`sofr.py:118-125`).

`fred/models/search.py:112-127` is the offset loop again plus two encodings the format lacks:
`tag_names` joined with `";"` (`:166`, `:168`) and `last_updated` normalized from FRED's compact
`+00` UTC offset to `+00:00` with a regex (`:173-177`), with `units`/`last_updated`/`popularity`
filled from the query or as constants (`:145-152`).
`fred/models/series.py:103-133`: `as_of` fans out to **two** upstream keys
(`realtime_start` = `realtime_end` = as_of, `:122-124`), `vintage_dates` joins with `","` (`:115`),
`output_type` is stringified (`:108`), and the realtime selector/as_of/vintage mutual exclusions are
enforced in the query model (`:67-75`).

### 3.5 Governed GET with response-shape sniffing (2 models)

`fmp/models/_client.py`: `apikey` as a query param (`:114`) with an error type that deliberately
**omits the URL** because the key travels in it; an HTML login page arriving with 200 is detected
(`_looks_like_html:156-158`) and reported as `FMP_HTML_RESPONSE`; a JSON parse failure is
classified by parser position into truncated-vs-malformed (`_looks_truncated:161-166`); a JSON
error *document* is detected by key sniffing (`_is_api_error_document:168-170`); statuses map to
`FMP_AUTH_ERROR/FORBIDDEN/RATE_LIMITED/UPSTREAM_ERROR` (`_http_error_code:143-153`).
`fmp/models/equity_quote.py:19-21` refuses a date window for a snapshot model, and `:55-56`
refuses anything but exactly one row (`FMP_QUOTE_EXPECTED_ONE_ROW`) while `:64-66` checks the
returned symbol case-insensitively against the request (`FMP_WRONG_SYMBOL`).

### 3.6 Recursive request splitting plus explicit budgets (1 model)

`fmp/models/equity_historical.py:63-131`: a deque of date windows; each response with
`len(rows) == 5000` (the provider's undocumented cap, `_client.py:18`) is **bisected** and both
halves re-requested (`:99-121`, `_split_window:178-189`), with three distinct terminal codes that
each carry `requests_made` (`:80-85` `FMP_INCOMPLETE_REQUEST_BUDGET`, `:100-118`
`FMP_INCOMPLETE_PROVIDER_LIMIT`, `:124-128` `FMP_INCOMPLETE_RECORD_BUDGET`).
`transform_data` (`:136-173`) stamps `query_window_scope`/`window_boundary_semantics` context
columns, rejects out-of-window rows, deduplicates by date with a conflict code, and publishes in
date order.

### 3.7 POST, credential in the body, series×year matrix, nested flatten (2 models)

`bls/models/_client.py:89-113` is a genuine JSON POST through the governed client
(`.request("POST", …, headers={"Content-Type": "application/json"}, json_body=payload)`), and
because `http_client.py:455` refuses `json_body` for GET the engine has no route to it.
`:230-240` selects v1 or v2 by whether a key exists (`:242`), puts the key in the body
(`:252`), sizes the fan-out (50×20 with a key, 25×10 without) and **preflights** the matrix against
both a per-call and a per-day budget (`:235-240`). `:262-272` merges the `Results[]` windows with a
cross-window conflict code; `:320-330` refuses a 200 whose `status` is not `REQUEST_SUCCEEDED` or
whose `message` list is unknown, and `:416-419` sniffs the BLS FAQ's wording out of the message to
produce `BLS_INVALID_SERIES_ID`. `:340-412` walks three nested levels (with two alternative
capitalizations) and deduplicates by `(series, date)` — while `bls/models/series.py:149-153` refuses
any value that is not a strict numeric string ("no undocumented missing marker is inferred") and
`:42-60` enforces year-vs-date window exclusions and refuses `as_of` outright ("BLS has no official
vintage API").

### 3.8 TSV catalog with client-side search and a page container (1 model)

`bls/models/_client.py:116-218`: the file is read as **bytes** (`:130`), refused on BOM (`:138`),
HTML (`:145`), oversize (`:134`) or emptiness (`:136`), parsed with a strict TAB dialect (`:148`),
header-validated (`:152-158`), control characters refused per cell (`:170-174`), duplicate ids
refused with a conflict code (`:194`), then filtered by a casefold substring match over the whole
catalog (`:204-211`) and sliced locally into a page whose `total` is **counted, not published**
(`:212-217`). `bls/models/search.py:73-83` publishes that page as a single typed container row
(`BlsCatalogPage(items, total, offset, limit, catalog_updated)`) — one row for the whole response,
which is the opposite of `transform_data`'s one-row-per-record shape.

### 3.9 Bespoke gateway: header credential, envelope, cache, two-step code resolution (11 models)

`ths/transport/http_client.py:55-56` authenticates with an `X-api-key` header; the same module
partitions a raw-response cache by url+headers+params+context (`:89`, `:191-211`), and enforces a
FetchContext scope grant per host/attempt (`:211-218`, `authorize_request`/`check_scope_liveness`
imported at `:39-43`). `ths/transport/envelope.py:5-11,26-46` documents the business envelope
`{"code":0,"message":…,"request_id":…,"data":{"timestamp":ms,"item":[…]}}` — success requires
`code == 0`, and HTTP 200 with a non-zero code is a failure classified through
`error_for_upstream_code`. Dates are Shanghai epoch-millis both ways (`endpoints.py:150`, `:181`),
requests chunk a window at 10 years (`:198`) and the ETF leg has a depth floor of 1827 days and a
1826-day span cap (`:1337-1342`). Symbols are **resolved before the data request**:
`ths/models/_client.py:108-140` turns `600519` into a qualified code through the search endpoint
and fails closed unless exactly one match comes back. The models then do per-response work the
engine never does: `endpoints.py:448-492` normalizes millis→date bars, `:1423-1470` re-computes
unadjusted prices from the raw frame plus a dividend join, `:1041-1057` reads its item whitelist out
of the pivot registry (`require_pivot`, `mapping/endpoint_map.yaml`) and melts wide columns into
`(item, value)` rows, `:580-643` derives `status` against the envelope snapshot date rather than
from `end_date` hollowness, and `models/trading_calendar.py:162-189` fills each row's
`prev_trade_date`/`next_trade_date` **from adjacent rows** while `:152` refuses a result that does
not cover the asked window. `models/instrument.py:158-169` loops pages until a short page and
raises `THS_INSTRUMENT_PAGINATION_STUCK` if none appears — it does not trust a short page to be an
end. **Withdrawn:** the first draft's "`models/option_daily.py:118-121` derives the row's
`underlying_date` from the expiry-strike column via a timestamp conversion" cannot be cited — the
file is 113 lines, it has no timestamp conversion and no `underlying_date` anywhere in the tree
(`grep -rn "underlying_date" --include="*.py" opendata/` → ∅). That claim is unverified and is
removed; `ths/models/option_daily.py`'s real per-request work is a second code resolution
(`resolve_option_code`, `:93`) plus the same closed→half-open window conversion (`:90-91`) — that is
what the draft should have cited. `models/stock_daily.py:78-85`
does convert the contract's closed window into the gateway's half-open one (`+1 day`, `:80-81`).

### 3.10 Vendored SDK returning pandas frames (10 models)

Every akshare `extract_data` is an import plus one vendor call returning a `DataFrame` — e.g.
`akshare/models/stock_daily.py:66-77` (`opendata_http.stock_zh_a_hist` with `%Y%m%d` dates and the
`""|"qfq"|"hfq"` adjust token), `:82-110` renames Chinese columns, multiplies `成交量` by 100
(lots→shares, `:110`) and re-sorts. The shared normalizer carries the per-source symbol spellings
(`_normalize.py:17` plain, `:30` eastmoney, `:53` sina, `:67` `within_window`, `:88` `as_date`).
`stock_action.py:90-92` makes **two** vendor calls (dividends and 配股) and `:29`/`:280` parse the
rights plan out of a free-text cell with `re.compile(r"10\s*配\s*([0-9.]+)")`, then divides by 10 and
drops undated rows. `financial_statement.py:5-14`/`:95` and `financial_indicator.py` melt a wide
frame into `(item, value)` rows using the column vocabulary declared in the pivot registry, dropping
anything non-numeric. `futures_daily.py:109-112` and `option_daily.py:111` publish `amount=0.0` for
columns the source does not carry, and `bond_daily.py:122` rewrites the sina code by prefix (11→sh,
12→sz).

### 3.11 Third-party SDK plus cross-source reconciliation (1 model)

`yfinance/models/stock_daily.py:59-95` merges two split sources and refuses disagreement
(`YFINANCE_SPLITS_UNAVAILABLE`/`YFINANCE_BAD_SPLIT_RATIO`); `:98-150` re-computes as-traded prices
by undoing the SDK's backward adjustment with a cumulative product over strictly-later split
events; `:153-167` drops NaN-priced rows as unsettled sessions; `:228-237` is the two-call join
(`require_history_frame` + `require_split_history`). `amount` is `None` by decision, not by absence.

## 4. What is already covered elsewhere (dedup — re-based on files that exist)

The `upstream-boundary-ledger_v0.2_20260724.md` I was dedupping against is **gone** (§0.2), so this
section no longer cites H-A/H-B item numbers I can no longer read. Dedup is against three things
that do exist:

1. **The tracked acceptance sheet** `docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/验收文档.md`
   (AC2-01…AC2-25, 90 lines). AC2-10's per-instance checklist (line 50) is the contract suite's
   eight judgements: 有效典型请求 / 参数非法在 I/O 前拒绝 / 原始缺字段·类型·单位异常 / 空响应与缺数
   区别 / 分页完整性或快照边界 / 时间窗·业务键·修订 / 来源限流·不可达 / 服务权限与序列化. Every
   capability in §5 names the one it serves. AC2-11 is the existing-source 85-row case, AC2-16 the
   applicability census, AC2-18 duplicate/conflict corporate actions, AC2-19 PIT, AC2-22 the
   request/security debt case (timeout, 429, paging anomalies, TLS/parse risk), AC2-08 the 350-row
   delta record whose `scenario` column is empty for all 350 rows today.
2. **`cboe/specs.py`'s own `NOT_DECLARABLE`** — §1 above: nine recorded reasons, and the file is the
   only place in the repo where "why not declarable" is written per model. Any engine capability
   that does not move at least one of those nine is not earning its complexity.
3. **The work that was in flight and has since landed**: `9077dc7` committed the two
   declaration-hygiene rules — offset/page pagination must carry `limit_key` **and** `offset_key`
   (`_engine/spec.py:156-161`) and a path placeholder must be required or defaulted (`:291-300`) —
   together with the AC2-10 judgment generator `tests/test_provider_model_contracts.py` (770 lines,
   tracked) and the `_engine/testing.py` fixture helpers; `6f12c4e` then added the `static_headers`
   slot (§0.3) and `catalog.engine_declared_models()` (`catalog.py:191`), and `884f9bd` added the
   declaration-provenance plane `scripts/quality/declaration_provenance.py` (425 lines), which asks
   each declared column name, parameter, host and path for its source.
   Those are declaration-*hygiene* rules, one constant-header slot and the AC2-10 judgment
   generator — they add no payload types, no bounds, no decoders, no derived columns, no POST, no
   joins, no per-request headers and no budgeting, so nothing in §5 duplicates them. `static_headers`
   is the one exception and §5 K11 is re-scoped around it. Anyone who lands the rest of §5 must land
   its fixture helpers into the same `SyntheticTransport`/`SequencedResponseTransport` seam
   (`testing.py:157`/`:243`), not a new one.

Also recorded, because it is a real overlap risk: the C69 pagination fixes **did** land, under
different names than the earlier draft of this report claimed — `effective_page_size`
(`http_json.py:364-386`, page size actually carried), `_declared_total`/`_declared_totals`
(`:466-477` + `:480-489`) and their use in `transform_data` (`:554-563`, pages must agree on one
total). What remains genuinely unclaimed: `INCOMPLETE` is used both for
"bound reached" (`:449`, `:456`, `:462`) and "rows short of total" (`:561`), and a short page is
still measured against the page **we** requested rather than what the response echoed (`:433`
computes `page_size = effective_page_size(spec, params)`, `:458` compares `len(rows) < page_size`).

Counting rules used from here on, stated once: **gated** = of the 21 hand-written fetchers that run
on the same governed HTTP/JSON transport as the engine (FRED 7, ECB 6, OECD 2, IMF 3, FMP 2,
BLS 1), the ones that cannot be declared at all without this capability. **touched** = the same
count over all 44, including the 22 that need a different transport family (§8). Ranking uses
`gated ÷ complexity` because a ranked backlog has to compare things the same engine can actually
adopt; §6 shows both columns.

## 5. The distinct capabilities

### K1 — Non-JSON payload decoders (`payload="csv"|"tsv"` + dialect)
- **Unblocks** 5 gated / 7 touched: `EcbCpiFetcher`, `EcbGdpFetcher`, `EcbRateFetcher`,
  `OecdCpiFetcher`, `OecdUnemploymentFetcher` (+ `BlsSearchFetcher`, whose result is a page
  container, §8; + cboe `EquitySearch`).
- **Sites** `ecb/models/_client.py:103` (`format=csvdata`), `:112` (`csv.DictReader`), `:113`
  (required-header check); `oecd/models/_client.py:138` (`format=csvfile`), `:146` (same reader);
  `bls/models/_client.py:148` (`csv.reader(…, delimiter="\t", strict=True)`);
  `cboe/specs.py:115` ("CSV download, not JSON"). Against the engine: `_http_get_json` parses at
  `:304` — `json.loads(response.text)` — so the CSV body is a parse failure before any decoding is
  possible.
- **Smallest declaration surface** `ModelSpec.payload: Literal["json","csv","tsv"] = "json"`,
  `PayloadSpec(dialect_delimiter=",", strict=True, required_columns=("KEY","TIME_PERIOD","OBS_VALUE"),
  charset="utf-8")`, and `HttpGet` generalized to return `HttpResponse(status, document | text)` so
  the decoder runs after transport. `_SAFE_TEXT`/size ceilings ride on K8.

### K2 — Column value decoders (`ColumnSpec.decode`)
- **Unblocks** 16 gated / 36 touched. Every B-class model except `EquityQuote` reads at least one
  value through a source-specific rule; nothing in `ColumnSpec(name, kind, source_key, required,
  nullable, units)` can express one, and every failure collapses to `_SHAPE_INVALID`
  (`http_json.py:266-267`).
- **Sites** `fred/models/_series.py:120-128` (sentinel `"."`/`""` ⇒ `None`) and `sofr.py:91`,
  `sonia.py:91`; `ecb/models/_series.py:114-121` (empty `OBS_VALUE` ⇒ `None`) and `:124-133`
  (`YYYY` / `YYYY-MM` / `YYYY-Qn` widening); `oecd/models/_client.py:27-35` (same widening);
  `imf/models/_indicator.py:116` (year key ⇒ January 1st); `fred/models/search.py:173-177`
  (compact `+00` UTC offset normalized with a regex); `bls/models/series.py:149-153` (strict
  numeric string, "no undocumented missing marker is inferred") and `:428`-area `latest`
  bool-or-`"true"`; `ths/endpoints.py:181` (Shanghai epoch-millis ⇒ trading date) for the 11 C-class
  models; `akshare/models/stock_daily.py:110` (`成交量` × 100 lots→shares — a *scale*, not a unit).
- **Smallest surface** `ColumnSpec.decode: str | None` with a registry keyed by name and a
  `decode_options: tuple[tuple[str, Any], …]`: `sentinel_null(".")`, `sdmx_period`, `year_to_date`,
  `compact_utc_offset`, `epoch_millis_date(tz)`, `strict_number_string`, `true_string_to_bool`,
  `scale(100)`. Decoders must raise *typed* failures (`{P}_VALUE_UNDECODABLE`,
  `{P}_KIND_MISMATCH`) instead of one `SHAPE_INVALID`, which is the AC2-10 "原始缺字段/类型/单位异常"
  judgement and AC2-13's integer-string question in one place.

### K3 — Request encoding the format cannot express
- **Unblocks** 12 gated / 15 touched.
- **Sites** `fred/models/search.py:166,168` (`";".join(tag_names)`; engine joins with `","`
  unconditionally, `http_json.py:190`); `fred/models/series.py:115` (`","` join over dates) and
  `:122-124` (**one** `as_of` filling **two** upstream keys); `ecb/models/_reference_client.py:78`
  (`"+".join`); `ecb/models/_client.py:108` and `oecd/models/_client.py:137` (path keys containing
  `.` and `,`/`@`: `_PATH_SEGMENT` at `http_json.py:42` refuses both); `imf/models/_client.py:88`
  (slash-bearing path key `PCPIP/USA` — `render_path:175` refuses the `/`);
  `ths/endpoints.py:249-250` (`start`/`end` as epoch millis with `end - 1`);
  `bls/models/_client.py:244-251` (`startyear`/`endyear` as *strings*).
- **Smallest surface** `ParamSpec.list_separator: str = ","`, `ParamSpec.maps_to: tuple[str, …] = ()`
  (fan-out), `ParamSpec.render: "iso_date"|"epoch_millis"|"string"`, and a widened path-key rule:
  `ModelSpec.path_charset` (per-placeholder allowed character class, still forbidding `?`, `#`,
  whitespace and `..`), plus `ParamSpec.patterns` for the `YYYY-MM-DD\Z` style assertions
  `fred/_sofr_query.py:18-22` and `fred/_sonia_query.py:51-61` hand-write today.

### K4 — Published row order (`ModelSpec.row_order`)
- **Unblocks** 17 gated / ~30 touched, and is the single cheapest item in this list: it is one
  declaration field plus one `sorted(...)` before `transform_data`'s return (`http_json.py:564`).
- **Sites** `fred/models/_series.py:94`, `ecb/models/_series.py:97`, `imf/models/_indicator.py:95`,
  `oecd/models/cpi.py:106`, `oecd/models/unemployment.py:113`,
  `bls/models/series.py:111` (sorted by `(series_id, year, period)` — the draft's `:138` is inside
  `_normalize_observation`, which does no sorting),
  `fmp/models/equity_historical.py:175` (`rows_by_date[day] for day in sorted(rows_by_date)`),
  `ths/endpoints.py:759` (constituent rows by `symbol`) and `:1127` (financial rows by
  `(report_period, announce_date, item)`), `ths/models/financial_statement.py:157`.
  **Correction:** the draft's "`ths/endpoints.py:580`-area instrument ordering" is not an ordering
  site — `:580` is the `normalize_instruments` signature and that function never sorts (its sorts
  are elsewhere in the module, listed above), so the THS leg of this count rests on `:759`/`:1127`.
  The akshare leg is real but sits in other files: `bond_daily.py:116`, `futures_daily.py:118` and
  `option_daily.py:118` sort by `trade_date`; `akshare/models/stock_daily.py` does not sort at all.
  The engine has no ordering concept: `resolve_rows:199-220` preserves response order and pages are
  concatenated in fetch order (`http_json.py:549-553`).
- **Smallest surface** `ModelSpec.row_order: tuple[OrderSpec, …] = ()` with
  `OrderSpec(column, direction="asc")`; stable sort applied to the **accumulated** rows, never
  per page (a per-page sort is what a naive implementation writes, and it is wrong for offset
  paging because the published order must be the whole-result order).

### K5 — Derived and context columns (`ColumnSpec.source`)
- **Unblocks** 14 gated / 27 touched. `ColumnSpec` can only name one flat key inside a record.
- **Sites** `fred/models/_series.py:66`+`:91`+`:113` (`series_id` is the request parameter,
  upper-cased and published as a column — the draft's `:59-63` is a docstring),
  `fred/models/sofr.py:18-24`+`:104` (`native_units` from a lookup keyed by that id), `sonia.py:82`
  (a parameter→(series_id, units) map **and** a consistency refusal when they disagree),
  `fred/models/search.py:134-151` (`last_updated` is the raw string re-written by
  `_timezone_aware_timestamp` `:172-177` at `:145`, `popularity` is an optional read at `:147` —
  **the draft's "`units` constant `"percent change"`" is withdrawn**: `units` is read from the row at
  `:141`, and the string `percent change` appears nowhere in the tree),
  `imf/models/_indicator.py:89` (`indicator.COUNTRY`),
  `oecd/models/cpi.py:133-152` (eight joined columns, `:142-151`), `bls/models/series.py:116-145`
  (`preliminary` derived from a `P` footnote at `:133` and published at `:143`, `latest` echoed at
  `:142` — **the draft's `actual_release_date` is withdrawn**: no such field exists in the repo,
  `grep -rn actual_release_date --include="*.py"` → ∅),
  `fmp/models/equity_historical.py:147-149` (`query_window_scope`, `window_boundary_semantics`,
  `provider_default_window_semantics`),
  `ths/endpoints.py:580-643` (`status` derived against the envelope snapshot date — snapshot picked
  at `:617`, published at `:634`, decided in `_instrument_status:643-655`,
  `envelope_snapshot:658`) and `ths/models/*.py`'s `adjust="unadjusted"` constants
  (`ths/models/stock_daily.py:85`);
  `akshare/models/futures_daily.py:115` (`amount=0.0` — a declared constant standing in for a column
  the source does not publish; same at `akshare/models/option_daily.py:115`).
- **Smallest surface** `ColumnSpec.source: str = "row"` with prefixes `query.<param>`,
  `constant:<value>`, `lookup:<registry-id>.<key-param>`, `join:<col1,col2,…>` (with a separator),
  `echo:<document.path>`, `footnote:<code>`; each derives **from the record or the declared query**,
  never from another row (that is K-NEIGHBOUR, §8) and never by arithmetic (K17, refused).
  `lookup:` and `join:` need a small registry object beside the declaration, like
  `mapping/endpoint_map.yaml` already is for akshare/THS.

### K6 — Row-set shapes other than "list under a pointer"
- **Unblocks** 3 gated (`ImfCpi/Gdp/UnemploymentFetcher`). `resolve_rows:199-220` requires a `list`;
  IMF's records are the *keys* of a dict, and the pointer itself is only known from the query.
- **Sites** `imf/models/_client.py:94` (`document["values"][indicator][country]`),
  `imf/models/_indicator.py:90` (`for year_text, value in raw.items()`).
- **Smallest surface** `ModelSpec.rows_shape: "list"|"record_mapping"` and
  `ColumnSpec.source_key` allowed to bind a mapping **key** (`@key`); the pointer accepts the same
  `{placeholder}` templates as `path`.

### K7 — Date-window and parameter policy
- **Unblocks** 10 gated / 32 touched.
- **Sites** `imf/models/_indicator.py:91-94` (filter client-side because upstream ignores the
  window, `_client.py:5`), `fmp/models/equity_historical.py:33-41` (`start_date`/`end_date`
  together) and `:161-166` (a row outside the window is `FMP_OUT_OF_WINDOW_ROW`, not a drop),
  `fmp/models/equity_quote.py:19-21` (a snapshot model that **refuses** a window),
  `fred/models/series.py:67-75` (as_of vs realtime vs vintage exclusions),
  `fred/models/sofr.py:107-112` (observation outside the requested day ⇒ error),
  `bls/models/series.py:42-60` (year-vs-date window exclusions, complete window required, `as_of`
  refused: "BLS has no official vintage API"), `ths/models/index_constituent.py:79-80` (snapshot
  legs fail closed on any date range), `akshare/models/_normalize.py:67` (`within_window`),
  `ths/models/stock_daily.py:80-85` (closed contract window ⇒ half-open gateway window, `+1 day`).
- **Smallest surface** `ModelSpec.window: WindowPolicy(mode="filter_rows"|"reject_out_of_window"
  |"passthrough", boundary="closed"|"half_open_plus_days:1", required="none"|"together"|"complete")`
  plus `ParamSpec.mutually_exclusive: tuple[str, …]`, `required_with: tuple[str, …]`,
  `forbidden_with: …`, and a per-model `rejects` list carrying the code name. AC2-19 (PIT) and
  AC2-13 both need this: `as_of` today means four different things in four providers.

### K8 — Response guards and must-reject shapes
- **Unblocks** 9 gated / 12 touched. The engine's only guards today are non-finite numbers
  (`:223-226`) and shape (`:215-219`); a 200-status HTML page parses to
  `{SRC}_BAD_RESPONSE` and nothing else.
- **Sites** `fmp/models/_client.py:84` (`FMP_HTML_RESPONSE`), `:156-158` (`_looks_like_html`),
  `:161-166` (truncated tail vs malformed JSON, by parser position), `:168-170` (error *document*
  sniffing), `:143-153` (the provider's own status vocabulary); `bls/models/_client.py:132-138`
  (oversize/empty/BOM), `:145` (HTML), `:152-158` (required header columns), `:170-174` (control
  characters), `:320-330` (a 200 whose `status != "REQUEST_SUCCEEDED"` or whose `message` list is
  unknown), `:416-419` (message-sniffed `BLS_INVALID_SERIES_ID`);
  `ecb/models/_client.py:113` (CSV header set), `oecd/models/_client.py:147`-area (same),
  `ecb/models/_reference_rates.py:121` (`<!DOCTYPE`/`<!ENTITY` refused before parsing);
  `fmp/models/equity_quote.py:55-56` (`FMP_QUOTE_EXPECTED_ONE_ROW`).
- **Smallest surface** `ModelSpec.guards: tuple[Guard, …]` with a closed vocabulary:
  `reject_html`, `reject_bom`, `reject_control_chars`, `max_response_bytes`, `max_records`,
  `required_columns=(…)`, `error_envelope_keys=("Error","error","message")`,
  `truncation_by_parse_position`, `success_field=("status","REQUEST_SUCCEEDED")`,
  `expected_rows="one"|"many"`, and for XML `forbid_dtd_or_entity`. Every one of these is a decision
  AC2-10's "空响应与缺数区别" and AC2-22's parse-risk case already ask for.

### K9 — Row identity, dedupe and conflict codes
- **Unblocks** 7 gated as recorded by the original read — **not re-derived here**: one of its four
  supporting sites was withdrawn by the verifier (below), so if that site was one of the seven the
  real figure is 6. §8 records this as an unverified count.
- **Sites** `fmp/models/equity_historical.py:168-172` (same date twice with different values ⇒
  `FMP_CONFLICTING_DUPLICATE_DATE`, identical duplicates collapse), `bls/models/_client.py:264-272`
  (cross-window merge keyed by `(series,date)`, conflict at `:269`), `:363-381` (in-response identity
  dedupe, `BLS_OBSERVATION_CONFLICT` at `:378`), `fred/models/_paged.py:158-168` (json-key identity
  built at `:45`/`:68`, conflict at `:165`), `bls/models/_client.py:194` (catalog duplicate id ⇒
  `BLS_CATALOG_DUPLICATE_CONFLICT`).
  **Withdrawn:** the draft's `ecb/models/reference_rates.py:152-155` ("one row per quote currency,
  duplicates collapsed") cannot exist — that file is 134 lines, and what it does at `:125-133` is
  *refuse* (`ECB_BAD_OBSERVATION`) any row whose quote currency was not requested or whose date is
  outside the window. That is a K7 window/policy refusal, not a K9 dedupe: the ECB leg belongs to
  K7, and no ECB model is shown collapsing duplicates.
- **Smallest surface** `ModelSpec.row_identity: tuple[str, …]` + `on_duplicate:
  "collapse"|"reject:<CODE>"`. Deliberately no "last wins" and no "first wins": both silently pick a
  value, which is what AC2-18 exists to forbid.

### K10 — Governed strict paging
- **Unblocks** 4 gated (`FredSearchFetcher`, `FredSeriesFetcher`, `FredSofrFetcher`,
  `FredSoniaFetcher`) and is the foundation K13's bisection reuses.
- **Sites** `fred/models/_paged.py:101-103` (a repeated request offset is a failure), `:121-131`
  (the echoed `offset` must equal the requested one and must not repeat), `:140-147` (**caps
  preflight**: from the first page's `count`, compute whether the whole answer fits
  `max_records`/`max_pages` and refuse *before* sending, with `limit_kind` and `maximum` in the
  error, `:16-24`), `:151-153` (**exact** page length `min(limit, count-offset)` — a short page is
  an error here, not a terminator, `:170-174` is the real terminator), `:160-168` (dedupe, K9),
  `_fixed_observation_pages.py:31-47`+`:348-382` (13 required page fields, each echoed value bound
  to the query), `:133-161` (scope authorization before the first request, per-page liveness
  re-check), `:176-197` (JSON depth pre-scan on the text), `:248-266` (per-page serialized byte
  budget), `:273-279` (duplicate JSON keys refused).
  Engine today: `cursor_field` is read at the document **root** only (`_next_cursor:389-399`),
  `total_key` likewise (`_declared_total:466-477`) and never drives the loop, termination is
  short-page (`:433` sizes the page, `:458` compares) or cursor-absent (`:451-453`), and the only
  bounds are post-hoc `max_pages`/`max_rows` ⇒ `INCOMPLETE` (`:449`, `:456`, `:462`).
- **Smallest surface** `PaginationSpec` + `echo_fields=("count","offset","limit")`,
  `row_cursor_path`/`total_count_path` accepting a dotted pointer, `page_count_param: str | None`
  (send it or suppress it), `termination="short_page"|"count_driven"|"cursor"`,
  `page_shape="exact"|"at_most"`, `duplicate="reject:<CODE>"`,
  `max_request_bytes`, `max_json_depth`, `reject_duplicate_json_keys`.

### K11 (re-scoped) — Header values that are not constant, and credentials outside the query
- **What already shipped:** the constant half of this item is **done** — `6f12c4e` added
  `ModelSpec.static_headers` (`_engine/spec.py:207`, validated `:260-277` against `_HEADER_TOKEN`
  `:37`), `fetch_pages` builds it once and passes it on **every** page (`http_json.py:422`, sent at
  `:434-440`), `_http_get_json` forwards it (`:286`, `:292-298`), the three test transports log it
  (`testing.py:193`), and the contract suite both checks the rules (`:744-769`) and asserts
  "on every page" (`:579`). ECB's `Accept: application/vnd.sdmx.genericdata+xml;version=2.1` is a
  module constant (`ecb/models/_reference_client.py:30`, sent `:94`; `ecb/models/_series_client.py:42`,
  sent `:98`), so it is declarable **today**: the three ECB XML legs gate on K12 (the reader), not on
  a header slot.
- **What remains** — two things `static_headers` cannot express:
  (1) a header whose **value is a credential**, i.e. read from the environment at request time.
  Sites: `ths/transport/http_client.py:55-56` (`API_KEY_HEADER = "X-api-key"`), applied to the
  request at `:341`/`:364` and to the raw-response cache key at `:252` — the cache partition needs
  the header value, which is exactly why a credential cannot be baked into a declaration constant.
  (2) a **credential in the body**: `bls/models/_client.py:252` (`payload["registrationkey"]`, with
  `Content-Type` at `:99` and `json_body` at `:102`).
  `ModelSpec.credential`/`credential_query_key` (`spec.py:201-202`) and `fetch_pages:419-420` put the
  credential in the query and nowhere else. No model in the 44 was found to vary a header *per page*,
  so that half is dropped rather than claimed.
- **Unblocks** 1 gated (`BlsSeriesFetcher`, via the body credential; it also needs K13's POST) and is
  a *precondition* for the 11 THS models, which reach the gateway only through K14's transport.
- **Smallest surface** `ModelSpec.credential_in: "query"|"header"|"body" = "query"` plus
  `credential_header: str | None`. **No new header field**: `static_headers` is the header field, and
  widening it to read the environment would put a secret in the review record, which is what the
  provenance plane (`scripts/quality/declaration_provenance.py`) exists to prevent.

### K12 — SDMX-ML 2.1 GenericData reader
- **Unblocks** 3 gated (`CurrencyReferenceRates`, `YieldCurve`, `BalanceOfPayments`).
- **Sites** `ecb/models/_reference_rates.py` (the whole file: `DefusedXMLParser` at `:21-22`, DTD
  refusal at `:121`, node/observation/output budgets at `:469-562`),
  `ecb/models/_series_sdmx.py:23-50` (the 7-tuple yield-curve and 17-tuple BPS dimension orders),
  `:61-68` (four raw attribute layers: dataset / series / observation / group context), `:74-94`
  (dimension values validated against the tuple, then the series key **derived** by joining them),
  `:97-119` (**two** per-dataset period grammars, not one annual-vs-monthly switch:
  `_validate_yc_period:97-106` requires a canonical **daily ISO date** with frequency `B`, while
  `_validate_bps_period:109-119` requires `YYYY-MM` for `M` and `YYYY-Qn` for `Q` — the draft's
  "`YYYY-A` vs `YYYY-MM-A`" described neither), `:133-184`.
- **Smallest surface** `ModelSpec.payload="sdmx-ml"` + `PayloadSpec(sdmx_dimensions=(…),
  sdmx_attribute_layers=(…), node_budget, observation_budget, output_budget)`; keep K1's
  `required_columns` idea honest by *not* pretending a flat column list covers nested attribute
  maps. This is the one item that may legitimately be deferred: 3 models, XL complexity, and the
  XML reader is already written.

### K13 — Multi-request composition: depends-on, batch matrix, cap bisection, POST
- **Unblocks** 2 gated (`EquityHistoricalFetcher`, `BlsSeriesFetcher`) and 3 of the cboe nine
  (`EquityHistorical`, `EtfHistorical`, `EquityQuote`); also the shape the 22 C-class models need.
- **Sites** `fmp/models/equity_historical.py:70-121` (window deque + bisection on the 5000-row cap)
  with `_split_window:178-189`; `bls/models/_client.py:89-102` (the POST itself —
  `http_client.py:455` refuses `json_body` for GET), `:230-234` (v1/v2 by credential, series×year
  matrix), `:235-240` (matrix preflight); `ths/models/_client.py:108-140` (a search request whose
  unique answer becomes this request's path key, fail-closed otherwise);
  `ths/endpoints.py:198` (10-year chunking) and `:1473-1509` (dividends fetched **first** and joined
  into bars); `cboe/specs.py:111` ("pre-flight index directory decides the URL"), `:114` ("four
  requests joined on symbol; percent columns scaled after the call" — the draft's `:113` is
  `EtfHistorical`'s alias line).
- **Smallest surface** `ModelSpec.method: "GET"|"POST"`, `ParamSpec.in_: "query"|"body"`,
  `ModelSpec.depends_on: DependencySpec(model=<sibling spec id>, bind="<param> ← <column>",
  require="exactly_one"|"fail:<CODE>")`, `ModelSpec.request_plan: "single"|"batch:<param>"
  |"window_chunk:<days>"|"cap_bisected:<row_cap>"`, `ModelSpec.max_requests` with a
  `requests_made`-carrying code (needs K16). Composition by *model id*, never by hand-written URL
  text, is the whole point: the second request must stay reviewable from the record.

### K14/K15 — The other transport families (envelope gateway / SDK)
- K14 `payload="envelope"` + `EnvelopeSpec(code_key, success_code=0, message_key, request_id_key,
  rows_at="data.item", document_timestamp_at="data.timestamp")` +
  `upstream_error_map="resource:ths_error_codes.yaml"` (`ths/transport/envelope.py:26-46`,
  `error_for_upstream_code`), plus K11's header credential and K16's scope/attempt accounting.
  Gates all 11 THS models.
- K15 `transport="sdk"` with an adapter protocol (`extract_data → list[Mapping] | Frame`), covering
  akshare's vendored `_vendor` calls (`akshare/models/stock_daily.py:66-77`, all 10 models) and
  yfinance's `_sdk.require_history_frame` (`yfinance/models/stock_daily.py:228-237`).
- These are listed last on purpose (§6): they are the highest *touched* counts in the table and the
  lowest *gated* ones, because 22 of the 44 cannot reach the JSON engine at any price. A backlog
  that ranked them #1 would be ranking by row count while shipping a second engine.

### K16 — Budget, deadline and attempt accounting inside the engine loop
- **Unblocks** 6 gated directly (FMP historical, BLS series, and the four FRED pagers — the draft
  wrote "5" while listing six entries) and is the item
  that makes the *other 38* honest: `fixture_context` (`_engine/testing.py:119`) already hands every
  declared model a real `RequestBudget` carrying a `RequestGrant` for `(source, canonical_model)`
  (`:136-153`), and
  `fetch_pages` never charges it (`http_json.py:417` reads `ctx` only for `timeout`, never for
  `remaining_ms`/deadline/cancellation — the machinery exists in `protocol.py:93-120`).
- **Sites** `fmp/models/equity_historical.py:80-85` (budget reached, with `requests_made`),
  `bls/models/_client.py:235-240` (pre-flight refusal, `required_requests`),
  `fred/models/_fixed_observation_pages.py:133-161` (scope authorization + per-page liveness),
  `ths/transport/http_client.py:39-43` (`reserve_scoped_attempt`, `scoped_host_slot`),
  `ecb/models/_reference_client.py:35-47` (per-model scope check).
- **Smallest surface** `ModelSpec.request_plan.max_requests` + engine-side: one budget charge and
  one `ctx` deadline/cancellation check **per page** inside `fetch_pages`, and an
  `attempts_used`/`requests_made` context field on `INCOMPLETE`-class errors. Without K16, landing
  K10/K13 means shipping an engine that provably cannot answer AC2-15's "逐源/全局/host 预算最小
  上限有效" or AC2-16's cancellation claim for the very models whose paging it just enabled.

### K17 — Post-fetch algebra (explicitly **not** proposed)
Unit scaling across two steps (akshare lots→shares is carried by K2's `scale`), THS's
`unadjust_bars` dividend join (`ths/endpoints.py:1423-1470`: prices minus the sum of cash dividends
whose ex-date is **strictly later** than the bar), yfinance's cumulative-product split undo
(`yfinance/models/stock_daily.py:98-150`), the pivot-registry wide→long melt
(`ths/endpoints.py:1041-1057`, `akshare/models/financial_statement.py:95`). A declaration that
admits arithmetic on values re-opens AC2-18's "which value won" question with no adjudicator, so
these 9 models stay bespoke by design (§8).

## 6. Ranked backlog

Ratio = `gated ÷ complexity units` (S=1, M=2, L=3, XL=5). `cum.` = fully declarable hand-written
models available once this item and everything above it in the **recommended build order** (§7) has
landed — that column is why the ranking and the build order differ (§7).

| # | id | capability | Cx | gated | touched | cum. models declarable | serves |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | K4 | `row_order` (whole-result sort) | S | 17 | ~30 | 0 | AC2-10 (typical request), AC2-17 |
| 2 | K2 | `ColumnSpec.decode` registry + typed decode errors | M | 16 | 36 | 3 | AC2-10 (字段/类型/单位), AC2-13 |
| 3 | K3 | separators, fan-out, path charset, epoch-millis render, patterns | M | 12 | 15 | 8 | AC2-10, AC2-13 |
| 4 | K1 | `payload="csv"/"tsv"` + dialect + required columns | M | 5 | 7 | 8 | AC2-10, AC2-14 |
| 5 | K5 | `ColumnSpec.source` (query/constant/lookup/join/echo) | L | 14 | 27 | 8 | AC2-10, AC2-17 |
| 6 | K6 | `rows_shape="record_mapping"` + templated pointer | S | 3 | 3 | 11 | AC2-10 |
| 7 | K7 | window + parameter policy (`filter`/`reject`/`together`/`forbidden`) | M | 10 | 32 | 11 | AC2-19, AC2-13 |
| 8 | K8 | response guards (`reject_html`, BOM, size, error envelope, truncation, expected rows, DTD) | M | 9 | 12 | 12 | AC2-10, AC2-22 |
| 9 | K9 | `row_identity` + `on_duplicate` code | M | 7 | 12 | 16 | AC2-18, AC2-10 |
| 10 | K10 | governed strict paging (echo bind, exact length, caps preflight, dotted total/cursor, budgets) | L | 4 | 4 | 16 | AC2-11, AC2-22 |
| 11 | K16 | budget/deadline/attempt accounting in `fetch_pages` | M | 5 | 44 | 18 | AC2-15, AC2-16, AC2-22 |
| 12 | K13 | composition: POST/body, depends-on, batch, chunk, bisection | XL | 2 | 8 | 18 | AC2-11, AC2-22 |
| 13 | K11 | `headers` + `credential_in="header"/"body"` | S | 3 | 14 | 21 | AC2-09 |
| 14 | K12 | SDMX-ML GenericData reader + dimension/attribute declaration | XL | 3 | 3 | 21 | AC2-10 |
| 15 | K14 | business-envelope transport (code/message/request_id/rows_at + upstream code map) | L | 0 | 11 | 21 | AC2-03, AC2-22 |
| 16 | K15 | SDK transport (`transport="sdk"`) | XL | 0 | 11 | 21 | AC2-05, AC2-20 |
| — | K17 | post-fetch algebra — **not proposed** | XL | 0 | 9 | — | AC2-18 (reason to refuse) |

Two readings of this table, both honest. **By ratio**, K4 (one field, one sort) and K2 gate more
models than anything else here. **By what actually converts**, ranks 1-5 deliver 3 models (the FRED
macro trio) and ranks 6-13 deliver the remaining 18 — the long tail is expensive because each model
needs *all* of its own row, not one more generic knob. If the goal is "most models declarable per
quarter", build §7's order; if it is "most future providers absorbed per line of engine code",
K4/K2/K3/K1/K5 are the five to fund.

## 7. Build order and what each step makes possible

The order below is dependency-ordered, so `cum.` rises at every step (the §6 ratio order would land
K4 before K2 and convert nothing for a whole sprint).

| wave | land | newly declarable | cum. (of 44) |
| --- | --- | --- | --- |
| 1 | K4 + K2 + K3 + K1 + K5 | `EcbCpiFetcher`, `EcbGdpFetcher`, `EcbRateFetcher`, `OecdCpiFetcher`, `OecdUnemploymentFetcher`, `FredCpiFetcher`, `FredGdpFetcher`, `FredUnemploymentFetcher` | 8 |
| 2 | K6 + K7 | `ImfCpiFetcher`, `ImfGdpFetcher`, `ImfUnemploymentFetcher` | 11 |
| 3 | K8 | `EquityQuoteFetcher` (guards + `expected_rows="one"` + window refusal) | 12 |
| 4 | K9 + K10 | `FredSearchFetcher`, `FredSeriesFetcher`, `FredSofrFetcher`, `FredSoniaFetcher` | 16 |
| 5 | K16 + K13 | `EquityHistoricalFetcher` (cap-bisected window + budgets), `BlsSeriesFetcher` (POST + matrix) | 18 |
| 6 | K11 + K12 | `CurrencyReferenceRatesFetcher`, `YieldCurveFetcher`, `BalanceOfPaymentsFetcher` | 21 |

Wave 1's eight are the ones worth funding first for a different reason than the count: they are all
`economy_cpi`/`economy_gdp`/`economy_rate`/`economy_unemployment` domain rows — the 42+9+3
`existing_domain_candidates` groups in the ledger CSV — so they are also the cheapest way to get the
contract suite judging a *family* rather than two cboe snapshots.

What each wave additionally unlocks in the **shadow** ledger (the 9 cboe `NOT_DECLARABLE`, which is
the only existing record of "why not"): wave 1 makes `EquitySearch` declarable (K1) and
`IndexHistorical`/`IndexSnapshots` declarable **if** K3 also grows a
`path_by`/`columns_by` branch (parameter-selected URL root — one enum-keyed map, the same shape as
K6's templated pointer); wave 2 makes `IndexSearch` declarable (K7's `filter_rows` with a
`contains` predicate); waves 4-5 make `EquityHistorical`/`EtfHistorical` (preflight directory) and
`EquityQuote` (four-way join, percent scaling) declarable **if** K13's `depends_on` is allowed to
bind a *collection* parameter (4 symbols, one request each, joined on `symbol`).
`OptionsChains` (regex over the contract symbol, strike ÷ 1000, columnar arrays) and
`FuturesCurve` (delegates to two other fetchers) stay unconvertible.
