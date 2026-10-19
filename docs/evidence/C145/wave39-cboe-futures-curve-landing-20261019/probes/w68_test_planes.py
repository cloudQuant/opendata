"""Run the wave-39 test plane twice, once in the edited tree and once in a pre-wave control.

Why two runs and not one count: wave 37 published "28 failures before and after" as evidence that
its
reds were pre-existing. The two sets were not the same 28 -- seven were repaired by the wave and
seven
others broke -- and a count comparison cannot see that. So this runner produces two junit files and
``w69_test_plane_attribution.py`` compares sorted node ids.

The control is a detached worktree of ``HEAD``, which for this wave is wave 38's docs commit: the
tree
exactly before any wave-39 edit. It is only a valid control if it carries the inputs the tests read
but
git does not track -- ``docs/evidence/C100/**`` is gitignored by ``.gitignore:249`` -- so those
bytes are
copied in first. Skipping that step is what made wave 37 call four fence reds "pre-existing": in the
bare control worktree those tests were red for a *different* reason (missing input), and the
equality of
the two counts was an accident of two different defects.

Neither run may touch the main tree's caches, so ``PYTHONDONTWRITEBYTECODE=1`` and
``-p no:cacheprovider`` are always passed, and ``-o addopts=`` drops the coverage plugin that
otherwise
writes ``.coverage`` and poisons later members of the same session. No live-e2e token is set, so
those
legs skip.

``--only final`` adds a third main-tree take for the case where the tree changed while a plane was
running. It writes under a distinct carrier name so the superseded take stays beside the replacing
one instead of being overwritten.
"""

from __future__ import annotations

import argparse
import datetime
import shutil
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
PY = "/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python"
GIT = "/usr/bin/git"
BUNDLE = "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019"
RUNS = REPO / BUNDLE / "runs"
CONTROL_DIR = Path("/tmp/w39_control_worktree")  # nosec B108  # noqa: S108
C100_REL = "docs/evidence/C100"
IGNORED_INPUTS = (C100_REL,)


def run(
    name: str, argv: list[str], cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Execute one plane and keep its carrier, whatever the verdict."""
    started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    proc = subprocess.run(  # nosec B603  # noqa: S603
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    carrier = RUNS / f"{name}.txt"
    lines = [
        f"# carrier for {name}",
        f"# cwd: {cwd}",
        f"# argv: {' '.join(argv)}",
        f"# started_utc: {started}",
        f"# exit: {proc.returncode}",
        f"# stdout_chars={len(proc.stdout)} stderr_chars={len(proc.stderr)}",
        "",
        proc.stdout[-6000:],
        "",
        "---- stderr tail ----",
        proc.stderr[-2000:],
        "",
        f"{name}_exit={proc.returncode}",
        "",
    ]
    carrier.write_text("\n".join(lines), encoding="utf-8")
    return proc


def git(*args: str) -> subprocess.CompletedProcess[str]:
    """One literal git argv in the main tree."""
    return subprocess.run(  # nosec B603  # noqa: S603
        [GIT, *args], cwd=REPO, capture_output=True, text=True, check=False
    )


def prepare_control(rev: str) -> dict[str, Any]:
    """Create the detached control worktree at ``rev`` and copy the gitignored inputs into it."""
    if CONTROL_DIR.exists():
        # Remove through git so .git/worktrees does not keep a dangling entry for a directory that
        # no longer exists; the plain rmtree is only the fallback for a half-created worktree.
        removed = git("worktree", "remove", "--force", str(CONTROL_DIR))
        if removed.returncode != 0:
            shutil.rmtree(CONTROL_DIR)
        git("worktree", "prune")
    resolved = git("rev-parse", rev).stdout.strip()
    added = git("worktree", "add", "--detach", str(CONTROL_DIR), rev)
    copied: dict[str, int] = {}
    for rel in IGNORED_INPUTS:
        source = REPO / rel
        if not source.is_dir():
            continue
        destination = CONTROL_DIR / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, dirs_exist_ok=True)
        copied[rel] = sum(1 for p in destination.rglob("*") if p.is_file())
    return {
        "requested_rev": rev,
        "resolved_rev": resolved[:9],
        "worktree_add_exit": added.returncode,
        "control_head": _control_head(),
        "ignored_input_files_copied": copied,
    }


def _control_head() -> str:
    """HEAD as the control worktree sees it."""
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [GIT, "rev-parse", "HEAD"],
        cwd=CONTROL_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip()[:9]


def main() -> int:
    """Run the worktree plane, then the control plane, and print both exit readings."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-rev", default="HEAD")
    parser.add_argument(
        "--only",
        default="both",
        choices=("both", "worktree", "control", "final"),
    )
    parser.add_argument("--final-name", default="test_plane_w39_worktree_final")
    args = parser.parse_args()
    RUNS.mkdir(parents=True, exist_ok=True)
    base = [
        PY,
        "-m",
        "pytest",
        "tests",
        "-q",
        "--continue-on-collection-errors",
        "-o",
        "addopts=",
        "-p",
        "no:cacheprovider",
    ]
    env = {
        "PYTHONPATH": str(REPO),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(Path.home()),
    }
    readings: dict[str, Any] = {}
    if args.only in ("both", "worktree"):
        xml = RUNS / "test_plane_w39_worktree.xml"
        proc = run("test_plane_w39_worktree", base + [f"--junitxml={xml}"], REPO, dict(env))
        readings["worktree"] = {"exit": proc.returncode, "xml": xml.name}
        repeat = RUNS / "test_plane_w39_worktree.take2.xml"
        proc2 = run(
            "test_plane_w39_worktree_take2", base + [f"--junitxml={repeat}"], REPO, dict(env)
        )
        readings["worktree_take2"] = {"exit": proc2.returncode, "xml": repeat.name}
    if args.only == "final":
        # Take 3 exists because take 1 started at 10:28:21Z and the A2 lint re-wrap landed at
        # 10:37:36Z, i.e. nine minutes into a ten-minute plane. Whichever tests it ran after that
        # stamp read post-lint bytes, so take 1 is a record of a race, not of the wave.
        xml = RUNS / f"{args.final_name}.xml"
        proc = run(args.final_name, base + [f"--junitxml={xml}"], REPO, dict(env))
        readings["final"] = {"exit": proc.returncode, "xml": xml.name, "cwd": str(REPO)}
    if args.only in ("both", "control"):
        info = prepare_control(args.control_rev)
        readings["control_setup"] = info
        if (
            info["worktree_add_exit"] != 0
            or info["control_head"] != info["resolved_rev"]
            or info["control_head"] == ""
        ):
            print(f"control_setup={readings['control_setup']}", flush=True)
            print("w68_test_planes_exit=1 # control_is_not_the_pre_wave_tree")
            return 1
        xml = RUNS / "test_plane_w39_control.xml"
        control_env = dict(env)
        control_env["PYTHONPATH"] = str(CONTROL_DIR)
        proc = run("test_plane_w39_control", base + [f"--junitxml={xml}"], CONTROL_DIR, control_env)
        readings["control"] = {"exit": proc.returncode, "xml": xml.name}
    print(f"readings={readings}", flush=True)
    print("w68_test_planes_exit=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
