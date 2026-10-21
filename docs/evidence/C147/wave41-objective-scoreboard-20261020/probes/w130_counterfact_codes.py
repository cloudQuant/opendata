"""Wave-41t: every accepted row's counterfact receipt landed exactly its declared refusal code.

``w75`` checks five phase receipts per row, that pinned files still hash to what the receipt
recorded, and that the counterfact receipt's text names a refusal. That is a text claim; this face
closes the stronger loop on the same carriers:

* the counterfact receipt records an ``expected.refusal_code`` *and* an ``observed.refusal_code`` --
  the observed one is what the executed probe actually raised;
* the expected code must equal the code the shipped witness declares for that model, which ties the
  receipt to the witness file rather than to itself;
* ``machine_execution.exit_code`` must be 0, so the phase really ran.

Population from the judge report, declared codes from the repo's own witness loader, observed codes
from the receipt tree. The equality arms are forged in memory once so a zero-mismatch reading cannot
be vacuous.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C147/wave41-objective-scoreboard-20261020"
RUNS = REPO / BUNDLE / "runs"
CARRIER = RUNS / "w130_counterfact_codes.txt"
REPORT = REPO / "docs/evidence/C101/judge/claims.merged.report.json"
RECEIPTS = REPO / "docs/evidence/C101/receipts"
EXPECTED_ACCEPTED = 347


def _declared_codes() -> dict[tuple[str, str], str]:
    """The refusal code each shipped witness declares, keyed by (provider, model)."""
    sys.path.insert(0, str(REPO))
    from scripts.quality import iter2_claims_fixtures as fixtures

    codes: dict[tuple[str, str], str] = {}
    for source in fixtures.providers_with_witness():
        models = fixtures.provider_config(source)["models"]
        for model, entry in models.items():
            code = entry.get("refusal_code")
            if isinstance(code, str) and code:
                codes[(source, model)] = code
    return codes


def _accepted() -> list[tuple[str, str]]:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    return sorted(
        (str(model["provider"]), str(model["upstream_model"]))
        for model in report["models"]
        if model["status"] == "OFFLINE_ACCEPTED"
    )


def _receipt(provider: str, model: str) -> dict[str, Any]:
    path = RECEIPTS / provider / model / "counterfact.json"
    receipt: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return receipt


def main() -> int:
    """Compare expected/observed/declared refusal codes across every accepted row."""
    RUNS.mkdir(parents=True, exist_ok=True)
    rows = _accepted()
    judge_accepted = int(
        json.loads(REPORT.read_text(encoding="utf-8"))["summary"]["offline_acceptance_count"]
    )
    declared = _declared_codes()
    missing_receipt: list[str] = []
    missing_expected: list[str] = []
    observed_mismatch: list[tuple[str, str, str]] = []
    declared_mismatch: list[tuple[str, str, str]] = []
    undeclared: list[str] = []
    nonzero_exit: list[tuple[str, int]] = []
    parsed: dict[tuple[str, str], tuple[str, str]] = {}
    for provider, model in rows:
        key = f"{provider}::{model}"
        try:
            receipt = _receipt(provider, model)
        except FileNotFoundError:
            missing_receipt.append(key)
            continue
        counterfact = receipt.get("counterfact") or {}
        expected = str((counterfact.get("expected") or {}).get("refusal_code") or "")
        observed = str((counterfact.get("observed") or {}).get("refusal_code") or "")
        exit_code = (receipt.get("machine_execution") or {}).get("exit_code")
        parsed[(provider, model)] = (expected, observed)
        if not expected:
            missing_expected.append(key)
            continue
        if observed != expected:
            observed_mismatch.append((key, expected, observed))
        if exit_code != 0:
            nonzero_exit.append((key, int(exit_code or -1)))
        wanted = declared.get((provider, model))
        if wanted is None:
            undeclared.append(key)
        elif wanted != expected:
            declared_mismatch.append((key, wanted, expected))

    forged = dict(parsed)
    forged_next = dict(forged)
    if forged_next:
        first = sorted(forged_next)[0]
        expected, observed = forged_next[first]
        forged_next[first] = (expected + "_FORGED", observed)
    forged_failures = [
        key for key, (expected, observed) in forged_next.items() if expected != observed
    ]

    lines = [
        f"accepted_rows={len(rows)} receipts_parsed={len(parsed)}",
        f"missing_receipt={len(missing_receipt)} missing_expected={len(missing_expected)}",
        f"observed_mismatch={len(observed_mismatch)} sample={observed_mismatch[:3]}",
        f"declared_mismatch={len(declared_mismatch)} sample={declared_mismatch[:3]}",
        f"witness_without_declared_code={len(undeclared)} sample={undeclared[:3]}",
        f"nonzero_exit={len(nonzero_exit)} sample={nonzero_exit[:3]}",
        f"forge_probe failures={forged_failures}",
    ]
    arms = {
        "A_every_accepted_row_has_a_receipt": len(rows)
        == judge_accepted
        == EXPECTED_ACCEPTED
        and not missing_receipt,
        "B_every_receipt_names_an_expected_code": not missing_expected,
        "C_observed_equals_expected": not observed_mismatch,
        "D_expected_equals_the_witness_declaration": not declared_mismatch and not undeclared,
        "E_the_phase_really_ran": not nonzero_exit,
        "F_equality_can_bite": forged_failures == [sorted(forged_next)[0]]
        if forged_next
        else False,
    }
    passed = sum(1 for ok in arms.values() if ok)
    ok = passed == len(arms)
    lines.append(
        f"counterfact_codes arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(name for name, value in arms.items() if not value)}"
    )
    text = "\n".join(lines) + "\n" + f"w130_counterfact_codes_exit={0 if ok else 1}\n"
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"w130_counterfact_codes_exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
