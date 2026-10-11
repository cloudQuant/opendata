#!/usr/bin/env python3
"""C92 census-repair control: the same registry judged against the pre-repair and post-repair rows.

Registering ``rows.order_limit`` as a *present* capability is what put six rows into
``shipped_cover_rows`` -- the bucket that says "this row's needs are all covered, yet the row still
declares itself inexpressible". The claim this archive owns is that those six rows got there by
under-reporting their need set, not by the flag being mis-set, and that the repairs -- adding
absent canonicals, and replacing one label whose mechanism the engine does not have -- are what
emptied the bucket.

Two arms, run through the repo's own judge (``model_capability_census.run``) with nothing but the
census directory swapped:

* ``ARM-PRE`` reads the census bytes at ``PRE_REPAIR_REV`` (the commit before C92's code landed)
  against *this* round's registry: six rows.
* ``ARM-POST`` reads the committed census bytes: zero rows.

A single-arm run would only show the current state, which is not a control. Exit 0 requires the
pre-repair arm to still read six, so this file goes red the day the pre-repair bytes stop
reproducing the defect it documents.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404  # read-only git argv, shell disabled
import sys
import tempfile
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))

import model_capability_census as mcc  # noqa: E402

CENSUS_REL: Final = mcc.CENSUS_DIR_REL
# Pinned to the C92 code commit rather than HEAD~1: the next commit moves HEAD, and a control whose
# "before" bytes drift into the "after" bytes would read 0 against 0 and call that a pass.
PRE_REPAIR_REV: Final = "5c8cd61^"
PRE_REPAIR_IDS: Final = {
    "OBB2-econdb-AvailableIndicators",
    "OBB2-federal_reserve-FederalFundsRate",
    "OBB2-federal_reserve-OvernightBankFundingRate",
    "OBB2-federal_reserve-SOFR",
    "OBB2-intrinio-SearchAttributes",
    "OBB2-tiingo-CryptoHistorical",
}


def _listing(rev: str) -> list[str]:
    proc = subprocess.run(  # nosec B603, B607  # noqa: S603
        ["git", "show", f"{rev}:{CENSUS_REL}"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    return [n for n in proc.stdout.splitlines() if n.startswith("census-") and n.endswith(".json")]


def _materialize_pre_repair(rev: str, outdir: Path) -> int:
    """Write ``rev``'s census bytes into ``outdir``; the row files are read from there verbatim."""
    outdir.mkdir(parents=True, exist_ok=True)
    names = _listing(rev)
    for name in names:
        blob = subprocess.run(  # nosec B603, B607  # noqa: S603
            ["git", "show", f"{rev}:{CENSUS_REL}/{name}"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
        (outdir / name).write_bytes(blob)
    return len(names)


def _shipped_cover(census_dir: Path | None) -> tuple[list[str], dict[str, int]]:
    if census_dir is None:
        report = mcc.run(round_id="C92-CONTROL")
    else:
        report = mcc.run(round_id="C92-CONTROL", census_dir=census_dir)
    face = json.loads(mcc.serialize(report))
    rows = face["roadmap"]["shipped_cover_rows"]
    counts = {r["task_id"]: len(r["needs"]) for r in rows}
    return sorted(counts), counts


def main() -> int:
    """Run both arms and refuse the reading that the repairs did not move."""
    failures: list[str] = []
    resolved = subprocess.run(  # nosec B603, B607  # noqa: S603
        ["git", "rev-parse", "--short", PRE_REPAIR_REV],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    print(f"PIN pre-repair rev={PRE_REPAIR_REV} resolves_to={resolved}")
    with tempfile.TemporaryDirectory(prefix="c92_census_control_") as raw:
        pre_dir = Path(raw) / "pre"
        file_count = _materialize_pre_repair(PRE_REPAIR_REV, pre_dir)
        pre_ids, pre_needs = _shipped_cover(pre_dir)
    post_ids, _ = _shipped_cover(None)
    print(
        f"ARM-PRE census_files={file_count} shipped_cover={len(pre_ids)} ids={pre_ids}"
        f"\nARM-PRE need-count={pre_needs}"
        f"\nARM-POST shipped_cover={len(post_ids)} ids={post_ids}"
        f"\nCONTROL pre-reproduces-defect={set(pre_ids) == PRE_REPAIR_IDS} "
        f"post_empties_bucket={post_ids == []}"
    )
    if set(pre_ids) != PRE_REPAIR_IDS:
        failures.append(
            f"the pre-repair bytes no longer read six rows, they read {pre_ids}: "
            "either the anchor revision moved or the repairs were not what emptied the bucket"
        )
    if post_ids:
        failures.append(f"the committed census still carries {len(post_ids)} over-claimed row(s)")
    for reason in failures:
        print(f"FAIL {reason}")
    print(
        f"VERDICT arm-pre={len(pre_ids)} arm-post={len(post_ids)} "
        f"movement={len(pre_ids) - len(post_ids)} failures={len(failures)}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
