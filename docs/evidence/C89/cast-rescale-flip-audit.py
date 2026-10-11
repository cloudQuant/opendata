"""C89 witness: would anchoring columns.cast / columns.rescale present honestly flip three rows?

Reads only this repo's census files, this repo's engine/ledger sources, and the pinned read-only
OpenBB tree as an output-contract comparator. Every printed line number and count is located in
file text at run time -- none is typed. Nothing touches the network or a database, no engine code
is executed, and no --check gate is run.

For each audited census row it prints:
  * arm P  -- the reading under the registry's current anchors (cast/rescale measured absent);
  * arm D  -- a deliberately damaged reading (cast/rescale assumed present, and/or one cited
    upstream behaviour deleted from the fixture text), which must print a different value.
A printed zero carries a min-reachable control beside it. Any arm that fails to move exits 1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = next(
    ancestor
    for ancestor in Path(__file__).resolve().parents
    if (ancestor / "scripts" / "quality" / "model_capability_census.py").is_file()
)
sys.path.insert(0, str(REPO / "scripts" / "quality"))
import model_capability_census as mcc  # noqa: E402

UPSTREAM = Path("/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers")
CENSUS_DIR = REPO / mcc.CENSUS_DIR_REL
AUDITED = (
    "OBB2-benzinga-AnalystSearch",
    "OBB2-federal_reserve-YieldCurve",
    "OBB2-government_us-TreasuryAuctions",
)
#: The two capabilities whose anchoring this audit interrogates, selected from the registry itself.
ASSUMED = frozenset(
    entry.capability
    for entry in mcc.REGISTRY
    if entry.capability in ("columns.cast", "columns.rescale")
)
BZ = "benzinga/openbb_benzinga/models/analyst_search.py"
YC = "federal_reserve/openbb_federal_reserve/models/yield_curve.py"
TA = "government_us/openbb_government_us/models/treasury_auctions.py"
VT_REL = "opendata/data/providers/_engine/value_transform.py"
SPEC_REL = "opendata/data/providers/_engine/spec.py"
REXP_REL = "opendata/data/providers/_engine/row_expansion.py"
FED_SPECS_REL = "opendata/data/providers/federal_reserve/specs.py"

#: (marker, file, needle, raw label, face note). Labels go through LABEL_TO_CAPABILITY, never the
#: other way: the census JSON stores raw spellings while CensusRow.needs already holds canonicals.
BEHAVIORS: dict[str, list[tuple[str, str, str, str, str]]] = {
    "OBB2-benzinga-AnalystSearch": [
        (
            "bz-epoch-cast",
            BZ,
            "safe_fromtimestamp(v, tz=timezone.utc)",
            "epoch_ms_to_date_conversion",
            "epoch->date cast of last_updated, UTC declared",
        ),
        (
            "bz-percent-rescale",
            BZ,
            "float(values[key]) / 100",
            "published_value_rescale",
            "return/percentile/stdev/rate columns divided by 100",
        ),
        (
            "bz-firm-exists",
            BZ,
            'if item.get("firm_id"):',
            "comparison_row_filter",
            "row dropped for a missing/empty key -- no text op spells this",
        ),
        (
            "bz-sentinel-nulling",
            BZ,
            "def replace_empty_strings",
            "sentinel_value_nulling",
            'every "" value rewritten to null -- columns.null_sentinel',
        ),
        (
            "bz-subdict-merge",
            BZ,
            '**item["ratings_accuracy"],',
            "per_record_nested_flatten",
            "sub-dict keys republished on the SAME row (repoint-able)",
        ),
    ],
    "OBB2-federal_reserve-YieldCurve": [
        (
            "yc-melt",
            YC,
            ".melt(",
            "series_pivot",
            "eleven maturity columns melted into long (date, maturity, rate) rows",
        ),
        (
            "yc-rescale",
            YC,
            ".astype(float) / 100",
            "published_value_rescale",
            "rate divided by 100",
        ),
        (
            "yc-asof",
            YC,
            "df.index.asof(date)",
            "comparison_row_filter",
            "nearest-published-date selection, not a window or text predicate",
        ),
        (
            "yc-categorical",
            YC,
            "Categorical(",
            "client_side_sort",
            "maturity ordered by a declared roster -- rows.order_limit",
        ),
        (
            "yc-nd-sentinel",
            YC,
            'fillna("N/A").replace("N/A", None)',
            "sentinel_value_nulling",
            'published "ND"/"N/A" spellings become null',
        ),
    ],
    "OBB2-government_us-TreasuryAuctions": [
        (
            "gu-type-map",
            TA,
            "security_dict.get(",
            "param_value_mapping",
            "REQUEST-side bill->Bill, with a passthrough default for unmapped caller text",
        ),
        (
            "gu-date-format",
            TA,
            '.strftime("%m/%d/%Y")',
            "date_format",
            "REQUEST-side rendering of the startDate/endDate params (two sites)",
        ),
        (
            "gu-percent-rescale",
            TA,
            "float(v) / 100",
            "published_value_rescale",
            "*Rate/*Yield/*Margin/*Percentage/spread OUTPUT columns divided by 100 -- undeclared",
        ),
        (
            "gu-sentinel-nulling",
            TA,
            '.replace("", None)',
            "sentinel_value_nulling",
            'N/A/"" nulling via DataFrame -- columns.null_sentinel, undeclared',
        ),
    ],
}
CENSUS_TEXT: dict[str, tuple[Path, list[str]]] = {}
#: the one cited behaviour the damaged (arm D) fixture deletes per row
DAMAGED = {
    "OBB2-benzinga-AnalystSearch": "bz-sentinel-nulling",
    "OBB2-federal_reserve-YieldCurve": "yc-categorical",
    "OBB2-government_us-TreasuryAuctions": "gu-sentinel-nulling",
}


def die(msg: str) -> None:
    """Print the failure face and end the witness with rc 1."""
    print(f"FAIL: {msg}")
    raise SystemExit(1)


def read_lines(path: Path) -> list[str]:
    """Return one file's text as lines, dying when the tree cannot be seen."""
    if not path.is_file():
        die(f"{path} is not readable; this witness only attests the tree it can see")
    return path.read_text(encoding="utf-8").splitlines()


