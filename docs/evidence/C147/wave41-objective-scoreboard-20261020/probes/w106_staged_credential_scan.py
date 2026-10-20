"""Staged credential scan for the scoreboard bundle, by re-pointing wave 40's wrapper.

``C146/.../w99_staged_credential_scan.py`` already carries the discipline (import wave 38's scanner,
tee its stdout into a carrier under this bundle, declare the wave's write set). Its declared set is
wave 40's, so pointing it at wave 41's files would red the ``scope`` arm -- correctly. Rather than
author a third copy, this driver loads w99, re-points its ``RUNS`` and ``WAVE_PREFIXES``, and calls
its ``main()``, which re-arms both before the scan runs.
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
WAVE_PREFIXES: tuple[str, ...] = ("docs/evidence/C147/",)


def main() -> int:
    """Re-point the wave-40 wrapper at this bundle's write set, then run its scan."""
    spec = importlib.util.spec_from_file_location("w99_scan_wave41", W99)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import scanner wrapper {W99}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w99_scan_wave41"] = module
    spec.loader.exec_module(module)
    module.RUNS = RUNS
    module.WAVE_PREFIXES = WAVE_PREFIXES
    armed = module.RUNS == RUNS and tuple(module.WAVE_PREFIXES) == WAVE_PREFIXES
    print(
        f"re_point wrapper={W99.name} runs={module.RUNS} prefixes={module.WAVE_PREFIXES} "
        f"armed={armed}",
        flush=True,
    )
    if not armed:
        print("w106_staged_credential_scan_exit=1 # re_point_failed")
        return 1
    rc = int(module.main())
    print(f"w106_reused_wrapper={W99.name} scan_returned={rc}", flush=True)
    print(f"w106_staged_credential_scan_exit={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
