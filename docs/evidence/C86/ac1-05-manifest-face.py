#!/usr/bin/env python3
"""AC-1|05's blocker, read off the manifest it actually validates -- no measure pass required.

``gap-reason-staleness.py``'s face F counts the runtime issue classes, but two numbers the archived
ledger text quotes live one level further in: how many entries the C65 build-source manifest pins,
and whether the two per-file issue classes can be added up at all. This instrument reads those
directly, in seconds, and gates:

* ``flagged == distinct flagged paths`` -- the two per-file classes are disjoint, so their counts
  may be summed; armed by forging one path into both classes, which has to break it.
* every flagged path really is a manifest entry (``stray == 0``), armed by injecting a path the
  manifest never pinned (which has to read 1), and the accounting identity
  ``entries == clean + flagged`` is then computed from two independent counts, not derived.
* the missing paths' top-level directories are absent from the tree today, armed by a directory
  that does exist and one that does not.

Importing the repo's own validator, never a rebuilt judge.
"""

from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import pathlib
import platform
import subprocess  # nosec B404  # literal argv: read-only git rev-parse, never a shell string
import sys
from typing import Any, Final

REPO = pathlib.Path(__file__).resolve().parents[3]
SELF: Final = "docs/evidence/C86/ac1-05-manifest-face.py"
ARCHIVE: Final = "docs/evidence/C86/ac1-05-manifest-face.txt"
MANIFEST: Final = "docs/evidence/C65/runtime-build-source.json"
VALIDATOR: Final = "scripts/quality/runtime_stack_evidence.py"
MISSING: Final = "build-source-file-missing"
CHANGED: Final = "build-source-file-hash-mismatch"
STRAY_PATH: Final = "opendata_qoder_manifest_probe_absent/never_pinned.py"
PRESENT_DIR: Final = "opendata"
ABSENT_DIR: Final = "opendata_qoder_manifest_probe_absent"
DIGEST_LEN: Final = 16
TITLE: Final = "# C86 —— AC-1|05 运行面 blocker 的 manifest 面（秒级可复算，不需整轮实测）"


def digest(rel: str) -> str:
    """sha256[:16] of a repo file's bytes."""
    return hashlib.sha256((REPO / rel).read_bytes()).hexdigest()[:DIGEST_LEN]


def path_of(message: str) -> str:
    """The source path a per-file issue names, taken from the issue's own message."""
    return message.split(": ", 1)[-1].strip()


def git_stdout(*args: str) -> str:
    """One read-only git command, stdout stripped. One claim per call, never a mixed argv."""
    done = subprocess.run(  # noqa: S603  # nosec B603 B607  # read-only git, exec on PATH
        ["git", *args],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip()


def head_ref() -> str:
    """Branch and short revision, from two calls.

    ``rev-parse --abbrev-ref HEAD --short HEAD`` prints the branch twice and no commit, so the
    single-argv version of this header pinned nothing -- the provenance face was itself the bug.
    """
    return (
        f"分支: {git_stdout('rev-parse', '--abbrev-ref', 'HEAD')}"
        f"  HEAD: {git_stdout('rev-parse', '--short', 'HEAD')}"
    )


def manifest_face() -> dict[str, Any]:
    """Read the pinned manifest, then re-run the repo's validator over it."""
    book = json.loads((REPO / MANIFEST).read_text(encoding="utf-8"))
    names = [str(name) for name in (book.get("files") or {})]
    sys.path.insert(0, str(REPO))
    from scripts.quality.runtime_stack_evidence import validate

    issues = validate(REPO).issues
    per_file = {
        code: [path_of(str(issue.message)) for issue in issues if issue.code == code]
        for code in (MISSING, CHANGED)
    }
    return {
        "names": names,
        "date": str(book.get("date", "")),
        "identity": str(book.get("source_identity", "")),
        "projection": str(book.get("projection", "")),
        "issues": len(issues),
        "per_file": per_file,
    }


def main() -> int:
    """Print the manifest face and its controls, and write the archive under a provenance block."""
    face = manifest_face()
    names = set(face["names"])
    missing = face["per_file"][MISSING]
    changed = face["per_file"][CHANGED]
    entries = len(names)
    flagged = len(missing) + len(changed)
    flagged_paths = set(missing) | set(changed)
    distinct = len(flagged_paths)
    dup_pool = [*changed, *missing[:1]]
    tampered = len(dup_pool) + len(missing) == len(set(missing) | set(dup_pool))
    stray = len(flagged_paths - names)
    stray_control = len((flagged_paths | {STRAY_PATH}) - names) == 1
    clean = len(names - flagged_paths)
    dirs = dict(collections.Counter(p.split("/")[0] for p in missing if "/" in p))
    exist_read = {name: (REPO / name).is_dir() for name in sorted(dirs)}
    control_present = (REPO / PRESENT_DIR).is_dir()
    control_absent = (REPO / ABSENT_DIR).is_dir()
    covered = bool(dirs) and sum(dirs.values()) == len(missing)
    lines = [
        f"manifest {MANIFEST}",
        f"  entries={entries} date={face['date']}",
        f"  source_identity={face['identity']}",
        f"  projection={face['projection']}",
        f"  validator issues={face['issues']} missing={len(missing)} changed={len(changed)}",
        f"  flagged={flagged} distinct_flagged_paths={distinct} additive={flagged == distinct}",
        f"  class_tamper(forged_dup_still_additive)={tampered} expected=False",
        f"  stray_flagged_paths_not_in_manifest={stray} control_injected_stray={stray_control}",
        f"  clean_entries={clean} accounting identity={entries == clean + flagged}"
        f" ({entries}=={clean}+{flagged})",
        f"  missing_top_dirs={dirs or '{}'} exist_today={exist_read or '{}'}",
        f"  control(present_dir={control_present}, absent_dir={control_absent},"
        f" dir_tally_covers_missing={covered})",
    ]
    for line in lines:
        print(line)
    ok = (
        entries > 0
        and flagged == distinct
        and not tampered
        and stray == 0
        and stray_control
        and entries == clean + flagged
        and bool(dirs)
        and covered
        and not any(exist_read.values())
        and control_present
        and not control_absent
    )
    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    header = [
        TITLE,
        f"# 采集时间: {stamp}",
        f"# {head_ref()}",
        f"# python: {platform.python_version()} @ {sys.executable}",
        f"# 仪器 sha256[:{DIGEST_LEN}]: {digest(SELF)}",
        f"# 校验器 sha256[:{DIGEST_LEN}]: {digest(VALIDATOR)}",
        f"# command: python {SELF}",
        "#",
        "",
    ]
    verdict = f"MANIFEST_FACE_CHECK {'PASS' if ok else 'FAIL'}\n"
    (REPO / ARCHIVE).write_text(
        "\n".join(header) + "\n".join(lines) + "\n" + verdict, encoding="utf-8"
    )
    print(verdict.strip())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
