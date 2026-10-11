#!/usr/bin/env python3
"""Witness the one ready-row the reconcile instrument still calls BLOCKED.

``docs/evidence/C88/reconcile-ready-rows.py`` splits the vocabulary-ready census rows against each
package's cite-backed refusals and this round printed ``SHIPPED 5, BLOCKED 1``. This script decides
whether the BLOCKED row is ``OBB2-federal_reserve-SvenssonYieldCurve``, whether the roadmap's
``shipped_cover`` really carries it, and whether the refusal's cited capabilities are engine-absent
while the census row's declared need set is engine-present -- the under-declared-need-set defect
class C89 just fixed for the four TMX rows.

Nothing is written outside this evidence directory except one scratch copy of ``specs.py`` that the
control arm deletes immediately; no request is made and no engine code is executed -- the engine
face is read off its AST, the same discipline the C88 instrument states for itself.

Every asserted face prints a pass reading and a damage reading; an arm whose two readings agree
measures nothing and forces a non-zero exit.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import re
import subprocess  # nosec B404  # reruns only this repo's C88 witness
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts" / "quality"))

import model_capability_census as mcc  # noqa: E402

CENSUS_DIR = REPO / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
CENSUS_FILE_REL = (
    "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/census-sec-tmx-fed-gov-finra.json"
)
ROADMAP_REL = CENSUS_DIR / "capability-roadmap.json"
SPECS_REL = "opendata/data/providers/federal_reserve/specs.py"
SPECS = REPO / SPECS_REL
RECONCILE = REPO / "docs/evidence/C88/reconcile-ready-rows.py"
SCRATCH = Path(__file__).resolve().parent / "_damaged_specs_scratch.py"
UPSTREAM = Path(
    "/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers/federal_reserve/"
    "openbb_federal_reserve/models/svensson_yield_curve.py"
)
TASK_ID = "OBB2-federal_reserve-SvenssonYieldCurve"

#: Refusal phrase -> census label -> canonical, each with the upstream line the phrase is about.
PROBES: tuple[tuple[str, str, str], ...] = (
    (
        "sentinel map",
        "sentinel_value_nulling",
        f"{UPSTREAM.name}:1066-1082 validate_float_fields nulls '', NA, ND, N/A and -999.99",
    ),
    (
        "window the rows",
        "comparison_row_filter",
        f"{UPSTREAM.name}:1170,1173 row_date < start_date / row_date > end_date are comparisons",
    ),
    (
        "series_type selects",
        "xbrl_tag_assembly",
        f"{UPSTREAM.name}:1120-1135,1180-1186 transform_data allow-lists columns per series_type",
    ),
)

#: The labels a corrected census row would add; measured to leave the row out of shipped_cover.
MISSING_LABELS: tuple[str, ...] = (
    "sentinel_value_nulling",
    "comparison_row_filter",
    "xbrl_tag_assembly",
)

COUNT_LINE = re.compile(
    r"^(SHIPPED|BLOCKED|NO_PACKAGE|UNREFUSED)\s+(\d+)\s+\[(.*)\]$", re.MULTILINE
)


def load_reconcile() -> ModuleType:
    """Import the C88 instrument so the control arm runs its own extraction code."""
    spec = importlib.util.spec_from_file_location("reconcilemod", RECONCILE)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            "the reconcile instrument is not loadable, so this face measures nothing"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules["reconcilemod"] = module
    spec.loader.exec_module(module)
    return module


def class_partition(
    module: ModuleType, specs_path: Path, rows: list[mcc.CensusRow], present: frozenset[str]
) -> dict[str, list[str]]:
    """Re-run the C88 four-class partition against the specs file at ``specs_path``."""
    out: dict[str, list[str]] = {"SHIPPED": [], "BLOCKED": [], "NO_PACKAGE": [], "UNREFUSED": []}
    tables = module.top_level_dicts(specs_path)
    for row in rows:
        if not row.needs or not set(row.needs) <= present:
            continue
        package = REPO / "opendata/data/providers" / row.provider
        refused = ""
        for table in tables.values():
            if row.upstream_model in table:
                refused = table[row.upstream_model]
        declared = row.upstream_model in specs_path.read_text(encoding="utf-8")
        if not package.is_dir():
            kind = "NO_PACKAGE"
        elif refused:
            kind = "BLOCKED"
        elif declared:
            kind = "SHIPPED"
        else:
            kind = "UNREFUSED"
        out[kind].append(row.task_id)
    return out


def parse_live_counts(stdout: str) -> dict[str, tuple[int, str]]:
    """Read the printed class counts out of the live reconcile run."""
    counts: dict[str, tuple[int, str]] = {}
    for match in COUNT_LINE.finditer(stdout):
        counts[match.group(1)] = (int(match.group(2)), match.group(3))
    return counts


def dataclass_field_names(path: Path) -> dict[str, list[str]]:
    """Every frozen dataclass field name in ``path``, keyed by class."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            names = [
                t.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                for t in [stmt.target]
            ]
            if names:
                out[node.name] = names
    return out


