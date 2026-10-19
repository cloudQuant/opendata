"""Landing face for seeking_alpha::CalendarEarnings -- every wave-38 claim, re-measured.

Nine arms, each read from an instrument and each able to fail:

``A_judge_row``
    the shipped plane-1 judge, imported and run in-process over the post-chain merged claims, giving
    this identity's own status, phase set, missing phases and issues -- and the status of the two
    refused rows, which must NOT be accepted.
``B_refusal_partition``
    the provider's own ledger: what it refuses, what the descriptor binds, and that the two
    together account for exactly the three inventory models. An in-memory injection must move
    the count.
``C_capture_backing``
    wave 38's receipt fields for the exact URL the adapter calls: status lines, body digests
    against the bytes on disk, and the page-closure arithmetic 50 + 6 == 56.
``D_guard_two_sided``
    the envelope guards run both ways on the captured pages.
``E_pagination_follows_envelope``
    the page cursor is driven by ``meta``/``links`` and not by a constant: serving both captured
    pages gives 56 rows in 2 requests with ``page[number]`` on the second; relabelling page 1 as the
    only page gives 50 rows in 1 request. An adapter hardcoded to one request per day would read
    the same on the second experiment -- that is the failure this arm excludes.
``F_contract_comparator``
    the reference checkout's declared column set, resolved by AST, against the landed model; the two
    consensus columns the envelope never publishes are named from the adapter's own output.
``G_phase_receipts``
    this row's five phase receipts and the refused row's counterfact receipt -- the latter must
    exist and read BLOCKED, so a refusal is recorded rather than hidden.
``H_census_bucket``
    the shipped residue census's own per-row action lines.
``I_authority_partition``
    the repo's own authority test predicate re-run in-process: every live domain absent from the
    authority table must be named by the exemption or native-model list. This is the arm wave 37
    missed -- ``cboe_index_snapshots`` had been landed without being listed -- and it now covers
    both waves' domains.
``J_generation``
    identity set, claims digest, witness digest, capture digests, and the implemented-source count.
"""

from __future__ import annotations

import ast
import copy
import csv
import datetime
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C143/wave38-seeking-alpha-landing-20261019"
CARRIER = REPO / BUNDLE / "runs" / "w48_calendar_landing_face.txt"
C142 = REPO / "docs/evidence/C142/wave38-seeking-alpha-calendar-20261019"
RECEIPT = C142 / "capture-receipt.json"
REFUSAL_FACE = (
    REPO / "docs/evidence/C139/wave34-tmxyfinance-rest-20261018/probes/w35_refusal_evidence_face.py"
)
CLAIMS = REPO / "docs/evidence/C101/claims.merged.json"
INVENTORY = REPO / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
WITNESS = REPO / "scripts/quality/iter2_witnesses/seeking_alpha.py"
RECEIPTS = REPO / "docs/evidence/C101/seeking_alpha/receipts/seeking_alpha"
SA_REF = Path(
    "/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform"
    "/providers/seeking_alpha/openbb_seeking_alpha/models/calendar_earnings.py"
)
CORE_REF = Path(
    "/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/core"
    "/openbb_core/provider/standard_models/calendar_earnings.py"
)
PHASES = ("implementation", "direct", "service", "contract", "counterfact")
DAY = datetime.date(2026, 10, 20)


def _load(name: str, path: Path) -> Any:  # noqa: ANN401  # shipped probes are heterogeneous
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
    """Run the shipped judge in-process and read three identities' own statuses."""
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
    rows: dict[str, Any] = {}
    for model in report["models"]:
        rows[f"{model['provider']}::{model['upstream_model']}"] = dict(model)
    landed: dict[str, Any] = rows.get("seeking_alpha::CalendarEarnings", {})
    refused: dict[str, Any] = rows.get("seeking_alpha::ForwardEpsEstimates", {})
    review: dict[str, Any] = landed.get("primary_review") or {}
    return {
        "accepted": report["summary"]["offline_acceptance_count"],
        "identities": report["summary"]["identity_count"],
        "claims": report["summary"]["claim_count"],
        "status_counts": report["summary"]["status_counts"],
        "identity_set": report["inventory"]["identity_set_sha256"][:16],
        "row_status": landed.get("status"),
        "row_phases": sorted(landed.get("phases", {})),
        "row_missing": landed.get("missing_phases"),
        "row_issues": landed.get("issues"),
        "row_review": review.get("status") or review.get("decision"),
        "refused_status": refused.get("status"),
        "refused_issues": refused.get("issues"),
    }


