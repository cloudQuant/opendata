"""Falsification census for the shape of the plane-1 residue after wave 22.

The predicate shipped beside this probe counts 90 unaccepted identities. A count cannot be
scheduled, so this census asks the one question that can: *what kind of edit does each residue
row need?* It answers by joining the judge's status per identity against a measured fact -- which
providers have ever had a claims producer run at all, read off the filesystem as ``*/claims.json``
under the evidence root, never hand-typed.

Two arms, printed as verdict lines:

* ``A_coverage_split`` -- the gate. Every ``UNREVIEWED`` identity belongs to a provider with no
  ``claims.json`` on disk, and every ``INCOMPLETE`` identity belongs to one that has. If the gate
  holds, plane 1's remaining 90 rows are two disjoint jobs: 83 rows whose provider has never been
  produced at all (the producer run is the precondition for any witness there), and 7 rows inside
  already-produced providers that a shard addition can reach. A single row landing on the wrong
  side turns the arm red, and the arm prints both directions so a partial overlap is visible
  rather than averaged away.
* ``B_control_flip`` -- the instrument must be able to disagree with itself. Two perturbations of
  the *coverage set only* (statuses and identities are untouched) are applied in memory: drop a
  provider that holds an ``INCOMPLETE`` row from the produced set, and add a provider that holds an
  ``UNREVIEWED`` row to it. Each perturbation must turn exactly one of the two sub-invariants red,
  and the unperturbed run must stay green. Without this, ``A`` could be green because of a constant
  rather than because of the join -- the failure shape this repo has been bitten by repeatedly.

The population is the judge's own ``models`` array (350 identities), and the freeze signal
``inventory.expected_identity_set_sha256`` is re-read and compared, so the census cannot silently
be describing a different inventory.

Run from the repository root with ``PYTHONPATH=. python`` followed by this path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT))

REPORT: Final[str] = "docs/evidence/C101/judge/claims.merged.report.json"
IDENTITY_SET_SHA256: Final[str] = "5168e1e723999312b0978bf9b1a8999c0b4960cdb12013e43e1e59e349e21e65"
UNREVIEWED: Final[str] = "UNREVIEWED"
INCOMPLETE: Final[str] = "INCOMPLETE"
OFFLINE_ACCEPTED: Final[str] = "OFFLINE_ACCEPTED"


def _provider(identity: str) -> str:
    return identity.split("::", 1)[0]


def _status_map(report: dict[str, Any]) -> dict[str, str]:
    return {str(model["model_identity"]): str(model.get("status")) for model in report["models"]}


def _produced_providers(evidence_root: Path) -> set[str]:
    """Providers with a claims.json directly under the evidence root -- read from disk."""
    return {p.parent.name for p in evidence_root.glob("*/claims.json") if p.is_file()}


def _split(status_map: dict[str, str], produced: set[str]) -> dict[str, set[str]]:
    out = {
        "unreviewed_in_unproduced": {
            i for i, s in status_map.items() if s == UNREVIEWED and _provider(i) not in produced
        },
        "unreviewed_in_produced": {
            i for i, s in status_map.items() if s == UNREVIEWED and _provider(i) in produced
        },
        "incomplete_in_produced": {
            i for i, s in status_map.items() if s == INCOMPLETE and _provider(i) in produced
        },
        "incomplete_in_unproduced": {
            i for i, s in status_map.items() if s == INCOMPLETE and _provider(i) not in produced
        },
    }
    return out


def _residue(status_map: dict[str, str], produced: set[str], *, inside: bool) -> int:
    """Count unaccepted identities whose provider is (``inside``) or is not a produced one."""
    return sum(
        1
        for identity, status in status_map.items()
        if status != OFFLINE_ACCEPTED and (_provider(identity) in produced) is inside
    )


def _verdict(arm: str, note: str, ok: bool) -> bool:
    print(f"{arm}: {'PASS' if ok else 'FAIL'} {note}")
    return ok


def arm_coverage_split(status_map: dict[str, str], produced: set[str]) -> dict[str, Any]:
    """Read the coverage join, and return it for both the gate and the control's targets."""
    parts = _split(status_map, produced)
    unreviewed = {i for i, s in status_map.items() if s == UNREVIEWED}
    incomplete = {i for i, s in status_map.items() if s == INCOMPLETE}
    accepted = {i for i, s in status_map.items() if s == OFFLINE_ACCEPTED}
    unproduced = sorted({p for p in map(_provider, status_map) if p not in produced})
    residue_by_kind = {
        "never_produced": _residue(status_map, produced, inside=False),
        "produced": _residue(status_map, produced, inside=True),
    }
    expected_residue = {"never_produced": len(unreviewed), "produced": len(incomplete)}
    ok = (
        len(status_map) == 350
        and len(accepted) == 260
        and parts["unreviewed_in_produced"] == set()
        and parts["incomplete_in_unproduced"] == set()
        and parts["unreviewed_in_unproduced"] == unreviewed
        and parts["incomplete_in_produced"] == incomplete
        and len(unreviewed) + len(incomplete) == 90
        and residue_by_kind == expected_residue
    )
    return {
        "ok": ok,
        "parts": parts,
        "unproduced": unproduced,
        "counts": {
            "identities": len(status_map),
            "accepted": len(accepted),
            "unreviewed": len(unreviewed),
            "incomplete": len(incomplete),
        },
        "residue_by_kind": residue_by_kind,
        "incomplete_providers": sorted({_provider(i) for i in incomplete}),
        "unreviewed_providers": sorted({_provider(i) for i in unreviewed}),
    }


