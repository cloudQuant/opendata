"""Wave-60 batch5 fence re-pin: run wave-38's rebase driver against this bundle only.

Why a wrapper instead of the driver itself: ``w49_batch5_rebase.py`` computes ``CARRIER`` from its own
``BUNDLE`` (C143), so invoking it directly would overwrite wave 38's committed carrier - the
recurring borrowed-driver defect. This wrapper re-points the bundle, the carrier and the scratch root,
and arms the re-point (it refuses to run unless every write target lands inside C147).

What is being re-pinned here is narrow: this wave's I001 import-order fix changed the bytes of
``opendata/data/models/__init__.py``, which the snapshot byte-pins, so four test files that compare
whole-file digests went red together. No domain identity was added or removed this wave, so the
driver's domain delta must print ``added=0 removed=0``; if it does not, the wrapper's own premise
(that this is a byte-pin re-pin, not a census change) is wrong and the run is reported, not smoothed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C147/wave41-objective-scoreboard-20261020"
RUNS = REPO / BUNDLE / "runs"
INSTRUMENT = REPO / "docs/evidence/C143/wave38-seeking-alpha-landing-20261019/probes/w49_batch5_rebase.py"


def main() -> int:
    """Re-point the driver's write targets into this bundle, arm them, then run it."""
    spec = importlib.util.spec_from_file_location("w49_batch5_rebase_wave60", INSTRUMENT)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import rebase driver {INSTRUMENT}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w49_batch5_rebase_wave60"] = module
    spec.loader.exec_module(module)
    module.BUNDLE = BUNDLE
    module.RUNS = RUNS
    module.CARRIER = RUNS / "w49_batch5_rebase.txt"
    module.SCRATCH = Path("/tmp/w161_batch5")  # nosec B108  # noqa: S108  # scratch tree, never committed
    module.WAVE_DOMAINS = ()  # this wave adds no domain identity; only a file digest moved
    armed = (
        str(module.BUNDLE) == BUNDLE
        and str(module.CARRIER).startswith(str(RUNS))
        and "C147" in str(module.CARRIER)
        and "C143" not in str(module.CARRIER)
        and module.WAVE_DOMAINS == ()
    )
    print(
        f"re_point bundle={module.BUNDLE} carrier={module.CARRIER} scratch={module.SCRATCH} "
        f"wave_domains={module.WAVE_DOMAINS} armed={armed}",
        flush=True,
    )
    if not armed:
        print("w161_batch5_repin_exit=1 # re_point_failed")
        return 1
    rc = int(module.main())
    print(f"w161_batch5_repin_returned={rc}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
