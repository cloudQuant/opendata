"""C89 differential: what does the fifth `generic-api-key` exemption in .gitleaks.toml silence?

The gate reported one finding at run 7 member 3: rule `generic-api-key` on
`opendata/data/providers/intrinio/provider.py:41` **as committed at 083b7e4**, secret
`environment_variable=`, entropy 3.5585186. This round reformatted that declaration into the
multi-line form the other four providers already use, but the history scan reads commits, not the
working tree, so the committed bytes stay reported until an exemption covers them. Every arm below
therefore scores a fixture that is read back out of git, not retyped.

Each arm declares the *set of rule ids* gitleaks must report. A bare count cannot tell "the
exemption bit" apart from "the rule never touched this line", and this round's finding is exactly a
case where the two look identical until a control variant speaks.

What the run is really testing, and what it refuses to pretend:

* [1]/[2] the exemption bites on the real committed bytes, and the same bytes report
  `generic-api-key` once the fifth regex is removed. Without [2], [1] would also pass on a rule
  that never matched.
* [3]/[4] THE BOUNDARY, and it is a shape, not a line. The value generic-api-key captures for
  `CredentialSpec(name="api_key", environment_variable="<anything>")` is the *next kwarg's name*,
  so a fixture whose quoted value grows by one character is silenced exactly like the real one.
  C75 could write "the exemption is the literal, not a prefix" for its `source_key` ids; this
  entry cannot say that, so it does not.
* [5]/[6] a fabricated `ghp_` token written into that same slot: `github-pat` still reports it with
  the exemption in place, and [6] shows the pair the exemption removes. The catch-all goes quiet
  about a kwarg name; the specific rules keep reading the value.
* [7]/[8] forward exposure, measured on a second provider's own env-var name substituted into the
  committed shape: a future one-liner of this shape is quiet under `generic-api-key` and reported
  once the regex is removed.
* [9]/[10] the evidence face that ships with this run: the printed fixture line, put at the path
  this round writes it to, is exempt under the shipped config and reported without the regex --
  which is the difference between a documented exemption and a round that rediscovers its own
  comment as a false positive next time.

Printed as surfaces, not arms, because arms must be able to differ:
the 14 files that carry a `CredentialSpec` token in the tree (13 provider packages plus
providers/catalog.py) scan to 0 findings in history mode under the DEFAULT ruleset with every
allowlist removed, and 0 under the shipped config -- so nothing in the live tree is covered by this
entry, and its silence is the code's multi-line shape rather than the exemption. Finally the pinned
gate command itself is re-run over the real repo, because a fixture differential that never meets
the tree it is supposed to protect is a reading of nothing.

The fixture token is fabricated (no account behind it) and assembled from four nine-character
literals rather than encoded: gitleaks 8.30.1 decodes one base64 layer before matching, so hiding a
secret-shaped string in this file would make the evidence itself the finding.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
SHIPPED = (REPO / ".gitleaks.toml").read_text(encoding="utf-8")
BASELINE_CONFIG = "[extend]\nuseDefault = true\n"
C89_REGEX_LINE = "  '''^environment_variable=$''',\n"
_git_path = shutil.which("git")
_gitleaks_path = shutil.which("gitleaks")
if _git_path is None:
    raise SystemExit("git is not on PATH; every arm commits a fixture, so it would run nothing")
if _gitleaks_path is None:
    raise SystemExit("gitleaks is not on PATH; the arms would be scoring nothing")
GIT: str = _git_path
GITLEAKS: str = _gitleaks_path

PAT_PREFIX = "ghp_"
PAT_BODY_CHUNKS = ("8kj2lm9pq", "4rt7yw3zn", "6bv1cx5ds", "2af9gh4jk")
FAKE_TOKEN = PAT_PREFIX + "".join(PAT_BODY_CHUNKS)
GENERIC_SLOT_VALUE = "q7Kd9fH2mR5tX8wB3nVC"

COMMITTED_PROVIDER = "opendata/data/providers/intrinio/provider.py"
LIVE_PROVIDER = REPO / COMMITTED_PROVIDER
CARRIERS = sorted((REPO / "opendata/data/providers").glob("*/provider.py")) + [
    REPO / "opendata/data/providers/catalog.py"
]


def git_show(rev: str, path: str) -> str:
    """Bytes of ``path`` as committed, because the gate scans history rather than the tree."""
    out = subprocess.run(  # nosec B603  # noqa: S603
        [GIT, "--no-pager", "show", f"{rev}:{path}"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout


def first_line_with(text: str, needle: str, source: str) -> str:
    """One verbatim line from ``text``, failing loudly instead of measuring a shape that moved."""
    for line in text.splitlines():
        if needle in line:
            return line
    raise SystemExit(
        f"{source} no longer contains {needle!r}; the arm would score a fixture that is not in "
        "the tree and a silent reading there would mean nothing"
    )


HIST = first_line_with(
    git_show("083b7e4", COMMITTED_PROVIDER), "environment_variable=", "083b7e4 (committed)"
)
LIVE_BLOCK = "\n".join(LIVE_PROVIDER.read_text(encoding="utf-8").splitlines()[-9:])
# The committed one-liner with its quoted value grown by one character: the only thing this can
# prove is whether the captured secret depends on the value at all.
HIST_OFF_BY_ONE = HIST.replace('="INTRINIO_API_KEY"', '="INTRINIO_API_KEYX"')
HIST_TOKEN_SLOT = HIST.replace('"INTRINIO_API_KEY"', f'"{FAKE_TOKEN}"')
HIST_GENERIC_SLOT = HIST.replace('"INTRINIO_API_KEY"', f'"{GENERIC_SLOT_VALUE}"')
# A second provider's own env-var name, read off the live tree, put into the committed shape.
OTHER_ENV = (
    first_line_with(
        (REPO / "opendata/data/providers/fmp/provider.py").read_text(encoding="utf-8"),
        'environment_variable="',
        "fmp/provider.py (live)",
    )
    .split('environment_variable="')[1]
    .split('"')[0]
)
OTHER_ONELINE = HIST.replace("INTRINIO_API_KEY", OTHER_ENV)

VARIANTS = {
    "shipped": SHIPPED,
    "no-c89-regex": SHIPPED.replace(C89_REGEX_LINE, ""),
}

# (fixture path, content, variant, expected rule ids, label)
ARMS: list[tuple[str, str, str, set[str], str]] = [
    ("p.py", HIST, "shipped", set(), "exemption bites on the committed bytes"),
    ("p.py", HIST, "no-c89-regex", {"generic-api-key"}, "control: same bytes, regex removed"),
    ("p.py", HIST_OFF_BY_ONE, "shipped", set(), "BOUNDARY: a shape rule, not this line"),
    ("p.py", HIST_OFF_BY_ONE, "no-c89-regex", {"generic-api-key"}, "BOUNDARY control"),
    ("p.py", HIST_TOKEN_SLOT, "shipped", {"github-pat"}, "token in the slot still reported"),
    ("p.py", HIST_TOKEN_SLOT, "no-c89-regex", {"generic-api-key", "github-pat"}, "what it removes"),
    ("p.py", HIST_GENERIC_SLOT, "shipped", set(), "generic secret in the slot is the exposure"),
    ("p.py", OTHER_ONELINE, "no-c89-regex", {"generic-api-key"}, "forward exposure, other name"),
    ("docs/evidence/C89/x.txt", HIST, "shipped", set(), "this round's own printed face"),
    (
        "docs/evidence/C89/x.txt",
        HIST,
        "no-c89-regex",
        {"generic-api-key"},
        "printed-face control",
    ),
]


def scan(files: dict[str, str], config_text: str) -> list[tuple[str, str]]:
    """Commit one fixture set in a throwaway repo, scan its history, return (rule, file) pairs."""
    root = Path(tempfile.mkdtemp(prefix="c89-cf-"))
    try:
        (root / ".gitleaks.toml").write_text(config_text, encoding="utf-8")
        for rel, content in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content + "\n", encoding="utf-8")
        for step in (
            ["init", "-q", "."],
            ["-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
            ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fixture"],
        ):
            subprocess.run(  # nosec B603  # noqa: S603
                [GIT, *step], cwd=root, check=True, capture_output=True, text=True
            )
        report = root / "report.json"
        code = subprocess.run(  # nosec B603  # noqa: S603
            [
                GITLEAKS,
                "detect",
                "--source",
                str(root),
                "--config",
                str(root / ".gitleaks.toml"),
                "--report-format",
                "json",
                "--report-path",
                str(report),
                "--no-banner",
                "--redact",
            ],
            capture_output=True,
            text=True,
        )
        findings = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else []
        if code.returncode not in (0, 1):
            raise SystemExit(
                f"gitleaks did not run to a verdict (rc={code.returncode}): {code.stderr[:300]}"
            )
        return sorted((str(f.get("RuleID")), str(f.get("File")).split("/")[-1]) for f in findings)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def rules_for(files: dict[str, str], config_text: str) -> set[str]:
    """The rule-id set a fixture set reports under one config."""
    return {rule for rule, _file in scan(files, config_text)}


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def parse_leak_tally(scan_out: str) -> tuple[int, str]:
    r"""Read gitleaks' own tally line and refuse to invent a count it never printed.

    Two defects this reader had before its own control caught them, both of which manufacture
    a silent reading out of a loud one: a first draft selected candidate lines on the word
    ``leaks`` alone, and gitleaks' startup banner spells ``gitleaks``, so the line it picked
    was the logo art and the count came from a defaulted 0. The second draft then took the
    first digit run of the real tally line, which for ``5:20PM WRN 11 leaks found`` is the
    timestamp ``5`` -- a leak count read off a clock. Selection now requires the words
    ``leaks found``, and the count is anchored to the digits adjacent to them.

    A third defect, caught by the reader failing loudly rather than defaulting: gitleaks wraps
    its log runs in ANSI colour, and the escape that opens the bold run welds itself to the
    words -- in ``\x1b[1mleaks found: 1`` there is no word boundary before ``leaks``, so the
    shipped selection found no candidate and raised. Member 3's own judge never saw this
    because it reads only the exit code; the count is this instrument's claim, so the escapes
    are stripped here before selection. Both faces are in the control printed by main().
    """
    scan_out = ANSI.sub("", scan_out)
    tally_line = ""
    for line in scan_out.splitlines():
        stripped = line.strip()
        if re.search(r"\bleaks? found\b", stripped):
            tally_line = stripped
    if not tally_line:
        raise LookupError("no `leaks found` / `no leaks found` line appears in the scan output")
    if "no leaks" in tally_line:
        return 0, tally_line
    count = re.search(r"(?:(\d+)\s+leaks? found\b)|(?:\bleaks? found:\s*(\d+))", tally_line)
    if count is None:
        raise LookupError(f"tally line carries no count adjacent to `leaks found`: {tally_line!r}")
    return int(count.group(1) or count.group(2)), tally_line


def carrier_surface() -> tuple[int, int, int, int, set[str], set[str]]:
    """Count the CredentialSpec carriers and scan all of their live bytes under both rulesets."""
    files = {str(path.relative_to(REPO)): path.read_text(encoding="utf-8") for path in CARRIERS}
    declaring = sum(1 for text in files.values() if "CredentialSpec(" in text)
    sites = sum(text.count("CredentialSpec(") for text in files.values())
    kwarg_lines = sum(
        1
        for path in REPO.rglob("*.py")
        if path.is_relative_to(REPO / "opendata")
        for line in path.read_text(encoding="utf-8").splitlines()
        if 'environment_variable="' in line and re.search(r"api_key|apikey|token|secret", line)
    )
    return (
        declaring,
        sites,
        kwarg_lines,
        len(files),
        rules_for(files, BASELINE_CONFIG),
        rules_for(files, SHIPPED),
    )


def main() -> int:
    """Print the surfaces, guard the variants, then score every arm against its declared set."""
    print("== the finding this round is triaging (read back out of git, not retyped) ==")
    print(f"  committed path:line   {COMMITTED_PROVIDER} as of 083b7e4")
    print(f"  fixture bytes       {len(HIST)} chars, {HIST.strip()[:64]}...")
    print(f"  live bytes now      {len(LIVE_BLOCK.splitlines())} lines, multi-line CredentialSpec")

    declaring, sites, kwarg_lines, carrier_count, base_rules, shipped_rules = carrier_surface()
    print("== live-tree surface (why the exemption covers nothing that is in the tree now) ==")
    print(f"  files carrying a CredentialSpec token: {carrier_count}")
    print(f"  of them declaring credentials: {declaring}, through {sites} call sites")
    print(
        "  opendata/**/*.py lines carrying a credential keyword AND "
        f'environment_variable=": {kwarg_lines}'
    )
    print(
        f"  those {carrier_count} files, DEFAULT ruleset, allowlists removed: rules="
        f"{sorted(base_rules)}"
    )
    print(f"  those {carrier_count} files, shipped config: rules={sorted(shipped_rules)}")
    if base_rules or shipped_rules or kwarg_lines:
        print("SURFACE FAIL: a live line is covered, so the boundary above is not the one measured")
        return 1

    for variant, text in VARIANTS.items():
        if variant != "shipped" and text == SHIPPED:
            raise SystemExit(
                f"variant {variant!r} did not change the config; its expectation would score the "
                "shipped ruleset and every reading taken from it is a fiction"
            )

    print("== baseline: default ruleset, no allowlists (the denominator every silent arm needs) ==")
    baseline: dict[str, set[str]] = {}
    for path, content, _variant, _expected, _label in ARMS:
        key = f"{path}|{content}"
        if key not in baseline:
            baseline[key] = rules_for({path: content}, BASELINE_CONFIG)
    failures = 0
    for path, content, _variant, expected, _label in ARMS:
        if not expected and not baseline[f"{path}|{content}"]:
            print(
                f"     VACUOUS {path}: no default rule fires, so claiming silence measures nothing"
            )
            failures += 1
    seen: set[str] = set()
    for path, content, _v, _e, _l in ARMS:
        key = f"{path}|{content}"
        if key in seen:
            continue
        seen.add(key)
        print(f"     baseline {content[:44]:46s} rules={sorted(baseline[key])}")
    if failures:
        print("COUNTERFACT FAIL: vacuous arm(s)")
        return 1

    print("\n== arms: shipped variants against declared rule-id sets ==")
    bad = 0
    for index, (path, content, variant, expected, label) in enumerate(ARMS, start=1):
        rules = rules_for({path: content}, VARIANTS[variant])
        state = "OK " if rules == expected else "FAIL"
        bad += rules != expected
        print(
            f"[{index:2d}] {state} {variant:14s} {Path(path).name:8s} "
            f"expect={sorted(expected)} got={sorted(rules)} :: {label}"
        )
    if bad:
        print(f"COUNTERFACT FAIL: {bad} arm(s) disagreed with their declared rule sets")
        return 1

    print("\n== the pinned gate command over the real repo (the face member 3 actually prints) ==")
    print("== the tally parser's own two-sided control (a zero needs a non-zero reading) ==")
    for face, want in (
        ("5:20PM INF no leaks found", 0),
        ("5:20PM WRN 11 leaks found", 11),
        ("5:20PM WRN leaks found: 13", 13),
        ("\x1b[90m5:24PM\x1b[0m \x1b[33mWRN\x1b[0m \x1b[1mleaks found: 1\x1b[0m", 1),
        ("░    gitleaks", None),
    ):
        try:
            got, _line = parse_leak_tally(face)
        except LookupError as exc:
            got, note = None, str(exc)
        else:
            note = ""
        state = "OK " if got == want else "FAIL"
        print(f"  {state} {face!r:40s} parsed={got} declared={want} {note}")
        if got != want:
            print("TALLY PARSER FAIL: the reader cannot both take a 0 and refuse a missing tally")
            return 1

    manifest = json.loads((REPO / "docs/quality/secret-scan.json").read_text(encoding="utf-8"))
    argv = [str(a) for a in manifest["scan_argv"]]
    run = subprocess.run(  # nosec B603  # noqa: S603
        argv, cwd=REPO, capture_output=True, text=True
    )
    try:
        leaks, leak_line = parse_leak_tally(run.stdout + run.stderr)
    except LookupError as exc:
        print(
            f"GATE SURFACE FAIL: {exc}; a defaulted 0 here would be a number the scanner never said"
        )
        return 1
    print(f"  argv[0]={argv[0]} rc={run.returncode} leaks_found={leaks} :: {leak_line}")
    if run.returncode != 0 or leaks:
        print("GATE SURFACE FAIL: member 3 still reports a credential over this history")
        return 1
    print(
        f"COUNTERFACT PASS: {len(ARMS)} arms agreed with their declared rule sets; "
        f"the gate's own scan returns rc={run.returncode}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
