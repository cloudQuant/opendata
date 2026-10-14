# C104 / h15-series-enumeration — the twelfth H.15 series id, and the enumeration that said eleven

## What was reported

The pre-commit credential scan of the exact staged payload (1515 files, 27 868 574 bytes) reported
18 findings with every rule allowlist stripped and **1** under the shipped `.gitleaks.toml`:

```text
opendata/data/providers/federal_reserve/specs.py:342#generic-api-key
```

`runs/finding-triage-identity-only.txt` carries the whole population as `file:line#rule` plus the
length of each flagged value — 12 in `federal_reserve/specs.py` (14 chars each) and 6 in
`tests/test_ecb_series_fetchers.py` (30 and 45 chars). None of the 18 is a credential: they are Fed
H.15 series identifiers (`RIFLGFC*_N.B`, `RIFSGFSW04_N.B`) and ECB Data Portal series keys
(`YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M`, `BPS.M.N…ALL`). `generic-api-key` reads the **field name**
`source_key` as a key name and the value as a key value, so a public series id written under it is
a finding.

The 17 that stayed quiet under the shipped config are covered by C36's SDMX shape regex and C75's
eleven-item enumeration. The one that did not is line 342 -- `week_4`, the **4-week bill**, which
the pinned capture prints LAST in its header row. `specs.py:52` says so in the file's own docstring:
*twelve ids, the 4-week bill printed LAST (sha256 23f3ed8ae67b4d7d)*. The enumeration said eleven.

## Why this is an enumeration and not a relaxation

`.gitleaks.toml` refused a value-class rule for exactly this field, and the reason is recorded in the
file: *"A value-class rule ('anything under a field called source_key') would silence a real
credential"*. So the fix adds one exact literal, `^RIFSGFSW04_N\.B$`, on its own line -- leaving
C75's regex byte-identical so its arms [16]/[17], which commit and drop *that* line, still mean the
eleven they were measured on.

The constant `scripts/quality/acceptance_item_probe.py:GENERIC_API_KEY_ALLOWED_REGEXES` is the other
half: `tests/test_acceptance_item_probe.py:715` asserts the config's list **equals** that tuple, so
a one-sided edit goes red there. Measured `probe_const_equals_config_regexes=True` at 6 entries, and
`tests/test_acceptance_item_probe.py` + `tests/test_env_example_security.py` re-read
`collected 45 items` -> `45 passed`, `PYTEST_EXIT=0`.

## The four arms (`probes/h15_allowlist.py`, exit 0)

| arm | config | payload | reading |
| --- | --- | --- | --- |
| `current` | repo `.gitleaks.toml` as it now stands | the exact staged payload | `exit=0 findings=0` |
| `dropped` | the same config minus that one line | the same payload | `exit=1 findings=1` at `specs.py:342#generic-api-key` |
| `sibling` | shipped | planted `RIFSGFSW08_N.B` (unenumerated neighbour) | `exit=1 findings=1` |
| `credential` | shipped | planted 60-char high-entropy token under `source_key=` | `exit=1 findings=1` |

`dropped` is the counterfact for the write: remove the entry and the finding returns, so the entry --
not a re-run or a wider rule -- is what closed it. `sibling` and `credential` are the arms that make
"enumeration" a measured word rather than a claim: if the added line opened a family class
(`^RIFSGFSW\d+_N\.B$`) or a field class (anything under `source_key`), either plant would go quiet.
Both stay reported, each at exactly one finding with `generic-api-key`.

Pre-edit the probe refuses instead of passing: `twelfth_id_enumerated=False`, `arm current: exit=1
findings=1` at `specs.py:342#generic-api-key`, `series_ids_enumerated_in_config=11 of 19`,
`verdict: REFUSED failures=1`. That refusal is the positive control -- the instrument detects the
absence of what it is going to require.

## Two readings the file count does not give you

`series_id_shaped=19` but only 12 are enumerated and only 12 are ever reported. The other seven
(`M1_N.M` … `MMFGB_N.M`) never fire `generic-api-key`, and the face that proves it is `current` at
`findings=0` over the whole staged payload -- not a count of ids. Reporting "12 of 19" as if seven
were uncovered would be a different claim than the one measured.

## Gate faces

`make secret-check` (the pinned device, `gitleaks 8.30.1`, argv from
`docs/quality/secret-scan.json`) reads the same on both sides of the edit -- `378 commits scanned`,
`no leaks found`, `SECRET_CHECK_EXIT=0` -- because `RIFSGFSW04_N.B` occurs 0 times at HEAD and 2
times in the worktree. It is new bytes: the history scan cannot see it yet, and the commit is what
would put it there. That is why the enumeration lands *before* the commit rather than as a follow-up.

## Re-scanning the payload the fix itself adds

The triage above measured 1515 staged files / 27 868 574 bytes. The C104 family then added 13 more
paths -- three of them instruments that *talk about* a planted credential (`h15_allowlist.py` builds
its token from four segments for that reason; `.gitleaks.toml`'s header records what happened when a
round wrote one whole). `probes/payload_rescan.py` re-copies the staged set and scans it with the
edited config:

```text
face: staged_paths=1528 copied=1528 bytes=28848593
face: missing_on_disk=0
face: c104_files_in_payload=11
face: config_edits_in_payload=['.gitleaks.toml', 'scripts/quality/acceptance_item_probe.py']
face: scan_exit=0
face: findings=0
verdict: PASS
```

`runs/staged-payload-rescan.txt` carries it. The two counts 1515 and 1528 are not a disagreement;
they are the payload before and after this round's own files joined it.

## Recompute

```text
/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C104/h15-series-enumeration/probes/h15_allowlist.py
/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C104/h15-series-enumeration/probes/payload_rescan.py
/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C104/h15-series-enumeration/probes/carrier_archiver.py
/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C104/h15-series-enumeration/probes/face_builder.py
make secret-check
/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python -m pytest -q --no-cov -p no:cacheprovider --tb=line tests/test_acceptance_item_probe.py tests/test_env_example_security.py
```

The probe reads `/tmp/c103_scan_payload` (the staged bytes, copied verbatim before any edit). If that
directory is gone it refuses with `/tmp/c103_scan_payload is gone, so the arms would not be scanning
the staged bytes` and exits 1, rather than scanning something smaller and calling it the payload.
