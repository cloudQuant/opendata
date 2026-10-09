"""C75 task #35: are SOFR and OvernightBankFundingRate really declarable today?

The census marked both ``expressible_today: true`` with an empty need list, while FederalFundsRate
-- whose own census note says SOFR/OBFR share "the same shape" -- was marked false with three
needs. Both readings cannot be right, and the difference is not bookkeeping: a declarative SOFR
spec written against the "true" reading would publish ``4.33`` where the upstream row model
publishes ``0.0433``, because the upstream percent fields pass through a before-validator that
divides by 100.

Three surfaces decide it, and every line number a replacement note cites is read out of them
rather than typed into this file:

* the installed oracle (``openbb_federal_reserve``, version printed on the second line) for the
  validator span, the division, the date sort and the undeclared-key pops;
* this repo's engine for what a declaration can express -- the ``ColumnSpec``/``ModelSpec``
  annotation spans and ``normalize_record``'s span and value-building line;
* ``opendata/data/providers/federal_reserve/specs.py`` for what production actually declares,
  which closes the arithmetic: declared + NOT_DECLARED == the census's federal_reserve rows.

The last surface is the one that already disagreed with the census: specs.py names SOFR,
OvernightBankFundingRate and FederalFundsRate inside ``NOT_DECLARED``, so a row claiming
"expressible today" was contradicted by the package that ships the declarations.

Two findings shrink the need list instead of growing it. The engine builds each row from declared
columns only, so upstream's ``d.pop(...)`` calls are a no-op for us and ``client_side_column_drop``
should never have been counted as a blocker; and the EFFR row's note blames ``spec.py:266-285`` for
"ModelSpec has no scale/drop/sort field", which is not ModelSpec at all.

A claim of the form "every cite was measured" needs a way to fail, so ``--self-test`` tampers four
lines **in memory** (the loaded line lists, never the files on disk) and requires each tamper to
abort the judgement. Nothing here writes to site-packages or to the engine.

Usage:
    python docs/evidence/C75/fed-percent-census-recheck.py             # measure, print row diff
    python docs/evidence/C75/fed-percent-census-recheck.py --write     # apply the three rewrites
    python docs/evidence/C75/fed-percent-census-recheck.py --self-test # four tampers must abort
    # --write is one shot: re-running reads "moved 0" and aborts, which is the applied reading.
    # The roadmap is a separate document, and it only changes when --json names its path:
    # python3 scripts/quality/model_capability_census.py --round-id C75 --json \
    #     docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/capability-roadmap.json
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
PLAN_DIR = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
CENSUS = REPO / PLAN_DIR / "census-sec-tmx-fed-gov-finra.json"
ROADMAP_REL = f"{PLAN_DIR}/capability-roadmap.json"
FED_PACKAGE = "opendata/data/providers/federal_reserve"
ENGINE_SPEC = REPO / "opendata/data/providers/_engine/spec.py"
ENGINE_HTTP = REPO / "opendata/data/providers/_engine/http_json.py"
FED_SPECS = REPO / FED_PACKAGE / "specs.py"

RESCALE = "published_value_rescale"
SORT = "client_side_sort"
DROP = "client_side_column_drop"

SOFR = "sofr.py"
OBFR = "overnight_bank_funding_rate.py"
EFFR = "federal_funds_rate.py"
ORACLE_KEYS = (SOFR, OBFR, EFFR)

#: the two claims the rewritten notes retract, kept verbatim so a note cannot drift away from the
#: row it replaces without the round-trip check noticing
OLD_SOFR_CLAIM = "params+rows_pointer cover it"
OLD_OBFR_CLAIM = "same shape as EFFR"
OLD_EFFR_FACE = "spec.py:266-285"

#: what a row's audited.basis points at: the package that ships the declarations
BASIS = f"{FED_PACKAGE}/specs.py -- NOT_DECLARED and the model= declarations"

DIVISION = "/ 100"
SORTED_FACE = "return sorted(results, key=lambda x: x.date)"
VALUES_LINE = "values[column.name] = value"
_FIELD_RE = re.compile(r"^    [a-z_][a-z0-9_]*: ")
_KEY_RE = re.compile(r'^    "([A-Za-z]+)": ', re.MULTILINE)


@dataclass(frozen=True)
class OracleCites:
    """One oracle model file's measured lines, 1-based, spans inclusive."""

    name: str
    validator: tuple[int, int]
    percent_fields: int
    division: int
    sort: int
    pops: tuple[int, int]