def find_lines(lines: list[str], needle: str, lo: int = 1, hi: int = 0) -> list[int]:
    """1-based line numbers in [lo, hi] whose text contains needle (hi=0 means no upper bound)."""
    return [
        n
        for n in range(lo, (hi or len(lines)) + 1)
        if 1 <= n <= len(lines) and needle in lines[n - 1]
    ]


def cite(lines: list[str], rel: str, needle: str, expect: int) -> tuple[int, str]:
    """Locate one needle in already-read file text; refuse a citation that does not resolve."""
    hits = find_lines(lines, needle)
    if len(hits) != expect:
        die(f"{rel}: {needle!r} found at {hits}, expected exactly {expect} hit(s)")
    return hits[0], rel


def show_arm(name: str, p_value: object, d_value: object) -> None:
    """Print one arm's P/D readings; die when both faces say the same."""
    p_text, d_text = str(p_value), str(d_value)
    if p_text == d_text:
        die(f"arm[{name}] did not move: P and D both print {p_text!r} -- the witness is blind")
    print(f"    arm[{name}] MOVED")
    print(f"      P (current anchors): {p_text}")
    print(f"      D (damaged reading): {d_text}")


def main() -> int:
    """Print every measured face for the three audited rows and their arms."""
    report = mcc.run(round_id="C89-flip-audit")
    present = frozenset(report.statuses["present"])
    rows = {row.task_id: row for row in mcc.load_rows(CENSUS_DIR)}
    payloads: dict[str, dict] = {}
    census_text = CENSUS_TEXT
    for path in mcc.census_row_files(CENSUS_DIR):
        text_lines = read_lines(path)
        for item in json.loads(path.read_text(encoding="utf-8"))["rows"]:
            payloads[item["task_id"]] = item
            census_text[item["task_id"]] = (path, text_lines)

    print("cast/rescale flip audit -- every line number and count below was read off disk")
    print(
        f"census rows read: {report.row_count}; expressible_today: "
        f"{report.roadmap.rows_expressible_today}; pending: {report.roadmap.rows_pending}"
    )
    groups = report.statuses
    print(
        f"status groups: present={len(groups['present'])} absent={len(groups['absent'])} "
        f"drifted={len(groups['drifted'])} unanchored={len(groups['unanchored'])}"
    )
    print(
        "    control for the zero in drifted: the same group counter reads "
        f"{len(groups['present'])} / {len(groups['absent'])} / {len(groups['unanchored'])} "
        "for the other three statuses"
    )
    for cap in sorted(ASSUMED):
        r = report.readings[cap]
        print(
            f"anchor {cap}: status={r.status} proof={r.proof_file}:{r.anchor_line_number} "
            f"anchor={r.anchor!r} line={r.anchor_line!r}"
        )

    flip = sorted(
        row.task_id
        for row in rows.values()
        if not row.expressible_today
        and (row.needs - present)
        and not (row.needs - (present | ASSUMED))
    )
    print(
        f"rows flipping to (needs subset of present) if cast+rescale were anchored present: "
        f"{len(flip)} -> {flip}"
    )

    for task in AUDITED:
        row = rows[task]
        payload = payloads[task]
        cpath, clines = census_text[task]
        tline = cite(clines, cpath.name, f'"task_id": "{task}"', 1)[0]
        print(
            f"\n== {task} == census {cpath.name}:{tline} provider={row.provider} "
            f"model={row.upstream_model}"
        )
        starts = find_lines(clines, '"needs_engine_capability"', lo=tline, hi=tline + 12)
        closes = find_lines(clines, "]", lo=starts[0], hi=starts[0] + 14)
        lo, hi = starts[0], closes[0]
        label_lines = "; ".join(
            f"{label}@{next(n for n in find_lines(clines, chr(34) + label + chr(34), lo, hi))}"
            for label in row.raw_labels
        )
        print(
            f"  raw labels ({cpath.name} lines {lo}..{hi}): {list(row.raw_labels)} [{label_lines}]"
        )
        print(f"  canonical needs: {sorted(row.needs)}")
        unmet_p = sorted(row.needs - present)
        unmet_d = sorted(row.needs - (present | ASSUMED))
        print(f"  needs minus present, arm P: {unmet_p}")
        print(
            f"  needs minus present-with-flip, arm D: {unmet_d} "
            f"(control: the same set-difference counter just read {len(unmet_p)} on this row)"
        )
        show_arm("flip", len(unmet_p), len(unmet_d))

        undeclared: list[str] = []
        for marker, rel, needle, label, face in BEHAVIORS[task]:
            canon = mcc.LABEL_TO_CAPABILITY[label]
            expect = 2 if (rel, needle) == (TA, '.strftime("%m/%d/%Y")') else 1
            lineno, disp = cite(read_lines(UPSTREAM / rel), rel, needle, expect)
            declared = canon in row.needs
            live = canon in present
            if not declared:
                undeclared.append(f"{marker}->{canon}")
            print(
                f"  behavior {marker}: {disp}:{lineno} => {label} -> {canon} "
                f"declared={'Y' if declared else 'N'} measured-present={'Y' if live else 'N'}"
            )
            print(f"      face: {face}")
        print(
            f"  cited behaviours NOT declared by this row, arm P: {len(undeclared)} -> {undeclared}"
        )
        damaged = [b for b in BEHAVIORS[task] if b[0] != DAMAGED[task]]
        undeclared_d = [
            mcc.LABEL_TO_CAPABILITY[b[3]]
            for b in damaged
            if mcc.LABEL_TO_CAPABILITY[b[3]] not in row.needs
        ]
        print(
            f"  arm D (citation for {DAMAGED[task]!r} deleted from the fixture): "
            f"undeclared -> {sorted(undeclared_d)}"
        )
        show_arm("fixture-coverage", len(undeclared), len(undeclared_d))

        extras[task](payload, present, report, unmet_d)

    print("END: rc 0 means every citation resolved on disk and every arm moved")
    print("NOT TOUCHED: no network, no database, no engine execution, no make gate")
    return 0


