# C97 · `columns.null_sentinel` — engine kernel, production wiring and its judgement face

Step 0 of the C96 ladder ("the engine facet that five rows are waiting on") shipped as one declared
column facet plus the execution point that consults it. Nothing here re-implements the judge: the
face imports `scripts/quality/model_capability_census.py` and calls `count_declarations`,
`verify_registry`, `greedy_cover`, `run` and `serialize` themselves.

## What is in the kernel

| plane | where | what it does |
| --- | --- | --- |
| declaration | `opendata/data/providers/_engine/spec.py:357` — `null_sentinel: tuple[str, ...] = ()` on `ColumnSpec` | the spellings *this* column's source publishes for "no value" |
| declaration refusal | `ColumnSpec._check_null_sentinel` | refuses a non-tuple, a non-str member, a token named twice, and any sentinel on a non-nullable column |
| model refusal | `ModelSpec.__post_init__` (after the derive-plan loop) | refuses a sentinel on a column whose value is **stamped** by a derive plan (`source_kind != "record"`) — the cell is never read, so the declaration could only ever be a silent no-op |
| execution | `opendata/data/providers/_engine/http_json.py` — `_is_null_sentinel`, called in `normalize_record` immediately after `value = record[key]` and in the envelope fallback | matched text becomes the same `None` an absent key becomes, and the column's own cast/rescale stages are skipped for it |
| census | `scripts/quality/model_capability_census.py` — `columns.null_sentinel` | `read_from_disk=True`, `cross_file=http_json`, `cross_anchor="def _is_null_sentinel("`, `probe=DeclarationProbe(ColumnSpec, null_sentinel, non_empty=True)` |

Matching is on **published text before coercion**. `0`, `0.0` and `False` are values a source
publishes; a sentinel that swallows them is a data-loss bug, so the engine refuses to spell one
(`tests/test_engine_null_sentinel.py::TestMatchIsOnText`).

## Production wiring (19 declaration sites, 117 sentinel-bearing columns)

`opendata/data/providers/federal_reserve/specs.py` declares the tokens its own two release bodies
print and the four the Svensson contract's validator absorbs:

* `H6_NULL_SENTINEL = ("ND",)` on `MoneyMeasures`' seven `_N.M` float columns — `money_measures.py:85`.
* `H15_NULL_SENTINEL = ("ND",)` on `TreasuryRates`' eleven `RIFLGFC*` str columns — `treasury_rates.py:97`.
* `SVENSSON_NULL_SENTINEL = ("", "NA", "ND", "N/A")` on all 99 Svensson series columns.

This changed observable behaviour, and the round owns that: the H.15 fixture row whose maturities
printed `ND` now comes out as `None`. `tests/test_provider_federal_reserve.py` was re-titled from
`test_blank_cells_are_nulls_and_nd_is_a_value` to
`test_blank_cells_and_the_nd_token_are_both_nulls` rather than having its expectation quietly kept.

**Not wired, and why.** Of the five rows the ladder credits to this facet, zero can be declared by
this repo's packages: `benzinga` and `econdb` have no package in the tree at all; `federal_reserve`
records `FederalFundsRate` and `Sofr` in `NOT_DECLARED`; `intrinio` records `InsiderTrading` in
`NOT_DECLARABLE` with no `ModelSpec` naming it. Their census `expressible_today` flags therefore stay
`false`, which is what the measured face below reports instead of the ledger's forecast.

## Running the face

```sh
export PATH="$HOME/opt/anaconda3/bin:$PATH"; export PYTHONPATH=.
python docs/evidence/C97/null-sentinel/face.py
```

Its "before" side is a rev-pinned baseline: `git archive 5917f40 -- opendata/data/providers <census
dir>` is extracted into a temporary directory and the same judge is run over it, so the pre-round
face is read out of a commit, not asserted. The script refuses to run if `HEAD` has moved off that
rev, and it writes nothing inside the repository.

## Measured face (this checkout vs the pinned rev `5917f40`)

