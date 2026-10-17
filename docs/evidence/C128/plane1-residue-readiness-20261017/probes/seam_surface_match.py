"""Measure whether the shared client seam can answer each provider's transport verbs.

A provider that owns its transport is witnessed by patching the module attribute
``get_shared_http_client`` with :class:`iter2_claims_probe_runner._FakeSharedClient`. That fake
publishes only the methods the runner implements, so a fetcher that calls a verb outside the fake's
surface cannot be witnessed through the seam at all -- the producer would die on an
``AttributeError`` that says nothing about the contract. Widening the fake means editing the shared
runner, which is a full re-sign of every seam claim in the repository, not a per-provider wave.

This probe decides that question from the shipped code rather than from prose, and it carries both
arms, because a one-sided "tmx is blocked" reading would be exactly the verbal judgment the
acceptance rule forbids:

* arm A is the positive control. It must read WITNESSABLE, and the judge report must show rows of
  that provider accepted -- otherwise "the seam answers this verb" is unfalsified.
* arm B is the claim under test: every verb each un-witnessed provider's transport module calls on
  the shared client, intersected with the fake's public surface.

There are three verdicts, not two. An empty verb set means the collection found no call on the
shared client at all, so the surface question was never posed -- it does *not* mean the seam
answers this provider's transport. Providers that own their transport (a lazy SDK import, an
injected ``http_transport`` for the engine leg) read NO_SEAM_VERBS_MEASURED and need a different
witness mechanism; collapsing that into WITNESSABLE would make the arm unsatisfiable-by-failure and
turn a reachability claim into the verbal judgment the acceptance rule forbids.

Prints one line per provider plus the face line the narrative quotes, and exits non-zero when an
arm reads opposite to its expected verdict.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))

import iter2_claims_probe_runner as runner  # noqa: E402  # the shipped fake is the measure

JUDGE_REPORT = REPO_ROOT / "docs/evidence/C101/judge/claims.merged.report.json"
PROVIDERS = (("eia", "WITNESSABLE"), ("tmx", "BLOCKED_ON_RUNNER_SURFACE"))
VERDICTS = ("WITNESSABLE", "BLOCKED_ON_RUNNER_SURFACE", "NO_SEAM_VERBS_MEASURED")


def _fake_surface() -> set[str]:
    """Every public callable the shared fake client actually answers."""
    return {
        name
        for name, value in vars(runner._FakeSharedClient).items()
        if not name.startswith("_") and callable(value)
    }


def _verbs_in(module_path: Path) -> set[str]:
    """Collect the verbs one module calls on ``get_shared_http_client()``."""
    verbs: set[str] = set()
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    # Shape 1: get_shared_http_client().verb(...) -- the governed client read inline.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "get_shared_http_client"
        ):
            verbs.add(node.func.attr)
    # Shape 2: client = get_shared_http_client() and a later client.verb(...) in one module.
    holders = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "get_shared_http_client"
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in holders
        ):
            verbs.add(node.func.attr)
    return verbs


def _transport_verbs(provider: str) -> set[str]:
    """Every verb this provider's ``models/`` modules call on the shared client."""
    models_dir = REPO_ROOT / "opendata/data/providers" / provider / "models"
    verbs: set[str] = set()
    for module_path in sorted(models_dir.glob("*.py")):
        verbs |= _verbs_in(module_path)
    return verbs


def _load_report() -> dict[str, Any]:
    """Read the judge report that the gate actually uses, at the shipped path."""
    report: dict[str, Any] = json.loads(JUDGE_REPORT.read_text(encoding="utf-8"))
    return report


def _accepted_by_provider(report: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Count judge statuses per provider straight out of the report the gate reads."""
    counts: dict[str, dict[str, int]] = {}
    for row in report["models"]:
        provider, _sep, _model = row["model_identity"].partition("::")
        bucket = counts.setdefault(provider, {})
        bucket[row["status"]] = bucket.get(row["status"], 0) + 1
    return counts


def main(argv: list[str] | None = None) -> int:
    """Print each arm's measured verbs and refuse a reading opposite to its expectation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--arm",
        action="append",
        default=[],
        metavar="PROVIDER:VERDICT",
        help="extra arm to measure, e.g. government_us:WITNESSABLE; repeatable",
    )
    args = parser.parse_args(argv)
    arms = list(PROVIDERS)
    for spec in args.arm:
        provider, _sep, expected = spec.partition(":")
        if not provider or expected not in VERDICTS:
            raise SystemExit(f"bad --arm {spec!r}, want PROVIDER:{'|'.join(VERDICTS)}")
        arms.append((provider, expected))
    surface = _fake_surface()
    report: dict[str, Any] = json.loads(JUDGE_REPORT.read_text(encoding="utf-8"))
    identity_set = report["inventory"]["expected_identity_set_sha256"][:16]
    accepted = _accepted_by_provider(report)
    failures = 0
    for provider, expected in arms:
        verbs = _transport_verbs(provider)
        missing = sorted(verbs - surface)
        if not verbs:
            verdict = "NO_SEAM_VERBS_MEASURED"
        elif missing:
            verdict = "BLOCKED_ON_RUNNER_SURFACE"
        else:
            verdict = "WITNESSABLE"
        face = accepted.get(provider, {})
        print(
            f"seam_surface_provider={provider} fake_methods={sorted(surface)} "
            f"verbs={sorted(verbs)} missing={missing} verdict={verdict} "
            f"expected={expected} judge_statuses={face}"
        )
        if verdict != expected:
            failures += 1
        if expected == "WITNESSABLE" and not face.get("OFFLINE_ACCEPTED"):
            # An arm may not be satisfied by a phrase: the positive control has to show rows.
            failures += 1
    print(
        f"seam_surface_arms={len(arms)} seam_surface_failed={failures} identity_set={identity_set}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
