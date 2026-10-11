"""Acceptance face for the C96 ``param.relative_default`` capability.

What is judged is the census instrument's own verdict, so the judge is imported and never re-typed
here: ``count_declarations`` reads the shipped provider declarations, ``verify_registry`` reads the
engine source, ``greedy_cover`` rebuilds the build-order ladder and ``run``/``serialize`` rebuild
the roadmap artifact byte for byte. Every number this file prints is a measurement taken from those
functions on this checkout.

Faces
    F1  presence is *read*: the registry entry is ``read_from_disk``, its anchor and its
        cross-anchor are located by line number in the two engine files, and the production
        declarations that exercise it are counted off the provider tree.
    F2  the wired bounds are read from the live declarations, not from the prose describing them.
    F3  the artifact is the tree: two serialised runs compare equal and compare equal to the
        committed ``capability-roadmap.json`` -- the F7-style reproducibility C95 shipped red.
    F4  the ladder face: the recomputed buckets and step-0 front beside the same judge run with the
        capability rolled back. The rollback must reproduce C95's published numbers exactly.
    F5  the monotonicity face: no row's blocker set may shrink except on a capability this round
        genuinely shipped, and no row's blocker set may grow at all.
    F6  the cite-carrier face: every upstream ``file:line`` a changed census row's C96 note cites
        resolves to a real line in the OpenBB tree that actually carries a date bound.

Counterfact arms -- each must move the verdict or this file exits 1
    C1  roll the registry entry back to its C95 shape       -> presence flips to absent.
    C2  retarget the anchor at a token the engine does not have -> flips to absent.
    C3  retarget the cross-anchor at a runtime symbol the request plane never grew -> flips absent.
    C4  flip the one wired census row back to ``expressible_today=false`` -> the buckets move.
    C5  strip the ``relative_default=`` keyword from the declaration -> the bound leaves the
        request.
    C6  C95's forward forecast (186, 10, 83): reachable only if all three credited rows get flagged.

Run: python docs/evidence/C96/relative-default-face.py
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENBB_PROVIDERS = Path("/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers")
OPENBB_ROOT = OPENBB_PROVIDERS.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.quality.model_capability_census as mcc  # noqa: E402
from opendata.data.providers._engine.http_json import encode_query  # noqa: E402
from opendata.data.providers.intrinio import specs as intrinio  # noqa: E402

if TYPE_CHECKING:  # these two names are only ever used in annotations
    from opendata.data.providers._engine.spec import ModelSpec, RelativeDefault

CAPABILITY = "param.relative_default"
CENSUS_DIR = REPO_ROOT / mcc.CENSUS_DIR_REL
ARTIFACT = REPO_ROOT / mcc.ROADMAP_REL
HD_TASK = "OBB2-intrinio-HistoricalDividends"

#: C95's published numbers, quoted verbatim so the rollback arm is judged against a prediction
#: rather than against whatever this run happens to read off the same tree it came from.
C95_PREDICTED_BUCKETS = (189, 7, 83)
C95_STEP0 = ("param.relative_default", 3)
C95_SHIPPED_COVER = 0
#: The falsifiable forward half of C95's prediction, kept so this face can name which part moved.
C95_FORECAST_BUCKETS = (186, 10, 83)

#: An upstream cite only carries the claim when the line it points at spells a bound the engine now
#: fills: one of the two window keys, the request-date read, or the offset arithmetic.
DATE_BOUND = re.compile(
    r"start_date|end_date|startdate|enddate|relativedelta|datetime\.now|now\b", re.I
)
CITE = re.compile(r"(?P<file>[A-Za-z0-9_]+\.py)?:(?P<start>\d+)(?:-(?P<end>\d+))?")

FAILURES: list[str] = []

#: A reading for a capability the registry never heard of, so ``_absent_needs`` stays total.
_UNKNOWN = mcc.EntryReading(
    capability="_unknown_",
    status="unanchored",
    present=False,
    engine_work=False,
    claim=False,
    read_from_disk=False,
    proof_file="",
    anchor="",
    anchor_found=False,
    anchor_line="",
    anchor_line_number=0,
    cross_file="",
    cross_anchor="",
    cross_anchor_found=False,
    why="",
    production_declarations=0,
)


def _record(label: str, value: object) -> None:
    print(f"  {label}: {value}")


def _judge(
    entries: tuple[mcc.CapabilityEntry, ...], rows: list[mcc.CensusRow]
) -> tuple[dict[str, mcc.EntryReading], mcc.Roadmap, dict[str, int], dict[str, dict[str, int]]]:
    """Run the repo's own instrument over a (possibly tampered) registry and row set."""
    declarations, by_file, _modules = mcc.count_declarations(entries, REPO_ROOT)
    readings = mcc.verify_registry(entries, REPO_ROOT, declarations)
    return readings, mcc.greedy_cover(rows, readings), declarations, by_file


