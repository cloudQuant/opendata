# C104 / h15-series-enumeration -- the faces, quoted out of the carriers

Every line below is copied out of the named carrier by `probes/face_builder.py`; no number
in it was typed. The carriers hold the full bodies and their own headers.

## probe-pass.txt

```text
probe_exit=0
face: probe_const_allowed_regexes=6 config_regexes=6
face: probe_const_equals_config_regexes=True
face: twelfth_id_enumerated=True
face: spec_distinct_source_key=37 series_id_shaped=19
face: series_ids_enumerated_in_config=12 of 19
face: staged_payload_files=1515
arm current: exit=0 findings=0 identities=[]
face: dropped_cfg_is_config_minus_one_line=True
arm dropped: exit=1 findings=1 identities=['opendata/data/providers/federal_reserve/specs.py:342#generic-api-key']
arm sibling: exit=1 findings=1 identities=['opendata/data/providers/federal_reserve/specs.py:342#generic-api-key']
arm credential: exit=1 findings=1 identities=['opendata/data/providers/federal_reserve/specs.py:342#generic-api-key']
face: workdir_removed=True
verdict: PASS
```

## finding-triage-identity-only.txt

```text
triage_exit=0
face: allowlist_free_config=every rules.allowlist removed
face: allowlist_free_findings=18
face: allowlist_free_files=2
face: shipped_config_config=repo .gitleaks.toml as it stood pre-edit
face: shipped_config_findings=1
face: shipped_config_files=1
verdict: 18 findings allowlist-free, 1 under the shipped config; every flagged value is a Fed
triage_exit=0
```

## pytest-allowlist-consumers.txt

```text
pytest_exit=0
collected 45 items
============================= 45 passed in 16.49s ==============================
```

## staged-payload-rescan.txt

```text
rescan_exit=0
face: staged_paths=1528 copied=1528 bytes=28848593
face: missing_on_disk=0
face: c104_files_in_payload=11
face: config_edits_in_payload=['.gitleaks.toml', 'scripts/quality/acceptance_item_probe.py']
face: scan_exit=0
face: findings=0
verdict: PASS
```

## secret-check-before-edit.txt

```text
secret_check_exit=0
[90m3:06AM[0m [32mINF[0m [1m378 commits scanned.[0m
[90m3:06AM[0m [32mINF[0m [1mscanned ~184488478 bytes (184.49 MB) in 1m34.2s[0m
[90m3:06AM[0m [32mINF[0m [1mno leaks found[0m
```

## secret-check-after-edit.txt

```text
secret_check_exit=0
[90m3:12AM[0m [32mINF[0m [1m378 commits scanned.[0m
[90m3:12AM[0m [32mINF[0m [1mscanned ~184488478 bytes (184.49 MB) in 1m41s[0m
[90m3:12AM[0m [32mINF[0m [1mno leaks found[0m
```
