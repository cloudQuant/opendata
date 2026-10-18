"""How far does the split ``ruled_out`` / ``no_ledger`` axis reach beyond the wave-27 block?

Wave 26's third defect was that the ledger axis returned *one* string, so "the ledger carries
this identity" and "this package ships no ledger module" both read ``ruled_out=True``. The run
that caught it was ``declare_block_axes.py --provider yfinance``, i.e. 26 rows of one provider.
That leaves the size of the blast radius unmeasured, and the size is what decides whether the
split was a curiosity or the thing that had been vetoing batches.

This probe points the *shipped* row measurer (``incomplete_row_guard_check._row``, imported
unchanged) at the whole residue population -- every identity in the landed judge report whose
status is not ``OFFLINE_ACCEPTED`` -- and tallies the two ledger causes per provider. The
provider-package census comes from the filesystem (``opendata/data/providers/*/specs.py``),
which is an independent measurement of the same fact the import inside ``_ruled_out`` reads. The
two populations are not identical: the report can name a provider that has no package directory,
and the first run of this probe found exactly that (two rows), so the arm below checks the
agreement per provider that exists as a package and handles the rest as its own reading.

Arms, all five of which can fail:

``A_landed_report_face``
    the judge report still reads 350 identities / ``{INCOMPLETE: 8, OFFLINE_ACCEPTED: 281,
    UNREVIEWED: 61}`` / ``identity_set[0:16]=5168e1e723999312``. Landed in
    ``runs/wave27-step0-block-axes.txt`` as ``{...266...77}`` and re-based three times since: to
    ``{7, 275, 68}`` by step 3, to ``{6, 276, 68}`` by step 4's ``federal_reserve::YieldCurve``, and
    to this face by wave 29's deribit 5 rows. The previous value and the measured per-status delta
    are printed on ``A_rebase_from_landed`` so the re-base is checkable rather than remembered. Any
    movement reddens this arm and invalidates every number below -- that is the invalidation
    condition, not a warning.
``B_three_causes_reachable``
    the three ledger readings ``(True, False)``, ``(False, False)``, ``(False, True)`` all occur
    in this population. If one is empty the tally would be made of one cause seen twice.
``C_conflation_identity``
    the pre-split predicate read ``bool(ruled_out or no_ledger)``, so the number it would have
    reported as "ruled out" must equal ``ledger_refusal + no_ledger_only`` exactly. If it does
    not, this probe has mis-stated the old rule rather than measured it; if the difference is
    zero the split changed no reading and the wave-27 block was the whole story.
``D_cause_matches_disk``
    two parts, because the first run's red said the populations differ. ``D1``: for every
    residue provider that *is* a package directory, either every one of its rows reads
    ``NO_SPECS_MODULE`` (no ``specs.py``) or none does (it ships one) -- the ledger cause is a
    property of the package, not of the row. ``D2``: the residue contains provider names that
    are not package directories at all, and every such row must read ``bound=False``; the arm
    also requires at least one on-package provider with a bound row, so D2 cannot be green by
    being vacuous. A name outside ``opendata/data/providers`` has no ledger to consult, which is
    why its ``NO_SPECS_MODULE`` reading is not the same fact as "package without specs.py".
``E_pricer_block_inside``
    the 26 rows that ``declare_block_axes.py`` measured as ``DECLARE_AND_WITNESS``/yfinance are
    all in this population and all read ``(False, True)`` here -- the two instruments must agree
    row by row about the block they both claim to have measured.

Exits non-zero when an arm fails. Per-row verdicts are not printed: only the second axis is in
scope here, and a verdict face printed next to a different instrument's verdict face is how two
generations get confused for one.
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import declare_candidates as dc  # the shipped bucket rule and population
import incomplete_row_guard_check as axes  # the shipped four-axis row measurer
import seam_surface_match as seam  # the shipped report reader

LANDED_TOTAL = 350
LANDED_STATUSES = {"INCOMPLETE": 8, "OFFLINE_ACCEPTED": 281, "UNREVIEWED": 61}
# The generation this probe asserted before wave-29 landed deribit's 5 rows. Kept so a re-base
# prints a delta instead of silently replacing the number step 4 quoted: the 5 deribit rows left
# UNREVIEWED, and government_us::TreasuryPrices/WeatherBulletinDownload left UNREVIEWED too but
# landed in INCOMPLETE, because the re-produce recorded five BLOCKED claims for each where the
# committed shard had none at all.
LANDED_STATUSES_PREVIOUS = {"INCOMPLETE": 6, "OFFLINE_ACCEPTED": 276, "UNREVIEWED": 68}
LANDED_IDENTITY_SET = "5168e1e723999312"
LANDED_BLOCK = ("yfinance", "DECLARE_AND_WITNESS")
PROVIDERS_DIR = seam.REPO_ROOT / "opendata" / "data" / "providers"
EXCLUDED_DIRS = {"__pycache__"}


def _statuses(report: dict[str, Any]) -> dict[str, str]:
    return {str(row["model_identity"]): str(row["status"]) for row in report["models"]}


def _specs_census() -> dict[str, bool]:
    """specs.py present or absent, read off the filesystem rather than by importing."""
    census: dict[str, bool] = {}
    for child in sorted(PROVIDERS_DIR.iterdir()):
        if not child.is_dir() or child.name in EXCLUDED_DIRS or child.name.startswith("_"):
            continue
        census[child.name] = (child / "specs.py").is_file()
    return census


def _cause(row: dict[str, Any]) -> tuple[bool, bool]:
    return (bool(row["ruled_out"]), bool(row.get("no_ledger")))


def main() -> int:
    """Tally the two ledger causes over the whole residue, then gate the instruments."""
    report = seam._load_report()
    judged = _statuses(report)
    statuses = Counter(judged.values())
    identity_set = str(report["inventory"]["expected_identity_set_sha256"])[:16]
    arm_a = len(judged) == LANDED_TOTAL and dict(statuses) == LANDED_STATUSES
    arm_a = arm_a and identity_set == LANDED_IDENTITY_SET
    rebase_delta = {
        key: int(statuses.get(key, 0)) - int(LANDED_STATUSES_PREVIOUS.get(key, 0))
        for key in set(statuses) | set(LANDED_STATUSES_PREVIOUS)
    }
    print(
        f"A_landed_report_face total={len(judged)} statuses={dict(sorted(statuses.items()))} "
        f"identity_set={identity_set} landed={LANDED_TOTAL} "
        f"{LANDED_STATUSES} {LANDED_IDENTITY_SET} PASS={arm_a}"
    )
    print(
        f"A_rebase_from_landed previous={dict(sorted(LANDED_STATUSES_PREVIOUS.items()))} "
        f"rebase_delta={dict(sorted(rebase_delta.items()))} "
        f"delta_sums_to_zero={sum(rebase_delta.values()) == 0} "
        f"identity_set_moved={identity_set != LANDED_IDENTITY_SET}"
    )

    residue = sorted(
        identity for identity, status in judged.items() if status != "OFFLINE_ACCEPTED"
    )
    rows = [axes._row(identity, judged[identity]) for identity in residue]
    causes = Counter(_cause(row) for row in rows)
    by_provider: dict[str, Counter] = defaultdict(Counter)
    bound_by_provider: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        provider = str(row["identity"]).partition("::")[0]
        by_provider[provider][_cause(row)] += 1
        bound_by_provider[provider][bool(row["bound"])] += 1

    row_statuses = dict(sorted(Counter(str(row["status"]) for row in rows).items()))
    print(f"residue_population={len(rows)} statuses={row_statuses}")
    for provider in sorted(by_provider):
        tally = by_provider[provider]
        bound = bound_by_provider[provider]
        print(
            f"  provider={provider} rows={sum(tally.values())} bound_true={bound[True]} "
            f"ledger_refusal={tally[(True, False)]} ledger_silent={tally[(False, False)]} "
            f"no_ledger_module={tally[(False, True)]} both={tally[(True, True)]}"
        )

    arm_b = all(causes[state] for state in ((True, False), (False, False), (False, True)))
    print(f"B_three_causes_reachable reads={dict(sorted(causes.items()))} PASS={arm_b}")

    new_true = causes[(True, False)]
    no_ledger_only = causes[(False, True)]
    old_true = sum(1 for row in rows if bool(row["ruled_out"]) or bool(row.get("no_ledger")))
    arm_c = old_true == new_true + no_ledger_only and no_ledger_only > 0
    print(
        f"C_conflation_identity old_predicate_ruled_out={old_true} new_ruled_out={new_true} "
        f"no_ledger={no_ledger_only} cause_3_both_set={causes[(True, True)]} "
        f"overstated_by={old_true - new_true} PASS={arm_c}"
    )

    census = _specs_census()
    disk_missing = {name for name, present in census.items() if not present}
    providers_with_rows = set(by_provider)
    off_census = providers_with_rows - set(census)
    on_census = providers_with_rows & set(census)
    read_missing = {
        provider
        for provider, tally in by_provider.items()
        if tally[(False, True)] or tally[(True, True)]
    }
    arm_d1 = all(
        (
            by_provider[provider][(False, True)] + by_provider[provider][(True, True)]
            == sum(by_provider[provider].values())
        )
        == (not census[provider])
        for provider in on_census
    )
    off_census_bound = sum(bound_by_provider[provider][True] for provider in off_census)
    arm_d2 = (
        bool(off_census)
        and off_census_bound == 0
        and any(bound_by_provider[provider][True] for provider in on_census)
    )
    arm_d = arm_d1 and arm_d2
    print(
        f"D_cause_matches_disk providers={len(census)} specs_absent={len(disk_missing)} "
        f"sorted_absent={sorted(disk_missing)} read_absent={sorted(read_missing)} "
        f"providers_with_residue_rows={len(providers_with_rows)} "
        f"off_census={sorted(off_census)} off_census_bound_rows={off_census_bound} "
        f"uniform_per_package={arm_d1} package_names_are_bounded={arm_d2} PASS={arm_d}"
    )

    priced = [dc._price(identity) for identity in dc._residue_needs_declare(report)]
    block = [
        str(row["identity"])
        for row in priced
        if row["identity"].partition("::")[0] == LANDED_BLOCK[0] and dc._tag(row) == LANDED_BLOCK[1]
    ]
    cause_of = {str(row["identity"]): _cause(row) for row in rows}
    bound_of = {str(row["identity"]): bool(row["bound"]) for row in rows}
    arm_e = bool(block) and all(cause_of.get(identity) == (False, True) for identity in block)
    arm_e = arm_e and set(block) <= set(residue)
    print(
        f"E_pricer_block_inside provider={LANDED_BLOCK[0]} tag={LANDED_BLOCK[1]} "
        f"block={len(block)} in_residue={len(set(block) & set(residue))} "
        f"block_bound_true={sum(1 for i in block if bound_of[i])} "
        f"all_no_ledger_module={all(cause_of.get(i) == (False, True) for i in block)} PASS={arm_e}"
    )

    no_ledger_bound = sum(1 for row in rows if _cause(row) == (False, True) and row["bound"])
    arms = [arm_a, arm_b, arm_c, arm_d, arm_e]
    passed = sum(1 for ok in arms if ok)
    print(
        f"ledger_blast_face residue={len(rows)} bound_true={sum(1 for r in rows if r['bound'])} "
        f"ledger_refusal={new_true} ledger_silent={causes[(False, False)]} "
        f"no_ledger_module={no_ledger_only} no_ledger_module_and_bound={no_ledger_bound} "
        f"pre_split_overstatement={old_true - new_true} arms={len(arms)} "
        f"arms_passed={passed} arms_failed={len(arms) - passed} "
        f"identity_set={identity_set}"
    )
    return 0 if passed == len(arms) else 1


if __name__ == "__main__":
    raise SystemExit(main())
