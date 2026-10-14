# C103 / batch5-source-pins — 2 stale per-file source pins inside the batch5 guard

## Fault

``tests/test_provider_domains.py::_batch5_identity_snapshot()`` asserts a three-link
digest chain: the payload digest (:1363), a per-file loop over ``source_hashes_sha256``
(:1366-1368) covering ``31`` keys under ``opendata/``, ``docs/`` and
``tests/fixtures/``, and ``candidate_sha256`` (:1371). ``31`` keys measured,
``matched=29 stale=2`` -- both stale entries are yfinance model files
(first: ``opendata/data/providers/yfinance/models/iter2.py``), each with ``vs_HEAD_numstat=2`` added lines --
a small edit iteration 2 made for its own reasons. A digest pin is a *census*
instrument, so the standing ruling allows the pin to be
re-derived from the bytes on disk; it does not allow the reviewed semantics to move.

## Repair, and the arm that makes it honest

``probes/pin_rebase.py`` rewrites only the stale entries, prints every
``before=``/``live=`` pair with its ``vs_HEAD_numstat``, and refuses if any other key,
the payload digest's own structure, or ``candidate_sha256`` drifts. It re-runs as its
own counterfact: after the apply a second dry pass reports
``matched=31 stale=0`` and exits ``1``, because
re-pinning a digest that already matches would erase whatever the next edit does to
that file.

## Selection faces, before and after

```text
runs/pytest-selection-before.txt
  7 failed / 110 passed  (collected 117)
runs/pytest-selection-after-pins.txt
  1 failed / 116 passed   (collected 117)
```

The one face still red after the pins is not a digest: it is the OECD
``scenario_present`` row, a semantic disagreement. It was not re-pinned away -- it is
what ``../batch5-scenario-census/`` decides.

## Recompute

```text
/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-source-pins/probes/pin_rebase.py     # dry; prints the delta, writes nothing
```
