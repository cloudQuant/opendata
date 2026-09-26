#!/usr/bin/env python3
"""Reconcile the acceptance document's two books (验收 AC-17「逐项显式阻断」).

Why this file exists
--------------------
``docs/迭代计划/迭代1-重构数据中台/验收文档.md`` keeps *two* records of the same thing:

* the **item book** -- the 130 ``- [ ]`` criteria inside the ``### AC-N`` blocks of §2-§3
  and the §4/§5/§6 planes;
* the **AC book** -- the 19-row §10 table with one status per acceptance criterion.

Through round C35 those books had never been read against each other: eight §10 rows said
``完成`` while **zero** of the 130 items was ticked, and nothing in the repository could
tell which of the two was the optimistic one. Every prior round appended to §10 because it
is one row per AC; nobody re-read the items, because re-reading 130 criteria by hand is
exactly the work that gets skipped.

What this assertion owns
------------------------
* ``docs/quality/acceptance-item-ledger.json`` maps every item -- identified by group,
  position and a digest of its own wording -- to a state: ``proven``, ``gap`` or
  ``unreviewed``. A tick in the document is legal only as the mirror image of a ``proven``
  entry, and a ``proven`` entry names the command that proves it plus evidence paths that
  must exist in the repository. Re-wording a criterion strands its ledger entry, which is
  a finding rather than a cleanup.
* Each §10 row must disclose its item-level reading as ``条目级 k/m``, matching what the
  document actually says. A row claiming completion with ``k < m`` must also carry the
  marker ``未逐条达标``, so the gap sits on the ledger line instead of in a paragraph.

This tool can never make a criterion provable, and it prints the census
(``proven / gap / unreviewed``) so that "not yet reviewed" can never be read as "passed".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = "docs/迭代计划/迭代1-重构数据中台/验收文档.md"
LEDGER_PATH = "docs/quality/acceptance-item-ledger.json"
LEDGER_VERSION = 1

AC_HEADING = re.compile(r"^### (AC-\d+)")
SECTION_HEADING = re.compile(r"^## (\d+)[.、]? ")
ITEM_LINE = re.compile(r"^- \[([ xX])\] (.*)$")
ROW_LINE = re.compile(r"^\|\s*(AC-\d+)\s*\|")
DISCLOSED = re.compile(r"条目级\s*(\d+)\s*/\s*(\d+)")
COMPLETE_WORDS = ("完成", "建立", "已通", "全绿")
# A row that already says "partly" is disclosing an unfinished criterion; only an
# unqualified claim has to spell out the item-level gap.
PARTIAL_WORDS = ("部分完成", "进行中", "未开始", "未达标")
GAP_MARKER = "未逐条达标"
STATES = ("proven", "gap", "unreviewed")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PLANES = ("§4", "§5", "§6")


class LedgerError(RuntimeError):
    """The ledger is absent, malformed or describes a different document."""


@dataclass(frozen=True)
class Item:
    """One ``- [ ]`` criterion in the acceptance document.

    Attributes:
        key: ``group|position|text-digest``; changes if the criterion is re-worded.
        group: ``AC-N`` inside an AC block, else the section tag (§4/§5/§6).
        index: 1-based position within the group.
        line: 1-based line number in the document; informational only.
        text: Criterion text with the checkbox prefix removed.
        ticked: Whether the box is checked in the document.
    """

    key: str
    group: str
    index: int
    line: int
    text: str
    ticked: bool


@dataclass(frozen=True)
class AcRow:
    """One §10 ledger row.

    Attributes:
        ac: Acceptance criterion id.
        status: The status cell, verbatim.
        line: 1-based line number in the document.
        raw: The whole row, for disclosure checks that span cells.
    """

    ac: str
    status: str
    line: int
    raw: str


def item_key(group: str, index: int, text: str) -> str:
    """Stable identity for a criterion: it moves with the wording, not with the line.

    The digest is an identity rather than a security property; sha256 is used because the
    security scan rightly flags sha1 anywhere in the tree.
    """
    normalized = " ".join(text.split()).lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{group}|{index:02d}|{digest}"


def split_cells(row: str) -> list[str]:
    r"""Split a markdown table row, honouring ``\\|`` escapes inside a cell."""
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", row)]


def parse_doc(text: str) -> tuple[list[Item], list[AcRow]]:
    """Read both books out of the acceptance document."""
    items: list[Item] = []
    rows: list[AcRow] = []
    group: str | None = None
    seen: Counter[str] = Counter()
    for number, line in enumerate(text.splitlines(), start=1):
        ac_heading = AC_HEADING.match(line)
        if ac_heading:
            group = ac_heading.group(1)
            continue
        section_heading = SECTION_HEADING.match(line)
        if section_heading:
            group = None if section_heading.group(1) == "10" else f"§{section_heading.group(1)}"
            continue
        item = ITEM_LINE.match(line)
        if item and group:
            seen[group] += 1
            index = seen[group]
            body = item.group(2).strip()
            items.append(
                Item(
                    key=item_key(group, index, body),
                    group=group,
                    index=index,
                    line=number,
                    text=body,
                    ticked=item.group(1).lower() == "x",
                )
            )
            continue
        matched_row = ROW_LINE.match(line)
        if matched_row:
            cells = split_cells(line)
            rows.append(
                AcRow(
                    ac=matched_row.group(1),
                    status=cells[3] if len(cells) > 3 else "",
                    line=number,
                    raw=line,
                )
            )
    return items, rows


def field_as_dict(ledger: dict[str, object], name: str) -> dict[str, object]:
    """Read one required object field, or explain why the ledger is unusable."""
    value = ledger.get(name)
    if not isinstance(value, dict):
        raise LedgerError(f"`{name}` must be an object")
    return value


def load_ledger(path: Path) -> dict[str, object]:
    """Read the ledger, or say precisely why it cannot be used."""
    if not path.is_file():
        raise LedgerError(f"{path.relative_to(REPO_ROOT)} is missing (run --scaffold)")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LedgerError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise LedgerError(f"{path.name} is not a JSON object")
    version = raw.get("version")
    if version != LEDGER_VERSION:
        raise LedgerError(f"ledger version is {version!r}, this checker writes {LEDGER_VERSION}")
    if raw.get("doc") != DOC_PATH:
        raise LedgerError(f"ledger points at {raw.get('doc')!r}, not {DOC_PATH!r}")
    field_as_dict(raw, "items")
    field_as_dict(raw, "group_counts")
    if not isinstance(raw.get("items_total"), int):
        raise LedgerError("`items_total` must be an integer")
    return raw


def _entry_problems(entry: dict[str, object], key: str) -> list[str]:
    """A ``proven`` claim is only as good as the command and the files it names."""
    problems: list[str] = []
    if not str(entry.get("command") or "").strip():
        problems.append(f"{key}: proven without a command that proves it")
    if not str(entry.get("round") or "").strip():
        problems.append(f"{key}: proven without the round that produced the evidence")
    if not DATE_PATTERN.match(str(entry.get("date") or "")):
        problems.append(f"{key}: proven without an ISO date")
    evidence = entry.get("evidence")
    if not (isinstance(evidence, list) and evidence):
        problems.append(f"{key}: proven without evidence paths")
        return problems
    for path in evidence:
        if not (isinstance(path, str) and path):
            problems.append(f"{key}: evidence entry is not a path")
        elif not (REPO_ROOT / path).is_file():
            problems.append(f"{key}: evidence path does not exist: {path}")
    return problems


def claims_completion(status: str) -> bool:
    """Whether a §10 status cell asserts the criterion is met, without qualification."""
    if any(word in status for word in PARTIAL_WORDS):
        return False
    return any(word in status for word in COMPLETE_WORDS)


def reconcile(
    items: list[Item], rows: list[AcRow], ledger: dict[str, object]
) -> tuple[list[str], str]:
    """Compare the two books. Return ``(findings, census line)``."""
    problems: list[str] = []
    raw_entries = field_as_dict(ledger, "items")
    entries = {key: value for key, value in raw_entries.items() if isinstance(value, dict)}
    if len(entries) != len(raw_entries):
        problems.append("every ledger entry must be an object")
    counts = field_as_dict(ledger, "group_counts")

    doc_keys = {item.key for item in items}
    problems += [f"item has no ledger entry: {key}" for key in sorted(doc_keys - set(entries))]
    problems += [
        f"ledger entry matches no item (criterion re-worded or removed): {key}"
        for key in sorted(set(entries) - doc_keys)
    ]

    total = ledger.get("items_total")
    if total != len(items):
        problems.append(
            f"the checklist moved: ledger records {total} items, the document has {len(items)}"
        )
    actual = Counter(item.group for item in items)
    for group in sorted(set(actual) | {g for g, n in counts.items() if isinstance(n, int)}):
        recorded = counts.get(group, 0)
        if actual.get(group, 0) != recorded:
            problems.append(
                f"group {group}: ledger records {recorded} criteria, the document has "
                f"{actual.get(group, 0)}"
            )

    states: Counter[str] = Counter()
    for item in items:
        entry = entries.get(item.key)
        if entry is None:
            continue
        state = entry.get("state")
        if state not in STATES:
            problems.append(f"{item.key}: unknown state {state!r}")
            continue
        states[str(state)] += 1
        if state == "proven" and not item.ticked:
            problems.append(f"{item.key}: ledger says proven but the document box is empty")
        if item.ticked and state != "proven":
            problems.append(f"{item.key}: box is ticked but the ledger state is {state!r}")
        if state == "proven":
            problems += _entry_problems(entry, item.key)
        if state == "gap" and not str(entry.get("reason") or "").strip():
            problems.append(f"{item.key}: gap recorded without a reason")

    row_acs = [row.ac for row in rows]
    problems += [
        f"§10 row {ac} has no item block in the document"
        for ac in sorted(set(row_acs) - set(actual))
    ]
    problems += [
        f"item block {ac} has no §10 row" for ac in sorted(set(actual) - set(row_acs) - set(PLANES))
    ]
    duplicated = sorted(ac for ac, number in Counter(row_acs).items() if number > 1)
    if duplicated:
        problems.append(f"§10 has duplicate rows for {duplicated}")

    for row in rows:
        group_items = [item for item in items if item.group == row.ac]
        seen = len(group_items)
        ticked = sum(1 for item in group_items if item.ticked)
        match = DISCLOSED.search(row.raw)
        if match is None:
            problems.append(f"{row.ac} (line {row.line}): must disclose 条目级 {ticked}/{seen}")
            continue
        if (int(match.group(1)), int(match.group(2))) != (ticked, seen):
            problems.append(
                f"{row.ac} (line {row.line}): discloses 条目级 {match.group(1)}/{match.group(2)} "
                f"while the document reads {ticked}/{seen}"
            )
        claims_done = claims_completion(row.status)
        if claims_done and ticked < seen and GAP_MARKER not in row.raw:
            problems.append(
                f"{row.ac} (line {row.line}): status claims completion with "
                f"{seen - ticked} item(s) unproven and carries no {GAP_MARKER} marker"
            )

    census = (
        f"items={len(items)} proven={states['proven']} gap={states['gap']} "
        f"unreviewed={states['unreviewed']} ticked={sum(1 for i in items if i.ticked)}"
    )
    return problems, census


def check(doc_text: str, ledger: dict[str, object]) -> int:
    """Print the reconciliation and return the process exit code."""
    items, rows = parse_doc(doc_text)
    problems, census = reconcile(items, rows, ledger)
    print(f"census: {census}")
    if problems:
        print(
            f"FAIL: the two acceptance books disagree ({len(problems)} finding(s)):",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    groups = len({item.group for item in items})
    print(f"OK: {census} across {groups} group(s) and {len(rows)} §10 row(s) reconcile.")
    return 0


def build_ledger(items: list[Item]) -> dict[str, object]:
    """A ledger that mirrors the document, with every item unreviewed."""
    return {
        "version": LEDGER_VERSION,
        "doc": DOC_PATH,
        "items_total": len(items),
        "group_counts": dict(sorted(Counter(item.group for item in items).items())),
        "items": {item.key: {"state": "unreviewed"} for item in items},
    }


def scaffold(force: bool) -> int:
    """Write a fresh ledger from the document. Existing claims are never replaced quietly."""
    doc = REPO_ROOT / DOC_PATH
    if not doc.is_file():
        print(f"FAIL: acceptance document not found: {DOC_PATH}", file=sys.stderr)
        return 1
    items, _ = parse_doc(doc.read_text(encoding="utf-8"))
    path = REPO_ROOT / LEDGER_PATH
    if path.is_file() and not force:
        print(
            f"FAIL: {LEDGER_PATH} exists; --scaffold --force would reset {len(items)} "
            "entries to unreviewed and destroy proven claims",
            file=sys.stderr,
        )
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(build_ledger(items), indent=2, ensure_ascii=False)
    path.write_text(payload + "\n", encoding="utf-8")
    print(f"Wrote {LEDGER_PATH}: {len(items)} item(s), all unreviewed.")
    return 0


def report() -> int:
    """Print the per-group item-level reading, without deciding anything."""
    doc = REPO_ROOT / DOC_PATH
    items, rows = parse_doc(doc.read_text(encoding="utf-8"))
    ledger = load_ledger(REPO_ROOT / LEDGER_PATH)
    problems, census = reconcile(items, rows, ledger)
    print(f"census: {census}")
    if problems:
        print(f"({len(problems)} finding(s); drop --report for the list)", file=sys.stderr)
    entries = field_as_dict(ledger, "items")
    for group in sorted({item.group for item in items}):
        group_items = [item for item in items if item.group == group]
        proven = 0
        for item in group_items:
            entry = entries.get(item.key)
            if isinstance(entry, dict) and str(entry.get("state")) == "proven":
                proven += 1
        row = next((r for r in rows if r.ac == group), None)
        claim = row.status[:24] if row else "(无 §10 行)"
        print(f"  {group:6s} 条目级 {proven}/{len(group_items)}  §10: {claim}")
    return 0


# --------------------------------------------------------------------------- #
# Self test: every way this tool could be fooled has to be seen failing.
# --------------------------------------------------------------------------- #

_PROOF_COMMAND = "python scripts/quality/acceptance_ledger_check.py --self-test"
_EVIDENCE = "scripts/quality/acceptance_ledger_check.py"

_CLEAN_DOC = """\
## 2. 功能验收

