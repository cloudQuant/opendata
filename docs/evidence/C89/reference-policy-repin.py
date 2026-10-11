"""C89 witness: is the reference-policy pin on the acceptance probe a digest change only?

docs/quality/akshare-reference-allowlist.json pins the bytes of
scripts/quality/acceptance_item_probe.py. This round edited that probe twice (it mirrors a 5th
``.gitleaks.toml`` regex, then a comment re-wrap), so the pin is stale and gate member 2
hard-stops on a stale pin. The fix is a digest re-pin, which is only legitimate while the
APPROVED CONTENT is unchanged -- brand names must not become load statements. This instrument
measures that from git and disk, with no hand-typed number:

  * which revision owns the pinned bytes, and how many revisions the path has since;
  * brand-name occurrence lines/counts, pinned vs live;
  * load-shape lines (import / importlib / __import__ spellings), pinned vs live;
  * the repo scanner's own (module, kind) hit multiset on both revisions, via
    scripts/codemod/verify_no_akshare.scan_source rather than a re-implemented regex;
  * changed-line accounting under two口径 (raw diff and text-dedupe), so a "no brand line was
    added" claim states the mechanism that explains any gap between them;
  * an arm that must MOVE: the same counters on a copy with one brand-name import injected.

Read-only: no network, no database, no engine execution, no gate, nothing written.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import subprocess  # nosec B404  # read-only git argv, shell disabled
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO = next(
    ancestor
    for ancestor in Path(__file__).resolve().parents
    if (ancestor / "scripts" / "quality" / "model_capability_census.py").is_file()
)
sys.path.insert(0, str(REPO / "scripts" / "codemod"))
import verify_no_akshare as vna  # noqa: E402

TARGET: Final = "scripts/quality/acceptance_item_probe.py"
POLICY: Final = "docs/quality/akshare-reference-allowlist.json"
#: The digest the C86 clauses left in the policy, carried as a literal so this instrument stays
#: runnable AFTER the re-pin it authorises. Reading the pin from the policy alone made the check
#: self-referential: once the pin equals the (uncommitted) live bytes, no revision owns it and the
#: audit reports "nothing to compare". The policy's CURRENT value is still read from disk and
#: reported below as the post-condition face.
PRIOR_PIN: Final = "51b2266dd60bce24f0580f3644c4cb7ef8b268061f088e09d8cae44a963188bb"
BRAND: Final = re.compile(r"akshare|openbb|yfinance|intrinio|tmx")
#: The two口径 of the same needle. The pin's accumulated review clauses quote the
#: case-insensitive readings (144 lines / 172 uses on the bytes this round pins), while a
#: case-sensitive reader on those very same bytes reports 138 / 163. Both are published so the
#: next round can tell a moved number apart from a renamed口径.
AKSHARE_CI: Final = re.compile(r"akshare", re.IGNORECASE)
OPENBB_CI: Final = re.compile(r"openbb", re.IGNORECASE)
#: 加载形态 AND 品牌名 simultaneously -- the narrow口径 whose absolute value should stay 0/1 for
#: a measurement tool that only ever names brands inside string literals.
BRAND_CI: Final = re.compile(r"akshare|openbb|yfinance", re.IGNORECASE)
LOAD_SHAPE: Final = re.compile(r"^\s*(?:import|from)\s+|importlib\.import_module\(|__import__\(")


def git(*args: str) -> str:
    """Run a read-only git command and return stdout."""
    return subprocess.run(  # nosec B603, B607  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def sha(text: str) -> str:
    """sha256 over the utf-8 bytes of a text blob."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def brand_bearing_lines(lines: list[str]) -> list[str]:
    """Lines carrying a source-brand name, verbatim."""
    return [line for line in lines if BRAND.search(line)]


def pin_entry() -> dict[str, object]:
    """The policy entry whose ``path`` is the probe."""
    policy = json.loads((REPO / POLICY).read_text(encoding="utf-8"))
    return next(e for e in policy["entries"] if e["path"] == TARGET)


def owners(digest: str) -> tuple[str, list[str]]:
    """The revision owning the pinned bytes, and every revision of the path after it."""
    revs = git("log", "--format=%H", "--", TARGET).split()
    index = {rev: sha(git("show", f"{rev}:{TARGET}")) for rev in revs}
    owner = next((rev for rev in revs if index[rev] == digest), "")
    if not owner:
        return "", revs
    return owner, revs[: revs.index(owner)]