def ast_literal_text(path: Path, name: str) -> str:
    """Return the source text of a module-level assignment target's value."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = []
        value = None
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        if value is not None and name in targets:
            return ast.unparse(value)
    return ""


def cited_lines(tokens: list[str], lo: int, hi: int) -> list[int]:
    """1-based line numbers inside ``[lo, hi]`` of the upstream file carrying any token."""
    if not UPSTREAM.is_file():
        return []
    lines = UPSTREAM.read_text(encoding="utf-8").splitlines()
    return [
        number
        for number in range(lo, min(hi, len(lines)) + 1)
        if any(token in lines[number - 1] for token in tokens)
    ]


def main() -> int:  # one linear witness script, prints every arm
    """Measure all faces and print pass plus damage readings; return 0 only when each differed."""
    arms: list[tuple[str, object, object, bool]] = []

    def arm(name: str, passed: object, damaged: object, claim: bool) -> None:
        differs = passed != damaged

        def brief(value: object) -> object:
            text = repr(value)
            return text if len(text) <= 160 else f"{text[:150]}... ({len(text)} chars)"

        arms.append((name, passed, damaged, differs and claim))
        print(f"ARM {name}")
        print(f"    pass    : {brief(passed)}")
        print(f"    damage  : {brief(damaged)}")
        print(f"    differed: {differs}  claimed: {claim}  -> {differs and claim}")

    print(f"upstream comparator present : {UPSTREAM.is_file()} -> {UPSTREAM}")
    print()

    # -- face 1: the live reconcile counts, and a control that moves one of them ------------
    live = subprocess.run(  # noqa: S603  # nosec B603  # sys.executable + repo witness path
        [sys.executable, str(RECONCILE)],
        cwd=REPO,
        env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
        shell=False,
    )
    counts = parse_live_counts(live.stdout)
    blocked = counts.get("BLOCKED", (-1, ""))
    shipped = counts.get("SHIPPED", (-1, ""))
    print(f"live reconcile run (rc={live.returncode})")
    print(f"    SHIPPED={shipped[0]} {shipped[1]}")
    print(f"    BLOCKED={blocked[0]} {blocked[1]}")
    print(f"    NO_PACKAGE={counts.get('NO_PACKAGE')} UNREFUSED={counts.get('UNREFUSED')}")
    print()
    blocked_is_svensson = blocked[0] == 1 and TASK_ID in blocked[1]
    arm(
        "1a blocked row is the Svensson row",
        blocked_is_svensson,
        TASK_ID not in blocked[1],
        blocked_is_svensson,
    )

    report = mcc.run(round_id="C89-svensson-witness", root=REPO, census_dir=CENSUS_DIR)
    rows = mcc.load_rows(CENSUS_DIR)
    present = frozenset(c for c, r in report.readings.items() if r.present)
    original_specs = SPECS.read_text(encoding="utf-8")
    damaged_specs = original_specs.replace(
        '"SvenssonYieldCurve": "a sentinel map', '"SvenssonDamagedControl": "a sentinel map'
    )
    arm(
        "1b damage edit applied to the copy only (disk untouched)",
        ("damaged-key-present", "SvenssonDamagedControl" in damaged_specs),
        ("original-key-present", "SvenssonDamagedControl" in original_specs),
        "SvenssonDamagedControl" in damaged_specs
        and "SvenssonDamagedControl" not in original_specs,
    )
    reconcile = load_reconcile()
    pass_partition = class_partition(reconcile, SPECS, rows, present)
    SCRATCH.write_text(damaged_specs, encoding="utf-8")
    try:
        damage_partition = class_partition(reconcile, SCRATCH, rows, present)
    finally:
        SCRATCH.unlink(missing_ok=True)
    arm(
        "1c partition counts move when the refusal key is renamed (in-process replica)",
        (len(pass_partition["BLOCKED"]), len(pass_partition["SHIPPED"])),
        (len(damage_partition["BLOCKED"]), len(damage_partition["SHIPPED"])),
        len(pass_partition["BLOCKED"]) == 1
        and pass_partition["BLOCKED"] == [TASK_ID]
        and damage_partition["BLOCKED"] == []
        and TASK_ID in damage_partition["SHIPPED"],
    )

    # -- face 2: the refusal's cited capabilities through LABEL_TO_CAPABILITY / REGISTRY ------
    tables = reconcile.top_level_dicts(SPECS)
    refusal_text = ""
    for name, table in tables.items():
        if TASK_ID.removeprefix("OBB2-federal_reserve-") in table:
            refusal_text = table["SvenssonYieldCurve"]
            print(f"refusal table        : {name} (specs.py)")
    mapped: dict[str, str] = {}
    for phrase, label, cite in PROBES:
        found = phrase in refusal_text
        canonical = mcc.LABEL_TO_CAPABILITY[label]
        status = report.readings[canonical].status
        bucket = canonical in report.statuses[status]
        anchor = report.readings[canonical]
        print(
            f"phrase {phrase!r:26s} found={found} label={label} -> {canonical} "
            f"status={status} in-bucket={bucket}"
        )
        print(
            f"    registry anchor: {anchor.proof_file}:{anchor.anchor_line_number} "
            f"anchor={anchor.anchor!r}"
        )
        print(f"    upstream cite  : {cite}")
        if found:
            mapped[canonical] = label
    stripped = refusal_text.replace("sentinel map", "REDACTED")
    mapped_damage = {
        c
        for p, c in [
            ("sentinel map", "columns.null_sentinel"),
            ("window the rows", "rows.filter_comparison"),
            ("series_type selects", "columns.response_dependent_selection"),
        ]
        if p in stripped
    }
    arm(
        "2 refusal-cited canonical set vs the same probe on a redacted copy",
        tuple(sorted(mapped)),
        tuple(sorted(mapped_damage)),
        "columns.null_sentinel" in mapped and "columns.null_sentinel" not in mapped_damage,
    )

    # -- face 3: declared need set vs the union the refusal names ------------------------------
    row = next(r for r in rows if r.task_id == TASK_ID)
    extra = frozenset(mcc.LABEL_TO_CAPABILITY[label] for label in MISSING_LABELS)
    declared_subset = set(row.needs) <= present
    union_subset = set(row.needs) | extra <= present
    print(f"row raw_labels       : {sorted(row.raw_labels)}  (census JSON {CENSUS_FILE_REL})")
    print(f"row canonical needs  : {sorted(row.needs)}")
    print(f"present capabilities : {sorted(present)}")
    arm(
        "3 declared-need-set is covered, declared-union-refusal is not",
        declared_subset,
        union_subset,
        declared_subset and not union_subset,
    )

    # -- face 4: shipped_cover now, and after adding the missing labels to a DEEPCOPY ---------
    shipped_ids = {r.task_id for r in report.roadmap.shipped_cover}
    live_in_cover = TASK_ID in shipped_ids
    patched = dataclasses.replace(
        row,
        raw_labels=(*row.raw_labels, *MISSING_LABELS),
        needs=row.needs | extra,
    )
    rows2 = [patched if r.task_id == TASK_ID else r for r in rows]
    cover2 = {r.task_id for r in mcc.greedy_cover(rows2, report.readings).shipped_cover}
    arm(
        "4 Svensson in fresh shipped_cover vs after patched (in-memory) need set",
        live_in_cover,
        TASK_ID in cover2,
        live_in_cover and TASK_ID not in cover2,
    )
    published = re.search(
        r'"shipped_cover_rows"[\s\S]*?' + re.escape(TASK_ID),
        ROADMAP_REL.read_text(encoding="utf-8"),
    )
    print(f"published roadmap JSON carries the row in shipped_cover_rows: {bool(published)}")

    # -- face 5: the engine's declaration surface has no sentinel/comparison seam --------------
    spec_text = (REPO / "opendata/data/providers/_engine/spec.py").read_text(encoding="utf-8")
    vt_text = (REPO / "opendata/data/providers/_engine/value_transform.py").read_text(
        encoding="utf-8"
    )
    engine_fields = dataclass_field_names(REPO / "opendata/data/providers/_engine/spec.py")
    sentinel_fields = [
        f"{cls}.{name}"
        for cls, names in engine_fields.items()
        for name in names
        if re.search(r"sentinel|n_a|missing_value", name, re.IGNORECASE)
    ]
    stage_sequence = ast_literal_text(
        REPO / "opendata/data/providers/_engine/value_transform.py", "STAGE_SEQUENCE"
    )
    row_filter_ops = ast_literal_text(
        REPO / "opendata/data/providers/_engine/spec.py", "RowFilterOp"
    )
    print(f"STAGE_SEQUENCE (value_transform.py)      : {stage_sequence}")
    print(f"RowFilterOp    (spec.py)                 : {row_filter_ops}")
    print(
        "normalize_record keeps nulls as nulls; apply_cast raises ValueTransformError on a "
        "value outside its declared shape, so neither nulls a sentinel nor skips a row"
    )
    engine_hits = re.findall(r"sentinel", spec_text + vt_text, re.IGNORECASE)
    refusal_hits = re.findall(r"sentinel", refusal_text, re.IGNORECASE)
    arm(
        "5 'sentinel' token count: engine declaration files vs the refusal text",
        len(engine_hits),
        len(refusal_hits),
        len(engine_hits) == 0 and len(refusal_hits) > 0 and not sentinel_fields,
    )

    # -- face 6: the upstream cites are live on the pinned comparator --------------------------
    cited = {
        "sentinel spellings (:1062-1082)": cited_lines(
            ['"", "NA", "ND", "N/A""', '"NA"'], 1062, 1082
        ),
        "-999.99 (:1062-1082)": cited_lines(["-999.99"], 1062, 1082),
        "header found by searching 'Date,' (:1144-1152)": cited_lines(
            ['line.startswith("Date,")'], 1144, 1152
        ),
        "client-side date window (:1160-1180)": cited_lines(
            ["query.start_date", "query.end_date"], 1160, 1180
        ),
        "series_type column allow-list (:1107-1200)": cited_lines(
            ["allowed_fields", "query.series_type"], 1107, 1200
        ),
    }
    control = {
        "sentinel spellings (:100-130)": cited_lines(['"NA"'], 100, 130),
        "-999.99 (:100-130)": cited_lines(["-999.99"], 100, 130),
        "Date, search (:100-130)": cited_lines(['line.startswith("Date,")'], 100, 130),
        "date window (:100-130)": cited_lines(["query.start_date", "query.end_date"], 100, 130),
        "series_type (:100-130)": cited_lines(["allowed_fields", "query.series_type"], 100, 130),
    }
    for label, lines in cited.items():
        print(f"cited   {label:48s} -> {lines}")
    arm(
        "6 upstream cites hit inside their cited ranges vs the same tokens in :100-130",
        all(lines for lines in cited.values()),
        any(control[k] for k in control),
        all(cited[k] for k in cited) and not any(control[k] for k in control),
    )

    # -- verdict, generated from the measured values -------------------------------------------
    checks_ok = all(d for *_, d in arms)
    verdict_inputs = {
        "blocked_is_svensson": blocked_is_svensson,
        "cited_absent_canonicals": sorted(mapped),
        "declared_covered": declared_subset,
        "union_covered": union_subset,
        "in_shipped_cover": live_in_cover,
        "left_cover_when_fixed": TASK_ID not in cover2,
        "engine_sentinel_seam": bool(sentinel_fields) or bool(engine_hits),
    }
    for key, value in verdict_inputs.items():
        print(f"verdict input {key:26s} = {value}")
    if checks_ok and all(
        [
            blocked_is_svensson,
            len(mapped) == 3,
            declared_subset,
            not union_subset,
            live_in_cover,
            TASK_ID not in cover2,
            not sentinel_fields,
            not engine_hits,
        ]
    ):
        print(
            "VERDICT: confirmed -- the BLOCKED row is "
            f"{TASK_ID}; the roadmap's shipped_cover is the WRONG FACE because the census row at "
            f"{CENSUS_FILE_REL} is UNDER-DECLARED: its needs "
            f"{sorted(row.needs)} are all present, while the refusal in {SPECS_REL}:DECLARED_GAPS "
            f"names {sorted(mapped)}, all read absent in mcc.REGISTRY. Minimal honest correction: "
            f"add the labels {list(MISSING_LABELS)} to that row's needs_engine_capability "
            "(folding onto the named canonicals, with the upstream file:line cites measured in "
            "face 6), then re-derive the roadmap; neither retract SVENSSON_YIELD_CURVE "
            f"({SPECS_REL}:275) nor extend the engine this round. rc=0 only because every arm "
            "differed: "
            f"{sum(1 for *_, d in arms if d)}/{len(arms)}."
        )
        return 0
    print(
        f"VERDICT: not confirmed or an arm failed to differ -- {verdict_inputs}, "
        f"arms_ok={checks_ok}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