@dataclass(frozen=True)
class EngineCites:
    """The measured declaration surface: two field spans and the record builder."""

    column_face: tuple[int, int]
    model_face: tuple[int, int]
    normalize_record: tuple[int, int]
    declared_models: tuple[str, ...]
    not_declared: tuple[str, ...]


def one(lines: list[str], needle: str, label: str) -> int:
    """Return the index of the single line holding ``needle``, aborting unless exactly one."""
    found = [index for index, line in enumerate(lines) if needle in line]
    if len(found) != 1:
        raise AssertionError(f"{label}: {needle!r} found on {found}, expected exactly one line")
    return found[0]


def oracle_cites(name: str, lines: list[str]) -> OracleCites:
    """Measure the validator, division, sort and pops of one oracle model file."""
    division = one(lines, DIVISION, f"{name} rescale")
    starts = [index for index in range(division) if lines[index].strip() == "@field_validator("]
    if not starts:
        raise AssertionError(f"{name}: no @field_validator decorator before {name}:{division + 1}")
    if lines[division + 1].strip() != "return None":
        raise AssertionError(f"{name}: the validator does not end one line after the division")
    start = starts[-1]
    modes = [index for index in range(start + 1, division) if 'mode="before"' in lines[index]]
    if len(modes) != 1:
        raise AssertionError(f'{name}: expected one mode="before" inside the validator, {modes}')
    fields = sum(1 for index in range(start + 1, modes[0]) if lines[index].strip().startswith('"'))
    pops = [index for index, line in enumerate(lines) if "d.pop(" in line]
    if not pops or pops != list(range(pops[0], pops[-1] + 1)):
        raise AssertionError(f"{name}: pops are not one contiguous block, found {pops}")
    return OracleCites(
        name=name,
        validator=(starts[-1] + 1, division + 2),
        percent_fields=fields,
        division=division + 1,
        sort=one(lines, SORTED_FACE, f"{name} sort") + 1,
        pops=(pops[0] + 1, pops[-1] + 1),
    )


def annotation_span(lines: list[str], class_name: str) -> tuple[int, int]:
    """Measure the inclusive span of a dataclass's own field annotations."""
    declared = one(lines, f"class {class_name}:", f"engine {class_name}")
    methods = [
        index
        for index in range(declared + 1, len(lines))
        if lines[index].startswith(("    def ", "    @"))
    ]
    if not methods:
        raise AssertionError(f"{class_name}: no method follows the field block")
    fields = [index for index in range(declared + 1, methods[0]) if _FIELD_RE.match(lines[index])]
    if not fields:
        raise AssertionError(
            f"{class_name}: no annotated fields between :{declared + 1} and :{methods[0]}"
        )
    return fields[0] + 1, fields[-1] + 1


def face(lines: list[str], span: tuple[int, int]) -> str:
    """Return the text a measured span holds."""
    return "\n".join(lines[span[0] - 1 : span[1]])


def engine_cites(spec: list[str], http: list[str], fed: list[str]) -> EngineCites:
    """Measure what a declaration can express, and what the fed package declares today."""
    column_face = annotation_span(spec, "ColumnSpec")
    model_face = annotation_span(spec, "ModelSpec")
    record_start = one(http, "def normalize_record(", "engine normalize_record")
    following = [i for i in range(record_start + 1, len(http)) if http[i].startswith("def ")]
    if not following:
        raise AssertionError("no def follows normalize_record; its span cannot be bounded")
    record_end = following[0] - 1
    while not http[record_end].strip():
        record_end -= 1
    block_start = one(fed, "NOT_DECLARED: dict[str, str] = {", "fed NOT_DECLARED")
    close = next((index for index in range(block_start + 1, len(fed)) if fed[index] == "}"), None)
    if close is None:
        raise AssertionError(
            "NOT_DECLARED has no closing brace at column 0; the block cannot be bounded"
        )
    keys = tuple(sorted(set(re.findall(_KEY_RE, "\n".join(fed[block_start + 1 : close])))))
    declared = tuple(sorted(set(re.findall(r'model="([A-Za-z]+)"', "\n".join(fed)))))
    return EngineCites(
        column_face=column_face,
        model_face=model_face,
        normalize_record=(record_start + 1, record_end + 1),
        declared_models=declared,
        not_declared=keys,
    )


