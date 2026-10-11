"""Counterfact arms for the C95 roster tripwire.

``tests/test_model_capability_census.py::test_registry_shape_is_anchored_or_declared_unproven``
asserts an exact roster of the engine capabilities that still have no declaration probe. C95 grew
that roster from 11 to 15 by registering four row-named capabilities, so the face that needs proving
is that the roster is a tripwire and not a rubber stamp: every arm below drives the REPO'S OWN test
function (imported, never re-typed) against a tampered ``mcc.REGISTRY`` and requires the verdict
to move.

C0 as-is registry      -> PASS (the shipped tree)
C1 drop one roster row -> AssertionError (a probe-less capability silently gains a probe)
C2 append an entry     -> AssertionError (a new engine capability lands unrostered)
C3 probe-less claim    -> AssertionError, and the module's own helper names the forged entry

Run: python docs/evidence/C95/roster-tripwire-counterfact.py
"""

from __future__ import annotations

import dataclasses
import pathlib
import sys
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import test_model_capability_census as the_test  # noqa: E402

import scripts.quality.model_capability_census as mcc  # noqa: E402

FAILURES: list[str] = []
NEW = (
    "rows.needle_transform",
    "rows.order_by_parameter",
    "shape.long_to_wide_pivot",
    "columns.many_to_one_rename",
)
SHIPPED_ROSTER: list[str] = []


def _roster(entries: tuple[mcc.CapabilityEntry, ...]) -> list[str]:
    return sorted(e.capability for e in entries if e.probe is None and e.engine_work)


def _off_roster(entries: tuple[mcc.CapabilityEntry, ...]) -> str:
    """Name which of the test's faces the tamper moved, instead of printing both full lists.

    The roster delta and the bare-claim list are the two ways this test can go red, so an empty
    delta beside a non-empty claim list is what distinguishes C1/C2's red from C3's.
    """
    got = set(_roster(entries)) - set(SHIPPED_ROSTER)
    gone = set(SHIPPED_ROSTER) - set(_roster(entries))
    bare = the_test._claims_without_a_probe(list(entries))
    return f"added={sorted(got)} dropped={sorted(gone)} bare_claims={bare}"


def _verdict(entries: tuple[mcc.CapabilityEntry, ...]) -> tuple[bool, str]:
    """Run the repo's test function against a patched registry and report red/green.

    Imported without pytest's assertion rewriting, so a bare ``assert`` carries an empty message;
    the detail line falls back to the roster delta rather than dying on an empty ``splitlines()``.
    """
    try:
        with mock.patch.object(mcc, "REGISTRY", entries):
            the_test.test_registry_shape_is_anchored_or_declared_unproven()
    except AssertionError:
        return False, f"AssertionError {_off_roster(entries)}"
    except Exception as exc:  # a non-AssertionError red is still a red
        return False, f"{type(exc).__name__}: {str(exc).splitlines()[:1]}"
    return True, "passed"


SHIPPED_ROSTER.extend(_roster(mcc.REGISTRY))

c0, c0_detail = _verdict(mcc.REGISTRY)
print(f"C0 as-is registry: roster={len(SHIPPED_ROSTER)} of which new={len(NEW)} -> {c0_detail}")
if not c0:
    FAILURES.append(f"C0 shipped tree must be green, got {c0_detail}")
if len(SHIPPED_ROSTER) != 11 + len(NEW):
    FAILURES.append(f"C0 roster must be 11 pre-repair + 4 new, measured {len(SHIPPED_ROSTER)}")
if any(cap not in SHIPPED_ROSTER for cap in NEW):
    FAILURES.append(f"C0 the four C95 capabilities must all be on the roster, got {SHIPPED_ROSTER}")

minus = tuple(e for e in mcc.REGISTRY if e.capability != "rows.needle_transform")
c1, c1_detail = _verdict(minus)
print(f"C1 drop rows.needle_transform from roster: -> {c1_detail}")
if c1:
    FAILURES.append("C1 a probe-less capability vanishing from the roster must go red")
if "dropped=['rows.needle_transform']" not in c1_detail:
    FAILURES.append(f"C1 the drop must name exactly rows.needle_transform, got {c1_detail}")

phantom = mcc.CapabilityEntry(
    capability="shape.forged_unrostered",
    why="counterfact only: an engine capability that landed without joining the roster",
    proof_file="tests/test_model_capability_census.py",
    anchor="def test_registry_shape_is_anchored_or_declared_unproven",
    claim=False,
    engine_work=True,
    probe=None,
)
c2, c2_detail = _verdict(mcc.REGISTRY + (phantom,))
print(f"C2 append a probe-less engine-work entry: -> {c2_detail}")
if c2:
    FAILURES.append("C2 an unrostered new engine capability must go red")
if "added=['shape.forged_unrostered']" not in c2_detail:
    FAILURES.append(f"C2 the append must name exactly the phantom, got {c2_detail}")

bare_claim = tuple(
    dataclasses.replace(e, claim=True) if e.capability == "rows.needle_transform" else e
    for e in mcc.REGISTRY
)
c3, c3_detail = _verdict(bare_claim)
named = the_test._claims_without_a_probe(list(bare_claim))
print(
    f"C3 promote probe-less rows.needle_transform to claim=True: -> {c3_detail} "
    f"named_by_helper={named}"
)
if c3:
    FAILURES.append("C3 a presence claim with no probe must go red")
if "bare_claims=['rows.needle_transform']" not in c3_detail:
    FAILURES.append(f"C3 the red must be attributed to the bare claim, got {c3_detail}")
if named != ["rows.needle_transform"]:
    FAILURES.append(f"C3 the helper must name exactly the forged entry, got {named}")

print("\nVERDICT:", "PASS" if not FAILURES else "FAIL")
for failure in FAILURES:
    print("  -", failure)
sys.exit(0 if not FAILURES else 1)
