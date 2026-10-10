#!/usr/bin/env python3
"""Do the gap cells' ledger reasons still describe what their probes read today?

Five measured faces, and no judgement relaxed anywhere:

* A -- ``state`` agreement: probe verdict vs ledger state, per gap cell. Zero would mean the
  ledger is not lying about any cell's state.
* B -- ``reason`` agreement, cross-read against the ledger's own ``round`` label. The relation
  "verbatim-equal text <=> refreshed this round" is *observed* over all 18 gap cells, not asserted.
* C -- the borrowed-key face for one named defect: it is a gate, it has to find the pair, and the
  control has to empty it.
* D -- a census of the number tokens a gap reason quotes that no fact of that cell carries, each
  printed with its literal context window and with its denominator. Deliberately *not* a ``== 0``
  gate, because the tokens are dates, round labels, ports and HTTP codes as often as they are
  readings -- that is the finding, so printing the residue is the point.
* E -- the two C65 blockers that today's readings re-attribute: both texts printed side by side, so
  a rewrite of the record has a carrier for every number it quotes. Disclosure, not a gate.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess  # nosec B404  # literal argv: this repo's probe, never a shell string
import sys
import tempfile
from pathlib import Path
from typing import Any, Final

REPO = Path(__file__).resolve().parents[3]
PROBE: Final = "scripts/quality/acceptance_item_probe.py"
LEDGER: Final = "docs/quality/acceptance-item-ledger.json"
NUMBER: Final = re.compile(r"\d[\d,]*(?:\.\d+)?")
REFRESHED_ROUND: Final = "C86"
# The one named defect: AC-9|08 quotes these two counts, measures neither, and §4|02 measures both.
BORROW_ITEM: Final = "AC-9|08"
BORROW_DONOR: Final = "§4|02"
BORROW_KEYS: Final = ("key_dup_groups", "key_conflict_groups")
BORROW_VALUES: Final = ("3", "2")
CONTEXT_WINDOW: Final = 9
# The two C65 blockers that today's readings re-attribute (face E).
REATTRIBUTED: Final = ("AC-1|05", "AC-16|01")


def run_probe_all(scratch: Path) -> dict[str, dict[str, Any]]:
    """One measure pass over every probe, read back from the ``--json`` payload."""
    payload = scratch / "all-readings.json"
    done = subprocess.run(  # noqa: S603  # nosec B603  # literal argv: this repo's probe, absolute paths
        [sys.executable, str(REPO / PROBE), "--all", "--json", str(payload)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0 or not payload.exists():
        raise SystemExit(f"probe --all failed rc={done.returncode}: {done.stderr.strip()[:200]}")
    records = json.loads(payload.read_text(encoding="utf-8"))["items"]
    return {str(entry["item"]): entry for entry in records}


def ledger_rows() -> dict[str, dict[str, Any]]:
    """Ledger entries keyed by the bare ``AC-N|NN`` / ``§N|NN`` id the probe uses."""
    book = json.loads((REPO / LEDGER).read_text(encoding="utf-8"))
    items = book["items"] if isinstance(book, dict) and "items" in book else book
    rows = (
        list(items.items())
        if isinstance(items, dict)
        else [(entry.get("id"), entry) for entry in items]
    )
    out: dict[str, dict[str, Any]] = {}
    for key, entry in rows:
        if isinstance(entry, dict):
            out["|".join(str(key).split("|")[:2])] = entry
    return out


def gap_ids(rows: dict[str, dict[str, Any]]) -> list[str]:
    """The ledger cells currently recorded as gap."""
    return sorted(item for item, entry in rows.items() if entry.get("state") == "gap")


def reason_tally(
    rows: dict[str, dict[str, Any]], recs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Count verbatim-equal reasons and cross-read them against the ledger ``round`` label."""
    ids = gap_ids(rows)
    same = [
        item
        for item in ids
        if str(rows[item].get("reason", "")).strip() == str(recs[item]["reason"]).strip()
    ]
    diff = [item for item in ids if item not in same]
    state_bad = [item for item in ids if str(recs[item]["state"]) != str(rows[item].get("state"))]
    agree = [
        item for item in ids if (item in same) == (str(rows[item].get("round")) == REFRESHED_ROUND)
    ]
    return {"ids": ids, "same": same, "diff": diff, "state_bad": state_bad, "agree": agree}


def fact_values(record: dict[str, Any]) -> set[str]:
    """Whole fact values a cell measures, as strings -- never substring matches."""
    return {str(value) for value in (record.get("facts") or {}).values()}


def unmatched_tokens(
    rows: dict[str, dict[str, Any]], recs: dict[str, dict[str, Any]]
) -> tuple[int, list[str]]:
    """Every number a gap reason quotes that no fact of that cell carries, with its context."""
    lines: list[str] = []
    cited = 0
    for item in gap_ids(rows):
        reason = str(rows[item].get("reason", ""))
        mine = fact_values(recs[item])
        for match in NUMBER.finditer(reason):
            cited += 1
            if match.group(0).replace(",", "") in mine:
                continue
            low = max(0, match.start() - CONTEXT_WINDOW)
            high = min(len(reason), match.end() + CONTEXT_WINDOW)
            lines.append(f"{item}: '{match.group(0)}' ...{reason[low:high]}...")
    return cited, lines


