#!/usr/bin/env python3
"""Write a C86 acceptance carrier from the shipped probe's own stdout, verbatim.

The archive body is not prose: it is what ``scripts/quality/acceptance_item_probe.py`` printed for
the named cells, with its exit code beside it, under a provenance block that pins the revision,
the interpreter and the probe module's digest. Defaults regenerate ``probe-seven.txt`` — the first
judging readings for the seven gap cells that had no probe at all; pass ``--item``/``--out`` for
any other cell, e.g. AC-17|02's exemption census.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import pathlib
import platform
import subprocess  # nosec B404  # carrier generator: literal argv git/python only, no shell string
import sys
from typing import Final

REPO: Final = pathlib.Path(__file__).resolve().parents[3]
MODULE: Final = "scripts/quality/acceptance_item_probe.py"
DIR: Final = pathlib.Path("docs/evidence/C86")
SEVEN: Final = ("§4|02", "§4|04", "§5|02", "AC-8|08", "AC-13|08", "AC-15|01", "AC-15|02")
DEFAULT_TITLE: Final = (
    "# C86 acceptance_item_probe —— 七个原本无探针的 gap 格子首次被判定（判定面为实测读数）"
)


def argv(*args: str) -> str:
    """Run one command from a literal argv list and return its stripped stdout."""
    done = subprocess.run(  # noqa: S603  # nosec B603  # argv is a tuple, no literal path
        args, cwd=REPO, capture_output=True, text=True, check=False
    )
    return done.stdout.strip()


def header(when: str, command: str, title: str) -> list[str]:
    """Return the provenance block: title, env, revision, digest, command, working tree."""
    digest = hashlib.sha256((REPO / MODULE).read_bytes()).hexdigest()[:16]
    return [
        title,
        f"# 采集时间（跑前）: {when}",
        f"# 分支: {argv('git', 'rev-parse', '--abbrev-ref', 'HEAD')}"
        f"  HEAD: {argv('git', 'rev-parse', '--short', 'HEAD')}",
        f"# python: {platform.python_version()} @ {sys.executable}",
        f"# 探针模块 sha256[:16]: {digest}",
        f"# command: {command}",
        "# git status --porcelain（跑前）:",
        argv("git", "status", "--porcelain"),
        "#",
        "",
    ]


def run(items: list[str], json_path: pathlib.Path) -> tuple[str, str, str, int]:
    """Invoke the probe over ``items`` and return (stdout, stderr, command, exit code)."""
    shells = [sys.executable, str(REPO / MODULE)]
    for item in items:
        shells += ["--item", item]
    shells += ["--json", str(json_path)]
    command = "python " + MODULE + "".join(f" --item {item!r}" for item in items)
    command += f" --json {json_path}"
    done = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, this interpreter, no shell
        shells, cwd=REPO, capture_output=True, text=True, check=False
    )
    return done.stdout, done.stderr, command, done.returncode


def main() -> int:
    """Generate one carrier archive and echo its exit face."""
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--item", action="append", dest="items", help="repeat once per cell")
    parser.add_argument("--out", default=str(DIR / "probe-seven.txt"))
    parser.add_argument("--json", dest="json_out", default=str(DIR / "probe-seven-readings.json"))
    parser.add_argument("--title", default=DEFAULT_TITLE)
    args = parser.parse_args()
    items: list[str] = args.items or list(SEVEN)
    json_path = REPO / args.json_out
    when = dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    out, err, command, rc = run(items, json_path)
    body = [out]
    if err.strip():
        body.append("--- stderr ---\n" + err)
    body.append(f"PROBE_EXIT={rc}\n")
    target = REPO / args.out
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(header(when, command, args.title)) + "\n".join(body), encoding="utf-8"
    )
    print(f"wrote {target.relative_to(REPO)}  bytes={target.stat().st_size}")
    print(f"PROBE_EXIT={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