def load_inputs(models: Path) -> dict[str, list[str]]:
    """Read every file the judgement cites, once, as line lists."""
    inputs: dict[str, list[str]] = {
        name: (models / name).read_text(encoding="utf-8").splitlines() for name in ORACLE_KEYS
    }
    inputs["spec.py"] = ENGINE_SPEC.read_text(encoding="utf-8").splitlines()
    inputs["http_json.py"] = ENGINE_HTTP.read_text(encoding="utf-8").splitlines()
    inputs["specs.py"] = FED_SPECS.read_text(encoding="utf-8").splitlines()
    return inputs


def evaluate(inputs: dict[str, list[str]]) -> tuple[list[str], dict[str, OracleCites], EngineCites]:
    """Judge the declarability facts from loaded lines; raise the moment a cite has moved."""
    facts: list[str] = []

    def claim(condition: bool, message: str) -> None:
        """Record a measured fact, or abort: a note may not cite a line that moved."""
        if not condition:
            raise AssertionError(message)
        facts.append(message)

    sofr, obfr, effr = (inputs[name] for name in ORACLE_KEYS)
    spec, http_lines = inputs["spec.py"], inputs["http_json.py"]
    cites: dict[str, OracleCites] = {}
    for name, body in zip(ORACLE_KEYS, (sofr, obfr, effr), strict=True):
        cites[name] = oracle_cites(name, body)
    engine = engine_cites(spec, http_lines, inputs["specs.py"])
    claim(
        bool(engine.declared_models) and bool(engine.not_declared),
        f"specs.py yields {len(engine.declared_models)} declarations and "
        f"{len(engine.not_declared)} NOT_DECLARED models",
    )
    declared_ids = {name.lower() for name in engine.declared_models}
    overlap = sorted(declared_ids & {key.lower() for key in engine.not_declared})
    claim(not overlap, f"no model is both declared and NOT_DECLARED (overlap: {overlap})")

    for cite in cites.values():
        body = inputs[cite.name]
        claim(
            body[cite.validator[0] - 1].strip() == "@field_validator(",
            f"{cite.name}:{cite.validator[0]} opens a validator over {cite.percent_fields} fields",
        )
        claim(DIVISION in body[cite.division - 1], f"{cite.name}:{cite.division} divides by 100")
        sort_claim = f"{cite.name}:{cite.sort} sorts by date"
        claim(body[cite.sort - 1] == f"        {SORTED_FACE}", sort_claim)
        claim(
            all("d.pop(" in body[i - 1] for i in range(cite.pops[0], cite.pops[1] + 1)),
            f"{cite.name}:{cite.pops[0]}-{cite.pops[1]} pop undeclared keys",
        )

    column_text = face(spec, engine.column_face)
    claim(
        "source_key" in column_text and "units" in column_text,
        f"spec.py:{engine.column_face[0]}-{engine.column_face[1]} is the ColumnSpec face",
    )
    claim("scale" not in column_text, "ColumnSpec declares no scale field")
    claim("sort" not in column_text, "ColumnSpec declares no sort field")
    model_text = face(spec, engine.model_face)
    claim(
        "rows_pointer" in model_text and "decoder" in model_text,
        f"spec.py:{engine.model_face[0]}-{engine.model_face[1]} is the ModelSpec face",
    )
    claim("scale" not in model_text, "ModelSpec declares no scale field")
    claim("sort" not in model_text, "ModelSpec declares no sort field")

    record = "\n".join(http_lines[engine.normalize_record[0] - 1 : engine.normalize_record[1]])
    record_span = f"http_json.py:{engine.normalize_record[0]}-{engine.normalize_record[1]}"
    claim(VALUES_LINE in record, f"{record_span} is normalize_record")
    claim("for column in spec.columns:" in record, "normalize_record iterates declared columns")
    claim("row_model(**values)" in record, "normalize_record builds the row from declared values")
    claim("record.keys()" not in record, "normalize_record never forwards undeclared keys")
    return facts, cites, engine


