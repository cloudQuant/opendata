r"""Prove that wave 29's full re-production moved no verdict, only the pins.

The wave re-executed every claim shard because the shared runner is the ``probe`` pin of every claim
on disk (see ``reproduce_all_shards.py``). That is a large, expensive action whose only honest
justification is "the evidence still says what it said". This instrument is that claim measured: for
every provider, it compares HEAD's ``claims.json`` with the worktree's, identity by identity, on the
axis that matters -- the five phase verdicts -- and reports any identity whose sequence moved.

Two axes are deliberately NOT charged as movement: the pin digests (they must now equal the widened
runner, and arm ``C`` requires exactly that) and the wall-clock ``executed_at_utc`` (re-execution is
the point). A third axis is charged loudly: a claim whose ``receipt`` path or ``exit_code`` differs,
because that is the difference between "same verdict" and "a different run happened".

Arms:
* ``A_population_reconciles`` - every provider dir in HEAD is present in the worktree, plus deribit,
  which is this wave's new shard, and the identity counts add up;
* ``B_verdict_sequences_unchanged`` - for every identity that already had claims, the ordered
  ``(phase, status)`` pairs are byte-equal between HEAD and the worktree, with the new identities
  reported separately as additions;
* ``C_probe_pin_moved_on_the_executed_population`` - the pin axis is read over the population that
  *has* a pin: every executed worktree claim's ``probe`` digest equals the runner on disk, no
  executed HEAD claim's did, and the excluded claims are exactly the BLOCKED ones, which carry an
  empty ``probe`` list because nothing ran to hash;
* ``D_receipt_carriers_unchanged`` - each identity's claim list names the same receipt paths and the
  same exit codes as HEAD. The bytes *under* those paths are rewritten by the re-run -- the receipt
  document carries its own execution instant, so its digest cannot be an invariance target -- but a
  claim that pointed somewhere else, or recorded a different exit, would be a different run;
* ``E_blocked_explains_the_produce_red`` - the produce driver marks a shard red when it reports any
  non-PASS claim, and printed ``red=5``. This arm reads that driver's own ledger lines back from the
  committed raw log and shows every one of those five is blocked-with-zero-fail, so the wave's face
  says what the number means instead of repeating a mis-named predicate;
* ``F_signable_population`` - the identity count the re-sign is checked against: how many identities
  have all five phases PASS, and that every excluded one is BLOCKED five times over;
* ``G_additions_cannot_be_accepted`` - the seven added identities are deribit's five, which are PASS
  and are the wave's product, plus two government_us rows the producer records as BLOCKED with
  ``no offline witness configured``, so no accepted count can be inflated by them.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
PROBE_DIR = Path(__file__).resolve().parent
EVIDENCE = "docs/evidence/C101"
RUNNER_REL = "scripts/quality/iter2_claims_probe_runner.py"
PRODUCE_LOG = "wave29-step2c-reproduce_all_shards.txt"
PHASE_ORDER = ("implementation", "direct", "service", "contract", "counterfact")
LEDGER = re.compile(
    r"shard provider=(\S+) exit=(-?\d+) emitted=(\d+) pass=(\d+) fail=(\d+) blocked=(\d+)"
)


def git_show(path: str) -> bytes | None:
    """HEAD's bytes for one path, or None when HEAD does not carry it."""
    completed = subprocess.run(  # noqa: S603  # nosec B603, B607  # read-only git show
        ["git", "-C", str(REPO_ROOT), "show", f"HEAD:{path}"],  # noqa: S607
        capture_output=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def claims_by_identity(raw: bytes) -> dict[str, dict[str, dict[str, Any]]]:
    """Index one claims document as ``identity -> phase -> claim``."""
    document = json.loads(raw)
    index: dict[str, dict[str, dict[str, Any]]] = {}
    for claim in document.get("claims", []):
        identity = f"{claim.get('provider')}::{claim.get('upstream_model')}"
        index.setdefault(identity, {})[str(claim.get("phase"))] = claim
    return index


def probe_of(claim: dict[str, Any]) -> list[Any]:
    """The claim's ``probe`` pin list, which is empty exactly when nothing was executed."""
    return list((claim.get("pins") or {}).get("probe") or [])


def probe_digest(claim: dict[str, Any]) -> str:
    """The ``probe`` pin digest of one claim, or ``""`` for a claim that never ran."""
    probe = probe_of(claim)
    return str(probe[0].get("after_sha256", ""))[:16] if probe else ""


def produce_ledger() -> tuple[list[dict[str, int | str]], str]:
    """Read the produce driver's own committed raw log back: its ledger lines and face line."""
    lines = (PROBE_DIR.parent / "runs" / PRODUCE_LOG).read_text(encoding="utf-8").splitlines()
    rows: list[dict[str, int | str]] = []
    for line in lines:
        match = LEDGER.match(line.strip())
        if not match:
            continue
        rows.append(
            {
                "provider": match.group(1),
                "exit": int(match.group(2)),
                "emitted": int(match.group(3)),
                "passed": int(match.group(4)),
                "failed": int(match.group(5)),
                "blocked": int(match.group(6)),
            }
        )
    face = next((line for line in lines if line.startswith("reproduce_face")), "")
    return rows, face


def sequence(entry: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    """The ordered phase verdicts of one identity, in the shipped phase order."""
    return [(phase, str(entry[phase]["status"])) for phase in PHASE_ORDER if phase in entry]


def main() -> int:
    """Compare HEAD and worktree shard by shard and print one face line."""
    runner_digest = hashlib.sha256((REPO_ROOT / RUNNER_REL).read_bytes()).hexdigest()[:16]
    worktree_dirs = sorted(
        p.name for p in (REPO_ROOT / EVIDENCE).iterdir() if (p / "claims.json").is_file()
    )
    deribit_new = git_show(f"{EVIDENCE}/deribit/claims.json") is None
    moved: list[str] = []
    added: list[str] = []
    dropped: list[str] = []
    new_shards: list[str] = []
    carrier_moved: list[str] = []
    identity_statuses: dict[str, list[str]] = {}
    all_worktree: dict[str, dict[str, dict[str, Any]]] = {}
    exec_ok = exec_stale = no_pin = no_pin_blocked = 0
    head_exec = head_exec_old = 0
    head_identity_total = worktree_identity_total = 0
    for provider in worktree_dirs:
        relative = f"{EVIDENCE}/{provider}/claims.json"
        worktree = claims_by_identity((REPO_ROOT / relative).read_bytes())
        worktree_identity_total += len(worktree)
        all_worktree.update(worktree)
        for identity, entry in worktree.items():
            identity_statuses[identity] = [str(c["status"]) for c in entry.values()]
            for claim in entry.values():
                digest = probe_digest(claim)
                if digest:
                    exec_ok += digest == runner_digest
                    exec_stale += digest != runner_digest
                else:
                    no_pin += 1
                    no_pin_blocked += claim.get("status") == "BLOCKED"
        head_raw = git_show(relative)
        if head_raw is None:
            added.extend(sorted(worktree))
            new_shards.append(provider)
            print(f"shard {provider}: NEW_IN_WORKTREE identities={len(worktree)}")
            continue
        head = claims_by_identity(head_raw)
        head_identity_total += len(head)
        for entry in head.values():
            for claim in entry.values():
                digest = probe_digest(claim)
                if not digest:
                    continue
                head_exec += 1
                head_exec_old += digest != runner_digest
        dropped.extend(sorted(set(head) - set(worktree)))
        for identity, entry in sorted(worktree.items()):
            if identity not in head:
                added.append(f"{identity}(absent-in-HEAD-shard)")
                continue
            old_entry = head[identity]
            if sequence(entry) != sequence(old_entry):
                moved.append(f"{identity} head={sequence(old_entry)} now={sequence(entry)}")
            for phase, claim in entry.items():
                old = old_entry.get(phase, {})
                # only the receipt *path* is charged: the receipt document itself carries the
                # execution instant, so its bytes and digest are expected to differ per run.
                new_carrier = (claim.get("receipt") or {}).get("path")
                old_carrier = (old.get("receipt") or {}).get("path")
                if new_carrier != old_carrier or claim.get("exit_code") != old.get("exit_code"):
                    carrier_moved.append(f"{identity}:{phase}")
    signable = sorted(i for i, s in identity_statuses.items() if set(s) == {"PASS"} and len(s) == 5)
    blocked_ids = sorted(i for i, s in identity_statuses.items() if set(s) == {"BLOCKED"})
    mixed = sorted(set(identity_statuses) - set(signable) - set(blocked_ids))
    reasons = sorted(
        {
            str(all_worktree[i][phase].get("reason", ""))
            for i in blocked_ids
            for phase in all_worktree[i]
            if not probe_digest(all_worktree[i][phase])
        }
    )
    non_deribit_adds = sorted(i for i in added if not i.startswith("deribit::"))
    print(f"shards_worktree={len(worktree_dirs)} new_shards={new_shards} deribit_new={deribit_new}")
    print(
        f"identities head={head_identity_total} worktree={worktree_identity_total} "
        f"added={len(added)} dropped={len(dropped)}"
    )
    print(
        f"probe pins runner_sha16={runner_digest} head_exec={head_exec} "
        f"head_exec_not_disk={head_exec_old} exec_matches_disk={exec_ok} exec_stale={exec_stale} "
        f"no_pin={no_pin} no_pin_and_blocked={no_pin_blocked}"
    )
    print(f"verdict_moved={len(moved)} moved={moved[:6]}")
    print(f"additions={len(added)} added={added[:8]}")
    print(f"carrier_moved={len(carrier_moved)} examples={carrier_moved[:6]}")
    print(
        f"population identities={len(identity_statuses)} all_pass={len(signable)} "
        f"all_blocked={len(blocked_ids)} mixed={len(mixed)} all_blocked_ids={blocked_ids}"
    )
    print(f"additions_not_deribit={non_deribit_adds} blocked_reasons={reasons[:2]}")
    ledger, face_line = produce_ledger()
    ledger_fail = sum(int(str(r["failed"])) for r in ledger)
    ledger_blocked = sum(int(str(r["blocked"])) for r in ledger)
    ledger_exit = sum(1 for r in ledger if int(str(r["exit"])) != 0)
    driver_red = sorted(
        {str(r["provider"]) for r in ledger if int(str(r["blocked"])) or int(str(r["failed"]))}
    )
    print(
        f"produce_log shards={len(ledger)} exits_nonzero={ledger_exit} fail={ledger_fail} "
        f"blocked={ledger_blocked} red_by_blocked={driver_red} face={face_line}"
    )
    arm_a = (
        new_shards == ["deribit"]
        and deribit_new
        and not dropped
        and worktree_identity_total == head_identity_total + len(added)
    )
    arm_b = not moved
    arm_c = (
        exec_stale == 0 and exec_ok > 0 and head_exec_old == head_exec and no_pin == no_pin_blocked
    )
    arm_d = not carrier_moved
    arm_e = (
        len(ledger) == 25
        and ledger_exit == 0
        and ledger_fail == 0
        and ledger_blocked == no_pin
        and driver_red == sorted({i.partition("::")[0] for i in blocked_ids})
        and not mixed
    )
    arm_f = (
        len(signable) + len(blocked_ids) + len(mixed) == len(identity_statuses)
        and 5 * len(blocked_ids) == ledger_blocked
        and all(len(identity_statuses[i]) == 5 for i in blocked_ids)
    )
    deribit_adds = [i.partition("(")[0] for i in added if i.startswith("deribit::")]
    added_blocked = [i.partition("(")[0] for i in non_deribit_adds]
    arm_g = (
        all(i in set(blocked_ids) for i in added_blocked)
        and all(i in set(signable) for i in deribit_adds)
        and len(deribit_adds) == 5
    )
    faces = (
        (
            "A_population_reconciles",
            arm_a,
            f"new_shards={new_shards} dropped={len(dropped)} "
            f"head={head_identity_total} added={len(added)} worktree={worktree_identity_total}",
        ),
        ("B_verdict_sequences_unchanged", arm_b, f"moved={len(moved)} added={len(added)}"),
        (
            "C_probe_pin_moved_on_the_executed_population",
            arm_c,
            f"head_exec={head_exec} head_old={head_exec_old} matches={exec_ok} "
            f"stale={exec_stale} no_pin={no_pin}/{no_pin_blocked}",
        ),
        ("D_receipt_carriers_unchanged", arm_d, f"carrier_moved={len(carrier_moved)}"),
        (
            "E_blocked_explains_the_produce_red",
            arm_e,
            f"ledger_shards={len(ledger)} exit_nonzero={ledger_exit} fail={ledger_fail} "
            f"blocked={ledger_blocked} red={driver_red}",
        ),
        (
            "F_signable_population",
            arm_f,
            f"all_pass={len(signable)} all_blocked={len(blocked_ids)} mixed={len(mixed)}",
        ),
        (
            "G_additions_cannot_be_accepted",
            arm_g,
            f"non_deribit_added={len(non_deribit_adds)} of_blocked={len(blocked_ids)}",
        ),
    )
    for name, ok, detail in faces:
        print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
    all_ok = all(ok for _, ok, _ in faces)
    print(
        f"reproduce_invariance_face shards={len(worktree_dirs)} arms={len(faces)} "
        f"moved={len(moved)} signable={len(signable)} verdict={'GREEN' if all_ok else 'RED'}"
    )
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
