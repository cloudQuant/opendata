# C77 — three gate instruments that reached their verdicts without doing the work

Round focus: while clearing iteration-2 acceptance items I found three places where a line in
`make gate` (or in an acceptance reading) was produced by an instrument's *error path*, an *empty
denominator*, or its *own output* rather than by a measurement. All three are repairs to the harness,
not relaxations: none changes what a passing run asserts, and each makes a previously-invisible class
of problem visible.

## Artifact inventory

| File | What it is | Face |
| --- | --- | --- |
| `a2-bandit-escape-face.py` | the probe; runs real bandit and a shaped byte stream through both the old and the new leg | `a2-bandit-escape-face.txt` |
| `a2-bandit-escape-face.txt` | verbatim stdout of the probe with its tree/digest header | — |
| `baseline-scope-widening-screen.py` | measures AC-10\|03's matched scope under the pinned 5-provider baseline and under a 10-provider one, plus a clone control | `baseline-scope-widening-screen.txt` |
| `baseline-scope-widening-screen.txt` | both arms, the 9-of-12 local-block census, and the clone control that must fire | — |
| `clean-room-record-schema-face.py` | drives eleven arms (one affirmative, nine refusals, one restore) over a temp git repo and asks the new AC-16 record face about each | `clean-room-record-schema-face.txt` |
| `clean-room-record-schema-face.txt` | the eleven arms with their stated expectations, plus the live tree's re-measured AC-16\|01/02 reading | — |
| `../clean-room/README.md` | the schema carrier the new face reads: one tracked record per provider package, digest-pinned | — |


## What was wrong, and what the face says

**`a2_check._bandit` parsed `stdout + stderr` as one JSON document.** bandit sends its `nosec`
bookkeeping warnings to stderr, and this repo's A2 set emits them: measured 7,540 stderr bytes
against 761 stdout bytes for a single file. The concatenation is not JSON, `json.loads` raised, and
`except JSONDecodeError: return True` answered *pass*. The leg's `ok bandit` line was therefore a
report about the parser, not about the code.

The fix keeps the streams apart (`_run_streams`) and fails closed on any of: non-zero exit, stdout
that is not JSON, `results` that is not a list. A pass now requires reading an empty `results` list.

**Scope, and my own over-claim.** The tightened leg is worth shipping, but the live arm shows
`parsed_results_count: 0`, and `bandit.yaml` carries neither `exit_zero` nor `level_severity`, so
bandit still exits non-zero when it reports and the old `code != 0` guard caught real findings. This
round's finding is therefore **a latent fail-open and a mislabelled verdict, not an observed
mis-verdict** — the shape that would let findings through has to be forced (the shaped arm), and it
does flip: same bytes, legacy `ok=True`, new leg `ok=False` with the B307 row printed
(`SHAPED-ARM-FIX-BITES: True`). Any wording that promised "bandit findings were being passed" is
retracted; the probe prints its own scope line rather than leaving it to prose.

**Why `make lint` never showed any of this.** `PY_SELFDEV` covers `opendata scripts tests alembic
alembic_data` and excludes `docs/`, while a2-check's A2 set is "everything added or modified since
`docs/quality/baseline.json`" — measured at 715 candidates this round (opendata 290, tests 252,
docs/evidence 88, scripts 68). Re-derive with:

```
python3.11 -c "import sys; sys.path.insert(0,'.'); from scripts.quality import a2_check as g; print(len(g.resolve_files(None)))"
```

That gap is what made C76's own three probe scripts (43 ruff + 6 bandit findings) invisible until a
gate member with the wider definition ran.

**AC-10|03's matched scope had no denominator for five of its twelve packages, and its local side
only ever saw nine.** `EXPECTED_BASELINE_PROVIDERS` pins `{ecb, fred, imf, oecd, yfinance}` while the
repo registers twelve provider packages; for `bls`, `cboe`, `federal_reserve`, `fmp`, `sec` the OpenBB
counterpart sits unused in the same pinned checkout, so a verdict of
`NO_COMPLEX_COPY_OBSERVED_IN_MATCHED_SCOPE` was measured over a baseline that cannot contain their
counterpart. Both arms, same 521 local blocks, same checkout:

| arm | baseline providers | baseline blocks | candidates |
| --- | --- | --- | --- |
| 5 (as shipped) | 5 | 1,105 | 8 |
| 10 (widened) | 10 | 2,844 | 14 |

Widening changes the answer — 2 of the new rows sit inside the five added packages
(`fmp/…/_validate_window` 0.7330, `bls/…/_year_windows` 0.7185), so they were invisible by
construction, not by absence. Reading the arms surfaced a second hole: `cboe`, `federal_reserve`,
`sec` contribute **zero blocks in both arms** (they are engine-declarative, 9/8/6 tracked `.py` files,
and `measure_tree` keeps only blocks over 40 nodes). And the zeros are calibrated: the clone control
injects one of our own 976-node blocks into the baseline as a fake upstream file and it comes back
`control_score 1.0`, `control_ranked_first true` — so "0 candidates" would mean something after this,
which it did not before.

