"""Price the ``SEAM_REACHABLE`` residue per row -- the batch that decides wave 27 step 2.

Section 90.7 armed a fork. Path 甲 edits ``scripts/quality/iter2_claims_probe_runner.py`` (a third
seam kind that can swap a positional module callable) and thereby stales the 1330 receipts pinned to
that file's bytes. Path 乙 is the seven rows the shipped reachability census puts in
``bucket=SEAM_REACHABLE mechanism=client_seam``, which need no runner edit at all. Neither half of
that fork was measured *per row*: the census aggregates by provider (``rows=/accepted=/residual=``),
and the four-axis probe that does measure per row is scoped to the ``INCOMPLETE`` statuses it was
written for. This probe points the shipped axis measurer at the census's own population.

No bucket rule is re-typed here. The provider-to-bucket assignment is read off a subprocess run
of ``seam_reachability_census.py`` -- its stdout is this probe's denominator -- and every per-row
column comes from ``incomplete_row_guard_check._row``, imported unchanged, for the same reason as in
``declare_block_axes.py``: two instruments disagreeing about a row would read as a finding about the
row rather than about the instrument.

Arms, all seven of which can fail:

``A_landed_positive``
    the two rows wave 25 landed (``axes.CONTROLS_POSITIVE``) must still read a document-trippable
    verdict. Dead here means the imported measurer moved, not that this batch is different.
``B_landed_negative``
    ``government_us::WeatherBulletinDownload`` must still read a non-trippable verdict, so the
    positive controls are not the only thing the tool can print.
``C_population_reconciles``
    ``population`` (selected row by row from the judged report) must equal the sum of the
    ``residual=`` fields the census printed for its ``SEAM_REACHABLE`` providers. One number comes
    from statuses, the other from the census's aggregation, so a silent narrowing of either side is
    a red rather than a smaller batch.
``D_selector_can_differ``
    the identical selector run over ``BLOCKED_ON_RUNNER_SURFACE`` providers (tmx holds 24) must come
    back non-empty and disjoint from the population. A selector that returns everything cannot be
    said to have selected 7.
``E_module_shape``
    every provider holding a population row must report ``witness_module=True`` on the census's own
    line, because "add one CONFIG entry to an existing shard" and "write a first module" are
    different prices (eia 168 lines/2 rows, government_us 183). Any False is named.
``F_verdict_enum``
    every verdict is one of the six shipped values; a new value means the imported classifier moved.
``G_two_instruments_agree``
    for each population row, ``declared``/``bound`` as the axis measurer reads them must agree with
    membership in ``declare_candidates._residue_needs_declare``, whose rule is ``bound and not
    declared``. Wave 27 step 1 moved 15 yfinance rows by writing ``row_model`` lines, so a row the
    two instruments price differently is the shape that would silently re-bucket a batch.

``movable`` follows the sibling probes: a constant verdict over the batch is a legitimate finding
about the population, so the gate is whether the instruments can still differ (arms A/B/D), not
whether this batch is heterogeneous. Exits non-zero when an arm fails.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import declare_candidates as dc  # the shipped NEED_DECLARE population and bucket rule
import incomplete_row_guard_check as axes  # the shipped four-axis row measurer
import seam_reachability_census as census  # repo root, carried for the census subprocess cwd
import seam_surface_match as seam  # the landed judged report

REACHABILITY_PROBE = Path(__file__).resolve().parent / "seam_reachability_census.py"
LINE = "seam_census_provider="


def _reachability_lines() -> list[dict[str, str]]:
    """Run the shipped census and parse its provider lines, so its numbers are this denominator."""
    run = subprocess.run(  # nosec B603  # noqa: S603
        [sys.executable, str(REACHABILITY_PROBE)],
        cwd=census.seam.REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        raise SystemExit(f"reachability census exited {run.returncode}: {run.stderr.strip()[:200]}")
    parsed = [
        dict(token.split("=", 1) for token in re.split(r"\s+(?=\w+=)", line) if "=" in token)
        for line in run.stdout.splitlines()
        if line.startswith(LINE)
    ]
    if not parsed:
        raise SystemExit("census printed no provider line -- the denominator is unreadable")
    return parsed


def _residual(lines: list[dict[str, str]], bucket: str) -> dict[str, int]:
    """One bucket's provider-to-residual map, exactly as the census printed it."""
    return {p["seam_census_provider"]: int(p["residual"]) for p in lines if p["bucket"] == bucket}


def _population(rows: list[dict[str, Any]], wanted: set[str]) -> list[tuple[str, str]]:
    """Every non-``OFFLINE_ACCEPTED`` identity whose provider sits in ``wanted``."""
    return sorted(
        (str(row["model_identity"]), str(row["status"]))
        for row in rows
        if str(row["model_identity"]).partition("::")[0] in wanted
        and str(row["status"]) != "OFFLINE_ACCEPTED"
    )