@dataclass(frozen=True)
class Face:
    """One file's measured counters, typed so the damage arm needs no cast to subtract them."""

    akshare_lines: int
    akshare_uses: int
    akshare_lines_ci: int
    akshare_uses_ci: int
    openbb_lines: int
    openbb_uses: int
    openbb_lines_ci: int
    openbb_uses_ci: int
    load_shape_lines: int
    brand_load_lines: int
    brand_lines: int
    scanner: list[str]

    def numeric(self) -> dict[str, int]:
        """The int faces, in the order and under the names this audit prints and compares."""
        return {
            "akshare_lines": self.akshare_lines,
            "akshare_uses": self.akshare_uses,
            "akshare_lines_ci": self.akshare_lines_ci,
            "akshare_uses_ci": self.akshare_uses_ci,
            "openbb_lines": self.openbb_lines,
            "openbb_uses": self.openbb_uses,
            "openbb_lines_ci": self.openbb_lines_ci,
            "openbb_uses_ci": self.openbb_uses_ci,
            "load_shape_lines": self.load_shape_lines,
            "brand_load_lines": self.brand_load_lines,
        }


def faces(text: str) -> Face:
    """Compute the counters this policy compares from one file's bytes."""
    lines = text.splitlines()
    hits = vna.scan_source(text, TARGET)
    return Face(
        akshare_lines=sum(1 for line in lines if "akshare" in line),
        akshare_uses=text.count("akshare"),
        akshare_lines_ci=sum(1 for line in lines if AKSHARE_CI.search(line)),
        akshare_uses_ci=len(AKSHARE_CI.findall(text)),
        openbb_lines=sum(1 for line in lines if "openbb" in line),
        openbb_uses=text.count("openbb"),
        openbb_lines_ci=sum(1 for line in lines if OPENBB_CI.search(line)),
        openbb_uses_ci=len(OPENBB_CI.findall(text)),
        load_shape_lines=sum(1 for line in lines if LOAD_SHAPE.search(line)),
        brand_load_lines=sum(
            1 for line in lines if LOAD_SHAPE.search(line) and BRAND_CI.search(line)
        ),
        brand_lines=len(brand_bearing_lines(lines)),
        scanner=sorted(f"{f.module}|{f.kind}" for f in hits),
    )


def show(name: str, face: Face) -> None:
    """Print one face on a single line, keeping the scanner multiset members visible."""
    counts = " ".join(f"{key}={value}" for key, value in face.numeric().items())
    print(f"  {name:8s} {counts}")
    print(f"         shape={face.brand_lines} scanner={len(face.scanner)} members={face.scanner}")


def changed_lines(pinned: str, live: str) -> tuple[list[str], list[str]]:
    """Added and deleted lines under the same口径 as `git diff --numstat` (difflib, no context)."""
    added: list[str] = []
    deleted: list[str] = []
    for line in difflib.unified_diff(pinned.splitlines(), live.splitlines(), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:].strip())
        elif line.startswith("-") and not line.startswith("---"):
            deleted.append(line[1:].strip())
    return added, deleted


