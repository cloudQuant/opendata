# C70 addendum — the §0 census re-run, and §3 does not partition the 44

Written at `2c21b68` (branch `dev`) on 2026-10-09, after the C72 wave (cboe rights registration, the
`_QUERY_INVALID` attribution, the derived registration order). This file **appends** to
[engine-capability-backlog.md](engine-capability-backlog.md) and changes nothing in it; every
citation below is a line of that file as it stands.

## A.1 The §0.1 census, re-measured in one pass

```bash
git rev-parse --abbrev-ref HEAD && git rev-parse --short HEAD        # dev / 2c21b68
PYTHONPATH=. python3 - <<'PY'
import csv, inspect
from collections import Counter
from pathlib import Path
from opendata.data.providers import catalog

bindings = [(p.source, type(f).__name__, getattr(type(f), "model_spec", None) is not None)
            for p in catalog.list_providers() for f in catalog.fetchers_for(p.source)]
print("providers_in_catalog", len(catalog.list_providers()))
print("TOTAL", len(bindings), "ENGINE", sum(b[2] for b in bindings), "HAND", sum(not b[2] for b in bindings))
print("verified", Counter(bool(getattr(f, "capability").verified)
      for p in catalog.list_providers() for f in catalog.fetchers_for(p.source)))
print("canonical_non_null", sum(1 for p in catalog.list_providers() for f in catalog.fetchers_for(p.source)
                                if getattr(type(f), "canonical_model", None) is not None), "of", len(bindings))
print("engine_declared", [(s, spec.model) for s, spec in catalog.engine_declared_models()])
print("sec_fetchers", len(list(catalog.fetchers_for("sec"))))
rows = list(csv.DictReader(Path("docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv")
                           .read_text(encoding="utf-8").splitlines()))
print("ledger_rows", len(rows))
print("impl_status", dict(Counter(r["implementation_task_status"] for r in rows)))
print("live_status", dict(Counter(r["live_verification_status"] for r in rows)))
print("scenario_nonempty", sum(1 for r in rows if r.get("scenario", "").strip()))
PY
```

Real output:

```
providers_in_catalog 34
TOTAL 46 ENGINE 2 HAND 44
verified Counter({True: 23, False: 23})
canonical_non_null 13 of 46
engine_declared [('cboe', 'AvailableIndices'), ('cboe', 'IndexConstituents')]
sec_fetchers 0
ledger_rows 350
impl_status {'NOT_RUN': 335, 'IN_PROGRESS': 15}
live_status {'NOT_RUN': 350}
scenario_nonempty 0
```

| item | §0.1 (`:28-51`) | this pass | moved? |
| --- | --- | --- | --- |
| registered bindings | 46 | **46** | no |
| engine-driven bindings | 2 (both cboe) | **2** (same two names) | no |
| hand-written fetchers | 44, per-source `akshare 10, bls 2, ecb 6, fmp 2, fred 7, imf 3, oecd 2, ths 11, yfinance 1` | **44**, identical per-source split | no |
| `Capability.verified` | 23 `True` / 23 `False` | **23 / 23** | no |
| non-null `canonical_model` | 13 of 46 | **13 of 46** | no |
| provider directories in `catalog` | 34 | **34** | no |
| `sec` fetchers | 0 | **0** | no |
| ledger rows | 350 | **350** | no |
| ledger `implementation_task_status=IN_PROGRESS` | 13 (`:46`, 337 `NOT_RUN`) | **15** (335 `NOT_RUN`) | **yes, +2** |
| ledger `live_verification_status != NOT_RUN` | 0 of 350 | **0 of 350** | no |
| ledger rows declaring a `scenario` | 0 of 350 | **0 of 350** | no |

The one move is the `IN_PROGRESS` count, and it is this wave's own bookkeeping: the two cboe legs
were flipped `NOT_RUN → IN_PROGRESS` when their specs went live, so `13 → 15` and `337 → 335`
(consistent with `docs/proposals/openbb-migration/provider-inventory.yaml`
`model_implementation_task_status_counts: {IN_PROGRESS: 15, NOT_RUN: 335}`). No capability, no
credential and no denominator moved; `34 sources` and `350 provider×model rows` are intact.

## A.2 The finding: §3's cohort headings sum to 43, its title says 44

`## 3. What the 44 hand-written fetchers actually do` (`:432`) declares eleven cohorts, and the
counts in the headings are:

| heading | line | count |
| --- | --- | --- |
| 3.1 Single GET, JSON, query-key credential, list under a pointer | `:436` | 3 |
| 3.2 Single GET with a JSON *object* as the record set | `:452` | 3 |
| 3.3 Single GET returning CSV | `:461` | 5 |
| 3.4 Governed GET with a strict page-identity loop | `:472` | 4 |
| 3.5 Governed GET with response-shape sniffing | `:500` | 2 |
| 3.6 Recursive request splitting plus explicit budgets | `:512` | 1 |
| 3.7 POST, credential in the body, series×year matrix, nested flatten | `:523` | 2 |
| 3.8 TSV catalog with client-side search and a page container | `:539` | 1 |
| 3.9 Bespoke gateway: header credential, envelope, cache, two-step code resolution | `:550` | 11 |
| 3.10 Vendored SDK returning pandas frames | `:580` | 10 |
| 3.11 Third-party SDK plus cross-source reconciliation | `:595` | 1 |
| **sum** | | **43** |

43 ≠ 44. §3 never lists cohort membership per model, so the gap is not readable from the document
alone. It was resolved by joining the modules §3 cites to the 44 classes the runtime actually
registers (the census command in A.1, with `inspect.getsourcefile` per class):

