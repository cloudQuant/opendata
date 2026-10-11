"""Acceptance face for the C97 ``columns.null_sentinel`` kernel.

What is judged is the census instrument's own verdict, so the judge is imported and never re-typed
here: ``count_declarations`` reads the shipped provider declarations, ``verify_registry`` reads the
engine source, ``greedy_cover`` rebuilds the build-order ladder and ``run``/``serialize`` rebuild
the roadmap artifact byte for byte.

The "before" side is neither a hand-written number nor an in-memory tamper: it is the same judge run
against a rev-pinned ``git archive`` of the pre-round tree, so the baseline is a measurement of a
commit rather than a claim about one.

Faces
    F1  presence is *read*: the registry entry is ``read_from_disk``, its declaration anchor and its
        runtime cross-anchor are located by line number in the two engine files, and the production
        declarations that exercise it are counted off the provider tree.
    F2  the wired sentinels are the live declarations' own values, and the predicate the engine
        consults at row-normalisation time says "no value" for exactly the tokens they spell.
    F3  the artifact is the tree: two serialised runs compare equal and compare equal to the
        committed ``capability-roadmap.json``, and the shipped capability is not
        ``built_but_unused``.
    F4  the ladder face beside the pinned-rev baseline: the bucket tuple, the shipped-cover set and
        step-0's front, measured at ``BASE_REV`` and on this checkout.
    F5  the monotonicity face: no row's blocker set may grow, and every row whose blocker set shrank
        must have dropped exactly this round's capability.
    F6  the refusal face: a sentinel the runtime cannot consult is rejected at construction -- the
        silent no-op this round exists to kill -- beside the positive control that must be accepted.

Counterfact arms -- each must move the verdict or this file exits 1
    C1  retarget the anchor at a token the declaration plane never grew            -> flips absent.
    C2  retarget the cross-anchor at a runtime symbol that does not exist          -> flips absent.
    C3  count declarations off the pinned pre-round tree                           -> 19 sites to 0.
    C4  roll the registry entry back to its C96 shape (no carrier, no probe)       -> ladder moves.
    C5  strip the keyword from one production column in place       -> the token survives.
    C6  the four construction refusals, each beside what the engine does accept    -> F6 above.
    C7  flag the five credited rows expressible while nothing declares them        -> §52.8 P4 face.
    C8  the artifact judge must be able to go red: a rolled-back registry does not serialise to the
        committed bytes.

Run: python docs/evidence/C97/null-sentinel/face.py
"""

from __future__ import annotations

import dataclasses
import json
import subprocess  # nosec B404  # read-only git argv, shell disabled
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

import scripts.quality.model_capability_census as mcc  # noqa: E402
from opendata.data.providers._engine import http_json  # noqa: E402
from opendata.data.providers._engine.spec import ColumnSpec, ModelSpec  # noqa: E402
from opendata.data.providers._engine.value_transform import DerivePlan  # noqa: E402
from opendata.data.providers.federal_reserve import specs as fed  # noqa: E402
from opendata.data.providers.intrinio import specs as intrinio  # noqa: E402

if TYPE_CHECKING:  # these names are only ever used in annotations
    from collections.abc import Callable

CAPABILITY = "columns.null_sentinel"
CENSUS_DIR = REPO_ROOT / mcc.CENSUS_DIR_REL
ARTIFACT = REPO_ROOT / mcc.ROADMAP_REL
#: The commit this round started from: the face refuses to run if HEAD has moved, because the
#: baseline it prints would then be a different commit than the one the round was priced against.
BASE_REV = "5917f40"

#: The five rows the C96 ladder credits to this capability. Their census ``expressible_today`` flags
#: are what moves the bucket face, and this round declares none of them -- see F4 and C7.
CREDITED = frozenset(
    {
        "OBB2-benzinga-PriceTarget",
        "OBB2-econdb-AvailableIndicators",
        "OBB2-intrinio-InsiderTrading",
        "OBB2-federal_reserve-FederalFundsRate",
        "OBB2-federal_reserve-SOFR",
    }
)
#: §52.8's P4 prediction for this step: expressible 8->13, pending 271->266, prefix 188->183, with
#: 13 + 183 + 83 = 279. Quoted verbatim so the face can name which part of it the tree gives back.
LEDGER_PREDICTION = (13, 183, 83)