def notes(cites: dict[str, OracleCites], engine: EngineCites, version: str) -> dict[str, Any]:
    """Build the three replacement rows from measured line numbers only -- nothing here is typed."""
    sofr, obfr, effr = cites[SOFR], cites[OBFR], cites[EFFR]
    column = f"spec.py:{engine.column_face[0]}-{engine.column_face[1]}"
    model = f"spec.py:{engine.model_face[0]}-{engine.model_face[1]}"
    record = f"http_json.py:{engine.normalize_record[0]}-{engine.normalize_record[1]}"
    oracle = f"openbb_federal_reserve {version}"
    declaration_face = (
        f"neither ColumnSpec ({column}) nor ModelSpec ({model}) declares a scale or a sort"
    )
    builds_from_columns = (
        f"normalize_record ({record}) builds each row from declared columns, so an undeclared "
        "key never reaches the row model"
    )
    return {
        "OBB2-federal_reserve-SOFR": (
            False,
            [RESCALE, SORT],
            f"DOWNGRADED true->false (C75): {oracle} {SOFR}:{sofr.validator[0]}-"
            f"{sofr.validator[1]} divides {sofr.percent_fields} published percent fields by 100 in "
            f"a before-validator (the division is {SOFR}:{sofr.division}) and {SOFR}:{sofr.sort} "
            f"sorts the rows by date, while {declaration_face}. The retracted note said "
            f"'{OLD_SOFR_CLAIM}': "
            f"that covers the request shape, not the published values. The pops at "
            f"{SOFR}:{sofr.pops[0]}-{sofr.pops[1]} are NOT a blocker, because "
            f"{builds_from_columns}.",
        ),
        "OBB2-federal_reserve-OvernightBankFundingRate": (
            False,
            [RESCALE, SORT],
            f"DOWNGRADED true->false (C75): the retracted note already said '{OLD_OBFR_CLAIM}', "
            "and the EFFR row reads false. The same two blockers, measured on the same lines of "
            f"the same oracle: {oracle} {OBFR}:{obfr.validator[0]}-{obfr.validator[1]} rescales "
            f"{obfr.percent_fields} published percents by /100 (division :{obfr.division}) and "
            f":{obfr.sort} sorts by date. Column-drop is excluded for the same reason as SOFR: "
            f"{builds_from_columns}. The pops in question are "
            f"{OBFR}:{obfr.pops[0]}-{obfr.pops[1]}.",
        ),
        "OBB2-federal_reserve-FederalFundsRate": (
            False,
            [RESCALE, SORT],
            f"NEEDS LIST CORRECTED (C75): expressible_today stays false on the same two "
            f"blockers -- {oracle} {EFFR}:{effr.validator[0]}-{effr.validator[1]} rescales "
            f"{effr.percent_fields} published percents (division :{effr.division}) and "
            f":{effr.sort} sorts by date, while {declaration_face}. Two removals: "
            f"{DROP} is not a blocker because {builds_from_columns}, and the pops at "
            f"{EFFR}:{effr.pops[0]}-{effr.pops[1]} are therefore a no-op for us; and the C74 note "
            f"blamed ModelSpec at {OLD_EFFR_FACE}, which is DecoderSpec.effective_delimiter and "
            "RowFilterSpec's docstring, not ModelSpec.",
        ),
    }