| cohort | models attributable to it | n |
| --- | --- | --- |
| 3.1 | `FredCpiFetcher`, `FredGdpFetcher`, `FredUnemploymentFetcher` (all subclass `_series.FredSeriesFetcher`) | 3 |
| 3.2 | `ImfCpiFetcher`, `ImfGdpFetcher`, `ImfUnemploymentFetcher` | 3 |
| 3.3 | `EcbCpiFetcher`, `EcbGdpFetcher`, `EcbRateFetcher` (`_client.fetch_observations`, `format=csvdata`) + `OecdCpiFetcher`, `OecdUnemploymentFetcher` | 5 |
| 3.4 | `FredSeriesFetcher`, `FredSearchFetcher` (`_paged`), `FredSofrFetcher`, `FredSoniaFetcher` (`_fixed_observation_pages`) | 4 |
| 3.5 | `EquityQuoteFetcher` | 1 |
| 3.6 | `EquityHistoricalFetcher` | 1 |
| 3.7 | `BlsSeriesFetcher` | 1 |
| 3.8 | `BlsSearchFetcher` | 1 |
| 3.9 | the 11 `Ths*Fetcher` legs | 11 |
| 3.10 | the 10 `Akshare*Fetcher` legs | 10 |
| 3.11 | `YfinanceStockDailyFetcher` | 1 |
| **attributable** | | **41** |
| **in no cohort** | `EcbCurrencyReferenceRatesFetcher` (`_reference_client` → `_reference_rates`, SDMX-ML 2.1 GenericData XML), `EcbYieldCurveFetcher`, `EcbBalanceOfPaymentsFetcher` (both `_series_client` → `_series_sdmx`) | **3** |
| **total** | 41 + 3 | **44** |

So the arithmetic closes in both directions, and the off-by-one is two separate errors that nearly
cancel:

- **−3**: the three ECB XML legs have no §3 cohort. They are real — `grep` of their imports shows
  none of them touches `_client.fetch_observations`; `reference_rates.py:15` imports
  `_reference_client.fetch_reference_rates`, whose module pins `ECB_REFERENCE_ACCEPT =
  "application/vnd.sdmx.genericdata+xml;version=2.1"` (`_reference_client.py:30`, sent `:94`) and is
  re-exported as `_series_client.py:42`, while
  `yield_curve.py:13` and `balance_of_payments.py:13` import `_series_client`. **The substance is
  not lost, only mis-shelved**: §5's K12 (`:872-887`) is exactly this reader and cites both
  `_reference_rates.py` and `_series_sdmx.py:23-50`, §5 already counts the legs at `:853` ("the
  three ECB XML legs gate on K12"), the ranked table carries them as row 14 with `3` affected models
  (`:970`), and §7's wave 6 names all three classes. A reader looking for "which hand-written fetchers
  need an XML reader" in §3 finds nothing, which is why §3's sum came out 43.
- **+2**: `3.5` and `3.7` each claim one model more than their own cited modules can support. fmp
  registers exactly two hand-written classes (`EquityQuoteFetcher`, `EquityHistoricalFetcher`) and
  §3.5 cites `fmp/models/_client.py` plus `equity_quote.py` while §3.6 cites `equity_historical.py`,
  so `3.5 (2) + 3.6 (1) = 3 > 2`; bls likewise registers exactly two (`BlsSeriesFetcher`,
  `BlsSearchFetcher`), §3.7 cites `bls/models/_client.py` plus `series.py` and §3.8 cites the same
  `_client.py` (`:116-218`) plus `search.py`, so `3.7 (2) + 3.8 (1) = 3 > 2`. In each pair the shared
  transport client is the module that reads like a second model. The class census is the measurement;
  which of the two headings loses a unit is a judgment about the author's intent, and it does not
  change the total.

Nothing downstream depends on §3's sum: §4 is a dedup list, §5's K items are keyed to citing sites
(not cohort membership), §6's "affected models" column is per-K and was checked against the classes
named in §7, and §7's `cum. (of 44)` column reaches 21 by naming classes, so it never used the §3
headings. **No claim in C70 is retracted by this addendum.** What is corrected is the shape of §3:
it is a mechanism survey with three legs shelved out and two headings inflated, not a partition of
the 44.

## A.3 What this wave changed around the files §3 cites

- `opendata/data/providers/_engine/http_json.py` and `cboe/specs.py` — §0.5 (`:210-227`) recorded the
  in-flight diff (`import sys`, the `ProviderEngineError(..., rejected=…)` field, the
  `_QUERY_INVALID` refusal that names rejected keys, the `EngineFetcher.__module__` stamp) and the
  two cboe `domain` string changes. Both are carried by the commit that ships this addendum, so the
  mapping §0.5 gives (committed → working) now reads `2c21b68` → its child; §0.5's own wording stays
  as it was written, because it describes the tree it measured.
- `docs/data-rights-registry.md` grew §1 row **14** for cboe (`已复核（受限）`, all three use columns
  `暂不批准`), and `provider-inventory.yaml` now carries `rights_rows: [Cboe（cdn.cboe.com 公开接口）]`
  with `registered_source_count: 10` / `registered_capability_count: 46`. This is the AC-1 trace
 面 the inventory guard had no witness for until C72 — see
  [../C72/cboe-rights-review.md](../C72/cboe-rights-review.md).
- The row-count change in §0 does **not** move the 46: cboe's 2 legs were already in the 46 as
  engine legs (`ENGINE 2`), and the rights row is a registry document, not a binding.

## A.4 Reading still owed

`--source sec` still exits 1 via `NO_MODELS_DECLARED` (`sec` registers 0 fetchers, measured in A.1),
so sec's honest zero is a *declared* zero, not a surveyed one: the wave that converts `sec` specs
into engine declarations has not been done, and C70's §1/§3 cohort inventory says nothing about it.
