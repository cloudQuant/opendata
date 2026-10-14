"""C101 witness: may these stale reference-policy digests be re-pinned, and on what evidence?

``docs/quality/akshare-reference-allowlist.json`` pins the sha256 of every path that is allowed,
by C65 review, to name an upstream library. Eight registered paths were rewritten by this
round's provider work, so gate member 2 refuses to load the policy at all
(``refusing to freeze an unusable reference policy: stale sha256: ...``) and never reaches the
scan-surface re-freeze. Re-pinning a digest is only legitimate while the *registered claim* about
the file still holds: the review says "this file names upstream libraries as data, it does not
depend on them", so the test is whether a dependency edge was added -- not whether bytes moved.

The stale set is derived from the repo's own judge (``verify_no_akshare``), never hand-typed, and
every registered path prints:

  * the pinned digest, the ``HEAD`` blob digest and the live digest, and which of the two the pin
    actually describes (a pin equal to neither is a pin nobody can re-derive);
  * the scanner's own finding multiset keyed by (file, module, kind) on ``HEAD`` bytes vs live
    bytes, via ``scan_source`` -- so a moved line is not a moved claim while a new ``import`` is;
  * load-shape line counts (``import`` / ``from`` / ``importlib.import_module(`` / ``__import__(``)
    on both revisions, as a second, regex-level witness of the same claim;
  * the verdict rule read off those numbers: no ``import``-kind and no ``dynamic``-kind finding
    may be added. Anything else is printed as a delta, not silenced.

Two arms must MOVE, because a witness that cannot fail proves nothing: injecting ``import
akshare`` into a live copy must turn that path's verdict to REJECT, and corrupting one hex
character of every pin must still be reported by the judge as ``stale sha256``.

Read-only by default. ``--apply`` rewrites only the stale ``sha256`` fields (byte-targeted
replacement, so purpose/category/reviewer/ast_exceptions cannot drift) and then re-asks the repo's
judge for the stale set, which must come back empty.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess  # nosec B404  # fixed argv, read-only git, shell disabled
import sys
from collections import Counter
from pathlib import Path
from typing import Final

REPO = next(
    ancestor
    for ancestor in Path(__file__).resolve().parents
    if (ancestor / "scripts" / "quality" / "model_capability_census.py").is_file()
)
sys.path.insert(0, str(REPO / "scripts" / "codemod"))
import verify_no_akshare as vna  # noqa: E402

POLICY: Final = "docs/quality/akshare-reference-allowlist.json"
LOAD_SHAPE: Final = re.compile(r"^\s*(?:import|from)\s+|importlib\.import_module\(|__import__\(")
BRAND_NAME: Final = re.compile(r"akshare|openbb|yfinance|intrinio|tmx", re.IGNORECASE)
#: kinds that mean "this file now depends on the upstream library" rather than "it names it".
DEPENDENCY_KINDS: Final = ("import", "dynamic")


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _git_show(rev: str, rel: str) -> str | None:
    proc = subprocess.run(  # nosec B603  # fixed argv, repo-relative path
        ["git", "show", f"{rev}:{rel}"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    return proc.stdout if proc.returncode == 0 else None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _findings(text: str, rel: str) -> Counter:
    return Counter(f.key() for f in vna.scan_source(text, rel))


def _lines_matching(text: str, needle: re.Pattern[str]) -> int:
    return sum(1 for line in text.splitlines() if needle.search(line))


def _stale_paths(raw: dict) -> list[str]:
    problems = vna.reference_policy_problems(raw, REPO)
    return sorted(p.split(": ", 1)[1] for p in problems if p.startswith("stale sha256:"))


def judge_path(rel: str, pin: str) -> dict:
    live_text = _read(rel)
    head_text = _git_show("HEAD", rel)
    head_digest = _digest(head_text) if head_text is not None else ""
    live_digest = _digest(live_text)
    live_findings = _findings(live_text, rel)
    head_findings = _findings(head_text, rel) if head_text is not None else Counter()
    added = live_findings - head_findings
    removed = head_findings - live_findings
    dep_added = {key: count for key, count in added.items() if key[2] in DEPENDENCY_KINDS}
    return {
        "path": rel,
        "pin": pin,
        "head_digest": head_digest,
        "live_digest": live_digest,
        "pin_equals_head": bool(head_digest) and head_digest == pin,
        "pin_equals_live": live_digest == pin,
        "findings_head": sum(head_findings.values()),
        "findings_live": sum(live_findings.values()),
        "findings_added": dict(sorted(added.items())),
        "findings_removed": dict(sorted(removed.items())),
        "dependency_added": dep_added,
        "brand_lines": (_lines_matching(head_text, BRAND_NAME) if head_text else -1,
                        _lines_matching(live_text, BRAND_NAME)),
        "load_shape_lines": (_lines_matching(head_text, LOAD_SHAPE) if head_text else -1,
                             _lines_matching(live_text, LOAD_SHAPE)),
        "verdict": "REJECT" if dep_added else "REPIN_OK",
    }


def arm_injected_dependency(rel: str) -> str:
    """Inject one real import and show the verdict rule bites on the same path."""
    live_text = _read(rel)
    tampered = f"import akshare  # injected by the C101 arm\n{live_text}"
    added = _findings(tampered, rel) - _findings(live_text, rel)
    dep = {key: count for key, count in added.items() if key[2] in DEPENDENCY_KINDS}
    return f"REJECT(dep={dep})" if dep else "REPIN_OK"


def arm_corrupted_pins(raw: dict) -> int:
    """Corrupt every pin by one hex character and count the judge's stale readings."""
    tampered = json.loads(json.dumps(raw))
    for entry in tampered["entries"]:
        digest = entry["sha256"]
        entry["sha256"] = ("0" if digest[0] != "0" else "1") + digest[1:]
    problems = vna.reference_policy_problems(tampered, REPO)
    return sum(1 for p in problems if p.startswith("stale sha256:"))


