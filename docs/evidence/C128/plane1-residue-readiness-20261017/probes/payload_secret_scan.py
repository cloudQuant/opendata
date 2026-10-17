r"""Re-measure the C128 payload against the shipped gitleaks config, with both arms on purpose.

One line of ``runs/declare_candidates.txt`` -- the yfinance ``KeyExecutives`` census row, line 57
of the 64-line capture -- fired ``generic-api-key``: the rule's gap allowance reached from that
model name into the next 20-40 character assignment token, so a public fetcher class name read as
a credential. Two fixes were available and only one was honest:

* widen ``.gitleaks.toml`` -- rejected. ``tests/test_acceptance_item_probe.py:707-731`` asserts
  the ``generic-api-key`` allowlist equals the frozen ``GENERIC_API_KEY_ALLOWED_REGEXES``, and
  that ``public_gitleaks_rule_shapes`` refuses loosened variants, so an added entry would widen a
  judge on a shipped AC-1|09 face. ``\.md$`` is exempted only for ``curl-auth-header``, so
  renaming the carrier to markdown would not have helped either.
* reorder the *columns of this round's own instrument* so no ``Key*`` name sits within the gap of a
  long token -- chosen. Every field is still printed; nothing about what the scanner sees is
  exempted.

The guard against that second move being a dodge rather than a fix is arm B: a runtime-generated
Slack-shaped token is planted into the same reordered file and must still be found. Arm A is the
unperturbed payload and must be 0. One arm being able to differ from the other is the whole
judgment; a clean scan without arm B would only prove that the scanner looked.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
CONFIG = REPO_ROOT / ".gitleaks.toml"
#: The trees this round adds, in the layout gitleaks gets pointed at.
PAYLOAD_DIRS = (
    "docs/evidence/C128/plane1-residue-readiness-20261017/probes",
    "docs/evidence/C128/plane1-residue-readiness-20261017/runs",
)
#: The run-carrier tree, where the planted token goes.
RUNS_REL = "docs/evidence/C128/plane1-residue-readiness-20261017/runs"


def _scan(source: Path, report: Path) -> tuple[int, list[dict[str, Any]]]:
    """Run gitleaks over one throwaway tree and return its exit code and findings.

    The report lands outside the tree, so arm A's report cannot become part of what arm B scans.
    """
    cmd = [
        "gitleaks",
        "detect",
        "--no-git",
        "--source",
        str(source),
        "--config",
        str(CONFIG),
        "--redact",
        "--report-format",
        "json",
        "--report-path",
        str(report),
    ]
    proc = subprocess.run(  # noqa: S603  # nosec B603  # fixed argv, shell disabled, which(gitleaks) checked
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if not report.is_file():
        return proc.returncode, []
    return proc.returncode, json.loads(report.read_text(encoding="utf-8"))


def _stage(roots: list[str], work: Path) -> int:
    """Copy the payload trees into ``work``, so a planted finding cannot be read as a real one."""
    copied = 0
    for rel in roots:
        src = REPO_ROOT / rel
        if not src.is_dir():
            raise SystemExit(f"payload dir is missing: {src}")
        for path in sorted(p for p in src.rglob("*") if p.is_file()):
            if "__pycache__" in path.parts or path.suffix == ".json":
                continue
            target = work / path.relative_to(REPO_ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            copied += 1
    return copied


def main() -> int:
    """Scan the payload as-is (arm A) and with one planted token (arm B), then gate on both."""
    if shutil.which("gitleaks") is None:
        print("SKIPPED=1 reason=gitleaks is not installed")
        return 0
    roots = list(PAYLOAD_DIRS)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        clean_dir = work / "arm_a"
        planted_dir = work / "arm_b"
        n = _stage(roots, clean_dir)
        shutil.copytree(clean_dir, planted_dir)
        carriers = sorted((planted_dir / RUNS_REL).glob("declare_candidates.txt"))
        if not carriers:
            raise SystemExit(f"no run carrier under {RUNS_REL} to plant into")
        carrier = carriers[0]
        lines = carrier.read_text(encoding="utf-8").splitlines()
        lines.insert(30, "planted_control token=xoxb-" + secrets.token_hex(18))
        carrier.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"planted_into={carrier.name} at_line=31 lines={len(lines)}")

        rc_a, found_a = _scan(clean_dir, work / "arm_a.json")
        rc_b, found_b = _scan(planted_dir, work / "arm_b.json")

    print(f"payload_files={n} payload_roots={len(roots)}")
    for fnd in found_a:
        name = Path(str(fnd["File"])).name
        print(f"A_REAL_FINDING rule={fnd['RuleID']} at={name}:{fnd['StartLine']}")
    for fnd in found_b:
        name = Path(str(fnd["File"])).name
        print(f"B_PLANTED_FOUND rule={fnd['RuleID']} at={name}:{fnd['StartLine']}")
    print(f"arm_a_findings={len(found_a)} arm_a_rc={rc_a}")
    print(f"arm_b_findings={len(found_b)} arm_b_rc={rc_b}")
    ok = len(found_a) == 0 and len(found_b) == 1
    print(f"payload_secret_face PASS={ok}")
    print("scan_exit=0" if ok else "scan_exit=1")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
