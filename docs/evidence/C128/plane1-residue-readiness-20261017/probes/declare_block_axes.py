"""Run the four conversion axes over one provider's ``DECLARE_AND_WITNESS`` block.

Wave 26's step 0 (``incomplete_row_guard_check.py``, face in
``runs/wave26-step0-second-axis.txt``) measured the seven ``INCOMPLETE``
identities and read ``convertible=0``. Its row population was that one judge
status, so the batch the same round priced as cheapest --
``DECLARE_AND_WITNESS=28``, of which yfinance holds 26 -- stayed unmeasured on
the third and fourth axis. This probe takes the *same* four axes and points
them at that block, with the population and the bucket rule both read off the
landed instruments:

* the identity set comes from ``declare_candidates._residue_needs_declare``
  and ``_tag``, so a row is in scope because the landed pricer put it in
  ``DECLARE_AND_WITNESS``, not because a list here says so;
* every axis is measured by ``incomplete_row_guard_check._row``, imported
  unchanged, because the two instruments disagreeing about a row would
  otherwise look like a finding about the row;
* the runner-surface column is ``seam_surface_match``'s own AST read (verbs
  the package calls against the callables the shared fake implements), which
  is the axis that blocked tmx's ``request`` in wave 24.

Why the last one is printed per provider and not assumed: yfinance's family
screen reads ``seam_verbs=[]``, i.e. the package never calls the shared
client, so a row that passes all four axes here is still only witnessable
through the engine ``transport`` leg
(``opendata/data/protocol.py:312-317``) -- the mechanism ecb, tiingo and
econdb rows were accepted on.

Arms, all seven of which can fail:

``A_landed_positive``
    the two rows wave 25 landed still read a document-trippable verdict and
    ``OFFLINE_ACCEPTED``. A changed reading here means the imported measurer
    moved, not that this block is different.
``B_landed_negative``
    ``government_us::WeatherBulletinDownload`` still reads a non-trippable
    verdict.
``C_row_class_arm_can_differ``
    the fourth axis is live on this run: ``government_us::CommodityPsdData``
    must read ``is_contract=True`` while ``federal_reserve::YieldCurve`` --
    whose emitted class shares all three contract fields -- must read
    ``is_contract=False``. An identity test that cannot tell those apart is
    what mis-priced the federal_reserve pair as convertible in draft 2.
``D_population_reconciles``
    the per-provider bucket counts recomputed here must equal the
    ``declare_face_by_provider`` line the landed carrier quotes from the
    shipped pricer's own stdout. This is the arm that stops a hand-picked
    subset from being called "the block".
``E_seam_read_can_differ``
    ``tmx`` must measure a verb the get-only fake lacks while ``yfinance``
    measures none at all, so the seam column is a measurement, not a constant.
``F_verdict_enum``
    every verdict printed for the block is one of the six shipped values, and
    the face line reports the convertible rows against the judge's statuses.
``G_ledger_cause_three_sided``
    the second axis must be able to show three different readings in one run:
    a ledger that carries the identity (``cboe::FuturesCurve`` ->
    ``(True, False)``), a ledger present but silent about it
    (``federal_reserve::YieldCurve`` -> ``(False, False)``), and a package
    with no ledger module at all (the head of this block -> ``(False,
    True)``). Before wave 26's third defect was split the first and third
    read identically as ``ruled_out=True``, which is what made 26 yfinance
    rows look pre-ruled-out. Running ``--provider`` for a package that *does*
    ship ``specs.py`` can therefore only produce two of the three readings,
    and this arm goes red -- that is the intended reading, not a bug in it.

Exits non-zero when an arm fails. A constant verdict across the block prints
``movable=False`` and is a legitimate finding about the population -- it is
not treated as a gate failure, because the gate here is whether the
*instruments* can still differ.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import declare_candidates as dc  # the shipped bucket rule and population
import incomplete_row_guard_check as axes  # the shipped four-axis row measurer
import seam_surface_match as seam  # the shipped report reader and runner-surface AST read

CARRIER = Path(__file__).resolve().parent.parent / "runs/wave26-step0-second-axis.txt"
FACE_PREFIX = re.compile(r"^\s*\d+\|\s*")
TRIPPABLE = axes.TRIPPABLE
NEGATIVE_CONTROL = axes.CONTROL_NEGATIVE
POSITIVE_CONTROLS = axes.CONTROLS_POSITIVE
CLASS_CONTRAST = ("government_us::CommodityPsdData", "federal_reserve::YieldCurve")
SEAM_CONTRAST = ("tmx", "yfinance")
LEDGER_CONTRAST = ("cboe::FuturesCurve", "federal_reserve::YieldCurve")


def _judged(report: dict[str, Any]) -> dict[str, str]:
    return {str(row["model_identity"]): str(row["status"]) for row in report["models"]}


def _block(provider: str, tag: str) -> tuple[list[dict[str, Any]], dict[str, Counter]]:
    """Priced rows of one provider in one tag bucket, plus the whole by-provider tag census."""
    report = seam._load_report()
    priced = [dc._price(identity) for identity in dc._residue_needs_declare(report)]
    by_provider: dict[str, Counter] = defaultdict(Counter)
    for row in priced:
        by_provider[row["identity"].partition("::")[0]][dc._tag(row)] += 1
    picked = [
        row
        for row in priced
        if row["identity"].partition("::")[0] == provider and dc._tag(row) == tag
    ]
    return sorted(picked, key=lambda item: str(item["identity"])), by_provider


def _carrier_face() -> dict[str, dict[str, int]]:
    """The ``declare_face_by_provider`` JSON the landed carrier quotes from the pricer's stdout."""
    for line in CARRIER.read_text(encoding="utf-8").splitlines():
        if "declare_face_by_provider=" in line:
            payload = FACE_PREFIX.sub("", line).split("declare_face_by_provider=", 1)[1]
            face: dict[str, dict[str, int]] = json.loads(payload)
            return face
    raise AssertionError(f"declare_face_by_provider line not found in {CARRIER}")


