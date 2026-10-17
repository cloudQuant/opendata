"""Falsification probe for the plane-1 reading that wave 22 produces.

Four arms, each printing one verdict line. Run from the repository root with the gate
interpreter and ``PYTHONPATH=.``::

    python docs/evidence/C127/intrinio-forward-witness-20261017/probes/plane1_delta_row_model.py

* ``A_delta_census`` -- the judge report on disk against ``HEAD``'s report, joined on
  ``model_identity`` in *both* directions. The claim is a set claim over the 350-identity
  inventory, so a +2 that arrived with a label flip elsewhere is exactly what ``REMOVED`` would
  show, and the freeze signal ``inventory.expected_identity_set_sha256`` is compared between the
  two generations.
* ``B_pin_census`` -- the financials rows must pin the shard as it sits on disk. The gated
  population is the shard's own rows (five claims per declared row, ``len(MODELS)`` read from the
  shard module the same way ``iter2_claims_fixtures`` loads it, never typed here); the
  provider-wide fan-out is printed beside it because the producer pins *every* part of a provider
  on *every* claim of that provider, so this one file is named by 185 intrinio claims and an edit
  to it stales the whole provider, not only the rows it declares.
* ``C_phase_floor`` -- the +2 must be earned by five phase claims plus one review *per row*, not
  by the judge's display arithmetic. The repo's own judge runs as a subprocess on six carriers:
  the control, each row without its claims, each row without its review, and one with both rows'
  claims dropped (the wave's own pre-state reproduced from post-wave bytes).
* ``D_row_model_identity`` -- the provider-local change this wave made is a *name*, so the arm
  checks the name against the served object twice: ``contract_model(domain) is fetcher.row_model``
  and ``type(row) is row_model`` on rows produced through the declared seam. Then it re-arms the
  refusal in memory -- Sales' pre-wave name (the wire class ``_SalesEstimateRow``) and Ebitda's
  pre-wave absence -- and requires the witness instrument to refuse each, then restores and proves
  the same leg green again. Nothing on disk is touched by this arm.

``--skip-judge`` exists for a fast re-read of A/B/D only; arm C is what makes the 260 a reading.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess  # nosec B404  # one run(): list argv only, shell never enabled
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[5]
SCRIPTS_QUALITY = REPO_ROOT / "scripts" / "quality"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPTS_QUALITY))

import iter2_claims_fixtures as witnesses  # noqa: E402  # the repo's own shard loader
import iter2_claims_probe_runner as runner  # noqa: E402  # the witness instrument itself

from opendata.data.domains import contract_model  # noqa: E402
from opendata.data.models.base import ContractModel  # noqa: E402
from opendata.data.providers.intrinio.models import iter2_financials  # noqa: E402

JUDGE: Final[str] = "scripts/quality/iter2_model_evidence.py"
INVENTORY: Final[str] = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
MERGED: Final[str] = "docs/evidence/C101/claims.merged.json"
REPORT: Final[str] = "docs/evidence/C101/judge/claims.merged.report.json"
SHARD: Final[str] = "scripts/quality/iter2_witnesses/_parts/intrinio/financials.py"
PROVIDER: Final[str] = "intrinio"
OFFLINE_ACCEPTED: Final[str] = "OFFLINE_ACCEPTED"
PHASES: Final[tuple[str, ...]] = runner.PHASES

#: The two rows this wave lands, each with the domain it is registered under and the wire-parse
#: class the provider *used* to publish (or, for Ebitda, the class it published nothing about).
WAVE_ROWS: Final[dict[str, dict[str, str]]] = {
    "ForwardSalesEstimates": {
        "domain": "intrinio_forward_sales_estimates",
        "wire_class": "_SalesEstimateRow",
    },
    "ForwardEbitdaEstimates": {
        "domain": "intrinio_forward_ebitda_estimates",
        "wire_class": "_EbitdaEstimateRow",
    },
}
IDENTITIES: Final[list[str]] = [f"{PROVIDER}::{m}" for m in sorted(WAVE_ROWS)]


def _git_head_json(relative: str) -> dict[str, Any]:
    """Read one JSON file exactly as it exists in HEAD, not as it exists in my memory."""
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not on PATH; this arm reads HEAD rather than a hand-typed copy")
    raw = subprocess.run(  # noqa: S603  # nosec B603  # argv is this probe's own git read
        [git, "show", f"HEAD:{relative}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result: dict[str, Any] = json.loads(raw)
    return result


def _status_map(report: dict[str, Any]) -> dict[str, str]:
    return {str(model["model_identity"]): str(model.get("status")) for model in report["models"]}


def _claims(document: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = document["claims"]
    return result


def _is_row(claim: dict[str, Any], model: str) -> bool:
    return claim.get("provider") == PROVIDER and claim.get("upstream_model") == model


def _verdict(arm: str, note: str, ok: bool) -> bool:
    print(f"{arm}: {'PASS' if ok else 'FAIL'} {note}")
    return ok


def _shard_models() -> set[str]:
    """Which rows the financials shard declares, read the way the loader reads it.

    The expectation is derived from the module rather than typed here, so adding an eleventh row
    moves this arm's denominators instead of silently going red on an old number.
    """
    spec = importlib.util.spec_from_file_location("_w22_financials_part", REPO_ROOT / SHARD)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the shard module at {SHARD}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    models: dict[str, Any] = module.MODELS
    return set(models)


def arm_delta_census() -> bool:
    """Compare this generation's judge report with HEAD's, in both directions."""
    new = json.loads((REPO_ROOT / REPORT).read_text(encoding="utf-8"))
    old = _git_head_json(REPORT)
    new_map, old_map = _status_map(new), _status_map(old)
    added = sorted(
        identity
        for identity, status in new_map.items()
        if status == OFFLINE_ACCEPTED and old_map.get(identity) != OFFLINE_ACCEPTED
    )
    removed = sorted(
        identity
        for identity, status in old_map.items()
        if status == OFFLINE_ACCEPTED and new_map.get(identity) != OFFLINE_ACCEPTED
    )
    n_new = int(new["summary"]["offline_acceptance_count"])
    n_old = int(old["summary"]["offline_acceptance_count"])
    set_new = str(new["inventory"]["expected_identity_set_sha256"])
    set_old = str(old["inventory"]["expected_identity_set_sha256"])
    ok = (
        added == IDENTITIES
        and removed == []
        and n_new == n_old + len(IDENTITIES)
        and len(new_map) == len(old_map) == 350
        and set_new == set_old
        and all(new_map.get(identity) == OFFLINE_ACCEPTED for identity in IDENTITIES)
        and all(old_map.get(identity) != OFFLINE_ACCEPTED for identity in IDENTITIES)
    )
    return _verdict(
        "A_delta_census",
        f"accepted_old={n_old} accepted_new={n_new} ADDED={added} REMOVED={removed}"
        f" populations={len(old_map)}/{len(new_map)} identity_set={set_new[:16]}"
        f" unchanged={set_new == set_old} old_statuses="
        f"{[old_map.get(i) for i in IDENTITIES]}",
        ok,
    )


def arm_pin_census() -> bool:
    """The shard on disk must be the bytes every financials-row claim pins.

    The producer pins *all* of a provider's shard parts on *every* claim of that provider, so the
    provider-wide fan-out (185 intrinio claims name this file) is printed as a denominator, not
    gated: the gated population is the shard's own rows, five claims each.
    """
    claims = _claims(json.loads((REPO_ROOT / MERGED).read_text(encoding="utf-8")))
    provider_claims = [claim for claim in claims if claim.get("provider") == PROVIDER]
    shard_models = _shard_models()

    def pins_shard(claim: dict[str, Any]) -> bool:
        return any(entry["path"] == SHARD for entry in (claim["pins"] or {}).get("probe", []))

    row_claims = [claim for claim in provider_claims if claim["upstream_model"] in shard_models]
    pinned_rows = [claim for claim in row_claims if pins_shard(claim)]
    all_pinned = [claim for claim in provider_claims if pins_shard(claim)]
    digests = {
        str(entry["after_sha256"])
        for claim in all_pinned
        for entry in (claim["pins"] or {}).get("probe", [])
        if entry["path"] == SHARD
    }
    on_disk = hashlib.sha256((REPO_ROOT / SHARD).read_bytes()).hexdigest()
    expected_row_claims = 5 * len(shard_models)
    statuses = sorted({str(claim["status"]) for claim in row_claims})
    not_pass = sorted(
        f"{claim['upstream_model']}/{claim['phase']}"
        for claim in row_claims
        if str(claim["status"]) != "PASS"
    )
    ok = (
        len(row_claims) == expected_row_claims
        and len(pinned_rows) == expected_row_claims
        and len(digests) == 1
        and digests == {on_disk}
        and statuses == ["PASS"]
        and not_pass == []
    )
    return _verdict(
        "B_pin_census",
        f"shard_declared_rows={len(shard_models)} shard_row_claims={len(row_claims)}"
        f" expected={expected_row_claims} row_claims_pinning_shard={len(pinned_rows)}"
        f" provider_claims_pinning_shard={len(all_pinned)} (fan-out, not gated)"
        f" distinct_shard_digests={len(digests)} on_disk_sha256={on_disk[:16]}"
        f" digest_matches_disk={digests == {on_disk}} statuses={statuses}"
        f" non_pass={not_pass} intrinio_claims_total={len(provider_claims)}",
        ok,
    )


def _run_judge(carrier: dict[str, Any], label: str, scratch: Path) -> dict[str, int]:
    claims_path = scratch / f"claims.{label}.json"
    report_path = scratch / f"report.{label}.json"
    claims_path.write_text(
        json.dumps(carrier, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    subprocess.run(  # noqa: S603  # nosec B603  # argv is this probe's own judge call
        [
            sys.executable,
            JUDGE,
            "--inventory",
            INVENTORY,
            "--claims",
            str(claims_path),
            "--output",
            str(report_path),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    accepted_map = _status_map(report)
    return {
        "accepted": int(report["summary"]["offline_acceptance_count"]),
        "reviews": int(report["summary"]["primary_review_count"]),
        "population": int(report["summary"]["identity_count"]),
        "wave_accepted": sum(
            1 for identity in IDENTITIES if accepted_map.get(identity) == OFFLINE_ACCEPTED
        ),
    }


def arm_phase_floor(scratch: Path) -> bool:
    """Re-run the repo's own judge on six carriers; each row needs claims AND a review."""
    document = json.loads((REPO_ROOT / MERGED).read_text(encoding="utf-8"))
    control_count = int(
        json.loads((REPO_ROOT / REPORT).read_text(encoding="utf-8"))["summary"][
            "offline_acceptance_count"
        ]
    )
    floors: dict[str, tuple[int, int]] = {}
    for model in WAVE_ROWS:
        n_claims = sum(1 for claim in document["claims"] if _is_row(claim, model))
        n_reviews = sum(1 for review in document["primary_reviews"] if _is_row(review, model))
        floors[model] = (n_claims, n_reviews)
    if not all(counts == (5, 1) for counts in floors.values()):
        return _verdict(
            "C_phase_floor",
            f"expected 5 claims and 1 review per row, found {floors}",
            False,
        )

    def drop_claims(models: list[str]) -> dict[str, Any]:
        return {
            **document,
            "claims": [
                claim
                for claim in copy.deepcopy(document["claims"])
                if not (claim.get("provider") == PROVIDER and claim["upstream_model"] in models)
            ],
        }

    def drop_reviews(models: list[str]) -> dict[str, Any]:
        return {
            **document,
            "primary_reviews": [
                review
                for review in copy.deepcopy(document["primary_reviews"])
                if not (review.get("provider") == PROVIDER and review["upstream_model"] in models)
            ],
        }

    variants: dict[str, tuple[dict[str, Any], int]] = {
        "control": (document, control_count),
        "sales_no_claims": (drop_claims(["ForwardSalesEstimates"]), control_count - 1),
        "sales_no_review": (drop_reviews(["ForwardSalesEstimates"]), control_count - 1),
        "ebitda_no_claims": (drop_claims(["ForwardEbitdaEstimates"]), control_count - 1),
        "ebitda_no_review": (drop_reviews(["ForwardEbitdaEstimates"]), control_count - 1),
        "both_no_claims": (drop_claims(list(WAVE_ROWS)), control_count - 2),
    }
    scratch.mkdir(parents=True, exist_ok=True)
    readings: dict[str, dict[str, int]] = {}
    for label, (carrier, _expected) in variants.items():
        readings[label] = _run_judge(carrier, label, scratch)
    for path in list(scratch.glob("claims.*.json")) + list(scratch.glob("report.*.json")):
        path.unlink()
    ok = True
    detail: list[str] = []
    for label, (_carrier, expected) in variants.items():
        got = readings[label]
        detail.append(
            f"{label}: accepted={got['accepted']} want={expected}"
            f" wave_rows={got['wave_accepted']} reviews={got['reviews']}"
            f" population={got['population']}"
        )
        ok = ok and got["accepted"] == expected and got["population"] == 350
    ok = (
        ok
        and readings["control"]["wave_accepted"] == 2
        and readings["sales_no_claims"]["wave_accepted"] == 1
        and readings["sales_no_review"]["wave_accepted"] == 1
        and readings["ebitda_no_claims"]["wave_accepted"] == 1
        and readings["ebitda_no_review"]["wave_accepted"] == 1
        and readings["both_no_claims"]["wave_accepted"] == 0
        and readings["sales_no_claims"]["reviews"] == readings["control"]["reviews"]
        and readings["sales_no_review"]["reviews"] == readings["control"]["reviews"] - 1
    )
    return _verdict("C_phase_floor", " | ".join(detail), ok)


