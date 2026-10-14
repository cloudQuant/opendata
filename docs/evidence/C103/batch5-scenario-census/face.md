# batch5-scenario-census — face readings (verbatim carrier lines)

Each block is copied out of the named carrier under ``runs/``; the carrier is the archive and this file is its index. Recompute a block with the ``argv=`` printed under it.

## runs/guard-two-file-reread.txt

```text
source_sha256_16=a7c9723ba63ecadb
argv=(printed in the body header)
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
guard_rerun_exit=0 (re-captured this turn)
argv=python -m pytest tests/test_provider_domains.py tests/test_pipeline_jobs.py -q --no-cov -p no:cacheprovider --tb=line
guard_rerun_exit=0
```

## runs/payload-literal-idempotence-arm.txt

```text
source_sha256_16=777a91f3c9b4eec6
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/payload_literal_repin.py   (dry, after the apply: the re-pinned digest is now recorded by the fixture itself, so 0 candidates remain)
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
payload_literal_repin_dry_exit=1 (re-captured this turn)
argv=python docs/evidence/C103/batch5-scenario-census/probes/payload_literal_repin.py (dry)
face: snapshot_bytes=238509
face: digest_recomputed_from_disk=786fb181884ea564c80c1883601d6aaaa3f1d56ba6758ce45727a68efbaa8d88
face: digest_at_git_head=7ce0ca613405e10dc3616e2cd1e6964b957331eb068a20ce49d997927b49c9f8
face: fixture_dirty_vs_head=True
face: tests/test_provider_domains.py hex_literals_not_recorded_by_the_fixture=0 distinct=[]
verdict: REFUSED - tests/test_provider_domains.py has 0 candidate payload literals; refusing to guess which one hashes the snapshot
payload_literal_repin_dry_exit=1
```

## runs/payload-literal-repin-apply.txt

```text
source_sha256_16=aa09916450886c28
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/payload_literal_repin.py --apply
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
payload_literal_repin_apply_exit=not_captured_at_run_time (verdict read from the body)
face: snapshot_bytes=238509
face: digest_recomputed_from_disk=786fb181884ea564c80c1883601d6aaaa3f1d56ba6758ce45727a68efbaa8d88
face: digest_at_git_head=7ce0ca613405e10dc3616e2cd1e6964b957331eb068a20ce49d997927b49c9f8
face: fixture_dirty_vs_head=True
face: tests/test_provider_domains.py hex_literals_not_recorded_by_the_fixture=1 distinct=['a8715f16248ab2ce4d3e9e27200fc17b07fa2d888a2f45fc56d2e6f8e8e041a6']
face: tests/test_provider_domains.py bytes_delta=0 lines_before=1990 lines_after=1990 differing_chars=59 inside_hex_window=True non_ascii_chars_before_literal=0
face: tests/test_provider_domains.py stale_literal=a8715f16248ab2ce4d3e9e27200fc17b07fa2d888a2f45fc56d2e6f8e8e041a6 head_matches_original=False
face: tests/test_pipeline_jobs.py hex_literals_not_recorded_by_the_fixture=1 distinct=['a8715f16248ab2ce4d3e9e27200fc17b07fa2d888a2f45fc56d2e6f8e8e041a6']
face: tests/test_pipeline_jobs.py bytes_delta=0 lines_before=1859 lines_after=1859 differing_chars=59 inside_hex_window=True non_ascii_chars_before_literal=71
face: tests/test_pipeline_jobs.py stale_literal=a8715f16248ab2ce4d3e9e27200fc17b07fa2d888a2f45fc56d2e6f8e8e041a6 head_matches_original=False
face: carriers=2 stale_literals_across_carriers=['a8715f16248ab2ce4d3e9e27200fc17b07fa2d888a2f45fc56d2e6f8e8e041a6']
face: applied tests/test_provider_domains.py readback_matches=True bytes=63851
face: applied tests/test_pipeline_jobs.py readback_matches=True bytes=74017
verdict: APPLIED - 2 payload literals re-pinned to 786fb181884ea564c80c1883601d6aaaa3f1d56ba6758ce45727a68efbaa8d88
```

## runs/payload-literal-repin-dry.txt

```text
source_sha256_16=a7564d14abb039bc
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/payload_literal_repin.py
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
payload_literal_repin_dry_exit=not_captured_at_run_time (verdict read from the body)
face: snapshot_bytes=238509
face: digest_recomputed_from_disk=786fb181884ea564c80c1883601d6aaaa3f1d56ba6758ce45727a68efbaa8d88
face: digest_at_git_head=7ce0ca613405e10dc3616e2cd1e6964b957331eb068a20ce49d997927b49c9f8
face: fixture_dirty_vs_head=True
face: tests/test_provider_domains.py hex_literals_not_recorded_by_the_fixture=1 distinct=['a8715f16248ab2ce4d3e9e27200fc17b07fa2d888a2f45fc56d2e6f8e8e041a6']
face: tests/test_provider_domains.py bytes_delta=0 lines_before=1990 lines_after=1990 differing_chars=59 inside_hex_window=True non_ascii_chars_before_literal=0
face: tests/test_provider_domains.py stale_literal=a8715f16248ab2ce4d3e9e27200fc17b07fa2d888a2f45fc56d2e6f8e8e041a6 head_matches_original=False
face: tests/test_pipeline_jobs.py hex_literals_not_recorded_by_the_fixture=1 distinct=['a8715f16248ab2ce4d3e9e27200fc17b07fa2d888a2f45fc56d2e6f8e8e041a6']
face: tests/test_pipeline_jobs.py bytes_delta=0 lines_before=1859 lines_after=1859 differing_chars=59 inside_hex_window=True non_ascii_chars_before_literal=71
face: tests/test_pipeline_jobs.py stale_literal=a8715f16248ab2ce4d3e9e27200fc17b07fa2d888a2f45fc56d2e6f8e8e041a6 head_matches_original=False
face: carriers=2 stale_literals_across_carriers=['a8715f16248ab2ce4d3e9e27200fc17b07fa2d888a2f45fc56d2e6f8e8e041a6']
verdict: DRY RUN - nothing written (pass --apply to write)
```

## runs/payload-literal-repin-rerun.txt

```text
source_sha256_16=2be1024ab1483d84
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/payload_literal_repin.py --apply  (second apply against the already-edited carriers)
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
payload_literal_repin_rerun_exit=not_captured_at_run_time (verdict read from the body)
face: snapshot_bytes=238509
face: digest_recomputed_from_disk=786fb181884ea564c80c1883601d6aaaa3f1d56ba6758ce45727a68efbaa8d88
face: digest_at_git_head=7ce0ca613405e10dc3616e2cd1e6964b957331eb068a20ce49d997927b49c9f8
face: fixture_dirty_vs_head=True
face: tests/test_provider_domains.py hex_literals_not_recorded_by_the_fixture=0 distinct=[]
verdict: REFUSED - tests/test_provider_domains.py has 0 candidate payload literals; refusing to guess which one hashes the snapshot
```

## runs/pytest-selection-117-reread-this-turn.txt

```text
source_sha256_16=8d1c2546b1d814b6
argv=(printed in the body header) -- this is the run that PROVES the argv attribution above: the reconstructed four-file selection collects exactly 117 items, the same denominator the three archived pytest receipts print (7+110, 1+116, 0+117)
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
c117_selection_exit=0 (re-captured this turn)
argv=python -m pytest tests/test_provider_domains.py tests/test_pipeline_jobs.py tests/test_alert_matrix.py tests/test_alert_matrix_model_domains.py -q --no-cov -p no:cacheprovider --tb=line
c117_selection_exit=0
```

## runs/pytest-selection-after-scenario-rebase.txt

```text
source_sha256_16=891cb052d2f24761
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python -m pytest tests/test_provider_domains.py tests/test_pipeline_jobs.py tests/test_alert_matrix.py tests/test_alert_matrix_model_domains.py -q --no-cov -p no:cacheprovider --tb=line
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
pytest_selection_after_scenario_exit=not_captured_at_run_time (117 passed; pytest exits 0 when every collected item passes)
```

## runs/scenario-census.txt

```text
source_sha256_16=27e864c788f51b84
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/scenario_census.py
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
scenario_census_exit=0 (recorded in the body)
face: snapshot_rows=351 live_bindings=351
face: identity_sets_equal=True
face: rows_without_live_binding=0
face: binding_fields=['canonical_model_ids', 'class_name', 'module', 'scenario']
face: scenario_move_rows=9
face: scenario_moves_by_source={'oecd': 9}
face: live_scenario_present_total=351
face: pin_scenario_present_total=342
face: documented_scenario_moves_by_source={}
face: documented_scenario_present_total={}
face: documented_field_move_counts={}
face: documented_matches_live=False
face: move_directions={'on': 9}
face: example oecd/CompositeLeadingIndicator pin=False live=True
face: example oecd/ConsumerPriceIndex pin=False live=True
face: example oecd/CountryInterestRates pin=False live=True
face: example oecd/GdpForecast pin=False live=True
face: example oecd/GdpNominal pin=False live=True
face: example oecd/GdpReal pin=False live=True
verdict: MEASURED - 9 scenario moves over 351 pinned identities, documented census does not match live
scenario_census_exit=0
```

## runs/scenario-counterfact-first-pass.txt

