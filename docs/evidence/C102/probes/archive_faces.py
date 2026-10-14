"""Archive the C102 ths-grant faces, each carrier with its own captured exit reading.

Fresh arms are re-run here (the tree is idle); historical runs are copied out of ``/tmp`` and
labelled with what they were, including the runs that carried my own wrong numbers.
"""

from __future__ import annotations

import datetime
import subprocess  # nosec B404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RUNS = ROOT / "docs/evidence/C102/ths-grants/runs"
PROBES = ROOT / "docs/evidence/C102/probes"
PY = sys.executable


def run(name: str, argv: list[str], tail_lines: int = 10_000) -> int:
    """Run one argv as a carrier, writing its tail under the family's ``runs/``."""
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)  # nosec B603  # noqa: S603
    body = proc.stdout + (("\n--- stderr ---\n" + proc.stderr) if proc.stderr else "")
    kept = body.splitlines()[-tail_lines:]
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    stem = name.split(".", 1)[0]
    header = [
        f"carrier: {name}",
        f"captured_at_local={stamp}",
        f"argv={' '.join(argv)}",
        f"interpreter={PY}",
        f"{stem}_exit={proc.returncode}",
        "-" * 70,
    ]
    (RUNS / name).write_text("\n".join(header) + "\n" + "\n".join(kept) + "\n", encoding="utf-8")
    print(f"archived {name} {stem}_exit={proc.returncode} lines={len(kept)}")
    return proc.returncode


def copy_historical(name: str, source: Path, note: str, tail_lines: int = 10_000) -> int:
    """Copy an existing receipt into ``runs/`` with the same carrier headers."""
    if not source.exists():
        print(f"MISSING historical carrier {source}")
        return 1
    lines = source.read_text(encoding="utf-8").splitlines()
    kept = lines[-tail_lines:]
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    stem = name.split(".", 1)[0]
    header = [
        f"carrier: {name}",
        f"copied_at_local={stamp}",
        f"source_log={source}",
        f"derivation={note}",
        # The original invocations were interactive; their wrapper rc was never captured, so the
        # verdict line inside the body is the reading and this field states how it was obtained.
        f"{stem}_exit=not_captured_at_run_time (verdict read from the body)",
        "-" * 70,
    ]
    (RUNS / name).write_text("\n".join(header) + "\n" + "\n".join(kept) + "\n", encoding="utf-8")
    print(f"copied {name} lines={len(kept)}")
    return 0


def main() -> int:
    """Archive this round's faces: the pytest re-take and the historical carrier copies."""
    exits: dict[str, int] = {}
    exits["pytest_after"] = run(
        "pytest-after-ths-file.txt",
        [
            PY,
            "-m",
            "pytest",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            "tests/test_ths_provider.py",
        ],
    )
    exits["repair_dry_post"] = run(
        "repair-dry-post-apply.txt",
        [PY, str(PROBES / "ths_grant_repair.py")],
    )
    exits["idempotence"] = run("idempotence-arm.txt", [PY, str(PROBES / "ths_idempotence_arm.py")])
    exits["arms"] = run("arms-final.txt", [PY, str(PROBES / "ths_grant_arms.py")])
    exits["ruff"] = run("ruff-final.txt", ["ruff", "check", "tests/test_ths_provider.py"])
    exits["format_audit"] = run(
        "collateral-audit-armA-armC.txt", [PY, str(PROBES / "ths_format_audit.py")]
    )
    exits["hunk_audit"] = run("collateral-audit-armB.txt", [PY, str(PROBES / "ths_hunk_audit.py")])

    tmp = Path("/tmp")  # nosec B108  # noqa: S108
    exits["hist_before"] = copy_historical(
        "pytest-before-ths-file.txt",
        tmp / "ths_after1.txt",
        "pre-repair direct run of the whole file; the 37 failing node ids are in the body",
    )
    exits["hist_dry37"] = copy_historical(
        "repair-dry-final.txt",
        tmp / "ths_dry4.txt",
        "the dry face the apply run acted on: failing_nodes=37 insertions=37 unresolved=0",
    )
    exits["hist_dry0"] = copy_historical(
        "repair-dry-fabricated-zero.txt",
        tmp / "ths_dry.txt",
        "my first rewriter run: it split Class::method ids on '.' and printed insertions=0",
    )
    exits["hist_dry9"] = copy_historical(
        "repair-dry-borrowed-identity.txt",
        tmp / "ths_dry2.txt",
        "second run: models_used=9 because the parametrized receiver borrowed a sibling's model",
    )
    exits["hist_refused"] = copy_historical(
        "repair-apply-refused-unparsable.txt",
        tmp / "ths_apply.txt",
        "third run: byte-vs-char col_offset produced unparsable text; the guard refused to write",
    )
    exits["hist_apply"] = copy_historical(
        "repair-apply-final.txt",
        tmp / "ths_apply2.txt",
        "the run that wrote the file: added=1447 bytes over the 72625-byte pre-image",
    )
    exits["hist_arms56"] = copy_historical(
        "arms-first-5-of-6.txt",
        tmp / "ths_arms1.txt",
        "first arm pass: attempt-bound failed because my arm omitted the required asset_type field",
    )

    print("face: " + " ".join(f"{k}={v}" for k, v in exits.items()))
    failed = [k for k, v in exits.items() if v != 0]
    print("verdict: " + ("ALL_CARRIERS_OK" if not failed else f"CARRIER_PROBLEMS={failed}"))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
