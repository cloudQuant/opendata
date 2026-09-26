"""Show that the C42 scan cannot certify a zero it did not measure.

``duplicate-symbol-scan.py`` answers one question - does any ``symbol`` ride two
catalog pages - and its answer is a count. A run where every page raised prints
``collided symbols   = 0`` just like the measured truth does, and the shipped
version of the scan still exited 0 on that face: a number read as a verdict.
This script drives the scan's own ``main()`` with its two fetch helpers patched
(no network, no credential read, no key touched) and checks the three readings:
unanswered pages -> exit 1, an empty calendar leg -> exit 1, both answered -> 0.

Usage (py313 env):
    python docs/evidence/C42/scan-failure-face.py
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from opendata.data.models import Instrument

if TYPE_CHECKING:
    from types import ModuleType

SCAN_PATH = Path(__file__).with_name("duplicate-symbol-scan.py")
HEALTHY_CALENDAR = ("2026-08-19 .. 2026-09-27", 27, 27, "2026-08-19 .. 2026-09-24")
EMPTY_CALENDAR = ("2026-08-19 .. 2026-09-27", 0, 0, "empty")


def _row(symbol: str) -> Instrument:
    """One catalog row - enough of a contract instance for the scan's counter."""
    return Instrument.model_validate(
        {
            "symbol": symbol,
            "exchange": "SSE",
            "name": symbol,
            "status": "active",
            "currency": "CNY",
            "list_date": date(2001, 8, 1),
            "delist_date": None,
            "board": None,
        }
    )


def _load_scan() -> ModuleType:
    """Import the sibling scan script as a module so its helpers can be patched."""
    spec = importlib.util.spec_from_file_location("c42_duplicate_symbol_scan", SCAN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCAN_PATH}")
    module = importlib.util.module_from_spec(spec)
    # The scan's own dataclass needs its module reachable by name at exec time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _reading(scan: ModuleType, *, pages_unanswered: bool, calendar_empty: bool) -> int:
    """One run of the scan's own ``main()`` against patched legs."""

    def fake_page(catalog: object, ctx: object, asset_type: str) -> tuple[list[object], str | None]:
        del catalog, ctx
        if pages_unanswered:
            return [], "SimulatedAuthError: no credential in this process"
        return [_row(f"{asset_type}-0001.SH")], None

    def fake_calendar() -> tuple[str, int, int, str]:
        return EMPTY_CALENDAR if calendar_empty else HEALTHY_CALENDAR

    namespace = scan.__dict__
    names = ("_fetch_page", "_scan_calendar")
    original = {name: namespace[name] for name in names}
    namespace.update({"_fetch_page": fake_page, "_scan_calendar": fake_calendar})
    try:
        return int(namespace["main"]())
    finally:
        namespace.update(original)


CASES: tuple[tuple[str, dict[str, bool], int], ...] = (
    (
        "A: every catalog page UNANSWERED (what a keyless run looks like)",
        {"pages_unanswered": True, "calendar_empty": False},
        1,
    ),
    (
        "B: pages answered, calendar leg answered with no row",
        {"pages_unanswered": False, "calendar_empty": True},
        1,
    ),
    (
        "C: control - both legs answered, and the zero is a measurement",
        {"pages_unanswered": False, "calendar_empty": False},
        0,
    ),
)


def _status(got: int, want: int) -> str:
    """Return the label one reading earns: matching or unexpected."""
    return "ok" if got == want else "UNEXPECTED"


def main() -> int:
    """Print the three readings and fail if the scan cannot tell an answer from none."""
    scan = _load_scan()
    failures: list[str] = []
    for label, kwargs, expected in CASES:
        print(f"\n-------- {label}")
        got = _reading(scan, **kwargs)
        print(f"   -------- scan exit = {got}, expected {expected} [{_status(got, expected)}]")
        if got != expected:
            failures.append(f"{label}: exit {got}, expected {expected}")
    if failures:
        print("\nThe scan's exit code no longer separates 'not answered' from 'no collision':")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(
        "\nReadings A and C print the same `collided symbols   = 0`; only the exit code"
        " tells an answer from an absence of one. That is the face this round closed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