class _Response:
    """The minimum surface the adapter reads from the governed client."""

    def __init__(self, body: bytes) -> None:
        self.content = body


class _Client:
    """Serve captured bodies in order and record the query parameters of every call."""

    def __init__(self, bodies: list[bytes]) -> None:
        self.bodies = bodies
        self.calls: list[dict[str, str]] = []

    def get(
        self,
        url: str,
        params: Any = None,  # noqa: ANN401  # the governed client's own signature
        headers: Any = None,  # noqa: ANN401
        timeout: Any = None,  # noqa: ANN401
        source: str = "unspecified",
        use_cache: bool = True,
    ) -> _Response:
        del url, headers, timeout, source, use_cache
        self.calls.append({str(key): str(value) for key, value in (params or {}).items()})
        if len(self.calls) > len(self.bodies):
            raise AssertionError("the adapter asked for a page the carrier does not declare")
        return _Response(self.bodies[len(self.calls) - 1])


def main() -> int:
    """Print the face; every arm can fail."""
    sys.path.insert(0, str(REPO))
    lines: list[str] = []
    arms: dict[str, bool] = {}

    judge_face = _judge_face()
    lines.append(
        f"A_judge_row accepted={judge_face['accepted']} identities={judge_face['identities']} "
        f"claims={judge_face['claims']} row_status={judge_face['row_status']} "
        f"row_phases={judge_face['row_phases']} row_missing={judge_face['row_missing']} "
        f"row_review={judge_face['row_review']} row_issues={judge_face['row_issues']} "
        f"refused_status={judge_face['refused_status']} status_counts={judge_face['status_counts']}"
    )
    arms["A_judge_row"] = (
        judge_face["identities"] == 350
        and judge_face["identity_set"] == "5168e1e723999312"
        and judge_face["row_status"] == "OFFLINE_ACCEPTED"
        and judge_face["row_phases"] == sorted(PHASES)
        and judge_face["row_missing"] == []
        and judge_face["row_issues"] == []
        and judge_face["row_review"] == "VALID"
        and judge_face["refused_status"] != "OFFLINE_ACCEPTED"
        and judge_face["accepted"] == judge_face["status_counts"].get("OFFLINE_ACCEPTED", -1)
        and judge_face["accepted"] > 0
    )

    from opendata.data.models.seeking_alpha import SeekingAlphaCalendarEarnings
    from opendata.data.providers.catalog import get_provider, list_providers
    from opendata.data.providers.seeking_alpha import specs as sa_specs
    from opendata.data.providers.seeking_alpha.models import calendar_earnings_public as adapter

    refused = dict(sa_specs.NOT_DECLARED)
    forged = copy.deepcopy(refused)
    forged["W48SyntheticRefusal"] = "in-memory injection, never written to disk"
    provider = get_provider("seeking_alpha")
    bound = {
        model_id
        for binding in provider.fetcher_bindings
        for model_id in binding.canonical_model_ids
    }
    # The population is read from the pinned inventory, not from the two sets the arm compares,
    # otherwise "bound and refused cover the inventory" is an identity that can only agree.
    with INVENTORY.open(newline="", encoding="utf-8") as handle:
        inventory_models = {
            str(row["upstream_model"])
            for row in csv.DictReader(handle)
            if row.get("provider") == "seeking_alpha"
        }
    implemented = [
        descriptor.source for descriptor in list_providers() if descriptor.is_implemented
    ]
    lines.append(
        f"B_refusal_partition refused={sorted(refused)} refused_count={len(refused)} "
        f"bound={sorted(bound)} overlap={sorted(bound & set(refused))} "
        f"union_is_inventory={bound | set(refused) == inventory_models} "
        f"inventory_models={len(inventory_models)} "
        f"injection_moves_count={len(forged) != len(refused)} forged_count={len(forged)}"
    )
    arms["B_refusal_partition"] = (
        len(refused) == 2
        and bound == {"CalendarEarnings"}
        and bound & set(refused) == set()
        and bound | set(refused) == inventory_models
        and len(forged) == len(refused) + 1
    )

    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    caps = {str(cap.get("url")): cap for cap in receipt.get("source_captures") or []}
    # The receipt keys every capture by the URL actually requested, query string included, so the
    # page-1 lookup is the date-filtered URL the adapter sends, not adapter.API_URL bare.
    p1_url = f"{adapter.API_URL}?filter%5Bselected_date%5D=2026-10-20"
    p1_cap = caps.get(p1_url) or {}
    p2_url = f"{adapter.API_URL}?filter%5Bselected_date%5D=2026-10-20&page%5Bnumber%5D=2"
    p2_cap = caps.get(p2_url) or {}
    literal_url = (
        "https://seekingalpha.com/earnings_calendar/tickers"
        "?filter[selected_date]=2026-10-20&page[number]=2"
    )
    literal_cap = caps.get(literal_url) or {}
    if not p1_cap or not p2_cap or not literal_cap:
        raise AssertionError(
            f"capture lookup failed p1={bool(p1_cap)} p2={bool(p2_cap)} "
            f"literal={bool(literal_cap)} receipt_urls={sorted(caps)}"
        )
    p1_path = C142 / str(p1_cap.get("body_file") or "")
    p2_path = C142 / str(p2_cap.get("body_file") or "")
    if not p1_path.is_file() or not p2_path.is_file():
        raise AssertionError(f"capture body missing p1={p1_path} p2={p2_path}")
    p1 = p1_path.read_bytes()
    p2 = p2_path.read_bytes()
    digests_ok = hashlib.sha256(p1).hexdigest() == str(
        p1_cap.get("body_sha256")
    ) and hashlib.sha256(p2).hexdigest() == str(p2_cap.get("body_sha256"))
    p1_doc = json.loads(p1) if p1 else {}
    p2_doc = json.loads(p2) if p2 else {}
    p1_meta = p1_doc.get("meta") or {}
    closure = len(p1_doc.get("data") or []) + len(p2_doc.get("data") or [])
    lines.append(
        f"C_capture_backing adapter_url_in_receipt={bool(p1_cap)} p2_url_in_receipt={bool(p2_cap)} "
        f"http_chain={[p1_cap.get('http'), p2_cap.get('http'), literal_cap.get('http')]} "
        f"body_digests_match={digests_ok} p1_records={len(p1_doc.get('data') or [])} "
        f"p2_records={len(p2_doc.get('data') or [])} closure={closure} "
        f"total_count={p1_meta.get('total_count')} total_pages={p1_meta.get('total_pages')} "
        f"literal_next_status={literal_cap.get('http')} "
        f"literal_next_is_json={str(literal_cap.get('body_file') or '').endswith('.json')}"
    )
    arms["C_capture_backing"] = (
        bool(p1_cap)
        and bool(p2_cap)
        and digests_ok
        and closure == p1_meta.get("total_count")
        and [p1_cap.get("http"), p2_cap.get("http")] == ["200", "200"]
        and literal_cap.get("http") == "404"
        and p1_meta.get("total_pages") == 2
    )

    rows1, more1 = adapter._parse_page(p1, requested=DAY, page=1)
    rows2, more2 = adapter._parse_page(p2, requested=DAY, page=2)

    def tamper(mut: Any, label: str) -> str:  # noqa: ANN401  # local mutators
        document = json.loads(p1)
        mut(document)
        try:
            adapter._parse_page(json.dumps(document).encode(), requested=DAY, page=1)
        except adapter.SeekingAlphaCalendarEarningsError as exc:
            return f"{label}={exc.code}"
        return f"{label}=NOT_REFUSED"

    symbol_guard = tamper(
        lambda document: document["data"][0]["attributes"].__setitem__("slug", True),
        "symbol_bool",
    )
    cursor_guard = tamper(
        lambda document: document["links"].__setitem__("next", None), "cursor_removed"
    )
    size_guard = tamper(lambda document: document["meta"].__setitem__("size", 49), "size_lie")
    lines.append(
        f"D_guard_two_sided clean_pages={len(rows1)}+{len(rows2)} more=[{more1},{more2}] "
        f"{symbol_guard} {cursor_guard} {size_guard}"
    )
    arms["D_guard_two_sided"] = (
        len(rows1) == 50
        and len(rows2) == 6
        and (more1, more2) == (True, False)
        and symbol_guard.endswith("BAD_TEXT_FIELD")
        and cursor_guard.endswith("BAD_PAGINATION")
        and size_guard.endswith("SIZE_MISMATCH")
    )

    fetcher = adapter.SeekingAlphaCalendarEarningsFetcher()
    real_client = adapter.get_shared_http_client
    both = _Client([p1, p2])
    client_factory: Any = lambda: both  # noqa: E731  # the seam needs a zero-arg callable
    adapter.get_shared_http_client = client_factory
    truncated_doc = json.loads(p1)
    truncated_doc["meta"] = {"size": 50, "total_count": 50, "total_pages": 1}
    truncated_doc["links"] = {"next": None, "self": "x"}
    single = _Client([json.dumps(truncated_doc).encode()])
    walked: list[dict[str, Any]] = []
    only: list[dict[str, Any]] = []
    try:
        query = fetcher.transform_query(start_date="2026-10-20", end_date="2026-10-20")
        from opendata.data.protocol import FetchContext

        walked = fetcher.extract_data(query, FetchContext(timeout=None))
        single_factory: Any = lambda: single  # noqa: E731  # seam patch, same shape
        adapter.get_shared_http_client = single_factory
        only = fetcher.extract_data(query, FetchContext(timeout=None))
    finally:
        adapter.get_shared_http_client = real_client
    second_call_has_page_param = bool(both.calls[1:2] and "page[number]" in both.calls[1])
    lines.append(
        f"E_pagination_follows_envelope two_page_rows={len(walked)} requests={len(both.calls)} "
        f"second_query={both.calls[1:2]} first_query={both.calls[:1]} "
        f"relabelled_single_page_rows={len(only)} relabelled_requests={len(single.calls)} "
        f"cursor_is_envelope_driven={second_call_has_page_param and len(only) == 50}"
    )
    arms["E_pagination_follows_envelope"] = (
        len(walked) == 56
        and len(both.calls) == 2
        and second_call_has_page_param
        and "page[number]" not in both.calls[0]
        and len(only) == 50
        and len(single.calls) == 1
    )

    standard = _annotated_fields(CORE_REF, "CalendarEarningsData")
    provider_ref = _annotated_fields(SA_REF, "SACalendarEarningsData")
    reference = sorted({*standard, *provider_ref})
    landed = sorted(SeekingAlphaCalendarEarnings.model_fields)
    emitted = sorted(rows1[0]) if rows1 else []
    never_filled = sorted(set(landed) - set(emitted))
    lines.append(
        f"F_contract_comparator standard={len(standard)} provider_own={len(provider_ref)} "
        f"reference_union={len(reference)} landed_columns={len(landed)} "
        f"landed_minus_reference={sorted(set(landed) - set(reference))} "
        f"reference_minus_landed={sorted(set(reference) - set(landed))} "
        f"never_filled_by_adapter={never_filled}"
    )
    arms["F_contract_comparator"] = (
        len(reference) >= 9
        and landed == reference
        and never_filled == ["eps_consensus", "eps_previous"]
    )

    readings: dict[str, str] = {}
    claims = json.loads(CLAIMS.read_text(encoding="utf-8"))["claims"]
    sa_rows = [row for row in claims if row.get("provider") == "seeking_alpha"]
    by_model: dict[str, list[dict[str, Any]]] = {}
    for row in sa_rows:
        by_model.setdefault(str(row.get("upstream_model")), []).append(row)
    accepted_rows = {str(row.get("phase")): row for row in by_model.get("CalendarEarnings", [])}
    for phase in PHASES:
        row = accepted_rows.get(phase)
        if row is None:
            readings[phase] = "MISSING_CLAIM"
            continue
        cited = str((row.get("receipt") or {}).get("path") or "")
        path = REPO / cited
        digest = str((row.get("receipt") or {}).get("sha256") or "")
        file_ok = path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest
        machine = (
            json.loads(path.read_text(encoding="utf-8")).get("machine_execution") or {}
            if file_ok
            else {}
        )
        readings[phase] = (
            f"claim={row.get('status')} receipt_bytes={file_ok} "
            f"exit={machine.get('exit_code')} receipt_phase={machine.get('phase')}"
        )
    refused_models = sorted(set(by_model) - {"CalendarEarnings"})
    refused_face = {
        model: (
            len(by_model[model]),
            sorted({str(row.get("status")) for row in by_model[model]}),
            sorted(
                {
                    str((row.get("receipt") or {}).get("path") or "<empty>")
                    for row in by_model[model]
                }
            ),
            sorted({str(row.get("reason") or "")[:48] for row in by_model[model]}),
        )
        for model in refused_models
    }
    counterfact_path = RECEIPTS / "CalendarEarnings" / "counterfact.json"
    counterfact = json.loads(counterfact_path.read_text(encoding="utf-8")).get("counterfact") or {}
    expected = counterfact.get("expected") or {}
    present_phases = sum(1 for value in readings.values() if value != "MISSING_CLAIM")
    lines.append(
        f"G_phase_receipts present={present_phases} "
        f"of={len(PHASES)} readings={readings} refusal_code={expected.get('refusal_code')} "
        f"expected_equals_observed={counterfact.get('expected') == counterfact.get('observed')} "
        f"claim_rows_by_model={ {model: len(rows) for model, rows in sorted(by_model.items())} } "
        f"refused_rows={refused_face}"
    )
    arms["G_phase_receipts"] = (
        all(value != "MISSING_CLAIM" for value in readings.values())
        and all("claim=PASS receipt_bytes=True exit=0" in value for value in readings.values())
        and len(accepted_rows) == len(PHASES)
        and expected.get("refusal_code") == "SEEKING_ALPHA_CALENDAR_BAD_TEXT_FIELD"
        and counterfact.get("expected") == counterfact.get("observed")
        and refused_models == ["ForwardEpsEstimates", "ForwardSalesEstimates"]
        and all(count == len(PHASES) for count, _s, _p, _r in refused_face.values())
        and all(statuses == ["BLOCKED"] for _c, statuses, _p, _r in refused_face.values())
        and all(paths == ["<empty>"] for _c, _s, paths, _r in refused_face.values())
        and all(any(reason.strip() for reason in reasons) for *_x, reasons in refused_face.values())
    )

    census_bucket = ""
    census_note = "sibling instrument unavailable"
    try:
        sib = _load("w35_refusal_sibling", REFUSAL_FACE)
        census = sib._census_face()
        rows = {
            row["identity"]: row.get("action", "")
            for row in census.get("rows", [])
            if row.get("identity")
        }
        census_bucket = rows.get("seeking_alpha::CalendarEarnings", "ABSENT_FROM_CENSUS_ROWS")
        census_note = (
            f"residue={census.get('residue')} actions={census.get('actions')} "
            f"exit={census.get('exit')} red_arms={census.get('red_arms')}"
        )
    except Exception as exc:  # a dead sibling instrument is a reading, not a crash
        census_note = f"census_face_unavailable {type(exc).__name__}"
    lines.append(
        f"H_census_bucket row=seeking_alpha::CalendarEarnings action={census_bucket or '-'} "
        f"detail={census_note}"
    )
    arms["H_census_bucket"] = census_bucket == "ABSENT_FROM_CENSUS_ROWS"

    unlisted_count = -1
    try:
        sys.path.insert(0, str(REPO / "tests"))
        authority_test = _load(
            "w48_authority_test", REPO / "tests/test_authority_reconciliation.py"
        )
        from opendata.data.registry import authority_baseline, get_registry, reconcile_authority

        registry = get_registry()
        from opendata.data.providers.catalog import register_providers

        register_providers(registry)
        served: dict[str, set[str]] = {}
        for capability in registry.capabilities():
            served.setdefault(capability.domain, set()).add(capability.source)
        unlisted = set(served) - set(authority_baseline())
        unlisted_count = len(unlisted)
        expected_set = authority_test.SINGLE_SOURCE_EXEMPTIONS | authority_test.NATIVE_MODEL_DOMAINS
        missing_from_pins = sorted(unlisted - expected_set)
        multi_without_row = sorted(domain for domain in unlisted if len(served[domain]) > 1)
        lines.append(
            f"I_authority_partition unlisted={unlisted_count} "
            f"absent_from_pinned_lists={missing_from_pins} "
            f"multi_source_without_row={multi_without_row} "
            f"wave37_domain_listed={'cboe_index_snapshots' in expected_set} "
            f"this_wave_domain_listed={'seeking_alpha_calendar_earnings' in expected_set} "
            f"reconcile={reconcile_authority(list(registry.capabilities())) or '()'}"
        )
        arms["I_authority_partition"] = (
            missing_from_pins == []
            and multi_without_row == []
            and "cboe_index_snapshots" in expected_set
            and "seeking_alpha_calendar_earnings" in expected_set
        )
    except Exception as exc:  # a broken instrument must report itself, not read as clean
        lines.append(f"I_authority_partition error={type(exc).__name__} {exc}")
        arms["I_authority_partition"] = False

    claims_sha = hashlib.sha256(CLAIMS.read_bytes()).hexdigest()[:16]
    lines.append(
        f"J_generation identity_set={judge_face['identity_set']} claims_sha256={claims_sha} "
        f"witness_sha256={hashlib.sha256(WITNESS.read_bytes()).hexdigest()[:16]} "
        f"receipt_sha256={hashlib.sha256(RECEIPT.read_bytes()).hexdigest()[:16]} "
        f"p1_sha16={hashlib.sha256(p1).hexdigest()[:16]} "
        f"implemented_sources={len(implemented)} of={len(list_providers())} "
        f"adapter_host={urlparse(adapter.API_URL).netloc}"
    )
    arms["J_generation"] = (
        judge_face["identity_set"] == "5168e1e723999312"
        and len(claims_sha) == 16
        and len(implemented) == 31
        and urlparse(adapter.API_URL).netloc == "seekingalpha.com"
    )

    failed = sorted(name for name, ok in arms.items() if not ok)
    lines.append(
        f"calendar_landing_face arms={len(arms)} arms_passed={sum(arms.values())} "
        f"arms_failed={len(failed)} failed={failed} PASS={not failed}"
    )
    lines.append("")
    exit_code = 0 if not failed else 1
    lines.append(f"w48_calendar_landing_face_exit={exit_code}")
    body = "\n".join(lines) + "\n"
    CARRIER.parent.mkdir(parents=True, exist_ok=True)
    CARRIER.write_text(
        "# carrier for w48_calendar_landing_face.py\n"
        f"# argv: {sys.executable} {REPO / BUNDLE / 'probes' / 'w48_calendar_landing_face.py'}\n"
        f"# claims_sha256={claims_sha} identity_set={judge_face['identity_set']}\n"
        "# reference checkout: OpenBB (output-contract comparator only)\n\n" + body,
        encoding="utf-8",
    )
    print(body, end="")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
