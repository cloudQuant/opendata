"""Post-commit closure face for wave 38: byte reconciliation plus an armed payload scan.

Clone of wave 37's shipped ``w43_committed_payload_face.py`` with the commit taken from argv instead
of hardcoded, so the face cannot be pointed at a commit by a stale literal (the failure shape this
repo recorded as "an expected literal is a claim about the producer").

Three readings, each with the control that makes its zero meaningful:

* **byte reconciliation** -- for every path the landing commit carries, ``git show <sha>:<path>``
  against the worktree bytes in binary mode; the falsifiable twin is the same predicate against the
  commit's parent, which must read non-zero because the wave really moved those files.
* **git-mode gitleaks** over exactly that commit, plus a scratch repo carrying one fabricated token
  scanned with the same argv -- the planted arm is what proves the invocation can report anything.
* **dir-mode gitleaks** over the committed blobs of the three bundles this wave writes (built from
  ``git ls-tree`` of the commit, never from the worktree copy, because local ``__pycache__`` holds
  constant-folded copies of the plant token), plus the repo's own ``secret_scan_check`` gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
GIT = "/usr/bin/git"
GITLEAKS = "/opt/homebrew/bin/gitleaks"
PY = "/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python"
BUNDLE = "docs/evidence/C143/wave38-seeking-alpha-landing-20261019"
RUNS = REPO / BUNDLE / "runs"
SCAN_CARRIER = RUNS / "w41b_staged_credential_scan.txt"
SCRATCH = Path("/tmp/w47_closure")  # nosec B108  # noqa: S108
BUNDLE_PREFIXES = ("docs/evidence/C142", "docs/evidence/C143", "docs/evidence/C101")
SCAN_CHECK = "scripts/quality/secret_scan_check.py"


def git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run one literal git argv."""
    return subprocess.run(  # nosec B603  # noqa: S603
        [GIT, *args], cwd=REPO, capture_output=True, text=True, check=False
    )


def _plant_token() -> str:
    """Fabricated and unused; assembled per call so no rule reads it off a literal."""
    return "ghp_" + ("0123456789" * 3 + "abcdefghij")


def blob_or_none(rev: str, path: str) -> bytes | None:
    """Bytes for a path at a revision, or None when that revision does not carry it.

    Read without ``text=True`` on purpose: a translated newline would make the byte reconciliation
    compare the worktree against an object the commit does not carry.
    """
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [GIT, "show", f"{rev}:{path}"],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    return None if proc.returncode != 0 else proc.stdout


def commit_paths(rev: str) -> list[str]:
    """Every path one commit carries, sorted."""
    proc = git("show", "--name-only", "--format=", rev)
    if proc.returncode != 0:
        raise SystemExit(f"git show {rev} failed rc={proc.returncode}")
    return sorted({line for line in proc.stdout.splitlines() if line.strip()})


def _reconcile(rev: str) -> dict[str, Any]:
    """Compare committed bytes with the worktree, and with the parent for the drift twin."""
    parent = git("rev-parse", f"{rev}^").stdout.strip()
    paths = commit_paths(rev)
    drift: list[str] = []
    moved: list[str] = []
    missing: list[str] = []
    for rel in paths:
        committed = blob_or_none(rev, rel)
        worktree = REPO / rel
        if committed is None:
            missing.append(rel)
            continue
        if not worktree.is_file() or worktree.read_bytes() != committed:
            drift.append(rel)
        if blob_or_none(parent, rel) != committed:
            moved.append(rel)
    buckets: dict[str, int] = {}
    for rel in paths:
        parts = rel.split("/")
        head = "/".join(parts[:3]) if parts[0] == "docs" and len(parts) > 2 else parts[0]
        buckets[head] = buckets.get(head, 0) + 1
    return {
        "parent": parent,
        "commit_paths": len(paths),
        "worktree_drift_vs_commit": len(drift),
        "drift_list": drift[:8],
        "moved_vs_parent": len(moved),
        "missing_in_commit": len(missing),
        "buckets": dict(sorted(buckets.items(), key=lambda item: -item[1])[:12]),
    }