```text
source_sha256_16=37a3e4fd8c1587f7
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/scenario_counterfact.py
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
scenario_counterfact_first_exit=not_captured_at_run_time (verdict read from the body)
face: live_fixture_digest=786fb181884ea564c80c1883601d6aaaa3f1d56ba6758ce45727a68efbaa8d88
face: test_literal_matches_live=True
face: tamper_target=('oecd', 'GdpReal') rows_matched=1
face: tampered_digest=f9bb80c252d57bc2b0e2211ca7f5f798fab5cbed23d72de397b050ce181c90a8
face: tamper_scope=row_dicts_differing=1 fields_changed=['scenario_present']
face: armA_exit=1 counts={'summary_line': '1 failed in 1.55s', 'failed': 1, 'passed': 0, 'error': 0, 'collected_header': None, 'accounted': 1, 'scenarios_assert_line_in_report': True, 'oecd_gdp_real_named_in_report': True}
face: armB_exit=0 counts={'summary_line': '1 passed in 1.49s', 'failed': 0, 'passed': 1, 'error': 0, 'collected_header': None, 'accounted': 1, 'scenarios_assert_line_in_report': False, 'oecd_gdp_real_named_in_report': False}
face: armA_failing_line='>           assert (binding.scenario is not None) is row["scenario_present"]'
face: armA_names_the_gdp_real_row=True
verdict: PASS - arm A fails the guard at (binding.scenario is not None) is row['scenario_present'] with exit 1; arm B passes the same shadow harness with exit 0
face: shadow_removed=True tracked_files_unchanged=True
```

## runs/scenario-counterfact-reread.txt

```text
source_sha256_16=9bfe6924e73a9a2e
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/scenario_counterfact.py
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
scenario_counterfact_exit=0 (re-captured this turn)
argv=python docs/evidence/C103/batch5-scenario-census/probes/scenario_counterfact.py
face: live_fixture_digest=786fb181884ea564c80c1883601d6aaaa3f1d56ba6758ce45727a68efbaa8d88
face: test_literal_matches_live=True
face: tamper_target=('oecd', 'GdpReal') rows_matched=1
face: tampered_digest=f9bb80c252d57bc2b0e2211ca7f5f798fab5cbed23d72de397b050ce181c90a8
face: tamper_scope=row_dicts_differing=1 fields_changed=['scenario_present']
face: armA_exit=1 counts={'summary_line': '1 failed in 1.54s', 'failed': 1, 'passed': 0, 'error': 0, 'collected_header': None, 'accounted': 1, 'scenarios_assert_line_in_report': True, 'oecd_gdp_real_named_in_report': True}
face: armB_exit=0 counts={'summary_line': '1 passed in 1.49s', 'failed': 0, 'passed': 1, 'error': 0, 'collected_header': None, 'accounted': 1, 'scenarios_assert_line_in_report': False, 'oecd_gdp_real_named_in_report': False}
face: armA_failing_line='>           assert (binding.scenario is not None) is row["scenario_present"]'
face: armA_names_the_gdp_real_row=True
verdict: PASS - arm A fails the guard at (binding.scenario is not None) is row['scenario_present'] with exit 1; arm B passes the same shadow harness with exit 0
face: shadow_removed=True tracked_files_unchanged=True
scenario_counterfact_exit=0
```

## runs/scenario-rebase-apply-rerun.txt

```text
source_sha256_16=c6ce8786c6d74a78
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/scenario_rebase.py --apply  (second apply against the already-written fixture)
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
scenario_rebase_rerun_exit=not_captured_at_run_time (verdict read from the body)
face: iter2_census_rebase_keys=['attribution', 'batch5_adjudication_counts', 'counterfact', 'delta_vs_adjudication', 'frozen_and_unchanged', 'live_counts', 'measured_by', 're_pinned_sources', 'registry_census_rebase', 'scope']
face: registry_census_rebase_keys=['approved_delta_conflict', 'before', 'binding_moves_by_source', 'c103_scenario_rebase', 'counterfact', 'counts', 'derived_from', 'descriptor_partition', 'field_added_by_iter2_census_rebase', 'field_move_counts', 'field_shapes_moved', 'identities_added', 'identities_removed', 'identity_fields_moved', 'identity_renames', 'pin_chain', 'scenario_moves_by_source', 'scenario_present_total', 'semantic_field_moves', 'semantic_fields_untouched']
face: recorded_pin_sha=7ce0ca613405e10dc3616e2cd1e6964b957331eb068a20ce49d997927b49c9f8
face: candidate_revisions=1
face: recovered_pin_rev=360c61dd0e85ca82c90720400d4f3d6b4deb1f68
face: pin_rows=350 pin_scenario_present=309
face: fixture_rows=351 fixture_scenario_present=351
face: live_bindings=351 unresolvable=22
face: unresolvable_examples=['ecb/cpi:LookupError', 'ecb/gdp:LookupError', 'ecb/rate:LookupError', 'fred/cpi:LookupError', 'fred/gdp:LookupError']
face: domain_spec_missing=0 []
face: live_rows=351 live_scenario_present_total=351
face: identities_added=1 identities_removed=0 net=1
face: added_examples=[('finviz', 'EquityScreener')]
face: scenario_moves_on_existing=41 by_source={'cftc': 2, 'eia': 2, 'federal_reserve': 13, 'finviz': 2, 'fred': 2, 'imf': 8, 'intrinio': 2, 'oecd': 9, 'sec': 1}
face: carried_by_added_identity=1
face: binding_moves=13 by_source={'federal_reserve': 13}
face: semantic_moves=contract=3 verified=0 (contract judged on 328, verified on 328 of 350 common identities)
face: domain_moves=3 [('federal_reserve', 'CentralBankHoldings'), ('federal_reserve', 'InflationExpectations'), ('federal_reserve', 'TotalFactorProductivity')]
face: identity_renames_records=3
face: moves_outside_rename_partition=contract=0 domain=0
face: arithmetic_balanced=True
face: live_export_census=all_symbols=329 contract=325 non_base=324 non_contract=4 ['BlsCatalogPage', 'FredOutputType', 'FredSeniorLoanOfficerSurveyQuery', 'FredTransformUnits']
face: candidate_counts_before={'basis': "Export census re-derived from the live opendata.data.models.__all__ at iteration-2 head (see iter2_census_rebase); domain/identity counts remain the batch5 manual arithmetic from the pinned batch4 snapshot plus the candidate-approved single SLOOS provider/domain identity. The three export counts below supersede the batch5 count adjudication's 330/326/325, which stays hashed unchanged at 2a929fd8614af09e28aa2ae16fa781c7da49d7692dcd6a3b6c78f2118c0906de.", 'domain_specs': 338, 'fixed_bound': 318, 'fixed_identities': 351, 'fixed_unbound': 33, 'provider_model_legs': 351, 'all_symbols': 329, 'contract_model_classes_including_base': 325, 'non_base_contract_model_classes': 324, 'non_contract_exports': [{'name': 'BlsCatalogPage', 'kind': 'sequence_dataclass', 'module': 'opendata.data.models.period_series'}, {'name': 'FredOutputType', 'kind': 'literal', 'values': [1, 2, 3, 4]}, {'name': 'FredTransformUnits', 'kind': 'literal', 'values': ['lin', 'chg', 'ch1', 'pch', 'pc1', 'pca', 'cch', 'cca', 'log']}, {'name': 'FredSeniorLoanOfficerSurveyQuery', 'kind': 'query_model', 'module': 'opendata.data.models.fred_sloos'}], 'registered_sources_or_descriptors': 34}
face: documented_totals_before={'batch5_pin': 309, 'live': 342, 'moved_on_existing_identity': 29, 'carried_by_added_identity': 4}
face: documented_field_moves_before={'binding': 10, 'scenario_present': 29, 'contract': 0, 'verified': 0}
face: documented_moves_by_source_before={'cftc': 2, 'eia': 2, 'federal_reserve': 10, 'finviz': 2, 'fred': 2, 'imf': 8, 'intrinio': 2, 'sec': 1}
face: documented_binding_moves_before={'federal_reserve': 10}
face: documented_identity_add_remove_before=[['federal_reserve', 'CentralBankHoldings', 'federal_reserve_central_bank_holdings'], ['federal_reserve', 'InflationExpectations', 'federal_reserve_inflation_expectations'], ['federal_reserve', 'TotalFactorProductivity', 'federal_reserve_total_factor_productivity'], ['finviz', 'EquityScreener', 'finviz_equity_screener']]/[['federal_reserve', 'CentralBankHoldings', 'central_bank_holdings'], ['federal_reserve', 'InflationExpectations', 'inflation_expectations'], ['federal_reserve', 'TotalFactorProductivity', 'total_factor_productivity']]
face: documented_semantic_field_moves_before={'contract': 0, 'verified': 0, 'rule': 'both must stay 0; a nonzero value means iteration 2 changed a reviewed output contract'}
face: documented_derived_from=opendata.data.providers.catalog.list_providers() bindings + load_domains() semantics_declared + alert_matrix.registered_legs()
face: documented_re_pinned_sources={'opendata/data/domains.yaml': {'test_constant_batch5_domain_source_sha256': '59e3888662d4ccc575db2421d38f76e34592ca9362245db6d14ab35f2ec8aee5', 'live': '044f3d6493542f23b17c37ec97a6de61f89357f81bff66bc48d264e3f92b32ca', 'why': "the fixture's own pin already carried the iteration-2 digest; only the duplicated test-side constant had drifted, so the constant is re-based to the pin rather than the registry being re-derived"}, 'opendata/data/models/__init__.py': {'pinned': '9dac556073190eff37c045b0c3a1880709840453ee12919534032f3428bb2509', 'live': '9cd3fb1387fc6676716bc0a9cc2309180bc41015c10821f89457f1195e437cca', 'why': 'the two look-alike exports above were removed from the import block and __all__'}, 'opendata/data/providers/fred/provider.py': {'pinned': '1407bfb6a930e8366346273e54bed652c71ce541e9f3fcee7ac52e4febe6a4fb', 'live': 'fe76cc1a1f4d4b65e9a2997023a91df132d1aa1a8b614f54550334ca0493daea', 'why': 'iteration-2 backfill added a 消费用途 scenario string to the RetailPrices and SeniorLoanOfficerSurvey fetcher bindings; no identity, domain or contract field moved'}}
face: frozen_and_unchanged=['batch5_count_adjudication (still hashes to 2a929fd8614af09e28aa2ae16fa781c7da49d7692dcd6a3b6c78f2118c0906de on disk)', 'batch4_baseline', 'approved_delta', 'counts', 'domain_ids and every membership list', 'provider_model_identities', 'candidate_sha256']
face: fixture_row_flips_needed=0
verdict: REFUSED - the fixture rows already match live, nothing to re-base
```

