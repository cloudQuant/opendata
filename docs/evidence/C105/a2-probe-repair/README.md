# C105 / a2-probe-repair — four static planes over 34 shipped probes, and the one printed value a re-wrap moved

## What had to be cleared, and what the plane actually checks

The inherited task was to fix the ruff findings in the probes that rounds C101-C104 shipped
**without changing a probe verdict**, then re-run the probes and confirm the faces came back
identical. `scripts/quality/a2_check.py` does not stop at ruff: its typed-file selection excludes
only the test tree, so a probe under docs/evidence is *also* a mypy target and a bandit target. The
before picture is the carrier `a2-before-33-probes.txt`, the gate over the C104-era targets, and it
exits `before_a2_exit=1`.

The scope is committed as `targets.txt` — 34 paths: the 33 repaired probe files plus this round's
`docs/evidence/C105/a2-probe-repair/probes/docs_reidentity.py`.

## The four planes, read one at a time

- `python -m ruff check --output-format=concise <the 34 probe targets in targets.txt>` -> `ruff_check_exit=0`
- `python -m ruff format --check <the 34 probe targets in targets.txt>` -> `ruff_format_exit=0`
- mypy per target with the gate argv, bandit as one batch (`python /tmp/c105_planes.py`):
  `mypy_targets=34 targets_with_errors=0 mypy_errors_total=0`, `bandit_exit=0 bandit_results_total=0`
- this round's own five probes on the same four planes (`python /tmp/c105_own_planes.py`):
  `own_probes=5`, `ruff_check_exit=0`, `ruff_format_exit=0`, `mypy_targets=5 targets_with_errors=0 mypy_errors_total=0`,
  `bandit_exit=0 bandit_results_total=0`

Bandit exits clean while printing 86 nosec warnings, so the warnings were attributed rather than
shrugged at: `nosec_warnings_total=86 warnings_parsed_to_file_line=86`, of which
`unused_nosec_on_nosec_lines=11 files=7` sit on a line that already carries a `# nosec` comment and
`other_warnings_not_on_a_nosec_line=75` do not. The reason the second class is large is measured in
C104's own face: bandit reads a `# nosec` directive as statement-scoped, so a code parked on an argv
line several statements below the call does not clear the call. Nothing here was "fixed" by moving a
directive onto a line just to silence a warning — the exit code was already clean.

## The repair is value-neutral, and the witness that says so is the one that fails

`python docs/evidence/C105/a2-probe-repair/probes/ast_witness.py <the 34 targets>` compares each
worktree file with its staged bytes at AST level (docstrings and annotations stripped) and refuses
any printed value that moved:

`targets=34 ast_identical=12 values_intact=15 import_churn=3 whitespace_reflow=0 value_moves=1 pre_state_absent=3 findings=4`
`verdict: FAIL - a repair moved a printed string value, not only its line length`

The five statuses account for every target: 12 files re-wrapped to byte-identical syntax, 15 with
imports restructured around unchanged values (`added_import=Callable`, `pruned_import=shutil`,
`pruned_import=re`), 3 pure import churn (`status=import_churn`), 3 with no staged predecessor to
compare against (`status=pre_state_absent`, the new probes of this round), and one `status=moved`.
That one is `docs/evidence/C103/probes/document_builder.py`, `whitespace_pairs=37`, `value_moves=4`:
two dropped string values and two added ones, all four of them README prose and a regex that the
builder splices out of a docstring — the re-wrap moved a section heading across a function boundary.
The witness is armed, not decorative: `python docs/evidence/C105/a2-probe-repair/probes/ast_witness.py --control`
tampers one literal in memory and reports `untampered_status=identical tampered_status=moved tampered_findings=2`,
`control_arm=as expected`, `mode=control controls_passed=1 controls_failed=0`.

## So the one real finding was judged at the level of what the file produces

`document_builder.py` emits README and face documents, so a moved string is only a finding if the
rendered document changes. `python docs/evidence/C105/a2-probe-repair/probes/docs_reidentity.py`
rebuilds the C103 documents from the staged builder and from the repaired builder and compares them
at three levels — bytes, paragraph blocks, claim tokens:

