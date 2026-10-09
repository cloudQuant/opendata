# C77 — two gate instruments that reached their verdicts without reading their inputs

Round focus: while clearing iteration-2 acceptance items I found two places where a green line in
`make gate` was produced by an instrument's *error path* rather than by its measurement. Both are
repairs to the harness, not relaxations: neither changes what a passing run asserts, and one of them
makes a previously-swallowed class of failure visible.

## Artifact inventory

| File | What it is | Face |
| --- | --- | --- |
| `a2-bandit-escape-face.py` | the probe; runs real bandit and a shaped byte stream through both the old and the new leg | `a2-bandit-escape-face.txt` |
| `a2-bandit-escape-face.txt` | verbatim stdout of the probe with its tree/digest header | — |
| `baseline-scope-widening-screen.py` | measures AC-10\|03's matched scope under the pinned 5-provider baseline and under a 10-provider one, plus a clone control | face pending — the run is in flight as of this commit |

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
candidates) is measured rather than quoted.

Not established:

- that any iteration-2 acceptance item moved to `proven` by this — C77 is harness repair;
- the widening screen's verdict on AC-10|03's matched scope (run in flight; its first attempt
  crashed inside my own clone control — the control path sat outside `openbb_platform/providers`, so
  `Block.is_baseline` filed the injected block as *local* and the shipped `_pair_sort_key` raised
  `KeyError: 'baseline_path'`. The arms printed; the calibration did not. Fixed and re-running);
- that gate 6 is green: nothing here has been through the full 17-member run.

## Next

1. Land the widening screen face; decide between widening `EXPECTED_BASELINE_PROVIDERS` and
   disclosing the five packages' empty matched scope.
2. Rebuild the provider source-review bundle over the 12-package census and replace `judge_ac10_03`'s
   hardcoded `"7"` / `"71"` / provider-name literal with a registry-derived expectation plus a
   missing-package counterfact (task #47).
3. AC-16|01/02 per-package clean-room records and commit attestations, `gap` with disclosure wherever
   OpenBB source was actually consulted.
