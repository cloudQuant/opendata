"""C90 witness: do the ``columns.cast`` / ``columns.rescale`` entries still describe this engine?

Those two registry entries are pinned ``claim=False``, so the census reads both capabilities absent
and every row that needs one of them stays out of cover. Their anchors and their ``why`` text were
written against ``ColumnSpec`` and ``_column_type`` -- before ``083b7e4`` landed the derive-stage
seam. This witness asks one question of each entry: is the limitation it states still the engine's
limitation? Nothing is typed in as a conclusion: every refutation is an arm that *executes* the
surface, and every count comes out of the repo's own scanner and the repo's own greedy cover.

Arms, all judged by :func:`model_capability_census.greedy_cover` itself, so a flip set is the
roadmap's own ``shipped_cover`` difference and not a predicate rebuilt here:

  * arm A -- the readings exactly as ``verify_registry`` produces them (cast/rescale read absent);
  * arm B -- arm A plus ``columns.cast`` and ``columns.rescale`` read present;
  * arm C -- arm B plus ``rows.filter_comparison`` and ``request.serialised_param`` (printed only:
    which rows this unlocks is a measurement, and the answer is not assumed);
  * arm M -- every ``engine_work`` capability the registry holds, the arm that proves this counter
    can reach a non-zero row set, so a 0 in arm B is a measurement and not a dead instrument.

The arms compare coverage, not the stored per-row verdict, so the face also prints the two
directions in which ``expressible_today`` and the cover can disagree: pending rows the cover already
holds, and expressible rows whose canonical need set the cover does not hold.

Once these two entries read present, an arm that "adds" them adds nothing, so arm B's empty gain is
structurally vacuous. The non-vacuous face is therefore the mentioner census: ``MENTIONERS`` rows
that list one audited capability, ``MENTIONER-BLOCKERS`` the absent capabilities that still keep
each of them out, ``MENTIONERS-IN-COVER`` the headroom counted directly, ``CONTROL`` a forged
cast-only row proving that predicate can return non-zero, and ``C89-PROMISED`` the three rows an
earlier round published as this re-anchor's payoff, re-measured against today's need sets.
No network, no database, no gate.
"""

from __future__ import annotations

import dataclasses
import subprocess  # nosec B404  # read-only git argv, shell disabled
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn

if TYPE_CHECKING:
    from collections.abc import Callable

    from opendata.data.providers._engine.spec import ValueKind

REPO = next(
    ancestor
    for ancestor in Path(__file__).resolve().parents
    if (ancestor / "scripts" / "quality" / "model_capability_census.py").is_file()
)
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "quality"))
import model_capability_census as mcc  # noqa: E402

from opendata.data.providers._engine.http_json import (  # noqa: E402
    build_row_model,
    normalize_record,
)
from opendata.data.providers._engine.spec import ColumnSpec, ModelSpec  # noqa: E402
from opendata.data.providers._engine.value_transform import (  # noqa: E402
    STAGE_SEQUENCE,
    CastSpec,
    DerivePlan,
    DeriveStage,
    ScaleSpec,
    apply_cast,
    apply_scale,
    resolve_output_units,
)

CENSUS_DIR = REPO / mcc.CENSUS_DIR_REL
UNDER_AUDIT = ("columns.cast", "columns.rescale")
#: Capabilities arm C adds; whether they unlock anything is one of the questions, not an input.
ARM_C_EXTRA = ("rows.filter_comparison", "request.serialised_param")
#: Quoted from C89 (开发验收记录 §45 / docs/evidence/C89/oecd-blocker-recheck.md §7 item 3): the
#: three rows that re-anchor was published to flip. The ids are a citation; their need sets and
#: cover status below are re-measured on this tree.
C89_PROMISED: Final = (
    "OBB2-benzinga-AnalystSearch",
    "OBB2-federal_reserve-YieldCurve",
    "OBB2-government_us-TreasuryAuctions",
)
EPOCH_MS: Final = int(datetime(2024, 7, 4, tzinfo=timezone.utc).timestamp() * 1000)


