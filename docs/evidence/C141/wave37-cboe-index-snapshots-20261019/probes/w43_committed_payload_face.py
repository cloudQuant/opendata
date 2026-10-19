"""Post-commit payload face for wave 37's landing commit (take 3).

Two readings, each with its own carrier, plus the controls that make each zero a measurement:

* **per-path byte reconciliation** over the files the landing commit carries:
  ``git show <sha>:<path>`` compared to the worktree bytes in binary mode. Its falsifiable twin
  is the same comparison against the commit's *parent*, which must read non-zero drift -- this
  wave really moved those files, and a comparer that can only ever print 0 measures nothing.
* **one gitleaks run over exactly that commit** (``gitleaks git --log-opts=-1``, shipped config),
  a dir-mode run over exactly the two evidence bundles' committed blobs (not the worktree copies,
  which carry local ``__pycache__`` byte-code), and the repo's own
  ``secret_scan_check`` gate. A ``findings=0`` from any of the three is only published beside the
  arm that moves it: a scratch repo carrying one fabricated token is scanned in git mode with the
  same argv, and the bundle copy is scanned in dir mode with the same config, so "the scanner is
  wired" is read off a non-zero counterfact rather than asserted.

Nothing is written inside the repository except the carriers under this bundle's ``runs/``; the
planted objects live in ``/tmp`` and the fabricated token is assembled at runtime, never printed.
"""

from __future__ import annotations

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
SHA = "b2e7421e7"
BUNDLE = "docs/evidence/C141/wave37-cboe-index-snapshots-20261019"
RUNS = REPO / BUNDLE / "runs"
SCAN_CARRIER = RUNS / "w41_staged_credential_scan.take2.txt"
SCRATCH = Path("/tmp/w43_payload_face")  # nosec B108  # noqa: S108
SCAN_CHECK = "scripts/quality/secret_scan_check.py"


def _plant_token() -> str:
    """Fabricated and unused; assembled per call so no rule reads it off a literal."""
    return "ghp_" + ("0123456789" * 3 + "abcdefghij")