def _buckets(roadmap: mcc.Roadmap) -> tuple[int, int, int]:
    return (
        roadmap.rows_unlocked_by_greedy_prefix,
        roadmap.rows_expressible_today,
        len(roadmap.rows_still_locked_after_engine_pool),
    )


def _entry(capability: str) -> mcc.CapabilityEntry:
    return next(entry for entry in mcc.REGISTRY if entry.capability == capability)


def _rollback(entry: mcc.CapabilityEntry) -> mcc.CapabilityEntry:
    """The one entry as the C95 round wrote it: a literal-default anchor, no probe, no runtime."""
    if entry.capability != CAPABILITY:
        return entry
    return mcc.CapabilityEntry(
        capability=entry.capability,
        claim=False,
        engine_work=True,
        proof_file=entry.proof_file,
        anchor="default: Any = None",
        why=entry.why,
    )


def _absent(readings: dict[str, mcc.EntryReading], needs: frozenset[str]) -> frozenset[str]:
    """One row's blocker set: the capabilities it needs that this run did not read as present."""
    return frozenset(
        capability for capability in needs if not readings.get(capability, _UNKNOWN).present
    )


def _rows() -> list[mcc.CensusRow]:
    return mcc.load_rows(CENSUS_DIR)


def _census_notes() -> dict[str, tuple[str, str]]:
    """Every census row this wave touched: its C96 note segment and the upstream file it cites."""
    notes: dict[str, tuple[str, str]] = {}
    for path in mcc.census_row_files(CENSUS_DIR):
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        for item in payload["rows"]:
            note = str(item.get("notes", ""))
            marker = re.search(r"\| C96\b", note)
            if marker is None:
                continue
            evidence = str(item.get("evidence", ""))
            head, _, tail = evidence.rpartition(":")
            notes[str(item["task_id"])] = (note[marker.start() :], head if tail.isdigit() else "")
    return notes


def _upstream_file(relative: str, file_name: str, provider: str) -> Path | None:
    """Resolve one cited file inside the provider the census row was read from."""
    if relative.endswith(file_name) and (OPENBB_ROOT / relative).is_file():
        return OPENBB_ROOT / relative
    base = OPENBB_PROVIDERS / provider if provider else OPENBB_PROVIDERS
    if not base.is_dir():
        return None
    hits = sorted(base.rglob(file_name))
    return hits[0] if hits else None


def _repo_file(file_name: str) -> Path | None:
    """A cite aimed at this repo's own engine plane rather than at the upstream tree."""
    hits = sorted((REPO_ROOT / "opendata").rglob(file_name))
    return hits[0] if hits else None


