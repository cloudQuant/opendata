# C72 — cboe 两条引擎声明的权利登记、路由与判定面

Wave: 迭代2 / 统一 Provider 架构 / declarative engine + cboe.
Tree at write time: branch `dev`, HEAD `2c21b68 fix(registry): C72 注册顺序改为派生…`, working tree
carrying this wave's uncommitted diff until the commit that follows this file.

Everything below is a recomputation, not a recitation: each PASS line is verbatim from
`docs/evidence/C72/counterfacts.txt`, each plane counter from
`docs/evidence/C72/inventory-plane-after-cboe-rights.txt`, and every count named in §6 was measured
in the same pass that wrote it.

## 1. What this wave shipped

| face | change | carrier |
| --- | --- | --- |
| cboe rights registration | §1 row **14** added to the registration table: 已复核（受限）, all three use columns 暂不批准, each derived from a quoted sentence of Cboe's own public terms | `docs/data-rights-registry.md`, `docs/evidence/C72/cboe-rights-review.md` |
| inventory trace | `rights_rows: [Cboe（cdn.cboe.com 公开接口）]` plus the reconciled census (`registered_source_count: 10`, `registered_capability_count: 46`, `model_implementation_task_status_counts: {IN_PROGRESS: 15, NOT_RUN: 335}`) | `docs/proposals/openbb-migration/provider-inventory.yaml` |
| routing | `resolve_domain` now honours the asked-for source's **own** asset class instead of the domain's first leg | `opendata/data/registry.py` |
| attributable refusal | `_QUERY_INVALID` names the rejected keys (never their values); `ProviderEngineError` grew `status/url/rejected/missing` fields — at HEAD the word `rejected` appeared only in a docstring (1 hit), now 7 | `opendata/data/providers/_engine/http_json.py` |
| serving strictness (judged, not shipped here) | a row is served only if `type(row)` **is** the domain's contract class, subclasses refused. This rule is already committed (`3f05f63`, `provider_model_query.py:324`); this wave supplies the faces that prove it bites (F3/F4) | `opendata/services/provider_model_query.py` (unchanged) |
| AC-10 admission | the two cboe model ids and their `scenario` strings entered the map pins, so the auto-promoted descriptors are named rather than tolerated | `tests/test_openbb_map.py`, `tests/test_openbb_map_registry_projection.py` |
| live domain declarations | `cboe_available_indices` / `cboe_index_constituent_quotes` (snapshot, transient, `permissions: [query]`, `natural_key: [symbol]`) | `opendata/data/domains.yaml`, `opendata/data/models/cboe_index.py` |
| new judgement faces | F1–F7 in the counterfact harness, `test_every_declared_rights_row_is_load_bearing`, the AC-1\|08 cell-level extension | `docs/evidence/C72/counterfacts.py`, `tests/test_openbb_inventory_guard.py`, `tests/test_acceptance_item_probe.py` |

Denominators this wave had to keep whole: 34 provider directories, 350 ledger rows, 46 fetcher
classes, 13 reviewed domains, 25 AC2 cases, 130 applicable iteration-1 ACs. None moved; the ledger
only flipped `implementation_task_status` on the two cboe rows (`13 → 15 IN_PROGRESS`, `337 → 335
NOT_RUN`).

## 2. The counterfact harness: seven faces, each two-sided

`python3 docs/evidence/C72/counterfacts.py` — every face must show the defect firing **and** the
fixed behaviour holding, against the repo's own judges (`resolve`/`resolve_domain`, the real
`make_http_json_fetcher` output path, the real `_reviewed_query_contract`, the real provenance
self-test, the real `openbb_inventory_plane.findings`).

```
PASS  F1 resolve_domain routes by the asked-for source's own asset class
PASS  F2 an undeclared parameter is refused by naming every rejected key
PASS  F3 rows are the domain's contract class, and only that class is served
PASS  F4 a boolean is refused for a numeric column on a contract row
PASS  F5 the provenance audit's domain checks arm against the live registry
PASS  F6 the capability census holds as an identity across two enumerations
PASS  F7 every declared rights row is load-bearing for its serving source
FACES=7 PASS=7 FAIL=0
HARNESS_EXIT=0
```

The detail lines carry the measurement (see `counterfacts.txt`); the three that judge most:

- **F1** registers two sources for one domain under *different* asset classes synthetically (no two
  live sources collide today), then shows the old first-leg formula raising
  `LookupError(source 'probe_b' has no registered capability for index/probe_shared_domain …)` while
  both legs route correctly through the real registry.
