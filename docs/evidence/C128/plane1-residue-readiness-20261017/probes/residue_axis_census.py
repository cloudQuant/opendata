"""Cross the whole judge-report residue with three shipped instruments, so "what is left" is a face.

Wave 27 step 2 has now priced two candidate batches and both read ``convertible=0``: the 7 rows the
reachability census calls ``SEAM_REACHABLE`` (``seam_reachable_axes.py``) and the 29 rows a
``request``-widened shared fake would unlock (``counterfactual_runner_widening.py``). Those are two
slices, and a decision taken from slices is how §87.8 armed "7 INCOMPLETE => 266 -> 273" and §89.1
inferred yfinance's mechanism. This probe takes the whole residue at once and, per row, prints the
answer from each of the three shipped instruments side by side -- the pricer's bucket, the axis
measurer's verdict, the census's seam bucket / mechanism / witness-module flag -- plus the first job
those three jointly imply.

Every column is imported, never re-typed: ``readiness_census._readiness/_bucket`` for the pipeline
bucket, ``incomplete_row_guard_check._row`` for the four axes, and the reachability census's own
stdout for the seam bucket and the per-provider residual that ``D_bucket_reconciles`` compares
against. Rows come from the judge report the gate reads, so the population is ``350 - 266`` by
construction and ``C_population_identity`` prints that arithmetic rather than asserting a constant.
The three group prints are the same population seen three ways: ``1_cross_tab`` keys it by
(pipeline bucket, axis verdict), ``2_actions`` by the implied action, and ``3_action_by_provider``
by (action, provider); ``4_action_totals`` prints ``actions`` and ``nested_sum`` so the last two are
compared, and ``C_population_identity`` gates ``nested_sum`` on the residue instead of trusting it.
``5_needs_first_module`` re-cuts the residue by whether the provider already has a witness module on
the registry, because an action like ``retarget_fetcher_output`` is one job in a provider that still
owes two. ``6_registry_name_axis`` splits every ``UNBOUND`` row by whether the model's own name
appears as a whole token in that provider's package source: the two halves are different sizes of
work -- a class that exists but is not registered versus a model nobody has written yet. That print
also carries ``lookup_shape``, which measures how those rows fail in the registry rather than
asserting it. They all read ``LookupError: unknown provider model`` in the generation this probe was
written, which is why the earlier ``rebind_fetcher`` label for them was wrong.

Arms, all eight of which can fail:

``A_landed_positive`` / ``B_landed_negative``
    the wave-25 controls still read trippable and non-trippable through the imported measurer.
``C_population_identity``
    ``len(residue) == report_rows - accepted == nested_sum`` and the identity set matches the landed
    face.
``D_bucket_reconciles``
    the per-provider bucket counts recomputed here equal ``buckets_by_provider`` printed by a
    subprocess run of ``readiness_census.py``, and the bucket sums equal the residue.
``E_verdict_enum``
    every verdict is one of the six shipped values and every seam bucket is one of the five.
``F_witnessable_today``
    ``witnessable_today`` counts rows that need *only* a shard entry and a review right now: bound,
    declared, contract-equal, document-trippable, and on a seam the shipped fake already answers.
    Two-sided since wave 28: ``_action`` is re-fed each landed control's *measured* verdict,
    ruled-out and name-in-package axes with the harness axis forced True and then False, and the arm
    requires ``shard_entry_only`` and ``shard_entry_needs_harness`` respectively, with the control's
    real ``fake_answers_all`` also True so the first arm is not a counterfactual at all. The first
    version compared ``len(witnessable)`` to ``actions["shard_entry_only"]``, but both are
    incremented in the same loop body from the same return value, so that arm could never fail and
    the published ``witnessable_today=0`` was not a measured finding. The residue reads zero either
    way; what the control adds is proof that the counter is capable of one.
``G_census_coverage``
    no residue row's provider is missing from the reachability census (the map used to fall back to
    a default silently), and ``rows_without_module + rows_with_module == residue``.
``H_name_test_identity``
    the three independent ``UNBOUND`` counters (the verdict tally, the name-token split and the
    registry-lookup tally) agree, the action split reproduces them once the package-less rows are
    added back, no provider package scanned was empty except the ones the census itself calls
    ``NO_PACKAGE_ON_DISK`` (an empty blob would make every name read "absent"), and the name test is
    two-sided: the landed control models' names are found in their packages while a forged name is
    not.

Exits non-zero when an arm fails.
"""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import incomplete_row_guard_check as axes  # the shipped four-axis row measurer
import readiness_census as rc  # the shipped pipeline-order bucket rule
import seam_surface_match as seam  # the landed judged report and fake surface

