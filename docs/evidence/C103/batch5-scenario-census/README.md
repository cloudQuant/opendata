# C103 / batch5-scenario-census — 9 OECD scenario rows: a census move, and the conflict underneath it

## The disagreement, measured

The batch5 oracle pins ``scenario_present`` per ``(source, model)``, and
``tests/test_provider_domains.py:1784`` re-reads it as
``assert (binding.scenario is not None) is row["scenario_present"]``. The live census
(``probes/scenario_census.py``, read-only) measures ``snapshot_rows=351 live_bindings=351`` with
``live_scenario_present_total=351`` against ``pin_scenario_present_total=342``:
``9`` rows disagree, all in one direction and one source --
``scenario_moves_by_source={'oecd': 9}``, ``move_directions={'on': 9}``.

Why this is a re-base and not a semantic change: the iteration-2 消费用途 backfill wrote *descriptive
scenario strings* onto those bindings. A scenario string is a usage annotation; the batch5
approved-use-case adjudication (``scenario_policy`` with ``scenario_present: False``) is a different
field and is untouched. Under the standing ruling the annotation counts are census and may be
re-derived, while the reviewed semantics stay byte-frozen. The two partitions the fixture carries are
each self-consistent and still disagree on these rows, so the disagreement is **recorded** in
``registry_census_rebase.c103_scenario_rebase`` instead of being resolved by this round.

## The write, and the gates that could have stopped it

``probes/scenario_rebase.py`` first recovers the committed pin by hashing every
``git log --all -- <fixture>`` blob (it lands on ``360c61dd0e85ca82c90720400d4f3d6b4deb1f68``), then rebuilds the live census exactly the
way the guard does and refuses on: a semantic move outside ``identity_renames``, a removed identity, an
unbalanced split, an empty flip set, an export count disagreeing with live, a serializer round-trip that
is not byte-identical, or any frozen field moving. It wrote ``fixture_row_flips_needed=9`` rows
and nothing else. Re-run after the apply it reports ``0`` needed and exits ``1``.

``runs/scenario-rebase-dry-3-shape-gate-refusal.txt`` is kept on purpose: it is the run where my own
comparator was caught judging a record-shaped block with an int-shaped key test and printing
``live_counts.non_contract_exports=None`` while live measured 4. A probe that mis-shapes its own gate
is the finding, and the refused run is the evidence that the gate fires.

## Why the counterfact has to re-pin the digest

``probes/scenario_counterfact.py`` builds a shadow root under ``/tmp`` and flips one row
(``oecd/GdpReal`` ``true -> false``). Flipping the fixture alone kills the guard earlier, at the
payload-digest assertion on :1363, so the scenario assertion is never reached and a "red" run proves
nothing about the field under test. Arm A therefore also re-pins ``BATCH5_SNAPSHOT_SHA256`` in the
*copied* module, and the probe only PASSes when arm A's failure line is verbatim
``>           assert (binding.scenario is not None) is row["scenario_present"]`` while untampered arm B exits 0. Measured ``scenario_counterfact_exit=0``,
``shadow_removed=True``, ``tracked_files_unchanged=True``.

## The two payload literals

``probes/payload_literal_repin.py`` re-pins the literals that hash the snapshot payload -- one in
``tests/test_provider_domains.py``, one inline in ``tests/test_pipeline_jobs.py`` -- to
``786fb181884ea564c80c1883601d6aaaa3f1d56ba6758ce45727a68efbaa8d88`` (HEAD still holds ``7ce0ca613405e10dc3616e2cd1e6964b957331eb068a20ce49d997927b49c9f8``; the pre-edit literal was
``a8715f16248ab2ce4d3e9e27200fc17b07fa2d888a2f45fc56d2e6f8e8e041a6``). It resolves the literal by *exclusion* of every 64-hex the fixture itself
records, requires exactly one candidate per carrier, confines the edit to the hex window, and measures
the window in **character** offsets: the second carrier has ``non_ascii_chars_before_literal=71``
and byte offsets there read ``inside_hex_window=False`` on a correct edit. Re-run after the
apply it refuses with 0 candidates and exits ``1`` -- the re-pinned digest is now
recorded by the fixture, so it is correctly no longer a candidate.

## Whole-suite face

```text
tests/test_fmp_iter2_market.py                           10 ->   0
tests/test_provider_domains.py                            4 ->   0
tests/test_alert_matrix.py                                2 ->   0
tests/test_alert_matrix_model_domains.py                  1 ->   0
tests/test_provider_model_contracts.py                    8 ->   8
TOTAL                                                    56 ->  39
```

Both derived carriers print their own ``face: failed_lines=`` and the FAILED population counted out of
each file agrees with it (56 and 39). Both readings satisfy
``collected == failed + passed + skipped`` at 26551. Nothing outside the four listed modules moved
between the two readings except the faces the re-base cleared, and the TOTAL line is the arithmetic
this family claims (cleared = 17).

The four-file selection that carries the batch5 guard re-reads green at this head
(``pytest-selection-117-reread-this-turn.txt`` -> collected 117, ``c117_selection_exit=0``); that run is also what proves the
argv printed on the archived before/after receipts, because 117 is the denominator all three of them
print.

``tests/test_provider_model_contracts.py`` is deliberately *not* in this family: it reads the same
number of faces before and after, so its mechanism is declaration-vs-contract typing rather than a
stale pin. It is attributed in the dev log and left red.

## Recompute

```text
/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/scenario_census.py        # read-only census
/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/scenario_rebase.py        # dry; refuses once applied
/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/payload_literal_repin.py  # dry; refuses once applied
/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/scenario_counterfact.py   # two-sided; writes nothing
```
