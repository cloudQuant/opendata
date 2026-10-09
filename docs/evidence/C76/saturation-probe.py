"""Measure cold-import and sleep-latency faces under synthetic load, to set test budgets.

Prints, per arm: median and max over repeats, so a budget can be derived from the max.
"""

from __future__ import annotations

import asyncio
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
WORKER_IMPORTS = (
    "import time\n"
    "t0=time.monotonic()\n"
    "from datetime import date\n"
    "from pathlib import Path\n"
    "from sqlalchemy import create_engine\n"
    "import opendata.data.minute_archive\n"
    "import pandas\n"
    f"print(time.monotonic()-t0)\n"
)


def spinners(n: int) -> list[subprocess.Popen]:
    return [
        subprocess.Popen(  # nosec B603
            [sys.executable, "-c", "import time\nend=time.time()+90\nwhile time.time()<end:\n pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(n)
    ]


def cold_import_seconds() -> float:
    env = dict(__import__("os").environ, PYTHONPATH=str(REPO))
    out = subprocess.run(  # nosec B602 B603
        [sys.executable, "-c", WORKER_IMPORTS],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    return float(out.stdout.strip().splitlines()[-1])


async def sleep_latency(sleep_for: float, repeats: int) -> list[float]:
    latencies = []
    for _ in range(repeats):
        started = time.monotonic()
        await asyncio.sleep(sleep_for)
        latencies.append(time.monotonic() - started - sleep_for)
        await asyncio.sleep(0)
    return latencies


def report(label: str, values: list[float]) -> None:
    print(
        f"{label:34s} n={len(values):3d} median={statistics.median(values):.4f} "
        f"p95={sorted(values)[max(0, int(len(values) * 0.95) - 1)]:.4f} max={max(values):.4f}",
        flush=True,
    )


def main() -> None:
    load = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    procs = spinners(load)
    print(f"load spinners: {load} (each burns one core for 90 s)", flush=True)
    try:
        imports = [cold_import_seconds() for _ in range(4)]
        report(f"cold import of minute_archive (load={load})", imports)
        for target in (0.07, 0.3):
            for _ in range(2):
                report(f"asyncio sleep {target}s overshoot", asyncio.run(sleep_latency(target, 30)))
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
