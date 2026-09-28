#!/usr/bin/env python3
"""C60 phase 2: the A2 touch-and-comply face of the six datetime.UTC files.

Phase 1 (``run_c60.py`` -> ``datetime-utc-cleanup.txt``) drove the A1 mypy plane
from 6 errors to 0. That edit *moved* the six files onto the A2 plane, and A2 is
zero tolerance: ``make a2-check`` went red with 68 ruff findings and 2 bandit
findings - exactly the debt C54 handed back (README lines 228-235). This runner
measures the second half: the six files cleaned to A2 standard and the whole A2
plane green again, plus three breaks. B3 is the finding that came out of a wrong
hypothesis: reverting a cleanup does not merely silence one checker, it removes
the file from the A2 plane entirely (the plane is defined by "changed"), and the
A1 ratchet is then the only member that still sees the debt.

The warehouse is never touched: this runner runs no e2e leg, every pytest call
carries ``-m "not e2e"``, and both MySQL hosts are forced to a refused port.
The patches in section 7 write real files and restore them from in-memory
originals after every single break, verified by sha256. Do not run this at the
same time as ``make gate``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PY = sys.executable
GIT = shutil.which("git")

#: Refused ports, so even a released live leg could not reach the warehouse.
CONTAINMENT = {
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_PORT": "1",
    "DATA_MYSQL_HOST": "127.0.0.1",
    "DATA_MYSQL_PORT": "1",
}

SIX = [
    "opendata/api/tasks.py",
    "opendata/api/auth.py",
    "opendata/services/execution_service.py",
    "opendata/services/notification_service.py",
    "opendata/services/retry_service.py",
    "opendata/services/scheduler_service.py",
]

EXEC_REL = "opendata/services/execution_service.py"
AUTH_REL = "opendata/api/auth.py"

# ---- anchors: each must match exactly once in its file -----------------------
D415_CLEAN = '    """执行监控服务."""'
D415_DIRTY = '    """执行监控服务"""'

S105_CLEAN = 'DEFAULT_PASSWORD = "admin123"  # noqa: S105  # nosec B105'
S105_DIRTY = (
    "# Detection of accounts still on the shipped default; it is not a credential.\n"
    'DEFAULT_PASSWORD = "admin123"\n'
)

IGNORE_ABSENT = '"opendata/services/execution_service.py" = ["D"'

PYTEST_SUBSET = [
    "tests",
    "-q",
    "--no-cov",
    "-m",
    "not e2e",
    "-p",
    "no:cacheprovider",
    "-k",
    "execution or scheduler or retry or notification or auth or task",
]


def sha(path: Path) -> str:
    """sha256 of a file, first 12 hex digits."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def section(title: str) -> None:
    """Print an untrimmed-log section banner."""
    print(f"\n==== {title} ====", flush=True)


def run(label: str, argv: list[str]) -> tuple[int, str]:
    """Run a literal argv, echo command plus untrimmed output, return (rc, out)."""
    print(f"$ {label}: {' '.join(argv)}", flush=True)
    proc = subprocess.run(  # noqa: S603  # nosec B603  # fixed argv
        argv,
        cwd=str(REPO),
        capture_output=True,
        text=True,
        shell=False,
        check=False,
        env={**dict(os.environ), **CONTAINMENT},
    )
    out = proc.stdout + proc.stderr
    print(out.rstrip("\n"), flush=True)
    print(f"[exit={proc.returncode}]", flush=True)
    return proc.returncode, out