## runs/scenario-rebase-apply.txt

```text
source_sha256_16=0c8f86309e42fe0b
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/scenario_rebase.py --apply
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
scenario_rebase_apply_exit=not_captured_at_run_time (verdict read from the body)
face: iter2_census_rebase_keys=['attribution', 'batch5_adjudication_counts', 'counterfact', 'delta_vs_adjudication', 'frozen_and_unchanged', 'live_counts', 'measured_by', 're_pinned_sources', 'registry_census_rebase', 'scope']
face: registry_census_rebase_keys=['approved_delta_conflict', 'before', 'binding_moves_by_source', 'counterfact', 'counts', 'derived_from', 'descriptor_partition', 'field_added_by_iter2_census_rebase', 'field_move_counts', 'field_shapes_moved', 'identities_added', 'identities_removed', 'identity_fields_moved', 'identity_renames', 'pin_chain', 'scenario_moves_by_source', 'scenario_present_total', 'semantic_field_moves', 'semantic_fields_untouched']
face: recorded_pin_sha=7ce0ca613405e10dc3616e2cd1e6964b957331eb068a20ce49d997927b49c9f8
face: candidate_revisions=1
face: recovered_pin_rev=360c61dd0e85ca82c90720400d4f3d6b4deb1f68
face: pin_rows=350 pin_scenario_present=309
face: fixture_rows=351 fixture_scenario_present=342
face: live_bindings=351 unresolvable=22
face: unresolvable_examples=['ecb/cpi:LookupError', 'ecb/gdp:LookupError', 'ecb/rate:LookupError', 'fred/cpi:LookupError', 'fred/gdp:LookupError']
face: domain_spec_missing=0 []
face: live_rows=351 live_scenario_present_total=351
face: identities_added=1 identities_removed=0 net=1
face: added_examples=[('finviz', 'EquityScreener')]
face: scenario_moves_on_existing=41 by_source={'cftc': 2, 'eia': 2, 'federal_reserve': 13, 'finviz': 2, 'fred': 2, 'imf': 8, 'intrinio': 2, 'oecd': 9, 'sec': 1}
face: carried_by_added_identity=1
face: binding_moves=13 by_source={'federal_reserve': 13}
face: semantic_moves=contract=3 verified=0 (contract judged on 328, verified on 328 of 350 common identities)
face: domain_moves=3 [('federal_reserve', 'CentralBankHoldings'), ('federal_reserve', 'InflationExpectations'), ('federal_reserve', 'TotalFactorProductivity')]
face: identity_renames_records=3
face: moves_outside_rename_partition=contract=0 domain=0
face: arithmetic_balanced=True
face: live_export_census=all_symbols=329 contract=325 non_base=324 non_contract=4 ['BlsCatalogPage', 'FredOutputType', 'FredSeniorLoanOfficerSurveyQuery', 'FredTransformUnits']
face: candidate_counts_before={'basis': "Export census re-derived from the live opendata.data.models.__all__ at iteration-2 head (see iter2_census_rebase); domain/identity counts remain the batch5 manual arithmetic from the pinned batch4 snapshot plus the candidate-approved single SLOOS provider/domain identity. The three export counts below supersede the batch5 count adjudication's 330/326/325, which stays hashed unchanged at 2a929fd8614af09e28aa2ae16fa781c7da49d7692dcd6a3b6c78f2118c0906de.", 'domain_specs': 338, 'fixed_bound': 318, 'fixed_identities': 351, 'fixed_unbound': 33, 'provider_model_legs': 351, 'all_symbols': 329, 'contract_model_classes_including_base': 325, 'non_base_contract_model_classes': 324, 'non_contract_exports': [{'name': 'BlsCatalogPage', 'kind': 'sequence_dataclass', 'module': 'opendata.data.models.period_series'}, {'name': 'FredOutputType', 'kind': 'literal', 'values': [1, 2, 3, 4]}, {'name': 'FredTransformUnits', 'kind': 'literal', 'values': ['lin', 'chg', 'ch1', 'pch', 'pc1', 'pca', 'cch', 'cca', 'log']}, {'name': 'FredSeniorLoanOfficerSurveyQuery', 'kind': 'query_model', 'module': 'opendata.data.models.fred_sloos'}], 'registered_sources_or_descriptors': 34}
face: documented_totals_before={'batch5_pin': 309, 'live': 342, 'moved_on_existing_identity': 29, 'carried_by_added_identity': 4}
face: documented_field_moves_before={'binding': 10, 'scenario_present': 29, 'contract': 0, 'verified': 0}
face: documented_moves_by_source_before={'cftc': 2, 'eia': 2, 'federal_reserve': 10, 'finviz': 2, 'fred': 2, 'imf': 8, 'intrinio': 2, 'sec': 1}
face: documented_binding_moves_before={'federal_reserve': 10}
face: documented_identity_add_remove_before=[['federal_reserve', 'CentralBankHoldings', 'federal_reserve_central_bank_holdings'], ['federal_reserve', 'InflationExpectations', 'federal_reserve_inflation_expectations'], ['federal_reserve', 'TotalFactorProductivity', 'federal_reserve_total_factor_productivity'], ['finviz', 'EquityScreener', 'finviz_equity_screener']]/[['federal_reserve', 'CentralBankHoldings', 'central_bank_holdings'], ['federal_reserve', 'InflationExpectations', 'inflation_expectations'], ['federal_reserve', 'TotalFactorProductivity', 'total_factor_productivity']]
face: documented_semantic_field_moves_before={'contract': 0, 'verified': 0, 'rule': 'both must stay 0; a nonzero value means iteration 2 changed a reviewed output contract'}
face: documented_derived_from=opendata.data.providers.catalog.list_providers() bindings + load_domains() semantics_declared + alert_matrix.registered_legs()
face: documented_re_pinned_sources={'opendata/data/domains.yaml': {'test_constant_batch5_domain_source_sha256': '59e3888662d4ccc575db2421d38f76e34592ca9362245db6d14ab35f2ec8aee5', 'live': '044f3d6493542f23b17c37ec97a6de61f89357f81bff66bc48d264e3f92b32ca', 'why': "the fixture's own pin already carried the iteration-2 digest; only the duplicated test-side constant had drifted, so the constant is re-based to the pin rather than the registry being re-derived"}, 'opendata/data/models/__init__.py': {'pinned': '9dac556073190eff37c045b0c3a1880709840453ee12919534032f3428bb2509', 'live': '9cd3fb1387fc6676716bc0a9cc2309180bc41015c10821f89457f1195e437cca', 'why': 'the two look-alike exports above were removed from the import block and __all__'}, 'opendata/data/providers/fred/provider.py': {'pinned': '1407bfb6a930e8366346273e54bed652c71ce541e9f3fcee7ac52e4febe6a4fb', 'live': 'fe76cc1a1f4d4b65e9a2997023a91df132d1aa1a8b614f54550334ca0493daea', 'why': 'iteration-2 backfill added a 消费用途 scenario string to the RetailPrices and SeniorLoanOfficerSurvey fetcher bindings; no identity, domain or contract field moved'}}
face: frozen_and_unchanged=['batch5_count_adjudication (still hashes to 2a929fd8614af09e28aa2ae16fa781c7da49d7692dcd6a3b6c78f2118c0906de on disk)', 'batch4_baseline', 'approved_delta', 'counts', 'domain_ids and every membership list', 'provider_model_identities', 'candidate_sha256']
face: fixture_row_flips_needed=9
face: flip oecd/CompositeLeadingIndicator False -> True
face: flip oecd/ConsumerPriceIndex False -> True
face: flip oecd/CountryInterestRates False -> True
face: flip oecd/GdpForecast False -> True
face: flip oecd/GdpNominal False -> True
face: flip oecd/GdpReal False -> True
face: flip oecd/HousePriceIndex False -> True
face: flip oecd/SharePriceIndex False -> True
face: flip oecd/Unemployment False -> True
face: export_counts_plan={'all_symbols': {'candidate_counts': 329, 'live_counts': 329, 'batch4_baseline': 328, 'live_measured': 329}, 'contract_model_classes_including_base': {'candidate_counts': 325, 'live_counts': 325, 'batch4_baseline': 325, 'live_measured': 325}, 'non_base_contract_model_classes': {'candidate_counts': 324, 'live_counts': 324, 'batch4_baseline': 324, 'live_measured': 324}, 'non_contract_exports': {'candidate_record_count': 4, 'candidate_record_names': ['BlsCatalogPage', 'FredOutputType', 'FredSeniorLoanOfficerSurveyQuery', 'FredTransformUnits'], 'batch4_baseline_names': ['BlsCatalogPage', 'FredOutputType', 'FredTransformUnits'], 'live_measured_count': 4, 'live_measured_names': ['BlsCatalogPage', 'FredOutputType', 'FredSeniorLoanOfficerSurveyQuery', 'FredTransformUnits']}}
face: live_counts_all_keys=['all_symbols', 'contract_model_classes_including_base', 'non_base_contract_model_classes']
face: candidate_counts_all_keys=['all_symbols', 'basis', 'contract_model_classes_including_base', 'domain_specs', 'fixed_bound', 'fixed_identities', 'fixed_unbound', 'non_base_contract_model_classes', 'non_contract_exports', 'provider_model_legs', 'registered_sources_or_descriptors']
face: staged_scenario_total=351 live_scenario_total=351
face: serializer_roundtrip_identical=True
face: rows_total_after=351
face: historical_census_keys=10 unchanged=True
face: frozen[candidate_sha256]=unchanged=True
face: frozen[approved_delta]=unchanged=True
face: frozen[source_hashes]=unchanged=True
face: frozen[counts]=unchanged=True
face: frozen[candidate_counts]=unchanged=True
face: frozen[batch4_baseline]=unchanged=True
face: frozen[domain_ids]=unchanged=True
face: frozen[historical_census]=unchanged=True
face: payload_sha_before=a8715f16248ab2ce4d3e9e27200fc17b07fa2d888a2f45fc56d2e6f8e8e041a6
face: payload_sha_after=786fb181884ea564c80c1883601d6aaaa3f1d56ba6758ce45727a68efbaa8d88
face: applied_written_sha_matches=True
face: applied_bytes_delta=3381
verdict: APPLIED - 9 scenario_present rows re-based from live bindings, every export count left byte-identical after being checked against live, and a derived c103_scenario_rebase block appended
```

