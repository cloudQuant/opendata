"""C90 second instrument: what the re-anchor changed in the *published* roadmap artifact.

:mod:`cast-rescale-anchor-honesty` judges the engine kernel and the census registry by executing
the surface and re-running the repo's own greedy cover. This one reads the two artifacts that round
wrote -- the roadmap the tree carried before the re-anchor (commit ``e05d1da``, round ``C89``) and
the one now on disk (round ``C90``) -- and answers the question §45 left open in its strongest form:

  * did anchoring ``columns.cast`` / ``columns.rescale`` change *which* rows the engine pool
    unlocks, or only which ladder step each of those rows is credited to?

The answer is published as a set identity over the full 279-row census, not as a 3-row sample, and
each equality claim is paired with a control that must read the other way -- the same blob diffed
against itself, a pool with one row dropped, a credit renamed -- so a green ``True`` below is a
measurement and not a hardcoded verdict. ``PRE_ANCHOR_REV`` is checked against the blob it
resolves to: once the tree moves past this round the instrument refuses, instead of silently
diffing a blob with itself.

No network, no database, no gate.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404  # read-only git argv, shell disabled
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn

if TYPE_CHECKING:
    from collections.abc import Callable

REPO = next(
    ancestor
    for ancestor in Path(__file__).resolve().parents
    if (ancestor / "scripts" / "quality" / "model_capability_census.py").is_file()
)
sys.path.insert(0, str(REPO / "scripts" / "quality"))
import model_capability_census as mcc  # noqa: E402

#: The commit whose tree carries the pre-anchor roadmap. Read from the blob, never assumed.
PRE_ANCHOR_REV: Final = "e05d1da"
UNDER_AUDIT: Final = ("columns.cast", "columns.rescale")
#: Registry reading fields whose face is published for each capability under audit.
READING_FIELDS: Final = (
    "status",
    "present",
    "claim",
    "read_from_disk",
    "engine_work",
    "proof_file",
    "anchor",
    "anchor_found",
    "anchor_line_number",
    "cross_file",
    "cross_anchor",
    "cross_anchor_found",
    "production_declarations",
)
BAD: list[str] = []
#: A ladder face that credits one row from two steps -- the counterfact proving the partition guard
#: inside :func:`pool_rows` is live rather than a branch no real artifact ever reaches.
DOUBLE_CREDIT_DOC: Final = {
    "roadmap": {
        "greedy_build_order": [
            {"capability": "a.first", "newly_unlocked_rows": [{"task_id": "R-1"}]},
            {"capability": "a.second", "newly_unlocked_rows": [{"task_id": "R-1"}]},
        ]
    }
}


class StaleRevisionError(Exception):
    """The pre-anchor revision resolves to a blob of the round already on disk."""


class DoubleCreditError(Exception):
    """One row is credited by two ladder steps, so the pool is not a partition of rows."""


def die(msg: str) -> NoReturn:
    """Print a FAIL line and stop the run with a non-zero exit."""
    print(f"FAIL: {msg}")
    raise SystemExit(1)


def refuse(label: str, thunk: Callable[[], object], needle: str) -> None:
    """Print the stop a guarded arm must raise, dying when the arm runs through instead."""
    try:
        thunk()
    except (StaleRevisionError, DoubleCreditError) as error:
        text = f"{type(error).__name__}: {error}"
        if needle not in text:
            die(f"{label}: refused with the wrong words: {text[:200]}")
        print(f"REFUSE {label:<44} -> {text[:170]}")
        return
    die(f"{label}: expected a refusal, the arm accepted")


def stop_as_failure(label: str, thunk: Callable[[], object]) -> None:
    """Run a guarded arm over the real artifacts, ending with a message if it refuses."""
    try:
        thunk()
    except (StaleRevisionError, DoubleCreditError) as error:
        die(f"{label}: {error}")


def show(value: object) -> str:
    """Render one reading without leaking set order, which is hash-randomised per process."""
    if isinstance(value, (set, frozenset)):
        return "{" + ", ".join(sorted(repr(item) for item in value)) + "}"
    return repr(value)


def require(label: str, got: object, want: object) -> None:
    """Publish one reading and record it as a failure when it is not the expected value."""
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'} {label}={show(got)} want={show(want)}")
    if not ok:
        BAD.append(label)


def git_show(rev: str, rel: str) -> str:
    """Return one blob from history so the pre-anchor face is read from git, not retyped."""
    proc = subprocess.run(  # nosec B603, B607  # noqa: S603
        ["git", "show", f"{rev}:{rel}"],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        die(f"git show {rev}:{rel} exited {proc.returncode}: {proc.stderr.strip()[:160]}")
    return proc.stdout


def artifact(text: str, label: str) -> dict[str, Any]:
    """Parse one roadmap blob, naming the face it came from when the bytes are not JSON."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        die(f"{label} roadmap artifact is not valid JSON: {error}")
    if not isinstance(parsed, dict):
        die(f"{label} roadmap artifact is a {type(parsed).__name__}, not an object")
    return parsed