FAILURES: list[str] = []

#: A reading for a capability the registry never heard of, so ``_absent`` stays total.
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
    entries: tuple[mcc.CapabilityEntry, ...],
    rows: list[mcc.CensusRow],
    root: Path = REPO_ROOT,
) -> tuple[dict[str, mcc.EntryReading], mcc.Roadmap, dict[str, int]]:
    """Run the repo's own instrument over a registry, a row set and a source tree."""
    declarations, _by_file, _modules = mcc.count_declarations(entries, root)
    readings = mcc.verify_registry(entries, root, declarations)
    return readings, mcc.greedy_cover(rows, readings), declarations


def _face(roadmap: mcc.Roadmap) -> tuple[int, int, int, int, int]:
    """The ledger's five numbers: expressible, greedy prefix, locked, outside, shipped cover."""
    return (
        roadmap.rows_expressible_today,
        roadmap.rows_unlocked_by_greedy_prefix,
        len(roadmap.rows_still_locked_after_engine_pool),
        len(roadmap.rows_outside_engine),
        len(roadmap.shipped_cover),
    )


def _identity(roadmap: mcc.Roadmap) -> tuple[int, bool]:
    """The bucket identity the ledger is priced against: expressible + prefix + locked == 279."""
    total = (
        roadmap.rows_expressible_today
        + roadmap.rows_unlocked_by_greedy_prefix
        + len(roadmap.rows_still_locked_after_engine_pool)
    )
    return total, total == 279


def _entry(capability: str) -> mcc.CapabilityEntry:
    return next(entry for entry in mcc.REGISTRY if entry.capability == capability)


def _rollback(entry: mcc.CapabilityEntry) -> mcc.CapabilityEntry:
    """The one entry as the C96 round wrote it: a nullable-field anchor, no runtime, no probe."""
    if entry.capability != CAPABILITY:
        return entry
    return mcc.CapabilityEntry(
        capability=entry.capability,
        claim=False,
        engine_work=True,
        proof_file=entry.proof_file,
        anchor="nullable: bool = True",
        why=entry.why,
    )


def _absent(readings: dict[str, mcc.EntryReading], needs: frozenset[str]) -> frozenset[str]:
    """One row's blocker set: the capabilities it needs that this run did not read as present."""
    return frozenset(
        capability for capability in needs if not readings.get(capability, _UNKNOWN).present
    )


