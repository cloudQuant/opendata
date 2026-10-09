# C78 — a judge that asked for the repository to be made smaller

Round focus: AC-10|03 was red, and the reason it printed was no longer a reason anyone could act
on. This round repairs the judge (harness work, already committed as `5e335ab`), measures what the
repaired judge actually says is missing, and finds that the rebuild everyone expects to be the next
step is refused by the shipped code for a reason that is *about the product*, not the harness.

## Artifact inventory

| File | What it is | Face |
| --- | --- | --- |
| `self-test-witness.txt` | gate member 11 (counterfact self-test) run at HEAD `5e335ab` on a clean worktree | it is the face; fence digest re-derives from the archive |
| `candidate-review-scope-screen.py` | the review population, the live candidate rows, an actual `build_bundle` call, and a schema-order control | instrument only — A2-clean, sha256 `181a5baf…` recorded in the archive |
| `candidate-review-scope-screen.txt` | the verbatim run (3 min 41 s, `SCREEN_EXIT=0`) at HEAD `5e335ab`, where the screen itself was still untracked (`DIRTY_PATHS=1`) | it is the face; fence digest re-derives from the archive, and the archive's own §-figures (474 = 158 + 316, 521 blocks, 9 edited-vendored, 121/1105/3e071fcc baseline, 520 pairs, 8 candidates, 107.3 s) are parsed out of its fenced body rather than typed |
| `ac10-03-live-reading.txt` | the repaired judge's live reading plus the whole-suite counterfact face, both at HEAD `5e335ab` | it is the face; the item's 9 declared breaks are recomputed from the probe registry and the run's own 115/758 totals are cross-checked against them |
| `block-census-differential.py` / `.txt` | shipped AST block census vs the method the archived artifact documents, over the 121 pinned baseline files | it is the face; `shipped + ClassDef.delta == reference` is checked by the builder, 11 declarations hold |
| `reference-policy-repin.py` / `.txt` | gate run 6's refusal (two stale `sha256` pins), the re-pin, and the drift audit that says the re-pin is not a new approval | it is the face; 28 declarations re-derive both digests from `git show` at the two revisions the fence itself names, and Control A forges each pin to show `reference_policy_problems` still emits `stale sha256: <path>` |

## 1. The judge was demanding `474 -> 71`

`judge_ac10_03` compared the reading against typed literals: `provider_count == "7"`, a seven-name
provider string, four separate `== "71"` file counts, `baseline_source_file_count == 121`, and the
OpenBB commit spelled out in the judge. The repository registered 12 packages with 474 provider
`.py` files, so the item's own printed repair face read `current_source_file_count: 474 -> 71` and
`provider_count: 12 -> 7`. That is the shape `self_test_findings` calls unsatisfiable — "a gap here
could never be closed by real work" — except the self-test could not see it, because the literals
made the *clean* reading internally consistent.

What it judges now is equality between two populations that are measured differently:

* the **registry census** — `provider_packages(ctx)` + `provider_py_files(ctx, name)`, walked out of
  `git ls-files` intersected with on-disk presence (the same primitives the AC-16 record face uses);
* the **review population** — the evidence validator's own `rglob` walk of the worktree.