def main() -> int:
    """Report the pin face and the diff face, plus the damage arm behind every zero."""
    entry = pin_entry()
    current_pin = str(entry["sha256"])
    owner, after = owners(PRIOR_PIN)
    live_text = (REPO / TARGET).read_text(encoding="utf-8")
    live_sha = sha(live_text)
    pinned_text = git("show", f"{owner}:{TARGET}") if owner else ""

    print("== pin identity ==")
    print(f"  target        {TARGET}")
    print(f"  prior pin (this audit's denominator) {PRIOR_PIN}")
    print(f"  live sha256                          {live_sha}   <-- the value the re-pin carries")
    print(f"  policy sha256 on disk right now      {current_pin}")
    print(
        f"  post-condition -- policy now pins the live bytes: {current_pin == live_sha} "
        f"(False here means the re-pin has not been applied yet, which is the pre-pin reading)"
    )
    print(f"  owner revision of the prior pin: {owner[:7] or 'NOT FOUND in any revision'}")
    print(f"  revisions of this path after that owner: {len(after)} {[r[:7] for r in after]}")
    print(f"  working tree == HEAD for this path: {sha(git('show', f'HEAD:{TARGET}')) == live_sha}")
    if not owner:
        print("AUDIT FAIL: the prior pinned bytes exist in no revision, so there is no denominator")
        return 1

    print("== content faces (pinned bytes vs live bytes) ==")
    pf, lf = faces(pinned_text), faces(live_text)
    show("pinned", pf)
    show("live", lf)
    same_scanner = pf.scanner == lf.scanner
    print(f"  scanner (module,kind) multiset identical: {same_scanner}")
    carriers = [
        line for line in live_text.splitlines() if LOAD_SHAPE.search(line) and BRAND_CI.search(line)
    ]
    print(f"  brand-load carriers (verbatim, live bytes, {len(carriers)}): {carriers}")

    print("== changed-line accounting ==")
    added, deleted = changed_lines(pinned_text, live_text)
    pinned_bodies = {line.strip() for line in pinned_text.splitlines()}
    dedupe = [t for t in added if t not in pinned_bodies]
    brand_added = brand_bearing_lines(added)
    brand_deleted = brand_bearing_lines(deleted)
    nsec_echo = [
        t
        for t in dedupe
        if any(t == f"{d}  # nosec B603" or t.startswith(f"{d}  #") for d in deleted)
    ]
    print(
        f"  diff口径 +{len(added)} / -{len(deleted)}; text-dedupe口径 +{len(dedupe)}; "
        f"gap explained by same-text-already-in-pin: {len(added) - len(dedupe)}"
    )
    print(f"  brand-name-bearing added / deleted lines: {len(brand_added)}/{len(brand_deleted)}")
    print(
        "  added lines that are a deleted line plus a nosec tail comment: "
        f"{len(nsec_echo)}/{len(dedupe)}"
    )
    print(f"  brand lines located (verbatim, up to 12): {brand_added[:12]}")
    loads = brand_bearing_lines(
        [t for t in added if LOAD_SHAPE.search(t) and not t.startswith("#")]
    )
    print(f"  added lines that are BOTH a load shape and brand-bearing: {len(loads)} {loads}")

    print("== damage arm: the counters must move the way they claim they can ==")
    damaged = faces(live_text + "\nimport akshare as aks  # injected by the audit\n")
    show("live", lf)
    show("damaged", damaged)
    expected_delta = {
        "akshare_lines": 1,
        "akshare_uses": 1,
        "akshare_lines_ci": 1,
        "openbb_lines": 0,
        "load_shape_lines": 1,
        "brand_load_lines": 1,
    }
    wrong: list[str] = []
    damaged_counts, live_counts = damaged.numeric(), lf.numeric()
    for key, delta in expected_delta.items():
        got = damaged_counts[key] - live_counts[key]
        if got != delta:
            wrong.append(f"{key}: expected +{delta}, got +{got}")
    new_members = [m for m in damaged.scanner if m not in set(lf.scanner)]
    if new_members != ["akshare|import"]:
        wrong.append(f"scanner new members: expected ['akshare|import'], got {new_members}")
    for line in wrong:
        print(f"  ARM DISAGREED {line}")
    if wrong:
        print("AUDIT FAIL: the damage arm did not produce its declared deltas")
        return 1
    print(
        f"  arm MOVED as declared: +{expected_delta['akshare_lines']} akshare line, "
        f"+{expected_delta['brand_load_lines']} brand-load line, "
        f"+1 scanner member {new_members}"
    )
    if not same_scanner:
        print("AUDIT FAIL: scanner hits differ -- this is a new approval, not a digest re-pin")
        return 1
    scanner_count = len(lf.scanner)
    print(
        f"VERDICT: re-pin under the same approval. scanner={scanner_count} identical, "
        f"any-import-line {pf.load_shape_lines} -> {lf.load_shape_lines}, "
        f"brand-load-line {pf.brand_load_lines} -> {lf.brand_load_lines} "
        f"(damage arm: {lf.brand_load_lines} -> {damaged.brand_load_lines}), "
        f"brand added={len(brand_added)} deleted={len(brand_deleted)}, "
        f"brand load-shape added={len(loads)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
