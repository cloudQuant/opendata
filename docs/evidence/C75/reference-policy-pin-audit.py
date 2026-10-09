"""C75 reference-policy pin audit: is a digest drift a hash refresh or a review?

`make gate` member 2 (`zero-dep-check`) refuses to re-freeze while any registered path's digest
has moved
(`scripts/codemod/verify_no_akshare.py:600-603`), and the tool is right to refuse: a hash pin is
the only tamper evidence that reviewed content is still the reviewed content. So the repair
decision needs two measured facts, not a judgement: does the pinned content still resolve to a
revision in this repo's history, and are the `akshare`-bearing lines identical between those bytes
and the live bytes?

The instrument calls the repo's own judge for the authoritative problem list and recomputes every
digest itself, so the two readings must agree; an in-memory control arm corrupts one pin and then
re-reads the whole policy, which is what makes a printed "0 problems" a measurement rather than a
silence. Two arms exist because of how this round actually ran:

* a pin refreshed before its commit equals the *staged* bytes, and history cannot resolve that, so
  the audit asks the index too. Without the second carrier the transient state printed "pin
  matches no revision -> needs a review", which is the same misreading this file exists to catch.
* the corrupt-one-pin control follows the drift list, so after the repair it corrupts an unrelated
  entry. The re-arm arm forges only the pin this round moved and refuses to call the zero clean
  unless the judge names exactly that path.

Nothing here writes: the shipped repair is one JSON field, made by hand and re-measured.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess  # nosec B404 - the arms below read pinned blobs out of git history
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
POLICY = REPO / "docs/quality/akshare-reference-allowlist.json"
sys.path.insert(0, str(REPO / "scripts/codemod"))

from verify_no_akshare import reference_policy_problems  # noqa: E402

_git_path = shutil.which("git")
if _git_path is None:  # pragma: no cover - the arms below read history, so they would run nothing
    raise SystemExit("git is not on PATH; a pin can only be audited against its revision")
GIT: str = _git_path

#: The entry this round moved. The control arm follows the drift list, so once the drift is
#: repaired it corrupts an unrelated entry and the printed "0 problems" would be a silence about
#: the very path the reading claims is clean -- this name keeps the control pointed at it.
REPINNED = "scripts/quality/acceptance_item_probe.py"


def digest(data: bytes) -> str:
    """sha256 as lowercase hex, the same encoding the policy pins and the judge compares."""
    return hashlib.sha256(data).hexdigest()


def git(*args: str) -> bytes:
    """Run git on stdout/stderr only, with the repo as cwd and a fixed executable."""
    process = subprocess.run(  # nosec B603  # noqa: S603 - no shell, argv is repo paths
        [GIT, *args],
        cwd=REPO,
        capture_output=True,
        check=True,
    )
    return process.stdout


def occurrences(text: str) -> list[str]:
    """Sorted multiset of the lines that name akshare, independent of where they sit."""
    return sorted(line.strip() for line in text.splitlines() if "akshare" in line.casefold())


def revision_holding(rel: str, want: str) -> tuple[str, str] | None:
    """The first revision whose blob for `rel` hashes to `want`, as (short sha, source)."""
    for rev in git("log", "--format=%H", "--", rel).decode("utf-8").split():
        try:
            blob = git("show", f"{rev}:{rel}")
        except subprocess.CalledProcessError:
            continue
        if digest(blob) == want:
            return rev[:12], blob.decode("utf-8")
    return None


def staged_blob(rel: str) -> str | None:
    """The index blob for `rel`, or None when the path is not staged.

    A re-pin made before its commit lands equals the *staged* bytes, so history alone cannot
    resolve it: the first form of this audit read that transient state as "pin matches no
    revision -> needs a review", which is the same misreading this file exists to catch (a pin is
    evidence about content, and the content here was sitting in the index). Naming the index as a
    second carrier decides the state instead of vetoing it; a pin matching neither history nor the
    index still falls through to the review branch.
    """
    try:
        return git("show", f":{rel}").decode("utf-8")
    except subprocess.CalledProcessError:
        return None


def judge(raw: object) -> list[str]:
    """The gate's own problem list for this policy document, from the shipped validator."""
    return list(reference_policy_problems(raw, REPO))


def stale_paths(problems: list[str]) -> list[str]:
    """Just the paths the judge calls a stale digest, so the census can be compared to it."""
    return sorted(p.split(": ", 1)[1] for p in problems if p.startswith("stale sha256: "))


