"""Re-base the batch5 registry fence for ``cboe_futures_curve``, on wave 38's shipped instrument.

The fence (``tests/fixtures/registry_batch5_snapshot.json``) is a tracked census over the
registry --
31 byte pins, six cohort arrays, a counts block -- and four test files compare it against the live
registry. Adding a domain key moves the census, so the fence goes red until it is re-based; wave 38
shipped ``w49_batch5_rebase.py`` to do exactly that, including the authority-order fix (read the leg
and warehouse censuses *after* ``register_providers()``) and the mechanical scan that rewrites every
fenced count literal so no site is left to a hand-typed table.

This probe does not re-implement that instrument. It imports it and re-points the three
constants that
decide where it writes -- ``BUNDLE``, ``CARRIER`` and ``WAVE_DOMAINS`` -- then refuses to run if any
write target resolves outside this bundle or the re-point left the carrier naming wave 38's
directory.
That guard is the lesson wave 38 recorded the hard way: re-pointing only ``RUNS`` let another wave's
committed carrier be overwritten.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019"
RUNS = REPO / BUNDLE / "runs"
INSTRUMENT = (
    REPO / "docs/evidence/C143/wave38-seeking-alpha-landing-20261019/probes/w49_batch5_rebase.py"
)
WAVE_DOMAINS = ("cboe_futures_curve",)
#: Paths the instrument writes: its carrier, the fixture, and the four fenced test files.
SHARED_WRITE_TARGETS = (
    "tests/fixtures/registry_batch5_snapshot.json",
    "tests/test_provider_domains.py",
    "tests/test_pipeline_jobs.py",
    "tests/test_alert_matrix.py",
    "tests/test_alert_matrix_model_domains.py",
)


def _load_instrument() -> Any:  # noqa: ANN401  # wave 38's rebaser is this probe's instrument
    """Import w49 and re-point every bundle-derived constant it computed at import time."""
    spec = importlib.util.spec_from_file_location("w38_batch5_rebase", INSTRUMENT)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import fence rebaser {INSTRUMENT}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w38_batch5_rebase"] = module
    spec.loader.exec_module(module)
    module.BUNDLE = BUNDLE
    module.CARRIER = RUNS / "w60_batch5_rebase.txt"
    module.WAVE_DOMAINS = WAVE_DOMAINS
    return module


def main() -> int:
    """Arm the re-point, run the shipped rebaser, and print its own delta readings."""
    RUNS.mkdir(parents=True, exist_ok=True)
    instrument = _load_instrument()
    carrier = Path(str(instrument.CARRIER))
    bundle_ok = str(carrier).startswith(str(RUNS))
    domain_ok = tuple(instrument.WAVE_DOMAINS) == WAVE_DOMAINS
    targets = sorted(str(REPO / rel) for rel in SHARED_WRITE_TARGETS)
    print(
        f"re_point carrier={carrier} inside_bundle={bundle_ok} "
        f"wave_domains={instrument.WAVE_DOMAINS} "
        f"shared_targets={len(targets)} instrument={INSTRUMENT.name}",
        flush=True,
    )
    if not (bundle_ok and domain_ok):
        failed = f"bundle_ok={bundle_ok} domain_ok={domain_ok}"
        print(f"w60_batch5_rebase_exit=1 # re_point_failed {failed}")
        return 1
    rc = int(instrument.main())
    print(f"w60_reused_instrument={INSTRUMENT.name} instrument_returned={rc}", flush=True)
    print(f"w60_batch5_rebase_exit={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
