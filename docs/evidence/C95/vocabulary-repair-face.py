#!/usr/bin/env python3
"""C95: recompute the ladder-front vocabulary repair from disk, with arms that can fail it.

Every face is derived from files -- the shipped roadmap, the 19 census files, the judge's own fold
table and the upstream OpenBB tree the census cites -- never from a claim made earlier in prose.
The judge is IMPORTED, not re-implemented (``map_needs`` is the repo's own fail-closed folder), so
this archive cannot drift from the thing it audits.

Faces:
    F1 registry      the four new canonicals read ``absent`` with their anchor on disk, and nothing
                    went ``drifted`` -- a drifted entry leaves the roadmap pool silently.
    F2 census        279 rows, 279 unique task ids, and the judge's folder accepts every spelling.
    F3 cite carrier  every label added or re-folded this round resolves to the exact upstream line
                    carrying the mechanism it names; a label without a landing cite is unfounded.
    F3b refutation   the one credit DELETED is refuted by a line, not by an argument.
    F4 monotonicity  per row, the absent-blocker set under the shipped judgment vs the repaired one.
                    A repair may only make rows HARDER to ship.
    F4b fold-only    the same face with 78710d0's credit lists held fixed, so the fold-table edit
                    is measured on its own instead of cancelling against a row edit inside F4.
    F5 arithmetic    the roadmap's mention delta equals the sum of the per-row label deltas, and the
                    changed-row set equals this round's declared scope.
    F6 ladder        prefix + expressible-today + still-locked partitions the census.
    F7 reproducibility
                    the judge is re-run on the census and registry on disk and its greedy ladder is
                    compared with the shipped artifact's, then the dispatch brief snapshot is
                    compared with that same ladder (a pre-repair brief would send builders at a
                    capability the repaired front no longer contains).
    F8 projection   the two moves a kernel round makes, measured through the repo's own greedy
                    cover: registering the capability present moves the ladder FRONT only, because
                    ``expressible_today`` is a STORED census field; the row buckets move when the
                    rows are re-derived. Both arms must keep partitioning the census, which is what
                    refutes a prose prediction that raises expressible-today and holds the prefix.

Counterfacts, each of which must move a verdict or the face it targets is decorative:
    C1 forge a row losing a blocker      -> F4 machinery must report the shrink.
    C2 feed the judge an unknown spelling -> it must raise.
    C3 shift a cite one line off          -> its token must no longer be there.
    C4 forge a row losing its only ladder blocker -> the recomputed greedy front must move.
    C5 revert one grown row's folds to the shipped table -> its blocker set must shrink back,
       which is what makes F4b's ``grew`` attributable to the fold edit rather than to noise.
    C6 re-derive ONE of the step-0 rows instead of all three -> F8's bucket move must shrink with
       it, so the +3 is attributable to the flipped rows rather than to a constant.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess  # nosec B404  # read-only git argv, shell disabled
import sys
import tempfile
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
CENSUS_DIR = REPO_ROOT / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
ARTIFACT = CENSUS_DIR / "capability-roadmap.json"
FRONT = pathlib.Path(__file__).resolve().parent / "ladder-front-steps0-3.json"
UPSTREAM = pathlib.Path("/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform")

#: The PRE-REPAIR tree every delta face is measured against, pinned by rev instead of read off HEAD.
#: Once the repair lands the two sides become the same tree: F4/F4b/F5 would each read a green-
#: looking zero of a vacuous comparison, and C5 would lose the row its arm is built from.
BASE_REV = "78710d0"

sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))
import model_capability_census as work_judge  # noqa: E402

NEW_CANONICALS = (
    "rows.needle_transform",
    "rows.order_by_parameter",
    "shape.long_to_wide_pivot",
    "columns.many_to_one_rename",
)

#: (task_id, spelling, carrier root, repo-relative path, 1-based line, token that must sit there)
CITES: tuple[tuple[str, str, str, str, int, str], ...] = (
    (
        "OBB2-tmx-EtfSearch",
        "filter_needle_normalization",
        "upstream",
        "providers/tmx/openbb_tmx/models/etf_search.py",
        243,
        ".capitalize()",
    ),
    (
        "OBB2-tmx-EtfSearch",
        "parameter_selected_sort_column",
        "upstream",
        "providers/tmx/openbb_tmx/models/etf_search.py",
        246,
        "sort_values(by=query.sort_by",
    ),
    (
        "OBB2-tmx-EtfSearch",
        "all_null_row_drop",
        "upstream",
        "providers/tmx/openbb_tmx/models/etf_search.py",
        261,
        "dropna(how=",
    ),
    (
        "OBB2-tmx-EtfHoldings",
        "dual_source_spelling_rename",
        "upstream",
        "providers/tmx/openbb_tmx/models/etf_holdings.py",
        127,
        '"shareChange"',
    ),
    (
        "OBB2-tmx-EtfSectors",
        "filter_needle_normalization",
        "upstream",
        "providers/tmx/openbb_tmx/models/etf_sectors.py",
        68,
        '.replace(".TO"',
    ),
    (
        "OBB2-tmx-EtfCountries",
        "filter_needle_normalization",
        "upstream",
        "providers/tmx/openbb_tmx/models/etf_countries.py",
        71,
        '.replace(".TO"',
    ),
    (
        "OBB2-tmx-EtfInfo",
        "filter_needle_normalization",
        "upstream",
        "providers/tmx/openbb_tmx/models/etf_info.py",
        223,
        '.replace("-", ".")',
    ),
    (
        "OBB2-tmx-EquitySearch",
        "multi_request_concat",
        "upstream",
        "providers/tmx/openbb_tmx/utils/helpers.py",
        455,
        'get_tmx_tickers("tsxv"',
    ),
    (
        "OBB2-sec-CikMap",
        "filter_needle_normalization",
        "upstream",
        "providers/sec/openbb_sec/utils/helpers.py",
        167,
        'lstrip("0")',
    ),
    (
        "OBB2-sec-CikMap",
        "source_order_cap",
        "upstream",
        "providers/sec/openbb_sec/utils/helpers.py",
        171,
        ".iloc[0]",
    ),
    (
        "OBB2-sec-SymbolMap",
        "filter_needle_normalization",
        "upstream",
        "providers/sec/openbb_sec/utils/helpers.py",
        137,
        '.upper().replace(".", "-")',
    ),
    (
        "OBB2-sec-SymbolMap",
        "source_order_cap",
        "upstream",
        "providers/sec/openbb_sec/utils/helpers.py",
        144,
        ".iloc[0]",
    ),
    (
        "OBB2-econdb-PortVolume",
        "long_to_wide_group_pivot",
        "upstream",
        "providers/econdb/openbb_econdb/models/port_volume.py",
        148,
        'columns="measure"',
    ),
    (
        "OBB2-benzinga-PriceTarget",
        "epoch_s_to_date_conversion",
        "upstream",
        "providers/benzinga/openbb_benzinga/models/price_target.py",
        155,
        "int(v.timestamp())",
    ),
    (
        "OBB2-tradier-EquityQuote",
        "code_table_forward_lookup",
        "upstream",
        "providers/tradier/openbb_tradier/models/equity_quote.py",
        258,
        "OPTIONS_EXCHANGES.get",
    ),
    (
        "OBB2-tradier-EquityQuote",
        "single_object_array_wrap",
        "upstream",
        "providers/tradier/openbb_tradier/models/equity_quote.py",
        243,
        "isinstance(data, list)",
    ),
    (
        "OBB2-intrinio-InsiderTrading",
        "relative_date_default",
        "upstream",
        "providers/intrinio/openbb_intrinio/models/insider_trading.py",
        135,
        "relativedelta(years=5)",
    ),
    # The engine cite this round corrected from docstring prose to the refusal itself.
    (
        "OBB2-sec-CikMap",
        "cite-fix resolve_rows refusal",
        "repo",
        "opendata/data/providers/_engine/http_json.py",
        434,
        "not isinstance(current, list)",
    ),
    # The credit a pre-compaction receipt wanted removed and the disk refused to remove.
    (
        "OBB2-tmx-EquitySearch",
        "cite-keep rows_from_object_keys",
        "upstream",
        "providers/tmx/openbb_tmx/models/equity_search.py",
        56,
        "index=companies",
    ),
)

#: The one credit this round deletes, with the line that refutes it.
REFUTED_CREDIT = (
    "OBB2-tmx-EtfSearch",
    "symbol_normalization",
    "providers/tmx/openbb_tmx/models/etf_search.py",
    216,
    "return TmxEtfSearchQueryParams(**params)",
)

#: This round's declared scope. F5 fails if the census on disk differs from it, so an unnoticed row
#: edit and an abandoned repair both surface as a red face instead of a silent gap.
EDITED_ROWS = frozenset(
    {
        "OBB2-tmx-EtfSearch",
        "OBB2-tmx-EtfHoldings",
        "OBB2-tmx-EtfSectors",
        "OBB2-tmx-EtfCountries",
        "OBB2-tmx-EtfInfo",
        "OBB2-tmx-EquitySearch",
        "OBB2-sec-CikMap",
        "OBB2-sec-SymbolMap",
        "OBB2-econdb-PortVolume",
        "OBB2-benzinga-PriceTarget",
        "OBB2-tradier-EquityQuote",
        "OBB2-intrinio-InsiderTrading",
    }
)


def git_show(rev_path: str) -> str:
    """Read one path at the pinned pre-repair rev: the before-side of every delta face."""
    proc = subprocess.run(  # nosec B603, B607  # noqa: S603
        ["git", "show", f"{BASE_REV}:{rev_path}"],  # noqa: S607  # fixed argv, git resolved on PATH
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def rows_from(text: str) -> dict[str, dict[str, Any]]:
    """Index one census document by task id."""
    return {str(r["task_id"]): r for r in json.loads(text)["rows"]}


def live_rows() -> dict[str, dict[str, Any]]:
    """The repaired census as it sits on disk, file by file."""
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(CENSUS_DIR.glob("census-*.json")):
        if f.name == work_judge.CENSUS_AUDIT_NAME:
            continue
        out.update(rows_from(f.read_text(encoding="utf-8")))
    return out


def head_rows() -> dict[str, dict[str, Any]]:
    """The census as shipped, so every row delta is measured against a committed face."""
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(CENSUS_DIR.glob("census-*.json")):
        if f.name == work_judge.CENSUS_AUDIT_NAME:
            continue
        out.update(rows_from(git_show(f.relative_to(REPO_ROOT).as_posix())))
    return out


def head_judge() -> ModuleType:
    """Load the judge as committed, so F4 can rebuild the judgment the roadmap actually carried."""
    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="c95_head_judge_"))
    path = tmp_dir / "head_judge.py"
    path.write_text(git_show("scripts/quality/model_capability_census.py"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("c95_head_judge", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise SystemExit("cannot load the committed judge")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["c95_head_judge"] = mod
    spec.loader.exec_module(mod)
    return mod


def present_of(artifact: dict[str, Any]) -> set[str]:
    """The capabilities the judge reads as shipped -- taken from the artifact's own statuses."""
    return {k for k, v in artifact["capability_registry"].items() if v["status"] == "present"}