## runs/scenario-rebase-dry-1.txt

```text
source_sha256_16=7fbca3bc838fe33a
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/scenario_rebase.py
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
scenario_rebase_dry_exit=not_captured_at_run_time (verdict read from the body)
face: iter2_census_rebase_keys=['attribution', 'batch5_adjudication_counts', 'counterfact', 'delta_vs_adjudication', 'frozen_and_unchanged', 'live_counts', 'measured_by', 're_pinned_sources', 'registry_census_rebase', 'scope']
face: registry_census_rebase_keys=['approved_delta_conflict', 'before', 'binding_moves_by_source', 'counterfact', 'counts', 'derived_from', 'descriptor_partition', 'field_added_by_iter2_census_rebase', 'field_move_counts', 'field_shapes_moved', 'identities_added', 'identities_removed', 'identity_fields_moved', 'identity_renames', 'pin_chain', 'scenario_moves_by_source', 'scenario_present_total', 'semantic_field_moves', 'semantic_fields_untouched']
face: recorded_pin_sha=7ce0ca613405e10dc3616e2cd1e6964b957331eb068a20ce49d997927b49c9f8
face: candidate_revisions=1
face: recovered_pin_rev=360c61dd0e85ca82c90720400d4f3d6b4deb1f68
face: pin_rows=350 pin_scenario_present=309
face: fixture_rows=351 fixture_scenario_present=342
face: live_bindings=351 unresolvable=22
face: unresolvable_examples=['ecb/cpi:LookupError', 'ecb/gdp:LookupError', 'ecb/rate:LookupError', 'fred/cpi:LookupError', 'fred/gdp:LookupError']
face: domain_spec_missing=0 []
face: live_rows=351 live_scenario_present_total=351
face: identities_added=1 identities_removed=0 net=1
face: added_examples=[('finviz', 'EquityScreener')]
face: scenario_moves_on_existing=41 by_source={'cftc': 2, 'eia': 2, 'federal_reserve': 13, 'finviz': 2, 'fred': 2, 'imf': 8, 'intrinio': 2, 'oecd': 9, 'sec': 1}
face: carried_by_added_identity=1
face: binding_moves=13 by_source={'federal_reserve': 13}
face: semantic_moves=contract=3 verified=0 (contract judged on 328, verified on 328 of 350 common identities)
face: domain_moves=3 [('federal_reserve', 'CentralBankHoldings'), ('federal_reserve', 'InflationExpectations'), ('federal_reserve', 'TotalFactorProductivity')]
face: arithmetic_balanced=True
face: documented_totals_before={'batch5_pin': 309, 'live': 342, 'moved_on_existing_identity': 29, 'carried_by_added_identity': 4}
face: documented_field_moves_before={'binding': 10, 'scenario_present': 29, 'contract': 0, 'verified': 0}
face: documented_moves_by_source_before={'cftc': 2, 'eia': 2, 'federal_reserve': 10, 'finviz': 2, 'fred': 2, 'imf': 8, 'intrinio': 2, 'sec': 1}
face: documented_binding_moves_before={'federal_reserve': 10}
face: documented_identity_add_remove_before=[['federal_reserve', 'CentralBankHoldings', 'federal_reserve_central_bank_holdings'], ['federal_reserve', 'InflationExpectations', 'federal_reserve_inflation_expectations'], ['federal_reserve', 'TotalFactorProductivity', 'federal_reserve_total_factor_productivity'], ['finviz', 'EquityScreener', 'finviz_equity_screener']]/[['federal_reserve', 'CentralBankHoldings', 'central_bank_holdings'], ['federal_reserve', 'InflationExpectations', 'inflation_expectations'], ['federal_reserve', 'TotalFactorProductivity', 'total_factor_productivity']]
face: documented_semantic_field_moves_before={'contract': 0, 'verified': 0, 'rule': 'both must stay 0; a nonzero value means iteration 2 changed a reviewed output contract'}
face: documented_derived_from=opendata.data.providers.catalog.list_providers() bindings + load_domains() semantics_declared + alert_matrix.registered_legs()
face: documented_re_pinned_sources={'opendata/data/domains.yaml': {'test_constant_batch5_domain_source_sha256': '59e3888662d4ccc575db2421d38f76e34592ca9362245db6d14ab35f2ec8aee5', 'live': '044f3d6493542f23b17c37ec97a6de61f89357f81bff66bc48d264e3f92b32ca', 'why': "the fixture's own pin already carried the iteration-2 digest; only the duplicated test-side constant had drifted, so the constant is re-based to the pin rather than the registry being re-derived"}, 'opendata/data/models/__init__.py': {'pinned': '9dac556073190eff37c045b0c3a1880709840453ee12919534032f3428bb2509', 'live': '9cd3fb1387fc6676716bc0a9cc2309180bc41015c10821f89457f1195e437cca', 'why': 'the two look-alike exports above were removed from the import block and __all__'}, 'opendata/data/providers/fred/provider.py': {'pinned': '1407bfb6a930e8366346273e54bed652c71ce541e9f3fcee7ac52e4febe6a4fb', 'live': 'fe76cc1a1f4d4b65e9a2997023a91df132d1aa1a8b614f54550334ca0493daea', 'why': 'iteration-2 backfill added a 消费用途 scenario string to the RetailPrices and SeniorLoanOfficerSurvey fetcher bindings; no identity, domain or contract field moved'}}
face: frozen_and_unchanged=['batch5_count_adjudication (still hashes to 2a929fd8614af09e28aa2ae16fa781c7da49d7692dcd6a3b6c78f2118c0906de on disk)', 'batch4_baseline', 'approved_delta', 'counts', 'domain_ids and every membership list', 'provider_model_identities', 'candidate_sha256']
face: fixture_row_flips_needed=9
face: flip oecd/CompositeLeadingIndicator False -> True
face: flip oecd/ConsumerPriceIndex False -> True
face: flip oecd/CountryInterestRates False -> True
face: flip oecd/GdpForecast False -> True
face: flip oecd/GdpNominal False -> True
face: flip oecd/GdpReal False -> True
face: flip oecd/HousePriceIndex False -> True
face: flip oecd/SharePriceIndex False -> True
face: flip oecd/Unemployment False -> True
verdict: REFUSED - a contract/verified move is semantic, the block pins those at zero
face: semantic_move ('federal_reserve', 'CentralBankHoldings')
face: semantic_move ('federal_reserve', 'InflationExpectations')
face: semantic_move ('federal_reserve', 'TotalFactorProductivity')
```

## runs/scenario-rebase-dry-2.txt

