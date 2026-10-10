#!/usr/bin/env python3
"""Preserve what the ledger says *before* a prose refresh replaces it, and say whether it was wrong.

``ledger-reason-refresh.py`` publishes a carrier's ``VERDICT`` line into a gap cell's ``reason``.
That is the right direction -- the ledger must not keep a sentence its probe stopped generating --
but for three of these cells the text being replaced is *longer* than the line coming in, and the
ledger JSON is the only file on disk holding it. So this instrument prints the pre-image verbatim
before the write, and it checks the other claim a length delta hides: a shorter line can be a
correction, not a loss.

``[1]`` per cell: ledger state vs carrier state, both lengths, and whether the pre-image is the
    longer one (the at-risk set ``[3]`` is drawn from exactly this predicate, not from a list);
``[2]`` the numeric residual in each pre-image -- number tokens that appear nowhere in that cell's
    carrier block, each with its context window and its denominator. Disclosure, not a gate: these
    are dates, round labels and ports as often as they are refuted readings;
``[3]`` the at-risk pre-images verbatim, with the round/date stamp they carry, so the record keeps a
    copy of prose the refresh is about to shorten;
``[4]`` the attribution face for the runtime blocker: the class names the pre-image says
    predominate against the classes today's census actually puts on top, so a "以 X 为主" that the
    census contradicts is named rather than silently overwritten;
``[5]`` both sides of every predicate used above -- the staleness test on a forged pair (ledger text
    = carrier text) must read *equal*, and the residual counter on that same forged pair must read
    *zero*, otherwise a non-zero ``[2]`` would be the instrument, not the record. The population
    itself is gated: the carrier's VERDICT count has to equal its ``###`` block count, and an arm
    points the two parsers at a verdict-less file to show that guard can actually fire -- the first
    run of this instrument printed PASS over ``cells=0`` because an unanchored regex matched
    nothing, and a zero denominator is the one reading that must never be allowed to pass.

Run: python3 docs/evidence/C86/reason-before-after.py
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any, Final

REPO: Final = pathlib.Path(__file__).resolve().parents[3]
LEDGER_REL: Final = "docs/quality/acceptance-item-ledger.json"
CARRIER_REL: Final = "docs/evidence/C86/gap-ten-rerun.txt"
VERDICT_RE: Final = re.compile(r"^VERDICT (\S+): (\w+) — (.*)$", re.MULTILINE)
BLOCK_RE: Final = re.compile(r"^### (\S+)", re.MULTILINE)
NUMBER_RE: Final = re.compile(r"\d{2,}")
CLASS_RE: Final = re.compile(r"(?:build|runtime|review)-[a-z][a-z-]*")
CONTEXT: Final = 16
CONTROL_ITEM: Final = "AC-1|05"


def is_stale(old: str, now: str) -> bool:
    """The predicate the refresh keys on: the ledger text is not the carrier's text."""
    return old != now


def residual(old: str, block: str) -> list[str]:
    """Number tokens the pre-image quotes that its own carrier block never printed."""
    return [token for token in NUMBER_RE.findall(old) if token not in block]


def blocks(text: str) -> dict[str, str]:
    """Split the carrier into one block per item, keyed by bare item id."""
    marks = [(m.group(1), m.start()) for m in BLOCK_RE.finditer(text)]
    found: dict[str, str] = {}
    for index, (item, start) in enumerate(marks):
        end = marks[index + 1][1] if index + 1 < len(marks) else len(text)
        found[item] = text[start:end]
    return found


def verdicts(text: str) -> dict[str, tuple[str, str]]:
    """Map each item to the (state, reason) its carrier line reports."""
    return {m.group(1): (m.group(2), m.group(3).strip()) for m in VERDICT_RE.finditer(text)}


def window(text: str, token: str) -> str:
    """The first occurrence of ``token`` with ``CONTEXT`` characters either side."""
    at = text.find(token)
    if at < 0:
        return ""
    return text[max(0, at - CONTEXT) : at + len(token) + CONTEXT].replace("\n", " ")


