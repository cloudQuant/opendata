# C96 — reachability face: what the remaining provider waves are gated on

## The requirement this face serves

Iteration 2 fixes the denominator at 32 providers / 350 models
(`Provider覆盖清单.md`). The standing plan items describe the remaining work in prose numbers
("112 rows", "22 packages, 146 rows"). This face replaces those with one measurement per gate, so a
wave can be dispatched from a printed reading instead of a typed label.

Run from the repo root:

```
python docs/evidence/C96/reachability/reachability-face.py HEAD   # committed bytes at <rev>
python docs/evidence/C96/reachability/reachability-face.py WORK   # worktree snapshot, may move
```

`HEAD` materialises the four carriers through `git archive <rev>`, so a concurrent worker editing
the worktree cannot move the reading; `WORK` is a labelled exception for fixes that are not committed
yet, and the header line says so. Both raw outputs of this round are in `face-HEAD.txt` /
`face-WORK.txt` next to this file (rev `557117753f7ee1bfee473c1d53d332e4fe33a7bf`).

## Faces (measured, not asserted)

| Face | HEAD | WORK (after this round's wiring landed) |
| --- | --- | --- |
| R1 denominator | 32 providers, 350 roster models, 279 censused rows, gap 71 = `{cboe: 2, fmp: 69}`, identity True | same |
| R2 ceiling | engine_reachable 196, outside_engine 83 (artifact also 83), expressible_today 7 | 196 / 83 / **8** |
| R3 packaging | 11/32 have a package dir, 21 without | **12**/32 (adds `congress_gov`), 20 without |
| R3b work-bucket | 108 census rows live in no-package providers, 84 of them engine-reachable; 171 rows in packaged ones; partition True | 100 rows / 77 reachable / 179; partition True |
| R4 rights drift | 1 stale row `{intrinio: RIGHTS2=NOT_REGISTERED, registry_dir_row=yes}` | **0**, consistent True |
| R4b connect-before-register | `['intrinio', 'sec']` | `['congress_gov', 'sec']` |
| Verdict | exit 1, FINDINGS 2, instrument FAILURES 0 | exit 1, FINDINGS 1, FAILURES 0 |

R3b is the dispatch number: the offline-implementable remainder at HEAD is **108 rows across the 21
providers that have no package**, of which 84 are engine-reachable and 24 need a capability the engine
pool does not have. Within the 11 already-packaged providers nothing is missing from the census
except 71 rows concentrated in two sources (`cboe` 2, `fmp` 69).

## Two refuted plan labels, published rather than silently corrected

- Task 「Census the 112 rows in providers that already have packages」: no face yields 112. Packaged
  providers hold 171 censused rows at HEAD, and the uncensused remainder of the roster is 71 rows,
  fully named as `{cboe: 2, fmp: 69}` (R1, and the identity `350 - 279 = 71 = sum(named gaps)`).
- Task 「Per-provider clean-room implementation waves (22 packages, 146 rows)」: measured 21
  no-package providers (20 at WORK) holding 108 census rows (100 at WORK), not 22 / 146. The 146
  figure is 38 rows above the census and cannot be reproduced from the roster, the census JSON, or
  the roadmap artifact.

Both labels stay in the task list text as the claim that was measured against; the numbers above
replace them.

## Counterfact arms — every judge can still fire

| Arm | Tampered input | Required move | Result |
| --- | --- | --- | --- |
| A1 | strip the only outside label off `OBB2-fred-DiscountWindowPrimaryCreditRate` | outside 83 → 82 | MOVED |
| A2 | forge `outside.sdk` onto `OBB2-intrinio-BalanceSheet` | outside 83 → 84 **and** reachable 196 → 195 | MOVED (both) |
| A3 | drop `fmp` from the roster table | roster_models −69 | MOVED |
| A4 | pretend `bls` has no package directory | packaged −1 | MOVED |
| A4b | same tamper, read the bucket instead of the count | rows_in_unpackaged +2 (108 → 110) | MOVED |
| A5 | forge a numbered registry row for `sec` while RIGHTS2 says NOT_REGISTERED | stale +1 | MOVED |
| A5n | delete `intrinio`'s numbered registry row | stale −1 | MOVED at HEAD; **skipped at WORK**, where the set is already empty and A5 carries the other side |
| A7 | remove `sec`'s package directory | connect_before_register −1 (proves R4b reads the pair, not either half) | MOVED |
| A6 | artifact stores one row fewer | R2 cross-check flips to False | MOVED |

An arm that cannot move its reading appends to `FAILURES` and the script exits 2 ("INSTRUMENT
BROKEN"); a rule the tree violates appends to `FINDINGS` and exits 1 ("RED WITH NAMED BLOCKERS").
This round: 0 failures both revs.

## The rights-status fix, proven in both directions

`docs/data-rights-registry.md` gained §1 #16 (Intrinio, reviewed 2026-10-11) while
`前置条件与决策清单.md` still carried `RIGHTS2-intrinio … 无逐源登记 | NOT_REGISTERED` and the registry
header still said v1.5. The edit (registry 19539 → 19600 bytes, HEAD digest `f37720e8d9aee3a1…` →
`fc83874b0a35c7ff…`; decisions 14802 → 15272 bytes, `8114203abd5a15a9…`) sets the header to v1.6 and
the status to `SOURCE_ROW_REGISTERED_MODEL_PENDING（等效DENIED，禁止auto）`.

The face proves the edit is the act that closes the finding, in both directions: R4 reads 1 stale row
at HEAD and 0 at WORK with the same judge, and A5/A5n show the counter can be driven up and down on
demand. R4b additionally shows `intrinio` leaving the connect-before-register list at WORK while
`sec` stays in it.

## What is NOT closed, and why

`sec` (24 rows) and `congress_gov` (8 rows) have package directories in this repo but no source row
in the rights registry, and `前置条件与决策清单.md` §1 requires the three per-product conclusions before
connecting. Writing those rows means quoting each source's own terms; offline that text is not
available, so R4b is published as a finding with the blocking act named. This is the same
authorization gate as the 350-row live verification axis, and is not cleared by any number of
probes.

No terms citation exists anywhere in `opendata/data/providers/congress_gov/` or `sec/` (checked by
reading both trees and grepping the registry for an SEC/EDGAR row: none), so the finding is a real
absence, not a parser artifact.

## Cross-instrument agreement

The sibling face of this round, `docs/evidence/C96/relative-default-face.py` (written independently,
re-run here and passing with `VERDICT: PASS`), reports buckets `(188, 8, 83)` for
(locked-but-reachable, expressible-today, outside-engine). `188 + 8 = 196` equals this face's
`engine_reachable`, and `83` equals its `outside_engine` — two instruments with no shared code path
land on the same ceiling at the same worktree. Any future divergence between them is a bug report
against one of the two.

## Digest pins in older evidence, checked before editing

`docs/evidence/C65/*.json` carry `docs/data-rights-registry.md: 8a255a99210f7e0e…`. That pin was
already stale at HEAD (HEAD's registry is `f37720e8d9aee3a1…`/19539 bytes), i.e. it is a historical
capture at `d552c08`, not a live contract. `grep -rn "final-gate-source-state\|source_state\|file_digests"
--include=*.py scripts tests` returns no reader that consumes those path→digest maps, so re-pinning
them would only falsify the capture. Left untouched deliberately.

## Lint/toolchain faces of this instrument

`ruff format --line-length 100` (unchanged), `ruff check --line-length 100` (All checks passed),
`mypy --ignore-missing-imports` (no issues in 1 source file), `bandit -c bandit.yaml` (exit 0,
"No issues identified."). The two subprocess uses carry line-level `# nosec B404/B603/B607` plus
`# noqa: S603/S607` markers, are fixed-argv with no shell, and reject a `rev` that starts with `-`.
