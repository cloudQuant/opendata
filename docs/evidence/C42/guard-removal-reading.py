"""Falsify the C42 key-collision guard: run the same path with it taken away.

Nothing here is a new production code path - ``refresh_metadata_backbone`` is the
shipped one, with its two legs fed the same three catalog rows (two of them
carrying the key ``symbol=000001.SZ``) and the writer injected so no database is
touched. The only difference between the two readings is whether
``_refuse_colliding_keys`` answers, which is exactly what C42 changed. What each
reading must show is asserted inside the script, so an archived log that stopped
matching the claim cannot be read as a passing one.

Usage (py313 env):
    python docs/evidence/C42/guard-removal-reading.py
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from opendata.data.models import Instrument
from opendata.pipeline import templates
from opendata.pipeline.runner import Window
from opendata.pipeline.templates import refresh_metadata_backbone

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pandas as pd

    from opendata.data.models.base import ContractModel
    from opendata.pipeline.templates import BackboneModelT

WINDOW = Window(start=date(2026, 9, 21), end=date(2026, 9, 25))
KEY = ("symbol",)

#: Two pages naming the same symbol (one live, one delisted) plus one clean row.
ROWS = [
    Instrument.model_validate(
        {
            "symbol": "000001.SZ",
            "exchange": "SZSE",
            "name": "平安银行",
            "status": "active",
            "currency": "CNY",
            "list_date": date(1991, 4, 3),
            "delist_date": None,
            "board": None,
        }
    ),
    Instrument.model_validate(
        {
            "symbol": "000001.SZ",
            "exchange": "SZSE",
            "name": "平安银行指数",
            "status": "active",
            "currency": "CNY",
            "list_date": date(1991, 4, 3),
            "delist_date": date(2020, 1, 1),
            "board": None,
        }
    ),
    Instrument.model_validate(
        {
            "symbol": "600519.SH",
            "exchange": "SSE",
            "name": "贵州茅台",
            "status": "active",
            "currency": "CNY",
            "list_date": date(2001, 8, 1),
            "delist_date": None,
            "board": None,
        }
    ),
]


def _pass_through(
    domain: str,
    rows: Sequence[BackboneModelT],
    *,
    model: type[ContractModel],
    key: Sequence[str],
) -> tuple[list[BackboneModelT], list[str]]:
    """The behaviour before C42: hand every row to the writer, collision or not."""
    del domain, model, key
    return list(rows), []


def _read(*, guard_removed: bool) -> dict[str, object]:
    """One backbone refresh, with the C42 guard answering or not."""
    original = templates._refuse_colliding_keys
    if guard_removed:
        templates._refuse_colliding_keys = _pass_through
    frames: dict[str, pd.DataFrame] = {}

    def land(domain: str, frame: pd.DataFrame, key: tuple[str, ...]) -> int:
        del key
        frames[domain] = frame
        return len(frame)

    try:
        report = refresh_metadata_backbone(
            None,  # the injected land means no warehouse is ever reached
            source="ths",
            fetch_instruments=lambda: list(ROWS),
            fetch_calendar=lambda: [],
            window=WINDOW,
            land=land,
            merged_at=datetime(2026, 9, 26, 17, 0),
        )
    finally:
        templates._refuse_colliding_keys = original

    frame = frames["instrument"]
    distinct = frame.drop_duplicates(subset=list(KEY)).shape[0]
    return {
        "symbols": frame[KEY[0]].tolist(),
        "frame_rows": len(frame),
        "distinct_keys": distinct,
        "landed": report.landed["instrument"],
        "refused": report.rejected["instrument"],
        "count_lies": bool(report.landed["instrument"] > distinct),
    }


def main() -> int:
    """Print both readings and check that only the guard separates them."""
    failures: list[str] = []
    for label, removed in (
        ("guard SHIPPED (C42 behaviour)", False),
        ("guard REMOVED (= behaviour before C42)", True),
    ):
        values = _read(guard_removed=removed)
        print(f"-- {label}")
        for key in ("symbols", "frame_rows", "distinct_keys", "landed", "refused", "count_lies"):
            print(f"   {key} = {values[key]}")
        if removed and values["count_lies"] is not True:
            failures.append("removing the guard did not reproduce the silent count")
        if removed and values["refused"] != ():
            failures.append("removing the guard still reported the collision")
        if not removed and values["count_lies"] is not False:
            failures.append("the shipped guard did not stop the collision")
        if not removed and values["refused"] == ():
            failures.append("the shipped guard landed a collision without saying so")
    if failures:
        for failure in failures:
            print(f"UNEXPECTED: {failure}")
        return 1
    print("\nBoth readings are what they claim: the guard is the difference.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
