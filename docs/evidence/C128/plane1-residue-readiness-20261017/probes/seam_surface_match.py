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
* arm B is the claim under test: every verb each arm's provider transport module calls on the shared
  client, intersected with the fake's public surface.

There are three verdicts, not two. An empty verb set means the collection found no call on the
shared client at all, so the surface question was never posed -- it does *not* mean the seam
answers this provider's transport. Providers that own their transport (a lazy SDK import, an
injected ``http_transport`` for the engine leg) read NO_SEAM_VERBS_MEASURED and need a different
witness mechanism; collapsing that into WITNESSABLE would make the arm unsatisfiable-by-failure and
turn a reachability claim into the verbal judgment the acceptance rule forbids.

Wave 30 re-based the second arm instead of deleting its expectation, and paid for the third verdict.
tmx used to be the face that proved the blockage: its transport calls ``request``, a verb the fake
answered only after wave 29 widened it. Run against the 285/350 report the probe now prints
``verbs=['request'] missing=[] verdict=WITNESSABLE`` for tmx, and the stale constant exited 1 (that
red reading is archived as the C130 carrier ``wave30-step0a-seam_surface_match.txt``), so the old
expectation was a landed-face value baked into an arm. Keeping the branch alive is a separate
problem: a census of all 32 providers in the report finds no live module that calls a verb outside
``get``/``request``, so no real provider carries that verdict any more and it can only be shown on
the surface as it was *before* wave 29. The forged arms read every provider's verb set against both
surfaces -- which rows reach outside ``get`` is derived, not named here -- and carry a ``get``-only
accepted provider beside them as the arm that must not flip. deribit is the live
NO_SEAM_VERBS_MEASURED carrier: it owns its transport, is accepted for its rows, and so shows that
the verdict is a different mechanism, not a blockage.

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
PROVIDERS = (
    ("eia", "WITNESSABLE"),
    ("tmx", "WITNESSABLE"),
    ("deribit", "NO_SEAM_VERBS_MEASURED"),
)
VERDICTS = ("WITNESSABLE", "BLOCKED_ON_RUNNER_SURFACE", "NO_SEAM_VERBS_MEASURED")

#: The fake's public surface as it was before wave 29 widened it to answer ``request``. No live
#: provider reads a verb outside the shipped surface any more, so this is the only carrier left for
#: the BLOCKED verdict -- the forged arms below measure real verb sets against it, and which
#: providers those are gets derived from the same read rather than named here.
PRE_WIDENING_SURFACE = frozenset({"get"})


def _verdict(verbs: set[str], surface: set[str]) -> str:
    """The shipped three-valued ladder, read against whichever surface the arm supplies."""
    if not verbs:
        return "NO_SEAM_VERBS_MEASURED"
    if verbs - surface:
        return "BLOCKED_ON_RUNNER_SURFACE"
    return "WITNESSABLE"


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
        verdict = _verdict(verbs, surface)
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

    # The BLOCKED branch has to stay capable of differing, so it is measured -- not asserted -- on
    # the pre-wave-29 surface. Which providers that branch applies to is derived from the same verb
    # read (no landed name list), and a get-only provider is carried beside them as the arm that
    # must not flip: without it "the surface changed" and "this provider changed" are the same face.
    pre = set(PRE_WIDENING_SURFACE)
    verb_sets = {p: _transport_verbs(p) for p in sorted(accepted)}
    flipped = [p for p, verbs in verb_sets.items() if verbs and verbs - pre]
    control = next((p for p, _ in arms if _transport_verbs(p) <= pre), "")
    forged = 0
    if not flipped:
        # No live provider would have been blocked by the old surface, so this generation cannot
        # show the BLOCKED branch. Say so and refuse rather than let the forged arm read vacuous.
        forged += 1
        print("seam_surface_prewidening flipped=NONE PASS=False")
    for provider in flipped:
        verbs = verb_sets[provider]
        live_v, pre_v = _verdict(verbs, surface), _verdict(verbs, pre)
        ok = live_v == "WITNESSABLE" and pre_v == "BLOCKED_ON_RUNNER_SURFACE"
        forged += 0 if ok else 1
        print(
            f"seam_surface_prewidening provider={provider} verbs={sorted(verbs)} "
            f"live={live_v} pre_widening={pre_v} "
            f"expected=BLOCKED_ON_RUNNER_SURFACE PASS={ok}"
        )
    if control:
        control_verbs = _transport_verbs(control)
        ok = _verdict(control_verbs, pre) == "WITNESSABLE"
        forged += 0 if ok else 1
        print(
            f"seam_surface_prewidening provider={control} verbs={sorted(control_verbs)} "
            f"live={_verdict(control_verbs, surface)} pre_widening={_verdict(control_verbs, pre)} "
            f"expected=WITNESSABLE PASS={ok}"
        )
    else:
        forged += 1
        print("seam_surface_prewidening control=NONE PASS=False")
    print(
        f"seam_surface_arms={len(arms)} flipped_by_widening={flipped} control={control} "
        f"seam_surface_failed={failures + forged} identity_set={identity_set}"
    )
    return 1 if (failures + forged) else 0


if __name__ == "__main__":
    raise SystemExit(main())