```text
source_sha256_16=4c715c66594c46e1
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/scenario_rebase.py
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
scenario_rebase_dry2_exit=not_captured_at_run_time (verdict read from the body)
face: iter2_census_rebase_keys=['attribution', 'batch5_adjudication_counts', 'counterfact', 'delta_vs_adjudication', 'frozen_and_unchanged', 'live_counts', 'measured_by', 're_pinned_sources', 'registry_census_rebase', 'scope']
face: registry_census_rebase_keys=['approved_delta_conflict', 'before', 'binding_moves_by_source', 'counterfact', 'counts', 'derived_from', 'descriptor_partition', 'field_added_by_iter2_census_rebase', 'field_move_counts', 'field_shapes_moved', 'identities_added', 'identities_removed', 'identity_fields_moved', 'identity_renames', 'pin_chain', 'scenario_moves_by_source', 'scenario_present_total', 'semantic_field_moves', 'semantic_fields_untouched']
face: recorded_pin_sha=7ce0ca613405e10dc3616e2cd1e6964b957331eb068a20ce49d997927b49c9f8
face: candidate_revisions=1
face: recovered_pin_rev=360c61dd0e85ca82c90720400d4f3d6b4deb1f68
face: pin_rows=350 pin_scenario_present=309
face: fixture_rows=351 fixture_scenario_present=342
face: live_bindings=351 unresolvable=22
face: unresolvable_examples=['ecb/cpi:LookupError', 'ecb/gdp:LookupError', 'ecb/rate:LookupError', 'fred/cpi:LookupError', 'fred/gdp:LookupError']
face: domain_spec_missing=0 []
face: live_rows=351 live_scenario_present_total=351
face: identities_added=1 identities_removed=0 net=1
face: added_examples=[('finviz', 'EquityScreener')]
face: scenario_moves_on_existing=41 by_source={'cftc': 2, 'eia': 2, 'federal_reserve': 13, 'finviz': 2, 'fred': 2, 'imf': 8, 'intrinio': 2, 'oecd': 9, 'sec': 1}
face: carried_by_added_identity=1
face: binding_moves=13 by_source={'federal_reserve': 13}
face: semantic_moves=contract=3 verified=0 (contract judged on 328, verified on 328 of 350 common identities)
face: domain_moves=3 [('federal_reserve', 'CentralBankHoldings'), ('federal_reserve', 'InflationExpectations'), ('federal_reserve', 'TotalFactorProductivity')]
face: identity_renames_records=3
face: moves_outside_rename_partition=contract=0 domain=0
face: arithmetic_balanced=True
face: live_export_census=all_symbols=329 contract=325 non_base=324 non_contract=4 ['BlsCatalogPage', 'FredOutputType', 'FredSeniorLoanOfficerSurveyQuery', 'FredTransformUnits']
face: candidate_counts_before={'basis': "Export census re-derived from the live opendata.data.models.__all__ at iteration-2 head (see iter2_census_rebase); domain/identity counts remain the batch5 manual arithmetic from the pinned batch4 snapshot plus the candidate-approved single SLOOS provider/domain identity. The three export counts below supersede the batch5 count adjudication's 330/326/325, which stays hashed unchanged at 2a929fd8614af09e28aa2ae16fa781c7da49d7692dcd6a3b6c78f2118c0906de.", 'domain_specs': 338, 'fixed_bound': 318, 'fixed_identities': 351, 'fixed_unbound': 33, 'provider_model_legs': 351, 'all_symbols': 329, 'contract_model_classes_including_base': 325, 'non_base_contract_model_classes': 324, 'non_contract_exports': [{'name': 'BlsCatalogPage', 'kind': 'sequence_dataclass', 'module': 'opendata.data.models.period_series'}, {'name': 'FredOutputType', 'kind': 'literal', 'values': [1, 2, 3, 4]}, {'name': 'FredTransformUnits', 'kind': 'literal', 'values': ['lin', 'chg', 'ch1', 'pch', 'pc1', 'pca', 'cch', 'cca', 'log']}, {'name': 'FredSeniorLoanOfficerSurveyQuery', 'kind': 'query_model', 'module': 'opendata.data.models.fred_sloos'}], 'registered_sources_or_descriptors': 34}
face: documented_totals_before={'batch5_pin': 309, 'live': 342, 'moved_on_existing_identity': 29, 'carried_by_added_identity': 4}
face: documented_field_moves_before={'binding': 10, 'scenario_present': 29, 'contract': 0, 'verified': 0}
face: documented_moves_by_source_before={'cftc': 2, 'eia': 2, 'federal_reserve': 10, 'finviz': 2, 'fred': 2, 'imf': 8, 'intrinio': 2, 'sec': 1}
face: documented_binding_moves_before={'federal_reserve': 10}
face: documented_identity_add_remove_before=[['federal_reserve', 'CentralBankHoldings', 'federal_reserve_central_bank_holdings'], ['federal_reserve', 'InflationExpectations', 'federal_reserve_inflation_expectations'], ['federal_reserve', 'TotalFactorProductivity', 'federal_reserve_total_factor_productivity'], ['finviz', 'EquityScreener', 'finviz_equity_screener']]/[['federal_reserve', 'CentralBankHoldings', 'central_bank_holdings'], ['federal_reserve', 'InflationExpectations', 'inflation_expectations'], ['federal_reserve', 'TotalFactorProductivity', 'total_factor_productivity']]
face: documented_semantic_field_moves_before={'contract': 0, 'verified': 0, 'rule': 'both must stay 0; a nonzero value means iteration 2 changed a reviewed output contract'}
face: documented_derived_from=opendata.data.providers.catalog.list_providers() bindings + load_domains() semantics_declared + alert_matrix.registered_legs()
face: documented_re_pinned_sources={'opendata/data/domains.yaml': {'test_constant_batch5_domain_source_sha256': '59e3888662d4ccc575db2421d38f76e34592ca9362245db6d14ab35f2ec8aee5', 'live': '044f3d6493542f23b17c37ec97a6de61f89357f81bff66bc48d264e3f92b32ca', 'why': "the fixture's own pin already carried the iteration-2 digest; only the duplicated test-side constant had drifted, so the constant is re-based to the pin rather than the registry being re-derived"}, 'opendata/data/models/__init__.py': {'pinned': '9dac556073190eff37c045b0c3a1880709840453ee12919534032f3428bb2509', 'live': '9cd3fb1387fc6676716bc0a9cc2309180bc41015c10821f89457f1195e437cca', 'why': 'the two look-alike exports above were removed from the import block and __all__'}, 'opendata/data/providers/fred/provider.py': {'pinned': '1407bfb6a930e8366346273e54bed652c71ce541e9f3fcee7ac52e4febe6a4fb', 'live': 'fe76cc1a1f4d4b65e9a2997023a91df132d1aa1a8b614f54550334ca0493daea', 'why': 'iteration-2 backfill added a 消费用途 scenario string to the RetailPrices and SeniorLoanOfficerSurvey fetcher bindings; no identity, domain or contract field moved'}}
face: frozen_and_unchanged=['batch5_count_adjudication (still hashes to 2a929fd8614af09e28aa2ae16fa781c7da49d7692dcd6a3b6c78f2118c0906de on disk)', 'batch4_baseline', 'approved_delta', 'counts', 'domain_ids and every membership list', 'provider_model_identities', 'candidate_sha256']
face: fixture_row_flips_needed=9
face: flip oecd/CompositeLeadingIndicator False -> True
face: flip oecd/ConsumerPriceIndex False -> True
face: flip oecd/CountryInterestRates False -> True
face: flip oecd/GdpForecast False -> True
face: flip oecd/GdpNominal False -> True
face: flip oecd/GdpReal False -> True
face: flip oecd/HousePriceIndex False -> True
face: flip oecd/SharePriceIndex False -> True
face: flip oecd/Unemployment False -> True
face: export_counts_plan={'all_symbols': {'candidate_counts': 329, 'live_counts': 329, 'batch4_baseline': 328, 'live_measured': 329}, 'contract_model_classes_including_base': {'candidate_counts': 325, 'live_counts': 325, 'batch4_baseline': 325, 'live_measured': 325}, 'non_base_contract_model_classes': {'candidate_counts': 324, 'live_counts': 324, 'batch4_baseline': 324, 'live_measured': 324}, 'non_contract_exports': {'candidate_counts': [{'name': 'BlsCatalogPage', 'kind': 'sequence_dataclass', 'module': 'opendata.data.models.period_series'}, {'name': 'FredOutputType', 'kind': 'literal', 'values': [1, 2, 3, 4]}, {'name': 'FredTransformUnits', 'kind': 'literal', 'values': ['lin', 'chg', 'ch1', 'pch', 'pc1', 'pca', 'cch', 'cca', 'log']}, {'name': 'FredSeniorLoanOfficerSurveyQuery', 'kind': 'query_model', 'module': 'opendata.data.models.fred_sloos'}], 'live_counts': None, 'batch4_baseline': ['BlsCatalogPage', 'FredOutputType', 'FredTransformUnits'], 'live_measured': 4}}
```

## runs/scenario-rebase-dry-3-shape-gate-refusal.txt

