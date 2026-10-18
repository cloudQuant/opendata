"""Wave-32 landing radius and attribution: who the re-sign touched, and who moved.

The harness file ``scripts/quality/iter2_claims_probe_runner.py`` is pinned as ``probe`` in every
landed receipt, so editing it stale-ates the whole landing set and the wave had to re-sign. This
probe reads that radius off the receipts themselves rather than restating it, and it separates two
claims the wave makes that can easily be confused:

* the **radius**: how many rows the re-sign had to touch, and whether the drift detector can see
  drift at all (a counterfact forges a digest and requires the counter to go non-zero);
* the **attribution**: which provider moved, and that nothing regressed.

The attribution arm was rebuilt on 2026-10-18 after its first face was caught vacuous: it walked
the *chain carriers'* file names (``producer_fmp``) against a judge report keyed by provider
(``fmp``), so all 28 rows read ``0 -> 0`` while yfinance had demonstrably gone 0 -> 26. It now
judges both generations with ``judge.build_report`` in-process -- HEAD's ``claims.merged.json``
against today's tree, and the working copy -- groups each report's own per-model ``provider``
field, and prints a real before->after row for every one of the 32 inventory providers. Its
invariants are what make the table mean something: 32 providers, 350 rows on both sides, 0
accepted before, 317 accepted after, no provider below its own before-count, and the mover set
equal to the set of providers that hold accepted rows. The regression predicate is then shown to
bite: one ``OFFLINE_ACCEPTED`` row is flipped to ``UNREVIEWED`` in an in-memory copy of the after
report, the same face goes red on regression *and* on the 317 total, and a freshly recomputed
real face goes green again. A second attribution face compares against the report the chain
itself committed at HEAD, where every non-yfinance provider was already landed -- there the mover
set really is exactly ``['yfinance']``.

Everything else is the repo's own instrument: the judge is imported in-process, the pin walk is the
one shipped in C135, the census is C128's probe re-run as a subprocess. No pinned artifact is
written; the judge writes only under /tmp.

The ``carriers_signed`` arm walks the wave's own carrier population by name: every ``runs/*.txt``
except the carrier this probe is writing (its exit line is written after this face prints) and
except the one file the face declares superseded -- ``snapshot_repin.txt``, written by another
agent with no exit reading of its own, which this wave re-took as
``runs/snapshot_repin_take2.txt`` rather than editing.

Two readings are deliberately kept in the record even though they are not green: the shipped
census's own ``B_landed_negative`` arm is red this generation, and the census calls
``yfinance::KeyMetrics`` producible while the installed SDK publishes no surface for it.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess  # nosec B604  # read-only evidence probe; every argv is a literal list
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[5]
BUNDLE: Final = "docs/evidence/C137/wave32-yfinance-landing-20261018"
C101: Final = "docs/evidence/C101"
RUNNER: Final = "scripts/quality/iter2_claims_probe_runner.py"
FIXTURES: Final = "scripts/quality/iter2_claims_fixtures.py"
C135_PIN_WALK: Final = (
    "docs/evidence/C135/wave31-step6-domains-pin-blast-radius-20261018/"
    "probes/domains_pin_blast_radius.py"
)
CENSUS: Final = "docs/evidence/C128/plane1-residue-readiness-20261017/probes/residue_axis_census.py"
C136_CARRIER: Final = (
    "docs/evidence/C136/wave31-step7-landing-set-blast-radius-20261018/runs/"
    "landing_set_blast_radius.take5_final_bytes.txt"
)
INVENTORY: Final = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
LANDED_REPORT: Final = f"{C101}/judge/claims.merged.report.json"
SCRATCH: Final = Path("/tmp/w32_landing_radius")  # noqa: S108  # judge --output only, never pinned
PIN_GROUPS: Final = ("contract", "fixture", "probe", "source")
ACCEPTED: Final = "OFFLINE_ACCEPTED"
MISSED: Final = "UNREVIEWED"
SELF_CARRIER: Final = Path(__file__).with_suffix(".txt").name
SUPERSEDED_CARRIERS: Final = ("snapshot_repin.txt",)
TAKE2_CARRIER: Final = "snapshot_repin_take2.txt"
CARRIER_EXIT_LINE: Final = re.compile(r"\b\w*_(?:exit|rc)\s*=\s*-?\d+")


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str]:
    proc = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, no shell interpolation
        argv,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
        timeout=900,
        env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _git_show(relative: str, rev: str = "HEAD") -> str:
    rc, out = _run(["git", "show", f"{rev}:{relative}"])  # nosec B607
    if rc != 0:
        raise RuntimeError(f"git show {rev}:{relative} failed rc={rc}")
    return out


def _claims(rev: str | None = None) -> list[dict[str, Any]]:
    raw = (
        _git_show(f"{C101}/claims.merged.json", rev)
        if rev
        else (REPO_ROOT / C101 / "claims.merged.json").read_text(encoding="utf-8")
    )
    claims: list[dict[str, Any]] = json.loads(raw)["claims"]
    return claims


def _pin_census(claims: list[dict[str, Any]], rel: str) -> tuple[int, int]:
    """Rows and byte-pin slots that reference ``rel`` -- read off the receipts, not asserted."""
    rows: set[str] = set()
    slots = 0
    for claim in claims:
        for group in PIN_GROUPS:
            entries = (claim.get("pins") or {}).get(group)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and entry.get("path") == rel:
                    rows.add(f"{claim.get('provider')}::{claim.get('upstream_model')}")
                    slots += 1
    return len(rows), slots


def _judge(claims_rel: str, out_name: str) -> dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT))
    from scripts.quality import iter2_model_evidence as judge

    SCRATCH.mkdir(parents=True, exist_ok=True)
    target = SCRATCH / out_name
    rc = judge.main(["--inventory", INVENTORY, "--claims", claims_rel, "--output", str(target)])
    report = json.loads(target.read_text(encoding="utf-8"))
    statuses: dict[str, int] = {}
    for model in report["models"]:
        statuses[model["status"]] = statuses.get(model["status"], 0) + 1
    return {
        "judge_exit": rc,
        "identity_set_sha256": report["inventory"]["identity_set_sha256"][:16],
        "identity_count": report["summary"]["identity_count"],
        "claim_count": report["summary"]["claim_count"],
        "accepted": report["summary"]["offline_acceptance_count"],
        "statuses": statuses,
    }


def _pin_walk(claims: list[dict[str, Any]], rel: str, digest: str | None) -> dict[str, Any]:
    """The shipped staleness predicate, driven by C135's walk -- no copied verdicts."""
    spec = importlib_spec(str(REPO_ROOT / C135_PIN_WALK))
    walk = spec["module"]
    rows, slots, issues, seen = walk._walk_claims(claims, rel, digest)
    return {
        "stale_rows": len(rows),
        "drift_slots": slots,
        "issue_names": sorted(issues),
        "slots_seen": seen,
        "sample": sorted(rows)[:3],
    }


