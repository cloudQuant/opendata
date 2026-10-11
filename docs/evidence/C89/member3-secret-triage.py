"""C89: what gate member 3's single credential finding really was, and what closed it.

Run:  python docs/evidence/C89/member3-secret-triage.py     (read-only, offline, writes nothing)

Member 3 pins ``gitleaks detect --source . --config .gitleaks.toml --redact`` over the whole
history. At gate pass 4 it reported 1 finding, in this round's own evidence archive: an
identifier-list repr that put three census row ids on one line, where the name of the second
one contains ``Key``. ``generic-api-key`` read that as a key name and the next list entry as
its value. Nothing in the tree is a credential, so the question this instrument answers is
which of the two available remedies is honest:

* widen ``.gitleaks.toml`` with a sixth regex for the captured literal, or
* change the print so no two long identifiers share a line, so there is nothing to exempt.

The remedy chosen is the second, and the arms below are what make that a measurement rather
than a preference: the old bytes report the finding under the shipped config AND under the
default ruleset with every allowlist removed, the new bytes report none under either (so no
exemption is doing the work), and a fabricated credential dropped into the same one-per-line
face is still reported. The flagged line itself is never written into this file or into any
committed artifact -- it is recomputed from the staged census JSON at run time, because
committing the pair again would re-open the finding this capture exists to close.

A fourth face closes the gap the shape census above cannot: member 3 reads commits, so the
*other* files this round puts into history are scanned as payloads before they land. The
round's own Bandit bundle quotes vendor source lines, and this instrument's reconstructed
``SHAPE`` regex read no credential-shaped line in it while ``gitleaks`` did flag one under the
default ruleset -- the scanner, not the reconstruction, decides whether a commit is safe. Each
payload line the default ruleset reports is then narrowed by halving the file down to that line
and attributed by deleting one exemption at a time and watching the finding come back, so the
record names the pre-existing boundary doing the work instead of claiming no exemption is.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess  # nosec B404  # read-only git argv, shell disabled
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import tomllib

if TYPE_CHECKING:
    from types import ModuleType

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
HERE = REPO / "docs/evidence/C89"
ARCHIVE = HERE / "census-staged-112.txt"
STAGED_JSON = HERE / "census-staged-112.json"
SHAPE = re.compile(r"""Key[A-Za-z]*['"],\s*['"][A-Za-z0-9=_\-]{14,}""")
SCANNED_SUFFIXES = frozenset({".txt", ".json", ".md", ".py", ".yaml", ".yml"})


