"""Post-commit closure face for wave 39, on wave 38's shipped instrument.

Both prefix sets are re-pointed, and that is the part that cannot be taken for granted.

The instrument reconciles every path a commit carries (``git show <sha>:<path>`` against the
worktree bytes), runs git-mode and dir-mode gitleaks over exactly that payload with a planted
control, and calls the repo's own ``secret_scan_check``. Its commit argument already comes from
argv, so the face cannot be
aimed at a stale literal.

Two constants have to move together and that is the whole reason this wrapper prints both:
``TREE_PREFIXES`` is computed from ``BUNDLE_PREFIXES`` **at import time**, so re-pointing only
the latter leaves the ``--scope tree`` reading scanning wave 38's bundles. ``SCAN_CARRIER`` is the
staged credential
scan the closure face takes as a premise, and it must name *this* wave's scan, produced by
``w62_staged_credential_scan.py`` before the commit.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019"
RUNS = REPO / BUNDLE / "runs"
INSTRUMENT = (
    REPO / "docs/evidence/C143/wave38-seeking-alpha-landing-20261019/probes/w47_closure_face.py"
)
BUNDLE_PREFIXES = ("docs/evidence/C144", "docs/evidence/C145", "docs/evidence/C101")
SCAN_CARRIER = RUNS / "w62_staged_credential_scan.txt"


def _load_instrument() -> Any:  # noqa: ANN401  # wave 38's closure face is this probe's instrument
    """Import w47 and re-point every bundle-derived constant, including the derivative."""
    spec = importlib.util.spec_from_file_location("w38_closure_face", INSTRUMENT)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import closure face {INSTRUMENT}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w38_closure_face"] = module
    spec.loader.exec_module(module)
    module.BUNDLE = BUNDLE
    module.RUNS = RUNS
    module.SCAN_CARRIER = SCAN_CARRIER
    module.SCRATCH = Path("/tmp/w63_closure")  # nosec B108  # noqa: S108
    module.BUNDLE_PREFIXES = BUNDLE_PREFIXES
    module.TREE_PREFIXES = BUNDLE_PREFIXES + (
        "opendata",
        "scripts/quality/iter2_witnesses",
        "tests",
    )
    return module


def main(argv: list[str] | None = None) -> int:
    """Arm the re-point, then run the shipped closure face over the named commit."""
    RUNS.mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(description="wave-39 post-commit closure face wrapper")
    parser.add_argument("--commit", default="", help="commit to scan; defaults to HEAD")
    parser.add_argument("--scope", default="diff", choices=("diff", "tree"))
    args = parser.parse_args(argv)
    instrument = _load_instrument()
    armed = (
        str(instrument.BUNDLE) == BUNDLE
        and tuple(instrument.BUNDLE_PREFIXES) == BUNDLE_PREFIXES
        and "C145" in str(instrument.TREE_PREFIXES)
        and "C143" not in str(instrument.TREE_PREFIXES)
        and Path(str(instrument.SCAN_CARRIER)).is_file()
    )
    print(
        f"re_point bundle={instrument.BUNDLE} prefixes={instrument.BUNDLE_PREFIXES} "
        f"tree={instrument.TREE_PREFIXES} scan_carrier_exists="
        f"{Path(str(instrument.SCAN_CARRIER)).is_file()}",
        flush=True,
    )
    if not armed:
        print("w63_closure_face_exit=1 # re_point_failed")
        return 1
    # The shipped reconciler asks git for the commit's path list, and git quotes non-ASCII paths by
    # default: with a CJK path in the payload it emitted ``"docs/\350\277\255..."``, parsed the root
    # as ``"docs``, and reported that path missing from the commit while its bytes were identical.
    # A process-scoped override keeps the child's listing raw without touching repo or user config.
    os.environ["GIT_CONFIG_COUNT"] = "1"
    os.environ["GIT_CONFIG_KEY_0"] = "core.quotepath"
    os.environ["GIT_CONFIG_VALUE_0"] = "false"
    printable = subprocess.run(  # nosec B603
        ["/usr/bin/git", "show", "--name-only", "--format=", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    nonascii = [line for line in printable.splitlines() if not line.isascii()]
    print(
        f"quotepath_off head_paths={len(printable.splitlines())} "
        f"nonascii_raw={len(nonascii)} sample={nonascii[:1]}",
        flush=True,
    )
    rc = int(instrument.main(["--commit", args.commit, "--scope", args.scope]))
    print(f"w63_reused_instrument={INSTRUMENT.name} closure_returned={rc}", flush=True)
    print(f"w63_closure_face_exit={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
