# C105 a2-probe-repair -- faces, projected from the carriers


Every line below is copied out of the named carrier, whose body is the verbatim stdout of
the run its ``argv=`` header records. Nothing on this page is typed from memory.


## a2-before-33-probes.txt

- carrier: a2-before-33-probes.txt
- round: C105 a2-probe-repair
- source_bytes=84864
- source_sha256_16=9ff57179b976f54b
- argv=python scripts/quality/a2_check.py --files <the 33 probe targets>  # the before picture
- before_a2_exit_exit=not_captured_at_run_time (verdict read from the body)
- "code": "69     REPORT.unlink()\n70 done = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled\n71     [  # nosec B404  # noqa: S607\n72         \"gitleaks\",\n73         \"detect\",\n74         \"--no-git\",\n75         \"--source\",\n76         str(PAYLOAD),\n77         \"--config\",\n78         str(REPO / \".gitleaks.toml\"),\n79         \"--redact=20\",\n80         \"-f\",\n81         \"json\",\n82         \"-r\",\n83         str(REPORT),\n84         \"--no-banner\",\n85     ],\n86     cwd=REPO,\n87     capture_output=True,\n88     text=True,\n89     shell=False,\n90     check=False,\n91 )\n92 print(f\"face: scan_exit={done.returncode}\")\n",
- before_a2_exit=1

## archiver-rerun.txt

- carrier: archiver-rerun.txt
- round: C105 a2-probe-repair
- source_bytes=1392
- source_sha256_16=2a6145e7b41cd0a1
- argv=python docs/evidence/C105/a2-probe-repair/probes/carrier_archiver.py  # the previous run
- archiver_exit=0
- face: receipts_declared=11 sources_missing=0 missing=[]
- face: carriers_written=11 bytes_written=109492
- face: carriers_on_disk=11
- face: header_repetitions=[]
- face: oversized_vs_source=[]
- face: carriers_without_an_exit_line=[]
- verdict: PASS

## c105-probes-own-a2.txt

- carrier: c105-probes-own-a2.txt
- round: C105 a2-probe-repair
- source_bytes=581
- source_sha256_16=f1d3f4c1a5185bf4
- argv=python /tmp/c105_own_planes.py  # the four planes over this round's own probes
- own_planes_exit=0
- face: own_probes=5
- face: ruff_check_exit=0
- face: ruff_format_exit=0
- face: mypy_targets=5 targets_with_errors=0 mypy_errors_total=0
- face: bandit_exit=0 bandit_results_total=0

## docs-reidentity.txt

- carrier: docs-reidentity.txt
- round: C105 a2-probe-repair
- source_bytes=2900
- source_sha256_16=f9e919e3cb6cc21a
- argv=python docs/evidence/C105/a2-probe-repair/probes/docs_reidentity.py
- reidentity_exit=0
- face: documents_in_scope=6 builder_lines=394 evidence_dirs_in_snapshot=4
- face: arm_head_exit=0 arm_live_exit=0 arm_prose_armed=True arm_claim_armed=True
- face: head_written=documents_written=6 live_written=documents_written=6
- identity: docs/evidence/C103/batch5-scenario-census/README.md bytes_same=False blocks_same=True tokens_same=True byte_delta=0 head_sha=650e7f30d76adcce live_sha=4dc495452d01962a
- identity: docs/evidence/C103/batch5-scenario-census/face.md bytes_same=True blocks_same=True tokens_same=True byte_delta=0 head_sha=5c80ef5b60dca863 live_sha=5c80ef5b60dca863
- identity: docs/evidence/C103/batch5-source-pins/README.md bytes_same=False blocks_same=True tokens_same=True byte_delta=-6 head_sha=69a243b23378e62f live_sha=625d266edc9394a2
- identity: docs/evidence/C103/batch5-source-pins/face.md bytes_same=True blocks_same=True tokens_same=True byte_delta=0 head_sha=360578c352d96080 live_sha=360578c352d96080
- identity: docs/evidence/C103/fmp-fetch-array-arity/README.md bytes_same=False blocks_same=True tokens_same=True byte_delta=2 head_sha=8d706636ccf7b421 live_sha=23f58de98359e4a1
- identity: docs/evidence/C103/fmp-fetch-array-arity/face.md bytes_same=True blocks_same=True tokens_same=True byte_delta=0 head_sha=30812b3d1416aa8e live_sha=30812b3d1416aa8e
- face: candidate_pool_prose=49 candidate_pool_claim=43
- face: control_prose_index=36 literal='/Users/yunjinqi/opt/anaconda3/envs/py313' into=docs/evidence/C103/fmp-fetch-array-arity/README.md blocks_moved=3 candidates_skipped=0
- face: control_claim_index=573 literal=' docs/evidence/C103/' into=docs/evidence/C103/fmp-fetch-array-arity/README.md tokens_moved=1 candidates_skipped=19
- face: control_arms_distinct=True levels_armed=2
- face: repo_restored=True stray_dirs_left=0
- verdict: PASS - bytes moved in 3 document(s) by re-wrapping only; every paragraph block and every claim token is intact, and each level is armed by its own clean builder run

