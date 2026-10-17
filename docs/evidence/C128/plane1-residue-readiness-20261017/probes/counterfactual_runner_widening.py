"""Price what a runner widening buys *before* editing the runner.

Section 90.7's path 甲 says: add the verbs the shared fake lacks, then re-produce the 1330 receipts
pinned to ``iter2_claims_probe_runner.py``. The step-2a probe (``seam_reachable_axes.py``) measured
``convertible=0`` over the 7 rows that are already seam-reachable, so 乙 buys nothing and plane 1
can only move by widening the harness -- *if* the rows behind the widening are convertible. This
probe answers three questions the fork actually turns on, all without touching a pinned byte:

1. **Which verbs, exactly?** ``_FakeSharedClient`` is widened to the union of the verbs provider
   packages call on the shared client, read through the shipped receiver rule
   (``incomplete_row_guard_check._client_names`` / ``_is_client_receiver``), so a BeautifulSoup
   ``tag.get(...)`` cannot impersonate a seam call. Anything the packages never call is *not*
   proposed -- wave 24's note said "``request`` / ``post``", and this prints whether ``post`` is
   measured anywhere.
2. **Which providers flip, and how many rows ride on them?** The bucket arithmetic is the shipped
   census's own: ``seam_reachability_census.py`` is run as a subprocess and its
   ``models_verbs=/package_verbs=/residual=`` fields are the denominator here.
3. **Are those rows convertible at all?** Each one goes through ``incomplete_row_guard_check._row``,
   imported unchanged -- ``bound``/``ruled_out``/``guard``/``row_class`` do not depend on the fake,
   so a row that reads ``UNBOUND`` or ``ROW_CLASS_NOT_CONTRACT`` today stays unconvertible after any
   widening, and no receipt re-production would have bought it.

Arms, all seven of which can fail:

``A_landed_positive`` / ``B_landed_negative``
    the wave-25 controls still read trippable / non-trippable, so a ``convertible=0`` here is a
    statement about the pool and not about a dead measurer.
``C_widening_can_differ``
    every provider in the flip set must be missing at least one verb against the *shipped* surface
    and zero verbs against the widened surface. A flip set that is empty on either side means the
    counterfactual is not being applied.
``D_verbs_are_measured``
    the widened verb set equals the shipped surface plus exactly the call sites found in provider
    packages; ``post_required`` is printed so a speculative fake method shows up as 0.
``E_population_reconciles``
    the row count selected from the judged report equals the sum of the census's ``residual=`` over
    the flip set.
``F_verdict_enum``
    every verdict is one of the six shipped values.
``G_declaration_axis``
    each row's membership in ``declare_candidates._residue_needs_declare`` must equal
    ``bound and not declared`` as the axis measurer reads them -- the split that decides whether the
    row costs a witness entry or a declaration plus a witness entry.

Exits non-zero when an arm fails. ``convertible`` is the price of path 甲; it is not a plane-1
prediction, because a landed row also needs the five phases and a primary review.
"""

from __future__ import annotations

import ast
import re
import subprocess  # nosec B404
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import declare_candidates as dc  # the shipped NEED_DECLARE population
import incomplete_row_guard_check as axes  # the shipped four-axis measurer and receiver rule
import seam_surface_match as seam  # the shipped fake surface, verb census and report reader

PROBE_DIR = Path(__file__).resolve().parent
REACHABILITY_PROBE = PROBE_DIR / "seam_reachability_census.py"
LINE = "seam_census_provider="


