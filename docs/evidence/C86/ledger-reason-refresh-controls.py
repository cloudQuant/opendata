#!/usr/bin/env python3
"""Controls for ``ledger-reason-refresh.py``: every counter it prints has an arm that moves it.

The refresh instrument's pass line is a conjunction over arms a single real carrier cannot fire, so
this instrument fires them by calling that module's own ``report`` and ``main`` (imported, not
reimplemented; no subprocess, so it adds no bandit exemption to AC-17|02's census either):

``[1]`` a gap reading whose item is absent from the ledger must make the run FAIL with
        ``matched=0`` -- the shape that keeps a typo'd item from being silently skipped;
``[2]`` a carrier reading ``proven`` against a ledger cell that says ``gap`` must be DECLINED and
        write nothing: a state flip is another instrument's job, prose must not fake one;
``[3]`` a named carrier that prints no ``VERDICT`` line must make the run FAIL, naming the file --
        pointing this at the wrong archive refreshes nothing and may not print PASS;
``[4]`` the instrument's own ``--verify-only`` path over the ledger as written must read rc=0;
``[5]`` byte identity both ways: one reading whose prose differs from the cell's moves the ledger's
        sha256 in ``write`` mode and not in ``dry`` -- without the second half, "dry changed
        nothing" would also be printed by an instrument that never reached its write branch at all;
``[6]`` the control's own write is rolled back in a ``finally`` and the rollback is *checked*
        against the saved bytes, not asserted.

Run: python3 docs/evidence/C86/ledger-reason-refresh-controls.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import sys
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from types import ModuleType

REPO: Final = pathlib.Path(__file__).resolve().parents[3]
INSTRUMENT_REL: Final = "docs/evidence/C86/ledger-reason-refresh.py"
LEDGER_REL: Final = "docs/quality/acceptance-item-ledger.json"
CARRIER_REL: Final = "docs/evidence/C86/ac9-08-reason-rerun.txt"
#: An archive this round really wrote that carries no ``VERDICT`` line -- [3]'s negative arm.
NO_VERDICT_REL: Final = "docs/evidence/C86/ac9-08-arm-check.txt"
ITEM: Final = "AC-9|08"
ABSENT_RC: Final = -1


def load_instrument() -> ModuleType:
    """Import the refresh instrument by path so its own arms are what gets tested."""
    path = REPO / INSTRUMENT_REL
    spec = importlib.util.spec_from_file_location("ledger_reason_refresh", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load instrument: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def ledger_bytes() -> bytes:
    """The ledger's bytes -- the thing [5] proves a dry run cannot move and a write can."""
    return (REPO / LEDGER_REL).read_bytes()


def sha(data: bytes) -> str:
    """sha256[:16] of ledger bytes."""
    return hashlib.sha256(data).hexdigest()[:16]


def tested_sha() -> str:
    """The refresh instrument's own bytes at the moment the arms fired.

    This control imports that module by path, so a carrier without this digest would read PASS
    without saying *which* version of the instrument it tested.
    """
    return sha((REPO / INSTRUMENT_REL).read_bytes())


def fresh(module: ModuleType) -> tuple[dict[str, Any], dict[str, str]]:
    """Re-read the ledger from disk so each arm starts from the tree's own state."""
    ledger: dict[str, Any] = json.loads((REPO / LEDGER_REL).read_text(encoding="utf-8"))
    return ledger, module.ledger_cells(ledger)


def call_main(module: ModuleType, argv_tail: list[str]) -> int:
    """Drive the instrument's real argument path without a subprocess."""
    backup = list(sys.argv)
    sys.argv = [INSTRUMENT_REL, *argv_tail]
    try:
        return int(module.main())
    finally:
        sys.argv = backup


