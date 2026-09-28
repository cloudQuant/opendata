#!/usr/bin/env python
"""C60 runner: archive the datetime.UTC cleanup face and break it three ways.

Read-only toward the warehouse: the pytest leg here is the service/API plane and
carries ``-m "not e2e"``, so no live e2e leg can run from this file. Usage::

    python docs/evidence/C60/run_c60.py > 正文      # then prepend the header

Every patched file is restored byte-for-byte and verified by sha256 before exit.

Body sections, in order: the change itself, the after-face (mypy / ruff / format /
ratchet), the affected tests, three counterfacts, the restore check.
"""

import hashlib
import subprocess  # nosec B404
import sys
from pathlib import Path

#: this file sits at ``<repo>/docs/evidence/C60/``.
REPO = Path(__file__).resolve().parents[3]
PY = sys.executable
DEBT_FILES = [
    "opendata/api/tasks.py",
    "opendata/api/auth.py",
    "opendata/services/execution_service.py",
    "opendata/services/notification_service.py",
    "opendata/services/retry_service.py",
    "opendata/services/scheduler_service.py",
]

TASKS_IMPORT_OLD = "from datetime import datetime, timezone"
TASKS_IMPORT_NEW = "from datetime import UTC, datetime"
TASKS_USE_OLD = "    task.updated_at = datetime.now(timezone.utc)"
TASKS_USE_NEW = "    task.updated_at = datetime.now(UTC)"
JOB_DEF_OLD = "    def _job_to_dict(self, job: Any) -> dict:"
JOB_DEF_NEW = "    def _job_to_dict(self, job) -> dict:"
PYVER_OLD = 'python_version = "3.10"'
PYVER_NEW = 'python_version = "3.13"'

#: label, (file, old, new) breaks, expected reading, the needle that proves it.
#: ``green`` on B3 is the point of that break: widening ``python_version`` also
#: silences mypy, which is why the round's rule forbids it instead.
BREAKS: list[tuple[str, list[tuple[str, str, str]], str, str]] = [
    (
        "B1 one UTC site put back (the debt returns)",
        [
            ("opendata/api/tasks.py", TASKS_IMPORT_OLD, TASKS_IMPORT_NEW),
            ("opendata/api/tasks.py", TASKS_USE_OLD, TASKS_USE_NEW),
        ],
        "red",
        "attr-defined",
    ),
    (
        "B2 the unannotated parameter left unannotated again",
        [("opendata/services/scheduler_service.py", JOB_DEF_OLD, JOB_DEF_NEW)],
        "red",
        "no-untyped-def",
    ),
    (
        "B3 the forbidden shortcut: widen python_version instead of fixing code",
        [("pyproject.toml", PYVER_OLD, PYVER_NEW)],
        "green",
        "Success",
    ),
]


def sha(name: str) -> str:
    """First twelve hex digits of a file's sha256."""
    return hashlib.sha256((REPO / name).read_bytes()).hexdigest()[:12]


def census(names: list[str]) -> dict[str, str]:
    """Digest of every file this runner may patch."""
    return {name: sha(name) for name in names}


def section(title: str) -> None:
    """Print a body separator."""
    print(f"\n==== {title} ====")