def _check_cites(
    notes: dict[str, tuple[str, str]],
) -> tuple[int, int, list[str], dict[str, int], list[str]]:
    """Resolve every upstream cite a touched row's note carries, and read the line it points at.

    Returns the number of upstream cites that resolved to a real line, the number resolved against
    this repo's own engine plane, the cites that resolve to no line anywhere, a per-row count of the
    cites whose lines actually spell a date bound, and any cite that landed outside the provider the
    census row names -- a same-named model file in another provider proves nothing about this row.
    """
    resolved: int = 0
    repo_resolved: int = 0
    dead: list[str] = []
    carried: dict[str, int] = {}
    mismatched: list[str] = []
    for key, (note, evidence) in sorted(notes.items()):
        provider = evidence.split("/")[1] if evidence.startswith("providers/") else ""
        current: Path | None = (
            OPENBB_ROOT / evidence if evidence and (OPENBB_ROOT / evidence).is_file() else None
        )
        carried[key] = 0
        for match in CITE.finditer(note):
            name, start, end = match.group("file"), int(match.group("start")), match.group("end")
            last = int(end) if end else start
            if name is not None:
                current = _upstream_file(evidence, name, provider)
                if current is None:
                    local = _repo_file(name)
                    if local is None:
                        dead.append(f"{key}: {name}:{start}-{last} in no provider or engine tree")
                        continue
                    lines = local.read_text(encoding="utf-8").splitlines()
                    if not 1 <= start <= len(lines) or not last <= len(lines):
                        dead.append(f"{key}: engine {name}:{start}-{last} past {len(lines)} lines")
                        continue
                    repo_resolved += 1
                    current = None
                    continue
            if current is None:
                continue
            lines = current.read_text(encoding="utf-8").splitlines()
            if not 1 <= start <= len(lines) or not last <= len(lines):
                dead.append(f"{key}: {current.name}:{start}-{last} past {len(lines)} lines")
                continue
            if provider and f"/{provider}/" not in f"/{current.relative_to(OPENBB_PROVIDERS)}":
                mismatched.append(f"{key}: {current.name}:{start} is not a {provider} file")
                continue
            resolved += 1
            if DATE_BOUND.search(" ".join(lines[start - 1 : last])):
                carried[key] += 1
    return resolved, repo_resolved, dead, carried, mismatched


def _wired_bounds() -> list[tuple[str, str, RelativeDefault]]:
    """Every relative bound this package actually declares, read off the live spec objects."""
    declared = [
        intrinio.CURRENCY_PAIRS,
        intrinio.EQUITY_SEARCH,
        intrinio.HISTORICAL_DIVIDENDS,
        intrinio.CALENDAR_IPO,
    ]
    return [
        (spec.model, parameter.name, parameter.relative_default)
        for spec in declared
        for parameter in spec.params
        if parameter.relative_default is not None
    ]


def _window(spec: ModelSpec) -> dict[str, str]:
    """The query string one fetch sends for a caller who named no bound."""
    return encode_query(
        SimpleNamespace(**{parameter.name: None for parameter in spec.params}),
        spec,
        now=date(2026, 3, 14),
    )