def die(msg: str) -> NoReturn:
    """Print a FAIL line and exit non-zero."""
    print(f"FAIL: {msg}")
    raise SystemExit(1)


def git(argv: list[str]) -> str:
    """Run a read-only git command and return its stdout, failing loudly."""
    proc = subprocess.run(  # nosec B603, B607  # noqa: S603
        argv,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        die(f"git {' '.join(argv)} exited {proc.returncode}: {proc.stderr.strip()[:160]}")
    return proc.stdout.strip()


def spec(columns: tuple[ColumnSpec, ...], derives: tuple[DerivePlan, ...]) -> ModelSpec:
    """One offline declaration whose domain is unregistered, so rows are declared columns."""
    return ModelSpec(
        model="anchor_honesty_probe",
        domain="c90_anchor_honesty_probe",
        asset_class="equity",
        period="d",
        market="us",
        base_url="https://example.invalid",
        path="/probe",
        columns=columns,
        params=(),
        scenario="离线探针：cast/scale 两个锚点的诚实性",
        error_prefix="PROBE",
        column_derives=derives,
    )


def row_of(
    column: str,
    kind: ValueKind,
    source_key: str,
    published: object,
    stage: DeriveStage,
    units: str = "",
) -> dict[str, Any]:
    """Run one record through the production seam with exactly one declared stage."""
    declared = spec(
        (ColumnSpec(column, kind, source_key=source_key, units=units),),
        (DerivePlan(column=column, stages=(stage,)),),
    )
    model = build_row_model(declared)
    row = normalize_record({source_key: published}, None, declared, model, None)
    return dict(row.model_dump())


def refuse(label: str, thunk: Callable[[], object], needle: str) -> None:
    """Print the refusal a guarded arm must raise, dying when the arm accepts instead."""
    try:
        thunk()
    except (ValueError, TypeError) as error:
        text = f"{type(error).__name__}: {error}"
        if needle not in text:
            die(f"{label}: refused with the wrong words: {text[:200]}")
        print(f"REFUSE {label:<46} -> {text[:150]}")
        return
    die(f"{label}: expected a refusal, the arm accepted")


def main() -> int:
    """Measure every face and print the verdicts."""
    print(f"HEAD {git(['git', 'rev-parse', '--short', 'HEAD'])}")
    vt = REPO / mcc.VALUE_TRANSFORM_REL
    print(f"KERNEL {mcc.VALUE_TRANSFORM_REL} bytes={vt.stat().st_size} STAGE={STAGE_SEQUENCE!r}")

    declarations, by_file, modules = mcc.count_declarations(mcc.REGISTRY, REPO)
    readings = mcc.verify_registry(mcc.REGISTRY, REPO, declarations)
    rows = mcc.load_rows(CENSUS_DIR)
    entries = {entry.capability: entry for entry in mcc.REGISTRY}
    for capability in UNDER_AUDIT:
        entry, reading = entries[capability], readings[capability]
        print(
            f"ENTRY {capability:<17} claim={entry.claim} read_from_disk={entry.read_from_disk} "
            f"status={reading.status} present={reading.present} "
            f"declarations={reading.production_declarations}"
        )
        print(
            f"  proof_file={entry.proof_file} anchor={entry.anchor!r} "
            f"line={reading.anchor_line_number}"
        )
        print(f"  probe={entry.probe!r}")
        print(f"  WHY verbatim: {entry.why}")

    # ---- positive controls: the surface each why-sentence says does not exist ----
    epoch = apply_cast(CastSpec.epoch_ms_to_date("UTC"), EPOCH_MS)
    epoch_ny = apply_cast(CastSpec.epoch_ms_to_date("America/New_York"), EPOCH_MS)
    print(f"ARM apply_cast epoch_ms_to_date(UTC) {EPOCH_MS} -> {epoch!r}")
    print(f"ARM apply_cast epoch_ms_to_date(America/New_York) -> {epoch_ny!r}")
    if (epoch, epoch_ny) != (date(2024, 7, 4), date(2024, 7, 3)):
        die("epoch cast did not produce the dates the published instant maps to")
    slash = apply_cast(CastSpec.date_string("%m/%d/%Y"), "07/04/2024")
    period = apply_cast(CastSpec.date_string("%Y-%m"), "2024-03")
    print(f"ARM apply_cast date_string('%m/%d/%Y') '07/04/2024' -> {slash!r}")
    print(f"ARM apply_cast date_string('%Y-%m') '2024-03' -> {period!r}")
    if (slash, period) != (date(2024, 7, 4), date(2024, 3, 1)):
        die("date_string cast did not reformat the published text")

    percent = ScaleSpec.percent_to_fraction()
    scaled = apply_scale(percent, Decimal("4.33"))
    strike = apply_scale(ScaleSpec.strike_per_mille(output_units="USD"), 43300)
    print(f"ARM apply_scale percent_to_fraction Decimal('4.33') -> {scaled!r}")
    print(f"ARM apply_scale strike_per_mille 43300 -> {strike!r}")
    if (scaled, strike) != (Decimal("0.0433"), Decimal("43.3")):
        die("rescale did not divide by the declared factor")
    units_after = resolve_output_units("percent", percent)
    units_none = resolve_output_units("percent", None)
    print(f"ARM resolve_output_units('percent', scale) -> {units_after!r}")
    print(f"ARM resolve_output_units('percent', None)  -> {units_none!r}")
    if (units_after, units_none) != ("fraction", "percent"):
        die("output units did not move onto the rescaled column")

    cast_row = row_of(
        "month",
        "date",
        "Period",
        "2024-03",
        DeriveStage(kind="cast", cast=CastSpec.date_string("%Y-%m")),
    )
    scale_row = row_of(
        "yield_rate",
        "float",
        "Yield",
        "4.33",
        DeriveStage(kind="scale", scale=percent),
        units="percent",
    )
    print(f"ARM normalize_record cast stage  -> month={cast_row['month']!r}")
    print(f"ARM normalize_record scale stage -> yield_rate={scale_row['yield_rate']!r}")
    if cast_row["month"] != date(2024, 3, 1) or scale_row["yield_rate"] != 0.0433:
        die("the production seam normalize_record did not run the declared stage")

    stage_factory: Any = DeriveStage
    column_factory: Any = ColumnSpec
    refuse("stage kind not in STAGE_SEQUENCE", lambda: stage_factory(kind="pivot"), "not one of")
    refuse(
        'stage kind="cast" carrying a ScaleSpec',
        lambda: stage_factory(kind="cast", scale=percent),
        "must carry a CastSpec",
    )
    refuse(
        "ColumnSpec carries no format= field",
        lambda: column_factory("month", "date", format="%Y-%m"),
        "format",
    )
    refuse(
        "ColumnSpec carries no scale= field",
        lambda: column_factory("rate", "float", scale=percent),
        "scale",
    )

    # ---- declaration counts, read by the repo's own scanner (provider modules only) ----
    def probe_count(label: str, probe: mcc.DeclarationProbe, expect: int | None) -> int:
        """Count one probe through ``count_declarations``, optionally asserting its reading."""
        entry = mcc.CapabilityEntry(
            capability=label,
            claim=True,
            engine_work=True,
            proof_file=mcc.VALUE_TRANSFORM_REL,
            anchor="STAGE_SEQUENCE",
            why="synthetic probe carrier for this witness",
            probe=probe,
        )
        totals, per_file, scanned = mcc.count_declarations((entry,), REPO)
        count = int(totals[label])
        print(f"COUNT {label:<44} -> {count} files={sorted(per_file[label])} modules={scanned}")
        if expect is not None and count != expect:
            die(f"{label}: expected {expect} declarations, scanner read {count}")
        return count

    live_probe = mcc.DeclarationProbe(call="DerivePlan")
    control = probe_count("DerivePlan (live carrier)", live_probe, None)
    if control == 0:
        die("the scanner read 0 for a carrier known to exist, so its zeros would prove nothing")
    live_cast = probe_count(
        'DeriveStage kind="cast"', mcc.DeclarationProbe("DeriveStage", "kind", '"cast"'), 0
    )
    live_scale = probe_count(
        'DeriveStage kind="scale"', mcc.DeclarationProbe("DeriveStage", "kind", '"scale"'), 0
    )
    registry_probe = mcc.DeclarationProbe("ColumnSpec", "format")
    probe_count("ColumnSpec keyword=format (registry probe)", registry_probe, 0)
    probe_count(
        "ColumnSpec keyword=scale (registry probe)", mcc.DeclarationProbe("ColumnSpec", "scale"), 0
    )

    # ---- cover arms, all through the repo's own greedy cover ----
    by_task = {row.task_id: row for row in rows}
    covered_a = frozenset(cap for cap, r in readings.items() if r.present)
    engine_all = frozenset(cap for cap, r in readings.items() if r.engine_work)

    def cover(assumed: frozenset[str]) -> tuple[set[str], Any]:
        """Read shipped cover with ``assumed`` capabilities additionally present."""
        merged = {
            cap: (dataclasses.replace(r, status="present", present=True) if cap in assumed else r)
            for cap, r in readings.items()
        }
        road = mcc.greedy_cover(rows, merged)
        return {row.task_id for row in road.shipped_cover}, road

    def newly(covered: frozenset[str], before: frozenset[str]) -> set[str]:
        """Rows pending on disk whose needs the cover holds now but did not hold before."""
        return {
            row.task_id
            for row in rows
            if not row.expressible_today
            and row.needs
            and row.needs <= covered
            and not row.needs <= before
        }

    shipped_a, road_a = cover(frozenset())
    assumed_b = frozenset(UNDER_AUDIT)
    shipped_b, road_b = cover(assumed_b)
    extras = frozenset(cap for cap in ARM_C_EXTRA if cap in readings)
    if len(extras) != len(ARM_C_EXTRA):
        die(f"arm C named a capability the registry does not hold: {sorted(extras)}")
    shipped_c, _ = cover(assumed_b | extras)
    shipped_m, road_m = cover(engine_all)

    print(f"CENSUS rows={len(rows)} pending={road_a.rows_pending} covered_now={len(covered_a)}")
    print(f"ARM A shipped_cover={len(shipped_a)} {sorted(shipped_a)}")
    for name, ship in (("B", shipped_b), ("C", shipped_c), ("M", shipped_m)):
        gained = sorted(ship - shipped_a)
        print(f"ARM {name} shipped_cover={len(ship)} newly_covered={len(gained)} {gained}")
    print(
        f"LADDER steps A={len(road_a.build_order)} B={len(road_b.build_order)} "
        f"M={len(road_m.build_order)} cumulative A={road_a.rows_unlocked_by_greedy_prefix} "
        f"B={road_b.rows_unlocked_by_greedy_prefix} M={road_m.rows_unlocked_by_greedy_prefix} "
        f"still_locked A={len(road_a.rows_still_locked_after_engine_pool)} "
        f"M={len(road_m.rows_still_locked_after_engine_pool)}"
    )

    # ---- the two directions in which the stored flag and the cover can disagree ----
    gained_b = sorted(shipped_b - shipped_a)
    direct_b = sorted(newly(covered_a | assumed_b, covered_a))
    if gained_b != direct_b:
        die(f"two paths to arm B disagree: greedy {gained_b} vs direct {direct_b}")
    if not shipped_a <= shipped_b:
        die("arm B lost a row arm A covers, so the flipped readings are not monotone")
    if not shipped_m - shipped_a:
        die("arm M reached no rows either, so this counter cannot be shown reaching non-zero")
    for task_id in sorted(shipped_a):
        row = by_task[task_id]
        print(
            f"IN-COVER-BUT-PENDING {task_id} expressible_today={row.expressible_today} "
            f"needs={sorted(row.needs)} raw={list(row.raw_labels)}"
        )
    stale_true = sorted(
        row.task_id
        for row in rows
        if row.expressible_today and row.needs and not row.needs <= covered_a
    )
    print(f"EXPRESSIBLE-BUT-NOT-COVERED count={len(stale_true)} {stale_true}")
    unmet = {
        row.task_id: sorted(row.needs - covered_a)
        for row in rows
        if not row.expressible_today and (row.needs - covered_a) <= assumed_b
    }
    print(f"UNMET-NEEDS-ONLY-CAST/RESCALE count={len(unmet)} {unmet}")
    mentioned = {
        cap: sum(1 for row in rows if cap in row.needs and not row.expressible_today)
        for cap in UNDER_AUDIT
    }
    print(f"MENTIONS pending rows listing each capability: {mentioned}")
    registry_counts = {cap: declarations[cap] for cap in UNDER_AUDIT}
    print(f"REGISTRY_DECLARATIONS {registry_counts}")

    # ---- the non-vacuous headroom face: the mentioners and what really blocks them ----
    audited = frozenset(UNDER_AUDIT)

    def mentioners_in_cover(population: list[mcc.CensusRow]) -> list[str]:
        """Rows listing one audited capability whose whole need set the shipped cover holds."""
        return sorted(
            row.task_id
            for row in population
            if not row.expressible_today and row.needs & audited and row.needs <= covered_a
        )

    mentioners = [row for row in rows if not row.expressible_today and row.needs & audited]
    blockers: dict[str, int] = {}
    for row in mentioners:
        for cap in row.needs - covered_a:
            blockers[cap] = blockers.get(cap, 0) + 1
    print(
        f"MENTIONERS n={len(mentioners)} pending rows list columns.cast or columns.rescale "
        f"(cast {mentioned['columns.cast']} + rescale {mentioned['columns.rescale']} mentions, "
        f"a row listing both is counted once)"
    )
    print(
        f"MENTIONER-BLOCKERS distinct={len(blockers)} "
        f"{sorted(blockers.items(), key=lambda kv: (-kv[1], kv[0]))[:10]}"
    )
    real_in_cover = mentioners_in_cover(rows)
    print(f"MENTIONERS-IN-COVER n={len(real_in_cover)} {real_in_cover}")
    forged = dataclasses.replace(
        rows[0],
        task_id="C90-FORGED-cast-only",
        needs=frozenset({"columns.cast"}),
        expressible_today=False,
    )
    control_in_cover = mentioners_in_cover([*rows, forged])
    if control_in_cover != sorted([*real_in_cover, forged.task_id]):
        die(
            "the headroom predicate cannot be shown reaching non-zero: forging a row whose "
            f"need set is only columns.cast yielded {control_in_cover}"
        )
    print(
        f"CONTROL forged={forged.task_id} needs={sorted(forged.needs)} "
        f"in_cover_after_forging={len(control_in_cover)} so the zero above is a reading, "
        "not a dead predicate"
    )
    for task_id in C89_PROMISED:
        row = by_task.get(task_id)
        if row is None:
            die(f"C89 published {task_id} as a flip candidate but this census has no such row")
        missing = row.needs - covered_a
        if not missing and task_id not in shipped_a:
            die(
                f"{task_id} needs nothing the registry does not already read present, yet the "
                "greedy cover does not hold it -- the cover and this predicate disagree"
            )
        print(
            f"C89-PROMISED {task_id:<40} in_cover={task_id in shipped_a} "
            f"needs={sorted(row.needs)} still_missing={sorted(missing)}"
        )

    print(f"CAST_WHY_REFUTED={epoch == date(2024, 7, 4) and slash == date(2024, 7, 4)}")
    print(f"RESCALE_WHY_REFUTED={scaled == Decimal('0.0433') and units_after == 'fraction'}")
    print(f"VERDICT headroom_rows_B={len(gained_b)}")
    print(f"VERDICT arm_M_rows={len(shipped_m - shipped_a)} in_cover_pending_now={len(shipped_a)}")
    print(f"VERDICT provider_declarations_of_stages={live_cast + live_scale}")
    print(
        f"VERDICT mentioner_rows={len(mentioners)} "
        f"mentioners_already_in_cover={len(real_in_cover)} "
        f"c89_promised_in_cover={sum(1 for t in C89_PROMISED if t in shipped_a)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
