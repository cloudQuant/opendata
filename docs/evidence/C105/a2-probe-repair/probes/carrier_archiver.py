#!/usr/bin/env python3
"""C105: archive the receipts this round reads, each with its own header and exit line.

Nothing here re-types a number: every carrier is the verbatim stdout of a run already on disk, and
the header fields (bytes, sha256-16, exit) are measured off that source file. The writer carries the
two gates C104 shipped (one ``carrier:`` line per file, no carrier that outgrew its own source by
more than the header) plus a third, because this round's judgment is about *unjudged* readings:
every carrier must hold at least one ``<name>_exit=<digit>`` line, since the evidence-traceability
gate greps for exactly that and a carrier whose exit lives only in prose is charged as a new
violation.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess  # nosec B404  # the header quotes git, and git is a literal argv
import sys
from datetime import datetime

REPO = pathlib.Path(__file__).resolve().parents[5]
FAMILY = REPO / "docs/evidence/C105/a2-probe-repair"
RUNS = FAMILY / "runs"

# carrier name -> (source log under /tmp, argv, verdict-bearing exit token)
RECEIPTS = (
    (
        "a2-before-33-probes.txt",
        "c105_before_a2.txt",
        "BEFORE_A2_EXIT",
        "python scripts/quality/a2_check.py --files <the 33 probe targets>  # the before picture",
    ),
    (
        "ruff-check-34.txt",
        "c105_ruff34.txt",
        "RUFF_CHECK_EXIT",
        "python -m ruff check --output-format=concise <the 34 probe targets in targets.txt>",
    ),
    (
        "ruff-format-34.txt",
        "c105_rufffmt34.txt",
        "RUFF_FORMAT_EXIT",
        "python -m ruff format --check <the 34 probe targets in targets.txt>",
    ),
    (
        "mypy-bandit-34.txt",
        "c105_planes34.txt",
        "PLANES_EXIT",
        "python /tmp/c105_planes.py  # mypy per target with the gate argv, bandit as one batch",
    ),
    (
        "witness-34.txt",
        "c105_witness34_run.txt",
        "WITNESS_EXIT",
        "python docs/evidence/C105/a2-probe-repair/probes/ast_witness.py <the 34 targets>",
    ),
    (
        "witness-control.txt",
        "c105_witness_control_run.txt",
        "CONTROL_EXIT",
        "python docs/evidence/C105/a2-probe-repair/probes/ast_witness.py --control",
    ),
    (
        "docs-reidentity.txt",
        "c105_reidentity_run.txt",
        "REIDENTITY_EXIT",
        "python docs/evidence/C105/a2-probe-repair/probes/docs_reidentity.py",
    ),
    (
        "rerun-and-refusals.txt",
        "c105_rerun_refusal.txt",
        "CAPTURE_EXIT",
        "python /tmp/c105_capture3.py  # bandit/mypy attribution, six probe re-runs, "
        "C104 carrier diff vs index then restore, three repair scripts re-run",
    ),
    (
        "c105-probes-own-a2.txt",
        "c105_own_planes.txt",
        "OWN_PLANES_EXIT",
        "python /tmp/c105_own_planes.py  # the four planes over this round's own probes",
    ),
    (
        "face-writer.txt",
        "c105_face_writer.txt",
        "FACE_WRITER_EXIT",
        "python docs/evidence/C105/a2-probe-repair/probes/face_writer.py",
    ),
    (
        "archiver-rerun.txt",
        "c105_archiver_run.txt",
        "ARCHIVER_EXIT",
        "python docs/evidence/C105/a2-probe-repair/probes/carrier_archiver.py  # the previous run",
    ),
)

repeated: list[str] = []
oversized: list[str] = []
no_exit_line: list[str] = []
missing: list[str] = []


def git(*args: str) -> str:
    """Run git with literal arguments in the repository and return its stdout."""
    done = subprocess.run(  # noqa: S603  # nosec B603 B607  # literal argv, shell disabled
        ["git", *args],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    return done.stdout.strip() if done.returncode == 0 else ""


def main() -> int:
    """Archive this round's probe receipts under ``runs/`` and print each carrier digest."""
    RUNS.mkdir(parents=True, exist_ok=True)
    commit = git("rev-parse", "HEAD")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    status = git("-c", "core.quotepath=off", "status", "--porcelain", "-uno")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dirty = len([line for line in status.splitlines() if line])
    written = 0
    total_bytes = 0

    for name, src, exit_token, argv in RECEIPTS:
        source = pathlib.Path("/tmp") / src  # nosec B108  # noqa: S108  # the run logs live here
        if not source.is_file():
            missing.append(name)
            continue
        body = source.read_text(encoding="utf-8", errors="replace")
        exits = re.findall(rf"{exit_token}\s*=\s*(-?\d+)", body)
        exit_line = (
            f"{exit_token.lower()}={exits[-1]}"
            if exits
            else f"{exit_token.lower()}_exit=not_captured_at_run_time (verdict read from the body)"
        )
        header = (
            f"carrier: {name}\n"
            f"round: C105 a2-probe-repair\n"
            f"captured_at_local={stamp}\n"
            f"commit={commit} branch={branch}\n"
            f"worktree_dirty_files_at_capture={dirty}\n"
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
        if not re.search(r"\b\w*_(exit|rc)\s*=\s*-?\d+", text):
            no_exit_line.append(name)

    for path in sorted(RUNS.glob("*.txt")):
        print(
            f"file: {path.relative_to(REPO)} bytes={path.stat().st_size}"
            f" sha256_16={hashlib.sha256(path.read_bytes()).hexdigest()[:16]}"
        )
    print(
        f"face: receipts_declared={len(RECEIPTS)} sources_missing={len(missing)} missing={missing}"
    )
    print(f"face: carriers_written={written} bytes_written={total_bytes}")
    print(f"face: carriers_on_disk={len(list(RUNS.glob('*.txt')))}")
    print(f"face: header_repetitions={repeated}")
    print(f"face: oversized_vs_source={oversized}")
    print(f"face: carriers_without_an_exit_line={no_exit_line}")
    if missing or repeated or oversized or no_exit_line or written != len(RECEIPTS):
        print("verdict: REFUSED")
        return 1
    print("verdict: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