def identity_problems(engine: EngineCites, rows: list[dict[str, Any]]) -> list[str]:
    """Check the census against the package that ships declarations; return what disagrees."""
    fed = [row for row in rows if str(row.get("provider")) == "federal_reserve"]

    def model_key(row: dict[str, Any]) -> str:
        """The upstream model name a census row carries, folded for comparison."""
        return str(row.get("upstream_model")).lower()

    true_ids = {model_key(row) for row in fed if row.get("expressible_today")}
    not_declared = {key.lower() for key in engine.not_declared}
    problems: list[str] = []
    if len(fed) != len(engine.declared_models) + len(engine.not_declared):
        problems.append(
            f"arithmetic: census {len(fed)} federal_reserve rows != declared "
            f"{len(engine.declared_models)} + NOT_DECLARED {len(engine.not_declared)}"
        )
    if true_ids != {name.lower() for name in engine.declared_models}:
        problems.append(
            f"expressible_today set {sorted(true_ids)} != production declarations "
            f"{sorted(engine.declared_models)}"
        )
    false_ids = {model_key(row) for row in fed if not row.get("expressible_today")}
    unlisted = sorted(false_ids - not_declared)
    if unlisted:
        problems.append(f"census rows that specs.py lists nowhere: {unlisted}")
    if true_ids & not_declared:
        problems.append(
            "rows claiming true that specs.py lists as NOT_DECLARED: "
            f"{sorted(true_ids & not_declared)}"
        )
    return problems


def cited_bytes(models: Path) -> dict[str, bytes]:
    """Snapshot the files on disk that the self-test must leave untouched."""
    paths = {
        SOFR: models / SOFR,
        OBFR: models / OBFR,
        EFFR: models / EFFR,
        "spec.py": ENGINE_SPEC,
        "http_json.py": ENGINE_HTTP,
        "specs.py": FED_SPECS,
    }
    return {name: path.read_bytes() for name, path in paths.items()}


def tamper_plan(
    cites: dict[str, OracleCites], engine: EngineCites, http: list[str]
) -> list[tuple[str, int, str]]:
    """Four planted lines, each derived from a cite and each able to break a different claim."""
    sofr = cites[SOFR]
    lo, hi = engine.normalize_record
    comment = next(
        (index for index in range(lo - 1, hi) if http[index].strip().startswith("#")), None
    )
    if comment is None:
        raise AssertionError("normalize_record holds no comment line to plant into")
    return [
        (SOFR, sofr.division, "        return None  # planted: the /100 division is gone"),
        (SOFR, sofr.sort, "        return results  # planted: no date sort"),
        (
            "spec.py",
            engine.column_face[1] + 1,
            "    rescale: float = 1.0  # planted: ColumnSpec grew a scale field",
        ),
        (
            "http_json.py",
            comment + 1,
            "    values = {key: record[key] for key in record.keys()}  # planted: extras flow in",
        ),
    ]


def self_test(models: Path) -> int:
    """Every tamper applied to the loaded lines must abort; the bytes on disk must not move."""
    baseline = load_inputs(models)
    facts, cites, engine = evaluate(baseline)
    before = cited_bytes(models)
    blank = baseline["spec.py"][engine.column_face[1]]
    if blank.strip():
        raise AssertionError(
            "the ColumnSpec face has no blank line after it; tamper 3 would hit a field"
        )

    fired = 0
    plan = tamper_plan(cites, engine, baseline["http_json.py"])
    for key, number, replacement in plan:
        touched = {name: list(body) for name, body in baseline.items()}
        touched[key][number - 1] = replacement
        try:
            evaluate(touched)
            print(f"   NOT CAUGHT: a tamper at {key}:{number} still measured clean")
        except AssertionError as error:
            fired += 1
            print(f"   caught {key}:{number} -> {str(error)[:76]}")

    if cited_bytes(models) != before:
        print("   SELF-TEST BROKE A FILE ON DISK -- the tampering was not confined to memory")
        return 1
    if fired != 4:
        print(f"SELF-TEST FAIL: {fired}/4 tampers aborted; not every cite is load-bearing")
        return 1
    print(
        f"SELF-TEST PASS: {fired}/4 in-memory tampers aborted the run "
        f"({len(facts)} facts intact, six cited files byte-unchanged)"
    )
    return 0