def blockers(labels: list[str], folds: dict[str, str], present: set[str]) -> set[str]:
    """The absent canonicals one row's spellings resolve to, UNMAPPED words kept visible."""
    out: set[str] = set()
    for label in labels:
        cap = folds.get(label)
        if cap is None:
            out.add(f"UNMAPPED:{label}")
        elif cap not in present:
            out.add(cap)
    return out


def carrier_lines(root: str, rel: str) -> list[str] | None:
    """Read a cite's carrier file, upstream or in-repo, returning None when it does not exist."""
    base = UPSTREAM if root == "upstream" else REPO_ROOT
    path = base / rel
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").split("\n")


def ladder_shape(roadmap: work_judge.Roadmap) -> list[tuple[str, int]]:
    """The ``(capability, rows unlocked)`` pairs the greedy cover produced, in step order."""
    return [(step.capability, step.rows_newly_unlocked) for step in roadmap.build_order]


def projection(
    rows: list[work_judge.CensusRow], readings: dict[str, work_judge.EntryReading]
) -> tuple[int, int, int, str]:
    """Run the repo's greedy cover over a SYNTHETIC tree and read its buckets back.

    Nothing is written: the rows and readings passed in are copies, so this measures what a next
    round's edit would produce without touching the census or the registry.
    """
    rm = work_judge.greedy_cover(rows, readings)
    return (
        int(rm.rows_unlocked_by_greedy_prefix),
        int(rm.rows_expressible_today),
        len(rm.rows_still_locked_after_engine_pool),
        str(rm.build_order[0].capability),
    )


