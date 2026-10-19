"""Residue gating face: for every row still outside plane 1, which gate actually holds it.

Wave 34 through 38 described the residue as "unevidenced" or "needs an owner ruling" on the strength
of
the ledger's prose. Two columns and one reference declaration say it more precisely, and they
disagree
with the prose for four rows: ``credential_fields`` in the model task ledger,
``key_decision_status``,
``rights_status``, and the ``credentials=[...]`` list of the reference provider package (read here
only
as the contract-side statement of whether the source is keyed at all -- this repo's implementations
are
clean-room).

Measured result: 4 of the 11 rows are **credential-gated in both channels** (biztoc::WorldNews,
econdb::CountryProfile, econdb::ExportDestinations, intrinio::OptionsSnapshots) -- so an
unauthenticated
access round cannot serve them and the authorization boundary of this goal forbids a credentialed
one.
6 rows are credential-free and were probed or are probeable. 1 row has no endpoint anywhere.

Every arm is two-sided: the credential column must be able to read non-empty (it does, on the four),
and
the "no credential" reading is checked against rows whose ledger does name a key, so a broken parser
cannot produce the partition by accident.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
LEDGER = REPO / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
UPSTREAM = Path("/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers")
RUNS = REPO / "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019/runs"
C144_RECEIPT = (
    REPO / "docs/evidence/C144/wave39-unauthenticated-access-20261019/capture-receipt.json"
)

#: The rows the wave-39 chain leaves outside plane 1: 350 identities minus 339 accepted.
RESIDUE = (
    "biztoc::WorldNews",
    "econdb::CountryProfile",
    "econdb::ExportDestinations",
    "intrinio::OptionsSnapshots",
    "seeking_alpha::ForwardEpsEstimates",
    "seeking_alpha::ForwardSalesEstimates",
    "stockgrid::ShortVolume",
    "wsj::ETFActive",
    "wsj::ETFGainers",
    "wsj::ETFLosers",
    "yfinance::AvailableIndices",
)


def ledger_rows() -> dict[str, dict[str, str]]:
    """The task ledger keyed by ``provider::Model``."""
    out: dict[str, dict[str, str]] = {}
    with LEDGER.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            out[f"{row['provider']}::{row['upstream_model']}"] = dict(row)
    return out


def reference_credentials(provider: str) -> list[str]:
    """The reference package's own ``credentials=[...]`` literals, read off its provider module."""
    candidates = list((UPSTREAM / provider).glob(f"openbb_{provider}/__init__.py"))
    if not candidates:
        return []
    text = candidates[0].read_text(encoding="utf-8", errors="replace")
    block = re.search(r"credentials=\[(?P<body>[^\]]*)\]", text, re.DOTALL)
    if not block:
        return []
    return list(re.findall(r'"([^"]+)"', block.group("body")))


def classify(name: str, rows: dict[str, dict[str, str]], probed: set[str]) -> dict[str, Any]:
    """One row's gate: what each channel says, and which channel decides."""
    row = rows.get(name)
    if row is None:
        return {"row": name, "gate": "ABSENT_FROM_LEDGER"}
    provider = name.split("::", 1)[0]
    cred = (row.get("credential_fields") or "").strip()
    ref = reference_credentials(provider)
    if cred or ref:
        gate = "CREDENTIAL_GATED"
    elif name in probed:
        gate = "PROBED_UNANSWERED"
    else:
        gate = "NOT_PROBED_THIS_ROUND"
    return {
        "row": name,
        "gate": gate,
        "ledger_credential_fields": cred or "<empty>",
        "reference_credentials": ref or "<none declared>",
        "key_decision_status": row.get("key_decision_status", ""),
        "rights_status": row.get("rights_status", ""),
    }


#: Rows the wave-39 round actually put a request against, taken from its receipt below.
PROBED_KEY = "dispositions"


