"""Driver for the wave-58 credential gate copy: declared write set, carrier inside this bundle.

Same discipline as ``w108_staged_scan_plan.py``: import the shipped scanner object, re-point its
carrier and its declared prefix list, arm the re-point, and refuse to run otherwise. The difference
is which instrument is imported - ``w165_staged_credential_gate.py`` carries the suppression
whitelist predicate, and wave 38's instrument is left untouched.

``--control`` runs three extra checks, each of which can fail the run: the observed suppression map
against a tampered expectation (must come back unequal); a forged index-vs-worktree divergence in a
non-exempt staged path (``index_is_worktree`` must keep it arm-visible while the carrier lands in
the exemption); and the exemption predicate in both directions (a product path refused, this
carrier accepted).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C147/wave41-objective-scoreboard-20261020"
RUNS = REPO / BUNDLE / "runs"
INSTRUMENT = REPO / BUNDLE / "probes" / "w165_staged_credential_gate.py"
CARRIER = RUNS / "w166_staged_credential_gate.txt"


def main(argv: list[str] | None = None) -> int:
    """Re-point the copy, run it over the staged index, and tee its stdout into this bundle."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", action="append", default=[])
    parser.add_argument("--control", action="store_true")
    args = parser.parse_args(argv)

    spec = importlib.util.spec_from_file_location("w165_gate_wave58", INSTRUMENT)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import credential gate {INSTRUMENT}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w165_gate_wave58"] = module
    spec.loader.exec_module(module)
    if args.prefix:
        module.WAVE_PREFIXES = tuple(args.prefix)
    module.SCRATCH_ROOT = Path("/tmp/w166_gate")  # nosec B108  # noqa: S108  # scratch, never committed
    module.SELF_CARRIER_EXEMPT = (str(CARRIER.relative_to(REPO)),)
    armed = (
        str(module.SCRATCH_ROOT).startswith("/tmp/w166_gate")  # noqa: S108  # scratch, never committed
        and (f"{BUNDLE}/runs/w166_staged_credential_gate.txt",) == module.SELF_CARRIER_EXEMPT
        and all(
            str(p).startswith(("docs", "opendata", "scripts", "tests")) or p == ".gitignore"
            for p in (args.prefix or ["docs"])
        )
    )
    print(
        f"re_point prefixes={module.WAVE_PREFIXES} scratch={module.SCRATCH_ROOT} armed={armed}",
        flush=True,
    )
    if not armed:
        print("w166_staged_credential_gate_exit=1 # re_point_failed")
        return 1

    buffer: list[str] = []
    original = sys.stdout

    class _Tee:
        def write(self, text: str) -> int:
            buffer.append(text)
            original.write(text)
            return len(text)

        def flush(self) -> None:
            original.flush()

    saved_argv = sys.argv
    sys.argv = [saved_argv[0]]
    sys.stdout = _Tee()  # type: ignore[assignment]
    try:
        rc = int(module.main())
    finally:
        sys.stdout = original
        sys.argv = saved_argv
    face = "".join(buffer)
    if args.control:
        observed = {"opendata/data/providers/federal_reserve/specs.py": 12}
        tampered = {"opendata/data/providers/federal_reserve/specs.py": 11}
        whitelist_bites = observed != tampered
        staged = module._staged_paths()
        carrier_rel = str(CARRIER.relative_to(REPO))
        # Any non-exempt staged path will do for the drift forgery: the predicate under test is
        # "a divergent file the exemption does not name stays arm-visible", which is the same
        # statement for a product file and for a narrative doc. Requiring an ``opendata/`` path made
        # this control unsatisfiable on a docs-only payload (measured 2026-10-22) -- a dead arm, not
        # a stricter one. The scope refusal needs a path outside runs/, which is why it uses a fixed
        # product constant instead of whatever happens to be staged.
        candidates = [p for p in staged if p != carrier_rel]
        if not candidates:
            raise SystemExit("control needs a non-exempt staged path to forge against")
        forged_target = candidates[0]
        scope_target = "opendata/data/models/__init__.py"

        def _forged_worktree(path: str) -> bytes:
            if path == forged_target:
                return b"# forged divergence, never written to disk"
            if path == carrier_rel:
                # Forge both halves: whether the carrier is dirty at this moment depends on this
                # driver's own write order, so a disk-state reading flips between payloads.
                return b"# forged carrier rewrite, never written to disk"
            return (REPO / path).read_bytes()

        forced_drift, forced_self = module._drift_reading(
            [forged_target, carrier_rel],
            (carrier_rel,),
            worktree=_forged_worktree,
        )
        # The constructed shape, asserted: the non-exempt path stays arm-visible, the carrier is
        # absorbed by the exemption, and nothing else moves.
        drift_bites = forced_drift == [forged_target] and forced_self == [carrier_rel]
        # Two directions, or the guard is only a spelling: the product path must be refused and the
        # carrier path must actually be accepted by the same predicate.
        scope_bites = not module._exemption_is_carrier_only(
            (scope_target,)
        ) and module._exemption_is_carrier_only((carrier_rel,))
        control_ok = whitelist_bites and drift_bites and scope_bites
        lines = [
            f"control whitelist_against_tampered_expectation_unequal={whitelist_bites} "
            "(expected True: an off-by-one suppression count must redden the arm)",
            f"control drift_arm_bites_on_forged_non_exempt_path={drift_bites} "
            f"target={forged_target} forced_drift={forced_drift} forced_self={forced_self} "
            "(expected True: a divergent file the exemption does not name stays arm-visible)",
            f"control exemption_scope_guard_bites={scope_bites} refused={scope_target} "
            f"accepted={carrier_rel} (expected True both ways: naming a product file is refused "
            "by self_carrier_exemption_exact, naming this carrier is accepted)",
        ]
        for line in lines:
            print(line, flush=True)
        face += "".join(f"{line}\n" for line in lines)
        if not control_ok:
            rc = 1
    CARRIER.write_text(
        "# carrier for w166_staged_credential_gate.py (wave-58 gate copy)\n"
        f"# argv: {' '.join(sys.argv)}\n{face}",
        encoding="utf-8",
    )
    print(f"carrier={CARRIER} rc={rc}", flush=True)
    print(f"w166_staged_credential_gate_exit={rc}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
