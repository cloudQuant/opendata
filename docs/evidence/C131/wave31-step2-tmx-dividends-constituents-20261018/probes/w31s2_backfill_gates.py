r"""Static gates plus a pin-drift re-take for the wave-31 step-2 back-fill commit.

The back-fill adds prose and one instrument after the plane-1 chain had already run. Two questions
have to be answered about that, and neither is answered by this file's prose:

* ``pin_drift`` -- did the back-fill stale any digest the judge reads? The reading is produced by
  the chain driver's own ``pin_drift_face()`` (imported, not restated), so the pin-walking predicate
  stays the one that priced the pre-production table. It is written to its own carrier instead of
  over ``wave31-step2-pin_drift_pre_production_rerun.txt``, because §7.5 of the face cites that file
  and re-aging a cited artefact is exactly the thing this wave was told not to do.
* ``staged scan`` -- the shipped index-plane screen over whatever this commit is about to add, taken
  in the same turn it is taken, since the answer changes the moment a carrier is appended.

Run: ``PYTHONPATH=. python <this file>``
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import shutil
import subprocess  # nosec B404  # two runs: shipped commands, list argv, shell never enabled
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[5]
PY = sys.executable
PROBES = Path(__file__).resolve().parent
RUNS = PROBES.parent / "runs"
SCREEN = REPO / "docs/evidence/C128/plane1-residue-readiness-20261017/probes/staged_payload_scan.py"
DRIVER = PROBES / "w31s2_run_chain.py"
NEW_PROBE = PROBES / "committed_payload_scan.py"


def _load(name: str, path: Path) -> Any:  # noqa: ANN401
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"module_unimportable={path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def cmd(name: str, argv: list[str]) -> tuple[str, int]:
    """Run one shipped command and return its verbatim face plus its exit."""
    proc = subprocess.run(  # noqa: S603  # nosec B603  # fixed argv, shell disabled
        [PY, *argv],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    body = (proc.stdout + proc.stderr).strip()
    return f"{name}: {body}\n{name}_exit={proc.returncode}", proc.returncode


def staged_paths() -> list[str]:
    """What the back-fill commit is about to add, read live from the index."""
    git = shutil.which("git") or "git"
    listed = subprocess.run(  # noqa: S603  # nosec B603  # resolved absolute path, fixed argv
        [git, "-c", "core.quotepath=off", "diff", "--cached", "--name-only"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        raise SystemExit(f"git_diff_cached_exit={listed.returncode} {listed.stderr.strip()[:120]}")
    return [line for line in listed.stdout.splitlines() if line]


def main() -> int:
    """Write the back-fill carrier and print every face it contains."""
    driver = _load("w31s2_run_chain", DRIVER)
    drift = driver.pin_drift_face()
    drift_lines = str(drift["stdout"]).splitlines()
    faces: list[str] = [
        "wave-31 step 2 -- back-fill gates (static + pin drift + index-plane screen over the "
        "paths this commit is about to add)",
        f"argv: {PY} {PROBES.relative_to(REPO) / Path(__file__).name}   (cwd = repo root)",
        "pin_drift (taken by the chain driver's own pin_drift_face, written beside the cited "
        "rerun carrier instead of over it):",
        f"argv: {PY} {DRIVER.relative_to(REPO)} --stage pin-drift",
        f"pin_drift_exit={drift['exit']}",
    ]
    faces.extend(f"  {line}" for line in drift_lines)
    for name, argv in [
        ("ruff_check_new_probe", ["-m", "ruff", "check", str(NEW_PROBE), str(PROBES)]),
        ("ruff_format_new_probe", ["-m", "ruff", "format", "--check", str(PROBES)]),
        ("mypy_backfill_probes", ["-m", "mypy", str(NEW_PROBE), str(Path(__file__))]),
        (
            "bandit_backfill_probes",
            ["-m", "bandit", "-q", "-c", "bandit.yaml", str(NEW_PROBE), str(Path(__file__))],
        ),
    ]:
        text, _code = cmd(name, argv)
        faces.append(text)
        if name == "bandit_backfill_probes":
            faces.append(
                "scope note: the factory's bandit plane is PY_BANDIT = opendata scripts alembic "
                "alembic_data, so docs/evidence sits outside it by design and this leg is "
                "self-imposed. B404 is exempted the way 34 shipped modules do it -- on the line "
                "that earns it, with a reason -- because bandit.yaml carries no global skips."
            )
    screen = _load("staged_payload_scan_screen", SCREEN)
    paths = staged_paths()
    face, child_code = cmd("staged_scan_child_form", [str(SCREEN), "--paths", *paths])
    faces.append(f"staged_paths={len(paths)}")
    faces.append(face)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        in_process = screen.main(["--paths", *paths])
    joined = " | ".join(buf.getvalue().splitlines())
    faces.append(f"staged_scan_in_process_exit={in_process} :: {joined}")
    faces.append(
        f"two_forms_agree={child_code == in_process} (child argv exit vs in-process main() return)"
    )
    ok = child_code == 0 and in_process == 0 and drift["exit"] == 0
    faces.append(f"backfill_chain_exit={0 if ok else 1}")
    carrier = RUNS / "wave31-step2-backfill_gates.txt"
    carrier.write_text("\n".join(faces) + "\n", encoding="utf-8")
    print("\n".join(faces), flush=True)
    print(f"carrier_written bytes={carrier.stat().st_size} path={carrier.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