| face | before (pinned rev) | after (this tree) |
| --- | --- | --- |
| registry reading for `columns.null_sentinel` | `absent` | `present`, anchor `spec.py:357`, cross-anchor found, **19** declaration sites |
| expressible / prefix / locked / outside | `8 / 188 / 83 / 83` | `8 / 188 / 83 / 83` |
| `shipped_cover_rows` | `2` | `7` (the five credited rows join; all five are in it) |
| ladder step 0 | `columns.null_sentinel +5` | `request.serialised_param +4` (the shipped facet is no longer a step) |
| rows whose blocker set shrank | — | **44**, each dropping exactly `columns.null_sentinel`; 0 rows gained a blocker |
| identity `expressible + prefix + locked` | 279 | 279 ✓ |
| `built_but_unused` | contained `columns.null_sentinel`? no — it was absent | no longer lists it (5 entries remain) |
| artifact | — | `run --round-id C97 --json …` reproduces the committed bytes; `--check` verdict: PASS |

### §52.8 P4: `13 + 183 + 83 = 279` — **REFUTED as a shipping prediction**

The arithmetic is reachable: arm C7 flags the five credited rows and the judge returns exactly
`(13, 183, 83)`. What is refuted is the premise that shipping the kernel produces that face. The
census derives `rows_expressible_today` from each row's hand-recorded `expressible_today` flag, and
no package can set those five flags honestly this round, so the shipped kernel moves
`shipped_cover_rows` (2 → 7) and step 0, and leaves the buckets at `8 / 188 / 83`. Publishing the
prediction as though it had been met would have required five flags over zero declarations.

## Counterfact arms

Every judge above has an arm that must move, and the refusals have theirs:

* **C1** anchor retargeted at a token the declaration plane never grew → `absent`.
* **C2** cross-anchor retargeted at a non-existent runtime symbol → `absent` — the "declared but the
  runtime never consults it" class, judged at the census level.
* **C3** the declaration count read off the pinned pre-round tree → `19 → 0`.
* **C4** the entry rolled back to its C96 shape reproduces the pinned rev's face `(8, 188, 83, 83, 2)`
  and puts `columns.null_sentinel` back at step 0 — in-memory tamper and git baseline agree.
* **C5** `null_sentinel` stripped from the production `TreasuryRates.year_1` in place → the runtime
  stops nulling `ND`; the shipped column still does.
* **C6/F6** each of the four construction refusals fires with the engine's own message, beside a
  positive control (same column, stamp removed) that must construct.
* **C7** the forecast measured both ways, with each row's in-tree refusal printed.
* **C8** serialising the rolled-back registry differs from the committed artifact — the byte-for-byte
  judge can go red.

## What this round found and did not bend

* `MoneyMeasures`' old gap text recorded `ND/-`. Upstream maps only `ND` (`money_measures.py:85`) and
  deliberately keeps `"-"` (`:93`); the declaration follows the source, and the `"-"` cell is still a
  refusal against a `float` column. `SvenssonYieldCurve`'s `-999.99` is likewise not carried — a
  sentinel matches published text, and those columns are `str`.
* `econdb AvailableIndicators` nulls `N/A` **before** its `sort_values` (`helpers.py:481-482`), while
  the engine runs `order_by` before `normalize_record`; the ordering dependency is not reproducible at
  the prescribed execution point. Moot for now — no econdb package exists — but it is a real shape for
  the next CSV row.
* `intrinio/specs.py::NOT_DECLARABLE["InsiderTrading"]` still names `columns.null_sentinel` as its
  blocker, and the guard that would catch such a stale claim
  (`tests/test_intrinio_not_declarable.py::SHIPPED_CANONICAL`) is a per-round hardcoded list that has
  not been promoted. Four intrinio rows are affected. Re-recording them is a ledger act this kernel
  round did not take on; the reason text was left verbatim rather than rewritten to something the
  guard would still accept.
