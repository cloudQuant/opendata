"""Driver for the wave-58 credential gate copy: declared write set, carrier inside this bundle.

Same discipline as ``w108_staged_scan_plan.py``: import the shipped scanner object, re-point its
carrier and its declared prefix list, arm the re-point, and refuse to run otherwise. The difference
is which instrument is imported - ``w165_staged_credential_gate.py`` carries the suppression
whitelist predicate, and wave 38's instrument is left untouched.

``--control`` runs three extra checks, each of which can fail the run: the observed suppression map
against a tampered expectation (must come back unequal); a forged index-vs-worktree divergence in a
real product path (``index_is_worktree`` must catch it, the carrier exemption notwithstanding); and
that same product path offered as an exemption (``self_carrier_exemption_exact`` must refuse).
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
        product = sorted(p for p in staged if p.startswith("opendata/"))
        if not product:
            raise SystemExit("control needs a product path in the staged payload to forge against")
        forged_target = product[0]

        def _forged_worktree(path: str) -> bytes:
            if path == forged_target:
                return b"# forged divergence, never written to disk"
            return (REPO / path).read_bytes()

        forced_drift, forced_self = module._drift_reading(
            [forged_target, carrier_rel],
            (carrier_rel,),
            worktree=_forged_worktree,
        )
        # Expected shape of the forged reading: the product path lands in the arm-visible drift and
        # the carrier in the exemption -- no more, no less. Asserting "no self hits" was itself the
        # wrong expectation (measured 2026-10-22): the control runs after this driver rewrote
        # its own carrier, so that one path really does differ from its staged blob, and an
        # expectation that ignores a real divergence fires on a correct run.
        drift_bites = forced_drift == [forged_target] and forced_self == [carrier_rel]
        scope_bites = not module._exemption_is_carrier_only((forged_target,))
        control_ok = whitelist_bites and drift_bites and scope_bites
        lines = [
            f"control whitelist_against_tampered_expectation_unequal={whitelist_bites} "
            "(expected True: an off-by-one suppression count must redden the arm)",
            f"control drift_arm_bites_on_forged_product_file={drift_bites} "
            f"target={forged_target} forced_drift={forced_drift} forced_self={forced_self} "
            "(expected True: the self-exemption must not excuse a product file)",
            f"control exemption_scope_guard_bites={scope_bites} "
            "(expected True: an exemption naming opendata/** is refused by "
            "self_carrier_exemption_exact)",
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
