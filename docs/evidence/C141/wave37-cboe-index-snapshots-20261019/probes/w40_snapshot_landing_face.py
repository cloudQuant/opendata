"""Landing face for cboe::IndexSnapshots: every claim of this wave, re-measured in one run.

The objective forbids verbal judgments. This wave asserts four things, and each is read from an
instrument rather than stated:

``A_judge_row``
    the shipped plane-1 judge run in-process over the post-chain merged claims, reporting the
    per-identity status of ``cboe::IndexSnapshots`` (not only the tree total).
``B_refusal_ledger``
    cboe's own ``NOT_DECLARABLE`` dict, read live, plus an in-memory injection that must move the
    count. A ledger arm that cannot shift is not measuring.
``C_capture_backing``
    whether the endpoint this row now reads is present in the shipped receipt index, computed with
    the *sibling wave's* own predicates (``w38_artifact_request_face._endpoint_key`` and
    ``._receipt_endpoints``), plus a forged-empty control.
``D_guard_two_sided``
    the adapter's numeric guard run both ways on the fixture panel: clean body yields rows, the
    boolean price refuses with the code the witness names.
``E_contract_comparator``
    the column set resolved by AST from the reference checkout (standard model plus the provider
    model), compared against ``CboeIndexSnapshots.model_fields``; the three columns the envelope
    does not publish are named, not assumed.
``F_phase_receipts``
    the five phase receipts for this row read from disk -- exit code and status per phase -- against
    a control row that has no receipts at all.
``G_generation``
    identity set, claims digest and witness-shard digest, so the face cannot be quoted against a
    tree it did not measure.
``H_census_bucket``
    the shipped residue census's own action bucket for this row, read through the sibling wave's
    parser (the census exits 1 on a consumed negative control; its ``row`` lines are still a
    reading, and that death is printed rather than hidden).

Nothing outside this bundle is written; every forged object lives in memory or under a temp dir.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C141/wave37-cboe-index-snapshots-20261019"
CARRIER = REPO / BUNDLE / "runs" / "w40_snapshot_landing_face.txt"
SIBLING_BUNDLE = REPO / "docs/evidence/C139/wave34-tmxyfinance-rest-20261018"
ARTIFACT_FACE = SIBLING_BUNDLE / "probes" / "w38_artifact_request_face.py"
REFUSAL_FACE = SIBLING_BUNDLE / "probes" / "w35_refusal_evidence_face.py"
CLAIMS = REPO / "docs/evidence/C101/claims.merged.json"
INVENTORY = REPO / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
JUDGE_OUT = REPO / "docs/evidence/C101/judge/claims.merged.report.json"
OPENBB_CORE = Path("/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/core")
CBOE_REF = Path(
    "/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers/cboe/openbb_cboe/models"
)
CAPTURE = (
    REPO
    / "docs/evidence/C140/wave37-unauthenticated-access-20261019/sources/cboe-all-us-indices.json"
)
C140_DIR = "docs/evidence/C140/wave37-unauthenticated-access-20261019"
C140_RECEIPT = REPO / f"{C140_DIR}/capture-receipt.json"
RECEIPT_DIR = REPO / "docs/evidence/C101/cboe/receipts/cboe/IndexSnapshots"
CONTROL_RECEIPT_DIR = REPO / "docs/evidence/C101/cboe/receipts/cboe/FuturesCurve"
STATUS_LINE_RE = re.compile(r"^HTTP/(?P<version>[0-9.]+)[ \t]+(?P<code>[1-5][0-9]{2})\b")


def _load(name: str, path: Path) -> Any:  # noqa: ANN401  # shipped probes are heterogeneous
    """Import one shipped probe by location without running its main()."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import shipped probe {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _annotated_fields(path: Path, class_name: str) -> list[str]:
    """Annotated assignment names inside one class body of a reference module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return [
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            ]
    return []


def _judge_face() -> dict[str, Any]:
    """Run the shipped judge in-process and read this row's own status out of its report."""
    sys.path.insert(0, str(REPO))
    from scripts.quality import iter2_model_evidence as judge

    inventory = judge.load_inventory(INVENTORY)
    document = json.loads(CLAIMS.read_text(encoding="utf-8"))
    report = judge.build_report(
        inventory,
        document,
        claims_sha256=hashlib.sha256(CLAIMS.read_bytes()).hexdigest(),
        claims_path=str(CLAIMS.relative_to(REPO)),
    )
    row: dict[str, Any] = next(
        (
            dict(model)
            for model in report["models"]
            if (model["provider"], model["upstream_model"]) == ("cboe", "IndexSnapshots")
        ),
        {},
    )
    review = row.get("primary_review") or {}
    return {
        "accepted": report["summary"]["offline_acceptance_count"],
        "identities": report["summary"]["identity_count"],
        "claims": report["summary"]["claim_count"],
        "status_counts": report["summary"]["status_counts"],
        "identity_set": report["inventory"]["identity_set_sha256"][:16],
        "row_status": row.get("status"),
        "row_phases": sorted(row.get("phases", {})),
        "row_missing_phases": row.get("missing_phases"),
        "row_issues": row.get("issues"),
        "row_review_status": review.get("status") or review.get("decision"),
        "row_review_keys": sorted(review),
    }


