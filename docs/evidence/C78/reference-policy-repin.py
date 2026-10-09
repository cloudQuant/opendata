"""Re-derive the C78 reference-policy re-pin and audit the drift it records.

Gate run 6 refused to start: member ``zero-dep-check`` reported two stale pins. The fix is a
digest re-pin, which is only honest if the *approval content* is unchanged, so this screen
re-measures that content with the repository's own scanner instead of asserting it.

A clean reading is only worth printing next to an arm that breaks: section 2 feeds the same
comparison a deliberately tampered source and requires it to report problems, so the zero in
section 1 is a measurement and not a judge that cannot fail.

Inputs are two revision constants -- the revision whose blob equals the old pin, and the
revision that moved the bytes. Every figure printed is computed from git and disk, so the screen
re-runs after the re-pin commit too: the only line whose reading depends on commit state is the one
labelled "reads True only before the re-pin commit", and the durable form of that claim is the
printed revision order (the newest revision carrying the new pin is newer than the newest one
carrying the old pin).

Run: /usr/local/bin/python3.11 -u docs/evidence/C78/reference-policy-repin.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess  # nosec B404
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import types

ROOT = Path(__file__).resolve().parents[3]
POLICY = "docs/quality/akshare-reference-allowlist.json"
VERIFIER = ROOT / "scripts/codemod/verify_no_akshare.py"
_git_path = shutil.which("git")
if _git_path is None:
    raise SystemExit("git is not on PATH; a pin can only be audited against its revision")
GIT: str = _git_path

# path -> (rev whose blob is the old pin, rev that moved the bytes)
CASES: dict[str, tuple[str, str]] = {
    "scripts/quality/a2_check.py": ("3f05f63", "4960738"),
    "scripts/quality/acceptance_item_probe.py": ("60148ce", "5e335ab"),
}

INVARIANT_FACES = ("brand_lines", "brand_occurrences", "load_shape_lines")
LOAD_SHAPE = re.compile(
    r"(import\s+akshare|from\s+akshare|import_module\(\s*['\"]akshare|__import__\(\s*['\"]akshare)"
)
BRAND = re.compile("akshare", re.IGNORECASE)
TAMPERED_LOAD = "\nimport akshare\n"
TAMPERED_STRING = '\nvalue = "akshare.fred"\n'


def git(*args: str) -> bytes:
    """Run git on stdout only, with the repo as cwd and a fixed executable."""
    process = subprocess.run(  # nosec B603  # noqa: S603 - no shell, argv is repo paths
        [GIT, *args],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return process.stdout


def sha256(data: bytes) -> str:
    """Return the hex digest of the bytes."""
    return hashlib.sha256(data).hexdigest()


def load_scanner() -> types.ModuleType:
    """Import the repository's own reference scanner by path."""
    spec = importlib.util.spec_from_file_location("vna", VERIFIER)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load the repository scanner")
    module = importlib.util.module_from_spec(spec)
    sys.modules["vna"] = module
    spec.loader.exec_module(module)
    return module


def policy_entries(text: str) -> dict[str, dict[str, object]]:
    """Map registered path -> policy entry from policy JSON text."""
    return {str(entry["path"]): entry for entry in json.loads(text)["entries"]}


def hits(scanner: types.ModuleType, source: str, name: str) -> Counter[tuple[str, str]]:
    """Return the scanner's (module, kind) finding counts for one source."""
    return Counter((finding.module, finding.kind) for finding in scanner.scan_source(source, name))


def brand_face(source: str) -> dict[str, int]:
    """Count the faces a re-pin has to leave untouched, plus total line count."""
    lines = source.splitlines()
    return {
        "lines": len(lines),
        "brand_lines": sum(1 for line in lines if BRAND.search(line)),
        "brand_occurrences": len(BRAND.findall(source)),
        "load_shape_lines": sum(1 for line in lines if LOAD_SHAPE.search(line)),
    }


def compare(
    path: str,
    scanner: types.ModuleType,
    old_source: str,
    live_source: str,
) -> list[str]:
    """Report every way the approval content moved between two versions of a file."""
    problems: list[str] = []
    old_hits, live_hits = hits(scanner, old_source, path), hits(scanner, live_source, path)
    if old_hits != live_hits:
        problems.append(f"{path}: finding multiset {dict(old_hits)} -> {dict(live_hits)}")
    old_face, live_face = brand_face(old_source), brand_face(live_source)
    problems.extend(
        f"{path}: {key} {old_face[key]} -> {live_face[key]}"
        for key in INVARIANT_FACES
        if old_face[key] != live_face[key]
    )
    return problems


def face_lines(old_source: str, live_source: str) -> tuple[int, int, int, int]:
    """Return added/removed line counts and how many of them carry the brand."""
    old_set, live_set = set(old_source.splitlines()), set(live_source.splitlines())
    added = [line for line in live_source.splitlines() if line not in old_set]
    removed = [line for line in old_source.splitlines() if line not in live_set]
    return (
        len(added),
        len(removed),
        sum(1 for line in added if BRAND.search(line)),
        sum(1 for line in removed if BRAND.search(line)),
    )