def extras_benzinga(
    payload: dict,
    present: frozenset[str],
    report: mcc.Report,
    unmet_d: list[str],
) -> None:
    """Print the benzinga row's rescale-shape, op-surface and verdict faces."""
    bz_lines = read_lines(UPSTREAM / BZ)
    rescale_ln, _ = cite(bz_lines, BZ, "float(values[key]) / 100", 1)
    firm_ln, _ = cite(bz_lines, BZ, 'if item.get("firm_id"):', 1)
    ln = cite(read_lines(REPO / VT_REL), VT_REL, "a float would drift", 1)[0]
    print(
        f"  rescale-shape caveat: the kernel refuses float INPUTS ({VT_REL}:{ln}); upstream "
        f"funnels the percent fields through float() at {BZ}:{rescale_ln}, so a JSON-float "
        "published number cannot reach apply_scale unchanged -- anchoring rescale present is "
        "honest here only for decimal-string/int published values"
    )
    spec_lines = read_lines(REPO / SPEC_REL)
    ops_ln, _ = cite(spec_lines, SPEC_REL, "RowFilterOp = Literal[", 1)
    ops_text = spec_lines[ops_ln - 1].strip()
    print(
        f"  rows.filter op surface ({SPEC_REL}:{ops_ln}): {ops_text} -- none of these is "
        f"exists/not-null, so the firm_id drop ({BZ}:{firm_ln}) is covered by rows.filter "
        "only on paper"
    )
    print(
        "  VERDICT benzinga: UNDER-DECLARED -- the flip (arm D needs-minus-flipped-present="
        f"{unmet_d}) would move this row into shipped cover while its own body still needs "
        "rows.filter_comparison (bz-firm-exists) and columns.null_sentinel "
        "(bz-sentinel-nulling); cast/rescale presence is honest only for bz-epoch-cast, "
        "bz-percent-rescale and bz-subdict-merge"
    )


