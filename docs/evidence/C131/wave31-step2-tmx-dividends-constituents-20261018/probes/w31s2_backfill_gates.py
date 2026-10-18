r"""Every gate a back-fill commit owes, taken over the set it is actually about to add.

The back-fill adds prose and instruments after the plane-1 chain had already run. Four questions
have to be answered about that, and none of them is answered by this file's prose:

* ``pin_drift`` -- did the back-fill stale any digest the judge reads? The reading is produced by
  the chain driver's own ``pin_drift_face()`` (imported, not restated), so the pin-walking predicate
  stays the one that priced the pre-production table. It is written to its own carrier instead of
  over ``wave31-step2-pin_drift_pre_production_rerun.txt``, because section 7.5 of the face cites
  that file and re-aging a cited artefact is exactly the thing this wave was told not to do.
* ``static legs`` -- ruff check, ruff format, mypy and bandit over **every** probe in ``probes/``,
  the list globbed rather than kept by hand. The first version of this driver named two files, and
  the widening found four mypy errors and two bandit findings in files it had never looked at,
  which is what a hand-kept gate list means in practice. Each leg's exit is folded into the
  verdict; a leg whose exit is printed but discarded cannot open this gate.
* ``probe inertness`` -- this step edited ``tmx_two_rows_prevalidation.py``. "Annotations only,
  behaviour unchanged" is a claim about what that probe prints, so it is re-run and compared line
  for line against the face already in the record, with a control that drops one line and must
  therefore read different.
* ``staged scan`` -- the shipped index-plane screen over whatever this commit is about to add, taken
  in the same turn it is taken, since the answer changes the moment a carrier is appended.

Run: ``PYTHONPATH=. python <this file> --out wave31-step2-backfill_gates_take2.txt``; ``--out``
exists because a re-take must not overwrite the first take's carrier, which the face already cites.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import os
import re
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
DEFAULT_OUT = "wave31-step2-backfill_gates.txt"
PREVAL_PROBE = PROBES / "tmx_two_rows_prevalidation.py"
PREVAL_CARRIER = RUNS / "wave31-step2-two_rows_prevalidation.txt"
PREVAL_EXIT_LINE = "two_rows_prevalidation_exit="
SCRATCH_ROOT = re.compile(r"w31s2-tmx-preval-[a-z0-9]+")
# Derived, not typed: a probe added to this wave after the first take would otherwise never reach
# the static legs, and a gate whose file list is a hand-kept pair silently mutes its own subject.
PROBE_FILES = sorted(PROBES.glob("*.py"))


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


def _body(text: str, source: str) -> list[str]:
    """The measured part of a prevalidation face: the first ``materialized:`` line down.

    Above that line sit the header, the argv and the stderr byte count, which the chain driver
    writes and this step edits, so comparing them would compare the record-keeping, not the probe.
    The scratch root's random suffix is normalized because it differs per run by design.
    """
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("materialized:")), None)
    if start is None:
        raise SystemExit(f"no_materialized_line_in={source}")
    return [SCRATCH_ROOT.sub("SCRATCH", ln) for ln in lines[start:]]


def preval_inertness() -> tuple[str, int]:
    """Re-run the probe this step edited and diff it against the face already in the record.

    The edits were an annotation widened to ``type[Any]``, two now-redundant ``type: ignore``
    suppressions and one dead ``nosec``. "Behaviourally inert" is a claim about what the probe
    prints, so it is measured by printing it again, not asserted in prose. Two things make the
    reading worth having: the driver-appended exit line is excluded **by name and printed**, and
    the control drops one line from one side -- an identical-vs-different comparison that cannot
    report different is not measuring anything.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    proc = subprocess.run(  # noqa: S603  # nosec B603  # the wave's own probe, list argv, no shell
        [PY, str(PREVAL_PROBE)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if not PREVAL_CARRIER.is_file():
        raise SystemExit(f"carrier_absent={PREVAL_CARRIER}")
    new_body = _body(proc.stdout, "re-taken stdout")
    old_all = _body(PREVAL_CARRIER.read_text(encoding="utf-8"), str(PREVAL_CARRIER))
    excluded = [ln for ln in old_all if ln.startswith(PREVAL_EXIT_LINE)]
    old_body = [ln for ln in old_all if not ln.startswith(PREVAL_EXIT_LINE)]
    identical = new_body == old_body
    first_diff = next(
        (
            f"line {i}: old={o!r} new={n!r}"
            for i, (o, n) in enumerate(zip(old_body, new_body, strict=False), 1)
            if o != n
        ),
        "none",
    )
    control = new_body != old_body[:-1]
    code = 0 if identical and control and proc.returncode == 0 else 1
    lines = [
        "preval_inertness: the probe edited by this step, run again and compared line for line",
        f"argv: {PY} {PREVAL_PROBE.relative_to(REPO)}   (cwd = repo root, PYTHONPATH=.)",
        f"probe_exit={proc.returncode} stderr_bytes={len(proc.stderr.encode())}",
        f"old_body_lines={len(old_body)} new_body_lines={len(new_body)} "
        f"lines_below={len(excluded)} excluded=[{', '.join(excluded)}]",
        f"identical={identical} first_difference={first_diff}",
        f"CONTROL_drop_one_line_from_old_side_differs={control}",
        f"preval_inertness_exit={code}",
    ]
    return "\n".join(lines), code


def main(argv: list[str] | None = None) -> int:
    """Write the back-fill carrier and print every face it contains."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help="carrier file name inside runs/ (a re-take must not age the take it corrects)",
    )
    args = parser.parse_args(argv)

    driver = _load("w31s2_run_chain", DRIVER)
    drift = driver.pin_drift_face()
    drift_lines = str(drift["stdout"]).splitlines()
    faces: list[str] = [
        "wave-31 step 2 -- back-fill gates (static + pin drift + index-plane screen over the "
        "paths this commit is about to add)",
        f"argv: {PY} {PROBES.relative_to(REPO) / Path(__file__).name} --out {args.out}"
        "   (cwd = repo root)",
        f"probe_files_gated={len(PROBE_FILES)} "
        f"[{', '.join(p.name for p in PROBE_FILES)}] (globbed from probes/, not listed by hand)",
        "pin_drift (taken by the chain driver's own pin_drift_face, written beside the cited "
        "rerun carrier instead of over it):",
        f"argv: {PY} {DRIVER.relative_to(REPO)} --stage pin-drift",
        f"pin_drift_exit={drift['exit']}",
    ]
    faces.extend(f"  {line}" for line in drift_lines)
    static = [str(p) for p in PROBE_FILES]
    static_codes: dict[str, int] = {}
    for name, cmd_argv in [
        ("ruff_check_probes", ["-m", "ruff", "check", *static]),
        ("ruff_format_probes", ["-m", "ruff", "format", "--check", *static]),
        ("mypy_probes", ["-m", "mypy", *static]),
        ("bandit_probes", ["-m", "bandit", "-q", "-c", "bandit.yaml", *static]),
    ]:
        text, code = cmd(name, cmd_argv)
        static_codes[name] = code
        faces.append(text)
        if name == "bandit_probes":
            faces.append(
                "scope note: the factory's bandit plane is PY_BANDIT = opendata scripts alembic "
                "alembic_data, so docs/evidence sits outside it by design and this leg is "
                "self-imposed. B404 is exempted the way 34 shipped modules do it -- on the line "
                "that earns it, with a reason -- because bandit.yaml carries no global skips."
            )
    inert_text, inert_code = preval_inertness()
    faces.append(inert_text)
    screen = _load("staged_payload_scan_screen", SCREEN)
    paths = staged_paths()
    faces.append(f"staged_paths={len(paths)}")
    if not paths:
        child_code = in_process = 1
        faces.append("staged_scan=NOT_RUN reason=index empty (nothing staged to scan)")
        # Not printed when the screen never ran: assigning both codes by hand would make the
        # agreement line a comparison of two constants, i.e. a control that cannot differ.
        faces.append("two_forms_agree=NOT_MEASURED screen did not run")
    else:
        face, child_code = cmd("staged_scan_child_form", [str(SCREEN), "--paths", *paths])
        faces.append(face)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            in_process = screen.main(["--paths", *paths])
        joined = " | ".join(buf.getvalue().splitlines())
        faces.append(f"staged_scan_in_process_exit={in_process} :: {joined}")
        faces.append(
            f"two_forms_agree={child_code == in_process} "
            "(child argv exit vs in-process main() return)"
        )
    static_failed = [n for n, c in static_codes.items() if c != 0]
    ok = (
        child_code == 0
        and in_process == 0
        and drift["exit"] == 0
        and inert_code == 0
        and not static_failed
    )
    faces.append(
        f"static_legs_failed={len(static_failed)} "
        f"[{', '.join(static_failed) if static_failed else 'none'}] "
        "(folded into the verdict: a leg whose exit is discarded cannot open this gate)"
    )
    faces.append(f"backfill_chain_exit={0 if ok else 1}")
    carrier = RUNS / args.out
    carrier.write_text("\n".join(faces) + "\n", encoding="utf-8")
    print("\n".join(faces), flush=True)
    shown = carrier.relative_to(REPO) if carrier.is_relative_to(REPO) else carrier
    print(f"carrier_written bytes={carrier.stat().st_size} path={shown}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
