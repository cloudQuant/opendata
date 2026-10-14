#!/usr/bin/env python3
"""C104: archive the receipts this family reads, each with its own header and exit line.

Nothing here re-types a number: every carrier is the verbatim stdout of a run already on disk, and
the header fields (bytes, sha256-16, exit) are measured off that source file. Two gates re-check
the writer itself, because C103's archiver shipped a `(HEADER "-") * 70` bug that inflated 25
carriers to 30-45 KB each: ``header_repetitions`` counts ``carrier: `` per file and
``oversized_vs_source`` rejects a carrier that outgrew its own source by more than the header.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess
import sys
from datetime import datetime

REPO = pathlib.Path(__file__).resolve().parents[5]
FAMILY = REPO / "docs/evidence/C104/h15-series-enumeration"
RUNS = FAMILY / "runs"

# carrier name -> (source log under /tmp, argv, verdict-bearing exit token)
RECEIPTS = (
    ("probe-pass.txt", "c104_probe_pass.txt", "PROBE_EXIT",
     "python docs/evidence/C104/h15-series-enumeration/probes/h15_allowlist.py"),
    ("finding-triage-identity-only.txt", "c104_triage.txt", "triage_exit",
     "python - (identity-only triage of /tmp/c103_gl_shipped.json + /tmp/c103_gl_free.json)"),
    ("pytest-allowlist-consumers.txt", "c104_pytest45.txt", "PYTEST_EXIT",
     "python -m pytest -q --no-cov -p no:cacheprovider --tb=line "
     "tests/test_acceptance_item_probe.py tests/test_env_example_security.py"),
    ("staged-payload-rescan.txt", "c104_rescan.txt", "RESCAN_EXIT",
     "python docs/evidence/C104/h15-series-enumeration/probes/payload_rescan.py"),
    ("secret-check-before-edit.txt", "c104_secretcheck_before.txt", "SECRET_CHECK_EXIT",
     "make secret-check  # command: scripts/quality/secret_scan_check.py, pinned argv"),
    ("secret-check-after-edit.txt", "c104_secretcheck_after.txt", "SECRET_CHECK_EXIT",
     "make secret-check  # command: scripts/quality/secret_scan_check.py, pinned argv"),
)

repeated: list[str] = []
oversized: list[str] = []
missing: list[str] = []


def git(*args: str) -> str:
    done = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
        ["git", *args], cwd=REPO, capture_output=True, text=True, shell=False, check=False
    )
    return done.stdout.strip() if done.returncode == 0 else ""


def main() -> int:
    RUNS.mkdir(parents=True, exist_ok=True)
    commit = git("rev-parse", "HEAD")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    status = git("-c", "core.quotepath=off", "status", "--porcelain", "-uno")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    written = 0
    total_bytes = 0

    for name, src, exit_token, argv in RECEIPTS:
        source = pathlib.Path("/tmp") / src
        if not source.is_file():
            missing.append(name)
            continue
        body = source.read_text(encoding="utf-8", errors="replace")
        exits = re.findall(rf"{exit_token}\s*=\s*(-?\d+)", body)
        exit_line = (
            f"{exit_token.lower()}={exits[-1]}"
            if exits
            else f"{exit_token.lower()}=not_captured_at_run_time (verdict read from the body)"
        )
        header = (
            f"carrier: {name}\n"
            f"round: C104 h15-series-enumeration\n"
            f"captured_at_local={stamp}\n"
            f"commit={commit} branch={branch}\n"
            f"worktree_dirty_files_at_capture={len([ln for ln in status.splitlines() if ln])}\n"
            f"source_log=/tmp/{src}\n"
            f"source_bytes={source.stat().st_size}\n"
            f"source_sha256_16={hashlib.sha256(body.encode()).hexdigest()[:16]}\n"
            f"argv={argv}\n"
            f"interpreter=gitleaks 8.30.1 / python 3.13.5 (envs/py313)\n"
            f"{exit_line}\n"
        )
        text = header + "-" * 70 + "\n" + body
        (RUNS / name).write_text(text, encoding="utf-8")
        written += 1
        total_bytes += len(text.encode())
        if text.count("carrier: ") != 1:
            repeated.append(name)
        if len(text.encode()) > source.stat().st_size + 2048:
            oversized.append(name)

    print(f"face: receipts_declared={len(RECEIPTS)} sources_missing={len(missing)} "
          f"missing={missing}")
    print(f"face: carriers_written={written} bytes_written={total_bytes}")
    print(f"face: zero_byte_carriers={[n for n in RUNS.glob('*.txt') if n.stat().st_size == 0]}")
    print(f"face: header_repetitions={repeated}")
    print(f"face: oversized_vs_source={oversized}")
    if missing or repeated or oversized or written != len(RECEIPTS):
        print("verdict: REFUSED")
        return 1
    print("verdict: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
