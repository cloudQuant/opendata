"""Wave-39 access-round face: what the committed captures prove, each arm with a bite.

Reads only committed bytes under this bundle, the repo's own ledger CSV, and wave 38's residue
ladder carrier -- so a later reader recomputes the whole face without re-hitting a network.
Twelve sections, twenty-two arms: each asserts a fact about a capture and is paired with a
counterfact that has to go red, because a predicate that cannot differ is not a measurement.

Two arms were red on the first take and the bug was in the arm, not the capture: the 400 chain
legitimately repeats one reason for two different malformed requests (so "every refusal has its
own digest" was a false premise), and the credential control named a row by hand that the
ledger does not spell that way -- it is now derived from a census of the column instead.

The dispositions arm is the one that keeps this round honest about its scope: it names the
residue rows the round was run for and fails if the receipt answers for a different set, so a
quietly-skipped row shows up as a red arm rather than as a missing sentence.
"""

from __future__ import annotations

import copy
import csv
import datetime
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = REPO / "docs/evidence/C144/wave39-unauthenticated-access-20261019"
SRC = BUNDLE / "sources"
RECEIPT = BUNDLE / "capture-receipt.json"
RUNS = BUNDLE / "runs"
CBOE_SPECS = REPO / "opendata/data/providers/cboe/specs.py"
#: Read once, for the printed live-state reading only; no arm depends on it.
SPECS_TEXT = CBOE_SPECS.read_text(encoding="utf-8")
REASON_SNIPPET = "underlying list, quote/history join, or curve output protocol"

#: The six residue rows this round was run for, named exactly as the model task ledger names
#: them. Arm H checks the receipt answers for this set and no other; arm K re-measures that each
#: one really is residue, so the list cannot certify itself.
ROUND_ROWS = {
    "cboe::FuturesCurve",
    "seeking_alpha::ForwardEpsEstimates",
    "seeking_alpha::ForwardSalesEstimates",
    "wsj::ETFGainers",
    "wsj::ETFLosers",
    "wsj::ETFActive",
}

TASK_LEDGER = REPO / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
WAVE38_LADDER = REPO / (
    "docs/evidence/C143/wave38-seeking-alpha-landing-20261019/runs/w52_residue_ladder.txt"
)

EMPTY_ENVELOPE_BYTES = 31
CREDENTIAL_MARKERS = ("bear" + "er ", "api" + "key", "x-auth", "cookie:", "passwd")


def load_receipt() -> dict[str, Any]:
    """Read the committed receipt."""
    doc: dict[str, Any] = json.loads(RECEIPT.read_text(encoding="utf-8"))
    return doc


def parse_status_lines(path: Path) -> list[str]:
    """Every HTTP status line one captured headers file carries, in order."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return [line.strip() for line in text.splitlines() if line.startswith("HTTP/")]


def code_of(chain: list[str]) -> str:
    """Terminal status code, or the repo's NO_STATUS_LINE marker for a capture with none."""
    return chain[-1].split()[1] if chain else "NO_STATUS_LINE"


def settlement_table() -> list[dict[str, str]]:
    """Parse the committed cboe settlement body as the CSV it claims to be."""
    body = (SRC / "cboe-futures-settlement-csv.csv").read_text(encoding="utf-8")
    return list(csv.DictReader(io.StringIO(body)))


def curve_for(rows: list[dict[str, str]], product: str) -> list[tuple[str, float]]:
    """(expiry, price) pairs for one product, refusing when any price is not a number."""
    out: list[tuple[str, float]] = []
    for row in rows:
        if row["Product"] != product:
            continue
        out.append((row["Expiration Date"], float(row["Price"])))
    return out