def main() -> int:
    """Print every face and refuse a zero that no tamper can produce."""
    scanner = load_scanner()
    policy_raw = json.loads((ROOT / POLICY).read_text(encoding="utf-8"))
    work = policy_entries(json.dumps(policy_raw))
    head = policy_entries(git("show", f"HEAD:{POLICY}").decode("utf-8"))
    print("policy entries (working):", len(work), "| (committed HEAD):", len(head))

    real: list[str] = []
    for path, (pin_rev, move_rev) in CASES.items():
        old_bytes = git("show", f"{pin_rev}:{path}")
        disk_bytes = (ROOT / path).read_bytes()
        old_digest, disk_digest = sha256(old_bytes), sha256(disk_bytes)
        old_source, live_source = old_bytes.decode("utf-8"), disk_bytes.decode("utf-8")
        policy_revs = git("log", "--format=%H", "--", POLICY).decode().split()
        texts = {rev: git("show", f"{rev}:{POLICY}").decode("utf-8") for rev in policy_revs}
        carrier = [rev for rev in policy_revs if old_digest in texts[rev]]
        new_carrier = [rev for rev in policy_revs if disk_digest in texts[rev]]
        face_old, face_live = brand_face(old_source), brand_face(live_source)
        added, removed, added_brand, removed_brand = face_lines(old_source, live_source)

        print("=" * 72)
        print("path:", path)
        print(f"old pin (hash of blob at {pin_rev}):", old_digest)
        print(f"disk (hash of {move_rev} blob == disk):", disk_digest)
        moved_ok = sha256(git("show", f"{move_rev}:{path}")) == disk_digest
        print("moved-by-rev blob == disk:", moved_ok)
        print("HEAD blob == disk:", sha256(git("show", f"HEAD:{path}")) == disk_digest)
        print("policy entry now pins == disk:", str(work[path]["sha256"]) == disk_digest)
        print(
            "HEAD policy still carries the old pin (reads True only before the re-pin commit):",
            str(head[path]["sha256"]) == old_digest,
        )
        print("policy revisions carrying the old pin:", len(carrier))
        print("policy revisions carrying the new pin:", len(new_carrier))
        order_ok = (
            bool(carrier)
            and bool(new_carrier)
            and policy_revs.index(new_carrier[0]) < (policy_revs.index(carrier[0]))
        )
        print(
            "newest carrier positions (0 = HEAD), new vs old:",
            policy_revs.index(new_carrier[0]) if new_carrier else "none",
            "/",
            policy_revs.index(carrier[0]) if carrier else "none",
            "-> re-pin is a committed revision newer than the stale one:",
            order_ok,
        )
        print(
            "findings old/live:",
            sum(hits(scanner, old_source, path).values()),
            "/",
            sum(hits(scanner, live_source, path).values()),
        )
        for key in ("lines", *INVARIANT_FACES):
            print(f"  {key}: old={face_old[key]} live={face_live[key]}")
        print(
            "changed lines: added",
            added,
            "removed",
            removed,
            "| brand-carrying added",
            added_brand,
            "removed",
            removed_brand,
        )

        real.extend(compare(path, scanner, old_source, live_source))
        if str(work[path]["sha256"]) != disk_digest:
            real.append(f"{path}: working policy does not pin the disk digest")
        if not carrier:
            real.append(f"{path}: no policy revision carries the old pin")
        if not order_ok:
            real.append(f"{path}: the re-pin is not a revision newer than the stale pin")

    # Control A: the repository's own pin judge, on the real policy and on each forged digest.
    # The forgery targets the two paths this round re-pinned, so the printed zero in the real arm
    # is bounded by a non-zero on the very paths the reading claims are clean.
    judge_out = scanner.reference_policy_problems(policy_raw, ROOT)
    stale_now = [problem for problem in judge_out if "stale sha256" in problem]
    forged_arms: list[tuple[str, list[str]]] = []
    for victim in CASES:
        tampered = json.loads(json.dumps(policy_raw))
        for entry in tampered["entries"]:
            if entry["path"] == victim:
                entry["sha256"] = sha256(f"forged-for-{victim}".encode())
        forged_arms.append(
            (victim, [p for p in scanner.reference_policy_problems(tampered, ROOT) if "stale" in p])
        )
    print("=" * 72)
    print("CONTROL A -- reference_policy_problems(), the predicate that stopped gate run 6:")
    print("  stale sha256 on the re-pinned policy:", len(stale_now), stale_now)
    for victim, reported in forged_arms:
        print(f"  forged {victim}: problems {len(reported)} {reported}")
    print("  every problem reported by the real policy:", len(judge_out), judge_out)

    # Control B: the content comparison used above must be able to see a new reference.
    base = git("show", f"{CASES['scripts/quality/a2_check.py'][0]}:scripts/quality/a2_check.py")
    control = compare(
        "control:tampered",
        scanner,
        base.decode("utf-8"),
        base.decode("utf-8") + TAMPERED_LOAD + TAMPERED_STRING,
    )
    print("CONTROL B -- compare() on the pin bytes plus one import and one module path:")
    print("  problems reported:", len(control))
    for problem in control:
        print("   -", problem)

    print("=" * 72)
    print("REAL problems:", len(real), real)
    bitten = sum(1 for _, reported in forged_arms if len(reported) == 1)
    print("CONTROL A arms that bit:", bitten, "of", len(forged_arms))
    print("CONTROL B problems:", len(control))
    arms_bite = all(
        len(reported) == 1 and victim in reported[0] for victim, reported in forged_arms
    )
    holds = not real and not stale_now and arms_bite and bool(control)
    verdict = "SUPPORTED: digest re-pin under unchanged approval content" if holds else "REFUTED"
    print("VERDICT:", verdict)
    return 0 if holds else 1


if __name__ == "__main__":
    raise SystemExit(main())