def main() -> int:
    """Print the four axes per ``SEAM_REACHABLE`` row, gated on arms that can each go red."""
    lines = _reachability_lines()
    report = seam._load_report()
    judged = {str(row["model_identity"]): str(row["status"]) for row in report["models"]}
    identity_set = str(report["inventory"]["expected_identity_set_sha256"])[:16]

    reachable = _residual(lines, "SEAM_REACHABLE")
    blocked = _residual(lines, "BLOCKED_ON_RUNNER_SURFACE")
    witness_missing = sorted(
        p["seam_census_provider"]
        for p in lines
        if p["bucket"] == "SEAM_REACHABLE" and p["witness_module"] != "True"
    )

    population = _population(report["models"], set(reachable))
    identities = [identity for identity, _ in population]
    control_ids = [identity for identity, _ in _population(report["models"], set(blocked))]
    overlap = sorted(set(control_ids) & set(identities))
    print(
        f"0_census_denominator reachable_providers={len(reachable)} "
        f"census_residual={sum(reachable.values())} blocked_providers={len(blocked)} "
        f"blocked_rows={len(control_ids)} overlap={len(overlap)}"
    )

    measured = [axes._row(identity, status) for identity, status in population]
    for row in measured:
        provider = str(row["identity"]).partition("::")[0]
        print(
            f"row {row['identity']} status={row['status']} bound={row['bound']} "
            f"ruled_out={bool(row['ruled_out'])} no_ledger={row.get('no_ledger') or '-'} "
            f"declared={row.get('declared', False)} witness_entry={row['witness_entry']} "
            f"verdict={row['verdict']} fetcher={row.get('fetcher', '-')} "
            f"raises={row.get('raises', 0)} code_raises={row.get('code_raises', 0)} "
            f"witness_module={provider not in witness_missing}"
        )

    verdicts: Counter[str] = Counter(str(row["verdict"]) for row in measured)
    convertible = [str(row["identity"]) for row in measured if row["verdict"] in axes.TRIPPABLE]
    witness_only = [
        str(row["identity"])
        for row in measured
        if row["verdict"] in axes.TRIPPABLE and row.get("declared") and not row["witness_entry"]
    ]
    by_provider: dict[str, int] = defaultdict(int)
    for identity in identities:
        by_provider[identity.partition("::")[0]] += 1

    needs_declare = set(dc._residue_needs_declare(report))
    inside = sorted(needs_declare & set(identities))
    inconsistent = sorted(
        str(row["identity"])
        for row in measured
        if (str(row["identity"]) in needs_declare)
        != (bool(row["bound"]) and not bool(row.get("declared")))
    )
    print(
        f"1_declaration_axis population={len(identities)} inside_NEED_DECLARE={len(inside)} "
        f"ids={inside} outside={len(identities) - len(inside)} contradictions={len(inconsistent)}"
    )

    positive = [axes._row(c, judged.get(c, "?")) for c in axes.CONTROLS_POSITIVE]
    for control in positive:
        print(
            f"A_landed_positive {control['identity']} status={control['status']} "
            f"verdict={control['verdict']} PASS={control['verdict'] in axes.TRIPPABLE}"
        )
    arm_a = all(r["verdict"] in axes.TRIPPABLE for r in positive) and len(positive) == 2

    negative = axes._row(axes.CONTROL_NEGATIVE, judged.get(axes.CONTROL_NEGATIVE, "?"))
    arm_b = negative["verdict"] not in axes.TRIPPABLE
    print(
        f"B_landed_negative {axes.CONTROL_NEGATIVE} status={negative['status']} "
        f"verdict={negative['verdict']} PASS={arm_b}"
    )

    arm_c = len(population) == sum(reachable.values())
    print(
        f"C_population_reconciles rows={len(population)} census_residual="
        f"{sum(reachable.values())} PASS={arm_c}"
    )

    arm_d = bool(control_ids) and not overlap
    print(
        f"D_selector_can_differ blocked_rows={len(control_ids)} blocked_sample="
        f"{sorted(blocked)[:3]} overlap={len(overlap)} PASS={arm_d}"
    )

    arm_e = not witness_missing
    print(
        f"E_module_shape witness_module_missing={len(witness_missing)} "
        f"providers={witness_missing} PASS={arm_e}"
    )

    off_enum = sorted(set(verdicts) - set(axes.VERDICTS))
    arm_f = not off_enum
    print(f"F_verdict_enum distinct={len(verdicts)} violations={off_enum} PASS={arm_f}")

    arm_g = not inconsistent
    print(
        f"G_two_instruments_agree contradictions={len(inconsistent)} "
        f"ids={inconsistent[:3]} PASS={arm_g}"
    )

    arms = (arm_a, arm_b, arm_c, arm_d, arm_e, arm_f, arm_g)
    print(
        f"seam_reachable_axes_face population={len(population)} "
        f"by_provider={dict(sorted(by_provider.items()))} "
        f"verdicts={dict(sorted(verdicts.items()))} "
        f"convertible={len(convertible)} convertible_rows={convertible} "
        f"witness_only={len(witness_only)} witness_only_rows={witness_only} "
        f"movable={len(verdicts) > 1} arms={len(arms)} arms_passed={sum(arms)} "
        f"arms_failed={len(arms) - sum(arms)} identity_set={identity_set}"
    )
    return 0 if all(arms) else 1


if __name__ == "__main__":
    raise SystemExit(main())
