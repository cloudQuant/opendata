"""Wave-41v: a clean-room *code-overlap* census against the reference checkout, not just imports.

``w73``/``w112`` prove this repo imports nothing from ``openbb*`` and that 49 files cite upstream
contract files as prose. Neither answers "was code copied without importing it". This face takes
every Python line under ``opendata/data/providers/**`` (vendored third-party excluded, as the
clean-room boundary already defines), normalizes it (strip, drop blanks/comments, collapse
whitespace, drop lines shorter than eight characters), and asks whether any run of six consecutive
such lines also appears, in the same order, anywhere under the reference checkout's provider tree.

Windows are split in two, because they mean different things. A **logic** window contains at least
one statement line (``def``/``return``/``if``/``for``/``raise``/calls); a **table** window is only
parameter and identifier rows (series ids, venue slugs, interval names). Reusing the same API's
parameter vocabulary is how two clients call the same endpoint; reusing a function body is copying.
Only the logic count is the clean-room hard claim -- the table count is reported, and the boundary
question (are curated parameter tables data or expression?) is left to the owner.

The detector is proved two-sided on a scratch copy: splicing six upstream *logic* lines into one of
our files makes the logic arm fire, and the untouched copy reads zero. Matches are reported as
locations only -- this carrier does not reproduce the reference source.
"""

from __future__ import annotations

import re
import shutil
import sys
from collections import Counter
from pathlib import Path

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
UPSTREAM = Path("/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers")
BUNDLE = "docs/evidence/C147/wave41-objective-scoreboard-20261020"
RUNS = REPO / BUNDLE / "runs"
CARRIER = RUNS / "w132_clean_room_overlap.txt"
SCRATCH = Path("/tmp/w132_overlap")  # nosec B108  # noqa: S108
WINDOW = 6
MIN_CHARS = 8
#: A shared window is "logic" when any line is a statement, not a parameter/value row.
LOGIC = re.compile(
    r"\b(def |class |return|if |elif |else:|for |while |try:|except |with |raise |yield |await "
    r"|lambda )|\.append\(|\.format\(|requests\.|pd\."
)


def _normalized(path: Path) -> list[tuple[int, str]]:
    kept: list[tuple[int, str]] = []
    for number, raw in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        line = " ".join(raw.split())
        if not line or line.startswith("#"):
            continue
        if len(line) < MIN_CHARS:
            continue
        kept.append((number, line))
    return kept


def _windows(lines: list[tuple[int, str]]) -> dict[tuple[str, ...], int]:
    out: dict[tuple[str, ...], int] = {}
    for index in range(len(lines) - WINDOW + 1):
        key = tuple(text for _number, text in lines[index : index + WINDOW])
        out.setdefault(key, lines[index][0])
    return out


def _upstream_index() -> tuple[dict[tuple[str, ...], str], int, int]:
    index: dict[tuple[str, ...], str] = {}
    files = 0
    for path in sorted(UPSTREAM.rglob("*.py")):
        files += 1
        windows = _windows(_normalized(path))
        for key, first_line in windows.items():
            index.setdefault(key, f"{path.relative_to(UPSTREAM)}:{first_line}")
    return index, files, len(index)


def _ours() -> list[Path]:
    root = REPO / "opendata/data/providers"
    return sorted(
        path
        for path in root.rglob("*.py")
        if "_vendor" not in path.parts and "__pycache__" not in path.parts
    )


def _label(path: Path) -> str:
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return f"scratch:{path.name}"


def _matches(index: dict[tuple[str, ...], str], paths: list[Path]) -> tuple[list[str], list[str]]:
    """Return (logic windows, table windows) that also appear upstream."""
    logic: list[str] = []
    table: list[str] = []
    for path in paths:
        lines = _normalized(path)
        for key, line_number in _windows(lines).items():
            hit = index.get(key)
            if hit is None:
                continue
            entry = f"{_label(path)}:{line_number} -> {hit}"
            (logic if any(LOGIC.search(text) for text in key) else table).append(entry)
    return logic, table


def _control(index: dict[tuple[str, ...], str]) -> tuple[int, int]:
    """Splice an upstream *logic* window into a scratch copy; return (untouched, spliced) counts."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    source = REPO / "opendata/data/providers/fmp/models/iter2.py"
    six: list[str] = []
    for path in sorted(UPSTREAM.rglob("*.py")):
        lines = _normalized(path)
        for start in range(len(lines) - WINDOW + 1):
            window = [text for _number, text in lines[start : start + WINDOW]]
            if any(LOGIC.search(text) for text in window):
                six = window
                break
        if six:
            break
    if not six:
        raise SystemExit("no upstream logic window found")
    clean = SCRATCH / source.name
    shutil.copyfile(source, clean)
    untouched_logic, _untouched_table = _matches(index, [clean])
    spliced = SCRATCH / "spliced.py"
    original = source.read_text(encoding="utf-8").splitlines()
    spliced.write_text("\n".join(["\n".join(six), *original]) + "\n", encoding="utf-8")
    spliced_logic, _spliced_table = _matches(index, [spliced])
    return len(untouched_logic), len(spliced_logic)


def main() -> int:
    """Build the upstream window index, split our overlaps, and prove the logic arm bites."""
    RUNS.mkdir(parents=True, exist_ok=True)
    index, upstream_files, windows = _upstream_index()
    ours = _ours()
    logic, table = _matches(index, ours)
    untouched, spliced = _control(index)
    pair_counts: Counter[str] = Counter(entry.split(" -> ")[1].split(":")[0] for entry in table)
    lines = [
        f"upstream_files={upstream_files} upstream_windows={windows}",
        f"table_pairs={len(pair_counts)} top={pair_counts.most_common(8)}",
        f"our_files={len(ours)} window={WINDOW} min_chars={MIN_CHARS}",
        f"shared_logic_windows={len(logic)} sample={logic[:3]}",
        f"shared_table_windows={len(table)} sample={table[:3]}",
        f"control untouched_logic={untouched} spliced_logic={spliced}",
    ]
    arms: dict[str, bool] = {
        "A_denominator_is_not_empty": upstream_files >= 500
        and windows >= 50000
        and len(ours) >= 300,
        "B_no_shared_window_carries_logic": logic == [],
        "C_shared_windows_are_parameter_tables": len(table) > 0 and logic == [],
        "D_untouched_copy_reads_zero_logic": untouched == 0,
        "E_spliced_logic_window_is_detected": spliced >= 1,
    }
    passed = sum(1 for ok in arms.values() if ok)
    ok = passed == len(arms)
    lines.append(
        f"clean_room_overlap arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(name for name, value in arms.items() if not value)}"
    )
    text = "\n".join(lines) + "\n" + f"w132_clean_room_overlap_exit={0 if ok else 1}\n"
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"w132_clean_room_overlap_exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
