"""Prove the wave-38 pre-chain judge report was moved out of wave 37's bundle and C141 restored.

What happened, in the order the evidence can still show it: ``w45_run_chain.py`` re-used wave 37's
shipped chain driver and re-pointed its ``RUNS`` directory at this bundle, but the driver also
writes its pre-chain judge report to a second bundle-relative attribute, ``PRE_CHAIN_OUTPUT``,
which was
left pointing at wave 37's committed carrier. So wave 38's report (1710 claims, 0 accepted, 337
STALE) landed on top of ``docs/evidence/C141/.../runs/judge_pre_chain.report.json`` and ``.md``,
whose committed content is wave 37's reading (1705 claims, 336 STALE, 9 UNREVIEWED).

This face measures the recovery instead of narrating it:

1. the payload now in this bundle agrees with the pre-chain reading ``chain_summary.txt`` printed
   while the chain ran, so the moved file is the file that carrier describes; the same predicate is
   re-evaluated against a forged summary and must refuse it, or the match proves nothing;
2. wave 37's two report files are byte-identical to ``git show HEAD:`` for those paths -- read as
   bytes, because text mode translates newlines and would make an equality claim about a different
   object than the one on disk -- and nothing else under C141 is modified;
3. the two generations differ, so the restore is not a copy of the same bytes;
4. the guard added to ``w45_run_chain.py`` after the fact accepts the real write targets and rejects
   a foreign bundle's, which is the only reason to believe the "now armed" claim.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import re
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
import types
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C143/wave38-seeking-alpha-landing-20261019"
FOREIGN = "docs/evidence/C141/wave37-cboe-index-snapshots-20261019"
RUNS = REPO / BUNDLE / "runs"
MOVED_JSON = RUNS / "judge_pre_chain.report.json"
MOVED_MD = RUNS / "judge_pre_chain.report.md"
GIT = "/usr/bin/git"  # absolute executable, so bandit sees no partial path
CARRIER = RUNS / "w50_carrier_path_correction.txt"
W45 = REPO / BUNDLE / "probes" / "w45_run_chain.py"
FOREIGN_JSON = REPO / f"{FOREIGN}/runs/judge_pre_chain.report.json"
FOREIGN_MD = REPO / f"{FOREIGN}/runs/judge_pre_chain.report.md"
SUMMARY_FIELDS = (
    ("pre_chain_identities", "identity_count"),
    ("pre_chain_claims", "claim_count"),
    ("pre_chain_accepted", "offline_acceptance_count"),
    ("pre_chain_status_counts", "status_counts"),
)


def _git_bytes(*args: str) -> bytes:
    """Run git and return stdout as bytes; a non-zero exit is a failure, never an empty reading."""
    result = subprocess.run(  # noqa: S603  # nosec B603  # literal argv
        [GIT, "-C", str(REPO), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args} exit={result.returncode} stderr={result.stderr[:200]!r}")
    return result.stdout


def _summary(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return dict(document.get("summary") or {})


def _carrier_reading() -> dict[str, Any]:
    """Read the pre-chain numbers the chain carrier printed while the chain ran."""
    text = (RUNS / "chain_summary.txt").read_text(encoding="utf-8")
    match = re.search(r"pre_chain_stale_reading=(\{.*?\}) post_chain_reading=", text)
    if match is None:
        raise AssertionError("chain_summary.txt carries no pre_chain_stale_reading block")
    return dict(ast.literal_eval(match.group(1)))


def _matches(summary: dict[str, Any], carrier: dict[str, Any]) -> bool:
    """True when a judge report summary is the reading the carrier recorded."""
    return all(carrier.get(left) == summary.get(right) for left, right in SUMMARY_FIELDS)


def main() -> int:
    """Compare the moved payload to the carrier, prove the restore, and arm the guard."""
    lines: list[str] = []
    arms: dict[str, bool] = {}

    moved = _summary(MOVED_JSON)
    carrier = _carrier_reading()
    forged = copy.deepcopy(moved)
    forged["claim_count"] = int(forged["claim_count"]) + 5
    forged["status_counts"] = dict(forged["status_counts"] or {})
    forged["status_counts"]["STALE"] = int(forged["status_counts"]["STALE"]) + 5
    lines.append(
        f"moved_payload identities={moved.get('identity_count')} claims={moved.get('claim_count')} "
        f"accepted={moved.get('offline_acceptance_count')} "
        f"status_counts={moved.get('status_counts')} "
        f"sha256={hashlib.sha256(MOVED_JSON.read_bytes()).hexdigest()[:16]} "
        f"json_bytes={MOVED_JSON.stat().st_size} md_bytes={MOVED_MD.stat().st_size}"
    )
    lines.append(f"carrier_reading {json.dumps(carrier, ensure_ascii=False, sort_keys=True)}")
    lines.append(
        f"counterfact_forged claims={forged['claim_count']} "
        f"matches_carrier={_matches(forged, carrier)} "
        f"real_matches_carrier={_matches(moved, carrier)}"
    )
    arms["moved_payload_matches_carrier"] = _matches(moved, carrier) and not _matches(
        forged, carrier
    )

    foreign = _summary(FOREIGN_JSON)
    lines.append(
        f"foreign_generation identities={foreign.get('identity_count')} "
        f"claims={foreign.get('claim_count')} "
        f"accepted={foreign.get('offline_acceptance_count')} "
        f"status_counts={foreign.get('status_counts')} "
        f"differs_from_moved={foreign != moved} "
        f"foreign_matches_wave38_carrier={_matches(foreign, carrier)}"
    )
    arms["generations_differ"] = (
        foreign != moved
        and foreign.get("claim_count") != moved.get("claim_count")
        and not _matches(foreign, carrier)
    )

    modified_foreign = sorted(
        _git_bytes("diff", "--name-only", "HEAD", "--", f"{FOREIGN}/").decode().splitlines()
    )
    byte_equal: list[str] = []
    for path in (FOREIGN_JSON, FOREIGN_MD):
        rel = str(path.relative_to(REPO))
        head_bytes = _git_bytes("show", f"HEAD:{rel}")
        worktree_bytes = path.read_bytes()
        byte_equal.append(
            f"{Path(rel).name} head={hashlib.sha256(head_bytes).hexdigest()[:16]} "
            f"worktree={hashlib.sha256(worktree_bytes).hexdigest()[:16]} "
            f"equal={head_bytes == worktree_bytes}"
        )
    lines.append(f"foreign_bundle_modified_files={len(modified_foreign)} {modified_foreign[:6]}")
    lines.extend(f"restore {row}" for row in byte_equal)
    arms["foreign_carriers_restored"] = (
        modified_foreign == []
        and len(byte_equal) == 2
        and all("equal=True" in row for row in byte_equal)
    )

    w45_text = W45.read_text(encoding="utf-8")
    repointed = 'module.PRE_CHAIN_OUTPUT = f"{BUNDLE}/runs/judge_pre_chain.report.json"' in w45_text
    armed = "_write_targets(chain)" in w45_text
    spec = importlib.util.spec_from_file_location("w45_for_face", W45)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot import {W45}")
    w45: Any = importlib.util.module_from_spec(spec)
    sys.modules["w45_for_face"] = w45
    spec.loader.exec_module(w45)
    chain = w45._load_driver()
    _real_targets, real_outside = w45._write_targets(chain)
    fake = types.SimpleNamespace(
        RUNS=REPO / FOREIGN / "runs",
        PRE_CHAIN_OUTPUT=f"{FOREIGN}/runs/judge_pre_chain.report.json",
    )
    _fake_targets, fake_outside = w45._write_targets(fake)
    lines.append(
        f"guard real_outside={real_outside} forged_outside={fake_outside} "
        f"driver_pre_chain_output={chain.PRE_CHAIN_OUTPUT} "
        f"driver_runs={chain.RUNS} repointed={repointed} armed={armed}"
    )
    arms["write_target_guard_bites"] = (
        real_outside == [] and len(fake_outside) == 2 and repointed and armed
    )

    failed = sorted(name for name, ok in arms.items() if not ok)
    lines.append(
        f"carrier_path_face arms={len(arms)} arms_passed={sum(arms.values())} "
        f"arms_failed={len(failed)} failed={failed} PASS={not failed}"
    )
    exit_code = 0 if not failed else 1
    lines.append("")
    lines.append(f"w50_carrier_path_correction_exit={exit_code}")
    body = "\n".join(lines) + "\n"
    CARRIER.write_text(
        "# carrier for w50_carrier_path_correction.py\n"
        f"# argv: {sys.executable} "
        f"{REPO / BUNDLE / 'probes' / 'w50_carrier_path_correction.py'}\n\n" + body,
        encoding="utf-8",
    )
    print(body, end="")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