```text
source_sha256_16=77079b9143094349
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/scenario_rebase.py
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
scenario_rebase_dry3_exit=not_captured_at_run_time (this is the instrument's own wrong-shape comparator being caught: live_counts.non_contract_exports=None while live measures 4)
face: iter2_census_rebase_keys=['attribution', 'batch5_adjudication_counts', 'counterfact', 'delta_vs_adjudication', 'frozen_and_unchanged', 'live_counts', 'measured_by', 're_pinned_sources', 'registry_census_rebase', 'scope']
face: registry_census_rebase_keys=['approved_delta_conflict', 'before', 'binding_moves_by_source', 'counterfact', 'counts', 'derived_from', 'descriptor_partition', 'field_added_by_iter2_census_rebase', 'field_move_counts', 'field_shapes_moved', 'identities_added', 'identities_removed', 'identity_fields_moved', 'identity_renames', 'pin_chain', 'scenario_moves_by_source', 'scenario_present_total', 'semantic_field_moves', 'semantic_fields_untouched']
face: recorded_pin_sha=7ce0ca613405e10dc3616e2cd1e6964b957331eb068a20ce49d997927b49c9f8
face: candidate_revisions=1
face: recovered_pin_rev=360c61dd0e85ca82c90720400d4f3d6b4deb1f68
face: pin_rows=350 pin_scenario_present=309
face: fixture_rows=351 fixture_scenario_present=342
face: live_bindings=351 unresolvable=22
face: unresolvable_examples=['ecb/cpi:LookupError', 'ecb/gdp:LookupError', 'ecb/rate:LookupError', 'fred/cpi:LookupError', 'fred/gdp:LookupError']
face: domain_spec_missing=0 []
face: live_rows=351 live_scenario_present_total=351
face: identities_added=1 identities_removed=0 net=1
face: added_examples=[('finviz', 'EquityScreener')]
face: scenario_moves_on_existing=41 by_source={'cftc': 2, 'eia': 2, 'federal_reserve': 13, 'finviz': 2, 'fred': 2, 'imf': 8, 'intrinio': 2, 'oecd': 9, 'sec': 1}
face: carried_by_added_identity=1
face: binding_moves=13 by_source={'federal_reserve': 13}
face: semantic_moves=contract=3 verified=0 (contract judged on 328, verified on 328 of 350 common identities)
face: domain_moves=3 [('federal_reserve', 'CentralBankHoldings'), ('federal_reserve', 'InflationExpectations'), ('federal_reserve', 'TotalFactorProductivity')]
face: identity_renames_records=3
face: moves_outside_rename_partition=contract=0 domain=0
face: arithmetic_balanced=True
face: live_export_census=all_symbols=329 contract=325 non_base=324 non_contract=4 ['BlsCatalogPage', 'FredOutputType', 'FredSeniorLoanOfficerSurveyQuery', 'FredTransformUnits']
face: candidate_counts_before={'basis': "Export census re-derived from the live opendata.data.models.__all__ at iteration-2 head (see iter2_census_rebase); domain/identity counts remain the batch5 manual arithmetic from the pinned batch4 snapshot plus the candidate-approved single SLOOS provider/domain identity. The three export counts below supersede the batch5 count adjudication's 330/326/325, which stays hashed unchanged at 2a929fd8614af09e28aa2ae16fa781c7da49d7692dcd6a3b6c78f2118c0906de.", 'domain_specs': 338, 'fixed_bound': 318, 'fixed_identities': 351, 'fixed_unbound': 33, 'provider_model_legs': 351, 'all_symbols': 329, 'contract_model_classes_including_base': 325, 'non_base_contract_model_classes': 324, 'non_contract_exports': [{'name': 'BlsCatalogPage', 'kind': 'sequence_dataclass', 'module': 'opendata.data.models.period_series'}, {'name': 'FredOutputType', 'kind': 'literal', 'values': [1, 2, 3, 4]}, {'name': 'FredTransformUnits', 'kind': 'literal', 'values': ['lin', 'chg', 'ch1', 'pch', 'pc1', 'pca', 'cch', 'cca', 'log']}, {'name': 'FredSeniorLoanOfficerSurveyQuery', 'kind': 'query_model', 'module': 'opendata.data.models.fred_sloos'}], 'registered_sources_or_descriptors': 34}
face: documented_totals_before={'batch5_pin': 309, 'live': 342, 'moved_on_existing_identity': 29, 'carried_by_added_identity': 4}
face: documented_field_moves_before={'binding': 10, 'scenario_present': 29, 'contract': 0, 'verified': 0}
face: documented_moves_by_source_before={'cftc': 2, 'eia': 2, 'federal_reserve': 10, 'finviz': 2, 'fred': 2, 'imf': 8, 'intrinio': 2, 'sec': 1}
face: documented_binding_moves_before={'federal_reserve': 10}
face: documented_identity_add_remove_before=[['federal_reserve', 'CentralBankHoldings', 'federal_reserve_central_bank_holdings'], ['federal_reserve', 'InflationExpectations', 'federal_reserve_inflation_expectations'], ['federal_reserve', 'TotalFactorProductivity', 'federal_reserve_total_factor_productivity'], ['finviz', 'EquityScreener', 'finviz_equity_screener']]/[['federal_reserve', 'CentralBankHoldings', 'central_bank_holdings'], ['federal_reserve', 'InflationExpectations', 'inflation_expectations'], ['federal_reserve', 'TotalFactorProductivity', 'total_factor_productivity']]
face: documented_semantic_field_moves_before={'contract': 0, 'verified': 0, 'rule': 'both must stay 0; a nonzero value means iteration 2 changed a reviewed output contract'}
face: documented_derived_from=opendata.data.providers.catalog.list_providers() bindings + load_domains() semantics_declared + alert_matrix.registered_legs()
face: documented_re_pinned_sources={'opendata/data/domains.yaml': {'test_constant_batch5_domain_source_sha256': '59e3888662d4ccc575db2421d38f76e34592ca9362245db6d14ab35f2ec8aee5', 'live': '044f3d6493542f23b17c37ec97a6de61f89357f81bff66bc48d264e3f92b32ca', 'why': "the fixture's own pin already carried the iteration-2 digest; only the duplicated test-side constant had drifted, so the constant is re-based to the pin rather than the registry being re-derived"}, 'opendata/data/models/__init__.py': {'pinned': '9dac556073190eff37c045b0c3a1880709840453ee12919534032f3428bb2509', 'live': '9cd3fb1387fc6676716bc0a9cc2309180bc41015c10821f89457f1195e437cca', 'why': 'the two look-alike exports above were removed from the import block and __all__'}, 'opendata/data/providers/fred/provider.py': {'pinned': '1407bfb6a930e8366346273e54bed652c71ce541e9f3fcee7ac52e4febe6a4fb', 'live': 'fe76cc1a1f4d4b65e9a2997023a91df132d1aa1a8b614f54550334ca0493daea', 'why': 'iteration-2 backfill added a 消费用途 scenario string to the RetailPrices and SeniorLoanOfficerSurvey fetcher bindings; no identity, domain or contract field moved'}}
face: frozen_and_unchanged=['batch5_count_adjudication (still hashes to 2a929fd8614af09e28aa2ae16fa781c7da49d7692dcd6a3b6c78f2118c0906de on disk)', 'batch4_baseline', 'approved_delta', 'counts', 'domain_ids and every membership list', 'provider_model_identities', 'candidate_sha256']
face: fixture_row_flips_needed=9
face: flip oecd/CompositeLeadingIndicator False -> True
face: flip oecd/ConsumerPriceIndex False -> True
face: flip oecd/CountryInterestRates False -> True
face: flip oecd/GdpForecast False -> True
face: flip oecd/GdpNominal False -> True
face: flip oecd/GdpReal False -> True
face: flip oecd/HousePriceIndex False -> True
face: flip oecd/SharePriceIndex False -> True
face: flip oecd/Unemployment False -> True
face: export_counts_plan={'all_symbols': {'candidate_counts': 329, 'live_counts': 329, 'batch4_baseline': 328, 'live_measured': 329}, 'contract_model_classes_including_base': {'candidate_counts': 325, 'live_counts': 325, 'batch4_baseline': 325, 'live_measured': 325}, 'non_base_contract_model_classes': {'candidate_counts': 324, 'live_counts': 324, 'batch4_baseline': 324, 'live_measured': 324}, 'non_contract_exports': {'candidate_counts': [{'name': 'BlsCatalogPage', 'kind': 'sequence_dataclass', 'module': 'opendata.data.models.period_series'}, {'name': 'FredOutputType', 'kind': 'literal', 'values': [1, 2, 3, 4]}, {'name': 'FredTransformUnits', 'kind': 'literal', 'values': ['lin', 'chg', 'ch1', 'pch', 'pc1', 'pca', 'cch', 'cca', 'log']}, {'name': 'FredSeniorLoanOfficerSurveyQuery', 'kind': 'query_model', 'module': 'opendata.data.models.fred_sloos'}], 'live_counts': None, 'batch4_baseline': ['BlsCatalogPage', 'FredOutputType', 'FredTransformUnits'], 'live_measured': 4}}
face: fixture_export_keys=['all_symbols', 'contract_model_classes_including_base', 'non_base_contract_model_classes'] measured_keys=['all_symbols', 'contract_model_classes_including_base', 'non_base_contract_model_classes', 'non_contract_exports']
verdict: REFUSED - live_counts.non_contract_exports=None but live measures 4; a census re-base cannot carry an export count that disagrees with live
```

## runs/scenario-rebase-dry-4.txt

