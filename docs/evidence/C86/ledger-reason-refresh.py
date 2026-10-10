#!/usr/bin/env python3
"""Republish a gap cell's ledger ``reason`` from a carrier the shipped probe already wrote.

The ledger's reason text is ungated by design: ``wording_drift`` only requires the criterion's own
wording to appear in the document, so a cell can keep publishing a sentence its probe stopped
generating. This instrument closes that gap for the cells it is pointed at, from the carrier's
``VERDICT <item>: <state> — <reason>`` lines -- the probe's own stdout, archived byte-for-byte --
so what the ledger claims and what the gate measures cannot drift apart. Nothing here re-measures:
the carrier is the measurement, and its digest is what the ``--verify-only`` arm re-checks against.

Faces it prints, all counted (a no-op run has to say *which* arm made it a no-op):
``verdicts``  VERDICT lines parsed, ``gap`` of them gap-state;
``matched``   of those, cells found in the ledger (an unmatched item is a PROBLEM, not a skip);
``written``   cells whose reason actually changed; ``equal`` cells already carrying this text;
``declined``  cells whose ledger state differs from the carrier's (flipping a state is another
              instrument's job, so their prose is left alone and named).

Round label and date are read out of the carrier itself (its directory name and its own
``采集时间/started`` header line), never typed here, so the same instrument serves the next round.
``command`` is written only when a cell has none, and the carrier is appended to ``evidence`` in
both arms -- both are counted (``evidence_appended``, ``command_written``) rather than narrated.
A ``--write`` run ends by re-parsing the file it just wrote (``readback_cells``,
``readback_problems``, and each cited path checked relative and present), because exit 0 is not the
effect. A named carrier that prints no ``VERDICT`` line at all is a PROBLEM rather than a no-op
-- otherwise pointing this instrument at the wrong file would print ``verdicts=0`` and PASS.

Run: python3 docs/evidence/C86/ledger-reason-refresh.py --carrier CARRIER
     python3 docs/evidence/C86/ledger-reason-refresh.py --carrier CARRIER --write
     python3 docs/evidence/C86/ledger-reason-refresh.py --carrier CARRIER --verify-only

``CARRIER`` is a probe carrier path (``docs/evidence/<round>/*.txt``); ``--carrier`` repeats, and
each carrier's own directory names the round its cells are stamped with.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
from typing import Any, Final, NamedTuple

REPO: Final = pathlib.Path(__file__).resolve().parents[3]
LEDGER_REL: Final = "docs/quality/acceptance-item-ledger.json"
VERDICT_RE: Final = re.compile(r"^VERDICT (\S+): (\w+) — (.*)$")
DATE_RE: Final = re.compile(r"^# (?:采集时间（跑前）|started): (\d{4}-\d{2}-\d{2})")
ROUND_RE: Final = re.compile(r"^[A-Z]+\d+$")
PROBE_REL: Final = "scripts/quality/acceptance_item_probe.py"


class Reading(NamedTuple):
    """One gap cell's verbatim reading, with the carrier and round label it came from."""

    item: str
    state: str
    reason: str
    carrier: str
    round_label: str
    date: str


def carrier_meta(path: pathlib.Path) -> tuple[str, str]:
    """Return the carrier's own (round label, capture date), or abort if either is absent."""
    label = path.parent.name
    if ROUND_RE.fullmatch(label) is None:
        raise ValueError(f"{path}: cannot derive a round label from directory {label!r}")
    date = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        match = DATE_RE.match(line)
        if match:
            date = match.group(1)
            break
    if not date:
        raise ValueError(f"{path}: no 采集时间/started header line to take the date from")
    try:
        dt.date.fromisoformat(date)
    except ValueError as exc:
        raise ValueError(f"{path}: carrier date {date!r} is not an ISO date") from exc
    return label, date


def relative(path: pathlib.Path) -> str:
    """The carrier's repo-relative posix path -- what a ledger ``evidence`` entry has to be.

    An absolute path would read present on this machine and absent on every other one, so the
    ledger would cite an evidence file that outlives nobody's checkout.
    """
    return path.relative_to(REPO).as_posix()


def parse_carrier(path: pathlib.Path) -> list[Reading]:
    """Parse every VERDICT line the carrier's probe printed, gap or not."""
    label, date = carrier_meta(path)
    readings: list[Reading] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = VERDICT_RE.match(line)
        if match is None:
            continue
        item, state, reason = match.group(1), match.group(2), match.group(3).strip()
        if state == "gap" and not reason:
            raise ValueError(f"{path}: {item} reads gap with an empty reason")
        readings.append(Reading(item, state, reason, relative(path), label, date))
    return readings


def ledger_cells(ledger: dict[str, Any]) -> dict[str, str]:
    """Map each ledger key's bare item id to its key, refusing to guess on a duplicate."""
    cells: dict[str, str] = {}
    for key in ledger["items"]:
        item = str(key).rsplit("|", 1)[0]
        if item in cells:
            raise ValueError(f"ledger has two cells for {item}: {cells[item]} and {key}")
        cells[item] = str(key)
    return cells