def main() -> int:
    """Print the per-row gate, the class census, and the arms that keep the census honest."""
    import json

    lines: list[str] = []
    arms: dict[str, bool] = {}
    rows = ledger_rows()
    receipt = json.loads(C144_RECEIPT.read_text(encoding="utf-8"))
    probed = {name for name in receipt[PROBED_KEY] if name in set(RESIDUE)}
    verdicts = [classify(name, rows, probed) for name in RESIDUE]
    census: dict[str, int] = {}
    for verdict in verdicts:
        census[verdict["gate"]] = census.get(verdict["gate"], 0) + 1
        lines.append(
            f"row {verdict['row']:38} gate={verdict['gate']:22} "
            f"ledger_cred={verdict['ledger_credential_fields']:10} "
            f"ref_cred={str(verdict['reference_credentials']):14} "
            f"key={verdict['key_decision_status']:26} rights={verdict['rights_status']}"
        )

    # The columns must be able to say "keyed", or CREDENTIAL_GATED is a constant, not a reading.
    keyed_elsewhere = [
        f"{r['provider']}::{r['upstream_model']}"
        for r in rows.values()
        if (r.get("credential_fields") or "").strip()
    ]
    empty_in_residue = [v["row"] for v in verdicts if v["ledger_credential_fields"] == "<empty>"]
    lines.append(
        f"ledger_population rows={len(rows)} credential_bearing={len(keyed_elsewhere)} "
        f"probed_rows_this_round={len(probed)} census={census} sums={sum(census.values())} "
        f"of={len(RESIDUE)} residue_with_no_credential={len(empty_in_residue)}"
    )
    arms["A_census_sums_to_the_residue"] = sum(census.values()) == len(RESIDUE) == 11
    arms["A_credential_column_is_not_constant"] = (
        len(keyed_elsewhere) > 0 and len(empty_in_residue) > 0
    )
    arms["B_four_rows_are_credential_gated"] = census.get("CREDENTIAL_GATED", 0) == 4
    arms["C_probed_rows_are_answered_for"] = census.get("PROBED_UNANSWERED", 0) == 5
    arms["C_unprobed_rows_are_named"] = census.get("NOT_PROBED_THIS_ROUND", 0) == 2

    # The probed-family claim has to be falsifiable: name what C144 actually captured.
    families = sorted({c["family"] for c in receipt["source_captures"]})
    lines.append(
        f"c144 capture_families={families} captures={len(receipt['source_captures'])} "
        f"dispositions={len(receipt['dispositions'])}"
    )
    arms["D_probe_families_are_the_recorded_ones"] = families == [
        "cboe",
        "control",
        "seeking_alpha",
        "wsj",
    ]
    arms["D_every_disposition_row_is_residue_or_landed"] = all(
        name in set(RESIDUE) or name == "cboe::FuturesCurve" for name in receipt["dispositions"]
    )

    # stockgrid: the only residue row whose reference package declares no key at all.
    stock_ref = reference_credentials("stockgrid")
    stock_row = rows.get("stockgrid::ShortVolume") or {}
    lines.append(
        f"stockgrid reference_credentials={stock_ref or '<none declared>'} "
        f"ledger_credential_fields={stock_row.get('credential_fields') or '<empty>'} "
        f"package_in_this_repo={(REPO / 'opendata/data/providers/stockgrid').is_dir()} "
        f"upstream_provider_dir={(UPSTREAM / 'stockgrid').is_dir()}"
    )
    arms["E_stockgrid_declares_no_key_in_both_channels"] = stock_ref == [] and not (
        (stock_row.get("credential_fields") or "").strip()
    )
    stock_pkg = REPO / "opendata/data/providers/stockgrid"
    arms["E_stockgrid_has_no_package_here"] = not stock_pkg.is_dir()

    passed = sum(1 for v in arms.values() if v)
    ok = passed == len(arms)
    lines.append(
        f"residue_gating_face arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(k for k, v in arms.items() if not v)}"
    )
    text = "\n".join(lines) + "\n"
    text = text + f"w67_residue_gating_face_exit={0 if ok else 1}" + "\n"
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / "w67_residue_gating_face.txt").write_text(text, encoding="utf-8")
    print(text, end="")
    print("w67_residue_gating_face_exit=0" if ok else "w67_residue_gating_face_exit=1")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
