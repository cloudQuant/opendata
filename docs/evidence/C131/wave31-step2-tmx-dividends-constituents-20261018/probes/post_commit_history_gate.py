r"""Re-take the factory history-plane credential gate over the commit that just landed.

``make secret-check`` -> ``scripts/quality/secret_scan_check.py`` -> ``gitleaks detect --source .``
walks git *history*. That makes it structurally impossible for a commit to carry its own
history-plane face: at the moment the bytes are being staged they are not yet reachable, and the
moment they are reachable the carrier recording the walk is itself a new commit. So each commit's
history reading is taken by the next one, and the previous wave's carrier is the only place the
number for *this* commit can be compared against.

This driver does not restate the wrapper's verdict. It runs the shipped script with a fixed list
argv and copies what it printed, then adds the two checks that make the copy worth having:

* ``commits_scanned`` is parsed off gitleaks' own INF line and compared with ``git rev-list --count
  HEAD``. A scan of an empty or partial history also prints ``no leaks found``, and without this
  equality the phrase is unfalsifiable -- it would read the same over zero commits.
* the delta against ``--prev-carrier`` is attributed to the commits in between, taken from
  ``git rev-list --count <that commit>..HEAD``, so a face that moves by two is not silently
  credited with moving by one.

The carrier is named by the commit it scanned and written in one ``write_text`` at the very end: a
crash before that leaves no half-file claiming a verdict.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess  # nosec B404  # git plus the shipped script, list argv, shell never enabled
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
SCRIPT = REPO_ROOT / "scripts/quality/secret_scan_check.py"
RUNS = Path(__file__).resolve().parent.parent / "runs"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SCANNED = re.compile(r"(\d+) commits scanned")
CARRIER_SHA = re.compile(r"([0-9a-f]{7,9})\.txt$")


def _git(*args: str) -> str:
    """Run git with a fixed list argv; a non-zero exit aborts instead of printing a fake count."""
    result = subprocess.run(  # noqa: S603  # nosec B603 B607  # literal argv, shell never enabled
        ["git", *args],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"git {args[0]} failed rc={result.returncode}: {result.stderr.strip()}")
    return result.stdout


def _prev_commits(carrier: Path) -> int | None:
    """Read the ``commits_scanned=`` figure out of the previous commit's carrier, if it has one."""
    if not carrier.is_file():
        return None
    for line in carrier.read_text(encoding="utf-8").splitlines():
        m = re.match(r"commits_scanned=(\d+)", line)
        if m:
            return int(m.group(1))
    return None


def _carrier_commit(carrier: Path) -> str:
    """Find the commit the previous face priced: its own field first, then its filename."""
    if carrier.is_file():
        for line in carrier.read_text(encoding="utf-8").splitlines():
            m = re.match(r"head_commit_at_scan=(\S+)", line)
            if m:
                return m.group(1)
    m = CARRIER_SHA.search(carrier.name)
    if m is None:
        raise SystemExit(f"cannot derive a commit from carrier {carrier.name}")
    return m.group(1)


def main(argv: list[str] | None = None) -> int:
    """Run the shipped wrapper once and write the carrier that names the commit it priced."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prev-carrier",
        required=True,
        type=Path,
        help="carrier of the previous history-plane face, for the delta",
    )
    args = parser.parse_args(argv)

    binary = shutil.which("gitleaks")
    if binary is None:
        print("post_commit_secret_check=SKIPPED reason=gitleaks-not-on-path")
        return 1

    head = _git("rev-parse", "--short=9", "HEAD").strip()
    prev_commit = _carrier_commit(args.prev_carrier)
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    result = subprocess.run(  # noqa: S603  # nosec B603  # the shipped script, list argv, no shell
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    out = ANSI.sub("", result.stdout)
    err = ANSI.sub("", result.stderr)
    info_lines = [ln for ln in err.splitlines() if " INF " in ln]
    leak_lines = [ln for ln in info_lines if "no leaks found" in ln]
    m = SCANNED.search(err)
    scanned = int(m.group(1)) if m else None
    rev_count = int(_git("rev-list", "--count", "HEAD").strip())
    span = int(_git("rev-list", "--count", f"{prev_commit}..HEAD").strip())
    prev = _prev_commits(args.prev_carrier)
    delta = None if prev is None or scanned is None else scanned - prev

    ok = (
        result.returncode == 0
        and len(leak_lines) == 1
        and scanned == rev_count
        and delta is not None
        and delta == span
    )
    faces = [
        "wave-31 step 2 -- post-commit factory secret check re-taken over the back-fill commit",
        f"argv: PYTHONPATH=. {sys.executable} {SCRIPT.relative_to(REPO_ROOT)}",
        "(cwd = repo root, no pipes; gitleaks writes its lines to stderr and puts the colour reset",
        "right after INF, so both streams below are shown with the ANSI escapes stripped)",
        "",
        f"stdout (verbatim, {len(result.stdout.encode())} bytes):",
        out.rstrip("\n"),
        "",
        "stderr (ANSI-stripped):",
        err.rstrip("\n"),
        "",
        f"info_lines={len(info_lines)} info_lines_matching_no_leaks_found={len(leak_lines)}",
        f"commits_scanned={scanned} rev_list_count_head={rev_count} "
        f"agreement={scanned == rev_count}",
        f"prev_carrier={args.prev_carrier.name} prev_carrier_commit={prev_commit}",
        f"commits_scanned_previous_face={prev}",
        f"commits_scanned_delta={delta} rev_list_span_prev_to_head={span} "
        f"delta_matches_span={delta == span}",
        f"head_commit_at_scan={head} gitleaks_binary={binary}",
        f"secret_scan_check_exit={result.returncode}",
        f"gate_agreement={ok}",
        "(exit, exactly one 'no leaks found', history counted twice by two different tools,",
        "  and the face-to-face delta equals the commit span between the two carriers)",
        f"chain_post_commit_secret_check_exit={0 if ok else 1}",
    ]
    text = "\n".join(faces) + "\n"

    carrier = RUNS / f"wave31-step2-post_commit_secret_check_{head}.txt"
    carrier.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"carrier_written bytes={len(text.encode())} path={carrier.relative_to(REPO_ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