`documents_in_scope=6 builder_lines=394 evidence_dirs_in_snapshot=4`, `head_written=documents_written=6 live_written=documents_written=6`
`arm_head_exit=0 arm_live_exit=0 arm_prose_armed=True arm_claim_armed=True`

Six `identity:` lines follow, one per document: the three face files are `bytes_same=True`, the three
READMEs are `bytes_same=False` with `byte_delta=0`, `byte_delta=-6` and `byte_delta=2` while every one
of them keeps `blocks_same=True tokens_same=True`. Each level of that comparison is armed by its own
clean builder run rather than asserted: `candidate_pool_prose=49 candidate_pool_claim=43`, the prose
control at `control_prose_index=36` moves `blocks_moved=3` with `candidates_skipped=0`, the claim
control at `control_claim_index=573` moves `tokens_moved=1` after `candidates_skipped=19` candidates
that either exited non-zero, wrote a stray directory, or left the level unchanged, and
`control_arms_distinct=True levels_armed=2`. The probe leaves the repository as it found it:
`repo_restored=True stray_dirs_left=0`, `verdict: PASS - bytes moved in 3 document(s) by re-wrapping only; every paragraph block and every claim token is intact, and each level is armed by its own clean builder run`.

That is why the round ships with the witness still reading FAIL. FAIL here is the correct reading of
a 4-value move in a generator whose product is provably unchanged at the block and token level; the
alternative — editing the witness or re-pinning the builder's bytes until it reads green — would
have deleted the only finding the static planes produced.

## The verdicts did not move

`python /tmp/c105_capture3.py` re-ran six of the repaired probes and printed each one's own verdict
line: `h15_allowlist.py`, `face_builder.py`, `payload_rescan.py`, `carrier_archiver.py` and
`document_builder.py` each `probe_exit=0` with their `verdict: PASS` unchanged, and
`scenario_rebase.py` still `probe_exit=1` with `verdict: REFUSED - the fixture rows already match live, nothing to re-base`
— the same refusal it produced before the repair, which is the reading that was expected of it.

## The repair drivers are not reproducible; their effect is

The edits were applied by scripts that lived in a scratch directory and were not re-taken after the
fact, so they are recorded as non-reproducible and proved by effect instead. Three of them are
all-or-nothing writers: re-run over the repaired tree they report `edits_applied=0 files_touched=17 reverted=True`
and `edits_applied=0 files_in_scope=14 reverted=True` with `verdict: REFUSED - an anchor was not unique or an edit broke a file, nothing written`,
and the bandit driver re-runs to `bandit_finding_sites=0 codes={}`. The whole scope then hashes
unchanged: `targets_hashed=34 changed_after_repair_reruns=0 names=[]`. Two drivers that insert by
line number, or write without reverting, were deliberately not re-run, because a second execution of
those would duplicate a docstring or leave a partial write — that is recorded as an absence, not as
a passing arm.

## The C104 carriers, and the one side effect of re-running its archiver

The C104 archiver was one of the re-run probes, and it rewrote the six carriers it had shipped:
`carrier_changed_lines_total=12 carriers_in_scope=6`, two lines per file — the capture timestamp and
the worktree dirty-file count, nothing else. The original capture is the record, so the staged bytes
were written back rather than re-aged; the diff and the restore are both in that carrier.

## What this round does not claim

- It does not claim the A2 plane is green end to end. It reports the four *static* planes over the 34
  targets, plus the before picture `before_a2_exit=1` that the round was asked to clear.
- `carriers=11` is the count the face writer printed on the run its own carrier holds. A receipt
  cannot archive the run that creates it, so that line is one archive cycle older than the page. The
  projection itself is reproducible: re-running
  `docs/evidence/C105/a2-probe-repair/probes/face_writer.py` over the shipped carriers rebuilds
  `face.md` byte-for-byte.
- Nothing in this document is typed from memory. Every line of `face.md` is copied out of a carrier
  under runs/, and every carrier is the verbatim stdout of the run its own `argv=` header records,
  with `source_bytes=` and `source_sha256_16=` measured off that file
  (`docs/evidence/C105/a2-probe-repair/probes/carrier_archiver.py`,
  `docs/evidence/C105/a2-probe-repair/probes/face_writer.py`).
