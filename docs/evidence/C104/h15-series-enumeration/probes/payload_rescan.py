#!/usr/bin/env python3
"""C104: copy the exact staged payload and scan it with the edited config.

The commit adds 12 files to the payload that did not exist when the family's finding was triaged,
two of which are this round's own instruments. A round that closes a credential finding by writing
a probe about credentials has to re-scan, or the fix is the next finding -- which is literally what
``.gitleaks.toml``'s header records for commit 5e7c81a2e39d.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[5]
PAYLOAD = pathlib.Path("/tmp/c104_scan_payload")
REPORT = pathlib.Path("/tmp/c104_gl_newpayload.json")

print("argv=gitleaks detect --no-git --source /tmp/c104_scan_payload --config <edited .gitleaks.toml> --redact=20 -f json --no-banner")
print("identity only: file:line#rule. No flagged text is written to this output.")

paths = [p for p in subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
    ["git", "diff", "--cached", "--name-only", "-z"],
    cwd=REPO,
    capture_output=True,
    text=True,
    shell=False,
    check=True,
).stdout.split("\0") if p]

if PAYLOAD.exists():
    shutil.rmtree(PAYLOAD)
PAYLOAD.mkdir(parents=True)
copied = missing = 0
total = 0
for rel in paths:
    src = REPO / rel
    if not src.is_file():
        missing += 1
        continue
    target = PAYLOAD / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    copied += 1
    total += target.stat().st_size

print(f"face: staged_paths={len(paths)} copied={copied} bytes={total}")
print(f"face: missing_on_disk={missing}")
print(f"face: c104_files_in_payload={len([p for p in paths if p.startswith('docs/evidence/C104/')])}")
print(f"face: config_edits_in_payload={[p for p in paths if p in ('.gitleaks.toml', 'scripts/quality/acceptance_item_probe.py')]}")

if REPORT.exists():
    REPORT.unlink()
done = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
    [
        "gitleaks",
        "detect",
        "--no-git",
        "--source",
        str(PAYLOAD),
        "--config",
        str(REPO / ".gitleaks.toml"),
        "--redact=20",
        "-f",
        "json",
        "-r",
        str(REPORT),
        "--no-banner",
    ],
    cwd=REPO,
    capture_output=True,
    text=True,
    shell=False,
    check=False,
)
print(f"face: scan_exit={done.returncode}")
body = REPORT.read_text(encoding="utf-8").strip() if REPORT.exists() else ""
findings = json.loads(body) if body else []
print(f"face: findings={len(findings)}")
for f in findings:
    print(f"  {f.get('File')}:{f.get('StartLine')}#{f.get('RuleID')}")
if copied != len(paths) - missing:
    print("verdict: REFUSED (the copy did not account for every staged path)")
    sys.exit(1)
if findings or done.returncode != 0:
    print("verdict: REFUSED (the payload the commit adds is not clean)")
    sys.exit(1)
print("verdict: PASS")
sys.exit(0)