def main() -> int:
    """Print the drift census, resolve each drift against history, and control the judge."""
    raw: dict[str, Any] = json.loads(POLICY.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = raw["entries"]
    print(f"repo={REPO}")

    problems = judge(raw)
    print(f"judge_problems={len(problems)}")
    for line in problems:
        print(f"  JUDGE: {line}")

    live_matches = 0
    drift: list[dict[str, Any]] = []
    missing: list[str] = []
    mine: list[str] = []
    for entry in entries:
        path = REPO / str(entry["path"])
        if not path.is_file():
            missing.append(str(entry["path"]))
            continue
        if digest(path.read_bytes()) == entry["sha256"]:
            live_matches += 1
        else:
            drift.append(entry)
            mine.append(str(entry["path"]))
    print(f"entries={len(entries)}")
    print(f"live_digest_matches={live_matches}")
    print(f"digest_drift={len(drift)}")
    print(f"missing_from_tree={len(missing)}")
    for name in missing:
        print(f"  MISSING: {name}")
    agrees = sorted(mine) == stale_paths(problems)
    print(f"census_agrees_with_judge={agrees}")

    unresolved = 0
    changed = 0
    staged_pending = 0
    for entry in drift:
        rel = str(entry["path"])
        found = revision_holding(rel, str(entry["sha256"]))
        if found is None:
            index_text = staged_blob(rel)
            if index_text is None or digest(index_text.encode("utf-8")) != str(entry["sha256"]):
                unresolved += 1
                print(
                    f"  DRIFT {rel}: pin matches no revision and is not the staged blob "
                    "-> needs a review, not a hash refresh"
                )
                continue
            rev = "INDEX(staged)"
            pinned_source = index_text
            staged_pending += 1
        else:
            rev, pinned_source = found
        live_source = (REPO / rel).read_text(encoding="utf-8")
        pinned_occ = occurrences(pinned_source)
        live_occ = occurrences(live_source)
        same = pinned_occ == live_occ
        if not same:
            changed += 1
        print(
            f"  DRIFT {rel}: pin == {rev} occurrences_pinned={len(pinned_occ)} "
            f"occurrences_live={len(live_occ)} identical={same}"
        )
        for line in set(pinned_occ) ^ set(live_occ):
            print(f"    OCCURRENCE DIFF: {line[:120]}")

    print(f"pin_resolved_in_staged_blob={staged_pending}")
    print(f"pin_resolved_nowhere={unresolved}")

    # Corrupt the entry this round actually moved, when there is one: the arm has to prove the
    # judge is live on the same path the reading claims is clean, not on an unrelated entry.
    target = drift[0] if drift else entries[0]
    control = copy.deepcopy(raw)
    wrong = digest(b"c75 control arm, never a registered digest")
    for entry in control["entries"]:
        if entry["path"] == target["path"]:
            entry["sha256"] = wrong
    corrupted = stale_paths(judge(control))
    print(f"control_target={target['path']}")
    print(f"control_corrupt_one_pin_problems={len(corrupted)}")
    print(f"control_corrupt_flags_only_target={corrupted == [str(target['path'])]}")

    repaired = copy.deepcopy(raw)
    for entry in repaired["entries"]:
        path = REPO / str(entry["path"])
        if path.is_file():
            entry["sha256"] = digest(path.read_bytes())
    after = judge(repaired)
    print(f"control_hypothetical_full_repin_problems={len(after)}")
    print(
        "  (arm only: it shows nothing but hash drift separates this tree from the policy; the "
        "shipped repair re-pins ONLY the entries audited above)"
    )
    for line in after:
        print(f"  RESIDUAL: {line}")

    # Re-arm the control on the path this round moved, whether or not that path is still drifting:
    # a repaired pin leaves the corrupted entry unrelated, and only this arm can show that the
    # printed zero is the judge answering about acceptance_item_probe.py.
    rearm = copy.deepcopy(raw)
    for entry in rearm["entries"]:
        if entry["path"] == REPINNED:
            entry["sha256"] = wrong
    rearmed = stale_paths(judge(rearm))
    rearm_ok = rearmed == [REPINNED]
    print(f"control_rearm_target={REPINNED}")
    print(f"control_rearm_problems={len(rearmed)}")
    print(f"control_rearm_flags_only_that_path={rearm_ok}")

    if not agrees:
        print("VERDICT: instrument disagreement - the census and the judge do not see one tree")
        return 1
    if not rearm_ok:
        print("VERDICT: the judge does not answer about the re-pinned path, zero unproven")
        return 1
    if unresolved or changed:
        print("VERDICT: at least one drift changes what is allowed; review before re-pinning")
        return 1
    print("VERDICT: every drift is the same allowed occurrences under moved bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