def main() -> int:
    apply = "--apply" in sys.argv
    raw = json.loads(_read(POLICY))
    pins = {entry["path"]: entry["sha256"] for entry in raw["entries"]}
    stale = _stale_paths(raw)
    print(f"population: registered_entries={len(raw['entries'])} stale_paths={len(stale)}")
    for rel in stale:
        print(f"  stale: {rel}")
    rows = [judge_path(rel, pins[rel]) for rel in stale]
    for row in rows:
        print(
            f"\n== {row['path']}\n"
            f"   pin={row['pin'][:12]} head={row['head_digest'][:12]} "
            f"live={row['live_digest'][:12]} "
            f"pin==head={row['pin_equals_head']} pin==live={row['pin_equals_live']}\n"
            f"   findings head/live={row['findings_head']}/{row['findings_live']} "
            f"added={row['findings_added']} removed={row['findings_removed']}\n"
            f"   dependency_edges_added={row['dependency_added']}\n"
            f"   brand_lines head/live={row['brand_lines'][0]}/{row['brand_lines'][1]} "
            f"load_shape head/live={row['load_shape_lines'][0]}/{row['load_shape_lines'][1]}\n"
            f"   verdict: {row['verdict']}"
        )
    repin_ok = sum(1 for row in rows if row["verdict"] == "REPIN_OK")
    dep_edges = sum(len(row["dependency_added"]) for row in rows)
    print(f"\nface: stale={len(rows)} repin_ok={repin_ok} rejected={len(rows) - repin_ok} "
          f"dependency_edges_added={dep_edges}")

    if stale:
        arm = arm_injected_dependency(stale[0])
        print(f"arm import-injection on {stale[0]}: {arm} (expected REJECT) "
              f"-> {'PASS' if arm.startswith('REJECT') else 'FAIL'}")
    corrupted = arm_corrupted_pins(raw)
    print(f"arm pin-corruption: stale_sha256_readings={corrupted} of {len(raw['entries'])} "
          f"(expected =={len(raw['entries'])}) -> {'PASS' if corrupted == len(raw['entries']) else 'FAIL'}")

    if not apply:
        print("\nread-only: policy untouched. Re-run with --apply once every verdict is REPIN_OK.")
        return 0
    if not rows or repin_ok != len(rows):
        print("FAIL: refusing to apply -- at least one path is not REPIN_OK (or nothing was stale).")
        return 1

    text = _read(POLICY)
    for row in rows:
        needle = f'"sha256": "{row["pin"]}"'
        if text.count(needle) != 1:
            print(f"FAIL: pin field for {row['path']} is not uniquely replaceable "
                  f"(count={text.count(needle)})")
            return 1
        text = text.replace(needle, f'"sha256": "{row["live_digest"]}"')
    (REPO / POLICY).write_text(text, encoding="utf-8")
    reparsed = json.loads(_read(POLICY))
    remaining = _stale_paths(reparsed)
    print(f"\napplied re-pin for {len(rows)} path(s)")
    print(f"post: stale_sha256_remaining={len(remaining)} -> "
          f"{'PASS' if not remaining else 'FAIL ' + str(remaining)}")
    print(f"post: registered_entries={len(reparsed['entries'])} (was {len(raw['entries'])})")
    return 0 if not remaining else 1


if __name__ == "__main__":
    raise SystemExit(main())