def load_scanner() -> ModuleType:
    """Import this round's gitleaks differential so its scanner and tally reader are shared."""
    path = HERE / "gitleaks-counterfactual.py"
    spec = importlib.util.spec_from_file_location("c89_gitleaks_cf", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the shared scanner from {path}")
    module = importlib.util.module_from_spec(spec)
    # register before exec: its dataclasses resolve their own class through sys.modules
    sys.modules["c89_gitleaks_cf"] = module
    spec.loader.exec_module(module)
    return module


def old_format_line() -> str:
    """Rebuild the flagged print from the staged census data, never from a transcription."""
    rows = json.loads(STAGED_JSON.read_text(encoding="utf-8"))
    ids = [str(row["task_id"]) for row in rows if not row.get("needs_engine_capability")]
    return f"staged rows with an empty need set: {len(ids)} {ids}"


def shape_hits(text: str) -> int:
    """Count lines of ``text`` carrying the key-name-then-long-value shape."""
    return sum(1 for line in text.splitlines() if SHAPE.search(line))


def tracked_shape_census(git: str) -> tuple[int, list[str]]:
    """Every tracked line that carries the shape, counted off ``git ls-files``."""
    listing = subprocess.run(  # nosec B603  # noqa: S603
        [git, "ls-files"], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    hits: list[str] = []
    for rel in listing:
        path = REPO / rel
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if shape_hits(text):
            hits.append(rel)
    return len(hits), hits


def history_payloads() -> dict[str, str]:
    """The bytes this round is about to hand to history: this round's carriers and the ledger.

    Member 3 reads commits, so a fix that leaves the working tree clean can still put the shape
    into history through a *different* file -- this round's Bandit bundle quotes vendor source
    lines, which is where a credential-shaped literal would re-appear. The tree-wide ``SHAPE``
    census above is a reconstruction of the rule and did not catch that; the scanner itself is
    the authority, so the payloads are committed to a throwaway repo and scanned here first.
    """
    payloads: dict[str, str] = {}
    for path in sorted(HERE.iterdir()):
        if path.is_file() and path.suffix in SCANNED_SUFFIXES:
            payloads[path.name] = path.read_text(encoding="utf-8", errors="replace")
    ledger = REPO / "docs/quality/acceptance-item-ledger.json"
    payloads["acceptance-item-ledger.json"] = ledger.read_text(encoding="utf-8", errors="replace")
    return payloads


def config_layers(shipped: str) -> list[tuple[str, str]]:
    """``(label, config without that one entry)`` for every exemption the shipped config carries.

    The labels come out of ``.gitleaks.toml`` itself through ``tomllib``, and the modified config
    is that file with one entry deleted -- so a hit is attributed to a named boundary by removing
    the boundary and watching the finding come back, not by reading the regex with my own eyes.
    A label is an *index* into that committed file, never the literal itself: this instrument
    prints its attributions into an archive, and pasting an exemption's literal into the print
    hands the next round a line carrying the very shape it documents.
    """
    lines = shipped.splitlines(keepends=True)
    parsed = tomllib.loads(shipped)
    layers: list[tuple[str, str]] = []
    for rule in parsed.get("rules", []):
        allowlist = rule.get("allowlist", {})
        regexes = allowlist.get("regexes", [])
        for position, literal in enumerate(regexes, start=1):
            for quote in ("'''%s'''", '"%s"'):
                want = (quote % literal).strip()
                kept = [ln for ln in lines if ln.strip().rstrip(",") != want]
                if len(kept) < len(lines):
                    layers.append(
                        (
                            f"entry {position} of {len(regexes)} in the {rule['id']} "
                            "rule allowlist",
                            "".join(kept),
                        )
                    )
                    break
    start = [i for i, ln in enumerate(lines) if ln.strip() == "[[allowlists]]"]
    if start:
        layers.append(
            (
                "the global allowlists block (evidence path to sha256 pairs)",
                "".join(lines[: start[0]]),
            )
        )
    return layers


def first_flagged_line(name: str, text: str, cf: ModuleType, baseline: str) -> tuple[int, str]:
    """Narrow one payload to the line the default ruleset reports, by halving it.

    ``gitleaks --redact`` collapses same-shape findings, so a reported count is not a line count;
    the index is what carries the attribution.
    """
    lines = text.splitlines(keepends=True)
    low, high = 0, len(lines)
    while high - low > 1:
        mid = (low + high) // 2
        for part in ((low, mid), (mid, high)):
            if cf.scan({name: "".join(lines[part[0] : part[1]])}, baseline):
                low, high = part
                break
        else:
            raise SystemExit(f"{name}: reported a finding but no half of it reproduces one")
    return low, lines[low].rstrip()


def payload_attributions(
    payloads: dict[str, str], cf: ModuleType, shipped: str, baseline: str
) -> list[tuple[str, list[str]]]:
    """Attribute every baseline-reporting payload line to the shipped boundary that covers it."""
    notes: list[tuple[str, list[str]]] = []
    layers = config_layers(shipped)
    for name, text in payloads.items():
        hits = cf.scan({name: text}, baseline)
        if not hits:
            continue
        index, line = first_flagged_line(name, text, cf, baseline)
        covers = [label for label, mod in layers if cf.scan({name: line}, mod)]
        quiet = cf.scan({name: line}, shipped)
        notes.append(
            (
                f"{name}: {len(hits)} finding(s) of "
                f"{' '.join(sorted({rule for rule, _ in hits}))} at line {index + 1} "
                f"({len(line)} chars; the line is not reproduced here, because a quote of it "
                "would carry the shape into the next archive -- open the committed file at that "
                f"line); the shipped config reads "
                f"{'nothing' if not quiet else ' '.join(sorted({r for r, _ in quiet}))} and "
                f"removing {'; '.join(covers) or 'no single entry'} brings it back",
                covers,
            )
        )
    return notes


def main() -> int:
    """Score the three faces: the finding, the fix, and the credential the fix cannot hide."""
    cf = load_scanner()
    shipped: str = cf.SHIPPED
    baseline: str = cf.BASELINE_CONFIG
    old_line = old_format_line()
    new_bytes = ARCHIVE.read_text(encoding="utf-8")

    print("== the finding (recomputed from the staged census JSON, not transcribed) ==")
    print(f"  old print: {len(old_line)} chars, carries the shape: {bool(SHAPE.search(old_line))}")
    print(f"  new archive carries the shape on: {shape_hits(new_bytes)} line(s)")
    old_shipped: set[str] = cf.rules_for({"e.txt": old_line}, shipped)
    old_default: set[str] = cf.rules_for({"e.txt": old_line}, baseline)
    print(f"  old bytes, shipped config : rules={sorted(old_shipped)}")
    print(f"  old bytes, no allowlists  : rules={sorted(old_default)}")

    print("\n== the fix (the regenerated archive's bytes under both rulesets) ==")
    new_shipped: set[str] = cf.rules_for({"e.txt": new_bytes}, shipped)
    new_default: set[str] = cf.rules_for({"e.txt": new_bytes}, baseline)
    print(f"  new bytes, shipped config : rules={sorted(new_shipped)}")
    print(f"  new bytes, no allowlists  : rules={sorted(new_default)}")

    print("\n== the damage arm (a real-shaped credential in the same one-per-line face) ==")
    damaged: set[str] = cf.rules_for({"e.txt": f"{new_bytes}\n- {cf.FAKE_TOKEN}\n"}, shipped)
    print(f"  new bytes + fabricated token, shipped: rules={sorted(damaged)}")

    print("\n== the payloads this round hands to history, scanned before they land ==")
    payloads = history_payloads()
    pre_shipped = cf.scan(payloads, shipped)
    pre_default = cf.scan(payloads, baseline)
    print(
        f"  {len(payloads)} payload file(s), shipped config: "
        f"{sorted({rule for rule, _ in pre_shipped})} ({len(pre_shipped)} finding(s))"
    )
    print(
        f"  the same set, no allowlists: {sorted({rule for rule, _ in pre_default})} "
        f"({len(pre_default)} finding(s)) -- the arm that can disagree, so the face above is a "
        "reading and not a silence"
    )
    attributions = payload_attributions(payloads, cf, shipped, baseline)
    for note, _covers in attributions:
        print(f"  attribution: {note}")
    print(
        "  (a reported finding is one gitleaks finding after --redact collapses same-shape lines;"
        " it is not a count of the lines carrying that shape)"
    )

    print("\n== tree-wide exposure of the shape (git ls-files, per file) ==")
    total, where = tracked_shape_census(str(cf.GIT))
    print(f"  tracked files with such a line: {total} {where[:5]}")
    print(f"  positive control, the recomputed old line: {len(SHAPE.findall(old_line))} hit(s)")

    scan_path = REPO / "docs/quality/secret-scan.json"
    argv = [str(a) for a in json.loads(scan_path.read_text(encoding="utf-8"))["scan_argv"]]
    run = subprocess.run(  # nosec B603  # noqa: S603
        argv, cwd=REPO, capture_output=True, text=True
    )
    try:
        leaks, tally = cf.parse_leak_tally(run.stdout + run.stderr)
    except LookupError as exc:
        print(f"SCAN FAIL: the live scanner printed no tally: {exc}")
        return 1
    print("\n== the pinned gate command over the real repo ==")
    print(f"  rc={run.returncode} leaks_found={leaks} :: {tally}")

    problems: list[str] = []
    if not old_default:
        problems.append("old bytes report nothing even with every allowlist removed: vacuous")
    if old_shipped != old_default:
        problems.append("the finding depends on an exemption, so this pair does not isolate format")
    if new_shipped or new_default:
        problems.append("the new bytes still report a finding under one of the two rulesets")
    if not damaged:
        problems.append("the one-per-line face hides a fabricated credential: a silence, not a fix")
    if pre_shipped:
        problems.append(
            "a payload this round hands to history reports under the shipped config: "
            f"{sorted({(rule, name) for rule, name in pre_shipped})}"
        )
    unattributed = [note for note, covers in attributions if not covers]
    if unattributed:
        problems.append(
            f"{len(unattributed)} payload line(s) are exempt under a boundary no removal "
            f"re-opens, so the silence is unattributed: {unattributed}"
        )
    if total:
        problems.append(f"{total} tracked file(s) still carry the shape")
    if leaks or run.returncode:
        problems.append("the live history scan still reports a credential (run after committing)")
    for line in problems:
        print(f"  TRIAGE FAIL {line}")
    if problems:
        return 1
    print(
        "VERDICT: the finding was this round's own print format. Closed without ADDING an "
        "exemption -- old bytes report 1 under both rulesets, new bytes report 0 under both, a"
        f" fabricated credential in the same face is still reported by {sorted(damaged)},"
        f" and the {len(pre_default)} finding(s) the default ruleset still reads across"
        f" {len(attributions)} payload file(s) are each narrowed to their first flagged line and"
        " attributed to a pre-existing boundary by removing that boundary and watching the"
        " finding come back."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
