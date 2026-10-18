"""Post-commit payload face for wave 32's landing commit a47eedacb.

Two readings, each with its own carrier:

* per-file digest reconciliation: for every path the landing commit carries, compare
  ``git show <sha>:<path>`` against the worktree bytes, so the claim "the commit carries the bytes
  the pre-commit scan read" is a measurement rather than a restatement. Its falsifiable twin is a
  drift count computed against the *parent* commit, which must be non-zero (the wave really moved
  those files); a comparer that can only ever read 0 is not measuring anything.
* one gitleaks run over exactly that commit (``--log-opts=-1`` scoped to the new commit), so the
  payload that is now in history is scanned by the shipped config, plus the repo's own
  ``secret_scan_check`` gate.

Nothing is written except the two carriers under this bundle's ``runs/``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
from pathlib import Path

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
GIT = "/usr/bin/git"
GITLEAKS = "/opt/homebrew/bin/gitleaks"
PY = "/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python"
SHA = "a47eedacb"
RUNS = REPO / "docs/evidence/C137/wave32-yfinance-landing-20261018/runs"


def git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run one literal git argv."""
    return subprocess.run(  # nosec B603  # noqa: S603
        [GIT, *args], cwd=REPO, capture_output=True, text=True, check=False
    )


def blob(rev: str, path: str) -> bytes:
    """Read one commit's bytes for a path.

    Deliberately *not* text mode: the comparison partner is ``Path.read_bytes()``, and a decoded
    ``str`` against raw ``bytes`` is a non-comparison that always reports drift-free (or drift)
    regardless of the file.
    """
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [GIT, "show", f"{rev}:{path}"],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"git show {rev}:{path} failed rc={proc.returncode}")
    return proc.stdout


def blob_or_none(rev: str, path: str) -> bytes | None:
    """Bytes for a path at a revision, or None when that revision does not carry it."""
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [GIT, "show", f"{rev}:{path}"],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    return None if proc.returncode != 0 else proc.stdout


def carrier(name: str, header: list[str], body: list[str], exit_code: int) -> Path:
    """Write one carrier with its argv, its face lines and its own exit reading."""
    path = RUNS / f"{name}.txt"
    text = "\n".join(
        [
            f"# carrier for {name}",
            *[f"# {line}" for line in header],
            "",
            *body,
            "",
            f"{name}_exit={exit_code}",
            "",
        ]
    )
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    """Reconcile the landing payload at HEAD and scan exactly that commit."""
    paths = [p for p in git("diff", "--name-only", f"{SHA}^", SHA).stdout.splitlines() if p]
    drift_vs_commit = [p for p in paths if blob(SHA, p) != (REPO / p).read_bytes()]
    added_vs_parent = 0
    removed_at_parent = 0
    for p in paths:
        at_parent = blob_or_none(f"{SHA}^", p)
        if at_parent is None:
            added_vs_parent += 1
        elif at_parent != blob(SHA, p):
            removed_at_parent += 1
    payload_sha = hashlib.sha256(
        "".join(f"{p}:{hashlib.sha256(blob(SHA, p)).hexdigest()}\n" for p in paths).encode()
    ).hexdigest()
    face_lines = [
        f"commit_paths={len(paths)}",
        f"worktree_drift_vs_commit={len(drift_vs_commit)} list={drift_vs_commit[:5]}",
        f"byte_identical={not drift_vs_commit}",
        f"payload_sha256={payload_sha}",
        f"CONTROL_paths_absent_at_parent={added_vs_parent}",
        f"CONTROL_changed_vs_parent={removed_at_parent}",
        f"CONTROL_drift_vs_parent={added_vs_parent + removed_at_parent} "
        f"bites={added_vs_parent + removed_at_parent > 0}",
        "note: the pre-commit scan read 2228 index paths; this commit's own tree adds the two "
        "scan carriers written after it, so the reconciliation population is git's, not prose",
    ]
    carrier(
        "committed_payload_scan_a47eedacb",
        [
            f"argv: {GIT} diff --name-only {SHA}^ {SHA}",
            f"per-path: {GIT} show {SHA}:<path> in bytes mode vs read_bytes()",
        ],
        face_lines,
        0 if not drift_vs_commit else 1,
    )

    report = Path("/tmp/w32_head_gitleaks.json")  # nosec B108  # noqa: S108
    gl = subprocess.run(  # nosec B603  # noqa: S603
        [
            GITLEAKS,
            "git",
            "--log-opts=-1",
            "--redact",
            "--no-banner",
            "--exit-code",
            "0",
            "--config",
            str(REPO / ".gitleaks.toml"),
            "--report-format",
            "json",
            "--report-path",
            str(report),
            str(REPO),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    findings = report.read_text(encoding="utf-8").strip() if report.is_file() else ""
    n_findings = 0 if findings in ("", "[]") else len(json.loads(findings))
    rev = git("rev-list", "--count", "HEAD")
    sec = subprocess.run(  # nosec B603  # noqa: S603
        [PY, "scripts/quality/secret_scan_check.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    face2 = [
        f"argv: {GITLEAKS} git --log-opts=-1 --config .gitleaks.toml (repo {REPO})",
        f"gitleaks_rc={gl.returncode} findings={n_findings}",
        f"head_commit_count={rev.stdout.strip()} rev_list_rc={rev.returncode}",
        f"argv: {PY} scripts/quality/secret_scan_check.py",
        f"secret_scan_check_rc={sec.returncode}",
        f"secret_scan_stdout_tail={sec.stdout.strip()[-220:]}",
        f"secret_scan_stderr_tail={sec.stderr.strip()[-160:]}",
        "plant_control: the pre-commit scan's planted arm moved 0 -> 2 on the same payload shape "
        "(see runs/w32_staged_credential_scan.take2_final.txt), so a clean read here is not an "
        "unrun scanner",
    ]
    exit2 = 0 if (n_findings == 0 and gl.returncode == 0 and sec.returncode == 0) else 1
    carrier("post_commit_secret_check_a47eedacb", ["see face lines for both argvs"], face2, exit2)
    print("\n".join(face_lines))
    print("\n".join(face2))
    print(f"w32_post_commit_face_exit={0 if exit2 == 0 else 1}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