def run(label: str, argv: list[str]) -> int:
    """Echo one command verbatim, stream its output untrimmed, return the exit code."""
    print(f"$ {label}: {' '.join(argv)}")
    result = subprocess.run(  # noqa: S603  # nosec B603  # fixed argv
        argv,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    print((result.stdout + result.stderr).rstrip())
    print(f"[exit={result.returncode}]")
    return result.returncode


def patch(pairs: list[tuple[str, str, str]]) -> dict[str, str]:
    """Apply one declared break; hand back the file contents to restore."""
    restore: dict[str, str] = {}
    for name, old, new in pairs:
        path = REPO / name
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise SystemExit(f"anchor {old!r} matched {text.count(old)} times in {name}")
        restore.setdefault(name, text)
        path.write_text(text.replace(old, new), encoding="utf-8")
    return restore


def typed_face() -> tuple[int, str]:
    """Run the A1 mypy plane this round drives to zero; return exit plus output."""
    argv = [PY, "-m", "mypy", "--no-color-output", "opendata/", "opendata_fuyao/"]
    print("$ " + " ".join(argv[1:]))
    result = subprocess.run(  # noqa: S603  # nosec B603  # fixed argv
        argv,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    out = (result.stdout + result.stderr).rstrip()
    print(out)
    print(f"[exit={result.returncode}]")
    return result.returncode, out


def ratchet_face() -> tuple[int, str]:
    """Run the frozen ratchet: any regression against the new floor is a hard FAIL."""
    argv = [PY, "scripts/quality/ratchet.py"]
    print("$ " + " ".join(argv))
    result = subprocess.run(  # noqa: S603  # nosec B603  # fixed argv
        argv,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    out = (result.stdout + result.stderr).rstrip()
    print(out)
    print(f"[exit={result.returncode}]")
    return result.returncode, out


def main() -> int:
    """Emit the archive body: change, after-face, tests, counterfacts, restore."""
    section("0. 运行出处（时钟与 HEAD 由本遍自己打印）")
    run("clock", ["/bin/date"])
    run("clock-utc", ["/bin/date", "-u"])
    run("head", ["git", "rev-parse", "--short", "HEAD"])
    run("branch", ["git", "branch", "--show-current"])
    run("python", [PY, "-V"])
    print("ARCHIVE_ROUND=C60")

    section("1. 改动面（本轮 diff 原样，含 ruff format 对两处被拉长的行的收线）")
    run("diff-stat", ["git", "diff", "--stat", "--", *DEBT_FILES, "docs/quality/ratchet.json"])
    run(
        "diff-body",
        ["git", "--no-pager", "diff", "--", *DEBT_FILES, "docs/quality/ratchet.json"],
    )

    section("2. 修后类型面：A1 mypy 从 6 到 0")
    mypy_code, _ = typed_face()

    section("3. 修后 lint 面：六个文件逐规则计数（69 到 68，少的就是 ANN001 那条）")
    run("ruff-statistics", [PY, "-m", "ruff", "check", *DEBT_FILES, "--statistics"])
    run("ruff-format-check", [PY, "-m", "ruff", "format", "--check", *DEBT_FILES])

    section("4. 棘轮（本轮把 mypy 6 与 ruff 219 冻成 0 与 218）")
    ratchet_code, _ = ratchet_face()

    section("5. 受影响用例（service/api 平面，带 not e2e 过滤，不跑仓库腿）")
    tests_code = run(
        "tests",
        [
            PY,
            "-m",
            "pytest",
            "tests",
            "-q",
            "--no-cov",
            "-m",
            "not e2e",
            "-p",
            "no:cacheprovider",
            "-k",
            "execution or scheduler or retry or notification or auth or task",
        ],
    )

    section("6. 三条反事实：B1/B2 必须咬红，B3 演示「改配置就绿」为什么被禁止")
    names = sorted({name for _, pairs, _, _ in BREAKS for name, _, _ in pairs})
    original = census(names)
    print("  digests before breaks: " + str(original))
    verdicts: list[tuple[str, bool]] = [("clean tree mypy green", mypy_code == 0)]
    for label, pairs, expect, needle in BREAKS:
        print(f"\n---- {label}  (expect {expect}) ----")
        restore = patch(pairs)
        code, out = typed_face()
        hit = needle in out
        bites = code == 0 and hit
        if expect == "red":
            rc_code, rc_out = ratchet_face()
            bites = code != 0 and hit and rc_code != 0 and "mypy_selfdev" in rc_out
        print(f"expect={expect} needle={needle!r} found={hit} -> {'OK' if bites else 'BAD'}")
        verdicts.append((label, bites))
        for name, text in restore.items():
            (REPO / name).write_text(text, encoding="utf-8")

    section("7. 还原校验与终局")
    after = census(names)
    for name in names:
        state = "identical" if after[name] == original[name] else "DRIFT"
        print(f"  restore {name}: {original[name]} -> {after[name]} {state}")
    final_code, final_out = typed_face()
    final_ratchet, _ = ratchet_face()
    summary = " / ".join(f"{label}:{'OK' if ok else 'BAD'}" for label, ok in verdicts)
    print(f"\nSUMMARY {summary}")
    print(f"final mypy exit={final_code} ratchet exit={final_ratchet} pytest exit={tests_code}")
    print(f"final mypy tail: {final_out.splitlines()[-1]}")
    ok = (
        all(b for _, b in verdicts)
        and final_code == 0
        and final_ratchet == 0
        and tests_code == 0
        and original == after
    )
    print(f"C60_RUNNER_EXIT={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