def importlib_spec(path: str) -> dict[str, Any]:
    """Load a shipped probe module by path so its helpers, not copies of them, do the walking."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pinwalk", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    loaded: Any = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO_ROOT))
    spec.loader.exec_module(loaded)
    return {"module": loaded}


def _judge_models(claims_path: Path) -> list[dict[str, Any]]:
    """One in-process ``build_report`` -- the judge's own per-model rows, nothing copied."""
    sys.path.insert(0, str(REPO_ROOT))
    from scripts.quality import iter2_model_evidence as judge

    raw = claims_path.read_bytes()
    report = judge.build_report(
        judge.load_inventory(REPO_ROOT / INVENTORY),
        json.loads(raw.decode("utf-8")),
        claims_sha256=judge.sha256_bytes(raw),
        claims_path=str(claims_path),
    )
    return [{str(key): value for key, value in entry.items()} for entry in report["models"]]


def _inventory_providers() -> list[str]:
    """The provider column of the fixed inventory -- the population the table has to cover."""
    with (REPO_ROOT / INVENTORY).open(encoding="utf-8-sig", newline="") as handle:
        return sorted({str(row["provider"]) for row in csv.DictReader(handle)})


def _provider_counts(models: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Group a report's per-model entries by their own ``provider`` field."""
    counts: dict[str, dict[str, int]] = {}
    for entry in models:
        bucket = counts.setdefault(str(entry["provider"]), {"rows": 0, "accepted": 0})
        bucket["rows"] += 1
        if entry["status"] == ACCEPTED:
            bucket["accepted"] += 1
    return counts


def _delta_face(
    tag: str,
    before: dict[str, dict[str, int]],
    after: dict[str, dict[str, int]],
    *,
    expected_providers: list[str],
    limit: int,
) -> dict[str, Any]:
    """A per-provider before->after face plus the invariants that stop it reading as vacuous."""
    providers = sorted(set(before) | set(after))
    accepted_before = {prov: before.get(prov, {}).get("accepted", 0) for prov in providers}
    accepted_after = {prov: after.get(prov, {}).get("accepted", 0) for prov in providers}
    rows = {prov: after.get(prov, before.get(prov, {})).get("rows", 0) for prov in providers}
    movers = [prov for prov in providers if accepted_before[prov] != accepted_after[prov]]
    regressions = [
        f"{prov}:{accepted_before[prov]}->{accepted_after[prov]}"
        for prov in providers
        if accepted_after[prov] < accepted_before[prov]
    ]
    face: dict[str, Any] = {
        "tag": tag,
        "providers": providers,
        "provider_count": len(providers),
        "rows_by_provider": rows,
        "rows_sum_before": sum(bucket["rows"] for bucket in before.values()),
        "rows_sum_after": sum(bucket["rows"] for bucket in after.values()),
        "accepted_before": accepted_before,
        "accepted_after": accepted_after,
        "accepted_sum_before": sum(accepted_before.values()),
        "accepted_sum_after": sum(accepted_after.values()),
        "movers": movers,
        "mover_deltas": {prov: accepted_after[prov] - accepted_before[prov] for prov in movers},
        "regressions": regressions,
        "provider_labels_match_inventory": providers == expected_providers,
    }
    return face


def _face_verdict(
    face: dict[str, Any],
    *,
    expected_rows: int,
    expected_before: int,
    expected_after: int,
    movers_are_landers: bool = False,
) -> bool:
    """Every invariant of a delta face, against the counts this run judged elsewhere."""
    deltas = face["mover_deltas"]
    landers = sorted(prov for prov, value in face["accepted_after"].items() if value > 0)
    return bool(
        face["provider_labels_match_inventory"]
        and face["rows_sum_before"] == expected_rows
        and face["rows_sum_after"] == expected_rows
        and sum(face["rows_by_provider"].values()) == expected_rows
        and face["accepted_sum_before"] == expected_before
        and face["accepted_sum_after"] == expected_after
        and not face["regressions"]
        and all(value > 0 for value in deltas.values())
        and sum(deltas.values()) == expected_after - expected_before
        and (not movers_are_landers or face["movers"] == landers)
    )


def _print_face(face: dict[str, Any], limit: int) -> None:
    print(
        f"{face['tag']}_providers={face['provider_count']}"
        f" rows_sum {face['rows_sum_before']}|{face['rows_sum_after']}"
        f" accepted {face['accepted_sum_before']} -> {face['accepted_sum_after']}"
        f" movers={len(face['movers'])} regressions={face['regressions']}"
        f" inventory_labels={face['provider_labels_match_inventory']}"
    )
    shown = face["providers"] if limit == 0 else face["providers"][:limit]
    for prov in shown:
        before = face["accepted_before"][prov]
        after = face["accepted_after"][prov]
        print(
            f"provider_row provider={prov} rows={face['rows_by_provider'][prov]}"
            f" accepted {before} -> {after} delta={after - before:+d}"
        )


def _flip_one_accepted(models: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Forge one landed row into a missed one, in memory, in a copy."""
    forged = json.loads(json.dumps(models))
    for entry in sorted(forged, key=lambda item: str(item["model_identity"])):
        if entry.get("status") == ACCEPTED:
            entry["status"] = MISSED
            return forged, str(entry["model_identity"])
    raise RuntimeError("no OFFLINE_ACCEPTED row in the after report to flip")


def _chain_providers() -> list[str]:
    """The 28 members, read from the chain carrier's own exit dictionaries."""
    text = (REPO_ROOT / BUNDLE / "runs" / "chain_summary.txt").read_text(encoding="utf-8")
    match = re.search(r"providers_loop=(\d+) producer_exits=\{([^}]*)\}", text)
    if not match:
        raise RuntimeError("chain_summary.txt carries no providers_loop/producer_exits reading")
    names = re.findall(r"'([a-z_0-9]+)': 0", match.group(2))
    return [str(name) for name in names] if len(names) == int(match.group(1)) else names


def _carrier_audit(chain: list[str]) -> dict[str, Any]:
    """Walk the wave's carrier population and stand behind exactly what the walk finds."""
    runs = REPO_ROOT / BUNDLE / "runs"
    files = sorted(path.name for path in runs.glob("*.txt"))
    self_present = [name for name in files if name == SELF_CARRIER]
    walked = [name for name in files if name != SELF_CARRIER]
    standing = [name for name in walked if name not in SUPERSEDED_CARRIERS]
    bodies = {name: (runs / name).read_text(encoding="utf-8") for name in walked}

    def _has_exit_line(name: str) -> bool:
        lines = [line for line in bodies[name].splitlines() if line.strip()]
        return bool(lines and CARRIER_EXIT_LINE.search(lines[-1]))

    missing_all = [name for name in walked if not _has_exit_line(name)]
    missing_standing = [name for name in standing if not _has_exit_line(name)]
    body = "\n".join(bodies.values())
    chain_lines = re.findall(r"\bchain_\w*_exit\s*=\s*(-?\d+)", body)
    negatives = re.findall(r"\b\w*_(?:rc|exit)\s*=\s*-\d+", body)
    take2 = bodies.get(TAKE2_CARRIER, "")
    counts_line = next((line for line in take2.splitlines() if line.startswith("passed=")), "")
    summary_line = next((line for line in take2.splitlines() if "pytest_summary=" in line), "")
    exit_match = CARRIER_EXIT_LINE.search("\n".join(take2.splitlines()[-1:]))
    summary = (REPO_ROOT / BUNDLE / "runs" / "chain_summary.txt").read_text(encoding="utf-8")
    digits = re.findall(r"(passed|failed)=(\d+)", counts_line)
    return {
        "population": f"{BUNDLE}/runs/*.txt",
        "carrier_files": len(files),
        "walked": len(walked),
        "standing": len(standing),
        "self_carrier": self_present,
        "superseded_present": [name for name in SUPERSEDED_CARRIERS if name in files],
        "missing_exit_line": missing_all,
        "standing_missing_exit_line": missing_standing,
        "standing_with_exit_line": len(standing) - len(missing_standing),
        "producer_carriers": len([name for name in standing if name.startswith("producer_")]),
        "recorder_carriers": len([name for name in standing if name.startswith("recorder_")]),
        "chain_members": len(chain),
        "chain_exit_readings": len(chain_lines),
        "chain_exit_nonzero": sorted({value for value in chain_lines if value != "0"}),
        "negative_readings": sorted(set(negatives)),
        "take2_exists": bool(take2),
        "take2_passed": int(next(value for name, value in digits if name == "passed") or 0)
        if any(name == "passed" for name, _ in digits)
        else -1,
        "take2_failed": int(next(value for name, value in digits if name == "failed") or 0)
        if any(name == "failed" for name, _ in digits)
        else -1,
        "take2_summary": summary_line,
        "take2_exit": int((exit_match.group(0).split("=")[-1]) if exit_match else -1),
        "nonzero_members": "nonzero_members=[]" in summary,
        "merge_idempotent": "merge_idempotent_stable_payload=True" in summary,
        "merge_stable_sha": re.findall(
            r"merge_take[12]_stable_sha[\"=:\s]+\"?([0-9a-f]{12})", summary
        ),
    }


def _keymetrics_face() -> dict[str, Any]:
    """Is KeyMetrics blocked by evidence machinery, or by the installed SDK?"""
    import yfinance

    pkg = Path(yfinance.__file__).resolve().parent
    rc, out = _run(["grep", "-rl", "-e", "valuation-measures", "-e", "peg_ratio", str(pkg)])
    return {
        "sdk_version": yfinance.__version__,
        "class_has_get_valuation_measures": hasattr(yfinance.Ticker, "get_valuation_measures"),
        "package_files_naming_the_surface": len(
            [line for line in out.splitlines() if line.strip()]
        ),
        "grep_exit": rc,
    }


def _seam_kind_census(accepted_rows: list[str]) -> dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))
    import iter2_claims_fixtures as fixtures

    kinds: dict[str, int] = {}
    sdk_rows: list[str] = []
    unmatched: list[str] = []
    for row in accepted_rows:
        provider, model = row.split("::", 1)
        try:
            entry = dict(fixtures.model_entry(provider, model))
        except KeyError:
            unmatched.append(row)
            continue
        seam = entry.get("client_seam") or {}
        kind = str(seam.get("kind", "engine_transport"))
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind == "governed_sdk_session":
            sdk_rows.append(row)
    return {
        "kinds": kinds,
        "accepted_total": len(accepted_rows),
        "sdk_session_rows": len(sdk_rows),
        "unmatched": len(unmatched),
        "unmatched_sample": sorted(unmatched)[:4],
        "sample": sorted(sdk_rows)[:3],
    }


