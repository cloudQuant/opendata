"""C89 tmx witness: the four shipped-cover rows the census under-declared, now locked.

One face per line, each arm paired with a reading that must differ. The judge (registry readings and
``greedy_cover``) is held fixed at this working tree; only the census need sets vary, so every move
below is attributable to the need-set edit and not to a taxonomy change.

G -- the roadmap face: HEAD's four rows come back into ``shipped_cover_rows`` when the file is
   reverted, and the working tree publishes none of them.
H -- the note is the oracle: every capability a row's own cite-bearing note names must be in that
   row's need set, in both directions.
I -- the lock is load-bearing: stripping the three added capabilities from a row's need set must put
   the row back in cover (a label that cannot do this is not blocking anything).
J -- provenance: ``audited.prior_*`` repeats HEAD byte-for-byte, ``expressible_today`` never moved.
K -- scope: exactly four rows changed in this file, and only in the three repaired keys.
U -- upstream literals: each cited window really carries its label's token (outside the repo, so
   this arm prints NOT JUDGED rather than pretending when the monorepo is not readable here).

Run: PYTHONPATH=. python docs/evidence/C89/tmx-needs-witness.py
"""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import scripts.quality.model_capability_census as mcc  # noqa: E402

CENSUS_REL = (
    "docs/\u8fed\u4ee3\u8ba1\u5212/\u8fed\u4ee32-\u7edf\u4e00Provider\u67b6\u6784\u4e0e"
    "\u5168\u91cf\u80fd\u529b\u8865\u9f50/census-sec-tmx-fed-gov-finra.json"
)
CENSUS = ROOT / CENSUS_REL
UPSTREAM = Path(
    "/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers/tmx/openbb_tmx"
)
REPAIRED = (
    "OBB2-tmx-EtfHoldings",
    "OBB2-tmx-EtfSectors",
    "OBB2-tmx-EtfCountries",
    "OBB2-tmx-EtfInfo",
)
ADDED = ("symbol_normalization", "sentinel_value_nulling", "percent_column_scaling")
TOKENS = {
    "symbol_normalization": ("upper", ".TO"),
    "sentinel_value_nulling": ("None", "N/A"),
    "percent_column_scaling": ("/ 100",),
}
CITE = re.compile(r"([a-z_]+\.py)?:(\d+)(?:-(\d+))?")


def json_rows(path_text: str) -> dict[str, dict]:
    """Index one census document by task id."""
    return {r["task_id"]: r for r in json.loads(path_text)["rows"]}