def pool_rows(doc: dict[str, Any]) -> dict[str, str]:
    """Map each row the full engine pool unlocks to the ladder step that credits it."""
    credited: dict[str, str] = {}
    for step in doc["roadmap"]["greedy_build_order"]:
        for row in step["newly_unlocked_rows"]:
            task_id = str(row["task_id"])
            if task_id in credited:
                raise DoubleCreditError(
                    f"{task_id} is credited twice: {credited[task_id]} and {step['capability']}"
                )
            credited[task_id] = str(step["capability"])
    return credited


def require_distinct_rounds(before: str, after: str) -> None:
    """Stop when a blob-to-itself diff would be published as a movement."""
    if before == after:
        raise StaleRevisionError(
            f"the pre-anchor revision already carries round {after}; point PRE_ANCHOR_REV at the "
            "commit whose tree holds the roadmap written before the re-anchor"
        )


def row_ids(rows: object) -> list[str]:
    """Sort one roadmap row list down to its task ids, tolerating scalar faces."""
    if isinstance(rows, int):
        return [f"scalar:{rows}"]
    if not isinstance(rows, list):
        die(f"roadmap face is a {type(rows).__name__}, not a row list or a row count")
    return sorted(str(row["task_id"]) if isinstance(row, dict) else str(row) for row in rows)


def status_sets(doc: dict[str, Any]) -> dict[str, set[str]]:
    """Return the registry's four status buckets as id sets."""
    return {name: set(members) for name, members in doc["taxonomy"]["statuses"].items()}


def caps_in_ladder(doc: dict[str, Any]) -> list[str]:
    """Return the engine-work pool the greedy ladder walks, in step order."""
    return [str(step["capability"]) for step in doc["roadmap"]["greedy_build_order"]]