def main() -> int:
    """Print the pre-image, its residuals and its attribution against today's readings."""
    ledger: dict[str, Any] = json.loads((REPO / LEDGER_REL).read_text(encoding="utf-8"))
    carrier = (REPO / CARRIER_REL).read_text(encoding="utf-8")
    cells: dict[str, tuple[str, dict[str, Any]]] = {}
    for key, cell in ledger["items"].items():
        cells[str(key).rsplit("|", 1)[0]] = (str(key), cell)
    say = verdicts(carrier)
    body = blocks(carrier)
    problems: list[str] = []
    at_risk: list[str] = []
    residual_total = 0
    residual_count = 0

    print(f"[1] {len(say)} cells over {len(body)} carrier blocks, carrier={CARRIER_REL}")
    if not say or len(say) != len(body):
        problems.append(
            f"the carrier parses {len(say)} VERDICT lines over {len(body)} item blocks -- an empty "
            "parse would make every count below read as a clean zero, which is a bug report "
            "against this instrument rather than a finding about the ledger"
        )
    stale_count = 0
    for item in sorted(say):
        key, cell = cells.get(item, ("<absent>", {}))
        state, reason = say[item]
        old = str(cell.get("reason") or "")
        block = body.get(item, "")
        longer = len(old) > len(reason)
        if longer:
            at_risk.append(item)
        if is_stale(old, reason):
            stale_count += 1
        tokens = residual(old, block)
        residual_total += len(NUMBER_RE.findall(old))
        residual_count += len(tokens)
        print(
            f"    {item:>9}  ledger={cell.get('state')} carrier={state}  "
            f"len {len(old)} -> {len(reason)}  longer_pre={'yes' if longer else 'no'}  "
            f"residual={len(tokens)}/{len(NUMBER_RE.findall(old))}"
        )
        for token in tokens:
            print(f"        {token} in …{window(old, token)}…")
        if key == "<absent>":
            problems.append(f"{item}: carrier cell is not in the ledger")
        elif cell.get("state") != "gap":
            problems.append(f"{key}: ledger state={cell.get('state')}, refresh would move a state")

    print(f"[2] residual census: {residual_count} of {residual_total} number tokens untraceable")
    print(
        f"[3] at-risk pre-images (longer than the line replacing them): {len(at_risk)} -- {at_risk}"
    )
    for item in at_risk:
        key, cell = cells[item]
        print(f"    --- {key} round={cell.get('round')} date={cell.get('date')} ---")
        print(f"    {cell.get('reason')}")

    pre = str(cells.get(CONTROL_ITEM, ("", {}))[1].get("reason") or "")
    now = say.get(CONTROL_ITEM, ("", ""))[1]
    block = body.get(CONTROL_ITEM, "")
    pre_classes, now_classes = set(CLASS_RE.findall(pre)), set(CLASS_RE.findall(now))
    added, dropped = now_classes - pre_classes, pre_classes - now_classes
    print("[4] AC-1|05 runtime attribution, pre-image vs today's census")
    print(f"    pre-image names classes: {sorted(pre_classes)}")
    print(f"    carrier top-3 names:     {sorted(now_classes)}")
    print(f"    in carrier top-3 but absent from the pre-image: {sorted(added)}")
    print(f"    claimed by the pre-image, not in today's top-3: {sorted(dropped)}")

    forged_stale = is_stale(now, now)
    forged_residual = residual(now, block)
    real_residual = residual(pre, block)
    empty = "# 一个没有 VERDICT 行的文件\n"
    guard_fires = bool(len(verdicts(empty)) != len(blocks(empty)) or not verdicts(empty))
    print(
        f"[5] both sides of each predicate: staleness over the real pair={stale_count}/{len(say)} "
        f"(needs all), forged pair={int(forged_stale)} (needs 0)"
    )
    print(
        f"    residual over AC-1|05: real={len(real_residual)} forged={len(forged_residual)} "
        f"(needs a non-zero/zero pair)"
    )
    print(
        f"    empty-population arm: {len(verdicts(empty))} verdicts over {len(blocks(empty))} "
        f"blocks, denominator guard fires={'yes' if guard_fires else 'no'} (needs yes)"
    )
    if not guard_fires:
        problems.append(
            "the denominator guard cannot fire: a carrier parsing zero cells would PASS"
        )
    if stale_count != len(say) or forged_stale:
        problems.append(
            f"staleness predicate did not move both ways: real pair {stale_count}/{len(say)}, "
            f"forged pair reads stale={int(forged_stale)}"
        )
    if len(forged_residual) != 0 or len(real_residual) == 0:
        problems.append(
            f"residual counter is not two-sided: forged={len(forged_residual)} "
            f"real={len(real_residual)}"
        )

    tally = (
        f"cells={len(say)} stale={stale_count} longer_pre={len(at_risk)} "
        f"residual={residual_count}/{residual_total} added={len(added)} dropped={len(dropped)}"
    )
    if problems:
        for problem in problems:
            print(f"PROBLEM {problem}")
        print(f"{tally}\nREASON_BEFORE_AFTER FAIL problems={len(problems)}")
        return 1
    print(f"{tally}\nREASON_BEFORE_AFTER PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
