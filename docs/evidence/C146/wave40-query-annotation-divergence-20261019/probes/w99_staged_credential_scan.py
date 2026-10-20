"""Staged-payload credential scan for wave 40, on wave 38's shipped instrument.

Wave 39's wrapper (``C145/.../w62_staged_credential_scan.py``) declares *wave 39's* write set, so
pointing it at wave 40's staged files reds the ``scope`` arm -- correctly, and that is why this wave
gets its own carrier and its own declared set instead of borrowing one.

Two things are re-pointed and armed before the scan: the carrier path (the instrument computes it
from its own ``__file__``) and the declared path set, which is what makes the ``scope`` arm mean
"nothing outside this wave is staged". The instrument tees its stdout and writes the carrier only
under ``__main__``, so this wrapper collects the same stdout itself and writes it under this
bundle's name -- the defect wave 39 recorded as
[[feedback-imported-instruments-do-not-run-their-main-block]].
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C146/wave40-query-annotation-divergence-20261019"
RUNS = REPO / BUNDLE / "runs"
INSTRUMENT = (
    REPO
    / "docs/evidence/C143/wave38-seeking-alpha-landing-20261019/probes"
    / "w41b_staged_credential_scan.py"
)
#: Wave 40's declared write set: the two repaired adapters, the regenerated claim tree they forced a
#: re-sign of, and this evidence bundle.
WAVE_PREFIXES: tuple[str, ...] = (
    "opendata/data/providers/benzinga/models/price_target.py",
    "opendata/data/providers/tradingeconomics/models/economic_calendar.py",
    "docs/evidence/C101/",
    "docs/evidence/C146/",
)


class _Tee:
    """Keep the run's stdout on the terminal and in memory, so the carrier is the printed face."""

    def __init__(self, stream: Any) -> None:  # noqa: ANN401  # the replaced sys.stdout stream
        self.stream = stream
        self.buffer: list[str] = []

    def write(self, text: str) -> int:
        self.stream.write(text)
        self.buffer.append(text)
        return len(text)

    def flush(self) -> None:
        self.stream.flush()


def _load_instrument() -> Any:  # noqa: ANN401  # wave 38's scanner is this probe's instrument
    """Import the shipped scanner and re-point its carrier and declared path set."""
    spec = importlib.util.spec_from_file_location("w38_staged_scan_wave40", INSTRUMENT)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import staged credential scanner {INSTRUMENT}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w38_staged_scan_wave40"] = module
    spec.loader.exec_module(module)
    module.CARRIER = RUNS / "w99_staged_credential_scan.txt"
    module.WAVE_PREFIXES = WAVE_PREFIXES
    return module


def main() -> int:
    """Arm the re-point, scan whatever is staged, and keep the scanner's stdout as this carrier."""
    RUNS.mkdir(parents=True, exist_ok=True)
    instrument = _load_instrument()
    carrier = Path(str(instrument.CARRIER))
    inside = str(carrier).startswith(str(RUNS))
    armed = tuple(instrument.WAVE_PREFIXES) == WAVE_PREFIXES and inside
    print(
        f"re_point carrier={carrier} inside_bundle={inside} wave_prefixes={len(WAVE_PREFIXES)} "
        f"instrument={INSTRUMENT.name}",
        flush=True,
    )
    if not armed:
        print(f"w99_staged_credential_scan_exit=1 # re_point_failed inside={inside}")
        return 1
    tee = _Tee(sys.stdout)
    sys.stdout = tee
    rc = 1
    try:
        rc = int(instrument.main())
    finally:
        sys.stdout = sys.__stdout__
        header = (
            "# carrier for w99_staged_credential_scan.py (scanner stdout, verbatim)\n"
            f"# argv: {sys.executable} {Path(__file__).resolve()}\n"
        )
        carrier.write_text(header + "".join(tee.buffer) + f"w99_staged_credential_scan_exit={rc}\n")
    print(f"w99_reused_instrument={INSTRUMENT.name} scan_returned={rc}", flush=True)
    print(f"w99_staged_credential_scan_exit={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
