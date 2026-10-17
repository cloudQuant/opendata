"""Bring the three wave-28 step-2 probes up to the repo's own ruff rule set.

The probes are carriers, so they are held to the standard the earlier waves' probes meet: D103
docstrings on every public function, and the S603/S108 handling the C126 probes already use. The
six E501 lines are reflowed by hand afterwards, because no mechanical break is safe inside an
f-string.
"""

from __future__ import annotations

import re
from pathlib import Path

PROBES = Path("docs/evidence/C128/plane1-residue-readiness-20261017/probes")
FILES = ["w28s2_run_chain.py", "receipt_drift_counterfact.py", "w28s2_shard_faces.py"]
DOCSTRING = {
    "run": "Execute one shipped command and return its face (argv, exit, streams).",
    "carrier": "Render one command face as the carrier text this wave commits.",
    "main": "Run the chain and write every carrier into the wave's runs directory.",
    "call": "Execute one shipped command and return its argv, exit and stream sizes.",
    "read_face": "Read the judge report's nasdaq subset as a comparable face.",
    "norm": "Drop the fields a re-run necessarily rotates, keeping key order canonical.",
    "sha": "Digest a document after normalization.",
    "load": "Load a JSON document from the repository root.",
    "nasdaq_verdicts": "Map provider model and phase to its emitted status.",
    "reproducibility": "Produce the produced-versus-signed agreement face.",
    "coherence": "Produce the four-document coherence face for the shipped shard.",
    "_array_can_move": "Show the claims-array comparison can be falsified by a flipped verdict.",
}
DEF_RE = re.compile(r"^def (\w+)\(")


def add_docstrings(text: str) -> str:
    """Insert the missing one-line docstring under each public def of a probe."""
    lines = text.split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        out.append(line)
        m = DEF_RE.match(line)
        if not m or not line.startswith("def "):
            continue
        body = lines[i + 1] if i + 1 < len(lines) else ""
        if body.strip().startswith('"""') or body.strip().startswith('r"""'):
            continue
        name = m.group(1)
        doc = DOCSTRING.get(name)
        if doc is None:
            continue
        out.append(f'    """{doc}"""')
    return "\n".join(out)


def fix_noqa(text: str) -> str:
    """Mark the probes' own subprocess calls the way the C126 probes mark theirs."""
    return re.sub(
        r"subprocess\.run\($",
        "subprocess.run(  # noqa: S603  # nosec B603  # argv is this probe's own shipped command",
        text,
        flags=re.M,
    )


def fix_tmp(text: str) -> str:
    """Send the merge snapshot to a mode-700 tempfile directory instead of a fixed /tmp name."""
    replacement = (
        "take1_dir = Path(tempfile.mkdtemp())\n"
        '    take1 = take1_dir / "claims.merged.take1.json"\n'
        "    shutil.copy(merged, take1)"
    )
    text = text.replace('shutil.copy(merged, "/tmp/w28s2_merged_take1.json")', replacement)
    text = text.replace("Path('/tmp/w28s2_merged_take1.json')", "take1")
    text = text.replace('stable_sha(Path("/tmp/w28s2_merged_take1.json"))', "stable_sha(take1)")
    if "import tempfile" not in text and "tempfile." in text:
        text = text.replace("import subprocess", "import subprocess\nimport tempfile", 1)
    return text


def main() -> None:
    """Rewrite each probe through the three passes and report what remains."""
    for name in FILES:
        path = PROBES / name
        text = path.read_text(encoding="utf-8")
        text = add_docstrings(text)
        text = fix_noqa(text)
        text = fix_tmp(text)
        path.write_text(text, encoding="utf-8")
        print(f"rewrote {name} bytes={path.stat().st_size}")


if __name__ == "__main__":
    main()