def head_text() -> str:
    """HEAD's copy of the census file, so 'before' is the repo's own bytes and not my memory."""
    cmd = ["git", "show", f"HEAD:{CENSUS_REL}"]
    out = subprocess.run(  # noqa: S603  # nosec B603 固定 argv，无外部输入
        cmd, cwd=ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout


def to_row(item: dict) -> mcc.CensusRow:
    """Rebuild a judge row from a census object, mapping labels through the live table."""
    labels = tuple(str(label) for label in item["needs_engine_capability"])
    return mcc.CensusRow(
        task_id=item["task_id"],
        provider=str(item.get("provider", "")),
        upstream_model=str(item.get("upstream_model", "")),
        raw_labels=labels,
        needs=mcc.map_needs(labels, item["task_id"]),
        expressible_today=item.get("expressible_today") is True,
    )


def all_rows(work_items: dict[str, dict]) -> list[mcc.CensusRow]:
    """The 279-row denominator with this file's rows taken from ``work_items``."""
    other = [
        r for r in mcc.load_rows(mcc._REPO_ROOT / mcc.CENSUS_DIR_REL) if r.task_id not in work_items
    ]
    return other + [to_row(work_items[t]) for t in sorted(work_items)]


def cover_of(rows: list[mcc.CensusRow], readings: dict[str, mcc.EntryReading]) -> list[str]:
    """Task ids the roadmap publishes as already covered by shipped capabilities."""
    return sorted(row.task_id for row in mcc.greedy_cover(rows, readings).shipped_cover)


def windows_for(note: str, label: str) -> list[tuple[str, int, int]]:
    """The ``label [file.py:1-2, file.py:3]`` citation group of one label in a row note."""
    match = re.search(rf"{label} \[([^\]]*)\]", note)
    if match is None:
        return []
    out: list[tuple[str, int, int]] = []
    current = ""
    for fname, start, end in CITE.findall(match.group(1)):
        current = fname or current
        out.append((current, int(start), int(end or start)))
    return out


def upstream_files() -> dict[str, Path]:
    """Index every upstream tmx module by file name (helpers live under utils/, not models/)."""
    return {p.name: p for p in sorted(UPSTREAM.rglob("*.py"))}


def main() -> int:
    """Run every arm, print one line each, and exit non-zero when an arm refuses to differ."""
    failures: list[str] = []
    upstream_skipped = not UPSTREAM.is_dir()

    def check(label: str, holds: bool, detail: str) -> None:
        print(f"{label:32s} {detail}")
        if not holds:
            failures.append(label)

    declarations, _, _ = mcc.count_declarations(mcc.REGISTRY, mcc._REPO_ROOT)
    readings = mcc.verify_registry(mcc.REGISTRY, mcc._REPO_ROOT, declarations)
    work = json_rows(CENSUS.read_text(encoding="utf-8"))
    base = json_rows(head_text())

    # ---- G: the cover face, HEAD vs working copy, one judge -----------------------------------
    head_ids = [task for task in REPAIRED if task in base]
    check("G0 all four rows located", len(head_ids) == 4, f"ids={head_ids}")
    rows_head = all_rows(
        {t: base[t] for t in REPAIRED} | {t: work[t] for t in work if t not in REPAIRED}
    )
    cover_head = cover_of(rows_head, readings)
    cover_work = cover_of(all_rows(work), readings)
    tmx_head = sorted(t for t in cover_head if t in REPAIRED)
    tmx_work = sorted(t for t in cover_work if t in REPAIRED)
    check(
        "G1 HEAD cover holds the four",
        tmx_head == sorted(REPAIRED),
        f"cover(HEAD rows)={len(cover_head)} tmx-in-cover={tmx_head}",
    )
    check(
        "G2 working cover holds none",
        not tmx_work and len(cover_work) == len(cover_head) - 4,
        f"cover(work)={len(cover_work)} tmx-in-cover={tmx_work or 'none'}",
    )

    # ---- H: the row note names exactly the added blockers -------------------------------------
    for task in REPAIRED:
        short = task.split("-")[-1]
        note, needs = work[task]["notes"], work[task]["needs_engine_capability"]
        named = [lab for lab in ADDED if f"{lab} [" in note]
        check(
            f"H1 {short:12s} note names adds",
            sorted(named) == sorted(ADDED) and set(named) <= set(needs),
            f"named={sorted(named)} needs={len(needs)}",
        )
        added_in_needs = [lab for lab in ADDED if lab in needs]
        check(
            f"H2 {short:12s} adds are cited",
            all(lab in note for lab in added_in_needs),
            f"added in needs={len(added_in_needs)} of them named in the note="
            f"{sum(1 for lab in added_in_needs if lab in note)}",
        )
        refused = {"symbol_fan_out", "client_side_column_drop"} & set(needs)
        check(
            f"H3 {short:12s} mislabels refused",
            not refused and "NO LABEL FOR" in note,
            f"fan_out/column_drop present in needs={sorted(refused) or 'none'}",
        )

    # ---- I: each added capability is what keeps the row out of cover --------------------------
    blocking = frozenset(mcc.LABEL_TO_CAPABILITY[lab] for lab in ADDED)
    for task in REPAIRED:
        short = task.split("-")[-1]
        row = to_row(work[task])
        stripped = replace(row, needs=row.needs - blocking)
        rows = [r if r.task_id != task else stripped for r in all_rows(work)]
        back = task in cover_of(rows, readings)
        present_only = replace(row, needs=row.needs - {"param.value_transform"})
        rows2 = [r if r.task_id != task else present_only for r in all_rows(work)]
        still = task not in cover_of(rows2, readings)
        check(
            f"I1 {short:12s} strip returns it",
            back,
            f"needs minus the 3 added caps -> back in cover={back}",
        )
        check(
            f"I2 {short:12s} one cap suffices",
            still,
            f"needs minus only param.value_transform -> still locked={still}",
        )

    # ---- J: provenance repeats HEAD, and no flag was flipped ----------------------------------
    for task in REPAIRED:
        short = task.split("-")[-1]
        aud = work[task].get("audited", {})
        check(
            f"J1 {short:12s} prior_* == HEAD",
            aud.get("prior_needs_engine_capability") == base[task]["needs_engine_capability"]
            and aud.get("prior_notes") == base[task]["notes"]
            and aud.get("prior_evidence") == base[task]["evidence"],
            f"prior needs={len(aud.get('prior_needs_engine_capability', []))} notes verbatim="
            f"{aud.get('prior_notes') == base[task]['notes']}",
        )
        check(
            f"J2 {short:12s} expressible_today unmoved",
            work[task]["expressible_today"] is False and base[task]["expressible_today"] is False,
            f"HEAD={base[task]['expressible_today']} now={work[task]['expressible_today']}",
        )

    # ---- K: the edit stayed inside four rows and three keys -----------------------------------
    moved = {
        t: sorted(k for k in set(work[t]) | set(base[t]) if work[t].get(k) != base[t].get(k))
        for t in work
        if work[t].get("needs_engine_capability") != base[t].get("needs_engine_capability")
        or work[t].get("notes") != base[t].get("notes")
        or work[t].get("audited") != base[t].get("audited")
        or set(work[t]) != set(base[t])
    }
    allowed = {"needs_engine_capability", "notes", "audited"}
    check(
        "K1 exactly the four moved",
        set(moved) == set(REPAIRED) and all(set(k) <= allowed for k in moved.values()),
        f"moved={dict(sorted(moved.items()))}",
    )
    other_keys = [t for t, k in moved.items() if any(x not in allowed for x in k)]
    check("K2 no other key touched", not other_keys, f"offenders={other_keys or 'none'}")

    # ---- U: the cited windows carry the token (needs the monorepo) ----------------------------
    files = upstream_files() if not upstream_skipped else {}
    judged = 0
    for task in REPAIRED:
        short = task.split("-")[-1]
        for label in ADDED:
            wins = windows_for(work[task]["notes"], label)
            if upstream_skipped:
                continue
            bodies = []
            sane = True
            head3 = ""
            for fname, start, end in wins:
                path = files.get(fname)
                if path is None:
                    sane = False
                    continue
                lines = path.read_text(encoding="utf-8").splitlines()
                sane = sane and 1 <= start <= end <= len(lines)
                bodies.append("\n".join(lines[start - 1 : end]))
                head3 = "\n".join(lines[:3])
            hit = any(any(tok in body for tok in TOKENS[label]) for body in bodies)
            control = not any(tok in head3 for tok in TOKENS[label])
            judged += 1
            check(
                f"U {short:12s} {label[:17]:17s}",
                sane and hit and control,
                f"windows={len(wins)} in_range={sane} token_in_window={hit} docstring={control}",
            )
    if upstream_skipped:
        print(f"U-ARMS: NOT JUDGED -- {UPSTREAM} is not readable from this machine")
    else:
        print(f"U-ARMS judged: {judged} (every add has a window carrying its token)")

    print()
    if failures:
        print(f"WITNESS FAIL: {len(failures)} arm(s) did not behave: {failures}")
        return 1
    print("ALL ARMS: every face has one pass reading and one differing damage reading")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
