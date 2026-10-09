# C76 — member-12 reds closed on measured faces, and two of my own claims refuted

Round narrative for `docs/evidence/C76/`. Gate run 4 (`docs/evidence/C75/gate-run4-red-member12.txt`)
was red inside member 12 with `3 failed, 21275 passed, 86 skipped in 1323.27s`; the coverage ratchet
itself was green (`TOTAL … 87.89%`, floor 84%), so all three were correctness failures. This round
fixed them by measuring, and in the process refuted two mechanism claims — one I had committed and one
I had drafted.

## What was wrong, and what the fix is

| # | red | cause established by | fix |
| --- | --- | --- | --- |
| 1 | `test_global_snapshot_lock_blocks_writer_and_expired_shard_purge_processes` | Face A (the run-4 excerpt) + Faces B-D | the worker now stamps its own stage as it goes; the wait stops the worker *before* reading its pipes, so the failure path can no longer deadlock on the lock it is accusing; `WORKER_STARTUP_BUDGET_S` 90 → 240, `WORKER_FINISH_BUDGET_S` = 120 replaces two bare `communicate(timeout=30)` calls |
| 2 | `test_slow_fragmented_body_uses_one_admission_deadline` | Face E (sleep-overshoot arms) | `ADMISSION_DEADLINE_S`/`BODY_CHUNK_S` 0.1/0.07 → 1.0/0.6, keeping the discriminating relation `each chunk < deadline < sum of chunks`; plus a positive control that the same body completes when the deadline is not the limiter |
| 3 | `test_the_roadmap_needs_are_shipped_yet_do_not_unblock` | the C75 label split, re-read in `capability-roadmap.json` | the test's premise and the stale prose in `opendata/data/providers/sec/specs.py` were corrected to the post-split face: sec's `shipped_cover_rows` measures empty and its three rows sit in the build pool |

No judge was relaxed: no assertion was deleted and no threshold lowered. Both new budget constants are
gated by counterfacts that fire in seconds —
`test_a_dead_worker_is_reported_dead_not_as_a_lock_miss` and
`test_a_stalled_alive_worker_is_killed_and_reported_by_stage` — so widening a timeout cannot hide a
broken worker.

## The two refutations

1. **`gate-run4-header-erratum.txt`** — the C75 header claimed the purge arm "can never signal" because
   its marker write sits under `if not exclusive:`. False: the write happens *before* the blocking
   `flock`, and `purge_expired_minute_shards` (`minute_archive.py:914`) does reach the non-exclusive
   snapshot lock through `_shard_lock` (`:968` → `:162`). The three-way deadlock on the *failure* path
   is real and is what the fix removes; the cause of the wait expiring is a budget too tight for a
   traced worker under gate load.
2. **Face B** (`worker-cov-env-probe.py` + `worker-cov-env-face.txt`) — I had drafted a comment claiming
   coverage tracing never reaches the test's own `python -c` worker. Measured the other way:
   `COV_CORE_CONFIG`/`COV_CORE_DATAFILE`/`COV_CORE_SOURCE` are present in the controller, in each xdist
   worker, and inherited by the grandchild, which is exactly what `pytest-cov.pth` keys its
   `embed.init()` on. Traced boot costs 0.115-0.131 s against 0.04 s untraced. The wrong sentence never
   reached a commit; the erratum records it anyway.

## Artifacts

- `member12-timing-measurements.txt` — the carrier: Faces A-F with raw output, re-run recipes, the
  derived-constants table, and two explicitly void measurements kept out of every constant.
- `worker-cov-env-probe.py` / `worker-cov-env-face.txt` — three-level `COV*` env + traced-boot cost.
- `worker-startup-contention-probe.py` / `proxy-run-face.txt` / `proxy-run-face.err` — the lock test
  with seven coverage-traced pytest peers, six repeats.
- `lock-test-tracing-pair.sh` / `lock-test-tracing-pair-face.txt` — the same test traced vs untraced,
  back to back.
