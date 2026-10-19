"""Credential face for the wave-39 captures: baseline over committed bytes, plant as the arm.

The captures are 46 files of other people's HTTP responses -- including a Cloudflare
``set-cookie`` the server issued on its own -- so "the bundle carries no credential" is a claim
that needs a detector that can report one. Both scans use this repo's shipped gitleaks shape
(``.gitleaks.toml``, file paths, report file, ``--exit-code 0`` so the rc is not the verdict), and
a missing report is returned as ``-1``/``NO_REPORT`` rather than as zero.

That distinction is the reason this probe exists. An earlier take of the same reading called
``gitleaks dir-mode`` -- a verb gitleaks 8.30.1 does not have -- and mapped its empty stdout to
``findings=0``, which is exactly how a false clean gets manufactured: the flag error, not the
payload, decided the number. The baseline here is therefore only meaningful because the plant arm,
on the same argv, reports the fabricated token.

The token is fabricated, unused, assembled per call, written only into a ``/tmp`` copy, and never
printed.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
GITLEAKS = "/opt/homebrew/bin/gitleaks"
BUNDLE = REPO / "docs/evidence/C144/wave39-unauthenticated-access-20261019"
SRC = BUNDLE / "sources"
RUNS = BUNDLE / "runs"
CARRIER = RUNS / "w58_sources_credential_scan.txt"
SCRATCH = Path("/tmp/w58_c144_plant")  # nosec B108  # noqa: S108


def _plant_token() -> str:
    """Fabricated and unused; assembled per call so no rule reads it off a literal."""
    return "ghp_" + ("0123456789" * 3 + "abcdefghij")


def _scan(root: Path, report: Path) -> tuple[int, int, list[str]]:
    """Run the shipped gitleaks argv over one directory; a missing report is never zero."""
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [
            GITLEAKS,
            "dir",
            str(root),
            "--config",
            str(REPO / ".gitleaks.toml"),
            "--redact",
            "--no-banner",
            "--exit-code",
            "0",
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if not report.is_file():
        return proc.returncode, -1, []
    try:
        findings: Any = json.loads(report.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError:
        return proc.returncode, -1, []
    if not isinstance(findings, list):
        return proc.returncode, -1, []
    rules = [str(f.get("RuleID") or f.get("ruleID") or "?") for f in findings]
    return proc.returncode, len(findings), rules


def main() -> int:
    """Scan the committed captures, then the same payload carrying one fabricated token."""
    files = sorted(p for p in SRC.rglob("*") if p.is_file())
    base_rc, base_n, base_rules = _scan(SRC, RUNS / "w58_baseline.json")
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    shutil.copytree(SRC, SCRATCH)
    victim = next(p for p in sorted(SCRATCH.rglob("*.csv")) if p.is_file())
    victim.write_bytes(victim.read_bytes() + f'api_token = "{_plant_token()}"\n'.encode())
    victim_present = victim.is_file() and victim.read_bytes().endswith(b"\n")
    plant_rc, plant_n, plant_rules = _scan(SCRATCH, RUNS / "w58_planted.json")
    shutil.rmtree(SCRATCH)

    arms = {
        "baseline_reports_nothing": base_n == 0,
        "plant_is_reported": plant_n >= 1,
        "both_scans_were_measurements": base_n >= 0 and plant_n >= 1,
        "victim_was_inside_the_populated_copy": victim_present,
    }
    passed = sum(1 for v in arms.values() if v)
    ok = passed == len(arms)
    lines = [
        "# carrier for w58_sources_credential_scan",
        f"# argv: {GITLEAKS} dir <root> --config <repo>/.gitleaks.toml --redact --no-banner "
        "--exit-code 0 --report-format json --report-path <report>",
        f"population files={len(files)} bytes={sum(p.stat().st_size for p in files)}",
        f"baseline dir={SRC.name} rc={base_rc} findings={base_n} rules={base_rules}",
        f"planted dir={SCRATCH.name} rc={plant_rc} findings={plant_n} rules={plant_rules} "
        f"victim={victim.name}",
        f"plant_printed=False plant_len={len(_plant_token())}",
        "correction an earlier take used the verb 'dir-mode', which gitleaks 8.30.1 does not "
        "implement; its empty stdout was mapped to findings=0, so that zero was a flag error. "
        "This carrier re-took the reading on the shipped argv with a plant arm.",
        f"arms={ {k: bool(v) for k, v in arms.items()} }",
        f"sources_credential_face arms={len(arms)} arms_passed={passed} PASS={ok}",
        "",
        f"w58_sources_credential_scan_exit={0 if ok else 1}",
    ]
    text = "\n".join(lines) + "\n"
    CARRIER.write_text(text, encoding="utf-8")
    # The planted report is the fabricated token's only on-disk trace besides /tmp; the carrier
    # above is
    # the durable reading, so neither raw report may stay in the bundle.
    for name in ("w58_baseline.json", "w58_planted.json"):
        (RUNS / name).unlink(missing_ok=True)
    print(text, end="")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
