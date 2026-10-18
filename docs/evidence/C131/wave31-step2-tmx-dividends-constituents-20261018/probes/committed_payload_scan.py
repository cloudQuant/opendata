r"""Scan the bytes a step actually committed, not the bytes it planned to commit.

``staged_payload_scan.py`` (the shipped C128 index-plane screen) takes the paths a wave is
*about* to add. That is the right instrument before a commit exists, and it is what step 2 ran
-- but it prices 86 paths while the commit carries 87, because a screen cannot include the carrier
that records it. The gap is real, and this driver closes it by measurement instead of by prose: it
re-derives the payload set from the commit itself with ``git diff-tree``, so the scanned list is the
set that is now in history.

Three things have to hold before a finding count means anything, and all three are printed:

* ``worktree_drift_vs_commit`` -- the shipped screen reads *working-tree* bytes, so "this scan saw
  the committed bytes" needs the tree to be unpolluted. The comparison is git's own
  (``git diff --name-only <commit>``) rather than a hand-parsed object listing, because the first
  draft of this gate did hand-parse one and turned a usage error into 87 fabricated findings.
* ``CONTROL_drift_vs_parent`` -- the same instrument aimed at the commit's parent must report
  exactly this commit's 87 paths as drifted. If it cannot, then the empty drift above is a
  comparison that compares nothing, and the screen is refused rather than given a clean zero.
* the double take -- the screen's own docstring asks to be run twice over one list, because that
  is the only way to show that appending the face line to a carrier did not itself create a finding.

Nothing here is stored that the screen did not print: the driver imports the shipped module by path
and calls its ``main()``, so the predicate, the allowlist-free single-copy tree and the ``findings``
count all stay the factory's.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import shutil
import subprocess  # nosec B404  # one run(): git with list argv, shell never enabled
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
SCREEN = (
    REPO_ROOT / "docs/evidence/C128/plane1-residue-readiness-20261017/probes/staged_payload_scan.py"
)
RUNS = Path(__file__).resolve().parent.parent / "runs"


def carrier_name(commit: str) -> str:
    """One carrier per scanned commit, named by that commit.

    A fixed filename would mean the re-take over the *back-fill* commit silently overwrote the face
    taken over the plane-1 commit, and §7.7/§8.1 of the wave's face cite the latter by name.
    """
    return f"wave31-step2-committed_payload_scan_{commit}.txt"


def _git(*args: str) -> str:
    git = shutil.which("git") or "git"
    proc = subprocess.run(  # noqa: S603  # nosec B603  # fixed argv, shell disabled
        [git, "-c", "core.quotepath=off", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"git_{args[0]}_exit={proc.returncode} {proc.stderr.strip()[:120]}")
    return proc.stdout


def commit_paths(commit: str) -> list[str]:
    """The exact path set the commit carries, read from history rather than from the index."""
    out = _git("diff-tree", "--no-commit-id", "--name-only", "-r", commit)
    return [line for line in out.splitlines() if line]


def worktree_drift(commit: str) -> list[str]:
    """Paths whose working-tree bytes differ from ``commit``, by git's own comparison.

    The first draft of this gate ran ``git hash-object --batch-check --stdin-paths`` and parsed
    ``<oid> <path>`` lines. That flag does not exist in this git, so the command died on usage, its
    stdout came back empty, and the driver reported 0/87 agreement -- a fabricated finding 87 paths
    long, produced by an instrument that ignored the subprocess exit code. Routing through ``_git``,
    which raises on a non-zero exit, is what makes an unknown flag impossible to read as a number.
    """
    return [line for line in _git("diff", "--name-only", commit).splitlines() if line]


def load_screen() -> Any:  # noqa: ANN401
    """Import the shipped screen by path so this driver cannot restate its predicate."""
    spec = importlib.util.spec_from_file_location("staged_payload_scan_screen", SCREEN)
    if spec is None or spec.loader is None:
        raise SystemExit(f"screen_unimportable={SCREEN}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def take(screen: Any, paths: list[str], label: str) -> list[str]:  # noqa: ANN401
    """Run the shipped ``main()`` in-process over the full list and keep every line it printed."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = screen.main(["--paths", *paths])
    text = buf.getvalue()
    return [f"{label}_exit={code} bytes={len(text.encode())}"] + text.splitlines()


def main(argv: list[str]) -> int:
    """Write the committed-payload face: the byte gate with its control, then the screen twice."""
    commit = argv[argv.index("--commit") + 1] if "--commit" in argv else "HEAD"
    paths = commit_paths(commit)
    drifted = worktree_drift(commit)
    parent_drift = worktree_drift(f"{commit}^")
    # The control is the same instrument over the commit's parent: if git cannot show this commit's
    # own 87 paths as a drift, then the empty drift above is a dead comparison and not a clean tree.
    control_bites = len(parent_drift) == len(paths)
    ok = not drifted and control_bites
    lines = [
        f"wave-31 step 2 -- index-plane screen re-taken over commit {commit}'s own payload",
        f"argv: {sys.executable} {Path(__file__).relative_to(REPO_ROOT)} --commit {commit}",
        "   (cwd = repo root, single process, no pipes, no xargs)",
        f"screen_shipped_at={SCREEN.relative_to(REPO_ROOT)}",
        f"commit_paths={len(paths)} head_commit={_git('rev-parse', '--short', 'HEAD').strip()}",
        f"worktree_drift_vs_commit={len(drifted)} byte_identical={not drifted}",
        f"CONTROL_drift_vs_parent={len(parent_drift)} expected={len(paths)} bites={control_bites} "
        "(git's own comparison, so this instrument cannot silently match nothing)",
    ]
    lines.extend(f"DRIFT {rel}" for rel in drifted[:20])
    if ok:
        screen = load_screen()
        lines += take(screen, paths, "take1")
        lines += take(screen, paths, "take2")
    else:
        lines.append(
            "screen_NOT_RUN: either the tree differs from the commit (DRIFT lines above) or the "
            "control did not bite, so a clean finding count here would describe bytes that are not "
            "in history or a comparison that compares nothing"
        )
    lines.append(f"chain_committed_payload_scan_exit={0 if ok else 1}")
    carrier = RUNS / carrier_name(_git("rev-parse", "--short", commit).strip())
    RUNS.mkdir(parents=True, exist_ok=True)
    carrier.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    size = carrier.stat().st_size
    print(f"carrier_written bytes={size} path={carrier.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
