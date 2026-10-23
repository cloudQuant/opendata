"""Statically adjudicate the completed batch-5 metadata observation.

This judge consumes a pinned, already-recorded runtime observation. It does
not import OpenData product modules, load the registry, or run a new smoke.
"""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[6]
RUN_DIR = Path(__file__).resolve().parent
RUN_REL = (
    "docs/evidence/C100/integration/runs/"
    "central-wiring-20261012T193703Z-batch5-sloos-retake1/run.json"
)
RUN_SHA256 = "2e9251d4b9d54f7df6ebc46b407bc112d0d4af16afa7b973e3dd6245e48e7f81"
HEAD = "85f95a40a57995c9e46c9a3be1528f416b4f0c70"
PREIMAGE_TAR_REL = "docs/evidence/C100/integration/preimages/central-wiring-batch5-preimages.tar"
PREIMAGE_TAR_SHA256 = "199504b85daa64da366036868cfe675a100860718c09b277714447af398251dc"
BATCH3_RUN_REL = "docs/evidence/C100/integration/runs/central-wiring-20261012T144316Z-batch3/run.json"
BATCH3_RUN_SHA256 = "a1f9a75a1d36fc28129b8323c3e2bbf8a2e19d7b36e74dc6fb4964ed6a254c03"
BATCH4_DELTA_RUN_REL = "docs/evidence/C100/integration/runs/central-wiring-20261012T175242Z-batch4-finviz-retake1/run.json"
BATCH4_DELTA_RUN_SHA256 = "cf5d49076d5c8bef35c2ceeb778dd452fb59776db57f36615bb0c62c1849f07e"
BATCH4_RECEIPT_REL = (
    "docs/evidence/C100/acceptance-central-batch4/runs/"
    "independent-retake-20261012T1855Z-priority-null/receipt.json"
)
BATCH4_RECEIPT_SHA256 = "59400c28c9f26d3e9343538dbada3d746030ea430cc116cd0641d823bc38fed5"
SLOOS_FREEZE_REL = "docs/evidence/C100/fred-sloos/source-freeze-v2.json"
SLOOS_FREEZE_SHA256 = "1166b7e9b821dedad4f52cb80d197b5baae3545d11183a142b89d704429ea893"
SLOOS_SOURCE_RECEIPT_REL = "docs/evidence/C100/acceptance-fred-sloos/runs/20261012T181729.721761Z/receipt.json"
SLOOS_SOURCE_RECEIPT_SHA256 = "6bbf32147907ddc9729865cbe54e7b7446bd7f47e93b5d345883e2e1de6cf4c5"
FIXED_CSV_REL = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
FIXED_CSV_SHA256 = "3bcfc4a0472c4496c714518e9303e33fd543a4170bc52529d7137dc47bc4c5d5"

