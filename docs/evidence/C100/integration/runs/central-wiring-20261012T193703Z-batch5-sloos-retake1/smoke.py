"""Guarded full metadata/import/registration snapshot for central wiring batch 5."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[6]
RUN_DIR = Path(__file__).resolve().parent
RUN_ID = RUN_DIR.name
GUARD_PATH = Path("docs/evidence/C100/acceptance-federal-reserve-clean-extra/hard_io_guard.py")
TASK_CSV = Path(
    "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
)
BASELINE_RUN = Path(
    "docs/evidence/C100/integration/runs/central-wiring-20261012T175242Z-batch4-finviz-retake1/run.json"
)
BATCH4_UNBOUND_RECEIPT = Path(
    "docs/evidence/C100/acceptance-central-batch4/runs/independent-retake-20261012T1855Z-priority-null/receipt.json"
)
BATCH4_UNBOUND_RECEIPT_SHA256 = (
    "59400c28c9f26d3e9343538dbada3d746030ea430cc116cd0641d823bc38fed5"
)
PRIOR_FAILED_RUN = Path(
    "docs/evidence/C100/integration/runs/central-wiring-20261012T192822Z-batch5-sloos/run.json"
)
PRIOR_FAILED_RUN_SHA256 = (
    "bcb5e6374e7d087296f216a257f5f253a779360c9b82b409492701b75f5fab36"
)
HARNESS_CORRECTION = Path(
    "docs/evidence/C100/integration/runs/central-wiring-20261012T193703Z-batch5-sloos-retake1/harness-correction.json"
)
NEWLY_BOUND_BATCH5_IDENTITIES = {("fred", "SeniorLoanOfficerSurvey")}
PIN_PATHS = [
    "opendata/data/domains.py",
    "opendata/data/domains.yaml",
    "opendata/data/models/__init__.py",
    "opendata/data/providers/catalog.py",
    "opendata/data/provider.py",
    "opendata/data/registry.py",
    "opendata/data/capability.py",
    "opendata/data/protocol.py",
    "opendata/data/providers/_engine/spec.py",
    "opendata/data/providers/_engine/http_json.py",
    "opendata/data/providers/_engine/row_expansion.py",
    "opendata/data/http_client.py",
    "opendata/data/request_budget.py",
    "opendata/services/provider_models.py",
    "opendata/services/provider_model_query.py",
    "opendata/api/provider_models.py",
    "opendata/api/provider_model_query.py",
    "opendata/data/models/base.py",
    "opendata/data/models/fred_sloos.py",
    "opendata/data/providers/fred/provider.py",
    "opendata/data/providers/fred/models/sloos.py",
    "opendata/data/providers/fred/sloos_series.py",
    "docs/evidence/C100/fred-sloos/source-freeze-v2.json",
    "docs/evidence/C100/acceptance-fred-sloos/runs/20261012T181729.721761Z/receipt.json",
    "docs/evidence/C100/integration/candidates/central-wiring-batch5-candidate.json",
    "docs/evidence/C100/integration/candidates/central-wiring-batch5-static-freeze.json",
    "docs/evidence/C100/integration/preimages/central-wiring-batch5-preimages.tar",
    "docs/evidence/C100/integration/runs/central-wiring-20261012T175242Z-batch4-finviz-retake1/run.json",
    "docs/evidence/C100/integration/runs/central-wiring-20261012T193703Z-batch5-sloos-retake1/a2-result.txt",
    "docs/evidence/C100/integration/runs/central-wiring-20261012T192822Z-batch5-sloos/run.json",
    "docs/evidence/C100/acceptance-central-batch4/runs/independent-retake-20261012T1855Z-priority-null/receipt.json",
    "docs/evidence/C100/integration/runs/central-wiring-20261012T193703Z-batch5-sloos-retake1/harness-correction.json",
    "docs/evidence/C100/acceptance-federal-reserve-clean-extra/hard_io_guard.py",
    "docs/evidence/C100/acceptance-ac24/pytest_io_guard.py",
    "docs/evidence/C100/acceptance-ac24-independent/runs/shared-promotion-retake-retry1-20261013/final-independent-receipt.json",
]
CENTRAL_HASH_PATHS = [
    "opendata/data/domains.py",
    "opendata/data/domains.yaml",
    "opendata/data/models/__init__.py",
    "opendata/data/providers/catalog.py",
    "opendata/data/provider.py",
    "opendata/data/registry.py",
    "opendata/data/capability.py",
    "opendata/data/protocol.py",
    "opendata/data/providers/_engine/spec.py",
    "opendata/data/providers/_engine/http_json.py",
    "opendata/data/providers/_engine/row_expansion.py",
    "opendata/data/http_client.py",
    "opendata/data/request_budget.py",
    "opendata/services/provider_models.py",
    "opendata/services/provider_model_query.py",
    "opendata/api/provider_models.py",
    "opendata/api/provider_model_query.py",
]
EXPECTED_COUNTS = {
    "fixed_identities": 350,
    "fixed_unique_identities": 350,
    "fixed_bound_intersection": 317,
    "fixed_unbound": 33,
    "provider_model_legs": 350,
    "domains": 337,
    "provider_descriptors": 34,
    "contract_model_exports": 326,
    "public_all_symbols": 330,
}
EXPECTED_NON_CONTRACT_EXPORTS = {
    "BlsCatalogPage",
    "FredOutputType",
    "FredTransformUnits",
    "FredSeniorLoanOfficerSurveyQuery",
}
SHARED_GUARD_PINS = {
    "parent_guard_sha256": "e6d59bd7583d9a62d5c69658fda5980d025256d8aacb80a6ccf00202d8ffa6a1",
    "child_guard_sha256": "958a6019d811766061c3f1be5fd8746f4ab20498d1838eee010441a2d4751805",
    "aggregate_receipt_sha256": "9ab681aad41d3ae20f541fcd9297231e6696bdc57d0b3ad74d4d369a0c7c4b5a",
}


def utc_now() -> str:
    """Return a timezone-aware UTC timestamp compatible with Python 3.10."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256(relative_path: str | Path) -> str:
    """Hash one repository-relative evidence or source file."""
    digest = hashlib.sha256()
    with (ROOT / relative_path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fixed_identities() -> list[tuple[str, str]]:
    """Read the fixed inventory independently of the runtime provider registry."""
    with (ROOT / TASK_CSV).open(encoding="utf-8-sig", newline="") as stream:
        return [
            (row["provider"], row["upstream_model"])
            for row in csv.DictReader(stream)
        ]


def main() -> int:
    """Install the reviewed guard before product imports and capture all metadata."""
    os.environ["TESTING"] = "true"
    expected_guard_files = {
        GUARD_PATH.as_posix(): SHARED_GUARD_PINS["parent_guard_sha256"],
        "docs/evidence/C100/acceptance-ac24/pytest_io_guard.py": SHARED_GUARD_PINS[
            "child_guard_sha256"
        ],
        "docs/evidence/C100/acceptance-ac24-independent/runs/shared-promotion-retake-retry1-20261013/final-independent-receipt.json": SHARED_GUARD_PINS[
            "aggregate_receipt_sha256"
        ],
    }
    for path, expected_hash in expected_guard_files.items():
        if sha256(path) != expected_hash:
            raise RuntimeError(f"shared guard pin mismatch: {path}")
    sys.path.insert(0, str(ROOT / GUARD_PATH.parent))
    from hard_io_guard import HardIOGuard

    guard = HardIOGuard()
    guard.install()
    guard_setup = guard.summary()
    if guard_setup["opendata_modules_imported"] != []:
        raise RuntimeError("product modules were imported before the I/O guard")

    provider_code_paths = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "opendata/data/providers").rglob("*.py")
    )
    contract_module_paths = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "opendata/data/models").rglob("*.py")
    )
    pin_paths = list(
        dict.fromkeys(
            [
                *PIN_PATHS,
                *provider_code_paths,
                *contract_module_paths,
                (RUN_DIR / "smoke.py").relative_to(ROOT).as_posix(),
            ]
        )
    )
    before_hashes = {path: sha256(path) for path in pin_paths}
    started_at = utc_now()
    status = "FAIL"
    error: dict[str, str] | None = None
    observation: dict[str, Any] = {}
    exit_code = 1

    try:
        sys.path.insert(0, str(ROOT))

        import opendata.data.models as public_models
        from opendata.data.domains import contract_model, load_domains
        from opendata.data.models.base import ContractModel
        from opendata.data.providers.catalog import list_providers, register_providers
        from opendata.data.registry import ProviderRegistry
        from opendata.services.provider_model_query import _reviewed_query_contract

        domains = load_domains()
        providers = list_providers()
        registry = ProviderRegistry()
        registration_result = register_providers(registry)

        binding_rows: list[dict[str, Any]] = []
        output_contract_mismatches: list[dict[str, str]] = []
        capability_domains: set[str] = set()
        for provider in providers:
            for binding, fetcher in zip(
                provider.fetcher_bindings, provider.fetchers, strict=True
            ):
                capability = fetcher.capability
                domain_id = capability.domain
                capability_domains.add(domain_id)
                domain = domains.get(domain_id)
                if domain is None:
                    raise RuntimeError(f"capability domain is undeclared: {domain_id}")
                contract_type = contract_model(domain_id)
                output_type = getattr(fetcher, "output_model", None)
                if isinstance(output_type, type) and output_type is not contract_type:
                    output_contract_mismatches.append(
                        {
                            "source": provider.source,
                            "canonical": binding.canonical_model_ids[0]
                            if binding.canonical_model_ids
                            else binding.model_key,
                            "domain": domain_id,
                            "fetcher_output": output_type.__name__,
                            "domain_contract": contract_type.__name__,
                        }
                    )
                canonical_ids = binding.canonical_model_ids or (binding.model_key,)
                for model_id in canonical_ids:
                    binding_rows.append(
                        {
                            "source": provider.source,
                            "canonical": model_id,
                            "domain": domain_id,
                            "fqclass": f"{binding.module}:{binding.class_name}",
                            "contract": contract_type.__name__,
                            "capability": {
                                "asset_class": capability.asset_class,
                                "period": capability.period,
                                "market": capability.market,
                                "source": capability.source,
                                "verified": capability.verified,
                            },
                            "scenario_present": binding.scenario is not None,
                            "provenance_class": "declared_local_provider_binding",
                        }
                    )

        domain_rows: dict[str, dict[str, Any]] = {}
        query_contract_failures: list[dict[str, str]] = []
        for domain_id, spec in sorted(domains.items()):
            model_type = contract_model(domain_id)
            if spec.contract != model_type.__name__:
                raise RuntimeError(f"domain contract identity mismatch: {domain_id}")
            if getattr(public_models, spec.contract, None) is not model_type:
                raise RuntimeError(f"domain contract is not the public exported class: {domain_id}")
            contract_fields = set(model_type.model_fields)
            semantic_fields = (
                (() if spec.time_field is None else (spec.time_field,))
                + spec.natural_key
                + spec.filter_dims
            )
            missing_fields = sorted(set(semantic_fields) - contract_fields)
            if missing_fields:
                raise RuntimeError(
                    f"domain semantics reference absent contract fields: {domain_id}: {missing_fields}"
                )
            if "query" in spec.permissions:
                try:
                    _reviewed_query_contract(domain_id)
                except Exception as exc:
                    query_contract_failures.append(
                        {"domain": domain_id, "error_type": type(exc).__name__}
                    )
            domain_rows[domain_id] = {
                **spec.model_dump(mode="json"),
                "contract_fields": sorted(contract_fields),
            }

        contract_exports = sorted(
            name
            for name in public_models.__all__
            if isinstance(getattr(public_models, name, None), type)
            and issubclass(getattr(public_models, name), ContractModel)
        )
        non_contract_exports = sorted(set(public_models.__all__) - set(contract_exports))

        expected_rows = fixed_identities()
        expected = set(expected_rows)
        actual_rows = [(row["source"], row["canonical"]) for row in binding_rows]
        actual = set(actual_rows)
        missing_from_runtime = sorted(expected - actual)
        duplicate_runtime_rows = sorted(
            [source, model]
            for source, model in actual
            if actual_rows.count((source, model)) > 1
        )
        domain_ids_without_capability = sorted(set(domain_rows) - capability_domains)
        capability_domains_without_domain = sorted(capability_domains - set(domain_rows))

        if sha256(BATCH4_UNBOUND_RECEIPT) != BATCH4_UNBOUND_RECEIPT_SHA256:
            raise RuntimeError("independent batch4 fixed-unbound receipt hash mismatch")
        if sha256(PRIOR_FAILED_RUN) != PRIOR_FAILED_RUN_SHA256:
            raise RuntimeError("prior failed smoke receipt hash mismatch")
        batch4_receipt = json.loads((ROOT / BATCH4_UNBOUND_RECEIPT).read_text(encoding="utf-8"))
        batch4_unbound = {
            tuple(identity)
            for identity in batch4_receipt["fixed_identity_audit"]["fixed_unbound_identities"]
        }
        if len(batch4_unbound) != 34 or not NEWLY_BOUND_BATCH5_IDENTITIES <= batch4_unbound:
            raise RuntimeError("batch4 fixed-unbound oracle differs from the reviewed 34-identity baseline")
        expected_unbound = sorted(batch4_unbound - NEWLY_BOUND_BATCH5_IDENTITIES)

        fred = next(provider for provider in providers if provider.source == "fred")
        sloos_binding = next(
            binding
            for binding in fred.fetcher_bindings
            if "SeniorLoanOfficerSurvey" in binding.canonical_model_ids
        )
        sloos_fetcher = registry.resolve_model("fred", "SeniorLoanOfficerSurvey")
        sloos_spec = domains["senior_loan_officer_survey"]
        sloos_contract = contract_model("senior_loan_officer_survey")
        if (
            sloos_binding.scenario is not None
            or sloos_fetcher.capability.domain != "senior_loan_officer_survey"
            or sloos_fetcher.capability.source != "fred"
            or sloos_fetcher.capability.market != "us"
            or sloos_fetcher.capability.period != "quarterly"
            or sloos_fetcher.capability.verified is not False
            or sloos_fetcher.output_model is not sloos_contract
            or sloos_spec.temporal_kind != "series"
            or sloos_spec.time_field != "date"
            or sloos_spec.natural_key != ("date", "symbol", "category", "transform")
            or sloos_spec.filter_dims != ("date", "symbol", "category", "transform")
            or sloos_spec.storage_mode != "transient"
            or sloos_spec.permissions != ("query",)
        ):
            raise RuntimeError("SLOOS source binding or DomainSpec differs from the approved candidate")

        observed_counts = {
            "fixed_identities": len(expected_rows),
            "fixed_unique_identities": len(expected),
            "fixed_bound_intersection": len(expected & actual),
            "fixed_unbound": len(expected - actual),
            "provider_model_legs": len(binding_rows),
            "domains": len(domain_rows),
            "provider_descriptors": len(providers),
            "contract_model_exports": len(contract_exports),
            "public_all_symbols": len(public_models.__all__),
        }
        observation = {
            "counts": observed_counts,
            "fixed_inventory_sha256": sha256(TASK_CSV),
            "actual_binding_unique_identities": len(actual),
            "actual_local_identities_outside_fixed_inventory": sorted(actual - expected),
            "duplicate_runtime_identity_rows": duplicate_runtime_rows,
            "missing_fixed_identities": missing_from_runtime,
            "expected_missing_fixed_identities_from_batch4_minus_batch5_additions": expected_unbound,
            "missing_set_matches_independent_batch4_oracle": missing_from_runtime == expected_unbound,
            "batch4_unbound_receipt_path": BATCH4_UNBOUND_RECEIPT.as_posix(),
            "batch4_unbound_receipt_sha256": BATCH4_UNBOUND_RECEIPT_SHA256,
            "registered_capability_count": len(registry.capabilities()),
            "new_capabilities_returned": len(registration_result),
            "domain_ids_without_capability": domain_ids_without_capability,
            "capability_domains_without_domain": capability_domains_without_domain,
            "query_contract_failure_count": len(query_contract_failures),
            "fetcher_output_contract_mismatch_count": len(output_contract_mismatches),
            "contract_model_export_names": contract_exports,
            "non_contract_export_names": non_contract_exports,
            "provider_source_ids": [provider.source for provider in providers],
            "registered_binding_identities": sorted(
                binding_rows, key=lambda row: (row["source"], row["canonical"])
            ),
            "domains": domain_rows,
        }
        if observed_counts != EXPECTED_COUNTS:
            raise RuntimeError(f"central count mismatch: observed={observed_counts}")
        if set(non_contract_exports) != EXPECTED_NON_CONTRACT_EXPORTS:
            raise RuntimeError(
                "public model export classification differs from reviewed erratum: "
                f"{non_contract_exports}"
            )
        if missing_from_runtime != expected_unbound or duplicate_runtime_rows:
            raise RuntimeError(
                "fixed inventory missing/duplicate identities differ from the independent batch4 oracle: "
                f"missing={missing_from_runtime}; expected={expected_unbound}; duplicates={duplicate_runtime_rows}"
            )
        if domain_ids_without_capability or capability_domains_without_domain:
            raise RuntimeError("capability and DomainSpec identities do not reconcile")
        if query_contract_failures:
            raise RuntimeError("one or more query-enabled DomainSpecs fail reviewed contract validation")
        if output_contract_mismatches:
            raise RuntimeError("fetcher output_model differs from DomainSpec contract")

        status = "PASS_GUARDED_FULL_METADATA_IMPORT_DOMAIN_AND_REGISTRATION_SMOKE"
        exit_code = 0
    except Exception as exc:
        status = "FAIL_GUARDED_METADATA_SMOKE"
        error = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
        exit_code = 1

    finished_at = utc_now()
    after_hashes = {path: sha256(path) for path in pin_paths}
    hash_drift = sorted(path for path in pin_paths if before_hashes[path] != after_hashes[path])
    guard_after = guard.summary()
    setup_blocked = guard_setup["blocked_attempts"]
    runtime_blocked_delta = {
        key: guard.blocked_attempts.get(key, 0) - setup_blocked.get(key, 0)
        for key in set(guard.blocked_attempts) | set(setup_blocked)
    }
    runtime_blocked_delta = {key: value for key, value in runtime_blocked_delta.items() if value}
    guard_assertions = {
        "guard_installed_before_product_import": guard_setup["opendata_modules_imported"] == [],
        "no_blocked_attempts_after_guard_bootstrap": runtime_blocked_delta == {},
        "saved_original_dispatch_total_zero": guard_after["saved_original_dispatch_total"] == 0,
        "fixture_route_count_zero": guard_after["registered_fixture_route_count"] == 0,
        "dbapi_guard_set_unchanged_after_bootstrap": guard_after["loaded_dbapi_modules"]
        == guard_setup["loaded_dbapi_modules"],
        "no_hash_drift": hash_drift == [],
    }
    if not all(guard_assertions.values()):
        status = "FAIL_GUARD_OR_HASH_ASSERTION"
        exit_code = 1

    command = (
        "OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 "
        "/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base python "
        f"{(RUN_DIR / 'smoke.py').relative_to(ROOT).as_posix()}"
    )
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": status,
        "exit_code": exit_code,
        "command": command,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "python": sys.version,
        "scope": "guarded offline metadata/import/DomainSpec/registration only; no provider query, live network, or database",
        "change_scope": [
            "one FRED SeniorLoanOfficerSurvey lazy binding",
            "one senior_loan_officer_survey DomainSpec",
            "one ContractModel and one query class public export",
        ],
        "shared_guard": {
            "parent_path": GUARD_PATH.as_posix(),
            "parent_sha256": SHARED_GUARD_PINS["parent_guard_sha256"],
            "child_path": "docs/evidence/C100/acceptance-ac24/pytest_io_guard.py",
            "child_sha256": SHARED_GUARD_PINS["child_guard_sha256"],
            "aggregate_receipt_path": "docs/evidence/C100/acceptance-ac24-independent/runs/shared-promotion-retake-retry1-20261013/final-independent-receipt.json",
            "aggregate_receipt_sha256": SHARED_GUARD_PINS["aggregate_receipt_sha256"],
        },
        "a2_check": {
            "receipt_path": (RUN_DIR / "a2-result.txt").relative_to(ROOT).as_posix(),
            "receipt_sha256": before_hashes[(RUN_DIR / "a2-result.txt").relative_to(ROOT).as_posix()],
            "files": [
                "opendata/data/providers/fred/provider.py",
                "opendata/data/models/__init__.py",
            ],
            "status": "PASS",
            "checks": ["ruff check", "ruff format --check", "mypy", "bandit"],
        },
        "expected_counts": EXPECTED_COUNTS,
        "predicate_correction": {
            "prior_run_path": PRIOR_FAILED_RUN.as_posix(),
            "prior_run_sha256": PRIOR_FAILED_RUN_SHA256,
            "prior_predicate": "if missing_from_runtime or duplicate_runtime_rows: fail",
            "corrected_predicate": "missing set must exactly equal batch4 fixed-unbound identities minus batch5 additions; duplicate set must be empty",
            "correction_manifest_path": HARNESS_CORRECTION.as_posix(),
            "correction_manifest_sha256": before_hashes[HARNESS_CORRECTION.as_posix()],
        },
        "count_erratum": {
            "supersedes_candidate_label": "329 public_contract_model_exports",
            "observed_contract_model_exports": 326,
            "public_all_symbols": 330,
            "non_contract_exports": sorted(EXPECTED_NON_CONTRACT_EXPORTS),
            "basis": "Anaconda runtime introspection: count exported classes that subclass ContractModel; the other four public symbols are not ContractModel subclasses.",
            "old_candidate_and_static_freeze_preserved": True,
        },
        "hashes_before": before_hashes,
        "hashes_after": after_hashes,
        "hash_drift": hash_drift,
        "guard_setup": {
            "summary": guard_setup,
            "runtime_blocked_attempt_delta": runtime_blocked_delta,
            "summary_after": guard_after,
            "assertions": guard_assertions,
        },
        "observation": observation,
        "error": error,
    }
    if status.startswith("PASS"):
        receipt["stdout"] = (
            "PASS guarded metadata snapshot: "
            f"domains={observation['counts']['domains']} "
            f"exports={observation['counts']['public_all_symbols']} "
            f"ContractModel_exports={observation['counts']['contract_model_exports']} "
            f"provider_descriptors={observation['counts']['provider_descriptors']} "
            f"model_legs={observation['counts']['provider_model_legs']} "
            f"fixed_intersection={observation['counts']['fixed_bound_intersection']}"
        )
    else:
        receipt["stdout"] = "FAIL guarded metadata snapshot; inspect error and guard assertions"
    (RUN_DIR / "run.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (RUN_DIR / "run.log").write_text(
        f"run_id={RUN_ID}\ncommand={command}\nexit_code={exit_code}\n"
        f"status={status}\nstarted_at_utc={started_at}\nfinished_at_utc={finished_at}\n"
        f"stdout={receipt['stdout']}\n",
        encoding="utf-8",
    )
    print(json.dumps({"run_id": RUN_ID, "status": status, "exit_code": exit_code}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
