"""Measure the snapshot-lock test's worker time-to-marker under member-12-like contention.

Member 12 runs ``pytest tests -n 8 -m "not e2e" --cov-branch``, and pytest.ini's addopts already
carry ``--cov=opendata``, so every one of those 8 workers is a coverage-traced pytest process.
This harness rebuilds that process mix -- 7 peer coverage-traced pytest processes plus the lock
test as the 8th -- and then reads the stage files the test leaves in --basetemp, so the number
comes from the test's own markers rather than from a synthetic spinner load.

Re-run: ``python3.11 docs/evidence/C76/worker-startup-contention-probe.py 3`` (argument = repeats;
each repeat costs up to 900 s). Face: ``proxy-run-face.txt`` (+ ``proxy-run-face.err``).
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
PEERS = [
    "tests/test_fuyao_endpoints.py",
    "tests/test_ths_provider.py",
    "tests/test_pipeline_jobs.py",
    "tests/test_provider_model_contracts.py",
    "tests/test_data_query_http_warehouse.py",
    "tests/test_provider_model_store.py",
    "tests/test_opendata_client.py",
]
TARGET = (
    "tests/test_minute_archive.py::"
    "test_global_snapshot_lock_blocks_writer_and_expired_shard_purge_processes"
)
LOCK_TEST = (
    "tests/test_minute_archive.py::test_a_stalled_alive_worker_is_killed_and_reported_by_stage"
)


def stages(basetemp: Path) -> list[str]:
    """Read the worker's own stage markers out of this run's --basetemp tree."""
    return [
        f"{path.name}={path.read_text(encoding='utf-8')}"
        for path in sorted(basetemp.rglob("*-stage"))
    ]


def boot_probe() -> float:
    """Parent-side spawn-to-first-line for an empty ``python -c``, i.e. interpreter boot alone."""
    marker = Path("/tmp/boot-probe-marker")  # nosec B108  # noqa: S108
    marker.unlink(missing_ok=True)
    started = time.monotonic()
    proc = subprocess.Popen(  # nosec B603  # noqa: S603
        [sys.executable, "-c", "import sys; open(sys.argv[1], 'w').close()", str(marker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    while not marker.exists() and proc.poll() is None and time.monotonic() - started < 240:
        time.sleep(0.005)
    elapsed = time.monotonic() - started
    proc.wait(timeout=30)
    return elapsed


def drain(peers: list[subprocess.Popen]) -> None:
    """Terminate the peer pytest processes, killing any that a 10 s wait does not collect."""
    for peer in peers:
        peer.terminate()
    for peer in peers:
        if peer.poll() is not None:
            continue
        try:
            peer.wait(timeout=10)
        except subprocess.TimeoutExpired:
            peer.kill()


def main() -> None:
    """Run the lock test once per repeat against 7 traced peers and print its own markers."""
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    for run in range(repeats):
        basetemp = Path(f"/tmp/wt-proxy-{run}")  # nosec B108  # noqa: S108
        shutil.rmtree(basetemp, ignore_errors=True)
        peers = [
            subprocess.Popen(  # nosec B603  # noqa: S603
                [sys.executable, "-m", "pytest", peer, "-q", "-p", "no:randomly"],
                cwd=REPO,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for peer in PEERS
        ]
        measured = subprocess.run(  # nosec B603  # noqa: S603
            [
                sys.executable,
                "-m",
                "pytest",
                TARGET,
                LOCK_TEST,
                "-q",
                "-p",
                "no:randomly",
                "--durations=0",
                f"--basetemp={basetemp}",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=900,
        )
        boots = [boot_probe(), boot_probe()]
        tail = [
            line for line in measured.stdout.splitlines() if " passed" in line or " failed" in line
        ]
        durations = [
            line.strip()
            for line in measured.stdout.splitlines()
            if "seconds" in line and ("call" in line or "setup" in line)
        ]
        drain(peers)
        print(
            f"=== run {run}: {' '.join(tail) or measured.returncode} "
            f"| boot probe: {' '.join(f'{b:.2f}s' for b in boots)} ===",
            flush=True,
        )
        for line in durations:
            print(f"  duration: {line}", flush=True)
        for entry in stages(basetemp):
            print(f"  {entry}", flush=True)


if __name__ == "__main__":
    main()