## face-writer.txt

- carrier: face-writer.txt
- round: C105 a2-probe-repair
- source_bytes=219
- source_sha256_16=8bfe2bc88e0124e0
- argv=python docs/evidence/C105/a2-probe-repair/probes/face_writer.py
- face_writer_exit=0
- face: carriers=11 face_md_bytes=19707 face_md_sha256_16=4db5f04589f01a8e
- face: readme_claims=85 unresolved_tokens=0 readme_bytes=8664
- verdict: PASS - every README literal and number is a face.md line

## mypy-bandit-34.txt

- carrier: mypy-bandit-34.txt
- round: C105 a2-probe-repair
- source_bytes=149
- source_sha256_16=e288354b6fd8b551
- argv=python /tmp/c105_planes.py  # mypy per target with the gate argv, bandit as one batch
- planes_exit=0
- face: mypy_targets=34 targets_with_errors=0 mypy_errors_total=0
- face: bandit_exit=0 bandit_results_total=0 codes={} nosec_warnings=86

## rerun-and-refusals.txt

- carrier: rerun-and-refusals.txt
- round: C105 a2-probe-repair
- source_bytes=2795
- source_sha256_16=4349239c0d1367e2
- argv=python /tmp/c105_capture3.py  # bandit/mypy attribution, six probe re-runs, C104 carrier diff vs index then restore, three repair scripts re-run
- capture_exit=0
- face: bandit_exit=0 bandit_results_total=0
- face: nosec_warnings_total=86 warnings_parsed_to_file_line=86
- face: unused_nosec_on_nosec_lines=11 files=7
- face: unused_by_file=verify_census_rebase.py=2, stale_fake_repair.py=1, suite_red_taxonomy.py=1, ths_format_audit.py=2, ths_hunk_audit.py=2, pin_rebase.py=1, payload_rescan.py=2
- face: other_warnings_not_on_a_nosec_line=75
- face: mypy_targets=34 targets_with_errors=0 mypy_errors_total=0
- rerun: h15_allowlist.py probe_exit=0 :: verdict: PASS
- rerun: face_builder.py probe_exit=0 :: verdict: PASS
- rerun: payload_rescan.py probe_exit=0 :: verdict: PASS
- rerun: carrier_archiver.py probe_exit=0 :: verdict: PASS
- rerun: document_builder.py probe_exit=0 :: verdict: PASS - every README value is a carrier-extracted token
- rerun: scenario_rebase.py probe_exit=1 :: verdict: REFUSED - the fixture rows already match live, nothing to re-base
- carrier_diff: finding-triage-identity-only.txt changed_lines=2 fields=['captured_at_local', 'worktree_dirty_files_at_capture'] line_counts=(43,43)
- carrier_diff: probe-pass.txt changed_lines=2 fields=['captured_at_local', 'worktree_dirty_files_at_capture'] line_counts=(26,26)
- carrier_diff: pytest-allowlist-consumers.txt changed_lines=2 fields=['captured_at_local', 'worktree_dirty_files_at_capture'] line_counts=(27,27)
- carrier_diff: secret-check-after-edit.txt changed_lines=2 fields=['captured_at_local', 'worktree_dirty_files_at_capture'] line_counts=(25,25)
- carrier_diff: secret-check-before-edit.txt changed_lines=2 fields=['captured_at_local', 'worktree_dirty_files_at_capture'] line_counts=(25,25)
- carrier_diff: staged-payload-rescan.txt changed_lines=2 fields=['captured_at_local', 'worktree_dirty_files_at_capture'] line_counts=(22,22)
- face: carrier_changed_lines_total=12 carriers_in_scope=6
- refusal_arm: c105_misc.py MISC_exit=1
- face: edits_applied=0 files_touched=17 reverted=True
- verdict: REFUSED - an anchor was not unique or an edit broke a file, nothing written
- refusal_arm: c105_mypy.py MYPY_SWEEP_exit=1
- face: edits_applied=0 files_in_scope=14 reverted=True
- verdict: REFUSED - an anchor was not unique or an edit broke a file, nothing written
- refusal_arm: c105_bandit.py BANDIT_SWEEP_exit=0
- face: bandit_finding_sites=0 codes={}
- face: targets_hashed=34 changed_after_repair_reruns=0 names=[]

