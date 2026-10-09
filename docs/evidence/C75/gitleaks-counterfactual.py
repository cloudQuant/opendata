"""C75 differential: what exactly do the four exemptions in .gitleaks.toml silence?

The claim under test is never "the scan went quiet" -- it is a per-rule difference. Each arm
declares the *set of rule ids* gitleaks must report, because a bare count hides which rule spoke,
and a quiet scan is equally consistent with "the exemption bit" and "nothing matched to begin with".

Fixtures are the repo's own bytes, read from disk at run time (a hand-written shape can miss the
rule by a character and then a silent arm measures nothing):

* the census prose line that produced this round's single finding
  (docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/census-sec-tmx-fed-gov-finra.json:137),
* a path->sha256 integrity pair line as archived under docs/evidence/C65/,
* a real ECB SDMX dataflow-key line from tests/test_ecb_series_client.py,
* the eleven H.15 maturity `source_key=` lines as they stand in
  opendata/data/providers/federal_reserve/specs.py -- all eleven together, one of them, and three
  mutations of that one.

Arms:

* [1] shipped, census line -> nothing (the new literal exemption bites)
* [2] third regex removed, census line -> generic-api-key reports it. Without this arm [1] would
  also pass if the rule never touched the file.
* [3] census line with ONE EXTRA CHARACTER in the exempted value -> reported: the exemption is the
  literal, not a prefix or a shape.
* [4]/[5] census line with a fabricated `ghp_` token in the exempted slot, lowercase and capitals
  -> still reported by github-pat. generic-api-key answers for neither spelling even with every
  allowlist gone (see the baseline stage), so those arms cannot be credited to, or charged
  against, this round's exemption.
* [6] the same census slot holding the token body with the `ghp_` prefix stripped -> reported by
  generic-api-key, which is the measurement that puts the [4]/[5] behaviour on the prefix and not
  on the rule's value class. A wrong theory (lowercase-only charset) cost two failed arms before
  this arm settled it, and is left in the record.
* [7] the same census slot holding a 64-hex credential -> reported: no high-entropy value can ride
  through the literal exemption in the very position it exempts.
* [8]/[9] real pair line -> exempt under shipped, reported once C73's block is dropped, so that
  older exemption is still the thing doing the work and is not being credited to this round's edit.
* [10] pair line plus a token on its own following line -> the token is reported (path scoping
  stays out; only the pair line itself is exempt).
* [11]/[12] BOUNDARY, measured and declared: token appended on the SAME line as the pair -> quiet
  under shipped, both rules reported when C73's block is dropped. This is not caused by this
  round's edit; it is C73's `regexTarget = "line"` doing exactly what it says, and it silences
  every rule on such a line, not just generic-api-key. The tree-wide count of co-located pair
  lines is 3 (docs/evidence/C65 hash records) and those 3 lines carry no credential.
* [13]/[14] real SDMX line -> exempt under shipped, reported once C36's regex is removed.
* [15] 64-hex digest under a non-path key ("api_key") -> reported: the pair rule needs a '/'.
* [16] all eleven real H.15 lines at once -> nothing. This is the arm that says the fourth
  exemption covers the finding set the gate actually reported (13 leaks, 11 of them these lines),
  not just one line of them.
* [17] same eleven lines, fourth regex removed -> generic-api-key reports. Arms [16]/[17] are the
  pair that makes [1] non-vacuous for this file: without a control on the same fixture, silence
  cannot be told apart from a rule that never touched it.
* [18] one of those lines with its final character changed (`..._N.B` -> `..._N.X`) -> reported:
  the exemption is the eleven ids, so an identifier that differs by a character is not covered.
* [19]/[20] that same slot holding a fabricated token / a real 64-hex digest -> reported by
  github-pat / generic-api-key: the field cannot smuggle a credential through the exemption.

Printed beside the arms, the blast radius is computed off the tree rather than asserted: how many
H.15 lines the spec file makes, whether the exemption regex matches each of them, how many
`source_key=` values exist across every provider spec, and whether any of them is matched by the
new regex without being one of the eleven Fed identifiers.

The fixture token is assembled from literal chunks, not encoded. An earlier draft of this file
hid it behind base64, and the shipped history scan still reported it -- tagged
``decoded:base64`` at depth 1, because gitleaks 8.30.1 decodes one base64 layer before matching.
Encoding a fabricated secret is therefore not a carrier, and the false positives it produced are
what arms [19]/[20] now reproduce deliberately through the assembled value.

Every arm commits one fixture in its own throwaway git repo and scans it in history mode, because
the gate scans history: ``--no-git`` hands gitleaks absolute paths, and a scan over absolute paths
cannot show what a config does to a real run (docs/evidence/C73/gitleaks-counterfactual.py records
the fixture that died that way). A baseline stage scans each fixture under the default ruleset with
no allowlists at all and refuses to continue if a fixture whose arm claims silence fires nothing
there -- an arm that cannot differ is not an arm.
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
_git_path = shutil.which("git")
_gitleaks_path = shutil.which("gitleaks")
if _git_path is None:
    raise SystemExit("git is not on PATH; every arm commits a fixture, so it would run nothing")
if _gitleaks_path is None:
    raise SystemExit("gitleaks is not on PATH; the arms would be scoring nothing")
GIT: str = _git_path
GITLEAKS: str = _gitleaks_path

# The fixture secret is fabricated (no account is behind it) and must not appear in this file as
# anything a scanner can read, because member 3 scans the *committed history*: a github-pat-shaped
# string parked here becomes a permanent history finding for a value that was never a credential --
# the very false-positive class this round is cleaning up. An earlier draft of this file hid it
# behind base64, and the history scan still reported it, tagged ``decoded:base64`` at depth 1:
# gitleaks 8.30.1 decodes one base64 layer before matching, so encoding is not a carrier. The
# fixture is therefore built from four nine-character literals: `github-pat` needs 36 contiguous
# body characters, and every boundary between the chunks is a quote and a comma in the source text.
# The encoding cannot neuter the arms either: the baseline stage scans the assembled value with
# every allowlist removed and refuses to continue unless `github-pat` reports it, so arms [4]/[5]
# still show the assembled value is a token-shaped secret the rule catches.
_PAT_PREFIX = "ghp_"
_PAT_BODY_CHUNKS = ("8kj2lm9pq", "4rt7yw3zn", "6bv1cx5ds", "2af9gh4jk")
FAKE_TOKEN = _PAT_PREFIX + "".join(_PAT_BODY_CHUNKS)
# The same token spelled with capitals in its body, kept because an earlier run of this tool guessed
# that generic-api-key's value class is lowercase-only in gitleaks 8.30.1. The lowercase arm below
# settled it: that rule answers for neither spelling, so the case theory was wrong and the prefix
# theory (next fixture) is what the run tests.
MIXED_TOKEN = _PAT_PREFIX + "".join(ch.upper() if ch.isalpha() else ch for ch in FAKE_TOKEN[4:])


def line_containing(rel: str, needle: str) -> str:
    """Return one verbatim line from a tracked file, failing loudly if it moved."""
    for line in (REPO / rel).read_text(encoding="utf-8").splitlines():
        if needle in line:
            return line
    raise SystemExit(
        f"fixture source {rel} no longer contains {needle!r}; the arm would measure a "
        "shape that is not in the tree"
    )


CENSUS = line_containing(
    "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/census-sec-tmx-fed-gov-finra.json",
    "rows_pointer=refRates",
)
PAIR = line_containing(
    "docs/evidence/C65/replay-pilot-manifest.json", '"opendata/pipeline/affected_keys.py": "'
)
SDMX = line_containing("tests/test_ecb_series_client.py", "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M")
DIGEST = PAIR.split('": "')[1].rstrip().rstrip(",").strip('"')

CENSUS_TOKEN_SLOT = CENSUS.replace("rows_pointer=refRates", FAKE_TOKEN)
CENSUS_MIXED_TOKEN_SLOT = CENSUS.replace("rows_pointer=refRates", MIXED_TOKEN)
# Why does generic-api-key answer for neither spelling above, even with all allowlists removed?
# The hypothesis is the `ghp_` prefix itself (the value body is otherwise the same lowercase run
# that the rule does capture for rows_pointer=refRates). Declared as an arm so the data decides:
# if stripping the prefix makes the rule fire, the prefix is what the default rule skips.
CENSUS_TOKEN_BODY = CENSUS.replace("rows_pointer=refRates", FAKE_TOKEN[len("ghp_") :])
CENSUS_DIGEST_SLOT = CENSUS.replace("rows_pointer=refRates", DIGEST)
CENSUS_OFF_BY_ONE = CENSUS.replace("rows_pointer=refRates", "rows_pointer=refRatesX")
PAIR_PLUS_TOKEN_NEXT_LINE = PAIR + f'\n    "token": "{FAKE_TOKEN}",'
PAIR_PLUS_TOKEN_SAME_LINE = PAIR.rstrip().rstrip(",") + f', "token": "{FAKE_TOKEN}"}}'
NON_PATH_DIGEST = f'{{\n  "api_key": "{DIGEST}"\n}}\n'

THIRD_REGEX = "  '''^rows_pointer=refRates$''',\n"
SDMX_REGEX = "  '''^[A-Z0-9]{1,12}(\\.[A-Z0-9_]{1,14}){3,}$''',\n"
FED_REGEX = "  '''^RIFLGFC(M01|M03|M06|Y01|Y02|Y03|Y05|Y07|Y10|Y20|Y30)_N\\.B$''',\n"

FED_SPEC = "opendata/data/providers/federal_reserve/specs.py"
SPECS_FIXTURE = FED_SPEC  # the arms commit this path inside the throwaway repo
FED_EXEMPT = re.compile(r"^RIFLGFC(M01|M03|M06|Y01|Y02|Y03|Y05|Y07|Y10|Y20|Y30)_N\.B$")


def verbatim_lines(rel: str, needle: str) -> list[str]:
    """Every line of a tracked file that contains ``needle``, straight off the disk."""
    return [
        line for line in (REPO / rel).read_text(encoding="utf-8").splitlines() if needle in line
    ]


# The eleven H.15 maturity columns the shipped declaration makes, read as they are on disk. A
# fixture that is typed by hand would prove nothing about the file the scanner actually reports.
FED_LINES = verbatim_lines(FED_SPEC, 'source_key="RIFLGFC')
FED_VALUES = re.findall(r'source_key="(RIFLGFC[^"]*)"', "\n".join(FED_LINES))
FED_ALL = "\n".join(FED_LINES)
FED_ONE = FED_LINES[0]
FED_OFF_BY_ONE = FED_ONE.replace("RIFLGFCM01_N.B", "RIFLGFCM01_N.X")
FED_TOKEN_SLOT = FED_ONE.replace('"RIFLGFCM01_N.B"', f'"{FAKE_TOKEN}"')
FED_DIGEST_SLOT = FED_ONE.replace('"RIFLGFCM01_N.B"', f'"{DIGEST}"')

# Blast radius, computed off the scanner: every source_key value in every provider spec, split by
# whether the new exemption regex matches it. Anything matched that is not one of the eleven Fed
# identifiers would mean the rule silences more than the identifiers it names.
ALL_SOURCE_KEYS: list[str] = []
for module in sorted((REPO / "opendata/data/providers").glob("*/specs.py")):
    ALL_SOURCE_KEYS += re.findall(r'source_key="([^"]*)"', module.read_text(encoding="utf-8"))
EXEMPTED_OTHER_THAN_FED = sorted(
    {v for v in ALL_SOURCE_KEYS if FED_EXEMPT.match(v) and v not in FED_VALUES}
)

VARIANTS = {
    "shipped": SHIPPED,
    "no-third-regex": SHIPPED.replace(THIRD_REGEX, ""),
    "no-sdmx-regex": SHIPPED.replace(SDMX_REGEX, ""),
    "no-fed-regex": SHIPPED.replace(FED_REGEX, ""),
    # dropping one regex out of a [[allowlists]] block is not legal gitleaks (a block must keep at
    # least one check), so this control removes the whole block instead
    "no-digest-allowlist": SHIPPED.split("[[allowlists]]")[0],
}

# (fixture path, content, variant, expected rule ids, label)
ARMS: list[tuple[str, str, str, set[str], str]] = [
    ("census.json", CENSUS, "shipped", set(), "exempted"),
    ("census.json", CENSUS, "no-third-regex", {"generic-api-key"}, "control"),
    ("census.json", CENSUS_OFF_BY_ONE, "shipped", {"generic-api-key"}, "literal not prefix"),
    ("census.json", CENSUS_TOKEN_SLOT, "shipped", {"github-pat"}, "token in slot still reported"),
    ("census.json", CENSUS_MIXED_TOKEN_SLOT, "shipped", {"github-pat"}, "same, capitals spelling"),
    (
        "census.json",
        CENSUS_TOKEN_BODY,
        "shipped",
        {"generic-api-key"},
        "ghp_ prefix is what it skips",
    ),
    (
        "census.json",
        CENSUS_DIGEST_SLOT,
        "shipped",
        {"generic-api-key"},
        "64-hex credential in the exempted slot",
    ),
    ("docs/evidence/m.json", PAIR, "shipped", set(), "C73 exempted"),
    ("docs/evidence/m.json", PAIR, "no-digest-allowlist", {"generic-api-key"}, "C73 control"),
    (
        "docs/evidence/m.json",
        PAIR_PLUS_TOKEN_NEXT_LINE,
        "shipped",
        {"github-pat"},
        "token own line",
    ),
    ("docs/evidence/m.json", PAIR_PLUS_TOKEN_SAME_LINE, "shipped", set(), "BOUNDARY co-located"),
    (
        "docs/evidence/m.json",
        PAIR_PLUS_TOKEN_SAME_LINE,
        "no-digest-allowlist",
        {"generic-api-key", "github-pat"},
        "BOUNDARY control",
    ),
    ("tests/ecb.json", SDMX, "shipped", set(), "C36 exempted"),
    ("tests/ecb.json", SDMX, "no-sdmx-regex", {"generic-api-key"}, "C36 control"),
    ("cfg.json", NON_PATH_DIGEST, "shipped", {"generic-api-key"}, "non-path key"),
    # The H.15 exemption: all eleven real lines at once, then its three boundary arms.
    (SPECS_FIXTURE, FED_ALL, "shipped", set(), "eleven H.15 ids exempted"),
    (SPECS_FIXTURE, FED_ALL, "no-fed-regex", {"generic-api-key"}, "H.15 control"),
    (SPECS_FIXTURE, FED_OFF_BY_ONE, "shipped", {"generic-api-key"}, "one character differs"),
    (SPECS_FIXTURE, FED_TOKEN_SLOT, "shipped", {"github-pat"}, "token in the source_key slot"),
    (SPECS_FIXTURE, FED_DIGEST_SLOT, "shipped", {"generic-api-key"}, "64-hex under source_key"),
]


def scan(fixture: str, content: str, config_text: str) -> set[str]:
    """Commit one fixture in a throwaway repo, scan its history, return the reported rule ids."""
    root = Path(tempfile.mkdtemp(prefix="c75-cf-"))
    try:
        (root / ".gitleaks.toml").write_text(config_text, encoding="utf-8")
        target = root / fixture
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content + "\n", encoding="utf-8")
        for step in (
            ["init", "-q", "."],
            ["-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
            ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fixture"],
        ):
            subprocess.run(  # nosec B603  # noqa: S603
                [GIT, *step],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
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
        rules = (
            {str(f.get("RuleID")) for f in json.loads(report.read_text(encoding="utf-8"))}
            if report.exists()
            else set()
        )
        if code.returncode not in (0, 1):
            raise SystemExit(
                f"gitleaks did not run to a verdict (rc={code.returncode}): {code.stderr[:300]}"
            )
        return rules
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    """Print the unexempted baseline, then score every arm against its declared rule-id set."""
    print("== H.15 exemption surface, read off the tree ==")
    print(f"  source_key lines naming RIFLGFC in {FED_SPEC}: {len(FED_LINES)}")
    print(
        f"  distinct values they carry: {len(set(FED_VALUES))}  all matched by the exemption: "
        f"{all(FED_EXEMPT.match(v) for v in FED_VALUES)}"
    )
    print(
        f"  source_key values across every provider spec: {len(ALL_SOURCE_KEYS)} "
        f"(distinct {len(set(ALL_SOURCE_KEYS))})"
    )
    print(
        f"  values the exemption matches that are NOT one of those Fed identifiers: "
        f"{len(EXEMPTED_OTHER_THAN_FED)} {EXEMPTED_OTHER_THAN_FED}"
    )

    for variant, text in VARIANTS.items():
        if variant != "shipped" and text == SHIPPED:
            raise SystemExit(
                f"variant {variant!r} did not change the config; its expectation would "
                "measure the shipped ruleset and every reading taken from it is a fiction"
            )

    print("== baseline: default ruleset, no allowlists (the denominator every silent arm needs) ==")
    baseline: dict[str, set[str]] = {}
    for fixture, content, _variant, _expected, _label in ARMS:
        if content not in baseline:
            baseline[content] = scan(fixture, content, BASELINE_CONFIG)
    failures = 0
    for fixture, content, _variant, expected, _label in ARMS:
        if not expected and not baseline.get(content):
            print(
                f"     VACUOUS {fixture}: no default rule fires on it, so claiming silence here "
                "measures nothing"
            )
            failures += 1
    seen: set[str] = set()
    for fixture, content, _v, _e, _l in ARMS:
        if content in seen:
            continue
        seen.add(content)
        print(f"     baseline {fixture:22s} rules={sorted(baseline.get(content, set()))}")
    if failures:
        print("COUNTERFACT FAIL: vacuous arm(s)")
        return 1

    print("\n== arms: shipped variants against declared rule-id sets ==")
    bad = 0
    for index, (fixture, content, variant, expected, label) in enumerate(ARMS, start=1):
        rules = scan(fixture, content, VARIANTS[variant])
        state = "OK " if rules == expected else "FAIL"
        if rules != expected:
            bad += 1
        print(
            f"[{index:2d}] {state} {variant:20s} {fixture:22s} "
            f"expect={sorted(expected)} got={sorted(rules)} :: {label}"
        )
    if bad:
        print(f"COUNTERFACT FAIL: {bad} arm(s) disagreed with their declared rule sets")
        return 1
    print(f"COUNTERFACT PASS: {len(ARMS)} arms agreed with their declared rule sets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