def _reachability_lines() -> list[dict[str, str]]:
    """Run the shipped census and parse its provider lines as this probe's denominator."""
    run = subprocess.run(  # nosec B603  # noqa: S603
        [sys.executable, str(REACHABILITY_PROBE)],
        cwd=seam.REPO_ROOT,
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


def _verbs(field: str) -> set[str]:
    """Decode one printed list field (``['get', 'request']``) into a set."""
    if not field.startswith("["):
        return set()
    try:
        decoded = ast.literal_eval(field)
    except (SyntaxError, ValueError):
        raise SystemExit(
            f"unparseable verb field {field!r} -- the census line was split wrong"
        ) from None
    return {str(verb) for verb in decoded}


def _seam_call_arity(provider: str, verbs: set[str]) -> dict[str, list[int]]:
    """Positional-argument counts for every seam-receiver call of ``verbs`` in this package."""
    found: dict[str, list[int]] = defaultdict(list)
    for path in sorted((seam.REPO_ROOT / "opendata/data/providers" / provider).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = axes._client_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not axes._is_client_receiver(node.func, names):
                continue
            if node.func.attr in verbs:
                found[node.func.attr].append(len(node.args))
    return dict(found)


def _residual_rows(report: dict[str, Any], wanted: set[str]) -> list[tuple[str, str]]:
    """Every non-``OFFLINE_ACCEPTED`` identity whose provider sits in ``wanted``."""
    return sorted(
        (str(row["model_identity"]), str(row["status"]))
        for row in report["models"]
        if str(row["model_identity"]).partition("::")[0] in wanted
        and str(row["status"]) != "OFFLINE_ACCEPTED"
    )


def main() -> int:
    """Print the counterfactual widening, the pool it unlocks, and that pool on the four axes."""
    lines = _reachability_lines()
    report = seam._load_report()
    judged = {str(row["model_identity"]): str(row["status"]) for row in report["models"]}
    identity_set = str(report["inventory"]["expected_identity_set_sha256"])[:16]
    surface = seam._fake_surface()
    needed: set[str] = set()
    for p in lines:
        needed |= _verbs(p.get("package_verbs", "[]")) - set(surface)
    widened = set(surface) | needed

    flip: dict[str, int] = {}
    for p in lines:
        package_verbs = _verbs(p.get("package_verbs", "[]"))
        residual = int(p["residual"])
        if not package_verbs or not residual:
            continue
        missing_now = package_verbs - set(surface)
        missing_after = package_verbs - widened
        reachable_after = p["bucket"] == "SEAM_REACHABLE"
        if missing_now and not missing_after:
            flip[p["seam_census_provider"]] = residual
            print(
                f"flip {p['seam_census_provider']} bucket_now={p['bucket']} "
                f"package_verbs={sorted(package_verbs)} missing_now={sorted(missing_now)} "
                f"missing_after={sorted(missing_after)} witness_module={p['witness_module']} "
                f"client_seam={p['client_seam']} residual={residual}"
            )
        elif not missing_now and not reachable_after and residual:
            print(
                f"unaffected {p['seam_census_provider']} bucket_now={p['bucket']} "
                f"missing_now={sorted(missing_now)} residual={residual}"
            )

    new_verbs = sorted(widened - set(surface))
    arity = {p: _seam_call_arity(p, set(new_verbs)) for p in sorted(flip)}
    for provider, per_verb in arity.items():
        print(
            f"2_arity {provider} new_verbs={ {v: sorted(c) for v, c in sorted(per_verb.items())} } "
            f"post_sites={len(per_verb.get('post', []))}"
        )
    print(
        f"0_surface shipped={sorted(surface)} required={sorted(needed)} "
        f"widened={sorted(widened)} new_verbs={new_verbs} "
        f"post_required={'post' in needed}"
    )

    population = _residual_rows(report, set(flip))
    measured = [axes._row(identity, status) for identity, status in population]
    needs_declare = set(dc._residue_needs_declare(report))
    for row in measured:
        identity = str(row["identity"])
        tag = dc._tag(dc._price(identity)) if identity in needs_declare else "-"
        print(
            f"row {identity} status={row['status']} bound={row['bound']} "
            f"ruled_out={bool(row['ruled_out'])} no_ledger={row.get('no_ledger') or '-'} "
            f"declared={row.get('declared', False)} witness_entry={row['witness_entry']} "
            f"declare_tag={tag} verdict={row['verdict']} "
            f"raises={row.get('raises', 0)} code_raises={row.get('code_raises', 0)}"
        )

    verdicts: Counter[str] = Counter(str(row["verdict"]) for row in measured)
    convertible = [str(row["identity"]) for row in measured if row["verdict"] in axes.TRIPPABLE]
    by_provider: dict[str, int] = defaultdict(int)
    for row in measured:
        by_provider[str(row["identity"]).partition("::")[0]] += 1

    positive = [axes._row(c, judged.get(c, "?")) for c in axes.CONTROLS_POSITIVE]
    arm_a = all(r["verdict"] in axes.TRIPPABLE for r in positive) and len(positive) == 2
    print(
        f"A_landed_positive controls={len(positive)} "
        f"trippable={sum(1 for r in positive if r['verdict'] in axes.TRIPPABLE)} PASS={arm_a}"
    )

    negative = axes._row(axes.CONTROL_NEGATIVE, judged.get(axes.CONTROL_NEGATIVE, "?"))
    arm_b = negative["verdict"] not in axes.TRIPPABLE
    print(f"B_landed_negative {axes.CONTROL_NEGATIVE} verdict={negative['verdict']} PASS={arm_b}")

    arm_c = bool(flip) and all(
        _verbs(p.get("package_verbs", "[]")) - set(surface)
        and not (_verbs(p.get("package_verbs", "[]")) - widened)
        for p in lines
        if p["seam_census_provider"] in flip
    )
    print(f"C_widening_can_differ flipped={len(flip)} PASS={arm_c}")

    arm_d = set(new_verbs) == (needed - set(surface)) and all(
        sum(len(c) for v, c in per_verb.items() if v != "post") > 0 for per_verb in arity.values()
    )
    print(
        f"D_verbs_are_measured new_verbs={new_verbs} providers_with_sites="
        f"{sum(1 for v in arity.values() if v)} PASS={arm_d}"
    )

    arm_e = len(population) == sum(flip.values())
    print(
        f"E_population_reconciles rows={len(population)} flip_residual={sum(flip.values())} "
        f"PASS={arm_e}"
    )

    off_enum = sorted(set(verdicts) - set(axes.VERDICTS))
    arm_f = not off_enum
    print(f"F_verdict_enum distinct={len(verdicts)} violations={off_enum} PASS={arm_f}")

    inconsistent = sorted(
        str(row["identity"])
        for row in measured
        if (str(row["identity"]) in needs_declare)
        != (bool(row["bound"]) and not bool(row.get("declared")))
    )
    arm_g = not inconsistent
    print(
        f"G_declaration_axis inside={len(needs_declare & {i for i, _ in population})} "
        f"contradictions={len(inconsistent)} ids={inconsistent[:3]} PASS={arm_g}"
    )

    arms = (arm_a, arm_b, arm_c, arm_d, arm_e, arm_f, arm_g)
    print(
        f"counterfactual_widening_face flipped_providers={dict(sorted(flip.items()))} "
        f"new_verbs={new_verbs} post_required={'post' in needed} "
        f"population={len(population)} by_provider={dict(sorted(by_provider.items()))} "
        f"verdicts={dict(sorted(verdicts.items()))} convertible={len(convertible)} "
        f"convertible_rows={convertible} movable={len(verdicts) > 1} "
        f"arms={len(arms)} arms_passed={sum(arms)} arms_failed={len(arms) - sum(arms)} "
        f"identity_set={identity_set}"
    )
    return 0 if all(arms) else 1


if __name__ == "__main__":
    raise SystemExit(main())