def _runner_surface(provider: str) -> dict[str, Any]:
    verbs = seam._transport_verbs(provider)
    surface = seam._fake_surface()
    return {
        "seam_verbs": sorted(verbs),
        "fake_callables": sorted(surface),
        "served": sorted(verbs & surface),
        "blocked": sorted(verbs - surface),
        "reachable": bool(verbs & surface),
    }


def main(argv: list[str] | None = None) -> int:
    """Print the block's four axes, then gate the instruments on their landed controls."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--provider", default="yfinance")
    parser.add_argument("--tag", default="DECLARE_AND_WITNESS")
    args = parser.parse_args(argv)

    report = seam._load_report()
    judged = _judged(report)
    picked, census = _block(args.provider, args.tag)
    surface = _runner_surface(args.provider)
    print(
        f"runner_surface {args.provider} seam_verbs={surface['seam_verbs']} "
        f"served={surface['served']} blocked={surface['blocked']} "
        f"seam_reachable={surface['reachable']}"
    )

    screen = axes._provider_screen(args.provider)
    rows: list[dict[str, Any]] = []
    for priced in picked:
        identity = str(priced["identity"])
        row = axes._row(identity, judged.get(identity, "?"))
        row["pricer_tag"] = dc._tag(priced)
        row["pricer_contract_in_module"] = priced.get("contract_in_module")
        row["pricer_emits"] = priced.get("emits")
        rows.append(row)
        axes._print_row(row, screen)
        print(
            f"    pricer {identity} tag={row['pricer_tag']} "
            f"contract_in_module={row['pricer_contract_in_module']} emits={row['pricer_emits']} "
            f"judge_status={row['status']}"
        )

    verdicts = Counter(str(row["verdict"]) for row in rows)
    convertible = [str(row["identity"]) for row in rows if str(row["verdict"]) in TRIPPABLE]

    positives = [axes._row(i, judged.get(i, "?")) for i in POSITIVE_CONTROLS]
    arm_a = all(
        r["verdict"] in TRIPPABLE and judged.get(str(r["identity"])) == "OFFLINE_ACCEPTED"
        for r in positives
    )
    for control in positives:
        print(
            f"A_landed_positive {control['identity']} verdict={control['verdict']} "
            f"judge_status={judged.get(str(control['identity']))} "
            f"PASS={control['verdict'] in TRIPPABLE}"
        )
    print(f"A_landed_positive all_pass={arm_a} PASS={arm_a}")

    negative = axes._row(NEGATIVE_CONTROL, judged.get(NEGATIVE_CONTROL, "?"))
    arm_b = negative["verdict"] not in TRIPPABLE
    print(
        f"B_landed_negative {NEGATIVE_CONTROL} verdict={negative['verdict']} "
        f"raises={negative.get('raises')} code_raises={negative.get('code_raises')} PASS={arm_b}"
    )

    contrast: dict[str, bool] = {}
    for identity in CLASS_CONTRAST:
        measured = axes._row(identity, judged.get(identity, "?"))
        contrast[identity] = bool(measured.get("is_contract"))
        print(
            f"C_row_class {identity} contract={measured.get('contract')} "
            f"is_contract={measured.get('is_contract')} look_alike={measured.get('look_alike')} "
            f"emitted={measured.get('emitted')}"
        )
    arm_c = contrast[CLASS_CONTRAST[0]] is True and contrast[CLASS_CONTRAST[1]] is False
    print(f"C_row_class_arm_can_differ reads={contrast} PASS={arm_c}")

    quoted = _carrier_face()
    recomputed = {p: dict(c) for p, c in sorted(census.items())}
    drift = [
        f"{p}:{k} carrier={quoted[p].get(k, 0)} here={c.get(k, 0)}"
        for p, c in recomputed.items()
        for k in set(c) | set(quoted.get(p, {}))
        if quoted.get(p, {}).get(k, 0) != c.get(k, 0)
    ]
    arm_d = not drift and quoted.get(args.provider, {}).get(args.tag, 0) == len(picked)
    print(
        f"D_population_reconciles provider={args.provider} tag={args.tag} here={len(picked)} "
        f"carrier={quoted.get(args.provider, {}).get(args.tag, 0)} providers={len(recomputed)} "
        f"divergent={len(drift)} drift={drift} PASS={arm_d}"
    )

    seam_reads = {p: _runner_surface(p) for p in SEAM_CONTRAST}
    arm_e = (
        bool(seam_reads[SEAM_CONTRAST[0]]["blocked"])
        and not seam_reads[SEAM_CONTRAST[1]]["seam_verbs"]
    )
    for name, face in seam_reads.items():
        print(
            f"E_seam {name} seam_verbs={face['seam_verbs']} served={face['served']} "
            f"blocked={face['blocked']}"
        )
    print(f"E_seam_read_can_differ PASS={arm_e}")

    off_enum = sorted(set(verdicts) - set(axes.VERDICTS))
    arm_f = not off_enum
    print(f"F_verdict_enum off_enum={off_enum} PASS={arm_f}")

    ledger_reads: list[tuple[str, dict[str, Any]]] = [
        (identity, axes._row(identity, judged.get(identity, "?"))) for identity in LEDGER_CONTRAST
    ]
    if rows:
        ledger_reads.append((str(rows[0]["identity"]), rows[0]))
    causes = {
        label: (bool(face["ruled_out"]), bool(face.get("no_ledger")))
        for label, face in ledger_reads
    }
    for label, face in ledger_reads:
        print(
            f"G_ledger_cause {label} ruled_out={bool(face['ruled_out'])} "
            f"no_ledger={face.get('no_ledger') or '-'} detail={face['ruled_out'] or '-'}"
        )
    arm_g = len(causes) == 3 and len(set(causes.values())) == 3
    print(f"G_ledger_cause_three_sided reads={causes} PASS={arm_g}")

    arms = [arm_a, arm_b, arm_c, arm_d, arm_e, arm_f, arm_g]
    passed = sum(1 for ok in arms if ok)
    print(
        f"declare_block_face provider={args.provider} tag={args.tag} rows={len(rows)} "
        f"verdicts={dict(sorted(verdicts.items()))} convertible={len(convertible)} "
        f"convertible_rows={convertible} movable={len(verdicts) > 1} "
        f"seam_reachable={surface['reachable']} "
        f"arms={len(arms)} arms_passed={passed} arms_failed={len(arms) - passed} "
        f"identity_set={str(report['inventory']['expected_identity_set_sha256'])[:16]}"
    )
    return 0 if passed == len(arms) else 1


if __name__ == "__main__":
    raise SystemExit(main())
