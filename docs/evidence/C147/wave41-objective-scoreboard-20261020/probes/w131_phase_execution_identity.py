"""Wave-41u: all 1,695 phase receipts are executed, green, and name their own row and phase.

``w75`` proves the five legs exist and their pinned digests are current; ``w130`` proves the
counterfact's observed code equals its expected code and the witness declaration. Neither reads the
``machine_execution`` block of the other four phases. This face does, for every accepted row:

* 339 x 5 = 1,695 receipts must exist;
* each must carry ``status == "PASS"`` and ``machine_execution.exit_code == 0``;
* each ``actual_command`` must name *its own* provider, model and phase (a copy-pasted receipt from
  another row or phase would still be green on status and exit code);
* each ``task_id`` must be ``OBB2-<provider>-<model>``, tying the receipt to the row identity the
  ledger uses.

Population from the judge report, receipts from the receipt tree. The command-identity arm is forged
in memory once so a zero-mismatch reading cannot be vacuous.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C147/wave41-objective-scoreboard-20261020"
RUNS = REPO / BUNDLE / "runs"
CARRIER = RUNS / "w131_phase_execution_identity.txt"
REPORT = REPO / "docs/evidence/C101/judge/claims.merged.report.json"
RECEIPTS = REPO / "docs/evidence/C101/receipts"
PHASES = ("implementation", "direct", "service", "contract", "counterfact")


def _accepted() -> list[tuple[str, str, str]]:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    return sorted(
        (str(m["provider"]), str(m["upstream_model"]), str(m["task_id"]))
        for m in report["models"]
        if m["status"] == "OFFLINE_ACCEPTED"
    )


def _triad(command: str, provider: str, model: str, phase: str) -> bool:
    return all(
        token in command
        for token in (
            f"--provider {provider}",
            f"--model {model}",
            f"--phase {phase}",
            "--evidence-root docs/evidence/C101",
        )
    )


def main() -> int:
    """Check every receipt's status, exit code, command identity and task id."""
    RUNS.mkdir(parents=True, exist_ok=True)
    rows = _accepted()
    missing: list[str] = []
    not_pass: list[str] = []
    nonzero: list[tuple[str, Any]] = []
    wrong_command: list[str] = []
    wrong_task: list[tuple[str, str]] = []
    commands: dict[str, str] = {}
    checked = 0
    for provider, model, task_id in rows:
        for phase in PHASES:
            key = f"{provider}::{model}::{phase}"
            path = RECEIPTS / provider / model / f"{phase}.json"
            if not path.is_file():
                missing.append(key)
                continue
            receipt: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            checked += 1
            if receipt.get("status") != "PASS":
                not_pass.append(key)
            execution = receipt.get("machine_execution") or {}
            if execution.get("exit_code") != 0:
                nonzero.append((key, execution.get("exit_code")))
            command = str(execution.get("actual_command") or "")
            commands[key] = command
            if not _triad(command, provider, model, phase):
                wrong_command.append(key)
            if str(receipt.get("task_id")) != task_id:
                wrong_task.append((key, str(receipt.get("task_id"))))

    forged = dict(commands)
    forged_failures: list[str] = []
    if forged:
        first = sorted(forged)[0]
        provider, model, phase = first.split("::")
        forged[first] = forged[first].replace(f"--model {model}", "--model __FORGED__")
        forged_failures = [
            key for key, command in forged.items() if not _triad(command, *key.split("::"))
        ]

    lines = [
        f"accepted_rows={len(rows)} expected_receipts={len(rows) * 5} checked={checked}",
        f"missing={len(missing)} not_pass={len(not_pass)} nonzero_exit={len(nonzero)}",
        f"wrong_command={len(wrong_command)} sample={wrong_command[:3]}",
        f"wrong_task={len(wrong_task)} sample={wrong_task[:3]}",
        f"forge_probe failures={forged_failures}",
    ]
    arms = {
        "A_all_five_receipts_exist": len(rows) == 339 and checked == 1695 and not missing,
        "B_every_receipt_is_pass": not not_pass,
        "C_every_execution_exited_zero": not nonzero,
        "D_every_command_names_its_own_row_and_phase": not wrong_command,
        "E_every_task_id_matches_the_report": not wrong_task,
        "F_command_identity_can_bite": forged_failures == [sorted(forged)[0]] if forged else False,
    }
    passed = sum(1 for ok in arms.values() if ok)
    ok = passed == len(arms)
    lines.append(
        f"phase_execution_identity arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(name for name, value in arms.items() if not value)}"
    )
    text = "\n".join(lines) + "\n" + f"w131_phase_execution_identity_exit={0 if ok else 1}\n"
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"w131_phase_execution_identity_exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
