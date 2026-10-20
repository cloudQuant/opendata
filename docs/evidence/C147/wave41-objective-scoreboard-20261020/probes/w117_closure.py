"""Wave-41 closure: reconcile the wave's latest docs commit and scan it, on wave 38's instrument.

Every wave-41 commit was made after its own staged credential scan, but none has a post-commit face:
the staged scan reads the index, and the commit is the object that ships. This driver imports wave
38's closure instrument directly (``w47_closure_face.py``) -- the wave-39 wrapper carries guards
pinned to its own bundle, so re-pointing that wrapper for a C147 commit would refuse itself -- and
sets every path constant the instrument writes or walks.

The commit's payload is 7 paths (C147 carriers plus the decisions doc), so ``--scope diff`` is the
right scope: dir-mode copies exactly those blobs, and the planted control proves the scanner speaks.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess  # nosec B404  # fixed argv child processes, no shell
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C147/wave41-objective-scoreboard-20261020"
RUNS = REPO / BUNDLE / "runs"
INSTRUMENT = (
    REPO / "docs/evidence/C143/wave38-seeking-alpha-landing-20261019/probes/w47_closure_face.py"
)
BUNDLE_PREFIXES = ("docs/evidence/C147", "docs/迭代计划", "tests")


def main(argv: list[str] | None = None) -> int:
    """Re-point the instrument at this bundle and run its closure over one commit."""
    spec = importlib.util.spec_from_file_location("w38_closure_face_wave41", INSTRUMENT)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import closure face {INSTRUMENT}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w38_closure_face_wave41"] = module
    spec.loader.exec_module(module)
    module.BUNDLE = BUNDLE
    module.RUNS = RUNS
    module.SCRATCH = Path("/tmp/w117_closure")  # nosec B108  # noqa: S108
    module.SCAN_CARRIER = RUNS / "w99_staged_credential_scan.txt"
    module.BUNDLE_PREFIXES = BUNDLE_PREFIXES
    module.TREE_PREFIXES = BUNDLE_PREFIXES + (
        "opendata",
        "scripts/quality/iter2_witnesses",
        "tests",
    )
    armed = (
        str(module.BUNDLE) == BUNDLE
        and Path(str(module.SCAN_CARRIER)).is_file()
        and tuple(module.BUNDLE_PREFIXES) == BUNDLE_PREFIXES
        and "C147" in str(module.TREE_PREFIXES)
    )
    print(
        f"re_point bundle={module.BUNDLE} prefixes={module.BUNDLE_PREFIXES} "
        f"scan_carrier_exists={Path(str(module.SCAN_CARRIER)).is_file()} armed={armed}",
        flush=True,
    )
    if not armed:
        print("w117_closure_exit=1 # re_point_failed")
        return 1
    # The payload carries a CJK plan-doc path; git quotes it by default, the instrument then
    # truncates the root to '"docs' and reports the path missing (wave 39 face.md §20). A
    # process-scoped override keeps the child's listing raw without touching repo or user config.
    commit = argv[0] if argv else "HEAD"
    os.environ["GIT_CONFIG_COUNT"] = "1"
    os.environ["GIT_CONFIG_KEY_0"] = "core.quotepath"
    os.environ["GIT_CONFIG_VALUE_0"] = "false"
    listing = subprocess.run(  # nosec B603  # noqa: S603
        ["/usr/bin/git", "show", "--name-only", "--format=", commit],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    print(
        f"quotepath_off paths={len(listing.splitlines())} "
        f"nonascii={sum(1 for line in listing.splitlines() if not line.isascii())}",
        flush=True,
    )
    rc = int(module.main(["--commit", commit, "--scope", "diff"]))
    print(f"w117_closure_commit={commit} closure_returned={rc}", flush=True)
    print(f"w117_closure_exit={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