def patch(rel: str, old: str, new: str) -> None:
    """Replace one anchor in one file; refuse to proceed unless it is unique."""
    path = REPO / rel
    text = path.read_text(encoding="utf-8")
    hits = text.count(old)
    if hits != 1:
        raise SystemExit(f"anchor not unique in {rel}: {hits} matches -> {old[:40]!r}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def a2_face() -> tuple[int, str]:
    """The whole A2 plane: ruff + format + mypy + bandit over the A2 file set."""
    return run("a2-check", [PY, "scripts/quality/a2_check.py"])


def git_show(rel: str) -> str:
    """HEAD text of a tracked file."""
    if GIT is None:
        raise SystemExit("git executable not found")
    proc = subprocess.run(  # noqa: S603  # nosec B603  # fixed argv
        [GIT, "show", f"HEAD:{rel}"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        shell=False,
        check=True,
    )
    return proc.stdout


def main() -> int:
    """Measure the A2 face, break it three ways, restore, re-verify."""
    watched = [REPO / EXEC_REL, REPO / AUTH_REL, REPO / "pyproject.toml"]
    original = {path: path.read_bytes() for path in watched}
    original_sha = {path: sha(path) for path in watched}
    exec_head = git_show(EXEC_REL)
    verdicts: list[str] = []

    def restore_all() -> bool:
        """Write every watched file back from memory and prove nothing drifted."""
        for path, blob in original.items():
            path.write_bytes(blob)
        restored = {path: sha(path) for path in watched}
        for path in watched:
            before, after = original_sha[path], restored[path]
            same = "identical" if before == after else "DRIFT"
            name = str(path.relative_to(REPO))
            print(f"  restore {name}: {before} -> {after} {same}", flush=True)
        return restored == original_sha

    section("0. 运行出处（时钟与 HEAD 由本遍自己打印）")
    run("clock", ["/bin/date"])
    run("clock-utc", ["/bin/date", "-u"])
    if GIT is None:
        raise SystemExit("git executable not found")
    run("head", [GIT, "rev-parse", "--short", "HEAD"])
    run("branch", [GIT, "branch", "--show-current"])
    run("python", [PY, "-V"])
    print("ARCHIVE_ROUND=C60 PHASE2=A2-TOUCH-COMPLY", flush=True)

    section("1. 本轮第二段改动面（63 条机械修 + 7 条手改，diff 原样）")
    run("diff-stat", [GIT, "diff", "--stat", "--", *SIX, "docs/quality/ratchet.json"])
    run("diff-body", [GIT, "--no-pager", "diff", "--", *SIX, "docs/quality/ratchet.json"])

    section("2. 六个文件的达标面（ruff check / ruff format / bandit）")
    rc_ruff, out_ruff = run("ruff", [PY, "-m", "ruff", "check", "--output-format=concise", *SIX])
    run("format", [PY, "-m", "ruff", "format", "--check", *SIX])
    bandit_cmd = [PY, "-m", "bandit", "-c", "bandit.yaml", "-q", "-f", "custom", *SIX]
    rc_bandit, _ = run("bandit", bandit_cmd)
    verdicts.append(f"clean ruff rc={rc_ruff} passed={'All checks passed' in out_ruff}")
    verdicts.append(f"clean bandit rc={rc_bandit}")

    section("3. A1 类型面（mypy 全树，与第一段同一判据）")
    rc_mypy, out_mypy = run(
        "mypy", [PY, "-m", "mypy", "--no-color-output", "opendata/", "opendata_fuyao/"]
    )
    verdicts.append(f"clean mypy rc={rc_mypy} success={'Success' in out_mypy}")

    section("4. 全 A2 平面（六个新触文件已进入零容忍集合）")
    rc_a2, out_a2 = a2_face()
    n_files = next((ln for ln in out_a2.splitlines() if ln.startswith("A2 files:")), "n/a")
    print(f"READING A2 clean-tree rc={rc_a2}; {n_files}", flush=True)
    verdicts.append(f"clean a2-check rc={rc_a2}")

    section("5. 棘轮冻结后的读数（无 NOTE 即已冻结）")
    run("ratchet", [PY, "scripts/quality/ratchet.py"])

    section("6. 受影响用例（service/api 平面，带 not e2e 过滤，不跑仓库腿）")
    rc_tests, _ = run("tests", [PY, "-m", "pytest", *PYTEST_SUBSET])
    verdicts.append(f"behaviour pytest rc={rc_tests}")

    section("7. 三条反事实：新绿必须承重，撤掉改动能躲过 A2 平面但躲不过棘轮")

    print("\n---- B1 把一条 D415 放回去（docstring 尾部句号） (expect red: ruff) ----", flush=True)
    patch(EXEC_REL, D415_CLEAN, D415_DIRTY)
    rc_b1, out_b1 = a2_face()
    ok_b1 = rc_b1 != 0 and "FAIL ruff check" in out_b1 and "D415" in out_b1
    mark = "OK" if ok_b1 else "WRONG"
    print(f"expect=red a2_rc={rc_b1} bit_on_ruff={ok_b1} -> {mark}", flush=True)
    verdicts.append(f"B1 D415 back: {mark}")
    restore_all()

    print("\n---- B2 摘掉 S105 的 nosec（证明抑制是承重的） (expect red: bandit) ----", flush=True)
    patch(AUTH_REL, S105_CLEAN, S105_DIRTY)
    rc_b2, out_b2 = a2_face()
    ok_b2 = rc_b2 != 0 and "FAIL bandit" in out_b2 and "B105" in out_b2
    mark = "OK" if ok_b2 else "WRONG"
    print(f"expect=red a2_rc={rc_b2} bit_on_bandit={ok_b2} -> {mark}", flush=True)
    verdicts.append(f"B2 nosec removed: {mark}")
    restore_all()

    print(
        "\n---- B3 撤掉改动能不能躲过零容忍平面：execution_service 还原 HEAD 内容 "
        "(expect ruff 红 + a2 绿(文件离场) + 棘轮红) ----",
        flush=True,
    )
    (REPO / EXEC_REL).write_text(exec_head, encoding="utf-8")
    rc_dirty_file, out_dirty = run("ruff (restored file)", [PY, "-m", "ruff", "check", EXEC_REL])
    rc_b3, out_b3 = a2_face()
    b3_files = next((ln for ln in out_b3.splitlines() if ln.startswith("A2 files:")), "n/a")
    rc_b3_ratchet, out_b3_ratchet = run("ratchet (restored)", [PY, "scripts/quality/ratchet.py"])
    escaped = (
        rc_dirty_file != 0 and "D415" in out_dirty and rc_b3 == 0 and "A2 files: 397" in b3_files
    )
    caught = rc_b3_ratchet != 0 and "mypy_selfdev" in out_b3_ratchet and "FAIL" in out_b3_ratchet
    ok_b3 = escaped and caught
    mark = "OK" if ok_b3 else "WRONG"
    print(
        f"expect=ruff-red a2-green-with-file-absent ratchet-red :: "
        f"ruff_rc={rc_dirty_file} a2_rc={rc_b3} ({b3_files}) ratchet_rc={rc_b3_ratchet} "
        f"escaped_a2={escaped} caught_by_ratchet={caught} -> {mark}",
        flush=True,
    )
    verdicts.append(f"B3 revert-to-HEAD escape: {mark}")
    restore_all()

    section("8. 还原校验与终局")
    digests_ok = restore_all()
    run("ruff", [PY, "-m", "ruff", "check", *SIX])
    rc_a2_final, out_a2_final = a2_face()
    run("ratchet", [PY, "scripts/quality/ratchet.py"])
    leftover_mypy = "attr-defined" in out_a2_final
    ignore_left = IGNORE_ABSENT in (REPO / "pyproject.toml").read_text(encoding="utf-8")
    print(f"READING A2 restored rc={rc_a2_final}", flush=True)
    print(f"  'attr-defined' left in restored a2 output: {leftover_mypy}", flush=True)
    print(f"  per-file-ignore left in pyproject: {ignore_left}", flush=True)
    verdicts.append(f"restore digests_equal={digests_ok} a2_rc={rc_a2_final}")

    print("\n" + " / ".join(verdicts), flush=True)
    all_ok = (
        digests_ok
        and rc_a2 == 0
        and rc_a2_final == 0
        and rc_tests == 0
        and not leftover_mypy
        and not ignore_left
        and not any("WRONG" in item for item in verdicts)
    )
    print(f"VERDICT C60-PHASE2 all_ok={all_ok}", flush=True)
    print(f"C60_PHASE2_RUNNER_EXIT={0 if all_ok else 1}", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
