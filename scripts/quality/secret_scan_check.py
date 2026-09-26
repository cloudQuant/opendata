#!/usr/bin/env python3
"""Credential-scan gate (验收 AC-16「凭证不在版本库中（含全历史扫描）」).

Why this file exists
--------------------
That scan lived only in ``.github/workflows/ci.yml``, pinned to a gitleaks release,
while ``make gate`` never ran it. Five pushes in a row were red on that job and every
local gate run in the same window was green. Worse, CI pinned 8.21.2 while this
machine has 8.30.1, so the two readings did not even describe the same rule set (CI:
3 findings, local: 5) and neither one told the other what it meant. A face only CI
runs is a face nobody is watching - the same shape of defect C29/C30/C35 closed on the
frontend and static planes.

What this target owns
---------------------
* The scanner version comes from ``docs/quality/secret-scan.json`` and CI installs it
  from there instead of hardcoding a release: one source of truth, drift impossible.
* The scanner found on PATH must report exactly that version, or the run fails as
  *drift* rather than printing a reading nothing else agrees with.
* A missing gitleaks is a failure, not a skip (contrast ``make deps-audit``, which
  echoes a hint and returns 0 - that is how a face goes dark).
* The scan itself is the pinned command, output passed through untrimmed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "docs" / "quality" / "secret-scan.json"
MANIFEST_VERSION = 1


@dataclass(frozen=True)
class Manifest:
    """The pinned credential-scan device."""

    gitleaks_version: str
    scan_argv: tuple[str, ...]
    ci_workflow: Path
    release_url_template: str


def load_manifest() -> Manifest:
    """Read and validate the pinned device description."""
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if raw.get("version") != MANIFEST_VERSION:
        raise ValueError(f"manifest version must be {MANIFEST_VERSION}, got {raw.get('version')!r}")
    version = raw.get("gitleaks_version")
    argv = raw.get("scan_argv")
    workflow = raw.get("ci_workflow")
    template = raw.get("release_url_template")
    if not (isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version)):
        raise ValueError(f"{MANIFEST_PATH.name}: `gitleaks_version` is not an x.y.z string")
    if not (isinstance(argv, list) and argv and all(isinstance(x, str) and x for x in argv)):
        raise ValueError(f"{MANIFEST_PATH.name}: `scan_argv` must be a non-empty list of strings")
    if not (isinstance(workflow, str) and workflow):
        raise ValueError(f"{MANIFEST_PATH.name}: `ci_workflow` must be a path string")
    if not (isinstance(template, str) and "{version}" in template):
        raise ValueError(f"{MANIFEST_PATH.name}: `release_url_template` must contain {{version}}")
    return Manifest(version, tuple(argv), Path(workflow), template)


def local_tool_version(manifest: Manifest) -> str | None:
    """Version the installed scanner reports, or ``None`` when there is no scanner."""
    tool = manifest.scan_argv[0]
    if shutil.which(tool) is None:
        return None
    result = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
        [tool, "version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"\d+\.\d+\.\d+", result.stdout + result.stderr)
    return match.group(0) if match else None


def check_ci_wiring(manifest: Manifest) -> list[str]:
    """Fail if CI could run a different device than this target does."""
    problems: list[str] = []
    path = REPO_ROOT / manifest.ci_workflow
    if not path.is_file():
        return [f"CI workflow {manifest.ci_workflow} is gone; the manifest still points at it"]
    text = path.read_text(encoding="utf-8")
    if MANIFEST_PATH.name not in text:
        problems.append(
            f"{manifest.ci_workflow} must read {MANIFEST_PATH.relative_to(REPO_ROOT)} to pick the "
            "scanner version, instead of pinning a release of its own"
        )
    hardcoded = sorted(set(re.findall(r"gitleaks_(\d+\.\d+\.\d+)_linux", text)))
    if hardcoded:
        problems.append(f"{manifest.ci_workflow} hardcodes scanner release(s) {hardcoded}")
    if "make secret-check" not in text:
        problems.append(f"{manifest.ci_workflow} does not run `make secret-check`")
    if "fetch-depth: 0" not in text:
        problems.append(f"{manifest.ci_workflow} has no full history, so the scan is shallow")
    return problems


def main() -> int:
    """Run the pinned scan; return the process exit code."""
    try:
        manifest = load_manifest()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: unusable device manifest: {exc}", file=sys.stderr)
        return 1

    problems = check_ci_wiring(manifest)
    tool = manifest.scan_argv[0]
    found = local_tool_version(manifest)
    if found is None:
        problems.append(
            f"`{tool}` is not on PATH. Install "
            f"{manifest.release_url_template.format(version=manifest.gitleaks_version)} "
            "- a skipped credential scan is not a passed one."
        )
    elif found != manifest.gitleaks_version:
        problems.append(
            f"scanner drift: PATH reports {found}, "
            f"docs/quality/secret-scan.json pins {manifest.gitleaks_version}"
        )
    if problems:
        print("FAIL: credential-scan device is not the pinned one:", file=sys.stderr)
        for line in problems:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print(f"device: {tool} {found}, command: {' '.join(manifest.scan_argv)}")
    result = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
        list(manifest.scan_argv),
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"FAIL: the history scan reports credentials (exit {result.returncode}). "
            "Triage each finding; only a public identifier belongs in the allowlist.",
            file=sys.stderr,
        )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
