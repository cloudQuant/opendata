r"""Scan the exact payload a wave stages, before the commit exists.

``make secret-check`` runs ``gitleaks detect --source .``, which reads commit history. It therefore
cannot see a file that is staged but not yet committed -- which is precisely when a
credential-shaped literal is still cheap to fix. Wave 23 found one that way (the reordered
pricing census row), so this probe takes the file set a wave is about to add, copies the
working-tree bytes into one throwaway tree, and scans that tree with the shipped
``.gitleaks.toml`` and no allowlist additions.

Run it twice over the same list: once before the scan's own face line is appended to the carrier and
once after. Equal counts prove the appended line is inert text rather than a new finding, which is
the one self-reference this kind of instrument cannot avoid.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
CONFIG = REPO_ROOT / ".gitleaks.toml"


def _collect(paths: list[str]) -> tuple[list[Path], list[str]]:
    """Expand the staged list into files, skipping only caches and interpreter by-products.

    A path that resolves to neither a file nor a directory is returned as unresolved instead of
    being dropped: a typo, a moved artifact, or a non-ASCII argument mangled by the caller's shell
    would otherwise shrink the scanned set while still printing ``findings=0``.
    """
    files: list[Path] = []
    unresolved: list[str] = []
    for raw in paths:
        candidate = REPO_ROOT / raw
        if candidate.is_dir():
            files.extend(
                path
                for path in sorted(candidate.rglob("*"))
                if path.is_file() and "__pycache__" not in path.parts
            )
        elif candidate.is_file():
            files.append(candidate)
        else:
            unresolved.append(raw)
    return files, unresolved


def _scan(files: list[Path], report: Path, binary: str) -> tuple[int, int, int]:
    """Copy one file set into a throwaway tree and scan it, returning counts and the exit code."""
    stage = Path(tempfile.mkdtemp(prefix="wave-scan-"))
    try:
        for path in files:
            target = stage / path.relative_to(REPO_ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        result = subprocess.run(  # noqa: S603  # nosec B603  # fixed argv, shell disabled
            [
                binary,
                "detect",
                "--no-git",
                "--source",
                str(stage),
                "--config",
                str(CONFIG),
                "--redact",
                "--report-format",
                "json",
                "--report-path",
                str(report),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if not report.exists():
            return len(files), -1, result.returncode
        findings = json.loads(report.read_text(encoding="utf-8"))
        return len(files), len(findings), result.returncode
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    """Print the one-line face the carrier quotes; exit non-zero only if gitleaks itself failed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", nargs="+", required=True, help="repo-relative payload paths")
    args = parser.parse_args(argv)
    binary = shutil.which("gitleaks")
    if binary is None:
        print("payload_scan=SKIPPED reason=gitleaks-not-on-path")
        return 0
    files, unresolved = _collect(args.paths)
    if unresolved:
        for raw in unresolved:
            print(f"payload_scan_unresolved_path={raw}", file=sys.stdout)
    if not files:
        print("payload_scan=FAIL reason=no payload files resolved from the given paths")
        return 1
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        report = Path(handle.name)
    try:
        count, findings, code = _scan(files, report, binary)
    finally:
        report.unlink(missing_ok=True)
    print(
        f"payload_scan_files={count} payload_scan_roots={len(args.paths)} "
        f"payload_scan_unresolved={len(unresolved)} "
        f"payload_scan_findings={findings} payload_scan_rc={code}",
        file=sys.stdout,
    )
    ok = findings == 0 and code == 0 and not unresolved
    if ok:
        print(f"payload_scan=PASS {count} file(s) clean")
    elif unresolved:
        print(f"payload_scan=FAIL unresolved={len(unresolved)} scanned set is short")
    else:
        print(f"payload_scan=FAIL findings={findings} rc={code}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