def main() -> int:
    """Print every face, then the verdict, and exit non-zero if any face is red."""
    failures: list[str] = []
    artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    head_artifact = json.loads(git_show(ARTIFACT.relative_to(REPO_ROOT).as_posix()))
    live, head = live_rows(), head_rows()
    head_mod = head_judge()

    tax, htax = artifact["taxonomy"], head_artifact["taxonomy"]
    print(
        f"round_id={artifact['round_id']}  canonical {htax['canonical_capability_count']}"
        f"->{tax['canonical_capability_count']}  labels_supported "
        f"{htax['labels_supported']}->{tax['labels_supported']}  raw_labels "
        f"{htax['raw_label_count']}->{tax['raw_label_count']}  mentions "
        f"{htax['raw_label_mention_count']}->{tax['raw_label_mention_count']}"
    )

    # ---- F1 registry -------------------------------------------------------
    reg = artifact["capability_registry"]
    drifted = sorted(k for k, v in reg.items() if v["status"] == "drifted")
    print(f"F1 registry: present={len(present_of(artifact))} drifted={len(drifted)}")
    for cap in NEW_CANONICALS:
        e = reg.get(cap)
        ok = e is not None and e["status"] == "absent" and e.get("anchor_found") is not False
        print(
            f"   {cap:28s} status={None if e is None else e['status']} -> {'OK' if ok else 'FAIL'}"
        )
        if not ok:
            failures.append(f"F1 {cap} is not an anchored absent canonical")
    if drifted:
        failures.append(f"F1 drifted entries leave the pool unseen: {drifted}")

    # ---- F2 census ---------------------------------------------------------
    all_labels = sorted({lbl for r in live.values() for lbl in r["needs_engine_capability"]})
    unmapped = [lbl for lbl in all_labels if lbl not in work_judge.LABEL_TO_CAPABILITY]
    try:
        work_judge.map_needs(tuple(all_labels), "C95-union-of-all-rows")
        judge_accepted = True
    except Exception as exc:  # the judge's refusal IS the face
        judge_accepted = False
        unmapped = [str(exc)]
    print(
        f"F2 census: rows={len(live)} expected={artifact['census']['row_count_measured']} "
        f"unique_labels={len(all_labels)} judge_accepted={judge_accepted}"
    )
    if len(live) != artifact["census"]["row_count_measured"] or not judge_accepted:
        failures.append(f"F2 row count or the judge's own fold: {unmapped[:3]}")
    dead_w = {lbl for lbl in work_judge.LABEL_TO_CAPABILITY if lbl not in set(all_labels)}
    head_labels = {lbl for r in head.values() for lbl in r["needs_engine_capability"]}
    dead_h = {lbl for lbl in head_mod.LABEL_TO_CAPABILITY if lbl not in head_labels}
    new_dead = sorted(dead_w - dead_h)
    print(f"F2b fold labels no row cites: work={len(dead_w)} head={len(dead_h)} new={new_dead}")
    if new_dead:
        failures.append(f"F2b the repair added vocabulary no row uses: {new_dead}")

    # ---- F3 cite carriers --------------------------------------------------
    miss = 0
    for tid, spelling, root, rel, line, token in CITES:
        lines = carrier_lines(root, rel)
        hit = lines is not None and 0 < line <= len(lines) and token in lines[line - 1]
        if not hit:
            miss += 1
            got = (
                "<file missing>"
                if lines is None
                else (lines[line - 1] if line <= len(lines) else "<eof>")
            )
            print(f"   MISSING CARRIER {tid}/{spelling} @ {rel}:{line} want {token!r} got {got!r}")
    print(f"F3 cite carriers: checked={len(CITES)} missing={miss}")
    if miss:
        failures.append(f"F3 {miss} label(s) have no cite carrier")

    rtid, rlabel, rel_r, rline, rtoken = REFUTED_CREDIT
    rlines = carrier_lines("upstream", rel_r)
    refuted = rlines is not None and rtoken in rlines[rline - 1]
    still = rlabel in live[rtid]["needs_engine_capability"]
    print(
        f"F3b refuted credit {rtid}/{rlabel}: passthrough carrier :{rline}={refuted} "
        f"still_credited={still}"
    )
    if not (refuted and not still):
        failures.append(f"F3b {rtid}/{rlabel} removed without a refuting carrier")

    # ---- F4 monotonicity ---------------------------------------------------
    # Both present sets come from their OWN artifact, so "no capability silently flipped to
    # present" is measured rather than asserted.
    hp, wp = present_of(head_artifact), present_of(artifact)
    print(
        f"F4 present-set: head={len(hp)} work={len(wp)} identical={hp == wp} "
        f"newly_present={sorted(wp - hp)} baseline={BASE_REV}"
    )
    if hp != wp:
        failures.append(f"F4 the repair flipped capabilities to present: {sorted(wp - hp)}")
    grew = same = 0
    shrunk: list[tuple[str, list[str]]] = []
    for tid in sorted(live):
        bo = {
            x
            for x in blockers(
                head[tid]["needs_engine_capability"], head_mod.LABEL_TO_CAPABILITY, hp
            )
            if not x.startswith("UNMAPPED:")
        }
        bn = {
            x
            for x in blockers(
                live[tid]["needs_engine_capability"], work_judge.LABEL_TO_CAPABILITY, wp
            )
            if not x.startswith("UNMAPPED:")
        }
        lost, gained = bo - bn, bn - bo
        if lost:
            shrunk.append((tid, sorted(lost)))
        elif gained:
            grew += 1
        else:
            same += 1
    unexpected = [t for t, _ in shrunk if t not in {REFUTED_CREDIT[0]}]
    print(
        f"F4 monotonicity: same={same} grew={grew} shrank={len(shrunk)} "
        f"unexpected={len(unexpected)}"
    )
    for tid, gone in shrunk:
        print(f"   blocker set shrank: {tid} lost {gone}")
    if unexpected:
        failures.append(f"F4 rows made shippable without a refutation: {unexpected}")

    # ---- F4b the fold edit alone, before a single row was touched ----------
    # HEAD's own credit lists folded by each table against the same present set. Without this the
    # "the vocabulary change only ever made rows harder" claim hides inside F4's combined number,
    # where a re-fold that freed a row and a row edit that blocked it would cancel out.
    f_same = f_grew = 0
    f_lost: list[tuple[str, list[str]]] = []
    f_grew_ids: list[str] = []
    for tid in sorted(head):
        labels = head[tid]["needs_engine_capability"]
        bo = {
            x
            for x in blockers(labels, head_mod.LABEL_TO_CAPABILITY, hp)
            if not x.startswith("UNMAPPED:")
        }
        bn = {
            x
            for x in blockers(labels, work_judge.LABEL_TO_CAPABILITY, hp)
            if not x.startswith("UNMAPPED:")
        }
        if bo == bn:
            f_same += 1
        elif bo < bn:
            f_grew += 1
            f_grew_ids.append(tid)
        else:
            f_lost.append((tid, sorted(bo - bn)))
    print(
        f"F4b fold-only: same={f_same} grew={f_grew} shrank={len(f_lost)} "
        f"total={f_same + f_grew + len(f_lost)}"
    )
    if f_lost or f_same + f_grew + len(f_lost) != len(head):
        failures.append(f"F4b the fold table alone freed rows or missed one: {f_lost}")

    # ---- F5 arithmetic -----------------------------------------------------
    per_row_delta = sum(
        len(live[t]["needs_engine_capability"]) - len(head[t]["needs_engine_capability"])
        for t in live
    )
    changed = sorted(t for t in live if live[t] != head[t])
    mention_delta = tax["raw_label_mention_count"] - htax["raw_label_mention_count"]
    print(
        f"F5 arithmetic: sum(per-row delta)={per_row_delta} artifact mention delta="
        f"{mention_delta} rows_changed={len(changed)}"
    )
    if per_row_delta != mention_delta:
        failures.append(f"F5 identity broken: per-row={per_row_delta} artifact={mention_delta}")
    if set(changed) != EDITED_ROWS:
        failures.append(f"F5 edited-row set is not this round's scope: {changed}")
    if not changed:
        failures.append(
            f"F5 nothing differs from baseline {BASE_REV}: the pin points at the repaired tree, "
            "so F4/F4b/F5 are comparing a face with itself"
        )

    # ---- F6 ladder ---------------------------------------------------------
    rm = artifact["roadmap"]
    total = (
        rm["rows_unlocked_by_greedy_prefix"]
        + rm["rows_expressible_today"]
        + len(rm["rows_still_locked_after_engine_pool"])
    )
    print(
        f"F6 ladder: {rm['rows_unlocked_by_greedy_prefix']}+{rm['rows_expressible_today']}"
        f"+{len(rm['rows_still_locked_after_engine_pool'])}={total} "
        f"census={artifact['census']['row_count_measured']}"
    )
    if total != artifact["census"]["row_count_measured"]:
        failures.append("F6 the ladder does not partition the census")

    # ---- F7 the published ladder is reproducible ---------------------------
    # The judge is re-run on the census and registry as they sit on disk right now. If the artifact
    # carries a ladder the current tree does not produce -- an edited row, a stale round, a hand
    # typed order -- the two lists part and the roadmap is not evidence.
    report = work_judge.run(round_id="C95-face-recompute")
    rec = [
        (s.capability, s.rows_newly_unlocked, s.cumulative_rows_unlocked)
        for s in report.roadmap.build_order
    ]
    art = [
        (s["capability"], s["rows_newly_unlocked"], s["cumulative_rows_unlocked"])
        for s in rm["greedy_build_order"]
    ]
    at = next((i for i, (x, y) in enumerate(zip(rec, art, strict=False)) if x != y), None)
    if rec != art and at is None:
        at = min(len(rec), len(art))
    print(f"F7 ladder recompute: steps={len(art)} reproducible={rec == art} first_divergence={at}")
    if rec != art:
        failures.append(
            f"F7 the artifact ladder is not what the tree produces: "
            f"steps {len(rec)} vs {len(art)}, diverging at step {at}"
        )

    # The dispatch brief snapshots the front four steps; it must still describe THIS artifact.
    front = json.loads(FRONT.read_text(encoding="utf-8"))
    fc = front["clusters"]

    def front_rows_agree(i: int) -> bool:
        """One cluster's row set against the artifact's own newly-unlocked list for that step."""
        if i >= len(rm["greedy_build_order"]):
            return False
        want = {x["task_id"] for x in rm["greedy_build_order"][i]["newly_unlocked_rows"]}
        return {str(r["task_id"]) for r in fc[i]["rows"]} == want

    f_at = next(
        (
            i
            for i, c in enumerate(fc)
            if i >= len(art)
            or c["capability"] != art[i][0]
            or c["rows_newly_unlocked"] != art[i][1]
        ),
        None,
    )
    f_rows = all(front_rows_agree(i) for i in range(len(fc)))
    f_round = front["round"] == artifact["round_id"]
    f_present = front["present_capabilities"] == sorted(present_of(artifact))
    print(
        f"F7b dispatch brief: clusters={len(fc)} front_matches={f_at is None} "
        f"rows_match={f_rows} round={f_round} present_match={f_present}"
    )
    if f_at is not None or not (f_rows and f_round and f_present):
        failures.append(
            f"F7b {FRONT.name} is stale against the shipped ladder "
            f"(step {f_at}, rows_match={f_rows}, round={f_round}, present={f_present})"
        )

    # ---- F8 next-round projection ------------------------------------------
    # A kernel round makes two distinct moves, and they have different faces. Registering the
    # capability present leaves the row buckets alone -- ``expressible_today`` is a STORED census
    # field, so only the ladder front re-lights -- and the buckets move when the rows themselves are
    # re-derived. Both arms run through the repo's own greedy cover, so §6 cannot publish a triple
    # the F6 partition forbids.
    p_rows = work_judge.load_rows(CENSUS_DIR)
    p_step = report.roadmap.build_order[0]
    p_cap = str(p_step.capability)
    p_ids = {str(r.task_id) for r in p_step.newly_unlocked_rows}
    p_readings = {
        cap: (replace(rd, present=True) if cap == p_cap else rd)
        for cap, rd in report.readings.items()
    }
    as_is = projection(p_rows, report.readings)
    arm_reg = projection(p_rows, p_readings)
    arm_re = projection(
        [replace(r, expressible_today=True) if r.task_id in p_ids else r for r in p_rows],
        p_readings,
    )
    row_total = int(artifact["census"]["row_count_measured"])
    print(f"F8 projection target: {p_cap} credit rows={len(p_ids)} {sorted(p_ids)[:3]}")
    for name, arm in (("AS-IS", as_is), ("register-only", arm_reg), ("register+re-derive", arm_re)):
        print(
            f"   {name:<19} prefix={arm[0]} expressible={arm[1]} locked={arm[2]} "
            f"sum={arm[0] + arm[1] + arm[2]} front={arm[3]}"
        )
    if as_is != (
        rm["rows_unlocked_by_greedy_prefix"],
        rm["rows_expressible_today"],
        len(rm["rows_still_locked_after_engine_pool"]),
        art[0][0],
    ):
        failures.append(f"F8 the synthetic baseline is not the shipped tree: {as_is}")
    if arm_reg[1] != as_is[1] or arm_reg[3] == as_is[3]:
        failures.append(
            f"F8 registering {p_cap} present must move the FRONT only, not the stored bucket: "
            f"{as_is} -> {arm_reg}"
        )
    if arm_re[1] != as_is[1] + len(p_ids) or arm_re[0] != as_is[0] - len(p_ids):
        failures.append(
            f"F8 the +{len(p_ids)} expressible must come out of the prefix: "
            f"prefix {as_is[0]}->{arm_re[0]}, expressible {as_is[1]}->{arm_re[1]}"
        )
    for name, arm in (("register-only", arm_reg), ("register+re-derive", arm_re)):
        if arm[0] + arm[1] + arm[2] != row_total:
            failures.append(f"F8 {name} breaks the {row_total}-row partition: {arm}")

    # ---- counterfacts ------------------------------------------------------
    print("--- counterfacts ---")
    forge_tid = "OBB2-tmx-EtfSectors"
    wl = list(live[forge_tid]["needs_engine_capability"])
    forged_labels = [x for x in wl if x != "filter_needle_normalization"]
    base = blockers(wl, work_judge.LABEL_TO_CAPABILITY, wp)
    forged = blockers(forged_labels, work_judge.LABEL_TO_CAPABILITY, wp)
    c1 = bool(base - forged)
    print(f"C1 forged blocker-removal on {forge_tid}: lost={sorted(base - forged)} -> {c1}")
    if not c1:
        failures.append("C1 F4 cannot see a row losing a blocker -- it is decorative")

    try:
        work_judge.map_needs(("no_such_spelling_c95",), "C95-probe")
        c2 = False
        print("C2 the judge ACCEPTED an unknown spelling -> F2 is not the judge's face")
    except Exception as exc:  # the raise IS the face
        c2 = True
        print(f"C2 the judge refuses an unmapped spelling: {type(exc).__name__}")
    if not c2:
        failures.append("C2 F2 does not measure the judge's fail-closed fold")

    _t, _s, _r, rel0, line0, tok0 = CITES[0]
    ln0 = carrier_lines(_r, rel0)
    neighbour = ln0[line0 - 2] if ln0 is not None and line0 >= 2 else ""
    c3 = tok0 not in neighbour
    print(f"C3 cite moved one line off {rel0}:{line0}-1 -> token_absent={c3}")
    if not c3:
        failures.append("C3 F3 cannot tell the cited line from its neighbour")

    step0 = report.roadmap.build_order[0]
    c4_tid = str(step0.newly_unlocked_rows[0].task_id)
    forged_rows = [
        replace(r, needs=frozenset(n for n in r.needs if n != step0.capability))
        if r.task_id == c4_tid
        else r
        for r in work_judge.load_rows(CENSUS_DIR)
    ]
    fz = work_judge.greedy_cover(forged_rows, report.readings)
    c4 = ladder_shape(fz) != ladder_shape(report.roadmap)
    print(
        f"C4 forged credit-list on {c4_tid} (dropped {step0.capability}): front_moved={c4} "
        f"step0={ladder_shape(report.roadmap)[0]}->{ladder_shape(fz)[0]}"
    )
    if not c4:
        failures.append("C4 F7 cannot see a row losing its only blocker -- it is decorative")

    if not f_grew_ids:
        print(f"C5 SKIPPED: no fold-grown row at baseline {BASE_REV}, so F4b has no arm to build")
        failures.append(
            f"C5 has no grown row against {BASE_REV}: F4b's grew count is unattributable here"
        )
    else:
        c5_tid = f_grew_ids[0]
        c5_labels = head[c5_tid]["needs_engine_capability"]
        reverted = dict(work_judge.LABEL_TO_CAPABILITY)
        for lbl in c5_labels:
            if lbl in reverted:
                reverted[lbl] = head_mod.LABEL_TO_CAPABILITY.get(lbl, reverted[lbl])
        c5_set = blockers(c5_labels, reverted, hp)
        w_set = blockers(c5_labels, work_judge.LABEL_TO_CAPABILITY, hp)
        c5 = c5_set < w_set
        print(
            f"C5 fold reverted on {c5_tid}: blockers {sorted(w_set)} -> {sorted(c5_set)} "
            f"shrank={c5}"
        )
        if not c5:
            failures.append(
                "C5 F4b's grew count is not caused by the fold table -- it is decorative"
            )

    c6_tid = sorted(p_ids)[0]
    c6_arm = projection(
        [replace(r, expressible_today=True) if r.task_id == c6_tid else r for r in p_rows],
        p_readings,
    )
    c6 = c6_arm != arm_re and c6_arm[1] == as_is[1] + 1 and c6_arm[0] == as_is[0] - 1
    print(
        f"C6 re-derive one row ({c6_tid}) instead of all {len(p_ids)}: "
        f"prefix {as_is[0]}->{c6_arm[0]} expressible {as_is[1]}->{c6_arm[1]} "
        f"differs_from_full_arm={c6}"
    )
    if not c6:
        failures.append(
            "C6 F8's bucket move is not caused by the rows it flips -- the projection is a constant"
        )

    print("\nVERDICT:", "PASS" if not failures else "FAIL")
    for f in failures:
        print(f"  - {f}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