def _gitleaks(
    argv: list[str], report: Path, *, cwd: Path | None = None
) -> tuple[int, int, list[str]]:
    proc = subprocess.run(  # nosec B603  # noqa: S603
        argv + ["--report-format", "json", "--report-path", str(report)],
        cwd=cwd or REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if not report.is_file():
        return proc.returncode, -1, []
    try:
        findings = json.loads(report.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError:
        return proc.returncode, -1, []
    if not isinstance(findings, list):
        return proc.returncode, -1, []
    return proc.returncode, len(findings), [str(f.get("RuleID", "?")) for f in findings]


def _git_scan(report: Path, *, cwd: Path | None = None) -> tuple[int, int, list[str]]:
    return _gitleaks(
        [
            GITLEAKS,
            "git",
            "--log-opts=-1",
            "--config",
            str(REPO / ".gitleaks.toml"),
            "--redact",
            "--no-banner",
            "--exit-code",
            "0",
        ],
        report,
        cwd=cwd,
    )


def _plant_scratch_repo(report: Path) -> tuple[int, int]:
    """One fabricated token in a scratch git repo, scanned with the same git-mode argv."""
    repo = SCRATCH / "planted"
    shutil.rmtree(repo, ignore_errors=True)
    repo.mkdir(parents=True)
    (repo / "carrier_like.txt").write_text(
        f'# fabricated control, never a real credential\napi_token = "{_plant_token()}"\n',
        encoding="utf-8",
    )
    for argv in (
        [GIT, "init", "-q", str(repo)],
        [GIT, "-C", str(repo), "add", "-A"],
        [
            GIT,
            "-C",
            str(repo),
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "user.name=control",
            "commit",
            "-q",
            "-m",
            "planted control",
        ],
    ):
        subprocess.run(  # nosec B603  # noqa: S603
            argv, capture_output=True, text=True, check=False
        )
    return _git_scan(report, cwd=repo)[:2]


def _copy_committed(dest: Path, rev: str, prefixes: tuple[str, ...]) -> tuple[int, list[str]]:
    """Materialize the commit's own blobs for the given prefixes; name what was skipped."""
    paths = [rel for rel in commit_paths(rev) if rel.startswith(prefixes)]
    copied = 0
    for rel in paths:
        data = blob_or_none(rev, rel)
        if data is None:
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        copied += 1
    committed = set(paths)
    local = {
        str(path.relative_to(REPO))
        for prefix in prefixes
        for path in (REPO / prefix).rglob("*")
        if path.is_file()
    }
    return copied, sorted(local - committed)


def main(argv: list[str] | None = None) -> int:
    """Reconcile the commit's bytes, then scan the commit and this wave's bundles with controls."""
    parser = argparse.ArgumentParser(description="wave-38 post-commit closure face")
    parser.add_argument("--commit", default="", help="commit to scan; defaults to HEAD")
    args = parser.parse_args(argv)
    rev = args.commit or git("rev-parse", "HEAD").stdout.strip()
    RUNS.mkdir(parents=True, exist_ok=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    face = _reconcile(rev)
    lines = [
        f"reconcile commit={rev[:9]} parent={face['parent'][:9]} "
        f"commit_paths={face['commit_paths']} "
        f"worktree_drift_vs_commit={face['worktree_drift_vs_commit']} "
        f"moved_vs_parent={face['moved_vs_parent']} missing_in_commit={face['missing_in_commit']}",
        f"drift_list={face['drift_list']}",
        f"commit_paths_by_root={face['buckets']}",
    ]
    head = git("rev-parse", "HEAD").stdout.strip()
    arms: dict[str, bool] = {
        "worktree_matches_commit": face["worktree_drift_vs_commit"] == 0
        and face["missing_in_commit"] == 0,
        "drift_twin_moves": face["moved_vs_parent"] > 0,
        "subject_is_tip": head.startswith(rev) or rev.startswith(head),
    }
    scan_lines: list[str] = []
    staged = 0
    for line in SCAN_CARRIER.read_text(encoding="utf-8").splitlines():
        if line.startswith("staged_files="):
            staged = int(line.split("=", 1)[1].split()[0])
        if line.startswith("payload_index_sha256="):
            scan_lines.append(f"pre_commit_gate {line}")
    scan_lines.append(
        f"staged_files_from_scan_carrier={staged} committed_paths={face['commit_paths']} "
        "gate_scope_note=the pre-commit gate read the index before its own carrier was staged, "
        "so this face is the arm that scans the commit"
    )
    arms["gate_scope_recorded"] = staged > 0

    git_rc, git_n, git_rules = _git_scan(SCRATCH / "git_mode.json")
    plant_rc, plant_n = _plant_scratch_repo(SCRATCH / "planted_git.json")
    scan_lines.append(
        f"gitleaks_git_mode commit={rev[:9]} rc={git_rc} findings={git_n} rules={git_rules} "
        f"planted_control rc={plant_rc} findings={plant_n} arm_can_move={plant_n > git_n}"
    )
    arms["git_mode_clean_and_armed"] = git_rc == 0 and git_n == 0 and plant_n > git_n

    tree = SCRATCH / "bundles"
    shutil.rmtree(tree, ignore_errors=True)
    copied, skipped = _copy_committed(tree, rev, BUNDLE_PREFIXES)
    dir_rc, dir_n, dir_rules = _gitleaks(
        [
            GITLEAKS,
            "dir",
            str(tree),
            "--config",
            str(REPO / ".gitleaks.toml"),
            "--redact",
            "--no-banner",
            "--exit-code",
            "0",
        ],
        SCRATCH / "dir_mode.json",
    )
    (tree / "plant.txt").write_text(f'api_token = "{_plant_token()}"\n', encoding="utf-8")
    dirp_rc, dirp_n, _ = _gitleaks(
        [
            GITLEAKS,
            "dir",
            str(tree),
            "--config",
            str(REPO / ".gitleaks.toml"),
            "--redact",
            "--no-banner",
            "--exit-code",
            "0",
        ],
        SCRATCH / "dir_mode_planted.json",
    )
    pycache = [name for name in skipped if "__pycache__" in name]
    shutil.rmtree(tree, ignore_errors=True)
    scan_lines.append(
        f"dir_mode_tree copied_from_commit={copied} local_only_files={len(skipped)} "
        f"local_only_sample={skipped[:4]} bytecode_skipped={len(pycache)}"
    )
    scan_lines.append(
        f"gitleaks_dir_mode bundles rc={dir_rc} findings={dir_n} rules={dir_rules} "
        f"planted rc={dirp_rc} findings={dirp_n} arm_can_move={dirp_n > dir_n}"
    )
    arms["dir_mode_clean_and_armed"] = dir_rc == 0 and dir_n == 0 and dirp_n > dir_n
    arms["local_only_files_named"] = len(skipped) > 0

    gate = subprocess.run(  # nosec B603  # noqa: S603
        [PY, SCAN_CHECK],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    tail = (gate.stdout + gate.stderr).strip().splitlines()[-3:]
    scan_lines.append(f"secret_scan_check rc={gate.returncode} tail={' | '.join(tail)[:220]}")
    arms["ship_gate_exit_zero"] = gate.returncode == 0
    claims_sha = hashlib.sha256(
        (REPO / "docs/evidence/C101/claims.merged.json").read_bytes()
    ).hexdigest()[:16]
    judge_sha = hashlib.sha256(
        (REPO / "docs/evidence/C101/judge/claims.merged.report.json").read_bytes()
    ).hexdigest()[:16]
    scan_lines.append(f"generation claims_sha256={claims_sha} judge_report_sha256={judge_sha}")

    failed = sorted(name for name, ok in arms.items() if not ok)
    lines.append("")
    lines.append("---- payload scan ----")
    lines.extend(scan_lines)
    lines.append("")
    lines.append(
        f"closure_face commit={rev[:9]} paths={face['commit_paths']} arms={len(arms)} "
        f"arms_passed={sum(arms.values())} arms_failed={len(failed)} failed={failed} "
        f"PASS={not failed}"
    )
    exit_code = 0 if not failed else 1
    name = "w47_closure_face"
    text = "\n".join(lines) + "\n"
    (RUNS / f"{name}.txt").write_text(
        "# carrier for w47_closure_face.py\n"
        f"# argv: {PY} {REPO / BUNDLE / 'probes' / 'w47_closure_face.py'}"
        f"{' --commit ' + args.commit if args.commit else ''}\n\n"
        + text
        + f"\n{name}_exit={exit_code}\n",
        encoding="utf-8",
    )
    print(text, end="")
    print(f"{name}_exit={exit_code}")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