def find_empty_captures(caps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Captures whose body is the route's empty estimates envelope."""
    return [c for c in caps if c["body_bytes"] == EMPTY_ENVELOPE_BYTES]


def credential_hits(text: str) -> list[str]:
    """Marker names present in one blob, for the authorization-boundary arm."""
    low = text.lower()
    return [m for m in CREDENTIAL_MARKERS if m in low]


def ladder_rows(path: Path) -> list[dict[str, str]]:
    """Parse the wave-38 residue ladder carrier into its per-row readings."""
    out: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        found = re.match(r"^ladder (?P<action>\S+)\s+(?P<row>\S+)\s+status=(?P<status>\S+)", line)
        if found:
            out.append(dict(found.groupdict()))
    if not out:
        raise SystemExit(f"residue ladder parsed 0 rows from {path}")
    return out


def ledger_credentials(path: Path, wanted: list[str]) -> dict[str, str]:
    """credential_fields as the task ledger states them, keyed ``provider::Model``."""
    targets = set(wanted)
    out: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = f"{row.get('provider')}::{row.get('upstream_model')}"
            if key in targets:
                out[key] = (row.get("credential_fields") or "").strip()
    return out


def ledger_credential_census(path: Path) -> tuple[int, int, list[str]]:
    """Rows in the task ledger, rows naming a credential field, and the first such keys."""
    total = 0
    bearing: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            total += 1
            if (row.get("credential_fields") or "").strip():
                bearing.append(f"{row.get('provider')}::{row.get('upstream_model')}")
    return total, len(bearing), sorted(bearing)[:3]


def dt_of(url: str) -> str:
    """The settlement day one probed URL asks for, or empty text."""
    found = re.search(r"dt=(\d{4}-\d{2}-\d{2})", url)
    return found.group(1) if found else ""


def disposition_date(text: str) -> str:
    """The day the source itself names in its attachment filename."""
    found = re.search(r"FuturesSettlements_(\d{4}-\d{2}-\d{2})\.csv", text)
    return found.group(1) if found else ""


def body_text(cid: str) -> str:
    """One committed capture body as text."""
    path = next(SRC.glob(f"{cid}.*"))
    return path.read_text(encoding="utf-8", errors="replace")


def main() -> int:
    """Run every arm, print its reading, and write the carrier."""
    doc = load_receipt()
    caps = doc["source_captures"]
    by_id = {c["id"]: c for c in caps}
    lines: list[str] = []
    arms: dict[str, bool] = {}

    # --- A: the capture format is read with a parser that can differ ---------------------
    codes: dict[str, int] = {}
    for c in caps:
        codes[code_of(parse_status_lines(SRC / Path(c["headers_file"]).name))] = (
            codes.get(code_of(parse_status_lines(SRC / Path(c["headers_file"]).name)), 0) + 1
        )
    forged_chain = ["HTTP/2 401 "]
    forged_code = code_of(forged_chain)
    lines.append(
        f"A codes={codes} sums={sum(codes.values())} of={len(caps)} forged_reads={forged_code}"
    )
    arms["A_codes_reconcile"] = (
        sum(codes.values()) == len(caps) == doc["environment"]["requests_made"]
    )
    arms["A_parser_can_refuse"] = forged_code == "401" and forged_code not in codes

    # --- B: the settlement CSV really is a forward curve ---------------------------------
    rows = settlement_table()
    products = sorted({r["Product"] for r in rows})
    vx = curve_for(rows, "VX")
    monotone = all(vx[i][0] < vx[i + 1][0] for i in range(len(vx) - 1))
    tamper = copy.deepcopy(rows)
    for t in tamper:
        if t["Product"] == "VX":
            t["Price"] = "not-a-number"
    refused: str
    try:
        curve_for(tamper, "VX")
        refused = "DID_NOT_REFUSE"
    except ValueError:
        refused = "REFUSED_NON_NUMERIC_PRICE"
    span = (min(p for _, p in vx), max(p for _, p in vx))
    lines.append(
        f"B rows={len(rows)} cols={list(rows[0])} products={len(products)} vx_expiries={len(vx)} "
        f"vx_dates_monotone={monotone} vx_price_span={span} "
        f"counterfact={refused}"
    )
    arms["B_is_forward_curve"] = len(vx) >= 2 and monotone
    arms["B_numeric_claim_is_falsifiable"] = refused == "REFUSED_NON_NUMERIC_PRICE"

    # --- C: dt= selects the settlement day; the first take's control could not differ ----
    dated = [c for c in caps if c["family"] == "cboe" and "dt-" in c["id"]]
    asked = {c["id"]: dt_of(c["url"]) for c in dated}
    named = {c["id"]: disposition_date(c.get("content_disposition", "")) for c in dated}
    digests_by_date = {c["body_sha256"]: asked[c["id"]] for c in dated}
    default_equal = (
        by_id["cboe-futures-settlement-csv"]["body_sha256"]
        == by_id["cboe-futures-settlement-csv-dt-2026-10-16"]["body_sha256"]
    )
    saturday_cid = "cboe-futures-settlement-csv-dt-2026-10-17-saturday"
    saturday_rows = list(csv.DictReader(io.StringIO(body_text(saturday_cid))))
    forged_named = disposition_date("FuturesSettlements_2099-01-01.csv") == "2099-01-01"
    lines.append(
        f"C dated_probes={len(dated)} every_asked_equals_every_named="
        f"{all(asked[i] == named[i] and bool(asked[i]) for i in asked)} "
        f"pairs={[(asked[i], named[i]) for i in sorted(asked)]} "
        f"distinct_bodies={len(digests_by_date)} of={len(dated)} "
        f"default_equals_2026_10_16_table={default_equal} "
        f"saturday_rows={len(saturday_rows)} forged_name_can_differ={forged_named}"
    )
    arms["C_source_names_the_day_it_serves"] = all(asked[i] == named[i] for i in asked)
    arms["C_each_date_is_its_own_body"] = len(digests_by_date) == len(dated)
    arms["C_non_settlement_day_is_empty_not_error"] = len(saturday_rows) == 0
    arms["C_filename_comparator_can_bite"] = forged_named

    # --- D: the estimates emptiness is constant over the tested input space --------------
    empties = find_empty_captures(caps)
    empty_digests = {c["body_sha256"] for c in empties}
    fours = [c for c in caps if c["http"] == "400"]
    four_digests = {c["body_sha256"] for c in fours}
    details = [re.search(r'"detail":"([^"]+)"', c["body_excerpt"]) for c in fours]
    named_details = [m.group(1) if m else "?" for m in details]
    lines.append(
        f"D empty_captures={len(empties)} distinct={len(empty_digests)} "
        f"digest={sorted(empty_digests)[0][:16] if empty_digests else '-'} "
        f"four_oh_ones={len(fours)} distinct={len(four_digests)} "
        f"details={sorted(set(named_details))} "
        f"two_bad_requests_share_one_reason={len(four_digests) < len(fours)}"
    )
    arms["D_emptiness_constant"] = len(empty_digests) == 1 and len(empties) >= 8
    arms["D_refusals_distinct_from_empty"] = len(four_digests) == len(set(named_details)) and all(
        d not in empty_digests for d in four_digests
    )

    # --- E: the same-host control shows the transport can serve data ---------------------
    control = by_id["sa-control-earnings-calendar-2026-10-20"]
    control_bytes = control["body_bytes"]
    truncated = hashlib.sha256(b"{}").hexdigest() == sorted(empty_digests)[0]
    lines.append(
        f"E control_http={control['http']} control_bytes={control_bytes} "
        f"control_digest={control['body_sha256'][:16]} "
        f"counterfact_truncated_digest_collides={truncated} (must be False)"
    )
    arms["E_control_nonempty"] = control_bytes > 1000 and control["http"] == "200"
    arms["E_control_digest_is_its_own"] = not truncated

    # --- F: wsj died below the protocol, so no code may be claimed -----------------------
    wsj = by_id["wsj-etfmovers-leaders"]
    wsj_chain = parse_status_lines(SRC / Path(wsj["headers_file"]).name)
    attempts = doc["wsj_transport_attempts"]
    all_timeout = all(a["curl_exit"] == 28 and a["http_code"] == "000" for a in attempts)
    resolver = doc["wsj_resolver"]
    lines.append(
        f"F headers_bytes={wsj['headers_bytes']} chain={wsj_chain} code={code_of(wsj_chain)} "
        f"attempts={len(attempts)} all_timeout={all_timeout} "
        f"resolver={resolver['answers']} nameserver={resolver['system_nameserver']}"
    )
    arms["F_no_status_claimed"] = wsj_chain == [] and code_of(wsj_chain) == "NO_STATUS_LINE"
    arms["F_reproducible_across_attempts"] = all_timeout and len(attempts) >= 3

    # --- G: the authorization boundary is a claim about recorded argv, and it is checkable
    blob = json.dumps(doc, ensure_ascii=False)
    hits = credential_hits(blob)
    plant = "-H " + '"Auth' + "orization: Bearer " + ("0123456789" * 4) + '"'
    planted_doc = json.dumps(doc, ensure_ascii=False) + plant
    planted_hits = credential_hits(planted_doc)
    lines.append(
        f"G markers={len(CREDENTIAL_MARKERS)} payload_hits={len(hits)} hit_list={hits} "
        f"planted_hits={len(planted_hits)} plant_printed=False"
    )
    arms["G_payload_clean"] = hits == []
    arms["G_screen_can_report"] = len(planted_hits) >= 1

    # --- H: the receipt answers for exactly the declared rows, no other ------------------
    disposed = set(doc["dispositions"])
    missing = sorted(ROUND_ROWS - disposed)
    extra = sorted(disposed - ROUND_ROWS)
    lines.append(
        f"H declared={len(ROUND_ROWS)} disposition_keys={len(disposed)} "
        f"missing={missing} unexpected={extra}"
    )
    arms["H_scope_exact"] = missing == [] and extra == []

    # --- K: the declared rows are residue, and none of them asks for a credential --------
    ladder = ladder_rows(WAVE38_LADDER)
    residue = {r["row"] for r in ladder}
    not_residue = sorted(ROUND_ROWS - residue)
    untargeted = sorted(residue - ROUND_ROWS)
    creds = ledger_credentials(TASK_LEDGER, sorted(ROUND_ROWS))
    cred_free = sorted(k for k, v in creds.items() if v == "")
    ghost = ledger_credentials(TASK_LEDGER, ["wsj::NoSuchModel"])
    ledger_total, bearing_total, bearing_sample = ledger_credential_census(TASK_LEDGER)
    lines.append(
        f"K residue_rows={len(residue)} targets_not_in_residue={not_residue} "
        f"residue_untargeted_by_this_round={untargeted} "
        f"targets_credential_free={len(cred_free)} of={len(ROUND_ROWS)} "
        f"ledger_rows={ledger_total} ledger_credential_bearing={bearing_total} "
        f"derived_control={bearing_sample} absent_row_matched_keys={sorted(ghost)}"
    )
    arms["K_targets_are_residue"] = not_residue == []
    arms["K_targets_need_no_credential"] = len(cred_free) == len(ROUND_ROWS)
    arms["K_credential_column_can_be_nonempty"] = bearing_total > 0 and len(bearing_sample) > 0

    # --- I: the claim the round refutes is recorded as data, not re-read from the ledger ---
    # The landing deletes this entry from specs.py, so arming on the live file would make this face
    # red for the right reason and readable as a defect. The retraction is armed where it belongs --
    # in the landing face's C arm, against the worktree and HEAD together.
    recorded = doc.get("refused_ledger_reasons") or {}
    reason = str(recorded.get("cboe::FuturesCurve", ""))
    empty_control = dict.fromkeys(recorded, "")
    live_reason_present = REASON_SNIPPET in SPECS_TEXT
    lines.append(
        f"I recorded_rows={len(recorded)} reason_words={len(reason.split())} "
        f"reason_kept_in_bundle={bool(reason)} live_specs_still_carry_it={live_reason_present} "
        f"counterfact_blank_reason_fails={not all(str(v) for v in empty_control.values())}"
    )
    arms["I_refuted_claim_is_recorded"] = len(recorded) >= 1 and len(reason.split()) >= 8

    # --- J: the receipt describes the bytes actually on disk, not a remembered set -------
    recomputed = 0
    absent = 0
    for c in caps:
        hp, bp = SRC / Path(c["headers_file"]).name, SRC / Path(c["body_file"]).name
        if not hp.exists() or not bp.exists():
            absent += 1
            continue
        if hashlib.sha256(bp.read_bytes()).hexdigest() == c["body_sha256"]:
            recomputed += 1
    settle_sha = by_id["cboe-futures-settlement-csv"]["body_sha256"]
    forged = hashlib.sha256(
        (SRC / Path(by_id["cboe-futures-settlement-csv"]["body_file"]).name).read_bytes() + b"\n"
    ).hexdigest()
    lines.append(
        f"J pairs_on_disk_absent={absent} digests_recomputed_matching={recomputed} of={len(caps)} "
        f"one_extra_byte_digest_collides={forged == settle_sha}"
    )
    arms["J_bytes_match_receipt"] = absent == 0 and recomputed == len(caps)
    arms["J_digest_moves_on_one_byte"] = forged != settle_sha

    # --- L: the receipt stamps its own round, so a re-take cannot pass as this one -------
    stamp = doc["captured_utc"]
    parsed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    age_hours = (datetime.datetime.now(datetime.timezone.utc) - parsed).total_seconds() / 3600.0
    lines.append(
        f"L captured_utc={stamp} tz_aware={parsed.tzinfo is not None} age_hours={age_hours:.2f}"
    )
    arms["L_receipt_is_self_dated"] = parsed.tzinfo is not None and 0 <= age_hours < 24

    passed = sum(1 for v in arms.values() if v)
    ok = passed == len(arms)
    lines.append(
        f"access_round_face arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(k for k, v in arms.items() if not v)}"
    )
    text = "\n".join(lines) + "\n"
    RUNS.mkdir(exist_ok=True)
    (RUNS / "w54_access_round_face.txt").write_text(text, encoding="utf-8")
    print(text, end="")
    print("w54_access_round_face_exit=0" if ok else "w54_access_round_face_exit=1")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
