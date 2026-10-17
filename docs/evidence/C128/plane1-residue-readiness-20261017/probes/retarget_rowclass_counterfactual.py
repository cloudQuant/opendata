"""Price the ``retarget_fetcher_output`` batch by opening exactly one gate in memory.

§92 set this step's predicate. The 24 rows the landed census calls
``retarget_fetcher_output`` all read ``verdict=ROW_CLASS_NOT_CONTRACT``, which
the shipped rule produces in exactly one way:
``incomplete_row_guard_check._verdict`` returns it when *both* ``declared`` and
``is_contract`` are False. So the counterfactual is the removal of that one
guard -- re-feed the row's own measured face with ``declared=True``, and
separately with ``is_contract=True``, and call the shipped ``_verdict`` again.
Every other column (``counts``, ``trippable``, ``raises``) stays the disk
measurement, untouched, so whatever the flipped verdict reads it is taken from
the serving path rather than asserted.

The two routes are the two candidate fixes and they are not the same edit:
``declared=True`` means putting ``row_model`` on the fetcher, while
``is_contract=True`` means changing which class the fetcher constructs. The
guard is a conjunction of two negations, so the two routes must produce the
*same* verdict for every row -- that invariance is arm C, and it is a claim
about the shipped rule, not about the repo.

Controls are deliberately not drawn from the quantity they judge, which is the
lesson of wave 28 step 0 (arm F there compared two counters incremented in the
same loop body):

* B re-feeds the unmodified face and requires the landed verdict back, so a
  copy that silently dropped a key is caught before any counterfactual is read;
* C requires the two routes to agree, row by row;
* D requires every target to move *off* ``ROW_CLASS_NOT_CONTRACT`` -- the
  arithmetic of the guard says all of them must, and a flip that ignored the
  flags would read 0;
* E applies the same flip to rows that already clear the gate (the two landed
  PSD controls and the wave-25 negative control, measured fresh) and requires a
  no-op there, so a flip that smeared every row to one label is caught. An
  empty denominator there is itself the failure: the arm would be vacuous.
* G recomputes "needs only a shard entry now" by a second route -- from the
  flipped verdict plus the census's own ``fake_answers_all`` field instead of
  from ``_action``'s return -- and requires the two to agree;
* H re-derives the ``is_contract`` bit from a second route (resolve
  ``contract_model(domain)`` again and test whether its module-qualified name is
  in the face's own ``emitted`` list) and requires it to match the shipped flag
  for every row, and requires that a row whose contract class will not resolve
  is one the face already marks ``no_contract``. The shape tally it prints --
  how many of these rows construct *another* contract class versus *no* contract
  class at all -- is data, not a gate: an earlier draft of this arm gated on
  "``emitted`` must be non-empty" and went red on 22 of the 24 rows for the
  wrong reason;
* I forges that very axis on one row in memory and requires H's test to catch
  it, because H has never been observed to fire on the real population -- an
  arm that has only ever read green is not yet known to be capable of red.

Nothing is edited: no producer, no judge, no claims file. The population comes
from the same ``readiness_census._residue`` the landed census used, and arm A
compares the identity set against the carrier
``runs/wave28-step0-residue-axis-census.txt``, so a repo that drifted between
the two generations shows up as a red arm instead of as a silently different
denominator.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROBE_DIR))

import incomplete_row_guard_check as axes  # noqa: E402  the shipped four-axis row measurer
import readiness_census as rc  # noqa: E402  the shipped residue population + bucket rule
import residue_axis_census as census  # noqa: E402  the shipped action classifier + seam map
import seam_surface_match as seam  # noqa: E402  the landed judged report

CARRIER = PROBE_DIR.parent / "runs" / "wave28-step0-residue-axis-census.txt"
GUARD_VERDICT = "ROW_CLASS_NOT_CONTRACT"
RETARGET = "retarget_fetcher_output"
ABSENT = "ABSENT_FROM_CENSUS"
NO_PACKAGE = "NO_PACKAGE_ON_DISK"


def _flip(measured: dict[str, Any], key: str) -> str:
    """Return the shipped verdict with one gate key opened; other columns untouched."""
    copy = dict(measured)
    copy[key] = True
    return str(axes._verdict(copy))


def _carrier_retarget() -> tuple[set[str], int]:
    """Read the landed carrier: the identities it priced as retarget, and its counter."""
    text = CARRIER.read_text(encoding="utf-8")
    pattern = rf"^row (\S+).*\baction={RETARGET}$"
    identities = {m.group(1) for m in re.finditer(pattern, text, re.MULTILINE)}
    hit = re.search(r"actions=\{([^}]*)\}", text)
    if hit is None:
        raise SystemExit(f"no actions tally on {CARRIER.name}")
    pairs = dict(re.findall(r"'([a-z_]+)': (\d+)", hit.group(1)))
    return identities, int(pairs.get(RETARGET, -1))


def _fetcher_file(identity: str) -> str:
    """Where the identity edit would have to happen, resolved from the bound fetcher."""
    provider, _, model = identity.partition("::")
    fetcher, _ = axes._fetcher(provider, model)
    if fetcher is None:
        return "UNBOUND"
    klass = type(fetcher)
    module = sys.modules.get(str(klass.__module__))
    path = inspect.getsourcefile(klass)
    if module is None or path is None:
        return "NO_SOURCEFILE"
    rel = str(Path(path).resolve().relative_to(seam.REPO_ROOT))
    tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)
    owners = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == klass.__name__]
    if not owners:
        return f"{rel}:?<no ClassDef {klass.__name__}>"
    return f"{rel}:{owners[0].lineno}"


def _contract_fqn(measured: dict[str, Any]) -> str:
    """Re-resolve the domain's contract class by the shipped route and name it module-qualified.

    ``incomplete_row_guard_check._row`` stores only ``contract`` (the bare class
    name) and the ``no_contract`` flag; ``emitted`` entries are
    ``module.qualname``. Resolving the class again here is what makes arm H a
    second route to ``is_contract`` instead of a restatement of it.
    """
    domain = measured.get("domain")
    if not domain:
        return ""
    try:
        klass = axes.contract_model(str(domain))
    except LookupError:
        return ""
    return f"{klass.__module__}.{klass.__qualname__}"


def _is_contract_inconsistent(row: dict[str, Any]) -> bool:
    """True when the shipped ``is_contract`` bit disagrees with the second route to it."""
    emitted = list(row["measured"].get("emitted") or [])
    derived = bool(row["contract_fqn"]) and row["contract_fqn"] in emitted
    return derived != bool(row["measured"].get("is_contract"))


def main() -> int:
    """Print the flipped face row by row, then the nine arms and the summary face."""
    report = seam._load_report()
    judged = {str(row["model_identity"]): str(row["status"]) for row in report["models"]}
    residue = rc._residue(report)
    seam_map = census._seam_map(census._run(census.CENSUS_PROBE, census.CENSUS_FIELDS))

    rows: dict[str, dict[str, Any]] = {}
    for identity, status in sorted(residue.items()):
        measured = axes._row(identity, status)
        if str(measured["verdict"]) != GUARD_VERDICT:
            continue
        provider, _, model = identity.partition("::")
        seam_row = seam_map.get(provider, {"bucket": ABSENT, "fake_answers_all": False})
        bucket = str(seam_row["bucket"])
        name_ok = bool(re.search(rf"\b{re.escape(model)}\b", census._package_blob(provider)))
        cf_declared = _flip(measured, "declared")
        rows[identity] = {
            "status": status,
            "measured": measured,
            "bucket": bucket,
            "fake_ok": bool(seam_row["fake_answers_all"]),
            "name_ok": name_ok,
            "contract_fqn": _contract_fqn(measured),
            "inert": str(axes._verdict(dict(measured))),
            "cf_declared": cf_declared,
            "cf_contract": _flip(measured, "is_contract"),
            "cf_action": census._action(
                cf_declared,
                bool(measured["ruled_out"]),
                bucket,
                bool(seam_row["fake_answers_all"]),
                name_ok,
            ),
        }

    print(f"population_residue={len(residue)} targeted={len(rows)}")
    for identity, row in sorted(rows.items()):
        measured = row["measured"]
        print(
            f"rowcf {identity} status={row['status']} verdict={measured['verdict']} "
            f"declared={measured.get('declared')} is_contract={measured.get('is_contract')} "
            f"look_alike={measured.get('look_alike')} no_contract={measured.get('no_contract')} "
            f"contract={measured.get('contract')} emitted={measured.get('emitted')} "
            f"contract_fqn={row['contract_fqn']} "
            f"counts={measured.get('counts')} "
            f"trippable_sites={len(measured.get('trippable', []))} "
            f"raises={measured.get('raises', 0)} code_raises={measured.get('code_raises', 0)} "
            f"seam_bucket={row['bucket']} "
            f"fake_ok={row['fake_ok']} name_in_package={row['name_ok']} "
            f"inert_refeed={row['inert']} cf_declared={row['cf_declared']} "
            f"cf_contract={row['cf_contract']} cf_action={row['cf_action']} "
            f"edit_site={_fetcher_file(identity)}"
        )

    flags: dict[str, bool] = {}
    carrier_set, carrier_counter = _carrier_retarget()
    flags["A_target_matches_carrier"] = set(rows) == carrier_set and len(rows) == carrier_counter
    print(
        f"A_target_matches_carrier mine={len(rows)} carrier_set={len(carrier_set)} "
        f"carrier_counter={carrier_counter} only_mine={sorted(set(rows) - carrier_set)[:4]} "
        f"only_carrier={sorted(carrier_set - set(rows))[:4]} "
        f"PASS={flags['A_target_matches_carrier']}"
    )

    reproduced = [i for i, r in rows.items() if r["inert"] == str(r["measured"]["verdict"])]
    flags["B_inert_refeed"] = len(reproduced) == len(rows) and len(rows) > 0
    print(
        f"B_inert_refeed rows={len(rows)} reproduced={len(reproduced)} "
        f"PASS={flags['B_inert_refeed']}"
    )

    agree = [i for i, r in rows.items() if r["cf_declared"] == r["cf_contract"]]
    flags["C_two_route_invariance"] = len(agree) == len(rows) and len(rows) > 0
    print(
        f"C_two_route_invariance rows={len(rows)} agree={len(agree)} "
        f"disagree={sorted(set(rows) - set(agree))[:4]} PASS={flags['C_two_route_invariance']}"
    )

    moved = [i for i, r in rows.items() if r["cf_declared"] != str(r["measured"]["verdict"])]
    flags["D_guard_removal_total"] = len(moved) == len(rows) and len(rows) > 0
    print(
        f"D_guard_removal_total targets={len(rows)} moved_off_guard={len(moved)} "
        f"PASS={flags['D_guard_removal_total']}"
    )

    control_ids = list(axes.CONTROLS_POSITIVE) + [axes.CONTROL_NEGATIVE]
    control_rows = [(cid, axes._row(cid, judged.get(cid, "?"))) for cid in control_ids]
    open_ids = {
        cid for cid, face in control_rows if face.get("declared") or face.get("is_contract")
    }
    noop_ids = {
        cid
        for cid, face in control_rows
        if cid in open_ids and _flip(face, "declared") == str(face["verdict"])
    }
    flags["E_non_target_invariance"] = bool(open_ids) and noop_ids == open_ids
    statuses = [(cid.partition("::")[2], str(face["verdict"])) for cid, face in control_rows]
    print(
        f"E_non_target_invariance sampled={len(control_rows)} gate_already_open={len(open_ids)} "
        f"flip_is_noop={len(noop_ids)} vacuous={not open_ids} gate_closed={statuses} "
        f"PASS={flags['E_non_target_invariance']}"
    )

    cf_values = sorted({r["cf_declared"] for r in rows.values()})
    off_enum = sorted(set(cf_values) - set(axes.VERDICTS))
    flags["F_cf_verdict_enum"] = not off_enum
    print(
        f"F_cf_verdict_enum distinct={cf_values} off_enum={off_enum} "
        f"PASS={flags['F_cf_verdict_enum']}"
    )

    action_dist = Counter(r["cf_action"] for r in rows.values())
    route_action = action_dist.get("shard_entry_only", 0)
    route_fields = sum(
        1
        for r in rows.values()
        if r["cf_declared"] in axes.TRIPPABLE and r["fake_ok"] and r["bucket"] != NO_PACKAGE
    )
    nested = sum(action_dist.values())
    flags["G_landing_reconciles"] = nested == len(rows) and route_action == route_fields
    print(
        f"G_landing_reconciles nested={nested} rows={len(rows)} "
        f"actions={dict(sorted(action_dist.items()))} "
        f"shard_entry_only_via_action={route_action} via_verdict_and_fake={route_fields} "
        f"PASS={flags['G_landing_reconciles']}"
    )

    # H: the first draft of this arm flagged ``emitted == []`` as an unexplained shape. That was the
    # arm's bug, not the repo's: the shipped walk only collects classes constructed inside the
    # fetcher's own MRO classes and their same-module helpers, so a serving path that builds plain
    # dicts reads ``emitted=[]`` with a contract present -- 22 of these 24 rows. What can be
    # inconsistent is the ``is_contract`` bit against a second route to it.
    shapes: Counter[str] = Counter()
    inconsistent = []
    unresolvable = []
    ctor_text_hits = 0
    for identity, row in rows.items():
        measured = row["measured"]
        emitted = list(measured.get("emitted") or [])
        if emitted:
            shapes["constructs_other_contract_class"] += 1
        elif measured.get("no_contract"):
            shapes["no_contract_class"] += 1
        else:
            shapes["constructs_no_contract_class"] += 1
            name = str(measured.get("contract") or "")
            blob = census._package_blob(identity.partition("::")[0])
            if name and re.search(rf"\b{re.escape(name)}\s*\(", blob):
                ctor_text_hits += 1
        fqn = row["contract_fqn"]
        if not fqn and not measured.get("no_contract"):
            unresolvable.append(identity)
        if _is_contract_inconsistent(row):
            inconsistent.append(identity)
    look_alike = sum(1 for r in rows.values() if r["measured"].get("look_alike"))
    gated_on_absent_contract = [
        i
        for i, r in rows.items()
        if r["cf_declared"] in axes.TRIPPABLE and r["measured"].get("no_contract")
    ]
    landing_on_absent_contract = [
        i for i in gated_on_absent_contract if rows[i]["cf_action"] == "shard_entry_only"
    ]
    flags["H_axis_consistency"] = bool(rows) and not inconsistent and not unresolvable
    print(
        f"H_axis_consistency rows={len(rows)} shapes={dict(sorted(shapes.items()))} "
        f"is_contract_rederived_inconsistently={sorted(inconsistent)[:4]} "
        f"contract_unresolvable={sorted(unresolvable)[:4]} look_alike={look_alike} "
        f"trippable_only_because_no_contract={len(gated_on_absent_contract)} "
        f"of_which_landing={len(landing_on_absent_contract)} "
        f"empty_emitted_with_ctor_text_in_package={ctor_text_hits} "
        f"(text route is a floor, not a gate) PASS={flags['H_axis_consistency']}"
    )

    # I: H's consistency test has never been observed to fire, so forge the axis in memory on one
    # row and require the same test to catch it. Nothing is written to disk.
    forged_id, sample = sorted(rows.items())[0]
    clean_hit = _is_contract_inconsistent(sample)
    forged = {
        **sample,
        "measured": {
            **sample["measured"],
            "is_contract": not bool(sample["measured"].get("is_contract")),
        },
    }
    forged_hit = _is_contract_inconsistent(forged)
    flags["I_h_rearm"] = (not clean_hit) and forged_hit
    print(
        f"I_h_rearm row={forged_id} clean_flagged={clean_hit} forged_flagged={forged_hit} "
        f"PASS={flags['I_h_rearm']}"
    )

    verdict_dist = Counter(r["cf_declared"] for r in rows.values())
    arms = len(flags)
    arms_passed = sum(1 for flag in flags.values() if flag)
    print(
        f"rowclass_counterfactual_face targeted={len(rows)} "
        f"cf_verdicts={dict(sorted(verdict_dist.items()))} "
        f"cf_actions={dict(sorted(action_dist.items()))} landing_today={route_action} "
        f"landing_needing_edit_only={route_action - len(landing_on_absent_contract)} "
        f"identity_set={str(report['inventory']['expected_identity_set_sha256'])[:16]} "
        f"arms={arms} arms_passed={arms_passed} arms_failed={arms - arms_passed}"
    )
    return 0 if arms_passed == arms else 1


if __name__ == "__main__":
    raise SystemExit(main())