```text
source_sha256_16=f4a3c8169664257f
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/scenario_rebase.py
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
scenario_rebase_dry4_exit=not_captured_at_run_time (verdict read from the body)
face: iter2_census_rebase_keys=['attribution', 'batch5_adjudication_counts', 'counterfact', 'delta_vs_adjudication', 'frozen_and_unchanged', 'live_counts', 'measured_by', 're_pinned_sources', 'registry_census_rebase', 'scope']
face: registry_census_rebase_keys=['approved_delta_conflict', 'before', 'binding_moves_by_source', 'counterfact', 'counts', 'derived_from', 'descriptor_partition', 'field_added_by_iter2_census_rebase', 'field_move_counts', 'field_shapes_moved', 'identities_added', 'identities_removed', 'identity_fields_moved', 'identity_renames', 'pin_chain', 'scenario_moves_by_source', 'scenario_present_total', 'semantic_field_moves', 'semantic_fields_untouched']
face: recorded_pin_sha=7ce0ca613405e10dc3616e2cd1e6964b957331eb068a20ce49d997927b49c9f8
face: candidate_revisions=1
face: recovered_pin_rev=360c61dd0e85ca82c90720400d4f3d6b4deb1f68
face: pin_rows=350 pin_scenario_present=309
face: fixture_rows=351 fixture_scenario_present=342
face: live_bindings=351 unresolvable=22
face: unresolvable_examples=['ecb/cpi:LookupError', 'ecb/gdp:LookupError', 'ecb/rate:LookupError', 'fred/cpi:LookupError', 'fred/gdp:LookupError']
face: domain_spec_missing=0 []
face: live_rows=351 live_scenario_present_total=351
face: identities_added=1 identities_removed=0 net=1
face: added_examples=[('finviz', 'EquityScreener')]
face: scenario_moves_on_existing=41 by_source={'cftc': 2, 'eia': 2, 'federal_reserve': 13, 'finviz': 2, 'fred': 2, 'imf': 8, 'intrinio': 2, 'oecd': 9, 'sec': 1}
face: carried_by_added_identity=1
face: binding_moves=13 by_source={'federal_reserve': 13}
face: semantic_moves=contract=3 verified=0 (contract judged on 328, verified on 328 of 350 common identities)
face: domain_moves=3 [('federal_reserve', 'CentralBankHoldings'), ('federal_reserve', 'InflationExpectations'), ('federal_reserve', 'TotalFactorProductivity')]
face: identity_renames_records=3
face: moves_outside_rename_partition=contract=0 domain=0
face: arithmetic_balanced=True
face: live_export_census=all_symbols=329 contract=325 non_base=324 non_contract=4 ['BlsCatalogPage', 'FredOutputType', 'FredSeniorLoanOfficerSurveyQuery', 'FredTransformUnits']
face: candidate_counts_before={'basis': "Export census re-derived from the live opendata.data.models.__all__ at iteration-2 head (see iter2_census_rebase); domain/identity counts remain the batch5 manual arithmetic from the pinned batch4 snapshot plus the candidate-approved single SLOOS provider/domain identity. The three export counts below supersede the batch5 count adjudication's 330/326/325, which stays hashed unchanged at 2a929fd8614af09e28aa2ae16fa781c7da49d7692dcd6a3b6c78f2118c0906de.", 'domain_specs': 338, 'fixed_bound': 318, 'fixed_identities': 351, 'fixed_unbound': 33, 'provider_model_legs': 351, 'all_symbols': 329, 'contract_model_classes_including_base': 325, 'non_base_contract_model_classes': 324, 'non_contract_exports': [{'name': 'BlsCatalogPage', 'kind': 'sequence_dataclass', 'module': 'opendata.data.models.period_series'}, {'name': 'FredOutputType', 'kind': 'literal', 'values': [1, 2, 3, 4]}, {'name': 'FredTransformUnits', 'kind': 'literal', 'values': ['lin', 'chg', 'ch1', 'pch', 'pc1', 'pca', 'cch', 'cca', 'log']}, {'name': 'FredSeniorLoanOfficerSurveyQuery', 'kind': 'query_model', 'module': 'opendata.data.models.fred_sloos'}], 'registered_sources_or_descriptors': 34}
face: documented_totals_before={'batch5_pin': 309, 'live': 342, 'moved_on_existing_identity': 29, 'carried_by_added_identity': 4}
face: documented_field_moves_before={'binding': 10, 'scenario_present': 29, 'contract': 0, 'verified': 0}
face: documented_moves_by_source_before={'cftc': 2, 'eia': 2, 'federal_reserve': 10, 'finviz': 2, 'fred': 2, 'imf': 8, 'intrinio': 2, 'sec': 1}
face: documented_binding_moves_before={'federal_reserve': 10}
face: documented_identity_add_remove_before=[['federal_reserve', 'CentralBankHoldings', 'federal_reserve_central_bank_holdings'], ['federal_reserve', 'InflationExpectations', 'federal_reserve_inflation_expectations'], ['federal_reserve', 'TotalFactorProductivity', 'federal_reserve_total_factor_productivity'], ['finviz', 'EquityScreener', 'finviz_equity_screener']]/[['federal_reserve', 'CentralBankHoldings', 'central_bank_holdings'], ['federal_reserve', 'InflationExpectations', 'inflation_expectations'], ['federal_reserve', 'TotalFactorProductivity', 'total_factor_productivity']]
face: documented_semantic_field_moves_before={'contract': 0, 'verified': 0, 'rule': 'both must stay 0; a nonzero value means iteration 2 changed a reviewed output contract'}
face: documented_derived_from=opendata.data.providers.catalog.list_providers() bindings + load_domains() semantics_declared + alert_matrix.registered_legs()
face: documented_re_pinned_sources={'opendata/data/domains.yaml': {'test_constant_batch5_domain_source_sha256': '59e3888662d4ccc575db2421d38f76e34592ca9362245db6d14ab35f2ec8aee5', 'live': '044f3d6493542f23b17c37ec97a6de61f89357f81bff66bc48d264e3f92b32ca', 'why': "the fixture's own pin already carried the iteration-2 digest; only the duplicated test-side constant had drifted, so the constant is re-based to the pin rather than the registry being re-derived"}, 'opendata/data/models/__init__.py': {'pinned': '9dac556073190eff37c045b0c3a1880709840453ee12919534032f3428bb2509', 'live': '9cd3fb1387fc6676716bc0a9cc2309180bc41015c10821f89457f1195e437cca', 'why': 'the two look-alike exports above were removed from the import block and __all__'}, 'opendata/data/providers/fred/provider.py': {'pinned': '1407bfb6a930e8366346273e54bed652c71ce541e9f3fcee7ac52e4febe6a4fb', 'live': 'fe76cc1a1f4d4b65e9a2997023a91df132d1aa1a8b614f54550334ca0493daea', 'why': 'iteration-2 backfill added a 消费用途 scenario string to the RetailPrices and SeniorLoanOfficerSurvey fetcher bindings; no identity, domain or contract field moved'}}
face: frozen_and_unchanged=['batch5_count_adjudication (still hashes to 2a929fd8614af09e28aa2ae16fa781c7da49d7692dcd6a3b6c78f2118c0906de on disk)', 'batch4_baseline', 'approved_delta', 'counts', 'domain_ids and every membership list', 'provider_model_identities', 'candidate_sha256']
face: fixture_row_flips_needed=9
face: flip oecd/CompositeLeadingIndicator False -> True
face: flip oecd/ConsumerPriceIndex False -> True
face: flip oecd/CountryInterestRates False -> True
face: flip oecd/GdpForecast False -> True
face: flip oecd/GdpNominal False -> True
face: flip oecd/GdpReal False -> True
face: flip oecd/HousePriceIndex False -> True
face: flip oecd/SharePriceIndex False -> True
face: flip oecd/Unemployment False -> True
face: export_counts_plan={'all_symbols': {'candidate_counts': 329, 'live_counts': 329, 'batch4_baseline': 328, 'live_measured': 329}, 'contract_model_classes_including_base': {'candidate_counts': 325, 'live_counts': 325, 'batch4_baseline': 325, 'live_measured': 325}, 'non_base_contract_model_classes': {'candidate_counts': 324, 'live_counts': 324, 'batch4_baseline': 324, 'live_measured': 324}, 'non_contract_exports': {'candidate_record_count': 4, 'candidate_record_names': ['BlsCatalogPage', 'FredOutputType', 'FredSeniorLoanOfficerSurveyQuery', 'FredTransformUnits'], 'batch4_baseline_names': ['BlsCatalogPage', 'FredOutputType', 'FredTransformUnits'], 'live_measured_count': 4, 'live_measured_names': ['BlsCatalogPage', 'FredOutputType', 'FredSeniorLoanOfficerSurveyQuery', 'FredTransformUnits']}}
face: live_counts_all_keys=['all_symbols', 'contract_model_classes_including_base', 'non_base_contract_model_classes']
face: candidate_counts_all_keys=['all_symbols', 'basis', 'contract_model_classes_including_base', 'domain_specs', 'fixed_bound', 'fixed_identities', 'fixed_unbound', 'non_base_contract_model_classes', 'non_contract_exports', 'provider_model_legs', 'registered_sources_or_descriptors']
face: staged_scenario_total=351 live_scenario_total=351
face: serializer_roundtrip_identical=True
face: rows_total_after=351
face: historical_census_keys=10 unchanged=True
face: frozen[candidate_sha256]=unchanged=True
face: frozen[approved_delta]=unchanged=True
face: frozen[source_hashes]=unchanged=True
face: frozen[counts]=unchanged=True
face: frozen[candidate_counts]=unchanged=True
face: frozen[batch4_baseline]=unchanged=True
face: frozen[domain_ids]=unchanged=True
face: frozen[historical_census]=unchanged=True
face: payload_sha_before=a8715f16248ab2ce4d3e9e27200fc17b07fa2d888a2f45fc56d2e6f8e8e041a6
face: payload_sha_after=786fb181884ea564c80c1883601d6aaaa3f1d56ba6758ce45727a68efbaa8d88
verdict: DRY RUN - nothing written (pass --apply to write)
```