def main() -> int:
    """Print the face; every arm can fail."""
    lines: list[str] = []
    arms: dict[str, bool] = {}

    judge_face = _judge_face()
    lines.append(
        f"A_judge_row accepted={judge_face['accepted']} identities={judge_face['identities']} "
        f"claims={judge_face['claims']} row_status={judge_face['row_status']} "
        f"row_phases={judge_face['row_phases']} row_missing={judge_face['row_missing_phases']} "
        f"row_review={judge_face['row_review_status']} row_issues={judge_face['row_issues']} "
        f"status_counts={judge_face['status_counts']}"
    )
    arms["A_judge_row"] = (
        judge_face["identities"] == 350
        and judge_face["identity_set"] == "5168e1e723999312"
        and judge_face["row_status"] == "OFFLINE_ACCEPTED"
        and judge_face["row_phases"]
        == [
            "contract",
            "counterfact",
            "direct",
            "implementation",
            "service",
        ]
        and judge_face["row_missing_phases"] == []
        and judge_face["row_issues"] == []
        and judge_face["accepted"] > 0
        and judge_face["accepted"] == judge_face["status_counts"].get("OFFLINE_ACCEPTED", -1)
    )

    from opendata.data.providers.cboe import specs as cboe_specs

    live = dict(cboe_specs.NOT_DECLARABLE)
    forged = copy.deepcopy(live)
    forged["W40SyntheticRefusal"] = "in-memory injection, never written to disk"
    lines.append(
        f"B_refusal_ledger live_count={len(live)} live_names={sorted(live)} "
        f"IndexSnapshots_absent={'IndexSnapshots' not in live} "
        f"injection_moves_count={len(forged) != len(live)} forged_count={len(forged)}"
    )
    arms["B_refusal_ledger"] = (
        len(live) == 1
        and set(live) == {"FuturesCurve"}
        and "IndexSnapshots" not in live
        and len(forged) == len(live) + 1
    )

    art = _load("w38_artifact_sibling", ARTIFACT_FACE)
    from opendata.data.providers.cboe.models.index_snapshots_public import SNAPSHOT_URL

    index = art._receipt_endpoints()
    key = art._endpoint_key(SNAPSHOT_URL)
    named_by = index.get(key, "")
    foreign = art._endpoint_key("https://w40-control.invalid/api/no-such-capture.json")
    # ``_endpoint_key`` is host|first-segment, so it cannot separate two documents on the same host
    # and prefix. The document-level claim is made against the receipt's own recorded URL instead.
    same_host = art._endpoint_key(
        "https://cdn.cboe.com/api/global/delayed_quotes/quotes/__w40_not_captured__.json"
    )
    receipt = json.loads(C140_RECEIPT.read_text(encoding="utf-8"))
    entries = [
        cap
        for cap in (receipt.get("source_captures") or [])
        if isinstance(cap, dict) and str(cap.get("url")) == SNAPSHOT_URL
    ]
    # One .headers file carries the whole redirect chain, so every status line is read and the
    # terminal one is the answer the adapter sees; taking the first line would read the 307.
    status_lines: list[str] = []
    body_matches: list[bool] = []
    for cap in entries:
        header_path = C140_RECEIPT.parent / str(cap.get("headers_file") or "")
        body_path = C140_RECEIPT.parent / str(cap.get("body_file") or "")
        if header_path.is_file():
            text = header_path.read_text("utf-8", errors="replace")
            status_lines += [line for line in text.splitlines() if STATUS_LINE_RE.match(line)]
        if body_path.is_file():
            body_matches.append(
                hashlib.sha256(body_path.read_bytes()).hexdigest() == str(cap.get("body_sha256"))
                and body_path.resolve() == CAPTURE.resolve()
            )
    receipt_http = [str(cap.get("http")) for cap in entries]
    lines.append(
        f"C_capture_backing endpoint_key={key} index_named_by={named_by} "
        f"index_size={len(index)} receipt_url_exact_matches={len(entries)} "
        f"receipt_http={receipt_http} status_line_chain={status_lines} "
        f"body_digest_and_path_match={body_matches} body_bytes={len(CAPTURE.read_bytes())} "
        f"same_host_coarser_key={same_host} same_host_also_present={same_host in index} "
        f"foreign_control_absent={foreign not in index}"
    )
    arms["C_capture_backing"] = (
        bool(named_by)
        and "C140" in named_by
        and len(entries) == 1
        and receipt_http == ["200"]
        and len(status_lines) >= 2
        and status_lines[-1].startswith("HTTP/2 200")
        and body_matches == [True]
        and same_host in index
        and foreign not in index
    )

    from opendata.data.models.cboe_index_snapshots import CboeIndexSnapshots
    from opendata.data.providers.cboe.models.index_snapshots_public import (
        CboeIndexSnapshotsError,
        _parse_panel,
    )

    panel = CAPTURE.read_bytes()
    clean_rows = _parse_panel(panel, symbols=(), limit=None)
    tampered_document = json.loads(panel)
    tampered_document["data"][0]["current_price"] = True
    tamper_code = ""
    try:
        _parse_panel(json.dumps(tampered_document).encode(), symbols=(), limit=None)
    except CboeIndexSnapshotsError as exc:
        tamper_code = exc.code
    selected = _parse_panel(panel, symbols=("VIX",), limit=None)
    lines.append(
        f"D_guard_two_sided clean_rows={len(clean_rows)} "
        f"tampered_refused_code={tamper_code or '-'} selected_rows={len(selected)} "
        f"selected_symbol={selected[0]['symbol'] if selected else '-'}"
    )
    arms["D_guard_two_sided"] = (
        len(clean_rows) > 1
        and tamper_code == "CBOE_INDEX_SNAPSHOTS_BAD_NUMERIC_FIELD"
        and len(selected) == 1
    )

    standard = _annotated_fields(
        OPENBB_CORE / "openbb_core/provider/standard_models/index_snapshots.py",
        "IndexSnapshotsData",
    )
    provider_ref = _annotated_fields(CBOE_REF / "index_snapshots.py", "CboeIndexSnapshotsData")
    reference = sorted({*standard, *provider_ref})
    landed = sorted(CboeIndexSnapshots.model_fields)
    unfilled = sorted(
        set(landed)
        - {
            str(cell)
            for row in _parse_panel(CAPTURE.read_bytes(), symbols=("VIX",), limit=None)
            for cell in row
        }
    )
    lines.append(
        f"E_contract_comparator reference_own={len(provider_ref)} reference_base={len(standard)} "
        f"reference_union={len(reference)} landed_columns={len(landed)} "
        f"landed_minus_reference={sorted(set(landed) - set(reference))} "
        f"reference_minus_landed={sorted(set(reference) - set(landed))} "
        f"never_filled_by_fetcher={unfilled}"
    )
    arms["E_contract_comparator"] = (
        len(reference) >= 12 and landed == reference and unfilled == ["currency", "name", "status"]
    )

    phases = ("implementation", "direct", "service", "contract", "counterfact")
    readings: dict[str, str] = {}
    for phase in phases:
        path = RECEIPT_DIR / f"{phase}.json"
        if not path.is_file():
            readings[phase] = "MISSING"
            continue
        receipt_document = json.loads(path.read_text(encoding="utf-8"))
        machine = receipt_document.get("machine_execution") or {}
        readings[phase] = f"exit={machine.get('exit_code')} status={receipt_document.get('status')}"
    control_files = (
        sorted(CONTROL_RECEIPT_DIR.glob("*.json")) if CONTROL_RECEIPT_DIR.is_dir() else []
    )
    counterfact_path = RECEIPT_DIR / "counterfact.json"
    counterfact = json.loads(counterfact_path.read_text(encoding="utf-8")).get("counterfact") or {}
    expected = counterfact.get("expected") or {}
    observed = counterfact.get("observed") or {}
    control_note = "untampered control produced" in str(counterfact.get("reason", ""))
    lines.append(
        f"F_phase_receipts present={sum(1 for v in readings.values() if v != 'MISSING')} "
        f"of={len(phases)} readings={readings} control_row_receipts={len(control_files)} "
        f"refusal_class={expected.get('refusal_class')} "
        f"refusal_code={expected.get('refusal_code')} "
        f"expected_equals_observed={expected == observed} "
        f"control_note_on_disk={control_note}"
    )
    arms["F_phase_receipts"] = (
        all(v != "MISSING" for v in readings.values())
        and all(v.startswith("exit=0 status=PASS") for v in readings.values())
        and len(control_files) == 0
        and expected.get("refusal_code") == "CBOE_INDEX_SNAPSHOTS_BAD_NUMERIC_FIELD"
        and expected.get("refusal_class") == "CboeIndexSnapshotsError"
        and expected == observed
        and control_note
    )

    witness = REPO / "scripts/quality/iter2_witnesses/_parts/cboe/snapshots.py"
    claims_sha = hashlib.sha256(CLAIMS.read_bytes()).hexdigest()[:16]
    judge_sha = (
        hashlib.sha256(JUDGE_OUT.read_bytes()).hexdigest()[:16] if JUDGE_OUT.is_file() else ""
    )
    lines.append(
        f"G_generation identity_set={judge_face['identity_set']} claims_sha256={claims_sha} "
        f"judge_report_sha256={judge_sha} witness_sha256="
        f"{hashlib.sha256(witness.read_bytes()).hexdigest()[:16]} capture_sha256="
        f"{hashlib.sha256(panel).hexdigest()[:16]} "
        f"snapshot_url_host={urlparse(SNAPSHOT_URL).netloc}"
    )
    arms["G_generation"] = (
        judge_face["identity_set"] == "5168e1e723999312"
        and len(claims_sha) == 16
        and witness.is_file()
        and CAPTURE.is_file()
    )

    census_note = "sibling face not importable"
    census_bucket = ""
    census_exit: Any = None
    try:
        sib = _load("w35_refusal_sibling", REFUSAL_FACE)
        census = sib._census_face()
        census_exit = census.get("exit")
        rows = {
            r["identity"]: r.get("action", "") for r in census.get("rows", []) if r.get("identity")
        }
        census_bucket = rows.get("cboe::IndexSnapshots", "ABSENT_FROM_CENSUS_ROWS")
        census_note = (
            f"residue={census.get('residue')} actions={census.get('actions')} "
            f"red_arms={census.get('red_arms')}"
        )
    except Exception as exc:  # a dead sibling instrument is a reading, not a crash
        census_note = f"census_face_unavailable {type(exc).__name__}"
    landed_absent = "IndexSnapshots" not in str(census_bucket) or census_bucket in {
        "implement_missing_model",
        "landed",
        "OFFLINE_ACCEPTED",
        "",
        "ABSENT_FROM_CENSUS_ROWS",
    }
    lines.append(
        f"H_census_bucket row=cboe::IndexSnapshots action={census_bucket or '-'} "
        f"census_exit={census_exit} detail={census_note} "
        f"ruled_out_bucket_absent_for_this_row={landed_absent}"
    )
    arms["H_census_bucket"] = bool(census_note) and census_bucket != "ledger_ruled_out"

    failed = [name for name, ok in sorted(arms.items()) if not ok]
    lines.append(
        f"snapshot_landing_face arms={len(arms)} arms_passed={sum(arms.values())} "
        f"arms_failed={len(failed)} failed={failed} PASS={not failed}"
    )
    lines.append("")
    lines.append(f"w40_snapshot_landing_face_exit={0 if not failed else 1}")
    body = "\n".join(lines) + "\n"
    CARRIER.parent.mkdir(parents=True, exist_ok=True)
    CARRIER.write_text(
        "# carrier for w40_snapshot_landing_face.py\n"
        f"# argv: {sys.executable} {REPO / BUNDLE / 'probes' / 'w40_snapshot_landing_face.py'}\n"
        f"# claims_sha256={claims_sha} identity_set={judge_face['identity_set']}\n"
        f"# reference checkout: {OPENBB_CORE.parent} (output-contract comparator only)\n\n" + body,
        encoding="utf-8",
    )
    print(body, end="")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
