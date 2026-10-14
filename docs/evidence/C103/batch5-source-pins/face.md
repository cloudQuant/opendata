# batch5-source-pins — face readings (verbatim carrier lines)

Each block is copied out of the named carrier under ``runs/``; the carrier is the archive and this file is its index. Recompute a block with the ``argv=`` printed under it.

## runs/pin-rebase-apply.txt

```text
source_sha256_16=5fc6dcf676807719
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-source-pins/probes/pin_rebase.py --apply
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
pin_rebase_apply_exit=not_captured_at_run_time (verdict read from the body)
face: repo_root=/Users/yunjinqi/Documents/new_projects/opendata
face: argv=['--apply']
face: pin_entries=31
face: snapshot_payload_sha_before=3755f13aaba43989373aa44c6ca3f43fa9aabd2e0b2681b100628cac0d9a7bc3
face: candidate_sha256_before=775dd322e145d84f0a1a850d46fd5f0754814ac68445c3b02bab4a41239e047e
face: matched=29 stale=2 missing=0
face: stale[opendata/data/providers/yfinance/models/iter2.py] before=c7341d4a1169381cc48846182a1c51b485587bfd031eb4ece22eab2316d68f88
face: stale[opendata/data/providers/yfinance/models/iter2.py] live =88fd4471823a7db319d7d08b25fc5f65a92999cc275e9844583633bbd395f7af
face: stale[opendata/data/providers/yfinance/models/iter2.py] vs_HEAD_numstat=2	2	opendata/data/providers/yfinance/models/iter2.py
face: stale[opendata/data/providers/yfinance/models/market_iter2.py] before=2feebf5a386af8a98a66de9716033f6375a2089264134c239bae43e8a6d1246a
face: stale[opendata/data/providers/yfinance/models/market_iter2.py] live =3678f6c8a851822730660ac80d360785723ce426bddead0ad9c059db9e3060ea
face: stale[opendata/data/providers/yfinance/models/market_iter2.py] vs_HEAD_numstat=2	2	opendata/data/providers/yfinance/models/market_iter2.py
face: snapshot_payload_sha_after=a8715f16248ab2ce4d3e9e27200fc17b07fa2d888a2f45fc56d2e6f8e8e041a6
face: candidate_sha256_after=775dd322e145d84f0a1a850d46fd5f0754814ac68445c3b02bab4a41239e047e
face: candidate_unchanged=True
face: snapshot_id_unchanged=True
face: snapshot_status_unchanged=True
face: counts_unchanged=True
face: identities_unchanged=True
face: pin_keys_unchanged=True
face: bytes_delta=0
face: literal[tests/test_provider_domains.py] occurrences_of_old_payload_sha=1
face: literal[tests/test_pipeline_jobs.py] occurrences_of_old_payload_sha=1
face: applied snapshot=True
face: applied[tests/test_provider_domains.py] new_literal_present=True stale_literal=False
face: applied[tests/test_pipeline_jobs.py] new_literal_present=True stale_literal=False
verdict: APPLIED - 2 source pins re-based from live bytes and 2 payload literals re-pinned
```

## runs/pin-rebase-dry.txt

```text
source_sha256_16=d10c2dad31059a2d
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-source-pins/probes/pin_rebase.py
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
pin_rebase_dry_exit=not_captured_at_run_time (verdict read from the body)
face: repo_root=/Users/yunjinqi/Documents/new_projects/opendata
face: argv=[]
face: pin_entries=31
face: snapshot_payload_sha_before=3755f13aaba43989373aa44c6ca3f43fa9aabd2e0b2681b100628cac0d9a7bc3
face: candidate_sha256_before=775dd322e145d84f0a1a850d46fd5f0754814ac68445c3b02bab4a41239e047e
face: matched=29 stale=2 missing=0
face: stale[opendata/data/providers/yfinance/models/iter2.py] before=c7341d4a1169381cc48846182a1c51b485587bfd031eb4ece22eab2316d68f88
face: stale[opendata/data/providers/yfinance/models/iter2.py] live =88fd4471823a7db319d7d08b25fc5f65a92999cc275e9844583633bbd395f7af
face: stale[opendata/data/providers/yfinance/models/iter2.py] vs_HEAD_numstat=2	2	opendata/data/providers/yfinance/models/iter2.py
face: stale[opendata/data/providers/yfinance/models/market_iter2.py] before=2feebf5a386af8a98a66de9716033f6375a2089264134c239bae43e8a6d1246a
face: stale[opendata/data/providers/yfinance/models/market_iter2.py] live =3678f6c8a851822730660ac80d360785723ce426bddead0ad9c059db9e3060ea
face: stale[opendata/data/providers/yfinance/models/market_iter2.py] vs_HEAD_numstat=2	2	opendata/data/providers/yfinance/models/market_iter2.py
face: snapshot_payload_sha_after=a8715f16248ab2ce4d3e9e27200fc17b07fa2d888a2f45fc56d2e6f8e8e041a6
face: candidate_sha256_after=775dd322e145d84f0a1a850d46fd5f0754814ac68445c3b02bab4a41239e047e
face: candidate_unchanged=True
face: snapshot_id_unchanged=True
face: snapshot_status_unchanged=True
face: counts_unchanged=True
face: identities_unchanged=True
face: pin_keys_unchanged=True
face: bytes_delta=0
face: literal[tests/test_provider_domains.py] occurrences_of_old_payload_sha=1
face: literal[tests/test_pipeline_jobs.py] occurrences_of_old_payload_sha=1
verdict: DRY RUN - nothing written (pass --apply to write)
```

## runs/pin-rebase-idempotence-arm.txt

```text
source_sha256_16=6c0a575d191f0160
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-source-pins/probes/pin_rebase.py   (re-run after the apply; must refuse)
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
pin_rebase_dry_exit=1 (re-captured this turn)
argv=python docs/evidence/C103/batch5-source-pins/probes/pin_rebase.py (dry)
face: repo_root=/Users/yunjinqi/Documents/new_projects/opendata
face: argv=[]
face: pin_entries=31
face: snapshot_payload_sha_before=786fb181884ea564c80c1883601d6aaaa3f1d56ba6758ce45727a68efbaa8d88
face: candidate_sha256_before=775dd322e145d84f0a1a850d46fd5f0754814ac68445c3b02bab4a41239e047e
face: matched=31 stale=0 missing=0
verdict: REFUSED - no stale pin to re-base, refusing to re-pin a moving digest
pin_rebase_dry_exit=1
```

## runs/pytest-selection-after-pins.txt

```text
source_sha256_16=336c0c875e3c6200
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python -m pytest tests/test_provider_domains.py tests/test_pipeline_jobs.py tests/test_alert_matrix.py tests/test_alert_matrix_model_domains.py -q --no-cov -p no:cacheprovider --tb=line
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
pytest_selection_after_pins_exit=not_captured_at_run_time (1 failed; pytest exits 1 on any collected failure)
```

## runs/pytest-selection-before.txt

```text
source_sha256_16=d6ab1dec20f995ec
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python -m pytest tests/test_provider_domains.py tests/test_pipeline_jobs.py tests/test_alert_matrix.py tests/test_alert_matrix_model_domains.py -q --no-cov -p no:cacheprovider --tb=line
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
pytest_selection_before_exit=not_captured_at_run_time (7 failed; pytest exits 1 on any collected failure)
```

