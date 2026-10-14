# Offline seam forwarding — stale test fakes (C101)

Claim this face supports: every product fetcher that accepts an injected
`transport` forwards it by keyword, and the offline fakes installed by
`monkeypatch.setattr` in `tests/` accept that keyword, so no test reaches a
provider seam through a signature the product no longer has.

Refusal-free here means measured, not asserted: the two probes under
`../probes/` judge the tree, and each carries a falsification arm that must be
able to differ from doing nothing.

## Instruments

| instrument | runs | what it counts |
| --- | --- | --- |
| `../probes/stale_fake_census.py` | `python docs/evidence/C101/probes/stale_fake_census.py` | every `monkeypatch.setattr` site whose patched attribute names a product seam that forwards `transport=` |
| same, `--arm` | `python docs/evidence/C101/probes/stale_fake_census.py --arm` | 7 shapes x (stale, widened) seeds; expects exactly the stale half flagged |
| `../probes/stale_fake_repair.py` | `python docs/evidence/C101/probes/stale_fake_repair.py [--apply]` | imports the census judgment, inserts `**_kwargs` at the parameter-list terminator |
| same, `--arm` | `python docs/evidence/C101/probes/stale_fake_repair.py --arm` | 8 seeds; each installed fake must raise `TypeError` on `transport=` before repair and not after |

Population denominators, printed by the census itself so a zero cannot come from
a shrunken scan (`runs/census-pass2-scoperepair-10sites.txt`):

```
population: seams=47 seam_names=41 seams_forwarded=34
population: setattr_sites=1373 seam_named_sites=166
```

## Arm readings

`python docs/evidence/C101/probes/stale_fake_census.py --arm`, verbatim from
`runs/census-arm.txt`:

```
arm: seeded=14 scanned=14 seam_named=14 unknown_target=0 unknown_fake=0
arm: flagged=['test_zz_chain_stale.py', 'test_zz_dotted_stale.py', 'test_zz_factory_stale.py', 'test_zz_local_import_stale.py', 'test_zz_module_stale.py', 'test_zz_reexport_stale.py', 'test_zz_shadow_stale.py']
arm: expected=['test_zz_chain_stale.py', 'test_zz_dotted_stale.py', 'test_zz_factory_stale.py', 'test_zz_local_import_stale.py', 'test_zz_module_stale.py', 'test_zz_reexport_stale.py', 'test_zz_shadow_stale.py'] -> PASS
observed_exit=0
```

The `shadow` seed is the two-sided control: a test-local fake shadows a
module-level fake of the same name, and the widened half (tight module-level
twin, tolerant local) must read clean. Without that half the judge resolved
names in whole-file order and released stale locals.

`python docs/evidence/C101/probes/stale_fake_repair.py --arm`, verbatim from
`runs/repair-arm.txt`:

```
arm: seeds=8 flagged=8 pre_typeerrors=8
arm: post_stale=0 post_typeerrors=0 files=8 rejected=0
arm: exploded_widened=True -> PASS
observed_exit=0
```

`pre_typeerrors=8` is the arm that proves the repair is not a no-op: the seeds
are executed, the callable each patch site *installs* is captured through a
recording `monkeypatch`, and calling it with `transport=` must raise before the
insert and succeed after it. `exploded_widened` covers the multi-line
`def f(\n a,\n b,\n):` signature style, which the first arm draft did not seed
and which produced invalid syntax on real files.

## Readings, in execution order

| step | reading | exit |
| --- | --- | --- |
| pass-1 census (pre-shadow-fix judge) | `face: stale_sites=66 in_files=14 unknown_target=0 unknown_fake=0` | not recorded |
| pass-1 repair dry run | `face: stale_sites=66 files=14 insertions=55 rejected=0 mode=dry-run` | 0 |
| pass-1 repair apply | `recheck: stale_sites=0` | 0 |
| census re-run | `face: stale_sites=0 in_files=0` (population unchanged) | 0 |
| affected subsets | `9 failed, 730 passed` — residual `transport` TypeErrors | 1 |
| pass-2 census, after the scope fix | `face: stale_sites=10 in_files=4 unknown_target=0 unknown_fake=0` | 0 |
| pass-2 repair apply | `face: stale_sites=10 files=4 insertions=10 rejected=0 mode=applied`, `recheck: stale_sites=0` | 0 |
| subsets re-run | `739 passed`, `unexpected keyword argument 'transport'` count = 0 | 0 |
| `ruff check --select E,F` on the 14 files | `All checks passed!` | 0 |
| `ruff format --check` on the 14 files | `14 files already formatted` | not recorded |

Lines in the archived runs that begin `observed_exit=` were appended by this
session to record the exit code its harness echoed for that run; every other line
is the tool's own stdout. The pass-1 census reading and the `ruff format --check`
reading were taken behind a pipe, so their codes were never captured.

The pass-1 `stale_sites=0` was a wrong number from my own judge: it is kept here
rather than erased, because the residual 9 subset failures are what exposed it.
65 unique fakes were widened in total (55 + 10 insertions), and the corrected
judge's pre-repair site total was not re-measured — the pre-repair bytes of these
14 files are not recoverable without disturbing other uncommitted work in the tree.
The effect claim does not rest on that total: it rests on the measured failure
identity below.

## Effect check

`runs/subset-mid-9fail.txt` and `runs/subset-final.txt` cover the same 14 files
under the same argv:

```
before: 9 failed, 730 passed in 1.54s
after:  739 passed in 1.33s
```

739 = 9 + 730, so the population did not move between the two runs; nothing was
deselected to reach the green. The 9 residual failures all blamed
`<locals>.fake_get()` / `<locals>.one_observation()` at
`opendata/data/providers/fred/models/_paged.py:118`, i.e. test-local fakes the
whole-file name lookup had attributed to an already-tolerant module-level twin.

## Files touched

`runs/digests-before-pass1.txt` and `runs/digests-final.txt` hold the first 16
hex characters of each touched file's SHA-256 before pass 1 and after pass 2; the
file list is identical (14 entries, `diff` of the name columns is empty):

- `tests/test_ecb_series_fetchers.py`, `tests/test_sec_documents_iter2.py`
- `tests/test_federal_reserve_iter2_models.py`
- `tests/test_fred_{generic_models,iorb_fetcher,provider,sloos,sofr_fetcher,sonia_fetcher,macro_iter2}.py`
- `tests/test_fred_iter2_{auxiliary,federal_funds,fixed_rates,treasury_rates}.py`

No product file was edited in this step; the seam signatures in
`opendata/data/providers/**` were already forwarding `transport=` by keyword,
which is what the census counts as `seams_forwarded=34`.

## Not claimed here

- That the whole suite is green. The remaining reds are a different taxonomy and
  are counted by the suite run, not by this face.
- That every widened fake was reachable by a failing test. The census counts patch
  sites, the suite counts failing tests, and parametrization multiplies the second,
  so the two numbers were never expected to agree; widening was justified because no
  test asserts that `transport` is *not* forwarded.
- That the corrected judge's pre-repair site count is known. It was not re-measured,
  and the `66` above is the superseded reading, kept deliberately.
- Any acceptance verdict for a provider row. This is harness repair upstream of
  acceptance, not a row's positive/refusal witness.