**AC-16|01/02's 「审查留档」 half was crediting the probe for describing the gap.** The record face was
a prose match: any line under `docs/evidence/**` containing 「无 OpenBB 源码参照」 and a package name
counted as that package's review record. Measured: 7 packages / 8 lines, and every line is either
`C64/remaining-items-audit.md`'s sentence saying the record **does not exist**
(「提交说明带「无 OpenBB 源码参照」的 0 个」) or a `C65/item-readings-*` dump of this probe's own output.
`PROBE_ECHO_MARKERS` did not catch them because neither line carries one of those markers.

`canonical_clean_room_records()` replaces it with a schema face: one tracked file per package at
`docs/evidence/clean-room/<package>.md`, ISO 审查日期, non-self-referential 审查人, 覆盖面 equal to the
package's measured `.py` census, 代码面摘要 equal to the sha256 of those exact bytes, a written
方法与反证, and a conclusion line binding the package name to the phrase with no negation token. Each
refusal prints its reason. The eleven arms in `clean-room-record-schema-face.py` are what make the
resulting `0 / 12` mean something: `affirmative` credits a conforming fixture, `wrong-path` refuses
the same text filed elsewhere, `bytes-moved` refuses a record whose package moved after it was written
(that arm is what caught a stale `@lru_cache` on `provider_py_digest`, since removed),
`negated-conclusion` refuses 「无法证明 …」 as the disclosure it is, and `restore` credits again after
every tamper. The legacy face is still measured and printed as a contrast, but it is in no judge, and
four new breaks (`record_refusals`) ride on the new fact.

## Test-side companion

`tests/test_a2_exclusion_scope.py` gained `TestBanditVerdictIsReadNotGuessed`: the old body is
reproduced byte for byte as `legacy_bandit_verdict()` and driven with the pre-`_run_streams` call
shape, so the paired rows measure *the same bandit run* and their disagreement is a property of the
two bodies, not of two samples. Four parametrized cases (clean / clean+warning) × (finding /
finding+warning), an unparseable-stdout arm, and a real-bandit control that needs no stub at all
(`eval('1 + 1')` → B307 is caught by the shipped leg). `55 passed in 21.96s`; the run's coverage
`FAIL` line is pytest.ini's whole-suite floor measured over one file, not a defect.

## Established / not established

Established: the escape is real and reachable in code; the tightened leg closes it; both legs'
verdicts are printed from one process on identical bytes; the fix's target population (715 A2
candidates) is measured rather than quoted; the screen fires on a byte-identical clone (1.0, ranked
first) and its shipped matched scope really was 5 providers / 1,105 blocks against a 12-package repo;
the new AC-16 record face can credit a conforming record and refuses nine distinct ways of not being one:
`digest-drift`, `stale-census`, `negated-conclusion`, `echo-mark`, `no-date`, `self-reviewer`,
`thin-method`, `wrong-path`, `bytes-moved`.

Not established:

- that any iteration-2 acceptance item moved to `proven` by this — C77 is harness repair, and two
  items (AC-16|01, AC-16|02) are now honestly `gap` where they previously read proven-shaped;
- that the 14 candidate rows in the widened arm are benign — they sit under the screen's stated
  bars, and the per-pair findings are task #47's work;
- that `bls`/`cboe`/`federal_reserve`/`fmp`/`sec` are clean-room. The face records that the screen
  could not see them, not that they are clear;
- that gate 6 is green: nothing here has been through the full 17-member run. The counterfact
  self-test (member 11) has since run over both C77's AC-16 breaks and C78's rewritten AC-10|03
  breaks at HEAD `5e335ab` — 115 probes, 758 breaks, each break applied once and each one flipping a
  clean reading back to a gap, `GATE_EXIT=0`; the verbatim archive is
  `docs/evidence/C78/self-test-witness.txt`. That is member 11 alone, not the other sixteen.

## Next

1. Rebuild the provider source-review bundle over the 12-package census: widen
   `EXPECTED_BASELINE_PROVIDERS` to the counterparts present in the pinned checkout, carry the
   9-of-12 local-block census in the same reading, and replace `judge_ac10_03`'s hardcoded `"7"` /
   `"71"` / provider-name literal with a registry-derived expectation plus a missing-package
   counterfact (task #47). C78 took the third clause (the literals are gone; the judge now compares
   two independently computed populations) and measured the first two as *not* a re-run: over
   today's tree `build_bundle` refuses to write, because the shipped thresholds cross on 8 named
   pairs and the evidence schema has no row shape for "candidate reviewed and cleared". See
   `docs/evidence/C78/README.md`.
2. AC-16|01/02 need real review acts, not more instrumentation: a per-package record under
   `docs/evidence/clean-room/` (schema in that directory's README), and a commit attestation where
   the claim is true. `gap` with disclosure stands where OpenBB source was consulted — the C76
   `openbb_sec` re-measurement is on record for `sec`.
3. Land gate run 6: the full 17-member pass that carries member 11's new breaks and reaches the
   frontend members 13-17.
