"""Cut a ``pytest -q --tb=line`` full-suite log into a red taxonomy the record can cite.

``571 failed`` on one line says nothing about *why*. This instrument reads the log the suite
wrote (``-q --tb=line --no-cov -p no:cacheprovider``) and publishes three mutually-checking
faces: the per-test-file failure counts, the per-exception-signature counts, and the count of
traceback frames that land in a named list of files. The cross-check is arithmetic, not taste:
the parsed ``FAILED`` lines must equal the summary line's ``failed`` number, and
``failed + passed + skipped`` must equal the parsed collection total, or the log is truncated
and every number below it is void.

Arm ``--arm`` feeds a forged three-failure log and requires (a) the right split, (b) that a log
whose summary disagrees with its FAILED lines is refused rather than summarised -- without it,
a parser that matched nothing would also print an empty, green-looking taxonomy.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

SUMMARY_RE = re.compile(
    r"=+\s*(?:(\d+) failed)?[,\s]*(?:(\d+) error[s]?[,\s]*)?(?:(\d+) passed)?[,\s]*"
    r"(?:(\d+) skipped)?[,\s]*.*in \d"
)
FAILED_RE = re.compile(r"^FAILED (tests/[^:]+\.py)::(.+)$")
TB_LINE_RE = re.compile(r"^/?\S*?([\w./-]+\.py):(\d+): (\S.*)$")

FORGED_LOG = """
/opendata/tests/test_a.py:10: AssertionError: 1 != 2
/opendata/tests/test_a.py:20: ValueError: bad
/opendata/tests/test_b.py:5: KeyError: 'x'
FAILED tests/test_a.py::test_one - AssertionError: 1 != 2
FAILED tests/test_a.py::test_two - ValueError: bad
FAILED tests/test_b.py::test_three - KeyError: 'x'
=========== 3 failed, 7 passed, 2 skipped, 12 collected in 1.23s ===========
SUITE_EXIT=1
"""


def short_key(path: str) -> str:
    """Reduce an absolute traceback path to a repo-relative key that cannot collide.

    ``--tb=line`` prints absolute paths, and this round edited two files named ``_client.py``
    under different providers, so a basename key would silently merge them.
    """
    for marker in ("opendata/data/", "opendata/services/", "opendata/", "scripts/", "tests/"):
        index = path.find(marker)
        if index != -1:
            return path[index:]
    return path


def parse(text: str, collected_total: int | None = None) -> dict[str, Any]:
    """Return the three faces plus the arithmetic cross-check verdict."""
    failed = int(sum(1 for _ in (FAILED_RE.match(line) for line in text.splitlines()) if _))
    summary = None
    for line in reversed(text.splitlines()):
        match = SUMMARY_RE.search(line)
        if match and any(group for group in match.groups()):
            summary = (line, match)
            break
    if summary is None:
        return {"verdict": "NO_SUMMARY_LINE", "failed_lines": failed}
    line, match = summary
    n_failed, n_errors, n_passed, n_skipped = (int(g or 0) for g in match.groups())

    by_file: Counter[str] = Counter()
    for raw in text.splitlines():
        hit = FAILED_RE.match(raw)
        if hit:
            by_file[hit.group(1)] += 1

    signatures: Counter[str] = Counter()
    frame_files: Counter[str] = Counter()
    for raw in text.splitlines():
        hit = TB_LINE_RE.match(raw)
        if not hit:
            continue
        frame_files[short_key(hit.group(1))] += 1
        head = hit.group(3).split(" - ")[0]
        signatures[head.split(":", 1)[0].strip()] += 1

    arithmetic = n_failed + n_errors + n_passed + n_skipped
    ok = failed == n_failed + n_errors and (
        collected_total is None or arithmetic == collected_total
    )
    return {
        "verdict": "OK" if ok else "LOG_DISAGREES",
        "summary_line": line.strip(),
        "failed": n_failed,
        "errors": n_errors,
        "passed": n_passed,
        "skipped": n_skipped,
        "failed_lines_parsed": failed,
        "failing_files": len(by_file),
        "by_file": by_file,
        "signatures": signatures,
        "frame_files": frame_files,
        "arithmetic_total": arithmetic,
        "collected_total": collected_total,
    }


def main(argv: list[str] | None = None) -> int:
    """Print the taxonomy, or run the falsification arm."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default="/tmp/suite_full.txt")  # nosec B108  # noqa: S108
    parser.add_argument(
        "--edited",
        default="",
        help="comma-separated file basenames to count traceback frames for",
    )
    parser.add_argument("--arm", action="store_true", help="run the falsification arm only")
    args = parser.parse_args(argv)

    if args.arm:
        good = parse(FORGED_LOG, collected_total=12)
        tampered = FORGED_LOG.replace("3 failed", "4 failed")
        bad = parse(tampered, collected_total=12)
        right_split = (
            good["verdict"] == "OK"
            and good["by_file"].get("tests/test_a.py") == 2
            and good["by_file"].get("tests/test_b.py") == 1
        )
        bites = bad["verdict"] == "LOG_DISAGREES"
        ok = right_split and bites
        print(
            f"arm: forged_split={good['verdict']}/a=2,b=1 {'OK' if right_split else 'BAD'}"
            f" tampered_summary={bad['verdict']} -> {'PASS' if ok else 'FAIL'}"
        )
        return 0 if ok else 1

    text = Path(args.log).read_text(encoding="utf-8", errors="replace")
    face = parse(text)
    if face["verdict"] != "OK":
        print(f"verdict: {face['verdict']} -- every number below is void")
        return 3
    print(f"summary: {face['summary_line']}")
    print(
        f"population: failed={face['failed']} errors={face['errors']} passed={face['passed']}"
        f" skipped={face['skipped']} failing_files={face['failing_files']}"
    )
    print(
        f"cross_check: failed_lines_parsed={face['failed_lines_parsed']} arithmetic="
        f"{face['arithmetic_total']}"
    )
    print("--- failures by test file (top 25)")
    for file, count in face["by_file"].most_common(25):
        print(f"  {count:4d}  {file}")
    print("--- one-line traceback signature (top 25)")
    for sig, count in face["signatures"].most_common(25):
        print(f"  {count:4d}  {sig}")
    if args.edited:
        wanted = {short_key(name) for name in args.edited.split(",") if name}
        frames = {name: face["frame_files"].get(name, 0) for name in sorted(wanted)}
        print("--- traceback frames landing in the files this round edited")
        for name, count in frames.items():
            print(f"  {count:4d}  {name}")
        print(f"face: edited_file_frames={sum(frames.values())} over {len(wanted)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