## runs/scenario-rebase-idempotence-arm.txt

```text
source_sha256_16=d7b7ce507e65cb80
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/batch5-scenario-census/probes/scenario_rebase.py   (dry, after the apply)
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
scenario_rebase_dry_exit=1 (re-captured this turn)
argv=python docs/evidence/C103/batch5-scenario-census/probes/scenario_rebase.py (dry)
face: iter2_census_rebase_keys=['attribution', 'batch5_adjudication_counts', 'counterfact', 'delta_vs_adjudication', 'frozen_and_unchanged', 'live_counts', 'measured_by', 're_pinned_sources', 'registry_census_rebase', 'scope']
face: registry_census_rebase_keys=['approved_delta_conflict', 'before', 'binding_moves_by_source', 'c103_scenario_rebase', 'counterfact', 'counts', 'derived_from', 'descriptor_partition', 'field_added_by_iter2_census_rebase', 'field_move_counts', 'field_shapes_moved', 'identities_added', 'identities_removed', 'identity_fields_moved', 'identity_renames', 'pin_chain', 'scenario_moves_by_source', 'scenario_present_total', 'semantic_field_moves', 'semantic_fields_untouched']
face: recorded_pin_sha=7ce0ca613405e10dc3616e2cd1e6964b957331eb068a20ce49d997927b49c9f8
face: candidate_revisions=1
face: recovered_pin_rev=360c61dd0e85ca82c90720400d4f3d6b4deb1f68
face: pin_rows=350 pin_scenario_present=309
face: fixture_rows=351 fixture_scenario_present=351
face: live_bindings=351 unresolvable=22
face: unresolvable_examples=['ecb/cpi:LookupError', 'ecb/gdp:LookupError', 'ecb/rate:LookupError', 'fred/cpi:LookupError', 'fred/gdp:LookupError']
face: domain_spec_missing=0 []
face: live_rows=351 live_scenario_present_total=351
face: identities_added=1 identities_removed=0 net=1
face: added_examples=[('finviz', 'EquityScreener')]
face: scenario_moves_on_existing=41 by_source={'cftc': 2, 'eia': 2, 'federal_reserve': 13, 'finviz': 2, 'fred': 2, 'imf': 8, 'intrinio': 2, 'oecd': 9, 'sec': 1}
face: carried_by_added_identity=1
face: binding_moves=13 by_source={'federal_reserve': 13}
face: semantic_moves=contract=3 verified=0 (contract judged on 328, verified on 328 of 350 common identities)
face: domain_moves=3 [('federal_reserve', 'CentralBankHoldings'), ('federal_reserve', 'InflationExpectations'), ('federal_reserve', 'TotalFactorProductivity')]
face: identity_renames_records=3
face: moves_outside_rename_partition=contract=0 domain=0
face: arithmetic_balanced=True
face: live_export_census=all_symbols=329 contract=325 non_base=324 non_contract=4 ['BlsCatalogPage', 'FredOutputType', 'FredSeniorLoanOfficerSurveyQuery', 'FredTransformUnits']
face: candidate_counts_before={'basis': "Export census re-derived from the live opendata.data.models.__all__ at iteration-2 head (see iter2_census_rebase); domain/identity counts remain the batch5 manual arithmetic from the pinned batch4 snapshot plus the candidate-approved single SLOOS provider/domain identity. The three export counts below supersede the batch5 count adjudication's 330/326/325, which stays hashed unchanged at 2a929fd8614af09e28aa2ae16fa781c7da49d7692dcd6a3b6c78f2118c0906de.", 'domain_specs': 338, 'fixed_bound': 318, 'fixed_identities': 351, 'fixed_unbound': 33, 'provider_model_legs': 351, 'all_symbols': 329, 'contract_model_classes_including_base': 325, 'non_base_contract_model_classes': 324, 'non_contract_exports': [{'name': 'BlsCatalogPage', 'kind': 'sequence_dataclass', 'module': 'opendata.data.models.period_series'}, {'name': 'FredOutputType', 'kind': 'literal', 'values': [1, 2, 3, 4]}, {'name': 'FredTransformUnits', 'kind': 'literal', 'values': ['lin', 'chg', 'ch1', 'pch', 'pc1', 'pca', 'cch', 'cca', 'log']}, {'name': 'FredSeniorLoanOfficerSurveyQuery', 'kind': 'query_model', 'module': 'opendata.data.models.fred_sloos'}], 'registered_sources_or_descriptors': 34}
face: documented_totals_before={'batch5_pin': 309, 'live': 342, 'moved_on_existing_identity': 29, 'carried_by_added_identity': 4}
face: documented_field_moves_before={'binding': 10, 'scenario_present': 29, 'contract': 0, 'verified': 0}
face: documented_moves_by_source_before={'cftc': 2, 'eia': 2, 'federal_reserve': 10, 'finviz': 2, 'fred': 2, 'imf': 8, 'intrinio': 2, 'sec': 1}
face: documented_binding_moves_before={'federal_reserve': 10}
face: documented_identity_add_remove_before=[['federal_reserve', 'CentralBankHoldings', 'federal_reserve_central_bank_holdings'], ['federal_reserve', 'InflationExpectations', 'federal_reserve_inflation_expectations'], ['federal_reserve', 'TotalFactorProductivity', 'federal_reserve_total_factor_productivity'], ['finviz', 'EquityScreener', 'finviz_equity_screener']]/[['federal_reserve', 'CentralBankHoldings', 'central_bank_holdings'], ['federal_reserve', 'InflationExpectations', 'inflation_expectations'], ['federal_reserve', 'TotalFactorProductivity', 'total_factor_productivity']]
face: documented_semantic_field_moves_before={'contract': 0, 'verified': 0, 'rule': 'both must stay 0; a nonzero value means iteration 2 changed a reviewed output contract'}
face: documented_derived_from=opendata.data.providers.catalog.list_providers() bindings + load_domains() semantics_declared + alert_matrix.registered_legs()
face: documented_re_pinned_sources={'opendata/data/domains.yaml': {'test_constant_batch5_domain_source_sha256': '59e3888662d4ccc575db2421d38f76e34592ca9362245db6d14ab35f2ec8aee5', 'live': '044f3d6493542f23b17c37ec97a6de61f89357f81bff66bc48d264e3f92b32ca', 'why': "the fixture's own pin already carried the iteration-2 digest; only the duplicated test-side constant had drifted, so the constant is re-based to the pin rather than the registry being re-derived"}, 'opendata/data/models/__init__.py': {'pinned': '9dac556073190eff37c045b0c3a1880709840453ee12919534032f3428bb2509', 'live': '9cd3fb1387fc6676716bc0a9cc2309180bc41015c10821f89457f1195e437cca', 'why': 'the two look-alike exports above were removed from the import block and __all__'}, 'opendata/data/providers/fred/provider.py': {'pinned': '1407bfb6a930e8366346273e54bed652c71ce541e9f3fcee7ac52e4febe6a4fb', 'live': 'fe76cc1a1f4d4b65e9a2997023a91df132d1aa1a8b614f54550334ca0493daea', 'why': 'iteration-2 backfill added a 消费用途 scenario string to the RetailPrices and SeniorLoanOfficerSurvey fetcher bindings; no identity, domain or contract field moved'}}
face: frozen_and_unchanged=['batch5_count_adjudication (still hashes to 2a929fd8614af09e28aa2ae16fa781c7da49d7692dcd6a3b6c78f2118c0906de on disk)', 'batch4_baseline', 'approved_delta', 'counts', 'domain_ids and every membership list', 'provider_model_identities', 'candidate_sha256']
face: fixture_row_flips_needed=0
verdict: REFUSED - the fixture rows already match live, nothing to re-base
scenario_rebase_dry_exit=1
```

## runs/whole-suite-after-39-faces-derived.txt

```text
source_sha256_16=5433cae6f07ad930
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
whole_suite_exit=1 (both readings were red; the after-reading carries its own SUITE_EXIT=1 line in the body)
face: failed_lines=39
face: collected_header=collected 26551 items
face: summary_line=========== 39 failed, 26238 passed, 274 skipped in 599.65s (0:09:59) ===========
face: exit_markers_in_body=
identity: failed + passed + skipped must equal the collected header
```

## runs/whole-suite-before-56-faces-derived.txt

```text
source_sha256_16=a7f36db6ea062175
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
whole_suite_exit=1 (both readings were red; the after-reading carries its own SUITE_EXIT=1 line in the body)
face: failed_lines=56
face: collected_header=collected 26551 items
face: summary_line=========== 56 failed, 26221 passed, 274 skipped in 607.51s (0:10:07) ===========
face: exit_markers_in_body=
identity: failed + passed + skipped must equal the collected header
```

