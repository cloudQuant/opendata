"""Print the registry status groups on both sides of the C86 label split.

``label-split.md`` claims ``absent`` moved 33 -> 38 across the split. That number is not this file's
author's arithmetic: each face is produced by loading a census instrument as a module and running
its own ``run()``, so the grouping comes from ``Report.statuses`` -- the same grouping the roadmap
prints. The pre-split side is loaded from a file the operator names on the command line (the bytes
of ``git show 912f59b:scripts/quality/model_capability_census.py``), and its digest is checked
against the pin ``label-map-before-split.json`` carries, so a stale scratch file cannot pass as the
old face.

Usage::

    git show 912f59b:scripts/quality/model_capability_census.py > /tmp/c86_pre_split.py
    python docs/evidence/C86/registry-status-both-faces.py --presplit /tmp/c86_pre_split.py
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
PLAN_DIR: Final = REPO_ROOT / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
LIVE_INSTRUMENT: Final = REPO_ROOT / "scripts/quality/model_capability_census.py"
# The digest of the blob the split reverted, pinned by the carrier this file cross-checks against.
PRESPLIT_SHA16: Final = "fe5a3a55ef600b7f"


def _load(name: str, path: Path) -> ModuleType:
    """Import one side of the instrument by path so neither face is a hand-typed table."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sha16(path: Path) -> str:
    """Digest the pre-split bytes so a scratch file that drifted from the pin is visible."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _face(label: str, path: Path) -> tuple[str, int, int, dict[str, int]]:
    """Run one instrument over the live census and return its canonical/registry/status face."""
    module = _load(f"mcc_{label}", path)
    report = module.run(round_id="C86", root=REPO_ROOT, census_dir=PLAN_DIR)
    groups = {status: len(ids) for status, ids in report.statuses.items()}
    return label, report.canonical_count, len(module.REGISTRY), groups


def main(argv: list[str] | None = None) -> int:
    """Run both instruments and print canonical counts beside their status groups."""
    parser = argparse.ArgumentParser(description="C86 registry status, both sides of the split")
    parser.add_argument("--presplit", type=Path, required=True, help="pre-split instrument bytes")
    args = parser.parse_args(argv)
    if not args.presplit.is_file():
        print(f"REFUSE {args.presplit} absent; run the git show named in the module docstring")
        return 2
    digest = _sha16(args.presplit)
    pinned = "MATCH" if digest == PRESPLIT_SHA16 else "MISMATCH"
    print(f"pre-split bytes: {args.presplit} sha256[:16]={digest} vs pin {PRESPLIT_SHA16} {pinned}")
    pre = _face("pre_split", args.presplit)
    post = _face("split", LIVE_INSTRUMENT)
    for label, canonical, entries, groups in (pre, post):
        print(
            f"{label}: canonical={canonical} registry_entries={entries} "
            f"statuses={json.dumps(groups, sort_keys=True)}"
        )
    every = set(pre[3]) | set(post[3])
    delta = {status: post[3].get(status, 0) - pre[3].get(status, 0) for status in sorted(every)}
    print(
        f"delta across the split: canonical {pre[1]}->{post[1]} registry {pre[2]}->{post[2]} "
        f"statuses={json.dumps(delta, sort_keys=True)}"
    )
    closure = {
        label: sum(groups.values()) == entries == canonical
        for label, canonical, entries, groups in (pre, post)
    }
    closes = all(closure.values()) and pinned == "MATCH"
    print(
        "closure (status groups sum == registry entries == canonical ids): "
        f"{json.dumps(closure, sort_keys=True)} digest_pin={pinned}"
    )
    return 0 if closes else 1


if __name__ == "__main__":
    raise SystemExit(main())