def _entry_for(model: str, evidence_root: str) -> dict[str, Any]:
    config = witnesses.provider_config(PROVIDER)
    entry = dict(witnesses.model_entry(PROVIDER, model))
    entry["hosts"] = config["allowed_hosts"]
    for name, value in config.get("credential_env", {}).items():
        if not os.environ.get(name):
            os.environ[name] = value
    entry["document"] = runner._fixture_document(REPO_ROOT / evidence_root, PROVIDER, model)
    return entry


def arm_row_model_identity(evidence_root: str) -> bool:
    """The declared name must be the class the serving path emits, and the old name must fail."""
    detail: list[str] = []
    ok = True
    for model, spec in sorted(WAVE_ROWS.items()):
        entry = _entry_for(model, evidence_root)
        fetcher_type = runner._resolve_fetcher_type(PROVIDER, model)
        contract = contract_model(spec["domain"])
        wire = getattr(iter2_financials, spec["wire_class"])
        original = fetcher_type.row_model
        checks: list[str] = []
        same_as_contract = original is contract
        issubclass_contract = isinstance(original, type) and issubclass(original, ContractModel)
        rows, _, _ = runner._run_direct_with_seam(PROVIDER, model, entry, entry["document"])
        served_is_named = bool(rows) and all(type(row) is original for row in rows)
        refused: dict[str, str] = {}
        try:
            fetcher_type.row_model = wire
            try:
                runner._run_direct_with_seam(PROVIDER, model, entry, entry["document"])
            except runner.WitnessFailure as failure:
                refused["wire_name"] = str(failure)[:60]
            else:
                refused["wire_name"] = "NOT-REFUSED"
        finally:
            fetcher_type.row_model = original
        try:
            saved = fetcher_type.row_model
            fetcher_type.row_model = None  # type: ignore[assignment]
            try:
                runner.witness_implementation(PROVIDER, model, entry)
            except runner.WitnessFailure as failure:
                refused["absent_name"] = str(failure)[:60]
            else:
                refused["absent_name"] = "NOT-REFUSED"
            finally:
                fetcher_type.row_model = saved
        except Exception as exc:  # the arm reports an unclassified refusal, never re-raises
            refused["absent_name"] = f"UNCLASSIFIED {type(exc).__name__}"
        rows_after, _, _ = runner._run_direct_with_seam(PROVIDER, model, entry, entry["document"])
        green_after = bool(rows_after) and all(type(row) is original for row in rows_after)
        checks.append(f"rows={len(rows)}/{len(rows_after)}")
        row_ok = (
            same_as_contract
            and issubclass_contract
            and served_is_named
            and green_after
            and "NOT-REFUSED" not in refused.values()
            and set(refused) == {"wire_name", "absent_name"}
        )
        ok = ok and row_ok
        detail.append(
            f"{model}: row_model={original.__name__} is_contract_model={same_as_contract}"
            f" served_is_named={served_is_named} rearmed_refusals={refused}"
            f" green_after_restore={green_after} {' '.join(checks)}"
        )
    return _verdict("D_row_model_identity", " | ".join(detail), ok)


def main(argv: list[str] | None = None) -> int:
    """Run the arms and exit 0 only when every selected one passes."""
    parser = argparse.ArgumentParser(description="plane-1 delta, pin, floor and row-name census")
    parser.add_argument(
        "--scratch-dir",
        default=".w22-scratch/counterfact",
        help="where arm C writes its temporary claims and report files",
    )
    parser.add_argument("--evidence-root", default="docs/evidence/C101")
    parser.add_argument("--skip-judge", action="store_true", help="run A, B and D only")
    args = parser.parse_args(argv)
    runner._block_real_sockets()  # every leg stays offline, in this process too
    scratch = REPO_ROOT / args.scratch_dir
    results = [arm_delta_census(), arm_pin_census()]
    if not args.skip_judge:
        results.append(arm_phase_floor(scratch))
        if scratch.is_dir():
            shutil.rmtree(scratch)
    results.append(arm_row_model_identity(args.evidence_root))
    passed = sum(1 for verdict in results if verdict)
    print(
        f"plane1_arms={len(results)} plane1_passed={passed} plane1_failed={len(results) - passed}"
    )
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