def main() -> int:
    """Run every face against the repo's own judge and print what it measured."""
    rows = _rows()
    readings, roadmap, _declarations, by_file = _judge(mcc.REGISTRY, rows)
    reading = readings[CAPABILITY]
    present = sorted(capability for capability, item in readings.items() if item.present)
    disk_read = sorted(entry.capability for entry in mcc.REGISTRY if entry.read_from_disk)

    print("F1 presence is read off the engine, not asserted")
    _record("status/present", f"{reading.status}/{reading.present}")
    _record("anchor", f"{reading.proof_file}:{reading.anchor_line_number} {reading.anchor!r}")
    _record(
        "cross-anchor",
        f"{reading.cross_file} {reading.cross_anchor!r} found={reading.cross_anchor_found}",
    )
    _record("present-set", f"head={len(present)} read_from_disk={len(disk_read)}")
    _record("declaration scan", f"{reading.production_declarations} {by_file.get(CAPABILITY)}")
    if not (reading.present and reading.read_from_disk and _entry(CAPABILITY).probe is not None):
        FAILURES.append("F1 presence must be measured from disk through a real declaration probe")
    if not (reading.anchor_found and reading.cross_anchor_found):
        FAILURES.append("F1 both anchors must be located on disk for presence to be read")
    if CAPABILITY not in present or CAPABILITY not in disk_read:
        FAILURES.append("F1 the capability is neither on the present set nor read from disk")

    print("\nF2 the wired bounds are read from the live declarations")
    wired = _wired_bounds()
    _record("declared bounds", [(m, n, vars(o)) for m, n, o in wired])
    _record("instrument count == live count", f"{reading.production_declarations} == {len(wired)}")
    if sorted(name for _m, name, _o in wired) != ["end_date", "start_date"]:
        FAILURES.append(f"F2 the wired bounds are not the reviewed window: {wired}")
    if reading.production_declarations != len(wired):
        FAILURES.append(
            f"F2 the instrument counted {reading.production_declarations} but the tree carries "
            f"{len(wired)}"
        )
    doubled = [
        parameter.name
        for parameter in intrinio.HISTORICAL_DIVIDENDS.params
        if parameter.relative_default is not None
        and (parameter.default is not None or parameter.required or parameter.kind != "date")
    ]
    if doubled:
        FAILURES.append(f"F2 a bound carries two answers or the wrong kind: {doubled}")

    print("\nF3 the artifact is what the tree produces (F7 reproducibility)")
    once = mcc.serialize(mcc.run(round_id="C96", root=REPO_ROOT, census_dir=CENSUS_DIR))
    twice = mcc.serialize(mcc.run(round_id="C96", root=REPO_ROOT, census_dir=CENSUS_DIR))
    shipped = ARTIFACT.read_text(encoding="utf-8")
    _record("two runs identical", once == twice)
    _record("recomputed == committed", once == shipped)
    _record("bytes", f"{len(once.encode())} recomputed vs {len(shipped.encode())} on disk")
    if once != twice:
        FAILURES.append("F3 the same tree serialised twice is not reproducible")
    if once != shipped:
        FAILURES.append("F3 the committed artifact is not what this tree produces")

    print("\nF4 the ladder face, beside the same judge with the capability rolled back")
    rolled_rows = [
        dataclasses.replace(row, expressible_today=False) if row.task_id == HD_TASK else row
        for row in rows
    ]
    back_readings, back_roadmap, _back_decl, _ = _judge(
        tuple(_rollback(entry) for entry in mcc.REGISTRY), rolled_rows
    )
    _record("after buckets (greedy, expressible, locked)", _buckets(roadmap))
    _record(
        "after step-0 front",
        f"{roadmap.build_order[0].capability} +{roadmap.build_order[0].rows_newly_unlocked}",
    )
    _record("after steps", len(roadmap.build_order))
    _record("after shipped-cover", sorted(i.task_id for i in roadmap.shipped_cover))
    _record("before buckets", _buckets(back_roadmap))
    _record(
        "before step-0 front",
        f"{back_roadmap.build_order[0].capability} "
        f"+{back_roadmap.build_order[0].rows_newly_unlocked}",
    )
    _record(
        "before shipped-cover",
        len(back_roadmap.shipped_cover),
    )
    _record(
        "before present-set",
        len([c for c, r in back_readings.items() if r.present]),
    )
    if _buckets(back_roadmap) != C95_PREDICTED_BUCKETS:
        FAILURES.append(
            f"F4 the rollback must reproduce C95's published {C95_PREDICTED_BUCKETS}, got "
            f"{_buckets(back_roadmap)}"
        )
    back_step0 = (
        back_roadmap.build_order[0].capability,
        back_roadmap.build_order[0].rows_newly_unlocked,
    )
    if back_step0 != C95_STEP0:
        FAILURES.append(f"F4 the rollback must put C95's step-0 front back, got {back_step0}")
    if len(back_roadmap.shipped_cover) != C95_SHIPPED_COVER:
        FAILURES.append("F4 the rollback must leave C95 with no shipped-cover row")
    if roadmap.build_order[0].capability == CAPABILITY:
        FAILURES.append("F4 a shipped capability is still a roadmap step")

    print("\nF5 monotonicity: no blocker set shrank except on this capability")
    shrank: list[tuple[str, frozenset[str]]] = []
    grew: list[str] = []
    for row in rows:
        after = _absent(readings, row.needs)
        before = _absent(back_readings, row.needs)
        if after - before:
            grew.append(row.task_id)
        if before - after:
            shrank.append((row.task_id, before - after))
    _record("rows whose blocker set shrank", sorted(task for task, _d in shrank))
    _record("blockers dropped", sorted({n for _t, d in shrank for n in d}))
    _record("rows whose blocker set grew", grew)
    _record("shrink / total rows", f"{len(shrank)} / {len(rows)}")
    _record(
        "locked rows still blocking on this capability",
        sum(1 for row in rows if CAPABILITY in _absent(readings, row.needs)),
    )
    if any(dropped != frozenset({CAPABILITY}) for _t, dropped in shrank):
        FAILURES.append("F5 a row lost a blocker this round did not ship")
    if grew:
        FAILURES.append(f"F5 a row gained a blocker: {grew}")
    if not shrank:
        FAILURES.append("F5 nothing shrank, so the capability unlocked no row")
    if {task for task, _d in shrank} and HD_TASK not in {task for task, _d in shrank}:
        FAILURES.append("F5 the one wired row kept its blocker -- the wiring is not being read")

    print("\nF6 cite-carrier: a changed census row cites lines that carry the bound")
    notes = _census_notes()
    _record("rows carrying a C96 note", sorted(notes))
    resolved, repo_resolved, dead, carried, mismatched = _check_cites(notes)
    _record("upstream cites resolved to a real line", resolved)
    _record("engine-plane cites resolved to a real line", repo_resolved)
    _record("date-bound cites per row", dict(sorted(carried.items())))
    _record("cites that resolve to no line", dead)
    _record("cites that resolve outside the row's provider", mismatched)
    if dead:
        FAILURES.append(f"F6 cites that resolve to no line in the OpenBB tree: {dead}")
    if mismatched:
        FAILURES.append(f"F6 cites that resolve outside the row's own provider: {mismatched}")
    uncarried = [key for key in sorted(notes) if not carried.get(key)]
    FAILURES.extend(
        f"F6 {key}: its C96 note carries no line that spells a date bound" for key in uncarried
    )
    if len(notes) < 3:
        FAILURES.append(
            f"F6 only {len(notes)} rows changed this wave, expected the 3 credited rows"
        )
    _arm_c7(notes)

    print("\nCounterfact arms -- each one must move the verdict")
    _arm_c1()
    _arm_c2()
    _arm_c3()
    _arm_c4(rows)
    _arm_c5()
    _forecast(rows)

    print("\nVERDICT:", "PASS" if not FAILURES else "FAIL")
    for failure in FAILURES:
        print("  -", failure)
    return 0 if not FAILURES else 1


