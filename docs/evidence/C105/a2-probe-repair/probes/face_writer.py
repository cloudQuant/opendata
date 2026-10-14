#!/usr/bin/env python3
"""C105: build ``face.md`` from the carriers and refuse a README that says anything they do not.

face.md is a projection, not a transcription: for every carrier under ``runs/`` it takes the header
lines the archiver wrote (``argv=``, the ``*_exit=`` line, the source digest) and each judgment
line in the body -- ``face:``, ``identity:``, ``refusal:``, ``rerun:``, ``carrier_diff:``,
``refusal_arm:``, ``verdict:`` -- so every number in it traces to a run already on disk.

README.md is the opposite direction. Its prose is authored, so every inline literal and every
number it contains is looked up in face.md; anything not found is an unresolved token and the
round refuses. That is what keeps this document from becoming a verbal judgment: the only way to
clear it is to make the carrier say the thing first.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
from collections import Counter

REPO = pathlib.Path(__file__).resolve().parents[5]
FAMILY = REPO / "docs/evidence/C105/a2-probe-repair"
RUNS = FAMILY / "runs"
FACE = FAMILY / "face.md"
README = FAMILY / "README.md"

JUDGMENT_PREFIXES = (
    "face:",
    "identity:",
    "refusal:",
    "rerun:",
    "carrier_diff:",
    "refusal_arm:",
    "verdict:",
    "    prose_skip",
    "    claim_skip",
    "    block_",
    "    tokens_only",
    "    restored",
)
HEADER_KEYS = ("argv=", "source_bytes=", "source_sha256_16=", "_exit=")


def carrier_lines(path: pathlib.Path) -> list[str]:
    """The header and judgment lines of one carrier, in the order they were written."""
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped == "-" * 70:
            continue
        if stripped.startswith("carrier:") or stripped.startswith("round:"):
            out.append(stripped)
            continue
        if any(key in stripped for key in HEADER_KEYS):
            out.append(stripped)
            continue
        if stripped.startswith(JUDGMENT_PREFIXES):
            out.append(stripped)
    return out


def build_face() -> str:
    """Render face.md as one section per carrier, each section the carrier's own lines."""
    parts = [
        "# C105 a2-probe-repair -- faces, projected from the carriers\n",
        "",
        "Every line below is copied out of the named carrier, whose body is the verbatim stdout of",
        "the run its ``argv=`` header records. Nothing on this page is typed from memory.\n",
    ]
    for path in sorted(RUNS.glob("*.txt")):
        parts.append(f"\n## {path.name}\n")
        parts.extend(f"- {line}" for line in carrier_lines(path))
    return "\n".join(parts) + "\n"


def claim_tokens(text: str) -> Counter[str]:
    """Every inline literal and every number the README asserts."""
    single = text.replace("``", "`")
    tokens = re.findall(r"`[^`\n]+`|-?\b\d[\d.,()%]*\b", single)
    skip = {"`", "``"}
    return Counter(t for t in tokens if t not in skip and len(t) > 1)


def main() -> int:
    """Write face.md, then check the README's claims against it."""
    if not RUNS.is_dir():
        print("verdict: REFUSED - the runs/ directory is not there yet")
        return 1
    face_text = build_face()
    FACE.write_text(face_text, encoding="utf-8")
    print(
        f"face: carriers={len(list(RUNS.glob('*.txt')))} face_md_bytes={len(face_text.encode())}"
        f" face_md_sha256_16={hashlib.sha256(face_text.encode()).hexdigest()[:16]}"
    )
    if not README.is_file():
        print("face: readme_present=False unresolved_tokens=not_measured")
        print("verdict: REFUSED - the README is not written yet")
        return 1
    readme = README.read_text(encoding="utf-8")
    haystack = face_text.replace("``", "`")
    unresolved = {
        token: count
        for token, count in sorted(claim_tokens(readme).items())
        if token.strip("`") not in haystack and token not in haystack
    }
    print(
        f"face: readme_claims={len(claim_tokens(readme))} unresolved_tokens={len(unresolved)}"
        f" readme_bytes={len(readme.encode())}"
    )
    for token, count in list(unresolved.items())[:20]:
        print(f"    unresolved {token!r} count={count}")
    if unresolved:
        print("verdict: REFUSED - the README asserts something no carrier says")
        return 1
    print("verdict: PASS - every README literal and number is a face.md line")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
