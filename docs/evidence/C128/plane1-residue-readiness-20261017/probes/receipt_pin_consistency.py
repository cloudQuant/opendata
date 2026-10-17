"""Receipt-pin consistency gate -- the pre-commit detector for a stale digest pin.

Every phase receipt under ``docs/evidence/C101/receipts/<provider>/<Model>/`` records ``pins`` for
five kinds (``contract``, ``fixture``, ``probe``, ``source``, ``contract_version``). A pin holds the
``before_sha256`` and ``after_sha256`` of a file the run touched. ``contract_version`` is a string,
not a list, so it is skipped rather than counted as a zero.

Why this exists as an instrument and not as a look: when a wave edits a *shared* file (the runner,
the fixture builder, a witness module) after some receipts were produced, those receipts keep
pinning the old bytes. The acceptance rule then says a row's evidence points at a file that no
longer exists in the shape it claims -- gate member 2 flags that, usually long after the wave that
caused it and attributed to the wrong wave. This probe closes the gap inside the wave: it re-hashes
every pinned path against the worktree and prints the count of mismatches.

Three arms, all required, so a zero cannot be a mute counter:

* arm A -- clean check over every receipt of every provider (or ``--providers`` subset). Counts
  ``stale`` (disk bytes differ from ``after_sha256``, or the path is gone) and ``before_ne_after``
  (slots whose two recorded digests differ). The measured tree-wide reading is ``before_ne_after=0``
  over all slots: the recorder writes the same digest into both fields, so the pin pair carries no
  mutation claim and only ``after_sha256`` is a live assertion. That is why arm C exists.
* arm B -- the same walk with one pinned path's expectation replaced by a real, *different* digest
  taken from another pinned file. The mismatch count must become non-zero and must equal the number
  of slots that reference the targeted path. If arm B also reads 0, the stale checker is dead.
* arm C -- the counter-proof for ``before_ne_after``: one synthetic slot, in memory, whose disk
  bytes match ``after_sha256`` but whose ``before_sha256`` is a different digest. It must read
  ``stale=0 before_ne_after=1``. Without it the arm-A zero is indistinguishable from a wiring gap.

Nothing is written; arm C fabricates in memory only. Read-only census, exits non-zero when an arm
disagrees.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT = Path(__file__).resolve().parents[5]
RECEIPTS_REL = "docs/evidence/C101/receipts"
PIN_KINDS = ("contract", "fixture", "probe", "source")
ARM_B_TARGET_KIND = "probe"
ARM_B_TARGET_SUFFIX = "iter2_claims_fixtures.py"
ARM_B_SUBSTITUTE = "scripts/quality/iter2_claims_probe_runner.py"
FAKE_SHA = "0" * 64


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _receipts(providers: list[str] | None) -> list[Path]:
    root = REPO_ROOT / RECEIPTS_REL
    if not root.is_dir():
        return []
    dirs = (
        [root / p for p in providers]
        if providers
        else sorted(d for d in root.iterdir() if d.is_dir())
    )
    return sorted(f for d in dirs for f in d.rglob("*.json"))


def _slots(receipt: Path) -> list[dict[str, Any]]:
    data = json.loads(receipt.read_text(encoding="utf-8"))
    pins = data.get("pins") or {}
    out: list[dict[str, Any]] = []
    for kind, entries in pins.items():
        if not isinstance(entries, list):
            continue  # contract_version is a string; not a byte pin
        out.extend({"kind": kind, **pin} for pin in entries)
    return out


def _labelled(receipts: list[Path]) -> Iterable[tuple[str, dict[str, Any]]]:
    for receipt in receipts:
        rel = str(receipt.relative_to(REPO_ROOT))
        for pin in _slots(receipt):
            yield rel, pin


def _walk(
    slots: Iterable[tuple[str, dict[str, Any]]],
    tampered: bool = False,
    substitute: str | None = None,
) -> tuple[int, int, int, list[str], dict[str, int]]:
    """Return (slots, stale, before_ne_after, samples, per-kind counts) vs the worktree bytes."""
    total = stale = before_ne_after = 0
    samples: list[str] = []
    per_kind: dict[str, int] = defaultdict(int)
    for label, pin in slots:
        total += 1
        per_kind[pin["kind"]] += 1
        want = pin["after_sha256"]
        if pin.get("before_sha256") != want:
            before_ne_after += 1
        if (
            tampered
            and pin["kind"] == ARM_B_TARGET_KIND
            and pin["path"].endswith(ARM_B_TARGET_SUFFIX)
        ):
            want = substitute
        disk = _sha(REPO_ROOT / pin["path"])
        if disk is None or disk != want:
            stale += 1
            if len(samples) < 5:
                samples.append(f"{label}#{pin['kind']}:{pin['path']}")
    return total, stale, before_ne_after, samples, dict(per_kind)


def _arm_c() -> tuple[int, int, bool]:
    """Show that ``before_ne_after`` can move: one synthetic slot, disk-clean, before faked."""
    path = ARM_B_SUBSTITUTE
    after = _sha(REPO_ROOT / path)
    if after is None:
        return 0, 0, False
    fake = [
        (
            "synthetic#probe:arm_c.json",
            {"kind": "probe", "path": path, "before_sha256": FAKE_SHA, "after_sha256": after},
        )
    ]
    total, stale, bna, _, _ = _walk(fake)
    return stale, bna, total == 1 and stale == 0 and bna == 1


def main(argv: list[str] | None = None) -> int:
    """Walk the receipts, run the three arms, and print the face line the narrative quotes."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--providers", nargs="*", default=None, help="provider shards to walk (default: all)"
    )
    args = ap.parse_args(argv)

    receipts = _receipts(args.providers)
    scope = ",".join(args.providers) if args.providers else "ALL"
    print(f"pin_scope={scope} receipt_files={len(receipts)} pin_kinds={','.join(PIN_KINDS)}")
    if not receipts:
        print("pin_face PASS=False reason=no-receipts-in-scope")
        return 1

    slots, stale, bna, samples, per_kind = _walk(_labelled(receipts))
    substitute = _sha(REPO_ROOT / ARM_B_SUBSTITUTE)
    t_slots, t_stale, _, _, _ = _walk(_labelled(receipts), tampered=True, substitute=substitute)
    targeted = sum(
        1
        for _, p in _labelled(receipts)
        if p["kind"] == ARM_B_TARGET_KIND and p["path"].endswith(ARM_B_TARGET_SUFFIX)
    )
    c_stale, c_bna, c_ok = _arm_c()

    print(
        f"A_clean: slots={slots} stale={stale} before_ne_after={bna} "
        f"samples={samples} by_kind={dict(sorted(per_kind.items()))}"
    )
    print(
        f"B_forced: slots={t_slots} stale={t_stale} targeted_slots={targeted} "
        f"substitute={ARM_B_SUBSTITUTE} must_equal_targeted={t_stale == targeted}"
    )
    print(
        f"C_rearm: stale={c_stale} before_ne_after={c_bna} expected=stale=0/"
        f"before_ne_after=1 PASS={c_ok}"
    )
    distinct = len({p["path"] for _, p in _labelled(receipts)})
    passed = sum([slots > 0 and stale == 0, targeted > 0 and t_stale == targeted, c_ok])
    print(
        f"pin_face slots={slots} stale={stale} before_ne_after={bna} "
        f"distinct_pinned_paths={distinct} arms=3 arms_passed={passed} "
        f"arms_failed={3 - passed} PASS={passed == 3}"
    )
    return 0 if passed == 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