def _arm_c7(notes: dict[str, tuple[str, str]]) -> None:
    """The cite carrier must be able to go red: forge a line and point it at the window."""
    forged = dict(notes)
    key = f"{HD_TASK} (forged)"
    forged[key] = ("| C96: forged cite (historical_dividends.py:99999-99999)", _hd_evidence())
    _resolved, _repo, dead, carried, _mismatch = _check_cites(forged)
    moved = bool(dead) and carried.get(key, 0) == 0
    _record("C7 forged past-end-of-file cite", f"dead={dead}")
    if not moved:
        FAILURES.append("C7 a cite past the end of the upstream file did not go red")
    off = {
        f"{HD_TASK} (off-topic)": (
            "| C96: an unrelated line (historical_dividends.py:41-41)",
            _hd_evidence(),
        )
    }
    _r, _rep, _d, off_carried, _m = _check_cites(off)
    _record("C7 cite to a non-date line", f"date-bound carried={off_carried}")
    if off_carried.get(f"{HD_TASK} (off-topic)"):
        FAILURES.append("C7 the carrier accepted a line that spells no date bound")


def _hd_evidence() -> str:
    for path in mcc.census_row_files(CENSUS_DIR):
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        for item in payload["rows"]:
            if item["task_id"] == HD_TASK:
                return str(item["evidence"]).rsplit(":", 1)[0]
    return ""


def _arm_c1() -> None:
    """Roll the registry entry back to its C95 shape; presence has to go with it."""
    readings, roadmap, declarations, _ = _judge(
        tuple(_rollback(entry) for entry in mcc.REGISTRY), _rows()
    )
    _record(
        "C1 entry rolled back to the literal-default anchor",
        f"present={readings[CAPABILITY].present} status={readings[CAPABILITY].status} "
        f"declarations={declarations[CAPABILITY]} buckets={_buckets(roadmap)}",
    )
    if readings[CAPABILITY].present or declarations[CAPABILITY]:
        FAILURES.append("C1 rolling the entry back did not flip presence -- presence is asserted")


