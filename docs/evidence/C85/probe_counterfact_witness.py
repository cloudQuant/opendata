#!/usr/bin/env python3
"""Print the counterfact face of one acceptance probe, break by break.

C85 witness instrument. It exists because a gate log for ``--self-test`` answers one question
about the whole suite ("did every judge bite?") and prints nothing about an individual item,
so the per-item evidence a reviewer needs -- which break, overlaid with which facts, flipping
which reading to which state -- had no carrier in the tree. The arithmetic is not re-derived
here: the repo's own ``resolve_repair``, ``wording_drift`` and ``Probe.judge`` are called, so
this transcript can be disagreed with only by disagreeing with the gate.

Two faces are printed, and the second is what makes the first readable:

* the real run -- every declared break must flip the repaired reading back to ``gap``, and the
  measured reading is printed too, because a probe whose gap has no judge-visible cause is a
  label rather than a decision;
* ``--tamper`` -- one break's fact overlay is replaced by nothing, so that break cannot change
  the reading it is applied to. The instrument must report the miss and exit non-zero. Without
  this arm a PASS line is indistinguishable from a hardcoded PASS.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.quality.acceptance_item_probe import (  # noqa: E402
    PROVEN,
    Break,
    Facts,
    load_context,
    probe_for,
    resolve_repair,
    wording_drift,
)

if TYPE_CHECKING:
    from scripts.quality.acceptance_item_probe import Probe


def facts_face(overlay: tuple[tuple[str, str], ...]) -> str:
    """Render a break's fact overlay on one line.

    Args:
        overlay: The ``(key, value)`` pairs the break writes onto the repaired reading.

    Returns:
        ``key: a -> b`` per pair, using ``-`` for a key the clean reading does not hold.
    """
    return "; ".join(f"{key}: {value}" for key, value in overlay) or "(no facts overlaid)"


def run_probe(probe: Probe, clean: Facts, breaks: tuple[Break, ...]) -> tuple[int, int]:
    """Apply each break to the repaired reading and report which ones flip it.

    Args:
        probe: The item's probe, from the registry rather than a copy.
        clean: The repaired reading, as ``self_test_findings`` builds it, so a ``*key`` repair
            carries today's measured value instead of the literal ``*key``.
        breaks: The breaks to apply, in declaration order.

    Returns:
        ``(bites, total)``: how many breaks reached ``gap``, and how many were applied.
    """
    bites = 0
    for brk in breaks:
        broken = dict(clean)
        broken.update(dict(brk.facts))
        state = probe.judge(broken).state
        ok = state == brk.expect
        bites += int(ok)
        print(f"  {'BITE ' if ok else 'MISS '} {brk.label} -> {state} (want {brk.expect})")
        print(f"         facts: {facts_face(brk.facts)}")
    return bites, len(breaks)


def main(argv: list[str] | None = None) -> int:
    """Run the witness for one item, with the tampered calibration arm on request."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--item", required=True, help="the AC-N|NN probe to witness")
    parser.add_argument(
        "--tamper",
        action="store_true",
        help="drop one break's facts: the run must then MISS and exit non-zero",
    )
    args = parser.parse_args(argv)

    ctx = load_context()
    probe = probe_for(args.item)
    drift = wording_drift(ctx, probe)
    print(f"item: {args.item}  (wording drift: {drift or 'clean'})")
    if drift:
        print(f"FAIL: {drift}")
        return 1

    measured = probe.measure(ctx)
    measured_verdict = probe.judge(measured)
    print(f"MEASURED: {measured_verdict.state}")
    for line in measured_verdict.readings:
        print("  - " + line.replace("\n", " "))
    if measured_verdict.reason:
        print(f"  reason: {measured_verdict.reason}")

    stray = sorted(set(probe.repair) - set(measured))
    if stray:
        print(f"FAIL: repair states facts no measure produces: {stray}")
        return 1

    clean = resolve_repair(measured, probe.repair)
    clean_state = probe.judge(clean).state
    print(f"REPAIR REACHABLE: {clean_state == PROVEN} (clean reading judges {clean_state})")

    breaks = probe.breaks
    if args.tamper:
        target = len(breaks) - 1
        tampered = tuple(
            Break(brk.label, (), brk.expect) if index == target else brk
            for index, brk in enumerate(breaks)
        )
        print(f"CALIBRATION: --tamper empties the fact overlay of break #{target + 1}")
        breaks = tampered

    bites, total = run_probe(probe, clean, breaks)
    print(f"breaks biting: {bites}/{total}")

    if args.tamper:
        passed = bites != total
        print(
            f"CALIBRATION {'PASS' if passed else 'FAIL'}: "
            + (
                "the instrument can see a break that does not flip its reading"
                if passed
                else "a stripped break still read as biting, so the PASS line proves nothing"
            )
        )
        return 0 if passed else 1
    return 0 if (bites == total and clean_state == PROVEN and total > 0) else 1


if __name__ == "__main__":
    sys.exit(main())