The judge requires the package names, the per-package file counts, the file *sets* in both
directions, and the four bundle faces (source / SHA ledger / review / similarity manifest) all to
equal the registry census, and keeps the absolute faces: zero OpenBB imports, sources parse, review
hash-bound, reviewer authorized, pinned baseline commit, the baseline package set equal to the set
the harness pins (re-derived here from the similarity artifact's manifest paths, not from the
artifact's own provider list), candidates cleared, and a positive nearest-pair conclusion. The
numbers `7`, `71` and `121` are gone; the baseline file count is now bounded below rather than
pinned, so widening `EXPECTED_BASELINE_PROVIDERS` will not re-open a false gate.

Measured on this tree, the two populations agree package for package:

```
akshare 341 | bls 8 | cboe 9 | ecb 18 | federal_reserve 8 | fmp 8 | fred 24 | imf 10
oecd 9 | sec 6 | ths 26 | yfinance 7        (bundle == registry for 12/12, 474 == 474)
```

So the repaired judge is reachable (its declared clean reading passes) and the live reading still
says `gap`, now for a truthful reason: the four faces read `474/474/71/71` — the review and
similarity manifests are still the C65 build over the 7-package tree — with 172 validator issues.
The repair face inverted direction: `reviewed_source_file_count: 71 -> 474`,
`reviewed_provider_names: <seven names> -> <the twelve the registry reports>`.

Counterfacts went from 6 to 9 for this item (a registered package with no review row, a census file
the bundle never saw, a baseline set other than the pinned one), and the whole suite is green at
`5e335ab`: **115 probes measured, 758 counterfacts, every one of them flips the clean reading back
to a gap** (`self-test-witness.txt`; C77's run reported 755).

## 2. A hypothesis about the nearest-review leg, refuted

I expected to find a fail-open in `_validate_nearest_review`: it is called with `ranked_pairs[:3]`
and returns early when that list is shorter than three, which with `candidate_count == 0` looked
like "the three rows are never bound to anything". It is not true, and the archive says so:

```
candidate_count(facts) = 0        ranked_pairs(live recompute) = 109
len(ranked_pairs[:3]) = 3  ->  the per-row binding loop DOES run
row0 ths/models/fund_action.py           in_current_bundle=True in_live_ranked_pairs=True
row1 akshare/models/index_constituent.py in_current_bundle=True in_live_ranked_pairs=True
row2 akshare/models/index_daily.py       in_current_bundle=True in_live_ranked_pairs=True
```

`nearest_pairs` is the prefiltered top-N list, not the candidate list, so a zero candidate count
does not shrink it. `nearest_review_count == "3"` therefore stayed in the judge — it is a real,
bound face, not a stale literal.

## 3. The self-report is one field over: `unreviewed_candidates`

`_nearest_document` writes `"unreviewed_candidates": 0` as a **literal**
(`provider_source_review_evidence_build.py:1148`), and `validate()` reads it back off the document
(`provider_source_review_evidence.py:913`) before the probe asserts `"0"`. Nothing measures it. The
face is only true because two *other* things hold: the inventory's `candidates` list must be empty
(`similarity-candidates-unreviewed`), and the builder raises before writing if the screen produced
any. So "no unreviewed candidate" is guaranteed by construction plus refusal, not by observation —
which is fine as a design only while the refusal is reachable, see section 4.

## 4. A rebuild cannot be run today, and the reason is a finding

`candidate-review-scope-screen.py` runs the shipped primitives over this tree and then actually asks
`build_bundle` to write a round (into a throwaway directory; the run is labelled
`GUARD PROBE -- CERTIFIES NOTHING` because no review act was performed for it).

* Population as the builder classifies it: 474 `.py` files = **158 hand-written + 316 lock-proven
  verbatim**, 521 local blocks over the 40-node floor. Nine of the 158 sit under `_vendor/` and are
  *not* excluded from structural comparison because `upstream.lock` marks them `manual_edits`
  (`bond/bond_china_money.py`, `bond/bond_convert.py`, `datasets.py`, `futures/futures_hf_em.py`,
  `index/index_cons.py`, `index/index_zh_em.py`, `option/option_em.py`, `stock/cons.py`,
  `stock_fundamental/stock_finance_sina.py`). Cross-check against the ledger's own note, which says
  325 vendored / 149 self-developed: `325 - 9 = 316` and `149 + 9 = 158`, so that sentence is stale
  by exactly the nine manually-edited files, and the disagreement is bookkeeping rather than a new
  fact about the code.
* Candidates over the pinned five-provider baseline: **8** rows, from 520 ranked pairs, in 107 s.
  C77's widening screen measured the same tree with its own harness and printed `pairs=520 /
  candidates=8` for arm5, so the number reproduces across two rounds and two instruments. Counting
  the eight rows back out of the archive: they sit in three local packages (`akshare`, `fred`,
  `ths`) against three baseline packages (`fred`, `imf`, `yfinance`), and **one** of them — not two,
  which is what I first wrote down — comes from an edited-vendored file
  (`0.7122 akshare/_vendor/bond/bond_china_money.py::[201, 207]`, the only `_vendor` path among the
  nine that produced a candidate). The highest is
  `0.7435 fred/models/_estr_query.py::_bind_omitted_series_id <- imf/utils/table_builder.py`.
  Those package tallies came from re-parsing the archive, and the first parse returned
  `rows parsed = 0`: my pattern matched the span field with `\S+`, which cannot match `[186, 190]`
  because of the space inside it. An empty face where eight rows are printed is a bug report against
  the instrument, so the field became `(\[[^\]]+\])` and the parser now has to reproduce all eight
  rows before any of them is quoted here.
* The rebuild attempt: `BuildError: 8 similarity candidates crossed the review thresholds, so this
  round is not a zero-copy observation: [...]`, and `nothing written to the output dir = True`. The
  guard names the same count section 2 of the same run measured, and the control arm shows a
  malformed stub dies earlier in `load_findings`, so the refusal is attributable to candidates and
  not to my stub.

Consequence for AC-10|03: closing it is **a review act over 8 named pairs**, not a re-run. The
evidence schema has no field for "a candidate that was reviewed and cleared" — the inventory can
only carry `candidates: []`, and the validator only accepts zero — so a truthful bundle cannot be
written for today's tree at all. That is a design gap in the iteration-2 harness, and the fix is to
record per-candidate review rows and compute `unreviewed_candidates` from them, which also removes
the literal in section 3.

## 5. Round label and directory, as a structural note

`validate()` reads its three artifacts from `EVIDENCE_DIR = docs/evidence/C65` and requires
`archive_round == "C65"`, while `write_bundle` refuses to write anywhere under `docs/evidence`
(`_refuse_archive`). A refresh therefore has to be copied by hand over a round archive the tool
calls protected, and would still label itself C65. Not exercised: the two artifacts have each been
committed exactly once (`5080639`, when C66 ingested the round), so no overwrite has happened and
the C65 bundle is still the one C65 wrote — its `generated_at` is `2026-09-30T21:57:23+00:00` and
its `comparison.local_head` is `d552c081`, against `5e335ab` today. That gap of nine days is why
`source-review-stale` is in the 172 issues, and it is a TTL (`MAX_EVIDENCE_AGE = 24 h`) that has to
be re-*built*, not re-aged.

## 6. The archive's zero and today's 8 are not outputs of the same screener

Chasing why §4's `8 candidates` contradicts the archived `review_candidates = 0` produced a worse
finding than either number: the two were not computed by the same code.

`block-census-differential.py` pins the population first — **all 121** archived baseline file digests
match the bytes on disk, and the OpenBB checkout is still at the pinned `3e071fcc` (`equal = True`),
so this is record-vs-disk, not a moved reference tree. Over those identical 121 files:

| enumerator | blocks >= 41 AST nodes |
| --- | --- |
| shipped `_blocks_and_imports` | **1105** |
| reference, written from the artifact's own `method` text | **1279** |
| what `provider-similarity-inventory.json` recorded in C65 | **1227** |

The histogram isolates it to one block kind: `ClassDef.body`, shipped **0** vs reference **174**;
every other kind matches exactly (`function_body` 348 both arms, plus `For/If/If.orelse/Try/
Try.handlers/ExceptHandler/With/AsyncWith/While`), and 93 of 121 files disagree. The identity
`1105 + 174 == 1279` is checked by the archive builder.

Neither arm reproduces 1227, and the archived number sits between them. This screen cannot attribute
that to one rule, and the two live explanations are not the same bug: it is consistent with class
bodies being *partly* enumerated by the C65 code (`1227 - 1105 = 122` of the 174), and equally
consistent with a different parser version moving ordinary blocks across the 41-node floor, since
`node_count` is a function of the AST a given interpreter builds. What is settled is only that the
shipped code reproduces neither the artifact nor the documented method:
`provider_source_review_evidence_build.py` was **added whole** in `60148ce` (C75, 1460 insertions)
and its docstring's "follows the archived C65 method" is a statement of intent this screen refutes on
counts.

Two consequences, and one thing this is *not*. It is not a copy-detection hole at member granularity:
the shipped walk descends into class bodies and emits every nested statement list, so members are
still compared one by one — what is lost is the class body as one comparison unit. The real costs
are (a) the ledger's "读数面已可复算" was too strong: recompute is self-consistent, not
archive-consistent, so a C65 zero cannot be cited about today's tree, and (b) any threshold tuning
in C79 has to say which enumeration it means, because the candidate set is a function of a block
population that three readings of one tree disagree about.

## 7. Gate run 6 stopped at a pin, and the pin was bookkeeping

Run 6 launched from a clean tree at `2535178` (`DIRTY_PATHS=0`). Member 1 `brand-check` passed;
member 2 `zero-dep-check` refused:

```
FAIL: reference policy is unusable: stale sha256: scripts/quality/a2_check.py; stale sha256: scripts/quality/acceptance_item_probe.py
```

so `GATE_RC=2` before any judgement probe ran. This is the round's own doing: `5e335ab` rewrote
`acceptance_item_probe.py` (section 1) and `4960738` rewrote `a2_check.py`, and `verify_no_akshare.py`
re-hashes every registered path, so a stale pin is a hard stop rather than a warning. A census of all
87 entries finds exactly these two stale — the refusal was complete, not partial.

A digest re-pin is only honest if the approval content did not move, so
`reference-policy-repin.py` re-measured it with the repository's own `scan_source` and printed a
forged arm beside each zero:

| file | old pin carried by | bytes moved by | findings old/live | brand lines / occurrences / load-shape |
| --- | --- | --- | --- | --- |
| `a2_check.py` | blob at `3f05f63` (7 policy revisions) | `4960738` | 1 / 1 | 2 / 2 / 0 → 2 / 2 / 0 |
| `acceptance_item_probe.py` | blob at `60148ce` (2 policy revisions) | `5e335ab` | 13 / 13 | 124 / 147 / 4 → 124 / 147 / 4 |

Line totals did move (364 → 390 and 16134 → 16468) — that is the rewrites. The reference content did
not: identical `(module, kind)` multisets, identical brand-line/occurrence/load-shape faces, zero new
load-shape lines. The interesting part is *why* the probe's brand face stayed at exactly 124 lines:
section 1's fix **deleted** the three literal `akshare, ecb, fred, imf, oecd, ths, yfinance` strings,
and three brand-carrying lines came in to replace them (a comment about the substring-negation guard
and a `record_refusal_detail` string). The de-hardcoding is visible in the pin face as a substitution,
not an addition.

Control A is the point: forging one entry's digest makes the repo's own judge return exactly
`stale sha256: <that path>`, 2 of 2 arms, each naming its own path — the same message that stopped
run 6. So the `0 problems` reading on the re-pinned policy is a measurement, not a judge that cannot
fail. Control B (one `import akshare` + one module-path string appended to the pin bytes) produces 4
problems through the same comparator that reported 0 for the real drift.

What this does *not* buy: the other 85 pins are current, not newly reviewed — a current digest is not
an approval — and `reference_policy_problems` was called here without `actual_hit_paths`, so the
bidirectional hit/register reconciliation arm is unexercised by this screen (member 2's own run does
exercise it; that run is what the archive is waiting to see green).

## Established / not established

Established: the old judge was unsatisfiable by real work and the new one is not — its clean reading
is reachable through an indirection-only repair, it declares 9 breaks recomputed out of the registry,
and the whole-suite witness at `5e335ab` (115 probes / 758 breaks, `GATE_EXIT=0`) certifies each of
them flips a clean reading back to a gap; the two provider populations agree package-for-package at
474 today; the candidate count over the pinned baseline is 8 and reproduces from C77's independent
harness; `build_bundle` refuses to write on today's tree, by execution rather than by reading the
source; `unreviewed_candidates` is a builder literal, and the three nearest-pair rows are genuinely
bound to the live ranking; and the shipped builder's AST census (1105 baseline blocks) reproduces
neither the archived artifact (1227) nor the method text that artifact carries (1279), over a tree
whose 121 digests all still match disk; and run 6's stop was a stale-pin refusal rather than a
judgement failure, with the two drifted files shown by the repo's own scanner to carry identical
reference content (1/1 and 13/13 findings, brand faces unchanged) and by the repo's own pin judge to
still refuse a forged digest (2 of 2 arms).

Not established:

- that AC-10|03 can be closed at all without a schema change — the 8 candidates have no place to be
  recorded, which is the claim section 4 argues and the next round has to act on;
- that the 8 candidate rows are benign. Nobody has reviewed them this round; exactly **one** of them
  (`bond_china_money.py::[201, 207]`) is a block in an `upstream.lock`-marked manual-edit file, which
  is a question for whoever owns the vendor bookkeeping (task #17), not for this harness;
- that any other judge is free of population literals. I grepped for the ones I typed here (`"7"`,
  `"71"`, `121`) and they are gone from `acceptance_item_probe.py`, but that is not an audit of the
  other 114 probes;
- that the gate is green: run 6 stopped at member 2 on a pin face and its retry had not finished
  when this file was written. Members 1 and 2 read green on the re-pinned policy; members 3 through
  17 have not been seen this round.

## Next

1. Enumeration first (C79, before any threshold work): decide what a "block" is, in code and in the
   method text at once, and re-measure the census under it. Until then every candidate count this
   harness prints is a function of an unreconciled definition. Then: carry per-candidate review rows,
   compute `unreviewed_candidates` from them, move the live bundle to a stable path with a measured
   round label, and give the builder a `--candidate-findings` input. Positive control first: a forged
   candidate must be refused, and a reviewed one must be recorded — both arms printed, not asserted.
2. The review act on the 8 rows (or 14 if the baseline widens), each identity copied from the face,
   with per-pair conclusions. Subagents may draft readings; nothing counts until it is re-measured.
3. AC-16's twelve canonical records still stand at `0 credited / 12 refused`, and they need the same
   review acts.
4. Gate run 6's retry on the committed, re-pinned tree, archived verbatim.

Re-derive this round's numbers:

```
python3.11 -u docs/evidence/C78/candidate-review-scope-screen.py   # 3 min 41 s, writes nowhere
python3.11 -u docs/evidence/C78/block-census-differential.py       # <1 s, read-only, two trees
python3.11 -u docs/evidence/C78/reference-policy-repin.py          # ~1 s, read-only, two forged arms
python3.11 -u scripts/quality/acceptance_item_probe.py --item 'AC-10|03'   # live reading
python3.11 -u scripts/quality/acceptance_item_probe.py --self-test  # ~12 min
```