def _arm_c2() -> None:
    """Retarget the anchor: a token the engine does not have must read absent."""
    target = dataclasses.replace(_entry(CAPABILITY), anchor="relative_default_bound: None = None")
    entries = tuple(target if entry.capability == CAPABILITY else entry for entry in mcc.REGISTRY)
    readings, _roadmap, _decl, _ = _judge(entries, _rows())
    _record(
        "C2 anchor retargeted",
        f"status={readings[CAPABILITY].status} anchor_found={readings[CAPABILITY].anchor_found}",
    )
    if readings[CAPABILITY].present:
        FAILURES.append("C2 a missing anchor still read present")


def _arm_c3() -> None:
    """Retarget the cross-anchor at a runtime symbol the request plane never grew."""
    target = dataclasses.replace(_entry(CAPABILITY), cross_anchor="def resolve_bounds_nobody(")
    entries = tuple(target if entry.capability == CAPABILITY else entry for entry in mcc.REGISTRY)
    readings, _roadmap, _decl, _ = _judge(entries, _rows())
    _record("C3 cross-anchor retargeted", f"status={readings[CAPABILITY].status}")
    if readings[CAPABILITY].present:
        FAILURES.append("C3 a declaration with no runtime still read present")


def _arm_c4(rows: list[mcc.CensusRow]) -> None:
    """Flip the one wired row back to pending; the bucket triple has to move with it."""
    as_is = _judge(mcc.REGISTRY, rows)[1]
    tampered = [
        dataclasses.replace(row, expressible_today=False) if row.task_id == HD_TASK else row
        for row in rows
    ]
    moved = _judge(mcc.REGISTRY, tampered)[1]
    _record("C4 wired row un-flagged", f"buckets {_buckets(as_is)} -> {_buckets(moved)}")
    if _buckets(moved) == _buckets(as_is):
        FAILURES.append("C4 un-flagging the only wired row left the ladder untouched")


def _arm_c5() -> None:
    """Strip the keyword from the declaration: the bound has to leave the request."""
    spec = intrinio.HISTORICAL_DIVIDENDS
    plain = tuple(
        dataclasses.replace(parameter, relative_default=None) for parameter in spec.params
    )
    stripped = dataclasses.replace(spec, params=plain)
    shipped_window = _window(spec)
    bare_window = _window(stripped)
    _record(
        "C5 keyword stripped",
        f"bounds sent {sorted(shipped_window)} -> {sorted(bare_window)}",
    )
    if {"start_date", "end_date"} <= set(bare_window):
        FAILURES.append("C5 the request kept the window after the declaration lost its carrier")
    if not {"start_date", "end_date"} <= set(shipped_window):
        FAILURES.append("C5 the shipped declaration does not put the window on the request")


def _forecast(rows: list[mcc.CensusRow]) -> None:
    """C95 forecast (186, 10, 83). Measure what the tree would have to look like to reach it."""
    credited = {
        HD_TASK,
        "OBB2-federal_reserve-OvernightBankFundingRate",
        "OBB2-tiingo-CryptoHistorical",
    }
    wired = {HD_TASK}
    as_is = _judge(mcc.REGISTRY, rows)[1]
    tampered = [
        dataclasses.replace(row, expressible_today=True) if row.task_id in credited else row
        for row in rows
    ]
    predicted = _judge(mcc.REGISTRY, tampered)[1]
    _record(
        "C6 all three credited rows flagged expressible",
        f"{_buckets(predicted)} vs {_buckets(as_is)} as-is (C95 wrote {C95_FORECAST_BUCKETS})",
    )
    _record("C6 rows flagged with no package declaring them", sorted(credited - wired))
    if _buckets(predicted) != C95_FORECAST_BUCKETS:
        FAILURES.append(
            f"C6 the forecast is not even reachable by flagging the three rows, got "
            f"{_buckets(predicted)}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