def borrowed_pair(recs: dict[str, dict[str, Any]]) -> tuple[bool, str]:
    """Face C: is AC-9|08's quoted pair really §4|02's live reading, and unmeasured by itself?"""
    borrower_facts = recs[BORROW_ITEM].get("facts") or {}
    donor_facts = recs[BORROW_DONOR].get("facts") or {}
    own_keys = sorted(key for key in borrower_facts if "dup" in key or "conflict" in key)
    held = {key: str(donor_facts.get(key, "")) for key in BORROW_KEYS}
    quoted = tuple(held[key] for key in BORROW_KEYS) == BORROW_VALUES
    absent = all(value not in fact_values(recs[BORROW_ITEM]) for value in BORROW_VALUES)
    ok = quoted and not own_keys and absent
    detail = (
        f"donor {BORROW_DONOR} measures {held} equals {list(BORROW_VALUES)} = {quoted}; "
        f"borrower {BORROW_ITEM} names no dup/conflict fact key = {not own_keys} "
        f"({own_keys or 'none'}); "
        f"those values absent from the borrower's own fact values = {absent}"
    )
    return ok, detail


def reattributed_lines(
    rows: dict[str, dict[str, Any]], recs: dict[str, dict[str, Any]], diff: list[str]
) -> list[str]:
    """Face E: both texts for the two re-attributed cells, plus every fact they measured."""
    lines: list[str] = []
    for item in REATTRIBUTED:
        facts = recs[item].get("facts") or {}
        lines.append(f"  {item} round={rows[item].get('round')} in_diff={item in diff}")
        lines.append(f"    LEDGER reason: {rows[item].get('reason')}")
        lines.append(f"    PROBE  reason: {recs[item]['reason']}")
        lines.extend(f"    FACT {key}={facts[key]}" for key in sorted(facts))
    return lines


def control_reason(rows: dict[str, dict[str, Any]], recs: dict[str, dict[str, Any]]) -> bool:
    """Mangle one reason that currently matches: the tally has to move by exactly one."""
    before = reason_tally(rows, recs)
    pool = before["same"]
    if not pool:
        print("CONTROL-REASON no matching reason to mangle -> cannot arm")
        return False
    victim = pool[0]
    touched = copy.deepcopy(rows)
    touched[victim]["reason"] = str(touched[victim].get("reason", "")) + "（tamper）"
    after = reason_tally(touched, recs)
    moved_agree = victim not in after["agree"]
    print(
        f"CONTROL-REASON victim={victim} same {len(before['same'])} -> {len(after['same'])} "
        f"expected={len(before['same']) - 1} agree {len(before['agree'])} -> {len(after['agree'])}"
    )
    return len(after["same"]) == len(before["same"]) - 1 and moved_agree


def control_borrow(recs: dict[str, dict[str, Any]]) -> bool:
    """Delete the donor's two facts: face C has to stop finding the pair."""
    live, _ = borrowed_pair(recs)
    stripped = copy.deepcopy(recs)
    for key in BORROW_KEYS:
        stripped[BORROW_DONOR]["facts"].pop(key, None)
    tampered, _ = borrowed_pair(stripped)
    print(f"CONTROL-BORROW live={live} donor-stripped={tampered} expected live=True stripped=False")
    return live and not tampered


def main() -> int:
    """Print faces A-E, then the two controls, and exit on the conjunction."""
    rows = ledger_rows()
    with tempfile.TemporaryDirectory() as tmp:
        recs = run_probe_all(Path(tmp))
    tally = reason_tally(rows, recs)
    print(f"gap cells={len(tally['ids'])} probes={len(recs)} ledger rows={len(rows)}")
    print(f"[A] state_mismatch={len(tally['state_bad'])} {tally['state_bad'] or '[]'}")
    print(f"[B] reason_same={len(tally['same'])} reason_diff={len(tally['diff'])}")
    print(f"      same: {', '.join(tally['same']) or '-'}")
    print(f"      diff: {', '.join(tally['diff']) or '-'}")
    print(
        f"[B] 'reason_same <=> round=={REFRESHED_ROUND}' holds for {len(tally['agree'])}"
        f"/{len(tally['ids'])} cells"
    )
    found, detail = borrowed_pair(recs)
    print(f"[C] borrowed pair face holds={found}  {detail}")
    cited, lines = unmatched_tokens(rows, recs)
    print(f"[D] cited={cited} unmatched={len(lines)} number tokens over gap reasons")
    print("      (disclosure, no gate -- the residue is the finding)")
    for line in lines:
        print(f"      UNMATCHED {line}")
    print(f"[E] re-attributed blockers over {len(REATTRIBUTED)} cells (disclosure, no gate)")
    for line in reattributed_lines(rows, recs, tally["diff"]):
        print(line)
    ok = (
        found
        and not tally["state_bad"]
        and len(tally["agree"]) == len(tally["ids"])
        and control_reason(rows, recs)
        and control_borrow(recs)
    )
    print(f"STALENESS_CHECK {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