### AC-1 品牌、许可与合规改造

- [x] 已证明的判据
- [ ] 尚未证明的判据

## 3. 工程质量验收

### AC-17 工程质量门禁

- [ ] `make gate` 全绿

## 4. 数据质量验收

- [ ] 双源校对报告

## 10. 实现进度与证据

| AC | 迭代 | 状态 | 证据 | 日期 | 备注 |
|----|------|------|------|------|------|
| AC-1 | 1A | 完成 条目级 1/2（未逐条达标） | x | 2026-09-26 | n |
| AC-17 | 全程 | 进行中 条目级 0/1 | x | 2026-09-26 | n |
"""


def _proven_entry() -> dict[str, object]:
    return {
        "state": "proven",
        "round": "C36",
        "date": "2026-09-26",
        "command": _PROOF_COMMAND,
        "evidence": [_EVIDENCE],
    }


def _ledger_for(text: str) -> dict[str, object]:
    """A ledger matching ``text``, with ticks mirrored and everything else a gap."""
    items, _ = parse_doc(text)
    ledger = build_ledger(items)
    entries = field_as_dict(ledger, "items")
    for item in items:
        entries[item.key] = (
            _proven_entry() if item.ticked else {"state": "gap", "reason": "自测占位"}
        )
    return ledger


_SELF_MUTATIONS: tuple[tuple[str, str, str], ...] = (
    (
        # A tick is what has to be proven, so flipping a box keeps the item identity and
        # must be caught by the tick <-> state rule rather than by a stranded entry.
        "tick without a proven entry",
        "- [ ] 尚未证明的判据",
        "- [x] 尚未证明的判据",
    ),
    (
        "proven entry while the box is empty",
        "- [x] 已证明的判据",
        "- [ ] 已证明的判据",
    ),
    (
        "criterion re-worded under a kept proven entry",
        "- [x] 已证明的判据",
        "- [x] 已证明的判据（口径另附）",
    ),
    (
        "stale 条目级 disclosure",
        "| AC-17 | 全程 | 进行中 条目级 0/1 | x | 2026-09-26 | n |",
        "| AC-17 | 全程 | 进行中 条目级 1/1 | x | 2026-09-26 | n |",
    ),
    (
        "completion claim without the gap marker",
        "| AC-1 | 1A | 完成 条目级 1/2（未逐条达标） | x | 2026-09-26 | n |",
        "| AC-1 | 1A | 完成 条目级 1/2 | x | 2026-09-26 | n |",
    ),
    (
        "missing disclosure entirely",
        "| AC-1 | 1A | 完成 条目级 1/2（未逐条达标） | x | 2026-09-26 | n |",
        "| AC-1 | 1A | 完成 | x | 2026-09-26 | n |",
    ),
    (
        "an item silently removed",
        "- [ ] 双源校对报告\n",
        "",
    ),
)


def self_test() -> int:
    """Prove the reconciliation fails on each way it can be fooled.

    The pass case matters as much as the fail cases: a device that reports everything is
    just noise, and the exemption for a qualified claim has to be a checked behaviour.
    """
    failures: list[str] = []

    items, rows = parse_doc(_CLEAN_DOC)
    baseline_problems, census = reconcile(items, rows, _ledger_for(_CLEAN_DOC))
    if baseline_problems:
        failures.append(f"the clean reading was not clean: {baseline_problems}")
    if "proven=1" not in census or "items=4" not in census:
        failures.append(f"the clean census is wrong: {census}")

    # The exemption for a qualified claim has to be a checked behaviour, not an accident:
    # a row that says 部分完成 still discloses k/m, so it does not need the gap marker.
    partial = _CLEAN_DOC.replace(
        "| AC-1 | 1A | 完成 条目级 1/2（未逐条达标） | x | 2026-09-26 | n |",
        "| AC-1 | 1A | 部分完成 条目级 1/2 | x | 2026-09-26 | n |",
        1,
    )
    problems, _ = reconcile(*parse_doc(partial), _ledger_for(_CLEAN_DOC))
    if problems:
        failures.append(f"a qualified claim with a matching disclosure was reported: {problems}")

    for label, needle, replacement in _SELF_MUTATIONS:
        mutated = _CLEAN_DOC.replace(needle, replacement, 1)
        if mutated == _CLEAN_DOC:
            failures.append(f"mutation `{label}` did not apply; the fixture drifted")
            continue
        problems, _ = reconcile(*parse_doc(mutated), _ledger_for(_CLEAN_DOC))
        if not problems:
            failures.append(f"{label}: not caught")

    proven = next(item for item in items if item.ticked)
    broken = _ledger_for(_CLEAN_DOC)
    entries = field_as_dict(broken, "items")
    entries[proven.key] = {
        **_proven_entry(),
        "evidence": ["docs/evidence/C36/does-not-exist.log"],
    }
    problems, _ = reconcile(*parse_doc(_CLEAN_DOC), broken)
    if not any("does not exist" in problem for problem in problems):
        failures.append("a proven claim pointing at a missing evidence file was accepted")

    entries[proven.key] = {**_proven_entry(), "command": ""}
    problems, _ = reconcile(*parse_doc(_CLEAN_DOC), broken)
    if not any("without a command" in problem for problem in problems):
        failures.append("a proven claim with no command was accepted")

    if failures:
        print("FAIL: ledger self-test:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(
        f"OK: ledger self-test passed ({len(_SELF_MUTATIONS)} drifts caught, plus "
        "missing-evidence and missing-command claims)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the reconciliation as a command-line tool."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scaffold", action="store_true", help="write a fresh all-unreviewed ledger"
    )
    parser.add_argument("--force", action="store_true", help="let --scaffold replace a ledger")
    parser.add_argument("--report", action="store_true", help="print the per-group reading only")
    parser.add_argument("--self-test", action="store_true", help="prove the checks bite")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.scaffold:
        return scaffold(args.force)
    if args.report:
        return report()
    doc = REPO_ROOT / DOC_PATH
    if not doc.is_file():
        print(f"FAIL: acceptance document not found: {DOC_PATH}", file=sys.stderr)
        return 1
    try:
        ledger = load_ledger(REPO_ROOT / LEDGER_PATH)
    except (LedgerError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return check(doc.read_text(encoding="utf-8"), ledger)


if __name__ == "__main__":
    sys.exit(main())