def git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run one literal git argv."""
    return subprocess.run(  # nosec B603  # noqa: S603
        [GIT, *args], cwd=REPO, capture_output=True, text=True, check=False
    )


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


def _commit_paths(rev: str) -> list[str]:
    proc = git("show", "--name-only", "--format=", rev)
    if proc.returncode != 0:
        raise SystemExit(f"git show {rev} failed rc={proc.returncode}")
    return sorted({line for line in proc.stdout.splitlines() if line.strip()})


def _reconcile() -> dict[str, Any]:
    """Compare committed bytes to worktree bytes, and to the parent commit for the drift twin."""
    parent = git("rev-parse", f"{SHA}^").stdout.strip()
    paths = _commit_paths(SHA)
    drift_now: list[str] = []
    drift_parent: list[str] = []
    missing_worktree: list[str] = []
    for rel in paths:
        committed = blob_or_none(SHA, rel)
        worktree = REPO / rel
        if committed is None:
            missing_worktree.append(rel)
            continue
        if not worktree.is_file() or worktree.read_bytes() != committed:
            drift_now.append(rel)
        if blob_or_none(parent, rel) != committed:
            drift_parent.append(rel)
    return {
        "commit": SHA,
        "parent": parent,
        "commit_paths": len(paths),
        "worktree_drift_vs_commit": len(drift_now),
        "drift_list": drift_now[:8],
        "moved_vs_parent": len(drift_parent),
        "missing_in_commit": len(missing_worktree),
        "commit_paths_by_root": _root_counts(paths),
    }


def _root_counts(paths: list[str]) -> dict[str, int]:
    buckets: dict[str, int] = {}
    for rel in paths:
        parts = rel.split("/")
        head = "/".join(parts[:3]) if parts[0] == "docs" and len(parts) > 2 else parts[0]
        buckets[head] = buckets.get(head, 0) + 1
    return dict(sorted(buckets.items(), key=lambda item: -item[1])[:12])


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


def _plant_scratch_repo(report: Path) -> tuple[int, int]:
    """One fabricated token in a scratch git repo, scanned with the same git-mode argv.

    gitleaks resolves the repository from the working directory, so the scan must run inside the
    scratch repo; scanning from ``REPO`` would silently re-measure the real commit.
    """
    shutil.rmtree(SCRATCH / "planted", ignore_errors=True)
    repo = SCRATCH / "planted"
    repo.mkdir(parents=True)
    token = _plant_token()
    (repo / "carrier_like.txt").write_text(
        f'# fabricated control, never a real credential\napi_token = "{token}"\n', encoding="utf-8"
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
    rc, count, _rules = _gitleaks(
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
        cwd=repo,
    )
    return rc, count


def _copy_committed(dest: Path, prefixes: tuple[str, ...]) -> tuple[int, list[str]]:
    """Materialize only the paths the landing commit carries, from its own blobs.

    Take 1 copied the worktree directories and read 2 ``github-pat`` findings inside
    ``probes/__pycache__/*.pyc`` -- local byte-code that ``.gitignore`` keeps out of the commit.
    CPython constant-folds ``"ghp_" + ("0123456789" * 3 + "abcdefghij")`` into the module's
    ``co_consts``, so a probe that assembles a fake token writes that token as a literal beside
    itself. The claim here is about committed bytes, so the scan tree is built from
    ``git ls-tree`` of the commit and the skipped local-only files are counted and named.
    """
    listed = git("ls-tree", "-r", "--name-only", SHA)
    if listed.returncode != 0:
        raise SystemExit(f"git ls-tree {SHA} failed rc={listed.returncode}")
    paths = [line for line in listed.stdout.splitlines() if line.startswith(prefixes)]
    copied = 0
    for rel in paths:
        data = blob_or_none(SHA, rel)
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


def main() -> int:
    """Reconcile the commit's bytes, then scan the commit and this wave's bundles with controls."""
    RUNS.mkdir(parents=True, exist_ok=True)
    face = _reconcile()
    header = [
        f"argv: {PY} {REPO / BUNDLE / 'probes' / 'w43_committed_payload_face.py'}",
        f"commit={SHA} parent={face['parent']}",
    ]
    lines = [
        "reconcile "
        + " ".join(
            f"{key}={face[key]}"
            for key in (
                "commit_paths",
                "worktree_drift_vs_commit",
                "moved_vs_parent",
                "missing_in_commit",
            )
        ),
        f"drift_list={face['drift_list']}",
        f"commit_paths_by_root={face['commit_paths_by_root']}",
    ]

    scan_lines: list[str] = []
    staged_from_carrier = 0
    for line in SCAN_CARRIER.read_text(encoding="utf-8").splitlines():
        if line.startswith("staged_files="):
            staged_from_carrier = int(line.split("=", 1)[1].split()[0])
        if line.startswith("payload_index_sha256="):
            scan_lines.append(f"pre_commit_gate {line}")
    commit_paths = face["commit_paths"]
    scan_lines.append(
        f"staged_files_from_scan_carrier={staged_from_carrier} committed_paths={commit_paths} "
        "gate_scope_note=the pre-commit gate read the index while its own terminal carrier "
        "was still unstaged, so this face is the arm that scans the commit"
    )

    arms: dict[str, bool] = {
        "worktree_matches_commit": face["worktree_drift_vs_commit"] == 0
        and face["missing_in_commit"] == 0,
        "drift_twin_moves": face["moved_vs_parent"] > 0,
        "gate_scope_recorded": staged_from_carrier > 0,
        # ``--log-opts=-1`` reads the tip, so the git-mode claim needs HEAD to BE this commit.
        "head_is_landing_commit": git("rev-parse", "HEAD").stdout.strip().startswith(SHA),
    }

    SCRATCH.mkdir(parents=True, exist_ok=True)
    git_rc, git_n, git_rules = _gitleaks(
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
        SCRATCH / "git_mode.json",
    )
    plant_rc, plant_n = _plant_scratch_repo(SCRATCH / "planted_git.json")
    scan_lines.append(
        f"gitleaks_git_mode commit={SHA} rc={git_rc} findings={git_n} rules={git_rules} "
        f"planted_control rc={plant_rc} findings={plant_n} arm_can_move={plant_n > git_n}"
    )
    arms["git_mode_clean_and_armed"] = git_rc == 0 and git_n == 0 and plant_n > git_n

    bundle_tree = SCRATCH / "bundles"
    copied, skipped = _copy_committed(bundle_tree, ("docs/evidence/C140", "docs/evidence/C141"))
    dir_rc, dir_n, dir_rules = _gitleaks(
        [
            GITLEAKS,
            "dir",
            str(bundle_tree),
            "--config",
            str(REPO / ".gitleaks.toml"),
            "--redact",
            "--no-banner",
            "--exit-code",
            "0",
        ],
        SCRATCH / "dir_mode.json",
    )
    (bundle_tree / "plant.txt").write_text(f'api_token = "{_plant_token()}"\n', encoding="utf-8")
    dirp_rc, dirp_n, _ = _gitleaks(
        [
            GITLEAKS,
            "dir",
            str(bundle_tree),
            "--config",
            str(REPO / ".gitleaks.toml"),
            "--redact",
            "--no-banner",
            "--exit-code",
            "0",
        ],
        SCRATCH / "dir_mode_planted.json",
    )
    shutil.rmtree(bundle_tree, ignore_errors=True)
    # The red in take 1 came from ``probes/__pycache__/*.pyc``, and those caches were then
    # deleted, so an arm still demanding bytecode would be graded on a state I removed myself.
    # What must hold is that the scan tree is narrower than the worktree and the difference is
    # named -- not that the original cause is still on disk.
    pycache_only = [name for name in skipped if "__pycache__" in name]
    local_only_sample_printed = skipped[:4]
    scan_lines.append(
        f"dir_mode_tree copied_from_commit={copied} local_only_files={len(skipped)} "
        f"local_only_sample={local_only_sample_printed} "
        f"bytecode_still_present={len(pycache_only)} (take 1 read 2 here)"
    )
    scan_lines.append(
        f"gitleaks_dir_mode bundles rc={dir_rc} findings={dir_n} rules={dir_rules} "
        f"planted rc={dirp_rc} findings={dirp_n} arm_can_move={dirp_n > dir_n}"
    )
    arms["dir_mode_clean_and_armed"] = dir_rc == 0 and dir_n == 0 and dirp_n > dir_n
    arms["local_only_files_named"] = len(skipped) > 0 and len(local_only_sample_printed) > 0

    gate = subprocess.run(  # nosec B603  # noqa: S603
        [PY, SCAN_CHECK],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    gate_tail = (gate.stdout + gate.stderr).strip().splitlines()[-3:]
    scan_lines.append(f"secret_scan_check rc={gate.returncode} tail={' | '.join(gate_tail)[:220]}")
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
        f"committed_payload_face commit={SHA} paths={commit_paths} arms={len(arms)} "
        f"arms_passed={sum(arms.values())} arms_failed={len(failed)} failed={failed} "
        f"PASS={not failed}"
    )
    exit_code = 0 if not failed else 1
    carrier("w43_committed_payload_face.take3", header, lines, exit_code)
    print("\n".join(lines))
    print(f"w43_committed_payload_face_exit={exit_code}")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