def _git(*args: str) -> bytes:
    """One read-only git command, run in this repository and never through a shell."""
    return subprocess.run(  # nosec B603, B607  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout


def _baseline_tree() -> tuple[Path, str]:
    """Extract the pinned pre-round rev into a temporary tree and hand back its root and HEAD.

    Only the paths the judge reads are archived -- the provider packages (the declaration plane and
    the engine kernel) and the census directory (the row set) -- and nothing is written inside the
    repository, so the baseline is a read of a commit rather than of this working tree.
    """
    head = _git("rev-parse", "--short", "HEAD").decode().strip()
    if head != BASE_REV:
        raise RuntimeError(f"HEAD is {head}, but this face prices the round against {BASE_REV}")
    paths = ("opendata/data/providers", str(CENSUS_DIR.relative_to(REPO_ROOT)))
    temp = Path(tempfile.mkdtemp(prefix="c97-baseline-"))
    stream = subprocess.Popen(  # nosec B603, B607  # noqa: S603
        ["git", "-C", str(REPO_ROOT), "archive", BASE_REV, "--", *paths],  # noqa: S607
        stdout=subprocess.PIPE,
    )
    if stream.stdout is None:
        raise RuntimeError("git archive gave no stdout")
    try:
        subprocess.run(  # nosec B603, B607  # noqa: S603
            ["tar", "-x", "-C", str(temp)],  # noqa: S607
            stdin=stream.stdout,
            check=True,
        )
    finally:
        code = stream.wait()
    if code:
        raise RuntimeError(f"git archive {BASE_REV} failed rc={code}")
    return temp, head


def _sentinel_columns() -> list[tuple[str, ColumnSpec]]:
    """Every production column this repo declares a sentinel set on, read off the live objects."""
    return [
        (spec.model, column)
        for spec in (fed.MONEY_MEASURES, fed.TREASURY_RATES, fed.SVENSSON_YIELD_CURVE)
        for column in spec.columns
        if column.null_sentinel
    ]


def _column(
    name: str,
    sentinel: tuple[Any, ...] = ("ND",),
    required: bool = False,
    nullable: bool = True,
) -> ColumnSpec:
    """One copied str column: the smallest declaration the refusal face needs.

    ``sentinel`` is typed loosely on purpose: three of the four refusals are about a
    declaration that is *wrong*, including the one that spells a sentinel as a number, so the
    helper must be able to hand the dataclass something its own validator will refuse.
    """
    return ColumnSpec(
        name, "str", source_key=name, required=required, nullable=nullable, null_sentinel=sentinel
    )


def _spec(columns: tuple[ColumnSpec, ...], derive: DerivePlan | None = None) -> ModelSpec:
    """A throwaway model whose only interesting parts are its columns and one derive plan."""
    declaration: dict[str, Any] = {
        "model": "C97Face",
        "domain": "probe_c97_face",
        "asset_class": "macro",
        "period": "d",
        "market": "us",
        "base_url": "https://probe.test",
        "path": "/sentinels",
        "rows_pointer": "records",
        "columns": columns,
        "scenario": "离线判定面：列哨兵置空的拒绝面",
        "error_prefix": "PROBE_C97_FACE",
    }
    if derive is not None:
        declaration["column_derives"] = (derive,)
    return ModelSpec(**declaration)


def _stamped() -> ModelSpec:
    """The no-op this round hunts: a sentinel on a column a derive plan stamps, never reads."""
    return _spec(
        (_column("value"),),
        DerivePlan(column="value", source="ND", source_kind="constant"),
    )


def _unstamped() -> ModelSpec:
    """The positive control: the same column with the stamp removed must be accepted."""
    return _spec((_column("value"),))


def _non_nullable() -> ColumnSpec:
    """A sentinel whose target the row model refuses to leave empty."""
    return _column("closed", required=True, nullable=False)


def _non_text() -> ColumnSpec:
    """A sentinel spelled as a number: the runtime matches published text, and only text."""
    return _column("number", sentinel=(0,))


def _duplicated() -> ColumnSpec:
    """The same token named twice, which is not a set a reader can consult."""
    return _column("twice", sentinel=("ND", "ND"))


def _refusal(thunk: Callable[[], object]) -> str | None:
    """Build one declaration and hand back the message it was refused with, or None.

    The refusal has to come from the engine's own validator rather than from this file: a
    declaration that constructs is one the runtime would accept and then ignore.
    """
    try:
        thunk()
    except ValueError as error:
        return str(error)
    return None


REFUSALS: tuple[tuple[str, Callable[[], object]], ...] = (
    ("a derive plan stamps the column", _stamped),
    ("a non-nullable column claims a null", _non_nullable),
    ("a non-str sentinel spelling", _non_text),
    ("the same sentinel named twice", _duplicated),
)


def _why_not_wired(row: mcc.CensusRow) -> str:
    """The in-tree claim that keeps one credited row's flag down, read off its own package.

    The row's own ``upstream_model`` is what a package's refusal dictionary is keyed on, and the
    census spells some of them in the release's uppercase ("SOFR") where the package uses the model
    class's case ("Sofr"), so both spellings are looked up before the row is called unwired.
    """
    provider = row.provider
    if not (REPO_ROOT / "opendata" / "data" / "providers" / provider).is_dir():
        return f"{provider}: no package in the tree, so nothing declares {row.upstream_model}"
    refused = {
        "federal_reserve": {key.casefold(): key for key in fed.NOT_DECLARED},
        "intrinio": {key.casefold(): key for key in intrinio.NOT_DECLARABLE},
    }.get(provider, {})
    hit = refused.get(row.upstream_model.casefold())
    if hit is not None:
        return f"{provider} records {row.upstream_model} as undeclared: no ModelSpec names it"
    return f"{provider}/{row.upstream_model}: no in-tree refusal found -- the flag should be set"


def main() -> int:
    """Run every face against the repo's own judge and print what it measured."""
    rows = mcc.load_rows(CENSUS_DIR)
    readings, roadmap, declarations = _judge(mcc.REGISTRY, rows)
    reading = readings[CAPABILITY]

    base_tree, head = _baseline_tree()
    base_rows = mcc.load_rows(base_tree / CENSUS_DIR.relative_to(REPO_ROOT))
    base_readings, base_roadmap, base_decl = _judge(mcc.REGISTRY, base_rows, root=base_tree)

    print(f"F1 presence is read off the engine, not asserted (HEAD {head})")
    _record("status/present", f"{reading.status}/{reading.present}")
    _record("anchor", f"{reading.proof_file}:{reading.anchor_line_number} {reading.anchor!r}")
    _record(
        "cross-anchor",
        f"{reading.cross_file} {reading.cross_anchor!r} found={reading.cross_anchor_found}",
    )
    _record("declaration scan", f"{reading.production_declarations} sites")
    _record("the same entry on the pinned rev", base_readings[CAPABILITY].status)
    if not (reading.present and reading.read_from_disk and _entry(CAPABILITY).probe is not None):
        FAILURES.append("F1 presence must be measured from disk through a real declaration probe")
    if not (reading.anchor_found and reading.cross_anchor_found):
        FAILURES.append("F1 both anchors must be located on disk for presence to be read")
    if base_readings[CAPABILITY].present:
        FAILURES.append("F1 the pinned pre-round rev already read this capability as present")

    print("\nF2 the wired sentinels are the declarations' own values, and the runtime reads them")
    sites = _sentinel_columns()
    by_model: dict[str, int] = {}
    for model, _ in sites:
        by_model[model] = by_model.get(model, 0) + 1
    _record("sentinel-bearing columns", f"{len(sites)} over {sorted(by_model)}")
    _record("spellings per model", sorted({(m, c.null_sentinel) for m, c in sites}))
    token = next(c for _m, c in sites if "ND" in c.null_sentinel)
    _record("runtime predicate on 'ND'", http_json._is_null_sentinel(token, "ND"))
    _record("...and on the published value '3.70'", http_json._is_null_sentinel(token, "3.70"))
    if not by_model:
        FAILURES.append("F2 no production column declares a sentinel, so the kernel is unused")
    if not http_json._is_null_sentinel(token, "ND"):
        FAILURES.append("F2 the runtime does not consult the token production spells")
    if http_json._is_null_sentinel(token, "3.70"):
        FAILURES.append("F2 the runtime nulls a value the source publishes")

    print("\nF3 the artifact is what the tree produces")
    once = mcc.serialize(mcc.run(round_id="C97", root=REPO_ROOT, census_dir=CENSUS_DIR))
    twice = mcc.serialize(mcc.run(round_id="C97", root=REPO_ROOT, census_dir=CENSUS_DIR))
    shipped = ARTIFACT.read_text(encoding="utf-8")
    unused = sorted(str(item) for item in json.loads(once)["built_but_unused"])
    _record("two runs identical", once == twice)
    _record("recomputed == committed", once == shipped)
    _record("bytes", f"{len(once.encode())} recomputed vs {len(shipped.encode())} on disk")
    _record("built_but_unused", unused)
    if once != twice:
        FAILURES.append("F3 the same tree serialised twice is not reproducible")
    if once != shipped:
        FAILURES.append("F3 the committed artifact is not what this tree produces")
    if CAPABILITY in unused:
        FAILURES.append("F3 a capability with production declarations is still built_but_unused")

    print("\nF4 the ladder face, beside the pinned-rev baseline")
    _record("after face (expr, prefix, locked, outside, cover)", _face(roadmap))
    _record("before face at the pinned rev", _face(base_roadmap))
    _record(
        "after step-0 front",
        f"{roadmap.build_order[0].capability} +{roadmap.build_order[0].rows_newly_unlocked}",
    )
    _record(
        "before step-0 front",
        f"{base_roadmap.build_order[0].capability} "
        f"+{base_roadmap.build_order[0].rows_newly_unlocked}",
    )
    _record(
        "declaration sites before / after",
        f"{base_decl.get(CAPABILITY, 0)} / {declarations.get(CAPABILITY, 0)}",
    )
    covered = {item.task_id for item in roadmap.shipped_cover}
    _record("after shipped-cover", sorted(covered))
    _record("before shipped-cover", sorted(i.task_id for i in base_roadmap.shipped_cover))
    _record("identity (expressible+prefix+locked == 279)", _identity(roadmap))
    _record("credited rows newly covered", sorted(CREDITED & covered))
    if base_decl.get(CAPABILITY):
        FAILURES.append("F4 the pinned pre-round tree already carries this round's declarations")
    if roadmap.build_order[0].capability == CAPABILITY:
        FAILURES.append("F4 a shipped capability is still a roadmap step")
    if CREDITED - covered:
        FAILURES.append(f"F4 a credited row is not covered: {sorted(CREDITED - covered)}")
    if _face(roadmap)[:4] != _face(base_roadmap)[:4]:
        FAILURES.append(
            "F4 the bucket face moved without a row being declared -- recheck the judge"
        )

    print("\nF5 monotonicity: no blocker set shrank except on this capability")
    shrank: list[tuple[str, frozenset[str]]] = []
    grew: list[str] = []
    for row in rows:
        after = _absent(readings, row.needs)
        before = _absent(base_readings, row.needs)
        if after - before:
            grew.append(row.task_id)
        if before - after:
            shrank.append((row.task_id, before - after))
    _record("rows whose blocker set shrank", sorted(task for task, _d in shrank))
    _record("blockers dropped", sorted({name for _t, dropped in shrank for name in dropped}))
    _record("rows whose blocker set grew", grew)
    _record("shrink / total rows", f"{len(shrank)} / {len(rows)}")
    if any(dropped != frozenset({CAPABILITY}) for _t, dropped in shrank):
        FAILURES.append("F5 a row lost a blocker this round did not ship")
    if grew:
        FAILURES.append(f"F5 a row gained a blocker: {grew}")
    if not shrank:
        FAILURES.append("F5 nothing shrank, so the kernel unlocked no row's blocker set")

    print("\nF6 the refusal face: a sentinel the runtime cannot consult is rejected")
    for label, thunk in REFUSALS:
        message = _refusal(thunk)
        if message is None:
            FAILURES.append(f"F6 {label} was accepted -- the kernel would be a silent no-op")
        else:
            _record(label, f"refused: {message[:92]}")
    try:
        accepted = _unstamped()
    except ValueError as error:
        FAILURES.append(f"F6 the un-stamped positive control was refused: {error}")
    else:
        sentinels = [column.null_sentinel for column in accepted.columns]
        _record("positive control, same column un-stamped", sentinels)

    print("\nCounterfact arms -- each one must move the verdict")
    _arm_c1()
    _arm_c2()
    _arm_c3(base_decl, declarations)
    _arm_c4(rows, roadmap, base_roadmap)
    _arm_c5()
    _arm_c7(rows)
    _arm_c8()

    print("\nVERDICT:", "PASS" if not FAILURES else "FAIL")
    for failure in FAILURES:
        print("  -", failure)
    return 0 if not FAILURES else 1


def _arm_c1() -> None:
    """Retarget the anchor at a token the declaration plane never grew; presence must go."""
    target = dataclasses.replace(_entry(CAPABILITY), anchor="sentinel_values: tuple[str, ...] = ()")
    entries = tuple(target if entry.capability == CAPABILITY else entry for entry in mcc.REGISTRY)
    readings, _roadmap, _decl = _judge(entries, mcc.load_rows(CENSUS_DIR))
    _record("C1 anchor retargeted", f"status={readings[CAPABILITY].status}")
    if readings[CAPABILITY].present:
        FAILURES.append("C1 a missing anchor still read present")


def _arm_c2() -> None:
    """Retarget the cross-anchor at a runtime symbol the row plane never grew."""
    target = dataclasses.replace(_entry(CAPABILITY), cross_anchor="def _is_null_sentinel_never(")
    entries = tuple(target if entry.capability == CAPABILITY else entry for entry in mcc.REGISTRY)
    readings, _roadmap, _decl = _judge(entries, mcc.load_rows(CENSUS_DIR))
    _record(
        "C2 cross-anchor retargeted",
        f"status={readings[CAPABILITY].status} present={readings[CAPABILITY].present}",
    )
    if readings[CAPABILITY].present:
        FAILURES.append("C2 a declared sentinel the runtime never consults still read present")


def _arm_c3(base_decl: dict[str, int], declarations: dict[str, int]) -> None:
    """The declaration count must come from the tree: the pinned pre-round tree carries none."""
    _record(
        "C3 declaration sites",
        f"this checkout {declarations.get(CAPABILITY, 0)} -> pinned rev "
        f"{base_decl.get(CAPABILITY, 0)}",
    )
    if not declarations.get(CAPABILITY):
        FAILURES.append("C3 this checkout counts no declaration of the capability it ships")
    if base_decl.get(CAPABILITY):
        FAILURES.append("C3 the pinned pre-round tree already declares a sentinel")


def _arm_c4(rows: list[mcc.CensusRow], roadmap: mcc.Roadmap, base_roadmap: mcc.Roadmap) -> None:
    """Roll the registry entry back to its C96 shape: the ladder must put the step back."""
    entries = tuple(_rollback(entry) for entry in mcc.REGISTRY)
    readings, back, _decl = _judge(entries, rows)
    _record(
        "C4 entry rolled back to the C96 anchor",
        f"present={readings[CAPABILITY].present} face={_face(back)} "
        f"step0={back.build_order[0].capability}",
    )
    if readings[CAPABILITY].present:
        FAILURES.append("C4 the rolled-back entry still read present")
    if _face(back)[:4] != _face(base_roadmap)[:4]:
        FAILURES.append(
            f"C4 the in-memory rollback {_face(back)} does not reproduce the pinned rev "
            f"{_face(base_roadmap)}"
        )
    if _face(back) == _face(roadmap):
        FAILURES.append("C4 rolling the capability back left the ladder untouched")


def _arm_c5() -> None:
    """Strip the keyword from one production column in place: the token has to survive."""
    spec = fed.TREASURY_RATES
    stripped = dataclasses.replace(
        spec,
        columns=tuple(
            dataclasses.replace(column, null_sentinel=()) if column.name == "year_1" else column
            for column in spec.columns
        ),
    )
    kept = next(c for c in spec.columns if c.name == "year_1")
    cut = next(c for c in stripped.columns if c.name == "year_1")
    _record(
        "C5 keyword stripped",
        f"'ND' nulled: declared={http_json._is_null_sentinel(kept, 'ND')} "
        f"stripped={http_json._is_null_sentinel(cut, 'ND')}",
    )
    if not http_json._is_null_sentinel(kept, "ND"):
        FAILURES.append("C5 the shipped declaration does not null the token")
    if http_json._is_null_sentinel(cut, "ND"):
        FAILURES.append("C5 the token was nulled after the declaration lost its carrier")


def _arm_c7(rows: list[mcc.CensusRow]) -> None:
    """§52.8's P4 face is reachable only by flagging undeclared rows. Measure both sides."""
    as_is = _judge(mcc.REGISTRY, rows)[1]
    flagged = [
        dataclasses.replace(row, expressible_today=True) if row.task_id in CREDITED else row
        for row in rows
    ]
    predicted = _judge(mcc.REGISTRY, flagged)[1]
    _record(
        "C7 the five credited rows flagged with nothing declared",
        f"{_face(predicted)[:3]} vs §52.8 P4 {LEDGER_PREDICTION} vs measured {_face(as_is)[:3]}",
    )
    if _face(predicted)[:3] != LEDGER_PREDICTION:
        FAILURES.append(
            f"C7 the §52.8 prediction is not even reachable by flagging, got {_face(predicted)[:3]}"
        )
    by_task = {row.task_id: row for row in rows}
    reasons = [_why_not_wired(by_task[task]) for task in sorted(CREDITED)]
    _record("C7 why each credited row stays pending", reasons)
    stragglers = [text for text in reasons if "should be" in text]
    if stragglers:
        FAILURES.append(
            f"C7 a credited row has no recorded refusal, so its flag is a lie: {stragglers}"
        )
    _record(
        "C7 verdict",
        "REFUTED as a shipping prediction: the (13, 183, 83) face is reachable only by flipping "
        "five census flags while no package puts forward one of the five ModelSpecs",
    )


def _arm_c8() -> None:
    """The artifact judge must be able to go red: this round's entry is what the bytes depend on."""
    shipped = ARTIFACT.read_text(encoding="utf-8")
    entries = tuple(_rollback(entry) for entry in mcc.REGISTRY)
    rolled = mcc.serialize(
        mcc.run(round_id="C97", root=REPO_ROOT, census_dir=CENSUS_DIR, entries=entries)
    )
    _record("C8 rolled-back artifact differs from the committed bytes", rolled != shipped)
    if rolled == shipped:
        FAILURES.append("C8 the committed bytes are insensitive to the registry entry")


if __name__ == "__main__":
    raise SystemExit(main())