def main(argv: list[str] | None = None) -> int:
    """Print the landing-radius face and verdict on its arms."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-delta-limit", type=int, default=0)
    parser.add_argument("--census-sample-limit", type=int, default=4)
    args = parser.parse_args(argv)

    arms: list[tuple[str, bool]] = []
    print(f"# carrier for {Path(__file__).name} (wave-32 landing radius and attribution)")
    print(f"# argv: {sys.executable} {Path(__file__).relative_to(REPO_ROOT)} {sys.argv[1:]}")
    print(
        f"# judge argv (disk): {sys.executable} scripts/quality/iter2_model_evidence.py"
        f" --inventory {INVENTORY} --claims {C101}/claims.merged.json"
        " --output <bundle>/runs/judge.txt"
    )
    print(
        "# pre-chain judge argv is the same with --claims <HEAD copy under /tmp>;"
        " nothing pinned is written"
    )

    head_raw = _git_show(f"{C101}/claims.merged.json")
    head_claims: list[dict[str, Any]] = json.loads(head_raw)["claims"]
    disk_claims = _claims()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "claims.head.json").write_text(head_raw, encoding="utf-8")

    probe_rows, probe_slots = _pin_census(head_claims, RUNNER)
    fixtures_rows, fixtures_slots = _pin_census(head_claims, FIXTURES)
    disk_probe_rows, disk_probe_slots = _pin_census(disk_claims, RUNNER)
    print(
        f"A_pin_table_head rows_pinning_probe_runner={probe_rows}/{len(head_claims)}"
        f" slots={probe_slots} rows_pinning_fixtures={fixtures_rows}"
        f" fixtures_slots={fixtures_slots} disk_rows_pinning_probe_runner={disk_probe_rows}"
        f"/{len(disk_claims)} disk_slots={disk_probe_slots}"
    )
    arms.append(("pin_table_read", probe_rows > 0 and disk_probe_rows > probe_rows))

    pre = _walk_pre_chain()
    print(
        f"A_pre_chain_judged accepted={pre['accepted']}/{pre['identity_count']}"
        f" statuses={pre['statuses']} claims={pre['claim_count']}"
        f" identity_set={pre['identity_set_sha256']} judge_exit={pre['judge_exit']}"
    )
    arms.append(
        (
            "pre_chain_zero_accepted",
            pre["accepted"] == 0 and pre["statuses"].get("STALE", 0) == probe_rows,
        )
    )

    post = _judge(f"{C101}/claims.merged.json", "judge.disk.json")
    print(
        f"B_post_chain accepted={post['accepted']}/{post['identity_count']}"
        f" statuses={post['statuses']} claims={post['claim_count']}"
        f" identity_set={post['identity_set_sha256']} judge_exit={post['judge_exit']}"
    )
    arms.append(
        (
            "post_chain_zero_stale",
            post["judge_exit"] == 0
            and post["statuses"].get("STALE", 0) == 0
            and post["accepted"] == disk_probe_rows,
        )
    )

    walk_head = _pin_walk(head_claims, RUNNER, None)
    walk_disk = _pin_walk(disk_claims, RUNNER, None)
    forged = _pin_walk(disk_claims, RUNNER, "0" * 64)
    partial = _pin_walk(disk_claims, "opendata/data/providers/yfinance/models/_sdk.py", "0" * 64)
    print(
        f"C_drift head_unsimulated stale_rows={walk_head['stale_rows']}"
        f" slots={walk_head['drift_slots']} issues={walk_head['issue_names']}"
    )
    print(
        f"C_drift disk_unsimulated stale_rows={walk_disk['stale_rows']}"
        f" issues={walk_disk['issue_names']}"
    )
    print(
        f"C_counterfact forged_probe_digest stale_rows={forged['stale_rows']}"
        f"/{post['identity_count']} slots={forged['drift_slots']}"
        f" partial_control=_sdk.py rows={partial['stale_rows']}"
        f" sample={forged['sample']}"
    )
    arms.append(
        (
            "drift_detector_bites",
            walk_head["stale_rows"] == probe_rows
            and walk_disk["stale_rows"] == 0
            and forged["stale_rows"] == post["accepted"] > 0
            and 0 < partial["stale_rows"] < forged["stale_rows"],
        )
    )

    expected_providers = _inventory_providers()
    inventory_rows = sum(1 for _ in _inventory_provider_rows())
    before_models = _judge_models(SCRATCH / "claims.head.json")
    after_models = _judge_models(REPO_ROOT / C101 / "claims.merged.json")
    real_face = _delta_face(
        "D_provider_delta",
        _provider_counts(before_models),
        _provider_counts(after_models),
        expected_providers=expected_providers,
        limit=args.provider_delta_limit,
    )
    _print_face(real_face, args.provider_delta_limit)
    real_green = _face_verdict(
        real_face,
        expected_rows=inventory_rows,
        expected_before=pre["accepted"],
        expected_after=post["accepted"],
        movers_are_landers=True,
    )
    print(
        f"D_provider_delta_summary providers={real_face['provider_count']}"
        f" inventory_rows={inventory_rows} rows_sum={real_face['rows_sum_after']}"
        f" accepted {real_face['accepted_sum_before']} -> {real_face['accepted_sum_after']}"
        f" movers={len(real_face['movers'])} regressions={real_face['regressions']}"
        f" mover_deltas={real_face['mover_deltas']} green={real_green}"
    )
    arms.append(
        (
            "provider_delta_invariants",
            real_green
            and real_face["accepted_sum_after"] == 317
            and real_face["regressions"] == [],
        )
    )

    landed_before = json.loads(_git_show(LANDED_REPORT))
    landed_models = [{str(k): v for k, v in entry.items()} for entry in landed_before["models"]]
    landed_face = _delta_face(
        "D2_landed_generation",
        _provider_counts(landed_models),
        _provider_counts(after_models),
        expected_providers=expected_providers,
        limit=args.provider_delta_limit,
    )
    landed_green = _face_verdict(
        landed_face,
        expected_rows=inventory_rows,
        expected_before=sum(
            bucket["accepted"] for bucket in _provider_counts(landed_models).values()
        ),
        expected_after=post["accepted"],
    )
    print(
        f"D2_landed_report file={LANDED_REPORT.split('/')[-1]}"
        f" providers={landed_face['provider_count']}"
        f" accepted {landed_face['accepted_sum_before']} -> {landed_face['accepted_sum_after']}"
        f" movers={landed_face['movers']} regressions={landed_face['regressions']}"
        f" yfinance {landed_face['accepted_before']['yfinance']} ->"
        f" {landed_face['accepted_after']['yfinance']} green={landed_green}"
    )
    arms.append(
        (
            "attribution_one_mover",
            landed_green
            and landed_face["movers"] == ["yfinance"]
            and landed_face["mover_deltas"] == {"yfinance": 26}
            and landed_face["accepted_after"] == real_face["accepted_after"],
        )
    )

    flipped_models, flipped_row = _flip_one_accepted(after_models)
    control_face = _delta_face(
        "D3_control",
        _provider_counts(after_models),
        _provider_counts(flipped_models),
        expected_providers=expected_providers,
        limit=args.provider_delta_limit,
    )
    control_green = _face_verdict(
        control_face,
        expected_rows=inventory_rows,
        expected_before=post["accepted"],
        expected_after=post["accepted"] - 1,
    )
    print(
        f"D3_control_forged row={flipped_row} status {ACCEPTED} -> {MISSED} (in memory only)"
        f" regressions={control_face['regressions']}"
        f" accepted {control_face['accepted_sum_before']} -> {control_face['accepted_sum_after']}"
        f" expected_after={post['accepted']} face_bites={not control_green}"
    )
    recheck = _delta_face(
        "D3_recheck",
        _provider_counts(_judge_models(SCRATCH / "claims.head.json")),
        _provider_counts(_judge_models(REPO_ROOT / C101 / "claims.merged.json")),
        expected_providers=expected_providers,
        limit=args.provider_delta_limit,
    )
    recheck_green = _face_verdict(
        recheck,
        expected_rows=inventory_rows,
        expected_before=pre["accepted"],
        expected_after=post["accepted"],
        movers_are_landers=True,
    )
    print(
        f"D3_recheck_real_data accepted {recheck['accepted_sum_before']} ->"
        f" {recheck['accepted_sum_after']} movers={len(recheck['movers'])}"
        f" regressions={recheck['regressions']} green={recheck_green}"
        f" identical_to_real_face={recheck['accepted_after'] == real_face['accepted_after']}"
    )
    arms.append(
        (
            "regression_arm_falsifies",
            not control_green
            and bool(control_face["regressions"])
            and control_face["accepted_sum_after"] == post["accepted"] - 1
            and recheck_green
            and recheck["regressions"] == []
            and recheck["accepted_after"] == real_face["accepted_after"],
        )
    )

    accepted_rows = sorted(
        f"{entry['provider']}::{entry['upstream_model']}" for entry in _accepted_entries()
    )
    seam_face = _seam_kind_census(accepted_rows)
    print(
        f"E_seam_kinds accepted_rows={seam_face['accepted_total']} kinds={seam_face['kinds']}"
        f" governed_sdk_session_rows={seam_face['sdk_session_rows']}"
        f" rows_without_witness_entry={seam_face['unmatched']}"
        f" sample={seam_face['unmatched_sample']}"
    )
    arms.append(
        (
            "new_kind_is_yfinance_only",
            seam_face["sdk_session_rows"] == 26
            and seam_face["kinds"].get("governed_sdk_session") == 26,
        )
    )

    census_rc, census_out = _run(
        [sys.executable, str(REPO_ROOT / CENSUS)],
        env={**dict(_env_with_path()), "PYTHONPATH": str(REPO_ROOT)},
    )
    face = next(
        (line for line in census_out.splitlines() if "residue_axis_census_face" in line), ""
    )
    failed_arms = [
        line.split()[0] + "_PASS=False"
        for line in census_out.splitlines()
        if line.startswith(("A_", "B_", "C_", "D_", "E_", "F_", "G_", "H_"))
        and "PASS=False" in line
    ]
    print(f"F_census_rerun exit={census_rc} {face} failing_arms={failed_arms}")
    bare_rc, bare_out = _run([sys.executable, str(REPO_ROOT / CENSUS)])
    print(
        f"F_census_without_pythonpath exit={bare_rc}"
        f" error={next((line for line in bare_out.splitlines() if 'rror' in line), 'none')[:110]}"
    )
    c136 = (REPO_ROOT / C136_CARRIER).read_text(encoding="utf-8").splitlines()
    before_line = next((line for line in c136 if line.startswith("population:")), "")
    print(f"F_census_previous_generation file={C136_CARRIER.split('/')[-1]} {before_line}")
    residue_match = re.search(r"residue=(\d+)", face)
    residue = int(residue_match.group(1)) if residue_match else -1
    arms.append(
        (
            "census_flip_and_its_red_arm",
            residue == 33
            and "arms_passed=7" in face
            and "arms_failed=1" in face
            and any("B_landed_negative" in item for item in failed_arms)
            and "residue=59" in before_line
            and bare_rc != 0,
        )
    )

    audit = _carrier_audit(_chain_providers())
    print(
        f"G_carriers population={audit['population']} files={audit['carrier_files']}"
        f" walked={audit['walked']} standing={audit['standing']}"
        f" with_exit_line={audit['standing_with_exit_line']}"
        f" self_carrier_excluded={audit['self_carrier']}"
        f" superseded={audit['superseded_present']} missing_exit_line={audit['missing_exit_line']}"
        f" standing_missing={audit['standing_missing_exit_line']}"
    )
    print(
        f"G_carriers_chain producer_carriers={audit['producer_carriers']}"
        f" recorder_carriers={audit['recorder_carriers']} chain_members={audit['chain_members']}"
        f" chain_exit_readings={audit['chain_exit_readings']}"
        f" chain_exit_nonzero={audit['chain_exit_nonzero']}"
        f" negative_readings={audit['negative_readings']}"
        f" nonzero_members_empty={audit['nonzero_members']}"
        f" merge_idempotent={audit['merge_idempotent']}"
        f" stable_shas={audit['merge_stable_sha']}"
    )
    print(
        f"G_repin_take2 exists={audit['take2_exists']} passed={audit['take2_passed']}"
        f" failed={audit['take2_failed']} exit={audit['take2_exit']}"
        f" summary={audit['take2_summary']}"
    )
    arms.append(
        (
            "carriers_signed",
            audit["missing_exit_line"] == audit["superseded_present"]
            and audit["standing_missing_exit_line"] == []
            and audit["standing_with_exit_line"] == audit["standing"]
            and audit["producer_carriers"] == audit["recorder_carriers"] == audit["chain_members"]
            and audit["chain_exit_nonzero"] == []
            and audit["negative_readings"] == []
            and audit["nonzero_members"]
            and audit["merge_idempotent"]
            and len(set(audit["merge_stable_sha"])) == 1
            and audit["take2_exists"]
            and audit["take2_exit"] == 0
            and audit["take2_passed"] == 117
            and audit["take2_failed"] == 0,
        )
    )

    km = _keymetrics_face()
    print(
        f"H_keymetrics sdk={km['sdk_version']} class_has_get_valuation_measures="
        f"{km['class_has_get_valuation_measures']} package_files_naming_the_surface="
        f"{km['package_files_naming_the_surface']} (grep exit={km['grep_exit']})"
        f" census_bucket_for_the_row=PRODUCIBLE_NOW/DOCUMENT_REACHABLE -> instrument blind spot"
    )
    arms.append(
        (
            "upstream_block_measured",
            km["class_has_get_valuation_measures"] is False
            and km["package_files_naming_the_surface"] == 0,
        )
    )

    passed = sum(1 for _, ok in arms if ok)
    failed = [name for name, ok in arms if not ok]
    print(
        f"landing_radius_face arms={len(arms)} arms_passed={passed} arms_failed={len(failed)}"
        f" failed={failed} pre_chain_accepted={pre['accepted']}"
        f" post_chain_accepted={post['accepted']}"
        f" radius_rows={probe_rows}->{disk_probe_rows} census_residue=59->{residue}"
        f" identity_set={post['identity_set_sha256']} PASS={not failed}"
    )
    rc = 0 if not failed else 1
    print(f"w32_landing_radius_exit={rc}")
    return rc


def _env_with_path() -> dict[str, str]:
    import os

    return dict(os.environ)


def _walk_pre_chain() -> dict[str, Any]:
    """Judge HEAD's claims against today's tree -- that is the pre-chain state, measured."""
    return _judge(str(SCRATCH / "claims.head.json"), "judge.head.json")


def _inventory_provider_rows() -> list[dict[str, str]]:
    """The fixed inventory's rows, counted rather than asserted to be 350."""
    with (REPO_ROOT / INVENTORY).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _accepted_entries() -> list[dict[str, Any]]:
    """The accepted identities, read from the judge report the chain itself wrote."""
    report = json.loads((REPO_ROOT / LANDED_REPORT).read_text(encoding="utf-8"))
    return [
        {"provider": model["provider"], "upstream_model": model["model_identity"].split("::")[-1]}
        for model in report["models"]
        if model["status"] == ACCEPTED
    ]


if __name__ == "__main__":
    raise SystemExit(main())