- **F6** re-derives the census twice: `registered=46 = hand 44 + engine 2` over `10` sources, with
  `domains=33 reviewed=13` and a duplicate key refused. The identity is the claim — a single
  enumeration could be wrong and still print a number.
- **F7** walks all 13 declared `rights_rows:` links against the 14 §1 rows and deletes each row in
  turn: `each of the 13 deletions reddened only its declaring source; baseline green`. It also pins
  that the §1 numbering is a contiguous `1..N` set, so a renumbered table cannot silently orphan a
  link.

## 3. The inventory plane: 7 findings → 0, with the "before" kept

`RIGHTS LINK` went red *because* cboe was registered as a serving source with no rights row — the
guard caught this wave's own omission. The reproduction of the before-state is kept on purpose:

```bash
# after (this wave)
python3 scripts/quality/openbb_inventory_plane.py          # PLANE_EXIT=0, all 15 counters 0
# before (the parent commit's copy of the same document, judged against today's runtime)
python3 scripts/quality/openbb_inventory_plane.py \
  --inventory docs/evidence/C72/provider-inventory.head.yaml   # EXIT=1, 7 findings
```

```
STALE STATUS   1   RIGHTS LINK   1   CAPABILITY COUNT   3   MODEL TASK STATUS   2
```

Both readings are archived: [inventory-plane-after-cboe-rights.txt](inventory-plane-after-cboe-rights.txt)
and [inventory-plane-on-head-baseline.txt](inventory-plane-on-head-baseline.txt), with the pinned
baseline copy in [provider-inventory.head.yaml](provider-inventory.head.yaml). A third record,
[inventory-plane-after-repin.txt](inventory-plane-after-repin.txt), is the run taken **after** the
six census re-pins of §6 — the plane reads the same 15 counters at 0 (`PLANE_EXIT=0`), which is the
check that re-pinning the alert-matrix and registry faces did not touch what the inventory plane
judges: it declares `generated:` / `branch=dev` / `HEAD=2c21b68` in its header, so all three records
are attributable to the same commit. Declaration provenance is separately green:
[declaration-provenance.txt](declaration-provenance.txt) prints
`models=2 findings=2 violations=0 info=2 self_test=9/9` and
`VERDICT: DECLARATION PROVENANCE HOLDS` (the 2 findings are `info`, the arms are the harness's own).

## 4. Repo-suite faces

| suite | reading | what it proves |
| --- | --- | --- |
| `tests/test_openbb_inventory_guard.py` | 34 passed | the 15 plane rules over a synthetic corpus **plus** the new load-bearing rights face |
| `tests/test_acceptance_item_probe.py` | 42 passed | AC-1\|08 now judges row 14 cell by cell (数据源/条款链接/三列用途/状态/日期) and the §1.1 prose, not just `rows == 14` |
| `tests/test_openbb_map.py` + `tests/test_openbb_map_registry_projection.py` | 24 passed | AC-10 admission: the two cboe ids are confirmed names with real `scenario` strings, and both legs stay `verified=False` |
| `tests/test_provider_model_contracts.py::TestDeclarationIsTheContract` | 144 passed, 30 skipped (the file as a whole reads 186 passed, 30 skipped) | the parametrised engine contract over every registered `ModelSpec`; the four remaining classes contribute the other 42 passes |

Every skip names a declaration the leg does not have — `AvailableIndices declares no credential`,
`… publishes no total`, `… addresses its resource in the query string` — so they split by what is
*absent from a record*, not by an unfed branch. Measured distribution of the 30: **15 fall on the
two live cboe legs** (`AvailableIndices` 10, `IndexConstituents` 5) and **15 on the five in-file
synthetic probe legs**. The cboe pair is complementary rather than uniformly thin: `AvailableIndices`
is skipped for `declares no numeric column` while `IndexConstituents` runs that arm, and
`IndexConstituents` is skipped for `has no required column the record alone must publish` while
`AvailableIndices` runs it — so each live cboe declaration is still exercised on every face its own
record can reach. §2's F4 is the numeric-column arm asserted on a contract row from outside the
parametrisation.

## 5. Three defects the judges found (not the ones I went looking for)

1. **`*_SHAPE_INVALID` was not attributable.** An engine refusal could fire without naming the URL
   or the rejected keys, so a reader could not tell "your parameter" from "their payload". Fixed by
   carrying `status/url/rejected/missing` on `ProviderEngineError` and naming every rejected key;
   F2 shows the refusal `code=CBOE_QUERY_INVALID rejected=['probe_absent_one', 'probe_absent_two']`
   while a valid query still serves 1 row.
2. **F6's own legacy enumeration had a counting bug.** The first version of the census face summed
   one enumeration by a join key that does not exist for legs without a `canonical_model`, so it
   could have printed a matching 46 while missing a source. Rewritten to derive both sides from the
   runtime and assert the identity.
3. **The registration landed ahead of its rights trace, and silently moved two AC-10 pins.** Adding
   cboe as a serving source reddened `RIGHTS LINK` (correctly), and `_project_default_map()`
   auto-promotes *any* registered descriptor, which broke
   `test_confirmed_models_are_never_guesses` (Extra items `AvailableIndices`, `IndexConstituents`)
   and `test_legacy_pending_aliases_and_old_scenarios_are_not_promoted` (`assert 13 == 11`). Both
   pins were **extended**, not loosened: the names are now confirmed because they appear in the
   pinned upstream inventory's cboe `models:` line, and the scenarios are declared.

## 6. The six census pins this wave's domain registration outran

Registering cboe's two domains made its two engine legs *registrable*, so every face that counts the
registry moved by the same +2. The full offline suite before the re-pin:

```
6 failed, 20826 passed, 121 skipped in 388.17s (0:06:28)
```

| failing face | before → after | why it is a census, not a defect |
| --- | --- | --- |
| `test_alert_matrix.py::TestCollection::test_every_leg_is_measured_or_counted_never_dropped` | `len(legs)` 31→33, deferred 11→13 names, total legs 44→46 | both cboe domains are snapshot/transient with no legacy table, so they join the **deferred** side; `scope.domains` stayed 20 |
| `test_alert_matrix.py::TestCollection::test_a_leg_with_no_field_mapping_is_the_measured_majority` | total legs 44→46, `sum(legs[d] for d in deferred)` 11→13 | the *measured* side is untouched: `sum(legacy_legs) == 33` and `len(unmapped) == 23` still hold |
| `test_alert_matrix_model_domains.py::test_default_scope_accounts_for_all_domains_legs_and_only_queries_legacy_tables` | 31→33, 44→46, `deferred_legs` 11→13, `scope_dict["deferred_legs"]` 11→13 | `len(statements) == len(reports) == 30` unchanged, and the "no `dwd_`/`ods_` statement for a native domain" walk now covers cboe too |
| `test_authority_reconciliation.py::…::test_domains_without_a_row_are_exactly_the_single_source_ones` | `NATIVE_MODEL_DOMAINS` +2 names | the guard then **judges** them: 1 descriptor each, `semantics_declared`, `query` permission, `descriptor.model == canonical_model`, full identity match, `verified is False`, no planned `dwd_`/`ods_` table |
| `test_ecb_reference_registration.py::test_real_ecb_bindings_register_three_canonical_models_without_auto_promotion` | `capabilities()` 44→46, `descriptors` 11→13 | 22 passed in that file after the change; `participates_in_auto()` is still **23**, the number that matters for routing |
| `test_pipeline_jobs.py::TestPartitionMaintenanceJob::test_the_registered_census_reaches_the_ods_layer` | `known` 31→33, legs 44→46, `deferred_domains` 11→13, `deferred_legs` 11→13 | `measured_domains` 20, `expected_ods` 33, `expected_dwd` 20 and `set(ods) == expected_ods` all unchanged — the落库 plan did not grow |

The invariants that did **not** move are the whole argument: 20 legacy-measurable domains, 33
measured legs, 33 ods tables, 20 dwd tables, 30 statements, 23 auto-routable capabilities. Only the
native-model/deferred side grew, which is exactly what `storage_mode: transient` declares. No judge
was widened to accept the new state; two faces were *strengthened* (the ecb test now names cboe's
model ids and asserts both stay unverified, the authority walk now inspects cboe's descriptors).

After the re-pin, the six carriers read:

```
tests/test_alert_matrix.py ...............................        (37 items)
tests/test_alert_matrix_model_domains.py .......                  (7)
tests/test_authority_reconciliation.py ......................      (22)
tests/test_ecb_reference_registration.py .....                    (5)
tests/test_pipeline_jobs.py::…::test_the_registered_census_reaches_the_ods_layer .
72 passed in 1.54s
```

The 72 is a **targeted** selection, not a file total, and the population is `37 + 7 + 22 + 5 + 1`:
`test_pipeline_jobs.py` contributes one node (the re-pinned census test), while the file itself
collects 66. Re-measured on this tree: the same five files run whole read **137 passed**, and §8's
command names the node so a reviewer gets 72 rather than 137. Both readings are the same assertions —
a re-pin changes numbers inside asserts, not the number of items collected.

and the full suite re-run:

```
================ 20832 passed, 121 skipped in 390.51s (0:06:30) ================
SUITE_EXIT=0
```

against the pre-re-pin reading of the same command on the same tree:

```
=========== 6 failed, 20826 passed, 121 skipped in 388.17s (0:06:28) ===========
```

The two readings differ by exactly the six items (`20826 + 6 = 20832`), the skip count is unchanged
at 121, and no file dropped out of collection — a re-pin that had deleted an assertion would show a
smaller passed count, not a larger one. Raw logs: [full-suite.txt](full-suite.txt).

## 7. sec: an honest zero, and the limit of the face that measured it

`opendata/data/providers/sec/` registers **0** fetchers (measured: `sec_fetchers 0`, and
`len(list_providers()) == 34` includes it). `scripts/quality/declaration_provenance.py --source sec`
therefore exits 1 with `NO_MODELS_DECLARED`. That refusal is correct and stays: a source with no
declared model cannot be audited for declaration provenance, and filling the face with a fixture
would fabricate a declaration. `sec` remains **NOT_RUN** in the ledger and its model slice stays
undone; the zero is a *declared* zero, not a surveyed one.

## 8. Reproduce

```bash
python3 docs/evidence/C72/counterfacts.py                      # FACES=7 PASS=7 FAIL=0
python3 scripts/quality/openbb_inventory_plane.py              # PLANE_EXIT=0, 15 counters 0
python3 scripts/quality/declaration_provenance.py              # DECLARATION PROVENANCE HOLDS
python3 scripts/quality/openbb_inventory_plane.py \
  --inventory docs/evidence/C72/provider-inventory.head.yaml    # EXIT=1, 7 findings
python3 -m pytest -q --no-cov tests/test_openbb_inventory_guard.py tests/test_acceptance_item_probe.py \
  tests/test_openbb_map.py tests/test_openbb_map_registry_projection.py
python3 -m pytest -q --no-cov tests/test_alert_matrix.py tests/test_alert_matrix_model_domains.py \
  tests/test_authority_reconciliation.py tests/test_ecb_reference_registration.py \
  tests/test_pipeline_jobs.py
```

The "before" reading is taken from the pinned copy in this directory, **not** from `git show HEAD:`.
That command only reproduces while `2c21b68` is HEAD; the commit that ships this wave carries the
registered inventory, so `git show HEAD:docs/proposals/openbb-migration/provider-inventory.yaml`
afterwards yields the *after* state and exits 0 — a reproduce command that changes meaning with the
tree is not a reproduce command. `provider-inventory.head.yaml` is the byte copy of the parent
commit's document, and it is what `inventory-plane-on-head-baseline.txt` was run against.

## 9. Deferred to the authorization queue (unchanged by this wave)

Blocked inside the denominator, not dropped from it:

- **SOURCE_VERIFIED** for cboe, sec and the other `NOT_RUN` slices — no real provider call was made;
  public-terms review is not a live fetch.
- **FMP and Cboe use authorization**: all three use columns are 暂不批准 (Cboe requires prior written
  consent for copying/storing; FMP's project-level subscription/multi-user licence is not held).
  `verified=False` keeps both out of `source="auto"`.
- **AC-13 `dwd_*` 落库**, **AC-15 `DROP`** (1,025 legacy tables stay read-only; the
  `legacy_transfer.py --drop --yes` codemod was not run), the 8 tables missing `p2027`
  (`ALTER TABLE … REORGANIZE PARTITION`), the disk-water-level alarm's measurement target, any
  warehouse DDL, dependency pinning / CI lock file, and Docker engine start — all need explicit user
  confirmation.
- **AC-19**运维五格 remains 2/5 with three authorization-blocked gaps (C63's reading, not re-opened
  here).
- `git push` — not performed.

## 10. What this wave deliberately left for the next one

The engine still declares 2 of 46 bindings, and the survey of *why* is C70, not this file:
[../C70/engine-capability-backlog.md](../C70/engine-capability-backlog.md) §5/§6 rank the 16 missing
capabilities (K1…K16) and §7 gives the dependency-ordered build waves. C70's
[addendum](../C70/addendum-c72-wave.md) records the correction that §3's cohort headings sum to 43
rather than 44 — three ECB XML legs shelved out of §3 although §5's K12 does cover them, and two
headings each counting a shared transport client twice — and re-measures the §0 census at `2c21b68`.

Wave 1 of that plan (K4 + K2 + K3 + K1 + K5) converts 8 macro legs. The ledger still reads
`implementation_task_status: {IN_PROGRESS: 15, NOT_RUN: 335}` over 350 rows, so the denominator is
intact and no row was promoted by prose.