def rewrite(rows: list[dict[str, Any]], replacements: dict[str, Any]) -> list[str]:
    """Apply the three row rewrites in place and report which task ids moved."""
    changed: list[str] = []
    for row in rows:
        tid = str(row.get("task_id"))
        if tid not in replacements:
            continue
        expressible, needs, note = replacements[tid]
        previous = (row.get("expressible_today"), list(row.get("needs_engine_capability", [])))
        if previous == (expressible, needs) and row.get("notes") == note:
            continue
        row["expressible_today"] = expressible
        row["needs_engine_capability"] = needs
        row["notes"] = note
        if previous[0] is True:
            row["audited"] = {
                "audited_by": "C75 federal percent recheck",
                "audited_date": "2026-10-09",
                "changed": ["expressible_today", "needs_engine_capability", "notes"],
                "prior_expressible_today": True,
                "prior_needs_engine_capability": list(previous[1]),
                "basis": BASIS,
            }
        changed.append(f"{tid}: {previous} -> {(expressible, needs)}")
    return changed


def main(argv: list[str]) -> int:
    """Measure, then either tamper (--self-test) or rewrite the three rows (--write)."""
    parser = argparse.ArgumentParser(description="C75 federal_reserve percent-row recheck")
    parser.add_argument("--write", action="store_true", help="apply the census row rewrites")
    parser.add_argument("--self-test", action="store_true", help="tamper four cites in memory")
    args = parser.parse_args(argv)

    module = importlib.import_module("openbb_federal_reserve")
    origin = getattr(module, "__file__", None)
    if not origin:
        raise SystemExit("openbb_federal_reserve has no __file__; the cites cannot be re-measured")
    from importlib.metadata import version

    installed = version("openbb_federal_reserve")
    models = Path(origin).resolve().parent / "models"
    print(f"repo={REPO}")
    print(f"oracle=openbb_federal_reserve {installed} at {models}")

    if args.self_test:
        return self_test(models)

    inputs = load_inputs(models)
    facts, cites, engine = evaluate(inputs)
    replacements = notes(cites, engine, installed)
    print("== facts re-measured against the installed oracle and this engine ==")
    for fact in facts:
        print(f"   {fact}")

    data = json.loads(CENSUS.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = data["rows"]
    fed_rows = [row for row in rows if str(row.get("provider")) == "federal_reserve"]
    print(
        f"   production face: specs.py declares {list(engine.declared_models)}, NOT_DECLARED "
        f"carries {len(engine.not_declared)}; census has {len(fed_rows)} federal_reserve rows"
    )
    before_problems = identity_problems(engine, rows)
    print(f"== census against production, before the rewrite: {len(before_problems)} problems ==")
    for problem in before_problems:
        print(f"   {problem}")

    before = sorted(str(r.get("task_id")) for r in rows if r.get("expressible_today") is True)
    print(f"\ncensus rows={len(rows)}; expressible_today=true before: {before}")
    changed = rewrite(rows, replacements)
    for line in changed:
        print(f"   rewrite {line}")
    for row in rows:
        tid = str(row.get("task_id"))
        if tid in replacements:
            print(f"   note {tid}: {row['notes']}")
    if len(changed) != 3:
        print(f"ABORT: expected exactly 3 rows to move, moved {len(changed)} (already applied?)")
        return 1

    untouched = [r for r in rows if str(r.get("task_id")) not in replacements]
    payload = json.dumps(data, indent=2) + "\n"
    reread = json.loads(payload)
    after = sorted(
        str(r.get("task_id")) for r in reread["rows"] if r.get("expressible_today") is True
    )
    print(f"rows untouched={len(untouched)}; true after the rewrite: {after}")
    print(
        f"round-trip row count: {len(reread['rows'])}; unique task ids: "
        f"{len({str(r['task_id']) for r in reread['rows']})}"
    )
    after_problems = identity_problems(engine, reread["rows"])
    print(f"== census against production, after the rewrite: {len(after_problems)} problems ==")
    for problem in after_problems:
        print(f"   {problem}")
    if after_problems:
        print("ABORT: the rewrite did not close the identity")
        return 1

    if not args.write:
        print("\nDRY RUN -- census untouched. Pass --write, then regenerate the roadmap:")
        print(
            "  python3 scripts/quality/model_capability_census.py --round-id C75 --json "
            f"{ROADMAP_REL}"
        )
        return 0

    CENSUS.write_text(payload, encoding="utf-8")
    print(f"wrote {CENSUS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