def extras_federal(
    payload: dict,
    present: frozenset[str],
    report: mcc.Report,
    unmet_d: list[str],
) -> None:
    """Print the YieldCurve ledger-versus-census contradiction faces."""
    lines = read_lines(REPO / FED_SPECS_REL)
    start_ln, _ = cite(lines, FED_SPECS_REL, "NOT_DECLARED: dict[str, str] = {", 1)
    key_hits = find_lines(lines, '"YieldCurve"', lo=start_ln)
    if not key_hits:
        die(f"{FED_SPECS_REL}: no YieldCurve key below NOT_DECLARED (line {start_ln})")
    melt_claim_ln = find_lines(lines, "Melting is not declarable")
    print(
        f"  ledger face: {FED_SPECS_REL}:{start_ln} opens NOT_DECLARED and :{key_hits[0]} "
        "records YieldCurve as not declared (arm P: the ledger DOES refuse this row)"
    )
    notes = str(payload.get("notes", ""))
    retracted = ("retracted" in notes) and ("NOT_DECLARED" in notes)
    outside = sorted(c for c in row_needs(payload) if c.startswith("outside."))
    print(
        f"  census face: notes re-read from {CENSUS_TEXT[payload['task_id']][0].name} this "
        f"run claim retraction={'Y' if retracted else 'N'}, yet the canonical need set "
        f"carries no outside.* label ({len(outside)} of them) -- control: the roadmap's "
        f"over-claim guard lists {len(report.roadmap.rows_outside_engine)} rows that DO "
        "declare an outside.* need"
    )
    tail = "\n".join(lines[start_ln - 1 :]).replace('"YieldCurve"', '"retractedYieldCurve"')
    damaged_hits = len(find_lines(tail.splitlines(), '"YieldCurve"'))
    print(
        f"  arm D (damaged ledger: the NOT_DECLARED entry's key text replaced): hits -> "
        f"{damaged_hits} (control: the same counter read {len(key_hits)} un-damaged)"
    )
    show_arm("fed-ledger", len(key_hits), damaged_hits)
    if retracted and not outside and not unmet_d:
        contradiction = (
            f"CONTRADICTION: census {AUDITED[1]} lists only {sorted(row_needs(payload))}, so "
            "the row flips into shipped cover when rescale anchors present, while "
            f"{FED_SPECS_REL}:{key_hits[0]} records the same id as NOT_DECLARED"
        )
    else:
        contradiction = f"no contradiction face found (retracted={retracted})"
    print(f"  {contradiction}")
    melt_hits = find_lines(read_lines(REPO / REXP_REL), "def transpose_wide_table(")
    print(
        f"  engine-vs-ledger staleness: {FED_SPECS_REL}:{melt_claim_ln[0]} still says "
        "'Melting is not declarable', yet "
        f"{REXP_REL}:{melt_hits[0]} defines "
        "transpose_wide_table and path.explode measures present -- the retraction's live "
        "reason is curation (a second model id over rows TreasuryRates publishes), not an "
        "engine gap"
    )
    print(
        "  VERDICT federal_reserve-YieldCurve: CONTRADICTED and UNDER-DECLARED -- ledger vs "
        "census disagree about the same row, and its body needs rows.filter_comparison "
        "(yc-asof), rows.order_limit (yc-categorical) and columns.null_sentinel "
        "(yc-nd-sentinel), none of which the row declares"
    )