- `saturation-probe.py` / `saturation-probe-face.txt` — pure-CPU proxy: import multiplication and the
  `asyncio` sleep-overshoot arms.
- `gate-run4-header-erratum.txt` — the refuted mechanism sentence, quoted verbatim from the C75 archive.
- `gate-run5-traceability-red.txt` — gate run 5 verbatim (69 lines, `sha256` pinned in its header):
  members 1-4 green on the C76 tree, member 5 red on this directory's missing narrative and the C75
  header's missing command face. It is the carrier for both claims two lines below.

## One face-formatting edit inside the C75 archive

Member 5 (`evidence-traceability`) charged `gate-run4-red-member12.txt` with a `command` gap: its rule
reads the first 40 lines and matches a command token, and my header had written the command only inside
backticks (`` `make gate` ``), whose preceding backtick is neither `^` nor whitespace. Line 8 now prints
the same claim with the tokens unquoted. The edit is one line, in place, adds no line, changes no
number, and leaves the digest-pinned log body untouched — `git diff HEAD~1 -- docs/evidence/C75/` shows
exactly this one line.

## Established / not established by this round

- Established: the three member-12 tests pass under tracing (`76 passed` across the three files,
  the coverage floor failing only because a subset cannot reach 84%); the faces above are
  re-derivable from the committed instruments.
- **Refuted mid-round: my own "ruff clean" claim.** As first written this bullet said "ruff clean",
  which was measured only over `tests/`. Gate member 8 (`a2-check`) walks the A2 set — everything
  added since the recorded baseline, `docs/evidence/**/*.py` included — and read `A2 files: 713`,
  `FAIL: A2 gate failed on: ruff check, ruff format, bandit` with **43 ruff findings and 6 bandit
  findings** concentrated in this round's three probe scripts. Run 5 could not have caught it:
  `make gate` orders `a2-check` 8th, behind the `evidence-traceability` that stopped it. The three
  files are now clean under the same reading path (`ok ruff check`, `ok ruff format --check`,
  `ok mypy`, `ok bandit`, `OK: A2 files meet the full A2 standard`), and the cleanup is
  behavior-unchanged: `SPIN_CODE`/`WORKER_IMPORTS` argv bodies, the printed labels and the face
  columns are the same strings, so a re-run reproduces the *shape* of `saturation-probe-face.txt`:

      cold import of minute_archive (load=0) n=  4 median=0.6493 p95=0.6605 max=0.6788
      asyncio sleep 0.3s overshoot       n= 30 median=0.0022 p95=0.0023 max=0.0023

  versus the archived 1.7940 / 0.0510. The digits are box state and are not re-derivable — which is
  why the constants were derived from the archived maxima (2.0059 s idle import, 51 ms sleep
  overshoot, 9.6654 s under 12 spinners) and not from this re-run. Re-running a timing instrument
  does not re-certify a budget; only the recorded instance does.
- Not established: gate members 13-17 (frontend), which run 4 never reached. Run 5
  (`gate-run5-traceability-red.txt`, archived here) is the run that stopped inside member 5 on exactly
  the two violations fixed above — this directory's missing narrative face and the C75 header's missing
  command face. Members 1-4 of run 5 were green (`brand-check`, `zero-dep-check`, `secret-check` with
  `gitleaks … no leaks found`, `ledger-check` with `items=130 proven=112 gap=18 unreviewed=0 ticked=112`
  across 22 groups and 19 §10 rows). Run 5 also did not reach member 12, so the three test fixes are
  proven only by the focused invocation, not by a suite pass. The next full pass is run 6.

- Explicitly out of scope: no provider call, no warehouse write, no `--cov` config change, no
  dependency pin. The 240 s budget is 2× the one observed instance, an attribution of the tail to box
  state (8 cores, 8 traced workers, 16 GB) by elimination over Faces B/C/D/E — not a prediction.