## ruff-check-34.txt

- carrier: ruff-check-34.txt
- round: C105 a2-probe-repair
- source_bytes=38
- source_sha256_16=67fb30d875c49033
- argv=python -m ruff check --output-format=concise <the 34 probe targets in targets.txt>
- ruff_check_exit=0

## ruff-format-34.txt

- carrier: ruff-format-34.txt
- round: C105 a2-probe-repair
- source_bytes=47
- source_sha256_16=aa11e2aa2d39f6bf
- argv=python -m ruff format --check <the 34 probe targets in targets.txt>
- ruff_format_exit=0

## witness-34.txt

- carrier: witness-34.txt
- round: C105 a2-probe-repair
- source_bytes=10319
- source_sha256_16=5791d9fe516c0a7c
- argv=python docs/evidence/C105/a2-probe-repair/probes/ast_witness.py <the 34 targets>
- witness_exit=1
- face: file=docs/evidence/C101/census-rebase/probes/falsify_census_arms.py status=values_intact top_level_delta=1 added_imports=1 pruned_imports=0 restructured_families=3 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C101/census-rebase/probes/falsify_census_arms.py restructured=For[3->3],ImportFrom[1->2],Try:block[1->1]
- face:   docs/evidence/C101/census-rebase/probes/falsify_census_arms.py added_import=Callable
- face: file=docs/evidence/C101/census-rebase/probes/patch_head_attribution.py status=identical top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=0 whitespace_pairs=0 value_moves=0
- face: file=docs/evidence/C101/census-rebase/probes/rebase_batch5_census.py status=values_intact top_level_delta=1 added_imports=0 pruned_imports=2 restructured_families=5 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C101/census-rebase/probes/rebase_batch5_census.py restructured=Assign:key[1->0],For[2->2],FunctionDef:key[0->1],Import[1->2],ImportFrom[6->4]
- face:   docs/evidence/C101/census-rebase/probes/rebase_batch5_census.py pruned_import=ProviderRegistry
- face:   docs/evidence/C101/census-rebase/probes/rebase_batch5_census.py pruned_import=register_providers
- face: file=docs/evidence/C101/census-rebase/probes/rebase_descriptor_census.py status=identical top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=0 whitespace_pairs=0 value_moves=0
- face: file=docs/evidence/C101/census-rebase/probes/verify_census_rebase.py status=values_intact top_level_delta=1 added_imports=1 pruned_imports=0 restructured_families=3 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C101/census-rebase/probes/verify_census_rebase.py restructured=Assign:turned[1->1],FunctionDef:judge[1->1],ImportFrom[6->7]
- face:   docs/evidence/C101/census-rebase/probes/verify_census_rebase.py added_import=Any
- face: file=docs/evidence/C101/probes/cast_guard_census.py status=identical top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=0 whitespace_pairs=0 value_moves=0
- face: file=docs/evidence/C101/probes/docstring_gap_census.py status=values_intact top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=1 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C101/probes/docstring_gap_census.py restructured=FunctionDef:missing_symbols[1->1]
- face: file=docs/evidence/C101/probes/ecb_seam_body_shapes.py status=identical top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=0 whitespace_pairs=0 value_moves=0
- face: file=docs/evidence/C101/probes/registry_scenario_census.py status=identical top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=0 whitespace_pairs=0 value_moves=0
- face: file=docs/evidence/C101/probes/repin_reference_policy.py status=identical top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=0 whitespace_pairs=0 value_moves=0
- face: file=docs/evidence/C101/probes/stale_fake_census.py status=values_intact top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=1 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C101/probes/stale_fake_census.py restructured=FunctionDef:_resolve_target[1->1]
- face: file=docs/evidence/C101/probes/stale_fake_repair.py status=values_intact top_level_delta=2 added_imports=1 pruned_imports=0 restructured_families=4 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C101/probes/stale_fake_repair.py restructured=ClassDef:_Staged[0->1],FunctionDef:_counters[0->1],FunctionDef:repair_files[1->1],ImportFrom[3->3]
- face:   docs/evidence/C101/probes/stale_fake_repair.py added_import=TypedDict
- face: file=docs/evidence/C101/probes/suite_red_taxonomy.py status=import_churn top_level_delta=1 added_imports=1 pruned_imports=0 restructured_families=1 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C101/probes/suite_red_taxonomy.py restructured=ImportFrom[3->4]
- face:   docs/evidence/C101/probes/suite_red_taxonomy.py added_import=Any
- face: file=docs/evidence/C102/probes/archive_faces.py status=import_churn top_level_delta=1 added_imports=0 pruned_imports=1 restructured_families=1 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C102/probes/archive_faces.py restructured=Import[4->3]
- face:   docs/evidence/C102/probes/archive_faces.py pruned_import=shutil
- face: file=docs/evidence/C102/probes/ths_format_audit.py status=identical top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=0 whitespace_pairs=0 value_moves=0
- face: file=docs/evidence/C102/probes/ths_grant_arms.py status=values_intact top_level_delta=2 added_imports=1 pruned_imports=0 restructured_families=4 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C102/probes/ths_grant_arms.py restructured=FunctionDef:_load_test_module[1->1],FunctionDef:main[1->1],If:block[1->2],ImportFrom[3->4]
- face:   docs/evidence/C102/probes/ths_grant_arms.py added_import=TYPE_CHECKING
- face: file=docs/evidence/C102/probes/ths_grant_repair.py status=values_intact top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=2 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C102/probes/ths_grant_repair.py restructured=FunctionDef:_class_models[1->1],FunctionDef:main[1->1]
- face: file=docs/evidence/C102/probes/ths_hunk_audit.py status=import_churn top_level_delta=1 added_imports=0 pruned_imports=1 restructured_families=1 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C102/probes/ths_hunk_audit.py restructured=Import[3->2]
- face:   docs/evidence/C102/probes/ths_hunk_audit.py pruned_import=re
- face: file=docs/evidence/C102/probes/ths_idempotence_arm.py status=values_intact top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=1 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C102/probes/ths_idempotence_arm.py restructured=FunctionDef:fetch_sites[1->1]
- face: file=docs/evidence/C103/batch5-scenario-census/probes/payload_literal_repin.py status=values_intact top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=1 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C103/batch5-scenario-census/probes/payload_literal_repin.py restructured=FunctionDef:main[1->1]
- face: file=docs/evidence/C103/batch5-scenario-census/probes/scenario_census.py status=values_intact top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=1 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C103/batch5-scenario-census/probes/scenario_census.py restructured=FunctionDef:main[1->1]
- face: file=docs/evidence/C103/batch5-scenario-census/probes/scenario_counterfact.py status=values_intact top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=2 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C103/batch5-scenario-census/probes/scenario_counterfact.py restructured=FunctionDef:main[1->1],FunctionDef:run_guard[1->1]
- face: file=docs/evidence/C103/batch5-scenario-census/probes/scenario_rebase.py status=values_intact top_level_delta=1 added_imports=0 pruned_imports=1 restructured_families=2 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C103/batch5-scenario-census/probes/scenario_rebase.py restructured=FunctionDef:main[1->1],Import[5->4]
- face:   docs/evidence/C103/batch5-scenario-census/probes/scenario_rebase.py pruned_import=re
- face: file=docs/evidence/C103/batch5-source-pins/probes/pin_rebase.py status=values_intact top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=1 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C103/batch5-source-pins/probes/pin_rebase.py restructured=FunctionDef:main[1->1]
- face: file=docs/evidence/C103/fmp-fetch-array-arity/probes/arity_ab.py status=identical top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=0 whitespace_pairs=0 value_moves=0
- face: file=docs/evidence/C103/probes/carrier_archiver.py status=identical top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=0 whitespace_pairs=0 value_moves=0
- face: file=docs/evidence/C103/probes/document_builder.py status=moved top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=4 whitespace_pairs=37 value_moves=4
- face:   docs/evidence/C103/probes/document_builder.py restructured=FunctionDef:module_split[1->1],FunctionDef:readme_census[1->1],FunctionDef:readme_fmp[1->1],FunctionDef:readme_pins[1->1]
- face:   docs/evidence/C103/probes/document_builder.py dropped_value=' OECD scenario rows: a census move, and the conflict underneath it\n\n## The disag...(+287)'
- face:   docs/evidence/C103/probes/document_builder.py dropped_value='snapshot_rows=\\d+ live_bindings=(\\d+)'
- face:   docs/evidence/C103/probes/document_builder.py added_value='\n\n## The disagreement, measured\n\nThe batch5 oracle pins ``scenario_present`` per...(+221)'
- face:   docs/evidence/C103/probes/document_builder.py added_value=' OECD scenario rows: a census move, and the conflict underneath it'
- face: file=docs/evidence/C104/h15-series-enumeration/probes/carrier_archiver.py status=identical top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=0 whitespace_pairs=0 value_moves=0
- face: file=docs/evidence/C104/h15-series-enumeration/probes/face_builder.py status=identical top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=0 whitespace_pairs=0 value_moves=0
- face: file=docs/evidence/C104/h15-series-enumeration/probes/h15_allowlist.py status=identical top_level_delta=0 added_imports=0 pruned_imports=0 restructured_families=0 whitespace_pairs=0 value_moves=0
- face: file=docs/evidence/C104/h15-series-enumeration/probes/payload_rescan.py status=values_intact top_level_delta=1 added_imports=0 pruned_imports=0 restructured_families=2 whitespace_pairs=0 value_moves=0
- face:   docs/evidence/C104/h15-series-enumeration/probes/payload_rescan.py restructured=Assign:_CONFIG_EDITS[0->1],Expr[11->11]
- face: file=docs/evidence/C105/a2-probe-repair/probes/ast_witness.py status=pre_state_absent
- face: file=docs/evidence/C105/a2-probe-repair/probes/docs_reidentity.py status=pre_state_absent
- face: file=docs/evidence/C105/a2-probe-repair/probes/split_long_lines.py status=pre_state_absent
- face: targets=34 ast_identical=12 values_intact=15 import_churn=3 whitespace_reflow=0 value_moves=1 pre_state_absent=3 findings=4
- verdict: FAIL - a repair moved a printed string value, not only its line length

## witness-control.txt

- carrier: witness-control.txt
- round: C105 a2-probe-repair
- source_bytes=407
- source_sha256_16=6b21d9219a31fd35
- argv=python docs/evidence/C105/a2-probe-repair/probes/ast_witness.py --control
- control_exit=0
- face: control_probe=docs/evidence/C101/census-rebase/probes/falsify_census_arms.py untampered_status=identical tampered_status=moved tampered_findings=2
- face:   control dropped_value='tests/fixtures/registry_batch5_snapshot.json'
- face:   control added_value='tests/fixtures/registry_batch5_snapshot.json!'
- face: control_arm=as expected
- face: mode=control controls_passed=1 controls_failed=0