def main() -> int:
    """Fire each arm, print its rc and counters, and roll the ledger back before exiting."""
    module = load_instrument()
    refresh_sha = tested_sha()
    saved = ledger_bytes()
    saved_sha = sha(saved)
    readings = module.parse_carrier(REPO / CARRIER_REL)
    gap = [r for r in readings if r.item == ITEM and r.state == "gap"]
    if not gap:
        print(f"PROBLEM carrier {CARRIER_REL} prints no gap reading for {ITEM}")
        return 1
    print(f"tested instrument {INSTRUMENT_REL} sha256[:16]={refresh_sha}")
    problems: list[str] = []
    rc_absent = ABSENT_RC
    rc_decline = ABSENT_RC
    rc_empty = ABSENT_RC
    rc_verify = ABSENT_RC
    rc_dry = ABSENT_RC
    rc_write = ABSENT_RC
    try:
        print("[1] a gap reading for an item the ledger does not have (needs rc=1, matched=0)")
        ledger, cells = fresh(module)
        rc_absent = module.report([gap[0]._replace(item="AC-99|77")], cells, ledger, "dry")
        if rc_absent != 1:
            problems.append(f"an unmatched item read rc={rc_absent}, so a typo would pass silently")

        print("[2] carrier reads proven while the cell reads gap (needs DECLINED, rc=0, no write)")
        ledger, cells = fresh(module)
        mark = ledger_bytes()
        flip = gap[0]._replace(state="proven", reason="the carrier says proven")
        rc_decline = module.report([flip], cells, ledger, "dry")
        if rc_decline != 0:
            problems.append(f"a declined flip read rc={rc_decline}; declining is not an error")
        if ledger_bytes() != mark:
            problems.append("the declined cell's bytes moved anyway")

        print(f"[3] a carrier with no VERDICT line at all (needs rc=1, names {NO_VERDICT_REL})")
        rc_empty = call_main(module, ["--carrier", NO_VERDICT_REL, "--verify-only"])
        if rc_empty != 1:
            problems.append(
                f"an empty carrier read rc={rc_empty}; pointing at the wrong archive must not PASS"
            )

        print("[4] the instrument's own --verify-only over the ledger as written (needs rc=0)")
        rc_verify = call_main(module, ["--carrier", CARRIER_REL, "--verify-only"])
        if rc_verify != 0:
            problems.append(
                f"--verify-only read rc={rc_verify} against the ledger this round wrote"
            )

        print("[5] one reading with a different reason: dry must not move the bytes, write must")
        tampered = gap[0]._replace(reason=f"{gap[0].reason}｜控制臂造的文字，不是探针读数")
        ledger, cells = fresh(module)
        rc_dry = module.report([tampered], cells, ledger, "dry")
        after_dry = sha(ledger_bytes())
        if rc_dry != 0 or after_dry != saved_sha:
            problems.append(f"dry run rc={rc_dry} sha={after_dry}, expected {saved_sha}")
        ledger, cells = fresh(module)
        rc_write = module.report([tampered], cells, ledger, "write")
        after_write = sha(ledger_bytes())
        if rc_write != 0 or after_write == saved_sha:
            problems.append(
                f"write arm rc={rc_write} sha {saved_sha} -> {after_write}; "
                "if it cannot move, the dry face above proves nothing"
            )
        print(f"    sha saved={saved_sha} after_dry={after_dry} after_write={after_write}")
    finally:
        (REPO / LEDGER_REL).write_bytes(saved)
        restored = sha(ledger_bytes())
        print(f"[6] rollback sha={restored} byte-identical={restored == saved_sha}")
        if restored != saved_sha:
            problems.append(f"rollback left {restored}, expected {saved_sha}")

    for problem in problems:
        print(f"PROBLEM {problem}")
    tally = (
        f"absent_rc={rc_absent} decline_rc={rc_decline} empty_rc={rc_empty} "
        f"verify_rc={rc_verify} dry_rc={rc_dry} write_rc={rc_write} "
        f"refresh_sha={refresh_sha} saved_sha={saved_sha}"
    )
    if problems:
        print(f"{tally}\nLEDGER_REFRESH_CONTROLS_CHECK FAIL problems={len(problems)}")
        return 1
    print(f"{tally}\nLEDGER_REFRESH_CONTROLS_CHECK PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
