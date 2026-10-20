"""Wave-41e: the `bond_daily_sina` replay mismatch is a wall-clock cache-buster, not wrong params.

`test_port_fidelity[bond_daily_sina]` raises `ReplayRequestMismatchError ... query/params` and AC6's
`case_pass_count` reads 13 where its expectation says 14. The failure text names a field, not the
two requests, so this face wraps the comparator's own ``route_assessment`` (no re-derivation of the
comparison rule), replays the case, and prints both sides.

The claim the arms have to support:

* the mismatch reproduces against the shipped comparator (A);
* the two URLs are identical once the ``d`` query parameter is removed (B), and that removal is not
  a tautology -- a different symbol still differs (C), and the recorded ``d`` is a date the vendored
  function could only have produced on the recording day (D);
* the vendored source builds ``d`` from ``datetime.datetime.now()`` (E), which is why replay on any
  other day cannot match. Nothing here decides who should change; it makes the decision decidable.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C147/wave41-objective-scoreboard-20261020"
RUNS = REPO / BUNDLE / "runs"
CARRIER = RUNS / "w110_request_identity_face.txt"
CASE = "bond_daily_sina"
VENDOR = REPO / "opendata/data/providers/akshare/_vendor/bond/bond_zh_cov.py"


def _strip_d(url: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(url)
    query = {}
    for pair in parts.query.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        query[key] = value
    query.pop("d", None)
    return f"{parts.scheme}://{parts.netloc}{parts.path}", query


def main() -> int:
    """Replay the case under a recording comparator hook and write the two-sided reading."""
    sys.path.insert(0, str(REPO))
    import scripts.codemod.compare_with_upstream as comparator

    RUNS.mkdir(parents=True, exist_ok=True)
    seen: list[tuple[str, dict[str, object], dict[str, object]]] = []
    original = comparator.route_assessment

    def spy(actual: dict[str, Any], recorded: dict[str, Any]) -> dict[str, Any]:
        result = original(actual, recorded)
        seen.append((str(result.get("status")), dict(actual), dict(recorded)))
        return result

    comparator.route_assessment = spy
    case = next(item for item in comparator.CASES if item.name == CASE)
    entries = comparator._read_transcript(comparator.FIXTURES_DIR / CASE / "responses.json.gz")
    function = comparator._load_case_function(comparator._PORTED_CASE_MODULE, case.function)
    comparator._pin_pure_requests_channel()
    replayer = comparator.HttpReplayer(entries)
    outcome = "replayed"
    try:
        with replayer:
            function(**case.kwargs)
    except Exception as error:  # the refusal is the reading
        outcome = f"{type(error).__name__}: {str(error).splitlines()[0][:120]}"
    comparator.route_assessment = original

    lines = [f"case={CASE} outcome={outcome} assessments={len(seen)}"]
    arms: dict[str, bool] = {}
    mismatch = next((row for row in seen if row[0] == "ROUTE_MISMATCH"), None)
    arms["A_the_mismatch_reproduces"] = mismatch is not None
    if mismatch is None:
        lines.append("no mismatch recorded; the rest of this face is vacuous")
    else:
        _status, actual, recorded = mismatch
        actual_url = str(actual.get("url") or "")
        recorded_url = str(recorded.get("url") or "")
        actual_base, actual_query = _strip_d(actual_url)
        recorded_base, recorded_query = _strip_d(recorded_url)
        lines.append(f"actual_url={actual_url}")
        lines.append(f"recorded_url={recorded_url}")
        lines.append(f"actual_without_d={actual_base} {actual_query}")
        lines.append(f"recorded_without_d={recorded_base} {recorded_query}")
        arms["B_identical_once_d_is_removed"] = (
            actual_base == recorded_base and actual_query == recorded_query
        )
        other_symbol, _q = _strip_d(actual_url.replace("sh010107", "sh010108"))
        arms["C_normalisation_is_not_a_tautology"] = other_symbol != recorded_base
        actual_d = re.search(r"[?&]d=(\d{4})_(\d{2})_(\d{2})", actual_url)
        recorded_d = re.search(r"[?&]d=(\d{4})_(\d{2})_(\d{2})", recorded_url)
        arms["D_both_dates_parse_and_differ"] = (
            actual_d is not None
            and recorded_d is not None
            and actual_d.group(0) != recorded_d.group(0)
        )
    source = VENDOR.read_text(encoding="utf-8")
    needle = 'datetime.datetime.now().strftime("%Y_%m_%d")'
    arms["E_vendored_source_uses_the_wall_clock"] = needle in source
    lines.append(
        f"vendored_wall_clock_expression_present={arms['E_vendored_source_uses_the_wall_clock']}"
    )

    passed = sum(1 for ok in arms.values() if ok)
    ok = passed == len(arms)
    lines.append(
        f"request_identity arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(name for name, value in arms.items() if not value)}"
    )
    text = "\n".join(lines) + "\n" + f"w110_request_identity_face_exit={0 if ok else 1}\n"
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"w110_request_identity_face_exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
