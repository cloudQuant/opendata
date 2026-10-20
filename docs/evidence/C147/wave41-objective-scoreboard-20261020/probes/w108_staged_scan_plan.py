"""Staged credential scan for the plan-document payload, keeping each carrier on its own name.

The decision queue lands in ``docs/迭代计划/…/前置条件与决策清单.md``, which is outside every
wave bundle's declared set, so a scan aimed at it would red ``scope`` under the existing wrappers.
This driver re-points wave 40's wrapper at the plan prefix, then moves the produced carrier onto a
wave-41 name and restores the bundle's committed carrier byte-for-byte -- the same defect wave 40
hit twice (a wrapper writes into the bundle it lives in) is closed here by an explicit
backup/restore instead of a manual ``git restore`` afterwards.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C147/wave41-objective-scoreboard-20261020"
RUNS = REPO / BUNDLE / "runs"
W99 = (
    REPO
    / "docs/evidence/C146/wave40-query-annotation-divergence-20261019/probes"
    / "w99_staged_credential_scan.py"
)
WAVE_PREFIXES: tuple[str, ...] = ("docs/迭代计划/", "docs/evidence/C147/")
PRODUCED_NAME = "w99_staged_credential_scan.txt"
OWN_NAME = "w108_staged_scan_plan.txt"


def main() -> int:
    """Run the scan, archive its carrier under this probe's name, and restore the prior carrier."""
    spec = importlib.util.spec_from_file_location("w99_scan_wave41_plan", W99)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import scanner wrapper {W99}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w99_scan_wave41_plan"] = module
    spec.loader.exec_module(module)
    module.RUNS = RUNS
    module.WAVE_PREFIXES = WAVE_PREFIXES
    print(f"re_point runs={module.RUNS} prefixes={module.WAVE_PREFIXES}", flush=True)
    prior_path = RUNS / PRODUCED_NAME
    prior = prior_path.read_bytes() if prior_path.is_file() else None
    rc = 1
    try:
        rc = int(module.main())
    finally:
        produced = RUNS / PRODUCED_NAME
        archived = RUNS / OWN_NAME
        if produced.is_file():
            archived.write_bytes(produced.read_bytes())
        if prior is not None:
            produced.write_bytes(prior)
    if not (RUNS / OWN_NAME).is_file():
        print("w108_staged_scan_plan_exit=1 # carrier_missing")
        return 1
    restored = prior is None or prior_path.read_bytes() == prior
    archived_text = (RUNS / OWN_NAME).read_text(encoding="utf-8", errors="replace")
    armed = restored and f"w99_staged_credential_scan_exit={rc}" in archived_text
    print(
        f"carrier archived={OWN_NAME} prior_restored={restored} exit_line_present={armed} rc={rc}",
        flush=True,
    )
    print(f"w108_staged_scan_plan_exit={0 if armed and rc == 0 else 1}")
    return 0 if armed and rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