LEGACY_GAPS = ["futures_fundamentals", "stock_adjust"]
SLOOS_IDENTITY = ("fred", "SeniorLoanOfficerSurvey")
EXPECTED_BATCH4_ADDITIONS = {
    ("finviz", "EtfPricePerformance"),
    ("finviz", "PricePerformance"),
}
EXPECTED_LEGACY_DOMAINS = {
    "stock_adjust": {
        "display_name": "A股复权因子",
        "rest_path": "stock/adjust",
        "contract": "AdjustFactor",
        "priority": "P0",
    },
    "futures_fundamentals": {
        "display_name": "期货合约基本面",
        "rest_path": "futures/fundamentals",
        "contract": "FuturesFundamentals",
        "priority": "P0",
    },
}
CORE_HASH_PATHS = (
    "opendata/data/domains.py",
    "opendata/data/domains.yaml",
    "opendata/data/models/__init__.py",
    "opendata/data/providers/catalog.py",
    "opendata/data/provider.py",
    "opendata/data/registry.py",
    "opendata/data/capability.py",
    "opendata/data/providers/_engine/spec.py",
    "opendata/data/providers/_engine/http_json.py",
    "opendata/data/providers/_engine/row_expansion.py",
    "opendata/data/http_client.py",
    "opendata/data/request_budget.py",
    "opendata/services/provider_models.py",
    "opendata/services/provider_model_query.py",
    "opendata/api/provider_models.py",
    "opendata/api/provider_model_query.py",
)
SLOOS_HASH_PATHS = (
    "opendata/data/providers/fred/provider.py",
    "opendata/data/providers/fred/models/sloos.py",
    "opendata/data/providers/fred/sloos_series.py",
    "opendata/data/models/fred_sloos.py",
    "docs/evidence/C100/fred-sloos/source-freeze-v2.json",
    "docs/evidence/C100/acceptance-fred-sloos/runs/20261012T181729.721761Z/receipt.json",
)
EVIDENCE_HASH_PATHS = (
    "tests/test_domains.py",
    "opendata/data/providers/ths/endpoint_map.yaml",
    FIXED_CSV_REL,
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(relative: str) -> str:
    return _sha256_bytes((ROOT / relative).read_bytes())


def _load_json(relative: str) -> dict[str, Any]:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _yaml_domains(data: bytes, label: str) -> dict[str, Any]:
    loaded = yaml.safe_load(data)
    if not isinstance(loaded, dict) or not isinstance(loaded.get("domains"), dict):
        raise AssertionError(f"{label} has no top-level domains mapping")
    return loaded["domains"]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _pair_list(values: set[tuple[str, str]]) -> list[list[str]]:
    return [list(value) for value in sorted(values)]


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _main() -> None:
    run_path = ROOT / RUN_REL
    _require(_sha256_file(RUN_REL) == RUN_SHA256, "pinned retake1 receipt SHA changed")
    run = _load_json(RUN_REL)
    _require(run["run_id"] == "central-wiring-20261012T193703Z-batch5-sloos-retake1", "wrong run id")
    _require(run["status"] == "FAIL_GUARDED_METADATA_SMOKE", "original runtime status was changed")
    _require(run["exit_code"] == 1, "original runtime exit code was changed")
    _require(run["error"]["type"] == "RuntimeError", "unexpected smoke failure type")
    _require(
        run["error"]["message"] == "capability and DomainSpec identities do not reconcile",
        "unexpected smoke failure reason",
    )

    archive_path = ROOT / PREIMAGE_TAR_REL
    _require(_sha256_file(PREIMAGE_TAR_REL) == PREIMAGE_TAR_SHA256, "batch5 preimage archive SHA changed")
    with tarfile.open(archive_path, "r") as archive:
        preimage_member = archive.extractfile("opendata/data/domains.yaml")
        if preimage_member is None:
            raise AssertionError("batch5 domains preimage is absent")
        preimage_bytes = preimage_member.read()
    preimage_domains = _yaml_domains(preimage_bytes, "batch5 domains preimage")

    current_domains_path = ROOT / "opendata/data/domains.yaml"
    current_domains_bytes = current_domains_path.read_bytes()
    current_domains = _yaml_domains(current_domains_bytes, "current domains.yaml")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    _require(head == HEAD, f"HEAD changed: expected {HEAD}, found {head}")
    head_domain_bytes = subprocess.run(
        ["git", "show", "HEAD:opendata/data/domains.yaml"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    head_domains = _yaml_domains(head_domain_bytes, "HEAD domains.yaml")

    before_ids = set(preimage_domains)
    domain_ids_after = set(current_domains)
    added_domains = domain_ids_after - before_ids
    deleted_domains = before_ids - domain_ids_after
    changed_domains = {
        domain_id
        for domain_id in before_ids & domain_ids_after
        if preimage_domains[domain_id] != current_domains[domain_id]
    }
    _require(added_domains == {"senior_loan_officer_survey"}, f"unexpected domain additions: {sorted(added_domains)}")
    _require(not deleted_domains, f"domains deleted from batch5 preimage: {sorted(deleted_domains)}")
    _require(not changed_domains, f"existing domains changed in batch5: {sorted(changed_domains)}")
    _require(len(preimage_domains) == 336 and len(current_domains) == 337, "unexpected domain count delta")

    for domain_id, expected in EXPECTED_LEGACY_DOMAINS.items():
        _require(preimage_domains.get(domain_id) == expected, f"preimage legacy domain differs: {domain_id}")
        _require(current_domains.get(domain_id) == expected, f"current legacy domain differs: {domain_id}")
        _require(head_domains.get(domain_id) == expected, f"HEAD legacy domain differs: {domain_id}")
        normalized = run["observation"]["domains"].get(domain_id)
        _require(normalized is not None, f"runtime observation lacks {domain_id}")
        _require(
            normalized["display_name"] == expected["display_name"]
            and normalized["rest_path"] == expected["rest_path"]
            and normalized["contract"] == expected["contract"]
            and normalized["priority"] == "P0"
            and normalized["temporal_kind"] is None
            and normalized["time_field"] is None
            and normalized["natural_key"] == []
            and normalized["filter_dims"] == []
            and normalized["storage_mode"] is None
            and normalized["permissions"] == [],
            f"legacy domain normalization changed: {domain_id}",
        )
        _require(
            set(current_domains[domain_id]) == {"display_name", "rest_path", "contract", "priority"},
            f"legacy domain acquired query semantics: {domain_id}",
        )

    observation = run["observation"]
    _require(
        set(observation["domain_ids_without_capability"]) == set(LEGACY_GAPS),
        "the only domain-without-capability set is not the exact approved legacy set",
    )
    _require(observation["capability_domains_without_domain"] == [], "a capability has no DomainSpec")
    _require(observation["query_contract_failure_count"] == 0, "query contract failures are present")
    _require(observation["fetcher_output_contract_mismatch_count"] == 0, "output contract mismatches are present")

    p0_test = (ROOT / "tests/test_domains.py").read_text(encoding="utf-8")
    p0_methods = (
        "test_all_p0_domains_registered",
        "test_explicit_p0_priorities_match_requirement_scope",
    )
    _require(all(name in p0_test for name in p0_methods), "expected P0 tests are absent")
    _require(p0_test.count('"stock_adjust"') >= 2, "stock_adjust is not present in both P0 assertions")
    _require(p0_test.count('"futures_fundamentals"') >= 2, "futures_fundamentals is not present in both P0 assertions")
    p0_lines = {
        name: next(i for i, line in enumerate(p0_test.splitlines(), 1) if f"def {name}" in line)
        for name in p0_methods
    }

    endpoint_map_rel = "opendata/data/providers/ths/endpoint_map.yaml"
    endpoint_lines = (ROOT / endpoint_map_rel).read_text(encoding="utf-8").splitlines()
    endpoint_matches = [
        (i, line.strip())
        for i, line in enumerate(endpoint_lines, 1)
        if "/api/futures/fundamentals/indicators-historical" in line
    ]
    _require(len(endpoint_matches) == 1, "expected THS futures endpoint row is not unique")
    endpoint_line, endpoint_text = endpoint_matches[0]
    _require("status: client_only" in endpoint_text, "THS futures fundamentals is not documented client_only")
    _require("domain: futures_fundamentals" in endpoint_text, "THS endpoint no longer maps to legacy domain")

    batch3 = _load_json(BATCH3_RUN_REL)
    _require(_sha256_file(BATCH3_RUN_REL) == BATCH3_RUN_SHA256, "batch3 run SHA changed")
    batch4_delta = _load_json(BATCH4_DELTA_RUN_REL)
    _require(_sha256_file(BATCH4_DELTA_RUN_REL) == BATCH4_DELTA_RUN_SHA256, "batch4 delta run SHA changed")
    batch4_receipt = _load_json(BATCH4_RECEIPT_REL)
    _require(_sha256_file(BATCH4_RECEIPT_REL) == BATCH4_RECEIPT_SHA256, "independent batch4 receipt SHA changed")

    batch3_rows = batch3["identity_manifest"]["rows"]
    _require(len(batch3_rows) == 347, "batch3 identity manifest row count changed")
    batch3_ids = {(row["source"], row["model"]) for row in batch3_rows}
    _require(len(batch3_ids) == 347, "batch3 identity manifest has duplicate identities")
    batch4_rows = batch4_delta["observation"]["new_model_rows"]
    batch4_ids = {(row["source"], row["canonical"]) for row in batch4_rows}
    _require(batch4_ids == EXPECTED_BATCH4_ADDITIONS, f"batch4 identity delta differs: {sorted(batch4_ids)}")
    _require(not (batch3_ids & batch4_ids), "batch4 additions duplicate a batch3 identity")

    current_binding_rows = observation["registered_binding_identities"]
    current_binding_ids = {(row["source"], row["canonical"]) for row in current_binding_rows}
    _require(len(current_binding_rows) == 350, "runtime observation binding row count is not 350")
    _require(len(current_binding_ids) == 350, "runtime observation contains duplicate binding identities")
    sloos_rows = [row for row in current_binding_rows if (row["source"], row["canonical"]) == SLOOS_IDENTITY]
    _require(len(sloos_rows) == 1, "SLOOS identity is not present exactly once")
    sloos_binding = sloos_rows[0]
    _require(sloos_binding["domain"] == "senior_loan_officer_survey", "SLOOS domain differs")
    _require(sloos_binding["contract"] == "FredSeniorLoanOfficerSurvey", "SLOOS contract differs")
    _require(
        sloos_binding["fqclass"] == "opendata.data.providers.fred.models.sloos:FredSeniorLoanOfficerSurveyFetcher",
        "SLOOS fetcher identity differs",
    )
    _require(sloos_binding["capability"] == {
        "asset_class": "macro", "market": "us", "period": "quarterly", "source": "fred", "verified": False
    }, "SLOOS capability metadata differs")
    _require(sloos_binding["scenario_present"] is False, "SLOOS has an unapproved scenario")
    expected_binding_ids = batch3_ids | batch4_ids | {SLOOS_IDENTITY}
    _require(current_binding_ids == expected_binding_ids, "runtime binding set differs from exact batch3+4+5 lineage")

    fixed_csv_path = ROOT / FIXED_CSV_REL
    _require(_sha256_file(FIXED_CSV_REL) == FIXED_CSV_SHA256, "fixed identity CSV SHA changed")
    with fixed_csv_path.open(encoding="utf-8-sig", newline="") as stream:
        fixed_rows = list(csv.DictReader(stream))
    fixed_ids = {(row["provider"], row["upstream_model"]) for row in fixed_rows}
    _require(len(fixed_rows) == 350 and len(fixed_ids) == 350, "fixed inventory is not 350 unique identities")
    fixed_bound = fixed_ids & current_binding_ids
    missing_fixed = fixed_ids - current_binding_ids
    batch4_unbound = {
        (row[0], row[1]) for row in batch4_receipt["fixed_identity_audit"]["fixed_unbound_identities"]
    }
    _require(len(batch4_unbound) == 34, "batch4 fixed-unbound baseline is not 34 identities")
    expected_missing = batch4_unbound - {SLOOS_IDENTITY}
    observed_missing = {(row[0], row[1]) for row in observation["missing_fixed_identities"]}
    _require(missing_fixed == expected_missing, "fixed missing set differs from batch4 oracle minus SLOOS")
    _require(observed_missing == expected_missing, "runtime missing set differs from exact fixed-set calculation")
    _require(
        {
            (row[0], row[1])
            for row in observation["expected_missing_fixed_identities_from_batch4_minus_batch5_additions"]
        }
        == expected_missing,
        "recorded batch4-minus-batch5 expected missing set differs",
    )
    _require(len(fixed_bound) == 317 and len(missing_fixed) == 33, "fixed inventory counts differ")
    _require(observation["missing_set_matches_independent_batch4_oracle"] is True, "runtime oracle comparison failed")
    _require(observation["duplicate_runtime_identity_rows"] == [], "runtime duplicate rows are present")
    _require(observation["fixed_inventory_sha256"] == FIXED_CSV_SHA256, "runtime fixed inventory pin differs")

    domain_observation = observation["domains"]
    _require(len(domain_observation) == 337, "runtime observed domain count is not 337")
    sloos_domain = domain_observation["senior_loan_officer_survey"]
    _require(
        sloos_domain["contract"] == "FredSeniorLoanOfficerSurvey"
        and sloos_domain["temporal_kind"] == "series"
        and sloos_domain["time_field"] == "date"
        and sloos_domain["natural_key"] == ["date", "symbol", "category", "transform"]
        and sloos_domain["filter_dims"] == ["date", "symbol", "category", "transform"]
        and sloos_domain["storage_mode"] == "transient"
        and sloos_domain["permissions"] == ["query"],
        "SLOOS DomainSpec semantics differ",
    )

    export_names = observation["contract_model_export_names"]
    non_contract_names = observation["non_contract_export_names"]
    _require(len(export_names) == 326 and "ContractModel" in export_names, "contract export count/base semantics differ")
    _require(len(non_contract_names) == 4, "non-contract public symbol count differs")
    _require(
        set(non_contract_names)
        == {"BlsCatalogPage", "FredOutputType", "FredSeniorLoanOfficerSurveyQuery", "FredTransformUnits"},
        "non-contract export identities differ",
    )
    _require(observation["counts"] == {
        "contract_model_exports": 326,
        "domains": 337,
        "fixed_bound_intersection": 317,
        "fixed_identities": 350,
        "fixed_unbound": 33,
        "fixed_unique_identities": 350,
        "provider_descriptors": 34,
        "provider_model_legs": 350,
        "public_all_symbols": 330,
    }, "recorded batch5 counts differ")

    _require(run["a2_check"]["status"] == "PASS", "A2 result is not PASS")
    _require(all(run["guard_setup"]["assertions"].values()), "guard setup assertions are not all true")
    _require(run["hash_drift"] == [], "runtime smoke recorded hash drift")
    _require(
        _sha256_file(SLOOS_FREEZE_REL) == SLOOS_FREEZE_SHA256
        and _sha256_file(SLOOS_SOURCE_RECEIPT_REL) == SLOOS_SOURCE_RECEIPT_SHA256,
        "SLOOS source freeze/direct receipt changed",
    )

    receipt_after = run["hashes_after"]
    pinned_paths = CORE_HASH_PATHS + SLOOS_HASH_PATHS
    observed_core_hashes: dict[str, str] = {}
    for relative in pinned_paths:
        expected = receipt_after.get(relative)
        _require(expected is not None, f"runtime receipt lacks central source pin {relative}")
        actual = _sha256_file(relative)
        _require(actual == expected, f"current file drifted from retake1 pin: {relative}")
        observed_core_hashes[relative] = actual
    evidence_hashes = {relative: _sha256_file(relative) for relative in EVIDENCE_HASH_PATHS}

    domains_file_sha_before = run["hashes_before"]["opendata/data/domains.yaml"]
    domains_file_sha_after = run["hashes_after"]["opendata/data/domains.yaml"]
    _require(domains_file_sha_before == domains_file_sha_after, "runtime receipt reports domains.yaml drift")
    _require(_sha256_bytes(current_domains_bytes) == domains_file_sha_after, "current domains differs from smoke after-pin")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    changed_identity_rows = [
        {
            "source": row["source"],
            "canonical_model": row["canonical"],
            "domain": row["domain"],
            "fetcher": row["fqclass"],
            "contract": row["contract"],
        }
        for row in batch4_rows
    ] + [
        {
            "source": sloos_binding["source"],
            "canonical_model": sloos_binding["canonical"],
            "domain": sloos_binding["domain"],
            "fetcher": sloos_binding["fqclass"],
            "contract": sloos_binding["contract"],
        }
    ]

    adjudication = {
        "schema_version": 1,
        "adjudication_id": "central-wiring-batch5-exact-legacy-domain-gap",
        "created_at_utc": now,
        "status": "ADJUDICATED_WITH_EXACT_LEGACY_DOMAIN_CAPABILITY_GAP",
        "runtime_execution": False,
        "runtime_receipt": {"path": RUN_REL, "sha256": RUN_SHA256, "status": run["status"], "exit_code": run["exit_code"]},
        "scope": "Static adjudication of the recorded batch5 SLOOS metadata observation. No new product import, registry load, service query, or smoke was run.",
        "adjudication": {
            "allowed_domain_ids_without_capability": LEGACY_GAPS,
            "classification": "LEGACY_DOMAIN_CAPABILITY_GAP",
            "queryable": False,
            "complete_coverage_claim": False,
            "reason": "Both unchanged P0 DomainSpecs contain only display_name/rest_path/contract/priority and have no capability binding or query semantics. tests/test_domains.py requires their P0 presence; THS documents the futures_fundamentals endpoint as client_only. These checks do not create provider capability, queryability, or source coverage.",
            "all_other_domain_capability_gaps": "FAIL_IF_NONEMPTY",
            "observed_capability_domains_without_domain": observation["capability_domains_without_domain"],
            "observed_query_contract_failure_count": observation["query_contract_failure_count"],
            "observed_output_contract_mismatch_count": observation["fetcher_output_contract_mismatch_count"],
        },
        "domain_delta_from_archived_batch5_preimage": {
            "added": sorted(added_domains),
            "deleted": sorted(deleted_domains),
            "changed_existing": sorted(changed_domains),
            "expected_exact_addition": "senior_loan_officer_survey",
            "legacy_domain_raw_entries_identical_across_head_preimage_current": True,
        },
        "binding_lineage": {
            "batch3_run": {"path": BATCH3_RUN_REL, "sha256": BATCH3_RUN_SHA256, "unique_identity_count": len(batch3_ids)},
            "batch4_delta_run": {"path": BATCH4_DELTA_RUN_REL, "sha256": BATCH4_DELTA_RUN_SHA256, "identities": changed_identity_rows[:2]},
            "batch5_sloos_addition": changed_identity_rows[2],
            "current_unique_binding_identities": len(current_binding_ids),
            "current_binding_ids_equal_exact_lineage": True,
        },
        "fixed_identity_reconciliation": {
            "fixed_inventory_path": FIXED_CSV_REL,
            "fixed_inventory_sha256": FIXED_CSV_SHA256,
            "fixed_identity_count": len(fixed_ids),
            "fixed_bound_count": len(fixed_bound),
            "fixed_unbound_count": len(missing_fixed),
            "missing_fixed_identities": _pair_list(missing_fixed),
            "batch4_unbound_receipt": {"path": BATCH4_RECEIPT_REL, "sha256": BATCH4_RECEIPT_SHA256},
            "batch4_unbound_count": len(batch4_unbound),
            "expected_missing_is_exact_batch4_unbound_minus_sloos": True,
            "duplicate_runtime_identity_rows": [],
        },
        "export_count_erratum": {
            "public_all_symbols": 330,
            "issubclass_contract_model_exports_including_base": 326,
            "non_base_contract_model_class_exports": 325,
            "base_class_export": "ContractModel",
            "non_contract_symbols": sorted(non_contract_names),
            "business_model_count_claim": False,
            "definition": "The 326 runtime issubclass count includes ContractModel itself because issubclass(ContractModel, ContractModel) is true; 325 are non-base subclasses. These are export-class counts, not a business-model count.",
        },
        "preserved_historical_artifacts": {
            "old_candidate": {"path": "docs/evidence/C100/integration/candidates/central-wiring-batch5-candidate.json", "sha256": "775dd322e145d84f0a1a850d46fd5f0754814ac68445c3b02bab4a41239e047e"},
            "old_static_freeze": {"path": "docs/evidence/C100/integration/candidates/central-wiring-batch5-static-freeze.json", "sha256": "988bf1feef4b37cb9688ed4bd3377447b9a282ce2480898ea0fdd9e72392aaba"},
            "original_failed_smoke_receipts_modified": False,
        },
    }

    candidate_addendum = {
        "schema_version": 1,
        "candidate_id": "central-wiring-batch5-count-adjudication",
        "status": "ADJUDICATED_STATIC_CANDIDATE_NOT_NEW_RUNTIME",
        "created_at_utc": now,
        "supersedes_label_only": "329 public_contract_model_exports",
        "baseline_candidate": {
            "path": "docs/evidence/C100/integration/candidates/central-wiring-batch5-candidate.json",
            "sha256": "775dd322e145d84f0a1a850d46fd5f0754814ac68445c3b02bab4a41239e047e",
        },
        "runtime_observation": {"path": RUN_REL, "sha256": RUN_SHA256},
        "counts": {
            "fixed_identities": 350,
            "fixed_bound": 317,
            "fixed_unbound": 33,
            "domain_specs": 337,
            "provider_model_legs": 350,
            "provider_descriptors": 34,
            "public_all_symbols": 330,
            "issubclass_contract_model_exports_including_base": 326,
            "non_base_contract_model_class_exports": 325,
            "non_contract_public_symbols": sorted(non_contract_names),
        },
        "domain_capability_adjudication": {
            "domain_ids_without_capability": LEGACY_GAPS,
            "classification": "LEGACY_DOMAIN_CAPABILITY_GAP",
            "not_queryable": True,
            "not_covered_or_complete": True,
            "any_additional_domain_or_capability_difference": "FAIL",
        },
        "lineage_delta": changed_identity_rows,
        "missing_fixed_identities": _pair_list(missing_fixed),
        "runtime_status_unchanged": run["status"],
    }

    adj_path = RUN_DIR / "batch5-adjudication.json"
    candidate_path = ROOT / "docs/evidence/C100/integration/candidates/central-wiring-batch5-count-adjudication.json"
    _write_json(adj_path, adjudication)
    _write_json(candidate_path, candidate_addendum)
    adj_rel = adj_path.relative_to(ROOT).as_posix()
    candidate_rel = candidate_path.relative_to(ROOT).as_posix()

    source_freeze = {
        "schema_version": 1,
        "freeze_id": "central-wiring-batch5-adjudicated-static-freeze",
        "created_at_utc": now,
        "status": "STATIC_SOURCE_FREEZE_NO_NEW_RUNTIME",
        "runtime_receipt": {"path": RUN_REL, "sha256": RUN_SHA256, "runtime_status": run["status"], "exit_code": run["exit_code"]},
        "adjudication": {"path": adj_rel, "sha256": _sha256_file(adj_rel)},
        "candidate_count_addendum": {"path": candidate_rel, "sha256": _sha256_file(candidate_rel)},
        "judge_script": {
            "path": Path(__file__).relative_to(ROOT).as_posix(),
            "sha256": _sha256_file(Path(__file__).relative_to(ROOT).as_posix()),
            "command": "OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 /Users/yunjinqi/opt/anaconda3/bin/conda run -n base python " + Path(__file__).relative_to(ROOT).as_posix(),
        },
        "git": {"head": head, "head_domains_file_sha256": _sha256_bytes(head_domain_bytes)},
        "preimage": {
            "archive_path": PREIMAGE_TAR_REL,
            "archive_sha256": PREIMAGE_TAR_SHA256,
            "domains_yaml_sha256": _sha256_bytes(preimage_bytes),
            "domains_yaml_sha256_runtime_before": domains_file_sha_before,
            "domains_yaml_sha256_runtime_after": domains_file_sha_after,
            "archive_is_earlier_pre_sloos_snapshot": _sha256_bytes(preimage_bytes) != domains_file_sha_before,
        },
        "core_hashes_from_retaken_runtime_and_current_files": observed_core_hashes,
        "sloos_source_hashes": {path: _sha256_file(path) for path in SLOOS_HASH_PATHS},
        "static_context_hashes": evidence_hashes,
        "source_facts": {
            "legacy_domain_gap_ids": LEGACY_GAPS,
            "legacy_domain_raw_fields": EXPECTED_LEGACY_DOMAINS,
            "legacy_domains_have_no_query_semantics": True,
            "test_domains_p0_assertion_methods": p0_lines,
            "ths_futures_fundamentals_endpoint": {
                "path": "/api/futures/fundamentals/indicators-historical",
                "status": "client_only",
                "domain": "futures_fundamentals",
                "endpoint_map_line": endpoint_line,
                "endpoint_map_text": endpoint_text,
            },
            "sloos": {
                "identity": ["fred", "SeniorLoanOfficerSurvey"],
                "domain": "senior_loan_officer_survey",
                "verified": False,
                "scenario_present": False,
                "domain_semantics": sloos_domain,
            },
            "batch5_domain_diff": {"added": sorted(added_domains), "deleted": [], "changed_existing": []},
            "batch5_binding_delta": changed_identity_rows,
            "fixed_identity_counts": {"total": 350, "bound": 317, "unbound": 33},
            "export_count_classification": adjudication["export_count_erratum"],
            "no_freeform_capability_notes_in_summary": True,
        },
        "no_new_runtime_execution": True,
        "acceptance": "NOT_CLAIMED; independent central/provider acceptance remains separate",
    }
    freeze_path = RUN_DIR / "batch5-source-freeze.json"
    _write_json(freeze_path, source_freeze)

    print(json.dumps({
        "status": "PASS_STATIC_ADJUDICATION",
        "runtime_smoke_rerun": False,
        "runtime_receipt_status_preserved": run["status"],
        "domain_delta": {"added": sorted(added_domains), "deleted": [], "changed_existing": []},
        "legacy_domain_capability_gap": LEGACY_GAPS,
        "bindings": {"batch3": len(batch3_ids), "batch4_additions": len(batch4_ids), "batch5_addition": 1, "current": len(current_binding_ids)},
        "fixed_inventory": {"total": len(fixed_ids), "bound": len(fixed_bound), "unbound": len(missing_fixed)},
        "export_counts": {"all_symbols": 330, "contract_subclasses_including_base": 326, "non_base_subclasses": 325},
        "artifacts": {
            "adjudication": {"path": adj_rel, "sha256": _sha256_file(adj_rel)},
            "candidate_count_addendum": {"path": candidate_rel, "sha256": _sha256_file(candidate_rel)},
            "source_freeze": {"path": freeze_path.relative_to(ROOT).as_posix(), "sha256": _sha256_file(freeze_path.relative_to(ROOT).as_posix())},
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