def main() -> int:
    """Run both arms and exit 0 only when the gate is green and the control can turn it red."""
    evidence_root = REPO_ROOT / "docs/evidence/C101"
    report = json.loads((REPO_ROOT / REPORT).read_text(encoding="utf-8"))
    status_map = _status_map(report)
    produced = _produced_providers(evidence_root)
    identity_set = str(report["inventory"]["expected_identity_set_sha256"])

    reading = arm_coverage_split(status_map, produced)
    arm_a = _verdict(
        "A_coverage_split",
        f"identities={reading['counts']['identities']}"
        f" accepted={reading['counts']['accepted']}"
        f" unreviewed={reading['counts']['unreviewed']}"
        f" incomplete={reading['counts']['incomplete']}"
        f" produced_providers={len(produced)} never_produced={len(reading['unproduced'])}"
        f" residue_by_kind={reading['residue_by_kind']}"
        f" unreviewed_in_produced={len(reading['parts']['unreviewed_in_produced'])}"
        f" incomplete_in_unproduced={len(reading['parts']['incomplete_in_unproduced'])}"
        f" identity_set={identity_set[:16]}"
        f" frozen={identity_set == IDENTITY_SET_SHA256}"
        f" never_produced_list={reading['unproduced']}",
        reading["ok"] and identity_set == IDENTITY_SET_SHA256,
    )

    # Arm B: perturb only the coverage set, and require exactly one sub-invariant to break each way.
    # Each "other invariant still green" clause is a consequence of arm A's preconditions, not an
    # independent measurement; it is printed so the reader can see which side of the partition the
    # perturbation is supposed to land on and confirm it did.
    incomplete_holders = sorted({_provider(i) for i in reading["parts"]["incomplete_in_produced"]})
    unreviewed_holders = sorted(
        {_provider(i) for i in reading["parts"]["unreviewed_in_unproduced"]}
    )
    drop = incomplete_holders[0] if incomplete_holders else "<none>"
    add = unreviewed_holders[0] if unreviewed_holders else "<none>"
    without = _split(status_map, produced - {drop})
    with_extra = _split(status_map, produced | {add})
    p1 = {
        "unperturbed": reading["parts"]["unreviewed_in_produced"] == set(),
        "drop": without["unreviewed_in_produced"] == set(),
        "add": with_extra["unreviewed_in_produced"] == set(),
    }
    p2 = {
        "unperturbed": reading["parts"]["incomplete_in_unproduced"] == set(),
        "drop": without["incomplete_in_unproduced"] == set(),
        "add": with_extra["incomplete_in_unproduced"] == set(),
    }
    flip_drop = p2["drop"] is False and p1["drop"] is True
    flip_add = p1["add"] is False and p2["add"] is True
    arm_b = _verdict(
        "B_control_flip",
        f"drop={drop} add={add}"
        f" drop_misfiles={len(without['incomplete_in_unproduced'])} incomplete_as_unproduced"
        f" add_misfiles={len(with_extra['unreviewed_in_produced'])} unreviewed_as_produced"
        f" p1_unreviewed_subset={p1} p2_incomplete_subset={p2}"
        f" drop_reds_only_p2={flip_drop} add_reds_only_p1={flip_add}",
        flip_drop and flip_add and p1["unperturbed"] and p2["unperturbed"],
    )

    arms = [arm_a, arm_b]
    passed = sum(1 for ok in arms if ok)
    print(
        f"coverage_arms={len(arms)} coverage_passed={passed} coverage_failed={len(arms) - passed}"
    )
    return 0 if passed == len(arms) else 1


if __name__ == "__main__":
    raise SystemExit(main())
