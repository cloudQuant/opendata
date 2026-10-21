"""Wave-49: measure how many provider modules carry the wrong-TRaw typing defect, tree-wide.

Why this face exists: §46 disclosed a pre-existing A2 mypy red on a committed file and §48 closed
that one file. Both sections then stated that no tree-wide sweep had been run, so "no other file
carries the defect" was explicitly not a measured claim. This instrument measures it.

Population: every module under ``opendata/data/providers/`` (vendored SDK trees excluded) in ONE
mypy process addressed by repo-relative paths -- a per-file run loses the siblings, which is what
made wave 46's A2 ``--files`` slice narrower than the tree.

Predicate: error codes ``override`` and ``no-any-return``, the exact shape §48 fixed (``TRaw``
declared as the row tuple while ``extract_data`` returns the raw response structure). Every other
code is histogrammed separately, so a zero here is not a claim that the tree is mypy-clean.

Two-sided control from git rather than a fabricated mutation: ``30faded84^`` holds the pre-fix
``econdb/models/profiles.py`` bytes and ``30faded84`` holds the fixed ones. Both are copied into the
scratch dir and joined to the same run. The predicate must flag the pre-fix copy and
read zero on the fixed copy; if either half fails, a census zero would mean nothing.
"""

from __future__ import annotations

import collections
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
from pathlib import Path
from typing import Final

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C147/wave41-objective-scoreboard-20261020"
RUNS = REPO / BUNDLE / "runs"
CARRIER = RUNS / "w148_fetcher_typing_census.py.txt"
SCRATCH = Path("/tmp/w148_fetcher_typing_census")  # nosec B108  # noqa: S108
PROVIDERS = REPO / "opendata/data/providers"
PREFIX: Final = "opendata/data/providers/"
DEFECT_CODES: Final = ("override", "no-any-return")
FIX_COMMIT = "30faded84"
ECONDB = f"{PREFIX}econdb/models/profiles.py"


def _git_show(rev: str, path: str) -> str:
    """Read one revision of one path, with CJK paths left unquoted."""
    result = subprocess.run(  # nosec B603  # noqa: S603
        ["/usr/bin/git", "-c", "core.quotepath=off", "show", f"{rev}:{path}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"git show {rev}:{path} failed rc={result.returncode}")
    return result.stdout


def _run_mypy(targets: list[str]) -> tuple[int, str]:
    """One mypy process over every target; return its rc and its combined output."""
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [sys.executable, "-m", "mypy", "--cache-dir", str(SCRATCH / ".mypy_cache"), *targets],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _error_lines(output: str) -> list[tuple[str, str]]:
    """Split mypy output into (file, code) pairs; a codeless error reads as uncoded."""
    pairs: list[tuple[str, str]] = []
    for line in output.splitlines():
        if ": error:" not in line:
            continue
        path = line.split(": error:", 1)[0].split(":", 1)[0]
        marker = line.rfind("[")
        code = line[marker + 1 : -1] if line.endswith("]") and marker > 0 else "uncoded"
        pairs.append((path, code))
    return pairs


def main() -> int:
    """Run the census, arm the two-sided control, and write the carrier."""
    RUNS.mkdir(parents=True, exist_ok=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    pre_fix = SCRATCH / "pre_fix_profiles.py"
    post_fix = SCRATCH / "post_fix_profiles.py"
    pre_fix.write_text(_git_show(f"{FIX_COMMIT}^", ECONDB), encoding="utf-8")
    post_fix.write_text(_git_show(FIX_COMMIT, ECONDB), encoding="utf-8")

    modules = [
        str(path.relative_to(REPO))
        for path in sorted(PROVIDERS.rglob("*.py"))
        if "_vendor" not in path.parts and "__pycache__" not in path.parts
    ]
    # Two mypy processes: the population sweep and the git-derived control keep separate error sets,
    # so a control hit can never be counted as a census hit.
    rc, sweep = _run_mypy(modules)
    control_rc, control = _run_mypy([str(pre_fix), str(post_fix)])
    pairs = _error_lines(sweep) + _error_lines(control)

    by_file: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    histogram: collections.Counter[str] = collections.Counter()
    for path, code in pairs:
        histogram[code] += 1
        by_file[path][code] += 1

    def defect(name: str) -> int:
        return sum(by_file[name][code] for code in DEFECT_CODES)

    carried = sorted(
        (name for name in modules if defect(name) > 0),
        key=lambda name: (-defect(name), name),
    )
    pre_defect = defect(str(pre_fix))
    post_defect = defect(str(post_fix))
    econdb_now = defect(ECONDB)

    lines = [
        "# carrier for w148_fetcher_typing_census.py",
        f"# argv: {' '.join(sys.argv)}",
        "",
        f"population provider_modules={len(modules)} excluded=('_vendor','__pycache__')",
        f"mypy sweep_rc={rc} control_rc={control_rc} error_lines={len(pairs)} "
        f"distinct_codes={len(histogram)}",
        f"code_histogram={dict(histogram.most_common(12))}",
        f"defect_class_files={len(carried)} defect_class_errors={sum(defect(n) for n in carried)}",
    ]
    lines.extend(f"  carried {defect(name):3d}  {name}" for name in carried[:20])
    if len(carried) > 20:
        lines.append(f"  ... {len(carried) - 20} further carried files")
    lines.append(
        f"control pre_fix_defect={pre_defect} (must be >0) "
        f"post_fix_defect={post_defect} (must be 0)"
    )
    lines.append(f"econdb_now_defect={econdb_now} (must be 0, the wave-48 fix)")

    arms = {
        "A_population_is_the_provider_tree": len(modules) > 100
        and all(name.startswith(PREFIX) for name in modules),
        "B_control_pre_fix_is_flagged": pre_defect > 0,
        "C_control_post_fix_is_clean": post_defect == 0,
        "D_wave48_fix_still_holds": econdb_now == 0,
        "E_census_covers_every_carried_file": sum(defect(name) for name in carried)
        == sum(defect(name) for name in modules)
        and len(carried) == len([name for name in modules if defect(name) > 0]),
        "F_mypy_spoke_and_no_uncoded_line": rc in (0, 1)
        and control_rc in (0, 1)
        and histogram.get("uncoded", 0) == 0,
    }
    passed = sum(1 for ok in arms.values() if ok)
    ok = passed == len(arms)
    lines.append(
        f"fetcher_typing_census arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(name for name, value in arms.items() if not value)}"
    )
    text = "\n".join(lines) + "\n" + f"w148_fetcher_typing_census_exit={0 if ok else 1}\n"
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"w148_fetcher_typing_census_exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