def steps_by_cap(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index the ladder by capability so per-step movement is a lookup, not a re-derivation."""
    return {str(step["capability"]): step for step in doc["roadmap"]["greedy_build_order"]}


def registry_reading(doc: dict[str, Any], cap: str) -> dict[str, Any]:
    """Return one capability's serialized :class:`EntryReading` from the artifact."""
    registry = doc["capability_registry"]
    if not isinstance(registry, dict) or cap not in registry:
        die(f"artifact has no capability_registry entry for {cap}")
    return dict(registry[cap])


def main() -> int:
    """Print the C89 -> C90 roadmap face and judge its invariants."""
    old = artifact(git_show(PRE_ANCHOR_REV, mcc.ROADMAP_REL), "pre-anchor")
    new = artifact((REPO / mcc.ROADMAP_REL).read_text(encoding="utf-8"), "disk")
    rev_old, rev_new = str(old["round_id"]), str(new["round_id"])
    print(f"ARTIFACT before={PRE_ANCHOR_REV} round_id={rev_old} path={mcc.ROADMAP_REL}")
    disk_bytes = (REPO / mcc.ROADMAP_REL).read_bytes()
    print(f"ARTIFACT after=disk round_id={rev_new} bytes={len(disk_bytes)}")
    stop_as_failure("staleness", lambda: require_distinct_rounds(rev_old, rev_new))
    print(f"STALENESS guard live: rounds differ ({rev_old} != {rev_new})")
    refuse(
        "same-round diff refused",
        lambda: require_distinct_rounds(rev_new, rev_new),
        "already carries",
    )

    census_old, census_new = old["census"], new["census"]
    for key in ("row_count_measured", "row_count_expected", "unique_task_ids"):
        require(f"CENSUS.{key}", census_new[key], census_old[key])
    require("CENSUS.row_count_guard_fired", census_new["row_count_guard_fired"], False)
    require("CENSUS.files", census_new["files"], census_old["files"])

    tax_old, tax_new = old["taxonomy"], new["taxonomy"]
    require(
        "TAXONOMY.canonical_capability_count",
        tax_new["canonical_capability_count"],
        tax_old["canonical_capability_count"],
    )
    require("TAXONOMY.raw_label_count", tax_new["raw_label_count"], tax_old["raw_label_count"])
    require(
        "TAXONOMY.raw_label_mention_count",
        tax_new["raw_label_mention_count"],
        tax_old["raw_label_mention_count"],
    )
    for cap in UNDER_AUDIT:
        mentions = tax_new["mentions_per_capability"][cap]
        require(f"MENTIONS.{cap}", mentions, tax_old["mentions_per_capability"][cap])
        print(f"MENTION {cap} n={mentions} (label population unchanged by the re-anchor)")
    require(
        "TAXONOMY.rows_needing_only_this_capability",
        tax_new["rows_needing_only_this_capability"],
        tax_old["rows_needing_only_this_capability"],
    )

    st_old, st_new = status_sets(old), status_sets(new)
    audit = set(UNDER_AUDIT)
    for name in ("present", "absent", "drifted", "unanchored"):
        before, after = st_old.get(name, set()), st_new.get(name, set())
        print(
            f"STATUS {name} n={len(before)}->{len(after)} added={sorted(after - before)} "
            f"removed={sorted(before - after)}"
        )
    require("PRESENT_ADDED", st_new["present"] - st_old["present"], audit)
    require("PRESENT_REMOVED", st_old["present"] - st_new["present"], set())
    require("ABSENT_REMOVED", st_old["absent"] - st_new["absent"], audit)
    require("DRIFTED_EMPTY", st_new["drifted"], set())
    total = sum(len(bucket) for bucket in st_new.values())
    require("STATUS_TOTAL", total, tax_new["canonical_capability_count"])

    for cap in UNDER_AUDIT:
        a, b = registry_reading(old, cap), registry_reading(new, cap)
        for field in READING_FIELDS:
            print(f"READING {cap}.{field} {a.get(field)!r} -> {b.get(field)!r}")
        print(f"READING {cap}.anchor_line_before={a.get('anchor_line')!r}")
        print(f"READING {cap}.anchor_line_after={b.get('anchor_line')!r}")
        print(f"READING {cap}.why_before={a.get('why')!r}")
        print(f"READING {cap}.why_after={b.get('why')!r}")
        require(f"STATUS.{cap}", b["status"], "present")
        require(f"DECLARATIONS.{cap}", b["production_declarations"], 0)

    require("BUILT_BUT_UNUSED.gained", audit <= set(new["built_but_unused"]), True)
    print(f"BUTU {rev_old}={sorted(old['built_but_unused'])}")
    print(f"BUTU {rev_new}={sorted(new['built_but_unused'])}")

    check_new = new["check"]
    require("CHECK.failures", check_new["failures"], [])
    require("CHECK.drifted_capabilities", sorted(check_new["drifted_capabilities"]), [])

    dec_old, dec_new = old["declaration_scan"], new["declaration_scan"]
    require("DECLSCAN.modules_scanned", dec_new["modules_scanned"], dec_old["modules_scanned"])
    require(
        "DECLSCAN.production_declarations_by_file",
        dec_new["production_declarations_by_file"],
        dec_old["production_declarations_by_file"],
    )

    road_old, road_new = old["roadmap"], new["roadmap"]
    for key in (
        "rows_pending",
        "rows_expressible_today",
        "rows_unlocked_by_greedy_prefix",
        "rows_with_no_engine_work_possible",
    ):
        require(f"TOTAL.{key}", road_new[key], road_old[key])
    for key in (
        "shipped_cover_rows",
        "rows_outside_engine",
        "rows_still_locked_after_engine_pool",
        "rows_with_empty_need_set",
    ):
        ids_old, ids_new = row_ids(road_old[key]), row_ids(road_new[key])
        print(
            f"ROWSET {key} n={len(ids_old)}->{len(ids_new)} "
            f"added={sorted(set(ids_new) - set(ids_old))} "
            f"removed={sorted(set(ids_old) - set(ids_new))}"
        )
        require(f"ROWSET.{key}.identical", ids_new == ids_old, True)

    pool_a: dict[str, str] = {}
    pool_b: dict[str, str] = {}

    def extract_pools() -> None:
        """Read both ladders under the partition guard so a double credit stops the run."""
        pool_a.update(pool_rows(old))
        pool_b.update(pool_rows(new))

    stop_as_failure("double-credit", extract_pools)
    refuse(
        "forged ladder crediting one row twice",
        lambda: pool_rows(DOUBLE_CREDIT_DOC),
        "credited twice",
    )
    require("POOL_SIZE", len(pool_b), len(pool_a))
    require("POOL_ID_SET_EQUAL", sorted(pool_b) == sorted(pool_a), True)
    require("POOL_ONLY_BEFORE", sorted(set(pool_a) - set(pool_b)), [])
    require("POOL_ONLY_AFTER", sorted(set(pool_b) - set(pool_a)), [])
    reattributed = {
        tid: (pool_a[tid], pool_b[tid])
        for tid in pool_a
        if tid in pool_b and pool_a[tid] != pool_b[tid]
    }
    print(f"POOL rows={len(pool_a)} reattributed={len(reattributed)}")
    for task_id in sorted(reattributed):
        print(f"REATTRIBUTED {task_id} {reattributed[task_id][0]} -> {reattributed[task_id][1]}")

    caps_old, caps_new = caps_in_ladder(old), caps_in_ladder(new)
    print(f"LADDER steps {len(caps_old)}->{len(caps_new)}")
    print(f"LADDER_DROPPED={sorted(set(caps_old) - set(caps_new))}")
    print(f"LADDER_ADDED={sorted(set(caps_new) - set(caps_old))}")
    require("LADDER_DROPPED", set(caps_old) - set(caps_new), audit)
    require("LADDER_ADDED", set(caps_new) - set(caps_old), set())
    steps_a, steps_b = steps_by_cap(old), steps_by_cap(new)
    for cap in sorted(set(caps_old) & set(caps_new)):
        a, b = steps_a[cap], steps_b[cap]
        print(
            f"LADDER_STEP {cap} newly={a['rows_newly_unlocked']}->{b['rows_newly_unlocked']} "
            f"cum={a['cumulative_rows_unlocked']}->{b['cumulative_rows_unlocked']} "
            f"moved={a['rows_newly_unlocked'] != b['rows_newly_unlocked']}"
        )
    for cap in UNDER_AUDIT:
        step = steps_a[cap]
        ids = sorted(str(row["task_id"]) for row in step["newly_unlocked_rows"])
        print(f"CREDITED_BEFORE {cap} n={step['rows_newly_unlocked']} ids={ids}")
        print(
            f"CREDITED_AFTER {cap} rows now credited to "
            f"{sorted({pool_b[i] for i in ids if i in pool_b})}"
        )
    sum_newly_old = sum(s["rows_newly_unlocked"] for s in old["roadmap"]["greedy_build_order"])
    sum_newly_new = sum(s["rows_newly_unlocked"] for s in new["roadmap"]["greedy_build_order"])
    require("SUM_NEWLY.equal", sum_newly_new, sum_newly_old)
    require("SUM_NEWLY.equals_pool", sum_newly_new, len(pool_b))
    shipped = len(row_ids(road_new["shipped_cover_rows"]))
    require(
        "CUM_LAST.equals_pool_plus_shipped",
        road_new["greedy_build_order"][-1]["cumulative_rows_unlocked"],
        len(pool_b) + shipped,
    )
    print(
        f"ARITHMETIC pool={len(pool_b)} + shipped_cover={shipped} = "
        f"cumulative={road_new['greedy_build_order'][-1]['cumulative_rows_unlocked']} "
        f"(both rounds: newly sum {sum_newly_old}/{sum_newly_new})"
    )

    same = pool_rows(new)
    require("CONTROL.self_diff_pools_equal", sorted(same) == sorted(pool_b), True)
    require(
        "CONTROL.self_diff_reattributes_nothing",
        {k for k in same if k in pool_b and same[k] != pool_b[k]},
        set(),
    )
    untouched = sorted(k for k in pool_b if k not in reattributed)
    if not untouched:
        die("no pool row is credited identically in both rounds, so the tamper arms have no target")
    tamper_row = untouched[0]
    dropped = dict(pool_b)
    dropped.pop(tamper_row)
    require("CONTROL.dropping_a_row_breaks_the_identity", sorted(dropped) == sorted(pool_a), False)
    require("CONTROL.dropping_a_row_changes_the_size", len(dropped), len(pool_a) - 1)
    renamed = dict(pool_b)
    renamed[tamper_row] = "not.a.capability"
    tamper_reattributed = {k for k in pool_a if k in renamed and pool_a[k] != renamed[k]}
    require(
        "CONTROL.renaming_a_credit_adds_only_the_tampered_row",
        sorted(tamper_reattributed - set(reattributed)),
        [tamper_row],
    )
    require(
        "CONTROL.renaming_a_credit_keeps_every_real_reattribution",
        sorted(set(reattributed) - tamper_reattributed),
        [],
    )
    print(
        f"CONTROL_TARGET self_pool={len(same)} tamper_row={tamper_row} "
        f"real_reattributed={len(reattributed)} tampered={len(tamper_reattributed)}"
    )

    print(f"METHOD_NOTE_UNCHANGED={old['method_note'] == new['method_note']}")
    require("SCHEMA_VERSION", new["schema_version"], old["schema_version"])
    if BAD:
        for failure in BAD:
            print(f"FAIL invariant: {failure}")
        return 1
    print(
        f"VERDICT rounds={rev_old}->{rev_new} pool_identity_holds=True "
        f"reattributed={len(reattributed)} headroom_rows=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
