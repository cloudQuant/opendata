#!/usr/bin/env python3
"""Write the C86 gap-reason staleness carrier from the instrument's own stdout, verbatim.

The body is not prose: it is what ``gap-reason-staleness.py`` printed for one full measure pass,
with its exit code beside it, under a provenance block that pins the revision, the interpreter and
the digests of *both* files the readings depend on (the instrument and the shipped probe module).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import pathlib
import platform
import subprocess  # nosec B404  # carrier generator: literal argv git/python only, no shell string
import sys
from typing import Final

REPO: Final = pathlib.Path(__file__).resolve().parents[3]
DIR: Final = pathlib.Path("docs/evidence/C86")
INSTRUMENT: Final = "docs/evidence/C86/gap-reason-staleness.py"
MODULE: Final = "scripts/quality/acceptance_item_probe.py"
OUT: Final = "docs/evidence/C86/gap-reason-staleness.txt"
TITLE: Final = "# C86 —— gap 台账 reason 与探针现读数是否仍一致"


def argv(*args: str) -> str:
    """Run one command from a literal argv list and return its stripped stdout."""
    done = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, git only, no shell string
        args, cwd=REPO, capture_output=True, text=True, check=False
    )
    return done.stdout.strip()


def digest(rel: str) -> str:
    """sha256[:16] of a repo file's bytes, with no newline translation."""
    return hashlib.sha256((REPO / rel).read_bytes()).hexdigest()[:16]


def main() -> int:
    """Run the instrument once and archive its stdout byte-for-byte under a provenance block."""
    when = dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    command = f"python {INSTRUMENT}"
    done = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, this interpreter, no shell
        [sys.executable, str(REPO / INSTRUMENT)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [
        TITLE,
        f"# 采集时间（跑前）: {when}",
        f"# 分支: {argv('git', 'rev-parse', '--abbrev-ref', 'HEAD')}"
        f"  HEAD: {argv('git', 'rev-parse', '--short', 'HEAD')}",
        f"# python: {platform.python_version()} @ {sys.executable}",
        f"# 仪器 sha256[:16]: {digest(INSTRUMENT)}",
        f"# 探针模块 sha256[:16]: {digest(MODULE)}",
        f"# command: {command}",
        "# git status --porcelain（跑前）:",
        argv("git", "status", "--porcelain"),
        "#",
        "",
    ]
    body = done.stdout.rstrip("\n")
    err = done.stderr.strip()
    if err:
        body += "\n# stderr:\n" + err
    text = "\n".join(lines) + body + f"\nINSTRUMENT_RC={done.returncode}\n"
    target = REPO / OUT
    target.write_text(text, encoding="utf-8")
    print(f"wrote {OUT} bytes={target.stat().st_size}")
    print(f"INSTRUMENT_RC={done.returncode}")
    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