def write_ledger(ledger: dict[str, Any]) -> None:
    """Serialize the ledger the way the repository stores it (CJK verbatim, indent 2)."""
    path = REPO / LEDGER_REL
    text = json.dumps(ledger, ensure_ascii=False, indent=2)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def read_back(touched: list[tuple[str, Reading]]) -> list[str]:
    """Re-parse the ledger from disk and require each written cell to say what was written.

    ``rc=0`` is not the effect; the effect is the bytes. A cell whose reason, round or cited
    evidence does not read back exactly as asked is reported against the file, not the intent.
    """
    findings: list[str] = []
    ledger: dict[str, Any] = json.loads((REPO / LEDGER_REL).read_text(encoding="utf-8"))
    for key, reading in touched:
        cell = ledger["items"].get(key)
        if not isinstance(cell, dict):
            findings.append(f"{key}: vanished from the ledger on disk")
            continue
        if str(cell.get("reason")) != reading.reason:
            findings.append(f"{key}: reason on disk is not the carrier's text")
        if str(cell.get("round")) != reading.round_label:
            findings.append(
                f"{key}: round on disk reads {cell.get('round')}, not {reading.round_label}"
            )
        for path in [str(p) for p in cell.get("evidence", [])]:
            if path.startswith("/") or str(path).startswith(str(REPO)):
                findings.append(f"{key}: evidence path is not repo-relative: {path}")
            elif not (REPO / path).is_file():
                findings.append(f"{key}: evidence path missing on disk: {path}")
    return findings


def report(
    readings: list[Reading], cells: dict[str, str], ledger: dict[str, Any], mode: str
) -> int:
    """Print the arms; apply the writes only in ``write`` mode, judge only in ``verify`` mode."""
    gaps = [r for r in readings if r.state == "gap"]
    write = mode == "write"
    verify = mode == "verify"
    problems: list[str] = []
    written = 0
    equal = 0
    stale = 0
    declined = 0
    missing = 0
    evidence_appended = 0
    command_written = 0
    touched: list[tuple[str, Reading]] = []
    for reading in gaps:
        key = cells.get(reading.item)
        if key is None:
            missing += 1
            problems.append(f"{reading.item}: carrier cell is not in the ledger")
            continue
        cell: dict[str, Any] = ledger["items"][key]
        if str(cell.get("state")) != "gap":
            declined += 1
            print(f"    DECLINED {key}: ledger state={cell.get('state')} carrier=gap")
            continue
        if str(cell.get("reason")) == reading.reason:
            equal += 1
            if not verify:
                print(f"    EQUAL    {key}: reason already this carrier's text")
            continue
        if verify:
            stale += 1
            problems.append(
                f"{reading.item}: ledger reason ({len(str(cell.get('reason')))}) "
                f"is not the carrier's text ({len(reading.reason)})"
            )
            continue
        written += 1
        evidence = [str(p) for p in cell.get("evidence", [])]
        appended = reading.carrier not in evidence
        command_absent = not str(cell.get("command") or "").strip()
        print(
            f"    WRITE    {key}: round {cell.get('round')} -> {reading.round_label}, "
            f"reason {len(str(cell.get('reason')))} -> {len(reading.reason)} chars, "
            f"evidence_appended={appended}, command_written={command_absent}"
        )
        cell["reason"] = reading.reason
        cell["round"] = reading.round_label
        cell["date"] = reading.date
        if appended:
            evidence.append(reading.carrier)
        cell["evidence"] = evidence
        if command_absent:
            cell["command"] = (
                f"python {PROBE_REL} --item '{reading.item}'"
                f"（判定面为实测读数，逐面见 {reading.carrier}）"
            )
        evidence_appended += int(appended)
        command_written += int(command_absent)
        touched.append((key, reading))
    for reading in readings:
        if reading.state == "gap":
            continue
        key = cells.get(reading.item)
        if key is not None and str(ledger["items"][key].get("state")) == "gap":
            declined += 1
            print(f"    DECLINED {key}: carrier={reading.state} ledger=gap, a flip is not prose")
    tally = (
        f"mode={mode} carriers={len({r.carrier for r in readings})} verdicts={len(readings)} "
        f"gap={len(gaps)} matched={len(gaps) - missing} written={written} "
        f"equal={equal} stale={stale} declined={declined} "
        f"evidence_appended={evidence_appended} command_written={command_written}"
    )
    for problem in problems:
        print(f"PROBLEM {problem}")
    if problems:
        print(f"{tally}\nLEDGER_REASON_REFRESH FAIL problems={len(problems)}")
        return 1
    print(tally)
    if written:
        if write:
            write_ledger(ledger)
            findings = read_back(touched)
            print(
                f"wrote {LEDGER_REL} readback_cells={len(touched)} "
                f"readback_problems={len(findings)}"
            )
            for finding in findings:
                print(f"PROBLEM {finding}")
            if findings:
                print("LEDGER_REASON_REFRESH FAIL readback")
                return 1
        else:
            print("dry run: pass --write to apply")
    else:
        print("nothing to write")
    print("LEDGER_REASON_REFRESH PASS")
    return 0


def main() -> int:
    """Parse the named carriers, then report, write, or read back the ledger cells."""
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument(
        "--carrier",
        action="append",
        dest="carriers",
        required=True,
        help="probe carrier to take the VERDICT lines from; repeat once per carrier",
    )
    arm = parser.add_mutually_exclusive_group()
    arm.add_argument("--write", action="store_true", help="apply the refresh to the ledger")
    arm.add_argument("--verify-only", action="store_true", help="judge the ledger, write nothing")
    args = parser.parse_args()
    mode = "write" if args.write else "verify" if args.verify_only else "dry"
    readings: list[Reading] = []
    for carrier in [str(raw) for raw in args.carriers or []]:
        path = REPO / carrier
        if not path.is_file():
            print(f"PROBLEM carrier missing: {carrier}")
            return 1
        parsed = parse_carrier(path)
        if not parsed:
            print(f"PROBLEM carrier {carrier} prints no VERDICT line, so it would refresh nothing")
            return 1
        readings += parsed
    ledger: dict[str, Any] = json.loads((REPO / LEDGER_REL).read_text(encoding="utf-8"))
    return report(readings, ledger_cells(ledger), ledger, mode)


if __name__ == "__main__":
    sys.exit(main())