* The census `notes` for `federal_reserve-SvenssonYieldCurve` keep four needs that this facet does
  not satisfy, so that row correctly remains pending even though 99 of its columns are now wired.

## Repairs made after the kernel landed (each one measured, not asserted)

**1. A swallowed token in the registry's own `why`.** `columns.null_sentinel`'s claim text read
`"Sentinels are admitted as only,"` -- the quoted type had been dropped, so the sentence named no
shape. Repaired in `scripts/quality/model_capability_census.py` to
`"admitted as a ``tuple[str, ...]`` of the published text"`. That edit moved a field the artifact
serialises, and **this face caught it**: the next run went red on
`F3 the committed artifact is not what this tree produces` (FACE_EXIT=1, read un-piped -- the first
time I printed `${PIPESTATUS[0]}` under zsh, which is not zsh syntax, so the exit code was simply
missing). `run --round-id C97 --json …` then reproduced the bytes and the face returned
`VERDICT: PASS`, FACE_EXIT=0. The face's own `import subprocess` also gained the house line-level
`# nosec B404`; bandit then printed the positive face (`No issues identified.`, severity counts
0/0/0/0) rather than the silent `-q` zero.

**2. A red this round inherited from C96 and did not cause: AC2-07.** `pytest` full-suite was red at
`tests/test_ac2_case_probe.py::test_self_test_closes_and_every_break_flips` with
`AC2-07 FAIL breaks=8 flipped=8 closure=gap`. Note the arms were fine (`breaks == flipped`); the
*closure reading* moved, so this was a content red, not an instrument red. Its two legs:

    zero_dep_exit=1        # the read-only leg fails before it can print, hence
    walker_count=unparsed  # census_identity_holds=False
    FAIL: the scan surface moved: opendata archived 339 but walks 345 file(s)

345 - 339 = **6**, exactly the six `.py` files C96 committed as `opendata/data/providers/congress_gov/`
(e39c1b6). The frozen scan surface was never re-frozen, so the round that added a package shipped a
red leg -- and C96 did not run the full suite afterwards, only 4 targeted files. Re-freezing first hit
the documented chained guard (`docs/evidence/C72/zero-dep-refreeze.txt` records the identical chain):

    FAIL: refusing to freeze an unusable reference policy: stale sha256: opendata/data/providers/catalog.py

The pin was audited before it was moved, not just re-pointed:

    pin b40f5729…  -> blob of 083b7e4   |  live fe9364af…  -> blob of e39c1b6
    git diff 083b7e4 e39c1b6 -- catalog.py, changed lines only:
      +from opendata.data.providers.congress_gov.provider import PROVIDER as CONGRESS_GOV_PROVIDER
      -    _reserved_provider("congress_gov", "api_key"),
      +    CONGRESS_GOV_PROVIDER,
    akshare occurrences pinned=[13, 90, 96] live=[13, 91, 97]  n=3 both, texts identical as a multiset

So the reviewed content is unchanged and the entry was re-pinned under the same approval, with the
audit written into `reviewer` and `reviewed_date` moved to 2026-10-12. 87 entries: 86 current, 1
stale, 0 absent before; 87 current after. Both arms then measured:

    python scripts/codemod/verify_no_akshare.py --update   -> Wrote … 0 frozen reference(s) across 672 file(s), exit 0
    python scripts/codemod/verify_no_akshare.py            -> OK: 672 file(s) walked (opendata=345, akshare/_vendor=325, opendata_client=2), exit 0
    python scripts/quality/ac2_case_probe.py --item AC2-07 -> AC2-07 OK, zero_dep_exit=0, walker_count=672 == baseline_scope_sum=672
    python scripts/quality/ac2_case_probe.py --self-test   -> SELF_TEST PASS failures=0, 25 cases, breaks=153 flipped=153

The `--update` path refuses to *grow findings* without `--force-update`; it did not need it, because
the frozen reference count stayed 0 -- the census grew, the akshare references did not. That
distinction is the reason a re-freeze is allowed here at all.
