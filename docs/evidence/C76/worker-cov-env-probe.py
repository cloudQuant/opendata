"""Does coverage tracing reach the process the lock test spawns? Measure all three levels.

Member 12's lock test spawns a bare ``python -c`` grandchild from inside a pytest process. Whether
that grandchild pays a coverage-tracing cost decides if the gate's ``--cov`` flag explains the
worker's startup time, so this harness reads the ``COV*`` environment at the three levels that
matter instead of arguing from the flag's presence on the command line:

1. the pytest controller (``-n 0``),
2. an xdist worker running the test (``-n 2``),
3. the ``python -c`` grandchild spawned from inside the test.

``site-packages/pytest-cov.pth`` calls ``pytest_cov.embed.init()`` only when ``COV_CORE_SOURCE`` is
already in ``os.environ``, so level 3's key list is the whole question. Measured answer (this file's
face): ``COV_CORE_CONFIG``/``COV_CORE_DATAFILE``/``COV_CORE_SOURCE`` are present in the controller and
in each xdist worker, and a ``python -c`` grandchild inherits all three -- so member 12's archive-lock
worker IS traced, and the claim that tracing stops at the pytest process is refuted.

Re-run: ``python3.11 docs/evidence/C76/worker-cov-env-probe.py``. ``--override-ini=addopts=`` drops
pytest.ini's ``--cov-report=term-missing --cov-report=html`` so the three runs cost seconds, while
the ``--cov=opendata`` flag under test is passed explicitly.

Caveat on the boot-cost column: ``--cov-config`` points coverage at a one-line scratch config, while
member 12 uses the repo's ``pyproject.toml`` (``branch = true``, the whole ``opendata`` source set), so
the traced-boot number here is a lower bound on what the gate's grandchildren pay.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
SCRATCH = Path("/tmp/cov3")
OUT = SCRATCH / "out"

TEST_SOURCE = '''
import json, os, subprocess, sys, time
from pathlib import Path

OUT = Path(os.environ["COV3_OUT"])
OUT.mkdir(parents=True, exist_ok=True)

def _boot_cost():
    """Parent-side spawn -> first executed line of a bare ``python -c`` child.

    The child writes its marker as its very first statement, so site initialization -- including
    pytest-cov's ``.pth`` hook, which is what a traced boot adds -- is inside the number. This is
    the same spawn shape as member 12's lock test.
    """
    costs = []
    for i in range(2):
        marker = OUT / f"boot-{os.getpid()}-{i}"
        marker.unlink(missing_ok=True)
        started = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; open(sys.argv[1], 'w').close()", str(marker)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        while not marker.exists() and proc.poll() is None and time.monotonic() - started < 120:
            time.sleep(0.005)
        costs.append(time.monotonic() - started)
        proc.wait(timeout=30)
        marker.unlink(missing_ok=True)
    return costs

def _dump():
    grand = subprocess.run(
        [sys.executable, "-c",
         "import os,json;print('GRANDCHILD'+json.dumps(sorted(k for k in os.environ if k.startswith('COV'))))"],
        capture_output=True, text=True,
    )
    hits = [line for line in grand.stdout.splitlines() if line.startswith("GRANDCHILD")]
    return {
        "arm": os.environ["COV3_ARM"],
        "role": os.environ.get("PYTEST_XDIST_WORKER", "controller"),
        "pid": os.getpid(),
        "cov_keys": sorted(k for k in os.environ if k.startswith("COV")),
        "grandchild_rc": grand.returncode,
        "grandchild_cov_keys": [json.loads(h[len("GRANDCHILD"):]) for h in hits] or ["<no marker>"],
        "grandchild_stderr_tail": grand.stderr[-200:],
        "boot_cost_s": [round(c, 3) for c in _boot_cost()],
    }

def _write():
    face = _dump()
    (OUT / f"{face['arm']}-{face['role']}-{face['pid']}.json").write_text(
        json.dumps(face, indent=2, sort_keys=True)
    )

def test_dump_serial_side():
    _write()

def test_dump_worker_side():
    _write()
'''

COMMON = ["--override-ini=addopts=", "-p", "no:randomly", "-p", "no:cacheprovider", "-q", "--no-header"]
ARMS = [
    ("serial-cov-on", "serial, coverage ON", ["--cov=opendata", "--cov-report="]),
    ("serial-cov-off", "serial, coverage OFF", []),
    ("xdist2-cov-on", "xdist -n 2, coverage ON", ["-n", "2", "--cov=opendata", "--cov-report="]),
]

#: Coverage config for the harness runs only. pyproject's ``fail_under = 84`` would otherwise turn
#: these two trivial probes into a coverage verdict, and the repo's ``.coverage``/``coverage.xml``
#: (member 12's own faces) must not be overwritten by a measurement run. The flag under test is
#: ``--cov`` being active at all, which this keeps.
COV_CFG = SCRATCH / "cov.cfg"


def run(arm: str, extra: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603
        [
            sys.executable, "-m", "pytest",
            str(SCRATCH / "test_cov3_probe.py"),
            *extra,
            *(["--cov-config", str(COV_CFG)] if "--cov=opendata" in extra else []),
            *COMMON,
        ],
        cwd=REPO,
        env={**os.environ, "COV3_OUT": str(OUT), "COV3_ARM": arm, "COVERAGE_FILE": str(SCRATCH / ".coverage")},
        capture_output=True,
        text=True,
        timeout=600,
    )


def main() -> None:
    shutil.rmtree(SCRATCH, ignore_errors=True)
    OUT.mkdir(parents=True)
    COV_CFG.write_text("[run]\nsource = opendata\n[report]\nfail_under = 0\n", encoding="utf-8")
    (SCRATCH / "test_cov3_probe.py").write_text(TEST_SOURCE, encoding="utf-8")

    print(f"python: {sys.version.split()[0]} {sys.executable}")
    print(f"cwd (rootdir): {REPO}")
    print(f"shared flags: {' '.join(COMMON)}")
    print(f"coverage data/config redirected to {SCRATCH} (COVERAGE_FILE + --cov-config), repo untouched")

    for arm, label, extra in ARMS:
        proc = run(arm, extra)
        print(f"\n=== {label} ===")
        print(f"  argv: -m pytest <probe> {' '.join(extra)}")
        print(f"  rc={proc.returncode}")
        for line in [l for l in proc.stdout.splitlines() if " passed" in l or " failed" in l or "ERROR" in l][:3]:
            print(f"  {line.strip()}")
        if proc.returncode != 0:
            print("  stdout tail:\n" + "\n".join("    " + l for l in proc.stdout.splitlines()[-25:]))
            print("  stderr tail:\n" + "\n".join("    " + l for l in proc.stderr.splitlines()[-25:]))

    print("\n=== COV* env faces ===")
    for path in sorted(OUT.glob("*.json")):
        face = json.loads(path.read_text(encoding="utf-8"))
        print(
            f"  arm={face['arm']:15s} role={face['role']:10s} parent COV*={face['cov_keys']} "
            f"-> grandchild rc={face['grandchild_rc']} COV*={face['grandchild_cov_keys']}"
        )
        print(f"    traced-boot cost (parent-side spawn -> child first line, 2 children): {face['boot_cost_s']} s")
        if face["grandchild_stderr_tail"].strip():
            print(f"    grandchild stderr: {face['grandchild_stderr_tail'].strip()}")

    print(f"\n.coverage* data files in repo root after the runs: {sorted(p.name for p in REPO.glob('.coverage*'))}")


if __name__ == "__main__":
    main()
