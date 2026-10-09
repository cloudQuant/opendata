"""Select C75 counterfact fixtures by measurement, not by a hand-written guess at rule shapes.

Copy three real repo files into a throwaway history repo and scan them with the DEFAULT gitleaks
ruleset only (no allowlists anywhere). Every finding printed here is a line that can be used as a
fixture; a fixture that no unexempted rule ever fires would make the silent arms measure nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404
import tempfile
from pathlib import Path

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
FILES = [
    "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/census-sec-tmx-fed-gov-finra.json",
    "docs/evidence/C65/replay-full-manifest.json",
    "tests/test_ecb_series_client.py",
]

root = Path(tempfile.mkdtemp(prefix="c75-base-"))
(root / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
for rel in FILES:
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / rel, dest)
git = shutil.which("git")
gitleaks = shutil.which("gitleaks")
if git is None or gitleaks is None:
    raise SystemExit("git and gitleaks must both be on PATH")
for step in (
    ["init", "-q", "."],
    ["-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
    ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "baseline"],
):
    subprocess.run(  # nosec B603  # noqa: S603
        [git, *step],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
report = root / "report.json"
code = subprocess.run(  # nosec B603  # noqa: S603
    [
        gitleaks,
        "detect",
        "--source",
        str(root),
        "--config",
        str(root / ".gitleaks.toml"),
        "--report-format",
        "json",
        "--report-path",
        str(report),
        "--no-banner",
        "--redact",
    ],
    capture_output=True,
    text=True,
)
findings = json.loads(report.read_text(encoding="utf-8")) if report.exists() else []
print(f"rc={code.returncode} findings={len(findings)}")
for f in findings:
    loc = f.get("Location") or {}
    start = loc.get("start") or 0
    rel = str(f.get("File"))
    src_files = [p for p in FILES if rel.endswith(p)] or [rel]
    line_text = ""
    try:
        lines = (root / src_files[0]).read_text(encoding="utf-8").splitlines()
        line_text = lines[start - 1][:150] if 0 < start <= len(lines) else ""
    except OSError:
        pass
    print(
        f"- rule={f.get('RuleID')} file={src_files[0]} line={start} "
        f"entropy={f.get('Entropy')} secret={f.get('Secret')!r}"
    )
    print(f"    LINE: {line_text}")
shutil.rmtree(root, ignore_errors=True)