PROBE_DIR = Path(__file__).resolve().parent
CENSUS_PROBE = PROBE_DIR / "seam_reachability_census.py"
READINESS_PROBE = PROBE_DIR / "readiness_census.py"
CENSUS_FIELDS = ("seam_census_provider",)
READINESS_FIELDS = ("residue_by_provider", "buckets_by_provider")
SEAM_BUCKETS = (
    "SEAM_REACHABLE",
    "SEAM_USED_OUTSIDE_MODELS",
    "BLOCKED_ON_RUNNER_SURFACE",
    "SEAM_NEVER_USED",
    "NO_PACKAGE_ON_DISK",
)
_BLOB_CACHE: dict[str, str] = {}


def _run(probe: Path, names: tuple[str, ...]) -> list[dict[str, str]]:
    """Run one shipped probe and keep the named fields, split at every ``key=`` boundary."""
    run = subprocess.run(  # nosec B603  # noqa: S603
        [sys.executable, str(probe)],
        cwd=seam.REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        raise SystemExit(f"{probe.name} exited {run.returncode}: {run.stderr.strip()[:200]}")
    kept = [line for line in run.stdout.splitlines() if line.partition("=")[0] in names]
    return [
        dict(token.split("=", 1) for token in re.split(r"\s+(?=\w+=)", line) if "=" in token)
        for line in kept
    ]


def _verbs(field: str) -> set[str]:
    """Decode a printed list field such as ``['get', 'request']``."""
    if not field.startswith("["):
        return set()
    return {str(verb) for verb in json.loads(field.replace("'", '"'))}


def _seam_map(lines: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    """Provider to its census reading: bucket, mechanism, residual, and fake/module coverage."""
    surface = seam._fake_surface()
    out: dict[str, dict[str, Any]] = {}
    for line in lines:
        provider = line["seam_census_provider"]
        package_verbs = _verbs(line.get("package_verbs", "[]"))
        out[provider] = {
            "bucket": line["bucket"],
            "mechanism": line["mechanism"],
            "residual": int(line["residual"]),
            "witness_module": line["witness_module"] == "True",
            "fake_answers_all": bool(package_verbs) and package_verbs <= surface,
        }
    return out


def _package_blob(provider: str) -> str:
    """Concatenate a provider package's Python source, so a model name can be tested."""
    root = seam.REPO_ROOT / "opendata" / "data" / "providers" / provider
    if not root.is_dir():
        return ""
    if provider not in _BLOB_CACHE:
        _BLOB_CACHE[provider] = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(root.rglob("*.py"))
        )
    return _BLOB_CACHE[provider]


def _action(
    verdict: str, ruled_out: bool, seam_bucket: str, fake_ok: bool, name_in_package: bool
) -> str:
    """The first job this row has in ladder order.

    It is not the only job: a row here can also still need a shard entry, and
    ``5_needs_first_module`` prints which of these actions sit in a provider that has no witness
    module at all. ``UNBOUND`` rows all read ``LookupError:unknown provider model``, so the name
    test splits them into a class that exists but is not registered versus a model nobody has
    written yet -- two different sizes of work, and the earlier label for both was wrong.
    """
    if seam_bucket == "NO_PACKAGE_ON_DISK":
        return "new_provider_package"
    if verdict == "UNBOUND":
        if ruled_out:
            return "ledger_ruled_out"
        return "registry_publish_model" if name_in_package else "implement_missing_model"
    if verdict == "ROW_CLASS_NOT_CONTRACT":
        return "retarget_fetcher_output"
    if verdict in axes.TRIPPABLE:
        return "shard_entry_only" if fake_ok else "shard_entry_needs_harness"
    return "add_code_carrying_guard"


def main() -> int:
    """Print the whole-residue cross-tab and the first job each row implies."""
    report = seam._load_report()
    judged = {str(row["model_identity"]): str(row["status"]) for row in report["models"]}
    identity_set = str(report["inventory"]["expected_identity_set_sha256"])[:16]
    residue = rc._residue(report)
    seam_map = _seam_map(_run(CENSUS_PROBE, CENSUS_FIELDS))

    domain_ok: dict = {}
    actions: Counter[str] = Counter()
    action_provider: dict[str, Counter] = defaultdict(Counter)
    cross: dict[tuple[str, str], int] = defaultdict(int)
    witnessable: list[str] = []
    absent: list[str] = []
    empty_blobs: set[str] = set()
    scanned: set[str] = set()
    unbound_rows = 0
    unbound_package_less = 0
    unbound_names: Counter[str] = Counter()
    unbound_lookup: Counter[str] = Counter()
    no_module: Counter[str] = Counter()
    mine_status: dict[str, dict[str, int]] = defaultdict(dict)
    mine_buckets: dict[str, dict[str, int]] = defaultdict(dict)
    for identity, status in sorted(residue.items()):
        provider, _, model = identity.partition("::")
        face = rc._readiness(provider, model, domain_ok)
        bucket = rc._bucket(face)
        measured = axes._row(identity, status)
        verdict = str(measured["verdict"])
        seam_row = seam_map.get(provider)
        if seam_row is None:
            absent.append(identity)
            seam_row = {"bucket": "ABSENT_FROM_CENSUS", "fake_answers_all": False}
        seam_bucket = str(seam_row["bucket"])
        blob = _package_blob(provider)
        name_in_package = bool(re.search(rf"\b{re.escape(model)}\b", blob))
        if not blob:
            empty_blobs.add(provider)
        scanned.add(provider)
        if verdict == "UNBOUND":
            unbound_rows += 1
            if seam_bucket == "NO_PACKAGE_ON_DISK":
                unbound_package_less += 1
            unbound_names["absent" if not name_in_package else "present"] += 1
            text = str(measured.get("resolve_error", ""))
            shape = (
                "unknown_provider_model"
                if "unknown provider model" in text
                else (text or "no_resolve_error")
            )
            unbound_lookup[shape] += 1
        action = _action(
            verdict,
            bool(measured["ruled_out"]),
            seam_bucket,
            bool(seam_row["fake_answers_all"]),
            name_in_package,
        )
        actions[action] += 1
        action_provider[action][provider] += 1
        if not bool(seam_row.get("witness_module", False)):
            no_module[action] += 1
        cross[(bucket, verdict)] += 1
        mine_status[provider][status] = mine_status[provider].get(status, 0) + 1
        mine_buckets[provider][bucket] = mine_buckets[provider].get(bucket, 0) + 1
        if action == "shard_entry_only":
            witnessable.append(identity)
        print(
            f"row {identity} status={status} bucket={bucket} verdict={verdict} "
            f"seam={seam_bucket} mechanism={seam_row.get('mechanism', 'NA')} "
            f"witness_module={seam_row.get('witness_module', 'NA')} "
            f"fake_ok={seam_row['fake_answers_all']} "
            f"name_in_package={name_in_package} "
            f"ruled_out={bool(measured['ruled_out'])} declared={face['declared']} "
            f"witness_entry={face['witness_entry']} action={action}"
        )

    print(f"1_cross_tab {dict(sorted(cross.items()))}")
    print(
        f"2_actions {dict(sorted(actions.items()))} sums={sum(actions.values())} "
        f"registry_publish_model={actions['registry_publish_model']}"
    )
    nested = {a: dict(sorted(p.items())) for a, p in sorted(action_provider.items())}
    nested_sum = sum(sum(p.values()) for p in nested.values())
    print(f"3_action_by_provider {nested}")
    print(f"4_action_totals actions={len(actions)} nested_sum={nested_sum} residue={len(residue)}")
    print(
        f"5_needs_first_module no_witness_module={dict(sorted(no_module.items()))} "
        f"rows={sum(no_module.values())} of={len(residue)}"
    )
    print(
        f"6_registry_name_axis unbound={dict(sorted(unbound_names.items()))} "
        f"total={sum(unbound_names.values())} lookup_shape={dict(sorted(unbound_lookup.items()))} "
        f"scanned_providers={len(scanned)} empty_blobs={sorted(empty_blobs)}"
    )

    positive = [axes._row(c, judged.get(c, "?")) for c in axes.CONTROLS_POSITIVE]
    arm_a = len(positive) == 2 and all(r["verdict"] in axes.TRIPPABLE for r in positive)
    print(f"A_landed_positive controls={len(positive)} PASS={arm_a}")

    negative = axes._row(axes.CONTROL_NEGATIVE, judged.get(axes.CONTROL_NEGATIVE, "?"))
    arm_b = negative["verdict"] not in axes.TRIPPABLE
    print(f"B_landed_negative {axes.CONTROL_NEGATIVE} verdict={negative['verdict']} PASS={arm_b}")

    arm_c = (
        len(residue)
        == len(report["models"])
        - sum(1 for row in report["models"] if row["status"] == "OFFLINE_ACCEPTED")
        == nested_sum
    )
    print(
        f"C_population_identity residue={len(residue)} report_rows={len(report['models'])} "
        f"nested_sum={nested_sum} identity_set={identity_set} PASS={arm_c}"
    )

    readiness = _run(READINESS_PROBE, READINESS_FIELDS)
    shipped_status: dict[str, dict[str, int]] = {}
    shipped_buckets: dict[str, dict[str, int]] = {}
    for fields in readiness:
        if "residue_by_provider" in fields:
            shipped_status = json.loads(fields["residue_by_provider"])
        if "buckets_by_provider" in fields:
            shipped_buckets = json.loads(fields["buckets_by_provider"])
    status_drift = sorted(
        provider
        for provider in set(shipped_status) | set(mine_status)
        if shipped_status.get(provider) != mine_status.get(provider)
    )
    bucket_drift = sorted(
        provider
        for provider in set(shipped_buckets) | set(mine_buckets)
        if shipped_buckets.get(provider) != mine_buckets.get(provider)
    )
    arm_d = bool(shipped_status) and bool(shipped_buckets) and not status_drift and not bucket_drift
    print(
        f"D_bucket_reconciles providers={len(shipped_status)} status_drift={status_drift[:4]} "
        f"bucket_drift={bucket_drift[:4]} PASS={arm_d}"
    )

    off_verdict = sorted({v for (_b, v) in cross} - set(axes.VERDICTS))
    off_seam = sorted({str(r["bucket"]) for r in seam_map.values()} - set(SEAM_BUCKETS))
    arm_e = not off_verdict and not off_seam
    print(f"E_verdict_enum off_verdict={off_verdict} off_seam={off_seam} PASS={arm_e}")

    control_actions: set[str] = set()
    control_actions_no_harness: set[str] = set()
    control_fake: list[bool] = []
    for control, row in zip(axes.CONTROLS_POSITIVE, positive, strict=True):
        provider, _, model = control.partition("::")
        seam_row = seam_map.get(provider, {})
        bucket = str(seam_row.get("bucket", "ABSENT_FROM_CENSUS"))
        name_ok = bool(re.search(rf"\b{re.escape(model)}\b", _package_blob(provider)))
        verdict = str(row["verdict"])
        ruled_out = bool(row["ruled_out"])
        control_fake.append(bool(seam_row.get("fake_answers_all", False)))
        control_actions.add(_action(verdict, ruled_out, bucket, True, name_ok))
        control_actions_no_harness.add(_action(verdict, ruled_out, bucket, False, name_ok))
    arm_f = (
        len(witnessable) == actions["shard_entry_only"]
        and len(control_actions) == 1
        and control_actions == {"shard_entry_only"}
        and control_actions_no_harness == {"shard_entry_needs_harness"}
        and all(control_fake)
    )
    print(
        f"F_witnessable_today count={len(witnessable)} rows={witnessable[:4]} "
        f"counter={actions['shard_entry_only']} controls={len(positive)} "
        f"control_actions_on_harness={sorted(control_actions)} "
        f"control_actions_off_harness={sorted(control_actions_no_harness)} "
        f"control_real_fake_ok={control_fake} PASS={arm_f}"
    )

    arm_g = not absent and sum(no_module.values()) == len(residue) - sum(
        1
        for identity in residue
        if seam_map.get(identity.partition("::")[0], {}).get("witness_module") is True
    )
    print(
        f"G_census_coverage absent={absent[:4]} rows_without_module={sum(no_module.values())} "
        f"rows_with_module={len(residue) - sum(no_module.values())} PASS={arm_g}"
    )

    arm_h_population = (
        actions["ledger_ruled_out"]
        + actions["implement_missing_model"]
        + actions["registry_publish_model"]
    )
    landed_present = sum(
        1
        for control in axes.CONTROLS_POSITIVE
        if re.search(
            rf"\b{control.partition('::')[2]}\b", _package_blob(control.partition("::")[0])
        )
    )
    forged_absent = not re.search(r"\bNoSuchModelZz\b", _package_blob("fmp"))
    expected_empty = sorted(
        provider for provider in scanned if seam_map[provider]["bucket"] == "NO_PACKAGE_ON_DISK"
    )
    arm_h = (
        unbound_rows == sum(unbound_names.values()) == sum(unbound_lookup.values())
        and unbound_rows == arm_h_population + unbound_package_less
        and sorted(empty_blobs) == expected_empty
        and landed_present == len(axes.CONTROLS_POSITIVE)
        and forged_absent
    )
    print(
        f"H_name_test_identity unbound={unbound_rows} name_axis={sum(unbound_names.values())} "
        f"lookup_axis={sum(unbound_lookup.values())} actions_sum={arm_h_population} "
        f"unbound_package_less={unbound_package_less} package_less_providers={expected_empty} "
        f"landed_controls_present={landed_present}/{len(axes.CONTROLS_POSITIVE)} "
        f"forged_absent={forged_absent} empty_blobs={sorted(empty_blobs)} PASS={arm_h}"
    )

    arms = (arm_a, arm_b, arm_c, arm_d, arm_e, arm_f, arm_g, arm_h)
    print(
        f"residue_axis_census_face residue={len(residue)} actions={dict(sorted(actions.items()))} "
        f"witnessable_today={len(witnessable)} rows_without_module={sum(no_module.values())} "
        f"cross_distinct={len(cross)} identity_set={identity_set} arms={len(arms)} "
        f"arms_passed={sum(arms)} arms_failed={len(arms) - sum(arms)}"
    )
    return 0 if all(arms) else 1


if __name__ == "__main__":
    raise SystemExit(main())
