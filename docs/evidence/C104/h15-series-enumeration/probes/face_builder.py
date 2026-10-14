#!/usr/bin/env python3
"""C104: build ``face.md`` by copying face lines out of the carriers -- no typed numbers.

A hand-written summary is where a stale reading gets re-published. Here every quoted line is taken
from a named carrier by pattern, and a carrier that yields no face line is printed as such rather
than skipped, so a silent archive cannot pass as an empty one.
"""

from __future__ import annotations

import pathlib
import re
import sys

RUNS = pathlib.Path(__file__).resolve().parent.parent / "runs"
ORDER = (
    "probe-pass.txt",
    "finding-triage-identity-only.txt",
    "pytest-allowlist-consumers.txt",
    "staged-payload-rescan.txt",
    "secret-check-before-edit.txt",
    "secret-check-after-edit.txt",
)
KEEP = re.compile(
    r"^(face: |arm |verdict: |=+ \d+ passed|collected \d+ items|.*_exit=|.*commits scanned|.*no leaks found|.*scanned ~)"
)


def main() -> int:
    out = [
        "# C104 / h15-series-enumeration -- the faces, quoted out of the carriers",
        "",
        "Every line below is copied out of the named carrier by `probes/face_builder.py`; no number",
        "in it was typed. The carriers hold the full bodies and their own headers.",
        "",
    ]
    missing: list[str] = []
    empty: list[str] = []
    for name in ORDER:
        path = RUNS / name
        out.append(f"## {name}")
        out.append("")
        out.append("```text")
        if not path.is_file():
            missing.append(name)
            out.append("(carrier absent)")
        else:
            kept = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if KEEP.match(ln.strip())]
            if not kept:
                empty.append(name)
            out.extend(kept or ["(no face line matched)"])
        out.append("```")
        out.append("")
    (RUNS.parent / "face.md").write_text("\n".join(out), encoding="utf-8")
    print(f"face: carriers_expected={len(ORDER)} absent={missing} no_face_lines={empty}")
    print(f"face: face_md_bytes={(RUNS.parent / 'face.md').stat().st_size}")
    if missing or empty:
        print("verdict: REFUSED")
        return 1
    print("verdict: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