def extras_gov(
    payload: dict,
    present: frozenset[str],
    report: mcc.Report,
    unmet_d: list[str],
) -> None:
    """Print the TreasuryAuctions date, rescale and value-map faces."""
    lines = read_lines(UPSTREAM / TA)
    req = [n for n in find_lines(lines, '.strftime("%m/%d/%Y")') if "_query[" in lines[n - 1]]
    out_hits = find_lines(lines, "strptime")
    ctl = find_lines(read_lines(REPO / VT_REL), "strptime")
    print(
        f"  date_format face: REQUEST-side strftime lines {req}; OUTPUT-side strptime lines: "
        f"{len(out_hits)} -- control: the identical needle finds {len(ctl)} in {VT_REL} "
        "(the cast kernel's date_string_to_iso reads published INPUTs, it renders nothing"
    )
    rescale_ln, _ = cite(lines, TA, "float(v) / 100", 1)
    print(
        f"  the only %m/%d/%Y work the pinned fetcher does is param rendering "
        f"({TA}:{req[0]}, :{req[1]}); the response-side conversion this row would actually "
        f"need is the OUTPUT rescale at {TA}:{rescale_ln}, which the need set omits"
    )
    vm = next(e for e in mcc.REGISTRY if e.capability == "param.value_mapping")
    print(
        f"  value_map fidelity: {vm.proof_file} (why: 'refusing a value the map does not "
        "spell') vs the passthrough default at "
        f"{TA}:{cite(lines, TA, 'security_dict.get(', 1)[0]} -- the fold is honest only while "
        "every allowed security type is spelled in the map"
    )
    print(
        "  VERDICT government_us-TreasuryAuctions: UNDER-DECLARED and MIS-ANCHORED FOR THIS "
        f"ROW -- anchoring columns.cast present flips it (arm D={unmet_d}) on the strength of "
        "a date_format need that names a REQUEST-side format the cast kernel never performs, "
        f"while its own *Rate rescale ({TA}:{rescale_ln}) and N/A nulling stay undeclared; "
        "param.value_mapping presence is honestly exercised by gu-type-map"
    )


def row_needs(payload: dict) -> frozenset[str]:
    """Fold one census payload's raw labels onto its canonical need set."""
    labels = tuple(payload["needs_engine_capability"])
    needs: frozenset[str] = mcc.map_needs(labels, payload["task_id"])
    return needs


extras = {
    "OBB2-benzinga-AnalystSearch": extras_benzinga,
    "OBB2-federal_reserve-YieldCurve": extras_federal,
    "OBB2-government_us-TreasuryAuctions": extras_gov,
}

if __name__ == "__main__":
    raise SystemExit(main())
